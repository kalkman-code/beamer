"""Desktop geometry and the pointer on Wayland: the twin of desktop_x11.py, with the same functions.

Wayland tells no app where the pointer is, and lets none move it. Beamer knows it only where it
puts it or is told: the InputCapture portal says where the pointer met a barrier, and every move
Beamer plays for a peer through RemoteDesktop is an absolute position it chose. So this module
keeps the last position as the truth, and the capture and injection modules keep it current:

- capture_portal, while the desktop holds the pointer at a barrier, moves it along the barrier
  and lets go of it at `position()` when input comes home;
- inject_portal registers a mover, which places the pointer with libei's absolute pointer while
  a peer drives this machine (and while nothing is captured).

The displays are the portal's zones when the InputCapture session has them, else the regions of
the RemoteDesktop absolute pointer, else what Qt says, all in the desktop's logical pixels. If
someone moves this machine's own mouse while a peer drives it, the position here is stale until
the peer's next move puts it back: no portal reports the local pointer.

A full-screen app cannot be seen and no button can be asked after (button_down is False), so the
full-screen hold and the drag guard have nothing to go on here."""

import logging
import threading
from typing import Callable, List, Optional, Tuple

from core.return_edge import Rect

LOGGER = logging.getLogger(__name__)

_lock = threading.Lock()
_position: Optional[Tuple[int, int]] = None
_zones: List[Rect] = []      # the InputCapture session's
_regions: List[Rect] = []    # the RemoteDesktop absolute pointer's
_mover: Optional[Callable[[int, int], None]] = None
_captured = False


def set_zones(zones: List[Rect]) -> None:
    global _zones
    with _lock:
        _zones = list(zones)


def set_regions(regions: List[Rect]) -> None:
    global _regions
    with _lock:
        _regions = list(regions)


def set_mover(mover: Optional[Callable[[int, int], None]]) -> None:
    """The injector's absolute move, or None when its session is gone."""
    global _mover
    with _lock:
        _mover = mover


def set_captured(captured: bool) -> None:
    """Whether the InputCapture session holds the pointer: then nothing is moved for real, and the
    position is where the pointer is let go."""
    global _captured
    with _lock:
        _captured = captured


def note_position(x: float, y: float) -> None:
    """Where the pointer is, as the desktop or Beamer's own last move says, held on a display."""
    global _position
    point = clamp_to(_displays(), x, y)
    with _lock:
        _position = point


def position() -> Optional[Tuple[int, int]]:
    with _lock:
        return _position


def clamp_to(displays: List[Rect], x: float, y: float) -> Tuple[int, int]:
    """The nearest point to (x, y) that is on a display: a barrier sits one pixel beyond the last
    one, and a staggered arrangement has gaps."""
    x, y = int(round(x)), int(round(y))
    if not displays:
        return x, y
    best = None
    for rect in displays:
        point = (min(max(x, rect.x), rect.right), min(max(y, rect.y), rect.bottom))
        distance = (point[0] - x) ** 2 + (point[1] - y) ** 2
        if best is None or distance < best[0]:
            best = (distance, point)
    return best[1]


def _qt_screens() -> List[Rect]:
    try:
        from PySide6.QtGui import QGuiApplication

        if QGuiApplication.instance() is None:
            return []
        return [Rect(g.x(), g.y(), g.width(), g.height()) for g in (screen.geometry() for screen in QGuiApplication.screens())]
    except Exception:
        LOGGER.exception("Could not read the displays from Qt")
        return []


def _displays() -> List[Rect]:
    with _lock:
        known = list(_zones or _regions)
    return known or _qt_screens()


def monitors() -> List[Rect]:
    return _displays() or [Rect(0, 0, 1920, 1080)]


def cursor_position() -> Tuple[int, int]:
    found = position()
    if found is not None:
        return found
    first = monitors()[0]
    return first.x + first.width // 2, first.y + first.height // 2


def set_cursor_position(x: int, y: int) -> None:
    """The position the pointer is at from now: placed there by the injector while nothing is
    captured, else where the capture lets it go when input comes home."""
    note_position(x, y)
    with _lock:
        mover = None if _captured else _mover
        point = _position
    if mover is not None:
        try:
            mover(*point)
        except Exception:
            LOGGER.exception("Could not place the pointer")


def button_down(name: str) -> bool:
    return False


def full_screen_app() -> Optional[str]:
    return None
