# Troubleshooting

Symptom → cause → fix. Ordered roughly by how often each one bites.

Run this first; it checks twelve areas and names the fix for every failure:

```bash
sudo scripts/preflight.sh
```

> Run it as the operator too, not only with `sudo`. Root passes every permission
> check, which is precisely the thing you want to test.

---

## `instrumentd is not running (no socket at ...)`

**1. It genuinely isn't.**

```bash
systemctl status instrumentd --no-pager
journalctl -u instrumentd -n 40 --no-pager
```

A daemon that refuses to start because `/etc/fielddeck/safety.yaml` is malformed
is working as designed. The journal line names the offending field.

**2. You are looking at the wrong socket.**

```bash
echo "$FIELDDECK_SOCKET"          # overrides everything if set
ls -l /run/fielddeck/
fdctl --socket /run/fielddeck/instrumentd.sock status
```

**3. You are not in the `fielddeck` group.** The socket is `srw-rw---- fielddeck
fielddeck`, so a non-member gets "connection refused" rather than a permissions
error, which reads exactly like "not running".

```bash
id -nG | tr ' ' '\n' | grep fielddeck
sudo usermod -aG fielddeck "$USER"
# then log out and back in — group membership is established at login
```

This is the single most common post-install confusion. `newgrp fielddeck` gets
you a shell with the group without logging out.

---

## `psu.set requires an active POWER authorization`

Working as designed. Arm it:

```bash
fdctl arm power --ttl 60
```

If you armed it and still get this, one of:

- **It expired.** Grants always have a TTL. `fdctl status` shows what is armed.
- **You armed the wrong class.** Authorization is exact-class: a POWER grant
  does not authorize a CONTROL action. The error lists what *is* armed. Arm
  several at once: `fdctl arm control power --ttl 120`.
- **The grant is scoped.** A grant made with `--device` or `--action` covers
  only that. The error names the device it was asked about.
- **The daemon restarted.** Grants never survive a restart, by design.
- **Policy denies the class.** `fdctl limits` shows `denied_permissions`. That
  refusal says "disabled by this deployment's safety policy" rather than "no
  active grant" — read which one you got.

---

## `emergency stop is latched`

```bash
fdctl estop clear
```

Then **arm again** — clearing a stop is not arming. After clearing, the unit is
SAFE with nothing armed.

If it re-latches immediately, something is calling it. `fdctl events --limit 20`
shows the `ESTOP` event with its `source` and `reason`.

---

## The value was rejected even though I'm armed

```
LIMIT_REJECTED  psu.voltage 30.0 exceeds the maximum 24.0 V
```

Authorization and limits are independent. Arming lets you *ask*; the limit
decides the answer. **No authorization waives a limit** — there is no override
flag.

```bash
fdctl limits                        # what this unit actually enforces
sudo nano /etc/fielddeck/safety.yaml
sudo systemctl restart instrumentd
fdctl limits                        # confirm what loaded
```

Check for a **device-specific** limit as well as a global one; the stricter of
the two applies. And for a **derived** limit: 24 V and 5 A can each be legal
while 120 W is not.

---

## The CAN interface shows nothing

In order:

**1. Is it up?** The installer deliberately does not bring CAN interfaces up.

```bash
ip -details link show can0
sudo ip link set can0 up type can bitrate 500000 listen-only on
```

**2. Is the bitrate right?** The commonest cause by far.

```bash
ip -details -statistics link show can0     # look at the error counters
```

Rising error counters with no frames means wrong bitrate. Sweep for it
*without transmitting*:

```bash
for rate in 125000 250000 500000 1000000; do
  sudo ip link set can0 down 2>/dev/null
  sudo ip link set can0 up type can bitrate $rate listen-only on
  echo "--- $rate ---"; timeout 3 candump -e can0,\#FFFFFFFF | head -5
done
```

For an MCP2515/2518FD HAT, check `oscillator=` in `config.txt` matches the
crystal on your board. 8 MHz and 12 MHz are both common, and getting it wrong
gives a bitrate wrong by exactly that ratio — 500 kbit/s configured, 333 kbit/s
actual.

**3. Is the bus terminated?** 120 Ω at each end, and only at the ends. With the
bus powered off, measure across CAN-H and CAN-L: **60 Ω** means correctly
terminated at both ends. 120 Ω means one terminator; ~40 Ω means three.

**4. Are H and L swapped?** Common, silent, and gives you exactly this symptom.

**5. Is there anything to hear?** A bus with one live node and no traffic looks
identical to a broken interface. Cross-check with `candump can0` in a shell.

---

## `refusing to transmit on a listen-only interface`

Intended. FieldDeck will not reconfigure your bus. It gives you the command:

```bash
sudo ip link set can0 down
sudo ip link set can0 up type can bitrate 500000
```

Consider whether you want to. Listen-only is the right default, and a node that
ACKs on a bus you have the wrong bitrate for can bus-off the real participants.

---

## Serial: nothing arrives, or it's garbage

**Garbage in a repeating pattern** is almost always the wrong baud rate.

```bash
fdctl serial capture ttyUSB0 --seconds 5
fdctl analyze --path serial/ttyUSB0-capture-0001.bin
```

`analyze` looks at bit timing and byte-value distribution and ranks hypotheses.

**Nothing at all:**

- TX/RX not swapped? A straight-through cable between two DTEs connects TX to
  TX and neither hears anything.
- Common ground? Two devices with no shared reference do not communicate.
- Right voltage level? TTL, RS-232 and RS-485 use the same connectors and are
  not interchangeable. A 3.3 V adapter on an RS-232 line is destroyed by it.
- Right port? `fdctl devices` shows vendor and serial number, not just paths.

**The board reboots when I connect.** Expected on Arduino/ESP32 — the auto-reset
circuit is wired to DTR/RTS. FieldDeck deasserts both *before* opening a port
precisely to avoid this, so if it still resets, something else opened the port
first (a `screen` you left running, ModemManager).

```bash
sudo systemctl mask ModemManager     # a common culprit on Debian
```

---

## Modbus: the device doesn't answer

- **Station address.** Off-by-one between documentation and wire format is
  endemic. Scan a small range: `fdctl modbus scan <device> --start 1 --end 16`.
- **Register offset.** "Holding register 40001" in a manual is usually address
  `0` on the wire. This trips everyone once.
- **Framing.** 9600 8E1 is common and is not the 8N1 default.
- **RS-485 direction control.** An adapter without automatic direction control
  needs RTS toggling and will look completely dead without it.
- **A/B swapped.** Silent, common, and gives exactly this symptom.

An **exception response** is much better news than silence: something is there
and it disagreed with you. `modbus.scan` reports present, absent and
exception-responded as three different findings.

---

## The panel is blank

**1. Does the framebuffer exist?**

```bash
ls -l /dev/fb*
```

`fb0` is HDMI; `fb1` is the SPI panel. **No `/dev/fb1` means the overlay did not
load** and nothing downstream can work.

```bash
grep -i "dtoverlay\|dtparam=spi" /boot/firmware/config.txt
dmesg | grep -i "fb\|spi"
```

The overlay name depends on your exact panel, and clones are inconsistent about
which they need. See [the Pi guide](raspberry-pi-setup.md#4-the-panel).

**2. Is the kiosk running?**

```bash
systemctl status fielddeck-kiosk --no-pager
journalctl -u fielddeck-kiosk -n 40 --no-pager
```

The installer *enables* it but does not *start* it, because starting it takes
over tty1.

**3. Did X find the right device?**

```bash
grep -iE "using input driver|no screens found|fbdev" /var/log/Xorg.0.log
```

---

## Touch is inverted / rotated / offset

Almost universal on a rotated panel. Experiment live, then persist:

```bash
DISPLAY=:0 xinput list
DISPLAY=:0 xinput set-prop <id> "Coordinate Transformation Matrix" 0 1 0 -1 0 1 0 0 1
```

Common matrices:

| Rotation | Matrix |
|---|---|
| none | `1 0 0 0 1 0 0 0 1` |
| 90° CW | `0 1 0 -1 0 1 0 0 1` |
| 180° | `-1 0 1 0 -1 1 0 0 1` |
| 270° CW | `0 -1 1 1 0 0 0 0 1` |

When one works, write it into `/etc/X11/xorg.conf.d/10-fielddeck-touch.conf`.
That file explains why it sets *options* rather than a `Driver` line, and what
to do if you genuinely need `evdev` rather than `libinput`.

---

## Dropped frames during capture

`CAPTURE_OVERFLOW` in the event log, or a frame count lower than the bus load
implies.

**1. The SD card.** By far the most likely, and it looks exactly like a bus
problem.

```bash
sudo hdparm -t /dev/mmcblk0        # a healthy A2 card does 20+ MB/s
dmesg | grep -i mmc                # I/O errors mean a dying card
```

**2. Power.** An under-volted Pi throttles.

```bash
vcgencmd get_throttled             # anything but 0x0 invalidates the session
```

`0x50000` means it *has* throttled since boot; `0x5` means it is throttling now.

**3. SPI CAN HAT throughput.** An MCP2515 on a Pi 3 will drop frames on a busy
500 kbit/s bus — it is an interrupt-rate limit, not a FieldDeck one. `ip
-statistics link show can0` shows kernel-level overruns, which are drops that
happened before FieldDeck ever saw the frame. Use a USB interface or an
MCP2518FD.

**4. Not a slow client.** Live subscriptions are bounded and lossy *on purpose*
so that a stuck UI cannot stall a capture; the session recorder is a lossless
sink. If the *UI* is missing events but the capture is complete, that is the
design working.

---

## The daemon won't start after a config change

```bash
journalctl -u instrumentd -n 40 --no-pager
```

It refuses rather than starting with a policy it could not parse, and the log
names the field. Validate before restarting:

```bash
sudo -u fielddeck /opt/fielddeck/venv/bin/instrumentd --config-dir /etc/fielddeck --log-text
```

A wildcard `remote.bind` is rejected outright — that is a validator, not a bug.

---

## `flash.program` fails

```bash
fdctl call flash.plan --json '{"device":"...","firmware":"app.bin"}'
```

`flash.plan` is PASSIVE and returns the **literal argument vector** that would
be executed plus the firmware's SHA-256. Run that command by hand: if `openocd`
fails the same way outside FieldDeck, the problem is the probe, the target or
the config file, not FieldDeck.

```bash
which openocd pyocd esptool.py avrdude dfu-util
lsusb                          # is the probe enumerated
ls -l /dev/bus/usb/*/*         # is it in the fielddeck group
```

---

## A bench instrument answers `*IDN?` but nothing else works

Almost certainly a SCPI dialect difference. **Every shipped profile has
`hardware_verified: false`** — they are readings of published programming
manuals, not measurements.

```bash
fdctl scpi query <device> '*IDN?'
fdctl scpi query <device> 'SYST:ERR?'      # the instrument's own complaint
```

`SYST:ERR?` is the fastest diagnosis: the instrument tells you what it did not
like.

Note that FieldDeck classifies compound SCPI by its most dangerous clause —
`OUTP ON;*IDN?` is a POWER command, not a query, and is refused as one.

Please [report what you find](https://github.com/saitokiku/field-deck/issues). A
profile that has actually been on a bench is worth ten that have been read
about.

---

## `fdctl` is slow to start

```bash
python3 -X importtime -c "import fielddeck" 2>&1 | tail -5
```

`fielddeck` uses lazy re-exports so importing it does not pull in `textual`,
`pyserial` or `python-can`. If something has broken that, this shows it.

---

## Two daemons

FieldDeck refuses to start a second daemon on a live socket, and says so. If you
see it complain, that is the protection working — an earlier instance is still
running.

```bash
systemctl status instrumentd --no-pager
ss -lx | grep fielddeck
```

Two processes each believing they own the hardware is the specific failure this
prevents: they disagree about whether an output is on, and an ESTOP reaches only
one of them.

---

## Timestamps are wrong / it thinks it's 1970

A Pi with no network and no RTC boots in 1970.

```bash
timedatectl
sudo apt install -y ntpdate && sudo ntpdate pool.ntp.org
```

Fit a DS3231 (~$3) for field use.

Note what this does *not* break: FieldDeck records a monotonic clock alongside
wall time, so ordering and intervals inside a session stay correct regardless.
A `CLOCK_STEPPED` event is recorded if the wall clock jumps more than a second.
Only the human-readable timestamps are wrong, and only until you fix the clock.

---

## Still stuck

Open an issue with:

```bash
fdctl --json status > status.json
fdctl --json devices > devices.json
sudo scripts/preflight.sh > preflight.txt 2>&1
journalctl -u instrumentd -n 200 --no-pager > daemon.log
```

Include the hardware: which Pi, which adapter or HAT, which panel, and what is
on the other end of the wire. "It doesn't work with my CAN HAT" and "it doesn't
work with a Waveshare 2-CH CAN FD HAT at 250 kbit/s on a Pi 4" are different
bug reports, and only one of them can be acted on.

Check the logs before posting them — they should not contain credentials
(FieldDeck redacts them structurally), but your device names and session notes
are yours.
