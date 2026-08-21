"""Fixtures for the integration suite: one real ``instrumentd`` per test.

Nothing here fakes a driver, stubs the dispatcher or reaches past the safety
manager.  Every test in this directory talks to a genuine
:class:`~fielddeck.daemon.service.InstrumentDaemon` running in this process,
over a genuine Unix socket, with the simulated bench in place of hardware —
which is the whole point of simulation mode: it exercises the production code
path rather than a parallel fake.

Three things here are load-bearing and will bite whoever changes them.

**The socket lives in a short private directory.**  ``AF_UNIX`` paths are
capped at 108 bytes and pytest's ``tmp_path`` embeds the test's name, so a
long test name plus a deep temp root silently becomes "cannot bind".  Each
daemon therefore gets ``/tmp/fd-it-XXXXXXXX`` from :func:`tempfile.mkdtemp`.

**Every daemon is torn down.**  ``InstrumentDaemon.stop`` drives every device
to safe state, closes the sockets and finalises the session; skipping it
leaves a background safety task running into the next test and a stale socket
on disk.

**Events are collected through a bus sink, not a subscription.**  Sinks are
lossless by contract, so an assertion about *whether* something was emitted
cannot fail because a queue was full.  The tests that are specifically about
backpressure subscribe instead, deliberately.

These fixtures are duplicated in ``tests/ui/conftest.py`` on purpose: the two
directories are owned separately and a shared root ``conftest.py`` would
couple them.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from fielddeck.common.config import FieldDeckConfig, SafetyConfig
from fielddeck.common.events import Event, EventType
from fielddeck.common.models import ClientSource, PermissionLevel
from fielddeck.common.paths import Paths
from fielddeck.daemon.client import InstrumentClient
from fielddeck.daemon.service import InstrumentDaemon

#: Long enough that a loaded CI box does not fail a correct assertion, short
#: enough that a genuinely broken one fails the run instead of hanging it.
WAIT_TIMEOUT_S = 10.0


@pytest.fixture
def paths() -> Iterator[Paths]:
    """A private FieldDeck layout with a socket path short enough to bind."""
    root = Path(tempfile.mkdtemp(prefix="fd-it-"))
    state = root / "state"
    yield Paths(
        home=root,
        config_dir=root / "config",
        state_dir=state,
        runtime_dir=root / "run",
        sessions_dir=state / "sessions",
        log_dir=state / "logs",
    )
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def sim_config() -> FieldDeckConfig:
    """Defaults, simulated, with the free-space floor out of the way.

    The floor is a real safety feature, but a CI container with a small
    overlay would refuse to open a session and every test would fail for a
    reason that has nothing to do with what it is testing.
    """
    config = FieldDeckConfig.defaults()
    config.simulate = True
    config.storage.min_free_mb = 0
    return config


@pytest.fixture
def safety_config() -> SafetyConfig:
    return SafetyConfig.defaults()


@pytest.fixture
async def daemon(
    paths: Paths, sim_config: FieldDeckConfig, safety_config: SafetyConfig
) -> AsyncIterator[InstrumentDaemon]:
    """A started daemon with the simulated bench discovered and safed."""
    service = InstrumentDaemon(
        paths=paths,
        config=sim_config,
        safety_config=safety_config,
        socket_path=paths.socket,
    )
    await service.start()
    try:
        yield service
    finally:
        await service.stop()


@pytest.fixture
async def client(daemon: InstrumentDaemon) -> AsyncIterator[InstrumentClient]:
    """An ``fdctl``-class client: allowed to arm, like a human at the CLI."""
    async with InstrumentClient(
        daemon.socket_path, source=ClientSource.FDCTL, timeout_s=20.0
    ) as connected:
        yield connected


@pytest.fixture
def connect(daemon: InstrumentDaemon) -> Callable[..., InstrumentClient]:
    """Build extra clients, for tests about what a *second* connection sees."""

    def factory(source: ClientSource = ClientSource.FDCTL, timeout_s: float = 20.0) -> Any:
        return InstrumentClient(daemon.socket_path, source=source, timeout_s=timeout_s)

    return factory


class EventLog:
    """A lossless record of everything the daemon published during one test."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [str(event.type) for event in self.events]

    async def wait_for(
        self,
        event_type: EventType,
        *,
        match: Callable[[Event], bool] | None = None,
        timeout_s: float = WAIT_TIMEOUT_S,
    ) -> Event:
        """Wait for one event, polling the sink rather than racing it."""

        def found() -> Event | None:
            for event in self.events:
                if event.type is event_type and (match is None or match(event)):
                    return event
            return None

        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            hit = found()
            if hit is not None:
                return hit
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(
                    f"no {event_type} event within {timeout_s:g}s; saw {sorted(set(self.types()))}"
                )
            await asyncio.sleep(0.02)


@pytest.fixture
def events(daemon: InstrumentDaemon) -> Iterator[EventLog]:
    """Every event the daemon publishes, from before the test's first action."""
    log = EventLog()
    remove = daemon.bus.add_sink(log)
    try:
        yield log
    finally:
        remove()


@pytest.fixture
async def session(client: InstrumentClient) -> AsyncIterator[str]:
    """An open recording session, closed again even if the test explodes."""
    result = await client.execute("session.start", {"name": "integration"})
    session_id = str(result.result["session"]["id"])
    try:
        yield session_id
    finally:
        await client.try_execute("session.stop")


async def arm(
    client: InstrumentClient,
    *permissions: PermissionLevel,
    ttl_s: float = 120.0,
    device_id: str | None = None,
) -> list[str]:
    """Authorize permission classes the way a human at the CLI would.

    Exact-class, one grant each: arming POWER here does not authorize a QUERY
    action, and a test that needs both has to say both — the same rule an
    operator lives with.
    """
    grants: list[str] = []
    for permission in permissions:
        payload: dict[str, Any] = {"permission": str(permission), "ttl_s": ttl_s}
        if device_id is not None:
            payload["scope"] = {"kind": "device", "device_id": device_id}
        reply = await client.call("safety.arm", payload)
        grants.append(str(reply["grant"]["grant_id"]))
    return grants


async def wait_until(
    predicate: Callable[[], bool], *, timeout_s: float = WAIT_TIMEOUT_S, what: str = "condition"
) -> None:
    """Poll until ``predicate`` holds.  Never a bare sleep as synchronisation."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"{what} did not become true within {timeout_s:g}s")
        await asyncio.sleep(0.02)


#: Device ids the simulated bench always provides.  Named rather than
#: discovered by index so a failure says which device went missing.
SIM_CAN = "sim:can:can0"
SIM_SERIAL = "sim:serial:sim-uart-0"
SIM_PSU = "sim:visa:sim-psu-0"
SIM_DMM = "sim:visa:sim-dmm-0"
