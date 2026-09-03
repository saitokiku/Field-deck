"""The chrome that never goes away.

Two rows at the top and one row of navigation at the bottom, present on every
screen, because the four things an operator must never have to go looking for
are: what is armed, whether anything is recording, whether something has
faulted, and which session they are in.

The second chrome row is a priority ladder, not a list.  It shows exactly one
thing, and the order is fixed: emergency stop, then a lost daemon, then the
live arm countdowns, then an unacknowledged fault, then the reassuring case.
An ESTOP banner is drawn in reverse video across the full width so it survives
a monochrome panel, a washed-out screen and a photograph of the bench.

The navigation row is five tiles of sixteen columns, so every one of them
clears the 90x45 pixel touch minimum on a 480x320 panel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from fielddeck.common.timebase import monotonic_ns
from fielddeck.ui.widgets import (
    GLYPH_ACTIVE,
    GLYPH_FAULT,
    GLYPH_IDLE,
    GLYPH_OK,
    GLYPH_UNKNOWN,
    GLYPH_WARNING,
    duration,
)
from fielddeck.ui.widgets.tiles import Tile

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fielddeck.ui.state import UiState

__all__ = ["HOME_NAV", "SUB_NAV", "NavBar", "StatusBar"]

#: The five tiles on the home screen.
HOME_NAV: tuple[tuple[str, str], ...] = (
    ("home", "HOME"),
    ("session", "SESSION"),
    ("arm", "ARM"),
    ("rec", "REC"),
    ("menu", "MENU"),
)

#: Sub-screens trade HOME and SESSION for BACK and MARK.  ARM, REC and MENU
#: never move: an operator reaching for the emergency-adjacent controls must
#: find them in the same place on every screen.
SUB_NAV: tuple[tuple[str, str], ...] = (
    ("back", "BACK"),
    ("mark", "MARK"),
    ("arm", "ARM"),
    ("rec", "REC"),
    ("menu", "MENU"),
)


class StatusBar(Vertical):
    """Safety, recording, fault and session — on screen at all times."""

    def __init__(self) -> None:
        super().__init__(id="status-bar")
        self._revision = -1

    def compose(self) -> ComposeResult:
        yield Static("", id="chrome-top")
        yield Static("", id="chrome-alert")

    def refresh_from(self, state: UiState, *, force: bool = False) -> None:
        """Redraw from a state snapshot.

        Skipped when nothing has changed, so the 10 Hz repaint timer costs
        nothing while the bench is idle — except while a countdown is running,
        where the whole point is that the number moves.
        """
        counting = bool(state.safety.active_grants())
        if not force and not counting and state.revision == self._revision:
            return
        self._revision = state.revision
        self.query_one("#chrome-top", Static).update(_top_line(state))
        alert = self.query_one("#chrome-alert", Static)
        text, css_class = _alert_line(state)
        alert.update(text)
        alert.set_classes(css_class)


class NavBar(Horizontal):
    """Five permanent tiles, sixteen columns each, on every screen."""

    def __init__(self, items: tuple[tuple[str, str], ...] = HOME_NAV) -> None:
        super().__init__(id="nav-bar")
        self._items = items

    def compose(self) -> ComposeResult:
        for key, label in self._items:
            yield Tile(key, label, classes="nav-tile", id=f"nav-{key}")

    def refresh_from(self, state: UiState) -> None:
        """Keep the two stateful tiles honest: REC and ARM both count."""
        recording = state.session is not None
        for tile in self.query("#nav-rec").results(Tile):
            tile.set_text(title=f"REC {GLYPH_ACTIVE if recording else GLYPH_IDLE}")
        armed = state.safety.armed_classes()
        if state.safety.estop_active:
            arm_label = "ESTOP"
        elif armed:
            arm_label = f"ARM {duration(state.safety.remaining_s(armed[-1]))}"
        else:
            arm_label = "ARM"
        for tile in self.query("#nav-arm").results(Tile):
            tile.set_text(title=arm_label)


def _top_line(state: UiState) -> str:
    safety = state.safety
    if safety.estop_active:
        word = "ESTOP"
    elif not state.link.connected:
        word = "LINK?"
    else:
        word = safety.state
    session = state.session.name if state.session else "--"
    fault = f"{GLYPH_FAULT} FAULT" if state.fault else f"{GLYPH_OK} OK"
    link = f"{GLYPH_OK} DAEMON" if state.link.connected else f"{GLYPH_FAULT} NO DAEMON"
    sim = " SIM" if (state.system and state.system.simulated) else ""
    # With the daemon gone, the last known session name is still worth showing
    # but the live fields are not: an unknown recording state has to read as
    # unknown, not as a confident circle nobody can trust.
    if state.link.connected:
        recording = GLYPH_ACTIVE if state.session is not None else GLYPH_IDLE
        devices = f"{len(state.devices):<2}"
    else:
        recording = GLYPH_UNKNOWN
        devices = "? "
    return (
        f"FIELDDECK{sim} {word:<6} "
        f"REC{recording} "
        f"SES {_clip(session, 16):<16} "
        f"DEV {devices} {fault:<8}{link}"
    )


def _alert_line(state: UiState) -> tuple[str, str]:
    """The one thing that matters most right now, and the CSS class for it."""
    safety = state.safety
    if safety.estop_active:
        reason = _clip(safety.estop_reason or "engaged", 40)
        return (
            f"{GLYPH_FAULT}{GLYPH_FAULT} EMERGENCY STOP LATCHED: {reason} "
            f"{GLYPH_FAULT}{GLYPH_FAULT} acknowledge on ARM",
            "estop",
        )
    if not state.link.connected:
        # Retry count first: it is the part that tells an operator whether the
        # panel is still trying, and it is the part a long path would cut off.
        detail = _clip(state.link.detail or "no connection", 44)
        return (
            f"{GLYPH_FAULT} instrumentd unreachable (retry {state.link.attempts}): {detail}",
            "offline",
        )
    grants = safety.active_grants()
    if grants:
        now = monotonic_ns()
        parts = " ".join(
            f"{grant.permission}:{duration(grant.remaining_s(now))}" for grant in grants[:3]
        )
        leases = f" LEASE{GLYPH_ACTIVE}{len(safety.leases)}" if safety.leases else ""
        return (f"{GLYPH_WARNING} ARMED {parts}{leases}", "armed")
    if state.fault is not None:
        return (
            f"{GLYPH_WARNING} {_clip(state.fault.message, 58)} "
            f"({duration(state.fault.age_s())} ago) MENU clears",
            "warn",
        )
    return (f"{GLYPH_OK} SAFE - nothing armed; PASSIVE actions only", "safe")


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "~"
