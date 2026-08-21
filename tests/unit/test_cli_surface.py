"""Every command family the specification names must exist.

`fdctl` had no `logic`, `debug`, `firmware` or `flash` family. Those actions
were reachable -- `fdctl call flash.plan tool=openocd operation=program ...` --
but the escape hatch is not the same as support. It offers no completion, no
help text naming the permission a command needs, and no confirmation prompt
before something irreversible.

The list below is the one in CLAUDE.md section 13. It changes rarely and
deliberately; if a family is dropped, that should be a decision someone makes,
not something that quietly stops being true.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent

#: Command families named in CLAUDE.md section 13.
FAMILIES = [
    "arm",
    "bench",
    "can",
    "convert",
    "crc",
    "debug",
    "devices",
    "disarm",
    "discover",
    "estop",
    "firmware",
    "flash",
    "logic",
    "modbus",
    "psu",
    "recipe",
    "scpi",
    "serial",
    "session",
    "status",
]


def _fdctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "fielddeck.cli.fdctl", *args],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        env={
            **os.environ,
            "FIELDDECK_SOCKET": "/nonexistent/fielddeck-cli-surface.sock",
            "NO_COLOR": "1",
            "COLUMNS": "100",
        },
        cwd=REPO,
    )


@pytest.mark.parametrize("family", FAMILIES)
def test_the_command_family_exists(family: str) -> None:
    result = _fdctl(family, "--help")
    assert "No such command" not in (result.stdout + result.stderr), (
        f"fdctl has no '{family}' family, which CLAUDE.md section 13 names"
    )


@pytest.mark.parametrize(
    ("family", "subcommands"),
    [
        ("logic", ["devices", "status", "capture", "decode"]),
        ("debug", ["probes", "tools", "target", "reset"]),
        ("firmware", ["inspect"]),
        ("flash", ["plan", "verify", "program", "erase"]),
    ],
)
def test_a_new_family_has_the_subcommands_it_advertises(
    family: str, subcommands: list[str]
) -> None:
    listing = _fdctl(family, "--help").stdout
    missing = [name for name in subcommands if name not in listing]
    assert not missing, f"fdctl {family} is missing {missing}"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        # The dangerous ones must refuse without an explicit confirmation, and
        # must refuse rather than assume when stdin is not a terminal.
        (["flash", "program", "openocd", "--firmware", "app.bin"], "FLASH"),
        (["flash", "erase", "openocd", "--target", "stm32f4x"], "ERASE"),
    ],
)
def test_an_irreversible_command_refuses_without_confirmation(
    args: list[str], expected: str
) -> None:
    result = _fdctl(*args)
    output = result.stdout + result.stderr
    assert "not confirmed" in output, f"{' '.join(args)} did not require confirmation"
    assert expected in output, "the confirmation prompt does not say what to type"
    assert "nothing was changed" in output


def test_flash_plan_needs_no_confirmation() -> None:
    """It is PASSIVE, and it is the thing you run *before* deciding.

    A prompt here would train people to type past prompts.
    """
    result = _fdctl("flash", "plan", "openocd", "--operation", "info", "--target", "stm32f4x")
    assert "not confirmed" not in (result.stdout + result.stderr)
