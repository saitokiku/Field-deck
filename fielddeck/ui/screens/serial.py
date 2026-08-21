"""Serial: watch bytes arrive, then say something back on purpose.

Receiving is PASSIVE and costs nothing but attention.  Transmitting is CONTROL,
because those bytes reach a real DUT — so SEND is the one control on this
screen with a second deliberate action in front of it, and the confirmation
shows the exact bytes, not the string that was typed.  ``55 AA`` typed into a
text field and ``55 AA`` typed into a hex field are different transmissions,
and the panel makes the operator look at which one it is about to perform.

The line view is a sampled window, like the CAN table: one second of monitoring
at a time, buffered locally for display and for the analysis tools.  What is
*preserved* is the capture, not this buffer — the annunciator says so whenever
a send or a capture happens.

Arrival timing shown under the lines is computed here from the timestamps the
daemon already returned, purely as a display aid.  The authoritative framing
and CRC analysis is ``tools.identify``, one tap away on ANALYZE, and it is the
one whose numbers belong in a report.
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from collections.abc import Iterable
from itertools import pairwise
from typing import Any, ClassVar

from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Input, Static

from fielddeck.common.models import PermissionLevel, TransportKind
from fielddeck.common.timebase import Timestamp
from fielddeck.ui.screens import PanelScreen
from fielddeck.ui.state import UiState, parse_payload
from fielddeck.ui.widgets import GLYPH_ACTIVE, GLYPH_IDLE, GLYPH_RX, GLYPH_UNKNOWN
from fielddeck.ui.widgets.status_bar import SUB_NAV
from fielddeck.ui.widgets.tiles import Tile, notice

__all__ = ["SerialScreen"]

#: One second of bytes per sample, repeated while this screen is in front.
SAMPLE_S = 1.0
SAMPLE_EVERY_S = 1.2

#: Chunks kept for the line view, and bytes handed to the analysis tools.
BUFFER_CHUNKS = 240
ANALYSIS_BYTES = 2048
VISIBLE_LINES = 8

VIEWS: tuple[str, ...] = ("ASCII", "HEX", "HEX+ASCII")


class SerialScreen(PanelScreen):
    screen_name: ClassVar[str] = "serial"
    hint: ClassVar[str] = "Receive is passive. SEND transmits to the DUT and needs CONTROL."
    NAV: ClassVar[tuple[tuple[str, str], ...]] = SUB_NAV

    def __init__(self) -> None:
        super().__init__()
        self._chunks: deque[tuple[int, bytes]] = deque(maxlen=BUFFER_CHUNKS)
        self._status: dict[str, Any] = {}
        self._view = "HEX+ASCII"
        self._analysis: str = ""
        self._error = ""

    def content(self) -> Iterable[Widget]:
        yield Static("", id="serial-head")
        yield Static("", id="serial-lines", markup=False)
        yield Static("", id="serial-analysis", markup=False)
        yield Input(placeholder="payload for SEND", id="serial-payload")
        with Horizontal(id="serial-actions"):
            yield Tile("view-ASCII", "ASCII", "text", classes="action-tile")
            yield Tile("view-HEX", "HEX", "bytes", classes="action-tile")
            yield Tile("view-HEX+ASCII", "BOTH", "hex+text", classes="action-tile")
            yield Tile("analyze", "ANALYZE", "framing/CRC", classes="action-tile")
            yield Tile("send", "SEND", "needs CONTROL", classes="action-tile")

    def on_mount(self) -> None:
        super().on_mount()
        self.set_interval(SAMPLE_EVERY_S, self._schedule_sample)
        self._schedule_sample()

    # -- sampling ----------------------------------------------------------

    def _schedule_sample(self) -> None:
        if self.app.screen is self:
            self.run_worker(self._sample(), exclusive=True, group="serial-poll")

    async def _sample(self) -> None:
        state = self.state
        device = state.device_for(TransportKind.SERIAL)
        if device is None:
            self._error = "no serial port; MENU then DISCOVER, or attach an adapter"
            return
        monitor = await state.run(
            "serial.monitor",
            {"device": device.id, "duration_s": SAMPLE_S},
            timeout_s=SAMPLE_S + 10.0,
            remember=False,
        )
        if not monitor.ok:
            self._error = monitor.summary()
            return
        self._error = ""
        self._status = monitor.data
        for chunk in monitor.data.get("chunks") or []:
            try:
                self._chunks.append((int(chunk["monotonic_ns"]), bytes.fromhex(chunk["hex"])))
            except (KeyError, TypeError, ValueError):
                continue

    # -- rendering ---------------------------------------------------------

    def render_state(self, state: UiState) -> None:
        device = state.device_for(TransportKind.SERIAL)
        name = device.display_name if device else "-"
        baud = self._status.get("baudrate") or (device.metadata.get("baudrate") if device else "?")
        framing = self._status.get("framing") or (device.metadata.get("framing") if device else "?")
        recording = state.session is not None
        received = sum(len(data) for _stamp, data in self._chunks)
        self.query_one("#serial-head", Static).update(
            f"SERIAL {name[:22]}  {baud} {framing}  VIEW {self._view}  "
            f"REC{GLYPH_ACTIVE if recording else GLYPH_IDLE}\n"
            f"{GLYPH_RX} buffered {received} B in {len(self._chunks)} frames   "
            f"CONTROL {'armed' if state.safety.armed(PermissionLevel.CONTROL) else 'not armed'}"
        )
        self.query_one("#serial-lines", Static).update(self._lines())
        self.query_one("#serial-analysis", Static).update(self._analysis or _timing(self._chunks))

    def _lines(self) -> str:
        if self._error:
            return self._error
        if not self._chunks:
            return "waiting for bytes..."
        now = Timestamp.now()
        rendered: list[str] = []
        for stamp, data in list(self._chunks)[-VISIBLE_LINES:]:
            clock = _clock(stamp, now)
            if self._view == "ASCII":
                rendered.append(f"{clock} {GLYPH_RX} {_ascii(data, 48)}")
            elif self._view == "HEX":
                rendered.append(f"{clock} {GLYPH_RX} {_hex(data, 16)}")
            else:
                rendered.append(f"{clock} {GLYPH_RX} {_hex(data, 8):<24} {_ascii(data, 8)}")
        return "\n".join(rendered)

    # -- gestures ----------------------------------------------------------

    def tile_pressed(self, key: str) -> None:
        if key.startswith("view-"):
            self._view = key.removeprefix("view-")
            self.query_one("#serial-payload", Input).placeholder = (
                "text to send" if self._view == "ASCII" else "hex bytes to send, e.g. 55 AA 04"
            )
            self.render_state(self.state)
            return
        if key == "analyze":
            self.run_worker(self._analyze(), exclusive=True, group="gesture")
            return
        if key == "send":
            self.run_worker(self._send(), exclusive=True, group="gesture")

    async def _analyze(self) -> None:
        """Passive classification of what has been received.  Nothing is sent."""
        data = b"".join(payload for _stamp, payload in self._chunks)[-ANALYSIS_BYTES:]
        if not data:
            self._analysis = "nothing captured yet to analyse"
            return
        outcome = await self.state.run(
            "tools.identify", {"hex": data.hex(), "limit": 3}, timeout_s=30.0
        )
        if not outcome.ok:
            self._analysis = outcome.summary()
            return
        best = outcome.data.get("best", "?")
        confidence = outcome.data.get("confidence", 0)
        suggestion = (outcome.data.get("recommended_next_test") or {}).get("test", "-")
        permission = (outcome.data.get("recommended_next_test") or {}).get("permission", "?")
        self._analysis = (
            f"Best hypothesis: {best} ({confidence}%)   {GLYPH_UNKNOWN} evidence, not proof\n"
            f"Next test ({permission}): {str(suggestion)[:56]}"
        )
        await notice(
            self,
            "FRAMING ANALYSIS",
            str(outcome.data.get("rendered") or "").splitlines()[:14]
            or ["no structure found in the captured bytes"],
        )

    async def _send(self) -> None:
        state = self.state
        device = state.device_for(TransportKind.SERIAL)
        if device is None:
            return
        text = self.query_one("#serial-payload", Input).value
        as_hex = self._view != "ASCII"
        payload, problem = parse_payload(text, as_hex=as_hex)
        if payload is None:
            self.query_one("#serial-analysis", Static).update(f"cannot send: {problem}")
            return
        confirmed = await self.ask(
            "TRANSMIT TO THE DUT",
            [
                f"port     {device.display_name}",
                f"bytes    {payload.hex(' ').upper()}",
                f"as       {'hex' if as_hex else 'text'}, {len(payload)} byte(s)",
                "",
                "This reaches the device under test and needs a CONTROL grant.",
            ],
            confirm_label="SEND",
        )
        if not confirmed:
            return
        await state.send_serial(device.id, text, as_hex=as_hex)


def _clock(monotonic_ns_value: int, now: Timestamp) -> str:
    """Wall clock for one arrival, derived from the shared monotonic clock."""
    utc_ns = now.utc_ns - (now.monotonic_ns - monotonic_ns_value)
    seconds, remainder = divmod(max(0, utc_ns), 1_000_000_000)
    return f"{time.strftime('%H:%M:%S', time.localtime(seconds))}.{remainder // 1_000_000:03d}"


def _hex(data: bytes, limit: int) -> str:
    shown = data[:limit].hex(" ").upper()
    return f"{shown}.." if len(data) > limit else shown


def _ascii(data: bytes, limit: int) -> str:
    return "".join(chr(byte) if 32 <= byte < 127 else "." for byte in data[:limit])


def _timing(chunks: Iterable[tuple[int, bytes]]) -> str:
    """Inter-arrival period of the buffered frames: a display aid, not evidence."""
    stamps = [stamp for stamp, _data in chunks]
    if len(stamps) < 3:
        return "Pattern: not enough frames yet    CRC: tap ANALYZE"
    deltas = [(later - earlier) / 1e6 for earlier, later in pairwise(stamps)]
    mean = statistics.fmean(deltas)
    spread = statistics.pstdev(deltas)
    return (
        f"Pattern: {mean:.1f} ms +/- {spread:.1f} ms over {len(deltas)} gaps (display aid)\n"
        f"CRC candidate: tap ANALYZE for tools.identify"
    )
