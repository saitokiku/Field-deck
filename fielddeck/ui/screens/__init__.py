"""The screen family, and the frame every one of them lives in.

:class:`PanelScreen` owns the parts that must be identical everywhere: the
two-row safety chrome at the top, the five permanent navigation tiles at the
bottom, and the single annunciator line between them where the daemon's answer
to the last gesture is printed.  A subclass supplies the twenty-column-wide,
nineteen-row-tall middle and nothing else.

Two rules are enforced here rather than trusted to each screen:

*Gestures are serialised.*  Every operator action runs in one exclusive worker
group, so a screen cannot queue up four output-enable requests because the
daemon took a moment to answer the first.

*Refusals are never swallowed.*  Anything a screen asks for comes back as an
:class:`~fielddeck.ui.state.Outcome`, and the annunciator prints it verbatim,
including the ``preserved`` clause.  The panel's job in a refusal is to be a
faithful messenger, not an interpreter.

The concrete screens are imported lazily by :func:`screen_map` so this module
can be imported by any of them without a cycle.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Static

from fielddeck.ui.state import Outcome, UiState
from fielddeck.ui.widgets import GLYPH_FAULT, GLYPH_OK, GLYPH_WARNING
from fielddeck.ui.widgets.status_bar import HOME_NAV, NavBar, StatusBar
from fielddeck.ui.widgets.tiles import Tile, confirm

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fielddeck.ui.app import FieldDeckApp

__all__ = ["PanelScreen", "screen_map"]

#: Repaints per second.  Fast enough that a countdown looks live, slow enough
#: that a busy bus cannot turn the panel into the most expensive thing on the
#: Pi.  Live data is *sampled* at this rate; it is never pushed frame by frame.
REFRESH_HZ = 10


class PanelScreen(Screen[None]):
    """Chrome, navigation and the annunciator.  Subclasses fill the middle."""

    #: Stable name used for navigation.  Must match the key in :func:`screen_map`.
    screen_name: ClassVar[str] = "panel"
    #: Printed on the annunciator when there is nothing more urgent to say.
    hint: ClassVar[str] = ""
    #: The five navigation tiles this screen shows.  Sub-screens use
    #: :data:`~fielddeck.ui.widgets.status_bar.SUB_NAV`.
    NAV: ClassVar[tuple[tuple[str, str], ...]] = HOME_NAV

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "back", "Back", show=False),
        Binding("h", "nav('home')", "Home", show=False),
        Binding("s", "nav('session')", "Session", show=False),
        Binding("a", "nav('arm')", "Arm", show=False),
        Binding("r", "nav('rec')", "Record", show=False),
        Binding("m", "nav('menu')", "Menu", show=False),
        Binding("k", "nav('mark')", "Mark", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield StatusBar()
        yield NavBar(self.NAV)
        yield Static("", id="annunciator")
        with Vertical(id="body"):
            yield from self.content()

    def content(self) -> Iterable[Widget]:
        """The middle of the screen.  Override in a subclass."""
        return ()

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> UiState:
        app: FieldDeckApp = self.app  # type: ignore[assignment]
        return app.state

    def on_mount(self) -> None:
        self.set_interval(1 / REFRESH_HZ, self._tick)
        self._tick()

    def _tick(self) -> None:
        state = self.state
        self.query_one(StatusBar).refresh_from(state)
        self.query_one(NavBar).refresh_from(state)
        self.query_one("#annunciator", Static).update(self._annunciator(state))
        self.render_state(state)

    def render_state(self, state: UiState) -> None:
        """Redraw the middle from a state snapshot.  Called at :data:`REFRESH_HZ`."""

    def _annunciator(self, state: UiState) -> str:
        outcome = state.last_outcome
        if outcome is None:
            return self.hint
        glyph = GLYPH_OK if outcome.ok else (GLYPH_WARNING if outcome.refused else GLYPH_FAULT)
        return f"{glyph} {outcome.summary()}"

    # -- gestures ----------------------------------------------------------

    def act(self, work: Awaitable[Outcome | None]) -> None:
        """Run one operator gesture.

        Exclusive: while the daemon is answering, a second tap on the same
        screen is dropped rather than queued.  On an instrument, a queued
        command is a command that runs after the operator stopped expecting it.
        """
        self.run_worker(work, exclusive=True, group="gesture")

    async def ask(self, title: str, lines: list[str], *, confirm_label: str = "CONFIRM") -> bool:
        return await confirm(self, title, lines, confirm_label=confirm_label)

    def on_tile_pressed(self, event: Tile.Pressed) -> None:
        if any(event.key == key for key, _label in self.NAV):
            event.stop()
            self.action_nav(event.key)
            return
        self.tile_pressed(event.key)

    def tile_pressed(self, key: str) -> None:
        """A tile that is not part of the chrome was tapped."""

    # -- navigation --------------------------------------------------------

    def action_nav(self, key: str) -> None:
        app: FieldDeckApp = self.app  # type: ignore[assignment]
        if key == "rec":
            self.act(app.state.toggle_recording(name=app.default_session_name()))
            return
        if key == "mark":
            self.act(
                app.state.mark(self.screen_name, note=f"marked on the {self.screen_name} panel")
            )
            return
        if key == "back":
            app.back()
            return
        target = {"home": "home", "session": "session", "arm": "safety", "menu": "system"}.get(key)
        if target is not None:
            app.go(target)

    def action_back(self) -> None:
        app: FieldDeckApp = self.app  # type: ignore[assignment]
        app.back()


def screen_map() -> dict[str, Callable[[], PanelScreen]]:
    """Name to factory, imported here so screens can import this module."""
    from fielddeck.ui.screens.bench import BenchScreen
    from fielddeck.ui.screens.can import CanScreen
    from fielddeck.ui.screens.discovery import DiscoveryScreen
    from fielddeck.ui.screens.home import HomeScreen
    from fielddeck.ui.screens.safety import SafetyScreen
    from fielddeck.ui.screens.serial import SerialScreen
    from fielddeck.ui.screens.session import SessionScreen
    from fielddeck.ui.screens.system import SystemScreen
    from fielddeck.ui.screens.tools import ToolsScreen

    return {
        "home": HomeScreen,
        "discovery": DiscoveryScreen,
        "session": SessionScreen,
        "can": CanScreen,
        "serial": SerialScreen,
        "tools": ToolsScreen,
        "safety": SafetyScreen,
        "system": SystemScreen,
        "bench": BenchScreen,
    }
