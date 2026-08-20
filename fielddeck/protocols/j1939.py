"""SAE J1939 identifier decoding.

J1939 rides on 29-bit CAN identifiers and packs four things into them:
priority, the parameter group number, the destination (sometimes), and the
source address.  Decoding that split is the difference between "0x18FEE500
appears 10 times a second" and "engine hours, from the engine ECU".

The PGN table here is deliberately short and only contains parameter groups
that are standardised in J1939-71.  Manufacturer-proprietary PGNs (0xEF00,
0xFF00-0xFFFF) are reported as proprietary rather than guessed at, because
the same proprietary PGN means different things on two different vehicles and
a confident wrong label is worse than no label.

Signal scaling for a small set of well-known parameters is included with its
units and offsets spelled out.  Anything not in the table is returned as raw
bytes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

__all__ = ["J1939Id", "decode_frame", "decode_id", "summarize"]

#: PDU2 (broadcast) format starts here; below it PF is a destination address.
_PDU2_THRESHOLD = 240

#: Standardised parameter groups from J1939-71 worth naming.
PGN_NAMES: dict[int, str] = {
    0x00000: "TSC1 - Torque/Speed Control 1",
    0x0EE00: "Address Claimed / Cannot Claim",
    0x0EA00: "Request",
    0x0EB00: "Transport Protocol - Data Transfer",
    0x0EC00: "Transport Protocol - Connection Management",
    0x0F004: "EEC1 - Electronic Engine Controller 1",
    0x0F003: "EEC2 - Electronic Engine Controller 2",
    0x0FEE5: "Engine Hours / Revolutions",
    0x0FEE9: "Fuel Consumption",
    0x0FEEE: "Engine Temperature 1",
    0x0FEEF: "Engine Fluid Level/Pressure 1",
    0x0FEF1: "CCVS1 - Cruise Control/Vehicle Speed",
    0x0FEF2: "LFE - Fuel Economy",
    0x0FEF5: "Ambient Conditions",
    0x0FEF6: "Inlet/Exhaust Conditions 1",
    0x0FEF7: "Vehicle Electrical Power 1",
    0x0FECA: "DM1 - Active Diagnostic Trouble Codes",
    0x0FECB: "DM2 - Previously Active DTCs",
    0x0FECC: "DM3 - Diagnostic Data Clear",
    0x0FEE6: "Time/Date",
    0x0FEE8: "Vehicle Distance",
    0x0FEEC: "Vehicle Identification",
    0x0FD7D: "Aftertreatment 1 Intake Gas 1",
}

#: A handful of parameters with published scaling, so a decode shows units.
#: (pgn, start byte, length, resolution, offset, unit, name)
SIGNALS: tuple[tuple[int, int, int, float, float, str, str], ...] = (
    (0x0F004, 3, 2, 0.125, 0.0, "rpm", "EngineSpeed"),
    (0x0F004, 2, 1, 1.0, -125.0, "%", "ActualEnginePercentTorque"),
    (0x0FEE5, 0, 4, 0.05, 0.0, "h", "TotalEngineHours"),
    (0x0FEEE, 0, 1, 1.0, -40.0, "degC", "EngineCoolantTemperature"),
    (0x0FEEE, 1, 1, 1.0, -40.0, "degC", "FuelTemperature"),
    (0x0FEF1, 1, 2, 1 / 256, 0.0, "km/h", "WheelBasedVehicleSpeed"),
    (0x0FEF5, 3, 2, 0.03125, -273.0, "degC", "AmbientAirTemperature"),
    (0x0FEF7, 4, 2, 0.05, 0.0, "V", "BatteryPotential"),
    (0x0FEEF, 3, 1, 4.0, 0.0, "kPa", "EngineOilPressure"),
    (0x0FEEF, 7, 1, 0.4, 0.0, "%", "EngineOilLevel"),
)

#: J1939 uses the top of each range to mean "not available" and "error", so a
#: raw 0xFFFF must never be scaled into a plausible-looking reading.
_NOT_AVAILABLE = {1: 0xFF, 2: 0xFFFF, 4: 0xFFFFFFFF}
_ERROR_INDICATOR = {1: 0xFE, 2: 0xFEFF, 4: 0xFEFFFFFF}


@dataclass(frozen=True, slots=True)
class J1939Id:
    """The four fields packed into a 29-bit J1939 identifier."""

    raw: int
    priority: int
    extended_data_page: int
    data_page: int
    pdu_format: int
    pdu_specific: int
    source_address: int
    pgn: int

    @property
    def is_broadcast(self) -> bool:
        """PDU2 is broadcast; PDU1 carries a destination in PDU-specific."""
        return self.pdu_format >= _PDU2_THRESHOLD

    @property
    def destination_address(self) -> int | None:
        return None if self.is_broadcast else self.pdu_specific

    @property
    def name(self) -> str:
        known = PGN_NAMES.get(self.pgn)
        if known:
            return known
        # Proprietary ranges: say proprietary, do not invent a meaning.
        if self.pdu_format in (0xEF, 0xFF) or 0x0FF00 <= self.pgn <= 0x0FFFF:
            return f"proprietary (PGN {self.pgn})"
        return f"unknown (PGN {self.pgn})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "can_id": f"0x{self.raw:08X}",
            "priority": self.priority,
            "pgn": self.pgn,
            "pgn_hex": f"0x{self.pgn:05X}",
            "name": self.name,
            "source_address": self.source_address,
            "destination_address": self.destination_address,
            "broadcast": self.is_broadcast,
            "data_page": self.data_page,
        }


def decode_id(can_id: int) -> J1939Id:
    """Split a 29-bit identifier into its J1939 fields."""
    raw = can_id & 0x1FFFFFFF
    source_address = raw & 0xFF
    pdu_specific = (raw >> 8) & 0xFF
    pdu_format = (raw >> 16) & 0xFF
    data_page = (raw >> 24) & 0x01
    extended_data_page = (raw >> 25) & 0x01
    priority = (raw >> 26) & 0x07

    # For PDU2 the PDU-specific byte is part of the PGN (group extension);
    # for PDU1 it is a destination address and the PGN's low byte is zero.
    if pdu_format >= _PDU2_THRESHOLD:
        pgn = (extended_data_page << 17) | (data_page << 16) | (pdu_format << 8) | pdu_specific
    else:
        pgn = (extended_data_page << 17) | (data_page << 16) | (pdu_format << 8)

    return J1939Id(
        raw=raw,
        priority=priority,
        extended_data_page=extended_data_page,
        data_page=data_page,
        pdu_format=pdu_format,
        pdu_specific=pdu_specific,
        source_address=source_address,
        pgn=pgn,
    )


def _extract(data: bytes, start: int, length: int) -> int | None:
    """Little-endian unsigned field, or None if it runs past the payload."""
    if start + length > len(data):
        return None
    return int.from_bytes(data[start : start + length], "little")


def decode_frame(can_id: int, data: bytes | str) -> dict[str, Any]:
    """Decode one extended CAN frame as J1939."""
    if isinstance(data, str):
        data = bytes.fromhex(data.replace(" ", ""))
    identifier = decode_id(can_id)
    result = identifier.as_dict()
    result["data_hex"] = data.hex().upper()

    signals: list[dict[str, Any]] = []
    for pgn, start, length, resolution, offset, unit, name in SIGNALS:
        if pgn != identifier.pgn:
            continue
        raw = _extract(data, start, length)
        if raw is None:
            continue
        if raw == _NOT_AVAILABLE.get(length):
            signals.append({"name": name, "value": None, "unit": unit, "state": "not available"})
            continue
        if raw == _ERROR_INDICATOR.get(length):
            signals.append({"name": name, "value": None, "unit": unit, "state": "error"})
            continue
        signals.append(
            {
                "name": name,
                "value": round(raw * resolution + offset, 4),
                "unit": unit,
                "raw": raw,
                "state": "ok",
            }
        )
    result["signals"] = signals
    if not signals and identifier.pgn not in PGN_NAMES:
        result["note"] = (
            "no published scaling for this parameter group; raw bytes preserved. "
            "Load a DBC to decode vendor signals."
        )
    return result


def summarize(frames: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Group a capture by PGN and source address.

    The per-source breakdown is the useful view on a vehicle bus: it tells you
    which ECU stopped talking, which is usually the question.
    """
    by_pgn: dict[int, dict[str, Any]] = {}
    sources: dict[int, int] = {}

    for frame in frames:
        raw_id = frame["can_id"]
        can_id = int(raw_id, 16) if isinstance(raw_id, str) else int(raw_id)
        if not frame.get("extended", True) and can_id <= 0x7FF:
            # An 11-bit id is not J1939; skip rather than mis-decode it.
            continue
        identifier = decode_id(can_id)
        entry = by_pgn.setdefault(
            identifier.pgn,
            {
                "pgn": identifier.pgn,
                "pgn_hex": f"0x{identifier.pgn:05X}",
                "name": identifier.name,
                "count": 0,
                "sources": set(),
                "priority": identifier.priority,
            },
        )
        entry["count"] += 1
        entry["sources"].add(identifier.source_address)
        sources[identifier.source_address] = sources.get(identifier.source_address, 0) + 1

    rows = []
    for entry in sorted(by_pgn.values(), key=lambda item: -item["count"]):
        rows.append({**entry, "sources": sorted(entry["sources"])})
    return {
        "parameter_groups": rows,
        "distinct_pgns": len(rows),
        "source_addresses": [
            {"address": address, "frames": count}
            for address, count in sorted(sources.items(), key=lambda item: -item[1])
        ],
    }
