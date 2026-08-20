"""The binary conversion toolbox.

Half of field debugging is answering "what *is* this number?".  A register
dump says ``0x41C80000``; the datasheet says the field is a float; the
firmware author swears it is two 16-bit words in the other order.  All three
readings are legitimate until evidence says otherwise, so
:func:`interpret` returns every plausible one at once and lets the engineer
recognise the right answer instead of guessing which conversion to type.

Everything here is a pure function over ``bytes``, ``str`` or ``int``.  There
is no file I/O: the action layer reads the file (after checking the path) and
hands the bytes in.  That split is what keeps this module trivially testable
and safe to call from a recipe, the HMI or Claude.

Risky edges worth knowing at 2am:

* **Unit names are case-sensitive.**  ``mV`` and ``MV`` differ by a factor of
  a billion.  A toolbox that lowercases units to be friendly is a toolbox
  that will one day tell someone a 5 MV bus is 5 mV.
* **Bare digit strings are ambiguous.**  ``1234`` is decimal 1234 and also a
  perfectly good hex literal (4660).  Both are reported, flagged as
  ambiguous, rather than one being picked silently.
* **Non-finite floats are reported as ``None`` with a note**, because a bit
  pattern that decodes to NaN is a real and interesting result but is not
  representable in JSON.
* **COBS and SLIP are implemented here** rather than pulled from a
  dependency: framing analysis needs them on a Pi with no wheels available,
  and they are short enough to verify against the reference vectors.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import math
import re
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from fielddeck.analysis.crc import crc
from fielddeck.common.errors import InvalidRequest

__all__ = [
    "ENDIANNESS",
    "EPOCH_UNITS",
    "FLOAT_WIDTHS",
    "INT_WIDTHS",
    "NumberParse",
    "Reading",
    "base64_decode",
    "base64_encode",
    "bitfield",
    "bytes_to_floats",
    "bytes_to_ints",
    "cobs_decode",
    "cobs_encode",
    "convert_unit",
    "epoch_to_iso",
    "float_to_bytes",
    "guess_epoch_units",
    "hash_bytes",
    "hexdump",
    "inspect_elf",
    "int_to_bytes",
    "interpret",
    "iso_to_epoch",
    "list_units",
    "number_candidates",
    "parse_hex_bytes",
    "parse_intel_hex",
    "parse_number",
    "printable_text",
    "slice_bytes",
    "slip_decode",
    "slip_encode",
    "to_base",
]

#: Integer widths a bus actually uses.  Anything wider is reported as raw
#: bytes; anything narrower is a bitfield, not an integer.
INT_WIDTHS: tuple[int, ...] = (8, 16, 32, 64)
FLOAT_WIDTHS: tuple[int, ...] = (16, 32, 64)
ENDIANNESS: tuple[str, ...] = ("big", "little")

ByteOrder = Literal["big", "little"]

_FLOAT_CODE: dict[int, str] = {16: "e", 32: "f", 64: "d"}
_ENDIAN_PREFIX: dict[str, str] = {"big": ">", "little": "<"}

#: Nanoseconds per unit, for epoch conversions.
EPOCH_UNITS: dict[str, int] = {"s": 1_000_000_000, "ms": 1_000_000, "us": 1_000, "ns": 1}

_HEX_CLEAN = re.compile(r"(?i)^(0x)?([0-9a-f]+)$")
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=_-]+$")


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reading:
    """One plausible interpretation of an input.

    ``group`` is what the HMI puts in a section header, ``label`` is the row.
    ``note`` carries the caveat that stops a reading being over-trusted.
    """

    group: str
    label: str
    value: Any
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"group": self.group, "label": self.label, "value": self.value}
        if self.note:
            payload["note"] = self.note
        return payload


# ---------------------------------------------------------------------------
# Number parsing and base conversion
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NumberParse:
    """One way an input string can be read as an integer."""

    value: int
    base: int
    label: str
    #: True when the input carried an explicit prefix (``0x``, ``0b``, ``0o``)
    #: so there is nothing to be ambiguous about.
    explicit: bool


def _clean_number(text: str) -> str:
    return text.strip().replace("_", "").replace(" ", "")


def parse_number(text: str, *, base: int | None = None) -> int:
    """Parse an integer literal, honouring ``0x``/``0b``/``0o`` prefixes.

    ``base`` forces an interpretation; without it a prefixed literal uses its
    prefix and a bare one is decimal.  Use :func:`number_candidates` when the
    caller wants every reading of an ambiguous string instead of one.
    """
    cleaned = _clean_number(text)
    if not cleaned:
        raise InvalidRequest("no number given", details={"input": text})
    negative = cleaned.startswith("-")
    body = cleaned[1:] if negative else cleaned
    try:
        if base is not None:
            value = int(body, base)
        elif body.lower().startswith(("0x", "0b", "0o")):
            value = int(body, 0)
        else:
            # int(body, 0) rejects leading zeros, which a register dump has
            # every right to contain.
            value = int(body, 10)
    except ValueError as exc:
        raise InvalidRequest(
            f"{text!r} is not an integer in base {base if base is not None else 'auto'}",
            details={"input": text, "base": base},
        ) from exc
    return -value if negative else value


def number_candidates(text: str) -> list[NumberParse]:
    """Every integer reading of ``text``, most likely first.

    A bare digit string is genuinely ambiguous, so both the decimal and the
    hexadecimal reading come back.  The HMI shows both; nothing downstream
    gets to pretend the input was unambiguous.
    """
    cleaned = _clean_number(text)
    if not cleaned:
        return []
    negative = cleaned.startswith("-")
    body = cleaned[1:] if negative else cleaned
    if not body:
        # A bare sign is not a number, and "every character is a hex digit" is
        # vacuously true of the empty string.
        return []
    sign = -1 if negative else 1
    lowered = body.lower()

    candidates: list[NumberParse] = []

    def add(value: int, base: int, label: str, explicit: bool) -> None:
        candidates.append(
            NumberParse(value=sign * value, base=base, label=label, explicit=explicit)
        )

    for prefix, base, label in (("0x", 16, "hexadecimal"), ("0b", 2, "binary"), ("0o", 8, "octal")):
        if lowered.startswith(prefix):
            try:
                add(int(lowered[2:], base), base, f"{label} literal", True)
            except ValueError:
                return []
            return candidates

    if lowered.isdigit():
        add(int(lowered, 10), 10, "decimal", False)
    if all(char in "01" for char in lowered) and len(lowered) >= 4:
        add(int(lowered, 2), 2, "binary digits", False)
    if all(char in "0123456789abcdef" for char in lowered):
        hex_value = int(lowered, 16)
        # Only worth showing when it differs from the decimal reading.
        if not (lowered.isdigit() and hex_value == int(lowered, 10)):
            add(hex_value, 16, "hexadecimal digits", False)
    return candidates


def to_base(value: int, base: int, *, width: int | None = None) -> str:
    """Render an integer in base 2, 8, 10 or 16.

    ``width`` pads to that many bits (two's complement for negatives), which
    is what makes a bitfield readable next to a datasheet.
    """
    if base not in (2, 8, 10, 16):
        raise InvalidRequest("base must be 2, 8, 10 or 16", details={"base": base})
    if width is not None:
        if width <= 0:
            raise InvalidRequest("width must be positive", details={"width": width})
        value &= (1 << width) - 1
    if base == 10:
        return str(value)
    negative = value < 0
    digits = format(abs(value), {2: "b", 8: "o", 16: "X"}[base])
    if width is not None:
        per_digit = {2: 1, 8: 3, 16: 4}[base]
        digits = digits.rjust((width + per_digit - 1) // per_digit, "0")
    prefix = {2: "0b", 8: "0o", 16: "0x"}[base]
    return f"{'-' if negative else ''}{prefix}{digits}"


# ---------------------------------------------------------------------------
# Bytes <-> numbers
# ---------------------------------------------------------------------------


def parse_hex_bytes(text: str) -> bytes:
    """Parse ``DE AD BE EF``, ``0xdeadbeef`` or ``deadbeef`` into bytes."""
    cleaned = re.sub(r"[\s:,_-]", "", text.strip())
    if cleaned.lower().startswith("0x"):
        cleaned = cleaned[2:]
    if not cleaned:
        return b""
    if len(cleaned) % 2:
        raise InvalidRequest(
            "hex input must be whole bytes",
            details={"input": text, "hex_digits": len(cleaned)},
        )
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise InvalidRequest(f"{text!r} is not hexadecimal", details={"input": text}) from exc


def _check_width(width: int, allowed: tuple[int, ...]) -> None:
    if width not in allowed:
        raise InvalidRequest(
            f"width must be one of {list(allowed)}",
            details={"width": width, "allowed": list(allowed)},
        )


def _check_endianness(endianness: str) -> ByteOrder:
    if endianness not in ENDIANNESS:
        raise InvalidRequest(
            "endianness must be 'big' or 'little'",
            details={"endianness": endianness},
        )
    return cast(ByteOrder, endianness)


def int_to_bytes(value: int, width: int, *, endianness: str = "big", signed: bool = False) -> bytes:
    """Encode an integer into a fixed-width field."""
    _check_width(width, INT_WIDTHS)
    order = _check_endianness(endianness)
    try:
        return value.to_bytes(width // 8, order, signed=signed)
    except OverflowError as exc:
        raise InvalidRequest(
            f"{value} does not fit in {'a signed' if signed else 'an unsigned'} {width}-bit field",
            details={"value": value, "width": width, "signed": signed},
        ) from exc


def bytes_to_ints(
    data: bytes, width: int, *, endianness: str = "big", signed: bool = False
) -> list[int]:
    """Decode a byte string as a sequence of fixed-width integers.

    A trailing partial element is dropped rather than zero-padded: padding
    invents data, and a half-read register is not a register.
    """
    _check_width(width, INT_WIDTHS)
    order = _check_endianness(endianness)
    size = width // 8
    return [
        int.from_bytes(data[offset : offset + size], order, signed=signed)
        for offset in range(0, len(data) - size + 1, size)
    ]


def bytes_to_floats(data: bytes, width: int, *, endianness: str = "big") -> list[float]:
    """Decode a byte string as IEEE-754 floats of the given width."""
    _check_width(width, FLOAT_WIDTHS)
    _check_endianness(endianness)
    size = width // 8
    code = _ENDIAN_PREFIX[endianness] + _FLOAT_CODE[width]
    return [
        struct.unpack(code, data[offset : offset + size])[0]
        for offset in range(0, len(data) - size + 1, size)
    ]


def float_to_bytes(value: float, width: int, *, endianness: str = "big") -> bytes:
    """Encode a float into its IEEE-754 bit pattern."""
    _check_width(width, FLOAT_WIDTHS)
    _check_endianness(endianness)
    try:
        return struct.pack(_ENDIAN_PREFIX[endianness] + _FLOAT_CODE[width], value)
    except OverflowError as exc:
        raise InvalidRequest(
            f"{value} overflows a float{width}",
            details={"value": value, "width": width},
        ) from exc


def bitfield(value: int, offset: int, count: int, *, total_width: int | None = None) -> int:
    """Extract ``count`` bits starting ``offset`` bits above bit 0.

    Bit 0 is the least significant bit, which is how every register map in
    every datasheet numbers them.
    """
    if offset < 0 or count <= 0:
        raise InvalidRequest(
            "offset must be >= 0 and count > 0",
            details={"offset": offset, "count": count},
        )
    if total_width is not None:
        if offset + count > total_width:
            raise InvalidRequest(
                f"bits {offset}..{offset + count - 1} fall outside a {total_width}-bit field",
                details={"offset": offset, "count": count, "total_width": total_width},
            )
        value &= (1 << total_width) - 1
    return (value >> offset) & ((1 << count) - 1)


def slice_bytes(data: bytes, offset: int = 0, length: int | None = None) -> bytes:
    """Bounds-checked raw slice.

    Python would silently return a short slice past the end; a protocol
    analyst reading "8 bytes at 0x40" and getting three needs to be told.
    """
    if offset < 0:
        raise InvalidRequest("offset must be >= 0", details={"offset": offset})
    if offset > len(data):
        raise InvalidRequest(
            f"offset {offset} is past the end of {len(data)} bytes",
            details={"offset": offset, "size": len(data)},
        )
    if length is None:
        return data[offset:]
    if length < 0:
        raise InvalidRequest("length must be >= 0", details={"length": length})
    end = offset + length
    if end > len(data):
        raise InvalidRequest(
            f"{length} bytes at offset {offset} runs past the end of {len(data)} bytes",
            details={"offset": offset, "length": length, "size": len(data)},
        )
    return data[offset:end]


def printable_text(data: bytes, *, placeholder: str = ".") -> str:
    """Render bytes the way a hex editor's right-hand column does."""
    return "".join(chr(byte) if 0x20 <= byte < 0x7F else placeholder for byte in data)


def hexdump(data: bytes, *, base_offset: int = 0, width: int = 16, max_bytes: int = 512) -> str:
    """Classic offset / hex / ASCII dump, truncated to ``max_bytes``."""
    if width <= 0:
        raise InvalidRequest("width must be positive", details={"width": width})
    shown = data[:max_bytes]
    lines: list[str] = []
    for start in range(0, len(shown), width):
        chunk = shown[start : start + width]
        hex_part = " ".join(f"{byte:02X}" for byte in chunk).ljust(width * 3 - 1)
        lines.append(f"{base_offset + start:08X}  {hex_part}  |{printable_text(chunk)}|")
    if len(data) > len(shown):
        lines.append(f"... {len(data) - len(shown)} more bytes")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# base64
# ---------------------------------------------------------------------------


def base64_encode(data: bytes, *, urlsafe: bool = False) -> str:
    encoder = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    return encoder(data).decode("ascii")


def base64_decode(text: str, *, urlsafe: bool = False) -> bytes:
    """Decode base64, rejecting anything that is not valid rather than guessing."""
    stripped = text.strip()
    try:
        if urlsafe:
            return base64.urlsafe_b64decode(stripped)
        return base64.b64decode(stripped, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidRequest(f"{text!r} is not valid base64", details={"input": text}) from exc


# ---------------------------------------------------------------------------
# COBS (Consistent Overhead Byte Stuffing, Cheshire & Baker)
# ---------------------------------------------------------------------------


def cobs_encode(data: bytes) -> bytes:
    """Encode one COBS block.  The frame delimiter is *not* appended.

    The encoded block contains no zero bytes, so the caller frames it by
    appending ``0x00``.  Empty input encodes to ``b"\\x01"``, which is a real
    frame — an empty packet is distinguishable from no packet.
    """
    out = bytearray()
    block_start = 0
    #: Whether the tail of the input still owes a code byte.  This is the
    #: subtlety the naive implementation gets wrong: input ending exactly on a
    #: full 254-byte run must not gain a trailing ``0x01``.
    needs_final_block = True
    for index, byte in enumerate(data):
        if byte == 0:
            out.append(index - block_start + 1)
            out += data[block_start:index]
            block_start = index + 1
            needs_final_block = True
        elif index - block_start == 0xFD:
            out.append(0xFF)
            out += data[block_start : index + 1]
            block_start = index + 1
            needs_final_block = False
    if block_start != len(data) or needs_final_block:
        out.append(len(data) - block_start + 1)
        out += data[block_start:]
    return bytes(out)


def cobs_decode(encoded: bytes) -> bytes:
    """Decode one COBS block, tolerating a single trailing delimiter."""
    block = encoded[:-1] if encoded.endswith(b"\x00") else encoded
    if not block:
        raise InvalidRequest(
            "an empty COBS block cannot be decoded",
            details={"hint": "the smallest valid block is 0x01, which decodes to no bytes"},
        )
    out = bytearray()
    index = 0
    while index < len(block):
        code = block[index]
        if code == 0:
            raise InvalidRequest(
                f"zero byte at offset {index} inside a COBS block",
                details={"offset": index},
                preserved=f"{len(out)} bytes decoded before the fault",
            )
        chunk = block[index + 1 : index + code]
        if len(chunk) != code - 1:
            raise InvalidRequest(
                f"COBS block truncated: code {code} at offset {index} needs {code - 1} bytes, "
                f"{len(chunk)} remain",
                details={"offset": index, "code": code, "remaining": len(chunk)},
                preserved=f"{len(out)} bytes decoded before the fault",
            )
        if 0 in chunk:
            raise InvalidRequest(
                f"zero byte inside the data of the COBS block at offset {index}",
                details={"offset": index},
                preserved=f"{len(out)} bytes decoded before the fault",
            )
        out += chunk
        index += code
        # A full 254-byte run is a continuation, not a zero that was removed.
        if code != 0xFF and index < len(block):
            out.append(0)
    return bytes(out)


# ---------------------------------------------------------------------------
# SLIP (RFC 1055)
# ---------------------------------------------------------------------------

SLIP_END = 0xC0
SLIP_ESC = 0xDB
SLIP_ESC_END = 0xDC
SLIP_ESC_ESC = 0xDD


def slip_encode(data: bytes, *, leading_end: bool = True) -> bytes:
    """Escape a packet and wrap it in END bytes.

    RFC 1055 recommends a leading END so that line noise before the packet is
    flushed as an empty frame rather than corrupting this one.
    """
    out = bytearray()
    if leading_end:
        out.append(SLIP_END)
    for byte in data:
        if byte == SLIP_END:
            out += bytes((SLIP_ESC, SLIP_ESC_END))
        elif byte == SLIP_ESC:
            out += bytes((SLIP_ESC, SLIP_ESC_ESC))
        else:
            out.append(byte)
    out.append(SLIP_END)
    return bytes(out)


def slip_decode(frame: bytes) -> bytes:
    """Unescape one SLIP frame, with or without its END bytes.

    An escape followed by anything other than ESC_END/ESC_ESC is undefined in
    RFC 1055.  It is rejected rather than passed through, because on a real
    link it means the frame boundary was lost and the bytes after it belong
    to a different packet.
    """
    body = frame.strip(bytes((SLIP_END,)))
    out = bytearray()
    index = 0
    while index < len(body):
        byte = body[index]
        if byte == SLIP_ESC:
            if index + 1 >= len(body):
                raise InvalidRequest(
                    "SLIP frame ends with a dangling escape byte",
                    details={"offset": index},
                    preserved=f"{len(out)} bytes decoded before the fault",
                )
            following = body[index + 1]
            if following == SLIP_ESC_END:
                out.append(SLIP_END)
            elif following == SLIP_ESC_ESC:
                out.append(SLIP_ESC)
            else:
                raise InvalidRequest(
                    f"invalid SLIP escape 0xDB 0x{following:02X} at offset {index}",
                    details={"offset": index, "byte": following},
                    preserved=f"{len(out)} bytes decoded before the fault",
                )
            index += 2
            continue
        if byte == SLIP_END:
            raise InvalidRequest(
                f"unescaped END byte at offset {index} inside a SLIP frame",
                details={"offset": index},
                preserved=f"{len(out)} bytes decoded before the fault",
            )
        out.append(byte)
        index += 1
    return bytes(out)


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def epoch_to_iso(value: float, *, unit: str = "s") -> str:
    """Render an epoch value as ISO-8601 UTC with microsecond resolution."""
    if unit not in EPOCH_UNITS:
        raise InvalidRequest(
            f"unknown epoch unit {unit!r}",
            details={"known": sorted(EPOCH_UNITS)},
        )
    seconds = value * EPOCH_UNITS[unit] / 1e9
    try:
        moment = datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise InvalidRequest(
            f"{value} {unit} is not a representable date",
            details={"value": value, "unit": unit},
        ) from exc
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def iso_to_epoch(text: str, *, unit: str = "s") -> int:
    """Parse an ISO-8601 timestamp into an epoch value.

    A timestamp with no timezone is read as UTC.  Guessing local time on a
    field device whose clock may never have been set is how captures end up
    hours away from the log they need to line up with.
    """
    if unit not in EPOCH_UNITS:
        raise InvalidRequest(f"unknown epoch unit {unit!r}", details={"known": sorted(EPOCH_UNITS)})
    cleaned = text.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise InvalidRequest(
            f"{text!r} is not an ISO-8601 timestamp",
            details={"input": text, "example": "2026-08-20T14:03:00Z"},
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return round(moment.timestamp() * 1e9 / EPOCH_UNITS[unit])


#: Dates a field instrument could plausibly be reading.  Outside this window
#: a number is almost certainly not a timestamp in that unit.
_PLAUSIBLE_EPOCH_S = (946_684_800, 4_102_444_800)  # 2000-01-01 .. 2100-01-01


def guess_epoch_units(value: int) -> list[str]:
    """Which epoch units would make ``value`` a plausible date?"""
    low, high = _PLAUSIBLE_EPOCH_S
    return [unit for unit, scale in EPOCH_UNITS.items() if low <= value * scale / 1e9 <= high]


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

#: ``unit -> (dimension, factor to the dimension's base unit)``.  Case is
#: significant throughout: ``mV`` and ``MV`` are nine orders of magnitude
#: apart and an instrument must never conflate them.
_LINEAR_UNITS: dict[str, tuple[str, float]] = {
    # length, base metre
    "m": ("length", 1.0),
    "cm": ("length", 1e-2),
    "mm": ("length", 1e-3),
    "um": ("length", 1e-6),
    "in": ("length", 0.0254),
    "mil": ("length", 2.54e-5),
    "ft": ("length", 0.3048),
    # time, base second
    "s": ("time", 1.0),
    "ms": ("time", 1e-3),
    "us": ("time", 1e-6),
    "ns": ("time", 1e-9),
    "min": ("time", 60.0),
    "h": ("time", 3600.0),
    # frequency, base hertz
    "Hz": ("frequency", 1.0),
    "kHz": ("frequency", 1e3),
    "MHz": ("frequency", 1e6),
    "GHz": ("frequency", 1e9),
    # voltage, base volt
    "V": ("voltage", 1.0),
    "mV": ("voltage", 1e-3),
    "uV": ("voltage", 1e-6),
    "kV": ("voltage", 1e3),
    # current, base ampere
    "A": ("current", 1.0),
    "mA": ("current", 1e-3),
    "uA": ("current", 1e-6),
    "nA": ("current", 1e-9),
    # power, base watt
    "W": ("power", 1.0),
    "mW": ("power", 1e-3),
    "uW": ("power", 1e-6),
    "kW": ("power", 1e3),
    # resistance, base ohm.  Spelled out because a Pi console is not
    # guaranteed to render the omega glyph.
    "ohm": ("resistance", 1.0),
    "mohm": ("resistance", 1e-3),
    "kohm": ("resistance", 1e3),
    "Mohm": ("resistance", 1e6),
    # capacitance, base farad
    "F": ("capacitance", 1.0),
    "uF": ("capacitance", 1e-6),
    "nF": ("capacitance", 1e-9),
    "pF": ("capacitance", 1e-12),
    # pressure, base pascal
    "Pa": ("pressure", 1.0),
    "kPa": ("pressure", 1e3),
    "bar": ("pressure", 1e5),
    "mbar": ("pressure", 1e2),
    "psi": ("pressure", 6894.757293168361),
    # mass, base gram
    "g": ("mass", 1.0),
    "kg": ("mass", 1e3),
    "mg": ("mass", 1e-3),
    "oz": ("mass", 28.349523125),
    "lb": ("mass", 453.59237),
    # data, base byte.  Decimal and binary multiples are both listed on
    # purpose: a datasheet's "kB" and a filesystem's "KiB" differ by 2.4%.
    "B": ("data", 1.0),
    "kB": ("data", 1e3),
    "MB": ("data", 1e6),
    "GB": ("data", 1e9),
    "KiB": ("data", 1024.0),
    "MiB": ("data", 1024.0**2),
    "GiB": ("data", 1024.0**3),
    "bit": ("data", 0.125),
    # angle, base radian
    "rad": ("angle", 1.0),
    "deg": ("angle", math.pi / 180.0),
}

_TEMPERATURE_UNITS = ("C", "F", "K")


def list_units() -> dict[str, list[str]]:
    """Known units grouped by dimension, for a picker or a help screen."""
    grouped: dict[str, list[str]] = {}
    for unit, (dimension, _factor) in _LINEAR_UNITS.items():
        grouped.setdefault(dimension, []).append(unit)
    grouped["temperature"] = list(_TEMPERATURE_UNITS)
    grouped["power_ratio"] = ["dBm"]
    return {dimension: sorted(units) for dimension, units in sorted(grouped.items())}


def _to_kelvin(value: float, unit: str) -> float:
    if unit == "K":
        return value
    if unit == "C":
        return value + 273.15
    return (value - 32.0) * 5.0 / 9.0 + 273.15


def _from_kelvin(kelvin: float, unit: str) -> float:
    if unit == "K":
        return kelvin
    if unit == "C":
        return kelvin - 273.15
    return (kelvin - 273.15) * 9.0 / 5.0 + 32.0


def convert_unit(value: float, from_unit: str, to_unit: str) -> dict[str, Any]:
    """Convert between units of the same dimension.

    Cross-dimension requests are refused with the list of units that would
    have worked, rather than being silently reinterpreted.  ``dBm`` converts
    to and from absolute power because that pair genuinely is one dimension
    seen two ways; frequency and period are *not* treated as convertible,
    because they are reciprocal rather than proportional and quietly
    inverting a number is not a conversion.
    """
    if from_unit in _TEMPERATURE_UNITS or to_unit in _TEMPERATURE_UNITS:
        if from_unit not in _TEMPERATURE_UNITS or to_unit not in _TEMPERATURE_UNITS:
            raise InvalidRequest(
                f"cannot convert {from_unit} to {to_unit}: temperature is its own dimension",
                details={"temperature_units": list(_TEMPERATURE_UNITS)},
            )
        converted = _from_kelvin(_to_kelvin(value, from_unit), to_unit)
        return {
            "value": converted,
            "unit": to_unit,
            "dimension": "temperature",
            "input": {"value": value, "unit": from_unit},
        }

    if "dBm" in (from_unit, to_unit):
        return _convert_dbm(value, from_unit, to_unit)

    source = _LINEAR_UNITS.get(from_unit)
    target = _LINEAR_UNITS.get(to_unit)
    if source is None or target is None:
        unknown = from_unit if source is None else to_unit
        raise InvalidRequest(
            f"unknown unit {unknown!r} (unit names are case-sensitive: mV is not MV)",
            details={"units": list_units()},
        )
    if source[0] != target[0]:
        raise InvalidRequest(
            f"cannot convert {from_unit} ({source[0]}) to {to_unit} ({target[0]})",
            details={"same_dimension": list_units()[source[0]]},
        )
    return {
        "value": value * source[1] / target[1],
        "unit": to_unit,
        "dimension": source[0],
        "input": {"value": value, "unit": from_unit},
    }


def _convert_dbm(value: float, from_unit: str, to_unit: str) -> dict[str, Any]:
    """dBm is logarithmic power referenced to one milliwatt."""
    if from_unit == to_unit:
        return {
            "value": value,
            "unit": to_unit,
            "dimension": "power_ratio",
            "input": {"value": value, "unit": from_unit},
        }
    if from_unit == "dBm":
        watts = 10.0 ** (value / 10.0) / 1000.0
        target = _LINEAR_UNITS.get(to_unit)
        if target is None or target[0] != "power":
            raise InvalidRequest(
                f"dBm converts to absolute power, not to {to_unit}",
                details={"power_units": list_units()["power"]},
            )
        return {
            "value": watts / target[1],
            "unit": to_unit,
            "dimension": "power",
            "input": {"value": value, "unit": from_unit},
        }
    source = _LINEAR_UNITS.get(from_unit)
    if source is None or source[0] != "power":
        raise InvalidRequest(
            f"only absolute power converts to dBm, not {from_unit}",
            details={"power_units": list_units()["power"]},
        )
    watts = value * source[1]
    if watts <= 0:
        raise InvalidRequest(
            "dBm is undefined for zero or negative power",
            details={"value": value, "unit": from_unit},
        )
    return {
        "value": 10.0 * math.log10(watts * 1000.0),
        "unit": "dBm",
        "dimension": "power_ratio",
        "input": {"value": value, "unit": from_unit},
    }


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def hash_bytes(data: bytes) -> dict[str, Any]:
    """Digests an engineer actually compares against a vendor's release notes.

    MD5 is included because firmware vendors still publish it, and marked
    ``usedforsecurity=False`` so it works on a FIPS-restricted host: this is
    file identity, not authentication.
    """
    return {
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "md5": hashlib.md5(data, usedforsecurity=False).hexdigest(),
        "crc32": f"0x{crc('crc32', data):08X}",
    }


# ---------------------------------------------------------------------------
# Intel HEX
# ---------------------------------------------------------------------------

_IHEX_DATA = 0x00
_IHEX_EOF = 0x01
_IHEX_EXT_SEGMENT = 0x02
_IHEX_START_SEGMENT = 0x03
_IHEX_EXT_LINEAR = 0x04
_IHEX_START_LINEAR = 0x05


def parse_intel_hex(text: str) -> dict[str, Any]:
    """Parse an Intel HEX file into address segments and integrity counts.

    Uses the ``intelhex`` package when it is installed and a native parser
    otherwise, because a field device must be able to answer "what address
    does this image land at?" with nothing downloaded.  Either way the record
    checksums are verified and reported: a HEX file with a bad checksum is a
    HEX file that a programmer may refuse halfway through a write.
    """
    native = _parse_intel_hex_native(text)
    try:
        from intelhex import IntelHex  # type: ignore[import-not-found]
    except ImportError:
        return {**native, "parser": "native"}

    try:
        image = IntelHex()
        image.loadhex(io.StringIO(text))
    except Exception as exc:  # noqa: BLE001 - third-party parser, any failure is a parse failure
        # The native result is still good evidence, so report both rather
        # than losing the analysis to an optional dependency's opinion.
        return {**native, "parser": "native", "intelhex_error": str(exc)}
    segments = [
        {"start": f"0x{start:08X}", "end": f"0x{end:08X}", "bytes": end - start}
        for start, end in image.segments()
    ]
    return {
        **native,
        "parser": "intelhex",
        "segments": segments,
        "start_address": f"0x{image.minaddr():08X}" if image.segments() else None,
        "end_address": f"0x{image.maxaddr() + 1:08X}" if image.segments() else None,
    }


def _parse_intel_hex_native(text: str) -> dict[str, Any]:
    """Minimal Intel HEX reader: records, extent, contiguous segments."""
    upper = 0
    records = 0
    data_bytes = 0
    bad_checksums = 0
    saw_eof = False
    entry_point: int | None = None
    spans: list[tuple[int, int]] = []

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise InvalidRequest(
                f"line {line_no} is not an Intel HEX record",
                details={"line_no": line_no, "line": line[:60]},
            )
        try:
            body = bytes.fromhex(line[1:])
        except ValueError as exc:
            raise InvalidRequest(
                f"line {line_no} is not valid hex",
                details={"line_no": line_no, "line": line[:60]},
            ) from exc
        if len(body) < 5:
            raise InvalidRequest(
                f"line {line_no} is shorter than the smallest Intel HEX record",
                details={"line_no": line_no},
            )
        records += 1
        if (sum(body) & 0xFF) != 0:
            bad_checksums += 1
        count = body[0]
        offset = int.from_bytes(body[1:3], "big")
        record_type = body[3]
        payload = body[4 : 4 + count]

        if record_type == _IHEX_DATA:
            start = upper + offset
            spans.append((start, start + count))
            data_bytes += count
        elif record_type == _IHEX_EOF:
            saw_eof = True
        elif record_type == _IHEX_EXT_SEGMENT:
            upper = int.from_bytes(payload, "big") << 4
        elif record_type == _IHEX_EXT_LINEAR:
            upper = int.from_bytes(payload, "big") << 16
        elif record_type in (_IHEX_START_SEGMENT, _IHEX_START_LINEAR):
            entry_point = int.from_bytes(payload, "big")

    segments = _merge_spans(spans)
    return {
        "records": records,
        "data_bytes": data_bytes,
        "segments": [
            {"start": f"0x{start:08X}", "end": f"0x{end:08X}", "bytes": end - start}
            for start, end in segments
        ],
        "start_address": f"0x{segments[0][0]:08X}" if segments else None,
        "end_address": f"0x{segments[-1][1]:08X}" if segments else None,
        "entry_point": f"0x{entry_point:08X}" if entry_point is not None else None,
        "bad_checksum_records": bad_checksums,
        "checksums_valid": bad_checksums == 0,
        # A missing EOF record usually means the file was truncated in
        # transfer, which is worth knowing *before* it reaches a programmer.
        "eof_record": saw_eof,
    }


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Collapse address ranges into contiguous segments."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


# ---------------------------------------------------------------------------
# ELF
# ---------------------------------------------------------------------------

_ELF_MAGIC = b"\x7fELF"

#: e_machine values worth naming.  Anything else is reported numerically
#: rather than guessed at.
_ELF_MACHINES: dict[int, str] = {
    0x02: "SPARC",
    0x03: "x86",
    0x08: "MIPS",
    0x14: "PowerPC",
    0x28: "ARM",
    0x3E: "x86-64",
    0x53: "AVR",
    0x5A: "Xtensa (ESP)",
    0xB7: "AArch64",
    0xF3: "RISC-V",
}


def inspect_elf(data: bytes) -> dict[str, Any]:
    """Summarise an ELF image.

    Section and symbol detail needs pyelftools.  When it is not installed the
    result says so plainly — ``detail_available: false`` — and still reports
    the identity fields from the 24-byte header, which are unambiguous and
    need no dependency.  Nothing here crashes on a missing optional package.
    """
    if not data.startswith(_ELF_MAGIC):
        raise InvalidRequest(
            "not an ELF image (missing the 0x7F 'ELF' magic)",
            details={"first_bytes": data[:4].hex().upper()},
        )
    header = _elf_header(data)
    try:
        from elftools.elf.elffile import ELFFile  # type: ignore[import-not-found]
    except ImportError:
        return {
            **header,
            "detail_available": False,
            "reason": "pyelftools is not installed; install fielddeck[analysis] for sections",
        }

    try:
        elf = ELFFile(io.BytesIO(data))
        sections = [
            {
                "name": section.name,
                "type": section["sh_type"],
                "address": f"0x{section['sh_addr']:08X}",
                "size": section["sh_size"],
            }
            for section in elf.iter_sections()
            if section.name
        ]
        symbols = sum(
            section.num_symbols()
            for section in elf.iter_sections()
            if section.header["sh_type"] in ("SHT_SYMTAB", "SHT_DYNSYM")
        )
    except Exception as exc:  # noqa: BLE001 - a malformed ELF must not take the daemon down
        return {**header, "detail_available": False, "reason": f"pyelftools failed: {exc}"}

    return {
        **header,
        "detail_available": True,
        "sections": sections,
        "symbols": symbols,
        "flash_bytes": sum(
            entry["size"] for entry in sections if entry["name"] in (".text", ".rodata", ".data")
        ),
        "ram_bytes": sum(entry["size"] for entry in sections if entry["name"] in (".data", ".bss")),
    }


def _elf_header(data: bytes) -> dict[str, Any]:
    """The identity fields, read straight out of the header."""
    if len(data) < 32:
        raise InvalidRequest(
            "ELF file is shorter than its own header",
            details={"size_bytes": len(data)},
        )
    bitness = 64 if data[4] == 2 else 32
    order: ByteOrder = "little" if data[5] == 1 else "big"
    machine = int.from_bytes(data[18:20], order)
    entry_size = 8 if bitness == 64 else 4
    entry = int.from_bytes(data[24 : 24 + entry_size], order)
    return {
        "format": "elf",
        "bitness": bitness,
        "endianness": order,
        "machine": _ELF_MACHINES.get(machine, f"unknown (0x{machine:02X})"),
        "machine_id": machine,
        "type": {1: "relocatable", 2: "executable", 3: "shared", 4: "core"}.get(
            int.from_bytes(data[16:18], order), "unknown"
        ),
        "entry_point": f"0x{entry:08X}",
    }


# ---------------------------------------------------------------------------
# interpret: every plausible reading at once
# ---------------------------------------------------------------------------


def _json_float(value: float) -> float | None:
    """JSON has no NaN or infinity, and a silently-dropped one is a lie."""
    return value if math.isfinite(value) else None


def _float_note(value: float) -> str | None:
    if math.isfinite(value):
        return None
    return f"decodes to {value!r}, which JSON cannot carry; reported as null"


def _integer_readings(parse: NumberParse) -> list[Reading]:
    """Every base, width and signedness reading of one integer."""
    value = parse.value
    group = f"integer ({parse.label})"
    readings = [
        Reading(group, "decimal", value),
        Reading(group, "hexadecimal", to_base(value, 16)),
        Reading(group, "binary", to_base(value, 2)),
        Reading(group, "octal", to_base(value, 8)),
        Reading(group, "bit length", value.bit_length()),
    ]

    magnitude = abs(value)
    for width in INT_WIDTHS:
        span = 1 << width
        if magnitude >= span:
            continue
        unsigned = value & (span - 1)
        signed = unsigned - span if unsigned >= span // 2 else unsigned
        fits_unsigned = 0 <= value < span
        note = None if fits_unsigned else f"{value} wrapped into {width} bits"
        readings.append(Reading(f"as {width}-bit", "unsigned", unsigned, note))
        readings.append(Reading(f"as {width}-bit", "signed (two's complement)", signed, note))
        for endianness in ENDIANNESS:
            encoded = int_to_bytes(unsigned, width, endianness=endianness)
            readings.append(
                Reading(f"as {width}-bit", f"bytes, {endianness}-endian", encoded.hex().upper())
            )
        if width in FLOAT_WIDTHS:
            pattern = int_to_bytes(unsigned, width, endianness="big")
            decoded = bytes_to_floats(pattern, width, endianness="big")[0]
            readings.append(
                Reading(
                    f"as {width}-bit",
                    f"IEEE-754 float{width} bit pattern",
                    _json_float(decoded),
                    _float_note(decoded),
                )
            )
        if width == 8 and 0x20 <= unsigned < 0x7F:
            readings.append(Reading("as 8-bit", "ASCII character", chr(unsigned)))

    for unit in guess_epoch_units(value):
        readings.append(
            Reading(
                "as a timestamp",
                f"epoch {unit}",
                epoch_to_iso(value, unit=unit),
                "plausible only because the result lands between 2000 and 2100",
            )
        )
    return readings


def _byte_readings(data: bytes, origin: str) -> list[Reading]:
    """Readings of a byte string, whatever produced it."""
    group = f"bytes ({origin})"
    readings = [
        Reading(group, "length", len(data)),
        Reading(group, "hex", data.hex().upper()),
        Reading(group, "printable", printable_text(data)),
        Reading(group, "base64", base64_encode(data)),
    ]
    try:
        readings.append(Reading(group, "utf-8", data.decode("utf-8")))
    except UnicodeDecodeError as exc:
        readings.append(
            Reading(group, "utf-8", None, f"not valid UTF-8 (byte {exc.start} of {len(data)})")
        )

    for width in INT_WIDTHS:
        if len(data) != width // 8:
            continue
        for endianness in ENDIANNESS:
            readings.append(
                Reading(
                    f"{origin} as {width}-bit",
                    f"unsigned, {endianness}-endian",
                    bytes_to_ints(data, width, endianness=endianness)[0],
                )
            )
            readings.append(
                Reading(
                    f"{origin} as {width}-bit",
                    f"signed, {endianness}-endian",
                    bytes_to_ints(data, width, endianness=endianness, signed=True)[0],
                )
            )
        if width in FLOAT_WIDTHS:
            for endianness in ENDIANNESS:
                decoded = bytes_to_floats(data, width, endianness=endianness)[0]
                readings.append(
                    Reading(
                        f"{origin} as {width}-bit",
                        f"float{width}, {endianness}-endian",
                        _json_float(decoded),
                        _float_note(decoded),
                    )
                )
    return readings


def _looks_like_base64(text: str) -> bool:
    stripped = text.strip()
    return (
        len(stripped) >= 8
        and len(stripped) % 4 == 0
        and bool(_BASE64_RE.fullmatch(stripped))
        and not _HEX_CLEAN.fullmatch(stripped)
    )


def interpret(value: str, *, max_bytes: int = 4096) -> dict[str, Any]:
    """Return every plausible reading of ``value`` at once.

    This is what the HMI's conversion screen renders: one input box, a table
    of simultaneous interpretations, and an explicit note wherever the input
    was ambiguous.  Nothing is chosen on the engineer's behalf.
    """
    raw = value.strip()
    if not raw:
        raise InvalidRequest("nothing to interpret", details={"input": value})

    parsed_as: list[str] = []
    readings: list[Reading] = []
    notes: list[str] = []

    numbers = number_candidates(raw)
    for parse in numbers:
        parsed_as.append(parse.label)
        readings.extend(_integer_readings(parse))
    if len(numbers) > 1:
        shown = ", ".join(f"{parse.label} = {parse.value}" for parse in numbers)
        notes.append(f"{raw!r} is ambiguous; every reading is shown ({shown})")

    hex_match = _HEX_CLEAN.fullmatch(raw.replace(" ", "").replace("_", ""))
    if hex_match and len(hex_match.group(2)) % 2 == 0:
        data = parse_hex_bytes(raw)[:max_bytes]
        parsed_as.append("hex byte string")
        readings.extend(_byte_readings(data, "hex"))

    if _looks_like_base64(raw):
        try:
            decoded = base64_decode(raw)[:max_bytes]
        except InvalidRequest:
            pass
        else:
            parsed_as.append("base64")
            readings.extend(_byte_readings(decoded, "base64-decoded"))

    if not any(parse.explicit for parse in numbers):
        # Anything can be read as text.  An explicitly prefixed literal such
        # as 0xDEADBEEF is the one case where the operator has already said
        # what they meant, so the text reading would be noise.
        encoded = raw.encode("utf-8")[:max_bytes]
        parsed_as.append("text")
        readings.extend(_byte_readings(encoded, "utf-8 text"))
        if len(raw) == 1:
            readings.append(Reading("text", "code point", f"U+{ord(raw):04X}"))

    try:
        epoch_ns = iso_to_epoch(raw, unit="ns")
    except InvalidRequest:
        pass
    else:
        parsed_as.append("ISO-8601 timestamp")
        for unit, scale in EPOCH_UNITS.items():
            readings.append(Reading("as a timestamp", f"epoch {unit}", epoch_ns // scale))

    if not readings:  # pragma: no cover - every branch above covers text
        raise InvalidRequest(f"no interpretation of {raw!r} was possible", details={"input": raw})

    return {
        "input": raw,
        "parsed_as": parsed_as,
        "ambiguous": len(numbers) > 1,
        "readings": [reading.as_dict() for reading in readings],
        "count": len(readings),
        "notes": notes,
    }
