"""The recipe engine, end to end through the daemon.

A recipe is automation, and automation is exactly where a safety model gets
quietly bypassed, so these tests check the three things that must remain
true: a passing run is a real run through the ordinary dispatcher, a failing
assertion stops the run *and still executes the cleanup phase*, and a dry run
tells an operator what authorization it would need without needing any of it.

Recipes are passed inline as ``text`` rather than as files.  The recipe roots
include the current working directory, and a test that depended on the repo's
own ``recipes/`` tree would change meaning depending on where pytest was
started from.
"""

from __future__ import annotations

import pytest

from fielddeck.common.errors import PermissionDenied, RecipeError
from fielddeck.common.events import EventType
from fielddeck.common.models import PermissionLevel
from fielddeck.daemon.client import InstrumentClient

from .conftest import SIM_SERIAL, arm

PASSING = f"""
version: 1
name: passive-listen
description: Configure this end of the link, record the stream, prove it ticked.
requires:
  devices:
    - id: {SIM_SERIAL}
steps:
  - mark: listen-start
  - action: serial.configure
    device: {SIM_SERIAL}
    baudrate: 115200
    bytesize: 8
    parity: "N"
    stopbits: 1
  - assert:
      expression: "serial.baudrate == 115200 and serial.framing == '8N1'"
      message: The port did not take the requested framing
  - action: serial.capture
    device: {SIM_SERIAL}
    duration_s: 1
    label: recipe
  - assert:
      expression: "serial.bytes > 0"
      message: Nothing arrived on the port
  - note: The stream and its arrival-time index are in the session.
finally:
  - mark: listen-complete
"""

FAILING = f"""
version: 1
name: impossible-assertion
description: A recipe whose assertion cannot hold, to prove cleanup still runs.
requires:
  devices:
    - id: {SIM_SERIAL}
steps:
  - action: serial.capture
    device: {SIM_SERIAL}
    duration_s: 0.5
    label: doomed
  - assert:
      expression: "serial.bytes > 100000000"
      message: The port cannot have delivered 100 MB in half a second
  - mark: should-never-run
finally:
  - mark: cleanup-ran
"""

NEEDS_POWER = """
version: 1
name: energise-the-controller
description: Set a rail, read it back, and put it down again.
requires:
  devices:
    - role: psu
limits:
  voltage_max: 24.5
  current_max: 1.0
steps:
  - action: psu.set
    device: role:psu
    voltage: 12.0
    current_limit: 0.5
  - action: psu.measure
    device: role:psu
finally:
  - action: psu.output
    device: role:psu
    enabled: false
"""

OVER_THE_LIMIT = """
version: 1
name: over-the-limit
description: Asks for more voltage than this deployment allows.
requires:
  devices:
    - role: psu
steps:
  - action: psu.set
    device: role:psu
    voltage: 400.0
    current_limit: 1.0
"""


@pytest.mark.slow
async def test_a_passing_recipe_runs_through_the_ordinary_pipeline(
    client: InstrumentClient, session: str
) -> None:
    result = await client.execute("recipe.run", {"text": PASSING}, timeout_s=90.0)
    run = result.result

    assert run["state"] == "PASSED"
    assert run["assertions_failed"] == 0
    assert run["assertions_passed"] == 2
    assert run["finally_ran"] is True
    assert [step["outcome"] for step in run["steps"]] == ["ok"] * len(run["steps"])
    assert run["failure"] is None

    # The run is on the timeline, and so is the capture it made.
    events = (await client.execute("session.events", {"session_id": session, "limit": 500})).result[
        "events"
    ]
    types = [row["type"] for row in events]
    assert str(EventType.RECIPE_STARTED) in types
    assert str(EventType.RECIPE_FINISHED) in types
    assert str(EventType.CAPTURE_STARTED) in types

    stored = (await client.execute("session.get", {"session_id": session})).result
    assert any("recipe" in row["relative_path"] for row in stored["artifacts"])

    summary = (await client.execute("session.summary", {"session_id": session})).result
    assert {"listen-start", "listen-complete"} <= {row["label"] for row in summary["marks"]}


@pytest.mark.slow
async def test_a_failed_assertion_stops_the_run_but_not_the_cleanup(
    client: InstrumentClient, session: str
) -> None:
    result = await client.execute("recipe.run", {"text": FAILING}, timeout_s=90.0)
    run = result.result

    # A failed assertion is a test result, not a broken tool: the action
    # itself succeeds and hands back the evidence.
    assert result.ok is True
    assert run["state"] == "FAILED"
    assert run["assertions_failed"] == 1
    assert run["cancelled"] is False
    assert run["estop"] is False

    # The run stops at the failure: the step after it has no record at all,
    # which is how the engine reports "this never happened".
    outcomes = [step["outcome"] for step in run["steps"]]
    assert outcomes[-1] == "failed"
    assert len(run["steps"]) < run["plan"]["steps"]

    assert run["finally_ran"] is True
    assert [step["outcome"] for step in run["finally_steps"]] == ["ok"]

    summary = (await client.execute("session.summary", {"session_id": session})).result
    labels = {row["label"] for row in summary["marks"]}
    assert "cleanup-ran" in labels, "the finally phase must run on failure"
    assert "should-never-run" not in labels

    assertion_events = (
        await client.execute(
            "session.events",
            {"session_id": session, "types": [str(EventType.RECIPE_ASSERTION)]},
        )
    ).result["events"]
    assert assertion_events
    assert any("serial.bytes" in (row["message"] or "") for row in assertion_events)

    # Whatever it captured before the assertion failed is still evidence.
    stored = (await client.execute("session.get", {"session_id": session})).result
    assert any("doomed" in row["relative_path"] for row in stored["artifacts"])


async def test_a_dry_run_reports_the_permissions_it_would_need(
    client: InstrumentClient, session: str
) -> None:
    """The question a dry run answers is what you *would* need, unarmed."""
    result = await client.execute("recipe.dry_run", {"text": NEEDS_POWER}, timeout_s=60.0)
    payload = result.result
    plan = payload["plan"]

    assert plan["max_permission"] == str(PermissionLevel.POWER)
    assert set(plan["permissions_required"]) == {
        str(PermissionLevel.POWER),
        str(PermissionLevel.QUERY),
    }
    assert plan["state_changing_steps"] >= 1
    assert plan["effective_limits"]["psu.voltage"]["maximum"] == 24.5

    # Nothing is armed, so it says so instead of refusing to answer.
    assert payload["would_start"] is False
    assert payload["run"]["dry_run"] is True
    assert payload["run"]["steps"] == []
    reason = payload["run"]["reason"] or ""
    assert "POWER" in reason or "QUERY" in reason

    # Arm what it asked for and the same question answers differently.
    await arm(client, PermissionLevel.POWER, PermissionLevel.QUERY, ttl_s=60.0)
    armed = (await client.execute("recipe.dry_run", {"text": NEEDS_POWER}, timeout_s=60.0)).result
    assert armed["would_start"] is True
    assert armed["run"]["steps"] == []


async def test_a_recipe_over_the_safety_limits_never_starts(client: InstrumentClient) -> None:
    """Limits are checked when the plan is compiled, before any device is touched."""
    validated = (await client.execute("recipe.validate", {"text": OVER_THE_LIMIT})).result
    assert validated["ok"] is False
    assert any("psu.voltage" in problem["message"] for problem in validated["plan"]["problems"])

    # Unarmed, the refusal is about authorization: ``recipe.run`` narrows
    # itself to the worst thing this particular recipe reaches, which is POWER.
    with pytest.raises(PermissionDenied) as unauthorized:
        await client.execute("recipe.run", {"text": OVER_THE_LIMIT}, timeout_s=60.0)
    assert unauthorized.value.details["permission"] == str(PermissionLevel.POWER)

    # Armed, the limit still refuses it. Being authorized never raises a ceiling.
    await arm(client, PermissionLevel.POWER, ttl_s=60.0)
    with pytest.raises(RecipeError) as refused:
        await client.execute("recipe.run", {"text": OVER_THE_LIMIT}, timeout_s=60.0)
    assert "no device was touched" in (refused.value.preserved or "")
    assert "psu.voltage" in str(refused.value.details["problems"])


async def test_recipe_validation_names_the_phase_and_the_step(
    client: InstrumentClient,
) -> None:
    """A broken recipe has to say *where* it is broken, not just that it is."""
    with pytest.raises(RecipeError) as broken:
        await client.execute(
            "recipe.validate",
            {"text": "version: 1\nname: nope\nsteps:\n  - frobnicate: yes\n"},
        )
    assert "steps step 0" in broken.value.message
    assert "nothing was run" in (broken.value.preserved or "")
