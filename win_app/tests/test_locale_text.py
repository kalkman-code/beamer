import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.append(str(Path(__file__).resolve().parents[2]))

from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

import widgets
from core import locale


class VisibleTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_us_spelling_applies_at_widget_menu_tooltip_and_action_setters(self):
        notices = []

        def capture(_tray, title, body, *_args):
            notices.append((title, body))

        with mock.patch.object(locale, "is_us_region", return_value=True):
            with mock.patch.object(QSystemTrayIcon, "showMessage", capture):
                widgets.install_text_localisation()
                label = widgets.label("Colourful behaviour", "note")
                label.setText("Favourite centre")
                label.setToolTip("Organising a colourful favourite")
                menu = QMenu()
                action = menu.addAction("Minimise")
                action.setText("Initialising cancelled")
                QSystemTrayIcon().showMessage("Initialising", "Cancelled colourful behaviour")

        self.assertEqual(label.text(), "Favorite center")
        self.assertEqual(label.toolTip(), "Organizing a colorful favorite")
        self.assertEqual(action.text(), "Initializing canceled")
        self.assertEqual(notices, [("Initializing", "Canceled colorful behavior")])
