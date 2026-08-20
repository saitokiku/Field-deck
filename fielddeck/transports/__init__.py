"""Real hardware transports.

Each module here wraps one physical bus behind the same
:class:`~fielddeck.drivers.base.Driver` contract the simulated devices use, so
the CLI, HMI, MCP surface and recipes cannot tell a real port from a simulated
one except by :attr:`DeviceDescriptor.simulated`.

Importing this package stays cheap and dependency-free on purpose: the optional
hardware libraries (pyserial, python-can, ...) are imported inside the functions
that need them, so a machine with none of them installed still imports FieldDeck
and still enumerates whatever it can see.
"""

from __future__ import annotations

from fielddeck.transports.serial_port import SerialDriver, discover_serial_drivers

__all__ = ["SerialDriver", "discover_serial_drivers"]
