"""Simulated camera.

Generates a small valid PNG so the snapshot path, artifact hashing and
session attachment can be exercised without a webcam.  The image is
recognisably synthetic — it is a test pattern with the session id burned in
as pixel noise, not a photograph — because an artifact that looks like real
evidence but is not would be the single most misleading thing in a session.
"""

from __future__ import annotations

import struct
import zlib
from typing import Any

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
from fielddeck.sim.base import SimulatedDeviceMixin

__all__ = ["SimCameraDriver", "build_simulated_camera_devices"]


class SimSnapshotParams(DeviceParams):
    label: str = Field(default="snapshot", max_length=64)
    width: int = Field(default=320, ge=16, le=1920)
    height: int = Field(default=240, ge=16, le=1080)
    note: str | None = None


def _png(width: int, height: int, seed: int) -> bytes:
    """Write a minimal valid RGB PNG without pulling in an image library."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 for each scanline
        for x in range(width):
            # A colour-bar test pattern: obviously synthetic at a glance.
            bar = (x * 8) // max(width, 1)
            raw += bytes(
                (
                    255 if bar & 1 else 32,
                    255 if bar & 2 else 32,
                    (255 if bar & 4 else 32) ^ ((y + seed) & 0x1F),
                )
            )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


class SimCameraDriver(SimulatedDeviceMixin, Driver):
    kind = TransportKind.CAMERA

    def __init__(self, name: str = "sim-cam-0") -> None:
        descriptor = DeviceDescriptor(
            id=f"sim:camera:{name}",
            kind=TransportKind.CAMERA,
            display_name="Simulated USB camera",
            vendor="FieldDeck",
            product="SIM-CAM",
            roles=[DeviceRole.CAMERA],
            capabilities=[DeviceCapability.SNAPSHOT],
            state=ConnectionState.READY,
            simulated=True,
            metadata={"auto_upload": False},
        )
        Driver.__init__(self, descriptor)
        SimulatedDeviceMixin.__init__(self)
        self._counter = 0

    async def status(self) -> dict[str, Any]:
        return {
            "state": str(self._descriptor.state),
            "snapshots": self._counter,
            "auto_upload": False,
            "simulated": True,
        }

    @action(
        "camera.status",
        permission=PermissionLevel.PASSIVE,
        params=DeviceParams,
        state_changing=False,
        description="Camera availability.",
        allowed_during_estop=True,
    )
    async def camera_status(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        return await self.status()

    @action(
        "camera.snapshot",
        permission=PermissionLevel.PASSIVE,
        params=SimSnapshotParams,
        state_changing=False,
        description="Capture one simulated still image into the session.",
        timeout_s=30.0,
    )
    async def camera_snapshot(
        self, ctx: ActionContext, params: SimSnapshotParams
    ) -> dict[str, Any]:
        if ctx.recorder is None:
            raise CaptureError(
                "a snapshot is attached to a session; start one first",
                preserved="no image was captured",
            )
        self._counter += 1
        path = ctx.recorder.capture_path("camera", params.label, ".png")
        path.write_bytes(_png(params.width, params.height, self._counter))
        artifact = ctx.recorder.add_artifact(
            path,
            kind="camera",
            media_type="image/png",
            device_id=self.device_id,
            raw=True,
            metadata={
                "width": params.width,
                "height": params.height,
                "note": params.note,
                "simulated": True,
                "uploaded": False,
            },
        )
        return {
            "artifact": artifact.model_dump(mode="json"),
            "sha256": artifact.sha256,
            "simulated": True,
            "note": "synthetic test pattern, not a photograph",
        }


def build_simulated_camera_devices() -> list[Driver]:
    return [SimCameraDriver()]
