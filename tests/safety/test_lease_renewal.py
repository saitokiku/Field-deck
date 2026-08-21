"""A lease is a promise that *this* client is still watching.

Renewing it is pulling a dead-man handle.  Three things followed from taking
that literally, and none of them held before these tests existed:

* An assistant on the restricted socket could renew a POWER lease it did not
  own, holding a rail up past the interval the operator set -- the dead-man
  handle held down by exactly the thing it exists to be independent of.
* Any client could renew any lease, so the promise was about a third party.
* A renewal could *lengthen* the interval without bound, and left ``ttl_s``
  reporting the old one, so the lease advertised a dead-man interval it was not
  honouring and the HMI displayed that number.

Releasing is deliberately still open to everyone, including the assistant, for
the same reason ``estop`` is: releasing ends a hazard, and stopping is never the
dangerous direction.
"""

from __future__ import annotations

import asyncio

import pytest

from fielddeck.common.errors import LeaseError, PermissionDenied
from fielddeck.common.models import ClientSource
from fielddeck.daemon.client import InstrumentClient
from fielddeck.daemon.service import InstrumentDaemon

SIM_PSU = "role:psu"


async def _energise(client: InstrumentClient, *, ttl_s: float) -> str:
    await client.call("safety.arm", {"permission": "power", "ttl_s": 60})
    result = await client.execute(
        "psu.output", {"device": SIM_PSU, "enabled": True, "lease_ttl_s": ttl_s}
    )
    return str(result.result["lease"]["lease_id"])


async def test_the_restricted_socket_cannot_renew_a_lease(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    """The whole finding, end to end.

    The operator's client hangs rather than disconnecting -- disconnecting
    releases the lease and is already handled.  A hang is the case the
    dead-man interval is for.
    """
    psu = daemon.registry.resolve(SIM_PSU)
    lease_id = await _energise(client, ttl_s=1.0)
    assert psu.output_enabled is True

    async with InstrumentClient(daemon.ai_socket_path, source=ClientSource.CLAUDE) as ai:
        # It can see the lease id -- safety.status is PASSIVE and should stay
        # readable. Seeing it must not be enough to act on it.
        status = await ai.call("safety.status", {})
        assert lease_id in [lease["lease_id"] for lease in status["leases"]]

        with pytest.raises(PermissionDenied):
            await ai.call("safety.lease_renew", {"lease_id": lease_id, "ttl_s": 100_000})

    await asyncio.sleep(1.6)
    assert psu.output_enabled is False, (
        "the rail outlived its dead-man interval because the restricted socket "
        "renewed a lease it does not hold"
    )


async def test_only_the_holder_may_renew(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    """Defence in depth: refused again below the transport.

    A second full-authority client is not a lesser client than the first, but
    it is not the one that promised to keep watching.
    """
    lease_id = await _energise(client, ttl_s=5.0)

    async with InstrumentClient(daemon.socket_path, source=ClientSource.FDCTL) as other:
        with pytest.raises(LeaseError, match="only its holder"):
            await other.call("safety.lease_renew", {"lease_id": lease_id})


async def test_the_holder_can_renew(client: InstrumentClient) -> None:
    """The other half: the guard must not break the normal case."""
    lease_id = await _energise(client, ttl_s=5.0)
    renewed = await client.call("safety.lease_renew", {"lease_id": lease_id})
    assert renewed["lease"]["lease_id"] == lease_id
    assert renewed["lease"]["released"] is False


async def test_a_renewal_cannot_lengthen_the_interval(client: InstrumentClient) -> None:
    lease_id = await _energise(client, ttl_s=2.0)

    renewed = await client.call("safety.lease_renew", {"lease_id": lease_id, "ttl_s": 100_000})

    assert renewed["lease"]["ttl_s"] <= 2.0, (
        "a client that can renew for an hour has replaced its dead-man handle with a timer"
    )
    # And the reported interval is the one actually in force. Leaving ttl_s
    # stale made the lease advertise a deadline it was not honouring.
    assert renewed["lease"]["ttl_s"] == 2.0
    assert renewed["lease"]["remaining_s"] <= 2.0 if "remaining_s" in renewed["lease"] else True


async def test_a_shorter_renewal_is_honoured(client: InstrumentClient) -> None:
    """Shortening is always safe, so it is always allowed."""
    lease_id = await _energise(client, ttl_s=5.0)
    renewed = await client.call("safety.lease_renew", {"lease_id": lease_id, "ttl_s": 0.8})
    assert renewed["lease"]["ttl_s"] == 0.8


async def test_the_restricted_socket_may_still_release(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    """Releasing ends a hazard, so the assistant keeps it.

    Same asymmetry as ``estop``: Claude can stop the bench and cannot start it.
    """
    psu = daemon.registry.resolve(SIM_PSU)
    lease_id = await _energise(client, ttl_s=30.0)
    assert psu.output_enabled is True

    async with InstrumentClient(daemon.ai_socket_path, source=ClientSource.CLAUDE) as ai:
        await ai.call("safety.lease_release", {"lease_id": lease_id})

    assert psu.output_enabled is False
