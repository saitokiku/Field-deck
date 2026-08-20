"""Real serial ports: TTL UART, RS-232 and RS-485, behind pyserial.

This is the transport most bring-up sessions start with, and the one with the
most ways to quietly mislead an engineer.  The decisions worth knowing at 2am:

* **pyserial blocks.**  Every read, write and break happens in a worker thread
  (:func:`asyncio.to_thread`); nothing here may stall the daemon's event loop
  while a DUT is energised.  One reader thread per open port drains bytes and
  fans them out to subscribers, so a live HMI monitor and a session capture
  observe the same stream instead of stealing bytes from one another.

* **Opening a port is not free.**  Most USB adapters assert DTR and RTS when
  the port is opened, which is exactly the auto-reset circuit on an Arduino or
  an ESP32.  Ports are therefore opened lazily — never during discovery — and
  opened with DTR and RTS deasserted.  Deliberately driving those lines is a
  CONTROL-class act on the DUT and is not implemented here.

* **Bytes are preserved exactly.**  No newline translation, no decoding, no
  helpful reframing.  Timestamps are host arrival times, taken the instant a
  read returns; a UART gives no hardware timestamping, so read them as
  "no later than this, minus scheduling latency", never as line timing.

* **The electrical class is unknown until an operator says so.**  TTL, RS-232
  and RS-485 differ in voltage, polarity and topology, and nothing in the byte
  stream distinguishes them.  ``electrical`` stays ``"unknown"`` until it is
  configured; guessing it is how a 3.3 V pin meets +/-12 V.

Action names and parameter shapes mirror :mod:`fielddeck.sim.serial` so the
CLI, HMI, MCP surface and recipes work unchanged against either.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno as errno_module
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator

from fielddeck import __version__
from fielddeck.common.config import FieldDeckConfig
from fielddeck.common.errors import (
    ActionTimeout,
    DeviceBusy,
    DeviceDisconnected,
    FieldDeckError,
    InvalidRequest,
    TransportError,
    UnsupportedCapability,
)
from fielddeck.common.events import EventSeverity, EventType, new_event
from fielddeck.common.logging import get_logger
from fielddeck.common.models import (
    ConnectionState,
    DeviceCapability,
    DeviceDescriptor,
    DeviceRole,
    PermissionLevel,
    TransportKind,
)
from fielddeck.common.timebase import monotonic_ns
from fielddeck.discovery.linux import list_serial_ports
from fielddeck.drivers.base import ActionContext, DeviceParams, Driver, action

__all__ = ["SerialDriver", "discover_serial_drivers"]

_log = get_logger("fielddeck.transports.serial_port")

#: How long one blocking read waits before the reader thread returns to check
#: whether it has been asked to stop.  Short enough that closing a port is
#: prompt, long enough that an idle port does not spin a core.
_READ_TIMEOUT_S = 0.1

#: Ceiling on a single read handed back from the reader thread.
_MAX_CHUNK_BYTES = 65536

#: Per-subscriber buffer ceiling.  A consumer that cannot keep up drops bytes
#: and says so, rather than growing until the Pi runs out of memory.
_MAX_BUFFERED_BYTES = 4 * 1024 * 1024

#: Longest a write may block in its worker thread.  Deliberately shorter than
#: the CONTROL actions' 10 s dispatcher timeout so a stalled transmit is
#: reported as a clean, attributable error instead of a killed task.  Hardware
#: flow control with CTS never asserted is the usual cause.
_WRITE_TIMEOUT_S = 5.0

#: How long a receive loop waits on its queue before re-checking cancellation,
#: the action deadline and the reader's health.
_POLL_S = 0.05

#: Stop a receive this long before the dispatcher's deadline, so a capture
#: closes and registers its files instead of being cancelled mid-write.
_DEADLINE_MARGIN_S = 0.25

#: Chunks echoed back in an action result.  The full stream lives in the
#: capture file; an RPC reply is not a place to return megabytes.
_PREVIEW_CHUNKS = 20

_VALID_STOPBITS = (1.0, 1.5, 2.0)


def _pyserial() -> Any:
    """Import pyserial on demand.

    Optional by design: the daemon must import and run on a machine with no
    hardware libraries at all, so this is never imported at module scope.
    """
    try:
        import serial
    except ImportError as exc:  # pragma: no cover - exercised on installs without the extra
        raise UnsupportedCapability(
            "pyserial is not installed; install it with: pip install 'fielddeck[serial]'",
            details={"module": "serial", "extra": "serial"},
            preserved="nothing was opened and no bytes were sent",
        ) from exc
    return serial


# ---------------------------------------------------------------------------
# Parameters — shapes mirror fielddeck.sim.serial
# ---------------------------------------------------------------------------


class SerialMonitorParams(DeviceParams):
    duration_s: float = Field(default=2.0, gt=0, le=3600)
    max_bytes: int = Field(default=65536, ge=1, le=8_000_000)


class SerialCaptureParams(SerialMonitorParams):
    label: str = "capture"


class SerialSendParams(DeviceParams):
    hex: str | None = Field(default=None, description="Payload as hex bytes")
    text: str | None = Field(default=None, description="Payload as text")
    append_newline: bool = False

    @field_validator("hex")
    @classmethod
    def _valid_hex(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.replace(" ", "").replace("0x", "")
        try:
            bytes.fromhex(cleaned)
        except ValueError as exc:
            raise ValueError(f"hex must be whole bytes, got {value!r}") from exc
        return cleaned


class SerialConfigureParams(DeviceParams):
    baudrate: int = Field(default=115200, ge=50, le=12_000_000)
    bytesize: int = Field(default=8, ge=5, le=8)
    parity: str = Field(default="N", pattern="^[NEOMS]$")
    stopbits: float = Field(default=1.0)
    #: Hardware (RTS/CTS) and software (XON/XOFF) flow control.  Both are off
    #: unless asked for: RTS/CTS on a three-wire cable stalls every write.
    rtscts: bool = False
    xonxoff: bool = False
    #: Recorded, never inferred.  ``None`` leaves whatever the operator set.
    electrical: str | None = Field(
        default=None,
        pattern="^(ttl|rs232|rs485|unknown)$",
        description="Electrical class as known by the operator; software cannot detect it",
    )

    @field_validator("stopbits")
    @classmethod
    def _valid_stopbits(cls, value: float) -> float:
        if value not in _VALID_STOPBITS:
            raise ValueError(f"stopbits must be one of {_VALID_STOPBITS}, got {value!r}")
        return value


class SerialBreakParams(DeviceParams):
    duration_ms: int = Field(default=250, ge=1, le=2000)


# ---------------------------------------------------------------------------
# Port state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PortSettings:
    """Local framing.  None of this is knowledge about the DUT."""

    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1.0
    rtscts: bool = False
    xonxoff: bool = False

    @property
    def framing(self) -> str:
        whole = self.stopbits == int(self.stopbits)
        stopbits = int(self.stopbits) if whole else self.stopbits
        return f"{self.bytesize}{self.parity}{stopbits}"

    @property
    def flow_control(self) -> str:
        active = [name for name, on in (("rtscts", self.rtscts), ("xonxoff", self.xonxoff)) if on]
        return "+".join(active) if active else "none"


@dataclass(slots=True, eq=False)
class _Subscriber:
    """One consumer of the byte stream.

    Bounded on purpose: a consumer that falls behind loses the newest bytes
    and is told exactly how many, which is the only honest option once the
    alternative is exhausting memory on a 4 GB Pi.
    """

    queue: asyncio.Queue[tuple[int, bytes]] = field(default_factory=asyncio.Queue)
    buffered_bytes: int = 0
    dropped_bytes: int = 0
    dropped_chunks: int = 0


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class SerialDriver(Driver):
    """A real serial port.

    The port is opened on first use and stays open until the device is removed
    or the daemon stops, because closing it drops DTR/RTS and that is itself a
    signal to a DUT.
    """

    kind = TransportKind.SERIAL

    def __init__(
        self,
        *,
        device_id: str,
        path: str,
        display_name: str,
        vendor: str | None = None,
        product: str | None = None,
        serial_number: str | None = None,
        stable_id: bool = True,
        by_id: str | None = None,
        settings: _PortSettings | None = None,
    ) -> None:
        self._settings = settings or _PortSettings()
        descriptor = DeviceDescriptor(
            id=device_id,
            kind=TransportKind.SERIAL,
            display_name=display_name,
            path=path,
            vendor=vendor,
            product=product,
            serial_number=serial_number,
            roles=[DeviceRole.BUS],
            capabilities=[
                DeviceCapability.RX,
                DeviceCapability.TX,
                DeviceCapability.BAUD_CONFIG,
                DeviceCapability.STREAM,
                DeviceCapability.SAFE_STATE,
            ],
            state=ConnectionState.DISCOVERED,
            stable_id=stable_id,
            metadata={
                "baudrate": self._settings.baudrate,
                "framing": self._settings.framing,
                "flow_control": self._settings.flow_control,
                # Never inferred from framing or traffic; see the module
                # docstring.  Only serial.configure moves this.
                "electrical": "unknown",
                "by_id": by_id,
                "open_note": (
                    "opening the port may move DTR/RTS; boards with an auto-reset "
                    "circuit restart when that happens"
                ),
            },
            warning=(
                None
                if stable_id
                else (
                    "no USB serial number: this port is identified by its kernel name, "
                    "which can change on re-plug or reboot"
                )
            ),
        )
        super().__init__(descriptor)
        self.path = path
        self.by_id = by_id
        self._port: Any = None  # a serial.Serial; Any because pyserial is optional
        self._reader: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._open_lock = asyncio.Lock()
        self._subscribers: set[_Subscriber] = set()
        self._read_error: BaseException | None = None
        self._rx_bytes = 0
        self._tx_bytes = 0

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        """Open the port.

        Not called during discovery: enumeration must never move a modem line
        on a board that might be mid-boot.
        """
        await self._ensure_open()

    async def disconnect(self) -> None:
        await self._close()
        self._set_state(ConnectionState.DISCOVERED)

    async def probe(self) -> bool:
        """Does the device node still exist?  Reads the filesystem only."""
        return await asyncio.to_thread(Path(self.path).exists)

    async def status(self) -> dict[str, Any]:
        port = self._port
        lines = await asyncio.to_thread(self._modem_lines, port) if port is not None else {}
        return {
            "path": self.path,
            "by_id": self.by_id,
            "baudrate": self._settings.baudrate,
            "framing": self._settings.framing,
            "flow_control": self._settings.flow_control,
            "electrical": self._descriptor.metadata["electrical"],
            "state": str(self._descriptor.state),
            "open": port is not None,
            "stable_id": self._descriptor.stable_id,
            "rx_bytes": self._rx_bytes,
            "tx_bytes": self._tx_bytes,
            "listeners": len(self._subscribers),
            "modem_inputs": lines,
            "read_error": str(self._read_error) if self._read_error is not None else None,
            "note": "electrical class is unknown to software; confirm the adapter physically",
        }

    async def safe_state(self) -> dict[str, Any]:
        """Receive-only, and nothing asserted on the line.

        Deliberately does *not* open or close the port: opening one would move
        DTR/RTS on a DUT that nobody asked us to touch, and closing an open one
        would drop those lines mid-test.  Safe here means "we are not driving
        the line", which is the state the driver is already in between writes.
        """
        port = self._port
        changed = False
        if port is not None:
            try:
                if getattr(port, "break_condition", False):
                    port.break_condition = False
                    changed = True
            except Exception as exc:  # noqa: BLE001 - a port that cannot clear break is reported, not raised
                _log.warning(
                    "could not clear break condition",
                    extra={"device": self.device_id, "error": str(exc)},
                )
        return {
            "device": self.device_id,
            "applied": True,
            "changed": changed,
            "state": "receive only; no transmit in progress",
            "port_open": port is not None,
        }

    # -- port plumbing -----------------------------------------------------

    async def _ensure_open(self) -> bool:
        """Open the port if needed.  Returns True when this call opened it."""
        if self._port is not None and self._read_error is None:
            return False
        async with self._open_lock:
            if self._port is not None and self._read_error is None:
                return False
            if self._port is not None:
                # A previous read failed; drop the stale handle before retrying
                # so we never hand out a file descriptor the kernel has given up on.
                await self._close()
            port = await self._open()
            self._port = port
            self._read_error = None
            self._stop = asyncio.Event()
            self._reader = asyncio.create_task(
                self._reader_loop(port), name=f"serial-reader:{self.device_id}"
            )
            self._set_state(ConnectionState.READY)
            _log.info(
                "serial port opened",
                extra={
                    "device": self.device_id,
                    "path": self.path,
                    "baudrate": self._settings.baudrate,
                    "framing": self._settings.framing,
                },
            )
            return True

    async def _open(self) -> Any:
        serial = _pyserial()
        settings = self._settings
        port = serial.Serial()
        port.port = self.path
        port.baudrate = settings.baudrate
        port.bytesize = settings.bytesize
        port.parity = settings.parity
        port.stopbits = settings.stopbits
        port.rtscts = settings.rtscts
        port.xonxoff = settings.xonxoff
        port.timeout = _READ_TIMEOUT_S
        port.write_timeout = _WRITE_TIMEOUT_S
        # pyserial applies these at open, before the DUT can see them.  We
        # arrive on the line asserting nothing.
        port.dtr = False
        if not settings.rtscts:
            port.rts = False
        try:
            # pyserial's open() flushes the input buffer.  That is the right
            # call: anything the kernel buffered while FieldDeck was not
            # listening has no trustworthy arrival time, and a capture that
            # stamps stale bytes with "now" is worse than one that starts clean.
            await asyncio.to_thread(port.open)
        # Any failure to open is classified into a typed FieldDeck error below.
        except Exception as exc:
            raise self._open_error(exc) from exc
        return port

    def _open_error(self, exc: BaseException) -> FieldDeckError:
        """Turn an OS-level open failure into something an operator can act on."""
        code = getattr(exc, "errno", None)
        details = {"device_id": self.device_id, "path": self.path, "errno": code}
        if isinstance(exc, FileNotFoundError) or code in (
            errno_module.ENOENT,
            errno_module.ENODEV,
            errno_module.ENXIO,
        ):
            return DeviceDisconnected(
                f"{self.path} is not present; the adapter looks unplugged",
                details=details,
                preserved="nothing was opened and no bytes were sent",
            )
        if isinstance(exc, PermissionError) or code == errno_module.EACCES:
            return TransportError(
                f"no permission to open {self.path}; add the account running instrumentd "
                "to the 'dialout' group (sudo usermod -aG dialout <user>) and log in again",
                details=details,
                preserved="nothing was opened and no bytes were sent",
            )
        if code in (errno_module.EBUSY, errno_module.EAGAIN):
            return DeviceBusy(
                f"{self.path} is held by another process (screen, minicom, ModemManager?)",
                details=details,
                preserved="nothing was opened and no bytes were sent",
            )
        return TransportError(
            f"cannot open {self.path}: {exc}",
            details={**details, "type": type(exc).__name__},
            preserved="nothing was opened and no bytes were sent",
        )

    async def _close(self) -> None:
        await self._stop_reader()
        port, self._port = self._port, None
        if port is None:
            return
        # Closing drops DTR/RTS on most adapters.  It happens on device removal
        # and daemon shutdown only, never as a side effect of an action.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(port.close)

    async def _stop_reader(self) -> None:
        """Stop the reader cooperatively, so no thread is inside read()."""
        task, self._reader = self._reader, None
        self._stop.set()
        port = self._port
        if port is not None:
            # Wakes a thread blocked in read() immediately instead of waiting
            # out its timeout.
            with contextlib.suppress(Exception):
                port.cancel_read()
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except (TimeoutError, asyncio.CancelledError):
            # A wedged USB adapter can leave a thread stuck in read().  Give up
            # waiting, but say so: the file descriptor is closed underneath it.
            task.cancel()
            _log.warning(
                "serial reader did not stop promptly; closing anyway",
                extra={"device": self.device_id, "path": self.path},
            )

    async def _reader_loop(self, port: Any) -> None:
        """Drain the port into every subscriber.

        Runs for the lifetime of the open port.  With no subscribers the bytes
        are discarded rather than buffered — that keeps the kernel FIFO from
        overflowing and guarantees that a monitor which starts later sees live
        bytes with trustworthy arrival times, not a backlog stamped "now".
        """
        while not self._stop.is_set():
            try:
                stamp, data = await asyncio.to_thread(self._blocking_read, port)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any read failure ends the stream; subscribers are told
                if not self._stop.is_set():
                    self._fail(exc)
                return
            if not data:
                continue
            self._rx_bytes += len(data)
            self._publish(stamp, data)

    def _blocking_read(self, port: Any) -> tuple[int, bytes]:
        """One bounded read, in a worker thread.

        Blocks for one byte, timestamps the moment it arrives, then sweeps up
        whatever else is already waiting so a burst becomes one chunk with one
        honest arrival time rather than a byte-per-timestamp fiction.
        """
        data: bytes = port.read(1)
        stamp = monotonic_ns()
        waiting = port.in_waiting
        if waiting:
            data += port.read(min(waiting, _MAX_CHUNK_BYTES))
        return stamp, data

    def _publish(self, stamp: int, data: bytes) -> None:
        for sub in self._subscribers:
            if sub.buffered_bytes + len(data) > _MAX_BUFFERED_BYTES:
                sub.dropped_bytes += len(data)
                sub.dropped_chunks += 1
                continue
            sub.buffered_bytes += len(data)
            sub.queue.put_nowait((stamp, data))

    def _fail(self, exc: BaseException) -> None:
        self._read_error = exc
        self._set_state(ConnectionState.FAULT)
        _log.warning(
            "serial read failed",
            extra={"device": self.device_id, "path": self.path, "error": str(exc)},
        )

    def _modem_lines(self, port: Any) -> dict[str, bool | None]:
        """Input-only modem lines.  Reading them puts nothing on the wire."""
        lines: dict[str, bool | None] = {}
        for name in ("cts", "dsr", "ri", "cd"):
            try:
                lines[name] = bool(getattr(port, name))
            except Exception:  # noqa: BLE001 - a port with no modem lines is normal, not a fault
                lines[name] = None
        return lines

    @contextlib.contextmanager
    def _subscribe(self) -> Iterator[_Subscriber]:
        sub = _Subscriber()
        self._subscribers.add(sub)
        try:
            yield sub
        finally:
            self._subscribers.discard(sub)

    # -- receive -----------------------------------------------------------

    async def _receive(
        self,
        ctx: ActionContext,
        params: SerialMonitorParams,
        sink: Callable[[int, bytes], None],
    ) -> dict[str, Any]:
        """Feed received bytes to ``sink`` until time, budget or cancel says stop.

        Raises :class:`DeviceDisconnected` if the port dies mid-stream, but only
        after every byte already received has been handed to the sink, so the
        caller can finish writing before it reports the failure.
        """
        opened = await self._ensure_open()
        start = monotonic_ns()
        deadline = start + int(params.duration_s * 1e9)
        total = 0
        chunks = 0
        discarded = 0
        cancelled = False
        stopped_early: str | None = None

        with self._subscribe() as sub:
            while True:
                if ctx.cancelled:
                    cancelled = True
                    break
                now = monotonic_ns()
                if now >= deadline:
                    break
                if total >= params.max_bytes:
                    stopped_early = "max_bytes reached"
                    break
                remaining = ctx.remaining_s()
                if remaining is not None and remaining <= _DEADLINE_MARGIN_S:
                    stopped_early = "action deadline reached"
                    break
                wait_s = max(min(_POLL_S, (deadline - now) / 1e9), 0.001)
                try:
                    stamp, data = await asyncio.wait_for(sub.queue.get(), wait_s)
                except TimeoutError:
                    # Queue empty: the only moment it is safe to conclude that
                    # a failed reader has no more bytes for us.
                    if self._read_error is not None:
                        raise DeviceDisconnected(
                            f"{self.path} stopped delivering bytes: {self._read_error}",
                            details={
                                "device_id": self.device_id,
                                "path": self.path,
                                "bytes": total,
                                "chunks": chunks,
                            },
                        ) from self._read_error
                    continue
                sub.buffered_bytes -= len(data)
                room = params.max_bytes - total
                if len(data) > room:
                    # Cut exactly at the operator's budget; the kept bytes stay
                    # byte-exact and the loss is reported rather than hidden.
                    discarded += len(data) - room
                    data = data[:room]
                sink(stamp, data)
                total += len(data)
                chunks += 1

            dropped_bytes = sub.dropped_bytes
            dropped_chunks = sub.dropped_chunks

        if dropped_bytes:
            self._report_overflow(ctx, dropped_bytes, dropped_chunks)

        return {
            "bytes": total,
            "chunks_received": chunks,
            "duration_s": round((monotonic_ns() - start) / 1e9, 3),
            "cancelled": cancelled,
            "stopped_early": stopped_early,
            "opened_port": opened,
            "dropped_bytes": dropped_bytes,
            "dropped_chunks": dropped_chunks,
            "discarded_over_max_bytes": discarded,
        }

    def _report_overflow(self, ctx: ActionContext, dropped_bytes: int, dropped_chunks: int) -> None:
        """Lost bytes are an event, not a footnote in a return value."""
        ctx.emit(
            new_event(
                EventType.CAPTURE_OVERFLOW,
                source=ctx.source,
                severity=EventSeverity.WARNING,
                session_id=ctx.session_id,
                device_id=self.device_id,
                request_id=ctx.request_id,
                message=(
                    f"{dropped_bytes} bytes dropped on {self.path}: the consumer fell "
                    f"more than {_MAX_BUFFERED_BYTES // 1024} KiB behind the port"
                ),
                payload={"dropped_bytes": dropped_bytes, "dropped_chunks": dropped_chunks},
            )
        )

    # -- actions -----------------------------------------------------------

    @action(
        "serial.status",
        permission=PermissionLevel.PASSIVE,
        params=DeviceParams,
        state_changing=False,
        description="Port configuration and byte counters.",
        allowed_during_estop=True,
    )
    async def serial_status(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        return await self.status()

    @action(
        "serial.configure",
        permission=PermissionLevel.PASSIVE,
        params=SerialConfigureParams,
        state_changing=False,
        description="Set local port framing. Does not transmit to the DUT.",
    )
    async def serial_configure(
        self, ctx: ActionContext, params: SerialConfigureParams
    ) -> dict[str, Any]:
        """Changes this end of the link only — no bytes reach the DUT.

        A closed port is only reconfigured on paper: opening one to apply a baud
        rate would move DTR/RTS on a DUT nobody asked us to touch.  An open port
        is reconfigured atomically-or-not-at-all — a half-applied framing (new
        baud, old parity) is a port that lies about what it is receiving.
        """
        desired = _PortSettings(
            baudrate=params.baudrate,
            bytesize=params.bytesize,
            parity=params.parity,
            stopbits=params.stopbits,
            rtscts=params.rtscts,
            xonxoff=params.xonxoff,
        )
        if self._port is not None:
            await self._apply_live(desired)
        self._settings = desired
        if params.electrical is not None:
            # Operator-supplied knowledge, the only source there is.
            self._descriptor.metadata["electrical"] = params.electrical
        self._descriptor.metadata["baudrate"] = desired.baudrate
        self._descriptor.metadata["framing"] = desired.framing
        self._descriptor.metadata["flow_control"] = desired.flow_control
        return await self.status()

    async def _apply_live(self, desired: _PortSettings) -> None:
        """Reconfigure an open port, rolling back if the adapter refuses.

        The reader is stopped first: driving tcsetattr under a thread that is
        blocked in read() is a race nobody should have to debug at 2am.  Bytes
        in flight across a framing change are lost either way — the old framing
        no longer describes them.
        """
        await self._stop_reader()
        port = self._port
        previous = port.get_settings()
        wanted = {
            "baudrate": desired.baudrate,
            "bytesize": desired.bytesize,
            "parity": desired.parity,
            "stopbits": desired.stopbits,
            "rtscts": desired.rtscts,
            "xonxoff": desired.xonxoff,
        }
        try:
            # apply_settings touches only what actually changes, so an adapter
            # is never asked to re-accept a setting it is already using.
            port.apply_settings(wanted)
        # Drivers reject combinations they cannot do (7 data bits, exotic
        # baud rates).  Put the port back the way we found it and say so.
        except Exception as exc:
            with contextlib.suppress(Exception):
                port.apply_settings(previous)
            raise TransportError(
                f"{self.path} rejected {desired.baudrate} {desired.framing} "
                f"(flow control {desired.flow_control}): {exc}",
                details={
                    "device_id": self.device_id,
                    "path": self.path,
                    "requested": wanted,
                    "restored": {key: previous[key] for key in wanted},
                },
                preserved="the port is still open at its previous settings; no bytes were sent",
            ) from exc
        finally:
            self._stop = asyncio.Event()
            self._reader = asyncio.create_task(
                self._reader_loop(port), name=f"serial-reader:{self.device_id}"
            )

    @action(
        "serial.monitor",
        permission=PermissionLevel.PASSIVE,
        params=SerialMonitorParams,
        state_changing=False,
        description="Receive bytes without transmitting anything.",
        cancelable=True,
        timeout_s=3600.0,
    )
    async def serial_monitor(
        self, ctx: ActionContext, params: SerialMonitorParams
    ) -> dict[str, Any]:
        """Receive only.  Nothing is written to the port for the whole run."""
        chunks: list[dict[str, Any]] = []

        def sink(stamp: int, data: bytes) -> None:
            chunks.append({"monotonic_ns": stamp, "hex": data.hex(), "len": len(data)})

        try:
            stats = await self._receive(ctx, params, sink)
        except DeviceDisconnected as exc:
            if exc.preserved is not None:
                # The port never opened; that error already states what survived.
                raise
            received = sum(int(chunk["len"]) for chunk in chunks)
            raise DeviceDisconnected(
                exc.message,
                details=exc.details,
                preserved=(
                    f"{received} bytes arrived before the port vanished but were held in "
                    "memory only; use serial.capture inside a session to keep bytes on disk"
                ),
            ) from exc
        return {
            "device": self.device_id,
            "path": self.path,
            "baudrate": self._settings.baudrate,
            "framing": self._settings.framing,
            "electrical": self._descriptor.metadata["electrical"],
            "chunks": chunks,
            **stats,
        }

    @action(
        "serial.capture",
        permission=PermissionLevel.PASSIVE,
        params=SerialCaptureParams,
        state_changing=False,
        description="Record the raw byte stream into the session, byte-exact.",
        cancelable=True,
        timeout_s=3600.0,
    )
    async def serial_capture(
        self, ctx: ActionContext, params: SerialCaptureParams
    ) -> dict[str, Any]:
        """Writes the bytes verbatim, plus a sidecar index of arrival times.

        Bytes go to disk as they arrive rather than at the end, so an unplugged
        adapter, a cancelled action or an hour-long run all leave a complete
        file behind instead of a lost buffer.
        """
        if ctx.recorder is None:
            monitor = await self.serial_monitor(ctx, params)
            return {**monitor, "artifact": None, "warning": "no active session; bytes not saved"}

        # Open before creating any files: a port that refuses to open should
        # not leave an empty artifact behind in the session.
        opened = await self._ensure_open()
        recorder = ctx.recorder
        raw_path = recorder.capture_path("serial", params.label, ".bin")
        index_path = raw_path.with_suffix(".idx.jsonl")
        preview: list[dict[str, Any]] = []
        offset = 0
        stats: dict[str, Any] = {}
        failure: BaseException | None = None

        with raw_path.open("wb") as raw, index_path.open("w", encoding="ascii") as index:

            def sink(stamp: int, data: bytes) -> None:
                nonlocal offset
                raw.write(data)
                index.write(f'{{"offset":{offset},"len":{len(data)},"monotonic_ns":{stamp}}}\n')
                if len(preview) < _PREVIEW_CHUNKS:
                    preview.append({"monotonic_ns": stamp, "hex": data.hex(), "len": len(data)})
                offset += len(data)

            try:
                stats = await self._receive(ctx, params, sink)
            except DeviceDisconnected as exc:
                failure = exc
            except asyncio.CancelledError as exc:
                # The dispatcher cancels cancelable actions.  Fall through so
                # the files close and the artifact is registered, then re-raise.
                failure = exc
        stats["opened_port"] = bool(stats.get("opened_port")) or opened

        # Files are closed here, so sizes and hashes describe complete files.
        artifact = recorder.add_artifact(
            raw_path,
            kind="serial",
            media_type="application/octet-stream",
            device_id=self.device_id,
            raw=True,
            metadata={
                "baudrate": self._settings.baudrate,
                "framing": self._settings.framing,
                "electrical": self._descriptor.metadata["electrical"],
                "bytes": offset,
            },
        )
        index_artifact = recorder.add_artifact(
            index_path,
            kind="serial",
            media_type="application/x-ndjson",
            device_id=self.device_id,
            raw=False,
            source_artifact_ids=[artifact.artifact_id],
            producer="fielddeck.transports.serial_port",
            producer_version=__version__,
            producer_config={"description": "byte offset to arrival time index"},
        )

        if failure is not None:
            preserved = (
                f"{offset} bytes are on disk at {artifact.relative_path} "
                f"(artifact {artifact.artifact_id}) with its arrival-time index "
                f"{index_artifact.relative_path}"
            )
            if isinstance(failure, asyncio.CancelledError):
                raise failure
            raise DeviceDisconnected(
                failure.message if isinstance(failure, FieldDeckError) else str(failure),
                details=failure.details if isinstance(failure, FieldDeckError) else {},
                preserved=preserved,
            ) from failure

        return {
            "device": self.device_id,
            "path": self.path,
            "baudrate": self._settings.baudrate,
            "framing": self._settings.framing,
            "electrical": self._descriptor.metadata["electrical"],
            "chunks": preview,
            "truncated_in_result": int(stats.get("chunks_received", 0)) > len(preview),
            "artifact": artifact.model_dump(mode="json"),
            "index_artifact": index_artifact.model_dump(mode="json"),
            **stats,
        }

    @action(
        "serial.send",
        permission=PermissionLevel.CONTROL,
        params=SerialSendParams,
        state_changing=True,
        description="Transmit bytes to the device.",
        safe_state_note="Transmission stops; the port stays open for receive.",
    )
    async def serial_send(self, ctx: ActionContext, params: SerialSendParams) -> dict[str, Any]:
        """Requires CONTROL: these bytes reach a real DUT."""
        if params.hex is None and params.text is None:
            raise InvalidRequest(
                "give either hex or text to send",
                preserved="nothing was transmitted",
            )
        if params.hex is not None and params.text is not None:
            raise InvalidRequest(
                "give hex or text, not both",
                preserved="nothing was transmitted",
            )
        payload = (
            bytes.fromhex(params.hex)
            if params.hex is not None
            else (params.text or "").encode("utf-8")
        )
        if params.append_newline:
            payload += b"\r\n"

        await self._ensure_open()
        port = self._port
        try:
            written = await asyncio.to_thread(port.write, payload)
        # Classified below into timeout, disconnect or generic transport failure.
        except Exception as exc:
            raise self._write_error(exc, len(payload)) from exc
        sent = int(written) if written is not None else len(payload)
        self._tx_bytes += sent
        return {
            "device": self.device_id,
            "sent_bytes": sent,
            "hex": payload.hex().upper(),
            "monotonic_ns": monotonic_ns(),
            "note": (
                "on a half-duplex RS-485 link these bytes may reappear in the receive "
                "stream; that is the transceiver echoing, not the DUT replying"
            ),
        }

    @action(
        "serial.break",
        permission=PermissionLevel.CONTROL,
        params=SerialBreakParams,
        state_changing=True,
        description="Hold the line in a break condition for a set time.",
        safe_state_note="The break is released; the port stays open for receive.",
        timeout_s=15.0,
    )
    async def serial_break(self, ctx: ActionContext, params: SerialBreakParams) -> dict[str, Any]:
        """Requires CONTROL: a break is a deliberate framing violation.

        Bootloaders and some protocol stacks treat a break as a wake, an abort
        or a reset, so this is as much of a signal to the DUT as any byte.
        """
        await self._ensure_open()
        port = self._port
        duration_s = params.duration_ms / 1000.0
        try:
            await asyncio.to_thread(port.send_break, duration_s)
        # Classified below into timeout, disconnect or generic transport failure.
        except Exception as exc:
            raise self._write_error(exc, 0) from exc
        return {
            "device": self.device_id,
            "break_ms": params.duration_ms,
            "monotonic_ns": monotonic_ns(),
        }

    def _write_error(self, exc: BaseException, attempted: int) -> FieldDeckError:
        """Classify a transmit failure, saying what may already have gone out."""
        details = {
            "device_id": self.device_id,
            "path": self.path,
            "attempted_bytes": attempted,
            "type": type(exc).__name__,
        }
        if type(exc).__name__ == "SerialTimeoutException":
            return ActionTimeout(
                f"{self.path} did not accept the write within {_WRITE_TIMEOUT_S:g}s; "
                "with rtscts enabled this usually means CTS is never asserted",
                details=details,
                preserved=(
                    "the port stays open for receive; an unknown prefix of the payload "
                    "may already have reached the DUT"
                ),
            )
        code = getattr(exc, "errno", None)
        if isinstance(exc, OSError) and code in (
            errno_module.ENODEV,
            errno_module.ENXIO,
            errno_module.EIO,
        ):
            return DeviceDisconnected(
                f"{self.path} disappeared during transmit: {exc}",
                details={**details, "errno": code},
                preserved="an unknown prefix of the payload may already have reached the DUT",
            )
        return TransportError(
            f"transmit on {self.path} failed: {exc}",
            details={**details, "errno": code},
            preserved="an unknown prefix of the payload may already have reached the DUT",
        )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _display_name(entry: dict[str, Any]) -> str:
    parts = [entry.get("vendor"), entry.get("product")]
    label = " ".join(str(part) for part in parts if part).strip()
    return label or Path(str(entry["path"])).name


def discover_serial_drivers(config: FieldDeckConfig) -> list[Driver]:
    """Enumerate serial ports and wrap each in a driver.

    Enumeration reads sysfs and udev symlinks only: no port is opened, so
    discovery cannot reset a board or interrupt somebody else's session.
    Without pyserial installed there is nothing that could drive a port, so the
    provider returns nothing and the daemon logs why.

    ``config`` is part of the discovery-hook contract.  Nothing in
    ``fielddeck.yaml`` describes a specific port's framing or electrical class
    today, and inventing one from the presets would be exactly the kind of
    guess this transport exists to avoid.
    """
    try:
        _pyserial()
    except UnsupportedCapability as exc:
        _log.info(
            "pyserial missing; serial ports are enumerable but not drivable",
            extra={"reason": exc.message},
        )
        return []

    drivers: list[Driver] = []
    for entry in list_serial_ports():
        drivers.append(
            SerialDriver(
                device_id=str(entry["id"]),
                path=str(entry["path"]),
                display_name=_display_name(entry),
                vendor=entry.get("vendor"),
                product=entry.get("product"),
                serial_number=entry.get("serial"),
                stable_id=bool(entry.get("stable_id", False)),
                by_id=entry.get("by_id"),
            )
        )
    return drivers
