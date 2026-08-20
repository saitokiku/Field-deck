"""Output leases — a dead-man's handle for sustained hazardous outputs.

A PSU output, an electronic load, a repeated CAN transmit and a driven GPIO
all share a failure mode: the client that turned them on goes away and the
hardware stays energised.  A lease makes that impossible to ignore.  It has
an owner, a TTL and a named safe action, and when it lapses ``instrumentd``
runs that safe action itself.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any

from fielddeck.common.errors import LeaseError
from fielddeck.common.models import ClientSource, OutputLease
from fielddeck.common.timebase import Timestamp

__all__ = ["LeaseManager"]


def _default_id() -> str:
    return f"lease-{secrets.token_hex(4)}"


class LeaseManager:
    """Tracks live leases.  Executing safe actions is the daemon's job."""

    def __init__(self, *, id_factory: Callable[[], str] = _default_id) -> None:
        self._leases: dict[str, OutputLease] = {}
        self._id_factory = id_factory

    def acquire(
        self,
        *,
        device_id: str,
        action: str,
        owner: ClientSource,
        ttl_s: float,
        safe_action: str | None = None,
        safe_params: dict[str, Any] | None = None,
        owner_connection: int | None = None,
    ) -> OutputLease:
        if ttl_s <= 0:
            raise LeaseError("lease TTL must be positive", details={"ttl_s": ttl_s})
        ts = Timestamp.now()
        lease = OutputLease(
            lease_id=self._id_factory(),
            device_id=device_id,
            action=action,
            owner=owner,
            owner_connection=owner_connection,
            created_monotonic_ns=ts.monotonic_ns,
            expires_monotonic_ns=ts.monotonic_ns + int(ttl_s * 1e9),
            ttl_s=ttl_s,
            safe_action=safe_action,
            safe_params=dict(safe_params or {}),
        )
        self._leases[lease.lease_id] = lease
        return lease

    def renew(self, lease_id: str, *, ttl_s: float | None = None) -> OutputLease:
        lease = self._leases.get(lease_id)
        if lease is None or lease.released:
            raise LeaseError(
                f"no active lease {lease_id}",
                details={"lease_id": lease_id},
                preserved="the device was left in whatever state it was already in",
            )
        now = Timestamp.now().monotonic_ns
        if now >= lease.expires_monotonic_ns:
            raise LeaseError(
                f"lease {lease_id} already expired; re-acquire it rather than renewing",
                details={"lease_id": lease_id},
            )
        extension = ttl_s if ttl_s is not None else lease.ttl_s
        lease.expires_monotonic_ns = now + int(extension * 1e9)
        return lease

    def release(self, lease_id: str) -> OutputLease | None:
        lease = self._leases.pop(lease_id, None)
        if lease is None or lease.released:
            return None
        lease.released = True
        return lease

    def active(self, at_monotonic_ns: int | None = None) -> list[OutputLease]:
        now = at_monotonic_ns if at_monotonic_ns is not None else Timestamp.now().monotonic_ns
        return sorted(
            (lease for lease in self._leases.values() if lease.is_active(now)),
            key=lambda lease: lease.expires_monotonic_ns,
        )

    def for_device(self, device_id: str) -> list[OutputLease]:
        return [lease for lease in self.active() if lease.device_id == device_id]

    def sweep(self, at_monotonic_ns: int | None = None) -> list[OutputLease]:
        """Pop and return lapsed leases so the daemon can drive safe state."""
        now = at_monotonic_ns if at_monotonic_ns is not None else Timestamp.now().monotonic_ns
        expired: list[OutputLease] = []
        for lease_id, lease in list(self._leases.items()):
            if now >= lease.expires_monotonic_ns and not lease.released:
                expired.append(lease)
                self._leases.pop(lease_id, None)
        return expired

    def take_for_connection(self, connection_id: int) -> list[OutputLease]:
        """Reclaim every lease held by a client that just disconnected."""
        taken: list[OutputLease] = []
        for lease_id, lease in list(self._leases.items()):
            if lease.owner_connection == connection_id:
                taken.append(lease)
                self._leases.pop(lease_id, None)
        return taken

    def take_all(self) -> list[OutputLease]:
        """Used by ESTOP and shutdown: every lease becomes a safe-state action."""
        taken = list(self._leases.values())
        self._leases.clear()
        return taken

    def __len__(self) -> int:
        return len(self.active())
