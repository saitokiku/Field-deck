"""Structure discovery over a captured byte stream.

Nothing here transmits.  Every function takes bytes that were already
recorded and asks the questions an engineer asks with a highlighter and a
printout: which byte keeps repeating, do the gaps between repeats look like a
frame, does a field count up, and do the last two bytes of each frame happen
to be a CRC of the rest.

Two rules shape the whole module:

* **Evidence, not verdicts.**  Every result carries its counts — how many
  frames, how many matched, how many did not.  ``candidate_delimiters``
  returns candidates, not "the delimiter".  Deciding what the protocol *is*
  happens one layer up, in :mod:`fielddeck.analysis.autodetect`, and even
  there it comes with the evidence attached.
* **Bounded work.**  A capture can be tens of megabytes and this runs on a
  Pi.  Every scan has an explicit cap and reports the prefix it actually
  examined, so a slow answer never becomes a hung console.

The Modbus RTU recogniser deserves a warning.  Real RTU framing is defined by
3.5-character silences on the wire, and a byte log has no silences in it.  So
framing is recovered by walking the stream, deriving each candidate frame's
length from its function code, and keeping only the frames whose CRC-16/MODBUS
validates.  That is strong evidence when it works and honest silence when it
does not; a stream that "looks a bit like Modbus" but fails CRC is reported as
exactly that.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any, Literal

from fielddeck.analysis.crc import crc, crc_candidates
from fielddeck.common.errors import InvalidRequest

__all__ = [
    "Segmentation",
    "analyze",
    "byte_histogram",
    "candidate_delimiters",
    "candidate_frame_lengths",
    "checksum_candidates",
    "constant_fields",
    "counter_fields",
    "detect_modbus_rtu",
    "printable_ratio",
    "repeated_preambles",
    "segmentations",
    "shannon_entropy",
    "split_fixed",
    "split_on_delimiter",
    "split_on_header",
    "text_lines",
]

#: How much of a capture the O(n*k) scans look at.  A protocol that cannot be
#: recognised from 32 KiB of traffic will not be recognised from 32 MiB, and
#: the operator would have gone to make coffee waiting for it.
DEFAULT_SCAN_LIMIT = 32_768

#: Longest fixed frame the length scan considers.
MAX_FRAME_LENGTH = 256

#: Bytes that are delimiters often enough to be worth saying so.
_KNOWN_DELIMITERS: dict[int, str] = {
    0x00: "COBS / null-terminated framing",
    0x0A: "LF, line-oriented ASCII",
    0x0D: "CR, line-oriented ASCII",
    0x02: "STX",
    0x03: "ETX",
    0x7E: "HDLC / PPP flag",
    0xC0: "SLIP END",
}


# ---------------------------------------------------------------------------
# Whole-stream statistics
# ---------------------------------------------------------------------------


def byte_histogram(data: bytes, *, top: int = 8) -> dict[str, Any]:
    """Byte-frequency census, with the most common values named."""
    counts = Counter(data)
    total = len(data) or 1
    return {
        "size_bytes": len(data),
        "unique_bytes": len(counts),
        "top": [
            {
                "byte": f"0x{value:02X}",
                "value": value,
                "count": count,
                "fraction": round(count / total, 4),
            }
            for value, count in counts.most_common(top)
        ],
        "zero_fraction": round(counts.get(0, 0) / total, 4),
        "high_bit_fraction": round(
            sum(count for value, count in counts.items() if value & 0x80) / total, 4
        ),
    }


def printable_ratio(data: bytes) -> float:
    """Fraction of bytes that are printable ASCII, tab, CR or LF.

    The single most useful first question about an unknown stream: text
    protocols and binary protocols need completely different tooling.
    """
    if not data:
        return 0.0
    printable = sum(1 for byte in data if 0x20 <= byte < 0x7F or byte in (0x09, 0x0A, 0x0D))
    return round(printable / len(data), 4)


def shannon_entropy(data: bytes) -> float:
    """Entropy in bits per byte.

    Near 8.0 means compressed, encrypted or random; a framed protocol with
    constant headers and small counters sits well below that.  Low entropy is
    what makes structure discovery worth attempting at all.
    """
    if not data:
        return 0.0
    counts = Counter(data)
    total = len(data)
    entropy = -sum((c / total) * math.log2(c / total) for c in counts.values())
    # A single repeated byte gives exactly zero, which floats render as -0.0.
    return round(entropy, 3) if entropy else 0.0


def _collision_probability(data: bytes) -> float:
    """Chance two independently drawn bytes of this stream are equal.

    The baseline every repetition score is measured against.  A stream that
    is 90% zeros will "match itself" at any offset, and calling that structure
    would be the classic false positive.
    """
    if not data:
        return 0.0
    total = len(data)
    return sum((count / total) ** 2 for count in Counter(data).values())


# ---------------------------------------------------------------------------
# Delimiters, frame lengths and preambles
# ---------------------------------------------------------------------------


def _gap_stats(positions: Sequence[int]) -> dict[str, Any]:
    gaps = [later - earlier for earlier, later in pairwise(positions)]
    if not gaps:
        return {"gaps": 0, "modal_gap": None, "gap_consistency": 0.0, "mean_gap": None}
    modal_gap, modal_count = Counter(gaps).most_common(1)[0]
    return {
        "gaps": len(gaps),
        "modal_gap": modal_gap,
        "gap_consistency": round(modal_count / len(gaps), 4),
        "mean_gap": round(statistics.fmean(gaps), 2),
        "stdev_gap": round(statistics.pstdev(gaps), 2) if len(gaps) > 1 else 0.0,
    }


def candidate_delimiters(
    data: bytes, *, limit: int = 5, scan_limit: int = DEFAULT_SCAN_LIMIT
) -> list[dict[str, Any]]:
    """Byte values that plausibly separate frames, best first.

    A delimiter is judged by the regularity of the gaps between its
    occurrences, not by how often it appears: the most common byte in a
    stream is usually padding, and padding is not framing.
    """
    window = data[:scan_limit]
    if len(window) < 8:
        return []
    counts = Counter(window)
    results: list[dict[str, Any]] = []
    for value, count in counts.items():
        fraction = count / len(window)
        # Too rare to divide the stream, or so common it is the payload.
        if count < 3 or fraction > 0.30:
            continue
        positions = [index for index, byte in enumerate(window) if byte == value]
        stats = _gap_stats(positions)
        consistency = float(stats["gap_consistency"])
        # Occurrences give confidence; 20 is where more stops adding much.
        support = min(1.0, count / 20.0)
        score = consistency * support
        if value in _KNOWN_DELIMITERS:
            # A known framing byte is a genuine prior, but only worth a
            # nudge: 0x0A is also just a byte inside binary payloads.
            score = min(1.0, score + 0.10)
        if score < 0.15:
            continue
        results.append(
            {
                "byte": f"0x{value:02X}",
                "value": value,
                "count": count,
                "fraction": round(fraction, 4),
                "score": round(score, 3),
                "known_as": _KNOWN_DELIMITERS.get(value),
                **stats,
            }
        )
    results.sort(key=lambda entry: (-entry["score"], -entry["count"]))
    return results[:limit]


def candidate_frame_lengths(
    data: bytes,
    *,
    limit: int = 5,
    max_length: int = MAX_FRAME_LENGTH,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
) -> list[dict[str, Any]]:
    """Fixed frame lengths suggested by the stream repeating against itself.

    For each candidate period the fraction of positions where
    ``data[i] == data[i + period]`` is compared against the chance of two
    random bytes of this stream being equal.  A ratio well above 1 means the
    repetition is structural rather than a consequence of a skewed byte
    distribution.
    """
    window = data[:scan_limit]
    baseline = _collision_probability(window)
    if len(window) < 32 or baseline <= 0:
        return []
    results: list[dict[str, Any]] = []
    ceiling = min(max_length, len(window) // 4)
    for period in range(2, ceiling + 1):
        comparable = len(window) - period
        matches = sum(1 for a, b in zip(window, window[period:], strict=False) if a == b)
        ratio = matches / comparable
        lift = ratio / baseline
        if ratio < 0.25 or lift < 1.5:
            continue
        results.append(
            {
                "length": period,
                "match_ratio": round(ratio, 4),
                "lift_over_random": round(lift, 2),
                "positions_compared": comparable,
            }
        )
    # A true period of 8 also scores at 16, 24, ...; keep the fundamental so
    # the caller is not offered four descriptions of the same structure.
    fundamentals: list[dict[str, Any]] = []
    for entry in sorted(results, key=lambda item: item["length"]):
        harmonic = next(
            (
                base
                for base in fundamentals
                if entry["length"] % base["length"] == 0
                and entry["match_ratio"] <= base["match_ratio"] * 1.15
            ),
            None,
        )
        if harmonic is None:
            fundamentals.append(entry)
        else:
            harmonic.setdefault("harmonics", []).append(entry["length"])
    fundamentals.sort(key=lambda entry: (-entry["match_ratio"], entry["length"]))
    return fundamentals[:limit]


def repeated_preambles(
    data: bytes,
    *,
    lengths: Sequence[int] = (2, 3, 4),
    limit: int = 5,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
) -> list[dict[str, Any]]:
    """Short byte sequences that recur at regular spacing — sync words.

    Overlapping occurrences are not counted twice: ``FF FF FF FF`` is one run
    of padding, not three preambles.
    """
    window = data[:scan_limit]
    results: list[dict[str, Any]] = []
    for length in lengths:
        if len(window) < length * 4:
            continue
        counts = Counter(bytes(window[i : i + length]) for i in range(len(window) - length + 1))
        for pattern, count in counts.most_common(12):
            if count < 3 or len(set(pattern)) == 1:
                continue
            positions: list[int] = []
            index = window.find(pattern)
            while index != -1:
                positions.append(index)
                index = window.find(pattern, index + length)
            if len(positions) < 3:
                continue
            stats = _gap_stats(positions)
            consistency = float(stats["gap_consistency"])
            if consistency < 0.5:
                continue
            results.append(
                {
                    "pattern": pattern.hex().upper(),
                    "length": length,
                    "count": len(positions),
                    "first_offset": positions[0],
                    "score": round(consistency * min(1.0, len(positions) / 20.0), 3),
                    **stats,
                }
            )
    results.sort(key=lambda entry: (-entry["score"], -entry["count"]))
    return results[:limit]


# ---------------------------------------------------------------------------
# Splitting a stream into frames
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Segmentation:
    """One hypothesis about where the frame boundaries are."""

    method: str
    description: str
    frames: list[bytes] = field(default_factory=list)
    offsets: list[int] = field(default_factory=list)
    #: Bytes that fell outside any frame.  High residue means the
    #: segmentation is wrong, and saying so is the whole point.
    residue_bytes: int = 0
    total_bytes: int = 0

    @property
    def coverage(self) -> float:
        if not self.total_bytes:
            return 0.0
        return round(1.0 - self.residue_bytes / self.total_bytes, 4)

    def summary(self) -> dict[str, Any]:
        lengths = [len(frame) for frame in self.frames]
        return {
            "method": self.method,
            "description": self.description,
            "frames": len(self.frames),
            "coverage": self.coverage,
            "residue_bytes": self.residue_bytes,
            "min_length": min(lengths) if lengths else None,
            "max_length": max(lengths) if lengths else None,
            "mean_length": round(statistics.fmean(lengths), 2) if lengths else None,
        }


def split_on_delimiter(data: bytes, delimiter: int, *, keep_empty: bool = False) -> Segmentation:
    """Split on a single delimiter byte, dropping empty runs by default."""
    if not 0 <= delimiter <= 0xFF:
        raise InvalidRequest("delimiter must be a byte value", details={"delimiter": delimiter})
    frames: list[bytes] = []
    offsets: list[int] = []
    start = 0
    for index, byte in enumerate(data):
        if byte != delimiter:
            continue
        chunk = data[start:index]
        if chunk or keep_empty:
            frames.append(chunk)
            offsets.append(start)
        start = index + 1
    # The bytes after the last delimiter are an unterminated fragment, not a
    # frame: a capture almost always stops mid-transmission.
    residue = len(data) - start
    return Segmentation(
        method="delimiter",
        description=f"split on 0x{delimiter:02X}",
        frames=frames,
        offsets=offsets,
        residue_bytes=residue,
        total_bytes=len(data),
    )


def best_phase(data: bytes, length: int, *, max_frames: int = 300) -> int:
    """Which alignment makes a fixed-length split line its columns up?

    A capture almost never starts on a frame boundary — the ASCII boot banner
    before the binary traffic is the usual culprit — and a fixed-length split
    at the wrong phase scrambles every field in the frame.  The phase that
    maximises column agreement is the one that has found the real boundary.
    """
    if length <= 0:
        raise InvalidRequest("frame length must be positive", details={"length": length})
    best, best_score = 0, -1.0
    for phase in range(length):
        end = min(len(data), phase + max_frames * length)
        frames = [data[start : start + length] for start in range(phase, end - length + 1, length)]
        if len(frames) < 3:
            continue
        score = sum(
            max(Counter(frame[column] for frame in frames).values()) / len(frames)
            for column in range(length)
        )
        if score > best_score:
            best, best_score = phase, score
    return best


def split_fixed(data: bytes, length: int, *, offset: int = 0) -> Segmentation:
    """Cut the stream into fixed-length frames from ``offset``."""
    if length <= 0:
        raise InvalidRequest("frame length must be positive", details={"length": length})
    frames = [
        data[start : start + length] for start in range(offset, len(data) - length + 1, length)
    ]
    covered = len(frames) * length
    return Segmentation(
        method="fixed-length",
        description=f"{length}-byte frames from offset {offset}",
        frames=frames,
        offsets=list(range(offset, offset + covered, length)),
        residue_bytes=len(data) - covered,
        total_bytes=len(data),
    )


def split_on_header(data: bytes, header: bytes, *, length: int | None = None) -> Segmentation:
    """Cut at each occurrence of a sync word.

    With ``length`` the frame is that many bytes from the header; without it
    the frame runs to the next header, which is what you want before the
    length is known.
    """
    if not header:
        raise InvalidRequest("header must be at least one byte", details={"header": ""})
    starts: list[int] = []
    index = data.find(header)
    while index != -1:
        starts.append(index)
        index = data.find(header, index + len(header))
    frames: list[bytes] = []
    offsets: list[int] = []
    consumed = 0
    for position, start in enumerate(starts):
        end = (
            start + length
            if length is not None
            else (starts[position + 1] if position + 1 < len(starts) else len(data))
        )
        if end > len(data):
            break
        frames.append(data[start:end])
        offsets.append(start)
        consumed += end - start
    return Segmentation(
        method="header",
        description=(
            f"frames starting at {header.hex().upper()}"
            + (f", {length} bytes each" if length else ", up to the next header")
        ),
        frames=frames,
        offsets=offsets,
        residue_bytes=len(data) - consumed,
        total_bytes=len(data),
    )


def text_lines(data: bytes, *, limit: int = 2000) -> dict[str, Any]:
    """Line structure of a stream that might be text.

    Reports the dominant terminator and how uniform the lines are, which is
    what separates a chatty console from a line-oriented protocol.
    """
    crlf = data.count(b"\r\n")
    lf_total = data.count(b"\n")
    cr_total = data.count(b"\r")
    if crlf and crlf >= lf_total - crlf:
        terminator = "\r\n"
    elif lf_total:
        terminator = "\n"
    elif cr_total:
        terminator = "\r"
    else:
        return {"terminator": None, "lines": 0, "complete_lines": 0}
    parts = data.split(terminator.encode("ascii"))
    complete = parts[:-1][:limit]
    lengths = [len(part) for part in complete]
    return {
        "terminator": {"\r\n": "CRLF", "\n": "LF", "\r": "CR"}[terminator],
        "lines": len(complete),
        "complete_lines": len(complete),
        "mean_length": round(statistics.fmean(lengths), 2) if lengths else None,
        "max_length": max(lengths) if lengths else None,
        "printable_lines": sum(1 for part in complete if printable_ratio(part) > 0.95),
        "preview": [part.decode("ascii", "replace") for part in complete[:5]],
    }


def segmentations(data: bytes, *, limit: int = 4) -> list[Segmentation]:
    """Plausible frame segmentations of a stream, best first.

    Built from the delimiter, preamble and fixed-length candidates, then
    ranked by coverage and by how uniform the resulting frames are.  Several
    are returned because more than one can be right — a length-prefixed
    protocol inside COBS framing is two true answers at once.
    """
    found: list[Segmentation] = []
    lengths = candidate_frame_lengths(data)

    for preamble in repeated_preambles(data)[:2]:
        header = bytes.fromhex(preamble["pattern"])
        modal_gap = preamble["modal_gap"]
        fixed_length = modal_gap if isinstance(modal_gap, int) and 2 <= modal_gap <= 4096 else None
        found.append(split_on_header(data, header, length=fixed_length))

    for delimiter in candidate_delimiters(data)[:2]:
        found.append(split_on_delimiter(data, int(delimiter["value"])))

    for entry in lengths[:2]:
        length = int(entry["length"])
        found.append(split_fixed(data, length, offset=best_phase(data, length)))

    def uniformity(segmentation: Segmentation) -> float:
        """Fraction of frames sharing the most common length."""
        if not segmentation.frames:
            return 0.0
        sizes = Counter(len(frame) for frame in segmentation.frames)
        return max(sizes.values()) / len(segmentation.frames)

    def rank(segmentation: Segmentation) -> float:
        # Coverage alone would always prefer a split that swallows the whole
        # stream, including the ASCII banner that is not part of the framing
        # at all.  Uniform frame lengths are the stronger signal.
        support = min(1.0, len(segmentation.frames) / 20.0)
        return segmentation.coverage * uniformity(segmentation) * support

    viable = [item for item in found if len(item.frames) >= 3]
    viable.sort(key=lambda item: (-rank(item), -len(item.frames)))
    return viable[:limit]


# ---------------------------------------------------------------------------
# Field discovery inside a set of frames
# ---------------------------------------------------------------------------


def _aligned(frames: Sequence[bytes], *, max_frames: int = 500) -> tuple[list[bytes], int]:
    """The frames worth comparing field-by-field, and their common length."""
    usable = [frame for frame in frames if frame][:max_frames]
    if len(usable) < 3:
        return [], 0
    return usable, min(len(frame) for frame in usable)


def constant_fields(frames: Sequence[bytes]) -> list[dict[str, Any]]:
    """Byte positions that never change — headers, addresses, type codes."""
    usable, width = _aligned(frames)
    results: list[dict[str, Any]] = []
    for position in range(width):
        values = Counter(frame[position] for frame in usable)
        value, count = values.most_common(1)[0]
        if count == len(usable):
            results.append(
                {
                    "offset": position,
                    "value": f"0x{value:02X}",
                    "frames": count,
                    "constant": True,
                }
            )
        elif count / len(usable) >= 0.8:
            results.append(
                {
                    "offset": position,
                    "value": f"0x{value:02X}",
                    "frames": count,
                    "constant": False,
                    "dominant_fraction": round(count / len(usable), 3),
                    "distinct_values": len(values),
                }
            )
    return results


def counter_fields(frames: Sequence[bytes]) -> list[dict[str, Any]]:
    """Positions that step by a constant amount from frame to frame.

    Both 8-bit and 16-bit counters are checked, wrapping included, because a
    rolling counter is the single most useful field to find: it tells you the
    frame order, and it tells you when the capture dropped something.
    """
    usable, width = _aligned(frames)
    if not usable:
        return []
    results: list[dict[str, Any]] = []

    def score(values: Sequence[int], modulus: int) -> tuple[int, float] | None:
        deltas = [(later - earlier) % modulus for earlier, later in pairwise(values)]
        if not deltas:
            return None
        delta, count = Counter(deltas).most_common(1)[0]
        if delta == 0:
            return None
        consistency = count / len(deltas)
        return (delta, consistency) if consistency >= 0.6 else None

    for position in range(width):
        result = score([frame[position] for frame in usable], 256)
        if result is not None:
            delta, consistency = result
            results.append(
                {
                    "offset": position,
                    "width": 8,
                    "step": delta,
                    "consistency": round(consistency, 3),
                    "frames": len(usable),
                    "gaps": round((1.0 - consistency) * (len(usable) - 1)),
                }
            )
    orders: tuple[Literal["big", "little"], ...] = ("big", "little")
    for position in range(width - 1):
        for order in orders:
            values = [int.from_bytes(frame[position : position + 2], order) for frame in usable]
            result = score(values, 65536)
            if result is None:
                continue
            delta, consistency = result
            # An 8-bit counter makes its own 16-bit window look like a
            # counter too; only report the wider field if it is at least as
            # consistent as the byte already claimed.
            byte_claim = next(
                (
                    entry
                    for entry in results
                    if entry["width"] == 8 and entry["offset"] in (position, position + 1)
                ),
                None,
            )
            if byte_claim is not None and byte_claim["consistency"] >= consistency:
                continue
            results.append(
                {
                    "offset": position,
                    "width": 16,
                    "endianness": order,
                    "step": delta,
                    "consistency": round(consistency, 3),
                    "frames": len(usable),
                }
            )
    results.sort(key=lambda entry: (-entry["consistency"], entry["offset"]))
    return results


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------


def _simple_checksums(payload: bytes) -> dict[str, bytes]:
    """The arithmetic checksums that predate everyone using CRCs."""
    total = sum(payload)
    xor = 0
    for byte in payload:
        xor ^= byte
    return {
        "sum8": bytes([total & 0xFF]),
        "xor8": bytes([xor]),
        "sum8-twos-complement": bytes([(-total) & 0xFF]),
        "sum16-be": (total & 0xFFFF).to_bytes(2, "big"),
        "sum16-le": (total & 0xFFFF).to_bytes(2, "little"),
    }


def checksum_candidates(
    frames: Sequence[bytes],
    *,
    widths: Sequence[int] = (1, 2, 4),
    payload_starts: Sequence[int] = (0, 1, 2),
    max_frames: int = 200,
    min_fraction: float = 0.25,
) -> list[dict[str, Any]]:
    """Which checksum, over which part of the frame, explains the trailer?

    Every catalogue CRC and a handful of arithmetic checksums are tried
    against each candidate split.  Results report how many frames validated
    *and* how many did not: a protocol with occasional line noise produces a
    high-but-not-perfect score, and hiding the failures would turn a useful
    signal-to-noise measurement into a false claim of certainty.
    """
    usable = [frame for frame in frames if len(frame) >= 4][:max_frames]
    if len(usable) < 3:
        return []
    tally: dict[tuple[str, str, int, int], dict[str, Any]] = {}

    for frame in usable:
        for width in widths:
            trailer = frame[-width:]
            for start in payload_starts:
                payload = frame[start : len(frame) - width]
                if len(payload) < 1:
                    continue
                matches = [
                    (str(match["model"]), str(match["byteorder"]))
                    for match in crc_candidates(payload, trailer)
                ]
                matches += [
                    (name, "n/a")
                    for name, value in _simple_checksums(payload).items()
                    if len(value) == width and value == trailer
                ]
                for model, byteorder in matches:
                    key = (model, byteorder, width, start)
                    entry = tally.setdefault(
                        key,
                        {
                            "model": model,
                            "byteorder": byteorder,
                            "width": width,
                            "payload_start": start,
                            "valid_frames": 0,
                            "first_failures": [],
                        },
                    )
                    entry["valid_frames"] += 1

    results: list[dict[str, Any]] = []
    for entry in tally.values():
        checked = len(usable)
        fraction = entry["valid_frames"] / checked
        if fraction < min_fraction:
            continue
        failures = _failing_frames(usable, entry)
        results.append(
            {
                **entry,
                "frames_checked": checked,
                "failed_frames": checked - entry["valid_frames"],
                "fraction": round(fraction, 4),
                "first_failures": failures[:5],
                "payload": (
                    f"bytes {entry['payload_start']}..-{entry['width']} of each frame"
                    if entry["payload_start"]
                    else f"all but the last {entry['width']} byte(s) of each frame"
                ),
            }
        )
    results.sort(key=lambda entry: (-entry["fraction"], entry["width"], entry["model"]))
    return results


def _failing_frames(frames: Sequence[bytes], entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Which frames this checksum candidate does *not* explain."""
    width = int(entry["width"])
    start = int(entry["payload_start"])
    model = str(entry["model"])
    byteorder = str(entry["byteorder"])
    failures: list[dict[str, Any]] = []
    for index, frame in enumerate(frames):
        payload = frame[start : len(frame) - width]
        trailer = frame[-width:]
        if not payload:
            continue
        if byteorder == "n/a":
            ok = _simple_checksums(payload).get(model) == trailer
        else:
            ok = any(
                match["model"] == model and match["byteorder"] == byteorder
                for match in crc_candidates(payload, trailer)
            )
        if not ok:
            failures.append({"frame": index, "hex": frame.hex().upper()[:64]})
        if len(failures) >= 5:
            break
    return failures


# ---------------------------------------------------------------------------
# Modbus RTU
# ---------------------------------------------------------------------------

#: Function codes a *passive listener* may see.  Deliberately wider than the
#: set FieldDeck is willing to transmit (see
#: :mod:`fielddeck.protocols.modbus`): recognising somebody else's traffic and
#: choosing what to put on a bus are different questions with different risks.
_MODBUS_FUNCTIONS: dict[int, str] = {
    0x01: "read_coils",
    0x02: "read_discrete_inputs",
    0x03: "read_holding_registers",
    0x04: "read_input_registers",
    0x05: "write_single_coil",
    0x06: "write_single_register",
    0x07: "read_exception_status",
    0x08: "diagnostics",
    0x0B: "get_comm_event_counter",
    0x0C: "get_comm_event_log",
    0x0F: "write_multiple_coils",
    0x10: "write_multiple_registers",
    0x11: "report_server_id",
    0x14: "read_file_record",
    0x15: "write_file_record",
    0x16: "mask_write_register",
    0x17: "read_write_multiple_registers",
    0x2B: "encapsulated_interface_transport",
}

_MODBUS_MAX_FRAME = 256


def _modbus_candidate_lengths(data: bytes, offset: int) -> list[tuple[int, str]]:
    """Frame lengths the function code at ``offset`` could imply.

    RTU has no length field, so the length comes from the function code and
    from whether the frame is a request or a response.  Both readings are
    offered and the CRC decides which — that is the only arbiter a passive
    listener has.
    """
    remaining = len(data) - offset
    if remaining < 4:
        return []
    function = data[offset + 1]
    if function & 0x80:
        base = function & 0x7F
        return [(5, "exception response")] if base in _MODBUS_FUNCTIONS else []
    if function not in _MODBUS_FUNCTIONS:
        return []

    lengths: list[tuple[int, str]] = []
    byte_count = data[offset + 2] if remaining > 2 else 0
    if function in (0x01, 0x02, 0x03, 0x04):
        lengths.append((8, "request"))
        lengths.append((5 + byte_count, "response"))
    elif function in (0x05, 0x06):
        lengths.append((8, "request or response"))
    elif function in (0x0F, 0x10):
        lengths.append((8, "response"))
        if remaining > 6:
            lengths.append((9 + data[offset + 6], "request"))
    elif function in (0x07, 0x0B, 0x0C, 0x11):
        lengths.append((4, "request"))
        lengths.append((5 + byte_count, "response"))
    elif function == 0x08:
        lengths.append((8, "request or response"))
    elif function == 0x16:
        lengths.append((10, "request or response"))
    elif function == 0x17:
        lengths.append((5 + byte_count, "response"))
        lengths.append((13 + (data[offset + 10] if remaining > 10 else 0), "request"))
    else:
        lengths.append((5 + byte_count, "response"))
    return [
        (length, role)
        for length, role in lengths
        if 4 <= length <= min(remaining, _MODBUS_MAX_FRAME)
    ]


def detect_modbus_rtu(data: bytes, *, scan_limit: int = DEFAULT_SCAN_LIMIT) -> dict[str, Any]:
    """Recover Modbus RTU framing from a byte log, CRC first.

    Walks the stream taking the shortest CRC-valid frame at each position.
    Bytes that cannot be explained are counted as resynchronisation, not
    quietly skipped: on a half-duplex RS-485 line, unexplained bytes usually
    mean a second master, a wrong baud rate or a collision, and that is
    exactly what the operator needs told.
    """
    window = data[:scan_limit]
    frames: list[dict[str, Any]] = []
    addresses: Counter[int] = Counter()
    functions: Counter[int] = Counter()
    exceptions: Counter[int] = Counter()
    crc_rejections = 0
    failed_frames = 0
    resync_bytes = 0
    in_sync = False

    offset = 0
    while offset + 4 <= len(window):
        candidates = sorted(_modbus_candidate_lengths(window, offset))
        matched: tuple[int, str] | None = None
        for length, role in candidates:
            frame = window[offset : offset + length]
            expected = crc("crc16-modbus", frame[:-2]).to_bytes(2, "little")
            if frame[-2:] == expected:
                matched = (length, role)
                break
            crc_rejections += 1
        if matched is None:
            # A frame that starts exactly where the previous one ended and
            # still fails CRC is a corrupted frame.  One found mid-resync is
            # just a byte that happened to look like a function code, and
            # counting those would invent a failure rate out of noise.
            if in_sync and candidates:
                failed_frames += 1
            in_sync = False
            resync_bytes += 1
            offset += 1
            continue
        in_sync = True
        length, role = matched
        frame = window[offset : offset + length]
        address, function = frame[0], frame[1]
        addresses[address] += 1
        functions[function] += 1
        if function & 0x80 and length >= 3:
            exceptions[frame[2]] += 1
        frames.append(
            {
                "offset": offset,
                "length": length,
                "address": address,
                "function": f"0x{function:02X}",
                "name": _MODBUS_FUNCTIONS.get(function & 0x7F, "unknown"),
                "role": role,
                "exception": bool(function & 0x80),
                "hex": frame.hex().upper(),
            }
        )
        offset += length

    explained = sum(int(entry["length"]) for entry in frames)
    return {
        "scanned_bytes": len(window),
        "frames": len(frames),
        "explained_bytes": explained,
        "coverage": round(explained / len(window), 4) if window else 0.0,
        "resync_bytes": resync_bytes,
        "crc_rejections": crc_rejections,
        "frames_failed_crc": failed_frames,
        "addresses": {f"0x{value:02X}": count for value, count in addresses.most_common(8)},
        "function_codes": {
            f"0x{value:02X}": {"name": _MODBUS_FUNCTIONS.get(value & 0x7F, "unknown"), "count": n}
            for value, n in functions.most_common(8)
        },
        "exception_codes": {f"0x{value:02X}": n for value, n in exceptions.most_common(8)},
        "frame_preview": frames[:10],
    }


# ---------------------------------------------------------------------------
# Everything at once
# ---------------------------------------------------------------------------


def analyze(
    data: bytes,
    *,
    timestamps_ns: Sequence[int] | None = None,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
) -> dict[str, Any]:
    """Run every structural analysis over one captured stream.

    The result is the evidence base :func:`fielddeck.analysis.autodetect.classify`
    reasons over, and is useful on its own in the HMI's analysis screen.
    """
    if not data:
        raise InvalidRequest("nothing to analyse", details={"size_bytes": 0})

    window = data[:scan_limit]
    found = segmentations(window)
    report: dict[str, Any] = {
        "size_bytes": len(data),
        "scanned_bytes": len(window),
        "truncated": len(data) > len(window),
        "histogram": byte_histogram(window),
        "printable_ratio": printable_ratio(window),
        "entropy_bits_per_byte": shannon_entropy(window),
        "delimiters": candidate_delimiters(window),
        "frame_lengths": candidate_frame_lengths(window),
        "preambles": repeated_preambles(window),
        "text": text_lines(window),
        "modbus_rtu": detect_modbus_rtu(window),
        "segmentations": [
            {
                **segmentation.summary(),
                "constant_fields": constant_fields(segmentation.frames),
                "counter_fields": counter_fields(segmentation.frames),
                "checksum_candidates": checksum_candidates(segmentation.frames),
            }
            for segmentation in found
        ],
    }
    if timestamps_ns:
        from fielddeck.analysis.timing import classify_periodicity

        report["timing"] = classify_periodicity(list(timestamps_ns))
    return report
