"""The emergency stop key the documentation names must actually work.

Four documents told an operator that **F9** is the emergency stop on the panel.
The HMI bound `ctrl+e` and had no F9 binding anywhere, so an operator who had
read the documentation and memorised one key would, in the moment that key
mattered, have pressed nothing.

Both keys work now. This test drives the real app through Textual's pilot and
presses them, rather than reading the BINDINGS list -- a binding that exists
but is shadowed by a modal, or whose action was renamed, still passes an
inspection and still fails an operator.
"""

from __future__ import annotations

import pytest

from fielddeck.ui.app import FieldDeckApp

#: Every key the docs promise, and the one the panel originally shipped with.
ESTOP_KEYS = ["f9", "ctrl+e"]


@pytest.mark.parametrize("key", ESTOP_KEYS)
async def test_the_estop_key_reaches_the_estop_action(key: str) -> None:
    app = FieldDeckApp(simulation_requested=True)
    fired: list[str] = []

    async with app.run_test() as pilot:
        # Patch the action rather than the daemon call: this test is about the
        # key reaching the action, and a real estop needs a running daemon.
        app.action_estop = lambda: fired.append(key)  # type: ignore[method-assign]
        await pilot.press(key)
        await pilot.pause()

    assert fired == [key], f"pressing {key} did not reach the emergency stop"


@pytest.mark.parametrize("key", ESTOP_KEYS)
def test_the_estop_binding_is_priority_so_a_modal_cannot_swallow_it(key: str) -> None:
    """Stopping must not depend on which screen the operator is looking at."""
    bindings = {
        binding.key: binding
        for binding in FieldDeckApp.BINDINGS
        if getattr(binding, "action", None) == "estop"
    }
    assert key in bindings, f"{key} is no longer bound to estop"
    assert bindings[key].priority, (
        f"{key} is not a priority binding, so a focused widget or modal can "
        "consume the emergency stop"
    )


def test_the_footer_advertises_exactly_one_estop_key() -> None:
    """One key in the footer, not two.

    An emergency stop advertising two keys invites a moment's choice, and that
    moment is the thing the binding exists to avoid.
    """
    shown = [
        binding.key
        for binding in FieldDeckApp.BINDINGS
        if getattr(binding, "action", None) == "estop" and binding.show
    ]
    assert shown == ["f9"], f"expected only f9 shown in the footer, got {shown}"


def test_the_docs_and_the_binding_name_the_same_key() -> None:
    """The bug was a disagreement between two files, so test the agreement."""
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent.parent
    docs = [repo / "README.md", *sorted((repo / "docs").glob("*.md"))]

    claimed: set[str] = set()
    for doc in docs:
        for match in re.finditer(
            r"`?(F9|Ctrl\+E)`?[^.\n]{0,40}(emergency stop|panel|E-?STOP)"
            r"|(emergency stop|E-?STOP)[^.\n]{0,40}`?(F9|Ctrl\+E)`?",
            doc.read_text(),
            re.I,
        ):
            claimed.update(
                group.lower()
                for group in match.groups()
                if group and group.lower() in {"f9", "ctrl+e"}
            )

    bound = {
        binding.key.lower()
        for binding in FieldDeckApp.BINDINGS
        if getattr(binding, "action", None) == "estop"
    }
    unbound = claimed - bound
    assert not unbound, (
        f"the docs tell an operator to press {sorted(unbound)} for the emergency "
        f"stop, but the HMI binds {sorted(bound)}"
    )
