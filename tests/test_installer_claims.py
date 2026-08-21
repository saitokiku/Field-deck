"""The installer must install what it says it installs.

`install.sh` announced "core build + git + tmux", and a comment stated that
tmux was in `PKGS_BUILD` "conceptually".  It was not in `PKGS_BUILD` at all.
tmux is the top-level session manager for the whole kiosk chain (Xorg → xterm →
**tmux** → HMI) and is not preinstalled on Raspberry Pi OS Lite, so a clean
install produced a unit whose panel could not start.

`preflight.sh` would have caught it -- it treats a missing tmux as a hard
failure -- but only after the install, on the Pi, at the point the operator
expected to be finished.

The same shape bit the camera: `camera.snapshot` shells out to fswebcam, ffmpeg
or v4l2-ctl, none of which was installed and none of which preflight mentioned,
so the camera actions were registered and could never succeed.

These tests read the shell scripts as data.  That is unusual, and it is worth
it: the alternative is finding out on hardware.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO / "scripts" / "install.sh"
PREFLIGHT_SH = REPO / "scripts" / "preflight.sh"

GROUPS = ("PKGS_BUILD", "PKGS_BUS", "PKGS_INSTRUMENT", "PKGS_KIOSK")


def _packages() -> dict[str, list[str]]:
    source = INSTALL_SH.read_text()
    found: dict[str, list[str]] = {}
    for name in GROUPS:
        match = re.search(rf"^{name}=\(\n(.*?)^\)", source, re.S | re.M)
        assert match, f"{name} is no longer a plain array in install.sh"
        found[name] = [
            stripped
            for line in match.group(1).splitlines()
            if (stripped := line.split("#")[0].strip())
        ]
    return found


PACKAGES = _packages()
ALL_PACKAGES = [pkg for group in PACKAGES.values() for pkg in group]


def test_the_package_groups_are_all_populated() -> None:
    """Guard the parser: an empty group would make every test below vacuous."""
    for name, packages in PACKAGES.items():
        assert packages, f"{name} parsed as empty; the extractor is broken"


@pytest.mark.parametrize(
    ("package", "why"),
    [
        ("tmux", "the top-level session manager for HMI/CLAUDE/SHELL/LOG"),
        ("python3-venv", "the virtualenv the daemon runs from"),
        ("git", "recipes and firmware are usually pulled from git"),
        ("can-utils", "candump is the reference for 'is the bus alive'"),
        ("usbutils", "lsusb is the first thing anyone runs when a probe vanishes"),
        ("xterm", "the kiosk terminal"),
        ("xfonts-base", "the 6x12 bitmap font that makes 80x25 land on 480x300"),
        ("xinit", "the kiosk service runs xinit directly"),
    ],
)
def test_a_required_package_is_actually_in_a_group(package: str, why: str) -> None:
    assert package in ALL_PACKAGES, f"install.sh does not install {package}, needed for {why}"


def test_the_kiosk_group_is_the_only_place_x_packages_live() -> None:
    """``--no-kiosk`` drops exactly one group, so nothing X may hide elsewhere."""
    non_kiosk = [pkg for name, group in PACKAGES.items() if name != "PKGS_KIOSK" for pkg in group]
    stray = [pkg for pkg in non_kiosk if pkg.startswith(("xserver-", "xfonts-")) or pkg == "xinit"]
    assert not stray, f"X packages outside PKGS_KIOSK would be installed by --no-kiosk: {stray}"


def test_every_camera_backend_the_code_accepts_is_named_by_preflight() -> None:
    """A registered action that can never succeed is worse than an absent one."""
    source = (REPO / "fielddeck" / "capture" / "camera.py").read_text()
    match = re.search(r"for tool in \(([^)]*)\):", source)
    assert match, "the capture-backend list is no longer a literal tuple"
    accepted = re.findall(r'"([^"]+)"', match.group(1))
    assert accepted, "no capture backends found"

    preflight = PREFLIGHT_SH.read_text()
    missing = [tool for tool in accepted if tool not in preflight]
    assert not missing, (
        f"camera.snapshot accepts {missing} but preflight never mentions them, so an "
        "operator with one installed is told the camera cannot work"
    )

    assert any(tool in ALL_PACKAGES for tool in (*accepted, "v4l-utils")), (
        "install.sh installs no still-capture backend, so camera.snapshot is "
        f"registered and can never succeed (it accepts {accepted})"
    )


def test_preflight_hard_failures_are_things_the_installer_provides() -> None:
    """Anything preflight calls a *failure* must be something install.sh supplies.

    A warning means "absent, and here is what you lose". A failure means broken.
    The installer failing to provide something it then reports as broken is the
    exact bug this file exists for.
    """
    #: Hard failures that apt does not answer for.  FieldDeck's own commands
    #: come from the virtualenv, not a package, and a label built from a shell
    #: variable is not a tool name at all.
    NOT_FROM_APT = {"instrumentd", "fdctl", "fielddeck-ui", "fielddeck-mcp"}

    preflight = PREFLIGHT_SH.read_text()
    # e.g.  fail "tmux installed" "no session manager, no panel navigation"
    failures = re.findall(r'^\s*fail\s+"([^"]+)"', preflight, re.M)
    assert failures, "no fail() calls found; the extractor is broken"

    tool_failures = {
        label.split()[0]
        for label in failures
        if label.endswith(" installed") and re.fullmatch(r"[a-z0-9][a-z0-9._-]*", label.split()[0])
    }
    missing = sorted(tool_failures - set(ALL_PACKAGES) - NOT_FROM_APT)
    assert not missing, (
        f"preflight reports {missing} as a hard failure, but install.sh never installs it"
    )
