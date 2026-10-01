"""The Mac's real controller, with its real links, against a real LinkResponder on loopback
(core/tests/responder_harness.Machine, standing in for a PC): the whole path from a captured
event to the other machine's injector, and back. Only the platform edges are faked."""

import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from bridge import KVMController
from core import protocol
from core.tests import responder_harness as harness
from bridge_fakes import PAIRED_TOKEN, FakeClipboard, FakeQuartz, quiet_logger
from settings_store import SettingsStore, config_to_raw
from windows_input import WindowsInput

wait_for = harness.wait_for
PEER = harness.B
HERE = harness.HERE


def _accepting(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


class Wire(unittest.TestCase):
    """A Mac (the controller) with one peer entry for the machine `Machine` stands for."""

    def setUp(self):
        FakeQuartz.reset_cursor_spies()
        self.machine = harness.Machine([harness.entry(PEER, "Mac", token=PAIRED_TOKEN, platform="macos")])
        self.machine.start()
        self.addCleanup(self.machine.stop)
        folder = Path(tempfile.mkdtemp())
        self.store = SettingsStore(folder / "settings.json")
        cfg = self.store.load()
        raw = {**config_to_raw(cfg), "host": "127.0.0.1", "port": self.machine.port, "auth_token": PAIRED_TOKEN, "pc_name": "Far PC"}
        cfg = self.store.save(raw)
        self.settings_machine_id = self.store.current()["machine_id"]
        # The far machine knows this Mac by the id the Mac made for itself.
        self.machine.settings.peer(PAIRED_TOKEN)["id"] = self.settings_machine_id
        self.clipboard = FakeClipboard("copied here")
        self.alerts = []
        self.controller = KVMController(
            cfg, logger=quiet_logger(), quartz=FakeQuartz, clipboard=self.clipboard,
            book=self.store.book(), identity=self.identity, desktop_bounds=lambda: (0, 0, 1920, 1080),
        )
        self.controller.on_user_alert = lambda title, message: self.alerts.append(message)
        self.controller.start()
        self.addCleanup(self.controller.stop)

    def identity(self):
        return {"id": protocol.read_id(self.store.current()["machine_id"]), "name": "This Mac", "platform": "macos",
                "app": "1.5.0", "caps": ["clipboard", "clipboard_image", "gestures", "media_keys", "text", "settings"],
                "port": 24820}

    def connected(self):
        return wait_for(lambda: self.controller.connected and self.controller._peers_up, 5.0)

    def key_event(self, kind, keycode, character=""):
        event = {FakeQuartz.kCGKeyboardEventKeycode: keycode, FakeQuartz.kCGKeyboardEventAutorepeat: 0, "unicode": character}
        return self.controller._event_tap_callback(None, kind, event, None)

    def flags_event(self, keycode, flags):
        return self.controller._event_tap_callback(None, FakeQuartz.kCGEventFlagsChanged, {FakeQuartz.kCGKeyboardEventKeycode: keycode, "flags": flags}, None)


class TakingTheFarMachineTests(Wire):
    def test_a_take_makes_this_mac_the_owner_and_its_input_is_injected_there(self):
        self.assertTrue(self.connected())
        self.assertTrue(self.controller.set_redirecting(True, edge="left", offset=0.5))
        self.assertTrue(wait_for(lambda: self.machine.responder.owner == protocol.read_id(self.settings_machine_id)), self.machine.owners)
        self.assertTrue(wait_for(lambda: ("move", 0, 0) not in self.machine.injected() and self.machine.arrivals))
        self.key_event(FakeQuartz.kCGEventKeyDown, 0x00, "a")
        self.assertTrue(wait_for(lambda: ("key", "a", True) in self.machine.injected()), self.machine.injected())
        self.assertEqual(self.machine.clipboard.text, "copied here")

    def test_command_arrives_as_control_on_a_pc_and_stays_command_on_a_mac(self):
        self.assertTrue(self.connected())
        self.controller.set_redirecting(True)
        self.assertTrue(wait_for(lambda: self.machine.responder.owner is not None))
        command = FakeQuartz.kCGEventFlagMaskCommand
        self.flags_event(0x37, command)
        self.assertTrue(wait_for(lambda: ("key", "ctrl", True) in self.machine.injected()), self.machine.injected())

    def test_going_home_releases_the_keys_then_the_far_machine_is_let_go(self):
        self.assertTrue(self.connected())
        self.controller.set_redirecting(True)
        self.key_event(FakeQuartz.kCGEventKeyDown, 0x00, "a")
        self.assertTrue(wait_for(lambda: ("key", "a", True) in self.machine.injected()))
        self.controller.set_redirecting(False)
        self.assertTrue(wait_for(lambda: self.machine.responder.owner is None))
        injected = self.machine.injected()
        self.assertLess(injected.index(("key", "a", True)), injected.index(("key", "a", False)))
        self.assertFalse(self.controller.redirecting)

    def test_a_far_machine_that_is_driving_another_refuses_and_the_input_comes_home(self):
        self.machine.away = True
        self.assertTrue(self.connected())
        self.controller.set_redirecting(True)
        self.assertTrue(wait_for(lambda: not self.controller.redirecting), self.alerts)
        self.assertIn("is driving another machine", self.alerts[-1])

    def test_the_far_machine_sending_input_home_brings_it_back_to_the_edge_it_left(self):
        self.assertTrue(self.connected())
        self.controller.set_redirecting(True, edge="left", offset=0.5)
        self.assertTrue(wait_for(lambda: self.machine.responder.owner is not None))
        self.assertTrue(self.machine.responder.send_home())
        self.assertTrue(wait_for(lambda: not self.controller.redirecting), "the Mac did not come home")
        self.assertTrue(wait_for(lambda: self.machine.responder.owner is None))

    def test_the_far_machine_going_away_brings_input_home_with_a_reason(self):
        self.assertTrue(self.connected())
        self.controller.set_redirecting(True)
        self.assertTrue(wait_for(lambda: self.machine.responder.owner is not None))
        self.machine.responder.stop()
        self.assertTrue(wait_for(lambda: not self.controller.redirecting, 5.0), self.alerts)
        self.assertTrue(any("Lost the link" in text or "closed" in text for text in self.alerts), self.alerts)

    def test_a_pairing_that_does_not_let_this_mac_drive_is_not_taken(self):
        self.machine.settings.peer(PAIRED_TOKEN)["allow_drive"] = False
        self.assertTrue(self.connected())
        self.controller.set_redirecting(True)
        self.assertFalse(self.controller.redirecting)
        self.assertIn("does not accept input from this Mac", self.alerts[-1])

    def test_the_far_machines_clipboard_follows_the_input_home(self):
        self.assertTrue(self.connected())
        self.controller.set_redirecting(True)
        self.assertTrue(wait_for(lambda: self.machine.responder.owner is not None))
        # The take's own clipboard first, read on its own thread: a copy there before it lands is
        # overwritten by it, as one made by input sent there could not be (input waits behind it).
        self.assertTrue(wait_for(lambda: self.machine.clipboard.set_calls))
        self.machine.clipboard.copy("copied there")
        self.controller.set_redirecting(False)
        self.assertTrue(wait_for(lambda: ("copied there", None) in self.clipboard.set_calls), self.clipboard.set_calls)


class DrivenMacTests(unittest.TestCase):
    """This Mac as the driven machine: its real WindowsInput (a LinkResponder) with a scripted
    initiator, and the injector, desktop and clipboard faked at the platform edge."""

    def setUp(self):
        FakeQuartz.reset_cursor_spies()
        folder = Path(tempfile.mkdtemp())
        self.store = SettingsStore(folder / "settings.json")
        cfg = self.store.load()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        cfg = self.store.save({**config_to_raw(cfg), "host": "127.0.0.1", "port": port, "auth_token": PAIRED_TOKEN, "pc_name": "Initiator"})
        settings = self.store.current()
        settings["peers"][0]["id"] = protocol.id_text(PEER)
        settings["peers"][0]["send"] = False
        self.store.save_settings(settings)
        self.controller = KVMController(
            cfg, logger=quiet_logger(), quartz=FakeQuartz, clipboard=FakeClipboard(),
            book=self.store.book(), identity=lambda: {
                "id": protocol.read_id(self.store.current()["machine_id"]), "name": "This Mac", "platform": "macos",
                "app": "1.5.0", "caps": ["clipboard", "text", "settings"], "port": port},
            desktop_bounds=lambda: (0, 0, 1920, 1080),
        )
        self.input = WindowsInput(self.controller)
        self.injector = harness.FakeInjector()
        self.input.server._injector = self.injector
        self.input.server._desktop = harness.FakeDesktop()
        self.input.server._clipboard = harness.FakeClipboard()
        self.input.sync(cfg)
        self.addCleanup(self.input.stop)
        self.addCleanup(self.controller.stop)
        self.assertTrue(harness.wait_for(lambda: self.input.server.listening and _accepting(port)), "the Mac did not listen")
        self.machine = type("Far", (), {"port": port})()
        self.initiator = harness.Initiator(self.machine, peer=PEER, key=PAIRED_TOKEN)
        self.addCleanup(self.initiator.close)
        self.mac_id = protocol.read_id(self.store.current()["machine_id"])

    def handshake(self):
        return self.initiator.handshake(self.initiator.hello(platform="windows"))

    def test_a_peer_takes_the_mac_and_its_input_is_injected_and_the_controller_knows(self):
        welcome = self.handshake()
        self.assertEqual(welcome["id"], self.mac_id)
        self.initiator.send(protocol.focus_v6(1, self.mac_id, resistance_px=120, reach=[]))
        self.assertEqual(self.initiator.answer(1)[0], protocol.MSG_ACCEPT)
        self.assertTrue(harness.wait_for(lambda: self.controller.receiving))
        self.initiator.key("keydown", "a")
        self.assertTrue(harness.wait_for(lambda: ("key", "a", True) in self.injector.seen()), self.injector.seen())

    def test_this_mac_asked_to_drive_sends_its_owner_home_and_goes_when_it_has_let_go(self):
        self.handshake()
        self.initiator.send(protocol.focus_v6(1, self.mac_id, resistance_px=120, reach=[]))
        self.assertEqual(self.initiator.answer(1)[0], protocol.MSG_ACCEPT)
        self.assertTrue(harness.wait_for(lambda: self.controller.receiving))
        self.assertTrue(self.controller.send_peer_home())
        switch = self.initiator.expect(protocol.MSG_SWITCH)
        self.assertEqual(switch["next"], protocol.id_text(PEER))
        self.assertEqual(switch["route"], 1)
        self.initiator.let_go()
        self.assertTrue(harness.wait_for(lambda: not self.controller.receiving))

    def test_an_owner_that_ignores_the_send_home_is_ended_by_force_after_a_second(self):
        self.handshake()
        self.initiator.send(protocol.focus_v6(1, self.mac_id, resistance_px=120, reach=[]))
        self.assertEqual(self.initiator.answer(1)[0], protocol.MSG_ACCEPT)
        self.assertTrue(harness.wait_for(lambda: self.controller.receiving))
        self.controller.send_peer_home()
        refused = self.initiator.expect(protocol.MSG_REFUSE, timeout=3.0)
        self.assertEqual(refused["why"], "sent_home")
        self.assertTrue(harness.wait_for(lambda: not self.controller.receiving))

    def test_a_take_is_refused_busy_while_this_macs_own_input_is_away(self):
        self.handshake()
        self.controller.owner.on = "somewhere"
        self.initiator.send(protocol.focus_v6(1, self.mac_id, resistance_px=120, reach=[]))
        answer = self.initiator.answer(1)
        self.assertEqual(answer[0], protocol.MSG_REFUSE)
        self.assertEqual(answer[1]["why"], "busy")
        self.assertFalse(self.controller.receiving)


if __name__ == "__main__":
    unittest.main()
