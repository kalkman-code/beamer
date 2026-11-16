import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import admin_mode
import autostart_win


def setUpModule():
    preference = patch.object(admin_mode, "enabled", return_value=False)
    preference.start()
    unittest.addModuleCleanup(preference.stop)


class OrdinaryAutostartTests(unittest.TestCase):
    def test_registered_task_runs_at_users_normal_privilege(self):
        root = ET.fromstring(autostart_win.task_xml(r"C:\A & B\Beamer.exe", r"PC\user"))
        self.assertEqual(root.findtext("{*}Principals/{*}Principal/{*}RunLevel"), "LeastPrivilege")
        self.assertEqual(root.findtext("{*}Principals/{*}Principal/{*}LogonType"), "InteractiveToken")

    def test_highest_task_cannot_count_as_a_completed_migration(self):
        exe = r"C:\Beamer\Beamer.exe"
        xml = autostart_win.task_xml(exe, r"PC\user").replace("LeastPrivilege", "HighestAvailable")
        self.assertFalse(autostart_win._replacement_ready(ET.fromstring(xml), exe, "--hidden"))

    def test_an_ordinary_signed_in_account_can_enable_startup(self):
        with patch.object(autostart_win, "current_user", return_value=r"PC\standard"), \
                patch.object(autostart_win, "_process_user", return_value=r"PC\standard"):
            self.assertTrue(autostart_win.can_run_for_current_user())


if __name__ == "__main__":
    unittest.main()
