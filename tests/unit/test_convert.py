"""The conversion toolbox, against vectors from outside FieldDeck.

Where a reference exists, this file uses it: COBS against the worked examples
in Cheshire & Baker's paper, base64 and the hashes against the standard
library, floats against :mod:`struct`, CRC-32 against :mod:`zlib`.  Where no
reference exists the vector is hand-computed and the working is in the test.

The unit conversions get more attention than they might seem to deserve.  ``mV``
and ``MV`` are nine orders of magnitude apart, and an instrument that conflates
them is an instrument that reports a fault as a healthy reading.
"""

from __future__ import annotations

import base64
import hashlib
import struct

import pytest

from fielddeck.analysis.convert import (
    base64_decode,
    base64_encode,
    bitfield,
    bytes_to_floats,
    bytes_to_ints,
    cobs_decode,
    cobs_encode,
    convert_unit,
    epoch_to_iso,
    float_to_bytes,
    guess_epoch_units,
    hash_bytes,
    hexdump,
    int_to_bytes,
    interpret,
    inspect_elf,
    iso_to_epoch,
    list_units,
    parse_hex_bytes,
    parse_intel_hex,
    parse_number,
    printable_text,
    slice_bytes,
    slip_decode,
    slip_encode,
    to_base,
)
from fielddeck.common.errors import InvalidRequest


# ---------------------------------------------------------------------------
# Numbers and bases
# ---------------------------------------------------------------------------


class TestNumbers:
    @pytest.mark.parametrize(
        ("text", "value"),
        [
            ("42", 42),
            ("0x2A", 42),
            ("0b101010", 42),
            ("0o52", 42),
            ("-42", -42),
            ("1_000", 1000),
            ("  0xff  ", 255),
        ],
    )
    def test_prefixed_and_plain_numbers_parse(self, text: str, value: int) -> None:
        assert parse_number(text) == value

    def test_an_explicit_base_overrides_the_lack_of_a_prefix(self) -> None:
        assert parse_number("ff", base=16) == 255
        assert parse_number("101", base=2) == 5

    @pytest.mark.parametrize("text", ["", "  ", "0x", "zz", "12ab"])
    def test_nonsense_is_refused_rather_than_guessed_at(self, text: str) -> None:
        with pytest.raises(InvalidRequest):
            parse_number(text)

    @pytest.mark.parametrize(
        ("value", "base", "width", "expected"),
        [
            (255, 16, None, "0xFF"),
            (255, 16, 16, "0x00FF"),
            (5, 2, 8, "0b00000101"),
            (42, 8, None, "0o52"),
            (-255, 16, None, "-0xFF"),
        ],
    )
    def test_base_rendering_pads_to_the_requested_width(
        self, value: int, base: int, width: int | None, expected: str
    ) -> None:
        assert to_base(value, base, width=width) == expected

    def test_base_ten_has_no_prefix(self) -> None:
        assert to_base(42, 10) == "42"


class TestHexBytes:
    @pytest.mark.parametrize(
        "text", ["DEADBEEF", "de ad be ef", "0xDEADBEEF", "DE:AD:BE:EF", "de-ad-be-ef"]
    )
    def test_every_spelling_an_engineer_pastes_works(self, text: str) -> None:
        assert parse_hex_bytes(text) == b"\xde\xad\xbe\xef"

    def test_an_empty_string_is_no_bytes(self) -> None:
        assert parse_hex_bytes("  ") == b""

    def test_an_odd_number_of_digits_is_refused(self) -> None:
        """Half a byte is not a byte, and padding it would invent data."""
        with pytest.raises(InvalidRequest, match="whole bytes"):
            parse_hex_bytes("ABC")

    def test_non_hexadecimal_input_is_refused(self) -> None:
        with pytest.raises(InvalidRequest, match="not hexadecimal"):
            parse_hex_bytes("zzzz")


class TestIntegers:
    @pytest.mark.parametrize("endianness", ["big", "little"])
    @pytest.mark.parametrize("width", [8, 16, 32, 64])
    def test_round_trip_matches_int_to_bytes(self, width: int, endianness: str) -> None:
        value = 0x12 if width == 8 else 0x1234
        encoded = int_to_bytes(value, width, endianness=endianness)
        assert encoded == value.to_bytes(width // 8, endianness)  # type: ignore[arg-type]
        assert bytes_to_ints(encoded, width, endianness=endianness) == [value]

    def test_signed_decoding_is_not_the_same_as_unsigned(self) -> None:
        """The whole reason the signed column exists."""
        raw = b"\xff\xfd"
        assert bytes_to_ints(raw, 16) == [0xFFFD]
        assert bytes_to_ints(raw, 16, signed=True) == [-3]

    def test_a_trailing_partial_element_is_dropped_not_padded(self) -> None:
        """Padding invents data, and half a register is not a register."""
        assert bytes_to_ints(b"\x00\x01\x00", 16) == [1]

    def test_an_overflowing_value_is_refused_with_the_field_width(self) -> None:
        with pytest.raises(InvalidRequest) as caught:
            int_to_bytes(300, 8)
        assert caught.value.details["width"] == 8

    def test_an_unsupported_width_lists_the_supported_ones(self) -> None:
        with pytest.raises(InvalidRequest) as caught:
            bytes_to_ints(b"\x00", 12)
        assert caught.value.details["allowed"] == [8, 16, 32, 64]

    def test_an_unknown_endianness_is_refused(self) -> None:
        with pytest.raises(InvalidRequest, match="big.*little"):
            bytes_to_ints(b"\x00\x01", 16, endianness="middle")


class TestFloats:
    def test_pi_decodes_from_its_ieee_754_bit_pattern(self) -> None:
        assert bytes_to_floats(bytes.fromhex("40490FDB"), 32) == [
            pytest.approx(3.14159274, rel=1e-8)
        ]

    @pytest.mark.parametrize("width", [16, 32, 64])
    @pytest.mark.parametrize("endianness", ["big", "little"])
    def test_encoding_matches_struct(self, width: int, endianness: str) -> None:
        code = {16: "e", 32: "f", 64: "d"}[width]
        prefix = ">" if endianness == "big" else "<"
        encoded = float_to_bytes(1.5, width, endianness=endianness)
        assert encoded == struct.pack(prefix + code, 1.5)
        assert bytes_to_floats(encoded, width, endianness=endianness) == [1.5]

    def test_several_floats_decode_in_order(self) -> None:
        payload = struct.pack(">ff", 1.0, 2.0)
        assert bytes_to_floats(payload, 32) == [1.0, 2.0]


class TestBitfields:
    def test_bit_zero_is_the_least_significant_bit(self) -> None:
        """Which is how every register map in every datasheet numbers them."""
        assert bitfield(0b1010_1010, 0, 1) == 0
        assert bitfield(0b1010_1010, 1, 1) == 1
        assert bitfield(0b1101_0110, 1, 3) == 0b011

    def test_a_field_may_span_the_whole_word(self) -> None:
        assert bitfield(0xABCD, 0, 16) == 0xABCD

    def test_a_field_outside_the_declared_width_is_refused(self) -> None:
        with pytest.raises(InvalidRequest, match="fall outside"):
            bitfield(0xFF, 6, 4, total_width=8)

    def test_the_value_is_masked_to_the_declared_width(self) -> None:
        assert bitfield(0x1FF, 0, 8, total_width=8) == 0xFF

    @pytest.mark.parametrize(("offset", "count"), [(-1, 1), (0, 0), (0, -3)])
    def test_a_nonsense_field_is_refused(self, offset: int, count: int) -> None:
        with pytest.raises(InvalidRequest):
            bitfield(0xFF, offset, count)


# ---------------------------------------------------------------------------
# Framing codecs
# ---------------------------------------------------------------------------


class TestCobs:
    #: The worked examples from Cheshire & Baker, "Consistent Overhead Byte
    #: Stuffing" — the reference every implementation is measured against.
    VECTORS = [
        (b"\x00", b"\x01\x01"),
        (b"\x00\x00", b"\x01\x01\x01"),
        (b"\x11\x22\x00\x33", b"\x03\x11\x22\x02\x33"),
        (b"\x11\x22\x33\x44", b"\x05\x11\x22\x33\x44"),
        (b"\x11\x00\x00\x00", b"\x02\x11\x01\x01\x01"),
        (bytes(range(1, 255)), b"\xff" + bytes(range(1, 255))),
    ]

    @pytest.mark.parametrize(("plain", "encoded"), VECTORS)
    def test_encoding_matches_the_paper(self, plain: bytes, encoded: bytes) -> None:
        assert cobs_encode(plain) == encoded

    @pytest.mark.parametrize(("plain", "encoded"), VECTORS)
    def test_decoding_matches_the_paper(self, plain: bytes, encoded: bytes) -> None:
        assert cobs_decode(encoded) == plain

    def test_the_254_byte_run_case_round_trips(self) -> None:
        """The case a naive implementation gets wrong."""
        payload = bytes(range(1, 255)) + b"\x00" + b"\x99"
        assert cobs_decode(cobs_encode(payload)) == payload

    def test_an_encoded_frame_never_contains_a_zero(self) -> None:
        """Which is the entire purpose: zero is left free as the delimiter."""
        for payload in (b"", b"\x00" * 10, bytes(range(256)), b"\x01\x00\x02"):
            assert 0 not in cobs_encode(payload)

    def test_a_truncated_frame_is_refused(self) -> None:
        with pytest.raises(InvalidRequest):
            cobs_decode(b"\x05\x11\x22")


class TestSlip:
    def test_the_two_reserved_bytes_are_escaped(self) -> None:
        encoded = slip_encode(b"\xc0\xdb\x01")
        assert encoded == b"\xc0\xdb\xdc\xdb\xdd\x01\xc0"

    def test_round_trips_including_the_escapes(self) -> None:
        payload = b"\x01\xc0\x02\xdb\x03"
        assert slip_decode(slip_encode(payload)) == payload

    def test_the_leading_delimiter_is_optional(self) -> None:
        assert slip_encode(b"\x01", leading_end=False) == b"\x01\xc0"

    def test_a_dangling_escape_is_refused(self) -> None:
        with pytest.raises(InvalidRequest):
            slip_decode(b"\xc0\x01\xdb\xc0")


class TestBase64:
    def test_encoding_matches_the_standard_library(self) -> None:
        payload = bytes(range(64))
        assert base64_encode(payload) == base64.b64encode(payload).decode("ascii")
        assert base64_decode(base64_encode(payload)) == payload

    def test_the_urlsafe_alphabet_is_available(self) -> None:
        payload = b"\xfb\xff\xbf"
        assert base64_encode(payload, urlsafe=True) == base64.urlsafe_b64encode(payload).decode()
        assert base64_decode(base64_encode(payload, urlsafe=True), urlsafe=True) == payload

    def test_invalid_base64_is_refused(self) -> None:
        with pytest.raises(InvalidRequest):
            base64_decode("not base64 at all!!")


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


class TestPresentation:
    def test_a_hexdump_shows_offsets_hex_and_text(self) -> None:
        dump = hexdump(b"Hello\x00world", base_offset=0x100)
        assert "00000100" in dump
        assert "48 65 6C 6C 6F" in dump
        assert "|Hello.world|" in dump

    def test_a_hexdump_is_bounded_and_says_so(self) -> None:
        dump = hexdump(bytes(300), max_bytes=64)
        assert "more" in dump.lower() or "truncated" in dump.lower()

    def test_unprintable_bytes_become_a_placeholder(self) -> None:
        assert printable_text(b"ab\x00\x1f~\x7f") == "ab..~."

    def test_slicing_returns_exactly_the_requested_window(self) -> None:
        assert slice_bytes(b"0123456789", 2, 3) == b"234"
        assert slice_bytes(b"0123456789", 8) == b"89"

    def test_a_slice_past_the_end_is_refused_rather_than_silently_short(self) -> None:
        """A short read that looks like a full one is how a field is misread."""
        with pytest.raises(InvalidRequest, match="runs past the end"):
            slice_bytes(b"0123456789", 8, 100)

    def test_a_negative_slice_is_refused(self) -> None:
        with pytest.raises(InvalidRequest):
            slice_bytes(b"0123", -1, 2)


# ---------------------------------------------------------------------------
# Time and units
# ---------------------------------------------------------------------------


class TestEpoch:
    def test_the_epoch_itself_renders(self) -> None:
        assert epoch_to_iso(0) == "1970-01-01T00:00:00.000000Z"

    @pytest.mark.parametrize(
        ("unit", "value"),
        [("s", 1_787_234_580), ("ms", 1_787_234_580_000), ("ns", 1_787_234_580_000_000_000)],
    )
    def test_every_unit_lands_on_the_same_instant(self, unit: str, value: int) -> None:
        assert epoch_to_iso(value, unit=unit) == "2026-08-20T14:03:00.000000Z"

    def test_iso_round_trips_back_to_the_epoch(self) -> None:
        assert iso_to_epoch("2026-08-20T14:03:00Z") == 1_787_234_580

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        """Guessing local time on a device whose clock may never have been set
        is how captures end up hours away from the log they must line up with."""
        assert iso_to_epoch("2026-08-20T14:03:00") == iso_to_epoch("2026-08-20T14:03:00Z")

    def test_an_offset_is_honoured(self) -> None:
        assert iso_to_epoch("2026-08-20T16:03:00+02:00") == iso_to_epoch("2026-08-20T14:03:00Z")

    def test_nonsense_is_refused_with_an_example(self) -> None:
        with pytest.raises(InvalidRequest) as caught:
            iso_to_epoch("last tuesday")
        assert "example" in caught.value.details

    def test_an_unknown_unit_is_refused(self) -> None:
        with pytest.raises(InvalidRequest):
            epoch_to_iso(0, unit="fortnights")

    def test_unit_guessing_only_offers_plausible_dates(self) -> None:
        assert "s" in guess_epoch_units(1_787_234_580)
        assert "ms" in guess_epoch_units(1_787_234_580_000)
        assert guess_epoch_units(42) == []


class TestUnits:
    @pytest.mark.parametrize(
        ("value", "source", "target", "expected"),
        [
            (1.0, "V", "mV", 1000.0),
            (1500.0, "mA", "A", 1.5),
            (1.0, "kohm", "ohm", 1000.0),
            (1.0, "MHz", "kHz", 1000.0),
            (1.0, "in", "mm", 25.4),
            (1.0, "KiB", "B", 1024.0),
            (1.0, "kB", "B", 1000.0),
            (1.0, "lb", "g", 453.59237),
        ],
    )
    def test_linear_conversions(
        self, value: float, source: str, target: str, expected: float
    ) -> None:
        assert convert_unit(value, source, target)["value"] == pytest.approx(expected)

    def test_case_matters_because_the_prefixes_do(self) -> None:
        """``mV`` and ``MV``: nine orders of magnitude, one keystroke apart."""
        assert convert_unit(1.0, "mV", "V")["value"] == pytest.approx(1e-3)
        with pytest.raises(InvalidRequest):
            convert_unit(1.0, "MV", "V")

    @pytest.mark.parametrize(
        ("value", "source", "target", "expected"),
        [
            (100.0, "C", "F", 212.0),
            (-40.0, "C", "F", -40.0),
            (0.0, "C", "K", 273.15),
            (32.0, "F", "C", 0.0),
        ],
    )
    def test_temperature_is_an_offset_scale_not_a_factor(
        self, value: float, source: str, target: str, expected: float
    ) -> None:
        assert convert_unit(value, source, target)["value"] == pytest.approx(expected)

    def test_temperature_cannot_be_converted_into_anything_else(self) -> None:
        with pytest.raises(InvalidRequest, match="own dimension"):
            convert_unit(20.0, "C", "V")

    def test_a_cross_dimension_request_is_refused_rather_than_reinterpreted(self) -> None:
        with pytest.raises(InvalidRequest):
            convert_unit(1.0, "Hz", "s")

    def test_dbm_converts_to_absolute_power_both_ways(self) -> None:
        assert convert_unit(0.0, "dBm", "mW")["value"] == pytest.approx(1.0)
        assert convert_unit(1.0, "mW", "dBm")["value"] == pytest.approx(0.0)

    def test_the_unit_list_is_grouped_by_dimension(self) -> None:
        units = list_units()
        assert "V" in units["voltage"]
        assert "C" in units["temperature"]
        assert set(units) >= {"voltage", "current", "power", "time", "data"}


# ---------------------------------------------------------------------------
# Hashes and file formats
# ---------------------------------------------------------------------------


class TestHashes:
    def test_hashes_match_the_standard_library(self) -> None:
        payload = b"123456789"
        digests = hash_bytes(payload)
        assert digests["sha256"] == hashlib.sha256(payload).hexdigest()
        assert digests["md5"] == hashlib.md5(payload).hexdigest()  # noqa: S324
        assert digests["crc32"] == "0xCBF43926"
        assert digests["size_bytes"] == 9


class TestIntelHex:
    #: Four bytes at 0x0100, then the EOF record.  Each checksum is the two's
    #: complement of the sum of every prior byte in its record:
    #: 04+01+00+00+DE+AD+BE+EF = 0x33D, low byte 0x3D, so the checksum is 0xC3.
    GOOD = ":04010000DEADBEEFC3\n:00000001FF\n"

    def test_a_valid_file_parses_into_a_segment(self) -> None:
        parsed = parse_intel_hex(self.GOOD)
        assert parsed["records"] == 2
        assert parsed["data_bytes"] == 4
        assert parsed["checksums_valid"] is True
        assert parsed["eof_record"] is True
        assert parsed["segments"] == [{"start": "0x00000100", "end": "0x00000104", "bytes": 4}]

    def test_a_bad_record_checksum_is_reported(self) -> None:
        """A mistyped digit is exactly what the record checksum is for."""
        parsed = parse_intel_hex(self.GOOD.replace("C3", "B0"))
        assert parsed["checksums_valid"] is False
        assert parsed["bad_checksum_records"] == 1

    def test_a_missing_eof_record_is_noticed(self) -> None:
        """A truncated download and a complete image must not look alike."""
        parsed = parse_intel_hex(":04010000DEADBEEFC3\n")
        assert parsed["eof_record"] is False


class TestElf:
    def test_a_non_elf_file_is_refused_by_its_magic(self) -> None:
        with pytest.raises(InvalidRequest, match="not an ELF image"):
            inspect_elf(b"not an ELF at all, but long enough to be a header......")

    def test_a_file_shorter_than_its_own_header_is_refused(self) -> None:
        with pytest.raises(InvalidRequest, match="shorter than its own header"):
            inspect_elf(b"\x7fELF\x01\x01\x01")

    def test_the_header_identity_fields_are_read_natively(self) -> None:
        """A 32-bit little-endian ARM executable header, hand-assembled.

        These fields need no optional dependency, so they are reported whether
        or not pyelftools is installed.
        """
        header = bytearray(52)
        header[0:4] = b"\x7fELF"
        header[4] = 1  # 32-bit
        header[5] = 1  # little endian
        header[6] = 1  # version
        header[16:18] = (2).to_bytes(2, "little")  # ET_EXEC
        header[18:20] = (40).to_bytes(2, "little")  # EM_ARM
        header[24:28] = (0x8000).to_bytes(4, "little")  # entry point

        result = inspect_elf(bytes(header))
        assert result["format"] == "elf"
        assert result["bitness"] == 32
        assert result["endianness"] == "little"
        assert result["machine"] == "ARM"
        assert result["type"] == "executable"
        assert result["entry_point"] == "0x00008000"


# ---------------------------------------------------------------------------
# interpret(): every plausible reading at once
# ---------------------------------------------------------------------------


class TestInterpret:
    def test_a_hex_byte_is_offered_as_several_readings(self) -> None:
        result = interpret("0x41")
        labels = " ".join(str(reading["label"]) for reading in result["readings"])
        assert result["count"] > 1
        assert "65" in str(result["readings"])  # decimal
        assert "A" in labels or "A" in str(result["readings"])  # ASCII

    def test_ambiguity_is_stated_rather_than_resolved(self) -> None:
        """``42`` could be decimal or hex, and the tool says so."""
        result = interpret("42")
        assert result["ambiguous"] is True
        assert result["notes"]

    def test_an_explicit_prefix_removes_the_ambiguity(self) -> None:
        assert interpret("0x42")["ambiguous"] is False

    @pytest.mark.parametrize("value", ["", "-", "0x", "!!!", " ", "not a number"])
    def test_degenerate_input_never_raises_an_untyped_error(self, value: str) -> None:
        try:
            result = interpret(value)
        except InvalidRequest:
            return  # a typed refusal is a fine answer
        assert isinstance(result["readings"], list)

    @pytest.mark.parametrize("digits", [2049, 4300, 20_000])
    def test_a_very_long_number_is_refused_with_a_typed_error(self, digits: int) -> None:
        """Regression: a long digit string used to escape as an untyped error.

        CPython caps int/str conversion at 4300 digits, and ``guess_epoch_units``
        converted the parsed value to float -- so past those thresholds a
        PASSIVE ``tools.convert`` request answered "internal error" instead of
        refusing the input. Both are now bounded at the edge.
        """
        with pytest.raises(InvalidRequest):
            interpret("4" * digits)
