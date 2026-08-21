"""Large touch blocks, and the full-screen panels built out of them.

Every primary control on this panel is a :class:`Tile`: at least 15 columns by
3 rows, which is the ~90x45 physical pixels a fingertip needs on a 480x320
resistive screen.  A tile answers to exactly one gesture — a single tap, or
Enter/Space when it has keyboard focus.  There is no double tap, no long
press, no hover state and no drag, because a resistive panel cannot report any
of them reliably and an operator wearing gloves cannot perform them.

Focus is drawn in reverse video rather than a colour, so the SSH user on a
monochrome terminal can still see where the keyboard is pointing.

:func:`confirm` is the second deliberate action that anything destructive
requires.  It is a separate full-screen panel on purpose: a small dialog with
a default button is something a stray touch can dismiss, and "I did not mean
to press that" is the failure mode the rule exists to prevent.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult, RenderResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import Digits, Static

__all__ = ["ConfirmScreen", "NoticeScreen", "Readout", "Tile", "confirm", "notice"]


class Tile(Widget):
    """One big, single-tap control.

    ``key`` is what the screen switches on; the label is what the operator
    reads.  Keeping them separate means a caption can be reworded without
    silently changing which action fires.
    """

    can_focus = True

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter,space", "press", "Select", show=False),
    ]

    class Pressed(Message):
        """Posted on tap or Enter.  Bubbles to the containing screen."""

        def __init__(self, tile: Tile) -> None:
            super().__init__()
            self.tile = tile
            self.key = tile.key

    def __init__(
        self,
        key: str,
        title: str,
        subtitle: str = "",
        *,
        status: str = "",
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes, disabled=disabled)
        self.key = key
        self.title_text = title
        self.subtitle_text = subtitle
        self.status_text = status

    def render(self) -> RenderResult:
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(self.title_text, style="bold")
        if self.status_text:
            text.append(f"  {self.status_text}")
        if self.subtitle_text:
            text.append("\n")
            text.append(self.subtitle_text, style="dim")
        return text

    def set_text(
        self, title: str | None = None, subtitle: str | None = None, status: str | None = None
    ) -> None:
        """Update the caption in place; cheap enough for a 10 Hz repaint."""
        changed = False
        for attribute, value in (
            ("title_text", title),
            ("subtitle_text", subtitle),
            ("status_text", status),
        ):
            if value is not None and getattr(self, attribute) != value:
                setattr(self, attribute, value)
                changed = True
        if changed:
            self.refresh()

    def on_click(self) -> None:
        self.press()

    def action_press(self) -> None:
        self.press()

    def press(self) -> None:
        """Fire this tile.  A disabled tile stays silent."""
        if self.disabled:
            return
        self.post_message(self.Pressed(self))


class Readout(Vertical):
    """A measurement, large enough to read from across a bench.

    The caption and unit are ordinary text so the value stays the only thing
    competing for attention; :class:`~textual.widgets.Digits` draws the number
    itself in seven-segment style across three rows.
    """

    def __init__(
        self,
        caption: str,
        *,
        unit: str = "",
        value: str = "0.000",
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=f"readout {classes}" if classes else "readout")
        self._caption = caption
        self._unit = unit
        self._value = value

    def compose(self) -> ComposeResult:
        yield Static(self._caption, classes="readout-caption")
        yield Digits(self._value, classes="readout-value")
        yield Static(self._unit, classes="readout-note")

    def show(self, value: str, *, note: str | None = None) -> None:
        if value != self._value:
            self._value = value
            self.query_one(Digits).update(value)
        if note is not None and note != self._unit:
            self._unit = note
            self.query_one(".readout-note", Static).update(note)


class NoticeScreen(ModalScreen[None]):
    """A full-screen message with one way out.

    Used where the honest answer is a sentence rather than a control — an
    unimplemented panel, or the assistant's short-form observations.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,enter,space", "close", "Back", show=False)
    ]

    def __init__(self, title: str, lines: list[str]) -> None:
        super().__init__()
        self._title = title
        self._lines = lines

    def compose(self) -> ComposeResult:
        with Vertical(id="notice"):
            yield Static(self._title, id="notice-title")
            yield Static("\n".join(self._lines), id="notice-body")
            yield Tile("close", "CLOSE", "back to the panel", id="notice-close")

    def on_tile_pressed(self, _event: Tile.Pressed) -> None:
        self.action_close()

    def action_close(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """The second deliberate action.

    Both choices are full-size tiles and neither is pre-armed: the cancel tile
    takes focus, so a stray Enter from a keyboard user backs out rather than
    commits.  A tap has to land on the confirm tile specifically.
    """

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(
        self,
        title: str,
        lines: list[str],
        *,
        confirm_label: str = "CONFIRM",
        cancel_label: str = "CANCEL",
    ) -> None:
        super().__init__()
        self._title = title
        self._lines = lines
        self._confirm_label = confirm_label
        self._cancel_label = cancel_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Static(self._title, id="confirm-title")
            yield Static("\n".join(self._lines), id="confirm-body")
            with Horizontal(id="confirm-actions"):
                yield Tile("cancel", self._cancel_label, "nothing happens", id="confirm-no")
                yield Tile("confirm", self._confirm_label, "do it now", id="confirm-yes")

    def on_mount(self) -> None:
        self.query_one("#confirm-no", Tile).focus()

    def on_tile_pressed(self, event: Tile.Pressed) -> None:
        self.dismiss(event.key == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


async def confirm(
    widget: Widget,
    title: str,
    lines: list[str],
    *,
    confirm_label: str = "CONFIRM",
) -> bool:
    """Ask for the second action.  Must be awaited from a worker, not a callback."""
    screen: Screen[Any] = ConfirmScreen(title, lines, confirm_label=confirm_label)
    return bool(await widget.app.push_screen_wait(screen))


async def notice(widget: Widget, title: str, lines: list[str]) -> None:
    """Show a full-screen message and wait for the operator to dismiss it."""
    screen: Screen[Any] = NoticeScreen(title, lines)
    await widget.app.push_screen_wait(screen)
