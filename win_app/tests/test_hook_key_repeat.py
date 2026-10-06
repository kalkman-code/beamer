"""Run the app's actual hook decision with a real Trigger and LinkSender, without a window."""

import time
import unittest
import os
import sys
from typing import Optional
from types import SimpleNamespace

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import capture_win
from core import protocol
from capture_win import Trigger
from links_rig import B, Rig, make_config
from core.tests.app_methods import load_methods

Application = load_methods("win_app/kvm_bridge_win.py", "WindowsApplication", ["_on_hook_key"], globals())


def dispatch(trigger, link_sender, name, down, vk=None, us=None):
    return Application._on_hook_key(SimpleNamespace(_trigger=trigger, sender=link_sender), name, down, vk, us)


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

    def test_shortcut_off_passes_the_key_without_switching_in_each_input_state(self):
        for style in ("double_tap", "hold"):
            for state in ("local", "receiving", "redirected"):
                with self.subTest(style=style, state=state):
                    rig = Rig(crossing_methods=["edge"])
                    trigger = Trigger("alt_r", style, 300)
                    if state == "redirected":
                        rig.sender.set_redirecting(True)
                    rig.driven = state == "receiving"
                    rig.flush()
                    rig.links.sent.clear()
                    for down in (True, False) * 2:
                        self.assertEqual(dispatch(trigger, rig.sender, "alt_r", down), state == "redirected")
                    self.assertEqual(rig.sender.redirecting, state == "redirected")
                    self.assertEqual(rig.sent_home, [])
                    self.assertEqual(rig.sent(B, "focus"), [])
                    keys = [message for message in rig.sent(B) if message["type"] in (protocol.MSG_KEYDOWN, protocol.MSG_KEYUP)]
                    expected = [{"type": kind, "data": {"key": "alt_r"}}
                                for kind in (protocol.MSG_KEYDOWN, protocol.MSG_KEYUP) * 2]
                    self.assertEqual(keys, expected if state == "redirected" else [])


if __name__ == "__main__":
    unittest.main()
