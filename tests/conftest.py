"""Shared fixtures for the FieldDeck test suite.

The expensive fixture here is :func:`daemon`: a real ``instrumentd`` running
in-process, in simulation, with its Unix socket in a private temporary
directory.  It is a *real* daemon — real RPC server, real dispatcher, real
safety manager, real session store — because the safety tests are only worth
anything if they exercise the same pipeline production does.  Nothing is
stubbed out except the hardware, which is what simulation mode is for.  There
is no test-only shortcut around authorization; if a test can energise the
simulated supply without arming POWER first, that is a bug worth failing over.

Three things are easy to get wrong and are handled once, here:

* **Environment leakage.**  ``fielddeck.common.paths`` reads a handful of
  ``FIELDDECK_*`` variables, and ``socket_path()`` will happily find a
  developer's real daemon if they are left set.  Every test runs with those
  cleared and ``FIELDDECK_HOME`` pointed at a fresh temporary directory.
* **Socket path length.**  ``AF_UNIX`` paths are capped at 108 bytes by the
  kernel, and pytest's ``tmp_path`` (which embeds the test's name) can blow
  through that.  The daemon home is created with a short prefix in the system
  temporary directory instead, and the length is asserted rather than left to
  produce a baffling ``OSError`` at bind time.
* **Teardown.**  A daemon that is not stopped leaves a safety task, two
  listening sockets and an open SQLite handle behind, and the next test in the
  same session inherits them.  Every daemon and client this module hands out
  is registered for shutdown, in reverse order of creation.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from fielddeck.capture.sessions import SessionManager
from fielddeck.common.config import FieldDeckConfig, SafetyConfig
from fielddeck.common.models import ArmScope, ClientSource, PermissionLevel, SafetyLimit
from fielddeck.common.paths import Paths
from fielddeck.daemon.client import InstrumentClient
from fielddeck.daemon.events import EventBus
from fielddeck.daemon.service import InstrumentDaemon
from fielddeck.safety.manager import SafetyManager

#: Environment variables that would otherwise let a developer's real
#: installation reach into a test run.
_FIELDDECK_ENV = (
    "FIELDDECK_HOME",
    "FIELDDECK_CONFIG_DIR",
    "FIELDDECK_STATE_DIR",
    "FIELDDECK_RUNTIME_DIR",
    "FIELDDECK_SESSIONS_DIR",
    "FIELDDECK_LOG_DIR",
    "FIELDDECK_SOCKET",
    "FIELDDECK_RECIPES_DIR",
    "FIELDDECK_SIM",
    "FIELDDECK_SCENARIO",
    "FIELDDECK_AI_GROUP",
    "FIELDDECK_VISA_DISCOVERY",
)

#: Comfortably inside the kernel's 108-byte ``sun_path``, with room for the
#: longest name the daemon appends (``instrumentd-ai.sock``).
_MAX_SOCKET_PATH = 100


# ---------------------------------------------------------------------------
# Paths and environment
# ---------------------------------------------------------------------------


@pytest.fixture
def fielddeck_home() -> Iterator[Path]:
    """A private FIELDDECK_HOME whose socket path fits in ``sun_path``."""
    home = Path(tempfile.mkdtemp(prefix="fd-"))
    socket = home / "run" / "instrumentd-ai.sock"
    assert len(str(socket)) <= _MAX_SOCKET_PATH, (
        f"temporary socket path {socket} is {len(str(socket))} bytes; AF_UNIX allows 108. "
        "Set TMPDIR to something shorter."
    )
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, fielddeck_home: Path) -> None:
    """Point every path lookup at the temporary home, for every test.

    Autouse: isolation is not something an individual test should have to
    remember, and forgetting it means writing into a real session store.
    """
    for name in _FIELDDECK_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FIELDDECK_HOME", str(fielddeck_home))


@pytest.fixture
def paths(fielddeck_home: Path) -> Paths:
    """The layout the daemon under test uses.  Not created until asked for."""
    return Paths(
        home=fielddeck_home,
        config_dir=fielddeck_home / "config",
        state_dir=fielddeck_home / "state",
        runtime_dir=fielddeck_home / "run",
        sessions_dir=fielddeck_home / "state" / "sessions",
        log_dir=fielddeck_home / "state" / "logs",
    )


@pytest.fixture
def fixtures_dir() -> Path:
    """Static test data: recipes and configuration files checked into the tree."""
    return Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


@pytest.fixture
def safety_config() -> SafetyConfig:
    """The built-in conservative policy, which is what a fresh unit ships with."""
    return SafetyConfig.defaults()


@pytest.fixture
def strict_safety_config() -> SafetyConfig:
    """Defaults, with a power ceiling low enough that V x I can actually bite.

    The shipped default is ``psu.power`` 90 W against ``psu.voltage`` 30 V and
    ``psu.current`` 3 A — exactly their product — so no combination of legal
    setpoints can exceed it.  A derived-limit test has to tighten one bound to
    be testing anything, and a real bench would tighten it for the same reason.
    """
    config = SafetyConfig.defaults()
    limits = dict(config.global_limits)
    limits["psu.power"] = SafetyLimit(quantity="psu.power", minimum=0.0, maximum=6.0, unit="W")
    return config.model_copy(update={"global_limits": limits})


@pytest.fixture
def safety(safety_config: SafetyConfig) -> SafetyManager:
    """A standalone safety manager with no daemon attached."""
    return SafetyManager(safety_config)


@pytest.fixture
def armed_safety(safety: SafetyManager) -> SafetyManager:
    """A manager with every grantable class armed.

    For tests about something *other* than authorization.  Never use it in a
    test whose subject is whether a permission is enforced.
    """
    for permission in PermissionLevel:
        if permission.requires_grant:
            safety.arm(permission=permission, ttl_s=300, source=ClientSource.FDCTL)
    return safety


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def sessions(paths: Paths) -> Iterator[SessionManager]:
    """A session manager on the temporary store, with no free-space floor.

    The floor is disabled because CI runners are routinely low on disk and a
    test that fails because of the *runner's* free space is a test that gets
    ignored.  The floor itself is covered explicitly in the session tests.
    """
    paths.ensure()
    manager = SessionManager(paths.sessions_dir, publish=None, min_free_mb=0, simulated=True)
    try:
        yield manager
    finally:
        manager.shutdown()


# ---------------------------------------------------------------------------
# Daemon and clients
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> FieldDeckConfig:
    """Simulated bench, no operator-supplied configuration file involved."""
    config = FieldDeckConfig.defaults()
    config.simulate = True
    # The free-space floor is a real safety feature, but a CI container with a
    # small overlay would refuse to open a session and every capture test would
    # fail for a reason unrelated to what it tests.  The floor itself is
    # covered directly in the session tests.
    config.storage.min_free_mb = 0
    return config


DaemonFactory = Callable[..., Awaitable[InstrumentDaemon]]


@pytest.fixture
async def daemon_factory(
    paths: Paths, config: FieldDeckConfig, safety_config: SafetyConfig
) -> AsyncIterator[DaemonFactory]:
    """Build and start daemons, and make sure every one of them is stopped.

    Tests that need a second daemon (restart semantics) or a different safety
    policy call this instead of the plain :func:`daemon` fixture.
    """
    started: list[InstrumentDaemon] = []

    async def factory(
        *,
        safety_config: SafetyConfig = safety_config,
        config: FieldDeckConfig = config,
        paths: Paths = paths,
        enable_restricted_socket: bool = True,
    ) -> InstrumentDaemon:
        daemon = InstrumentDaemon(
            paths=paths,
            config=config,
            safety_config=safety_config,
            enable_restricted_socket=enable_restricted_socket,
        )
        await daemon.start()
        started.append(daemon)
        return daemon

    try:
        yield factory
    finally:
        for daemon in reversed(started):
            await daemon.stop()


@pytest.fixture
async def daemon(daemon_factory: DaemonFactory) -> InstrumentDaemon:
    """A running simulated instrumentd.  Boots SAFE, as the real one does."""
    return await daemon_factory()


ClientFactory = Callable[..., Awaitable[InstrumentClient]]


@pytest.fixture
async def client_factory(daemon: InstrumentDaemon) -> AsyncIterator[ClientFactory]:
    """Extra connections, e.g. to prove what happens when one goes away."""
    clients: list[InstrumentClient] = []

    async def factory(
        *,
        source: ClientSource = ClientSource.FDCTL,
        socket_path: Path | None = None,
        timeout_s: float = 10.0,
    ) -> InstrumentClient:
        client = InstrumentClient(
            socket_path or daemon.socket_path, source=source, timeout_s=timeout_s
        )
        await client.connect()
        clients.append(client)
        return client

    try:
        yield factory
    finally:
        for client in reversed(clients):
            await client.close()


@pytest.fixture
async def client(client_factory: ClientFactory) -> InstrumentClient:
    """A connected fdctl-equivalent client on the full control socket."""
    return await client_factory()


@pytest.fixture
def restricted_socket(daemon: InstrumentDaemon) -> Path:
    """The AI-facing socket the daemon opened alongside the control socket."""
    path = daemon.ai_socket_path
    assert path is not None, "the daemon under test was built without a restricted socket"
    return path


@pytest.fixture
async def ai_client(
    client_factory: ClientFactory, restricted_socket: Path
) -> InstrumentClient:
    """A client on the restricted socket, as the MCP server would connect.

    It declares ``source=claude``, but the socket forces that identity anyway —
    which is the property the restricted-socket tests are about.
    """
    return await client_factory(source=ClientSource.CLAUDE, socket_path=restricted_socket)


# ---------------------------------------------------------------------------
# Helpers exposed as fixtures
#
# These are fixtures rather than an importable module so that test files never
# have to care about sys.path; pytest hands them over by name.
# ---------------------------------------------------------------------------

WaitFor = Callable[..., Awaitable[Any]]


@pytest.fixture
def wait_for() -> WaitFor:
    """Poll a predicate until it holds.

    Used instead of a fixed sleep wherever the daemon does something on its own
    timer (lease sweeps, disconnect handling): the test then takes as long as
    the daemon actually takes, and fails with what the value was rather than
    with a bare timeout.
    """

    async def _wait_for(
        predicate: Callable[[], Any],
        *,
        timeout_s: float = 5.0,
        interval_s: float = 0.02,
        message: str = "condition was never met",
    ) -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        last: Any = None
        while True:
            last = predicate()
            if inspect.isawaitable(last):
                last = await last
            if last:
                return last
            if loop.time() >= deadline:
                raise AssertionError(f"{message} (waited {timeout_s:g}s, last value {last!r})")
            await asyncio.sleep(interval_s)

    return _wait_for


Arm = Callable[..., Awaitable[dict[str, Any]]]


@pytest.fixture
def arm(client: InstrumentClient) -> Arm:
    """Create an arm grant the way a human at fdctl would."""

    async def _arm(
        permission: PermissionLevel | str,
        *,
        ttl_s: float = 60.0,
        scope: ArmScope | None = None,
        using: InstrumentClient | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        reply = await (using or client).call(
            "safety.arm",
            {
                "permission": str(permission),
                "ttl_s": ttl_s,
                "scope": scope.model_dump(mode="json") if scope is not None else None,
                "note": note,
            },
        )
        return dict(reply["grant"])

    return _arm


def pytest_configure(config: pytest.Config) -> None:
    # Nothing in the suite may reach real hardware, and the simulated drivers
    # must be selected by the fixtures rather than by whatever the developer
    # happened to export before running pytest.
    os.environ.pop("FIELDDECK_SIM", None)
