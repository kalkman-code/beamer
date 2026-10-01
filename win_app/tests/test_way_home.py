import unittest

from core import receiver, return_edge
from core.protocol import id_text
from core.tests.responder_harness import B

PEER = id_text(B)


class WayHomeFollowsThisPcTests(unittest.TestCase):
    """A peer's pointer goes on from this PC through the zones this PC's own settings hold, the ones
    its own pointer crosses by."""

    def arm(self, zones, side="bottom"):
        peers = [{"id": PEER, "side": side}]
        return [model for _peer, model in receiver.zone_models(zones, peers, {B}, 120)]

    def test_part_of_the_edge_arms_only_its_thirds(self):
        armed = self.arm([{"peer": PEER, "kind": "part", "parts": ["start", "end"]}])
        self.assertIsInstance(armed[0], return_edge.PartEdge)
        self.assertEqual(armed[0].parts, frozenset({"start", "end"}))

    def test_a_corner_zone_arms_the_edge_it_names(self):
        armed = self.arm([{"peer": PEER, "kind": "corner", "corner": "bottom_left", "edge": "bottom"}])
        self.assertIsInstance(armed[0], return_edge.CornerPush)
        self.assertEqual(armed[0].edge, "bottom")

    def test_the_shortcut_alone_leaves_no_way_on_by_the_pointer(self):
        self.assertEqual(self.arm([]), [])

    def test_a_zone_that_is_off_is_not_armed(self):
        self.assertEqual(self.arm([{"peer": PEER, "kind": "edge", "off": True}]), [])


if __name__ == "__main__":
    unittest.main()
