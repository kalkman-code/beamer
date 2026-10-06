"""Every settings page, rendered offscreen in Light and in Dark, read back as pixels: each piece of
text against what is behind it, the module cards against the palette, and both after the
appearance changes with the window already built and another page open. The Mac twin of this
check caught 1.5.0-rc.5 leaving the hidden pages' cards dark after Light was chosen on Design.

BEAMER_RENDERS=<folder> also writes each page at 900 DIP in both appearances."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "win_app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel, QWidget  # noqa: E402

import motion  # noqa: E402
import pages_win  # noqa: E402
import theme  # noqa: E402
import tokens  # noqa: E402
from test_settings_layout_acceptance import SyntheticWindow, write_synthetic_settings  # noqa: E402

WIDTH = 900
KEYS = tuple(key for key, _name, _purpose in pages_win.PAGES)


def _luminance(rgb):
    def channel(value):
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    red, green, blue = (channel(value) for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first, second):
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _rgb(colour: QColor):
    return colour.redF(), colour.greenF(), colour.blueF()


class AppearanceContrast(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        cls.app.setFont(theme.font(theme.TYPE["body"]))
        cls.folder = tempfile.TemporaryDirectory(prefix="beamer-contrast-")
        cls.window = SyntheticWindow(write_synthetic_settings(Path(cls.folder.name)))
        cls.window.full_screen_timer.stop()
        cls.window.refresh_timer.stop()
        cls.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        cls.window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        cls.window.resize(WIDTH, 900)
        cls.window.show()
        cls.renders = os.environ.get("BEAMER_RENDERS")
        # The cross-fade's ghost of the old palette would sit over the grab; measured is the
        # settled window, as with reduced motion.
        cls.still = mock.patch.object(motion, "reduced", return_value=True)
        cls.still.start()

    @classmethod
    def tearDownClass(cls):
        cls.still.stop()
        cls.window.server.stop()
        cls.window.close()
        cls.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        cls.app.processEvents()
        theme.set_dark(True)
        cls.folder.cleanup()

    def settle(self):
        for _ in range(7):
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
            self.app.processEvents()

    def choose(self, choice):
        self.window.appearance_choice.set_value(choice)
        self.window._apply_appearance(choice)
        self.settle()

    def show_page(self, key):
        """The page at 900 DIP, the window tall enough that the whole page is in view."""
        window = self.window
        window._select_page(key)
        window.resize(WIDTH, 900)
        self.settle()
        scroll = window.stack.currentWidget()
        document = scroll.widget()
        chrome = window.height() - scroll.viewport().height()
        window.resize(WIDTH, document.sizeHint().height() + chrome + 2)
        self.settle()
        return window.grab().toImage()

    @staticmethod
    def ground(image, rect, ratio):
        counts = Counter()
        steps_x, steps_y = 24, 8
        for i in range(steps_x):
            for j in range(steps_y):
                x = int((rect.x() + rect.width() * (i + 0.5) / steps_x) * ratio)
                y = int((rect.y() + rect.height() * (j + 0.5) / steps_y) * ratio)
                x = min(max(x, 0), image.width() - 1)
                y = min(max(y, 0), image.height() - 1)
                counts[image.pixelColor(x, y).rgb()] += 1
        return QColor.fromRgb(counts.most_common(1)[0][0])

    @staticmethod
    def strongest(image, rect, ratio, behind):
        best, best_value = behind, 1.0
        for y in range(int(rect.y() * ratio), int((rect.y() + rect.height()) * ratio)):
            for x in range(int(rect.x() * ratio), int((rect.x() + rect.width()) * ratio)):
                colour = image.pixelColor(min(x, image.width() - 1), min(y, image.height() - 1))
                value = contrast(_rgb(colour), _rgb(behind))
                if value > best_value:
                    best, best_value = colour, value
        return best

    def texts(self):
        for widget in self.window.findChildren(QWidget):
            if isinstance(widget, QLabel):
                text, role = widget.text(), QPalette.ColorRole.WindowText
                if widget.textFormat() == Qt.TextFormat.RichText or "<" in text:
                    continue
            elif isinstance(widget, QAbstractButton):
                text, role = widget.text(), QPalette.ColorRole.ButtonText
            else:
                continue
            if not text.strip() or not widget.isVisibleTo(self.window) or not widget.isEnabled():
                continue
            if widget.width() < 2 or widget.height() < 2:
                continue
            yield widget, text, role

    def check_text(self, image, label):
        failures, checked = [], 0
        ratio = image.devicePixelRatio()
        for widget, text, role in self.texts():
            origin = widget.mapTo(self.window, QPoint())
            rect = widget.rect().translated(origin)
            if rect.bottom() > self.window.height() or rect.right() > self.window.width():
                continue
            behind = self.ground(image, rect, ratio)
            ink = widget.palette().color(role)
            if isinstance(widget, QAbstractButton) and widget.isChecked():
                # A :checked rule recolours the text without touching the palette, so the ink is
                # read off the pixels: the one that stands out most from the ground.
                ink = self.strongest(image, rect, ratio, behind)
            size = widget.font().pointSizeF() if widget.font().pointSizeF() > 0 else widget.font().pixelSize() * 0.75
            bold = widget.font().weight() >= 700
            needed = 3.0 if size >= 18 or (size >= 14 and bold) else 4.5
            value = contrast(_rgb(ink), _rgb(behind))
            checked += 1
            if value < needed:
                failures.append(f"{label}: {type(widget).__name__} {text[:40]!r} {value:.2f} < {needed}")
        return checked, failures

    def check_cards(self, image, label):
        """Every module card in view measures as `panel` in the palette in use."""
        palette = tokens.PALETTE if theme.is_dark() else tokens.PALETTE_LIGHT
        panel = QColor(palette["panel"])
        ratio = image.devicePixelRatio()
        page = self.window.stack.currentWidget()
        failures, cards = [], 0
        for widget in page.findChildren(QWidget):
            if widget.property("vernier") != "module" or not widget.isVisibleTo(self.window):
                continue
            point = widget.mapTo(self.window, QPoint(6, 6))
            if point.y() >= self.window.height():
                continue
            cards += 1
            measured = image.pixelColor(int(point.x() * ratio), int(point.y() * ratio))
            if max(abs(a - b) for a, b in zip(_rgb(measured), _rgb(panel))) > 3 / 255:
                failures.append(f"{label}: card at {point.x()},{point.y()} measures {measured.name()} not {panel.name()}")
        return cards, failures

    def sweep(self, appearance):
        checked = cards = 0
        failures = []
        for key in KEYS:
            image = self.show_page(key)
            count, text_failures = self.check_text(image, f"{appearance}/{key}")
            panels, card_failures = self.check_cards(image, f"{appearance}/{key}")
            checked += count
            cards += panels
            failures += text_failures + card_failures
            if self.renders:
                folder = Path(self.renders)
                folder.mkdir(parents=True, exist_ok=True)
                image.save(str(folder / f"qt-{key}-{appearance}-{WIDTH}.png"))
        return checked, cards, failures

    def test_every_page_reads_in_dark_and_in_light_chosen_from_another_page(self):
        self.choose("dark")
        self.window._select_page("design")
        self.choose("light")
        self.assertFalse(theme.is_dark())
        checked, cards, failures = self.sweep("light")
        self.assertGreater(checked, 100)
        self.assertGreater(cards, 10)
        self.assertEqual(failures, [])
        self.window._select_page("crossing")
        self.choose("dark")
        checked, cards, failures = self.sweep("dark")
        self.assertGreater(checked, 100)
        self.assertGreater(cards, 10)
        self.assertEqual(failures, [])

    def test_system_switching_while_the_window_exists_repaints_pages_out_of_view(self):
        self.choose("system")
        for dark in (False, True, False):
            self.window._select_page("overview" if dark else "keyboard")
            with mock.patch.object(theme, "system_dark", return_value=dark):
                self.window._apply_appearance()
            self.settle()
            self.assertEqual(theme.is_dark(), dark)
            image = self.show_page("crossing")
            _count, text_failures = self.check_text(image, f"system-{dark}/crossing")
            _cards, card_failures = self.check_cards(image, f"system-{dark}/crossing")
            self.assertEqual(text_failures + card_failures, [])
        self.choose("dark")


if __name__ == "__main__":
    unittest.main()
