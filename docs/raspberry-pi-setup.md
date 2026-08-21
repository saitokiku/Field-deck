# Raspberry Pi setup

Everything needed to go from a bare Pi to a FieldDeck unit you can put in a bag.

Read [What you need](#what-you-need) before buying anything, and
[Before you connect to something that matters](#before-you-connect-to-something-that-matters)
before connecting to something that matters.

> **This install path has not been run on physical hardware.** It has been
> syntax-checked, dry-run end to end, and every configuration file it writes has
> been validated against the code that reads it. That is not the same as having
> booted a Pi with it. Run `--dry-run` first, read what it intends to do, and
> [tell us what happened](https://github.com/saitokiku/field-deck/issues).

---

## Contents

1. [What you need](#what-you-need)
2. [Choosing a CAN interface](#choosing-a-can-interface)
3. [Prepare the SD card](#1-prepare-the-sd-card)
4. [Install FieldDeck](#2-install-fielddeck)
5. [Verify](#3-verify)
6. [The panel](#4-the-panel)
7. [Bring up a CAN interface](#5-bring-up-a-can-interface)
8. [Serial and RS-485](#6-serial-and-rs-485)
9. [Bench instruments](#7-bench-instruments)
10. [Configuration](#8-configuration)
11. [Headless units](#headless-units)
12. [Updating and removing](#updating-and-removing)
13. [Before you connect to something that matters](#before-you-connect-to-something-that-matters)

---

## What you need

### The computer

| | | |
|---|---|---|
| **Raspberry Pi 4**, 2 GB+ | required | A Pi 5 is fine and faster. A Pi 400 works but has no GPIO header for a SPI panel or CAN HAT. A **Pi 3 will do serial, Modbus and bench work** but drops frames under sustained CAN load — the SPI bus and the USB/Ethernet controller share bandwidth. A Pi Zero 2 W is not enough. |
| **microSD, 32 GB+, A2** | required | Captures are small individual writes, constantly. A slow or worn card is the **single most common cause of dropped frames**, and it looks exactly like a bus problem. SanDisk Extreme or Samsung Pro Endurance. |
| **5 V / 3 A supply** | required | Use the official one. An under-volted Pi throttles mid-capture, and a browning-out CAN transceiver looks identical to a dead ECU. `vcgencmd get_throttled` returning anything but `0x0` invalidates a debugging session. |
| **Case with airflow** | recommended | A Pi 4 at 80 °C throttles. In a sealed field case in summer, that is not hypothetical. |

### The panel

| | |
|---|---|
| **3.5" 480×320 SPI touchscreen** | The HMI is laid out for exactly **80×25 characters**, which is what an 80-column 6×12 bitmap font gives you at 480×300 — leaving 20 px for the tmux status bar. Waveshare 3.5" (A)/(B), and the many clones of them, are the reference. |
| Resistive vs capacitive | **Resistive is better here.** It works with gloves, with a stylus, with a wet finger, and in a workshop. The HMI's touch targets are sized for a fingertip on a resistive panel (minimum 15 columns × 3 rows). |
| HDMI instead | Perfectly fine. `--no-kiosk` and any monitor, or just SSH. The 80×25 layout is a floor, not a ceiling. |

### Buses and instruments

Buy for what you actually work on. Every one of these is optional.

| For | Get | Watch out for |
|---|---|---|
| **Serial / UART** | FTDI FT232R, CP2102, or CH340 USB adapter | Counterfeit FTDI chips are everywhere and fail in confusing intermittent ways. CH340 is cheap and honest. |
| **RS-232** | A real level shifter (MAX3232 based) | RS-232 swings ±12 V. **A 3.3 V TTL adapter on an RS-232 line dies**, and may take the Pi with it. FieldDeck will not guess which one you have — the `electrical` field in a serial preset is `unknown` until you say. |
| **RS-485 / Modbus RTU** | USB-RS485 with automatic direction control, or a MAX3485 breakout | Termination (120 Ω) and biasing are bus-level decisions. Adding a second terminator to a properly terminated bus breaks it. |
| **CAN / CAN FD** | See [below](#choosing-a-can-interface) | |
| **Bench instruments** | Anything SCPI over USBTMC or LXI | Rigol DP800/DS/DL, Siglent SPD/SDS/SDL, Keysight, Tektronix, Keithley, R&S. |
| **Firmware / debug** | ST-Link V2, J-Link, CMSIS-DAP | Clone ST-Links are fine and cost $3. |
| **Logic analysis** | Any `sigrok`-supported analyzer | The $8 8-channel "Saleae clone" is genuinely useful up to ~8 MHz. |

### Strongly recommended, and cheap

- **A USB isolator** (ADuM3160/ADuM4160 based, ~$15) between the Pi and
  *anything* attached to a vehicle, an industrial machine, or mains-adjacent
  equipment. Ground potential differences kill Pis, and the Pi is not the
  expensive thing on the bench.
- **A real-time clock** (DS3231, ~$3). A Pi with no network boots in 1970.
  FieldDeck records a monotonic clock alongside wall time precisely so that a
  wrong RTC cannot corrupt *correlation* — but a report timestamped 1970 is
  still a report nobody can file.
- **A UPS HAT or USB power bank** so a capture survives being unplugged, and so
  the SD card is not corrupted by a hard power cut mid-write.
- **Ferrules and a label maker.** The failure you will spend the most time on is
  a wire in the wrong hole.

---

## Choosing a CAN interface

This is the decision that most affects whether FieldDeck is pleasant to use.

| Option | Bitrate | Notes |
|---|---|---|
| **MCP2515 HAT** (SPI) | reliable to ~500 kbit/s | ~$10. The most common Pi CAN HAT. The SPI interrupt path limits sustained throughput; on a busy 500 kbit/s bus you *will* see overruns on a Pi 3. Classical CAN only. |
| **MCP2518FD HAT** (SPI) | 1 Mbit/s + FD | Waveshare 2-CH CAN FD HAT and similar, ~$30. Much better interrupt behaviour, supports CAN FD. **This is the recommended HAT.** |
| **USB: CANable / candleLight** | 1 Mbit/s | ~$25, `gs_usb` driver, works out of the box as SocketCAN. No GPIO header needed, so it coexists with an SPI panel without conflict. |
| **USB: PEAK PCAN-USB, Kvaser Leaf** | 1 Mbit/s + FD | $200–400. What you buy when the capture has to be trusted. Excellent Linux drivers. |
| **Anything without a transceiver** | — | Do not. A "CAN module" that is only a controller, or a TTL-level pin pair, will not work and may damage the bus. |

**SPI conflict warning.** A 3.5" SPI panel and an MCP2515/2518FD HAT both want
the SPI bus and both want interrupt GPIOs. They *can* coexist (different chip
selects, different interrupt pins), but it requires care in `config.txt` and it
is the most common cause of "the panel works until I bring up CAN". **If you are
buying both, buy a USB CAN interface.** It sidesteps the problem entirely for
about the same money.

---

## 1. Prepare the SD card

Use **Raspberry Pi OS Bookworm (64-bit) Lite**. Lite, not Desktop: FieldDeck
brings its own minimal Xorg for the panel, and a desktop environment fights it
for tty1, for the framebuffer, and for the input devices.

In Raspberry Pi Imager, use the gear icon to set:

- **hostname** — something you will recognise on a network of unknown machines
- **your user account** — *not* `pi`; the installer will add it to the
  `fielddeck` group
- **SSH on**, with your public key (password auth on a field device that gets
  plugged into other people's networks is a bad trade)
- **Wi-Fi**, if you want it. A field unit with no network is a perfectly good
  field unit — everything works offline.

First boot:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo raspi-config     # Interface Options: enable SPI and I2C if your panel or HAT needs them
sudo reboot
```

Confirm you have what the installer expects:

```bash
cat /etc/os-release | grep VERSION_CODENAME   # bookworm
uname -m                                       # aarch64
python3 --version                              # 3.11 or newer
```

FieldDeck needs Python **3.11+**. Bookworm ships 3.11. On Bullseye (3.9) it will
refuse to install rather than half-work.

---

## 2. Install FieldDeck

```bash
git clone https://github.com/saitokiku/field-deck.git
cd field-deck
```

**Read the plan first.** `--dry-run` needs no root and changes nothing:

```bash
scripts/install.sh --dry-run
```

It prints every command it intends to run, every file it intends to write, and
an explicit list of what it will *not* do. Read that list. Then:

```bash
sudo scripts/install.sh
```

Useful options:

| | |
|---|---|
| `--no-kiosk` | Daemon only. No Xorg, no terminal, no HMI unit. For a headless unit driven over SSH. |
| `--user NAME` | The operator account to add to the `fielddeck` group. Defaults to `$SUDO_USER`. |
| `--prefix DIR` | Where the virtualenv lives. Default `/opt/fielddeck`. |
| `--no-apt` | Skip apt entirely; you are providing the packages yourself. |
| `--from-lock` | Install Python dependencies from `requirements.lock` instead of resolving fresh — reproduces an earlier unit exactly. |
| `-y` | Do not prompt, even on an unsupported OS. |

### What it does

1. **Checks the machine.** Architecture, OS, Python version. On anything that
   isn't a Pi running Bookworm it says so plainly and asks before continuing.
2. **Installs 23 apt packages** in four groups — build, bus tools, instrument
   tools, and (unless `--no-kiosk`) a minimal Xorg. Every package has a comment
   in `scripts/install.sh` explaining why it is there. A package nobody can
   justify is a package that should not be on a field device.
3. **Creates the `fielddeck` system user and group.** The daemon runs as this
   unprivileged user. That is the entire security story: the one process allowed
   to touch hardware is also a process that cannot touch anything else.
4. **Builds a virtualenv** at `/opt/fielddeck/venv` and writes a
   `requirements.lock` snapshot, then links `fdctl`, `instrumentd`,
   `fielddeck-ui` and `fielddeck-mcp` into `/usr/local/bin`.
5. **Creates directories.** Config at `/etc/fielddeck` is **root-owned** — the
   daemon reads its safety policy and cannot rewrite it. State at
   `/var/lib/fielddeck` is `fielddeck`-owned, mode 0750.
6. **Installs udev rules** putting known adapters, probes and instruments into
   the `fielddeck` group. (See the note on `dialout` below.)
7. **Installs the kiosk assets** — tmux config, launcher, Xorg touch config.
8. **Installs and enables systemd units.**

### The `dialout` trade-off

The udev rules set `GROUP="fielddeck"` on USB serial adapters, which **replaces**
Debian's default `GROUP="dialout"`. Other software that expects `dialout`
(`minicom`, `screen`, the Arduino IDE) loses access to those specific adapters.

Two ways out, both documented at the top of `config/udev/99-fielddeck.rules`:

```bash
# Either: add those users to the fielddeck group as well
sudo usermod -aG fielddeck alice

# Or: delete the SUBSYSTEM=="tty" rules and rely on the fielddeck
# user's own dialout membership, which the installer also sets up.
```

### Log out and back in

Group membership is established at login. If `id -nG` doesn't list `fielddeck`
after the installer ran, you have not logged in again yet. This is the single
most common post-install confusion.

---

## 3. Verify

```bash
sudo scripts/preflight.sh
```

Twelve sections, from "does the daemon have an account to run as" through
"can the operator actually reach the socket without sudo" to "which optional
hardware libraries are missing and what that costs you". Every failure comes
with the exact command that fixes it.

It distinguishes **failures** (something is broken) from **warnings** (something
is absent, and here is what you lose). A unit with no logic analyzer installed
should show warnings, not failures.

> Run it as the operator too, not only with `sudo`. Root passes every permission
> check, which is exactly the thing you want to test. `preflight.sh` warns you
> about this itself.

Then, by hand:

```bash
systemctl status instrumentd --no-pager
ls -l /run/fielddeck/                # expect srw-rw---- fielddeck fielddeck instrumentd.sock
id -nG | tr ' ' '\n' | grep fielddeck
fdctl status
fdctl devices
fdctl limits                         # the safety policy this unit actually loaded
```

`fdctl limits` is worth reading carefully on a new unit. It is the answer to
"what will this thing refuse to do", and it is better to find out now than
while holding probes.

### Prove it works with nothing attached

```bash
sudo systemctl stop instrumentd
sudo -u fielddeck /opt/fielddeck/venv/bin/instrumentd --simulate --log-text
```

In another shell, `fdctl status` should show seven simulated devices and
`[SIMULATED - no hardware is attached]` in the banner. If simulation works and
real hardware doesn't, the problem is device access, not FieldDeck.

---

## 4. The panel

**The installer does not edit `/boot/firmware/config.txt`.** Your panel's
overlay is your decision, and a bad edit there costs you a boot. You do this
part.

For a typical Waveshare-compatible 3.5" SPI panel, add to
`/boot/firmware/config.txt`:

```ini
dtparam=spi=on
dtoverlay=waveshare35a:rotate=90
```

The correct overlay name depends on your exact panel — `waveshare35a`,
`waveshare35b`, `piscreen`, `tft35a`, and others exist, and clones are not
consistent about which they need. Check the vendor's documentation, and see
`/boot/overlays/README` for what your kernel actually ships:

```bash
grep -il "3.5\|480x320" /boot/overlays/*.dtbo 2>/dev/null
dtoverlay -h waveshare35a
```

After a reboot, `/dev/fb1` should exist:

```bash
ls -l /dev/fb*
# fb0 is HDMI, fb1 is the SPI panel
```

**If you have `/dev/fb0` but no `/dev/fb1`, the overlay did not load.** Nothing
downstream will work until it does. `dmesg | grep -i fb` usually says why.

### Touch

```bash
sudo systemctl start fielddeck-kiosk
```

The kiosk is *enabled* but not *started* by the installer, because starting it
takes over tty1 and would drop the shell you are typing in.

If touch is inverted or rotated — and on a rotated panel it usually is — the
calibration matrix lives in `/etc/X11/xorg.conf.d/10-fielddeck-touch.conf`.
That file explains, at length, why it sets *options* rather than a `Driver`
line, and what to do if you genuinely need `evdev` instead of `libinput`. Find
your device and experiment live:

```bash
DISPLAY=:0 xinput list
DISPLAY=:0 xinput set-prop <id> "Coordinate Transformation Matrix" 0 1 0 -1 0 1 0 0 1
```

When a matrix works, write it into the conf file so it survives a reboot.

### What the kiosk actually is

There is no desktop. No window manager, no compositor, no panel. The chain is:

```
systemd → xinit → xterm (6x12 bitmap font, 80x25) → tmux → fielddeck-ui
```

tmux gives you four windows, and the session is supervised so it never dies:

| | |
|---|---|
| **1 HMI** | The FieldDeck console |
| **2 CLAUDE** | An assistant session, if you want one |
| **3 SHELL** | A plain shell |
| **4 LOG** | `journalctl -fu instrumentd` |

The status bar is touchable — the window numbers are the tabs.

The same four windows, over SSH, without X:

```bash
fielddeck-session
```

---

## 5. Bring up a CAN interface

**The installer does not do this.** Bringing up a CAN interface energises a
transceiver on somebody's bus. That is an operator's act.

```bash
# Listen only. Nothing this interface does can be seen by the bus.
sudo ip link set can0 up type can bitrate 500000 listen-only on

ip -details -statistics link show can0
```

`listen-only on` puts the controller in a mode where it does not even send ACK
bits. On a two-node bus, a listening third node that ACKs can mask a fault you
are trying to find; on a bus you have the wrong bitrate for, a node that ACKs
generates error frames and can bus-off the real participants.

FieldDeck defaults every CAN interface to listen-only and **will refuse to clear
it for you.** If you ask FieldDeck to transmit on a listen-only interface, it
tells you what to run and why; it does not silently reconfigure your bus.

To transmit, you take the interface down and bring it up deliberately:

```bash
sudo ip link set can0 down
sudo ip link set can0 up type can bitrate 500000
```

### For an MCP2515/2518FD HAT

In `/boot/firmware/config.txt` — note `oscillator` must match the crystal on
your specific board (8 MHz and 12 MHz are both common, and getting it wrong
gives you a bitrate that is wrong by exactly that ratio):

```ini
dtparam=spi=on
dtoverlay=mcp2515-can0,oscillator=12000000,interrupt=25
```

### Don't guess the bitrate by transmitting

FieldDeck never transmits to determine a bitrate. To find an unknown one, bring
the interface up listen-only at a candidate rate and watch the error counters:

```bash
for rate in 125000 250000 500000 1000000; do
  sudo ip link set can0 down 2>/dev/null
  sudo ip link set can0 up type can bitrate $rate listen-only on
  echo "--- $rate ---"; timeout 3 candump -e can0,\#FFFFFFFF | head -5
done
```

The rate at which you see clean frames and no error frames is the rate.

### A virtual bus, for practice

```bash
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan && sudo ip link set vcan0 up
```

`vcan0` behaves like a real interface to everything in FieldDeck, and there is
nothing on the other end to damage.

---

## 6. Serial and RS-485

USB adapters need no setup beyond the udev rules; `fdctl devices` will show them.

To use the Pi's own UART on the GPIO header, you must first take it away from
the kernel console — otherwise you are sharing a port with a login prompt:

```ini
# /boot/firmware/config.txt
enable_uart=1
dtoverlay=disable-bt        # gives /dev/ttyAMA0 the good PL011 UART
```

```bash
sudo systemctl disable --now serial-getty@ttyS0.service
# and remove console=serial0,115200 from /boot/firmware/cmdline.txt
```

**The Pi's UART is 3.3 V TTL.** It is not RS-232 and it is not 5 V tolerant.
Connecting it to an RS-232 port destroys it.

### Presets

Rather than remembering settings, put them in `/etc/fielddeck/fielddeck.yaml`:

```yaml
serial_presets:
  - name: "modbus-rtu-9600"
    baudrate: 9600
    parity: E
    stopbits: 1
    electrical: rs485        # unknown | ttl | rs232 | rs485
  - name: "console-115200"
    baudrate: 115200
    electrical: ttl
```

`electrical` defaults to `unknown` and FieldDeck will not infer it. It is
recorded in the session so that a capture six months from now still says what
was physically on the wire.

---

## 7. Bench instruments

USBTMC instruments are handled by the udev rules; LXI instruments need nothing
but a route.

```bash
lsusb                        # is it enumerated at all
ls -l /dev/usbtmc*           # expect group fielddeck
fdctl bench devices
fdctl scpi query <device> '*IDN?'
```

Install the VISA extra if it wasn't already:

```bash
sudo /opt/fielddeck/venv/bin/pip install 'fielddeck[bench]'
sudo systemctl restart instrumentd
```

> **Every instrument profile that ships with FieldDeck has
> `hardware_verified: false`.** They are careful readings of published
> programming manuals, not measurements. SCPI dialects differ in ways manuals
> do not mention. Check a setpoint with a meter before trusting it with
> something expensive, and please
> [report what you find](https://github.com/saitokiku/field-deck/issues) — a
> profile that has actually been on a bench is worth ten that have been read
> about.

---

## 8. Configuration

| Path | Owner | What |
|---|---|---|
| `/etc/fielddeck/fielddeck.yaml` | root | Devices, presets, aliases, display, storage |
| `/etc/fielddeck/safety.yaml` | root | Limits, TTL caps, denied permission classes |
| `/etc/fielddeck/instrumentd.env` | root | Environment for the systemd unit |
| `/var/lib/fielddeck/sessions/` | fielddeck | Captures, timelines, reports |
| `/run/fielddeck/instrumentd.sock` | fielddeck | The control socket |
| `/run/fielddeck/instrumentd-ai.sock` | fielddeck | The restricted socket |

Config is root-owned on purpose: the daemon reads its own safety policy and
cannot rewrite it. A daemon that can raise its own limits does not have limits.

Start from the annotated examples in `config/`. A minimal `safety.yaml`:

```yaml
global_limits:
  psu.voltage: { quantity: psu.voltage, unit: V, maximum: 24.0 }
  psu.current: { quantity: psu.current, unit: A, maximum: 3.0 }

max_arm_ttl_s:
  POWER: 180
  FLASH: 300
  DESTRUCTIVE: 60

# On a unit that should never erase anything, say so here and it becomes
# impossible rather than merely discouraged:
# denied_permissions: [DESTRUCTIVE]
```

Limits are enforced by the dispatcher and **cannot be waived by any
authorization**. Arming POWER lets you *ask* for 30 V; a 24 V limit is what
decides the answer is no. After editing:

```bash
sudo systemctl restart instrumentd
fdctl limits                  # confirm what actually loaded
```

A daemon that refuses to start because `safety.yaml` is malformed is working as
designed. `journalctl -u instrumentd -n 40` will name the field.

---

## Headless units

```bash
sudo scripts/install.sh --no-kiosk
```

No Xorg, no panel, no kiosk unit. Everything else is identical: the daemon, the
CLI, recipes, the MCP server, sessions.

```bash
ssh pi-unit fdctl status
ssh pi-unit fielddeck-session      # the same four tmux windows, over SSH
```

**Do not expose the control socket to a network.** The `remote` block in
`fielddeck.yaml` is off by default, binds to `127.0.0.1`, and the config model
*rejects* a wildcard bind address outright. If you need remote access, use SSH
port forwarding — an SSH tunnel is authenticated, and the control API is not:

```bash
ssh -L 8787:127.0.0.1:8787 pi-unit
```

---

## Updating and removing

```bash
cd field-deck
git pull
sudo scripts/install.sh          # idempotent; never overwrites your config
sudo systemctl restart instrumentd
sudo scripts/preflight.sh
```

To pin a unit to a known-good dependency set, keep its `requirements.lock` and
install with `--from-lock`.

```bash
sudo scripts/uninstall.sh        # removes units, venv and udev rules
```

The uninstaller keeps two things by default, and tells you so:

* **Your sessions.** Captured traces are evidence — a recording of a fault you
  may not be able to reproduce. `--purge-sessions` deletes them, and prompts
  before it does.
* **The `fielddeck` user**, because it still owns those session files.

Delete both by hand when you actually mean to.

---

## Before you connect to something that matters

A checklist worth running once, on the bench, before the first time it counts.

1. **Confirm the ground reference.** Measure between the Pi's ground and the
   target's ground before connecting anything else. If it isn't zero, use an
   isolator; the difference is about to flow through your USB adapter.
2. **Confirm the voltage levels.** TTL, RS-232 and RS-485 are three different
   things that use the same connectors. FieldDeck will not guess, and neither
   should you.
3. **Confirm the bus is terminated correctly** — and that adding your node
   doesn't make it terminated twice.
4. **Start listen-only.** Always. On every bus. The first thing FieldDeck does
   is nothing, and that is the right first thing.
5. **Start a session before you start probing.** `fdctl session start "<name>"`.
   Everything after that is timestamped on one clock and reconstructable later.
   A capture you didn't record is a test you have to repeat.
6. **Check `fdctl limits`** and set a current limit lower than you think you
   need. The first energised rail is an experiment.
7. **Know where the stop is.** `F9` on the panel, `fdctl estop` from any shell,
   from any client, at any time — including while an action is running. It
   latches, and clearing it requires a human acknowledgement.

FieldDeck's job is to make the careful path the easy one. It cannot make a bad
connection safe, and it does not pretend to.

---

## See also

- [docs/troubleshooting.md](troubleshooting.md) — symptom → cause → fix
- [docs/safety-model.md](safety-model.md) — what the permission classes mean
- [docs/usage.md](usage.md) — actually using it
