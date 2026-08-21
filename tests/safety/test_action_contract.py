"""The action contract, as a property over every action that exists.

``state_changing`` is the flag the whole safety model reads: the dispatcher
takes an exclusive device lock for it, the compiler counts it, the HMI warns on
it, and an operator trusts it.  An action that changes hardware state while
claiming ``state_changing=False`` — or that hides a transmit inside something
called ``status`` — is a safety defect, not a style problem.

So this file does not enumerate actions.  It walks the whole package, and the
whole live registry, and asserts the invariants over whatever it finds.  A
driver added next year is covered the day it is written, and an action that
breaks one of these rules fails here rather than on a bench.

Two passes, deliberately:

* a **static** pass over every ``@action``-decorated method in the package,
  which reaches drivers that need hardware and can never be instantiated in CI
  (SocketCAN, pyserial, pymodbus, VISA, GPIO);
* a **live** pass over the running daemon's registry, which reaches actions
  registered at runtime by a subsystem factory and never seen by a decorator
  scan (``recipe.run`` and its permission resolver, for one).
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import Any

import pytest
from pydantic import BaseModel

import fielddeck
from fielddeck.common.models import PermissionLevel
from fielddeck.daemon.dispatcher import MAX_TIMEOUT_S
from fielddeck.daemon.service import InstrumentDaemon
from fielddeck.drivers.base import ActionSpec, Driver

#: Client surfaces.  They drive the daemon over a socket and declare no
#: actions of their own; importing them would drag in textual and typer for
#: nothing.  Everything that can reach hardware is outside this set.
_CLIENT_PACKAGES = ("fielddeck.ui", "fielddeck.cli", "fielddeck.mcp")

#: A scan that silently found nothing would make every assertion below vacuous.
_MINIMUM_DECLARED_ACTIONS = 90

#: Verbs that promise an operator the action only looks.  None of them may be
#: attached to something that changes state — that is the specific lie the
#: contract exists to prevent.
_INSPECTION_WORDS = (
    "status",
    "list",
    "get",
    "read",
    "inspect",
    "discover",
    "summary",
    "stats",
    "describe",
    "measure",
    "identify",
    "query",
    "info",
    "validate",
    "dry_run",
    "events",
    "window",
    "listen",
    "monitor",
    "capture",
    "decode",
)


def _declared_actions() -> dict[str, Any]:
    """Every ``@action``-decorated method in the package, by where it lives."""
    found: dict[str, Any] = {}
    modules = []
    for module in pkgutil.walk_packages(fielddeck.__path__, prefix="fielddeck."):
        if any(
            module.name == package or module.name.startswith(f"{package}.")
            for package in _CLIENT_PACKAGES
        ):
            continue
        # Every module in this package must import with no optional hardware
        # library installed; a failure here is itself a defect worth failing on.
        modules.append(importlib.import_module(module.name))

    for module in modules:
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module.__name__:
                continue
            for attribute, member in vars(obj).items():
                meta = getattr(member, "_fielddeck_action", None)
                if meta is not None:
                    found[f"{obj.__module__}.{obj.__qualname__}.{attribute}"] = meta
    return found


def _live_specs(daemon: InstrumentDaemon) -> dict[str, ActionSpec]:
    """Everything the running daemon would dispatch, global and per device."""
    specs: dict[str, ActionSpec] = dict(daemon.registry.global_actions)
    for driver in daemon.registry.drivers:
        for name, spec in driver.actions().items():
            specs[f"{driver.device_id}:{name}"] = spec
    return specs


@pytest.fixture(scope="module")
def declared() -> dict[str, Any]:
    actions = _declared_actions()
    assert len(actions) >= _MINIMUM_DECLARED_ACTIONS, (
        f"only {len(actions)} declared actions found; the package walk is probably broken, "
        "which would make every invariant in this file vacuous"
    )
    return actions


def _check(where: str, action: Any) -> None:
    """The contract, applied to one action's metadata.

    ``action`` is either an ``_ActionMeta`` (static pass) or an
    :class:`~fielddeck.drivers.base.ActionSpec` (live pass); the fields this
    reads are common to both by construction.
    """
    name = action.name
    permission: PermissionLevel = action.permission
    label = f"{name} ({where})"

    # 1. Anything above QUERY touches the DUT, so it must say so.
    if permission.rank > PermissionLevel.QUERY.rank:
        assert action.state_changing, (
            f"{label} requires {permission} but claims state_changing=False. "
            "Anything above QUERY changes what the hardware is doing."
        )

    # 2. The converse: a state-changing action may not be declared PASSIVE
    #    unless it is a declared safe-state action, which has to say both that
    #    it is allowed during a stop and what its safe state is.
    if action.state_changing and permission is PermissionLevel.PASSIVE:
        assert action.allowed_during_estop and action.safe_state_note, (
            f"{label} changes state but is declared PASSIVE without being a declared "
            "safe-state action (allowed_during_estop plus safe_state_note)."
        )

    # 3. An inspection verb must not be attached to a state change.
    leaf = name.rsplit(".", 1)[-1]
    if action.state_changing:
        offending = [word for word in _INSPECTION_WORDS if word in leaf]
        assert not offending, (
            f"{label} is state_changing but is named after {offending}. Rename it: an "
            "operator reads the name, not the flag."
        )

    # 4. A capture records what is already on the wire.  One that transmits
    #    would make its own evidence.
    if name.endswith(".capture"):
        assert not action.state_changing, f"{label} is a capture and must not transmit"

    # 5. A sustained output needs a dead-man handle, and the handle is only
    #    meaningful if the action says what "safe" means for it.
    if action.requires_lease:
        assert action.state_changing, f"{label} takes an output lease but changes nothing"
        assert action.safe_state_note, (
            f"{label} takes an output lease without a safe_state_note; the operator cannot "
            "tell what happens when it lapses"
        )

    # 6. A state change permitted during a latched stop must be able to resolve
    #    to PASSIVE for the safe direction — that is what makes it safe to
    #    waive the latch, rather than a blanket exemption.
    if action.state_changing and action.allowed_during_estop:
        assert action.permission_resolver is not None, (
            f"{label} changes state during a latched ESTOP with no permission resolver; "
            "only the direction that makes hardware safer may be exempt."
        )

    # 7. Self-description an operator and a client both depend on.
    assert action.description.strip(), f"{label} has no description"
    assert 0 < action.timeout_s <= MAX_TIMEOUT_S, f"{label} has timeout {action.timeout_s}"

    # 8. Parameters are validated by a strict model, so nothing unvalidated can
    #    reach a driver through an extra key.
    model = action.params_model
    assert isinstance(model, type) and issubclass(model, BaseModel), f"{label} has no params model"
    assert model.model_config.get("extra") == "forbid", (
        f"{label} accepts unknown parameters; a silently ignored current_limit is how a DUT "
        "gets the previous setpoint"
    )


def test_every_declared_action_keeps_the_contract(declared: dict[str, Any]) -> None:
    for where, meta in sorted(declared.items()):
        _check(where, meta)


def test_every_live_action_keeps_the_contract(daemon: InstrumentDaemon) -> None:
    specs = _live_specs(daemon)
    assert len(specs) >= 60, "the simulated registry is suspiciously small"
    for where, spec in sorted(specs.items()):
        _check(where, spec)


def test_the_live_registry_is_covered_by_the_static_scan(
    declared: dict[str, Any], daemon: InstrumentDaemon
) -> None:
    """Anything the daemon can dispatch should also be findable by name.

    The scan is what covers drivers CI cannot instantiate, so an action that
    exists only at runtime is worth knowing about: it means the static pass has
    a blind spot.
    """
    static_names = {meta.name for meta in declared.values()}
    live_names = {spec.name for spec in _live_specs(daemon).values()}
    runtime_only = live_names - static_names
    # ``recipe.run`` and friends are built by a factory with a bound resolver
    # rather than declared on a class, which is why they are listed here.
    assert runtime_only <= {"recipe.run"}, f"unexpected runtime-only actions: {runtime_only}"


def test_a_permission_resolver_may_only_narrow(daemon: InstrumentDaemon) -> None:
    """The declared permission is the worst case a client can plan for."""
    for where, spec in _live_specs(daemon).items():
        if spec.permission_resolver is None:
            continue
        assert spec.permission is not PermissionLevel.PASSIVE, (
            f"{where} has a resolver but nothing to narrow from"
        )


def test_psu_output_resolves_to_passive_only_for_the_safe_direction(
    daemon: InstrumentDaemon,
) -> None:
    """The reference case, spelled out, because everything else copies it."""
    driver = daemon.registry.resolve("sim:visa:sim-psu-0")
    spec = driver.actions()["psu.output"]

    enabling = spec.params_model.model_validate({"device": driver.device_id, "enabled": True})
    disabling = spec.params_model.model_validate({"device": driver.device_id, "enabled": False})

    assert spec.effective_permission(enabling) is PermissionLevel.POWER
    assert spec.effective_permission(disabling) is PermissionLevel.PASSIVE


def test_every_driver_with_a_state_changing_action_can_be_made_safe(
    daemon: InstrumentDaemon,
) -> None:
    """A device that can act must define what stopping means for it.

    ``Driver.safe_state`` defaults to a no-op for devices with no outputs.  A
    driver that can energise, drive or transmit and does *not* override it
    would report ``applied: false, reason: no outputs`` to an emergency stop.
    """
    for driver in daemon.registry.drivers:
        changes_state = any(spec.state_changing for spec in driver.actions().values())
        if not changes_state:
            continue
        assert type(driver).safe_state is not Driver.safe_state, (
            f"{driver.device_id} has state-changing actions but inherits the no-op safe_state"
        )


async def test_every_driver_reports_a_safe_state_result(daemon: InstrumentDaemon) -> None:
    """Safe state is asked of every device, and every device answers."""
    results = await daemon.dispatcher.apply_safe_state(reason="contract test")
    assert {entry["device"] for entry in results} == {
        driver.device_id for driver in daemon.registry.drivers
    }
    for entry in results:
        assert "applied" in entry
