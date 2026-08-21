"""The panel with no daemon behind it.

This is not an error path, it is a normal state: the panel is the thing an
engineer is looking at *while* they restart the service that died, so it has
to come up without one, keep saying so, keep retrying, and refuse to pretend
it knows anything it cannot currently see.  A frozen panel showing the last
good numbers is worse than one that admits the link is down.
"""

from __future__ import annotations

from fielddeck.daemon.service import InstrumentDaemon

from .conftest import Panel


async def test_the_panel_starts_and_renders_without_instrumentd(
    offline_panel: Panel,
) -> None:
    panel = offline_panel
    await panel.settle(lambda: "NO DAEMON" in panel.chrome, what="the offline chrome")

    assert len(panel.lines()) == 25
    assert panel.screen_name == "home"
    assert panel.app.state.link.connected is False

    # The one-word state is not "SAFE": we do not know that.
    assert "LINK?" in panel.chrome
    assert "instrumentd unreachable" in panel.alert
    assert panel.widget("#chrome-alert").has_class("offline")

    # Live fields read as unknown rather than as a confident zero.
    assert "DEV ?" in panel.chrome


async def test_it_keeps_retrying_and_says_how_many_times(offline_panel: Panel) -> None:
    panel = offline_panel
    first = panel.app.state.link.attempts
    await panel.settle(
        lambda: panel.app.state.link.attempts > first, what="a second connection attempt"
    )
    await panel.settle(
        lambda: f"retry {panel.app.state.link.attempts}" in panel.alert,
        what="the retry count on the chrome",
    )


async def test_gestures_fail_honestly_instead_of_hanging(offline_panel: Panel) -> None:
    """A refused gesture must say that nothing was sent, not just fail."""
    panel = offline_panel
    outcome = await panel.app.state.mark("nobody-is-listening")
    assert outcome.ok is False
    assert outcome.code == "TransportError"
    assert outcome.preserved == "nothing was sent"

    await panel.settle(lambda: panel.shows("failed"), what="the failure on the annunciator")


async def test_navigation_still_works_with_the_link_down(offline_panel: Panel) -> None:
    panel = offline_panel
    for name in ("session", "safety", "system"):
        await panel.go(name)
        assert panel.screen_name == name
        # The chrome keeps saying the link is down on every screen.
        assert "NO DAEMON" in panel.chrome
    await panel.go("home")
    assert panel.screen_name == "home"


async def test_a_daemon_that_dies_under_a_running_panel_degrades_it(
    panel: Panel, daemon: InstrumentDaemon
) -> None:
    """Losing instrumentd mid-session must not take the panel with it."""
    assert panel.app.state.link.connected is True

    await daemon.stop()

    await panel.settle(lambda: "NO DAEMON" in panel.chrome, what="the panel to notice the loss")
    assert panel.app.state.link.connected is False
    assert "instrumentd unreachable" in panel.alert
    # The last known safety state is kept but marked stale, not shown as fresh.
    assert panel.app.state.safety.stale is True

    # Still a working panel: it renders, it navigates, it refuses honestly.
    assert len(panel.lines()) == 25
    await panel.go("safety")
    assert panel.shows("the daemon is unreachable")

    outcome = await panel.app.state.mark("after-the-daemon-died")
    assert outcome.ok is False
    assert outcome.code == "TransportError"
