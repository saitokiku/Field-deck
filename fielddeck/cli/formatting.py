"""How ``fdctl`` puts things on a screen, and why it is separate from the commands.

Two rules shape everything here.

The first is that ``--json`` must produce a document a script can parse with no
cleaning up: pure JSON on stdout, no ANSI, no progress spinner, no "connecting
to instrumentd..." line that a pipe would swallow into the middle of an object.
So :class:`Emitter` owns the two streams — stdout carries either the JSON
document or the human rendering and never both, and every human aside goes to
stderr where a pipe cannot corrupt it.

The second is that this is an instrument panel.  An operator glancing at
``fdctl status`` on a 480x320 panel has to see a latched emergency stop
immediately, and has to see it even on a monochrome display, so the ESTOP
banner says so in words and does not rely on the terminal having colour.  The
same goes for arm banners: colour is decoration, the text carries the meaning.

Nothing in this module decides what is allowed.  It renders what the daemon
said.  If a command body starts computing a permission or a countdown for
display, that logic belongs on the other side of the socket.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from fielddeck.common.errors import FieldDeckError
from fielddeck.common.models import ArmGrant
from fielddeck.common.timebase import format_utc_ns

__all__ = [
    "Emitter",
    "action_table",
    "arm_banner",
    "danger_notice",
    "device_detail",
    "device_table",
    "error_view",
    "estop_banner",
    "event_line",
    "event_table",
    "kv_panel",
    "live_status",
    "result_view",
    "rows_table",
    "running_table",
    "session_table",
    "status_view",
    "watch_line",
]

#: Severity is what an operator scans for first, so it gets the strongest
#: styling budget.  Every entry still reads correctly with colour stripped.
_SEVERITY_STYLES: dict[str, str] = {
    "debug": "dim",
    "info": "",
    "warning": "yellow",
    "error": "bold red",
    "critical": "bold white on red",
}

_PERMISSION_STYLES: dict[str, str] = {
    "PASSIVE": "dim",
    "QUERY": "cyan",
    "CONTROL": "yellow",
    "POWER": "bold yellow",
    "FLASH": "bold magenta",
    "DESTRUCTIVE": "bold red",
}

_STATE_STYLES: dict[str, str] = {
    "SAFE": "bold green",
    "ARMED": "bold yellow",
    "ESTOP": "bold white on red",
}

#: Rows of a nested list rendered inside a generic result view.  A ``can.listen``
#: result can hold thousands of frames; printing all of them turns a terminal
#: into a log file and buries whatever the operator was looking for.
_MAX_NESTED_ROWS = 20

#: Columns of a generic table.  Beyond this the row wraps into unreadability on
#: an 80-column panel, so the remainder is named rather than shown.
_MAX_NESTED_COLUMNS = 8


# ---------------------------------------------------------------------------
# Output routing
# ---------------------------------------------------------------------------


class Emitter:
    """Owns stdout and stderr for one ``fdctl`` invocation.

    Constructed per invocation and passed down; there is no module-level
    console, because ``--json`` changes what stdout means and a shared console
    would leak human text into a machine-readable document.
    """

    def __init__(self, *, json_mode: bool = False, color: bool = True) -> None:
        self.json_mode = json_mode
        self._out = Console(no_color=not color, highlight=False, emoji=False)
        self._err = Console(stderr=True, no_color=not color, highlight=False, emoji=False)

    @property
    def console(self) -> Console:
        """The stdout console.  Only a live view should need this."""
        return self._out

    # -- machine-readable --------------------------------------------------

    def data(self, payload: Any) -> None:
        """One JSON document on stdout.  No ANSI, nothing else, ever."""
        print(json.dumps(payload, indent=2, default=str))

    def stream(self, payload: Any) -> None:
        """One JSON document per line, for ``--follow`` style commands.

        Line-delimited rather than one big array: a stream has no end, and a
        consumer should be able to act on each record as it arrives.
        """
        print(json.dumps(payload, separators=(",", ":"), default=str), flush=True)

    # -- human-readable ----------------------------------------------------

    def emit(self, payload: Any, view: Callable[[], RenderableType] | None = None) -> None:
        """The normal end of a command: JSON, or the rendered view of it.

        ``view`` is a callable so that building a table full of rich objects is
        skipped entirely in JSON mode.
        """
        if self.json_mode:
            self.data(payload)
            return
        self._out.print(view() if view is not None else result_view(payload))

    def show(self, renderable: RenderableType) -> None:
        """Human-only decoration, suppressed in JSON mode."""
        if self.json_mode:
            return
        self._out.print(renderable)

    def note(self, text: str) -> None:
        """Guidance for a person, on stderr so it never pollutes a pipe."""
        if self.json_mode:
            return
        self._err.print(Text(text, style="dim"))

    def warn(self, text: str) -> None:
        """Something the operator must know regardless of output mode.

        Always printed, JSON mode included, because a clamped TTL or a lease
        that will lapse when this process exits is not decoration.
        """
        self._err.print(Text(f"warning: {text}", style="bold yellow"))

    def error(self, exc: FieldDeckError) -> None:
        """Render a failure with the details and what survived it."""
        if self.json_mode:
            self.data({"ok": False, "error": exc.to_dict()})
            return
        self._err.print(error_view(exc))

    # -- interaction -------------------------------------------------------

    def confirm_word(self, notice: RenderableType, *, expected: str) -> bool:
        """Second, deliberate confirmation: type the word, not a keystroke.

        A y/N prompt is answered by muscle memory.  Typing ``DESTRUCTIVE``
        cannot be done by accident, which is the entire point of asking.

        Returns False when stdin is not a terminal: an unattended pipe must
        never be able to satisfy a confirmation by ending.
        """
        self._err.print(notice)
        if not sys.stdin.isatty():
            return False
        try:
            answer = self._err.input(f"Type {expected} to continue: ")
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip() == expected


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def error_view(exc: FieldDeckError) -> RenderableType:
    """Message, the details that help, and the ``preserved`` line.

    ``preserved`` is printed even when it is absent, as an explicit "nothing
    was reported as preserved" — an operator who has just lost a capture needs
    an answer, and silence is not one.
    """
    body: list[RenderableType] = [Text(exc.message, style="bold")]
    if exc.details:
        table = Table(box=None, show_header=False, pad_edge=False)
        table.add_column(style="dim", no_wrap=True)
        table.add_column(overflow="fold")
        for key, value in exc.details.items():
            table.add_row(str(key), _scalar(value))
        body.append(table)
    if exc.preserved:
        body.append(Text(f"preserved: {exc.preserved}", style="green"))
    else:
        body.append(Text("preserved: not reported by the daemon", style="dim"))
    return Panel(
        Group(*body),
        title=f"{exc.code}",
        title_align="left",
        border_style="red",
        box=box.HEAVY,
    )


def danger_notice(title: str, lines: Sequence[str]) -> RenderableType:
    """The block shown before a confirmation prompt."""
    return Panel(
        Group(*[Text(line) for line in lines]),
        title=title,
        title_align="left",
        border_style="red",
        box=box.HEAVY,
    )


# ---------------------------------------------------------------------------
# Generic rendering
# ---------------------------------------------------------------------------


def _scalar(value: Any) -> str:
    """Render one leaf value compactly and without lying about its type."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        if not value:
            return "-"
        return ", ".join(_scalar(item) for item in value)
    if isinstance(value, Mapping):
        return ", ".join(f"{key}={_scalar(item)}" for key, item in value.items())
    return str(value)


def _is_row_list(value: Any) -> bool:
    return (
        isinstance(value, list) and bool(value) and all(isinstance(item, Mapping) for item in value)
    )


def kv_panel(title: str | None, mapping: Mapping[str, Any], *, style: str = "") -> RenderableType:
    """A two-column key/value block, the workhorse of every detail screen."""
    table = Table(box=None, show_header=False, pad_edge=False)
    table.add_column(style="dim", no_wrap=True)
    table.add_column(overflow="fold")
    for key, value in mapping.items():
        table.add_row(str(key), _scalar(value))
    if title is None:
        return table
    return Panel(
        table, title=title, title_align="left", border_style=style or "none", box=box.ROUNDED
    )


def rows_table(
    title: str | None,
    rows: Sequence[Mapping[str, Any]],
    *,
    columns: Sequence[str] | None = None,
    limit: int = _MAX_NESTED_ROWS,
) -> RenderableType:
    """Tabulate a list of records, bounded so a big result stays readable."""
    if not rows:
        return Text(f"{title}: none" if title else "none", style="dim")
    keys = list(columns) if columns else list(dict.fromkeys(k for row in rows for k in row))
    hidden = keys[_MAX_NESTED_COLUMNS:]
    keys = keys[:_MAX_NESTED_COLUMNS]
    table = Table(title=title, title_justify="left", box=box.SIMPLE, expand=False)
    for key in keys:
        table.add_column(key, overflow="fold")
    for row in rows[:limit]:
        table.add_row(*[_scalar(row.get(key)) for key in keys])
    parts: list[RenderableType] = [table]
    if len(rows) > limit:
        parts.append(Text(f"... {len(rows) - limit} more rows (use --json for all)", style="dim"))
    if hidden:
        parts.append(Text(f"columns not shown: {', '.join(hidden)}", style="dim"))
    return Group(*parts)


def result_view(payload: Any, *, title: str | None = None) -> RenderableType:
    """Last-resort rendering for a result with no bespoke view.

    Scalars go in a key/value block; lists of records become their own tables.
    It is deliberately plain: a command whose output deserves better should get
    a named view in this module rather than special-casing here.
    """
    if not isinstance(payload, Mapping):
        return Text(_scalar(payload))
    scalars: dict[str, Any] = {}
    blocks: list[RenderableType] = []
    for key, value in payload.items():
        if _is_row_list(value):
            blocks.append(rows_table(key, value))
        elif isinstance(value, Mapping) and value:
            blocks.append(kv_panel(key, value))
        else:
            scalars[key] = value
    parts: list[RenderableType] = []
    if scalars:
        parts.append(kv_panel(title, scalars))
    elif title:
        parts.append(Text(title, style="bold"))
    parts.extend(blocks)
    return Group(*parts) if parts else Text("(empty result)", style="dim")


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


def _permission_text(permission: str) -> Text:
    return Text(permission, style=_PERMISSION_STYLES.get(permission, ""))


def estop_banner(safety: Mapping[str, Any]) -> RenderableType:
    """The one thing on ``fdctl status`` that must be impossible to miss.

    Spelled out in words rather than signalled by colour: the target display is
    a small panel that may be monochrome, and an operator reading this has
    already had a bad minute.
    """
    reason = safety.get("estop_reason") or "no reason recorded"
    engaged = safety.get("estop_utc_ns")
    lines: list[RenderableType] = [
        Text("EMERGENCY STOP IS LATCHED", style="bold white on red"),
        Text(f"reason:  {reason}"),
    ]
    if engaged:
        lines.append(Text(f"engaged: {format_utc_ns(int(engaged))}"))
    lines.extend(
        [
            Text(
                "Outputs were driven to safe state and every grant was revoked. "
                "Captured data is preserved.",
                style="dim",
            ),
            Text("Nothing can be armed until this is cleared:  fdctl estop clear"),
        ]
    )
    return Panel(
        Group(*lines),
        title="!!! ESTOP !!!",
        title_align="center",
        border_style="red",
        box=box.DOUBLE,
    )


def _grant_rows(grants: Sequence[Mapping[str, Any]]) -> RenderableType:
    if not grants:
        return Text("armed:    nothing (PASSIVE only)", style="dim")
    table = Table(box=box.SIMPLE, title="armed", title_justify="left", expand=False)
    table.add_column("permission")
    table.add_column("scope")
    table.add_column("expires in", justify="right")
    table.add_column("by")
    table.add_column("grant")
    for grant in grants:
        remaining = float(grant.get("remaining_s") or 0.0)
        table.add_row(
            _permission_text(str(grant.get("permission", "?"))),
            str(grant.get("scope", "?")),
            Text(f"{remaining:.0f}s", style="bold" if remaining < 10 else ""),
            str(grant.get("created_by", "?")),
            str(grant.get("grant_id", "?")),
        )
    return table


def _lease_rows(leases: Sequence[Mapping[str, Any]]) -> RenderableType:
    if not leases:
        return Text("", style="dim")
    table = Table(box=box.SIMPLE, title="output leases", title_justify="left", expand=False)
    table.add_column("device")
    table.add_column("action")
    table.add_column("expires in", justify="right")
    table.add_column("owner")
    table.add_column("lease")
    for lease in leases:
        table.add_row(
            str(lease.get("device_id", "?")),
            str(lease.get("action", "?")),
            f"{float(lease.get('remaining_s') or 0.0):.0f}s",
            str(lease.get("owner", "?")),
            str(lease.get("lease_id", "?")),
        )
    return table


def arm_banner(grants: Sequence[ArmGrant]) -> RenderableType:
    """Shown after ``fdctl arm``: what is armed, over what, until when.

    Arming is the moment a human takes responsibility for what the hardware
    does next, so the banner restates the scope and the expiry rather than
    assuming the operator remembers the flags they just typed.  The grants are
    the daemon's own models, so the scope wording here is the same wording the
    authorization check uses.
    """
    body: list[RenderableType] = []
    for grant in grants:
        expires_utc = format_utc_ns(grant.created_utc_ns + int(grant.ttl_s * 1e9))
        line = Text()
        line.append(
            str(grant.permission).ljust(12),
            style=_PERMISSION_STYLES.get(str(grant.permission), "bold"),
        )
        line.append(f"over {grant.scope.describe()}".ljust(34))
        line.append(f"for {grant.ttl_s:g}s, until {expires_utc}")
        body.append(line)
        if grant.note:
            body.append(Text(f"             note: {grant.note}", style="yellow"))
        body.append(Text(f"             grant {grant.grant_id}", style="dim"))
    body.append(Text("Disarm early with:  fdctl disarm", style="dim"))
    return Panel(
        Group(*body),
        title="ARMED - you are now responsible for what the hardware does",
        title_align="left",
        border_style="yellow",
        box=box.HEAVY,
    )


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def status_view(
    status: Mapping[str, Any],
    devices: Sequence[Mapping[str, Any]],
    aliases: Mapping[str, str] | None = None,
) -> RenderableType:
    """The one-screen overview: safety first, then session, then hardware."""
    safety = status.get("safety") or {}
    state = str(safety.get("state", "?"))
    blocks: list[RenderableType] = []
    if safety.get("estop_active"):
        blocks.append(estop_banner(safety))

    headline = Table(box=None, show_header=False, pad_edge=False)
    headline.add_column(style="dim", no_wrap=True)
    headline.add_column(overflow="fold")
    headline.add_row("state", Text(state, style=_STATE_STYLES.get(state, "bold")))
    headline.add_row(
        "daemon",
        f"fielddeck {status.get('version', '?')}"
        + ("  [SIMULATED - no hardware is attached]" if status.get("simulated") else "")
        + f"  up {float(status.get('uptime_s') or 0.0):.0f}s",
    )
    headline.add_row("utc", str(status.get("utc", "-")))
    session = status.get("session")
    if session:
        headline.add_row(
            "session",
            f"{session.get('name', '?')}  ({session.get('id', '?')})  "
            f"{float(session.get('elapsed_s') or 0.0):.0f}s  recording",
        )
    else:
        headline.add_row(
            "session", Text('none - start one with: fdctl session start "<name>"', style="dim")
        )
    storage = status.get("storage") or {}
    if storage.get("sessions_dir"):
        headline.add_row("storage", str(storage["sessions_dir"]))
    blocks.append(Panel(headline, title="FieldDeck", title_align="left", box=box.ROUNDED))

    blocks.append(_grant_rows(safety.get("armed") or []))
    leases = safety.get("leases") or []
    if leases:
        blocks.append(_lease_rows(leases))

    blocks.append(device_table(devices, aliases))

    running = status.get("running_actions") or []
    if running:
        blocks.append(running_table(running))
    return Group(*blocks)


def watch_line(status: Mapping[str, Any]) -> Text:
    """One dense line for a second terminal.  No panels, no scrolling."""
    safety = status.get("safety") or {}
    state = str(safety.get("state", "?"))
    line = Text()
    line.append(f" {state} ", style=_STATE_STYLES.get(state, "bold"))
    grants = safety.get("armed") or []
    if grants:
        summary = " ".join(
            f"{grant.get('permission')}:{float(grant.get('remaining_s') or 0):.0f}s"
            for grant in grants
        )
        line.append(f" armed {summary}", style="yellow")
    leases = safety.get("leases") or []
    if leases:
        line.append(f" leases {len(leases)}", style="bold yellow")
    session = status.get("session")
    if session:
        line.append(f" | {session.get('name')} {float(session.get('elapsed_s') or 0):.0f}s")
    else:
        line.append(" | no session", style="dim")
    devices = status.get("devices") or {}
    line.append(f" | {devices.get('total', 0)} dev")
    running = status.get("running_actions") or []
    if running:
        line.append(
            f" | running {', '.join(str(item.get('action')) for item in running)}", style="cyan"
        )
    if safety.get("estop_reason") and safety.get("estop_active"):
        line.append(f" | {safety['estop_reason']}", style="bold red")
    return line


# ---------------------------------------------------------------------------
# Devices and actions
# ---------------------------------------------------------------------------


def device_table(
    devices: Sequence[Mapping[str, Any]], aliases: Mapping[str, str] | None = None
) -> RenderableType:
    """Inventory.  Warnings and unstable ids are shown, never quietly dropped."""
    if not devices:
        return Text("no devices; run: fdctl discover", style="dim")
    by_id: dict[str, list[str]] = {}
    for alias, target in (aliases or {}).items():
        by_id.setdefault(target, []).append(alias)

    table = Table(box=box.SIMPLE, expand=False)
    table.add_column("id", overflow="fold")
    table.add_column("kind")
    table.add_column("name", overflow="fold")
    table.add_column("state")
    table.add_column("roles")
    table.add_column("alias")
    warnings: list[RenderableType] = []
    for device in devices:
        device_id = str(device.get("id", "?"))
        state = str(device.get("state", "?"))
        name = str(device.get("display_name", ""))
        if device.get("simulated"):
            name += "  [sim]"
        table.add_row(
            device_id,
            str(device.get("kind", "?")),
            name,
            Text(state, style="green" if state == "READY" else "yellow"),
            ", ".join(str(role) for role in device.get("roles") or []) or "-",
            ", ".join(by_id.get(device_id, [])) or "-",
        )
        if device.get("warning"):
            warnings.append(Text(f"{device_id}: {device['warning']}", style="yellow"))
        if device.get("stable_id") is False:
            warnings.append(
                Text(
                    f"{device_id}: identified by a kernel name that can change between boots",
                    style="yellow",
                )
            )
    return Group(table, *warnings) if warnings else table


def device_detail(payload: Mapping[str, Any]) -> RenderableType:
    """``device.status``: descriptor, driver status and what it is busy with."""
    descriptor = dict(payload.get("descriptor") or {})
    metadata = descriptor.pop("metadata", {}) or {}
    capabilities = descriptor.pop("capabilities", []) or []
    blocks: list[RenderableType] = [
        kv_panel(str(descriptor.get("display_name", "device")), descriptor),
        Text(f"capabilities: {', '.join(str(item) for item in capabilities) or 'none'}"),
    ]
    if metadata:
        blocks.append(kv_panel("metadata", metadata))
    status = payload.get("status")
    if isinstance(status, Mapping) and status:
        blocks.append(kv_panel("driver status", status))
    busy = payload.get("busy_with")
    blocks.append(Text(f"busy with: {busy}" if busy else "idle", style="dim"))
    return Group(*blocks)


def action_table(actions: Sequence[Mapping[str, Any]]) -> RenderableType:
    """Every action with the permission it needs and whether it changes state.

    ``state changes`` gets its own column rather than being folded into the
    permission, because they answer different questions: one is what you must
    be authorized for, the other is whether the DUT will be different
    afterwards.
    """
    if not actions:
        return Text("no matching actions", style="dim")
    table = Table(box=box.SIMPLE, expand=False)
    table.add_column("action", overflow="fold")
    table.add_column("permission")
    table.add_column("changes state")
    table.add_column("estop ok")
    table.add_column("device", overflow="fold")
    table.add_column("description", overflow="fold")
    for descriptor in actions:
        table.add_row(
            str(descriptor.get("name", "?")),
            _permission_text(str(descriptor.get("permission", "?"))),
            Text("yes", style="yellow")
            if descriptor.get("state_changing")
            else Text("no", style="dim"),
            "yes" if descriptor.get("allowed_during_estop") else "-",
            str(descriptor.get("device_id") or "-"),
            str(descriptor.get("description", "")),
        )
    return table


def running_table(running: Sequence[Mapping[str, Any]]) -> RenderableType:
    if not running:
        return Text("no actions in flight", style="dim")
    return rows_table(
        "running actions",
        running,
        columns=["action", "device_id", "source", "permission", "elapsed_s", "request_id"],
    )


# ---------------------------------------------------------------------------
# Sessions and events
# ---------------------------------------------------------------------------


def session_table(sessions: Sequence[Mapping[str, Any]]) -> RenderableType:
    if not sessions:
        return Text('no sessions yet; start one with: fdctl session start "<name>"', style="dim")
    table = Table(box=box.SIMPLE, expand=False)
    table.add_column("id", overflow="fold")
    table.add_column("name", overflow="fold")
    table.add_column("state")
    table.add_column("started (utc)")
    table.add_column("live")
    for session in sessions:
        started = session.get("started_utc_ns")
        table.add_row(
            str(session.get("id", "?")),
            str(session.get("name", "")) + ("  [sim]" if session.get("simulated") else ""),
            str(session.get("state", "?")),
            format_utc_ns(int(started)) if started else "-",
            Text("ACTIVE", style="bold green") if session.get("active") else "-",
        )
    return table


def event_line(event: Mapping[str, Any]) -> Text:
    """One timeline record on one line, ordered the way an eye scans it."""
    severity = str(event.get("severity", "info"))
    utc = event.get("utc_ns")
    clock = format_utc_ns(int(utc))[11:23] if utc else "-"
    line = Text()
    line.append(f"{clock} ", style="dim")
    line.append(f"{severity[:4].upper():<5}", style=_SEVERITY_STYLES.get(severity, ""))
    line.append(f"{event.get('type', '?')} ", style="bold")
    for field in ("device_id", "action"):
        if event.get(field):
            line.append(f"{event[field]} ", style="cyan")
    if event.get("permission"):
        line.append(
            f"{event['permission']} ", style=_PERMISSION_STYLES.get(str(event["permission"]), "")
        )
    if event.get("message"):
        line.append(str(event["message"]))
    return line


def event_table(events: Sequence[Mapping[str, Any]]) -> RenderableType:
    if not events:
        return Text("no events", style="dim")
    return Group(*[event_line(event) for event in events])


@contextmanager
def live_status(emitter: Emitter) -> Iterator[Callable[[Mapping[str, Any]], None]]:
    """A single self-updating status line, or one JSON document per poll.

    Yields the update function so the caller only has to hand it a status
    payload; whether that becomes a repainted line or a line of NDJSON is a
    presentation decision and stays here.
    """
    if emitter.json_mode:
        yield emitter.stream
        return
    with Live(console=emitter.console, refresh_per_second=4) as live:
        yield lambda status: live.update(watch_line(status))
