"""Filesystem layout.

Two modes:

* **Deployed** — ``/etc/fielddeck``, ``/var/lib/fielddeck``, ``/run/fielddeck``.
* **Development** — everything under ``$FIELDDECK_HOME`` (default
  ``~/.local/share/fielddeck``) so the whole stack runs unprivileged on a
  laptop with no install step.

Every path is overridable by environment variable, because a field device
often has its session store on an external SSD.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Paths", "default_paths", "socket_path"]

_SYSTEM_STATE = Path("/var/lib/fielddeck")
_SYSTEM_CONFIG = Path("/etc/fielddeck")
_SYSTEM_RUNTIME = Path("/run/fielddeck")


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def _system_install() -> bool:
    """True when the deployed layout is present on this machine.

    Detected by **existence**, deliberately not by writability.  On a real
    install ``/var/lib/fielddeck`` belongs to the daemon's own system user at
    mode 0750: an operator is in the ``fielddeck`` group, so they can open the
    control socket, but they cannot write to the state directory.  Keying this
    off write access sent every non-root client looking for the socket under
    its own home directory, where no daemon has ever listened, and ``fdctl``
    reported "instrumentd is not running" on a perfectly healthy unit.

    A developer who has FieldDeck installed *and* wants a private instance
    sets ``FIELDDECK_HOME``, which is checked before this function.
    """
    return _SYSTEM_STATE.is_dir() or _SYSTEM_RUNTIME.is_dir()


@dataclass(frozen=True, slots=True)
class Paths:
    home: Path
    config_dir: Path
    state_dir: Path
    runtime_dir: Path
    sessions_dir: Path
    log_dir: Path

    @property
    def socket(self) -> Path:
        return self.runtime_dir / "instrumentd.sock"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "fielddeck.yaml"

    @property
    def safety_file(self) -> Path:
        return self.config_dir / "safety.yaml"

    @property
    def ui_file(self) -> Path:
        return self.config_dir / "ui.yaml"

    @property
    def instruments_dir(self) -> Path:
        return self.config_dir / "instruments"

    def ensure(self) -> Paths:
        """Create the directories this process needs.  Idempotent.

        Only the daemon calls this.  Clients resolve paths without creating
        anything, because a client has no business writing to the state store
        and usually lacks permission to.
        """
        for path in (self.state_dir, self.sessions_dir, self.runtime_dir, self.log_dir):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except PermissionError as exc:
                from fielddeck.common.errors import ConfigurationError

                raise ConfigurationError(
                    f"cannot create {path}: {exc}. Run instrumentd as the "
                    "'fielddeck' user, or set FIELDDECK_HOME to a directory you "
                    "own for a private development instance.",
                    details={"path": str(path)},
                ) from exc
        return self


def default_paths() -> Paths:
    """Resolve the active layout from the environment."""
    explicit_home = _env_path("FIELDDECK_HOME")
    if explicit_home is not None:
        home = explicit_home
        config_dir = _env_path("FIELDDECK_CONFIG_DIR") or home / "config"
        state_dir = _env_path("FIELDDECK_STATE_DIR") or home / "state"
        runtime_dir = _env_path("FIELDDECK_RUNTIME_DIR") or home / "run"
    elif _system_install():
        home = _SYSTEM_STATE
        config_dir = _env_path("FIELDDECK_CONFIG_DIR") or _SYSTEM_CONFIG
        state_dir = _env_path("FIELDDECK_STATE_DIR") or _SYSTEM_STATE
        runtime_dir = _env_path("FIELDDECK_RUNTIME_DIR") or _SYSTEM_RUNTIME
    else:
        home = (
            Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")).expanduser()
            / "fielddeck"
        )
        config_dir = _env_path("FIELDDECK_CONFIG_DIR") or home / "config"
        state_dir = _env_path("FIELDDECK_STATE_DIR") or home / "state"
        runtime_dir = _env_path("FIELDDECK_RUNTIME_DIR") or home / "run"

    return Paths(
        home=home,
        config_dir=config_dir,
        state_dir=state_dir,
        runtime_dir=runtime_dir,
        sessions_dir=_env_path("FIELDDECK_SESSIONS_DIR") or state_dir / "sessions",
        log_dir=_env_path("FIELDDECK_LOG_DIR") or state_dir / "logs",
    )


def socket_path() -> Path:
    """The control socket, honouring ``FIELDDECK_SOCKET``.

    When no layout has been forced, a socket that actually exists wins over
    one that merely would have been correct.  Being unable to find a running
    daemon is the single most annoying failure a CLI can have.
    """
    override = _env_path("FIELDDECK_SOCKET")
    if override is not None:
        return override

    candidate = default_paths().socket
    if candidate.exists() or os.environ.get("FIELDDECK_HOME"):
        return candidate

    deployed = _SYSTEM_RUNTIME / "instrumentd.sock"
    return deployed if deployed.exists() else candidate
