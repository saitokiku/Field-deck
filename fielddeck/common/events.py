"""The event vocabulary.

Every meaningful state transition emits an event.  Events are the substrate
of the unified timeline: the same records drive the live HMI, the session
log, the audit trail and every "what happened 300 ms before the fault?"
question a client can ask.
"""

from __future__ import annotations

import itertools
from enum import StrEnum
from typing import Any

from pydantic import Field

from fielddeck.common.models import ClientSource, PermissionLevel, StrictModel
from fielddeck.common.timebase import Timestamp, format_utc_ns

__all__ = ["AUDIT_EVENTS", "Event", "EventSeverity", "EventType", "new_event"]


class EventType(StrEnum):
    # Devices
    DEVICE_DISCOVERED = "DEVICE_DISCOVERED"
    DEVICE_LOST = "DEVICE_LOST"
    DEVICE_CONNECTED = "DEVICE_CONNECTED"
    DEVICE_DISCONNECTED = "DEVICE_DISCONNECTED"
    DEVICE_FAULT = "DEVICE_FAULT"

    # Actions
    ACTION_REQUESTED = "ACTION_REQUESTED"
    ACTION_STARTED = "ACTION_STARTED"
    ACTION_COMPLETED = "ACTION_COMPLETED"
    ACTION_FAILED = "ACTION_FAILED"
    ACTION_CANCELLED = "ACTION_CANCELLED"
    ACTION_DENIED = "ACTION_DENIED"

    # Safety
    ARM_GRANTED = "ARM_GRANTED"
    ARM_EXPIRED = "ARM_EXPIRED"
    ARM_REVOKED = "ARM_REVOKED"
    LEASE_ACQUIRED = "LEASE_ACQUIRED"
    LEASE_RENEWED = "LEASE_RENEWED"
    LEASE_RELEASED = "LEASE_RELEASED"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    OUTPUT_ENABLED = "OUTPUT_ENABLED"
    OUTPUT_DISABLED = "OUTPUT_DISABLED"
    LIMIT_REJECTED = "LIMIT_REJECTED"
    SAFE_STATE_APPLIED = "SAFE_STATE_APPLIED"
    ESTOP = "ESTOP"
    ESTOP_CLEARED = "ESTOP_CLEARED"

    # Sessions and capture
    SESSION_STARTED = "SESSION_STARTED"
    SESSION_STOPPED = "SESSION_STOPPED"
    SESSION_MARK = "SESSION_MARK"
    SESSION_NOTE = "SESSION_NOTE"
    CAPTURE_STARTED = "CAPTURE_STARTED"
    CAPTURE_STOPPED = "CAPTURE_STOPPED"
    ARTIFACT_ADDED = "ARTIFACT_ADDED"
    MEASUREMENT = "MEASUREMENT"
    CAPTURE_OVERFLOW = "CAPTURE_OVERFLOW"

    # Recipes
    RECIPE_STARTED = "RECIPE_STARTED"
    RECIPE_STEP_STARTED = "RECIPE_STEP_STARTED"
    RECIPE_STEP_COMPLETED = "RECIPE_STEP_COMPLETED"
    RECIPE_ASSERTION = "RECIPE_ASSERTION"
    RECIPE_FINISHED = "RECIPE_FINISHED"

    # Assistant
    ASSISTANT_OBSERVATION = "ASSISTANT_OBSERVATION"

    # Daemon
    CLOCK_STEPPED = "CLOCK_STEPPED"
    DAEMON_STARTED = "DAEMON_STARTED"
    DAEMON_STOPPING = "DAEMON_STOPPING"
    CLIENT_CONNECTED = "CLIENT_CONNECTED"
    CLIENT_DISCONNECTED = "CLIENT_DISCONNECTED"


class EventSeverity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


#: Events that always belong in the immutable audit trail, even when no
#: session is recording.  ESTOP and authorization changes are never dropped.
AUDIT_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.ESTOP,
        EventType.ESTOP_CLEARED,
        EventType.ARM_GRANTED,
        EventType.ARM_EXPIRED,
        EventType.ARM_REVOKED,
        EventType.ACTION_DENIED,
        EventType.LIMIT_REJECTED,
        EventType.LEASE_EXPIRED,
        EventType.SAFE_STATE_APPLIED,
        EventType.OUTPUT_ENABLED,
        EventType.OUTPUT_DISABLED,
        EventType.DEVICE_FAULT,
        EventType.CAPTURE_OVERFLOW,
        EventType.CLOCK_STEPPED,
    }
)

_SEQ = itertools.count(1)


class Event(StrictModel):
    """One timeline record.

    Both clocks are always present: monotonic for correlation, UTC for
    humans.  Neither is ever rewritten after the event is emitted.
    """

    event_id: str
    seq: int
    type: EventType
    monotonic_ns: int
    utc_ns: int
    source: ClientSource = ClientSource.SYSTEM
    severity: EventSeverity = EventSeverity.INFO
    session_id: str | None = None
    device_id: str | None = None
    action: str | None = None
    permission: PermissionLevel | None = None
    request_id: str | None = None
    message: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_audit(self) -> bool:
        return self.type in AUDIT_EVENTS or self.severity in (
            EventSeverity.ERROR,
            EventSeverity.CRITICAL,
        )

    def utc_iso(self) -> str:
        return format_utc_ns(self.utc_ns)


def new_event(
    type: EventType,
    *,
    source: ClientSource = ClientSource.SYSTEM,
    severity: EventSeverity = EventSeverity.INFO,
    session_id: str | None = None,
    device_id: str | None = None,
    action: str | None = None,
    permission: PermissionLevel | None = None,
    request_id: str | None = None,
    message: str | None = None,
    payload: dict[str, Any] | None = None,
    timestamp: Timestamp | None = None,
) -> Event:
    """Build an event, stamping both clocks at the moment of the call."""
    ts = timestamp or Timestamp.now()
    seq = next(_SEQ)
    return Event(
        event_id=f"ev-{ts.utc_ns:x}-{seq:x}",
        seq=seq,
        type=type,
        monotonic_ns=ts.monotonic_ns,
        utc_ns=ts.utc_ns,
        source=source,
        severity=severity,
        session_id=session_id,
        device_id=device_id,
        action=action,
        permission=permission,
        request_id=request_id,
        message=message,
        payload=dict(payload or {}),
    )
