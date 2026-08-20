"""``instrumentd``: the single authority for every piece of hardware.

Re-exports here are **lazy** (PEP 562).  ``fdctl`` only ever needs
:class:`InstrumentClient`, but importing it executes this file, and eagerly
re-exporting the daemon dragged the dispatcher, the device registry, the
safety manager and the YAML config parser into every CLI invocation — about
85 ms on a development machine and closer to a third of a second on a Pi,
paid on every command, for code the client never runs.

The public names are unchanged; they are just resolved on first use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fielddeck.daemon.client import InstrumentClient, connect
    from fielddeck.daemon.dispatcher import Dispatcher
    from fielddeck.daemon.events import EventBus, Subscription
    from fielddeck.daemon.registry import DeviceRegistry
    from fielddeck.daemon.service import InstrumentDaemon

__all__ = [
    "DeviceRegistry",
    "Dispatcher",
    "EventBus",
    "InstrumentClient",
    "InstrumentDaemon",
    "Subscription",
    "connect",
]

_EXPORTS = {
    "InstrumentClient": "fielddeck.daemon.client",
    "connect": "fielddeck.daemon.client",
    "Dispatcher": "fielddeck.daemon.dispatcher",
    "EventBus": "fielddeck.daemon.events",
    "Subscription": "fielddeck.daemon.events",
    "DeviceRegistry": "fielddeck.daemon.registry",
    "InstrumentDaemon": "fielddeck.daemon.service",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_name), name)


def __dir__() -> list[str]:
    return sorted(__all__)
