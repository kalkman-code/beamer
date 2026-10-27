"""Scrolling past settings must not edit them until the control has focus."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDoubleSpinBox, QLineEdit, QScrollArea,
    QSlider, QSpinBox, QVBoxLayout, QWidget,
)

import diagram
import widgets


class ScrollWheelFocusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        widgets.keyboard_navigation()

    def setUp(self):
        self.dialog = QDialog()
        layout = QVBoxLayout(self.dialog)
        self.scroll = QScrollArea()
        layout.addWidget(self.scroll)
        self.page = QWidget()
        self.page.setFixedSize(400, 1600)
        self.scroll.setWidget(self.page)
        self.other = QLineEdit(self.page)
        self.other.setGeometry(20, 650, 240, 30)
        self.dialog.resize(500, 300)

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

    def prepare(self, control):
        control.setParent(self.page)
        control.setGeometry(20, 700, 240, 80)
        if isinstance(control, QComboBox):
            control.addItems(["First", "Second", "Third"])
            control.setCurrentIndex(1)
        else:
            control.setRange(0, 100)
            control.setValue(50)
        control.show()
        self.dialog.show()
        self.dialog.activateWindow()
        self.app.processEvents()
        self.scroll.verticalScrollBar().setValue(600)
        self.other.setFocus()
        self.app.processEvents()
        self.assertTrue(self.other.hasFocus())
        self.assertFalse(control.hasFocus())
        return control

    def value(self, control):
        return control.currentIndex() if isinstance(control, QComboBox) else control.value()

    def wheel(self, control, *, pixels=False):
        point = control.rect().center()
        event = QWheelEvent(
            QPointF(point), QPointF(control.mapToGlobal(point)),
            QPoint(0, -30) if pixels else QPoint(), QPoint(0, -120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate if pixels else Qt.ScrollPhase.NoScrollPhase, False,
        )
        self.app.sendEvent(control, event)
        self.app.processEvents()

    def assert_scrolls_without_editing(self, control, **kwargs):
        value = self.value(control)
        position = self.scroll.verticalScrollBar().value()
        self.wheel(control, **kwargs)
        self.assertEqual(self.value(control), value)
        self.assertFalse(control.hasFocus(), "the wheel must not acquire focus")
        self.assertGreater(self.scroll.verticalScrollBar().value(), position)

    def test_unfocused_controls_scroll_the_dialog_instead_of_editing(self):
        factories = (
            lambda: QSlider(Qt.Orientation.Horizontal),
            lambda: widgets.Ruler("Shortcut", 100),
            lambda: widgets.Ruler("Length", 100, ticks=(0, 50, 100)),
            diagram.PushStrip, QSpinBox, QDoubleSpinBox, QComboBox,
        )
        for factory in factories:
            control = self.prepare(factory())
            with self.subTest(control=type(control).__name__):
                self.assert_scrolls_without_editing(control)
            control.deleteLater()
            self.app.processEvents()

    def test_touchpad_scroll_leaves_unfocused_slider_unchanged(self):
        control = self.prepare(widgets.Ruler("Length", 100))
        self.assert_scrolls_without_editing(control, pixels=True)

    def test_native_wheel_does_not_acquire_focus_or_scroll_twice(self):
        for factory in (lambda: widgets.Ruler("Length", 100), QSpinBox, QComboBox):
            control = self.prepare(factory())
            with self.subTest(control=type(control).__name__):
                value = self.value(control)
                point = control.mapTo(self.dialog, control.rect().center())
                QTest.wheelEvent(self.dialog.windowHandle(), QPointF(point), QPoint(0, -120))
                self.app.processEvents()
                self.assertEqual(self.value(control), value)
                self.assertTrue(self.other.hasFocus())
                distance = self.scroll.verticalScrollBar().value() - 600
                self.assertGreater(distance, 0)
                self.scroll.verticalScrollBar().setValue(600)
                viewport = self.scroll.viewport()
                point = viewport.mapTo(self.dialog, QPoint(350, 100))
                QTest.wheelEvent(self.dialog.windowHandle(), QPointF(point), QPoint(0, -120))
                self.app.processEvents()
                self.assertEqual(self.scroll.verticalScrollBar().value() - 600, distance)
            control.deleteLater()
            self.app.processEvents()

    def test_focused_controls_take_the_wheel_and_stop_after_focus_moves(self):
        for factory in (lambda: widgets.Ruler("Length", 100), diagram.PushStrip,
                        QSpinBox, QDoubleSpinBox, QComboBox):
            control = self.prepare(factory())
            with self.subTest(control=type(control).__name__):
                control.setFocus(Qt.FocusReason.TabFocusReason)
                self.assertTrue(control.hasFocus())
                before = self.value(control)
                position = self.scroll.verticalScrollBar().value()
                self.wheel(control)
                self.assertNotEqual(self.value(control), before)
                self.assertEqual(self.scroll.verticalScrollBar().value(), position)
                self.other.setFocus()
                self.assert_scrolls_without_editing(control)
            control.deleteLater()
            self.app.processEvents()

    def test_tab_and_arrow_keys_still_edit_the_slider(self):
        control = self.prepare(widgets.Ruler("Length", 100))
        QWidget.setTabOrder(self.other, control)
        QTest.keyClick(self.other, Qt.Key.Key_Tab)
        self.assertTrue(control.hasFocus())
        QTest.keyClick(control, Qt.Key.Key_Right)
        self.assertEqual(control.value(), 60)
        self.wheel(control)
        self.assertNotEqual(control.value(), 60)

    def test_click_focus_allows_wheel_on_custom_sliders(self):
        for factory in (lambda: widgets.Ruler("Length", 100, ticks=(0, 50, 100)),
                        diagram.PushStrip):
            control = self.prepare(factory())
            with self.subTest(control=type(control).__name__):
                QTest.mouseClick(control, Qt.MouseButton.LeftButton,
                                 pos=QPoint(control.width() // 2, 25))
                self.assertTrue(control.hasFocus())
                value = control.value()
                self.wheel(control)
                self.assertNotEqual(control.value(), value)
            control.deleteLater()
            self.app.processEvents()

    def test_spin_box_editor_does_not_swallow_unfocused_wheel(self):
        control = self.prepare(QSpinBox())
        value = control.value()
        position = self.scroll.verticalScrollBar().value()
        self.wheel(control.lineEdit())
        self.assertEqual(control.value(), value)
        self.assertGreater(self.scroll.verticalScrollBar().value(), position)

    def test_slider_outside_scroll_area_keeps_its_wheel_behaviour(self):
        control = QSlider(Qt.Orientation.Horizontal, self.dialog)
        self.dialog.layout().addWidget(control)
        control.setValue(50)
        self.dialog.show()
        self.dialog.activateWindow()
        self.other.setFocus()
        self.app.processEvents()
        self.wheel(control)
        self.assertNotEqual(control.value(), 50)

    def test_all_settings_pages_protect_their_wheel_controls(self):
        import theme
        from test_settings_layout_acceptance import SyntheticWindow, write_synthetic_settings

        theme.init_fonts()
        with tempfile.TemporaryDirectory() as folder:
            window = SyntheticWindow(write_synthetic_settings(Path(folder)))
            try:
                window.full_screen_timer.stop()
                window.refresh_timer.stop()
                window.resize(1100, 500)
                window.show()
                window.activateWindow()
                found = set()
                for key in ("overview", "crossing", "keyboard", "design", "connection"):
                    window._select_page(key)
                    self.app.processEvents()
                    area = window.stack.currentWidget()
                    controls = [control for control in area.findChildren(QWidget)
                                if isinstance(control, (QSlider, QComboBox, QSpinBox, QDoubleSpinBox))]
                    for control in controls:
                        with self.subTest(page=key, control=control.accessibleName()):
                            self.assertNotEqual(control.focusPolicy(), Qt.FocusPolicy.WheelFocus)
                            if not control.isVisibleTo(window) or not control.isEnabled():
                                continue
                            found.add(key)
                            area.ensureWidgetVisible(control)
                            control.clearFocus()
                            bar = area.verticalScrollBar()
                            before = bar.value()
                            value = self.value(control)
                            event = QWheelEvent(
                                QPointF(control.rect().center()),
                                QPointF(control.mapToGlobal(control.rect().center())),
                                QPoint(), QPoint(0, 120 if before else -120),
                                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                Qt.ScrollPhase.NoScrollPhase, False)
                            self.app.sendEvent(control, event)
                            self.assertEqual(self.value(control), value)
                            if bar.maximum() > 0:
                                self.assertNotEqual(bar.value(), before)
                self.assertEqual(found, {"crossing", "keyboard", "design"})
            finally:
                window.server.stop()
                window.close()
                window.deleteLater()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
