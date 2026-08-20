"""A simulated Modbus RTU slave.

This exists so the whole Modbus path — parameters, authorization, the bus
lock, transaction logging, register decoding, the address scan — can be
exercised on a laptop with nothing plugged in.  It is not a mock: it subclasses
the same :class:`~fielddeck.protocols.modbus.ModbusDriverBase` the pymodbus
driver does, so the action set and the permission mapping are literally the
same objects.  Only :meth:`_transact` differs.

The register map is modelled on a small pump/heater controller, and it is
deliberately awkward in the ways real devices are:

* holding 30 is a **signed** temperature in tenths, which spends part of its
  cycle below zero — read as ``uint16`` it reads about 6500 degrees, which is
  exactly the misreading the decoder's ``int16`` column exists to prevent
* pressure and flow are **float32 pairs**, so a client that guesses the word
  order gets a plausible-looking wrong answer rather than an obvious one
* the energy total is a **uint32 pair** that only advances while the pump coil
  is on, so a CONTROL write has a visible, causal effect on later reads
* writing a read-only register or an address past the end of the map returns
  a proper exception response rather than silence, and every other station on
  the bus stays silent — which is what makes ``modbus.scan`` worth running

Values are derived from elapsed time plus a seeded RNG, so a session that saw
a particular reading sees it again on the next run.
"""

from __future__ import annotations

import asyncio
import math
import struct
from typing import Any

from fielddeck.common.models import (
    ConnectionState,
    DeviceCapability,
    DeviceDescriptor,
    DeviceRole,
    PermissionLevel,
    TransportKind,
)
from fielddeck.drivers.base import Driver
from fielddeck.protocols.modbus import (
    FUNCTION_READ_COILS,
    FUNCTION_READ_DISCRETE,
    FUNCTION_READ_HOLDING,
    FUNCTION_READ_INPUT,
    FUNCTION_WRITE_COIL,
    FUNCTION_WRITE_REGISTER,
    FUNCTION_WRITE_REGISTERS,
    ModbusDriverBase,
    ModbusReply,
    ModbusRequest,
)
from fielddeck.sim.base import SimulatedDeviceMixin, seeded_random

__all__ = ["SimModbusDriver", "build_simulated_modbus_devices"]

#: The one station that answers.  Every other address on the simulated bus is
#: silent, so a scan reports one present address and the rest as timeouts.
SIM_SLAVE_ID = 1

#: Sizes of the four data tables.  Reads past these return IllegalDataAddress,
#: which is what a real device does and what a scan needs to see.
_HOLDING_COUNT = 32
_INPUT_COUNT = 16
_COIL_COUNT = 8
_DISCRETE_COUNT = 8

#: Registers the controller reports but will not let anyone change.
_READ_ONLY_HOLDING = frozenset({0, 1, 2, 10, 11, 12, 13, 20, 21, 30})

#: Holding registers that carry the write-side model.
_REG_SETPOINT = 3
_REG_MODE = 4
_REG_PUMP_SPEED = 5

_SETPOINT_MIN = 0
_SETPOINT_MAX = 1000  # tenths of a degree: 0.0 .. 100.0 C
_MODE_MAX = 2  # 0 off, 1 auto, 2 manual

_COIL_PUMP = 0
_COIL_VALVE = 1
_COIL_HEATER = 2

_MODEL_CODE = 0x4644  # "FD"
_FIRMWARE = 0x0104  # v1.4

#: One frame out and one frame back at 19200 8E1 is a few milliseconds.  Long
#: enough to be a real await, short enough that a test suite stays quick.
_TURNAROUND_S = 0.004

#: What a silent address costs.  A real scan waits out the full per-address
#: timeout; burning that here would make a 16-address scan take five seconds
#: of wall clock for no extra coverage, so the sim answers silence quickly and
#: says so in its status.
_SILENCE_S = 0.02

#: Wire encodings for a single-coil write; anything else is IllegalDataValue.
_COIL_ON = 0xFF00
_COIL_OFF = 0x0000


class SimModbusDriver(SimulatedDeviceMixin, ModbusDriverBase):
    """A virtual Modbus RTU controller at station 1 on a virtual RS-485 bus."""

    def __init__(self, name: str = "sim-rtu-0") -> None:
        descriptor = DeviceDescriptor(
            id=f"sim:modbus:{name}",
            kind=TransportKind.MODBUS,
            display_name="Simulated Modbus RTU controller",
            path=f"/dev/null#{name}",
            vendor="FieldDeck",
            product="SIM-MB-100",
            serial_number="SIMMB0001",
            roles=[DeviceRole.BUS],
            capabilities=[
                DeviceCapability.RX,
                DeviceCapability.TX,
                DeviceCapability.DECODE,
            ],
            permission_floor=PermissionLevel.PASSIVE,
            state=ConnectionState.READY,
            simulated=True,
            metadata={
                "transport": "rtu",
                "framing": "19200 8E1",
                "station": SIM_SLAVE_ID,
                "map": {
                    "holding": f"0..{_HOLDING_COUNT - 1} (40001..)",
                    "input": f"0..{_INPUT_COUNT - 1} (30001..)",
                    "coils": f"0..{_COIL_COUNT - 1} (00001..)",
                    "discrete": f"0..{_DISCRETE_COUNT - 1} (10001..)",
                },
            },
        )
        ModbusDriverBase.__init__(self, descriptor, default_slave=SIM_SLAVE_ID, timeout_s=1.0)
        SimulatedDeviceMixin.__init__(self)
        self.name = name
        self._rng = seeded_random(descriptor.id)
        self._setpoint = 250  # 25.0 C
        self._mode = 1  # auto
        self._pump_speed = 600  # rpm-ish, writable
        self._coils = [False] * _COIL_COUNT
        self._energy_wh = 0.0
        self._energy_marker_s = 0.0

    # -- the physical model ------------------------------------------------

    def _advance_energy(self) -> None:
        """Integrate the energy counter up to now.

        Done on read rather than on a timer: nothing here should keep a task
        alive when no one is looking at the device.
        """
        now = self.sim_elapsed_s
        elapsed = max(0.0, now - self._energy_marker_s)
        self._energy_marker_s = now
        if self._coils[_COIL_PUMP]:
            # ~180 W while the pump runs, plus the heater when it is on.
            watts = 180.0 + (900.0 if self._coils[_COIL_HEATER] else 0.0)
            self._energy_wh += watts * elapsed / 3600.0

    @property
    def _pressure_bar(self) -> float:
        if not self._coils[_COIL_PUMP]:
            return 0.02 + self._rng.uniform(-0.002, 0.002)
        base = 1.9 + 0.18 * math.sin(self.sim_elapsed_s / 3.0)
        if not self._coils[_COIL_VALVE]:
            base += 0.35  # deadheaded against a closed valve
        return base + self._rng.uniform(-0.01, 0.01)

    @property
    def _flow_lpm(self) -> float:
        if not self._coils[_COIL_PUMP] or not self._coils[_COIL_VALVE]:
            return 0.0
        return (
            self._pump_speed / 60.0
            + 0.4 * math.sin(self.sim_elapsed_s / 5.0)
            + self._rng.uniform(-0.05, 0.05)
        )

    @property
    def _temperature_tenths(self) -> int:
        """Signed on purpose: this is the register that catches uint16 bugs."""
        target = self._setpoint if self._coils[_COIL_HEATER] else -80
        swing = 45.0 * math.sin(self.sim_elapsed_s / 7.0)
        return round(target * 0.9 + swing + self._rng.uniform(-1.5, 1.5))

    @staticmethod
    def _float_words(value: float) -> tuple[int, int]:
        """IEEE-754 float32 as (high word, low word) — big word order."""
        high, low = struct.unpack(">HH", struct.pack(">f", value))
        return high, low

    def _holding_table(self) -> list[int]:
        self._advance_energy()
        table = [0] * _HOLDING_COUNT
        table[0] = _MODEL_CODE
        table[1] = _FIRMWARE
        table[2] = int(self.sim_elapsed_s) & 0xFFFF
        table[_REG_SETPOINT] = self._setpoint
        table[_REG_MODE] = self._mode
        table[_REG_PUMP_SPEED] = self._pump_speed
        table[10], table[11] = self._float_words(self._pressure_bar)
        table[12], table[13] = self._float_words(self._flow_lpm)
        energy = int(self._energy_wh) & 0xFFFF_FFFF
        table[20], table[21] = energy >> 16, energy & 0xFFFF
        table[30] = self._temperature_tenths & 0xFFFF
        return table

    def _input_table(self) -> list[int]:
        table = [0] * _INPUT_COUNT
        # Raw ADC counts behind the pressure reading, so a client can check
        # the vendor's scaling against the engineering value.
        table[0] = max(0, min(4095, int(self._pressure_bar / 4.0 * 4095)))
        table[1] = 24_100 + self._rng.randint(-60, 60)  # supply mV
        current = 0.42 + (1.9 if self._coils[_COIL_PUMP] else 0.0)
        table[2], table[3] = self._float_words(current)
        table[4] = (-67 + self._rng.randint(-3, 3)) & 0xFFFF  # RS-485 bias, dBm-ish, signed
        table[5] = 0x0001 if self._pressure_bar > 2.1 else 0x0000
        return table

    def _discrete_table(self) -> list[bool]:
        return [
            self._pressure_bar > 2.1,  # high pressure switch
            self._flow_lpm > 1.0,  # flow proven
            self._temperature_tenths > self._setpoint + 100,  # over-temperature latch
            True,  # enclosure door closed
            self._coils[_COIL_PUMP],
            self._coils[_COIL_VALVE],
            False,
            False,
        ]

    # -- driver contract ---------------------------------------------------

    async def _endpoint_status(self) -> dict[str, Any]:
        self._advance_energy()
        return {
            "endpoint": {
                "name": self.name,
                "transport": "rtu (simulated)",
                "location": f"/dev/null#{self.name}",
                "framing": "19200 8E1",
                "default_slave": SIM_SLAVE_ID,
            },
            "connected": True,
            "client": "SimModbusDriver",
            "client_api": "in-process",
            "state": str(self._descriptor.state),
            "station": SIM_SLAVE_ID,
            "uptime_s": round(self.sim_elapsed_s, 3),
            "model": {
                "setpoint_c": self._setpoint / 10.0,
                "mode": self._mode,
                "pump": self._coils[_COIL_PUMP],
                "valve": self._coils[_COIL_VALVE],
                "heater": self._coils[_COIL_HEATER],
                "energy_wh": round(self._energy_wh, 3),
            },
            "note": (
                "silent addresses answer after a short delay rather than burning the "
                "full per-address timeout, so a scan is quick; the outcome reported "
                "is the same timeout a real bus produces"
            ),
        }

    # -- the one transport method -----------------------------------------

    async def _transact(self, request: ModbusRequest, *, timeout_s: float) -> ModbusReply:
        if request.slave != SIM_SLAVE_ID:
            await asyncio.sleep(min(_SILENCE_S, timeout_s))
            return ModbusReply(outcome="timeout", detail=f"no station at address {request.slave}")

        await asyncio.sleep(min(_TURNAROUND_S, timeout_s))

        if request.function == FUNCTION_READ_HOLDING:
            return self._slice_registers(self._holding_table(), request)
        if request.function == FUNCTION_READ_INPUT:
            return self._slice_registers(self._input_table(), request)
        if request.function == FUNCTION_READ_COILS:
            return self._slice_bits(list(self._coils), request)
        if request.function == FUNCTION_READ_DISCRETE:
            return self._slice_bits(self._discrete_table(), request)
        if request.function == FUNCTION_WRITE_COIL:
            return self._write_coil(request)
        if request.function == FUNCTION_WRITE_REGISTER:
            return self._write_registers(request)
        if request.function == FUNCTION_WRITE_REGISTERS:
            return self._write_registers(request)
        return ModbusReply(outcome="exception", exception_code=0x01, detail="unsupported function")

    # -- table access ------------------------------------------------------

    @staticmethod
    def _slice_registers(table: list[int], request: ModbusRequest) -> ModbusReply:
        end = request.address + request.count
        if end > len(table):
            return ModbusReply(
                outcome="exception",
                exception_code=0x02,
                detail=f"registers {request.address}..{end - 1} run past {len(table) - 1}",
            )
        return ModbusReply(
            outcome="ok",
            registers=tuple(value & 0xFFFF for value in table[request.address : end]),
        )

    @staticmethod
    def _slice_bits(table: list[bool], request: ModbusRequest) -> ModbusReply:
        end = request.address + request.count
        if end > len(table):
            return ModbusReply(
                outcome="exception",
                exception_code=0x02,
                detail=f"bits {request.address}..{end - 1} run past {len(table) - 1}",
            )
        return ModbusReply(outcome="ok", bits=tuple(table[request.address : end]))

    def _write_coil(self, request: ModbusRequest) -> ModbusReply:
        if request.address >= _COIL_COUNT:
            return ModbusReply(
                outcome="exception",
                exception_code=0x02,
                detail=f"coil {request.address} does not exist",
            )
        value = request.values[0] if request.values else _COIL_OFF
        if value not in (_COIL_ON, _COIL_OFF):
            # Real devices are strict about this; accepting anything non-zero
            # would hide a client that encodes coil writes incorrectly.
            return ModbusReply(
                outcome="exception",
                exception_code=0x03,
                detail=f"coil value must be 0x0000 or 0xFF00, got 0x{value:04X}",
            )
        self._advance_energy()
        self._coils[request.address] = value == _COIL_ON
        return ModbusReply(outcome="ok", registers=(value,))

    def _write_registers(self, request: ModbusRequest) -> ModbusReply:
        words = request.values
        for offset, _word in enumerate(words):
            address = request.address + offset
            if address >= _HOLDING_COUNT:
                return ModbusReply(
                    outcome="exception",
                    exception_code=0x02,
                    detail=f"holding register {address} does not exist",
                )
            if address in _READ_ONLY_HOLDING:
                return ModbusReply(
                    outcome="exception",
                    exception_code=0x02,
                    detail=f"holding register {address} is read-only",
                )
        rejection = self._validate_values(request.address, words)
        if rejection is not None:
            return rejection

        for offset, word in enumerate(words):
            self._apply_write(request.address + offset, word)

        if request.function == FUNCTION_WRITE_REGISTER:
            return ModbusReply(outcome="ok", registers=(words[0],))
        # Function 0x10 echoes the starting address and the quantity written.
        return ModbusReply(outcome="ok", registers=(len(words),))

    def _validate_values(self, address: int, words: tuple[int, ...]) -> ModbusReply | None:
        """Range-check like a real controller: out of range is a refusal."""
        for offset, word in enumerate(words):
            target = address + offset
            if target == _REG_SETPOINT and not _SETPOINT_MIN <= word <= _SETPOINT_MAX:
                return ModbusReply(
                    outcome="exception",
                    exception_code=0x03,
                    detail=(
                        f"setpoint {word} is outside {_SETPOINT_MIN}..{_SETPOINT_MAX} "
                        "(tenths of a degree)"
                    ),
                )
            if target == _REG_MODE and word > _MODE_MAX:
                return ModbusReply(
                    outcome="exception",
                    exception_code=0x03,
                    detail=f"mode {word} is not one of 0 (off), 1 (auto), 2 (manual)",
                )
        return None

    def _apply_write(self, address: int, word: int) -> None:
        self._advance_energy()
        if address == _REG_SETPOINT:
            self._setpoint = word
        elif address == _REG_MODE:
            self._mode = word
        elif address == _REG_PUMP_SPEED:
            self._pump_speed = word


def build_simulated_modbus_devices() -> list[Driver]:
    """The simulated Modbus bench: one RTU line with one station on it."""
    return [SimModbusDriver()]
