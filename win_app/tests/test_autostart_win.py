import subprocess
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import autostart_win

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
EXE = r"C:\Users\alex\AppData\Local\Beamer\Beamer.exe"


class FakeSchtasks:
    def __init__(self, exists=False, fail=None):
        self.tasks = {"Beamer"} if exists else set()
        self.fail = fail
        self.calls = []
        self.xml = None

    def __call__(self, args):
        self.calls.append(args[1])
        name = args[args.index("/tn") + 1] if "/tn" in args else "Beamer"
        if args[1] == "/create" and self.fail is None:
            with open(args[args.index("/xml") + 1], encoding="utf-16") as handle:
                self.xml = handle.read()
            self.tasks.add(name)
        if args[1] == "/delete" and self.fail is None:
            self.tasks.discard(name)
        if args[1] == "/query":
            xml = autostart_win.task_xml(EXE, r"STUDIO-PC\alex")
            return subprocess.CompletedProcess(args, 0 if name in self.tasks else 1, xml, "")
        return subprocess.CompletedProcess(args, 1 if self.fail else 0, "", self.fail or "")


class TaskXmlTests(unittest.TestCase):
    def test_starts_hidden_with_normal_privilege_and_never_times_out(self):
        root = ET.fromstring(autostart_win.task_xml(EXE, r"STUDIO-PC\alex").replace('encoding="UTF-16"', ""))
        self.assertEqual(root.find("t:Actions/t:Exec/t:Command", NS).text, EXE)
        self.assertEqual(root.find("t:Actions/t:Exec/t:Arguments", NS).text, "--hidden")
        self.assertEqual(root.find("t:Principals/t:Principal/t:UserId", NS).text, r"STUDIO-PC\alex")
        self.assertEqual(root.find("t:Principals/t:Principal/t:RunLevel", NS).text, "LeastPrivilege")
        self.assertEqual(root.find("t:Settings/t:ExecutionTimeLimit", NS).text, "PT0S")
        self.assertEqual(root.find("t:Triggers/t:LogonTrigger/t:UserId", NS).text, r"STUDIO-PC\alex")

    def test_escapes_the_path(self):
        ET.fromstring(autostart_win.task_xml(r"C:\A & B\Beamer.exe", "u").replace('encoding="UTF-16"', ""))

    def test_omitted_runlevel_matches_only_the_default_privilege_preference(self):
        root = ET.fromstring(autostart_win.task_xml(EXE, r"STUDIO-PC\alex", run_level="LeastPrivilege").replace(
            "<RunLevel>LeastPrivilege</RunLevel>", ""))
        with patch.object(autostart_win, "preferred_run_level", return_value="LeastPrivilege"):
            self.assertTrue(autostart_win._replacement_ready(root, EXE, "--hidden"))
        with patch.object(autostart_win, "preferred_run_level", return_value="HighestAvailable"):
            self.assertFalse(autostart_win._replacement_ready(root, EXE, "--hidden"))


class SwitchTests(unittest.TestCase):
    def test_empty_session_identity_is_reported_as_unavailable(self):
        with patch.object(autostart_win, "_session_value", side_effect=["", ""]):
            with self.assertRaisesRegex(OSError, "signed-in account"):
                autostart_win.current_user()

    def test_on_then_off(self):
        fake = FakeSchtasks()
        with patch.object(autostart_win, "can_run_for_current_user", return_value=True):
            autostart_win.set_enabled(True, EXE, user="STUDIO-PC\\alex", run=fake)
        self.assertIn("--hidden", fake.xml)
        self.assertTrue(autostart_win.is_enabled(fake, "STUDIO-PC\\alex"))
        autostart_win.set_enabled(False, EXE, user="STUDIO-PC\\alex", run=fake)
        self.assertFalse(autostart_win.is_enabled(fake, "STUDIO-PC\\alex"))

    def test_off_when_absent_touches_nothing(self):
        fake = FakeSchtasks()
        autostart_win.set_enabled(False, EXE, user="u", run=fake)
        self.assertEqual(fake.calls, ["/query", "/query"])

    def test_refusal_is_raised(self):
        fake = FakeSchtasks(fail="ERROR: Access is denied.")
        with patch.object(autostart_win, "can_run_for_current_user", return_value=True):
            with self.assertRaises(OSError) as caught:
                autostart_win.set_enabled(True, EXE, user="u", run=fake)
        self.assertIn("Access is denied", str(caught.exception))

    def test_task_name_is_scoped_to_the_signed_in_user(self):
        self.assertEqual(autostart_win.task_name("STUDIO-PC\\alex"), "Beamer-STUDIO-PC-alex")

    def test_refuses_silent_elevation_for_another_users_session(self):
        fake = FakeSchtasks()
        with patch.object(autostart_win, "can_run_for_current_user", return_value=False):
            with self.assertRaisesRegex(OSError, "signed-in account"):
                autostart_win.set_enabled(True, EXE, user="STUDIO-PC\\alex", run=fake)
        self.assertEqual(fake.calls, [])

    def test_uninstall_removes_the_legacy_global_task(self):
        fake = FakeSchtasks(exists=True)
        autostart_win.remove_legacy_task(fake)
        self.assertEqual(fake.calls, ["/query", "/delete"])

    def test_uninstall_leaves_an_absent_legacy_global_task_alone(self):
        fake = FakeSchtasks()
        autostart_win.remove_legacy_task(fake)
        self.assertEqual(fake.calls, ["/query"])

    def test_reading_the_switch_migrates_the_legacy_task_before_reporting_state(self):
        fake = FakeSchtasks(exists=True)
        with patch.object(autostart_win, "installed_exe", return_value=EXE), patch.object(autostart_win, "can_run_for_current_user", return_value=True):
            self.assertTrue(autostart_win.is_enabled(fake, "STUDIO-PC\\alex"))
        self.assertNotIn("Beamer", fake.tasks)
        self.assertLess(fake.calls.index("/create"), fake.calls.index("/delete"))

    def test_enabling_startup_replaces_the_legacy_task_instead_of_adding_a_second(self):
        fake = FakeSchtasks(exists=True)
        with patch.object(autostart_win, "can_run_for_current_user", return_value=True):
            autostart_win.set_enabled(True, EXE, user="STUDIO-PC\\alex", run=fake)
        self.assertEqual(fake.tasks, {autostart_win.task_name("STUDIO-PC\\alex")})


class InstallerTests(unittest.TestCase):
    def test_inno_setup_keeps_app_operations_unelevated(self):
        setup = (Path(__file__).parents[1] / "Beamer-Setup.iss").read_text(encoding="utf-8")
        self.assertIn("PrivilegesRequired=lowest", setup)
        self.assertIn('Flags: unchecked', setup[setup.index('[Tasks]') : setup.index('[Files]')])
        code = setup[setup.index("[Code]") :]
        self.assertIn("Exec(ExpandConstant('{app}\\Beamer.exe'), '--enable-autostart'", code)
        self.assertIn("Invoke-InstallMaintenance.ps1", code)
        self.assertNotIn("ShellExec('runas'", code)
        self.assertIn("ewWaitUntilTerminated", code)
        self.assertIn("ResultCode <> 0", code)
        self.assertIn("RaiseException", code)
        self.assertNotIn("same administrator account", code)
        self.assertNotIn("<Task version=", code)
        self.assertNotIn('/Create /TN', code)
        self.assertNotIn('/delete /tn "Beamer" /f', code)


if __name__ == "__main__":
    unittest.main()
