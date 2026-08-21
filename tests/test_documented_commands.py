"""Every ``fdctl`` command printed in the docs must actually parse.

This exists because six of them did not.  ``fdctl bench devices`` was
contamination from the MCP tool name (which really is ``bench_devices``),
``serial configure --baudrate`` is spelled ``--baud``, ``serial send
--append-crlf`` is spelled ``--newline``, and every ``fdctl call`` example
passed a JSON blob when ``call`` takes ``KEY=VALUE`` positionals.  All of them
were written confidently, read back, and wrong.

A wrong flag in the documentation is worse than an undocumented feature: the
reader assumes their situation is unusual rather than that the docs are wrong,
and goes looking for the problem somewhere real.

**Why this runs without a daemon.**  Not only for speed.  The docs contain
``fdctl estop``, and executing the documented commands against a live daemon
latches it -- which then makes every later command in the sweep fail for an
unrelated reason.  Pointing at a socket that cannot exist keeps the check to
the one property it is asserting: that the command line is well formed.

**Why the exit code is not enough.**  ``fdctl`` uses exit 2 for its own
``usage`` class, which also covers a refused confirmation prompt (``arm
flash`` without ``--yes`` is a deliberate, correct refusal).  So a parse
failure is identified by Click's own diagnosis in the output, not by the code.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCS = [REPO / "README.md", *sorted((REPO / "docs").glob("*.md"))]

#: What Click says when it could not parse a command line.  Matching on the
#: message rather than the exit code keeps FieldDeck's own typed refusals --
#: which share the exit code -- from reading as documentation bugs.
PARSE_ERROR = re.compile(
    r"No such option|No such command|[Gg]ot unexpected extra argument"
    r"|Missing argument|Missing option|Invalid value for",
)

#: Placeholders meaning "substitute your own value" -- an illustration, not
#: something a reader types verbatim.
PLACEHOLDERS = ("<", ">", "...", "|", "&", "$(")

#: Box-drawing characters.  An architecture diagram can contain a line that
#: starts with "fdctl" without being a command.
BOX_DRAWING = re.compile(r"[─-╿▀-▟▶◀]")


def _documented_commands() -> list[tuple[str, str]]:
    """Every runnable ``fdctl ...`` line in the docs, as (location, command)."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for doc in DOCS:
        if not doc.exists():  # pragma: no cover - the docs are checked in
            continue
        for lineno, raw in enumerate(doc.read_text().splitlines(), 1):
            if BOX_DRAWING.search(raw):
                continue
            line = raw.strip().removeprefix("$").strip()
            if not line.startswith("fdctl "):
                continue
            # A trailing "# explanatory comment" is for the reader, not a shell.
            command = line.split("#")[0].strip()
            if any(token in command for token in PLACEHOLDERS):
                continue
            try:
                shlex.split(command)
            except ValueError:
                continue
            if command in seen:
                continue
            seen.add(command)
            found.append((f"{doc.relative_to(REPO)}:{lineno}", command))
    return found


DOCUMENTED = _documented_commands()


def test_the_docs_actually_contain_commands() -> None:
    """Guard against the extractor silently matching nothing.

    A regex that stops matching turns this whole file into a no-op that still
    reports green, which is the failure mode worth defending against.
    """
    assert len(DOCUMENTED) > 40, f"only found {len(DOCUMENTED)} commands; the extractor is broken"


@pytest.mark.parametrize(
    ("location", "command"),
    DOCUMENTED,
    ids=[f"{loc} {cmd[:48]}" for loc, cmd in DOCUMENTED],
)
def test_a_documented_command_parses(location: str, command: str) -> None:
    argv = shlex.split(command)
    assert argv[0] == "fdctl"

    result = subprocess.run(
        [sys.executable, "-m", "fielddeck.cli.fdctl", *argv[1:]],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        env={
            **os.environ,
            # Somewhere that cannot exist, so a well-formed command stops at
            # the socket instead of reaching a real daemon and acting on it.
            "FIELDDECK_SOCKET": "/nonexistent/fielddeck-doc-check.sock",
            "NO_COLOR": "1",
            "COLUMNS": "100",
        },
        cwd=REPO,
    )
    output = f"{result.stdout}\n{result.stderr}"
    match = PARSE_ERROR.search(output)
    if match is None:
        return

    reason = next(
        (line.strip() for line in output.splitlines() if PARSE_ERROR.search(line)),
        match.group(0),
    )
    pytest.fail(f"{location} documents a command that does not parse\n  $ {command}\n  {reason}")


def test_no_documented_call_passes_a_json_blob() -> None:
    """``fdctl call`` takes ``KEY=VALUE`` positionals, not JSON.

    Checked separately because these lines usually carry a ``...`` placeholder,
    which keeps them out of the executable sweep above -- and a reader copies
    the *shape* regardless of the placeholder.  The daemon's refusal is
    ``'{"device": ...}' is not key=value``, which is clear once you hit it and
    entirely avoidable before you ship it.
    """
    offenders: list[str] = []
    pattern = re.compile(r"fdctl\b[^\n]*\bcall\b[^\n]*(--json\s*'?\{|--params\b)")
    for doc in DOCS:
        if not doc.exists():  # pragma: no cover
            continue
        for lineno, line in enumerate(doc.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{doc.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert not offenders, "fdctl call takes KEY=VALUE, not a JSON blob:\n  " + "\n  ".join(
        offenders
    )
