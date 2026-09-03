"""A shared fault scenario for the simulated bench.

Without this, the simulated PSU and the simulated CAN bus each have their own
unrelated timer, and "correlate a fault across subsystems" cannot actually be
demonstrated — there is nothing to correlate.

With it, the simulated devices share one causal story — the worked example the
README walks through::

    output enabled
      -> current sits at 0.418 A into the 57.4 ohm load
      -> at +1.4 s the current climbs to 0.914 A
      -> 312 ms later the controller stops transmitting CAN 0x181
      -> the UART reports error 0x17

Every subsystem timestamps that story on the same monotonic axis, so
``session.window`` around the CAN dropout returns the current rise that
preceded it.  That is the whole point of the unified timeline, and it is now
reproducible on a laptop with ``FIELDDECK_SIM_FAULT=1``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from fielddeck.common.timebase import monotonic_ns

__all__ = ["Scenario", "scenario_enabled"]

#: Seconds after the output is enabled at which the current climbs.
CURRENT_RISE_AT_S = 1.4
#: How long the controller keeps transmitting after the current climbs.
CAN_SILENCE_DELAY_S = 0.312
#: Seconds after the current climbs at which the UART reports a fault.
UART_ERROR_DELAY_S = 0.319

NORMAL_CURRENT_A = 0.418
FAULT_CURRENT_A = 0.914


def scenario_enabled() -> bool:
    """``FIELDDECK_SIM_FAULT=1`` arms the scripted failure."""
    return os.environ.get("FIELDDECK_SIM_FAULT", "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Scenario:
    """Shared state the simulated devices consult.

    Deliberately tiny and side-effect free: it answers questions about the
    scripted story, and the drivers decide what to do about the answers.
    """

    armed: bool = False
    #: When the supply output was last enabled, on the monotonic axis.
    output_since_ns: int | None = field(default=None)

    def note_output(self, enabled: bool) -> None:
        self.output_since_ns = monotonic_ns() if enabled else None

    def _elapsed_s(self) -> float | None:
        if self.output_since_ns is None:
            return None
        return (monotonic_ns() - self.output_since_ns) / 1e9

    @property
    def current_a(self) -> float:
        """Load current, which climbs once the scripted fault develops."""
        elapsed = self._elapsed_s()
        if elapsed is None:
            return 0.0
        if self.armed and elapsed >= CURRENT_RISE_AT_S:
            return FAULT_CURRENT_A
        return NORMAL_CURRENT_A

    @property
    def fault_developing(self) -> bool:
        elapsed = self._elapsed_s()
        return bool(self.armed and elapsed is not None and elapsed >= CURRENT_RISE_AT_S)

    def can_id_silent(self, can_id: int, at_monotonic_ns: int) -> bool:
        """Whether the controller has stopped transmitting this id yet.

        Takes an explicit timestamp because CAN frames are generated for a
        window that has already elapsed, and asking "is it silent *now*" would
        retro-actively delete frames that really did arrive.
        """
        if not self.armed or can_id != 0x181 or self.output_since_ns is None:
            return False
        elapsed = (at_monotonic_ns - self.output_since_ns) / 1e9
        return elapsed >= CURRENT_RISE_AT_S + CAN_SILENCE_DELAY_S

    def uart_error(self, at_monotonic_ns: int) -> bool:
        if not self.armed or self.output_since_ns is None:
            return False
        elapsed = (at_monotonic_ns - self.output_since_ns) / 1e9
        return elapsed >= CURRENT_RISE_AT_S + UART_ERROR_DELAY_S

    def describe(self) -> dict[str, object]:
        return {
            "armed": self.armed,
            "output_since_ns": self.output_since_ns,
            "current_a": round(self.current_a, 4),
            "fault_developing": self.fault_developing,
            "story": (
                "output on -> +1.400 s current 0.418 A climbs to 0.914 A "
                "-> +1.712 s CAN 0x181 stops -> +1.719 s UART error 0x17"
                if self.armed
                else "no fault armed; set FIELDDECK_SIM_FAULT=1 to arm it"
            ),
        }
