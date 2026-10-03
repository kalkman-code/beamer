"""Win32 pointer and monitor geometry, imported lazily by the receiver so the
crossing logic stays testable without ctypes.windll. GetCursorPos, SetCursorPos
and EnumDisplayMonitors share one coordinate space — virtual-desktop pixels,
DPI-virtualised identically for this process — so values from one feed the
others unconverted."""

import ctypes
import logging
import os
import sys
from typing import List, Tuple

from core.return_edge import Rect

_IS_WINDOWS = sys.platform == "win32"
user32 = ctypes.WinDLL("user32", use_last_error=True) if _IS_WINDOWS else None
LOGGER = logging.getLogger(__name__)

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


if _IS_WINDOWS:
    MONITORENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(RECT), ctypes.c_ssize_t)
    user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.EnumDisplayMonitors.argtypes = [ctypes.c_void_p, ctypes.c_void_p, MONITORENUMPROC, ctypes.c_ssize_t]
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
    user32.MonitorFromWindow.restype = ctypes.c_void_p
    user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


def _require() -> None:
    if user32 is None:
        raise RuntimeError("Win32 desktop geometry is only available on Windows")


def monitors() -> List[Rect]:
    _require()
    found: List[Rect] = []

    def collect(_monitor, _dc, rect, _lparam):
        r = rect.contents
        found.append(Rect(r.left, r.top, r.right - r.left, r.bottom - r.top))
        return 1

    if not user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(collect), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return found


def cursor_position() -> Tuple[int, int]:
    _require()
    point = POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return point.x, point.y


_refused_under = [None]


def set_cursor_position(x: int, y: int) -> None:
    """SetCursorPos returns FALSE with no error code, and SendInput is dropped
    in silence, whenever Windows' UIPI puts the foreground window above
    Beamer's integrity level, so a SendInput move is no fallback. The one such
    window seen so far is GameInput's (see input_injector); for it the
    placement runs again once the service has been restarted, and anything
    else in front is named in the log."""
    _require()
    x, y = int(x), int(y)
    if user32.SetCursorPos(x, y):
        _refused_under[0] = None
        return
    error = ctypes.get_last_error()
    import input_injector

    # Placed through this function again, so a refusal after the restart is logged too.
    if input_injector.release_gameinput_foreground(after=lambda: set_cursor_position(x, y)):
        return
    blocker = input_injector.foreground_class()
    # A hold at the return edge lands here on every move, so once per blocker.
    if blocker != _refused_under[0]:
        _refused_under[0] = blocker
        LOGGER.warning(
            "SetCursorPos refused (error %s) with %r in the foreground; Windows refuses Beamer's input "
            "while a window above its integrity level is in front, until a click on this PC",
            error, blocker,
        )


# What SHQueryUserNotificationState says while something owns the whole screen. 3 is a Direct3D
# exclusive full-screen game and 4 is Presentation Settings, both unambiguous. 2, QUNS_BUSY, is
# also what a borderless game or a browser on F11 gives, but overlays such as NVIDIA's raise it
# with nothing full screen at all (PowerToys PR #45891 split it out of its own guard for that), so
# it counts only with the foreground window covering its whole monitor. A maximised window stops
# at the taskbar and never does, which is what keeps this from becoming the size test the Mac
# learned catches every zoomed window.
_EXCLUSIVE_STATES = {3, 4}
QUNS_BUSY = 2
# The desktop and the taskbar are monitor-sized and foreground whenever the desktop is clicked.
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def full_screen_app():
    """The name of the foreground app while it is full screen, else None. GUI thread, once a
    second, as the Mac checks."""
    _require()
    pid = _foreground_process_id()
    if pid is None or pid == os.getpid():
        return None
    state = ctypes.c_int(0)
    if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
        return None
    if state.value in _EXCLUSIVE_STATES or (state.value == QUNS_BUSY and _foreground_covers_its_monitor()):
        return _foreground_app_name() or "An app"
    return None


def _foreground_process_id():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]


def _foreground_covers_its_monitor() -> bool:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    name = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, name, 64)
    if name.value in _SHELL_CLASSES:
        return False
    window = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(window)):
        return False
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return False
    screen = info.rcMonitor
    return (window.left, window.top, window.right, window.bottom) == (
        screen.left, screen.top, screen.right, screen.bottom
    )


# The hook reports logical buttons; GetAsyncKeyState reads physical ones, so with the buttons
# swapped in Settings the left and right virtual keys trade places.
_BUTTON_VKS = {"left": 0x01, "right": 0x02, "middle": 0x04, "back": 0x05, "forward": 0x06}


def button_down(name: str) -> bool:
    _require()
    vk = _BUTTON_VKS.get(name)
    if vk is None:
        return False
    if vk in (0x01, 0x02) and user32.GetSystemMetrics(23):  # SM_SWAPBUTTON
        vk = 0x03 - vk
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def _foreground_app_name():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle =kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        size = ctypes.c_ulong(260)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        name = buffer.value.rsplit("\\", 1)[-1]
        return name[:-4] if name.lower().endswith(".exe") else name
    finally:
        kernel32.CloseHandle(handle)
