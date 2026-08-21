# FieldDeck

**A safety-first, terminal-native engineering console for the Raspberry Pi.**

FieldDeck turns a Pi and a 3.5" touchscreen into the instrument you actually
want on a bench or in a field kit: a protocol analyzer, a bus sniffer, a bench
controller, a data logger, a firmware programmer, and a test-automation
runner — all sharing one clock, one session log, and one authorization model.

It is designed for the moment you are kneeling next to a machine that is not
working, holding a $35 computer, and the thing you most need is to *not* make
it worse.

```
┌─ FIELDDECK ──────────────────────────── SAFE ── ● rec 00:04:17 ─┐
│                                                                  │
│  can0    ✓ listen-only   500 kbit/s      1,284 fr   12 IDs      │
│  ttyUSB0 ✓ 115200 8N1    RS-232?          4.1 kB    3 errors    │
│  psu0    ○ output OFF    24.00 V set      0.000 A               │
│                                                                  │
│  ! 0x181 stopped transmitting 312 ms after the rail came up      │
│                                                                  │
│  [F1] devices  [F2] capture  [F3] bench  [F5] session  [F9] STOP │
└──────────────────────────────────────────────────────────────────┘
```

---

## The idea

Most bench tools assume you know what you are connected to. FieldDeck assumes
you don't — and that being wrong could destroy the thing on the bench.

So it boots **PASSIVE**. On a cold start FieldDeck can listen to a CAN bus, read
a UART, decode a protocol, take a capture and write a report. It cannot transmit
a frame, energise a rail, write a register or flash a chip until a human says so,
out loud, with a timer running.

That is not a mode you switch off. It's the architecture:

- **One process owns the hardware.** `instrumentd` is the only thing that opens
  `/dev/tty*`, `/dev/spidev*`, `/dev/i2c-*`, a CAN interface or a USB instrument.
  The UI, the CLI, recipes and the AI assistant are all *clients* of it, over a
  Unix socket, with identical rules.
- **Authorization is exact-class and expires.** Six permission classes, each
  armed separately, each with a TTL. A `POWER` grant does not authorize a
  `CONTROL` action. Nothing inherits.
- **Sustained hazards need a dead-man's handle.** An energised output is held
  by a *lease*. If the client holding it dies, stops renewing, or the daemon
  restarts, the output goes off. Not eventually — as part of the same code path.
- **Raw capture data is immutable.** Analysis produces new artifacts with
  provenance. Nothing rewrites what the wire actually said.

---

## Try it with no hardware

FieldDeck ships a complete simulated bench — a CAN interface, a serial adapter,
a programmable supply, a DMM, a Modbus slave and a logic analyzer. They are
driven through the *same* drivers, the *same* dispatcher and the *same*
authorization pipeline as real hardware. There is no separate demo data path.

```bash
git clone https://github.com/saitokiku/field-deck.git
cd field-deck
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'

# Terminal 1 — the daemon, with the simulated bench attached
.venv/bin/instrumentd --simulate --log-text

# Terminal 2 — the console
.venv/bin/fdctl status
```

Then watch it refuse to do something dangerous, and then do it:

```
$ fdctl psu set role:psu --voltage 5.0
┏━ PermissionDenied ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ psu.set requires an active POWER authorization. A human must grant it:     ┃
┃ fdctl arm power --ttl 60                                                   ┃
┃ action      psu.set                                                        ┃
┃ permission  POWER                                                          ┃
┃ device_id   sim:visa:sim-psu-0                                             ┃
┃ armed       -                                                              ┃
┃ preserved: no command was sent to the device                               ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

$ fdctl arm power --ttl 60
┏━ ARMED - you are now responsible for what the hardware does ━━━━━━━━━━━━━━━┓
┃ POWER       for 60s, until 01:35:26Z      over all devices                 ┃
┃              grant arm-cf14eb27                                            ┃
┃ Disarm early with:  fdctl disarm                                           ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

$ fdctl psu set role:psu --voltage 5.0
╭─ role:psu ─────────────────────────────────────────────────────────────────╮
│ setpoint_v       5                                                         │
│ current_limit_a  0.5                                                       │
│ output           no                                                        │
╰────────────────────────────────────────────────────────────────────────────╯
```

Note what did *not* happen: setting a voltage did not turn the output on.
Energising is a separate action, and it takes a lease.

Turn on the simulated fault and watch a real investigation:

```bash
FIELDDECK_SIM_FAULT=1 instrumentd --simulate --log-text
```

The simulated board now browns out 1.4 s after you energise it, its CAN
heartbeat stops 312 ms later, and its UART reports an error 7 ms after that —
one causal story across three buses, correlated on one monotonic clock, which is
exactly the thing FieldDeck exists to show you.

---

## What you need

The minimum that is genuinely useful, and what each thing buys you.

### Required

| Part | Notes |
|---|---|
| **Raspberry Pi 4**, 2 GB or more | A Pi 5 works. A Pi 3 works for serial/Modbus but struggles with sustained CAN capture. |
| **microSD card, 32 GB+, A2-rated** | Capture writes are small but constant. A slow card is the single most common cause of dropped frames. |
| **Raspberry Pi OS Bookworm (64-bit) Lite** | Lite, not Desktop. FieldDeck brings its own minimal Xorg for the panel. |
| **A good 5 V supply** | 3 A official PSU. Under-volting a Pi mid-capture corrupts the session, and a bus transceiver browning out looks exactly like a bus fault. |

### Strongly recommended

| Part | Why |
|---|---|
| **3.5" 480×320 SPI touchscreen** | The HMI is laid out for exactly 80×25 characters at this size. Waveshare 3.5" (B) and clones work; resistive is fine and works with gloves. |
| **USB-serial adapter** with a real chip | FTDI FT232, Silicon Labs CP2102, or CH340. Counterfeit FTDI chips are common and fail in confusing ways. |
| **CAN interface** | See below — this is the choice that matters most. |

### Per-protocol

| You want to work on | Get |
|---|---|
| **CAN / CAN FD** | A SocketCAN-capable interface. MCP2515 HATs (SPI) are cheap and cap out around 500 kbit/s reliably; a Waveshare 2-CH CAN FD HAT (MCP2518FD) or a USB Kvaser/PEAK/CANable is better. **Do not** use an interface without a proper transceiver. |
| **RS-485 / Modbus RTU** | A USB-RS485 adapter with automatic direction control, or a MAX3485 breakout. Note whether your bus needs 120 Ω termination — FieldDeck will not guess. |
| **RS-232** | A real level shifter (MAX3232). A TTL adapter on an RS-232 line sees ±12 V and dies. |
| **Bench instruments** | Anything speaking SCPI over USBTMC or LXI: Rigol DP800/DS, Siglent SPD/SDS/SDL, Keysight, Tektronix, Keithley. |
| **Firmware / debug** | ST-Link V2, J-Link, CMSIS-DAP, or a Pi acting as an SWD probe via OpenOCD. |
| **Logic analysis** | Any `sigrok`-supported analyzer. An $8 8-channel clone is genuinely useful here. |

### Very good ideas

- **A USB isolator** between the Pi and anything attached to a vehicle,
  an industrial bus, or mains-adjacent equipment. Ground loops destroy Pis.
- **A UPS HAT or a power bank** so a capture survives being unplugged.
- **A real-time clock module.** Without one, a Pi with no network boots at
  1970 and your session timestamps are fiction. (FieldDeck records a monotonic
  clock alongside wall time specifically so a wrong RTC does not corrupt
  correlation — but you still want to know when something happened.)

---

## Install on a Raspberry Pi

Full detail, including panel wiring and CAN bring-up, is in
**[docs/raspberry-pi-setup.md](docs/raspberry-pi-setup.md)**. The short version:

```bash
# On a fresh Raspberry Pi OS Bookworm Lite (64-bit)
sudo apt update
git clone https://github.com/saitokiku/field-deck.git
cd field-deck

sudo scripts/install.sh --dry-run    # read what it intends to do
sudo scripts/install.sh              # then let it
sudo scripts/preflight.sh            # then verify the result
sudo reboot
```

The installer creates an unprivileged `fielddeck` system user, builds a
virtualenv at `/opt/fielddeck`, installs udev rules that put adapters and
instruments into the `fielddeck` group, writes a hardened systemd unit, and
sets up the kiosk (Xorg → xterm → tmux → HMI) on tty1.

Two things it deliberately does **not** do:

- **It does not edit `/boot/firmware/config.txt`.** Your panel's `dtoverlay`
  is your decision, and a bad edit there costs you a boot.
- **It does not bring up a CAN interface.** That energises a transceiver on
  someone's bus. That is an operator's act, not an installer's.

`--dry-run` prints the whole plan and needs no root. `--no-kiosk` gives you a
headless unit driven over SSH. `preflight.sh` checks twelve areas — from
"does the daemon have an account to run as" to "which optional hardware
libraries are missing and what that costs you" — and gives an exact
remediation command for every failure.

---

## What it speaks

| | |
|---|---|
| **Serial** | UART / RS-232 / RS-485, arbitrary framing, byte-exact capture, auto-baud by evidence |
| **CAN** | Raw frames, CAN FD, DBC decode, **ISO-TP** (ISO 15765-2), **UDS** (ISO 14229), **J1939** PGN decode |
| **Modbus** | RTU and TCP, read/write, address scan with an explicit fault-report policy |
| **SCPI** | Bench supplies, loads, DMMs, scopes over USBTMC and LXI |
| **Logic** | `sigrok` capture, I²C / SPI / UART decode |
| **Debug** | OpenOCD, pyOCD, esptool, avrdude, dfu-util, picotool — behind a `FLASH` grant |
| **Analysis** | 20 CRC models with reverse lookup, framing detection, COBS/SLIP, entropy, timing histograms, protocol identification with stated confidence |

The analyzer answers *"unknown / insufficient evidence"* when that is the
honest answer. A tool that always has a theory is a tool that will confidently
point you at the wrong wire.

---

## Claude, and why it can't hurt anything

FieldDeck ships an [MCP](https://modelcontextprotocol.io) server so Claude
can sit in the second tmux window, watch the same session you're watching,
and help you read what the bus is doing.

It connects to a **different socket** than you do. On that socket:

- Every request is stamped `source=claude`, in the audit log, forever.
- `safety.arm`, `safety.disarm`, `safety.estop_clear` and `safety.lease_renew`
  are refused **at the transport**, before any handler sees them. Not by policy
  — by there being no code path. Releasing a lease is still allowed, because
  that ends a hazard.
- Of the 29 tools exposed, none arms anything. `estop` is there: Claude can
  stop the bench, and cannot start it.
- When the next useful step needs authority, the tool result says so and tells
  the model to stop and ask you.

The assistant is a very good pair of eyes on a 4 MB capture. It is not an
operator, and the architecture is what enforces that rather than the prompt.

See **[docs/claude-integration.md](docs/claude-integration.md)**.

---

## Documentation

| | |
|---|---|
| [docs/raspberry-pi-setup.md](docs/raspberry-pi-setup.md) | Hardware, wiring, install, panel, CAN bring-up, first boot |
| [docs/safety-model.md](docs/safety-model.md) | Permissions, grants, leases, limits, ESTOP — and the reasoning |
| [docs/architecture.md](docs/architecture.md) | Daemon, dispatcher, drivers, event bus, sessions |
| [docs/usage.md](docs/usage.md) | `fdctl` and the HMI, task by task |
| [docs/protocols.md](docs/protocols.md) | What each bus decoder does and what it refuses to assume |
| [docs/recipes.md](docs/recipes.md) | Writing repeatable test procedures in YAML |
| [docs/claude-integration.md](docs/claude-integration.md) | The MCP server and the AI boundary |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Symptom → cause → fix |

---

## Project status

**Alpha, and honest about it.**

Everything in this repository has been verified in simulation: the full safety
gauntlet, lease expiry and client death, ESTOP under load, daemon restart,
backpressure, byte-exact serial round-trips through a pty, and hostile RPC
input. The test suite runs with no hardware attached and is what CI enforces.

**Nothing here has been verified against physical hardware.** No bench profile
carries `hardware_verified: true`. The Pi install path, the SPI panel and the
kiosk boot have been syntax-checked and dry-run, not run on a real Pi. Every
instrument profile is a best-effort reading of a published programming manual.

If you run FieldDeck against real hardware, [tell us what
happened](https://github.com/saitokiku/field-deck/issues) — especially if it
was wrong. A profile that has actually been on a bench is worth more than ten
that have been read about.

Treat the first energised rail as an experiment, and put a current limit on it.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: `make check` is
exactly what CI runs, new hardware support needs a simulated device alongside
it, and a change that widens what FieldDeck can do without authorization needs
to explain itself in the pull request.

Security issues: [SECURITY.md](SECURITY.md).

## License

[Apache-2.0](LICENSE).
