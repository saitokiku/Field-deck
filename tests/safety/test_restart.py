"""What survives a restart, and what deliberately does not.

Authority does not survive.  Grants, leases and a latched stop live only in
the daemon's memory, so a restart — or a power cut, which is the same thing on
a field device — always comes back SAFE.  There is no persistence to
accidentally restore, and that is the design, not an omission.

Evidence does survive.  Sessions, captures and the timeline are written as
they happen, so the restart that follows a fault still has the fault in it.
"""

from __future__ import annotations

from pathlib import Path

from fielddeck.common.events import EventType
from fielddeck.common.models import ClientSource, PermissionLevel
from fielddeck.daemon.client import InstrumentClient

SIM_PSU = "sim:visa:sim-psu-0"
SIM_CAN = "sim:can:can0"


async def test_a_restarted_daemon_starts_safe_with_no_grants(daemon_factory) -> None:
    """Armed, energised and latched before the stop; SAFE after the start."""
    first = await daemon_factory()
    async with InstrumentClient(first.socket_path, source=ClientSource.FDCTL) as client:
        await client.call("safety.arm", {"permission": "POWER", "ttl_s": 300})
        await client.call("safety.arm", {"permission": "QUERY", "ttl_s": 300})
        await client.execute("psu.set", {"device": SIM_PSU, "voltage": 12.0})
        await client.execute("psu.output", {"device": SIM_PSU, "enabled": True, "lease_ttl_s": 300})
        await client.call("safety.estop", {"reason": "fault before the restart"})

        snapshot = first.safety.snapshot()
        assert snapshot.estop_active
        assert first.safety.leases.active() == []

    await first.stop()
    second = await daemon_factory()

    snapshot = second.safety.snapshot()
    assert snapshot.grants == []
    assert snapshot.leases == []
    assert snapshot.armed_permissions == []
    assert not snapshot.estop_active, "a latched stop must not be restored across a restart"
    assert snapshot.state_word == "SAFE"

    async with InstrumentClient(second.socket_path, source=ClientSource.FDCTL) as client:
        status = (await client.execute("system.status")).result
        assert status["safety"]["state"] == "SAFE"
        assert status["safety"]["armed"] == []
        assert status["safety"]["leases"] == []
        # And the hardware itself, not just the bookkeeping.
        assert (await client.execute("psu.status", {"device": SIM_PSU})).result["output"] is False


async def test_boot_drives_every_device_to_safe_state_before_serving(daemon_factory) -> None:
    """Nothing can connect until the bench is in a known state.

    ``start()`` applies safe state before the RPC server begins listening, so
    the first client to connect cannot observe — or extend — whatever the
    hardware was doing when the last daemon died.
    """
    daemon = await daemon_factory()
    boot_events = daemon.bus.recent(limit=400)
    safed = [event for event in boot_events if event.type is EventType.SAFE_STATE_APPLIED]
    started = [event for event in boot_events if event.type is EventType.DAEMON_STARTED]

    assert {event.device_id for event in safed} >= {SIM_PSU, SIM_CAN}
    assert started, "the daemon never announced itself"
    # Ordering, on the shared monotonic axis: safe first, ready second.
    assert max(event.seq for event in safed) < started[-1].seq


async def test_evidence_outlives_the_daemon_that_recorded_it(daemon_factory) -> None:
    """The session, its artifact and its timeline are all still there."""
    first = await daemon_factory()
    async with InstrumentClient(first.socket_path, source=ClientSource.FDCTL) as client:
        session_id = (await client.execute("session.start", {"name": "before restart"})).result[
            "session"
        ]["id"]
        await client.execute("session.mark", {"label": "the interesting moment"})
        capture = await client.execute(
            "can.capture", {"device": SIM_CAN, "duration_s": 0.3, "label": "restart"}
        )
        artifact = capture.result["artifact"]
        recorder = first.sessions.recorder
        assert recorder is not None
        root: Path = recorder.root
        payload = (root / artifact["relative_path"]).read_bytes()
        assert payload

    # A restart, without a clean session.stop from the client.
    await first.stop()
    second = await daemon_factory()

    async with InstrumentClient(second.socket_path, source=ClientSource.FDCTL) as client:
        listed = {
            entry["id"]: entry
            for entry in (await client.execute("session.list")).result["sessions"]
        }
        assert session_id in listed
        assert listed[session_id]["active"] is False

        stored = (await client.execute("session.get", {"session_id": session_id})).result
        relative = {entry["relative_path"]: entry for entry in stored["artifacts"]}
        assert artifact["relative_path"] in relative
        assert relative[artifact["relative_path"]]["sha256"] == artifact["sha256"]

        rows = (await client.execute("session.events", {"session_id": session_id})).result["events"]
        assert str(EventType.CAPTURE_STARTED) in [row["type"] for row in rows]
        marks = (await client.execute("session.summary", {"session_id": session_id})).result[
            "marks"
        ]
        assert [mark["label"] for mark in marks] == ["the interesting moment"]

    # The bytes are untouched by the shutdown that closed the session.
    assert (root / artifact["relative_path"]).read_bytes() == payload


async def test_a_second_daemon_refuses_to_bind_over_a_live_one(daemon_factory) -> None:
    """Two daemons on one socket would each believe they own the hardware.

    Both could open the same interface, one could be holding an output up
    under a lease the other cannot see, and an ESTOP would reach only one of
    them.  Starting the second is refused instead.
    """
    from fielddeck.common.errors import ConfigurationError

    await daemon_factory()
    try:
        await daemon_factory()
    except ConfigurationError as exc:
        assert "already listening" in str(exc)
        assert exc.preserved == "the running daemon was left untouched"
    else:  # pragma: no cover - the assertion below reports it
        raise AssertionError("a second daemon bound over a live socket")


async def test_a_stale_socket_file_does_not_block_a_restart(daemon_factory, paths) -> None:
    """A killed daemon leaves its socket behind; the next one may clean it up."""
    first = await daemon_factory()
    socket = first.socket_path
    await first.stop()
    socket.parent.mkdir(parents=True, exist_ok=True)
    socket.write_bytes(b"")  # not a socket at all: a leftover file

    second = await daemon_factory()
    assert second.socket_path == socket
    async with InstrumentClient(socket, source=ClientSource.FDCTL) as client:
        assert (await client.execute("system.status")).result["safety"]["state"] == "SAFE"


async def test_grants_do_not_leak_between_daemons_on_the_same_store(daemon_factory) -> None:
    """Nothing on disk can re-arm a unit.  The proof is that nothing is written."""
    first = await daemon_factory()
    first.safety.arm(permission=PermissionLevel.FLASH, ttl_s=300, source=ClientSource.FDCTL)
    await first.stop()

    second = await daemon_factory()
    assert second.safety.arm_registry.active() == []
    assert second.safety.snapshot().armed_permissions == []
