"""USB camera evidence.

A photograph of the connector orientation before you unplugged it is worth
more than a paragraph describing it from memory.  Snapshots are attached to
the session, hashed like every other artifact, and stored **locally**.

Nothing here uploads anything, ever.  If an image is to be analysed by an AI
service the operator invokes that path explicitly; there is no automatic
route from a camera on a bench to a network request.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field

from fielddeck.common.config import FieldDeckConfig
from fielddeck.common.errors import CaptureError, UnsupportedCapability
from fielddeck.common.models import (
    ConnectionState,
    DeviceCapability,
    DeviceDescriptor,
    DeviceRole,
    PermissionLevel,
    TransportKind,
)
from fielddeck.common.process import have_tool, run_tool, tool_version
from fielddeck.discovery.linux import list_video_devices
from fielddeck.drivers.base import ActionContext, DeviceParams, Driver, action

__all__ = ["CameraDriver", "discover_camera_drivers"]


class SnapshotParams(DeviceParams):
    label: str = Field(default="snapshot", max_length=64)
    width: int = Field(default=1280, ge=64, le=8192)
    height: int = Field(default=720, ge=64, le=8192)
    note: str | None = None


def _capture_backend() -> tuple[str, str] | None:
    """Pick whichever still-capture tool this system actually has."""
    for tool in ("ffmpeg", "fswebcam", "v4l2-ctl"):
        if have_tool(tool):
            return tool, tool
    return None


class CameraDriver(Driver):
    """A V4L2 capture device."""

    kind = TransportKind.CAMERA

    def __init__(self, *, path: str, name: str | None, index: int | None) -> None:
        descriptor = DeviceDescriptor(
            id=f"camera:v4l2:{Path(path).name}",
            kind=TransportKind.CAMERA,
            display_name=name or f"Camera {path}",
            path=path,
            product=name,
            roles=[DeviceRole.CAMERA],
            capabilities=[DeviceCapability.SNAPSHOT],
            state=ConnectionState.DISCOVERED,
            # /dev/video2 is not a stable identity across reboots, and V4L2
            # exposes no serial number, so say so rather than pretending.
            stable_id=False,
            metadata={"index": index, "auto_upload": False},
        )
        super().__init__(descriptor)
        self.path = path

    async def status(self) -> dict[str, Any]:
        backend = _capture_backend()
        return {
            "path": self.path,
            "state": str(self._descriptor.state),
            "backend": backend[0] if backend else None,
            "backend_version": await tool_version(backend[0]) if backend else None,
            "auto_upload": False,
            "note": "images are stored in the session and never uploaded automatically",
        }

    @action(
        "camera.status",
        permission=PermissionLevel.PASSIVE,
        params=DeviceParams,
        state_changing=False,
        description="Camera availability and capture backend.",
        allowed_during_estop=True,
        timeout_s=15.0,
    )
    async def camera_status(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        return await self.status()

    @action(
        "camera.snapshot",
        permission=PermissionLevel.PASSIVE,
        params=SnapshotParams,
        state_changing=False,
        description="Capture one still image into the current session.",
        timeout_s=30.0,
    )
    async def camera_snapshot(self, ctx: ActionContext, params: SnapshotParams) -> dict[str, Any]:
        """PASSIVE: photographing a DUT does not touch it electrically."""
        if ctx.recorder is None:
            raise CaptureError(
                "a snapshot is attached to a session; start one first with: "
                'fdctl session start "<name>"',
                preserved="no image was captured",
            )
        backend = _capture_backend()
        if backend is None:
            raise UnsupportedCapability(
                "no still-capture tool found; install one with: sudo apt install ffmpeg",
                details={"tried": ["ffmpeg", "fswebcam", "v4l2-ctl"]},
            )

        tool = backend[0]
        out = ctx.recorder.capture_path("camera", params.label, ".jpg")
        size = f"{params.width}x{params.height}"
        if tool == "ffmpeg":
            args = [
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "v4l2",
                "-video_size",
                size,
                "-i",
                self.path,
                "-frames:v",
                "1",
                str(out),
            ]
        elif tool == "fswebcam":
            args = ["-d", self.path, "-r", size, "--no-banner", str(out)]
        else:
            args = [
                "-d",
                self.path,
                "--set-fmt-video",
                f"width={params.width},height={params.height},pixelformat=MJPG",
                "--stream-mmap",
                "--stream-count=1",
                f"--stream-to={out}",
            ]

        result = await run_tool(tool, args, timeout_s=25.0, allowed_path_roots=[ctx.recorder.root])
        result.check(what=f"{tool} snapshot")
        if not out.exists() or out.stat().st_size == 0:
            raise CaptureError(
                f"{tool} produced no image",
                details={"command": result.command_line, "stderr": result.stderr[-800:]},
                preserved="the session is unchanged",
            )

        artifact = ctx.recorder.add_artifact(
            out,
            kind="camera",
            media_type="image/jpeg",
            device_id=self.device_id,
            raw=True,
            metadata={
                "width": params.width,
                "height": params.height,
                "note": params.note,
                "backend": tool,
                "uploaded": False,
            },
        )
        return {
            "artifact": artifact.model_dump(mode="json"),
            "path": str(out),
            "sha256": artifact.sha256,
            "note": (
                "stored locally; analysis by an AI service requires an explicit operator action"
            ),
        }


def discover_camera_drivers(config: FieldDeckConfig) -> list[Driver]:
    if not config.camera.enabled:
        return []
    return [
        CameraDriver(path=entry["path"], name=entry.get("name"), index=entry.get("index"))
        for entry in list_video_devices()
    ]
