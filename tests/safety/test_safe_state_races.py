"""An action already in flight must not undo a safe state that overtook it.

``Dispatcher.apply_safe_state`` deliberately does not take the device lock: an
emergency stop that queues behind a wedged driver is not an emergency stop, and
running the devices concurrently is what makes a stop take milliseconds instead
of one timeout per device.

The price of that choice is a race, and it was real:

    ``psu.output(enabled=True)`` is authorized, takes its lease, takes the
    device lock, and is mid-write to the instrument.  ESTOP fires.  It cancels
    what it can -- ``psu.output`` is not ``cancelable``, because abandoning an
    instrument half-configured is its own hazard -- and drives every device
    safe.  The rail goes off.  Then the handler finishes and turns it back on.

    The stop reported success.  The action reported success.  The rail was live
    with the stop latched.

Lease expiry and daemon shutdown reach ``apply_safe_state`` by the same path
and lost the same way, so the guard keys on *"was this device driven safe while
I was running"* rather than on the emergency stop specifically.  Both routes
are tested here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from fielddeck.common.models import ClientSource, PermissionLevel
from fielddeck.daemon.client import InstrumentClient
from fielddeck.daemon.service import InstrumentDaemon

SIM_PSU = "role:psu"


def _slow_down(daemon: InstrumentDaemon, action: str, seconds: float) -> Callable[[], None]:
    """Make one action take long enough to be overtaken.

    A real ``psu.output`` is a USBTMC round trip, so "slow enough to race" is
    the normal case on hardware rather than a contrivance.  Returns a callable
    that puts the original handler back.
    """
    spec = daemon.registry.resolve(SIM_PSU).actions()[action]
    original = spec.handler

    async def slow(ctx: Any, params: Any) -> Any:
        await asyncio.sleep(seconds)
        return await original(ctx, params)

    spec.handler = slow

    def restore() -> None:
        spec.handler = original

    return restore


async def _energise_slowly(
    client: InstrumentClient, daemon: InstrumentDaemon, *, lease_ttl_s: float
) -> asyncio.Task[Any]:
    await client.call("safety.arm", {"permission": "power", "ttl_s": 60})
    return asyncio.create_task(
        client.try_execute(
            "psu.output",
            {"device": SIM_PSU, "enabled": True, "lease_ttl_s": lease_ttl_s},
        )
    )


async def test_an_estop_mid_action_is_not_undone_when_the_action_finishes(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    psu = daemon.registry.resolve(SIM_PSU)
    restore = _slow_down(daemon, "psu.output", 1.0)
    try:
        task = await _energise_slowly(client, daemon, lease_ttl_s=30)
        await asyncio.sleep(0.3)  # the handler is past authorization and running

        async with InstrumentClient(daemon.socket_path, source=ClientSource.HMI) as stopper:
            await stopper.call("safety.estop", {"reason": "mid-action"})

        result = await task
        await asyncio.sleep(0.2)

        assert daemon.safety.snapshot().estop_active
        assert psu.output_enabled is False, (
            "the rail came back up after the emergency stop, because the in-flight "
            "action finished and re-applied its effect"
        )
        assert not result.ok, "the action reported success while its effect was being undone"
        assert result.error["code"] == "EstopActive"
        assert "undone" in result.error["message"]
    finally:
        restore()


async def test_a_lapsed_lease_mid_action_is_not_undone_when_the_action_finishes(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    """The same race without an emergency stop, so the guard cannot key on one."""
    psu = daemon.registry.resolve(SIM_PSU)
    restore = _slow_down(daemon, "psu.output", 2.0)
    try:
        # A lease shorter than the handler: the safety loop reaps it and drives
        # the supply safe while the handler is still running.
        result = await (await _energise_slowly(client, daemon, lease_ttl_s=0.5))
        await asyncio.sleep(0.5)

        assert not daemon.safety.snapshot().estop_active, "no stop was engaged in this test"
        assert psu.output_enabled is False, (
            "the rail stayed up past its lease, because the in-flight action "
            "finished and re-applied its effect"
        )
        assert not result.ok
        # Not EstopActive: nothing latched, and telling the operator otherwise
        # would send them to `fdctl estop clear` for a lease that simply lapsed.
        assert result.error["code"] == "LeaseError"
    finally:
        restore()


async def test_the_reverted_action_leaves_no_lease_behind(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    """A lease outliving the effect it sustained is its own bug.

    The daemon would believe an output it just turned off is still held, and
    the next expiry sweep would safe a device for a second time for no reason.
    """
    restore = _slow_down(daemon, "psu.output", 1.0)
    try:
        task = await _energise_slowly(client, daemon, lease_ttl_s=30)
        await asyncio.sleep(0.3)
        async with InstrumentClient(daemon.socket_path, source=ClientSource.HMI) as stopper:
            await stopper.call("safety.estop", {"reason": "lease check"})
        await task
        await asyncio.sleep(0.2)

        assert daemon.safety.leases.active() == []
    finally:
        restore()


@pytest.mark.parametrize("permission", [PermissionLevel.POWER])
async def test_a_capture_that_finishes_during_a_stop_keeps_its_data(
    client: InstrumentClient, daemon: InstrumentDaemon, permission: PermissionLevel
) -> None:
    """Only *state-changing* work is reverted.

    A capture that completed during a stop has already written its bytes.
    Discarding evidence to tidy up the bookkeeping is the opposite of what a
    safety system should do -- the stop is on the timeline right beside it.
    """
    await client.execute("session.start", {"name": "capture-during-stop"})
    task = asyncio.create_task(
        client.try_execute("can.capture", {"device": "sim:can:can0", "duration_s": 1.0})
    )
    await asyncio.sleep(0.3)
    async with InstrumentClient(daemon.socket_path, source=ClientSource.HMI) as stopper:
        await stopper.call("safety.estop", {"reason": "during a capture"})

    result = await task
    assert result.ok, "a capture was discarded because a stop happened while it ran"
    assert result.result["count"] > 0
    assert result.result["artifact"] is not None
