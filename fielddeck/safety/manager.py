"""The safety authority.

Every hardware-affecting request passes through :meth:`SafetyManager.authorize`
before a driver sees it.  Safety is enforced here, server-side, because the
HMI, the CLI, recipes and Claude are all untrusted for this purpose — a client
that can decide it is allowed to do something is not an authorization system.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fielddeck.common.config import SafetyConfig
from fielddeck.common.errors import EstopActive, PermissionDenied
from fielddeck.common.events import Event, EventSeverity, EventType, new_event
from fielddeck.common.models import (
    ArmGrant,
    ArmScope,
    ClientSource,
    OutputLease,
    PermissionLevel,
    SafetySnapshot,
)
from fielddeck.common.timebase import Timestamp
from fielddeck.safety.arm import ArmRegistry
from fielddeck.safety.estop import EstopController
from fielddeck.safety.leases import LeaseManager
from fielddeck.safety.limits import LimitEnforcer

__all__ = ["SafetyManager"]

EmitFn = Callable[[Event], Any]


class SafetyManager:
    """Composes arm grants, limits, leases and the ESTOP latch."""

    def __init__(self, config: SafetyConfig, *, emit: EmitFn | None = None) -> None:
        self._config = config
        self._emit = emit or (lambda _event: None)
        self.arm_registry = ArmRegistry()
        self.leases = LeaseManager()
        self.limits = LimitEnforcer(config)
        self.estop_controller = EstopController(requires_ack=config.estop_requires_ack)

    def set_emitter(self, emit: EmitFn) -> None:
        self._emit = emit

    @property
    def config(self) -> SafetyConfig:
        return self._config

    # -- authorization -----------------------------------------------------

    def authorize(
        self,
        *,
        action: str,
        permission: PermissionLevel,
        device_id: str | None,
        source: ClientSource,
        allowed_during_estop: bool = False,
        request_id: str | None = None,
        session_id: str | None = None,
    ) -> ArmGrant | None:
        """Return the grant authorizing this action, or raise.

        ``None`` means the action is PASSIVE and needed no grant.  Denials are
        emitted as ``ACTION_DENIED`` so that refused attempts are as visible
        in the timeline as successful ones.
        """
        if permission in self._config.denied_permissions:
            self._deny(
                action,
                permission,
                device_id,
                source,
                request_id,
                session_id,
                f"{permission} is disabled by this deployment's safety policy",
            )
            raise PermissionDenied(
                f"{permission} actions are disabled by safety policy on this unit",
                details={"action": action, "permission": str(permission)},
                preserved="no command was sent to the device",
            )

        if self.estop_controller.active and not allowed_during_estop:
            status = self.estop_controller.status
            self._deny(
                action,
                permission,
                device_id,
                source,
                request_id,
                session_id,
                "emergency stop is latched",
            )
            raise EstopActive(
                "emergency stop is latched; acknowledge it before running "
                f"{action} (reason: {status.reason or 'unspecified'})",
                details={"action": action, "estop_reason": status.reason},
                preserved="captured data and session metadata are intact",
            )

        if not permission.requires_grant:
            return None

        grant = self.arm_registry.find(permission=permission, action=action, device_id=device_id)
        if grant is None:
            hint = f"fdctl arm {str(permission).lower()} --ttl 60"
            self._deny(
                action,
                permission,
                device_id,
                source,
                request_id,
                session_id,
                f"no active {permission} grant",
            )
            raise PermissionDenied(
                f"{action} requires an active {permission} authorization. "
                f"A human must grant it: {hint}",
                details={
                    "action": action,
                    "permission": str(permission),
                    "device_id": device_id,
                    "source": str(source),
                    "hint": hint,
                    "armed": [str(p) for p in self.arm_registry.armed_permissions()],
                },
                preserved="no command was sent to the device",
            )
        return grant

    def _deny(
        self,
        action: str,
        permission: PermissionLevel,
        device_id: str | None,
        source: ClientSource,
        request_id: str | None,
        session_id: str | None,
        reason: str,
    ) -> None:
        self._emit(
            new_event(
                EventType.ACTION_DENIED,
                source=source,
                severity=EventSeverity.WARNING,
                device_id=device_id,
                action=action,
                permission=permission,
                request_id=request_id,
                session_id=session_id,
                message=reason,
                payload={"reason": reason},
            )
        )

    # -- arming ------------------------------------------------------------

    def arm(
        self,
        *,
        permission: PermissionLevel,
        ttl_s: float | None,
        source: ClientSource,
        scope: ArmScope | None = None,
        note: str | None = None,
        session_id: str | None = None,
    ) -> ArmGrant:
        if self.estop_controller.active and self._config.estop_requires_ack:
            raise EstopActive(
                "cannot arm while emergency stop is latched; acknowledge it first "
                "(fdctl estop clear)",
                details={"permission": str(permission)},
            )
        grant = self.arm_registry.create(
            permission=permission,
            ttl_s=ttl_s if ttl_s is not None else self._config.default_arm_ttl_s,
            source=source,
            scope=scope,
            note=note,
            max_ttl_s=self._config.max_ttl(permission),
        )
        self._emit(
            new_event(
                EventType.ARM_GRANTED,
                source=source,
                severity=EventSeverity.WARNING,
                permission=permission,
                session_id=session_id,
                message=f"{permission} armed for {grant.ttl_s:g}s ({grant.scope.describe()})",
                payload=grant.model_dump(mode="json"),
            )
        )
        return grant

    def disarm(
        self,
        *,
        source: ClientSource,
        grant_id: str | None = None,
        session_id: str | None = None,
    ) -> list[ArmGrant]:
        if grant_id:
            grant = self.arm_registry.revoke(grant_id)
            revoked = [grant] if grant else []
        else:
            revoked = self.arm_registry.revoke_all()
        for grant in revoked:
            self._emit(
                new_event(
                    EventType.ARM_REVOKED,
                    source=source,
                    permission=grant.permission,
                    session_id=session_id,
                    message=f"{grant.permission} authorization revoked",
                    payload=grant.model_dump(mode="json"),
                )
            )
        return revoked

    # -- emergency stop ----------------------------------------------------

    def engage_estop(
        self,
        *,
        reason: str,
        source: ClientSource,
        session_id: str | None = None,
    ) -> list[OutputLease]:
        """Latch ESTOP and hand back every lease needing a safe-state action.

        Grants are revoked, leases are surrendered, evidence is untouched.
        """
        status = self.estop_controller.engage(reason, source)
        leases = self.leases.take_all()
        revoked = self.arm_registry.revoke_all(reason="emergency stop")
        self._emit(
            new_event(
                EventType.ESTOP,
                source=source,
                severity=EventSeverity.CRITICAL,
                session_id=session_id,
                message=f"EMERGENCY STOP: {reason}",
                payload={
                    "reason": reason,
                    "engaged_utc_ns": status.engaged_utc_ns,
                    "revoked_grants": [g.grant_id for g in revoked],
                    "surrendered_leases": [lease.lease_id for lease in leases],
                },
            )
        )
        return leases

    def acknowledge_estop(self, *, source: ClientSource, session_id: str | None = None) -> None:
        self.estop_controller.acknowledge(source)
        self._emit(
            new_event(
                EventType.ESTOP_CLEARED,
                source=source,
                severity=EventSeverity.WARNING,
                session_id=session_id,
                message="emergency stop acknowledged; system remains SAFE until re-armed",
            )
        )

    # -- periodic maintenance ---------------------------------------------

    def sweep(self) -> tuple[list[ArmGrant], list[OutputLease]]:
        """Expire grants and leases.  Called on the daemon's safety timer."""
        now = Timestamp.now().monotonic_ns
        expired_grants = self.arm_registry.sweep(now)
        for grant in expired_grants:
            self._emit(
                new_event(
                    EventType.ARM_EXPIRED,
                    permission=grant.permission,
                    message=f"{grant.permission} authorization expired",
                    payload=grant.model_dump(mode="json"),
                )
            )
        expired_leases = self.leases.sweep(now)
        for lease in expired_leases:
            self._emit(
                new_event(
                    EventType.LEASE_EXPIRED,
                    severity=EventSeverity.WARNING,
                    device_id=lease.device_id,
                    action=lease.action,
                    message=f"output lease {lease.lease_id} expired; driving safe state",
                    payload=lease.model_dump(mode="json"),
                )
            )
        return expired_grants, expired_leases

    # -- introspection -----------------------------------------------------

    def snapshot(self) -> SafetySnapshot:
        status = self.estop_controller.status
        return SafetySnapshot(
            estop_active=status.active,
            estop_reason=status.reason,
            estop_utc_ns=status.engaged_utc_ns,
            grants=self.arm_registry.active(),
            leases=self.leases.active(),
            armed_permissions=self.arm_registry.armed_permissions(),
        )

    def reset(self) -> None:
        """Return to boot state.  Used by tests and by daemon startup."""
        self.arm_registry.clear()
        self.leases.take_all()
        self.estop_controller = EstopController(requires_ack=self._config.estop_requires_ack)
