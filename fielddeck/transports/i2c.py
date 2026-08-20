"""I2C through the Linux i2c-dev interface.

Note the permission level on the scan: **an I2C scan is QUERY, not PASSIVE**.
Scanning drives the bus, addresses every device on it and waits for an ACK.
On a well-behaved sensor that is harmless; on a device whose address happens
to collide with a write-protected EEPROM control word, or on a bus shared
with something mid-transaction, it is not. FieldDeck will not do it without
authorization, and will not do it as a side effect of discovery.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import Field

from fielddeck.common.config import FieldDeckConfig
from fielddeck.common.errors import ProtocolError, UnsupportedCapability
from fielddeck.common.models import (
    ConnectionState,
    DeviceCapability,
    DeviceDescriptor,
    DeviceRole,
    PermissionLevel,
    TransportKind,
)
from fielddeck.drivers.base import ActionContext, DeviceParams, Driver, action

__all__ = ["I2cDriver", "discover_i2c_drivers"]

ELECTRICAL_WARNING = (
    "I2C on the Pi is 3.3 V with on-board pull-ups and no isolation. A 5 V bus, "
    "a second set of pull-ups, or a long cable will all misbehave in ways "
    "software cannot detect. Confirm levels and pull-ups physically."
)

#: Addresses outside this range are reserved by the I2C specification.
_FIRST_ADDRESS = 0x08
_LAST_ADDRESS = 0x77


def _smbus() -> Any:
    try:
        from smbus2 import SMBus, i2c_msg  # type: ignore[import-not-found]
    except ImportError as exc:
        raise UnsupportedCapability(
            "smbus2 is not installed; install with: pip install 'fielddeck[gpio]'",
            details={"module": "smbus2"},
        ) from exc
    return SMBus, i2c_msg


class I2cScanParams(DeviceParams):
    first: int = Field(default=_FIRST_ADDRESS, ge=0x00, le=0x7F)
    last: int = Field(default=_LAST_ADDRESS, ge=0x00, le=0x7F)


class I2cReadParams(DeviceParams):
    address: int = Field(ge=0x00, le=0x7F)
    # Named register_address rather than 'register': a pydantic field called
    # 'register' shadows ABCMeta.register on the model class.
    register_address: int | None = Field(default=None, ge=0, le=0xFFFF)
    length: int = Field(default=1, ge=1, le=256)


class I2cWriteParams(DeviceParams):
    address: int = Field(ge=0x00, le=0x7F)
    # Named register_address rather than 'register': a pydantic field called
    # 'register' shadows ABCMeta.register on the model class.
    register_address: int | None = Field(default=None, ge=0, le=0xFFFF)
    data: str = Field(description="Payload as hex bytes")


class I2cDriver(Driver):
    kind = TransportKind.I2C

    def __init__(self, *, path: str, bus_number: int, name: str | None = None) -> None:
        descriptor = DeviceDescriptor(
            id=f"i2c:dev:i2c-{bus_number}",
            kind=TransportKind.I2C,
            display_name=name or f"I2C bus {bus_number}",
            path=path,
            roles=[DeviceRole.BUS],
            capabilities=[DeviceCapability.RX, DeviceCapability.TX],
            state=ConnectionState.DISCOVERED,
            warning=ELECTRICAL_WARNING,
            metadata={"bus": bus_number, "voltage": "3.3V", "isolated": False},
        )
        super().__init__(descriptor)
        self.bus_number = bus_number

    async def status(self) -> dict[str, Any]:
        return {
            "bus": self.bus_number,
            "path": self._descriptor.path,
            "voltage": "3.3V",
            "isolated": False,
            "warning": ELECTRICAL_WARNING,
        }

    async def safe_state(self) -> dict[str, Any]:
        # I2C is transaction-based: between transactions nothing is driven, so
        # there is no output to turn off.
        return {
            "device": self.device_id,
            "applied": True,
            "changed": False,
            "state": "idle between transactions; nothing is driven",
        }

    @action(
        "i2c.info",
        permission=PermissionLevel.PASSIVE,
        params=DeviceParams,
        state_changing=False,
        description="Bus availability. Does not address any device.",
        allowed_during_estop=True,
    )
    async def i2c_info(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        return await self.status()

    @action(
        "i2c.scan",
        permission=PermissionLevel.QUERY,
        params=I2cScanParams,
        state_changing=False,
        description="Probe the address range for devices that acknowledge.",
        timeout_s=60.0,
    )
    async def i2c_scan(self, ctx: ActionContext, params: I2cScanParams) -> dict[str, Any]:
        """QUERY, because scanning actively addresses every device on the bus."""
        smbus_cls, i2c_msg = _smbus()

        def _scan() -> list[int]:
            found: list[int] = []
            with smbus_cls(self.bus_number) as bus:
                for address in range(params.first, params.last + 1):
                    try:
                        # A zero-length write is the least intrusive probe
                        # available: it addresses the device and stops.
                        bus.i2c_rdwr(i2c_msg.write(address, []))
                    except OSError:
                        continue
                    found.append(address)
            return found

        found = await asyncio.to_thread(_scan)
        return {
            "bus": self.bus_number,
            "found": [f"0x{address:02X}" for address in found],
            "count": len(found),
            "scanned": f"0x{params.first:02X}-0x{params.last:02X}",
            "warning": ELECTRICAL_WARNING,
        }

    @action(
        "i2c.read",
        permission=PermissionLevel.QUERY,
        params=I2cReadParams,
        state_changing=False,
        description="Read bytes from a device, optionally from a register.",
        timeout_s=15.0,
    )
    async def i2c_read(self, ctx: ActionContext, params: I2cReadParams) -> dict[str, Any]:
        smbus_cls, i2c_msg = _smbus()

        def _read() -> bytes:
            with smbus_cls(self.bus_number) as bus:
                if params.register_address is None:
                    message = i2c_msg.read(params.address, params.length)
                    bus.i2c_rdwr(message)
                    return bytes(list(message))
                write = i2c_msg.write(params.address, [params.register_address & 0xFF])
                read = i2c_msg.read(params.address, params.length)
                bus.i2c_rdwr(write, read)
                return bytes(list(read))

        try:
            data = await asyncio.to_thread(_read)
        except OSError as exc:
            raise ProtocolError(
                f"no response from 0x{params.address:02X} on i2c-{self.bus_number}: {exc}",
                details={"address": params.address, "bus": self.bus_number},
                preserved="the bus was left idle",
            ) from exc
        return {
            "address": f"0x{params.address:02X}",
            "register": (
                f"0x{params.register_address:02X}" if params.register_address is not None else None
            ),
            "hex": data.hex().upper(),
            "bytes": list(data),
            "length": len(data),
        }

    @action(
        "i2c.write",
        permission=PermissionLevel.CONTROL,
        params=I2cWriteParams,
        state_changing=True,
        description="Write bytes to a device, optionally to a register.",
        timeout_s=15.0,
        safe_state_note="I2C writes are transactional; there is no sustained output to disable.",
    )
    async def i2c_write(self, ctx: ActionContext, params: I2cWriteParams) -> dict[str, Any]:
        """CONTROL: this changes state inside the DUT."""
        smbus_cls, i2c_msg = _smbus()
        payload = bytes.fromhex(params.data.replace(" ", "").replace("0x", ""))
        prefix = [params.register_address & 0xFF] if params.register_address is not None else []

        def _write() -> None:
            with smbus_cls(self.bus_number) as bus:
                bus.i2c_rdwr(i2c_msg.write(params.address, prefix + list(payload)))

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            raise ProtocolError(
                f"write to 0x{params.address:02X} failed: {exc}",
                details={"address": params.address, "bus": self.bus_number},
                preserved="the transaction was not acknowledged; the device may be unchanged",
            ) from exc
        return {
            "address": f"0x{params.address:02X}",
            "register": (
                f"0x{params.register_address:02X}" if params.register_address is not None else None
            ),
            "written": len(payload),
            "hex": payload.hex().upper(),
        }


def discover_i2c_drivers(config: FieldDeckConfig) -> list[Driver]:
    """List /dev/i2c-* without opening any bus."""
    drivers: list[Driver] = []
    dev = Path("/dev")
    if not dev.is_dir():
        return drivers
    for node in sorted(dev.glob("i2c-*")):
        suffix = node.name.removeprefix("i2c-")
        if not suffix.isdigit():
            continue
        name_file = Path(f"/sys/class/i2c-dev/{node.name}/name")
        name = name_file.read_text().strip() if name_file.exists() else None
        drivers.append(I2cDriver(path=str(node), bus_number=int(suffix), name=name))
    return drivers
