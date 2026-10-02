"""Reproduces win-sender-3: a key held down while input is on a peer, released after it has come
home, must still reach the peer as a key-up, because on_key's gate reads where input is now rather
than whether the key-down was actually forwarded. The owner (core/owner.py) now answers for it: it
releases what it pressed on a peer when input leaves, and swallows the physical release."""

import os
import sys
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from links_rig import B, Rig
from core import protocol


class KeyUpAfterSwitchBackTest(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(cursor=(0, 500))
        self.sender = self.rig.sender
        self.sender.set_redirecting(True)
        self.rig.accept_take()

    def keys(self):
        return [m["type"] for m in self.rig.sent(B) if m["type"] in (protocol.MSG_KEYDOWN, protocol.MSG_KEYUP)]

    def test_key_released_after_switch_back_is_dropped(self):
        sent_down = self.sender.on_key("a", True)
        self.assertTrue(sent_down, "the key-down while redirecting should have been forwarded")

        self.sender.set_redirecting(False)

        sent_up = self.sender.on_key("a", False)

        self.assertEqual(
            self.keys(),
            [protocol.MSG_KEYDOWN, protocol.MSG_KEYUP],
            "a key-down forwarded to the peer owes it the matching key-up, "
            "even after redirecting has already been switched back off",
        )
        self.assertTrue(
            sent_up,
            "on_key must swallow the key-up locally, since Windows never saw the key go down",
        )

    def test_a_held_keys_autorepeat_at_home_is_swallowed_until_its_release(self):
        self.sender.on_key("a", True)
        self.sender.set_redirecting(False)
        self.assertTrue(self.sender.on_key("a", True), "an autorepeat of a key the peer holds")
        self.assertTrue(self.sender.on_key("a", False), "the release Windows never saw the down for")
        self.assertFalse(self.sender.on_key("a", True), "a fresh press is Windows' own again")

    def test_a_release_still_queued_when_input_comes_home_is_sent_once(self):
        self.sender.on_key("a", True)
        self.rig.flush()
        # The release is queued but not yet sent when input comes home: the owner must not add a
        # second one of its own.
        self.sender.on_key("a", False)
        self.sender.set_redirecting(False)
        self.assertEqual(self.keys(), [protocol.MSG_KEYDOWN, protocol.MSG_KEYUP])

    def test_the_jump_chord_for_the_current_machine_returns_input_home(self):
        entry = self.rig.settings.data["peers"][0]
        entry["jump_key"] = "ctrl+shift+2"
        self.sender.refresh()
        self.assertFalse(self.sender.handle_jump_key("ctrl", True))
        self.assertFalse(self.sender.handle_jump_key("shift", True))
        self.assertTrue(self.sender.handle_jump_key("2", True))
        self.assertFalse(self.sender.redirecting)
        self.assertTrue(self.sender.handle_jump_key("2", True))
        self.assertFalse(self.sender.redirecting)
        self.assertTrue(self.sender.handle_jump_key("2", False))

    def test_a_key_already_down_when_its_modifiers_arrive_is_not_a_jump(self):
        self.rig.settings.data["peers"][0]["jump_key"] = "ctrl+shift+2"
        self.sender.refresh()
        self.assertFalse(self.sender.handle_jump_key("2", True, 0x32))
        self.assertFalse(self.sender.handle_jump_key("ctrl", True))
        self.assertFalse(self.sender.handle_jump_key("shift", True))
        self.assertFalse(self.sender.handle_jump_key("2", True, 0x32))
        self.assertTrue(self.sender.redirecting)
        self.assertFalse(self.sender.handle_jump_key("2", False, 0x32))

    def test_a_swallowed_key_is_released_by_its_virtual_key_even_when_shift_changed_its_name(self):
        self.rig.settings.data["peers"][0]["jump_key"] = "ctrl+shift+@"
        self.sender.refresh()
        self.sender.handle_jump_key("ctrl", True)
        self.sender.handle_jump_key("shift", True)
        self.assertTrue(self.sender.handle_jump_key("@", True, 0x32))
        self.sender.handle_jump_key("shift", False)
        self.assertTrue(self.sender.handle_jump_key("2", False, 0x32))
        self.assertFalse(self.sender.handle_jump_key("@", True, 0x32))

    def test_nothing_jumps_while_a_jump_key_is_being_recorded(self):
        self.rig.settings.data["peers"][0]["jump_key"] = "ctrl+shift+2"
        self.sender.refresh()
        self.sender.jump_recording = True
        self.sender.handle_jump_key("ctrl", True)
        self.sender.handle_jump_key("shift", True)
        self.assertFalse(self.sender.handle_jump_key("2", True, 0x32))
        self.assertTrue(self.sender.redirecting)


if __name__ == "__main__":
    unittest.main()
