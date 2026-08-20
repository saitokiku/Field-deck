"""Unix-domain-socket RPC server.

Two listening sockets, and the difference between them is a real security
boundary rather than a convention:

``instrumentd.sock`` (mode 0660, group ``fielddeck``)
    The full control surface.  The HMI and ``fdctl`` use it.

``instrumentd-ai.sock`` (mode 0660, optional)
    Every request on this socket is stamped ``source=claude`` by the server,
    and the authorization methods are refused at the transport.  An AI client
    cannot arm FieldDeck by claiming to be the HMI, because the socket it can
    reach does not accept ``safety.arm`` at all.

Neither socket is ever a TCP port.  The control API does not listen on the
network.
"""

from __future__ import annotations

import asyncio
import contextlib
import grp
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fielddeck.common.errors import FieldDeckError, InvalidRequest, PermissionDenied
from fielddeck.common.logging import get_logger
from fielddeck.common.models import ClientSource
from fielddeck.daemon.protocol import (
    MAX_LINE_BYTES,
    decode_request,
    encode_error,
    encode_event,
    encode_response,
)

__all__ = ["ClientConnection", "RpcServer"]

_log = get_logger("fielddeck.daemon.rpc")

Handler = Callable[["ClientConnection", str, dict[str, Any]], Awaitable[Any]]

#: Methods that change what FieldDeck is allowed to do.  Refused outright on
#: the restricted socket.
AUTHORIZATION_METHODS = frozenset({"safety.arm", "safety.disarm", "safety.estop_clear"})


class ClientConnection:
    """One connected client."""

    _next_id = 0

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        forced_source: ClientSource | None,
        allow_authorization: bool,
    ) -> None:
        ClientConnection._next_id += 1
        self.id = ClientConnection._next_id
        self.reader = reader
        self.writer = writer
        self.forced_source = forced_source
        self.allow_authorization = allow_authorization
        #: Client-declared identity, used for the audit trail.  On the
        #: restricted socket it is overridden and cannot be chosen.
        self.source: ClientSource = forced_source or ClientSource.FDCTL
        self.subscriptions: dict[str, Any] = {}
        self._write_lock = asyncio.Lock()
        self._closed = False

    def resolve_source(self, declared: str | None) -> ClientSource:
        """Trust the declaration only where the socket permits it."""
        if self.forced_source is not None:
            return self.forced_source
        if declared is None:
            return self.source
        try:
            return ClientSource(declared)
        except ValueError as exc:
            raise InvalidRequest(
                f"unknown client source {declared!r}",
                details={"known": [str(s) for s in ClientSource]},
            ) from exc

    async def send(self, data: bytes) -> None:
        if self._closed:
            return
        async with self._write_lock:
            try:
                self.writer.write(data)
                await self.writer.drain()
            except (ConnectionResetError, BrokenPipeError):  # pragma: no cover
                self._closed = True

    async def send_event(self, subscription_id: str, event: dict[str, Any]) -> None:
        await self.send(encode_event(subscription_id, event))

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        self._closed = True
        for task in list(self.subscriptions.values()):
            task.cancel()
        self.subscriptions.clear()
        with contextlib.suppress(Exception):
            self.writer.close()
            await self.writer.wait_closed()


class RpcServer:
    """Serves the FieldDeck control protocol on one or two Unix sockets."""

    def __init__(
        self,
        handler: Handler,
        *,
        socket_path: Path,
        group: str | None = "fielddeck",
        mode: int = 0o660,
        restricted_socket_path: Path | None = None,
        restricted_source: ClientSource = ClientSource.CLAUDE,
        on_disconnect: Callable[[ClientConnection], Awaitable[None]] | None = None,
    ) -> None:
        self._handler = handler
        self._socket_path = socket_path
        self._restricted_path = restricted_socket_path
        self._restricted_source = restricted_source
        self._group = group
        self._mode = mode
        self._on_disconnect = on_disconnect
        self._servers: list[asyncio.AbstractServer] = []
        self._connections: set[ClientConnection] = set()

    @property
    def connections(self) -> list[ClientConnection]:
        return list(self._connections)

    async def start(self) -> None:
        self._servers.append(
            await self._listen(self._socket_path, forced_source=None, allow_authorization=True)
        )
        if self._restricted_path is not None:
            self._servers.append(
                await self._listen(
                    self._restricted_path,
                    forced_source=self._restricted_source,
                    allow_authorization=False,
                )
            )

    async def _listen(
        self, path: Path, *, forced_source: ClientSource | None, allow_authorization: bool
    ) -> asyncio.AbstractServer:
        # Socket setup is one-shot at startup; the blocking filesystem calls
        # here are measured in microseconds and happen before we serve anyone.
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():  # noqa: ASYNC240
            path.unlink()  # noqa: ASYNC240

        async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._serve_client(
                reader,
                writer,
                forced_source=forced_source,
                allow_authorization=allow_authorization,
            )

        server = await asyncio.start_unix_server(on_client, path=str(path))
        self._harden(path)
        _log.info(
            "listening",
            extra={
                "path": str(path),
                "forced_source": str(forced_source) if forced_source else None,
                "authorization": allow_authorization,
            },
        )
        return server

    def _harden(self, path: Path) -> None:
        """Group-restricted socket.  Never world-writable."""
        try:
            path.chmod(self._mode)
        except OSError as exc:  # pragma: no cover - unusual filesystems
            _log.warning("could not set socket mode", extra={"path": str(path), "error": str(exc)})
        if not self._group:
            return
        try:
            gid = grp.getgrnam(self._group).gr_gid
            os.chown(path, -1, gid)
        except (KeyError, PermissionError, OSError):
            # Developer laptops have no fielddeck group; the socket is still
            # owner-only-ish under 0660 and lives in a private runtime dir.
            _log.debug("socket group not applied", extra={"group": self._group})

    async def _serve_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        forced_source: ClientSource | None,
        allow_authorization: bool,
    ) -> None:
        connection = ClientConnection(
            reader,
            writer,
            forced_source=forced_source,
            allow_authorization=allow_authorization,
        )
        self._connections.add(connection)
        _log.debug("client connected", extra={"connection": connection.id})
        try:
            while not connection.closed:
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.IncompleteReadError:
                    break
                except asyncio.LimitOverrunError:
                    await connection.send(
                        encode_error(
                            None,
                            InvalidRequest(
                                f"request line exceeds {MAX_LINE_BYTES} bytes"
                            ).to_dict(),
                        )
                    )
                    break
                except (ConnectionResetError, BrokenPipeError):  # pragma: no cover
                    break
                if not line.strip():
                    continue
                await self._handle_line(connection, line)
        finally:
            self._connections.discard(connection)
            if self._on_disconnect is not None:
                with contextlib.suppress(Exception):
                    await self._on_disconnect(connection)
            await connection.close()
            _log.debug("client disconnected", extra={"connection": connection.id})

    async def _handle_line(self, connection: ClientConnection, line: bytes) -> None:
        request_id: str | None = None
        try:
            request = decode_request(line)
            request_id = request.id
            if not connection.allow_authorization and request.method in AUTHORIZATION_METHODS:
                raise PermissionDenied(
                    f"{request.method} is not available on the restricted socket; "
                    "a human must authorize FieldDeck from the HMI or fdctl",
                    details={"method": request.method, "socket": "restricted"},
                )
            result = await self._handler(connection, request.method, request.params)
            await connection.send(encode_response(request_id, result))
        except FieldDeckError as exc:
            await connection.send(encode_error(request_id, exc.to_dict()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one malformed request must not kill the server
            _log.exception("unhandled RPC error")
            await connection.send(
                encode_error(
                    request_id,
                    FieldDeckError(
                        f"internal error: {exc}", details={"type": type(exc).__name__}
                    ).to_dict(),
                )
            )

    async def stop(self) -> None:
        for connection in list(self._connections):
            await connection.close()
        self._connections.clear()
        for server in self._servers:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        self._servers.clear()
        for path in (self._socket_path, self._restricted_path):
            if path is not None:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
