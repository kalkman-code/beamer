import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import diagram
import theme
import widgets


class WideLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.init_fonts()

    def test_wrapped_paragraphs_keep_a_readable_measure(self):
        paragraph = widgets.label("A long settings explanation.", "note", wrap=True)

        self.assertEqual(paragraph.maximumWidth(), 560)

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

if __name__ == "__main__":
    unittest.main()
