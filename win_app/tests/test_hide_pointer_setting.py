"""Hide the pointer on the machine not in use: the switch, its saved value, and what the window hides
and shows through the desktop module, with a recording stand-in for the real cursors."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

try:
    from PySide6.QtWidgets import QApplication
    import kvm_bridge_win
    import theme
    from core.tests import responder_harness as harness
except ImportError:  # PySide6 is only in the Windows venv
    kvm_bridge_win = None


class RecordingDesktop:
    CAN_HIDE_POINTER = True

    def __init__(self):
        self.calls = []

    def hide_pointer(self, reason):
        self.calls.append(("hide", reason))

    def show_pointer(self, reason):
        self.calls.append(("show", reason))

    def restore_pointer(self):
        self.calls.append(("restore",))

    def set_click_through(self, hwnd, through):
        self.calls.append(("click_through", through))

    def cursor_position(self):
        return (10, 10)


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class HidePointerSettingTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        folder = Path(tempfile.mkdtemp())
        self.config = folder / "settings.json"
        raw = {"host": "192.0.2.20", "port": harness.free_port(), "auth_token": "synthetic", "paired_with": "MacBook Pro",
               "mac_host": "192.0.2.10", "mac_return_edge": "left"}
        (folder / "config.json").write_text(json.dumps(raw))
        self.desktop = RecordingDesktop()
        patch = mock.patch.object(kvm_bridge_win, "desktop_win", self.desktop)
        patch.start()
        self.addCleanup(patch.stop)
        self.window = kvm_bridge_win.WindowsApplication(self.config)
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.server.stop)
        self.window._idle_pointer._settle = 60

    def test_defaults_on_and_persists(self):
        self.assertTrue(self.window._config.hide_idle_pointer)
        self.assertTrue(self.window.pointer_switch.isChecked())
        self.window.pointer_switch.setChecked(False)
        self.assertFalse(json.loads(self.config.read_text())["hide_idle_pointer"])

    def test_driving_hides_this_pcs_pointer_and_coming_home_shows_it(self):
        self.window._on_sender_redirecting(True)
        self.window._on_sender_redirecting(False)
        driving = [call for call in self.desktop.calls if call[:2] in (("hide", "driving"), ("show", "driving"))]
        self.assertEqual(driving[-2:], [("hide", "driving"), ("show", "driving")])

    def test_on_windows_a_veil_covers_an_apps_own_cursor_while_driving(self):
        with mock.patch.object(kvm_bridge_win.sys, "platform", "win32"):
            self.window._on_sender_redirecting(True)
            self.app.processEvents()
            self.assertTrue(self.window._veil.isVisible())
            self.assertIn(("click_through", False), self.desktop.calls)
            self.window._on_sender_redirecting(False)
            # Click-through at once, before the queued hide has run.
            self.assertEqual(self.desktop.calls[-1], ("click_through", True))
            self.app.processEvents()
            self.assertFalse(self.window._veil.isVisible())

    def test_switched_off_no_veil_is_shown(self):
        self.window.pointer_switch.setChecked(False)
        with mock.patch.object(kvm_bridge_win.sys, "platform", "win32"):
            self.window._on_sender_redirecting(True)
            self.app.processEvents()
        self.assertTrue(self.window._veil is None or not self.window._veil.isVisible())

    def test_a_driver_letting_go_hides_it_until_a_driver_returns(self):
        self.window._on_owner("peer")
        self.window._on_owner("")
        self.assertEqual(self.desktop.calls[-1], ("hide", "idle"))
        self.window._on_owner("peer")
        self.assertEqual(self.desktop.calls[-1], ("show", "idle"))

    def test_switched_off_nothing_is_hidden_and_what_was_hidden_comes_back(self):
        self.window._on_owner("peer")
        self.window._on_owner("")
        self.window.pointer_switch.setChecked(False)
        self.assertIn(("show", "idle"), self.desktop.calls)
        self.desktop.calls.clear()
        self.window._on_sender_redirecting(True)
        self.window._on_owner("peer")
        self.window._on_owner("")
        self.assertNotIn("hide", [call[0] for call in self.desktop.calls])

    def test_same_on_all_machines_leaves_it_alone(self):
        self.assertNotIn("hide_idle_pointer", json.dumps(self.window._same_fields(), default=str))

    def test_an_earlier_runs_hidden_pointer_is_given_back_at_start(self):
        self.assertEqual(self.desktop.calls[0], ("restore",))

    def test_wayland_explains_idle_hiding_without_offering_an_ineffective_switch(self):
        from PySide6.QtWidgets import QLabel

        self.desktop.CAN_HIDE_POINTER = False
        self.window._idle_pointer.cancel()
        self.window._idle_pointer = self.window._make_idle_pointer()
        with mock.patch.object(kvm_bridge_win.platform_parts, "WAYLAND", True):
            module = self.window._input_module()
        self.addCleanup(module.deleteLater)
        self.assertIsNone(self.window._idle_pointer)
        self.assertTrue(self.window.pointer_switch.isHidden())
        notes = [label for label in module.findChildren(QLabel)
                 if "cannot hide the pointer after another machine stops driving" in label.text()]
        self.assertEqual(len(notes), 1)
        self.assertFalse(notes[0].isHidden())
        self.desktop.calls.clear()
        self.window._on_sender_redirecting(True)
        self.window._on_sender_redirecting(False)
        self.window._on_owner("peer")
        self.window._on_owner("")
        self.assertEqual(self.desktop.calls, [])

    def test_x11_keeps_the_existing_pointer_setting_without_the_wayland_limitation(self):
        from PySide6.QtWidgets import QLabel

        with mock.patch.object(kvm_bridge_win.platform_parts, "WAYLAND", False):
            module = self.window._input_module()
        self.addCleanup(module.deleteLater)
        self.assertFalse(self.window.pointer_switch.isHidden())
        notes = [label for label in module.findChildren(QLabel)
                 if "cannot hide the pointer after another machine stops driving" in label.text()]
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].isHidden())


if __name__ == "__main__":
    unittest.main()
