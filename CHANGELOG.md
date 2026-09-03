# Changelog

All notable changes to FieldDeck are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project uses [semantic versioning](https://semver.org/spec/v2.0.0.html).

Two things are treated as breaking changes even though they are not API changes,
because in this project they matter more than one:

- **Widening what can happen without authorization** — an action moving to a
  less strict permission class, or a limit ceasing to be enforced.
- **Changing what happens on restart, disconnect or lease expiry.**

## [Unreleased]

### Fixed

- `scripts/field-op-desktop.sh` accepted `-y`/`--assume-yes` and documented it
  as "Do not prompt", but had no prompt to suppress: it installed a desktop
  session and about thirty packages without ever asking. It now confirms before
  it changes anything, honours `--assume-yes`, refuses rather than guessing on a
  non-interactive run without it, and still asks nothing on `--dry-run`.

## [0.1.0] — 2026-08-21

First release. Alpha, and honest about it: everything here has been verified in
simulation, and **nothing has been verified against physical hardware.**

### Added

**The daemon.** `instrumentd`, the single process permitted to open a serial
port, a CAN interface, an I²C or SPI bus, a debug probe or a USB instrument.
Boots SAFE, drives every device to a known state before the control socket
exists, and reaps expired grants and leases every 250 ms.

**The safety model.**

- Six permission classes — PASSIVE, QUERY, CONTROL, POWER, FLASH, DESTRUCTIVE —
  with **exact-class** authorization. Nothing inherits.
- Grants carry a TTL, are clamped by policy, can be scoped to a device or an
  action, are creatable only by a human-facing client, and never survive a
  restart.
- Per-call `permission_resolver`s that narrow but can never widen, so
  `psu.output(enabled=False)` is PASSIVE and works during a latched stop.
- Output leases: a dead-man's handle on any sustained hazard, released on
  expiry, client death, ESTOP or daemon exit.
- Declarative limits, including derived quantities, enforced by the dispatcher
  and **unwaivable by any authorization**.
- Emergency stop that latches, revokes every grant, surrenders every lease and
  applies safe state to every device concurrently.

**Buses and instruments.** Serial (UART/RS-232/RS-485), SocketCAN including
CAN FD, Modbus RTU and TCP, SCPI over USBTMC and LXI, I²C, SPI, GPIO, network,
sigrok logic capture, and debug probes via OpenOCD, pyOCD, esptool, avrdude,
dfu-util and picotool.

**Protocol decoding.** ISO-TP (ISO 15765-2) reassembly, UDS (ISO 14229) with a
FieldDeck permission class on each of 27 services, SAE J1939 PGN decode with
transport-protocol reassembly, and DBC decoding for CAN.

**Analysis.** Twenty CRC models with reverse lookup in both byte orders, framing
detection, COBS and SLIP, entropy, timing histograms, and protocol
identification that answers "unknown / insufficient evidence" when that is the
honest answer.

**Sessions and the unified timeline.** Append-only event log, immutable raw
artifacts with SHA-256, derived artifacts carrying provenance, dual monotonic
and UTC clocks, and the correlation query — *"what was the supply doing 300 ms
before the frames stopped?"*

**Recipes.** YAML test procedures, compiled and validated ahead of execution,
with an AST-allowlist expression evaluator, `finally` blocks that run on
failure, and no ability to arm anything.

**Clients.** `fdctl` with `--json` on every command, covering CAN, serial,
Modbus, bench/SCPI, PSU, logic, debug, firmware, flash, recipes, sessions and
the conversion toolbox; a Textual HMI laid out for
80×25 on a 480×320 panel; and an MCP server exposing 29 read-only tools, none of
which can arm.

**Simulation.** A complete simulated bench — CAN, serial, PSU, DMM, Modbus,
logic analyzer, camera — implementing the same `Driver` interfaces and
dispatched through the same pipeline as real hardware, plus a shared fault
scenario (`FIELDDECK_SIM_FAULT=1`) that produces one causal story across three
subsystems.

**Deployment.** An installer that explains itself and supports `--dry-run`, a
preflight check covering twelve areas with a remediation command for each
failure, a hardened systemd unit, udev rules, and a supervised tmux kiosk.

### Security

- Two-socket boundary: `instrumentd-ai.sock` stamps `source=claude` by the
  socket rather than by client claim, and refuses `safety.arm`,
  `safety.disarm`, `safety.estop_clear` and `safety.lease_renew` at the
  transport. `safety.lease_release` stays available: releasing ends a hazard.
- The daemon refuses to start on a live socket, so two processes can never both
  believe they own the hardware.
- Subprocesses are invoked with argument arrays; no shell execution is reachable
  through RPC or MCP.
- Credentials are redacted structurally by the logger, not by pattern-matching
  message text.
- `camera.auto_upload` is rejected by a validator rather than merely defaulting
  to false.
- The remote bind address rejects wildcards outright.

### Fixed

Nothing user-facing: 0.1.0 is the first release. The commit history covers the
pre-release audit, whose findings are worth knowing about because they say where
the sharp edges in this kind of software are — a path-traversal bypass in the
external-tool guard, a QUERY-level action that could reach a firmware write, a
lost device that kept its output lease, an in-flight action that could undo an
emergency stop that overtook it, and a lease the restricted AI socket could
renew on someone else's behalf. Each is pinned by a regression test.

### Known limitations

- **No physical hardware verification.** Every instrument profile is
  `hardware_verified: false`.
- SocketCAN has not been exercised against a real interface.
- The Raspberry Pi install path, the SPI panel and the kiosk boot have been
  dry-run and syntax-checked, not run on a Pi.
- **No typed oscilloscope or function-generator support.** The six shipped
  instrument profiles cover supplies, DMMs and electronic loads. A scope is
  reachable through raw `scpi.query` and nothing more; there is no waveform
  capture, screenshot or trigger handling.
- No typed CAN interface configuration: bringing a bus up remains an
  `ip link` command, deliberately.

[Unreleased]: https://github.com/saitokiku/field-deck/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/saitokiku/field-deck/releases/tag/v0.1.0
