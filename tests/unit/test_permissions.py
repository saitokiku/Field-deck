"""Permission semantics: ordering is for display, authorization is by class.

The distinction this file protects is the one people get wrong.  ``POWER`` is
*ranked* above ``CONTROL`` so a recipe can say "the most dangerous thing in
here is POWER" and a banner can sort by severity.  It does not follow — and
must never come to follow — that holding POWER authorizes a CONTROL action.
"""

from __future__ import annotations

import itertools

import pytest

from fielddeck.common.models import (
    ArmGrant,
    ArmScope,
    ClientSource,
    OutputLease,
    PermissionLevel,
)

ORDER = [
    PermissionLevel.PASSIVE,
    PermissionLevel.QUERY,
    PermissionLevel.CONTROL,
    PermissionLevel.POWER,
    PermissionLevel.FLASH,
    PermissionLevel.DESTRUCTIVE,
]


class TestOrdering:
    def test_the_ladder_is_the_documented_one(self) -> None:
        assert [str(level) for level in PermissionLevel] == [str(level) for level in ORDER]
        assert [level.rank for level in ORDER] == [0, 1, 2, 3, 4, 5]

    @pytest.mark.parametrize(("lower", "higher"), list(itertools.pairwise(ORDER)))
    def test_comparisons_follow_the_ladder(
        self, lower: PermissionLevel, higher: PermissionLevel
    ) -> None:
        assert lower < higher
        assert lower <= higher
        assert higher > lower
        assert higher >= lower
        assert not higher < lower

    def test_a_level_compares_equal_to_itself(self) -> None:
        for level in ORDER:
            assert level <= level
            assert level >= level
            assert not level < level

    def test_max_picks_the_most_dangerous(self) -> None:
        """How a recipe plan decides what an operator has to arm."""
        assert max([PermissionLevel.QUERY, PermissionLevel.POWER, PermissionLevel.PASSIVE]) is (
            PermissionLevel.POWER
        )

    def test_comparing_against_a_bare_string_falls_back_to_alphabetical_order(self) -> None:
        """A trap worth pinning: compare enum members, never strings.

        ``PermissionLevel`` is a ``StrEnum``, so its ``__lt__`` returns
        ``NotImplemented`` for a plain string and Python falls back to
        ``str.__lt__``.  That comparison is alphabetical, and alphabetical
        order is not severity order — ``PASSIVE`` sorts above ``FLASH``.  Every
        call site in the package compares members or ``.rank``; this test is
        here so that stays a deliberate habit.
        """
        assert PermissionLevel.PASSIVE > "FLASH"  # alphabetical, and backwards
        assert PermissionLevel.PASSIVE < PermissionLevel.FLASH  # what it should mean
        assert PermissionLevel.PASSIVE.rank < PermissionLevel.FLASH.rank

    def test_the_value_is_the_wire_form(self) -> None:
        """Clients, logs and YAML all carry the uppercase name."""
        assert str(PermissionLevel.POWER) == "POWER"
        assert PermissionLevel("POWER") is PermissionLevel.POWER


class TestGrantRequirement:
    def test_passive_never_needs_a_grant(self) -> None:
        assert PermissionLevel.PASSIVE.requires_grant is False

    @pytest.mark.parametrize("level", [level for level in ORDER if level is not ORDER[0]])
    def test_every_other_class_needs_one(self, level: PermissionLevel) -> None:
        assert level.requires_grant is True


class TestWhoMayGrant:
    @pytest.mark.parametrize(
        ("source", "allowed"),
        [
            (ClientSource.HMI, True),
            (ClientSource.FDCTL, True),
            (ClientSource.RECIPE, False),
            (ClientSource.CLAUDE, False),
            (ClientSource.SYSTEM, False),
        ],
    )
    def test_only_a_human_facing_client_may_create_grants(
        self, source: ClientSource, allowed: bool
    ) -> None:
        assert source.may_create_grants is allowed

    def test_claude_is_not_in_the_granting_set(self) -> None:
        """Stated on its own because it is the whole point.

        An assistant that can widen its own authority is not an authorization
        system, and this assertion is what keeps that true after a refactor.
        """
        assert ClientSource.CLAUDE.may_create_grants is False
        assert ClientSource.RECIPE.may_create_grants is False


class TestScope:
    def test_an_unscoped_grant_covers_everything(self) -> None:
        scope = ArmScope()
        assert scope.matches(device_id="anything", action="psu.set")
        assert scope.matches(device_id=None, action="system.discover")
        assert scope.describe() == "all devices"

    def test_a_device_scope_matches_only_that_device(self) -> None:
        scope = ArmScope(kind="device", device_id="can:socketcan:can0")
        assert scope.matches(device_id="can:socketcan:can0", action="can.send")
        assert not scope.matches(device_id="can:socketcan:can1", action="can.send")

    def test_a_device_scope_never_covers_a_deviceless_action(self) -> None:
        """A grant naming one device must not authorize a global action."""
        scope = ArmScope(kind="device", device_id="psu-a")
        assert not scope.matches(device_id=None, action="flash.erase")

    def test_an_action_scope_ignores_the_device(self) -> None:
        scope = ArmScope(kind="action", action="psu.output")
        assert scope.matches(device_id="psu-a", action="psu.output")
        assert scope.matches(device_id=None, action="psu.output")
        assert not scope.matches(device_id="psu-a", action="psu.set")

    def test_scope_rejects_an_unknown_kind(self) -> None:
        with pytest.raises(ValueError, match="kind"):
            ArmScope(kind="everything")  # type: ignore[arg-type]


class TestGrantLifetime:
    def _grant(self, *, ttl_ns: int = 1_000_000_000, created: int = 1_000) -> ArmGrant:
        return ArmGrant(
            grant_id="arm-test",
            permission=PermissionLevel.POWER,
            created_by=ClientSource.FDCTL,
            created_monotonic_ns=created,
            created_utc_ns=0,
            expires_monotonic_ns=created + ttl_ns,
            ttl_s=ttl_ns / 1e9,
        )

    def test_a_grant_is_active_until_its_deadline_and_not_after(self) -> None:
        grant = self._grant()
        assert grant.is_active(grant.created_monotonic_ns)
        assert grant.is_active(grant.expires_monotonic_ns - 1)
        # The boundary belongs to the *expired* side: at the deadline it is gone.
        assert not grant.is_active(grant.expires_monotonic_ns)
        assert grant.remaining_s(grant.expires_monotonic_ns) == 0.0

    def test_remaining_never_goes_negative(self) -> None:
        grant = self._grant()
        assert grant.remaining_s(grant.expires_monotonic_ns + 10**12) == 0.0

    def test_revocation_is_immediate_and_independent_of_the_clock(self) -> None:
        grant = self._grant()
        grant.revoked = True
        assert not grant.is_active(grant.created_monotonic_ns)
        assert grant.remaining_s(grant.created_monotonic_ns) == 0.0

    def test_a_grant_records_who_created_it(self) -> None:
        """The audit trail answers "who armed this", not just "something did"."""
        assert self._grant().created_by is ClientSource.FDCTL


class TestLeaseLifetime:
    def _lease(self, *, ttl_ns: int = 1_000_000_000) -> OutputLease:
        return OutputLease(
            lease_id="lease-test",
            device_id="sim:visa:sim-psu-0",
            action="psu.output",
            owner=ClientSource.HMI,
            created_monotonic_ns=0,
            expires_monotonic_ns=ttl_ns,
            ttl_s=ttl_ns / 1e9,
            safe_action="psu.output",
            safe_params={"enabled": False},
        )

    def test_a_lease_expires_at_its_deadline(self) -> None:
        lease = self._lease()
        assert lease.is_active(lease.expires_monotonic_ns - 1)
        assert not lease.is_active(lease.expires_monotonic_ns)

    def test_a_released_lease_is_never_active(self) -> None:
        lease = self._lease()
        lease.released = True
        assert not lease.is_active(0)
        assert lease.remaining_s(0) == 0.0

    def test_a_lease_carries_the_action_that_makes_it_safe(self) -> None:
        lease = self._lease()
        assert lease.safe_action == "psu.output"
        assert lease.safe_params == {"enabled": False}
