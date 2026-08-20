"""Safety limits.

Two layers, and the stricter one always wins:

``GLOBAL HARD LIMIT``
    From ``safety.yaml``.  The ceiling for the whole deployment.

``DEVICE / PROFILE LIMIT``
    Per instrument, or per DUT profile.  A 5 V board on a 30 V-capable supply
    is exactly why this layer exists.

Limits are checked **after** authorization and are not waivable by it.  Being
armed for POWER does not mean being allowed to apply 60 V.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fielddeck.common.config import SafetyConfig
from fielddeck.common.errors import SafetyLimitExceeded
from fielddeck.common.models import SafetyLimit

__all__ = ["DerivedLimitCheck", "LimitCheck", "LimitEnforcer"]


@dataclass(frozen=True, slots=True)
class LimitCheck:
    """Binds an action parameter to a limited physical quantity.

    ``LimitCheck("voltage", "psu.voltage")`` means: whatever the caller passed
    as ``voltage`` is bounded by the ``psu.voltage`` limit.
    """

    param: str
    quantity: str
    #: When False a missing parameter is fine (e.g. an optional setpoint).
    required: bool = False


@dataclass(frozen=True, slots=True)
class DerivedLimitCheck:
    """A limit on a quantity computed from several parameters.

    24 V and 3 A can each sit inside their own limits while 72 W is not what
    the operator meant to put into a DUT.  Declaring this on the action rather
    than computing it inside a driver means it cannot be forgotten.
    """

    quantity: str
    params: tuple[str, ...]
    op: str = "product"

    def compute(self, values: Sequence[float]) -> float:
        if self.op == "product":
            result = 1.0
            for value in values:
                result *= value
            return result
        if self.op == "sum":
            return float(sum(values))
        raise ValueError(f"unknown derived limit op {self.op!r}")


class LimitEnforcer:
    """Applies the effective limit set to action parameters."""

    def __init__(self, config: SafetyConfig) -> None:
        self._config = config

    @property
    def config(self) -> SafetyConfig:
        return self._config

    def effective(self, quantity: str, device_id: str | None = None) -> SafetyLimit | None:
        return self._config.limit_for(quantity, device_id)

    def check_value(self, quantity: str, value: float, *, device_id: str | None = None) -> None:
        """Raise :class:`SafetyLimitExceeded` if ``value`` is out of bounds."""
        limit = self.effective(quantity, device_id)
        if limit is None:
            return
        violation = limit.violation(float(value))
        if violation is not None:
            raise SafetyLimitExceeded(
                violation,
                details={
                    "quantity": quantity,
                    "value": value,
                    "minimum": limit.minimum,
                    "maximum": limit.maximum,
                    "unit": limit.unit,
                    "device_id": device_id,
                },
                preserved="no command was sent to the device",
            )

    def check_params(
        self,
        params: Mapping[str, Any],
        checks: Sequence[LimitCheck],
        *,
        device_id: str | None = None,
    ) -> None:
        """Validate every limited parameter of one action request."""
        for check in checks:
            if check.param not in params or params[check.param] is None:
                if check.required:
                    raise SafetyLimitExceeded(
                        f"{check.param} is required so its {check.quantity} limit can be checked",
                        details={"quantity": check.quantity, "param": check.param},
                        preserved="no command was sent to the device",
                    )
                continue
            raw = params[check.param]
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise SafetyLimitExceeded(
                    f"{check.param} must be numeric to be limit-checked, got {raw!r}",
                    details={"quantity": check.quantity, "param": check.param, "value": raw},
                    preserved="no command was sent to the device",
                ) from exc
            self.check_value(check.quantity, value, device_id=device_id)

    def check_derived(
        self,
        params: Mapping[str, Any],
        checks: Sequence[DerivedLimitCheck],
        *,
        device_id: str | None = None,
    ) -> None:
        """Evaluate every declared derived limit for one action request."""
        for check in checks:
            values: list[float] = []
            for name in check.params:
                raw = params.get(name)
                if raw is None:
                    break
                try:
                    values.append(float(raw))
                except (TypeError, ValueError):
                    break
            else:
                self.check_value(check.quantity, check.compute(values), device_id=device_id)

    def check_derived_power(
        self,
        voltage: float | None,
        current: float | None,
        *,
        quantity: str = "psu.power",
        device_id: str | None = None,
    ) -> None:
        """Bound V x I as well as V and I individually.

        24 V and 3 A can each be inside their limits while the product is not
        something the operator meant to put into a DUT.
        """
        if voltage is None or current is None:
            return
        self.check_value(quantity, float(voltage) * float(current), device_id=device_id)

    def describe(self, device_id: str | None = None) -> dict[str, dict[str, Any]]:
        """Every effective limit, for ``fdctl status`` and the HMI."""
        out: dict[str, dict[str, Any]] = {}
        quantities = set(self._config.global_limits)
        if device_id:
            quantities |= set(self._config.device_limits.get(device_id, {}))
        for quantity in sorted(quantities):
            limit = self.effective(quantity, device_id)
            if limit is not None:
                out[quantity] = {
                    "minimum": limit.minimum,
                    "maximum": limit.maximum,
                    "unit": limit.unit,
                }
        return out
