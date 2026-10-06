"""The live Beam's inward fade, through real Core Animation layers with no windows."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from beam_live_offscreen import live_glow, profile, layer_profile


class LiveBeamFalloffTest(unittest.TestCase):
    def test_every_edge_and_size_fades_before_the_panel_ends_at_both_scales(self):
        for size in ("small", "medium", "large"):
            for edge in ("left", "right", "top", "bottom"):
                for scale in (1, 2):
                    with self.subTest(size=size, edge=edge, scale=scale):
                        values = profile(live_glow(edge, size), edge, scale)
                        self.assertGreater(values[0], 20)
                        self.assertLessEqual(values[-1], 5)
                        self.assertGreater(values[len(values) // 4], values[len(values) // 2])

    def test_live_falloff_matches_the_windows_live_gradient(self):
        values = profile(live_glow("left", "large"), "left", 2)
        depth = 67.02 * 2
        stops = ((0, 1), (.08, .85), (.3, .25), (1, 0))
        for index, alpha in enumerate(values):
            at = (index + .5) / depth
            expected = 0
            for (a, av), (b, bv) in zip(stops, stops[1:]):
                if a <= at <= b:
                    expected = av + (bv - av) * (at - a) / (b - a)
                    break
            # The along-edge comet is at its midpoint; mask sampling can round by one alpha unit.
            self.assertAlmostEqual(alpha / values[0], expected / (1 - .15 * .5 / (.08 * depth)), delta=.025)

    def test_both_corner_walls_fade_inward(self):
        for corner in ("top_left", "top_right", "bottom_left", "bottom_right"):
            glow = live_glow("right", "medium", corner=corner)
            for layer, wall in zip((glow.fill, glow.side), corner.split("_")):
                values = layer_profile(layer, wall, 2)
                with self.subTest(corner=corner, wall=wall):
                    self.assertGreater(values[0], 10)
                    self.assertLessEqual(values[-1], 2)

    def test_a_third_keeps_its_along_edge_taper_and_inward_fade(self):
        glow = live_glow("left", "medium", third=True)
        values = profile(glow, "left", 2)
        self.assertGreater(values[0], 200)
        self.assertLessEqual(values[-1], 2)
        from beam_live_offscreen import render
        width, height, rgba = render(glow.panel.contentView().layer(), 2)
        self.assertLess(rgba[3], 5)
        self.assertLess(rgba[((height - 2) * width) * 4 + 3], 5)

    def test_switching_back_to_glow_removes_the_beam_mask(self):
        glow = live_glow("left", "medium")
        glow.controller.cfg.crossing["glow_style"] = "glow"
        # The real reconfiguration path, held offscreen by OffscreenPanel.
        from unittest import mock
        import kvm_bridge_app
        with mock.patch.object(kvm_bridge_app.time, "monotonic", return_value=100):
            glow._draw()
        values = profile(glow, "left", 2)
        self.assertGreater(values[len(values) // 2], 200)


if __name__ == "__main__":
    unittest.main()
