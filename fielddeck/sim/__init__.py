"""Simulated devices.

Enabled with ``FIELDDECK_SIM=1``.  Every simulated driver implements the same
contract and is dispatched through the same authorization pipeline as real
hardware, so simulation exercises the production code path rather than
shadowing it.
"""

from __future__ import annotations

import importlib

from fielddeck.drivers.base import Driver
from fielddeck.sim.can import SimCanDriver
from fielddeck.sim.dmm import SimDmmDriver
from fielddeck.sim.psu import SimPsuDriver
from fielddeck.sim.scenario import Scenario, scenario_enabled
from fielddeck.sim.serial import SimSerialDriver

__all__ = [
    "Scenario",
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
    # One scenario object shared by every device, so the fault they each
    # report is one causal story rather than three unrelated timers.
    scenario = Scenario(armed=fault_mode or scenario_enabled())
    drivers: list[Driver] = [
        SimCanDriver("can0", scenario=scenario),
        SimSerialDriver("sim-uart-0", scenario=scenario),
        SimPsuDriver("sim-psu-0", fault_mode=scenario.armed, scenario=scenario),
        SimDmmDriver("sim-dmm-0"),
    ]
    drivers.extend(_optional_simulated_devices())
    return drivers


#: Simulated devices contributed by subsystems that may not be present.
_OPTIONAL_SIM_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("fielddeck.sim.modbus", "build_simulated_modbus_devices"),
    ("fielddeck.sim.logic", "build_simulated_logic_devices"),
    ("fielddeck.sim.camera", "build_simulated_camera_devices"),
)


def _optional_simulated_devices() -> list[Driver]:
    extra: list[Driver] = []
    for module_name, factory_name in _OPTIONAL_SIM_PROVIDERS:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        factory = getattr(module, factory_name, None)
        if factory is not None:
            extra.extend(factory())
    return extra
