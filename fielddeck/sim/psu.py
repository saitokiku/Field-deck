"""Simulated programmable power supply.

This is the device the whole safety model exists for.  It demonstrates, in
code you can run without owning a bench supply:

* setpoints and output enable require a POWER grant
* limits are checked after authorization and cannot be waived by it
* the output takes a lease, so a client that dies leaves the rail off
* ``psu.output(enabled=False)`` resolves to PASSIVE — turning an output off
  is never blocked by a lapsed grant or a latched emergency stop

The load model is a plain resistor plus an optional fault where the current
climbs after about 1.4 s, which is the scenario the timeline correlation
example is built around.
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
from fielddeck.common.timebase import Timestamp, monotonic_ns
from fielddeck.drivers.base import ActionContext, DeviceParams, Driver, action
from fielddeck.safety.limits import DerivedLimitCheck, LimitCheck
from fielddeck.sim.base import SimulatedDeviceMixin
from fielddeck.sim.scenario import Scenario

__all__ = ["SimPsuDriver"]

#: Load resistance of the simulated DUT, chosen so 24 V draws ~0.418 A.
_LOAD_OHMS = 57.4
#: When the fault is enabled, current climbs to this after ``_FAULT_AT_S``.
_FAULT_CURRENT_A = 0.914
_FAULT_AT_S = 1.4


class PsuSetParams(DeviceParams):
    voltage: float | None = Field(default=None, ge=0)
    current_limit: float | None = Field(default=None, ge=0)


class PsuOutputParams(DeviceParams):
    enabled: bool
    #: How long the output may stay on without a renewal.
    lease_ttl_s: float = Field(default=30.0, gt=0, le=3600)


class ScpiQueryParams(DeviceParams):
    command: str = Field(min_length=1, max_length=256)


def _output_permission(params: Any) -> PermissionLevel:
    """Enabling an output is POWER; disabling one is always allowed."""
    return PermissionLevel.POWER if params.enabled else PermissionLevel.PASSIVE


class SimPsuDriver(SimulatedDeviceMixin, Driver):
    """A virtual 30 V / 3 A bench supply speaking SCPI."""

    kind = TransportKind.VISA

    def __init__(
        self,
        name: str = "sim-psu-0",
        *,
        fault_mode: bool = False,
        scenario: Scenario | None = None,
    ) -> None:
        descriptor = DeviceDescriptor(
            id=f"sim:visa:{name}",
            kind=TransportKind.VISA,
            display_name="Simulated bench PSU",
            vendor="FieldDeck",
            product="SIM-PSU-3003",
            serial_number="SIMPSU0001",
            roles=[DeviceRole.PSU],
            capabilities=[
                DeviceCapability.MEASURE,
                DeviceCapability.OUTPUT,
                DeviceCapability.SETPOINT,
                DeviceCapability.SAFE_STATE,
            ],
            #: Even reading this instrument means talking to it over SCPI.
            permission_floor=PermissionLevel.PASSIVE,
            state=ConnectionState.READY,
            simulated=True,
            metadata={"model": "SIM-PSU-3003", "channels": 1, "max_voltage": 30.0},
        )
        Driver.__init__(self, descriptor)
        SimulatedDeviceMixin.__init__(self)
        self.name = name
        self.voltage_setpoint = 0.0
        self.current_limit = 0.5
        self.output_enabled = False
        # The scenario is shared with the CAN and serial sims so the fault
        # they each report is one causal story on one time axis.
        self._scenario = scenario or Scenario(armed=fault_mode)
        self._fault_mode = self._scenario.armed
        self._output_since_ns: int | None = None

    # -- physics -----------------------------------------------------------

    def _measure(self) -> tuple[float, float, bool]:
        """Returns (volts, amps, in_current_limit)."""
        if not self.output_enabled:
            return 0.0, 0.0, False
        ideal = self.voltage_setpoint / _LOAD_OHMS
        if self._scenario.fault_developing:
            ideal = _FAULT_CURRENT_A
        limited = ideal >= self.current_limit
        current = min(ideal, self.current_limit)
        # A real supply drops out of constant voltage when it hits the limit.
        voltage = self.current_limit * _LOAD_OHMS if limited else self.voltage_setpoint
        return round(voltage + 0.002, 4), round(current, 4), limited

    async def status(self) -> dict[str, Any]:
        voltage, current, limited = self._measure()
        return {
            "identity": "FieldDeck,SIM-PSU-3003,SIMPSU0001,1.0",
            "output": self.output_enabled,
            "setpoint_v": self.voltage_setpoint,
            "current_limit_a": self.current_limit,
            "measured_v": voltage,
            "measured_a": current,
            "mode": "CC" if limited else ("CV" if self.output_enabled else "OFF"),
            "fault_mode": self._fault_mode,
            "scenario": self._scenario.describe(),
        }

    async def safe_state(self) -> dict[str, Any]:
        """Output off.  This runs on ESTOP, lease expiry, and shutdown."""
        was_on = self.output_enabled
        self.output_enabled = False
        self._output_since_ns = None
        self._scenario.note_output(False)
        return {
            "device": self.device_id,
            "applied": True,
            "changed": was_on,
            "state": "output disabled",
        }

    # -- actions -----------------------------------------------------------

    @action(
        "psu.status",
        permission=PermissionLevel.PASSIVE,
        params=DeviceParams,
        state_changing=False,
        description="Cached supply state without querying the instrument.",
        allowed_during_estop=True,
    )
    async def psu_status(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        return await self.status()

    @action(
        "bench.status",
        permission=PermissionLevel.PASSIVE,
        params=DeviceParams,
        state_changing=False,
        description="Cached instrument state. Does not talk to the instrument.",
        allowed_during_estop=True,
    )
    async def bench_status(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        return await self.status()

    @action(
        "bench.identify",
        permission=PermissionLevel.QUERY,
        params=DeviceParams,
        state_changing=False,
        description="Query instrument identity with *IDN? and select a profile.",
    )
    async def bench_identify(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        """QUERY: asking an instrument who it is means transmitting to it."""
        status = await self.status()
        return {
            "identity": status["identity"],
            "profile": "fielddeck.sim",
            "role": "psu",
            "hardware_verified": False,
            "simulated": True,
        }

    @action(
        "psu.measure",
        permission=PermissionLevel.QUERY,
        params=DeviceParams,
        state_changing=False,
        description="Read output voltage and current from the instrument.",
        # Not allowed_during_estop: this sends SCPI, and a latched stop is
        # only ever waived for PASSIVE work. The question it answers -- what
        # is the rail actually doing -- is served during a stop by the
        # PASSIVE status action and by the SAFE_STATE_APPLIED event payload.
    )
    async def psu_measure(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        """QUERY, not PASSIVE: this sends SCPI to the instrument."""
        voltage, current, limited = self._measure()
        ts = Timestamp.now()
        if ctx.recorder is not None:
            ctx.recorder.measurement(
                quantity="psu.voltage",
                value=voltage,
                device_id=self.device_id,
                unit="V",
                timestamp=ts,
            )
            ctx.recorder.measurement(
                quantity="psu.current",
                value=current,
                device_id=self.device_id,
                unit="A",
                timestamp=ts,
            )
        return {
            "voltage": voltage,
            "current": current,
            "power": round(voltage * current, 4),
            "mode": "CC" if limited else ("CV" if self.output_enabled else "OFF"),
            "monotonic_ns": ts.monotonic_ns,
            "utc_ns": ts.utc_ns,
        }

    @action(
        "psu.set",
        permission=PermissionLevel.POWER,
        params=PsuSetParams,
        state_changing=True,
        description="Change the voltage setpoint and/or current limit.",
        limit_checks=(
            LimitCheck(param="voltage", quantity="psu.voltage"),
            LimitCheck(param="current_limit", quantity="psu.current"),
        ),
        derived_limit_checks=(
            DerivedLimitCheck(quantity="psu.power", params=("voltage", "current_limit")),
        ),
        safe_state_note="Setpoints persist; the output is disabled on safe state.",
    )
    async def psu_set(self, ctx: ActionContext, params: PsuSetParams) -> dict[str, Any]:
        """POWER: changing a setpoint changes what a DUT will be subjected to."""
        if params.voltage is not None:
            self.voltage_setpoint = params.voltage
        if params.current_limit is not None:
            self.current_limit = params.current_limit
        return {
            "setpoint_v": self.voltage_setpoint,
            "current_limit_a": self.current_limit,
            "output": self.output_enabled,
        }

    @action(
        "psu.output",
        permission=PermissionLevel.POWER,
        params=PsuOutputParams,
        state_changing=True,
        description="Enable or disable the output.",
        permission_resolver=_output_permission,
        requires_lease=True,
        allowed_during_estop=True,
        safe_state_note="Disabling the output is always permitted, including during ESTOP.",
    )
    async def psu_output(self, ctx: ActionContext, params: PsuOutputParams) -> dict[str, Any]:
        """Enabling needs POWER and takes a lease; disabling is always allowed."""
        self.output_enabled = params.enabled
        self._output_since_ns = monotonic_ns() if params.enabled else None
        self._scenario.note_output(params.enabled)
        voltage, current, _limited = self._measure()
        return {
            "output": self.output_enabled,
            "setpoint_v": self.voltage_setpoint,
            "current_limit_a": self.current_limit,
            "measured_v": voltage,
            "measured_a": current,
        }

    @action(
        "scpi.query",
        permission=PermissionLevel.QUERY,
        params=ScpiQueryParams,
        state_changing=False,
        description="Send a SCPI query and return the response.",
        timeout_s=10.0,
    )
    async def scpi_query(self, ctx: ActionContext, params: ScpiQueryParams) -> dict[str, Any]:
        """QUERY only.  Commands that would change state are refused here.

        An arbitrary SCPI string is classified conservatively: anything that
        is not clearly a query gets rejected rather than guessed at, because
        ``OUTP ON`` looks harmless right up until it energises something.
        """
        from fielddeck.bench.scpi import classify_scpi
        from fielddeck.common.errors import PermissionDenied

        command = params.command.strip()
        # Not `endswith("?")`: SCPI allows several commands in one message, so
        # `OUTP ON;*IDN?` ends in a question mark and would energise an output.
        # classify_scpi checks every segment.
        classification = classify_scpi(command)
        if not classification.is_query:
            raise PermissionDenied(
                f"{command!r} is not a query ({classification.reason}). Use the typed "
                "actions (psu.set, psu.output) so the permission model can see what "
                "you are asking for.",
                details={
                    "command": command,
                    "device_id": self.device_id,
                    "classification": str(classification.kind),
                },
                preserved="nothing was sent to the instrument",
            )
        upper = command.upper()
        voltage, current, _limited = self._measure()
        responses = {
            "*IDN?": "FieldDeck,SIM-PSU-3003,SIMPSU0001,1.0",
            "MEAS:VOLT?": f"{voltage:.4f}",
            "MEAS:CURR?": f"{current:.4f}",
            "VOLT?": f"{self.voltage_setpoint:.4f}",
            "CURR?": f"{self.current_limit:.4f}",
            "OUTP?": "1" if self.output_enabled else "0",
            "SYST:ERR?": '0,"No error"',
        }
        return {
            "command": command,
            "response": responses.get(upper, '-113,"Undefined header"'),
            "known": upper in responses,
        }
