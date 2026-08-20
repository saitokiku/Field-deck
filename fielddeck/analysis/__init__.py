"""Passive analysis: what the captured bytes mean.

Nothing in this package touches hardware.  Every module takes data that was
already recorded and reasons about it, which is why the whole subsystem is
PASSIVE and stays usable while an emergency stop is latched — understanding a
fault is exactly what an operator should be doing while the bench is safe.

The layers, bottom up:

* :mod:`~fielddeck.analysis.crc` — a parameterised CRC engine and the
  catalogue of families that show up on embedded buses, each entry carrying
  its own check value so a wrong parameter set cannot ship quietly.
* :mod:`~fielddeck.analysis.timing` — period and jitter statistics that always
  report the sample count they came from.
* :mod:`~fielddeck.analysis.convert` — the conversion toolbox: bases, widths,
  endianness, floats, bitfields, base64, COBS, SLIP, timestamps, units,
  hashes, Intel HEX and ELF.  Pure functions over bytes, no I/O.
* :mod:`~fielddeck.analysis.framing` — structure discovery: entropy,
  delimiters, frame lengths, preambles, counters and checksum fields.
* :mod:`~fielddeck.analysis.autodetect` — Stage C of auto-detect: named
  hypotheses with supporting *and* contradicting evidence, a confidence that
  small samples cap, and the smallest active test that would settle the
  question.  It never runs that test; transmitting is an authorization
  decision for a human with an arm grant.

``actions`` is imported by the daemon rather than from here, so importing this
package stays cheap for the CLI and the HMI.
"""

from __future__ import annotations

from fielddeck.analysis import autodetect, convert, crc, framing, timing
from fielddeck.analysis.autodetect import Hypothesis, classify, identify
from fielddeck.analysis.convert import (
    Reading,
    base64_decode,
    base64_encode,
    bitfield,
    bytes_to_floats,
    bytes_to_ints,
    cobs_decode,
    cobs_encode,
    convert_unit,
    epoch_to_iso,
    hash_bytes,
    hexdump,
    inspect_elf,
    interpret,
    iso_to_epoch,
    parse_hex_bytes,
    parse_intel_hex,
    parse_number,
    slip_decode,
    slip_encode,
)
from fielddeck.analysis.crc import CATALOGUE, CrcModel, crc_candidates, list_models
from fielddeck.analysis.framing import (
    Segmentation,
    analyze,
    checksum_candidates,
    detect_modbus_rtu,
    printable_ratio,
    shannon_entropy,
)
from fielddeck.analysis.timing import classify_periodicity, summarize_periods

__all__ = [
    "CATALOGUE",
    "CrcModel",
    "Hypothesis",
    "Reading",
    "Segmentation",
    "analyze",
    "autodetect",
    "base64_decode",
    "base64_encode",
    "bitfield",
    "bytes_to_floats",
    "bytes_to_ints",
    "checksum_candidates",
    "classify",
    "classify_periodicity",
    "cobs_decode",
    "cobs_encode",
    "convert",
    "convert_unit",
    "crc",
    "crc_candidates",
    "detect_modbus_rtu",
    "epoch_to_iso",
    "framing",
    "hash_bytes",
    "hexdump",
    "identify",
    "inspect_elf",
    "interpret",
    "iso_to_epoch",
    "list_models",
    "parse_hex_bytes",
    "parse_intel_hex",
    "parse_number",
    "printable_ratio",
    "shannon_entropy",
    "slip_decode",
    "slip_encode",
    "summarize_periods",
    "timing",
]
