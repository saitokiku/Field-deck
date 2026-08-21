"""``fdctl`` as a script, against a live daemon.

Run as a subprocess rather than through typer's test runner, because the
contract this file is about is the one a shell script depends on: the exit
code, and whether ``--json`` really put a single machine-readable document on
stdout with nothing else beside it.  Neither of those survives being tested
in-process.

The exit codes come from :data:`fielddeck.common.errors.EXIT_CODES` and are
part of the interface: a script that branches on 3 (denied) versus 4 (over a
limit) versus 9 (no daemon) has to keep working.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from fielddeck.common.errors import EXIT_CODES, ErrorCode
from fielddeck.daemon.service import InstrumentDaemon

from .conftest import SIM_CAN, SIM_PSU

pytestmark = pytest.mark.slow

RUN_TIMEOUT_S = 60.0


@dataclass(frozen=True, slots=True)
class Run:
    code: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        """The one document ``--json`` promises.  Fails loudly if it is not."""
        return json.loads(self.stdout)


def fdctl_binary() -> Path:
    return Path(sys.executable).parent / "fdctl"


async def fdctl(socket: Path | str, *args: str) -> Run:
    binary = fdctl_binary()
    if not binary.exists():  # pragma: no cover - depends on how the venv was built
        pytest.skip(f"fdctl console script not installed at {binary}")
    process = await asyncio.create_subprocess_exec(
        str(binary),
        "--socket",
        str(socket),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        env=dict(os.environ),
    )
    out, err = await asyncio.wait_for(process.communicate(), timeout=RUN_TIMEOUT_S)
    assert process.returncode is not None
    return Run(process.returncode, out.decode(), err.decode())


async def test_json_status_is_one_clean_document(daemon: InstrumentDaemon) -> None:
    run = await fdctl(daemon.socket_path, "--json", "status")
    assert run.code == EXIT_CODES["ok"]
    # No ANSI, no commentary, nothing on stderr to corrupt a pipe.
    assert "\x1b" not in run.stdout
    assert run.stderr == ""

    payload = run.json()
    assert payload["simulated"] is True
    assert payload["safety"]["state"] == "SAFE"
    assert {device["id"] for device in payload["device_list"]} >= {SIM_CAN, SIM_PSU}


async def test_devices_and_actions_report_what_the_daemon_says(
    daemon: InstrumentDaemon,
) -> None:
    devices = (await fdctl(daemon.socket_path, "--json", "devices")).json()
    assert {device["id"] for device in devices["devices"]} >= {SIM_CAN, SIM_PSU}

    listed = (await fdctl(daemon.socket_path, "--json", "actions", "--device", SIM_PSU)).json()
    permissions = {row["name"]: row["permission"] for row in listed["actions"]}
    # The CLI displays the daemon's own classification; it must not invent one.
    assert permissions["psu.set"] == "POWER"
    assert permissions["psu.measure"] == "QUERY"
    assert permissions["psu.status"] == "PASSIVE"


async def test_denials_and_limits_have_distinct_exit_codes(daemon: InstrumentDaemon) -> None:
    denied = await fdctl(
        daemon.socket_path, "--json", "can", "send", SIM_CAN, "--id", "0x123", "--data", "0102"
    )
    assert denied.code == EXIT_CODES[str(ErrorCode.PERMISSION_DENIED)] == 3
    payload = denied.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == str(ErrorCode.PERMISSION_DENIED)
    assert "no command was sent to the device" in payload["error"]["preserved"]

    # Arming is a separate, deliberate act, and it persists in the daemon
    # rather than in this process.
    armed = await fdctl(daemon.socket_path, "--json", "arm", "power", "query", "--ttl", "60")
    assert armed.code == 0

    over_limit = await fdctl(
        daemon.socket_path, "--json", "psu", "set", SIM_PSU, "--voltage", "400"
    )
    assert over_limit.code == EXIT_CODES[str(ErrorCode.SAFETY_LIMIT_EXCEEDED)] == 4
    assert over_limit.json()["error"]["code"] == str(ErrorCode.SAFETY_LIMIT_EXCEEDED)

    # Being armed did not raise the ceiling; the same grant still works inside it.
    inside = await fdctl(
        daemon.socket_path,
        "--json",
        "psu",
        "set",
        SIM_PSU,
        "--voltage",
        "12",
        "--current-limit",
        "0.5",
    )
    assert inside.code == 0
    assert inside.json()["setpoint_v"] == 12.0

    measured = await fdctl(daemon.socket_path, "--json", "psu", "measure", SIM_PSU)
    assert measured.code == 0
    assert "voltage" in measured.json()

    unknown_device = await fdctl(daemon.socket_path, "--json", "device", "not-a-device")
    assert unknown_device.code == EXIT_CODES[str(ErrorCode.DEVICE_NOT_FOUND)] == 6


async def test_no_daemon_is_a_transport_error_not_a_traceback(tmp_path: Path) -> None:
    run = await fdctl(tmp_path / "absent.sock", "--json", "status")
    assert run.code == EXIT_CODES[str(ErrorCode.TRANSPORT_ERROR)] == 9
    payload = run.json()
    assert payload["error"]["code"] == str(ErrorCode.TRANSPORT_ERROR)
    assert "instrumentd is not running" in payload["error"]["message"]
    assert "Traceback" not in run.stderr


async def test_usage_errors_are_exit_two(daemon: InstrumentDaemon) -> None:
    bad_pair = await fdctl(daemon.socket_path, "--json", "call", "system.status", "not-a-pair")
    assert bad_pair.code == EXIT_CODES["usage"] == 2

    missing_arg = await fdctl(daemon.socket_path, "--json", "psu", "set", SIM_PSU)
    assert missing_arg.code == EXIT_CODES["usage"]


async def test_call_reaches_actions_with_no_bespoke_command(daemon: InstrumentDaemon) -> None:
    """The escape hatch goes through the same pipeline, permission and all."""
    started = await fdctl(daemon.socket_path, "--json", "session", "start", "fdctl-integration")
    assert started.code == 0
    session_id = started.json()["session"]["id"]

    captured = await fdctl(
        daemon.socket_path,
        "--json",
        "call",
        "serial.capture",
        "device=sim:serial:sim-uart-0",
        "duration_s=0.5",
        "label=viacall",
    )
    assert captured.code == 0
    artifact = captured.json()["artifact"]
    assert artifact["raw"] is True

    path = daemon.sessions.sessions_dir / session_id / artifact["relative_path"]
    assert path.stat().st_size == artifact["size_bytes"] > 0

    stopped = await fdctl(daemon.socket_path, "--json", "session", "stop")
    assert stopped.code == 0
    assert stopped.json()["session"]["state"] == "CLOSED"


async def test_estop_is_never_gated_and_blocks_arming_afterwards(
    daemon: InstrumentDaemon,
) -> None:
    stopped = await fdctl(daemon.socket_path, "--json", "estop", "--reason", "integration test")
    assert stopped.code == 0
    assert stopped.json()["estop"] is True

    blocked = await fdctl(daemon.socket_path, "--json", "arm", "power", "--ttl", "30")
    assert blocked.code == EXIT_CODES[str(ErrorCode.ESTOP_ACTIVE)] == 5

    # Clearing is the deliberate act, and refuses to happen unconfirmed.
    unconfirmed = await fdctl(daemon.socket_path, "--json", "estop", "clear")
    assert unconfirmed.code == EXIT_CODES["usage"]
    assert daemon.safety.snapshot().estop_active is True

    cleared = await fdctl(daemon.socket_path, "--json", "estop", "clear", "--yes")
    assert cleared.code == 0
    assert daemon.safety.snapshot().estop_active is False
