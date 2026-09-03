"""Getting somewhere, and getting back.

An instrument panel is used by someone with one hand free and no attention to
spare, so the navigation contract is small and absolute: every screen is
reachable, every screen keeps the safety chrome, and there is always a way
back to home that does not require remembering how you got here.
"""

from __future__ import annotations

import pytest

from .conftest import Panel

#: Every screen the panel offers.  Listed rather than discovered, so adding a
#: screen without a way to reach it fails here.
PEER_SCREENS = ("session", "safety", "system", "bench", "can", "serial", "tools")


async def test_home_reaches_every_screen_and_returns(panel: Panel) -> None:
    for name in PEER_SCREENS:
        await panel.go(name)
        assert panel.screen_name == name
        # The chrome is not optional on any screen: safety state stays visible.
        assert panel.chrome.startswith("FIELDDECK")
        assert "ARM" in panel.navigation

        await panel.go("home")
        assert panel.screen_name == "home"


async def test_the_navigation_keys_match_the_tiles(panel: Panel) -> None:
    """Everything reachable by touch is reachable from a keyboard."""
    await panel.press("s")
    await panel.settle(lambda: panel.screen_name == "session", what="the session screen")

    await panel.press("a")
    await panel.settle(lambda: panel.screen_name == "safety", what="the safety screen")
    assert panel.shows("SAFE - nothing armed")

    await panel.press("m")
    await panel.settle(lambda: panel.screen_name == "system", what="the system screen")

    await panel.press("h")
    await panel.settle(lambda: panel.screen_name == "home", what="the home screen")


async def test_a_device_opens_above_the_list_so_back_returns_to_it(panel: Panel) -> None:
    """Drilling is descent: BACK goes to the list, not to the home grid."""
    panel.app.go_discovery("bus")
    await panel.settle(lambda: panel.screen_name == "discovery", what="the discovery list")
    assert panel.shows("DISCOVERY")
    assert [getattr(screen, "screen_name", None) for screen in panel.app.screen_stack][-2:] == [
        "home",
        "discovery",
    ]

    await panel.press("escape")
    await panel.settle(lambda: panel.screen_name == "home", what="the home screen")


async def test_the_discovery_list_shows_what_enumeration_found(panel: Panel) -> None:
    panel.app.go_discovery("bus")
    await panel.settle(lambda: panel.screen_name == "discovery", what="the discovery list")
    await panel.wait_for_text("filter bus", what="the filter to be applied")

    # The simulated bench has a CAN interface, a UART and a Modbus station.
    await panel.settle(
        lambda: "3 device(s)" in panel.screen_text(),
        what="the filtered device count",
    )
    assert "RESCAN" in panel.screen_text()


@pytest.mark.slow
async def test_the_bus_screens_show_live_state_from_the_daemon(panel: Panel) -> None:
    """CAN and serial screens render what the daemon reports, not placeholders."""
    await panel.go("can")
    await panel.wait_for_text("500 kbit/s", what="the CAN bitrate from can.status")
    assert panel.shows("LISTEN-ONLY")

    await panel.go("serial")
    await panel.wait_for_text("115200", what="the serial baud rate from serial.status")
