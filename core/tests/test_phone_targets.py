"""A sender-only peer cannot acquire desktop crossing controls."""
import unittest
from core import protocol, receiver, ways


class PhoneTargetsTests(unittest.TestCase):
    def setUp(self):
        self.ident = protocol.id_text(bytes([1]) * 16)
        self.phone = {"id": self.ident, "token": "synthetic", "port": 0, "send": True, "side": "right", "jump_key": "ctrl+shift+2"}
        self.settings = {"machine_id": protocol.id_text(bytes([2]) * 16), "peers": [self.phone], "zones": []}

    def test_phone_has_no_jump_key_even_if_stale_settings_hold_one(self):
        self.assertIsNone(ways.jump_key(self.phone))

    def test_phone_is_never_a_recorded_jump_target(self):
        self.assertIsNone(ways.jump_peer([self.phone], "ctrl+shift+2"))

    def test_phone_cannot_be_given_a_shared_edge(self):
        with self.assertRaises(ValueError):
            ways.share(self.settings, "right", {part: self.ident for part in ways.PARTS}, kinds=["part"])
        self.assertEqual(self.settings["zones"], [])

    def test_phone_does_not_change_a_lone_desktops_default_side(self):
        desktop = {"id": self.settings["machine_id"], "token": "desktop", "port": 24820, "side": ""}
        for peers in ([self.phone, desktop], [desktop, self.phone]):
            desktop["side"] = ""
            self.settings["peers"] = peers
            self.assertTrue(ways.default_side(self.settings))
            self.assertEqual(desktop["side"], "right")

    def test_phone_cannot_be_given_jump_key(self):
        with self.assertRaises(ValueError):
            ways.check_jump_key(self.settings, self.ident, "ctrl+shift+2", "alt_r")

    def test_phone_cannot_be_given_side_or_zones(self):
        with self.assertRaises(ValueError):
            ways.edit(self.settings, self.ident, side="right", methods=["edge"], parts=["start"],
                      corner="top_right", kinds=["edge"], corner_edge=lambda c, s: s, now=100)
        self.assertEqual(self.settings["zones"], [])

    def test_phone_arrangement_is_ignored(self):
        self.assertEqual(ways.arrangement(self.settings, self.ident, "left", 100,
                                         self.ident, "this machine"), (False, []))

    def test_no_kind_of_zone_can_lead_to_phone(self):
        zones = [{"peer": self.ident, "kind": "edge"},
                 {"peer": self.ident, "kind": "corner", "corner": "top_right", "edge": "right"},
                 {"peer": self.ident, "kind": "part", "parts": ["start"]},
                 {"peer": self.ident, "kind": "notch"}]
        self.assertEqual(receiver.zone_models(zones, [self.phone], {protocol.read_id(self.ident)},
                                              120, notch_span=(0.4, 0.6)), [])
