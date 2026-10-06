"""Real AppKit regressions without ever ordering or activating a window."""

import unittest
import time
from unittest import mock

from mac_app.tests.design_offscreen import (
    AppKit, kvm_bridge_app, lit_pixels, offscreen_control_window, rect_list, resize_measurements, settle, tiles,
)

widgets = kvm_bridge_app.widgets


class LayoutStack(AppKit.NSStackView):
    passes = []

    def layout(self):
        LayoutStack.passes.append(self.enclosingScrollView())
        kvm_bridge_app.objc.super(LayoutStack, self).layout()


def measured_stack(vertical=True, spacing=10):
    view = LayoutStack.alloc().init()
    view.setTranslatesAutoresizingMaskIntoConstraints_(False)
    view.setOrientation_(1 if vertical else 0)
    view.setSpacing_(spacing)
    view.setAlignment_(AppKit.NSLayoutAttributeLeading if vertical else AppKit.NSLayoutAttributeCenterY)
    view.setDistribution_(AppKit.NSStackViewDistributionFill)
    return view


def raster(view):
    rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
    view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
    return rep


def accent_pixels_near_top_edge(view):
    rep = raster(view)
    left, right = int(rep.pixelsWide() * .25), int(rep.pixelsWide() * .75)
    return sum(
        1 for y in range(2, min(16, rep.pixelsHigh())) for x in range(left, right)
        if (lambda colour: colour.blueComponent() > .55 and
            colour.blueComponent() - colour.redComponent() > .18)(rep.colorAtX_y_(x, y))
    )


def raster_difference(a, b):
    if (a.pixelsWide(), a.pixelsHigh()) != (b.pixelsWide(), b.pixelsHigh()):
        return 0
    return sum(x != y for x, y in zip(bytes(a.bitmapData()), bytes(b.bitmapData())))


class DesignOffscreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        kvm_bridge_app.theme.init_fonts()

    def test_style_hint_is_present(self):
        control = offscreen_control_window()
        control._select_page("design")
        settle(control.window)
        self.assertEqual(control.style_preview_hint.view.stringValue(), "Hover to preview")

    def test_selection_marker_is_on_tile_pixels(self):
        control = offscreen_control_window()
        control._select_page("design")
        settle(control.window)
        control.glow_style_select.value = "beam"
        tile = control.glow_style_select.rows[0].tiles[1][1]
        self.assertGreater(accent_pixels_near_top_edge(tile), 0,
                           "the selected tile's own raster has no accent marker")

    def test_hover_state_changes_the_tile_raster(self):
        control = offscreen_control_window()
        control._select_page("design")
        settle(control.window)
        tile = control.glow_style_select.rows[0].tiles[1][1]
        before = raster(tile).colorAtX_y_(int(tile.bounds().size.width / 2), 1)
        owner = getattr(tile, "_tile_hover_owner", None)
        self.assertIsNotNone(owner, "the style tile has no discoverable hover-state handler")
        owner.mouseEntered_(None)
        after = raster(tile).colorAtX_y_(int(tile.bounds().size.width / 2), 1)
        contrast = sum(abs(a - b) for a, b in zip(
            (before.redComponent(), before.greenComponent(), before.blueComponent()),
            (after.redComponent(), after.greenComponent(), after.blueComponent())))
        self.assertGreater(contrast, 0.1, f"hover edge contrast was only {contrast:.3f}")
        self.assertEqual(accent_pixels_near_top_edge(tile), 0)
        owner.mouseExited_(None)

    def test_style_families_fill_the_panel_at_requested_widths(self):
        control = offscreen_control_window()
        control._select_page("design")
        columns = []
        for width, expected in ((900, 3), (1200, 3), (1500, 3), (1900, 3)):
            with self.subTest(width=width):
                control.window.setFrame_display_(((100000, 100000), (width, 900)), False)
                settle(control.window)
                self.assertAlmostEqual(control.window.frame().size.width, width, delta=.5)
                columns.append(control.glow_style_select.rows[0].columns)
                for row in control.glow_style_select.rows:
                    self.assertEqual(row.columns, expected)
                    group_frames = [rect_list(tile.convertRect_toView_(tile.bounds(), row.view))
                                    for _, tile, _ in row.tiles]
                    group_frames.sort()
                    self.assertAlmostEqual(group_frames[0][0], 0, delta=.5)
                    self.assertAlmostEqual(group_frames[-1][0] + group_frames[-1][2],
                                           row.view.bounds().size.width, delta=1)
                    for first, second in zip(group_frames, group_frames[1:]):
                        self.assertAlmostEqual(second[0] - first[0] - first[2], 12, delta=1)
                for family in control.glow_style_select.rows:
                    for preview in family.previews:
                        frame = preview.frame()
                        self.assertAlmostEqual(frame.size.width / frame.size.height, 2.0, delta=.02)
        self.assertEqual(columns, sorted(columns))

    def test_continuous_wide_resizing_does_not_layout_hidden_pages(self):
        with mock.patch.object(widgets, "stack", measured_stack):
            control = offscreen_control_window()
        control._select_page("design")
        settle(control.window)
        LayoutStack.passes = []
        control.window.setFrame_display_(((100000, 100000), (1900, 900)), False)
        control.window.contentView().layoutSubtreeIfNeeded()
        hidden = [scroll for scroll in LayoutStack.passes
                  if scroll is not None and scroll in control.pages.values() and scroll.isHidden()]
        self.assertEqual(hidden, [])

    def test_every_effect_still_contains_visible_colour_including_beam(self):
        control = offscreen_control_window()
        control._select_page("design")
        for width in (640, 760, 900, 1100, 1400, 1900):
            control.window.setFrame_display_(((100000, 100000), (width, 900)), False)
            settle(control.window)
            for row in control.glow_style_select.rows:
                for (value, _tile, _name), preview in zip(row.tiles, row.previews):
                    with self.subTest(effect=value, width=width):
                        self.assertGreater(lit_pixels(preview), 30)

    def test_resize_callbacks_fit_a_frame_budget(self):
        control = offscreen_control_window()
        control._select_page("design")
        settle(control.window)
        measured = resize_measurements(control)
        slow = [(frame["width"], frame["milliseconds"], frame["columns"])
                for frame in measured["frames"] if frame["milliseconds"] >= 16]
        print(f"Design resize: median {measured['median_ms']:.3f} ms, p95 "
              f"{measured['p95_ms']:.3f} ms, max {measured['max_ms']:.3f} ms", flush=True)
        if slow:
            details = [(frame["width"], round(frame["milliseconds"], 3),
                        round(frame["callback_ms"], 3), round(frame["layout_ms"], 3),
                        round(frame["arrange_ms"], 3), frame["wide"], frame["pair_flips"])
                       for frame in measured["frames"] if frame["milliseconds"] >= 16]
            print(f"slow frame breakdown (width, total, callback, layout, arrange, wide, pair flips): "
                  f"{details}", flush=True)
        self.assertLess(measured["p95_ms"], 16, "95% of live-resize frames must fit 16 ms")
        self.assertLess(measured["max_ms"], 25, "no live-resize frame may take 25 ms")
        self.assertEqual(measured["unsettled_frames"], [], "resize geometry changed after settling")

    def test_length_and_size_have_readable_segments_at_minimum_width(self):
        control = offscreen_control_window()
        control._select_page("design")
        control.window.setFrame_display_(((100000, 100000), control.window.minSize()), False)
        settle(control.window)
        for selector in (control.length_select, control.size_select):
            self.assertLessEqual(selector.view.frame().size.width, 240)
            self.assertEqual(selector.layout_columns, 1)
            for _, cell, words in selector.cells:
                self.assertGreaterEqual(cell.bounds().size.height, 28)
                self.assertGreaterEqual(words.view.frame().size.width, words.view.attributedStringValue().size().width)

    def test_stacked_length_and_size_share_the_field_column(self):
        control = offscreen_control_window()
        control._select_page("design")
        for width in (640, 760, 900):
            control.window.setFrame_display_(((100000, 100000), (width, 900)), False)
            settle(control.window)
            for row in (control.length_row, control.size_row):
                self.assertAlmostEqual(row.frame().origin.x, 0, delta=.5)
                self.assertAlmostEqual(row.frame().size.width, control.style_module.body.frame().size.width, delta=1)

    def test_wide_length_and_size_keep_equal_halves(self):
        control = offscreen_control_window()
        control._select_page("design")
        control.window.setFrame_display_(((100000, 100000), (1900, 900)), False)
        settle(control.window)
        self.assertAlmostEqual(control.length_select.view.frame().size.width, 240, delta=1)
        self.assertAlmostEqual(control.size_select.view.frame().size.width, 240, delta=1)
        self.assertAlmostEqual(control.length_row.frame().size.width, control.size_row.frame().size.width, delta=1)

    def test_trackpad_options_do_not_truncate_at_any_width(self):
        control = offscreen_control_window()
        control._select_page("design")
        for width in (640, 760, 900, 1100, 1400, 1900):
            control.window.setFrame_display_(((100000, 100000), (width, 900)), False)
            settle(control.window)
            for value, _cell, words in control.tick_steps_select.cells:
                with self.subTest(width=width, value=value):
                    self.assertGreaterEqual(words.view.frame().size.width,
                                            words.view.cell().cellSize().width)

    def test_wide_trackpad_options_fill_the_field_column(self):
        control = offscreen_control_window()
        control._select_page("design")
        control.window.setFrame_display_(((100000, 100000), (1900, 900)), False)
        settle(control.window)
        self.assertAlmostEqual(control.tick_steps_select.view.frame().size.width, 360, delta=1)
        self.assertFalse(control.tick_steps_pair.field_owner.stacked)

    def test_native_cursor_resets_do_not_stall_scrolling(self):
        control = offscreen_control_window()
        control._select_page("design")
        def walk(view):
            yield view
            for child in view.subviews():
                yield from walk(child)
        controls = [view for view in walk(control.pages["design"])
                    if isinstance(view, widgets.Pressable)]
        started = time.perf_counter()
        for _ in range(5):
            for view in controls:
                view.resetCursorRects()
        elapsed = (time.perf_counter() - started) * 1000
        self.assertLess(elapsed, 20, f"five cursor resets: {elapsed:.3f} ms")

    def test_scroll_changes_no_layout_and_bottom_content_clears_footer(self):
        with mock.patch.object(widgets, "stack", measured_stack):
            control = offscreen_control_window()
        control._select_page("design")
        settle(control.window)
        scroll = control.pages["design"]
        clip = scroll.contentView()
        page = scroll.documentView()
        body = page.subviews()[0]
        maximum = page.frame().size.height - clip.bounds().size.height
        LayoutStack.passes = []
        for index in range(21):
            clip.scrollToPoint_((0, maximum * index / 20))
            scroll.reflectScrolledClipView_(clip)
            control.window.contentView().layoutSubtreeIfNeeded()
        self.assertEqual(LayoutStack.passes, [])
        last = body.arrangedSubviews()[-1].convertRect_toView_(body.arrangedSubviews()[-1].bounds(), clip)
        self.assertLessEqual(last.origin.y + last.size.height, clip.bounds().origin.y + clip.bounds().size.height - 20)
        content = control.window.contentView()
        viewport = scroll.convertRect_toView_(scroll.bounds(), content)
        footer = control.message_label.view.convertRect_toView_(control.message_label.view.bounds(), content)
        self.assertGreater(viewport.origin.y, footer.origin.y + footer.size.height)
        self.assertFalse(scroll.hasHorizontalScroller())


if __name__ == "__main__":
    unittest.main()
