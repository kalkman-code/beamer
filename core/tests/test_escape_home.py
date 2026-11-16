import unittest

from core.escape_home import EscapeTaps


class EscapeTapsTests(unittest.TestCase):
    def test_three_quick_taps_fire_on_the_third_only(self):
        taps = EscapeTaps()
        self.assertFalse(taps.press(0.0))
        self.assertFalse(taps.press(0.3))
        self.assertTrue(taps.press(0.6))

    def test_a_slow_tap_starts_the_run_again(self):
        taps = EscapeTaps()
        taps.press(0.0)
        taps.press(0.3)
        self.assertFalse(taps.press(0.9))
        self.assertFalse(taps.press(1.2))
        self.assertTrue(taps.press(1.5))

    def test_reset_and_firing_start_from_zero(self):
        taps = EscapeTaps()
        taps.press(0.0)
        taps.press(0.1)
        taps.reset()
        self.assertFalse(taps.press(0.2))
        taps.press(0.3)
        self.assertTrue(taps.press(0.4))
        self.assertFalse(taps.press(0.5))
        self.assertFalse(taps.press(0.6))
        self.assertTrue(taps.press(0.7))
