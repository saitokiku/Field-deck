"""Capture-subsystem actions that are not tied to one already-known device.

Scanning for logic analyzers spawns ``sigrok-cli``, which probes USB; that is
too heavy to run on the discovery timer, so it lives behind an explicit
action the operator (or a client) calls when they actually want it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fielddeck.common.errors import SessionError, UnsupportedCapability
from fielddeck.common.models import PermissionLevel, StrictModel
from fielddeck.discovery.linux import list_video_devices
from fielddeck.drivers.base import ActionContext, ActionSpec, NoParams, action, collect_actions

if TYPE_CHECKING:  # pragma: no cover
    from fielddeck.daemon.service import InstrumentDaemon

__all__ = ["build_action_specs"]


class ReportParams(StrictModel):
    session_id: str | None = None
    format: str = "markdown"
    save: bool = True


class CaptureActions:
    def __init__(self, daemon: InstrumentDaemon) -> None:
        self.daemon = daemon

    @action(
        "session.report",
        permission=PermissionLevel.PASSIVE,
        params=ReportParams,
        state_changing=False,
        description="Build a deterministic report for a session.",
        allowed_during_estop=True,
        timeout_s=120.0,
    )
    async def session_report(self, ctx: ActionContext, params: ReportParams) -> dict[str, Any]:
        """Facts and AI interpretation are rendered in separate sections."""
        import asyncio

        from fielddeck.capture.report import build_report, render_markdown

        session_id = params.session_id or self.daemon.sessions.current_id
        if session_id is None:
            raise SessionError("no active session and no session_id given")
        # Flush first so a report on the live session includes everything
        # recorded up to this instant.
        if self.daemon.sessions.recorder is not None:
            self.daemon.sessions.recorder.flush()
        session_dir = self.daemon.sessions.sessions_dir / session_id

        report = await asyncio.to_thread(build_report, session_dir)
        if params.format not in {"markdown", "json"}:
            raise SessionError(
                f"unknown report format {params.format!r}",
                details={"known": ["markdown", "json"]},
            )
        rendered = render_markdown(report) if params.format == "markdown" else None

        saved: str | None = None
        if params.save and rendered is not None:
            path = session_dir / "reports" / "session-report.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_text, rendered, "utf-8")
            saved = str(path.relative_to(session_dir))
            if (
                self.daemon.sessions.recorder is not None
                and self.daemon.sessions.current_id == session_id
            ):
                self.daemon.sessions.recorder.add_artifact(
                    path,
                    kind="reports",
                    media_type="text/markdown",
                    raw=False,
                    producer="fielddeck.capture.report",
                    producer_version="1",
                )
        return {
            "session_id": session_id,
            "format": params.format,
            "markdown": rendered,
            "report": report if params.format == "json" else None,
            "saved_to": saved,
        }

    @action(
        "logic.devices",
        permission=PermissionLevel.PASSIVE,
        params=NoParams,
        state_changing=False,
        description="Scan for sigrok-supported logic analyzers and register them.",
        allowed_during_estop=True,
        timeout_s=45.0,
    )
    async def logic_devices(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
        from fielddeck.capture.sigrok import build_drivers_from_scan, scan_logic_devices

        try:
            found = await scan_logic_devices()
        except UnsupportedCapability as exc:
            return {"devices": [], "available": False, "reason": exc.message}

        added: list[str] = []
        for driver in build_drivers_from_scan(found):
            if ctx.registry.get(driver.device_id) is None:
                ctx.registry.add(driver)
                added.append(driver.device_id)
        return {"devices": found, "count": len(found), "registered": added, "available": True}

    @action(
        "camera.list",
        permission=PermissionLevel.PASSIVE,
        params=NoParams,
        state_changing=False,
        description="V4L2 capture devices present on this system.",
        allowed_during_estop=True,
    )
    async def camera_list(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
        devices = list_video_devices()
        return {
            "cameras": devices,
            "count": len(devices),
            "auto_upload": False,
        }

    @action(
        "system.inventory",
        permission=PermissionLevel.PASSIVE,
        params=NoParams,
        state_changing=False,
        description="Raw passive inventory of buses and interfaces, driver or not.",
        allowed_during_estop=True,
        timeout_s=20.0,
    )
    async def system_inventory(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
        """Shows hardware FieldDeck can see even where it has no driver yet."""
        from fielddeck.discovery import inventory

        return inventory()


def build_action_specs(daemon: InstrumentDaemon) -> dict[str, ActionSpec]:
    return collect_actions(CaptureActions(daemon))
