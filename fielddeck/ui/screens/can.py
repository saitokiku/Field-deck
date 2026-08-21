"""Live CAN, without ever putting a bit on the wire.

Everything this screen does is receive-only.  The interface is opened
listen-only, the ID table comes from ``can.stats`` (a PASSIVE action), and the
bitrate is never "detected" by transmitting — an autobaud probe that ACKs a
frame is a probe that has already joined somebody's vehicle bus.

The TX indicator is the safety-critical part of the layout and gets its own
band across the screen.  It reports two independent facts, because they fail
independently: what the *interface* is doing (listen-only or not) and whether a
CONTROL grant currently exists.  Both must be true before anything can
transmit, and this panel is not the thing that decides either of them — it
reports what ``instrumentd`` says and points at ``fdctl can send`` for the
transmit path, which is a deliberate act with its own confirmation.

Sampling, not streaming: the table refreshes from a one-second statistics
window every couple of seconds.  A 1 kframe/s bus pushed through Textual would
spend the Pi's CPU on layout passes, and an operator cannot read 1000 rows a
second anyway.  What is *recorded* is the capture file, not this table.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, ClassVar

from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Static

from fielddeck.common.models import PermissionLevel, TransportKind
from fielddeck.ui.screens import PanelScreen
from fielddeck.ui.state import UiState
from fielddeck.ui.widgets import GLYPH_ACTIVE, GLYPH_IDLE, GLYPH_RX, duration
from fielddeck.ui.widgets.status_bar import SUB_NAV
from fielddeck.ui.widgets.tiles import Tile, notice

__all__ = ["CanScreen"]

#: Seconds of bus time each statistics sample covers, and how often one runs.
#: Every action costs three timeline events; two seconds keeps the panel live
#: without burying a fault under HMI heartbeats.
SAMPLE_S = 1.0
SAMPLE_EVERY_S = 2.0

#: Arbitration IDs that fit the table.  A busier bus is a capture, not a panel.
MAX_ROWS = 8


class CanScreen(PanelScreen):
    screen_name: ClassVar[str] = "can"
    hint: ClassVar[str] = (
        "Listen-only. CAPTURE writes frames to the session; the table is a sample."
    )
    NAV: ClassVar[tuple[tuple[str, str], ...]] = SUB_NAV

    def __init__(self) -> None:
        super().__init__()
        self._status: dict[str, Any] = {}
        self._stats: dict[str, Any] = {}
        self._error: str = ""
        #: Held tables are for reading a transient by eye.  Nothing stops
        #: recording: FREEZE affects this view and only this view.
        self._frozen = False

    def content(self) -> Iterable[Widget]:
        yield Static("", id="can-head")
        yield Static("", id="can-table", markup=False)
        yield Static("", id="can-tx")
        with Horizontal(id="can-actions"):
            yield Tile("capture", "CAPTURE", "2 s to file", classes="action-tile", id="can-capture")
            yield Tile("sample", "SAMPLE", "refresh now", classes="action-tile", id="can-sample")
            yield Tile("stats", "STATS", "period/jitter", classes="action-tile", id="can-stats")
            yield Tile("freeze", "FREEZE", "hold the table", classes="action-tile", id="can-freeze")

    def on_mount(self) -> None:
        super().on_mount()
        self.set_interval(SAMPLE_EVERY_S, self._schedule_sample)
        self._schedule_sample()

    # -- sampling ----------------------------------------------------------

    def _schedule_sample(self) -> None:
        # Only the screen the operator is looking at is allowed to generate
        # bus traffic statistics; a backgrounded screen keeps its last picture.
        if self.app.screen is self and not self._frozen:
            self.run_worker(self._sample(), exclusive=True, group="can-poll")

    async def _sample(self) -> None:
        state = self.state
        device = state.device_for(TransportKind.CAN)
        if device is None:
            self._error = "no CAN interface; MENU then DISCOVER, or attach an adapter"
            return
        status = await state.run("can.status", {"device": device.id}, remember=False)
        if not status.ok:
            self._error = status.summary()
            return
        stats = await state.run(
            "can.stats",
            {"device": device.id, "duration_s": SAMPLE_S},
            timeout_s=SAMPLE_S + 10.0,
            remember=False,
        )
        self._status = status.data
        if stats.ok:
            self._stats = stats.data
            self._error = ""
        else:
            self._error = stats.summary()

    # -- rendering ---------------------------------------------------------

    def render_state(self, state: UiState) -> None:
        device = state.device_for(TransportKind.CAN)
        status, stats = self._status, self._stats
        interface = str(status.get("interface") or (device.display_name if device else "-"))
        bitrate = status.get("bitrate")
        mode = str(status.get("mode") or "?")
        recording = state.session is not None
        rx_rate = float(stats.get("total_frames") or 0) / max(
            float(stats.get("duration_s") or SAMPLE_S), 1e-9
        )
        self.query_one("#can-head", Static).update(
            f"CAN {interface}  {_bitrate(bitrate)}  {mode.upper()}  "
            f"REC{GLYPH_ACTIVE if recording else GLYPH_IDLE}\n"
            f"{GLYPH_RX} RX/s {rx_rate:<7.0f} ERR {status.get('bus_errors', 0):<5} "
            f"LOAD {stats.get('bus_load_percent', 0)}%   IDS {len(stats.get('ids') or [])}"
        )
        self.query_one("#can-table", Static).update(self._table())
        band = self.query_one("#can-tx", Static)
        band.update(_tx_band(state, mode, device.id if device else None))
        band.set_classes("unlocked" if not mode.lower().startswith("listen") else "")

    def _table(self) -> str:
        if self._error:
            return f"{self._error}\n\n(the last good sample is kept until it recovers)"
        rows = self._stats.get("ids") or []
        if not rows:
            return "no frames in the last sample window"
        lines = [f"{'ID':<8}{'Hz':>8}  {'DLC':>3}  LAST"]
        for row in rows[:MAX_ROWS]:
            payload = _payload(str(row.get("last") or ""))
            lines.append(
                f"{row.get('can_id', '?')!s:<8}{float(row.get('hz') or 0.0):>8.1f}  "
                f"{_dlc(payload):>3}  {payload}"
            )
        if len(rows) > MAX_ROWS:
            lines.append(f"... {len(rows) - MAX_ROWS} more ID(s); use fdctl can stats for all")
        return "\n".join(lines)

    # -- gestures ----------------------------------------------------------

    def tile_pressed(self, key: str) -> None:
        state = self.state
        device = state.device_for(TransportKind.CAN)
        if key == "freeze":
            self._frozen = not self._frozen
            self.query_one("#can-freeze", Tile).set_text(
                subtitle="held - tap to resume" if self._frozen else "hold the table"
            )
            return
        if key == "sample":
            self._frozen = False
            self.run_worker(self._sample(), exclusive=True, group="can-poll")
            return
        if key == "stats":
            self.run_worker(self._detail(), exclusive=True, group="gesture")
            return
        if device is None:
            return
        if key == "capture":
            self.act(
                state.run(
                    "can.capture",
                    {"device": device.id, "duration_s": 2.0, "label": "panel"},
                    timeout_s=30.0,
                )
            )

    async def _detail(self) -> None:
        """Period and jitter per ID: the numbers that identify a stuck node."""
        rows = self._stats.get("ids") or []
        lines = [f"{'ID':<8}{'count':>7}{'period ms':>11}{'jitter ms':>11}"]
        for row in rows[:MAX_ROWS]:
            lines.append(
                f"{row.get('can_id', '?')!s:<8}{row.get('count', 0):>7}"
                f"{row.get('period_ms', 0):>11}{row.get('jitter_ms', 0):>11}"
            )
        if not rows:
            lines.append("(no frames in the last sample)")
        lines.append("")
        lines.append(
            f"window {self._stats.get('duration_s', SAMPLE_S)} s, passive; nothing was sent"
        )
        await notice(self, "CAN TIMING", lines)


def _bitrate(value: object) -> str:
    if isinstance(value, int | float) and value:
        return f"{int(value) // 1000} kbit/s"
    return "bitrate ?"


def _payload(hex_text: str) -> str:
    data = hex_text.replace(" ", "")
    return " ".join(data[index : index + 2] for index in range(0, len(data), 2)).upper()


def _dlc(payload: str) -> int:
    return len(payload.split()) if payload else 0


def _tx_band(state: UiState, mode: str, device_id: str | None) -> str:
    """The TX band: what the interface is doing, and what is authorized.

    Deliberately two sentences.  "Listen-only" and "no CONTROL grant" are
    different facts with different fixes, and an operator who conflates them
    ends up arming the bench to solve a driver problem.
    """
    listening = mode.lower().startswith("listen")
    armed = state.safety.armed(PermissionLevel.CONTROL, device_id=device_id, action="can.send")
    left = "TX LOCKED" if listening else "TX UNLOCKED"
    interface = (
        "interface is listen-only; nothing here reaches the bus"
        if listening
        else "interface has left listen-only"
    )
    grant = (
        f"CONTROL armed {duration(state.safety.remaining_s(PermissionLevel.CONTROL))}"
        if armed
        else "CONTROL not armed"
    )
    return f"{left}   {interface}\n{grant}   transmit with: fdctl can send"
