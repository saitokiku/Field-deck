"""A capture must never claim to be evidence of something that did not happen.

The rule: if nothing arrived, there is no artifact.  A zero-byte file carrying
a SHA-256 and a row in the session index reads as *"we recorded, and the bus
was quiet"* -- which is a completely different claim from *"the capture
produced nothing"*, and the operator cannot tell them apart six months later.

The real SocketCAN driver has always deleted empty captures.  The simulated
one did not, so the property was false in exactly the place a new user tests
it first, and the divergence broke the project's contract that simulation
exercises the same behaviour as real hardware.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from fielddeck.daemon.client import InstrumentClient
from fielddeck.daemon.service import InstrumentDaemon

SIM_CAN = "sim:can:can0"

#: An arbitration ID the simulated bus never transmits, so the filter matches
#: nothing and the capture is genuinely empty rather than merely short.
SILENT_ID = 0x7FF


def _zero_byte_captures(sessions_dir: Path) -> list[Path]:
    return [p for p in sessions_dir.rglob("*.log") if p.stat().st_size == 0]


async def test_a_capture_that_recorded_nothing_leaves_no_artifact(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    await client.execute("session.start", {"name": "empty-capture"})

    result = await client.execute(
        "can.capture",
        {"device": SIM_CAN, "duration_s": 0.4, "id_filter": [SILENT_ID]},
    )

    assert result.result["count"] == 0, "the filter was supposed to match nothing"
    assert result.result["artifact"] is None, (
        "a capture that recorded nothing registered an artifact; a zero-byte file "
        "with a hash is indistinguishable from a real recording of a quiet bus"
    )

    # Blocking filesystem calls in an async test are fine -- there is nothing
    # else on this loop -- but ruff's ASYNC240 is right in general, so keep the
    # walk in a plain function rather than silencing the rule everywhere.
    strays = _zero_byte_captures(Path(daemon.paths.sessions_dir))
    assert not strays, f"zero-byte capture files left in the session store: {strays}"


async def test_a_capture_that_recorded_something_does_leave_one(
    client: InstrumentClient,
) -> None:
    """The other half.  Deleting empty captures must not delete real ones."""
    await client.execute("session.start", {"name": "real-capture"})

    result = await client.execute("can.capture", {"device": SIM_CAN, "duration_s": 1.0})

    assert result.result["count"] > 0
    artifact = result.result["artifact"]
    assert artifact is not None
    assert artifact["size_bytes"] > 0
    assert artifact["sha256"]
    assert artifact["raw"] is True


def test_both_can_drivers_agree_about_empty_captures() -> None:
    """Pin the rule in *both* implementations, not just the one under test.

    A behavioural test only exercises whichever driver the fixture built.  This
    reads the source of both so that a future change to one is visible as a
    failure here rather than as a surprise on a bench.
    """
    from fielddeck.sim.can import SimCanDriver
    from fielddeck.transports.socketcan import SocketCanDriver

    for driver in (SimCanDriver, SocketCanDriver):
        source = inspect.getsource(driver.can_capture)
        assert "unlink" in source, (
            f"{driver.__name__}.can_capture no longer removes the file for an empty "
            "capture; the two CAN drivers have diverged on it"
        )
