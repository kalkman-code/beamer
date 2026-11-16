"""LocalSystem SCM supervisor for the fixed secure-desktop worker executable."""

import ctypes as c
from ctypes import wintypes as w
from pathlib import Path
import re
import threading
import time
import uuid


SERVICE_NAME = "BeamerSecureDesktop"
k32 = c.WinDLL("kernel32", use_last_error=True)
a32 = c.WinDLL("advapi32", use_last_error=True)


class SECURITY_ATTRIBUTES(c.Structure):
    _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", w.LPVOID),
                ("bInheritHandle", w.BOOL)]


class STARTUPINFOW(c.Structure):
    _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR),
                ("lpTitle", w.LPWSTR), ("dwX", w.DWORD), ("dwY", w.DWORD),
                ("dwXSize", w.DWORD), ("dwYSize", w.DWORD),
                ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD),
                ("dwFillAttribute", w.DWORD), ("dwFlags", w.DWORD),
                ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                ("lpReserved2", c.POINTER(w.BYTE)), ("hStdInput", w.HANDLE),
                ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE)]


class PROCESS_INFORMATION(c.Structure):
    _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE),
                ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(c.Structure):
    _fields_ = [("PerProcessUserTimeLimit", c.c_longlong),
                ("PerJobUserTimeLimit", c.c_longlong), ("LimitFlags", w.DWORD),
                ("MinimumWorkingSetSize", c.c_size_t),
                ("MaximumWorkingSetSize", c.c_size_t), ("ActiveProcessLimit", w.DWORD),
                ("Affinity", c.c_size_t), ("PriorityClass", w.DWORD),
                ("SchedulingClass", w.DWORD)]


class IO_COUNTERS(c.Structure):
    _fields_ = [(name, c.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(c.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", c.c_size_t),
                ("JobMemoryLimit", c.c_size_t), ("PeakProcessMemoryUsed", c.c_size_t),
                ("PeakJobMemoryUsed", c.c_size_t)]


class LUID(c.Structure):
    _fields_ = [("LowPart", w.DWORD), ("HighPart", w.LONG)]


class LUID_AND_ATTRIBUTES(c.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", w.DWORD)]


class TOKEN_PRIVILEGES(c.Structure):
    _fields_ = [("PrivilegeCount", w.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


class SID_AND_ATTRIBUTES(c.Structure):
    _fields_ = [("Sid", w.LPVOID), ("Attributes", w.DWORD)]


class SERVICE_STATUS(c.Structure):
    _fields_ = [(name, w.DWORD) for name in (
        "dwServiceType", "dwCurrentState", "dwControlsAccepted", "dwWin32ExitCode",
        "dwServiceSpecificExitCode", "dwCheckPoint", "dwWaitHint")]


SERVICE_MAIN_FUNCTION = c.WINFUNCTYPE(None, w.DWORD, c.POINTER(w.LPWSTR))
HANDLER_FUNCTION = c.WINFUNCTYPE(w.DWORD, w.DWORD, w.DWORD, w.LPVOID, w.LPVOID)


class SERVICE_TABLE_ENTRYW(c.Structure):
    _fields_ = [("lpServiceName", w.LPWSTR), ("lpServiceProc", SERVICE_MAIN_FUNCTION)]


def _signature(dll, name, result, *args):
    function = getattr(dll, name)
    function.restype, function.argtypes = result, list(args)


_signature(k32, "GetCurrentProcess", w.HANDLE)
_signature(k32, "WTSGetActiveConsoleSessionId", w.DWORD)
_signature(k32, "CloseHandle", w.BOOL, w.HANDLE)
_signature(k32, "LocalFree", w.LPVOID, w.LPVOID)
_signature(k32, "CreateEventW", w.HANDLE, c.POINTER(SECURITY_ATTRIBUTES), w.BOOL, w.BOOL, w.LPCWSTR)
_signature(k32, "OpenEventW", w.HANDLE, w.DWORD, w.BOOL, w.LPCWSTR)
_signature(k32, "SetEvent", w.BOOL, w.HANDLE)
_signature(k32, "CreateJobObjectW", w.HANDLE, c.POINTER(SECURITY_ATTRIBUTES), w.LPCWSTR)
_signature(k32, "SetInformationJobObject", w.BOOL, w.HANDLE, c.c_int, w.LPVOID, w.DWORD)
_signature(k32, "AssignProcessToJobObject", w.BOOL, w.HANDLE, w.HANDLE)
_signature(k32, "TerminateJobObject", w.BOOL, w.HANDLE, w.UINT)
_signature(k32, "TerminateProcess", w.BOOL, w.HANDLE, w.UINT)
_signature(k32, "ResumeThread", w.DWORD, w.HANDLE)
_signature(k32, "WaitForSingleObject", w.DWORD, w.HANDLE, w.DWORD)
_signature(a32, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD, c.POINTER(w.HANDLE))
_signature(a32, "GetTokenInformation", w.BOOL, w.HANDLE, c.c_int, w.LPVOID, w.DWORD, c.POINTER(w.DWORD))
_signature(a32, "IsWellKnownSid", w.BOOL, w.LPVOID, c.c_int)
_signature(a32, "LookupPrivilegeValueW", w.BOOL, w.LPCWSTR, w.LPCWSTR, c.POINTER(LUID))
_signature(a32, "AdjustTokenPrivileges", w.BOOL, w.HANDLE, w.BOOL, c.POINTER(TOKEN_PRIVILEGES),
           w.DWORD, c.POINTER(TOKEN_PRIVILEGES), c.POINTER(w.DWORD))
_signature(a32, "DuplicateTokenEx", w.BOOL, w.HANDLE, w.DWORD, c.POINTER(SECURITY_ATTRIBUTES),
           c.c_int, c.c_int, c.POINTER(w.HANDLE))
_signature(a32, "SetTokenInformation", w.BOOL, w.HANDLE, c.c_int, w.LPVOID, w.DWORD)
_signature(a32, "CreateProcessAsUserW", w.BOOL, w.HANDLE, w.LPCWSTR, w.LPWSTR,
           c.POINTER(SECURITY_ATTRIBUTES), c.POINTER(SECURITY_ATTRIBUTES), w.BOOL,
           w.DWORD, w.LPVOID, w.LPCWSTR, c.POINTER(STARTUPINFOW), c.POINTER(PROCESS_INFORMATION))
_signature(a32, "ConvertStringSecurityDescriptorToSecurityDescriptorW", w.BOOL,
           w.LPCWSTR, w.DWORD, c.POINTER(w.LPVOID), c.POINTER(w.DWORD))
_signature(a32, "StartServiceCtrlDispatcherW", w.BOOL, c.POINTER(SERVICE_TABLE_ENTRYW))
_signature(a32, "RegisterServiceCtrlHandlerExW", w.HANDLE, w.LPCWSTR, HANDLER_FUNCTION, w.LPVOID)
_signature(a32, "SetServiceStatus", w.BOOL, w.HANDLE, c.POINTER(SERVICE_STATUS))


def _error():
    return c.WinError(c.get_last_error() or 1)


def is_system() -> bool:
    """Read the current process identity; administrator membership is insufficient."""
    token = w.HANDLE()
    try:
        if not a32.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, c.byref(token)):
            return False
        needed = w.DWORD()
        a32.GetTokenInformation(token, 1, None, 0, c.byref(needed))
        if needed.value < c.sizeof(SID_AND_ATTRIBUTES):
            return False
        buffer = c.create_string_buffer(needed.value)
        if not a32.GetTokenInformation(token, 1, buffer, len(buffer), c.byref(needed)):
            return False
        user = c.cast(buffer, c.POINTER(SID_AND_ATTRIBUTES)).contents
        return bool(user.Sid and a32.IsWellKnownSid(user.Sid, 22))
    except OSError:
        return False
    finally:
        if token.value:
            k32.CloseHandle(token.value)


def _enable_privileges(token):
    for name in ("SeTcbPrivilege", "SeAssignPrimaryTokenPrivilege", "SeIncreaseQuotaPrivilege"):
        privileges = TOKEN_PRIVILEGES()
        privileges.PrivilegeCount = 1
        privileges.Privileges[0].Attributes = 2
        if not a32.LookupPrivilegeValueW(None, name, c.byref(privileges.Privileges[0].Luid)):
            raise _error()
        c.set_last_error(0)
        if not a32.AdjustTokenPrivileges(token, False, c.byref(privileges), 0, None, None):
            raise _error()
        if c.get_last_error():
            raise _error()


def _create_stop_event():
    name = "Global\\BeamerSecureStop-" + uuid.uuid4().hex
    descriptor = w.LPVOID()
    if not a32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        "O:SYG:SYD:P(A;;GA;;;SY)(A;;GA;;;BA)", 1, c.byref(descriptor), None
    ):
        raise _error()
    try:
        attributes = SECURITY_ATTRIBUTES(c.sizeof(SECURITY_ATTRIBUTES), descriptor, False)
        c.set_last_error(0)
        event = k32.CreateEventW(c.byref(attributes), True, False, name)
        error = c.get_last_error()
        if not event:
            raise c.WinError(error or 1)
        if error == 183:
            k32.CloseHandle(event)
            raise c.WinError(error)
        return event, name
    finally:
        k32.LocalFree(descriptor)


class _StopEvent:
    def __init__(self, handle):
        self.handle = handle

    def is_set(self):
        result = k32.WaitForSingleObject(self.handle, 0)
        if result not in (0, 258):
            raise _error()
        return result == 0

    def close(self):
        if self.handle:
            k32.CloseHandle(self.handle)
            self.handle = None


def open_worker_stop_event(name: str):
    """Open the service-owned lifecycle event with no permission to signal it."""
    if not isinstance(name, str) or not re.fullmatch(r"Global\\BeamerSecureStop-[0-9a-f]{32}", name):
        raise ValueError("Invalid service lifecycle event")
    handle = k32.OpenEventW(0x00100000, False, name)
    if not handle:
        raise _error()
    return _StopEvent(handle)


class _WorkerCleanupError(OSError):
    pass


class _Worker:
    def __init__(self, job, process, stop_event):
        self.job, self.process, self.stop_event = job, process, stop_event

    def exited(self):
        result = k32.WaitForSingleObject(self.process, 0)
        if result not in (0, 258):
            raise _error()
        return result == 0

    def close(self):
        if not self.process:
            return
        job, process, event = self.job, self.process, self.stop_event
        self.job = self.process = self.stop_event = None
        try:
            signalled = k32.SetEvent(event)
            result = k32.WaitForSingleObject(process, 2000) if signalled else 258
            if result != 0:
                k32.TerminateJobObject(job, 0)
            # Kill-on-close also covers descendants and a failed explicit termination.
            k32.CloseHandle(job)
            job = None
            if result != 0 and k32.WaitForSingleObject(process, 5000) != 0:
                raise OSError(1460, "Secure worker termination was not confirmed")
        finally:
            if job:
                k32.CloseHandle(job)
            k32.CloseHandle(process)
            k32.CloseHandle(event)


def _launch_worker(executable: Path, session: int):
    if session <= 0 or session == 0xFFFFFFFF:
        raise ValueError("No active console session")
    own, primary = w.HANDLE(), w.HANDLE()
    child = PROCESS_INFORMATION()
    job = event = None
    assigned = False
    launched = False
    try:
        if not a32.OpenProcessToken(k32.GetCurrentProcess(), 0x002B, c.byref(own)):
            raise _error()
        _enable_privileges(own)
        if not a32.DuplicateTokenEx(own, 0x018B, None, 2, 1, c.byref(primary)):
            raise _error()
        target = w.DWORD(session)
        if not a32.SetTokenInformation(primary, 12, c.byref(target), c.sizeof(target)):
            raise _error()
        event, event_name = _create_stop_event()
        job = k32.CreateJobObjectW(None, None)
        if not job:
            raise _error()
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = 0x2000
        if not k32.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)):
            raise _error()
        startup = STARTUPINFOW()
        startup.cb, startup.lpDesktop = c.sizeof(startup), "winsta0\\winlogon"
        command = c.create_unicode_buffer(f'"{executable}" --worker --stop-event "{event_name}"')
        if not a32.CreateProcessAsUserW(primary, str(executable), command, None, None, False,
                                      0x08000004, None, str(executable.parent),
                                      c.byref(startup), c.byref(child)):
            raise _error()
        if not k32.AssignProcessToJobObject(job, child.hProcess):
            raise _error()
        assigned = True
        if k32.ResumeThread(child.hThread) == 0xFFFFFFFF:
            raise _error()
        launched = True
        return _Worker(job, child.hProcess, event)
    finally:
        if child.hThread:
            k32.CloseHandle(child.hThread)
        if primary.value:
            k32.CloseHandle(primary.value)
        if own.value:
            k32.CloseHandle(own.value)
        if not launched:
            terminated = True
            if child.hProcess:
                if assigned:
                    k32.TerminateJobObject(job, 1)
                else:
                    # Only our not-yet-runnable child exists outside the job on this path.
                    k32.TerminateProcess(child.hProcess, 1)
            if job:
                k32.CloseHandle(job)
            if child.hProcess:
                terminated = k32.WaitForSingleObject(child.hProcess, 5000) == 0
                k32.CloseHandle(child.hProcess)
            if event:
                k32.CloseHandle(event)
            if not terminated:
                raise _WorkerCleanupError(1460, "Secure worker termination was not confirmed")


class _ServiceHost:
    def __init__(self, executable):
        self.executable = executable
        self.stop = threading.Event()
        self.status_handle = None
        self.exit_code = 0
        self.state = 2
        self.checkpoint = 0
        self.status_lock = threading.RLock()
        self.main_callback = SERVICE_MAIN_FUNCTION(self._main)
        self.control_callback = HANDLER_FUNCTION(self._control)

    def _status(self, state, error=0):
        with self.status_lock:
            self.state = state
            pending = state in (2, 3)
            self.checkpoint = self.checkpoint + 1 if pending else 0
            status = SERVICE_STATUS(0x10, state, 5 if state == 4 else 0, error, 0,
                                    self.checkpoint, 10000 if pending else 0)
            if not a32.SetServiceStatus(self.status_handle, c.byref(status)):
                raise _error()

    def _control(self, control, event_type, event_data, context):
        try:
            if control in (1, 5):
                self.stop.set()
                self._status(3)
            elif control == 4:
                self._status(self.state)
            else:
                return 120
            return 0
        except OSError:
            self.stop.set()
            return 1

    def _supervise(self):
        worker, session, retry_at = None, None, 0.0
        try:
            while not self.stop.is_set():
                console = k32.WTSGetActiveConsoleSessionId()
                now = time.monotonic()
                if console != session:
                    if worker:
                        old, worker = worker, None
                        old.close()
                    session, retry_at = console, 0.0
                if worker and worker.exited():
                    old, worker = worker, None
                    old.close()
                    retry_at = now + 2.0
                if not worker and console not in (0, 0xFFFFFFFF) and now >= retry_at and not self.stop.is_set():
                    try:
                        worker = _launch_worker(self.executable, console)
                    except _WorkerCleanupError:
                        raise
                    except OSError:
                        retry_at = now + 2.0
                self.stop.wait(0.25)
        finally:
            if worker:
                worker.close()

    def _main(self, argc, argv):
        try:
            self.status_handle = a32.RegisterServiceCtrlHandlerExW(SERVICE_NAME, self.control_callback, None)
            if not self.status_handle:
                raise _error()
            with self.status_lock:
                if not self.stop.is_set():
                    self._status(2)
                    self._status(4)
            self._supervise()
        except Exception as error:
            self.exit_code = getattr(error, "winerror", None) or 1
        finally:
            if self.status_handle:
                try:
                    self._status(1, self.exit_code)
                except OSError:
                    self.exit_code = self.exit_code or 1

    def run(self):
        table = (SERVICE_TABLE_ENTRYW * 2)()
        table[0].lpServiceName = SERVICE_NAME
        table[0].lpServiceProc = self.main_callback
        if not a32.StartServiceCtrlDispatcherW(table):
            return c.get_last_error() or 1
        return self.exit_code


def run_service(worker_executable: Path) -> int:
    """Enter SCM after the caller verifies the frozen Program Files path and ACL."""
    executable = Path(worker_executable)
    if not executable.is_absolute() or any(character in str(executable) for character in ('"', '\0', '\r', '\n')):
        return 87
    if not is_system():
        return 5
    return _ServiceHost(executable).run()
