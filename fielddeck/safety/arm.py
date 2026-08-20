"""Arm grants — temporary, scoped, revocable authorization.

Authorization is exact-class: a ``POWER`` grant authorizes ``POWER`` actions
and nothing else.  An operator who wants two classes arms two classes, and
can see both counting down in the HMI banner.  Nothing inherits authority it
was not explicitly given.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterable

from fielddeck.common.errors import InvalidRequest, PermissionDenied
from fielddeck.common.models import ArmGrant, ArmScope, ClientSource, PermissionLevel
from fielddeck.common.timebase import Timestamp

__all__ = ["ArmRegistry"]


def _default_id() -> str:
    return f"arm-{secrets.token_hex(4)}"


class ArmRegistry:
    """In-memory grant store.

    Grants live only in this process: restarting ``instrumentd`` or rebooting
    the Pi drops every grant and returns the system to SAFE.  That is the
    whole point — there is no persistence to accidentally restore.
    """

    def __init__(self, *, id_factory: Callable[[], str] = _default_id) -> None:
        self._grants: dict[str, ArmGrant] = {}
        self._id_factory = id_factory

    # -- creation ----------------------------------------------------------

    def create(
        self,
        *,
        permission: PermissionLevel,
        ttl_s: float,
        source: ClientSource,
        scope: ArmScope | None = None,
        note: str | None = None,
        max_ttl_s: float | None = None,
    ) -> ArmGrant:
        """Issue a grant.  Raises rather than silently clamping to nothing."""
        if not source.may_create_grants:
            raise PermissionDenied(
                f"{source} may not create authorization grants; a human must arm "
                "FieldDeck from the HMI or fdctl",
                details={"source": str(source), "permission": str(permission)},
            )
        if permission is PermissionLevel.PASSIVE:
            raise InvalidRequest(
                "PASSIVE needs no authorization; there is nothing to arm",
                details={"permission": str(permission)},
            )
        if ttl_s <= 0:
            raise InvalidRequest("arm TTL must be positive", details={"ttl_s": ttl_s})

        effective_ttl = ttl_s
        clamped = False
        if max_ttl_s is not None and ttl_s > max_ttl_s:
            effective_ttl = max_ttl_s
            clamped = True

        ts = Timestamp.now()
        grant = ArmGrant(
            grant_id=self._id_factory(),
            permission=permission,
            scope=scope or ArmScope(),
            created_by=source,
            created_monotonic_ns=ts.monotonic_ns,
            created_utc_ns=ts.utc_ns,
            expires_monotonic_ns=ts.monotonic_ns + int(effective_ttl * 1e9),
            ttl_s=effective_ttl,
            note=(
                f"{note} (TTL clamped from {ttl_s:g}s to policy maximum)"
                if clamped and note
                else (f"TTL clamped from {ttl_s:g}s to policy maximum" if clamped else note)
            ),
        )
        self._grants[grant.grant_id] = grant
        return grant

    # -- lookup ------------------------------------------------------------

    def find(
        self,
        *,
        permission: PermissionLevel,
        action: str,
        device_id: str | None,
        at_monotonic_ns: int | None = None,
    ) -> ArmGrant | None:
        """The narrowest active grant authorizing this exact action, if any.

        Narrowest wins so that the audit trail records the most specific
        authorization the operator actually gave.
        """
        now = at_monotonic_ns if at_monotonic_ns is not None else Timestamp.now().monotonic_ns
        candidates = [
            grant
            for grant in self._grants.values()
            if grant.permission is permission
            and grant.is_active(now)
            and grant.scope.matches(device_id=device_id, action=action)
        ]
        if not candidates:
            return None
        specificity = {"action": 0, "device": 1, "all": 2}
        candidates.sort(key=lambda g: (specificity[g.scope.kind], g.expires_monotonic_ns))
        return candidates[0]

    def active(self, at_monotonic_ns: int | None = None) -> list[ArmGrant]:
        now = at_monotonic_ns if at_monotonic_ns is not None else Timestamp.now().monotonic_ns
        return sorted(
            (grant for grant in self._grants.values() if grant.is_active(now)),
            key=lambda g: g.expires_monotonic_ns,
        )

    def get(self, grant_id: str) -> ArmGrant | None:
        return self._grants.get(grant_id)

    def armed_permissions(self, at_monotonic_ns: int | None = None) -> list[PermissionLevel]:
        seen = {grant.permission for grant in self.active(at_monotonic_ns)}
        return sorted(seen, key=lambda p: p.rank)

    # -- removal -----------------------------------------------------------

    def revoke(self, grant_id: str, *, reason: str = "revoked by operator") -> ArmGrant | None:
        grant = self._grants.get(grant_id)
        if grant is None or grant.revoked:
            return None
        grant.revoked = True
        grant.revoked_reason = reason
        return grant

    def revoke_all(self, *, reason: str = "disarmed") -> list[ArmGrant]:
        now = Timestamp.now().monotonic_ns
        revoked: list[ArmGrant] = []
        for grant in list(self._grants.values()):
            if grant.is_active(now):
                grant.revoked = True
                grant.revoked_reason = reason
                revoked.append(grant)
        return revoked

    def sweep(self, at_monotonic_ns: int | None = None) -> list[ArmGrant]:
        """Return grants that have just expired, once each.

        The daemon calls this on a timer and emits ``ARM_EXPIRED`` for each
        result, so expiry is visible in the timeline rather than implicit.
        """
        now = at_monotonic_ns if at_monotonic_ns is not None else Timestamp.now().monotonic_ns
        expired: list[ArmGrant] = []
        for grant_id, grant in list(self._grants.items()):
            if grant.revoked:
                self._grants.pop(grant_id, None)
                continue
            if now >= grant.expires_monotonic_ns:
                expired.append(grant)
                self._grants.pop(grant_id, None)
        return expired

    def clear(self) -> None:
        self._grants.clear()

    def __len__(self) -> int:
        return len(self.active())

    def __iter__(self) -> Iterable[ArmGrant]:  # pragma: no cover - convenience
        return iter(self.active())
