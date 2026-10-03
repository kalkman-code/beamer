"""WIRE.md section 7's key table: which wire name each physical key sends to each kind of peer."""

import unittest

from core import keytable

MODS = ("ctrl", "ctrl_r", "cmd", "cmd_r", "alt", "alt_r")
MAC = ("macos", "ios")
PC = ("windows", "linux", "android", "plan9")

# Section 7, row by row: (physical key, same family, Semantic, Positional).
MAC_ROWS = [
    ("ctrl", "ctrl", "cmd", "ctrl"),
    ("ctrl_r", "ctrl_r", "cmd", "ctrl"),
    ("cmd", "cmd", "ctrl", "cmd"),
    ("cmd_r", "cmd_r", "ctrl", "cmd"),
    ("alt", "alt", "alt", "alt"),
    ("alt_r", "alt_r", "alt", "alt"),
]
PC_ROWS = [
    ("ctrl", "ctrl", "cmd", "ctrl"),
    ("ctrl_r", "ctrl_r", "cmd_r", "ctrl_r"),
    ("cmd", "cmd", "ctrl", "cmd"),
    ("cmd_r", "cmd_r", "ctrl_r", "cmd_r"),
    ("alt", "alt", "alt", "alt"),
    ("alt_r", "alt_r", "alt_r", "alt_r"),
]
PC_MAC_LAYOUT_ROWS = [
    ("ctrl", "ctrl"), ("ctrl_r", "ctrl_r"),
    ("cmd", "alt"), ("cmd_r", "alt_r"),
    ("alt", "cmd"), ("alt_r", "cmd_r"),
]

# 1.4.x as it stands: the Mac's two key_map styles, and Windows' capture names (Ctrl is "cmd", the
# Windows key is "ctrl") with POSITIONAL_SWAP on top.
DEFAULT_KEY_MAP = {"alt": "alt", "alt_r": "alt", "ctrl": "cmd", "ctrl_r": "cmd", "cmd": "ctrl", "cmd_r": "ctrl"}
LEGACY_POSITIONAL_KEY_MAP = {"alt": "alt", "alt_r": "alt", "ctrl": "ctrl", "ctrl_r": "ctrl", "cmd": "cmd", "cmd_r": "cmd"}
POSITIONAL_SWAP = {"cmd": "ctrl", "cmd_r": "ctrl_r", "ctrl": "cmd", "ctrl_r": "cmd_r"}
WINDOWS_CAPTURE = {"ctrl": "cmd", "ctrl_r": "cmd_r", "cmd": "ctrl", "cmd_r": "ctrl_r", "alt": "alt", "alt_r": "alt_r"}


class TableTests(unittest.TestCase):
    def test_every_row_for_a_mac_sender(self):
        for own in MAC:
            for physical, same, semantic, positional in MAC_ROWS:
                for peer in MAC:
                    for style in ("semantic", "positional"):
                        self.assertEqual(keytable.wire_name(physical, own, peer, style), same, (own, peer, physical))
                for peer in PC:
                    self.assertEqual(keytable.wire_name(physical, own, peer, "semantic"), semantic, (own, peer, physical))
                    self.assertEqual(keytable.wire_name(physical, own, peer, "positional"), positional, (own, peer, physical))

    def test_every_row_for_a_pc_sender(self):
        for own in PC:
            for physical, same, semantic, positional in PC_ROWS:
                for peer in PC:
                    for style in ("semantic", "positional"):
                        self.assertEqual(keytable.wire_name(physical, own, peer, style), same, (own, peer, physical))
                for peer in MAC:
                    self.assertEqual(keytable.wire_name(physical, own, peer, "semantic"), semantic, (own, peer, physical))
                    self.assertEqual(keytable.wire_name(physical, own, peer, "positional"), positional, (own, peer, physical))

    def test_mac_layout_maps_each_pc_modifier_side_to_its_mac_key(self):
        for physical, expected in PC_MAC_LAYOUT_ROWS:
            self.assertEqual(keytable.wire_name(physical, "windows", "macos", "mac_layout"), expected)

    def test_mac_layout_leaves_non_modifier_keys_unchanged(self):
        for name in ("shift", "shift_r", "caps_lock", "a", "enter", "f13"):
            self.assertEqual(keytable.wire_name(name, "windows", "macos", "mac_layout"), name)

    def test_an_unknown_platform_is_a_pc_on_both_sides(self):
        self.assertEqual(keytable.wire_name("cmd", "haiku", "macos", "semantic"), "ctrl")
        self.assertEqual(keytable.wire_name("cmd", "macos", "haiku", "semantic"), "ctrl")
        self.assertEqual(keytable.wire_name("cmd", "haiku", "beos", "semantic"), "cmd")

    def test_keys_that_are_not_those_six_go_out_as_themselves(self):
        for name in ("shift", "shift_r", "caps_lock", "a", "enter", "f13", "volume_up", "not_a_key"):
            for own, peer in (("macos", "windows"), ("windows", "macos"), ("linux", "linux"), ("macos", "ios")):
                for style in ("semantic", "positional"):
                    self.assertEqual(keytable.wire_name(name, own, peer, style), name)


class OldStylesTests(unittest.TestCase):
    def test_a_mac_with_a_saved_key_map_sends_what_1_4_x_sent(self):
        for saved, style in ((DEFAULT_KEY_MAP, "semantic"), (LEGACY_POSITIONAL_KEY_MAP, "positional")):
            for name in MODS + ("shift", "caps_lock", "f1"):
                expected = saved.get(name, name)
                self.assertEqual(keytable.wire_name(name, "macos", "windows", style), expected, (style, name))
                self.assertEqual(keytable.wire_name(name, "macos", "windows", dict(saved)), expected, (style, name))

    def test_windows_with_a_saved_modifier_style_sends_what_1_4_x_sent(self):
        for physical in MODS:
            capture = WINDOWS_CAPTURE[physical]
            self.assertEqual(keytable.wire_name(physical, "windows", "macos", "semantic"), capture)
            self.assertEqual(keytable.wire_name(physical, "windows", "macos", "positional"), POSITIONAL_SWAP.get(capture, capture))

    def test_a_hand_written_map_applies_across_families_only(self):
        mine = {"alt": "cmd", "cmd": "alt"}
        self.assertEqual(keytable.wire_name("alt", "macos", "windows", mine), "cmd")
        self.assertEqual(keytable.wire_name("ctrl", "macos", "windows", mine), "ctrl")
        self.assertEqual(keytable.wire_name("alt", "macos", "macos", mine), "alt")
        self.assertEqual(keytable.wire_name("alt", "windows", "windows", mine), "alt")

    def test_a_style_nobody_offers_is_refused(self):
        with self.assertRaises(ValueError):
            keytable.wire_name("ctrl", "macos", "windows", "sideways")


class EvdevTests(unittest.TestCase):
    def test_the_modifier_codes_the_spec_names(self):
        self.assertEqual(keytable.evdev_physical(29), "ctrl")
        self.assertEqual(keytable.evdev_physical(97), "ctrl_r")
        self.assertEqual(keytable.evdev_physical(125), "cmd")
        self.assertEqual(keytable.evdev_physical(126), "cmd_r")
        self.assertEqual(keytable.evdev_physical(56), "alt")
        self.assertEqual(keytable.evdev_physical(100), "alt_r")
        self.assertEqual(keytable.evdev_physical(42), "shift")
        self.assertEqual(keytable.evdev_physical(54), "shift_r")
        self.assertEqual(keytable.evdev_physical(58), "caps_lock")

    def test_a_linux_sender_goes_through_the_same_table(self):
        cases = {29: "cmd", 97: "cmd_r", 125: "ctrl", 126: "ctrl_r", 56: "alt", 100: "alt_r"}
        for code, wire in cases.items():
            self.assertEqual(keytable.wire_name(keytable.evdev_physical(code), "linux", "macos", "semantic"), wire)
        self.assertEqual(keytable.wire_name(keytable.evdev_physical(125), "linux", "macos", "positional"), "cmd")
        self.assertEqual(keytable.wire_name(keytable.evdev_physical(125), "linux", "windows", "semantic"), "cmd")

    def test_named_keys_and_the_numpad_enter(self):
        self.assertEqual(keytable.evdev_physical(1), "esc")
        self.assertEqual(keytable.evdev_physical(28), "enter")
        self.assertEqual(keytable.evdev_physical(96), "enter")
        self.assertEqual(keytable.evdev_physical(59), "f1")
        self.assertEqual(keytable.evdev_physical(88), "f12")
        self.assertEqual(keytable.evdev_physical(183), "f13")
        self.assertEqual(keytable.evdev_physical(194), "f24")
        self.assertEqual(keytable.evdev_physical(111), "delete")
        self.assertEqual(keytable.evdev_physical(113), "volume_mute")

    def test_every_name_it_returns_is_in_the_spec_and_no_name_twice_but_enter(self):
        names = list(keytable.EVDEV_NAMES.values())
        self.assertLessEqual(set(names), keytable.SPEC_NAMES)
        self.assertEqual([n for n in set(names) if names.count(n) > 1], ["enter"])

    def test_a_character_key_or_an_unknown_code_is_none(self):
        self.assertIsNone(keytable.evdev_physical(30))
        self.assertIsNone(keytable.evdev_physical(99999))

    def test_the_stand_in_code_is_still_right_ctrl(self):
        self.assertEqual(keytable.evdev_physical(97), "ctrl_r")


if __name__ == "__main__":
    unittest.main()
