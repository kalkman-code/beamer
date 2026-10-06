"""Settings layout acceptance checks, rendered offscreen with Beamer's actual font."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "win_app"))

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QApplication, QAbstractButton, QComboBox, QFrame, QLabel, QPushButton, QStyle,
                               QStyleOptionButton, QWidget)

import kvm_bridge_win as kvm
import pages_win
import theme
import widgets
import app_config
from core import protocol
from core.tests import responder_harness


WIDTHS = (320, 426, 512, 640, 720, 900, 1100, 1440, 1920)
SCALES = ("1.0", "1.25", "1.5", "2.0")
RENDER_WIDTHS = (320, 640, 900, 1440, 1920)
PAGE_KEYS = tuple(key for key, _name, _purpose in pages_win.PAGES)


class NullSignal:
    def connect(self, _callback):
        pass


class NullAction:
    def __init__(self, text=""):
        self._text = text
        self.triggered = NullSignal()

    def text(self):
        return self._text

    def setText(self, text):
        self._text = text

    def setEnabled(self, _enabled):
        pass

    def setVisible(self, _visible):
        pass

    def setChecked(self, _checked):
        pass

    def isChecked(self):
        return False

    def setCheckable(self, _checkable):
        pass

    def deleteLater(self):
        pass


class NullMenu(QObject):
    def removeAction(self, _action):
        pass

    def insertAction(self, _before, _action):
        pass


class NullTray:
    def setToolTip(self, _text):
        pass

    def setIcon(self, _icon):
        pass

    def hide(self):
        pass

    def show(self):
        pass

    def showMessage(self, *_args):
        pass


class SyntheticWindow(kvm.WindowsApplication):
    def _listen(self):
        pass

    def _start_sending(self, _config):
        pass

    def _start_receiver(self, _config):
        pass

    def _build_tray(self):
        self.tray = NullTray()
        for name in ("header_action", "open_action", "status_action", "redirect_action",
                     "pause_action", "drive_action", "send_action"):
            setattr(self, name, NullAction())
        self.tray_menu = NullMenu(self)


def write_synthetic_settings(folder):
    settings = app_config.migrate(None, machine_id=protocol.id_text(responder_harness.HERE))
    settings.update(port=responder_harness.free_port(), appearance="dark", shortcut_arrival=True,
                    check_updates=False)
    first = protocol.id_text(responder_harness.B)
    settings["peers"] = [
        responder_harness.entry(responder_harness.B, "Synthetic workstation with a long machine name", side="right"),
        responder_harness.entry(responder_harness.C, "Synthetic laptop", side="left"),
    ]
    settings["zones"] = [
        {"peer": first, "kind": "part", "parts": ["start", "middle", "end"]},
        {"peer": first, "kind": "corner", "corner": "top_right", "edge": "right"},
    ]
    path = folder / "settings.json"
    app_config.write_settings(path, settings)
    return path


class SettingsLayoutAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        cls.app.setFont(theme.font(theme.TYPE["body"]))
        cls.effective_font = cls.app.font()

    def setUp(self):
        self.assertTrue(self.effective_font.family().lower().startswith("hanken"),
                        f"tests must use the bundled Hanken Grotesk font, got {self.effective_font.family()!r}")
        scratch = ROOT / "renders" / "test-working"
        scratch.mkdir(parents=True, exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(prefix="beamer-layout-acceptance-", dir=scratch)
        folder = Path(self.folder.name)
        self.window = SyntheticWindow(write_synthetic_settings(folder))
        self.window.full_screen_timer.stop()
        self.window.refresh_timer.stop()
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.window._select_page(PAGE_KEYS[0])
        self.window.show()
        self.settle()

    def tearDown(self):
        self.window.server.stop()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.folder.cleanup()

    def settle(self):
        for _ in range(7):
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
            self.app.processEvents()

    def set_size(self, width, height=900):
        self.window.resize(width, height)
        self.settle()

    def page_scroll(self, key):
        self.window._select_page(key)
        self.settle()
        return self.window.stack.currentWidget()

    def test_window_accepts_320_dip_minimum_at_every_matrix_cell(self):
        width = int(os.environ.get("BEAMER_LAYOUT_WIDTH", "320"))
        self.set_size(width, 300)
        self.assertEqual(self.window.width(), width,
                         f"requested {width} DIP but window settled at {self.window.width()} DIP")
        self.assertEqual(self.window.height(), 300,
                         f"requested 300 DIP but window settled at {self.window.height()} DIP")
        self.assertLessEqual(self.window.minimumWidth(), 320)
        self.assertLessEqual(self.window.minimumHeight(), 300)
        expected_sidebar = 56 if width < 640 else 144 if width < 800 else 176
        self.assertEqual(self.window.sidebar.width(), expected_sidebar)
        if width < 640:
            for key, button in self.window.sidebar._buttons.items():
                with self.subTest(page=key):
                    self.assertEqual((button.width(), button.height()), (44, 36))
                    self.assertAlmostEqual(button.x() + button.width() / 2,
                                           self.window.sidebar.width() / 2, delta=1)
                    self.assertTrue(button.toolTip(), f"{key} lost its accessible page-name tooltip")

    def test_follow_banner_keeps_whole_words_and_stop_following_its_natural_size(self):
        self.page_scroll("design")
        self.window._lock_followed_design({"id": "b" * 32}, "Windows workstation")
        label, button = self.window.follow_banner_label, self.window.stop_follow_button
        words = label.text().split()
        for width in WIDTHS:
            with self.subTest(width=width):
                self.set_size(width)
                self.assertTrue(self.window.follow_banner.isVisible())
                widest = max(label.fontMetrics().horizontalAdvance(word) for word in words)
                self.assertGreaterEqual(label.width(), widest, "a banner word is wider than its column")
                self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()))
                natural = button.sizeHint()
                self.assertEqual(button.width(), natural.width(), "Stop following lost its natural width")
                self.assertGreaterEqual(button.height(), natural.height())
                self.assertTrue(self.paints_on_one_line(button), "Stop following wraps or truncates its label")

    @classmethod
    def paints_on_one_line(cls, button):
        """ActionButton paints its label word-wrapped into the content rectangle, so the label has
        to leave slack there; a plain QPushButton never wraps and only has to fit."""
        metrics = button.fontMetrics()
        room = cls.button_text_room(button)
        if not isinstance(button, widgets.ActionButton):
            return metrics.horizontalAdvance(button.text()) <= room
        drawn = metrics.boundingRect(QRect(0, 0, room, 10000), int(Qt.TextFlag.TextWordWrap), button.text())
        return drawn.height() <= metrics.height() + 1 and metrics.horizontalAdvance(button.text()) < room

    @staticmethod
    def button_text_room(button):
        option = QStyleOptionButton()
        button.initStyleOption(option)
        return button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button).width()

    def test_no_button_label_wraps_or_clips_on_any_page(self):
        for width in WIDTHS:
            self.set_size(width)
            for key in PAGE_KEYS:
                scroll = self.page_scroll(key)
                for button in scroll.widget().findChildren(QPushButton):
                    text = button.text()
                    if (not text or not button.isVisibleTo(scroll.widget()) or isinstance(button, widgets._Swatch)
                            or button.property("vernier") in ("page", "foot", "choice")):
                        continue
                    with self.subTest(width=width, page=key, button=text):
                        self.assertTrue(self.paints_on_one_line(button), "the label wraps or clips inside its button")
                        self.assertGreaterEqual(button.height(), 30)

    def test_effective_dpr_matches_the_scale_of_this_process(self):
        expected = float(os.environ.get("QT_SCALE_FACTOR", "1.0"))
        self.assertAlmostEqual(self.window.devicePixelRatioF(), expected, delta=0.01,
                               msg="Qt must apply the requested DPR once while geometry stays in DIP")

    def test_every_page_uses_the_same_centred_880_dip_section_column(self):
        width = int(os.environ.get("BEAMER_LAYOUT_WIDTH", "320"))
        self.set_size(width)
        measurements = {}
        for key in PAGE_KEYS:
            scroll = self.page_scroll(key)
            page = scroll.widget()
            modules = [module for module in page.findChildren(QFrame)
                       if module.property("vernier") == "module" and module.isVisible()]
            column = page.findChild(QWidget, "page_column")
            self.assertIsNotNone(column, f"{key} has no page column")
            self.assertGreater(len(modules), 0, f"{key} has no settings sections")
            rects = [(module.mapTo(page, QPoint()).x(), module.width()) for module in modules]
            measurements[key] = rects
            expected = min(880, page.width() - 48)
            self.assertAlmostEqual(column.width(), expected, delta=1,
                                   msg=f"{key} column {column.width()} vs {expected} at {width} DIP")
            self.assertAlmostEqual(column.x(), (page.width() - expected) / 2, delta=1,
                                   msg=f"{key} column is not centred: {column.geometry().getRect()}")
            for x, module_width in rects:
                self.assertAlmostEqual(x, column.x(), delta=1,
                                       msg=f"{key} section leading edge {x}; column {column.x()}")
                self.assertAlmostEqual(module_width, column.width(), delta=1,
                                       msg=f"{key} section width {module_width}; column {column.width()}")
            self.assertLessEqual(page.width(), scroll.viewport().width() + 1,
                                 f"{key} document width {page.width()} exceeds viewport {scroll.viewport().width()}")

    def test_form_labels_keep_a_180_dip_track_without_wrapping_in_normal_windows(self):
        self.assertEqual(self.window.edge_heading.text(), "Where the other machine is")
        self.assertIn("Synthetic workstation", self.window.edge_heading.accessibleName())
        self.set_size(int(os.environ.get("BEAMER_LAYOUT_WIDTH", "900")))
        for key in PAGE_KEYS:
            scroll = self.page_scroll(key)
            column = scroll.widget().findChild(QWidget, "page_column")
            if column.width() <= 560:
                continue
            labels = [label for label in column.findChildren(QLabel)
                      if label.property("form_label") or label.objectName() == "form_label"]
            for label in labels:
                if not label.isVisible():
                    continue
                with self.subTest(page=key, label=label.text()):
                    self.assertLessEqual(label.width(), 180)
                    self.assertLessEqual(label.height(), label.fontMetrics().height() + 2,
                                         f"form label wraps: {label.text()!r}")
                    self.assertLessEqual(label.fontMetrics().horizontalAdvance(label.text()), label.width(),
                                         f"form label is clipped: {label.text()!r}")

    def test_segmented_controls_and_menu_controls_obey_their_caps(self):
        self.set_size(int(os.environ.get("BEAMER_LAYOUT_WIDTH", "900")))
        for key in PAGE_KEYS:
            column = self.page_scroll(key).widget().findChild(QWidget, "page_column")
            for control in column.findChildren(QWidget):
                if control.property("segmented_choice") or control.objectName() == "segmented_choice":
                    cap = int(control.property("segment_cap") or 360)
                    with self.subTest(page=key, control=control.objectName()):
                        self.assertLessEqual(control.width(), cap)
                        for segment in control.findChildren(QAbstractButton):
                            self.assertLessEqual(segment.width(), control.cell_width())
                            self.assertGreaterEqual(segment.height(), segment.heightForWidth(segment.width()))
            for menu in column.findChildren(QComboBox):
                with self.subTest(page=key, menu=menu.objectName()):
                    self.assertLessEqual(menu.width(), 320)

    def test_form_controls_never_overlap_their_labels_and_children_fit_the_column(self):
        self.set_size(int(os.environ.get("BEAMER_LAYOUT_WIDTH", "900")))
        for key in PAGE_KEYS:
            column = self.page_scroll(key).widget().findChild(QWidget, "page_column")
            for row in column.findChildren(widgets.FormRow):
                if not row.isVisible():
                    continue
                point = row.holder.mapTo(row, QPoint())
                with self.subTest(page=key, label=row.heading.text()):
                    if column.width() >= 560:
                        self.assertEqual(row.heading.width(), 180)
                        self.assertGreaterEqual(point.x(), row.heading.x() + row.heading.width() + 16)
                    else:
                        self.assertGreaterEqual(point.y(), row.heading.y() + row.heading.height() + 8)
            for child in column.findChildren(QWidget):
                if child.isVisible():
                    x = child.mapTo(column, QPoint()).x()
                    with self.subTest(page=key, child=type(child).__name__):
                        self.assertGreaterEqual(x, 0)
                        self.assertLessEqual(x + child.width(), column.width() + 1)

    def test_each_style_family_stays_horizontal_and_reflows_as_a_group(self):
        width = int(os.environ.get("BEAMER_LAYOUT_WIDTH", "320"))
        self.set_size(width)
        self.page_scroll("design")
        groups = self.window.glow_style_choice.groups
        column_width = self.page_scroll("design").widget().findChild(QWidget, "page_column").width()
        per_row = 3 if column_width >= 600 else 2 if column_width >= 400 else 1
        for family, choices in groups.items():
            tiles = list(choices._tiles.values())
            self.assertEqual(len(tiles), 3, f"{family} lost catalogue choices")
            positions = sorted((tile.y(), tile.x()) for tile in tiles)
            rows = {}
            for tile in tiles:
                rows.setdefault(tile.y(), []).append(tile)
            self.assertEqual(max(map(len, rows.values())), per_row,
                             f"{family}: expected at most {per_row} choices per row, got {positions}")
            for row in rows.values():
                self.assertLessEqual(max(tile.width() for tile in row) - min(tile.width() for tile in row), 1)
                self.assertEqual(len({tile.height() for tile in row}), 1)
            self.assertLessEqual(max(tile.width() for tile in tiles) - min(tile.width() for tile in tiles), 1)

    def test_colour_families_are_five_three_or_two_cells_per_row(self):
        width = int(os.environ.get("BEAMER_LAYOUT_WIDTH", "320"))
        self.set_size(width)
        self.page_scroll("design")
        view = self.window.glow_colour_choice.view
        column_width = self.page_scroll("design").widget().findChild(QWidget, "page_column").width()
        per_row = 5 if column_width >= 600 else 3 if column_width >= 400 else 2
        swatches = list(self.window.glow_colour_choice._swatches.values())
        by_y = {}
        for swatch in swatches:
            by_y.setdefault(swatch.mapTo(view, QPoint()).y(), []).append(swatch)
        self.assertEqual(max(map(len, by_y.values())), per_row,
                         f"expected {per_row} colour choices per row; rows={[len(r) for r in by_y.values()]}")
        for row in by_y.values():
            self.assertLessEqual(max(cell.width() for cell in row) - min(cell.width() for cell in row), 1)
            self.assertEqual(len({cell.height() for cell in row}), 1)
        mono_choices = [value for value in self.window.glow_colour_choice._swatches
                        if value.startswith("mono")]
        self.assertEqual(len(mono_choices), 5, "Mono must be a full five-colour family")

    def test_design_controls_stay_inside_their_logical_width_caps(self):
        self.set_size(int(os.environ.get("BEAMER_LAYOUT_WIDTH", "900")))
        self.page_scroll("design")
        capped = {
            "Length": (self.window.length_choice.view, self.window.length_choice.view.cap),
            "Size": (self.window.size_choice.view, self.window.size_choice.view.cap),
            "Style for": (self.window.design_mode_choice.view, self.window.design_mode_choice.view.cap),
            "Show at": (self.window.effect_method_choice.view, self.window.effect_method_choice.view.cap),
            "Match design with": (self.window.design_follow_choice, 320),
        }
        for label, (control, maximum) in capped.items():
            with self.subTest(control=label):
                self.assertLessEqual(control.width(), maximum,
                                     f"{label} is {control.width()} DIP; cap is {maximum}")
        for control_name in ("length_choice", "size_choice", "design_mode_choice", "effect_method_choice"):
            choice = getattr(self.window, control_name)
            for value, button in choice._buttons.items():
                with self.subTest(choice=control_name, option=value):
                    self.assertLessEqual(button.width(), choice.view.cell_width(),
                                         f"{control_name}.{value} segment exceeds its measured label width")
        self.assertLessEqual(self.window.design_follow_choice.width(), 320)

    def test_effect_preview_cache_uses_ceil_logical_size_at_effective_dpr(self):
        scale = float(os.environ.get("QT_SCALE_FACTOR", "1.0"))
        self.set_size(int(os.environ.get("BEAMER_LAYOUT_WIDTH", "900")))
        self.page_scroll("design")
        preview = self.window.effect_stills["glow"]
        preview.grab()
        self.assertIsNotNone(preview._image, "preview still was not rasterised")
        image = preview._image
        self.assertEqual(image.devicePixelRatio(), scale)
        self.assertGreaterEqual(image.width(), __import__("math").ceil(preview.width() * scale))
        self.assertGreaterEqual(image.height(), __import__("math").ceil(preview.height() * scale))

    def test_selected_style_detail_and_length_size_stay_with_style_group(self):
        self.set_size(int(os.environ.get("BEAMER_LAYOUT_WIDTH", "900")))
        self.page_scroll("design")
        style_module = self.window.look_module
        module_top = style_module.mapTo(self.window.stack.currentWidget().widget(), QPoint()).y()
        family_bottom = max(self.window.glow_style_choice.tile(value).mapTo(
            self.window.stack.currentWidget().widget(), QPoint()).y() +
            self.window.glow_style_choice.tile(value).height()
            for choices in self.window.glow_style_choice.groups.values()
            for value in choices._tiles)
        for widget in (self.window.effect_name, self.window.effect_note,
                       self.window.length_choice.view, self.window.size_choice.view):
            y = widget.mapTo(self.window.stack.currentWidget().widget(), QPoint()).y()
            self.assertGreaterEqual(y, family_bottom,
                                    f"{widget.objectName() or type(widget).__name__} precedes style rows")
            self.assertGreaterEqual(y, module_top)

    def test_sidebar_selection_tracks_keyboard_focus_and_mouse_click(self):
        self.set_size(int(os.environ.get("BEAMER_LAYOUT_WIDTH", "900")))
        sidebar = self.window.sidebar
        selected = [key for key, button in sidebar._buttons.items() if button.isChecked()]
        self.assertEqual(selected, [self.window._page], "selected sidebar page must match displayed page")
        focused = [key for key, button in sidebar._buttons.items() if button.hasFocus()]
        self.assertTrue(not focused or focused == selected,
                        f"focus must be absent or belong to selected page; got {focused}, selected {selected}")
        target = PAGE_KEYS[-1]
        QTest.mouseClick(sidebar._buttons[target], Qt.MouseButton.LeftButton)
        self.settle()
        selected = [key for key, button in sidebar._buttons.items() if button.isChecked()]
        self.assertEqual(selected, [target])
        focused = [key for key, button in sidebar._buttons.items() if button.hasFocus()]
        self.assertNotIn(target, focused, "pointer selection must not leave a keyboard focus ring")
        QApplication.setActiveWindow(self.window)
        QTest.keyClick(sidebar._buttons[target], Qt.Key.Key_Up)
        self.settle()
        current = PAGE_KEYS[PAGE_KEYS.index(target) - 1]
        selected = [key for key, button in sidebar._buttons.items() if button.isChecked()]
        focused = [key for key, button in sidebar._buttons.items() if button.hasFocus()]
        self.assertEqual(selected, [current], "arrow navigation must switch selected page immediately")
        self.assertEqual(focused, [current], "keyboard focus must follow the selected page")
        QCoreApplication.sendEvent(self.window, QEvent(QEvent.Type.WindowActivate))
        self.settle()
        selected = [key for key, button in sidebar._buttons.items() if button.isChecked()]
        self.assertEqual(selected, [current], "window activation must leave one selected page")

    def test_selected_tile_has_a_separate_keyboard_ring_that_pointer_input_clears(self):
        self.set_size(900)
        self.page_scroll("design")
        tile = self.window.glow_style_choice.tile("glow")
        QApplication.setActiveWindow(self.window)
        tile.setFocus(Qt.FocusReason.TabFocusReason)
        self.window.sidebar.keyboard_mode = False
        self.window.sidebar._update_rings()
        self.settle()

        def ink_at_focus_edge():
            image = tile.grab().toImage()
            dpr = image.devicePixelRatio()
            return image.pixelColor(image.width() // 2, round(5 * dpr)).lightness()

        pointer_ink = ink_at_focus_edge()
        QTest.keyClick(tile, Qt.Key.Key_Shift)
        self.settle()
        self.assertEqual(ink_at_focus_edge(), pointer_ink, "only Tab, Shift-Tab and the arrows show rings")
        QTest.keyClick(tile, Qt.Key.Key_Tab)
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Backtab)
        self.settle()
        self.assertIs(QApplication.focusWidget(), tile)
        self.assertTrue(tile.isChecked())
        self.assertGreater(ink_at_focus_edge(), pointer_ink + 40)
        QTest.mouseClick(self.window.footer_note, Qt.MouseButton.LeftButton)
        self.settle()
        self.assertEqual(ink_at_focus_edge(), pointer_ink)


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--matrix":
        log_root = ROOT / "renders" / "acceptance-matrix"
        log_root.mkdir(parents=True, exist_ok=True)

        def run_cell(cell):
            width, scale = cell
            env = os.environ.copy()
            env["QT_QPA_PLATFORM"] = "offscreen"
            env["QT_SCALE_FACTOR"] = scale
            env["BEAMER_LAYOUT_WIDTH"] = str(width)
            log_path = log_root / f"width-{width}-scale-{scale}.log"
            with log_path.open("w", encoding="utf-8") as log:
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), "--case"],
                    env=env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, text=True,
                )
            output = log_path.read_text(encoding="utf-8", errors="replace")
            count = int(re.search(r"Ran (\d+) tests?", output).group(1)) if re.search(
                r"Ran (\d+) tests?", output) else 0
            failed = re.search(r"FAILED \(([^)]*)\)", output)
            failure_count = sum(int(value) for value in re.findall(
                r"(?:failures|errors)=(\d+)", failed.group(1))) if failed else 0
            return {"cell": cell, "returncode": result.returncode, "tests": count,
                    "failures": failure_count, "log": log_path}

        cells = [(width, scale) for width in WIDTHS for scale in SCALES]
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(run_cell, cells))
        failures = [result for result in results if result["returncode"]]
        print(f"acceptance matrix: cells={len(results)} passed={len(results) - len(failures)} "
              f"failed={len(failures)} tests={sum(result['tests'] for result in results)} "
              f"assertion-failures={sum(result['failures'] for result in results)} "
              f"logs={log_root}")
        if failures:
            print("failed cells: " + ", ".join(
                f"{r['cell'][0]}@{r['cell'][1]}x" for r in failures), file=sys.stderr)
            raise SystemExit(1)
    elif len(sys.argv) == 2 and sys.argv[1] == "--case":
        unittest.main(argv=[sys.argv[0]])
    else:
        unittest.main()
