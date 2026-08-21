"""The panel application: navigation, global keys, and the state lifetime.

Everything structural lives here and nowhere else — which screen is on top, how
BACK behaves, and the two keys that must work from any screen on any day
(emergency stop and quit).

Navigation is a stack with HOME as its floor.  ``go`` moves between peers and
never pushes a screen that is already open (Textual will not hold one instance
twice, and an operator who taps ARM four times should still be one BACK from
where they were).  ``drill`` is for genuine descent — a device chosen on the
discovery screen opens its bus screen *above* the list, so BACK returns to the
list rather than to the home grid.

The application starts and stays useful with ``instrumentd`` down.  ``UiState``
is created before anything connects, every screen renders from whatever it has,
and the chrome says the daemon is unreachable rather than the panel hanging on
a socket that is not there.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from textual.app import App
from textual.binding import Binding, BindingType
from textual.screen import Screen

from fielddeck.common.models import ClientSource
from fielddeck.common.timebase import Timestamp
from fielddeck.ui.screens import screen_map
from fielddeck.ui.state import UiState

__all__ = ["FieldDeckApp"]


class FieldDeckApp(App[None]):
    """The FieldDeck HMI."""

    CSS_PATH = "theme.tcss"
    TITLE = "FieldDeck"

    #: The command palette is a desktop-application idiom and a way to reach
    #: actions without the tiles that describe what they cost.  Off.
    ENABLE_COMMAND_PALETTE = False

    SCREENS: ClassVar[dict[str, Callable[[], Screen[Any]]]] = dict(screen_map())

    BINDINGS: ClassVar[list[BindingType]] = [
        # Reachable from every screen, including modals: stopping is never
        # gated, never confirmed and never more than one key away.
        Binding("ctrl+e", "estop", "E-STOP", show=True, priority=True),
        Binding("ctrl+q", "quit", "Quit", show=True, priority=True),
    ]

    def __init__(
        self,
        *,
        socket_path: Path | str | None = None,
        simulation_requested: bool = False,
    ) -> None:
        super().__init__()
        self.state = UiState(
            socket_path=socket_path,
            source=ClientSource.HMI,
            simulation_requested=simulation_requested,
        )

    # -- lifecycle ---------------------------------------------------------

    async def on_mount(self) -> None:
        await self.state.start()
        await self.push_screen("home")

    async def on_unmount(self) -> None:
        """Let go of the daemon cleanly.

        Not load-bearing for safety: an output lease belongs to this
        connection, so process exit drops it whatever happens here.  This just
        makes the daemon's log say the panel left rather than died.
        """
        await self.state.stop()

    # -- navigation --------------------------------------------------------

    def go(self, name: str) -> None:
        """Move to a peer screen, collapsing back to it if it is already open."""
        for index, screen in enumerate(self.screen_stack):
            if getattr(screen, "screen_name", None) == name:
                while len(self.screen_stack) > index + 1:
                    self.pop_screen()
                return
        if len(self.screen_stack) > 1:
            self.switch_screen(name)
        else:
            self.push_screen(name)

    def drill(self, name: str) -> None:
        """Open a screen *above* the current one, so BACK returns here."""
        if any(getattr(screen, "screen_name", None) == name for screen in self.screen_stack):
            self.go(name)
            return
        self.push_screen(name)

    def back(self) -> None:
        if len(self.screen_stack) > 1:
            self.pop_screen()

    def go_discovery(self, filter_key: str | None) -> None:
        """Open the device list showing one family of transports."""
        from fielddeck.ui.screens.discovery import DiscoveryScreen

        screen = self.get_screen("discovery", DiscoveryScreen)
        screen.set_filter(filter_key)
        self.drill("discovery")

    # -- global actions ----------------------------------------------------

    def action_estop(self) -> None:
        """Stop everything, from wherever the operator happens to be."""
        self.run_worker(self.state.estop("ESTOP key at the panel"), exclusive=False)
        self.go("safety")

    def default_session_name(self) -> str:
        """A name that sorts, without asking anyone to type at 2am."""
        stamp = Timestamp.now().utc.strftime("%Y%m%d-%H%M")
        return f"panel-{stamp}"
