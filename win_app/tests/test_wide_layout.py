import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QWidget

import diagram
import theme
import widgets


class WideLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        cls.app.setFont(theme.font(theme.TYPE["body"]))

    def test_wrapped_paragraphs_use_the_layout_width_for_their_measure(self):
        paragraph = widgets.label("A long settings explanation.", "note", wrap=True)

        self.assertGreater(paragraph.maximumWidth(), 560)

    def test_the_arrangement_diagram_uses_more_width_on_a_wide_page(self):
        drawing = diagram.ArrangementDiagram()
        drawing.set_machines(
            [{"key": "desk", "label": "Desk", "side": "right", "methods": ["edge"],
              "parts": [], "chosen": True}],
            "Right Ctrl", "hold", True,
        )

        narrow = drawing._drawing(640.0)[0][2]
        wide = drawing._drawing(1440.0)[0][2]
        self.assertGreater(wide, narrow)


class WindowsPageWidthTest(unittest.TestCase):
    """One centred 880-DIP section column and horizontal style-family rows."""

    WIDTHS = (320, 426, 512, 640, 720, 900, 1100, 1440, 1920)

    def setUp(self):
        import tempfile
        from pathlib import Path

        import app_config
        import kvm_bridge_win
        import pages_win
        from core import protocol
        from core.tests import responder_harness as harness

        self.pages = pages_win
        self.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        self.app.setFont(theme.font(theme.TYPE["body"]))
        scratch = Path(__file__).resolve().parents[2] / "renders" / "test-working"
        scratch.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "settings.json"
        settings = app_config.migrate(None, machine_id=protocol.id_text(harness.HERE))
        settings["peers"] = [harness.entry(harness.B, "Mac", side="right")]
        settings["port"] = harness.free_port()
        app_config.write_settings(path, settings)
        self.window = kvm_bridge_win.WindowsApplication(path)
        self.window.full_screen_timer.stop()
        self.window.refresh_timer.stop()
        self.window._send_to = lambda peer, message: True
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.server.stop)

    def column_width(self, key):
        self.window._select_page(key)
        # A scrollbar that comes or goes narrows the viewport, which refits the column: let that settle.
        for _ in range(5):
            self.app.processEvents()
        scroll = self.window.stack.currentWidget()
        column = scroll.widget().findChild(QWidget, "page_column")
        return column.width()

    def test_every_page_uses_the_same_centred_column_and_section_edges(self):
        for width in self.WIDTHS:
            self.window.resize(width, 900)
            self.window.show()
            for _ in range(6):
                self.app.processEvents()
            self.assertEqual(self.window.width(), width)
            for key, _, _ in self.pages.PAGES:
                with self.subTest(width=width, page=key):
                    self.column_width(key)
                    scroll = self.window.stack.currentWidget()
                    page = scroll.widget()
                    column = page.findChild(QWidget, "page_column")
                    expected = min(880, page.width() - 48)
                    self.assertAlmostEqual(column.width(), expected, delta=1)
                    self.assertAlmostEqual(column.x(), (page.width() - expected) / 2, delta=1)
                    sections = [widget for widget in page.findChildren(QWidget)
                                if widget.property("vernier") == "module" and widget.isVisible()]
                    self.assertTrue(sections)
                    for section in sections:
                        self.assertAlmostEqual(section.mapTo(page, QPoint()).x(), column.x(), delta=1)
                        self.assertAlmostEqual(section.width(), column.width(), delta=1)
                    self.assertLessEqual(page.width(), scroll.viewport().width() + 1)

    def test_the_crossing_diagram_sits_beside_its_ways_once_the_column_holds_both(self):
        # Both blocks at their minimums, with the gap, must fit before they sit beside each other.
        for (width, height) in ((1366, 900), (1920, 1000), (2560, 1440), (1440, 3440)):
            with self.subTest(window=(width, height)):
                self.window.resize(width, height)
                self.window.show()
                self.column_width("crossing")
                pair = self.window.ways_pair
                self.assertGreaterEqual(pair.width(), pair.needed())
                self.assertTrue(pair.beside())
                self.assertEqual(pair.first.y(), pair.second.y())
                self.assertLess(pair.first.x(), pair.second.x())
                self.assertIs(pair.first.parentWidget(), pair)
                self.assertIs(pair.second.parentWidget(), pair)
                self.assertTrue(pair.first.isVisible() and pair.second.isVisible())

    def test_the_crossing_pair_stacks_below_the_width_both_need(self):
        self.window.resize(320, 900)
        self.window.show()
        self.column_width("crossing")
        pair = self.window.ways_pair
        self.assertLess(pair.width(), pair.needed())
        self.assertFalse(pair.beside())
        self.assertLess(pair.first.y(), pair.second.y())

    def test_style_families_form_horizontal_equal_tile_rows_at_each_breakpoint(self):
        for width in self.WIDTHS:
            with self.subTest(width=width):
                self.window.resize(width, 900)
                self.window.show()
                self.column_width("design")
                for _ in range(6):
                    self.app.processEvents()
                column = self.window.stack.currentWidget().widget().findChild(QWidget, "page_column")
                per_row = 3 if column.width() >= 600 else 2 if column.width() >= 400 else 1
                family_views = list(self.window.glow_style_choice.group_views.values())
                family_x = [view.x() for view in family_views]
                family_y = [view.y() for view in family_views]
                self.assertEqual(len(set(family_x)), 1, f"families must share a leading edge: {family_x}")
                self.assertEqual(family_y, sorted(family_y), "families must follow catalogue order vertically")
                for title, choices in self.window.glow_style_choice.groups.items():
                    positions = {}
                    for tile in choices._tiles.values():
                        point = tile.mapTo(choices.view, QPoint())
                        positions.setdefault(point.y(), []).append(tile)
                    self.assertEqual(max(map(len, positions.values())), per_row,
                                     f"{title} should have {per_row} tiles per row")
                    for row in positions.values():
                        self.assertLessEqual(max(tile.width() for tile in row) - min(tile.width() for tile in row), 1)
                        self.assertEqual(len({tile.height() for tile in row}), 1)
                    self.assertLessEqual(max(tile.width() for tile in choices._tiles.values()) - min(tile.width() for tile in choices._tiles.values()), 1)

    def test_unfixed_choice_tiles_keep_sharing_their_row(self):
        choices = widgets.ChoiceTiles(
            (("one", "One", "First", QWidget()), ("two", "Two", "Second", QWidget())), "one"
        )
        choices.view.resize(400, 120)
        choices.view.show()
        self.app.processEvents()
        widths = [tile.width() for tile in choices._tiles.values()]
        self.assertGreater(min(widths), 96)
        self.assertAlmostEqual(widths[0], widths[1], delta=1)
        self.assertEqual(sum(widths) + 10, choices.view.width())
        choices.view.deleteLater()

    def test_every_page_column_fills_a_window_too_narrow_for_the_minimum(self):
        for width in (320, 426, 512):
            self.window.resize(width, 700)
            self.window.show()
            for key, _, _ in self.pages.PAGES:
                with self.subTest(width=width, page=key):
                    self.column_width(key)
                    scroll = self.window.stack.currentWidget()
                    page = scroll.widget()
                    column = page.findChild(QWidget, "page_column")
                    self.assertAlmostEqual(column.width(), page.width() - 48, delta=1)


if __name__ == "__main__":
    unittest.main()
