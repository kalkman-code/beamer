"""Opt-in elevation belongs to this Windows account, never the paired settings file."""

from __future__ import annotations

import copy
import logging
import ntpath
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from core.i18n import tr
import autostart_win


LOGGER = logging.getLogger(__name__)
REGISTRY_PATH = r"Software\KalkmanCode\Beamer"
REGISTRY_VALUE = "RunAsAdministrator"
NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
DESKTOP_SHELL_UNAVAILABLE = 2


class DesktopShellUnavailable(OSError):
    pass


def _read_preference():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
            return winreg.QueryValueEx(key, REGISTRY_VALUE)
    except FileNotFoundError:
        return None


def enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        return _read_preference() == (1, winreg.REG_DWORD)
    except OSError:
        return False


def _write_preference(value: bool) -> None:
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, REGISTRY_VALUE, 0, winreg.REG_DWORD, int(value))


def _restore_preference(previous) -> None:
    import winreg
    if previous is not None:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, REGISTRY_VALUE, 0, previous[1], previous[0])
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, REGISTRY_VALUE)
    except FileNotFoundError:
        pass


def _token_information(kind):
    import win32api
    import win32con
    import win32security
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, kind)
    finally:
        token.Close()


def _administrator_account() -> bool:
    import win32security
    # A split administrator's medium token has its administrator group disabled.
    return bool(_token_information(win32security.TokenElevation)) or _token_information(win32security.TokenElevationType) == 3


def _is_elevated() -> bool:
    if sys.platform != "win32":
        return False
    import win32security
    return bool(_token_information(win32security.TokenElevation))


def unavailable_reason() -> str:
    if sys.platform != "win32":
        return tr("This setting is only available on Windows.")
    if autostart_win.installed_exe() is None:
        return tr("Install Beamer to control administrator windows. This setting is unavailable in portable mode.")
    try:
        if not autostart_win.can_run_for_current_user():
            return tr("This setting needs Beamer to run as the signed-in account.")
        if not _administrator_account():
            return tr("This setting needs a Windows administrator account.")
        import win32security
        if _token_information(win32security.TokenElevationType) not in (2, 3):
            return tr("Enable Windows User Account Control to use this setting.")
    except Exception:
        return tr("Windows could not check this account's administrator permission.")
    return ""


def _run_elevated_helper(value: bool, config_path=None, *, startup_only=False, hidden=True) -> None:
    import pythoncom
    import win32api
    import win32event
    from win32com.shell import shell
    arguments = ["--set-admin-startup" if startup_only else "--set-admin-mode", "on" if value else "off"]
    if config_path is not None:
        arguments.extend(["--config", os.path.abspath(str(config_path))])
    if not hidden and not startup_only:
        arguments.append("--show")
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    try:
        result = shell.ShellExecuteEx(
            fMask=0x40 | 0x100, lpVerb="runas", lpFile=sys.executable,
            lpParameters=subprocess.list2cmdline(arguments),
            lpDirectory=os.path.dirname(sys.executable), nShow=0,
        )
        process = result.get("hProcess")
        if process is None:
            raise OSError(tr("Windows could not start the administrator setting change."))
        try:
            win32event.WaitForSingleObject(process, win32event.INFINITE)
            import win32process
            result_code = win32process.GetExitCodeProcess(process)
            if result_code == DESKTOP_SHELL_UNAVAILABLE:
                raise OSError(tr("Windows could not find the desktop shell. Start Windows Explorer and try again. Your previous setting has been kept."))
            if result_code != 0:
                raise OSError(tr("The administrator setting could not be changed. Your previous setting has been kept."))
        finally:
            win32api.CloseHandle(process)
    finally:
        pythoncom.CoUninitialize()


def change(value: bool, config_path=None) -> bool:
    reason = unavailable_reason()
    if reason:
        raise OSError(reason)
    if bool(value) == enabled():
        return False
    try:
        # The switch lives on the visible Settings page, so the replacement reopens it.
        _run_elevated_helper(bool(value), config_path, hidden=False)
    except Exception as exc:
        if getattr(exc, "winerror", None) == 1223 or getattr(exc, "errno", None) == 1223:
            raise OSError(tr("Administrator permission was cancelled. Your previous setting has been kept.")) from exc
        if isinstance(exc, OSError):
            raise
        raise OSError(tr("The administrator setting could not be changed. Your previous setting has been kept.")) from exc
    return True


def change_startup(value: bool) -> None:
    reason = unavailable_reason()
    if reason:
        raise OSError(reason)
    try:
        _run_elevated_helper(value, startup_only=True)
    except Exception as exc:
        if getattr(exc, "winerror", None) == 1223 or getattr(exc, "errno", None) == 1223:
            raise OSError(tr("Administrator permission was cancelled. Your previous setting has been kept.")) from exc
        raise OSError(tr("Start at sign-in could not be changed. Your previous setting has been kept.")) from exc


def relaunch_if_required(config_path=None, *, hidden=True) -> bool:
    if sys.platform != "win32" or not enabled() or unavailable_reason():
        return False
    import win32security
    if _token_information(win32security.TokenElevation):
        return False
    try:
        if hidden:
            _run_elevated_helper(True, config_path)
        else:
            _run_elevated_helper(True, config_path, hidden=False)
    except Exception as exc:
        if getattr(exc, "winerror", None) == 1223 or getattr(exc, "errno", None) == 1223:
            raise OSError(tr("Administrator permission was cancelled. Your previous setting has been kept.")) from exc
        raise OSError(tr("The administrator setting could not be changed. Your previous setting has been kept.")) from exc
    return True


def _register_task(name: str, root, run) -> None:
    descriptor, path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-16") as handle:
            handle.write(ET.tostring(root, encoding="unicode"))
        result = run(["schtasks", "/create", "/tn", name, "/xml", path, "/f"])
        if result.returncode:
            raise OSError((result.stderr or result.stdout or "Windows could not update Beamer's sign-in task").strip())
    finally:
        os.unlink(path)


def _checked_task(root, exe: str, user: str):
    principals = root.findall(NS + "Principals/" + NS + "Principal")
    actions = root.findall(NS + "Actions/*")
    if len(principals) != 1 or len(actions) != 1 or actions[0].tag != NS + "Exec":
        raise OSError("Beamer's sign-in task has an unexpected definition")
    principal = principals[0]
    owner = principal.findtext(NS + "UserId", "")
    if owner.casefold() != user.casefold() and owner.casefold() != autostart_win._user_sid(user).casefold():
        raise OSError("Beamer's sign-in task belongs to a different account")
    command = actions[0].findtext(NS + "Command", "").strip('"')
    if (principal.findtext(NS + "LogonType") != "InteractiveToken"
            or ntpath.normcase(command) != ntpath.normcase(exe)):
        raise OSError("Beamer's sign-in task does not start this installed app")
    level = principal.find(NS + "RunLevel")
    if level is None:
        level = ET.SubElement(principal, NS + "RunLevel")
    return level


def _transition(value: bool, exe: str, user: str, run=autostart_win._run, *, config_path=None, hidden=True) -> None:
    name = autostart_win.task_name(user)
    previous = autostart_win._task_root(name, run)
    preference = _read_preference()
    if previous is None:
        updated = ET.fromstring(autostart_win.task_xml(exe, user, run_level="LeastPrivilege"))
        # The task can hold the privilege choice without opting the user into logon startup.
        updated.find(NS + "Triggers/" + NS + "LogonTrigger/" + NS + "Enabled").text = "false"
    else:
        updated = copy.deepcopy(previous)
    level = _checked_task(updated, exe, user)
    level.text = "HighestAvailable" if value else "LeastPrivilege"
    _register_task(name, updated, run)
    try:
        _write_preference(value)
        if not hidden:
            _launch_replacement(exe, value, config_path, hidden=False)
        elif config_path is None:
            _launch_replacement(exe, value)
        else:
            _launch_replacement(exe, value, config_path)
    except Exception:
        rollback_errors = []
        try:
            if previous is None:
                result = run(["schtasks", "/delete", "/tn", name, "/f"])
                if result.returncode:
                    raise OSError("Windows could not remove the incomplete Beamer sign-in task")
            else:
                _register_task(name, previous, run)
        except Exception as rollback_error:
            rollback_errors.append(rollback_error)
            LOGGER.exception("Could not restore the administrator setting after a failed change")
        try:
            _restore_preference(preference)
        except Exception as rollback_error:
            rollback_errors.append(rollback_error)
            LOGGER.exception("Could not restore the administrator preference after a failed change")
        if rollback_errors:
            raise OSError("Windows could not restore the previous administrator setting") from rollback_errors[0]
        raise


def _launch_unelevated(command: list[str]) -> None:
    import pythoncom
    from win32com.client import VARIANT, dynamic
    from win32com.shell import shell, shellcon

    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    windows = desktop = provider = browser = view = background = application = None
    try:
        # The desktop view's Application lives in Explorer; creating Shell.Application
        # directly can retain elevation. Raymond Chen's sequence and desktop finder:
        # https://devblogs.microsoft.com/oldnewthing/20131118-00/?p=2643
        # https://devblogs.microsoft.com/oldnewthing/20130318-00/?p=4933
        try:
            windows = dynamic.DumbDispatch(pythoncom.CoCreateInstance(
                pythoncom.MakeIID("{9BA05972-F6A8-11CF-A442-00A0C90A8F39}"), None,
                pythoncom.CLSCTX_LOCAL_SERVER, pythoncom.IID_IDispatch))
            windows._FlagAsMethod("FindWindowSW")
            desktop = windows.FindWindowSW(
                VARIANT(pythoncom.VT_I4, shellcon.CSIDL_DESKTOP),
                VARIANT(pythoncom.VT_EMPTY, None), 8,
                VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0), 1)
            if desktop is None:
                raise DesktopShellUnavailable("Windows could not find this account's desktop shell")
            provider = desktop._oleobj_.QueryInterface(pythoncom.IID_IServiceProvider)
            browser = provider.QueryService(
                pythoncom.MakeIID("{4C96BE40-915C-11CF-99D3-00AA004AE837}"), shell.IID_IShellBrowser)
            view = browser.QueryActiveShellView()
            background = dynamic.Dispatch(view.GetItemObject(shellcon.SVGIO_BACKGROUND, pythoncom.IID_IDispatch))
            application = background.Application
        except Exception as exc:
            raise DesktopShellUnavailable("Windows could not find this account's desktop shell") from exc
        # Windows applies this to the first ShowWindow, so 0 would hide a Settings window Qt shows.
        show = 0 if "--hidden" in command else 1
        application.ShellExecute(command[0], subprocess.list2cmdline(command[1:]), os.path.dirname(command[0]), "open", show)
    finally:
        application = background = view = browser = provider = desktop = windows = None
        pythoncom.CoUninitialize()


def _launch_replacement(exe: str, value: bool, config_path=None, *, hidden=True) -> int | None:
    command = [exe, *(["--hidden"] if hidden else []), "--relaunched"]
    if config_path is not None:
        command.extend(["--config", os.path.abspath(str(config_path))])
    if value:
        environment = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
        return subprocess.Popen(command, cwd=os.path.dirname(exe), creationflags=0x08000000, env=environment).pid
    return _launch_unelevated(command)


def run_helper(argv: list[str]) -> int:
    hidden = not (argv and argv[-1] == "--show")
    if not hidden:
        argv = argv[:-1]
    if (len(argv) not in (2, 4) or argv[0] not in ("--set-admin-mode", "--set-admin-startup")
            or argv[1] not in ("on", "off")
            or (not hidden and argv[0] == "--set-admin-startup")
            or (len(argv) == 4 and (argv[0] == "--set-admin-startup" or argv[2] != "--config" or not os.path.isabs(argv[3])))):
        return 1
    try:
        reason = unavailable_reason()
        if reason:
            raise OSError(reason)
        import win32security
        if not _token_information(win32security.TokenElevation):
            raise OSError("Administrator setting changes require administrator permission")
        exe = autostart_win.installed_exe()
        autostart_win.migrate_legacy_task()
        if argv[0] == "--set-admin-startup":
            autostart_win.set_enabled(argv[1] == "on", exe, autostart_win.current_user())
            return 0
        config_path = argv[3] if len(argv) == 4 else None
        if hidden:
            _transition(argv[1] == "on", exe, autostart_win.current_user(), config_path=config_path)
        else:
            _transition(argv[1] == "on", exe, autostart_win.current_user(), config_path=config_path, hidden=False)
        return 0
    except DesktopShellUnavailable:
        LOGGER.exception("Could not find the desktop shell for the administrator setting change")
        return DESKTOP_SHELL_UNAVAILABLE
    except Exception:
        LOGGER.exception("Could not change the administrator setting")
        return 1
