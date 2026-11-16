import unittest

import sender
from tests.test_sender_shake import PEER, Clock, Owner

ESC = 0x1B
A = 0x41


class EscapeHomeSenderTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.sender = sender.LinkSender.__new__(sender.LinkSender)
        self.sender._clock = self.clock
        self.sender._owner = Owner()
        self.sender._reported_redirecting = False
        self.sender._redirect_callback = None
        self.sender._lock = __import__("threading").RLock()
        self.sender._buttons_held = set()
        self.sender._shake_home_pending = False
        self.sender._shake_detector = None
        self.sender._escape_taps = None
        self.sender._escape_callback = None
        self.sender._keys_down = {}
        self.sender._peers = {}
        self.sender._intent_windows_up = set()
        self.sender._intent_keys_up = set()
        self.sender._pending_windows_key = None
        self.sender._ignore_gate = sender.ignored.Gate()
        self.sender._config_lock = __import__("threading").RLock()
        self.sender._config = None
        self.sender._sent_names = {}
        self.sender._input_messages = []
        self.sender._input = lambda message: self.sender._input_messages.append(message) or True
        self.sender._queue = lambda actions: None
        self.sender._report = lambda actions: None
        self.sender.links = type("Links", (), {"live": lambda _self, _peer: True})()

    def tap(self, vk, name, at):
        self.clock.now = at
        sent_down = self.sender.on_key(name, True, vk)
        sent_up = self.sender.on_key(name, False, vk)
        return sent_down, sent_up

    def sent_keys(self):
        return [message["type"] for message in self.sender._input_messages]

    def test_third_quick_escape_comes_home_notifies_and_is_not_sent(self):
        homes = []
        self.sender.set_escape_home(True, on_escape=homes.append)
        self.tap(ESC, "esc", 0.0)
        self.tap(ESC, "esc", 0.2)
        before = len(self.sender._input_messages)
        self.clock.now = 0.4
        self.assertTrue(self.sender.on_key("esc", True, ESC))
        self.assertEqual(len(self.sender._input_messages), before)
        self.assertEqual(self.sender._owner.homes, 1)
        self.assertEqual(homes, [PEER])
        self.assertNotIn(ESC, self.sender._keys_down)
        self.assertEqual(before, 4)

    def test_auto_repeat_does_not_count(self):
        self.sender.set_escape_home(True)
        self.clock.now = 0.0
        for step in range(10):
            self.clock.now = step * 0.03
            self.sender.on_key("esc", True, ESC)
        self.sender.on_key("esc", False, ESC)
        self.assertEqual(self.sender._owner.homes, 0)

    def test_another_key_between_escapes_starts_again(self):
        self.sender.set_escape_home(True)
        self.tap(ESC, "esc", 0.0)
        self.tap(ESC, "esc", 0.1)
        self.tap(A, "a", 0.2)
        self.tap(ESC, "esc", 0.3)
        self.assertEqual(self.sender._owner.homes, 0)

    def test_off_never_comes_home(self):
        self.sender.set_escape_home(False)
        for step in range(3):
            self.tap(ESC, "esc", step * 0.1)
        self.assertEqual(self.sender._owner.homes, 0)


if __name__ == "__main__":
    unittest.main()
