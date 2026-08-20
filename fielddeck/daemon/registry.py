"""Device and action registry.

The registry answers two questions:

* "which driver does ``role:psu`` mean right now?"
* "what code runs for ``psu.output``, and what may it do?"

Device references are resolved here so drivers never have to care whether the
caller said ``bench-psu``, ``role:psu`` or ``visa:usb:0957:1798:MY123``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from fielddeck.common.errors import DeviceNotFound, UnknownAction
from fielddeck.common.models import (
    ActionDescriptor,
    ConnectionState,
    DeviceDescriptor,
    DeviceRole,
)
from fielddeck.drivers.base import ActionSpec, Driver

__all__ = ["DeviceRegistry"]


class DeviceRegistry:
    """Live inventory of drivers plus the daemon's own global actions."""

    def __init__(self, *, aliases: Mapping[str, str] | None = None) -> None:
        self._drivers: dict[str, Driver] = {}
        self._aliases: dict[str, str] = dict(aliases or {})
        self._global_actions: dict[str, ActionSpec] = {}

    # -- drivers -----------------------------------------------------------

    def add(self, driver: Driver) -> Driver:
        self._drivers[driver.device_id] = driver
        return driver

    def remove(self, device_id: str) -> Driver | None:
        return self._drivers.pop(device_id, None)

    def get(self, device_id: str) -> Driver | None:
        return self._drivers.get(device_id)

    @property
    def drivers(self) -> list[Driver]:
        return list(self._drivers.values())

    def descriptors(self) -> list[DeviceDescriptor]:
        return [driver.describe() for driver in self._drivers.values()]

    def set_aliases(self, aliases: Mapping[str, str]) -> None:
        self._aliases = dict(aliases)

    @property
    def aliases(self) -> dict[str, str]:
        return dict(self._aliases)

    # -- resolution --------------------------------------------------------

    def resolve(self, reference: str) -> Driver:
        """Turn any device reference into a driver, or explain why not.

        Accepts a device id, a configured alias, or ``role:<role>``.  A role
        that matches several ready devices is an error rather than a coin
        flip — silently picking one of two power supplies is how the wrong
        DUT gets energised.
        """
        if not reference:
            raise DeviceNotFound("no device specified", details={"reference": reference})

        driver = self._drivers.get(reference)
        if driver is not None:
            return driver

        aliased = self._aliases.get(reference)
        if aliased is not None:
            driver = self._drivers.get(aliased)
            if driver is None:
                raise DeviceNotFound(
                    f"alias {reference!r} points at {aliased!r}, which is not present",
                    details={"reference": reference, "device_id": aliased},
                )
            return driver

        if reference.startswith("role:"):
            role_name = reference.split(":", 1)[1]
            try:
                role = DeviceRole(role_name)
            except ValueError as exc:
                raise DeviceNotFound(
                    f"unknown device role {role_name!r}",
                    details={"reference": reference, "roles": [str(r) for r in DeviceRole]},
                ) from exc
            matches = [d for d in self._drivers.values() if role in d.descriptor.roles]
            if not matches:
                raise DeviceNotFound(
                    f"no device fills the {role} role",
                    details={"reference": reference, "role": str(role)},
                )
            if len(matches) > 1:
                raise DeviceNotFound(
                    f"{len(matches)} devices fill the {role} role; name one explicitly",
                    details={
                        "reference": reference,
                        "candidates": [d.device_id for d in matches],
                    },
                )
            return matches[0]

        raise DeviceNotFound(
            f"no device matches {reference!r}",
            details={
                "reference": reference,
                "known": sorted(self._drivers),
                "aliases": sorted(self._aliases),
            },
        )

    def try_resolve(self, reference: str) -> Driver | None:
        try:
            return self.resolve(reference)
        except DeviceNotFound:
            return None

    # -- actions -----------------------------------------------------------

    def register_global(self, specs: Mapping[str, ActionSpec]) -> None:
        """Daemon-level actions: ``system.*``, ``session.*``, ``tools.*``."""
        for name, spec in specs.items():
            if name in self._global_actions:
                raise RuntimeError(f"duplicate global action {name}")
            self._global_actions[name] = spec

    def lookup(self, action: str, params: Mapping[str, object]) -> tuple[ActionSpec, Driver | None]:
        """Find the spec for one call, resolving the device when needed."""
        spec = self._global_actions.get(action)
        if spec is not None:
            return spec, None

        reference = params.get("device")
        if not isinstance(reference, str) or not reference:
            known = sorted(set(self._global_actions) | self._all_device_action_names())
            raise UnknownAction(
                f"action {action!r} is a device action and needs a 'device' parameter",
                details={"action": action, "known_actions": known},
            )

        driver = self.resolve(reference)
        spec = driver.actions().get(action)
        if spec is None:
            raise UnknownAction(
                f"{driver.device_id} does not provide {action!r}",
                details={
                    "action": action,
                    "device_id": driver.device_id,
                    "available": sorted(driver.actions()),
                },
            )
        return spec, driver

    def _all_device_action_names(self) -> set[str]:
        names: set[str] = set()
        for driver in self._drivers.values():
            names |= set(driver.actions())
        return names

    def action_descriptors(
        self, *, device_id: str | None = None, ready_only: bool = False
    ) -> list[ActionDescriptor]:
        out: list[ActionDescriptor] = []
        if device_id is None:
            out.extend(spec.describe() for spec in self._global_actions.values())
        for driver in self._drivers.values():
            if device_id is not None and driver.device_id != device_id:
                continue
            if ready_only and driver.descriptor.state is not ConnectionState.READY:
                continue
            out.extend(driver.action_descriptors())
        return sorted(out, key=lambda d: (d.name, d.device_id or ""))

    @property
    def global_actions(self) -> dict[str, ActionSpec]:
        return dict(self._global_actions)

    async def safe_state_all(self) -> list[dict[str, object]]:
        """Ask every driver for its safe state.  Used at boot and on ESTOP."""
        results: list[dict[str, object]] = []
        for driver in self._drivers.values():
            try:
                results.append(await driver.safe_state())
            except Exception as exc:
                results.append({"device": driver.device_id, "applied": False, "error": str(exc)})
        return results

    def __len__(self) -> int:
        return len(self._drivers)

    def __iter__(self) -> Iterable[Driver]:  # pragma: no cover - convenience
        return iter(self._drivers.values())
