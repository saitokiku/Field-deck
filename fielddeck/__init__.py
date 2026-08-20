"""FieldDeck — a safety-first universal engineering console.

The public contract lives in :mod:`fielddeck.common`.  Everything that can
touch hardware goes through :mod:`fielddeck.daemon` (``instrumentd``).
"""

from __future__ import annotations

__version__ = "0.1.0"

#: Wire-protocol version spoken over the ``instrumentd`` Unix socket.
#: Bumped only on incompatible changes; additive changes keep the major.
RPC_PROTOCOL_VERSION = 1

__all__ = ["RPC_PROTOCOL_VERSION", "__version__"]
