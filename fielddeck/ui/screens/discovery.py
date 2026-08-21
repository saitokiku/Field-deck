"""What is attached, and nothing more.

Discovery is Stage A of the auto-detect engine: enumeration only.  Opening this
screen and pressing RESCAN reads the system's own inventory — udev, sysfs, the
network interface list — and transmits nothing to anything.  A panel that
probed a bus to fill in a row would be a panel that woke a DUT, so the tiles
here show only what enumeration can honestly report, including the awkward
parts: an unstable ``/dev/ttyUSB0``-derived id, a device that came up in FAULT,
a driver that loaded degraded.

Devices are paged as four large tiles rather than listed as rows.  A one-line
row is well under the touch minimum, and a list that needs a stylus is a list
that gets read wrong.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import ClassVar

from textual.containers import Grid, Horizontal
from textual.widget import Widget
from textual.widgets import Static

from fielddeck.common.models import DeviceDescriptor, DeviceRole, TransportKind
from fielddeck.ui.screens import PanelScreen
from fielddeck.ui.state import UiState
from fielddeck.ui.widgets import GLYPH_WARNING, device_glyph
from fielddeck.ui.widgets.status_bar import SUB_NAV
from fielddeck.ui.widgets.tiles import Tile, notice

__all__ = ["DiscoveryScreen"]

#: Which transports each home tile means.  ``None`` is "everything".
FILTERS: dict[str, tuple[TransportKind, ...]] = {
    "bus": (TransportKind.CAN, TransportKind.SERIAL, TransportKind.MODBUS),
    "logic": (TransportKind.LOGIC, TransportKind.I2C, TransportKind.SPI),
    "device": (TransportKind.PROBE, TransportKind.USB, TransportKind.GPIO),
    "bench": (TransportKind.VISA,),
}

PAGE_SIZE = 4


class DiscoveryScreen(PanelScreen):
    screen_name: ClassVar[str] = "discovery"
    hint: ClassVar[str] = "Tap a device to open it. RESCAN enumerates; it never transmits."
    NAV: ClassVar[tuple[tuple[str, str], ...]] = SUB_NAV

    def __init__(self) -> None:
        super().__init__()
        self.filter_key: str | None = None
        self._page = 0

    def set_filter(self, filter_key: str | None) -> None:
        """Point the list at one family of transports, from the home grid."""
        self.filter_key = filter_key
        self._page = 0

    def content(self) -> Iterable[Widget]:
        yield Static("", id="discovery-head")
        with Grid(id="discovery-grid"):
            for index in range(PAGE_SIZE):
                yield Tile(f"dev-{index}", "", classes="device-tile", id=f"device-{index}")
        with Horizontal(id="discovery-actions"):
            yield Tile("rescan", "RESCAN", "enumerate", classes="action-tile", id="disc-rescan")
            yield Tile("page", "PAGE", "next four", classes="action-tile", id="disc-page")
            yield Tile("all", "ALL", "clear filter", classes="action-tile", id="disc-all")
            yield Tile("detail", "DETAIL", "first device", classes="action-tile", id="disc-detail")

    # -- rendering ---------------------------------------------------------

    def visible_devices(self, state: UiState) -> tuple[DeviceDescriptor, ...]:
        kinds = FILTERS.get(self.filter_key or "", ())
        if not kinds:
            return state.devices
        return tuple(device for device in state.devices if device.kind in kinds)

    def render_state(self, state: UiState) -> None:
        devices = self.visible_devices(state)
        pages = max(1, math.ceil(len(devices) / PAGE_SIZE))
        self._page %= pages
        window = devices[self._page * PAGE_SIZE : self._page * PAGE_SIZE + PAGE_SIZE]
        self.query_one("#discovery-head", Static).update(
            f"DISCOVERY  filter {self.filter_key or 'all'}  "
            f"{len(devices)} device(s)  page {self._page + 1}/{pages}"
        )
        for index in range(PAGE_SIZE):
            tile = self.query_one(f"#device-{index}", Tile)
            if index < len(window):
                device = window[index]
                tile.disabled = False
                tile.set_text(
                    title=f"{device_glyph(device.state)} {_name(device)}",
                    subtitle=_subtitle(device),
                    status=_flags(device),
                )
            else:
                tile.disabled = True
                tile.set_text(title="", subtitle="", status="")

    # -- gestures ----------------------------------------------------------

    def tile_pressed(self, key: str) -> None:
        if key == "rescan":
            self.act(self.state.discover())
            return
        if key == "page":
            self._page += 1
            self.render_state(self.state)
            return
        if key == "all":
            self.filter_key = None
            self._page = 0
            self.render_state(self.state)
            return
        if key == "detail":
            self.run_worker(self._detail(0), exclusive=True, group="gesture")
            return
        if key.startswith("dev-"):
            self._open(int(key.removeprefix("dev-")))

    def _device_at(self, index: int) -> DeviceDescriptor | None:
        devices = self.visible_devices(self.state)
        position = self._page * PAGE_SIZE + index
        return devices[position] if position < len(devices) else None

    def _open(self, index: int) -> None:
        device = self._device_at(index)
        if device is None:
            return
        state = self.state
        state.select(device.id)
        target = {
            TransportKind.CAN: "can",
            TransportKind.SERIAL: "serial",
            TransportKind.MODBUS: "serial",
        }.get(device.kind)
        if target is None and DeviceRole.PSU in device.roles:
            target = "bench"
        if target is None:
            self.run_worker(self._detail(index), exclusive=True, group="gesture")
            return
        self.panel.drill(target)

    async def _detail(self, index: int) -> None:
        device = self._device_at(index)
        if device is None:
            await notice(self, "DEVICE", ["Nothing on this page."])
            return
        lines = [
            f"id        {device.id}",
            f"kind      {device.kind}   state {device.state}",
            f"path      {device.path or '-'}",
            f"vendor    {device.vendor or '-'}  product {device.product or '-'}",
            f"serial    {device.serial_number or '-'}",
            f"roles     {', '.join(str(role) for role in device.roles) or '-'}",
            f"caps      {', '.join(str(cap) for cap in device.capabilities) or '-'}",
            f"floor     {device.permission_floor}",
        ]
        if not device.stable_id:
            lines.append(
                f"{GLYPH_WARNING} id is derived from a name the kernel may reuse; "
                "set an alias before relying on it"
            )
        if device.warning:
            lines.append(f"{GLYPH_WARNING} {device.warning}")
        await notice(self, f"DEVICE {_name(device)}", lines)


def _name(device: DeviceDescriptor) -> str:
    return device.display_name[:28]


def _subtitle(device: DeviceDescriptor) -> str:
    roles = "/".join(str(role) for role in device.roles) or str(device.kind)
    return f"{roles} {device.path or device.id}"[:34]


def _flags(device: DeviceDescriptor) -> str:
    flags = []
    if device.simulated:
        flags.append("SIM")
    if not device.stable_id:
        flags.append(f"{GLYPH_WARNING}ID")
    if device.warning:
        flags.append(GLYPH_WARNING)
    return " ".join(flags)
