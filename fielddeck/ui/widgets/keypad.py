"""Numeric entry for gloved fingers.

A setpoint on this panel can be reached three ways, and all three exist for a
reason.  ``-`` and ``+`` are for the nudge an engineer makes while watching a
meter.  The keypad is for "make it 12.6 now", which a stepper turns into forty
taps.  The keys are twenty columns by four rows, far past the 90x45 pixel
minimum, because this is the one place where a mis-tap changes a number rather
than a screen.

The widget deliberately does **not** clamp to the safety limits.  It bounds the
stepper to the instrument's own range so a held key cannot run away, and it
prints the policy limit as a hint — but if an operator enters 40 V into a
supply the policy caps at 24.5 V, the request goes to ``instrumentd`` and the
refusal comes back with the daemon's own wording.  A panel that silently
rounded a setpoint down to something legal would be teaching the operator that
the limit does not exist.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static

from fielddeck.ui.widgets.tiles import Tile

__all__ = ["Keypad", "KeypadScreen", "NumericField"]

#: (key, label) laid out four across.  CLR wipes, DEL backspaces, +/- flips
#: sign, and the two commit keys sit apart from the digits on the last row.
_KEYS: tuple[tuple[str, str], ...] = (
    ("7", "7"),
    ("8", "8"),
    ("9", "9"),
    ("clr", "CLR"),
    ("4", "4"),
    ("5", "5"),
    ("6", "6"),
    ("del", "DEL"),
    ("1", "1"),
    ("2", "2"),
    ("3", "3"),
    ("sign", "+/-"),
    ("0", "0"),
    (".", "."),
    ("cancel", "CANCEL"),
    ("ok", "OK"),
)


class Keypad(Grid):
    """The key field itself, so a screen can embed one without the modal."""

    class Key(Message):
        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key

    def compose(self) -> ComposeResult:
        for key, label in _KEYS:
            yield Tile(key, label, classes="keypad-key", id=f"key-{_slug(key)}")

    def on_tile_pressed(self, event: Tile.Pressed) -> None:
        event.stop()
        self.post_message(self.Key(event.key))


class KeypadScreen(ModalScreen[float | None]):
    """Full-screen direct entry.  Returns the value, or None if cancelled."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("enter", "accept", "Accept", show=False),
        Binding("backspace", "backspace", "Delete", show=False),
    ]

    def __init__(
        self,
        title: str,
        *,
        unit: str = "",
        value: float | None = None,
        hint: str = "",
    ) -> None:
        super().__init__()
        self._title = title
        self._unit = unit
        self._hint = hint
        self._buffer = "" if value is None else _trim(value)

    def compose(self) -> ComposeResult:
        with Vertical(id="keypad-panel"):
            yield Static(
                f"{self._title}  [{self._unit}]" if self._unit else self._title, id="keypad-title"
            )
            yield Static(self._display(), id="keypad-display")
            yield Static(self._hint, id="keypad-hint")
            yield Keypad(id="keypad-grid")

    def _display(self) -> str:
        return self._buffer or "_"

    def on_key(self, event: object) -> None:
        """Type the number instead of tapping it.  SSH is the maintenance path."""
        key = getattr(event, "character", None)
        if key and (key.isdigit() or key in {".", "-"}):
            self._press(key if key != "-" else "sign")
            stop = getattr(event, "stop", None)
            if callable(stop):
                stop()

    def on_keypad_key(self, event: Keypad.Key) -> None:
        self._press(event.key)

    def _press(self, key: str) -> None:
        if key == "cancel":
            self.dismiss(None)
            return
        if key == "ok":
            self.action_accept()
            return
        if key == "clr":
            self._buffer = ""
        elif key == "del":
            self._buffer = self._buffer[:-1]
        elif key == "sign":
            self._buffer = self._buffer[1:] if self._buffer.startswith("-") else "-" + self._buffer
        elif key == "." and "." not in self._buffer:
            self._buffer = (self._buffer or "0") + "."
        # Long enough for any setpoint, short enough that a stuck touch panel
        # cannot build a number nobody can read.
        elif key.isdigit() and len(self._buffer.lstrip("-")) < 10:
            self._buffer += key
        self.query_one("#keypad-display", Static).update(self._display())

    def action_backspace(self) -> None:
        self._press("del")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_accept(self) -> None:
        try:
            value = float(self._buffer)
        except ValueError:
            self.query_one("#keypad-hint", Static).update("not a number - CLR and try again")
            return
        self.dismiss(value)


class NumericField(Horizontal):
    """``-`` / value / ``+`` / direct entry, as one control.

    Emits :class:`NumericField.Changed` whenever the operator settles on a new
    number.  It does not act on it: the screen decides which action that is,
    and the daemon decides whether it is allowed.
    """

    class Changed(Message):
        def __init__(self, field: NumericField, value: float) -> None:
            super().__init__()
            self.field = field
            self.value = value

    def __init__(
        self,
        key: str,
        caption: str,
        *,
        value: float = 0.0,
        step: float = 0.1,
        unit: str = "",
        decimals: int = 3,
        minimum: float = 0.0,
        maximum: float = 1_000_000.0,
        hint: str = "",
        id: str | None = None,
    ) -> None:
        super().__init__(id=id, classes="numeric-field")
        self.key = key
        self.caption = caption
        self.value = value
        self.step = step
        self.unit = unit
        self.decimals = decimals
        #: The instrument's own range, used only to stop the ``+``/``-`` keys
        #: running away.  Safety limits are the daemon's business, not this
        #: widget's, and are shown in ``hint``.
        self.minimum = minimum
        self.maximum = maximum
        self.hint = hint

    def compose(self) -> ComposeResult:
        yield Tile(f"{self.key}-down", "-", classes="step-tile", id=f"{self.key}-down")
        yield Static(self._label(), classes="numeric-value", id=f"{self.key}-value")
        yield Tile(f"{self.key}-up", "+", classes="step-tile", id=f"{self.key}-up")
        yield Tile(f"{self.key}-set", "SET", classes="step-tile wide", id=f"{self.key}-set")

    def _label(self) -> str:
        return f"{self.caption} {self.value:.{self.decimals}f} {self.unit}".rstrip()

    def show(self, value: float) -> None:
        """Adopt a value reported by the instrument, without emitting Changed."""
        if abs(value - self.value) < 10 ** (-self.decimals - 1):
            return
        self.value = value
        self.query_one(f"#{self.key}-value", Static).update(self._label())

    def on_tile_pressed(self, event: Tile.Pressed) -> None:
        event.stop()
        if event.key.endswith("-set"):
            self.run_worker(self._enter(), exclusive=True, group=f"keypad-{self.key}")
            return
        delta = self.step if event.key.endswith("-up") else -self.step
        self._commit(min(self.maximum, max(self.minimum, self.value + delta)))

    async def _enter(self) -> None:
        entered = await self.app.push_screen_wait(
            KeypadScreen(self.caption, unit=self.unit, value=self.value, hint=self.hint)
        )
        if entered is not None:
            self._commit(entered)

    def _commit(self, value: float) -> None:
        self.value = round(value, self.decimals)
        self.query_one(f"#{self.key}-value", Static).update(self._label())
        self.post_message(self.Changed(self, self.value))


def _trim(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _slug(key: str) -> str:
    return {".": "dot", "+/-": "sign"}.get(key, key)
