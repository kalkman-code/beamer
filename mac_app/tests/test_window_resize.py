import unittest
from unittest import mock

from mac_app.tests.test_window_geometry import offscreen_control_window, kvm_bridge_app

AppKit = kvm_bridge_app.AppKit
widgets = kvm_bridge_app.widgets
objc = kvm_bridge_app.objc


class MeasuredStack(AppKit.NSStackView):
    passes = 0

    def layout(self):
        MeasuredStack.passes += 1
        objc.super(MeasuredStack, self).layout()


def measured_stack(vertical=True, spacing=10.0):
    view = MeasuredStack.alloc().init()
    view.setTranslatesAutoresizingMaskIntoConstraints_(False)
    view.setOrientation_(1 if vertical else 0)
    view.setSpacing_(spacing)
    view.setAlignment_(AppKit.NSLayoutAttributeLeading if vertical else AppKit.NSLayoutAttributeCenterY)
    view.setDistribution_(AppKit.NSStackViewDistributionFill)
    return view


def settle(window):
    from Foundation import NSDate, NSRunLoop
    window.contentView().layoutSubtreeIfNeeded()
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.01))
    window.contentView().layoutSubtreeIfNeeded()


class WindowResizeTests(unittest.TestCase):
    def test_page_breakpoint_jitter_keeps_the_current_typography(self):
        control = offscreen_control_window()
        control._apply_width(700)
        flips = 0
        for width in [599, 601] * 10:
            before = control.wide
            control._apply_width(width)
            flips += control.wide != before
        print(f"page breakpoint jitter: {flips} flips for 20 samples", flush=True)
        self.assertEqual(flips, 0)
        control._apply_width(570)
        self.assertFalse(control.wide)
        control._apply_width(630)
        self.assertTrue(control.wide)

    def test_resize_notification_at_unchanged_width_does_not_relayout_pages(self):
        control = offscreen_control_window()
        settle(control.window)
        with mock.patch.object(control, "_apply_width", wraps=control._apply_width) as apply_width:
            for _ in range(20):
                control.windowDidResize_(None)
        self.assertEqual(apply_width.call_count, 0)

    def test_threshold_jitter_does_not_flip_crossing_pair(self):
        pair = widgets.SideBySide(widgets.stack(), widgets.stack(), 254, 354)
        horizontal = AppKit.NSUserInterfaceLayoutOrientationHorizontal
        vertical = AppKit.NSUserInterfaceLayoutOrientationVertical
        pair.view.setFrameSize_((700, 200))
        pair.update_orientation()
        self.assertEqual(pair.orientation(), horizontal)
        flips = 0
        for width in [631, 633] * 20:
            before = pair.orientation()
            pair.view.setFrameSize_((width, 200))
            pair.update_orientation()
            flips += before != pair.orientation()
        print(f"threshold jitter: {flips} flips for 40 samples", flush=True)
        self.assertEqual(flips, 0)
        pair.view.setFrameSize_((600, 200))
        pair.update_orientation()
        self.assertEqual(pair.orientation(), vertical)

    def test_resize_and_move_sweep_settles_without_layout_feedback(self):
        with mock.patch.object(widgets, "stack", measured_stack):
            control = offscreen_control_window()
        control._select_page("crossing")
        window = control.window
        previous = control.ways_pair.orientation()
        flips = 0
        ambiguous = 0
        MeasuredStack.passes = 0
        widths = list(range(640, 1601, 20)) + list(range(1600, 639, -20))
        for index, width in enumerate(widths):
            window.setFrame_display_(((100000 + index, 100000 + index), (width, 800)), False)
            settle(window)
            orientation = control.ways_pair.orientation()
            flips += orientation != previous
            previous = orientation
            self.assertAlmostEqual(window.frame().size.width, width, delta=1)
            views = (control.pages["crossing"].documentView().subviews()[0],
                     control.ways_pair.view, control.ways_pair.first, control.ways_pair.second)
            ambiguous += sum(view.hasAmbiguousLayout() for view in views)
        resize_passes = MeasuredStack.passes
        MeasuredStack.passes = 0
        for index in range(20):
            window.setFrameOrigin_((100000 + index * 10, 100000))
            settle(window)
        move_passes = MeasuredStack.passes
        MeasuredStack.passes = 0
        for _ in range(5):
            settle(window)
        print(f"sweep: {len(widths)} sizes, {resize_passes} stack layouts, {flips} Crossing flips; "
              f"20 moves: {move_passes} layouts; idle: {MeasuredStack.passes}; "
              f"ambiguous Crossing layouts: {ambiguous}", flush=True)
        self.assertLessEqual(flips, 3)
        self.assertEqual(MeasuredStack.passes, 0)
        self.assertEqual(ambiguous, 0)


if __name__ == "__main__":
    unittest.main()
