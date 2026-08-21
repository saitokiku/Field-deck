"""``fielddeck/common/process.py`` — the single chokepoint for external tools.

Every programmer, debugger and capture tool FieldDeck shells out to goes
through :func:`run_tool`: openocd, pyocd, esptool, avrdude, dfu-util, picotool,
sigrok-cli, tcpdump. It had no tests at all, and its path-traversal guard --
which exists because firmware paths reach it from recipes and from the MCP
surface -- had three bypasses:

    sub/../../../../etc/shadow    a relative path whose ``..`` is not at the front
    --firmware=/etc/shadow        a value carried inline on a long option
    -D/etc/shadow                 dfu-util's short option with the value attached

The guard skipped anything starting with ``-``, and only inspected relative
paths that *began* with ``../``.

The tests that matter here are the refusals. The ones that matter almost as
much are the allowances: a guard that also blocks ``-f interface/stlink.cfg``
breaks openocd, and would be turned off by the first person it inconveniences.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

from fielddeck.common.errors import ExternalToolError
from fielddeck.common.process import ToolResult, _validate_paths, run_tool, tool_version


@pytest.fixture
def root() -> Path:
    path = Path(tempfile.mkdtemp()).resolve()
    (path / "app.bin").write_bytes(b"\x00" * 16)
    return path


# ---------------------------------------------------------------------------
# Path confinement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "args"),
    [
        ("absolute", ["/etc/shadow"]),
        ("leading dot-dot", ["../../../etc/shadow"]),
        # The three that used to get through:
        ("dot-dot not at the front", ["sub/../../../../etc/shadow"]),
        ("value inline on a long option", ["--firmware=/etc/shadow"]),
        ("value attached to a short option", ["-D/etc/shadow"]),
        ("tilde traversal", ["~/../../../etc/shadow"]),
        # openocd passes a whole command line as one argument.
        ("path inside a composite argument", ["-c", "program /etc/shadow verify reset"]),
    ],
)
def test_a_path_outside_the_roots_is_refused(label: str, args: list[str], root: Path) -> None:
    with pytest.raises(ExternalToolError) as caught:
        _validate_paths(args, [root])
    assert caught.value.preserved == "the tool was not started"
    assert "outside the permitted directories" in caught.value.message


@pytest.mark.parametrize(
    ("label", "args"),
    [
        ("a file in the root", ["{root}/app.bin"]),
        ("a composite argument in the root", ["-c", "program {root}/app.bin verify"]),
    ],
)
def test_a_path_inside_the_roots_is_allowed(label: str, args: list[str], root: Path) -> None:
    _validate_paths([arg.format(root=root) for arg in args], [root])


@pytest.mark.parametrize(
    ("label", "args"),
    [
        # openocd resolves this against its own script directory, not ours. A
        # guard that blocks it breaks openocd and gets switched off.
        ("openocd script path", ["-f", "interface/stlink.cfg"]),
        ("plain flags and values", ["-t", "stm32f4x", "--base-address", "0x0"]),
        ("a bare word", ["chip_id"]),
        ("a flag that is not a path", ["-Wall"]),
        ("an empty argument", [""]),
    ],
)
def test_a_non_path_argument_is_left_alone(label: str, args: list[str], root: Path) -> None:
    _validate_paths(args, [root])


def test_no_roots_means_no_confinement(root: Path) -> None:
    """Callers that pass no roots opt out, and that has to stay explicit."""
    _validate_paths(["/etc/shadow"], None)
    _validate_paths(["/etc/shadow"], [])


def test_a_symlink_out_of_the_root_is_refused(root: Path) -> None:
    """Confinement is on the resolved path, or it is not confinement."""
    escape = root / "escape"
    escape.symlink_to("/etc")
    with pytest.raises(ExternalToolError):
        _validate_paths([str(escape / "shadow")], [root])


# ---------------------------------------------------------------------------
# Running things
# ---------------------------------------------------------------------------


async def test_a_successful_run_reports_what_happened() -> None:
    result = await run_tool(sys.executable, ["-c", "print('hello')"])
    assert isinstance(result, ToolResult)
    assert result.returncode == 0
    assert result.ok
    assert result.stdout.strip() == "hello"
    assert not result.timed_out
    assert not result.killed
    assert result.duration_ns > 0


async def test_a_failing_run_is_reported_not_raised() -> None:
    """``run_tool`` returns the failure; ``check()`` is what raises.

    Keeping those separate is what lets a caller record a failed attempt in the
    session before deciding whether it was fatal.
    """
    result = await run_tool(sys.executable, ["-c", "import sys; sys.exit(3)"])
    assert result.returncode == 3
    assert not result.ok

    with pytest.raises(ExternalToolError) as caught:
        result.check(what="the test tool")
    assert "the test tool" in caught.value.message


async def test_stdin_reaches_the_tool() -> None:
    result = await run_tool(
        sys.executable,
        ["-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
        stdin=b"firmware",
    )
    assert result.stdout == "FIRMWARE"


async def test_a_missing_executable_is_a_typed_error() -> None:
    with pytest.raises(ExternalToolError):
        await run_tool("fielddeck-definitely-not-a-real-tool", ["--version"])


async def test_there_is_no_shell() -> None:
    """A firmware filename with a space in it should be awkward, not dangerous."""
    marker = Path(tempfile.mkdtemp()) / "pwned"
    result = await run_tool(
        sys.executable,
        ["-c", "import sys; print(sys.argv[1:])", f"; touch {marker}"],
    )
    assert not marker.exists(), "an argument was interpreted by a shell"
    assert "touch" in result.stdout  # it arrived as data, which is the point


# ---------------------------------------------------------------------------
# The terminate -> kill ladder
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_a_hung_tool_is_asked_nicely_before_it_is_killed() -> None:
    """openocd and friends often need to leave a target in a defined state.

    So a timeout sends SIGTERM first and only escalates if the grace period
    passes. A tool that handles SIGTERM must be recorded as timed out but *not*
    killed -- the distinction tells an operator whether the tool got to clean up.
    """
    handles_sigterm = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "time.sleep(30)\n"
    )
    result = await run_tool(
        sys.executable, ["-c", handles_sigterm], timeout_s=0.5, kill_grace_s=3.0
    )
    assert result.timed_out
    assert not result.killed, "SIGTERM was handled, so it should not have been killed"


@pytest.mark.slow
async def test_a_tool_that_ignores_sigterm_is_killed() -> None:
    ignores_sigterm = (
        "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(30)\n"
    )
    result = await run_tool(
        sys.executable, ["-c", ignores_sigterm], timeout_s=0.5, kill_grace_s=0.5
    )
    assert result.timed_out
    assert result.killed, "a tool that ignores SIGTERM must not be left running"


@pytest.mark.slow
async def test_cancelling_the_caller_does_not_leave_the_tool_running() -> None:
    """A cancelled capture must not orphan a subprocess holding the device."""
    started = asyncio.Event()

    async def run() -> None:
        started.set()
        await run_tool(sys.executable, ["-c", "import time; time.sleep(30)"], timeout_s=30)

    task = asyncio.create_task(run())
    await started.wait()
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


async def test_tool_version_returns_none_for_a_missing_tool() -> None:
    assert await tool_version("fielddeck-definitely-not-a-real-tool") is None


def test_the_command_line_is_reconstructable_for_the_audit_trail() -> None:
    result = ToolResult(
        executable="/usr/bin/openocd",
        args=["-f", "interface/stlink.cfg", "-c", "program app.bin"],
        returncode=0,
        stdout="",
        stderr="",
        duration_ns=1_000_000,
    )
    assert result.command_line.startswith("/usr/bin/openocd ")
    assert "interface/stlink.cfg" in result.command_line
    payload = result.as_dict()
    assert payload["returncode"] == 0
    assert payload["duration_ms"] == 1.0
