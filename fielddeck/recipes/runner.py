"""Executing a compiled plan, and cleaning up whatever happens.

The runner is a *client*.  It holds no authority: every step goes out over the
control socket as ``source=recipe``, and ``instrumentd`` decides whether it is
allowed, exactly as it would for a human typing ``fdctl``.  A recipe cannot arm
anything — :attr:`ClientSource.RECIPE` is not permitted to create grants — so a
recipe that needs POWER needs an operator who has already armed POWER.  That is
checked in preflight, before step one, because discovering it at step four with
a rail already at 24 V helps nobody.

Three properties are load-bearing:

**``finally`` runs.**  On success, on a failed assertion, on a step error, on a
timeout, on cancellation, on device loss, on ESTOP.  When the run is cancelled
mid-flight the cleanup is shielded so it still completes.  What it is *not* is a
guarantee: if the socket itself has gone, no client can turn anything off.  The
guarantee lives in the daemon — the output lease this runner's connection holds
expires, and ``instrumentd`` drives the device safe.  The ``finally`` phase is
the tidy path; the lease is the one that survives the runner being killed.

**ESTOP supersedes recipe logic.**  The safety state is re-read before every
step, and an emergency stop stops the run immediately rather than at the next
convenient boundary.  A running step is cancelled through the daemon by request
id, which is why every step carries one.

**Nothing is decided here.**  Preflight refusals are conservative: the runner
can refuse to start something the daemon would have allowed, never the reverse.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from pydantic import Field

from fielddeck.common.errors import ErrorCode, FieldDeckError, RecipeError
from fielddeck.common.events import Event, EventSeverity, EventType, new_event
from fielddeck.common.logging import get_logger
from fielddeck.common.models import (
    ActionResult,
    ArmGrant,
    ClientSource,
    PermissionLevel,
    StrictModel,
)
from fielddeck.common.timebase import Timestamp, monotonic_ns
from fielddeck.daemon.client import InstrumentClient
from fielddeck.recipes.assertions import compile_expression, evaluate_assertion, namespace_entry
from fielddeck.recipes.compiler import ExecutionPlan, PlannedStep
from fielddeck.recipes.schema import RecipePhase, StepKind

__all__ = ["RecipeRun", "RecipeRunner", "RecipeState", "StepOutcome", "StepRecord"]

_log = get_logger("fielddeck.recipes.runner")

#: How long the cleanup phase gets when the run is being torn down.  Long
#: enough for a handful of safe-state actions, short enough that a wedged
#: daemon does not hold the operator hostage.
CLEANUP_TIMEOUT_S = 30.0
#: Slack added to the plan's estimate when the caller gives no deadline.
DEADLINE_SLACK_S = 30.0
MAX_DEADLINE_S = 3600.0
#: Per-step ceiling for a cleanup action, so one wedged device cannot eat the
#: whole cleanup budget and starve the outputs behind it.
CLEANUP_STEP_TIMEOUT_S = 10.0

#: Failures that mean the run was interrupted rather than that the DUT failed
#: the test.  The distinction matters: FAILED is a test result an engineer
#: should read, ABORTED is a run that never got to say anything.
_ABORT_REASONS: dict[str, str] = {
    str(ErrorCode.ESTOP_ACTIVE): "emergency stop is latched",
    str(ErrorCode.DEVICE_NOT_FOUND): "a required device is no longer present",
    str(ErrorCode.DEVICE_DISCONNECTED): "a required device disconnected",
    str(ErrorCode.ACTION_CANCELLED): "a step was cancelled",
    str(ErrorCode.TRANSPORT_ERROR): "the connection to instrumentd was lost",
}


class RecipeState(StrEnum):
    PENDING = "PENDING"
    PREFLIGHT = "PREFLIGHT"
    RUNNING = "RUNNING"
    CANCELLING = "CANCELLING"
    FAILED = "FAILED"
    PASSED = "PASSED"
    ABORTED = "ABORTED"

    @property
    def finished(self) -> bool:
        return self in (RecipeState.FAILED, RecipeState.PASSED, RecipeState.ABORTED)


class StepOutcome(StrEnum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class StepRecord(StrictModel):
    """What one step actually did, as opposed to what it was planned to do."""

    index: int
    phase: RecipePhase
    kind: StepKind
    description: str
    outcome: StepOutcome
    action: str | None = None
    device_id: str | None = None
    permission: PermissionLevel = PermissionLevel.PASSIVE
    started_monotonic_ns: int = 0
    started_utc_ns: int = 0
    duration_s: float = 0.0
    #: Trimmed: bulk payloads stay in the capture files, not in the report.
    result: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    expression: str | None = None
    message: str | None = None
    assertion: dict[str, Any] | None = None


class RecipeRun(StrictModel):
    """The full record of one execution, whatever became of it."""

    run_id: str
    recipe: str
    state: RecipeState
    dry_run: bool = False
    session_id: str | None = None
    started_utc_ns: int = 0
    ended_utc_ns: int = 0
    duration_s: float = 0.0
    plan: dict[str, Any] = Field(default_factory=dict)
    steps: list[StepRecord] = Field(default_factory=list)
    finally_steps: list[StepRecord] = Field(default_factory=list)
    assertions_passed: int = 0
    assertions_failed: int = 0
    #: The first thing that went wrong; the reason the run stopped where it did.
    failure: dict[str, Any] | None = None
    reason: str | None = None
    estop: bool = False
    cancelled: bool = False
    finally_ran: bool = False
    cleanup_note: str | None = None
    leases_held: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    #: The scalar namespace assertions saw, kept for post-mortem reading.
    namespace: dict[str, Any] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.state is RecipeState.PASSED

    def headline(self) -> str:
        if self.state is RecipeState.PASSED:
            return f"{self.recipe} PASSED ({len(self.steps)} steps, {self.duration_s:.1f}s)"
        detail = f": {self.reason}" if self.reason else ""
        return f"{self.recipe} {self.state} after {len(self.steps)} steps{detail}"


class RecipeRunner:
    """Runs one compiled plan through one client connection."""

    def __init__(
        self,
        client: InstrumentClient,
        plan: ExecutionPlan,
        *,
        emit: Callable[[Event], Any] | None = None,
        run_id: str | None = None,
        dry_run: bool = False,
        open_session: bool = True,
        deadline_s: float | None = None,
    ) -> None:
        self.client = client
        self.plan = plan
        self.run_id = run_id or f"run-{secrets.token_hex(4)}"
        self.dry_run = dry_run
        self.open_session = open_session
        self._emit = emit or (lambda _event: None)
        self._deadline_s = min(
            deadline_s
            if deadline_s is not None
            else plan.estimated_duration_s * 2 + DEADLINE_SLACK_S,
            MAX_DEADLINE_S,
        )
        self._state = RecipeState.PENDING
        self._stop = asyncio.Event()
        self._namespace: dict[str, dict[str, Any]] = {}
        self._steps: list[StepRecord] = []
        self._finally: list[StepRecord] = []
        self._warnings: list[str] = []
        self._leases: list[str] = []
        self._failure: dict[str, Any] | None = None
        self._reason: str | None = None
        self._estop = False
        self._cancelled = False
        self._finally_ran = False
        self._cleanup_note: str | None = None
        self._session_id: str | None = None
        self._session_opened_here = False
        self._started = Timestamp.now()
        self._deadline_ns = 0
        self._passed = 0
        self._failed = 0

    # -- public API --------------------------------------------------------

    @property
    def state(self) -> RecipeState:
        return self._state

    def cancel(self, reason: str = "cancelled by operator") -> None:
        """Ask the run to stop.  Cooperative: the current step finishes or is
        cancelled through the daemon, then ``finally`` runs."""
        if self._state.finished:
            return
        self._cancelled = True
        self._reason = self._reason or reason
        self._state = RecipeState.CANCELLING
        self._stop.set()

    async def run(self) -> RecipeRun:
        self._started = Timestamp.now()
        self._deadline_ns = self._started.monotonic_ns + int(self._deadline_s * 1e9)
        self._state = RecipeState.PREFLIGHT
        self._emit_event(
            EventType.RECIPE_STARTED,
            message=(
                f"recipe {self.plan.recipe_name} "
                f"{'dry run' if self.dry_run else 'started'} "
                f"({len(self.plan.steps)} steps, max {self.plan.max_permission})"
            ),
            payload={"run_id": self.run_id, "dry_run": self.dry_run, "plan": self.plan.summary()},
        )

        try:
            await self._preflight()
        except FieldDeckError as exc:
            # Nothing has run, so there is nothing to clean up.  Raise rather
            # than return: the operator asked to run a recipe and it did not
            # start, and the reason belongs in the error, not buried in a report.
            self._state = RecipeState.FAILED
            self._failure = exc.to_dict()
            self._reason = exc.message
            self._cleanup_note = "no step ran, so no cleanup was needed"
            self._emit_event(
                EventType.RECIPE_FINISHED,
                severity=EventSeverity.WARNING,
                message=f"recipe {self.plan.recipe_name} did not start: {exc.message}",
                payload={"run_id": self.run_id, "state": str(self._state)},
            )
            raise

        if self.dry_run:
            self._state = RecipeState.PENDING
            self._cleanup_note = "dry run: nothing was executed"
            self._emit_event(
                EventType.RECIPE_FINISHED,
                message=f"recipe {self.plan.recipe_name} dry run complete; it would run now",
                payload={"run_id": self.run_id, "state": str(self._state), "dry_run": True},
            )
            return self._record()

        self._state = RecipeState.RUNNING
        try:
            await self._run_steps()
        except asyncio.CancelledError:
            # The action running this recipe was cancelled or timed out.  The
            # cleanup still has to happen, so it runs shielded before the
            # cancellation is allowed to continue on its way.
            self._cancelled = True
            self._state = RecipeState.CANCELLING
            self._reason = self._reason or "the recipe run was cancelled"
            await self._shielded_cleanup()
            self._finish_state()
            self._emit_finished()
            raise
        else:
            await self._cleanup()
        self._finish_state()
        self._emit_finished()
        return self._record()

    # -- preflight ---------------------------------------------------------

    async def _preflight(self) -> None:
        """Everything that can be known before the first step, checked at once."""
        self.plan.raise_if_unrunnable()

        safety = await self.client.call("safety.status")
        if safety.get("estop_active"):
            self._estop = True
            raise RecipeError(
                "emergency stop is latched; acknowledge it before running "
                f"{self.plan.recipe_name} (fdctl estop clear)",
                details={"recipe": self.plan.recipe_name, "reason": safety.get("estop_reason")},
                preserved="nothing was run and nothing was energised",
            )
        self._check_authorization(safety)

        status = await self.client.call("system.status")
        session = status.get("session")
        self._session_id = session.get("id") if isinstance(session, dict) else None
        if self._session_id is None and self.open_session and not self.dry_run:
            await self._start_session()

    def _check_authorization(self, safety: dict[str, Any]) -> None:
        """Refuse up front when the operator has not armed what this needs.

        This is a courtesy, not enforcement: the daemon authorizes every step
        regardless.  It exists so a POWER recipe fails at second zero with
        "arm POWER" rather than at step four with an energised DUT.
        """
        if not self.plan.permissions_required:
            return
        grants = [ArmGrant.model_validate(raw) for raw in safety.get("grants", [])]
        remaining: dict[str, float] = {
            str(key): float(value)
            for key, value in (safety.get("grants_remaining_s") or {}).items()
        }

        missing: dict[PermissionLevel, list[str]] = {}
        tightest: dict[PermissionLevel, float] = {}
        for step in self.plan.all_steps:
            if not step.permission.requires_grant or step.action is None:
                continue
            grant = next(
                (
                    candidate
                    for candidate in grants
                    if candidate.permission is step.permission
                    and candidate.scope.matches(device_id=step.device_id, action=step.action)
                ),
                None,
            )
            if grant is None:
                actions = missing.setdefault(step.permission, [])
                if step.action not in actions:
                    actions.append(step.action)
            else:
                left = remaining.get(grant.grant_id, grant.ttl_s)
                current = tightest.get(step.permission)
                tightest[step.permission] = left if current is None else min(current, left)

        if missing:
            classes = sorted(missing, key=lambda permission: permission.rank)
            words = " ".join(str(permission).lower() for permission in classes)
            detail = "; ".join(
                f"{permission} for {', '.join(missing[permission])}" for permission in classes
            )
            raise RecipeError(
                f"{self.plan.recipe_name} needs {detail}. A recipe cannot arm anything: "
                f"an operator must run 'fdctl arm {words} --ttl "
                f"{max(60, int(self.plan.estimated_duration_s * 2))}' first.",
                details={
                    "recipe": self.plan.recipe_name,
                    "required": [str(permission) for permission in classes],
                    "actions": {str(k): v for k, v in missing.items()},
                    "armed": [str(grant.permission) for grant in grants],
                    "hint": f"fdctl arm {words}",
                },
                preserved="nothing was run and nothing was energised",
            )

        for permission, left in sorted(tightest.items(), key=lambda item: item[0].rank):
            if left < self.plan.estimated_duration_s:
                self._warnings.append(
                    f"the {permission} authorization has {left:.0f}s left but the recipe "
                    f"needs about {self.plan.estimated_duration_s:.0f}s; it will expire "
                    "mid-run unless you re-arm with a longer TTL"
                )

    async def _start_session(self) -> None:
        """A recipe run without a session leaves no evidence, so open one."""
        result = await self.client.execute(
            "session.start",
            {
                "name": f"recipe {self.plan.recipe_name}",
                "metadata": {
                    "recipe": self.plan.recipe_name,
                    "run_id": self.run_id,
                    "source": self.plan.source,
                },
            },
        )
        session = result.result.get("session") or {}
        self._session_id = session.get("id")
        self._session_opened_here = True

    # -- execution ---------------------------------------------------------

    async def _run_steps(self) -> None:
        for step in self.plan.steps:
            if not await self._may_continue():
                self._steps.extend(self._skipped(step))
                break
            record = await self._execute(step)
            self._steps.append(record)
            if record.outcome is not StepOutcome.OK:
                break

    async def _may_continue(self) -> bool:
        """Re-read the world between steps.  ESTOP wins over recipe logic."""
        if self._stop.is_set():
            return False
        if monotonic_ns() > self._deadline_ns:
            self._reason = (
                f"the recipe exceeded its {self._deadline_s:.0f}s deadline; "
                "raise it or shorten the recipe"
            )
            self._cancelled = True
            self._stop.set()
            return False
        try:
            safety = await self.client.call("safety.status")
        except FieldDeckError as exc:
            self._reason = f"lost contact with instrumentd: {exc.message}"
            self._failure = exc.to_dict()
            self._stop.set()
            return False
        if safety.get("estop_active"):
            self._estop = True
            self._reason = f"emergency stop: {safety.get('estop_reason') or 'engaged'}"
            self._stop.set()
            return False
        return True

    def _skipped(self, step: PlannedStep) -> list[StepRecord]:
        return [
            StepRecord(
                index=step.index,
                phase=step.phase,
                kind=step.kind,
                description=step.describe(),
                outcome=StepOutcome.SKIPPED,
                action=step.action,
                device_id=step.device_id,
                permission=step.permission,
            )
        ]

    async def _execute(self, step: PlannedStep) -> StepRecord:
        started = Timestamp.now()
        self._emit_event(
            EventType.RECIPE_STEP_STARTED,
            action=step.action,
            device_id=step.device_id,
            permission=step.permission,
            message=f"step {step.index}: {step.describe()}",
            payload={"run_id": self.run_id, "step": step.index, "phase": str(step.phase)},
        )
        if step.kind is StepKind.WAIT:
            record = await self._run_wait(step, started)
        elif step.kind is StepKind.ASSERT:
            record = self._run_assert(step, started)
        else:
            record = await self._run_action(step, started)

        self._emit_event(
            EventType.RECIPE_STEP_COMPLETED,
            severity=(
                EventSeverity.INFO if record.outcome is StepOutcome.OK else EventSeverity.WARNING
            ),
            action=step.action,
            device_id=step.device_id,
            permission=step.permission,
            message=f"step {step.index}: {step.describe()} -> {record.outcome}",
            payload={
                "run_id": self.run_id,
                "step": step.index,
                "outcome": str(record.outcome),
                "duration_s": round(record.duration_s, 3),
                "error": record.error,
            },
        )
        if record.outcome is StepOutcome.FAILED and self._failure is None:
            self._failure = record.error or {
                "code": "AssertionFailed",
                "message": record.message or record.description,
            }
            self._reason = self._reason or (record.error or {}).get("message") or record.message
        return record

    async def _run_wait(self, step: PlannedStep, started: Timestamp) -> StepRecord:
        seconds = min(step.seconds or 0.0, max(0.0, (self._deadline_ns - monotonic_ns()) / 1e9))
        interrupted = False
        try:
            # Waiting on the stop event rather than sleeping means a cancel or
            # an ESTOP does not have to wait out a settling time.
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            interrupted = True
        except TimeoutError:
            pass
        return self._record_step(
            step,
            started,
            StepOutcome.CANCELLED if interrupted else StepOutcome.OK,
            result={"waited_s": round((monotonic_ns() - started.monotonic_ns) / 1e9, 3)},
        )

    def _run_assert(self, step: PlannedStep, started: Timestamp) -> StepRecord:
        assert step.expression is not None
        try:
            # Recompiled rather than carried across: the allowlist check runs
            # once more, on the same string, immediately before evaluation.
            compiled = compile_expression(step.expression)
            outcome = evaluate_assertion(compiled, self._namespace)
        except RecipeError as exc:
            self._failed += 1
            self._emit_event(
                EventType.RECIPE_ASSERTION,
                severity=EventSeverity.ERROR,
                message=f"assertion could not be evaluated: {exc.message}",
                payload={"run_id": self.run_id, "step": step.index, "error": exc.to_dict()},
            )
            return self._record_step(
                step,
                started,
                StepOutcome.FAILED,
                error=exc.to_dict(),
                message=step.message,
            )

        if outcome.passed:
            self._passed += 1
        else:
            self._failed += 1
        self._emit_event(
            EventType.RECIPE_ASSERTION,
            severity=EventSeverity.INFO if outcome.passed else EventSeverity.ERROR,
            message=(
                f"assertion {'passed' if outcome.passed else 'FAILED'}: {outcome.detail}"
                + (f" - {step.message}" if step.message and not outcome.passed else "")
            ),
            payload={
                "run_id": self.run_id,
                "step": step.index,
                "expression": step.expression,
                **outcome.as_dict(),
            },
        )
        return self._record_step(
            step,
            started,
            StepOutcome.OK if outcome.passed else StepOutcome.FAILED,
            message=step.message,
            assertion=outcome.as_dict(),
        )

    async def _run_action(
        self, step: PlannedStep, started: Timestamp, *, timeout_s: float | None = None
    ) -> StepRecord:
        assert step.action is not None
        request_id = f"{self.run_id}.{step.index}"
        call = asyncio.ensure_future(
            self.client.execute(
                step.action,
                step.params,
                timeout_s=timeout_s if timeout_s is not None else step.timeout_s,
                request_id=request_id,
            )
        )
        stop = asyncio.ensure_future(self._stop.wait())
        try:
            done, _pending = await asyncio.wait({call, stop}, return_when=asyncio.FIRST_COMPLETED)
            if call not in done:
                # Stopping mid-step: ask the daemon to cancel the action by its
                # request id rather than dropping the call on the floor, so the
                # driver gets to finish writing whatever it had captured.
                with contextlib.suppress(FieldDeckError):
                    await self.client.call("action.cancel", {"request_id": request_id})
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(call), CLEANUP_STEP_TIMEOUT_S)
        finally:
            stop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop

        try:
            result: ActionResult = await call
        except FieldDeckError as exc:
            reason = _ABORT_REASONS.get(str(exc.code))
            if reason is not None:
                self._reason = self._reason or reason
                self._cancelled = self._cancelled or exc.code is ErrorCode.ACTION_CANCELLED
                if exc.code is ErrorCode.ESTOP_ACTIVE:
                    self._estop = True
                self._stop.set()
            return self._record_step(step, started, StepOutcome.FAILED, error=exc.to_dict())

        payload = dict(result.result)
        self._merge_namespace(step, payload)
        lease = payload.get("lease")
        if isinstance(lease, dict) and lease.get("lease_id"):
            self._leases.append(str(lease["lease_id"]))
        return self._record_step(step, started, StepOutcome.OK, result=payload)

    def _merge_namespace(self, step: PlannedStep, result: dict[str, Any]) -> None:
        if step.action is None:
            return
        name = step.store or step.action.split(".", 1)[0]
        entry = self._namespace.setdefault(name, {})
        entry.update(namespace_entry(result))

    def _record_step(
        self,
        step: PlannedStep,
        started: Timestamp,
        outcome: StepOutcome,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        message: str | None = None,
        assertion: dict[str, Any] | None = None,
    ) -> StepRecord:
        return StepRecord(
            index=step.index,
            phase=step.phase,
            kind=step.kind,
            description=step.describe(),
            outcome=outcome,
            action=step.action,
            device_id=step.device_id,
            permission=step.permission,
            started_monotonic_ns=started.monotonic_ns,
            started_utc_ns=started.utc_ns,
            duration_s=round((monotonic_ns() - started.monotonic_ns) / 1e9, 3),
            result=_trim(result or {}),
            error=error,
            expression=step.expression,
            message=message,
            assertion=assertion,
        )

    # -- cleanup -----------------------------------------------------------

    async def _cleanup(self) -> None:
        await self._run_finally()
        await self._close_session()

    async def _shielded_cleanup(self) -> None:
        """Cleanup that survives the run being cancelled underneath it."""
        task = asyncio.ensure_future(self._cleanup())
        try:
            await asyncio.wait_for(asyncio.shield(task), CLEANUP_TIMEOUT_S)
        except (asyncio.CancelledError, TimeoutError):
            self._cleanup_note = (
                "cleanup did not finish in time; instrumentd will drive the affected "
                "devices to safe state when their output leases lapse"
            )
            # Left running deliberately: it is turning outputs off.  The callback
            # keeps the result observed so asyncio does not report it as lost.
            task.add_done_callback(_log_late_cleanup)

    async def _run_finally(self) -> None:
        """Always attempted, and every step is attempted even if one fails."""
        if not self.plan.finally_steps:
            self._finally_ran = True
            self._cleanup_note = self._cleanup_note or "the recipe declares no finally steps"
            return
        for step in self.plan.finally_steps:
            started = Timestamp.now()
            try:
                if step.kind is StepKind.ASSERT:
                    record = self._run_assert(step, started)
                elif step.kind is StepKind.WAIT:
                    record = self._record_step(step, started, StepOutcome.OK)
                else:
                    record = await self._run_action(step, started, timeout_s=CLEANUP_STEP_TIMEOUT_S)
            except FieldDeckError as exc:  # pragma: no cover - _run_action absorbs these
                record = self._record_step(step, started, StepOutcome.FAILED, error=exc.to_dict())
            self._finally.append(record)
            self._emit_event(
                EventType.RECIPE_STEP_COMPLETED,
                severity=(
                    EventSeverity.INFO if record.outcome is StepOutcome.OK else EventSeverity.ERROR
                ),
                action=step.action,
                device_id=step.device_id,
                message=f"cleanup: {step.describe()} -> {record.outcome}",
                payload={
                    "run_id": self.run_id,
                    "step": step.index,
                    "phase": str(RecipePhase.FINALLY),
                    "outcome": str(record.outcome),
                    "error": record.error,
                },
            )
        self._finally_ran = True
        failed = [record for record in self._finally if record.outcome is not StepOutcome.OK]
        if failed:
            self._cleanup_note = (
                f"{len(failed)} of {len(self._finally)} cleanup steps failed; "
                "instrumentd's lease expiry and safe-state path remain the backstop"
            )

    async def _close_session(self) -> None:
        if not self._session_opened_here:
            return
        try:
            await self.client.execute("session.stop", {})
        except FieldDeckError as exc:
            self._warnings.append(f"could not close the recipe's session: {exc.message}")

    # -- results -----------------------------------------------------------

    def _finish_state(self) -> None:
        stopped_early = any(record.outcome is not StepOutcome.OK for record in self._steps)
        if self._estop or self._cancelled:
            # An interrupted run is not a test result: nothing about the DUT
            # was proved either way, and saying FAILED would imply it was.
            self._state = RecipeState.ABORTED
        elif self._failed or self._failure is not None or stopped_early:
            self._state = RecipeState.FAILED
        else:
            self._state = RecipeState.PASSED

    def _emit_finished(self) -> None:
        run = self._record()
        self._emit_event(
            EventType.RECIPE_FINISHED,
            severity=(
                EventSeverity.INFO if self._state is RecipeState.PASSED else EventSeverity.ERROR
            ),
            message=run.headline(),
            payload={
                "run_id": self.run_id,
                "state": str(self._state),
                "assertions": {"passed": self._passed, "failed": self._failed},
                "finally_ran": self._finally_ran,
                "cleanup_note": self._cleanup_note,
                "estop": self._estop,
                "reason": self._reason,
            },
        )

    def _record(self) -> RecipeRun:
        ended = Timestamp.now()
        return RecipeRun(
            run_id=self.run_id,
            recipe=self.plan.recipe_name,
            state=self._state,
            dry_run=self.dry_run,
            session_id=self._session_id,
            started_utc_ns=self._started.utc_ns,
            ended_utc_ns=ended.utc_ns,
            duration_s=round((ended.monotonic_ns - self._started.monotonic_ns) / 1e9, 3),
            plan=self.plan.summary(),
            steps=self._steps,
            finally_steps=self._finally,
            assertions_passed=self._passed,
            assertions_failed=self._failed,
            failure=self._failure,
            reason=self._reason,
            estop=self._estop,
            cancelled=self._cancelled,
            finally_ran=self._finally_ran,
            cleanup_note=self._cleanup_note,
            leases_held=list(self._leases),
            warnings=list(self._warnings),
            namespace={name: dict(values) for name, values in self._namespace.items()},
        )

    def _emit_event(
        self,
        event_type: EventType,
        *,
        severity: EventSeverity = EventSeverity.INFO,
        action: str | None = None,
        device_id: str | None = None,
        permission: PermissionLevel | None = None,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._emit(
            new_event(
                event_type,
                source=ClientSource.RECIPE,
                severity=severity,
                session_id=self._session_id,
                device_id=device_id,
                action=action,
                permission=permission,
                request_id=self.run_id,
                message=message,
                payload=payload or {},
            )
        )


def _log_late_cleanup(task: asyncio.Task[None]) -> None:  # pragma: no cover - timing dependent
    if task.cancelled():
        _log.warning("recipe cleanup was cancelled before it finished")
        return
    error = task.exception()
    if error is not None:
        _log.error("recipe cleanup failed after the run ended", extra={"error": str(error)})


def _trim(result: dict[str, Any], *, max_items: int = 20) -> dict[str, Any]:
    """Keep step records readable: the bulk data is in the capture files."""
    out: dict[str, Any] = {}
    for key, value in result.items():
        if isinstance(value, list) and len(value) > max_items:
            out[key] = f"<{len(value)} items>"
        elif isinstance(value, str) and len(value) > 200:
            out[key] = value[:200] + "..."
        else:
            out[key] = value
    return out
