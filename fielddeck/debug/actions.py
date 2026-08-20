"""Debug and programming actions.

Registered as daemon-level actions because a probe is selected by parameters
rather than being a long-lived device: the same ST-Link programs a different
target every ten minutes on a bench.

Note the split between planning and doing.  ``flash.plan`` is PASSIVE and
returns the literal command that would run, so the risky step can be reviewed
before anyone authorizes it.  ``flash.program`` needs FLASH.  ``flash.erase``
needs DESTRUCTIVE, which is a separate class precisely so that being armed to
update firmware does not also authorize wiping it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import Field

from fielddeck.common.errors import InvalidRequest
from fielddeck.common.models import PermissionLevel, StrictModel
from fielddeck.debug.firmware import inspect_firmware
from fielddeck.debug.flash import build_plan, firmware_roots, run_plan
from fielddeck.debug.probes import known_probes, programming_tools
from fielddeck.drivers.base import ActionContext, ActionSpec, NoParams, action, collect_actions

if TYPE_CHECKING:  # pragma: no cover
    from fielddeck.daemon.service import InstrumentDaemon

__all__ = ["build_action_specs"]


class FirmwareInspectParams(StrictModel):
    path: str


class PlanParams(StrictModel):
    tool: str = Field(description="openocd | pyocd | esptool | dfu-util")
    operation: str = Field(default="info")
    target: str = ""
    interface: str = "stlink"
    port: str = ""
    firmware_path: str | None = None
    address: str | None = None
    baud: int = Field(default=460800, ge=9600, le=4_000_000)
    alt: str = "0"
    device: str | None = None


class ExecuteParams(PlanParams):
    timeout_s: float = Field(default=300.0, gt=0, le=1800)
    #: Erase is irreversible, so the caller has to name what it is destroying.
    confirm: str | None = None


class DebugActions:
    def __init__(self, daemon: InstrumentDaemon) -> None:
        self.daemon = daemon

    def _roots(self, ctx: ActionContext) -> list[Path]:
        roots = list(firmware_roots())
        if ctx.recorder is not None:
            roots.append(ctx.recorder.root)
        return roots

    # -- inspection --------------------------------------------------------

    @action(
        "firmware.inspect",
        permission=PermissionLevel.PASSIVE,
        params=FirmwareInspectParams,
        state_changing=False,
        description="Identify a firmware file: format, architecture, extent, hash.",
        allowed_during_estop=True,
        timeout_s=60.0,
    )
    async def firmware_inspect(
        self, ctx: ActionContext, params: FirmwareInspectParams
    ) -> dict[str, Any]:
        """Offline file analysis. No probe is opened and no target is powered."""
        roots = self._roots(ctx)
        requested = params.path

        def _inspect() -> dict[str, Any]:
            # Hashing and scanning a multi-megabyte image is genuinely slow, so
            # it runs off the event loop; a firmware inspection must not stall a
            # concurrent CAN capture.
            candidate = Path(requested).expanduser().resolve()
            resolved_roots = [root.resolve() for root in roots]
            if not resolved_roots or not any(
                candidate.is_relative_to(root) for root in resolved_roots
            ):
                raise InvalidRequest(
                    f"{requested} is outside the permitted firmware directories",
                    details={"allowed": [str(root) for root in resolved_roots]},
                    preserved="no file was read",
                )
            return dict(inspect_firmware(candidate))

        return await asyncio.to_thread(_inspect)

    @action(
        "debug.probes",
        permission=PermissionLevel.PASSIVE,
        params=NoParams,
        state_changing=False,
        description="Debug probes visible on USB, classified by VID/PID.",
        allowed_during_estop=True,
    )
    async def debug_probes(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
        probes = known_probes()
        return {
            "probes": probes,
            "count": len(probes),
            "note": "USB enumeration only; no target was contacted",
        }

    @action(
        "debug.tools",
        permission=PermissionLevel.PASSIVE,
        params=NoParams,
        state_changing=False,
        description="Which programming tools are installed, and their versions.",
        allowed_during_estop=True,
        timeout_s=30.0,
    )
    async def debug_tools(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
        return {"tools": await programming_tools()}

    # -- planning ----------------------------------------------------------

    @action(
        "flash.plan",
        permission=PermissionLevel.PASSIVE,
        params=PlanParams,
        state_changing=False,
        description="Show the exact command a flash operation would run, without running it.",
        allowed_during_estop=True,
        timeout_s=60.0,
    )
    async def flash_plan(self, ctx: ActionContext, params: PlanParams) -> dict[str, Any]:
        """Review before you authorize. This is the whole point of the module."""
        plan, info = await asyncio.to_thread(
            build_plan, **params.model_dump(), extra_roots=self._roots(ctx)
        )
        return {
            "plan": plan.as_dict(),
            "firmware": info,
            "required_permission": str(plan.permission),
            "note": "nothing was executed; run flash.program / flash.erase to act on this plan",
        }

    # -- execution ---------------------------------------------------------

    @action(
        "debug.target_info",
        permission=PermissionLevel.QUERY,
        params=ExecuteParams,
        state_changing=False,
        description="Read target identity through the probe.",
        timeout_s=120.0,
    )
    async def debug_target_info(self, ctx: ActionContext, params: ExecuteParams) -> dict[str, Any]:
        return await self._execute(ctx, params, expect="info")

    @action(
        "debug.reset",
        permission=PermissionLevel.CONTROL,
        params=ExecuteParams,
        state_changing=True,
        description="Reset the target through the probe.",
        timeout_s=120.0,
        safe_state_note="A reset leaves the target running its existing firmware.",
    )
    async def debug_reset(self, ctx: ActionContext, params: ExecuteParams) -> dict[str, Any]:
        return await self._execute(ctx, params, expect="reset")

    @action(
        "flash.verify",
        permission=PermissionLevel.QUERY,
        params=ExecuteParams,
        state_changing=False,
        description="Compare target flash against an image without writing.",
        timeout_s=600.0,
    )
    async def flash_verify(self, ctx: ActionContext, params: ExecuteParams) -> dict[str, Any]:
        return await self._execute(ctx, params, expect="verify")

    @action(
        "flash.program",
        permission=PermissionLevel.FLASH,
        params=ExecuteParams,
        state_changing=True,
        description="Write a firmware image to the target.",
        timeout_s=900.0,
        safe_state_note=(
            "An interrupted program leaves the target partially written and "
            "usually unbootable until reprogrammed."
        ),
    )
    async def flash_program(self, ctx: ActionContext, params: ExecuteParams) -> dict[str, Any]:
        return await self._execute(ctx, params, expect="program")

    @action(
        "flash.erase",
        permission=PermissionLevel.DESTRUCTIVE,
        params=ExecuteParams,
        state_changing=True,
        description="Mass-erase the target's non-volatile memory. Irreversible.",
        timeout_s=900.0,
        safe_state_note="There is no safe state to return to; erased flash is gone.",
    )
    async def flash_erase(self, ctx: ActionContext, params: ExecuteParams) -> dict[str, Any]:
        """DESTRUCTIVE, and separate from FLASH on purpose.

        Being armed to update firmware must not also authorize destroying it,
        and the caller has to name the target in ``confirm`` so an erase cannot
        be issued by replaying a program request with one word changed.
        """
        expected = params.target or params.port or params.device or "target"
        if params.confirm != expected:
            raise InvalidRequest(
                f"flash.erase is irreversible; set confirm={expected!r} to proceed",
                details={"expected_confirm": expected, "received": params.confirm},
                preserved="the target was not touched",
            )
        return await self._execute(ctx, params, expect="erase")

    # -- shared ------------------------------------------------------------

    async def _execute(
        self, ctx: ActionContext, params: ExecuteParams, *, expect: str
    ) -> dict[str, Any]:
        plan_args = params.model_dump(exclude={"timeout_s", "confirm"})
        plan_args["operation"] = expect
        plan, info = await asyncio.to_thread(build_plan, **plan_args, extra_roots=self._roots(ctx))

        record = await run_plan(
            plan,
            timeout_s=min(params.timeout_s, (ctx.remaining_s() or params.timeout_s)),
            allowed_roots=self._roots(ctx),
        )
        if ctx.recorder is not None:
            # The audit record is the answer to "what firmware is on this unit,
            # and who put it there?" long after the bench is packed away.
            path = ctx.recorder.capture_path("firmware", f"{expect}-{plan.tool}", ".json")
            payload = json.dumps({**record, "firmware_info": info}, indent=2)
            await asyncio.to_thread(path.write_text, payload, "utf-8")
            artifact = ctx.recorder.add_artifact(
                path,
                kind="firmware",
                media_type="application/json",
                raw=False,
                producer=plan.tool,
                producer_version=record.get("tool_version"),
                producer_config={"command": record["command"]},
                metadata={"operation": expect, "firmware_sha256": plan.firmware_sha256},
            )
            record["artifact"] = artifact.model_dump(mode="json")
        return record


def build_action_specs(daemon: InstrumentDaemon) -> dict[str, ActionSpec]:
    return collect_actions(DebugActions(daemon))
