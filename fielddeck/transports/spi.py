"""SPI through spidev.

There is deliberately no passive SPI action.  SPI has no idle read: every
transfer asserts chip select and clocks bytes out of MOSI, so "just reading a
register" drives three lines on the DUT.  Calling that PASSIVE would be a lie
the permission model would then repeat to the operator, so ``spi.transfer``
is CONTROL and there is no read-only alternative.

Mode, bit order and clock rate are never guessed.  A device clocked at the
wrong mode returns plausible-looking rubbish rather than an error, which is
exactly the failure that wastes an afternoon.
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

__all__ = ["SpiDriver", "discover_spi_drivers"]

ELECTRICAL_WARNING = (
    "SPI on the Pi is 3.3 V, unisolated, and every transfer drives SCLK, MOSI "
    "and CS. There is no passive SPI read. Confirm logic levels, mode and "
    "chip-select polarity against the datasheet before transferring."
)


def _spidev() -> Any:
    try:
        import spidev  # type: ignore[import-not-found]
    except ImportError as exc:
        raise UnsupportedCapability(
            "spidev is not installed; install with: pip install 'fielddeck[gpio]'",
            details={"module": "spidev"},
        ) from exc
    return spidev


class SpiTransferParams(DeviceParams):
    #: Bytes clocked out on MOSI. The same number are clocked in on MISO.
    data: str = Field(description="Payload as hex bytes")
    speed_hz: int = Field(default=1_000_000, ge=1000, le=50_000_000)
    #: SPI mode 0-3: CPOL/CPHA. Wrong mode returns plausible garbage, not an error.
    mode: int = Field(default=0, ge=0, le=3)
    bits_per_word: int = Field(default=8, ge=4, le=32)
    lsb_first: bool = False


class SpiDriver(Driver):
    kind = TransportKind.SPI

    def __init__(self, *, path: str, bus: int, device: int) -> None:
        descriptor = DeviceDescriptor(
            id=f"spi:dev:spidev{bus}.{device}",
            kind=TransportKind.SPI,
            display_name=f"SPI {bus}.{device}",
            path=path,
            roles=[DeviceRole.BUS],
            capabilities=[DeviceCapability.RX, DeviceCapability.TX],
            state=ConnectionState.DISCOVERED,
            warning=ELECTRICAL_WARNING,
            metadata={"bus": bus, "device": device, "voltage": "3.3V", "isolated": False},
        )
        super().__init__(descriptor)
        self.bus = bus
        self.device = device

    async def status(self) -> dict[str, Any]:
        return {
            "path": self._descriptor.path,
            "bus": self.bus,
            "device": self.device,
            "voltage": "3.3V",
            "isolated": False,
            "warning": ELECTRICAL_WARNING,
            "note": "no passive read exists on SPI; every transfer drives the bus",
        }

    async def safe_state(self) -> dict[str, Any]:
        return {
            "device": self.device_id,
            "applied": True,
            "changed": False,
            "state": "no transfer in progress; chip select is released",
        }

    @action(
        "spi.info",
        permission=PermissionLevel.PASSIVE,
        params=DeviceParams,
        state_changing=False,
        description="SPI device node availability. Performs no transfer.",
        allowed_during_estop=True,
    )
    async def spi_info(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        return await self.status()

    @action(
        "spi.transfer",
        permission=PermissionLevel.CONTROL,
        params=SpiTransferParams,
        state_changing=True,
        description="Full-duplex transfer. Clocks bytes out and reads bytes in.",
        timeout_s=15.0,
        safe_state_note="Chip select is released after every transfer.",
    )
    async def spi_transfer(self, ctx: ActionContext, params: SpiTransferParams) -> dict[str, Any]:
        """CONTROL: even a 'read' asserts CS and clocks data at the device."""
        spidev = _spidev()
        payload = list(bytes.fromhex(params.data.replace(" ", "").replace("0x", "")))

        def _transfer() -> list[int]:
            handle = spidev.SpiDev()
            handle.open(self.bus, self.device)
            try:
                handle.max_speed_hz = params.speed_hz
                handle.mode = params.mode
                handle.bits_per_word = params.bits_per_word
                handle.lsbfirst = params.lsb_first
                return list(handle.xfer2(list(payload)))
            finally:
                handle.close()

        try:
            received = await asyncio.to_thread(_transfer)
        except OSError as exc:
            raise ProtocolError(
                f"SPI transfer on {self._descriptor.path} failed: {exc}",
                details={"bus": self.bus, "device": self.device, "mode": params.mode},
                preserved="chip select was released",
            ) from exc
        return {
            "sent": bytes(payload).hex().upper(),
            "received": bytes(received).hex().upper(),
            "bytes": len(received),
            "mode": params.mode,
            "speed_hz": params.speed_hz,
            "warning": ELECTRICAL_WARNING,
        }


def discover_spi_drivers(config: FieldDeckConfig) -> list[Driver]:
    """List /dev/spidev* without opening any of them."""
    drivers: list[Driver] = []
    dev = Path("/dev")
    if not dev.is_dir():
        return drivers
    for node in sorted(dev.glob("spidev*")):
        suffix = node.name.removeprefix("spidev")
        if "." not in suffix:
            continue
        bus_part, _, device_part = suffix.partition(".")
        if not (bus_part.isdigit() and device_part.isdigit()):
            continue
        drivers.append(SpiDriver(path=str(node), bus=int(bus_part), device=int(device_part)))
    return drivers
