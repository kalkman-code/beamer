import sys
import unittest
import logging
import threading
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kvm_bridge_app
import settings_store


def offscreen_control_window():
    AppKit = kvm_bridge_app.AppKit
    AppKit.NSApplication.sharedApplication()
    token = "synthetic-render-only"
    cfg = settings_store.editable_default_config()
    cfg.host = "192.0.2.20"
    cfg.port = 24820
    cfg.auth_token = token
    cfg.pc_name = "Windows PC"
    store = object.__new__(settings_store.SettingsStore)
    store.lock = threading.RLock()
    store._handed_token = token
    store._cache = store._complete({**store._fresh(), "peers": []})
    logger = logging.getLogger("test-window-geometry")
    from wake import WakingController

    controller = WakingController(cfg, logger=logger)
    with mock.patch.object(kvm_bridge_app, "accessibility_granted", return_value=True), mock.patch.object(
        kvm_bridge_app, "input_monitoring_granted", return_value=True
    ):
        return kvm_bridge_app.ControlWindow.alloc().initWithController_settingsStore_logger_(
            controller, store, logger
        )


class WindowGeometryTests(unittest.TestCase):
    def test_resizable_window_keeps_a_requested_full_screen_width(self):
        AppKit = kvm_bridge_app.AppKit
        window = offscreen_control_window()
        # The window takes its largest size from every constraint above WindowSizeStayPut (500),
        # so one page's rigid row or reading-width note used to cap it at 773 to 837 pt.
        screen_width = AppKit.NSScreen.mainScreen().visibleFrame().size.width

        window.window.setFrame_display_(((100000, 100000), (screen_width, 900)), False)
        from Foundation import NSDate, NSRunLoop

        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.25))

        self.assertEqual(window.window.frame().size.width, screen_width)

    def test_a_full_screen_page_keeps_its_content_column_centred(self):
        window = offscreen_control_window()
        window.window.setFrame_display_(((100000, 100000), (1728, 900)), False)
        from Foundation import NSDate, NSRunLoop

        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.25))

        # Every page: one row of a reading-width note and a button, with nothing to take the spare
        # width, held Overview's column at 706 pt and Connection's at 684.
        for key, page in window.pages.items():
            page = page.documentView()
            body = page.subviews()[0]
            with self.subTest(page=key):
                self.assertEqual(body.frame().size.width, kvm_bridge_app.theme.PAGE_CONTENT_WIDTH)
                left = body.frame().origin.x
                right = page.frame().size.width - left - body.frame().size.width
                self.assertAlmostEqual(left, right, delta=1)

    def test_a_full_screen_remove_question_keeps_its_buttons_their_own_width(self):
        from types import SimpleNamespace
        from Foundation import NSDate, NSRunLoop

        window = offscreen_control_window()
        state = SimpleNamespace(word="Connected", led="signal", blink=False, tone="signal", detail="")
        row = SimpleNamespace(label="Windows PC", state=state, token="t1", platform="Windows", address="192.0.2.20",
                              in_use=True, drives=True, driven=True)
        window.panel.removing = "t1"
        window.panel._build_rows([row])
        window.window.setFrame_display_(((100000, 100000), (1728, 900)), False)
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.25))

        def walk(view):
            yield view
            for sub in view.subviews():
                yield from walk(sub)

        chips = {view.accessibilityLabel(): view for view in walk(window.panel.list)
                 if view.accessibilityLabel() in ("Confirm removing Windows PC", "Keep Windows PC")}
        self.assertEqual(len(chips), 2)
        # The question's note stopped at the reading width and the spare width went to Keep.
        for name, chip in chips.items():
            with self.subTest(chip=name):
                self.assertAlmostEqual(chip.frame().size.width, chip.fittingSize().width, delta=1)

    def test_zoom_uses_the_visible_screen_frame(self):
        frame = ((40.0, 24.0), (1440.0, 940.0))
        screen = mock.Mock()
        screen.visibleFrame.return_value = frame
        window = mock.Mock()
        window.screen.return_value = screen
        fill_frame = getattr(kvm_bridge_app, "window_fill_frame", lambda _window: None)

        self.assertEqual(fill_frame(window), frame)

    def test_the_global_titlebar_setting_selects_fill_or_minimise(self):
        action = getattr(kvm_bridge_app, "titlebar_action", lambda _setting: None)

        self.assertEqual(action("Fill"), "fill")
        self.assertEqual(action("Zoom"), "fill")
        self.assertEqual(action("Minimise"), "minimise")

    def test_double_click_applies_the_selected_titlebar_action(self):
        perform = getattr(kvm_bridge_app, "perform_titlebar_double_click", lambda *_args: None)
        window = mock.Mock()

        perform(window, "Fill")
        window.zoom_.assert_called_once_with(None)

        window.reset_mock()
        perform(window, "Minimise")
        window.miniaturize_.assert_called_once_with(None)

    def test_single_click_on_the_top_strip_starts_a_window_drag(self):
        strip = kvm_bridge_app.TitlebarStrip.alloc().initWithFrame_(((0, 0), (1, 1)))
        event = mock.Mock()
        event.clickCount.return_value = 1

        with mock.patch.object(kvm_bridge_app, "perform_titlebar_drag") as drag:
            strip.mouseDown_(event)

        drag.assert_called_once_with(strip.window(), event)


if __name__ == "__main__":
    unittest.main()
