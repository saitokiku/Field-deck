## What this changes

<!-- One or two sentences. What is different after this merges? -->

## Why

<!-- The problem, not the solution. Link an issue if there is one. -->

## How it was tested

<!--
  "Simulation only" is a completely acceptable answer, and much better than an
  implication of hardware testing that did not happen.

  If you did run it against real hardware, say what and how you verified the
  result — "measured 4.998 V with a Fluke 87V" is a different claim from
  "it reported 5 V", and we want to know which one this is.
-->

- [ ] `make check` passes
- [ ] Tested in simulation
- [ ] Tested against real hardware — *if so, which:*

## Safety

<!--
  Delete this section if the change cannot affect hardware behaviour.
  Otherwise, tick what applies and explain below. These get read carefully.
-->

- [ ] Changes the permission class of an action
- [ ] Adds a way to transmit, energise, write or erase
- [ ] Touches the dispatcher, the safety manager, leases, limits or ESTOP
- [ ] Changes what happens on daemon restart, client disconnect or lease expiry
- [ ] Changes what any non-human client (recipe, MCP) is able to do

## Checklist

- [ ] Docs updated in this PR
- [ ] `CHANGELOG.md` entry under `Unreleased`
- [ ] New hardware support has a simulated counterpart in `fielddeck/sim/`
- [ ] New instrument profiles are `hardware_verified: false` unless I actually
      ran them against the instrument
