"""Shared contracts: models, errors, time, ids, events, config, logging.

Nothing in this package touches hardware, spawns a subprocess or opens a
socket.  That keeps it importable from tests, the CLI, the HMI and the MCP
server without dragging in a single optional dependency.
"""

from __future__ import annotations

from fielddeck.common.errors import (
    ActionTimeout,
    ConfigurationError,
    DeviceBusy,
    DeviceDisconnected,
    DeviceNotFound,
    ErrorCode,
    EstopActive,
    FieldDeckError,
    PermissionDenied,
    SafetyLimitExceeded,
)
from fielddeck.common.events import Event, EventSeverity, EventType, new_event
from fielddeck.common.models import (
    ActionDescriptor,
    ActionRequest,
    ActionResult,
    ArmGrant,
    ArmScope,
    CaptureArtifact,
    ClientSource,
    ConnectionState,
    DeviceCapability,
    DeviceDescriptor,
    DeviceRole,
    OutputLease,
    PermissionLevel,
    SafetyLimit,
    SafetySnapshot,
    Session,
    SessionMark,
    SessionState,
    TransportKind,
)
from fielddeck.common.timebase import TimeAnchor, Timestamp, monotonic_ns, utc_ns

__all__ = [
    "ActionDescriptor",
    "ActionRequest",
    "ActionResult",
    "ActionTimeout",
    "ArmGrant",
    "ArmScope",
    "CaptureArtifact",
    "ClientSource",
    "ConfigurationError",
    "ConnectionState",
    "DeviceBusy",
    "DeviceCapability",
    "DeviceDescriptor",
    "DeviceDisconnected",
    "DeviceNotFound",
    "DeviceRole",
    "ErrorCode",
    "EstopActive",
    "Event",
    "EventSeverity",
    "EventType",
    "FieldDeckError",
    "OutputLease",
    "PermissionDenied",
    "PermissionLevel",
    "SafetyLimit",
    "SafetyLimitExceeded",
    "SafetySnapshot",
    "Session",
    "SessionMark",
    "SessionState",
    "TimeAnchor",
    "Timestamp",
    "TransportKind",
    "monotonic_ns",
    "new_event",
    "utc_ns",
]
