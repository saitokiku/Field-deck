"""Device resolution: id, alias, or role — and never a coin flip.

``role:psu`` is a convenience that has to fail loudly when it is ambiguous.
Silently picking one of two power supplies is how the wrong DUT gets
energised, so two matching devices is an error that names both, not a choice
the registry makes on the operator's behalf.
"""

from __future__ import annotations

import pytest

from fielddeck.common.errors import DeviceNotFound, UnknownAction
from fielddeck.common.models import (
    ConnectionState,
    DeviceDescriptor,
    DeviceRole,
    PermissionLevel,
    TransportKind,
)
from fielddeck.daemon.registry import DeviceRegistry
from fielddeck.drivers.base import Driver, NoParams, action


class FakeDriver(Driver):
    """A driver with no hardware behind it, for registry tests only."""

    kind = TransportKind.SERIAL

    def __init__(self, device_id: str, *, roles: list[DeviceRole] | None = None) -> None:
        super().__init__(
            DeviceDescriptor(
                id=device_id,
                kind=TransportKind.SERIAL,
                display_name=device_id,
                roles=roles or [],
                state=ConnectionState.READY,
                simulated=True,
            )
        )

    async def status(self) -> dict[str, object]:
        return {"id": self.device_id}

    @action(
        "fake.status",
        permission=PermissionLevel.PASSIVE,
        state_changing=False,
        description="Report the fake device's state.",
    )
    async def fake_status(self, ctx, params: NoParams) -> dict[str, object]:
        return {"ok": True}


@pytest.fixture
def registry() -> DeviceRegistry:
    registry = DeviceRegistry(aliases={"bench-psu": "sim:visa:sim-psu-0"})
    registry.add(FakeDriver("sim:visa:sim-psu-0", roles=[DeviceRole.PSU]))
    registry.add(FakeDriver("sim:can:can0", roles=[DeviceRole.BUS]))
    registry.add(FakeDriver("sim:serial:sim-uart-0", roles=[DeviceRole.BUS]))
    return registry


class TestResolution:
    def test_a_device_id_resolves_to_itself(self, registry: DeviceRegistry) -> None:
        assert registry.resolve("sim:can:can0").device_id == "sim:can:can0"

    def test_a_configured_alias_resolves(self, registry: DeviceRegistry) -> None:
        assert registry.resolve("bench-psu").device_id == "sim:visa:sim-psu-0"

    def test_an_alias_pointing_at_a_missing_device_says_so(self) -> None:
        registry = DeviceRegistry(aliases={"bench-psu": "visa:usb:0957:1798:MY123"})
        with pytest.raises(DeviceNotFound) as caught:
            registry.resolve("bench-psu")
        assert caught.value.details["device_id"] == "visa:usb:0957:1798:MY123"

    def test_a_role_resolves_when_exactly_one_device_fills_it(
        self, registry: DeviceRegistry
    ) -> None:
        assert registry.resolve("role:psu").device_id == "sim:visa:sim-psu-0"

    def test_an_ambiguous_role_is_refused_with_the_candidates(
        self, registry: DeviceRegistry
    ) -> None:
        """Two buses, no guessing: the operator names one."""
        with pytest.raises(DeviceNotFound) as caught:
            registry.resolve("role:bus")
        assert sorted(caught.value.details["candidates"]) == [
            "sim:can:can0",
            "sim:serial:sim-uart-0",
        ]

    def test_an_unfilled_role_is_refused(self, registry: DeviceRegistry) -> None:
        with pytest.raises(DeviceNotFound, match="no device fills"):
            registry.resolve("role:scope")

    def test_an_unknown_role_lists_the_roles_that_exist(self, registry: DeviceRegistry) -> None:
        with pytest.raises(DeviceNotFound) as caught:
            registry.resolve("role:teleporter")
        assert str(DeviceRole.PSU) in caught.value.details["roles"]

    def test_an_unknown_reference_lists_what_is_known(self, registry: DeviceRegistry) -> None:
        with pytest.raises(DeviceNotFound) as caught:
            registry.resolve("ttyUSB0")
        assert "sim:can:can0" in caught.value.details["known"]
        assert "bench-psu" in caught.value.details["aliases"]

    def test_an_empty_reference_is_refused(self, registry: DeviceRegistry) -> None:
        with pytest.raises(DeviceNotFound, match="no device specified"):
            registry.resolve("")

    def test_try_resolve_swallows_only_the_not_found_case(self, registry: DeviceRegistry) -> None:
        assert registry.try_resolve("nope") is None
        assert registry.try_resolve("sim:can:can0") is not None

    def test_a_device_id_wins_over_an_alias_of_the_same_name(self) -> None:
        """An alias can never shadow a real device id."""
        registry = DeviceRegistry(aliases={"sim:can:can0": "sim:visa:sim-psu-0"})
        registry.add(FakeDriver("sim:can:can0"))
        assert registry.resolve("sim:can:can0").device_id == "sim:can:can0"


class TestInventory:
    def test_devices_can_be_added_and_removed(self, registry: DeviceRegistry) -> None:
        assert len(registry) == 3
        removed = registry.remove("sim:can:can0")
        assert removed is not None
        assert len(registry) == 2
        assert registry.remove("sim:can:can0") is None

    def test_adding_the_same_id_twice_replaces_the_driver(self, registry: DeviceRegistry) -> None:
        """Re-discovery of the same device must not double it in the inventory."""
        replacement = FakeDriver("sim:can:can0")
        registry.add(replacement)
        assert len(registry) == 3
        assert registry.get("sim:can:can0") is replacement

    def test_descriptors_describe_every_device(self, registry: DeviceRegistry) -> None:
        assert {descriptor.id for descriptor in registry.descriptors()} == {
            "sim:visa:sim-psu-0",
            "sim:can:can0",
            "sim:serial:sim-uart-0",
        }

    def test_aliases_can_be_replaced_wholesale(self, registry: DeviceRegistry) -> None:
        registry.set_aliases({"dut": "sim:can:can0"})
        assert registry.aliases == {"dut": "sim:can:can0"}
        with pytest.raises(DeviceNotFound):
            registry.resolve("bench-psu")


class TestActionLookup:
    def test_a_global_action_needs_no_device(self, registry: DeviceRegistry) -> None:
        driver = FakeDriver("global")
        registry.register_global({"system.status": driver.actions()["fake.status"]})
        spec, resolved = registry.lookup("system.status", {})
        assert spec.name == "fake.status"
        assert resolved is None

    def test_a_device_action_resolves_its_driver(self, registry: DeviceRegistry) -> None:
        spec, driver = registry.lookup("fake.status", {"device": "role:psu"})
        assert driver is not None and driver.device_id == "sim:visa:sim-psu-0"
        assert spec.device_id == "sim:visa:sim-psu-0"

    def test_a_device_action_without_a_device_says_what_is_missing(
        self, registry: DeviceRegistry
    ) -> None:
        with pytest.raises(UnknownAction) as caught:
            registry.lookup("fake.status", {})
        assert "needs a 'device' parameter" in str(caught.value)
        assert "fake.status" in caught.value.details["known_actions"]

    def test_an_action_the_device_does_not_provide_lists_what_it_does(
        self, registry: DeviceRegistry
    ) -> None:
        with pytest.raises(UnknownAction) as caught:
            registry.lookup("psu.output", {"device": "sim:can:can0"})
        assert caught.value.details["available"] == ["fake.status"]

    def test_registering_the_same_global_action_twice_is_a_programming_error(
        self, registry: DeviceRegistry
    ) -> None:
        driver = FakeDriver("global")
        specs = {"system.status": driver.actions()["fake.status"]}
        registry.register_global(specs)
        with pytest.raises(RuntimeError, match="duplicate global action"):
            registry.register_global(specs)

    def test_descriptors_can_be_narrowed_to_one_device(self, registry: DeviceRegistry) -> None:
        described = registry.action_descriptors(device_id="sim:can:can0")
        assert {descriptor.device_id for descriptor in described} == {"sim:can:can0"}

    def test_ready_only_hides_a_device_that_is_not_up(self, registry: DeviceRegistry) -> None:
        registry.get("sim:can:can0")._set_state(ConnectionState.FAULT)
        described = registry.action_descriptors(ready_only=True)
        assert "sim:can:can0" not in {descriptor.device_id for descriptor in described}


class TestSafeStateAll:
    async def test_every_driver_is_asked_and_one_failure_does_not_stop_the_rest(
        self, registry: DeviceRegistry
    ) -> None:
        class Broken(FakeDriver):
            async def safe_state(self) -> dict[str, object]:
                raise RuntimeError("the adapter is gone")

        registry.add(Broken("sim:serial:broken"))
        results = await registry.safe_state_all()

        assert len(results) == len(registry)
        failed = [entry for entry in results if entry.get("error")]
        assert [entry["device"] for entry in failed] == ["sim:serial:broken"]
