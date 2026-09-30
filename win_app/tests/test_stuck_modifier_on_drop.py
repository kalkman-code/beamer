import sys
import unittest
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import input_injector
from core import receiver


class FakeInjector:
    def __init__(self):
        self.calls = []

    def inject_key(self, name, down, us=None):
        self.calls.append((name, down))

    def release_all(self):
        self.calls.append(("release_all",))


class PeerKeysReleasedTests(unittest.TestCase):
    """Invariant 3: anything injected for the peer is released when input
    comes home or the link dies, on this PC exactly as on the Mac."""

    def _server(self, injector):
        server = receiver.ReceiverServer(
            status_callback=lambda *a: None,
            injector=injector,
            focus_callback=lambda target: None,
        )
        server._peer_driving = True
        return server

    def test_hand_back_releases_what_the_peer_held(self):
        injector = FakeInjector()
        server = self._server(injector)
        receiver.handle_message({"type": "keydown", "data": {"key": "ctrl"}}, injector=injector)
        server._hand_back("peer went away")
        self.assertIn(("release_all",), injector.calls)

    def test_input_going_home_releases_what_the_peer_held(self):
        injector = FakeInjector()
        server = self._server(injector)
        server._clipboard = type("C", (), {"changed_contents": staticmethod(lambda: (None, None))})
        server._handle_focus({"type": "focus", "data": {"target": "mac"}}, None, None, "peer", "192.168.1.5")
        self.assertIn(("release_all",), injector.calls)

    @unittest.skipUnless(sys.platform == "win32", "the scan-code lookup needs user32")
    def test_the_windows_injector_releases_keys_and_buttons(self):
        sent = []
        original_send = input_injector._send_input
        input_injector._send_input = lambda *inputs: sent.extend(inputs)
        try:
            input_injector._mods_down.update({"ctrl", "shift"})
            input_injector._char_vk_down["a"] = 0x41
            input_injector._buttons_down.add("left")
            input_injector.release_all()
        finally:
            input_injector._send_input = original_send
        self.assertEqual(input_injector._mods_down, set())
        self.assertEqual(input_injector._char_vk_down, {})
        self.assertEqual(input_injector._buttons_down, set())
        keyups = [i for i in sent if i.type == input_injector.INPUT_KEYBOARD and i.union.ki.dwFlags & input_injector.KEYEVENTF_KEYUP]
        buttonups = [i for i in sent if i.type == input_injector.INPUT_MOUSE and i.union.mi.dwFlags == input_injector.MOUSEEVENTF_LEFTUP]
        self.assertEqual(len(keyups), 3)
        self.assertEqual(len(buttonups), 1)


if __name__ == "__main__":
    unittest.main()
