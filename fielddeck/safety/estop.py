"""Emergency stop.

ESTOP is latched, not momentary.  Once engaged the system stays in the safe
state until a human acknowledges it, because "the fault cleared itself" is
not a reason to re-energise a bench.

ESTOP never deletes evidence.  Captures are flushed and closed, never
discarded — the moments either side of an emergency stop are usually the most
valuable data in the session.
"""

from __future__ import annotations

from dataclasses import dataclass

from fielddeck.common.models import ClientSource
from fielddeck.common.timebase import Timestamp

__all__ = ["EstopController", "EstopStatus"]


@dataclass(frozen=True, slots=True)
class EstopStatus:
    active: bool
    reason: str | None = None
    source: ClientSource | None = None
    engaged_monotonic_ns: int | None = None
    engaged_utc_ns: int | None = None


class EstopController:
    """Holds the latch.  Deliberately tiny and free of I/O."""

    def __init__(self, *, requires_ack: bool = True) -> None:
        self._requires_ack = requires_ack
        self._status = EstopStatus(active=False)

    @property
    def status(self) -> EstopStatus:
        return self._status

    @property
    def active(self) -> bool:
        return self._status.active

    @property
    def requires_ack(self) -> bool:
        return self._requires_ack

    def engage(self, reason: str, source: ClientSource) -> EstopStatus:
        """Latch ESTOP.  Idempotent: the first reason is the one that sticks."""
        if self._status.active:
            return self._status
        ts = Timestamp.now()
        self._status = EstopStatus(
            active=True,
            reason=reason,
            source=source,
            engaged_monotonic_ns=ts.monotonic_ns,
            engaged_utc_ns=ts.utc_ns,
        )
        return self._status

    def acknowledge(self, source: ClientSource) -> EstopStatus:
        """Clear the latch.  Only a human-facing client may do this."""
        from fielddeck.common.errors import PermissionDenied

        if not source.may_create_grants:
            raise PermissionDenied(
                f"{source} may not acknowledge an emergency stop; a human must "
                "clear it from the HMI or fdctl",
                details={"source": str(source)},
            )
        self._status = EstopStatus(active=False)
        return self._status
