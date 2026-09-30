"""Display names for ignored_inputs entries, and the recorder's pure decision behind one."""

import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import ignored
import ignored_titles
import media_keys


class EntryTitleTests(unittest.TestCase):
    def test_special_key_uses_its_own_title(self):
        self.assertEqual(ignored_titles.entry_title(ignored.key(0x3D)), "Right Option")

    def test_printable_key_falls_back_to_its_upper_cased_character(self):
        self.assertEqual(ignored_titles.entry_title(ignored.key(0x00)), "A")

    def test_unrecognised_key_code_names_itself(self):
        self.assertEqual(ignored_titles.entry_title(ignored.key(9999)), "Key 9999")

    def test_named_buttons(self):
        for name, title in (
            ("right", "Right button"),
            ("middle", "Middle button"),
            ("back", "Back button"),
            ("forward", "Forward button"),
        ):
            self.assertEqual(ignored_titles.entry_title(ignored.button(name)), title)

    def test_numbered_button(self):
        self.assertEqual(ignored_titles.entry_title(ignored.button("6")), "Button 6")

    def test_media_keys(self):
        for name, title in (
            ("volume_up", "Volume Up"),
            ("volume_down", "Volume Down"),
            ("volume_mute", "Mute"),
            ("media_play_pause", "Play/Pause"),
            ("media_next", "Next Track"),
            ("media_prev", "Previous Track"),
        ):
            self.assertEqual(ignored_titles.entry_title(ignored.media(name)), title)


class RecordedEntryTests(unittest.TestCase):
    TRIGGER = 0x3D  # alt_r

    def test_a_key_becomes_an_entry(self):
        self.assertEqual(ignored_titles.recorded_entry("key", 0x00, self.TRIGGER), ignored.key(0))

    def test_the_trigger_key_refuses(self):
        self.assertIsNone(ignored_titles.recorded_entry("key", self.TRIGGER, self.TRIGGER))

    def test_right_mouse_is_always_the_named_button(self):
        self.assertEqual(ignored_titles.recorded_entry("right", 0, self.TRIGGER), ignored.button("right"))

    def test_other_mouse_maps_the_named_buttons(self):
        self.assertEqual(ignored_titles.recorded_entry("other", 2, self.TRIGGER), ignored.button("middle"))
        self.assertEqual(ignored_titles.recorded_entry("other", 3, self.TRIGGER), ignored.button("back"))
        self.assertEqual(ignored_titles.recorded_entry("other", 4, self.TRIGGER), ignored.button("forward"))

    def test_other_mouse_numbers_a_button_the_wire_has_no_name_for(self):
        self.assertEqual(ignored_titles.recorded_entry("other", 5, self.TRIGGER), ignored.button("6"))

    def test_a_modifier_already_held_is_not_recorded_on_its_release(self):
        # Left Shift is keycode 0x38, device bit 0x2; Right Shift 0x3C, device bit 0x4.
        self.assertTrue(ignored_titles.is_modifier_release(0x38, 0x100))
        self.assertTrue(ignored_titles.is_modifier_release(0x38, 0x20000 | 0x4))
        self.assertFalse(ignored_titles.is_modifier_release(0x38, 0x20000 | 0x2))
        self.assertFalse(ignored_titles.is_modifier_release(0x38, 0x20000))

    def test_caps_lock_and_ordinary_keys_are_always_presses(self):
        self.assertFalse(ignored_titles.is_modifier_release(0x39, 0))
        self.assertFalse(ignored_titles.is_modifier_release(0x00, 0))

    def _media_data1(self, nx_key, down):
        state = 0x0A if down else 0x0B
        return (nx_key << 16) | (state << 8)

    def test_a_media_key_down_press_becomes_an_entry(self):
        data1 = self._media_data1(0, down=True)  # volume_up
        self.assertEqual(ignored_titles.recorded_entry("media", data1, self.TRIGGER), ignored.media("volume_up"))

    def test_a_media_key_release_keeps_listening(self):
        data1 = self._media_data1(0, down=False)
        self.assertIsNone(ignored_titles.recorded_entry("media", data1, self.TRIGGER))

    def test_a_media_key_beamer_does_not_track_keeps_listening(self):
        data1 = self._media_data1(99, down=True)
        self.assertIsNone(ignored_titles.recorded_entry("media", data1, self.TRIGGER))
        self.assertIsNone(media_keys.decode(media_keys.NX_SUBTYPE_AUX_CONTROL_BUTTONS, data1))


if __name__ == "__main__":
    unittest.main()
