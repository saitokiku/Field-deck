"""Everything the panel knows, and the only place it is allowed to learn it.

Textual callbacks in this package do three things: read a snapshot from
:class:`UiState`, draw it, and hand an operator gesture back to a coroutine in
here.  No screen opens a socket, no widget decides what is permitted, and
nothing under ``fielddeck.ui`` imports a driver.  ``instrumentd`` decides; the
panel reports what it decided.

Four things in this module are worth knowing at 2am.

**A missing daemon is a normal state, not a crash.**  The panel is the thing an
engineer stares at while restarting the service that went down, so every call
here returns an :class:`Outcome` instead of raising, the connection is retried
forever with a bounded backoff, and the chrome says plainly that instrumentd is
unreachable rather than freezing on stale numbers.

**Polling is deliberately uneven.**  ``safety.status`` is a plain RPC method: it
never enters the dispatcher, so asking twice a second costs nothing.
``system.status`` is an *action*, and every action publishes three timeline
events, so polling it at the same rate would bury a real fault under thousands
of HMI heartbeats.  It runs every few seconds, and immediately when an event
tells us it changed.  The same reasoning is why the live bus screens sample a
window rather than streaming every frame through the UI.

**Countdowns are computed locally from the daemon's own deadline.**  Grants
carry ``expires_monotonic_ns``, and CLOCK_MONOTONIC is system-wide on Linux, so
the panel and the daemon are reading the same clock and the countdown is exact
between polls instead of interpolated.  The socket is a Unix socket, so "same
machine" is guaranteed by construction.

**An enabled output is held by a lease belonging to this connection.**  If the
panel exits, the rail drops — that is the point.  While it is up,
:class:`UiState` renews the lease three times per TTL, so one missed renewal
costs nothing and a dead UI still costs the output.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fielddeck.common.errors import FieldDeckError, TransportError
from fielddeck.common.events import Event, EventSeverity, EventType
from fielddeck.common.models import (
    ArmGrant,
    ArmScope,
    ClientSource,
    DeviceDescriptor,
    DeviceRole,
    OutputLease,
    PermissionLevel,
    TransportKind,
)
from fielddeck.common.timebase import monotonic_ns
from fielddeck.daemon.client import InstrumentClient

__all__ = [
    "FaultView",
    "LinkView",
    "Outcome",
    "SafetyView",
    "SessionView",
    "SystemView",
    "UiState",
    "parse_payload",
]

#: ``safety.status`` is an RPC method, not an action, so it leaves no trace on
#: the timeline.  Twice a second keeps the arm countdown honest.
SAFETY_POLL_S = 0.5

#: ``system.status`` *is* an action and costs three timeline events per call.
#: Events drive the panel between these; this is only the safety net.
STATUS_POLL_S = 5.0

RECONNECT_MIN_S = 0.5
RECONNECT_MAX_S = 5.0

#: Recent events kept for the session screen and the fault indicator.  Bounded:
#: the timeline on disk is the record, this is just what fits on a panel.
EVENT_HISTORY = 200

#: Renew an output lease three times inside its TTL, matching ``fdctl``.  Once
#: would make a single scheduling hiccup fatal to a rail that is meant to stay
#: up; three never lengthens the dead-man interval.
LEASE_RENEW_DIVISOR = 3.0


# ---------------------------------------------------------------------------
# Plain data the screens render
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Outcome:
    """The result of one operator gesture, in a form a panel can show.

    Never an exception: a refusal is information, and the screen that asked for
    it is the screen that must display it.
    """

    ok: bool
    action: str
    message: str = ""
    code: str | None = None
    preserved: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def refused(self) -> bool:
        """Denied by the safety model rather than broken."""
        return self.code in {"PermissionDenied", "EstopActive", "SafetyLimitExceeded"}

    def summary(self) -> str:
        if self.ok:
            return f"{self.action}: {self.message}" if self.message else f"{self.action} ok"
        head = "refused" if self.refused else "failed"
        text = f"{self.action} {head}: {self.message or self.code or 'unknown error'}"
        if self.preserved:
            text = f"{text} [kept: {self.preserved}]"
        return text


@dataclass(frozen=True, slots=True)
class LinkView:
    """Whether the panel can currently reach ``instrumentd``."""

    connected: bool
    socket: str
    detail: str = ""
    attempts: int = 0
    server: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SafetyView:
    """The authorization picture, as the daemon last reported it.

    The ``armed`` helpers exist so a screen can *tell the operator* what is
    missing before they press something.  They never gate the press: the panel
    sends the action anyway and shows whatever the daemon answers, because two
    implementations of the permission model would mean one of them is wrong.
    """

    state: str = "?"
    estop_active: bool = False
    estop_reason: str | None = None
    grants: tuple[ArmGrant, ...] = ()
    leases: tuple[OutputLease, ...] = ()
    stale: bool = True

    def armed(
        self,
        permission: PermissionLevel,
        *,
        device_id: str | None = None,
        action: str | None = None,
    ) -> bool:
        now = monotonic_ns()
        return any(
            grant.permission is permission
            and grant.is_active(now)
            and grant.scope.matches(device_id=device_id, action=action or "")
            for grant in self.grants
        )

    def remaining_s(self, permission: PermissionLevel) -> float:
        """Seconds left on the longest-lived grant of one class."""
        now = monotonic_ns()
        return max(
            (grant.remaining_s(now) for grant in self.grants if grant.permission is permission),
            default=0.0,
        )

    def active_grants(self) -> tuple[ArmGrant, ...]:
        now = monotonic_ns()
        return tuple(grant for grant in self.grants if grant.is_active(now))

    def armed_classes(self) -> tuple[PermissionLevel, ...]:
        seen: list[PermissionLevel] = []
        for grant in self.active_grants():
            if grant.permission not in seen:
                seen.append(grant.permission)
        return tuple(sorted(seen, key=lambda level: level.rank))


@dataclass(frozen=True, slots=True)
class SessionView:
    id: str
    name: str
    elapsed_s: float = 0.0
    recording: bool = True


@dataclass(frozen=True, slots=True)
class SystemView:
    version: str = "?"
    simulated: bool = False
    uptime_s: float = 0.0
    utc: str = ""
    device_count: int = 0
    running_actions: int = 0
    sessions_dir: str = ""
    compression: str = ""
    free_note: str = ""


@dataclass(frozen=True, slots=True)
class FaultView:
    """The most recent thing that went wrong, until an operator clears it."""

    message: str
    device_id: str | None
    utc_ns: int
    monotonic_ns: int
    severity: str
    type: str

    def age_s(self) -> float:
        return max(0.0, (monotonic_ns() - self.monotonic_ns) / 1e9)


def parse_payload(text: str, *, as_hex: bool) -> tuple[bytes | None, str]:
    """Turn what the operator typed into the bytes that will go on the wire.

    Returns ``(None, reason)`` rather than guessing.  ``55 AA`` is two bytes in
    hex and five characters as text, and a panel that quietly picked one of
    those readings would eventually pick the wrong one while somebody watched a
    motor instead of the screen.
    """
    stripped = text.strip()
    if not stripped:
        return None, "nothing to send"
    if not as_hex:
        return stripped.encode("utf-8"), ""
    cleaned = stripped.replace(" ", "").replace(",", "").replace("0x", "").replace("_", "")
    if len(cleaned) % 2:
        return None, f"{len(cleaned)} hex digits is not a whole number of bytes"
    try:
        return bytes.fromhex(cleaned), ""
    except ValueError:
        return None, f"{stripped!r} is not hex; switch the view to ASCII to send it as text"


# ---------------------------------------------------------------------------
# The state object
# ---------------------------------------------------------------------------


class UiState:
    """Owns the client, the polling, and every piece of logic the panel needs.

    Screens read the public attributes and call the coroutines.  Everything
    mutating happens on the Textual event loop, so no locking is needed; what
    is needed is that nothing here ever raises into a Textual callback.
    """

    def __init__(
        self,
        *,
        socket_path: Path | str | None = None,
        source: ClientSource = ClientSource.HMI,
        simulation_requested: bool = False,
        safety_poll_s: float = SAFETY_POLL_S,
        status_poll_s: float = STATUS_POLL_S,
    ) -> None:
        self._socket_path = Path(socket_path) if socket_path else None
        self._source = source
        self._safety_poll_s = safety_poll_s
        self._status_poll_s = status_poll_s
        self.simulation_requested = simulation_requested

        socket_text = str(self._socket_path) if self._socket_path else "(default)"
        self.link = LinkView(connected=False, socket=socket_text, detail="starting")
        self.safety = SafetyView()
        self.system: SystemView | None = None
        self.session: SessionView | None = None
        self.devices: tuple[DeviceDescriptor, ...] = ()
        self.aliases: dict[str, str] = {}
        self.limits: dict[str, Any] = {}
        self.max_arm_ttl_s: dict[str, float] = {}
        self.denied_permissions: tuple[str, ...] = ()
        self.fault: FaultView | None = None
        self.last_outcome: Outcome | None = None
        self.events: deque[Event] = deque(maxlen=EVENT_HISTORY)

        #: Bumped on every change worth repainting.  Widgets compare it so a
        #: 10 Hz refresh timer costs nothing while the bench is quiet.
        self.revision = 0

        self._client: InstrumentClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._lease_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        self._selected: dict[TransportKind, str] = {}
        self._want_devices = True
        self._want_status = True
        self._held_lease: tuple[str, float] | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Begin connecting.  Returns immediately; the panel draws regardless."""
        if self._task is None:
            self._task = asyncio.create_task(self._supervise(), name="fielddeck-ui-state")

    async def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        await self._cancel(self._lease_task)
        self._lease_task = None
        await self._cancel(self._task)
        self._task = None
        client, self._client = self._client, None
        if client is not None:
            # Closing a socket we are abandoning anyway: nothing it can raise
            # is worth failing shutdown over.
            with contextlib.suppress(Exception):
                await client.close()

    @staticmethod
    async def _cancel(task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # -- selection ---------------------------------------------------------

    def select(self, device_id: str) -> None:
        """Remember which device a bus screen should show."""
        for device in self.devices:
            if device.id == device_id:
                self._selected[device.kind] = device_id
                self._touch()
                return

    def device_for(
        self, kind: TransportKind | None = None, *, role: DeviceRole | None = None
    ) -> DeviceDescriptor | None:
        """The device a screen should act on: the operator's pick, else the first."""
        candidates = [
            device
            for device in self.devices
            if (kind is None or device.kind is kind) and (role is None or role in device.roles)
        ]
        if not candidates:
            return None
        if kind is not None:
            chosen = self._selected.get(kind)
            for device in candidates:
                if device.id == chosen:
                    return device
        return candidates[0]

    def device_by_id(self, device_id: str) -> DeviceDescriptor | None:
        return next((device for device in self.devices if device.id == device_id), None)

    def devices_of(self, *kinds: TransportKind) -> tuple[DeviceDescriptor, ...]:
        return tuple(device for device in self.devices if device.kind in kinds)

    # -- operator actions --------------------------------------------------

    async def run(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
        remember: bool = True,
    ) -> Outcome:
        """Execute one action.  Never raises; a refusal comes back as data."""
        client = self._client
        if client is None:
            return self._record(
                Outcome(
                    ok=False,
                    action=action,
                    message=self.link.detail or "instrumentd is unreachable",
                    code="TransportError",
                    preserved="nothing was sent",
                ),
                remember,
            )
        try:
            result = await client.execute(action, params or {}, timeout_s=timeout_s)
        except FieldDeckError as exc:
            self._note_error(exc)
            return self._record(
                Outcome(
                    ok=False,
                    action=action,
                    message=exc.message,
                    code=str(exc.code),
                    preserved=exc.preserved,
                    data=exc.details,
                ),
                remember,
            )
        return self._record(Outcome(ok=True, action=action, data=result.result), remember)

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, remember: bool = True
    ) -> Outcome:
        """Invoke a daemon RPC method that is deliberately not an action.

        Only the safety surface lives here: arming is not itself a hardware
        operation, and routing it through the dispatcher would let a grant
        authorize its own creation.
        """
        client = self._client
        if client is None:
            return self._record(
                Outcome(
                    ok=False,
                    action=method,
                    message=self.link.detail or "instrumentd is unreachable",
                    code="TransportError",
                    preserved="nothing was sent",
                ),
                remember,
            )
        try:
            payload = await client.call(method, params or {})
        except FieldDeckError as exc:
            self._note_error(exc)
            return self._record(
                Outcome(
                    ok=False,
                    action=method,
                    message=exc.message,
                    code=str(exc.code),
                    preserved=exc.preserved,
                    data=exc.details,
                ),
                remember,
            )
        data = payload if isinstance(payload, dict) else {"result": payload}
        return self._record(Outcome(ok=True, action=method, data=data), remember)

    async def arm(
        self,
        permission: PermissionLevel,
        *,
        ttl_s: float,
        device_id: str | None = None,
        note: str | None = None,
    ) -> Outcome:
        """Ask for one permission class for a bounded time.

        The scope is narrowed to a device whenever the screen knows which one
        it means, because "all devices" is the grant an operator regrets.
        """
        if not permission.requires_grant:
            return self._record(
                Outcome(
                    ok=False,
                    action="safety.arm",
                    message="PASSIVE needs no authorization; there is nothing to arm",
                    code="InvalidRequest",
                ),
                True,
            )
        scope = ArmScope(kind="device", device_id=device_id) if device_id else ArmScope(kind="all")
        outcome = await self.call(
            "safety.arm",
            {
                "permission": str(permission),
                "ttl_s": ttl_s,
                "scope": scope.model_dump(mode="json"),
                "note": note or "armed at the panel",
            },
        )
        if outcome.ok:
            grant = ArmGrant.model_validate(outcome.data["grant"])
            clamped = " (clamped by policy)" if grant.ttl_s < ttl_s - 0.01 else ""
            outcome = Outcome(
                ok=True,
                action="safety.arm",
                message=f"{grant.permission} for {grant.ttl_s:g}s{clamped}",
                data=outcome.data,
            )
            self._record(outcome, True)
        await self.refresh_safety()
        return outcome

    async def disarm(self, grant_id: str | None = None) -> Outcome:
        outcome = await self.call("safety.disarm", {"grant_id": grant_id} if grant_id else {})
        if outcome.ok:
            revoked = outcome.data.get("revoked") or []
            outcome = Outcome(
                ok=True,
                action="safety.disarm",
                message=f"{len(revoked)} grant(s) revoked" if revoked else "nothing was armed",
                data=outcome.data,
            )
            self._record(outcome, True)
        await self.refresh_safety()
        return outcome

    async def estop(self, reason: str = "operator pressed ESTOP at the panel") -> Outcome:
        """Stop everything.  Never gated, never confirmed: stopping is safe."""
        await self._drop_lease()
        outcome = await self.call("safety.estop", {"reason": reason})
        if outcome.ok:
            surrendered = outcome.data.get("surrendered_leases") or []
            outcome = Outcome(
                ok=True,
                action="safety.estop",
                message=f"engaged; {len(surrendered)} lease(s) surrendered, evidence kept",
                data=outcome.data,
            )
            self._record(outcome, True)
        await self.refresh_safety()
        return outcome

    async def clear_estop(self) -> Outcome:
        outcome = await self.call("safety.estop_clear", {})
        if outcome.ok:
            outcome = Outcome(
                ok=True,
                action="safety.estop_clear",
                message="acknowledged; nothing was re-armed and nothing was re-energised",
                data=outcome.data,
            )
            self._record(outcome, True)
        await self.refresh_safety()
        return outcome

    async def discover(self) -> Outcome:
        outcome = await self.run("system.discover")
        if outcome.ok:
            added = outcome.data.get("added") or []
            removed = outcome.data.get("removed") or []
            outcome = Outcome(
                ok=True,
                action="system.discover",
                message=f"+{len(added)} / -{len(removed)} devices",
                data=outcome.data,
            )
            self._record(outcome, True)
        await self.refresh_devices()
        return outcome

    async def start_session(self, name: str) -> Outcome:
        outcome = await self.run("session.start", {"name": name})
        await self.refresh_status()
        return outcome

    async def stop_session(self) -> Outcome:
        outcome = await self.run("session.stop")
        await self.refresh_status()
        return outcome

    async def toggle_recording(self, *, name: str) -> Outcome:
        """The REC key: one gesture, whichever direction it means right now."""
        if self.session is not None:
            return await self.stop_session()
        return await self.start_session(name)

    async def mark(self, label: str, note: str | None = None) -> Outcome:
        return await self.run("session.mark", {"label": label, "note": note})

    async def note(self, text: str) -> Outcome:
        return await self.run("session.note", {"text": text})

    async def send_serial(
        self,
        device_id: str,
        payload: str,
        *,
        as_hex: bool,
        append_newline: bool = False,
    ) -> Outcome:
        """Transmit to a DUT.  Malformed input is refused here, before the wire.

        A rejected payload never reaches ``instrumentd``: the daemon would
        refuse it too, but a round trip that ends in "not hex" reads to an
        operator like the port failed rather than the typing did.
        """
        data, problem = parse_payload(payload, as_hex=as_hex)
        if data is None:
            return self._record(
                Outcome(
                    ok=False,
                    action="serial.send",
                    message=problem,
                    code="InvalidRequest",
                    preserved="nothing was transmitted",
                ),
                True,
            )
        params: dict[str, Any] = {"device": device_id, "append_newline": append_newline}
        if as_hex:
            params["hex"] = data.hex()
        else:
            params["text"] = data.decode("utf-8", errors="replace")
        return await self.run("serial.send", params)

    # -- power output ------------------------------------------------------

    async def set_output(
        self, device_id: str, *, enabled: bool, lease_ttl_s: float = 30.0
    ) -> Outcome:
        """Enable or disable a supply output, keeping the dead-man alive.

        Enabling needs POWER and takes a lease on this connection; disabling
        resolves to PASSIVE in the driver and is allowed even during ESTOP.
        Either way the lease keeper is stopped first, so a failed enable can
        never leave a renewal loop running against a rail that is off.
        """
        await self._drop_lease()
        outcome = await self.run(
            "psu.output", {"device": device_id, "enabled": enabled, "lease_ttl_s": lease_ttl_s}
        )
        lease = outcome.data.get("lease") if outcome.ok else None
        if enabled and isinstance(lease, dict) and lease.get("lease_id"):
            ttl = float(lease.get("expires_in_s") or lease_ttl_s)
            self._held_lease = (str(lease["lease_id"]), ttl)
            self._lease_task = asyncio.create_task(self._keep_lease(), name="fielddeck-ui-lease")
        return outcome

    async def _keep_lease(self) -> None:
        """Renew the held output lease until the output goes away."""
        while self._held_lease is not None and not self._stopping.is_set():
            lease_id, ttl = self._held_lease
            await self._sleep(max(1.0, ttl / LEASE_RENEW_DIVISOR))
            if self._held_lease is None or self._stopping.is_set():
                return
            outcome = await self.call("safety.lease_renew", {"lease_id": lease_id}, remember=False)
            if not outcome.ok:
                # The daemon has already driven the device to its safe state by
                # the time a renewal is refused.  Say so rather than leaving a
                # panel that still claims the rail is up.
                self._held_lease = None
                self._record(
                    Outcome(
                        ok=False,
                        action="safety.lease_renew",
                        message=f"output lease lapsed: {outcome.message}",
                        code=outcome.code,
                        preserved="the output was driven to its safe state",
                    ),
                    True,
                )
                return

    async def _drop_lease(self) -> None:
        self._held_lease = None
        task, self._lease_task = self._lease_task, None
        await self._cancel(task)

    @property
    def holds_output_lease(self) -> bool:
        return self._held_lease is not None

    # -- explicit refreshes ------------------------------------------------

    async def refresh_safety(self) -> None:
        client = self._client
        if client is None:
            return
        with contextlib.suppress(FieldDeckError):
            self._apply_safety(await client.call("safety.status"))

    async def refresh_status(self) -> None:
        self._want_status = True
        self._wake.set()

    async def refresh_devices(self) -> None:
        self._want_devices = True
        self._wake.set()

    def clear_fault(self) -> None:
        """Operator acknowledgement.  The event stays on the timeline."""
        self.fault = None
        self._touch()

    def recent_events(
        self, limit: int = 20, types: Iterable[EventType] | None = None
    ) -> list[Event]:
        wanted = set(types) if types else None
        chosen = [event for event in self.events if wanted is None or event.type in wanted]
        return chosen[-limit:][::-1]

    # -- background --------------------------------------------------------

    async def _supervise(self) -> None:
        """Connect, serve, and reconnect forever.  Never lets an error escape."""
        delay = RECONNECT_MIN_S
        while not self._stopping.is_set():
            client = await self._connect()
            if client is None:
                await self._sleep(delay)
                delay = min(RECONNECT_MAX_S, delay * 2)
                continue
            delay = RECONNECT_MIN_S
            pump = asyncio.create_task(self._pump_events(client), name="fielddeck-ui-events")
            try:
                await self._poll_forever(client)
            except asyncio.CancelledError:
                raise
            except FieldDeckError as exc:
                self._note_error(exc)
            finally:
                await self._cancel(pump)
                self._client = None
                await self._drop_lease()
                with contextlib.suppress(Exception):
                    await client.close()
                if not self._stopping.is_set():
                    self.link = LinkView(
                        connected=False,
                        socket=self.link.socket,
                        detail=self.link.detail or "instrumentd closed the connection",
                        attempts=self.link.attempts,
                    )
                    self.safety = SafetyView(
                        state=self.safety.state,
                        estop_active=self.safety.estop_active,
                        estop_reason=self.safety.estop_reason,
                        grants=self.safety.grants,
                        leases=self.safety.leases,
                        stale=True,
                    )
                    self._touch()

    async def _connect(self) -> InstrumentClient | None:
        client = InstrumentClient(self._socket_path, source=self._source, timeout_s=20.0)
        attempts = self.link.attempts + 1
        try:
            await client.connect()
        except FieldDeckError as exc:
            self.link = LinkView(
                connected=False,
                socket=str(client.socket_path),
                detail=exc.message,
                attempts=attempts,
            )
            self._touch()
            return None
        self._client = client
        self.link = LinkView(
            connected=True,
            socket=str(client.socket_path),
            detail="",
            attempts=attempts,
            server=dict(client.server_info),
        )
        self._want_devices = True
        self._want_status = True
        await self._load_limits(client)
        self._touch()
        return client

    async def _load_limits(self, client: InstrumentClient) -> None:
        """Policy ceilings, read once per connection: they cannot change under us."""
        try:
            result = await client.execute("system.limits", {})
        except FieldDeckError:
            return
        self.limits = dict(result.result.get("global") or {})
        self.max_arm_ttl_s = {
            str(key): float(value)
            for key, value in (result.result.get("max_arm_ttl_s") or {}).items()
        }
        self.denied_permissions = tuple(
            str(p) for p in result.result.get("denied_permissions") or []
        )

    async def _poll_forever(self, client: InstrumentClient) -> None:
        next_status = 0.0
        while not self._stopping.is_set():
            self._apply_safety(await client.call("safety.status"))
            now = monotonic_ns() / 1e9
            if self._want_status or now >= next_status:
                self._want_status = False
                next_status = now + self._status_poll_s
                await self._poll_status(client)
            if self._want_devices:
                self._want_devices = False
                await self._poll_devices(client)
            self._touch()
            await self._sleep(self._safety_poll_s)

    async def _poll_status(self, client: InstrumentClient) -> None:
        try:
            payload = (await client.execute("system.status", {})).result
        except TransportError:
            raise
        except FieldDeckError as exc:
            self._note_error(exc)
            return
        storage = payload.get("storage") or {}
        self.system = SystemView(
            version=str(payload.get("version", "?")),
            simulated=bool(payload.get("simulated")),
            uptime_s=float(payload.get("uptime_s") or 0.0),
            utc=str(payload.get("utc") or ""),
            device_count=int((payload.get("devices") or {}).get("total") or 0),
            running_actions=len(payload.get("running_actions") or []),
            sessions_dir=str(storage.get("sessions_dir") or ""),
            compression=str(storage.get("compression") or ""),
        )
        session = payload.get("session")
        self.session = (
            SessionView(
                id=str(session["id"]),
                name=str(session["name"]),
                elapsed_s=float(session.get("elapsed_s") or 0.0),
                recording=bool(session.get("recording", True)),
            )
            if isinstance(session, dict)
            else None
        )

    async def _poll_devices(self, client: InstrumentClient) -> None:
        try:
            payload = (await client.execute("device.list", {})).result
        except TransportError:
            raise
        except FieldDeckError as exc:
            self._note_error(exc)
            return
        devices: list[DeviceDescriptor] = []
        for raw in payload.get("devices") or []:
            try:
                devices.append(DeviceDescriptor.model_validate(raw))
            except ValueError:
                # A newer daemon may describe a device this panel cannot model.
                # Losing one row beats losing the device list.
                continue
        self.devices = tuple(devices)
        self.aliases = dict(payload.get("aliases") or {})

    async def _pump_events(self, client: InstrumentClient) -> None:
        """Consume the event stream.  Events never repaint anything directly.

        They land in a bounded deque and set refresh flags; the screens redraw
        on their own timer.  That is what stops a busy CAN bus from turning
        into a hundred layout passes a second.
        """
        try:
            async for event in client.subscribe():
                self.events.append(event)
                self._observe(event)
        except asyncio.CancelledError:
            raise
        except FieldDeckError as exc:
            self._note_error(exc)

    def _observe(self, event: Event) -> None:
        if event.type in {
            EventType.DEVICE_DISCOVERED,
            EventType.DEVICE_LOST,
            EventType.DEVICE_CONNECTED,
            EventType.DEVICE_DISCONNECTED,
        }:
            self._want_devices = True
            self._wake.set()
        if event.type in {
            EventType.SESSION_STARTED,
            EventType.SESSION_STOPPED,
            EventType.CAPTURE_STARTED,
            EventType.CAPTURE_STOPPED,
            EventType.DAEMON_STARTED,
        }:
            self._want_status = True
            self._wake.set()
        if event.type in {EventType.ESTOP, EventType.ESTOP_CLEARED}:
            self._wake.set()
        if event.type in {EventType.LEASE_EXPIRED, EventType.LEASE_RELEASED} and self._held_lease:
            payload_id = str(event.payload.get("lease_id") or "")
            if payload_id and payload_id == self._held_lease[0]:
                self._held_lease = None
        if event.severity in {EventSeverity.ERROR, EventSeverity.CRITICAL} or event.type in {
            EventType.DEVICE_FAULT,
            EventType.CAPTURE_OVERFLOW,
            EventType.STORAGE_LOW,
            EventType.LIMIT_REJECTED,
        }:
            self.fault = FaultView(
                message=event.message or str(event.type),
                device_id=event.device_id,
                utc_ns=event.utc_ns,
                monotonic_ns=event.monotonic_ns,
                severity=str(event.severity),
                type=str(event.type),
            )

    # -- helpers -----------------------------------------------------------

    def _apply_safety(self, payload: dict[str, Any]) -> None:
        grants: list[ArmGrant] = []
        for raw in payload.get("grants") or []:
            try:
                grants.append(ArmGrant.model_validate(raw))
            except ValueError:
                continue
        leases: list[OutputLease] = []
        for raw in payload.get("leases") or []:
            try:
                leases.append(OutputLease.model_validate(raw))
            except ValueError:
                continue
        self.safety = SafetyView(
            state=str(payload.get("state") or "?"),
            estop_active=bool(payload.get("estop_active")),
            estop_reason=payload.get("estop_reason"),
            grants=tuple(grants),
            leases=tuple(leases),
            stale=False,
        )

    def _record(self, outcome: Outcome, remember: bool) -> Outcome:
        if remember:
            self.last_outcome = outcome
            self._touch()
        return outcome

    def _note_error(self, exc: FieldDeckError) -> None:
        if isinstance(exc, TransportError):
            self.link = LinkView(
                connected=False,
                socket=self.link.socket,
                detail=exc.message,
                attempts=self.link.attempts,
            )
            self._client = None
            self._touch()

    def _touch(self) -> None:
        self.revision += 1

    async def _sleep(self, delay: float) -> None:
        """Wait, but wake early on shutdown or on a refresh request."""
        self._wake.clear()
        waiters: Sequence[asyncio.Task[bool]] = [
            asyncio.ensure_future(self._stopping.wait()),
            asyncio.ensure_future(self._wake.wait()),
        ]
        try:
            await asyncio.wait(waiters, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
