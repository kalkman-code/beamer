"""An empty Choice survives a layout pass beside buttons that override their size hints.

The machine choice is built empty whenever fewer than two machines are paired. Before
38eb92b its view divided by a zero column count in resizeEvent. On PySide6 6.11 an
exception inside a C++ virtual override during a layout pass does not surface as a
traceback: the next Python override Qt calls in the same pass (an ActionButton's
minimumSizeHint) segfaults in Shiboken::BindingManager::getOverride. The pass runs in a
subprocess so a crash fails this test instead of killing the runner.
"""

import os
from pathlib import Path
import subprocess
import sys
import unittest

WIN_APP = Path(__file__).resolve().parents[1]

CHILD = """
import sys
sys.path[:0] = [sys.argv[1], str(__import__("pathlib").Path(sys.argv[1]).parent)]
from PySide6.QtWidgets import QApplication, QHBoxLayout, QVBoxLayout, QWidget
app = QApplication([])
import widgets
window = QWidget()
column = QVBoxLayout(window)
for columns in (1, 2):
    row = QHBoxLayout()
    column.addLayout(row)
    row.addWidget(widgets.Choice((), columns, "").view)
    row.addWidget(widgets.ActionButton("Pair"))
    row.addWidget(widgets.ActionButton("Forget"))
window.resize(640, 400)
window.grab()
window.resize(320, 600)
window.grab()
print("survived")
"""


class EmptyChoiceLayout(unittest.TestCase):
    def test_empty_choice_lays_out_without_error_or_crash(self):
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen",
                           QT_QPA_FONTDIR=str(WIN_APP / "assets"))
        result = subprocess.run([sys.executable, "-X", "faulthandler", "-c", CHILD, str(WIN_APP)],
                                env=environment, capture_output=True, text=True, timeout=120)
        self.assertNotIn("Error calling Python override", result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("survived", result.stdout)


if __name__ == "__main__":
    unittest.main()
