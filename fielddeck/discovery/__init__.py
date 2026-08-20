"""Stage 1 of the auto-detect engine: non-invasive inventory.

Discovery enumerates.  It does not query, probe, scan or transmit.  An
instrument identity query is QUERY-level work and belongs in an explicit
action the operator authorizes, not in something that runs on a timer.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable

from fielddeck.common.config import FieldDeckConfig
from fielddeck.common.logging import get_logger
from fielddeck.common.models import (
    ConnectionState,
    DeviceCapability,
    DeviceDescriptor,
    DeviceRole,
    TransportKind,
)
from fielddeck.discovery.linux import (
    list_can_interfaces,
    list_network_interfaces,
    list_serial_ports,
    list_usb_devices,
    list_video_devices,
)
from fielddeck.drivers.base import Driver

__all__ = ["inventory", "scan"]

_log = get_logger("fielddeck.discovery")

Provider = Callable[[FieldDeckConfig], Awaitable[list[Driver]]]

#: Real-hardware providers, in the order they are consulted.  Each is
#: ``(module, factory)`` and each is allowed to be absent.
_REAL_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("fielddeck.transports.serial_port", "discover_serial_drivers"),
    ("fielddeck.transports.socketcan", "discover_can_drivers"),
    ("fielddeck.bench.visa", "discover_visa_drivers"),
    ("fielddeck.protocols.modbus", "discover_modbus_drivers"),
    ("fielddeck.capture.camera", "discover_camera_drivers"),
    ("fielddeck.capture.sigrok", "discover_logic_drivers"),
    ("fielddeck.transports.gpio", "discover_gpio_drivers"),
    ("fielddeck.transports.i2c", "discover_i2c_drivers"),
    ("fielddeck.transports.spi", "discover_spi_drivers"),
    ("fielddeck.transports.network", "discover_network_drivers"),
)


async def _simulated(config: FieldDeckConfig) -> list[Driver]:
    from fielddeck.sim import build_simulated_devices

    return build_simulated_devices()


async def _real(config: FieldDeckConfig) -> list[Driver]:
    """Real hardware providers.

    Each provider is independent and optional: a Pi without python-can still
    enumerates serial ports, and a laptop with neither still runs the daemon.
    A missing driver library degrades that one transport, never the daemon.
    """
    drivers: list[Driver] = []
    for module_name, factory_name in _REAL_PROVIDERS:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            _log.info(
                "transport unavailable",
                extra={"module": module_name, "reason": str(exc)},
            )
            continue
        factory = getattr(module, factory_name, None)
        if factory is None:  # pragma: no cover - defensive
            continue
        try:
            drivers.extend(factory(config))
        except Exception:  # noqa: BLE001 - one bad transport must not hide the others
            _log.exception("provider failed", extra={"module": module_name})
    return drivers


async def scan(config: FieldDeckConfig) -> list[Driver]:
    """Build the driver set for the current environment."""
    if config.simulate:
        return await _simulated(config)
    try:
        return await _real(config)
    except Exception:  # noqa: BLE001 - discovery must never take the daemon down
        _log.exception("hardware discovery failed; continuing with no devices")
        return []


def inventory() -> dict[str, list[dict[str, object]]]:
    """The raw passive inventory, independent of whether a driver exists.

    Useful on its own: it shows an operator that a probe is plugged in even
    when FieldDeck has no driver for it yet.
    """
    return {
        "can": list_can_interfaces(),
        "serial": list_serial_ports(),
        "usb": list_usb_devices(),
        "network": list_network_interfaces(),
        "cameras": list_video_devices(),
    }


def descriptor_for_unsupported(
    *, id: str, kind: TransportKind, display_name: str, **extra: object
) -> DeviceDescriptor:
    """Describe something we can see but cannot drive yet."""
    return DeviceDescriptor(
        id=id,
        kind=kind,
        display_name=display_name,
        roles=[DeviceRole.BUS],
        capabilities=[DeviceCapability.RX],
        state=ConnectionState.DISCOVERED,
        warning="no FieldDeck driver for this device yet; it is listed for reference only",
        metadata=dict(extra),
    )
