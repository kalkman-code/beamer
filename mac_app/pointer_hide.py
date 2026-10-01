"""Hide the Mac's pointer while it drives the other machine.

With the mouse dissociated (bridge._set_cursor_follows_mouse) the pointer sits pinned where it
crossed, a sliver at a corner. Hiding it needs two things: CGDisplayHideCursor, which only works
for a process that is frontmost unless the connection is told otherwise, hence the private
CGSSetConnectionProperty call. Hide and show are counted by the window server, so this keeps one
flag and calls each once per redirect: a second hide would need a second show.

The hide belongs to this process's connection to the window server, so if Beamer dies while the
pointer is hidden the server gives it back with the connection. A normal exit shows it on the way
out (atexit, and the controller's stop).
"""

import atexit
import ctypes
import logging
import threading

try:
    import Quartz
except ImportError:  # pragma: no cover - exercised only off macOS
    Quartz = None

_CORE_GRAPHICS = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
_CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_UTF8 = 0x08000100


def allow_background_hiding() -> None:
    """Tell the window server this connection may set the cursor while another app is frontmost."""
    cf = ctypes.CDLL(_CORE_FOUNDATION)
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    key = cf.CFStringCreateWithCString(None, b"SetsCursorInBackground", _UTF8)
    true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue").value
    cg = ctypes.CDLL(_CORE_GRAPHICS)
    cg._CGSDefaultConnection.restype = ctypes.c_int
    cg.CGSSetConnectionProperty.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
    cg.CGSSetConnectionProperty.restype = ctypes.c_int
    connection = cg._CGSDefaultConnection()
    status = cg.CGSSetConnectionProperty(connection, connection, key, true)
    if status != 0:
        raise OSError(f"CGSSetConnectionProperty returned {status}")


class PointerHider:
    def __init__(self, quartz=Quartz, background=None, logger=None):
        self.quartz = quartz
        # The private call is for the real window server only; a fake Quartz gets a no-op.
        self.background = background or (allow_background_hiding if quartz is Quartz else (lambda: None))
        self.logger = logger or logging.getLogger("Beamer")
        self._lock = threading.Lock()
        self._hidden = False
        self._background_tried = False
        atexit.register(self.show)

    def hide(self) -> None:
        with self._lock:
            if self._hidden:
                return
            if not self._background_tried:
                self._background_tried = True
                try:
                    self.background()
                except Exception:
                    self.logger.exception("could not let the pointer hide while another app is frontmost")
            try:
                status = self.quartz.CGDisplayHideCursor(self.quartz.CGMainDisplayID())
            except Exception:
                self.logger.exception("could not hide the pointer")
                return
            if status == 0:
                self._hidden = True
            else:
                self.logger.warning("the system would not hide the pointer (%s)", status)

    def show(self) -> None:
        with self._lock:
            if not self._hidden:
                return
            self._hidden = False
            try:
                self.quartz.CGDisplayShowCursor(self.quartz.CGMainDisplayID())
            except Exception:
                self.logger.exception("could not show the pointer")
