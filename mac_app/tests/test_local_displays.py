"""Crossings between this Mac's own displays: the geometry that finds a slide from one display onto
another, and what the overlay's Player gets for one."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import crossing
from core import effects
from core.return_edge import Rect
import effects_overlay
import local_displays

# A 3440x1440 primary with a 1920x1080 display below it, shifted right, as Quartz reports them.
STACKED = [(0, 0, 3440, 1440), (1074, 1440, 1920, 1080)]
SIDE_BY_SIDE = [(0, 0, 1920, 1080), (1920, -360, 2560, 1440)]


class CrossingGeometryTest(unittest.TestCase):
    def test_sliding_down_onto_the_display_below(self):
        found = local_displays.crossing_between((2000, 1438), (2004, 1441), STACKED)
        self.assertEqual(found, ((2004, 1439), "bottom", (2004, 1441), "top"))

    def test_sliding_back_up(self):
        found = local_displays.crossing_between((1500, 1442), (1500, 1438), STACKED)
        self.assertEqual(found, ((1500, 1440), "top", (1500, 1438), "bottom"))

    def test_sliding_right_onto_a_taller_display(self):
        found = local_displays.crossing_between((1918, 20), (1921, 20), SIDE_BY_SIDE)
        self.assertEqual(found, ((1919, 20), "right", (1921, 20), "left"))

    def test_sliding_left_off_a_taller_display_clamps_to_the_edge_it_left(self):
        found = local_displays.crossing_between((1922, 1070), (1919, 1070), SIDE_BY_SIDE)
        self.assertEqual(found, ((1920, 1070), "left", (1919, 1070), "right"))

    def test_moving_within_one_display_is_not_a_crossing(self):
        self.assertIsNone(local_displays.crossing_between((100, 100), (140, 120), STACKED))

    def test_a_warp_onto_another_display_is_not_a_crossing(self):
        # An arrival from another machine or a switch puts the pointer there in one jump.
        self.assertIsNone(local_displays.crossing_between((100, 100), (2000, 2000), STACKED))

    def test_no_previous_point_or_a_point_off_every_display(self):
        self.assertIsNone(local_displays.crossing_between(None, (2000, 2000), STACKED))
        self.assertIsNone(local_displays.crossing_between((10, 1430), (10, 1445), STACKED))


class _Controller:
    def __init__(self, style="glow", on=True):
        self.cfg = SimpleNamespace(crossing=dict(crossing.DEFAULT_CROSSING, glow_style=style, local_displays=on))

    def _current_desktop_bounds(self):
        return (0.0, 0.0, 3440.0, 2520.0)


class OverlayLocalCrossingTest(unittest.TestCase):
    def setUp(self):
        mock.patch.object(effects_overlay.desktop_mac, "monitors",
                          return_value=[Rect(*display) for display in STACKED]).start()
        self.addCleanup(mock.patch.stopall)

    def _overlay(self, controller):
        overlay = effects_overlay.EffectsOverlay(controller, mock.Mock())
        overlay.timer = mock.Mock()
        overlay.timer.is_alive.return_value = False
        overlay._notch = mock.Mock(return_value=None)
        mock.patch.object(overlay, "_draw").start()
        return overlay

    def test_wanted_only_with_the_switch_on_and_crossings_animated(self):
        overlay = self._overlay(_Controller())
        self.assertTrue(overlay.wanted_local(overlay.controller.cfg.crossing))
        self.assertFalse(overlay.wanted_local(dict(overlay.controller.cfg.crossing, local_displays=False)))
        self.assertFalse(overlay.wanted_local(dict(overlay.controller.cfg.crossing, glow=False)))
        self.assertFalse(crossing.DEFAULT_CROSSING["local_displays"])

    def test_departure_where_it_left_and_arrival_where_it_landed(self):
        overlay = self._overlay(_Controller(style="rupture"))
        overlay.local_crossing((2004, 1439), "bottom", (2004, 1441), "top")
        depart, arrival = overlay.player.depart, overlay.player.arrival
        self.assertIsNotNone(depart["crossed_at"])
        self.assertEqual((depart["region"]["edge"], depart["display"]), ("bottom", (0, 0, 3440, 1440)))
        self.assertEqual((arrival["edge"], arrival["at"], arrival["display"]),
                         ("top", (2004, 1441), (1074, 1440, 1920, 1080)))
        self.assertIs(overlay.fx, effects.effect("rupture"))
        overlay.timer.start.assert_called()
        self.assertFalse(overlay.disabled)

    def test_glow_plays_as_the_design_preview_draws_it(self):
        overlay = self._overlay(_Controller(style="glow"))
        overlay.local_crossing((2004, 1439), "bottom", (2004, 1441), "top")
        self.assertIs(overlay.fx, effects.preview_effect("glow"))
        # Glow has no arrival of its own: the ring shows where the pointer landed.
        self.assertIs(overlay.player.arrival["fx"], effects.LOCATOR)

    def test_every_style_draws_frames_on_both_displays(self):
        for style in ("glow", "beam") + effects.EFFECT_IDS:
            overlay = self._overlay(_Controller(style=style))
            overlay.local_crossing((2004, 1439), "bottom", (2004, 1441), "top")
            later = overlay.player.depart["crossed_at"] + 0.1
            pens = overlay.player.frames(later, overlay.fx, (3440.0, 2520.0, "mac"), overlay.palette, False, True)
            clips = {getattr(pen, "clip", None) for pen in pens}
            self.assertIn((1074, 1440, 2994, 2520), clips, style)


if __name__ == "__main__":
    unittest.main()
