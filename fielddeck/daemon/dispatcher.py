"""The action pipeline.

Every request from every client — HMI, fdctl, a recipe, Claude — passes
through :meth:`Dispatcher.execute`.  The order of the stages is the safety
model, and it is not negotiable:

1. resolve the action and the device
2. validate parameters against the action's schema
3. resolve the *effective* permission for these specific parameters
4. authorize against the arm grants (server-side, never the client)
5. check safety limits — which authorization cannot waive
6. take exclusive ownership of the device for state-changing work
7. run the handler under a timeout, cancellable
8. take or release the output lease
9. emit the outcome and record it in the session timeline

There is no bypass path.  A driver is never called except from here.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from fielddeck.capture.sessions import SessionManager
from fielddeck.common.errors import (
    ActionCancelled,
    ActionTimeout,
    DeviceBusy,
    FieldDeckError,
    InvalidRequest,
)
from fielddeck.common.events import EventSeverity, EventType, new_event
from fielddeck.common.logging import get_logger
from fielddeck.common.models import (
    ActionRequest,
    ActionResult,
    ClientSource,
    PermissionLevel,
)
from fielddeck.common.timebase import Timestamp, monotonic_ns
from fielddeck.daemon.events import EventBus
from fielddeck.daemon.registry import DeviceRegistry
from fielddeck.drivers.base import ActionContext, ActionSpec, Driver
from fielddeck.safety.manager import SafetyManager

__all__ = ["Dispatcher"]

_log = get_logger("fielddeck.daemon.dispatcher")

#: No client may ask for an unbounded action.  Long captures stream and are
#: cancellable instead of running without a deadline.
MAX_TIMEOUT_S = 3600.0


@dataclass(slots=True)
class _Running:
    key: str
    task: asyncio.Task[Any]
    ctx: ActionContext
    spec: ActionSpec
    device_id: str | None
    source: ClientSource
    started: Timestamp
    request_id: str | None = None
    lease_id: str | None = field(default=None)


class Dispatcher:
    """Runs actions under authorization, limits, leases and timeouts."""

    def __init__(
        self,
        *,
        registry: DeviceRegistry,
        safety: SafetyManager,
        bus: EventBus,
        sessions: SessionManager,
    ) -> None:
        self.registry = registry
        self.safety = safety
        self.bus = bus
        self.sessions = sessions
        self._running: dict[str, _Running] = {}
        self._counter = 0

    # -- public API --------------------------------------------------------

    async def execute(
        self, request: ActionRequest, *, connection_id: int | None = None
    ) -> ActionResult:
        """Run one action.  Never raises for expected failures — the failure
        is returned as an :class:`ActionResult` so the caller always gets the
        timing and the structured error together."""
        started = Timestamp.now()
        session_id = self.sessions.current_id
        try:
            result = await self._execute(request, connection_id, started, session_id)
        except FieldDeckError as exc:
            self._emit_failure(request, exc, session_id, started)
            return ActionResult(
                action=request.action,
                ok=False,
                error=exc.to_dict(),
                started_monotonic_ns=started.monotonic_ns,
                started_utc_ns=started.utc_ns,
                duration_ns=monotonic_ns() - started.monotonic_ns,
                request_id=request.request_id,
            )
        except Exception as exc:  # noqa: BLE001 - a driver bug must not take the daemon down with it
            _log.exception("unhandled error in action", extra={"action": request.action})
            wrapped = FieldDeckError(
                f"internal error running {request.action}: {exc}",
                details={"action": request.action, "type": type(exc).__name__},
                preserved="captured data and session metadata are intact",
            )
            self._emit_failure(request, wrapped, session_id, started)
            return ActionResult(
                action=request.action,
                ok=False,
                error=wrapped.to_dict(),
                started_monotonic_ns=started.monotonic_ns,
                started_utc_ns=started.utc_ns,
                duration_ns=monotonic_ns() - started.monotonic_ns,
                request_id=request.request_id,
            )
        return result

    async def cancel(self, *, request_id: str) -> bool:
        """Ask a running cancellable action to stop.  Returns whether it matched."""
        matched = False
        for running in list(self._running.values()):
            if running.request_id != request_id:
                continue
            matched = True
            running.ctx.cancel.set()
            if running.spec.cancelable:
                running.task.cancel()
        return matched

    def running(self) -> list[dict[str, Any]]:
        now = monotonic_ns()
        return [
            {
                "action": running.spec.name,
                "device_id": running.device_id,
                "source": str(running.source),
                "request_id": running.request_id,
                "permission": str(running.ctx.granted_permission),
                "elapsed_s": (now - running.started.monotonic_ns) / 1e9,
                "cancelable": running.spec.cancelable,
            }
            for running in self._running.values()
        ]

    # -- the pipeline ------------------------------------------------------

    async def _execute(
        self,
        request: ActionRequest,
        connection_id: int | None,
        started: Timestamp,
        session_id: str | None,
    ) -> ActionResult:
        # 1. Resolve action + device.
        spec, driver = self.registry.lookup(request.action, request.params)
        device_id = driver.device_id if driver is not None else None

        # 2. Validate parameters.  Defaults are applied here, so limits are
        #    checked against what the driver will actually receive.
        params = self._validate(spec, request.params)
        values = params.model_dump()

        # 3. Effective permission for *these* parameters.
        effective = spec.effective_permission(params)

        self.bus.publish(
            new_event(
                EventType.ACTION_REQUESTED,
                source=request.source,
                session_id=session_id,
                device_id=device_id,
                action=spec.name,
                permission=effective,
                request_id=request.request_id,
                message=f"{request.source} requested {spec.name}",
                payload={"params": _safe_params(values)},
            )
        )

        # 4. Authorize.  Raises PermissionDenied / EstopActive.
        self.safety.authorize(
            action=spec.name,
            permission=effective,
            device_id=device_id,
            source=request.source,
            allowed_during_estop=spec.allowed_during_estop,
            request_id=request.request_id,
            session_id=session_id,
        )

        # 5. Limits.  Being armed does not raise a ceiling.
        try:
            self.safety.limits.check_params(values, spec.limit_checks, device_id=device_id)
            self.safety.limits.check_derived(values, spec.derived_limit_checks, device_id=device_id)
        except FieldDeckError as exc:
            self.bus.publish(
                new_event(
                    EventType.LIMIT_REJECTED,
                    source=request.source,
                    severity=EventSeverity.WARNING,
                    session_id=session_id,
                    device_id=device_id,
                    action=spec.name,
                    permission=effective,
                    request_id=request.request_id,
                    message=exc.message,
                    payload=exc.details,
                )
            )
            raise

        # 6. Exclusive ownership for state-changing work.
        async with self._device_lock(spec, driver):
            ctx = ActionContext(
                source=request.source,
                emit=self.bus.publish,
                safety=self.safety,
                registry=self.registry,
                request_id=request.request_id,
                session_id=session_id,
                recorder=self.sessions.recorder,
                granted_permission=effective,
            )
            timeout = self._timeout_for(spec, request)
            ctx.deadline_monotonic_ns = monotonic_ns() + int(timeout * 1e9)

            self.bus.publish(
                new_event(
                    EventType.ACTION_STARTED,
                    source=request.source,
                    session_id=session_id,
                    device_id=device_id,
                    action=spec.name,
                    permission=effective,
                    request_id=request.request_id,
                    message=f"{spec.name} started",
                    payload={"timeout_s": timeout},
                )
            )

            if spec.is_capture:
                self.bus.publish(
                    new_event(
                        EventType.CAPTURE_STARTED,
                        source=request.source,
                        session_id=session_id,
                        device_id=device_id,
                        action=spec.name,
                        request_id=request.request_id,
                        message=f"capture started on {device_id or spec.name}",
                        payload={"params": _safe_params(values)},
                    )
                )
            try:
                payload = await self._run_handler(
                    spec, ctx, params, timeout, device_id, request, started
                )
            finally:
                if spec.is_capture:
                    # Emitted in a finally block: a capture that timed out or
                    # was cancelled still wrote bytes, and the timeline has to
                    # show where the recording ended.
                    self.bus.publish(
                        new_event(
                            EventType.CAPTURE_STOPPED,
                            source=request.source,
                            session_id=session_id,
                            device_id=device_id,
                            action=spec.name,
                            request_id=request.request_id,
                            message=f"capture stopped on {device_id or spec.name}",
                        )
                    )

        # 8. Leases.  Sustained outputs get a dead-man handle; turning an
        #    output off surrenders it.
        lease_info = self._settle_lease(spec, effective, device_id, request, values, connection_id)
        if lease_info:
            payload = {**payload, **lease_info}

        duration = monotonic_ns() - started.monotonic_ns
        self.bus.publish(
            new_event(
                EventType.ACTION_COMPLETED,
                source=request.source,
                session_id=session_id,
                device_id=device_id,
                action=spec.name,
                permission=effective,
                request_id=request.request_id,
                message=f"{spec.name} completed",
                payload={"duration_ns": duration, "result": _summarize(payload)},
            )
        )
        return ActionResult(
            action=spec.name,
            ok=True,
            result=payload,
            permission=effective,
            started_monotonic_ns=started.monotonic_ns,
            started_utc_ns=started.utc_ns,
            duration_ns=duration,
            request_id=request.request_id,
        )

    # -- stages ------------------------------------------------------------

    @staticmethod
    def _validate(spec: ActionSpec, raw: dict[str, Any]) -> BaseModel:
        try:
            return spec.params_model.model_validate(raw)
        except ValidationError as exc:
            raise InvalidRequest(
                f"invalid parameters for {spec.name}",
                details={
                    "action": spec.name,
                    "errors": [
                        {"field": ".".join(str(p) for p in err["loc"]), "problem": err["msg"]}
                        for err in exc.errors()
                    ],
                },
                preserved="no command was sent to the device",
            ) from exc

    @staticmethod
    def _timeout_for(spec: ActionSpec, request: ActionRequest) -> float:
        requested = request.timeout_s if request.timeout_s is not None else spec.timeout_s
        if requested <= 0:
            raise InvalidRequest("timeout must be positive", details={"timeout_s": requested})
        return min(requested, MAX_TIMEOUT_S)

    @contextlib.asynccontextmanager
    async def _device_lock(self, spec: ActionSpec, driver: Driver | None):
        """Serialise mutually exclusive control.  Passive readers coexist."""
        if driver is None or not spec.state_changing:
            yield
            return
        if driver.lock.locked():
            raise DeviceBusy(
                f"{driver.device_id} is busy with {driver.busy_with or 'another operation'}",
                details={"device_id": driver.device_id, "busy_with": driver.busy_with},
                preserved="the in-flight operation was not disturbed",
            )
        await driver.lock.acquire()
        driver._mark_busy(spec.name)
        try:
            yield
        finally:
            driver._mark_busy(None)
            driver.lock.release()

    async def _run_handler(
        self,
        spec: ActionSpec,
        ctx: ActionContext,
        params: BaseModel,
        timeout_s: float,
        device_id: str | None,
        request: ActionRequest,
        started: Timestamp,
    ) -> dict[str, Any]:
        self._counter += 1
        key = f"run-{self._counter}"
        task = asyncio.ensure_future(spec.handler(ctx, params))
        self._running[key] = _Running(
            key=key,
            task=task,
            ctx=ctx,
            spec=spec,
            device_id=device_id,
            source=request.source,
            started=started,
            request_id=request.request_id,
        )
        try:
            raw = await asyncio.wait_for(task, timeout_s)
        except TimeoutError as exc:
            raise ActionTimeout(
                f"{spec.name} did not finish within {timeout_s:g}s",
                details={"action": spec.name, "device_id": device_id, "timeout_s": timeout_s},
                preserved="any data written before the timeout is on disk",
            ) from exc
        except asyncio.CancelledError:
            if ctx.cancel.is_set():
                raise ActionCancelled(
                    f"{spec.name} was cancelled by {request.source}",
                    details={"action": spec.name, "device_id": device_id},
                    preserved="any data written before cancellation is on disk",
                ) from None
            raise
        finally:
            self._running.pop(key, None)
        return _as_dict(raw)

    def _settle_lease(
        self,
        spec: ActionSpec,
        effective: PermissionLevel,
        device_id: str | None,
        request: ActionRequest,
        values: dict[str, Any],
        connection_id: int | None,
    ) -> dict[str, Any]:
        if not spec.requires_lease or device_id is None:
            return {}
        session_id = self.sessions.current_id

        if effective is PermissionLevel.PASSIVE:
            # The safe direction: whatever this action was sustaining is over.
            released = [
                lease
                for lease in self.safety.leases.for_device(device_id)
                if lease.action == spec.name
            ]
            for lease in released:
                self.safety.leases.release(lease.lease_id)
                self.bus.publish(
                    new_event(
                        EventType.LEASE_RELEASED,
                        source=request.source,
                        session_id=session_id,
                        device_id=device_id,
                        action=spec.name,
                        message=f"output lease {lease.lease_id} released",
                        payload={"lease_id": lease.lease_id},
                    )
                )
            if released:
                self.bus.publish(
                    new_event(
                        EventType.OUTPUT_DISABLED,
                        source=request.source,
                        session_id=session_id,
                        device_id=device_id,
                        action=spec.name,
                        message=f"{device_id} output disabled",
                    )
                )
            return {"lease": None}

        ttl = values.get("lease_ttl_s") or self.safety.config.default_lease_ttl_s
        lease = self.safety.leases.acquire(
            device_id=device_id,
            action=spec.name,
            owner=request.source,
            ttl_s=float(ttl),
            safe_action=spec.name,
            safe_params={**values, **_safe_off_params(values)},
            owner_connection=connection_id,
        )
        self.bus.publish(
            new_event(
                EventType.LEASE_ACQUIRED,
                source=request.source,
                severity=EventSeverity.WARNING,
                session_id=session_id,
                device_id=device_id,
                action=spec.name,
                permission=effective,
                message=(
                    f"output lease {lease.lease_id} held for {lease.ttl_s:g}s; "
                    "the output drops to safe state if it is not renewed"
                ),
                payload=lease.model_dump(mode="json"),
            )
        )
        self.bus.publish(
            new_event(
                EventType.OUTPUT_ENABLED,
                source=request.source,
                severity=EventSeverity.WARNING,
                session_id=session_id,
                device_id=device_id,
                action=spec.name,
                permission=effective,
                message=f"{device_id} output enabled",
                payload={"lease_id": lease.lease_id},
            )
        )
        return {
            "lease": {
                "lease_id": lease.lease_id,
                "expires_in_s": lease.ttl_s,
                "renew_with": "safety.lease_renew",
            }
        }

    def _emit_failure(
        self,
        request: ActionRequest,
        exc: FieldDeckError,
        session_id: str | None,
        started: Timestamp,
    ) -> None:
        event_type = (
            EventType.ACTION_CANCELLED
            if isinstance(exc, ActionCancelled)
            else EventType.ACTION_FAILED
        )
        self.bus.publish(
            new_event(
                event_type,
                source=request.source,
                severity=EventSeverity.ERROR,
                session_id=session_id,
                action=request.action,
                request_id=request.request_id,
                message=exc.message,
                payload={
                    "error": exc.to_dict(),
                    "duration_ns": monotonic_ns() - started.monotonic_ns,
                },
            )
        )

    # -- lease reaping -----------------------------------------------------

    async def apply_safe_state(
        self, *, reason: str, device_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Drive devices to safe state, bypassing normal authorization.

        Used by ESTOP, lease expiry and shutdown.  This is the one path that
        does not consult the arm grants, because everything it does makes
        hardware *safer* — refusing to turn an output off because a grant
        lapsed would be the opposite of a safety system.
        """
        results: list[dict[str, Any]] = []
        drivers = (
            [d for d in self.registry.drivers if device_ids is None or d.device_id in device_ids]
            if device_ids is not None
            else self.registry.drivers
        )
        for driver in drivers:
            try:
                outcome = await asyncio.wait_for(driver.safe_state(), timeout=10.0)
            except Exception as exc:  # noqa: BLE001 - report the failure and keep driving the remaining devices safe
                outcome = {"device": driver.device_id, "applied": False, "error": str(exc)}
                _log.error(
                    "safe state failed",
                    extra={"device": driver.device_id, "error": str(exc)},
                )
            results.append(outcome)
            self.bus.publish(
                new_event(
                    EventType.SAFE_STATE_APPLIED,
                    severity=EventSeverity.WARNING,
                    session_id=self.sessions.current_id,
                    device_id=driver.device_id,
                    message=f"safe state applied to {driver.device_id}: {reason}",
                    payload={"reason": reason, "outcome": outcome},
                )
            )
        return results


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    return {"value": value}


def _safe_params(values: dict[str, Any]) -> dict[str, Any]:
    """Trim bulk payloads out of event records; the capture file has them."""
    out: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, (bytes, bytearray)):
            out[key] = f"<{len(value)} bytes>"
        elif isinstance(value, str) and len(value) > 256:
            out[key] = value[:256] + "..."
        elif isinstance(value, list) and len(value) > 32:
            out[key] = f"<{len(value)} items>"
        else:
            out[key] = value
    return out


def _summarize(payload: dict[str, Any]) -> dict[str, Any]:
    return _safe_params(payload)


def _safe_off_params(values: dict[str, Any]) -> dict[str, Any]:
    """The parameter overrides that turn a sustained action off.

    Keeps the lease's stored safe action honest: replaying ``psu.output`` with
    ``enabled=False`` is what "safe" means for that action.
    """
    overrides: dict[str, Any] = {}
    for key in ("enabled", "output", "on", "active", "transmit"):
        if key in values:
            overrides[key] = False
    return overrides
