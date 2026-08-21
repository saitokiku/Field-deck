"""Client connections: the handshake, the identity, and the dead-man handle.

An output lease belongs to the *connection* that took it.  That is the whole
mechanism behind "if the UI dies, the rail drops", so it is tested here by
killing a connection while a simulated supply is energised and watching the
daemon put it back down on its own.

The restricted socket is tested in the same place because it is the other
property of a connection that cannot be delegated to a client: whatever a
client declares itself to be, the socket it arrived on decides what it is
allowed to do.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from fielddeck.common.errors import PermissionDenied, TransportError
from fielddeck.common.events import EventType
from fielddeck.common.models import ClientSource, PermissionLevel
from fielddeck.daemon.client import InstrumentClient
from fielddeck.daemon.service import InstrumentDaemon

from .conftest import SIM_PSU, EventLog, arm, wait_until


async def test_handshake_describes_the_daemon_honestly(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    info = client.server_info
    assert info["source"] == str(ClientSource.FDCTL)
    assert info["simulated"] is True
    assert info["restricted"] is False
    assert info["devices"] == len(daemon.registry)
    assert info["protocol"]


async def test_a_closed_client_fails_loudly_rather_than_hanging(
    connect: Callable[..., InstrumentClient],
) -> None:
    extra = await connect().connect()
    assert (await extra.execute("system.status")).ok
    await extra.close()

    with pytest.raises(TransportError):
        await extra.execute("system.status")


async def test_losing_the_lease_owner_drives_the_output_safe(
    daemon: InstrumentDaemon,
    client: InstrumentClient,
    connect: Callable[..., InstrumentClient],
    events: EventLog,
) -> None:
    """The dead-man handle: a dead client must not leave a rail energised."""
    await arm(client, PermissionLevel.POWER, PermissionLevel.QUERY, ttl_s=120.0)

    holder = await connect().connect()
    await holder.execute("psu.set", {"device": SIM_PSU, "voltage": 5.0, "current_limit": 0.2})
    enabled = await holder.execute(
        "psu.output", {"device": SIM_PSU, "enabled": True, "lease_ttl_s": 300.0}
    )
    lease_id = enabled.result["lease"]["lease_id"]
    assert enabled.result["output"] is True
    assert [lease.lease_id for lease in daemon.safety.leases.active()] == [lease_id]

    status = (await client.execute("psu.status", {"device": SIM_PSU})).result
    assert status["output"] is True

    # The client dies with the output still on.  Nothing else changes.
    await holder.close()

    expired = await events.wait_for(
        EventType.LEASE_EXPIRED, match=lambda event: event.payload.get("lease_id") == lease_id
    )
    assert "disconnected" in (expired.message or "")
    await wait_until(
        lambda: not daemon.safety.leases.active(), what="the orphaned lease to be reaped"
    )

    status = (await client.execute("psu.status", {"device": SIM_PSU})).result
    assert status["output"] is False, "a disconnected client left the output energised"
    assert any(
        event.type is EventType.SAFE_STATE_APPLIED and event.device_id == SIM_PSU
        for event in events.events
    )

    # The grant is untouched: losing a connection is not an authorization event.
    assert daemon.safety.snapshot().armed_permissions


async def test_releasing_a_lease_explicitly_also_safes_the_output(
    daemon: InstrumentDaemon, client: InstrumentClient
) -> None:
    await arm(client, PermissionLevel.POWER, ttl_s=120.0)
    enabled = await client.execute(
        "psu.output", {"device": SIM_PSU, "enabled": True, "lease_ttl_s": 300.0}
    )
    lease_id = enabled.result["lease"]["lease_id"]

    renewed = await client.call("safety.lease_renew", {"lease_id": lease_id})
    assert renewed["lease"]["lease_id"] == lease_id

    released = await client.call("safety.lease_release", {"lease_id": lease_id})
    assert released["released"] == lease_id
    assert daemon.safety.leases.active() == []
    status = (await client.execute("psu.status", {"device": SIM_PSU})).result
    assert status["output"] is False


async def test_the_restricted_socket_decides_who_you_are(daemon: InstrumentDaemon) -> None:
    """A client on the AI socket is ``claude``, whatever it claims to be."""
    assert daemon.ai_socket_path is not None
    async with InstrumentClient(
        daemon.ai_socket_path,
        # Deliberately lying about being the operator's panel.
        source=ClientSource.HMI,
        timeout_s=20.0,
    ) as ai:
        assert ai.server_info["source"] == str(ClientSource.CLAUDE)
        assert ai.server_info["restricted"] is True

        # Reading is fine.
        assert (await ai.execute("system.status")).ok

        # Arming is refused at the transport, not by the client's good manners.
        with pytest.raises(PermissionDenied) as denied:
            await ai.call("safety.arm", {"permission": "POWER", "ttl_s": 30})
        assert "restricted socket" in denied.value.message

        # And an action above PASSIVE is denied with a human-facing remedy.
        with pytest.raises(PermissionDenied) as refused:
            await ai.execute("psu.measure", {"device": SIM_PSU})
        assert refused.value.details["source"] == str(ClientSource.CLAUDE)
        assert "fdctl arm" in refused.value.details["hint"]
