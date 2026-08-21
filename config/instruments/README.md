# `instruments/` — devices FieldDeck cannot find on its own

Installed as `/etc/fielddeck/instruments/`.

Discovery is passive: FieldDeck enumerates what the kernel already knows about
by reading sysfs, `/dev` and udev symlinks, and it transmits nothing to find
anything. That works for USB — a USBTMC scope, an FTDI adapter and a SocketCAN
interface all announce themselves — and it cannot work for two cases:

* **LAN instruments.** A supply at `10.0.0.5:5025` is only discoverable by
  sending something to it. FieldDeck will not scan your network to find test
  equipment, so you name it here.
* **Serial instruments.** A `/dev/ttyUSB0` might be a supply, a Modbus sensor,
  or a DUT's console. Asking it `*IDN?` to find out is a transmission into
  something that may be neither, so FieldDeck does not guess.

Everything in this directory is an operator saying *"this is what is on the
other end"*. That statement is trusted; the alternative is FieldDeck probing to
find out, which is exactly the thing it must not do.

## Two kinds of file

### `modbus.yaml` — Modbus endpoints

Read by name, from this directory. A missing file means "no Modbus endpoints".
A file that exists and does not parse is a **hard error that stops the daemon**:
quietly ignoring half a written endpoint list would leave you convinced you had
configured a bus you had not.

```yaml
endpoints:
  - name: flow-meter              # letters, digits, . _ - ; the device id is
                                  # built from this, so recipes keep working
                                  # when the IP or the tty number changes
    transport: rtu                # rtu | tcp
    label: Coriolis meter, cabinet 2
    serial_port: /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AB0KXYZ1-if00-port0
    baudrate: 19200
    parity: E                     # N | E | O
    stopbits: 1
    bytesize: 8
    default_slave: 1              # used when an action does not name a station
    timeout_s: 1.0

  - name: plc
    transport: tcp
    host: 10.0.0.40
    port: 502
    default_slave: 1
    timeout_s: 2.0
```

Prefer a `/dev/serial/by-id/...` path over `/dev/ttyUSB0`. The latter is
enumeration order, not identity: unplug two adapters and plug them back in the
other order and `ttyUSB0` is now the other device — with the same baud rate,
the same framing, and a completely different thing on the far end.

Duplicate `name` values are rejected. Two endpoints with one name means a
recipe referring to that name is ambiguous, and the daemon will not pick one.

### `*.yaml` — SCPI / VISA instrument declarations

Every other `.yaml`/`.yml` file in this directory is read as a list of
instrument declarations, either as a bare list or under an `instruments:` key.
Split them however suits you — one file per rack, one per bench, one big file.

Unlike `modbus.yaml`, a broken declaration file is **logged and skipped**, and
so is a single bad entry inside a good file. The daemon still starts with the
hardware it can see. That asymmetry is deliberate: nothing in here can widen
what an operator is allowed to do, so failing closed would cost availability
without buying safety. `safety.yaml` is the file that stops the daemon.

```yaml
instruments:
  - resource: TCPIP0::10.0.0.5::5025::SOCKET
    name: bench-psu
    profile: rigol.dp800          # optional; see the list below
    channel: 1
    timeout_s: 5.0
    note: Left rack, feeds the DUT 12 V rail

  - resource: ASRL/dev/serial/by-id/usb-Korad_KA3005P_0001-if00::INSTR
    name: aux-psu
    profile: korad.kaxxxxp
    # Some supplies want no terminator at all. "auto" derives it from the
    # resource class; "" means send nothing; or give the literal characters.
    write_termination: "\n"
    read_termination: auto

  - resource: USB0::0x1AB1::0x0588::DM3R000000000::INSTR
    name: bench-dmm
    profile: keysight.34461a
```

`resource` is a VISA resource name, and it is the only place a raw address is
accepted. Declarations may **pin a profile** but may never supply command text:
a field of raw SCPI strings would be a way around the typed actions the
permission model reasons about, so there isn't one.

Profile keys that ship with FieldDeck:

| key                | what it is                                    |
| ------------------ | --------------------------------------------- |
| `rigol.dp800`      | Rigol DP800-series programmable supply         |
| `rigol.dl3000`     | Rigol DL3000-series electronic load            |
| `siglent.spd`      | Siglent SPD-series supply                      |
| `siglent.sdl`      | Siglent SDL-series load                        |
| `keysight.34461a`  | Keysight 34461A DMM                            |
| `korad.kaxxxxp`    | Korad KA/KD-series supply (and its many clones)|
| `generic.scpi`     | Fallback: `*IDN?` and the common SCPI subset   |

Leave `profile` out and FieldDeck matches on the identity string the instrument
returns. Pin it when a clone reports something unhelpful, or when you want the
declaration to fail loudly rather than silently fall back to `generic.scpi`.

## Permissions

Files here are read by the daemon, which runs as `fielddeck`. Keep them
`root:fielddeck`, mode `0644` — the same as the rest of `/etc/fielddeck`. The
daemon reads its configuration; it does not write it.

```sh
sudo install -o root -g fielddeck -m 0644 my-bench.yaml /etc/fielddeck/instruments/
sudo systemctl restart instrumentd
fdctl devices                      # your declarations should now be listed
```

Nothing here is contacted at startup. A declared instrument that is switched off
appears as a device that is not ready, not as a boot failure — and FieldDeck
still does not talk to it until an action with the right permission asks it to.
