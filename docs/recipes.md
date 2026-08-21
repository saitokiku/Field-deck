# Recipes

A recipe is a repeatable test procedure in YAML. It runs through the same
dispatcher, the same authorization pipeline and the same session recorder as a
human typing `fdctl`, so a recipe cannot do anything you could not do by hand —
and everything it does lands on the timeline.

Recipes live in `recipes/local/` (yours) and `recipes/examples/` (shipped).

---

## The rule that shapes everything

**A recipe cannot arm anything.**

`ClientSource.RECIPE.may_create_grants` is `False`. A recipe that needs POWER
fails at the step unless a human armed POWER first.

This is the difference between automation and delegation. A procedure that can
authorize itself is not a procedure — it is a program with root, wearing a
YAML costume.

So the workflow is always: **validate, dry-run, arm what it says, run.**

```bash
fdctl recipe validate controller-smoke-test
fdctl recipe dry-run controller-smoke-test
fdctl arm query power --ttl 300
fdctl recipe run controller-smoke-test
```

`dry-run` tells you exactly what to arm, and why:

```
would_start  no
reason       controller-smoke-test needs QUERY for psu.measure; POWER for
             psu.set, psu.output. A recipe cannot arm anything: an operator
             must run 'fdctl arm query power --ttl 60' first.
```

Note it names the *actions* that need each class, not just the classes. If
`psu.set` is in that list and you did not expect this recipe to change a
setpoint, stop and read it.

---

## A passive recipe

The whole of `recipes/examples/serial-identify.yaml`:

```yaml
version: 1
name: serial-identify
description: >-
  Set the local port framing, record two seconds of the incoming stream, and
  confirm the link is producing plausible traffic. Transmits nothing.

requires:
  devices:
    - id: sim:serial:sim-uart-0
      note: the adapter connected to the DUT

steps:
  - mark: serial-identify

  - action: serial.configure
    device: sim:serial:sim-uart-0
    baudrate: 115200
    bytesize: 8
    parity: "N"
    stopbits: 1

  - assert:
      expression: "serial.baudrate == 115200 and serial.framing == '8N1'"
      message: "The port did not take the requested framing"

  - action: serial.capture
    device: sim:serial:sim-uart-0
    duration_s: 2
    label: identify

  - assert:
      expression: "serial.bytes > 0"
      message: "Nothing arrived: wrong baud rate, wrong pins, or the DUT is silent"

  - assert:
      expression: "serial.chunks > 1"
      message: "A single burst is not a running link; expected periodic traffic"

  - note: >-
      Raw bytes and an arrival-time index are in the session. Check framing
      against the capture before concluding the baud rate is right.

finally:
  - mark: serial-identify-complete
```

This whole recipe is PASSIVE. It configures *this end* of the link and listens.
Adding one `serial.send` step would raise the entire recipe to CONTROL — which
is precisely why the passive pass is worth doing first.

---

## Step kinds

| | |
|---|---|
| `action` | Run a registered action. Any of the 80. |
| `assert` | Evaluate an expression against the namespace; fail the recipe if false. |
| `wait` | Sleep, up to 3600 s. |
| `mark` | Drop a labelled mark on the timeline. |
| `note` | Append a note to the session. |
| `repeat` | Run a block of steps *n* times (max 1000). |

### `action`

```yaml
- action: psu.set
  device: role:psu
  voltage: 5.0
  current_limit: 0.5
  store: rail          # bind the result into the namespace under this name
  timeout_s: 5
```

Use `role:psu` rather than a device id where you can. A recipe that names roles
survives replacing the instrument.

### `assert`

```yaml
- assert:
    expression: "psu.measured_v > 4.9 and psu.measured_v < 5.1"
    message: "Rail out of tolerance — check the load and the sense leads"
```

Write the `message` for the person who will read it at 2 a.m., not for yourself
today. "Assertion failed" tells them nothing; "wrong baud rate, wrong pins, or
the DUT is silent" tells them where to look next.

### `repeat`

```yaml
- repeat:
    count: 2
    steps:
      - action: can.stats
        device: sim:can:can0
        duration_s: 1
      - assert:
          expression: "can.total_frames > 0"
          message: "No frames observed; check bitrate, termination and wiring"
```

Two short samples rather than one long one, because *"the bus was alive for the
first sample and not the second"* is a different fault from *"the bus was quiet
throughout"*, and one long sample cannot tell them apart.

---

## Expressions

Assertion expressions are evaluated against a **namespace** built from the
results of previous steps, grouped by subsystem: `can.*`, `serial.*`, `psu.*`,
`modbus.*`, `session.*`, plus anything you bound with `store`.

They are **not** `eval`. The evaluator walks a Python AST against an explicit
allowlist of node types. Attribute access is a dict lookup, never `getattr`, so
`psu.__class__` is a missing key rather than a foothold. Numeric results are
bounded — `((9**32)**32)**32` is rejected rather than hanging the daemon for
forty seconds.

Available: comparisons, `and`/`or`/`not`, arithmetic, `in`, and a small set of
functions (`abs`, `min`, `max`, `len`, `round`, `any`, `all`).

Namespace keys are exactly what an action returned. Check with:

```bash
fdctl --json call can.stats device=can0 duration_s=1 | jq keys
```

Watch for near-miss keys. In `can-bringup` the example asserts on `can.count`,
not `can.frames`:

> `can.count`, not `can.frames`: the capture action returns only the first
> frames in its result, so `frames` counts what came back, while `count` counts
> what was recorded. For "was there any traffic at all" they agree; for a
> threshold they do not.

---

## `finally`

```yaml
finally:
  - action: psu.output
    device: role:psu
    enabled: false
  - mark: smoke-test-complete
```

`finally` runs when the recipe finishes, when a step fails, when an assertion
fails, and when the recipe is cancelled.

This is where you turn the rail off. Because `psu.output(enabled=false)` is
PASSIVE and `allowed_during_estop`, that step works even if the POWER grant
lapsed mid-recipe and even if someone hit the emergency stop.

(Verified with a live rail: the `finally` block runs and de-energises.)

---

## `requires` and `limits`

```yaml
requires:
  devices:
    - role: psu
      note: any supply that can do 5 V at 500 mA
    - id: sim:can:can0
      note: the interface under test, in listen-only mode

limits:
  voltage_max: 5.5
  current_max: 0.6
  power_max: 3.0
```

`requires` is checked before anything runs — a missing device fails validation,
not step 6.

`limits` are **recipe-local, and only ever tighter**. They intersect with the
unit's `safety.yaml`; a recipe cannot raise a ceiling. Declaring
`voltage_max: 5.5` in a recipe on a unit limited to 24 V means 5.5 V. Declaring
`voltage_max: 30` on that unit still means 24 V.

Write them anyway. A recipe that declares its own envelope documents what it
expects, and fails loudly on a unit configured for something else.

---

## Running

```bash
fdctl recipe list          # names, worst-case permission, step count, requirements
fdctl recipe validate <name>
fdctl recipe dry-run <name>
fdctl recipe run <name>
fdctl recipe cancel --run-id <run-id>
```

`validate` compiles and reports:

```
recipe                can-bringup
ok                    yes
steps                 10
finally_steps         1
max_permission        PASSIVE
permissions_required  -
missing_devices       -
state_changing_steps  0
estimated_duration_s  4.8
```

`state_changing_steps 0` is worth checking. If a recipe you believe is passive
reports anything other than zero, read it again before running it.

Every run gets a `run_id`, is recorded in the session, and can be cancelled.
Cancellation is cooperative — steps check for it — and `finally` still runs.

---

## `recipe.run` is declared DESTRUCTIVE

Because it can be: a recipe may contain anything. That is the declared
*maximum*.

A `permission_resolver` narrows it per invocation to the highest class the
specific recipe actually uses. `can-bringup` resolves to PASSIVE and needs no
authorization at all. `controller-smoke-test` resolves to POWER.

So the declared permission is honest about the worst case, and the enforced
permission is honest about this case.

---

## Writing a good one

**Listen before you talk.** Do the passive characterisation as its own recipe.
It costs nothing, needs no authorization, runs during an emergency stop, and
tells you whether the next step is reasonable.

**Assert the precondition, not just the result.** `can-bringup` asserts
`can.mode == 'listen-only'` before characterising, so it refuses to describe a
bus it might be driving.

**Make failure messages actionable.** The message is the deliverable when
something goes wrong.

**Put the teardown in `finally`, not at the end of `steps`.** Steps do not run
after a failure. That is the whole point of the distinction.

**Use `role:`, not device ids**, so the recipe survives new hardware.

**Keep secrets out.** Recipes are recorded into the session with their SHA-256.
Anything you write in one is in the record forever.

---

## Where they live

| | |
|---|---|
| `recipes/examples/` | Shipped. Read them; they are commented as documentation. |
| `recipes/local/` | Yours. Not tracked by git. |
| `/etc/fielddeck/recipes/` | System-wide on an installed unit. |

Every recipe is recorded into the session with its path, size and SHA-256, so a
report says exactly which version of a procedure produced a result.
