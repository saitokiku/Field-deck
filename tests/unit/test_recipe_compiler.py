"""The recipe compiler: refuse before the first action, never halfway through.

Failing at step seven of nine is how a supply is left at 24 V on a bench nobody
is standing at.  So compilation answers, for the whole file at once and without
touching any hardware: which devices it needs and whether they are here, which
actions it will call and whether they exist, what each call's parameters
validate to, the *effective* permission for those exact parameters, every
statically detectable limit violation, and every assertion expression.

Problems accumulate rather than raising one at a time, because an operator
fixing a recipe wants the whole list, not a game of whack-a-mole with a live
bench.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fielddeck.common.config import SafetyConfig
from fielddeck.common.errors import RecipeError
from fielddeck.common.models import PermissionLevel
from fielddeck.daemon.service import InstrumentDaemon
from fielddeck.recipes.compiler import ProblemSeverity, compile_recipe
from fielddeck.recipes.schema import RecipePhase, load_recipe_file, load_recipe_text

SIM_PSU = "sim:visa:sim-psu-0"
SIM_CAN = "sim:can:can0"


@pytest.fixture
def plan_of(daemon: InstrumentDaemon):
    """Compile recipe text against the live simulated bench."""

    def compile_text(text: str):
        loaded = load_recipe_text(text, path="test.yaml")
        return compile_recipe(
            loaded.recipe,
            registry=daemon.registry,
            safety=daemon.safety,
            source=loaded.source,
        )

    return compile_text


# ---------------------------------------------------------------------------
# What a plan says
# ---------------------------------------------------------------------------


class TestPlanning:
    async def test_a_passive_recipe_asks_for_nothing(self, plan_of, fixtures_dir: Path) -> None:
        loaded = load_recipe_file(fixtures_dir / "recipes" / "passive-listen.yaml")
        plan = plan_of(
            (fixtures_dir / "recipes" / "passive-listen.yaml").read_text(encoding="utf-8")
        )

        assert plan.ok
        assert plan.max_permission is PermissionLevel.PASSIVE
        assert plan.permissions_required == []
        assert plan.state_changing_steps == 0
        assert plan.recipe_name == loaded.recipe.name

    async def test_a_power_recipe_names_both_classes_it_needs(
        self, plan_of, fixtures_dir: Path
    ) -> None:
        """Exact-class authorization, made visible before anything runs."""
        plan = plan_of((fixtures_dir / "recipes" / "power-up.yaml").read_text(encoding="utf-8"))

        assert plan.ok, [problem.describe() for problem in plan.errors]
        assert plan.max_permission is PermissionLevel.POWER
        assert set(plan.permissions_required) == {PermissionLevel.POWER, PermissionLevel.QUERY}
        assert plan.state_changing_steps >= 2

    async def test_disabling_an_output_plans_as_passive(self, plan_of) -> None:
        """``psu.output(enabled: false)`` must not ask an operator to arm POWER
        in order to turn a rail off."""
        plan = plan_of(
            "version: 1\nname: n\nsteps:\n"
            f"  - action: psu.output\n    device: {SIM_PSU}\n    enabled: false\n"
        )
        step = plan.steps[0]
        assert step.permission is PermissionLevel.PASSIVE
        assert step.declared_permission is PermissionLevel.POWER
        assert plan.max_permission is PermissionLevel.PASSIVE

    async def test_repeat_is_expanded_into_the_steps_that_will_run(self, plan_of) -> None:
        plan = plan_of(
            "version: 1\nname: n\nsteps:\n"
            "  - repeat:\n      count: 3\n      steps:\n        - wait: 0.1\n        - mark: tick\n"
        )
        assert len(plan.steps) == 6
        assert [step.iteration for step in plan.steps] == [1, 1, 2, 2, 3, 3]

    async def test_the_finally_phase_is_planned_separately(self, plan_of) -> None:
        plan = plan_of(
            "version: 1\nname: n\nsteps:\n  - wait: 0.1\n"
            f"finally:\n  - action: psu.output\n    device: {SIM_PSU}\n    enabled: false\n"
        )
        assert [step.phase for step in plan.steps] == [RecipePhase.STEPS]
        assert [step.phase for step in plan.finally_steps] == [RecipePhase.FINALLY]
        assert len(plan.all_steps) == 2

    async def test_devices_are_resolved_and_reported_with_their_real_ids(self, plan_of) -> None:
        plan = plan_of(
            "version: 1\nname: n\nrequires:\n  devices: [role:psu]\n"
            "steps:\n  - action: psu.status\n    device: role:psu\n"
        )
        assert [device.device_id for device in plan.devices] == [SIM_PSU]
        assert plan.devices[0].present is True
        assert plan.devices[0].simulated is True
        assert plan.steps[0].device_id == SIM_PSU

    async def test_the_summary_is_the_short_form_a_client_renders(self, plan_of) -> None:
        plan = plan_of("version: 1\nname: n\nsteps:\n  - action: system.status\n")
        summary = plan.summary()
        assert summary["recipe"] == "n"
        assert summary["ok"] is True
        assert summary["max_permission"] == str(PermissionLevel.PASSIVE)
        assert summary["errors"] == []

    async def test_the_plan_records_which_file_it_came_from(self, daemon: InstrumentDaemon) -> None:
        loaded = load_recipe_text(
            "version: 1\nname: n\nsteps:\n  - action: system.status\n", path="n.yaml"
        )
        plan = compile_recipe(
            loaded.recipe, registry=daemon.registry, safety=daemon.safety, source=loaded.source
        )
        assert plan.source["path"] == "n.yaml"
        assert plan.source["sha256"] == loaded.source.sha256


# ---------------------------------------------------------------------------
# What a plan refuses
# ---------------------------------------------------------------------------


class TestRefusals:
    async def test_a_missing_device_is_an_error_before_anything_runs(self, plan_of) -> None:
        plan = plan_of(
            "version: 1\nname: n\nrequires:\n  devices: [serial:usb:0403:6001:NOTHERE]\n"
            "steps:\n  - action: system.status\n"
        )
        assert not plan.ok
        assert plan.missing_devices == ["serial:usb:0403:6001:NOTHERE"]

    async def test_an_unknown_action_is_caught_with_a_suggestion(self, plan_of) -> None:
        plan = plan_of(
            f"version: 1\nname: n\nsteps:\n  - action: psu.sett\n    device: {SIM_PSU}\n"
        )
        assert not plan.ok
        assert any("psu.set" in problem.message for problem in plan.errors)

    async def test_bad_parameters_are_caught_at_compile_time(self, plan_of) -> None:
        """The action's own model validates, so a typo'd parameter never runs."""
        plan = plan_of(
            f"version: 1\nname: n\nsteps:\n"
            f"  - action: psu.set\n    device: {SIM_PSU}\n    voltag: 12.0\n"
        )
        assert not plan.ok
        assert any("voltag" in problem.message for problem in plan.errors)

    async def test_a_setpoint_over_the_deployment_ceiling_is_refused(
        self, plan_of, fixtures_dir: Path
    ) -> None:
        plan = plan_of((fixtures_dir / "recipes" / "over-limit.yaml").read_text(encoding="utf-8"))
        assert not plan.ok
        assert any("psu.voltage" in problem.message for problem in plan.errors)

    async def test_the_gate_raises_with_everything_that_is_wrong(self, plan_of) -> None:
        plan = plan_of(
            "version: 1\nname: n\nsteps:\n"
            f"  - action: psu.set\n    device: {SIM_PSU}\n    voltage: 60.0\n"
            "  - action: psu.nonsense\n    device: role:psu\n"
        )
        with pytest.raises(RecipeError) as caught:
            plan.raise_if_unrunnable()

        assert caught.value.preserved == "no action was requested and no device was touched"
        assert len(caught.value.details["problems"]) >= 2

    async def test_a_runnable_plan_passes_the_gate_silently(self, plan_of) -> None:
        plan_of("version: 1\nname: n\nsteps:\n  - action: system.status\n").raise_if_unrunnable()

    async def test_problems_accumulate_rather_than_stopping_at_the_first(self, plan_of) -> None:
        """An operator fixing a recipe wants the whole list."""
        plan = plan_of(
            "version: 1\nname: n\nrequires:\n  devices: [can:socketcan:nothere]\n"
            "steps:\n"
            f"  - action: psu.set\n    device: {SIM_PSU}\n    voltage: 60.0\n"
            "  - action: does.not.exist\n"
            "  - assert: \"__import__('os')\"\n"
        )
        assert len(plan.errors) >= 4

    async def test_a_malicious_assertion_never_compiles(self, plan_of, fixtures_dir: Path) -> None:
        plan = plan_of(
            (fixtures_dir / "recipes" / "malicious-assert.yaml").read_text(encoding="utf-8")
        )
        assert not plan.ok
        assert any(
            "assert" in problem.code or "assert" in problem.message.lower()
            for problem in plan.errors
        )

    async def test_an_assertion_is_parsed_at_compile_time_not_at_step_nine(self, plan_of) -> None:
        plan = plan_of('version: 1\nname: n\nsteps:\n  - assert: "can.frames >"\n')
        assert not plan.ok


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


class TestLimits:
    async def test_a_recipe_may_tighten_the_deployment_envelope(self, plan_of) -> None:
        plan = plan_of(
            "version: 1\nname: n\nlimits:\n  voltage_max: 12.0\nsteps:\n"
            f"  - action: psu.set\n    device: {SIM_PSU}\n    voltage: 24.0\n"
        )
        assert not plan.ok
        assert plan.effective_limits["psu.voltage"]["maximum"] == 12.0
        assert plan.declared_limits["psu.voltage"]["maximum"] == 12.0

    async def test_a_recipe_cannot_widen_the_deployment_envelope(self, plan_of) -> None:
        """A recipe asking for more headroom than safety.yaml allows was written
        for a different bench; it is refused rather than quietly clamped."""
        plan = plan_of(
            "version: 1\nname: n\nlimits:\n  voltage_max: 60.0\nsteps:\n"
            f"  - action: psu.set\n    device: {SIM_PSU}\n    voltage: 40.0\n"
        )
        assert not plan.ok
        assert plan.effective_limits["psu.voltage"]["maximum"] == 30.0

    async def test_the_compiler_and_the_dispatcher_share_the_enforcement_code(
        self, daemon: InstrumentDaemon
    ) -> None:
        """So the two can never disagree about what is allowed.

        The plan's effective limits are what ``LimitEnforcer`` will apply at
        run time, not a second implementation of the same idea.
        """
        loaded = load_recipe_text(
            "version: 1\nname: n\nsteps:\n"
            f"  - action: psu.set\n    device: {SIM_PSU}\n    voltage: 12.0\n",
            path="n.yaml",
        )
        plan = compile_recipe(
            loaded.recipe, registry=daemon.registry, safety=daemon.safety, source=loaded.source
        )
        for quantity, bounds in plan.effective_limits.items():
            live = daemon.safety.limits.effective(quantity)
            if live is not None:
                assert bounds["maximum"] == live.maximum

    async def test_a_derived_limit_is_checked_statically(
        self, daemon_factory, strict_safety_config: SafetyConfig
    ) -> None:
        """V x I is caught in the plan, before the supply is asked for either."""
        daemon = await daemon_factory(safety_config=strict_safety_config)
        loaded = load_recipe_text(
            "version: 1\nname: n\nsteps:\n"
            f"  - action: psu.set\n    device: {SIM_PSU}\n"
            "    voltage: 5.0\n    current_limit: 2.0\n",
            path="n.yaml",
        )
        plan = compile_recipe(
            loaded.recipe, registry=daemon.registry, safety=daemon.safety, source=loaded.source
        )
        assert not plan.ok
        assert any("psu.power" in problem.message for problem in plan.errors)


# ---------------------------------------------------------------------------
# Warnings, which do not block a run
# ---------------------------------------------------------------------------


class TestWarnings:
    async def test_a_lease_shorter_than_the_steps_that_follow_is_a_warning(self, plan_of) -> None:
        """The daemon will drop the rail mid-test; better to say so up front."""
        plan = plan_of(
            "version: 1\nname: n\nsteps:\n"
            f"  - action: psu.output\n    device: {SIM_PSU}\n    enabled: true\n"
            "    lease_ttl_s: 1\n"
            "  - wait: 30\n"
            f"  - action: psu.status\n    device: {SIM_PSU}\n"
        )
        assert plan.ok, "a short lease is a warning, not a refusal"
        assert any(problem.severity is ProblemSeverity.WARNING for problem in plan.problems)

    async def test_the_estimate_uses_the_durations_the_recipe_declared(self, plan_of) -> None:
        plan = plan_of("version: 1\nname: n\nsteps:\n  - wait: 2\n  - wait: 3\n")
        assert plan.estimated_duration_s == pytest.approx(5.0, abs=0.5)
