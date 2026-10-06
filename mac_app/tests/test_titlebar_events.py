import unittest
from unittest import mock

from mac_app.tests.test_window_geometry import offscreen_control_window, kvm_bridge_app
from mac_app.tests.test_window_resize import settle

AppKit = kvm_bridge_app.AppKit


def double_click(window, point):
    for kind in (AppKit.NSEventTypeLeftMouseDown, AppKit.NSEventTypeLeftMouseUp):
        event = AppKit.NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
            kind, point, 0, 0, window.windowNumber(), None, 1, 2, 1.0
        )
        window.sendEvent_(event)
    settle(window)


class TitlebarEventTests(unittest.TestCase):
    def test_titlebar_routing_excludes_traffic_lights_and_page_controls(self):
        control = offscreen_control_window()
        window = control.window
        settle(window)
        for kind in (AppKit.NSWindowCloseButton, AppKit.NSWindowMiniaturizeButton, AppKit.NSWindowZoomButton):
            button = window.standardWindowButton_(kind)
            rect = button.convertRect_toView_(button.bounds(), window.contentView().superview())
            point = AppKit.NSMakePoint(AppKit.NSMidX(rect), AppKit.NSMidY(rect))
            self.assertFalse(window.titlebar_background(point))
        self.assertFalse(window.titlebar_background(AppKit.NSMakePoint(450, 200)))

    def test_explicit_do_nothing_keeps_the_frame_on_all_backgrounds(self):
        control = offscreen_control_window()
        window = control.window
        window.setFrame_display_(((100000, 100000), (900, 700)), False)
        settle(window)
        original = window.frame()
        height = window.contentView().frame().size.height
        with mock.patch.object(kvm_bridge_app, "titlebar_action", return_value=None):
            for point in ((450, height + 10), (450, height - 9), (80, height - 30)):
                double_click(window, point)
                self.assertEqual(window.frame(), original)

    def test_real_zoom_fills_then_restores_the_previous_frame(self):
        for state in ("initial", "resized"):
            with self.subTest(state=state):
                control = offscreen_control_window()
                window = control.window
                if state == "resized":
                    window.setFrame_display_(((100000, 100000), (900, 700)), False)
                settle(window)
                original = window.frame()
                expected = kvm_bridge_app.window_fill_frame(window)
                kvm_bridge_app.perform_titlebar_double_click(window, "Fill")
                settle(window)
                print(f"direct fill ({state}): before={original}, after={window.frame()}, expected={expected}", flush=True)
                self.assertEqual(window.frame(), expected)
                kvm_bridge_app.perform_titlebar_double_click(window, "Fill")
                settle(window)
                self.assertEqual(window.frame(), original)
                self.assertFalse(window.isVisible())

    def test_titlebar_and_strip_and_sidebar_top_deliver_fill_and_restore(self):
        control = offscreen_control_window()
        window = control.window
        for region in ("native", "strip", "sidebar"):
            window.setFrame_display_(((100000, 100000), (900, 700)), False)
            settle(window)
            original = window.frame()
            expected = kvm_bridge_app.window_fill_frame(window)
            with self.subTest(region=region):
                content_height = window.contentView().frame().size.height
                if region == "native":
                    point = (450, content_height + 10)
                elif region == "strip":
                    point = (450, content_height - 9)
                else:
                    point = (80, content_height - 30)
                hit = window.contentView().superview().hitTest_(point)
                print(f"{region} double-click hit={type(hit).__name__}, point={point}", flush=True)
                # No default is changed on Toby's Mac.
                with mock.patch.object(kvm_bridge_app, "titlebar_action", return_value="fill"):
                    double_click(window, point)
                    print(f"{region} frame={window.frame()}", flush=True)
                    self.assertEqual(window.frame(), expected)
                    content_height = window.contentView().frame().size.height
                    point = (450, content_height + 10) if region == "native" else (
                        (450, content_height - 9) if region == "strip" else (80, content_height - 30)
                    )
                    double_click(window, point)
                    self.assertEqual(window.frame(), original)
                self.assertFalse(window.isVisible())


if __name__ == "__main__":
    unittest.main()
