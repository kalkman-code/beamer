"""The Design page's Style for switch shows the tiles it names, in the real window: built offscreen
from a synthetic config as the README's screenshots build it, which starts no receiver, hooks or
announcer."""

import json
import os
import tempfile
import unittest
from pathlib import Path

try:
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication, QWidget

    import kvm_bridge_win
    from core.tests import responder_harness as harness
    import theme
except ImportError:  # PySide6 is only in the Windows venv
    kvm_bridge_win = None


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class StyleForTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        self.app.setFont(theme.font(theme.TYPE["body"]))
        scratch = Path(__file__).resolve().parents[2] / "renders" / "test-working"
        scratch.mkdir(parents=True, exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=scratch)
        folder = Path(self.folder.name)
        (folder / "config.json").write_text(json.dumps({
            "host": "192.168.1.20", "port": harness.free_port(), "auth_token": "synthetic", "paired_with": "MacBook Pro",
            "mac_host": "192.168.1.10", "mac_return_edge": "left", "shortcut_arrival": True,
        }))
        self.window = kvm_bridge_win.WindowsApplication(folder / "settings.json")
        self.window.full_screen_timer.stop()
        self.window.refresh_timer.stop()
        self.window._select_page("design")
        self.window._refresh_window()

    def tearDown(self):
        self.window.server.stop()
        self.window.close()
        self.window.deleteLater()
        self.folder.cleanup()

    def shown(self):
        return {name for name, choice in (("crossing", self.window.glow_style_choice),
                                          ("switch", self.window.switch_style_choice))
                if not choice.view.isHidden()}

    def test_each_choice_shows_its_own_tiles_every_time(self):
        self.assertEqual(self.shown(), {"crossing"})
        for choice in ("switch", "crossing", "switch", "crossing"):
            self.window.design_mode_choice._buttons[choice].click()
            self.app.processEvents()
            self.assertEqual(self.window.design_mode_choice.value, choice)
            self.assertEqual(self.shown(), {choice})

    def test_style_hint_is_visible(self):
        self.assertEqual(self.window.style_preview_hint.text(), "Hover to preview")

    def test_selected_marker_is_in_tile_grab(self):
        from PySide6.QtGui import QColor

        choice = self.window.glow_style_choice
        choice.set_value("beam")
        self.app.processEvents()
        tile = choice.tile("beam")
        image = tile.grab().toImage()
        sample = image.pixelColor(image.width() // 2, 1)
        signal = QColor(theme.colour("signal"))
        distance = sum(abs(a - b) for a, b in zip(
            (sample.red(), sample.green(), sample.blue()), (signal.red(), signal.green(), signal.blue())))
        self.assertLess(distance, 60,
                        "the selected-tile edge is not visible in the tile's own grab")

    def test_hover_edge_is_neutral(self):
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QColor, QEnterEvent

        choice = self.window.glow_style_choice
        tile = choice.tile("beam")
        self.app.sendEvent(tile, QEnterEvent(QPointF(1, 1), QPointF(1, 1), QPointF(1, 1)))
        self.app.processEvents()
        image = tile.grab().toImage()
        sample = image.pixelColor(image.width() // 2, 1)
        signal = QColor(theme.colour("signal"))
        distance = sum(abs(a - b) for a, b in zip(
            (sample.red(), sample.green(), sample.blue()), (signal.red(), signal.green(), signal.blue())))
        self.assertGreater(distance, 60, "the hovered-tile edge must not use the selection accent")

    def test_style_families_stay_horizontal_and_wrap_at_spec_widths(self):
        choice = self.window.glow_style_choice
        self.window.show()
        for width in (320, 426, 512, 640, 720, 900, 1440, 1920):
            self.window.resize(width, 900)
            for _ in range(6):
                self.app.processEvents()
            column = self.window.stack.currentWidget().widget().findChild(QWidget, "page_column")
            per_row = 3 if column.width() >= 600 else 2 if column.width() >= 400 else 1
            group_views = list(choice.group_views.values())
            self.assertEqual(len({view.x() for view in group_views}), 1)
            y_positions = [view.y() for view in group_views]
            self.assertEqual(y_positions, sorted(y_positions))
            for title, group in choice.groups.items():
                rows = {}
                for tile in group._tiles.values():
                    point = tile.mapTo(group.view, QPoint())
                    rows.setdefault(point.y(), []).append(tile)
                with self.subTest(width=width, family=title):
                    self.assertEqual(max(map(len, rows.values())), per_row)
                    self.assertLessEqual(max(tile.width() for tile in group._tiles.values()) - min(tile.width() for tile in group._tiles.values()), 1)
                    for row in rows.values():
                        self.assertLessEqual(max(tile.width() for tile in row) - min(tile.width() for tile in row), 1)
                        self.assertEqual(len({tile.height() for tile in row}), 1)
                    for tile in group._tiles.values():
                        preview = tile.layout().itemAt(0).widget()
                        self.assertAlmostEqual(preview.width() / preview.height(), 2.0, delta=0.03)


if __name__ == "__main__":
    unittest.main()
