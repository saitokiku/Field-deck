"""Sessions, marks, notes and the correlation query.

The unified timeline is the reason FieldDeck keeps a session at all: CAN
frames, supply measurements and an operator's mark have to land on *one*
monotonic axis so that "what happened 300 ms before that?" is a query rather
than an afternoon with three log files.  The test below therefore drives two
subsystems on purpose — a bench supply and a CAN interface — and then asks
one window query to hand both of them back.
"""

from __future__ import annotations

import json

from fielddeck.common.events import EventType
from fielddeck.common.models import PermissionLevel
from fielddeck.daemon.client import InstrumentClient
from fielddeck.daemon.service import InstrumentDaemon

from .conftest import SIM_CAN, SIM_PSU, arm


async def test_marks_and_notes_are_recorded_and_readable(
    daemon: InstrumentDaemon, client: InstrumentClient, session: str
) -> None:
    mark = (await client.execute("session.mark", {"label": "power-up", "note": "24 V"})).result[
        "mark"
    ]
    assert mark["monotonic_ns"] > 0
    assert mark["source"] == "fdctl"

    await client.execute("session.note", {"text": "controller ticks at 100 ms"})
    await client.execute("session.mark", {"label": "fault-seen"})

    summary = (await client.execute("session.summary", {"session_id": session})).result
    assert [row["label"] for row in summary["marks"]] == ["power-up", "fault-seen"]
    assert "controller ticks at 100 ms" in summary["notes"]

    # session.json is written as it happens, not at close: a session that
    # loses power mid-bench still has its marks on disk.
    on_disk = json.loads(
        (daemon.sessions.sessions_dir / session / "session.json").read_text(encoding="utf-8")
    )
    assert [entry["label"] for entry in on_disk["marks"]] == ["power-up", "fault-seen"]
    assert on_disk["simulated"] is True


async def test_window_query_returns_two_subsystems_on_one_axis(
    client: InstrumentClient, session: str
) -> None:
    """The flagship correlation query, against a bench doing two things."""
    await arm(client, PermissionLevel.QUERY, PermissionLevel.POWER, ttl_s=120.0)

    await client.execute("psu.set", {"device": SIM_PSU, "voltage": 12.0, "current_limit": 0.5})
    await client.execute("psu.output", {"device": SIM_PSU, "enabled": True, "lease_ttl_s": 60.0})
    try:
        for _ in range(3):
            await client.execute("psu.measure", {"device": SIM_PSU})
        await client.execute(
            "can.capture",
            {"device": SIM_CAN, "duration_s": 0.5, "label": "correlate"},
            timeout_s=30.0,
        )
        mark = (await client.execute("session.mark", {"label": "look-here"})).result["mark"]
    finally:
        await client.execute("psu.output", {"device": SIM_PSU, "enabled": False})

    window = (
        await client.execute(
            "session.window",
            {
                "session_id": session,
                "center_monotonic_ns": mark["monotonic_ns"],
                "before_ms": 60_000.0,
                "after_ms": 5_000.0,
            },
        )
    ).result

    assert window["start_monotonic_ns"] < mark["monotonic_ns"] < window["end_monotonic_ns"]

    # Evidence from the supply: real measurements, not action metadata.
    quantities = {row["quantity"] for row in window["measurements"]}
    assert {"psu.voltage", "psu.current"} <= quantities
    assert all(row["device_id"] == SIM_PSU for row in window["measurements"])
    currents = [row for row in window["measurements"] if row["quantity"] == "psu.current"]
    assert any(row["value"] > 0 for row in currents)

    # Evidence from the bus, on the same axis, in the same answer.
    devices = {row["device_id"] for row in window["events"] if row["device_id"]}
    assert {SIM_CAN, SIM_PSU} <= devices
    types = {row["type"] for row in window["events"]}
    assert str(EventType.CAPTURE_STARTED) in types
    assert str(EventType.OUTPUT_ENABLED) in types

    # And the operator's own mark, so the window is anchored to what a person
    # saw rather than to what the software noticed.
    assert [row["label"] for row in window["marks"]] == ["look-here"]

    ordered = [row["monotonic_ns"] for row in window["events"]]
    assert ordered == sorted(ordered)


async def test_window_can_be_centred_on_an_event_type(
    client: InstrumentClient, session: str
) -> None:
    """ "Around the first capture" without the caller knowing any timestamps."""
    await client.execute(
        "can.capture", {"device": SIM_CAN, "duration_s": 0.4, "label": "anchor"}, timeout_s=30.0
    )
    window = (
        await client.execute(
            "session.window",
            {
                "session_id": session,
                "around_event_type": str(EventType.CAPTURE_STARTED),
                "before_ms": 2_000.0,
                "after_ms": 2_000.0,
            },
        )
    ).result
    assert any(row["type"] == str(EventType.CAPTURE_STARTED) for row in window["events"])


async def test_session_summary_reports_what_was_recorded(
    client: InstrumentClient, session: str
) -> None:
    await arm(client, PermissionLevel.QUERY, ttl_s=60.0)
    await client.execute("psu.measure", {"device": SIM_PSU})
    await client.execute(
        "serial.capture",
        {"device": "sim:serial:sim-uart-0", "duration_s": 0.4, "label": "summary"},
        timeout_s=30.0,
    )
    await client.execute("session.mark", {"label": "done"})

    summary = (await client.execute("session.summary", {"session_id": session})).result
    assert summary["id"] == session
    assert summary["simulated"] is True
    assert "psu.voltage" in summary["measurement_quantities"]
    assert summary["timeline"]["events"] > 0
    assert summary["timeline"]["artifacts"] >= 2
    assert {row["kind"] for row in summary["artifacts"]} == {"serial"}
    assert summary["software"]["fielddeck"]


async def test_a_closed_session_is_still_queryable_from_disk(
    client: InstrumentClient, session: str
) -> None:
    """Stopping a session finalises it; it must not take the evidence with it."""
    await client.execute(
        "can.capture", {"device": SIM_CAN, "duration_s": 0.4, "label": "closing"}, timeout_s=30.0
    )
    await client.execute("session.mark", {"label": "last-thing"})
    stopped = (await client.execute("session.stop")).result["session"]
    assert stopped["state"] == "CLOSED"
    assert stopped["ended_utc_ns"] > stopped["started_utc_ns"]

    listed = (await client.execute("session.list")).result["sessions"]
    assert any(entry["id"] == session and not entry["active"] for entry in listed)

    # Read back through the same action a client would use next week.
    reopened = (await client.execute("session.get", {"session_id": session})).result
    assert reopened["timeline"]["artifacts"] >= 1
    events = (await client.execute("session.events", {"session_id": session, "limit": 500})).result
    assert events["count"] > 0
    assert any(row["type"] == str(EventType.SESSION_STOPPED) for row in events["events"])
