"""Simulated devices.

Enabled with ``FIELDDECK_SIM=1``.  Every simulated driver implements the same
contract and is dispatched through the same authorization pipeline as real
hardware, so simulation exercises the production code path rather than
shadowing it.
"""

from __future__ import annotations

from fielddeck.drivers.base import Driver
from fielddeck.sim.can import SimCanDriver
from fielddeck.sim.dmm import SimDmmDriver
from fielddeck.sim.psu import SimPsuDriver
from fielddeck.sim.serial import SimSerialDriver

__all__ = [
    "SimCanDriver",
    "SimDmmDriver",
    "SimPsuDriver",
    "SimSerialDriver",
    "build_simulated_devices",
]


def build_simulated_devices(*, fault_mode: bool = False) -> list[Driver]:
    """The standard simulated bench.

    One CAN interface, one serial adapter, a programmable supply and a DMM —
    enough to exercise discovery, capture, the timeline, arming, leases and
    ESTOP without any hardware attached.
    """
    return [
        SimCanDriver("can0"),
        SimSerialDriver("sim-uart-0"),
        SimPsuDriver("sim-psu-0", fault_mode=fault_mode),
        SimDmmDriver("sim-dmm-0"),
    ]
