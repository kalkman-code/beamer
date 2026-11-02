"""Crossing between this PC's own displays: the seam between two monitors meets the crossing
resistance, and a push through it lands the pointer on the next display with an arrival."""

import os
import sys
import unittest

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import links_rig
from core.return_edge import Rect, local_seams

LEFT, RIGHT = Rect(0, 0, 1920, 1080), Rect(1920, 200, 1280, 720)


class LocalSeamsTests(unittest.TestCase):
    def test_touching_displays_get_one_model_each_way_over_their_shared_stretch(self):
        models = local_seams([LEFT, RIGHT], 40)
        self.assertEqual([(m.display, m.edge, m.target_display, m.target_side) for m in models],
                         [("0", "right", "1", "left"), ("1", "left", "0", "right")])
        self.assertAlmostEqual(models[0].contact_span[0], 200 / 1080)
        self.assertEqual(models[1].contact_span, (0.0, 1.0))

    def test_one_display_or_displays_apart_have_no_seams(self):
        self.assertEqual(local_seams([LEFT]), [])
        self.assertEqual(local_seams([LEFT, Rect(5000, 0, 100, 100)]), [])


class LocalCrossingTests(unittest.TestCase):
    def rig(self, **config):
        return links_rig.Rig(cursor=(1919, 500), monitors=[LEFT, RIGHT], up=(), zones=[], **config)

    def test_a_push_through_the_seam_lands_on_the_next_display_at_the_same_height(self):
        rig = self.rig(local_crossing=True)
        rig.push(times=4, dx=20)
        self.assertEqual(rig.desktop.cursor, (1920, 500))
        self.assertTrue(any(crossed for _edge, _pressure, crossed, _part in rig.pressure))
        # Only the push's own breakthrough plays: an arrival a pixel away would show it twice.
        self.assertEqual(rig.arrivals, [])

    def test_off_by_default_and_never_while_a_button_is_held(self):
        rig = self.rig()
        rig.push(times=4, dx=20)
        self.assertEqual(rig.desktop.cursor, (1919, 500))
        rig = self.rig(local_crossing=True)
        rig.sender._buttons_held = {"left"}
        rig.push(times=4, dx=20)
        self.assertEqual(rig.desktop.cursor, (1919, 500))
        self.assertIsNone(rig.sender.seam_clip_position(1925, 500))

    def test_the_seam_holds_the_pointer_while_armed(self):
        rig = self.rig(local_crossing=True)
        self.assertEqual(rig.sender.seam_clip_position(1925, 500), (1919, 500))
        self.assertIsNone(rig.sender.seam_clip_position(1925, 100))

    def test_a_pointer_on_the_far_display_is_not_pulled_back_to_the_seam(self):
        # Measured on a 1920x1200 laptop beside a 1920x1080 monitor: a move on the laptop was held
        # at the monitor's seam, and after a crossing every move was pulled back across it.
        laptop, monitor = Rect(0, 0, 1920, 1200), Rect(1920, 0, 1920, 1080)
        rig = links_rig.Rig(cursor=(600, 946), monitors=[monitor, laptop], up=(), zones=[], local_crossing=True)
        self.assertIsNone(rig.sender.seam_clip_position(603, 946))
        rig.desktop.cursor = (1919, 900)
        rig.push(times=4, dx=50)
        self.assertEqual(rig.desktop.cursor, (1920, 900))
        self.assertIsNone(rig.sender.seam_clip_position(1941, 900))
        self.assertEqual(rig.sender.seam_clip_position(1915, 900), (1920, 900))


if __name__ == "__main__":
    unittest.main()
