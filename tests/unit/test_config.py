"""Configuration loading, and the one rule that matters most.

``safety.yaml`` is the file that says how much voltage this unit may apply.
If it is present and does not parse, ``instrumentd`` refuses to start.  A
safety file that is silently ignored is worse than no safety file at all: the
operator believes a ceiling is in place, and there is none.

The other half of this file is the tightening rule.  Operator limits may only
narrow the built-in ceilings.  A configuration that asks for more headroom
than the defaults allow does not get it, and the test that proves that is the
one to look at when someone asks why their 60 V setting "did not work".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fielddeck.common.config import (
    DEFAULT_MAX_TTL_S,
    CameraConfig,
    FieldDeckConfig,
    LoggingConfig,
    RemoteConfig,
    SafetyConfig,
    load_config,
    load_safety_config,
    simulation_enabled,
)
from fielddeck.common.errors import ConfigurationError
from fielddeck.common.models import PermissionLevel, SafetyLimit
from fielddeck.common.paths import Paths


def write(paths: Paths, name: str, text: str) -> Path:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    target = paths.config_dir / name
    target.write_text(text, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# safety.yaml
# ---------------------------------------------------------------------------


class TestSafetyConfigLoading:
    def test_a_missing_file_yields_the_conservative_defaults(self, paths: Paths) -> None:
        config = load_safety_config(paths)
        assert config.global_limits["psu.voltage"].maximum == 30.0
        assert config.global_limits["psu.current"].maximum == 3.0
        assert config.max_arm_ttl_s == DEFAULT_MAX_TTL_S

    def test_a_broken_safety_file_refuses_to_load(self, paths: Paths) -> None:
        """The headline rule: unreadable policy is a hard failure."""
        write(paths, "safety.yaml", "global_limits: [this is a list, not a mapping]\n")

        with pytest.raises(ConfigurationError) as caught:
            load_safety_config(paths)

        assert "refusing to start" in str(caught.value)
        assert caught.value.details["path"].endswith("safety.yaml")

    def test_invalid_yaml_is_refused_rather_than_skipped(self, paths: Paths) -> None:
        write(paths, "safety.yaml", "global_limits:\n  psu.voltage: {maximum: 12\n")
        with pytest.raises(ConfigurationError, match="not valid YAML"):
            load_safety_config(paths)

    def test_an_unknown_key_is_refused_rather_than_ignored(self, paths: Paths) -> None:
        """A typo'd key would otherwise read as a limit that is not there."""
        write(paths, "safety.yaml", "maximum_voltage: 12.0\n")
        with pytest.raises(ConfigurationError) as caught:
            load_safety_config(paths)
        assert "maximum_voltage" in str(caught.value)

    def test_a_top_level_list_is_refused(self, paths: Paths) -> None:
        write(paths, "safety.yaml", "- psu.voltage\n")
        with pytest.raises(ConfigurationError, match="mapping at the top level"):
            load_safety_config(paths)

    def test_an_empty_file_is_treated_as_no_overrides(self, paths: Paths) -> None:
        """Empty is unambiguous; broken is not.  Only the second one fails."""
        write(paths, "safety.yaml", "\n")
        config = load_safety_config(paths)
        assert config.global_limits["psu.voltage"].maximum == 30.0

    def test_an_unreadable_path_is_reported_rather_than_skipped(self, paths: Paths) -> None:
        """Anything that is present but cannot be read is the same hard failure.

        A directory where the file should be is the reproducible version of
        this (a permission bit does not stop root, and CI often is root), but
        the path through the loader is the one an I/O error takes.
        """
        paths.config_dir.mkdir(parents=True, exist_ok=True)
        paths.safety_file.mkdir()

        with pytest.raises(ConfigurationError, match="cannot read"):
            load_safety_config(paths)


class TestTightening:
    def test_an_operator_limit_tightens_a_built_in_one(self, paths: Paths) -> None:
        write(
            paths,
            "safety.yaml",
            "global_limits:\n"
            "  psu.voltage:\n"
            "    quantity: psu.voltage\n"
            "    maximum: 12.0\n"
            "    unit: V\n",
        )
        config = load_safety_config(paths)
        assert config.global_limits["psu.voltage"].maximum == 12.0
        # Everything the file did not mention keeps its built-in ceiling.
        assert config.global_limits["psu.current"].maximum == 3.0

    def test_an_operator_limit_cannot_widen_a_built_in_one(self, paths: Paths) -> None:
        """The stricter bound always wins, in both directions.

        Note for whoever reads the source: the comment above the merge in
        ``load_safety_config`` suggests a file can widen a ceiling "by
        replacing the entry".  It cannot — the intersection below is
        unconditional, which is the safer behaviour and the one asserted here.
        """
        write(
            paths,
            "safety.yaml",
            "global_limits:\n"
            "  psu.voltage:\n"
            "    quantity: psu.voltage\n"
            "    maximum: 60.0\n"
            "    minimum: -5.0\n",
        )
        config = load_safety_config(paths)
        limit = config.global_limits["psu.voltage"]
        assert limit.maximum == 30.0
        assert limit.minimum == 0.0

    def test_a_quantity_the_defaults_never_heard_of_is_simply_added(self, paths: Paths) -> None:
        write(
            paths,
            "safety.yaml",
            "global_limits:\n"
            "  load.resistance:\n"
            "    quantity: load.resistance\n"
            "    minimum: 1.0\n"
            "    unit: ohm\n",
        )
        config = load_safety_config(paths)
        assert config.global_limits["load.resistance"].minimum == 1.0

    def test_ttl_ceilings_are_replaced_per_class(self, paths: Paths) -> None:
        write(paths, "safety.yaml", "max_arm_ttl_s:\n  POWER: 30.0\n")
        config = load_safety_config(paths)
        assert config.max_ttl(PermissionLevel.POWER) == 30.0
        assert config.max_ttl(PermissionLevel.QUERY) == DEFAULT_MAX_TTL_S[PermissionLevel.QUERY]

    def test_a_deployment_can_refuse_a_whole_permission_class(self, paths: Paths) -> None:
        write(paths, "safety.yaml", "denied_permissions: [DESTRUCTIVE, FLASH]\n")
        config = load_safety_config(paths)
        assert config.denied_permissions == [
            PermissionLevel.DESTRUCTIVE,
            PermissionLevel.FLASH,
        ]


class TestEffectiveLimits:
    def test_a_device_limit_intersects_the_global_one(self) -> None:
        config = SafetyConfig(
            global_limits={"psu.voltage": SafetyLimit(quantity="psu.voltage", maximum=30.0)},
            device_limits={
                "psu-a": {"psu.voltage": SafetyLimit(quantity="psu.voltage", maximum=5.0)}
            },
        )
        assert config.limit_for("psu.voltage").maximum == 30.0
        assert config.limit_for("psu.voltage", "psu-a").maximum == 5.0
        assert config.limit_for("psu.voltage", "psu-b").maximum == 30.0

    def test_a_device_limit_cannot_loosen_the_global_one(self) -> None:
        config = SafetyConfig(
            global_limits={"psu.voltage": SafetyLimit(quantity="psu.voltage", maximum=12.0)},
            device_limits={
                "psu-a": {"psu.voltage": SafetyLimit(quantity="psu.voltage", maximum=48.0)}
            },
        )
        assert config.limit_for("psu.voltage", "psu-a").maximum == 12.0

    def test_a_device_limit_applies_where_there_is_no_global_one(self) -> None:
        config = SafetyConfig(
            device_limits={"psu-a": {"dut.voltage": SafetyLimit(quantity="dut.voltage", maximum=5)}}
        )
        assert config.limit_for("dut.voltage", "psu-a").maximum == 5
        assert config.limit_for("dut.voltage") is None

    def test_an_unknown_quantity_has_no_limit(self) -> None:
        """Documented so it is a decision, not a surprise.

        ``LimitEnforcer.check_value`` returns silently when no limit exists, so
        a quantity nobody has bounded is unbounded.  Declaring a limit check on
        an action is therefore only half the job; the deployment has to name
        the quantity in ``safety.yaml`` too.
        """
        assert SafetyConfig.defaults().limit_for("load.resistance") is None


class TestSafetyLimitArithmetic:
    def test_violation_reports_the_bound_that_was_crossed(self) -> None:
        limit = SafetyLimit(quantity="psu.voltage", minimum=0.0, maximum=24.0, unit="V")
        assert limit.violation(24.0) is None
        assert limit.violation(-0.1) is not None
        message = limit.violation(24.1)
        assert message is not None and "exceeds maximum 24V" in message

    def test_intersection_keeps_the_stricter_bound_on_each_side(self) -> None:
        a = SafetyLimit(quantity="psu.voltage", minimum=0.0, maximum=30.0, unit="V")
        b = SafetyLimit(quantity="psu.voltage", minimum=1.0, maximum=12.0)
        combined = a.intersect(b)
        assert (combined.minimum, combined.maximum) == (1.0, 12.0)
        assert combined.unit == "V"

    def test_intersecting_different_quantities_is_a_programming_error(self) -> None:
        a = SafetyLimit(quantity="psu.voltage", maximum=30.0)
        b = SafetyLimit(quantity="psu.current", maximum=3.0)
        with pytest.raises(ValueError, match="cannot intersect"):
            a.intersect(b)


# ---------------------------------------------------------------------------
# fielddeck.yaml
# ---------------------------------------------------------------------------


class TestFieldDeckConfigLoading:
    def test_a_missing_file_yields_usable_defaults(self, paths: Paths) -> None:
        config = load_config(paths)
        assert config.display.columns == 80
        assert config.display.rows == 25
        assert [preset.name for preset in config.can_presets] == ["125k", "250k", "500k", "1M"]
        assert config.simulate is False

    def test_an_unknown_key_is_refused(self, paths: Paths) -> None:
        write(paths, "fielddeck.yaml", "displey:\n  columns: 132\n")
        with pytest.raises(ConfigurationError) as caught:
            load_config(paths)
        assert "displey" in str(caught.value)

    def test_a_valid_file_is_applied(self, paths: Paths) -> None:
        write(
            paths,
            "fielddeck.yaml",
            "operator: A. Engineer\n"
            "aliases:\n"
            "  - alias: bench-psu\n"
            "    device_id: visa:usb:0957:1798:MY123\n",
        )
        config = load_config(paths)
        assert config.operator == "A. Engineer"
        assert config.alias_map() == {"bench-psu": "visa:usb:0957:1798:MY123"}

    def test_simulation_is_forced_by_the_environment(
        self, paths: Paths, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIELDDECK_SIM", "1")
        assert simulation_enabled() is True
        assert load_config(paths).simulate is True

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on", " On "])
    def test_the_simulation_flag_accepts_the_obvious_spellings(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIELDDECK_SIM", value)
        assert simulation_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "", "no", "maybe"])
    def test_anything_else_leaves_simulation_off(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIELDDECK_SIM", value)
        assert simulation_enabled() is False


class TestConfigValidators:
    def test_a_bad_log_level_is_rejected_with_the_valid_set(self) -> None:
        with pytest.raises(ValueError, match="log level must be one of"):
            LoggingConfig(level="chatty")

    def test_log_levels_are_normalised_to_upper_case(self) -> None:
        assert LoggingConfig(level="debug").level == "DEBUG"

    @pytest.mark.parametrize("bind", ["0.0.0.0", "::", "*"])
    def test_the_control_api_refuses_to_bind_to_every_interface(self, bind: str) -> None:
        """An instrument bus reachable from the network is not a feature."""
        with pytest.raises(ValueError, match="refusing to bind"):
            RemoteConfig(bind=bind)

    def test_binding_to_a_specific_address_is_allowed(self) -> None:
        assert RemoteConfig(bind="127.0.0.1").port == 8787

    def test_automatic_camera_upload_cannot_be_configured_on(self) -> None:
        """Images leave the unit only when an operator asks, per request."""
        with pytest.raises(ValueError, match="automatic camera upload"):
            CameraConfig(auto_upload=True)

    def test_there_is_no_capture_file_size_knob(self) -> None:
        """A key that silently does nothing reads as a guarantee.

        Rolling a capture mid-stream is not implemented, so the configuration
        deliberately has no ``max_capture_file_mb``.  Bound a capture with its
        duration or frame count instead.
        """
        with pytest.raises(ValueError):
            FieldDeckConfig.model_validate({"storage": {"max_capture_file_mb": 100}})
