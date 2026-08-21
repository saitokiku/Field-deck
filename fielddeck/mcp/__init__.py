"""The FieldDeck MCP server: Claude's read-only window onto the instrument.

Claude is one of FieldDeck's four clients, and the least privileged.  This
package gives it the same backend the HMI, ``fdctl`` and recipes use — no
private path to hardware, no separate code that "just reads" a port — and
takes away the ability to authorize anything:

* :mod:`~fielddeck.mcp.tools` is the catalogue: what Claude can ask for, in
  what shape, and what each request costs in permission terms.  The
  descriptions are written for a model, so they say plainly which tools listen
  and which transmit.
* :mod:`~fielddeck.mcp.server` speaks MCP over stdio and forwards to
  ``instrumentd`` on the restricted socket, where the daemon stamps every
  request ``source=claude`` and refuses the arming methods at the transport.

Nothing in this package imports a transport, opens a device file or runs a
subprocess.  If a change here ever needs one of those, the change belongs in
the daemon instead — that is where hardware access can be authorized, limited
and audited.

Re-exports are lazy (PEP 562), as elsewhere in the package: reading the tool
catalogue should not drag in the RPC client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fielddeck.mcp.server import (
        MCP_PROTOCOL_VERSION,
        DaemonLink,
        McpServer,
        main,
        restricted_socket_path,
    )
    from fielddeck.mcp.tools import TOOLS, ToolDef, tool_by_name, tool_list_payload

__all__ = [
    "MCP_PROTOCOL_VERSION",
    "TOOLS",
    "DaemonLink",
    "McpServer",
    "ToolDef",
    "main",
    "restricted_socket_path",
    "tool_by_name",
    "tool_list_payload",
]

_EXPORTS = {
    "MCP_PROTOCOL_VERSION": "fielddeck.mcp.server",
    "DaemonLink": "fielddeck.mcp.server",
    "McpServer": "fielddeck.mcp.server",
    "main": "fielddeck.mcp.server",
    "restricted_socket_path": "fielddeck.mcp.server",
    "TOOLS": "fielddeck.mcp.tools",
    "ToolDef": "fielddeck.mcp.tools",
    "tool_by_name": "fielddeck.mcp.tools",
    "tool_list_payload": "fielddeck.mcp.tools",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_name), name)


def __dir__() -> list[str]:
    return sorted(__all__)
