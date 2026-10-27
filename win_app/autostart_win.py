"""Start at logon, owned by the app: each interactive account has its own scheduled task, so there
is no config field to disagree with it.

The task uses the signed-in user's ordinary token unless that administrator explicitly opts in
on this PC. The installer lowers old elevated tasks when that preference is absent.

Registered from XML, not `schtasks /create /sc onlogon`: that form cannot set a working directory
and gives every task a 72-hour execution limit, which would stop Beamer three days after logon.
"""

from __future__ import annotations

import ctypes
import os
import ntpath
import subprocess
import sys
import tempfile
from typing import Callable, Optional
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET

from core import windows_tools
from core import portable_runtime

TASK_PREFIX = "Beamer-"
CREATE_NO_WINDOW = 0x08000000
NAME_SAM_COMPATIBLE = 2
WTS_CURRENT_SESSION = 0xFFFFFFFF
WTS_USER_NAME = 5
WTS_DOMAIN_NAME = 7

Runner = Callable[[list], "subprocess.CompletedProcess"]


def _run(args: list) -> subprocess.CompletedProcess:
    return windows_tools.run(args)


def _process_user() -> str:
    """DOMAIN\\user from the process token, which may be an administrator entered at UAC."""
    size = ctypes.c_ulong(0)
    secur32 = ctypes.windll.secur32
    secur32.GetUserNameExW(NAME_SAM_COMPATIBLE, None, ctypes.byref(size))
    buffer = ctypes.create_unicode_buffer(size.value)
    if not secur32.GetUserNameExW(NAME_SAM_COMPATIBLE, buffer, ctypes.byref(size)):
        raise OSError("could not read the signed-in account name")
    return buffer.value


def _session_value(info_class: int) -> str:
    value = ctypes.c_void_p()
    size = ctypes.c_ulong()
    wtsapi32 = ctypes.windll.wtsapi32
    query = wtsapi32.WTSQuerySessionInformationW
    query.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_ulong),
    ]
    query.restype = ctypes.c_int
    free_memory = wtsapi32.WTSFreeMemory
    free_memory.argtypes = [ctypes.c_void_p]
    free_memory.restype = None
    if not query(
        None, WTS_CURRENT_SESSION, info_class, ctypes.byref(value), ctypes.byref(size)
    ):
        raise OSError("could not read the signed-in account")
    try:
        return ctypes.wstring_at(value.value)
    finally:
        free_memory(value)


def current_user() -> str:
    """DOMAIN\\user from the interactive session, even when another admin approved UAC."""
    domain = _session_value(WTS_DOMAIN_NAME)
    username = _session_value(WTS_USER_NAME)
    if not username:
        raise OSError("could not identify the signed-in account")
    return f"{domain}\\{username}" if domain else username


def task_name(user: str) -> str:
    return TASK_PREFIX + user.replace("\\", "-")


def can_run_for_current_user() -> bool:
    """Do not configure another user's session after an over-the-shoulder administrator launch."""
    return current_user().casefold() == _process_user().casefold()


def installed_exe() -> Optional[str]:
    """The exe a task can start, or None when running from source, where there is none."""
    return sys.executable if getattr(sys, "frozen", False) and not portable_runtime.enabled() else None


def preferred_run_level() -> str:
    import admin_mode
    return "HighestAvailable" if admin_mode.enabled() else "LeastPrivilege"


def task_xml(exe: str, user: str, arguments: str = "--hidden", *, run_level: Optional[str] = None) -> str:
    run_level = preferred_run_level() if run_level is None else run_level
    if run_level not in ("LeastPrivilege", "HighestAvailable"):
        raise ValueError("invalid Windows task run level")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>{run_level}</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(exe)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(os.path.dirname(exe))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def is_enabled(run: Runner = _run, user: Optional[str] = None) -> bool:
    if portable_runtime.enabled():
        return False
    user = user or current_user()
    migrate_legacy_task(run, user)
    return _task_enabled(task_name(user), run)


def _task_root(name: str, run: Runner):
    result = run(["schtasks", "/query", "/tn", name, "/xml"])
    if result.returncode != 0:
        return None
    try:
        return ET.fromstring(result.stdout.lstrip("\ufeff"))
    except ET.ParseError as exc:
        raise OSError("Windows returned invalid sign-in task XML") from exc


def _task_enabled(name: str, run: Runner) -> bool:
    root = _task_root(name, run)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    return (root is not None
            and root.findtext("t:Settings/t:Enabled", "true", ns).lower() == "true"
            and any(trigger.findtext("t:Enabled", "true", ns).lower() == "true"
                    for trigger in root.findall("t:Triggers/t:LogonTrigger", ns)))


def _replacement_ready(root, exe: str, arguments: str) -> bool:
    if root is None:
        return False
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    command = root.findtext("t:Actions/t:Exec/t:Command", "", ns).strip('"')
    return (
        root.findtext("t:Settings/t:Enabled", "true", ns).lower() == "true"
        and any(trigger.findtext("t:Enabled", "true", ns).lower() == "true"
                for trigger in root.findall("t:Triggers/t:LogonTrigger", ns))
        and ntpath.normcase(command) == ntpath.normcase(exe)
        and root.findtext("t:Actions/t:Exec/t:Arguments", "", ns) == arguments
        and root.findtext("t:Principals/t:Principal/t:RunLevel", "LeastPrivilege", ns) == preferred_run_level()
        and root.findtext("t:Principals/t:Principal/t:LogonType", "", ns) == "InteractiveToken"
    )


def migrate_legacy_task(run: Runner = _run, user: Optional[str] = None) -> None:
    """Keep the old starter until its owner's replacement has been registered successfully."""
    if portable_runtime.enabled():
        return
    user = user or current_user()
    root = _task_root("Beamer", run)
    if root is None:
        return
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    owner = root.findtext("t:Principals/t:Principal/t:UserId", "", ns)
    # schtasks can export the principal as a SID rather than its account name.
    if owner.casefold() != user.casefold():
        if not owner.casefold().startswith("s-1-"):
            return
        try:
            if owner.casefold() != _user_sid(user).casefold():
                return
        except OSError:
            return
    exe = installed_exe()
    command = root.findtext("t:Actions/t:Exec/t:Command", "", ns).strip('"')
    if exe is None or ntpath.normcase(command) != ntpath.normcase(exe):
        return
    name = task_name(user)
    arguments = root.findtext("t:Actions/t:Exec/t:Arguments", "", ns)
    if _replacement_ready(_task_root(name, run), exe, arguments):
        remove_legacy_task(run, disable_first=True)
    elif root.findtext("t:Settings/t:Enabled", "true", ns).lower() != "true":
        remove_legacy_task(run)
    else:
        set_enabled(True, exe, user, run, arguments=arguments)


def _user_sid(user: str) -> str:
    if sys.platform != "win32":
        return user
    import win32security
    try:
        sid, _, _ = win32security.LookupAccountName(None, user)
    except win32security.error as exc:
        raise OSError("could not identify the sign-in task owner") from exc
    return win32security.ConvertSidToStringSid(sid)


def set_enabled(enabled: bool, exe: str, user: Optional[str] = None, run: Runner = _run,
                *, arguments: str = "--hidden") -> None:
    """Raises OSError with schtasks' own words when Windows refuses."""
    if portable_runtime.enabled():
        raise OSError("Portable Beamer cannot change start at sign-in")
    user = user or current_user()
    name = task_name(user)
    if preferred_run_level() == "HighestAvailable":
        import admin_mode
        if not admin_mode._is_elevated():
            if enabled and _replacement_ready(_task_root(name, run), exe, arguments):
                return
            admin_mode.change_startup(enabled)
            return
    if not enabled:
        remove_legacy_task(run)
        if run(["schtasks", "/query", "/tn", name]).returncode != 0:
            return
        result = run(["schtasks", "/delete", "/tn", name, "/f"])
    else:
        if not can_run_for_current_user():
            raise OSError("Start at sign-in needs Beamer to run as the signed-in account")
        if _replacement_ready(_task_root(name, run), exe, arguments):
            remove_legacy_task(run, disable_first=True)
            return
        fd, path = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(fd, "w", encoding="utf-16") as handle:
                handle.write(task_xml(exe, user, arguments))
            result = run(["schtasks", "/create", "/tn", name, "/xml", path, "/f"])
        finally:
            os.unlink(path)
    if result.returncode != 0:
        raise OSError((result.stderr or result.stdout or "schtasks failed").strip())
    if enabled:
        remove_legacy_task(run, disable_first=True)


def remove_legacy_task(run: Runner = _run, *, disable_first: bool = False) -> None:
    if portable_runtime.enabled():
        return
    if run(["schtasks", "/query", "/tn", "Beamer"]).returncode == 0:
        if disable_first:
            result = run(["schtasks", "/change", "/tn", "Beamer", "/disable"])
            if result.returncode != 0:
                raise OSError((result.stderr or result.stdout or "schtasks failed").strip())
        result = run(["schtasks", "/delete", "/tn", "Beamer", "/f"])
        if result.returncode != 0:
            raise OSError((result.stderr or result.stdout or "schtasks failed").strip())
