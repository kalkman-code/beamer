"""Long machine names stay readable in the space the Qt audit measured."""

import os
import sys
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "ui_audit"))

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QStyle, QStyleOptionButton, QVBoxLayout

import kvm_bridge_win as kvm
import machines_win
import qt_audit
import theme
import widgets


LONG_NAME = "Studio MacBook Pro with an exceptionally long machine name"


class ElidingTextGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        cls.app.setFont(theme.font(theme.TYPE["body"]))

    def settle(self):
        for _ in range(3):
            QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
            self.app.processEvents()

    def make_button(self, text: str, text_width: int):
        button_type = getattr(widgets, "ElidingButton", QPushButton)
        button = button_type(text)
        button.setFont(theme.font(theme.TYPE["body"]))
        button.resize(1000, 48)
        option = QStyleOptionButton()
        button.initStyleOption(option)
        content = button.style().subElementRect(
            QStyle.SubElement.SE_PushButtonContents, option, button)
        horizontal_inset = button.width() - content.width()
        button.setFixedWidth(text_width + horizontal_inset)
        button.setFixedHeight(48)
        self.settle()
        return button

    def test_audited_button_text_areas_elide_painted_text_and_keep_full_names(self):
        cases = (
            ("pairing discovery", 339,
             f"{LONG_NAME[:48]}  ·  Windows  ·  showing a code",
             "  ·  Windows  ·  showing a code"),
            ("Send input", 335, f"Send input to {LONG_NAME}", ""),
            ("machine picker", 135, LONG_NAME, ""),
            ("share recipient tile", 219, f"Middle\n{LONG_NAME}", ""),
        )
        for surface, text_width, full_text, suffix in cases:
            with self.subTest(surface=surface, text_width=text_width):
                button = self.make_button(full_text, text_width)
                set_suffix = getattr(button, "set_elision_suffix", None)
                if callable(set_suffix):
                    set_suffix(suffix)
                button.setAccessibleName(f"Accessible: {full_text}")
                displayed = getattr(button, "displayed_text", button.text)()
                self.assertIn("…", displayed)
                if suffix:
                    self.assertTrue(displayed.endswith(suffix))
                self.assertLessEqual(
                    max(button.fontMetrics().horizontalAdvance(line)
                        for line in displayed.splitlines()), text_width)
                self.assertEqual(button.text(), full_text)
                self.assertEqual(button.accessibleName(), f"Accessible: {full_text}")
                self.assertEqual(button.toolTip(), full_text)
                self.assertIsNone(qt_audit.text_measure(button))

    def test_audited_peer_headings_elide_to_their_real_label_width(self):
        for title in (f"Ways from this screen to {LONG_NAME}", f"Jump to {LONG_NAME}"):
            with self.subTest(title=title):
                label_type = getattr(widgets, "ElidingLabel", QLabel)
                label = label_type(title)
                label.setFont(theme.font(theme.HEADING, 700))
                label.setFixedWidth(361)
                label.setFixedHeight(32)
                self.settle()
                displayed = getattr(label, "displayed_text", label.text)()
                self.assertIn("…", displayed)
                self.assertLessEqual(label.fontMetrics().horizontalAdvance(displayed), label.contentsRect().width())
                self.assertEqual(label.text(), title)
                self.assertEqual(label.toolTip(), title)
                self.assertIsNone(qt_audit.text_measure(label))

    def test_three_machine_choices_keep_full_names_within_the_capped_column(self):
        items = tuple((str(index), LONG_NAME) for index in range(3))
        choice = widgets.Choice(items, 3, "0")
        choice.set_names("Machine")
        choice.view.resize(360, choice.view.heightForWidth(360))
        choice.view.show()
        self.addCleanup(choice.view.deleteLater)
        self.settle()
        self.assertLessEqual(choice.view.width(), 360)
        for button in choice._buttons.values():
            with self.subTest(machine=button.text()):
                self.assertLessEqual(button.geometry().right(), choice.view.width() - 1)
                displayed = getattr(button, "displayed_text", button.text)()
                self.assertEqual(displayed, button.text())
                self.assertGreaterEqual(button.height(), button.heightForWidth(button.width()))
                self.assertEqual(button.accessibleName(), f"Machine, {LONG_NAME}")

    def test_replacing_machine_choices_hides_the_old_container_before_deferred_deletion(self):
        from PySide6.QtWidgets import QWidget
        holder = QWidget()
        slot = QVBoxLayout(holder)
        previous = widgets.Choice((), 1, "")
        slot.addWidget(previous.view)
        holder.show()
        self.addCleanup(holder.deleteLater)
        self.settle()
        owner = SimpleNamespace(_machine_items=(), machine_choice=previous, _machine_slot=slot,
                                _choose_machine=lambda *_: None)
        kvm.WindowsApplication._rebuild_machine_choice(owner, (("first", LONG_NAME), ("second", "Desk")))
        self.assertTrue(previous.view.isHidden())

    def test_short_machine_name_stays_readable_between_long_names(self):
        self.app.setStyleSheet(theme.stylesheet())
        choice = widgets.Choice((("first", LONG_NAME[:48]), ("short", "Desk"),
                                 ("third", "Laboratory Linux workstation with the external d")), 3, "first")
        choice.view.resize(360, choice.view.heightForWidth(360))
        choice.view.show()
        self.addCleanup(choice.view.deleteLater)
        self.settle()
        button = choice._buttons["short"]
        self.assertEqual(button.displayed_text(), "Desk",
                         f"short choice allocated {button.width()}px in the audited 361px row")

    def test_pairing_discovery_retains_the_separator_before_its_platform_and_status(self):
        panel = machines_win.PairingSheet(*([lambda *_: None] * 4))
        self.addCleanup(panel.deleteLater)
        panel.set_machines([dict(name=LONG_NAME[:48], platform="windows", address="192.0.2.10",
                                 pairing=3, pair_id="fixture")], lambda *_: "")
        button = panel.group.buttons()[0]
        button.resize(300, 32)
        displayed = button.displayed_text()
        self.assertIn("…  ·  Windows", displayed)

    def test_audit_keeps_source_text_when_elision_itself_cannot_fit(self):
        button = self.make_button(LONG_NAME, 1)
        button_text = getattr(button, "displayed_text", button.text)()
        button_finding = qt_audit.text_measure(button)
        if button_text != LONG_NAME:
            self.assertEqual(button_finding["source_text"], LONG_NAME)

        label_type = getattr(widgets, "ElidingLabel", QLabel)
        label = label_type(LONG_NAME)
        label.setFixedSize(1, 28)
        self.settle()
        label_text = getattr(label, "displayed_text", label.text)()
        label_finding = qt_audit.text_measure(label)
        if label_text != LONG_NAME:
            self.assertEqual(label_finding["source_text"], LONG_NAME)

    def test_linux_connection_guidance_does_not_name_windows(self):
        owner = SimpleNamespace(_firewall_action=lambda: None)
        layout = QVBoxLayout()
        with mock.patch.object(kvm, "sys", SimpleNamespace(platform="linux")):
            kvm.WindowsApplication._firewall_module(owner, layout)
        module = layout.itemAt(0).widget()
        copy = " ".join((module.eyebrow.text(), owner.firewall_note.text()))
        self.assertNotIn("Windows", copy)
        self.assertIn("Linux", copy)

    def test_linux_appearance_guidance_names_the_linux_desktop(self):
        owner = SimpleNamespace(own_notes=[])
        owner._own_note = MethodType(kvm.WindowsApplication._own_note, owner)
        owner._row = kvm.WindowsApplication._row
        owner._appearance_chosen = lambda _choice: None
        with mock.patch.object(kvm, "sys", SimpleNamespace(platform="linux")):
            module = kvm.WindowsApplication._appearance_module(owner, kvm.default_config())
        self.addCleanup(module.deleteLater)
        copy = next(label.text() for label in module.findChildren(QLabel)
                    if label.text().startswith("System follows"))
        self.assertIn("Linux", copy)
        self.assertNotIn("Windows", copy)


if __name__ == "__main__":
    unittest.main()
