"""Regression bounds from the rc.2 offscreen audit, using production fonts and ink checks."""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget

import diagram
import machines_win
import theme
import widgets
from scripts.ui_audit.qt_audit import measure, text_measure


class AuditGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        cls.app.setFont(theme.font(theme.TYPE["body"]))
        cls.app.setStyleSheet(theme.stylesheet())

    def settle(self):
        for _ in range(6):
            QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
            self.app.processEvents()

    def page(self, item, width=800, height=160):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(item)
        layout.addStretch(1)
        scroll.setWidget(root)
        scroll.resize(width, height)
        scroll.show()
        self.addCleanup(scroll.deleteLater)
        self.settle()
        return scroll

    def test_wrapped_note_allocates_height_for_its_actual_width(self):
        note = widgets.label(
            "This screen decides, including when another machine drives it. On, a full-screen app here "
            "holds pointer crossings; shortcuts and jump keys still work. Off, another machine's pointer "
            "can go home or onward through the configured ways.", "note", wrap=True)
        self.page(note)
        self.assertIsNone(text_measure(note), f"actual note size {note.size().toTuple()}: {text_measure(note)}")

    def test_choice_children_fit_their_allocated_row(self):
        choice = widgets.Choice((("auto", "Automatic"), ("same", "Same keys"), ("swap", "Swap keys")), 3, "auto")
        module = widgets.Module("Modifier keys")
        module.body.addWidget(choice.view)
        self.page(module, height=85)
        self.assertFalse(measure(module), measure(module))

    def peer(self):
        row = machines_win.PeerRow("synthetic", *([lambda *_: None] * 4))
        row.refresh({"port": 24820}, "Studio MacBook Pro with an exceptionally long mach", 
                    SimpleNamespace(tone="off", word="Waiting", detail="Waiting for this machine."), "Mac")
        return row

    def test_direction_switches_have_separate_uncropped_tracks(self):
        row = self.peer()
        row.directions.setChecked(True)
        self.page(row, width=700, height=160)
        self.assertFalse(measure(row.direction_group), measure(row.direction_group))

    def test_confirmation_ink_and_controls_fit(self):
        row = self.peer()
        row._ask(True)
        self.page(row, width=700, height=160)
        self.assertFalse(measure(row.confirm), measure(row.confirm))

    def test_diagram_reports_required_height_before_parent_allocation(self):
        drawing = diagram.ArrangementDiagram()
        drawing.set_machines([dict(key="desk", label="Desk", side="right", methods=["edge"],
                                   parts=["middle"], corner="top_right", chosen=True)], "F13", "double_tap", True)
        self.assertTrue(drawing.hasHeightForWidth())
        for width in (254, 361, 800):
            drawing.resize(width, 100)
            self.assertGreaterEqual(drawing.heightForWidth(width), drawing._height())


if __name__ == "__main__":
    unittest.main()
