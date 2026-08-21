"""Arming, disarming, and the button that stops everything.

This screen is the only place at the panel where authority is created, and it
is built so that authority is always visibly temporary: every grant shows the
seconds left on it, the TTL is chosen before the class is armed rather than
buried in a submenu, and the policy ceiling for each class is printed on its
own tile.  Nothing here inherits: arming POWER does not arm CONTROL, because
authorization is exact-class and pretending otherwise on a panel would teach an
operator a model the daemon does not implement.

FLASH and DESTRUCTIVE ask a second time.  ESTOP never does — stopping is not
the dangerous direction, and a confirmation dialog between an operator and an
emergency stop is a design defect.  Clearing a latched stop *is* confirmed,
because that is the act that makes the bench armable again.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Static

from fielddeck.common.models import PermissionLevel
from fielddeck.common.timebase import monotonic_ns
from fielddeck.ui.screens import PanelScreen
from fielddeck.ui.state import UiState
from fielddeck.ui.widgets import GLYPH_ACTIVE, GLYPH_FAULT, GLYPH_OK, GLYPH_WARNING, duration
from fielddeck.ui.widgets.keypad import NumericField
from fielddeck.ui.widgets.status_bar import SUB_NAV
from fielddeck.ui.widgets.tiles import Tile

__all__ = ["SafetyScreen"]

#: Classes an operator can arm from the panel, in severity order.
ARMABLE: tuple[PermissionLevel, ...] = (
    PermissionLevel.QUERY,
    PermissionLevel.CONTROL,
    PermissionLevel.POWER,
    PermissionLevel.FLASH,
    PermissionLevel.DESTRUCTIVE,
)

#: These rewrite or destroy what is on the target, so they cost a second tap.
CONFIRMED: frozenset[PermissionLevel] = frozenset(
    {PermissionLevel.FLASH, PermissionLevel.DESTRUCTIVE}
)

DEFAULT_TTL_S = 60.0


class SafetyScreen(PanelScreen):
    screen_name: ClassVar[str] = "safety"
    hint: ClassVar[str] = "Set the TTL, then tap a class. Grants expire on their own."
    NAV: ClassVar[tuple[tuple[str, str], ...]] = SUB_NAV

    def content(self) -> Iterable[Widget]:
        yield Static("", id="safety-state")
        yield NumericField(
            "ttl",
            "TTL",
            value=DEFAULT_TTL_S,
            step=15.0,
            unit="s",
            decimals=0,
            minimum=5.0,
            maximum=900.0,
            hint="the daemon clamps this to its own per-class ceiling",
            id="safety-ttl",
        )
        with Horizontal(id="safety-classes"):
            for level in ARMABLE:
                yield Tile(
                    f"arm-{level}",
                    str(level)[:9],
                    "",
                    classes="action-tile",
                    id=f"arm-{level.lower()}",
                )
        with Horizontal(id="safety-bottom"):
            yield Tile("estop", "E-STOP", "stop everything now", classes="estop-tile")
            yield Tile("disarm", "DISARM", "revoke all grants", classes="action-tile")
            yield Tile("clear", "CLEAR", "acknowledge estop", classes="action-tile")

    # -- rendering ---------------------------------------------------------

    def render_state(self, state: UiState) -> None:
        self.query_one("#safety-state", Static).update(_state_block(state))
        for level in ARMABLE:
            tile = self.query_one(f"#arm-{level.lower()}", Tile)
            remaining = state.safety.remaining_s(level)
            ceiling = state.max_arm_ttl_s.get(str(level))
            denied = str(level) in state.denied_permissions
            if denied:
                subtitle = "denied by policy"
            elif remaining > 0:
                subtitle = f"{GLYPH_ACTIVE} {duration(remaining)} left"
            else:
                subtitle = f"max {duration(ceiling)}" if ceiling else "arm"
            tile.set_text(subtitle=subtitle)

    # -- gestures ----------------------------------------------------------

    def tile_pressed(self, key: str) -> None:
        if key == "estop":
            self.act(self.state.estop())
            return
        if key == "disarm":
            self.act(self.state.disarm())
            return
        if key == "clear":
            self.run_worker(self._clear(), exclusive=True, group="gesture")
            return
        if key.startswith("arm-"):
            self.run_worker(self._arm(key.removeprefix("arm-")), exclusive=True, group="gesture")

    def _ttl(self) -> float:
        return float(self.query_one("#safety-ttl", NumericField).value)

    async def _arm(self, level_name: str) -> None:
        try:
            level = PermissionLevel(level_name)
        except ValueError:
            return
        ttl = self._ttl()
        if level in CONFIRMED:
            confirmed = await self.ask(
                f"ARM {level}",
                [
                    f"{level} rewrites or destroys what is on the target.",
                    f"Everything attached to the bench is in scope for {duration(ttl)}.",
                    "",
                    "Narrow the scope from fdctl if only one device should be covered.",
                ],
                confirm_label=f"ARM {level}",
            )
            if not confirmed:
                return
        await self.state.arm(level, ttl_s=ttl, note="armed at the panel")

    async def _clear(self) -> None:
        if not self.state.safety.estop_active:
            return
        confirmed = await self.ask(
            "CLEAR EMERGENCY STOP",
            [
                f"reason: {self.state.safety.estop_reason or 'unknown'}",
                "",
                "Clearing acknowledges the stop and makes the bench armable again.",
                "It re-arms nothing and re-energises nothing.",
                "Confirm the hazard is gone before you do this.",
            ],
            confirm_label="CLEAR",
        )
        if confirmed:
            await self.state.clear_estop()


def _state_block(state: UiState) -> str:
    now = monotonic_ns()
    safety = state.safety
    if safety.estop_active:
        head = f"{GLYPH_FAULT} ESTOP LATCHED - {safety.estop_reason or 'engaged'}"
    elif safety.armed_classes():
        head = f"{GLYPH_WARNING} ARMED - authority is live and counting down"
    else:
        head = f"{GLYPH_OK} SAFE - nothing armed; PASSIVE actions only"
    lines = [head]
    grants = safety.active_grants()
    for grant in grants[:3]:
        lines.append(
            f"  {grant.permission:<12}{duration(grant.remaining_s(now)):>7} left   "
            f"scope {grant.scope.describe()}   by {grant.created_by}"
        )
    if not grants:
        lines.append("  no grants")
    for lease in safety.leases[:2]:
        lines.append(
            f"  {GLYPH_ACTIVE} lease {lease.action} on {lease.device_id}"
            f"{duration(lease.remaining_s(now)):>7} left"
        )
    return "\n".join(lines)
