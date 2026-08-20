"""Capture-subsystem actions that are not tied to one already-known device.

Scanning for logic analyzers spawns ``sigrok-cli``, which probes USB; that is
too heavy to run on the discovery timer, so it lives behind an explicit
action the operator (or a client) calls when they actually want it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fielddeck.common.errors import UnsupportedCapability
from fielddeck.common.models import PermissionLevel
from fielddeck.discovery.linux import list_video_devices
from fielddeck.drivers.base import ActionContext, ActionSpec, NoParams, action, collect_actions

if TYPE_CHECKING:  # pragma: no cover
    from fielddeck.daemon.service import InstrumentDaemon

__all__ = ["build_action_specs"]


class CaptureActions:
    def __init__(self, daemon: InstrumentDaemon) -> None:
        self.daemon = daemon

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
