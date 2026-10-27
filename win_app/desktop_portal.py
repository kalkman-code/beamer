"""Desktop geometry and the pointer on Wayland: the twin of desktop_x11.py, with the same functions.

Wayland tells no app where the pointer is, and lets none move it. Beamer knows it only where it
puts it or is told: the InputCapture portal says where the pointer met a barrier, and every move
Beamer plays for a peer through RemoteDesktop is an absolute position it chose. So this module
keeps the last position as the truth, and the capture and injection modules keep it current:

- capture_portal, while the desktop holds the pointer at a barrier, moves it along the barrier
  and lets go of it at `position()` when input comes home;
- inject_portal registers a mover, which places the pointer with libei's absolute pointer while
  a peer drives this machine (and while nothing is captured).

InputCapture zones and RemoteDesktop absolute-pointer regions can describe the same displays at
different scales and in different orders. Qt's display keys are reconciled to each portal's
rectangles; remembered pointer positions stay in the InputCapture coordinate space and are
translated before libei absolute moves. If someone moves this machine's own mouse while a peer
drives it, the position here is stale until the peer's next move puts it back: no portal reports
the local pointer.

A full-screen app cannot be seen and no button can be asked after (button_down is False), so the
full-screen hold and the drag guard have nothing to go on here. GNOME and KDE hide the pointer
for the lifetime of active InputCapture, restoring it on release or client disconnection.
Outside capture, Wayland provides no global hide or local-motion notification. A blank-cursor
surface would have to take pointer focus from other apps, so idle hiding is left unavailable."""

import logging
import math
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
_screens: List[Rect] = []
_mapping_warning_logged = False


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


def refresh_qt_screens() -> None:
    """Capture Qt geometry on the GUI thread for link and input threads."""
    global _screens
    try:
        from PySide6.QtCore import QThread
        from PySide6.QtGui import QGuiApplication

        app = QGuiApplication.instance()
        if app is None or QThread.currentThread() != app.thread():
            return
        screens = [Rect(g.x(), g.y(), g.width(), g.height())
                   for g in (screen.geometry() for screen in app.screens())]
        with _lock:
            _screens = screens
    except Exception:
        LOGGER.exception("Could not read the displays from Qt")


def _qt_screens() -> List[Rect]:
    with _lock:
        return list(_screens)


def _displays() -> List[Rect]:
    with _lock:
        known = list(_zones or _regions)
    return known or _qt_screens()


def _warn_ambiguous_mapping() -> None:
    global _mapping_warning_logged
    with _lock:
        if _mapping_warning_logged:
            return
        _mapping_warning_logged = True
    LOGGER.warning("Portal rectangles do not uniquely identify Qt displays; skipping display mapping")


def monitors() -> List[Rect]:
    return _displays() or [Rect(0, 0, 1920, 1080)]


def _match_rectangles(screens: List[Rect], rectangles: List[Rect]) -> List[Rect]:
    """Match portal rectangles to Qt's display order without relying on portal enumeration or
    absolute scale. The portal exposes no display IDs, so aspect ratio and relative desktop layout
    are the identity available across the two coordinate spaces."""
    if not screens or len(screens) != len(rectangles):
        return []
    if len(screens) == 1:
        return list(rectangles)

    def normalised_centres(items):
        left = min(rect.x for rect in items)
        top = min(rect.y for rect in items)
        right = max(rect.x + rect.width for rect in items)
        bottom = max(rect.y + rect.height for rect in items)
        width, height = max(right - left, 1), max(bottom - top, 1)
        return [((r.x + r.width / 2 - left) / width,
                 (r.y + r.height / 2 - top) / height) for r in items]

    screen_centres = normalised_centres(screens)
    rect_centres = normalised_centres(rectangles)

    costs = []
    for i, screen in enumerate(screens):
        row = []
        for j, rect in enumerate(rectangles):
            screen_width, screen_height = max(screen.width, 1), max(screen.height, 1)
            rect_width, rect_height = max(rect.width, 1), max(rect.height, 1)
            ratio_error = abs(math.log((screen_width / screen_height) / (rect_width / rect_height)))
            sx, sy = screen_centres[i]
            rx, ry = rect_centres[j]
            row.append(ratio_error * 8 + abs(sx - rx) + abs(sy - ry))
        costs.append(row)

    def solve(matrix):
        count = len(matrix)
        row_potential = [0.0] * (count + 1)
        column_potential = [0.0] * (count + 1)
        assigned_row = [0] * (count + 1)
        previous_column = [0] * (count + 1)
        for row in range(1, count + 1):
            assigned_row[0] = row
            column = 0
            minimum = [float("inf")] * (count + 1)
            used = [False] * (count + 1)
            while True:
                used[column] = True
                active_row = assigned_row[column]
                delta, next_column = float("inf"), 0
                for candidate in range(1, count + 1):
                    if used[candidate]:
                        continue
                    reduced = (matrix[active_row - 1][candidate - 1]
                               - row_potential[active_row] - column_potential[candidate])
                    if reduced < minimum[candidate]:
                        minimum[candidate] = reduced
                        previous_column[candidate] = column
                    if minimum[candidate] < delta:
                        delta, next_column = minimum[candidate], candidate
                for candidate in range(count + 1):
                    if used[candidate]:
                        row_potential[assigned_row[candidate]] += delta
                        column_potential[candidate] -= delta
                    else:
                        minimum[candidate] -= delta
                column = next_column
                if assigned_row[column] == 0:
                    break
            while True:
                prior = previous_column[column]
                assigned_row[column] = assigned_row[prior]
                column = prior
                if column == 0:
                    break

        columns = [0] * count
        for column in range(1, count + 1):
            columns[assigned_row[column] - 1] = column - 1
        return columns

    # If another full assignment has the same geometry score, the portal gave no identity
    # signal for those displays. Leave them unmatched instead of targeting one by guesswork.
    assignment = solve(costs)
    best_cost = sum(costs[row][column] for row, column in enumerate(assignment))
    for row, column in enumerate(assignment):
        alternative = [line[:] for line in costs]
        alternative[row][column] = float("inf")
        other = solve(alternative)
        other_cost = sum(costs[index][candidate] for index, candidate in enumerate(other))
        if abs(other_cost - best_cost) <= 1e-9:
            _warn_ambiguous_mapping()
            return []
    return [rectangles[column] for column in assignment]


def _map_rectangles(rectangles: List[Rect]) -> dict:
    screens = _qt_screens()
    if not rectangles:
        rectangles = screens
    matched = _match_rectangles(screens, rectangles)
    return {str(index): rect for index, rect in enumerate(matched)}


def display_map() -> dict:
    """Qt display keys expressed in the InputCapture zone coordinates used for crossing."""
    with _lock:
        zones, regions = list(_zones), list(_regions)
    return _map_rectangles(zones or regions)


def absolute_display_map() -> dict:
    """Qt display keys expressed in libei's absolute pointer coordinate space."""
    with _lock:
        regions = list(_regions)
    return _map_rectangles(regions) if regions else display_map()


def absolute_position(x: int, y: int) -> Optional[Tuple[int, int]]:
    """Translate a point from InputCapture coordinates into libei's absolute-pointer regions."""
    source = display_map()
    target = absolute_display_map()
    if not source or not target:
        with _lock:
            zones = list(_zones)
        if not zones:
            return x, y
        _warn_ambiguous_mapping()
        return None
    for key, rect in source.items():
        if rect.x <= x <= rect.right and rect.y <= y <= rect.bottom:
            other = target.get(key)
            if other is None:
                break
            px = (x - rect.x) / max(rect.width - 1, 1)
            py = (y - rect.y) / max(rect.height - 1, 1)
            return (other.x + round(px * max(other.width - 1, 0)),
                    other.y + round(py * max(other.height - 1, 0)))
    return x, y


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
