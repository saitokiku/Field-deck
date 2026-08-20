"""Simulated serial device.

Produces the kind of stream that makes passive analysis worth having: an
ASCII boot banner, then a binary protocol with a 0x55 0xAA header, a rolling
counter, a CRC-16/MODBUS trailer, roughly 100 ms spacing, and an occasional
corrupted frame so the CRC checker has something to disagree with.

The electrical class is recorded as ``unknown`` on purpose.  Software cannot
tell TTL from RS-232 from RS-485, and FieldDeck never pretends otherwise.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import Field, field_validator

from fielddeck.analysis.crc import crc
from fielddeck.common.models import (
    ConnectionState,
    DeviceCapability,
    DeviceDescriptor,
    DeviceRole,
    PermissionLevel,
    TransportKind,
)
from fielddeck.common.timebase import monotonic_ns
from fielddeck.drivers.base import ActionContext, DeviceParams, Driver, action
from fielddeck.sim.base import JitterClock, SimulatedDeviceMixin, seeded_random

__all__ = ["SimSerialDriver"]

_BOOT_LINES = (
    b"\r\n[boot] fielddeck-sim controller v1.4.2\r\n",
    b"[boot] flash ok, 262144 bytes\r\n",
    b"[boot] entering run mode\r\n",
)


class SerialMonitorParams(DeviceParams):
    duration_s: float = Field(default=2.0, gt=0, le=3600)
    max_bytes: int = Field(default=65536, ge=1, le=8_000_000)


class SerialCaptureParams(SerialMonitorParams):
    label: str = "capture"


class SerialSendParams(DeviceParams):
    hex: str | None = Field(default=None, description="Payload as hex bytes")
    text: str | None = Field(default=None, description="Payload as text")
    append_newline: bool = False

    @field_validator("hex")
    @classmethod
    def _valid_hex(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.replace(" ", "").replace("0x", "")
        try:
            bytes.fromhex(cleaned)
        except ValueError as exc:
            raise ValueError(f"hex must be whole bytes, got {value!r}") from exc
        return cleaned


class SerialConfigureParams(DeviceParams):
    baudrate: int = Field(default=115200, ge=50, le=12_000_000)
    bytesize: int = Field(default=8, ge=5, le=8)
    parity: str = Field(default="N", pattern="^[NEOMS]$")
    stopbits: float = Field(default=1.0)


class SimSerialDriver(SimulatedDeviceMixin, Driver):
    """A virtual USB-serial adapter with a chatty DUT behind it."""

    kind = TransportKind.SERIAL

    def __init__(self, name: str = "sim-uart-0", *, baudrate: int = 115200) -> None:
        descriptor = DeviceDescriptor(
            id=f"sim:serial:{name}",
            kind=TransportKind.SERIAL,
            display_name=f"Simulated serial {name}",
            path=f"/dev/null#{name}",
            vendor="FieldDeck",
            product="Simulated FT232R",
            serial_number="SIM0001",
            roles=[DeviceRole.BUS],
            capabilities=[
                DeviceCapability.RX,
                DeviceCapability.TX,
                DeviceCapability.BAUD_CONFIG,
                DeviceCapability.STREAM,
            ],
            state=ConnectionState.READY,
            simulated=True,
            metadata={
                "baudrate": baudrate,
                "framing": "8N1",
                # Never inferred. RS-232, TTL and RS-485 are electrically
                # different and only the operator knows which is wired up.
                "electrical": "unknown",
            },
        )
        Driver.__init__(self, descriptor)
        SimulatedDeviceMixin.__init__(self)
        self.baudrate = baudrate
        self.framing = "8N1"
        self._rng = seeded_random(descriptor.id)
        self._clock = JitterClock(0.100, 0.0018, seeded_random(f"{descriptor.id}:pkt"))
        self._counter = 0
        self._tx_bytes = 0
        self._boot_emitted = False

    async def status(self) -> dict[str, Any]:
        return {
            "path": self._descriptor.path,
            "baudrate": self.baudrate,
            "framing": self.framing,
            "electrical": self._descriptor.metadata["electrical"],
            "state": str(self._descriptor.state),
            "tx_bytes": self._tx_bytes,
            "note": "electrical class is unknown to software; confirm the adapter physically",
        }

    async def safe_state(self) -> dict[str, Any]:
        return {
            "device": self.device_id,
            "applied": True,
            "changed": False,
            "state": "receive only; no transmit in progress",
        }

    # -- stream generation -------------------------------------------------

    def _packet(self, counter: int, *, corrupt: bool = False) -> bytes:
        body = bytes([0x55, 0xAA, 0x04, 0x10, counter & 0xFF, 0x00])
        checksum = crc("crc16-modbus", body).to_bytes(2, "little")
        if corrupt:
            checksum = bytes([checksum[0] ^ 0xFF, checksum[1]])
        return body + checksum

    def _bytes_between(self, start_ns: int, end_ns: int) -> list[tuple[int, bytes]]:
        chunks: list[tuple[int, bytes]] = []
        if not self._boot_emitted:
            self._boot_emitted = True
            for index, line in enumerate(_BOOT_LINES):
                chunks.append((start_ns + index * 1_000_000, line))
        for stamp in self._clock.timestamps(start_ns, end_ns):
            self._counter = (self._counter + 1) & 0xFF
            # Roughly 1 frame in 40 is damaged, so CRC checking has a real
            # signal-to-noise ratio to report instead of a perfect score.
            corrupt = self._rng.random() < 0.025
            chunks.append((stamp, self._packet(self._counter, corrupt=corrupt)))
        return chunks

    # -- actions -----------------------------------------------------------

    @action(
        "serial.status",
        permission=PermissionLevel.PASSIVE,
        params=DeviceParams,
        state_changing=False,
        description="Port configuration and byte counters.",
        allowed_during_estop=True,
    )
    async def serial_status(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        return await self.status()

    @action(
        "serial.configure",
        permission=PermissionLevel.PASSIVE,
        params=SerialConfigureParams,
        state_changing=False,
        description="Set local port framing. Does not transmit to the DUT.",
    )
    async def serial_configure(
        self, ctx: ActionContext, params: SerialConfigureParams
    ) -> dict[str, Any]:
        """Changes this end of the link only — no bytes reach the DUT."""
        self.baudrate = params.baudrate
        whole = params.stopbits == int(params.stopbits)
        stopbits = int(params.stopbits) if whole else params.stopbits
        self.framing = f"{params.bytesize}{params.parity}{stopbits}"
        self._descriptor.metadata["baudrate"] = params.baudrate
        self._descriptor.metadata["framing"] = self.framing
        return await self.status()

    @action(
        "serial.monitor",
        permission=PermissionLevel.PASSIVE,
        params=SerialMonitorParams,
        state_changing=False,
        description="Receive bytes without transmitting anything.",
        cancelable=True,
        timeout_s=3600.0,
    )
    async def serial_monitor(
        self, ctx: ActionContext, params: SerialMonitorParams
    ) -> dict[str, Any]:
        start = monotonic_ns()
        deadline = start + int(params.duration_s * 1e9)
        cursor = start
        chunks: list[tuple[int, bytes]] = []
        total = 0
        while cursor < deadline and total < params.max_bytes:
            await asyncio.sleep(min(0.05, max(0.0, (deadline - cursor) / 1e9)))
            now = monotonic_ns()
            for stamp, data in self._bytes_between(cursor, now):
                chunks.append((stamp, data))
                total += len(data)
            cursor = now
            if ctx.cancelled:
                break
        return {
            "device": self.device_id,
            "baudrate": self.baudrate,
            "framing": self.framing,
            "chunks": [
                {"monotonic_ns": stamp, "hex": data.hex(), "len": len(data)}
                for stamp, data in chunks
            ],
            "bytes": total,
            "duration_s": round((monotonic_ns() - start) / 1e9, 3),
            "cancelled": ctx.cancelled,
        }

    @action(
        "serial.capture",
        permission=PermissionLevel.PASSIVE,
        params=SerialCaptureParams,
        state_changing=False,
        description="Record the raw byte stream into the session, byte-exact.",
        cancelable=True,
        timeout_s=3600.0,
    )
    async def serial_capture(
        self, ctx: ActionContext, params: SerialCaptureParams
    ) -> dict[str, Any]:
        """Writes the bytes verbatim, plus a sidecar index of arrival times."""
        monitor = await self.serial_monitor(ctx, params)
        if ctx.recorder is None:
            return {**monitor, "artifact": None, "warning": "no active session; bytes not saved"}

        raw_path = ctx.recorder.capture_path("serial", params.label, ".bin")
        index_path = raw_path.with_suffix(".idx.jsonl")
        offset = 0
        with raw_path.open("wb") as raw, index_path.open("w", encoding="ascii") as index:
            for chunk in monitor["chunks"]:
                data = bytes.fromhex(chunk["hex"])
                raw.write(data)
                index.write(
                    f'{{"offset":{offset},"len":{len(data)},'
                    f'"monotonic_ns":{chunk["monotonic_ns"]}}}\n'
                )
                offset += len(data)

        artifact = ctx.recorder.add_artifact(
            raw_path,
            kind="serial",
            media_type="application/octet-stream",
            device_id=self.device_id,
            raw=True,
            metadata={"baudrate": self.baudrate, "framing": self.framing, "bytes": offset},
        )
        ctx.recorder.add_artifact(
            index_path,
            kind="serial",
            media_type="application/x-ndjson",
            device_id=self.device_id,
            raw=False,
            source_artifact_ids=[artifact.artifact_id],
            producer="fielddeck.sim.serial",
            producer_config={"description": "byte offset to arrival time index"},
        )
        return {
            **monitor,
            "chunks": monitor["chunks"][:20],
            "truncated_in_result": len(monitor["chunks"]) > 20,
            "artifact": artifact.model_dump(mode="json"),
        }

    @action(
        "serial.send",
        permission=PermissionLevel.CONTROL,
        params=SerialSendParams,
        state_changing=True,
        description="Transmit bytes to the device.",
        safe_state_note="Transmission stops; the port stays open for receive.",
    )
    async def serial_send(self, ctx: ActionContext, params: SerialSendParams) -> dict[str, Any]:
        """Requires CONTROL: these bytes reach a real DUT."""
        from fielddeck.common.errors import InvalidRequest

        if params.hex is None and params.text is None:
            raise InvalidRequest("give either hex or text to send")
        if params.hex is not None and params.text is not None:
            raise InvalidRequest("give hex or text, not both")
        payload = (
            bytes.fromhex(params.hex)
            if params.hex is not None
            else (params.text or "").encode("utf-8")
        )
        if params.append_newline:
            payload += b"\r\n"
        self._tx_bytes += len(payload)
        return {
            "device": self.device_id,
            "sent_bytes": len(payload),
            "hex": payload.hex().upper(),
            "monotonic_ns": monotonic_ns(),
        }
