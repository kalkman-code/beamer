"""Same on all machines over loopback: the Mac's real controller and responder against a PC end
made of core's own link and responder, announcing on connect and carrying each change, with two
small ends holding each machine's state through settings_sync as the apps' windows do (apply_same on
the Mac, _on_settings on the PC). The Mac's state travels over its own link and over the PC's."""

import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_WIN = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "win_app")
if _WIN not in sys.path:
    sys.path.append(_WIN)

import app_config  # noqa: E402
from bridge import KVMController  # noqa: E402
from bridge_fakes import PAIRED_TOKEN, FakeClipboard, FakeQuartz, quiet_logger  # noqa: E402
from core import protocol, settings_sync  # noqa: E402
from core.link import OutboundLink  # noqa: E402
from core.tests import responder_harness as harness  # noqa: E402
from key_codes import KEY_NAME_TO_CODE  # noqa: E402
from settings_store import SettingsStore, config_to_raw  # noqa: E402
from windows_input import WindowsInput  # noqa: E402

wait_for = harness.wait_for
PC_ID = harness.HERE
WITHOUT_SETTINGS = [cap for cap in harness.CAPS if cap != "settings"]


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class MacEnd:
    """The Mac's state as the app holds it, and what its apply_same does with a message."""

    def __init__(self, rig):
        self.rig = rig
        self.raw = config_to_raw(rig.controller.cfg)
        self.raw["same_on_both"], self.raw["same_set_at"], self.raw["same_by"] = False, 0, ""
        self.lock = threading.Lock()
        self.taken = []

    def state(self):
        return settings_sync.message_data(self.raw["same_on_both"], self.raw["same_set_at"],
                                          settings_sync.mac_values(self.raw), by=self.raw["same_by"] or self.rig.mac_id_text)

    def take(self, data, peer=None):
        with self.lock:
            taken = settings_sync.arrived(data, self.raw["same_set_at"], KEY_NAME_TO_CODE, by_here=self.raw["same_by"])
            if taken is None:
                return
            on, set_at, values = taken
            raw = settings_sync.apply_mac(self.raw, values) if on else dict(self.raw)
            raw["same_on_both"], raw["same_set_at"], raw["same_by"] = on, set_at, data.get("by", "")
            self.raw = raw
            self.taken.append(data)
        self.send(data, source=peer)

    def send(self, data, source=None):
        self.rig.controller.send_settings(data, source=source)
        self.rig.input.send_settings(data, source=source)

    def set(self, on=None, at=0, **crossing):
        with self.lock:
            if on is not None:
                self.raw["same_on_both"] = on
            self.raw["crossing"] = {**self.raw["crossing"], **crossing}
            self.raw["same_set_at"], self.raw["same_by"] = at, self.rig.mac_id_text
        self.send(self.state())


class PCEnd:
    """The PC's state, as the PC window holds it, and what its _on_settings does with a message."""

    def __init__(self, rig):
        self.rig = rig
        self.config = app_config.Config(host="127.0.0.1", port=rig.machine.port, auth_token=PAIRED_TOKEN, mac_return_edge="left")
        self.by = ""
        self.lock = threading.Lock()
        self.taken = []
        self.heard = []

    def state(self):
        return settings_sync.message_data(self.config.same_on_both, self.config.same_set_at,
                                          settings_sync.pc_values(self.config), by=self.by or protocol.id_text(PC_ID))

    def take(self, data, peer=None):
        self.heard.append(data)
        with self.lock:
            taken = settings_sync.arrived(data, self.config.same_set_at, app_config.TRIGGER_KEYS, by_here=self.by)
            if taken is None:
                return
            on, set_at, values = taken
            if on:
                settings_sync.apply_pc(self.config, values)
            self.config.same_on_both, self.config.same_set_at, self.by = on, set_at, data.get("by", "")
            self.taken.append(data)

    def set(self, on=None, at=0, **fields):
        with self.lock:
            if on is not None:
                self.config.same_on_both = on
            for name, value in fields.items():
                setattr(self.config, name, value)
            self.config.same_set_at, self.by = at, protocol.id_text(PC_ID)
        data = self.state()
        self.rig.pc_link.post(protocol.settings_msg(data))
        self.rig.machine.responder.send(self.rig.mac_id, protocol.settings_msg(data))


class Announced(dict):
    """The PC's responder announcing its current state to whoever links in, not one frozen at build."""

    def __init__(self, rig):
        super().__init__()
        self.rig = rig

    def get(self, peer, default=None):
        return [protocol.settings_msg(self.rig.pc.state())]


class Feed(list):
    def __init__(self, rig):
        super().__init__()
        self.rig = rig

    def append(self, item):
        super().append(item)
        self.rig.pc.take(item[1], protocol.id_text(item[0]))


class Rig:
    """The Mac (controller, responder, settings.json) and a PC end, each linked to the other the way
    the apps do it: the Mac's controller dials the PC's responder, the PC's link dials the Mac's."""

    def __init__(self, test, pc_caps=harness.CAPS, side=None):
        folder = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, folder, True)
        self.store = SettingsStore(folder / "settings.json")
        cfg = self.store.load()
        self.mac_id_text = self.store.current()["machine_id"]
        self.mac_id = protocol.read_id(self.mac_id_text)
        self.mac_port = free_port()
        self.machine = harness.Machine(
            [harness.entry(self.mac_id_text, "Mac", token=PAIRED_TOKEN, platform="macos", host="127.0.0.1", port=self.mac_port)],
            caps=pc_caps,
        )
        self.machine.start()
        test.addCleanup(self.machine.stop)

        cfg = self.store.save({**config_to_raw(cfg), "host": "127.0.0.1", "port": self.machine.port,
                               "auth_token": PAIRED_TOKEN, "pc_name": "PC"})
        settings = self.store.current()
        settings["peers"][0].update(id=protocol.id_text(PC_ID), from_1_4=False)
        if side is not None:
            settings["peers"][0].update(side=side[0], side_set_at=side[1], side_by=self.mac_id_text)
        self.store.save_settings(settings)
        FakeQuartz.reset_cursor_spies()
        self.controller = KVMController(
            cfg, logger=quiet_logger(), quartz=FakeQuartz, clipboard=FakeClipboard(),
            book=self.store.book(), identity=self.identity, desktop_bounds=lambda: (0, 0, 1920, 1080),
        )
        test.addCleanup(self.controller.stop)
        self.input = WindowsInput(self.controller)
        self.input.server._injector = harness.FakeInjector()
        self.input.server._desktop = harness.FakeDesktop()
        self.input.server._clipboard = harness.FakeClipboard()
        test.addCleanup(self.input.stop)

        self.mac = MacEnd(self)
        self.pc = PCEnd(self)
        self.controller.announce = lambda: [protocol.settings_msg(self.mac.state())]
        self.controller.on_settings = self.mac.take
        self.input.settings_callback = self.mac.take
        self.machine.announcements = Announced(self)
        self.machine.settings_seen = Feed(self)

        self.pc_link = OutboundLink(
            PAIRED_TOKEN, self.machine.book, lambda: {"id": PC_ID, "name": "PC", "platform": "windows", "app": "1.5.0",
                                                      "caps": list(pc_caps), "port": self.machine.port},
            up=self._pc_link_up, message=lambda link, message: self._pc_link_message(message), reconnect_seconds=0.05,
        )
        test.addCleanup(self.pc_link.stop)
        self.input.sync(replace(cfg, port=self.mac_port))

    def identity(self):
        return {"id": self.mac_id, "name": "This Mac", "platform": "macos", "app": "1.5.0", "caps": list(harness.CAPS), "port": self.mac_port}

    def _pc_link_up(self, link, fields):
        if "settings" in link.caps:
            link.post(protocol.settings_msg(self.pc.state()))

    def _pc_link_message(self, message):
        if message.get("type") == protocol.MSG_SETTINGS:
            self.pc.take(message["data"], protocol.id_text(self.mac_id))

    def link_mac_to_pc(self):
        """The Mac's own link to the PC, up before this returns."""
        self.controller.start()
        assert wait_for(lambda: self.controller.connected and self.controller._peers_up, 5.0), self.controller.connection_status
        return self

    def link_pc_to_mac(self):
        """The PC's link to the Mac, up before this returns."""
        self.pc_link.start()
        assert wait_for(lambda: self.pc_link.live() and self.input.server.links(), 5.0), self.pc_link.status
        return self

    def both_links(self):
        return self.link_mac_to_pc().link_pc_to_mac()


class SettingsOverLoopbackTests(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(self)
        self.mac, self.pc = self.rig.mac, self.rig.pc

    def test_turning_it_on_carries_this_machines_values_across(self):
        self.rig.both_links()
        self.mac.set(on=True, at=100, glow_style="beam", resistance_px=60)
        self.assertTrue(wait_for(lambda: self.pc.config.same_on_both))
        self.assertEqual((self.pc.config.glow_style, self.pc.config.crossing_resistance_px), ("beam", 60))
        self.assertEqual(self.pc.config.same_set_at, 100)
        self.assertEqual(self.pc.by, self.rig.mac_id_text)

    def test_a_change_on_either_machine_reaches_the_other_while_on(self):
        self.rig.both_links()
        self.mac.set(on=True, at=100)
        self.assertTrue(wait_for(lambda: self.pc.config.same_on_both))
        self.pc.set(at=101, effect_length="long", crossing_methods=["edge"])
        self.assertTrue(wait_for(lambda: self.mac.raw["crossing"]["effect_length"] == "long"))
        # The shortcut went with the PC's; the notch is the Mac's own way in and stays.
        self.assertNotIn("shortcut", self.mac.raw["crossing"]["methods"])
        self.assertEqual("notch" in self.mac.raw["crossing"]["methods"], "notch" in self.rig.controller.cfg.crossing["methods"])
        self.assertEqual(self.mac.raw["same_by"], protocol.id_text(PC_ID))
        self.mac.set(at=102, glow_colour="ocean")
        self.assertTrue(wait_for(lambda: self.pc.config.glow_colour == "ocean"))

    def test_the_newer_end_wins_when_a_link_comes_up(self):
        # Changed on the PC while the Mac was away: the PC's is newer, and the Mac takes it the
        # moment its own link connects, from the PC responder's announcement.
        self.pc.config.same_on_both, self.pc.config.same_set_at = True, 500
        self.pc.config.glow_style = "beam"
        self.mac.raw["same_on_both"], self.mac.raw["same_set_at"] = True, 400
        self.rig.link_mac_to_pc()
        self.assertTrue(wait_for(lambda: self.mac.raw["same_set_at"] == 500))
        self.assertEqual(self.mac.raw["crossing"]["glow_style"], "beam")
        # And the older end's own announcement changed nothing at the newer.
        self.assertEqual(self.pc.config.same_set_at, 500)

    def test_the_same_second_goes_to_the_larger_machine_id_at_both_ends(self):
        self.pc.config.same_on_both, self.pc.config.same_set_at, self.pc.by = True, 100, protocol.id_text(PC_ID)
        self.pc.config.glow_style = "beam"
        self.mac.raw["same_on_both"], self.mac.raw["same_set_at"], self.mac.raw["same_by"] = True, 100, self.rig.mac_id_text
        self.mac.raw["crossing"] = {**self.mac.raw["crossing"], "glow_style": "glow"}
        self.rig.both_links()
        winner = "beam" if PC_ID > self.rig.mac_id else "glow"
        self.assertTrue(wait_for(lambda: self.pc.config.glow_style == winner and self.mac.raw["crossing"]["glow_style"] == winner))
        time.sleep(0.2)
        self.assertEqual((self.pc.config.glow_style, self.mac.raw["crossing"]["glow_style"]), (winner, winner))

    def test_turning_it_off_turns_it_off_at_both_and_leaves_the_values(self):
        self.rig.both_links()
        self.mac.set(on=True, at=100, glow_style="beam")
        self.assertTrue(wait_for(lambda: self.pc.config.glow_style == "beam"))
        self.pc.set(on=False, at=200)
        self.assertTrue(wait_for(lambda: self.mac.raw["same_on_both"] is False))
        self.assertEqual(self.mac.raw["crossing"]["glow_style"], "beam")
        self.mac.raw["crossing"]["glow_style"] = "glow"
        self.assertEqual(self.pc.config.glow_style, "beam")

    def test_a_state_is_not_sent_back_to_the_machine_it_came_from(self):
        self.rig.both_links()
        self.pc.set(on=True, at=300, glow_style="beam")
        self.assertTrue(wait_for(lambda: self.mac.raw["same_set_at"] == 300))
        time.sleep(0.3)
        self.assertEqual([data for data in self.pc.heard if data["set_at"] == 300], [])

    def test_a_state_goes_to_the_peer_once_when_both_links_are_up(self):
        # Counted by a stamp only this send carries: the announcements as each link comes up can
        # land after links() shows it, so a count of everything heard cannot tell them apart.
        self.rig.both_links()
        self.mac.set(at=700)
        ours = lambda: [data for data in self.pc.heard if data["set_at"] == 700]
        self.assertTrue(wait_for(lambda: len(ours()) == 1))
        time.sleep(0.3)
        self.assertEqual(len(ours()), 1)

    def test_a_state_goes_over_the_pcs_link_when_the_mac_has_none_of_its_own(self):
        # The Mac's server also announces its state on this link as it comes up, a moment after
        # links() shows it, so the PC's count of messages heard cannot say which one is ours:
        # a stamp only this send carries does.
        self.rig.link_pc_to_mac()
        self.mac.set(at=700)
        self.assertTrue(wait_for(lambda: any(data["set_at"] == 700 for data in self.pc.heard)))

    def test_a_state_made_by_a_real_machine_id_is_taken(self):
        data = settings_sync.message_data(True, 100, settings_sync.mac_values(self.mac.raw), by=protocol.id_text(PC_ID))
        taken = settings_sync.arrived(data, 0, KEY_NAME_TO_CODE)
        self.assertIsNotNone(taken)

    def test_both_ends_know_a_peer_that_keeps_settings_in_step(self):
        self.rig.both_links()
        self.assertTrue(wait_for(lambda: self.rig.input.peer_settings is True))
        self.assertTrue(self.rig.controller.peer_settings)
        self.assertIn("settings", self.rig.pc_link.caps)
        self.assertTrue(wait_for(lambda: "settings" in (self.rig.machine.responder.caps_of(self.rig.mac_id) or ())))


class AnOlderPeerTests(unittest.TestCase):
    def test_a_peer_without_the_settings_capability_is_known_as_one_on_either_link(self):
        rig = Rig(self, pc_caps=WITHOUT_SETTINGS)
        rig.both_links()
        self.assertTrue(wait_for(lambda: rig.input.peer_settings is False))
        self.assertIs(rig.controller.peer_settings, False)

    def test_nothing_is_sent_to_a_peer_that_does_not_keep_settings(self):
        rig = Rig(self, pc_caps=WITHOUT_SETTINGS)
        rig.both_links()
        self.assertTrue(wait_for(lambda: rig.input.peer_settings is False))
        data = rig.mac.state()
        self.assertFalse(rig.controller.send_settings(data))
        self.assertFalse(rig.input.send_settings(data))
        self.assertEqual(rig.pc.heard, [])


class AnnouncedOnLinkTests(unittest.TestCase):
    def test_the_arrangement_is_announced_when_a_link_comes_up(self):
        # A change made while nobody was connected reaches the other end on the next link.
        rig = Rig(self, side=("top", 300))
        rig.link_mac_to_pc()
        self.assertTrue(wait_for(lambda: any(read["edge"] == "top" and read["set_at"] == 300 for _, read in rig.machine.arrangements)))


if __name__ == "__main__":
    unittest.main()
