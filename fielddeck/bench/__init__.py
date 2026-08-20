"""Bench instruments: programmable supplies, meters, loads and anything SCPI.

The layering here is deliberate:

``scpi``
    One VISA session at a time, with framing, timeouts, typed errors, and the
    conservative "is this string provably a query?" classifier that the generic
    command path leans on.

``profiles``
    Model families mapped to a role and a dialect, bound only after an
    authorised identity query.  The command strings are transcribed from vendor
    programming guides and are **not** hardware-verified.

``visa``
    Passive enumeration and the driver that turns all of the above into
    FieldDeck actions with real permissions attached.

Importing this package is cheap and dependency-free: pyvisa is imported inside
the functions that need it, so a machine without the ``bench`` extra still
imports FieldDeck, still enumerates whatever else is attached, and simply has
no bench drivers to offer.
"""

from __future__ import annotations

from fielddeck.bench.profiles import (
    GENERIC_SCPI,
    PROFILES,
    Identity,
    InstrumentProfile,
    ScpiDialect,
    match_profile,
    parse_idn,
    profile_by_key,
)
from fielddeck.bench.scpi import (
    ScpiClassification,
    ScpiCommandClass,
    ScpiTransport,
    classify_scpi,
    parse_resource,
    require_query,
)
from fielddeck.bench.visa import (
    BenchInstrumentDriver,
    DeclaredInstrument,
    discover_visa_drivers,
)

__all__ = [
    "GENERIC_SCPI",
    "PROFILES",
    "BenchInstrumentDriver",
    "DeclaredInstrument",
    "Identity",
    "InstrumentProfile",
    "ScpiClassification",
    "ScpiCommandClass",
    "ScpiDialect",
    "ScpiTransport",
    "classify_scpi",
    "discover_visa_drivers",
    "match_profile",
    "parse_idn",
    "parse_resource",
    "profile_by_key",
    "require_query",
]
