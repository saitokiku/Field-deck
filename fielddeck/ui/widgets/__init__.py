"""Panel primitives, and the glyph vocabulary the whole HMI shares.

The panel has to be readable on a 480x320 resistive screen under a work light,
on a monochrome LCD, and over SSH on somebody's laptop.  So meaning is carried
by the glyph and the word; colour only ever reinforces something that already
reads without it.  Every screen imports these constants rather than typing a
character inline, because "does a filled circle mean recording or fault?" is a
question an operator should only have to answer once.

The vocabulary is fixed.  Do not extend it casually: a new glyph is a new
thing to learn at 2am.
"""

from __future__ import annotations

from fielddeck.common.models import ConnectionState

__all__ = [
    "GLYPH_ACTIVE",
    "GLYPH_FAULT",
    "GLYPH_IDLE",
    "GLYPH_OK",
    "GLYPH_RX",
    "GLYPH_TX",
    "GLYPH_UNKNOWN",
    "GLYPH_WARNING",
    "Keypad",
    "KeypadScreen",
    "NavBar",
    "NoticeScreen",
    "NumericField",
    "Readout",
    "StatusBar",
    "Tile",
    "confirm",
    "device_glyph",
    "duration",
    "ok_glyph",
]

GLYPH_OK = "✓"
GLYPH_IDLE = "○"
GLYPH_ACTIVE = "●"
GLYPH_WARNING = "!"
# Deliberately U+00D7, not the letter x: the spec fixes this vocabulary and a
# multiplication sign is visually distinct from a hex digit in a payload dump.
GLYPH_FAULT = "×"  # noqa: RUF001
GLYPH_UNKNOWN = "?"
GLYPH_TX = "→"
GLYPH_RX = "←"

_DEVICE_GLYPHS: dict[ConnectionState, str] = {
    ConnectionState.ABSENT: GLYPH_FAULT,
    ConnectionState.DISCOVERED: GLYPH_IDLE,
    ConnectionState.CONNECTING: GLYPH_UNKNOWN,
    ConnectionState.READY: GLYPH_OK,
    ConnectionState.BUSY: GLYPH_ACTIVE,
    ConnectionState.FAULT: GLYPH_FAULT,
    ConnectionState.DISCONNECTING: GLYPH_WARNING,
}


def device_glyph(state: ConnectionState) -> str:
    """One character for a connection state, per the shared vocabulary."""
    return _DEVICE_GLYPHS.get(state, GLYPH_UNKNOWN)


def ok_glyph(ok: bool) -> str:
    return GLYPH_OK if ok else GLYPH_FAULT


def duration(seconds: float) -> str:
    """Compact clock for countdowns and elapsed times: 42s, 3m12, 1h04."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}"
    return f"{total // 3600}h{(total % 3600) // 60:02d}"


def __getattr__(name: str) -> object:
    # Widgets pull in Textual; the glyph vocabulary above must stay importable
    # by anything that only needs to render a string.
    if name in {"StatusBar", "NavBar"}:
        from fielddeck.ui.widgets import status_bar

        return getattr(status_bar, name)
    if name in {"Tile", "Readout", "NoticeScreen", "confirm"}:
        from fielddeck.ui.widgets import tiles

        return getattr(tiles, name)
    if name in {"Keypad", "KeypadScreen", "NumericField"}:
        from fielddeck.ui.widgets import keypad

        return getattr(keypad, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
