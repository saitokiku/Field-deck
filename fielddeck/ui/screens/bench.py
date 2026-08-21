"""The bench supply: big numbers, and an unmissable statement of authority.

Two things dominate this screen because they are the two things that hurt
people and hardware: what the rail is actually doing, and whether anything is
allowed to change it.  The measurements are drawn in seven-segment digits
readable from the far side of a bench; the authorization band under them says
in words whether POWER is armed and what will happen if it is not.

The numbers here come from ``psu.status``, which is PASSIVE and reads the
driver's cached state.  MEASURE runs ``psu.measure``, which is QUERY, because
asking an instrument for a reading means transmitting SCPI to it — the panel
labels which of the two you are looking at rather than blurring them into one
"live" display, since a cached number from a disconnected supply looks exactly
like a fresh one.

Enabling the output takes a lease owned by this panel's connection.  If the UI
dies, the rail drops.  That is not a limitation to work around; it is the
feature.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, ClassVar

from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Static

from fielddeck.common.models import DeviceRole, PermissionLevel
from fielddeck.ui.screens import PanelScreen
from fielddeck.ui.state import UiState
from fielddeck.ui.widgets import GLYPH_ACTIVE, GLYPH_IDLE, GLYPH_WARNING, duration
from fielddeck.ui.widgets.keypad import NumericField
from fielddeck.ui.widgets.status_bar import SUB_NAV
from fielddeck.ui.widgets.tiles import Readout, Tile

__all__ = ["BenchScreen"]

POLL_EVERY_S = 1.0

#: Dead-man interval for an enabled output.  Short enough that a wedged panel
#: costs one interval of energised rail, long enough to survive a hiccup.
LEASE_TTL_S = 30.0


class BenchScreen(PanelScreen):
    screen_name: ClassVar[str] = "bench"
    hint: ClassVar[str] = "Setpoints and OUTPUT need POWER. MEASURE needs QUERY."
    NAV: ClassVar[tuple[tuple[str, str], ...]] = SUB_NAV

    def __init__(self) -> None:
        super().__init__()
        self._status: dict[str, Any] = {}
        self._source = "cached"
        #: True once the operator has moved a setpoint, so the poll stops
        #: overwriting what they are in the middle of dialling in.
        self._touched = False

    def content(self) -> Iterable[Widget]:
        yield Static("", id="bench-head")
        with Horizontal(id="bench-readouts"):
            yield Readout("VOLTAGE", unit="V", id="bench-volts")
            yield Readout("CURRENT", unit="A", id="bench-amps")
        yield NumericField(
            "setv",
            "SET V",
            value=0.0,
            step=0.5,
            unit="V",
            decimals=3,
            maximum=60.0,
            hint="the daemon enforces psu.voltage; this field does not",
            id="bench-setv",
        )
        yield NumericField(
            "seti",
            "LIMIT I",
            value=0.5,
            step=0.1,
            unit="A",
            decimals=3,
            maximum=10.0,
            hint="the daemon enforces psu.current; this field does not",
            id="bench-seti",
        )
        yield Static("", id="bench-auth")
        with Horizontal(id="bench-actions"):
            yield Tile("output", "OUTPUT", "", classes="action-tile", id="bench-output")
            yield Tile("apply", "APPLY", "send setpoints", classes="action-tile", id="bench-apply")
            yield Tile(
                "measure", "MEASURE", "QUERY the meter", classes="action-tile", id="bench-measure"
            )
            yield Tile("safe", "OFF", "always allowed", classes="action-tile", id="bench-off")

    def on_mount(self) -> None:
        super().on_mount()
        self.set_interval(POLL_EVERY_S, self._schedule_poll)
        self._schedule_poll()

    # -- polling -----------------------------------------------------------

    def _schedule_poll(self) -> None:
        if self.app.screen is self:
            self.run_worker(self._poll(), exclusive=True, group="bench-poll")

    async def _poll(self) -> None:
        device = self._device()
        if device is None:
            return
        outcome = await self.state.run("psu.status", {"device": device.id}, remember=False)
        if outcome.ok:
            self._status = outcome.data
            self._source = "cached"

    def _device(self) -> Any:
        return self.state.device_for(role=DeviceRole.PSU)

    # -- rendering ---------------------------------------------------------

    def render_state(self, state: UiState) -> None:
        device = self._device()
        status = self._status
        mode = str(status.get("mode") or "OFF")
        output_on = bool(status.get("output"))
        self.query_one("#bench-head", Static).update(
            f"BENCH PSU  {(device.display_name if device else 'no supply found')[:30]}  "
            f"MODE {mode}  SRC {self._source}"
        )
        self.query_one("#bench-volts", Readout).show(
            _digits(status.get("measured_v")), note=f"V  {'OUT ON' if output_on else 'OUT OFF'}"
        )
        self.query_one("#bench-amps", Readout).show(
            _digits(status.get("measured_a")), note=f"A  limit {status.get('current_limit_a', 0)}"
        )
        if not self._touched:
            self.query_one("#bench-setv", NumericField).show(float(status.get("setpoint_v") or 0.0))
            self.query_one("#bench-seti", NumericField).show(
                float(status.get("current_limit_a") or 0.0)
            )
        self.query_one("#bench-output", Tile).set_text(
            title=f"OUTPUT {GLYPH_ACTIVE if output_on else GLYPH_IDLE}",
            subtitle="tap to turn off" if output_on else "tap to energise",
        )
        self.query_one("#bench-auth", Static).update(_authorization(state, device, output_on))

    # -- gestures ----------------------------------------------------------

    def on_numeric_field_changed(self, event: NumericField.Changed) -> None:
        event.stop()
        self._touched = True

    def tile_pressed(self, key: str) -> None:
        device = self._device()
        if device is None:
            return
        if key == "apply":
            self.act(self._apply(device.id))
        elif key == "measure":
            self.act(self._measure(device.id))
        elif key == "safe":
            self.act(self.state.set_output(device.id, enabled=False))
        elif key == "output":
            self.run_worker(self._toggle_output(device.id), exclusive=True, group="gesture")

    async def _apply(self, device_id: str) -> None:
        outcome = await self.state.run(
            "psu.set",
            {
                "device": device_id,
                "voltage": self.query_one("#bench-setv", NumericField).value,
                "current_limit": self.query_one("#bench-seti", NumericField).value,
            },
        )
        if outcome.ok:
            # Let the instrument's own values drive the fields again.
            self._touched = False

    async def _measure(self, device_id: str) -> None:
        outcome = await self.state.run("psu.measure", {"device": device_id})
        if outcome.ok:
            self._status = {**self._status, **outcome.data}
            self._status["measured_v"] = outcome.data.get("voltage")
            self._status["measured_a"] = outcome.data.get("current")
            self._source = "measured"

    async def _toggle_output(self, device_id: str) -> None:
        if bool(self._status.get("output")):
            await self.state.set_output(device_id, enabled=False)
            return
        volts = self.query_one("#bench-setv", NumericField).value
        amps = self.query_one("#bench-seti", NumericField).value
        confirmed = await self.ask(
            "ENERGISE THE OUTPUT",
            [
                f"setpoint  {volts:.3f} V   limit {amps:.3f} A",
                f"lease     {LEASE_TTL_S:g} s dead-man; the rail drops if this panel stops",
                "",
                "Anything connected to the supply is about to be powered.",
            ],
            confirm_label="OUTPUT ON",
        )
        if confirmed:
            await self.state.set_output(device_id, enabled=True, lease_ttl_s=LEASE_TTL_S)


def _digits(value: object) -> str:
    try:
        return f"{float(value):.3f}"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "---.---"


def _authorization(state: UiState, device: Any, output_on: bool) -> str:
    """The band that says, in words, what is allowed right now.

    Advisory only.  The tiles stay live whether or not POWER is armed, and the
    refusal an operator gets comes from the daemon in the daemon's words; a
    panel that greys out its own controls is a panel that has re-implemented
    the permission model badly.
    """
    device_id = device.id if device is not None else None
    if state.safety.estop_active:
        return (
            f"{GLYPH_WARNING} ESTOP LATCHED - setpoints and OUTPUT ON are refused. "
            "Turning the output OFF is still allowed."
        )
    armed = state.safety.armed(PermissionLevel.POWER, device_id=device_id, action="psu.output")
    if not armed:
        return (
            "POWER authorization required for OUTPUT and setpoints - ARM POWER first\n"
            f"{GLYPH_IDLE} nothing is energised by this panel while POWER is unarmed"
        )
    remaining = duration(state.safety.remaining_s(PermissionLevel.POWER))
    lease = f"lease held {GLYPH_ACTIVE}" if state.holds_output_lease else "no lease held"
    return (
        f"{GLYPH_WARNING} POWER armed {remaining} left - OUTPUT is live authority\n"
        f"{'OUTPUT ON' if output_on else 'output off'}   {lease}"
    )
