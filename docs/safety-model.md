# The safety model

FieldDeck's premise is that the operator does not fully know what is on the
other end of the wire, and that being wrong is expensive. Everything here
follows from that.

This document explains what each mechanism does *and why it is shaped that way*,
because a safety property you don't understand is one you will remove during a
refactor.

---

## The one-paragraph version

One process (`instrumentd`) owns all hardware. It boots with no authority.
Six permission classes are armed separately by a human, each with an expiry.
Sustained hazards additionally require a lease that must be renewed or the
output drops. Limits are enforced independently of authorization and cannot be
waived. An emergency stop latches, revokes everything, and drives every device
to its safe state. Every one of those decisions is an event on a timeline.

---

## 1. One authority

Nothing except `instrumentd` opens:

```
/dev/tty*   /dev/spidev*   /dev/i2c-*   /dev/usbtmc*   /sys/class/gpio
CAN interfaces   USB instruments   debug probes
```

The HMI, `fdctl`, recipes and the MCP server are **clients**, over a Unix
socket, subject to identical rules. There is no privileged client.

This is not about tidiness. It buys three things that are otherwise impossible:

- **A single place where safety is decided.** A rule enforced in four clients
  is a rule enforced in three clients and one you forgot.
- **A single place that knows the truth.** Two processes that both believe they
  own a PSU will disagree about whether the output is on, and an ESTOP will
  reach only one of them. (FieldDeck refuses to start a second daemon on a live
  socket for exactly this reason.)
- **One unprivileged process to secure.** `instrumentd` runs as the `fielddeck`
  user, not root. udev rules grant that user the specific device nodes it needs.
  A bug in an optional hardware library is then a `fielddeck`-user bug, not a
  root bug.

The systemd unit narrows it further: `DevicePolicy=closed` with an explicit
allow-list, `ProtectSystem=strict`, `NoNewPrivileges`, and
`RestrictAddressFamilies` limited to the socket families FieldDeck actually uses.

---

## 2. Six permission classes

| Class | Covers | Example |
|---|---|---|
| **PASSIVE** | Observation only. Nothing leaves the Pi. | `can.listen`, `serial.capture`, `tools.crc`, every `session.*` |
| **QUERY** | Reads that put a request on a bus. | `modbus.read_holding`, `scpi.query`, `psu.measure` |
| **CONTROL** | Changes state on a target. | `can.send`, `serial.send`, `modbus.write_register`, `debug.reset` |
| **POWER** | Energises something. | `psu.set`, `psu.output` |
| **FLASH** | Writes firmware. | `flash.program` |
| **DESTRUCTIVE** | Irreversible. | `flash.erase`, `recipe.run` |

Of the 80 actions FieldDeck registers, **58 are PASSIVE**. A cold-booted unit
can capture, decode, correlate, analyse and report without any authorization at
all. That is the intended default posture, not a degraded one.

### QUERY is not free

Reading a Modbus holding register is a *transmission*. On a bus with a
misconfigured slave, or an address that means something different than you
assume, a read can change state. Calling it QUERY rather than PASSIVE is a
deliberate admission that "just reading" puts energy on a wire.

### Why `recipe.run` is DESTRUCTIVE

It is the declared *maximum*: `recipe.run` can do anything a recipe contains.
The `permission_resolver` (below) narrows it per invocation to the highest class
the specific recipe actually uses. A recipe that only listens resolves to
PASSIVE.

---

## 3. Exact-class authorization

**A grant authorizes exactly its own class. Nothing inherits.**

```python
safety.arm(permission=PermissionLevel.DESTRUCTIVE, ttl_s=60, source=FDCTL)
safety.authorize(action="modbus.read_holding", permission=QUERY, ...)
# -> PermissionDenied
```

This surprises people, and it is the single most deliberate decision in the
design.

The alternative — a rank ladder where DESTRUCTIVE implies everything below it —
means that an operator who arms one dangerous thing has silently armed six. The
question "what is this unit currently allowed to do?" then has an answer you
must derive rather than read. With exact classes, the banner lists exactly the
classes armed, and that list is the complete answer.

`PermissionLevel` *does* have a `rank`, used for ordering severity in displays
and for computing "the most dangerous thing this recipe will do". It is never
used to decide authorization.

Arming several classes is one command:

```bash
fdctl arm control power --ttl 120 --note "bringing up the controller board"
```

### Grants

- Created **only** by `ClientSource.HMI` or `ClientSource.FDCTL`. Recipes and
  Claude cannot create them, and there is no RPC path by which they could.
- Always carry a **TTL**, clamped by `max_arm_ttl_s` in `safety.yaml`. Asking
  for an hour of POWER on a unit capped at 180 s gets you 180 s, not an error.
- **Scopable** to one device (`--device`) or one action (`--action`). A grant
  scoped to a device does not cover a global action.
- **Never persisted.** A daemon restart or a reboot returns the unit to SAFE.
  There is no on-disk state that could restore what was armed before the power
  went out. A reboot is always a return to safe, never a restoration.
- Revocable individually or all at once (`fdctl disarm`).

### Denied classes

```yaml
denied_permissions: [DESTRUCTIVE]
```

A class listed here is refused *before* the grant lookup and before the ESTOP
check. On a unit that should never erase a chip, this makes it impossible
rather than merely discouraged — and the operator can still see the action
exists, and read why it was refused.

---

## 4. Permission resolvers: narrowing, never widening

An action declares a maximum. A resolver may reduce it for a specific call:

```python
def _output_permission(params: PsuOutputParams) -> PermissionLevel:
    # Turning an output OFF is never a POWER operation.
    return PermissionLevel.POWER if params.enabled else PermissionLevel.PASSIVE
```

So `psu.output(enabled=False)` is PASSIVE, and it is also
`allowed_during_estop=True`. **Turning an output off must never be blocked by a
lapsed grant or a latched emergency stop.** A safety system that can prevent you
from making something safer has failed at its job.

Widening is a programming error, not a policy question — `effective_permission`
raises if a resolver returns a class above the declared maximum. Clients are
shown the declared maximum so they can plan for the worst case; a resolver that
could exceed it would make that number a lie.

---

## 5. Limits: independent, and unwaivable

Authorization and limits answer different questions:

> **Authorization:** may this client ask for this kind of thing right now?
> **Limits:** is this specific value acceptable on this unit at all?

```yaml
global_limits:
  psu.voltage: { quantity: psu.voltage, unit: V, maximum: 24.0 }
device_limits:
  "usb:1ab1:0e11":
    psu.voltage: { quantity: psu.voltage, unit: V, maximum: 5.5 }
```

Arming POWER lets you *ask* for 30 V. The 24 V limit decides the answer is no.
**No authorization waives a limit.** There is no override flag, no `--force`, no
DESTRUCTIVE-class escape hatch. To change a limit you edit root-owned config and
restart the daemon, which is a deliberate, auditable, two-step act.

Device limits intersect with global limits — the stricter of the two wins, so a
per-device entry can only tighten.

Derived limits cover quantities no single parameter expresses:

```python
DerivedLimitCheck(quantity="psu.power", params=("voltage", "current_limit"), op="mul")
```

24 V and 5 A may each be within limits while 120 W is not.

Limits are checked by the dispatcher, after authorization and before the handler
runs, and a rejection is published as `LIMIT_REJECTED` — visible in the timeline
exactly like a successful action.

---

## 6. Leases: a dead-man's handle

Some hazards are *sustained*. A frame you transmit is over in microseconds; an
energised rail stays energised. Actions with `requires_lease=True` acquire an
`OutputLease` for the duration.

A lease has a TTL and must be renewed. It ends — and the daemon drives the
device to its safe state — when:

- it **expires** without renewal,
- the client that holds it **disconnects** (including by crashing),
- the operator **releases** it,
- an **ESTOP** is engaged,
- the daemon **stops**.

```
$ fdctl psu output role:psu on --ttl 10 --hold 5
│ output   yes
│ lease    lease_id=lease-e0928408, expires_in_s=10,
│          renew_with=safety.lease_renew
warning: holding the output for 4s; Ctrl-C drops it immediately, and it
drops on its own when this command ends
```

The failure this prevents is specific and common: a UI that crashes, or an SSH
session that drops, while a supply is on. Without a lease, the rail stays up
until somebody walks over to the bench. With one, the daemon notices the socket
close and turns it off — through the same `safe_state()` code path everything
else uses, not a special case.

Renewing has two restrictions, because renewing is pulling the dead-man handle:

- **Only the holder may renew.** A lease says *this client* is still watching.
  A second client renewing it turns that into a statement about a third party,
  and a hung operator whose rail is held up by somebody else is exactly the
  failure the lease exists to prevent.
- **A renewal cannot lengthen the interval.** Renewing means "keep going for
  another interval", not "change the interval" — a client that could renew for
  an hour has replaced its dead-man handle with a timer. Shorter is always
  allowed; longer is clamped, and the lease reports the interval actually in
  force.

The safety loop reaps expired grants and leases every **250 ms**: fast enough
that a lapsed lease drops an output promptly, cheap enough to idle at ~0.09%
CPU.

An action that is still running when its device is driven to a safe state — by
an emergency stop, a lapsed lease, or shutdown — does not get to undo it. The
handler's effect is reverted and the call fails, because otherwise a slow
`psu.output` could finish after a stop and turn the rail back on.

---

## 7. Emergency stop

`fdctl estop`, `F9` on the panel, or the `estop` MCP tool — from any client, at
any time, including while an action is running.

It:

1. **Latches.** Not a momentary signal; a state.
2. **Revokes every grant** (each emitting `ARM_REVOKED`, so the timeline shows
   what authority was withdrawn, not just that something happened).
3. **Surrenders every lease.**
4. **Applies `safe_state()` to every device**, concurrently.

Concurrency matters. Sequentially, one wedged driver delays every device behind
it — with a 10 s timeout each, that is minutes before the last supply turns off.
Running them together, with a 5 s cap on the whole operation, de-energises the
bench in milliseconds even when drivers are hanging. (Measured: 6 ms to
de-energise with two drivers hanging forever.)

While latched, every action is refused **except** those declaring
`allowed_during_estop=True` — which are exactly the actions that move hardware
toward safety, plus status reads. You can always find out what happened, and you
can always turn something off.

Clearing requires an explicit human acknowledgement, and clearing is *not*
arming. After `fdctl estop clear` the unit is SAFE, with nothing armed. Whoever
cleared it has to decide, deliberately, to arm again.

---

## 8. The AI boundary

Claude connects to `instrumentd-ai.sock`, not `instrumentd.sock`.

- Every request from that socket is stamped `source=claude`, in the audit log,
  permanently. There is no way to connect as `claude` and be recorded as
  something else, or vice versa — the source is assigned by which socket the
  connection arrived on, not claimed by the client.
- `safety.arm`, `safety.disarm`, `safety.estop_clear` and `safety.lease_renew`
  are refused **at the transport**, before any handler sees the request.
  `safety.lease_release` is deliberately *not* on that list, for the same
  reason `estop` is available: releasing ends a hazard.
- `ClientSource.CLAUDE.may_create_grants` is `False`. Even if a request reached
  the safety manager, it would be refused there too.
- Of 29 MCP tools, **none arms anything**. `estop` is present: Claude can stop
  the bench and cannot start it.

Set `FIELDDECK_AI_GROUP` and run the MCP server as a user in only that group to
make the boundary kernel-enforced (socket permissions) rather than a matter of
how a client is configured.

The reasoning is simple: an assistant that can widen its own authority is not an
authorization system, it is a suggestion. See
[claude-integration.md](claude-integration.md).

---

## 9. Data integrity

- **Raw capture data is immutable.** Analysis writes *new* artifacts recording
  `source_artifact_ids`, `producer` and `producer_config`. Nothing rewrites
  what the wire said.
- **Every artifact carries a SHA-256** computed at creation.
- **Two clocks, always.** `monotonic_ns` for correlation, `utc_ns` for humans.
  A `TimeAnchor` projects between them. An NTP step or a wrong RTC changes what
  a timestamp *reads as*; it can never change the *ordering* or the *intervals*,
  because those come from the monotonic axis. A `ClockWatch` notices steps over
  1 s and records them.
- **Observations are labelled by author.** Assistant observations go in their
  own section of a session report. A theory must never be laundered into a
  measurement.
- **Empty captures are deleted, not kept.** A zero-frame file in a session reads
  as "we recorded and there was nothing", which is a different claim from "the
  capture failed". If nothing arrived, the session says so instead.

---

## 10. What FieldDeck refuses to assume

An error that says "I don't know" is worth more than a guess that is usually
right. FieldDeck will not:

- **Transmit to detect a bitrate.** Listen-only, always. Guessing by
  transmitting generates error frames on a bus you have the wrong rate for, and
  can bus-off real participants.
- **Clear listen-only for you.** If you ask it to transmit on a listen-only
  interface, it tells you the command to run and why. It does not reconfigure
  your bus.
- **Infer voltage levels, pinouts, RS-232-vs-TTL, RS-485 polarity, or CAN
  termination.** The `electrical` field on a serial preset stays `unknown`
  until a human says otherwise, and `unknown` is recorded in the session.
- **Probe a Modbus address range silently.** Scanning is a QUERY action with an
  explicit fault-report policy.
- **Decide a target is safe to energise.**
- **Have a theory when it doesn't.** `tools.identify_protocol` answers
  *"unknown / insufficient evidence"* and says what evidence would settle it. A
  tool that always has an answer will confidently point you at the wrong wire.

---

## 11. Where this is enforced

Every action goes through one pipeline. There is no bypass, in any client:

```
 1. Resolve action + device
 2. Validate parameters          (defaults applied here, so limits see real values)
 3. Compute effective permission (resolver may narrow, never widen)
    → ACTION_REQUESTED
 4. Authorize                    → PermissionDenied / EstopActive → ACTION_DENIED
 5. Check limits                 → LIMIT_REJECTED     (authorization cannot waive these)
 6. Acquire lease                → LEASE_ACQUIRED
 7. Acquire device lock
 8. Run handler with a timeout   → ACTION_STARTED / ACTION_COMPLETED / ACTION_FAILED
 9. Release lease
```

Steps 4 and 5 are separate on purpose, and in that order: you learn *"you are
not allowed to ask"* before *"and the answer would have been no anyway"*.

---

## 12. Auditability

Every decision is an event with `source`, `session_id`, `device_id`, `action`,
`permission`, `request_id` and both clocks. Refusals are as visible as
successes — `ACTION_DENIED` and `LIMIT_REJECTED` are on the timeline next to
`ACTION_COMPLETED`.

That matters because the interesting question after an incident is usually
*"what did we try that didn't work?"*, and a log that only records successes
cannot answer it.

```bash
fdctl events --limit 50
fdctl session events --limit 200
fdctl session window --around OUTPUT_ENABLED --before-ms 500 --after-ms 200
```

Credentials and API keys are never logged; the logger redacts extras
structurally rather than by pattern-matching message text.

---

## Testing this

The safety properties above are covered by `tests/safety/`, which runs against
a real in-process daemon with simulated drivers — the same dispatcher, the same
authorization pipeline, the same drivers. There is no test-only shortcut around
authorization. If a test can energise the simulated supply without arming POWER
first, that is a bug worth failing over.

```bash
make test              # everything
.venv/bin/pytest tests/safety -v
```

The exact-class rule in particular is pinned by a matrix test that walks every
`(granted, requested)` pair and asserts only the diagonal authorizes. If someone
"fixes" exact-class authorization into a rank ladder, that test fails first.
