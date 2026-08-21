"""Simulated CAN bus.

Emits a small but realistic traffic pattern: three periodic arbitration IDs at
different rates, a rolling counter, a per-frame checksum, and an optional
fault where 0x181 stops transmitting — the scenario the unified timeline is
designed to explain.

Nothing here transmits anywhere.  ``can.send`` still requires a CONTROL grant,
because the point of simulation is exercising the real authorization path.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from fielddeck.analysis.crc import crc
from fielddeck.common.errors import InvalidRequest
from fielddeck.common.models import (
    ConnectionState,
    DeviceCapability,
    DeviceDescriptor,
    DeviceRole,
    PermissionLevel,
    TransportKind,
)
from fielddeck.common.timebase import Timestamp, monotonic_ns
from fielddeck.drivers.base import ActionContext, DeviceParams, Driver, action
from fielddeck.sim.base import JitterClock, SimulatedDeviceMixin, seeded_random
from fielddeck.sim.scenario import Scenario

__all__ = ["SimCanDriver"]


class CanListenParams(DeviceParams):
    duration_s: float = Field(default=2.0, gt=0, le=3600)
    max_frames: int = Field(default=2000, ge=1, le=200_000)
    id_filter: list[int] | None = None


class CanCaptureParams(CanListenParams):
    label: str = "capture"


class CanSendParams(DeviceParams):
    can_id: int = Field(ge=0, le=0x1FFFFFFF)
    data: str = Field(description="Payload as hex, e.g. '01 03 00 00'")
    extended: bool = False
    count: int = Field(default=1, ge=1, le=1000)

    @field_validator("data")
    @classmethod
    def _valid_hex(cls, value: str) -> str:
        cleaned = value.replace(" ", "").replace("0x", "")
        try:
            payload = bytes.fromhex(cleaned)
        except ValueError as exc:
            raise ValueError(f"data must be hex bytes, got {value!r}") from exc
        if len(payload) > 8:
            raise ValueError("classic CAN payload is at most 8 bytes")
        return cleaned


class CanStatsParams(DeviceParams):
    duration_s: float = Field(default=2.0, gt=0, le=60)


class CanDecodeParams(DeviceParams):
    dbc: str = Field(description="Path to a .dbc/.kcd/.sym database")
    path: str = Field(description="Capture file, relative to the session directory")
    label: str = Field(default="decoded", max_length=64)
    max_frames: int = Field(default=500_000, ge=1, le=20_000_000)


#: (arbitration id, period, jitter, dlc, description)
_TRAFFIC = (
    (0x101, 0.010, 0.0004, 8, "motor command"),
    (0x181, 0.100, 0.0018, 8, "motor status"),
    (0x280, 1.000, 0.0100, 4, "diagnostics"),
)


class SimCanDriver(SimulatedDeviceMixin, Driver):
    """A virtual 500 kbit/s CAN interface."""

    kind = TransportKind.CAN

    def __init__(
        self,
        interface: str = "can0",
        *,
        bitrate: int = 500_000,
        fault_after_s: float | None = None,
        scenario: Scenario | None = None,
    ) -> None:
        descriptor = DeviceDescriptor(
            id=f"sim:can:{interface}",
            kind=TransportKind.CAN,
            display_name=f"Simulated CAN {interface}",
            path=f"/dev/null#{interface}",
            vendor="FieldDeck",
            product="Simulated SocketCAN",
            roles=[DeviceRole.BUS],
            capabilities=[
                DeviceCapability.RX,
                DeviceCapability.TX,
                DeviceCapability.BITRATE_CONFIG,
                DeviceCapability.LISTEN_ONLY,
                DeviceCapability.STREAM,
                DeviceCapability.SAFE_STATE,
            ],
            state=ConnectionState.READY,
            simulated=True,
            metadata={"bitrate": bitrate, "interface": interface, "mode": "listen-only"},
        )
        Driver.__init__(self, descriptor)
        SimulatedDeviceMixin.__init__(self)
        self.interface = interface
        self.bitrate = bitrate
        self._fault_after_s = fault_after_s
        self._scenario = scenario or Scenario()
        self._rng = seeded_random(descriptor.id)
        self._clocks = {
            can_id: JitterClock(period, jitter, seeded_random(f"{descriptor.id}:{can_id:x}"))
            for can_id, period, jitter, _dlc, _desc in _TRAFFIC
        }
        self._counters: dict[int, int] = dict.fromkeys(self._clocks, 0)
        self._tx_count = 0
        #: TX is locked until a CONTROL grant lets the dispatcher through.
        self._listen_only = True

    # -- driver contract ---------------------------------------------------

    async def status(self) -> dict[str, Any]:
        return {
            "interface": self.interface,
            "bitrate": self.bitrate,
            "mode": "listen-only" if self._listen_only else "normal",
            "state": str(self._descriptor.state),
            "tx_frames": self._tx_count,
            "bus_errors": 0,
            "uptime_s": round(self.sim_elapsed_s, 3),
            "scenario": self._scenario.describe(),
        }

    async def safe_state(self) -> dict[str, Any]:
        """Stop transmitting and return to listen-only."""
        was_transmitting = not self._listen_only
        self._listen_only = True
        return {
            "device": self.device_id,
            "applied": True,
            "changed": was_transmitting,
            "state": "listen-only, TX locked",
        }

    # -- traffic generation ------------------------------------------------

    def _frames_between(
        self, start_ns: int, end_ns: int, *, id_filter: list[int] | None = None
    ) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        for can_id, _period, _jitter, dlc, description in _TRAFFIC:
            if id_filter is not None and can_id not in id_filter:
                continue
            for stamp in self._clocks[can_id].timestamps(start_ns, end_ns):
                if self._faulted(stamp, can_id):
                    continue
                self._counters[can_id] = (self._counters[can_id] + 1) & 0xFF
                frames.append(
                    {
                        "monotonic_ns": stamp,
                        "can_id": can_id,
                        "extended": False,
                        "dlc": dlc,
                        "data": self._payload(can_id, dlc, self._counters[can_id]).hex(),
                        "description": description,
                    }
                )
        frames.sort(key=lambda frame: frame["monotonic_ns"])
        return frames

    def _faulted(self, stamp_ns: int, can_id: int) -> bool:
        """The scripted failure: the controller stops transmitting 0x181.

        Driven by the shared bench scenario rather than a timer of its own,
        so the dropout lands a fixed 312 ms after the supply current climbs
        and the two are genuinely correlated on the timeline.
        """
        if self._scenario.can_id_silent(can_id, stamp_ns):
            return True
        if self._fault_after_s is None or can_id != 0x181:
            return False
        return (stamp_ns - self._sim_started_ns) / 1e9 > self._fault_after_s

    def _payload(self, can_id: int, dlc: int, counter: int) -> bytes:
        body = bytearray(dlc)
        body[0] = counter
        for index in range(1, dlc - 1):
            body[index] = (can_id + counter * index) & 0xFF
        if dlc >= 2:
            # Last byte is an 8-bit CRC over the rest, so the analysis tools
            # have something genuine to discover rather than a magic constant.
            body[dlc - 1] = crc("crc8", bytes(body[: dlc - 1]))
        return bytes(body)

    # -- actions -----------------------------------------------------------

    @action(
        "can.status",
        permission=PermissionLevel.PASSIVE,
        params=DeviceParams,
        state_changing=False,
        description="Interface configuration and error counters.",
        allowed_during_estop=True,
    )
    async def can_status(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        return await self.status()

    @action(
        "can.listen",
        permission=PermissionLevel.PASSIVE,
        params=CanListenParams,
        state_changing=False,
        description="Receive frames without transmitting anything.",
        cancelable=True,
        timeout_s=3600.0,
    )
    async def can_listen(self, ctx: ActionContext, params: CanListenParams) -> dict[str, Any]:
        """Passive receive.  Listen-only: nothing reaches the bus."""
        import asyncio

        start = monotonic_ns()
        frames: list[dict[str, Any]] = []
        deadline = start + int(params.duration_s * 1e9)
        cursor = start
        while cursor < deadline and len(frames) < params.max_frames:
            await asyncio.sleep(min(0.05, max(0.0, (deadline - cursor) / 1e9)))
            now = monotonic_ns()
            frames.extend(self._frames_between(cursor, now, id_filter=params.id_filter))
            cursor = now
            if ctx.cancelled:
                break
        frames = frames[: params.max_frames]
        return {
            "interface": self.interface,
            "frames": frames,
            "count": len(frames),
            "duration_s": round((monotonic_ns() - start) / 1e9, 3),
            "cancelled": ctx.cancelled,
            "mode": "listen-only",
        }

    @action(
        "can.capture",
        permission=PermissionLevel.PASSIVE,
        params=CanCaptureParams,
        state_changing=False,
        description="Record frames to an immutable capture file in the session.",
        cancelable=True,
        timeout_s=3600.0,
    )
    async def can_capture(self, ctx: ActionContext, params: CanCaptureParams) -> dict[str, Any]:
        """Writes candump-format text, which can-utils and Wireshark both read."""
        listen = await self.can_listen(ctx, params)
        if ctx.recorder is None:
            return {**listen, "artifact": None, "warning": "no active session; frames not saved"}

        path = ctx.recorder.capture_path("can", f"{self.interface}-{params.label}", ".log")
        anchor = ctx.recorder.anchor
        with path.open("w", encoding="ascii") as handle:
            for frame in listen["frames"]:
                seconds = anchor.utc_for(frame["monotonic_ns"]) / 1e9
                handle.write(
                    f"({seconds:.6f}) {self.interface} "
                    f"{frame['can_id']:03X}#{frame['data'].upper()}\n"
                )

        artifact = None
        if listen["count"]:
            artifact = ctx.recorder.add_artifact(
                path,
                kind="can",
                media_type="text/vnd.candump",
                device_id=self.device_id,
                raw=True,
                metadata={"frames": listen["count"], "bitrate": self.bitrate},
            )
        else:
            # Same rule as the real SocketCAN driver: an empty file in the
            # session is worse than no file, because a zero-byte artifact with
            # a hash reads as "we recorded and the bus was quiet" -- evidence
            # of something that never happened. The frame count still says so.
            path.unlink(missing_ok=True)

        return {
            **listen,
            "frames": listen["frames"][:50],
            "truncated_in_result": listen["count"] > 50,
            "artifact": artifact.model_dump(mode="json") if artifact is not None else None,
        }

    @action(
        "can.stats",
        permission=PermissionLevel.PASSIVE,
        params=CanStatsParams,
        state_changing=False,
        description="Per-arbitration-ID rate, period and jitter statistics.",
        timeout_s=120.0,
    )
    async def can_stats(self, ctx: ActionContext, params: CanStatsParams) -> dict[str, Any]:
        listen = await self.can_listen(
            ctx, CanListenParams(device=params.device, duration_s=params.duration_s)
        )
        from fielddeck.analysis.timing import summarize_periods

        by_id: dict[int, list[int]] = {}
        last: dict[int, str] = {}
        for frame in listen["frames"]:
            by_id.setdefault(frame["can_id"], []).append(frame["monotonic_ns"])
            last[frame["can_id"]] = frame["data"]

        rows = []
        for can_id, stamps in sorted(by_id.items()):
            timing = summarize_periods(stamps)
            rows.append(
                {
                    "can_id": f"0x{can_id:03X}",
                    "count": len(stamps),
                    "hz": round(len(stamps) / max(params.duration_s, 1e-9), 1),
                    "period_ms": timing["mean_ms"],
                    "jitter_ms": timing["jitter_ms"],
                    "last": last[can_id].upper(),
                }
            )
        return {
            "interface": self.interface,
            "duration_s": params.duration_s,
            "total_frames": listen["count"],
            "ids": rows,
            "bus_load_percent": round(
                min(100.0, listen["count"] * 128 / (self.bitrate * params.duration_s) * 100), 1
            ),
        }

    @action(
        "can.decode",
        permission=PermissionLevel.PASSIVE,
        params=CanDecodeParams,
        state_changing=False,
        description="Decode a stored capture against a DBC into a derived artifact.",
        allowed_during_estop=True,
        timeout_s=300.0,
    )
    async def can_decode(self, ctx: ActionContext, params: CanDecodeParams) -> dict[str, Any]:
        """Post-processing over a file that already exists.

        Shares the real transport's implementation so a decode in simulation
        exercises the same code an engineer will run against a vehicle.
        """
        import asyncio

        from fielddeck.common.errors import CaptureError
        from fielddeck.transports.socketcan import decode_capture_file

        if ctx.recorder is None:
            raise CaptureError("decoding writes into a session; start one first")
        root = ctx.recorder.root.resolve()
        source = (root / params.path).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise CaptureError(
                f"no capture at {params.path}",
                details={"session": ctx.recorder.session_id},
                preserved="no file was read",
            )
        out = ctx.recorder.capture_path("can", f"{source.stem}-{params.label}", ".csv")
        summary, dbc_path, dbc_hash = await asyncio.to_thread(
            decode_capture_file, params.dbc, source, out, params.max_frames
        )
        sources = [
            row["artifact_id"]
            for row in ctx.recorder.timeline.artifacts()
            if row["relative_path"] == params.path
        ]
        artifact = ctx.recorder.add_artifact(
            out,
            kind="can",
            media_type="text/csv",
            device_id=self.device_id,
            raw=False,
            source_artifact_ids=sources,
            producer="cantools",
            producer_config={"dbc": dbc_path.name, "dbc_sha256": dbc_hash},
        )
        return {
            **summary,
            "artifact": artifact.model_dump(mode="json"),
            "derived_from": params.path,
            "dbc_sha256": dbc_hash,
        }

    @action(
        "can.send",
        permission=PermissionLevel.CONTROL,
        params=CanSendParams,
        state_changing=True,
        description="Transmit a frame onto the bus.",
        safe_state_note="Transmission stops and the interface returns to listen-only.",
    )
    async def can_send(self, ctx: ActionContext, params: CanSendParams) -> dict[str, Any]:
        """Requires CONTROL: this puts energy on a bus attached to a real DUT."""
        payload = bytes.fromhex(params.data)
        if params.can_id > 0x7FF and not params.extended:
            raise InvalidRequest(
                f"0x{params.can_id:X} needs an extended (29-bit) frame; pass extended=true",
                details={"can_id": params.can_id},
            )
        self._listen_only = False
        self._tx_count += params.count
        ts = Timestamp.now()
        return {
            "transmitted": params.count,
            "can_id": f"0x{params.can_id:X}",
            "data": payload.hex().upper(),
            "dlc": len(payload),
            "monotonic_ns": ts.monotonic_ns,
        }
