"""kvm_bridge_win.py:2351 (`_on_hook_key`) cannot be imported here (it
needs PySide6, Windows-only), so this drives its exact dispatch by hand
against the two pure-Python pieces it calls: `capture_win.Trigger.feed` and
`sender.LinkSender.on_key`. The dispatch replicated below is copied verbatim
from `_on_hook_key`'s body."""

import time
import unittest
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import capture_win
from core import protocol
from capture_win import Trigger
from links_rig import B, Rig, make_config


def dispatch(trigger, link_sender, name, down, vk=None, us=None):
    """`WindowsApplication._on_hook_key`, verbatim."""
    action = trigger.feed(name, down, time.monotonic())
    if action is not None:
        if not link_sender.shortcut_armed:
            return True
        if action == capture_win.TOGGLE:
            link_sender.toggle()
        else:
            link_sender.set_redirecting(action == capture_win.REDIRECT)
        return True
    if trigger.claims(name, down, link_sender.redirecting):
        return True
    return link_sender.on_key(name, down, vk, us)


class HeldTriggerRepeatTests(unittest.TestCase):
    def setUp(self):
        self.trigger = Trigger("alt_r", "hold", 300)
        self.rig = Rig()
        self.link_sender = self.rig.sender

    def keys(self, kind):
        return self.rig.sent(B, kind)

    def test_held_trigger_key_repeat_is_not_forwarded_to_the_peer(self):
        # Physical press: the trigger claims it, redirecting turns on.
        self.assertTrue(dispatch(self.trigger, self.link_sender, "alt_r", True))
        self.assertTrue(self.link_sender.redirecting)

        # Windows autorepeats WM_KEYDOWN for as long as the key stays down.
        # The trigger's own comment says a repeat "never reaches an app";
        # it must not be forwarded to the peer as a live keystroke. Any key
        # at all is checked, not one name: the key table renames keys per peer.
        dispatch(self.trigger, self.link_sender, "alt_r", True)

        self.assertEqual(
            self.keys(protocol.MSG_KEYDOWN),
            [],
            "an autorepeat of the held trigger key was forwarded to the peer "
            "as MSG_KEYDOWN",
        )

    def test_a_lone_tap_at_home_under_double_tap_is_an_ordinary_key(self):
        trigger = Trigger("cmd_r", "double_tap", 300)
        self.assertFalse(dispatch(trigger, self.link_sender, "cmd_r", True))
        self.assertFalse(dispatch(trigger, self.link_sender, "cmd_r", False))

    def test_the_trigger_is_swallowed_while_input_is_away_under_double_tap(self):
        trigger = Trigger("cmd_r", "double_tap", 300)
        self.link_sender.set_redirecting(True)
        self.assertTrue(dispatch(trigger, self.link_sender, "cmd_r", True))
        self.assertTrue(dispatch(trigger, self.link_sender, "cmd_r", False))
        self.assertEqual(self.keys(protocol.MSG_KEYDOWN) + self.keys(protocol.MSG_KEYUP), [])

    def test_the_second_taps_release_is_swallowed_after_coming_home(self):
        # Two taps while away toggle input home on the second down; that
        # tap's release must not reach Windows as a key-up with no key-down.
        trigger = Trigger("cmd_r", "double_tap", 300)
        self.link_sender.set_redirecting(True)
        self.assertTrue(dispatch(trigger, self.link_sender, "cmd_r", True))
        self.assertTrue(dispatch(trigger, self.link_sender, "cmd_r", False))
        self.assertTrue(dispatch(trigger, self.link_sender, "cmd_r", True))
        self.assertFalse(self.link_sender.redirecting)
        self.assertTrue(dispatch(trigger, self.link_sender, "cmd_r", False))
        # A later lone tap at home is an ordinary key again.
        self.assertFalse(dispatch(trigger, self.link_sender, "cmd_r", True))
        self.assertFalse(dispatch(trigger, self.link_sender, "cmd_r", False))

    def test_physical_release_never_reaches_on_key_so_no_keyup_is_sent(self):
        dispatch(self.trigger, self.link_sender, "alt_r", True)
        dispatch(self.trigger, self.link_sender, "alt_r", True)  # the repeat
        dispatch(self.trigger, self.link_sender, "alt_r", False)  # real release

        # The release is swallowed before sender.on_key ever sees it, so a
        # key-down that did leak (proven above) is never balanced by a
        # key-up -- the peer is left with a phantom held key.
        self.assertEqual(self.keys(protocol.MSG_KEYUP), [])

    def test_with_the_shortcut_off_the_trigger_is_swallowed_and_moves_nothing(self):
        self.link_sender.update_config(make_config(crossing_methods=["edge"]))
        self.assertTrue(dispatch(self.trigger, self.link_sender, "alt_r", True))
        self.assertFalse(self.link_sender.redirecting)
        self.assertEqual(self.rig.sent(B, "focus"), [])


if __name__ == "__main__":
    unittest.main()
