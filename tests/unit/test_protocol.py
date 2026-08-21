"""The RPC codec: newline-delimited JSON, versioned, bounded.

Everything that drives FieldDeck speaks this, so the decoder is a trust
boundary.  It has to reject three things clearly rather than crashing or
guessing: a frame that is too large to buffer, a version it does not speak,
and anything that is not a well-formed request.  Each refusal is a typed
error, because a client that gets a connection reset cannot tell a protocol
mismatch from a dead daemon.
"""

from __future__ import annotations

import json

import pytest

from fielddeck import RPC_PROTOCOL_VERSION
from fielddeck.common.errors import (
    ERROR_CLASSES,
    ErrorCode,
    FieldDeckError,
    InvalidRequest,
    PermissionDenied,
    SafetyLimitExceeded,
    error_from_dict,
)
from fielddeck.daemon.protocol import (
    MAX_LINE_BYTES,
    decode_request,
    encode_error,
    encode_event,
    encode_response,
)


def line(payload: dict) -> bytes:
    return (json.dumps(payload) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


class TestDecoding:
    def test_a_well_formed_request_decodes(self) -> None:
        request = decode_request(
            line(
                {
                    "v": RPC_PROTOCOL_VERSION,
                    "id": "7",
                    "method": "action.execute",
                    "params": {"action": "psu.status"},
                }
            )
        )
        assert (request.id, request.method) == ("7", "action.execute")
        assert request.params == {"action": "psu.status"}

    def test_the_version_may_be_omitted_and_defaults_to_this_one(self) -> None:
        assert decode_request(line({"method": "hello"})).method == "hello"

    def test_params_default_to_empty(self) -> None:
        assert decode_request(line({"method": "hello"})).params == {}
        assert decode_request(line({"method": "hello", "params": None})).params == {}

    def test_an_integer_id_is_accepted_and_normalised_to_text(self) -> None:
        assert decode_request(line({"id": 7, "method": "hello"})).id == "7"

    def test_a_request_without_an_id_is_allowed(self) -> None:
        assert decode_request(line({"method": "hello"})).id is None

    def test_a_version_mismatch_names_both_sides(self) -> None:
        """A client from the future gets a diagnosis, not a reset connection."""
        with pytest.raises(InvalidRequest) as caught:
            decode_request(line({"v": RPC_PROTOCOL_VERSION + 1, "method": "hello"}))

        assert caught.value.details["client_version"] == RPC_PROTOCOL_VERSION + 1
        assert caught.value.details["server_version"] == RPC_PROTOCOL_VERSION
        assert "this daemon speaks" in str(caught.value)

    def test_an_oversized_line_is_refused_with_its_size(self) -> None:
        """Bulk data belongs in a capture file, not in a protocol frame."""
        payload = b'{"method":"hello","params":{"blob":"' + b"x" * MAX_LINE_BYTES + b'"}}\n'
        with pytest.raises(InvalidRequest) as caught:
            decode_request(payload)

        assert caught.value.details["limit"] == MAX_LINE_BYTES
        assert caught.value.details["size"] == len(payload)

    def test_a_frame_at_the_limit_is_still_decoded(self) -> None:
        filler = "x" * (MAX_LINE_BYTES - 64)
        payload = line({"method": "hello", "params": {"note": filler}})
        assert len(payload) <= MAX_LINE_BYTES
        assert decode_request(payload).method == "hello"

    @pytest.mark.parametrize(
        "payload",
        [
            b"{not json}\n",
            b"\n",
            b"\xff\xfe\x00\x01\n",  # arbitrary bytes: a UnicodeDecodeError inside json
            b'{"method": "hello"',
        ],
    )
    def test_malformed_input_is_an_invalid_request_not_a_crash(self, payload: bytes) -> None:
        with pytest.raises(InvalidRequest):
            decode_request(payload)

    @pytest.mark.parametrize("payload", [b"[]\n", b'"hello"\n', b"42\n", b"null\n"])
    def test_a_request_must_be_an_object(self, payload: bytes) -> None:
        with pytest.raises(InvalidRequest, match="JSON object"):
            decode_request(payload)

    @pytest.mark.parametrize("method", [None, "", 7, [], {}])
    def test_a_request_needs_a_method_string(self, method: object) -> None:
        with pytest.raises(InvalidRequest, match="'method' string"):
            decode_request(line({"method": method}))

    @pytest.mark.parametrize("params", ["nope", 7, []])
    def test_params_must_be_an_object(self, params: object) -> None:
        with pytest.raises(InvalidRequest, match="'params' must be an object"):
            decode_request(line({"method": "hello", "params": params}))

    @pytest.mark.parametrize("request_id", [[], {}, 1.5])
    def test_an_id_must_be_a_string_or_an_integer(self, request_id: object) -> None:
        with pytest.raises(InvalidRequest, match="'id' must be"):
            decode_request(line({"id": request_id, "method": "hello"}))


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


class TestEncoding:
    def test_a_response_is_one_line_of_json(self) -> None:
        encoded = encode_response("7", {"ok": True})
        assert encoded.endswith(b"\n")
        assert encoded.count(b"\n") == 1

        decoded = json.loads(encoded)
        assert decoded == {
            "v": RPC_PROTOCOL_VERSION,
            "id": "7",
            "ok": True,
            "result": {"ok": True},
        }

    def test_an_error_response_carries_the_typed_payload(self) -> None:
        error = PermissionDenied(
            "psu.set requires an active POWER authorization",
            details={"hint": "fdctl arm power --ttl 60"},
            preserved="no command was sent to the device",
        )
        decoded = json.loads(encode_error("7", error.to_dict()))

        assert decoded["ok"] is False
        assert decoded["error"]["code"] == str(ErrorCode.PERMISSION_DENIED)
        assert decoded["error"]["preserved"] == "no command was sent to the device"

    def test_an_event_frame_is_tagged_and_carries_its_subscription(self) -> None:
        decoded = json.loads(encode_event("sub-1", {"type": "ESTOP"}))
        assert decoded["type"] == "event"
        assert decoded["subscription"] == "sub-1"
        assert decoded["event"] == {"type": "ESTOP"}

    def test_unserialisable_values_degrade_to_text_rather_than_failing(self) -> None:
        """A driver returning an exotic object must not break the connection."""
        decoded = json.loads(encode_response("1", {"path": object()}))
        assert isinstance(decoded["result"]["path"], str)

    def test_a_response_survives_a_round_trip_through_the_decoder(self) -> None:
        """Encoder and decoder agree on the frame shape, including the newline."""
        payload = encode_response("7", {"value": 1})
        assert json.loads(payload)["id"] == "7"


# ---------------------------------------------------------------------------
# Typed errors across the wire
# ---------------------------------------------------------------------------


class TestErrorRoundTrip:
    @pytest.mark.parametrize("code", sorted(ERROR_CLASSES))
    def test_every_wire_code_rebuilds_its_own_class(self, code: str) -> None:
        rebuilt = error_from_dict({"code": code, "message": "something happened"})
        assert type(rebuilt) is ERROR_CLASSES[code]
        assert str(rebuilt.code) == code

    def test_details_and_preserved_survive_the_trip(self) -> None:
        original = SafetyLimitExceeded(
            "psu.voltage=60V exceeds maximum 30V",
            details={"quantity": "psu.voltage", "value": 60.0},
            preserved="no command was sent to the device",
        )
        rebuilt = error_from_dict(json.loads(json.dumps(original.to_dict())))

        assert isinstance(rebuilt, SafetyLimitExceeded)
        assert rebuilt.message == original.message
        assert rebuilt.details == original.details
        assert rebuilt.preserved == original.preserved

    def test_an_unknown_code_becomes_a_generic_error_rather_than_vanishing(self) -> None:
        rebuilt = error_from_dict({"code": "SomethingNewerThanUs", "message": "from the future"})
        assert type(rebuilt) is FieldDeckError
        assert rebuilt.code is ErrorCode.INTERNAL_ERROR
        assert rebuilt.message == "from the future"

    def test_a_known_code_that_has_no_class_keeps_its_identity(self) -> None:
        rebuilt = error_from_dict({"code": str(ErrorCode.INTERNAL_ERROR), "message": "boom"})
        assert rebuilt.code is ErrorCode.INTERNAL_ERROR

    def test_the_preserved_line_is_part_of_the_message_a_human_reads(self) -> None:
        error = FieldDeckError("capture failed", preserved="42 MiB of frames are on disk")
        assert str(error) == "capture failed (preserved: 42 MiB of frames are on disk)"
