"""Core domain models.

These types cross every boundary in FieldDeck: RPC, session storage, recipes,
the HMI and the MCP surface.  They are deliberately strict — an instrument
that silently coerces a voltage is an instrument that damages hardware.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from fielddeck.common.timebase import Timestamp

__all__ = [
    "ActionDescriptor",
    "ActionRequest",
    "ActionResult",
    "ArmGrant",
    "ArmScope",
    "CaptureArtifact",
    "ClientSource",
    "ConnectionState",
    "DeviceCapability",
    "DeviceDescriptor",
    "DeviceRole",
    "OutputLease",
    "PermissionLevel",
    "SafetyLimit",
    "SafetySnapshot",
    "Session",
    "SessionMark",
    "SessionState",
    "TransportKind",
]


class StrictModel(BaseModel):
    """Base model: reject unknown fields at every trust boundary."""

    model_config = ConfigDict(extra="forbid", frozen=False, validate_assignment=True)


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


class PermissionLevel(StrEnum):
    """The single permission class an action requires.

    Authorization is **exact-class**, not hierarchical: a ``POWER`` grant does
    not authorize a ``CONTROL`` action and vice versa.  Arming several classes
    at once is one command (``fdctl arm control power``); silently inheriting
    authority is not something an operator can audit at a glance.

    :attr:`rank` exists only for ordering severity in displays and for
    computing "the most dangerous thing this recipe will do".
    """

    PASSIVE = "PASSIVE"
    QUERY = "QUERY"
    CONTROL = "CONTROL"
    POWER = "POWER"
    FLASH = "FLASH"
    DESTRUCTIVE = "DESTRUCTIVE"

    @property
    def rank(self) -> int:
        return _PERMISSION_RANK[self]

    @property
    def requires_grant(self) -> bool:
        """PASSIVE is the boot state and never needs authorization."""
        return self is not PermissionLevel.PASSIVE

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, PermissionLevel):
            return self.rank < other.rank
        return NotImplemented

    def __le__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, PermissionLevel):
            return self.rank <= other.rank
        return NotImplemented

    def __gt__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, PermissionLevel):
            return self.rank > other.rank
        return NotImplemented

    def __ge__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, PermissionLevel):
            return self.rank >= other.rank
        return NotImplemented


_PERMISSION_RANK: dict[PermissionLevel, int] = {
    PermissionLevel.PASSIVE: 0,
    PermissionLevel.QUERY: 1,
    PermissionLevel.CONTROL: 2,
    PermissionLevel.POWER: 3,
    PermissionLevel.FLASH: 4,
    PermissionLevel.DESTRUCTIVE: 5,
}


class ClientSource(StrEnum):
    """Who asked.  Recorded on every event and audit line."""

    HMI = "hmi"
    FDCTL = "fdctl"
    RECIPE = "recipe"
    CLAUDE = "claude"
    SYSTEM = "system"

    @property
    def may_create_grants(self) -> bool:
        """Only a human at the HMI or the CLI can authorize hardware access.

        Recipes and Claude are explicitly excluded: an automated client that
        can widen its own authority is not an authorization system.
        """
        return self in (ClientSource.HMI, ClientSource.FDCTL)


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


class TransportKind(StrEnum):
    SERIAL = "serial"
    CAN = "can"
    MODBUS = "modbus"
    VISA = "visa"
    LOGIC = "logic"
    CAMERA = "camera"
    GPIO = "gpio"
    I2C = "i2c"
    SPI = "spi"
    USB = "usb"
    NET = "net"
    PROBE = "probe"


class DeviceRole(StrEnum):
    """What an instrument is *for*, so recipes can bind by role."""

    PSU = "psu"
    DMM = "dmm"
    SCOPE = "scope"
    LOAD = "load"
    FUNCGEN = "funcgen"
    COUNTER = "counter"
    GENERIC_SCPI = "generic_scpi"
    BUS = "bus"
    ANALYZER = "analyzer"
    PROGRAMMER = "programmer"
    CAMERA = "camera"


class DeviceCapability(StrEnum):
    RX = "rx"
    TX = "tx"
    BAUD_CONFIG = "baud_config"
    BITRATE_CONFIG = "bitrate_config"
    LISTEN_ONLY = "listen_only"
    STREAM = "stream"
    MEASURE = "measure"
    OUTPUT = "output"
    SETPOINT = "setpoint"
    FLASH = "flash"
    ERASE = "erase"
    SNAPSHOT = "snapshot"
    DECODE = "decode"
    SAFE_STATE = "safe_state"


class ConnectionState(StrEnum):
    ABSENT = "ABSENT"
    DISCOVERED = "DISCOVERED"
    CONNECTING = "CONNECTING"
    READY = "READY"
    BUSY = "BUSY"
    FAULT = "FAULT"
    DISCONNECTING = "DISCONNECTING"


class DeviceDescriptor(StrictModel):
    """Everything a client may know about a device without touching it."""

    id: str
    kind: TransportKind
    display_name: str
    path: str | None = None
    vendor: str | None = None
    product: str | None = None
    serial_number: str | None = None
    roles: list[DeviceRole] = Field(default_factory=list)
    capabilities: list[DeviceCapability] = Field(default_factory=list)
    #: The lowest permission any action on this device can require.  A device
    #: whose very presence implies transmission would raise this above PASSIVE.
    permission_floor: PermissionLevel = PermissionLevel.PASSIVE
    state: ConnectionState = ConnectionState.DISCOVERED
    #: False when the id is derived from a non-persistent name such as
    #: ``/dev/ttyUSB0``.  Surfaced to the operator; never silently ignored.
    stable_id: bool = True
    simulated: bool = False
    #: Set when the device profile could not be applied, or the driver is
    #: degraded but still enumerable.
    warning: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def has(self, capability: DeviceCapability) -> bool:
        return capability in self.capabilities


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


class ActionDescriptor(StrictModel):
    """Self-describing metadata for one registered action.

    An action whose ``state_changing`` flag lies is a safety defect.  Never
    hide state-changing behaviour inside something named ``read``, ``status``
    or ``discover``.
    """

    name: str
    description: str
    permission: PermissionLevel
    #: None for daemon-wide actions such as ``system.status``.
    device_id: str | None = None
    state_changing: bool
    cancelable: bool = False
    timeout_s: float = 10.0
    params_schema: dict[str, Any] = Field(default_factory=dict)
    result_schema: dict[str, Any] = Field(default_factory=dict)
    #: What happens to the hardware if this action is interrupted or its
    #: lease expires.  Required reading for anything with an output.
    safe_state_note: str | None = None
    #: Actions permitted to run while ESTOP is latched.  Only ever things
    #: that make hardware *safer* (output off, load off, transmit stop).
    allowed_during_estop: bool = False


class ActionRequest(StrictModel):
    """A client's request to run one action."""

    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    source: ClientSource = ClientSource.FDCTL
    #: Overrides the action's default timeout.  Capped by the daemon.
    timeout_s: float | None = None
    #: Client-supplied correlation id, echoed into events and audit records.
    request_id: str | None = None

    @field_validator("action")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("action name must not be empty")
        return value


class ActionResult(StrictModel):
    action: str
    ok: bool
    result: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    permission: PermissionLevel = PermissionLevel.PASSIVE
    started_monotonic_ns: int = 0
    started_utc_ns: int = 0
    duration_ns: int = 0
    request_id: str | None = None


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


class ArmScope(StrictModel):
    """What a grant covers.  Narrower is better."""

    kind: Literal["all", "device", "action"] = "all"
    device_id: str | None = None
    action: str | None = None

    def matches(self, *, device_id: str | None, action: str) -> bool:
        if self.kind == "all":
            return True
        if self.kind == "device":
            return device_id is not None and device_id == self.device_id
        return action == self.action

    def describe(self) -> str:
        if self.kind == "all":
            return "all devices"
        if self.kind == "device":
            return f"device {self.device_id}"
        return f"action {self.action}"


class ArmGrant(StrictModel):
    """A temporary, revocable authorization for one permission class.

    Grants never survive a daemon restart or a reboot, always carry a TTL,
    and can only be created by a human-facing client.
    """

    grant_id: str
    permission: PermissionLevel
    scope: ArmScope = Field(default_factory=ArmScope)
    created_by: ClientSource
    created_monotonic_ns: int
    created_utc_ns: int
    expires_monotonic_ns: int
    ttl_s: float
    note: str | None = None
    revoked: bool = False
    revoked_reason: str | None = None

    def is_active(self, at_monotonic_ns: int) -> bool:
        return not self.revoked and at_monotonic_ns < self.expires_monotonic_ns

    def remaining_s(self, at_monotonic_ns: int) -> float:
        if self.revoked:
            return 0.0
        return max(0.0, (self.expires_monotonic_ns - at_monotonic_ns) / 1e9)


class OutputLease(StrictModel):
    """A dead-man's handle on a sustained hazardous output.

    If the owning client dies or stops renewing, ``instrumentd`` drives the
    device to its safe state.  A PSU left energised by a crashed UI is exactly
    the failure this prevents.
    """

    lease_id: str
    device_id: str
    action: str
    owner: ClientSource
    owner_connection: int | None = None
    created_monotonic_ns: int
    expires_monotonic_ns: int
    ttl_s: float
    #: What this lease was sustaining, e.g. ``psu.output`` with
    #: ``{"enabled": true}``, and the parameters that would undo it.
    #:
    #: Recorded for the audit trail, **not** executed. When a lease lapses the
    #: daemon calls the driver's own :meth:`~fielddeck.drivers.base.Driver.
    #: safe_state`, which safes the whole device rather than reversing one
    #: action. That is deliberately the blunter instrument: a lapsed lease means
    #: nobody is watching, and at that point "everything off" is a better answer
    #: than "undo precisely the thing I remember doing".
    safe_action: str | None = None
    safe_params: dict[str, Any] = Field(default_factory=dict)
    released: bool = False

    def is_active(self, at_monotonic_ns: int) -> bool:
        return not self.released and at_monotonic_ns < self.expires_monotonic_ns

    def remaining_s(self, at_monotonic_ns: int) -> float:
        if self.released:
            return 0.0
        return max(0.0, (self.expires_monotonic_ns - at_monotonic_ns) / 1e9)


class SafetyLimit(StrictModel):
    """A hard bound on a physical quantity, e.g. ``psu.voltage`` <= 24.5 V."""

    quantity: str
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    note: str | None = None

    def violation(self, value: float) -> str | None:
        """Return a human explanation if ``value`` is out of bounds."""
        if self.maximum is not None and value > self.maximum:
            return (
                f"{self.quantity}={value:g}{self.unit} exceeds maximum {self.maximum:g}{self.unit}"
            )
        if self.minimum is not None and value < self.minimum:
            return f"{self.quantity}={value:g}{self.unit} below minimum {self.minimum:g}{self.unit}"
        return None

    def intersect(self, other: SafetyLimit) -> SafetyLimit:
        """Combine two limits, keeping the stricter bound on each side."""
        if other.quantity != self.quantity:
            raise ValueError(f"cannot intersect {self.quantity} with {other.quantity}")
        minimum = _stricter(self.minimum, other.minimum, keep=max)
        maximum = _stricter(self.maximum, other.maximum, keep=min)
        return SafetyLimit(
            quantity=self.quantity,
            minimum=minimum,
            maximum=maximum,
            unit=self.unit or other.unit,
            note=self.note or other.note,
        )


def _stricter(a: float | None, b: float | None, *, keep: Any) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return float(keep(a, b))


class SafetySnapshot(StrictModel):
    """The complete authorization picture, as shown in the HMI banner."""

    estop_active: bool = False
    estop_reason: str | None = None
    estop_utc_ns: int | None = None
    grants: list[ArmGrant] = Field(default_factory=list)
    leases: list[OutputLease] = Field(default_factory=list)
    #: Highest permission currently granted, for the one-word banner state.
    armed_permissions: list[PermissionLevel] = Field(default_factory=list)

    @property
    def state_word(self) -> str:
        if self.estop_active:
            return "ESTOP"
        if self.armed_permissions:
            return "ARMED"
        return "SAFE"


# ---------------------------------------------------------------------------
# Sessions and artifacts
# ---------------------------------------------------------------------------


class SessionState(StrEnum):
    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    FINALIZING = "FINALIZING"
    CLOSED = "CLOSED"


class SessionMark(StrictModel):
    label: str
    monotonic_ns: int
    utc_ns: int
    source: ClientSource = ClientSource.FDCTL
    note: str | None = None


class CaptureArtifact(StrictModel):
    """A file belonging to a session.

    Raw captures are immutable.  Anything derived records where it came from
    and what produced it, so a decoded CSV can always be traced back to the
    bytes on the wire.
    """

    artifact_id: str
    session_id: str
    relative_path: str
    kind: str
    media_type: str = "application/octet-stream"
    size_bytes: int = 0
    sha256: str | None = None
    created_monotonic_ns: int = 0
    created_utc_ns: int = 0
    device_id: str | None = None
    #: True for bytes as they came off the wire.  Never rewritten.
    raw: bool = True
    #: Provenance for derived artifacts.
    source_artifact_ids: list[str] = Field(default_factory=list)
    producer: str | None = None
    producer_version: str | None = None
    producer_config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Session(StrictModel):
    id: str
    name: str
    state: SessionState = SessionState.ACTIVE
    operator: str | None = None
    started_monotonic_ns: int
    started_utc_ns: int
    ended_monotonic_ns: int | None = None
    ended_utc_ns: int | None = None
    notes: list[str] = Field(default_factory=list)
    marks: list[SessionMark] = Field(default_factory=list)
    devices: list[DeviceDescriptor] = Field(default_factory=list)
    software: dict[str, str] = Field(default_factory=dict)
    simulated: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    def elapsed_s(self, at_monotonic_ns: int | None = None) -> float:
        end = self.ended_monotonic_ns or at_monotonic_ns or Timestamp.now().monotonic_ns
        return max(0.0, (end - self.started_monotonic_ns) / 1e9)
