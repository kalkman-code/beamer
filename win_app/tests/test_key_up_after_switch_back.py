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


if __name__ == "__main__":
    unittest.main()
