"""Fixtures for the HMI suite: a real panel driven by Textual's test pilot.

The panel is the thing an operator stares at while something is going wrong,
so these tests run the real :class:`~fielddeck.ui.app.FieldDeckApp` against a
real ``instrumentd`` — no stubbed state object, no fake device list.  What
the assertions look at is the *rendered* 80x25 screen wherever possible,
because "the operator can see that nothing is armed" is a statement about
pixels, not about an attribute.

Two things to know before changing anything here.

**Rendered text comes from the compositor.**  Textual has no public "give me
the screen as text" API, so :meth:`Panel.lines` reaches for
``screen._compositor.render_strips()``.  That is the single private access in
this suite and it is deliberate: asserting on widget attributes instead would
pass happily while the layout pushed the safety chrome off the screen.

**Nothing here sleeps as a synchronisation primitive.**  ``Panel.settle``
pumps the message loop and polls a predicate, because the panel repaints on a
10 Hz timer and a fixed sleep would be either flaky or slow.

The daemon fixtures below are duplicated from ``tests/integration/conftest.py``
on purpose: the two directories are owned separately, and a shared root
``conftest.py`` would couple them.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from textual.app import ScreenStackError

from fielddeck.common.config import FieldDeckConfig, SafetyConfig
from fielddeck.common.paths import Paths
from fielddeck.daemon.service import InstrumentDaemon
from fielddeck.ui.app import FieldDeckApp

#: The panel's one and only target: a 480x320 panel at this character size.
PANEL_SIZE = (80, 25)

SETTLE_TIMEOUT_S = 15.0


@pytest.fixture
def paths() -> Iterator[Paths]:
    """A private layout whose socket path is short enough for AF_UNIX."""
    root = Path(tempfile.mkdtemp(prefix="fd-ui-"))
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
async def daemon(paths: Paths) -> AsyncIterator[InstrumentDaemon]:
    config = FieldDeckConfig.defaults()
    config.simulate = True
    config.storage.min_free_mb = 0
    service = InstrumentDaemon(
        paths=paths,
        config=config,
        safety_config=SafetyConfig.defaults(),
        socket_path=paths.socket,
    )
    await service.start()
    try:
        yield service
    finally:
        await service.stop()


@dataclass(slots=True)
class Panel:
    """One running panel, plus the few things a test needs to ask it."""

    app: FieldDeckApp
    pilot: Any

    # -- what the operator can see ----------------------------------------

    def lines(self) -> list[str]:
        """The rendered screen, one string per row."""
        return [strip.text for strip in self.app.screen._compositor.render_strips()]

    def screen_text(self) -> str:
        return "\n".join(self.lines())

    @property
    def chrome(self) -> str:
        """Row 0: safety word, recording, session, device count, link."""
        return self.lines()[0]

    @property
    def alert(self) -> str:
        """Row 1: the priority ladder — ESTOP, link, countdowns, or SAFE."""
        return self.lines()[1]

    @property
    def navigation(self) -> str:
        """The row carrying the five permanent tile labels."""
        return self.lines()[-2]

    def shows(self, text: str) -> bool:
        return text in self.screen_text()

    # -- where the operator is --------------------------------------------

    @property
    def screen_name(self) -> str | None:
        return getattr(self.app.screen, "screen_name", None)

    def widget(self, selector: str) -> Any:
        """A widget on the *active* screen, not the app's default one."""
        return self.app.screen.query_one(selector)

    # -- driving it --------------------------------------------------------

    async def press(self, *keys: str) -> None:
        await self.pilot.press(*keys)
        await self.pilot.pause()

    async def settle(
        self,
        predicate: Callable[[], bool],
        *,
        timeout_s: float = SETTLE_TIMEOUT_S,
        what: str = "the panel",
    ) -> None:
        """Pump the loop until the panel shows what we are waiting for."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            await self.pilot.pause()
            if predicate():
                return
            if loop.time() >= deadline:
                raise AssertionError(
                    f"{what} did not settle within {timeout_s:g}s.\n"
                    f"screen={self.screen_name}\n{self.screen_text()}"
                )
            await asyncio.sleep(0.05)

    async def go(self, name: str) -> None:
        """Navigate to a peer screen and wait for it to be on top."""
        self.app.go(name)
        await self.settle(lambda: self.screen_name == name, what=f"the {name} screen")

    async def wait_for_text(self, text: str, *, what: str | None = None) -> None:
        await self.settle(lambda: self.shows(text), what=what or f"{text!r} on screen")


@asynccontextmanager
async def _running_panel(socket_path: Path) -> AsyncIterator[Panel]:
    """Start a panel, hand it to the test, and shut it down afterwards.

    The context manager is entered and exited by hand rather than with
    ``async with`` for one reason: on shutdown, ``PanelScreen._tick`` reads
    ``self.app.screen`` from its 10 Hz repaint timer after Textual has emptied
    the screen stack, which raises ``ScreenStackError``.  Textual records that
    and re-raises it from ``run_test``'s ``__aexit__``, where it would both
    fail unrelated tests and *replace* a real assertion failure that was on
    its way out.  Exiting explicitly in a ``finally`` means the test's own
    exception keeps propagating and only this one shutdown artefact is
    swallowed.  It is a defect in ``fielddeck/ui/screens/__init__.py``, not
    something this suite should paper over silently — see the test report.
    """
    app = FieldDeckApp(socket_path=socket_path)
    context = app.run_test(size=PANEL_SIZE)
    pilot = await context.__aenter__()
    try:
        yield Panel(app, pilot)
    finally:
        with suppress(ScreenStackError):
            await context.__aexit__(None, None, None)


@pytest.fixture
async def panel(daemon: InstrumentDaemon) -> AsyncIterator[Panel]:
    """A panel connected to the simulated bench, on the home screen."""
    async with _running_panel(daemon.socket_path) as harness:
        await harness.settle(
            lambda: harness.app.state.link.connected and len(harness.app.state.devices) >= 4,
            what="the panel connecting to instrumentd",
        )
        await harness.settle(
            lambda: harness.screen_name == "home", what="the home screen to come up"
        )
        yield harness


@pytest.fixture
async def offline_panel(tmp_path: Path) -> AsyncIterator[Panel]:
    """A panel started with no daemon at all, which is a normal state."""
    async with _running_panel(tmp_path / "there-is-no-daemon.sock") as harness:
        await harness.settle(
            lambda: harness.app.state.link.attempts >= 1, what="the first connection attempt"
        )
        yield harness
