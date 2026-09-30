import sys
import unittest
from types import SimpleNamespace


@unittest.skipUnless(sys.platform == "win32", "the app module needs Windows")
class WayHomeFollowsThisPcTests(unittest.TestCase):
    """The Mac's pointer comes home through this PC's edge as this PC's own settings say."""

    def model(self, methods, parts=("middle",), corner="bottom_left", edge="bottom"):
        import kvm_bridge_win

        config = SimpleNamespace(crossing_methods=list(methods), crossing_edge_parts=list(parts), crossing_corner=corner)
        return kvm_bridge_win.WindowsApplication._return_model(SimpleNamespace(_config=config), edge, 120)

    def test_part_of_the_edge_arms_only_its_thirds(self):
        from core import return_edge

        armed = self.model(["part", "shortcut"], parts=("start", "end"))
        self.assertIsInstance(armed, return_edge.PartEdge)
        self.assertEqual(armed.parts, frozenset({"start", "end"}))

    def test_a_corner_on_that_edge_and_none_elsewhere(self):
        from core import return_edge

        self.assertIsInstance(self.model(["corner"], corner="bottom_left"), return_edge.CornerPush)
        self.assertIsNone(self.model(["corner"], corner="top_left"))

    def test_the_shortcut_alone_leaves_no_way_home_by_the_pointer(self):
        self.assertIsNone(self.model(["shortcut"]))


if __name__ == "__main__":
    unittest.main()
