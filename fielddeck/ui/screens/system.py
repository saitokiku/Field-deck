"""The MENU screen: what this box is, and what it is doing.

Mostly diagnosis.  When the panel is wrong, the first question is whether it is
talking to the daemon at all, the second is which daemon and which socket, and
the third is what the safety policy actually says — so all three are on one
screen with the retry button next to them.

The limits shown here are the deployment's own ceilings, read from
``system.limits`` when the connection came up.  They are printed rather than
edited: safety policy is a file an operator changes deliberately with a text
editor and a restart, not a slider on a touchscreen.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Static

from fielddeck.ui.screens import PanelScreen
from fielddeck.ui.state import UiState
from fielddeck.ui.widgets import GLYPH_FAULT, GLYPH_OK, GLYPH_WARNING, duration
from fielddeck.ui.widgets.status_bar import SUB_NAV
from fielddeck.ui.widgets.tiles import Tile, notice

__all__ = ["SystemScreen"]


class SystemScreen(PanelScreen):
    screen_name: ClassVar[str] = "system"
    hint: ClassVar[str] = "Diagnosis first: link, version, socket, policy."
    NAV: ClassVar[tuple[tuple[str, str], ...]] = SUB_NAV

    def content(self) -> Iterable[Widget]:
        yield Static("", id="system-body", markup=False)
        with Horizontal(id="system-actions"):
            yield Tile(
                "discover", "DISCOVER", "re-enumerate", classes="action-tile", id="sys-discover"
            )
            yield Tile("retry", "RETRY", "reconnect now", classes="action-tile", id="sys-retry")
            yield Tile("clear", "CLEAR", "acknowledge fault", classes="action-tile", id="sys-clear")
            yield Tile("limits", "LIMITS", "safety policy", classes="action-tile", id="sys-limits")
            yield Tile("quit", "QUIT", "leave the panel", classes="action-tile", id="sys-quit")

    def render_state(self, state: UiState) -> None:
        self.query_one("#system-body", Static).update(_body(state))

    def tile_pressed(self, key: str) -> None:
        if key == "discover":
            self.act(self.state.discover())
        elif key == "retry":
            self.state.retry_now()
        elif key == "clear":
            self.state.clear_fault()
        elif key == "limits":
            self.run_worker(self._limits(), exclusive=True, group="gesture")
        elif key == "quit":
            self.run_worker(self._quit(), exclusive=True, group="gesture")

    async def _limits(self) -> None:
        limits = self.state.limits
        lines = [f"  {name!s:<18}{_limit_text(spec)}" for name, spec in list(limits.items())[:10]]
        await notice(
            self,
            "SAFETY POLICY",
            [
                "Hard limits, applied after authorization and never waived by it:",
                *(lines or ["  (the daemon reported no limits)"]),
                "",
                f"denied classes: {', '.join(self.state.denied_permissions) or 'none'}",
                "Edit safety.yaml and restart instrumentd to change these.",
            ],
        )

    async def _quit(self) -> None:
        """Leaving the panel is deliberate: on a kiosk it is the whole UI."""
        confirmed = await self.ask(
            "LEAVE THE PANEL",
            [
                "instrumentd keeps running, and so does any recording session.",
                "Any output lease this panel holds is released, which turns that",
                "output off.",
            ],
            confirm_label="QUIT",
        )
        if confirmed:
            self.app.exit()


def _body(state: UiState) -> str:
    system = state.system
    link = state.link
    link_line = (
        f"{GLYPH_OK} connected to instrumentd"
        if link.connected
        else f"{GLYPH_FAULT} unreachable: {link.detail} (attempt {link.attempts})"
    )
    server = link.server
    lines = [
        f"LINK      {link_line}",
        f"socket    {link.socket}",
        f"daemon    pid {server.get('pid', '?')}  protocol {server.get('protocol', '?')}  "
        f"source {server.get('source', '?')}",
    ]
    if system is not None:
        lines += [
            f"version   {system.version}{'  SIMULATED' if system.simulated else ''}",
            f"uptime    {duration(system.uptime_s)}   utc {system.utc[:19]}",
            f"devices   {system.device_count}   running actions {system.running_actions}",
            f"sessions  {system.sessions_dir}",
            f"logs      {system.compression}",
        ]
    else:
        lines.append("version   (waiting for the daemon)")
    # A panel started with --sim against a daemon that is driving real hardware
    # is the one confusion on this screen that can cost a DUT.
    if state.simulation_requested and system is not None and not system.simulated:
        lines.append(f"{GLYPH_WARNING} --sim was requested here, but this daemon is NOT simulating")
    if state.fault is not None:
        lines.append(
            f"{GLYPH_WARNING} fault   {state.fault.message[:52]} "
            f"({duration(state.fault.age_s())} ago)"
        )
    lines.append(
        f"policy    {len(state.limits)} limit(s); max TTL "
        + ", ".join(f"{name}:{value:g}s" for name, value in list(state.max_arm_ttl_s.items())[:4])
    )
    return "\n".join(lines)


def _limit_text(spec: object) -> str:
    if isinstance(spec, dict):
        low = spec.get("minimum")
        high = spec.get("maximum")
        unit = spec.get("unit") or ""
        return f"{low if low is not None else '-'} .. {high if high is not None else '-'} {unit}"
    return str(spec)
