"""Network diagnostics.

Bench instruments increasingly speak SCPI over TCP, and a surprising share of
"the instrument is broken" turns out to be "the instrument is on a different
subnet".  This transport wraps the standard Linux tools rather than
reimplementing them.

The permission split follows what actually reaches the wire:

* reading interface state and capturing packets are **PASSIVE** — a capture
  is a receiver, it emits nothing
* ping, arping and port scanning are **QUERY** — they transmit at a host that
  may be a live piece of production equipment
* packet injection is deliberately **not implemented**.  There is no
  legitimate MVP workflow for it here, and an unrestricted injection tool
  reachable from an AI client is not something this project is going to ship.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from pydantic import Field, field_validator

from fielddeck.common.config import FieldDeckConfig
from fielddeck.common.errors import CaptureError, InvalidRequest, UnsupportedCapability
from fielddeck.common.models import (
    ConnectionState,
    DeviceCapability,
    DeviceDescriptor,
    DeviceRole,
    PermissionLevel,
    TransportKind,
)
from fielddeck.common.process import have_tool, run_tool, tool_version
from fielddeck.discovery.linux import list_network_interfaces
from fielddeck.drivers.base import ActionContext, DeviceParams, Driver, action

__all__ = ["NetworkDriver", "discover_network_drivers"]


def _require_private(target: str) -> str:
    """Refuse to probe anything outside a private/link-local range.

    FieldDeck is a bench and field tool.  A scan aimed at the public internet
    is either a mistake or something this project should not be helping with,
    and either way the operator can use nmap directly if they mean it.
    """
    try:
        address = ipaddress.ip_address(target)
    except ValueError:
        # A hostname: leave resolution to the tool, but keep it off the
        # obviously-public path by requiring a local-looking name.
        if "." in target and not target.endswith((".local", ".lan", ".internal", ".home")):
            raise InvalidRequest(
                f"{target!r} does not look like a local host; FieldDeck only probes "
                "private, link-local and .local addresses",
                details={"target": target},
                preserved="nothing was transmitted",
            ) from None
        return target
    if not (address.is_private or address.is_link_local or address.is_loopback):
        raise InvalidRequest(
            f"{target} is a public address; FieldDeck only probes private, "
            "link-local and loopback ranges",
            details={"target": target},
            preserved="nothing was transmitted",
        )
    return target


class PingParams(DeviceParams):
    target: str
    count: int = Field(default=4, ge=1, le=100)
    timeout_s: float = Field(default=5.0, gt=0, le=60)

    @field_validator("target")
    @classmethod
    def _local_only(cls, value: str) -> str:
        return _require_private(value)


class ScanParams(DeviceParams):
    target: str = Field(description="Host or CIDR, private ranges only")
    ports: str = Field(default="80,111,443,4880,5025,5555,111", max_length=200)
    timeout_s: float = Field(default=60.0, gt=0, le=600)

    @field_validator("target")
    @classmethod
    def _local_only(cls, value: str) -> str:
        network_part = value.split("/", 1)[0]
        _require_private(network_part)
        return value


class CaptureParams(DeviceParams):
    seconds: float = Field(default=5.0, gt=0, le=300)
    packet_filter: str = Field(default="", max_length=200, description="pcap filter expression")
    max_packets: int = Field(default=5000, ge=1, le=1_000_000)
    label: str = "netcap"


class NetworkDriver(Driver):
    """One network interface."""

    kind = TransportKind.NET

    def __init__(self, *, interface: str, mac: str | None, up: bool) -> None:
        descriptor = DeviceDescriptor(
            id=f"net:linux:{interface}",
            kind=TransportKind.NET,
            display_name=f"Network {interface}",
            path=f"/sys/class/net/{interface}",
            serial_number=mac,
            roles=[DeviceRole.BUS],
            capabilities=[DeviceCapability.RX, DeviceCapability.STREAM],
            state=ConnectionState.READY if up else ConnectionState.DISCOVERED,
            metadata={"interface": interface, "mac": mac},
        )
        super().__init__(descriptor)
        self.interface = interface

    async def status(self) -> dict[str, Any]:
        current = next(
            (entry for entry in list_network_interfaces() if entry["interface"] == self.interface),
            None,
        )
        return {
            "interface": self.interface,
            "up": bool(current and current["up"]),
            "operstate": current["operstate"] if current else "absent",
            "mac": current["mac"] if current else None,
            "mtu": current["mtu"] if current else None,
            "tools": {
                "ping": have_tool("ping"),
                "arping": have_tool("arping"),
                "nmap": have_tool("nmap"),
                "tcpdump": have_tool("tcpdump"),
            },
        }

    async def safe_state(self) -> dict[str, Any]:
        # FieldDeck never brings an interface up or down, so there is nothing
        # to undo. Taking a link down on safe state could disconnect the very
        # SSH session an operator is using to recover.
        return {
            "device": self.device_id,
            "applied": True,
            "changed": False,
            "state": "link state is not managed by FieldDeck",
        }

    # -- actions -----------------------------------------------------------

    @action(
        "net.status",
        permission=PermissionLevel.PASSIVE,
        params=DeviceParams,
        state_changing=False,
        description="Interface link state, MAC and MTU.",
        allowed_during_estop=True,
    )
    async def net_status(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        return await self.status()

    @action(
        "net.capture",
        permission=PermissionLevel.PASSIVE,
        params=CaptureParams,
        state_changing=False,
        description="Capture packets to a pcap file. Receives only.",
        cancelable=True,
        timeout_s=360.0,
    )
    async def net_capture(self, ctx: ActionContext, params: CaptureParams) -> dict[str, Any]:
        """PASSIVE: a capture is a receiver and transmits nothing."""
        if not have_tool("tcpdump"):
            raise UnsupportedCapability(
                "tcpdump is not installed; install with: sudo apt install tcpdump",
                details={"tool": "tcpdump"},
            )
        if ctx.recorder is None:
            raise CaptureError("packet capture needs an active session to write into")

        path = ctx.recorder.capture_path("logic", f"{self.interface}-{params.label}", ".pcap")
        args = [
            "-i",
            self.interface,
            "-w",
            str(path),
            "-c",
            str(params.max_packets),
            # Never resolve names: a reverse lookup is a transmission the
            # operator did not ask for on an interface they are observing.
            "-n",
            "-G",
            str(int(params.seconds)),
            "-W",
            "1",
        ]
        if params.packet_filter:
            args.append(params.packet_filter)

        result = await run_tool(
            "tcpdump",
            args,
            timeout_s=params.seconds + 15.0,
            allowed_path_roots=[ctx.recorder.root],
        )
        if not path.exists():
            raise CaptureError(
                "tcpdump produced no capture; check that FieldDeck has CAP_NET_RAW "
                "or that the interface exists",
                details={"stderr": result.stderr[-800:], "command": result.command_line},
                preserved="the session is unchanged",
            )
        artifact = ctx.recorder.add_artifact(
            path,
            kind="logic",
            media_type="application/vnd.tcpdump.pcap",
            device_id=self.device_id,
            raw=True,
            metadata={"interface": self.interface, "filter": params.packet_filter},
        )
        return {
            "artifact": artifact.model_dump(mode="json"),
            "interface": self.interface,
            "filter": params.packet_filter or None,
            "size_bytes": artifact.size_bytes,
        }

    @action(
        "net.ping",
        permission=PermissionLevel.QUERY,
        params=PingParams,
        state_changing=False,
        description="ICMP echo to a private-range host.",
        timeout_s=90.0,
    )
    async def net_ping(self, ctx: ActionContext, params: PingParams) -> dict[str, Any]:
        """QUERY: this transmits at a host that may be live equipment."""
        result = await run_tool(
            "ping",
            [
                "-c",
                str(params.count),
                "-W",
                str(int(params.timeout_s)),
                "-I",
                self.interface,
                params.target,
            ],
            timeout_s=params.timeout_s * params.count + 10.0,
        )
        return {
            "target": params.target,
            "interface": self.interface,
            "reachable": result.returncode == 0,
            "output": result.stdout.strip().splitlines()[-4:],
            "returncode": result.returncode,
        }

    @action(
        "net.scan",
        permission=PermissionLevel.QUERY,
        params=ScanParams,
        state_changing=False,
        description="Port scan a private-range host or subnet to find instruments.",
        timeout_s=660.0,
        cancelable=True,
    )
    async def net_scan(self, ctx: ActionContext, params: ScanParams) -> dict[str, Any]:
        """QUERY, and restricted to private ranges.

        The default port list is the one that finds bench instruments: 5025 is
        SCPI-raw, 4880 is VXI-11/HiSLIP-adjacent, 111 is the portmapper VXI-11
        needs.
        """
        if not have_tool("nmap"):
            raise UnsupportedCapability(
                "nmap is not installed; install with: sudo apt install nmap",
                details={"tool": "nmap"},
            )
        result = await run_tool(
            "nmap",
            ["-Pn", "-p", params.ports, "--open", "-oG", "-", params.target],
            timeout_s=params.timeout_s,
        )
        hosts: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            if not line.startswith("Host:") or "Ports:" not in line:
                continue
            address = line.split()[1]
            ports_part = line.split("Ports:", 1)[1]
            open_ports = [
                chunk.strip().split("/")[0] for chunk in ports_part.split(",") if "/open/" in chunk
            ]
            if open_ports:
                hosts.append({"address": address, "open_ports": open_ports})
        return {
            "target": params.target,
            "hosts": hosts,
            "count": len(hosts),
            "nmap_version": await tool_version("nmap"),
            "note": "open port 5025 usually means a SCPI-raw instrument socket",
        }


def discover_network_drivers(config: FieldDeckConfig) -> list[Driver]:
    """Enumerate interfaces, excluding loopback."""
    return [
        NetworkDriver(interface=entry["interface"], mac=entry["mac"], up=entry["up"])
        for entry in list_network_interfaces()
        if not entry["loopback"]
    ]
