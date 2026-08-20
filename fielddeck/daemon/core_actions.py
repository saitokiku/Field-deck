"""Daemon-level actions: system, device and session.

These are registered as global actions rather than special RPC methods so
they go through exactly the same pipeline as a CAN transmit — validated,
authorized, audited and recorded on the timeline.  Uniformity here is what
makes the audit trail complete.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import Field

from fielddeck import __version__
from fielddeck.common.config import compression_available_note
from fielddeck.common.models import (
    ClientSource,
    PermissionLevel,
    StrictModel,
)
from fielddeck.common.timebase import Timestamp, format_utc_ns
from fielddeck.drivers.base import ActionContext, NoParams, action, collect_actions

if TYPE_CHECKING:  # pragma: no cover
    from fielddeck.daemon.service import InstrumentDaemon

__all__ = ["CoreActions"]


class DeviceRef(StrictModel):
    device: str


class SessionStartParams(StrictModel):
    name: str
    operator: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionMarkParams(StrictModel):
    label: str
    note: str | None = None


class SessionNoteParams(StrictModel):
    text: str


class SessionRefParams(StrictModel):
    session_id: str | None = None


class SessionEventsParams(StrictModel):
    session_id: str | None = None
    limit: int = Field(default=200, ge=1, le=10000)
    offset: int = Field(default=0, ge=0)
    types: list[str] | None = None
    device_id: str | None = None
    severity_at_least: str | None = None


class SessionWindowParams(StrictModel):
    """The correlation query: everything around one instant."""

    session_id: str | None = None
    center_monotonic_ns: int | None = None
    around_event_type: str | None = None
    before_ms: float = Field(default=300.0, ge=0, le=600_000)
    after_ms: float = Field(default=100.0, ge=0, le=600_000)
    limit: int = Field(default=1000, ge=1, le=20000)


class ActionListParams(StrictModel):
    device: str | None = None
    permission: PermissionLevel | None = None


class CoreActions:
    """Bound to the daemon; registered into the action registry at startup."""

    def __init__(self, daemon: InstrumentDaemon) -> None:
        self.daemon = daemon

    def specs(self) -> dict[str, Any]:
        return collect_actions(self)

    # -- system ------------------------------------------------------------

    @action(
        "system.status",
        permission=PermissionLevel.PASSIVE,
        params=NoParams,
        state_changing=False,
        description="Overall daemon, safety, session and device state.",
        allowed_during_estop=True,
    )
    async def system_status(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
        daemon = self.daemon
        snapshot = daemon.safety.snapshot()
        now = Timestamp.now()
        return {
            "version": __version__,
            "simulated": daemon.config.simulate,
            "uptime_s": (now.monotonic_ns - daemon.started_at.monotonic_ns) / 1e9,
            "utc": format_utc_ns(now.utc_ns),
            "safety": {
                "state": snapshot.state_word,
                "estop_active": snapshot.estop_active,
                "estop_reason": snapshot.estop_reason,
                "armed": [
                    {
                        "grant_id": grant.grant_id,
                        "permission": str(grant.permission),
                        "scope": grant.scope.describe(),
                        "remaining_s": round(grant.remaining_s(now.monotonic_ns), 1),
                        "created_by": str(grant.created_by),
                    }
                    for grant in snapshot.grants
                ],
                "leases": [
                    {
                        "lease_id": lease.lease_id,
                        "device_id": lease.device_id,
                        "action": lease.action,
                        "owner": str(lease.owner),
                        "remaining_s": round(lease.remaining_s(now.monotonic_ns), 1),
                    }
                    for lease in snapshot.leases
                ],
            },
            "session": (
                {
                    "id": daemon.sessions.current.id,
                    "name": daemon.sessions.current.name,
                    "elapsed_s": round(daemon.sessions.current.elapsed_s(), 1),
                    "recording": True,
                }
                if daemon.sessions.current
                else None
            ),
            "devices": {
                "total": len(daemon.registry),
                "by_kind": _count_by_kind(daemon),
            },
            "running_actions": daemon.dispatcher.running(),
            "events": daemon.bus.stats(),
            "storage": {
                "sessions_dir": str(daemon.sessions.sessions_dir),
                "compression": compression_available_note(),
            },
        }

    @action(
        "system.discover",
        permission=PermissionLevel.PASSIVE,
        params=NoParams,
        state_changing=False,
        description="Re-run passive inventory of attached interfaces.",
        timeout_s=30.0,
    )
    async def system_discover(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
        """Stage 1 inventory only.  Nothing is transmitted to a DUT."""
        added, removed = await self.daemon.discover()
        return {
            "added": added,
            "removed": removed,
            "devices": [d.model_dump(mode="json") for d in self.daemon.registry.descriptors()],
        }

    @action(
        "system.limits",
        permission=PermissionLevel.PASSIVE,
        params=NoParams,
        state_changing=False,
        description="Effective safety limits for this deployment.",
        allowed_during_estop=True,
    )
    async def system_limits(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
        return {
            "global": self.daemon.safety.limits.describe(),
            "denied_permissions": [str(p) for p in self.daemon.safety.config.denied_permissions],
            "max_arm_ttl_s": {
                str(k): v for k, v in self.daemon.safety.config.max_arm_ttl_s.items()
            },
        }

    # -- devices -----------------------------------------------------------

    @action(
        "device.list",
        permission=PermissionLevel.PASSIVE,
        params=NoParams,
        state_changing=False,
        description="Every device currently known to instrumentd.",
        allowed_during_estop=True,
    )
    async def device_list(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
        return {
            "devices": [d.model_dump(mode="json") for d in self.daemon.registry.descriptors()],
            "aliases": self.daemon.registry.aliases,
        }

    @action(
        "device.status",
        permission=PermissionLevel.PASSIVE,
        params=DeviceRef,
        state_changing=False,
        description="Driver-reported status for one device. Never transmits.",
        allowed_during_estop=True,
    )
    async def device_status(self, ctx: ActionContext, params: DeviceRef) -> dict[str, Any]:
        driver = ctx.registry.resolve(params.device)
        return {
            "descriptor": driver.describe().model_dump(mode="json"),
            "status": await driver.status(),
            "busy_with": driver.busy_with,
        }

    @action(
        "action.list",
        permission=PermissionLevel.PASSIVE,
        params=ActionListParams,
        state_changing=False,
        description="Available actions and the permission each one requires.",
        allowed_during_estop=True,
    )
    async def action_list(self, ctx: ActionContext, params: ActionListParams) -> dict[str, Any]:
        device_id = ctx.registry.resolve(params.device).device_id if params.device else None
        descriptors = ctx.registry.action_descriptors(device_id=device_id)
        if params.permission is not None:
            descriptors = [d for d in descriptors if d.permission is params.permission]
        return {"actions": [d.model_dump(mode="json") for d in descriptors]}

    # -- sessions ----------------------------------------------------------

    @action(
        "session.start",
        permission=PermissionLevel.PASSIVE,
        params=SessionStartParams,
        state_changing=False,
        description="Open a new recording session.",
    )
    async def session_start(self, ctx: ActionContext, params: SessionStartParams) -> dict[str, Any]:
        session = self.daemon.sessions.start(
            params.name,
            operator=params.operator or self.daemon.config.operator,
            source=ctx.source,
            devices=self.daemon.registry.descriptors(),
            metadata=params.metadata,
        )
        return {"session": session.model_dump(mode="json")}

    @action(
        "session.stop",
        permission=PermissionLevel.PASSIVE,
        params=NoParams,
        state_changing=False,
        description="Close the active session and finalise its artifacts.",
        allowed_during_estop=True,
    )
    async def session_stop(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
        session = self.daemon.sessions.stop(source=ctx.source)
        return {"session": session.model_dump(mode="json")}

    @action(
        "session.mark",
        permission=PermissionLevel.PASSIVE,
        params=SessionMarkParams,
        state_changing=False,
        description="Drop an operator mark on the timeline.",
        allowed_during_estop=True,
    )
    async def session_mark(self, ctx: ActionContext, params: SessionMarkParams) -> dict[str, Any]:
        recorder = self._require_recorder()
        mark = recorder.mark(params.label, source=ctx.source, note=params.note)
        return {"mark": mark.model_dump(mode="json")}

    @action(
        "session.note",
        permission=PermissionLevel.PASSIVE,
        params=SessionNoteParams,
        state_changing=False,
        description="Append a free-text note to the active session.",
        allowed_during_estop=True,
    )
    async def session_note(self, ctx: ActionContext, params: SessionNoteParams) -> dict[str, Any]:
        recorder = self._require_recorder()
        recorder.note(params.text)
        return {"notes": len(recorder.session.notes)}

    @action(
        "session.list",
        permission=PermissionLevel.PASSIVE,
        params=NoParams,
        state_changing=False,
        description="Sessions on this device, newest first.",
        allowed_during_estop=True,
    )
    async def session_list(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
        return {"sessions": self.daemon.sessions.list_sessions()}

    @action(
        "session.get",
        permission=PermissionLevel.PASSIVE,
        params=SessionRefParams,
        state_changing=False,
        description="Metadata, timeline summary and artifacts for one session.",
        allowed_during_estop=True,
    )
    async def session_get(self, ctx: ActionContext, params: SessionRefParams) -> dict[str, Any]:
        return self.daemon.sessions.get(self._session_id(params.session_id))

    @action(
        "session.events",
        permission=PermissionLevel.PASSIVE,
        params=SessionEventsParams,
        state_changing=False,
        description="Query timeline events for a session.",
        allowed_during_estop=True,
    )
    async def session_events(
        self, ctx: ActionContext, params: SessionEventsParams
    ) -> dict[str, Any]:
        events = self.daemon.sessions.events(
            self._session_id(params.session_id),
            limit=params.limit,
            offset=params.offset,
            types=params.types,
            device_id=params.device_id,
            severity_at_least=params.severity_at_least,
        )
        return {"events": events, "count": len(events)}

    @action(
        "session.window",
        permission=PermissionLevel.PASSIVE,
        params=SessionWindowParams,
        state_changing=False,
        description="Correlated evidence around one instant across all subsystems.",
        allowed_during_estop=True,
    )
    async def session_window(
        self, ctx: ActionContext, params: SessionWindowParams
    ) -> dict[str, Any]:
        """Answers 'what happened 300 ms before the CAN fault?'."""
        session_id = self._session_id(params.session_id)
        center = params.center_monotonic_ns
        if center is None:
            if not params.around_event_type:
                raise _bad_window()
            events = self.daemon.sessions.events(
                session_id, types=[params.around_event_type], limit=1
            )
            if not events:
                from fielddeck.common.errors import SessionError

                raise SessionError(
                    f"no {params.around_event_type} event in session {session_id}",
                    details={"session_id": session_id, "type": params.around_event_type},
                )
            center = int(events[0]["monotonic_ns"])
        return self.daemon.sessions.window(
            session_id,
            center_monotonic_ns=center,
            before_ms=params.before_ms,
            after_ms=params.after_ms,
            limit=params.limit,
        )

    @action(
        "session.summary",
        permission=PermissionLevel.PASSIVE,
        params=SessionRefParams,
        state_changing=False,
        description="Deterministic session summary suitable for a report.",
        allowed_during_estop=True,
    )
    async def session_summary(self, ctx: ActionContext, params: SessionRefParams) -> dict[str, Any]:
        return self.daemon.sessions.summary(self._session_id(params.session_id))

    # -- helpers -----------------------------------------------------------

    def _require_recorder(self) -> Any:
        recorder = self.daemon.sessions.recorder
        if recorder is None:
            from fielddeck.common.errors import SessionError

            raise SessionError(
                'no active session; start one with: fdctl session start "<name>"',
            )
        return recorder

    def _session_id(self, given: str | None) -> str:
        if given:
            return given
        current = self.daemon.sessions.current_id
        if current is None:
            from fielddeck.common.errors import SessionError

            raise SessionError("no active session and no session_id given")
        return current


def _bad_window() -> Exception:
    from fielddeck.common.errors import InvalidRequest

    return InvalidRequest(
        "give either center_monotonic_ns or around_event_type",
        details={"hint": "around_event_type='DEVICE_FAULT'"},
    )


def _count_by_kind(daemon: InstrumentDaemon) -> dict[str, int]:
    counts: dict[str, int] = {}
    for descriptor in daemon.registry.descriptors():
        counts[str(descriptor.kind)] = counts.get(str(descriptor.kind), 0) + 1
    return counts


def _unused(source: ClientSource) -> None:  # pragma: no cover - import anchor
    return None
