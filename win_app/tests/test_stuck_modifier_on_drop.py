import sys
import unittest
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import input_injector


class WindowsInjectorReleaseTests(unittest.TestCase):
    """Anything injected for the peer is released when input comes home or the link dies; that the
    responder calls release_all then is held by core/tests (LinkResponder). This is the Windows
    injector doing it."""

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
