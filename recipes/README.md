# Recipes

A recipe is a YAML file describing a repeatable test: which instruments it
needs, what envelope it stays inside, what it does, and what must be true
afterwards. The full reference lives in [`docs/recipe-format.md`](../docs/recipe-format.md);
this is the short version.

```bash
fdctl recipe list                        # what is on this unit, and what each needs
fdctl recipe validate controller-smoke-test
fdctl recipe dry-run  controller-smoke-test   # would it run right now?
fdctl arm query power --ttl 120               # a recipe cannot arm anything
fdctl recipe run controller-smoke-test
```

`examples/` ships recipes that run against the simulated bench
(`FIELDDECK_SIM=1`); `local/` is yours and is not tracked. Recipes are also
loaded from `$FIELDDECK_RECIPES_DIR`, `<config>/recipes` and `<state>/recipes`.
A recipe outside those directories is refused rather than read.

## Shape

```yaml
version: 1
name: controller-smoke-test
description: what this proves, in one sentence

requires:
  devices:
    - role: psu           # bound at compile time; ambiguous roles are an error
    - id: sim:can:can0    # or a device id from `fdctl device list`

limits:                   # may only tighten safety.yaml, never widen it
  voltage_max: 24.5
  current_max: 1.0

steps:
  - action: psu.set       # any action `fdctl action list` shows
    device: role:psu
    voltage: 24.0         # parameters inline, or grouped under `params:`
    current_limit: 1.0

  - wait: 2                              # settle time, in seconds
  - mark: power-up                       # a label on the unified timeline
  - note: "clip the probe to TP4"        # free text in the session record
  - assert:
      expression: "can.frames > 0"
      message: "No CAN traffic observed" # shown when it fails
  - repeat:
      count: 3
      steps: [...]

finally:                  # runs whatever happened above
  - action: psu.output
    device: role:psu
    enabled: false
```

`- action: wait` with `seconds:` and `- action: assert` with `expression:` are
accepted as well; they mean the same thing.

Parameters are literals. There is no templating, which is what lets the
compiler check every voltage in the file before anything runs.

## Assertions

Assertions read a namespace built from earlier step results, filed under the
action's prefix: `can.capture` fills `can`, `psu.measure` fills `psu`. The most
recent value of each field wins.

The namespace holds **scalars**. Numbers, text and booleans pass through
untouched; anything with a size — a frame list, a byte payload — binds to that
size, so `can.frames > 0` asks whether any frames arrived. Watch for actions
that truncate bulk data in their result: `can.capture` returns only the first
frames, so `can.frames` is what came back and `can.count` is what was recorded.
Use the explicit count for a threshold.

Expressions are not Python. They are parsed and checked against an allowlist —
comparisons, boolean operators, arithmetic, and the functions `len`, `abs`,
`min`, `max`, `round`, `any`, `all` — and anything else is rejected when the
recipe compiles. Attribute access is a lookup in the results, never on a Python
object. A recipe from a stranger cannot reach the machine through an assertion.

## What the engine promises

* **Nothing physical happens until the whole file has been checked.** Missing
  devices, unknown actions, bad parameters, a setpoint above the effective
  limit, an unparseable assertion: all of them fail compilation, with the DUT
  untouched. A recipe never dies halfway through with a rail still energised.
* **The permission a recipe asks for is the most dangerous thing it will
  actually do**, computed per call — `psu.output(enabled: false)` is PASSIVE,
  because turning an output *off* is never the dangerous direction.
* **A recipe has no authority of its own.** It cannot arm anything. An operator
  arms what it needs first, and authorization is exact-class: reading an
  instrument is QUERY, energising it is POWER, and a POWER grant does not cover
  the QUERY step next to it. If something is missing, the run says so before
  step one rather than failing partway through.
* **`finally` runs** on success, assertion failure, error, timeout, cancellation,
  device loss and emergency stop. It is the tidy path, not the guarantee: the
  guarantee is the daemon, which drives every output to its safe state if the
  run dies or its lease lapses.
* **Every run leaves evidence.** A session is opened if none is active, and the
  run's steps, assertions and captures land on the unified timeline.

## States

`PENDING` → `PREFLIGHT` → `RUNNING` → `PASSED` | `FAILED` | `ABORTED`, with
`CANCELLING` in between when a run is stopping. `FAILED` is a test result — a
step errored or an assertion was false. `ABORTED` means the run was interrupted
(cancelled, emergency stop, deadline, lost device) and proved nothing either way.
