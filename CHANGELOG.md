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

**Clients.** `fdctl` with `--json` on every command; a Textual HMI laid out for
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

Found during pre-release verification, listed because they say something about
where the sharp edges are:

- **Three path-traversal bypasses in the external-tool guard.** Firmware paths
  reach `run_tool` from recipes and from the MCP surface, and the guard skipped
  any argument beginning with `-` and only inspected relative paths that
  *began* with `../`. So `--firmware=/etc/shadow`, dfu-util's `-D/etc/shadow`
  and `sub/../../../../etc/shadow` all passed, as did a path inside openocd's
  composite `-c "program ... "` argument.
- **A QUERY action could write firmware.** Every planner in
  `fielddeck/debug/flash.py` ended with an unconditional "and anything else is
  a program" return, so an operation a given tool did not implement silently
  became a firmware write: `pyocd verify` and `dfu-util verify` built a
  *program* plan under a QUERY grant, `dfu-util reset` did under CONTROL, and
  `dfu-util erase` did under a DESTRUCTIVE confirmation naming an erase.
  Planners now refuse what they cannot do, and `DebugActions._execute` refuses
  any plan more dangerous than the permission the dispatcher granted — so the
  same bug in a future tool wrapper fails closed.
- **A safe state that failed was recorded as "safe state applied".** The
  timeline showed *"safe state applied to bench-psu"* at WARNING for a supply
  whose `safe_state()` had just timed out or raised. Failures are now CRITICAL,
  say *"treat this device as live"*, and the emergency-stop reply carries
  `all_devices_safe` and `devices_not_safed` at the top level rather than
  leaving a client to scan a list. The same change untangled `applied: False`,
  which meant both "I failed" and "a DMM has no outputs" — reading those the
  same way made the DMM look dangerous and the live supply look ordinary.
- **An in-flight action could undo a safe state that overtook it.** A slow
  `psu.output(enabled=True)` that was authorized before an emergency stop
  finished after it, and turned the rail back on. The stop reported success,
  the action reported success, and the rail was live with the stop latched.
  Lease expiry lost the same way. Devices now carry a safe-state generation
  that a handler checks across its own execution.
- **The restricted AI socket could renew a lease it did not own**, holding a
  rail up past the interval its operator set — the dead-man handle held down
  by the thing it exists to be independent of. Renewal is now refused at that
  transport, restricted to the lease's holder, and cannot lengthen the
  interval.
- **An ESTOP bypass.** `allowed_during_estop` was read off the `ActionSpec`
  rather than the resolved permission, so the flag that lets you *de-energise*
  a rail during a latched stop also let you *energise* one. It was masked by
  the default `estop_requires_ack` policy rather than prevented by design.
- **Sequential safe-state on ESTOP**, where one wedged driver delayed every
  device behind it. Now concurrent: measured 6 ms to de-energise with two
  drivers hanging indefinitely.
- **A 64 KB RPC response limit** — asyncio's `StreamReader` default — that
  killed the client's read loop on a capture of roughly 800 CAN frames.
- **Event batching with no age bound**, which lost a partial batch on `SIGKILL`.
- **`fdctl` could not find the daemon on an installed system**, because the
  install-layout probe required write access to a directory operators
  legitimately cannot write.
- **A compound-SCPI bypass**: `OUTP ON;*IDN?` classified as a query because it
  ends in a question mark. A message is a query only if every
  semicolon-separated segment is one.
- **A nested-exponent denial of service** in the recipe expression evaluator.
- **`tools.convert` answered "internal error"** on long digit strings, where
  CPython's int/str conversion limit and a float conversion both escaped
  untyped.
- **Serial ports opened without deasserting DTR/RTS first**, which reboots any
  Arduino or ESP32 you were trying to observe.
- **The simulated CAN driver kept zero-frame capture files** where the real
  driver deletes them, so a 0-byte artifact with a hash read as "we recorded
  and the bus was quiet".
- **The installer never installed tmux**, while announcing that it did. tmux is
  the top-level session manager for the whole kiosk chain and is not
  preinstalled on Raspberry Pi OS Lite, so a clean install produced a unit
  whose panel could not start.
- **No still-capture backend was installed**, so `camera.snapshot` was
  registered and could never succeed — and preflight, whose optional-tools
  section exists to say what is missing and what it costs, did not mention it.
- **The 90° and 270° touch rotation matrices were swapped** between the
  troubleshooting guide and the shipped Xorg configuration.
- **The documented emergency-stop key did not exist.** Four documents named
  F9; the HMI bound only `ctrl+e`. F9 is now bound (and is the one shown in
  the footer, because an emergency stop advertising two keys invites a
  moment's choice); `ctrl+e` still works.

### Known limitations

- **No physical hardware verification.** Every instrument profile is
  `hardware_verified: false`.
- SocketCAN has not been exercised against a real interface.
- The Raspberry Pi install path, the SPI panel and the kiosk boot have been
  dry-run and syntax-checked, not run on a Pi.
- `config/ui.example.yaml` is a reserved design sketch; no loader reads it, it
  says so at the top, and the installer deliberately does not install it.

[Unreleased]: https://github.com/saitokiku/field-deck/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/saitokiku/field-deck/releases/tag/v0.1.0
