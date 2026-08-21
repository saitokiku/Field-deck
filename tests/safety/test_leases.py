"""Output leases: the dead-man's handle on a sustained hazardous output.

A lease exists for one failure mode — the client that turned an output on goes
away, and the hardware stays energised.  Two ways that happens, both covered
here: the client stops renewing, and the client's connection dies.  In both
cases ``instrumentd`` is the thing that puts the rail down, without being
asked, and says so on the timeline.

Expiry is forced by moving the lease's own deadline into the past rather than
by sleeping out a TTL: the daemon's safety timer then reaps it on its next
tick, which is the same code path a real lapse takes.
"""

from __future__ import annotations

import pytest

from fielddeck.common.errors import LeaseError
from fielddeck.common.events import EventType
from fielddeck.common.models import ClientSource, PermissionLevel
from fielddeck.common.timebase import monotonic_ns
from fielddeck.daemon.client import InstrumentClient
from fielddeck.daemon.service import InstrumentDaemon

SIM_PSU = "sim:visa:sim-psu-0"


async def energise(client: InstrumentClient, *, lease_ttl_s: float = 60.0) -> str:
    """Arm, set a sane rail, enable it, and hand back the lease id."""
    await client.call("safety.arm", {"permission": "POWER", "ttl_s": 120})
    await client.execute("psu.set", {"device": SIM_PSU, "voltage": 12.0, "current_limit": 0.5})
    result = await client.execute(
        "psu.output", {"device": SIM_PSU, "enabled": True, "lease_ttl_s": lease_ttl_s}
    )
    assert result.result["output"] is True
    return str(result.result["lease"]["lease_id"])


async def output_is_off(client: InstrumentClient) -> bool:
    status = await client.execute("psu.status", {"device": SIM_PSU})
    return status.result["output"] is False


# ---------------------------------------------------------------------------
# Acquisition
# ---------------------------------------------------------------------------


async def test_enabling_an_output_takes_a_lease_before_the_handler_runs(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    lease_id = await energise(client)

    leases = daemon.safety.leases.active()
    assert [lease.lease_id for lease in leases] == [lease_id]
    assert leases[0].device_id == SIM_PSU
    assert leases[0].action == "psu.output"
    # The stored safe action is what the daemon will replay if this lapses.
    assert leases[0].safe_params["enabled"] is False

    types = [event.type for event in daemon.bus.recent(limit=100)]
    assert EventType.LEASE_ACQUIRED in types
    assert EventType.OUTPUT_ENABLED in types


async def test_turning_the_output_off_releases_the_lease(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    await energise(client)
    result = await client.execute("psu.output", {"device": SIM_PSU, "enabled": False})

    assert result.result["lease"] is None
    assert daemon.safety.leases.active() == []
    types = [event.type for event in daemon.bus.recent(limit=100)]
    assert EventType.LEASE_RELEASED in types
    assert EventType.OUTPUT_DISABLED in types


async def test_a_disable_takes_no_lease_of_its_own(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    """The safe direction needs no dead-man handle; there is nothing to hold up."""
    await client.execute("psu.output", {"device": SIM_PSU, "enabled": False})
    assert daemon.safety.leases.active() == []


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


async def test_an_expiring_lease_disables_the_output(
    client: InstrumentClient, daemon: InstrumentDaemon, wait_for
) -> None:
    """Nobody renewed, so the daemon puts the rail down on its own."""
    lease_id = await energise(client, lease_ttl_s=30)
    lease = next(l for l in daemon.safety.leases.active() if l.lease_id == lease_id)
    lease.expires_monotonic_ns = monotonic_ns() - 1

    await wait_for(
        lambda: output_is_off(client),
        message="the supply was still energised after its lease lapsed",
    )

    assert daemon.safety.leases.active() == []
    recent = daemon.bus.recent(limit=300)
    expired = [e for e in recent if e.type is EventType.LEASE_EXPIRED]
    assert [e.payload.get("lease_id") for e in expired] == [lease_id]
    safed = [
        e
        for e in recent
        if e.type is EventType.SAFE_STATE_APPLIED and e.payload.get("reason") == "output lease expired"
    ]
    assert [e.device_id for e in safed] == [SIM_PSU]


async def test_renewing_keeps_the_output_up(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    lease_id = await energise(client, lease_ttl_s=30)
    before = next(l for l in daemon.safety.leases.active() if l.lease_id == lease_id)
    deadline_before = before.expires_monotonic_ns

    reply = await client.call("safety.lease_renew", {"lease_id": lease_id, "ttl_s": 45})

    assert reply["lease"]["expires_monotonic_ns"] > deadline_before
    assert not await output_is_off(client)


async def test_an_already_expired_lease_cannot_be_renewed(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    """Re-acquire instead: renewing a lapsed handle would hide the lapse."""
    lease_id = await energise(client, lease_ttl_s=30)
    lease = next(l for l in daemon.safety.leases.active() if l.lease_id == lease_id)
    lease.expires_monotonic_ns = monotonic_ns() - 1

    with pytest.raises(LeaseError):
        await client.call("safety.lease_renew", {"lease_id": lease_id})


async def test_releasing_a_lease_explicitly_drives_safe_state(
    client: InstrumentClient, wait_for
) -> None:
    lease_id = await energise(client)
    reply = await client.call("safety.lease_release", {"lease_id": lease_id})

    assert reply["released"] == lease_id
    await wait_for(
        lambda: output_is_off(client), message="releasing the lease left the rail up"
    )


# ---------------------------------------------------------------------------
# Disconnection
# ---------------------------------------------------------------------------


async def test_a_client_disconnecting_releases_its_lease_and_drives_safe_state(
    client_factory, client: InstrumentClient, daemon: InstrumentDaemon, wait_for
) -> None:
    """A PSU left energised by a crashed UI is exactly what this prevents."""
    owner = await client_factory(source=ClientSource.HMI)
    lease_id = await energise(owner, lease_ttl_s=300)
    assert not await output_is_off(client)

    # The owning connection goes away without releasing anything, which is
    # what a killed process looks like from the daemon's side.
    await owner.close()

    await wait_for(
        lambda: output_is_off(client),
        message="the rail stayed up after the client holding its lease disconnected",
    )
    assert daemon.safety.leases.active() == []

    orphaned = [
        event
        for event in daemon.bus.recent(limit=300)
        if event.type is EventType.LEASE_EXPIRED and event.payload.get("lease_id") == lease_id
    ]
    assert orphaned and "disconnected" in (orphaned[0].message or "")


async def test_another_client_disconnecting_leaves_the_lease_alone(
    client_factory, client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    """Only the owner's connection matters; a bystander leaving is not a lapse."""
    await energise(client, lease_ttl_s=300)
    bystander = await client_factory()
    await bystander.execute("system.status")
    await bystander.close()

    # Give the disconnect handler a tick to run before asserting nothing changed.
    await client.execute("can.listen", {"device": "sim:can:can0", "duration_s": 0.05})
    assert daemon.safety.leases.active()
    assert not await output_is_off(client)


async def test_a_lease_expiring_does_not_revoke_authorization(
    client: InstrumentClient, daemon: InstrumentDaemon, wait_for
) -> None:
    """The rail drops; the operator's POWER grant is still theirs to use.

    A lapsed lease means "nobody is watching this output", not "the operator
    is no longer trusted" — they can turn it back on without re-arming.
    """
    lease_id = await energise(client, lease_ttl_s=30)
    lease = next(l for l in daemon.safety.leases.active() if l.lease_id == lease_id)
    lease.expires_monotonic_ns = monotonic_ns() - 1
    await wait_for(lambda: output_is_off(client), message="the rail never came down")

    assert daemon.safety.snapshot().armed_permissions == [PermissionLevel.POWER]
    again = await client.execute(
        "psu.output", {"device": SIM_PSU, "enabled": True, "lease_ttl_s": 30}
    )
    assert again.result["output"] is True
