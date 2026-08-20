"""Shared contracts: models, errors, time, ids, events, config, logging.

Nothing in this package touches hardware, spawns a subprocess or opens a
socket.  That keeps it importable from tests, the CLI, the HMI and the MCP
server without dragging in a single optional dependency.

Re-exports are **lazy** (PEP 562).  ``errors`` is pure standard library, but
eagerly re-exporting the whole package meant importing it also built every
pydantic model — 148 ms rather than 15 ms, paid by anything that only wanted
to catch a FieldDeckError.  The public names are unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
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
    from fielddeck.common.events import (
        Event,
        EventSeverity,
        EventType,
        new_event,
    )
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
    from fielddeck.common.timebase import (
        TimeAnchor,
        Timestamp,
        monotonic_ns,
        utc_ns,
    )

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

_EXPORTS = {
    "ActionDescriptor": "fielddeck.common.models",
    "ActionRequest": "fielddeck.common.models",
    "ActionResult": "fielddeck.common.models",
    "ActionTimeout": "fielddeck.common.errors",
    "ArmGrant": "fielddeck.common.models",
    "ArmScope": "fielddeck.common.models",
    "CaptureArtifact": "fielddeck.common.models",
    "ClientSource": "fielddeck.common.models",
    "ConfigurationError": "fielddeck.common.errors",
    "ConnectionState": "fielddeck.common.models",
    "DeviceBusy": "fielddeck.common.errors",
    "DeviceCapability": "fielddeck.common.models",
    "DeviceDescriptor": "fielddeck.common.models",
    "DeviceDisconnected": "fielddeck.common.errors",
    "DeviceNotFound": "fielddeck.common.errors",
    "DeviceRole": "fielddeck.common.models",
    "ErrorCode": "fielddeck.common.errors",
    "EstopActive": "fielddeck.common.errors",
    "Event": "fielddeck.common.events",
    "EventSeverity": "fielddeck.common.events",
    "EventType": "fielddeck.common.events",
    "FieldDeckError": "fielddeck.common.errors",
    "OutputLease": "fielddeck.common.models",
    "PermissionDenied": "fielddeck.common.errors",
    "PermissionLevel": "fielddeck.common.models",
    "SafetyLimit": "fielddeck.common.models",
    "SafetyLimitExceeded": "fielddeck.common.errors",
    "SafetySnapshot": "fielddeck.common.models",
    "Session": "fielddeck.common.models",
    "SessionMark": "fielddeck.common.models",
    "SessionState": "fielddeck.common.models",
    "TimeAnchor": "fielddeck.common.timebase",
    "Timestamp": "fielddeck.common.timebase",
    "TransportKind": "fielddeck.common.models",
    "monotonic_ns": "fielddeck.common.timebase",
    "new_event": "fielddeck.common.events",
    "utc_ns": "fielddeck.common.timebase",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_name), name)


def __dir__() -> list[str]:
    return sorted(__all__)
