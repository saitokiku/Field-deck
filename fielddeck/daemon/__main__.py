"""``python -m fielddeck.daemon`` / the ``instrumentd`` console script."""

from __future__ import annotations

import sys

from fielddeck.daemon.service import main

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
