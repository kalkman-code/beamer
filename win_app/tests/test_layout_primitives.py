"""Control bounds and responsive rows in the application's actual typeface."""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QAccessible, QAccessibleActionInterface
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

import theme
import widgets


class LayoutPrimitivesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        cls.app.setFont(theme.font(theme.TYPE["body"]))
        cls.app.setStyleSheet(theme.stylesheet())

    def mount(self, view, width):
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(view)
        host.resize(width, 600)
        host.show()
        self.addCleanup(host.deleteLater)
        self.settle()
        return host

    def settle(self):
        for _ in range(8):
            QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
            self.app.processEvents()

    def test_segmented_group_keeps_its_cap_in_a_wide_parent(self):
        choice = widgets.Choice((("a", "Short"), ("b", "Normal"), ("c", "Long")), 3, "a")
        self.mount(choice.view, 880)
        self.assertLessEqual(choice.view.width(), 360)
        self.assertLessEqual(max(button.width() for button in choice._buttons.values()), 120)

    def test_segmented_group_reflows_without_eliding_essential_options(self):
        choice = widgets.Choice((("a", "Same shortcuts"), ("b", "Same positions"),
                                 ("c", "Mac keyboard layout")), 3, "a")
        host = self.mount(choice.view, 160)
        self.assertLessEqual(host.width(), 160)
        self.assertLessEqual(choice.view.width(), 160)
        buttons = list(choice._buttons.values())
        self.assertEqual(len({button.x() for button in buttons}), 1)
        self.assertEqual(len({button.y() for button in buttons}), 3)
        for button in buttons:
            self.assertEqual(button.displayed_text(), button.text())
            self.assertGreaterEqual(button.height(), 30)

    def test_long_switch_wraps_and_keeps_a_constant_track(self):
        switch = widgets.Switch("Hold this PC's edges while an app on it is full screen")
        host = self.mount(switch, 183)
        self.assertLessEqual(host.width(), 183)
        self.assertGreaterEqual(switch.height(), switch.fontMetrics().height() * 2)
        self.assertEqual(switch.TRACK.toTuple(), (34, 18))

    def test_form_row_reserves_the_label_track_before_placing_controls(self):
        choice = widgets.Choice((("a", "Crossing"), ("b", "Shortcut and menu")), 2, "a")
        choice.set_width(240)
        row = widgets.FormRow(widgets.label("Style for", "key"), choice.view)
        host = self.mount(row, 627)
        host.layout().addStretch(1)
        self.settle()
        self.assertEqual(row.heading.width(), 180)
        self.assertGreaterEqual(choice.view.mapTo(row, choice.view.rect().topLeft()).x(), 196)
        self.assertGreaterEqual(row.height(), choice.view.heightForWidth(choice.view.width()))
        for width in (183, 528, 627, 848, 183, 627):
            host.resize(width, 600)
            self.settle()
            point = choice.view.mapTo(row, choice.view.rect().topLeft())
            if width + 32 >= 560:
                self.assertEqual(point.x(), 196)
            else:
                self.assertEqual(point.x(), 0)
                self.assertGreaterEqual(point.y(), row.heading.height() + 8)

    def test_capped_segments_grow_together_to_keep_full_option_text(self):
        choice = widgets.Choice((("a", "Crossing"), ("b", "Shortcut and menu")), 2, "a")
        choice.set_width(240)
        self.mount(choice.view, 627)
        buttons = list(choice._buttons.values())
        self.assertEqual(len({button.height() for button in buttons}), 1)
        for button in buttons:
            self.assertGreaterEqual(button.height(), button.heightForWidth(button.width()))

    def test_style_tiles_expose_checked_state_and_accessible_activation(self):
        choice = widgets.ChoiceTiles(
            [(key, key, "Quiet", QWidget()) for key in ("a", "b", "c")], "a", responsive=True)
        self.mount(choice.view, 848)
        chosen = QAccessible.queryAccessibleInterface(choice.tile("a"))
        other = QAccessible.queryAccessibleInterface(choice.tile("b"))
        self.assertTrue(chosen.state().checked)
        self.assertFalse(other.state().checked)
        self.assertIn(QAccessibleActionInterface.pressAction(), other.actionInterface().actionNames())
        other.actionInterface().doAction(QAccessibleActionInterface.pressAction())
        for _ in range(60):
            if choice.value == "b":
                break
            QTest.qWait(50)
        self.assertEqual(choice.value, "b")
        self.assertTrue(other.state().checked)
        self.assertFalse(chosen.state().checked)

    def test_family_rows_distribute_rounding_remainder_to_the_final_edge(self):
        choice = widgets.ChoiceTiles(
            [(key, key, "Quiet", QWidget()) for key in ("a", "b", "c")], "a", responsive=True)
        self.mount(choice.view, 848)
        tiles = list(choice._tiles.values())
        self.assertEqual(tiles[0].x(), 0)
        self.assertEqual(tiles[-1].x() + tiles[-1].width(), choice.view.width())
        self.assertLessEqual(max(tile.width() for tile in tiles) - min(tile.width() for tile in tiles), 1)

    def test_accessible_toggle_keeps_one_style_selected_and_notifies_once(self):
        changes = []
        choice = widgets.ChoiceTiles(
            [(key, key, "Quiet", QWidget()) for key in ("a", "b", "c")],
            "a", on_change=changes.append, responsive=True)
        self.mount(choice.view, 848)
        other = QAccessible.queryAccessibleInterface(choice.tile("b"))
        other.actionInterface().doAction(QAccessibleActionInterface.toggleAction())
        self.assertEqual(choice.value, "b")
        self.assertEqual([tile.isChecked() for tile in choice._tiles.values()], [False, True, False])
        self.assertEqual(changes, ["b"])
        other.actionInterface().doAction(QAccessibleActionInterface.toggleAction())
        self.assertTrue(choice.tile("b").isChecked())
        self.assertEqual(changes, ["b"])
        choice.set_value("c")
        self.assertEqual(changes, ["b"])

    def test_accessible_colour_toggle_notifies_and_programmatic_change_stays_silent(self):
        changes = []
        choice = widgets.SwatchGroups(
            [("Mono", [(key, key, ["#ffffff", "#888888"]) for key in ("a", "b", "c")])],
            "a", on_change=changes.append)
        self.mount(choice.view, 848)
        other = QAccessible.queryAccessibleInterface(choice._swatches["b"])
        other.actionInterface().doAction(QAccessibleActionInterface.toggleAction())
        self.assertEqual(choice.value, "b")
        self.assertEqual(changes, ["b"])
        choice.set_value("c")
        self.assertEqual(choice.value, "c")
        self.assertEqual(changes, ["b"])

    def test_corrected_breakpoints_settle_identically_in_both_resize_directions(self):
        for kind, count in (("style", 3), ("colour", 5)):
            view = widgets._FamilyChoices(kind)
            for _ in range(count):
                cell = QWidget() if kind == "colour" else widgets._Tile(lambda: None)
                if kind == "style":
                    QVBoxLayout(cell).addWidget(QWidget())
                view.add_cell(cell)
            host = self.mount(view, 848)
            captured = {}
            widths = (399, 400, 401, 599, 600, 601, 879, 880, 881)
            for outer in (*widths, *reversed(widths)):
                host.resize(outer - 32, 600)
                self.settle()
                expected = ((3 if outer >= 600 else 2 if outer >= 400 else 1) if kind == "style"
                            else (5 if outer >= 600 else 3 if outer >= 400 else 2))
                self.assertEqual(sum(cell.y() == 0 for cell in view.cells), expected)
                geometry = tuple(cell.geometry().getRect() for cell in view.cells)
                self.settle()
                self.assertEqual(geometry, tuple(cell.geometry().getRect() for cell in view.cells))
                if outer in captured:
                    self.assertEqual(geometry, captured[outer])
                captured[outer] = geometry
        sidebar = widgets.Sidebar([(key, key, "") for key in ("a", "b")], lambda key: None)
        self.addCleanup(sidebar.deleteLater)
        for width in (639, 640, 641, 799, 800, 801, 801, 800, 799, 641, 640, 639):
            sidebar.set_window_width(width)
            self.assertEqual(sidebar.width(), 56 if width < 640 else 144 if width < 800 else 176)
        row = widgets.FormRow(widgets.label("Style for", "key"), QWidget())
        host = self.mount(row, 848)
        for outer in (559, 560, 561, 561, 560, 559):
            host.resize(outer - 32, 600)
            self.settle()
            self.assertEqual(row._box.direction(), QVBoxLayout.Direction.LeftToRight
                             if outer >= 560 else QVBoxLayout.Direction.TopToBottom)


if __name__ == "__main__":
    unittest.main()
