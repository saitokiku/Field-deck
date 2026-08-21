"""The touchscreen HMI.

An instrument panel, not an application: 80x25 on a 480x320 resistive panel,
single tap, no hover, no gestures, and every control large enough to hit with
a gloved finger.  The same layout is the SSH maintenance path, so everything
reachable by touch is reachable from the keyboard.

Nothing in this package touches hardware.  Screens read a snapshot from
:class:`fielddeck.ui.state.UiState` and hand gestures back to it; ``UiState``
is the only thing here that owns a socket.  If a control looks disabled it is
because ``instrumentd`` said so, never because a widget decided.

Imports are lazy so ``fielddeck-ui --help`` does not pay for Textual.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fielddeck.ui.app import FieldDeckApp
    from fielddeck.ui.state import UiState

__all__ = ["FieldDeckApp", "UiState"]


def __getattr__(name: str) -> Any:
    if name == "FieldDeckApp":
        from fielddeck.ui.app import FieldDeckApp

        return FieldDeckApp
    if name == "UiState":
        from fielddeck.ui.state import UiState

        return UiState
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
