"""SocketCAN, on real hardware.

CAN is the most dangerous bus FieldDeck attaches to and the danger is not
obvious from the software side.  A single 8-byte frame can move an actuator,
and the act of *listening* is only free if the controller is configured not to
acknowledge.  Two rules therefore shape this module:

**Nothing transmits unless an authorized CONTROL action says so.**  The driver
holds a TX lock that starts closed, opens only inside ``can.send``, and is
closed again by :meth:`SocketCanDriver.safe_state`.  Receive paths open a
socket that is never handed a frame to send, and the TX socket exists only for
the duration of one send.

**The link is never reconfigured behind the operator's back.**  Bitrate and
listen-only ctrlmode live on the netdev, and changing either requires taking
the interface down — which on a live machine means an outage nobody asked for.
FieldDeck reads those settings (sysfs, and ``ip -details`` for what sysfs does
not expose) and reports them; it never writes them.  There is deliberately no
``can.configure`` and no bitrate autodetection: every autodetect scheme in the
wild either cycles the link through candidate bitrates or transmits to provoke
an ACK, and transmitting into a vehicle bus at the wrong bitrate is how a
diagnostic tool becomes an incident.  What this driver offers instead is
passive evidence — the ``bitrate_evidence`` block in ``can.stats``: error
frames with no valid traffic mean the configured bitrate is wrong, and that
conclusion costs nothing.

Risky edges worth knowing at 2am:

* ``python-can``'s ``recv()`` blocks.  Every receive runs on a reader thread
  feeding a **bounded** queue; a bus faster than the drain loop drops frames,
  counts them, and emits ``CAPTURE_OVERFLOW`` rather than growing until the Pi
  runs out of memory.
* Frame timestamps come from the kernel on ``CLOCK_REALTIME``.  They are
  projected onto FieldDeck's monotonic axis through an anchor taken when the
  capture opened; a wall-clock step mid-capture skews that projection, so the
  raw kernel value travels with every frame as ``utc_ns``.
* Raw capture files are candump text and are never rewritten.  ``can.decode``
  reads one and writes a *separate* CSV that names its source capture, the DBC
  it used (by hash) and the cantools version that produced it.
"""

from __future__ import annotations

import asyncio
import csv
import json
import queue
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import Field, field_validator, model_validator

from fielddeck.capture.storage import sha256_file
from fielddeck.common.config import FieldDeckConfig
from fielddeck.common.errors import (
    CaptureError,
    DeviceDisconnected,
    FieldDeckError,
    InvalidRequest,
    TransportError,
    UnsupportedCapability,
)
from fielddeck.common.events import EventSeverity, EventType, new_event
from fielddeck.common.ids import device_id as compose_device_id
from fielddeck.common.logging import get_logger
from fielddeck.common.models import (
    CaptureArtifact,
    ConnectionState,
    DeviceCapability,
    DeviceDescriptor,
    DeviceRole,
    PermissionLevel,
    TransportKind,
)
from fielddeck.common.process import have_tool, run_tool
from fielddeck.common.timebase import TimeAnchor, Timestamp, monotonic_ns
from fielddeck.discovery.linux import list_can_interfaces
from fielddeck.drivers.base import ActionContext, DeviceParams, Driver, action

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fielddeck.capture.recorder import SessionRecorder

__all__ = ["SocketCanDriver", "discover_can_drivers"]

_log = get_logger("fielddeck.transports.socketcan")

_SYS_NET = Path("/sys/class/net")

#: How long the reader thread blocks in ``recv()`` before checking whether it
#: has been asked to stop.  Short enough that tearing a capture down never
#: stalls the event loop for long, long enough to cost nothing at idle.
_RECV_POLL_S = 0.05

#: How often the async side drains the reader queue.  20 Hz keeps the loop
#: responsive while still batching thousands of frames per wake-up.
_DRAIN_INTERVAL_S = 0.05

#: Ceiling on frames buffered between the reader thread and the drain loop.
#: Bounded on purpose: a saturated 1 Mbit/s bus is ~15k frames/s and an
#: unbounded queue would only defer the failure until the Pi is out of memory.
_MAX_QUEUE_FRAMES = 65_536

#: Frames echoed back in a capture result; the capture file holds all of them.
_RESULT_SAMPLE_FRAMES = 50

#: A timestamp below this (2001-09-09) is not a wall clock, so it came from a
#: backend without kernel timestamping rather than from ``SO_TIMESTAMPNS``.
_EPOCH_SANITY_S = 1_000_000_000.0

_CANDUMP_MEDIA_TYPE = "text/vnd.candump"

#: ``CAN_ERR_FLAG`` from <linux/can.h>.  python-can strips it off
#: ``arbitration_id``; candump keeps it in the printed id, so it is restored
#: when writing a log that can-utils and Wireshark have to read back.
_CAN_ERR_FLAG = 0x20000000

#: ``IFF_UP``/``IFF_RUNNING`` from <linux/if.h>.  ``operstate`` is not usable
#: as the up/down answer for CAN: vcan reports "unknown" while working fine.
_IFF_UP = 0x1
_IFF_RUNNING = 0x40


# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------


def _load_can() -> Any | None:
    """python-can if this install has it, else None.

    Imported lazily so the daemon starts on a machine without the CAN extra:
    a missing library degrades one transport, never the console.
    """
    try:
        import can
    except ImportError:
        return None
    return can


def _require_can() -> Any:
    module = _load_can()
    if module is None:
        raise UnsupportedCapability(
            "python-can is not installed; install it with: pip install 'fielddeck[can]'",
            details={"package": "python-can", "transport": "socketcan"},
            preserved="nothing was sent to or read from the bus",
        )
    return module


def _load_cantools() -> Any | None:
    try:
        import cantools
    except ImportError:
        return None
    return cantools


def _can_version() -> str | None:
    module = _load_can()
    return getattr(module, "__version__", None) if module is not None else None


# ---------------------------------------------------------------------------
# Passive link facts
# ---------------------------------------------------------------------------


def _read_sysfs(*parts: str) -> str | None:
    try:
        return _SYS_NET.joinpath(*parts).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _read_int(*parts: str, base: int = 10) -> int | None:
    raw = _read_sysfs(*parts)
    if raw is None:
        return None
    try:
        return int(raw, base)
    except ValueError:
        return None


def _link_facts(interface: str) -> dict[str, Any]:
    """Everything sysfs will say about the interface, reading only.

    sysfs exposes no CAN controller state and no ctrlmode; those come from
    netlink via :meth:`SocketCanDriver._link_details`.  What is here is always
    available, never blocks, and never touches the bus.
    """
    present = _SYS_NET.joinpath(interface).exists()
    flags = _read_int(interface, "flags", base=16) or 0
    counters = {
        name: _read_int(interface, "statistics", name)
        for name in (
            "rx_packets",
            "tx_packets",
            "rx_errors",
            "tx_errors",
            "rx_dropped",
            "tx_dropped",
            "rx_over_errors",
            "rx_frame_errors",
        )
    }
    mtu = _read_int(interface, "mtu") or 0
    return {
        "present": present,
        "operstate": _read_sysfs(interface, "operstate") or "unknown",
        "up": present and bool(flags & _IFF_UP),
        "running": bool(flags & _IFF_RUNNING),
        "bitrate": _read_int(interface, "can_bittiming", "bitrate"),
        "sample_point": _read_int(interface, "can_bittiming", "sample_point"),
        "mtu": mtu,
        "fd_capable": mtu >= 72,
        "counters": counters,
        "bus_errors": (counters["rx_errors"] or 0) + (counters["tx_errors"] or 0),
    }


def _interface_backing(interface: str) -> tuple[str | None, bool]:
    """Where the netdev comes from, and whether its name is plug-order luck.

    ``can0`` on an MCP2515 hat is the same physical port after every reboot.
    ``can0`` on a USB adapter is whichever adapter enumerated first, so that
    id is reported as unstable rather than silently trusted by a recipe.
    """
    try:
        target = _SYS_NET.joinpath(interface, "device").resolve()
    except OSError:  # pragma: no cover - racing a hot-unplug
        return None, False
    if not target.exists():
        return None, False
    text = str(target)
    return text, "/usb" in text


def _controller_summary(details: dict[str, Any] | None) -> dict[str, Any]:
    """Pull the CAN controller facts out of ``ip -details -json`` output."""
    if not details:
        return {"source": None, "listen_only": None, "state": None}
    info = details.get("linkinfo") or {}
    data = info.get("info_data") or {}
    ctrlmode = [str(flag).lower() for flag in (data.get("ctrlmode") or [])]
    bittiming = data.get("bittiming") or {}
    return {
        "source": "ip -details link",
        "kind": info.get("info_kind"),
        # None, not False, when there is nothing to read: "unknown" and "not
        # listen-only" are very different claims to make about a live bus.
        "listen_only": ("listen-only" in ctrlmode) if data else None,
        "ctrlmode": ctrlmode,
        "state": data.get("state"),
        "bitrate": bittiming.get("bitrate"),
        "sample_point": bittiming.get("sample_point"),
        "berr_counter": data.get("berr_counter"),
        "restart_ms": data.get("restart_ms"),
    }


def _bitrate_evidence(
    *, bitrate: int | None, valid_frames: int, error_frames: int
) -> dict[str, Any]:
    """What observed traffic says about the configured bitrate.

    This is the whole of FieldDeck's bitrate story, on purpose.  Autodetection
    by transmitting to provoke an ACK, or by cycling the link through candidate
    bitrates, disturbs a bus that may be driving something.  Watching is free.
    """
    if valid_frames == 0 and error_frames > 0:
        verdict = "likely wrong: only error frames were seen"
    elif valid_frames == 0:
        verdict = "no evidence: the bus was silent"
    elif error_frames > valid_frames:
        verdict = "suspect: error frames outnumber valid frames"
    else:
        verdict = "consistent: valid frames were received"
    return {
        "configured_bitrate": bitrate,
        "valid_frames": valid_frames,
        "error_frames": error_frames,
        "verdict": verdict,
        "method": "passive observation only; FieldDeck never transmits to detect a bitrate",
    }


# ---------------------------------------------------------------------------
# Receive plumbing
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _RxClock:
    """Projects kernel frame timestamps onto FieldDeck's monotonic axis."""

    anchor: TimeAnchor

    def project(self, timestamp: float) -> tuple[int, int]:
        """Return ``(monotonic_ns, utc_ns)`` for one received frame."""
        if timestamp >= _EPOCH_SANITY_S:
            utc = int(timestamp * 1e9)
            # The offset is fixed when the capture opens.  A wall-clock step
            # during the capture skews this projection, which is exactly why
            # the raw kernel value is kept rather than thrown away.
            return self.anchor.monotonic_ns + (utc - self.anchor.utc_ns), utc
        # Backends without kernel timestamping hand back 0 or something
        # relative.  Arrival time is then the only honest answer available.
        mono = monotonic_ns()
        return mono, self.anchor.utc_for(mono)


class _RxPump:
    """Bridges python-can's blocking ``recv()`` into asyncio.

    The reader thread owns the socket read; the event loop only ever drains a
    bounded queue.  When the bus outruns the drain loop the *newest* frames are
    dropped and counted — losing the tail of an overflow is recoverable, losing
    the beginning of the fault that caused it is not.
    """

    def __init__(self, bus: Any, *, capacity: int, name: str) -> None:
        self._bus = bus
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=capacity)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        #: Incremented only by the reader thread, read only by the loop thread.
        self.dropped = 0
        self.error: BaseException | None = None

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                message = self._bus.recv(timeout=_RECV_POLL_S)
            except Exception as exc:  # noqa: BLE001 - the reader records the fault and exits; the action reports it
                self.error = exc
                return
            if message is None:
                continue
            try:
                self._queue.put_nowait(message)
            except queue.Full:
                self.dropped += 1

    def drain(self) -> list[Any]:
        """Everything queued right now, without blocking."""
        out: list[Any] = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                return out

    def stop(self) -> None:
        """Stop the reader and wait for it to let go of the socket.

        Deliberately synchronous, including on the cancellation path: the
        socket is closed immediately after this returns, and a reader thread
        still selecting on a closed descriptor is a far worse problem than one
        drain interval of event-loop latency.
        """
        self._stop.set()
        self._thread.join(timeout=2.0)


def _kernel_filters(id_filter: Sequence[int] | None) -> list[dict[str, int]] | None:
    """Translate an id filter into SocketCAN kernel filters.

    Filtering in the kernel keeps unwanted frames out of userspace entirely.
    It is an RX-side socket option and transmits nothing.
    """
    if not id_filter:
        return None
    return [
        {"can_id": can_id, "can_mask": 0x1FFFFFFF if can_id > 0x7FF else 0x7FF}
        for can_id in id_filter
    ]


def _frame_dict(message: Any, clock: _RxClock) -> dict[str, Any]:
    """One received frame, in FieldDeck terms."""
    monotonic, utc = clock.project(float(message.timestamp or 0.0))
    data = bytes(message.data or b"")
    return {
        "monotonic_ns": monotonic,
        "utc_ns": utc,
        "can_id": int(message.arbitration_id),
        "extended": bool(message.is_extended_id),
        "dlc": int(message.dlc or len(data)),
        "data": data.hex(),
        "error": bool(message.is_error_frame),
        "remote": bool(message.is_remote_frame),
        "fd": bool(getattr(message, "is_fd", False)),
        "brs": bool(getattr(message, "bitrate_switch", False)),
        "esi": bool(getattr(message, "error_state_indicator", False)),
        # False for frames this machine put on the bus (kernel loopback), so a
        # capture can distinguish what was heard from what FieldDeck said.
        # Everything on a vcan interface is locally generated.
        "rx": bool(getattr(message, "is_rx", True)),
        # Present so a real capture has the same shape as the simulated one.
        # A description needs a DBC, which is what can.decode is for.
        "description": None,
    }


def _frame_bits(frame: dict[str, Any]) -> int:
    """Nominal on-the-wire bits for one classic CAN frame, stuffing excluded.

    47 bits of overhead for a standard frame, 67 for extended, both including
    the 3-bit inter-frame space.  Bit stuffing adds up to ~20% more, so a bus
    load computed from this is a floor and is reported as one.
    """
    overhead = 67 if frame.get("extended") else 47
    return overhead + 8 * (len(frame.get("data") or "") // 2)


# ---------------------------------------------------------------------------
# candump text
# ---------------------------------------------------------------------------

_CANDUMP_LINE = re.compile(
    r"^\((?P<ts>\d+(?:\.\d+)?)\)\s+(?P<iface>\S+)\s+"
    r"(?P<can_id>[0-9A-Fa-f]+)(?P<sep>#{1,2})(?P<rest>\S*)\s*$"
)


@dataclass(slots=True)
class _LoggedFrame:
    utc_s: float
    interface: str
    can_id: int
    extended: bool
    data: bytes
    remote: bool
    error: bool
    fd: bool


def _candump_line(interface: str, utc_ns: int, frame: dict[str, Any]) -> str:
    """Render one frame the way ``candump -l`` would.

    The format is load-bearing: can-utils' ``canplayer`` and Wireshark both
    read it, so a FieldDeck capture stays useful to tools that have never
    heard of FieldDeck.
    """
    can_id = int(frame["can_id"])
    if frame.get("error"):
        identifier = f"{_CAN_ERR_FLAG | can_id:08X}"
    elif frame.get("extended"):
        identifier = f"{can_id:08X}"
    else:
        identifier = f"{can_id:03X}"

    if frame.get("remote"):
        body = f"#R{int(frame.get('dlc') or 0)}"
    elif frame.get("fd"):
        flags = (1 if frame.get("brs") else 0) | (2 if frame.get("esi") else 0)
        body = f"##{flags:X}{str(frame['data']).upper()}"
    else:
        body = f"#{str(frame['data']).upper()}"
    return f"({utc_ns / 1e9:.6f}) {interface} {identifier}{body}\n"


def _parse_candump_line(line: str) -> _LoggedFrame | None:
    """Parse one candump line, or None if it is not one."""
    match = _CANDUMP_LINE.match(line.strip())
    if match is None:
        return None
    raw_id = int(match["can_id"], 16)
    error = bool(raw_id & _CAN_ERR_FLAG)
    # candump prints 3 hex digits for 11-bit ids and 8 for everything else,
    # which is the only extended/standard signal the text format carries.
    extended = len(match["can_id"]) > 3 and not error
    rest = match["rest"]
    fd = match["sep"] == "##"
    remote = rest[:1] in {"R", "r"}

    if remote:
        data = b""
    else:
        try:
            data = bytes.fromhex(rest[1:] if fd else rest)
        except ValueError:
            return None

    return _LoggedFrame(
        utc_s=float(match["ts"]),
        interface=match["iface"],
        can_id=raw_id & 0x1FFFFFFF,
        extended=extended,
        data=data,
        remote=remote,
        error=error,
        fd=fd,
    )


# ---------------------------------------------------------------------------
# Action parameters
# ---------------------------------------------------------------------------


class CanListenParams(DeviceParams):
    duration_s: float = Field(default=2.0, gt=0, le=3600)
    max_frames: int = Field(default=2000, ge=1, le=200_000)
    id_filter: list[int] | None = None

    @field_validator("id_filter")
    @classmethod
    def _valid_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        for can_id in value:
            if not 0 <= can_id <= 0x1FFFFFFF:
                raise ValueError(f"{can_id} is not a CAN arbitration id")
        return value


class CanCaptureParams(CanListenParams):
    label: str = Field(default="capture", max_length=64)


class CanSendParams(DeviceParams):
    can_id: int = Field(ge=0, le=0x1FFFFFFF)
    data: str = Field(description="Payload as hex, e.g. '01 03 00 00'")
    extended: bool = False
    count: int = Field(default=1, ge=1, le=1000)

    @field_validator("data")
    @classmethod
    def _valid_hex(cls, value: str) -> str:
        cleaned = value.replace(" ", "").replace("0x", "")
        try:
            payload = bytes.fromhex(cleaned)
        except ValueError as exc:
            raise ValueError(f"data must be hex bytes, got {value!r}") from exc
        if len(payload) > 8:
            raise ValueError("classic CAN payload is at most 8 bytes")
        return cleaned


class CanStatsParams(DeviceParams):
    duration_s: float = Field(default=2.0, gt=0, le=60)


class CanDecodeParams(DeviceParams):
    dbc: str = Field(description="Path to a .dbc/.kcd/.sym database")
    artifact_id: str | None = Field(
        default=None, description="Capture artifact in the current session"
    )
    path: str | None = Field(
        default=None, description="Capture file, relative to the session directory"
    )
    label: str = Field(default="decoded", max_length=64)
    max_frames: int = Field(default=500_000, ge=1, le=20_000_000)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> CanDecodeParams:
        if bool(self.artifact_id) == bool(self.path):
            raise ValueError("give exactly one of artifact_id or path")
        return self


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class SocketCanDriver(Driver):
    """One SocketCAN interface, driven through python-can.

    The driver holds no long-lived socket.  Each receive action opens its own
    RAW socket — SocketCAN copies every frame to every bound socket, so
    concurrent captures never steal frames from each other — and each send
    opens one for exactly as long as the transmission takes.
    """

    kind = TransportKind.CAN

    def __init__(
        self,
        interface: str,
        *,
        bitrate: int | None = None,
        fd_capable: bool = False,
        virtual: bool = False,
        mtu: int = 16,
        ip_tool: str = "ip",
        bus_interface: str = "socketcan",
    ) -> None:
        backing, usb_backed = _interface_backing(interface)
        facts = _link_facts(interface)
        up = facts["up"]
        descriptor = DeviceDescriptor(
            id=compose_device_id("can", "socketcan", interface),
            kind=TransportKind.CAN,
            display_name=f"CAN {interface}" + (f" @ {bitrate // 1000}k" if bitrate else ""),
            path=f"/sys/class/net/{interface}",
            product="SocketCAN",
            roles=[DeviceRole.BUS],
            capabilities=[
                DeviceCapability.RX,
                DeviceCapability.TX,
                DeviceCapability.STREAM,
                DeviceCapability.DECODE,
                DeviceCapability.SAFE_STATE,
            ],
            state=ConnectionState.READY if up else ConnectionState.DISCOVERED,
            # A USB CAN adapter's interface name is assigned in plug order, so
            # can0 is not evidence that this is yesterday's adapter.
            stable_id=not usb_backed,
            simulated=False,
            warning=(
                None
                if up
                else (
                    f"{interface} is down; bring it up with: "
                    f"sudo ip link set {interface} up type can bitrate 500000"
                )
            ),
            metadata={
                "interface": interface,
                "bitrate": bitrate or facts["bitrate"],
                "fd_capable": fd_capable or facts["fd_capable"],
                "mtu": mtu or facts["mtu"],
                "virtual": virtual,
                "sysfs_device": backing,
                "usb_backed": usb_backed,
                # The software TX lock, not the controller ctrlmode.  can.status
                # reports the controller separately, because only one of the two
                # is something FieldDeck is entitled to change.
                "mode": "listen-only",
            },
        )
        super().__init__(descriptor)
        self.interface = interface
        self.bitrate = bitrate or facts["bitrate"]
        self.fd_capable = fd_capable or facts["fd_capable"]
        self.virtual = virtual
        self._ip_tool = ip_tool
        #: Which python-can backend to open.  Only SocketCAN is used in
        #: production; the seam exists so the frame path can be exercised
        #: against python-can's in-process virtual bus on a machine with no
        #: kernel CAN support.
        self._bus_interface = bus_interface
        self._tx_unlocked = False
        self._tx_count = 0
        self._details_cache: tuple[int, dict[str, Any] | None] | None = None

    # -- driver contract ---------------------------------------------------

    async def probe(self) -> bool:
        """Cheap check that the netdev is still there (USB adapters vanish)."""
        present = _SYS_NET.joinpath(self.interface).exists()
        if not present:
            self._set_state(ConnectionState.ABSENT)
        return present

    async def status(self) -> dict[str, Any]:
        facts = _link_facts(self.interface)
        controller = _controller_summary(await self._link_details())
        if not facts["present"]:
            self._set_state(ConnectionState.ABSENT)
        elif facts["up"] and self._descriptor.state in (
            ConnectionState.ABSENT,
            ConnectionState.DISCOVERED,
        ):
            self._set_state(ConnectionState.READY)

        self.bitrate = facts["bitrate"] or controller.get("bitrate") or self.bitrate
        return {
            "interface": self.interface,
            "bitrate": self.bitrate,
            "mode": "normal" if self._tx_unlocked else "listen-only",
            "state": str(self._descriptor.state),
            "link_up": facts["up"],
            "operstate": facts["operstate"],
            "tx_frames": self._tx_count,
            "bus_errors": facts["bus_errors"],
            "counters": facts["counters"],
            "mtu": facts["mtu"],
            "fd_capable": facts["fd_capable"],
            "virtual": self.virtual,
            # None means "the controller mode could not be read", which is not
            # the same claim as "the controller is not in listen-only".
            "controller_listen_only": controller.get("listen_only"),
            "controller_state": controller.get("state"),
            "berr_counter": controller.get("berr_counter"),
            "controller_source": controller.get("source"),
            "python_can": _can_version(),
            "note": (
                "FieldDeck never changes this interface's bitrate or ctrlmode; "
                "both require taking the link down"
            ),
            "warning": self._descriptor.warning,
        }

    async def safe_state(self) -> dict[str, Any]:
        """Lock transmission and return the driver to listen-only."""
        was_transmitting = self._tx_unlocked
        self._tx_unlocked = False
        self._descriptor.metadata["mode"] = "listen-only"
        return {
            "device": self.device_id,
            "applied": True,
            "changed": was_transmitting,
            "state": "listen-only, TX locked",
        }

    # -- link details ------------------------------------------------------

    async def _link_details(self, *, timeout_s: float = 3.0) -> dict[str, Any] | None:
        """Controller state and ctrlmode, which sysfs does not expose.

        ``ip -details -json link show`` reads netlink and changes nothing.  A
        missing or too-old iproute2 just means the controller fields read as
        unknown; it is never a reason to fail an action.  Cached briefly
        because the HMI polls status continuously.
        """
        now = monotonic_ns()
        if self._details_cache is not None and now - self._details_cache[0] < 1_000_000_000:
            return self._details_cache[1]

        details: dict[str, Any] | None = None
        if have_tool(self._ip_tool):
            result = await run_tool(
                self._ip_tool,
                ["-details", "-json", "link", "show", self.interface],
                timeout_s=timeout_s,
            )
            if result.ok:
                try:
                    payload = json.loads(result.stdout or "[]")
                except json.JSONDecodeError:
                    payload = []
                if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                    details = payload[0]
            else:
                _log.debug(
                    "ip link details unavailable",
                    extra={"interface": self.interface, "stderr": result.stderr[:200]},
                )
        self._details_cache = (now, details)
        return details

    # -- receive core ------------------------------------------------------

    def _open_bus(self, *, id_filter: Sequence[int] | None = None) -> Any:
        """Open a socket on the interface.  Opening puts nothing on the wire.

        Called from a worker thread: binding an ``AF_CAN`` socket is quick, but
        it is still a syscall that can block on a wedged driver.
        """
        can_module = _require_can()
        try:
            return can_module.Bus(
                interface=self._bus_interface,
                channel=self.interface,
                receive_own_messages=False,
                fd=self.fd_capable,
                can_filters=_kernel_filters(id_filter),
            )
        except (OSError, can_module.CanError) as exc:
            raise self._open_failure(exc) from exc

    def _open_failure(self, exc: BaseException) -> FieldDeckError:
        """Turn a python-can open failure into something an operator can act on."""
        facts = _link_facts(self.interface)
        if not facts["present"]:
            return DeviceDisconnected(
                f"{self.interface} no longer exists; the adapter was unplugged",
                details={"interface": self.interface, "error": str(exc)},
                preserved="nothing was read from or written to the bus",
            )
        hint = (
            ""
            if facts["up"]
            else (
                f"; the link is down — bring it up with: "
                f"sudo ip link set {self.interface} up type can bitrate 500000"
            )
        )
        return TransportError(
            f"cannot open {self.interface}: {exc}{hint}",
            details={
                "interface": self.interface,
                "operstate": facts["operstate"],
                "error": str(exc),
            },
            preserved="nothing was read from or written to the bus",
        )

    async def _receive(
        self,
        ctx: ActionContext,
        *,
        action_name: str,
        duration_s: float,
        max_frames: int,
        id_filter: list[int] | None = None,
        sink: Callable[[list[dict[str, Any]]], None] | None = None,
        collect: bool = True,
    ) -> dict[str, Any]:
        """Listen for frames.  No path through here transmits anything.

        ``sink`` receives each drained batch so a capture reaches disk while it
        is still running: a cancelled or timed-out capture must leave behind
        the frames it already heard, not an empty file.
        """
        bus = await asyncio.to_thread(self._open_bus, id_filter=id_filter)
        clock = _RxClock(TimeAnchor.capture())
        pump = _RxPump(
            bus,
            capacity=max(1024, min(max_frames, _MAX_QUEUE_FRAMES)),
            name=f"fielddeck-can-rx-{self.interface}",
        )
        wanted = set(id_filter) if id_filter else None

        frames: list[dict[str, Any]] = []
        sample: list[dict[str, Any]] = []
        counts = {"count": 0, "error_frames": 0, "local": 0}
        overflow_reported = False
        started = monotonic_ns()
        deadline = started + int(duration_s * 1e9)

        def absorb(batch: list[Any]) -> None:
            converted: list[dict[str, Any]] = []
            for message in batch:
                if counts["count"] >= max_frames:
                    break
                frame = _frame_dict(message, clock)
                # The kernel filter is a coarse mask; this makes the delivered
                # set exactly the ids the operator asked for.
                if wanted is not None and frame["can_id"] not in wanted:
                    continue
                counts["count"] += 1
                if frame["error"]:
                    counts["error_frames"] += 1
                if not frame["rx"]:
                    counts["local"] += 1
                converted.append(frame)
                if len(sample) < _RESULT_SAMPLE_FRAMES:
                    sample.append(frame)
            if collect:
                frames.extend(converted)
            if sink is not None and converted:
                sink(converted)

        pump.start()
        try:
            while counts["count"] < max_frames:
                now = monotonic_ns()
                if now >= deadline:
                    break
                await asyncio.sleep(min(_DRAIN_INTERVAL_S, (deadline - now) / 1e9))
                absorb(pump.drain())
                if pump.dropped and not overflow_reported:
                    overflow_reported = True
                    self._report_overflow(ctx, action_name, pump.dropped)
                if pump.error is not None or ctx.cancelled:
                    break
        finally:
            pump.stop()
            # Whatever the reader already pulled off the socket is evidence
            # that has been paid for; take it before the socket goes away.
            absorb(pump.drain())
            # Synchronous on purpose: an ``await`` here would be skipped when
            # the action is being cancelled, and that leaks the socket.
            bus.shutdown()

        if pump.error is not None:
            raise TransportError(
                f"receive failed on {self.interface}: {pump.error}",
                details={"interface": self.interface, "error": str(pump.error)},
                preserved=f"{counts['count']} frames received before the failure were kept",
            )

        return {
            "interface": self.interface,
            "frames": frames,
            "sample": sample,
            "count": counts["count"],
            "error_frames": counts["error_frames"],
            "locally_transmitted": counts["local"],
            "dropped": pump.dropped,
            "duration_s": round((monotonic_ns() - started) / 1e9, 3),
            "cancelled": ctx.cancelled,
            "mode": "normal" if self._tx_unlocked else "listen-only",
            "anchor": clock.anchor.as_dict(),
        }

    def _report_overflow(self, ctx: ActionContext, action_name: str, dropped: int) -> None:
        """Say so, once and loudly: silent frame loss invalidates the analysis."""
        ctx.emit(
            new_event(
                EventType.CAPTURE_OVERFLOW,
                source=ctx.source,
                severity=EventSeverity.WARNING,
                session_id=ctx.session_id,
                device_id=self.device_id,
                action=action_name,
                request_id=ctx.request_id,
                message=(
                    f"{self.interface} is delivering frames faster than they can be "
                    f"drained; {dropped} frames were dropped"
                ),
                payload={"interface": self.interface, "dropped": dropped},
            )
        )

    # -- actions -----------------------------------------------------------

    @action(
        "can.status",
        permission=PermissionLevel.PASSIVE,
        params=DeviceParams,
        state_changing=False,
        description="Interface configuration and error counters.",
        allowed_during_estop=True,
        timeout_s=15.0,
    )
    async def can_status(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        return await self.status()

    @action(
        "can.listen",
        permission=PermissionLevel.PASSIVE,
        params=CanListenParams,
        state_changing=False,
        description="Receive frames without transmitting anything.",
        cancelable=True,
        timeout_s=3600.0,
    )
    async def can_listen(self, ctx: ActionContext, params: CanListenParams) -> dict[str, Any]:
        """Passive receive: the socket is opened and never given a frame to send."""
        outcome = await self._receive(
            ctx,
            action_name="can.listen",
            duration_s=params.duration_s,
            max_frames=params.max_frames,
            id_filter=params.id_filter,
        )
        outcome.pop("sample", None)
        return outcome

    @action(
        "can.capture",
        permission=PermissionLevel.PASSIVE,
        params=CanCaptureParams,
        state_changing=False,
        description="Record frames to an immutable capture file in the session.",
        cancelable=True,
        timeout_s=3600.0,
    )
    async def can_capture(self, ctx: ActionContext, params: CanCaptureParams) -> dict[str, Any]:
        """Writes candump-format text, which can-utils and Wireshark both read."""
        if ctx.recorder is None:
            outcome = await self.can_listen(ctx, params)
            return {**outcome, "artifact": None, "warning": "no active session; frames not saved"}

        recorder = ctx.recorder
        path = recorder.capture_path("can", f"{self.interface}-{params.label}", ".log")
        ctx.emit(
            new_event(
                EventType.CAPTURE_STARTED,
                source=ctx.source,
                session_id=ctx.session_id,
                device_id=self.device_id,
                action="can.capture",
                request_id=ctx.request_id,
                message=f"capturing {self.interface} to {path.name}",
                payload={"path": str(path), "duration_s": params.duration_s},
            )
        )

        handle = path.open("w", encoding="ascii")
        written = 0
        artifact: CaptureArtifact | None = None

        def write(batch: list[dict[str, Any]]) -> None:
            nonlocal written
            for frame in batch:
                handle.write(_candump_line(self.interface, frame["utc_ns"], frame))
                written += 1
            # Flushed per batch rather than per frame: a capture killed by a
            # timeout or a pulled plug loses at most one drain interval.
            handle.flush()

        try:
            outcome = await self._receive(
                ctx,
                action_name="can.capture",
                duration_s=params.duration_s,
                max_frames=params.max_frames,
                id_filter=params.id_filter,
                sink=write,
                collect=False,
            )
        finally:
            handle.close()
            if written:
                # Registered even when the capture was cancelled or timed out:
                # an unregistered file is a capture the operator cannot find.
                artifact = recorder.add_artifact(
                    path,
                    kind="can",
                    media_type=_CANDUMP_MEDIA_TYPE,
                    device_id=self.device_id,
                    raw=True,
                    metadata={
                        "frames": written,
                        "bitrate": self.bitrate,
                        "interface": self.interface,
                        "format": "candump",
                    },
                )
                ctx.emit(
                    new_event(
                        EventType.ARTIFACT_ADDED,
                        source=ctx.source,
                        session_id=ctx.session_id,
                        device_id=self.device_id,
                        action="can.capture",
                        request_id=ctx.request_id,
                        message=f"{written} frames written to {artifact.relative_path}",
                        payload=artifact.model_dump(mode="json"),
                    )
                )
            else:
                # An empty file in the session is worse than no file: it looks
                # like evidence that the bus was quiet when in fact nothing ran.
                path.unlink(missing_ok=True)
            ctx.emit(
                new_event(
                    EventType.CAPTURE_STOPPED,
                    source=ctx.source,
                    session_id=ctx.session_id,
                    device_id=self.device_id,
                    action="can.capture",
                    request_id=ctx.request_id,
                    message=f"capture of {self.interface} finished",
                    payload={"frames": written, "path": str(path)},
                )
            )

        sample = outcome.pop("sample", [])
        return {
            **outcome,
            "frames": sample,
            "truncated_in_result": outcome["count"] > len(sample),
            "path": str(path) if artifact is not None else None,
            "artifact": artifact.model_dump(mode="json") if artifact is not None else None,
        }

    @action(
        "can.stats",
        permission=PermissionLevel.PASSIVE,
        params=CanStatsParams,
        state_changing=False,
        description="Per-arbitration-ID rate, period and jitter statistics.",
        timeout_s=120.0,
    )
    async def can_stats(self, ctx: ActionContext, params: CanStatsParams) -> dict[str, Any]:
        from fielddeck.analysis.timing import summarize_periods

        listen = await self._receive(
            ctx,
            action_name="can.stats",
            duration_s=params.duration_s,
            max_frames=200_000,
        )

        by_id: dict[int, list[int]] = {}
        last: dict[int, str] = {}
        extended: dict[int, bool] = {}
        bits = 0
        for frame in listen["frames"]:
            if frame["error"]:
                continue
            by_id.setdefault(frame["can_id"], []).append(frame["monotonic_ns"])
            last[frame["can_id"]] = frame["data"]
            extended[frame["can_id"]] = frame["extended"]
            bits += _frame_bits(frame)

        rows = []
        for can_id, stamps in sorted(by_id.items()):
            timing = summarize_periods(stamps)
            width = 8 if extended[can_id] else 3
            rows.append(
                {
                    "can_id": f"0x{can_id:0{width}X}",
                    "extended": extended[can_id],
                    "count": len(stamps),
                    "hz": round(len(stamps) / max(params.duration_s, 1e-9), 1),
                    "period_ms": timing["mean_ms"],
                    "jitter_ms": timing["jitter_ms"],
                    "last": last[can_id].upper(),
                }
            )

        elapsed = max(listen["duration_s"], 1e-9)
        if self.bitrate:
            load: float | None = round(min(100.0, bits / (self.bitrate * elapsed) * 100), 1)
            load_note = "nominal; bit stuffing makes the true figure up to ~20% higher"
        else:
            load = None
            load_note = f"{self.interface} reports no bitrate, so bus load cannot be computed"

        return {
            "interface": self.interface,
            "duration_s": params.duration_s,
            "total_frames": listen["count"],
            "ids": rows,
            "bus_load_percent": load,
            "bus_load_note": load_note,
            "error_frames": listen["error_frames"],
            "dropped": listen["dropped"],
            "bitrate_evidence": _bitrate_evidence(
                bitrate=self.bitrate,
                valid_frames=listen["count"] - listen["error_frames"],
                error_frames=listen["error_frames"],
            ),
        }

    @action(
        "can.send",
        permission=PermissionLevel.CONTROL,
        params=CanSendParams,
        state_changing=True,
        description="Transmit a frame onto the bus.",
        safe_state_note="Transmission stops and the interface returns to listen-only.",
    )
    async def can_send(self, ctx: ActionContext, params: CanSendParams) -> dict[str, Any]:
        """Requires CONTROL: this puts energy on a bus attached to a real DUT."""
        payload = bytes.fromhex(params.data)
        if params.can_id > 0x7FF and not params.extended:
            raise InvalidRequest(
                f"0x{params.can_id:X} needs an extended (29-bit) frame; pass extended=true",
                details={"can_id": params.can_id},
                preserved="nothing was transmitted",
            )

        facts = _link_facts(self.interface)
        if not facts["present"]:
            raise DeviceDisconnected(
                f"{self.interface} no longer exists; the adapter was unplugged",
                details={"interface": self.interface},
                preserved="nothing was transmitted",
            )
        if not facts["up"]:
            raise TransportError(
                f"{self.interface} is down; bring it up deliberately with: "
                f"sudo ip link set {self.interface} up type can bitrate <bitrate>",
                details={"interface": self.interface, "operstate": facts["operstate"]},
                preserved="nothing was transmitted",
            )

        controller = _controller_summary(await self._link_details())
        if controller.get("listen_only") is True:
            # Refuse rather than "fix" it.  Clearing listen-only means taking a
            # live bus interface down, which is not a side effect anyone should
            # get from asking to send one frame.
            raise TransportError(
                f"{self.interface} is configured listen-only and FieldDeck will not "
                f"reconfigure the link to transmit; clear it deliberately with: "
                f"sudo ip link set {self.interface} down && "
                f"sudo ip link set {self.interface} type can listen-only off",
                details={"interface": self.interface, "ctrlmode": controller.get("ctrlmode")},
                preserved="nothing was transmitted",
            )

        can_module = _require_can()
        message = can_module.Message(
            arbitration_id=params.can_id,
            is_extended_id=params.extended,
            data=payload,
            is_fd=False,
        )

        # Unlocked before the first frame leaves, never after: a banner that
        # under-reports transmit capability is worse than one that over-reports.
        self._tx_unlocked = True
        self._descriptor.metadata["mode"] = "normal"

        bus = await asyncio.to_thread(self._open_bus)
        try:
            sent = await asyncio.to_thread(self._send_blocking, bus, message, params.count)
        finally:
            # The TX socket lives exactly as long as the transmission does.
            # The mode flag stays set until safe_state, so an operator reading
            # the banner can see that this interface has transmitted.
            bus.shutdown()

        self._tx_count += sent
        ts = Timestamp.now()
        return {
            "transmitted": sent,
            "can_id": f"0x{params.can_id:X}",
            "data": payload.hex().upper(),
            "dlc": len(payload),
            "extended": params.extended,
            "interface": self.interface,
            "monotonic_ns": ts.monotonic_ns,
            "mode": "normal",
            "safe_state_note": "safe state returns this interface to listen-only",
        }

    def _send_blocking(self, bus: Any, message: Any, count: int) -> int:
        """Transmit ``count`` copies from a worker thread.

        Reports how many frames actually reached the controller if the
        transmission fails part way through: "it failed" is not an answer when
        the question is what the DUT already saw.
        """
        can_module = _require_can()
        sent = 0
        for _ in range(count):
            try:
                bus.send(message, timeout=1.0)
            except (can_module.CanError, OSError) as exc:
                raise TransportError(
                    f"transmit failed on {self.interface} after {sent} of {count} frames: {exc}",
                    details={
                        "interface": self.interface,
                        "sent": sent,
                        "requested": count,
                        "error": str(exc),
                    },
                    preserved=f"{sent} frame(s) were transmitted before the failure",
                ) from exc
            sent += 1
        return sent

    @action(
        "can.decode",
        permission=PermissionLevel.PASSIVE,
        params=CanDecodeParams,
        state_changing=False,
        description="Decode a capture with a DBC into a derived signal table.",
        timeout_s=300.0,
    )
    async def can_decode(self, ctx: ActionContext, params: CanDecodeParams) -> dict[str, Any]:
        """Reads a capture and writes a *new* file; the raw capture is untouched."""
        cantools = _load_cantools()
        if cantools is None:
            raise UnsupportedCapability(
                "cantools is not installed; install it with: pip install 'fielddeck[can]'",
                details={"package": "cantools"},
                preserved="the raw capture is untouched",
            )
        if ctx.recorder is None:
            raise CaptureError(
                "a decoded capture is attached to a session; start one first with: "
                'fdctl session start "<name>"',
                preserved="the raw capture is untouched",
            )

        recorder = ctx.recorder
        source_path, source_artifact_id = _resolve_capture(recorder, params)
        dbc_path = Path(params.dbc).expanduser()
        if not dbc_path.is_file():
            raise InvalidRequest(
                f"DBC file not found: {dbc_path}",
                details={"dbc": str(dbc_path)},
                preserved="the raw capture is untouched",
            )

        try:
            database = await asyncio.to_thread(cantools.database.load_file, str(dbc_path))
        except Exception as exc:  # noqa: BLE001 - cantools raises a family of parse errors that all mean one thing to the operator
            raise InvalidRequest(
                f"cannot read {dbc_path.name} as a CAN database: {exc}",
                details={"dbc": str(dbc_path), "error": str(exc)},
                preserved="the raw capture is untouched",
            ) from exc
        if not hasattr(database, "messages"):
            raise InvalidRequest(
                f"{dbc_path.name} is not a CAN database (a diagnostics database has no frames)",
                details={"dbc": str(dbc_path)},
                preserved="the raw capture is untouched",
            )

        out_path = recorder.capture_path("can", f"{self.interface}-{params.label}", ".csv")
        summary = await asyncio.to_thread(
            _decode_file, database, source_path, out_path, params.max_frames
        )

        artifact = recorder.add_artifact(
            out_path,
            kind="can-decoded",
            media_type="text/csv",
            device_id=self.device_id,
            # The provenance chain: derived, from that capture, by that version
            # of that tool, using a database with that hash.
            raw=False,
            source_artifact_ids=[source_artifact_id] if source_artifact_id else [],
            producer="cantools",
            producer_version=getattr(cantools, "__version__", None),
            producer_config={
                "dbc": dbc_path.name,
                "dbc_sha256": sha256_file(dbc_path),
                "source_path": str(source_path),
                "decode_choices": True,
                "allow_truncated": True,
            },
            metadata=summary,
        )
        ctx.emit(
            new_event(
                EventType.ARTIFACT_ADDED,
                source=ctx.source,
                session_id=ctx.session_id,
                device_id=self.device_id,
                action="can.decode",
                request_id=ctx.request_id,
                message=(
                    f"decoded {summary['decoded_frames']} frames into {artifact.relative_path}"
                ),
                payload=artifact.model_dump(mode="json"),
            )
        )
        return {
            **summary,
            "source": {
                "path": str(source_path),
                "artifact_id": source_artifact_id,
                "preserved": True,
            },
            "dbc": dbc_path.name,
            "artifact": artifact.model_dump(mode="json"),
            "note": "the raw capture was read only; decoded signals are a separate artifact",
        }


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def _resolve_capture(recorder: SessionRecorder, params: CanDecodeParams) -> tuple[Path, str | None]:
    """Find the capture to decode, and its artifact id if it has one.

    Paths are confined to the sessions directory.  The daemon holds device
    permissions the calling client may not, so it must not double as a "read me
    any file" oracle for a recipe or for the MCP surface.
    """
    registered = recorder.timeline.artifacts()
    root = recorder.root
    sessions_root = root.parent.resolve()

    if params.artifact_id:
        for row in registered:
            if row["artifact_id"] != params.artifact_id:
                continue
            path = root / str(row["relative_path"])
            if not path.is_file():
                raise CaptureError(
                    f"artifact {params.artifact_id} is registered but its file is missing",
                    details={"artifact_id": params.artifact_id, "path": str(path)},
                    preserved="the session index is unchanged",
                )
            return path, params.artifact_id
        raise InvalidRequest(
            f"no artifact {params.artifact_id} in the current session",
            details={"artifact_id": params.artifact_id},
            preserved="nothing was read",
        )

    candidate = Path(str(params.path)).expanduser()
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if not resolved.is_relative_to(sessions_root):
        raise InvalidRequest(
            f"{resolved} is outside the sessions directory; can.decode reads captures, "
            "not arbitrary files",
            details={"path": str(resolved), "allowed_root": str(sessions_root)},
            preserved="nothing was read",
        )
    if not resolved.is_file():
        raise InvalidRequest(
            f"capture file not found: {resolved}",
            details={"path": str(resolved)},
            preserved="nothing was read",
        )
    for row in registered:
        if (root / str(row["relative_path"])).resolve() == resolved:
            return resolved, str(row["artifact_id"])
    return resolved, None


def _decode_file(database: Any, source: Path, destination: Path, max_frames: int) -> dict[str, Any]:
    """Decode a candump log into a CSV of signal values.

    Runs on a worker thread: a long capture is a lot of lines and the event
    loop has an HMI to keep responsive.  The source is opened read-only and is
    never written back to — that is the whole point of a derived artifact.
    """
    read_frames = 0
    decoded_frames = 0
    rows_written = 0
    malformed = 0
    skipped_special = 0
    decode_errors = 0
    unknown_ids: dict[int, int] = {}
    first_error: str | None = None
    units: dict[str, dict[str, str | None]] = {}

    with (
        source.open("r", encoding="ascii", errors="replace") as handle,
        destination.open("w", encoding="utf-8", newline="") as out,
    ):
        writer = csv.writer(out)
        writer.writerow(
            [
                "utc_s",
                "interface",
                "can_id",
                "extended",
                "message",
                "signal",
                "value",
                "unit",
                "raw",
            ]
        )
        for line in handle:
            if read_frames >= max_frames:
                break
            frame = _parse_candump_line(line)
            if frame is None:
                if line.strip():
                    malformed += 1
                continue
            read_frames += 1
            if frame.error or frame.remote:
                # Neither carries signal data; they are counted so the summary
                # still adds up to the number of lines in the capture.
                skipped_special += 1
                continue
            try:
                message = database.get_message_by_frame_id(
                    frame.can_id, force_extended_id=frame.extended
                )
            except KeyError:
                unknown_ids[frame.can_id] = unknown_ids.get(frame.can_id, 0) + 1
                continue
            try:
                # allow_truncated: a real bus contains short frames, and one
                # malformed payload must not abandon the rest of the file.
                signals = message.decode(frame.data, decode_choices=True, allow_truncated=True)
            except Exception as exc:  # noqa: BLE001 - cantools raises several decode errors; a bad frame is data, not a crash
                decode_errors += 1
                if first_error is None:
                    first_error = f"0x{frame.can_id:X}: {exc}"
                continue
            decoded_frames += 1

            message_units = units.get(message.name)
            if message_units is None:
                message_units = {signal.name: signal.unit for signal in message.signals}
                units[message.name] = message_units

            width = 8 if frame.extended else 3
            for name, value in signals.items():
                writer.writerow(
                    [
                        f"{frame.utc_s:.6f}",
                        frame.interface,
                        f"0x{frame.can_id:0{width}X}",
                        int(frame.extended),
                        message.name,
                        name,
                        value.hex() if isinstance(value, (bytes, bytearray)) else value,
                        message_units.get(name) or "",
                        frame.data.hex().upper(),
                    ]
                )
                rows_written += 1

    return {
        "frames_read": read_frames,
        "decoded_frames": decoded_frames,
        "signal_rows": rows_written,
        "malformed_lines": malformed,
        "skipped_error_or_remote": skipped_special,
        "decode_errors": decode_errors,
        "first_decode_error": first_error,
        "unknown_ids": {f"0x{can_id:X}": count for can_id, count in sorted(unknown_ids.items())},
        "truncated": read_frames >= max_frames,
    }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_can_drivers(config: FieldDeckConfig) -> list[Driver]:
    """Build a driver for every SocketCAN interface on this machine.

    Enumeration itself is sysfs-only (see :mod:`fielddeck.discovery.linux`): no
    socket is opened here and nothing reaches a bus.  Interfaces that are down
    are still returned, carrying the warning that says so — an operator asking
    "why can't I see can0" needs to see can0.
    """
    if _load_can() is None:
        _log.info(
            "python-can is not installed; SocketCAN interfaces are enumerated but not driven",
            extra={"install": "pip install 'fielddeck[can]'"},
        )
        return []

    return [
        SocketCanDriver(
            str(entry["interface"]),
            bitrate=entry.get("bitrate"),
            fd_capable=bool(entry.get("fd_capable")),
            virtual=bool(entry.get("virtual")),
            mtu=int(entry.get("mtu") or 0),
            ip_tool=config.tools.ip,
        )
        for entry in list_can_interfaces()
    ]
