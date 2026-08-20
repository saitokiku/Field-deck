"""Firmware file inspection.

Reading a firmware image tells you a great deal before you go anywhere near a
target: what architecture it is for, where it wants to live in memory, how big
it is, and — most usefully when something has gone wrong — whether it is
byte-for-byte the image you think it is.

Everything here is offline file analysis.  No probe is opened, no target is
powered, nothing is written.  ELF parsing uses pyelftools when it is
installed; Intel HEX and UF2 are parsed natively because both formats are
small and having them always available matters more than saving fifty lines.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from fielddeck.common.errors import InvalidRequest

__all__ = ["FirmwareInfo", "detect_format", "inspect_firmware"]

_ELF_MAGIC = b"\x7fELF"
_UF2_MAGIC = b"UF2\n"
_UF2_MAGIC_START1 = 0x9E5D5157
_PRINTABLE = re.compile(rb"[ -~]{6,}")

#: e_machine values worth naming; anything else is reported numerically
#: rather than guessed at.
_ELF_MACHINES = {
    0x28: "ARM",
    0xB7: "AArch64",
    0x03: "x86",
    0x3E: "x86-64",
    0xF3: "RISC-V",
    0x53: "AVR",
    0x08: "MIPS",
    0x5A: "Xtensa (ESP)",
    0x14: "PowerPC",
}


class FirmwareInfo(dict):
    """Inspection result.  A plain dict so it serialises straight to JSON."""


def detect_format(head: bytes, path: Path) -> str:
    if head.startswith(_ELF_MAGIC):
        return "elf"
    if head[:4] == _UF2_MAGIC or (
        len(head) >= 4 and int.from_bytes(head[:4], "little") == _UF2_MAGIC_START1
    ):
        return "uf2"
    if head.startswith(b":") and path.suffix.lower() in {".hex", ".ihx", ".ihex", ""}:
        return "ihex"
    if head.startswith(b"S0") or head.startswith(b"S1"):
        return "srec"
    return "bin"


def _entropy(data: bytes) -> float:
    """Shannon entropy in bits/byte.

    Near 8.0 means compressed or encrypted; a plain firmware image with lots
    of padding and repeated instruction patterns sits well below that.
    """
    if not data:
        return 0.0
    counts = Counter(data)
    total = len(data)
    return round(-sum((count / total) * math.log2(count / total) for count in counts.values()), 3)


def _parse_ihex(text: str) -> dict[str, Any]:
    """Minimal Intel HEX reader: address extent, byte count, checksum health."""
    lowest: int | None = None
    highest: int | None = None
    data_bytes = 0
    upper = 0
    records = 0
    bad_checksums = 0
    entry_point: int | None = None

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise InvalidRequest(
                f"line {line_no} is not an Intel HEX record", details={"line": line[:60]}
            )
        try:
            body = bytes.fromhex(line[1:])
        except ValueError as exc:
            raise InvalidRequest(
                f"line {line_no} is not valid hex", details={"line": line[:60]}
            ) from exc
        if len(body) < 5:
            raise InvalidRequest(f"line {line_no} is too short to be a record")
        records += 1
        if (sum(body) & 0xFF) != 0:
            bad_checksums += 1
        count, offset, record_type = body[0], int.from_bytes(body[1:3], "big"), body[3]
        payload = body[4 : 4 + count]

        if record_type == 0x00:  # data
            address = upper + offset
            lowest = address if lowest is None else min(lowest, address)
            highest = max(highest or 0, address + count)
            data_bytes += count
        elif record_type == 0x02:  # extended segment address
            upper = int.from_bytes(payload, "big") << 4
        elif record_type == 0x04:  # extended linear address
            upper = int.from_bytes(payload, "big") << 16
        elif record_type in (0x03, 0x05):  # start address
            entry_point = int.from_bytes(payload, "big")

    return {
        "records": records,
        "data_bytes": data_bytes,
        "start_address": f"0x{lowest:08X}" if lowest is not None else None,
        "end_address": f"0x{highest:08X}" if highest is not None else None,
        "span_bytes": (highest - lowest) if (lowest is not None and highest is not None) else None,
        "entry_point": f"0x{entry_point:08X}" if entry_point is not None else None,
        "bad_checksum_records": bad_checksums,
        "checksums_valid": bad_checksums == 0,
    }


def _parse_uf2(data: bytes) -> dict[str, Any]:
    """UF2 is 512-byte blocks with a fixed header; parse the extent."""
    block_size = 512
    blocks = len(data) // block_size
    if blocks == 0:
        return {"blocks": 0}
    addresses: list[int] = []
    payload_bytes = 0
    family: int | None = None
    for index in range(blocks):
        block = data[index * block_size : (index + 1) * block_size]
        if int.from_bytes(block[0:4], "little") != _UF2_MAGIC_START1:
            continue
        flags = int.from_bytes(block[8:12], "little")
        addresses.append(int.from_bytes(block[12:16], "little"))
        payload_bytes += int.from_bytes(block[16:20], "little")
        if flags & 0x00002000:  # familyID present
            family = int.from_bytes(block[28:32], "little")
    return {
        "blocks": blocks,
        "payload_bytes": payload_bytes,
        "start_address": f"0x{min(addresses):08X}" if addresses else None,
        "end_address": f"0x{max(addresses):08X}" if addresses else None,
        "family_id": f"0x{family:08X}" if family is not None else None,
    }


def _parse_elf_header(data: bytes) -> dict[str, Any]:
    """Read the ELF header directly; enough to identify an image without deps."""
    if len(data) < 24:
        return {}
    bitness = 64 if data[4] == 2 else 32
    order: Literal["little", "big"] = "little" if data[5] == 1 else "big"
    machine = int.from_bytes(data[18:20], order)
    entry_offset = 24
    entry_size = 8 if bitness == 64 else 4
    entry = int.from_bytes(data[entry_offset : entry_offset + entry_size], order)
    return {
        "bitness": bitness,
        "endianness": order,
        "machine": _ELF_MACHINES.get(machine, f"unknown (0x{machine:02X})"),
        "machine_id": machine,
        "type": {1: "relocatable", 2: "executable", 3: "shared", 4: "core"}.get(
            int.from_bytes(data[16:18], order),
            "unknown",  # type: ignore[arg-type]
        ),
        "entry_point": f"0x{entry:08X}",
    }


def _parse_elf_sections(path: Path) -> dict[str, Any]:
    """Section and symbol detail, when pyelftools is installed."""
    try:
        from elftools.elf.elffile import ELFFile  # type: ignore[import-not-found]
    except ImportError:
        return {
            "sections": None,
            "symbols": None,
            "note": "install pyelftools for section and symbol detail",
        }

    with path.open("rb") as handle:
        elf = ELFFile(handle)
        sections = [
            {
                "name": section.name,
                "type": section["sh_type"],
                "address": f"0x{section['sh_addr']:08X}",
                "size": section["sh_size"],
                "flags": section["sh_flags"],
            }
            for section in elf.iter_sections()
            if section.name
        ]
        symbol_count = 0
        for section in elf.iter_sections():
            if section.header["sh_type"] in ("SHT_SYMTAB", "SHT_DYNSYM"):
                symbol_count += section.num_symbols()
        flash_size = sum(
            entry["size"] for entry in sections if entry["name"] in (".text", ".rodata", ".data")
        )
        ram_size = sum(entry["size"] for entry in sections if entry["name"] in (".data", ".bss"))
        return {
            "sections": sections,
            "symbols": symbol_count,
            "flash_bytes": flash_size,
            "ram_bytes": ram_size,
        }


def inspect_firmware(path: Path, *, strings_limit: int = 20) -> FirmwareInfo:
    """Identify and summarise a firmware image without touching a target."""
    if not path.is_file():
        raise InvalidRequest(f"no firmware file at {path}", details={"path": str(path)})

    data = path.read_bytes()
    head = data[:64]
    fmt = detect_format(head, path)

    digest_sha = hashlib.sha256(data).hexdigest()
    info = FirmwareInfo(
        path=str(path),
        filename=path.name,
        format=fmt,
        size_bytes=len(data),
        sha256=digest_sha,
        md5=hashlib.md5(data, usedforsecurity=False).hexdigest(),
        entropy_bits_per_byte=_entropy(data),
    )

    if fmt == "elf":
        info["elf"] = {**_parse_elf_header(data), **_parse_elf_sections(path)}
    elif fmt == "ihex":
        info["ihex"] = _parse_ihex(data.decode("ascii", errors="replace"))
    elif fmt == "uf2":
        info["uf2"] = _parse_uf2(data)
    else:
        # A raw image has no metadata at all, so the only honest things to
        # report are its shape and whatever text is embedded in it.
        blank = data.count(0xFF) + data.count(0x00)
        info["binary"] = {
            "blank_fraction": round(blank / len(data), 4) if data else 0.0,
            "looks_compressed": _entropy(data) > 7.5,
        }

    strings = [match.decode("ascii", "replace") for match in _PRINTABLE.findall(data)]
    info["strings_found"] = len(strings)
    info["strings_preview"] = strings[:strings_limit]
    info["note"] = "file inspection only; nothing was read from or written to a target"
    return info
