# Architecture

```
                     ┌──────────────────────────────────────────┐
   HMI (Textual) ───▶│                                          │
   fdctl (CLI)   ───▶│  instrumentd.sock       ┌─────────────┐  │
   recipes       ───▶│                         │   safety    │  │
                     │                         │  manager    │  │
   Claude (MCP)  ───▶│  instrumentd-ai.sock    └──────┬──────┘  │
                     │   (source=claude,              │         │
                     │    cannot arm)                 ▼         │
                     │  ┌──────────┐  ┌────────────────────┐    │
                     │  │   RPC    │─▶│    dispatcher      │    │
                     │  │  server  │  │ validate→authorize │    │
                     │  └──────────┘  │ →limits→lease→run  │    │
                     │                └─────────┬──────────┘    │
                     │  ┌──────────┐            ▼               │
                     │  │  event   │◀── ┌───────────────┐       │
                     │  │   bus    │    │    drivers    │       │
                     │  └────┬─────┘    └───────┬───────┘       │
                     │       │                  │               │
                     │  ┌────▼─────┐            │  instrumentd  │
                     │  │ sessions │            │               │
                     │  │ timeline │            │               │
                     │  └──────────┘            │               │
                     └──────────────────────────┼───────────────┘
                                                ▼
                              serial · CAN · I²C · SPI · GPIO
                              USBTMC · LXI · debug probes
```

---

## The daemon

`fielddeck/daemon/service.py` — `InstrumentDaemon` wires everything together
and owns the lifecycle.

Startup order is load-bearing:

```python
self.safety.reset()                                   # no grants, no leases
await self.discover()                                 # passive inventory only
await self.dispatcher.apply_safe_state(...)           # before any client can connect
await self.rpc.start()                                # now accept connections
self._safety_task = asyncio.create_task(...)          # reap expired grants/leases
```

The bench is driven to a known-safe state **before** the socket exists. A client
cannot observe, or race, a window in which the daemon is up but the hardware
state is unknown.

Discovery is passive: it enumerates what is present without opening, energising
or interrogating anything. A device is found, not probed.

### The safety loop

Every 250 ms: reap expired grants and leases, apply safe state to anything whose
lease lapsed, check for clock steps, and every 20 ticks check free storage. This
is the mechanism behind "a lapsed lease drops the output" — it is a loop, not a
timer per lease, so it stays correct when hundreds of leases exist and costs
about 0.09% of a Pi's CPU when none do.

---

## Transport: newline-delimited JSON over a Unix socket

`fielddeck/daemon/protocol.py`, `rpc.py`, `client.py`.

One JSON document per line. Versioned (`RPC_PROTOCOL_VERSION`), negotiated in a
`hello` exchange. Requests carry a `request_id`; responses and events reference
it.

Deliberate choices, each from a specific failure:

| | |
|---|---|
| **Unix socket, never TCP** | Filesystem permissions are the access control. There is no port to accidentally expose, and the config model *rejects* a wildcard bind for the optional remote block. |
| **Two sockets** | `instrumentd.sock` (full) and `instrumentd-ai.sock` (stamps `source=claude`, refuses arming at the transport). The security boundary is which socket you connected to, not what you claim to be. |
| **4 MB line limit** | asyncio's default `StreamReader` limit is 64 KB. A capture of ~800 CAN frames exceeds that, and the symptom is the client's read loop dying rather than an error you can act on. |
| **32 in-flight requests per connection** | A request is a task, so a 3-second capture does not block a status poll on the same connection. Measured: 20 concurrent status polls at 0.40 ms p50 during a running capture. |
| **Liveness probe before unlinking** | Starting a second daemon used to silently steal the socket, leaving two processes each believing they owned the hardware — and an ESTOP reaching only one. The server now connects to an existing socket before removing it, and refuses to start if something answers. |

---

## Dispatcher

`fielddeck/daemon/dispatcher.py`. Every action from every client goes through
`_execute`, in this order:

```
1. Resolve action + device      registry.lookup()
2. Validate parameters          pydantic; defaults applied here
3. Effective permission         spec.effective_permission(params)   → ACTION_REQUESTED
4. Authorize                    safety.authorize()                  → ACTION_DENIED
5. Limits                       limits.check_params/check_derived   → LIMIT_REJECTED
6. Lease                        _take_lease()                       → LEASE_ACQUIRED
7. Device lock                  one action per device at a time
8. Run handler                  with timeout + cancellation         → ACTION_STARTED
9. Release leases                                                   → ACTION_COMPLETED
```

Parameters are validated *before* limits are checked so that limits see the
values the driver will actually receive, defaults included. A limit checked
against `None` is a limit that isn't checked.

On failure the lease is abandoned rather than released — the difference decides
whether the device is driven to safe state or merely let go.

---

## Drivers

`fielddeck/drivers/base.py`. A driver is a class with a `DeviceDescriptor` and
some `@action`-decorated methods:

```python
class SimPsuDriver(Driver):
    @action(
        "psu.output",
        permission=PermissionLevel.POWER,
        state_changing=True,
        requires_lease=True,
        permission_resolver=_output_permission,   # PASSIVE when disabling
        allowed_during_estop=True,                # so you can always turn it off
        safe_state_note="output disabled",
    )
    async def output(self, params: PsuOutputParams, ctx: ActionContext) -> dict:
        ...
```

`collect_actions()` gathers them by introspection at registration. Metadata
lives next to the code it describes, so a handler and its permission class
cannot drift apart in review.

Every driver implements `probe`, `connect`, `disconnect`, `status` and
`safe_state`. `safe_state()` is the contract that makes ESTOP, lease expiry,
client death and daemon shutdown all one code path rather than four.

`ActionContext` gives handlers `remaining_s`, `cancelled` and
`raise_if_cancelled()` — a long capture is expected to check.

### Simulation is not a separate path

`fielddeck/sim/` drivers implement the same `Driver` ABC, register through the
same `@action` decorator, and are dispatched through the same pipeline. There is
no fake-data mode in the UI and no bypass of authorization.

This is a testability decision *and* a safety one: the code path you exercise
with no hardware attached is the code path that runs with hardware attached. A
test suite that proves things about a simulator proves nothing unless the
simulator is wired in the same place.

The simulated action set is checked to be a strict subset of the real one, per
subsystem, so a simulation cannot offer something real hardware doesn't.

---

## Event bus

`fielddeck/daemon/events.py`. Every state change is an `Event` with `source`,
`session_id`, `device_id`, `action`, `permission`, `request_id`, both clocks and
a payload.

Two consumer kinds, and the distinction is the point:

- **Lossless sinks** — the session recorder. Never drops anything.
- **Lossy bounded subscriptions** — live UI feeds. Bounded queues; a slow
  consumer drops events and is told it did (`CAPTURE_OVERFLOW`).

**A slow client must never stall a capture.** A UI stuck on a redraw, an SSH
session over a bad link, a Claude process paused by the scheduler — none of them
may cost you frames. Verified: a deliberately dead-weight subscriber cost a
running capture zero frames.

---

## Sessions and the timeline

`fielddeck/capture/`.

A session is a directory: metadata, an append-only event log (zstd, gzip
fallback), and artifacts. Artifacts record `kind`, `media_type`, `size_bytes`,
`sha256`, both clocks, `device_id`, and — for derived artifacts —
`source_artifact_ids`, `producer`, `producer_version`, `producer_config`.

The **timeline** merges every subsystem onto one monotonic axis. That is what
makes the correlation query possible:

```bash
fdctl session window --around OUTPUT_ENABLED --before-ms 500 --after-ms 200
```

*"What was the supply doing 300 ms before the CAN frames stopped?"* — answerable
because the PSU measurement and the CAN frame were stamped from the same clock
source, in the same process, at the moment they happened.

Writes are batched (64 events, or 1 second, whichever comes first). The age
bound is not an optimisation: without it, a `SIGKILL` mid-session lost every
event in a partial batch. With it, a `SIGKILL` costs at most a second.

### Two clocks

`fielddeck/common/timebase.py`. `monotonic_ns` for correlation and intervals;
`utc_ns` for humans. A `TimeAnchor` projects between them, and `ClockWatch`
notices steps over 1 s and records `CLOCK_STEPPED`.

An NTP correction or a wrong RTC changes what a timestamp *reads as*. It can
never change ordering or intervals, because those come from the monotonic axis.
On a field unit with no network — which boots in 1970 — this is the difference
between a usable capture and a worthless one.

---

## Analysis

`fielddeck/analysis/` — CRC (20 models, reverse lookup), framing detection,
COBS/SLIP, entropy, timing histograms, protocol identification.

`fielddeck/protocols/` — Modbus RTU/TCP, ISO-TP (ISO 15765-2) reassembly, UDS
(ISO 14229) with FieldDeck permission classification per service, J1939 PGN
decode.

Analysis never mutates a raw artifact. It reads one and writes another,
recording what produced it and with what configuration, so a conclusion can be
traced back to the bytes that support it and reproduced.

`tools.identify_protocol` returns ranked hypotheses with confidence and the
evidence for each, and returns *"unknown / insufficient evidence"* when that is
the honest answer. (Verified: 0.92 confidence on Modbus RTU, and "unknown" for
random bytes.)

---

## Recipes

`fielddeck/recipes/` — YAML procedures, compiled and validated before anything
runs.

- **Compiled ahead of execution**, so `recipe.validate` and `recipe.dry_run`
  report what a recipe *would* do and what it would need, without doing it.
- **Expressions use an AST allowlist**, never `eval`. Attribute access is a dict
  lookup, never `getattr`. Numeric results are bounded (`MAX_RESULT_BITS`), so
  `((9**32)**32)**32` is rejected rather than hanging the daemon.
- **Recipes cannot create grants.** A recipe requiring POWER fails at a step
  unless a human armed POWER first. `recipe.dry_run` tells you which classes it
  will need, so you can arm exactly those.
- **Cleanup runs.** A recipe's `finally` block executes even when a step fails —
  verified with a live rail, which is when it matters.

See [recipes.md](recipes.md).

---

## Clients

| | |
|---|---|
| **`fdctl`** | Typer + Rich. Every command is an RPC. `--json` on any command for scripting. |
| **HMI** | Textual, laid out for 80×25. Glyphs (`✓ ○ ● ! × ? → ←`) carry meaning so it reads on a monochrome panel. Tracks other clients' actions live via the event bus, and survives the daemon dying — it shows a disconnected banner rather than exiting. |
| **MCP server** | `fielddeck/mcp/`. Protocol `2025-06-18`, stdio, implemented natively — no SDK dependency. 29 tools, none of which arm anything. |

All three are ordinary clients. None has a privileged path.

---

## Process execution

`fielddeck/common/process.py`. External tools (`sigrok-cli`, `openocd`,
`esptool`, `candump`, `tcpdump`) are invoked with **argument arrays**, never
shell strings. There is no arbitrary shell execution reachable through RPC or
MCP.

`flash.plan` is a PASSIVE action that returns the literal argument vector that
*would* be executed, plus the firmware's SHA-256, so you can read the exact
command before authorizing FLASH.

---

## Layout

```
fielddeck/
  common/       models, config, events, errors, timebase, logging, paths, process
  daemon/       service, rpc, protocol, client, dispatcher, registry, core_actions
  safety/       manager, arm, estop, leases, limits
  drivers/      the Driver ABC and the @action decorator
  transports/   serial, socketcan, i2c, spi, gpio, network
  protocols/    modbus, isotp, uds, j1939
  bench/        scpi, visa, instrument profiles
  capture/      sessions, recorder, storage, timeline, report, sigrok, camera
  analysis/     crc, framing, timing, autodetect, convert
  debug/        probes, flash, firmware
  recipes/      schema, compiler, runner, assertions
  discovery/    passive Linux device enumeration
  sim/          simulated devices (same Driver ABC, same pipeline)
  ui/           Textual HMI
  cli/          fdctl
  mcp/          MCP server and tools
```

`fielddeck/__init__.py` is fifteen lines and re-exports **nothing** — just a
version and the protocol number. Importing `fielddeck` therefore pulls in no
`textual`, no `pyserial`, no `python-can`; every subsystem is imported by the
module that needs it, and the optional hardware stacks are imported inside the
functions that use them.

That is why `fdctl status` does not pay for the HMI, and why the package
imports cleanly on a machine with none of the hardware extras installed. CI
asserts it: a job fails the build if importing `fielddeck` pulls in any of the
heavy optional modules.
