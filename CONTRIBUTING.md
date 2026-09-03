# Contributing to FieldDeck

Thank you for considering it. This document is short because most of what
matters is one idea, and the rest follows from it.

---

## The one idea

**FieldDeck is a tool people point at hardware they do not fully understand,
often in a hurry, sometimes when something expensive is already broken.**

So the bar for a change is not "does it work". It is:

- Does it fail safely when an assumption is wrong?
- Does it say what it does not know, instead of guessing?
- Can an operator tell, from the output, what actually happened?

A feature that works when everything is as expected, and does something
surprising when it isn't, is a net negative here.

---

## Before you write code

**Open an issue for anything non-trivial.** Especially anything that widens what
FieldDeck can do without authorization, adds a way to transmit, or changes the
permission class of an existing action. Those are worth discussing before you
have spent an afternoon on them.

**Small fixes need no ceremony.** Typos, a wrong CRC constant, a driver that
mishandles a real device you own — just send the pull request.

---

## Getting set up

```bash
git clone https://github.com/saitokiku/field-deck.git
cd field-deck
make install          # venv + editable install with dev extras
make check            # exactly what CI runs
```

`make check` is `lint` + `typecheck` + `test`. A green local check means a green
pull request; if that stops being true, that is a bug in the Makefile and worth
reporting.

Everything runs with **no hardware attached**. Develop against simulation:

```bash
make sim              # instrumentd with the simulated bench
make ui               # the HMI against it
```

---

## The rules that are not negotiable

These are the architectural invariants the project is built on;
[docs/safety-model.md](docs/safety-model.md) explains the reasoning. A pull
request that breaks one will be asked to change rather than debated.

**1. `instrumentd` is the only thing that touches hardware.** No client opens
`/dev/tty*`, `/dev/spidev*`, `/dev/i2c-*`, a CAN interface or a USB instrument.
If a client needs something, it needs an action.

**2. Nothing bypasses the dispatcher.** Validate → authorize → limits → lease →
run. There is no fast path, no privileged client, and no test-only shortcut.

**3. Simulation uses the same interfaces as real hardware.** A simulated driver
implements the same `Driver` ABC, registers through the same `@action`
decorator, and is dispatched through the same pipeline. No fake-data path in the
UI.

**4. Boot state is SAFE.** Nothing about authorization is recoverable from disk.

**5. Nothing widens its own authority.** Recipes cannot arm. The MCP server
cannot arm. If you are adding a client, it cannot arm either.

**6. Raw capture data is immutable.** Analysis writes new artifacts with
provenance.

**7. Subprocesses take argument arrays, never shell strings.** No arbitrary
shell execution reachable through RPC or MCP.

**8. Never log credentials or API keys.**

**9. Don't guess.** No inferring voltage levels, pinouts, RS-232-vs-TTL, RS-485
polarity or CAN termination. No transmitting to detect a bitrate. `unknown` is a
valid, useful answer and is often the correct one.

---

## Adding a device driver

1. Subclass `Driver` in the right subsystem package.
2. Declare actions with `@action`, and think hard about the permission class.
   When in doubt, choose the stricter one — it is easy to relax later and
   awkward to tighten after people depend on it.
3. Implement `safe_state()` properly. It is what ESTOP, lease expiry, client
   death and shutdown all call. A driver whose `safe_state()` is a no-op is a
   driver that cannot be made safe.
4. **Add a simulated counterpart** in `fielddeck/sim/`. This is not optional:
   without it your driver is untestable in CI and undemonstrable to anyone who
   does not own the hardware.
5. Add tests. `tests/unit/` for logic, `tests/safety/` for anything touching
   authorization.

Actions that energise something need `requires_lease=True`, and a
`permission_resolver` so that the *off* direction is PASSIVE and works during a
latched emergency stop.

### Instrument profiles

Profiles ship with `hardware_verified: false`. If you have actually run one
against the real instrument, say so in the pull request and flip it — and please
say what you checked. A profile that has been on a bench is worth ten that have
been read about.

---

## Adding a protocol decoder

1. Pure functions over bytes in `fielddeck/protocols/` or `fielddeck/analysis/`.
   No I/O, no device access.
2. Decode actions are PASSIVE.
3. **Report what you could not decode as explicitly as what you could.** A
   decoder that silently drops what it does not understand makes a broken stream
   look healthy, which is the worst thing a diagnostic tool can do.
4. Add real captured bytes to `tests/fixtures/` and a test that uses them.

---

## Tests

```bash
make test
make test-fast        # skips anything marked slow
make coverage
.venv/bin/pytest tests/safety -v
```

| | |
|---|---|
| `tests/unit/` | Pure logic. Fast. |
| `tests/safety/` | The invariants. Real daemon, real dispatcher, simulated drivers. |
| `tests/integration/` | End to end over the socket. |
| `tests/ui/` | The HMI, driven by Textual's pilot. |

Mark anything needing physical hardware `@pytest.mark.hardware`; CI never
collects those.

**A safety test must not shortcut authorization.** If a test can energise the
simulated supply without arming POWER first, that is a bug in the product, not
an inconvenience in the test.

### If you find a bug you were not looking for

Write the failing test. If fixing it is out of scope for your pull request, mark
it `xfail(strict=True)` with a reason that says where the bug is and what the
fix looks like — and open an issue. Two of the bugs fixed before the first
release were found exactly this way.

---

## Style

Ruff and mypy are configured; `make format` applies both. Beyond that:

**Write comments that explain why, not what.** The codebase is full of comments
like:

```python
# asyncio's default StreamReader limit is 64 KB. A capture of ~800 CAN frames
# exceeds that, and the symptom is the client's read loop dying rather than an
# error you can act on.
```

That comment exists because somebody lost an hour to it. Yours should too.

**Write error messages for the person reading them at 2 a.m.** Say what
happened, what state things are in now, and what to do next. FieldDeck errors
carry a `preserved` field for exactly this — *"no command was sent to the
device"* is often the most important sentence in a refusal.

**Prefer an honest "unknown" to a confident guess**, in code and in output.

---

## Pull requests

- One concern per pull request.
- Say what you tested it against. **"Simulation only" is a completely
  acceptable answer** and much better than an implication of hardware testing
  that did not happen.
- If it changes a permission class, a limit, or anything in the safety pipeline,
  say so explicitly in the description. Those get read carefully.
- Update the docs in the same pull request.
- Add a `CHANGELOG.md` entry under `Unreleased`.

CI runs `make check` on Python 3.11 and 3.12.

---

## Reporting bugs

Include:

```bash
fdctl --json status > status.json
fdctl --json devices > devices.json
sudo scripts/preflight.sh > preflight.txt 2>&1
journalctl -u instrumentd -n 200 --no-pager > daemon.log
```

And the hardware: which Pi, which adapter, which panel, and what is on the other
end. "It doesn't work with my CAN HAT" and "it doesn't work with a Waveshare
2-CH CAN FD HAT at 250 kbit/s on a Pi 4" are different bug reports, and only one
of them can be acted on.

**Reports from real hardware are the most valuable thing you can contribute.**
Nothing in this repository has been verified against physical hardware yet. If
you run FieldDeck against something real, tell us what happened — especially if
it was wrong.

Security issues go to [SECURITY.md](SECURITY.md), not the issue tracker.

---

## Licence

By contributing you agree that your contributions are licensed under
[Apache-2.0](LICENSE), the same as the project.
