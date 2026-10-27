"""Select only installed RU/EN layouts for the foreground Windows application."""

import ctypes
import logging
import sys
import time

LOGGER = logging.getLogger(__name__)
user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None

if user32 is not None:
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    user32.GetKeyboardLayout.argtypes = [ctypes.c_ulong]
    user32.GetKeyboardLayout.restype = ctypes.c_void_p
    user32.GetKeyboardLayoutList.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]


def foreground_layout():
    if user32 is None:
        return None
    window = user32.GetForegroundWindow()
    thread = user32.GetWindowThreadProcessId(window, None) if window else 0
    return user32.GetKeyboardLayout(thread)


def language_of(layout):
    primary = int(layout or 0) & 0x3ff
    return {0x09: "en", 0x19: "ru"}.get(primary)


def current_language():
    return language_of(foreground_layout())


def select_language(language):
    if user32 is None or language not in {"en", "ru"}:
        return False
    window = user32.GetForegroundWindow()
    if not window:
        return False
    thread = user32.GetWindowThreadProcessId(window, None)
    if language_of(user32.GetKeyboardLayout(thread)) == language:
        return True
    count = user32.GetKeyboardLayoutList(0, None)
    if count <= 0 or count > 256:
        return False
    values = (ctypes.c_void_p * count)()
    actual = user32.GetKeyboardLayoutList(count, values)
    matches = [value for value in values[:actual] if language_of(value) == language]
    if not matches:
        LOGGER.warning("No installed keyboard layout for %s; leaving the active layout unchanged", language)
        return False
    # WM_INPUTLANGCHANGEREQUEST is posted to the focused application, not to
    # Beamer's worker thread. Applications may refuse the request.
    if not user32.PostMessageW(window, 0x0050, 0, matches[0]):
        return False
    deadline = time.monotonic() + 0.25
    while time.monotonic() < deadline:
        if user32.GetForegroundWindow() != window:
            return False
        if language_of(user32.GetKeyboardLayout(thread)) == language:
            return True
        time.sleep(0.005)
    LOGGER.warning("The foreground application did not accept the keyboard language %s", language)
    return False
