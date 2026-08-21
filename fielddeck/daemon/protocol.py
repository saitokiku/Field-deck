"""The ``instrumentd`` wire protocol.

Newline-delimited JSON over a Unix domain socket.  No pickle, no
language-specific serialization — anything that can write a line of JSON can
drive FieldDeck, and every message is readable in a log.

Requests::

    {"v":1,"id":"7","method":"action.execute","params":{...}}

Responses::

    {"v":1,"id":"7","ok":true,"result":{...}}
    {"v":1,"id":"7","ok":false,"error":{"code":"PermissionDenied", ...}}

Server-pushed events::

    {"v":1,"type":"event","subscription":"sub-1","event":{...}}

The protocol is versioned.  Additive changes keep ``v``; anything that breaks
an existing client bumps it.
"""

from __future__ import annotations

import json
from typing import Any

from fielddeck import RPC_PROTOCOL_VERSION
from fielddeck.common.errors import InvalidRequest

__all__ = [
    "MAX_LINE_BYTES",
    "RPC_PROTOCOL_VERSION",
    "RpcRequest",
    "decode_request",
    "encode_error",
    "encode_event",
    "encode_response",
]

#: Ceiling on one protocol frame, in either direction.
#:
#: Both the server and the client pass this to asyncio as their stream limit,
#: so it is the real limit rather than a documented one. asyncio's own default
#: is 64 KiB, which is small enough that a CAN listen of a few hundred frames
#: overran it — and the failure mode was the reader dying, not a typed error.
#:
#: A frame beyond this ceiling costs the connection: the daemon will not
#: buffer an unbounded request in order to reply politely to it. Bulk data
#: belongs in a capture file, and every action that can produce a lot of it
#: has a bound (``duration_s``, ``max_frames``, ``max_bytes``) for that reason.
MAX_LINE_BYTES = 4 * 1024 * 1024


class RpcRequest:
    __slots__ = ("id", "method", "params")

    def __init__(self, id: str | None, method: str, params: dict[str, Any]) -> None:
        self.id = id
        self.method = method
        self.params = params

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RpcRequest(id={self.id!r}, method={self.method!r})"


def decode_request(line: bytes) -> RpcRequest:
    """Parse one request line, or raise :class:`InvalidRequest`."""
    if len(line) > MAX_LINE_BYTES:
        raise InvalidRequest(
            f"request exceeds {MAX_LINE_BYTES} bytes",
            details={"size": len(line), "limit": MAX_LINE_BYTES},
        )
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        # json.loads sniffs the encoding, so arbitrary bytes can surface as a
        # UnicodeDecodeError rather than a JSONDecodeError. Both are just a
        # malformed request and neither deserves a traceback in the log.
        raise InvalidRequest(f"malformed JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise InvalidRequest("request must be a JSON object")

    version = payload.get("v", RPC_PROTOCOL_VERSION)
    if version != RPC_PROTOCOL_VERSION:
        raise InvalidRequest(
            f"unsupported protocol version {version}; this daemon speaks v{RPC_PROTOCOL_VERSION}",
            details={"client_version": version, "server_version": RPC_PROTOCOL_VERSION},
        )

    method = payload.get("method")
    if not isinstance(method, str) or not method:
        raise InvalidRequest("request needs a 'method' string")

    params = payload.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise InvalidRequest("'params' must be an object")

    request_id = payload.get("id")
    if request_id is not None and not isinstance(request_id, (str, int)):
        raise InvalidRequest("'id' must be a string or integer")

    return RpcRequest(
        id=str(request_id) if request_id is not None else None,
        method=method,
        params=params,
    )


def _line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, default=str, separators=(",", ":")) + "\n").encode("utf-8")


def encode_response(request_id: str | None, result: Any) -> bytes:
    return _line({"v": RPC_PROTOCOL_VERSION, "id": request_id, "ok": True, "result": result})


def encode_error(request_id: str | None, error: dict[str, Any]) -> bytes:
    return _line({"v": RPC_PROTOCOL_VERSION, "id": request_id, "ok": False, "error": error})


def encode_event(subscription_id: str, event: dict[str, Any]) -> bytes:
    return _line(
        {
            "v": RPC_PROTOCOL_VERSION,
            "type": "event",
            "subscription": subscription_id,
            "event": event,
        }
    )
