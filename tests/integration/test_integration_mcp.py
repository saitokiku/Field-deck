"""The MCP server, driven the way an AI client drives it: over a pipe.

``fielddeck-mcp`` is started as a real subprocess against the daemon's
restricted socket, and spoken to in JSON-RPC on stdin/stdout.  That matters
more than it looks: the boundary being tested is a *process* boundary, and
importing the module in-process would quietly skip the part where the server
has to find the right socket, keep stdout clean of everything but protocol,
and turn a refusal into something a model can act on.

The one property worth stating out loud: this client never arms anything, and
cannot.  The denial below is the whole safety story of the AI surface.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from fielddeck.daemon.service import InstrumentDaemon

from .conftest import SIM_PSU

#: Startup, one round trip and shutdown of a subprocess: worth marking so a
#: fast local loop can skip it.
pytestmark = pytest.mark.slow

READ_TIMEOUT_S = 30.0


def mcp_binary() -> Path:
    """The installed console script, next to the interpreter running pytest."""
    return Path(sys.executable).parent / "fielddeck-mcp"


class McpPipe:
    """One JSON-RPC-over-stdio conversation with the server."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self._counter = 0

    async def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await self.process.stdin.drain()

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._counter += 1
        request_id = self._counter
        await self.send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        while True:
            message = await self._read()
            # Notifications and anything unsolicited are not this call's reply.
            if message.get("id") == request_id:
                return message

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def _read(self) -> dict[str, Any]:
        assert self.process.stdout is not None
        line = await asyncio.wait_for(self.process.stdout.readline(), timeout=READ_TIMEOUT_S)
        if not line:
            stderr = b""
            if self.process.stderr is not None:
                stderr = await self.process.stderr.read()
            raise AssertionError(f"fielddeck-mcp closed stdout; stderr was:\n{stderr.decode()}")
        return json.loads(line)


def tool_payload(reply: dict[str, Any]) -> dict[str, Any]:
    """The JSON document the server puts in the tool result's text content."""
    content = reply["result"]["content"]
    assert content[0]["type"] == "text"
    return json.loads(content[0]["text"])


@pytest.fixture
async def mcp(daemon: InstrumentDaemon) -> AsyncIterator[McpPipe]:
    binary = mcp_binary()
    if not binary.exists():  # pragma: no cover - depends on how the venv was built
        pytest.skip(f"fielddeck-mcp console script not installed at {binary}")
    assert daemon.ai_socket_path is not None

    environment = dict(os.environ)
    environment["FIELDDECK_MCP_SOCKET"] = str(daemon.ai_socket_path)
    environment["FIELDDECK_LOG_LEVEL"] = "WARNING"

    process = await asyncio.create_subprocess_exec(
        str(binary),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    pipe = McpPipe(process)
    try:
        yield pipe
    finally:
        if process.returncode is None:
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=10.0)
            except TimeoutError:  # pragma: no cover - a wedged server
                process.kill()
                await process.wait()


async def test_initialize_list_and_a_passive_call(mcp: McpPipe) -> None:
    handshake = await mcp.request(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "integration-test", "version": "0"},
        },
    )
    result = handshake["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"]
    # The server tells the model what it is allowed to do before it tries.
    assert "authorize" in result["instructions"].lower()

    await mcp.notify("notifications/initialized")

    listed = (await mcp.request("tools/list"))["result"]["tools"]
    names = {tool["name"] for tool in listed}
    assert {"fielddeck_status", "can_status", "session_events", "estop"} <= names
    # Nothing on this surface can create authority.
    assert not {name for name in names if name.startswith(("arm", "disarm", "estop_clear"))} - {
        "estop"
    }
    for tool in listed:
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema.get("additionalProperties") is False

    called = await mcp.request("tools/call", {"name": "fielddeck_status", "arguments": {}})
    assert called["result"]["isError"] is False
    payload = tool_payload(called)
    assert payload["ok"] is True
    assert payload["call"] == "system.status"
    assert payload["result"]["simulated"] is True
    assert payload["result"]["safety"]["state"] == "SAFE"


async def test_a_call_above_passive_is_denied_and_says_who_must_fix_it(mcp: McpPipe) -> None:
    """The assistant cannot arm, so the refusal has to name the human step."""
    await mcp.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})

    denied = await mcp.request(
        "tools/call",
        {"name": "scpi_query", "arguments": {"device": SIM_PSU, "command": "*IDN?"}},
    )
    # A refusal is a successful protocol exchange carrying an error the model
    # can act on, not a JSON-RPC error the client plumbing would swallow.
    assert "error" not in denied
    assert denied["result"]["isError"] is True

    payload = tool_payload(denied)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "PermissionDenied"
    assert payload["error"]["details"]["source"] == "claude"
    assert "fdctl arm query" in payload["error"]["details"]["hint"]
    assert "no command was sent to the device" in payload["error"]["preserved"]
    assert "human" in payload["next_step"].lower()


async def test_an_unknown_tool_and_a_malformed_line_cost_only_themselves(mcp: McpPipe) -> None:
    await mcp.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})

    unknown = await mcp.request("tools/call", {"name": "arm", "arguments": {}})
    assert unknown["error"]["code"] == -32602
    assert "arm" in unknown["error"]["message"]

    await mcp.send({"jsonrpc": "2.0", "id": 999, "method": "no.such.method"})
    # The server keeps serving after a bad request.
    still_alive = await mcp.request("ping")
    assert still_alive["result"] == {}
