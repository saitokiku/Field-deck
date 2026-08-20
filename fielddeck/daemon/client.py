"""Client for the ``instrumentd`` control socket.

Used by ``fdctl``, the HMI, the recipe runner and the MCP server.  None of
them ever touches hardware directly; they all speak this protocol.

The client multiplexes: several calls can be in flight while events stream in
on the same connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

from fielddeck import RPC_PROTOCOL_VERSION
from fielddeck.common.errors import FieldDeckError, TransportError, error_from_dict
from fielddeck.common.events import Event, EventType
from fielddeck.common.models import ActionResult, ClientSource
from fielddeck.common.paths import socket_path as default_socket_path

__all__ = ["InstrumentClient", "connect"]


class InstrumentClient:
    """Async client.  Use as an async context manager."""

    def __init__(
        self,
        socket_path: Path | str | None = None,
        *,
        source: ClientSource = ClientSource.FDCTL,
        timeout_s: float = 30.0,
    ) -> None:
        self.socket_path = Path(socket_path) if socket_path else default_socket_path()
        self.source = source
        self.timeout_s = timeout_s
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._subscriptions: dict[str, asyncio.Queue[Event]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._counter = 0
        self.server_info: dict[str, Any] = {}

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> InstrumentClient:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)), timeout=5.0
            )
        except FileNotFoundError as exc:
            raise TransportError(
                f"instrumentd is not running (no socket at {self.socket_path})",
                details={"socket": str(self.socket_path)},
                preserved="nothing was attempted",
            ) from exc
        except PermissionError as exc:
            raise TransportError(
                f"no permission to open {self.socket_path}; add your user to the "
                "'fielddeck' group and log in again",
                details={"socket": str(self.socket_path)},
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise TransportError(
                f"cannot reach instrumentd at {self.socket_path}: {exc}",
                details={"socket": str(self.socket_path)},
            ) from exc

        self._reader_task = asyncio.create_task(self._read_loop())
        self.server_info = await self.call("hello", {"source": str(self.source)})
        return self

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
                await self._writer.wait_closed()
            self._writer = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(TransportError("connection closed"))
        self._pending.clear()

    async def __aenter__(self) -> InstrumentClient:
        return await self.connect()

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # -- calls -------------------------------------------------------------

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float | None = None
    ) -> Any:
        """One request/response round trip.  Raises the server's typed error."""
        if self._writer is None:
            raise TransportError("client is not connected")
        self._counter += 1
        request_id = str(self._counter)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        line = (
            json.dumps(
                {
                    "v": RPC_PROTOCOL_VERSION,
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                },
                default=str,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        try:
            self._writer.write(line)
            await self._writer.drain()
            return await asyncio.wait_for(future, timeout_s or self.timeout_s)
        except TimeoutError as exc:
            raise TransportError(
                f"instrumentd did not answer {method} within {timeout_s or self.timeout_s:g}s",
                details={"method": method},
            ) from exc
        except (ConnectionResetError, BrokenPipeError) as exc:
            raise TransportError(f"connection to instrumentd lost during {method}") from exc
        finally:
            self._pending.pop(request_id, None)

    async def execute(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
        request_id: str | None = None,
    ) -> ActionResult:
        """Run an action.  Raises on failure so callers can use try/except."""
        payload = await self.call(
            "action.execute",
            {
                "action": action,
                "params": params or {},
                "source": str(self.source),
                "timeout_s": timeout_s,
                "request_id": request_id,
            },
            # Give the daemon room to enforce its own deadline first.
            timeout_s=(timeout_s + 10.0) if timeout_s else None,
        )
        result = ActionResult.model_validate(payload)
        if not result.ok:
            raise error_from_dict(result.error or {"message": f"{action} failed"})
        return result

    async def try_execute(
        self, action: str, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> ActionResult:
        """Like :meth:`execute` but returns the failed result instead of raising."""
        try:
            return await self.execute(action, params, **kwargs)
        except FieldDeckError as exc:
            return ActionResult(action=action, ok=False, error=exc.to_dict())

    # -- events ------------------------------------------------------------

    async def subscribe(
        self,
        *,
        types: Iterable[EventType | str] | None = None,
        session_id: str | None = None,
        maxsize: int = 512,
    ) -> AsyncIterator[Event]:
        """Stream events until the iterator is closed."""
        reply = await self.call(
            "events.subscribe",
            {
                "types": [str(t) for t in types] if types else None,
                "session_id": session_id,
            },
        )
        subscription_id = reply["subscription"]
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._subscriptions[subscription_id] = queue
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscriptions.pop(subscription_id, None)
            with contextlib.suppress(Exception):
                await self.call("events.unsubscribe", {"subscription": subscription_id})

    # -- internals ---------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                try:
                    line = await self._reader.readuntil(b"\n")
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    break
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:  # pragma: no cover - corrupt frame
                    continue
                if message.get("type") == "event":
                    self._deliver_event(message)
                    continue
                future = self._pending.get(str(message.get("id")))
                if future is None or future.done():
                    continue
                if message.get("ok"):
                    future.set_result(message.get("result"))
                else:
                    future.set_exception(error_from_dict(message.get("error") or {}))
        except asyncio.CancelledError:
            raise
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(TransportError("instrumentd closed the connection"))

    def _deliver_event(self, message: dict[str, Any]) -> None:
        queue = self._subscriptions.get(message.get("subscription", ""))
        if queue is None:
            return
        try:
            event = Event.model_validate(message["event"])
        except Exception:
            return
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # The consumer is slow.  Dropping the oldest keeps the newest
            # state visible, which is what a live display wants.
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)


async def connect(
    socket_path: Path | str | None = None, *, source: ClientSource = ClientSource.FDCTL
) -> InstrumentClient:
    return await InstrumentClient(socket_path, source=source).connect()
