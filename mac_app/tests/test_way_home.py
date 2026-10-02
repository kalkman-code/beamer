"""The way home for a machine driving this Mac: the zones this Mac's own settings carry, armed by
the responder while that machine's input is here. settings_store writes them from the Crossing
page's choices, core.receiver.zone_models turns them into the pointer models, and
WindowsInput.sync rebuilds them when a setting changes."""

import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import settings_store
from bridge import KVMController
from bridge_fakes import PAIRED_TOKEN, FakeClipboard, FakeQuartz, quiet_logger
from core import protocol, receiver, return_edge
from core.tests import responder_harness as harness
from windows_input import WindowsInput

PEER = harness.B
PEER_TEXT = protocol.id_text(PEER)
NOTCH = (656.0, 856.0)


def zones(methods, parts=("middle",), corner="top_right"):
    crossing = {"methods": list(methods), "edge_parts": list(parts), "corner": corner}
    return settings_store._apply_zones([], PEER_TEXT, crossing)


def models(methods, parts=("middle",), corner="top_right", edge="right", notch=lambda: NOTCH, allowed=(PEER,)):
    peers = [{"id": PEER_TEXT, "side": edge}]
    return [model for _, model in receiver.zone_models(zones(methods, parts, corner), peers, set(allowed), 120, notch)]


def crosses_at(armed, x):
    """Whether a hard push up at `x` along the top of a 1512x982 display goes home."""
    display = [return_edge.Rect(0, 0, 1512, 982)]
    return any(model.feed(display, (x, 0), 0, -40).action == return_edge.CROSS for model in armed for _ in range(20))


class WayHomeFollowsThisMacTests(unittest.TestCase):
    """The PC's pointer comes home through this Mac's edge as this Mac's own settings say."""

    def test_part_of_the_edge_arms_only_its_thirds(self):
        armed = models(["part", "shortcut"], parts=("start",))
        self.assertEqual([type(model) for model in armed], [return_edge.PartEdge])
        self.assertEqual(armed[0].parts, frozenset({"start"}))
        self.assertEqual(armed[0].edge, "right")

    def test_the_whole_edge(self):
        armed = models(["edge"])
        self.assertEqual([type(model) for model in armed], [return_edge.ReturnEdge])
        self.assertEqual(armed[0].edge, "right")
        self.assertEqual(armed[0].resistance_px, 120)

    def test_the_whole_edge_stands_in_for_its_thirds(self):
        self.assertEqual([type(model) for model in models(["edge", "part"])], [return_edge.ReturnEdge])

    def test_the_notch_arms_only_the_notch(self):
        # Reported 28-09-2026: with only the notch and the shortcut on, the PC's pointer went home
        # anywhere along the Mac's top edge.
        armed = models(["notch", "shortcut"], edge="top")
        self.assertEqual([type(model) for model in armed], [return_edge.SpanEdge])
        self.assertTrue(crosses_at(armed, 756))
        self.assertFalse(crosses_at(models(["notch", "shortcut"], edge="top"), 200))
        self.assertFalse(crosses_at(models(["notch", "shortcut"], edge="top"), 1300))

    def test_the_notch_crosses_the_top_edge_whichever_side_the_pc_is_on(self):
        # WIRE.md section 8: a notch zone is the Mac's notch and crosses the top edge. 1.4.x left
        # no way home by the pointer when the PC faced another edge.
        armed = models(["notch", "shortcut"], edge="right")
        self.assertEqual([(type(model), model.edge) for model in armed], [(return_edge.SpanEdge, "top")])

    def test_without_a_measured_notch_there_is_no_notch_zone(self):
        self.assertEqual(models(["notch", "shortcut"], edge="top", notch=None), [])

    def test_the_notch_is_read_at_each_push(self):
        # A focus that arrives before the first measurement, or across a display change, must not
        # leave the pointer without its way home for the rest of the session.
        held = {"span": None}
        armed = models(["notch", "shortcut"], edge="top", notch=lambda: held["span"])
        self.assertFalse(crosses_at(armed, 756))
        held["span"] = NOTCH
        self.assertTrue(crosses_at(armed, 756))

    def test_the_whole_edge_still_wins_over_the_notch(self):
        armed = models(["edge", "notch"], edge="top")
        self.assertEqual([type(model) for model in armed], [return_edge.ReturnEdge, return_edge.SpanEdge])
        self.assertTrue(crosses_at(armed[:1], 200))

    def test_a_corner_is_the_corner_push_on_the_edge_it_touches(self):
        # 1.4.x armed a corner only when it sat on the edge facing the PC. A corner zone now names
        # its own edge (WIRE.md section 8: a Mac's crosses by the corner's own left or right edge,
        # whatever the side), so it is armed whichever side the PC is on.
        for corner, edge in (("top_right", "right"), ("top_left", "left"), ("bottom_right", "right")):
            with self.subTest(corner=corner):
                armed = models(["corner"], corner=corner, edge="right")
                self.assertEqual([type(model) for model in armed], [return_edge.CornerPush])
                self.assertEqual((armed[0].corner, armed[0].edge), (corner, edge))

    def test_a_corner_is_checked_before_the_edge_it_sits_in(self):
        armed = models(["corner", "edge"])
        self.assertEqual([type(model) for model in armed], [return_edge.CornerPush, return_edge.ReturnEdge])

    def test_the_shortcut_alone_leaves_no_way_home_by_the_pointer(self):
        self.assertEqual(models(["shortcut"]), [])

    def test_a_machine_that_is_not_driving_has_no_zones_armed(self):
        self.assertEqual(models(["edge", "part", "corner", "notch"], allowed=()), [])

    def test_a_peer_with_no_side_has_no_edge_or_thirds_but_keeps_its_corner_and_notch(self):
        armed = models(["edge", "corner", "notch"], edge="")
        self.assertEqual([type(model) for model in armed], [return_edge.CornerPush, return_edge.SpanEdge])


class SyncRearmsTheZonesTests(unittest.TestCase):
    def test_a_sync_tells_the_responder_to_rebuild_the_zones_only_when_the_peers_or_zones_changed(self):
        controller = KVMController(
            settings_store.editable_default_config(), logger=quiet_logger(), quartz=FakeQuartz, clipboard=FakeClipboard(),
            desktop_bounds=lambda: (0, 0, 1920, 1080),
        )
        wire = WindowsInput(controller)
        self.addCleanup(wire.server.stop)
        wire.server = mock.Mock(input_scale=None, listening=True)
        cfg = controller.cfg
        wire.sync(cfg)
        wire.server.start.assert_called_once_with(cfg.port)
        wire.server.rearm.assert_not_called()
        wire.sync(cfg)
        wire.sync(cfg)
        wire.server.rearm.assert_not_called()
        with controller.book.lock:
            controller.book.data["zones"] = [{"peer": "", "kind": "edge"}]
        wire.sync(cfg)
        wire.sync(cfg)
        self.assertEqual(wire.server.rearm.call_count, 1)
        self.assertEqual(wire.server.peers_changed.call_count, 1)


class ChangesReachTheArmedZonesTests(unittest.TestCase):
    """A real responder with a peer driving this Mac: the zones armed for it follow the settings."""

    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.store = settings_store.SettingsStore(folder / "settings.json")
        cfg = self.store.load()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.cfg = self.store.save({**settings_store.config_to_raw(cfg), "host": "127.0.0.1", "port": port,
                                    "auth_token": PAIRED_TOKEN, "pc_name": "Driver"})
        self.set_crossing(["edge", "shortcut"], ("middle",))
        self.controller = KVMController(
            self.cfg, logger=quiet_logger(), quartz=FakeQuartz, clipboard=FakeClipboard(), book=self.store.book(),
            identity=lambda: {"id": protocol.read_id(self.store.current()["machine_id"]), "name": "This Mac",
                              "platform": "macos", "app": "1.5.0", "caps": ["clipboard", "text", "settings"], "port": port},
            desktop_bounds=lambda: (0, 0, 1920, 1080),
        )
        self.wire = WindowsInput(self.controller)
        self.wire.server._injector = harness.FakeInjector()
        self.wire.server._desktop = harness.FakeDesktop()
        self.wire.server._clipboard = harness.FakeClipboard()
        self.wire.sync(self.cfg)
        self.addCleanup(self.wire.stop)
        self.addCleanup(self.controller.stop)
        def accepting():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return True
            except OSError:
                return False

        self.assertTrue(harness.wait_for(accepting), "the Mac did not listen")
        self.driver = harness.Initiator(type("Far", (), {"port": port})(), peer=PEER, key=PAIRED_TOKEN)
        self.addCleanup(self.driver.close)
        self.mac_id = protocol.read_id(self.store.current()["machine_id"])
        self.driver.handshake(self.driver.hello(platform="windows"))
        self.driver.send(protocol.focus_v6(1, self.mac_id, resistance_px=120, reach=[]))
        self.assertEqual(self.driver.answer(1)[0], protocol.MSG_ACCEPT)
        self.assertTrue(harness.wait_for(lambda: self.controller.receiving))

    def set_crossing(self, methods, parts, corner="top_right"):
        settings = self.store.current()
        settings["peers"][0].update(id=PEER_TEXT, side="right", send=False, from_1_4=False)
        settings["zones"] = zones(methods, parts, corner)
        self.store.save_settings(settings)

    def armed(self):
        return [model for _, model in self.wire.server._armed]

    def test_the_zones_armed_are_the_ones_the_settings_carry(self):
        self.assertEqual([type(model) for model in self.armed()], [return_edge.ReturnEdge])

    def test_a_change_to_the_ways_applies_at_the_next_sync_while_the_peer_is_here(self):
        # Reported 28-09-2026: a change to Part of the edge applied only after crossing out and back.
        self.set_crossing(["part", "shortcut"], ("start",))
        self.wire.sync(self.cfg)
        armed = self.armed()
        self.assertEqual([type(model) for model in armed], [return_edge.PartEdge])
        self.assertEqual(armed[0].parts, frozenset({"start"}))
        self.set_crossing(["part", "shortcut"], ("end",))
        self.wire.sync(self.cfg)
        self.assertEqual(self.armed()[0].parts, frozenset({"end"}))

    def push_right(self, dx):
        self.wire.server._desktop.cursor = (1919, 540)
        self.driver.move(dx, 0)
        time.sleep(0.1)

    def sent_home(self):
        return self.driver.expect(protocol.MSG_SWITCH, timeout=0.5) is not None

    def test_two_pushes_that_add_up_to_the_resistance_cross(self):
        self.push_right(80)
        self.push_right(80)
        self.assertTrue(self.sent_home())

    def test_a_sync_between_two_pushes_does_not_lose_the_first(self):
        # The status tick calls sync every 0.4 s: a rebuilt model starts with no pressure, so a
        # push in progress would lose its travel at each tick.
        self.push_right(80)
        self.wire.sync(self.cfg)
        self.push_right(80)
        self.assertTrue(self.sent_home())

if __name__ == "__main__":
    unittest.main()
