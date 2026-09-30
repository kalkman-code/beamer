"""What the PC gained so each machine offers the same control as the other: a recorded
trigger key, the modifier style, Pause crossing, the full-screen hold, never crossing
while dragging, alerts, and waking the Mac."""

import time
import unittest
from dataclasses import replace
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app_config
import capture_win
from core import protocol
from app_config import ConfigError, config_from_dict, config_to_dict, default_config
import sender as sender_module
import test_sender
from test_sender import MONITORS, MacSenderWithLink, make_config, wait_for
from fakes import FakeClipboard, FakeDesktop, wait_for_calls


def raw(**values):
    return {"host": "192.0.2.3", "port": 24820, "auth_token": "t", **values}


class TriggerKeyTests(unittest.TestCase):
    def test_every_key_that_types_nothing_can_be_the_trigger(self):
        for name in ("f13", "insert", "scroll_lock", "shift", "caps_lock", "page_down", "esc"):
            self.assertEqual(config_from_dict(raw(trigger_key=name)).trigger_key, name)

    def test_the_eight_keys_saved_before_the_recorder_still_load(self):
        for name in ("cmd_r", "cmd", "alt_r", "alt", "ctrl_r", "ctrl", "shift_r", "menu"):
            self.assertEqual(config_from_dict(raw(trigger_key=name)).trigger_key, name)

    def test_a_character_or_a_media_key_is_refused(self):
        for name in ("a", "media_next", "volume_up", "browser_back"):
            with self.assertRaises(ConfigError):
                config_from_dict(raw(trigger_key=name))

    def test_every_trigger_is_titled_in_windows_words(self):
        self.assertEqual(app_config.TRIGGER_KEYS["cmd_r"], "Right Ctrl")
        self.assertEqual(app_config.TRIGGER_KEYS["ctrl"], "Left Windows")
        self.assertEqual(app_config.TRIGGER_KEYS["f13"], "F13")

    def test_a_recorded_trigger_is_what_the_hook_names_it(self):
        for name, vk in app_config.TRIGGER_VKS.items():
            self.assertEqual(capture_win.VK_TO_NAME[vk], name)
        trigger = capture_win.Trigger("f13", "double_tap", 300)
        self.assertIsNone(trigger.feed("f13", True, 0.0))
        self.assertEqual(trigger.feed("f13", True, 0.1), capture_win.TOGGLE)


class ReviewFixTests(unittest.TestCase):
    def test_a_key_recorded_in_hold_style_while_held_is_let_go_on_windows(self):
        # Right Alt went down before it was the trigger, so Windows has its key-down.
        trigger = capture_win.Trigger("cmd_r", "hold", 300)
        trigger.configure("alt_r", "hold", 300)
        self.assertIsNone(trigger.feed("alt_r", False, 0.0))
        self.assertFalse(trigger.claims("alt_r", False, False))
        # A hold that is the trigger's own still swallows every event of the key.
        self.assertEqual(trigger.feed("alt_r", True, 1.0), capture_win.REDIRECT)
        self.assertTrue(trigger.claims("alt_r", True, True))

    def test_typing_keys_are_never_offered_as_the_trigger(self):
        for vk in (0x08, 0x09, 0x0D, 0x1B, 0x20, 0x2C):
            self.assertIn(vk, app_config.UNRECORDABLE_TRIGGER_VKS)

    def test_the_subnet_broadcast_of_a_home_address(self):
        from core import wol

        self.assertEqual(wol.subnet_broadcast("192.168.1.10"), "192.168.1.255")
        self.assertIsNone(wol.subnet_broadcast("mac.local"))
        self.assertIsNone(wol.subnet_broadcast(None))


class ConfigRangeTests(unittest.TestCase):
    def test_the_macs_ranges(self):
        config_to_dict(replace(default_config(), auth_token="t", double_tap_ms=50, crossing_resistance_px=500))
        for change in ({"double_tap_ms": 40}, {"double_tap_ms": 2001}, {"crossing_resistance_px": 501}):
            with self.assertRaises(ConfigError):
                config_to_dict(replace(default_config(), auth_token="t", **change))

    def test_new_fields_default_and_round_trip(self):
        config = config_from_dict(raw())
        self.assertEqual(config.modifier_style, "semantic")
        self.assertTrue(config.block_while_dragging)
        self.assertEqual(config.mac_hardware_address, "")
        saved = config_to_dict(replace(config, modifier_style="positional", block_while_dragging=False))
        again = config_from_dict(saved)
        self.assertEqual((again.modifier_style, again.block_while_dragging), ("positional", False))

    def test_the_full_screen_hold_defaults_on_and_round_trips(self):
        config = config_from_dict(raw())
        self.assertTrue(config.hold_full_screen)
        saved = config_to_dict(replace(config, hold_full_screen=False))
        self.assertFalse(config_from_dict(saved).hold_full_screen)
        self.assertTrue(config_from_dict(raw()).hold_full_screen)

    def test_an_unknown_modifier_style_is_refused(self):
        with self.assertRaises(ConfigError):
            config_from_dict(raw(modifier_style="sideways"))


class HeldEdgeTests(unittest.TestCase):
    def setUp(self):
        self.desktop = FakeDesktop(MONITORS, cursor=(0, 500))
        self.link = MacSenderWithLink(desktop=self.desktop)
        self.sender = self.link.sender

    def tearDown(self):
        self.link.close()

    def push(self, times=10):
        for _ in range(times):
            self.sender.on_motion(-20, 0)

    def test_pause_holds_the_edge_and_leaves_the_shortcut(self):
        self.sender.crossing_paused = True
        self.push()
        self.assertFalse(self.sender.redirecting)
        self.assertTrue(self.sender.shortcut_armed)
        self.sender.crossing_paused = False
        self.push()
        self.assertTrue(self.sender.redirecting)

    def test_a_full_screen_app_holds_the_edge(self):
        self.sender.full_screen_app = "Game"
        self.push()
        self.assertFalse(self.sender.redirecting)

    def test_with_the_hold_off_a_full_screen_app_does_not_hold_the_edge_either_way(self):
        self.sender.update_config(make_config(hold_full_screen=False))
        self.sender.full_screen_app = "Game"
        self.assertIsNone(self.sender.full_screen_app)
        self.assertFalse(self.sender.edges_held)
        self.push()
        self.assertTrue(self.sender.redirecting)

    def test_a_drag_against_the_edge_does_not_cross(self):
        self.sender.on_mouse(capture_win.WM_LBUTTONDOWN, 0, 500, 0)
        self.push()
        self.assertFalse(self.sender.redirecting)
        self.sender.on_mouse(capture_win.WM_LBUTTONUP, 0, 500, 0)
        self.push()
        self.assertTrue(self.sender.redirecting)

    def test_a_button_whose_release_was_never_seen_does_not_hold_the_edge_for_ever(self):
        # Let go over the secure desktop: the hook never saw the release, Windows says it is up.
        self.desktop.button_down = lambda name: False
        self.sender.on_mouse(capture_win.WM_MBUTTONDOWN, 0, 500, 0)
        self.push()
        self.assertTrue(self.sender.redirecting)

    def test_a_drag_crosses_when_the_setting_is_off(self):
        self.sender.update_config(make_config(block_while_dragging=False))
        self.sender.on_mouse(capture_win.WM_LBUTTONDOWN, 0, 500, 0)
        self.push()
        self.assertTrue(self.sender.redirecting)


class AlertAndWakeTests(unittest.TestCase):
    def setUp(self):
        self.alerts = []
        self.packets = []
        self.sender = sender_module.MacSender(
            desktop=FakeDesktop(MONITORS, cursor=(900, 500)),
            clipboard=FakeClipboard(),
            is_local=lambda host: False,
            wake_sender=lambda mac, host: self.packets.append((mac, host)),
            mac_lookup=lambda host: None,
        )
        self.sender.on_alert = lambda title, message: self.alerts.append(message)

    def test_a_switch_with_no_link_and_no_address_says_why(self):
        self.sender.update_config(make_config())
        self.assertFalse(self.sender.set_redirecting(True))
        self.assertTrue(self.alerts and self.alerts[0].startswith("Cannot switch"))
        self.assertEqual(self.packets, [])

    def test_a_switch_with_no_link_wakes_a_mac_whose_address_is_known(self):
        self.sender.update_config(make_config(mac_hardware_address="02:1A:2B:3C:0D:4E"))
        self.assertFalse(self.sender.set_redirecting(True))
        self.assertTrue(wait_for(lambda: self.packets))
        self.assertEqual(self.packets[0], ("02:1A:2B:3C:0D:4E", "127.0.0.1"))
        self.assertTrue(wait_for(lambda: "Waking your Mac…" in self.alerts))
        self.sender._stop_event.set()

    def test_a_mac_that_refused_the_token_is_not_woken(self):
        self.sender.update_config(make_config(mac_hardware_address="02:1A:2B:3C:0D:4E"))
        self.sender._status = sender_module.AUTH_FAILED_STATUS
        self.assertFalse(self.sender.set_redirecting(True))
        self.assertEqual(self.packets, [])
        self.assertTrue(self.alerts[0].endswith(sender_module.AUTH_FAILED_STATUS))

    def test_a_dead_link_sending_input_home_says_so(self):
        self.sender.redirecting = True
        self.sender._force_local("the link went quiet")
        self.assertEqual(self.alerts, ["Input returned to this PC"])


class ModifierAndRoundTripTests(test_sender.LinkTests):
    """Over the real loopback link to the Mac's receiver, reusing LinkTests' set-up; its own
    tests are proved in test_sender and not run twice."""

    def test_positional_sends_ctrl_as_control(self):
        self.sender.update_config(make_config(port=self.port, modifier_style="positional"))
        self.sender.set_redirecting(True, arrival_edge="right", offset=0.5)
        self.sender.on_key("cmd", True)
        # The style changes while the key is held: its release still matches its press.
        self.sender.update_config(make_config(port=self.port))
        self.sender.on_key("cmd", False)
        wait_for_calls(self.injector.calls, 2)
        self.assertEqual(self.injector.calls, [("key", ("ctrl", True)), ("key", ("ctrl", False))])

    def test_shift_let_go_first_still_releases_the_capital(self):
        self.sender.set_redirecting(True, arrival_edge="right", offset=0.5)
        self.sender.on_key("A", True, 0x41)
        self.sender.on_key("a", False, 0x41)
        wait_for_calls(self.injector.calls, 2)
        self.assertEqual(self.injector.calls, [("key", ("A", True)), ("key", ("A", False))])
        self.assertEqual(self.sender._keys_down, {})

    def test_the_round_trip_is_measured_while_input_is_on_the_mac(self):
        self.assertIsNone(self.sender.round_trip_ms)
        self.sender.set_redirecting(True, arrival_edge="right", offset=0.5)
        for _ in range(5):
            self.sender.on_motion(3, 0)
            time.sleep(0.05)
        self.assertTrue(wait_for(lambda: self.sender.round_trip_ms is not None), "no round trip measured")
        self.assertGreaterEqual(self.sender.round_trip_ms, 0)


for _name in [n for n in dir(test_sender.LinkTests) if n.startswith("test_")]:
    setattr(ModifierAndRoundTripTests, _name, None)


if __name__ == "__main__":
    unittest.main()
