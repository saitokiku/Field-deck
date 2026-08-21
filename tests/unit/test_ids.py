"""Device identity: what a device id is allowed to be built from.

``/dev/ttyUSB0`` is not an identity — it is whichever adapter enumerated
first.  Everything downstream (aliases, scoped grants, saved sessions, recipe
device bindings) assumes an id refers to the same physical thing after a
reboot, so these tests are about the two ways that promise breaks: an id built
out of non-persistent evidence, and an id whose components collide after
sanitisation.
"""

from __future__ import annotations

import pytest

from fielddeck.common.ids import (
    DeviceIdParts,
    device_id,
    is_simulated_id,
    parse_device_id,
    sanitize_component,
    usb_serial_id,
)


class TestSanitisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("FTDI", "FTDI"),
            ("  spaced  ", "spaced"),
            ("FT232R USB UART", "FT232R-USB-UART"),
            ("a/b\\c", "a-b-c"),
            ("A10ABC", "A10ABC"),
            ("dots.and-dashes_ok", "dots.and-dashes_ok"),
            ("!!!", "unknown"),
            ("", "unknown"),
            (None, "unknown"),
        ],
    )
    def test_components_are_normalised_predictably(self, raw: str | None, expected: str) -> None:
        assert sanitize_component(raw) == expected

    def test_a_separator_can_never_survive_into_a_component(self) -> None:
        """A colon inside a component would forge an extra id field."""
        assert ":" not in sanitize_component("vendor:product")

    def test_the_fallback_is_configurable(self) -> None:
        assert sanitize_component(None, fallback="noserial") == "noserial"

    def test_case_is_preserved(self) -> None:
        """Serial numbers are case-sensitive; folding them would merge devices."""
        assert sanitize_component("aB12") == "aB12"


class TestComposition:
    def test_the_documented_shapes_round_trip(self) -> None:
        for value in (
            "serial:usb:0403:6001:A10ABC",
            "can:socketcan:can0",
            "visa:usb:0957:1798:MY12345678",
            "sim:serial:sim-uart-0",
        ):
            parts = parse_device_id(value)
            assert device_id(parts.transport, parts.bus, *parts.identity) == value

    def test_composition_drops_missing_identity_components(self) -> None:
        """An absent serial number shortens the id; it does not become "None"."""
        assert device_id("serial", "usb", "0403", "6001", None) == "serial:usb:0403:6001"

    def test_composition_sanitises_every_part(self) -> None:
        assert device_id("Serial", "USB Bus", "0403") == "Serial:USB-Bus:0403"

    def test_parsing_splits_transport_bus_and_identity(self) -> None:
        parts = parse_device_id("serial:usb:0403:6001:A10ABC")
        assert parts == DeviceIdParts(
            transport="serial", bus="usb", identity=("0403", "6001", "A10ABC")
        )
        assert parts.identity_str == "0403:6001:A10ABC"

    def test_an_id_with_no_bus_is_malformed(self) -> None:
        with pytest.raises(ValueError, match="malformed device id"):
            parse_device_id("serial")

    def test_a_two_part_id_is_legal_and_has_no_identity(self) -> None:
        parts = parse_device_id("can:can0")
        assert parts.identity == ()

    def test_simulated_ids_are_recognisable_without_parsing(self) -> None:
        assert is_simulated_id("sim:visa:sim-psu-0")
        assert not is_simulated_id("visa:usb:0957:1798:MY123")
        # A client must never have to guess whether it is looking at hardware.
        assert not is_simulated_id("serial:usb:sim:0403")


class TestUsbSerialIdentity:
    def test_a_serial_number_makes_the_id_stable(self) -> None:
        identity, stable = usb_serial_id(0x0403, 0x6001, "A10ABC")
        assert identity == "0403:6001:A10ABC"
        assert stable is True

    def test_without_a_serial_number_the_id_is_reported_unstable(self) -> None:
        """Two identical adapters are indistinguishable, and we say so."""
        identity, stable = usb_serial_id(0x0403, 0x6001, None)
        assert identity == "0403:6001"
        assert stable is False

    def test_an_empty_serial_number_counts_as_absent(self) -> None:
        assert usb_serial_id(0x0403, 0x6001, "") == ("0403:6001", False)

    def test_missing_vendor_or_product_is_marked_rather_than_omitted(self) -> None:
        """``xxxx`` keeps the field count fixed, so parsing stays positional."""
        identity, stable = usb_serial_id(None, None, "A10ABC")
        assert identity == "xxxx:xxxx:A10ABC"
        assert stable is True

    def test_ids_are_lowercase_hex_so_they_compare_bytewise(self) -> None:
        identity, _ = usb_serial_id(0x0ABC, 0xDEF0, "S")
        assert identity == "0abc:def0:S"

    def test_a_serial_number_with_separators_is_sanitised_not_rejected(self) -> None:
        identity, stable = usb_serial_id(0x1A86, 0x7523, "AB:CD/EF")
        assert identity == "1a86:7523:AB-CD-EF"
        assert stable is True
        assert len(parse_device_id(f"serial:usb:{identity}").identity) == 3
