# Protocols

What each decoder does, and — more usefully — what it refuses to assume.

Every decoder here is PASSIVE: it reads bytes that already exist, from a live
stream or a stored capture, and produces a new artifact. Decoding never
transmits and never modifies a raw capture.

---

## CAN

`fielddeck/transports/socketcan.py`

Raw frames and CAN FD over SocketCAN. Captures are written in candump format,
which is deliberate: it is plain text, everybody's tooling reads it, and it
survives being emailed to someone who does not have FieldDeck.

```
(1787268692.043049) can0 101#01020304050607D8
(1787268692.052592) can0 101#02030507090B0DC3
```

### Listen-only, always, by default

Every interface FieldDeck opens is listen-only unless a human changed that at
the `ip link` level. In listen-only mode the controller does not even send ACK
bits.

That matters more than it sounds. On a two-node bus, a third node that ACKs can
mask the exact fault you are hunting. On a bus you have the wrong bitrate for, a
node that ACKs generates error frames and can bus-off the real participants.

**FieldDeck will not clear listen-only for you.** Ask it to transmit on a
listen-only interface and it tells you the command to run and why. Reconfiguring
someone's bus is an operator's act.

**FieldDeck never transmits to detect a bitrate.** See
[the Pi guide](raspberry-pi-setup.md#dont-guess-the-bitrate-by-transmitting)
for the listen-only sweep that does the job safely.

### Characterising a bus

```bash
fdctl can stats can0 --seconds 5
```

Per-arbitration-id rate, period and jitter. This is usually the fastest way to
understand an unknown bus: a 10 ms period with 0.2 ms jitter is a cyclic status
message; an ID appearing four times in five seconds with no pattern is an event.

### DBC decode

```bash
fdctl can decode <artifact> --dbc vehicle.dbc
```

Produces a derived artifact recording `source_artifact_ids`, `producer` and
`producer_config`. Signals the DBC does not describe are reported as
undecoded — not dropped, and not guessed at.

### Empty captures are deleted

If a capture recorded zero frames, FieldDeck removes the file and says so.

A zero-byte file in a session reads as *"we recorded, and there was nothing on
the bus"*, which is a completely different claim from *"the capture failed"*.
An empty file in the session is worse than no file.

---

## ISO-TP (ISO 15765-2)

`fielddeck/protocols/isotp.py`

Reassembles multi-frame CAN messages: single frames, first frame + consecutive
frames, flow control, block size and separation time.

```bash
fdctl call can.isotp --json '{"device":"can0","path":"can/can0-capture-0001.log"}'
```

Reports what it could not reassemble as explicitly as what it could. A gap in a
consecutive-frame sequence is a finding, not a rounding error — it usually means
dropped frames on your side or a real fault on theirs, and you need to know
which.

---

## UDS (ISO 14229)

`fielddeck/protocols/uds.py`

Decodes 27 UDS services from ISO-TP payloads, with request/response pairing and
negative-response-code interpretation.

```bash
fdctl call uds.services          # the catalogue, with FieldDeck's classification
fdctl call can.uds_decode --json '{"device":"can0","path":"..."}'
```

### Each service carries a FieldDeck permission class

This is the part worth knowing about. Every UDS service is annotated with what
it would require *if FieldDeck were the one sending it*:

| Class | Count | Examples |
|---|---|---|
| **QUERY** | 5 | `ReadDataByIdentifier`, `ReadDTCInformation` |
| **CONTROL** | 17 | `ECUReset`, `SecurityAccess`, `RoutineControl`, `WriteDataByIdentifier`, `InputOutputControlByIdentifier` |
| **FLASH** | 5 | `RequestDownload`, `TransferData`, `RequestTransferExit`, `RequestFileTransfer`, `WriteMemoryByAddress` |

So when you decode a capture of somebody else's diagnostic session, the output
tells you not just *what* they did but *how dangerous it was*. A trace
containing `0x34 RequestDownload` is a trace of a reflash, and that is worth
seeing at a glance.

Decoding a UDS trace is PASSIVE regardless — you are reading bytes that already
happened. The classification describes what sending it would cost.

---

## J1939 (SAE)

`fielddeck/protocols/j1939.py`

Decodes 29-bit identifiers into priority, PGN, source address and destination
address, with PGN naming for the common set, plus transport protocol (BAM and
RTS/CTS) reassembly.

```bash
fdctl call can.j1939 --json '{"device":"can0","path":"..."}'
```

An unknown PGN is reported as an unknown PGN with its number, not silently
dropped and not matched to the nearest known one.

---

## Modbus

`fielddeck/protocols/modbus.py`

RTU (serial) and TCP. Read/write coils, discrete inputs, holding registers and
input registers.

```bash
fdctl modbus read <device> --station 1 --kind holding --address 0 --count 10
fdctl modbus write <device> --station 1 --address 40 --value 1234   # CONTROL
fdctl modbus scan <device> --start 1 --end 32
```

### Reads are QUERY, not PASSIVE

A Modbus read is a transmission. You are putting a request on a bus, addressed
to a station, and if your assumption about what lives at that address is wrong,
"just reading" can change state. Classifying it as QUERY is an admission that
this is not free.

### Scanning

`modbus.scan` is bounded — you give it a range, it does not sweep 1–247 by
default — and it forces `retries=1`. Retrying into an address where nothing
lives just multiplies bus traffic while you wait for three timeouts instead of
one.

The scan reports a station as present, absent, or *responded with an exception*,
and those are three different findings. An exception response means something is
there and it disagreed with you, which is much more interesting than silence.

### Register interpretation

Modbus has no types. A holding register is 16 bits and the meaning is between
you and the vendor. FieldDeck shows you the raw registers and offers
interpretations — `uint16`, `int16`, `uint32`/`int32`/`float32` in both word
orders — and does not pick one for you.

Word order is the classic trap: `float32` across two registers is big-endian in
some devices, little-endian in others, and byte-swapped-within-word in a few.
`fdctl convert` exists partly for this.

---

## Serial framing

`fielddeck/analysis/framing.py`

Given a byte capture with arrival times, FieldDeck looks for structure:

- **Inter-byte gaps** — Modbus RTU's 3.5-character silence, or any protocol with
  an idle-time frame boundary
- **Repeating prefixes** — a sync byte or preamble
- **Length fields** — a byte at a fixed offset that predicts the distance to the
  next plausible boundary
- **Terminators** — CR, LF, CRLF, NUL
- **COBS and SLIP** framing

Each candidate comes with the evidence for it and a confidence, and none is
applied silently.

---

## Protocol identification

```bash
fdctl analyze --file capture.bin
fdctl call tools.identify_protocol --json '{"hex":"..."}'
```

Ranked hypotheses with confidence and the evidence for each. Verified: Modbus
RTU is identified at 0.92 confidence, and random bytes come back as:

```
unknown / insufficient evidence
```

along with what *would* settle it — a longer capture, arrival timing, or a known
message from the other direction.

This is the feature that most affects whether the tool is trustworthy. A tool
that always produces a confident answer will eventually produce a confident
wrong answer, and you will spend an afternoon on the wrong wire. FieldDeck would
rather say it does not know.

---

## CRC

`fielddeck/analysis/crc.py`

Twenty models, every one verified against its published check value:

```
crc5-usb
crc8  crc8-maxim  crc8-sae-j1850  crc8-autosar
crc16-arc  crc16-ccitt-false  crc16-dnp  crc16-kermit  crc16-maxim
crc16-mcrf4xx  crc16-modbus  crc16-profibus  crc16-t10-dif  crc16-usb  crc16-xmodem
crc32  crc32-bzip2  crc32-mpeg2  crc32c
```

Compute all twenty at once:

```bash
fdctl crc --hex 0103000A0002
```

Or work backwards from a trailer you observed:

```
$ fdctl crc --hex 0103000A0002 --expected E409
match_count  1
note         one catalogue model produces that trailer
```

It tries **both byte orders**, because a large fraction of protocols transmit
the low byte first, and "the CRC is wrong" is the wrong conclusion to draw from
a byte-order difference.

When nothing matches:

```
match_count  0
note         no catalogue model produces that trailer over these bytes
```

That is genuinely useful information. It usually means your frame boundaries are
wrong — you are CRC-ing the wrong span of bytes — rather than that the device
uses an exotic polynomial.

---

## Logic analysis

`fielddeck/capture/sigrok.py`

Any `sigrok`-supported analyzer, via `sigrok-cli` invoked with an argument
array. Capture, then decode I²C, SPI or UART.

```bash
fdctl call logic.devices
fdctl call logic.capture --json '{"device":"...","channels":8,"samplerate":"4MHz","seconds":2}'
fdctl call logic.decode --json '{"device":"...","protocol":"i2c"}'
```

Decode output is a derived artifact with provenance, like everything else.

---

## Adding a decoder

1. Put the decoding logic in `fielddeck/protocols/` or `fielddeck/analysis/`,
   as a pure function over bytes. No I/O, no device access.
2. Register actions in that subsystem's `actions.py` with `@action`. Decode
   actions are PASSIVE.
3. Add a simulated device in `fielddeck/sim/` that produces representative
   traffic, so the decoder is testable and demonstrable with no hardware.
4. Report what you could *not* decode as explicitly as what you could.
5. Add a test with real captured bytes in `tests/fixtures/`.

The fourth point is the one that gets skipped and matters most. A decoder that
silently drops what it does not understand will make a broken stream look
healthy, which is the worst thing a diagnostic tool can do.
