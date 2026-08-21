"""The driver contract: how an action declares what it is allowed to do.

``@action`` is where an author answers "does this touch the DUT?", and
``state_changing`` has no default precisely so the question cannot be skipped.
The tests here cover the metadata machinery itself — collection, description,
and the one rule about permission resolvers that keeps a declared permission
trustworthy: a resolver may narrow, never widen.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from fielddeck.common.errors import UnsupportedCapability
from fielddeck.common.models import (
    ConnectionState,
    DeviceCapability,
    DeviceDescriptor,
    PermissionLevel,
    TransportKind,
)
from fielddeck.drivers.base import (
    ActionContext,
    DeviceParams,
    Driver,
    NoParams,
    action,
    collect_actions,
)
from fielddeck.safety.limits import DerivedLimitCheck, LimitCheck


class OutputParams(DeviceParams):
    enabled: bool


def _output_permission(params: Any) -> PermissionLevel:
    return PermissionLevel.POWER if params.enabled else PermissionLevel.PASSIVE


class ExampleDriver(Driver):
    """A miniature driver exercising every part of the declaration surface."""

    kind = TransportKind.VISA

    def __init__(self) -> None:
        super().__init__(
            DeviceDescriptor(
                id="sim:visa:example",
                kind=TransportKind.VISA,
                display_name="Example",
                capabilities=[DeviceCapability.OUTPUT, DeviceCapability.MEASURE],
                state=ConnectionState.READY,
                simulated=True,
            )
        )
        self.output = False
        self.safed = 0

    async def status(self) -> dict[str, Any]:
        return {"output": self.output}

    async def safe_state(self) -> dict[str, Any]:
        self.safed += 1
        self.output = False
        return {"device": self.device_id, "applied": True}

    @action(
        "example.status",
        permission=PermissionLevel.PASSIVE,
        params=DeviceParams,
        state_changing=False,
        description="Cached state; never transmits.",
        allowed_during_estop=True,
    )
    async def example_status(self, ctx: ActionContext, params: DeviceParams) -> dict[str, Any]:
        return await self.status()

    @action(
        "example.output",
        permission=PermissionLevel.POWER,
        params=OutputParams,
        state_changing=True,
        description="Enable or disable the output.",
        permission_resolver=_output_permission,
        requires_lease=True,
        allowed_during_estop=True,
        safe_state_note="The output is disabled on safe state.",
        limit_checks=(LimitCheck(param="voltage", quantity="psu.voltage"),),
        derived_limit_checks=(
            DerivedLimitCheck(quantity="psu.power", params=("voltage", "current")),
        ),
        timeout_s=5.0,
        cancelable=True,
    )
    async def example_output(self, ctx: ActionContext, params: OutputParams) -> dict[str, Any]:
        self.output = params.enabled
        return {"output": self.output}

    async def not_an_action(self) -> None:
        """Undecorated, and therefore invisible to the dispatcher."""


@pytest.fixture
def driver() -> ExampleDriver:
    return ExampleDriver()


class TestCollection:
    def test_only_decorated_methods_become_actions(self, driver: ExampleDriver) -> None:
        assert set(driver.actions()) == {"example.status", "example.output"}

    def test_collection_is_cached_so_specs_are_stable(self, driver: ExampleDriver) -> None:
        assert driver.actions() is driver.actions()

    def test_every_spec_is_bound_to_its_device(self, driver: ExampleDriver) -> None:
        for spec in driver.actions().values():
            assert spec.device_id == "sim:visa:example"

    def test_a_duplicate_action_name_is_a_programming_error(self) -> None:
        class Duplicated:
            @action("dupe", permission=PermissionLevel.PASSIVE, state_changing=False, description="a")
            async def first(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
                return {}

            @action("dupe", permission=PermissionLevel.PASSIVE, state_changing=False, description="b")
            async def second(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
                return {}

        with pytest.raises(RuntimeError, match="duplicate action dupe"):
            collect_actions(Duplicated())

    def test_the_description_falls_back_to_the_docstring(self) -> None:
        class Documented:
            @action("doc.example", permission=PermissionLevel.PASSIVE, state_changing=False)
            async def documented(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
                """The first line of the docstring becomes the description.

                The rest does not.
                """
                return {}

        spec = collect_actions(Documented())["doc.example"]
        assert spec.description == "The first line of the docstring becomes the description."


class TestSpecMetadata:
    def test_the_declaration_reaches_the_spec_intact(self, driver: ExampleDriver) -> None:
        spec = driver.actions()["example.output"]
        assert spec.permission is PermissionLevel.POWER
        assert spec.state_changing is True
        assert spec.requires_lease is True
        assert spec.allowed_during_estop is True
        assert spec.cancelable is True
        assert spec.timeout_s == 5.0
        assert spec.limit_checks[0].quantity == "psu.voltage"
        assert spec.derived_limit_checks[0].params == ("voltage", "current")

    def test_a_descriptor_is_what_a_client_sees(self, driver: ExampleDriver) -> None:
        descriptor = driver.actions()["example.output"].describe()
        assert descriptor.name == "example.output"
        assert descriptor.permission is PermissionLevel.POWER
        assert descriptor.state_changing is True
        assert descriptor.safe_state_note
        assert descriptor.params_schema["properties"]["enabled"]["type"] == "boolean"

    def test_a_descriptor_does_not_leak_the_limit_machinery(
        self, driver: ExampleDriver
    ) -> None:
        """Limits are the daemon's business; the client sees the permission."""
        descriptor = driver.actions()["example.output"].describe()
        assert not hasattr(descriptor, "limit_checks")

    def test_the_capture_convention_is_name_based(self) -> None:
        class Capturing:
            @action(
                "thing.capture",
                permission=PermissionLevel.PASSIVE,
                state_changing=False,
                description="Record what is already on the wire.",
            )
            async def capture(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
                return {}

            @action(
                "thing.status",
                permission=PermissionLevel.PASSIVE,
                state_changing=False,
                description="Report state.",
            )
            async def status(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
                return {}

        specs = collect_actions(Capturing())
        assert specs["thing.capture"].is_capture is True
        assert specs["thing.status"].is_capture is False


class TestPermissionResolution:
    def test_a_resolver_narrows_for_the_safe_direction(self, driver: ExampleDriver) -> None:
        spec = driver.actions()["example.output"]
        enabling = OutputParams(device="sim:visa:example", enabled=True)
        disabling = OutputParams(device="sim:visa:example", enabled=False)

        assert spec.effective_permission(enabling) is PermissionLevel.POWER
        assert spec.effective_permission(disabling) is PermissionLevel.PASSIVE

    def test_without_a_resolver_the_declared_permission_stands(
        self, driver: ExampleDriver
    ) -> None:
        spec = driver.actions()["example.status"]
        assert (
            spec.effective_permission(DeviceParams(device="sim:visa:example"))
            is PermissionLevel.PASSIVE
        )

    def test_a_resolver_that_widens_is_a_defect_and_says_so(self) -> None:
        """Clients plan for the declared worst case; widening past it would
        mean they were shown a permission they cannot trust."""

        class Widening:
            @action(
                "bad.action",
                permission=PermissionLevel.QUERY,
                state_changing=False,
                description="Claims QUERY, resolves to DESTRUCTIVE.",
                permission_resolver=lambda params: PermissionLevel.DESTRUCTIVE,
            )
            async def bad(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
                return {}

        spec = collect_actions(Widening())["bad.action"]
        with pytest.raises(RuntimeError, match="tried to widen"):
            spec.effective_permission(NoParams())


class TestDriverLifecycle:
    async def test_the_default_safe_state_is_a_no_op_that_says_so(self) -> None:
        class Passive(Driver):
            kind = TransportKind.LOGIC

            async def status(self) -> dict[str, Any]:
                return {}

        driver = Passive(
            DeviceDescriptor(id="sim:logic:x", kind=TransportKind.LOGIC, display_name="x")
        )
        result = await driver.safe_state()
        assert result == {"device": "sim:logic:x", "applied": False, "reason": "no outputs"}

    async def test_connect_and_disconnect_move_the_reported_state(
        self, driver: ExampleDriver
    ) -> None:
        await driver.disconnect()
        assert driver.descriptor.state is ConnectionState.DISCOVERED
        await driver.connect()
        assert driver.descriptor.state is ConnectionState.READY

    async def test_probe_is_false_only_for_an_absent_device(
        self, driver: ExampleDriver
    ) -> None:
        assert await driver.probe() is True
        driver._set_state(ConnectionState.ABSENT)
        assert await driver.probe() is False

    def test_a_missing_capability_is_refused_by_name(self, driver: ExampleDriver) -> None:
        driver.require(DeviceCapability.OUTPUT)
        with pytest.raises(UnsupportedCapability) as caught:
            driver.require(DeviceCapability.FLASH)
        assert caught.value.details["capability"] == str(DeviceCapability.FLASH)

    def test_the_busy_marker_names_what_is_running(self, driver: ExampleDriver) -> None:
        assert driver.busy_with is None
        driver._mark_busy("example.output")
        assert driver.busy_with == "example.output"


class TestActionContext:
    def test_the_context_offers_no_route_back_to_the_safety_manager(self) -> None:
        """A handler can read safety state; it cannot widen its own authority."""
        assert not hasattr(ActionContext, "arm")
        assert not hasattr(ActionContext, "grant")

    async def test_a_deadline_counts_down_and_never_goes_negative(self) -> None:
        from fielddeck.common.timebase import monotonic_ns

        ctx = ActionContext(
            source=None,  # type: ignore[arg-type]
            emit=lambda event: None,
            safety=None,  # type: ignore[arg-type]
            registry=None,  # type: ignore[arg-type]
        )
        assert ctx.remaining_s() is None

        ctx.deadline_monotonic_ns = monotonic_ns() + 1_000_000_000
        assert 0.0 < (ctx.remaining_s() or 0) <= 1.0

        ctx.deadline_monotonic_ns = monotonic_ns() - 1_000_000_000
        assert ctx.remaining_s() == 0.0

    async def test_cancellation_is_cooperative_and_typed(self) -> None:
        from fielddeck.common.errors import ActionCancelled

        ctx = ActionContext(
            source=None,  # type: ignore[arg-type]
            emit=lambda event: None,
            safety=None,  # type: ignore[arg-type]
            registry=None,  # type: ignore[arg-type]
            cancel=asyncio.Event(),
        )
        assert ctx.cancelled is False
        ctx.raise_if_cancelled()

        ctx.cancel.set()
        assert ctx.cancelled is True
        with pytest.raises(ActionCancelled):
            ctx.raise_if_cancelled()
