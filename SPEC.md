# FIELDDECK — Implementation Specification

**Document type:** Engineering build contract  
**Target:** Raspberry Pi 4, 8 GB RAM, low-resolution resistive touchscreen  
**Primary UI:** terminal-native touchscreen HMI  
**Primary OS:** Raspberry Pi OS Lite 64-bit  
**AI integration:** Claude Code as an optional client through a constrained FieldDeck MCP layer  
**Companion instruction file:** `CLAUDE.md`

---

# 0. Executive Directive to the Implementing Model

You are the lead implementation engineer for **FieldDeck**.

Your job is not to produce a prototype-looking demo. Your job is to build a maintainable, testable, hardware-safe engineering instrument that can be used manually without AI and can optionally be operated and analyzed by Claude.

Treat this specification and `CLAUDE.md` as binding product requirements.

## Leadership expectations

Work in disciplined phases.

For each phase:

1. inspect the existing repository before modifying it
2. define the exact deliverable before coding
3. implement the smallest coherent slice that meets the acceptance criteria
4. write tests while implementing, not afterward
5. run all relevant tests
6. run static/type checks
7. exercise the feature manually in simulation mode
8. fix regressions before moving on
9. keep documentation synchronized with behavior
10. make a clean git commit for the completed milestone if the repository is under git

Do not perform a giant one-pass implementation.

Do not invent hardware details that are not known.

Do not make AI a dependency of any core function.

Do not bypass the safety model because a lower-level tool makes it convenient.

Do not replace deterministic parsers with LLM interpretation when a known decoder exists.

When a design decision is not explicitly specified, choose the solution that is:

```text
SAFE
DETERMINISTIC
TESTABLE
OBSERVABLE
REVERSIBLE
LOW-RESOURCE
MAINTAINABLE
```

If two designs are functionally equal, prefer the simpler one.

---

# 1. Product Definition

FieldDeck is a compact universal engineering console built around a Raspberry Pi 4.

It combines:

- protocol inspection
- bus analysis
- embedded debugging
- test-bench control
- programmable data logging
- synchronized multi-instrument capture
- firmware inspection/programming
- conversion and binary analysis tools
- repeatable test recipes
- optional camera evidence
- optional AI analysis and control through Claude

FieldDeck is not:

- a desktop Linux replacement
- a general-purpose tablet
- a web dashboard wrapped around random shell scripts
- an AI chatbot with GPIO access
- a single-purpose CAN analyzer
- a toy oscilloscope
- an unrestricted remote-control shell

The small resistive touchscreen is the local instrument panel.

The Pi performs orchestration, decoding, storage, analysis, networking, and automation.

---

# 2. Product Principles

## 2.1 Manual-first

Everything required for normal operation must be available without Claude.

There must always be deterministic paths through:

```text
HMI
CLI
Recipes
```

Claude is a fourth client, not the product core.

## 2.2 One backend

All clients use the same authority:

```text
instrumentd
```

No client may directly manipulate DUT-facing hardware during normal operation.

## 2.3 Safe by default

Boot state is passive.

Unknown hardware is observed before it is queried.

Query before control.

Control before power.

Power before destructive programming only when explicitly required.

## 2.4 Raw evidence is sacred

Never overwrite source capture data.

Decoded data, AI conclusions, plots, reports, and annotations are derived artifacts.

## 2.5 The UI is an instrument panel

The UI should feel closer to a Fluke instrument, Rigol bench device, industrial HMI, or field-service terminal than to a smartphone or desktop application.

## 2.6 The software must survive hardware absence

The complete application must run in simulation mode on:

- a development laptop
- CI
- the Pi with no adapters connected

This is a hard requirement.

---

# 3. Hardware Target

Initial target:

```text
Raspberry Pi 4
8 GB RAM
64-bit OS
small 480x320-class resistive SPI touchscreen
USB available for adapters/instruments
Ethernet and Wi-Fi available
```

The implementation must not hard-code an exact touchscreen controller into application logic. Display/touch setup belongs in deployment configuration.

Potential external hardware includes:

- CAN / CAN-FD adapters
- isolated RS485 adapters
- RS232 adapters
- TTL UART adapters
- USB logic analyzers
- SWD/JTAG probes
- USBTMC/VISA instruments
- Ethernet SCPI instruments
- programmable PSU
- DMM
- oscilloscope
- electronic load
- function generator
- USB camera
- powered USB hub
- external SSD
- optional RTC

No DUT voltage, pinout, polarity, termination, or logic level should be assumed by the software.

---

# 4. Operating System Architecture

## 4.1 Base OS

Preferred:

```text
Raspberry Pi OS Lite 64-bit
```

DietPi may be supported later, but the initial deployment path must target Raspberry Pi OS Lite.

Do not install a conventional desktop environment.

## 4.2 Graphical/input layer

Initial local display stack:

```text
Linux
  └── minimal Xorg
       └── lightweight terminal emulator
            └── tmux
                 ├── HMI
                 ├── CLAUDE
                 ├── SHELL
                 └── LOG
```

The purpose of Xorg is only:

- reliable framebuffer/display output
- resistive touch to pointer mapping
- stable terminal mouse events

Do not install:

- desktop panel
- file manager
- compositor
- wallpaper service
- desktop notifications
- application launcher
- full desktop shell

If direct-console touch becomes reliable later, Xorg may be removed without changing the HMI.

## 4.3 Boot behavior

Normal boot sequence:

```text
kernel
  -> systemd
      -> instrumentd
      -> optional supporting services
      -> local graphical target
          -> Xorg
              -> fullscreen terminal
                  -> tmux attach/create fielddeck
```

The user should reach the FieldDeck HMI without seeing a Linux desktop.

A maintenance boot mode may remain available through SSH or a documented key combination.

---

# 5. Top-Level Runtime Architecture

```text
┌────────────────────────────────────────────────────────────┐
│                         CLIENTS                            │
│                                                            │
│  Textual HMI     fdctl CLI     Recipes      Claude MCP     │
└─────────┬─────────────┬────────────┬────────────┬──────────┘
          │             │            │            │
          └─────────────┴────────────┴────────────┘
                             │
                    local RPC over UDS
                             │
                  ┌──────────▼───────────┐
                  │     instrumentd      │
                  │  single authority    │
                  └──────────┬───────────┘
                             │
       ┌─────────────┬───────┼────────┬───────────────┐
       │             │       │        │               │
   transports    protocols  bench   debug          capture
       │             │       │        │               │
 CAN/UART/...    Modbus/... SCPI    SWD/...       sigrok/cam
       │             │       │        │               │
       └─────────────┴───────┴────────┴───────────────┘
                             │
                          HARDWARE
```

`instrumentd` is the only normal path to hardware.

---

# 6. Repository Layout

Create this structure unless an equivalent cleaner structure already exists.

```text
fielddeck/
├── CLAUDE.md
├── SPEC.md
├── README.md
├── CHANGELOG.md
├── pyproject.toml
├── .gitignore
├── config/
│   ├── fielddeck.example.yaml
│   ├── safety.example.yaml
│   ├── ui.example.yaml
│   └── instruments/
├── docs/
│   ├── architecture.md
│   ├── deployment.md
│   ├── hardware-safety.md
│   ├── protocol-support.md
│   ├── recipe-format.md
│   ├── mcp.md
│   └── troubleshooting.md
├── fielddeck/
│   ├── common/
│   ├── daemon/
│   ├── safety/
│   ├── discovery/
│   ├── drivers/
│   ├── transports/
│   ├── protocols/
│   ├── bench/
│   ├── debug/
│   ├── capture/
│   ├── analysis/
│   ├── recipes/
│   ├── cli/
│   ├── ui/
│   └── mcp/
├── recipes/
│   ├── examples/
│   └── local/
├── scripts/
├── systemd/
├── tmux/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── safety/
│   ├── ui/
│   └── fixtures/
└── sessions/
```

Do not create empty abstractions merely to match the tree. Add modules when their phase is implemented.

---

# 7. Python and Dependency Policy

Use Python for the core implementation.

Requirements:

- use the system-supported modern Python available on the target OS
- require Python >= 3.11 unless target validation indicates otherwise
- use `asyncio` for long-running I/O
- type-annotate public interfaces
- use Pydantic for RPC/config boundary validation
- use `pytest`
- use `ruff`
- use `mypy` or `pyright`
- pin deployable dependencies in a lockfile or reproducible requirements mechanism
- do not run the daemon from an unpinned global pip environment

Recommended packages by subsystem:

```text
UI:
  textual
  rich

CAN:
  python-can
  cantools
  can-isotp
  udsoncan

Serial:
  pyserial

Modbus:
  pymodbus

Bench:
  pyvisa
  pyvisa-py

USB:
  pyusb
  hidapi

Linux hardware:
  gpiod
  smbus2
  spidev

Binary:
  construct
  kaitaistruct
  bitstruct
  crccheck
  cobs
  sliplib
  pyelftools
  intelhex

Analysis:
  numpy

Camera:
  OpenCV only if needed
```

Use external system tools where they are superior:

```text
can-utils
sigrok-cli
OpenOCD
pyOCD
esptool
avrdude
dfu-util
picotool
usbutils
iproute2
ethtool
tcpdump
tshark
nmap
```

Wrap system tools through explicit adapters. Never scatter raw subprocess calls throughout the codebase.

---

# 8. Core Domain Models

Define strict core models early.

At minimum:

```text
DeviceId
DeviceDescriptor
DeviceCapability
ConnectionState
PermissionLevel
ActionDescriptor
ActionRequest
ActionResult
Event
Session
CaptureArtifact
ArmGrant
SafetyLimit
Recipe
RecipeStep
RecipeResult
```

## 8.1 Permission levels

Canonical enum:

```text
PASSIVE
QUERY
CONTROL
POWER
FLASH
DESTRUCTIVE
```

Every action must declare exactly one required permission.

## 8.2 Action metadata

Every registered action must expose:

```text
name
description
device_id
permission
arguments schema
result schema
state_changing: bool
cancelable: bool
default timeout
safe-state behavior
```

An action with hidden state-changing behavior is forbidden.

---

# 9. `instrumentd`

## 9.1 Responsibilities

`instrumentd` owns:

- hardware registry
- device discovery
- hardware connection lifecycle
- command validation
- permission checks
- safety limits
- output leases
- event distribution
- session recording
- capture ownership
- recipe execution
- emergency safe state
- audit logging

It does not own:

- Claude conversation state
- UI presentation
- shell history
- web browser functionality

## 9.2 Runtime user

Run `instrumentd` as a dedicated non-root system user, e.g. `fielddeck`.

Grant device access through:

- udev rules
- Linux groups
- explicitly documented capabilities

Do not run the daemon as root unless a specific unavoidable subsystem is isolated and justified.

## 9.3 RPC

Use a local Unix domain socket:

```text
/run/fielddeck/instrumentd.sock
```

Preferred protocol:

```text
newline-delimited JSON request/response + subscriptions
```

Requirements:

- versioned protocol
- request IDs
- structured errors
- timeout support
- cancellation support where possible
- event subscription
- schema validation
- backward-compatible evolution within a major version

Do not expose Python object serialization over RPC.

---

# 10. Event Bus

All meaningful state transitions must emit events.

Examples:

```text
DEVICE_DISCOVERED
DEVICE_LOST
DEVICE_CONNECTED
DEVICE_FAULT
ACTION_REQUESTED
ACTION_STARTED
ACTION_COMPLETED
ACTION_FAILED
ACTION_CANCELLED
ARM_GRANTED
ARM_EXPIRED
ARM_REVOKED
OUTPUT_ENABLED
OUTPUT_DISABLED
SESSION_STARTED
SESSION_STOPPED
SESSION_MARK
CAPTURE_STARTED
CAPTURE_STOPPED
ESTOP
```

Every event includes:

```text
event_id
monotonic_ns
utc_ns
session_id if present
source client
device_id if present
event type
structured payload
```

Sources must distinguish:

```text
hmi
fdctl
recipe
claude
system
```

---

# 11. Safety Architecture

Safety must be implemented server-side.

The HMI, Claude, recipes, and CLI are not trusted to enforce safety themselves.

## 11.1 Boot state

On daemon start:

```text
SAFE
no arm grants
no active output lease
no recipe running
```

Request safe-state actions on attached controllable instruments where practical.

## 11.2 Arm grants

An arm grant contains:

```text
grant_id
permission
created_at
expires_at
created_by
scope
```

Scope may be:

```text
all compatible devices
one device
one action
```

Grants:

- never survive daemon restart
- never survive reboot
- always have TTL
- can be explicitly revoked
- are shown prominently in HMI
- are logged

Claude cannot create grants. Recipes cannot create grants.

## 11.3 Output leases

Any sustained hazardous output should have a lease.

Examples:

- PSU output
- electronic load enable
- repeated CAN transmission
- GPIO drive
- actuator command

Lease includes:

```text
owner
device
expiration
safe action
```

If the controlling client dies or lease expires, `instrumentd` executes safe state where supported.

## 11.4 Limits

Support two layers:

```text
GLOBAL HARD LIMIT
DEVICE/PROFILE LIMIT
```

Effective allowed range is the stricter one.

Software must reject requests beyond limits even when armed.

## 11.5 Emergency stop

Implement:

```text
fdctl estop
```

ESTOP sequence:

1. mark system ESTOP
2. stop recipes
3. cancel active control tasks
4. disable programmable outputs where supported
5. stop active transmissions
6. preserve capture data
7. flush session metadata
8. emit high-priority audit event
9. revoke arm grants
10. remain in safe state until explicitly acknowledged

Do not delete evidence.

---

# 12. Device Driver Contract

Define an abstract driver contract with conceptual methods:

```text
probe()
connect()
disconnect()
describe()
capabilities()
status()
actions()
execute(action_request)
safe_state()
```

Streaming devices additionally provide:

```text
start_stream
stop_stream
subscribe
```

Drivers must:

- own cleanup
- define timeouts
- handle device removal
- map low-level errors to FieldDeck errors
- implement safe-state behavior if the hardware has outputs
- support a simulated counterpart where practical

---

# 13. Stable Device IDs

Do not depend on `/dev/ttyUSB0` as identity.

Use persistent evidence such as:

```text
bus type
VID
PID
USB serial
manufacturer
product
udev path
network address
instrument identity
```

Examples:

```text
serial:usb:0403:6001:A10ABC
visa:usb:0957:1798:MY12345678
can:socketcan:can0
```

If identity is unstable, mark it explicitly.

---

# 14. Discovery

Implement non-invasive inventory first.

Discovery screen/data must include:

```text
USB devices
serial ports
CAN interfaces
network interfaces
VISA resources
USBTMC instruments
cameras
debug probes
GPIO/I2C/SPI availability
```

Discovery itself must not intentionally transmit to a DUT except where the operating system necessarily enumerates the bus.

Instrument identity queries belong to QUERY, not passive discovery.

---

# 15. CAN

Use:

```text
SocketCAN
can-utils
python-can
cantools
can-isotp
udsoncan
```

Features:

- list CAN interfaces
- configure interface
- listen-only where supported
- receive and timestamp
- live arbitration-ID table
- frequency per ID
- DLC and data preview
- bus error statistics
- filters
- raw recording
- replay only when CONTROL authorized
- DBC decode
- period/jitter analysis
- ISO-TP reassembly
- UDS queries in QUERY mode

Automatic bitrate testing must use listen-only mode when supported and never transmit as part of passive detection.

Preserve raw frames even when DBC decoding is enabled.

---

# 16. Serial / UART / RS232 / RS485

Use `pyserial`.

Features:

- port list
- stable device selection
- baud
- data bits
- parity
- stop bits
- flow control
- raw byte stream
- ASCII view
- hex view
- combined hex+ASCII view
- timestamps
- line mode
- packet framing helpers
- file capture
- controlled transmit
- repeated transmit with CONTROL authorization

Electrical classes must remain distinct:

```text
TTL UART
RS232
RS485
```

Never infer electrical compatibility from protocol framing.

---

# 17. Modbus

Use `pymodbus`.

Support:

```text
Modbus RTU
Modbus TCP
```

Actions:

```text
read coils
read discrete inputs
read holding registers
read input registers
write coil
write register
write multiple
```

Permission mapping:

```text
passive observation -> PASSIVE
reads -> QUERY
writes -> CONTROL
```

Active address scanning is QUERY.

---

# 18. Bench Instruments / SCPI

Use:

```text
PyVISA
PyVISA-py
USBTMC
TCP sockets
serial SCPI
```

Implement a generic SCPI transport plus typed roles:

```text
PSU
DMM
SCOPE
ELECTRONIC_LOAD
FUNCTION_GENERATOR
GENERIC_SCPI
```

Unknown arbitrary SCPI commands must be classified conservatively.

## PSU

Core model:

```text
set voltage
set current limit
measure voltage
measure current
query output state
enable output
disable output
```

Permission:

```text
measure/query -> QUERY
setpoint changes -> POWER
output enable/disable -> POWER
```

Disable output remains permitted during ESTOP.

## DMM

Measurements are QUERY.

## Scope

Support:

- identify
- acquire
- stop/run
- waveform export if supported
- screenshot capture
- trigger state
- measurement queries

---

# 19. Logic Analyzer / Sigrok

Use `sigrok-cli` and the libsigrok ecosystem.

Features:

- enumerate supported connected devices
- configure sample rate
- configure channels
- capture
- preserve raw/native capture
- invoke protocol decoders
- collect decoder output
- link decoded events to raw time offsets

Initial decoder workflows:

```text
UART
I2C
SPI
CAN where hardware supports it
```

---

# 20. Embedded Debug / Flash

Wrap:

```text
OpenOCD
pyOCD
esptool
avrdude
dfu-util
picotool
```

Features:

- discover probes
- identify tool availability
- inspect firmware files
- hash firmware
- parse ELF metadata
- list basic sections/symbols
- read target info when supported
- flash
- verify
- reset
- mass erase only with DESTRUCTIVE authorization

Programming is FLASH. Mass erase is DESTRUCTIVE.

Each flash workflow records tool, version, probe, target, firmware path, SHA-256, command plan, timestamps, result, and verification result.

---

# 21. GPIO / I2C / SPI

Use:

```text
libgpiod
gpiod Python bindings
smbus2
spidev
i2c-tools
```

Never use deprecated direct-memory GPIO hacks.

GPIO outputs are CONTROL.

I2C scan is QUERY because it actively addresses devices.

SPI requires explicit configuration.

Show an electrical warning when opening raw GPIO/I2C/SPI tools because software cannot guarantee voltage compatibility.

---

# 22. Network Tools

Provide wrappers around:

```text
ip
ethtool
ping
arping
tcpdump
tshark
nmap
```

Classify:

```text
interface status -> PASSIVE
packet capture -> PASSIVE
ping/arping -> QUERY
active scan -> QUERY
packet injection -> CONTROL
```

Do not expose unrestricted packet injection in MVP.

---

# 23. Binary / Conversion Toolbox

Implement both HMI and CLI access.

Functions:

```text
hex <-> decimal <-> binary
ASCII / UTF-8
signed/unsigned integers
8/16/32/64-bit
little/big endian
float16/32/64
bitfield extraction
base64
CRC families
simple checksums
COBS
SLIP
timestamps
unit conversions
file hash
ELF inspection
Intel HEX inspection
raw binary slice
```

The HMI should allow manual entry and show multiple interpretations simultaneously.

---

# 24. Passive Auto-Detection Engine

The auto-detection system must be evidence-based.

## Stage A — Inventory

Discover available adapters/interfaces.

## Stage B — Passive Capture

Analyze without transmitting:

- byte frequency
- printable ASCII percentage
- packet-length candidates
- delimiter candidates
- timing/periodicity
- repeated headers
- counters
- candidate checksums/CRCs
- known Modbus framing
- known CAN behavior
- entropy

## Stage C — Classification

Return hypotheses with confidence and evidence.

Example:

```text
Possible Modbus RTU: 0.87

Evidence:
+ address bytes predominantly 0x01
+ function codes 0x03/0x06 present
+ CRC16/Modbus validates 91/100 candidate frames
- 9 candidate frames fail CRC
```

Do not return fake precision when evidence is weak.

## Stage D — Recommended Next Action

Recommend the smallest active test. Do not execute it automatically.

---

# 25. Sessions

Every meaningful engineering activity should be session-based.

```text
fdctl session start "Motor Controller Bring-Up"
```

Session stores:

```text
name
ID
start/end time
operator
notes
connected device descriptors
software versions
captures
actions
measurements
marks
errors
recipe runs
Claude observations
camera evidence
```

---

# 26. Unified Timeline

This is a flagship feature.

Every subsystem shares common time semantics.

Record:

```text
monotonic_ns
utc_ns
```

Use monotonic time for correlation and UTC for external reference.

Example:

```text
+0.000000s  PSU output enabled
+0.041183s  current = 0.412 A
+0.097414s  UART "BOOT"
+0.186028s  CAN 0x181 first seen
+1.412223s  current = 0.914 A
+1.701889s  CAN 0x181 stops
+1.709004s  UART error 0x17
+1.710118s  operator MARK
```

The architecture must support retrieving all evidence around a chosen event window.

---

# 27. Storage

Default:

```text
/var/lib/fielddeck/sessions/
```

Session structure:

```text
<session-id>/
├── session.json
├── timeline.sqlite
├── events.jsonl.zst
├── audit.jsonl.zst
├── can/
├── serial/
├── logic/
├── scope/
├── bench/
├── camera/
├── firmware/
├── reports/
└── notes/
```

Use:

- SQLite for searchable metadata
- WAL mode where appropriate
- native capture files for bulk streams
- zstd for append-only logs
- SHA-256 for artifact integrity

---

# 28. Test Recipe Engine

Recipes provide reproducible test execution.

Format: YAML.

A recipe must compile into a validated execution plan before any action runs.

Example:

```yaml
version: 1
name: controller-smoke-test

requires:
  devices:
    - role: psu
    - id: can:can0

limits:
  voltage_max: 24.5
  current_max: 1.0

steps:
  - action: session.mark
    label: power-up

  - action: psu.set
    device: role:psu
    voltage: 24.0
    current_limit: 1.0

  - action: psu.output
    device: role:psu
    enabled: true

  - action: wait
    seconds: 2

  - action: can.capture
    interface: can:can0
    duration: 5

  - assert:
      expression: "can.frames > 0"
      message: "No CAN traffic observed"

finally:
  - action: psu.output
    device: role:psu
    enabled: false
```

The compiler must calculate devices, permissions, maximum safety level, unavailable actions, and limit violations before execution.

Cleanup executes on assertion failure, timeout, cancel, device loss, and client disconnect. ESTOP supersedes recipe logic.

---

# 29. CLI — `fdctl`

Create one deterministic CLI.

Rules:

- human-readable output by default
- `--json` structured output
- meaningful exit codes
- no ANSI dependence in JSON mode
- no direct hardware access
- all commands go through `instrumentd`

Core command families:

```text
fdctl status
fdctl discover
fdctl session ...
fdctl arm ...
fdctl disarm
fdctl estop
fdctl can ...
fdctl serial ...
fdctl modbus ...
fdctl bench ...
fdctl logic ...
fdctl debug ...
fdctl firmware ...
fdctl flash ...
fdctl convert ...
fdctl crc ...
fdctl recipe ...
```

---

# 30. Terminal HMI

Use Textual.

Target approximately:

```text
80 columns x 25 rows
```

Design for 480x320-class display.

Inputs:

```text
resistive touch
keyboard
SSH keyboard
optional rotary encoder/buttons
```

The HMI must never require multitouch, hover-only behavior, or double-click.

## tmux shell

Permanent windows:

```text
[HMI] [CLAUDE] [SHELL] [LOG]
```

Provide documented keyboard shortcuts to switch between them.

## HMI chrome

Always show:

```text
safety/arm state
recording state
critical fault indicator
current session
```

If armed, show both permission class and countdown.

## Home screen

```text
┌─ FIELDDECK ─ SAFE ─ REC○ ─ 42C ─────────────────────────┐
│                                                        │
│ ┌────────────────┐ ┌────────────────┐ ┌──────────────┐ │
│ │      BUS       │ │     BENCH      │ │    LOGIC     │ │
│ │ CAN 485 UART   │ │ PSU DMM SCOPE  │ │ SPI I2C LA   │ │
│ └────────────────┘ └────────────────┘ └──────────────┘ │
│                                                        │
│ ┌────────────────┐ ┌────────────────┐ ┌──────────────┐ │
│ │     DEVICE     │ │     TOOLS      │ │  ASSISTANT   │ │
│ │ SWD FLASH USB  │ │ CRC HEX CONV   │ │ CLAUDE / ASK │ │
│ └────────────────┘ └────────────────┘ └──────────────┘ │
│                                                        │
│ CAN0 500k RX 1.2k/s | ttyUSB0 115200 | SESSION --      │
├────────────────────────────────────────────────────────┤
│ HOME      SESSION       ARM       REC       MENU        │
└────────────────────────────────────────────────────────┘
```

Exact geometry may vary to fit terminal metrics.

Target roughly 90x45 physical pixels or larger for primary touch controls.

Visual semantics:

```text
✓ good
○ idle
● active
! warning
× fault
? unknown
← RX
→ TX
```

Color only reinforces meaning.

---

# 31. Required HMI Screens

MVP:

```text
HOME
DISCOVERY
SESSION
CAN
SERIAL
TOOLS
SYSTEM
ARM/SAFETY
```

Later:

```text
MODBUS
BENCH
LOGIC
DEVICE/DEBUG
RECIPES
ASSISTANT
CAMERA
```

CAN view should show interface, bitrate, mode, RX rate, error count, ID table, frequency, DLC, last payload, recording state, and TX lock state.

Serial view should show device, framing, RX/TX, ASCII/HEX mode, timestamp, recent bytes/lines, recording, analysis, and SEND.

Bench PSU view should prioritize large voltage/current measurements and clearly show output authorization.

Tools view should expose large buttons for HEX, DEC, BIN, ASCII, FLOAT, ENDIAN, BITS, CRC, UNIT, FILE, HASH, and PACKET.

---

# 32. Physical Controls

If physical buttons are added, prefer exposing them to Linux as standard input events.

Preferred:

```text
gpio-keys device-tree overlay
```

or a small USB HID microcontroller.

Suggested mappings:

```text
UP
DOWN
LEFT
RIGHT
ENTER
BACK
ESTOP
```

Do not make the HMI depend directly on arbitrary GPIO pin numbers if Linux can present normal key events.

---

# 33. Claude Window

The `CLAUDE` tmux window runs Claude Code in the FieldDeck repository/context.

FieldDeck itself must remain functional if:

- network is unavailable
- Claude fails to authenticate
- Claude CLI crashes
- an AI service is down

---

# 34. FieldDeck MCP Server

Implement:

```text
fielddeck-mcp
```

Preferred initial transport:

```text
stdio
```

It talks only to `instrumentd`.

It must not open DUT hardware directly.

Initial read-oriented tools:

```text
fielddeck_status
fielddeck_discover
session_list
session_get
session_events
session_summary_data
can_interfaces
can_status
can_capture
can_stats
can_decode_capture
serial_devices
serial_capture
serial_analyze_capture
bench_devices
bench_status
logic_devices
logic_decode_file
firmware_inspect
convert_value
calculate_crc
permission_status
```

Later state-changing tools may include Modbus reads/writes, SCPI query, CAN transmit, serial send, PSU control, recipe run, and flash programming.

`instrumentd` must reject them unless the user already granted the corresponding authorization.

MCP can never create an arm grant.

MCP may invoke ESTOP.

---

# 35. Claude UX Integration

The HMI Assistant screen is not a full chat client.

It shows concise engineering assistance:

```text
CURRENT SESSION
Finding
Evidence
Suggested next test
```

Long conversation stays in the CLAUDE tmux window.

AI conclusions must be stored separately from raw factual evidence.

---

# 36. Camera

Support V4L2 USB cameras first.

Features:

- enumerate
- single snapshot
- save into current session
- user label
- QR/barcode decode later
- OCR later
- image metadata

Do not upload camera data automatically.

Camera analysis by Claude must be explicitly requested.

---

# 37. Remote UI

Not MVP.

Architecture must permit a later browser UI for large plots, full timelines, long CAN tables, waveform viewing, recipe editing, and reports.

Local HMI remains the primary safety/control surface.

Do not expose the control API directly to the public Internet.

---

# 38. Simulation Mode

Simulation mode is mandatory from Phase 1.

Example launch:

```text
FIELDDECK_SIM=1 fielddeck-ui
```

Simulated devices:

```text
CAN bus
serial device
Modbus device
bench PSU
DMM
logic capture
camera placeholder
```

Simulated CAN should generate several IDs, periodic traffic, jitter, and a fault scenario.

Simulated serial should generate ASCII logs, binary framed packets, known CRCs, periodic packets, and optional corruption.

Simulated PSU should support setpoint, current limit, output state, load current, and a fault scenario.

Simulation must exercise the same driver/action interfaces as real hardware. Do not create a separate fake UI data path.

---

# 39. Testing Strategy

## Unit tests

Cover:

- models
- permission comparisons
- arm expiry
- safety limits
- CRC
- framing
- recipe schema
- recipe compiler
- RPC serialization
- device ID normalization

## Mandatory safety tests

Include:

- CONTROL rejected while SAFE
- POWER rejected with CONTROL-only grant
- expired grant rejected
- Claude source cannot create grant
- recipe cannot create grant
- safety limit beats arm authorization
- ESTOP revokes grants
- ESTOP triggers safe-state calls
- output lease expiration disables output
- daemon restart starts SAFE

## Integration tests

Use simulated devices to test discovery, connect/disconnect, streaming, session capture, recipe execution, device loss, timeouts, event ordering, and timeline correlation.

## UI tests

Test home navigation, safety banner, arm countdown, dialogs, locked controls, session indicator, and target viewport accessibility.

## Hardware-in-loop

Add later under explicit test markers. CI must never require physical hardware.

---

# 40. Quality Gates

Before a milestone is complete:

```text
pytest passes
ruff passes
type checker passes
simulation smoke test passes
no known safety-test regression
docs updated
```

Do not move forward with red tests.

---

# 41. Performance Targets

Targets:

```text
HMI idle CPU: low single-digit percent where practical
instrumentd idle CPU: near-idle
HMI memory: <150 MB target
instrumentd memory: <250 MB target excluding captures
menu transition: <100 ms target
normal local RPC acknowledgement: <50 ms target
```

For high-rate captures:

- stream to files
- batch DB writes
- do not push every sample through the HMI
- rate-limit display updates
- use bounded queues

The UI generally needs no more than 10-20 refreshes per second.

---

# 42. Logging

Use structured logs.

Fields where applicable:

```text
timestamp
level
component
session
device
action
source
request_id
duration
error
```

Never log credentials, API tokens, private keys, or secrets.

Use journal integration. The tmux LOG window should follow FieldDeck service logs.

---

# 43. Error Handling

Define actionable errors.

At minimum:

```text
PermissionDenied
SafetyLimitExceeded
DeviceNotFound
DeviceDisconnected
DeviceBusy
ActionTimeout
ProtocolError
ConfigurationError
ExternalToolError
CaptureError
RecipeError
UnsupportedCapability
```

Errors should tell the operator what happened and what was preserved.

---

# 44. Configuration

Use YAML or TOML for operator-editable config.

Configuration areas:

```text
display
touch
storage
logging
global safety limits
known device aliases
instrument profiles
preferred CAN bitrates
serial presets
external tool paths
camera defaults
```

Validate config at startup.

Invalid safety configuration must fail safe, not be silently ignored.

---

# 45. Security

Requirements:

- daemon non-root
- restricted control socket permissions
- no public listening socket by default
- secrets outside git
- Claude/API credentials never stored in sessions
- explicit opt-in for remote access
- no automatic cloud upload
- reproducible dependencies
- subprocess argument arrays, never shell-concatenated commands
- validate file paths passed to external tools
- no arbitrary shell execution through RPC/MCP

---

# 46. Installation

Create an idempotent-ish `scripts/install.sh` that:

1. validates OS/architecture
2. installs required apt packages
3. creates system user/group where needed
4. installs the Python environment/application
5. installs systemd units
6. creates storage/config directories
7. installs udev rules where applicable
8. installs tmux config
9. installs kiosk startup
10. enables services
11. prints verification steps

Also provide `scripts/uninstall.sh` that does not delete session data unless explicitly requested.

---

# 47. Systemd

## `instrumentd.service`

Requirements:

```text
restart on failure
dedicated user
runtime directory
controlled device permissions
journal logging
safe shutdown behavior
```

## `fielddeck-kiosk.service`

Starts display/input layer and HMI.

If HMI crashes, restart HMI without restarting `instrumentd`.

---

# 48. tmux Session Bootstrap

Guarantee:

```text
window 1: HMI
window 2: CLAUDE
window 3: SHELL
window 4: LOG
```

Semantics:

```text
HMI:
  fielddeck-ui

CLAUDE:
  cd /opt/fielddeck && claude

SHELL:
  cd /opt/fielddeck && exec bash

LOG:
  journalctl -fu instrumentd
```

If Claude is not installed, the CLAUDE window should show a clear offline/unavailable message rather than breaking the session.

---

# 49. Documentation

README must answer:

```text
What is FieldDeck?
How do I run simulation?
How do I install on Pi?
How do I start a session?
How do I discover hardware?
How does arming work?
How do I run a recipe?
How do I add a driver?
How do I configure Claude MCP?
```

Create architecture, deployment, safety, protocol, recipe, MCP, and troubleshooting docs.

---

# 50. Implementation Phases

The order below is deliberate. Do not skip to AI or flashy features before backend contracts and safety are correct.

## PHASE 0 — Repository and Tooling

Deliver:

- repository skeleton
- pyproject
- test/lint/type configuration
- base docs
- package entry points
- simulation launcher placeholder

Acceptance: tests/lint/type tools run successfully.

## PHASE 1 — Core Daemon + Safety + RPC

Deliver:

- core models
- PermissionLevel
- arm grants
- limits
- leases
- event model
- device registry
- instrumentd
- UDS RPC
- fdctl status/arm/disarm/estop
- simulation device registry

Acceptance:

- daemon starts
- CLI connects
- unsafe action rejected
- temporary grant permits only matching permission
- expiry works
- ESTOP works
- restart returns SAFE
- safety tests pass

Do not start real bus drivers until this is solid.

## PHASE 2 — Sessions + Event Timeline

Deliver session start/stop, marks, timeline SQLite, event log, artifact registration, monotonic + UTC timestamps, and CLI.

Acceptance: simulated actions appear in timeline, captures link to sessions, ordering is preserved, sessions survive restart, hashes recorded.

## PHASE 3 — Textual HMI Skeleton

Deliver home, discovery, session, system, safety screens, status banner, simulated data, touch/mouse, keyboard navigation.

Acceptance: keyboard and mouse usability, target viewport fit, arm/session always visible, HMI restart independent from daemon.

## PHASE 4 — Serial

Deliver discovery, stable IDs, monitor, ASCII/HEX, session capture, controlled send, simulated stream, HMI, CLI.

Acceptance: bytes preserved exactly; send rejected SAFE and succeeds only with CONTROL; device loss handled.

## PHASE 5 — CAN

Deliver SocketCAN adapter, interface list/config, passive listen, live ID stats, capture, DBC, simulation, HMI, CLI.

Acceptance: raw frames preserved; UI stays responsive; TX locked by default; transmission permission tests pass.

## PHASE 6 — Tools + Passive Analysis

Deliver conversion toolbox, CRC tools, framing analysis, periodicity, evidence-based auto-detection.

Acceptance: known vectors pass; raw data immutable; confidence includes evidence.

## PHASE 7 — Modbus

Deliver RTU/TCP, reads, controlled writes, HMI, simulation.

Acceptance: reads QUERY; writes CONTROL; scanning QUERY; transactions logged.

## PHASE 8 — Bench / SCPI

Deliver VISA discovery, generic SCPI, PSU, DMM, simulation, bench UI, limits, leases.

Acceptance: query under QUERY; PSU set/output under POWER; hard limits cannot be bypassed; lease safe-state works.

## PHASE 9 — Recipe Engine

Deliver YAML schema, validation, dry-run, permission preflight, runner, assertions, timeout, cancellation, finally cleanup, HMI, CLI.

Acceptance: recipe cannot bypass safety; cleanup on failure; ESTOP interrupts; dry-run reports required resources.

## PHASE 10 — Sigrok

Deliver device discovery, capture wrapper, decoder wrapper, UART/I2C/SPI examples, artifact linkage, logic UI.

Acceptance: raw capture preserved and derived decode linked to source.

## PHASE 11 — Debug / Flash

Deliver probe discovery, firmware inspect/hash, OpenOCD, pyOCD, esptool, DFU wrappers, flash permission model.

Acceptance: inspect without target; flash requires FLASH; erase DESTRUCTIVE; commands/results audited.

## PHASE 12 — Camera

Deliver V4L2 discovery, snapshot, session attachment, camera HMI, optional barcode support.

Acceptance: local storage, hash recorded, no automatic upload.

## PHASE 13 — MCP / Claude Read-Only

Deliver stdio MCP with status, discovery, session, CAN/serial capture access, conversion/CRC, permission status.

Acceptance: MCP has no hardware handles, cannot arm, works in simulation, Claude crash does not affect FieldDeck.

## PHASE 14 — Claude Authorized Actions

Only after prior safety tests pass.

Deliver selected QUERY/CONTROL/POWER/recipe actions through MCP.

Acceptance: requests rejected without grant; scoped grant permits only correct actions; expiry enforced; Claude cannot create/extend grants; source logged as `claude`.

## PHASE 15 — Kiosk Deployment

Deliver install script, systemd, minimal Xorg, terminal, tmux, autostart, Claude/Shell/Log windows, touch calibration docs, recovery procedure.

Acceptance on Pi:

- cold boot reaches HMI
- no normal desktop
- touch works
- HMI restart independent
- daemon starts SAFE
- Claude absence does not break boot

---

# 51. Definition of MVP

MVP requires:

```text
instrumentd
safety/arming
ESTOP
sessions
unified timeline
Textual HMI
tmux shell
fdctl
simulation mode
serial monitor/send
CAN monitor/capture
conversion toolbox
basic passive analysis
Raspberry Pi kiosk deployment
```

Claude integration is intentionally not required for MVP.

---

# 52. Definition of V1

V1 adds:

```text
Modbus
SCPI PSU/DMM
recipe engine
sigrok integration
debug/flash wrappers
camera snapshots
Claude MCP read access
Claude authorized actions
polished install/deployment
documentation
```

---

# 53. Non-Goals for Initial V1

Do not spend early effort on:

- custom kernel
- custom Linux distribution
- arbitrary FPGA support
- high-bandwidth oscilloscope acquisition through Pi GPIO
- cloud account system
- enterprise RBAC
- public Internet remote control
- fancy animations
- 3D UI
- LLM-only protocol decoding
- plugin marketplace
- mobile app

---

# 54. Coding Style Expectations

Prefer:

```text
small explicit classes
data models
clear state machines
dependency injection for hardware
async context managers
structured errors
test fixtures
adapters around external tools
```

Avoid:

```text
global mutable state
god classes
shell=True
magic numbers
unbounded retries
sleep-based synchronization
hidden permission upgrades
hard-coded /dev names
UI code controlling hardware directly
business logic inside Textual callbacks
```

---

# 55. State Machines

Model major state explicitly.

Device:

```text
ABSENT
DISCOVERED
CONNECTING
READY
BUSY
FAULT
DISCONNECTING
```

Session:

```text
IDLE
ACTIVE
FINALIZING
CLOSED
```

Recipe:

```text
PENDING
PREFLIGHT
RUNNING
CANCELLING
FAILED
PASSED
ABORTED
```

Safety is grants + ESTOP state, not one loose boolean.

---

# 56. Concurrency Rules

Only one owner may perform mutually exclusive control of a device at a time.

Passive subscribers may coexist when the driver supports it.

Use per-device async locking or equivalent arbitration.

Do not let HMI, recipe, Claude, and CLI silently fight over the same instrument.

If busy, report owner and operation.

---

# 57. Data Rate / Backpressure

High-rate streams must use bounded queues.

Rules:

- capture path gets priority over HMI
- UI may drop intermediate display updates
- raw capture must not silently drop; log overflow if loss occurs
- subscribers must not block hardware readers
- batch DB writes
- expose overflow metrics

A slow Claude client must never stall CAN capture.

---

# 58. Timeouts and Cancellation

Every I/O action has a timeout.

Long captures are cancelable.

External subprocess adapters must:

- capture stdout/stderr
- use timeout
- terminate then kill if needed
- return structured results
- log executable/arguments excluding secrets

---

# 59. Artifact Provenance

Every derived artifact records:

```text
source artifact IDs
tool/decoder
tool version
configuration
creation timestamp
```

Example:

```text
decoded UART CSV
  <- logic capture
  <- sigrok UART decoder
  <- baud 115200 / 8N1
```

---

# 60. Reports

V1 should support a simple deterministic session report containing:

```text
session summary
hardware attached
test sequence
measurements
faults
captures
operator marks
recipe results
artifact hashes
```

Claude narrative must be stored separately from factual report data.

---

# 61. Development Workflow for the Implementing Model

When starting:

1. read `CLAUDE.md`
2. read `SPEC.md`
3. inspect repository tree
4. inspect tests
5. map current state to phases 0-15
6. begin with the earliest incomplete phase

For each phase:

1. write/update tests
2. implement
3. run tests
4. run lint/type checks
5. run simulation
6. inspect logs
7. update docs
8. commit cleanly

If hardware is unavailable, use simulation. Do not stall implementation waiting for hardware.

If real hardware behavior differs from assumptions, keep the public FieldDeck contract stable and modify the adapter.

---

# 62. Completion Report Format

At the end of each major implementation session, report:

```text
Implemented
- ...

Verified
- tests...
- simulation...

Known limitations
- ...

Next phase
- ...
```

Do not claim physical hardware verification when only simulation was used.

Use precise phrases such as:

```text
verified in simulation
verified on Raspberry Pi
verified on physical CAN adapter
not yet hardware-verified
```

---

# 63. Final Product Character

The finished local experience should feel like:

```text
power on
  ↓
FIELDDECK appears
  ↓
plug hardware in
  ↓
DISCOVERY notices it
  ↓
start SESSION
  ↓
listen/capture first
  ↓
decode and inspect
  ↓
manually control or run recipe
  ↓
optionally ask Claude to interpret evidence
  ↓
all activity remains synchronized and auditable
```

The best implementation is not the one with the most features.

The best implementation is the one where an engineer trusts the device enough to put it between a real DUT and a real bench supply.

Build for that level of trust.
