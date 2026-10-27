"""Synthetic Input controls: no task registration or UAC prompts."""

import ctypes
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout

import firewall_win
import input_injector
import kvm_bridge_win as kvm
import widgets
from input_blocker import ForegroundBlocker


class AdminControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_control(self, enabled=False, reason=""):
        import admin_mode
        page = SimpleNamespace(
            _set_admin_mode=Mock(),
            _refresh_admin_mode=lambda: kvm.WindowsApplication._refresh_admin_mode(page),
        )
        host = QWidget()
        self.addCleanup(host.deleteLater)
        module = SimpleNamespace(body=QVBoxLayout(host))
        with patch.object(admin_mode, "enabled", return_value=enabled), \
                patch.object(admin_mode, "unavailable_reason", return_value=reason):
            kvm.WindowsApplication._admin_control(page, module)
        return page, host

    def test_toggle_matches_existing_switch_and_defaults_off(self):
        page, host = self.make_control()
        self.assertIsInstance(page.admin_switch, widgets.Switch)
        self.assertFalse(page.admin_switch.isChecked())
        self.assertTrue(page.admin_switch.isEnabled())
        self.assertEqual(page.admin_switch.text(), "Control administrator windows")
        self.assertIn("this PC", page.admin_note.text())
        host.resize(340, 200)
        host.layout().activate()
        self.assertLessEqual(page.admin_switch.geometry().right(), host.width())

    def test_unavailable_account_or_portable_has_visible_reason(self):
        for reason in ("Use an administrator account on this PC.", "Install Beamer to use this setting."):
            page, _ = self.make_control(reason=reason)
            self.assertFalse(page.admin_switch.isEnabled())
            self.assertEqual(page.admin_note.text(), reason)

    def test_cancelled_request_restores_toggle_without_quitting(self):
        page, _ = self.make_control(enabled=False)
        page.quit = Mock()
        page.admin_switch.setChecked(True)
        kvm.WindowsApplication._on_admin_mode_changed(page, OSError("Administrator permission was cancelled. Nothing changed."))
        self.assertFalse(page.admin_switch.isChecked())
        self.assertIn("cancelled", page.admin_note.text())
        page.quit.assert_not_called()

    def test_success_quits_only_after_helper_completes(self):
        page, _ = self.make_control()
        page.quit = Mock()
        kvm.WindowsApplication._on_admin_mode_changed(page, None)
        page.quit.assert_called_once_with()

    def test_pending_request_keeps_old_state_and_passes_active_pairing_path(self):
        import admin_mode
        page, _ = self.make_control()
        page.config_path = Path(r"C:\Synthetic Profile\settings.json")
        page.bridge = SimpleNamespace(admin_changed=SimpleNamespace(emit=Mock()))
        with patch.object(kvm.threading, "Thread") as thread, \
                patch.object(admin_mode, "change", return_value=True) as change:
            kvm.WindowsApplication._set_admin_mode(page, True)
            self.assertFalse(page.admin_switch.isChecked())
            self.assertFalse(page.admin_switch.isEnabled())
            change.assert_not_called()
            thread.call_args.kwargs["target"]()
        change.assert_called_once_with(True, config_path=page.config_path)
        page.bridge.admin_changed.emit.assert_called_once_with(None)

    def test_no_change_does_not_quit_the_only_receiver(self):
        page, _ = self.make_control()
        page.quit = Mock()
        page._refresh_admin_mode = Mock()
        kvm.WindowsApplication._on_admin_mode_changed(page, False)
        page.quit.assert_not_called()
        page._refresh_admin_mode.assert_called_once_with()


class ElevatedDiagnosticTests(unittest.TestCase):
    def test_elevated_receiver_does_not_blame_equal_integrity_target(self):
        blocker = ForegroundBlocker(process_name="admin.exe", elevated=True, integrity=0x3000)
        notices = []
        with patch.object(input_injector, "is_elevated", return_value=True, create=True), \
                patch.object(input_injector, "on_problem", notices.append), \
                patch.object(input_injector, "_problem_at", float("-inf")), \
                patch.object(input_injector, "_problem_notified", set()):
            input_injector.report_input_problem(blocker)
        self.assertEqual(len(notices), 1)
        self.assertNotIn("running as administrator", notices[0])
        self.assertIn("admin.exe", notices[0])

    def test_elevated_firewall_denial_does_not_ask_for_permission_it_has(self):
        status = firewall_win.FirewallStatus(24820, False, False, (), (), False, (), True,
                                             error="permission-denied")
        self.assertNotIn("without administrator permission", firewall_win.advise(status).sentence)

    def test_medium_replacement_waits_for_high_instance_mutex(self):
        kernel = SimpleNamespace(CreateMutexW=Mock(return_value=None))
        with patch.object(kvm.sys, "platform", "win32"), \
                patch.object(kvm, "_windows_kernel32", return_value=kernel), \
                patch.object(ctypes, "get_last_error", return_value=5, create=True):
            self.assertIsNone(kvm._claim_instance())


if __name__ == "__main__":
    unittest.main()
