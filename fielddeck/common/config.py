"""Operator-editable configuration.

Two files matter:

``fielddeck.yaml``
    Everything convenient — display, storage, presets, tool paths, aliases.

``safety.yaml``
    The global hard limits and arm TTL ceilings.  If this file exists and
    does not parse, ``instrumentd`` refuses to start.  A safety file that is
    silently ignored is worse than no safety file at all.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, ValidationError, field_validator

from fielddeck.common.errors import ConfigurationError
from fielddeck.common.models import PermissionLevel, SafetyLimit, StrictModel
from fielddeck.common.paths import Paths, default_paths

__all__ = [
    "FieldDeckConfig",
    "SafetyConfig",
    "compression_available_note",
    "load_config",
    "load_safety_config",
    "simulation_enabled",
]


def compression_available_note() -> str:
    """Which codec the append-only logs will actually use on this install."""
    from fielddeck.capture.storage import compression_available

    return compression_available()


def simulation_enabled() -> bool:
    """``FIELDDECK_SIM=1`` swaps every driver for its simulated counterpart."""
    return os.environ.get("FIELDDECK_SIM", "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# safety.yaml
# ---------------------------------------------------------------------------

#: Conservative ceilings that apply when the operator has not configured
#: anything.  They are deliberately low: raising a limit must be a deliberate,
#: reviewable edit, never an accident.
DEFAULT_GLOBAL_LIMITS: dict[str, dict[str, Any]] = {
    "psu.voltage": {"maximum": 30.0, "minimum": 0.0, "unit": "V"},
    "psu.current": {"maximum": 3.0, "minimum": 0.0, "unit": "A"},
    "psu.power": {"maximum": 90.0, "minimum": 0.0, "unit": "W"},
    "load.current": {"maximum": 3.0, "minimum": 0.0, "unit": "A"},
    "load.power": {"maximum": 90.0, "minimum": 0.0, "unit": "W"},
    "gpio.voltage": {"maximum": 3.3, "minimum": 0.0, "unit": "V"},
}

#: The longest an arm grant of each class may live.  Shorter is safer; the
#: operator can always re-arm.
DEFAULT_MAX_TTL_S: dict[PermissionLevel, float] = {
    PermissionLevel.QUERY: 900.0,
    PermissionLevel.CONTROL: 600.0,
    PermissionLevel.POWER: 300.0,
    PermissionLevel.FLASH: 900.0,
    PermissionLevel.DESTRUCTIVE: 120.0,
}


class SafetyConfig(StrictModel):
    """Global hard limits and authorization policy."""

    global_limits: dict[str, SafetyLimit] = Field(default_factory=dict)
    #: Per-permission TTL ceilings, in seconds.
    max_arm_ttl_s: dict[PermissionLevel, float] = Field(default_factory=dict)
    #: Default TTL used when the operator does not pass ``--ttl``.
    default_arm_ttl_s: float = 60.0
    #: Default dead-man interval for sustained outputs.
    default_lease_ttl_s: float = 30.0
    #: Permission classes this deployment refuses outright.  A shop that never
    #: wants firmware erased from the field unit sets ``["DESTRUCTIVE"]``.
    denied_permissions: list[PermissionLevel] = Field(default_factory=list)
    #: ESTOP must be acknowledged explicitly before anything can be re-armed.
    estop_requires_ack: bool = True
    #: Per-device overrides, keyed by device id or alias.
    device_limits: dict[str, dict[str, SafetyLimit]] = Field(default_factory=dict)

    @classmethod
    def defaults(cls) -> SafetyConfig:
        return cls(
            global_limits={
                name: SafetyLimit(quantity=name, **spec)
                for name, spec in DEFAULT_GLOBAL_LIMITS.items()
            },
            max_arm_ttl_s=dict(DEFAULT_MAX_TTL_S),
        )

    def limit_for(self, quantity: str, device_id: str | None = None) -> SafetyLimit | None:
        """Effective limit: the stricter of the global and per-device bounds."""
        combined = self.global_limits.get(quantity)
        if device_id:
            device = self.device_limits.get(device_id, {}).get(quantity)
            if device is not None:
                combined = device if combined is None else combined.intersect(device)
        return combined

    def max_ttl(self, permission: PermissionLevel) -> float:
        return self.max_arm_ttl_s.get(
            permission, DEFAULT_MAX_TTL_S.get(permission, self.default_arm_ttl_s)
        )


# ---------------------------------------------------------------------------
# fielddeck.yaml
# ---------------------------------------------------------------------------


class DisplayConfig(StrictModel):
    columns: int = 80
    rows: int = 25
    #: Physical panel size, used only for documentation and touch sizing hints.
    width_px: int = 480
    height_px: int = 320
    #: Minimum touch target in character cells, derived from the 90x45 px rule.
    min_touch_cols: int = 15
    min_touch_rows: int = 3
    monochrome: bool = False


class StorageConfig(StrictModel):
    sessions_dir: Path | None = None
    #: Stop accepting new captures below this much free space. Enforced at
    #: session start, at every new capture file, and warned about on a timer.
    min_free_mb: int = 256
    compress_event_log: bool = True

    # Note: there is deliberately no max_capture_file_mb here. Rolling a
    # capture mid-stream is not implemented, and a configuration key that
    # silently does nothing is worse than an absent feature — it reads as a
    # guarantee. Bound a capture with its duration or max_frames instead.


class LoggingConfig(StrictModel):
    level: str = "INFO"
    #: Structured JSON to stderr (journald-friendly) or human text.
    json_output: bool = True

    @field_validator("level")
    @classmethod
    def _valid_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log level must be one of {sorted(allowed)}")
        return upper


class SerialPreset(StrictModel):
    name: str
    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    #: ``ttl``, ``rs232`` or ``rs485`` — recorded, never inferred.  These are
    #: electrically different and FieldDeck will not treat them as equivalent.
    electrical: str = "unknown"


class CanPreset(StrictModel):
    name: str
    bitrate: int
    data_bitrate: int | None = None
    fd: bool = False
    listen_only: bool = True


class DeviceAlias(StrictModel):
    alias: str
    device_id: str
    note: str | None = None


class ToolPaths(StrictModel):
    """External binaries.  Resolved via PATH when left as bare names."""

    sigrok_cli: str = "sigrok-cli"
    openocd: str = "openocd"
    pyocd: str = "pyocd"
    esptool: str = "esptool.py"
    avrdude: str = "avrdude"
    dfu_util: str = "dfu-util"
    picotool: str = "picotool"
    ip: str = "ip"
    candump: str = "candump"
    tcpdump: str = "tcpdump"
    v4l2_ctl: str = "v4l2-ctl"


class CameraConfig(StrictModel):
    enabled: bool = True
    default_device: str | None = None
    width: int = 1280
    height: int = 720
    #: Images are stored locally and attached to the session.  Nothing is ever
    #: uploaded without an explicit operator action.
    auto_upload: bool = False

    @field_validator("auto_upload")
    @classmethod
    def _never_auto_upload(cls, value: bool) -> bool:
        if value:
            raise ValueError(
                "automatic camera upload is not supported; image analysis must be "
                "invoked explicitly by the operator"
            )
        return value


class RemoteConfig(StrictModel):
    """Optional localhost HTTP/WebSocket surface.  Off by default."""

    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = 8787

    @field_validator("bind")
    @classmethod
    def _no_wildcard(cls, value: str) -> str:
        if value in {"0.0.0.0", "::", "*"}:  # noqa: S104 - this is the rejection path
            raise ValueError(
                "refusing to bind the control API to all interfaces; bind to a "
                "specific trusted address and put it behind a VPN or SSH tunnel"
            )
        return value


class FieldDeckConfig(StrictModel):
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    tools: ToolPaths = Field(default_factory=ToolPaths)
    camera: CameraConfig = Field(default_factory=CameraConfig)
    remote: RemoteConfig = Field(default_factory=RemoteConfig)
    serial_presets: list[SerialPreset] = Field(default_factory=list)
    can_presets: list[CanPreset] = Field(default_factory=list)
    aliases: list[DeviceAlias] = Field(default_factory=list)
    #: Operator name recorded on new sessions.
    operator: str | None = None
    simulate: bool = False

    def alias_map(self) -> dict[str, str]:
        return {entry.alias: entry.device_id for entry in self.aliases}

    @classmethod
    def defaults(cls) -> FieldDeckConfig:
        return cls(
            serial_presets=[
                SerialPreset(name="115200 8N1 TTL", baudrate=115200, electrical="ttl"),
                SerialPreset(name="9600 8N1 RS485", baudrate=9600, electrical="rs485"),
                SerialPreset(
                    name="19200 8E1 Modbus RTU",
                    baudrate=19200,
                    parity="E",
                    electrical="rs485",
                ),
            ],
            can_presets=[
                CanPreset(name="125k", bitrate=125_000),
                CanPreset(name="250k", bitrate=250_000),
                CanPreset(name="500k", bitrate=500_000),
                CanPreset(name="1M", bitrate=1_000_000),
            ],
        )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"cannot read {path}: {exc}", details={"path": str(path)}) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            f"{path} is not valid YAML: {exc}", details={"path": str(path)}
        ) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigurationError(
            f"{path} must contain a YAML mapping at the top level",
            details={"path": str(path)},
        )
    return data


def load_config(paths: Paths | None = None) -> FieldDeckConfig:
    """Load ``fielddeck.yaml``, falling back to built-in defaults."""
    paths = paths or default_paths()
    if not paths.config_file.exists():
        config = FieldDeckConfig.defaults()
    else:
        raw = _read_yaml(paths.config_file)
        try:
            config = FieldDeckConfig.model_validate(raw)
        except ValidationError as exc:
            raise ConfigurationError(
                f"{paths.config_file} is invalid:\n{exc}",
                details={"path": str(paths.config_file)},
            ) from exc
    if simulation_enabled():
        config.simulate = True
    return config


def load_safety_config(paths: Paths | None = None) -> SafetyConfig:
    """Load ``safety.yaml``.

    A missing file yields the conservative built-in defaults.  A *present but
    broken* file is a hard failure — the daemon must not come up with an
    authorization policy the operator did not write.
    """
    paths = paths or default_paths()
    if not paths.safety_file.exists():
        return SafetyConfig.defaults()

    raw = _read_yaml(paths.safety_file)
    merged = SafetyConfig.defaults()
    try:
        override = SafetyConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(
            f"{paths.safety_file} is invalid; refusing to start with an "
            f"unreadable safety policy:\n{exc}",
            details={"path": str(paths.safety_file)},
        ) from exc

    # Operator limits *tighten* the built-in ceilings; a config that tries to
    # widen one has to say so explicitly by replacing the entry, which it does
    # here — but the intersect below keeps whichever bound is stricter for
    # quantities that appear in both.
    limits = dict(merged.global_limits)
    for quantity, limit in override.global_limits.items():
        limits[quantity] = limit if quantity not in limits else limits[quantity].intersect(limit)
    ttls = dict(merged.max_arm_ttl_s)
    ttls.update(override.max_arm_ttl_s)

    return SafetyConfig(
        global_limits=limits,
        max_arm_ttl_s=ttls,
        default_arm_ttl_s=override.default_arm_ttl_s,
        default_lease_ttl_s=override.default_lease_ttl_s,
        denied_permissions=override.denied_permissions,
        estop_requires_ack=override.estop_requires_ack,
        device_limits=override.device_limits,
    )
