"""Time semantics shared by every FieldDeck subsystem.

Two clocks, always recorded together:

``monotonic_ns``
    Never jumps, never goes backwards, survives NTP steps.  This is what
    correlation across CAN, serial, PSU and logic captures is built on.

``utc_ns``
    Wall clock, for humans, reports and cross-referencing external logs.

Original timestamps are never rewritten after capture.  If a translation
between the two clocks is needed later, use the :class:`TimeAnchor` that was
recorded when the session started.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

__all__ = [
    "TimeAnchor",
    "Timestamp",
    "format_utc_ns",
    "monotonic_ns",
    "now",
    "utc_ns",
]


def monotonic_ns() -> int:
    """Nanoseconds from an arbitrary, never-decreasing origin."""
    return time.monotonic_ns()


def utc_ns() -> int:
    """Nanoseconds since the Unix epoch, UTC."""
    return time.time_ns()


@dataclass(frozen=True, slots=True)
class Timestamp:
    """A monotonic/UTC pair captured at the same instant."""

    monotonic_ns: int
    utc_ns: int

    @classmethod
    def now(cls) -> Timestamp:
        # Read monotonic first: if the process is descheduled between the two
        # reads the UTC value is the one that drifts, and UTC is the clock we
        # only use for human reference.
        return cls(monotonic_ns=monotonic_ns(), utc_ns=utc_ns())

    @property
    def utc(self) -> datetime:
        return datetime.fromtimestamp(self.utc_ns / 1e9, tz=UTC)

    def isoformat(self) -> str:
        return format_utc_ns(self.utc_ns)

    def as_dict(self) -> dict[str, int]:
        return {"monotonic_ns": self.monotonic_ns, "utc_ns": self.utc_ns}


def now() -> Timestamp:
    """Shorthand for :meth:`Timestamp.now`."""
    return Timestamp.now()


@dataclass(frozen=True, slots=True)
class TimeAnchor:
    """Fixes the offset between the monotonic clock and UTC for a session.

    Recorded once at session start.  ``utc_for(monotonic_ns)`` lets a report
    render a monotonic-correlated event in wall-clock terms without ever
    mutating the stored timestamps.
    """

    monotonic_ns: int
    utc_ns: int

    @classmethod
    def capture(cls) -> TimeAnchor:
        ts = Timestamp.now()
        return cls(monotonic_ns=ts.monotonic_ns, utc_ns=ts.utc_ns)

    def utc_for(self, monotonic_ns_value: int) -> int:
        """Project a monotonic reading onto the UTC axis of this anchor."""
        return self.utc_ns + (monotonic_ns_value - self.monotonic_ns)

    def elapsed_s(self, monotonic_ns_value: int) -> float:
        """Seconds since the anchor, the ``+1.412223s`` column in a timeline."""
        return (monotonic_ns_value - self.monotonic_ns) / 1e9

    def as_dict(self) -> dict[str, int]:
        return {"monotonic_ns": self.monotonic_ns, "utc_ns": self.utc_ns}


def format_utc_ns(value: int) -> str:
    """Render epoch nanoseconds as an ISO-8601 UTC string with microseconds."""
    dt = datetime.fromtimestamp(value / 1e9, tz=UTC)
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def format_duration_ns(value: int) -> str:
    """Human-friendly duration for logs and the HMI."""
    if value < 1_000:
        return f"{value} ns"
    if value < 1_000_000:
        return f"{value / 1_000:.1f} us"
    if value < 1_000_000_000:
        return f"{value / 1_000_000:.1f} ms"
    return f"{value / 1_000_000_000:.3f} s"
