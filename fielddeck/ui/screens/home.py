"""The home screen: six blocks, and the answer to "what is plugged in?".

This is the screen the panel boots into and the one an operator returns to
between tasks, so it earns its space by answering two questions without a tap:
which families of hardware are present, and what the bench is doing right now.
Each tile carries its own live count, because a BUS tile that says ``2 ✓`` has
already told the operator that the CAN adapter came back after they re-seated
it.

The grid is the SPEC section 30 layout: three across, two down, every tile far
larger than the 90x45 pixel touch minimum.  BUS, LOGIC and DEVICE open the
discovery list filtered to their transports rather than guessing which of two
adapters was meant.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from textual.containers import Grid
from textual.widget import Widget
from textual.widgets import Static

from fielddeck.common.models import DeviceDescriptor, DeviceRole, TransportKind
from fielddeck.ui.screens import PanelScreen
from fielddeck.ui.state import UiState
from fielddeck.ui.widgets import GLYPH_ACTIVE, GLYPH_IDLE, GLYPH_OK, GLYPH_UNKNOWN, duration
from fielddeck.ui.widgets.tiles import Tile, notice

__all__ = ["HomeScreen"]

_BUS_KINDS = (TransportKind.CAN, TransportKind.SERIAL, TransportKind.MODBUS)
_LOGIC_KINDS = (TransportKind.LOGIC, TransportKind.I2C, TransportKind.SPI)
_DEVICE_KINDS = (TransportKind.PROBE, TransportKind.USB, TransportKind.GPIO)
_BENCH_ROLES = (DeviceRole.PSU, DeviceRole.DMM, DeviceRole.SCOPE, DeviceRole.LOAD)

_TILES: tuple[tuple[str, str, str], ...] = (
    ("bus", "BUS", "CAN 485 UART"),
    # PSU, DMM and LOAD are what the shipped profiles actually cover. A tile
    # advertising SCOPE sends an operator into a screen that can only offer
    # raw scpi.query, which is a worse experience than the tile not claiming it.
    ("bench", "BENCH", "PSU DMM LOAD"),
    ("logic", "LOGIC", "SPI I2C LA"),
    ("device", "DEVICE", "SWD FLASH USB"),
    ("tools", "TOOLS", "CRC HEX CONV"),
    ("assistant", "ASSISTANT", "CLAUDE / ASK"),
)


class HomeScreen(PanelScreen):
    screen_name: ClassVar[str] = "home"
    hint: ClassVar[str] = "Tap a block. Keys: h home  s session  a arm  r rec  m menu"

    def content(self) -> Iterable[Widget]:
        with Grid(id="home-grid"):
            for key, title, subtitle in _TILES:
                yield Tile(key, title, subtitle, classes="home-tile", id=f"home-{key}")
        yield Static("", id="home-summary")

    def render_state(self, state: UiState) -> None:
        self.query_one("#home-bus", Tile).set_text(status=_count(state, kinds=_BUS_KINDS))
        self.query_one("#home-bench", Tile).set_text(status=_bench_status(state))
        self.query_one("#home-logic", Tile).set_text(status=_count(state, kinds=_LOGIC_KINDS))
        self.query_one("#home-device", Tile).set_text(status=_count(state, kinds=_DEVICE_KINDS))
        self.query_one("#home-tools", Tile).set_text(status=GLYPH_OK)
        self.query_one("#home-assistant", Tile).set_text(status=GLYPH_IDLE)
        self.query_one("#home-summary", Static).update(_summary(state))

    def tile_pressed(self, key: str) -> None:
        app = self.panel
        if key == "bus":
            app.go_discovery("bus")
        elif key == "logic":
            app.go_discovery("logic")
        elif key == "device":
            app.go_discovery("device")
        elif key in {"bench", "tools"}:
            app.go(key)
        elif key == "assistant":
            self.run_worker(self._assistant(), exclusive=True, group="gesture")

    async def _assistant(self) -> None:
        """Short-form only, by design.

        CLAUDE.md section 23 puts long conversations in the CLAUDE tmux window,
        not here: an assistant that can fill the panel is an assistant that can
        push the safety chrome off it.
        """
        from fielddeck.common.events import EventType

        observations = self.state.recent_events(4, types=[EventType.ASSISTANT_OBSERVATION])
        lines = [event.message or "(no text)" for event in observations] or [
            "No assistant observations on this session yet.",
        ]
        await notice(
            self,
            "ASSISTANT",
            [
                *lines,
                "",
                "Ask questions in the CLAUDE tmux window; the assistant reads the",
                "same timeline this panel shows and can never arm the bench.",
            ],
        )


def _count(state: UiState, *, kinds: tuple[TransportKind, ...]) -> str:
    devices = state.devices_of(*kinds)
    if not devices:
        return GLYPH_UNKNOWN
    ready = sum(1 for device in devices if str(device.state) == "READY")
    return f"{len(devices)} {GLYPH_OK if ready else GLYPH_IDLE}"


def _bench_status(state: UiState) -> str:
    devices = [
        device for device in state.devices if any(role in device.roles for role in _BENCH_ROLES)
    ]
    if not devices:
        return GLYPH_UNKNOWN
    live = GLYPH_ACTIVE if state.safety.leases else GLYPH_IDLE
    return f"{len(devices)} {live}"


def _summary(state: UiState) -> str:
    """The one-line bench summary from the SPEC mockup, filled from real state."""
    parts: list[str] = []
    can = state.device_for(TransportKind.CAN)
    if can is not None:
        bitrate = can.metadata.get("bitrate")
        rate = f"{int(bitrate) // 1000}k" if isinstance(bitrate, int | float) else "?"
        parts.append(f"{_short(can)} {rate}")
    serial = state.device_for(TransportKind.SERIAL)
    if serial is not None:
        parts.append(f"{_short(serial)} {serial.metadata.get('baudrate', '?')}")
    psu = state.device_for(role=DeviceRole.PSU)
    if psu is not None:
        parts.append(f"PSU {_short(psu)}")
    if not parts:
        parts.append("no interfaces - MENU then DISCOVER")
    session = state.session
    session_text = (
        f"SESSION {session.name} {duration(session.elapsed_s)}" if session else "SESSION --"
    )
    return f"{' | '.join(parts)}\n{session_text}"


def _short(device: DeviceDescriptor) -> str:
    """A device's shortest honest name: the interface, not the whole path."""
    name = device.path or device.product or device.display_name
    return name.rsplit("/", 1)[-1].rsplit("#", 1)[-1][:14]
