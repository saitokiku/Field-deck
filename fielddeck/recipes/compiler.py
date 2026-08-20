"""Turn a parsed recipe into a plan, or refuse it.

The whole point of this module is a promise: **a recipe that cannot run
correctly must fail before its first action, not halfway through with the DUT
energised**.  Failing at step seven of nine is how a supply is left at 24 V on
a bench nobody is standing at.

So compilation answers, for the whole file at once and without touching any
hardware:

* which devices it needs, and whether each one is present right now
* which actions it will call, and whether they exist on those devices
* what each call's parameters validate to, and the *effective* permission for
  those exact parameters — ``psu.output(enabled=false)`` is PASSIVE, and a plan
  that pretended otherwise would ask an operator to arm POWER to turn a rail off
* the set of permission classes required and the single most dangerous one
* every statically detectable limit violation, checked against the stricter of
  the deployment's limits and the recipe's own declared envelope
* every assertion expression, parsed and vetted against the allowlist

Problems are collected rather than raised one at a time: an operator fixing a
recipe wants the whole list, not a game of whack-a-mole with a live bench.
"""

from __future__ import annotations

import difflib
from enum import StrEnum
from typing import Any

from pydantic import Field, ValidationError

from fielddeck.common.errors import FieldDeckError, RecipeError, SafetyLimitExceeded
from fielddeck.common.models import PermissionLevel, SafetyLimit, StrictModel
from fielddeck.common.timebase import Timestamp
from fielddeck.daemon.registry import DeviceRegistry
from fielddeck.drivers.base import ActionSpec
from fielddeck.recipes.assertions import compile_expression
from fielddeck.recipes.schema import (
    ActionStep,
    AssertStep,
    MarkStep,
    NoteStep,
    Recipe,
    RecipePhase,
    RecipeSource,
    RepeatStep,
    Step,
    StepKind,
    WaitStep,
)
from fielddeck.safety.limits import LimitEnforcer
from fielddeck.safety.manager import SafetyManager

__all__ = [
    "ExecutionPlan",
    "PlanProblem",
    "PlannedAction",
    "PlannedDevice",
    "PlannedStep",
    "ProblemSeverity",
    "compile_recipe",
]

#: Expanded steps, after ``repeat``.  A plan larger than this is not a test.
MAX_PLAN_STEPS = 2000
#: Assumed cost of an action whose duration is not declared, used only for the
#: estimate shown to the operator.  Never used as a timeout.
_DEFAULT_STEP_S = 0.2
#: Parameters whose value is a duration in seconds, for the same estimate.
_DURATION_PARAMS = ("duration_s", "seconds", "timeout_s")


class ProblemSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class PlanProblem(StrictModel):
    """One reason a plan will not run, or one thing worth knowing before it does."""

    severity: ProblemSeverity
    code: str
    message: str
    phase: RecipePhase | None = None
    #: Index into the compiled plan, not into the YAML list.
    step: int | None = None
    source_index: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    def describe(self) -> str:
        where = f"{self.phase} step {self.source_index}" if self.phase is not None else "recipe"
        return f"[{self.severity}] {where}: {self.message}"


class PlannedDevice(StrictModel):
    reference: str
    present: bool
    device_id: str | None = None
    display_name: str | None = None
    simulated: bool = False
    #: True when the id was derived from a name like ``/dev/ttyUSB0``, which is
    #: not identity — worth saying before a recipe binds to it.
    stable_id: bool = True
    note: str | None = None


class PlannedAction(StrictModel):
    """One action name the recipe uses, summarised across its calls."""

    action: str
    calls: int
    permission: PermissionLevel
    available: bool
    state_changing: bool
    device_ids: list[str] = Field(default_factory=list)
    problem: str | None = None


class PlannedStep(StrictModel):
    """Exactly one thing that will happen, in the order it will happen."""

    index: int
    phase: RecipePhase
    kind: StepKind
    source_index: int
    #: Set for steps produced by ``repeat``: 1-based iteration number.
    iteration: int | None = None
    action: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    device: str | None = None
    device_id: str | None = None
    #: The permission these exact parameters resolve to.
    permission: PermissionLevel = PermissionLevel.PASSIVE
    #: The action's declared worst case, which may be higher.
    declared_permission: PermissionLevel | None = None
    state_changing: bool = False
    allowed_during_estop: bool = False
    requires_lease: bool = False
    timeout_s: float | None = None
    store: str | None = None
    seconds: float | None = None
    expression: str | None = None
    message: str | None = None
    available: bool = True
    estimated_s: float = _DEFAULT_STEP_S

    def describe(self) -> str:
        if self.kind is StepKind.WAIT:
            return f"wait {self.seconds:g}s"
        if self.kind is StepKind.ASSERT:
            return f"assert {self.expression}"
        target = f" on {self.device_id or self.device}" if self.device else ""
        return f"{self.action}{target}"


class ExecutionPlan(StrictModel):
    """Everything that will happen, decided before anything does."""

    recipe_name: str
    version: int
    description: str | None = None
    source: dict[str, Any] = Field(default_factory=dict)
    compiled_utc_ns: int = 0
    devices: list[PlannedDevice] = Field(default_factory=list)
    actions: list[PlannedAction] = Field(default_factory=list)
    steps: list[PlannedStep] = Field(default_factory=list)
    finally_steps: list[PlannedStep] = Field(default_factory=list)
    #: Permission classes an operator must have armed, PASSIVE excluded.
    permissions_required: list[PermissionLevel] = Field(default_factory=list)
    #: The most dangerous thing this recipe will do.
    max_permission: PermissionLevel = PermissionLevel.PASSIVE
    state_changing_steps: int = 0
    #: The limits that will actually be enforced, after tightening.
    effective_limits: dict[str, dict[str, Any]] = Field(default_factory=dict)
    declared_limits: dict[str, dict[str, float | None]] = Field(default_factory=dict)
    problems: list[PlanProblem] = Field(default_factory=list)
    estimated_duration_s: float = 0.0

    @property
    def errors(self) -> list[PlanProblem]:
        return [p for p in self.problems if p.severity is ProblemSeverity.ERROR]

    @property
    def warnings(self) -> list[PlanProblem]:
        return [p for p in self.problems if p.severity is ProblemSeverity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def all_steps(self) -> list[PlannedStep]:
        return [*self.steps, *self.finally_steps]

    @property
    def missing_devices(self) -> list[str]:
        return [device.reference for device in self.devices if not device.present]

    def raise_if_unrunnable(self) -> None:
        """The gate.  Nothing executes a plan without calling this first."""
        errors = self.errors
        if not errors:
            return
        summary = "; ".join(problem.describe() for problem in errors[:5])
        if len(errors) > 5:
            summary += f"; and {len(errors) - 5} more"
        raise RecipeError(
            f"{self.recipe_name} cannot run: {summary}",
            details={
                "recipe": self.recipe_name,
                "problems": [problem.model_dump(mode="json") for problem in self.problems],
                "missing_devices": self.missing_devices,
            },
            preserved="no action was requested and no device was touched",
        )

    def summary(self) -> dict[str, Any]:
        """The short form for events, the HMI banner and ``--json`` output."""
        return {
            "recipe": self.recipe_name,
            "ok": self.ok,
            "steps": len(self.steps),
            "finally_steps": len(self.finally_steps),
            "max_permission": str(self.max_permission),
            "permissions_required": [str(p) for p in self.permissions_required],
            "devices": [
                {"reference": d.reference, "device_id": d.device_id, "present": d.present}
                for d in self.devices
            ],
            "missing_devices": self.missing_devices,
            "state_changing_steps": self.state_changing_steps,
            "estimated_duration_s": round(self.estimated_duration_s, 1),
            "errors": [problem.message for problem in self.errors],
            "warnings": [problem.message for problem in self.warnings],
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


class _Compiler:
    """Walks a recipe once, accumulating the plan and every problem found."""

    def __init__(self, recipe: Recipe, registry: DeviceRegistry, safety: SafetyManager) -> None:
        self.recipe = recipe
        self.registry = registry
        self.safety = safety
        self.problems: list[PlanProblem] = []
        self.steps: dict[RecipePhase, list[PlannedStep]] = {
            RecipePhase.STEPS: [],
            RecipePhase.FINALLY: [],
        }
        self.action_calls: dict[str, PlannedAction] = {}
        #: Namespaces an assertion may legitimately read at that point.
        self.produced: set[str] = set()
        self.declared_references = {
            requirement.reference for requirement in recipe.requires.devices
        }
        self.limits = self._effective_limits()
        self.enforcer = self._enforcer()
        self._index = 0
        self._truncated = False

    # -- limits ------------------------------------------------------------

    def _effective_limits(self) -> dict[str, SafetyLimit]:
        """Deployment limits tightened by the recipe's declared envelope.

        A recipe may only narrow.  One that declares more headroom than
        ``safety.yaml`` allows was written for a different bench, and is
        refused rather than clamped: clamping would silently run a different
        test from the one the author described.
        """
        limits = dict(self.safety.config.global_limits)
        for quantity, declared in self.recipe.limits.as_limits().items():
            existing = limits.get(quantity)
            if existing is None:
                limits[quantity] = declared
                continue
            if (
                declared.maximum is not None
                and existing.maximum is not None
                and declared.maximum > existing.maximum
            ):
                self.problem(
                    ProblemSeverity.ERROR,
                    "recipe-limit-too-wide",
                    f"the recipe allows {quantity} up to {declared.maximum:g} but this "
                    f"unit's limit is {existing.maximum:g}; it was written for a "
                    "different bench",
                    details={
                        "quantity": quantity,
                        "recipe_maximum": declared.maximum,
                        "unit_maximum": existing.maximum,
                    },
                )
            limits[quantity] = existing.intersect(declared)
        return limits

    def _enforcer(self) -> LimitEnforcer:
        """A limit enforcer over the tightened set, reusing the daemon's code.

        Static checking uses exactly the class the dispatcher uses at runtime,
        so a value the compiler accepts is one the dispatcher accepts, and the
        two can never drift into disagreeing.
        """
        config = self.safety.config.model_copy(update={"global_limits": self.limits})
        return LimitEnforcer(config)

    # -- problems ----------------------------------------------------------

    def problem(
        self,
        severity: ProblemSeverity,
        code: str,
        message: str,
        *,
        phase: RecipePhase | None = None,
        step: PlannedStep | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.problems.append(
            PlanProblem(
                severity=severity,
                code=code,
                message=message,
                phase=phase or (step.phase if step else None),
                step=step.index if step else None,
                source_index=step.source_index if step else None,
                details=details or {},
            )
        )

    # -- devices -----------------------------------------------------------

    def plan_devices(self) -> list[PlannedDevice]:
        planned: list[PlannedDevice] = []
        for requirement in self.recipe.requires.devices:
            reference = requirement.reference
            driver = self.registry.try_resolve(reference)
            if driver is None:
                planned.append(
                    PlannedDevice(reference=reference, present=False, note=requirement.note)
                )
                self.problem(
                    ProblemSeverity.ERROR,
                    "device-missing",
                    f"{reference} is required but no such device is present. "
                    "Check the connection, then run: fdctl discover",
                    details={"reference": reference, "known": sorted(self.registry.aliases)},
                )
                continue
            descriptor = driver.describe()
            planned.append(
                PlannedDevice(
                    reference=reference,
                    present=True,
                    device_id=descriptor.id,
                    display_name=descriptor.display_name,
                    simulated=descriptor.simulated,
                    stable_id=descriptor.stable_id,
                    note=requirement.note,
                )
            )
            if not descriptor.stable_id:
                self.problem(
                    ProblemSeverity.WARNING,
                    "unstable-device-id",
                    f"{reference} resolves to {descriptor.id}, whose id is not stable "
                    "across re-plugging; confirm it is the adapter you mean",
                    details={"device_id": descriptor.id},
                )
        return planned

    # -- steps -------------------------------------------------------------

    def plan_phase(self, phase: RecipePhase) -> None:
        for step in self.recipe.phase(phase):
            self._plan_step(step, phase=phase, iteration=None)

    def _plan_step(self, step: Step, *, phase: RecipePhase, iteration: int | None) -> None:
        if len(self.steps[RecipePhase.STEPS]) + len(self.steps[RecipePhase.FINALLY]) >= (
            MAX_PLAN_STEPS
        ):
            if not self._truncated:
                self._truncated = True
                self.problem(
                    ProblemSeverity.ERROR,
                    "plan-too-large",
                    f"the recipe expands to more than {MAX_PLAN_STEPS} steps; reduce the "
                    "repeat counts",
                    phase=phase,
                )
            return

        if isinstance(step, RepeatStep):
            for number in range(1, step.count + 1):
                for inner in step.steps:
                    self._plan_step(inner, phase=phase, iteration=number)
            return

        planned = self._make_planned(step, phase=phase, iteration=iteration)
        self.steps[phase].append(planned)
        self._index += 1

        if planned.kind is StepKind.ASSERT:
            self._check_assertion(planned)
        elif planned.action is not None:
            self._check_action(planned)

    def _make_planned(
        self, step: Step, *, phase: RecipePhase, iteration: int | None
    ) -> PlannedStep:
        common: dict[str, Any] = {
            "index": self._index,
            "phase": phase,
            "source_index": step.source_index,
            "iteration": iteration,
        }
        if isinstance(step, WaitStep):
            return PlannedStep(
                kind=StepKind.WAIT, seconds=step.seconds, estimated_s=step.seconds, **common
            )
        if isinstance(step, AssertStep):
            return PlannedStep(
                kind=StepKind.ASSERT,
                expression=step.expression,
                message=step.message,
                estimated_s=0.0,
                **common,
            )
        if isinstance(step, MarkStep):
            # Lowered to the real action so a recipe mark is the same timeline
            # record an operator's ``fdctl session mark`` produces.
            params: dict[str, Any] = {"label": step.label}
            if step.note is not None:
                params["note"] = step.note
            return PlannedStep(kind=StepKind.MARK, action="session.mark", params=params, **common)
        if isinstance(step, NoteStep):
            return PlannedStep(
                kind=StepKind.NOTE, action="session.note", params={"text": step.text}, **common
            )
        assert isinstance(step, ActionStep)  # RepeatStep is expanded before this point
        return PlannedStep(
            kind=StepKind.ACTION,
            action=step.action,
            params=dict(step.params),
            device=step.device,
            store=step.store,
            timeout_s=step.timeout_s,
            estimated_s=_estimate(step.params),
            **common,
        )

    # -- per-step checks ---------------------------------------------------

    def _check_assertion(self, planned: PlannedStep) -> None:
        assert planned.expression is not None
        try:
            compiled = compile_expression(planned.expression)
        except RecipeError as exc:
            planned.available = False
            self.problem(
                ProblemSeverity.ERROR,
                "bad-expression",
                exc.message,
                step=planned,
                details=exc.details,
            )
            return
        unknown = [name for name in compiled.names if name not in self.produced]
        if unknown:
            planned.available = False
            self.problem(
                ProblemSeverity.ERROR,
                "unknown-namespace",
                f"the assertion reads {', '.join(unknown)}, which no earlier step "
                f"produces. Results so far: {', '.join(sorted(self.produced)) or 'none'}",
                step=planned,
                details={"unknown": unknown, "available": sorted(self.produced)},
            )

    def _check_action(self, planned: PlannedStep) -> None:
        assert planned.action is not None
        try:
            spec, driver = self.registry.lookup(planned.action, planned.params)
        except FieldDeckError as exc:
            planned.available = False
            self._note_action(planned, None, problem=exc.message)
            self.problem(
                ProblemSeverity.ERROR,
                "action-unavailable",
                exc.message,
                step=planned,
                details=exc.details,
            )
            return

        planned.device_id = driver.device_id if driver is not None else None
        planned.declared_permission = spec.permission
        planned.state_changing = spec.state_changing
        planned.allowed_during_estop = spec.allowed_during_estop
        planned.requires_lease = spec.requires_lease
        if planned.timeout_s is None:
            planned.timeout_s = spec.timeout_s

        if planned.device and planned.device not in self.declared_references:
            self.problem(
                ProblemSeverity.WARNING,
                "undeclared-device",
                f"step uses {planned.device}, which is not listed under requires.devices; "
                "declaring it makes a missing instrument a compile error instead of a "
                "surprise",
                step=planned,
                details={"device": planned.device},
            )

        params = self._validate_params(planned, spec)
        if params is None:
            return

        planned.permission = spec.effective_permission(params)
        values = params.model_dump()
        self._check_limits(planned, spec, values)
        self._check_policy(planned)
        self._note_action(planned, spec)
        self.produced.add(planned.store or planned.action.split(".", 1)[0])

    def _validate_params(self, planned: PlannedStep, spec: ActionSpec) -> Any:
        try:
            return spec.params_model.model_validate(planned.params)
        except ValidationError as exc:
            planned.available = False
            fields = set(spec.params_model.model_fields)
            problems = []
            for error in exc.errors():
                field = ".".join(str(part) for part in error["loc"]) or "(parameters)"
                hint = ""
                if error["type"] == "extra_forbidden":
                    close = difflib.get_close_matches(field, fields, n=1)
                    hint = f"; did you mean {close[0]!r}?" if close else ""
                problems.append({"field": field, "problem": error["msg"] + hint})
            first = problems[0]
            self.problem(
                ProblemSeverity.ERROR,
                "bad-parameters",
                f"{planned.action}: {first['field']}: {first['problem']}",
                step=planned,
                details={
                    "action": planned.action,
                    "errors": problems,
                    "accepts": sorted(fields),
                },
            )
            return None

    def _check_limits(self, planned: PlannedStep, spec: ActionSpec, values: dict[str, Any]) -> None:
        """The check that makes the promise real: every literal, before the run."""
        try:
            self.enforcer.check_params(values, spec.limit_checks, device_id=planned.device_id)
            self.enforcer.check_derived(
                values, spec.derived_limit_checks, device_id=planned.device_id
            )
        except SafetyLimitExceeded as exc:
            planned.available = False
            self.problem(
                ProblemSeverity.ERROR,
                "limit-exceeded",
                f"{planned.action}: {exc.message}",
                step=planned,
                details=exc.details,
            )

    def _check_policy(self, planned: PlannedStep) -> None:
        if planned.permission in self.safety.config.denied_permissions:
            planned.available = False
            self.problem(
                ProblemSeverity.ERROR,
                "permission-denied-by-policy",
                f"{planned.action} needs {planned.permission}, which this unit's "
                "safety policy disables outright",
                step=planned,
                details={"permission": str(planned.permission)},
            )

    def _note_action(
        self, planned: PlannedStep, spec: ActionSpec | None, *, problem: str | None = None
    ) -> None:
        assert planned.action is not None
        entry = self.action_calls.get(planned.action)
        if entry is None:
            entry = PlannedAction(
                action=planned.action,
                calls=0,
                permission=spec.permission if spec else PermissionLevel.PASSIVE,
                available=spec is not None,
                state_changing=bool(spec and spec.state_changing),
                problem=problem,
            )
            self.action_calls[planned.action] = entry
        entry.calls += 1
        if planned.device_id and planned.device_id not in entry.device_ids:
            entry.device_ids.append(planned.device_id)

    # -- cross-step checks -------------------------------------------------

    def check_lease_coverage(self) -> None:
        """Warn when an output would drop mid-run because its lease is shorter.

        A sustained output is held by a dead-man lease.  If the recipe still has
        45 s of work after enabling one with a 30 s lease, the daemon will drive
        the output safe partway through and the run will fail in a way that
        looks like a hardware fault.  Better to say so now.
        """
        ordered = self.steps[RecipePhase.STEPS]
        for position, planned in enumerate(ordered):
            if not planned.requires_lease or planned.permission is PermissionLevel.PASSIVE:
                continue
            ttl = planned.params.get("lease_ttl_s")
            ttl_s = (
                float(ttl)
                if isinstance(ttl, (int, float))
                else (self.safety.config.default_lease_ttl_s)
            )
            remaining = sum(step.estimated_s for step in ordered[position + 1 :])
            if remaining > ttl_s:
                self.problem(
                    ProblemSeverity.WARNING,
                    "lease-too-short",
                    f"{planned.action} holds a {ttl_s:g}s output lease but about "
                    f"{remaining:.1f}s of steps follow it; the output will drop to safe "
                    "state partway through unless lease_ttl_s is raised",
                    step=planned,
                    details={"lease_ttl_s": ttl_s, "remaining_s": round(remaining, 1)},
                )

    # -- assembly ----------------------------------------------------------

    def build(self, source: RecipeSource) -> ExecutionPlan:
        devices = self.plan_devices()
        self.plan_phase(RecipePhase.STEPS)
        self.plan_phase(RecipePhase.FINALLY)
        self.check_lease_coverage()

        all_steps = [*self.steps[RecipePhase.STEPS], *self.steps[RecipePhase.FINALLY]]
        required = sorted(
            {step.permission for step in all_steps if step.permission.requires_grant},
            key=lambda permission: permission.rank,
        )
        maximum = required[-1] if required else PermissionLevel.PASSIVE
        return ExecutionPlan(
            recipe_name=self.recipe.name,
            version=self.recipe.version,
            description=self.recipe.description,
            source=source.describe(),
            compiled_utc_ns=Timestamp.now().utc_ns,
            devices=devices,
            actions=sorted(self.action_calls.values(), key=lambda entry: entry.action),
            steps=self.steps[RecipePhase.STEPS],
            finally_steps=self.steps[RecipePhase.FINALLY],
            permissions_required=required,
            max_permission=maximum,
            state_changing_steps=sum(1 for step in all_steps if step.state_changing),
            effective_limits={
                quantity: {
                    "minimum": limit.minimum,
                    "maximum": limit.maximum,
                    "unit": limit.unit,
                }
                for quantity, limit in sorted(self.limits.items())
            },
            declared_limits=self.recipe.limits.describe(),
            problems=self.problems,
            estimated_duration_s=sum(step.estimated_s for step in all_steps),
        )


def _estimate(params: dict[str, Any]) -> float:
    """A lower bound on how long a step takes, for the operator's benefit."""
    for name in _DURATION_PARAMS:
        value = params.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return _DEFAULT_STEP_S


def compile_recipe(
    recipe: Recipe,
    *,
    registry: DeviceRegistry,
    safety: SafetyManager,
    source: RecipeSource,
) -> ExecutionPlan:
    """Compile one recipe against the live device and safety state.

    Never raises for a bad recipe: the problems are in the returned plan so a
    client can show all of them at once.  Call
    :meth:`ExecutionPlan.raise_if_unrunnable` before executing.
    """
    return _Compiler(recipe, registry, safety).build(source)
