"""The one place FieldDeck runs an external program.

sigrok-cli, OpenOCD, esptool, dfu-util and friends are better at their jobs
than anything reimplemented here would be.  They are also, from FieldDeck's
point of view, a supply of ways to accidentally erase a customer's firmware,
so every invocation goes through this adapter and nothing else.

Guarantees this adapter makes, all of which exist because the alternative has
burned someone before:

* argument **arrays**, never a shell string — there is no shell to inject into
* a mandatory timeout, then terminate, then kill
* stdout and stderr captured and returned, never dumped on the operator
* the exact command recorded for the audit trail, with secrets redacted
* a structured :class:`ToolResult` instead of a raw exit code
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from fielddeck.common.errors import ExternalToolError
from fielddeck.common.logging import get_logger
from fielddeck.common.timebase import monotonic_ns

__all__ = ["ToolResult", "have_tool", "run_tool", "tool_version", "which"]

_log = get_logger("fielddeck.common.process")

#: Argument values that must never reach a log or an event payload.
_SECRET_FLAGS = ("--password", "--token", "--key", "--secret")


@dataclass(slots=True)
class ToolResult:
    """The outcome of one external command."""

    executable: str
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ns: int
    timed_out: bool = False
    killed: bool = False
    env_overrides: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def command_line(self) -> str:
        """For display and audit only.  Never re-parsed and never executed."""
        return " ".join([self.executable, *_redact_args(self.args)])

    def as_dict(self) -> dict[str, object]:
        return {
            "executable": self.executable,
            "args": _redact_args(self.args),
            "returncode": self.returncode,
            "duration_ms": round(self.duration_ns / 1e6, 2),
            "timed_out": self.timed_out,
            "killed": self.killed,
            "stdout_bytes": len(self.stdout),
            "stderr_bytes": len(self.stderr),
        }

    def check(self, *, what: str) -> ToolResult:
        """Raise a useful error if the tool failed."""
        if self.ok:
            return self
        if self.timed_out:
            raise ExternalToolError(
                f"{what} timed out after {self.duration_ns / 1e9:.1f}s",
                details={"command": self.command_line, "stderr": _tail(self.stderr)},
                preserved="any output produced before the timeout is in this error's details",
            )
        raise ExternalToolError(
            f"{what} failed with exit code {self.returncode}",
            details={
                "command": self.command_line,
                "returncode": self.returncode,
                "stderr": _tail(self.stderr),
                "stdout": _tail(self.stdout),
            },
            preserved="no partial write was performed by FieldDeck itself",
        )


def _redact_args(args: Sequence[str]) -> list[str]:
    out: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            out.append("***redacted***")
            redact_next = False
            continue
        lowered = arg.lower()
        if any(lowered.startswith(flag) for flag in _SECRET_FLAGS):
            if "=" in arg:
                out.append(arg.split("=", 1)[0] + "=***redacted***")
            else:
                out.append(arg)
                redact_next = True
            continue
        out.append(arg)
    return out


def _tail(text: str, limit: int = 4000) -> str:
    """External tools can be extremely chatty; keep the end, which explains."""
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def which(executable: str) -> str | None:
    """Resolve a tool on PATH, or return None."""
    return shutil.which(executable)


def have_tool(executable: str) -> bool:
    return which(executable) is not None


def _resolve(executable: str) -> str:
    resolved = which(executable)
    if resolved is None:
        raise ExternalToolError(
            f"{executable} is not installed or not on PATH",
            details={"executable": executable, "path": os.environ.get("PATH", "")},
            preserved="nothing was attempted",
        )
    return resolved


#: A long option carrying its value inline, e.g. ``--firmware=/etc/shadow``.
_INLINE_VALUE = re.compile(r"^--?[^=\s]+=(?P<value>.+)$", re.S)

#: A short option with the value attached, e.g. dfu-util's ``-D/path/to.bin``.
_ATTACHED_VALUE = re.compile(r"^-[A-Za-z](?P<value>[^-].*)$", re.S)


def _path_candidates(arg: str) -> list[str]:
    """Every substring of one argument that might be a path.

    Checking the argument as a whole is not enough.  Tools take a path in at
    least three shapes, and the original guard skipped anything beginning with
    ``-``, so ``--firmware=/etc/shadow`` and dfu-util's ``-D/etc/shadow`` went
    straight through.  openocd additionally passes a whole command line as one
    argument (``-c "program /tmp/x.bin verify reset exit"``), so whitespace
    inside an argument is split as well.
    """
    candidates = [arg]
    for pattern in (_INLINE_VALUE, _ATTACHED_VALUE):
        match = pattern.match(arg)
        if match:
            candidates.append(match.group("value"))
    if any(ch.isspace() for ch in arg):
        candidates.extend(token for token in arg.split() if token)
    return candidates


def _escapes_roots(candidate: str, resolved_roots: Sequence[Path]) -> bool:
    """Is this string a path that lands outside every permitted root?

    A *relative* path with no ``..`` component cannot escape, and openocd
    genuinely needs those -- ``-f interface/stlink.cfg`` is resolved by openocd
    against its own script directory, not by us.  A relative path that contains
    ``..`` is a different matter: ``sub/../../../../etc/shadow`` used to pass,
    because the old guard only looked at arguments that *began* with ``../``.
    """
    if "/" not in candidate and not candidate.startswith("~"):
        return False
    expanded = Path(candidate).expanduser()
    traverses = ".." in expanded.parts
    if not expanded.is_absolute() and not traverses and not candidate.startswith("~"):
        return False
    resolved = expanded.resolve()
    return not any(resolved.is_relative_to(root) for root in resolved_roots)


def _validate_paths(args: Sequence[str], allowed_roots: Sequence[Path] | None) -> None:
    """Refuse path arguments that escape the roots the caller allows.

    Firmware paths and capture filenames reach these tools from recipes and
    from the MCP surface.  ``../../etc/shadow`` has to be impossible, not
    merely unlikely.
    """
    if not allowed_roots:
        return
    resolved_roots = [root.resolve() for root in allowed_roots]
    for arg in args:
        if not arg:
            continue
        for candidate in _path_candidates(arg):
            if not _escapes_roots(candidate, resolved_roots):
                continue
            raise ExternalToolError(
                f"path argument {candidate!r} is outside the permitted directories",
                details={
                    "argument": arg,
                    "path": candidate,
                    "allowed_roots": [str(root) for root in resolved_roots],
                },
                preserved="the tool was not started",
            )


async def run_tool(
    executable: str,
    args: Sequence[str],
    *,
    timeout_s: float = 30.0,
    stdin: bytes | None = None,
    cwd: Path | None = None,
    env_overrides: dict[str, str] | None = None,
    allowed_path_roots: Sequence[Path] | None = None,
    kill_grace_s: float = 3.0,
) -> ToolResult:
    """Run one external tool safely.

    ``args`` is a list.  There is deliberately no string form and no
    ``shell=True``: a firmware filename with a space in it should be awkward,
    not dangerous.
    """
    resolved = _resolve(executable)
    args = [str(arg) for arg in args]
    _validate_paths(args, allowed_path_roots)

    env = None
    if env_overrides:
        env = {**os.environ, **env_overrides}

    started = monotonic_ns()
    _log.debug(
        "running external tool",
        extra={"executable": resolved, "args": _redact_args(args)},
    )
    process = await asyncio.create_subprocess_exec(
        resolved,
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=env,
    )

    timed_out = False
    killed = False
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout_s)
    except TimeoutError:
        timed_out = True
        # Ask nicely first: OpenOCD and friends often need to leave a target
        # in a defined state rather than being shot mid-transaction.
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), kill_grace_s)
        except TimeoutError:
            killed = True
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        with contextlib.suppress(TimeoutError, ProcessLookupError):
            await asyncio.wait_for(process.wait(), kill_grace_s)
        raise

    return ToolResult(
        executable=resolved,
        args=args,
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=(stdout or b"").decode("utf-8", errors="replace"),
        stderr=(stderr or b"").decode("utf-8", errors="replace"),
        duration_ns=monotonic_ns() - started,
        timed_out=timed_out,
        killed=killed,
        env_overrides=dict(env_overrides or {}),
    )


async def tool_version(executable: str, *flags: str, timeout_s: float = 5.0) -> str | None:
    """Best-effort version string, recorded on derived artifacts for provenance."""
    if not have_tool(executable):
        return None
    for candidate in flags or ("--version", "-V", "-v"):
        try:
            result = await run_tool(executable, [candidate], timeout_s=timeout_s)
        except ExternalToolError:  # pragma: no cover - tool vanished mid-check
            return None
        text = (result.stdout or result.stderr).strip()
        if text:
            return text.splitlines()[0][:200]
    return None
