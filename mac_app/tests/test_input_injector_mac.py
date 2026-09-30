"""The planning half of the Mac injector: which key code a wire name lands on,
when a character is typed as text instead, where a move is clamped to, and
when a click counts as a double. No Quartz — everything here is arithmetic and
table lookup, which is exactly the part that can be wrong without crashing."""

import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import input_injector_mac as injector
from core.return_edge import Rect


class KeyPlanTests(unittest.TestCase):
    def plan(self, name, down=True, mods=None):
        return injector.plan_key_event(name, down, set() if mods is None else mods)

    def test_a_named_key_lands_on_its_key_code(self):
        self.assertEqual(self.plan("enter"), (0x24, None))
        self.assertEqual(self.plan("left"), (0x7B, None))

    def test_ctrl_on_the_pc_arrives_as_command(self):
        # The Windows capture has already made the swap, so the injector's job
        # is only to place "cmd" on the Command key -- but if that table ever
        # moved, every shortcut would land on the wrong modifier silently.
        self.assertEqual(self.plan("cmd"), (0x37, None))
        self.assertEqual(self.plan("ctrl"), (0x3B, None))
        self.assertEqual(self.plan("alt"), (0x3A, None))

    def test_a_letter_uses_the_key_its_layout_produces(self):
        self.assertEqual(self.plan("c"), (0x08, None))
        self.assertEqual(self.plan("a"), (0x00, None))

    def test_a_shortcut_keeps_the_key_code_rather_than_typing_text(self):
        # A Unicode event carries no key code, so Cmd+C sent as text copies
        # nothing at all.
        mods = {"cmd"}
        self.assertEqual(injector.plan_key_event("c", True, mods), (0x08, None))

    def test_a_character_off_the_table_is_typed_as_text(self):
        self.assertEqual(self.plan("é"), (0, "é"))

    def test_modifiers_are_tracked_in_both_directions(self):
        mods = set()
        injector.plan_key_event("shift", True, mods)
        self.assertEqual(mods, {"shift"})
        injector.plan_key_event("shift", False, mods)
        self.assertEqual(mods, set())

    def test_an_unknown_name_sends_nothing(self):
        self.assertIsNone(self.plan("nonsense_key"))


class ClampTests(unittest.TestCase):
    SCREENS = [Rect(0, 0, 1000, 1000), Rect(1000, 200, 800, 600)]

    def test_a_point_on_a_display_is_left_alone(self):
        self.assertEqual(injector.clamp_to_displays(500, 500, self.SCREENS), (500, 500))

    def test_a_point_past_the_edge_comes_back_to_it(self):
        self.assertEqual(injector.clamp_to_displays(-40, 500, self.SCREENS), (0, 500))

    def test_a_point_in_the_gap_beside_a_shorter_display_lands_on_one(self):
        # Level with the second screen's dead space above it: the pointer must
        # end up on a real display, not hovering in the bounding box.
        x, y = injector.clamp_to_displays(1400, 50, self.SCREENS)
        self.assertTrue(any(r.x <= x <= r.right and r.y <= y <= r.bottom for r in self.SCREENS))


class ClickStateTests(unittest.TestCase):
    def test_two_quick_clicks_in_one_place_are_a_double(self):
        last = {}
        self.assertEqual(injector.plan_click_state("left", 100, 100, 1.0, last), 1)
        self.assertEqual(injector.plan_click_state("left", 101, 100, 1.2, last), 2)
        self.assertEqual(injector.plan_click_state("left", 101, 100, 1.4, last), 3)

    def test_a_slow_second_click_starts_again(self):
        last = {}
        injector.plan_click_state("left", 100, 100, 1.0, last)
        self.assertEqual(injector.plan_click_state("left", 100, 100, 3.0, last), 1)

    def test_a_second_click_somewhere_else_starts_again(self):
        last = {}
        injector.plan_click_state("left", 100, 100, 1.0, last)
        self.assertEqual(injector.plan_click_state("left", 400, 100, 1.1, last), 1)


if __name__ == "__main__":
    unittest.main()
