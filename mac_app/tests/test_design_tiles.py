"""Measure Design tile states without ordering a window."""

import unittest

from mac_app.tests.design_offscreen import offscreen_control_window, settle, tiles
from mac_app.tests.test_design_offscreen import raster, accent_pixels_near_top_edge
import theme


def luminance(colour):
    colour = colour.colorUsingColorSpace_(theme.AppKit.NSColorSpace.deviceRGBColorSpace())
    rgb = [colour.redComponent(), colour.greenComponent(), colour.blueComponent()]
    linear = [x / 12.92 if x <= .04045 else ((x + .055) / 1.055) ** 2.4 for x in rgb]
    return sum(x * weight for x, weight in zip(linear, (.2126, .7152, .0722)))


class DesignTileMeasurements(unittest.TestCase):
    def test_hover_background_brightens_in_both_appearances(self):
        control = offscreen_control_window()
        tile = control.glow_style_select.rows[0].tiles[1][1]
        previous = theme.is_dark()
        try:
            for dark in (True, False):
                theme.set_dark(dark)
                tile.set_tile_state(selected=False, hovered=False)
                rest = theme.AppKit.NSColor.colorWithCGColor_(tile.layer().backgroundColor())
                tile.set_tile_state(hovered=True)
                hovered = theme.AppKit.NSColor.colorWithCGColor_(tile.layer().backgroundColor())
                with self.subTest(dark=dark):
                    self.assertGreater(luminance(hovered), luminance(rest))
                theme.set_dark(not dark)
                theme.repaint(tile)
                switched = theme.AppKit.NSColor.colorWithCGColor_(tile.layer().backgroundColor())
                border = theme.AppKit.NSColor.colorWithCGColor_(tile.layer().borderColor())
                with self.subTest(switched_to_dark=not dark):
                    self.assertGreater(luminance(switched), luminance(theme.colour("ground")))
                    self.assertAlmostEqual(border.alphaComponent(), .18)
        finally:
            theme.set_dark(previous)

    def test_hover_has_no_accent_and_selection_has_thicker_ring(self):
        control = offscreen_control_window()
        control._select_page("design")
        settle(control.window)
        control.glow_style_select.value = "glow"
        selected, hovered = [item[1] for item in control.glow_style_select.rows[0].tiles[:2]]
        before = tiles(control)
        hovered._tile_hover_owner.mouseEntered_(None)
        self.assertEqual(accent_pixels_near_top_edge(hovered), 0)
        self.assertGreater(accent_pixels_near_top_edge(selected), 0)
        rep = raster(selected)
        x = rep.pixelsWide() // 2
        thickness = sum(rep.colorAtX_y_(x, y).blueComponent() -
                        rep.colorAtX_y_(x, y).redComponent() > .18 for y in range(8))
        self.assertGreaterEqual(thickness, 2, f"selected ring: {thickness} pixels")
        self.assertEqual(tiles(control), before)
        hovered._tile_hover_owner.mouseExited_(None)

    def test_preview_hint_uses_secondary_ink_with_readable_contrast(self):
        control = offscreen_control_window()
        hint = control.style_preview_hint
        self.assertEqual(hint.view.stringValue(), "Hover to preview")
        self.assertEqual(hint.ink, "ink_2")
        for dark in (True, False):
            theme.set_dark(dark)
            a, b = sorted((luminance(theme.colour(hint.ink)), luminance(theme.colour("panel"))))
            self.assertGreaterEqual((b + .05) / (a + .05), 4.5)

    def test_geometry_is_unchanged_by_hover_or_selection_at_every_width(self):
        control = offscreen_control_window()
        control._select_page("design")
        for width in (900, 1200, 1500, 1900):
            control.window.setFrame_display_(((100000, 100000), (width, 900)), False)
            settle(control.window)
            baseline = tiles(control)
            for family in control.glow_style_select.rows:
                frames = [tile.convertRect_toView_(tile.bounds(), family.view) for _, tile, _ in family.tiles]
                self.assertLessEqual(max(f.size.height for f in frames) - min(f.size.height for f in frames), 1)
                self.assertLessEqual(max(f.origin.y for f in frames) - min(f.origin.y for f in frames), 1)
            hover = control.glow_style_select.rows[0].tiles[1][1]._tile_hover_owner
            hover.mouseEntered_(None)
            settle(control.window)
            self.assertEqual(tiles(control), baseline)
            hover.mouseExited_(None)
            control.glow_style_select.value = "beam"
            settle(control.window)
            self.assertEqual(tiles(control), baseline)


if __name__ == "__main__":
    unittest.main()
