"""Stage B/C of the passive auto-detect engine: what is this stream?

Stage A is inventory (which adapters exist) and lives in discovery.  Stage B
is passive capture and structural analysis, which is
:mod:`fielddeck.analysis.framing`.  This module is Stage C — turning that
structure into named hypotheses — and Stage D, naming the smallest active
test that would settle the question.  It never runs that test.  Deciding to
transmit is an authorization decision and belongs to a human with an arm
grant, not to a classifier that is pleased with itself.

The design rules, in order of importance:

* **Every claim carries its evidence, supporting *and* contradicting.**  A
  hypothesis that only lists what fits is a sales pitch.  The nine frames
  that failed CRC are the most valuable line in the output, because they are
  what tells an engineer whether they are looking at a wrong guess or at a
  real fault on a real bus.
* **Weak evidence produces low confidence, and few samples cap it.**  Four
  frames that agree cannot support 0.9, however neatly they agree.  Nothing
  ever reaches 1.0: passive observation cannot rule out the protocol being
  something else that shares a framing convention.
* **"Unknown / insufficient evidence" is a first-class answer** and is always
  present in the list.  A console that never says "I do not know" trains its
  operator to ignore it.

Confidences are combined with a noisy-OR over independent evidence, then
reduced by the observed failure rate and capped by sample size.  The
arithmetic is deliberately simple and visible: a number nobody can explain is
worse than no number at all.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from fielddeck.analysis import framing
from fielddeck.analysis.convert import cobs_decode, slip_decode
from fielddeck.common.errors import FieldDeckError, InvalidRequest
from fielddeck.common.models import PermissionLevel

__all__ = ["Hypothesis", "classify", "identify"]

#: No passive analysis is ever allowed to claim certainty.  The stream could
#: be a different protocol that shares a framing convention, and the only way
#: to know is to interact — which is a decision for a human.
MAX_CONFIDENCE = 0.92

UNKNOWN = "unknown / insufficient evidence"


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """One named possibility, its confidence, and why."""

    protocol: str
    confidence: float
    supporting: tuple[str, ...] = ()
    contradicting: tuple[str, ...] = ()
    #: The *smallest* active step that would settle it.  Never executed here.
    recommended_next_test: str | None = None
    #: The permission that next test would require, so a client can say
    #: "you are not armed for this" before the operator commits to it.
    next_test_permission: str = str(PermissionLevel.PASSIVE)
    decoder: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "confidence": self.confidence,
            "supporting": list(self.supporting),
            "contradicting": list(self.contradicting),
            "recommended_next_test": self.recommended_next_test,
            "next_test_permission": self.next_test_permission,
            "decoder": self.decoder,
            "evidence": [f"+ {item}" for item in self.supporting]
            + [f"- {item}" for item in self.contradicting],
        }

    def render(self) -> str:
        """The block an operator reads on a 80x25 screen."""
        lines = [f"Possible {self.protocol}: {self.confidence:.2f}"]
        lines += [f"+ {item}" for item in self.supporting]
        lines += [f"- {item}" for item in self.contradicting]
        if self.recommended_next_test:
            lines.append(f"> next test ({self.next_test_permission}): {self.recommended_next_test}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Confidence arithmetic
# ---------------------------------------------------------------------------


def _noisy_or(weights: Sequence[float]) -> float:
    """Combine independent evidence without ever reaching certainty."""
    remaining = 1.0
    for weight in weights:
        remaining *= 1.0 - max(0.0, min(1.0, weight))
    return 1.0 - remaining


def _sample_cap(samples: int) -> tuple[float, str | None]:
    """How confident this many observations may make us, and why not more."""
    if samples < 3:
        return 0.20, f"only {samples} frames of evidence"
    if samples < 10:
        return 0.45, f"only {samples} frames of evidence"
    if samples < 30:
        return 0.65, f"{samples} frames is a small sample"
    if samples < 100:
        return 0.80, f"{samples} frames observed"
    return MAX_CONFIDENCE, None


def _confidence(weights: Sequence[float], *, samples: int, penalty: float = 0.0) -> float:
    cap, _reason = _sample_cap(samples)
    return round(min(cap, _noisy_or(weights) * (1.0 - max(0.0, min(0.9, penalty)))), 2)


def _percent(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):.0f}%" if whole else "0%"


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def _nmea(data: bytes) -> tuple[int, int, list[str]]:
    """Count NMEA 0183 sentences whose XOR checksum validates."""
    valid = 0
    invalid = 0
    identifiers: Counter[str] = Counter()
    for raw in data.split(b"\n"):
        line = raw.strip(b"\r")
        if not line.startswith((b"$", b"!")) or b"*" not in line:
            continue
        body, _, checksum = line[1:].rpartition(b"*")
        try:
            expected = int(checksum[:2], 16)
        except ValueError:
            invalid += 1
            continue
        actual = 0
        for byte in body:
            actual ^= byte
        if actual == expected:
            valid += 1
            identifiers[body[:5].decode("ascii", "replace")] += 1
        else:
            invalid += 1
    return valid, invalid, [name for name, _count in identifiers.most_common(4)]


def _ascii_hypotheses(data: bytes, report: dict[str, Any]) -> list[Hypothesis]:
    text = report["text"]
    printable = float(report["printable_ratio"])
    lines = int(text.get("lines") or 0)
    hypotheses: list[Hypothesis] = []

    valid, invalid, identifiers = _nmea(data)
    if valid >= 3:
        total = valid + invalid
        supporting = [
            f"NMEA 0183 XOR checksums validate on {valid}/{total} sentences",
            f"sentence identifiers seen: {', '.join(identifiers)}" if identifiers else "",
            f"{_percent(int(printable * 1000), 1000)} of bytes are printable ASCII",
        ]
        contradicting = []
        if invalid:
            contradicting.append(f"{invalid} sentences carry a checksum that does not match")
        hypotheses.append(
            Hypothesis(
                protocol="NMEA 0183 sentences",
                confidence=_confidence(
                    [0.85 if valid >= 10 else 0.6, 0.4],
                    samples=total,
                    penalty=invalid / total if total else 0.0,
                ),
                supporting=tuple(item for item in supporting if item),
                contradicting=tuple(contradicting),
                recommended_next_test=(
                    "none: NMEA talkers broadcast unprompted, so watching longer costs "
                    "nothing. Configuring the talker would be a CONTROL write."
                ),
                next_test_permission=str(PermissionLevel.PASSIVE),
                decoder="nmea0183",
            )
        )

    if lines >= 3 or printable >= 0.6:
        weights: list[float] = []
        supporting = []
        contradicting = []
        if printable >= 0.95:
            weights.append(0.7)
            supporting.append(f"{printable * 100:.0f}% of bytes are printable ASCII")
        elif printable >= 0.75:
            weights.append(0.45)
            supporting.append(f"{printable * 100:.0f}% of bytes are printable ASCII")
        else:
            contradicting.append(
                f"only {printable * 100:.0f}% of bytes are printable; the stream is "
                "mostly binary, or text mixed with binary"
            )
        terminator = text.get("terminator")
        if terminator and lines >= 3:
            weights.append(0.5 if lines >= 10 else 0.3)
            supporting.append(
                f"{lines} {terminator}-terminated lines, mean length "
                f"{text.get('mean_length')} bytes"
            )
        printable_lines = int(text.get("printable_lines") or 0)
        if lines and printable_lines < lines:
            contradicting.append(
                f"{lines - printable_lines} of {lines} lines contain non-printable bytes"
            )
        if weights:
            hypotheses.append(
                Hypothesis(
                    protocol="ASCII line protocol",
                    confidence=_confidence(
                        weights,
                        samples=max(lines, 1),
                        penalty=(lines - printable_lines) / lines if lines else 0.0,
                    ),
                    supporting=tuple(supporting),
                    contradicting=tuple(contradicting),
                    recommended_next_test=(
                        "send a single carriage return (serial.send, CONTROL) and watch for "
                        "an echo or a prompt: one byte is the smallest possible probe"
                    ),
                    next_test_permission=str(PermissionLevel.CONTROL),
                    decoder="ascii-lines",
                )
            )
    return hypotheses


def _modbus_hypothesis(report: dict[str, Any]) -> Hypothesis | None:
    modbus = report["modbus_rtu"]
    frames = int(modbus["frames"])
    if frames < 3:
        return None

    failed = int(modbus["frames_failed_crc"])
    total_candidates = frames + failed
    coverage = float(modbus["coverage"])
    addresses = modbus["addresses"]
    functions = modbus["function_codes"]

    weights = [0.85 if frames >= 20 else 0.6]
    supporting = [
        f"CRC-16/MODBUS validates {frames}/{total_candidates} candidate frames, "
        f"covering {coverage * 100:.0f}% of the capture"
    ]
    contradicting: list[str] = []

    if addresses:
        top_address, top_count = next(iter(addresses.items()))
        share = top_count / frames
        if share >= 0.6:
            weights.append(0.45)
            supporting.append(
                f"address bytes predominantly {top_address} ({top_count}/{frames} frames)"
            )
        else:
            supporting.append(f"{len(addresses)} station addresses seen, most common {top_address}")
    if functions:
        named = [code for code, info in functions.items() if info["name"] != "unknown"]
        if named:
            weights.append(0.45)
            supporting.append(f"function codes {'/'.join(named[:4])} present")
    if modbus["exception_codes"]:
        supporting.append(
            f"exception responses seen ({', '.join(modbus['exception_codes'])}): "
            "a station is answering, and refusing"
        )
    if failed:
        contradicting.append(
            f"{failed} candidate frames fail the same CRC — line noise, a collision, "
            "or a second master on the bus"
        )
    residue = int(modbus["resync_bytes"])
    if residue:
        contradicting.append(
            f"{residue} bytes ({_percent(residue, int(modbus['scanned_bytes']))}) could not "
            "be assigned to any frame"
        )

    penalty = (failed / total_candidates if total_candidates else 0.0) + max(
        0.0, 0.9 - coverage
    ) / 2
    return Hypothesis(
        protocol="Modbus RTU",
        confidence=_confidence(weights, samples=frames, penalty=penalty),
        supporting=tuple(supporting),
        contradicting=tuple(contradicting),
        recommended_next_test=(
            "read one holding register from the observed address with modbus.read "
            "(QUERY): a single 8-byte request is the smallest frame that confirms it, "
            "and it must not be issued while another master owns the bus"
        ),
        next_test_permission=str(PermissionLevel.QUERY),
        decoder="modbus-rtu",
    )


@dataclass(frozen=True, slots=True)
class _LengthFit:
    """One consistent reading of a length field, and how well it explained the stream."""

    length_offset: int
    length_width: int
    endianness: str
    extra: int
    frames: int
    coverage: float
    resyncs: int
    distinct_lengths: int
    distinct_declared: int
    constant_columns: int
    min_frame: int
    max_frame: int


def _try_length_prefix(
    data: bytes, offset: int, width: int, endianness: str, extra: int
) -> _LengthFit | None:
    """Walk the stream assuming a length field, and see how far it gets."""
    order: Any = endianness
    minimum = offset + width + 1
    frames: list[bytes] = []
    consumed = 0
    resyncs = 0
    lengths: Counter[int] = Counter()
    declared_values: Counter[int] = Counter()
    position = 0
    while position + offset + width <= len(data):
        declared = int.from_bytes(data[position + offset : position + offset + width], order)
        total = declared + extra
        if total < minimum or total > 4096 or position + total > len(data):
            resyncs += 1
            position += 1
            continue
        frames.append(data[position : position + total])
        lengths[total] += 1
        declared_values[declared] += 1
        consumed += total
        position += total
    if len(frames) < 5:
        return None

    shortest = min(len(frame) for frame in frames)
    columns = 0
    for column in range(min(shortest, 4)):
        if column in range(offset, offset + width):
            continue
        if len({frame[column] for frame in frames}) == 1:
            columns += 1
    return _LengthFit(
        length_offset=offset,
        length_width=width,
        endianness=endianness,
        extra=extra,
        frames=len(frames),
        coverage=round(consumed / len(data), 4),
        resyncs=resyncs,
        distinct_lengths=len(lengths),
        distinct_declared=len(declared_values),
        constant_columns=columns,
        min_frame=shortest,
        max_frame=max(len(frame) for frame in frames),
    )


def _length_prefix_fit(data: bytes) -> _LengthFit | None:
    """Best length-field reading of the stream, or None if none is credible.

    The trap here is that *any* byte can be read as a length and will usually
    "fit": a stream parsed into frames whose lengths were taken from the
    stream itself covers the whole stream by construction, and random data
    scores 100% coverage every time.  Three gates keep that honest, and all
    three have to hold:

    * **A constant column outside the length field.**  A real framed protocol
      has a sync byte, a type code or an address that does not change.  A
      coincidental parse has nothing that holds still.
    * **A length field that actually varies.**  A "length" that is the same in
      every frame is a type byte, and the stream is fixed-length framing —
      which is a different, better-supported answer.
    * **Almost no resynchronisation.**  A length field that is real predicts
      the next frame boundary exactly, every time.
    """
    best: _LengthFit | None = None
    for offset in (0, 1, 2):
        for width in (1, 2):
            orders = ("big",) if width == 1 else ("big", "little")
            for endianness in orders:
                for extra in range(0, 7):
                    fit = _try_length_prefix(data, offset, width, endianness, extra)
                    if fit is None or fit.frames < 8 or fit.coverage < 0.85:
                        continue
                    if fit.constant_columns == 0 or fit.distinct_declared < 2:
                        continue
                    if fit.resyncs > max(2, len(data) * 0.02):
                        continue
                    key = (fit.constant_columns, fit.coverage, fit.frames)
                    if best is None or key > (best.constant_columns, best.coverage, best.frames):
                        best = fit
    return best


def _length_prefix_hypothesis(data: bytes) -> Hypothesis | None:
    fit = _length_prefix_fit(data)
    if fit is None:
        return None

    field = (
        f"{fit.length_width * 8}-bit"
        + (f" {fit.endianness}-endian" if fit.length_width > 1 else "")
        + f" length field at offset {fit.length_offset}"
    )
    weights = [0.6 if fit.frames >= 20 else 0.4]
    supporting = [
        f"{field} explains {fit.frames} frames and {fit.coverage * 100:.0f}% of the bytes",
        f"frame length = length field + {fit.extra} bytes of framing overhead",
    ]
    contradicting: list[str] = []
    weights.append(0.4)
    supporting.append(
        f"{fit.constant_columns} byte position(s) outside the length field are "
        "identical in every frame — a sync word or type code"
    )
    if fit.distinct_lengths <= max(3, fit.frames // 4):
        weights.append(0.3)
        supporting.append(
            f"only {fit.distinct_lengths} distinct frame lengths across {fit.frames} "
            "frames, so the field is quantised the way a real payload is"
        )
    if fit.min_frame == fit.max_frame:
        contradicting.append(
            f"every frame is {fit.min_frame} bytes, so fixed-length framing explains "
            "the stream just as well as a length field does"
        )
    else:
        weights.append(0.3)
        supporting.append(
            f"frame lengths vary between {fit.min_frame} and {fit.max_frame} bytes and "
            "the field tracks them"
        )
    if fit.resyncs:
        contradicting.append(f"{fit.resyncs} bytes had to be skipped to stay in sync")

    return Hypothesis(
        protocol="length-prefixed binary framing",
        confidence=_confidence(
            weights, samples=fit.frames, penalty=fit.resyncs / max(1, fit.frames) / 2
        ),
        supporting=tuple(supporting),
        contradicting=tuple(contradicting),
        recommended_next_test=(
            "none is needed to keep decoding. If the DUT is expected to answer, the "
            "smallest active step is one well-formed frame with the shortest legal "
            "payload (serial.send, CONTROL)"
        ),
        next_test_permission=str(PermissionLevel.CONTROL),
        decoder="length-prefixed",
    )


def _delimiter_hypotheses(data: bytes, report: dict[str, Any]) -> list[Hypothesis]:
    """Delimiter framing, including the COBS and SLIP special cases.

    Several delimiter candidates are evaluated rather than only the
    highest-scoring one.  A COBS stream whose payload contains a byte that
    happens to recur on a tidy period will rank that byte above 0x00, and
    testing only the top candidate would miss the framing that actually
    decodes.  0x00 and 0xC0 are therefore always tried when they occur often
    enough to be a delimiter at all.
    """
    candidates = list(report["delimiters"][:3])
    seen = {int(entry["value"]) for entry in candidates}
    for special in (0x00, 0xC0):
        if special in seen:
            continue
        occurrences = data.count(bytes((special,)))
        if occurrences >= 3:
            candidates.append(
                {
                    "value": special,
                    "count": occurrences,
                    "gap_consistency": 0.0,
                    "modal_gap": None,
                    "known_as": framing.KNOWN_DELIMITERS.get(special),
                }
            )
    found = [
        hypothesis
        for hypothesis in (_one_delimiter(data, entry) for entry in candidates)
        if hypothesis is not None
    ]
    found.sort(key=lambda item: -item.confidence)
    return found[:2]


def _one_delimiter(data: bytes, entry: dict[str, Any]) -> Hypothesis | None:
    value = int(entry["value"])
    segmentation = framing.split_on_delimiter(data, value)
    frames = segmentation.frames
    if len(frames) < 3:
        return None

    consistency = float(entry["gap_consistency"])
    # Regular spacing is the whole claim; irregular spacing must not be
    # rescued by a floor, or every stream contains a "delimiter".
    weights = [min(0.65, consistency * 0.7)]
    gap = entry.get("modal_gap")
    supporting = [
        f"0x{value:02X} occurs {entry['count']} times and the gaps between occurrences "
        f"agree {consistency * 100:.0f}% of the time" + (f" (modal gap {gap} bytes)" if gap else "")
    ]
    contradicting: list[str] = []
    # The byte is part of the name: two delimiter hypotheses for the same
    # stream are a normal outcome, and "which one?" must be readable at a
    # glance in a list of five lines on a 480x320 screen.
    protocol = f"delimiter-framed binary protocol (0x{value:02X})"
    decoder = "delimiter"

    if value == 0x00:
        decoded, testable = _decodes_as(frames, cobs_decode)
        if testable >= 3 and decoded >= max(3, int(testable * 0.9)):
            protocol = "COBS-framed binary protocol"
            decoder = "cobs"
            weights.append(0.8)
            supporting.append(
                f"{decoded}/{testable} frames decode as valid COBS blocks — the first "
                "byte of each frame points at the next zero, which noise does not do"
            )
        elif testable >= 3:
            contradicting.append(
                f"only {decoded}/{testable} frames decode as COBS, so 0x00 is "
                "probably padding rather than a COBS delimiter"
            )
    elif value == 0xC0:
        decoded, testable = _decodes_as(frames, slip_decode)
        if testable >= 3 and decoded >= max(3, int(testable * 0.9)):
            protocol = "SLIP-framed protocol (RFC 1055)"
            decoder = "slip"
            weights.append(0.75)
            supporting.append(f"{decoded}/{testable} frames unescape cleanly as SLIP")
        elif testable >= 3:
            contradicting.append(f"only {decoded}/{testable} frames unescape cleanly as SLIP")

    lengths = {len(frame) for frame in frames}
    if len(lengths) == 1:
        contradicting.append(
            f"every frame is {next(iter(lengths))} bytes, so fixed-length framing "
            "explains the stream equally well"
        )
    if segmentation.residue_bytes:
        contradicting.append(
            f"{segmentation.residue_bytes} bytes after the last delimiter are an "
            "unterminated fragment"
        )
    if entry.get("known_as"):
        supporting.append(f"0x{value:02X} is conventionally {entry['known_as']}")

    return Hypothesis(
        protocol=protocol,
        confidence=_confidence(weights, samples=len(frames)),
        supporting=tuple(supporting),
        contradicting=tuple(contradicting),
        recommended_next_test=(
            "none is needed to keep decoding. The smallest active step, if the DUT is "
            "expected to answer, is one framed empty packet (serial.send, CONTROL)"
        ),
        next_test_permission=str(PermissionLevel.CONTROL),
        decoder=decoder,
    )


def _decodes_as(frames: Sequence[bytes], decoder: Any, *, min_length: int = 2) -> tuple[int, int]:
    """How many frames survive a codec, and how many were worth testing.

    Frames of one byte are excluded: a lone 0x01 between two zeros is a valid
    COBS block for an empty packet, so a stream of alternating 0x00/0x01
    "decodes perfectly" while carrying no evidence at all.
    """
    good = 0
    testable = 0
    for frame in frames:
        if len(frame) < min_length:
            continue
        testable += 1
        try:
            decoder(frame)
        except FieldDeckError:
            continue
        good += 1
    return good, testable


def _fixed_frame_hypothesis(report: dict[str, Any]) -> Hypothesis | None:
    """Uniform frames behind a sync word, ideally with a checksum trailer."""
    uniform = [
        segmentation
        for segmentation in report["segmentations"]
        if segmentation["frames"] >= 3
        and segmentation["min_length"] == segmentation["max_length"]
        and segmentation["method"] in ("header", "fixed-length")
    ]
    if not uniform:
        return None
    # Prefer whichever segmentation actually explained a checksum: that is a
    # far stronger claim than coverage, which any alignment can score highly.
    best = max(
        uniform,
        key=lambda item: (
            max((c["fraction"] for c in item["checksum_candidates"]), default=0.0),
            item["coverage"],
        ),
    )
    length = best["min_length"]
    frames = int(best["frames"])

    weights = [0.5 if frames >= 20 else 0.3]
    supporting = [f"{frames} frames of exactly {length} bytes ({best['description']})"]
    contradicting: list[str] = []
    penalty = 0.0
    decoder = "fixed-length"

    checksums = best["checksum_candidates"]
    if checksums:
        top = checksums[0]
        weights.append(0.8 if top["fraction"] >= 0.9 else 0.5)
        byteorder = "" if top["byteorder"] == "n/a" else f" {top['byteorder']}-endian"
        supporting.append(
            f"{top['model']}{byteorder} over {top['payload']} validates "
            f"{top['valid_frames']}/{top['frames_checked']} frames checked"
        )
        if top["failed_frames"]:
            penalty = top["failed_frames"] / top["frames_checked"]
            contradicting.append(
                f"{top['failed_frames']}/{top['frames_checked']} frames fail the same "
                "checksum — corrupted frames, or a field the checksum does not cover"
            )
        if len(checksums) > 1:
            others = ", ".join(f"{entry['model']}/{entry['byteorder']}" for entry in checksums[1:4])
            contradicting.append(
                f"the same trailer also matches {others}; a two-byte checksum rarely "
                "identifies one algorithm on its own"
            )
        decoder = f"fixed-length+{top['model']}"
    else:
        contradicting.append("no checksum candidate explains the trailing bytes of the frames")

    counters = best["counter_fields"]
    if counters:
        counter = counters[0]
        weights.append(0.35)
        width = "byte" if counter["width"] == 8 else f"{counter['width']}-bit field"
        supporting.append(
            f"{width} at offset {counter['offset']} steps by {counter['step']} in "
            f"{counter['consistency'] * 100:.0f}% of consecutive frames — a rolling counter"
        )
        if counter.get("gaps"):
            contradicting.append(
                f"the counter skips {counter['gaps']} times: frames were dropped, or the "
                "capture started mid-frame"
            )

    if float(report["printable_ratio"]) >= 0.95:
        # Fixed-width text records (a log line, a fixed-column table) are
        # genuinely fixed-length, but calling them binary framing sends the
        # operator to the wrong decoder.
        penalty = max(penalty, 0.3)
        contradicting.append(
            "every frame is printable ASCII, so this is a fixed-width text record "
            "layout rather than binary framing"
        )

    constants = [entry for entry in best["constant_fields"] if entry.get("constant")]
    if constants:
        weights.append(0.3)
        preview = ", ".join(f"offset {c['offset']}={c['value']}" for c in constants[:4])
        supporting.append(f"{len(constants)} byte positions never change ({preview})")

    residue = int(best["residue_bytes"])
    if residue:
        contradicting.append(
            f"{residue} bytes fall outside the framing (a banner before the traffic, or "
            "a capture that started mid-frame)"
        )

    return Hypothesis(
        protocol="fixed-length binary framing",
        confidence=_confidence(weights, samples=frames, penalty=penalty),
        supporting=tuple(supporting),
        contradicting=tuple(contradicting),
        recommended_next_test=(
            "none is needed to decode what is already being sent. To confirm which bytes "
            "the checksum covers, the smallest active step is one crafted frame with a "
            "single bit changed (serial.send, CONTROL)"
        ),
        next_test_permission=str(PermissionLevel.CONTROL),
        decoder=decoder,
    )


def _timing_evidence(report: dict[str, Any]) -> str | None:
    """Periodicity, phrased as the measurement it is."""
    timing = report.get("timing")
    if not timing or not timing.get("mean_ms"):
        return None
    if timing["confidence"] < 0.5:
        return None
    # Peak-to-peak jitter is the wrong figure here: one outlier from a boot
    # banner makes a tight 100 ms stream look like it has 100 ms of jitter.
    return (
        f"traffic is periodic at {timing['mean_ms']:.1f} ms, stdev "
        f"{timing['stdev_ms']:.1f} ms over {timing['samples']} samples"
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(
    data: bytes,
    *,
    timestamps_ns: Sequence[int] | None = None,
    report: dict[str, Any] | None = None,
    limit: int = 6,
) -> list[Hypothesis]:
    """Name what this stream might be, with evidence for and against.

    ``report`` accepts a :func:`fielddeck.analysis.framing.analyze` result so
    a caller that already ran the structural pass does not pay for it twice.
    The returned list always contains an "unknown / insufficient evidence"
    entry, whose confidence is whatever the named hypotheses left unexplained.
    """
    if not data:
        raise InvalidRequest("no bytes to classify", details={"size_bytes": 0})
    analysis = report if report is not None else framing.analyze(data, timestamps_ns=timestamps_ns)
    window = data[: int(analysis.get("scanned_bytes", len(data)))]

    hypotheses: list[Hypothesis] = []
    hypotheses.extend(_ascii_hypotheses(window, analysis))
    for detector in (_modbus_hypothesis, _fixed_frame_hypothesis):
        found = detector(analysis)
        if found is not None:
            hypotheses.append(found)
    hypotheses.extend(_delimiter_hypotheses(window, analysis))
    length_prefixed = _length_prefix_hypothesis(window)
    if length_prefixed is not None:
        hypotheses.append(length_prefixed)

    timing = _timing_evidence(analysis)
    if timing is not None:
        # Periodicity supports whatever the framing turns out to be; it is not
        # evidence for any particular protocol, so it does not move the score.
        hypotheses = [
            Hypothesis(**{**_as_kwargs(item), "supporting": (*item.supporting, timing)})
            for item in hypotheses
        ]

    hypotheses.sort(key=lambda item: -item.confidence)
    hypotheses = hypotheses[: max(1, limit - 1)]
    hypotheses.append(_unknown(window, analysis, hypotheses))
    hypotheses.sort(key=lambda item: -item.confidence)
    return hypotheses


def _as_kwargs(hypothesis: Hypothesis) -> dict[str, Any]:
    return {
        "protocol": hypothesis.protocol,
        "confidence": hypothesis.confidence,
        "supporting": hypothesis.supporting,
        "contradicting": hypothesis.contradicting,
        "recommended_next_test": hypothesis.recommended_next_test,
        "next_test_permission": hypothesis.next_test_permission,
        "decoder": hypothesis.decoder,
    }


def _unknown(data: bytes, report: dict[str, Any], hypotheses: Sequence[Hypothesis]) -> Hypothesis:
    """The honest answer, always offered alongside the named ones."""
    best = max((item.confidence for item in hypotheses), default=0.0)
    entropy = float(report["entropy_bits_per_byte"])
    supporting: list[str] = []
    contradicting: list[str] = []

    if entropy > 7.5:
        supporting.append(
            f"entropy is {entropy:.2f} bits/byte, which is compressed, encrypted or "
            "random — structure is not recoverable by framing analysis"
        )
    if not report["segmentations"]:
        supporting.append("no frame segmentation covered the stream")
    if not report["delimiters"] and not report["preambles"]:
        supporting.append("no repeating delimiter or sync word was found")
    if len(data) < 256:
        supporting.append(f"only {len(data)} bytes were captured")
    for item in hypotheses[:2]:
        contradicting.append(f"{item.protocol} scored {item.confidence:.2f}")

    return Hypothesis(
        protocol=UNKNOWN,
        confidence=round(max(0.0, 1.0 - best), 2),
        supporting=tuple(supporting or ["the evidence does not identify a known protocol"]),
        contradicting=tuple(contradicting),
        recommended_next_test=(
            "capture longer, and confirm the physical layer by hand: baud rate, bit order "
            "and electrical class (TTL / RS-232 / RS-485) cannot be read off a byte log. "
            "Nothing should be transmitted until the framing is understood"
        ),
        next_test_permission=str(PermissionLevel.PASSIVE),
    )


def identify(
    data: bytes,
    *,
    timestamps_ns: Sequence[int] | None = None,
    limit: int = 6,
    include_report: bool = False,
) -> dict[str, Any]:
    """Classification in the shape clients render: hypotheses plus evidence.

    The ``rendered`` field is the block the HMI prints verbatim; the
    structured hypotheses are what a recipe or Claude reads.  Both say the
    same thing, and neither performs the recommended test.
    """
    analysis = framing.analyze(data, timestamps_ns=timestamps_ns)
    hypotheses = classify(data, report=analysis, limit=limit)
    top = hypotheses[0]
    result: dict[str, Any] = {
        "size_bytes": len(data),
        "scanned_bytes": analysis["scanned_bytes"],
        "hypotheses": [item.as_dict() for item in hypotheses],
        "rendered": "\n\n".join(item.render() for item in hypotheses),
        "best": top.protocol,
        "confidence": top.confidence,
        "recommended_next_test": {
            "for": top.protocol,
            "test": top.recommended_next_test,
            "permission": top.next_test_permission,
            "executed": False,
        },
        "note": (
            "passive analysis only: nothing was transmitted, and the recommended test was not run"
        ),
    }
    if include_report:
        result["framing"] = analysis
    return result
