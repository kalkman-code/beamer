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
    def _assert_side_by_side_layout(self, window, attribute, width, height, expected_orientation):
        AppKit = kvm_bridge_app.AppKit
        from Foundation import NSDate, NSRunLoop

        window.window.setFrame_display_(((100000, 100000), (width, height)), False)
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.25))
        self.assertEqual(window.window.frame().size.width, width)
        pair = getattr(window, attribute, None)
        self.assertIsNotNone(pair, f"{attribute} was not built")
        self.assertEqual(
            pair.orientation(), expected_orientation,
            f"pair width {pair.bounds().size.width}, threshold {pair.needed()}, window {window.window.frame().size.width}",
        )
        self.assertFalse(pair.isHidden())
        self.assertFalse(pair.first.isHidden())
        self.assertFalse(pair.second.isHidden())
        self.assertEqual(pair.first.superview(), pair.view)
        self.assertEqual(pair.second.superview(), pair.view)
        self.assertEqual(tuple(pair.view.arrangedSubviews()), (pair.first, pair.second))
        return pair

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

    def test_page_column_scales_with_free_width_and_stays_centred(self):
        window = offscreen_control_window()
        from Foundation import NSDate, NSRunLoop

        for width in (1366, 1920, 2560, 3440, 7680):
            window.window.setFrame_display_(((100000, 100000), (width, 900)), False)
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.25))
            self.assertEqual(window.window.frame().size.width, width)

            for key, scroll in window.pages.items():
                window._select_page(key)
                window.window.contentView().layoutSubtreeIfNeeded()
                page = scroll.documentView()
                body = page.subviews()[0]
                free = page.frame().size.width
                expected = kvm_bridge_app.page_column_width(free)
                with self.subTest(window_width=width, page=key):
                    if key == "design":
                        # Its tile grid keeps its intrinsic column width; the Design test checks the rows.
                        available = page.frame().size.width - sum(kvm_bridge_app.theme.PAGE_PADDING_NARROW[1::2])
                        self.assertLessEqual(body.frame().size.width, available + 1)
                    else:
                        self.assertAlmostEqual(body.frame().size.width, expected, delta=1)
                    left = body.frame().origin.x
                    right = page.frame().size.width - left - body.frame().size.width
                    self.assertAlmostEqual(left, right, delta=1)

    def test_crossing_pair_flips_without_reparenting_at_wide_and_narrow_sizes(self):
        AppKit = kvm_bridge_app.AppKit
        window = offscreen_control_window()
        window._select_page("crossing")
        horizontal = AppKit.NSUserInterfaceLayoutOrientationHorizontal
        vertical = AppKit.NSUserInterfaceLayoutOrientationVertical

        for width, height in ((1366, 900), (1920, 900), (2560, 900), (1440, 3440)):
            with self.subTest(window=(width, height)):
                pair = self._assert_side_by_side_layout(window, "ways_pair", width, height, horizontal)
                self.assertEqual(pair.needed(), 632)
                self.assertEqual(pair.gap, 24)
                self.assertEqual(pair.view.spacing(), 24)
                self.assertGreaterEqual(pair.first.frame().size.width, 254)
                self.assertGreaterEqual(pair.second.frame().size.width, 354)

        with self.subTest(window=(700, 900)):
            pair = self._assert_side_by_side_layout(window, "ways_pair", 700, 900, vertical)
            self.assertLess(pair.bounds().size.width, pair.needed())

    def test_design_controls_lead_full_width_responsive_tile_groups(self):
        window = offscreen_control_window()
        window._select_page("design")
        window.landing_box.value = True
        window._reflect()
        self.assertFalse(window.style_for_row.isHidden())
        self.assertFalse(window.effect_method_view.isHidden())
        last_group = window.glow_style_select.rows[-1]
        window.glow_style_select.value = last_group.values[-1]
        from Foundation import NSDate, NSRunLoop

        for width in (640, 1366, 2560, 1366, 640):
            window.window.setFrame_display_(((100000, 100000), (width, 900)), False)
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
            host = window.style_module.body
            controls = [view.convertRect_toView_(view.bounds(), host)
                        for view in (window.style_for_row, window.effect_method_view)]
            tiles = [tile.convertRect_toView_(tile.bounds(), host)
                     for row in window.glow_style_select.rows for _value, tile, _name in row.tiles]
            with self.subTest(width=width):
                # NSStackView is unflipped: a row's bottom must be above every tile's top.
                self.assertGreater(min(rect.origin.y for rect in controls),
                                   max(rect.origin.y + rect.size.height for rect in tiles))
                for rect in controls:
                    self.assertAlmostEqual(rect.size.width, host.bounds().size.width, delta=1)
                for rect in tiles:
                    self.assertGreaterEqual(rect.size.width, 96)
                first_row = max(rect.origin.y + rect.size.height for rect in tiles)
                count = sum(abs(rect.origin.y + rect.size.height - first_row) < 1 for rect in tiles)
                first_family = max(
                    window.glow_style_select.rows,
                    key=lambda row: max(tile.convertRect_toView_(tile.bounds(), host).origin.y
                                        + tile.convertRect_toView_(tile.bounds(), host).size.height
                                        for _value, tile, _name in row.tiles),
                )
                self.assertEqual(count, first_family.grid.columns)
                for family in window.glow_style_select.rows:
                    bounds = family.view.bounds()
                    bounds = kvm_bridge_app.AppKit.NSInsetRect(bounds, -1, -1)
                    self.assertGreater(bounds.size.width, 0)
                    self.assertGreater(bounds.size.height, 0)
                    for _value, tile, _name in family.tiles:
                        rect = tile.convertRect_toView_(tile.bounds(), family.view)
                        self.assertTrue(kvm_bridge_app.AppKit.NSContainsRect(bounds, rect),
                                        f"tile {rect} outside family row {bounds}")
                selected = next(tile for row in window.glow_style_select.rows
                                for value, tile, _name in row.tiles if value == window.glow_style_select.value)
                self.assertIsNone(window.glow_style_select.ring)
                self.assertTrue(selected.tile_selected)
                self.assertEqual(selected.layer().borderWidth(), 2)

    def test_narrow_page_uses_its_current_padding_for_available_width(self):
        window = offscreen_control_window()
        window._select_page("crossing")
        from Foundation import NSDate, NSRunLoop

        window.window.setFrame_display_(((100000, 100000), (640, 700)), False)
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
        page = window.pages["crossing"].documentView()
        body = page.subviews()[0]
        padding = kvm_bridge_app.theme.PAGE_PADDING_NARROW if not window.wide else kvm_bridge_app.theme.PAGE_PADDING
        available = page.frame().size.width - padding[1] - padding[3]
        expected = min(
            kvm_bridge_app.page_column_width(page.frame().size.width),
            page.frame().size.width - sum(padding[1::2]),
        )
        self.assertAlmostEqual(body.frame().size.width, expected, delta=1)

    def test_switch_mode_keeps_long_tile_titles_readable(self):
        window = offscreen_control_window()
        window._select_page("design")
        window.landing_box.value = True
        window.style_for_select.value = "switch"
        window._reflect()
        from Foundation import NSDate, NSRunLoop

        window.window.setFrame_display_(((100000, 100000), (640, 700)), False)
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
        _value, tile, name = next(item for row in window.switch_style_select.rows
                                  for item in row.tiles if item[0] == "match")
        single_line_height = name.view.attributedStringValue().size().height
        self.assertGreaterEqual(name.view.frame().size.height, single_line_height)
        self.assertGreaterEqual(name.view.frame().size.width, name.view.attributedStringValue().size().width)
        self.assertGreaterEqual(tile.frame().size.width, 96)
        self.assertTrue(kvm_bridge_app.AppKit.NSContainsRect(tile.bounds(),
                        name.view.convertRect_toView_(name.view.bounds(), tile)))

    def test_a_full_screen_remove_question_keeps_its_buttons_their_own_width(self):
        from types import SimpleNamespace
        from Foundation import NSDate, NSRunLoop

        window = offscreen_control_window()
        state = SimpleNamespace(word="Connected", led="signal", blink=False, tone="signal", detail="")
        row = SimpleNamespace(label="Windows PC", state=state, token="t1", platform="Windows", address="192.0.2.20",
                              in_use=True, drives=True, driven=True, phone=False)
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

        self.assertEqual(action(None), "fill")
        self.assertEqual(action(""), "fill")
        self.assertEqual(action("Maximize"), "fill")
        self.assertEqual(action("Fill"), "fill")
        self.assertEqual(action("Zoom"), "fill")
        self.assertEqual(action("Minimise"), "minimise")
        self.assertEqual(action("Minimize"), "minimise")
        self.assertIsNone(action("None"))
        self.assertIsNone(action("Do Nothing"))
        self.assertEqual(action("Unexpected"), "fill")

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
