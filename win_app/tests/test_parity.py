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
from app_config import ConfigError, config_from_dict, config_to_dict, default_config
from core import protocol
from core.return_edge import Rect
from core.tests import responder_harness as harness
from core.tests.responder_harness import Initiator, Machine
from links_rig import B, Rig, make_config


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


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

    def test_the_full_screen_hold_defaults_off_and_preserves_each_saved_choice(self):
        self.assertFalse(config_from_dict(raw()).hold_full_screen)
        for choice in (False, True):
            saved = config_to_dict(replace(default_config(), auth_token="t", hold_full_screen=choice))
            self.assertEqual(config_from_dict(saved).hold_full_screen, choice)

    def test_mac_layout_round_trips_and_an_older_file_defaults_to_semantic(self):
        old = config_from_dict(raw())
        self.assertEqual(old.modifier_style, "semantic")
        saved = config_to_dict(replace(old, modifier_style="mac_layout"))
        self.assertEqual(config_from_dict(saved).modifier_style, "mac_layout")

    def test_an_unknown_modifier_style_is_refused(self):
        with self.assertRaises(ConfigError):
            config_from_dict(raw(modifier_style="sideways"))


class HeldEdgeTests(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(cursor=(0, 500))
        self.sender = self.rig.sender

    def push(self, times=10):
        self.rig.push(times)

    def test_pause_holds_the_edge_and_leaves_the_shortcut(self):
        self.sender.crossing_paused = True
        self.push()
        self.assertFalse(self.sender.redirecting)
        self.assertTrue(self.sender.shortcut_armed)
        self.sender.crossing_paused = False
        self.push()
        self.assertTrue(self.sender.redirecting)

    def test_a_full_screen_app_holds_the_edge(self):
        # The hold is off by default since 02-10-2026.
        self.sender.update_config(make_config(hold_full_screen=True))
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

    def test_this_pcs_full_screen_setting_decides_its_edges_while_the_mac_drives_it(self):
        for enabled in (False, True):
            with self.subTest(hold_full_screen=enabled):
                rig = Rig(cursor=(0, 500), hold_full_screen=enabled)
                rig.sender.full_screen_app = "Game"
                desktop = harness.FakeDesktop([Rect(0, 0, 1920, 1080)], cursor=(0, 500))
                machine = Machine(
                    [harness.entry(B, "Mac", side="left")],
                    [{"peer": protocol.id_text(B), "kind": "edge"}],
                    desktop=desktop,
                ).start()
                self.addCleanup(machine.stop)
                # This is the WindowsApplication receiver wiring, using this PC's own setting.
                machine.responder.edges_held = lambda: rig.sender.edges_held
                link = Initiator(machine, B)
                self.addCleanup(link.close)
                link.handshake()
                route = link.take()
                self.assertEqual(link.answer(route)[0], protocol.MSG_ACCEPT)
                seq = link.move(-200, 0)
                self.assertIsNotNone(link.acked(seq))
                crossed = link.expect(protocol.MSG_SWITCH, timeout=0.2)
                if enabled:
                    self.assertIsNone(crossed, "the PC's enabled hold let the Mac cross its edge")
                else:
                    self.assertEqual(crossed["next"], protocol.id_text(B))

    def test_a_drag_against_the_edge_does_not_cross(self):
        self.sender.on_button("left", True)
        self.push()
        self.assertFalse(self.sender.redirecting)
        self.sender.on_button("left", False)
        self.push()
        self.assertTrue(self.sender.redirecting)

    def test_a_button_whose_release_was_never_seen_does_not_hold_the_edge_for_ever(self):
        # Let go over the secure desktop: the hook never saw the release, Windows says it is up.
        self.rig.desktop.button_down = lambda name: False
        self.sender.on_button("middle", True)
        self.push()
        self.assertTrue(self.sender.redirecting)

    def test_a_drag_crosses_when_the_setting_is_off(self):
        self.sender.update_config(make_config(block_while_dragging=False))
        self.sender.on_button("left", True)
        self.push()
        self.assertTrue(self.sender.redirecting)


class AlertAndWakeTests(unittest.TestCase):
    def setUp(self):
        self.packets = []

    def rig(self, **fields):
        rig = Rig(entries=[harness.entry(B, "Mac", side="left", **fields)], up=())
        rig.sender._wake_sender = lambda address, host=None: self.packets.append((address, host))
        return rig

    def test_a_switch_with_no_link_and_no_address_says_why(self):
        rig = self.rig()
        self.assertFalse(rig.sender.set_redirecting(True))
        self.assertTrue(rig.alerts and rig.alerts[0].startswith("Cannot switch"))
        self.assertEqual(self.packets, [])

    def test_a_switch_with_no_link_wakes_a_mac_whose_address_is_known(self):
        rig = self.rig(hw="02:1A:2B:3C:0D:4E")
        self.assertFalse(rig.sender.set_redirecting(True))
        self.assertTrue(wait_for(lambda: self.packets))
        self.assertEqual(self.packets[0], ("02:1A:2B:3C:0D:4E", "192.168.77.9"))
        self.assertTrue(wait_for(lambda: "Waking Mac…" in rig.alerts))
        rig.sender._stop_event.set()

    def test_a_mac_that_refused_the_token_is_not_woken(self):
        rig = self.rig(hw="02:1A:2B:3C:0D:4E")
        rig.links.refusing.add(B)
        rig.sender.on_status(rig.links.key_of(rig.settings.data["peers"][0]), False, "Mac refused the pairing")
        self.assertFalse(rig.sender.set_redirecting(True))
        self.assertEqual(self.packets, [])
        self.assertTrue(rig.alerts[0].endswith("Mac refused the pairing"))

    def test_a_dead_link_sending_input_home_says_so(self):
        rig = Rig()
        rig.sender.set_redirecting(True)
        rig.accept_take()
        rig.links.up_set.discard(B)
        rig.sender.on_key("a", True, 0x41, "a")
        self.assertFalse(rig.sender.redirecting)
        self.assertEqual(rig.alerts, ["Lost the link to Mac"])


class ModifierStyleTests(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.sender = self.rig.sender
        self.sender.set_redirecting(True)
        self.rig.accept_take()

    def keys(self):
        return [(m["type"], m["data"]["key"]) for m in self.rig.sent(B) if m["type"] in ("keydown", "keyup")]

    def test_positional_sends_ctrl_as_control(self):
        self.sender.update_config(make_config(modifier_style="positional"))
        self.sender.on_key("cmd", True)
        self.assertEqual(self.keys(), [("keydown", "ctrl")])

    # LinkSender._key_message applies the style when the outbound worker delivers a key, and
    # `_keys_down` holds only the physical name, so a release after a style change leaves under
    # the new style's name: the Mac gets ctrl down and cmd up. The press goes out within
    # milliseconds, hence the flush between the two calls.
    def test_the_style_changing_while_a_key_is_held_still_releases_it_under_its_press_name(self):
        self.sender.update_config(make_config(modifier_style="positional"))
        self.sender.on_key("cmd", True)
        self.rig.flush()
        self.sender.update_config(make_config())
        self.sender.on_key("cmd", False)
        self.assertEqual(self.keys(), [("keydown", "ctrl"), ("keyup", "ctrl")])

    def test_shift_let_go_first_still_releases_the_capital(self):
        self.sender.on_key("A", True, 0x41)
        self.sender.on_key("a", False, 0x41)
        self.assertEqual(self.keys(), [("keydown", "A"), ("keyup", "A")])
        self.assertEqual(self.sender._keys_down, {})


if __name__ == "__main__":
    unittest.main()
