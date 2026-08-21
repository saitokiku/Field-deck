# Claude integration

FieldDeck ships an [MCP](https://modelcontextprotocol.io) server so an assistant
can sit in the second tmux window, watch the same session you are watching, and
help you read what a bus is doing.

The interesting part is not that it works. It is what it cannot do, and how that
is enforced.

---

## What it is good at

A 4 MB CAN capture with 40 arbitration IDs is a lot of bytes to hold in your
head at 11 p.m. next to a machine that is not working. An assistant with access
to the same timeline can:

- Read a capture and describe the periodic structure — which IDs are cyclic,
  at what period, with what jitter, and which one stopped
- Correlate across subsystems: *"the current climbed 312 ms before 0x181 went
  quiet"*
- Work out which CRC a trailer uses, or that none of the twenty catalogued
  models produces it and therefore your frame boundaries are wrong
- Read a UDS trace and tell you that somebody was reflashing, not reading
- Draft the session report, with its observations clearly labelled as its own

None of that needs any authority over hardware, which is the point.

---

## The boundary

### A different socket

Claude connects to `instrumentd-ai.sock`. You connect to `instrumentd.sock`.

Every request arriving on the restricted socket is stamped `source=claude` —
**by the socket, not by the client**. There is no way to connect there and be
recorded as something else, and no way to connect to the full socket and claim
to be Claude. The audit trail cannot be spoofed by a client because the client
does not get a say.

### Refused at the transport

`safety.arm`, `safety.disarm` and `safety.estop_clear` are rejected on the
restricted socket **before any handler sees the request**. Not by a policy check
that could be misconfigured — by there being no path.

Defence in depth behind that: `ClientSource.CLAUDE.may_create_grants` is
`False`, so even if a request reached the safety manager it would be refused
again.

### No arming tool exists

Of the 29 MCP tools, **none arms anything**. There is no tool that takes a
permission class. A model cannot call what is not there.

### Kernel-enforced, if you want it

```bash
# In /etc/fielddeck/instrumentd.env
FIELDDECK_AI_GROUP=fielddeck-ai
```

Run the MCP server as a user in only that group and the boundary becomes
filesystem permissions on the socket, enforced by the kernel, rather than a
matter of how a client is configured.

---

## `estop` is the exception, and it points the right way

Claude *can* call `estop`.

```
Permission: none required, by design. This is the one call that needs no
grant, cannot be refused for lack of one, and is still accepted while an
emergency stop is latched. It does change hardware state — outputs go off —
but only ever in the direction of safety, and it destroys no evidence.
```

An assistant that notices a current climbing toward a limit should be able to
stop the bench. It should never be able to start it. That asymmetry is the whole
design in one tool.

---

## The tools

29 tools, in six groups. Exactly one — `estop` — changes state. Every tool is
declared PASSIVE or QUERY; nothing above QUERY is exposed at all.

| Group | Tools |
|---|---|
| **Status** | `fielddeck_status`, `fielddeck_discover`, `permission_status` |
| **Sessions** | `session_list`, `session_get`, `session_events`, `session_window`, `session_summary` |
| **CAN** | `can_interfaces`, `can_status`, `can_capture`, `can_stats`, `can_decode_capture` |
| **Serial** | `serial_devices`, `serial_capture`, `serial_analyze_capture` |
| **Bench / bus** | `bench_devices`, `bench_status`, `scpi_query`, `modbus_read`, `logic_devices`, `firmware_inspect` |
| **Analysis** | `convert_value`, `calculate_crc`, `identify_protocol` |
| **Recipes** | `recipe_list`, `recipe_validate`, `recipe_dry_run` |
| **Safety** | `estop` |

Note what is present and what is absent. `recipe_validate` and `recipe_dry_run`
are there; `recipe_run` is not. `bench_status` is there; `psu_set` is not.
`can_capture` is there; `can_send` is not.

The model can tell you *exactly* what a recipe would do, which classes it needs,
and which limits it would hit. Then you decide.

The few QUERY tools — `scpi_query`, `modbus_read` — put traffic on a bus, and
they are refused unless *you* have armed QUERY. Claude reaching for one is how
you find out it wants to interrogate something, and you get to say no.

---

## Telling a model when to stop

The hard part of AI-assisted diagnosis is not capability. It is a model that,
refused once, tries a slightly different phrasing.

So a refusal from FieldDeck carries a `next_step` field addressed to the model:

**On `PERMISSION_DENIED`:**

> Stop here. This needs a human. Tell the operator what you want to send, to
> which device, and why, then ask them to run `fdctl arm power --ttl 60` or
> press ARM on the HMI. Do not retry until they confirm; the refusal is a
> policy decision, not a transient error.

**On `ESTOP_ACTIVE`:**

> An emergency stop is latched, so only PASSIVE work is available. Clearing it
> is a human's job and cannot be done from here. Use the time to read the
> session: `session_window` around the fault is usually the fastest way to
> explain what happened.

**On a transport error from `estop` specifically:**

> The emergency stop could NOT be confirmed. Say so to the operator
> immediately, in plain words, and tell them to hit the physical stop or run
> `fdctl estop`. Do not describe the machine as safe.

That last one is the one to notice. When a stop cannot be confirmed, silence is
the dangerous answer. A model's instinct is to report the tool call and move on;
this tells it, in the tool result, to say out loud that it does not know whether
the machine is safe.

The daemon's error messages are already actionable for a human at a terminal.
`next_step` adds the part a model gets wrong: **whether retrying is pointless,
and who has to do something instead.**

---

## Setting it up

The MCP server speaks protocol `2025-06-18` over stdio and is implemented
natively — no SDK dependency.

```bash
fielddeck-mcp
```

For Claude Code, in `.mcp.json` or your user config:

```json
{
  "mcpServers": {
    "fielddeck": {
      "command": "/opt/fielddeck/venv/bin/fielddeck-mcp",
      "env": {
        "FIELDDECK_SOCKET": "/run/fielddeck/instrumentd-ai.sock"
      }
    }
  }
}
```

**Point it at the restricted socket.** It defaults there, and it is the only
sensible configuration. Pointing an assistant at `instrumentd.sock` gives it a
path to arming, and there is no reason to.

On a kiosk unit, window 2 of the tmux session is already set up for this.

---

## What it still cannot protect you from

Be clear-eyed about this.

**Claude can be wrong about what a capture means.** The permission model stops
it from *acting* on a wrong conclusion, not from *reaching* one. If it says
"0x181 is the wheel-speed message" and it isn't, nothing in FieldDeck catches
that. It is a reading of evidence, and it deserves the scepticism you would give
a colleague's reading of the same evidence.

**A confident explanation is not a verified one.** Ask what would falsify it.
Ask what it would expect to see if it were wrong. The tools that support this —
`session_window`, `identify_protocol` with its confidence and evidence — are
there precisely so a claim can be checked against the capture.

**You are still the one holding the probes.** The assistant cannot see that the
connector is on backwards.

FieldDeck's guarantee is narrow and worth stating exactly: **a wrong conclusion
by an assistant cannot become a wrong action on hardware without a human arming
it first.** That is a real guarantee. It is not the same as the conclusion being
right.

---

## Camera images

`fielddeck/capture/camera.py` can capture stills — a scope screen, a panel of
indicator lights, the label on a connector.

**Images are never uploaded to an AI service automatically.** `auto_upload` in
`CameraConfig` is not merely `False` by default; the config model has a
validator that rejects `True` outright. Sending a photograph of somebody's
equipment to a third party is a decision that gets made deliberately, by a
person, each time — not by a config file they edited once.

---

## Working with it well

**Start a session first.** The assistant's value is proportional to what it can
see, and an unrecorded capture is invisible to it.

**Let it do the reading.** Correlating four subsystems across a 90-second window
is exactly what it is for, and exactly what you do not want to do by hand at
11 p.m.

**Ask for the evidence, not the conclusion.** *"Which IDs stopped, and when
relative to the current rise?"* beats *"what's wrong?"*

**Take `next_step` seriously when it appears.** It means the model hit a wall
that is not going to move, and the next action is yours.

**Read the observations section of the report.** Assistant observations are
recorded in their own labelled section, separate from measurements, on purpose.
A theory must never be laundered into a measurement — and six months later,
that section is how you tell which was which.
