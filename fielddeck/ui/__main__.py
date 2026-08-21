"""``fielddeck-ui`` — start the panel.

Deliberately tiny.  The panel's job is to come up on a Pi at boot, from a
systemd unit or a tmux window, and to keep running whether or not
``instrumentd`` is there yet — so this entry point parses a handful of options,
starts the app, and gets out of the way.  Anything that fails before Textual
takes the screen prints one line to stderr rather than a traceback nobody can
read on a 480x320 display.

``--sim`` sets ``FIELDDECK_SIM`` in this process's environment.  That does not
simulate anything here — the panel has no drivers to simulate — but a daemon
started from this environment picks it up, and the panel's chrome shows SIM as
soon as ``system.status`` reports a simulated daemon, so the two cannot
disagree about which one you are looking at.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from fielddeck import __version__

__all__ = ["main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fielddeck-ui",
        description="FieldDeck touchscreen HMI (80x25, single tap, keyboard equivalent).",
    )
    parser.add_argument(
        "--socket",
        type=Path,
        default=None,
        help="instrumentd control socket; defaults to the deployment's own path.",
    )
    parser.add_argument(
        "--sim",
        action="store_true",
        help="Set FIELDDECK_SIM=1 so a daemon started from this environment simulates.",
    )
    parser.add_argument("--version", action="version", version=f"fielddeck {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.sim:
        os.environ["FIELDDECK_SIM"] = "1"

    # Imported after the arguments are parsed so ``--help`` and ``--version``
    # do not pay for Textual on a Pi's slow SD card.
    from fielddeck.ui.app import FieldDeckApp

    app = FieldDeckApp(socket_path=args.socket, simulation_requested=bool(args.sim))
    try:
        app.run()
    except KeyboardInterrupt:  # pragma: no cover - operator pressed ^C
        return 130
    except OSError as exc:  # pragma: no cover - no terminal, no framebuffer
        print(f"fielddeck-ui: cannot start the panel: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
