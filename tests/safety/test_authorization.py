"""Authorization, end to end through the dispatcher.

These are the mandatory tests from SPEC.md section 39, and they are written
against a real ``instrumentd`` over a real socket on purpose.  A unit test of
:class:`~fielddeck.safety.manager.SafetyManager` proves the manager's own
logic; it does not prove that the *pipeline* consults it, in the right order,
before a driver runs.  That is the property these tests defend: there is no
path to a simulated PSU or a simulated CAN interface that skips the check.

Nothing here sleeps to make a grant expire.  Expiry is forced by moving the
grant's own deadline into the past, so the test is deterministic and instant
and still goes through exactly the code an expired grant goes through.

Companion file: ``test_permission_matrix.py`` pins the same rules as unit tests
against a bare :class:`SafetyManager`, exhaustively -- every (granted,
requested) pair rather than the few interesting ones.  The overlap between the
two files is deliberate and neither replaces the other: the matrix proves the
rule is right, these prove the pipeline actually asks.
"""

from __future__ import annotations

import pytest

from fielddeck.common.errors import EstopActive, PermissionDenied
from fielddeck.common.events import EventType
from fielddeck.common.models import ArmScope, ClientSource, PermissionLevel
from fielddeck.common.timebase import monotonic_ns
from fielddeck.daemon.client import InstrumentClient
from fielddeck.daemon.service import InstrumentDaemon

SIM_PSU = "sim:visa:sim-psu-0"
SIM_CAN = "sim:can:can0"
SIM_SERIAL = "sim:serial:sim-uart-0"


async def psu_output_is(client: InstrumentClient, expected: bool) -> bool:
    status = await client.execute("psu.status", {"device": SIM_PSU})
    return bool(status.result["output"]) is expected


# ---------------------------------------------------------------------------
# CONTROL rejected while SAFE
# ---------------------------------------------------------------------------


async def test_control_action_is_rejected_while_safe(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    """A CONTROL action from a fresh daemon reaches no hardware."""
    assert daemon.safety.snapshot().state_word == "SAFE"

    with pytest.raises(PermissionDenied) as caught:
        await client.execute("can.send", {"device": SIM_CAN, "can_id": 0x100, "data": "01 02"})

    assert caught.value.preserved == "no command was sent to the device"
    assert "fdctl arm control" in caught.value.details["hint"]
    assert caught.value.details["permission"] == str(PermissionLevel.CONTROL)
    # The refusal is as visible on the timeline as a success would be.
    denials = [e for e in daemon.bus.recent(limit=100) if e.type is EventType.ACTION_DENIED]
    assert [e.action for e in denials] == ["can.send"]


async def test_the_same_action_succeeds_once_control_is_armed(
    client: InstrumentClient, arm
) -> None:
    await arm(PermissionLevel.CONTROL)
    result = await client.execute("can.send", {"device": SIM_CAN, "can_id": 0x100, "data": "01 02"})
    assert result.ok
    assert result.permission is PermissionLevel.CONTROL


async def test_passive_work_never_needs_a_grant(client: InstrumentClient) -> None:
    """PASSIVE is the boot state: listening is always available."""
    result = await client.execute("can.listen", {"device": SIM_CAN, "duration_s": 0.1})
    assert result.ok
    assert result.result["mode"] == "listen-only"


# ---------------------------------------------------------------------------
# POWER rejected with a CONTROL-only grant (exact-class authorization)
# ---------------------------------------------------------------------------


async def test_power_is_rejected_with_only_control_armed(
    client: InstrumentClient, arm, daemon: InstrumentDaemon
) -> None:
    await arm(PermissionLevel.CONTROL)

    with pytest.raises(PermissionDenied) as caught:
        await client.execute("psu.set", {"device": SIM_PSU, "voltage": 5.0})

    assert caught.value.details["permission"] == str(PermissionLevel.POWER)
    # The operator is told what *is* armed, so the mismatch is obvious.
    assert caught.value.details["armed"] == [str(PermissionLevel.CONTROL)]
    assert await psu_output_is(client, False)


async def test_control_is_rejected_with_only_power_armed(client: InstrumentClient, arm) -> None:
    """The other direction: a higher class does not imply a lower one."""
    await arm(PermissionLevel.POWER)
    assert PermissionLevel.POWER > PermissionLevel.CONTROL

    with pytest.raises(PermissionDenied):
        await client.execute("serial.send", {"device": SIM_SERIAL, "text": "hello"})


async def test_query_is_not_covered_by_power(client: InstrumentClient, arm) -> None:
    """Reading an instrument transmits to it, so it needs its own class."""
    await arm(PermissionLevel.POWER)
    with pytest.raises(PermissionDenied):
        await client.execute("psu.measure", {"device": SIM_PSU})

    await arm(PermissionLevel.QUERY)
    assert (await client.execute("psu.measure", {"device": SIM_PSU})).ok


async def test_a_device_scoped_grant_does_not_cover_another_device(
    client: InstrumentClient, arm
) -> None:
    await arm(PermissionLevel.CONTROL, scope=ArmScope(kind="device", device_id=SIM_CAN))
    assert (await client.execute("can.send", {"device": SIM_CAN, "can_id": 1, "data": "00"})).ok
    with pytest.raises(PermissionDenied):
        await client.execute("serial.send", {"device": SIM_SERIAL, "text": "x"})


# ---------------------------------------------------------------------------
# An expired grant is rejected
# ---------------------------------------------------------------------------


async def test_an_expired_grant_no_longer_authorizes(
    client: InstrumentClient, arm, daemon: InstrumentDaemon
) -> None:
    """Grants lapse.  The clock is moved, not waited on."""
    grant_id = (await arm(PermissionLevel.CONTROL, ttl_s=60))["grant_id"]
    assert (await client.execute("can.send", {"device": SIM_CAN, "can_id": 1, "data": "00"})).ok

    grant = daemon.safety.arm_registry.get(grant_id)
    assert grant is not None
    # One nanosecond past its own deadline: nothing else about it changes.
    grant.expires_monotonic_ns = monotonic_ns() - 1
    assert not grant.is_active(monotonic_ns())

    with pytest.raises(PermissionDenied) as caught:
        await client.execute("can.send", {"device": SIM_CAN, "can_id": 1, "data": "00"})
    assert caught.value.details["armed"] == []

    status = (await client.execute("system.status")).result
    assert status["safety"]["state"] == "SAFE"
    assert status["safety"]["armed"] == []


async def test_the_safety_sweep_reports_expiry_on_the_timeline(
    client: InstrumentClient, arm, daemon: InstrumentDaemon, wait_for
) -> None:
    """An expiry an operator cannot see is an expiry they will be surprised by."""
    grant_id = (await arm(PermissionLevel.QUERY, ttl_s=60))["grant_id"]
    grant = daemon.safety.arm_registry.get(grant_id)
    assert grant is not None
    grant.expires_monotonic_ns = monotonic_ns() - 1

    await wait_for(
        lambda: [e for e in daemon.bus.recent(limit=200) if e.type is EventType.ARM_EXPIRED],
        message="the safety timer never reported the grant as expired",
    )


async def test_ttl_is_clamped_to_the_policy_ceiling(client: InstrumentClient, arm) -> None:
    grant = await arm(PermissionLevel.POWER, ttl_s=86_400)
    assert grant["ttl_s"] <= 300.0  # DEFAULT_MAX_TTL_S[POWER]
    assert "clamped" in (grant["note"] or "")


# ---------------------------------------------------------------------------
# Automated clients cannot create grants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", [ClientSource.CLAUDE, ClientSource.RECIPE])
async def test_an_automated_client_cannot_arm(client_factory, source: ClientSource) -> None:
    """Claude and a recipe are explicitly outside the granting set.

    A client that can widen its own authority is not an authorization system,
    so this is refused at the RPC surface, on the full socket, with the
    client's own declared identity.
    """
    automated = await client_factory(source=source)

    with pytest.raises(PermissionDenied) as caught:
        await automated.call("safety.arm", {"permission": "POWER", "ttl_s": 30})

    assert str(source) in str(caught.value)
    assert caught.value.details["source"] == str(source)


@pytest.mark.parametrize("source", [ClientSource.CLAUDE, ClientSource.RECIPE])
async def test_an_automated_client_still_cannot_act_unarmed(
    client_factory, source: ClientSource
) -> None:
    """Failing to arm is not a detour: the action itself is still refused."""
    automated = await client_factory(source=source)
    with pytest.raises(PermissionDenied):
        await automated.call("safety.arm", {"permission": "CONTROL", "ttl_s": 30})
    with pytest.raises(PermissionDenied):
        await automated.execute("can.send", {"device": SIM_CAN, "can_id": 1, "data": "00"})


async def test_claude_may_engage_the_emergency_stop(
    client_factory, client: InstrumentClient, arm
) -> None:
    """Stopping is never the dangerous direction, so it is not restricted."""
    await arm(PermissionLevel.POWER)
    claude = await client_factory(source=ClientSource.CLAUDE)

    reply = await claude.call("safety.estop", {"reason": "assistant saw a fault"})

    assert reply["estop"] is True
    assert "preserved" in reply["evidence"]
    status = (await client.execute("system.status")).result
    assert status["safety"]["state"] == "ESTOP"


# ---------------------------------------------------------------------------
# The restricted socket
# ---------------------------------------------------------------------------


async def test_restricted_socket_refuses_arming_even_when_the_client_claims_hmi(
    ai_client: InstrumentClient,
) -> None:
    """Identity laundering is refused at the transport, not by the client.

    The MCP server has no code path to arming, but that is *its* property.
    This is the daemon's: the restricted socket refuses the method outright,
    whatever the request says it is.
    """
    with pytest.raises(PermissionDenied) as caught:
        await ai_client.call("safety.arm", {"permission": "POWER", "ttl_s": 30, "source": "hmi"})

    assert "restricted socket" in str(caught.value)
    assert caught.value.details["socket"] == "restricted"


@pytest.mark.parametrize("method", ["safety.arm", "safety.disarm", "safety.estop_clear"])
async def test_restricted_socket_refuses_every_authorization_method(
    ai_client: InstrumentClient, method: str
) -> None:
    with pytest.raises(PermissionDenied):
        await ai_client.call(method, {"permission": "CONTROL", "source": "hmi"})


async def test_restricted_socket_stamps_the_source_it_was_given(
    ai_client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    """A request from the AI socket is recorded as Claude, whatever it claims."""
    assert ai_client.server_info["source"] == str(ClientSource.CLAUDE)
    assert ai_client.server_info["restricted"] is True

    await ai_client.execute("can.listen", {"device": SIM_CAN, "duration_s": 0.05})
    listened = [
        event
        for event in daemon.bus.recent(limit=200)
        if event.type is EventType.ACTION_STARTED and event.action == "can.listen"
    ]
    assert listened and all(event.source is ClientSource.CLAUDE for event in listened)


async def test_restricted_socket_still_allows_passive_work(ai_client: InstrumentClient) -> None:
    """The restriction is on authority, not on understanding what happened."""
    assert (await ai_client.execute("system.status")).ok
    assert (await ai_client.execute("device.list")).ok


# ---------------------------------------------------------------------------
# Denied-by-policy classes
# ---------------------------------------------------------------------------


async def test_a_class_disabled_by_policy_is_refused_by_the_dispatcher(
    daemon_factory, safety_config
) -> None:
    """``denied_permissions`` outranks an operator's own grant.

    This one builds its own daemon (a deployment that refuses CONTROL outright
    is not the default fixture), so it connects its own client rather than
    pulling in the shared one and starting a second daemon on the same socket.
    """
    policy = safety_config.model_copy(update={"denied_permissions": [PermissionLevel.CONTROL]})
    daemon = await daemon_factory(safety_config=policy)

    async with InstrumentClient(daemon.socket_path, source=ClientSource.FDCTL) as client:
        await client.call("safety.arm", {"permission": "CONTROL", "ttl_s": 60})

        with pytest.raises(PermissionDenied) as caught:
            await client.execute("can.send", {"device": SIM_CAN, "can_id": 1, "data": "00"})
        assert "safety policy" in str(caught.value)
        assert caught.value.preserved == "no command was sent to the device"


async def test_arming_is_refused_while_the_stop_is_latched(client: InstrumentClient, arm) -> None:
    await client.call("safety.estop", {"reason": "test"})
    with pytest.raises(EstopActive):
        await arm(PermissionLevel.POWER)

    await client.call("safety.estop_clear", {})
    # Clearing a stop is not arming: the system comes back SAFE.
    status = (await client.execute("system.status")).result
    assert status["safety"]["state"] == "SAFE"
    assert status["safety"]["armed"] == []
