"""Reproduces win-sender-3: a key held down while redirecting, released after
switching back to Windows, never reaches the Mac as a key-up -- because
on_key's gate reads the live `redirecting` flag rather than whether the
key-down was actually forwarded."""

import time
import unittest
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import protocol
import sender
from fakes import FakeClipboard, FakeDesktop
from core.return_edge import Rect

MONITORS = [Rect(0, 0, 1920, 1080)]


def make_config(**overrides):
    from app_config import Config

    values = dict(
        host="127.0.0.1",
        port=0,
        auth_token="shared-token",
        mac_host="127.0.0.1",
        mac_return_edge="left",
        mac_resistance_px=40,
        crossing_resistance_px=40,
    )
    values.update(overrides)
    return Config(**values)


class KeyUpAfterSwitchBackTest(unittest.TestCase):
    def setUp(self):
        self.desktop = FakeDesktop(MONITORS, cursor=(0, 500))
        self.sender = sender.MacSender(desktop=self.desktop, clipboard=FakeClipboard(), is_local=lambda host: False)
        self.sender.update_config(make_config())
        self.sender._sock = object()
        self.sender._connected_at = time.monotonic()
        self.sender._last_ack_at = time.monotonic()

    def tearDown(self):
        self.sender._sock = None

    def _drain_keys(self):
        keys = []
        while not self.sender._outbound.empty():
            message = self.sender._outbound.get_nowait()
            if message.get("type") in (protocol.MSG_KEYDOWN, protocol.MSG_KEYUP):
                keys.append(message)
        return keys

    def test_key_released_after_switch_back_is_dropped(self):
        self.sender.set_redirecting(True, arrival_edge="right", offset=0.5)

        sent_down = self.sender.on_key("a", True)
        self.assertTrue(sent_down, "the key-down while redirecting should have been forwarded")

        self.sender.set_redirecting(False)

        sent_up = self.sender.on_key("a", False)

        keys = self._drain_keys()
        self.assertEqual(
            [k["type"] for k in keys],
            [protocol.MSG_KEYDOWN, protocol.MSG_KEYUP],
            "a key-down forwarded to the Mac owes it the matching key-up, "
            "even after redirecting has already been switched back off",
        )
        self.assertTrue(
            sent_up,
            "on_key must swallow the key-up locally, since Windows never saw the key go down",
        )

    def test_a_held_keys_autorepeat_at_home_is_swallowed_until_its_release(self):
        self.sender.set_redirecting(True, arrival_edge="right", offset=0.5)
        self.sender.on_key("a", True)
        self.sender.set_redirecting(False)
        self.assertTrue(self.sender.on_key("a", True), "an autorepeat of a key the Mac holds")
        self.assertTrue(self.sender.on_key("a", False), "the release Windows never saw the down for")
        self.assertFalse(self.sender.on_key("a", True), "a fresh press is Windows' own again")

    def test_a_release_still_queued_when_input_comes_home_is_sent(self):
        sent = []
        self.sender._send_raw = lambda message: sent.append(message) or True

        def drain():
            while not self.sender._outbound.empty():
                self.sender._process_outbound(self.sender._outbound.get_nowait())

        self.sender.set_redirecting(True, arrival_edge="right", offset=0.5)
        self.sender.on_key("a", True)
        drain()
        # The release is queued but not yet sent when input comes home.
        self.sender.on_key("a", False)
        self.sender.set_redirecting(False)
        drain()
        self.assertEqual(
            [m["type"] for m in sent if m["type"] in (protocol.MSG_KEYDOWN, protocol.MSG_KEYUP)],
            [protocol.MSG_KEYDOWN, protocol.MSG_KEYUP],
        )


if __name__ == "__main__":
    unittest.main()
