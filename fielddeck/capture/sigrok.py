"""Logic analyzer capture and protocol decoding via sigrok.

libsigrok already supports several hundred instruments and libsigrokdecode
already implements the protocol decoders.  Reimplementing either would be a
worse decoder that supports one device, so FieldDeck drives ``sigrok-cli``
through the external-tool adapter instead.

Two rules shape this module:

* the **native** capture (``.sr``) is written first and never touched again
* every decode is a *derived* artifact that names the capture it came from,
  the decoder that produced it and the decoder's options, so a surprising
  UART line can always be traced back to the samples underneath it
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field

from fielddeck.common.config import FieldDeckConfig
from fielddeck.common.errors import CaptureError, ExternalToolError, UnsupportedCapability
from fielddeck.common.models import (
    ConnectionState,
    DeviceCapability,
    DeviceDescriptor,
    DeviceRole,
    PermissionLevel,
    TransportKind,
)
from fielddeck.common.process import have_tool, run_tool, tool_version
from fielddeck.drivers.base import ActionContext, DeviceParams, Driver, action

__all__ = ["SigrokDriver", "discover_logic_drivers"]

#: sigrok decoders FieldDeck exposes directly.  Any other decoder libsigrokdecode
#: knows about can still be named explicitly; these are the ones with a
#: first-class parameter shape.
DECODERS: dict[str, dict[str, Any]] = {
    "uart": {"channels": ("rx", "tx"), "options": ("baudrate", "data_bits", "parity", "stop_bits")},
    "i2c": {"channels": ("scl", "sda"), "options": ()},
    "spi": {"channels": ("clk", "mosi", "miso", "cs"), "options": ("cpol", "cpha", "bitorder")},
    "can": {"channels": ("can_rx",), "options": ("bitrate",)},
    "onewire_link": {"channels": ("owr",), "options": ()},
}


class LogicCaptureParams(DeviceParams):
    samplerate: str = Field(default="1m", description="e.g. '1m', '4m', '20m'")
    samples: int | None = Field(default=None, ge=1, le=100_000_000)
    seconds: float | None = Field(default=None, gt=0, le=600)
    channels: list[str] | None = None
    label: str = "logic"
    trigger: str | None = Field(
        default=None, description="sigrok trigger spec, e.g. 'D2=r' for rising edge"
    )


class LogicDecodeParams(DeviceParams):
    """Decode an existing capture.  Never touches hardware."""

    artifact_path: str
    decoder: str
    #: sigrok channel assignment, e.g. {"rx": "D0"}
    channels: dict[str, str] = Field(default_factory=dict)
    options: dict[str, str | int] = Field(default_factory=dict)
    annotation: str | None = None


class SigrokDriver(Driver):
    """One sigrok-supported acquisition device."""

    kind = TransportKind.LOGIC

    def __init__(
        self,
        *,
        driver_name: str,
        connection: str | None,
        model: str,
        channels: list[str],
        serial: str | None = None,
    ) -> None:
        identity = serial or connection or driver_name
        descriptor = DeviceDescriptor(
            id=f"logic:sigrok:{driver_name}:{identity}".replace(" ", "-"),
            kind=TransportKind.LOGIC,
            display_name=f"{model} ({driver_name})",
            vendor="sigrok",
            product=model,
            serial_number=serial,
            roles=[DeviceRole.ANALYZER],
            capabilities=[DeviceCapability.RX, DeviceCapability.STREAM, DeviceCapability.DECODE],
            state=ConnectionState.DISCOVERED,
            stable_id=serial is not None,
            metadata={
                "driver": driver_name,
                "connection": connection,
                "channels": channels,
                "decoders": sorted(DECODERS),
            },
        )
        super().__init__(descriptor)
        self.driver_name = driver_name
        self.connection = connection
        self.channels = channels

    @property
    def _device_arg(self) -> str:
        return f"{self.driver_name}:conn={self.connection}" if self.connection else self.driver_name

    async def status(self) -> dict[str, Any]:
        return {
            "driver": self.driver_name,
            "connection": self.connection,
            "channels": self.channels,
            "state": str(self._descriptor.state),
            "sigrok_cli": await tool_version("sigrok-cli"),
            "decoders": sorted(DECODERS),
        }

    # -- actions -----------------------------------------------------------

    @action(
        "logic.status",
        permission=PermissionLevel.PASSIVE,
        params=DeviceParams,
        state_changing=False,
        description="Analyzer configuration and available decoders.",
        allowed_during_estop=True,
        timeout_s=20.0,
    )
    async def logic_status(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        return await self.status()

    @action(
        "logic.capture",
        permission=PermissionLevel.PASSIVE,
        params=LogicCaptureParams,
        state_changing=False,
        description="Acquire samples into a native sigrok capture file.",
        cancelable=True,
        timeout_s=660.0,
    )
    async def logic_capture(self, ctx: ActionContext, params: LogicCaptureParams) -> dict[str, Any]:
        """A logic analyzer only listens, so acquisition is PASSIVE."""
        if ctx.recorder is None:
            raise CaptureError(
                "logic capture needs an active session to write into; "
                'start one with: fdctl session start "<name>"',
                preserved="no acquisition was started",
            )
        if params.samples is None and params.seconds is None:
            raise CaptureError(
                "give either samples or seconds so the capture is bounded",
                details={"hint": "samples=1000000 or seconds=2"},
            )

        path = ctx.recorder.capture_path("logic", params.label, ".sr")
        args = ["-d", self._device_arg, "--config", f"samplerate={params.samplerate}"]
        if params.channels:
            args += ["--channels", ",".join(params.channels)]
        if params.trigger:
            args += ["--trigger", params.trigger]
        if params.samples is not None:
            args += ["--samples", str(params.samples)]
        else:
            args += ["--time", str(int((params.seconds or 1) * 1000))]
        args += ["-o", str(path)]

        timeout = (ctx.remaining_s() or 600.0) - 5.0
        result = await run_tool(
            "sigrok-cli",
            args,
            timeout_s=max(5.0, timeout),
            allowed_path_roots=[ctx.recorder.root],
        )
        result.check(what="sigrok-cli capture")
        if not path.exists() or path.stat().st_size == 0:
            raise CaptureError(
                "sigrok-cli reported success but produced no capture file",
                details={"command": result.command_line, "stderr": result.stderr[-1000:]},
                preserved="nothing was written; the analyzer was not left configured",
            )

        artifact = ctx.recorder.add_artifact(
            path,
            kind="logic",
            media_type="application/x-sigrok",
            device_id=self.device_id,
            raw=True,
            metadata={
                "samplerate": params.samplerate,
                "samples": params.samples,
                "trigger": params.trigger,
                "channels": params.channels or self.channels,
            },
        )
        return {
            "artifact": artifact.model_dump(mode="json"),
            "path": str(path),
            "size_bytes": artifact.size_bytes,
            "command": result.command_line,
            "note": "raw capture preserved; decode it with logic.decode",
        }

    @action(
        "logic.decode",
        permission=PermissionLevel.PASSIVE,
        params=LogicDecodeParams,
        state_changing=False,
        description="Run a libsigrokdecode protocol decoder over an existing capture.",
        timeout_s=300.0,
        allowed_during_estop=True,
    )
    async def logic_decode(self, ctx: ActionContext, params: LogicDecodeParams) -> dict[str, Any]:
        """Pure post-processing: reads a saved capture, writes a derived artifact."""
        if ctx.recorder is None:
            raise CaptureError("decoding writes into a session; start one first")
        source = (ctx.recorder.root / params.artifact_path).resolve()
        if not source.is_relative_to(ctx.recorder.root.resolve()):
            raise CaptureError(
                "capture path is outside the session directory",
                details={"artifact_path": params.artifact_path},
            )
        if not source.exists():
            raise CaptureError(
                f"no capture at {params.artifact_path}",
                details={"session": ctx.recorder.session_id},
            )

        spec = params.decoder
        for channel, assignment in params.channels.items():
            spec += f":{channel}={assignment}"
        for option, value in params.options.items():
            spec += f":{option}={value}"

        args = ["-i", str(source), "-P", spec]
        if params.annotation:
            args += ["-A", params.annotation]
        result = await run_tool(
            "sigrok-cli",
            args,
            timeout_s=max(10.0, (ctx.remaining_s() or 300.0) - 5.0),
            allowed_path_roots=[ctx.recorder.root],
        )
        result.check(what=f"sigrok-cli {params.decoder} decode")

        out_path = ctx.recorder.capture_path("logic", f"{source.stem}-{params.decoder}", ".txt")
        out_path.write_text(result.stdout, encoding="utf-8")
        version = await tool_version("sigrok-cli")

        source_artifacts = [
            row["artifact_id"]
            for row in ctx.recorder.timeline.artifacts()
            if row["relative_path"] == params.artifact_path
        ]
        artifact = ctx.recorder.add_artifact(
            out_path,
            kind="logic",
            media_type="text/plain",
            device_id=self.device_id,
            raw=False,
            source_artifact_ids=source_artifacts,
            producer="sigrok-cli",
            producer_version=version,
            producer_config={"decoder": spec, "annotation": params.annotation},
        )
        lines = result.stdout.splitlines()
        return {
            "decoder": params.decoder,
            "spec": spec,
            "lines": len(lines),
            "preview": lines[:40],
            "artifact": artifact.model_dump(mode="json"),
            "derived_from": params.artifact_path,
        }


def _parse_scan(output: str) -> list[dict[str, Any]]:
    """Parse ``sigrok-cli --scan`` output.

    The format is stable but not machine-oriented::

        fx2lafw:conn=1.7 - Saleae Logic with 8 channels: D0 D1 D2 ...
    """
    devices: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("The following") or ":" not in line:
            continue
        head, _, tail = line.partition(" - ")
        if not tail:
            continue
        driver_part = head.strip()
        driver_name, _, conn_part = driver_part.partition(":")
        connection = conn_part.split("=", 1)[1] if "=" in conn_part else None
        model, _, channel_part = tail.partition(" with ")
        channels = [
            token
            for token in channel_part.replace("channels:", "").split()
            if token and not token.isdigit()
        ]
        devices.append(
            {
                "driver": driver_name.strip(),
                "connection": connection,
                "model": model.strip(),
                "channels": channels,
            }
        )
    return devices


async def scan_logic_devices() -> list[dict[str, Any]]:
    """Ask sigrok what is attached.  Enumeration only."""
    if not have_tool("sigrok-cli"):
        raise UnsupportedCapability(
            "sigrok-cli is not installed; install it with: sudo apt install sigrok-cli",
            details={"tool": "sigrok-cli"},
        )
    try:
        result = await run_tool("sigrok-cli", ["--scan"], timeout_s=20.0)
    except ExternalToolError:
        return []
    return _parse_scan(result.stdout)


def discover_logic_drivers(config: FieldDeckConfig) -> list[Driver]:
    """Discovery hook.

    ``sigrok-cli --scan`` spawns a process and probes USB, which is too heavy
    for the discovery timer, so this returns nothing and the operator scans
    explicitly through ``logic.devices``.  Being slow is not a reason to make
    device enumeration surprising.
    """
    return []


def build_drivers_from_scan(devices: list[dict[str, Any]]) -> list[Driver]:
    return [
        SigrokDriver(
            driver_name=entry["driver"],
            connection=entry.get("connection"),
            model=entry.get("model", entry["driver"]),
            channels=entry.get("channels", []),
        )
        for entry in devices
    ]


def decoder_catalogue() -> str:
    return json.dumps(DECODERS, indent=2)
