import subprocess
import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import Mock, patch

import admin_mode
import autostart_win


EXE = r"C:\Beamer\Beamer.exe"
USER = r"PC\owner"


class AdminModeTests(unittest.TestCase):
    def setUp(self):
        token_launcher = patch("win32api.OpenProcess", side_effect=AssertionError("Token-based launch is forbidden"))
        token_launcher.start()
        self.addCleanup(token_launcher.stop)

    def test_unelevated_launch_uses_desktop_view_application_and_preserves_arguments(self):
        import pythoncom
        from win32com.client import dynamic
        from win32com.shell import shell, shellcon

        desktop = Mock()
        provider = desktop._oleobj_.QueryInterface.return_value
        browser = provider.QueryService.return_value
        view = browser.QueryActiveShellView.return_value
        background = view.GetItemObject.return_value
        windows = Mock()
        command = [EXE, "--hidden", "--relaunched", "--config", r"C:\A & B\owner's settings.json"]

        def find(location, root, window_class, hwnd, options):
            self.assertEqual((location.varianttype, location.value), (pythoncom.VT_I4, 0))
            self.assertEqual(root.varianttype, pythoncom.VT_EMPTY)
            self.assertEqual((window_class, options), (8, 1))
            self.assertEqual(hwnd.varianttype, pythoncom.VT_BYREF | pythoncom.VT_I4)
            hwnd.value = 123
            return desktop

        windows.FindWindowSW.side_effect = find
        with patch.object(pythoncom, "CoInitializeEx") as initialise, \
                patch.object(pythoncom, "CoUninitialize") as uninitialise, \
                patch.object(pythoncom, "CoCreateInstance", return_value=windows) as create, \
                patch.object(dynamic, "DumbDispatch", side_effect=lambda value: value), \
                patch.object(dynamic, "Dispatch", side_effect=lambda value: value):
            self.assertIsNone(admin_mode._launch_unelevated(command))
        initialise.assert_called_once_with(pythoncom.COINIT_APARTMENTTHREADED)
        uninitialise.assert_called_once_with()
        create.assert_called_once_with(
            pythoncom.MakeIID("{9BA05972-F6A8-11CF-A442-00A0C90A8F39}"), None,
            pythoncom.CLSCTX_LOCAL_SERVER, pythoncom.IID_IDispatch)
        desktop._oleobj_.QueryInterface.assert_called_once_with(pythoncom.IID_IServiceProvider)
        provider.QueryService.assert_called_once_with(
            pythoncom.MakeIID("{4C96BE40-915C-11CF-99D3-00AA004AE837}"), shell.IID_IShellBrowser)
        view.GetItemObject.assert_called_once_with(shellcon.SVGIO_BACKGROUND, pythoncom.IID_IDispatch)
        background.Application.ShellExecute.assert_called_once_with(
            EXE, '--hidden --relaunched --config "C:\\A & B\\owner\'s settings.json"', r"C:\Beamer", "open", 0)

    def test_visible_unelevated_launch_shows_its_window(self):
        import pythoncom
        from win32com.client import dynamic

        desktop = Mock()
        background = (desktop._oleobj_.QueryInterface.return_value.QueryService.return_value
                      .QueryActiveShellView.return_value.GetItemObject.return_value)
        windows = Mock()

        def find(location, root, window_class, hwnd, options):
            hwnd.value = 123
            return desktop

        windows.FindWindowSW.side_effect = find
        with patch.object(pythoncom, "CoInitializeEx"), patch.object(pythoncom, "CoUninitialize"), \
                patch.object(pythoncom, "CoCreateInstance", return_value=windows), \
                patch.object(dynamic, "DumbDispatch", side_effect=lambda value: value), \
                patch.object(dynamic, "Dispatch", side_effect=lambda value: value):
            admin_mode._launch_unelevated([EXE, "--relaunched"])
        background.Application.ShellExecute.assert_called_once_with(EXE, "--relaunched", r"C:\Beamer", "open", 1)

    def test_missing_desktop_refuses_launch_and_balances_com(self):
        import pythoncom
        from win32com.client import dynamic
        windows = Mock()
        windows.FindWindowSW.return_value = None
        with patch.object(pythoncom, "CoInitializeEx"), \
                patch.object(pythoncom, "CoUninitialize") as uninitialise, \
                patch.object(pythoncom, "CoCreateInstance", return_value=windows), \
                patch.object(dynamic, "DumbDispatch", side_effect=lambda value: value), \
                patch.object(admin_mode.subprocess, "Popen") as direct_launch:
            with self.assertRaisesRegex(OSError, "desktop shell"):
                admin_mode._launch_unelevated([EXE, "--hidden"])
        uninitialise.assert_called_once_with()
        direct_launch.assert_not_called()

    def test_shell_execution_failure_balances_com_without_another_launcher(self):
        import pythoncom
        from win32com.client import dynamic
        windows, desktop = Mock(), Mock()
        windows.FindWindowSW.return_value = desktop
        provider = desktop._oleobj_.QueryInterface.return_value
        view = provider.QueryService.return_value.QueryActiveShellView.return_value
        application = view.GetItemObject.return_value.Application
        application.ShellExecute.side_effect = OSError("shell launch refused")
        with patch.object(pythoncom, "CoInitializeEx"), \
                patch.object(pythoncom, "CoUninitialize") as uninitialise, \
                patch.object(pythoncom, "CoCreateInstance", return_value=windows), \
                patch.object(dynamic, "DumbDispatch", side_effect=lambda value: value), \
                patch.object(dynamic, "Dispatch", side_effect=lambda value: value), \
                patch.object(admin_mode.subprocess, "Popen") as direct_launch:
            with self.assertRaisesRegex(OSError, "shell launch refused"):
                admin_mode._launch_unelevated([EXE])
        uninitialise.assert_called_once_with()
        direct_launch.assert_not_called()

    def test_failed_com_initialisation_is_not_uninitialised(self):
        import pythoncom
        with patch.object(pythoncom, "CoInitializeEx", side_effect=OSError("COM unavailable")), \
                patch.object(pythoncom, "CoUninitialize") as uninitialise:
            with self.assertRaises(OSError):
                admin_mode._launch_unelevated([EXE])
        uninitialise.assert_not_called()

    def test_shell_unavailable_helper_result_reaches_the_setting_reason(self):
        import pythoncom
        import win32api
        import win32event
        import win32process
        from win32com.shell import shell
        process = Mock()
        with patch.object(pythoncom, "CoInitializeEx") as initialise, \
                patch.object(pythoncom, "CoUninitialize") as uninitialise, \
                patch.object(shell, "ShellExecuteEx", return_value={"hProcess": process}), \
                patch.object(win32event, "WaitForSingleObject"), \
                patch.object(win32process, "GetExitCodeProcess", return_value=2), \
                patch.object(win32api, "CloseHandle") as close:
            with self.assertRaisesRegex(OSError, "desktop shell"):
                admin_mode._run_elevated_helper(False)
        close.assert_called_once_with(process)
        initialise.assert_called_once_with(pythoncom.COINIT_APARTMENTTHREADED)
        uninitialise.assert_called_once_with()

    def test_declined_uac_balances_the_helper_worker_com_apartment(self):
        import pythoncom
        from win32com.shell import shell
        with patch.object(pythoncom, "CoInitializeEx") as initialise, \
                patch.object(pythoncom, "CoUninitialize") as uninitialise, \
                patch.object(shell, "ShellExecuteEx", side_effect=OSError(1223, "cancelled")):
            with self.assertRaises(OSError):
                admin_mode._run_elevated_helper(True)
        initialise.assert_called_once_with(pythoncom.COINIT_APARTMENTTHREADED)
        uninitialise.assert_called_once_with()

    def test_missing_shell_rolls_back_and_returns_its_distinct_helper_result(self):
        import pythoncom
        from win32com.client import dynamic
        windows = Mock()
        windows.FindWindowSW.return_value = None
        original = autostart_win.task_xml(EXE, USER, run_level="HighestAvailable")
        task = original

        def runner(args):
            nonlocal task
            if args[1] == "/query":
                return subprocess.CompletedProcess(args, 0, task, "")
            if args[1] == "/create":
                with open(args[args.index("/xml") + 1], encoding="utf-16") as handle:
                    task = handle.read()
            return subprocess.CompletedProcess(args, 0, "", "")

        transition = admin_mode._transition
        with patch.object(pythoncom, "CoInitializeEx"), \
                patch.object(pythoncom, "CoUninitialize"), \
                patch.object(pythoncom, "CoCreateInstance", return_value=windows), \
                patch.object(dynamic, "DumbDispatch", side_effect=lambda value: value), \
                patch.object(admin_mode, "unavailable_reason", return_value=""), \
                patch.object(admin_mode, "_token_information", return_value=True), \
                patch.object(admin_mode, "_read_preference", return_value=(1, 4)), \
                patch.object(admin_mode, "_write_preference"), \
                patch.object(admin_mode, "_restore_preference") as restore, \
                patch.object(autostart_win, "installed_exe", return_value=EXE), \
                patch.object(autostart_win, "current_user", return_value=USER), \
                patch.object(autostart_win, "migrate_legacy_task"), \
                patch.object(admin_mode, "_transition", side_effect=lambda value, exe, user, **kwargs: transition(value, exe, user, runner, **kwargs)):
            self.assertEqual(admin_mode.run_helper(["--set-admin-mode", "off"]), 2)
        self.assertEqual(ET.tostring(ET.fromstring(task)), ET.tostring(ET.fromstring(original)))
        restore.assert_called_once_with((1, 4))

    def test_token_query_closes_non_context_manager_pyhandle(self):
        import win32security
        closed = []
        token = SimpleNamespace(Close=lambda: closed.append(True))
        with patch.object(win32security, "OpenProcessToken", return_value=token), \
                patch.object(win32security, "GetTokenInformation", return_value=3):
            self.assertEqual(admin_mode._token_information(win32security.TokenElevationType), 3)
        self.assertEqual(closed, [True])

    def test_task_xml_obeys_machine_local_preference(self):
        for elevated, expected in ((False, "LeastPrivilege"), (True, "HighestAvailable")):
            with self.subTest(elevated=elevated), patch.object(admin_mode, "enabled", return_value=elevated):
                root = ET.fromstring(autostart_win.task_xml(EXE, USER))
                self.assertEqual(root.findtext("{*}Principals/{*}Principal/{*}RunLevel"), expected)

    def test_replacement_validation_obeys_machine_local_preference(self):
        xml = autostart_win.task_xml(EXE, USER).replace("LeastPrivilege", "HighestAvailable")
        with patch.object(admin_mode, "enabled", return_value=True):
            self.assertTrue(autostart_win._replacement_ready(ET.fromstring(xml), EXE, "--hidden"))
        with patch.object(admin_mode, "enabled", return_value=False):
            self.assertFalse(autostart_win._replacement_ready(ET.fromstring(xml), EXE, "--hidden"))

    def test_cancelled_uac_preserves_preference(self):
        with patch.object(admin_mode, "unavailable_reason", return_value=""), \
                patch.object(admin_mode, "enabled", return_value=False), \
                patch.object(admin_mode, "_run_elevated_helper", side_effect=OSError(1223, "cancelled")), \
                patch.object(admin_mode, "_write_preference") as write:
            with self.assertRaises(OSError):
                admin_mode.change(True)
        write.assert_not_called()

    def test_portable_and_source_builds_are_unavailable(self):
        with patch.object(admin_mode.sys, "platform", "win32"), \
                patch.object(admin_mode.autostart_win, "installed_exe", return_value=None):
            self.assertTrue(admin_mode.unavailable_reason())

    def test_standard_account_is_unavailable(self):
        with patch.object(admin_mode.sys, "platform", "win32"), \
                patch.object(admin_mode.autostart_win, "installed_exe", return_value=EXE), \
                patch.object(admin_mode.autostart_win, "can_run_for_current_user", return_value=True), \
                patch.object(admin_mode, "_administrator_account", return_value=False):
            self.assertTrue(admin_mode.unavailable_reason())

    def test_another_administrators_account_is_unavailable(self):
        with patch.object(admin_mode.sys, "platform", "win32"), \
                patch.object(admin_mode.autostart_win, "installed_exe", return_value=EXE), \
                patch.object(admin_mode.autostart_win, "can_run_for_current_user", return_value=False), \
                patch.object(admin_mode, "_administrator_account", return_value=True):
            self.assertTrue(admin_mode.unavailable_reason())

    def test_unsplit_administrator_token_is_unavailable(self):
        with patch.object(admin_mode.sys, "platform", "win32"), \
                patch.object(admin_mode.autostart_win, "installed_exe", return_value=EXE), \
                patch.object(admin_mode.autostart_win, "can_run_for_current_user", return_value=True), \
                patch.object(admin_mode, "_administrator_account", return_value=True), \
                patch.object(admin_mode, "_token_information", return_value=1):
            self.assertIn("User Account Control", admin_mode.unavailable_reason())

    def test_helper_only_accepts_fixed_boolean_argument(self):
        with patch.object(admin_mode, "_transition") as transition:
            self.assertNotEqual(admin_mode.run_helper(["--set-admin-mode", "maybe"]), 0)
        transition.assert_not_called()

    def test_helper_rejects_relative_config_before_changing_anything(self):
        with patch.object(admin_mode, "_transition") as transition:
            self.assertNotEqual(admin_mode.run_helper(["--set-admin-mode", "on", "--config", "relative.json"]), 0)
        transition.assert_not_called()

    def test_unchanged_selection_does_not_request_relaunch(self):
        with patch.object(admin_mode, "unavailable_reason", return_value=""), \
                patch.object(admin_mode, "enabled", return_value=True), \
                patch.object(admin_mode, "_run_elevated_helper") as helper:
            self.assertFalse(admin_mode.change(True))
        helper.assert_not_called()

    def test_custom_config_is_forwarded_to_helper_without_reading_it(self):
        config_path = r"C:\A & B\owner's settings.json"
        with patch.object(admin_mode, "unavailable_reason", return_value=""), \
                patch.object(admin_mode, "enabled", return_value=False), \
                patch.object(admin_mode, "_run_elevated_helper") as helper:
            self.assertTrue(admin_mode.change(True, config_path))
        helper.assert_called_once_with(True, config_path, hidden=False)

    def test_manual_medium_launch_honours_the_existing_preference(self):
        with patch.object(admin_mode.sys, "platform", "win32"), \
                patch.object(admin_mode, "enabled", return_value=True), \
                patch.object(admin_mode, "unavailable_reason", return_value=""), \
                patch.object(admin_mode, "_token_information", return_value=False), \
                patch.object(admin_mode, "_run_elevated_helper") as helper:
            self.assertTrue(admin_mode.relaunch_if_required(r"C:\Beamer\settings.json"))
        helper.assert_called_once_with(True, r"C:\Beamer\settings.json")

    def test_already_elevated_logon_start_does_not_prompt(self):
        with patch.object(admin_mode.sys, "platform", "win32"), \
                patch.object(admin_mode, "enabled", return_value=True), \
                patch.object(admin_mode, "unavailable_reason", return_value=""), \
                patch.object(admin_mode, "_token_information", return_value=True), \
                patch.object(admin_mode, "_run_elevated_helper") as helper:
            self.assertFalse(admin_mode.relaunch_if_required())
        helper.assert_not_called()

    def test_manual_visible_launch_preserves_requested_visibility(self):
        with patch.object(admin_mode.sys, "platform", "win32"), \
                patch.object(admin_mode, "enabled", return_value=True), \
                patch.object(admin_mode, "unavailable_reason", return_value=""), \
                patch.object(admin_mode, "_token_information", return_value=False), \
                patch.object(admin_mode, "_run_elevated_helper") as helper:
            self.assertTrue(admin_mode.relaunch_if_required(hidden=False))
        helper.assert_called_once_with(True, None, hidden=False)

    def test_replacement_visibility_matches_request(self):
        for hidden in (False, True):
            with self.subTest(hidden=hidden), patch.object(admin_mode.subprocess, "Popen") as launch:
                admin_mode._launch_replacement(EXE, True, hidden=hidden)
            self.assertEqual("--hidden" in launch.call_args.args[0], hidden)
            self.assertIn("--relaunched", launch.call_args.args[0])

    def test_elevated_replacement_gets_an_independent_pyinstaller_environment(self):
        with patch.dict(admin_mode.os.environ, {"FW26B_PARENT_ENV": "kept"}, clear=True), \
                patch.object(admin_mode.subprocess, "Popen") as launch:
            admin_mode._launch_replacement(EXE, True)
            self.assertEqual(dict(admin_mode.os.environ), {"FW26B_PARENT_ENV": "kept"})
        self.assertEqual(launch.call_args.kwargs["env"], {
            "FW26B_PARENT_ENV": "kept", "PYINSTALLER_RESET_ENVIRONMENT": "1"})

    def test_startup_helper_rejects_show_argument(self):
        with patch.object(admin_mode, "_transition") as transition:
            self.assertNotEqual(admin_mode.run_helper(["--set-admin-startup", "on", "--show"]), 0)
        transition.assert_not_called()


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.task = autostart_win.task_xml(EXE, USER)
        self.saved = []
        self.pref = False

    def runner(self, args):
        if args[1] == "/query":
            return subprocess.CompletedProcess(args, 0, self.task, "")
        if args[1] == "/create":
            with open(args[args.index("/xml") + 1], encoding="utf-16") as handle:
                self.task = handle.read()
            self.saved.append(self.task)
        return subprocess.CompletedProcess(args, 0, "", "")

    def test_success_changes_only_runlevel_and_launches_hidden_replacement(self):
        with patch.object(admin_mode, "_read_preference", return_value=None), \
                patch.object(admin_mode, "_write_preference") as write, \
                patch.object(admin_mode, "_launch_replacement") as launch:
            admin_mode._transition(True, EXE, USER, self.runner)
        changed = ET.fromstring(self.task)
        original = ET.fromstring(self.task.replace("HighestAvailable", "LeastPrivilege"))
        self.assertEqual(changed.findtext("{*}Principals/{*}Principal/{*}RunLevel"), "HighestAvailable")
        self.assertEqual(ET.tostring(original), ET.tostring(ET.fromstring(autostart_win.task_xml(EXE, USER))))
        write.assert_called_once_with(True)
        launch.assert_called_once_with(EXE, True)

    def test_transition_accepts_the_omitted_default_runlevel(self):
        for value, expected in ((False, "LeastPrivilege"), (True, "HighestAvailable")):
            self.task = autostart_win.task_xml(EXE, USER, run_level="LeastPrivilege").replace(
                "<RunLevel>LeastPrivilege</RunLevel>", "")
            with self.subTest(value=value), \
                    patch.object(admin_mode, "_read_preference", return_value=None), \
                    patch.object(admin_mode, "_write_preference") as write, \
                    patch.object(admin_mode, "_launch_replacement") as launch:
                admin_mode._transition(value, EXE, USER, self.runner)
                self.assertEqual(ET.fromstring(self.task).findtext("{*}Principals/{*}Principal/{*}RunLevel"), expected)
                write.assert_called_once_with(value)
                launch.assert_called_once_with(EXE, value)

    def test_failed_replacement_restores_task_and_absent_preference(self):
        original = self.task
        with patch.object(admin_mode, "_read_preference", return_value=None), \
                patch.object(admin_mode, "_write_preference") as write, \
                patch.object(admin_mode, "_restore_preference") as restore, \
                patch.object(admin_mode, "_launch_replacement", side_effect=OSError("launch failed")):
            with self.assertRaisesRegex(OSError, "launch failed"):
                admin_mode._transition(True, EXE, USER, self.runner)
        self.assertEqual(ET.tostring(ET.fromstring(self.task)), ET.tostring(ET.fromstring(original)))
        restore.assert_called_once_with(None)

    def test_disabled_startup_stays_disabled_when_admin_mode_changes(self):
        self.task = self.task.replace("<Enabled>true</Enabled>", "<Enabled>false</Enabled>")
        with patch.object(admin_mode, "_read_preference", return_value=None), \
                patch.object(admin_mode, "_write_preference"), \
                patch.object(admin_mode, "_launch_replacement"):
            admin_mode._transition(True, EXE, USER, self.runner)
        self.assertFalse(autostart_win._task_enabled(autostart_win.task_name(USER), self.runner))

    def test_no_task_creates_only_disabled_logon_trigger(self):
        self.task = None
        runner = self.runner

        def absent_until_create(args):
            if args[1] == "/query" and self.task is None:
                return subprocess.CompletedProcess(args, 1, "", "")
            return runner(args)

        with patch.object(admin_mode, "_read_preference", return_value=None), \
                patch.object(admin_mode, "_write_preference"), \
                patch.object(admin_mode, "_launch_replacement"):
            admin_mode._transition(True, EXE, USER, absent_until_create)
        self.assertFalse(autostart_win._task_enabled(autostart_win.task_name(USER), runner))
        self.assertEqual(ET.fromstring(self.task).findtext("{*}Principals/{*}Principal/{*}RunLevel"), "HighestAvailable")

    def test_task_failure_never_changes_preference_or_launches(self):
        def refusal(args):
            if args[1] == "/create":
                return subprocess.CompletedProcess(args, 1, "", "Access denied")
            return self.runner(args)

        with patch.object(admin_mode, "_read_preference", return_value=None), \
                patch.object(admin_mode, "_write_preference") as write, \
                patch.object(admin_mode, "_launch_replacement") as launch:
            with self.assertRaisesRegex(OSError, "Access denied"):
                admin_mode._transition(True, EXE, USER, refusal)
        write.assert_not_called()
        launch.assert_not_called()

    def test_unrelated_task_is_never_overwritten(self):
        self.task = self.task.replace(EXE, r"C:\Other\Beamer.exe")
        with patch.object(admin_mode, "_read_preference", return_value=None), \
                patch.object(admin_mode, "_write_preference") as write:
            with self.assertRaisesRegex(OSError, "installed app"):
                admin_mode._transition(True, EXE, USER, self.runner)
        self.assertEqual(self.saved, [])
        write.assert_not_called()

    def test_ordinary_upgrade_keeps_opted_in_task_without_registering_it_again(self):
        self.task = self.task.replace("LeastPrivilege", "HighestAvailable")
        with patch.object(admin_mode, "enabled", return_value=True), \
                patch.object(autostart_win, "can_run_for_current_user", return_value=True):
            autostart_win.set_enabled(True, EXE, USER, self.runner)
        self.assertEqual(self.saved, [])

    def test_medium_startup_update_requests_consent_without_relaunching(self):
        for start_at_sign_in in (False, True):
            with self.subTest(enabled=start_at_sign_in), \
                    patch.object(admin_mode, "enabled", return_value=True), \
                    patch.object(admin_mode, "_is_elevated", return_value=False), \
                    patch.object(admin_mode, "change_startup") as change:
                autostart_win.set_enabled(start_at_sign_in, EXE, USER, self.runner)
            change.assert_called_once_with(start_at_sign_in)
        self.assertEqual(self.saved, [])

