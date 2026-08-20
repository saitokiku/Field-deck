"""SCPI over VISA: one session, one lock, and a very suspicious parser.

This module is the only place in FieldDeck that puts characters onto a bench
instrument's control channel.  What is worth knowing about it at 2am:

* **A VISA session is a request/response channel with no framing.**  If two
  tasks interleave a query on one session, the second reader collects the
  first one's answer and every reading after that is off by one command.  A
  DMM that reports the previous measurement is worse than one that reports an
  error, so every exchange is serialised behind :attr:`ScpiTransport.lock`.

* **Termination characters decide whether a query works at all.**  USBTMC is
  message-based and ends a read on EOI, so it needs no read terminator; a raw
  ``TCPIP::...::5025::SOCKET`` connection has no EOI at all and will block
  until the timeout unless a read terminator is set.  Serial instruments vary,
  and a few (Korad and its clones) want no terminator on the way out either.
  Getting this wrong looks exactly like a dead instrument, so the defaults are
  derived from the resource class and profiles may override them.

* **A ``?`` is not proof of harmlessness.**  ``*TST?`` runs a self-test that
  operates relays on real hardware, and ``*CAL?`` starts a calibration.  The
  classifier therefore refuses those alongside anything that is not a query at
  all, and the generic ``scpi.query`` path refuses everything it cannot prove
  is read-only.  Typed actions (``psu.set``, ``psu.output``) exist so the
  permission model can see what is being asked for; an arbitrary string cannot
  be authorised meaningfully because ``OUTP ON`` looks harmless right up until
  it energises something.

* **Every failure says what was transmitted.**  A timeout on a query means the
  command *was* sent and the instrument may well have acted on it.  Callers
  and operators need that distinction to decide whether it is safe to retry.

pyvisa is imported inside functions: FieldDeck must import and run on a
machine with no instrument libraries installed at all.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from fielddeck.common.errors import (
    ActionTimeout,
    DeviceBusy,
    DeviceDisconnected,
    FieldDeckError,
    PermissionDenied,
    ProtocolError,
    TransportError,
    UnsupportedCapability,
)
from fielddeck.common.logging import get_logger

__all__ = [
    "AUTO",
    "ScpiClassification",
    "ScpiCommandClass",
    "ScpiSession",
    "ScpiTransport",
    "VisaResource",
    "classify_scpi",
    "default_terminations",
    "parse_resource",
    "parse_scpi_error",
    "require_query",
    "resolve_terminations",
]

_log = get_logger("fielddeck.bench.scpi")

#: Marker meaning "derive this termination from the resource class".  Profiles
#: and operator declarations use it so that ``""`` can keep its real meaning of
#: "send/expect no terminator at all".
AUTO = "auto"

#: VISA status codes from the VISA specification (visa.h).  They are part of
#: the standard rather than of any one implementation, so comparing against the
#: numbers keeps the mapping working under NI-VISA and pyvisa-py alike without
#: importing pyvisa at module scope.
_VI_ERROR_TMO = -1073807339
_VI_ERROR_RSRC_NFOUND = -1073807343
_VI_ERROR_RSRC_BUSY = -1073807246
_VI_ERROR_CONN_LOST = -1073807194
_VI_ERROR_INV_OBJECT = -1073807346
_VI_ERROR_NSUP_OPER = -1073807257
_VI_ERROR_IO = -1073807298

#: Longest a single blocking VISA call may sit in its worker thread beyond the
#: instrument timeout before the transport gives up on the thread itself.  A
#: hung USB stack must not pin an action past the dispatcher's deadline.
_THREAD_MARGIN_S = 2.0


# ---------------------------------------------------------------------------
# Resource names
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VisaResource:
    """A parsed VISA resource name.

    Only the parts FieldDeck needs for identity and framing are extracted.  The
    original string is kept verbatim because it, not this parse, is what gets
    handed to the VISA library.
    """

    name: str
    interface: str
    #: USB instruments only.
    vendor_id: str | None = None
    product_id: str | None = None
    serial_number: str | None = None
    #: TCPIP instruments only.
    host: str | None = None
    port: int | None = None
    #: Serial instruments only: the port name as VISA spells it.
    port_name: str | None = None
    #: True for ``TCPIP::host::5025::SOCKET`` style raw sockets.
    raw_socket: bool = False

    @property
    def is_usb(self) -> bool:
        return self.interface == "usb"

    @property
    def is_serial(self) -> bool:
        return self.interface == "asrl"


def parse_resource(name: str) -> VisaResource:
    """Split a VISA resource name into the fields FieldDeck reasons about.

    Unknown or malformed names are not an error here: they are returned with
    ``interface="other"`` so the operator still sees the instrument they
    declared and gets a real error from the VISA library rather than a parser
    complaint about a name that library might well accept.
    """
    text = name.strip()
    parts = [chunk for chunk in text.split("::") if chunk != ""]
    if not parts:
        return VisaResource(name=text, interface="other")
    head = parts[0].upper()

    if head.startswith("USB"):
        vid = parts[1].lower() if len(parts) > 1 else None
        pid = parts[2].lower() if len(parts) > 2 else None
        serial = parts[3] if len(parts) > 3 and parts[3].upper() != "INSTR" else None
        return VisaResource(
            name=text,
            interface="usb",
            vendor_id=_normalise_hex(vid),
            product_id=_normalise_hex(pid),
            serial_number=serial,
        )

    if head.startswith("TCPIP"):
        host = parts[1] if len(parts) > 1 else None
        raw_socket = parts[-1].upper() == "SOCKET"
        port: int | None = None
        if raw_socket and len(parts) > 2 and parts[2].isdigit():
            port = int(parts[2])
        return VisaResource(
            name=text,
            interface="tcpip",
            host=host,
            port=port,
            raw_socket=raw_socket,
        )

    if head.startswith("ASRL"):
        # ``ASRL/dev/ttyUSB0::INSTR`` keeps the device node in the head chunk;
        # ``ASRL1::INSTR`` uses a board index instead.
        return VisaResource(name=text, interface="asrl", port_name=parts[0][4:] or None)

    if head.startswith("GPIB"):
        return VisaResource(name=text, interface="gpib")

    return VisaResource(name=text, interface="other")


def _normalise_hex(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.lower().removeprefix("0x")
    return cleaned or None


def default_terminations(resource: str) -> tuple[str | None, str]:
    """Sensible (read, write) terminators for a resource class.

    These are framing defaults, not knowledge about the instrument:

    * USBTMC carries message boundaries itself, so a read terminator would
      truncate a response that legitimately contains a newline.
    * A raw TCP socket has no message boundary whatsoever.  Without a read
      terminator every query blocks until the timeout expires.
    * VXI-11/HiSLIP ``INSTR`` sessions are message-based like USBTMC.
    * Serial instruments almost universally answer with a newline; the ones
      that do not are handled by a profile override.
    """
    parsed = parse_resource(resource)
    if parsed.interface == "usb":
        return None, "\n"
    if parsed.interface == "tcpip":
        return ("\n", "\n") if parsed.raw_socket else (None, "\n")
    if parsed.interface == "asrl":
        return "\n", "\n"
    return None, "\n"


def resolve_terminations(
    resource: str, read: str | None = AUTO, write: str | None = AUTO
) -> tuple[str | None, str]:
    """Apply :data:`AUTO` and the empty-string convention to a pair of overrides.

    ``AUTO`` derives the terminator from the resource class; ``""`` means "no
    terminator", which is a real answer for instruments that frame by timeout.
    """
    default_read, default_write = default_terminations(resource)
    if read == AUTO or read is None:
        resolved_read = default_read
    else:
        resolved_read = read or None
    resolved_write = default_write if (write == AUTO or write is None) else write
    return resolved_read, resolved_write


# ---------------------------------------------------------------------------
# Command classification
# ---------------------------------------------------------------------------


class ScpiCommandClass(StrEnum):
    """What an arbitrary SCPI string is allowed to be treated as."""

    #: Provably read-only: every segment is a query and none is on the
    #: known-hazardous list.
    QUERY = "query"
    #: Anything else.  Refused by the generic path — never guessed at.
    COMMAND = "command"


@dataclass(frozen=True, slots=True)
class ScpiClassification:
    command: str
    kind: ScpiCommandClass
    reason: str

    @property
    def is_query(self) -> bool:
        return self.kind is ScpiCommandClass.QUERY


#: Query-shaped commands that change instrument state anyway.  Keyed by the
#: first mnemonic of the header, upper case, leading colon stripped.
#:
#: ``*TST?`` runs the power-on self test, which on a supply or a DMM operates
#: internal relays and can drop an output.  ``*CAL?`` starts self-calibration,
#: which takes the instrument out of service for minutes.  ``CAL:``/``DIAG:``
#: subsystems are vendor territory where "query" and "perform" are routinely
#: the same command.  None of these belong on a path whose entire promise is
#: "this only reads".
_STATE_CHANGING_QUERY_HEADERS: dict[str, str] = {
    "*TST": "*TST? runs a self-test that operates relays and can drop an output",
    "*CAL": "*CAL? starts a self-calibration and takes the instrument out of service",
    "CAL": "the CALibration subsystem changes stored calibration state",
    "CALIBRATION": "the CALibration subsystem changes stored calibration state",
    "DIAG": "the DIAGnostic subsystem performs vendor test routines, not reads",
    "DIAGNOSTIC": "the DIAGnostic subsystem performs vendor test routines, not reads",
}

#: Characters that would let a second command ride along inside one string.
_SMUGGLING = ("\n", "\r", "\x00")


def classify_scpi(command: str) -> ScpiClassification:
    """Decide, conservatively, whether ``command`` is a pure query.

    The rule is deliberately narrow, because the cost of a wrong "yes" is an
    energised DUT and the cost of a wrong "no" is an operator typing a typed
    action instead:

    1. no embedded line terminators or NULs — those can carry a second command
    2. every ``;``-separated segment must end in ``?``
    3. no segment may start with a header known to act rather than read
    """
    text = command.strip()
    if not text:
        return ScpiClassification(text, ScpiCommandClass.COMMAND, "empty command")

    for bad in _SMUGGLING:
        if bad in text:
            return ScpiClassification(
                text,
                ScpiCommandClass.COMMAND,
                "contains an embedded terminator, which can carry a second command",
            )

    segments = [segment.strip() for segment in text.split(";")]
    for segment in segments:
        if not segment:
            return ScpiClassification(
                text, ScpiCommandClass.COMMAND, "contains an empty compound segment"
            )
        if not segment.endswith("?"):
            return ScpiClassification(
                text,
                ScpiCommandClass.COMMAND,
                f"segment {segment!r} is not a query",
            )
        header = _header_mnemonic(segment)
        hazard = _STATE_CHANGING_QUERY_HEADERS.get(header)
        if hazard is not None:
            return ScpiClassification(text, ScpiCommandClass.COMMAND, hazard)

    return ScpiClassification(text, ScpiCommandClass.QUERY, "every segment is a query")


def _header_mnemonic(segment: str) -> str:
    """First mnemonic of a SCPI header, normalised for lookup."""
    header = segment.split(" ", 1)[0].strip().lstrip(":")
    header = header.rstrip("?")
    return header.split(":", 1)[0].upper()


def require_query(
    command: str, *, device_id: str, typed_actions: Sequence[str] = ()
) -> ScpiClassification:
    """Return the classification, or refuse the command.

    This is the posture the whole generic SCPI path rests on: an arbitrary
    string that is not provably a query is rejected with a pointer at the typed
    actions, because those are the ones the permission model can reason about.
    """
    classified = classify_scpi(command)
    if classified.is_query:
        return classified
    suggestion = ", ".join(typed_actions) if typed_actions else "the typed bench actions"
    raise PermissionDenied(
        f"{classified.command!r} is not a query ({classified.reason}). Use the typed "
        f"actions ({suggestion}) so the permission model can see what you are asking for.",
        details={
            "command": classified.command,
            "device_id": device_id,
            "reason": classified.reason,
            "typed_actions": list(typed_actions),
        },
        preserved="nothing was sent to the instrument",
    )


def parse_scpi_error(response: str) -> tuple[int, str]:
    """Parse a ``SYST:ERR?`` reply into ``(code, text)``.

    The standard reply is ``<code>,"<message>"`` but instruments pad it in
    creative ways, so an unparseable reply is reported as a non-zero error
    rather than silently treated as "no error".
    """
    text = response.strip()
    if not text:
        return -1, "empty error-queue response"
    head, _, tail = text.partition(",")
    try:
        code = int(float(head.strip()))
    except ValueError:
        return -1, f"unparseable error-queue response: {text!r}"
    return code, tail.strip().strip('"') or ("No error" if code == 0 else text)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class ScpiSession(Protocol):
    """The slice of a pyvisa message-based resource this module uses.

    Declared as a protocol so tests — and anything that needs to stand in for
    an instrument — can supply an object with three methods instead of a USB
    stack.
    """

    def query(self, command: str) -> str: ...

    def write(self, command: str) -> Any: ...

    def close(self) -> None: ...


class ScpiTransport:
    """One VISA session, opened lazily and used by one caller at a time."""

    def __init__(
        self,
        resource: str,
        *,
        device_id: str | None = None,
        timeout_s: float = 5.0,
        read_termination: str | None = AUTO,
        write_termination: str | None = AUTO,
        min_command_interval_s: float = 0.0,
        backend: str | None = None,
        opener: Any = None,
    ) -> None:
        self.resource = resource
        self.device_id = device_id or resource
        self.timeout_s = timeout_s
        self.min_command_interval_s = min_command_interval_s
        self._backend = backend
        #: Injection point for tests and for anything that wants to supply its
        #: own session; the default opens a real pyvisa resource.
        self._opener = opener
        self._read_termination, self._write_termination = resolve_terminations(
            resource, read_termination, write_termination
        )
        self._session: Any = None
        self._lock = asyncio.Lock()
        self._last_io_monotonic = 0.0
        self.queries = 0
        self.writes = 0

    # -- state -------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._session is not None

    @property
    def terminations(self) -> tuple[str | None, str]:
        return self._read_termination, self._write_termination

    def describe(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "open": self.is_open,
            "timeout_s": self.timeout_s,
            "read_termination": self._read_termination,
            "write_termination": self._write_termination,
            "min_command_interval_s": self.min_command_interval_s,
            "queries": self.queries,
            "writes": self.writes,
        }

    async def set_terminations(self, read: str | None, write: str | None) -> None:
        """Apply profile or operator terminations to this and future sessions."""
        self._read_termination, self._write_termination = resolve_terminations(
            self.resource, read, write
        )
        async with self._lock:
            session = self._session
            if session is None:
                return
            # pyvisa exposes these as live attributes on a message-based
            # resource; anything else simply does not have them and keeps the
            # framing it was opened with.
            for attribute, value in (
                ("read_termination", self._read_termination),
                ("write_termination", self._write_termination),
            ):
                if hasattr(session, attribute):
                    setattr(session, attribute, value)

    # -- lifecycle ---------------------------------------------------------

    async def open(self) -> bool:
        """Open the session if it is not already open.  Returns whether it opened.

        Opening is not free: on a serial resource it takes the port away from
        anything else on the machine, and it is therefore never done during
        discovery.
        """
        async with self._lock:
            # Checked under the lock, not before it: two actions can reach here
            # at once and only one of them may open a session on the instrument.
            if self.is_open:
                return False
            session = await self._run(self._open_session, what="open")
            self._session = session
            _log.info(
                "visa session opened",
                extra={"device": self.device_id, "resource": self.resource},
            )
            return True

    async def close(self) -> None:
        """Close the session.

        Closing is **not** a safe state: a supply keeps its output exactly as
        it was when the control channel went away.  Drive the hardware safe
        first, then close.
        """
        async with self._lock:
            session, self._session = self._session, None
        if session is None:
            return
        try:
            await asyncio.to_thread(session.close)
        except Exception as exc:  # noqa: BLE001 - a close that fails is logged, never raised over a caller's result
            _log.warning(
                "visa session close failed",
                extra={"device": self.device_id, "error": str(exc)},
            )

    # -- exchanges ---------------------------------------------------------

    async def query(
        self,
        command: str,
        *,
        timeout_s: float | None = None,
        lock_timeout_s: float | None = None,
    ) -> str:
        """Send ``command`` and return the response, stripped."""
        async with self._exclusive(lock_timeout_s, command):
            session = await self._session_or_open()
            effective = timeout_s if timeout_s is not None else self.timeout_s
            await self._pace()
            response = await self._run(
                self._do_query,
                session,
                command,
                effective,
                what="query",
                command=command,
                timeout_s=effective,
            )
            self.queries += 1
            return response

    async def write(
        self,
        command: str,
        *,
        timeout_s: float | None = None,
        lock_timeout_s: float | None = None,
    ) -> None:
        """Send ``command`` with no response expected.

        Nothing in this module decides whether a write is allowed; that is the
        dispatcher's job, and callers reach this only from a typed action whose
        permission the operator has already granted.
        """
        async with self._exclusive(lock_timeout_s, command):
            session = await self._session_or_open()
            effective = timeout_s if timeout_s is not None else self.timeout_s
            await self._pace()
            await self._run(
                self._do_write,
                session,
                command,
                effective,
                what="write",
                command=command,
                timeout_s=effective,
            )
            self.writes += 1

    # -- internals ---------------------------------------------------------

    def _exclusive(self, lock_timeout_s: float | None, command: str) -> Any:
        return _Exclusive(self, lock_timeout_s, command)

    async def _session_or_open(self) -> Any:
        """Caller already holds the lock."""
        if self._session is None:
            self._session = await self._run(self._open_session, what="open")
        return self._session

    async def _pace(self) -> None:
        """Honour a device-mandated gap between commands.

        This is not a sleep standing in for a status check: a few supplies
        (Korad and its relabels) drop commands that arrive inside their
        firmware's command window, and the gap is the documented remedy.
        """
        if self.min_command_interval_s <= 0:
            return
        elapsed = time.monotonic() - self._last_io_monotonic
        remaining = self.min_command_interval_s - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _run(
        self,
        func: Any,
        *args: Any,
        what: str,
        command: str | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        """Run one blocking VISA call in a worker thread, mapped to our errors."""
        timeout = timeout_s if timeout_s is not None else self.timeout_s
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(func, *args), timeout + _THREAD_MARGIN_S
            )
        except TimeoutError as exc:
            # The VISA call itself did not return within its own timeout plus a
            # margin, which means the library, not the instrument, is stuck.
            raise TransportError(
                f"the VISA library stopped responding during {what} on {self.resource}",
                details={"device_id": self.device_id, "resource": self.resource, "op": what},
                preserved=self._preserved(what, command),
            ) from exc
        except FieldDeckError:
            # Already one of ours — a missing pyvisa, for instance.  Re-wrapping
            # it would bury the message that tells the operator what to install.
            raise
        except Exception as exc:
            raise self._map_error(exc, what=what, command=command) from exc
        finally:
            self._last_io_monotonic = time.monotonic()

    def _open_session(self) -> Any:
        if self._opener is not None:
            return self._opener()
        pyvisa = _pyvisa()
        manager = (
            pyvisa.ResourceManager(self._backend) if self._backend else pyvisa.ResourceManager()
        )
        session = manager.open_resource(self.resource)
        session.timeout = int(self.timeout_s * 1000)
        for attribute, value in (
            ("read_termination", self._read_termination),
            ("write_termination", self._write_termination),
        ):
            if hasattr(session, attribute):
                setattr(session, attribute, value)
        return session

    @staticmethod
    def _do_query(session: Any, command: str, timeout_s: float) -> str:
        if hasattr(session, "timeout"):
            session.timeout = int(timeout_s * 1000)
        response = session.query(command)
        return str(response).strip()

    @staticmethod
    def _do_write(session: Any, command: str, timeout_s: float) -> None:
        if hasattr(session, "timeout"):
            session.timeout = int(timeout_s * 1000)
        session.write(command)

    def _preserved(self, what: str, command: str | None) -> str:
        if what == "open":
            return "no session was opened and no SCPI was sent"
        return (
            f"{command!r} was transmitted; the instrument may have acted on it, so read "
            "back before assuming it did not"
        )

    def _map_error(self, exc: BaseException, *, what: str, command: str | None) -> Exception:
        """Turn a VISA/OS failure into something an operator can act on."""
        details: dict[str, Any] = {
            "device_id": self.device_id,
            "resource": self.resource,
            "op": what,
            "command": command,
            "type": type(exc).__name__,
        }
        preserved = self._preserved(what, command)
        code = getattr(exc, "error_code", None)
        code = int(code) if isinstance(code, int) else None
        if code is not None:
            details["visa_status"] = code

        if code == _VI_ERROR_TMO:
            return ActionTimeout(
                f"{self.resource} did not answer within {self.timeout_s:g}s"
                + (f" for {command!r}" if command else ""),
                details=details,
                preserved=(
                    preserved if what != "open" else "no session was opened and no SCPI was sent"
                )
                + "; a query timeout with the wrong read terminator looks identical to a "
                "dead instrument, so check the resource's termination settings",
            )
        if code in (_VI_ERROR_RSRC_NFOUND, _VI_ERROR_CONN_LOST, _VI_ERROR_INV_OBJECT):
            self._session = None
            return DeviceDisconnected(
                f"{self.resource} is not reachable; the instrument looks unplugged or powered down",
                details=details,
                preserved=preserved,
            )
        if code == _VI_ERROR_RSRC_BUSY:
            return DeviceBusy(
                f"{self.resource} is held by another program",
                details=details,
                preserved=preserved,
            )
        if code == _VI_ERROR_NSUP_OPER:
            return UnsupportedCapability(
                f"{self.resource} does not support this operation",
                details=details,
                preserved=preserved,
            )
        if code == _VI_ERROR_IO:
            return TransportError(
                f"I/O error talking to {self.resource}",
                details=details,
                preserved=preserved,
            )

        if isinstance(exc, PermissionError):
            return TransportError(
                f"no permission to open {self.resource}; a USBTMC instrument needs a udev "
                "rule granting the account running instrumentd access to the device "
                "(and a serial one needs the 'dialout' group)",
                details=details,
                preserved="no session was opened and no SCPI was sent",
            )
        if isinstance(exc, FileNotFoundError):
            return DeviceDisconnected(
                f"{self.resource} is not present",
                details=details,
                preserved="no session was opened and no SCPI was sent",
            )
        if isinstance(exc, UnicodeDecodeError):
            return ProtocolError(
                f"{self.resource} answered with bytes that are not text; this usually means "
                "the framing is wrong for this instrument",
                details=details,
                preserved=preserved,
            )
        if isinstance(exc, OSError):
            return TransportError(
                f"cannot reach {self.resource}: {exc}",
                details={**details, "errno": exc.errno},
                preserved=preserved,
            )
        return TransportError(
            f"{what} failed on {self.resource}: {exc}",
            details=details,
            preserved=preserved,
        )


class _Exclusive:
    """Async context manager guarding one VISA session.

    Bounded on purpose: safe-state work asks for the lock with a deadline so a
    stuck exchange cannot make "turn the output off" wait forever.
    """

    def __init__(self, transport: ScpiTransport, timeout_s: float | None, command: str) -> None:
        self._transport = transport
        self._timeout_s = timeout_s
        self._command = command
        self._held = False

    async def __aenter__(self) -> None:
        lock = self._transport._lock
        if self._timeout_s is None:
            await lock.acquire()
        else:
            try:
                await asyncio.wait_for(lock.acquire(), self._timeout_s)
            except TimeoutError as exc:
                raise DeviceBusy(
                    f"{self._transport.resource} is mid-exchange and did not free up within "
                    f"{self._timeout_s:g}s",
                    details={
                        "device_id": self._transport.device_id,
                        "resource": self._transport.resource,
                        "command": self._command,
                    },
                    preserved="the in-flight exchange was not disturbed",
                ) from exc
        self._held = True

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._held:
            self._transport._lock.release()
            self._held = False


def _pyvisa() -> Any:
    """Import pyvisa on demand.

    Optional by design: a FieldDeck install with no bench extra still imports,
    still enumerates, and simply has no VISA drivers to offer.
    """
    try:
        import pyvisa
    except ImportError as exc:  # pragma: no cover - exercised on installs without the extra
        raise UnsupportedCapability(
            "pyvisa is not installed; install it with: pip install 'fielddeck[bench]'",
            details={"module": "pyvisa", "extra": "bench"},
            preserved="no session was opened and no SCPI was sent",
        ) from exc
    return pyvisa
