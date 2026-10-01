"""Keys and buttons that stay on this PC while its input is on the Mac: the sender's decision on
the hook thread, the side buttons now crossing, and the setting's round trip through settings.json."""

import json
import tempfile
import unittest
from pathlib import Path
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app_config
import capture_win
from core import ignored
from core import protocol
from links_rig import B, Rig, make_config as rig_config

VK_F13 = 0x7C
VK_A = 0x41


def make_config(**overrides):
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
    return app_config.Config(**values)


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(cursor=(900, 500), ignored_inputs=[ignored.key(VK_F13), ignored.button("back")])
        self.sender = self.rig.sender
        self.sender.set_redirecting(True)
        self.rig.accept_take()

    def configure(self, entries):
        self.sender.update_config(rig_config(ignored_inputs=list(entries)))

    def sent(self):
        return [(m["type"], m["data"]) for m in self.rig.sent(B) if m["type"] in (
            protocol.MSG_KEYDOWN, protocol.MSG_KEYUP, protocol.MSG_MOUSEDOWN, protocol.MSG_MOUSEUP
        )]

    def mouse(self, message, data=0):
        """`WindowsApplication._on_hook_mouse` for a button: the hook's message, then the sender's call."""
        event = capture_win.mouse_event(message, data)
        self.assertEqual(event[0], "button")
        return self.sender.on_button(event[1], event[2])

    def test_an_ignored_key_reaches_this_pc_and_not_the_peer(self):
        self.assertFalse(self.sender.on_key("f13", True, VK_F13))
        self.assertFalse(self.sender.on_key("f13", False, VK_F13))
        self.assertEqual(self.sent(), [])

    def test_any_other_key_still_goes_to_the_peer(self):
        self.assertTrue(self.sender.on_key("a", True, VK_A))
        self.assertTrue(self.sender.on_key("a", False, VK_A))
        self.assertEqual(self.sent(), [
            (protocol.MSG_KEYDOWN, {"key": "a"}),
            (protocol.MSG_KEYUP, {"key": "a"}),
        ])

    def test_a_key_ignored_while_held_on_the_peer_is_still_released_there(self):
        self.configure([])
        self.assertTrue(self.sender.on_key("f13", True, VK_F13))
        self.configure([ignored.key(VK_F13)])
        self.assertTrue(self.sender.on_key("f13", False, VK_F13))
        self.assertEqual(self.sent(), [
            (protocol.MSG_KEYDOWN, {"key": "f13"}),
            (protocol.MSG_KEYUP, {"key": "f13"}),
        ])

    def test_the_side_buttons_cross_as_back_and_forward(self):
        self.configure([])
        for message in (capture_win.WM_XBUTTONDOWN, capture_win.WM_XBUTTONUP):
            self.assertTrue(self.mouse(message, 2 << 16))
        self.assertEqual(self.sent(), [
            (protocol.MSG_MOUSEDOWN, {"button": "forward"}),
            (protocol.MSG_MOUSEUP, {"button": "forward"}),
        ])

    def test_an_ignored_button_stays_on_this_pc(self):
        self.assertFalse(self.mouse(capture_win.WM_XBUTTONDOWN, 1 << 16))
        self.assertFalse(self.mouse(capture_win.WM_XBUTTONUP, 1 << 16))
        self.assertTrue(self.mouse(capture_win.WM_MBUTTONDOWN))
        self.assertEqual(self.sent(), [(protocol.MSG_MOUSEDOWN, {"button": "middle"})])


class CaptureTests(unittest.TestCase):
    def test_x_buttons_are_named_from_the_high_word(self):
        self.assertEqual(capture_win.button_of(capture_win.WM_XBUTTONDOWN, 1 << 16), ("back", True))
        self.assertEqual(capture_win.button_of(capture_win.WM_XBUTTONUP, 2 << 16), ("forward", False))
        self.assertIsNone(capture_win.button_of(capture_win.WM_XBUTTONDOWN, 3 << 16))
        self.assertIsNone(capture_win.button_of(capture_win.WM_MOUSEWHEEL, 120 << 16))
        self.assertEqual(capture_win.button_of(capture_win.WM_RBUTTONDOWN, 0), ("right", True))

    def test_entries_are_shown_in_windows_own_words(self):
        titles = {
            ignored.key(0xA3): "Right Ctrl",
            ignored.key(VK_F13): "F13",
            ignored.key(VK_A): "A",
            ignored.key(0xAF): "Volume Up",
            ignored.key(0xFE): "Key 254",
            ignored.button("back"): "Back button",
            ignored.button("6"): "Button 6",
        }
        for entry, title in titles.items():
            self.assertEqual(capture_win.input_title(entry), title)

    def test_every_trigger_key_has_its_virtual_key(self):
        self.assertEqual(set(app_config.TRIGGER_VKS), set(app_config.TRIGGER_KEYS))
        for name, vk in app_config.TRIGGER_VKS.items():
            self.assertEqual(capture_win.VK_TO_NAME[vk], name)

    def test_a_windows_modifier_is_recorded_as_the_side_the_hook_reports(self):
        self.assertEqual(capture_win.hook_vk(0x11, 0x1D), 0xA2)
        self.assertEqual(capture_win.hook_vk(0x11, 0x11D), 0xA3)
        self.assertEqual(capture_win.hook_vk(0x12, 0x38), 0xA4)
        self.assertEqual(capture_win.hook_vk(0x12, 0x138), 0xA5)
        # How Qt 6 reports them: the extended prefix, not the bit.
        self.assertEqual(capture_win.hook_vk(0x11, 0xE01D), 0xA3)
        self.assertEqual(capture_win.hook_vk(0x12, 0xE038), 0xA5)
        self.assertEqual(capture_win.hook_vk(0x10, 0x2A), 0xA0)
        self.assertEqual(capture_win.hook_vk(0x10, 0x36), 0xA1)
        self.assertEqual(capture_win.hook_vk(VK_A, 0x1E), VK_A)


class ConfigTests(unittest.TestCase):
    def test_the_list_survives_a_save_and_a_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            entries = [ignored.key(VK_F13), ignored.button("back")]
            app_config.save_config(path, make_config(port=24820, ignored_inputs=entries))
            self.assertEqual(json.loads(path.read_text())["ignored_inputs"], entries)
            self.assertEqual(app_config.load_config(path).ignored_inputs, entries)

    def test_an_older_config_has_nothing_ignored(self):
        raw = app_config.config_to_dict(make_config(port=24820))
        del raw["ignored_inputs"]
        self.assertEqual(app_config.config_from_dict(raw).ignored_inputs, [])

    def test_an_unreadable_entry_is_refused(self):
        with self.assertRaises(app_config.ConfigError):
            app_config.validate_config(make_config(port=24820, ignored_inputs=["f13"]))


if __name__ == "__main__":
    unittest.main()
