"""Embedded debug and programming.

Firmware inspection is offline and passive.  Talking to a target is QUERY,
resetting it is CONTROL, programming it is FLASH, and erasing it is
DESTRUCTIVE — four separate authorizations, because they are four separate
levels of consequence.
"""

from __future__ import annotations

from fielddeck.debug.firmware import inspect_firmware
from fielddeck.debug.flash import FlashPlan, build_plan, run_plan
from fielddeck.debug.probes import known_probes, programming_tools

__all__ = [
    "FlashPlan",
    "build_plan",
    "inspect_firmware",
    "known_probes",
    "programming_tools",
    "run_plan",
]
