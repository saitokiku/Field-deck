"""The recipe engine's daemon-level actions.

Recipes are exposed as ordinary actions so they go through the same pipeline as
a CAN transmit: validated, authorized, audited, recorded on the timeline.  There
is no side door for automation.

``recipe.run`` is the interesting declaration.  Its permission is the *most
dangerous thing the named recipe will actually do*, resolved per call:
``ActionSpec.permission`` is DESTRUCTIVE, which is the honest worst case for
"run this YAML file" as a capability, and a ``permission_resolver`` narrows it
to the compiled plan's maximum before authorization happens.  So a recipe that
only listens asks for nothing, a recipe that energises a rail asks for POWER —
the same POWER grant its ``psu.set`` step will need — and one that erases a part
asks for DESTRUCTIVE.  An operator reading ``fdctl action list`` sees the
worst case; an operator running one specific recipe is asked for exactly what
that recipe needs, and nothing broader.

Resolving the permission means compiling the file twice: once during
authorization and once inside the handler.  The handler then checks that the
plan it is about to execute still reaches the same maximum, which closes the
window where a file could be edited between the two — an unwatched recipe
directory should not be able to upgrade a PASSIVE authorization into a POWER
run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, model_validator

from fielddeck.common.errors import RecipeError
from fielddeck.common.models import ClientSource, PermissionLevel, StrictModel
from fielddeck.daemon.client import InstrumentClient
from fielddeck.drivers.base import ActionContext, ActionSpec, action, collect_actions
from fielddeck.recipes.compiler import ExecutionPlan, compile_recipe
from fielddeck.recipes.runner import RecipeRunner
from fielddeck.recipes.schema import (
    LoadedRecipe,
    list_recipe_files,
    load_recipe_file,
    load_recipe_text,
    recipe_roots,
    resolve_recipe_reference,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fielddeck.daemon.service import InstrumentDaemon

__all__ = ["RecipeActions", "build_action_specs"]

#: Client-side ceiling for the recipe's own connection.  Individual steps carry
#: their own deadlines; this only stops a wedged socket from hanging forever.
_CLIENT_TIMEOUT_S = 60.0


class RecipeRef(StrictModel):
    """Which recipe: a name or path under the recipe directories, or inline YAML."""

    recipe: str | None = Field(default=None, max_length=1024)
    text: str | None = Field(default=None, max_length=512 * 1024)

    @model_validator(mode="after")
    def _exactly_one(self) -> RecipeRef:
        if (self.recipe is None) == (self.text is None):
            raise ValueError("give either 'recipe' (a name or path) or 'text' (inline YAML)")
        return self


class RecipeRunParams(RecipeRef):
    #: Open a session when none is active, so the run leaves evidence behind.
    open_session: bool = True
    #: Overall wall-clock budget.  Defaults to twice the plan's estimate.
    deadline_s: float | None = Field(default=None, gt=0, le=3600)


class RecipeCancelParams(StrictModel):
    #: Omit to signal every run in flight.
    run_id: str | None = Field(default=None, max_length=64)
    reason: str = Field(default="cancelled by operator", max_length=200)


class RecipeListParams(StrictModel):
    limit: int = Field(default=100, ge=1, le=200)


class RecipeActions:
    """Bound to one daemon; owns the runs currently in flight."""

    def __init__(self, daemon: InstrumentDaemon) -> None:
        self.daemon = daemon
        self._runs: dict[str, RecipeRunner] = {}

    # -- loading and compiling --------------------------------------------

    def _load(self, params: RecipeRef) -> LoadedRecipe:
        if params.text is not None:
            return load_recipe_text(params.text, path=None)
        assert params.recipe is not None
        return load_recipe_file(resolve_recipe_reference(params.recipe))

    def _compile(self, loaded: LoadedRecipe) -> ExecutionPlan:
        return compile_recipe(
            loaded.recipe,
            registry=self.daemon.registry,
            safety=self.daemon.safety,
            source=loaded.source,
        )

    def run_permission(self, params: BaseModel) -> PermissionLevel:
        """Narrow ``recipe.run`` to what this particular recipe reaches.

        Called by the dispatcher before authorization.  A recipe that cannot be
        read or parsed raises here, which is the right moment: refusing to
        authorize something we cannot describe is safer than authorizing the
        worst case and sorting it out later.
        """
        assert isinstance(params, RecipeRef)
        return self._compile(self._load(params)).max_permission

    # -- actions -----------------------------------------------------------

    @action(
        "recipe.list",
        permission=PermissionLevel.PASSIVE,
        params=RecipeListParams,
        state_changing=False,
        description="Recipes available on this unit, and what each one would need.",
        allowed_during_estop=True,
        timeout_s=30.0,
    )
    async def recipe_list(self, ctx: ActionContext, params: RecipeListParams) -> dict[str, Any]:
        """Reads recipe files.  Compiles each one, which touches no hardware."""
        roots = recipe_roots(self.daemon.paths)
        entries: list[dict[str, Any]] = []
        for path in list_recipe_files(roots, limit=params.limit):
            entries.append(self._describe_file(path))
        return {
            "recipes": entries,
            "roots": [str(root) for root in roots],
            "running": [
                {
                    "run_id": run_id,
                    "recipe": runner.plan.recipe_name,
                    "state": str(runner.state),
                }
                for run_id, runner in self._runs.items()
            ],
        }

    def _describe_file(self, path: Path) -> dict[str, Any]:
        try:
            loaded = load_recipe_file(path)
        except RecipeError as exc:
            # A broken recipe is listed with its error rather than hidden: a
            # recipe that silently vanishes from the list is a recipe an
            # operator will look for at the worst possible moment.
            return {"path": str(path), "name": path.stem, "ok": False, "error": exc.message}
        plan = self._compile(loaded)
        return {
            "path": str(path),
            "name": loaded.recipe.name,
            "description": loaded.recipe.description,
            "steps": len(plan.steps),
            "max_permission": str(plan.max_permission),
            "requires": [device.reference for device in plan.devices],
            "missing_devices": plan.missing_devices,
            "ok": plan.ok,
            "problems": [problem.message for problem in plan.errors[:3]],
        }

    @action(
        "recipe.validate",
        permission=PermissionLevel.PASSIVE,
        params=RecipeRef,
        state_changing=False,
        description="Compile a recipe and report devices, permissions and limit problems.",
        allowed_during_estop=True,
        timeout_s=30.0,
    )
    async def recipe_validate(self, ctx: ActionContext, params: RecipeRef) -> dict[str, Any]:
        """Compilation only: no client connection, no steps, no hardware."""
        plan = self._compile(self._load(params))
        return {
            "ok": plan.ok,
            "summary": plan.summary(),
            "plan": plan.model_dump(mode="json"),
        }

    @action(
        "recipe.dry_run",
        permission=PermissionLevel.PASSIVE,
        params=RecipeRunParams,
        state_changing=False,
        description="Compile and preflight a recipe without running any step.",
        allowed_during_estop=True,
        timeout_s=60.0,
    )
    async def recipe_dry_run(self, ctx: ActionContext, params: RecipeRunParams) -> dict[str, Any]:
        """Answers "would this run right now?", including the authorization.

        Unlike a real run, a missing grant is reported rather than raised: the
        question a dry run answers is what you would need, so refusing to
        answer it because you do not have it yet would be perverse.
        """
        plan = self._compile(self._load(params))
        async with await self._connect() as client:
            runner = RecipeRunner(
                client,
                plan,
                emit=ctx.emit,
                dry_run=True,
                open_session=False,
                deadline_s=params.deadline_s,
            )
            run = await runner.run()
        return {
            "ok": plan.ok and run.would_start,
            "would_start": run.would_start,
            "run": run.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json"),
        }

    @action(
        "recipe.run",
        permission=PermissionLevel.DESTRUCTIVE,
        params=RecipeRunParams,
        state_changing=True,
        description="Compile and execute a recipe. Requires whatever the recipe itself needs.",
        cancelable=True,
        timeout_s=3600.0,
        safe_state_note=(
            "The recipe's finally steps run on any outcome, and every output it took "
            "is held by a lease that lapses to safe state if the run dies."
        ),
        # Replaced in build_action_specs with a resolver that can see the live
        # device registry.  Declared here so the field is never absent.
        permission_resolver=None,
    )
    async def recipe_run(self, ctx: ActionContext, params: RecipeRunParams) -> dict[str, Any]:
        loaded = self._load(params)
        plan = self._compile(loaded)
        plan.raise_if_unrunnable()

        # The plan is compiled twice: once to resolve the permission the
        # dispatcher authorized, and again here.  If a recipe file changed in
        # between, the two disagree and nothing runs -- an editable file must
        # not be able to spend an authorization that was granted for a
        # different version of it.
        if plan.max_permission is not ctx.granted_permission:
            raise RecipeError(
                f"{plan.recipe_name} now needs {plan.max_permission} but "
                f"{ctx.granted_permission} was authorized; the recipe changed while it "
                "was being started. Run it again to authorize the new version.",
                details={
                    "recipe": plan.recipe_name,
                    "authorized": str(ctx.granted_permission),
                    "required": str(plan.max_permission),
                    "sha256": loaded.source.sha256,
                },
                preserved="no step was run and nothing was energised",
            )

        async with await self._connect() as client:
            runner = RecipeRunner(
                client,
                plan,
                emit=ctx.emit,
                open_session=params.open_session,
                deadline_s=params.deadline_s,
            )
            self._runs[runner.run_id] = runner
            # A cancel or an ESTOP reaches the dispatcher first and sets this
            # event; the runner turns it into an orderly stop with cleanup
            # rather than a dropped connection.
            watcher = asyncio.ensure_future(self._forward_cancel(ctx, runner))
            try:
                run = await runner.run()
            finally:
                watcher.cancel()
                self._runs.pop(runner.run_id, None)
        return run.model_dump(mode="json")

    @action(
        "recipe.cancel",
        permission=PermissionLevel.PASSIVE,
        params=RecipeCancelParams,
        state_changing=False,
        description="Ask a running recipe to stop; its finally steps still run.",
        allowed_during_estop=True,
    )
    async def recipe_cancel(self, ctx: ActionContext, params: RecipeCancelParams) -> dict[str, Any]:
        """PASSIVE on purpose: stopping is never the dangerous direction.

        The run is asked to stop rather than killed, so the cleanup phase gets
        to turn outputs off instead of leaving them to the lease timeout.
        """
        targets = (
            [self._runs[params.run_id]]
            if params.run_id in self._runs
            else list(self._runs.values())
        )
        if params.run_id is not None and params.run_id not in self._runs:
            targets = []
        for runner in targets:
            runner.cancel(f"{params.reason} (via {ctx.source})")
        return {
            "cancelled": [runner.run_id for runner in targets],
            "running": [run_id for run_id in self._runs],
        }

    # -- plumbing ----------------------------------------------------------

    async def _connect(self) -> InstrumentClient:
        """A client connection of the recipe's own, as ClientSource.RECIPE.

        Not a shortcut into the dispatcher: the runner is a client like any
        other, and a client is what the safety model understands.  The
        connection matters as well as the identity -- output leases are tied to
        it, so if this run dies the daemon sees the socket close and drives the
        devices safe without waiting for anything to time out.
        """
        socket_path = getattr(self.daemon, "_socket_path", None) or self.daemon.paths.socket
        client = InstrumentClient(
            socket_path, source=ClientSource.RECIPE, timeout_s=_CLIENT_TIMEOUT_S
        )
        try:
            return await client.connect()
        except Exception as exc:  # noqa: BLE001 - reported as a recipe failure, not a daemon crash
            raise RecipeError(
                f"the recipe engine could not reach the control socket at {socket_path}: {exc}",
                details={"socket": str(socket_path)},
                preserved="nothing was run and nothing was energised",
            ) from exc

    async def _forward_cancel(self, ctx: ActionContext, runner: RecipeRunner) -> None:
        """Turn a dispatcher-level cancel into a cooperative recipe stop."""
        await ctx.cancel.wait()
        runner.cancel("the recipe action was cancelled")


def build_action_specs(daemon: InstrumentDaemon) -> dict[str, ActionSpec]:
    actions = RecipeActions(daemon)
    specs = collect_actions(actions)
    # The resolver needs the live registry and safety config, which exist only
    # once there is a daemon, so it is bound here rather than in the decorator.
    specs["recipe.run"].permission_resolver = actions.run_permission
    return specs
