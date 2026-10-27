"""Denied input stays visible without giving the ordinary app maintenance rights."""

import ctypes
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import desktop_win
import firewall_win
import input_injector
from input_blocker import ForegroundBlocker


class OrdinaryInputTests(unittest.TestCase):
    def setUp(self):
        notified = patch.object(input_injector, "_problem_notified", set())
        notified.start()
        self.addCleanup(notified.stop)

    def test_sendinput_refusal_reports_problem_without_spamming(self):
        notices = []
        with patch.object(input_injector, "user32", SimpleNamespace(SendInput=lambda *a: 0)), \
                patch.object(input_injector, "foreground_blocker", return_value=ForegroundBlocker(process_name="admin.exe", elevated=True)), \
                patch.object(input_injector, "on_problem", notices.append, create=True), \
                patch.object(input_injector, "_problem_at", float("-inf"), create=True), \
                patch.object(ctypes, "get_last_error", return_value=0, create=True):
            for _ in range(3):
                with self.assertRaises(OSError):
                    input_injector._send_input(input_injector.INPUT())
        self.assertEqual(len(notices), 1)
        self.assertIn("admin.exe", notices[0])

    def test_successful_input_has_no_warning(self):
        notices = []
        with patch.object(input_injector, "user32", SimpleNamespace(SendInput=lambda n, *a: n)), \
                patch.object(input_injector, "on_problem", notices.append, create=True):
            input_injector._send_input(input_injector.INPUT())
        self.assertEqual(notices, [])

    def test_gameinput_refusal_delegates_recovery_without_inline_maintenance(self):
        notices, commands = [], []
        with patch.object(input_injector, "foreground_class", return_value="GameInputServiceWindow"), \
                patch.object(input_injector, "foreground_blocker", return_value=ForegroundBlocker(window_class="GameInputServiceWindow")), \
                patch.object(input_injector, "_gameinput_running", False), \
                patch.object(input_injector, "_gameinput_restarted_at", float("-inf")), \
                patch.object(input_injector, "on_problem", notices.append, create=True), \
                patch.object(input_injector, "_problem_at", float("-inf"), create=True), \
                patch.object(subprocess, "run", side_effect=lambda *a, **k: commands.append(a)), \
                patch.object(input_injector.threading, "Thread") as thread:
            self.assertTrue(input_injector.release_gameinput_foreground())
        self.assertEqual(commands, [])
        thread.return_value.start.assert_called_once()
        self.assertEqual(len(notices), 1)

    def test_failed_cursor_placement_reports_problem(self):
        notices = []
        with patch.object(desktop_win, "user32", SimpleNamespace(SetCursorPos=lambda *a: 0)), \
                patch.object(input_injector, "foreground_blocker", return_value=ForegroundBlocker(window_class="AdminWindow", process_name="admin.exe", elevated=True)), \
                patch.object(input_injector, "on_problem", notices.append, create=True), \
                patch.object(input_injector, "_problem_at", float("-inf"), create=True), \
                patch.object(desktop_win, "_refused_under", [None]), \
                patch.object(ctypes, "get_last_error", return_value=0, create=True):
            desktop_win.set_cursor_position(10, 20)
        self.assertEqual(len(notices), 1)


class InstallerOwnedFirewallTests(unittest.TestCase):
    def test_missing_rules_direct_user_to_installer_even_in_elevated_session(self):
        for elevated in (False, True):
            status = firewall_win.FirewallStatus(24820, False, False, ("Private",),
                                                 ("Private",), False, (), elevated)
            advice = firewall_win.advise(status)
            self.assertEqual(advice.action, "check")
            self.assertIn("installer", advice.sentence)


if __name__ == "__main__":
    unittest.main()
