"""The tool catalogue Claude sees, and the mapping to ``instrumentd`` actions.

This module is a data table, not a driver.  Every entry names an action that
already exists in the daemon; nothing here opens a port, and nothing here
decides whether a call is allowed.  That decision belongs to the dispatcher
and the safety manager, on the other side of the socket, where it can be
audited — a client that made its own authorization decisions would be a
second, weaker copy of the safety model.

Two things about this file deserve care when editing it:

**Descriptions are read by a model, not by a person browsing docs.**  They
are the only place Claude learns that ``can_capture`` listens and
``scpi_query`` transmits.  Vague wording here turns into a model that probes
a bus because it could not tell the difference.  Every description therefore
states its permission class explicitly, and anything above PASSIVE says, in
words, that the operator must arm it and that retrying will not help.

**The permission recorded here is a label, never an enforcement point.**  It
is copied from the action's own ``@action(permission=...)`` declaration so the
description can be honest about what a call will cost.  If the two ever
disagree the daemon wins, the call is refused, and this table is what needs
fixing.

No state-changing DUT tool is exposed: there is no CAN transmit, no serial
send, no PSU control, no flash.  ``estop`` is the one tool that changes
hardware state, and it only ever moves hardware toward safety.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fielddeck.common.errors import InvalidRequest
from fielddeck.common.models import PermissionLevel

__all__ = [
    "TOOLS",
    "ToolDef",
    "tool_by_name",
    "tool_definitions",
    "tool_list_payload",
]

#: Builds the RPC/action parameters from the tool arguments.  Used where the
#: tool surface deliberately differs from the action surface (``modbus_read``
#: collapses four actions into one tool, for example).
ParamBuilder = Callable[[dict[str, Any]], dict[str, Any]]

#: Post-processes the daemon's result before it reaches the model.  Only ever
#: narrows or relabels; never invents a field the daemon did not report.
ResultShaper = Callable[[dict[str, Any]], dict[str, Any]]

#: Picks the concrete action for one call, for tools that front several.
ActionSelector = Callable[[dict[str, Any]], str]


@dataclass(frozen=True, slots=True)
class ToolDef:
    """One MCP tool and the ``instrumentd`` call behind it."""

    name: str
    #: Short human label, shown in tool pickers.
    title: str
    #: Model-facing prose.  ``describe()`` appends the permission paragraph.
    body: str
    permission: PermissionLevel
    input_schema: dict[str, Any]
    #: The action executed through ``action.execute``.  Exactly one of this
    #: and :attr:`rpc_method` is set.
    action: str | None = None
    #: A daemon RPC method called directly, for the two things that are not
    #: actions: the safety snapshot and the emergency stop.
    rpc_method: str | None = None
    #: Truthful, and mirrored from the action declaration.  Only ``estop``
    #: changes hardware state, and only toward safety.
    state_changing: bool = False
    #: True when the call reaches beyond FieldDeck to a device or a bus.
    touches_hardware: bool = True
    #: Client-side deadline for this call.  ``duration_s``, where a tool has
    #: one, is added on top so a long capture is not cut off by its own
    #: timeout.
    timeout_s: float = 30.0
    build_params: ParamBuilder | None = None
    select_action: ActionSelector | None = None
    shape_result: ResultShaper | None = None
    #: Extra sentences appended after the body, before the permission note.
    caveats: tuple[str, ...] = field(default_factory=tuple)
    #: Replaces the generated permission paragraph where the standard wording
    #: would be untrue.  Only ``estop`` needs it.
    permission_note: str | None = None

    def resolve_action(self, arguments: dict[str, Any]) -> str:
        """The action name this call will execute."""
        if self.select_action is not None:
            return self.select_action(arguments)
        if self.action is not None:
            return self.action
        raise InvalidRequest(
            f"tool {self.name} has no action; it is served by RPC {self.rpc_method}",
            details={"tool": self.name},
        )

    def params_for(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Arguments translated into daemon parameters.

        Values are passed through untouched where the shapes already match.
        Validation is the daemon's job: its Pydantic models reject unknown
        keys and out-of-range values with a typed error, and duplicating those
        rules here would mean two places to keep in step and one of them
        silently wrong.
        """
        if self.build_params is not None:
            return self.build_params(arguments)
        return _drop_none(arguments)

    def describe(self) -> str:
        parts = [self.body.strip(), *(c.strip() for c in self.caveats)]
        parts.append(self.permission_note or _permission_note(self.permission))
        return "\n\n".join(parts)

    def to_payload(self) -> dict[str, Any]:
        """One entry of the MCP ``tools/list`` result."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.describe(),
            "inputSchema": self.input_schema,
            "annotations": {
                "title": self.title,
                "readOnlyHint": not self.state_changing,
                # Nothing here erases, overwrites or reprograms anything.
                # ESTOP interrupts work, but it preserves every byte captured
                # so far, and a client that hesitated to call it would be a
                # worse client.
                "destructiveHint": False,
                "idempotentHint": not self.state_changing or self.name == "estop",
                "openWorldHint": self.touches_hardware,
            },
        }


# ---------------------------------------------------------------------------
# Description helpers
# ---------------------------------------------------------------------------


def _permission_note(permission: PermissionLevel) -> str:
    """The paragraph every tool ends with.

    Written for a model deciding what to do next.  The important sentence is
    the last one: a permission failure is not a transient error and retrying
    is not the remedy.
    """
    if permission is PermissionLevel.PASSIVE:
        return (
            "Permission: PASSIVE. Needs no authorization, transmits nothing to the device "
            "under test, and stays available while an emergency stop is latched."
        )
    level = str(permission).lower()
    return (
        f"Permission: {permission}. This puts a signal on the bus, so instrumentd refuses it "
        f"unless an operator has an active {permission} grant. This server cannot create one: "
        "it is connected to the restricted socket, where the arming methods are refused at the "
        "transport. If the call comes back PermissionDenied, do not retry and do not look for "
        "another route. Tell the operator exactly what you want to send and why, and ask them "
        f"to run `fdctl arm {level} --ttl 60` or press ARM on the HMI first."
    )


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    """A closed object schema.

    ``additionalProperties: false`` mirrors the daemon, whose parameter models
    are all ``extra="forbid"``. Advertising a laxer schema than the server
    enforces just means the model learns about the restriction by being
    rejected.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


_NO_ARGS = _schema({})

_DEVICE_PROPERTY = {
    "type": "string",
    "description": (
        "Device id, a configured alias, or a role such as 'role:psu'. "
        "Get ids from fielddeck_discover or the per-transport listing tools."
    ),
}

_SESSION_PROPERTY = {
    "type": "string",
    "description": "Session id. Omit to use the session that is currently recording.",
}

#: Where analysis tools get their bytes.  Mirrors ``DataParams``: exactly one
#: source, because quietly preferring one of several is how an analysis ends
#: up describing bytes nobody sent.
_DATA_PROPERTIES: dict[str, Any] = {
    "path": {
        "type": "string",
        "description": (
            "Capture file relative to the session directory, e.g. "
            "'serial/capture-0001.bin'. Reads are confined to the session store."
        ),
    },
    "hex": {"type": "string", "description": "Inline bytes as hex."},
    "text": {"type": "string", "description": "Inline bytes as UTF-8 text."},
    "base64": {"type": "string", "description": "Inline bytes as base64."},
    "session_id": _SESSION_PROPERTY,
    "offset": {"type": "integer", "minimum": 0, "description": "Byte offset into the source."},
    "max_bytes": {"type": "integer", "minimum": 1, "description": "Cap on bytes read."},
}

_DATA_SOURCE_CAVEAT = (
    "Give exactly one of path, hex, text or base64. 'path' is the useful one for real work: "
    "it points at a capture the daemon already wrote, so the analysis runs on the bytes as "
    "they came off the wire rather than on a copy that passed through a transcription."
)

_TRUNCATION_CAVEAT = (
    "Long lists in the result are truncated before they reach you, and the reply says so. "
    "The complete capture is on disk as a session artifact; the artifact id and path are in "
    "the result, and the analysis tools read it from there."
)


# ---------------------------------------------------------------------------
# Parameter and result mapping
# ---------------------------------------------------------------------------


def _devices_of_kind(kind: str) -> ResultShaper:
    """Narrow ``device.list`` to one transport.

    The per-transport listing tools exist because a model asking "what CAN
    interfaces are there?" should not have to filter a mixed inventory and
    guess at the transport spelling.
    """

    def shape(result: dict[str, Any]) -> dict[str, Any]:
        devices = [d for d in result.get("devices", []) if d.get("kind") == kind]
        return {
            "kind": kind,
            "count": len(devices),
            "devices": devices,
            "aliases": {
                alias: target
                for alias, target in (result.get("aliases") or {}).items()
                if any(d.get("id") == target for d in devices)
            },
        }

    return shape


#: ``modbus_read`` fronts four actions so the model picks a register space
#: rather than a function code it has to remember.
_MODBUS_READ_ACTIONS = {
    "holding": "modbus.read_holding",
    "input": "modbus.read_input",
    "coils": "modbus.read_coils",
    "discrete": "modbus.read_discrete",
}

#: Word and byte order only mean something for 16-bit registers.  Sending
#: them with a bit read would be refused by the daemon's strict models.
_REGISTER_ONLY_KEYS = ("word_order", "byte_order")


def _modbus_register_space(arguments: dict[str, Any]) -> str:
    space = str(arguments.get("register_type", "holding"))
    if space not in _MODBUS_READ_ACTIONS:
        raise InvalidRequest(
            f"unknown register_type {space!r}",
            details={"known": sorted(_MODBUS_READ_ACTIONS)},
            preserved="nothing was sent to the bus",
        )
    return space


def _modbus_action(arguments: dict[str, Any]) -> str:
    return _MODBUS_READ_ACTIONS[_modbus_register_space(arguments)]


def _modbus_params(arguments: dict[str, Any]) -> dict[str, Any]:
    space = _modbus_register_space(arguments)
    params = {key: value for key, value in _drop_none(arguments).items() if key != "register_type"}
    if space in ("coils", "discrete"):
        for key in _REGISTER_ONLY_KEYS:
            params.pop(key, None)
    return params


def _drop_none(arguments: dict[str, Any]) -> dict[str, Any]:
    """Omit explicit nulls so the daemon's own defaults apply.

    Models routinely fill every optional field, ``null`` included.  No FieldDeck
    parameter treats an explicit null as different from an omission — the
    optional ones already default to ``None``, and the rest would simply refuse
    a null — so dropping them turns a pointless validation error into the call
    the model meant to make.
    """
    return {key: value for key, value in arguments.items() if value is not None}


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

TOOLS: tuple[ToolDef, ...] = (
    ToolDef(
        name="fielddeck_status",
        title="FieldDeck status",
        body=(
            "Overall state of the instrument: firmware version, whether it is running "
            "simulated devices, the safety state (SAFE / ARMED / ESTOP) with any active "
            "grants and output leases, the recording session, the device inventory by "
            "transport, and what is executing right now. Start here."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_NO_ARGS,
        action="system.status",
        touches_hardware=False,
    ),
    ToolDef(
        name="fielddeck_discover",
        title="Discover devices",
        body=(
            "Re-run the passive inventory: USB, serial ports, CAN interfaces, network "
            "interfaces, VISA instruments, debug probes, cameras. Returns every device "
            "with its id, transport, capabilities and permission floor. This is stage 1 "
            "of auto-detect — enumeration only. It does not open a bus, set a bitrate, "
            "or send a byte anywhere."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_NO_ARGS,
        action="system.discover",
        timeout_s=60.0,
    ),
    ToolDef(
        name="permission_status",
        title="Permission status",
        body=(
            "The complete authorization picture: whether an emergency stop is latched "
            "and why, which permission classes are currently armed and for how much "
            "longer, which outputs are held by a lease, and the configured safety "
            "limits. Check this before proposing anything above PASSIVE, and check it "
            "again after a PermissionDenied — it tells you whether the operator has "
            "armed what you asked for."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_NO_ARGS,
        rpc_method="safety.status",
        touches_hardware=False,
    ),
    ToolDef(
        name="session_list",
        title="List sessions",
        body=(
            "Every recording session on this device, newest first, with its id, name, "
            "state and duration. A session is the container for captures, measurements, "
            "marks and notes; almost every other read is scoped to one."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_NO_ARGS,
        action="session.list",
        touches_hardware=False,
    ),
    ToolDef(
        name="session_get",
        title="Get session",
        body=(
            "Metadata, timeline summary and the artifact list for one session: what was "
            "attached, what was captured, where each file lives and what produced it. "
            "Use the artifact paths from here as the 'path' argument to the analysis "
            "tools."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema({"session_id": _SESSION_PROPERTY}),
        action="session.get",
        touches_hardware=False,
    ),
    ToolDef(
        name="session_events",
        title="Query session events",
        body=(
            "Timeline events for a session, filterable by type, device and severity. "
            "Every event carries both a monotonic timestamp, for correlating subsystems "
            "against each other, and a UTC timestamp for human reference. Denied actions "
            "appear here too, so this is where you see what an operator tried and what "
            "the safety layer refused."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema(
            {
                "session_id": _SESSION_PROPERTY,
                "limit": {"type": "integer", "minimum": 1, "maximum": 10000},
                "offset": {"type": "integer", "minimum": 0},
                "types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Event type names, e.g. ['DEVICE_FAULT', 'ACTION_DENIED'].",
                },
                "device_id": {"type": "string"},
                "severity_at_least": {
                    "type": "string",
                    "description": "DEBUG, INFO, WARNING, ERROR or CRITICAL.",
                },
            }
        ),
        action="session.events",
        touches_hardware=False,
        caveats=(_TRUNCATION_CAVEAT,),
    ),
    ToolDef(
        name="session_window",
        title="Correlate around an instant",
        body=(
            "Everything that happened in a window around one instant, across every "
            "subsystem at once: CAN, serial, supply readings, measurements, logic "
            "captures, marks and faults on one timeline. This is the tool for 'what "
            "happened 300 ms before the fault?'. Anchor it either on an explicit "
            "monotonic timestamp or on the first event of a given type."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema(
            {
                "session_id": _SESSION_PROPERTY,
                "center_monotonic_ns": {
                    "type": "integer",
                    "description": "Anchor instant, from an event's monotonic_ns.",
                },
                "around_event_type": {
                    "type": "string",
                    "description": "Anchor on the first event of this type instead.",
                },
                "before_ms": {"type": "number", "minimum": 0, "maximum": 600000},
                "after_ms": {"type": "number", "minimum": 0, "maximum": 600000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20000},
            }
        ),
        action="session.window",
        touches_hardware=False,
        caveats=(
            "Give exactly one anchor: center_monotonic_ns or around_event_type.",
            _TRUNCATION_CAVEAT,
        ),
    ),
    ToolDef(
        name="session_summary",
        title="Summarize session",
        body=(
            "A deterministic summary of one session — counts, durations, devices, "
            "artifacts, faults — computed by the daemon rather than inferred. Use it as "
            "the factual base for a report, and keep your interpretation separate from "
            "it."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema({"session_id": _SESSION_PROPERTY}),
        action="session.summary",
        touches_hardware=False,
    ),
    ToolDef(
        name="can_interfaces",
        title="List CAN interfaces",
        body=(
            "CAN and CAN FD interfaces known to instrumentd, with their ids, bitrates "
            "where configured, capabilities and state. Enumeration only; no interface is "
            "opened and no bitrate is changed."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_NO_ARGS,
        action="device.list",
        shape_result=_devices_of_kind("can"),
    ),
    ToolDef(
        name="can_status",
        title="CAN interface status",
        body=(
            "Configuration and controller state for one CAN interface: bitrate, "
            "listen-only flag, error counters, bus state and frame counters. Read from "
            "the interface's own state; nothing is transmitted."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema({"device": _DEVICE_PROPERTY}, required=("device",)),
        action="can.status",
    ),
    ToolDef(
        name="can_capture",
        title="Capture CAN frames",
        body=(
            "Listen on a CAN interface for a bounded time and record the frames into the "
            "session as an immutable candump-format file. Receive only: the controller "
            "does not acknowledge, request or transmit anything, which is what makes "
            "this safe on a bus whose topology you do not yet know. Returns the frame "
            "count, a sample of frames and the artifact the full capture was written to."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema(
            {
                "device": _DEVICE_PROPERTY,
                "duration_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600},
                "max_frames": {"type": "integer", "minimum": 1, "maximum": 200000},
                "id_filter": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0, "maximum": 536870911},
                    "description": "Arbitration ids to keep. Omit for everything.",
                },
                "label": {"type": "string", "maxLength": 64},
            },
            required=("device",),
        ),
        action="can.capture",
        timeout_s=30.0,
        caveats=(
            "If no session is recording, the frames are not saved to disk and the reply "
            "says so. Ask the operator to start a session first when the capture matters.",
            _TRUNCATION_CAVEAT,
        ),
    ),
    ToolDef(
        name="can_stats",
        title="CAN traffic statistics",
        body=(
            "Per-arbitration-id statistics gathered by listening for a bounded time: "
            "frame counts, rates, mean period and jitter, and the last payload seen for "
            "each id. The fastest way to tell a periodic bus from an event-driven one. "
            "Receive only."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema(
            {
                "device": _DEVICE_PROPERTY,
                "duration_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
            },
            required=("device",),
        ),
        action="can.stats",
        timeout_s=90.0,
    ),
    ToolDef(
        name="can_decode_capture",
        title="Decode a CAN capture",
        body=(
            "Decode a CAN capture that is already on disk against a DBC/KCD/SYM "
            "database, producing signal values per frame. Pure file work: it reads a "
            "stored capture and writes a derived artifact that records which raw capture "
            "it came from. The bus is not touched, so this is equally usable after the "
            "hardware has been unplugged."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema(
            {
                "device": _DEVICE_PROPERTY,
                "dbc": {"type": "string", "description": "Path to a .dbc, .kcd or .sym file."},
                "artifact_id": {
                    "type": "string",
                    "description": "Capture artifact id from session_get.",
                },
                "path": {
                    "type": "string",
                    "description": "Capture file relative to the session directory.",
                },
                "label": {"type": "string", "maxLength": 64},
                "max_frames": {"type": "integer", "minimum": 1, "maximum": 20000000},
            },
            required=("device", "dbc"),
        ),
        action="can.decode",
        timeout_s=120.0,
        caveats=(
            "Give exactly one of artifact_id or path. Some drivers accept only path: if "
            "artifact_id comes back rejected, pass the artifact's relative_path from "
            "session_get instead.",
            "A decode is a hypothesis about the bus that is only as good as the database. "
            "Signals that decode to implausible values usually mean the wrong DBC, not a "
            "faulty device.",
            _TRUNCATION_CAVEAT,
        ),
    ),
    ToolDef(
        name="serial_devices",
        title="List serial devices",
        body=(
            "Serial, RS232 and RS485 adapters known to instrumentd, identified by "
            "vendor, product and serial number rather than by /dev name, so an id stays "
            "valid across a replug. Enumeration only; no port is opened."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_NO_ARGS,
        action="device.list",
        shape_result=_devices_of_kind("serial"),
        caveats=(
            "The electrical class — TTL, RS232 or RS485 — is whatever an operator "
            "recorded, and is never detected. If it is unknown, say so rather than "
            "assuming; they are not interchangeable and guessing damages hardware.",
        ),
    ),
    ToolDef(
        name="serial_capture",
        title="Capture serial bytes",
        body=(
            "Record the incoming byte stream from a serial port for a bounded time, "
            "byte-exact, into the session, together with a sidecar index of arrival "
            "times. The port is opened for receive; nothing is transmitted, no line is "
            "asserted to wake a device, and no framing is assumed. Returns byte counts, "
            "a sample and the artifact holding the full capture."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema(
            {
                "device": _DEVICE_PROPERTY,
                "duration_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 8000000},
                "label": {"type": "string", "maxLength": 64},
            },
            required=("device",),
        ),
        action="serial.capture",
        timeout_s=30.0,
        caveats=(
            "Framing comes from the port's current configuration. A capture full of "
            "high-entropy bytes is usually the wrong baud rate, not an encrypted link.",
            _TRUNCATION_CAVEAT,
        ),
    ),
    ToolDef(
        name="serial_analyze_capture",
        title="Analyze a serial capture",
        body=(
            "Structural analysis of captured bytes: entropy, printable ratio, candidate "
            "delimiters and preambles, frame lengths, repeating fields, counters and "
            "checksum positions. Where a capture has an arrival-time index, repeated "
            "frames are reported with real periods and jitter instead of 'looks "
            "periodic'. Reads stored data only."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema(
            {
                **_DATA_PROPERTIES,
                "use_timestamp_index": {
                    "type": "boolean",
                    "description": "Use the capture's arrival-time sidecar when present.",
                },
            }
        ),
        action="tools.analyze_bytes",
        touches_hardware=False,
        timeout_s=60.0,
        caveats=(_DATA_SOURCE_CAVEAT,),
    ),
    ToolDef(
        name="bench_devices",
        title="List bench instruments",
        body=(
            "VISA, USBTMC and socket-connected bench instruments known to instrumentd — "
            "supplies, meters, scopes, loads, generators — with their roles and "
            "capabilities. Enumeration from the resource layer; no instrument is queried, "
            "so the identity shown is whatever was cached or configured, not a fresh "
            "*IDN?."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_NO_ARGS,
        action="device.list",
        shape_result=_devices_of_kind("visa"),
    ),
    ToolDef(
        name="bench_status",
        title="Bench instrument status",
        body=(
            "Cached state for one bench instrument: identity, bound profile, setpoints "
            "and last readings, with the age of each. Nothing is sent to the instrument, "
            "which is exactly why the values may be stale — for a live reading an "
            "operator must arm QUERY."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema({"device": _DEVICE_PROPERTY}, required=("device",)),
        action="bench.status",
    ),
    ToolDef(
        name="scpi_query",
        title="SCPI query",
        body=(
            "Send one SCPI query to a bench instrument and return its response, e.g. "
            "'*IDN?' or 'MEAS:VOLT:DC?'. This transmits on the instrument bus. It is "
            "meant for queries: a command that changes instrument state belongs to a "
            "CONTROL or POWER action with its own limits and lease, not to this tool."
        ),
        permission=PermissionLevel.QUERY,
        input_schema=_schema(
            {
                "device": _DEVICE_PROPERTY,
                "command": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": "SCPI query, normally ending in '?'.",
                },
            },
            required=("device", "command"),
        ),
        action="scpi.query",
    ),
    ToolDef(
        name="modbus_read",
        title="Modbus read",
        body=(
            "Read holding registers, input registers, coils or discrete inputs from one "
            "Modbus station over RTU or TCP. This transmits a request frame and is "
            "therefore an active query, not passive observation — to watch an existing "
            "RS485 conversation without joining it, capture the serial line instead. "
            "Register reads also return 16- and 32-bit interpretations of the raw words."
        ),
        permission=PermissionLevel.QUERY,
        input_schema=_schema(
            {
                "device": _DEVICE_PROPERTY,
                "register_type": {
                    "type": "string",
                    "enum": ["holding", "input", "coils", "discrete"],
                    "description": "Register space to read. Defaults to holding.",
                },
                "slave": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 247,
                    "description": "Station address. Broadcast (0) is not a read.",
                },
                "address": {"type": "integer", "minimum": 0, "maximum": 65535},
                "count": {"type": "integer", "minimum": 1, "maximum": 125},
                "word_order": {
                    "type": "string",
                    "enum": ["big", "little"],
                    "description": (
                        "Which register holds the high word of a 32-bit value. "
                        "Vendor-specific and undetectable from the data. Registers only."
                    ),
                },
                "byte_order": {
                    "type": "string",
                    "enum": ["big", "little"],
                    "description": "Byte order inside one register. Registers only.",
                },
            },
            required=("device",),
        ),
        select_action=_modbus_action,
        build_params=_modbus_params,
        caveats=(
            "Addresses are raw protocol addresses, not 4xxxx-style documentation "
            "addresses. An off-by-one here reads a different quantity and looks "
            "perfectly plausible while doing it.",
        ),
    ),
    ToolDef(
        name="logic_devices",
        title="List logic analyzers",
        body=(
            "Logic analyzers and oscilloscopes visible to sigrok, with their channels, "
            "sample rates and available protocol decoders. Scans for supported hardware "
            "and registers what it finds with instrumentd. Nothing is driven onto the "
            "probes."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_NO_ARGS,
        action="logic.devices",
        timeout_s=60.0,
    ),
    ToolDef(
        name="firmware_inspect",
        title="Inspect a firmware file",
        body=(
            "Identify a firmware file already on the device: format (ELF, Intel HEX, "
            "raw binary), architecture, sections and entry point, load extent, and "
            "hashes. Offline file analysis — no debug probe is opened, no target is "
            "powered, and nothing is written to the target."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema(
            {
                "path": {
                    "type": "string",
                    "description": (
                        "Path inside the session store or a configured firmware "
                        "directory. Anything outside those roots is refused."
                    ),
                }
            },
            required=("path",),
        ),
        action="firmware.inspect",
        touches_hardware=False,
        timeout_s=90.0,
    ),
    ToolDef(
        name="convert_value",
        title="Convert a value",
        body=(
            "Read one value every way it can plausibly be read: bases, signed and "
            "unsigned widths, endianness, floats, ASCII — or convert a unit, extract a "
            "bitfield, or turn an epoch value into a timestamp. Pure computation inside "
            "the daemon; no device is involved."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema(
            {
                "value": {
                    "type": "string",
                    "description": "The value, e.g. '0xDEADBEEF', '1013.25', '55 AA 04'.",
                },
                "operation": {
                    "type": "string",
                    "enum": ["interpret", "unit", "bitfield", "timestamp"],
                    "description": "Defaults to 'interpret'.",
                },
                "from_unit": {"type": "string"},
                "to_unit": {"type": "string"},
                "bit_offset": {"type": "integer", "minimum": 0, "maximum": 63},
                "bit_count": {"type": "integer", "minimum": 1, "maximum": 64},
                "total_width": {"type": "integer", "minimum": 1, "maximum": 64},
                "epoch_unit": {"type": "string", "enum": ["s", "ms", "us", "ns"]},
            },
            required=("value",),
        ),
        action="tools.convert",
        touches_hardware=False,
    ),
    ToolDef(
        name="calculate_crc",
        title="Calculate or identify a CRC",
        body=(
            "Compute a CRC over bytes with a named model, or — the useful direction when "
            "reverse-engineering — give the observed trailer as 'expected' and get back "
            "every catalogued model that produces it. Each catalogue entry carries its "
            "own check value, so a wrong parameter set cannot pass quietly. Pure "
            "computation."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema(
            {
                **_DATA_PROPERTIES,
                "model": {
                    "type": "string",
                    "description": (
                        "CRC name, e.g. 'crc16-modbus'. Omit to run the whole catalogue."
                    ),
                },
                "expected": {
                    "type": "string",
                    "description": "Observed trailer bytes as hex; reports which models match.",
                },
            }
        ),
        action="tools.crc",
        touches_hardware=False,
        timeout_s=60.0,
        caveats=(_DATA_SOURCE_CAVEAT,),
    ),
    ToolDef(
        name="identify_protocol",
        title="Identify a protocol",
        body=(
            "Evidence-based hypotheses about what a captured stream is: each candidate "
            "comes with supporting and contradicting evidence, a confidence that small "
            "samples cap, and the smallest active test that would settle the question. "
            "Read the evidence, not just the ranking, and report the result as a "
            "hypothesis until a test confirms it. This tool reads stored bytes and never "
            "runs the test it suggests — transmitting is an operator's decision."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema(
            {
                **_DATA_PROPERTIES,
                "use_timestamp_index": {"type": "boolean"},
                "include_framing_report": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12},
            }
        ),
        action="tools.identify_protocol",
        touches_hardware=False,
        timeout_s=60.0,
        caveats=(_DATA_SOURCE_CAVEAT,),
    ),
    ToolDef(
        name="recipe_list",
        title="List recipes",
        body=(
            "Test recipes available on this device, with their names, descriptions, "
            "declared requirements and the highest permission each one would need. A "
            "recipe is the repeatable version of a procedure; prefer improving one to "
            "improvising a sequence of calls."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 200}}),
        action="recipe.list",
        touches_hardware=False,
    ),
    ToolDef(
        name="recipe_validate",
        title="Validate a recipe",
        body=(
            "Parse and compile a recipe — by name, or as inline YAML you drafted — and "
            "report every problem statically: unknown actions, unresolvable devices, "
            "setpoints beyond the deployment's limits, missing safe-state steps. Nothing "
            "is executed. Validate any recipe you write before showing it to an operator."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema(
            {
                "recipe": {"type": "string", "description": "Recipe name or path."},
                "text": {"type": "string", "description": "Inline recipe YAML."},
            }
        ),
        action="recipe.validate",
        touches_hardware=False,
        timeout_s=60.0,
        caveats=("Give exactly one of recipe or text.",),
    ),
    ToolDef(
        name="recipe_dry_run",
        title="Dry-run a recipe",
        body=(
            "Answer 'would this run right now?' without running a step: the compiled "
            "plan, the permission each step resolves to, which of those are armed, and "
            "the first thing that would stop it. A missing grant is reported rather than "
            "raised — telling the operator what to arm is the point. No device is "
            "commanded and no output is energised."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema(
            {
                "recipe": {"type": "string", "description": "Recipe name or path."},
                "text": {"type": "string", "description": "Inline recipe YAML."},
                "deadline_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600},
            }
        ),
        action="recipe.dry_run",
        touches_hardware=False,
        timeout_s=90.0,
        caveats=(
            "Give exactly one of recipe or text.",
            "Running a recipe for real is not available through this server. Once a dry "
            "run is clean, hand the operator the command: `fdctl recipe run <name>`.",
        ),
    ),
    ToolDef(
        name="estop",
        title="Emergency stop",
        body=(
            "Latch the emergency stop. Programmable outputs and electronic loads are "
            "driven to their safe state, running actions are cancelled, recipes stop, "
            "active grants are surrendered, and the event is written to the session log. "
            "Captured data is preserved — nothing is deleted, and the session stays "
            "readable so the fault can be understood afterwards.\n\n"
            "Every client may call this, including you, at any time, and it works while "
            "an emergency stop is already latched. Stopping is never the dangerous "
            "direction: if you believe hardware is being damaged, someone is at risk, or "
            "you have simply lost track of what is energised, call it and explain "
            "afterwards. Clearing it is a human's job and cannot be done from here."
        ),
        permission=PermissionLevel.PASSIVE,
        input_schema=_schema(
            {
                "reason": {
                    "type": "string",
                    "maxLength": 200,
                    "description": (
                        "What you observed, in one line. It goes in the session log and "
                        "is the first thing the operator reads."
                    ),
                }
            },
            required=("reason",),
        ),
        rpc_method="safety.estop",
        state_changing=True,
        timeout_s=30.0,
        permission_note=(
            "Permission: none required, by design. This is the one call that needs no "
            "grant, cannot be refused for lack of one, and is still accepted while an "
            "emergency stop is latched. It does change hardware state — outputs go off — "
            "but only ever in the direction of safety, and it destroys no evidence."
        ),
    ),
)


_BY_NAME: dict[str, ToolDef] = {tool.name: tool for tool in TOOLS}


def tool_by_name(name: str) -> ToolDef:
    """Look up one tool, or raise :class:`InvalidRequest`."""
    tool = _BY_NAME.get(name)
    if tool is None:
        raise InvalidRequest(
            f"unknown tool {name!r}",
            details={"tool": name, "known": sorted(_BY_NAME)},
            preserved="nothing was sent to instrumentd",
        )
    return tool


def tool_definitions() -> tuple[ToolDef, ...]:
    return TOOLS


def tool_list_payload() -> list[dict[str, Any]]:
    """The MCP ``tools/list`` result body.

    Built from the static catalogue and nothing else, so it answers correctly
    even when instrumentd is not running: a model that cannot see the tool
    surface cannot tell the operator what FieldDeck would be able to do once
    the daemon is back.
    """
    return [tool.to_payload() for tool in TOOLS]
