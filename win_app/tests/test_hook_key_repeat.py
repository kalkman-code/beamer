"""kvm_bridge_win.py:1056-1071 (`_on_hook_key`) cannot be imported here (it
needs PySide6, Windows-only), so this drives its exact dispatch by hand
against the two pure-Python pieces it calls: `capture_win.Trigger.feed` and
`sender.MacSender.on_key`. The dispatch replicated below is copied verbatim
from `_on_hook_key`'s body."""

import time
import unittest
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import capture_win
from core import protocol
import sender
from capture_win import Trigger
from fakes import FakeDesktop
from sender import MacSender


def dispatch(trigger, mac_sender, name, down):
    """`WindowsApplication._on_hook_key`, verbatim."""
    action = trigger.feed(name, down, time.monotonic())
    if action is not None:
        if action == capture_win.TOGGLE:
            mac_sender.toggle()
        else:
            mac_sender.set_redirecting(action == capture_win.REDIRECT)
        return True
    if trigger.claims(name, down, mac_sender.redirecting):
        return True
    return mac_sender.on_key(name, down)


class HeldTriggerRepeatTests(unittest.TestCase):
    def setUp(self):
        self.trigger = Trigger("alt_r", "hold", 300)
        self.mac_sender = MacSender(desktop=FakeDesktop([]))
        # Make the link look live without a real socket or connection
        # thread, exactly as `_link_live`/`connected` require.
        self.mac_sender._sock = object()
        self.mac_sender._connected_at = time.monotonic()

    def test_held_trigger_key_repeat_is_not_forwarded_to_mac(self):
        # Physical press: the trigger claims it, redirecting turns on.
        self.assertTrue(dispatch(self.trigger, self.mac_sender, "alt_r", True))
        self.assertTrue(self.mac_sender.redirecting)

        # Windows autorepeats WM_KEYDOWN for as long as the key stays down.
        # The trigger's own comment says a repeat "never reaches an app";
        # it must not be forwarded to the Mac as a live keystroke.
        dispatch(self.trigger, self.mac_sender, "alt_r", True)

        forwarded = [
            message
            for message in list(self.mac_sender._outbound.queue)
            if message.get("type") == protocol.MSG_KEYDOWN
            and message.get("data", {}).get("key") == "alt_r"
        ]
        self.assertEqual(
            forwarded,
            [],
            "an autorepeat of the held trigger key was forwarded to the Mac "
            "as MSG_KEYDOWN",
        )

    def test_a_lone_tap_at_home_under_double_tap_is_an_ordinary_key(self):
        trigger = Trigger("cmd_r", "double_tap", 300)
        self.assertFalse(dispatch(trigger, self.mac_sender, "cmd_r", True))
        self.assertFalse(dispatch(trigger, self.mac_sender, "cmd_r", False))

    def test_the_trigger_is_swallowed_while_input_is_away_under_double_tap(self):
        trigger = Trigger("cmd_r", "double_tap", 300)
        self.mac_sender.set_redirecting(True)
        self.assertTrue(dispatch(trigger, self.mac_sender, "cmd_r", True))
        self.assertTrue(dispatch(trigger, self.mac_sender, "cmd_r", False))
        forwarded = [m for m in list(self.mac_sender._outbound.queue) if m.get("data", {}).get("key") == "cmd_r"]
        self.assertEqual(forwarded, [])

    def test_the_second_taps_release_is_swallowed_after_coming_home(self):
        # Two taps while away toggle input home on the second down; that
        # tap's release must not reach Windows as a key-up with no key-down.
        trigger = Trigger("cmd_r", "double_tap", 300)
        self.mac_sender.set_redirecting(True)
        self.assertTrue(dispatch(trigger, self.mac_sender, "cmd_r", True))
        self.assertTrue(dispatch(trigger, self.mac_sender, "cmd_r", False))
        self.assertTrue(dispatch(trigger, self.mac_sender, "cmd_r", True))
        self.assertFalse(self.mac_sender.redirecting)
        self.assertTrue(dispatch(trigger, self.mac_sender, "cmd_r", False))
        # A later lone tap at home is an ordinary key again.
        self.assertFalse(dispatch(trigger, self.mac_sender, "cmd_r", True))
        self.assertFalse(dispatch(trigger, self.mac_sender, "cmd_r", False))

    def test_physical_release_never_reaches_on_key_so_no_keyup_is_sent(self):
        dispatch(self.trigger, self.mac_sender, "alt_r", True)
        dispatch(self.trigger, self.mac_sender, "alt_r", True)  # the repeat
        dispatch(self.trigger, self.mac_sender, "alt_r", False)  # real release

        keyups = [
            message
            for message in list(self.mac_sender._outbound.queue)
            if message.get("type") == protocol.MSG_KEYUP
            and message.get("data", {}).get("key") == "alt_r"
        ]
        # RETURN swallows the release before sender.on_key ever sees it, so
        # a key-down that did leak (proven above) is never balanced by a
        # key-up -- the Mac is left with a phantom held key.
        self.assertEqual(keyups, [])


if __name__ == "__main__":
    unittest.main()
