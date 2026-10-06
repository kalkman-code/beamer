import json
import math
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

import machines_win
import theme


class PairCodeRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        previous_font = cls.app.font()
        previous_stylesheet = cls.app.styleSheet()
        cls.addClassCleanup(cls.app.setStyleSheet, previous_stylesheet)
        cls.addClassCleanup(cls.app.setFont, previous_font)
        theme.init_fonts()
        cls.app.setFont(theme.font(theme.TYPE["body"]))
        cls.app.setStyleSheet(theme.stylesheet())

    def setUp(self):
        self.sheet = machines_win.PairingSheet(lambda: None, lambda: None, lambda *_: None, lambda *_: None)
        self.sheet.resize(432, 900)
        self.sheet.show()
        self.app.processEvents()
        self.addCleanup(self.sheet.deleteLater)

    def _pixel_metrics(self, widget, digit):
        cells = widget.digit_cells()
        self.assertEqual(len(cells), 6)
        image = widget.grab().toImage().convertToFormat(QImage.Format.Format_ARGB32)
        scale = image.devicePixelRatio()
        result = []
        for index, cell in enumerate(cells):
            left = max(0, math.ceil(cell.left() * scale - 0.5))
            top = max(0, math.ceil(cell.top() * scale - 0.5))
            right = min(image.width(), math.ceil(cell.right() * scale - 0.5))
            bottom = min(image.height(), math.ceil(cell.bottom() * scale - 0.5))
            points = []
            for y in range(top, bottom):
                for x in range(left, right):
                    colour = image.pixelColor(x, y)
                    if colour.red() > 175 and colour.green() > 175 and colour.blue() > 165:
                        points.append((x, y))
            if not points:
                result.append(None)
                continue
            ink_box = (min(x for x, _ in points), min(y for _, y in points),
                       max(x for x, _ in points), max(y for _, y in points))
            box_centre = (cell.center().x() * scale, cell.center().y() * scale)
            ink_centre = ((ink_box[0] + ink_box[2] + 1) / 2,
                          (ink_box[1] + ink_box[3] + 1) / 2)
            result.append({"digit": digit, "slot": index,
                           "dx": round(ink_centre[0] - box_centre[0], 3),
                           "dy": round(ink_centre[1] - box_centre[1], 3)})
        return result

    def _save(self, widget, name):
        root = Path(__file__).resolve().parents[2] / "renders" / "qt"
        root.mkdir(parents=True, exist_ok=True)
        widget.grab().save(str(root / name))

    def _save_atlas(self, images, name):
        from PySide6.QtGui import QPainter

        first = images[0]
        atlas = QImage(first.width(), first.height() * len(images), QImage.Format.Format_ARGB32)
        atlas.fill(QColor(theme.colour("ground")))
        painter = QPainter(atlas)
        for index, image in enumerate(images):
            image.setDevicePixelRatio(1)
            painter.drawImage(0, index * image.height(), image)
        painter.end()
        root = Path(__file__).resolve().parents[2] / "renders" / "qt"
        root.mkdir(parents=True, exist_ok=True)
        atlas.save(str(root / name))

    @staticmethod
    def _name(view, widget, width=None):
        scale = widget.grab().devicePixelRatio()
        width_tag = f"-{width}px" if width else ""
        return f"{view}{width_tag}-{scale:g}x.png"

    def _assert_box_borders(self, widget):
        image = widget.grab().toImage().convertToFormat(QImage.Format.Format_ARGB32)
        scale = image.devicePixelRatio()
        edge = QColor(theme.colour("edge"))
        for cell in widget._cell_rectangles():
            self.assertGreaterEqual(cell.left(), 0, f"cell extends left of its widget: {cell}, {widget.size()}")
            self.assertLessEqual(cell.right(), widget.width(), f"cell extends right of its widget: {cell}, {widget.size()}")
            self.assertGreaterEqual(cell.top(), 0, f"cell extends above its widget: {cell}, {widget.size()}")
            self.assertLessEqual(cell.bottom(), widget.height(), f"cell extends below its widget: {cell}, {widget.size()}")
            x = round(cell.center().x() * scale)
            y = round(cell.top() * scale)
            found = False
            for px in range(x - 2, x + 3):
                for py in range(y - 2, y + 3):
                    if not (0 <= px < image.width() and 0 <= py < image.height()):
                        continue
                    colour = image.pixelColor(px, py)
                    if max(abs(colour.red() - edge.red()), abs(colour.green() - edge.green()),
                           abs(colour.blue() - edge.blue())) < 24:
                        found = True
            self.assertTrue(found, f"missing rendered cell border near {(x, y)}")

    def test_typed_digits_are_ink_centred_in_six_boxes_for_every_digit(self):
        measurements = []
        for width in (432, 640):
            self.sheet.resize(width, 900)
            self.app.processEvents()
            samples = []
            for digit in "0123456789":
                with self.subTest(width=width, digit=digit):
                    self.sheet.code.setText(digit * 6)
                    self.app.processEvents()
                    offsets = self._pixel_metrics(self.sheet.code, digit)
                    self.assertTrue(all(offset is not None for offset in offsets), offsets)
                    measurements.extend(offsets)
                    samples.append(self.sheet.code.grab().toImage())
            self._save_atlas(samples, self._name("digits-typed", self.sheet.code, width))
            self.sheet.code.setText("012345")
            self.app.processEvents()
            self._save(self.sheet, self._name("sheet-typed", self.sheet, width))
            self._assert_box_borders(self.sheet.code)
            self.sheet.code.clear()
            self.app.processEvents()
            self._save(self.sheet, self._name("sheet-empty", self.sheet, width))
            self._assert_box_borders(self.sheet.code)
            self.assertTrue(all(offset is None for offset in self._pixel_metrics(self.sheet.code, "")))
        self._write_measurements("typed", measurements)
        self.assertTrue(all(abs(offset[axis]) <= 1 for offset in measurements for axis in ("dx", "dy")),
                        measurements)

    def test_shown_digits_are_ink_centred_in_six_boxes_for_every_digit(self):
        measurements = []
        for width in (432, 640):
            self.sheet.resize(width, 900)
            self.app.processEvents()
            samples = []
            for digit in "0123456789":
                with self.subTest(width=width, digit=digit):
                    self.sheet.show_code(digit * 6, 60, "192.0.2.1:51820", None, "")
                    self.app.processEvents()
                    offsets = self._pixel_metrics(self.sheet.code_label, digit)
                    self.assertTrue(all(offset is not None for offset in offsets), offsets)
                    measurements.extend(offsets)
                    samples.append(self.sheet.code_label.grab().toImage())
            self._save_atlas(samples, self._name("digits-shown", self.sheet.code_label, width))
            self.sheet.show_code("012345", 60, "192.0.2.1:51820", None, "")
            self.app.processEvents()
            self._save(self.sheet, self._name("sheet-shown", self.sheet, width))
            self._assert_box_borders(self.sheet.code_label)
        self._write_measurements("shown", measurements)
        self.assertTrue(all(abs(offset[axis]) <= 1 for offset in measurements for axis in ("dx", "dy")),
                        measurements)

    def test_pair_action_precedes_the_machines_list(self):
        module = machines_win.MachinesModule(*([lambda *_: None] * 5))
        module.resize(560, 300)
        module.show()
        self.app.processEvents()
        button_y = module.pair_button.mapTo(module, module.pair_button.rect().topLeft()).y()
        empty_y = module.empty.mapTo(module, module.empty.rect().topLeft()).y()
        self._save(module, self._name("machines-empty", module))
        self.assertLess(button_y, empty_y)
        entry = {"token": "test-machine", "name": "Studio", "platform": "macos", "port": 24820,
                 "host": "192.0.2.10", "send": True, "allow_drive": False, "in_use": True}
        state = machines_win.peers_view.PeerState("amber", "Waiting", "Looking for Studio.")
        module.set_peers([(entry, "Studio", state, "Mac · 192.0.2.10 · port 24820")])
        self.app.processEvents()
        row = module.rows["test-machine"]
        button_y = module.pair_button.mapTo(module, module.pair_button.rect().topLeft()).y()
        row_y = row.mapTo(module, row.rect().topLeft()).y()
        self.assertLess(button_y, row_y)
        self._save(module, self._name("machines-filled", module))

    def test_paste_and_backspace_keep_the_six_digit_editing_contract(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        self.sheet.code.setFocus()
        self.app.clipboard().setText("123 456")
        QTest.keyClick(self.sheet.code, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.sheet.code.text(), "123456")
        self._save(self.sheet, self._name("sheet-typed-input", self.sheet, 432))
        QTest.keyClick(self.sheet.code, Qt.Key.Key_Backspace)
        self.assertEqual(self.sheet.code.text(), "12345")

    def test_cell_click_shift_selection_drag_and_final_backspace_follow_visible_slots(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        code = self.sheet.code
        code.setText("123456")
        cells = code._cell_rectangles()
        point = lambda index, after=False: QPoint(round(cells[index].center().x() + (1 if after else -1)), round(cells[index].center().y()))
        QTest.mouseClick(code, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point(0))
        QTest.mouseClick(code, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, point(3))
        self.assertEqual(code.selectedText(), "123")

        code.setText("123456")
        QTest.mousePress(code, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point(0))
        QTest.mouseMove(code, point(5, True))
        QTest.mouseRelease(code, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point(5, True))
        self.assertEqual(code.selectedText(), "123456")

        code.setText("123456")
        last = cells[5]
        QTest.mouseClick(code, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(round(last.right() - 1), round(last.center().y())))
        self.assertEqual(code.cursorPosition(), 6)
        QTest.keyClick(code, Qt.Key.Key_Backspace)
        self.assertEqual(code.text(), "12345")

        code.setText("123456")
        code.setCursorPosition(6)
        QTest.mouseClick(code, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, point(2))
        self.assertEqual(code.selectedText(), "3456")
        QTest.mouseDClick(code, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point(2))
        self.assertEqual(code.selectedText(), "3")

    def test_shown_code_selection_uses_the_six_visible_cells(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        label = self.sheet.code_label
        self.sheet.show_code("123456", 60, "192.0.2.1:51820", None, "")
        cells = label._cell_rectangles()
        point = lambda index, after=False: QPoint(round(cells[index].center().x() + (1 if after else -1)), round(cells[index].center().y()))
        QTest.mousePress(label, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point(0))
        QTest.mouseMove(label, point(5, True))
        QTest.mouseRelease(label, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point(5, True))
        self.assertEqual(label.selectedText(), "123 456")
        QTest.mouseClick(label, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point(5, True))
        QTest.mouseClick(label, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, point(2))
        self.assertEqual(label.selectedText(), "3 456")
        QTest.mouseDClick(label, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point(2))
        self.assertEqual(label.selectedText(), "3")

    def _write_measurements(self, state, measurements):
        scale = QApplication.primaryScreen().devicePixelRatio() if QApplication.primaryScreen() else 1
        path = Path(__file__).resolve().parents[2] / "renders" / "qt" / f"metrics-{state}-{scale:g}x.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"scale": scale, "measurements": measurements}, indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()
