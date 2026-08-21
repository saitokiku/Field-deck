"""What the panel says about authority, and how loudly it says it.

Three claims are tested here, and all three are about what an operator can
see without touching anything:

* nothing is armed unless the chrome says so, and when it does the number
  next to it is counting down;
* a latched emergency stop is impossible to miss, from whatever screen you
  happened to be on;
* a control that needs authorization says so *before* it is pressed, and when
  it is pressed anyway the daemon's own refusal is printed verbatim.

The panel never decides what is permitted — every refusal below comes from
``instrumentd``.  That is the property being protected: two implementations
of the permission model would mean one of them is wrong.
"""

from __future__ import annotations

import re

import pytest

from .conftest import Panel

COUNTDOWN = re.compile(r"ARMED CONTROL:(\d+)s")


async def test_the_chrome_shows_safe_then_the_armed_class_and_a_countdown(
    panel: Panel,
) -> None:
    assert "SAFE - nothing armed" in panel.alert
    assert panel.widget("#chrome-alert").has_class("safe")

    await panel.go("safety")
    await panel.pilot.click("#arm-control")
    await panel.settle(lambda: "ARMED CONTROL" in panel.alert, what="the armed banner")

    alert = panel.widget("#chrome-alert")
    # Colour is never load-bearing: the class drives the style, the words and
    # the class are both asserted so a monochrome panel still reads correctly.
    assert alert.has_class("armed")
    assert not alert.has_class("safe")
    assert "ARM" in panel.navigation

    countdown = COUNTDOWN.search(panel.alert)
    assert countdown is not None, f"no countdown in {panel.alert!r}"
    assert 0 < int(countdown.group(1)) <= 60

    # Exact-class authorization, visible on the panel: arming CONTROL did not
    # arm POWER.
    assert "POWER" not in panel.alert

    await panel.pilot.click("#safety-disarm")
    await panel.settle(lambda: "SAFE - nothing armed" in panel.alert, what="the SAFE banner")
    assert panel.widget("#chrome-alert").has_class("safe")


@pytest.mark.slow
async def test_the_arm_countdown_ticks_down(panel: Panel) -> None:
    """Authority is visibly temporary, or an operator stops believing it is.

    Slow by nature: the display is in whole seconds, so proving the number
    moves costs a second of wall clock.
    """
    await panel.go("safety")
    await panel.pilot.click("#arm-control")
    await panel.settle(lambda: COUNTDOWN.search(panel.alert) is not None, what="the countdown")

    start = COUNTDOWN.search(panel.alert)
    assert start is not None
    await panel.settle(
        lambda: (
            (match := COUNTDOWN.search(panel.alert)) is not None
            and int(match.group(1)) < int(start.group(1))
        ),
        what="the arm countdown to tick down",
    )


async def test_the_estop_state_is_rendered_unmissably(panel: Panel) -> None:
    """From whatever screen the operator was on, in words and reverse video."""
    await panel.go("bench")
    await panel.press("ctrl+e")

    await panel.settle(lambda: "EMERGENCY STOP LATCHED" in panel.alert, what="the ESTOP banner")
    alert = panel.widget("#chrome-alert")
    assert alert.has_class("estop")
    assert alert.size.width == 80, "the ESTOP banner must span the whole width"
    assert "acknowledge on ARM" in panel.alert

    # And in the top row's one-word state, and on the ARM tile.
    await panel.settle(lambda: "ESTOP" in panel.chrome, what="the chrome state word")
    assert "ESTOP" in panel.navigation

    # Arming is refused while it is latched, and the refusal is the daemon's.
    await panel.go("safety")
    await panel.pilot.click("#arm-power")
    # Waited for on the *screen*: the annunciator is what an operator reads,
    # and it repaints on the panel's own timer rather than on the reply.
    await panel.settle(lambda: panel.shows("refused"), what="the refusal on the annunciator")
    outcome = panel.app.state.last_outcome
    assert outcome is not None
    assert outcome.code == "EstopActive"
    assert "emergency stop" in outcome.message.lower()
    assert panel.app.state.safety.estop_active is True

    # Clearing is the deliberate act, and it asks a second time.
    await panel.pilot.click("#safety-clear")
    await panel.settle(lambda: panel.shows("CLEAR EMERGENCY STOP"), what="the confirmation panel")
    await panel.pilot.click("#confirm-yes")
    await panel.settle(
        lambda: not panel.app.state.safety.estop_active, what="the emergency stop to clear"
    )
    # Cleared is not re-armed: the safety screen goes back to SAFE, and the
    # chrome drops the ESTOP styling.  The alert row still carries the stop as
    # an unacknowledged *fault* until an operator clears it from MENU, which
    # is the priority ladder working as designed.
    await panel.settle(
        lambda: not panel.widget("#chrome-alert").has_class("estop"),
        what="the ESTOP banner to come down",
    )
    assert panel.shows("SAFE - nothing armed")
    assert panel.app.state.safety.armed_classes() == ()
    assert "EMERGENCY STOP" in panel.alert, "the stop stays on the annunciator until acknowledged"


async def test_the_confirmation_panel_can_be_backed_out_of(panel: Panel) -> None:
    """A stray touch must not be able to clear an emergency stop."""
    await panel.press("ctrl+e")
    await panel.settle(lambda: panel.app.state.safety.estop_active, what="the emergency stop")
    await panel.go("safety")

    await panel.pilot.click("#safety-clear")
    await panel.settle(lambda: panel.shows("CLEAR EMERGENCY STOP"), what="the confirmation panel")
    await panel.press("escape")

    await panel.settle(lambda: panel.screen_name == "safety", what="the safety screen")
    assert panel.app.state.safety.estop_active is True


async def test_power_controls_say_they_are_locked_before_they_are_pressed(
    panel: Panel,
) -> None:
    await panel.go("bench")
    await panel.wait_for_text("POWER authorization required", what="the bench authorization band")
    assert panel.shows("nothing is energised by this panel while POWER is unarmed")

    # Pressing it anyway prints the daemon's refusal, not the panel's opinion.
    await panel.pilot.click("#bench-apply")
    await panel.settle(lambda: panel.shows("refused"), what="the refusal on the annunciator")
    outcome = panel.app.state.last_outcome
    assert outcome is not None
    assert outcome.code == "PermissionDenied"
    assert "psu.set requires an active POWER authorization" in outcome.message
    assert outcome.preserved == "no command was sent to the device"

    # Nothing was energised by the attempt.
    assert panel.app.state.holds_output_lease is False

    # Once POWER is armed the same band says so, with the time left on it.
    await panel.go("safety")
    await panel.pilot.click("#arm-power")
    await panel.settle(lambda: "ARMED POWER" in panel.alert, what="the armed banner")

    await panel.go("bench")
    await panel.wait_for_text("POWER armed", what="the bench band to unlock")
    assert panel.shows("OUTPUT is live authority")


@pytest.mark.slow
async def test_the_can_screen_separates_listen_only_from_not_armed(panel: Panel) -> None:
    """Two different facts with two different fixes, never conflated."""
    await panel.go("can")
    await panel.wait_for_text("TX LOCKED", what="the CAN transmit band")
    assert panel.shows("interface is listen-only")
    assert panel.shows("CONTROL not armed")
