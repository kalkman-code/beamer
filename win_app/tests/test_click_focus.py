"""Spec section 6: a click never leaves a focus ring; Tab, Shift-Tab and the arrows show one.

A ring is anything the control draws differently with focus than without, so each check grabs the
control focused and again with focus cleared, offscreen, and compares the two."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "win_app"))
sys.path.insert(0, str(ROOT / "win_app" / "tests"))

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton, QWidget

import pages_win
import theme
import widgets
from test_settings_layout_acceptance import SyntheticWindow, write_synthetic_settings

PAGE_KEYS = tuple(key for key, _name, _purpose in pages_win.PAGES)


class ClickFocusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        cls.app.setFont(theme.font(theme.TYPE["body"]))
        cls.app.setStyleSheet(theme.stylesheet())

    def setUp(self):
        scratch = ROOT / "renders" / "test-working"
        scratch.mkdir(parents=True, exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(prefix="beamer-click-focus-", dir=scratch)
        self.window = SyntheticWindow(write_synthetic_settings(Path(self.folder.name)))
        self.window.full_screen_timer.stop()
        self.window.refresh_timer.stop()
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.window.resize(1100, 900)
        self.window.show()
        self.window.activateWindow()
        QApplication.setActiveWindow(self.window)
        self.settle()

    def tearDown(self):
        self.window.server.stop()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.folder.cleanup()

    def settle(self):
        for _ in range(5):
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
            self.app.processEvents()

    def ring_drawn(self, widget):
        self.settle()
        self.assertTrue(widget.hasFocus(), f"{widget!r} should hold focus for this check")
        focused = widget.grab().toImage()
        widget.clearFocus()
        self.settle()
        plain = widget.grab().toImage()
        widget.setFocus(Qt.FocusReason.OtherFocusReason)
        self.settle()
        return focused != plain

    def press(self, widget):
        """A mouse press that takes focus, released off the control so nothing is triggered."""
        QTest.mousePress(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         widget.rect().center())
        QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                           QPoint(-50, -50))
        self.settle()

    def visible(self, kind, page, test=lambda _widget: True):
        self.window._select_page(page)
        self.settle()
        for widget in self.window.findChildren(kind):
            if widget.isVisibleTo(self.window) and widget.focusPolicy() & Qt.FocusPolicy.ClickFocus and test(widget):
                return widget
        return None

    def controls(self):
        found = {}
        kinds = {
            "choice": (QPushButton, lambda w: w.property("vernier") == "choice"),
            "tile": (widgets._Tile, lambda _w: True),
            "swatch": (widgets._Swatch, lambda _w: True),
            "switch": (widgets.Switch, lambda _w: True),
            "remove": (QPushButton, lambda w: w.property("vernier") == "remove"),
            "primary": (QPushButton, lambda w: w.property("vernier") == "primary"),
            "button": (QPushButton, lambda w: w.property("vernier") in (None, "", "small")),
            "keycap": (QPushButton, lambda w: w.property("vernier") == "keycap"),
        }
        for name, (kind, test) in kinds.items():
            for page in PAGE_KEYS:
                widget = self.visible(kind, page, test)
                if widget is not None:
                    found[name] = (page, widget)
                    break
        if "remove" not in found:
            # Remove buttons exist only beside ignored inputs, which the synthetic settings lack.
            remove = QPushButton("Remove", self.window)
            remove.setProperty("vernier", "remove")
            remove.move(8, 8)
            remove.show()
            found["remove"] = (PAGE_KEYS[0], remove)
        return found

    def test_a_press_on_each_control_type_leaves_no_ring(self):
        found = self.controls()
        self.assertTrue({"choice", "tile", "swatch", "switch", "remove", "button"} <= set(found), sorted(found))
        for name, (page, widget) in found.items():
            with self.subTest(control=name, page=page):
                self.window._select_page(page)
                self.settle()
                self.press(widget)
                if widget.hasFocus():
                    self.assertFalse(self.ring_drawn(widget), name)

    def test_tab_shows_a_ring_and_the_next_press_clears_it(self):
        page, choice = self.controls()["choice"]
        self.window._select_page(page)
        self.settle()
        self.press(choice)
        choice.setFocus(Qt.FocusReason.OtherFocusReason)
        QTest.keyClick(choice, Qt.Key.Key_Tab)
        self.settle()
        tabbed = QApplication.focusWidget()
        self.assertIsNotNone(tabbed)
        self.assertIsNot(tabbed, choice)
        if not isinstance(tabbed, QLineEdit):
            self.assertTrue(self.ring_drawn(tabbed), repr(tabbed))
        QTest.keyClick(tabbed, Qt.Key.Key_Backtab)
        self.settle()
        back = QApplication.focusWidget()
        self.assertTrue(self.ring_drawn(back), repr(back))
        words = next(w for w in self.window.findChildren(QLabel)
                     if w.isVisibleTo(self.window) and w.focusPolicy() == Qt.FocusPolicy.NoFocus and w.width() > 20)
        self.press(words)
        self.assertIs(QApplication.focusWidget(), back, "a press on a label keeps focus where it was")
        self.assertFalse(self.ring_drawn(back), "but ends keyboard navigation")
        QTest.keyClick(back, Qt.Key.Key_Tab)
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Backtab)
        self.settle()
        self.assertTrue(self.ring_drawn(QApplication.focusWidget()))
        self.press(choice)
        if choice.hasFocus():
            self.assertFalse(self.ring_drawn(choice))
        back.setFocus(Qt.FocusReason.OtherFocusReason)
        self.settle()
        self.assertFalse(self.ring_drawn(back), "a press ends keyboard navigation everywhere")

    def test_other_keys_after_a_press_leave_the_ring_off(self):
        page, choice = self.controls()["choice"]
        self.window._select_page(page)
        self.settle()
        self.press(choice)
        choice.setFocus(Qt.FocusReason.OtherFocusReason)
        QTest.keyClick(choice, Qt.Key.Key_A)
        QTest.keyClick(choice, Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier)
        self.settle()
        self.assertFalse(self.ring_drawn(choice))

    def test_arrows_count_as_navigation(self):
        self.assertTrue(widgets.is_navigation_key(Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier))
        self.assertTrue(widgets.is_navigation_key(Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier))
        self.assertFalse(widgets.is_navigation_key(Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier))
        self.assertFalse(widgets.is_navigation_key(Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier))

    def test_a_line_edit_keeps_its_ring_while_typing(self):
        for page in PAGE_KEYS:
            field = self.visible(QLineEdit, page, lambda w: w.property("vernier") != "pair-code" and w.isEnabled())
            if field is not None:
                break
        self.assertIsNotNone(field)
        self.press(field)
        self.assertTrue(field.hasFocus())
        self.assertTrue(self.ring_drawn(field))


if __name__ == "__main__":
    unittest.main()
