"""Passive Linux inventory.

Stage 1 of the auto-detect engine: find out what is attached without sending
anything anywhere.  Everything here reads sysfs, /dev and udev symlinks.  No
subprocess, no probing, no bus traffic — enumeration must be safe to run with
a live DUT connected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fielddeck.common.ids import device_id, sanitize_component, usb_serial_id
from fielddeck.common.logging import get_logger

__all__ = [
    "list_can_interfaces",
    "list_network_interfaces",
    "list_serial_ports",
    "list_usb_devices",
    "list_video_devices",
]

_log = get_logger("fielddeck.discovery.linux")

_SYS_NET = Path("/sys/class/net")
_SYS_USB = Path("/sys/bus/usb/devices")
_DEV_SERIAL_BY_ID = Path("/dev/serial/by-id")

#: ARPHRD_CAN from <linux/if_arp.h>.  A CAN interface is exactly the netdev
#: whose type is this; matching on the name "can0" would miss "vcan0" setups
#: and would falsely match an Ethernet device someone renamed.
_ARPHRD_CAN = 280


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def list_can_interfaces() -> list[dict[str, Any]]:
    """Every SocketCAN interface, real or virtual."""
    out: list[dict[str, Any]] = []
    if not _SYS_NET.is_dir():
        return out
    for interface in sorted(_SYS_NET.iterdir()):
        type_value = _read(interface / "type")
        if type_value is None or int(type_value) != _ARPHRD_CAN:
            continue
        operstate = _read(interface / "operstate") or "unknown"
        bitrate = _read(interface / "can_bittiming" / "bitrate")
        out.append(
            {
                "id": device_id("can", "socketcan", interface.name),
                "interface": interface.name,
                "operstate": operstate,
                "up": operstate == "up",
                "bitrate": int(bitrate) if bitrate and bitrate.isdigit() else None,
                "mtu": int(_read(interface / "mtu") or 0),
                #: CAN FD interfaces have an MTU of 72 rather than 16.
                "fd_capable": int(_read(interface / "mtu") or 0) >= 72,
                "virtual": (interface / "device").exists() is False,
            }
        )
    return out


def list_usb_devices() -> list[dict[str, Any]]:
    """USB tree from sysfs.  Enumeration only; no control transfers."""
    out: list[dict[str, Any]] = []
    if not _SYS_USB.is_dir():
        return out
    for entry in sorted(_SYS_USB.iterdir()):
        vid = _read(entry / "idVendor")
        pid = _read(entry / "idProduct")
        if vid is None or pid is None:
            continue  # an interface node, not a device
        serial = _read(entry / "serial")
        identity, stable = usb_serial_id(int(vid, 16), int(pid, 16), serial)
        out.append(
            {
                "id": device_id("usb", "usb", identity),
                "vid": f"0x{vid.lower()}",
                "pid": f"0x{pid.lower()}",
                "manufacturer": _read(entry / "manufacturer"),
                "product": _read(entry / "product"),
                "serial": serial,
                "stable_id": stable,
                "path": entry.name,
                "class": _read(entry / "bDeviceClass"),
                "speed_mbps": _read(entry / "speed"),
            }
        )
    return out


def list_serial_ports() -> list[dict[str, Any]]:
    """Serial ports, preferring persistent ``/dev/serial/by-id`` identities.

    ``/dev/ttyUSB0`` is whichever adapter enumerated first, so it is recorded
    as the path but never as the identity.  When no USB serial number exists
    the entry is flagged unstable rather than given a fake stable id.
    """
    ports: dict[str, dict[str, Any]] = {}

    # pyserial knows about vendor metadata; use it when the extra is installed.
    try:
        from serial.tools import list_ports as pyserial_ports  # type: ignore[import-not-found]
    except ImportError:
        pyserial_ports = None  # type: ignore[assignment]

    if pyserial_ports is not None:
        for port in pyserial_ports.comports():
            identity, stable = usb_serial_id(port.vid, port.pid, port.serial_number)
            ports[port.device] = {
                "id": device_id("serial", "usb" if port.vid else "tty", identity)
                if port.vid
                else device_id("serial", "tty", Path(port.device).name),
                "path": port.device,
                "vendor": port.manufacturer,
                "product": port.product or port.description,
                "serial": port.serial_number,
                "vid": f"0x{port.vid:04x}" if port.vid else None,
                "pid": f"0x{port.pid:04x}" if port.pid else None,
                "stable_id": stable if port.vid else False,
                "by_id": None,
            }

    if _DEV_SERIAL_BY_ID.is_dir():
        for link in sorted(_DEV_SERIAL_BY_ID.iterdir()):
            try:
                target = str(link.resolve())
            except OSError:  # pragma: no cover - racing udev
                continue
            entry = ports.setdefault(
                target,
                {
                    "id": device_id("serial", "by-id", sanitize_component(link.name)),
                    "path": target,
                    "vendor": None,
                    "product": None,
                    "serial": None,
                    "vid": None,
                    "pid": None,
                    "stable_id": True,
                    "by_id": None,
                },
            )
            entry["by_id"] = str(link)
            entry["stable_id"] = True

    if not ports:
        # Fall back to raw sysfs for built-in UARTs, which have no USB metadata.
        tty_dir = Path("/sys/class/tty")
        if tty_dir.is_dir():
            for tty in sorted(tty_dir.iterdir()):
                if not (tty / "device").exists():
                    continue
                if not tty.name.startswith(("ttyS", "ttyAMA", "ttyUSB", "ttyACM")):
                    continue
                driver = _read(tty / "device" / "driver" / "..")
                if tty.name.startswith("ttyS") and not (tty / "device" / "driver").exists():
                    continue
                path = f"/dev/{tty.name}"
                ports[path] = {
                    "id": device_id("serial", "tty", tty.name),
                    "path": path,
                    "vendor": None,
                    "product": driver,
                    "serial": None,
                    "vid": None,
                    "pid": None,
                    "stable_id": False,
                    "by_id": None,
                }

    return sorted(ports.values(), key=lambda entry: entry["path"])


def list_video_devices() -> list[dict[str, Any]]:
    """V4L2 capture devices."""
    out: list[dict[str, Any]] = []
    dev = Path("/dev")
    if not dev.is_dir():
        return out
    for node in sorted(dev.glob("video*")):
        index = node.name.removeprefix("video")
        name = _read(Path(f"/sys/class/video4linux/{node.name}/name"))
        out.append(
            {
                "id": device_id("camera", "v4l2", node.name),
                "path": str(node),
                "index": int(index) if index.isdigit() else None,
                "name": name,
            }
        )
    return out


def list_network_interfaces() -> list[dict[str, Any]]:
    """Network interfaces, excluding CAN (which is reported separately)."""
    out: list[dict[str, Any]] = []
    if not _SYS_NET.is_dir():
        return out
    for interface in sorted(_SYS_NET.iterdir()):
        type_value = _read(interface / "type")
        if type_value is not None and int(type_value) == _ARPHRD_CAN:
            continue
        operstate = _read(interface / "operstate") or "unknown"
        out.append(
            {
                "id": device_id("net", "linux", interface.name),
                "interface": interface.name,
                "operstate": operstate,
                "up": operstate == "up",
                "mac": _read(interface / "address"),
                "mtu": int(_read(interface / "mtu") or 0),
                "loopback": interface.name == "lo",
            }
        )
    return out
