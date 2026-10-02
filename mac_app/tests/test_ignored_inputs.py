"""Keys and buttons that stay on this Mac while its input is on Windows: the event tap's decision,
the side buttons now crossing, and the setting's round trip through config.json."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import ignored
from core import protocol
import settings_store
from bridge import KVMController
from bridge_fakes import PAIRED_TOKEN, FakeClock, FakeQuartz, make_config, quiet_logger, redirect_to
from fake_link import FakeLink

KEY_F13 = 0x69
KEY_A = 0x00


def key_event(keycode, unicode=""):
    return {FakeQuartz.kCGKeyboardEventKeycode: keycode, FakeQuartz.kCGKeyboardEventAutorepeat: 0, "unicode": unicode}


def other_button(number):
    return {FakeQuartz.kCGMouseEventButtonNumber: number}


class TapTests(unittest.TestCase):
    def setUp(self):
        self.controller = KVMController(
            replace(make_config(PAIRED_TOKEN), ignored_inputs=[ignored.key(KEY_F13), ignored.button("back"), ignored.media("volume_up")]),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=FakeClock(),
            link_factory=FakeLink,
            system_event_converter=lambda event: event,
        )
        redirect_to(self.controller)

    def tap(self, event_type, event):
        return self.controller._event_tap_callback(None, event_type, event, None)

    def sent(self):
        messages = []
        while not self.controller.outbound.empty():
            messages.append(self.controller.outbound.get_nowait())
        return [(m["type"], m["data"]) for m in messages]

    def configure(self, entries):
        self.controller.apply_settings(replace(self.controller.cfg, ignored_inputs=list(entries)))

    def test_an_ignored_key_reaches_this_mac_and_not_windows(self):
        down, up = key_event(KEY_F13), key_event(KEY_F13)
        self.assertIs(self.tap(FakeQuartz.kCGEventKeyDown, down), down)
        self.assertIs(self.tap(FakeQuartz.kCGEventKeyUp, up), up)
        self.assertEqual(self.sent(), [])

    def test_any_other_key_still_goes_to_windows(self):
        self.assertIsNone(self.tap(FakeQuartz.kCGEventKeyDown, key_event(KEY_A, "a")))
        self.assertEqual(self.sent(), [(protocol.MSG_KEYDOWN, {"key": "a", "us": "a"})])

    def test_a_key_ignored_while_held_on_windows_is_still_released_there(self):
        self.configure([])
        self.assertIsNone(self.tap(FakeQuartz.kCGEventKeyDown, key_event(KEY_F13)))
        self.configure([ignored.key(KEY_F13)])
        self.assertIsNone(self.tap(FakeQuartz.kCGEventKeyUp, key_event(KEY_F13)))
        self.assertEqual(self.sent(), [(protocol.MSG_KEYDOWN, {"key": "f13"}), (protocol.MSG_KEYUP, {"key": "f13"})])

    def test_the_side_buttons_cross_as_back_and_forward(self):
        self.configure([])
        self.assertIsNone(self.tap(FakeQuartz.kCGEventOtherMouseDown, other_button(4)))
        self.assertIsNone(self.tap(FakeQuartz.kCGEventOtherMouseUp, other_button(4)))
        self.assertEqual(self.sent(), [
            (protocol.MSG_MOUSEDOWN, {"button": "forward"}),
            (protocol.MSG_MOUSEUP, {"button": "forward"}),
        ])

    def test_a_button_with_no_wire_name_stays_on_this_mac(self):
        # Button 6 was swallowed while redirecting: sent nowhere, and kept from this Mac too.
        self.configure([])
        down, up = other_button(5), other_button(5)
        self.assertIs(self.tap(FakeQuartz.kCGEventOtherMouseDown, down), down)
        self.assertIs(self.tap(FakeQuartz.kCGEventOtherMouseUp, up), up)
        self.assertEqual(self.sent(), [])

    def test_caps_lock_listed_mid_session_stays_here_from_its_next_toggle(self):
        # Each toggle is one FlagsChanged the translator reads as down; the first went across and
        # pinned its route until input came home.
        self.configure([])
        toggle_on = {FakeQuartz.kCGKeyboardEventKeycode: 0x39, "flags": FakeQuartz.kCGEventFlagMaskAlphaShift}
        self.assertIsNone(self.tap(FakeQuartz.kCGEventFlagsChanged, toggle_on))
        self.assertEqual(len(self.sent()), 2)
        self.configure([ignored.key(0x39)])
        toggle_off = {FakeQuartz.kCGKeyboardEventKeycode: 0x39, "flags": 0}
        self.assertIs(self.tap(FakeQuartz.kCGEventFlagsChanged, toggle_off), toggle_off)
        self.assertEqual(self.sent(), [])

    def test_an_ignored_button_stays_on_this_mac(self):
        down, up = other_button(3), other_button(3)
        self.assertIs(self.tap(FakeQuartz.kCGEventOtherMouseDown, down), down)
        self.assertIs(self.tap(FakeQuartz.kCGEventOtherMouseUp, up), up)
        self.assertEqual(self.sent(), [])

    def test_an_ignored_media_key_stays_on_this_mac(self):
        # NX key 0 is volume up; 0x0A00 is key down.
        event = (8, (0 << 16) | 0x0A00)
        self.assertIs(self.controller._handle_system_event(event), event)
        self.assertEqual(self.sent(), [])
        mute = (8, (7 << 16) | 0x0A00)
        self.assertIsNone(self.controller._handle_system_event(mute))
        self.assertEqual(self.sent(), [(protocol.MSG_KEYDOWN, {"key": "volume_mute"})])

    def test_coming_home_forgets_what_went_across(self):
        self.configure([])
        self.tap(FakeQuartz.kCGEventKeyDown, key_event(KEY_F13))
        self.configure([ignored.key(KEY_F13)])
        self.controller._return_local()
        redirect_to(self.controller)
        up = key_event(KEY_F13)
        self.assertIs(self.tap(FakeQuartz.kCGEventKeyUp, up), up)


class SettingsTests(unittest.TestCase):
    def test_the_list_survives_a_save_and_a_load(self):
        with tempfile.TemporaryDirectory() as directory:
            store = settings_store.SettingsStore(Path(directory) / "config.json")
            raw = settings_store.config_to_raw(settings_store.editable_default_config())
            raw["ignored_inputs"] = [ignored.key(KEY_F13), ignored.button("back"), ignored.media("volume_up")]
            store.save(raw)
            self.assertEqual(json.loads(store.path.read_text())["ignored_inputs"], raw["ignored_inputs"])
            self.assertEqual(store.load().ignored_inputs, raw["ignored_inputs"])

    def test_an_unreadable_entry_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            store = settings_store.SettingsStore(Path(directory) / "config.json")
            raw = settings_store.config_to_raw(settings_store.editable_default_config())
            raw["ignored_inputs"] = ["f13"]
            with self.assertRaises(settings_store.SettingsError):
                store.save(raw)


if __name__ == "__main__":
    unittest.main()
