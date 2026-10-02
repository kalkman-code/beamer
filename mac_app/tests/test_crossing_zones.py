"""The Mac's crossing engine with zones for several machines (WIRE.md section 8): each push says
which machine its zone leads to, zones are checked corners first, then edges, then thirds, then
the notch, and a machine with no side can still be reached by its corner or the notch."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from crossing import CrossingEngine

BOUNDS = (0, 0, 1728, 1117)
RIGHT_X, LEFT_X, TOP_Y = 1727.0, 0.0, 0.0
A, B, C = "machine-a", "machine-b", "machine-c"


def peer(ident, side):
    return {"id": ident, "side": side}


def engine(peers, zones, **overrides):
    crossing = {"resistance_px": 0, "block_while_dragging": True}
    crossing.update(overrides)
    return CrossingEngine.from_zones(crossing, zones, peers)


def push(machine, x, y, dx, dy, notch_range=None, displays=None):
    return machine.feed(x, y, dx, dy, BOUNDS, 1.0, notch_range, False, displays)


class EachZoneSaysItsMachine(unittest.TestCase):
    def test_two_machines_on_two_edges_each_take_their_own_push(self):
        machine = engine([peer(A, "right"), peer(B, "left")],
                         [{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge"}])
        right = push(machine, RIGHT_X, 500.0, 20, 0)
        self.assertTrue(right.crossed)
        self.assertEqual((right.peer, right.edge, right.mac_edge), (A, "left", "right"))
        left = push(machine, LEFT_X, 500.0, -20, 0)
        self.assertTrue(left.crossed)
        self.assertEqual((left.peer, left.edge, left.mac_edge), (B, "right", "left"))

    def test_two_machines_share_one_edge_by_thirds(self):
        machine = engine([peer(A, "right"), peer(B, "right")], [
            {"peer": A, "kind": "part", "parts": ["start"]},
            {"peer": B, "kind": "part", "parts": ["end"]},
        ])
        self.assertEqual(push(machine, RIGHT_X, 100.0, 20, 0).peer, A)
        self.assertEqual(push(machine, RIGHT_X, 1000.0, 20, 0).peer, B)
        middle = push(machine, RIGHT_X, 560.0, 20, 0)
        self.assertFalse(middle.crossed)
        self.assertIsNone(middle.peer)

    def test_a_corner_wins_over_another_machines_edge(self):
        machine = engine([peer(A, "right"), peer(B, "top")], [
            {"peer": A, "kind": "edge"},
            {"peer": B, "kind": "corner", "corner": "top_right", "edge": "right"},
        ])
        step = push(machine, RIGHT_X, 2.0, 20, -20)
        self.assertTrue(step.crossed)
        self.assertEqual((step.peer, step.via), (B, "corner"))
        straight = push(machine, RIGHT_X, 2.0, 20, 0)
        self.assertEqual((straight.peer, straight.via), (A, "edge"))

    def test_a_machine_with_no_side_is_reached_by_its_corner_but_never_by_an_edge(self):
        zones = [{"peer": A, "kind": "edge"}, {"peer": A, "kind": "corner", "corner": "bottom_left", "edge": "left"}]
        self.assertFalse(push(engine([peer(A, "")], zones), LEFT_X, 500.0, -20, 0).crossed)
        corner = push(engine([peer(A, "")], zones), LEFT_X, 1115.0, -20, 20)
        self.assertEqual((corner.crossed, corner.peer), (True, A))

    def test_the_notch_leads_to_its_own_machine(self):
        machine = engine([peer(A, "right"), peer(B, "top")], [{"peer": A, "kind": "edge"}, {"peer": B, "kind": "notch"}])
        step = push(machine, 860.0, TOP_Y, 0, -20, notch_range=(800.0, 920.0))
        self.assertEqual((step.crossed, step.peer, step.via), (True, B, "notch"))

    def test_zones_that_are_off_or_name_a_machine_left_out_are_not_armed(self):
        machine = engine([peer(A, "right"), peer(B, "left")], [
            {"peer": A, "kind": "edge", "off": True},
            {"peer": B, "kind": "edge"},
            {"peer": C, "kind": "edge"},
        ])
        self.assertFalse(push(machine, RIGHT_X, 500.0, 20, 0).crossed)
        self.assertTrue(machine.armed)
        self.assertEqual(machine.peers, {B})

    def test_a_machine_not_in_use_keeps_its_zones_but_does_not_arm_them(self):
        machine = engine([peer(A, "right"), {**peer(B, "left"), "in_use": False}], [
            {"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge"},
        ])
        self.assertEqual(machine.peers, {A})
        self.assertEqual(push(machine, LEFT_X, 500.0, -20, 0).crossed, False)

    def test_nothing_in_use_is_not_armed(self):
        machine = engine([peer(A, "right")], [{"peer": A, "kind": "edge", "off": True}])
        self.assertFalse(machine.armed)

    def test_each_edge_with_nothing_beyond_its_display_is_a_wall_for_its_own_machine(self):
        # Two displays side by side, the right one shorter: below it, the left display's right edge
        # is not exposed, and the right display's own bottom is.
        displays = [(0, 0, 1000, 1117), (1000, 0, 1728, 600)]
        machine = engine([peer(A, "right"), peer(B, "bottom")],
                         [{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge"}])
        step = push(machine, 1727.0, 300.0, 20, 0, displays=displays)
        self.assertEqual((step.crossed, step.peer), (True, A))
        under = push(machine, 1500.0, 599.0, 0, 20, displays=displays)
        self.assertEqual((under.crossed, under.peer), (True, B))


class OneMachineAsBefore(unittest.TestCase):
    def test_the_old_settings_still_build_an_engine_whose_steps_name_no_machine(self):
        machine = CrossingEngine(methods=("edge",), edge="right", resistance_px=0)
        step = push(machine, RIGHT_X, 500.0, 20, 0)
        self.assertTrue(step.crossed)
        self.assertIsNone(step.peer)
        self.assertEqual(machine.edge, "right")


if __name__ == "__main__":
    unittest.main()
