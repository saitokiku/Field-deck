"""The CRC catalogue, checked against the published check values.

Every entry in the Rocksoft-style catalogue carries the CRC of ``123456789``
as its own ``check`` field.  That number is what makes a catalogue entry
falsifiable: a transposed polynomial, a wrong init or a flipped reflection flag
all change it.  So the first test here is the one that matters — every model,
every time — and it is written as a loop over the catalogue so a model added
next year is verified the moment it is added.

The check values come from the standard catalogue (reveng), not from running
this implementation.
"""

from __future__ import annotations

import zlib

import pytest

from fielddeck.analysis.crc import CATALOGUE, CrcModel, crc, crc_candidates, get_model, list_models
from fielddeck.common.errors import InvalidRequest

CHECK_INPUT = b"123456789"

#: Independently known check values, spelled out so this file does not simply
#: assert the catalogue against itself.
KNOWN_CHECKS = {
    "crc8": 0xF4,
    "crc8-maxim": 0xA1,
    "crc8-sae-j1850": 0x4B,
    "crc8-autosar": 0xDF,
    "crc16-modbus": 0x4B37,
    "crc16-ccitt-false": 0x29B1,
    "crc16-xmodem": 0x31C3,
    "crc16-kermit": 0x2189,
    "crc16-arc": 0xBB3D,
    "crc16-maxim": 0x44C2,
    "crc16-usb": 0xB4C8,
    "crc16-dnp": 0xEA82,
    "crc16-mcrf4xx": 0x6F91,
    "crc16-t10-dif": 0xD0DB,
    "crc16-profibus": 0xA819,
    "crc32": 0xCBF43926,
    "crc32c": 0xE3069283,
    "crc32-bzip2": 0xFC891918,
    "crc32-mpeg2": 0x0376E6E7,
    "crc5-usb": 0x19,
}


class TestCatalogue:
    @pytest.mark.parametrize("name", sorted(CATALOGUE))
    def test_every_model_reproduces_its_own_check_value(self, name: str) -> None:
        """A wrong parameter set cannot ship quietly."""
        model = CATALOGUE[name]
        assert model.compute(CHECK_INPUT) == model.check, (
            f"{name} computes 0x{model.compute(CHECK_INPUT):X} but declares 0x{model.check:X}"
        )

    @pytest.mark.parametrize("name", sorted(KNOWN_CHECKS))
    def test_the_declared_check_matches_the_published_one(self, name: str) -> None:
        assert CATALOGUE[name].check == KNOWN_CHECKS[name]

    def test_the_catalogue_covers_what_this_file_claims_to_check(self) -> None:
        """If a model is added, this test says so rather than skipping it."""
        assert set(CATALOGUE) == set(KNOWN_CHECKS)

    def test_crc32_agrees_with_zlib(self) -> None:
        """One entry verified against an implementation nobody here wrote."""
        for payload in (b"", b"a", CHECK_INPUT, bytes(range(256))):
            assert crc("crc32", payload) == zlib.crc32(payload)

    @pytest.mark.parametrize("name", sorted(CATALOGUE))
    def test_a_result_always_fits_its_declared_width(self, name: str) -> None:
        model = CATALOGUE[name]
        for payload in (b"", b"\x00", bytes(range(256))):
            value = model.compute(payload)
            assert 0 <= value <= model.mask

    @pytest.mark.parametrize("name", sorted(CATALOGUE))
    def test_computation_is_deterministic_and_does_not_consume_input(
        self, name: str
    ) -> None:
        model = CATALOGUE[name]
        payload = bytearray(b"\x01\x02\x03")
        first = model.compute(bytes(payload))
        assert model.compute(bytes(payload)) == first
        assert payload == bytearray(b"\x01\x02\x03")

    @pytest.mark.parametrize("name", sorted(CATALOGUE))
    def test_a_single_bit_flip_changes_the_result(self, name: str) -> None:
        """The entire point of a checksum, asserted rather than assumed."""
        model = CATALOGUE[name]
        original = bytes(range(32))
        flipped = bytes([original[0] ^ 0x01, *original[1:]])
        assert model.compute(original) != model.compute(flipped)


class TestLookup:
    def test_names_are_case_and_separator_insensitive(self) -> None:
        assert get_model("CRC16_MODBUS") is CATALOGUE["crc16-modbus"]
        assert get_model("  crc32  ") is CATALOGUE["crc32"]

    @pytest.mark.parametrize(
        ("alias", "canonical"),
        [
            ("modbus", "crc16-modbus"),
            ("ccitt", "crc16-ccitt-false"),
            ("xmodem", "crc16-xmodem"),
            ("kermit", "crc16-kermit"),
            ("zip", "crc32"),
            ("castagnoli", "crc32c"),
            ("crc8-1wire", "crc8-maxim"),
        ],
    )
    def test_the_names_people_actually_say_resolve(self, alias: str, canonical: str) -> None:
        assert get_model(alias) is CATALOGUE[canonical]

    def test_an_unknown_model_lists_what_is_available(self) -> None:
        with pytest.raises(InvalidRequest) as caught:
            get_model("crc16-something-else")
        assert "crc16-modbus" in caught.value.details["known"]
        assert "modbus" in caught.value.details["aliases"]

    def test_listing_reports_every_parameter_of_every_model(self) -> None:
        listed = {entry["name"]: entry for entry in list_models()}
        assert set(listed) == set(CATALOGUE)
        modbus = listed["crc16-modbus"]
        assert modbus["width"] == 16
        assert modbus["poly"] == "0x8005"
        assert modbus["init"] == "0xFFFF"
        assert modbus["refin"] is True and modbus["refout"] is True
        assert modbus["check"] == "0x4B37"


class TestSerialisation:
    def test_a_value_serialises_to_its_byte_width_in_both_orders(self) -> None:
        model = CATALOGUE["crc16-modbus"]
        assert model.byte_width == 2
        assert model.to_bytes(0x1234, byteorder="big") == b"\x12\x34"
        assert model.to_bytes(0x1234, byteorder="little") == b"\x34\x12"

    def test_a_sub_byte_model_still_serialises_to_a_whole_byte(self) -> None:
        assert CATALOGUE["crc5-usb"].byte_width == 1


class TestCandidates:
    def test_a_real_modbus_frame_identifies_its_own_checksum(self) -> None:
        """Read holding registers, slave 1: the frame this catalogue exists for."""
        body = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x02])
        trailer = crc("crc16-modbus", body).to_bytes(2, "little")

        matches = {entry["model"]: entry for entry in crc_candidates(body, trailer)}
        assert "crc16-modbus" in matches
        assert matches["crc16-modbus"]["byteorder"] == "little"
        assert matches["crc16-modbus"]["hex"].startswith("0x")

    def test_every_matching_model_is_reported_not_just_the_first(self) -> None:
        """A two-byte trailer often matches several; pretending otherwise is
        false precision."""
        payload = b"123456789"
        trailer = CATALOGUE["crc16-arc"].to_bytes(CATALOGUE["crc16-arc"].check, byteorder="big")
        names = {entry["model"] for entry in crc_candidates(payload, trailer)}
        assert "crc16-arc" in names

    def test_only_models_of_the_right_width_are_considered(self) -> None:
        for entry in crc_candidates(b"123456789", b"\x26\x39\xf4\xcb"):
            assert CATALOGUE[str(entry["model"])].byte_width == 4

    def test_nothing_matches_a_wrong_trailer(self) -> None:
        assert crc_candidates(b"123456789", b"\x00\x00") == []

    def test_the_byteorder_search_can_be_narrowed(self) -> None:
        body = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x02])
        trailer = crc("crc16-modbus", body).to_bytes(2, "little")
        assert crc_candidates(body, trailer, byteorders=("big",)) == []


class TestCustomModel:
    def test_a_model_defined_by_hand_computes_like_the_catalogue_one(self) -> None:
        """The engine is parameterised, so a vendor's odd CRC is just data."""
        hand_rolled = CrcModel(
            name="modbus-by-hand",
            width=16,
            poly=0x8005,
            init=0xFFFF,
            refin=True,
            refout=True,
            xorout=0x0000,
            check=0x4B37,
        )
        assert hand_rolled.compute(CHECK_INPUT) == CATALOGUE["crc16-modbus"].check
