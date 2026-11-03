"""The planning half of the Mac injector: which key code a wire name lands on,
when a character is typed as text instead, where a move is clamped to, and
when a click counts as a double. No Quartz — everything here is arithmetic and
table lookup, which is exactly the part that can be wrong without crashing."""

import unittest
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import input_injector_mac as injector
from key_codes import KEY_TITLES, SPECIAL_KEY_NAMES
from core.return_edge import Rect


class KeyPlanTests(unittest.TestCase):
    def test_auxiliary_key_numbers_cover_volume_and_brightness(self):
        self.assertEqual(injector.MEDIA_NX_KEYS["volume_up"], 0)
        self.assertEqual(injector.MEDIA_NX_KEYS["volume_down"], 1)
        self.assertEqual(injector.MEDIA_NX_KEYS["volume_mute"], 7)
        self.assertEqual(injector.MEDIA_NX_KEYS["brightness_up"], 2)
        self.assertEqual(injector.MEDIA_NX_KEYS["brightness_down"], 3)

    def plan(self, name, down=True, mods=None):
        return injector.plan_key_event(name, down, set() if mods is None else mods)

    def test_a_named_key_lands_on_its_key_code(self):
        self.assertEqual(self.plan("enter"), (0x24, None))
        self.assertEqual(self.plan("left"), (0x7B, None))

    def test_input_method_keys_land_on_jis_codes_without_modifier_flags(self):
        for name, code in (("hangul", 0x68), ("hanja", 0x66)):
            with self.subTest(name=name):
                self.assertEqual(self.plan(name), (code, None))
                self.assertEqual(injector.key_flags(code), 0)
        self.assertEqual((SPECIAL_KEY_NAMES[0x68], SPECIAL_KEY_NAMES[0x66]), ("hangul", "hanja"))
        self.assertEqual((KEY_TITLES["hangul"], KEY_TITLES["hanja"]), ("Han/Yeong", "Hanja"))
        self.assertFalse({"hangul", "hanja"} & set(injector.MODIFIER_FLAG_NAMES))

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

    def test_caps_capitals_keep_chord_keys_and_plain_text_in_each_script(self):
        for lower in ("c", "ф", "ω"):
            with mock.patch.object(injector.keyboard_layout, "code_for", side_effect=lambda ch: 0x08 if ch == lower else None):
                upper = lower.upper()
                self.assertEqual(self.plan(upper), (0, upper))
                for modifier in sorted(injector.CHORD_MODIFIERS):
                    for shifted in (False, True):
                        with self.subTest(character=upper, modifier=modifier, shifted=shifted):
                            mods = {modifier} | ({"shift"} if shifted else set())
                            held = {}
                            self.assertEqual(injector.plan_key_event(upper, True, mods, held), (0x08, None))
                            mods.clear()
                            self.assertEqual(injector.plan_key_event(upper, False, mods, held), (0x08, None))
                            self.assertEqual(held, {})

    def test_a_character_off_the_table_is_typed_as_text(self):
        self.assertEqual(self.plan("é"), (0, "é"))

    def test_modifiers_are_tracked_in_both_directions(self):
        mods = set()
        injector.plan_key_event("shift", True, mods)
        self.assertEqual(mods, {"shift"})
        injector.plan_key_event("shift", False, mods)
        self.assertEqual(mods, set())

    def test_an_unknown_name_sends_nothing(self):
        old_names = {name: code for name, code in injector.KEY_NAME_TO_CODE.items() if name not in ("hangul", "hanja")}
        with mock.patch.object(injector, "KEY_NAME_TO_CODE", old_names):
            for name in ("hangul", "hanja"):
                with self.subTest(name=name), self.assertLogs(injector.LOGGER, "WARNING"):
                    self.assertIsNone(self.plan(name))


class TextChunkTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(injector.text_chunks("héllo"), ["héllo"])

    def test_long_text_is_cut_inside_the_events_limit_of_utf_16_units(self):
        chunks = injector.text_chunks("a" * 40)
        self.assertEqual("".join(chunks), "a" * 40)
        self.assertTrue(all(len(chunk) <= injector.TEXT_CHUNK_UNITS for chunk in chunks))

    def test_a_character_outside_the_bmp_is_never_split(self):
        text = "x" * (injector.TEXT_CHUNK_UNITS - 1) + "\U0001F600" + "y"
        chunks = injector.text_chunks(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= injector.TEXT_CHUNK_UNITS for chunk in chunks))
        self.assertIn("\U0001F600", "".join(chunks))
        self.assertFalse(any(chunk.endswith("\ud83d") for chunk in chunks))

    def test_nothing_is_no_chunks(self):
        self.assertEqual(injector.text_chunks(""), [])


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
