# Using FieldDeck

Task-oriented. For what the permission classes *mean*, read
[safety-model.md](safety-model.md) first.

Everything below works with no hardware attached — start a simulated daemon and
follow along:

```bash
instrumentd --simulate --log-text
```

---

## The two rules that save the most time

1. **Start a session before you start probing.** Everything after that is
   timestamped on one clock and reconstructable months later. A capture you
   didn't record is a test you have to repeat.
2. **`--json` works on every command.** The pretty output is for you; the JSON
   is for scripts, and it is the same data.

---

## Orientation

```bash
fdctl status        # safety state, session, devices, work in flight
fdctl devices       # everything instrumentd knows about
fdctl device <id>   # descriptor and driver status for one
fdctl actions       # every action, and the permission it needs
fdctl limits        # the safety policy this unit actually loaded
fdctl watch         # a compact live status line, for a second terminal
```

`fdctl status` on a fresh simulated unit:

```
╭─ FieldDeck ──────────────────────────────────────────────────────────────╮
│ state    SAFE                                                            │
│ daemon   fielddeck 0.1.0  [SIMULATED - no hardware is attached]  up 7s   │
│ utc      2026-08-21T01:34:26.303337Z                                     │
│ session  none - start one with: fdctl session start "<name>"             │
│ storage  /var/lib/fielddeck/sessions                                     │
╰──────────────────────────────────────────────────────────────────────────╯
armed:    nothing (PASSIVE only)
```

`fdctl limits` is the one to read on a unit you did not set up yourself. It is
the answer to *"what will this thing refuse to do"*, and it is better to know
before you are holding probes.

### Naming a device

Three ways, all interchangeable:

```bash
fdctl can status sim:can:can0        # the full device id
fdctl psu measure role:psu           # by role — psu, dmm, bus, analyzer, camera
fdctl serial status uart0            # an alias from fielddeck.yaml
```

`role:` is what you want in recipes and scripts: it survives replacing the
instrument.

---

## Sessions

```bash
fdctl session start "brownout on the rev-C board"
fdctl session mark "swapped the connector"
fdctl session note "customer says it only happens cold"
fdctl session stop
```

Then, later:

```bash
fdctl session list
fdctl session show <id>
fdctl session events --limit 200
fdctl session summary
fdctl session report            # deterministic Markdown
```

### The correlation query

This is the feature the rest of the architecture exists to support:

```bash
fdctl session window --around OUTPUT_ENABLED --before-ms 500 --after-ms 200
```

*"Everything that happened, on every subsystem, in that window."* The PSU
current reading, the CAN frame, the UART byte and the operator's mark are all on
one monotonic axis, so *"what was the supply doing 300 ms before the frames
stopped?"* is a query rather than an exercise in comparing log files.

Centre on an exact instant instead with `--center-ns`.

### Try it

```bash
FIELDDECK_SIM_FAULT=1 instrumentd --simulate --log-text
```

The simulated board now browns out 1.4 s after you energise it, its CAN
heartbeat stops 312 ms after that, and the UART reports error 0x17 7 ms later —
one causal story across three buses, which is exactly what the timeline is for.

```bash
fdctl session start "sim-fault"
fdctl arm power --ttl 120
fdctl can capture sim:can:can0 --seconds 6 &
fdctl psu output role:psu on --ttl 10 --hold 5
fdctl session window --around OUTPUT_ENABLED --before-ms 200 --after-ms 3000
```

---

## CAN

```bash
fdctl can interfaces
fdctl can status can0             # config and error counters
fdctl can listen can0 --seconds 5 # to the terminal, nothing recorded
fdctl can capture can0 --seconds 30
fdctl can stats can0              # per-id rate, period, jitter
```

`can stats` is usually the fastest way to characterise an unknown bus: it tells
you which arbitration IDs exist, how often each appears, and how much the period
jitters. An ID with 10 ms period and 0.2 ms jitter is a cyclic status message; a
sporadic one is an event.

Capture writes an immutable candump-format artifact with a SHA-256:

```
│ relative_path  can/can0-capture-0001.log
│ sha256         e6a69b3ee939892673c91d40cf7260c943cef996039f5f4cd8f55faead5c2fa1
│ metadata       frames=630, bitrate=500000
```

### Decoding

```bash
fdctl can decode <capture-artifact> --dbc /path/to/vehicle.dbc
fdctl call can.isotp --json '{"device":"can0","path":"can/can0-capture-0001.log"}'
fdctl call can.uds_decode ...
fdctl call can.j1939 ...
```

Decoding produces a **new** artifact recording which artifact it came from and
what produced it. The raw capture is never modified.

### Transmitting

```bash
fdctl arm control --ttl 60
fdctl can send can0 --id 0x123 --data 0102030405060708
```

If the interface is listen-only, FieldDeck refuses and tells you the exact `ip
link` command to change that. It does not reconfigure your bus for you — see
[the CAN section of the Pi guide](raspberry-pi-setup.md#5-bring-up-a-can-interface).

---

## Serial

```bash
fdctl serial list
fdctl serial configure ttyUSB0 --baud 115200 --parity N --stopbits 1 --electrical rs232
fdctl serial status ttyUSB0
fdctl serial monitor ttyUSB0 --seconds 10
fdctl serial capture ttyUSB0 --seconds 60
```

`--electrical` records what is physically on the wire — `ttl`, `rs232`, `rs485`
or `unknown`. FieldDeck never infers it, and `unknown` is what it stays until
you say. It goes into the session, so a capture you open in six months still
says what the adapter was.

Capture is **byte-exact**: all 256 byte values, embedded NULs, CRLF and invalid
UTF-8 survive a round trip unchanged. Verified against a pty.

FieldDeck deasserts DTR and RTS *before* opening a port, because on an Arduino
or ESP32 the auto-reset circuit is wired to them — opening a port the naive way
reboots the board you were trying to observe.

Transmitting needs CONTROL:

```bash
fdctl arm control --ttl 60
fdctl serial send ttyUSB0 --hex 010300000002
fdctl serial send ttyUSB0 --text 'AT+GMR' --newline
```

### Unknown baud rate

```bash
fdctl serial capture ttyUSB0 --seconds 5
fdctl analyze --path serial/ttyUSB0-capture-0001.bin
```

`analyze` looks at bit-time distribution, framing-error patterns and byte-value
entropy and returns ranked hypotheses with confidence. It answers *"unknown /
insufficient evidence"* when the evidence isn't there, and says what would
settle it.

---

## Modbus

```bash
fdctl modbus read <device> --station 1 --kind holding --address 0 --count 10
fdctl modbus scan <device> --start 1 --end 32
```

Reads are **QUERY**, not PASSIVE: a read puts a request on the wire, and on a bus
with a misconfigured slave that is not free.

Writes are CONTROL:

```bash
fdctl arm control --ttl 60
fdctl modbus write <device> --station 1 --address 40 --value 1234
```

`modbus scan` is bounded, and FieldDeck forces `retries=1` on scans — retrying
into an address that isn't there just multiplies bus traffic while you wait.

---

## Bench instruments

```bash
fdctl bench list
fdctl bench identify <device>
fdctl scpi query <device> '*IDN?'          # QUERY
```

Supplies:

```bash
fdctl arm power --ttl 180
fdctl psu set role:psu --voltage 5.0 --current-limit 0.5
fdctl psu output role:psu on --ttl 10 --hold 30
fdctl psu measure role:psu                 # QUERY
```

Two things worth internalising:

**Setting a voltage does not turn the output on.** `psu.set` and `psu.output`
are separate actions on purpose.

**An energised output holds a lease.** `--ttl` is the dead-man interval; if the
client stops renewing — or crashes, or its SSH session drops — the daemon turns
the output off. `--hold` renews for you for a bounded time, and Ctrl-C drops it
immediately.

Turning an output **off** is PASSIVE and allowed during an emergency stop. You
can always make something safer.

> Every shipped instrument profile has `hardware_verified: false`. Check a
> setpoint with a meter before trusting it with something expensive.

---

## Analysis tools

These are all PASSIVE and work without a daemon connection where they can.

```bash
fdctl convert 0x1A2B          # every plausible reading of one value
fdctl crc --hex 0103000A0002  # all 20 CRC models at once
fdctl hash --file firmware.bin
fdctl analyze --file capture.bin
```

### Which CRC is this?

Give it the trailer and it tells you which model produced it:

```
$ fdctl crc --hex 0103000A0002 --expected E409
expected     E409
match_count  1
note         one catalogue model produces that trailer
```

It tries both byte orders, because half the protocols in the world transmit the
low byte first. When nothing matches it says so plainly:

```
match_count  0
note         no catalogue model produces that trailer over these bytes
```

That is a useful answer: it means your framing is wrong, not your CRC.

### `convert`

```
$ fdctl convert 0x1A2B
parsed_as  hexadecimal literal, hex byte string
ambiguous  no
count      31
  integer (hexadecimal literal)   decimal                      6699
  as 16-bit                       signed (two's complement)    6699
  as 16-bit                       bytes, little-endian         2B1A
  as 16-bit                       IEEE-754 float16 bit pattern 0.0030117
  ...
```

Thirty-one readings of one value, and an explicit `ambiguous` flag when the
input could be parsed more than one way. This exists because the actual question
at 2 a.m. is *"is this little-endian, or is it a float, or did I read the
datasheet wrong?"* and the answer is usually visible if you can see all the
readings at once.

---

## Recipes

```bash
fdctl recipe list        # and the worst thing each one would need
fdctl recipe validate <name>
fdctl recipe dry-run <name>    # would this run right now?
fdctl arm power control --ttl 300
fdctl recipe run <name>
```

Always `dry-run` first. It reports which devices a recipe needs, which
permission classes, and which limits it would bump into — without executing a
step. Then arm exactly those classes. See [recipes.md](recipes.md).

---

## Flashing

```bash
fdctl call flash.plan --json '{"device":"...","firmware":"app.bin"}'
```

`flash.plan` is **PASSIVE**. It returns the literal argument vector that would be
executed and the firmware's SHA-256, so you read the exact command before
authorizing anything.

```bash
fdctl arm flash --ttl 300
fdctl call flash.program ...
fdctl call flash.verify ...          # QUERY
```

`flash.erase` requires **DESTRUCTIVE** *and* a `confirm` parameter naming the
target. Two independent acts, because there is no undo.

---

## Emergency stop

```bash
fdctl estop                    # latches immediately
fdctl estop clear              # explicit human acknowledgement
```

`F9` on the panel. Available from any client at any time, including while an
action is running. It revokes every grant, surrenders every lease, and drives
every device to its safe state concurrently.

Clearing is not arming. After clearing, the unit is SAFE with nothing armed.

---

## Live events

```bash
fdctl events                   # follow the stream
fdctl events --limit 50        # the most recent
fdctl watch                    # a one-line status, for a second terminal
```

Refusals appear here too. `ACTION_DENIED` and `LIMIT_REJECTED` sit next to
`ACTION_COMPLETED`, because after an incident the interesting question is
usually *"what did we try that didn't work?"*

---

## The panel

Four tmux windows, on the panel or over SSH via `fielddeck-session`:

| | |
|---|---|
| **1 HMI** | The console |
| **2 CLAUDE** | An assistant session, if you want one |
| **3 SHELL** | A plain shell |
| **4 LOG** | `journalctl -fu instrumentd` |

The HMI is a client like any other. It shows actions taken by *other* clients
live — run `fdctl arm power` over SSH and the panel banner changes. If the
daemon dies, the HMI shows a disconnected banner rather than exiting, and
reconnects when the daemon returns.

Glyphs carry meaning so it reads on a monochrome panel:

| | |
|---|---|
| `✓` | present and working |
| `○` | present, inactive (an output that is off) |
| `●` | active (recording, energised) |
| `!` | warning |
| `×` | error or absent |
| `?` | unknown — FieldDeck will not guess |

---

## Scripting

```bash
fdctl --json status | jq -r '.safety.state'
fdctl --json can stats can0 | jq '.ids[] | select(.jitter_ms > 1)'
fdctl --json session list | jq -r '.[0].id'
```

`-y` skips confirmation prompts. `--timeout` bounds how long to wait for the
daemon.

Note that arming from a script is still arming: the grant is stamped
`source=fdctl` and audited, and it still expires. A script cannot obtain
authority a human could not.
