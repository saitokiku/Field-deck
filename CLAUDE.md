# FIELDDECK — Claude Operating Contract

> Project instruction file for Claude Code.
> Place this file at the root of the FieldDeck repository as `CLAUDE.md`.

## 0. Mission

FieldDeck is a Raspberry Pi 4 based universal engineering HMI, field console, protocol analyzer, bench controller, data logger, programmer/debugger, and AI-assisted test fixture.

The device has a small low-resolution resistive touchscreen. The local UI is intentionally terminal-native and touch-friendly. Do not redesign FieldDeck as a desktop application, web-first tablet, or conventional Linux GUI.

FieldDeck must remain useful with Claude completely unavailable. Claude is an optional assistant layered on top of a deterministic manual interface.

The system has four equal clients:

1. **HMI** — the local touchscreen interface.
2. **CLI** — `fdctl`, for deterministic manual control.
3. **Recipes** — repeatable scripted tests.
4. **Claude** — an AI client using the FieldDeck MCP/API surface.

All four clients MUST use the same backend. Never implement special hardware access only for Claude or only for the GUI.

---

# 1. Core Architecture

```text
                      ┌──────────────────────────┐
                      │      FIELDDECK HMI       │
                      │  Textual / terminal UI   │
                      └────────────┬─────────────┘
                                   │
                      ┌────────────┴─────────────┐
                      │          fdctl           │
                      │     deterministic CLI    │
                      └────────────┬─────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
      ┌───────▼────────┐   ┌───────▼────────┐   ┌──────▼───────┐
      │ Test Recipes   │   │ FieldDeck MCP  │   │ Remote API   │
      │ YAML / Python  │   │ Claude tools   │   │ optional     │
      └───────┬────────┘   └───────┬────────┘   └──────┬───────┘
              │                    │                    │
              └────────────────────┼────────────────────┘
                                   │
                         ┌─────────▼─────────┐
                         │    instrumentd    │
                         │ single authority  │
                         │ for all hardware  │
                         └─────────┬─────────┘
                                   │
     ┌─────────────┬───────────────┼───────────────┬──────────────┐
     │             │               │               │              │
    CAN        SERIAL/RS485      BENCH          DEBUG          LOGIC
 SocketCAN      UART/Modbus     SCPI/VISA     SWD/JTAG       sigrok
     │             │               │               │              │
     └─────────────┴───────────────┼───────────────┴──────────────┘
                                   │
                                HARDWARE
```

`instrumentd` is the single source of truth.

No client should directly manipulate `/dev/tty*`, `/dev/spidev*`, `/dev/i2c-*`, `/sys/class/gpio`, CAN interfaces, USB instruments, programmable power supplies, or debug probes except through an explicitly approved low-level diagnostic workflow.

---

# 2. Safety Contract — Non-Negotiable

## 2.1 Default state

FieldDeck always starts in:

```text
SAFE / PASSIVE
```

PASSIVE means:

- receive traffic
- enumerate hardware
- inspect configuration
- read cached data
- capture camera images locally
- parse files
- decode traffic
- inspect logs
- calculate conversions
- analyze previously captured data

PASSIVE does **not** permit transmissions to a DUT.

## 2.2 Permission levels

Every backend operation declares one of these levels:

| Level | Meaning | Examples |
|---|---|---|
| `PASSIVE` | No signal transmitted to DUT | CAN listen, serial listen, USB enumerate |
| `QUERY` | Known read/query operation | SCPI `*IDN?`, Modbus read registers |
| `CONTROL` | Changes DUT/instrument state | CAN transmit, Modbus write, GPIO output |
| `POWER` | Changes electrical power conditions | PSU voltage/current/output, electronic load |
| `FLASH` | Alters firmware/nonvolatile state | OpenOCD flash, DFU, esptool |
| `DESTRUCTIVE` | Reset/erase/high-risk operation | mass erase, unbounded power/output operations |

Claude may inspect and suggest operations at all levels.

Claude must NEVER promote its own permission level.

The user must authorize `QUERY`, `CONTROL`, `POWER`, `FLASH`, or `DESTRUCTIVE` actions through the normal FieldDeck authorization mechanism.

## 2.3 Arming

Arming should be explicit and temporary.

Recommended model:

```text
fdctl arm control --ttl 60
fdctl arm power   --ttl 30
fdctl arm flash   --ttl 120
```

An arm grant:

- has a permission class
- has an expiration time
- is bound to the current user/session
- is visible in the HMI
- is logged
- can be cancelled immediately
- does not survive reboot

If a physical ARM switch is later added, software authorization MUST still be required for `POWER`, `FLASH`, and `DESTRUCTIVE`.

## 2.4 Emergency stop

`instrumentd` must expose a single emergency-safe-state function.

Emergency stop should:

1. disable programmable outputs where supported
2. disable electronic loads where supported
3. stop automated recipes
4. stop active transmissions where possible
5. preserve captured data
6. write an immutable event to the session log
7. return the UI to SAFE

Never delete logs during emergency stop.

## 2.5 Claude-specific hard rules

Claude MUST NOT:

- use `sudo` to bypass FieldDeck permissions
- write directly to hardware device files
- silently change CAN bitrate
- silently transmit CAN frames
- silently probe Modbus addresses
- change a PSU voltage/current/output without an active POWER grant
- flash a device without an active FLASH grant
- execute `--dangerously-skip-permissions`
- erase firmware unless the user explicitly requests erase behavior
- infer that a target is safe to energize
- assume voltage levels
- assume pinouts
- assume RS232 is TTL UART
- assume RS485 polarity
- assume a CAN termination topology
- overwrite original capture data
- rewrite raw observations to match a theory

When uncertain, capture first and reason second.

---

# 3. Operating System Philosophy

Preferred base:

```text
Raspberry Pi OS Lite 64-bit
```

DietPi 64-bit is acceptable if the exact touchscreen and touch controller work reliably.

Do not install a conventional desktop environment unless required by a hardware driver.

Preferred local graphics stack:

```text
Linux
  └── minimal Xorg
       └── fullscreen terminal emulator
            └── tmux
                 ├── HMI
                 ├── CLAUDE
                 ├── SHELL
                 └── LOG
```

The purpose of Xorg is only:

- display the terminal
- map the resistive touchscreen to pointer events
- provide stable input handling

No panel.
No file manager.
No wallpaper.
No notification daemon.
No compositor.
No desktop shell.

If the touchscreen works reliably from the Linux console with mouse/touch events, Xorg may later be removed.

## 3.1 The one sanctioned exception: the Field-Op desktop

Everything above governs **the bench unit** — the Pi wired to a DUT, driven from
a 3.5" panel. It is not a rule about the hardware, it is a rule about that role.

The same Pi is also carried back to a desk, where a monitor, keyboard and mouse
get plugged in and the operator wants to read a datasheet, open a capture in
Wireshark, and write up what happened. `scripts/field-op-desktop.sh` installs an
Openbox desktop for exactly that, and it does have a panel, a file manager and a
wallpaper. See [docs/field-op-desktop.md](docs/field-op-desktop.md).

This is a deliberate, owner-approved exception, not drift. Do not remove it as a
§3 violation. The constraints that keep it from becoming one:

- It is **never** installed by `scripts/install.sh`, and is never a dependency of
  `instrumentd`, the HMI, `fdctl`, recipes or the MCP server. A bench unit that
  never runs it must be fully functional, and is.
- It is a **session you choose at the greeter**, alongside the existing entries,
  not a replacement for any of them and not something boot pulls in.
- It installs no hardware access of its own. Its FieldDeck menu entries shell out
  to `fdctl`, `fielddeck-ui` and `fielddeck-session` like any other client, and
  are bound by the same authorization model. Nothing in it touches `/dev/*`.
- The kiosk path (§3's Xorg → terminal → tmux → HMI) is untouched and remains
  what a panel-equipped unit boots into.

If a future change would make the daemon, the panel or the CLI depend on this
desktop being present, that change is wrong — fix the change, not this file.

---

# 4. Local UI Contract

The HMI must work at approximately:

```text
80 columns × 25 lines
```

Target font class:

```text
~6×12 pixel monospace
```

On a roughly 480×320 display this leaves enough physical area for touchable terminal widgets.

## 4.1 Permanent navigation

Use `tmux` as the top-level session manager.

Recommended windows:

```text
[ HMI ] [ CLAUDE ] [ SHELL ] [ LOG ]
```

Keep a one-line tmux status bar permanently visible.

Touching/clicking a window name should switch views.

Suggested processes:

```text
HMI     -> fielddeck-ui
CLAUDE  -> claude
SHELL   -> bash
LOG     -> journalctl -fu instrumentd
```

The user must always be able to leave Claude and return to the deterministic HMI.

## 4.2 Home screen

Design around large blocks, not lists of tiny buttons.

Example:

```text
┌─ FIELDDECK ─ SAFE ─ REC○ ─ 42C ─ USB:4 ─ ETH✓ ───────┐
│                                                       │
│ ┌────────────────┐ ┌────────────────┐ ┌─────────────┐ │
│ │      BUS       │ │     BENCH      │ │    LOGIC    │ │
│ │ CAN 485 UART   │ │ PSU DMM SCOPE  │ │ SPI I2C LA  │ │
│ └────────────────┘ └────────────────┘ └─────────────┘ │
│                                                       │
│ ┌────────────────┐ ┌────────────────┐ ┌─────────────┐ │
│ │     DEVICE     │ │     TOOLS      │ │  ASSISTANT  │ │
│ │ SWD FLASH USB  │ │ CRC HEX CONV   │ │ CLAUDE/ASK  │ │
│ └────────────────┘ └────────────────┘ └─────────────┘ │
│                                                       │
│ CAN0:500k RX 1.2k/s     ttyUSB0:115200     SESSION:-- │
├───────────────────────────────────────────────────────┤
│ HOME    SESSION    ARM    REC    MENU                  │
└───────────────────────────────────────────────────────┘
```

UI priorities:

1. current safety state
2. active output/power state
3. active recording state
4. selected interface
5. live error/fault state
6. primary measurements
7. secondary details

Do not waste screen area on decorative animation.

## 4.3 Color is secondary

The UI must remain understandable in monochrome.

Use text and glyphs first:

```text
✓ online
○ idle
● active
! warning
× fault
? unknown
→ TX
← RX
```

Color may reinforce states but never carry meaning by itself.

## 4.4 Touch rules

- minimum touch target: about 90×45 physical pixels
- no tiny checkboxes
- no hover-only controls
- destructive actions require a second deliberate action
- scrolling lists should also support keyboard/encoder navigation
- numeric settings should have `-`, value, `+`, and direct-entry options
- never require a double-click

---

# 5. Backend: `instrumentd`

Implement `instrumentd` in Python unless a specific performance constraint proves otherwise.

Python is preferred because the hardware ecosystem is broad and integration speed matters more than microsecond-level application latency.

Use:

- Python 3
- `asyncio`
- Pydantic models
- Unix domain socket for local RPC
- optional localhost HTTP/WebSocket API
- systemd service
- structured JSON logs
- SQLite for metadata/indexes
- original/native capture formats for bulk data

Suggested source tree:

```text
fielddeck/
├── CLAUDE.md
├── pyproject.toml
├── config/
│   ├── fielddeck.yaml
│   ├── limits.yaml
│   └── instruments/
├── fielddeck/
│   ├── daemon/
│   │   ├── service.py
│   │   ├── rpc.py
│   │   ├── events.py
│   │   └── state.py
│   ├── safety/
│   │   ├── permissions.py
│   │   ├── arm.py
│   │   └── estop.py
│   ├── discovery/
│   │   ├── usb.py
│   │   ├── serial.py
│   │   ├── network.py
│   │   └── instruments.py
│   ├── transports/
│   │   ├── can.py
│   │   ├── serial.py
│   │   ├── rs485.py
│   │   ├── ethernet.py
│   │   ├── gpio.py
│   │   ├── i2c.py
│   │   ├── spi.py
│   │   └── usb.py
│   ├── protocols/
│   │   ├── modbus.py
│   │   ├── scpi.py
│   │   ├── uds.py
│   │   ├── j1939.py
│   │   ├── ascii.py
│   │   └── binary.py
│   ├── bench/
│   │   ├── visa.py
│   │   ├── psu.py
│   │   ├── dmm.py
│   │   ├── scope.py
│   │   └── load.py
│   ├── debug/
│   │   ├── openocd.py
│   │   ├── pyocd.py
│   │   ├── esptool.py
│   │   ├── avrdude.py
│   │   └── dfu.py
│   ├── capture/
│   │   ├── timeline.py
│   │   ├── sigrok.py
│   │   ├── camera.py
│   │   └── recorder.py
│   ├── recipes/
│   │   ├── schema.py
│   │   ├── runner.py
│   │   └── assertions.py
│   ├── cli/
│   │   └── fdctl.py
│   ├── ui/
│   │   ├── app.py
│   │   ├── screens/
│   │   └── widgets/
│   └── mcp/
│       └── server.py
├── recipes/
├── sessions/
├── tests/
└── scripts/
```

---

# 6. Driver Interface

All hardware drivers should expose a common conceptual contract.

```text
probe()
connect()
disconnect()
capabilities()
status()
read()
query()
write()
stream()
safe_state()
```

Not every device implements every method.

Each action must expose metadata including:

```text
action name
device ID
permission level
arguments
timeout
expected response type
whether action changes state
whether action can be cancelled
```

Never hide state-changing behavior inside a function named `read`, `status`, or `discover`.

---

# 7. Unified Device Model

Each discovered interface/device should have a stable runtime object.

Example conceptual record:

```json
{
  "id": "serial:usb-FTDI_FT232R_A10ABC",
  "transport": "serial",
  "path": "/dev/ttyUSB0",
  "vendor": "FTDI",
  "product": "FT232R",
  "serial": "A10ABC",
  "capabilities": ["rx", "tx", "baud_config"],
  "permission_floor": "PASSIVE",
  "metadata": {}
}
```

Prefer persistent identifiers from udev/vendor/product/serial information.

Do not make user profiles depend only on unstable names such as `/dev/ttyUSB0`.

---

# 8. Protocol and Tool Stack

FieldDeck should use mature Linux tools and libraries rather than reimplementing everything.

## CAN / CAN FD

Primary:

```text
SocketCAN
can-utils
python-can
cantools
```

Recommended additions:

```text
can-isotp
udsoncan
J1939 support where needed
DBC import/export
```

Capabilities:

- listen
- log
- replay
- filter
- decode DBC
- statistics by arbitration ID
- period/jitter analysis
- signal extraction
- ISO-TP reassembly
- UDS diagnostics
- optional controlled transmit

CAN autodetection must begin PASSIVELY.

When testing bitrates, use listen-only mode where supported.

Never transmit merely to detect a bitrate.

## UART / RS232 / RS485

Primary:

```text
pyserial
```

Provide:

- arbitrary baud
- parity
- stop bits
- flow control
- raw hex view
- ASCII view
- timestamped view
- line mode
- packet mode
- file capture
- playback
- RFC2217 support where useful

RS232 and TTL UART are electrically different. Never treat them as interchangeable.

## Modbus

Primary:

```text
pymodbus
```

Support:

```text
Modbus RTU
Modbus TCP
```

Passive serial observation is PASSIVE.

Active address scanning is QUERY and requires authorization.

Writes are CONTROL.

## I2C / SPI / GPIO

Use modern Linux interfaces.

Preferred tools/libraries:

```text
libgpiod
gpiod Python bindings
smbus2
spidev
i2c-tools
```

GPIO outputs are CONTROL.

Do not assume a target signal is 3.3 V tolerant.

## Logic analyzer / protocol decoding

Primary:

```text
sigrok-cli
libsigrok
libsigrokdecode
```

Use sigrok's existing drivers and protocol decoders whenever possible.

Preserve the original capture.

Derived decoded results must link back to the raw capture.

## Bench instruments

Primary abstraction:

```text
SCPI
VISA
USBTMC
TCP sockets
serial SCPI
```

Preferred Python:

```text
PyVISA
PyVISA-py
```

Support:

- programmable PSU
- DMM
- oscilloscope
- function generator
- electronic load
- frequency counter
- spectrum analyzer where supported
- temperature/data acquisition instruments

Always query instrument identity when authorized before applying a device-specific profile.

## Debug / programming

Support wrappers around:

```text
OpenOCD
pyOCD
esptool
avrdude
dfu-util
picotool
```

Firmware reading may be PASSIVE/QUERY depending on target.

Firmware programming is FLASH.

Mass erase is DESTRUCTIVE.

## USB

Recommended:

```text
usbutils
PyUSB
hidapi
```

Support:

- USB enumeration
- VID/PID lookup
- endpoint/interface inspection
- HID reports
- USB serial discovery

## Networking

Use:

```text
iproute2
ethtool
ping
arping
tcpdump
tshark
nmap
scapy
```

Packet capture is PASSIVE.

Active scanning/probing is QUERY.

## Binary/protocol toolbox

Recommended:

```text
construct
kaitaistruct
bitstruct
crccheck
cobs
sliplib
pyelftools
intelhex
```

Useful built-in conversion tools:

```text
HEX <-> DEC <-> BIN
ASCII / UTF-8
signed / unsigned
int8/16/32/64
float16/32/64
little/big endian
bit fields
CRC families
checksums
COBS
SLIP
base64
timestamps
units
```

This toolbox should be available from both the HMI and `fdctl`.

---

# 9. Auto-Detect Engine

Auto-detection must be staged.

## Stage 1 — Inventory

Always safe:

- enumerate USB
- enumerate serial ports
- enumerate CAN interfaces
- enumerate network interfaces
- enumerate cameras
- enumerate debug probes
- enumerate VISA/USBTMC instruments
- enumerate GPIO/I2C/SPI availability

## Stage 2 — Passive observation

Examples:

- listen for CAN traffic
- capture UART bytes without transmitting
- observe serial timing
- identify printable ASCII
- look for known packet framing
- test candidate CRCs against captured packets
- identify repeated IDs/periods
- classify probable protocol

Output confidence, never certainty without evidence.

Example:

```text
LIKELY PROTOCOL
Modbus RTU               86%

Evidence:
✓ repeating address byte 0x01
✓ valid CRC16 on 91/100 frames
✓ function codes 0x03 and 0x06 observed

[OPEN DECODER] [KEEP RAW]
```

## Stage 3 — Non-destructive query

Requires QUERY authorization.

Examples:

- SCPI `*IDN?`
- Modbus register reads
- controlled serial identity query
- network service interrogation

## Stage 4 — Control

Requires CONTROL/POWER/FLASH authorization.

Never jump directly from inventory to control.

---

# 10. Sessions and Unified Timeline

Everything important belongs to a session.

Example:

```text
session:
  id: 2026-08-20_motor-controller-01
  name: Motor Controller Bring-Up
  started_utc: ...
  notes: ...
```

A session may contain:

```text
CAN capture
serial capture
Modbus transactions
logic analyzer traces
scope screenshots/waveforms
PSU voltage/current
DMM measurements
camera snapshots
firmware hashes
terminal commands
Claude observations
user annotations
faults
test results
```

Every event should include both:

```text
monotonic timestamp
UTC timestamp
```

Use monotonic time for correlation.

Use UTC for human/log reference.

Never alter original timestamps after capture.

---

# 11. Storage Model

Recommended:

```text
sessions/<session-id>/
├── session.json
├── timeline.sqlite
├── events.jsonl.zst
├── can/
├── serial/
├── logic/
├── scope/
├── camera/
├── firmware/
├── reports/
└── notes/
```

Use SQLite for searchable metadata.

Use native/raw formats for high-volume captures.

Compress archival logs with zstd.

Raw data is immutable.

Analysis artifacts are derived data.

---

# 12. Test Recipes

FieldDeck should support repeatable, human-readable recipes.

Example conceptual recipe:

```yaml
name: controller-smoke-test

requirements:
  - bench_psu
  - can0

limits:
  max_voltage: 24.5
  max_current: 2.0

steps:
  - action: session.start
    name: controller-smoke-test

  - action: psu.set
    device: bench_psu
    voltage: 24.0
    current_limit: 1.0

  - action: psu.output
    device: bench_psu
    value: true

  - action: wait
    seconds: 2

  - action: can.capture
    interface: can0
    duration: 5

  - action: assert
    expression: can.frames > 0

finally:
  - action: psu.output
    device: bench_psu
    value: false
```

Recipe execution must validate all permission and limit requirements before starting.

`finally`/safe-state actions should run even when a step fails.

---

# 13. Manual CLI Contract

The deterministic CLI is named:

```text
fdctl
```

Target interaction examples:

```text
fdctl status
fdctl discover
fdctl session start "bench bringup"
fdctl session stop

fdctl can interfaces
fdctl can listen can0 --bitrate 500000
fdctl can stats can0
fdctl can decode capture.log --dbc vehicle.dbc

fdctl serial list
fdctl serial monitor serial:FTDI_A10ABC --baud 115200
fdctl serial send serial:FTDI_A10ABC --hex "01 03 00 00 00 02"

fdctl modbus read --device rs485:1 --slave 1 --holding 0 --count 8

fdctl bench list
fdctl scpi query bench:psu "*IDN?"
fdctl psu status bench:psu

fdctl logic devices
fdctl logic capture --device fx2lafw --seconds 2

fdctl debug probes
fdctl flash inspect firmware.bin

fdctl convert hex 0xDEADBEEF
fdctl crc crc16-modbus capture.bin

fdctl arm query --ttl 60
fdctl arm control --ttl 60
fdctl arm power --ttl 30
fdctl disarm
fdctl estop
```

These are interface goals, not permission bypasses.

All CLI actions still go through `instrumentd`.

---

# 14. Claude Integration

Claude is a client, not the backend.

Preferred integration:

```text
Claude Code
   │
   └── FieldDeck MCP server
          │
          └── instrumentd
```

Claude should primarily receive structured tools such as:

```text
fielddeck_status
fielddeck_discover
session_list
session_summary
session_get_events

can_status
can_listen
can_capture
can_analyze
can_decode

serial_list
serial_capture
serial_analyze

modbus_read

bench_list
bench_status
scpi_query

logic_capture
logic_decode

camera_snapshot
camera_list

firmware_inspect

convert_value
calculate_crc

recipe_validate
recipe_run

permission_status
```

State-changing MCP tools may exist, but `instrumentd` must reject them unless the user has already granted the corresponding temporary authorization.

Claude must never be able to create its own authorization grant.

---

# 15. How Claude Should Reason About Unknown Hardware

When asked to identify or debug an unknown device:

1. inventory interfaces
2. read the current FieldDeck safety state
3. inspect existing session information
4. capture passively
5. preserve raw bytes
6. identify obvious framing/timing
7. try known parsers
8. calculate protocol confidence
9. present evidence
10. recommend the smallest next query
11. wait for authorization if transmission is required
12. record conclusions as hypotheses until verified

Preferred language:

```text
Observed:
- ...

Likely:
- ...

Unknown:
- ...

Safest next test:
- ...
```

Do not present guessed pinouts, voltages, baud rates, or protocols as facts.

---

# 16. Claude Analysis Features

Claude is especially useful for:

- explaining unfamiliar protocol captures
- identifying patterns in CAN IDs
- comparing good vs bad test sessions
- finding likely packet fields
- recognizing counters/checksums/CRCs
- generating DBC candidates
- explaining Modbus maps
- analyzing SCPI results
- interpreting scope/logic measurements
- correlating PSU current spikes with bus events
- producing test recipes
- generating reports
- searching datasheets already saved in the project
- suggesting the next least-invasive measurement
- explaining firmware metadata and symbols
- converting raw observations into a concise engineering narrative

Claude should not replace deterministic decoding when a known decoder exists.

Use deterministic parser first, AI interpretation second.

---

# 17. Camera

Camera support is optional but valuable.

Use cases:

- photograph DUT before test
- capture connector orientation
- read serial/model labels
- QR/barcode identification
- attach physical evidence to a session
- photograph wiring state
- OCR a display or instrument
- compare before/after hardware condition

Preferred pipeline for USB camera:

```text
V4L2
  -> capture
  -> local file
  -> optional OpenCV processing
  -> optional OCR/barcode
  -> session attachment
```

Do not upload camera images to an AI service automatically.

If Claude needs an image, the user should explicitly invoke that analysis path.

---

# 18. Bench Orchestration

One of FieldDeck's highest-value features is synchronized multi-instrument testing.

Example:

```text
         ┌────────── PSU voltage/current
         │
         ├────────── DMM measurement
         │
 DUT ────┼────────── CAN traffic
         │
         ├────────── UART logs
         │
         ├────────── logic analyzer
         │
         ├────────── oscilloscope
         │
         └────────── camera snapshot

                  ↓

             ONE TIMELINE
```

A user should be able to say:

```text
Show me what happened 300 ms before the CAN fault.
```

FieldDeck should then correlate:

- CAN
- serial
- PSU
- DMM
- scope/logic capture
- user annotations

This unified timeline is a first-class feature.

---

# 19. HMI Screens

Recommended screen hierarchy:

```text
HOME
├── BUS
│   ├── CAN
│   ├── RS485 / MODBUS
│   └── UART / RS232
├── BENCH
│   ├── PSU
│   ├── DMM
│   ├── SCOPE
│   ├── LOAD
│   └── OTHER SCPI
├── LOGIC
│   ├── LOGIC ANALYZER
│   ├── I2C
│   ├── SPI
│   └── DECODERS
├── DEVICE
│   ├── USB
│   ├── SWD/JTAG
│   ├── FLASH
│   └── GPIO
├── TOOLS
│   ├── HEX/DEC/BIN
│   ├── ENDIAN
│   ├── FLOAT
│   ├── CRC
│   ├── UNIT
│   └── FILE INSPECT
├── SESSION
│   ├── START/STOP
│   ├── MARK EVENT
│   ├── NOTES
│   └── EXPORT
└── ASSISTANT
    ├── ASK ABOUT SESSION
    ├── EXPLAIN SELECTION
    ├── NEXT TEST
    └── OPEN CLAUDE
```

---

# 20. HMI CAN Example

```text
┌─ CAN0 ─ 500 kbit/s ─ LISTEN ─ REC● ────────────────┐
│ RX/s  1248      ERR 0       LOAD 18%               │
│                                                     │
│ ID       Hz      DLC     LAST                       │
│ 0x101    100.0   8       31 04 00 7A 10 00 00 8C  │
│ 0x181     10.0   8       00 00 4A 12 00 01 00 00  │
│ 0x280      1.0   4       7F 00 02 91              │
│                                                     │
│ [FILTER] [DECODE] [STATS] [RAW]                     │
│                                                     │
│ TX LOCKED                                SAFE       │
├─────────────────────────────────────────────────────┤
│ BACK        MARK        REC         MENU             │
└─────────────────────────────────────────────────────┘
```

---

# 21. HMI Serial Example

```text
┌─ SERIAL ─ FTDI_A10ABC ─ 115200 8N1 ────────────────┐
│ VIEW: HEX+ASCII                           REC●      │
│                                                     │
│ 12:16:01.102 ← 55 AA 04 10 7F 00 91 2C   U......, │
│ 12:16:01.202 ← 55 AA 04 10 80 00 82 1B   U....... │
│ 12:16:01.302 ← 55 AA 04 10 81 00 F3 0A   U....... │
│                                                     │
│ Pattern: 100 ms ± 1.8 ms                            │
│ CRC candidate: CRC-16/MODBUS  ?                     │
│                                                     │
│ [ASCII] [HEX] [ANALYZE] [SEND]                      │
├─────────────────────────────────────────────────────┤
│ BACK        MARK        REC         MENU             │
└─────────────────────────────────────────────────────┘
```

---

# 22. HMI Bench Example

```text
┌─ BENCH PSU ─ SAFE ──────────────────────────────────┐
│                                                     │
│         24.002 V            0.418 A                 │
│                                                     │
│ SET     24.000 V        LIMIT 1.000 A               │
│                                                     │
│ OUTPUT  ○ OFF                                       │
│                                                     │
│ [ -V ] [ +V ]   [ -I ] [ +I ]                      │
│                                                     │
│ POWER authorization required for OUTPUT             │
├─────────────────────────────────────────────────────┤
│ BACK        GRAPH       ARM         MENU             │
└─────────────────────────────────────────────────────┘
```

---

# 23. Assistant UX

Claude must not dominate the HMI.

The Assistant screen should be short-form.

Example:

```text
┌─ ASSISTANT ─────────────────────────────────────────┐
│ CURRENT SESSION: motor-test-04                      │
│                                                     │
│ Finding:                                             │
│ CAN 0x181 stops 312 ms after current rises above    │
│ 0.91 A in all three failed runs.                    │
│                                                     │
│ Suggested next test:                                │
│ Capture CAN + PSU at power-up with 1 ms logging.    │
│                                                     │
│ [DETAIL] [RUN SETUP] [OPEN CLAUDE]                  │
├─────────────────────────────────────────────────────┤
│ BACK        MARK        REC         MENU             │
└─────────────────────────────────────────────────────┘
```

Long conversations belong in the CLAUDE tmux window, not this screen.

---

# 24. Remote Interface

The local 480×320 screen is for immediate field control.

Detailed plots should be available from another computer.

Optional later component:

```text
instrumentd
   └── local WebSocket/HTTP API
          └── FieldDeck Web
```

Remote UI can provide:

- large CAN tables
- waveform plots
- protocol trees
- synchronized timelines
- test reports
- firmware browsing
- camera images
- recipe editor

Remote access should be disabled by default or bound to a trusted interface.

Never expose the control API directly to the public Internet.

---

# 25. Development Standards

## Code

- type annotate public Python APIs
- use Pydantic models at RPC boundaries
- prefer composition over deep inheritance
- keep hardware-specific code behind driver adapters
- no magic sleeps when an event/status check exists
- define timeouts
- define cancellation behavior
- preserve raw data
- keep safety checks server-side

## Tests

Every driver should have:

- parser tests
- timeout tests
- disconnect tests
- malformed-input tests
- permission tests
- mocked-hardware tests

Backend safety must be testable without physical hardware.

## Logs

Use structured logs.

Include:

```text
timestamp
session
component
device
action
permission level
result
duration
error
```

Never log credentials/API keys.

---

# 26. Implementation Order

Implement in this order unless the user changes priorities.

## Phase 1 — Skeleton

1. repository structure
2. `instrumentd`
3. RPC
4. safety/arming
5. `fdctl status`
6. session logging
7. Textual HMI shell

## Phase 2 — First useful interfaces

1. serial
2. CAN
3. Modbus
4. conversion/CRC tools
5. session recorder

## Phase 3 — Bench

1. SCPI/VISA discovery
2. PSU
3. DMM
4. scope capture
5. electronic load

## Phase 4 — Analysis

1. sigrok
2. protocol decoders
3. unified timeline
4. capture comparison
5. camera

## Phase 5 — Device programming

1. SWD/JTAG
2. OpenOCD
3. pyOCD
4. esptool
5. DFU
6. AVR/Pico tools

## Phase 6 — Claude

1. MCP server
2. read-only tools
3. session analysis
4. authorized query tools
5. authorized control tools
6. test recipe generation/validation

Do not begin by giving Claude unrestricted shell access to hardware.

---

# 27. Claude Startup Behavior

When Claude starts in this repository:

1. read this file
2. inspect the repository tree
3. inspect current `instrumentd` status if tools are available
4. do not assume hardware is attached
5. do not assume hardware mappings from a previous session
6. use existing adapters before writing new ones
7. preserve backwards compatibility with existing recipe schemas
8. run tests after backend changes
9. state when a recommendation depends on unverified electrical information

If the user says:

```text
"talk to this"
"figure out what this is"
"scan this"
"debug this"
```

interpret the workflow as:

```text
discover -> passive capture -> decode -> explain -> suggest query -> authorize -> query
```

not:

```text
guess -> transmit
```

---

# 28. Design Principle

FieldDeck is an instrument.

Its highest priorities are:

```text
DETERMINISTIC
SAFE
FAST
VISIBLE
REVERSIBLE
LOGGED
SCRIPTABLE
AI-OPTIONAL
```

The manual UI must always remain complete enough to operate the device without Claude.

Claude should make FieldDeck smarter, not make FieldDeck dependent on Claude.
