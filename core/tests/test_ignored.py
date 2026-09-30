"""The ignored-inputs gate both apps share. Byte-identical in mac_app and win_app."""

import unittest

from core import ignored


class GateTests(unittest.TestCase):
    def test_a_listed_press_and_its_release_stay_here(self):
        gate = ignored.Gate([ignored.button("back")])
        self.assertTrue(gate.keeps("button:back", True))
        self.assertTrue(gate.keeps("button:back", False))

    def test_anything_else_goes_across(self):
        gate = ignored.Gate([ignored.button("back")])
        self.assertFalse(gate.keeps("key:12", True))
        self.assertFalse(gate.keeps("key:12", False))

    def test_a_release_follows_its_press_across_when_the_list_changes_mid_hold(self):
        gate = ignored.Gate()
        self.assertFalse(gate.keeps("key:122", True))
        gate.configure([ignored.key(122)])
        self.assertFalse(gate.keeps("key:122", True), "an autorepeat of a key already across stays across")
        self.assertFalse(gate.keeps("key:122", False), "its release must reach the far side")
        self.assertTrue(gate.keeps("key:122", True), "the next press is kept here")

    def test_a_press_kept_here_keeps_its_release_here_when_taken_off_the_list(self):
        gate = ignored.Gate([ignored.key(122)])
        self.assertTrue(gate.keeps("key:122", True))
        gate.configure([])
        self.assertTrue(gate.keeps("key:122", False), "the release must reach this machine, which has the press")
        self.assertFalse(gate.keeps("key:122", True), "the next press goes across")

    def test_a_release_outside_the_list_with_no_press_seen_goes_across(self):
        gate = ignored.Gate()
        self.assertFalse(gate.keeps("key:5", False))

    def test_reset_forgets_what_went_across(self):
        gate = ignored.Gate()
        gate.keeps("key:5", True)
        gate.configure(["key:5"])
        gate.reset()
        self.assertTrue(gate.keeps("key:5", False))

    def test_validate_takes_every_kind_of_entry(self):
        entries = [ignored.key(0), ignored.key(0xA3), ignored.button("forward"), ignored.button("6"), ignored.media("volume_up")]
        self.assertEqual(ignored.validate(entries), entries)

    def test_validate_refuses_what_it_cannot_read(self):
        for bad in ("f13", ["key:"], ["key:abc"], ["mouse:back"], [12], ["key:1"] * (ignored.MAX_ENTRIES + 1)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ignored.validate(bad)


if __name__ == "__main__":
    unittest.main()
