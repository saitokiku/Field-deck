"""The device driver contract.

Every driver — a real SocketCAN interface, a USB serial adapter, a simulated
PSU — exposes the same shape.  Clients never learn which is which except
through :attr:`DeviceDescriptor.simulated`, which is exactly what makes
simulation mode a real test of the production code path rather than a
parallel fake.

Actions are declared with the :func:`action` decorator so their metadata sits
next to the implementation.  An action that changes hardware state while
claiming ``state_changing=False`` is a safety defect, not a style problem.
"""

from __future__ import annotations

import asyncio
import inspect
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from pydantic import BaseModel

from fielddeck.common.errors import UnsupportedCapability
from fielddeck.common.events import Event
from fielddeck.common.models import (
    ActionDescriptor,
    ClientSource,
    ConnectionState,
    DeviceCapability,
    DeviceDescriptor,
    PermissionLevel,
    StrictModel,
    TransportKind,
)
from fielddeck.safety.limits import DerivedLimitCheck, LimitCheck

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fielddeck.capture.recorder import SessionRecorder
    from fielddeck.daemon.registry import DeviceRegistry
    from fielddeck.safety.manager import SafetyManager

__all__ = [
    "ActionContext",
    "ActionSpec",
    "DeviceParams",
    "Driver",
    "NoParams",
    "action",
    "collect_actions",
]


class NoParams(StrictModel):
    """For actions that take nothing."""


class DeviceParams(StrictModel):
    """Base for driver actions.

    ``device`` accepts a device id, a configured alias, or ``role:psu``.
    Resolution happens in the registry, never in the driver.
    """

    device: str


# ---------------------------------------------------------------------------
# Execution context
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ActionContext:
    """Services an action handler is allowed to use.

    Handed to the handler by the dispatcher.  Note what is *absent*: there is
    no way to reach back into the safety manager and widen authorization.
    """

    source: ClientSource
    emit: Callable[[Event], Any]
    safety: SafetyManager
    registry: DeviceRegistry
    request_id: str | None = None
    session_id: str | None = None
    recorder: SessionRecorder | None = None
    deadline_monotonic_ns: int | None = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    #: The permission the dispatcher actually authorized for this call.
    granted_permission: PermissionLevel = PermissionLevel.PASSIVE

    def remaining_s(self) -> float | None:
        if self.deadline_monotonic_ns is None:
            return None
        from fielddeck.common.timebase import monotonic_ns

        return max(0.0, (self.deadline_monotonic_ns - monotonic_ns()) / 1e9)

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def raise_if_cancelled(self) -> None:
        from fielddeck.common.errors import ActionCancelled

        if self.cancel.is_set():
            raise ActionCancelled("action cancelled by client")


Handler = Callable[..., Awaitable[Any]]


@dataclass(slots=True)
class ActionSpec:
    """One registered action: metadata plus the code that performs it."""

    name: str
    description: str
    #: The *declared maximum* permission.  Clients see this so they can plan
    #: authorization for the worst case.
    permission: PermissionLevel
    params_model: type[BaseModel]
    handler: Handler
    state_changing: bool
    cancelable: bool = False
    timeout_s: float = 10.0
    limit_checks: tuple[LimitCheck, ...] = ()
    #: Limits on quantities computed from several parameters, e.g. V x I.
    derived_limit_checks: tuple[DerivedLimitCheck, ...] = ()
    #: True only for actions that move hardware *toward* safety.
    allowed_during_estop: bool = False
    safe_state_note: str | None = None
    #: Narrows the permission for a specific call.  ``psu.output`` is POWER
    #: when enabling and PASSIVE when disabling — turning an output off must
    #: never be blocked by a lapsed grant or a latched ESTOP.
    permission_resolver: Callable[[BaseModel], PermissionLevel] | None = None
    #: Sustained outputs take a lease so a dead client cannot leave them on.
    requires_lease: bool = False
    device_id: str | None = None

    @property
    def is_capture(self) -> bool:
        """Whether this action brackets a recording on the timeline.

        Any action named ``<something>.capture`` counts. The convention is
        load-bearing on purpose: it means every subsystem's capture action
        shows its start and end on the unified timeline without each driver
        having to remember to say so.
        """
        return self.name.endswith(".capture")

    def effective_permission(self, params: BaseModel) -> PermissionLevel:
        if self.permission_resolver is None:
            return self.permission
        resolved = self.permission_resolver(params)
        if resolved.rank > self.permission.rank:
            # A resolver may only narrow.  Widening past the declared maximum
            # would mean clients were shown a permission they cannot trust.
            raise RuntimeError(
                f"action {self.name} resolver tried to widen {self.permission} to {resolved}"
            )
        return resolved

    def describe(self, device_id: str | None = None) -> ActionDescriptor:
        return ActionDescriptor(
            name=self.name,
            description=self.description,
            permission=self.permission,
            device_id=device_id or self.device_id,
            state_changing=self.state_changing,
            cancelable=self.cancelable,
            timeout_s=self.timeout_s,
            params_schema=self.params_model.model_json_schema(),
            safe_state_note=self.safe_state_note,
            allowed_during_estop=self.allowed_during_estop,
        )


@dataclass(frozen=True, slots=True)
class _ActionMeta:
    name: str
    description: str
    permission: PermissionLevel
    params_model: type[BaseModel]
    state_changing: bool
    cancelable: bool
    timeout_s: float
    limit_checks: tuple[LimitCheck, ...]
    derived_limit_checks: tuple[DerivedLimitCheck, ...]
    allowed_during_estop: bool
    safe_state_note: str | None
    permission_resolver: Callable[[BaseModel], PermissionLevel] | None
    requires_lease: bool


F = TypeVar("F", bound=Handler)


def action(
    name: str,
    *,
    permission: PermissionLevel,
    params: type[BaseModel] = NoParams,
    state_changing: bool,
    description: str = "",
    cancelable: bool = False,
    timeout_s: float = 10.0,
    limit_checks: Sequence[LimitCheck] = (),
    derived_limit_checks: Sequence[DerivedLimitCheck] = (),
    allowed_during_estop: bool = False,
    safe_state_note: str | None = None,
    permission_resolver: Callable[[Any], PermissionLevel] | None = None,
    requires_lease: bool = False,
) -> Callable[[F], F]:
    """Declare a driver method as a FieldDeck action.

    ``state_changing`` is mandatory and has no default on purpose: every
    author must answer the question "does this touch the DUT?".
    """

    def decorate(func: F) -> F:
        func._fielddeck_action = _ActionMeta(  # type: ignore[attr-defined]
            name=name,
            description=description or (inspect.getdoc(func) or "").split("\n")[0],
            permission=permission,
            params_model=params,
            state_changing=state_changing,
            cancelable=cancelable,
            timeout_s=timeout_s,
            limit_checks=tuple(limit_checks),
            derived_limit_checks=tuple(derived_limit_checks),
            allowed_during_estop=allowed_during_estop,
            safe_state_note=safe_state_note,
            permission_resolver=permission_resolver,
            requires_lease=requires_lease,
        )
        return func

    return decorate


def collect_actions(obj: object, *, device_id: str | None = None) -> dict[str, ActionSpec]:
    """Gather every ``@action``-decorated method bound to ``obj``."""
    specs: dict[str, ActionSpec] = {}
    for _attr, member in inspect.getmembers(obj, callable):
        meta: _ActionMeta | None = getattr(member, "_fielddeck_action", None)
        if meta is None:
            continue
        handler = cast(Handler, member)
        if meta.name in specs:
            raise RuntimeError(f"duplicate action {meta.name} on {type(obj).__name__}")
        specs[meta.name] = ActionSpec(
            name=meta.name,
            description=meta.description,
            permission=meta.permission,
            params_model=meta.params_model,
            handler=handler,
            state_changing=meta.state_changing,
            cancelable=meta.cancelable,
            timeout_s=meta.timeout_s,
            limit_checks=meta.limit_checks,
            derived_limit_checks=meta.derived_limit_checks,
            allowed_during_estop=meta.allowed_during_estop,
            safe_state_note=meta.safe_state_note,
            permission_resolver=meta.permission_resolver,
            requires_lease=meta.requires_lease,
            device_id=device_id,
        )
    return specs


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class Driver(ABC):
    """Base class for everything that owns a piece of hardware.

    Drivers own their cleanup, define their timeouts, survive device removal,
    map low-level errors onto FieldDeck errors, and — if they have outputs —
    implement :meth:`safe_state`.
    """

    kind: ClassVar[TransportKind]

    def __init__(self, descriptor: DeviceDescriptor) -> None:
        self._descriptor = descriptor
        self._actions: dict[str, ActionSpec] | None = None
        #: Serialises mutually exclusive control of this device.  Passive
        #: subscribers do not take it.
        self.lock = asyncio.Lock()
        self._busy_with: str | None = None

    # -- identity ----------------------------------------------------------

    @property
    def device_id(self) -> str:
        return self._descriptor.id

    @property
    def descriptor(self) -> DeviceDescriptor:
        return self._descriptor

    def describe(self) -> DeviceDescriptor:
        return self._descriptor

    def capabilities(self) -> list[DeviceCapability]:
        return list(self._descriptor.capabilities)

    def supports(self, capability: DeviceCapability) -> bool:
        return capability in self._descriptor.capabilities

    def require(self, capability: DeviceCapability) -> None:
        if not self.supports(capability):
            raise UnsupportedCapability(
                f"{self.device_id} does not support {capability}",
                details={"device_id": self.device_id, "capability": str(capability)},
            )

    # -- lifecycle ---------------------------------------------------------

    async def probe(self) -> bool:
        """Cheap, passive check that the device is still there."""
        return self._descriptor.state is not ConnectionState.ABSENT

    async def connect(self) -> None:
        self._set_state(ConnectionState.READY)

    async def disconnect(self) -> None:
        self._set_state(ConnectionState.DISCOVERED)

    @abstractmethod
    async def status(self) -> dict[str, Any]:
        """Current device state.  Must not transmit to a DUT."""

    async def safe_state(self) -> dict[str, Any]:
        """Drive the device to its safest condition.

        The default is a no-op for devices with no outputs.  Anything that can
        energise, drive or transmit **must** override this.
        """
        return {"device": self.device_id, "applied": False, "reason": "no outputs"}

    # -- actions -----------------------------------------------------------

    def actions(self) -> dict[str, ActionSpec]:
        if self._actions is None:
            self._actions = collect_actions(self, device_id=self.device_id)
        return self._actions

    def action_descriptors(self) -> list[ActionDescriptor]:
        return [spec.describe(self.device_id) for spec in self.actions().values()]

    # -- state helpers -----------------------------------------------------

    def _set_state(self, state: ConnectionState) -> None:
        self._descriptor.state = state

    @property
    def busy_with(self) -> str | None:
        return self._busy_with

    def _mark_busy(self, what: str | None) -> None:
        self._busy_with = what

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.device_id} {self._descriptor.state}>"
