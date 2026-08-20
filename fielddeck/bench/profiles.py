"""Instrument profiles: identity, role, and the SCPI dialect that follows.

**These dialects are NOT hardware-verified.**  Every command string below was
transcribed from the vendor's published programming guide.  Nobody has run
them against the physical instrument from FieldDeck, firmware revisions rename
and reorder commands, and relabelled clones (Tenma, RND, Velleman) ship
firmware that differs from the Korad original it was built from.  Treat a
profile as a hypothesis that the driver then checks: after every setpoint the
driver reads the instrument's error queue and reads the setpoint back, because
a supply that silently ignored ``:VOLT 5`` and stayed at 30 V is the failure
that damages a board.

Two rules govern how a profile is chosen, and both exist to stop a plausible
guess from becoming an energised DUT:

* **A profile is applied only after an authorised ``*IDN?`` query.**  Identity
  queries are QUERY-class work; enumeration never performs one.
* **Nothing here is keyed on a USB VID/PID.**  A vendor ships supplies, loads
  and scopes behind one vendor id, so a VID/PID lookup could hand a power
  supply's output-enable command to something that is not a power supply.

The one exception to "identify first" is an operator who *declares* a profile
for a resource in ``config/instruments``.  That is operator knowledge rather
than a software guess, and FieldDeck uses it for exactly one thing without an
identity query: sending that model's output-off command when it is driving
everything to a safe state.  Typed control actions still require ``bench.identify``.

Command templates take ``{value}`` and ``{channel}`` and carry their own
format specification, because instruments differ on what they accept: Korad
supplies want ``VSET1:12.00`` to two decimals and reject a bare ``12``.
Templates live here and only here; an operator declaration may pin a profile
but may never supply raw command text, since that would be a way to smuggle
writes past the typed actions the permission model reasons about.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fielddeck.bench.scpi import AUTO
from fielddeck.common.models import DeviceRole

__all__ = [
    "GENERIC_SCPI",
    "PROFILES",
    "Identity",
    "InstrumentProfile",
    "LoadMode",
    "ScpiDialect",
    "match_profile",
    "parse_idn",
    "profile_by_key",
]


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Identity:
    """A parsed ``*IDN?`` response."""

    raw: str
    vendor: str
    model: str
    serial: str | None = None
    firmware: str | None = None

    @property
    def haystack(self) -> str:
        """The text profile matching runs against.

        Serial number and firmware are deliberately excluded: a serial number
        containing ``DP8`` must not select the Rigol DP800 profile.
        """
        return f"{self.vendor} {self.model}".upper()

    def describe(self) -> dict[str, str | None]:
        return {
            "raw": self.raw,
            "vendor": self.vendor,
            "model": self.model,
            "serial": self.serial,
            "firmware": self.firmware,
        }


def parse_idn(raw: str) -> Identity:
    """Parse ``*IDN?`` into fields without assuming the instrument obeys 488.2.

    IEEE 488.2 says four comma-separated fields.  Real instruments disagree:
    Korad supplies answer ``KORADKA3005PV2.0`` with no commas at all, some
    return an empty or ``0`` serial, and a few append extra comma-separated
    firmware components.  Anything that does not split into four fields keeps
    the whole string as both vendor and model so substring matching still
    works and nothing is invented.
    """
    text = raw.strip().strip('"').strip()
    fields = [chunk.strip() for chunk in text.split(",")]
    vendor = model = text
    serial: str | None = None
    firmware: str | None = None
    if len(fields) >= 4:
        vendor, model, serial = fields[0], fields[1], fields[2]
        firmware = ",".join(fields[3:]).strip() or None
    elif len(fields) == 3:
        vendor, model, serial = fields[0], fields[1], fields[2]
    elif len(fields) == 2:
        vendor, model = fields[0], fields[1]
    # "0" is what several instruments return when they have no serial number
    # programmed; treating it as an identity would collide every one of them.
    if serial is not None and serial.strip().strip("0") == "":
        serial = None
    return Identity(
        raw=text,
        vendor=vendor,
        model=model,
        serial=serial or None,
        firmware=firmware,
    )


# ---------------------------------------------------------------------------
# Dialect
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoadMode:
    """One electronic-load regulation mode and the limit it is bounded by."""

    function: str
    setpoint: str
    quantity: str
    unit: str


@dataclass(frozen=True, slots=True)
class ScpiDialect:
    """The command strings one instrument family answers to.

    Every field is optional and ``None`` means "this instrument family has no
    such command as far as the programming guide says".  The driver refuses the
    corresponding action instead of substituting a command from another vendor,
    which is the entire point of keeping dialects declarative.
    """

    identify: str = "*IDN?"
    #: ``None`` for instruments with no error queue: the driver then says it
    #: could not confirm a setpoint rather than pretending it did.
    error_query: str | None = "SYST:ERR?"
    reset: str | None = "*RST"

    channels: int = 1
    #: Sent before a per-channel command on instruments that select a channel
    #: rather than naming it in each command.
    select_channel: str | None = None

    # Power supply
    set_voltage: str | None = None
    set_current: str | None = None
    query_voltage_setpoint: str | None = None
    query_current_setpoint: str | None = None
    output_on: str | None = None
    output_off: str | None = None
    query_output: str | None = None
    measure_voltage: str | None = None
    measure_current: str | None = None
    measure_power: str | None = None

    # Electronic load
    load_input_on: str | None = None
    load_input_off: str | None = None
    query_load_input: str | None = None
    select_load_mode: str | None = None
    load_modes: dict[str, LoadMode] = field(default_factory=dict)

    # Multimeter: function name -> (command, unit)
    dmm_functions: dict[str, tuple[str, str]] = field(default_factory=dict)

    #: Framing.  ``AUTO`` derives it from the VISA resource class; ``""`` means
    #: no terminator at all, which is a real answer for Korad-style supplies.
    read_termination: str = AUTO
    write_termination: str = AUTO
    #: Gap a firmware needs between commands.  Documented remedy, not a sleep
    #: standing in for a status check.
    min_command_interval_s: float = 0.0
    timeout_s: float = 5.0


@dataclass(frozen=True, slots=True)
class InstrumentProfile:
    """A model family, what it is for, and how to speak to it."""

    key: str
    display_name: str
    role: DeviceRole
    dialect: ScpiDialect
    #: Substrings matched case-insensitively against vendor + model.  A profile
    #: matches when at least one vendor substring and one model substring hit.
    vendor_match: tuple[str, ...] = ()
    model_match: tuple[str, ...] = ()
    #: False for every profile shipped here.  FieldDeck has never run these
    #: commands against the physical instrument; the flag exists so clients can
    #: say so out loud rather than implying verification nobody performed.
    hardware_verified: bool = False
    source: str = "vendor programming guide"
    notes: tuple[str, ...] = ()

    def matches(self, identity: Identity) -> bool:
        if not self.vendor_match and not self.model_match:
            return False
        haystack = identity.haystack
        if self.vendor_match and not any(token in haystack for token in self.vendor_match):
            return False
        return not self.model_match or any(token in haystack for token in self.model_match)

    # -- capability questions the driver asks -----------------------------

    @property
    def can_set_supply(self) -> bool:
        return bool(self.dialect.set_voltage or self.dialect.set_current)

    @property
    def can_switch_output(self) -> bool:
        return bool(self.dialect.output_on and self.dialect.output_off)

    @property
    def can_measure_supply(self) -> bool:
        return bool(self.dialect.measure_voltage or self.dialect.measure_current)

    @property
    def can_set_load(self) -> bool:
        return bool(self.dialect.load_modes)

    @property
    def can_switch_load(self) -> bool:
        return bool(self.dialect.load_input_on and self.dialect.load_input_off)

    @property
    def can_measure_dmm(self) -> bool:
        return bool(self.dialect.dmm_functions)

    def supported_actions(self) -> tuple[str, ...]:
        """Which typed actions this profile can actually carry out."""
        actions = ["bench.identify", "bench.status", "scpi.query"]
        if self.can_set_supply or self.can_measure_supply or self.can_switch_output:
            actions.append("psu.status")
        if self.can_measure_supply:
            actions.append("psu.measure")
        if self.can_set_supply:
            actions.append("psu.set")
        if self.can_switch_output:
            actions.append("psu.output")
        if self.can_measure_dmm:
            actions.append("dmm.measure")
        if self.can_set_load:
            actions.append("load.set")
        if self.can_switch_load:
            actions.append("load.input")
        if self.can_measure_supply and self.role is DeviceRole.LOAD:
            actions.append("load.measure")
        return tuple(actions)

    def describe(self) -> dict[str, object]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "role": str(self.role),
            "hardware_verified": self.hardware_verified,
            "source": self.source,
            "channels": self.dialect.channels,
            "supported_actions": list(self.supported_actions()),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Shipped profiles
# ---------------------------------------------------------------------------

#: Standard SCPI measurement headers.  Safe for the generic profile because a
#: MEASure query is a read; an instrument that does not implement one answers
#: with an error rather than doing something unexpected.
_STANDARD_DMM_FUNCTIONS: dict[str, tuple[str, str]] = {
    "dc_voltage": ("MEAS:VOLT:DC?", "V"),
    "ac_voltage": ("MEAS:VOLT:AC?", "V"),
    "dc_current": ("MEAS:CURR:DC?", "A"),
    "ac_current": ("MEAS:CURR:AC?", "A"),
    "resistance": ("MEAS:RES?", "ohm"),
    "frequency": ("MEAS:FREQ?", "Hz"),
}

RIGOL_DP800 = InstrumentProfile(
    key="rigol.dp800",
    display_name="Rigol DP800 series programmable supply",
    role=DeviceRole.PSU,
    vendor_match=("RIGOL",),
    model_match=("DP8", "DP7"),
    dialect=ScpiDialect(
        channels=3,
        select_channel=":INST:NSEL {channel}",
        set_voltage=":VOLT {value:.3f}",
        set_current=":CURR {value:.3f}",
        query_voltage_setpoint=":VOLT?",
        query_current_setpoint=":CURR?",
        output_on=":OUTP CH{channel},ON",
        output_off=":OUTP CH{channel},OFF",
        query_output=":OUTP? CH{channel}",
        measure_voltage=":MEAS:VOLT? CH{channel}",
        measure_current=":MEAS:CURR? CH{channel}",
        measure_power=":MEAS:POWE? CH{channel}",
        error_query=":SYST:ERR?",
    ),
    notes=(
        "setpoint commands act on the channel selected by :INST:NSEL, so the driver "
        "always selects before setting",
        "DP811/DP821 have fewer than three channels; the instrument rejects a channel "
        "it does not have rather than FieldDeck guessing the model's channel count",
    ),
)

RIGOL_DL3000 = InstrumentProfile(
    key="rigol.dl3000",
    display_name="Rigol DL3000 series electronic load",
    role=DeviceRole.LOAD,
    vendor_match=("RIGOL",),
    model_match=("DL3",),
    dialect=ScpiDialect(
        load_input_on=":SOUR:INP:STAT ON",
        load_input_off=":SOUR:INP:STAT OFF",
        query_load_input=":SOUR:INP:STAT?",
        select_load_mode=":SOUR:FUNC {function}",
        load_modes={
            "current": LoadMode("CURR", ":SOUR:CURR:LEV:IMM {value:.4f}", "load.current", "A"),
            "resistance": LoadMode(
                "RES", ":SOUR:RES:LEV:IMM {value:.3f}", "load.resistance", "ohm"
            ),
            "power": LoadMode("POW", ":SOUR:POW:LEV:IMM {value:.3f}", "load.power", "W"),
        },
        measure_voltage=":MEAS:VOLT?",
        measure_current=":MEAS:CURR?",
        measure_power=":MEAS:POW?",
        error_query=":SYST:ERR?",
    ),
    notes=(
        "a load in constant-current mode dissipates whatever the DUT's voltage times "
        "the setpoint comes to, so the power limit cannot be checked from the setpoint "
        "alone; only the current setpoint is bounded before the command is sent",
    ),
)

SIGLENT_SPD = InstrumentProfile(
    key="siglent.spd",
    display_name="Siglent SPD series programmable supply",
    role=DeviceRole.PSU,
    vendor_match=("SIGLENT",),
    model_match=("SPD",),
    dialect=ScpiDialect(
        channels=3,
        set_voltage="CH{channel}:VOLT {value:.3f}",
        set_current="CH{channel}:CURR {value:.3f}",
        output_on="OUTP CH{channel},ON",
        output_off="OUTP CH{channel},OFF",
        # SPD instruments report output state inside a packed SYST:STAT? word
        # whose bit layout differs across models. Decoding it from a guide
        # without hardware to check against would be inventing state, so the
        # driver reports the state it last commanded and says it is cached.
        query_output=None,
        measure_voltage="MEAS:VOLT? CH{channel}",
        measure_current="MEAS:CURR? CH{channel}",
        measure_power="MEAS:POWE? CH{channel}",
        error_query="SYST:ERR?",
        min_command_interval_s=0.05,
    ),
    notes=(
        "SPD3303 CH3 is a fixed-voltage rail with no programmable setpoint",
        "output state is not read back: SYST:STAT? packs it into a status word whose "
        "layout is model-specific and unverified here",
        "the LAN interface is a raw socket on port 5025 and needs newline framing",
    ),
)

SIGLENT_SDL = InstrumentProfile(
    key="siglent.sdl",
    display_name="Siglent SDL1000X electronic load",
    role=DeviceRole.LOAD,
    vendor_match=("SIGLENT",),
    model_match=("SDL",),
    dialect=ScpiDialect(
        load_input_on=":SOUR:INP:STAT ON",
        load_input_off=":SOUR:INP:STAT OFF",
        query_load_input=":SOUR:INP:STAT?",
        select_load_mode=":SOUR:FUNC {function}",
        load_modes={
            "current": LoadMode("CURR", ":SOUR:CURR:LEV:IMM {value:.4f}", "load.current", "A"),
            "resistance": LoadMode(
                "RES", ":SOUR:RES:LEV:IMM {value:.3f}", "load.resistance", "ohm"
            ),
            "power": LoadMode("POW", ":SOUR:POW:LEV:IMM {value:.3f}", "load.power", "W"),
        },
        measure_voltage="MEAS:VOLT?",
        measure_current="MEAS:CURR?",
        measure_power="MEAS:POW?",
        error_query="SYST:ERR?",
    ),
)

KEYSIGHT_34461A = InstrumentProfile(
    key="keysight.34461a",
    display_name="Keysight 34460A/34461A Truevolt multimeter",
    role=DeviceRole.DMM,
    vendor_match=("KEYSIGHT", "AGILENT", "HEWLETT"),
    model_match=("3446",),
    dialect=ScpiDialect(
        dmm_functions={
            "dc_voltage": ("MEAS:VOLT:DC?", "V"),
            "ac_voltage": ("MEAS:VOLT:AC?", "V"),
            "dc_current": ("MEAS:CURR:DC?", "A"),
            "ac_current": ("MEAS:CURR:AC?", "A"),
            "resistance": ("MEAS:RES?", "ohm"),
            "resistance_4w": ("MEAS:FRES?", "ohm"),
            "frequency": ("MEAS:FREQ?", "Hz"),
            "capacitance": ("MEAS:CAP?", "F"),
            "continuity": ("MEAS:CONT?", "ohm"),
            "diode": ("MEAS:DIOD?", "V"),
            "temperature": ("MEAS:TEMP?", "degC"),
        },
        error_query="SYST:ERR?",
        timeout_s=15.0,
    ),
    notes=(
        "MEASure reconfigures the meter's function and autoranges before reading, so a "
        "measurement can take seconds; it changes the instrument, not the DUT",
        "current ranges use separate physical terminals: selecting a current function "
        "does not move the leads, and a current reading with the leads in the volts "
        "jacks measures nothing",
    ),
)

KORAD_KAXXXXP = InstrumentProfile(
    key="korad.kaxxxxp",
    display_name="Korad KAxxxxP / Tenma 72-xxxx programmable supply",
    role=DeviceRole.PSU,
    vendor_match=("KORAD", "TENMA", "RND", "VELLEMAN"),
    model_match=("KA", "KD", "72-", "PS-", "LAB"),
    dialect=ScpiDialect(
        channels=1,
        set_voltage="VSET{channel}:{value:05.2f}",
        set_current="ISET{channel}:{value:05.3f}",
        query_voltage_setpoint="VSET{channel}?",
        query_current_setpoint="ISET{channel}?",
        output_on="OUT1",
        output_off="OUT0",
        # STATUS? answers with a single raw status byte whose bit meanings vary
        # between firmware revisions. Guessing at it would report an output
        # state FieldDeck cannot stand behind.
        query_output=None,
        measure_voltage="VOUT{channel}?",
        measure_current="IOUT{channel}?",
        measure_power=None,
        # No error queue at all: these supplies accept or drop a command in
        # silence, which is why the driver reads setpoints back.
        error_query=None,
        reset=None,
        read_termination="",
        write_termination="",
        min_command_interval_s=0.05,
        timeout_s=3.0,
    ),
    notes=(
        "this family is a USB-CDC serial device, not USBTMC: reach it as an ASRL "
        "resource declared in config/instruments, at 9600 8N1",
        "*IDN? answers without commas, e.g. KORADKA3005PV2.0",
        "commands take no terminator and the firmware drops commands sent back to "
        "back, hence the 50 ms spacing",
        "setpoints are fixed-width: VSET1:12.00, ISET1:1.000",
        "relabelled units (Tenma, RND, Velleman) share the protocol but not always the "
        "firmware quirks",
    ),
)

GENERIC_SCPI = InstrumentProfile(
    key="generic.scpi",
    display_name="Generic SCPI instrument",
    role=DeviceRole.GENERIC_SCPI,
    vendor_match=(),
    model_match=(),
    dialect=ScpiDialect(
        dmm_functions=dict(_STANDARD_DMM_FUNCTIONS),
        error_query="SYST:ERR?",
    ),
    source="IEEE 488.2 and SCPI-99 common commands",
    notes=(
        "queries only: an unrecognised instrument gets no output-enable, no setpoint "
        "and no load command, because there is no standard spelling for those and a "
        "wrong guess energises something",
        "MEASure queries are standard; an instrument that does not implement one "
        "answers with an error rather than acting",
    ),
)

#: Consulted in order; the first profile whose match rules hit wins.  Specific
#: families come before anything broad.
PROFILES: tuple[InstrumentProfile, ...] = (
    RIGOL_DP800,
    RIGOL_DL3000,
    SIGLENT_SPD,
    SIGLENT_SDL,
    KEYSIGHT_34461A,
    KORAD_KAXXXXP,
)


def profile_by_key(key: str) -> InstrumentProfile | None:
    """Look up a profile the operator pinned by name."""
    wanted = key.strip().lower()
    if wanted == GENERIC_SCPI.key:
        return GENERIC_SCPI
    for profile in PROFILES:
        if profile.key == wanted:
            return profile
    return None


def match_profile(identity: Identity) -> InstrumentProfile | None:
    """Pick the profile for an identified instrument, or ``None``.

    ``None`` means "nothing recognised it".  The caller falls back to
    :data:`GENERIC_SCPI`, which can query and nothing else — an unrecognised
    instrument never gets an output-enable command.
    """
    for profile in PROFILES:
        if profile.matches(identity):
            return profile
    return None
