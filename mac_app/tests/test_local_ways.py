"""Crossing between this Mac's own displays: each seam is a way whose peer is LOCAL, and a push
through it crosses to the neighbouring display at the same height."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crossing import LOCAL, CrossingEngine, local_ways

DISPLAYS = [(0, 0, 1728, 1117), (1728, 0, 3648, 1080)]
BOUNDS = (0, 0, 3648, 1117)


class LocalWaysTests(unittest.TestCase):
    def test_one_way_each_side_of_the_seam_and_none_for_one_display(self):
        ways = local_ways(DISPLAYS)
        self.assertEqual([(w.peer, w.display, w.edge, w.target_display, w.target_side) for w in ways],
                         [(LOCAL, "0", "right", "1", "left"), (LOCAL, "1", "left", "0", "right")])
        self.assertEqual(local_ways(DISPLAYS[:1]), [])
        self.assertEqual(local_ways(None), [])

    def test_a_push_through_the_seam_crosses_to_the_next_display(self):
        engine = CrossingEngine(resistance_px=0, ways=local_ways(DISPLAYS))
        step = engine.feed(1727.0, 500.0, 20, 0, BOUNDS, 1.0, None, False, DISPLAYS)
        self.assertTrue(step.crossed)
        self.assertEqual((step.peer, step.display, step.side), (LOCAL, "1", "left"))
        x, y = CrossingEngine.arrival_point(step.edge, step.offset, BOUNDS, displays=DISPLAYS,
                                            display=step.display, side=step.side)
        self.assertGreaterEqual(x, 1728)
        self.assertAlmostEqual(y, 500.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()
