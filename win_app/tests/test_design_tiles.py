"""Measured Design tile states and geometry, using synthetic settings offscreen."""

import colorsys
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF
    from PySide6.QtGui import QColor, QEnterEvent, QPalette
    from PySide6.QtWidgets import QApplication, QWidget
    import kvm_bridge_win
    import theme
    from test_settings_layout_acceptance import SyntheticWindow, write_synthetic_settings
except ImportError:
    kvm_bridge_win = None


def luminance(colour):
    channels = [channel / 255 for channel in colour.getRgb()[:3]]
    linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
              for value in channels]
    return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))


def accent_pixels(image, points):
    signal = QColor(theme.colour("signal"))
    hue, saturation, _ = colorsys.rgb_to_hsv(*[value / 255 for value in signal.getRgb()[:3]])
    count = 0
    for x, y in points:
        colour = image.pixelColor(x, y)
        pixel_hue, pixel_saturation, _ = colorsys.rgb_to_hsv(*[value / 255 for value in colour.getRgb()[:3]])
        distance = abs(pixel_hue - hue)
        if min(distance, 1 - distance) < .06 and pixel_saturation >= saturation * .65:
            count += 1
    return count


def border_points(image):
    scale = image.devicePixelRatio()
    inset, depth = round(12 * scale), max(1, round(3 * scale))
    width, height = image.width(), image.height()
    return [(x, y) for x in range(inset, width - inset) for y in range(depth)] + [
        (x, y) for x in range(depth) for y in range(inset, height - inset)]


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class DesignTilesTest(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        self.app.setFont(theme.font(theme.TYPE["body"]))
        scratch = Path(__file__).resolve().parents[2] / "renders" / "test-working"
        scratch.mkdir(parents=True, exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=scratch)
        folder = Path(self.folder.name)
        self.window = SyntheticWindow(write_synthetic_settings(folder))
        self.window.full_screen_timer.stop()
        self.window.refresh_timer.stop()
        self.window._select_page("design")
        self.window.show()
        self.choice = self.window.glow_style_choice
        self.settle()

    def tearDown(self):
        self.window.server.stop()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.folder.cleanup()

    def settle(self):
        for _ in range(8):
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
            self.app.processEvents()

    def hover(self, enabled):
        tile = self.choice.tile("beam")
        event = (QEnterEvent(QPointF(1, 1), QPointF(1, 1), QPointF(1, 1)) if enabled
                 else QEvent(QEvent.Type.Leave))
        self.app.sendEvent(tile, event)
        self.settle()

    def capture_tile(self, tile):
        image = self.window.grab().toImage()
        scale = image.devicePixelRatio()
        point = tile.mapTo(self.window, QPoint())
        crop = image.copy(round(point.x() * scale), round(point.y() * scale),
                          round(tile.width() * scale), round(tile.height() * scale))
        crop.setDevicePixelRatio(scale)
        return crop

    def rectangles(self):
        return {value: (tile.mapTo(self.choice.view, QPoint()).x(),
                        tile.mapTo(self.choice.view, QPoint()).y(), tile.width(), tile.height())
                for group in self.choice.groups.values() for value, tile in group._tiles.items()}

    def test_hover_is_neutral_and_selected_has_accent_and_an_extra_cue(self):
        self.window.resize(1200, 900)
        self.choice.set_value("beam")
        self.settle()
        unselected = self.capture_tile(self.choice.tile("glow"))
        self.choice.set_value("glow")
        self.hover(True)
        selected = self.capture_tile(self.choice.tile("glow"))
        hovered = self.capture_tile(self.choice.tile("beam"))
        self.assertEqual(accent_pixels(hovered, border_points(hovered)), 0,
                         "hover's border contains the selection accent")
        self.assertGreater(accent_pixels(selected, border_points(selected)), 20)
        scale = selected.devicePixelRatio()
        badge = [(x, y) for x in range(selected.width() - round(26 * scale),
                                      selected.width() - round(4 * scale))
                 for y in range(round(4 * scale), round(26 * scale))]
        badge_extra = accent_pixels(selected, badge) - accent_pixels(unselected, badge)
        middle = selected.width() // 2
        thicker_ring = accent_pixels(selected, [(middle, y) for y in range(round(4 * scale))])
        self.assertTrue(badge_extra >= 20 * scale * scale or thicker_ring >= round(3 * scale),
                        f"selection has no extra cue: badge delta {badge_extra}, ring pixels {thicker_ring}")

    def test_hover_background_brightens_in_both_appearances(self):
        previous = theme.is_dark()
        tile = self.choice.tile("beam")
        try:
            for dark in (True, False):
                theme.set_dark(dark)
                self.app.setStyleSheet(theme.stylesheet())
                self.hover(False)
                image = self.capture_tile(tile)
                scale = image.devicePixelRatio()
                rest = image.pixelColor(image.width() // 2, round(5 * scale))
                self.hover(True)
                image = self.capture_tile(tile)
                hovered = image.pixelColor(image.width() // 2, round(5 * scale))
                with self.subTest(dark=dark):
                    self.assertGreater(luminance(hovered), luminance(rest))
        finally:
            theme.set_dark(previous)
            self.app.setStyleSheet(theme.stylesheet())

    def test_hint_has_plain_secondary_text_and_accessible_contrast(self):
        hint = self.window.style_preview_hint
        self.assertEqual(hint.text(), "Hover to preview")
        foreground = hint.palette().color(QPalette.ColorRole.WindowText)
        self.assertEqual(foreground.name(), QColor(theme.colour("ink_2")).name())
        values = sorted((luminance(foreground), luminance(QColor(theme.colour("panel")))))
        contrast = (values[1] + .05) / (values[0] + .05)
        self.assertGreaterEqual(contrast, 4.5, f"hint contrast {contrast:.2f}:1")

    def test_tile_dimensions_and_rows_match_at_every_width_and_state(self):
        for width in (320, 426, 512, 640, 720, 900, 1440, 1920):
            self.window.resize(width, 900)
            self.choice.set_value("glow")
            self.hover(False)
            self.settle()
            rest = self.rectangles()
            page = self.window.stack.currentWidget().widget()
            column = page.findChild(QWidget, "page_column")
            per_row = 3 if column.width() >= 600 else 2 if column.width() >= 400 else 1
            with self.subTest(width=width, state="all tile dimensions"):
                self.assertLessEqual(max(rect[2] for rect in rest.values()) - min(rect[2] for rect in rest.values()), 1)
                self.assertEqual(len({rect[3] for rect in rest.values()}), 1)
            with self.subTest(width=width, state="rest"):
                family_views = list(self.choice.group_views.values())
                self.assertEqual(len({view.x() for view in family_views}), 1,
                                 "style families must share one leading edge")
                self.assertEqual([view.y() for view in family_views],
                                 sorted(view.y() for view in family_views),
                                 "style families must follow catalogue order vertically")
                for title, group in self.choice.groups.items():
                    rows = {}
                    for value, tile in group._tiles.items():
                        point = tile.mapTo(group.view, QPoint())
                        rows.setdefault(point.y(), []).append((value, tile))
                    self.assertEqual(max(map(len, rows.values())), per_row,
                                     f"{title} must have {per_row} choices per row")
                    for row in rows.values():
                        self.assertLessEqual(max(tile.width() for _v, tile in row) - min(tile.width() for _v, tile in row), 1)
                        self.assertEqual(len({tile.height() for _v, tile in row}), 1)
                    self.assertLessEqual(max(tile.width() for tile in group._tiles.values()) - min(tile.width() for tile in group._tiles.values()), 1)
            self.hover(True)
            with self.subTest(width=width, state="hover"):
                self.assertEqual(self.rectangles(), rest)
            self.hover(False)
            self.choice.set_value("beam")
            self.settle()
            with self.subTest(width=width, state="selected"):
                self.assertEqual(self.rectangles(), rest)
            for value, tile in ((value, tile) for group in self.choice.groups.values()
                                for value, tile in group._tiles.items()):
                preview = tile.layout().itemAt(0).widget()
                with self.subTest(width=width, tile=value):
                    self.assertAlmostEqual(preview.width() / preview.height(), 2, delta=.03)

    def test_sidebar_has_no_ring_on_a_different_page(self):
        self.settle()
        for key, button in self.window.sidebar._buttons.items():
            with self.subTest(page=key):
                self.assertEqual(button.isChecked(), key == "design")
                if key != "design":
                    self.assertFalse(button.hasFocus(), f"{key} kept focus after Design became current")
                    image = button.grab().toImage()
                    self.assertEqual(accent_pixels(image, border_points(image)), 0,
                                     f"{key} has an accent ring while Design is current")


if __name__ == "__main__":
    unittest.main()
