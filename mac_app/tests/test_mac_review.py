"""What the cold review of the Mac controller found, each shown failing before it was fixed."""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import bridge
from bridge_fakes import PAIRED_TOKEN, FakeClipboard, FakeClock, FakeQuartz, make_config, quiet_logger
from core import protocol
from fake_link import OTHER_ID, PEER_ID, FakeLink, bring_up
from settings_store import SettingsStore


class Base(unittest.TestCase):
    def setUp(self):
        FakeQuartz.reset_cursor_spies()
        self.clock = FakeClock()
        self.clipboard = FakeClipboard("hello")
        self.made = []

        def factory(*args, **kwargs):
            link = FakeLink(*args, **kwargs)
            self.made.append(link)
            return link

        self.controller = bridge.KVMController(
            make_config(PAIRED_TOKEN), logger=quiet_logger(), quartz=FakeQuartz, clock=self.clock,
            clipboard=self.clipboard, link_factory=factory, desktop_bounds=lambda: (0, 0, 1920, 1080),
        )
        self.alerts = []
        self.controller.on_user_alert = lambda title, message: self.alerts.append(message)
        self.link = self.controller._primary_link
        self.own = protocol.id_text(self.controller.identity()["id"])

    def tap(self, kind, keycode=0x00, character="a"):
        event = {FakeQuartz.kCGKeyboardEventKeycode: keycode, FakeQuartz.kCGKeyboardEventAutorepeat: 0, "unicode": character}
        return self.controller._event_tap_callback(None, kind, event, None)

    def drain(self):
        while not self.controller.outbound.empty():
            self.controller._process_outbound(self.controller.outbound.get_nowait())

    def take(self):
        self.controller.set_redirecting(True)
        self.link.arrives(self.controller, protocol.accept_msg(self.controller.owner.route))


class StartupTests(unittest.TestCase):
    def test_unreadable_settings_start_from_the_defaults_instead_of_crashing(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "settings.json").write_text("{ not json")
        store = SettingsStore(folder / "settings.json")
        book, identity = bridge.links_from_store(store, "1.5.0")
        self.assertIsNone(book)
        self.assertIsNone(identity)

    def test_readable_settings_give_the_book_and_the_identity(self):
        store = SettingsStore(Path(tempfile.mkdtemp()) / "settings.json")
        book, identity = bridge.links_from_store(store, "1.5.0-beta.2")
        self.assertEqual(identity()["app"], "1.5.0-beta.2")
        self.assertEqual(identity()["platform"], "macos")
        self.assertEqual(book.peers(), [])


class ReleasesAtHomeTests(Base):
    def test_a_key_held_when_input_comes_home_does_not_swallow_its_next_press(self):
        bring_up(self.controller)
        self.take()
        self.tap(FakeQuartz.kCGEventKeyDown)
        self.drain()
        self.link.arrives(self.controller, protocol.switch_v6(1, protocol.read_id(self.own)))
        self.assertFalse(self.controller.redirecting)
        self.tap(FakeQuartz.kCGEventKeyUp)
        self.drain()
        self.link.sent.clear()
        self.take()
        self.tap(FakeQuartz.kCGEventKeyDown)
        self.drain()
        self.assertEqual([m["type"] for m in self.link.sent], [protocol.MSG_KEYDOWN])

    def test_a_stale_home_press_does_not_swallow_the_release_of_a_key_pressed_while_away(self):
        bring_up(self.controller)
        self.tap(FakeQuartz.kCGEventKeyDown)
        self.controller.receiving = True
        self.tap(FakeQuartz.kCGEventKeyUp)
        self.controller.receiving = False
        self.take()
        self.tap(FakeQuartz.kCGEventKeyDown)
        self.tap(FakeQuartz.kCGEventKeyUp)
        self.drain()
        self.assertEqual([m["type"] for m in self.link.sent], [protocol.MSG_KEYDOWN, protocol.MSG_KEYUP])

    def test_a_press_that_reaches_the_owner_after_it_came_home_is_not_remembered(self):
        bring_up(self.controller)
        self.take()
        self.tap(FakeQuartz.kCGEventKeyDown)
        self.link.arrives(self.controller, protocol.switch_v6(1, protocol.read_id(self.own)))
        self.drain()
        self.assertEqual(self.controller.owner._here, set())


class LinkSetRaceTests(Base):
    def test_concurrent_syncs_make_one_link_for_one_peer(self):
        self.controller.started = True
        self.controller.links.clear()
        self.made.clear()
        threads = [threading.Thread(target=self.controller._sync_links) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.made), 1)
        self.assertEqual(len(self.controller.links), 1)


class ArrangementOriginTests(Base):
    def test_an_arrangement_from_a_peer_that_is_not_the_first_is_not_applied_to_the_first(self):
        seen = []
        self.controller.on_arrangement = lambda *args: seen.append(args)
        with self.controller.book.lock:
            peers = self.controller.book.data["peers"]
            peers[0]["id"] = protocol.id_text(PEER_ID)
            peers.append({**peers[0], "id": protocol.id_text(OTHER_ID), "token": "second", "send": False})
        bring_up(self.controller)
        message = protocol.arrangement_v6("right", int(time.time()) - 5, OTHER_ID)
        self.controller._handle_arrangement(message, protocol.id_text(OTHER_ID))
        self.assertEqual(seen, [])
        self.controller._handle_arrangement(message, protocol.id_text(PEER_ID))
        self.assertEqual(len(seen), 1)


class LetGoWaitTests(Base):
    def test_a_take_waiting_for_the_owner_to_let_go_is_cancelled_by_coming_home(self):
        bring_up(self.controller)
        self.controller.send_peer_home = lambda: True
        self.controller.set_receiving(True)
        self.assertTrue(self.controller.set_redirecting(True))
        self.controller.set_redirecting(False)
        self.controller.set_receiving(False)
        time.sleep(0.4)
        self.assertFalse(self.controller.redirecting)


class FailureTests(Base):
    def test_coming_home_cleans_up_the_pointer_even_when_a_send_raises(self):
        bring_up(self.controller)
        self.take()

        def broken(message):
            raise RuntimeError("synthetic")

        self.link.post = broken
        self.link.send_input = broken
        self.controller.set_redirecting(False)
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(FakeQuartz.associate_calls[-1], True)

    def test_an_exception_in_the_owner_tick_does_not_end_the_outbound_worker(self):
        original = self.controller._owner_tick
        calls = []

        def broken():
            calls.append(1)
            raise RuntimeError("synthetic")

        self.controller._owner_tick = broken
        thread = threading.Thread(target=self.controller._outbound_worker, daemon=True)
        thread.start()
        time.sleep(0.3)
        self.assertTrue(thread.is_alive())
        self.assertGreater(len(calls), 1)
        self.controller.stop_event.set()
        thread.join(2)


class CapabilityAndPairedTests(Base):
    def test_settings_from_a_peer_that_does_not_keep_them_are_ignored(self):
        seen = []
        self.controller.on_settings = lambda data, peer: seen.append(data)
        bring_up(self.controller, caps=("clipboard",))
        self.link.arrives(self.controller, protocol.settings_msg({"on": True, "set_at": 1}))
        self.assertEqual(seen, [])

    def test_a_changed_list_of_peers_is_announced_again(self):
        bring_up(self.controller)
        self.link.posted.clear()
        with self.controller.book.lock:
            peers = self.controller.book.data["peers"]
            peers.append({**peers[0], "id": protocol.id_text(OTHER_ID), "token": "second", "send": False})
        self.controller._announce_changes()
        self.assertIn(protocol.MSG_PAIRED, self.link.types())
        self.link.posted.clear()
        self.controller._announce_changes()
        self.assertNotIn(protocol.MSG_PAIRED, self.link.types())

    def test_a_peers_paired_list_is_stored_with_its_entry(self):
        with self.controller.book.lock:
            self.controller.book.data["peers"][0]["id"] = protocol.id_text(PEER_ID)
        bring_up(self.controller)
        self.link.arrives(self.controller, protocol.paired_msg([OTHER_ID]))
        self.assertEqual(self.controller.book.peers()[0]["paired_with"], [protocol.id_text(OTHER_ID)])


class WrongLinkTests(Base):
    def test_a_failure_drops_the_link_input_was_on_not_the_first(self):
        second = FakeLink("second", self.controller.book, self.controller.identity)
        self.controller.links["second"] = second
        second.up(self.controller, ident=OTHER_ID, name="Second")
        bring_up(self.controller)
        self.controller.owner.on = protocol.id_text(OTHER_ID)
        self.controller._connection_failed("synthetic")
        self.assertEqual(second.dropped, ["synthetic"])
        self.assertEqual(self.link.dropped, [])


if __name__ == "__main__":
    unittest.main()
