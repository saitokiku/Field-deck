"""Simulated logic analyzer.

Produces a plausible decoded UART trace so the logic screen, the decode
artifact chain and the provenance model can be exercised without owning a
Saleae.  It deliberately does NOT fabricate a ``.sr`` file: a fake native
capture that no real sigrok could read would be a trap for anyone who later
tried to open it. What it writes is clearly labelled simulated CSV.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import Field

from fielddeck.common.errors import CaptureError
from fielddeck.common.models import (
    ConnectionState,
    DeviceCapability,
    DeviceDescriptor,
    DeviceRole,
    PermissionLevel,
    TransportKind,
)
from fielddeck.drivers.base import ActionContext, DeviceParams, Driver, action
from fielddeck.sim.base import JitterClock, SimulatedDeviceMixin, seeded_random

__all__ = ["SimLogicDriver", "build_simulated_logic_devices"]

_CHANNELS = ["D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7"]


class SimLogicCaptureParams(DeviceParams):
    samplerate: str = "1m"
    seconds: float = Field(default=0.5, gt=0, le=10)
    channels: list[str] | None = None
    label: str = "logic"


class SimLogicDecodeParams(DeviceParams):
    artifact_path: str
    decoder: str = "uart"
    channels: dict[str, str] = Field(default_factory=lambda: {"rx": "D0"})
    options: dict[str, str | int] = Field(
        default_factory=lambda: cast("dict[str, str | int]", {"baudrate": 115200})
    )
    annotation: str | None = None


class SimLogicDriver(SimulatedDeviceMixin, Driver):
    kind = TransportKind.LOGIC

    def __init__(self, name: str = "sim-la-0") -> None:
        descriptor = DeviceDescriptor(
            id=f"sim:logic:{name}",
            kind=TransportKind.LOGIC,
            display_name="Simulated 8-channel logic analyzer",
            vendor="FieldDeck",
            product="SIM-LA-8",
            serial_number="SIMLA0001",
            roles=[DeviceRole.ANALYZER],
            capabilities=[DeviceCapability.RX, DeviceCapability.STREAM, DeviceCapability.DECODE],
            state=ConnectionState.READY,
            simulated=True,
            metadata={"channels": _CHANNELS, "decoders": ["uart", "i2c", "spi"]},
        )
        Driver.__init__(self, descriptor)
        SimulatedDeviceMixin.__init__(self)
        self._rng = seeded_random(descriptor.id)
        self._clock = JitterClock(0.001, 0.00002, seeded_random(f"{descriptor.id}:bit"))

    async def status(self) -> dict[str, Any]:
        return {
            "channels": _CHANNELS,
            "decoders": ["uart", "i2c", "spi"],
            "state": str(self._descriptor.state),
            "simulated": True,
        }

    @action(
        "logic.status",
        permission=PermissionLevel.PASSIVE,
        params=DeviceParams,
        state_changing=False,
        description="Analyzer configuration and available decoders.",
        allowed_during_estop=True,
    )
    async def logic_status(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        return await self.status()

    @action(
        "logic.capture",
        permission=PermissionLevel.PASSIVE,
        params=SimLogicCaptureParams,
        state_changing=False,
        description="Acquire simulated samples into the session.",
        cancelable=True,
        timeout_s=60.0,
    )
    async def logic_capture(
        self, ctx: ActionContext, params: SimLogicCaptureParams
    ) -> dict[str, Any]:
        import asyncio

        if ctx.recorder is None:
            raise CaptureError("logic capture needs an active session")
        await asyncio.sleep(min(params.seconds, 1.0))

        channels = params.channels or _CHANNELS[:2]
        path = ctx.recorder.capture_path("logic", params.label, ".csv")
        rows = int(min(params.seconds, 1.0) * 2000)
        with path.open("w", encoding="ascii") as handle:
            handle.write("# SIMULATED capture from FieldDeck sim:logic - not a sigrok .sr file\n")
            handle.write("time_s," + ",".join(channels) + "\n")
            for index in range(rows):
                bits = [str((index >> position) & 1) for position in range(len(channels))]
                handle.write(f"{index / 2000:.6f}," + ",".join(bits) + "\n")

        artifact = ctx.recorder.add_artifact(
            path,
            kind="logic",
            media_type="text/csv",
            device_id=self.device_id,
            raw=True,
            metadata={
                "samplerate": params.samplerate,
                "channels": channels,
                "simulated": True,
            },
        )
        return {
            "artifact": artifact.model_dump(mode="json"),
            "rows": rows,
            "channels": channels,
            "simulated": True,
        }

    @action(
        "logic.decode",
        permission=PermissionLevel.PASSIVE,
        params=SimLogicDecodeParams,
        state_changing=False,
        description="Decode a simulated capture; writes a derived artifact.",
        timeout_s=60.0,
        allowed_during_estop=True,
    )
    async def logic_decode(
        self, ctx: ActionContext, params: SimLogicDecodeParams
    ) -> dict[str, Any]:
        if ctx.recorder is None:
            raise CaptureError("decoding writes into a session; start one first")
        source = (ctx.recorder.root / params.artifact_path).resolve()
        if not source.is_relative_to(ctx.recorder.root.resolve()) or not source.exists():
            raise CaptureError(
                f"no capture at {params.artifact_path}",
                details={"session": ctx.recorder.session_id},
            )

        lines = [f"uart-1: rx: '{char}'" for char in "BOOT OK"]
        out = ctx.recorder.capture_path("logic", f"{source.stem}-{params.decoder}", ".txt")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")

        source_ids = [
            row["artifact_id"]
            for row in ctx.recorder.timeline.artifacts()
            if row["relative_path"] == params.artifact_path
        ]
        artifact = ctx.recorder.add_artifact(
            out,
            kind="logic",
            media_type="text/plain",
            device_id=self.device_id,
            raw=False,
            source_artifact_ids=source_ids,
            producer="fielddeck.sim.logic",
            producer_version="0.1.0",
            producer_config={"decoder": params.decoder, "options": params.options},
        )
        return {
            "decoder": params.decoder,
            "lines": len(lines),
            "preview": lines,
            "artifact": artifact.model_dump(mode="json"),
            "derived_from": params.artifact_path,
        }


def build_simulated_logic_devices() -> list[Driver]:
    return [SimLogicDriver()]
