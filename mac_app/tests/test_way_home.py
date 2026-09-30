import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import return_edge
from windows_input import WindowsInput


def model(methods, parts=("middle",), corner="top_right", edge="right", notch_range=(656.0, 856.0)):
    crossing = {"methods": list(methods), "edge_parts": list(parts), "corner": corner}
    owner = SimpleNamespace(_controller=SimpleNamespace(cfg=SimpleNamespace(crossing=crossing), notch_range=notch_range))
    return WindowsInput._return_model(owner, edge, 120)


def crosses_at(armed, x):
    """Whether a hard push up at `x` along the top of a 1512x982 display goes home."""
    display = [return_edge.Rect(0, 0, 1512, 982)]
    return any(armed.feed(display, (x, 0), 0, -40).action == return_edge.CROSS for _ in range(20))


class WayHomeFollowsThisMacTests(unittest.TestCase):
    """The PC's pointer comes home through this Mac's edge as this Mac's own settings say."""

    def test_part_of_the_edge_arms_only_its_thirds(self):
        armed = model(["part", "shortcut"], parts=("start",))
        self.assertIsInstance(armed, return_edge.PartEdge)
        self.assertEqual(armed.parts, frozenset({"start"}))

    def test_the_whole_edge(self):
        self.assertEqual(type(model(["edge"])), return_edge.ReturnEdge)

    def test_the_notch_arms_only_the_notch(self):
        # Toby, 28-09-2026: with only the notch and the shortcut on, the PC's pointer went home
        # anywhere along the Mac's top edge.
        self.assertTrue(crosses_at(model(["notch", "shortcut"], edge="top"), 756))
        self.assertFalse(crosses_at(model(["notch", "shortcut"], edge="top"), 200))
        self.assertFalse(crosses_at(model(["notch", "shortcut"], edge="top"), 1300))
        self.assertIsNone(model(["notch", "shortcut"], edge="right"))

    def test_the_notch_is_read_at_each_push(self):
        # A focus that arrives before the first measurement, or across a display change, must not
        # leave the pointer without its way home for the rest of the session.
        controller = SimpleNamespace(cfg=SimpleNamespace(crossing={"methods": ["notch", "shortcut"]}), notch_range=None)
        armed = WindowsInput._return_model(SimpleNamespace(_controller=controller), "top", 120)
        self.assertFalse(crosses_at(armed, 756))
        controller.notch_range = (656.0, 856.0)
        self.assertTrue(crosses_at(armed, 756))

    def test_the_whole_edge_still_wins_over_the_notch(self):
        self.assertTrue(crosses_at(model(["edge", "notch"], edge="top"), 200))

    def test_a_corner_on_that_edge(self):
        self.assertIsInstance(model(["corner"], corner="top_right"), return_edge.CornerPush)
        self.assertIsNone(model(["corner"], corner="top_left"))

    def test_the_shortcut_alone_leaves_no_way_home_by_the_pointer(self):
        self.assertIsNone(model(["shortcut"]))


class CrossingChangesReachTheWayHomeTests(unittest.TestCase):
    def test_a_change_to_the_ways_in_rebuilds_the_way_home_once(self):
        # Toby, 28-09-2026: a change to Part of the edge applied only after crossing out and back.
        owner = SimpleNamespace(server=mock.Mock(input_scale=None), _ways=None, _running_for=None, _lock=threading.RLock())
        crossing = {"methods": ["part"], "edge_parts": ["start"], "corner": "top_right"}
        cfg = SimpleNamespace(pointer_speed=1.0, scroll_speed=1.0, reverse_scroll=False,
                              allow_windows_to_drive=False, crossing=crossing)
        WindowsInput.sync(owner, cfg)
        WindowsInput.sync(owner, cfg)
        self.assertEqual(owner.server.rearm_return.call_count, 1)
        crossing["edge_parts"] = ["end"]
        WindowsInput.sync(owner, cfg)
        self.assertEqual(owner.server.rearm_return.call_count, 2)


if __name__ == "__main__":
    unittest.main()
