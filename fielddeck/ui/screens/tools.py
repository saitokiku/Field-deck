"""The conversion bench: arithmetic over bytes, and nothing else.

Every tool here is PASSIVE and stays available while an emergency stop is
latched, which is deliberate — working out what a capture *meant* is exactly
what an engineer should be doing while the bench is safe, and a toolbox that
greys itself out during a stop is a toolbox nobody trusts.

The tiles do not each implement a converter.  They ask ``instrumentd`` for the
same ``tools.*`` actions ``fdctl`` uses and then show one slice of the answer,
so a number read at the panel and the same number read from a script come from
one implementation.  The full answer is always one tap further in: the panel
shows the eight most useful readings, not all forty.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, ClassVar

from textual.containers import Grid
from textual.widget import Widget
from textual.widgets import Input, Static

from fielddeck.ui.screens import PanelScreen
from fielddeck.ui.state import UiState, parse_payload
from fielddeck.ui.widgets.status_bar import SUB_NAV
from fielddeck.ui.widgets.tiles import Tile, notice

__all__ = ["ToolsScreen"]

#: (key, label, hint keyword).  The keyword selects which readings of the
#: universal ``tools.convert`` answer this tile is about.
#: Matched against whole words of a reading's label, anchored at the start, so
#: DEC does not also select "hexadecimal" and FLOAT still selects "float32".
_CONVERT_TILES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("hex", "HEX", ("hex",)),
    ("dec", "DEC", ("decimal", "unsigned", "signed")),
    ("bin", "BIN", ("binary", "bit")),
    ("ascii", "ASCII", ("ascii", "printable", "utf-8", "code", "text")),
    ("float", "FLOAT", ("float", "ieee-754")),
    ("endian", "ENDIAN", ("little-endian", "big-endian")),
    ("bits", "BITS", ("binary", "bit")),
)

_TILES: tuple[tuple[str, str, str], ...] = (
    *((key, label, "convert") for key, label, _keywords in _CONVERT_TILES),
    ("crc", "CRC", "crc"),
    ("unit", "UNIT", "unit"),
    ("file", "FILE", "file"),
    ("hash", "HASH", "hash"),
    ("packet", "PACKET", "packet"),
)

MAX_ROWS = 7


class ToolsScreen(PanelScreen):
    screen_name: ClassVar[str] = "tools"
    hint: ClassVar[str] = "Type a value, then tap a tool. Everything here is passive arithmetic."
    NAV: ClassVar[tuple[tuple[str, str], ...]] = SUB_NAV

    def content(self) -> Iterable[Widget]:
        yield Input(placeholder="value: 0x1F, 4660, 55AA04, hello", id="tools-input")
        yield Static("readings appear here", id="tools-result", markup=False)
        with Grid(id="tools-grid"):
            for key, label, _kind in _TILES:
                yield Tile(key, label, classes="tool-tile", id=f"tool-{key}")

    def render_state(self, state: UiState) -> None:
        """Nothing here tracks the bench; the result panel is event driven."""

    # -- gestures ----------------------------------------------------------

    def tile_pressed(self, key: str) -> None:
        kind = {tile_key: tile_kind for tile_key, _label, tile_kind in _TILES}.get(key)
        if kind is None:
            return
        self.run_worker(self._run(key, kind), exclusive=True, group="gesture")

    def _value(self) -> str:
        return self.query_one("#tools-input", Input).value.strip()

    def _show(self, lines: Iterable[str]) -> None:
        self.query_one("#tools-result", Static).update("\n".join(lines))

    async def _run(self, key: str, kind: str) -> None:
        value = self._value()
        if not value and kind in {"convert", "crc", "hash", "packet"}:
            self._show(["type a value first - tap the field, then use the keyboard"])
            return
        if kind == "convert":
            await self._convert(key, value)
        elif kind == "crc":
            await self._crc(value)
        elif kind == "hash":
            await self._hash(value)
        elif kind == "packet":
            await self._packet(value)
        elif kind == "unit":
            await notice(
                self,
                "UNIT CONVERSION",
                [
                    "Unit conversion needs a from-unit and a to-unit, which is two more",
                    "fields than this panel has room for without shrinking the results.",
                    "",
                    "  fdctl convert unit 24 --from V --to mV",
                    "",
                    "It runs the same tools.convert action this screen uses.",
                ],
            )
        elif kind == "file":
            await notice(
                self,
                "FILE INSPECT",
                [
                    "Artifacts are inspected by path inside the session store:",
                    "",
                    "  fdctl session artifacts",
                    "  fdctl inspect can/can0-0001.log",
                    "",
                    "Paths are resolved inside the session directory only; the daemon",
                    "refuses anything that resolves outside it, symlinks included.",
                ],
            )

    async def _convert(self, key: str, value: str) -> None:
        keywords = {tile: words for tile, _label, words in _CONVERT_TILES}[key]
        outcome = await self.state.run("tools.convert", {"value": value, "operation": "interpret"})
        if not outcome.ok:
            self._show([outcome.summary()])
            return
        readings = [
            reading
            for reading in outcome.data.get("readings") or []
            if _matches(str(reading.get("label", "")), keywords)
        ]
        lines = [f"{value}  read as {', '.join(outcome.data.get('parsed_as') or ['?'])}"]
        for reading in readings[:MAX_ROWS]:
            lines.append(_reading_line(reading))
        if not readings:
            lines.append(f"no {key.upper()} reading of that input")
        for note in outcome.data.get("notes") or []:
            lines.append(f"! {note}")
        self._show(lines)

    async def _crc(self, value: str) -> None:
        data, problem = parse_payload(value, as_hex=True)
        if data is None:
            self._show([f"CRC works over bytes: {problem}"])
            return
        outcome = await self.state.run("tools.crc", {"hex": data.hex()})
        if not outcome.ok:
            self._show([outcome.summary()])
            return
        values = outcome.data.get("values") or {}
        lines = [f"CRC over {outcome.data.get('bytes', 0)} byte(s)"]
        lines.extend(f"  {name:<18}{result}" for name, result in list(values.items())[:MAX_ROWS])
        lines.append("to identify a trailer instead: fdctl crc --expected <bytes>")
        self._show(lines)

    async def _hash(self, value: str) -> None:
        data, problem = parse_payload(value, as_hex=True)
        params: dict[str, Any] = {"hex": data.hex()} if data is not None else {"text": value}
        outcome = await self.state.run("tools.hash", params)
        if not outcome.ok:
            self._show([outcome.summary()])
            return
        source = "hex bytes" if data is not None else f"text ({problem})"
        lines = [f"digests over {source}"]
        for name in ("sha256", "md5", "crc32"):
            if name in outcome.data:
                lines.append(f"  {name:<8}{outcome.data[name]}")
        lines.append(
            f"  covers {outcome.data.get('bytes', 0)} byte(s), complete "
            f"{outcome.data.get('source', {}).get('complete', '?')}"
        )
        self._show(lines)

    async def _packet(self, value: str) -> None:
        data, problem = parse_payload(value, as_hex=True)
        if data is None:
            self._show([f"packet analysis works over bytes: {problem}"])
            return
        outcome = await self.state.run(
            "tools.identify_protocol", {"hex": data.hex(), "limit": 3}, timeout_s=30.0
        )
        if not outcome.ok:
            self._show([outcome.summary()])
            return
        lines = [
            f"best {outcome.data.get('best', '?')} "
            f"({outcome.data.get('confidence', 0)}%) over {outcome.data.get('size_bytes', 0)} B"
        ]
        for hypothesis in (outcome.data.get("hypotheses") or [])[:3]:
            lines.append(
                f"  {hypothesis.get('protocol', '?')!s:<16}"
                f"{hypothesis.get('confidence', 0):>4}%  "
                f"{str(hypothesis.get('summary', ''))[:38]}"
            )
        lines.append("evidence, not proof - the recommended test was not run")
        self._show(lines)


def _matches(label: str, keywords: tuple[str, ...]) -> bool:
    words = re.split(r"[^a-z0-9+-]+", label.lower())
    return any(word.startswith(keyword) for word in words for keyword in keywords)


def _reading_line(reading: dict[str, Any]) -> str:
    """Group, label and value in fixed columns, with a caveat marker."""
    group = str(reading.get("group", ""))[:18]
    label = str(reading.get("label", ""))[:22]
    value = str(reading.get("value"))[:32]
    text = f"  {group:<18} {label:<22} {value}"
    return f"{text} !" if reading.get("note") else text
