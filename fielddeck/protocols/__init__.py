"""Protocol layers that sit on top of a transport.

A transport moves bytes; the modules here give those bytes meaning.  Two
different jobs live side by side, and the difference decides the permission
class before any code is written:

* **Decoders** — ISO-TP reassembly, UDS, J1939 — are pure functions over
  frames that have already been captured.  They transmit nothing, so they are
  PASSIVE and remain available while an emergency stop is latched.  Reading a
  capture is exactly what an operator should be doing while the bench is safe.

* **Masters** — Modbus — take a turn on a live bus.  Every action puts a
  frame in front of a DUT, so reads are QUERY, writes are CONTROL, and none
  of it is PASSIVE.  A protocol module that transmits owns a driver and goes
  through the dispatcher like any other piece of hardware.

Everything here imports with no optional dependencies installed.  pymodbus,
python-can and cantools are pulled in inside the functions that need them, so
importing this package on a machine with nothing plugged in still works.
"""

from __future__ import annotations

from fielddeck.protocols import isotp, j1939, modbus, uds
from fielddeck.protocols.modbus import (
    ModbusDriver,
    ModbusDriverBase,
    ModbusEndpoint,
    ModbusTransaction,
    data_reference,
    decode_registers,
    discover_modbus_drivers,
    load_modbus_endpoints,
)

__all__ = [
    "ModbusDriver",
    "ModbusDriverBase",
    "ModbusEndpoint",
    "ModbusTransaction",
    "data_reference",
    "decode_registers",
    "discover_modbus_drivers",
    "isotp",
    "j1939",
    "load_modbus_endpoints",
    "modbus",
    "uds",
]
