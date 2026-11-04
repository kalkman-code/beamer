"""The Mac window in another language, built offscreen.

In the pseudo-language every string that went through core.i18n is bracketed, so text that is not
bracketed skipped tr(). In Italian, the first real language, no label on the Crossing, Design and
Overview pages or the pairing sheet is clipped or overlaps another at the narrowest window.
"""

import os
import re
import socket
import unittest

from core.i18n import install, LANGUAGES


def walk(AppKit, view):
    yield view
    if isinstance(view, (AppKit.NSTextField, AppKit.NSButton)):
        return
    for child in view.subviews():
        if not child.isHidden():
            yield from walk(AppKit, child)


class _Localised(unittest.TestCase):
    LANGUAGE = ""

    @classmethod
    def setUpClass(cls):
        os.environ["BEAMER_LANGUAGE"] = cls.LANGUAGE
        install("auto")
        from mac_app.tests.design_offscreen import AppKit, offscreen_control_window

        cls.AppKit = AppKit
        cls.control = offscreen_control_window()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("BEAMER_LANGUAGE", None)
        install("en")


class PseudoLanguageTests(_Localised):
    LANGUAGE = "qps"

    def test_settings_pages_and_pairing_window_translate_visible_text(self):
        from mac_app.tests.design_offscreen import settle
        from mac_app.key_codes import KEY_SYMBOLS, KEY_TITLES

        control = self.control
        # The synthetic config supplies the peer, its address and port; the machine running the test
        # shows its own name; recorded keys show their names and glyphs.
        supplied = {"Windows PC", "192.0.2.20", "24820", "synthetic-render-only",
                    socket.gethostname(), socket.gethostname().split(".", 1)[0]}
        supplied.update(KEY_TITLES.values())
        supplied.update(KEY_SYMBOLS.values())
        supplied.update(f"{symbol} {KEY_TITLES[key]}" for key, symbol in KEY_SYMBOLS.items())
        for key in control.pages:
            control._select_page(key)
            settle(control.window)
            with self.subTest(page=key):
                self._assert_qps_text(walk(self.AppKit, control.window.contentView()), supplied)
        control._select_page("overview")
        control.panel._toggle_sheet()
        settle(control.window)
        self._assert_qps_text(walk(self.AppKit, control.window.contentView()), supplied)
        self.assertFalse(control.window.isVisible())

    def _assert_qps_text(self, views, supplied):
        for view in views:
            if isinstance(view, self.AppKit.NSTextField):
                text = str(view.attributedStringValue().string())
            elif isinstance(view, self.AppKit.NSButton):
                text = str(view.title())
                if isinstance(view, self.AppKit.NSPopUpButton):
                    for index in range(view.numberOfItems()):
                        self._assert_translated(str(view.itemTitleAtIndex_(index)), supplied)
            else:
                continue
            self._assert_translated(text, supplied)
            label = view.accessibilityLabel()
            if label:
                self._assert_translated(str(label), supplied)

    def _assert_translated(self, text, supplied):
        text = text.strip()
        if not text or text in supplied or set(text) <= set("0123456789 ·:/.-%"):
            return
        if re.fullmatch(r"\d+(?:\.\d+)?(?: ms| px| pt| s|%)", text):
            return
        if re.fullmatch(r"— (?:ms|px|pt|s)", text):
            return
        # Language names stay in their own language; key symbols and the product name are not prose.
        if text in {"Beamer", "⌃", "⌥", "⇧", "⌘"} | set(LANGUAGES.values()):
            return
        self.assertTrue(text.startswith("⟦") and text.endswith("⟧"), repr(text))


class ItalianLayoutTests(_Localised):
    LANGUAGE = "it"

    def test_shake_sensitivity_range_fits_beside_its_italian_heading(self):
        from mac_app.tests.design_offscreen import settle

        control = self.control
        control._select_page("crossing")
        control.window.setFrame_display_(
            ((100000, 100000), (max(320, control.window.minSize().width), 900)), False)
        figure = control.shake_sensitivity_figure
        original = figure.text
        try:
            for value in (50, 100, 200):
                figure.set(text=str(value))
                settle(control.window)
                with self.subTest(sensitivity=value):
                    self._assert_label_geometry(control.pages["crossing"].documentView())
                    self.assertFalse(figure.view.hasAmbiguousLayout())
        finally:
            figure.set(text=original)
        self.assertFalse(control.window.isVisible())

    def test_labels_fit_at_the_narrowest_window(self):
        from mac_app.tests.design_offscreen import settle

        control = self.control
        width = max(320, control.window.minSize().width)
        for key in ("crossing", "design", "overview"):
            control.window.setFrame_display_(((100000, 100000), (width, 900)), False)
            control._select_page(key)
            settle(control.window)
            with self.subTest(page=key):
                self._assert_label_geometry(control.pages[key].documentView())
        control._select_page("overview")
        control.panel._toggle_sheet()
        settle(control.window)
        with self.subTest(page="pairing"):
            self._assert_label_geometry(control.pages["overview"].documentView())
        self.assertFalse(control.window.isVisible())

    def _assert_label_geometry(self, root):
        AppKit = self.AppKit
        labels = [view for view in walk(AppKit, root) if isinstance(view, AppKit.NSTextField)]
        for label in labels:
            text = label.attributedStringValue()
            available = label.cell().drawingRectForBounds_(label.bounds()).size.width
            if label.cell().wraps():
                measured = text.boundingRectWithSize_options_(
                    (available, 10000), AppKit.NSStringDrawingUsesLineFragmentOrigin)
                self.assertLessEqual(measured.size.height, label.bounds().size.height + 1, str(text.string()))
            else:
                self.assertLessEqual(text.size().width, available + 1, str(text.string()))
        frames = [(label, label.convertRect_toView_(label.bounds(), root)) for label in labels]
        for index, (first, rect) in enumerate(frames):
            for second, other in frames[index + 1:]:
                overlap = AppKit.NSIntersectionRect(rect, other)
                if overlap.size.width > 1 and overlap.size.height > 1:
                    self.fail(f"labels overlap: {first.stringValue()!r} and {second.stringValue()!r}")


if __name__ == "__main__":
    unittest.main()
