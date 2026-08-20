"""CRC engine.

A parameterised, table-driven CRC implementation plus a catalogue of the
families that actually show up on embedded buses.  Implemented here rather
than pulled from a dependency because protocol identification leans on
checking a capture against every candidate at once, and because "which CRC is
this?" is a question FieldDeck should answer without network access.

Every catalogue entry carries its check value — the CRC of ``b"123456789"`` —
which is verified by the test suite, so a wrong parameter set cannot ship
quietly.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

__all__ = ["CATALOGUE", "CrcModel", "crc", "crc_candidates", "list_models"]


@dataclass(frozen=True, slots=True)
class CrcModel:
    """One CRC parameterisation, in the standard Rocksoft notation."""

    name: str
    width: int
    poly: int
    init: int
    refin: bool
    refout: bool
    xorout: int
    check: int
    aliases: tuple[str, ...] = ()

    @property
    def mask(self) -> int:
        return (1 << self.width) - 1

    @property
    def byte_width(self) -> int:
        return (self.width + 7) // 8

    def compute(self, data: bytes) -> int:
        return _compute(self, data)

    def to_bytes(self, value: int, *, byteorder: str = "big") -> bytes:
        return value.to_bytes(self.byte_width, byteorder)  # type: ignore[arg-type]


def _reflect(value: int, width: int) -> int:
    out = 0
    for _ in range(width):
        out = (out << 1) | (value & 1)
        value >>= 1
    return out


@lru_cache(maxsize=64)
def _table(poly: int, width: int, refin: bool) -> tuple[int, ...]:
    """Byte-wise lookup table.  Cached: building it per call would dominate."""
    mask = (1 << width) - 1
    table: list[int] = []
    if refin:
        poly_r = _reflect(poly, width)
        for index in range(256):
            value = index
            for _ in range(8):
                value = (value >> 1) ^ (poly_r if value & 1 else 0)
            table.append(value & mask)
    else:
        top = 1 << (width - 1)
        shift = width - 8
        for index in range(256):
            value = (index << shift) & mask
            for _ in range(8):
                value = ((value << 1) ^ poly) & mask if value & top else (value << 1) & mask
            table.append(value & mask)
    return tuple(table)


def _compute(model: CrcModel, data: bytes) -> int:
    mask = model.mask
    if model.width < 8:
        # Sub-byte CRCs are rare enough that the bitwise path is fine and much
        # easier to verify against a reference than a packed table.
        value = model.init
        for byte in data:
            current = _reflect(byte, 8) if model.refin else byte
            for bit in range(7, -1, -1):
                top = (value >> (model.width - 1)) & 1
                inbit = (current >> bit) & 1
                value = ((value << 1) & mask) ^ (model.poly if top ^ inbit else 0)
        if model.refout != model.refin or model.refout:
            value = _reflect(value, model.width)
        return (value ^ model.xorout) & mask

    table = _table(model.poly, model.width, model.refin)
    if model.refin:
        value = _reflect(model.init, model.width)
        for byte in data:
            value = table[(value ^ byte) & 0xFF] ^ (value >> 8)
        value &= mask
        if not model.refout:
            value = _reflect(value, model.width)
    else:
        value = model.init
        shift = model.width - 8
        for byte in data:
            value = table[((value >> shift) ^ byte) & 0xFF] ^ ((value << 8) & mask)
        if model.refout:
            value = _reflect(value, model.width)
    return (value ^ model.xorout) & mask


CATALOGUE: dict[str, CrcModel] = {
    model.name: model
    for model in (
        CrcModel("crc8", 8, 0x07, 0x00, False, False, 0x00, 0xF4, ("crc-8", "crc8-smbus")),
        CrcModel(
            "crc8-maxim", 8, 0x31, 0x00, True, True, 0x00, 0xA1, ("crc8-dallas", "crc8-1wire")
        ),
        CrcModel("crc8-sae-j1850", 8, 0x1D, 0xFF, False, False, 0xFF, 0x4B, ()),
        CrcModel("crc8-autosar", 8, 0x2F, 0xFF, False, False, 0xFF, 0xDF, ()),
        CrcModel(
            "crc16-modbus",
            16,
            0x8005,
            0xFFFF,
            True,
            True,
            0x0000,
            0x4B37,
            ("modbus", "crc16-ibm"),
        ),
        CrcModel("crc16-ccitt-false", 16, 0x1021, 0xFFFF, False, False, 0x0000, 0x29B1, ("ccitt",)),
        CrcModel("crc16-xmodem", 16, 0x1021, 0x0000, False, False, 0x0000, 0x31C3, ("xmodem",)),
        CrcModel("crc16-kermit", 16, 0x1021, 0x0000, True, True, 0x0000, 0x2189, ("kermit",)),
        CrcModel("crc16-arc", 16, 0x8005, 0x0000, True, True, 0x0000, 0xBB3D, ("arc",)),
        CrcModel("crc16-maxim", 16, 0x8005, 0x0000, True, True, 0xFFFF, 0x44C2, ()),
        CrcModel("crc16-usb", 16, 0x8005, 0xFFFF, True, True, 0xFFFF, 0xB4C8, ()),
        CrcModel("crc16-dnp", 16, 0x3D65, 0x0000, True, True, 0xFFFF, 0xEA82, ()),
        CrcModel("crc16-mcrf4xx", 16, 0x1021, 0xFFFF, True, True, 0x0000, 0x6F91, ()),
        CrcModel(
            "crc32",
            32,
            0x04C11DB7,
            0xFFFFFFFF,
            True,
            True,
            0xFFFFFFFF,
            0xCBF43926,
            ("crc32-ieee", "zip"),
        ),
        CrcModel(
            "crc32c",
            32,
            0x1EDC6F41,
            0xFFFFFFFF,
            True,
            True,
            0xFFFFFFFF,
            0xE3069283,
            ("castagnoli",),
        ),
        CrcModel(
            "crc32-bzip2", 32, 0x04C11DB7, 0xFFFFFFFF, False, False, 0xFFFFFFFF, 0xFC891918, ()
        ),
        CrcModel(
            "crc32-mpeg2", 32, 0x04C11DB7, 0xFFFFFFFF, False, False, 0x00000000, 0x0376E6E7, ()
        ),
        CrcModel("crc16-t10-dif", 16, 0x8BB7, 0x0000, False, False, 0x0000, 0xD0DB, ()),
        CrcModel("crc16-profibus", 16, 0x1DCF, 0xFFFF, False, False, 0xFFFF, 0xA819, ()),
        CrcModel("crc5-usb", 5, 0x05, 0x1F, True, True, 0x1F, 0x19, ()),
    )
}

_ALIASES: dict[str, str] = {}
for _model in CATALOGUE.values():
    for _alias in _model.aliases:
        _ALIASES[_alias] = _model.name


def get_model(name: str) -> CrcModel:
    """Look up a CRC by canonical name or alias."""
    key = name.strip().lower().replace("_", "-")
    if key in CATALOGUE:
        return CATALOGUE[key]
    if key in _ALIASES:
        return CATALOGUE[_ALIASES[key]]
    from fielddeck.common.errors import InvalidRequest

    raise InvalidRequest(
        f"unknown CRC model {name!r}",
        details={"known": sorted(CATALOGUE), "aliases": sorted(_ALIASES)},
    )


def crc(name: str, data: bytes) -> int:
    """Compute one CRC by name."""
    return get_model(name).compute(data)


def crc_candidates(
    payload: bytes,
    expected: bytes,
    *,
    byteorders: tuple[str, ...] = ("big", "little"),
) -> list[dict[str, object]]:
    """Which catalogue CRCs turn ``payload`` into ``expected``?

    This is the workhorse of protocol identification: take the bytes before
    the suspected checksum, take the suspected checksum, and find out which
    algorithms agree.  Returns every match rather than the first, because a
    two-byte trailer often matches several models and pretending otherwise is
    false precision.
    """
    matches: list[dict[str, object]] = []
    for model in CATALOGUE.values():
        if model.byte_width != len(expected):
            continue
        value = model.compute(payload)
        for order in byteorders:
            if model.to_bytes(value, byteorder=order) == expected:
                matches.append(
                    {
                        "model": model.name,
                        "byteorder": order,
                        "value": value,
                        "hex": f"0x{value:0{model.byte_width * 2}X}",
                    }
                )
                break
    return matches


def list_models() -> list[dict[str, object]]:
    return [
        {
            "name": model.name,
            "width": model.width,
            "poly": f"0x{model.poly:X}",
            "init": f"0x{model.init:X}",
            "refin": model.refin,
            "refout": model.refout,
            "xorout": f"0x{model.xorout:X}",
            "check": f"0x{model.check:X}",
            "aliases": list(model.aliases),
        }
        for model in CATALOGUE.values()
    ]
