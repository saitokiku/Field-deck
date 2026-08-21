"""Protocol classification: named hypotheses, and what they are *not*.

Three rules hold for every answer this module gives, and each has a test:

* certainty is impossible — passive analysis cannot exceed 0.92, because the
  stream could be a different protocol sharing a framing convention;
* "unknown / insufficient evidence" is always on the list, carrying whatever
  confidence the named hypotheses left unexplained;
* the recommended next test is described and never executed.  Transmitting is
  an authorization decision for a human with an arm grant.
"""

from __future__ import annotations

import os
import random

import pytest

from fielddeck.analysis.autodetect import MAX_CONFIDENCE, UNKNOWN, classify, identify
from fielddeck.analysis.convert import cobs_encode, slip_encode
from fielddeck.analysis.crc import crc
from fielddeck.common.errors import InvalidRequest
from fielddeck.common.models import PermissionLevel


def telemetry(count: int = 200) -> bytes:
    """55 AA 04 10 <counter> <value> <crc16-modbus LE>: eight bytes, fixed."""
    frames = []
    for index in range(count):
        body = bytes([0x55, 0xAA, 0x04, 0x10, index & 0xFF, (index * 7) & 0xFF])
        frames.append(body + crc("crc16-modbus", body).to_bytes(2, "little"))
    return b"".join(frames)


def modbus_traffic(exchanges: int = 40) -> bytes:
    request = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x02])
    response = bytes([0x01, 0x03, 0x04, 0x00, 0x0A, 0x00, 0x0B])
    return (
        b"".join(
            body + crc("crc16-modbus", body).to_bytes(2, "little") for body in (request, response)
        )
        * exchanges
    )


def nmea_stream(count: int = 60) -> bytes:
    lines = []
    for index in range(count):
        body = f"GPGGA,{120000 + index},4807.0{index:02d},N,01131.000,E,1,08,0.9,545.4,M,,"
        checksum = 0
        for character in body:
            checksum ^= ord(character)
        lines.append(f"${body}*{checksum:02X}\r\n".encode())
    return b"".join(lines)


def cobs_stream(count: int = 80, seed: int = 3) -> bytes:
    rng = random.Random(seed)
    return b"".join(
        cobs_encode(bytes(rng.randrange(0, 256) for _ in range(rng.randrange(6, 12)))) + b"\x00"
        for _ in range(count)
    )


def slip_stream(count: int = 80, seed: int = 5) -> bytes:
    rng = random.Random(seed)
    return b"".join(
        slip_encode(bytes(rng.randrange(0, 256) for _ in range(rng.randrange(6, 12))))
        for _ in range(count)
    )


def best(data: bytes) -> tuple[str, float]:
    top = classify(data)[0]
    return top.protocol, top.confidence


# ---------------------------------------------------------------------------
# What it recognises
# ---------------------------------------------------------------------------


class TestRecognition:
    def test_a_fixed_binary_frame_with_a_crc_is_recognised(self) -> None:
        protocol, confidence = best(telemetry())
        assert "fixed-length" in protocol
        assert confidence >= 0.8

        top = classify(telemetry())[0]
        evidence = " ".join(top.supporting)
        assert "8 bytes" in evidence
        assert "crc16-modbus" in evidence
        assert "counter" in evidence

    def test_modbus_rtu_is_named_by_its_own_recogniser(self) -> None:
        protocols = [item.protocol for item in classify(modbus_traffic())]
        assert any("Modbus" in protocol for protocol in protocols)

    def test_nmea_is_recognised_as_more_than_just_ascii(self) -> None:
        protocols = [item.protocol for item in classify(nmea_stream())]
        assert protocols[0].startswith("NMEA")
        assert any("ASCII" in protocol for protocol in protocols)

    def test_cobs_framing_is_recognised(self) -> None:
        assert any("COBS" in item.protocol for item in classify(cobs_stream()))

    def test_slip_framing_is_recognised(self) -> None:
        assert any("SLIP" in item.protocol for item in classify(slip_stream()))

    def test_a_length_prefixed_stream_is_recognised(self) -> None:
        """Sync byte, then a length: the shape the length detector accepts."""
        rng = random.Random(11)
        frames = []
        for _ in range(80):
            payload = bytes(rng.randrange(0, 256) for _ in range(rng.randrange(4, 30)))
            frames.append(b"\xaa" + bytes([len(payload)]) + payload)

        top = classify(b"".join(frames))[0]
        assert "length-prefixed" in top.protocol
        assert "offset 1" in " ".join(top.supporting)

    def test_a_bare_length_field_with_nothing_constant_is_not_claimed(self) -> None:
        """Any byte can be read as a length and will usually "fit".

        A stream parsed into frames whose lengths came from the stream itself
        covers the whole stream by construction, so coverage alone proves
        nothing.  Without a constant column — a sync byte, a type code, an
        address — the reading is a coincidence and is not offered.
        """
        rng = random.Random(11)
        frames = []
        for _ in range(80):
            payload = bytes(rng.randrange(0, 256) for _ in range(rng.randrange(4, 30)))
            frames.append(bytes([len(payload)]) + payload)

        protocols = [item.protocol for item in classify(b"".join(frames))]
        assert not any("length-prefixed" in protocol for protocol in protocols)


# ---------------------------------------------------------------------------
# What it refuses to claim
# ---------------------------------------------------------------------------


class TestHonesty:
    def test_random_data_is_unknown_with_high_confidence(self) -> None:
        """The single most important negative result in the whole module."""
        protocol, confidence = best(os.urandom(32_768))
        assert protocol == UNKNOWN
        assert confidence > 0.8

    def test_the_reason_for_unknown_is_stated(self) -> None:
        top = classify(os.urandom(32_768))[0]
        evidence = " ".join(top.supporting)
        assert "entropy" in evidence
        assert "segmentation" in evidence or "delimiter" in evidence

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(b"\x00" * 4096, id="all-zeros"),
            pytest.param(b"\xff" * 4096, id="all-ones"),
            pytest.param(b"short", id="tiny"),
            pytest.param(bytes(range(256)), id="every-byte-once"),
            pytest.param(b"\x00\x01" * 2048, id="alternating"),
        ],
    )
    def test_degenerate_streams_do_not_produce_a_confident_named_answer(self, data: bytes) -> None:
        hypotheses = classify(data)
        assert any(item.protocol == UNKNOWN for item in hypotheses)
        for item in hypotheses:
            if item.protocol == UNKNOWN:
                # "I am certain I do not know" is an honest 1.00; the cap is on
                # claiming to have recognised something.
                continue
            assert 0.0 <= item.confidence <= MAX_CONFIDENCE

    def test_a_small_sample_caps_the_confidence(self) -> None:
        """Six frames is not a protocol identification, however clean it looks."""
        _, small = best(telemetry(6))
        _, large = best(telemetry(400))
        assert small < large

    def test_no_named_protocol_ever_reaches_certainty(self) -> None:
        """The stream could be a different protocol sharing a convention, and
        the only way to know is to interact — which is a human's decision."""
        for data in (telemetry(1000), modbus_traffic(200), nmea_stream(200)):
            for item in classify(data):
                if item.protocol != UNKNOWN:
                    assert item.confidence <= MAX_CONFIDENCE

    def test_unknown_is_always_offered(self) -> None:
        for data in (telemetry(), modbus_traffic(), nmea_stream(), cobs_stream()):
            assert any(item.protocol == UNKNOWN for item in classify(data))

    def test_classifying_nothing_is_refused(self) -> None:
        with pytest.raises(InvalidRequest):
            classify(b"")

    def test_corrupt_frames_appear_as_contradicting_evidence(self) -> None:
        """A 2.5% checksum failure rate is a fact about the link, not a rounding
        error, and it belongs where the operator can see it."""
        frames = []
        for index in range(200):
            body = bytes([0x55, 0xAA, 0x04, 0x10, index & 0xFF, (index * 7) & 0xFF])
            checksum = crc("crc16-modbus", body).to_bytes(2, "little")
            if index % 40 == 0:
                checksum = bytes([checksum[0] ^ 0xFF, checksum[1]])
            frames.append(body + checksum)

        top = classify(b"".join(frames))[0]
        assert top.contradicting
        assert any("fail" in item for item in top.contradicting)


# ---------------------------------------------------------------------------
# The next test is described, never run
# ---------------------------------------------------------------------------


class TestNextTest:
    def test_every_hypothesis_names_the_permission_its_next_test_needs(self) -> None:
        known = {str(level) for level in PermissionLevel}
        for item in classify(telemetry()):
            assert item.next_test_permission in known

    def test_the_unknown_hypothesis_recommends_only_passive_work(self) -> None:
        unknown = next(item for item in classify(os.urandom(8192)) if item.protocol == UNKNOWN)
        assert unknown.next_test_permission == str(PermissionLevel.PASSIVE)
        assert "transmitted" in (unknown.recommended_next_test or "")

    def test_the_report_states_that_nothing_was_transmitted(self) -> None:
        result = identify(telemetry())
        assert result["recommended_next_test"]["executed"] is False
        assert "nothing was transmitted" in result["note"]


# ---------------------------------------------------------------------------
# The shape clients render
# ---------------------------------------------------------------------------


class TestIdentify:
    def test_the_result_carries_both_the_prose_and_the_structure(self) -> None:
        result = identify(telemetry())
        assert result["size_bytes"] == len(telemetry())
        assert result["best"] == result["hypotheses"][0]["protocol"]
        assert result["confidence"] == result["hypotheses"][0]["confidence"]
        assert result["rendered"].startswith("Possible ")
        assert "+ " in result["rendered"]

    def test_evidence_is_rendered_with_signs_an_operator_can_scan(self) -> None:
        hypothesis = classify(telemetry())[0].as_dict()
        assert all(item.startswith(("+ ", "- ")) for item in hypothesis["evidence"])

    def test_the_framing_report_is_optional(self) -> None:
        assert "framing" not in identify(telemetry())
        assert "framing" in identify(telemetry(), include_report=True)

    def test_timestamps_add_periodicity_as_supporting_evidence(self) -> None:
        stamps = [index * 100_000_000 for index in range(200)]
        with_timing = classify(telemetry(200), timestamps_ns=stamps)[0]
        assert any("periodic" in item for item in with_timing.supporting)

    def test_periodicity_alone_does_not_move_the_score(self) -> None:
        """Timing supports whatever the framing turns out to be; it is not
        evidence for any particular protocol."""
        stamps = [index * 100_000_000 for index in range(200)]
        without = classify(telemetry(200))[0].confidence
        with_timing = classify(telemetry(200), timestamps_ns=stamps)[0].confidence
        assert with_timing == without

    def test_a_precomputed_report_is_reused_rather_than_recomputed(self) -> None:
        from fielddeck.analysis import framing

        data = telemetry()
        report = framing.analyze(data)
        assert classify(data, report=report)[0].protocol == classify(data)[0].protocol
