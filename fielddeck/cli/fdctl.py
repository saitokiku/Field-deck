"""``fdctl`` — the deterministic path to every FieldDeck capability.

FieldDeck is manual-first: the HMI is a convenience and the AI is a guest, but
this CLI is the contract.  Anything an operator can do at the panel they must
be able to do here, in a command they can paste into a runbook and a script can
branch on.  That is why the command surface is wide and the output discipline
is narrow:

* ``--json`` puts one JSON document on stdout and nothing else — no ANSI, no
  status chatter, no progress line.  Human asides go to stderr.
* exit codes come from :data:`fielddeck.common.errors.EXIT_CODES`, so
  ``fdctl can send ...`` exiting 3 means "denied", not "something went wrong".
* every failure prints the message, the structured details worth reading, and
  the ``preserved`` line, because the first question after a failed capture is
  always "what did I lose?".

This module contains no hardware access and no authorization logic.  Every
command reaches :class:`~fielddeck.daemon.client.InstrumentClient` and reports
what ``instrumentd`` decided; a client that decided for itself what was allowed
would be a second, weaker safety model.

The one deliberate exception is the analysis toolbox — ``convert``, ``crc``,
``hash`` and ``analyze``.  Those are arithmetic over bytes, they need no
authority and no device, and an engineer staring at a hex dump on a laptop with
no daemon running should not be told to start a daemon.  They prefer the socket
when it is there (so the work lands on the session timeline) and fall back to
computing locally when it is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, NoReturn, TypeVar

import typer

from fielddeck import __version__
from fielddeck.cli import formatting as fmt
from fielddeck.cli.formatting import Emitter
from fielddeck.common.errors import (
    DEFAULT_ERROR_EXIT,
    EXIT_CODES,
    FieldDeckError,
    InvalidRequest,
    TransportError,
)
from fielddeck.common.models import ArmGrant, ArmScope, ClientSource, PermissionLevel
from fielddeck.daemon.client import InstrumentClient

__all__ = ["app", "main"]

T = TypeVar("T")

#: Permission classes that get a typed confirmation rather than a keystroke.
#: Everything here either rewrites a part or destroys something on it.
_CONFIRMED_PERMISSIONS = frozenset({PermissionLevel.FLASH, PermissionLevel.DESTRUCTIVE})

#: Renew an output lease three times inside its TTL.  Once would make a single
#: dropped renewal fatal; three keeps a rail up across a scheduling hiccup
#: without ever letting the dead-man interval grow.
_LEASE_RENEW_DIVISOR = 3.0


# ---------------------------------------------------------------------------
# Invocation state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Globals:
    """Options from the root command, stored on the click context.

    On the context rather than in a module global: a module global would be
    shared state between invocations, and the test suite runs many.
    """

    socket: Path | None
    json_mode: bool
    timeout_s: float
    assume_yes: bool
    color: bool


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything one command body needs to talk and to print."""

    socket: Path | None
    json_mode: bool
    timeout_s: float
    assume_yes: bool
    emitter: Emitter


def _settings(ctx: typer.Context, json_out: bool = False, *, yes: bool = False) -> Settings:
    """Merge the root options with this command's own ``--json``/``--yes``.

    Both spellings work — ``fdctl --json status`` and ``fdctl status --json`` —
    because operators type the second and scripts tend to generate the first.
    """
    globals_ = ctx.obj if isinstance(ctx.obj, _Globals) else _DEFAULT_GLOBALS
    json_mode = globals_.json_mode or json_out
    return Settings(
        socket=globals_.socket,
        json_mode=json_mode,
        timeout_s=globals_.timeout_s,
        assume_yes=globals_.assume_yes or yes,
        emitter=Emitter(json_mode=json_mode, color=globals_.color),
    )


_DEFAULT_GLOBALS = _Globals(
    socket=None, json_mode=False, timeout_s=30.0, assume_yes=False, color=True
)


# ---------------------------------------------------------------------------
# Talking to instrumentd
# ---------------------------------------------------------------------------


def exit_code_for(error: FieldDeckError) -> int:
    """The documented exit code for one error class."""
    return EXIT_CODES.get(str(error.code), DEFAULT_ERROR_EXIT)


def _die(settings: Settings, error: FieldDeckError, *, code: int | None = None) -> NoReturn:
    settings.emitter.error(error)
    raise typer.Exit(code if code is not None else exit_code_for(error))


def _attempt(settings: Settings, work: Callable[[InstrumentClient], Awaitable[T]]) -> T:
    """Run one unit of work against the daemon, letting errors propagate."""

    async def runner() -> T:
        client = InstrumentClient(
            settings.socket, source=ClientSource.FDCTL, timeout_s=settings.timeout_s
        )
        await client.connect()
        try:
            return await work(client)
        finally:
            await client.close()

    return asyncio.run(runner())


def _call(settings: Settings, work: Callable[[InstrumentClient], Awaitable[T]]) -> T:
    """The normal path: run the work, or render the failure and exit."""
    try:
        return _attempt(settings, work)
    except FieldDeckError as exc:
        _die(settings, exc)


def _execute(
    settings: Settings, action: str, params: Mapping[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    """Run one action and hand back its result payload."""
    result = _call(settings, lambda client: client.execute(action, dict(params or {}), **kwargs))
    return result.result


def _params(**values: Any) -> dict[str, Any]:
    """Only the parameters the operator actually gave.

    Action parameter models forbid unknown fields, and the same action name is
    served by drivers with slightly different options — a real bench supply has
    a channel, the simulated one does not.  An option nobody passed must be
    absent from the request, not present and null.
    """
    return {key: value for key, value in values.items() if value is not None}


def _require_confirmation(
    settings: Settings, *, word: str, title: str, lines: Sequence[str]
) -> None:
    """The second, deliberate confirmation before something irreversible.

    Refused rather than assumed when stdin is not a terminal: an unattended
    pipe must never be able to authorize a flash by reaching end-of-file.
    """
    if settings.assume_yes:
        return
    if settings.emitter.confirm_word(fmt.danger_notice(title, lines), expected=word):
        return
    _die(
        settings,
        InvalidRequest(
            f"not confirmed: {title}",
            details={
                "expected": word,
                "hint": "pass --yes to confirm non-interactively in a script",
            },
            preserved="nothing was changed and nothing was sent to a device",
        ),
        code=EXIT_CODES["usage"],
    )


def _usage_error(settings: Settings, message: str, **details: Any) -> NoReturn:
    _die(
        settings,
        InvalidRequest(message, details=details, preserved="nothing was attempted"),
        code=EXIT_CODES["usage"],
    )


def _permission(settings: Settings, raw: str) -> PermissionLevel:
    try:
        return PermissionLevel(raw.strip().upper())
    except ValueError:
        _usage_error(
            settings,
            f"unknown permission class {raw!r}",
            known=[str(level) for level in PermissionLevel],
        )


def _resolve_device_id(settings: Settings, reference: str) -> str:
    """Turn an alias or ``role:psu`` into the id an arm scope can match.

    Scope matching in the daemon compares device ids, so a grant scoped to
    ``role:psu`` would match nothing.  Resolving here, through a PASSIVE
    action, means the banner also shows the operator which physical device
    they just took responsibility for.
    """
    payload = _execute(settings, "device.status", {"device": reference})
    return str((payload.get("descriptor") or {}).get("id", reference))


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

JsonFlag = Annotated[
    bool, typer.Option("--json", help="Emit one JSON document on stdout and nothing else.")
]
YesFlag = Annotated[
    bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt (for scripts).")
]
DeviceArg = Annotated[
    str, typer.Argument(metavar="DEVICE", help="Device id, configured alias, or role:<role>.")
]

app = typer.Typer(
    name="fdctl",
    help="FieldDeck control. Every command goes through instrumentd.",
    no_args_is_help=True,
    add_completion=True,
    rich_markup_mode=None,
)
session_app = typer.Typer(help="Recording sessions and the unified timeline.", no_args_is_help=True)
estop_app = typer.Typer(help="Emergency stop.", invoke_without_command=True)
can_app = typer.Typer(help="CAN / CAN FD interfaces.", no_args_is_help=True)
serial_app = typer.Typer(help="Serial / UART / RS-232 / RS-485 ports.", no_args_is_help=True)
modbus_app = typer.Typer(help="Modbus RTU and TCP.", no_args_is_help=True)
bench_app = typer.Typer(help="Bench instruments over SCPI.", no_args_is_help=True)
scpi_app = typer.Typer(help="Raw SCPI queries.", no_args_is_help=True)
psu_app = typer.Typer(help="Programmable power supplies.", no_args_is_help=True)
recipe_app = typer.Typer(help="Test recipes.", no_args_is_help=True)

app.add_typer(session_app, name="session")
app.add_typer(estop_app, name="estop")
app.add_typer(can_app, name="can")
app.add_typer(serial_app, name="serial")
app.add_typer(modbus_app, name="modbus")
app.add_typer(bench_app, name="bench")
app.add_typer(scpi_app, name="scpi")
app.add_typer(psu_app, name="psu")
app.add_typer(recipe_app, name="recipe")


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    socket: Annotated[
        Path | None,
        typer.Option(
            "--socket",
            envvar="FIELDDECK_SOCKET",
            help="instrumentd control socket. Resolved from the install layout when unset.",
        ),
    ] = None,
    json_mode: Annotated[
        bool, typer.Option("--json", help="Emit JSON for every command in this invocation.")
    ] = False,
    timeout: Annotated[
        float, typer.Option("--timeout", min=0.1, help="Seconds to wait for a daemon reply.")
    ] = 30.0,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Answer every confirmation prompt in advance.")
    ] = False,
    no_color: Annotated[bool, typer.Option("--no-color", help="Disable ANSI styling.")] = False,
    version: Annotated[bool, typer.Option("--version", help="Print the version and exit.")] = False,
) -> None:
    """FieldDeck control."""
    ctx.obj = _Globals(
        socket=socket,
        json_mode=json_mode,
        timeout_s=timeout,
        assume_yes=yes,
        color=not no_color,
    )
    if version:
        print(f"fdctl {__version__}")
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(EXIT_CODES["usage"])


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


@app.command()
def status(ctx: typer.Context, json_out: JsonFlag = False) -> None:
    """The one-screen overview: safety, session, devices, work in flight.

    The status query tags itself with a request id and drops that one row from
    "running actions". Every invocation would otherwise report itself, and a
    panel that is never empty is a panel an operator stops reading — which is
    exactly the panel that should be shouting when a capture is still running.
    """
    settings = _settings(ctx, json_out)
    self_request = f"fdctl-status-{secrets.token_hex(4)}"

    async def work(client: InstrumentClient) -> tuple[dict[str, Any], dict[str, Any]]:
        overview = (await client.execute("system.status", request_id=self_request)).result
        devices = (await client.execute("device.list")).result
        overview["running_actions"] = [
            item
            for item in overview.get("running_actions") or []
            if item.get("request_id") != self_request
        ]
        return overview, devices

    overview, devices = _call(settings, work)
    payload = {
        **overview,
        "device_list": devices.get("devices", []),
        "aliases": devices.get("aliases", {}),
    }
    settings.emitter.emit(
        payload,
        lambda: fmt.status_view(overview, devices.get("devices", []), devices.get("aliases", {})),
    )


@app.command()
def discover(ctx: typer.Context, json_out: JsonFlag = False) -> None:
    """Re-run the passive inventory. Nothing is transmitted to a DUT."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "system.discover")
    devices = payload.get("devices", [])
    settings.emitter.emit(payload, lambda: fmt.device_table(devices))
    for gone in payload.get("removed") or []:
        settings.emitter.warn(f"{gone} is no longer present")


@app.command()
def devices(ctx: typer.Context, json_out: JsonFlag = False) -> None:
    """Every device instrumentd currently knows about."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "device.list")
    settings.emitter.emit(
        payload,
        lambda: fmt.device_table(payload.get("devices", []), payload.get("aliases", {})),
    )


@app.command()
def device(ctx: typer.Context, reference: DeviceArg, json_out: JsonFlag = False) -> None:
    """Descriptor and driver status for one device. Never transmits."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "device.status", {"device": reference})
    settings.emitter.emit(payload, lambda: fmt.device_detail(payload))


@app.command()
def actions(
    ctx: typer.Context,
    device_ref: Annotated[
        str | None, typer.Option("--device", help="Only actions offered by this device.")
    ] = None,
    permission: Annotated[
        str | None, typer.Option("--permission", help="Only actions in this permission class.")
    ] = None,
    json_out: JsonFlag = False,
) -> None:
    """Available actions and the permission each one requires."""
    settings = _settings(ctx, json_out)
    level = _permission(settings, permission) if permission else None
    payload = _execute(
        settings,
        "action.list",
        _params(device=device_ref, permission=str(level) if level else None),
    )
    settings.emitter.emit(payload, lambda: fmt.action_table(payload.get("actions", [])))


@app.command()
def limits(ctx: typer.Context, json_out: JsonFlag = False) -> None:
    """Effective safety limits and authorization policy for this unit."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "system.limits")
    settings.emitter.emit(payload)


def _parse_kv(settings: Settings, pairs: Sequence[str]) -> dict[str, Any]:
    """Parse ``key=value`` arguments, reading each value as JSON when it parses.

    So ``count=3`` is a number, ``enabled=true`` is a boolean and
    ``label=capture`` stays a string.  Anything the action's own schema
    disagrees with is rejected by the daemon, not guessed at here.
    """
    params: dict[str, Any] = {}
    for pair in pairs:
        key, separator, raw = pair.partition("=")
        if not separator or not key:
            _usage_error(settings, f"{pair!r} is not key=value", example="count=3")
        try:
            params[key] = json.loads(raw)
        except json.JSONDecodeError:
            params[key] = raw
    return params


@app.command("call")
def call_action(
    ctx: typer.Context,
    action: Annotated[str, typer.Argument(help="Action name, e.g. logic.capture.")],
    pairs: Annotated[
        list[str] | None, typer.Argument(metavar="KEY=VALUE...", help="Action parameters.")
    ] = None,
    timeout: Annotated[
        float | None, typer.Option("--action-timeout", help="Override the action's own deadline.")
    ] = None,
    yes: YesFlag = False,
    json_out: JsonFlag = False,
) -> None:
    """Run any registered action by name.

    The escape hatch that keeps this CLI complete: subsystems with no bespoke
    command family here — logic capture, GPIO, network, flash — are still
    fully reachable, with the same authorization, limits and audit trail. The
    permission is read from the daemon's own descriptor, so a FLASH or
    DESTRUCTIVE action still asks for confirmation.
    """
    settings = _settings(ctx, json_out, yes=yes)
    params = _parse_kv(settings, pairs or [])

    descriptor = _call(settings, lambda client: _describe_action(client, action))
    if descriptor is not None:
        level = _permission(settings, str(descriptor.get("permission", "PASSIVE")))
        if level in _CONFIRMED_PERMISSIONS:
            _require_confirmation(
                settings,
                word=str(level),
                title=f"{action} requires {level}",
                lines=[
                    str(descriptor.get("description", "")),
                    f"state changing: {'yes' if descriptor.get('state_changing') else 'no'}",
                    f"parameters: {json.dumps(params, default=str)}",
                    str(descriptor.get("safe_state_note") or ""),
                ],
            )
    payload = _execute(settings, action, params, timeout_s=timeout)
    settings.emitter.emit(payload)


async def _describe_action(client: InstrumentClient, action: str) -> dict[str, Any] | None:
    """The daemon's own descriptor for one action, or None if it has none."""
    catalogue = (await client.execute("action.list")).result
    for item in catalogue.get("actions", []):
        if item.get("name") == action:
            return dict(item)
    return None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@session_app.command("start")
def session_start(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="What this session is for, e.g. 'bench bringup'.")],
    operator: Annotated[
        str | None, typer.Option("--operator", help="Who is running the bench.")
    ] = None,
    json_out: JsonFlag = False,
) -> None:
    """Open a recording session. Everything after this lands on its timeline."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "session.start", _params(name=name, operator=operator))
    session = payload.get("session", {})
    settings.emitter.emit(payload, lambda: fmt.kv_panel("session started", session))


@session_app.command("stop")
def session_stop(ctx: typer.Context, json_out: JsonFlag = False) -> None:
    """Close the active session and finalise its artifacts."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "session.stop")
    settings.emitter.emit(
        payload, lambda: fmt.kv_panel("session closed", payload.get("session", {}))
    )


@session_app.command("list")
def session_list(ctx: typer.Context, json_out: JsonFlag = False) -> None:
    """Sessions stored on this unit, newest first."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "session.list")
    settings.emitter.emit(payload, lambda: fmt.session_table(payload.get("sessions", [])))


@session_app.command("show")
def session_show(
    ctx: typer.Context,
    session_id: Annotated[
        str | None, typer.Argument(help="Session id; defaults to the active session.")
    ] = None,
    json_out: JsonFlag = False,
) -> None:
    """Metadata, timeline summary and artifacts for one session."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "session.get", _params(session_id=session_id))
    settings.emitter.emit(payload)


@session_app.command("mark")
def session_mark(
    ctx: typer.Context,
    label: Annotated[str, typer.Argument(help="Short label for this instant.")],
    note: Annotated[str | None, typer.Option("--note", help="Longer explanation.")] = None,
    json_out: JsonFlag = False,
) -> None:
    """Drop an operator mark on the timeline, right now."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "session.mark", _params(label=label, note=note))
    settings.emitter.emit(payload, lambda: fmt.kv_panel("mark", payload.get("mark", {})))


@session_app.command("note")
def session_note(
    ctx: typer.Context,
    text: Annotated[str, typer.Argument(help="Free text to append to the session.")],
    json_out: JsonFlag = False,
) -> None:
    """Append a note to the active session."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "session.note", {"text": text})
    settings.emitter.emit(payload)


@session_app.command("events")
def session_events(
    ctx: typer.Context,
    session_id: Annotated[str | None, typer.Option("--session", help="Defaults to active.")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=10000)] = 200,
    offset: Annotated[int, typer.Option("--offset", min=0)] = 0,
    types: Annotated[
        list[str] | None, typer.Option("--type", help="Event type; repeatable.")
    ] = None,
    device_ref: Annotated[str | None, typer.Option("--device", help="Filter by device id.")] = None,
    severity: Annotated[
        str | None, typer.Option("--severity", help="Minimum severity, e.g. warning.")
    ] = None,
    json_out: JsonFlag = False,
) -> None:
    """Query the recorded timeline of a session."""
    settings = _settings(ctx, json_out)
    payload = _execute(
        settings,
        "session.events",
        _params(
            session_id=session_id,
            limit=limit,
            offset=offset,
            types=list(types) if types else None,
            device_id=device_ref,
            severity_at_least=severity,
        ),
    )
    settings.emitter.emit(payload, lambda: fmt.event_table(payload.get("events", [])))


@session_app.command("window")
def session_window(
    ctx: typer.Context,
    session_id: Annotated[str | None, typer.Option("--session", help="Defaults to active.")] = None,
    center_ns: Annotated[
        int | None, typer.Option("--center-ns", help="Monotonic nanoseconds to centre on.")
    ] = None,
    around: Annotated[
        str | None, typer.Option("--around", help="Centre on the first event of this type.")
    ] = None,
    before_ms: Annotated[float, typer.Option("--before-ms", min=0, max=600_000)] = 300.0,
    after_ms: Annotated[float, typer.Option("--after-ms", min=0, max=600_000)] = 100.0,
    limit: Annotated[int, typer.Option("--limit", min=1, max=20000)] = 1000,
    json_out: JsonFlag = False,
) -> None:
    """Everything that happened around one instant, across every subsystem.

    The correlation query: 'what was the supply doing 300 ms before the CAN
    frame stopped?'. Give either --center-ns or --around.
    """
    settings = _settings(ctx, json_out)
    if center_ns is None and not around:
        _usage_error(
            settings,
            "give either --center-ns or --around",
            example="fdctl session window --around DEVICE_FAULT --before-ms 300",
        )
    payload = _execute(
        settings,
        "session.window",
        _params(
            session_id=session_id,
            center_monotonic_ns=center_ns,
            around_event_type=around,
            before_ms=before_ms,
            after_ms=after_ms,
            limit=limit,
        ),
    )
    settings.emitter.emit(payload)


@session_app.command("summary")
def session_summary(
    ctx: typer.Context,
    session_id: Annotated[
        str | None, typer.Argument(help="Defaults to the active session.")
    ] = None,
    json_out: JsonFlag = False,
) -> None:
    """Deterministic summary of a session, suitable for a report."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "session.summary", _params(session_id=session_id))
    settings.emitter.emit(payload)


@session_app.command("report")
def session_report(
    ctx: typer.Context,
    session_id: Annotated[
        str | None, typer.Argument(help="Defaults to the active session.")
    ] = None,
    report_format: Annotated[str, typer.Option("--format", help="markdown or json.")] = "markdown",
    save: Annotated[
        bool, typer.Option("--save/--no-save", help="Write it into the session.")
    ] = True,
    json_out: JsonFlag = False,
) -> None:
    """Build the session report. Measurements and interpretations stay separate."""
    settings = _settings(ctx, json_out)
    payload = _execute(
        settings,
        "session.report",
        _params(session_id=session_id, format=report_format, save=save),
    )
    markdown = payload.get("markdown")
    if settings.json_mode or not markdown:
        settings.emitter.emit(payload)
        return
    print(markdown)
    if payload.get("saved_to"):
        settings.emitter.note(f"saved to {payload['saved_to']}")


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


@app.command()
def arm(
    ctx: typer.Context,
    classes: Annotated[
        list[str],
        typer.Argument(
            metavar="CLASS...", help="One or more of: query control power flash destructive."
        ),
    ],
    ttl: Annotated[
        float | None, typer.Option("--ttl", min=0.1, help="Seconds. Clamped by policy.")
    ] = None,
    device_ref: Annotated[
        str | None, typer.Option("--device", help="Scope the grant to one device.")
    ] = None,
    action_name: Annotated[
        str | None, typer.Option("--action", help="Scope the grant to one action.")
    ] = None,
    note: Annotated[str | None, typer.Option("--note", help="Why you are arming; audited.")] = None,
    yes: YesFlag = False,
    json_out: JsonFlag = False,
) -> None:
    """Authorize one or more permission classes for a bounded time.

    Authorization is exact-class: a POWER grant does not authorize a CONTROL
    action. Arm both in one command when you need both.

    The grant lives only in the running daemon. Restarting instrumentd or
    rebooting the Pi returns the unit to SAFE.
    """
    settings = _settings(ctx, json_out, yes=yes)
    levels = [_permission(settings, item) for item in classes]
    for level in levels:
        if not level.requires_grant:
            _usage_error(
                settings,
                "PASSIVE needs no authorization; there is nothing to arm",
                given=str(level),
            )
    if device_ref and action_name:
        _usage_error(settings, "a grant is scoped to a device or to an action, not both")

    scope = ArmScope()
    if action_name:
        scope = ArmScope(kind="action", action=action_name)
    elif device_ref:
        scope = ArmScope(kind="device", device_id=_resolve_device_id(settings, device_ref))

    dangerous = [level for level in levels if level in _CONFIRMED_PERMISSIONS]
    if dangerous:
        _require_confirmation(
            settings,
            word=str(dangerous[0]),
            title=f"arming {', '.join(str(level) for level in dangerous)}",
            lines=[
                "These classes rewrite or destroy what is on the target.",
                f"scope: {scope.describe()}",
                "Anything already connected to the bench is in scope for the TTL.",
            ],
        )

    async def work(client: InstrumentClient) -> list[ArmGrant]:
        grants: list[ArmGrant] = []
        for level in levels:
            reply = await client.call(
                "safety.arm",
                _params(
                    permission=str(level),
                    ttl_s=ttl,
                    scope=scope.model_dump(mode="json"),
                    note=note,
                ),
            )
            grants.append(ArmGrant.model_validate(reply["grant"]))
        return grants

    grants = _call(settings, work)
    settings.emitter.emit(
        {"grants": [grant.model_dump(mode="json") for grant in grants]},
        lambda: fmt.arm_banner(grants),
    )
    for grant in grants:
        if ttl is not None and grant.ttl_s < ttl:
            settings.emitter.warn(
                f"{grant.permission} TTL was clamped from {ttl:g}s to the policy maximum "
                f"of {grant.ttl_s:g}s"
            )


@app.command()
def disarm(
    ctx: typer.Context,
    grant_id: Annotated[
        str | None, typer.Option("--grant-id", help="Revoke one grant; omit to revoke all.")
    ] = None,
    json_out: JsonFlag = False,
) -> None:
    """Revoke authorization. Never needs confirmation: this is the safe direction."""
    settings = _settings(ctx, json_out)
    payload = _call(
        settings, lambda client: client.call("safety.disarm", _params(grant_id=grant_id))
    )
    revoked = payload.get("revoked") or []
    settings.emitter.emit(
        payload,
        lambda: fmt.kv_panel(
            "disarmed",
            {"revoked": revoked or "nothing was armed", "state": "SAFE unless a lease is held"},
        ),
    )


@estop_app.callback(invoke_without_command=True)
def estop(
    ctx: typer.Context,
    reason: Annotated[
        str | None, typer.Option("--reason", help="What you saw. Recorded on the timeline.")
    ] = None,
    json_out: JsonFlag = False,
) -> None:
    """Stop everything now: outputs to safe state, grants revoked, evidence kept.

    Any client may trigger this, including the assistant. Stopping is never the
    dangerous direction, so it is never gated on a grant or a confirmation.
    """
    if ctx.invoked_subcommand is not None:
        return
    settings = _settings(ctx, json_out)
    payload = _call(settings, lambda client: client.call("safety.estop", _params(reason=reason)))
    settings.emitter.emit(
        payload,
        lambda: fmt.danger_notice(
            "EMERGENCY STOP ENGAGED",
            [
                f"reason: {payload.get('reason', 'requested by fdctl')}",
                f"leases surrendered: {len(payload.get('surrendered_leases') or [])}",
                str(payload.get("evidence", "")),
                "Clear it with: fdctl estop clear",
            ],
        ),
    )


@estop_app.command("clear")
def estop_clear(ctx: typer.Context, yes: YesFlag = False, json_out: JsonFlag = False) -> None:
    """Acknowledge and clear a latched emergency stop.

    Confirmed deliberately because clearing it is what makes the bench armable
    again. It does not re-arm anything and it does not re-energise anything.
    """
    settings = _settings(ctx, json_out, yes=yes)
    _require_confirmation(
        settings,
        word="CLEAR",
        title="clearing the emergency stop",
        lines=[
            "Confirm the hazard that caused it is actually gone.",
            "Clearing revokes nothing and energises nothing, but it allows arming again.",
        ],
    )
    payload = _call(settings, lambda client: client.call("safety.estop_clear", {}))
    settings.emitter.emit(payload, lambda: fmt.kv_panel("emergency stop cleared", payload))


# ---------------------------------------------------------------------------
# Buses
# ---------------------------------------------------------------------------


def _filtered_devices(
    settings: Settings, *, kinds: Sequence[str] = (), roles: Sequence[str] = ()
) -> dict[str, Any]:
    """One family's slice of the inventory.

    Filtering happens here rather than in the daemon because it is a display
    choice, not an authorization one: ``device.list`` already returned
    everything this client is allowed to see.
    """
    payload = _execute(settings, "device.list")
    wanted_kinds = set(kinds)
    wanted_roles = set(roles)
    selected = [
        device
        for device in payload.get("devices", [])
        if (not wanted_kinds or str(device.get("kind")) in wanted_kinds)
        and (not wanted_roles or wanted_roles & {str(role) for role in device.get("roles") or []})
    ]
    return {"devices": selected, "aliases": payload.get("aliases", {})}


def _hex_ids(settings: Settings, values: Sequence[str] | None) -> list[int] | None:
    """Parse CAN arbitration ids given as ``0x181`` or ``385``."""
    if not values:
        return None
    parsed: list[int] = []
    for raw in values:
        try:
            parsed.append(int(raw, 0))
        except ValueError:
            _usage_error(settings, f"{raw!r} is not a CAN id", example="--id 0x181")
    return parsed


@can_app.command("interfaces")
def can_interfaces(ctx: typer.Context, json_out: JsonFlag = False) -> None:
    """CAN interfaces known to instrumentd."""
    settings = _settings(ctx, json_out)
    payload = _filtered_devices(settings, kinds=["can"])
    settings.emitter.emit(payload, lambda: fmt.device_table(payload["devices"], payload["aliases"]))


@can_app.command("status")
def can_status(ctx: typer.Context, reference: DeviceArg, json_out: JsonFlag = False) -> None:
    """Interface configuration and error counters."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "can.status", {"device": reference})
    settings.emitter.emit(payload, lambda: fmt.kv_panel(reference, payload))


@can_app.command("listen")
def can_listen(
    ctx: typer.Context,
    reference: DeviceArg,
    seconds: Annotated[float, typer.Option("--seconds", min=0.001, max=3600)] = 2.0,
    max_frames: Annotated[int, typer.Option("--max-frames", min=1, max=200_000)] = 2000,
    id_filter: Annotated[
        list[str] | None, typer.Option("--id", help="Only these arbitration ids; repeatable.")
    ] = None,
    json_out: JsonFlag = False,
) -> None:
    """Receive frames without transmitting. Listen-only: nothing reaches the bus."""
    settings = _settings(ctx, json_out)
    payload = _execute(
        settings,
        "can.listen",
        _params(
            device=reference,
            duration_s=seconds,
            max_frames=max_frames,
            id_filter=_hex_ids(settings, id_filter),
        ),
        timeout_s=seconds + 30.0,
    )
    frames = payload.get("frames", [])
    settings.emitter.emit(
        payload,
        lambda: fmt.rows_table(
            f"{payload.get('count', 0)} frames in {payload.get('duration_s', 0)}s "
            f"({payload.get('mode', '?')})",
            _display_frames(frames),
            columns=["monotonic_ns", "can_id", "dlc", "data", "description"],
        ),
    )


def _display_frames(frames: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Render CAN frames the way every other CAN tool does.

    Arbitration ids arrive as integers and are shown in hex, because that is
    how they appear in a DBC, in candump, on a scope decode and in every
    datasheet — an engineer reading 257 has to convert it to 0x101 before it
    means anything.
    """
    out: list[dict[str, Any]] = []
    for frame in frames:
        row = dict(frame)
        raw = row.get("can_id")
        if isinstance(raw, int):
            width = 8 if row.get("extended") or raw > 0x7FF else 3
            row["can_id"] = f"0x{raw:0{width}X}"
        out.append(row)
    return out


@can_app.command("capture")
def can_capture(
    ctx: typer.Context,
    reference: DeviceArg,
    seconds: Annotated[float, typer.Option("--seconds", min=0.001, max=3600)] = 2.0,
    max_frames: Annotated[int, typer.Option("--max-frames", min=1, max=200_000)] = 2000,
    label: Annotated[str, typer.Option("--label", help="Names the capture file.")] = "capture",
    id_filter: Annotated[list[str] | None, typer.Option("--id")] = None,
    json_out: JsonFlag = False,
) -> None:
    """Record frames into an immutable capture file in the active session."""
    settings = _settings(ctx, json_out)
    payload = _execute(
        settings,
        "can.capture",
        _params(
            device=reference,
            duration_s=seconds,
            max_frames=max_frames,
            label=label,
            id_filter=_hex_ids(settings, id_filter),
        ),
        timeout_s=seconds + 30.0,
    )
    if payload.get("warning"):
        settings.emitter.warn(str(payload["warning"]))
    settings.emitter.emit(
        payload, lambda: fmt.kv_panel("capture", payload.get("artifact") or payload)
    )


@can_app.command("stats")
def can_stats(
    ctx: typer.Context,
    reference: DeviceArg,
    seconds: Annotated[float, typer.Option("--seconds", min=0.001, max=60)] = 2.0,
    json_out: JsonFlag = False,
) -> None:
    """Per-arbitration-id rate, period and jitter."""
    settings = _settings(ctx, json_out)
    payload = _execute(
        settings,
        "can.stats",
        {"device": reference, "duration_s": seconds},
        timeout_s=seconds + 30.0,
    )
    settings.emitter.emit(
        payload,
        lambda: fmt.rows_table(
            f"{payload.get('total_frames', 0)} frames, bus load "
            f"{payload.get('bus_load_percent', 0)}%",
            payload.get("ids", []),
            limit=64,
        ),
    )


@can_app.command("send")
def can_send(
    ctx: typer.Context,
    reference: DeviceArg,
    can_id: Annotated[str, typer.Option("--id", help="Arbitration id, e.g. 0x123.")],
    data: Annotated[str, typer.Option("--data", help="Payload as hex, e.g. '01 03 00 00'.")],
    extended: Annotated[bool, typer.Option("--extended", help="29-bit identifier.")] = False,
    count: Annotated[int, typer.Option("--count", min=1, max=1000)] = 1,
    json_out: JsonFlag = False,
) -> None:
    """Transmit a frame. Requires an active CONTROL grant: this reaches the DUT."""
    settings = _settings(ctx, json_out)
    ids = _hex_ids(settings, [can_id]) or []
    payload = _execute(
        settings,
        "can.send",
        {
            "device": reference,
            "can_id": ids[0],
            "data": data,
            "extended": extended,
            "count": count,
        },
    )
    settings.emitter.emit(payload, lambda: fmt.kv_panel("transmitted", payload))


@can_app.command("decode")
def can_decode(
    ctx: typer.Context,
    reference: DeviceArg,
    dbc: Annotated[str, typer.Option("--dbc", help="Path to a .dbc/.kcd/.sym database.")],
    path: Annotated[
        str | None, typer.Option("--path", help="Capture file, relative to the session directory.")
    ] = None,
    artifact_id: Annotated[
        str | None, typer.Option("--artifact-id", help="Capture artifact in the session.")
    ] = None,
    label: Annotated[str, typer.Option("--label")] = "decoded",
    json_out: JsonFlag = False,
) -> None:
    """Decode a stored capture against a DBC into a derived artifact.

    Post-processing over bytes already on disk. The derived CSV records which
    capture and which database produced it.
    """
    settings = _settings(ctx, json_out)
    if bool(path) == bool(artifact_id):
        _usage_error(settings, "give exactly one of --path or --artifact-id")
    payload = _execute(
        settings,
        "can.decode",
        _params(device=reference, dbc=dbc, path=path, artifact_id=artifact_id, label=label),
        timeout_s=300.0,
    )
    settings.emitter.emit(payload)


@serial_app.command("list")
def serial_list(ctx: typer.Context, json_out: JsonFlag = False) -> None:
    """Serial ports known to instrumentd."""
    settings = _settings(ctx, json_out)
    payload = _filtered_devices(settings, kinds=["serial"])
    settings.emitter.emit(payload, lambda: fmt.device_table(payload["devices"], payload["aliases"]))


@serial_app.command("status")
def serial_status(ctx: typer.Context, reference: DeviceArg, json_out: JsonFlag = False) -> None:
    """Port framing and byte counters. The electrical class is never inferred."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "serial.status", {"device": reference})
    settings.emitter.emit(payload, lambda: fmt.kv_panel(reference, payload))


@serial_app.command("monitor")
def serial_monitor(
    ctx: typer.Context,
    reference: DeviceArg,
    seconds: Annotated[float, typer.Option("--seconds", min=0.001, max=3600)] = 2.0,
    max_bytes: Annotated[int, typer.Option("--max-bytes", min=1, max=8_000_000)] = 65536,
    json_out: JsonFlag = False,
) -> None:
    """Receive from a port without transmitting."""
    settings = _settings(ctx, json_out)
    payload = _execute(
        settings,
        "serial.monitor",
        {"device": reference, "duration_s": seconds, "max_bytes": max_bytes},
        timeout_s=seconds + 30.0,
    )
    settings.emitter.emit(payload)


@serial_app.command("capture")
def serial_capture(
    ctx: typer.Context,
    reference: DeviceArg,
    seconds: Annotated[float, typer.Option("--seconds", min=0.001, max=3600)] = 2.0,
    max_bytes: Annotated[int, typer.Option("--max-bytes", min=1, max=8_000_000)] = 65536,
    label: Annotated[str, typer.Option("--label")] = "capture",
    json_out: JsonFlag = False,
) -> None:
    """Record bytes into an immutable capture file in the active session."""
    settings = _settings(ctx, json_out)
    payload = _execute(
        settings,
        "serial.capture",
        {"device": reference, "duration_s": seconds, "max_bytes": max_bytes, "label": label},
        timeout_s=seconds + 30.0,
    )
    if payload.get("warning"):
        settings.emitter.warn(str(payload["warning"]))
    settings.emitter.emit(payload)


@serial_app.command("send")
def serial_send(
    ctx: typer.Context,
    reference: DeviceArg,
    hex_payload: Annotated[str | None, typer.Option("--hex", help="Payload as hex bytes.")] = None,
    text: Annotated[str | None, typer.Option("--text", help="Payload as text.")] = None,
    newline: Annotated[bool, typer.Option("--newline", help="Append CR LF.")] = False,
    json_out: JsonFlag = False,
) -> None:
    """Transmit bytes. Requires an active CONTROL grant: these reach the DUT."""
    settings = _settings(ctx, json_out)
    if bool(hex_payload) == bool(text):
        _usage_error(settings, "give exactly one of --hex or --text")
    payload = _execute(
        settings,
        "serial.send",
        _params(device=reference, hex=hex_payload, text=text, append_newline=newline),
    )
    settings.emitter.emit(payload, lambda: fmt.kv_panel("transmitted", payload))


@serial_app.command("configure")
def serial_configure(
    ctx: typer.Context,
    reference: DeviceArg,
    baud: Annotated[int, typer.Option("--baud", min=50, max=12_000_000)] = 115200,
    bytesize: Annotated[int, typer.Option("--bytesize", min=5, max=8)] = 8,
    parity: Annotated[str, typer.Option("--parity", help="N, E, O, M or S.")] = "N",
    stopbits: Annotated[float, typer.Option("--stopbits")] = 1.0,
    rtscts: Annotated[
        bool | None, typer.Option("--rtscts/--no-rtscts", help="Hardware flow control.")
    ] = None,
    xonxoff: Annotated[
        bool | None, typer.Option("--xonxoff/--no-xonxoff", help="Software flow control.")
    ] = None,
    electrical: Annotated[
        str | None,
        typer.Option("--electrical", help="ttl, rs232, rs485 or unknown. Recorded, never guessed."),
    ] = None,
    json_out: JsonFlag = False,
) -> None:
    """Set this end of the link. No bytes reach the DUT.

    Software cannot tell TTL from RS-232 from RS-485. --electrical records what
    you know about the wiring; it never changes what the adapter is.
    """
    settings = _settings(ctx, json_out)
    payload = _execute(
        settings,
        "serial.configure",
        _params(
            device=reference,
            baudrate=baud,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            rtscts=rtscts,
            xonxoff=xonxoff,
            electrical=electrical,
        ),
    )
    settings.emitter.emit(payload, lambda: fmt.kv_panel(reference, payload))


_MODBUS_READ_ACTIONS = {
    "holding": "modbus.read_holding",
    "input": "modbus.read_input",
    "coils": "modbus.read_coils",
    "discrete": "modbus.read_discrete",
}


@modbus_app.command("read")
def modbus_read(
    ctx: typer.Context,
    device_ref: Annotated[str, typer.Option("--device", help="Device id, alias or role:bus.")],
    slave: Annotated[int, typer.Option("--slave", min=1, max=247)] = 1,
    kind: Annotated[
        str, typer.Option("--kind", help="holding, input, coils or discrete.")
    ] = "holding",
    address: Annotated[int, typer.Option("--address", min=0)] = 0,
    count: Annotated[int, typer.Option("--count", min=1)] = 1,
    word_order: Annotated[
        str, typer.Option("--word-order", help="Which register holds the high word.")
    ] = "big",
    byte_order: Annotated[str, typer.Option("--byte-order")] = "big",
    json_out: JsonFlag = False,
) -> None:
    """Read from a Modbus station. QUERY, never passive: it addresses the bus."""
    settings = _settings(ctx, json_out)
    action = _MODBUS_READ_ACTIONS.get(kind)
    if action is None:
        _usage_error(
            settings, f"unknown register kind {kind!r}", known=sorted(_MODBUS_READ_ACTIONS)
        )
    bits = kind in {"coils", "discrete"}
    payload = _execute(
        settings,
        action,
        _params(
            device=device_ref,
            slave=slave,
            address=address,
            count=count,
            word_order=None if bits else word_order,
            byte_order=None if bits else byte_order,
        ),
    )
    settings.emitter.emit(payload)


@modbus_app.command("write")
def modbus_write(
    ctx: typer.Context,
    device_ref: Annotated[str, typer.Option("--device")],
    address: Annotated[int, typer.Option("--address", min=0)],
    slave: Annotated[int, typer.Option("--slave", min=1, max=247)] = 1,
    coil: Annotated[
        bool | None, typer.Option("--coil/--no-coil", help="Write a single coil.")
    ] = None,
    register: Annotated[int | None, typer.Option("--register", help="Write one register.")] = None,
    registers: Annotated[
        str | None, typer.Option("--registers", help="Comma-separated words to write.")
    ] = None,
    json_out: JsonFlag = False,
) -> None:
    """Write to a Modbus station. Requires a CONTROL grant: this changes the DUT."""
    settings = _settings(ctx, json_out)
    given = [
        name
        for name, value in (("--coil", coil), ("--register", register), ("--registers", registers))
        if value is not None
    ]
    if len(given) != 1:
        _usage_error(settings, "give exactly one of --coil, --register or --registers", given=given)
    extra: dict[str, Any]
    if coil is not None:
        action, extra = "modbus.write_coil", {"value": coil}
    elif register is not None:
        action, extra = "modbus.write_register", {"value": register}
    else:
        words: list[int] = []
        for item in str(registers).split(","):
            try:
                words.append(int(item.strip(), 0))
            except ValueError:
                _usage_error(
                    settings, f"{item!r} is not a register value", example="--registers 1,2,0x10"
                )
        action, extra = "modbus.write_registers", {"values": words}
    payload = _execute(
        settings, action, {"device": device_ref, "slave": slave, "address": address, **extra}
    )
    settings.emitter.emit(payload)


@modbus_app.command("scan")
def modbus_scan(
    ctx: typer.Context,
    device_ref: Annotated[str, typer.Option("--device")],
    start: Annotated[int, typer.Option("--start", min=1, max=247)] = 1,
    end: Annotated[int, typer.Option("--end", min=1, max=247)] = 16,
    probe: Annotated[
        str, typer.Option("--probe", help="Which read to use as the probe.")
    ] = "holding",
    address: Annotated[int, typer.Option("--address", min=0)] = 0,
    count: Annotated[int, typer.Option("--count", min=1, max=8)] = 1,
    per_address_timeout: Annotated[float, typer.Option("--per-address-timeout", min=0.01)] = 0.3,
    json_out: JsonFlag = False,
) -> None:
    """Bounded address scan. QUERY: it sends one read to every station in range."""
    settings = _settings(ctx, json_out)
    payload = _execute(
        settings,
        "modbus.scan",
        {
            "device": device_ref,
            "start": start,
            "end": end,
            "probe": probe,
            "address": address,
            "count": count,
            "per_address_timeout_s": per_address_timeout,
        },
        timeout_s=max(60.0, (end - start + 1) * per_address_timeout * 2 + 30.0),
    )
    settings.emitter.emit(payload)


# ---------------------------------------------------------------------------
# Bench instruments
# ---------------------------------------------------------------------------

#: Roles that make a device "a bench instrument" for listing purposes.
_BENCH_ROLES = ("psu", "dmm", "scope", "load", "funcgen", "counter", "generic_scpi")


@bench_app.command("list")
def bench_list(ctx: typer.Context, json_out: JsonFlag = False) -> None:
    """Bench instruments known to instrumentd."""
    settings = _settings(ctx, json_out)
    payload = _filtered_devices(settings, roles=_BENCH_ROLES)
    settings.emitter.emit(payload, lambda: fmt.device_table(payload["devices"], payload["aliases"]))


@bench_app.command("identify")
def bench_identify(ctx: typer.Context, reference: DeviceArg, json_out: JsonFlag = False) -> None:
    """Ask an instrument who it is. QUERY: this transmits *IDN? to it."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "bench.identify", {"device": reference})
    settings.emitter.emit(payload, lambda: fmt.kv_panel(reference, payload))


@bench_app.command("status")
def bench_status(ctx: typer.Context, reference: DeviceArg, json_out: JsonFlag = False) -> None:
    """Cached instrument state. Does not talk to the instrument."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "bench.status", {"device": reference})
    settings.emitter.emit(payload, lambda: fmt.kv_panel(reference, payload))


@scpi_app.command("query")
def scpi_query(
    ctx: typer.Context,
    reference: DeviceArg,
    command: Annotated[str, typer.Argument(help='SCPI query, e.g. "*IDN?".')],
    json_out: JsonFlag = False,
) -> None:
    """Send a SCPI query and print the response.

    Queries only. Anything that would change instrument state is refused by the
    daemon in favour of the typed actions, because 'OUTP ON;*IDN?' ends in a
    question mark right up until it energises something.
    """
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "scpi.query", {"device": reference, "command": command})
    settings.emitter.emit(payload, lambda: fmt.kv_panel(command, payload))


@psu_app.command("status")
def psu_status(ctx: typer.Context, reference: DeviceArg, json_out: JsonFlag = False) -> None:
    """Cached supply state, without querying the instrument."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "psu.status", {"device": reference})
    settings.emitter.emit(payload, lambda: fmt.kv_panel(reference, payload))


@psu_app.command("measure")
def psu_measure(ctx: typer.Context, reference: DeviceArg, json_out: JsonFlag = False) -> None:
    """Read output voltage and current. QUERY: this sends SCPI to the supply."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "psu.measure", {"device": reference})
    settings.emitter.emit(payload, lambda: fmt.kv_panel(reference, payload))


@psu_app.command("set")
def psu_set(
    ctx: typer.Context,
    reference: DeviceArg,
    voltage: Annotated[float | None, typer.Option("--voltage", min=0.0, help="Volts.")] = None,
    current_limit: Annotated[
        float | None, typer.Option("--current-limit", min=0.0, help="Amps.")
    ] = None,
    channel: Annotated[int | None, typer.Option("--channel", min=1, max=8)] = None,
    json_out: JsonFlag = False,
) -> None:
    """Change setpoints. Requires POWER, and the daemon still enforces limits.

    Authorization and limits are independent: a POWER grant lets you ask, the
    configured limits decide whether the answer is yes.
    """
    settings = _settings(ctx, json_out)
    if voltage is None and current_limit is None:
        _usage_error(settings, "give --voltage and/or --current-limit")
    payload = _execute(
        settings,
        "psu.set",
        _params(device=reference, voltage=voltage, current_limit=current_limit, channel=channel),
    )
    settings.emitter.emit(payload, lambda: fmt.kv_panel(reference, payload))


async def _hold_lease(
    client: InstrumentClient, emitter: Emitter, lease_id: str, *, ttl_s: float, hold_s: float
) -> None:
    """Keep a sustained output alive for a bounded time, then let it go.

    An output lease belongs to the connection that took it, so the rail drops
    the moment ``fdctl`` exits.  Holding it is therefore an explicit, bounded,
    interruptible act — and it always ends with the output off, whether the
    hold ran out or the operator hit Ctrl-C.

    Nothing about safety depends on the release below succeeding: if this
    process dies mid-hold, the daemon sees the socket close and drives the
    device to safe state itself.  The explicit release only makes the ordinary
    case tidy and the timeline honest.
    """
    from fielddeck.common.timebase import monotonic_ns

    interval = max(0.5, ttl_s / _LEASE_RENEW_DIVISOR)
    deadline = monotonic_ns() + int(hold_s * 1e9)
    emitter.warn(
        f"holding the output for {hold_s:g}s; Ctrl-C drops it immediately, "
        "and it drops on its own when this command ends"
    )
    try:
        while True:
            remaining_s = (deadline - monotonic_ns()) / 1e9
            if remaining_s <= 0:
                break
            await asyncio.sleep(min(interval, remaining_s))
            if monotonic_ns() >= deadline:
                break
            await client.call("safety.lease_renew", {"lease_id": lease_id, "ttl_s": ttl_s})
            emitter.note(
                f"lease {lease_id} renewed, {(deadline - monotonic_ns()) / 1e9:.1f}s of hold left"
            )
    except (KeyboardInterrupt, asyncio.CancelledError):
        emitter.warn("interrupted: releasing the output")
    finally:
        with contextlib.suppress(FieldDeckError, asyncio.CancelledError):
            await client.call("safety.lease_release", {"lease_id": lease_id})


def _on_off(settings: Settings, raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"on", "true", "1", "enable", "enabled", "yes"}:
        return True
    if value in {"off", "false", "0", "disable", "disabled", "no"}:
        return False
    _usage_error(settings, f"expected on or off, got {raw!r}")


@psu_app.command("output")
def psu_output(
    ctx: typer.Context,
    reference: DeviceArg,
    state: Annotated[str, typer.Argument(metavar="on|off", help="Enable or disable the output.")],
    ttl: Annotated[
        float, typer.Option("--ttl", min=0.1, max=3600, help="Dead-man interval, seconds.")
    ] = 30.0,
    hold: Annotated[
        float,
        typer.Option("--hold", min=0.0, help="Keep the output on for this many seconds, renewing."),
    ] = 0.0,
    channel: Annotated[int | None, typer.Option("--channel", min=1, max=8)] = None,
    json_out: JsonFlag = False,
) -> None:
    """Enable or disable the output.

    Enabling needs POWER and takes an output lease. Disabling is always
    permitted, including while an emergency stop is latched, because turning an
    output off is never the dangerous direction.

    The lease belongs to this process: without --hold the rail comes back down
    as soon as the command exits, which is deliberate. Use --hold to keep it up
    for a bounded, interruptible window.
    """
    settings = _settings(ctx, json_out)
    enabled = _on_off(settings, state)

    async def work(client: InstrumentClient) -> dict[str, Any]:
        result = await client.execute(
            "psu.output",
            _params(device=reference, enabled=enabled, lease_ttl_s=ttl, channel=channel),
        )
        payload = result.result
        # Shown before the hold rather than after it, so an operator watching a
        # rail come up sees the reading now instead of when the hold ends.
        settings.emitter.show(fmt.kv_panel(f"{reference} output", payload))
        lease_id = (payload.get("lease") or {}).get("lease_id")
        if enabled and hold > 0 and lease_id:
            await _hold_lease(client, settings.emitter, str(lease_id), ttl_s=ttl, hold_s=hold)
            payload = {**payload, "held_for_s": hold, "lease_released": True}
        return payload

    payload = _call(settings, work)
    if settings.json_mode:
        settings.emitter.data(payload)
    elif enabled and hold <= 0 and payload.get("lease"):
        settings.emitter.warn(
            "the output lease is owned by this fdctl process, so instrumentd has already "
            "driven the rail back to safe state. Use --hold SECONDS to keep it energised."
        )


# ---------------------------------------------------------------------------
# Analysis toolbox (works without a daemon)
# ---------------------------------------------------------------------------

HexOpt = Annotated[str | None, typer.Option("--hex", help="Inline bytes as hex.")]
TextOpt = Annotated[str | None, typer.Option("--text", help="Inline bytes as UTF-8 text.")]
Base64Opt = Annotated[str | None, typer.Option("--base64", help="Inline bytes as base64.")]
PathOpt = Annotated[
    str | None,
    typer.Option("--path", help="Artifact inside the session store. Needs a running daemon."),
]
FileOpt = Annotated[
    str | None,
    typer.Option("--file", help="A local file. Read here, not by the daemon."),
]
SessionOpt = Annotated[str | None, typer.Option("--session", help="Session id for --path.")]
OffsetOpt = Annotated[int, typer.Option("--offset", min=0, help="Start this many bytes in.")]
MaxBytesOpt = Annotated[int, typer.Option("--max-bytes", min=1, help="Read at most this many.")]

_LocalLoader = Callable[[], tuple[bytes, dict[str, Any]]]


def _local_bytes(
    settings: Settings,
    *,
    hex_value: str | None,
    text: str | None,
    base64_value: str | None,
    file: str | None,
    offset: int,
    max_bytes: int,
) -> tuple[bytes, dict[str, Any]]:
    """Load bytes in this process, in the same shape the daemon reports them.

    ``--file`` reads the operator's own filesystem on purpose.  The daemon
    deliberately refuses paths outside the session store, because its analysis
    actions are reachable from recipes and from the assistant over the
    restricted socket.  ``fdctl`` is neither: it runs as the person who typed
    the path, with exactly their permissions, and nothing it reads here is sent
    anywhere.
    """
    from fielddeck.analysis import convert as tools
    from fielddeck.common.errors import CaptureError

    if file is not None:
        path = Path(file).expanduser()
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(max_bytes)
        except OSError as exc:
            raise CaptureError(
                f"cannot read {path}: {exc}",
                details={"path": str(path)},
                preserved="no bytes were read",
            ) from exc
        return data, {
            "kind": "file",
            "path": str(path),
            "size_bytes": size,
            "offset": offset,
            "bytes_read": len(data),
            "complete": offset == 0 and len(data) == size,
        }

    if hex_value is not None:
        raw = tools.parse_hex_bytes(hex_value)
    elif base64_value is not None:
        raw = tools.base64_decode(base64_value)
    else:
        raw = (text or "").encode("utf-8")
    data = tools.slice_bytes(raw, offset)[:max_bytes]
    return data, {
        "kind": "inline",
        "size_bytes": len(raw),
        "offset": offset,
        "bytes_read": len(data),
        "complete": offset == 0 and len(data) == len(raw),
    }


def _data_source(
    settings: Settings,
    *,
    hex_value: str | None,
    text: str | None,
    base64_value: str | None,
    path: str | None,
    file: str | None,
    session: str | None,
    offset: int,
    max_bytes: int,
) -> tuple[dict[str, Any] | None, _LocalLoader | None]:
    """Resolve one byte source into (daemon params, local loader).

    Either half may be None: a session artifact can only be read by the daemon
    that owns the session store, and a local file can only be read here.
    """
    given = {
        "--hex": hex_value,
        "--text": text,
        "--base64": base64_value,
        "--path": path,
        "--file": file,
    }
    chosen = [name for name, value in given.items() if value is not None]
    if len(chosen) != 1:
        _usage_error(
            settings,
            "give exactly one byte source",
            options=sorted(given),
            given=chosen,
        )

    def loader() -> tuple[bytes, dict[str, Any]]:
        return _local_bytes(
            settings,
            hex_value=hex_value,
            text=text,
            base64_value=base64_value,
            file=file,
            offset=offset,
            max_bytes=max_bytes,
        )

    if path is not None:
        return (
            _params(path=path, session_id=session, offset=offset, max_bytes=max_bytes),
            None,
        )
    if file is not None:
        return None, loader
    return (
        _params(hex=hex_value, text=text, base64=base64_value, offset=offset, max_bytes=max_bytes),
        loader,
    )


def _run_local(settings: Settings, local: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return local()
    except FieldDeckError as exc:
        _die(settings, exc)


def _analysis(
    settings: Settings,
    action: str,
    params: dict[str, Any] | None,
    local: Callable[[], dict[str, Any]] | None,
) -> dict[str, Any]:
    """Prefer the daemon; compute here when there is none.

    Preferring the daemon is not about permission — this is arithmetic over
    bytes and needs none — it is about evidence: work done through the daemon
    lands on the session timeline next to the capture it describes.
    """
    if params is None:
        assert local is not None
        return _run_local(settings, local)
    if local is None:
        return _execute(settings, action, params)
    try:
        return _attempt(settings, lambda client: client.execute(action, params)).result
    except TransportError:
        settings.emitter.note(
            "instrumentd is not reachable; computing locally. Nothing is recorded on a "
            "session timeline."
        )
        return _run_local(settings, local)
    except FieldDeckError as exc:
        _die(settings, exc)


@app.command("convert")
def convert_command(
    ctx: typer.Context,
    value: Annotated[str, typer.Argument(help="The value to read, e.g. 0xDEADBEEF.")],
    operation: Annotated[
        str, typer.Option("--op", help="interpret, unit, bitfield or timestamp.")
    ] = "interpret",
    from_unit: Annotated[str | None, typer.Option("--from", help="Source unit.")] = None,
    to_unit: Annotated[str | None, typer.Option("--to", help="Target unit.")] = None,
    bit_offset: Annotated[int, typer.Option("--bit-offset", min=0, max=63)] = 0,
    bit_count: Annotated[int, typer.Option("--bit-count", min=1, max=64)] = 1,
    total_width: Annotated[int | None, typer.Option("--total-width", min=1, max=64)] = None,
    epoch_unit: Annotated[str, typer.Option("--epoch-unit", help="s, ms, us or ns.")] = "s",
    json_out: JsonFlag = False,
) -> None:
    """Read one value every way it can plausibly be read. Works with no daemon."""
    settings = _settings(ctx, json_out)
    params = _params(
        value=value,
        operation=operation,
        from_unit=from_unit,
        to_unit=to_unit,
        bit_offset=bit_offset,
        bit_count=bit_count,
        total_width=total_width,
        epoch_unit=epoch_unit,
    )

    def local() -> dict[str, Any]:
        return _local_convert(
            settings,
            value=value,
            operation=operation,
            from_unit=from_unit,
            to_unit=to_unit,
            bit_offset=bit_offset,
            bit_count=bit_count,
            total_width=total_width,
            epoch_unit=epoch_unit,
        )

    payload = _analysis(settings, "tools.convert", params, local)
    settings.emitter.emit(payload)


def _local_convert(
    settings: Settings,
    *,
    value: str,
    operation: str,
    from_unit: str | None,
    to_unit: str | None,
    bit_offset: int,
    bit_count: int,
    total_width: int | None,
    epoch_unit: str,
) -> dict[str, Any]:
    """The daemon's ``tools.convert`` dispatch, run in this process.

    Kept deliberately shape-for-shape identical with the action handler: a
    script must not get a different document depending on whether instrumentd
    happened to be running.
    """
    from fielddeck.analysis import convert as tools

    if operation == "interpret":
        return tools.interpret(value)
    if operation == "unit":
        if not from_unit or not to_unit:
            raise InvalidRequest(
                "unit conversion needs --from and --to",
                details={"units": tools.list_units()},
            )
        try:
            number = float(value)
        except ValueError:
            number = float(tools.parse_number(value))
        return tools.convert_unit(number, from_unit, to_unit)
    if operation == "bitfield":
        source = tools.parse_number(value)
        extracted = tools.bitfield(source, bit_offset, bit_count, total_width=total_width)
        return {
            "input": value,
            "value": source,
            "bit_offset": bit_offset,
            "bit_count": bit_count,
            "extracted": extracted,
            "hex": tools.to_base(extracted, 16),
            "binary": tools.to_base(extracted, 2, width=bit_count),
            "source_binary": tools.to_base(source, 2, width=total_width),
        }
    if operation != "timestamp":
        _usage_error(
            settings,
            f"unknown operation {operation!r}",
            known=["interpret", "unit", "bitfield", "timestamp"],
        )
    text = value.strip()
    try:
        epoch = tools.parse_number(text)
    except InvalidRequest:
        return {
            "input": text,
            "direction": "iso -> epoch",
            "unit": epoch_unit,
            "value": tools.iso_to_epoch(text, unit=epoch_unit),
            "all_units": {unit: tools.iso_to_epoch(text, unit=unit) for unit in tools.EPOCH_UNITS},
        }
    return {
        "input": text,
        "direction": "epoch -> iso",
        "unit": epoch_unit,
        "utc": tools.epoch_to_iso(epoch, unit=epoch_unit),
        "all_units": {unit: tools.epoch_to_iso(epoch, unit=unit) for unit in tools.EPOCH_UNITS},
        "plausible_units": tools.guess_epoch_units(epoch),
    }


@app.command("crc")
def crc_command(
    ctx: typer.Context,
    model: Annotated[
        str | None, typer.Argument(help="CRC name; omit for the whole catalogue.")
    ] = None,
    hex_value: HexOpt = None,
    text: TextOpt = None,
    base64_value: Base64Opt = None,
    path: PathOpt = None,
    file: FileOpt = None,
    session: SessionOpt = None,
    offset: OffsetOpt = 0,
    max_bytes: MaxBytesOpt = 4 * 1024 * 1024,
    expected: Annotated[
        str | None,
        typer.Option("--expected", help="Trailer bytes; reports which models produce them."),
    ] = None,
    catalogue: Annotated[
        bool, typer.Option("--list", help="Print the CRC catalogue and exit.")
    ] = False,
    json_out: JsonFlag = False,
) -> None:
    """Compute CRCs, or work out which CRC produced a trailer. Works with no daemon."""
    settings = _settings(ctx, json_out)
    if catalogue:

        def models() -> dict[str, Any]:
            from fielddeck.analysis import crc as tools

            listed = tools.list_models()
            return {
                "models": listed,
                "count": len(listed),
                "note": "check is the CRC of b'123456789', verified by the test suite",
            }

        payload = _analysis(settings, "tools.crc_list", {}, models)
        settings.emitter.emit(
            payload, lambda: fmt.rows_table("crc catalogue", payload["models"], limit=64)
        )
        return

    params, loader = _data_source(
        settings,
        hex_value=hex_value,
        text=text,
        base64_value=base64_value,
        path=path,
        file=file,
        session=session,
        offset=offset,
        max_bytes=max_bytes,
    )
    if params is not None:
        params = {**params, **_params(model=model, expected=expected)}

    def local() -> dict[str, Any]:
        assert loader is not None
        return _local_crc(loader(), model=model, expected=expected)

    payload = _analysis(settings, "tools.crc", params, local if loader else None)
    settings.emitter.emit(payload)


def _local_crc(
    loaded: tuple[bytes, dict[str, Any]], *, model: str | None, expected: str | None
) -> dict[str, Any]:
    """``tools.crc`` computed in this process, in the same result shape."""
    from fielddeck.analysis import convert as convert_tools
    from fielddeck.analysis import crc as tools

    data, source = loaded
    result: dict[str, Any] = {"source": source, "bytes": len(data)}
    if expected is not None:
        trailer = convert_tools.parse_hex_bytes(expected)
        matches = tools.crc_candidates(data, trailer)
        result["expected"] = trailer.hex().upper()
        result["matches"] = matches
        result["match_count"] = len(matches)
        result["note"] = (
            "several models can produce the same short trailer; a match here is evidence, not proof"
            if len(matches) > 1
            else "no catalogue model produces that trailer over these bytes"
            if not matches
            else "one catalogue model produces that trailer"
        )
        return result
    if model is not None:
        found = tools.get_model(model)
        value = found.compute(data)
        result["model"] = found.name
        result["value"] = value
        result["hex"] = f"0x{value:0{found.byte_width * 2}X}"
        result["big_endian"] = found.to_bytes(value, byteorder="big").hex().upper()
        result["little_endian"] = found.to_bytes(value, byteorder="little").hex().upper()
        return result
    result["values"] = {
        name: f"0x{entry.compute(data):0{entry.byte_width * 2}X}"
        for name, entry in tools.CATALOGUE.items()
    }
    return result


@app.command("hash")
def hash_command(
    ctx: typer.Context,
    hex_value: HexOpt = None,
    text: TextOpt = None,
    base64_value: Base64Opt = None,
    path: PathOpt = None,
    file: FileOpt = None,
    session: SessionOpt = None,
    offset: OffsetOpt = 0,
    max_bytes: MaxBytesOpt = 64 * 1024 * 1024,
    json_out: JsonFlag = False,
) -> None:
    """SHA-256, MD5 and CRC-32 over bytes or a file. Works with no daemon.

    The result says which byte range the digest covers: a partial hash is
    labelled as one, because a digest that is not the file's digest is worse
    than no digest at all.
    """
    settings = _settings(ctx, json_out)
    params, loader = _data_source(
        settings,
        hex_value=hex_value,
        text=text,
        base64_value=base64_value,
        path=path,
        file=file,
        session=session,
        offset=offset,
        max_bytes=max_bytes,
    )

    def local() -> dict[str, Any]:
        from fielddeck.analysis import convert as tools

        assert loader is not None
        data, source = loader()
        covers = (
            "the whole file"
            if source.get("complete")
            else f"bytes {source['offset']}..{source['offset'] + source['bytes_read']} "
            f"of {source['size_bytes']}"
        )
        return {**tools.hash_bytes(data), "source": source, "covers": covers}

    payload = _analysis(settings, "tools.hash", params, local if loader else None)
    settings.emitter.emit(payload)


@app.command("analyze")
def analyze_command(
    ctx: typer.Context,
    hex_value: HexOpt = None,
    text: TextOpt = None,
    base64_value: Base64Opt = None,
    path: PathOpt = None,
    file: FileOpt = None,
    session: SessionOpt = None,
    offset: OffsetOpt = 0,
    max_bytes: MaxBytesOpt = 4 * 1024 * 1024,
    framing_only: Annotated[
        bool, typer.Option("--framing", help="Raw framing report instead of hypotheses.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, max=12)] = 6,
    json_out: JsonFlag = False,
) -> None:
    """What is this stream? Evidence-based hypotheses. Works with no daemon.

    Passive analysis of bytes that were already captured. Nothing is
    transmitted, and the recommended next test is printed with the permission
    it would need rather than being run.
    """
    settings = _settings(ctx, json_out)
    params, loader = _data_source(
        settings,
        hex_value=hex_value,
        text=text,
        base64_value=base64_value,
        path=path,
        file=file,
        session=session,
        offset=offset,
        max_bytes=max_bytes,
    )
    action = "tools.analyze_bytes" if framing_only else "tools.identify_protocol"
    if params is not None and not framing_only:
        params = {**params, "limit": limit}

    def local() -> dict[str, Any]:
        from fielddeck.analysis import autodetect, framing

        assert loader is not None
        data, source = loader()
        if framing_only:
            report = framing.analyze(data)
            return {**report, "source": source, "timestamps_used": 0}
        found = autodetect.identify(data, limit=limit)
        return {**found, "source": source, "timestamps_used": 0}

    payload = _analysis(settings, action, params, local if loader else None)
    if settings.json_mode or framing_only or not payload.get("rendered"):
        settings.emitter.emit(payload)
        return
    print(payload["rendered"])


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


def _recipe_ref(settings: Settings, recipe: str | None, text_file: str | None) -> dict[str, Any]:
    if bool(recipe) == bool(text_file):
        _usage_error(settings, "give either a recipe name/path or --file")
    if text_file:
        path = Path(text_file).expanduser()
        try:
            return {"text": path.read_text(encoding="utf-8")}
        except OSError as exc:
            _usage_error(settings, f"cannot read {path}: {exc}")
    return {"recipe": recipe}


@recipe_app.command("list")
def recipe_list(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 100,
    json_out: JsonFlag = False,
) -> None:
    """Recipes on this unit, and the worst thing each one would need."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "recipe.list", {"limit": limit}, timeout_s=60.0)
    settings.emitter.emit(
        payload,
        lambda: fmt.rows_table(
            "recipes",
            payload.get("recipes", []),
            columns=["name", "max_permission", "steps", "requires", "ok", "problems"],
            limit=limit,
        ),
    )


@recipe_app.command("validate")
def recipe_validate(
    ctx: typer.Context,
    recipe: Annotated[str | None, typer.Argument(help="Recipe name or path.")] = None,
    text_file: Annotated[
        str | None, typer.Option("--file", help="Read YAML from a local file.")
    ] = None,
    json_out: JsonFlag = False,
) -> None:
    """Compile a recipe and report devices, permissions and limit problems."""
    settings = _settings(ctx, json_out)
    payload = _execute(
        settings, "recipe.validate", _recipe_ref(settings, recipe, text_file), timeout_s=60.0
    )
    settings.emitter.emit(payload, lambda: fmt.result_view(payload.get("summary") or payload))


@recipe_app.command("dry-run")
def recipe_dry_run(
    ctx: typer.Context,
    recipe: Annotated[str | None, typer.Argument(help="Recipe name or path.")] = None,
    text_file: Annotated[str | None, typer.Option("--file")] = None,
    json_out: JsonFlag = False,
) -> None:
    """Would this run right now? Preflight only; no step is executed.

    A missing grant is reported rather than raised: what you need is precisely
    the question a dry run answers.
    """
    settings = _settings(ctx, json_out)
    payload = _execute(
        settings, "recipe.dry_run", _recipe_ref(settings, recipe, text_file), timeout_s=120.0
    )
    settings.emitter.emit(payload, lambda: fmt.result_view(payload.get("run") or payload))


@recipe_app.command("run")
def recipe_run(
    ctx: typer.Context,
    recipe: Annotated[str | None, typer.Argument(help="Recipe name or path.")] = None,
    text_file: Annotated[str | None, typer.Option("--file")] = None,
    open_session: Annotated[
        bool, typer.Option("--session/--no-session", help="Open a session if none is active.")
    ] = True,
    deadline: Annotated[
        float | None, typer.Option("--deadline", min=0.1, max=3600, help="Wall-clock budget.")
    ] = None,
    yes: YesFlag = False,
    json_out: JsonFlag = False,
) -> None:
    """Execute a recipe. It needs whatever its own steps need, and no more.

    The permission asked for is the worst thing this particular recipe reaches,
    resolved by compiling it — so a listening recipe asks for nothing and one
    that energises a rail asks for the same POWER its psu step will use.
    """
    settings = _settings(ctx, json_out, yes=yes)
    reference = _recipe_ref(settings, recipe, text_file)
    plan = _execute(settings, "recipe.validate", reference, timeout_s=60.0)
    max_permission = str((plan.get("plan") or {}).get("max_permission", "PASSIVE"))
    settings.emitter.show(fmt.result_view(plan.get("summary") or {}, title="plan"))
    if _permission(settings, max_permission) in _CONFIRMED_PERMISSIONS:
        _require_confirmation(
            settings,
            word=max_permission,
            title=f"this recipe reaches {max_permission}",
            lines=[
                f"recipe: {recipe or text_file}",
                f"steps: {len((plan.get('plan') or {}).get('steps') or [])}",
                "Confirm the target is the one you mean to modify.",
            ],
        )
    payload = _execute(
        settings,
        "recipe.run",
        {**reference, **_params(open_session=open_session, deadline_s=deadline)},
        timeout_s=deadline + 60.0 if deadline else 3600.0,
    )
    settings.emitter.emit(payload)
    if payload.get("failed_assertions") or payload.get("outcome") in {"failed", "FAILED"}:
        raise typer.Exit(EXIT_CODES["assertion_failed"])


@recipe_app.command("cancel")
def recipe_cancel(
    ctx: typer.Context,
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="Omit to signal every run.")
    ] = None,
    reason: Annotated[str, typer.Option("--reason")] = "cancelled by operator",
    json_out: JsonFlag = False,
) -> None:
    """Ask a running recipe to stop. Its cleanup steps still run."""
    settings = _settings(ctx, json_out)
    payload = _execute(settings, "recipe.cancel", _params(run_id=run_id, reason=reason))
    settings.emitter.emit(payload)


# ---------------------------------------------------------------------------
# Live views
# ---------------------------------------------------------------------------


@app.command("events")
def events_command(
    ctx: typer.Context,
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Stream until interrupted.")
    ] = False,
    types: Annotated[
        list[str] | None, typer.Option("--type", help="Event type to include; repeatable.")
    ] = None,
    session: Annotated[str | None, typer.Option("--session", help="Only this session.")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
    json_out: JsonFlag = False,
) -> None:
    """The live event stream, or the most recent events.

    With --follow and --json the output is one JSON document per line: a stream
    has no end, and a consumer should be able to act on each record as it
    arrives rather than waiting for a closing bracket.
    """
    settings = _settings(ctx, json_out)
    wanted = list(types) if types else None
    if not follow:
        payload = _call(
            settings,
            lambda client: client.call("events.recent", _params(limit=limit, types=wanted)),
        )
        settings.emitter.emit(payload, lambda: fmt.event_table(payload.get("events", [])))
        return

    async def stream(client: InstrumentClient) -> None:
        async for event in client.subscribe(types=wanted, session_id=session):
            record = event.model_dump(mode="json")
            if settings.json_mode:
                settings.emitter.stream(record)
            else:
                settings.emitter.show(fmt.event_line(record))

    try:
        _call(settings, stream)
    except KeyboardInterrupt:
        raise typer.Exit(0) from None


@app.command("watch")
def watch_command(
    ctx: typer.Context,
    interval: Annotated[
        float, typer.Option("--interval", min=0.1, max=60, help="Seconds between polls.")
    ] = 1.0,
    json_out: JsonFlag = False,
) -> None:
    """A compact live status line, for a second terminal.

    Deliberately one line: this is meant to sit in a tmux pane next to the work
    and answer "am I armed, and is anything energised?" at a glance.
    """
    settings = _settings(ctx, json_out)
    self_request = f"fdctl-watch-{secrets.token_hex(4)}"

    async def poll(client: InstrumentClient) -> None:
        with fmt.live_status(settings.emitter) as update:
            while True:
                # Same reason as ``fdctl status``: a watch line that always says
                # "running system.status" is a watch line nobody reads.
                overview = (await client.execute("system.status", request_id=self_request)).result
                overview["running_actions"] = [
                    item
                    for item in overview.get("running_actions") or []
                    if item.get("request_id") != self_request
                ]
                update(overview)
                await asyncio.sleep(interval)

    try:
        _call(settings, poll)
    except KeyboardInterrupt:
        raise typer.Exit(0) from None


def main(argv: list[str] | None = None) -> int:
    """Console entry point.

    Typer exits the process itself with the code raised by ``typer.Exit``, so
    the return value below is only reached if that ever stops being true.
    """
    app(args=argv)
    return 0


if __name__ == "__main__":  # pragma: no cover - module execution
    raise SystemExit(main())
