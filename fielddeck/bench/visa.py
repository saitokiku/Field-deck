"""Real bench instruments over SCPI/VISA.

This is the subsystem that can put voltage on a DUT, so the rules it follows
are worth reading before changing anything here.

**Enumeration never asks who you are.**  ``*IDN?`` is a transmission, which
makes it QUERY-class work an operator authorises; discovery is Stage 1
inventory and stays silent.  What discovery does instead is read sysfs for USB
devices whose interface descriptor says USBTMC (class 0xFE, subclass 0x03) and
turn each into a resource name.  That is the same information the kernel
already read when the device enumerated, and it costs the instrument nothing.

**The VISA library's own resource list is not passive.**  pyvisa-py's
``list_resources()`` asks every registered session class for its devices no
matter what filter you pass, and its TCPIP class discovers instruments by
sending a VXI-11 RPC portmapper request to the broadcast address and waiting a
second for replies.  That is active network probing — QUERY-class by
FieldDeck's own rules — so it is opt-in via ``FIELDDECK_VISA_DISCOVERY=backend``
and never the default.

**Serial and LAN instruments are declared, not sniffed.**  Nothing in a byte
stream distinguishes a bench supply from any other UART without transmitting
to it, and ``/dev/ttyUSB0`` already belongs to the serial transport, which
would then be fighting this one for the port.  A LAN instrument cannot be seen
at all without probing the network.  Both therefore come from
``config/instruments/*.yaml``, where the operator names the resource.

**A profile is a hypothesis until the instrument confirms its identity.**  No
typed control action works before ``bench.identify`` binds a profile.  The one
thing an operator-declared profile is trusted for without an identity query is
the direction that can only make things safer: sending that model's output-off
command during boot, ESTOP or lease expiry.  Without a declared profile, an
instrument FieldDeck has never identified is also an instrument FieldDeck has
never energised, and safe state says so rather than guessing a command.

**Closing a session is not a safe state.**  A supply holds its output exactly
as it was when the control channel disappeared.  If instrumentd is restarted
while a rail is live, the new process starts with no profile bound and cannot
turn that rail off — declare the instrument's profile if that matters on your
bench, and treat the rail as live until an instrument says otherwise.

Action names and parameter shapes mirror :mod:`fielddeck.sim.psu` and
:mod:`fielddeck.sim.dmm` so the CLI, HMI, MCP surface and recipes work
unchanged against a simulated instrument or a real one.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator

from fielddeck.bench.profiles import (
    GENERIC_SCPI,
    Identity,
    InstrumentProfile,
    match_profile,
    parse_idn,
    profile_by_key,
)
from fielddeck.bench.scpi import (
    AUTO,
    ScpiTransport,
    parse_resource,
    parse_scpi_error,
    require_query,
)
from fielddeck.common.config import FieldDeckConfig
from fielddeck.common.errors import (
    FieldDeckError,
    InvalidRequest,
    ProtocolError,
    UnsupportedCapability,
)
from fielddeck.common.ids import device_id as make_device_id
from fielddeck.common.ids import usb_serial_id
from fielddeck.common.logging import get_logger
from fielddeck.common.models import (
    ConnectionState,
    DeviceCapability,
    DeviceDescriptor,
    DeviceRole,
    PermissionLevel,
    StrictModel,
    TransportKind,
)
from fielddeck.common.paths import Paths, default_paths
from fielddeck.common.timebase import Timestamp
from fielddeck.discovery.linux import list_usb_devices
from fielddeck.drivers.base import ActionContext, DeviceParams, Driver, action
from fielddeck.safety.limits import DerivedLimitCheck, LimitCheck

__all__ = [
    "BenchInstrumentDriver",
    "DeclaredInstrument",
    "discover_visa_drivers",
]

_log = get_logger("fielddeck.bench.visa")

#: USB interface class/subclass that says "this is a USBTMC instrument".  From
#: the USB Test and Measurement Class specification.
_USBTMC_CLASS = 0xFE
_USBTMC_SUBCLASS = 0x03

_SYS_USB = Path("/sys/bus/usb/devices")

#: Selects how discovery finds instruments.  ``passive`` reads sysfs only.
#: ``backend`` additionally asks the VISA library for its resource list, which
#: on pyvisa-py broadcasts a VXI-11 discovery request onto the network.
_DISCOVERY_ENV = "FIELDDECK_VISA_DISCOVERY"

#: Safe-state work gets a short deadline of its own: driving an output off must
#: not sit behind a stuck exchange until the daemon's 10 s cap kills it.
_SAFE_STATE_TIMEOUT_S = 2.0
_SAFE_STATE_LOCK_S = 3.0

#: Tolerance used when comparing a setpoint against what the instrument reads
#: back.  Supplies quantise setpoints, so an exact match is the wrong test.
_READBACK_REL = 0.01
_READBACK_ABS = 0.02


# ---------------------------------------------------------------------------
# Operator-declared instruments
# ---------------------------------------------------------------------------


class DeclaredInstrument(StrictModel):
    """One instrument named by the operator in ``config/instruments/*.yaml``.

    Declarations exist for the instruments that cannot be enumerated without
    transmitting: LAN instruments and serial ones.  They may pin a profile by
    key, but never supply command text — raw command strings would be a way
    around the typed actions the permission model reasons about.

    ``read_termination`` and ``write_termination`` take ``"auto"`` (derive it
    from the resource class), ``""`` (no terminator, which some supplies want),
    or the literal characters.
    """

    resource: str
    name: str | None = None
    #: Profile key from :mod:`fielddeck.bench.profiles`, e.g. ``rigol.dp800``.
    profile: str | None = None
    channel: int = Field(default=1, ge=1, le=8)
    read_termination: str = AUTO
    write_termination: str = AUTO
    timeout_s: float = Field(default=5.0, gt=0, le=120)
    note: str | None = None

    @field_validator("resource")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(
                "resource must be a VISA resource name, e.g. TCPIP0::10.0.0.5::5025::SOCKET"
            )
        return value.strip()


def _declared_instruments(paths: Paths | None = None) -> list[DeclaredInstrument]:
    """Read every declaration file.

    A broken file is logged and skipped rather than raised: an unreadable
    instrument declaration must not stop the daemon from coming up with the
    hardware it *can* see.  Unlike ``safety.yaml``, nothing here can widen
    what an operator is allowed to do.
    """
    directory = (paths or default_paths()).instruments_dir
    if not directory.is_dir():
        return []
    declared: list[DeclaredInstrument] = []
    for file in sorted(directory.glob("*.y*ml")):
        try:
            raw = yaml.safe_load(file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            _log.error(
                "instrument declaration unreadable; skipping it",
                extra={"path": str(file), "error": str(exc)},
            )
            continue
        entries = raw.get("instruments", []) if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            _log.error(
                "instrument declaration must be a list, or a mapping with an "
                "'instruments' list; skipping it",
                extra={"path": str(file)},
            )
            continue
        for entry in entries:
            try:
                declared.append(DeclaredInstrument.model_validate(entry))
            except Exception as exc:  # noqa: BLE001 - one bad entry must not hide the rest of the file
                _log.error(
                    "invalid instrument declaration; skipping it",
                    extra={"path": str(file), "entry": repr(entry), "error": str(exc)},
                )
    return declared


# ---------------------------------------------------------------------------
# Passive USBTMC inventory
# ---------------------------------------------------------------------------


def _read_sysfs(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _usbtmc_interface(device_dir: Path) -> dict[str, Any] | None:
    """Return USBTMC interface details for a USB device, or None.

    Reads interface descriptors the kernel already cached.  A device is only
    treated as an instrument when it actually advertises the Test and
    Measurement class; matching on vendor id would put a vendor's oscilloscope
    and its power supply into the same bucket.
    """
    if not device_dir.is_dir():
        return None
    for interface in sorted(device_dir.glob(f"{device_dir.name}:*")):
        klass = _read_sysfs(interface / "bInterfaceClass")
        subclass = _read_sysfs(interface / "bInterfaceSubClass")
        if klass is None or subclass is None:
            continue
        try:
            if int(klass, 16) != _USBTMC_CLASS or int(subclass, 16) != _USBTMC_SUBCLASS:
                continue
        except ValueError:  # pragma: no cover - sysfs would have to lie
            continue
        driver = interface / "driver"
        return {
            "interface": interface.name,
            "protocol": _read_sysfs(interface / "bInterfaceProtocol"),
            # When the kernel's usbtmc driver has claimed the interface, a
            # libusb-based backend has to detach it first; that shows up as a
            # busy device rather than a missing one.
            "kernel_driver": driver.resolve().name if driver.is_symlink() else None,
        }
    return None


def _usbtmc_instruments(sysfs_root: Path | None = None) -> list[dict[str, Any]]:
    """Every attached USBTMC instrument, from sysfs only.

    Reuses the shared passive USB inventory for vendor metadata and adds the
    one thing it does not carry: whether an interface speaks USBTMC.
    """
    root = sysfs_root or _SYS_USB
    found: list[dict[str, Any]] = []
    for entry in list_usb_devices():
        path = entry.get("path")
        if not isinstance(path, str):
            continue
        usbtmc = _usbtmc_interface(root / path)
        if usbtmc is None:
            continue
        vid = str(entry.get("vid") or "").removeprefix("0x")
        pid = str(entry.get("pid") or "").removeprefix("0x")
        serial = entry.get("serial")
        serial = str(serial) if serial else None
        # usb_serial_id is consulted for the stability verdict rather than the
        # string: the id is composed from separate components so it comes out
        # as visa:usb:<vid>:<pid>:<serial>, the shape the rest of FieldDeck
        # writes into sessions, aliases and recipes.
        _identity, stable = usb_serial_id(
            int(vid, 16) if vid else None, int(pid, 16) if pid else None, serial
        )
        found.append(
            {
                "id": make_device_id("visa", "usb", vid or "xxxx", pid or "xxxx", serial),
                "resource": _usb_resource(vid, pid, serial),
                "vid": vid,
                "pid": pid,
                "serial": serial,
                "stable_id": stable,
                "manufacturer": entry.get("manufacturer"),
                "product": entry.get("product"),
                "sysfs_path": path,
                "usbtmc": usbtmc,
            }
        )
    return found


def _usb_resource(vid: str, pid: str, serial: str | None) -> str:
    """Build the VISA resource name for a USBTMC device.

    The board index is 0 because VISA backends resolve a USB instrument by
    vendor/product/serial rather than by that number.  Without a serial number
    the name is ambiguous between two identical instruments, which is why the
    descriptor is flagged as an unstable id in that case.
    """
    tail = f"::{serial}" if serial else ""
    return f"USB0::0x{vid}::0x{pid}{tail}::INSTR"


def _backend_resources() -> list[str]:
    """Ask the VISA library what it can see.

    Opt-in only.  On pyvisa-py this broadcasts a VXI-11 discovery request to
    the local network and blocks for about a second; that is active probing,
    which FieldDeck classifies as QUERY-level work rather than inventory.
    """
    try:
        import pyvisa
    except ImportError:  # pragma: no cover - guarded by the caller
        return []
    try:
        manager = pyvisa.ResourceManager()
        return [str(name) for name in manager.list_resources("?*::INSTR")]
    except Exception as exc:  # noqa: BLE001 - a backend that cannot enumerate must not stop discovery
        _log.warning("VISA backend enumeration failed", extra={"error": str(exc)})
        return []


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_visa_drivers(config: FieldDeckConfig) -> list[Driver]:
    """Enumerate bench instruments without talking to any of them.

    Returns an empty list, and says why in the log, when pyvisa is missing:
    the instruments may well be plugged in, but nothing here could drive them.

    ``config`` is part of the discovery-hook contract.  Nothing in
    ``fielddeck.yaml`` describes an instrument's dialect today, and inventing
    one from the aliases would be exactly the guess this module refuses to
    make; declarations live in ``config/instruments`` instead.
    """
    if not _module_available("pyvisa"):
        _log.info(
            "pyvisa is not installed; bench instruments are not drivable "
            "(pip install 'fielddeck[bench]')",
            extra={"module": "pyvisa"},
        )
        return []

    pyusb_missing = not _module_available("usb")
    drivers: list[Driver] = []
    claimed: set[str] = set()

    for declaration in _declared_instruments():
        claimed.add(_resource_key(declaration.resource))
        drivers.append(_driver_from_declaration(declaration))

    for instrument in _usbtmc_instruments():
        if _resource_key(str(instrument["resource"])) in claimed:
            # The operator declared this one explicitly; their framing and
            # profile win over anything derived from sysfs.
            continue
        claimed.add(_resource_key(str(instrument["resource"])))
        drivers.append(_driver_from_usbtmc(instrument, pyusb_missing=pyusb_missing))

    if _discovery_mode() == "backend":
        for name in _backend_resources():
            key = _resource_key(name)
            if key in claimed:
                continue
            claimed.add(key)
            drivers.append(_driver_from_backend(name))

    if drivers:
        _log.info(
            "bench instruments enumerated",
            extra={"count": len(drivers), "identified": 0},
        )
    return drivers


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # pragma: no cover - broken install
        return False


def _discovery_mode() -> str:
    value = os.environ.get(_DISCOVERY_ENV, "passive").strip().lower()
    if value not in {"passive", "backend"}:
        _log.warning(
            "unknown VISA discovery mode; staying passive",
            extra={"value": value, "env": _DISCOVERY_ENV},
        )
        return "passive"
    return value


def _resource_key(resource: str) -> str:
    """Compare resource names by what they address, not how they are spelled.

    ``USB0::0x1234::0x5678::SER::INSTR`` and ``USB::0x1234::0x5678::SER::INSTR``
    are the same instrument, and the operator's declaration must win over the
    sysfs-derived entry rather than creating a second driver for one device.
    """
    parsed = parse_resource(resource)
    if parsed.is_usb:
        return f"usb:{parsed.vendor_id}:{parsed.product_id}:{parsed.serial_number}"
    if parsed.interface == "tcpip":
        return f"tcpip:{parsed.host}:{parsed.port}"
    if parsed.is_serial:
        return f"asrl:{parsed.port_name}"
    return resource.strip().upper()


def _driver_from_usbtmc(instrument: dict[str, Any], *, pyusb_missing: bool) -> Driver:
    vendor = instrument.get("manufacturer")
    product = instrument.get("product")
    label = " ".join(str(part) for part in (vendor, product) if part) or "USBTMC instrument"
    warning = None
    if pyusb_missing:
        warning = (
            "pyusb is not installed; the pyvisa-py backend cannot open a USBTMC session "
            "without it (pip install 'fielddeck[bench]')"
        )
    return BenchInstrumentDriver(
        device_id=str(instrument["id"]),
        resource=str(instrument["resource"]),
        display_name=f"{label} (unidentified)",
        vendor=str(vendor) if vendor else None,
        product=str(product) if product else None,
        serial_number=instrument.get("serial"),
        stable_id=bool(instrument.get("stable_id", False)),
        origin="usbtmc",
        warning=warning,
        metadata={
            "usb": {
                "vid": f"0x{instrument['vid']}",
                "pid": f"0x{instrument['pid']}",
                "sysfs_path": instrument.get("sysfs_path"),
                "kernel_driver": (instrument.get("usbtmc") or {}).get("kernel_driver"),
            }
        },
    )


def _driver_from_declaration(declaration: DeclaredInstrument) -> Driver:
    """Build a driver for an instrument the operator named.

    A declaration that pins an unknown profile still produces a driver: the
    instrument is real and worth showing, and ``bench.identify`` can still bind
    a profile from its identity.  What is dropped is the pin, because binding a
    dialect the operator did not actually name is the guess this module exists
    to avoid.
    """
    warnings: list[str] = []
    profile: InstrumentProfile | None = None
    if declaration.profile is not None:
        profile = profile_by_key(declaration.profile)
        if profile is None:
            message = (
                f"declared profile {declaration.profile!r} is not a known profile; the "
                "declaration is kept but nothing is pinned — run bench.identify"
            )
            _log.error(
                "declared instrument names an unknown profile",
                extra={"resource": declaration.resource, "profile": declaration.profile},
            )
            warnings.append(message)
    parsed = parse_resource(declaration.resource)
    if parsed.is_serial:
        warnings.append(
            "declared as a serial VISA resource: the serial transport can see the same "
            "device node, and only one of them may hold it open at a time"
        )
    return BenchInstrumentDriver(
        device_id=make_device_id(
            "visa", parsed.interface, *_resource_identity(declaration.resource)
        ),
        resource=declaration.resource,
        display_name=declaration.name or f"Declared instrument {declaration.resource}",
        stable_id=True,
        declared_profile=profile,
        channel=declaration.channel,
        read_termination=declaration.read_termination,
        write_termination=declaration.write_termination,
        timeout_s=declaration.timeout_s,
        origin="declared",
        warning="; ".join(warnings) or None,
        metadata={"declaration": declaration.model_dump(mode="json")},
    )


def _driver_from_backend(resource: str) -> Driver:
    parsed = parse_resource(resource)
    return BenchInstrumentDriver(
        device_id=make_device_id("visa", parsed.interface, *_resource_identity(resource)),
        resource=resource,
        display_name=f"VISA instrument {resource} (unidentified)",
        serial_number=parsed.serial_number,
        stable_id=parsed.serial_number is not None,
        origin="backend",
        metadata={"discovered_by": "visa backend list_resources"},
    )


def _resource_identity(resource: str) -> tuple[str, ...]:
    """Identity components for a device id, most significant first.

    Kept as separate components so the composed id reads
    ``visa:usb:0957:1798:MY53100101`` rather than having its separators
    flattened; sessions, aliases and recipes are written against that shape.
    """
    parsed = parse_resource(resource)
    if parsed.is_usb:
        parts = [parsed.vendor_id or "xxxx", parsed.product_id or "xxxx"]
        if parsed.serial_number:
            parts.append(parsed.serial_number)
        return tuple(parts)
    if parsed.interface == "tcpip":
        host = parsed.host or "unknown"
        return (host, str(parsed.port)) if parsed.port else (host,)
    if parsed.is_serial:
        return (parsed.port_name or resource,)
    return (resource,)


# ---------------------------------------------------------------------------
# Parameters — shapes mirror fielddeck.sim.psu and fielddeck.sim.dmm
# ---------------------------------------------------------------------------


class ChannelParams(DeviceParams):
    #: None means the channel the operator configured for this instrument.
    channel: int | None = Field(default=None, ge=1, le=8)


class ScpiQueryParams(DeviceParams):
    command: str = Field(min_length=1, max_length=256)


class PsuSetParams(ChannelParams):
    voltage: float | None = Field(default=None, ge=0)
    current_limit: float | None = Field(default=None, ge=0)


class PsuOutputParams(ChannelParams):
    enabled: bool
    #: How long the output may stay on without a renewal.
    lease_ttl_s: float = Field(default=30.0, gt=0, le=3600)


class DmmMeasureParams(DeviceParams):
    function: str = Field(default="dc_voltage")
    samples: int = Field(default=1, ge=1, le=256)


class LoadSetParams(DeviceParams):
    mode: str = Field(default="current", pattern="^(current|resistance|power)$")
    current: float | None = Field(default=None, ge=0)
    resistance: float | None = Field(default=None, gt=0)
    power: float | None = Field(default=None, ge=0)


class LoadInputParams(DeviceParams):
    enabled: bool
    lease_ttl_s: float = Field(default=30.0, gt=0, le=3600)


def _output_permission(params: Any) -> PermissionLevel:
    """Enabling an output is POWER; disabling one is always allowed."""
    return PermissionLevel.POWER if params.enabled else PermissionLevel.PASSIVE


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class BenchInstrumentDriver(Driver):
    """One SCPI instrument reached over VISA.

    Deliberately one class rather than a PSU class and a DMM class: what an
    instrument *is* only becomes known when ``bench.identify`` runs, and a
    device whose action list changes underneath a client that cached it is a
    worse problem than an action that answers "this instrument has no such
    command".  Every typed action therefore checks the bound profile first and
    refuses, with the reason, when the profile does not carry that command.
    """

    kind = TransportKind.VISA

    def __init__(
        self,
        *,
        device_id: str,
        resource: str,
        display_name: str,
        vendor: str | None = None,
        product: str | None = None,
        serial_number: str | None = None,
        stable_id: bool = True,
        declared_profile: InstrumentProfile | None = None,
        channel: int = 1,
        read_termination: str = AUTO,
        write_termination: str = AUTO,
        timeout_s: float = 5.0,
        origin: str = "usbtmc",
        warning: str | None = None,
        metadata: dict[str, Any] | None = None,
        transport: ScpiTransport | None = None,
    ) -> None:
        parsed = parse_resource(resource)
        descriptor = DeviceDescriptor(
            id=device_id,
            kind=TransportKind.VISA,
            display_name=display_name,
            path=resource,
            vendor=vendor,
            product=product,
            serial_number=serial_number,
            # Until an identity query runs, all that is known is "something
            # that probably speaks SCPI".  Roles and capabilities are filled in
            # by bench.identify, not guessed from a vendor id.
            roles=[DeviceRole.GENERIC_SCPI],
            capabilities=[],
            permission_floor=PermissionLevel.PASSIVE,
            state=ConnectionState.DISCOVERED,
            stable_id=stable_id,
            warning=warning
            or ("identity unknown: run bench.identify (QUERY) before any typed bench action"),
            metadata={
                "resource": resource,
                "interface": parsed.interface,
                "origin": origin,
                "identified": False,
                "profile": None,
                "declared_profile": declared_profile.key if declared_profile else None,
                "channel": channel,
                "hardware_verified": False,
                **(metadata or {}),
            },
        )
        super().__init__(descriptor)
        self.resource = resource
        #: Warnings that stay true after identification — port contention, a
        #: missing backend library — as opposed to "identity unknown", which
        #: bench.identify clears.
        self._standing_warning = warning
        self._declared_profile = declared_profile
        self._profile: InstrumentProfile | None = None
        self._identity: Identity | None = None
        self._channel = channel
        dialect = declared_profile.dialect if declared_profile else None
        self._transport = transport or ScpiTransport(
            resource,
            device_id=device_id,
            timeout_s=dialect.timeout_s if dialect else timeout_s,
            read_termination=dialect.read_termination if dialect else read_termination,
            write_termination=dialect.write_termination if dialect else write_termination,
            min_command_interval_s=dialect.min_command_interval_s if dialect else 0.0,
        )
        #: What FieldDeck last commanded.  None means "never commanded here",
        #: which is not the same as "off" — see the module docstring.
        self._output_on: bool | None = None
        self._load_input_on: bool | None = None
        self._setpoints: dict[str, float | None] = {"voltage": None, "current_limit": None}
        self._load_setpoint: dict[str, Any] = {"mode": None, "value": None}
        self._last_measurement: dict[str, Any] | None = None

    # -- identity and state ------------------------------------------------

    @property
    def transport(self) -> ScpiTransport:
        return self._transport

    @property
    def profile(self) -> InstrumentProfile | None:
        """The profile bound by ``bench.identify``, if any."""
        return self._profile

    async def status(self) -> dict[str, Any]:
        """Everything the driver knows without transmitting anything."""
        profile = self._profile
        return {
            "resource": self.resource,
            "identified": profile is not None,
            # Scalar first, detail alongside: a client written against the
            # simulated instruments reads result["identity"] and
            # result["profile"] as strings, and must keep working here.
            "identity": self._identity.raw if self._identity else None,
            "identity_fields": self._identity.describe() if self._identity else None,
            "profile": profile.key if profile else None,
            "profile_detail": profile.describe() if profile else None,
            "declared_profile": self._declared_profile.key if self._declared_profile else None,
            "role": str(profile.role) if profile else None,
            "channel": self._channel,
            "output": self._output_on,
            "load_input": self._load_input_on,
            "setpoints": dict(self._setpoints),
            "load_setpoint": dict(self._load_setpoint),
            "last_measurement": self._last_measurement,
            "state": str(self._descriptor.state),
            "transport": self._transport.describe(),
            "hardware_verified": False,
            "note": (
                "output and setpoint values are what FieldDeck last commanded, not a "
                "live readback; use psu.measure (QUERY) for the instrument's own view"
            ),
            "warning": self._descriptor.warning,
        }

    async def disconnect(self) -> None:
        """Close the VISA session.

        Closing does not change the instrument: an output that was on stays on.
        Safe state is a separate, explicit step.
        """
        await self._transport.close()
        await super().disconnect()

    async def safe_state(self) -> dict[str, Any]:
        """Disable every output and load this instrument has.

        Runs on ESTOP, lease expiry, daemon start and daemon stop, and never
        consults the authorization state — refusing to turn an output off
        because a grant lapsed would be the opposite of a safety system.

        With no profile bound there is no known output-off command.  That is
        reported honestly rather than guessed at, and it is survivable: an
        instrument FieldDeck has not identified is one FieldDeck has never
        energised.  Declaring the model in ``config/instruments`` closes that
        gap for a bench where it matters.
        """
        profile = self._profile or self._declared_profile
        if profile is None:
            return {
                "device": self.device_id,
                "applied": False,
                "reason": (
                    "no instrument profile is bound, so no output-off command is known; "
                    "FieldDeck has never enabled an output on this instrument either"
                ),
                "hint": (
                    "run bench.identify, or declare the model in config/instruments so "
                    "safe state works without an identity query"
                ),
            }

        commands = self._safe_state_commands(profile)
        if not commands:
            return {
                "device": self.device_id,
                "applied": False,
                "reason": f"{profile.display_name} has no output or load input to disable",
            }

        sent: list[str] = []
        errors: list[dict[str, str]] = []
        for command in commands:
            try:
                await self._transport.write(
                    command,
                    timeout_s=_SAFE_STATE_TIMEOUT_S,
                    lock_timeout_s=_SAFE_STATE_LOCK_S,
                )
                sent.append(command)
            except FieldDeckError as exc:
                errors.append({"command": command, "error": exc.message})
            except Exception as exc:  # noqa: BLE001 - safe state reports every failure and keeps going
                errors.append({"command": command, "error": str(exc)})
        if sent:
            was_on = bool(self._output_on) or bool(self._load_input_on)
            self._output_on = False if profile.can_switch_output else self._output_on
            self._load_input_on = False if profile.can_switch_load else self._load_input_on
        else:
            was_on = False
        if errors:
            _log.error(
                "safe state incomplete on bench instrument",
                extra={"device": self.device_id, "errors": errors},
            )
        return {
            "device": self.device_id,
            "applied": bool(sent) and not errors,
            "changed": was_on,
            "state": "outputs and loads commanded off" if sent else "nothing was sent",
            "commands": sent,
            "errors": errors,
            "profile": profile.key,
        }

    def _safe_state_commands(self, profile: InstrumentProfile) -> list[str]:
        """Every off command this instrument needs, all channels included.

        A three-channel supply with only channel 1 turned off is still
        energised, so safe state addresses each channel the profile knows
        about rather than only the configured one.
        """
        dialect = profile.dialect
        commands: list[str] = []
        if profile.can_switch_output and dialect.output_off is not None:
            for channel in range(1, max(1, dialect.channels) + 1):
                command = _render(dialect.output_off, channel=channel)
                if command not in commands:
                    commands.append(command)
        if profile.can_switch_load and dialect.load_input_off is not None:
            commands.append(_render(dialect.load_input_off))
        return commands

    # -- profile plumbing --------------------------------------------------

    def _require_profile(self, what: str) -> InstrumentProfile:
        if self._profile is None:
            raise UnsupportedCapability(
                f"{self.device_id} has not been identified, so {what} has no known command. "
                "Run bench.identify (QUERY) first; FieldDeck will not guess a SCPI dialect "
                "from a USB vendor id.",
                details={
                    "device_id": self.device_id,
                    "action": what,
                    "resource": self.resource,
                    "declared_profile": (
                        self._declared_profile.key if self._declared_profile else None
                    ),
                },
                preserved="nothing was sent to the instrument",
            )
        return self._profile

    def _require_command(self, profile: InstrumentProfile, supported: bool, what: str) -> None:
        if supported:
            return
        raise UnsupportedCapability(
            f"the {profile.display_name} profile ({profile.key}) has no {what} command, so "
            f"{self.device_id} cannot do that. A command borrowed from another vendor is "
            "how the wrong thing gets energised.",
            details={
                "device_id": self.device_id,
                "profile": profile.key,
                "role": str(profile.role),
                "needed": what,
                "supported_actions": list(profile.supported_actions()),
            },
            preserved="nothing was sent to the instrument",
        )

    def _resolve_channel(self, requested: int | None, profile: InstrumentProfile) -> int:
        channel = requested if requested is not None else self._channel
        if channel > profile.dialect.channels:
            raise InvalidRequest(
                f"{profile.display_name} has {profile.dialect.channels} channel(s); "
                f"channel {channel} does not exist",
                details={
                    "device_id": self.device_id,
                    "channel": channel,
                    "channels": profile.dialect.channels,
                },
                preserved="nothing was sent to the instrument",
            )
        return channel

    async def _select_channel(self, profile: InstrumentProfile, channel: int) -> None:
        """Point a channel-selecting instrument at the right channel.

        Rigol supplies apply ``:VOLT`` to whichever channel is selected, so the
        selection has to happen in the same exchange as the setpoint or a
        setpoint lands on the channel someone else selected.
        """
        template = profile.dialect.select_channel
        if template is None:
            return
        await self._transport.write(_render(template, channel=channel))

    async def _check_error_queue(self, profile: InstrumentProfile, what: str) -> dict[str, Any]:
        """Ask the instrument whether it accepted the last command.

        SCPI writes are fire-and-forget: an out-of-range or misspelled command
        goes into the error queue and the instrument carries on at its old
        setpoint.  Without this check, "psu.set succeeded" would mean nothing
        more than "the bytes left the Pi".
        """
        query = profile.dialect.error_query
        if query is None:
            return {
                "error_queue": None,
                "accepted": None,
                "note": (
                    f"{profile.display_name} has no error queue; the setpoint readback "
                    "below is the only confirmation available"
                ),
            }
        response = await self._transport.query(query)
        code, message = parse_scpi_error(response)
        if code != 0:
            raise ProtocolError(
                f"{self.device_id} rejected {what}: {code}, {message}",
                details={
                    "device_id": self.device_id,
                    "profile": profile.key,
                    "scpi_error_code": code,
                    "scpi_error": message,
                },
                preserved=(
                    "the instrument's own error queue reports the command was not applied; "
                    "read psu.measure to see what it is actually doing"
                ),
            )
        return {"error_queue": f"{code},{message}", "accepted": True}

    async def _read_setpoints(
        self, profile: InstrumentProfile, channel: int
    ) -> dict[str, float | None]:
        """Read back voltage and current setpoints where the profile can."""
        dialect = profile.dialect
        readback: dict[str, float | None] = {"voltage": None, "current_limit": None}
        if dialect.query_voltage_setpoint is not None:
            readback["voltage"] = _to_float(
                await self._transport.query(
                    _render(dialect.query_voltage_setpoint, channel=channel)
                ),
                device_id=self.device_id,
                what="voltage setpoint",
            )
        if dialect.query_current_setpoint is not None:
            readback["current_limit"] = _to_float(
                await self._transport.query(
                    _render(dialect.query_current_setpoint, channel=channel)
                ),
                device_id=self.device_id,
                what="current setpoint",
            )
        return readback

    async def _measure_vip(self, profile: InstrumentProfile, channel: int) -> dict[str, Any]:
        """Voltage, current and power as the instrument reports them."""
        dialect = profile.dialect
        voltage: float | None = None
        current: float | None = None
        power: float | None = None
        if dialect.measure_voltage is not None:
            voltage = _to_float(
                await self._transport.query(_render(dialect.measure_voltage, channel=channel)),
                device_id=self.device_id,
                what="measured voltage",
            )
        if dialect.measure_current is not None:
            current = _to_float(
                await self._transport.query(_render(dialect.measure_current, channel=channel)),
                device_id=self.device_id,
                what="measured current",
            )
        if dialect.measure_power is not None:
            power = _to_float(
                await self._transport.query(_render(dialect.measure_power, channel=channel)),
                device_id=self.device_id,
                what="measured power",
            )
        # A supply that cannot report power itself still gives V and I, and
        # V x I is worth having on the timeline as long as it is labelled as
        # computed rather than measured.
        computed = False
        if power is None and voltage is not None and current is not None:
            power = round(voltage * current, 6)
            computed = True
        return {
            "voltage": voltage,
            "current": current,
            "power": power,
            "power_measured": not computed and power is not None,
        }

    def _record(
        self,
        ctx: ActionContext,
        readings: dict[str, Any],
        *,
        prefix: str,
        timestamp: Timestamp,
    ) -> None:
        if ctx.recorder is None:
            return
        for quantity, unit in (("voltage", "V"), ("current", "A"), ("power", "W")):
            value = readings.get(quantity)
            if value is None:
                continue
            ctx.recorder.measurement(
                quantity=f"{prefix}.{quantity}",
                value=float(value),
                device_id=self.device_id,
                unit=unit,
                timestamp=timestamp,
            )

    # -- actions -----------------------------------------------------------

    @action(
        "bench.identify",
        permission=PermissionLevel.QUERY,
        params=DeviceParams,
        state_changing=False,
        description="Query *IDN? and bind the matching instrument profile.",
        timeout_s=20.0,
    )
    async def bench_identify(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        """QUERY: this opens a session and transmits ``*IDN?``.

        Nothing on the instrument changes; what changes is FieldDeck's model of
        it, and that is the point — no typed control action works until this
        has run.  An operator-pinned profile wins over the automatic match,
        because a relabelled clone answers with a name no rule here knows, but
        a pin that disagrees with the identity is reported rather than hidden.
        """
        opened = await self._transport.open()
        raw = await self._transport.query(self._identify_command())
        identity = parse_idn(raw)
        matched = match_profile(identity)
        pinned = self._declared_profile
        profile = pinned or matched or GENERIC_SCPI
        pin_conflict = pinned is not None and matched is not None and matched.key != pinned.key
        if pin_conflict and matched is not None:
            _log.warning(
                "declared profile does not match the instrument's identity; using the "
                "declared one because the operator asserted it",
                extra={
                    "device": self.device_id,
                    "declared": pinned.key if pinned else None,
                    "matched": matched.key,
                    "identity": identity.raw,
                },
            )

        self._identity = identity
        self._profile = profile
        # Framing the profile knows about beats the framing guessed from the
        # resource class; a Korad answers without terminators at all.
        await self._transport.set_terminations(
            profile.dialect.read_termination, profile.dialect.write_termination
        )
        self._transport.min_command_interval_s = profile.dialect.min_command_interval_s
        self._apply_descriptor(identity, profile)
        self._set_state(ConnectionState.READY)

        return {
            "identity": identity.raw,
            "identity_fields": identity.describe(),
            "profile": profile.key,
            "role": str(profile.role),
            "profile_detail": profile.describe(),
            "matched_automatically": matched is not None,
            "pinned_by_operator": pinned is not None,
            "pin_conflicts_with_identity": pin_conflict,
            "session_opened": opened,
            "hardware_verified": False,
            "note": (
                "the dialect for this profile is transcribed from the vendor's published "
                "programming guide and has not been verified against hardware by FieldDeck; "
                "setpoints are read back and the instrument's error queue is checked after "
                "every command"
            ),
        }

    def _identify_command(self) -> str:
        profile = self._declared_profile
        return profile.dialect.identify if profile else "*IDN?"

    def _apply_descriptor(self, identity: Identity, profile: InstrumentProfile) -> None:
        """Publish what the instrument turned out to be."""
        descriptor = self._descriptor
        descriptor.vendor = identity.vendor or descriptor.vendor
        descriptor.product = identity.model or descriptor.product
        descriptor.serial_number = identity.serial or descriptor.serial_number
        descriptor.display_name = (
            f"{identity.vendor} {identity.model}".strip() or descriptor.display_name
        )
        descriptor.roles = [profile.role]
        capabilities = []
        if profile.can_measure_supply or profile.can_measure_dmm:
            capabilities.append(DeviceCapability.MEASURE)
        if profile.can_switch_output or profile.can_switch_load:
            capabilities.extend([DeviceCapability.OUTPUT, DeviceCapability.SAFE_STATE])
        if profile.can_set_supply or profile.can_set_load:
            capabilities.append(DeviceCapability.SETPOINT)
        descriptor.capabilities = capabilities
        descriptor.metadata = {
            **descriptor.metadata,
            "identified": True,
            "identity": identity.raw,
            "profile": profile.key,
            "profile_source": "operator declaration"
            if self._declared_profile is not None
            else ("identity match" if profile is not GENERIC_SCPI else "generic fallback"),
            "hardware_verified": profile.hardware_verified,
            "supported_actions": list(profile.supported_actions()),
        }
        warnings = [self._standing_warning] if self._standing_warning else []
        if profile is GENERIC_SCPI:
            warnings.append(
                "no profile matched this identity; the generic profile can query but has no "
                "setpoint, output or load commands"
            )
        descriptor.warning = "; ".join(warnings) or None

    @action(
        "bench.status",
        permission=PermissionLevel.PASSIVE,
        params=DeviceParams,
        state_changing=False,
        description="Cached instrument state without querying the instrument.",
        allowed_during_estop=True,
    )
    async def bench_status(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        return await self.status()

    @action(
        "scpi.query",
        permission=PermissionLevel.QUERY,
        params=ScpiQueryParams,
        state_changing=False,
        description="Send a SCPI query and return the response.",
        timeout_s=15.0,
    )
    async def scpi_query(self, ctx: ActionContext, params: ScpiQueryParams) -> dict[str, Any]:
        """QUERY only.  Commands that would change state are refused here.

        An arbitrary SCPI string is classified conservatively: anything that is
        not clearly a query gets rejected rather than guessed at, because
        ``OUTP ON`` looks harmless right up until it energises something.  A
        trailing ``?`` is not enough on its own either — ``*TST?`` runs a
        self-test that operates relays.
        """
        classified = require_query(
            params.command,
            device_id=self.device_id,
            typed_actions=("psu.set", "psu.output", "psu.measure", "load.set", "load.input"),
        )
        started = Timestamp.now()
        response = await self._transport.query(classified.command)
        return {
            "command": classified.command,
            "response": response,
            "elapsed_ms": round((Timestamp.now().monotonic_ns - started.monotonic_ns) / 1e6, 3),
            "profile": self._profile.key if self._profile else None,
        }

    @action(
        "psu.status",
        permission=PermissionLevel.PASSIVE,
        params=DeviceParams,
        state_changing=False,
        description="Cached supply state without querying the instrument.",
        allowed_during_estop=True,
    )
    async def psu_status(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        profile = self._require_profile("psu.status")
        self._require_command(
            profile,
            profile.can_set_supply or profile.can_switch_output or profile.can_measure_supply,
            "power supply",
        )
        return {
            "identity": self._identity.raw if self._identity else None,
            "profile": profile.key,
            "channel": self._channel,
            "output": self._output_on,
            "setpoint_v": self._setpoints["voltage"],
            "current_limit_a": self._setpoints["current_limit"],
            "last_measurement": self._last_measurement,
            "cached": True,
            "note": (
                "cached values are what FieldDeck last commanded or measured; the "
                "instrument's live state comes from psu.measure (QUERY)"
            ),
        }

    @action(
        "psu.measure",
        permission=PermissionLevel.QUERY,
        params=ChannelParams,
        state_changing=False,
        description="Read output voltage and current from the instrument.",
        # Not allowed_during_estop: this sends SCPI, and a latched stop is
        # only ever waived for PASSIVE work. The question it answers -- what
        # is the rail actually doing -- is served during a stop by the
        # PASSIVE status action and by the SAFE_STATE_APPLIED event payload.
        timeout_s=15.0,
    )
    async def psu_measure(self, ctx: ActionContext, params: ChannelParams) -> dict[str, Any]:
        """QUERY, not PASSIVE: this sends SCPI to the instrument."""
        profile = self._require_profile("psu.measure")
        self._require_command(profile, profile.can_measure_supply, "measurement")
        channel = self._resolve_channel(params.channel, profile)
        await self._select_channel(profile, channel)
        readings = await self._measure_vip(profile, channel)
        ts = Timestamp.now()
        self._record(ctx, readings, prefix="psu", timestamp=ts)
        self._last_measurement = {**readings, "channel": channel, "utc_ns": ts.utc_ns}
        return {
            **readings,
            "channel": channel,
            "output": self._output_on,
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
        timeout_s=20.0,
    )
    async def psu_set(self, ctx: ActionContext, params: PsuSetParams) -> dict[str, Any]:
        """POWER: changing a setpoint changes what a DUT will be subjected to."""
        profile = self._require_profile("psu.set")
        self._require_command(profile, profile.can_set_supply, "setpoint")
        if params.voltage is None and params.current_limit is None:
            raise InvalidRequest(
                "psu.set needs a voltage, a current_limit, or both",
                details={"device_id": self.device_id},
                preserved="nothing was sent to the instrument",
            )
        dialect = profile.dialect
        if params.voltage is not None:
            self._require_command(profile, dialect.set_voltage is not None, "voltage setpoint")
        if params.current_limit is not None:
            self._require_command(profile, dialect.set_current is not None, "current limit")

        channel = self._resolve_channel(params.channel, profile)
        await self._select_channel(profile, channel)
        # The current limit goes first on purpose: raising the voltage while
        # the old, higher limit is still in force is how a DUT sees more
        # current than the operator asked for.
        if params.current_limit is not None and dialect.set_current is not None:
            await self._transport.write(
                _render(dialect.set_current, channel=channel, value=params.current_limit)
            )
        if params.voltage is not None and dialect.set_voltage is not None:
            await self._transport.write(
                _render(dialect.set_voltage, channel=channel, value=params.voltage)
            )

        confirmation = await self._check_error_queue(profile, "the setpoint")
        readback = await self._read_setpoints(profile, channel)
        if params.voltage is not None:
            self._setpoints["voltage"] = params.voltage
        if params.current_limit is not None:
            self._setpoints["current_limit"] = params.current_limit
        return {
            "channel": channel,
            "setpoint_v": self._setpoints["voltage"],
            "current_limit_a": self._setpoints["current_limit"],
            "output": self._output_on,
            "readback": readback,
            "readback_matches": _readback_matches(
                {"voltage": params.voltage, "current_limit": params.current_limit}, readback
            ),
            **confirmation,
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
        timeout_s=20.0,
    )
    async def psu_output(self, ctx: ActionContext, params: PsuOutputParams) -> dict[str, Any]:
        """Enabling needs POWER and takes a lease; disabling is always allowed."""
        profile = self._require_profile("psu.output")
        self._require_command(profile, profile.can_switch_output, "output enable")
        channel = self._resolve_channel(params.channel, profile)
        dialect = profile.dialect
        template = dialect.output_on if params.enabled else dialect.output_off
        assert template is not None  # guaranteed by can_switch_output
        await self._select_channel(profile, channel)
        await self._transport.write(_render(template, channel=channel))
        self._output_on = params.enabled

        confirmation = await self._confirm_switch(
            profile,
            query=dialect.query_output,
            channel=channel,
            expected=params.enabled,
            what="output",
        )
        return {
            "output": self._output_on,
            "channel": channel,
            "setpoint_v": self._setpoints["voltage"],
            "current_limit_a": self._setpoints["current_limit"],
            **confirmation,
        }

    @action(
        "dmm.measure",
        permission=PermissionLevel.QUERY,
        params=DmmMeasureParams,
        state_changing=False,
        description="Take one or more readings.",
        timeout_s=60.0,
    )
    async def dmm_measure(self, ctx: ActionContext, params: DmmMeasureParams) -> dict[str, Any]:
        """QUERY: a reading means transmitting to the meter.

        A ``MEASure`` query reconfigures the meter's own function and range
        before reading.  That changes the instrument, not the DUT, which is why
        this stays ``state_changing=False`` — but it is why a reading can take
        seconds and why switching functions mid-sequence is worth noticing.
        """
        profile = self._require_profile("dmm.measure")
        self._require_command(profile, profile.can_measure_dmm, "measurement")
        functions = profile.dialect.dmm_functions
        if params.function not in functions:
            raise InvalidRequest(
                f"unknown DMM function {params.function!r} for {profile.display_name}",
                details={"known": sorted(functions), "profile": profile.key},
                preserved="nothing was sent to the instrument",
            )
        command, unit = functions[params.function]
        readings: list[float] = []
        for _ in range(params.samples):
            ctx.raise_if_cancelled()
            value = _to_float(
                await self._transport.query(command),
                device_id=self.device_id,
                what=params.function,
            )
            if value is not None:
                readings.append(value)
        if not readings:
            raise ProtocolError(
                f"{self.device_id} returned no usable readings for {params.function}",
                details={"device_id": self.device_id, "command": command},
                preserved="the queries were sent; no reading could be parsed",
            )
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
        self._last_measurement = {
            "function": params.function,
            "value": mean,
            "unit": unit,
            "utc_ns": ts.utc_ns,
        }
        return {
            "function": params.function,
            "unit": unit,
            "value": round(mean, 9),
            "readings": readings,
            "samples": len(readings),
            "spread": round(max(readings) - min(readings), 9),
            "command": command,
            "monotonic_ns": ts.monotonic_ns,
            "utc_ns": ts.utc_ns,
        }

    @action(
        "load.measure",
        permission=PermissionLevel.QUERY,
        params=DeviceParams,
        state_changing=False,
        description="Read the load's terminal voltage, current and power.",
        # Not allowed_during_estop: this sends SCPI, and a latched stop is
        # only ever waived for PASSIVE work. The question it answers -- what
        # is the rail actually doing -- is served during a stop by the
        # PASSIVE status action and by the SAFE_STATE_APPLIED event payload.
        timeout_s=15.0,
    )
    async def load_measure(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        profile = self._require_profile("load.measure")
        self._require_command(profile, profile.can_measure_supply, "measurement")
        readings = await self._measure_vip(profile, 1)
        ts = Timestamp.now()
        self._record(ctx, readings, prefix="load", timestamp=ts)
        self._last_measurement = {**readings, "utc_ns": ts.utc_ns}
        return {
            **readings,
            "input": self._load_input_on,
            "monotonic_ns": ts.monotonic_ns,
            "utc_ns": ts.utc_ns,
        }

    @action(
        "load.set",
        permission=PermissionLevel.POWER,
        params=LoadSetParams,
        state_changing=True,
        description="Set the electronic load's regulation mode and setpoint.",
        limit_checks=(
            LimitCheck(param="current", quantity="load.current"),
            LimitCheck(param="power", quantity="load.power"),
            LimitCheck(param="resistance", quantity="load.resistance"),
        ),
        safe_state_note="Setpoints persist; the load input is disabled on safe state.",
        timeout_s=20.0,
    )
    async def load_set(self, ctx: ActionContext, params: LoadSetParams) -> dict[str, Any]:
        """POWER: a load setpoint decides how hard a DUT is pulled down.

        Only the setpoint itself can be bounded before the command is sent. A
        constant-current load dissipates the DUT's voltage times the setpoint,
        and FieldDeck does not know the DUT's voltage until it measures, so
        ``load.power`` bounds a power-mode setpoint and nothing else.
        """
        profile = self._require_profile("load.set")
        self._require_command(profile, profile.can_set_load, "load setpoint")
        modes = profile.dialect.load_modes
        if params.mode not in modes:
            raise InvalidRequest(
                f"{profile.display_name} has no {params.mode!r} mode",
                details={"known": sorted(modes), "profile": profile.key},
                preserved="nothing was sent to the instrument",
            )
        mode = modes[params.mode]
        value = {
            "current": params.current,
            "resistance": params.resistance,
            "power": params.power,
        }[params.mode]
        if value is None:
            raise InvalidRequest(
                f"load.set in {params.mode!r} mode needs a {params.mode} value",
                details={"device_id": self.device_id, "mode": params.mode},
                preserved="nothing was sent to the instrument",
            )
        if profile.dialect.select_load_mode is not None:
            await self._transport.write(
                _render(profile.dialect.select_load_mode, function=mode.function)
            )
        await self._transport.write(_render(mode.setpoint, value=value))
        confirmation = await self._check_error_queue(profile, "the load setpoint")
        self._load_setpoint = {"mode": params.mode, "value": value, "unit": mode.unit}
        return {
            "mode": params.mode,
            "value": value,
            "unit": mode.unit,
            "input": self._load_input_on,
            "limit_quantity": mode.quantity,
            **confirmation,
        }

    @action(
        "load.input",
        permission=PermissionLevel.POWER,
        params=LoadInputParams,
        state_changing=True,
        description="Enable or disable the electronic load's input.",
        permission_resolver=_output_permission,
        requires_lease=True,
        allowed_during_estop=True,
        safe_state_note="Disabling the load input is always permitted, including during ESTOP.",
        timeout_s=20.0,
    )
    async def load_input(self, ctx: ActionContext, params: LoadInputParams) -> dict[str, Any]:
        """Enabling needs POWER and takes a lease; disabling is always allowed."""
        profile = self._require_profile("load.input")
        self._require_command(profile, profile.can_switch_load, "load input")
        dialect = profile.dialect
        template = dialect.load_input_on if params.enabled else dialect.load_input_off
        assert template is not None  # guaranteed by can_switch_load
        await self._transport.write(_render(template))
        self._load_input_on = params.enabled
        confirmation = await self._confirm_switch(
            profile,
            query=dialect.query_load_input,
            channel=1,
            expected=params.enabled,
            what="load input",
        )
        return {
            "input": self._load_input_on,
            "setpoint": dict(self._load_setpoint),
            **confirmation,
        }

    async def _confirm_switch(
        self,
        profile: InstrumentProfile,
        *,
        query: str | None,
        channel: int,
        expected: bool,
        what: str,
    ) -> dict[str, Any]:
        """Check the instrument agrees about an output or load input.

        The asymmetry is deliberate.  If a *disable* did not take, the DUT is
        still connected to something live and the caller has to know: that
        raises, which also leaves the lease in place so the daemon's safe-state
        path tries again.  If an *enable* did not take, the hazard is absent,
        so it is reported as unconfirmed rather than raised — raising would
        drop the lease while the output might still be live.
        """
        error_state = await self._check_error_queue_soft(profile, what)
        if query is None:
            return {
                "confirmed": None,
                "note": (
                    f"{profile.display_name} does not report its {what} state in a form this "
                    "profile can read; the value above is what FieldDeck commanded"
                ),
                **error_state,
            }
        response = await self._transport.query(_render(query, channel=channel))
        actual = _to_bool(response)
        if actual is None:
            return {"confirmed": None, "readback": response, **error_state}
        if actual != expected:
            if not expected:
                raise ProtocolError(
                    f"{self.device_id} still reports its {what} enabled after being told to "
                    "turn it off; treat the DUT as live",
                    details={
                        "device_id": self.device_id,
                        "profile": profile.key,
                        "readback": response,
                        "what": what,
                    },
                    preserved=(
                        "the off command was sent and the instrument disagrees; the output "
                        "lease is still held so safe state will try again"
                    ),
                )
            _log.warning(
                "instrument did not confirm the commanded state",
                extra={"device": self.device_id, "what": what, "readback": response},
            )
        return {"confirmed": actual == expected, "readback": response, **error_state}

    async def _check_error_queue_soft(
        self, profile: InstrumentProfile, what: str
    ) -> dict[str, Any]:
        """Error-queue check that reports instead of raising.

        Used on the output paths, where losing the result of a command that
        was already sent is worse than returning it with a warning attached.
        """
        try:
            return await self._check_error_queue(profile, what)
        except ProtocolError as exc:
            return {"error_queue": exc.message, "accepted": False}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _render(template: str, *, channel: int = 1, value: float = 0.0, function: str = "") -> str:
    """Fill a dialect template.

    Templates come from :mod:`fielddeck.bench.profiles` and nowhere else; an
    operator declaration may pin a profile but never supply command text.
    """
    return template.format(channel=channel, value=value, function=function)


def _to_float(response: str, *, device_id: str, what: str) -> float | None:
    """Parse a numeric SCPI response, or explain what came back instead.

    Instruments answer with things like ``1.234E+00``, sometimes with a unit
    suffix, and occasionally with several comma-separated values when they are
    configured for multiple readings.  Anything that is not a number is a
    protocol error rather than a silently dropped measurement.
    """
    text = response.strip()
    if not text:
        return None
    head = text.split(",")[0].strip()
    try:
        return float(head)
    except ValueError as exc:
        raise ProtocolError(
            f"{device_id} answered {response!r} for {what}, which is not a number",
            details={"device_id": device_id, "response": response, "what": what},
            preserved="the query was sent and the instrument answered; the reply is above",
        ) from exc


def _to_bool(response: str) -> bool | None:
    """Interpret an instrument's ON/OFF answer, or give up honestly."""
    text = response.strip().upper().strip('"')
    if text in {"1", "ON", "TRUE"}:
        return True
    if text in {"0", "OFF", "FALSE"}:
        return False
    return None


def _readback_matches(
    requested: dict[str, float | None], readback: dict[str, float | None]
) -> dict[str, bool | None]:
    """Compare requested setpoints with what the instrument reports.

    Supplies quantise setpoints, so this is a tolerance check rather than an
    equality test, and a mismatch is surfaced rather than raised: the error
    queue is the authority on whether a command was accepted.
    """
    out: dict[str, bool | None] = {}
    for key, wanted in requested.items():
        actual = readback.get(key)
        if wanted is None or actual is None:
            out[key] = None
            continue
        out[key] = abs(actual - wanted) <= max(_READBACK_ABS, abs(wanted) * _READBACK_REL)
    return out
