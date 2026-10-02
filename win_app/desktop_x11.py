"""X11 pointer and monitor geometry, the Linux twin of desktop_win.

Monitors come from RandR 1.5 rather than Qt's screens: the sender asks from the capture thread,
where Qt's screen list is not safe to read, and RandR reports X root pixels, the pointer's own
space, which effect_overlay.logical_point already maps to Qt's. There is no work area here
because nothing reads one.

python-xlib is imported inside _connect, never at module import, so this module imports on the
Mac and on Windows, where the test suite loads every test module without it installed. One X
connection serves the module, opened on first use from $DISPLAY and guarded by one lock: the
capture and link threads call in alongside the GUI thread's full_screen_app, and a python-xlib
connection is not safe to share across threads."""

import threading
from contextlib import contextmanager
from types import SimpleNamespace
from typing import List, Optional, Tuple

from core.return_edge import Rect

_lock = threading.Lock()
_conn = None

# Core pointer-state mask bits; X has none for the back and forward buttons.
_BUTTON_MASKS = {"left": 1 << 8, "middle": 1 << 9, "right": 1 << 10}
# Xlib.X.AnyPropertyType, spelled out so the property reads need no import.
_ANY_PROPERTY_TYPE = 0


def _connect():
    from Xlib import display, error

    opened = display.Display()
    return SimpleNamespace(
        display=opened,
        root=opened.screen().root,
        x_error=error.XError,
        closed=(error.ConnectionClosedError, OSError),
    )


def _drop() -> None:
    global _conn
    dead, _conn = _conn, None
    try:
        dead.display.close()
    except Exception:
        pass


def _reset() -> None:
    with _lock:
        if _conn is not None:
            _drop()


@contextmanager
def _session():
    global _conn
    with _lock:
        if _conn is None:
            _conn = _connect()
        conn = _conn
        try:
            yield conn
        except conn.closed:
            # A dead socket never recovers, so the next call reconnects; this one reports it.
            _drop()
            raise


def monitors() -> List[Rect]:
    with _session() as conn:
        try:
            if conn.display.has_extension("RANDR"):
                version = conn.display.xrandr_query_version()
                if (version.major_version, version.minor_version) >= (1, 5):
                    found = [
                        Rect(m.x, m.y, m.width_in_pixels, m.height_in_pixels)
                        for m in conn.root.xrandr_get_monitors(is_active=True).monitors
                    ]
                    if found:
                        return found
        except conn.x_error:
            pass
        geometry = conn.root.get_geometry()
        return [Rect(0, 0, geometry.width, geometry.height)]


def cursor_position() -> Tuple[int, int]:
    with _session() as conn:
        pointer = conn.root.query_pointer()
        return pointer.root_x, pointer.root_y


def set_cursor_position(x: int, y: int) -> None:
    """Called on every pointer move while input is away, so one warp and one flush, no round trip.
    XWarpPointer produces no XInput2 raw motion, which is what keeps the sender's pin from
    feeding itself back."""
    with _session() as conn:
        conn.root.warp_pointer(int(x), int(y))
        conn.display.flush()


def button_down(name: str) -> bool:
    mask = _BUTTON_MASKS.get(name)
    if mask is None:
        return False
    with _session() as conn:
        return bool(conn.root.query_pointer().mask & mask)


def full_screen_app() -> Optional[str]:
    """The name of the foreground app while it is full screen, else None. GUI thread, once a
    second, as the Mac checks."""
    with _session() as conn:
        display = conn.display
        try:
            active = conn.root.get_full_property(display.intern_atom("_NET_ACTIVE_WINDOW"), _ANY_PROPERTY_TYPE)
            if active is None or not len(active.value) or not active.value[0]:
                return None
            window = display.create_resource_object("window", active.value[0])
            state = window.get_full_property(display.intern_atom("_NET_WM_STATE"), _ANY_PROPERTY_TYPE)
            if state is None or display.intern_atom("_NET_WM_STATE_FULLSCREEN") not in state.value:
                return None
            wm_class = window.get_wm_class()
        except conn.x_error:
            # The window can vanish between the two reads.
            return None
        return (wm_class[1] if wm_class else "") or "An app"
