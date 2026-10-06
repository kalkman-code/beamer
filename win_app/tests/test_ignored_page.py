"""The Keyboard page's Stays on this PC list, as far as it can be reached without building the
window: what it does when the list cannot be saved."""

import types
import unittest
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app_config
from core import ignored

try:
    import kvm_bridge_win
except ImportError:  # PySide6 is only in the Windows venv
    kvm_bridge_win = None


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class SetIgnoredTests(unittest.TestCase):
    def window(self, saves):
        shown, updates = [], []
        window = types.SimpleNamespace(
            _config=app_config.default_config(),
            _persist=lambda: saves,
            sender=types.SimpleNamespace(update_config=updates.append),
            _show_ignored=lambda entries, refused="": shown.append((entries, refused)),
        )
        window._config.ignored_inputs = [ignored.button("back")]
        return window, shown, updates

    def test_a_list_that_saves_is_applied(self):
        window, shown, updates = self.window(saves=True)
        entries = [ignored.button("back"), ignored.key(0xAF)]
        kvm_bridge_win.WindowsApplication._set_ignored(window, entries)
        self.assertEqual(window._config.ignored_inputs, entries)
        self.assertEqual(len(updates), 1)
        self.assertEqual(shown, [(entries, "")])

    def test_a_list_that_cannot_be_saved_changes_nothing(self):
        # A 65th entry was applied live and shown as added, then lost at the next start.
        window, shown, updates = self.window(saves=False)
        kvm_bridge_win.WindowsApplication._set_ignored(window, [ignored.button("back"), ignored.key(0xAF)])
        self.assertEqual(window._config.ignored_inputs, [ignored.button("back")])
        self.assertEqual(updates, [])
        self.assertEqual(shown[-1][0], [ignored.button("back")])
        self.assertTrue(shown[-1][1])


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class RecorderTests(unittest.TestCase):
    def setUp(self):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        import capture_win
        import widgets

        self.app = QApplication.instance() or QApplication([])
        self.recorded = []
        self.recorder = widgets.InputRecorder("Add", lambda kind, value: self.recorded.append((kind, value)), capture_win.hook_vk)
        self.recorder._toggle()

    def tearDown(self):
        self.recorder.cancel()

    def key(self, press, vk, scan):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        kind = QEvent.Type.KeyPress if press else QEvent.Type.KeyRelease
        event = QKeyEvent(kind, 0, Qt.KeyboardModifier.NoModifier, scan, vk, 0)
        self.recorder.eventFilter(self.recorder, event)

    def test_right_ctrl_is_recorded_as_right_ctrl(self):
        # What Qt 6 reports for Right Ctrl: nativeVirtualKey 0x11, nativeScanCode 0xE01D.
        self.key(True, 0x11, 0xE01D)
        self.assertEqual(self.recorded, [("key", 0xA3)])

    def test_altgr_is_recorded_as_right_alt_not_its_made_up_left_ctrl(self):
        self.key(True, 0x11, 0x1D)
        self.key(True, 0x12, 0xE038)
        self.assertEqual(self.recorded, [("key", 0xA5)])

    def test_left_ctrl_on_its_own_is_recorded_when_let_go(self):
        self.key(True, 0x11, 0x1D)
        self.assertEqual(self.recorded, [])
        self.key(False, 0x11, 0x1D)
        self.assertEqual(self.recorded, [("key", 0xA2)])

    def test_laid_out_keycap_has_room_for_its_key_and_hint(self):
        # A widget that owns a layout is sized from that layout, not from its heightForWidth, so the
        # keycap's own padding has to live in the layout: without it the hint's last pixels sat
        # under the bottom border ("Click, then press the key" half cut off).
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        self.holder = holder = QWidget()
        column = QVBoxLayout(holder)
        column.addWidget(self.recorder)
        column.addStretch(1)
        holder.resize(280, 400)
        holder.show()
        self.app.processEvents()
        room = self.recorder.width() - 24
        need = self.recorder.key.heightForWidth(room) + self.recorder.hint.heightForWidth(room) + 10 + 16
        self.assertGreaterEqual(self.recorder.height(), need)
        holder.hide()

    def test_hint_gets_the_whole_width_the_box_reserved_for_it(self):
        # Windows' fonts wrapped "Click, then press the key" inside a 110 px hint at 150% while the
        # box had reserved one line at its full width, so "key" sat under the bottom border. A
        # longer hint wraps at the size-hint width on any font, which is what the old layout used.
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        self.recorder.cancel()
        self.holder = holder = QWidget()
        column = QVBoxLayout(holder)
        column.addWidget(self.recorder)
        column.addStretch(1)
        for text in ("Click, then press the key", "Click here, then press the key you want to use as the shortcut"):
            self.recorder.hint.setText(text)
            for width in (240, 280, 420):
                with self.subTest(text=text, width=width):
                    holder.resize(width, 400)
                    holder.show()
                    self.app.processEvents()
                    hint = self.recorder.hint
                    self.assertEqual(hint.width(), self.recorder.width() - 24)
                    self.assertGreaterEqual(hint.height(), hint.heightForWidth(hint.width()))
                    self.assertLessEqual(hint.geometry().bottom(), self.recorder.height() - 8)
        holder.hide()


if __name__ == "__main__":
    unittest.main()
