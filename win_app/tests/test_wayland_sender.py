"""The sender on a desktop where input leaves only across an edge (Wayland's InputCapture): the
shortcut and the tray cannot send input away while the desktop has not captured it, an edge push
still can, and the capture reads the zone models it puts barriers on."""

import os
import sys
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import protocol, return_edge
from links_rig import B, Rig


class WaylandSenderTest(unittest.TestCase):
    def test_the_shortcut_cannot_send_input_away_while_the_desktop_holds_none(self):
        rig = Rig()
        rig.sender.input_held = lambda: False
        self.assertFalse(rig.sender.set_redirecting(True))
        self.assertFalse(rig.sender.redirecting)
        self.assertEqual(rig.sent(B, protocol.MSG_FOCUS), [])
        self.assertTrue(any("edge" in alert for alert in rig.alerts), rig.alerts)

    def test_an_edge_push_still_sends_it_while_the_desktop_holds_input(self):
        rig = Rig()
        held = [False]
        rig.sender.input_held = lambda: held[0]
        held[0] = True  # the barrier captured the pointer
        rig.push(times=6)
        self.assertTrue(rig.sender.redirecting)
        self.assertEqual(len(rig.sent(B, protocol.MSG_FOCUS)), 1)

    def test_the_shortcut_works_as_before_where_input_can_always_be_held(self):
        rig = Rig()
        self.assertTrue(rig.sender.set_redirecting(True))
        self.assertTrue(rig.sender.redirecting)

    def test_the_zone_models_are_read_for_the_barriers_and_are_empty_while_crossing_is_paused(self):
        rig = Rig()
        models = rig.sender.zone_models()
        self.assertEqual(len(models), 1)
        self.assertIsInstance(models[0], return_edge.ReturnEdge)
        self.assertEqual(models[0].edge, "left")
        rig.sender.crossing_paused = True
        self.assertEqual(rig.sender.zone_models(), [])


if __name__ == "__main__":
    unittest.main()
