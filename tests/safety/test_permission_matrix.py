"""The permission matrix: exact-class, revocable, never self-granted.

These are the tests that would have to fail before FieldDeck could hurt
something.  If you change one of them, change it deliberately.

This module builds its own :class:`SafetyManager` rather than taking the
shared fixture, so that the authorization rules are pinned to the policy
written here and cannot drift when a fixture elsewhere gains a new default.
"""

from __future__ import annotations

import pytest

from fielddeck.common.errors import EstopActive, PermissionDenied
from fielddeck.common.models import ArmScope, ClientSource, PermissionLevel
from fielddeck.common.timebase import monotonic_ns
from fielddeck.common.config import SafetyConfig
from fielddeck.safety.manager import SafetyManager

GRANTABLE = [p for p in PermissionLevel if p.requires_grant]


@pytest.fixture
def safety() -> SafetyManager:
    """A manager on stock policy, armed with nothing."""
    return SafetyManager(SafetyConfig.defaults())


@pytest.fixture
def armed_safety(safety: SafetyManager) -> SafetyManager:
    """Every grantable class armed at once.

    Only for tests whose subject is something other than whether a permission
    is enforced -- never to shortcut authorization in a test about
    authorization.
    """
    for permission in GRANTABLE:
        safety.arm(permission=permission, ttl_s=300, source=ClientSource.FDCTL)
    return safety


def authorize(safety: SafetyManager, permission: PermissionLevel, **kwargs: object):
    return safety.authorize(
        action=kwargs.pop("action", "test.action"),  # type: ignore[arg-type]
        permission=permission,
        device_id=kwargs.pop("device_id", None),  # type: ignore[arg-type]
        source=kwargs.pop("source", ClientSource.FDCTL),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


class TestBootState:
    def test_boot_state_is_passive(self, safety: SafetyManager) -> None:
        snapshot = safety.snapshot()
        assert snapshot.armed_permissions == []
        assert snapshot.state_word == "SAFE"
        assert not snapshot.estop_active

    def test_passive_needs_no_grant(self, safety: SafetyManager) -> None:
        assert authorize(safety, PermissionLevel.PASSIVE) is None

    @pytest.mark.parametrize("permission", GRANTABLE)
    def test_every_other_class_is_refused_at_boot(
        self, safety: SafetyManager, permission: PermissionLevel
    ) -> None:
        with pytest.raises(PermissionDenied) as caught:
            authorize(safety, permission)
        # The refusal has to tell the operator how to proceed, and has to say
        # that nothing reached the device.
        assert "fdctl arm" in str(caught.value)
        assert caught.value.preserved == "no command was sent to the device"


class TestExactClass:
    """A POWER grant is not a superset of a CONTROL grant."""

    @pytest.mark.parametrize("granted", GRANTABLE)
    @pytest.mark.parametrize("requested", GRANTABLE)
    def test_only_the_matching_class_authorizes(
        self, safety: SafetyManager, granted: PermissionLevel, requested: PermissionLevel
    ) -> None:
        safety.arm(permission=granted, ttl_s=60, source=ClientSource.FDCTL)
        if granted is requested:
            grant = authorize(safety, requested)
            assert grant is not None
            assert grant.permission is requested
        else:
            with pytest.raises(PermissionDenied):
                authorize(safety, requested)

    def test_higher_rank_does_not_imply_lower(self, safety: SafetyManager) -> None:
        """The specific case people expect to work, and which must not."""
        safety.arm(permission=PermissionLevel.DESTRUCTIVE, ttl_s=60, source=ClientSource.FDCTL)
        assert PermissionLevel.DESTRUCTIVE > PermissionLevel.QUERY
        with pytest.raises(PermissionDenied):
            authorize(safety, PermissionLevel.QUERY)

    def test_arming_several_classes_is_one_command_not_inheritance(
        self, safety: SafetyManager
    ) -> None:
        for permission in (PermissionLevel.CONTROL, PermissionLevel.POWER):
            safety.arm(permission=permission, ttl_s=60, source=ClientSource.FDCTL)
        assert authorize(safety, PermissionLevel.CONTROL) is not None
        assert authorize(safety, PermissionLevel.POWER) is not None
        with pytest.raises(PermissionDenied):
            authorize(safety, PermissionLevel.FLASH)


class TestScope:
    def test_device_scope_excludes_other_devices(self, safety: SafetyManager) -> None:
        safety.arm(
            permission=PermissionLevel.POWER,
            ttl_s=60,
            source=ClientSource.FDCTL,
            scope=ArmScope(kind="device", device_id="psu-a"),
        )
        assert authorize(safety, PermissionLevel.POWER, device_id="psu-a") is not None
        with pytest.raises(PermissionDenied):
            authorize(safety, PermissionLevel.POWER, device_id="psu-b")

    def test_action_scope_excludes_other_actions(self, safety: SafetyManager) -> None:
        safety.arm(
            permission=PermissionLevel.POWER,
            ttl_s=60,
            source=ClientSource.FDCTL,
            scope=ArmScope(kind="action", action="psu.output"),
        )
        assert authorize(safety, PermissionLevel.POWER, action="psu.output") is not None
        with pytest.raises(PermissionDenied):
            authorize(safety, PermissionLevel.POWER, action="psu.set")

    def test_device_scope_refuses_a_deviceless_action(self, safety: SafetyManager) -> None:
        """A grant naming one device must not cover a global action."""
        safety.arm(
            permission=PermissionLevel.POWER,
            ttl_s=60,
            source=ClientSource.FDCTL,
            scope=ArmScope(kind="device", device_id="psu-a"),
        )
        with pytest.raises(PermissionDenied):
            authorize(safety, PermissionLevel.POWER, device_id=None)


class TestExpiry:
    def test_a_grant_stops_working_when_it_lapses(self, safety: SafetyManager) -> None:
        grant = safety.arm(
            permission=PermissionLevel.POWER, ttl_s=0.01, source=ClientSource.FDCTL
        )
        assert grant.is_active(monotonic_ns())
        # Rather than sleeping, ask the grant about a moment in its future.
        future = grant.expires_monotonic_ns + 1
        assert not grant.is_active(future)
        assert grant.remaining_s(future) == 0.0

    def test_ttl_is_capped_by_policy(self, safety: SafetyManager) -> None:
        cap = safety.config.max_ttl(PermissionLevel.POWER)
        grant = safety.arm(
            permission=PermissionLevel.POWER, ttl_s=cap * 100, source=ClientSource.FDCTL
        )
        assert grant.ttl_s <= cap

    def test_revoked_grant_stops_authorizing(self, safety: SafetyManager) -> None:
        grant = safety.arm(permission=PermissionLevel.POWER, ttl_s=60, source=ClientSource.FDCTL)
        assert authorize(safety, PermissionLevel.POWER) is not None
        safety.disarm(source=ClientSource.FDCTL, grant_id=grant.grant_id)
        with pytest.raises(PermissionDenied):
            authorize(safety, PermissionLevel.POWER)

    def test_disarm_without_an_id_revokes_everything(self, safety: SafetyManager) -> None:
        for permission in GRANTABLE:
            safety.arm(permission=permission, ttl_s=60, source=ClientSource.FDCTL)
        revoked = safety.disarm(source=ClientSource.FDCTL)
        assert len(revoked) == len(GRANTABLE)
        assert safety.snapshot().armed_permissions == []


class TestEstop:
    def test_estop_latches_and_blocks_even_armed_actions(self, armed_safety: SafetyManager) -> None:
        armed_safety.engage_estop(reason="operator pressed stop", source=ClientSource.HMI)
        for permission in GRANTABLE:
            with pytest.raises(EstopActive):
                authorize(armed_safety, permission)

    def test_estop_revokes_grants(self, armed_safety: SafetyManager) -> None:
        assert armed_safety.snapshot().armed_permissions
        armed_safety.engage_estop(reason="test", source=ClientSource.HMI)
        assert armed_safety.snapshot().armed_permissions == []

    def test_estop_does_not_block_a_passive_action(self, safety: SafetyManager) -> None:
        """Reading the state of the world stays possible after a stop.

        An operator who cannot see what happened cannot decide what to do
        next, so PASSIVE work continues.
        """
        safety.engage_estop(reason="test", source=ClientSource.HMI)
        with pytest.raises(EstopActive):
            authorize(safety, PermissionLevel.PASSIVE)
        # ...unless the action declares itself safe during a stop, which is how
        # "turn the output off" and "read status" stay reachable.
        assert (
            authorize(safety, PermissionLevel.PASSIVE, allowed_during_estop=True) is None
        )

    def test_cannot_arm_while_latched(self, safety: SafetyManager) -> None:
        safety.engage_estop(reason="test", source=ClientSource.HMI)
        with pytest.raises(EstopActive):
            safety.arm(permission=PermissionLevel.POWER, ttl_s=60, source=ClientSource.FDCTL)

    def test_acknowledging_clears_the_latch(self, safety: SafetyManager) -> None:
        safety.engage_estop(reason="test", source=ClientSource.HMI)
        safety.acknowledge_estop(source=ClientSource.HMI)
        assert not safety.snapshot().estop_active
        # And the operator still has to re-arm; clearing a stop is not arming.
        with pytest.raises(PermissionDenied):
            authorize(safety, PermissionLevel.POWER)


class TestDeniedByPolicy:
    def test_a_class_disabled_by_policy_cannot_be_used_even_when_armed(
        self, safety: SafetyManager
    ) -> None:
        safety.config.denied_permissions.append(PermissionLevel.DESTRUCTIVE)
        safety.arm(permission=PermissionLevel.DESTRUCTIVE, ttl_s=60, source=ClientSource.FDCTL)
        with pytest.raises(PermissionDenied) as caught:
            authorize(safety, PermissionLevel.DESTRUCTIVE)
        assert "safety policy" in str(caught.value)

    def test_policy_denial_is_checked_before_estop(self, safety: SafetyManager) -> None:
        """Order matters for the error message the operator reads."""
        safety.config.denied_permissions.append(PermissionLevel.FLASH)
        safety.engage_estop(reason="test", source=ClientSource.HMI)
        with pytest.raises(PermissionDenied):
            authorize(safety, PermissionLevel.FLASH)


class TestWhoMayGrant:
    @pytest.mark.parametrize(
        ("source", "allowed"),
        [
            (ClientSource.HMI, True),
            (ClientSource.FDCTL, True),
            (ClientSource.CLAUDE, False),
            (ClientSource.RECIPE, False),
            (ClientSource.SYSTEM, False),
        ],
    )
    def test_only_a_human_facing_client_may_create_grants(
        self, source: ClientSource, allowed: bool
    ) -> None:
        assert source.may_create_grants is allowed

    def test_claude_is_not_in_the_granting_set(self) -> None:
        """Stated as its own test because it is the whole point.

        An assistant that can widen its own authority is not an authorization
        system, and this assertion is what keeps that true after a refactor.
        """
        assert ClientSource.CLAUDE.may_create_grants is False
