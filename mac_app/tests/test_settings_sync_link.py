"""Same on both machines over loopback: the real links of both apps, announcing on connect and
carrying each change, with two small ends holding each machine's state through settings_sync as
the apps' windows do (apply_same on the Mac, _on_settings on the PC)."""

import unittest
from unittest import mock
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from two_machines import Duo, wait_for  # first: it puts the PC's own modules on the path

import app_config  # noqa: E402
from core import protocol  # noqa: E402
from core import settings_sync  # noqa: E402
from key_codes import KEY_NAME_TO_CODE  # noqa: E402
from settings_store import config_to_raw  # noqa: E402


class MacEnd:
    def __init__(self, duo):
        self.duo = duo
        self.raw = config_to_raw(duo.mac.cfg)
        self.raw["same_on_both"], self.raw["same_set_at"] = False, 0

    def state(self):
        return settings_sync.message_data(self.raw["same_on_both"], self.raw["same_set_at"],
                                          settings_sync.mac_values(self.raw))

    def take(self, data):
        taken = settings_sync.arrived(data, self.raw["same_set_at"], KEY_NAME_TO_CODE)
        if taken is None:
            return
        on, set_at, values = taken
        raw = settings_sync.apply_mac(self.raw, values) if on else dict(self.raw)
        raw["same_on_both"], raw["same_set_at"] = on, set_at
        self.raw = raw

    def set(self, on=None, at=0, **crossing):
        if on is not None:
            self.raw["same_on_both"] = on
        self.raw["crossing"] = {**self.raw["crossing"], **crossing}
        self.raw["same_set_at"] = at
        data = self.state()
        self.duo.mac.send_settings(data)
        self.duo.mac_server.send_settings(data)


class PCEnd:
    def __init__(self, duo):
        self.duo = duo
        self.config = duo.pc_config

    def state(self):
        return settings_sync.message_data(self.config.same_on_both, self.config.same_set_at,
                                          settings_sync.pc_values(self.config))

    def take(self, data):
        taken = settings_sync.arrived(data, self.config.same_set_at, app_config.TRIGGER_KEYS)
        if taken is None:
            return
        on, set_at, values = taken
        if on:
            settings_sync.apply_pc(self.config, values)
        self.config.same_on_both, self.config.same_set_at = on, set_at

    def set(self, on=None, at=0, **fields):
        if on is not None:
            self.config.same_on_both = on
        for name, value in fields.items():
            setattr(self.config, name, value)
        self.config.same_set_at = at
        data = self.state()
        self.duo.pc.send_settings(data)
        self.duo.pc_server.send_settings(data)


class SettingsOverLoopbackTests(unittest.TestCase):
    def setUp(self):
        self.duo = Duo(pc=dict(mac_return_edge="left"))
        self.addCleanup(self.duo.close)
        self.mac, self.pc = MacEnd(self.duo), PCEnd(self.duo)
        for link in (self.duo.mac, self.duo.mac_server):
            link.announce = lambda: [protocol.settings_msg(self.mac.state())]
        self.duo.mac.on_settings = self.mac.take
        self.duo.mac_server.settings_callback = self.mac.take
        for link in (self.duo.pc, self.duo.pc_server):
            link.announce = lambda: [protocol.settings_msg(self.pc.state())]
            link.settings_callback = self.pc.take

    def both_links(self):
        self.duo.listen().link_mac_to_pc().link_pc_to_mac()

    def test_turning_it_on_carries_this_machines_values_across(self):
        self.both_links()
        self.mac.set(on=True, at=100, glow_style="beam", corner="bottom_right", resistance_px=60)
        self.assertTrue(wait_for(lambda: self.pc.config.same_on_both))
        self.assertEqual((self.pc.config.glow_style, self.pc.config.crossing_resistance_px), ("beam", 60))
        # The PC's Mac is on its left, so the Mac's bottom right is the PC's bottom left.
        self.assertEqual(self.pc.config.crossing_corner, "bottom_left")
        self.assertEqual(self.pc.config.same_set_at, 100)

    def test_a_change_on_either_machine_reaches_the_other_while_on(self):
        self.both_links()
        self.mac.set(on=True, at=100)
        self.assertTrue(wait_for(lambda: self.pc.config.same_on_both))
        self.pc.set(at=101, effect_length="long", crossing_methods=["corner"])
        self.assertTrue(wait_for(lambda: self.mac.raw["crossing"]["effect_length"] == "long"))
        # The notch is the Mac's own way in and stays.
        self.assertEqual(self.mac.raw["crossing"]["methods"], ["corner", "notch"]
                         if "notch" in self.duo.mac.cfg.crossing["methods"] else ["corner"])
        self.mac.set(at=102, glow_colour="ocean")
        self.assertTrue(wait_for(lambda: self.pc.config.glow_colour == "ocean"))

    def test_the_newer_end_wins_when_a_link_comes_up(self):
        # Changed on the PC while the Mac was away: the PC's is newer, and the Mac takes it the
        # moment its own link connects, from the PC receiver's announcement.
        self.pc.config.same_on_both, self.pc.config.same_set_at = True, 500
        self.pc.config.glow_style = "beam"
        self.mac.raw["same_on_both"], self.mac.raw["same_set_at"] = True, 400
        self.duo.listen().link_mac_to_pc()
        self.assertTrue(wait_for(lambda: self.mac.raw["same_set_at"] == 500))
        self.assertEqual(self.mac.raw["crossing"]["glow_style"], "beam")
        # And the older end's own announcement changed nothing at the newer.
        self.assertEqual(self.pc.config.same_set_at, 500)

    def test_turning_it_off_turns_it_off_at_both_and_leaves_the_values(self):
        self.both_links()
        self.mac.set(on=True, at=100, glow_style="beam")
        self.assertTrue(wait_for(lambda: self.pc.config.glow_style == "beam"))
        self.pc.set(on=False, at=200)
        self.assertTrue(wait_for(lambda: self.mac.raw["same_on_both"] is False))
        self.assertEqual(self.mac.raw["crossing"]["glow_style"], "beam")
        self.mac.raw["crossing"]["glow_style"] = "glow"
        self.assertEqual(self.pc.config.glow_style, "beam")

    def test_both_ends_know_a_peer_that_keeps_settings_in_step(self):
        self.both_links()
        self.assertTrue(wait_for(lambda: self.duo.mac_server.peer_settings is True))
        self.assertTrue(self.duo.mac.peer_settings)
        self.assertTrue(self.duo.pc.peer_settings)
        self.assertTrue(wait_for(lambda: self.duo.pc_server.peer_settings is True))

    def test_an_older_peer_is_known_as_one_on_either_link(self):
        # A Beamer before 1.5.0 sends a hello and a welcome with no settings flag.
        older_hello, older_welcome = protocol.hello_msg, protocol.welcome_msg

        def strip(message):
            message["data"].pop("settings", None)
            return message

        with mock.patch.object(protocol, "hello_msg", lambda *a, **k: strip(older_hello(*a, **k))), \
                mock.patch.object(protocol, "welcome_msg", lambda *a, **k: strip(older_welcome(*a, **k))):
            self.both_links()
            self.assertTrue(wait_for(lambda: self.duo.mac_server.peer_settings is False))
        self.assertIs(self.duo.mac.peer_settings, False)
        self.assertIs(self.duo.pc.peer_settings, False)

    def test_the_arrangement_is_announced_when_a_link_comes_up(self):
        # G1: a change made while nobody was connected reaches the other end on the next link.
        self.duo.mac.announce = lambda: [protocol.arrangement_msg("top", 300), protocol.settings_msg(self.mac.state())]
        self.duo.listen().link_mac_to_pc()
        self.assertTrue(wait_for(lambda: ("top", 300) in self.duo.pc_arrangements))


if __name__ == "__main__":
    unittest.main()
