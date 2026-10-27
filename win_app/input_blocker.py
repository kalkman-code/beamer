"""Read-only diagnostics for the foreground window when Windows refuses input."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import ntpath
import sys

from core.i18n import tr


DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20
TOKEN_INTEGRITY_LEVEL = 25
SECURITY_MANDATORY_SYSTEM_RID = 0x4000
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_INVALID_DATA = 13
GAMEINPUT_WINDOW_CLASS = "GameInputServiceWindow"


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", DWORD)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


def _configure_apis(user, kernel, security) -> None:
    user.GetForegroundWindow.argtypes = []
    user.GetForegroundWindow.restype = HANDLE
    user.GetClassNameW.argtypes = [HANDLE, ctypes.c_wchar_p, ctypes.c_int]
    user.GetClassNameW.restype = ctypes.c_int
    user.GetWindowThreadProcessId.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
    user.GetWindowThreadProcessId.restype = DWORD
    kernel.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
    kernel.OpenProcess.restype = HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [HANDLE, DWORD, ctypes.c_wchar_p, ctypes.POINTER(DWORD)]
    kernel.QueryFullProcessImageNameW.restype = BOOL
    kernel.CloseHandle.argtypes = [HANDLE]
    kernel.CloseHandle.restype = BOOL
    security.OpenProcessToken.argtypes = [HANDLE, DWORD, ctypes.POINTER(HANDLE)]
    security.OpenProcessToken.restype = BOOL
    security.GetTokenInformation.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD)]
    security.GetTokenInformation.restype = BOOL
    security.IsValidSid.argtypes = [ctypes.c_void_p]
    security.IsValidSid.restype = BOOL
    security.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    security.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    security.GetSidSubAuthority.argtypes = [ctypes.c_void_p, DWORD]
    security.GetSidSubAuthority.restype = ctypes.POINTER(DWORD)


user32 = kernel32 = advapi32 = None
if sys.platform == "win32":
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        _configure_apis(user32, kernel32, advapi32)
    except (OSError, AttributeError):
        user32 = kernel32 = advapi32 = None


@dataclass(frozen=True)
class ForegroundBlocker:
    hwnd: int = 0
    pid: int = 0
    window_class: str = ""
    process_name: str = ""
    elevated: bool | None = None
    integrity: int | None = None
    query_error: int | None = None

    @property
    def key(self) -> tuple[int, int, str]:
        return self.pid, self.hwnd, self.window_class

    def message(self, *, receiver_elevated: bool = False) -> str:
        app = self.process_name or self.window_class or tr("the foreground window")
        if self.window_class == GAMEINPUT_WINDOW_CLASS:
            return tr("Windows blocked Beamer's input because {app} is GameInput's system window. Click a normal window on this PC to continue.").format(app=app)
        if self.integrity is not None and self.integrity >= SECURITY_MANDATORY_SYSTEM_RID:
            return tr("Windows blocked Beamer's input because {app} is a system-level window. Use it locally and click a normal window on this PC to continue.").format(app=app)
        if self.elevated is True and not receiver_elevated:
            return tr("Windows blocked Beamer's input because {app} is running as administrator. Click a normal window on this PC to continue.").format(app=app)
        if self.query_error is not None:
            return tr("Windows blocked Beamer's input while {app} was in front. Windows did not allow Beamer to inspect this window's process. Click a normal window on this PC to continue.").format(app=app)
        return tr("Windows blocked Beamer's input while {app} was in front. Click a normal window on this PC to continue.").format(app=app)


def _last_error() -> int:
    return ctypes.get_last_error()


def _integrity_level(token) -> tuple[int | None, int | None]:
    size = DWORD()
    if not advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size)):
        error = _last_error()
        if error != ERROR_INSUFFICIENT_BUFFER:
            return None, error
    if size.value < ctypes.sizeof(TOKEN_MANDATORY_LABEL):
        return None, ERROR_INVALID_DATA
    buffer = ctypes.create_string_buffer(size.value)
    capacity = size.value
    if not advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buffer, capacity, ctypes.byref(size)):
        return None, _last_error()
    if not ctypes.sizeof(TOKEN_MANDATORY_LABEL) <= size.value <= capacity:
        return None, ERROR_INVALID_DATA
    sid = ctypes.cast(buffer, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents.Label.Sid
    if not sid or not advapi32.IsValidSid(sid):
        return None, ERROR_INVALID_DATA
    count = advapi32.GetSidSubAuthorityCount(sid)
    if not count or not count[0]:
        return None, ERROR_INVALID_DATA
    rid = advapi32.GetSidSubAuthority(sid, count[0] - 1)
    return (int(rid[0]), None) if rid else (None, ERROR_INVALID_DATA)


def foreground_blocker() -> ForegroundBlocker:
    """Keep inaccessible processes uncertain; access denial alone does not prove elevation."""
    if user32 is None or kernel32 is None or advapi32 is None:
        return ForegroundBlocker()
    values = {}
    process = None
    token = HANDLE()
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ForegroundBlocker()
        values["hwnd"] = int(hwnd)
        window_class = ctypes.create_unicode_buffer(256)
        if user32.GetClassNameW(hwnd, window_class, len(window_class)):
            values["window_class"] = window_class.value
        else:
            values["query_error"] = _last_error()
        pid = DWORD()
        if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or not pid.value:
            values.setdefault("query_error", _last_error())
            return ForegroundBlocker(**values)
        values["pid"] = pid.value
        process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not process:
            values.setdefault("query_error", _last_error())
            return ForegroundBlocker(**values)
        image = ctypes.create_unicode_buffer(32768)
        size = DWORD(len(image))
        if kernel32.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size)):
            values["process_name"] = ntpath.basename(image.value)
        else:
            values.setdefault("query_error", _last_error())
        if not advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
            values.setdefault("query_error", _last_error())
            return ForegroundBlocker(**values)
        elevation = DWORD()
        size = DWORD()
        if advapi32.GetTokenInformation(token.value, TOKEN_ELEVATION, ctypes.byref(elevation),
                                       ctypes.sizeof(elevation), ctypes.byref(size)):
            if size.value == ctypes.sizeof(elevation):
                values["elevated"] = bool(elevation.value)
            else:
                values.setdefault("query_error", ERROR_INVALID_DATA)
        else:
            values.setdefault("query_error", _last_error())
        values["integrity"], error = _integrity_level(token.value)
        if error is not None:
            values.setdefault("query_error", error)
    except OSError as exc:
        values.setdefault("query_error", getattr(exc, "winerror", None) or exc.errno or _last_error())
    finally:
        if token.value:
            kernel32.CloseHandle(token.value)
        if process:
            kernel32.CloseHandle(process)
    return ForegroundBlocker(**values)
