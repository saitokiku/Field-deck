"""``instrumentd`` — the single authority for hardware.

Boot state is SAFE: no grants, no leases, no recipe, and a safe-state request
sent to every controllable device that supports one.  Nothing about that is
recoverable from disk, which is the point — a reboot is always a return to
safe, never a restoration of whatever was armed before the power went out.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import signal
from pathlib import Path
from typing import Any, ClassVar

from fielddeck import RPC_PROTOCOL_VERSION, __version__
from fielddeck.capture.sessions import SessionManager
from fielddeck.common.config import (
    FieldDeckConfig,
    SafetyConfig,
    load_config,
    load_safety_config,
)
from fielddeck.common.errors import InvalidRequest, PermissionDenied, UnknownAction
from fielddeck.common.events import EventSeverity, EventType, new_event
from fielddeck.common.logging import configure_logging, get_logger
from fielddeck.common.models import (
    ActionRequest,
    ArmScope,
    PermissionLevel,
)
from fielddeck.common.paths import Paths, default_paths
from fielddeck.common.timebase import Timestamp
from fielddeck.daemon.client import InstrumentClient  # noqa: F401  (re-export convenience)
from fielddeck.daemon.core_actions import CoreActions
from fielddeck.daemon.dispatcher import Dispatcher
from fielddeck.daemon.events import EventBus
from fielddeck.daemon.registry import DeviceRegistry
from fielddeck.daemon.rpc import ClientConnection, RpcServer
from fielddeck.safety.manager import SafetyManager

__all__ = ["InstrumentDaemon"]

_log = get_logger("fielddeck.daemon.service")

#: How often expired grants and leases are reaped.  Fast enough that a lapsed
#: POWER lease drops the output promptly, cheap enough to idle at ~0% CPU.
SAFETY_TICK_S = 0.25

#: Subsystems that contribute daemon-level actions.  Each is
#: ``(module, factory)`` where the factory takes the daemon and returns a
#: ``{name: ActionSpec}`` mapping.  A subsystem whose optional dependency is
#: missing simply does not register; the daemon still starts.
_OPTIONAL_ACTION_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("fielddeck.analysis.actions", "build_action_specs"),
    ("fielddeck.recipes.actions", "build_action_specs"),
    ("fielddeck.capture.actions", "build_action_specs"),
    ("fielddeck.debug.actions", "build_action_specs"),
    ("fielddeck.protocols.actions", "build_action_specs"),
)


class InstrumentDaemon:
    """Owns every device, the safety state and the RPC surface."""

    def __init__(
        self,
        *,
        paths: Paths | None = None,
        config: FieldDeckConfig | None = None,
        safety_config: SafetyConfig | None = None,
        socket_path: Path | None = None,
        restricted_socket_path: Path | None = None,
        enable_restricted_socket: bool = True,
    ) -> None:
        self.paths = (paths or default_paths()).ensure()
        self.config = config or load_config(self.paths)
        self.safety_config = safety_config or load_safety_config(self.paths)
        self.started_at = Timestamp.now()

        self.bus = EventBus()
        self.safety = SafetyManager(self.safety_config, emit=self.bus.publish)
        self.registry = DeviceRegistry(aliases=self.config.alias_map())
        self.sessions = SessionManager(
            self.config.storage.sessions_dir or self.paths.sessions_dir,
            publish=self.bus.publish,
            min_free_mb=self.config.storage.min_free_mb,
            simulated=self.config.simulate,
        )
        self.sessions.attach_bus(self.bus)
        self.dispatcher = Dispatcher(
            registry=self.registry,
            safety=self.safety,
            bus=self.bus,
            sessions=self.sessions,
        )
        self.core = CoreActions(self)
        self.registry.register_global(self.core.specs())
        self._register_optional_actions()

        self._socket_path = socket_path or self.paths.socket
        self._restricted_socket_path = (
            restricted_socket_path or (self._socket_path.with_name("instrumentd-ai.sock"))
            if enable_restricted_socket
            else None
        )
        self.rpc = RpcServer(
            self._handle_rpc,
            socket_path=self._socket_path,
            restricted_socket_path=self._restricted_socket_path,
            on_disconnect=self._on_disconnect,
        )
        self._safety_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._subscription_counter = 0

    def _register_optional_actions(self) -> None:
        """Load action providers that ship with optional subsystems."""
        for module_name, factory_name in _OPTIONAL_ACTION_PROVIDERS:
            try:
                module = importlib.import_module(module_name)
            except ImportError as exc:
                _log.info(
                    "action provider unavailable",
                    extra={"module": module_name, "reason": str(exc)},
                )
                continue
            factory = getattr(module, factory_name, None)
            if factory is None:  # pragma: no cover - defensive
                continue
            try:
                self.registry.register_global(factory(self))
            except Exception:  # noqa: BLE001 - a bad provider must not block boot
                _log.exception("action provider failed", extra={"module": module_name})

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        _log.info(
            "instrumentd starting",
            extra={
                "version": __version__,
                "simulated": self.config.simulate,
                "socket": str(self._socket_path),
            },
        )
        self.safety.reset()
        await self.discover()
        # Boot into a known-safe hardware state before any client can connect.
        await self.dispatcher.apply_safe_state(reason="daemon startup")
        await self.rpc.start()
        self._safety_task = asyncio.create_task(self._safety_loop())
        self.bus.publish(
            new_event(
                EventType.DAEMON_STARTED,
                message=f"instrumentd {__version__} ready ({len(self.registry)} devices)",
                payload={
                    "version": __version__,
                    "protocol": RPC_PROTOCOL_VERSION,
                    "simulated": self.config.simulate,
                    "devices": len(self.registry),
                    "socket": str(self._socket_path),
                    "restricted_socket": str(self._restricted_socket_path)
                    if self._restricted_socket_path
                    else None,
                },
            )
        )

    async def serve_forever(self) -> None:
        await self.start()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stopping.set)
        try:
            await self._stopping.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        _log.info("instrumentd stopping")
        self.bus.publish(new_event(EventType.DAEMON_STOPPING, message="instrumentd stopping"))
        if self._safety_task is not None:
            self._safety_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._safety_task
            self._safety_task = None
        # Anything still energised gets turned off before we let go of it.
        await self.dispatcher.apply_safe_state(reason="daemon shutdown")
        await self.rpc.stop()
        for driver in self.registry.drivers:
            with contextlib.suppress(Exception):
                await driver.disconnect()
        self.sessions.shutdown()

    # -- discovery ---------------------------------------------------------

    async def discover(self) -> tuple[list[str], list[str]]:
        """Passive inventory.  Adds new devices, retires vanished ones."""
        from fielddeck.discovery import scan

        found = await scan(self.config)
        found_ids = {driver.device_id for driver in found}
        existing_ids = {driver.device_id for driver in self.registry.drivers}

        added: list[str] = []
        for driver in found:
            if driver.device_id in existing_ids:
                continue
            self.registry.add(driver)
            added.append(driver.device_id)
            self.bus.publish(
                new_event(
                    EventType.DEVICE_DISCOVERED,
                    device_id=driver.device_id,
                    message=f"discovered {driver.descriptor.display_name}",
                    payload=driver.describe().model_dump(mode="json"),
                )
            )

        removed: list[str] = []
        for device_id in existing_ids - found_ids:
            gone = self.registry.remove(device_id)
            if gone is not None:
                with contextlib.suppress(Exception):
                    await gone.disconnect()
                removed.append(device_id)
                self.bus.publish(
                    new_event(
                        EventType.DEVICE_LOST,
                        device_id=device_id,
                        severity=EventSeverity.WARNING,
                        message=f"{device_id} is no longer present",
                    )
                )
        return added, removed

    # -- safety timer ------------------------------------------------------

    async def _safety_loop(self) -> None:
        """Reap expired grants and leases; drive safe state on lapse."""
        try:
            while True:
                await asyncio.sleep(SAFETY_TICK_S)
                _grants, leases = self.safety.sweep()
                if leases:
                    await self.dispatcher.apply_safe_state(
                        reason="output lease expired",
                        device_ids=[lease.device_id for lease in leases],
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the safety timer must never die quietly
            _log.exception("safety loop failed; restarting it")
            await asyncio.sleep(1.0)
            self._safety_task = asyncio.create_task(self._safety_loop())

    async def _on_disconnect(self, connection: ClientConnection) -> None:
        """A client that dies must not leave hardware energised."""
        orphaned = self.safety.leases.take_for_connection(connection.id)
        if not orphaned:
            return
        for lease in orphaned:
            self.bus.publish(
                new_event(
                    EventType.LEASE_EXPIRED,
                    severity=EventSeverity.WARNING,
                    device_id=lease.device_id,
                    action=lease.action,
                    message=(
                        f"client holding lease {lease.lease_id} disconnected; driving safe state"
                    ),
                    payload=lease.model_dump(mode="json"),
                )
            )
        await self.dispatcher.apply_safe_state(
            reason="lease owner disconnected",
            device_ids=[lease.device_id for lease in orphaned],
        )

    # -- RPC ---------------------------------------------------------------

    async def _handle_rpc(
        self, connection: ClientConnection, method: str, params: dict[str, Any]
    ) -> Any:
        handler = self._RPC_METHODS.get(method)
        if handler is None:
            raise UnknownAction(
                f"unknown RPC method {method!r}",
                details={"method": method, "known": sorted(self._RPC_METHODS)},
            )
        return await handler(self, connection, params)

    async def _rpc_hello(
        self, connection: ClientConnection, params: dict[str, Any]
    ) -> dict[str, Any]:
        connection.source = connection.resolve_source(params.get("source"))
        self.bus.publish(
            new_event(
                EventType.CLIENT_CONNECTED,
                source=connection.source,
                severity=EventSeverity.DEBUG,
                message=f"{connection.source} connected",
                payload={"connection": connection.id},
            )
        )
        return {
            "version": __version__,
            "protocol": RPC_PROTOCOL_VERSION,
            "source": str(connection.source),
            "simulated": self.config.simulate,
            "restricted": not connection.allow_authorization,
            "pid": os.getpid(),
            "devices": len(self.registry),
        }

    async def _rpc_action_execute(
        self, connection: ClientConnection, params: dict[str, Any]
    ) -> dict[str, Any]:
        payload = dict(params)
        payload["source"] = str(connection.resolve_source(payload.get("source")))
        request = ActionRequest.model_validate(payload)
        result = await self.dispatcher.execute(request, connection_id=connection.id)
        return result.model_dump(mode="json")

    async def _rpc_action_cancel(
        self, connection: ClientConnection, params: dict[str, Any]
    ) -> dict[str, Any]:
        request_id = params.get("request_id")
        if not isinstance(request_id, str):
            raise InvalidRequest("request_id is required to cancel an action")
        return {"cancelled": await self.dispatcher.cancel(request_id=request_id)}

    async def _rpc_action_running(
        self, connection: ClientConnection, params: dict[str, Any]
    ) -> dict[str, Any]:
        return {"running": self.dispatcher.running()}

    # -- safety RPC (never routed through the dispatcher) ------------------

    async def _rpc_safety_status(
        self, connection: ClientConnection, params: dict[str, Any]
    ) -> dict[str, Any]:
        now = Timestamp.now().monotonic_ns
        snapshot = self.safety.snapshot()
        return {
            **snapshot.model_dump(mode="json"),
            "state": snapshot.state_word,
            "grants_remaining_s": {
                grant.grant_id: round(grant.remaining_s(now), 1) for grant in snapshot.grants
            },
            "leases_remaining_s": {
                lease.lease_id: round(lease.remaining_s(now), 1) for lease in snapshot.leases
            },
            "limits": self.safety.limits.describe(),
        }

    async def _rpc_safety_arm(
        self, connection: ClientConnection, params: dict[str, Any]
    ) -> dict[str, Any]:
        source = connection.resolve_source(params.get("source"))
        if not source.may_create_grants:
            raise PermissionDenied(
                f"{source} may not arm FieldDeck",
                details={"source": str(source)},
            )
        try:
            permission = PermissionLevel(str(params.get("permission", "")).upper())
        except ValueError as exc:
            raise InvalidRequest(
                f"unknown permission {params.get('permission')!r}",
                details={"known": [str(p) for p in PermissionLevel if p.requires_grant]},
            ) from exc
        scope_raw = params.get("scope")
        scope = ArmScope.model_validate(scope_raw) if scope_raw else None
        grant = self.safety.arm(
            permission=permission,
            ttl_s=params.get("ttl_s"),
            source=source,
            scope=scope,
            note=params.get("note"),
            session_id=self.sessions.current_id,
        )
        return {"grant": grant.model_dump(mode="json")}

    async def _rpc_safety_disarm(
        self, connection: ClientConnection, params: dict[str, Any]
    ) -> dict[str, Any]:
        source = connection.resolve_source(params.get("source"))
        revoked = self.safety.disarm(
            source=source,
            grant_id=params.get("grant_id"),
            session_id=self.sessions.current_id,
        )
        return {"revoked": [grant.grant_id for grant in revoked]}

    async def _rpc_safety_estop(
        self, connection: ClientConnection, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Any client may trigger ESTOP, including Claude.  Stopping is never
        the dangerous direction."""
        source = connection.resolve_source(params.get("source"))
        reason = str(params.get("reason") or f"requested by {source}")
        leases = self.safety.engage_estop(
            reason=reason, source=source, session_id=self.sessions.current_id
        )
        for running in self.dispatcher.running():
            if running.get("request_id"):
                await self.dispatcher.cancel(request_id=str(running["request_id"]))
        results = await self.dispatcher.apply_safe_state(reason=f"ESTOP: {reason}")
        if self.sessions.recorder is not None:
            self.sessions.recorder.flush()
        return {
            "estop": True,
            "reason": reason,
            "surrendered_leases": [lease.lease_id for lease in leases],
            "safe_state": results,
            "evidence": "all captured data and session metadata preserved",
        }

    async def _rpc_safety_estop_clear(
        self, connection: ClientConnection, params: dict[str, Any]
    ) -> dict[str, Any]:
        source = connection.resolve_source(params.get("source"))
        self.safety.acknowledge_estop(source=source, session_id=self.sessions.current_id)
        return {"estop": False, "state": self.safety.snapshot().state_word}

    async def _rpc_lease_renew(
        self, connection: ClientConnection, params: dict[str, Any]
    ) -> dict[str, Any]:
        lease_id = str(params.get("lease_id", ""))
        lease = self.safety.leases.renew(lease_id, ttl_s=params.get("ttl_s"))
        self.bus.publish(
            new_event(
                EventType.LEASE_RENEWED,
                source=connection.source,
                device_id=lease.device_id,
                action=lease.action,
                session_id=self.sessions.current_id,
                message=f"lease {lease.lease_id} renewed for {lease.ttl_s:g}s",
            )
        )
        return {"lease": lease.model_dump(mode="json")}

    async def _rpc_lease_release(
        self, connection: ClientConnection, params: dict[str, Any]
    ) -> dict[str, Any]:
        lease_id = str(params.get("lease_id", ""))
        lease = self.safety.leases.release(lease_id)
        if lease is not None:
            await self.dispatcher.apply_safe_state(
                reason="lease released", device_ids=[lease.device_id]
            )
        return {"released": lease.lease_id if lease else None}

    # -- events RPC --------------------------------------------------------

    async def _rpc_events_subscribe(
        self, connection: ClientConnection, params: dict[str, Any]
    ) -> dict[str, Any]:
        types_raw = params.get("types")
        types = None
        if types_raw:
            try:
                types = [EventType(str(t)) for t in types_raw]
            except ValueError as exc:
                raise InvalidRequest(
                    f"unknown event type in {types_raw!r}",
                    details={"known": [str(t) for t in EventType]},
                ) from exc
        subscription = self.bus.subscribe(types=types, session_id=params.get("session_id"))
        self._subscription_counter += 1
        subscription_id = f"sub-{self._subscription_counter}"

        async def pump() -> None:
            try:
                async for event in subscription:
                    if connection.closed:
                        break
                    await connection.send_event(subscription_id, event.model_dump(mode="json"))
            except asyncio.CancelledError:
                raise
            finally:
                subscription.close()

        connection.subscriptions[subscription_id] = asyncio.create_task(pump())
        return {"subscription": subscription_id}

    async def _rpc_events_unsubscribe(
        self, connection: ClientConnection, params: dict[str, Any]
    ) -> dict[str, Any]:
        subscription_id = str(params.get("subscription", ""))
        task = connection.subscriptions.pop(subscription_id, None)
        if task is not None:
            task.cancel()
        return {"unsubscribed": subscription_id if task is not None else None}

    async def _rpc_events_recent(
        self, connection: ClientConnection, params: dict[str, Any]
    ) -> dict[str, Any]:
        limit = int(params.get("limit", 100))
        types_raw = params.get("types")
        types = [EventType(str(t)) for t in types_raw] if types_raw else None
        events = self.bus.recent(limit=min(limit, 1000), types=types)
        return {"events": [event.model_dump(mode="json") for event in events]}

    _RPC_METHODS: ClassVar[dict[str, Any]] = {
        "hello": _rpc_hello,
        "action.execute": _rpc_action_execute,
        "action.cancel": _rpc_action_cancel,
        "action.running": _rpc_action_running,
        "safety.status": _rpc_safety_status,
        "safety.arm": _rpc_safety_arm,
        "safety.disarm": _rpc_safety_disarm,
        "safety.estop": _rpc_safety_estop,
        "safety.estop_clear": _rpc_safety_estop_clear,
        "safety.lease_renew": _rpc_lease_renew,
        "safety.lease_release": _rpc_lease_release,
        "events.subscribe": _rpc_events_subscribe,
        "events.unsubscribe": _rpc_events_unsubscribe,
        "events.recent": _rpc_events_recent,
    }


async def amain(argv: list[str] | None = None) -> int:
    daemon = InstrumentDaemon()
    configure_logging(daemon.config.logging.level, json_output=daemon.config.logging.json_output)
    await daemon.serve_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    try:
        return asyncio.run(amain(argv))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 0
