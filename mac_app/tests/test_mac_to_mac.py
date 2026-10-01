"""Mac to Mac on loopback: two real KVMControllers, each with the Mac's own responder
(windows_input.WindowsInput, its injector, desktop and clipboard faked as two_machines.py does),
paired to each other. Either drives the other, and every physical modifier pressed on the one is
pressed as the same key on the other (WIRE.md section 7, same family), in either style; a pinch and
a page swipe arrive as the Mac's own chords."""

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config
import input_injector_mac
import windows_input
from bridge import KVMController
import bridge_fakes
from bridge_fakes import PAIRED_TOKEN, FakeClock, FakeQuartz, crossing_config, quiet_logger
from core import protocol
from core.return_edge import Rect
from core.tests.responder_harness import FakeDesktop, FakeInjector, entry
from settings_store import SettingsStore
from two_machines import free_port, wait_for

BOUNDS = (0, 0, 1728, 1117)
# The Mac's modifier key codes, and the flag each sets while it is down.
MODIFIERS = {
    0x38: ("Left Shift", "kCGEventFlagMaskShift"), 0x3C: ("Right Shift", "kCGEventFlagMaskShift"),
    0x3B: ("Left Control", "kCGEventFlagMaskControl"), 0x3E: ("Right Control", "kCGEventFlagMaskControl"),
    0x3A: ("Left Option", "kCGEventFlagMaskAlternate"), 0x3D: ("Right Option", "kCGEventFlagMaskAlternate"),
    0x37: ("Left Command", "kCGEventFlagMaskCommand"), 0x36: ("Right Command", "kCGEventFlagMaskCommand"),
}
SWIPE_TYPE, MAGNIFY_TYPE = 31, 30


class _Mac:
    def __init__(self, name, clock):
        self.name = name
        self.port = free_port()
        self.dir = tempfile.TemporaryDirectory()
        self.store = SettingsStore(Path(self.dir.name) / "settings.json")
        settings = copy.deepcopy(self.store.current())
        settings["port"] = self.port
        self.store.save_settings(settings)
        self.id = protocol.read_id(self.store.current()["machine_id"])
        self.text = protocol.id_text(self.id)
        self.clock = clock

    def pair_with(self, other, side, key_map):
        settings = copy.deepcopy(self.store.current())
        settings["peers"] = [entry(other.id, other.name, platform="macos", token=PAIRED_TOKEN, host="127.0.0.1",
                                   port=other.port, side=side)]
        settings["zones"] = [{"peer": other.text, "kind": "edge"}]
        self.store.save_settings(settings)
        cfg = crossing_config(PAIRED_TOKEN, edge=side, resistance_px=40)
        cfg.host, cfg.port, cfg.auth_token = "127.0.0.1", self.port, PAIRED_TOKEN
        cfg.reconnect_interval_s = 0.05
        cfg.key_map = key_map
        # Right Option is Beamer's own shortcut by default and stays on the Mac; moved off the
        # modifiers so every one of them is pressed through.
        cfg.trigger_key = "f19"
        self.cfg = cfg
        self.clipboard = bridge_fakes.FakeClipboard(f"on {self.name}")
        self.controller = KVMController(
            cfg, logger=quiet_logger(), quartz=FakeQuartz, clock=self.clock, desktop_bounds=lambda: BOUNDS,
            book=self.store.book(), clipboard=self.clipboard,
        )
        self.controller.on_user_alert = lambda title, message: None
        self.injector = FakeInjector()
        left, top, right, bottom = BOUNDS
        with mock.patch.multiple(
            windows_input, input_injector_mac=self.injector,
            desktop_mac=FakeDesktop([Rect(left, top, right - left, bottom - top)], cursor=(800, 500)),
            clipboard_mac=self.clipboard,
        ):
            self.input = windows_input.WindowsInput(
                self.controller, logger=quiet_logger(), arrangement_callback=lambda *a: None, arrival_callback=lambda *a: None)

    def listen(self):
        self.input.sync(self.cfg)
        assert wait_for(lambda: self.input.server.listening), f"{self.name}'s responder never listened"

    def close(self):
        self.controller.stop()
        self.input.server.stop()
        self.dir.cleanup()

    def tap(self, event_type, fields):
        return self.controller._event_tap_callback(None, event_type, fields, None)


class _Pair:
    style = "semantic"

    def setUp(self):
        FakeQuartz.reset_cursor_spies()
        clock = FakeClock()
        key_map = config.DEFAULT_KEY_MAP if self.style == "semantic" else config.LEGACY_POSITIONAL_KEY_MAP
        self.near, self.far = _Mac("Near Mac", clock), _Mac("Far Mac", clock)
        self.near.pair_with(self.far, "right", dict(key_map))
        self.far.pair_with(self.near, "left", dict(key_map))
        for mac in (self.near, self.far):
            mac.listen()
            self.addCleanup(mac.close)

    def drive(self, driver, driven):
        driver.controller.start()
        assert wait_for(lambda: driven.text in driver.controller._peers_up), f"{driver.name} never linked to {driven.name}: {driver.controller.connection_status}"
        self.assertTrue(driver.controller.set_redirecting(True))
        self.assertTrue(wait_for(lambda: driven.input.server.owner == driver.id))

    def keys_pressed_on(self, driven, count):
        self.assertTrue(wait_for(lambda: len([c for c in driven.injector.calls if c[0] == "key"]) >= count),
                        driven.injector.calls)
        return [(name, down) for kind, name, down in (c for c in driven.injector.calls if c[0] == "key")]

    def modifiers_each_as_itself(self, driver, driven):
        self.drive(driver, driven)
        for code, (_, mask) in MODIFIERS.items():
            for down in (True, False):
                flags = getattr(FakeQuartz, mask) if down else 0
                self.assertIsNone(driver.tap(FakeQuartz.kCGEventFlagsChanged,
                                             {FakeQuartz.kCGKeyboardEventKeycode: code, "flags": flags}))
        pressed = self.keys_pressed_on(driven, 2 * len(MODIFIERS))
        got = [(input_injector_mac.plan_key_event(name, down, set())[0], down) for name, down in pressed]
        expected = [(code, down) for code in MODIFIERS for down in (True, False)]
        self.assertEqual(got, expected, pressed)

    def test_every_modifier_near_presses_is_pressed_as_itself_on_far(self):
        self.modifiers_each_as_itself(self.near, self.far)

    def test_every_modifier_far_presses_is_pressed_as_itself_on_near(self):
        self.modifiers_each_as_itself(self.far, self.near)


class SemanticStyle(_Pair, unittest.TestCase):
    style = "semantic"

    def test_a_page_swipe_is_command_bracket_on_the_other_mac(self):
        self.drive(self.near, self.far)
        gesture = type("G", (), {"magnification": 0.0, "deltaX": 1.0, "deltaY": 0.0, "phase": 0})()
        self.near.controller.handle_overlay_gesture(SWIPE_TYPE, gesture)
        self.assertEqual(self.keys_pressed_on(self.far, 4), [("cmd", True), ("[", True), ("[", False), ("cmd", False)])

    def test_a_pinch_is_command_plus_on_the_other_mac(self):
        self.drive(self.near, self.far)
        gesture = type("G", (), {"magnification": 0.06, "deltaX": 0.0, "deltaY": 0.0, "phase": 0})()
        self.near.controller.handle_overlay_gesture(MAGNIFY_TYPE, gesture)
        self.assertEqual(self.keys_pressed_on(self.far, 4), [("cmd", True), ("=", True), ("=", False), ("cmd", False)])
        self.assertFalse([c for c in self.far.injector.calls if c[0] == "scroll"])


class PositionalStyle(_Pair, unittest.TestCase):
    style = "positional"


if __name__ == "__main__":
    unittest.main()
