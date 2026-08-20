"""Stable device identity.

``/dev/ttyUSB0`` is not an identity — it is whichever FTDI cable happened to
enumerate first.  A FieldDeck device id is built from persistent evidence so
that an operator profile, a recipe, or a saved session still refers to the
same physical adapter after a reboot or a re-plug::

    serial:usb:0403:6001:A10ABC
    can:socketcan:can0
    visa:usb:0957:1798:MY12345678
    sim:serial:sim-uart-0

When no persistent evidence exists the id is marked unstable rather than
faked, and callers are expected to surface that to the operator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "DeviceIdParts",
    "device_id",
    "is_simulated_id",
    "parse_device_id",
    "sanitize_component",
]

_UNSAFE = re.compile(r"[^A-Za-z0-9_.\-]+")


def sanitize_component(value: str | None, *, fallback: str = "unknown") -> str:
    """Normalise one id component: lowercase-safe, no separators, never empty."""
    if value is None:
        return fallback
    cleaned = _UNSAFE.sub("-", value.strip()).strip("-")
    return cleaned or fallback


def device_id(transport: str, bus: str, *identity: str | None) -> str:
    """Compose a device id from transport, bus and identity components."""
    parts = [sanitize_component(transport), sanitize_component(bus)]
    parts.extend(sanitize_component(component) for component in identity if component is not None)
    return ":".join(parts)


@dataclass(frozen=True, slots=True)
class DeviceIdParts:
    transport: str
    bus: str
    identity: tuple[str, ...]

    @property
    def identity_str(self) -> str:
        return ":".join(self.identity)


def parse_device_id(value: str) -> DeviceIdParts:
    """Split a device id.  Raises :class:`ValueError` on a malformed id."""
    chunks = value.split(":")
    if len(chunks) < 2:
        raise ValueError(f"malformed device id: {value!r}")
    return DeviceIdParts(transport=chunks[0], bus=chunks[1], identity=tuple(chunks[2:]))


def is_simulated_id(value: str) -> bool:
    return value.startswith("sim:")


def usb_serial_id(vid: int | None, pid: int | None, serial: str | None) -> tuple[str, bool]:
    """Build the identity tail for a USB serial adapter.

    Returns ``(identity, stable)``.  Without a USB serial number two identical
    adapters are indistinguishable, so the id is reported as unstable.
    """
    vid_s = f"{vid:04x}" if vid is not None else "xxxx"
    pid_s = f"{pid:04x}" if pid is not None else "xxxx"
    if serial:
        return f"{vid_s}:{pid_s}:{sanitize_component(serial)}", True
    return f"{vid_s}:{pid_s}", False
