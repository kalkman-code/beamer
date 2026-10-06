"""Measurements for the campaign's Qt polish faults, using synthetic settings only."""

import unittest

from PySide6.QtCore import QPoint, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFontInfo, QFontMetrics, QImage, QPainter, QPalette
from PySide6.QtWidgets import QLabel, QWidget

import test_settings_layout_acceptance as acceptance
import diagram
import effect_previews
import pages_win
import theme
import widgets


class QtPolishTests(unittest.TestCase):
    setUpClass = acceptance.SettingsLayoutAcceptance.__dict__["setUpClass"]
    setUp = acceptance.SettingsLayoutAcceptance.setUp
    tearDown = acceptance.SettingsLayoutAcceptance.tearDown
    settle = acceptance.SettingsLayoutAcceptance.settle
    set_size = acceptance.SettingsLayoutAcceptance.set_size
    page_scroll = acceptance.SettingsLayoutAcceptance.page_scroll

    def test_01_selected_segment_has_dark_text_on_light_with_contrast(self):
        self.set_size(900)
        for dark in (False, True):
            theme.set_dark(dark)
            self.window.setStyleSheet(theme.stylesheet())
            self.page_scroll("design")
            button = self.window.design_mode_choice._buttons["crossing"]
            self.settle()
            image = self.window.grab().toImage()
            dpr = image.devicePixelRatio()
            point = button.mapTo(self.window, QPoint())
            image = image.copy(round(point.x() * dpr), round(point.y() * dpr),
                               round(button.width() * dpr), round(button.height() * dpr))
            bg = image.pixelColor(image.width() // 2, round(5 * image.devicePixelRatio()))
            self.assertGreater(bg.lightnessF(), .7)
            # Measure actual painted glyph ink, rather than merely the stylesheet palette.
            centre = image.copy(round(12 * image.devicePixelRatio()), round(10 * image.devicePixelRatio()),
                                image.width() - round(24 * image.devicePixelRatio()),
                                image.height() - round(20 * image.devicePixelRatio()))
            ink = min((centre.pixelColor(x, y) for x in range(centre.width())
                       for y in range(centre.height())), key=lambda c: c.lightnessF())
            def luminance(colour):
                values = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4
                          for v in colour.getRgbF()[:3]]
                return sum(v * w for v, w in zip(values, (.2126, .7152, .0722)))
            self.assertGreaterEqual((luminance(bg) + .05) / (luminance(ink) + .05), 4.5)

    def test_02_segments_measure_longest_label_and_never_wrap(self):
        for width in (320, 640, 900, 1440, 1920):
            self.set_size(width)
            for key, *_ in pages_win.PAGES:
                column = self.page_scroll(key).widget().findChild(QWidget, "page_column")
                for view in column.findChildren(widgets._SegmentedView):
                    if not view.isVisible():
                        continue
                    widths = [button.width() for button in view.buttons]
                    self.assertLessEqual(max(widths, default=0) - min(widths, default=0), 1)
                    for button in view.buttons:
                        with self.subTest(width=width, page=key, text=button.text()):
                            required = button.fontMetrics().horizontalAdvance(button.text()) + 16
                            longest = max(b.fontMetrics().horizontalAdvance(b.text()) + 16 for b in view.buttons)
                            if longest <= view.width():
                                self.assertGreaterEqual(button.width(), required)
                                self.assertEqual(button.height(), 30)
                            else:
                                # Full machine names can be longer than the entire narrow column.
                                self.assertGreaterEqual(button.height(), button.heightForWidth(button.width()))

    def test_03_form_controls_follow_label_track_exactly(self):
        for width in (900, 1440, 1920):
            self.set_size(width)
            self.page_scroll("design")
            for row in self.window.look_module.findChildren(widgets.FormRow):
                control = row.holder.layout().itemAt(0).widget()
                self.assertEqual(control.mapTo(row, QPoint()).x(), 196)

    def test_04_hover_hint_is_plain_secondary_text(self):
        self.assertEqual(self.window.style_preview_hint.text(), "Hover to preview")

    def test_05_effect_layer_preserves_screen_transform(self):
        # Identical screen/pointer scene with and without a pen in its own layer.
        scene = {"screens": [(100, 0, 100, 100, "windows")], "pens": [], "pointer": None, "notch": None}
        def paint(pens):
            image = QImage(300, 120, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(QColor("magenta"))
            painter = QPainter(image)
            painter.translate(50, 10)
            effect_previews.paint_scene(painter, __import__("PySide6.QtCore", fromlist=["QRectF"]).QRectF(0, 0, 200, 100),
                                       __import__("PySide6.QtCore", fromlist=["QRectF"]).QRectF(100, 0, 200, 100),
                                       dict(scene, pens=pens), 10, False)
            painter.end()
            return image
        from unittest.mock import patch
        with patch.object(effect_previews, "replay", lambda painter, _: painter.fillRect(QRectF(100, 0, 10, 10), QColor("cyan"))):
            self.assertEqual(paint([object()]).pixelColor(55, 15), QColor("cyan"))

    def test_06_peer_actions_are_together_at_right_edge(self):
        self.set_size(1920)
        self.page_scroll("overview")
        for row in self.window.machines.rows.values():
            self.assertLessEqual(row.directions.x() - (row.in_use.x() + row.in_use.width()), 16)
            self.assertAlmostEqual(row.remove_button.x() + row.remove_button.width(), row.width(), delta=1)

    def test_07_parts_are_segmented_and_corners_use_two_columns(self):
        self.assertIsInstance(self.window.crossing_rows["parts"].holder.layout().itemAt(0).widget(), widgets._SegmentedView)
        self.set_size(1440)
        self.page_scroll("crossing")
        view = self.window.corner_choice.view
        view.show()
        self.settle()
        self.assertLessEqual(len({button.y() for button in view.buttons}), 2)

    def test_08_every_legend_way_has_a_description(self):
        self.assertTrue(all(description for _, _, description in pages_win.WAY_ROWS))

    def test_09_headers_and_diagram_labels_wrap(self):
        self.assertTrue(self.window.ways_module.eyebrow.wordWrap())
        self.assertTrue(self.window.jump_heading.wordWrap())
        strip = self.window.resistance_strip
        strip.resize(400, 44)
        strip.set_other_name("Other machine")
        metrics = QFontMetrics(theme.font(theme.TYPE["small"], 600))
        width = strip.target_width() if hasattr(strip, "target_width") else strip.MAC
        self.assertGreaterEqual(width, metrics.horizontalAdvance("Other machine") + 12)
        strip.set_other_name("Synthetic workstation with a long machine name")
        strip.resize(160, 44)
        needed = metrics.boundingRect(QRect(0, 0, round(strip.target_width() - 8), 10000),
                                      int(Qt.TextFlag.TextWrapAnywhere), strip._other)
        self.assertGreaterEqual(strip.heightForWidth(160), needed.height() + 4)

    def test_10_status_uses_hanken_with_normal_tracking(self):
        self.page_scroll("overview")
        labels = [self.window.location_readout, *[row.word for row in self.window.machines.rows.values()]]
        for item in labels:
            self.assertEqual(QFontInfo(item.font()).family(), theme.sans())
            self.assertIn(item.font().letterSpacing(), (0, 100))

    def test_11_narrow_rows_have_readable_targets_and_whole_diagram(self):
        self.set_size(320)
        self.page_scroll("overview")
        for row in self.window.machines.rows.values():
            self.assertGreaterEqual(row.word.mapTo(row, QPoint()).y(), row.name.mapTo(row, QPoint()).y() + row.name.height())
            for button in (row.directions, row.remove_button):
                self.assertGreaterEqual(button.width(), button.fontMetrics().horizontalAdvance(button.text()) + 16)
                self.assertGreaterEqual(button.height(), 30)
        self.page_scroll("crossing")
        drawing = self.window.arrangement_diagram
        self.assertFalse(self.window.ways_pair.beside())
        self.assertGreaterEqual(drawing.width(), 120)
        pc, screens, _ = drawing._drawing(drawing.width())
        for rect in [pc, *screens.values()]:
            self.assertGreaterEqual(rect[0], 0)
            self.assertLessEqual(rect[0] + rect[2], drawing.width() + 1)
        metrics = QFontMetrics(theme.font(theme.TYPE["small"], 600))
        for key, rect in screens.items():
            needed = metrics.boundingRect(QRect(0, 0, max(1, round(rect[2] - 8)), 10000),
                                          int(Qt.TextFlag.TextWrapAnywhere), drawing._labels[key])
            self.assertLessEqual(needed.height(), rect[3] - 4)
            self.assertLessEqual(needed.width(), rect[2] - 8 + 1)
        self.assertGreaterEqual(self.window.jump_recorder.height(), self.window.jump_recorder.heightForWidth(self.window.jump_recorder.width()))

    def test_12_rail_ends_above_footer_at_minimum_height(self):
        self.set_size(320, 300)
        sidebar = self.window.sidebar
        footer = self.window.footer_note.parentWidget()
        for child in [*sidebar._buttons.values(), sidebar.foot]:
            self.assertLessEqual(child.mapTo(self.window, QPoint()).y() + child.height(), footer.y())

    def test_13_full_document_background_is_opaque(self):
        import render_settings_layout as render
        page = self.page_scroll("design").widget()
        image = render.capture_document(page) if hasattr(render, "capture_document") else page.grab().toImage()
        self.assertEqual(image.pixelColor(0, 0).alpha(), 255)
        self.assertEqual(image.pixelColor(0, 0).name(), theme.colour("ground"))


if __name__ == "__main__":
    unittest.main()
