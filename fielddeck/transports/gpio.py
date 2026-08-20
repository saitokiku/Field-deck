"""GPIO through libgpiod.

The Pi's own header is the most tempting and most dangerous thing on the
board.  It is 3.3 V, it is not isolated, it shares ground with everything
else in the enclosure, and nothing in software can tell you what is on the
other end of the jumper wire.  So:

* every action on this transport carries an explicit electrical warning
* driving a line is CONTROL, never something that happens as a side effect
* :meth:`safe_state` releases every line FieldDeck requested, returning them
  to inputs, and it runs on ESTOP, on lease expiry and at shutdown

libgpiod v2 is used rather than the removed sysfs interface or any
/dev/mem poking.  Direct-memory GPIO tricks bypass the kernel's arbitration
and will happily fight a driver that already owns the pin.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from fielddeck.common.config import FieldDeckConfig
from fielddeck.common.errors import UnsupportedCapability
from fielddeck.common.models import (
    ConnectionState,
    DeviceCapability,
    DeviceDescriptor,
    DeviceRole,
    PermissionLevel,
    TransportKind,
)
from fielddeck.drivers.base import ActionContext, DeviceParams, Driver, action

__all__ = ["GpioDriver", "discover_gpio_drivers"]

ELECTRICAL_WARNING = (
    "Raspberry Pi GPIO is 3.3 V and NOT 5 V tolerant, and is not isolated from "
    "the DUT. FieldDeck cannot verify logic levels, pin mapping or ground "
    "reference — confirm them physically before driving anything."
)


def _gpiod() -> Any:
    try:
        import gpiod  # type: ignore[import-not-found]
    except ImportError as exc:
        raise UnsupportedCapability(
            "libgpiod Python bindings are not installed; "
            "install with: pip install 'fielddeck[gpio]' (and apt install libgpiod2)",
            details={"module": "gpiod"},
        ) from exc
    if not hasattr(gpiod, "request_lines") and not hasattr(gpiod, "Chip"):
        raise UnsupportedCapability(
            "the installed gpiod module is not the libgpiod binding FieldDeck expects",
            details={"module": "gpiod"},
        )
    return gpiod


class GpioInfoParams(DeviceParams):
    pass


class GpioReadParams(DeviceParams):
    lines: list[int] = Field(min_length=1, max_length=64)
    bias: str = Field(default="as-is", pattern="^(as-is|pull-up|pull-down|disabled)$")


class GpioWriteParams(DeviceParams):
    #: line offset -> value
    values: dict[int, int]
    drive: str = Field(default="push-pull", pattern="^(push-pull|open-drain|open-source)$")
    #: How long the line may stay driven without a renewal.
    lease_ttl_s: float = Field(default=30.0, gt=0, le=3600)


class GpioDriver(Driver):
    """One gpiochip."""

    kind = TransportKind.GPIO

    def __init__(self, *, path: str, label: str, num_lines: int) -> None:
        descriptor = DeviceDescriptor(
            id=f"gpio:gpiochip:{label}",
            kind=TransportKind.GPIO,
            display_name=f"GPIO {label} ({num_lines} lines)",
            path=path,
            product=label,
            roles=[DeviceRole.BUS],
            capabilities=[
                DeviceCapability.RX,
                DeviceCapability.TX,
                DeviceCapability.SAFE_STATE,
            ],
            state=ConnectionState.DISCOVERED,
            warning=ELECTRICAL_WARNING,
            metadata={"lines": num_lines, "label": label, "voltage": "3.3V", "isolated": False},
        )
        super().__init__(descriptor)
        self.path = path
        self.label = label
        self.num_lines = num_lines
        #: Lines FieldDeck currently drives, so safe_state knows what to release.
        self._driven: dict[int, int] = {}
        self._request: Any = None

    async def status(self) -> dict[str, Any]:
        return {
            "chip": self.path,
            "label": self.label,
            "lines": self.num_lines,
            "driven": dict(self._driven),
            "voltage": "3.3V",
            "isolated": False,
            "warning": ELECTRICAL_WARNING,
        }

    async def safe_state(self) -> dict[str, Any]:
        """Release every line we requested; they revert to inputs."""
        released = sorted(self._driven)
        self._driven.clear()
        if self._request is not None:
            try:
                self._request.release()
            except Exception as exc:  # noqa: BLE001 - releasing must not raise onward
                return {
                    "device": self.device_id,
                    "applied": False,
                    "error": str(exc),
                    "released": released,
                }
            finally:
                self._request = None
        return {
            "device": self.device_id,
            "applied": True,
            "changed": bool(released),
            "released": released,
            "state": "all FieldDeck-held lines released to inputs",
        }

    # -- actions -----------------------------------------------------------

    @action(
        "gpio.info",
        permission=PermissionLevel.PASSIVE,
        params=GpioInfoParams,
        state_changing=False,
        description="Chip and line inventory, including which lines are already in use.",
        allowed_during_estop=True,
        timeout_s=15.0,
    )
    async def gpio_info(self, ctx: ActionContext, params: GpioInfoParams) -> dict[str, Any]:
        """Kernel metadata only — no line is requested and nothing is driven."""
        import asyncio

        def _read() -> list[dict[str, Any]]:
            gpiod = _gpiod()
            lines: list[dict[str, Any]] = []
            with gpiod.Chip(self.path) as chip:
                for offset in range(self.num_lines):
                    info = chip.get_line_info(offset)
                    lines.append(
                        {
                            "offset": offset,
                            "name": info.name or None,
                            "consumer": info.consumer or None,
                            "direction": str(info.direction).rsplit(".", 1)[-1],
                            # A line another driver already owns must not be
                            # grabbed; report it so the operator sees the clash.
                            "used": bool(info.used),
                        }
                    )
            return lines

        lines = await asyncio.to_thread(_read)
        return {
            **await self.status(),
            "line_detail": lines,
            "in_use_by_others": [entry for entry in lines if entry["used"]],
        }

    @action(
        "gpio.read",
        permission=PermissionLevel.PASSIVE,
        params=GpioReadParams,
        state_changing=False,
        description="Sample input lines. Configures them as inputs, never drives them.",
        timeout_s=15.0,
    )
    async def gpio_read(self, ctx: ActionContext, params: GpioReadParams) -> dict[str, Any]:
        """PASSIVE: an input is high impedance and puts nothing onto the pin.

        A bias setting other than ``as-is`` does connect an internal pull
        resistor, which is a real, if weak, electrical change — it is reported
        back so it never happens invisibly.
        """
        import asyncio

        def _sample() -> dict[int, int]:
            gpiod = _gpiod()
            settings = gpiod.LineSettings(
                direction=gpiod.line.Direction.INPUT,
                bias={
                    "as-is": gpiod.line.Bias.AS_IS,
                    "pull-up": gpiod.line.Bias.PULL_UP,
                    "pull-down": gpiod.line.Bias.PULL_DOWN,
                    "disabled": gpiod.line.Bias.DISABLED,
                }[params.bias],
            )
            config = dict.fromkeys(params.lines, settings)
            with gpiod.request_lines(
                self.path, consumer="fielddeck-read", config=config
            ) as request:
                return {
                    offset: int(request.get_value(offset) == gpiod.line.Value.ACTIVE)
                    for offset in params.lines
                }

        values = await asyncio.to_thread(_sample)
        return {
            "values": values,
            "bias": params.bias,
            "warning": (
                ELECTRICAL_WARNING
                if params.bias == "as-is"
                else f"{ELECTRICAL_WARNING} An internal {params.bias} resistor was enabled."
            ),
        }

    @action(
        "gpio.write",
        permission=PermissionLevel.CONTROL,
        params=GpioWriteParams,
        state_changing=True,
        description="Drive output lines to the given values.",
        requires_lease=True,
        timeout_s=15.0,
        safe_state_note=(
            "Every driven line is released back to an input on safe state, "
            "ESTOP, lease expiry or daemon shutdown."
        ),
    )
    async def gpio_write(self, ctx: ActionContext, params: GpioWriteParams) -> dict[str, Any]:
        """CONTROL: this puts a 3.3 V push-pull driver onto a real pin."""
        import asyncio

        def _drive() -> None:
            gpiod = _gpiod()
            settings = gpiod.LineSettings(
                direction=gpiod.line.Direction.OUTPUT,
                drive={
                    "push-pull": gpiod.line.Drive.PUSH_PULL,
                    "open-drain": gpiod.line.Drive.OPEN_DRAIN,
                    "open-source": gpiod.line.Drive.OPEN_SOURCE,
                }[params.drive],
            )
            config = dict.fromkeys(params.values, settings)
            if self._request is not None:
                self._request.release()
            # The request is held open so the lines stay driven; releasing it
            # is exactly what safe_state does.
            self._request = gpiod.request_lines(
                self.path, consumer="fielddeck", config=config
            )
            for offset, value in params.values.items():
                self._request.set_value(
                    offset,
                    gpiod.line.Value.ACTIVE if value else gpiod.line.Value.INACTIVE,
                )

        await asyncio.to_thread(_drive)
        self._driven.update({int(k): int(v) for k, v in params.values.items()})
        return {
            "driven": dict(self._driven),
            "drive": params.drive,
            "warning": ELECTRICAL_WARNING,
        }


def discover_gpio_drivers(config: FieldDeckConfig) -> list[Driver]:
    """Enumerate gpiochips from sysfs.  Does not open or request any line."""
    from pathlib import Path

    drivers: list[Driver] = []
    sys_chips = Path("/sys/bus/gpio/devices")
    if not sys_chips.is_dir():
        return drivers
    for entry in sorted(sys_chips.iterdir()):
        dev = Path("/dev") / entry.name
        if not dev.exists():
            continue
        label_file = entry / "label"
        lines_file = entry / "ngpio"
        label = label_file.read_text().strip() if label_file.exists() else entry.name
        try:
            num_lines = int(lines_file.read_text().strip()) if lines_file.exists() else 0
        except ValueError:  # pragma: no cover - malformed sysfs
            num_lines = 0
        drivers.append(GpioDriver(path=str(dev), label=label or entry.name, num_lines=num_lines))
    return drivers
