"""Limits, end to end: being armed is not permission to exceed a ceiling.

Authorization and limits are two different questions.  A POWER grant answers
"may this operator change a rail at all"; the limit answers "is this the rail
they meant".  The dispatcher checks them in that order, and these tests pin
both the order and the outcome — including the derived V x I check, which is
the one that catches 24 V into a 3 A limit when neither number is alarming on
its own.

The module runs against a *tightened* policy (``psu.power`` at 6 W).  That is
not an arbitrary choice: with the shipped defaults the power ceiling is exactly
``psu.voltage`` maximum times ``psu.current`` maximum, so no legal pair of
setpoints can exceed it and a derived-limit test would pass without testing
anything.  The last test in this file states that fact explicitly.
"""

from __future__ import annotations

import pytest

from fielddeck.common.config import DEFAULT_GLOBAL_LIMITS, SafetyConfig
from fielddeck.common.errors import InvalidRequest, PermissionDenied, SafetyLimitExceeded
from fielddeck.common.events import EventType
from fielddeck.common.models import ClientSource, PermissionLevel, SafetyLimit
from fielddeck.daemon.client import InstrumentClient
from fielddeck.daemon.service import InstrumentDaemon

SIM_PSU = "sim:visa:sim-psu-0"


@pytest.fixture
def safety_config(strict_safety_config: SafetyConfig) -> SafetyConfig:
    """Every daemon in this module gets the tightened power ceiling."""
    return strict_safety_config


async def test_a_limit_beats_an_active_arm_grant(
    client: InstrumentClient, arm, daemon: InstrumentDaemon
) -> None:
    """Armed for POWER, still refused 60 V — and the setpoint does not move."""
    await arm(PermissionLevel.POWER)

    with pytest.raises(SafetyLimitExceeded) as caught:
        await client.execute("psu.set", {"device": SIM_PSU, "voltage": 60.0})

    assert caught.value.details["quantity"] == "psu.voltage"
    assert caught.value.details["maximum"] == 30.0
    assert caught.value.preserved == "no command was sent to the device"

    status = (await client.execute("psu.status", {"device": SIM_PSU})).result
    assert status["setpoint_v"] == 0.0
    assert status["output"] is False

    rejections = [e for e in daemon.bus.recent(limit=100) if e.type is EventType.LIMIT_REJECTED]
    assert [e.action for e in rejections] == ["psu.set"]


async def test_authorization_is_checked_before_limits(client: InstrumentClient) -> None:
    """An unarmed over-limit request is refused for the *first* reason.

    Not a stylistic point: the error an operator sees has to name the thing
    they have to fix first, and there is no need to reason about a setpoint
    that was never authorized in the first place.
    """
    with pytest.raises(PermissionDenied):
        await client.execute("psu.set", {"device": SIM_PSU, "voltage": 60.0})


async def test_a_derived_limit_catches_a_product_neither_parameter_exceeds(
    client: InstrumentClient, arm
) -> None:
    """5 V is fine.  2 A is fine.  10 W is not, and V x I is what says so."""
    await arm(PermissionLevel.POWER)
    limits = (await client.execute("system.limits")).result["global"]
    assert limits["psu.voltage"]["maximum"] >= 5.0
    assert limits["psu.current"]["maximum"] >= 2.0
    assert limits["psu.power"]["maximum"] == 6.0

    assert (
        await client.execute(
            "psu.set", {"device": SIM_PSU, "voltage": 5.0, "current_limit": 0.5}
        )
    ).ok

    with pytest.raises(SafetyLimitExceeded) as caught:
        await client.execute(
            "psu.set", {"device": SIM_PSU, "voltage": 5.0, "current_limit": 2.0}
        )

    assert caught.value.details["quantity"] == "psu.power"
    assert caught.value.details["value"] == pytest.approx(10.0)

    # The rejected call left the earlier, legal setpoint exactly as it was.
    status = (await client.execute("psu.status", {"device": SIM_PSU})).result
    assert status["setpoint_v"] == 5.0
    assert status["current_limit_a"] == 0.5


async def test_a_device_limit_tightens_the_global_one(daemon_factory, safety_config) -> None:
    """A 5 V board on a 30 V-capable supply is why the device layer exists."""
    scoped = safety_config.model_copy(
        update={
            "device_limits": {
                SIM_PSU: {
                    "psu.voltage": SafetyLimit(
                        quantity="psu.voltage", minimum=0.0, maximum=5.0, unit="V"
                    )
                }
            }
        }
    )
    daemon = await daemon_factory(safety_config=scoped)

    async with InstrumentClient(daemon.socket_path, source=ClientSource.FDCTL) as client:
        await client.call("safety.arm", {"permission": "POWER", "ttl_s": 60})

        assert (await client.execute("psu.set", {"device": SIM_PSU, "voltage": 5.0})).ok
        with pytest.raises(SafetyLimitExceeded) as caught:
            await client.execute("psu.set", {"device": SIM_PSU, "voltage": 12.0})

    assert caught.value.details["maximum"] == 5.0
    assert caught.value.details["device_id"] == SIM_PSU


async def test_a_non_numeric_setpoint_is_refused_rather_than_coerced(
    client: InstrumentClient, arm
) -> None:
    """A supply that silently coerces a voltage is a supply that damages a DUT."""
    await arm(PermissionLevel.POWER)
    with pytest.raises(InvalidRequest) as caught:
        await client.execute("psu.set", {"device": SIM_PSU, "voltage": "twenty four"})

    fields = {problem["field"] for problem in caught.value.details["errors"]}
    assert fields == {"voltage"}
    assert caught.value.preserved == "no command was sent to the device"


def test_the_shipped_defaults_cannot_trigger_the_derived_power_check() -> None:
    """Documents a real gap in the default policy, so it is not rediscovered.

    ``DEFAULT_GLOBAL_LIMITS`` sets psu.power to 90 W, which is exactly 30 V x
    3 A — the largest product the individual ceilings allow.  Because
    :meth:`SafetyLimit.violation` uses a strict ``>``, the worst legal pair
    lands *on* the ceiling and passes.  The derived check is therefore inert
    out of the box and only becomes meaningful once a deployment tightens one
    of the three, which is what ``strict_safety_config`` does above.
    """
    defaults = SafetyConfig.defaults()
    voltage = defaults.global_limits["psu.voltage"].maximum
    current = defaults.global_limits["psu.current"].maximum
    power = defaults.global_limits["psu.power"]
    assert voltage is not None and current is not None
    assert power.maximum == voltage * current == 90.0
    assert power.violation(voltage * current) is None
    assert DEFAULT_GLOBAL_LIMITS["psu.power"]["maximum"] == 90.0
