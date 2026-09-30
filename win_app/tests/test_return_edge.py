import unittest
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.return_edge import CROSS, HOLD, PASS, Rect, ReturnEdge, arrival_position, edge_offset, union


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# A 1080p monitor hanging off the left of a 1440p primary, so the bounding box
# starts at x=-1920 and the primary is not its origin.
LEFT = Rect(-1920, 200, 1920, 1080)
PRIMARY = Rect(0, 0, 2560, 1440)
ARRANGEMENT = [PRIMARY, LEFT]
BOUNDS = union(ARRANGEMENT)


class GeometryTests(unittest.TestCase):
    def test_union_spans_a_negative_origin(self):
        self.assertEqual(BOUNDS, Rect(-1920, 0, 4480, 1440))
        self.assertEqual(BOUNDS.right, 2559)
        self.assertEqual(BOUNDS.bottom, 1439)

    def test_offset_is_measured_along_the_edge_against_the_bounding_box(self):
        self.assertAlmostEqual(edge_offset(BOUNDS, "left", (-1920, 720)), 0.5)
        self.assertAlmostEqual(edge_offset(BOUNDS, "top", (-1920 + 1120, 0)), 0.25)
        self.assertEqual(edge_offset(BOUNDS, "right", (2559, 99999)), 1.0)

    def test_arrival_lands_on_the_monitor_owning_each_edge(self):
        # The offset is taken against the whole box, and lands on the outermost monitor spanning
        # that point: the left monitor for most of the left edge, the primary above it, where the
        # primary's own left edge is the wall.
        self.assertEqual(arrival_position(ARRANGEMENT, "left", 0.5), (-1920, 720))
        self.assertEqual(arrival_position(ARRANGEMENT, "left", 0.0), (0, 0))
        # right: the primary, at its last pixel column.
        self.assertEqual(arrival_position(ARRANGEMENT, "right", 0.5), (2559, 720))
        # top: 10% of 4480 from x=-1920 is over the left monitor, whose top is exposed at y=200.
        self.assertEqual(arrival_position(ARRANGEMENT, "top", 0.1), (-1472, 200))
        self.assertEqual(arrival_position(ARRANGEMENT, "top", 0.75), (1440, 0))
        # bottom: the primary reaches y=1439; the left monitor stops at 1279.
        self.assertEqual(arrival_position(ARRANGEMENT, "bottom", 1.0), (2559, 1439))


class PressureTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.edge = ReturnEdge("left", resistance_px=120, clock=self.clock)

    def feed(self, pointer, dx, dy=0, after=0.0):
        self.clock.advance(after)
        return self.edge.feed(ARRANGEMENT, pointer, dx, dy)

    def test_movement_away_from_the_edge_passes_through(self):
        self.assertEqual(self.feed((-1000, 500), -40).action, PASS)
        self.assertEqual(self.feed((-1920, 500), 5).action, PASS)
        self.assertEqual(self.edge.pressure, 0.0)

    def test_outward_push_at_the_edge_holds_and_accumulates(self):
        outcome = self.feed((-1920, 500), -30, 4)
        self.assertEqual(outcome.action, HOLD)
        self.assertAlmostEqual(outcome.pressure, 0.25)
        # Pinned to the edge; the lateral component still slides along it.
        self.assertEqual(outcome.position, (-1920, 504))
        outcome = self.feed((-1920, 504), -30)
        self.assertAlmostEqual(outcome.pressure, 0.5)

    def test_inward_movement_subtracts(self):
        self.feed((-1920, 500), -60)
        outcome = self.feed((-1920, 500), 24)
        self.assertEqual(outcome.action, PASS)
        self.assertAlmostEqual(self.edge.pressure, 0.3)

    def test_pressure_decays_to_nothing_over_400ms(self):
        self.feed((-1920, 500), -60)
        self.assertAlmostEqual(self.feed((-1920, 500), 0, after=0.2).pressure, 0.0)
        self.feed((-1920, 500), -60)
        self.assertAlmostEqual(self.feed((-1920, 500), 0, after=0.1).pressure, 0.25)

    def test_breakthrough_sends_the_opposite_edge_and_the_offset(self):
        self.feed((-1920, 720), -100)
        outcome = self.feed((-1920, 720), -20)
        self.assertEqual(outcome.action, CROSS)
        self.assertEqual(outcome.edge, "right")
        self.assertAlmostEqual(outcome.offset, 0.5)
        self.assertEqual(outcome.pressure, 1.0)
        # Disarmed until the Mac's next switch: deltas still in flight pass.
        self.assertFalse(self.edge.armed)
        self.assertEqual(self.feed((-1920, 720), -200).action, PASS)

    def test_zero_resistance_crosses_on_contact(self):
        edge = ReturnEdge("bottom", resistance_px=0, clock=self.clock)
        outcome = edge.feed(ARRANGEMENT, (100, 1439), 0, 1)
        self.assertEqual(outcome.action, CROSS)
        self.assertEqual(outcome.edge, "top")

    def test_a_shorter_monitors_outer_edge_counts(self):
        # The left monitor stops at y=1279, well short of the box's 1439, with
        # nothing below it: the pointer is stopped there, so it is a way home.
        edge = ReturnEdge("bottom", resistance_px=120, clock=self.clock)
        self.assertEqual(edge.feed(ARRANGEMENT, (-1000, 1279), 0, 50).action, HOLD)
        self.assertEqual(edge.feed(ARRANGEMENT, (1000, 1439), 0, 50).action, HOLD)
        top = ReturnEdge("top", resistance_px=120, clock=self.clock)
        self.assertEqual(top.feed(ARRANGEMENT, (-1000, 200), 0, -50).action, HOLD)

    def test_an_edge_shared_with_another_monitor_does_not_count(self):
        # x=0 on the primary meets the left monitor between y=200 and 1279.
        edge = ReturnEdge("left", resistance_px=120, clock=self.clock)
        self.assertEqual(edge.feed(ARRANGEMENT, (0, 500), -50, 0).action, PASS)
        self.assertEqual(edge.feed(ARRANGEMENT, (0, 100), -50, 0).action, HOLD)

    def test_rejects_an_unknown_edge(self):
        with self.assertRaises(ValueError):
            ReturnEdge("sideways")



class PartEdgeTests(unittest.TestCase):
    def test_only_the_chosen_thirds_cross(self):
        from core.return_edge import PartEdge

        # Thirds are the pointer's own display's: the left monitor spans 200 to 1280, so they
        # break at 560 and 920.
        for y, crosses in ((300, False), (700, True), (1100, False)):
            with self.subTest(y=y):
                model = PartEdge("left", ("middle",), resistance_px=40, clock=FakeClock())
                outcome = None
                for _ in range(5):
                    outcome = model.feed(ARRANGEMENT, (-1920, y), -20, 0)
                    if outcome.action == CROSS:
                        break
                self.assertEqual(outcome.action == CROSS, crosses)

    def test_a_short_display_beside_a_tall_one_keeps_every_third(self):
        # Measured on the 1440-tall bounding box, the 900-tall display's end third would start at
        # 960 and most of it would not exist.
        from core.return_edge import display_fraction

        short, tall = Rect(0, 0, 1440, 900), Rect(-2560, 0, 2560, 1440)
        self.assertAlmostEqual(display_fraction([tall, short], "right", (1439, 800)), 800 / 900)

    def test_parts_are_named_by_thirds(self):
        from core.return_edge import in_parts, part_of

        self.assertEqual([part_of(f) for f in (0.0, 0.34, 0.99, 1.0)], ["start", "middle", "end", "end"])
        self.assertFalse(in_parts(0.1, ("middle",)))


class LandingMonitorTests(unittest.TestCase):
    def test_two_stacked_monitors_each_take_their_own_stretch_of_the_edge(self):
        stacked = [Rect(0, 0, 1920, 1080), Rect(0, 1080, 1920, 1080)]
        self.assertEqual(arrival_position(stacked, "left", 0.2), (0, 432))
        self.assertEqual(arrival_position(stacked, "left", 0.8), (0, 1728))

    def test_side_by_side_monitors_share_the_bottom_edge(self):
        side = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1920, 1080)]
        self.assertEqual(arrival_position(side, "bottom", 0.75)[0], 2880)

    def test_a_staggered_arrangement_lands_on_whichever_edge_is_the_wall(self):
        staggered = [Rect(0, 0, 1920, 1080), Rect(1920, 300, 1280, 720)]
        # Past the short monitor's bottom, the tall one's right edge is the wall.
        self.assertEqual(arrival_position(staggered, "right", 0.95), (1919, 1026))
        self.assertEqual(arrival_position(staggered, "right", 0.5), (3199, 540))

    def test_a_monitor_left_of_and_above_the_primary(self):
        monitors = [Rect(0, 0, 1728, 1117), Rect(-2560, -200, 2560, 1440)]
        self.assertEqual(arrival_position(monitors, "left", 0.0), (-2560, -200))
        self.assertEqual(arrival_position(monitors, "right", 0.5)[0], 1727)


if __name__ == "__main__":
    unittest.main()
