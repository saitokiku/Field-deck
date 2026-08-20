"""Debug probe discovery.

Identifying a probe is a USB enumeration question, not a target question: the
probe is on the bench, the target may not even be powered.  Nothing here
touches a target, reads a chip id, or asserts a reset line.
"""

from __future__ import annotations

from typing import Any

from fielddeck.common.process import have_tool, tool_version
from fielddeck.discovery.linux import list_usb_devices

__all__ = ["PROBE_IDS", "known_probes", "programming_tools"]

#: (vid, pid) -> (family, description, preferred tool).  Written from public
#: USB id lists; a probe missing here still shows in the USB inventory, it
#: just is not classified.
PROBE_IDS: dict[tuple[str, str], tuple[str, str, str]] = {
    ("0x0483", "0x3748"): ("stlink", "ST-Link/V2", "openocd"),
    ("0x0483", "0x374b"): ("stlink", "ST-Link/V2-1", "openocd"),
    ("0x0483", "0x374e"): ("stlink", "ST-Link/V3", "openocd"),
    ("0x0483", "0x374f"): ("stlink", "ST-Link/V3", "openocd"),
    ("0x0483", "0x3754"): ("stlink", "ST-Link/V3", "openocd"),
    ("0x1366", "0x0101"): ("jlink", "SEGGER J-Link", "pyocd"),
    ("0x1366", "0x0105"): ("jlink", "SEGGER J-Link", "pyocd"),
    ("0x1366", "0x1051"): ("jlink", "SEGGER J-Link", "pyocd"),
    ("0x1366", "0x1015"): ("jlink", "SEGGER J-Link", "pyocd"),
    ("0x1d50", "0x6018"): ("bmp", "Black Magic Probe", "gdb"),
    ("0x0d28", "0x0204"): ("cmsis-dap", "CMSIS-DAP / DAPLink", "pyocd"),
    ("0xc251", "0xf001"): ("cmsis-dap", "CMSIS-DAP (Keil)", "pyocd"),
    ("0x2e8a", "0x0003"): ("picoboot", "RP2040 in BOOTSEL mode", "picotool"),
    ("0x2e8a", "0x000c"): ("cmsis-dap", "Raspberry Pi Debug Probe", "openocd"),
    ("0x1209", "0x2444"): ("cmsis-dap", "Orbtrace", "openocd"),
    ("0x03eb", "0x2111"): ("edbg", "Atmel EDBG", "openocd"),
    ("0x1cbe", "0x00fd"): ("ti-icdi", "TI ICDI", "openocd"),
    ("0x0451", "0xbef3"): ("ti-xds", "TI XDS110", "openocd"),
    ("0x1a86", "0x8010"): ("wch-link", "WCH-Link", "openocd"),
    # Bootloader modes worth recognising, since they change the right tool.
    ("0x0483", "0xdf11"): ("dfu", "STM32 DFU bootloader", "dfu-util"),
    ("0x10c4", "0xea60"): ("esp-uart", "CP210x (often an ESP32 board)", "esptool"),
    ("0x303a", "0x1001"): ("esp-usb", "Espressif USB-JTAG/serial", "esptool"),
}

#: Programming and debug tools FieldDeck knows how to drive.
TOOLS: tuple[tuple[str, str], ...] = (
    ("openocd", "SWD/JTAG for most Cortex-M and many others"),
    ("pyocd", "CMSIS-DAP focused Cortex-M debug"),
    ("esptool.py", "Espressif ESP8266/ESP32 flashing"),
    ("avrdude", "AVR / Arduino classic programming"),
    ("dfu-util", "USB DFU class devices"),
    ("picotool", "RP2040 UF2 and BOOTSEL"),
)


def known_probes() -> list[dict[str, Any]]:
    """Classify attached USB devices that look like debug probes."""
    found: list[dict[str, Any]] = []
    for device in list_usb_devices():
        key = (str(device.get("vid", "")).lower(), str(device.get("pid", "")).lower())
        classification = PROBE_IDS.get(key)
        if classification is None:
            continue
        family, description, preferred = classification
        found.append(
            {
                **device,
                "family": family,
                "description": description,
                "preferred_tool": preferred,
                "tool_available": have_tool(preferred),
            }
        )
    return found


async def programming_tools() -> list[dict[str, Any]]:
    """Which programming tools are installed, and at what version."""
    return [
        {
            "tool": name,
            "purpose": purpose,
            "available": have_tool(name),
            "version": await tool_version(name) if have_tool(name) else None,
        }
        for name, purpose in TOOLS
    ]
