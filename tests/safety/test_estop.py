"""Emergency stop, end to end.

Four properties, all of them things an operator bets equipment on:

* it revokes every authorization, so nothing survives the stop by accident;
* it drives *every* device to its safe state, not just the one that faulted;
* it never destroys evidence — the moments either side of a stop are usually
  the most valuable data in the session;
* it does not block the safe direction.  Turning an output off during a
  latched stop has to work, or the stop has made the bench harder to make
  safe rather than easier.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fielddeck.common.errors import EstopActive, PermissionDenied
from fielddeck.common.events import EventType
from fielddeck.common.models import ClientSource, PermissionLevel
from fielddeck.daemon.client import InstrumentClient
from fielddeck.daemon.service import InstrumentDaemon

SIM_PSU = "sim:visa:sim-psu-0"
SIM_CAN = "sim:can:can0"
SIM_SERIAL = "sim:serial:sim-uart-0"


async def energise(client: InstrumentClient, arm) -> None:
    """Get the simulated supply into the state a stop is supposed to end."""
    await arm(PermissionLevel.POWER)
    await client.execute("psu.set", {"device": SIM_PSU, "voltage": 12.0, "current_limit": 0.5})
    result = await client.execute(
        "psu.output", {"device": SIM_PSU, "enabled": True, "lease_ttl_s": 60}
    )
    assert result.result["output"] is True


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------


async def test_estop_revokes_every_grant(
    client: InstrumentClient, arm, daemon: InstrumentDaemon
) -> None:
    await arm(PermissionLevel.QUERY)
    await arm(PermissionLevel.POWER)
    assert len(daemon.safety.snapshot().grants) == 2

    reply = await client.call("safety.estop", {"reason": "smoke from the DUT"})

    assert reply["estop"] is True
    snapshot = daemon.safety.snapshot()
    assert snapshot.grants == []
    assert snapshot.armed_permissions == []
    assert snapshot.state_word == "ESTOP"

    # Each authorization is taken away visibly, not just implied by the stop.
    revoked = [e for e in daemon.bus.recent(limit=200) if e.type is EventType.ARM_REVOKED]
    assert {str(e.permission) for e in revoked} == {"QUERY", "POWER"}


async def test_after_a_stop_an_armed_action_is_refused_for_the_stop(
    client: InstrumentClient, arm
) -> None:
    """The reason reported is the latch, which is the thing to clear first."""
    await arm(PermissionLevel.POWER)
    await client.call("safety.estop", {"reason": "test"})

    with pytest.raises(EstopActive) as caught:
        await client.execute("psu.set", {"device": SIM_PSU, "voltage": 5.0})
    assert "acknowledge" in str(caught.value)
    assert caught.value.preserved == "captured data and session metadata are intact"


async def test_clearing_the_latch_does_not_re_arm_anything(
    client: InstrumentClient, arm, daemon: InstrumentDaemon
) -> None:
    await arm(PermissionLevel.POWER)
    await client.call("safety.estop", {"reason": "test"})
    await client.call("safety.estop_clear", {})

    snapshot = daemon.safety.snapshot()
    assert not snapshot.estop_active
    assert snapshot.armed_permissions == []
    assert snapshot.state_word == "SAFE"


# ---------------------------------------------------------------------------
# Safe state on every device
# ---------------------------------------------------------------------------


async def test_estop_drives_every_device_to_safe_state(
    client: InstrumentClient, arm, daemon: InstrumentDaemon
) -> None:
    await energise(client, arm)
    device_ids = {driver.device_id for driver in daemon.registry.drivers}
    assert SIM_PSU in device_ids

    reply = await client.call("safety.estop", {"reason": "operator pressed stop"})

    safed = {str(entry["device"]) for entry in reply["safe_state"]}
    assert safed == device_ids, "a device that was skipped is a device left live"

    applied = [
        event
        for event in daemon.bus.recent(limit=400)
        if event.type is EventType.SAFE_STATE_APPLIED and "ESTOP" in (event.message or "")
    ]
    assert {event.device_id for event in applied} == device_ids

    status = (await client.execute("psu.status", {"device": SIM_PSU})).result
    assert status["output"] is False
    assert status["mode"] == "OFF"


async def test_estop_surrenders_the_output_lease(
    client: InstrumentClient, arm, daemon: InstrumentDaemon
) -> None:
    await energise(client, arm)
    assert daemon.safety.leases.active()

    reply = await client.call("safety.estop", {"reason": "test"})

    assert reply["surrendered_leases"]
    assert daemon.safety.leases.active() == []


# ---------------------------------------------------------------------------
# Output-off during a latched stop
# ---------------------------------------------------------------------------


async def test_turning_an_output_off_is_still_permitted_during_a_stop(
    client: InstrumentClient, arm
) -> None:
    """The safe direction is never blocked by the stop or by a lapsed grant.

    ``psu.output`` is declared POWER, but its permission resolver reads the
    parameters: disabling resolves to PASSIVE and the action is marked
    ``allowed_during_estop``, so this call goes through with every grant
    revoked and the latch closed.
    """
    await energise(client, arm)
    await client.call("safety.estop", {"reason": "test"})

    result = await client.execute("psu.output", {"device": SIM_PSU, "enabled": False})

    assert result.ok
    assert result.permission is PermissionLevel.PASSIVE
    assert result.result["output"] is False


async def test_turning_an_output_on_during_a_stop_is_refused(
    client: InstrumentClient, arm
) -> None:
    """Enabling stays refused while the stop is latched.

    Note *which* refusal it is.  ``psu.output`` declares
    ``allowed_during_estop=True`` for the sake of the disable direction, but
    that only waives the latch for calls whose *resolved* permission is
    PASSIVE.  Enabling resolves to POWER, so this is refused by the latch
    itself -- ``EstopActive``, naming the stop -- rather than by the grant
    lookup happening to come up empty.

    The distinction matters to whoever reads the error: "emergency stop is
    latched" tells them what to do next, and "no active POWER grant" sends
    them off to arm one.
    """
    await arm(PermissionLevel.POWER)
    await client.call("safety.estop", {"reason": "test"})

    with pytest.raises(EstopActive):
        await client.execute("psu.output", {"device": SIM_PSU, "enabled": True})

    status = (await client.execute("psu.status", {"device": SIM_PSU})).result
    assert status["output"] is False


async def test_a_latched_stop_blocks_energising_even_without_ack_policy(
    daemon_factory, safety_config
) -> None:
    """A latched emergency stop must outrank an arm grant, whatever the policy.

    Regression test. ``allowed_during_estop`` used to be read straight off the
    ActionSpec, so the flag ``psu.output`` sets in order to let you *disable* a
    rail during a latched stop also let you *enable* one. Under the default
    ``estop_requires_ack`` policy the grant lookup happened to catch it, which
    meant the bypass was closed by accident rather than by design; with the ack
    requirement relaxed the rail came up with the stop still engaged.

    The latch is now waived only when the *resolved* permission is PASSIVE.
    """
    permissive = safety_config.model_copy(update={"estop_requires_ack": False})
    daemon = await daemon_factory(safety_config=permissive)

    async with InstrumentClient(daemon.socket_path, source=ClientSource.FDCTL) as client:
        await client.call("safety.estop", {"reason": "operator pressed stop"})
        await client.call("safety.arm", {"permission": "POWER", "ttl_s": 60})

        with pytest.raises((EstopActive, PermissionDenied)):
            await client.execute("psu.output", {"device": SIM_PSU, "enabled": True})

        assert daemon.safety.snapshot().estop_active
        status = (await client.execute("psu.status", {"device": SIM_PSU})).result
        assert status["output"] is False, "the rail came up while the stop was latched"


async def test_reading_the_world_still_works_during_a_stop(client: InstrumentClient) -> None:
    """An operator who cannot see what happened cannot decide what to do next."""
    await client.call("safety.estop", {"reason": "test"})

    assert (await client.execute("system.status")).ok
    assert (await client.execute("device.list")).ok
    assert (await client.execute("psu.status", {"device": SIM_PSU})).ok
    assert (await client.execute("action.list")).ok


async def test_a_passive_action_not_marked_estop_safe_is_still_blocked(
    client: InstrumentClient,
) -> None:
    """PASSIVE is not a blanket exemption; the action has to declare itself.

    ``can.listen`` is passive and harmless, and it is still refused: during a
    latched stop the default is "do nothing until a human has looked", and an
    action opts out of that explicitly or not at all.
    """
    await client.call("safety.estop", {"reason": "test"})
    with pytest.raises(EstopActive):
        await client.execute("can.listen", {"device": SIM_CAN, "duration_s": 0.05})


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


async def test_estop_preserves_captured_data(
    client: InstrumentClient, arm, daemon: InstrumentDaemon
) -> None:
    """Files and timeline rows both survive the stop, byte for byte."""
    session_id = (await client.execute("session.start", {"name": "estop evidence"})).result[
        "session"
    ]["id"]
    await client.execute("session.mark", {"label": "before the fault"})
    can_capture = await client.execute(
        "can.capture", {"device": SIM_CAN, "duration_s": 0.3, "label": "evidence"}
    )
    serial_capture = await client.execute(
        "serial.capture", {"device": SIM_SERIAL, "duration_s": 0.3, "label": "evidence"}
    )
    await energise(client, arm)

    recorder = daemon.sessions.recorder
    assert recorder is not None
    root: Path = recorder.root
    artifacts = {
        can_capture.result["artifact"]["relative_path"]: can_capture.result["artifact"]["sha256"],
        serial_capture.result["artifact"]["relative_path"]: serial_capture.result["artifact"][
            "sha256"
        ],
    }
    before = {name: (root / name).read_bytes() for name in artifacts}
    assert all(len(payload) > 0 for payload in before.values())

    await client.call("safety.estop", {"reason": "current excursion"})

    # 1. The bytes on disk are untouched.
    for name, payload in before.items():
        assert (root / name).read_bytes() == payload

    # 2. The session still owns them, with the same content hash.
    registered = {
        entry["relative_path"]: entry["sha256"]
        for entry in (await client.execute("session.get", {"session_id": session_id})).result[
            "artifacts"
        ]
    }
    for name, digest in artifacts.items():
        assert registered[name] == digest

    # 3. The timeline still has the capture, the mark, and now the stop.
    events = (await client.execute("session.events", {"limit": 2000})).result["events"]
    types = [row["type"] for row in events]
    assert str(EventType.CAPTURE_STARTED) in types
    assert str(EventType.CAPTURE_STOPPED) in types
    assert str(EventType.ESTOP) in types
    marks = (await client.execute("session.summary", {"session_id": session_id})).result["marks"]
    assert [mark["label"] for mark in marks] == ["before the fault"]

    # 4. And the session is still open, so the records after the stop land in
    #    the same place as the ones before it.
    assert daemon.sessions.current_id == session_id


async def test_the_stop_reply_states_what_was_preserved(client: InstrumentClient) -> None:
    reply = await client.call("safety.estop", {"reason": "test"})
    assert reply["evidence"] == "all captured data and session metadata preserved"


async def test_the_first_reason_is_the_one_that_sticks(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    """A second stop must not overwrite why the first one happened."""
    await client.call("safety.estop", {"reason": "the real reason"})
    await client.call("safety.estop", {"reason": "someone pressed it again"})
    assert daemon.safety.snapshot().estop_reason == "the real reason"
