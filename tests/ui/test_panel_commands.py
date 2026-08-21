"""Commands the panel tells an operator to type must exist.

The TOOLS screen's UNIT and FILE tiles printed three commands, none of which
parsed:

    fdctl convert unit 24 --from V --to mV   (unit is --op unit, not a subcommand)
    fdctl session artifacts                  (no such subcommand; it is session show)
    fdctl inspect can/can0-0001.log          (no such command)

The panel is the last place this should happen. Someone reading it is at the
bench, on a 480x320 screen, and has just been told the thing they want needs
the CLI -- so they switch to the SHELL window and type what it said.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
UI = REPO / "fielddeck" / "ui"

#: What Click says when it could not parse a command line.
PARSE_ERROR = re.compile(
    r"No such option|No such command|[Gg]ot unexpected extra argument"
    r"|Missing argument|Missing option|Invalid value for",
)


def _panel_commands() -> list[tuple[str, str]]:
    """Every complete ``fdctl ...`` command the HMI prints, as (location, command)."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source in sorted(UI.rglob("*.py")):
        for lineno, line in enumerate(source.read_text().splitlines(), 1):
            # The panel prints them inside string literals, indented.
            for match in re.finditer(r'"\s*(fdctl [^"]+)"', line):
                command = match.group(1).strip()
                if any(token in command for token in ("<", ">", "...", "|")):
                    continue  # a placeholder, not something to type verbatim
                try:
                    shlex.split(command)
                except ValueError:
                    continue
                if command in seen:
                    continue
                seen.add(command)
                found.append((f"{source.relative_to(REPO)}:{lineno}", command))
    return found


PANEL_COMMANDS = _panel_commands()


def test_the_panel_does_print_some_commands() -> None:
    """Guard the extractor, so this file cannot quietly become a no-op."""
    assert PANEL_COMMANDS, "found no fdctl commands in the HMI; the extractor is broken"


@pytest.mark.parametrize(
    ("location", "command"),
    PANEL_COMMANDS,
    ids=[f"{loc} {cmd[:44]}" for loc, cmd in PANEL_COMMANDS],
)
def test_a_command_the_panel_prints_actually_parses(location: str, command: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "fielddeck.cli.fdctl", *shlex.split(command)[1:]],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        env={
            **os.environ,
            "FIELDDECK_SOCKET": "/nonexistent/fielddeck-panel-check.sock",
            "NO_COLOR": "1",
            "COLUMNS": "100",
        },
        cwd=REPO,
    )
    output = f"{result.stdout}\n{result.stderr}"
    if not PARSE_ERROR.search(output):
        return
    reason = next(line.strip() for line in output.splitlines() if PARSE_ERROR.search(line))
    pytest.fail(
        f"{location} tells the operator to run a command that does not parse\n"
        f"  $ {command}\n  {reason}"
    )
