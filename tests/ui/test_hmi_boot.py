"""The panel comes up, and comes up the right size.

80x25 is not a preference, it is the hardware: a 480x320 resistive panel at
the 6x12 console font.  A layout that needs 81 columns loses a column of the
safety chrome on the physical device and nowhere else, so the row and column
budget is asserted here rather than eyeballed.
"""

from __future__ import annotations

from fielddeck.daemon.service import InstrumentDaemon
from fielddeck.ui.widgets.tiles import Tile

from .conftest import PANEL_SIZE, Panel


async def test_the_panel_renders_at_eighty_by_twenty_five(panel: Panel) -> None:
    assert tuple(panel.app.size) == PANEL_SIZE

    lines = panel.lines()
    assert len(lines) == 25
    assert {len(line) for line in lines} == {80}, "a row is not exactly 80 columns wide"


async def test_the_row_budget_is_spent_where_the_spec_says(panel: Panel) -> None:
    """Two rows of chrome at the top, five at the bottom, body in between."""
    status_bar = panel.widget("#status-bar")
    bottom = panel.widget("#chrome-bottom")
    nav = panel.widget("#nav-bar")

    assert status_bar.size.height == 2
    assert bottom.size.height == 5
    assert nav.size.height == 3
    assert status_bar.size.width == nav.size.width == 80


async def test_every_navigation_tile_clears_the_touch_minimum(panel: Panel) -> None:
    """15 columns by 3 rows is the ~90x45 px a fingertip needs on the panel.

    Measured on the outer box, border included: the border is part of what a
    finger lands on, and the tile takes the tap wherever inside it lands.
    """
    tiles = list(panel.widget("#nav-bar").query(Tile).results(Tile))
    assert len(tiles) == 5
    for tile in tiles:
        assert tile.outer_size.width >= 15, f"{tile.key} is too narrow to hit with a glove"
        assert tile.outer_size.height >= 3, f"{tile.key} is too short to hit with a glove"
    # Five tiles, sixteen columns each, filling the row exactly.
    assert sum(tile.outer_size.width for tile in tiles) == 80

    for tile in panel.app.screen.query(".home-tile").results(Tile):
        assert tile.outer_size.width >= 15
        assert tile.outer_size.height >= 3


async def test_the_chrome_answers_the_four_permanent_questions(
    panel: Panel, daemon: InstrumentDaemon
) -> None:
    """What is armed, is it recording, has anything faulted, which session."""
    await panel.settle(
        lambda: f"DEV {len(daemon.registry)}" in panel.chrome,
        what="the device count to reach the chrome",
    )
    chrome = panel.chrome
    assert chrome.startswith("FIELDDECK")
    # Simulation is never hidden: an operator must know what they are looking at.
    assert "SIM" in chrome
    assert "SAFE" in chrome
    assert "REC" in chrome
    assert "SES" in chrome
    assert "DAEMON" in chrome

    assert "SAFE - nothing armed" in panel.alert


async def test_home_shows_the_families_of_hardware_present(panel: Panel) -> None:
    text = panel.screen_text()
    for label in ("BUS", "BENCH", "LOGIC", "DEVICE", "TOOLS", "ASSISTANT"):
        assert label in text

    # The tiles carry live counts, so "3 ✓" means the adapters really came back.
    await panel.settle(
        lambda: "BUS  3" in panel.screen_text(),
        what="the BUS tile to count the simulated bus devices",
    )
    assert "HOME" in panel.navigation
    assert "MENU" in panel.navigation
