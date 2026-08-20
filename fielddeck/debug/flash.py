"""Programming and debug tool wrappers.

Flashing is the operation with the worst failure mode in the whole product:
get it wrong and a customer's device is a brick on a bench three hours' drive
away.  So this module is built around one idea — **the plan is visible before
it runs**.

``flash.plan`` builds the exact argument vector that would be executed and
returns it, with the firmware's SHA-256, without touching anything.  An
operator (or Claude, which can call the passive planner but not the flash
itself without a grant) can read it, disagree with it, and change it before
any bytes move.

Permission mapping is fixed:

* inspecting a file            PASSIVE
* reading target identity      QUERY
* reset                        CONTROL
* programming                  FLASH
* mass erase                   DESTRUCTIVE
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fielddeck.common.errors import InvalidRequest, UnsupportedCapability
from fielddeck.common.models import PermissionLevel
from fielddeck.common.paths import default_paths
from fielddeck.common.process import ToolResult, have_tool, run_tool, tool_version
from fielddeck.common.timebase import Timestamp
from fielddeck.debug.firmware import inspect_firmware

__all__ = ["FlashPlan", "build_plan", "firmware_roots", "run_plan"]


@dataclass(slots=True)
class FlashPlan:
    """Exactly what would be run, and what it would do."""

    tool: str
    args: list[str]
    operation: str
    permission: PermissionLevel
    description: str
    target: str | None = None
    firmware: str | None = None
    firmware_sha256: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "command": " ".join([self.tool, *self.args]),
            "args": self.args,
            "operation": self.operation,
            "permission": str(self.permission),
            "description": self.description,
            "target": self.target,
            "firmware": self.firmware,
            "firmware_sha256": self.firmware_sha256,
            "warnings": self.warnings,
            "tool_available": have_tool(self.tool),
        }


def firmware_roots() -> list[Path]:
    """Directories a firmware file may be read from.

    Deliberately narrow: a path arriving from a recipe or the MCP surface must
    not be able to hand an external programmer an arbitrary file.  Override the
    firmware library location with ``FIELDDECK_FIRMWARE_DIR``.
    """
    paths = default_paths()
    roots = [paths.state_dir / "firmware", paths.sessions_dir]
    override = os.environ.get("FIELDDECK_FIRMWARE_DIR")
    if override:
        roots.append(Path(override).expanduser())
    return [root for root in roots if root.exists()]


def _resolve_firmware(path_str: str, *, extra_roots: list[Path] | None = None) -> Path:
    candidate = Path(path_str).expanduser().resolve()
    roots = [root.resolve() for root in (firmware_roots() + (extra_roots or []))]
    if not roots:
        raise InvalidRequest(
            "no firmware directory is configured; create "
            f"{default_paths().state_dir / 'firmware'} or set FIELDDECK_FIRMWARE_DIR",
            details={"requested": path_str},
        )
    if not any(candidate.is_relative_to(root) for root in roots):
        raise InvalidRequest(
            f"{path_str} is outside the permitted firmware directories",
            details={"requested": str(candidate), "allowed": [str(r) for r in roots]},
            preserved="nothing was read and no programmer was started",
        )
    if not candidate.is_file():
        raise InvalidRequest(f"no firmware file at {candidate}", details={"path": str(candidate)})
    return candidate


# ---------------------------------------------------------------------------
# Per-tool planners
# ---------------------------------------------------------------------------


def _plan_openocd(
    *, operation: str, target: str, interface: str, firmware: Path | None, address: str | None
) -> FlashPlan:
    base = ["-f", f"interface/{interface}.cfg", "-f", f"target/{target}.cfg"]
    warnings = [
        "OpenOCD config names are not validated by FieldDeck; a wrong target "
        "config can halt or misprogram a device",
    ]
    if operation == "info":
        return FlashPlan(
            tool="openocd",
            args=[*base, "-c", "init", "-c", "targets", "-c", "shutdown"],
            operation="info",
            permission=PermissionLevel.QUERY,
            description="halt-free target enumeration",
            target=target,
            warnings=warnings,
        )
    if operation == "reset":
        return FlashPlan(
            tool="openocd",
            args=[*base, "-c", "init", "-c", "reset run", "-c", "shutdown"],
            operation="reset",
            permission=PermissionLevel.CONTROL,
            description="assert reset and run",
            target=target,
            warnings=warnings,
        )
    if operation == "erase":
        return FlashPlan(
            tool="openocd",
            args=[
                *base,
                "-c",
                "init",
                "-c",
                "halt",
                "-c",
                "flash erase_sector 0 0 last",
                "-c",
                "reset run",
                "-c",
                "shutdown",
            ],
            operation="erase",
            permission=PermissionLevel.DESTRUCTIVE,
            description="MASS ERASE: every sector of flash bank 0 is destroyed",
            target=target,
            warnings=[*warnings, "this is irreversible and removes the existing firmware"],
        )
    if firmware is None:
        raise InvalidRequest(f"{operation} needs a firmware file")
    if operation == "verify":
        return FlashPlan(
            tool="openocd",
            args=[
                *base,
                "-c",
                "init",
                "-c",
                "halt",
                "-c",
                f"verify_image {firmware}",
                "-c",
                "reset run",
                "-c",
                "shutdown",
            ],
            operation="verify",
            permission=PermissionLevel.QUERY,
            description="compare target flash against the image without writing",
            target=target,
            firmware=str(firmware),
            warnings=warnings,
        )
    write = f"program {firmware}" + (f" {address}" if address else "") + " verify reset exit"
    return FlashPlan(
        tool="openocd",
        args=[*base, "-c", write],
        operation="program",
        permission=PermissionLevel.FLASH,
        description="erase the affected sectors, write the image, verify, reset",
        target=target,
        firmware=str(firmware),
        warnings=warnings,
    )


def _plan_pyocd(
    *, operation: str, target: str, firmware: Path | None, address: str | None
) -> FlashPlan:
    if operation == "info":
        return FlashPlan(
            tool="pyocd",
            args=["list", "--targets"] if not target else ["cmd", "-t", target, "-c", "status"],
            operation="info",
            permission=PermissionLevel.QUERY,
            description="probe and target enumeration",
            target=target,
        )
    if operation == "reset":
        return FlashPlan(
            tool="pyocd",
            args=["reset", "-t", target],
            operation="reset",
            permission=PermissionLevel.CONTROL,
            description="reset the target",
            target=target,
        )
    if operation == "erase":
        return FlashPlan(
            tool="pyocd",
            args=["erase", "-t", target, "--chip"],
            operation="erase",
            permission=PermissionLevel.DESTRUCTIVE,
            description="CHIP ERASE: all flash contents destroyed",
            target=target,
            warnings=["irreversible"],
        )
    if firmware is None:
        raise InvalidRequest(f"{operation} needs a firmware file")
    args = ["flash", "-t", target]
    if address:
        args += ["--base-address", address]
    args.append(str(firmware))
    return FlashPlan(
        tool="pyocd",
        args=args,
        operation="program",
        permission=PermissionLevel.FLASH,
        description="program and verify the image",
        target=target,
        firmware=str(firmware),
    )


def _plan_esptool(
    *, operation: str, port: str, firmware: Path | None, address: str | None, baud: int
) -> FlashPlan:
    base = ["--port", port, "--baud", str(baud)]
    if operation == "info":
        return FlashPlan(
            tool="esptool.py",
            args=[*base, "chip_id"],
            operation="info",
            permission=PermissionLevel.QUERY,
            description="read chip type and MAC",
            target=port,
        )
    if operation == "erase":
        return FlashPlan(
            tool="esptool.py",
            args=[*base, "erase_flash"],
            operation="erase",
            permission=PermissionLevel.DESTRUCTIVE,
            description="ERASE ENTIRE FLASH including any stored calibration data",
            target=port,
            warnings=[
                "on many ESP modules this also destroys factory Wi-Fi/RF calibration "
                "data stored in NVS, which cannot be regenerated in the field"
            ],
        )
    if firmware is None:
        raise InvalidRequest(f"{operation} needs a firmware file")
    if operation == "verify":
        return FlashPlan(
            tool="esptool.py",
            args=[*base, "verify_flash", address or "0x0", str(firmware)],
            operation="verify",
            permission=PermissionLevel.QUERY,
            description="compare flash contents against the image",
            target=port,
            firmware=str(firmware),
        )
    return FlashPlan(
        tool="esptool.py",
        args=[*base, "write_flash", address or "0x0", str(firmware)],
        operation="program",
        permission=PermissionLevel.FLASH,
        description="write the image at the given offset",
        target=port,
        firmware=str(firmware),
        warnings=["the offset must match the partition table; a wrong offset bricks the boot"],
    )


def _plan_dfu(*, operation: str, firmware: Path | None, alt: str, device: str | None) -> FlashPlan:
    base = ["-a", alt] + (["-d", device] if device else [])
    if operation == "info":
        return FlashPlan(
            tool="dfu-util",
            args=["-l"],
            operation="info",
            permission=PermissionLevel.QUERY,
            description="list DFU-capable devices and their alt settings",
        )
    if firmware is None:
        raise InvalidRequest(f"{operation} needs a firmware file")
    return FlashPlan(
        tool="dfu-util",
        args=[*base, "-D", str(firmware)],
        operation="program",
        permission=PermissionLevel.FLASH,
        description="download the image over USB DFU",
        firmware=str(firmware),
        warnings=["choosing the wrong alt setting can overwrite the bootloader itself"],
    )


_PLANNERS = {"openocd", "pyocd", "esptool", "dfu-util"}


def build_plan(
    *,
    tool: str,
    operation: str,
    target: str = "",
    interface: str = "stlink",
    port: str = "",
    firmware_path: str | None = None,
    address: str | None = None,
    baud: int = 460800,
    alt: str = "0",
    device: str | None = None,
    extra_roots: list[Path] | None = None,
) -> tuple[FlashPlan, dict[str, Any] | None]:
    """Build a command plan and inspect the firmware it would write.

    Returns ``(plan, firmware_info)``.  Nothing is executed.
    """
    if tool not in _PLANNERS:
        raise UnsupportedCapability(
            f"unknown programming tool {tool!r}",
            details={"known": sorted(_PLANNERS)},
        )
    if operation not in {"info", "reset", "program", "verify", "erase"}:
        raise InvalidRequest(
            f"unknown operation {operation!r}",
            details={"known": ["info", "reset", "program", "verify", "erase"]},
        )

    firmware: Path | None = None
    info: dict[str, Any] | None = None
    if firmware_path:
        firmware = _resolve_firmware(firmware_path, extra_roots=extra_roots)
        info = dict(inspect_firmware(firmware))

    if tool == "openocd":
        if not target:
            raise InvalidRequest("openocd needs a target config name, e.g. target='stm32f4x'")
        plan = _plan_openocd(
            operation=operation,
            target=target,
            interface=interface,
            firmware=firmware,
            address=address,
        )
    elif tool == "pyocd":
        if not target and operation != "info":
            raise InvalidRequest("pyocd needs a target name, e.g. target='stm32f407vg'")
        plan = _plan_pyocd(operation=operation, target=target, firmware=firmware, address=address)
    elif tool == "esptool":
        if not port:
            raise InvalidRequest("esptool needs a serial port, e.g. port='/dev/ttyUSB0'")
        plan = _plan_esptool(
            operation=operation, port=port, firmware=firmware, address=address, baud=baud
        )
    else:
        plan = _plan_dfu(operation=operation, firmware=firmware, alt=alt, device=device)

    if info is not None:
        plan.firmware_sha256 = info.get("sha256")
    if not have_tool(plan.tool):
        plan.warnings.append(f"{plan.tool} is not installed on this system")
    return plan, info


async def run_plan(
    plan: FlashPlan, *, timeout_s: float = 300.0, allowed_roots: list[Path] | None = None
) -> dict[str, Any]:
    """Execute a plan and return a complete audit record.

    Records the tool and its version, the exact command, the firmware hash and
    the timings — everything needed to answer "what was actually put on this
    device, and when?" months later.
    """
    if not have_tool(plan.tool):
        raise UnsupportedCapability(
            f"{plan.tool} is not installed; install it before running a {plan.operation}",
            details={"tool": plan.tool, "operation": plan.operation},
            preserved="the target was not touched",
        )
    started = Timestamp.now()
    version = await tool_version(plan.tool)
    result: ToolResult = await run_tool(
        plan.tool,
        plan.args,
        timeout_s=timeout_s,
        allowed_path_roots=(allowed_roots or firmware_roots()) or None,
    )
    finished = Timestamp.now()
    record = {
        "operation": plan.operation,
        "tool": plan.tool,
        "tool_version": version,
        "command": result.command_line,
        "target": plan.target,
        "firmware": plan.firmware,
        "firmware_sha256": plan.firmware_sha256,
        "permission": str(plan.permission),
        "started_utc_ns": started.utc_ns,
        "finished_utc_ns": finished.utc_ns,
        "duration_ms": round(result.duration_ns / 1e6, 1),
        "returncode": result.returncode,
        "ok": result.ok,
        "timed_out": result.timed_out,
        "stdout": result.stdout[-8000:],
        "stderr": result.stderr[-8000:],
    }
    if not result.ok:
        result.check(what=f"{plan.tool} {plan.operation}")
    return record
