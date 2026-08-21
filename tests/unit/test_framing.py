"""Structure discovery over a byte stream.

Framing analysis is the part of auto-detect that has to be honest about
coincidence.  Any 32 KiB of random data contains repeated byte pairs and
plausible-looking gaps; a delimiter finder that reports them is worse than
useless, because it hands an engineer a confident wrong answer at the point
where they are least able to check it.

So the tests come in pairs: a synthetic stream with known structure that must
be found, and a random or degenerate stream where nothing must be found.
"""

from __future__ import annotations

import os
import random

import pytest

from fielddeck.analysis.crc import crc
from fielddeck.analysis.framing import (
    analyze,
    best_phase,
    byte_histogram,
    candidate_delimiters,
    candidate_frame_lengths,
    checksum_candidates,
    constant_fields,
    counter_fields,
    detect_modbus_rtu,
    printable_ratio,
    repeated_preambles,
    segmentations,
    shannon_entropy,
    split_fixed,
    split_on_delimiter,
    split_on_header,
    text_lines,
)
from fielddeck.common.errors import InvalidRequest

HEADER = b"\x55\xaa"


def telemetry(count: int = 120, *, corrupt: set[int] | None = None) -> bytes:
    """A plausible 8-byte binary frame: sync, type, length, counter, value, CRC.

    ``55 AA 04 10 <counter> <value> <crc16-modbus little-endian>``
    """
    corrupt = corrupt or set()
    frames = []
    for index in range(count):
        body = bytes([0x55, 0xAA, 0x04, 0x10, index & 0xFF, (index * 7) & 0xFF])
        checksum = crc("crc16-modbus", body).to_bytes(2, "little")
        if index in corrupt:
            checksum = bytes([checksum[0] ^ 0xFF, checksum[1]])
        frames.append(body + checksum)
    return b"".join(frames)


def modbus_traffic(exchanges: int = 15) -> bytes:
    request = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x02])
    response = bytes([0x01, 0x03, 0x04, 0x00, 0x0A, 0x00, 0x0B])
    frame = b"".join(
        body + crc("crc16-modbus", body).to_bytes(2, "little") for body in (request, response)
    )
    return frame * exchanges


NMEA = b"$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47\r\n"


def nmea_stream(count: int = 40) -> bytes:
    """Sentences that differ from each other, as a real receiver's would."""
    lines = []
    for index in range(count):
        body = f"GPGGA,{120000 + index},4807.0{index:02d},N,01131.000,E,1,08,0.9,545.4,M,,"
        checksum = 0
        for character in body:
            checksum ^= ord(character)
        lines.append(f"${body}*{checksum:02X}\r\n".encode())
    return b"".join(lines)


def delimited_binary(count: int = 60, seed: int = 7) -> bytes:
    """Variable-length payloads separated by an HDLC-style 0x7E flag."""
    rng = random.Random(seed)
    return b"".join(
        bytes(rng.randrange(1, 0x7D) for _ in range(rng.randrange(4, 9))) + b"\x7e"
        for _ in range(count)
    )


# ---------------------------------------------------------------------------
# Cheap statistics
# ---------------------------------------------------------------------------


class TestStatistics:
    def test_the_histogram_reports_the_shape_of_the_data(self) -> None:
        report = byte_histogram(b"aabbbcc", top=2)
        assert report["size_bytes"] == 7
        assert report["unique_bytes"] == 3
        assert [entry["value"] for entry in report["top"]] == [ord("b"), ord("a")]

    def test_zero_and_high_bit_fractions_separate_text_from_binary(self) -> None:
        assert byte_histogram(b"hello")["high_bit_fraction"] == 0.0
        assert byte_histogram(bytes([0x00, 0x00, 0xFF, 0xFF]))["zero_fraction"] == 0.5

    def test_printable_ratio_recognises_text_and_binary(self) -> None:
        assert printable_ratio(b"plain ASCII text\r\n") == 1.0
        assert printable_ratio(bytes(range(0, 32))) < 0.2

    def test_entropy_spans_the_expected_range(self) -> None:
        assert shannon_entropy(b"\x00" * 1000) == 0.0
        assert shannon_entropy(bytes(range(256))) == 8.0
        assert 3.0 < shannon_entropy(NMEA * 10) < 6.0

    def test_random_data_has_almost_maximal_entropy(self) -> None:
        """Which is how the classifier knows not to look for framing in it."""
        assert shannon_entropy(os.urandom(20_000)) > 7.5


# ---------------------------------------------------------------------------
# Delimiters, lengths, preambles
# ---------------------------------------------------------------------------


class TestCandidates:
    def test_a_delimiter_stands_out_in_a_variable_length_stream(self) -> None:
        """The realistic case: payload lengths vary, the flag byte does not."""
        found = candidate_delimiters(delimited_binary())
        assert found[0]["value"] == 0x7E
        assert found[0]["known_as"] == "HDLC / PPP flag"

    def test_a_line_terminator_is_found_and_named(self) -> None:
        """In a rigidly formatted ASCII stream it is one of several winners.

        Every byte at a fixed column of a fixed-width sentence has perfectly
        regular gaps, so ``$``, ``P`` and ``A`` score exactly as well as the
        terminator does and the default top-five can be all payload.  The
        terminator is still found, and still named — which is what the ASCII
        branch of the classifier goes on to use.
        """
        found = candidate_delimiters(nmea_stream(), limit=40)
        named = {entry["value"]: entry for entry in found if entry["known_as"]}
        assert 0x0A in named and 0x0D in named
        assert named[0x0A]["gap_consistency"] > 0.9
        assert named[0x0A]["modal_gap"] == len(nmea_stream(1))

    def test_nothing_is_reported_for_random_data(self) -> None:
        """The property that keeps a confident wrong answer off the screen."""
        noise = os.urandom(32_768)
        assert candidate_delimiters(noise) == []
        assert candidate_frame_lengths(noise) == []
        assert repeated_preambles(noise) == []

    def test_a_fixed_frame_length_is_recovered_with_its_evidence(self) -> None:
        found = candidate_frame_lengths(telemetry())
        assert found, "an 8-byte frame stream reported no candidate length"
        assert found[0]["length"] == 8
        assert found[0]["lift_over_random"] > 2.0

    def test_harmonics_are_folded_into_the_fundamental(self) -> None:
        """16 and 24 are the same finding as 8, and reporting them as
        separate candidates is how a frame length gets misread."""
        found = candidate_frame_lengths(telemetry())
        assert [entry["length"] for entry in found] == [8]
        assert 16 in found[0]["harmonics"]

    def test_a_sync_word_is_recovered(self) -> None:
        found = repeated_preambles(telemetry())
        patterns = {entry["pattern"] for entry in found}
        assert "55AA" in patterns
        best = next(entry for entry in found if entry["pattern"] == "55AA")
        assert best["count"] == 120
        assert best["modal_gap"] == 8


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


class TestSegmentation:
    def test_splitting_on_a_delimiter_reports_the_bytes_it_left_over(self) -> None:
        segmentation = split_on_delimiter(b"AA\x00BB\x00CC", 0x00)
        assert segmentation.frames == [b"AA", b"BB"]
        assert segmentation.offsets == [0, 3]
        # The trailing "CC" has no terminator, so it is residue, not a frame.
        assert segmentation.residue_bytes == 2
        assert segmentation.coverage == 0.75

    def test_an_out_of_range_delimiter_is_refused(self) -> None:
        with pytest.raises(InvalidRequest):
            split_on_delimiter(b"data", 256)

    def test_fixed_length_splitting_reports_the_remainder(self) -> None:
        segmentation = split_fixed(b"0123456789", 4)
        assert segmentation.frames == [b"0123", b"4567"]
        assert segmentation.residue_bytes == 2

    def test_header_splitting_keeps_the_header_and_counts_the_preamble(self) -> None:
        segmentation = split_on_header(b"xx" + HEADER + b"0123" + HEADER + b"4567", HEADER)
        assert segmentation.frames == [HEADER + b"0123", HEADER + b"4567"]
        assert segmentation.residue_bytes == 2, "the two bytes before the first sync"

    def test_the_best_phase_finds_the_frame_grid_after_a_banner(self) -> None:
        """It finds the grid; the sync word is what resolves the rotation.

        A perfectly periodic stream is rotationally ambiguous by construction:
        shifting the split by one byte rotates the columns and leaves the
        column-agreement score identical, so phases 4 through 7 are all "the
        grid" for an 8-byte frame after a 4-byte banner.  What the phase does
        rule out is a split that straddles two frames, which scrambles every
        field.  Naming the true frame start is the job of the sync word, via
        :func:`repeated_preambles` and :func:`split_on_header`.
        """
        stream = b"junk" + telemetry(40)
        phase = best_phase(stream, 8)
        assert phase >= len(b"junk")

        frames = split_fixed(stream, 8, offset=phase).frames
        assert len({frame[0] for frame in frames}) == 1, "the first column is not constant"

    def test_a_summary_describes_a_segmentation_without_the_frames(self) -> None:
        summary = split_fixed(telemetry(10), 8).summary()
        assert summary["frames"] == 10
        assert summary["min_length"] == summary["max_length"] == 8
        assert summary["coverage"] == 1.0

    def test_segmentations_are_offered_best_first(self) -> None:
        found = segmentations(telemetry())
        assert found, "no segmentation was proposed for a well-framed stream"
        assert found[0].coverage >= 0.9

    def test_text_lines_are_recognised_with_their_terminator(self) -> None:
        report = text_lines(NMEA * 3)
        assert report["terminator"] == "CRLF"
        assert report["lines"] == 3
        assert report["preview"][0].startswith("$GPGGA")


# ---------------------------------------------------------------------------
# Field discovery
# ---------------------------------------------------------------------------


class TestFields:
    def test_constant_fields_are_the_ones_that_never_change(self) -> None:
        frames = split_fixed(telemetry(), 8).frames
        constants = {entry["offset"]: entry["value"] for entry in constant_fields(frames)}
        assert constants[0] == "0x55"
        assert constants[1] == "0xAA"
        assert constants[2] == "0x04"
        assert constants[3] == "0x10"
        assert 4 not in constants, "the counter is not constant"

    def test_a_rolling_counter_is_found_with_its_step(self) -> None:
        frames = split_fixed(telemetry(), 8).frames
        counters = {entry["offset"]: entry for entry in counter_fields(frames)}
        assert counters[4]["step"] == 1
        assert counters[4]["consistency"] == 1.0

    def test_a_checksum_field_is_identified_by_model_and_byte_order(self) -> None:
        frames = split_fixed(telemetry(), 8).frames
        candidates = checksum_candidates(frames)
        assert candidates, "the trailing CRC was not recognised"
        best = candidates[0]
        assert best["model"] == "crc16-modbus"
        assert best["byteorder"] == "little"
        assert best["fraction"] == 1.0

    def test_corrupt_frames_are_counted_rather_than_hidden(self) -> None:
        """2.5% failures is a fact about the link, and it belongs in the report."""
        frames = split_fixed(telemetry(200, corrupt={7, 91, 150, 151, 199}), 8).frames
        best = checksum_candidates(frames)[0]
        assert best["failed_frames"] == 5
        assert best["fraction"] < 1.0
        assert best["first_failures"]

    def test_no_checksum_is_claimed_for_random_frames(self) -> None:
        frames = split_fixed(os.urandom(8 * 200), 8).frames
        assert checksum_candidates(frames) == []

    def test_field_discovery_needs_no_frames_to_be_safe(self) -> None:
        assert constant_fields([]) == []
        assert counter_fields([]) == []
        assert checksum_candidates([]) == []


# ---------------------------------------------------------------------------
# Modbus RTU
# ---------------------------------------------------------------------------


class TestModbusRtu:
    def test_a_real_exchange_is_explained_frame_by_frame(self) -> None:
        report = detect_modbus_rtu(modbus_traffic())
        assert report["coverage"] == 1.0
        assert report["frames_failed_crc"] == 0
        assert report["addresses"] == {"0x01": 30}
        assert report["function_codes"]["0x03"]["name"] == "read_holding_registers"
        roles = {entry["role"] for entry in report["frame_preview"]}
        assert roles == {"request", "response"}

    def test_the_recogniser_is_crc_first_so_random_data_is_not_modbus(self) -> None:
        report = detect_modbus_rtu(os.urandom(4096))
        assert report["coverage"] < 0.1

    def test_a_stream_that_starts_mid_frame_resynchronises(self) -> None:
        report = detect_modbus_rtu(b"\x99\x99\x99" + modbus_traffic())
        assert report["resync_bytes"] >= 3
        assert report["coverage"] > 0.9


# ---------------------------------------------------------------------------
# The whole report
# ---------------------------------------------------------------------------


class TestAnalyze:
    def test_the_report_carries_every_kind_of_evidence(self) -> None:
        report = analyze(telemetry())
        assert report["size_bytes"] == 960
        assert report["scanned_bytes"] == 960
        assert report["truncated"] is False
        assert set(report) >= {
            "histogram",
            "printable_ratio",
            "entropy_bits_per_byte",
            "delimiters",
            "frame_lengths",
            "preambles",
            "text",
            "modbus_rtu",
            "segmentations",
        }
        first = report["segmentations"][0]
        assert first["constant_fields"] and first["checksum_candidates"]

    def test_timing_is_included_only_when_timestamps_are_offered(self) -> None:
        stamps = [index * 100_000_000 for index in range(50)]
        assert "timing" not in analyze(telemetry(10))
        report = analyze(telemetry(10), timestamps_ns=stamps)
        assert report["timing"]["mean_ms"] == pytest.approx(100.0)

    def test_a_long_stream_is_scanned_up_to_a_bound_and_says_so(self) -> None:
        report = analyze(telemetry(2000), scan_limit=1024)
        assert report["scanned_bytes"] == 1024
        assert report["truncated"] is True
        assert report["size_bytes"] == 16_000

    def test_analysing_nothing_is_refused(self) -> None:
        with pytest.raises(InvalidRequest, match="nothing to analyse"):
            analyze(b"")

    @pytest.mark.parametrize(
        "data",
        [
            b"\x00",
            b"\x00" * 4096,
            b"\xff" * 4096,
            bytes(range(256)) * 4,
            b"\x00\x01" * 2048,
            NMEA,
            b"A",
        ],
    )
    def test_degenerate_streams_produce_a_report_rather_than_an_exception(
        self, data: bytes
    ) -> None:
        report = analyze(data)
        assert report["size_bytes"] == len(data)
