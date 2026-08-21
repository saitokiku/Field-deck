"""A safe state that did not apply must never be recorded as one.

`apply_safe_state` published `SAFE_STATE_APPLIED` with the message *"safe state
applied to <device>"* at WARNING severity for every device, including ones
whose `safe_state()` had just timed out or raised.  The per-device outcome
carried `applied: False`, but the message did not, and the message is what a
person reads.

An operator reading back an emergency stop is asking these lines exactly one
question. Answering it wrongly is worse than not logging at all.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fielddeck.common.events import EventSeverity, EventType
from fielddeck.common.models import ClientSource
from fielddeck.daemon.client import InstrumentClient
from fielddeck.daemon.service import InstrumentDaemon

SIM_PSU = "role:psu"


def _break_safe_state(daemon: InstrumentDaemon, *, mode: str) -> None:
    """Make one driver fail to reach its safe state, the two ways it can."""
    driver = daemon.registry.resolve(SIM_PSU)

    async def hangs() -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {}

    async def raises() -> dict[str, Any]:
        raise OSError("the instrument stopped answering")

    driver.safe_state = hangs if mode == "timeout" else raises  # type: ignore[method-assign]


async def test_a_device_with_nothing_to_safe_is_not_reported_as_unsafe(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    """``applied: False`` means two different things, and only one is bad.

    A DMM, a logic analyzer and a camera all return ``applied: False,
    reason: "no outputs"`` -- they are fine, there was simply nothing to do. A
    supply whose ``safe_state()`` raised returns ``applied: False`` as well, and
    is live. Reading the two the same way makes the DMM look dangerous and,
    much worse, makes the supply look ordinary.
    """
    results = await daemon.dispatcher.apply_safe_state(reason="healthy bench")

    no_output_devices = [r for r in results if r.get("reason") == "no outputs"]
    assert no_output_devices, "expected the simulated bench to include devices with no outputs"

    for outcome in no_output_devices:
        assert outcome["applied"] is False, "these devices change nothing"
        assert outcome["safe"] is True, (
            f"{outcome['device']} has no outputs and was reported as not safe"
        )

    assert all(r["safe"] for r in results), "a healthy bench reported a device as unsafe"


async def test_the_estop_reply_says_plainly_that_a_device_was_not_safed(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    """A client must not have to scan a list to learn the bench is not safe."""
    _break_safe_state(daemon, mode="raise")

    reply = await client.call("safety.estop", {"reason": "reporting test"})

    assert reply["all_devices_safe"] is False
    assert daemon.registry.resolve(SIM_PSU).device_id in reply["devices_not_safed"]


async def test_a_successful_estop_says_every_device_is_safe(
    client: InstrumentClient,
) -> None:
    """The other half: the flag must not cry wolf on a healthy bench."""
    reply = await client.call("safety.estop", {"reason": "healthy"})

    assert reply["all_devices_safe"] is True
    assert reply["devices_not_safed"] == []


async def test_a_timeout_is_reported_as_a_failure_too(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    """A wedged driver is the case the concurrent safe state was built for.

    It must still make the stop return promptly *and* say the device is live.
    """
    _break_safe_state(daemon, mode="timeout")

    started = asyncio.get_running_loop().time()
    reply = await client.call("safety.estop", {"reason": "wedged driver"})
    elapsed = asyncio.get_running_loop().time() - started

    assert reply["all_devices_safe"] is False
    assert daemon.registry.resolve(SIM_PSU).device_id in reply["devices_not_safed"]
    # The whole point of safing devices concurrently: one wedged driver costs
    # its own timeout, not the sum of every device's.
    assert elapsed < 10.0, f"the stop took {elapsed:.1f}s behind one wedged driver"


async def test_the_event_for_a_failed_safe_state_is_not_labelled_applied(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    """The message an operator reads has to match what happened."""
    _break_safe_state(daemon, mode="raise")

    async with InstrumentClient(daemon.socket_path, source=ClientSource.HMI) as watcher:
        await watcher.call("safety.estop", {"reason": "message text"})
        recent = await watcher.call("events.recent", {"limit": 200})

    safe_events = [
        event
        for event in recent["events"]
        if event["type"] == str(EventType.SAFE_STATE_APPLIED)
        and event.get("device_id") == daemon.registry.resolve(SIM_PSU).device_id
    ]
    assert safe_events, "no safe-state event was recorded for the failing device"

    failure = safe_events[-1]
    assert "SAFE STATE FAILED" in failure["message"]
    assert "treat this device as live" in failure["message"]
    assert failure["severity"] == str(EventSeverity.CRITICAL)
    assert failure["payload"]["safe"] is False
