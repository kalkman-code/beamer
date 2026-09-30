import json
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import return_edge
from core.receiver import ReceiverServer


class ArmReturnResistanceTest(unittest.TestCase):
    """json.loads accepts the bare Infinity and NaN tokens, so an authenticated
    peer can put either in `resistance_px`; int() of them raises, and nothing
    above _arm_return caught it, so the session thread died unlogged. A bad
    number now gets the default and the session carries on."""

    def _armed_with(self, literal):
        server = ReceiverServer(status_callback=lambda state, text: None)
        server._arm_return(json.loads('{"target": "windows", "return_edge": "left", "resistance_px": %s}' % literal))
        return server._return_edge

    def test_infinite_resistance_falls_back_to_the_default(self):
        model = self._armed_with("Infinity")
        self.assertEqual(model.edge, "left")
        self.assertEqual(model.resistance_px, return_edge.DEFAULT_RESISTANCE_PX)

    def test_nan_resistance_falls_back_to_the_default(self):
        model = self._armed_with("NaN")
        self.assertEqual(model.resistance_px, return_edge.DEFAULT_RESISTANCE_PX)

    def test_a_real_resistance_is_kept(self):
        self.assertEqual(self._armed_with("40").resistance_px, 40)


if __name__ == "__main__":
    unittest.main()
