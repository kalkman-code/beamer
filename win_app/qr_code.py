"""The pairing QR as a grid of dark and light modules, for the window to paint.

The text is the one WIRE.md section 6 specifies (`core.pairing.qr_text`); segno is pure Python, so the
built app needs nothing beside it. Level M: a phone camera reads it off a screen at that, and every
level above it only makes the code denser.
"""

from __future__ import annotations

from typing import Optional

try:
    import segno
except ImportError:  # the sheet shows the code and the address alone
    segno = None

QUIET = 4


def modules(text: str) -> Optional[list]:
    """Rows of booleans, True for a dark module, with the four-module quiet zone the QR standard
    asks of the reader's surround; None when segno is not installed."""
    if segno is None:
        return None
    rows = [list(map(bool, row)) for row in segno.make(text, error="m", micro=False, boost_error=False).matrix]
    edge = len(rows[0]) + 2 * QUIET
    return [[False] * edge] * QUIET + [[False] * QUIET + row + [False] * QUIET for row in rows] + [[False] * edge] * QUIET
