from __future__ import annotations

import os
import re
import socket
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "win_app"))

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QAbstractButton, QComboBox, QGroupBox, QLabel, QLineEdit, QWidget

import pages_win
import theme
import app_config
from core import i18n
from core import protocol
from core.tests import responder_harness
from win_app.tests.test_settings_layout_acceptance import SyntheticWindow


# Language names stay self-named; the remaining text is test data, brand, glyphs, or units.
# Key names are recorded values, the synthetic row supplies its peer name, and the machine running
# the test shows its own name as this machine's.
ALLOWED_TEXT = {
    "Beamer",
    "English", "Español", "Italiano",
    "Synthetic workstation",
    socket.gethostname(), socket.gethostname().split(".", 1)[0],
    "Right Ctrl",
    "https://kalkmancode.co.uk/beamer",
    "⌘", "⌥", "⌃", "⇧", "⌫", "↵", "⎋", "←", "→", "↑", "↓",
    "px", "ms", "s", "Hz", "DIP",
}


class LocalisedPages(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        cls.app.setFont(theme.font(theme.TYPE["body"]))

    def setUp(self):
        os.environ["BEAMER_LANGUAGE"] = "qps"
        i18n.install("auto")
        self.folder = tempfile.TemporaryDirectory(prefix="beamer-i18n-")
        settings = app_config.migrate(None, machine_id=protocol.id_text(responder_harness.HERE))
        settings.update(port=49152, appearance="dark", check_updates=False)
        settings["peers"] = [
            responder_harness.entry(responder_harness.B, "Synthetic workstation", side="right"),
        ]
        settings["zones"] = [{"peer": protocol.id_text(responder_harness.B), "kind": "part", "parts": ["middle"]}]
        config_path = Path(self.folder.name) / "settings.json"
        app_config.write_settings(config_path, settings)
        self.window = SyntheticWindow(config_path)
        self.window.full_screen_timer.stop()
        self.window.refresh_timer.stop()
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.window.resize(320, 900)
        self.window.show()
        self.settle()

    def tearDown(self):
        self.window.server.stop()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.folder.cleanup()
        os.environ.pop("BEAMER_LANGUAGE", None)
        i18n.install("en")

    def settle(self):
        for _ in range(5):
            QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
            self.app.processEvents()

    def visible_text(self):
        values = []
        for widget in self.window.findChildren(QLabel) + self.window.findChildren(QAbstractButton):
            if widget.isVisible() and widget.text().strip():
                values.append((widget, "text", widget.text().strip()))
        for widget in self.window.findChildren(QGroupBox):
            if widget.isVisible() and widget.title().strip():
                values.append((widget, "title", widget.title().strip()))
        for widget in self.window.findChildren(QLineEdit):
            if widget.isVisible() and widget.placeholderText().strip():
                values.append((widget, "placeholder", widget.placeholderText().strip()))
        for combo in self.window.findChildren(QComboBox):
            if combo.isVisible():
                values.extend((combo, "choice", combo.itemText(index).strip()) for index in range(combo.count()))
        for action in self.window.findChildren(QAction):
            if action.isVisible() and action.text().strip():
                values.append((action, "action", action.text().strip()))
        for widget in self.window.findChildren(QWidget):
            if not widget.isVisible():
                continue
            for field in ("toolTip", "statusTip", "whatsThis", "accessibleName", "accessibleDescription"):
                text = getattr(widget, field)().strip()
                if text:
                    values.append((widget, field, text))
        return values

    def test_every_settings_page_and_pairing_sheet_use_pseudo_localised_text(self):
        missing = []
        for key in self.window._page_indexes:
            with self.subTest(page=key):
                self.window._select_page(key)
                self.settle()
                if key == "overview":
                    self.window._toggle_pairing_sheet()
                    self.settle()
                for widget, field, text in self.visible_text():
                    full_text = getattr(widget, "full_text", "")
                    if full_text and full_text.startswith("⟦") and full_text.endswith("⟧"):
                        continue
                    if text in ALLOWED_TEXT or not any(char.isalpha() for char in text):
                        continue
                    remainder = text
                    for data in sorted(ALLOWED_TEXT, key=len, reverse=True):
                        remainder = remainder.replace(data, "")
                    remainder = re.sub(r"\d+(?:\.\d+)?\s*(?:px|ms|s|Hz|pt|DIP)?", "", remainder)
                    if remainder.strip(" ,.;:·()") and not (remainder.startswith("⟦") and remainder.endswith("⟧")):
                        missing.append(f"{key}: {type(widget).__name__}.{field}: {text}")
        self.assertEqual(missing, [])

    def test_no_page_text_is_translated_twice(self):
        # A tr() over text already translated is one pseudo wrap round another and nothing else; in a
        # real language it would look the translation up as an English msgid. A translated value
        # filling a translated template has text of the template's own beside it.
        twice = []
        for key in pages_win.KEYS:
            self.window._select_page(key)
            self.settle()
            twice.extend(f"{key}: {text}" for _widget, _field, text in self.visible_text()
                         if re.fullmatch(r"⟦⟦[^⟦⟧]*⟧·*⟧", text))
        self.assertEqual(twice, [])

    def test_crossing_design_and_pairing_labels_fit_at_narrow_width(self):
        for key in ("crossing", "design", "overview"):
            self.window._select_page(key)
            self.settle()
            if key == "overview":
                self.window._toggle_pairing_sheet()
                self.settle()
            labels = [label for label in self.window.findChildren(QLabel)
                      if label.isVisible() and label.text().strip()]
            for label in labels:
                with self.subTest(page=key, text=label.text()):
                    if label.wordWrap():
                        self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()))
                    else:
                        self.assertLessEqual(label.fontMetrics().horizontalAdvance(label.text()), label.width())
            for index, first in enumerate(labels):
                for second in labels[index + 1:]:
                    if first.parentWidget() is second.parentWidget():
                        with self.subTest(page=key, first=first.text(), second=second.text()):
                            self.assertFalse(first.geometry().intersects(second.geometry()))


if __name__ == "__main__":
    unittest.main()
