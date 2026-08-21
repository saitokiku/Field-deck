"""``fielddeck-mcp``: the Model Context Protocol server, spoken over stdio.

This is how Claude reads FieldDeck.  It is deliberately the thinnest client in
the system, and what it *cannot* do is the point of it:

* It holds no hardware handles.  There is no serial port, no CAN socket, no
  VISA resource and no device file anywhere in this process.  Every answer it
  gives came from ``instrumentd`` over a Unix socket, which means every answer
  passed the dispatcher, the safety manager, the limit checks and the audit
  trail on the way out.
* It connects to the **restricted** socket (``instrumentd-ai.sock``).  The
  daemon stamps every request from there ``source=claude`` and refuses the
  arming methods at the transport, so this server cannot create an
  authorization grant even if this file were rewritten to try.  It never falls
  back to the full-authority socket: reaching hardware is not worth being
  mistaken for the operator.
* It executes no shell commands and reads no files.  File-shaped arguments are
  paths that the daemon resolves inside the session store or a configured
  firmware root, and refuses anywhere else.
* It exposes no tool that arms, disarms or clears an emergency stop.  It does
  expose ``estop``, because stopping is never the dangerous direction.

The protocol is implemented directly — JSON-RPC 2.0, one object per line, on
stdin and stdout — rather than through an SDK.  A field instrument should not
grow a dependency tree to answer twenty-nine read-only questions, and the
whole surface is ``initialize``, ``tools/list``, ``tools/call`` and ``ping``.

Two operational rules that a maintainer will otherwise learn the hard way:

**stdout is the protocol channel.**  A stray write there corrupts the session
and the client's error will not point back here.  Every diagnostic goes to
stderr, which is why this module never calls ``print``.

**Neither side may take the other down.**  A stopped daemon must still leave
``tools/list`` answerable and must turn a ``tools/call`` into an explanatory
error, not a traceback; and a malformed request from the client must cost that
request and nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fielddeck import __version__
from fielddeck.common.errors import (
    ErrorCode,
    FieldDeckError,
    InvalidRequest,
    TransportError,
)
from fielddeck.common.logging import configure_logging, get_logger
from fielddeck.common.models import ActionResult, ClientSource
from fielddeck.common.paths import socket_path as control_socket_path
from fielddeck.daemon.client import InstrumentClient
from fielddeck.mcp.tools import ToolDef, tool_by_name, tool_list_payload

__all__ = [
    "MCP_PROTOCOL_VERSION",
    "DaemonLink",
    "McpServer",
    "main",
    "restricted_socket_path",
]

_log = get_logger("fielddeck.mcp.server")

#: The MCP revision this server implements.
MCP_PROTOCOL_VERSION = "2025-06-18"

#: Revisions whose ``initialize`` this server answers in kind.  An unknown
#: version is answered with :data:`MCP_PROTOCOL_VERSION` and the client
#: decides whether it can live with that, which is what the spec asks for.
KNOWN_PROTOCOL_VERSIONS = frozenset({"2025-06-18", "2025-03-26", "2024-11-05"})

SERVER_NAME = "fielddeck"

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: Ceiling on one inbound message.  Matches the daemon's own frame limit so a
#: request that instrumentd would refuse is refused here rather than buffered.
MAX_MESSAGE_BYTES = 4 * 1024 * 1024

#: Ceiling on one tool result, measured on the compact encoding.  A capture of
#: two hundred thousand frames is a file, not a conversation turn; past this
#: point the reply points at the artifact instead.
MAX_RESULT_BYTES = 64 * 1024

#: Longest list handed back inline before it is cut down to a sample.
MAX_LIST_ITEMS = 50

#: Longest single string handed back inline (a hexdump, a decoder log).
MAX_STRING_CHARS = 8_000

#: Structures deeper than this are summarised rather than walked.
MAX_DEPTH = 8

#: Tool calls allowed to be in flight at once.  Several is right — a three
#: second capture must not block a status check — but unbounded means a client
#: bug becomes a device queue nobody can see.
MAX_CONCURRENT_CALLS = 8

#: Result keys never dropped when a payload is over budget: they say where the
#: real data went and what the daemon thought of it.
_PROTECTED_KEYS = frozenset(
    {"artifact", "artifacts", "warning", "error", "count", "session", "session_id", "device"}
)

INSTRUCTIONS = """\
FieldDeck is a field engineering console: CAN, serial/RS485, Modbus, bench \
instruments, logic analyzers and firmware, all behind one daemon (instrumentd) \
that owns the hardware.

You are a read-and-reason client. Everything you can call here is PASSIVE \
(observe, decode, correlate, compute) except two SCPI/Modbus query tools, which \
transmit and therefore need an operator's authorization, and estop, which you \
may always call.

Three rules make you useful rather than dangerous:

1. Capture first, reason second. Prefer passive observation and the \
deterministic decoders over inference. Report what you observed, what is \
likely, what is unknown, and the safest next test — in those terms.
2. You cannot authorize anything. This server is on the restricted socket and \
has no path to arming. When a call is denied, say what you wanted to send and \
why, and ask the operator to arm it. Retrying will be refused identically.
3. Never assume electrical facts. Voltages, pinouts, RS232-versus-TTL, RS485 \
polarity, CAN termination and bitrates are things an operator tells you or an \
instrument measures. A guess stated as a fact damages hardware.

Start with fielddeck_status, then permission_status if you are considering \
anything active. If you believe something is being damaged, call estop and \
explain afterwards.\
"""


def restricted_socket_path() -> Path:
    """Where this server looks for instrumentd.

    ``FIELDDECK_MCP_SOCKET`` wins, for deployments that put the restricted
    socket somewhere of their own.  Otherwise it is the AI socket beside the
    control socket — never the control socket itself.
    """
    override = os.environ.get("FIELDDECK_MCP_SOCKET")
    if override:
        return Path(override).expanduser()
    return control_socket_path().with_name("instrumentd-ai.sock")


class JsonRpcError(Exception):
    """A protocol-level failure: malformed request, unknown method.

    Distinct from a *tool* failure, which is a successful JSON-RPC response
    carrying ``isError`` — the model needs to read that one and act on it,
    while this one usually means the client and the server disagree.
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# ---------------------------------------------------------------------------
# The link to instrumentd
# ---------------------------------------------------------------------------


class DaemonLink:
    """The only route this process has to anything physical.

    Holds at most one connection, opened lazily so the server starts and lists
    its tools with the daemon stopped.  A dropped connection is discarded and
    reopened on the next call, but a call that failed mid-flight is **not**
    retried: the daemon may have already executed it, and quietly running an
    instrument command twice because a socket hiccupped is exactly the kind of
    helpfulness an instrument must not have.
    """

    def __init__(
        self,
        socket_path: Path | None = None,
        *,
        source: ClientSource = ClientSource.CLAUDE,
    ) -> None:
        self.socket_path = socket_path or restricted_socket_path()
        # Declared honestly even though the restricted socket overrides it.
        # If someone points this at another socket, the audit trail should
        # still say who was really asking.
        self.source = source
        self._client: InstrumentClient | None = None
        self._lock = asyncio.Lock()

    async def _connected(self) -> InstrumentClient:
        async with self._lock:
            if self._client is not None:
                return self._client
            client = InstrumentClient(self.socket_path, source=self.source)
            try:
                await client.connect()
            except TransportError as exc:
                raise self._unreachable(exc) from exc
            _log.info(
                "connected to instrumentd",
                extra={
                    "socket": str(self.socket_path),
                    "restricted": bool(client.server_info.get("restricted")),
                    "simulated": bool(client.server_info.get("simulated")),
                },
            )
            if not client.server_info.get("restricted", False):
                # Worth saying out loud: the operator has pointed the AI client
                # at the full control surface. Requests are still stamped
                # source=claude and arming still needs ClientSource.HMI/FDCTL,
                # but the transport-level refusal is gone.
                _log.warning(
                    "connected to a socket that permits authorization methods; "
                    "the intended target is the restricted socket",
                    extra={"socket": str(self.socket_path)},
                )
            self._client = client
            return client

    def _unreachable(self, exc: TransportError) -> TransportError:
        """Turn 'no socket there' into something an operator can act on."""
        control = control_socket_path()
        hints = [
            "Start it with 'systemctl start instrumentd', or for a development "
            "instance run 'instrumentd' (FIELDDECK_SIM=1 for simulated devices).",
        ]
        if not self.socket_path.exists() and control.exists():
            hints.append(
                f"The full control socket at {control} does exist. This server will not "
                "use it: on that socket an AI client is indistinguishable from the "
                "operator. Restart instrumentd so it opens the restricted socket, or set "
                "FIELDDECK_MCP_SOCKET to the restricted socket's real path."
            )
        return TransportError(
            f"instrumentd is not reachable on {self.socket_path}: {exc.message}",
            details={
                "socket": str(self.socket_path),
                "control_socket_present": control.exists(),
                "hints": hints,
            },
            preserved="nothing was attempted; no device was touched",
        )

    async def _drop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    def _lost(self, exc: TransportError, what: str) -> TransportError:
        """A connection that failed mid-call, said out loud.

        The distinction that matters to a caller is not "the socket broke" but
        "nobody knows whether this ran".  The daemon may have executed the
        request and died before answering, so the answer is to go and look —
        never to send it again on the assumption that it did not land.
        """
        return TransportError(
            f"{what} did not complete: {exc.message}",
            details={
                **exc.details,
                "socket": str(self.socket_path),
                "call": what,
                "retried": False,
            },
            preserved=(
                "instrumentd owns the session log; whatever it had already written is on "
                "disk. This request was not resent — check fielddeck_status and "
                "session_events to find out whether it ran."
            ),
        )

    async def call(self, method: str, params: dict[str, Any], *, timeout_s: float) -> Any:
        """One RPC round trip.  Raises the daemon's own typed error."""
        client = await self._connected()
        try:
            return await client.call(method, params, timeout_s=timeout_s)
        except TransportError as exc:
            await self._drop()
            raise self._lost(exc, method) from exc

    async def execute(
        self, action: str, params: dict[str, Any], *, timeout_s: float
    ) -> ActionResult:
        """Run one action.  Raises on refusal or failure."""
        client = await self._connected()
        try:
            return await client.execute(action, params, timeout_s=timeout_s)
        except TransportError as exc:
            await self._drop()
            raise self._lost(exc, action) from exc

    async def close(self) -> None:
        await self._drop()


# ---------------------------------------------------------------------------
# Result trimming
# ---------------------------------------------------------------------------


def render(value: Any) -> str:
    """The single place a payload becomes text.

    Trimming measures what this produces, and the tool reply is built from it,
    so the budget is a bound on the bytes that actually reach the model rather
    than on a compact encoding nobody ever sees.
    """
    return json.dumps(value, default=str, indent=2)


def _encoded_size(value: Any) -> int:
    return len(render(value).encode("utf-8"))


def _shrink(
    value: Any, *, notes: list[dict[str, Any]], path: str, depth: int, list_limit: int
) -> Any:
    """Cut lists and strings down to a readable sample, recording each cut.

    Every cut is announced.  A model that silently receives the first fifty of
    twelve thousand frames will reason about "the traffic" and be wrong about
    it; one that is told it saw fifty of twelve thousand goes and reads the
    artifact.
    """
    if depth > MAX_DEPTH:
        notes.append({"field": path, "elided": True, "reason": "structure nested too deeply"})
        return "<elided: too deeply nested>"
    if isinstance(value, dict):
        return {
            str(key): _shrink(
                item, notes=notes, path=f"{path}.{key}", depth=depth + 1, list_limit=list_limit
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        total = len(value)
        # Slicing before recursing keeps every pass proportional to what is
        # kept, not to the two hundred thousand frames that were offered.
        kept = value[:list_limit]
        if total > list_limit:
            notes.append(
                {
                    "field": path,
                    "returned": list_limit,
                    "total": total,
                    "advice": "the complete data is in the session artifact named in this result",
                }
            )
        return [
            _shrink(item, notes=notes, path=f"{path}[]", depth=depth + 1, list_limit=list_limit)
            for item in kept
        ]
    if isinstance(value, str) and len(value) > MAX_STRING_CHARS:
        notes.append({"field": path, "returned_chars": MAX_STRING_CHARS, "total_chars": len(value)})
        return value[:MAX_STRING_CHARS] + " …[truncated]"
    return value


#: Sample sizes tried in order until the result fits the budget.  Shrinking a
#: list beats dropping it: fifteen frames with the total stated is something a
#: model can reason from, an absent field is not.
_SAMPLE_STEPS = (MAX_LIST_ITEMS, 25, 12, 5, 2, 1)


def trim_result(payload: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Bound a daemon result to something a model can actually read.

    Returns the trimmed payload and the list of cuts made.  Lists are sampled
    progressively harder until the whole thing fits; only if that is not enough
    are whole fields dropped, largest first, and never the ones that say where
    the real data lives — an answer that loses its artifact pointer is worse
    than an answer that admits it dropped a field.
    """
    notes: list[dict[str, Any]] = []
    trimmed: Any = payload
    for limit in _SAMPLE_STEPS:
        notes = []
        trimmed = _shrink(payload, notes=notes, path="result", depth=0, list_limit=limit)
        if _encoded_size(trimmed) <= MAX_RESULT_BYTES:
            return trimmed, notes

    if not isinstance(trimmed, dict):
        notes.append(
            {
                "field": "result",
                "dropped": True,
                "reason": f"result exceeds {MAX_RESULT_BYTES} bytes",
            }
        )
        return "<dropped: too large to return inline>", notes

    droppable = sorted(
        ((key, _encoded_size(item)) for key, item in trimmed.items() if key not in _PROTECTED_KEYS),
        key=lambda pair: pair[1],
        reverse=True,
    )
    for key, size in droppable:
        if _encoded_size(trimmed) <= MAX_RESULT_BYTES:
            break
        trimmed.pop(key, None)
        notes.append(
            {
                "field": f"result.{key}",
                "dropped": True,
                "bytes": size,
                "reason": f"result exceeds {MAX_RESULT_BYTES} bytes",
                "advice": "read it from the session artifact, or ask for a narrower window",
            }
        )
    return trimmed, notes


# ---------------------------------------------------------------------------
# Turning a FieldDeck error into the next thing to do
# ---------------------------------------------------------------------------


def next_step_for(error: FieldDeckError, tool: ToolDef) -> str | None:
    """What the model should do about this failure, in one sentence.

    The daemon's messages are already actionable for a human at a terminal.
    This adds the part a model gets wrong: whether retrying is pointless, and
    who has to do something instead.
    """
    if error.code is ErrorCode.PERMISSION_DENIED:
        hint = error.details.get("hint") or f"fdctl arm {str(tool.permission).lower()} --ttl 60"
        return (
            "Stop here. This needs a human. Tell the operator what you want to send, to which "
            f"device, and why, then ask them to run `{hint}` or press ARM on the HMI. Do not "
            "retry until they confirm; the refusal is a policy decision, not a transient error."
        )
    if error.code is ErrorCode.ESTOP_ACTIVE:
        return (
            "An emergency stop is latched, so only PASSIVE work is available. Clearing it is a "
            "human's job (`fdctl estop clear` or the HMI) and cannot be done from here. Use the "
            "time to read the session: session_window around the fault is usually the fastest "
            "way to explain what happened."
        )
    if error.code is ErrorCode.TRANSPORT_ERROR:
        if tool.name == "estop":
            return (
                "The emergency stop could NOT be confirmed. Say so to the operator immediately, "
                "in plain words, and tell them to hit the physical stop or run `fdctl estop`. Do "
                "not describe the machine as safe."
            )
        return (
            "instrumentd could not be reached, or did not answer in time. No result means no "
            "knowledge: check fielddeck_status before assuming anything about the call's effect."
        )
    if error.code in (ErrorCode.DEVICE_NOT_FOUND, ErrorCode.DEVICE_DISCONNECTED):
        return (
            "Call fielddeck_discover and use an id from the inventory. Device ids come from "
            "vendor/serial information, not from /dev names, so a remembered path from an "
            "earlier session may no longer refer to the same adapter."
        )
    if error.code is ErrorCode.DEVICE_BUSY:
        return (
            "Another client holds this device. Wait, or ask the operator what is running; do not "
            "loop on it."
        )
    if error.code is ErrorCode.SESSION_ERROR:
        return (
            "The session, or something asked for inside it, could not be resolved. Use "
            "session_list and session_get to see which sessions exist and what each one "
            "actually contains. If nothing is recording, an operator has to start a session "
            '(`fdctl session start "<name>"`) before captures can be saved.'
        )
    if error.code in (ErrorCode.UNSUPPORTED_CAPABILITY, ErrorCode.UNKNOWN_ACTION):
        return (
            "This device does not implement that. Check its capabilities with "
            "fielddeck_discover before proposing an alternative — a simulated or degraded "
            "driver implements a subset of what the real one does."
        )
    if error.code is ErrorCode.INVALID_REQUEST:
        return (
            "The arguments were rejected before anything was sent; details.errors names the "
            "field and the problem. Unlike a permission failure, this one is yours to fix and "
            "safe to call again once corrected."
        )
    return None


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------

Writer = Callable[[bytes], Awaitable[None]]


class McpServer:
    """One MCP session: JSON-RPC 2.0 over a pair of byte streams."""

    def __init__(self, link: DaemonLink) -> None:
        self._link = link
        self._initialized = False
        self._client_info: dict[str, Any] = {}
        self._protocol_version = MCP_PROTOCOL_VERSION
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
        self._tasks: set[asyncio.Task[None]] = set()

    # -- transport ---------------------------------------------------------

    async def serve(self, reader: asyncio.StreamReader, write: Writer) -> None:
        """Read messages until EOF, then return.

        EOF is the client closing stdin, which is how an MCP client says it is
        done.  It is a normal shutdown, not an error.
        """
        while True:
            try:
                line = await reader.readline()
            except (asyncio.LimitOverrunError, ValueError):
                # The buffer overran, so the stream is no longer aligned to
                # message boundaries and nothing after this point can be
                # trusted to be a whole message.
                await self._safe_write(
                    write,
                    _error_payload(
                        None,
                        PARSE_ERROR,
                        f"message exceeds the {MAX_MESSAGE_BYTES} byte limit",
                    ),
                )
                break
            except (ConnectionResetError, BrokenPipeError):
                break
            if not line:
                _log.info("client closed stdin; shutting down")
                break
            if not line.strip():
                continue
            await self._on_line(line, write)
        await self.aclose()

    async def _on_line(self, line: bytes, write: Writer) -> None:
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            await self._safe_write(write, _error_payload(None, PARSE_ERROR, f"invalid JSON: {exc}"))
            return

        if isinstance(message, list):
            # Batching was removed in MCP 2025-06-18. Saying so is more useful
            # than half-supporting it.
            await self._safe_write(
                write,
                _error_payload(None, INVALID_REQUEST, "JSON-RPC batches are not supported"),
            )
            return
        if not isinstance(message, dict):
            await self._safe_write(
                write, _error_payload(None, INVALID_REQUEST, "message must be a JSON object")
            )
            return

        method = message.get("method")
        if not isinstance(method, str):
            # A response to a request this server never sent. Ignoring it is
            # correct; answering would start a loop.
            _log.debug("ignoring non-request message", extra={"keys": sorted(message)})
            return

        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            await self._safe_write(
                write,
                _error_payload(message.get("id"), INVALID_PARAMS, "'params' must be an object"),
            )
            return

        request_id = message.get("id")
        if request_id is None:
            self._on_notification(method, params)
            return

        task = asyncio.create_task(self._respond(request_id, method, params, write))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        """Notifications get no reply, ever — including unknown ones."""
        if method == "notifications/initialized":
            self._initialized = True
            _log.info("session initialized", extra={"client": self._client_info.get("name")})
        elif method == "notifications/cancelled":
            # Cancellation is accepted and logged but not acted on: an
            # in-flight capture is a bounded, harmless read, and cancelling it
            # halfway would leave the model reasoning about a partial window it
            # was never told the length of.
            _log.info("cancellation notice", extra={"request": params.get("requestId")})
        else:
            _log.debug("ignoring notification", extra={"field_method": method})

    async def _respond(
        self, request_id: Any, method: str, params: dict[str, Any], write: Writer
    ) -> None:
        try:
            result = await self._handle(method, params)
        except JsonRpcError as exc:
            payload = _error_payload(request_id, exc.code, exc.message, exc.data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad request must not end the session
            _log.exception("unhandled error", extra={"field_method": method})
            payload = _error_payload(
                request_id, INTERNAL_ERROR, f"internal error: {exc}", {"type": type(exc).__name__}
            )
        else:
            payload = {"jsonrpc": "2.0", "id": request_id, "result": result}
        await self._safe_write(write, payload)

    async def _safe_write(self, write: Writer, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            await write(line)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            # The client is gone. The read loop will see EOF momentarily; there
            # is nowhere left to report this but stderr.
            _log.warning("cannot write to stdout", extra={"error": str(exc)})

    # -- methods -----------------------------------------------------------

    async def _handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            # Deliberately independent of the daemon: knowing what FieldDeck
            # could answer is useful precisely when it is not running.
            return {"tools": tool_list_payload()}
        if method == "tools/call":
            return await self._call_tool(params)
        raise JsonRpcError(
            METHOD_NOT_FOUND,
            f"unknown method {method!r}",
            {"supported": ["initialize", "ping", "tools/list", "tools/call"]},
        )

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        if isinstance(requested, str) and requested in KNOWN_PROTOCOL_VERSIONS:
            self._protocol_version = requested
        else:
            self._protocol_version = MCP_PROTOCOL_VERSION
        client_info = params.get("clientInfo")
        self._client_info = client_info if isinstance(client_info, dict) else {}
        _log.info(
            "initialize",
            extra={
                "client": self._client_info.get("name"),
                "requested_protocol": requested,
                "protocol": self._protocol_version,
                "socket": str(self._link.socket_path),
            },
        )
        return {
            "protocolVersion": self._protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": SERVER_NAME,
                "title": "FieldDeck",
                "version": __version__,
            },
            "instructions": INSTRUCTIONS,
        }

    async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise JsonRpcError(INVALID_PARAMS, "tools/call requires a 'name' string")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise JsonRpcError(INVALID_PARAMS, "'arguments' must be an object")
        try:
            tool = tool_by_name(name)
        except InvalidRequest as exc:
            raise JsonRpcError(INVALID_PARAMS, exc.message, exc.details) from exc

        async with self._slots:
            return await self._execute(tool, arguments)

    async def _execute(self, tool: ToolDef, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one tool and shape the reply.

        Every failure below becomes a *successful* JSON-RPC response carrying
        ``isError`` and an explanation, because the model is the one who has to
        act on it — a protocol error would be swallowed by the client's plumbing
        and reappear as "the tool didn't work".
        """
        call_name = tool.rpc_method or tool.action or tool.name
        try:
            call_name = tool.rpc_method or tool.resolve_action(arguments)
            params = tool.params_for(arguments)
            timeout_s = _timeout_for(tool, arguments)
            _log.info(
                "tool call",
                extra={
                    "tool": tool.name,
                    "action": call_name,
                    "permission": str(tool.permission),
                    "timeout_s": timeout_s,
                },
            )
            if tool.rpc_method is not None:
                payload = await self._link.call(tool.rpc_method, params, timeout_s=timeout_s)
                permission = tool.permission
            else:
                outcome = await self._link.execute(call_name, params, timeout_s=timeout_s)
                payload = outcome.result
                # The permission the dispatcher actually authorized, which for
                # an action with a permission_resolver can be lower than the
                # declared class.
                permission = outcome.permission
        except FieldDeckError as exc:
            _log.info(
                "tool refused or failed",
                extra={"tool": tool.name, "action": call_name, "error": str(exc.code)},
            )
            return _tool_reply(_error_envelope(tool, call_name, exc), is_error=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never let a client bug reach the model as a crash
            _log.exception("tool failed", extra={"tool": tool.name, "action": call_name})
            error = FieldDeckError(
                f"{tool.name} failed unexpectedly: {exc}",
                details={"type": type(exc).__name__, "action": call_name},
                preserved="no result was produced; nothing was written",
            )
            return _tool_reply(_error_envelope(tool, call_name, error), is_error=True)

        body = (
            tool.shape_result(payload)
            if tool.shape_result and isinstance(payload, dict)
            else payload
        )
        trimmed, notes = trim_result(body)
        envelope: dict[str, Any] = {
            "tool": tool.name,
            "call": call_name,
            "permission": str(permission),
            "ok": True,
            "result": trimmed,
        }
        if notes:
            envelope["truncated"] = notes
        return _tool_reply(envelope, is_error=False)

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()


def _timeout_for(tool: ToolDef, arguments: dict[str, Any]) -> float:
    """Client deadline for one call.

    A capture's own duration is added to the tool's budget so a five minute
    listen is not killed at thirty seconds by the client that asked for it.
    The daemon still applies its own ceiling.
    """
    duration = arguments.get("duration_s")
    extra = float(duration) if isinstance(duration, (int, float)) and duration > 0 else 0.0
    return tool.timeout_s + extra


def _error_envelope(tool: ToolDef, call_name: str, error: FieldDeckError) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "tool": tool.name,
        "call": call_name,
        "permission": str(tool.permission),
        "ok": False,
        "error": error.to_dict(),
    }
    next_step = next_step_for(error, tool)
    if next_step:
        envelope["next_step"] = next_step
    return envelope


def _tool_reply(envelope: dict[str, Any], *, is_error: bool) -> dict[str, Any]:
    """An MCP ``CallToolResult``.

    JSON as text: the model reads it, and a human reading the transcript can
    see exactly what the daemon said rather than a prose paraphrase of it.
    """
    return {
        "content": [{"type": "text", "text": render(envelope)}],
        "isError": is_error,
    }


def _error_payload(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


# ---------------------------------------------------------------------------
# stdio plumbing and entry point
# ---------------------------------------------------------------------------


async def _stdin_reader() -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES)
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    return reader


def _stdout_writer() -> Writer:
    """Serialised, off-loop writes to stdout.

    The lock keeps concurrent tool replies from interleaving into one corrupt
    line; the thread keeps a client that is slow to read from stalling a
    capture that is still running.
    """
    lock = asyncio.Lock()
    stream = sys.stdout.buffer

    def _write_all(data: bytes) -> None:
        stream.write(data)
        stream.flush()

    async def write(data: bytes) -> None:
        async with lock:
            await asyncio.to_thread(_write_all, data)

    return write


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fielddeck-mcp",
        description=(
            "FieldDeck MCP server (stdio). Read-only access to instrumentd for an AI "
            "client; it cannot arm FieldDeck and holds no hardware handles."
        ),
    )
    parser.add_argument(
        "--socket",
        metavar="PATH",
        help=(
            "instrumentd socket to use. Defaults to the restricted socket "
            "(instrumentd-ai.sock), or $FIELDDECK_MCP_SOCKET."
        ),
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Print the tool catalogue as JSON and exit, without serving. "
        "Use it to review exactly what an AI client is told it can do.",
    )
    parser.add_argument("--version", action="version", version=f"fielddeck-mcp {__version__}")
    return parser


async def _serve(socket_path: Path) -> int:
    link = DaemonLink(socket_path)
    server = McpServer(link)
    try:
        reader = await _stdin_reader()
    except (OSError, ValueError) as exc:
        _log.error(
            "cannot read stdin; this server speaks MCP over a pipe", extra={"error": str(exc)}
        )
        return 2
    write = _stdout_writer()
    _log.info(
        "fielddeck-mcp ready",
        extra={"socket": str(socket_path), "tools": len(tool_list_payload())},
    )
    try:
        await server.serve(reader, write)
    finally:
        await link.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # stderr only: stdout belongs to the protocol from here on.
    configure_logging(os.environ.get("FIELDDECK_LOG_LEVEL", "INFO"))
    if args.list_tools:
        sys.stdout.write(json.dumps(tool_list_payload(), indent=2) + "\n")
        return 0
    socket_path = Path(args.socket).expanduser() if args.socket else restricted_socket_path()
    try:
        return asyncio.run(_serve(socket_path))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
