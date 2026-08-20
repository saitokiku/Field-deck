"""Modbus RTU and TCP, behind pymodbus.

Modbus is the protocol most likely to be pointed at something that can move,
heat or pressurise, and the wire format offers an operator no protection at
all: no handshake, no capability discovery, no acknowledgement that the
station you addressed is the one you meant.  A write is a single frame that
takes effect the moment it lands.  What follows from that:

* **Nothing here is PASSIVE except ``modbus.status``.**  A Modbus master is
  only ever a talker; even a register read puts a frame on the wire and takes
  a slave's turnaround time.  Reads are QUERY, writes are CONTROL, and the
  address scan is QUERY because it addresses every station in its range one
  at a time.  Watching somebody else's RTU line without transmitting is real
  PASSIVE work and belongs to the serial transport, not to this module.

* **Endpoints are configured, never guessed.**  A USB-RS485 adapter is not
  evidence that a Modbus device is behind it, and probing to find out would
  be precisely the unauthorised transmission the safety model exists to
  prevent.  Discovery only builds drivers for endpoints an operator wrote
  down in ``instruments/modbus.yaml``.

* **Word order is a per-vendor coin flip.**  The specification fixes byte
  order inside a register and says nothing about how a 32-bit value is split
  across two of them.  Swapping the words turns a 1.9 bar reading into 3e-8
  or 2e33 with nothing looking broken, so every register read reports the
  order it used *and* what the other order would have produced.  FieldDeck
  never picks one silently.

* **RTU is a half-duplex bus with exactly one master.**  Overlapping
  transactions do not interleave politely, they destroy each other's framing
  and produce CRC errors that look like a wiring fault.  Every transaction on
  a driver is therefore serialised through one lock, even though the
  dispatcher would happily run two QUERY reads concurrently.

* **A timeout on a write means the DUT state is unknown**, not that the write
  did not happen.  The frame went out; a slave that applied it and then failed
  to answer is indistinguishable from one that never heard it.  That is said
  in the error rather than left for someone to work out at 2am.

* **Broadcast (slave 0) is not implemented.**  Writing one register on every
  station of a bus simultaneously is part of the protocol and an appalling
  thing to reach by typo, so addresses start at 1.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import math
import struct
from abc import abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from fielddeck.common.config import FieldDeckConfig
from fielddeck.common.errors import (
    ActionTimeout,
    ConfigurationError,
    DeviceBusy,
    FieldDeckError,
    ProtocolError,
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
    StrictModel,
    TransportKind,
)
from fielddeck.common.paths import Paths, default_paths
from fielddeck.common.timebase import Timestamp, monotonic_ns
from fielddeck.drivers.base import ActionContext, DeviceParams, Driver, action

__all__ = [
    "EXCEPTION_NAMES",
    "MAX_SCAN_ADDRESSES",
    "ModbusDriver",
    "ModbusDriverBase",
    "ModbusEndpoint",
    "ModbusReply",
    "ModbusRequest",
    "ModbusTransaction",
    "data_reference",
    "decode_registers",
    "discover_modbus_drivers",
    "load_modbus_endpoints",
]

_log = get_logger("fielddeck.protocols.modbus")

#: The function codes FieldDeck issues.  Everything else — file records,
#: FIFO queues, vendor functions — is deliberately out of scope: an action
#: nobody can explain in one sentence has no business writing to a PLC.
FUNCTION_READ_COILS = 0x01
FUNCTION_READ_DISCRETE = 0x02
FUNCTION_READ_HOLDING = 0x03
FUNCTION_READ_INPUT = 0x04
FUNCTION_WRITE_COIL = 0x05
FUNCTION_WRITE_REGISTER = 0x06
FUNCTION_WRITE_REGISTERS = 0x10

FUNCTION_NAMES: dict[int, str] = {
    FUNCTION_READ_COILS: "read_coils",
    FUNCTION_READ_DISCRETE: "read_discrete_inputs",
    FUNCTION_READ_HOLDING: "read_holding_registers",
    FUNCTION_READ_INPUT: "read_input_registers",
    FUNCTION_WRITE_COIL: "write_single_coil",
    FUNCTION_WRITE_REGISTER: "write_single_register",
    FUNCTION_WRITE_REGISTERS: "write_multiple_registers",
}

#: Exception codes from the specification.  A slave that answers with one of
#: these is *present and talking* — that is evidence, not a failure to scan.
EXCEPTION_NAMES: dict[int, str] = {
    0x01: "IllegalFunction",
    0x02: "IllegalDataAddress",
    0x03: "IllegalDataValue",
    0x04: "SlaveDeviceFailure",
    0x05: "Acknowledge",
    0x06: "SlaveDeviceBusy",
    0x08: "MemoryParityError",
    0x0A: "GatewayPathUnavailable",
    0x0B: "GatewayTargetDeviceFailedToRespond",
}

#: Protocol maxima: one PDU carries at most 2000 bits or 125 registers, and a
#: multiple-register write at most 123.  These are the protocol's limits, not
#: policy — a device may accept far fewer.
MAX_READ_BITS = 2000
MAX_READ_REGISTERS = 125
MAX_WRITE_REGISTERS = 123

MIN_SLAVE_ID = 1
MAX_SLAVE_ID = 247
MAX_DATA_ADDRESS = 0xFFFF

#: Exceptions raised by a *gateway* about a station behind it, rather than by
#: the station itself.  They mean the target did not answer the bridge.
_GATEWAY_EXCEPTIONS = frozenset({0x0A, 0x0B})

#: Widest range one ``modbus.scan`` may cover.  A scan is not free: it puts a
#: frame on every address in the range and waits out the silent ones, so an
#: unbounded "scan everything" is both slow and rude to a live bus.
MAX_SCAN_ADDRESSES = 64

#: Longest a single address may be given to answer during a scan.
MAX_SCAN_TIMEOUT_S = 5.0

#: Stop scanning this long before the dispatcher deadline so the partial
#: result is returned rather than lost to an ActionTimeout.
_SCAN_DEADLINE_MARGIN_S = 0.25

#: Give up waiting for the bus this long before the action deadline, so the
#: caller learns *what* the bus is busy with instead of getting a bare
#: timeout from the dispatcher.
_BUS_WAIT_MARGIN_S = 0.15
_MIN_BUS_WAIT_S = 0.05

#: Register reads longer than this are not written to the timeline as
#: individual measurements; the full array is still in the action result and
#: the transaction record.  Polling 125 registers at 10 Hz would otherwise
#: bury every other event in the session.
MAX_TIMELINE_REGISTERS = 32

#: Base of the classic 5-digit reference convention, per data table.  The
#: wire address is always 0-based; the documentation on the engineer's desk
#: almost never is, and that off-by-one misreads a sensor by one register.
_REFERENCE_BASE: dict[str, int] = {
    "coils": 1,
    "discrete": 10001,
    "input": 30001,
    "holding": 40001,
}

WordOrder = Literal["big", "little"]
ByteOrder = Literal["big", "little"]
Outcome = Literal["ok", "exception", "timeout", "error"]
RegisterKind = Literal["coils", "discrete", "input", "holding"]

_SCAN_FUNCTIONS: dict[str, int] = {
    "holding": FUNCTION_READ_HOLDING,
    "input": FUNCTION_READ_INPUT,
    "coils": FUNCTION_READ_COILS,
    "discrete": FUNCTION_READ_DISCRETE,
}


def data_reference(kind: RegisterKind, address: int) -> str:
    """The documentation-style reference for a 0-based wire address.

    Holding register 0 is ``40001`` in every vendor manual ever printed.
    Reporting both removes the most common Modbus mistake there is.
    """
    return str(_REFERENCE_BASE[kind] + address)


# ---------------------------------------------------------------------------
# Endpoint configuration
# ---------------------------------------------------------------------------


class ModbusEndpoint(StrictModel):
    """One operator-declared Modbus endpoint.

    An endpoint says where the bus is and how to frame bytes on it.  It says
    nothing about what is attached: register maps belong to the DUT, and
    FieldDeck refuses to invent them.
    """

    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    transport: Literal["rtu", "tcp"]
    label: str | None = None

    # TCP
    host: str | None = None
    port: int = Field(default=502, ge=1, le=65535)

    # RTU
    serial_port: str | None = Field(
        default=None,
        description="Serial device path; prefer a /dev/serial/by-id symlink",
    )
    baudrate: int = Field(default=19200, ge=50, le=12_000_000)
    parity: str = Field(default="E", pattern="^[NEO]$")
    stopbits: int = Field(default=1, ge=1, le=2)
    bytesize: int = Field(default=8, ge=7, le=8)

    #: Used when an action does not name a station explicitly.
    default_slave: int = Field(default=1, ge=MIN_SLAVE_ID, le=MAX_SLAVE_ID)
    #: How long one transaction may wait for a reply.
    timeout_s: float = Field(default=1.0, gt=0, le=30.0)

    @model_validator(mode="after")
    def _consistent(self) -> ModbusEndpoint:
        if self.transport == "tcp" and not self.host:
            raise ValueError(f"endpoint {self.name!r} is tcp and needs a host")
        if self.transport == "rtu" and not self.serial_port:
            raise ValueError(f"endpoint {self.name!r} is rtu and needs a serial_port")
        return self

    @property
    def device_id(self) -> str:
        # Keyed on the operator's chosen name rather than on the address: a
        # DHCP lease or a /dev/ttyUSB number changing must not silently
        # rename the device a saved recipe refers to.
        return f"modbus:{self.transport}:{self.name}"

    @property
    def location(self) -> str:
        if self.transport == "tcp":
            return f"{self.host}:{self.port}"
        return str(self.serial_port)

    @property
    def framing(self) -> str:
        return f"{self.baudrate} {self.bytesize}{self.parity}{self.stopbits}"

    def summary(self) -> dict[str, Any]:
        common: dict[str, Any] = {
            "name": self.name,
            "transport": self.transport,
            "location": self.location,
            "default_slave": self.default_slave,
            "timeout_s": self.timeout_s,
        }
        if self.transport == "rtu":
            common["framing"] = self.framing
        return common


class ModbusEndpointFile(StrictModel):
    """Schema of ``<config>/instruments/modbus.yaml``."""

    endpoints: list[ModbusEndpoint] = Field(default_factory=list)


def load_modbus_endpoints(paths: Paths | None = None) -> list[ModbusEndpoint]:
    """Read the operator's declared endpoints.

    A missing file means "no Modbus endpoints", which is the safe reading.  A
    file that exists and does not parse is a hard error: quietly ignoring a
    half-written endpoint list would leave an operator convinced they had
    configured a bus they had not.
    """
    resolved = paths or default_paths()
    path: Path = resolved.instruments_dir / "modbus.yaml"
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigurationError(f"cannot read {path}: {exc}", details={"path": str(path)}) from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            f"{path} is not valid YAML: {exc}", details={"path": str(path)}
        ) from exc
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise ConfigurationError(
            f"{path} must contain a YAML mapping with an 'endpoints' list",
            details={"path": str(path)},
        )
    try:
        parsed = ModbusEndpointFile.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(f"{path} is invalid:\n{exc}", details={"path": str(path)}) from exc

    seen: set[str] = set()
    for endpoint in parsed.endpoints:
        if endpoint.name in seen:
            raise ConfigurationError(
                f"{path} declares two endpoints named {endpoint.name!r}",
                details={"path": str(path), "name": endpoint.name},
            )
        seen.add(endpoint.name)
    return parsed.endpoints


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def _swap_bytes(word: int) -> int:
    return ((word & 0xFF) << 8) | (word >> 8)


def _to_int16(word: int) -> int:
    return word - 0x10000 if word & 0x8000 else word


def _finite(value: float) -> float | None:
    """JSON has no NaN or infinity, and a silently-dropped one is a lie."""
    return value if math.isfinite(value) else None


def _combine(high: int, low: int) -> int:
    return (high << 16) | low


def _pairs(
    registers: Sequence[int],
    *,
    word_order: WordOrder,
    address: int,
    kind: RegisterKind,
) -> list[dict[str, Any]]:
    """Non-overlapping 32-bit interpretations of consecutive register pairs."""
    out: list[dict[str, Any]] = []
    for index in range(0, len(registers) - 1, 2):
        first, second = registers[index], registers[index + 1]
        high, low = (first, second) if word_order == "big" else (second, first)
        raw = _combine(high, low)
        signed = raw - 0x1_0000_0000 if raw & 0x8000_0000 else raw
        (value,) = struct.unpack(">f", raw.to_bytes(4, "big"))
        out.append(
            {
                "address": address + index,
                "reference": data_reference(kind, address + index),
                "registers": [first, second],
                "hex": f"0x{raw:08X}",
                "uint32": raw,
                "int32": signed,
                "float32": _finite(float(value)),
                "float32_finite": math.isfinite(value),
            }
        )
    return out


def decode_registers(
    registers: Sequence[int],
    *,
    address: int = 0,
    kind: RegisterKind = "holding",
    word_order: WordOrder = "big",
    byte_order: ByteOrder = "big",
) -> dict[str, Any]:
    """Interpret raw registers every way that is plausible, and say which.

    16-bit values are unambiguous once byte order is fixed; 32-bit ones are
    not, because the specification never said which register holds the high
    word.  Both candidate decodings are returned, tagged, so the engineer
    compares them against a plausible physical value instead of trusting
    whichever one this code happened to compute first.
    """
    raw = [int(value) & 0xFFFF for value in registers]
    ordered = [_swap_bytes(value) for value in raw] if byte_order == "little" else list(raw)
    other_order: WordOrder = "little" if word_order == "big" else "big"

    values = [
        {
            "address": address + index,
            "reference": data_reference(kind, address + index),
            "hex": f"0x{word:04X}",
            "uint16": word,
            "int16": _to_int16(word),
        }
        for index, word in enumerate(ordered)
    ]
    return {
        "word_order": word_order,
        "byte_order": byte_order,
        "registers": ordered,
        "raw_registers": raw,
        "values": values,
        "pairs": _pairs(ordered, word_order=word_order, address=address, kind=kind),
        "alternate_word_order": {
            "word_order": other_order,
            "pairs": _pairs(ordered, word_order=other_order, address=address, kind=kind),
            "note": (
                "Modbus does not define 32-bit word order. Compare both against a "
                "physically plausible value before trusting either."
            ),
        },
        "unpaired_register": len(ordered) % 2 == 1,
    }


def bits_to_hex(bits: Sequence[bool]) -> str:
    """Pack bits back into the on-the-wire byte order, LSB first."""
    packed = bytearray((len(bits) + 7) // 8)
    for index, bit in enumerate(bits):
        if bit:
            packed[index // 8] |= 1 << (index % 8)
    return packed.hex()


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModbusRequest:
    """One PDU FieldDeck is about to put on the wire."""

    slave: int
    function: int
    address: int
    count: int
    #: Raw words for a write, exactly as they will be sent.  Never derived
    #: from a friendlier representation anywhere below this point: what is
    #: logged is what leaves the port.
    values: tuple[int, ...] = ()

    @property
    def function_name(self) -> str:
        return FUNCTION_NAMES.get(self.function, f"function_0x{self.function:02X}")

    @property
    def kind(self) -> RegisterKind:
        if self.function in (FUNCTION_READ_COILS, FUNCTION_WRITE_COIL):
            return "coils"
        if self.function == FUNCTION_READ_DISCRETE:
            return "discrete"
        if self.function == FUNCTION_READ_INPUT:
            return "input"
        return "holding"

    @property
    def is_write(self) -> bool:
        return self.function in (
            FUNCTION_WRITE_COIL,
            FUNCTION_WRITE_REGISTER,
            FUNCTION_WRITE_REGISTERS,
        )


@dataclass(frozen=True, slots=True)
class ModbusReply:
    """What came back — including the useful failures.

    ``outcome`` separates the three failures that look identical in a naive
    client and mean completely different things: ``exception`` (the station is
    there and refused), ``timeout`` (nothing answered) and ``error`` (the
    transport itself failed, so nothing was asked at all).
    """

    outcome: Outcome
    registers: tuple[int, ...] = ()
    bits: tuple[bool, ...] = ()
    exception_code: int | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    @property
    def exception_name(self) -> str | None:
        if self.exception_code is None:
            return None
        return EXCEPTION_NAMES.get(self.exception_code, f"Exception{self.exception_code}")


class ModbusTransaction(StrictModel):
    """The audit record for one request/response pair.

    This is what the session timeline shows about what was asked of the DUT:
    station, function, address, count, and how it ended.
    """

    device_id: str
    action: str
    slave: int
    function: int
    function_name: str
    address: int
    reference: str
    count: int
    values: list[int] = Field(default_factory=list)
    outcome: Outcome
    ok: bool
    exception_code: int | None = None
    exception_name: str | None = None
    duration_ms: float = 0.0
    detail: str | None = None

    def one_line(self) -> str:
        tail = self.exception_name or self.outcome
        return (
            f"slave {self.slave} fn 0x{self.function:02X} {self.function_name} "
            f"@{self.reference} x{self.count} -> {tail}"
        )


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


class ModbusStationParams(DeviceParams):
    """Base for anything that addresses a station.

    Slave 0 (broadcast) is excluded at the schema level, not by a check
    somewhere further in: a broadcast write reaches every device on the bus
    and there is no reply to tell you what it did.
    """

    slave: int = Field(default=1, ge=MIN_SLAVE_ID, le=MAX_SLAVE_ID)


class ModbusReadBitsParams(ModbusStationParams):
    address: int = Field(default=0, ge=0, le=MAX_DATA_ADDRESS)
    count: int = Field(default=1, ge=1, le=MAX_READ_BITS)

    @model_validator(mode="after")
    def _within_address_space(self) -> ModbusReadBitsParams:
        _check_span(self.address, self.count)
        return self


class ModbusReadRegistersParams(ModbusStationParams):
    address: int = Field(default=0, ge=0, le=MAX_DATA_ADDRESS)
    count: int = Field(default=1, ge=1, le=MAX_READ_REGISTERS)
    #: Which register holds the high word of a 32-bit value.  Vendor-specific
    #: and undetectable from the data, so it is asked for rather than assumed.
    word_order: WordOrder = "big"
    #: Byte order inside one register.  The specification says big-endian;
    #: some gateways disagree, and the reading is unrecognisable when they do.
    byte_order: ByteOrder = "big"

    @model_validator(mode="after")
    def _within_address_space(self) -> ModbusReadRegistersParams:
        _check_span(self.address, self.count)
        return self


class ModbusWriteCoilParams(ModbusStationParams):
    address: int = Field(ge=0, le=MAX_DATA_ADDRESS)
    value: bool


class ModbusWriteRegisterParams(ModbusStationParams):
    address: int = Field(ge=0, le=MAX_DATA_ADDRESS)
    #: Accepts either an unsigned word or its signed reading; both map to one
    #: unambiguous 16-bit pattern, which is echoed back in the result.
    value: int = Field(ge=-32768, le=65535)


class ModbusWriteRegistersParams(ModbusStationParams):
    address: int = Field(ge=0, le=MAX_DATA_ADDRESS)
    values: list[int] = Field(min_length=1, max_length=MAX_WRITE_REGISTERS)

    @field_validator("values")
    @classmethod
    def _valid_words(cls, values: list[int]) -> list[int]:
        for value in values:
            if not -32768 <= value <= 65535:
                raise ValueError(f"{value} does not fit in a 16-bit register")
        return values

    @model_validator(mode="after")
    def _within_address_space(self) -> ModbusWriteRegistersParams:
        _check_span(self.address, len(self.values))
        return self


class ModbusScanParams(DeviceParams):
    """Bounded, cancellable address scan."""

    start: int = Field(default=1, ge=MIN_SLAVE_ID, le=MAX_SLAVE_ID)
    end: int = Field(default=16, ge=MIN_SLAVE_ID, le=MAX_SLAVE_ID)
    #: Which read is used as the probe.  A device that answers "illegal data
    #: address" is present too, so the probe does not have to be one it likes.
    probe: RegisterKind = "holding"
    address: int = Field(default=0, ge=0, le=MAX_DATA_ADDRESS)
    count: int = Field(default=1, ge=1, le=8)
    per_address_timeout_s: float = Field(default=0.3, gt=0, le=MAX_SCAN_TIMEOUT_S)

    @model_validator(mode="after")
    def _bounded(self) -> ModbusScanParams:
        if self.end < self.start:
            raise ValueError(f"end ({self.end}) is below start ({self.start})")
        span = self.end - self.start + 1
        if span > MAX_SCAN_ADDRESSES:
            raise ValueError(
                f"a scan may cover at most {MAX_SCAN_ADDRESSES} addresses; "
                f"{self.start}..{self.end} is {span}"
            )
        return self


def _check_span(address: int, count: int) -> None:
    if address + count - 1 > MAX_DATA_ADDRESS:
        raise ValueError(
            f"address {address} + count {count} runs past the end of the 16-bit "
            f"address space ({MAX_DATA_ADDRESS})"
        )


def _to_word(value: int) -> int:
    """Two's-complement a signed reading into the word that goes on the wire."""
    return value & 0xFFFF


# ---------------------------------------------------------------------------
# Driver base — shared by the pymodbus driver and the simulator
# ---------------------------------------------------------------------------


class ModbusDriverBase(Driver):
    """Everything Modbus except the bytes.

    The action set, the permission mapping, the transaction log and the
    decoding live here so the simulator cannot drift away from the hardware
    driver: both are dispatched through the same specs, and a permission
    changed in one place changes for both.  Subclasses implement exactly one
    thing — :meth:`_transact` — plus a passive status snapshot.
    """

    kind = TransportKind.MODBUS

    def __init__(
        self,
        descriptor: DeviceDescriptor,
        *,
        default_slave: int = 1,
        timeout_s: float = 1.0,
    ) -> None:
        Driver.__init__(self, descriptor)
        self.default_slave = default_slave
        self.timeout_s = timeout_s
        #: One master, one transaction at a time.  The dispatcher's device
        #: lock only covers state-changing actions, and two concurrent QUERY
        #: reads on an RTU line corrupt each other's framing.
        self._bus_lock = asyncio.Lock()
        self._bus_busy_with: str | None = None
        self._counters: dict[str, int] = {"ok": 0, "exception": 0, "timeout": 0, "error": 0}
        self._last_transaction: ModbusTransaction | None = None

    # -- subclass contract -------------------------------------------------

    @abstractmethod
    async def _transact(self, request: ModbusRequest, *, timeout_s: float) -> ModbusReply:
        """Perform one transaction.

        Implementations report expected failures as an outcome rather than an
        exception, because the scan needs to keep going past a silent address
        and a caught-and-classified timeout is worth more than a traceback.
        """

    @abstractmethod
    async def _endpoint_status(self) -> dict[str, Any]:
        """Configuration and link state.  Must not put anything on the wire."""

    # -- driver contract ---------------------------------------------------

    async def status(self) -> dict[str, Any]:
        endpoint = await self._endpoint_status()
        total = sum(self._counters.values())
        return {
            **endpoint,
            "default_slave": self.default_slave,
            "timeout_s": self.timeout_s,
            "busy_with": self._bus_busy_with,
            "transactions": {"total": total, **self._counters},
            "last_transaction": (
                self._last_transaction.model_dump(mode="json")
                if self._last_transaction is not None
                else None
            ),
        }

    async def safe_state(self) -> dict[str, Any]:
        """Stop talking.  Deliberately does not write anything.

        There is no such thing as a generically safe register value: zero
        closes one vendor's valve and opens another's.  FieldDeck will not
        guess, so safe state here means "issue nothing further" and say
        plainly that the DUT keeps whatever state it was left in.
        """
        return {
            "device": self.device_id,
            "applied": True,
            "changed": False,
            "state": "no further transactions issued",
            "reason": (
                "a Modbus master cannot know which register values are safe for a "
                "given DUT; no write was invented. Whatever the device was last "
                "commanded to do, it is still doing."
            ),
        }

    # -- transaction plumbing ---------------------------------------------

    @contextlib.asynccontextmanager
    async def _bus(self, ctx: ActionContext, action_name: str) -> AsyncIterator[None]:
        """Serialise the bus, and explain the wait rather than time out blankly.

        Transactions may interleave — a master is free to address one station
        while another is idle — but two *frames* may never be in flight at
        once, which is what this lock guarantees.

        A caller that cannot get the bus before its own deadline is told what
        is holding it.  The wait is cut short of the action deadline on
        purpose: letting the dispatcher's timeout fire instead would replace
        "busy with modbus.scan" with a generic "did not finish in time".
        """
        if not self._bus_lock.locked():
            # A free asyncio.Lock is acquired without suspending, so nothing
            # can slip in between this check and the acquire below.
            await self._bus_lock.acquire()
            self._bus_busy_with = action_name
            try:
                yield
            finally:
                self._bus_busy_with = None
                self._bus_lock.release()
            return

        remaining = ctx.remaining_s()
        wait_s = None if remaining is None else max(_MIN_BUS_WAIT_S, remaining - _BUS_WAIT_MARGIN_S)
        try:
            await asyncio.wait_for(self._bus_lock.acquire(), timeout=wait_s)
        except TimeoutError as exc:
            raise DeviceBusy(
                f"{self.device_id} is busy with {self._bus_busy_with or 'another transaction'}; "
                "Modbus is a one-transaction-at-a-time bus",
                details={
                    "device_id": self.device_id,
                    "busy_with": self._bus_busy_with,
                    "action": action_name,
                },
                preserved="the in-flight transaction was not disturbed",
            ) from exc
        self._bus_busy_with = action_name
        try:
            yield
        finally:
            self._bus_busy_with = None
            self._bus_lock.release()

    async def _perform(
        self,
        ctx: ActionContext,
        action_name: str,
        request: ModbusRequest,
        *,
        timeout_s: float | None = None,
        raise_on_failure: bool = True,
        report_fault: bool = True,
    ) -> tuple[ModbusReply, ModbusTransaction]:
        """Run one transaction, record it, and decide whether it is an error."""
        timeout = timeout_s if timeout_s is not None else self.timeout_s
        async with self._bus(ctx, action_name):
            started = monotonic_ns()
            try:
                reply = await self._transact(request, timeout_s=timeout)
            except UnsupportedCapability:
                # Not a transaction and not a bus problem: the driver library
                # is missing, so nothing was or could be transmitted.  Passing
                # it through keeps the "install this" error code intact rather
                # than dressing it up as a link failure.
                raise
            except FieldDeckError as exc:
                # A typed transport failure is still a transaction that
                # happened; it gets a record like any other.
                reply = ModbusReply(outcome="error", detail=exc.message)
            duration_ms = round((monotonic_ns() - started) / 1e6, 3)

        txn = self._record(ctx, action_name, request, reply, duration_ms, report_fault=report_fault)
        if raise_on_failure and not reply.ok:
            raise self._failure(request, reply, timeout)
        return reply, txn

    def _record(
        self,
        ctx: ActionContext,
        action_name: str,
        request: ModbusRequest,
        reply: ModbusReply,
        duration_ms: float,
        *,
        report_fault: bool,
    ) -> ModbusTransaction:
        txn = ModbusTransaction(
            device_id=self.device_id,
            action=action_name,
            slave=request.slave,
            function=request.function,
            function_name=request.function_name,
            address=request.address,
            reference=data_reference(request.kind, request.address),
            count=request.count,
            values=list(request.values),
            outcome=reply.outcome,
            ok=reply.ok,
            exception_code=reply.exception_code,
            exception_name=reply.exception_name,
            duration_ms=duration_ms,
            detail=reply.detail,
        )
        self._counters[reply.outcome] = self._counters.get(reply.outcome, 0) + 1
        self._last_transaction = txn

        record = {
            "device": self.device_id,
            "action": action_name,
            "slave": txn.slave,
            "function": f"0x{txn.function:02X}",
            "function_name": txn.function_name,
            "address": txn.address,
            "reference": txn.reference,
            "count": txn.count,
            "outcome": txn.outcome,
            "exception": txn.exception_name,
            "duration_ms": duration_ms,
            "request_id": ctx.request_id,
            "session": ctx.session_id,
        }
        if reply.ok:
            _log.info("modbus transaction", extra=record)
        elif report_fault:
            _log.warning("modbus transaction failed", extra=record)
        else:
            # Same reasoning as the suppressed event below: during a scan,
            # silence is the expected answer from most addresses. Logging a
            # warning per probe would flood the journal on every scan.
            _log.debug("modbus transaction failed", extra=record)

        if not reply.ok and report_fault:
            # Emitted for ordinary transactions but not per scan address: a
            # scan is *expected* to find silence, and burying the audit log in
            # 60 "no answer" events would hide the one that matters.
            ctx.emit(
                new_event(
                    EventType.DEVICE_FAULT,
                    source=ctx.source,
                    severity=EventSeverity.WARNING,
                    session_id=ctx.session_id,
                    device_id=self.device_id,
                    action=action_name,
                    permission=ctx.granted_permission,
                    request_id=ctx.request_id,
                    message=txn.one_line(),
                    payload=txn.model_dump(mode="json"),
                )
            )
        return txn

    def _failure(
        self, request: ModbusRequest, reply: ModbusReply, timeout_s: float
    ) -> FieldDeckError:
        details = {
            "device_id": self.device_id,
            "slave": request.slave,
            "function": f"0x{request.function:02X}",
            "function_name": request.function_name,
            "address": request.address,
            "reference": data_reference(request.kind, request.address),
            "count": request.count,
            "detail": reply.detail,
        }
        if reply.outcome == "exception":
            return ProtocolError(
                f"slave {request.slave} refused {request.function_name} at "
                f"{details['reference']}: {reply.exception_name} "
                f"(code {reply.exception_code})",
                details={**details, "exception_code": reply.exception_code},
                preserved=(
                    "the station answered, so it is present and addressable; "
                    "nothing further was sent"
                ),
            )
        if reply.outcome == "timeout":
            preserved = (
                "the request frame was transmitted. A slave that applied the write "
                "and then failed to answer looks exactly like one that never heard "
                "it, so read the value back before assuming nothing changed."
                if request.is_write
                else "nothing was changed; the request was sent once and not retried"
            )
            return ActionTimeout(
                f"slave {request.slave} did not answer {request.function_name} "
                f"within {timeout_s:g}s",
                details={**details, "timeout_s": timeout_s},
                preserved=preserved,
            )
        return TransportError(
            f"{self.device_id} transaction failed: {reply.detail or 'transport error'}",
            details=details,
            preserved="no reply was parsed; the bus is idle",
        )

    def _resolve_slave(self, slave: int | None) -> int:
        return self.default_slave if slave is None else slave

    # -- reads -------------------------------------------------------------

    async def _read_bits(
        self,
        ctx: ActionContext,
        action_name: str,
        function: int,
        params: ModbusReadBitsParams,
    ) -> dict[str, Any]:
        request = ModbusRequest(
            slave=params.slave,
            function=function,
            address=params.address,
            count=params.count,
        )
        reply, txn = await self._perform(ctx, action_name, request)
        bits = list(reply.bits[: params.count])
        kind = request.kind
        return {
            "slave": params.slave,
            "function": function,
            "function_name": request.function_name,
            "address": params.address,
            "reference": data_reference(kind, params.address),
            "count": params.count,
            "bits": bits,
            "packed_hex": bits_to_hex(bits),
            "values": [
                {
                    "address": params.address + index,
                    "reference": data_reference(kind, params.address + index),
                    "value": bit,
                }
                for index, bit in enumerate(bits)
            ],
            "transaction": txn.model_dump(mode="json"),
        }

    async def _read_registers(
        self,
        ctx: ActionContext,
        action_name: str,
        function: int,
        params: ModbusReadRegistersParams,
    ) -> dict[str, Any]:
        request = ModbusRequest(
            slave=params.slave,
            function=function,
            address=params.address,
            count=params.count,
        )
        reply, txn = await self._perform(ctx, action_name, request)
        registers = list(reply.registers[: params.count])
        kind = request.kind
        decoded = decode_registers(
            registers,
            address=params.address,
            kind=kind,
            word_order=params.word_order,
            byte_order=params.byte_order,
        )
        recorded = self._record_measurements(ctx, kind, params.address, decoded["values"])
        return {
            "slave": params.slave,
            "function": function,
            "function_name": request.function_name,
            "address": params.address,
            "reference": data_reference(kind, params.address),
            "count": params.count,
            "registers": registers,
            "decoded": decoded,
            "timeline_measurements": recorded,
            "transaction": txn.model_dump(mode="json"),
        }

    def _record_measurements(
        self,
        ctx: ActionContext,
        kind: RegisterKind,
        address: int,
        values: list[dict[str, Any]],
    ) -> int:
        """Put register values on the timeline so they can be correlated.

        A register that is polled during a fault is only useful if it lands on
        the same time axis as the PSU current and the CAN traffic.  Long reads
        are skipped: the array is still in the result and in the transaction.
        """
        if ctx.recorder is None or len(values) > MAX_TIMELINE_REGISTERS:
            return 0
        stamp = Timestamp.now()
        for entry in values:
            ctx.recorder.measurement(
                quantity=f"modbus.{kind}.{entry['address']}",
                value=float(entry["uint16"]),
                device_id=self.device_id,
                timestamp=stamp,
            )
        return len(values)

    # -- actions -----------------------------------------------------------

    @action(
        "modbus.status",
        permission=PermissionLevel.PASSIVE,
        params=DeviceParams,
        state_changing=False,
        description="Endpoint configuration and transaction counters; sends nothing.",
        allowed_during_estop=True,
    )
    async def modbus_status(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        """PASSIVE because it reports FieldDeck's own state, not the DUT's."""
        return await self.status()

    @action(
        "modbus.read_coils",
        permission=PermissionLevel.QUERY,
        params=ModbusReadBitsParams,
        state_changing=False,
        description="Read coils (function 0x01).",
        timeout_s=15.0,
    )
    async def modbus_read_coils(
        self, ctx: ActionContext, params: ModbusReadBitsParams
    ) -> dict[str, Any]:
        """QUERY, never PASSIVE: reading addresses a station on the bus."""
        return await self._read_bits(ctx, "modbus.read_coils", FUNCTION_READ_COILS, params)

    @action(
        "modbus.read_discrete",
        permission=PermissionLevel.QUERY,
        params=ModbusReadBitsParams,
        state_changing=False,
        description="Read discrete inputs (function 0x02).",
        timeout_s=15.0,
    )
    async def modbus_read_discrete(
        self, ctx: ActionContext, params: ModbusReadBitsParams
    ) -> dict[str, Any]:
        return await self._read_bits(ctx, "modbus.read_discrete", FUNCTION_READ_DISCRETE, params)

    @action(
        "modbus.read_holding",
        permission=PermissionLevel.QUERY,
        params=ModbusReadRegistersParams,
        state_changing=False,
        description="Read holding registers (function 0x03) with 16/32-bit decodes.",
        timeout_s=15.0,
    )
    async def modbus_read_holding(
        self, ctx: ActionContext, params: ModbusReadRegistersParams
    ) -> dict[str, Any]:
        """QUERY.

        ``state_changing`` is False because FieldDeck commands no change — but
        note that some vendors implement read-to-clear alarm registers, which
        is one more reason a read is authorised work rather than free.
        """
        return await self._read_registers(ctx, "modbus.read_holding", FUNCTION_READ_HOLDING, params)

    @action(
        "modbus.read_input",
        permission=PermissionLevel.QUERY,
        params=ModbusReadRegistersParams,
        state_changing=False,
        description="Read input registers (function 0x04) with 16/32-bit decodes.",
        timeout_s=15.0,
    )
    async def modbus_read_input(
        self, ctx: ActionContext, params: ModbusReadRegistersParams
    ) -> dict[str, Any]:
        return await self._read_registers(ctx, "modbus.read_input", FUNCTION_READ_INPUT, params)

    @action(
        "modbus.scan",
        permission=PermissionLevel.QUERY,
        params=ModbusScanParams,
        state_changing=False,
        description="Address scan: sends one read to each station in a bounded range.",
        cancelable=True,
        timeout_s=600.0,
    )
    async def modbus_scan(self, ctx: ActionContext, params: ModbusScanParams) -> dict[str, Any]:
        """QUERY: this actively addresses every station in the range.

        Bounded by :data:`MAX_SCAN_ADDRESSES` and by a per-address timeout, and
        it stops early on cancellation or when the action deadline approaches
        so the partial result survives.  A station that answers with an
        exception is reported as present: refusing a request is proof of life.
        """
        function = _SCAN_FUNCTIONS[params.probe]
        started = monotonic_ns()
        transactions: list[dict[str, Any]] = []
        answered: list[int] = []
        exceptions: list[dict[str, Any]] = []
        gateway: list[dict[str, Any]] = []
        silent: list[int] = []
        errors: list[dict[str, Any]] = []
        scanned = 0
        deadline_reached = False

        try:
            for slave in range(params.start, params.end + 1):
                if ctx.cancelled:
                    break
                remaining = ctx.remaining_s()
                if (
                    remaining is not None
                    and remaining <= params.per_address_timeout_s + _SCAN_DEADLINE_MARGIN_S
                ):
                    deadline_reached = True
                    break
                request = ModbusRequest(
                    slave=slave,
                    function=function,
                    address=params.address,
                    count=params.count,
                )
                _reply, txn = await self._perform(
                    ctx,
                    "modbus.scan",
                    request,
                    timeout_s=params.per_address_timeout_s,
                    raise_on_failure=False,
                    report_fault=False,
                )
                scanned += 1
                transactions.append(txn.model_dump(mode="json"))
                if txn.outcome == "ok":
                    answered.append(slave)
                elif txn.outcome == "exception":
                    entry = {
                        "slave": slave,
                        "exception_code": txn.exception_code,
                        "exception": txn.exception_name,
                    }
                    # A gateway exception is the *gateway* talking about a
                    # station it could not reach, so it is evidence of absence
                    # rather than presence.  Counting it as a hit is how a
                    # TCP-to-RTU bridge makes an empty bus look fully
                    # populated.
                    if txn.exception_code in _GATEWAY_EXCEPTIONS:
                        gateway.append(entry)
                    else:
                        exceptions.append(entry)
                elif txn.outcome == "timeout":
                    silent.append(slave)
                else:
                    errors.append({"slave": slave, "detail": txn.detail})
        except asyncio.CancelledError:
            # Cancellation destroys the return value, so the findings go to
            # the log before the exception continues on its way.
            _log.info(
                "modbus scan cancelled",
                extra={
                    "device": self.device_id,
                    "scanned": scanned,
                    "answered": answered,
                    "present": answered + [entry["slave"] for entry in exceptions],
                    "silent": silent,
                },
            )
            raise

        present = sorted({*answered, *(int(entry["slave"]) for entry in exceptions)})
        return {
            "range": {"start": params.start, "end": params.end},
            "probe": {
                "kind": params.probe,
                "function": function,
                "function_name": FUNCTION_NAMES[function],
                "address": params.address,
                "reference": data_reference(params.probe, params.address),
                "count": params.count,
                "per_address_timeout_s": params.per_address_timeout_s,
            },
            "scanned": scanned,
            "present": present,
            "answered": answered,
            "exception_responses": exceptions,
            "gateway_no_response": gateway,
            "silent": silent,
            "errors": errors,
            "cancelled": ctx.cancelled,
            "deadline_reached": deadline_reached,
            "duration_s": round((monotonic_ns() - started) / 1e9, 3),
            "transactions": transactions,
            "note": (
                "A station that answers with an exception is present; a gateway "
                "exception (0x0A/0x0B) is the bridge reporting that it could not "
                "reach one, so it is listed separately. Silence is not proof of "
                "absence either: wrong baud rate, wrong parity, wrong termination "
                "and a half-connected A/B pair all look identical."
            ),
        }

    @action(
        "modbus.write_coil",
        permission=PermissionLevel.CONTROL,
        params=ModbusWriteCoilParams,
        state_changing=True,
        description="Write a single coil (function 0x05).",
        safe_state_note=(
            "The coil keeps whatever it was last commanded; FieldDeck will not "
            "guess a safe value for a DUT it did not configure."
        ),
    )
    async def modbus_write_coil(
        self, ctx: ActionContext, params: ModbusWriteCoilParams
    ) -> dict[str, Any]:
        """CONTROL: a coil is usually wired to something that moves."""
        wire_value = 0xFF00 if params.value else 0x0000
        request = ModbusRequest(
            slave=params.slave,
            function=FUNCTION_WRITE_COIL,
            address=params.address,
            count=1,
            values=(wire_value,),
        )
        reply, txn = await self._perform(ctx, "modbus.write_coil", request)
        echoed = list(reply.registers[:1])
        return {
            "slave": params.slave,
            "function": FUNCTION_WRITE_COIL,
            "function_name": request.function_name,
            "address": params.address,
            "reference": data_reference("coils", params.address),
            "value": params.value,
            "wire_value": f"0x{wire_value:04X}",
            "echoed": [f"0x{value:04X}" for value in echoed],
            # A device that acknowledges a different value than it was sent is
            # the kind of thing that is obvious in hindsight and invisible
            # unless somebody checks.
            "echo_matches": echoed == [wire_value],
            "transaction": txn.model_dump(mode="json"),
        }

    @action(
        "modbus.write_register",
        permission=PermissionLevel.CONTROL,
        params=ModbusWriteRegisterParams,
        state_changing=True,
        description="Write a single holding register (function 0x06).",
        safe_state_note=(
            "The register keeps its written value; no safe default is invented "
            "on safe state or emergency stop."
        ),
    )
    async def modbus_write_register(
        self, ctx: ActionContext, params: ModbusWriteRegisterParams
    ) -> dict[str, Any]:
        """CONTROL: one register can be a setpoint, a mode or a command word."""
        word = _to_word(params.value)
        request = ModbusRequest(
            slave=params.slave,
            function=FUNCTION_WRITE_REGISTER,
            address=params.address,
            count=1,
            values=(word,),
        )
        reply, txn = await self._perform(ctx, "modbus.write_register", request)
        echoed = list(reply.registers[:1])
        return {
            "slave": params.slave,
            "function": FUNCTION_WRITE_REGISTER,
            "function_name": request.function_name,
            "address": params.address,
            "reference": data_reference("holding", params.address),
            "requested": params.value,
            "written": word,
            "written_hex": f"0x{word:04X}",
            "echoed": echoed,
            "echo_matches": echoed == [word],
            "transaction": txn.model_dump(mode="json"),
        }

    @action(
        "modbus.write_registers",
        permission=PermissionLevel.CONTROL,
        params=ModbusWriteRegistersParams,
        state_changing=True,
        description="Write consecutive holding registers (function 0x10).",
        safe_state_note=(
            "The registers keep their written values; no safe default is "
            "invented on safe state or emergency stop."
        ),
    )
    async def modbus_write_registers(
        self, ctx: ActionContext, params: ModbusWriteRegistersParams
    ) -> dict[str, Any]:
        """CONTROL.

        The words are written exactly as given.  Nothing here packs a float
        into two registers: that needs the vendor's word order, and encoding
        it silently is the write-side of the same mistake that misreads a
        pressure sensor by three orders of magnitude.
        """
        words = tuple(_to_word(value) for value in params.values)
        request = ModbusRequest(
            slave=params.slave,
            function=FUNCTION_WRITE_REGISTERS,
            address=params.address,
            count=len(words),
            values=words,
        )
        reply, txn = await self._perform(ctx, "modbus.write_registers", request)
        acknowledged = reply.registers[0] if reply.registers else len(words)
        return {
            "slave": params.slave,
            "function": FUNCTION_WRITE_REGISTERS,
            "function_name": request.function_name,
            "address": params.address,
            "reference": data_reference("holding", params.address),
            "requested": list(params.values),
            "written": list(words),
            "written_hex": [f"0x{word:04X}" for word in words],
            "acknowledged_count": acknowledged,
            "echo_matches": acknowledged == len(words),
            "transaction": txn.model_dump(mode="json"),
        }


# ---------------------------------------------------------------------------
# pymodbus-backed driver
# ---------------------------------------------------------------------------


def _pymodbus_clients() -> tuple[Any, Any, bool]:
    """Import pymodbus on demand and say whether it is the async API.

    Optional by design: the daemon imports and runs on a machine with no
    hardware libraries at all, so nothing from pymodbus is imported at module
    scope.  Both client families are accepted because which one a given
    pymodbus release ships has changed more than once, and a field unit is a
    bad place to discover a packaging difference.
    """
    try:
        from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient

        return AsyncModbusSerialClient, AsyncModbusTcpClient, True
    except ImportError:
        pass
    try:
        from pymodbus.client import ModbusSerialClient, ModbusTcpClient

        return ModbusSerialClient, ModbusTcpClient, False
    except ImportError as exc:  # pragma: no cover - exercised on installs without the extra
        raise UnsupportedCapability(
            "pymodbus is not installed; install it with: pip install 'fielddeck[modbus]'",
            details={"module": "pymodbus", "extra": "modbus"},
            preserved="nothing was opened and no frame was sent",
        ) from exc


def _station_kwarg(method: Any) -> str:
    """Which keyword this pymodbus release uses for the station address.

    pymodbus has called it ``unit``, then ``slave``, then ``device_id`` across
    3.x.  Getting it wrong silently addresses station 1 instead of the one the
    operator asked for, which is a write landing on the wrong machine, so it
    is read off the signature rather than assumed.
    """
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):  # pragma: no cover - C-implemented callable
        return "slave"
    for name in ("slave", "device_id", "unit"):
        if name in parameters:
            return name
    return "slave"


@dataclass(slots=True)
class _ClientState:
    client: Any = None
    is_async: bool = False
    station_kwarg: dict[str, str] = field(default_factory=dict)


class ModbusDriver(ModbusDriverBase):
    """A configured Modbus endpoint, RTU over serial or TCP over a socket.

    The client is created and connected lazily, on the first transaction, so
    discovery never opens a serial port — opening one toggles DTR/RTS on most
    USB adapters, which resets a fair number of DUTs.
    """

    def __init__(self, endpoint: ModbusEndpoint, *, warning: str | None = None) -> None:
        stable = endpoint.transport == "tcp" or _is_stable_serial_path(endpoint.serial_port)
        descriptor = DeviceDescriptor(
            id=endpoint.device_id,
            kind=TransportKind.MODBUS,
            display_name=endpoint.label or f"Modbus {endpoint.transport.upper()} {endpoint.name}",
            path=endpoint.location,
            roles=[DeviceRole.BUS],
            capabilities=[
                DeviceCapability.RX,
                DeviceCapability.TX,
                DeviceCapability.DECODE,
            ],
            # PASSIVE: the endpoint's presence transmits nothing.  Every action
            # that does touch the bus declares QUERY or CONTROL for itself.
            permission_floor=PermissionLevel.PASSIVE,
            state=ConnectionState.DISCOVERED,
            stable_id=stable,
            warning=warning,
            metadata={
                **endpoint.summary(),
                "passive_observation": (
                    "listening to an RTU line without transmitting is serial.monitor "
                    "on the underlying port, not a Modbus action"
                ),
            },
        )
        super().__init__(
            descriptor,
            default_slave=endpoint.default_slave,
            timeout_s=endpoint.timeout_s,
        )
        self.endpoint = endpoint
        self._state = _ClientState()

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        await self._ensure_client()
        self._set_state(ConnectionState.READY)

    async def disconnect(self) -> None:
        client = self._state.client
        self._state.client = None
        if client is not None:
            closer = getattr(client, "close", None)
            if closer is not None:
                try:
                    outcome = closer()
                    if inspect.isawaitable(outcome):
                        await outcome
                except Exception as exc:  # noqa: BLE001 - a failed close must not block teardown
                    _log.warning(
                        "modbus close failed",
                        extra={"device": self.device_id, "error": str(exc)},
                    )
        self._set_state(ConnectionState.DISCOVERED)

    async def _endpoint_status(self) -> dict[str, Any]:
        client = self._state.client
        return {
            "endpoint": self.endpoint.summary(),
            "connected": bool(client is not None and getattr(client, "connected", False)),
            "client": type(client).__name__ if client is not None else None,
            "client_api": "async" if self._state.is_async else "threaded",
            "state": str(self._descriptor.state),
        }

    def _create_client(self) -> Any:
        serial_cls, tcp_cls, is_async = _pymodbus_clients()
        self._state.is_async = is_async
        client_cls = tcp_cls if self.endpoint.transport == "tcp" else serial_cls
        # pymodbus retries a request three times by default.  A silent retry
        # turns "the request was sent once" into a lie and can apply a
        # non-idempotent write twice, so a transaction here is one attempt and
        # the operator decides whether to repeat it.
        extra: dict[str, Any] = {}
        try:
            if "retries" in inspect.signature(client_cls).parameters:
                extra["retries"] = 1
        except (TypeError, ValueError):  # pragma: no cover - unintrospectable ctor
            pass
        if self.endpoint.transport == "tcp":
            return client_cls(
                host=self.endpoint.host,
                port=self.endpoint.port,
                timeout=self.endpoint.timeout_s,
                **extra,
            )
        return client_cls(
            port=self.endpoint.serial_port,
            baudrate=self.endpoint.baudrate,
            bytesize=self.endpoint.bytesize,
            parity=self.endpoint.parity,
            stopbits=self.endpoint.stopbits,
            timeout=self.endpoint.timeout_s,
            **extra,
        )

    async def _ensure_client(self) -> Any:
        if self._state.client is None:
            self._state.client = self._create_client()
        client = self._state.client
        if getattr(client, "connected", False):
            return client
        try:
            connected = await self._call(client.connect)
        except Exception as exc:  # pymodbus raises a version-dependent family here
            raise TransportError(
                f"cannot reach {self.endpoint.location}: {exc}",
                details={"device_id": self.device_id, "endpoint": self.endpoint.summary()},
                preserved="no frame was sent",
            ) from exc
        if connected is False and not getattr(client, "connected", False):
            raise TransportError(
                f"cannot reach {self.endpoint.location}",
                details={"device_id": self.device_id, "endpoint": self.endpoint.summary()},
                preserved="no frame was sent",
            )
        return client

    async def _call(self, method: Any, /, **kwargs: Any) -> Any:
        """Call a pymodbus method, whichever concurrency model it uses.

        The synchronous clients block for the whole transaction — hundreds of
        milliseconds on a slow RTU link, which is far too long to hold the
        event loop while other devices may be energised — so they run in a
        worker thread.

        Awaitability is checked on the *result*, not on the callable: in
        pymodbus 3.x only ``connect()`` is a coroutine function, while the
        read and write methods are plain functions that return an awaitable.
        Testing the callable with :func:`inspect.iscoroutinefunction` would
        therefore hand back an un-awaited coroutine and report an empty,
        successful-looking read.
        """
        if not self._state.is_async:
            return await asyncio.to_thread(lambda: method(**kwargs))
        outcome = method(**kwargs)
        if inspect.isawaitable(outcome):
            return await outcome
        return outcome

    # -- the one transport method -----------------------------------------

    async def _transact(self, request: ModbusRequest, *, timeout_s: float) -> ModbusReply:
        try:
            client = await self._ensure_client()
        except TransportError as exc:
            return ModbusReply(outcome="error", detail=exc.message)

        method_name = _METHOD_FOR_FUNCTION[request.function]
        method = getattr(client, method_name, None)
        if method is None:  # pragma: no cover - defensive against API drift
            return ModbusReply(
                outcome="error",
                detail=f"this pymodbus client has no {method_name}()",
            )
        kwargs = self._kwargs_for(request, method, method_name)
        try:
            # The client has its own timeout, but a client wedged below that
            # level would otherwise hold the bus lock indefinitely.  The extra
            # second gives pymodbus room to report its own timeout first, which
            # produces the better error message.
            response = await asyncio.wait_for(self._call(method, **kwargs), timeout=timeout_s + 1.0)
        except TimeoutError:
            return ModbusReply(outcome="timeout", detail="no response within the client timeout")
        except Exception as exc:  # noqa: BLE001 - pymodbus exception types vary by version and transport
            return _reply_from_exception(exc)
        return _reply_from_response(response, request)

    def _kwargs_for(self, request: ModbusRequest, method: Any, method_name: str) -> dict[str, Any]:
        station = self._state.station_kwarg.get(method_name)
        if station is None:
            station = _station_kwarg(method)
            self._state.station_kwarg[method_name] = station
        kwargs: dict[str, Any] = {"address": request.address, station: request.slave}
        if request.function == FUNCTION_WRITE_COIL:
            kwargs["value"] = bool(request.values and request.values[0])
        elif request.function == FUNCTION_WRITE_REGISTER:
            kwargs["value"] = request.values[0]
        elif request.function == FUNCTION_WRITE_REGISTERS:
            kwargs["values"] = list(request.values)
        else:
            kwargs["count"] = request.count
        return kwargs


_METHOD_FOR_FUNCTION: dict[int, str] = {
    FUNCTION_READ_COILS: "read_coils",
    FUNCTION_READ_DISCRETE: "read_discrete_inputs",
    FUNCTION_READ_HOLDING: "read_holding_registers",
    FUNCTION_READ_INPUT: "read_input_registers",
    FUNCTION_WRITE_COIL: "write_coil",
    FUNCTION_WRITE_REGISTER: "write_register",
    FUNCTION_WRITE_REGISTERS: "write_registers",
}

#: Substrings pymodbus uses when nothing answered, as opposed to when the
#: link itself broke.  The distinction matters: silence means "check baud,
#: parity, termination and address", a broken link means "check the cable".
_SILENCE_HINTS = ("no response", "timeout", "timed out")


def _reply_from_exception(exc: Exception) -> ModbusReply:
    text = str(exc) or type(exc).__name__
    lowered = text.lower()
    if any(hint in lowered for hint in _SILENCE_HINTS):
        return ModbusReply(outcome="timeout", detail=text)
    return ModbusReply(outcome="error", detail=text)


def _reply_from_response(response: Any, request: ModbusRequest) -> ModbusReply:
    if response is None:
        return ModbusReply(outcome="error", detail="pymodbus returned no response object")

    is_error = bool(getattr(response, "isError", lambda: False)())
    exception_code = getattr(response, "exception_code", None)
    if is_error:
        if isinstance(exception_code, int) and exception_code:
            return ModbusReply(
                outcome="exception",
                exception_code=exception_code,
                detail=str(response),
            )
        return _reply_from_exception(
            response if isinstance(response, Exception) else RuntimeError(str(response))
        )

    registers = tuple(int(value) & 0xFFFF for value in getattr(response, "registers", ()) or ())
    bits = tuple(bool(value) for value in getattr(response, "bits", ()) or ())
    if request.is_write and not registers:
        # Write responses echo the request; pymodbus models them with the
        # request's own attributes rather than a registers list.
        echoed = getattr(response, "value", None)
        count = getattr(response, "count", None)
        if request.function == FUNCTION_WRITE_REGISTERS and isinstance(count, int):
            registers = (count,)
        elif isinstance(echoed, bool):
            registers = (0xFF00 if echoed else 0x0000,)
        elif isinstance(echoed, int):
            registers = (echoed & 0xFFFF,)
        else:
            registers = tuple(request.values)
    return ModbusReply(outcome="ok", registers=registers, bits=bits)


def _is_stable_serial_path(path: str | None) -> bool:
    """``/dev/ttyUSB0`` is a queue position, not an identity."""
    if not path:
        return False
    return path.startswith(("/dev/serial/by-id/", "/dev/serial/by-path/"))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_modbus_drivers(config: FieldDeckConfig, paths: Paths | None = None) -> list[Driver]:
    """Build drivers for the endpoints an operator declared.

    Modbus deliberately has no autodetection.  Nothing on a serial port
    announces that it speaks Modbus, and the only way to find out is to
    transmit at a guessed baud rate to a guessed address — which is a QUERY
    act against unknown hardware, dressed up as discovery.  So this reads
    ``<config>/instruments/modbus.yaml`` and creates exactly what is in it.
    Use ``modbus.scan``, with authorization, to find stations on a bus the
    operator has already told FieldDeck about.

    Without pymodbus there is nothing that could drive an endpoint, so the
    provider returns nothing and logs why.
    """
    try:
        _pymodbus_clients()
    except UnsupportedCapability as exc:
        _log.info(
            "pymodbus missing; configured Modbus endpoints are not drivable",
            extra={"reason": exc.message},
        )
        return []

    endpoints = load_modbus_endpoints(paths)
    if not endpoints:
        return []

    # Imported here so the protocols package never depends on the discovery
    # package at import time; this is the only place it is needed.
    from fielddeck.discovery.linux import list_serial_ports

    serial_paths = {str(entry.get("path")) for entry in list_serial_ports()}
    drivers: list[Driver] = []
    for endpoint in endpoints:
        warning: str | None = None
        if endpoint.transport == "rtu":
            if endpoint.serial_port in serial_paths:
                # Both drivers can open the same port; the bytes then
                # interleave and both sides see corruption that looks like a
                # wiring fault.  Say so up front.
                warning = (
                    f"{endpoint.serial_port} is also enumerated as a serial device; "
                    "do not run a serial monitor and Modbus transactions on it at once"
                )
            elif not _is_stable_serial_path(endpoint.serial_port):
                warning = (
                    f"{endpoint.serial_port} is not a /dev/serial/by-id path, so it may "
                    "point at a different adapter after a re-plug"
                )
        drivers.append(ModbusDriver(endpoint, warning=warning))
    _log.info(
        "modbus endpoints configured",
        extra={"count": len(drivers), "devices": [driver.device_id for driver in drivers]},
    )
    return drivers
