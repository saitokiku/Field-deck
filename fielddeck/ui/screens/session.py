"""Sessions: the thing that turns a bench afternoon into evidence.

Recording is one tap, and so is a mark, because the moment an engineer sees
something strange is the moment they have the least attention to spare.  MARK
is the highest-value control on this screen: it stamps the timeline with a
monotonic instant that ``session.window`` can later be asked about — "what
happened 300 ms before that?" — and it costs nothing to press.

The event list is the live tail of that timeline, newest first.  It is a view
of the daemon's records, not a copy: the panel holds a couple of hundred events
for display, while the session directory holds all of them.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Input, Static

from fielddeck.common.timebase import format_utc_ns
from fielddeck.ui.screens import PanelScreen
from fielddeck.ui.state import UiState
from fielddeck.ui.widgets import GLYPH_ACTIVE, GLYPH_IDLE, duration
from fielddeck.ui.widgets.status_bar import SUB_NAV
from fielddeck.ui.widgets.tiles import Tile, notice

__all__ = ["SessionScreen"]

VISIBLE_EVENTS = 8


class SessionScreen(PanelScreen):
    screen_name: ClassVar[str] = "session"
    hint: ClassVar[str] = "MARK stamps the timeline. Nothing here needs authorization."
    NAV: ClassVar[tuple[tuple[str, str], ...]] = SUB_NAV

    def content(self) -> Iterable[Widget]:
        yield Static("", id="session-head")
        yield Static("", id="session-events", markup=False)
        yield Input(placeholder="name for START, text for NOTE", id="session-text")
        with Horizontal(id="session-actions"):
            yield Tile("toggle", "START", "", classes="action-tile", id="session-toggle")
            yield Tile("mark", "MARK", "stamp now", classes="action-tile", id="session-mark")
            yield Tile("note", "NOTE", "from the field", classes="action-tile", id="session-note")
            yield Tile(
                "summary",
                "SUMMARY",
                "what was recorded",
                classes="action-tile",
                id="session-summary",
            )

    # -- rendering ---------------------------------------------------------

    def render_state(self, state: UiState) -> None:
        session = state.session
        if session is None:
            head = (
                f"SESSION {GLYPH_IDLE} not recording\n"
                "Captures still run without a session, but nothing is written to disk\n"
                "and no timeline is kept. START one before the interesting part."
            )
        else:
            head = (
                f"SESSION {GLYPH_ACTIVE} {session.name}\n"
                f"id {session.id}   elapsed {duration(session.elapsed_s)}   "
                f"{'recording' if session.recording else 'idle'}\n"
                f"store {state.system.sessions_dir if state.system else '?'}"
            )
        self.query_one("#session-head", Static).update(head)
        self.query_one("#session-toggle", Tile).set_text(
            title="STOP" if session else "START",
            subtitle="finalise artifacts" if session else "begin recording",
        )
        self.query_one("#session-events", Static).update(self._events(state))

    def _events(self, state: UiState) -> str:
        events = state.recent_events(VISIBLE_EVENTS)
        if not events:
            return "no events yet"
        lines = []
        for event in events:
            stamp = format_utc_ns(event.utc_ns)[11:23]
            lines.append(f"{stamp} {event.type!s:<18}{(event.message or '')[:44]}")
        return "\n".join(lines)

    # -- gestures ----------------------------------------------------------

    def tile_pressed(self, key: str) -> None:
        state = self.state
        text = self.query_one("#session-text", Input).value.strip()
        if key == "toggle":
            if state.session is not None:
                self.act(state.stop_session())
            else:
                self.act(state.start_session(text or self.panel.default_session_name()))
        elif key == "mark":
            self.act(state.mark(text or "mark", note="marked at the panel"))
        elif key == "note":
            if not text:
                self.query_one("#session-events", Static).update(
                    "type the note in the field first, then tap NOTE"
                )
                return
            self.act(state.note(text))
        elif key == "summary":
            self.run_worker(self._summary(), exclusive=True, group="gesture")

    async def _summary(self) -> None:
        outcome = await self.state.run("session.summary", {})
        if not outcome.ok:
            await notice(self, "SESSION SUMMARY", [outcome.summary()])
            return
        data = outcome.data
        counts = data.get("event_counts") or data.get("counts") or {}
        lines = [
            f"session   {data.get('name', '?')}  ({data.get('id', '?')})",
            f"elapsed   {duration(float(data.get('elapsed_s') or 0))}",
            f"artifacts {len(data.get('artifacts') or [])}",
            f"marks     {len(data.get('marks') or [])}",
            "",
            *[f"  {name!s:<24}{count}" for name, count in list(counts.items())[:8]],
        ]
        await notice(self, "SESSION SUMMARY", lines)
