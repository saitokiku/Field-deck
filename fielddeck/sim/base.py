"""Shared helpers for simulated devices.

Simulation is a hard requirement, not a convenience: the whole application
must run on a laptop, in CI, and on a Pi with nothing plugged in.  These
drivers therefore implement the *same* :class:`~fielddeck.drivers.base.Driver`
contract and are dispatched through the *same* pipeline as real hardware.
There is no separate fake data path for the UI to read.

Traffic is generated deterministically from a seed, so a test that sees a
CRC error on frame 91 sees it again on the next run.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from fielddeck.common.timebase import monotonic_ns

__all__ = ["JitterClock", "SimulatedDeviceMixin", "seeded_random"]


def seeded_random(seed: str) -> random.Random:
    """Deterministic RNG keyed by a device id, so runs are reproducible."""
    return random.Random(seed)  # noqa: S311 - simulation jitter, not cryptography


@dataclass(slots=True)
class JitterClock:
    """Generates periodic timestamps with realistic jitter.

    Real buses are never exactly periodic.  A simulator that emits perfect
    100.000 ms spacing would let a periodicity analyser look far better than
    it will on a real DUT, which is the opposite of useful.
    """

    period_s: float
    jitter_s: float
    rng: random.Random

    def timestamps(self, start_ns: int, end_ns: int) -> list[int]:
        out: list[int] = []
        period_ns = int(self.period_s * 1e9)
        jitter_ns = int(self.jitter_s * 1e9)
        if period_ns <= 0:
            return out
        # Anchor on the period grid so two calls over adjacent windows do not
        # produce overlapping or missing frames.
        index = start_ns // period_ns
        while True:
            nominal = (index + 1) * period_ns
            if nominal > end_ns:
                break
            offset = self.rng.randint(-jitter_ns, jitter_ns) if jitter_ns else 0
            stamp = nominal + offset
            if start_ns <= stamp <= end_ns:
                out.append(stamp)
            index += 1
        return sorted(out)


class SimulatedDeviceMixin:
    """Marks a driver as simulated and gives it a virtual start time."""

    def __init__(self) -> None:
        self._sim_started_ns = monotonic_ns()

    @property
    def sim_elapsed_s(self) -> float:
        return (monotonic_ns() - self._sim_started_ns) / 1e9
