"""The safety authority in isolation: grants, leases, limits and the latch.

The end-to-end proof that the *pipeline* consults these lives in
``tests/safety/``.  This file is about the pieces themselves — the arithmetic
of expiry, the narrowest-grant rule, what a lease remembers, and the order in
which :meth:`SafetyManager.authorize` refuses things, because the order decides
which sentence an operator reads first.
"""

from __future__ import annotations

import pytest

from fielddeck.common.config import SafetyConfig
from fielddeck.common.errors import (
    EstopActive,
    InvalidRequest,
    LeaseError,
    PermissionDenied,
    SafetyLimitExceeded,
)
from fielddeck.common.events import EventType
from fielddeck.common.models import ArmScope, ClientSource, PermissionLevel, SafetyLimit
from fielddeck.common.timebase import monotonic_ns
from fielddeck.safety.arm import ArmRegistry
from fielddeck.safety.estop import EstopController
from fielddeck.safety.leases import LeaseManager
from fielddeck.safety.limits import DerivedLimitCheck, LimitCheck, LimitEnforcer
from fielddeck.safety.manager import SafetyManager

GRANTABLE = [level for level in PermissionLevel if level.requires_grant]
SECOND = 1_000_000_000


def authorize(safety: SafetyManager, permission: PermissionLevel, **kwargs):
    return safety.authorize(
        action=kwargs.pop("action", "test.action"),
        permission=permission,
        device_id=kwargs.pop("device_id", None),
        source=kwargs.pop("source", ClientSource.FDCTL),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The arm registry
# ---------------------------------------------------------------------------


class TestArmRegistry:
    def test_only_a_human_facing_client_may_create_a_grant(self) -> None:
        registry = ArmRegistry()
        for source in (ClientSource.CLAUDE, ClientSource.RECIPE, ClientSource.SYSTEM):
            with pytest.raises(PermissionDenied):
                registry.create(permission=PermissionLevel.POWER, ttl_s=60, source=source)

    def test_there_is_nothing_to_arm_for_passive(self) -> None:
        with pytest.raises(InvalidRequest, match="nothing to arm"):
            ArmRegistry().create(
                permission=PermissionLevel.PASSIVE, ttl_s=60, source=ClientSource.FDCTL
            )

    @pytest.mark.parametrize("ttl", [0, -1, -0.5])
    def test_a_grant_must_have_a_positive_lifetime(self, ttl: float) -> None:
        with pytest.raises(InvalidRequest, match="positive"):
            ArmRegistry().create(
                permission=PermissionLevel.POWER, ttl_s=ttl, source=ClientSource.FDCTL
            )

    def test_a_ttl_is_clamped_to_policy_and_the_clamp_is_recorded(self) -> None:
        """Silently shortening a grant would leave an operator surprised when
        it lapsed, so the note says what happened."""
        grant = ArmRegistry().create(
            permission=PermissionLevel.POWER,
            ttl_s=3600,
            source=ClientSource.FDCTL,
            max_ttl_s=300,
            note="bring-up",
        )
        assert grant.ttl_s == 300
        assert "clamped" in (grant.note or "")
        assert "bring-up" in (grant.note or "")

    def test_authorization_is_exact_class(self) -> None:
        registry = ArmRegistry()
        registry.create(
            permission=PermissionLevel.DESTRUCTIVE, ttl_s=60, source=ClientSource.FDCTL
        )
        assert registry.find(
            permission=PermissionLevel.DESTRUCTIVE, action="flash.erase", device_id=None
        )
        # Ranked higher, and still not a substitute.
        assert PermissionLevel.DESTRUCTIVE > PermissionLevel.QUERY
        assert (
            registry.find(permission=PermissionLevel.QUERY, action="psu.measure", device_id=None)
            is None
        )

    def test_the_narrowest_grant_is_the_one_recorded(self) -> None:
        """The audit trail should show the most specific authority the operator
        actually gave, not the broadest one that happened to cover it."""
        registry = ArmRegistry()
        registry.create(permission=PermissionLevel.POWER, ttl_s=60, source=ClientSource.FDCTL)
        registry.create(
            permission=PermissionLevel.POWER,
            ttl_s=60,
            source=ClientSource.FDCTL,
            scope=ArmScope(kind="device", device_id="psu-a"),
        )
        narrow = registry.create(
            permission=PermissionLevel.POWER,
            ttl_s=60,
            source=ClientSource.FDCTL,
            scope=ArmScope(kind="action", action="psu.output"),
        )

        found = registry.find(
            permission=PermissionLevel.POWER, action="psu.output", device_id="psu-a"
        )
        assert found is not None and found.grant_id == narrow.grant_id

    def test_an_expired_grant_stops_authorizing_without_being_swept(self) -> None:
        registry = ArmRegistry()
        grant = registry.create(
            permission=PermissionLevel.POWER, ttl_s=60, source=ClientSource.FDCTL
        )
        later = grant.expires_monotonic_ns + 1
        assert (
            registry.find(
                permission=PermissionLevel.POWER,
                action="psu.set",
                device_id=None,
                at_monotonic_ns=later,
            )
            is None
        )
        assert registry.active(later) == []

    def test_sweeping_reports_each_expiry_once(self) -> None:
        registry = ArmRegistry()
        grant = registry.create(
            permission=PermissionLevel.POWER, ttl_s=60, source=ClientSource.FDCTL
        )
        later = grant.expires_monotonic_ns + 1

        assert [expired.grant_id for expired in registry.sweep(later)] == [grant.grant_id]
        assert registry.sweep(later) == []

    def test_a_revoked_grant_is_dropped_by_the_next_sweep(self) -> None:
        registry = ArmRegistry()
        grant = registry.create(
            permission=PermissionLevel.CONTROL, ttl_s=60, source=ClientSource.FDCTL
        )
        registry.revoke(grant.grant_id)
        assert registry.sweep(monotonic_ns()) == []
        assert registry.get(grant.grant_id) is None

    def test_revoking_the_same_grant_twice_reports_nothing_the_second_time(self) -> None:
        registry = ArmRegistry()
        grant = registry.create(
            permission=PermissionLevel.CONTROL, ttl_s=60, source=ClientSource.FDCTL
        )
        assert registry.revoke(grant.grant_id) is not None
        assert registry.revoke(grant.grant_id) is None

    def test_armed_permissions_are_listed_in_severity_order(self) -> None:
        registry = ArmRegistry()
        for permission in (PermissionLevel.POWER, PermissionLevel.QUERY, PermissionLevel.FLASH):
            registry.create(permission=permission, ttl_s=60, source=ClientSource.FDCTL)
        assert registry.armed_permissions() == [
            PermissionLevel.QUERY,
            PermissionLevel.POWER,
            PermissionLevel.FLASH,
        ]


# ---------------------------------------------------------------------------
# Leases
# ---------------------------------------------------------------------------


class TestLeases:
    def _acquire(self, manager: LeaseManager, **kwargs):
        return manager.acquire(
            device_id=kwargs.pop("device_id", "sim:visa:sim-psu-0"),
            action="psu.output",
            owner=ClientSource.HMI,
            ttl_s=kwargs.pop("ttl_s", 30.0),
            safe_action="psu.output",
            safe_params={"enabled": False},
            **kwargs,
        )

    def test_a_lease_remembers_how_to_make_the_device_safe(self) -> None:
        lease = self._acquire(LeaseManager())
        assert lease.safe_action == "psu.output"
        assert lease.safe_params == {"enabled": False}

    def test_a_lease_must_have_a_positive_lifetime(self) -> None:
        with pytest.raises(LeaseError):
            self._acquire(LeaseManager(), ttl_s=0)

    def test_renewing_extends_from_now_not_from_the_old_deadline(self) -> None:
        manager = LeaseManager()
        lease = self._acquire(manager, ttl_s=30)
        before = lease.expires_monotonic_ns
        renewed = manager.renew(lease.lease_id, ttl_s=60)
        assert renewed.expires_monotonic_ns > before

    def test_an_expired_lease_cannot_be_renewed(self) -> None:
        """Re-acquire instead; renewing a lapsed handle would hide the lapse."""
        manager = LeaseManager()
        lease = self._acquire(manager)
        lease.expires_monotonic_ns = monotonic_ns() - 1
        with pytest.raises(LeaseError, match="already expired"):
            manager.renew(lease.lease_id)

    def test_renewing_something_that_never_existed_says_what_survived(self) -> None:
        with pytest.raises(LeaseError) as caught:
            LeaseManager().renew("lease-nope")
        assert caught.value.preserved == (
            "the device was left in whatever state it was already in"
        )

    def test_sweeping_hands_back_each_lapsed_lease_once(self) -> None:
        manager = LeaseManager()
        lease = self._acquire(manager)
        lease.expires_monotonic_ns = monotonic_ns() - 1

        assert [entry.lease_id for entry in manager.sweep()] == [lease.lease_id]
        assert manager.sweep() == []

    def test_a_released_lease_is_gone_from_the_active_set(self) -> None:
        manager = LeaseManager()
        lease = self._acquire(manager)
        assert manager.release(lease.lease_id) is not None
        assert manager.active() == []
        assert manager.release(lease.lease_id) is None

    def test_leases_can_be_reclaimed_by_owning_connection(self) -> None:
        """What the daemon does when a client's socket goes away."""
        manager = LeaseManager()
        mine = self._acquire(manager, owner_connection=7)
        self._acquire(manager, device_id="sim:visa:other", owner_connection=8)

        taken = manager.take_for_connection(7)
        assert [entry.lease_id for entry in taken] == [mine.lease_id]
        assert len(manager.active()) == 1

    def test_taking_everything_is_how_estop_surrenders_them(self) -> None:
        manager = LeaseManager()
        self._acquire(manager)
        self._acquire(manager, device_id="sim:visa:other")
        assert len(manager.take_all()) == 2
        assert manager.active() == []

    def test_leases_for_a_device_are_findable(self) -> None:
        manager = LeaseManager()
        self._acquire(manager, device_id="psu-a")
        self._acquire(manager, device_id="psu-b")
        assert [entry.device_id for entry in manager.for_device("psu-a")] == ["psu-a"]


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


class TestLimitEnforcer:
    @pytest.fixture
    def enforcer(self) -> LimitEnforcer:
        return LimitEnforcer(
            SafetyConfig(
                global_limits={
                    "psu.voltage": SafetyLimit(
                        quantity="psu.voltage", minimum=0.0, maximum=24.0, unit="V"
                    ),
                    "psu.current": SafetyLimit(
                        quantity="psu.current", minimum=0.0, maximum=2.0, unit="A"
                    ),
                    "psu.power": SafetyLimit(quantity="psu.power", maximum=10.0, unit="W"),
                }
            )
        )

    def test_a_value_inside_the_bounds_passes_silently(self, enforcer: LimitEnforcer) -> None:
        enforcer.check_value("psu.voltage", 24.0)
        enforcer.check_value("psu.voltage", 0.0)

    def test_a_value_outside_the_bounds_is_refused(self, enforcer: LimitEnforcer) -> None:
        with pytest.raises(SafetyLimitExceeded) as caught:
            enforcer.check_value("psu.voltage", 24.1)
        assert caught.value.details["maximum"] == 24.0
        assert caught.value.preserved == "no command was sent to the device"

    def test_an_unlimited_quantity_passes(self, enforcer: LimitEnforcer) -> None:
        """Documented rather than assumed: a quantity nobody bounded is
        unbounded, so declaring a check on an action is only half the job."""
        enforcer.check_value("load.resistance", 0.001)

    def test_parameters_are_checked_by_the_action_declaration(
        self, enforcer: LimitEnforcer
    ) -> None:
        checks = (
            LimitCheck(param="voltage", quantity="psu.voltage"),
            LimitCheck(param="current_limit", quantity="psu.current"),
        )
        enforcer.check_params({"voltage": 12.0, "current_limit": 1.0}, checks)
        with pytest.raises(SafetyLimitExceeded):
            enforcer.check_params({"voltage": 30.0}, checks)

    def test_an_absent_optional_parameter_is_not_a_violation(
        self, enforcer: LimitEnforcer
    ) -> None:
        enforcer.check_params({}, (LimitCheck(param="voltage", quantity="psu.voltage"),))

    def test_a_required_parameter_that_is_missing_is_a_violation(
        self, enforcer: LimitEnforcer
    ) -> None:
        with pytest.raises(SafetyLimitExceeded, match="is required"):
            enforcer.check_params(
                {}, (LimitCheck(param="voltage", quantity="psu.voltage", required=True),)
            )

    def test_a_non_numeric_parameter_is_refused_rather_than_skipped(
        self, enforcer: LimitEnforcer
    ) -> None:
        """Skipping the check would let a string past the ceiling entirely."""
        with pytest.raises(SafetyLimitExceeded, match="must be numeric"):
            enforcer.check_params(
                {"voltage": "twelve"}, (LimitCheck(param="voltage", quantity="psu.voltage"),)
            )

    def test_a_derived_limit_bounds_the_product(self, enforcer: LimitEnforcer) -> None:
        checks = (DerivedLimitCheck(quantity="psu.power", params=("voltage", "current_limit")),)
        enforcer.check_derived({"voltage": 5.0, "current_limit": 2.0}, checks)
        with pytest.raises(SafetyLimitExceeded) as caught:
            enforcer.check_derived({"voltage": 6.0, "current_limit": 2.0}, checks)
        assert caught.value.details["value"] == pytest.approx(12.0)

    def test_a_derived_limit_needs_every_parameter_to_mean_anything(
        self, enforcer: LimitEnforcer
    ) -> None:
        checks = (DerivedLimitCheck(quantity="psu.power", params=("voltage", "current_limit")),)
        enforcer.check_derived({"voltage": 100.0}, checks)

    def test_a_sum_derived_limit_works_too(self, enforcer: LimitEnforcer) -> None:
        checks = (DerivedLimitCheck(quantity="psu.power", params=("a", "b"), op="sum"),)
        with pytest.raises(SafetyLimitExceeded):
            enforcer.check_derived({"a": 6.0, "b": 6.0}, checks)

    def test_an_unknown_derived_operation_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="unknown derived limit op"):
            DerivedLimitCheck(quantity="q", params=("a",), op="convolve").compute([1.0])

    def test_describing_the_limits_is_what_the_hmi_shows(
        self, enforcer: LimitEnforcer
    ) -> None:
        described = enforcer.describe()
        assert described["psu.voltage"] == {"minimum": 0.0, "maximum": 24.0, "unit": "V"}


# ---------------------------------------------------------------------------
# The ESTOP latch
# ---------------------------------------------------------------------------


class TestEstopController:
    def test_it_latches_rather_than_following_the_fault(self) -> None:
        """"The fault cleared itself" is not a reason to re-energise a bench."""
        controller = EstopController()
        controller.engage("over-current", ClientSource.HMI)
        assert controller.active
        assert controller.status.reason == "over-current"

    def test_the_first_reason_is_the_one_that_sticks(self) -> None:
        controller = EstopController()
        controller.engage("the real reason", ClientSource.HMI)
        controller.engage("someone pressed it again", ClientSource.FDCTL)
        assert controller.status.reason == "the real reason"

    def test_only_a_human_facing_client_may_clear_it(self) -> None:
        controller = EstopController()
        controller.engage("test", ClientSource.HMI)
        for source in (ClientSource.CLAUDE, ClientSource.RECIPE, ClientSource.SYSTEM):
            with pytest.raises(PermissionDenied):
                controller.acknowledge(source)
        assert controller.active

        controller.acknowledge(ClientSource.HMI)
        assert not controller.active

    def test_engaging_records_both_clocks(self) -> None:
        controller = EstopController()
        status = controller.engage("test", ClientSource.HMI)
        assert status.engaged_monotonic_ns and status.engaged_utc_ns


# ---------------------------------------------------------------------------
# The manager, composing all of it
# ---------------------------------------------------------------------------


class TestSafetyManager:
    def test_the_boot_state_is_safe(self, safety: SafetyManager) -> None:
        snapshot = safety.snapshot()
        assert snapshot.state_word == "SAFE"
        assert snapshot.grants == []
        assert snapshot.leases == []
        assert not snapshot.estop_active

    def test_passive_needs_no_grant(self, safety: SafetyManager) -> None:
        assert authorize(safety, PermissionLevel.PASSIVE) is None

    @pytest.mark.parametrize("permission", GRANTABLE)
    def test_everything_else_is_refused_at_boot_with_a_way_forward(
        self, safety: SafetyManager, permission: PermissionLevel
    ) -> None:
        with pytest.raises(PermissionDenied) as caught:
            authorize(safety, permission)
        assert "fdctl arm" in caught.value.details["hint"]
        assert caught.value.preserved == "no command was sent to the device"

    def test_a_denial_is_emitted_as_an_event(self, safety_config: SafetyConfig) -> None:
        """A refused attempt is as visible on the timeline as a successful one."""
        emitted = []
        safety = SafetyManager(safety_config, emit=emitted.append)
        with pytest.raises(PermissionDenied):
            authorize(safety, PermissionLevel.POWER)
        assert [event.type for event in emitted] == [EventType.ACTION_DENIED]

    def test_a_policy_denial_outranks_a_grant(self, safety_config: SafetyConfig) -> None:
        policy = safety_config.model_copy(
            update={"denied_permissions": [PermissionLevel.DESTRUCTIVE]}
        )
        safety = SafetyManager(policy)
        safety.arm(
            permission=PermissionLevel.DESTRUCTIVE, ttl_s=60, source=ClientSource.FDCTL
        )
        with pytest.raises(PermissionDenied, match="safety policy"):
            authorize(safety, PermissionLevel.DESTRUCTIVE)

    def test_the_latch_is_checked_before_the_grant(self, safety: SafetyManager) -> None:
        """So the operator is told to clear the stop, not to arm something."""
        safety.arm(permission=PermissionLevel.POWER, ttl_s=60, source=ClientSource.FDCTL)
        safety.estop_controller.engage("test", ClientSource.HMI)
        with pytest.raises(EstopActive):
            authorize(safety, PermissionLevel.POWER)

    def test_policy_is_checked_before_the_latch(self, safety_config: SafetyConfig) -> None:
        policy = safety_config.model_copy(update={"denied_permissions": [PermissionLevel.FLASH]})
        safety = SafetyManager(policy)
        safety.estop_controller.engage("test", ClientSource.HMI)
        with pytest.raises(PermissionDenied):
            authorize(safety, PermissionLevel.FLASH)

    def test_an_estop_safe_action_is_exempt_from_the_latch(
        self, safety: SafetyManager
    ) -> None:
        safety.estop_controller.engage("test", ClientSource.HMI)
        assert authorize(safety, PermissionLevel.PASSIVE, allowed_during_estop=True) is None
        with pytest.raises(EstopActive):
            authorize(safety, PermissionLevel.PASSIVE)

    def test_arming_is_refused_while_the_latch_is_closed(self, safety: SafetyManager) -> None:
        safety.estop_controller.engage("test", ClientSource.HMI)
        with pytest.raises(EstopActive, match="acknowledge"):
            safety.arm(permission=PermissionLevel.POWER, ttl_s=60, source=ClientSource.FDCTL)

    def test_engaging_hands_back_every_lease_and_revokes_every_grant(
        self, armed_safety: SafetyManager
    ) -> None:
        armed_safety.leases.acquire(
            device_id="sim:visa:sim-psu-0",
            action="psu.output",
            owner=ClientSource.HMI,
            ttl_s=30,
        )
        leases = armed_safety.engage_estop(reason="test", source=ClientSource.HMI)

        assert len(leases) == 1
        assert armed_safety.snapshot().armed_permissions == []
        assert armed_safety.leases.active() == []

    def test_the_sweep_reports_expiry_of_both_kinds(
        self, safety_config: SafetyConfig
    ) -> None:
        emitted = []
        safety = SafetyManager(safety_config, emit=emitted.append)
        grant = safety.arm(
            permission=PermissionLevel.POWER, ttl_s=60, source=ClientSource.FDCTL
        )
        lease = safety.leases.acquire(
            device_id="psu-a", action="psu.output", owner=ClientSource.HMI, ttl_s=30
        )
        grant.expires_monotonic_ns = monotonic_ns() - 1
        lease.expires_monotonic_ns = monotonic_ns() - 1

        expired_grants, expired_leases = safety.sweep()

        assert [entry.grant_id for entry in expired_grants] == [grant.grant_id]
        assert [entry.lease_id for entry in expired_leases] == [lease.lease_id]
        types = [event.type for event in emitted]
        assert EventType.ARM_EXPIRED in types
        assert EventType.LEASE_EXPIRED in types

    def test_reset_returns_the_unit_to_its_boot_state(
        self, armed_safety: SafetyManager
    ) -> None:
        """Which is what the daemon does at startup, so nothing is inherited."""
        armed_safety.leases.acquire(
            device_id="psu-a", action="psu.output", owner=ClientSource.HMI, ttl_s=30
        )
        armed_safety.estop_controller.engage("test", ClientSource.HMI)

        armed_safety.reset()

        snapshot = armed_safety.snapshot()
        assert snapshot.state_word == "SAFE"
        assert snapshot.grants == []
        assert snapshot.leases == []
        assert not snapshot.estop_active
