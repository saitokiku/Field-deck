"""The ``fdctl`` command line: FieldDeck's manual, scriptable surface.

FieldDeck is manual-first, so this package is the reference client rather than
a convenience wrapper — anything the HMI or the assistant can do must be
reachable from a command an engineer can paste into a runbook.  Nothing here
touches hardware or decides what is permitted; every command speaks to
``instrumentd`` over the control socket and reports what it was told.

:mod:`fielddeck.cli.fdctl` holds the command tree, :mod:`fielddeck.cli.formatting`
holds every decision about how something looks.
"""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Entry point indirection so importing this package stays cheap.

    ``fdctl`` is on the PATH of a device that also runs the daemon and the HMI;
    importing typer and rich only when the CLI is actually invoked keeps
    ``import fielddeck.cli`` free for anything that just wants the name.
    """
    from fielddeck.cli.fdctl import main as _main

    return _main(argv)
