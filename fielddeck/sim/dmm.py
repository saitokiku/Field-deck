"""Simulated digital multimeter.

Measurements are QUERY: reading a DMM means sending it a SCPI command.  The
reading carries realistic noise so averaging and stability checks have
something to work with.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from fielddeck.common.models import (
    ConnectionState,
    DeviceCapability,
    DeviceDescriptor,
    DeviceRole,
    PermissionLevel,
    TransportKind,
)
from fielddeck.common.timebase import Timestamp
from fielddeck.drivers.base import ActionContext, DeviceParams, Driver, action
from fielddeck.sim.base import SimulatedDeviceMixin, seeded_random

__all__ = ["SimDmmDriver"]

_FUNCTIONS = {
    "dc_voltage": ("V", 24.002, 0.0008),
    "ac_voltage": ("V", 0.012, 0.0020),
    "dc_current": ("A", 0.418, 0.0015),
    "resistance": ("ohm", 57.4, 0.0500),
    "frequency": ("Hz", 0.0, 0.0),
    "continuity": ("ohm", 0.3, 0.0100),
}


class DmmMeasureParams(DeviceParams):
    function: str = Field(default="dc_voltage")
    samples: int = Field(default=1, ge=1, le=256)


class SimDmmDriver(SimulatedDeviceMixin, Driver):
    kind = TransportKind.VISA

    def __init__(self, name: str = "sim-dmm-0") -> None:
        descriptor = DeviceDescriptor(
            id=f"sim:visa:{name}",
            kind=TransportKind.VISA,
            display_name="Simulated bench DMM",
            vendor="FieldDeck",
            product="SIM-DMM-6500",
            serial_number="SIMDMM0001",
            roles=[DeviceRole.DMM],
            capabilities=[DeviceCapability.MEASURE],
            state=ConnectionState.READY,
            simulated=True,
            metadata={"functions": sorted(_FUNCTIONS)},
        )
        Driver.__init__(self, descriptor)
        SimulatedDeviceMixin.__init__(self)
        self._rng = seeded_random(descriptor.id)

    async def status(self) -> dict[str, Any]:
        return {
            "identity": "FieldDeck,SIM-DMM-6500,SIMDMM0001,1.0",
            "functions": sorted(_FUNCTIONS),
            "state": str(self._descriptor.state),
        }

    @action(
        "dmm.status",
        permission=PermissionLevel.PASSIVE,
        params=DeviceParams,
        state_changing=False,
        description="Instrument identity and supported functions.",
        allowed_during_estop=True,
    )
    async def dmm_status(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        return await self.status()

    @action(
        "dmm.measure",
        permission=PermissionLevel.QUERY,
        params=DmmMeasureParams,
        state_changing=False,
        description="Take one or more readings.",
        timeout_s=30.0,
    )
    async def dmm_measure(self, ctx: ActionContext, params: DmmMeasureParams) -> dict[str, Any]:
        from fielddeck.common.errors import InvalidRequest

        if params.function not in _FUNCTIONS:
            raise InvalidRequest(
                f"unknown DMM function {params.function!r}",
                details={"known": sorted(_FUNCTIONS)},
            )
        unit, nominal, noise = _FUNCTIONS[params.function]
        readings = [round(nominal + self._rng.gauss(0.0, noise), 6) for _ in range(params.samples)]
        ts = Timestamp.now()
        if ctx.recorder is not None:
            for reading in readings:
                ctx.recorder.measurement(
                    quantity=f"dmm.{params.function}",
                    value=reading,
                    device_id=self.device_id,
                    unit=unit,
                    timestamp=ts,
                )
        mean = sum(readings) / len(readings)
        return {
            "function": params.function,
            "unit": unit,
            "value": round(mean, 6),
            "readings": readings,
            "samples": len(readings),
            "spread": round(max(readings) - min(readings), 6),
            "monotonic_ns": ts.monotonic_ns,
        }
