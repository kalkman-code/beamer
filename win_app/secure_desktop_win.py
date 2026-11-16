"""Win32 boundary for the optional secure-input worker; no GUI or capture hooks."""

import ctypes as c
from ctypes import wintypes as w
import threading
import time

import input_injector as keys

INPUT = keys.INPUT
SERVICE_NAME = "BeamerSecureDesktop"
SECURE_PORT = 24822
u32 = c.WinDLL("user32", use_last_error=True)
k32 = c.WinDLL("kernel32", use_last_error=True)
a32 = c.WinDLL("advapi32", use_last_error=True)


def signature(dll, name, result, *args):
    function = getattr(dll, name)
    function.restype, function.argtypes = result, list(args)
    return function


signature(u32, "OpenInputDesktop", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
signature(u32, "OpenDesktopW", w.HANDLE, w.LPCWSTR, w.DWORD, w.BOOL, w.DWORD)
signature(u32, "CloseDesktop", w.BOOL, w.HANDLE)
signature(u32, "GetUserObjectInformationW", w.BOOL, w.HANDLE, c.c_int, w.LPVOID, w.DWORD, c.POINTER(w.DWORD))
signature(u32, "SetThreadDesktop", w.BOOL, w.HANDLE)
signature(u32, "GetThreadDesktop", w.HANDLE, w.DWORD)
signature(k32, "GetCurrentThreadId", w.DWORD)
signature(k32, "GetCurrentProcessId", w.DWORD)
signature(k32, "ProcessIdToSessionId", w.BOOL, w.DWORD, c.POINTER(w.DWORD))
signature(k32, "WTSGetActiveConsoleSessionId", w.DWORD)
signature(k32, "CloseHandle", w.BOOL, w.HANDLE)
signature(a32, "OpenSCManagerW", w.HANDLE, w.LPCWSTR, w.LPCWSTR, w.DWORD)
signature(a32, "OpenServiceW", w.HANDLE, w.HANDLE, w.LPCWSTR, w.DWORD)
signature(a32, "QueryServiceStatus", w.BOOL, w.HANDLE, w.LPVOID)
signature(a32, "CloseServiceHandle", w.BOOL, w.HANDLE)


def current_session():
    session = w.DWORD()
    if not k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), c.byref(session)):
        raise c.WinError(c.get_last_error())
    return session.value


def active_console_session():
    return k32.WTSGetActiveConsoleSessionId()


def desktop_name(handle):
    name, needed = c.create_unicode_buffer(256), w.DWORD()
    if not handle or not u32.GetUserObjectInformationW(handle, 2, name, c.sizeof(name), c.byref(needed)):
        return None
    return name.value


def input_desktop_name():
    handle = u32.OpenInputDesktop(0, False, 1)
    if not handle:
        return None
    try:
        return desktop_name(handle)
    finally:
        u32.CloseDesktop(handle)


def secure_desktop_matches(session, console, name):
    return session > 0 and session == console and console != 0xFFFFFFFF and name == "Winlogon"


def secure_desktop_available():
    try:
        return secure_desktop_matches(current_session(), active_console_session(), input_desktop_name())
    except OSError:
        return False


def service_running():
    manager = a32.OpenSCManagerW(None, None, 1)
    if not manager:
        return None
    service = None
    try:
        service = a32.OpenServiceW(manager, SERVICE_NAME, 4)
        if not service:
            return False if c.get_last_error() == 1060 else None
        status = (w.DWORD * 7)()
        return status[1] == 4 if a32.QueryServiceStatus(service, status) else None
    finally:
        if service:
            a32.CloseServiceHandle(service)
        a32.CloseServiceHandle(manager)


# The responder asks twice per input event and once per message it sends. `service_running` is
# three Service Control Manager calls, an RPC to services.exe at 70 us a call on the rig
# (28-10-2026), and whether the service exists changes only on install; the desktop name, which
# changes on lock and UAC, stays a 1 us call made every time the service is present. With no
# service the desktop is not read at all, as before the service existed; the one cost is that a
# service installed under 2 s ago is not seen yet.
SERVICE_CHECK_SECONDS = 2.0
_service_seen = [float("-inf"), None]


def _service_state(clock=time.monotonic):
    now = clock()
    if now - _service_seen[0] >= SERVICE_CHECK_SECONDS:
        _service_seen[:] = [now, service_running()]
    return _service_seen[1]


def normal_desktop_available(clock=time.monotonic):
    return _service_state(clock) is False or input_desktop_name() == "Default"


# Manage-SecureDesktop.ps1 Grant writes the approved peer IDs (never tokens) here as an
# administrator-only value that any user can read; Disable and Remove delete it. The grants
# themselves stay unreadable to the signed-in user, and the service still refuses every peer not in
# them, so a stale or forged entry here can only keep an owner at a locked PC, never admit anyone.
APPROVED_KEY = r"SOFTWARE\Beamer\SecureDesktop"
APPROVED_VALUE = "ApprovedPeers"


def approved_peer_ids(read=None):
    if read is None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, APPROVED_KEY, 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                value, kind = winreg.QueryValueEx(key, APPROVED_VALUE)
        except OSError:
            return frozenset()
        if kind != winreg.REG_MULTI_SZ:
            return frozenset()
    else:
        value = read()
    return frozenset(item.strip() for item in value if isinstance(item, str) and item.strip())


def hands_over_to_service(peer_id, clock=time.monotonic, approved=approved_peer_ids):
    """True while the service runs and this peer is approved for it."""
    from core import protocol
    return _service_state(clock) is True and protocol.id_text(peer_id) in approved()


def bind_secure_desktop():
    if not secure_desktop_available():
        return False
    # The worker starts on Winlogon; new Python threads can inherit another desktop.
    original = u32.GetThreadDesktop(k32.GetCurrentThreadId())
    if desktop_name(original) == "Winlogon":
        return True
    return False


class SecureInjector:
    def __init__(self, bind=bind_secure_desktop, send=None):
        self._bind = bind
        self._send = send or (lambda inputs: keys._send_input(*inputs))
        self._held = {}
        self._mods, self._chars = set(), {}
        self._scroll = [0.0, 0.0]
        self._lock = threading.RLock()

    def _emit(self, inputs):
        if not self._bind():
            raise PermissionError("Secure input desktop is unavailable")
        if inputs:
            self._send(inputs)

    def inject_key(self, name, down, us=None):
        with self._lock:
            tag = ("key", name)
            mods = set(self._mods)
            if not down and tag in self._held:
                inputs = self._held[tag]
                chars = dict(self._chars)
                keys.plan_key_inputs(name, False, mods, chars,
                                     keys._vk_key_scan, keys._map_virtual_key, us, keys._key_at)
                self._emit(inputs)
                self._mods = mods
                self._chars = chars
                self._held.pop(tag)
                return
            chars = dict(self._chars)
            plan = keys.plan_key_inputs(name, down, mods, chars,
                                        keys._vk_key_scan, keys._map_virtual_key, us, keys._key_at)
            inputs = [keys._keybd_input(*item) for item in plan]
            self._emit(inputs)
            self._mods = mods
            self._chars = chars
            if down and inputs:
                self._held[tag] = [keys._keybd_input(vk, scan, flags | keys.KEYEVENTF_KEYUP)
                                   for vk, scan, flags in plan]

    def inject_mouse_move(self, dx, dy):
        with self._lock:
            self._emit([keys._mouse_input(dx, dy, 0, keys.MOUSEEVENTF_MOVE)])

    def inject_mouse_button(self, button, down):
        with self._lock:
            flags = (keys.BUTTON_DOWN_FLAGS if down else keys.BUTTON_UP_FLAGS).get(button)
            if flags is None:
                return
            data = keys.BUTTON_DATA.get(button, 0)
            self._emit([keys._mouse_input(0, 0, data, flags)])
            if down:
                self._held[("button", button)] = [keys._mouse_input(0, 0, data, keys.BUTTON_UP_FLAGS[button])]
            else:
                self._held.pop(("button", button), None)

    def inject_scroll(self, dy, dx=0.0, mode="line"):
        with self._lock:
            y, x = keys.plan_scroll_units(dy, dx, mode, self._scroll)
            inputs = []
            if y:
                inputs.append(keys._mouse_input(0, 0, y, keys.MOUSEEVENTF_WHEEL))
            if x:
                inputs.append(keys._mouse_input(0, 0, x, keys.MOUSEEVENTF_HWHEEL))
            self._emit(inputs)

    def release_all(self):
        with self._lock:
            if self._bind():
                for tag, inputs in list(self._held.items()):
                    self._emit(inputs)
                    self._held.pop(tag, None)
                self._mods.clear()
                self._chars.clear()
                self._scroll[:] = [0.0, 0.0]
            else:
                # Never replay key releases onto the next desktop.
                self._held.clear()
                self._mods.clear()
                self._chars.clear()
                self._scroll[:] = [0.0, 0.0]
