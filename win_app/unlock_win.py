"""Unlock a locked Windows console so injected input has somewhere to land.

The ordinary user-session receiver cannot type on the lock screen: that is the Winlogon
secure desktop, whose security descriptor admits only SYSTEM, and SendInput
only reaches the desktop its calling thread is attached to. Running elevated
clears UIPI, not desktop access -- which is why an admin Beamer still cannot
touch a locked machine.

The optional secure-desktop service uses a separate SYSTEM worker and explicit
administrator-approved pairing grants. It never calls this provider.

So this module does not inject into the lock screen. It asks an external
unlock credential provider to take the machine *off* the lock screen, after
which the console is back on the Default desktop and the ordinary
input_injector.py path works unchanged. That provider is optional and not
part of this repository.

The provider is a COM DLL that LogonUI loads as SYSTEM. It waits on an
auto-reset event and, when signalled, submits the password stored in a
machine-DPAPI blob. Its event's DACL grants Authenticated Users
EVENT_MODIFY_STATE, so an unelevated Beamer can signal it and no Beamer
process ever holds a password.

The provider owns the unlock; Beamer only knocks. Shipping a second
provider would put two waiters on one auto-reset event, which is a real way
to lose the signal. If the provider is not installed, every call here fails
cleanly and Beamer behaves as it did before.

Guarded like clipboard_win.py and input_injector.py so it stays importable,
and its orchestration testable, on a machine without ctypes.windll.
"""

import ctypes
import logging
import sys
import time
from typing import Optional


LOGGER = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if _IS_WINDOWS else None

# The name is fixed by the provider: Global\RoomDeckUnlock.<its own session
# id>. Global rather than Local because an RDP round trip leaves the
# provider and the console in different sessions; the session suffix
# restores the per-session isolation that stops a stale lock screen
# elsewhere swallowing the auto-reset signal.
UNLOCK_EVENT_PREFIX = "Global\\RoomDeckUnlock."

EVENT_MODIFY_STATE = 0x0002
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH = 260

# LogonUI.exe is the lock screen: it exists while the console is locked and
# not otherwise. The session table cannot answer this -- locking does not
# disconnect the console session, which stays Active throughout.
LOCK_SCREEN_PROCESS = "logonui.exe"

UNLOCK_TIMEOUT_SECONDS = 10.0
UNLOCK_POLL_SECONDS = 0.5


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


if _IS_WINDOWS:
    kernel32.OpenEventW.restype = ctypes.c_void_p
    kernel32.OpenEventW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.SetEvent.restype = ctypes.c_int
    kernel32.SetEvent.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.WTSGetActiveConsoleSessionId.restype = ctypes.c_ulong
    kernel32.WTSGetActiveConsoleSessionId.argtypes = []
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    kernel32.Process32FirstW.restype = ctypes.c_int
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = ctypes.c_int
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.ProcessIdToSessionId.restype = ctypes.c_int
    kernel32.ProcessIdToSessionId.argtypes = [ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]


def _running_processes(mem32) -> Optional[list]:
    """Every running process as (lowercased image name, PID), or None if the
    snapshot could not be taken. Enumeration only reads names and PIDs, so it
    needs no privilege over the processes it lists -- LogonUI runs as SYSTEM
    and still shows up."""
    snapshot = mem32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not mem32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return None
        processes = []
        while True:
            processes.append((entry.szExeFile.lower(), entry.th32ProcessID))
            if not mem32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
        return processes
    finally:
        mem32.CloseHandle(snapshot)


def _in_console_session(mem32, pid: int, console: int) -> bool:
    """Whether `pid` runs in the console's session. A LogonUI in another session -- a second
    account's lock screen, an RDP logon -- is not this console's lock screen. A session that
    cannot be read counts as the console's, as it did before this check existed."""
    session = ctypes.c_ulong()
    if not mem32.ProcessIdToSessionId(pid, ctypes.byref(session)):
        return True
    return session.value == console


def is_locked(mem32=None) -> Optional[bool]:
    """True if the console is sitting on the lock screen, False if it is not,
    None if that could not be determined. None is deliberately distinct from
    False: an unknown state must not be reported as unlocked, or the caller
    silently skips an unlock the machine actually needed."""
    mem32 = mem32 if mem32 is not None else kernel32
    if mem32 is None:
        return None
    try:
        processes = _running_processes(mem32)
        if processes is None:
            return None
        console = mem32.WTSGetActiveConsoleSessionId()
        return any(
            name == LOCK_SCREEN_PROCESS and _in_console_session(mem32, pid, console)
            for name, pid in processes
        )
    except Exception:
        LOGGER.exception("Could not enumerate processes to test for the lock screen")
        return None


def signal_unlock(mem32=None) -> bool:
    """Signal the provider's trigger event for the active console session.
    False means the event could not be opened, which normally means the
    provider is not registered on this machine."""
    mem32 = mem32 if mem32 is not None else kernel32
    if mem32 is None:
        return False
    name = f"{UNLOCK_EVENT_PREFIX}{mem32.WTSGetActiveConsoleSessionId()}"
    handle = mem32.OpenEventW(EVENT_MODIFY_STATE, False, name)
    if not handle:
        LOGGER.warning(
            "Could not open %s -- is the unlock provider registered?", name
        )
        return False
    try:
        return bool(mem32.SetEvent(handle))
    finally:
        mem32.CloseHandle(handle)


def ensure_unlocked(
    mem32=None,
    timeout: float = UNLOCK_TIMEOUT_SECONDS,
    poll: float = UNLOCK_POLL_SECONDS,
    sleep=time.sleep,
) -> bool:
    """Get the console off the lock screen so injected input can land.

    True means input has somewhere to go -- either the machine was already
    unlocked, or the provider unlocked it in time. False means it is still
    locked, so the caller should tell the user rather than inject into
    nothing.

    An unknown lock state is treated as unlocked: Beamer's existing behaviour
    is to inject and let it fail visibly, which is better than refusing to
    send input on a machine that was never locked."""
    locked = is_locked(mem32)
    if locked is not True:
        return True
    if not signal_unlock(mem32):
        return False
    deadline = timeout
    while deadline > 0:
        sleep(poll)
        deadline -= poll
        if is_locked(mem32) is False:
            LOGGER.info("Console unlocked after %.1fs", timeout - deadline)
            return True
    LOGGER.error(
        "Console still locked after %.0fs -- see the unlock provider's own log",
        timeout,
    )
    return False
