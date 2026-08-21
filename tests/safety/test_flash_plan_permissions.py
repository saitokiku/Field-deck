"""A plan may never be more dangerous than the permission that authorized it.

`flash.verify` declares QUERY.  `debug.reset` declares CONTROL.  Both went
through `DebugActions._execute`, which built a plan with `build_plan()` and ran
whatever came back without comparing the plan's own permission against the one
the dispatcher had granted.

Every planner ended with an unconditional "and anything else is a program"
return, so an operation a tool did not implement silently became a firmware
write:

    pyocd    verify -> program (FLASH)   authorized as QUERY
    dfu-util verify -> program (FLASH)   authorized as QUERY
    dfu-util reset  -> program (FLASH)   authorized as CONTROL
    dfu-util erase  -> program (FLASH)   authorized as DESTRUCTIVE, and not
                                         even the operation that was confirmed

Fixed in two places on purpose.  The planners now refuse an operation they do
not implement, and `_execute` refuses to run any plan above the granted
permission -- so a tool wrapper added next year with the same bug is caught
even if the first check is forgotten.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from fielddeck.common.errors import UnsupportedCapability
from fielddeck.common.models import PermissionLevel
from fielddeck.debug.flash import build_plan

#: The action each operation is reached through, and what that action declares.
DECLARED: dict[str, PermissionLevel] = {
    "info": PermissionLevel.QUERY,
    "verify": PermissionLevel.QUERY,
    "reset": PermissionLevel.CONTROL,
    "program": PermissionLevel.FLASH,
    "erase": PermissionLevel.DESTRUCTIVE,
}

TOOLS = ["openocd", "pyocd", "esptool", "dfu-util"]


@pytest.fixture
def firmware() -> Path:
    path = Path(tempfile.mkdtemp()) / "app.bin"
    path.write_bytes(b"\x00" * 64)
    return path


def _plan(tool: str, operation: str, firmware: Path):
    return build_plan(
        tool=tool,
        operation=operation,
        target="stm32f4x",
        interface="stlink",
        port="/dev/ttyUSB0",
        firmware_path=str(firmware),
        address="0x0",
        baud=460800,
        alt="0",
        device=None,
        extra_roots=[firmware.parent],
    )[0]


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("operation", sorted(DECLARED))
def test_a_plan_never_exceeds_the_permission_its_action_declares(
    tool: str, operation: str, firmware: Path
) -> None:
    try:
        plan = _plan(tool, operation, firmware)
    except UnsupportedCapability:
        # Refusing is the correct answer for a tool that cannot do this. It is
        # the *silent substitution* that was dangerous, not the absence.
        return

    declared = DECLARED[operation]
    assert plan.permission.rank <= declared.rank, (
        f"{tool} {operation} builds a {plan.permission} plan "
        f"({plan.operation}: {' '.join([plan.tool, *plan.args])}) but the action that "
        f"reaches it is declared {declared}"
    )


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("operation", sorted(DECLARED))
def test_a_plan_does_what_it_was_asked_to_do(tool: str, operation: str, firmware: Path) -> None:
    """The operation must not be substituted either.

    ``dfu-util erase`` returned a *program* plan. Even at matching permission
    that would be wrong: the operator confirmed an erase by name, and would
    have got a write.
    """
    try:
        plan = _plan(tool, operation, firmware)
    except UnsupportedCapability:
        return
    assert plan.operation == operation, (
        f"{tool} was asked to {operation} and planned to {plan.operation} instead"
    )


#: The exact pairs that used to fall through to a firmware write. ``build_plan``
#: already rejected a *wholly unknown* operation; the bug was a operation that
#: FieldDeck knows about reaching a tool that does not implement it.
UNSUPPORTED = [
    ("pyocd", "verify"),
    ("dfu-util", "verify"),
    ("dfu-util", "reset"),
    ("dfu-util", "erase"),
    ("esptool", "reset"),
]


@pytest.mark.parametrize(("tool", "operation"), UNSUPPORTED)
def test_an_unsupported_operation_is_refused_rather_than_substituted(
    tool: str, operation: str, firmware: Path
) -> None:
    """Each of these used to return a plan that writes firmware."""
    with pytest.raises(UnsupportedCapability) as caught:
        _plan(tool, operation, firmware)
    assert caught.value.preserved == "nothing was sent to the target"
    # The refusal has to say what the tool *can* do, or the operator is left
    # guessing which of six wrappers to reach for instead.
    assert "supports" in str(caught.value)


def test_a_wholly_unknown_operation_is_still_rejected(firmware: Path) -> None:
    """The pre-existing guard, kept honest: it catches names, not capabilities."""
    from fielddeck.common.errors import InvalidRequest

    with pytest.raises(InvalidRequest):
        _plan("openocd", "definitely-not-an-operation", firmware)


async def test_the_dispatcher_refuses_a_plan_above_the_granted_permission(client, daemon) -> None:
    """The structural half, exercised end to end.

    Reaches ``_execute``'s guard by asking a QUERY action for a plan that a
    planner would build at FLASH. If the planners are correct this cannot
    happen -- which is the point of having the second check.
    """
    from fielddeck.debug import actions as debug_actions

    firmware = Path(tempfile.mkdtemp()) / "app.bin"
    firmware.write_bytes(b"\x00" * 64)

    from fielddeck.debug.flash import FlashPlan

    def _lying_plan(**kwargs):
        return (
            FlashPlan(
                tool="openocd",
                args=["-c", "program app.bin verify reset exit"],
                operation="program",
                permission=PermissionLevel.FLASH,
                description="write the image",
                target="stm32f4x",
                firmware=str(firmware),
            ),
            {},
        )

    original = debug_actions.build_plan
    debug_actions.build_plan = _lying_plan  # type: ignore[assignment]
    try:
        await client.call("safety.arm", {"permission": "query", "ttl_s": 60})
        result = await client.try_execute(
            "flash.verify",
            {"tool": "openocd", "target": "stm32f4x", "firmware_path": str(firmware)},
        )
    finally:
        debug_actions.build_plan = original  # type: ignore[assignment]

    assert not result.ok, "a FLASH plan ran under a QUERY grant"
    assert result.error["code"] == "PermissionDenied"
    assert "authorized" in result.error["message"]
