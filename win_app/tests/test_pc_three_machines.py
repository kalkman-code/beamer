"""Three machines on the Windows app (1.5.0 milestone 5): the shortcut's machine, a tray item per
machine, each machine's ways written on its own, the Crossing page's choice of machine, the diagram
of this PC and its neighbours, and Pause counting every machine's zones (WIRE.md sections 5 and 8)."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from links_rig import B, C, Rig, edge_zone

import app_config
import pages_win
import sender as sender_module
from core import protocol
from core import ways
from core.tests import responder_harness as harness

try:
    from PySide6.QtWidgets import QApplication

    import diagram
    import kvm_bridge_win
    import theme
except ImportError:  # PySide6 is only in the Windows venv
    kvm_bridge_win = None

D = harness.D
BT, CT, DT = protocol.id_text(B), protocol.id_text(C), protocol.id_text(D)
HIGH_ID = bytes([200]) * 16
HIGH = protocol.id_text(HIGH_ID)


def target_of(message):
    return protocol.read_id(message["data"]["target"])


class ShortcutMachineTests(unittest.TestCase):
    """The shortcut, the Send button and the tray send input where core.ways.shortcut_peer says."""

    def rig(self, up, **entries):
        peers = [harness.entry(B, "Bee", side="left", **entries.get("b", {})),
                 harness.entry(C, "Sea", side="right", **entries.get("c", {}))]
        return Rig(entries=peers, zones=[edge_zone(B), edge_zone(C)], up=up)

    def test_with_the_first_machine_down_the_shortcut_takes_one_that_is_up(self):
        rig = self.rig(up=(C,))
        self.assertTrue(rig.sender.set_redirecting(True))
        self.assertEqual(rig.sender.owner, C)
        self.assertEqual(target_of(rig.sent(C, "focus")[0]), C)

    def test_the_shortcut_goes_back_to_the_machine_input_was_last_on(self):
        rig = self.rig(up=(B, C))
        rig.sender.go(C)
        rig.accept_take(C)
        rig.sender.set_redirecting(False)
        self.assertEqual(rig.sender.shortcut_target(), C)
        self.assertTrue(rig.sender.set_redirecting(True))
        self.assertEqual(rig.sender.owner, C)

    def test_a_machine_that_does_not_accept_input_is_passed_over(self):
        rig = self.rig(up=(C,))
        rig.bring_up(B, accepts=False)
        self.assertEqual(rig.sender.shortcut_target(), C)

    def test_a_machine_this_pc_does_not_send_to_is_never_picked(self):
        rig = self.rig(up=(B, C), b={"send": False})
        self.assertEqual(rig.sender.shortcut_target(), C)

    def test_with_every_machine_down_the_first_is_woken(self):
        rig = self.rig(up=(), b={"hw": "aa:bb:cc:dd:ee:ff"})
        woken = []
        rig.sender._wake_sender = lambda address, host: woken.append(address)
        self.addCleanup(rig.sender._stop_event.set)
        self.assertFalse(rig.sender.set_redirecting(True))
        self.assertTrue(rig.sender.is_waking(B))
        self.assertFalse(rig.sender.is_waking(C))

    def test_with_every_machine_down_and_none_to_wake_the_first_is_named(self):
        rig = self.rig(up=())
        rig.sender.on_status(rig.links.key_of(rig.settings.data["peers"][0]), False, "Bee is unreachable")
        self.assertFalse(rig.sender.set_redirecting(True))
        self.assertEqual(rig.alerts, ["Cannot switch — Bee is unreachable"])

    def test_the_status_is_the_shortcut_machines(self):
        rig = self.rig(up=(C,))
        rig.sender.on_status(rig.links.key_of(rig.settings.data["peers"][0]), False, "Bee is unreachable")
        rig.sender.on_status(rig.links.key_of(rig.settings.data["peers"][1]), True, "Connected to Sea")
        self.assertTrue(rig.sender.connected)
        self.assertEqual(rig.sender.status, "Connected to Sea")

    def test_the_status_does_not_name_a_machine_not_in_use(self):
        rig = self.rig(up=(), b={"in_use": False})
        rig.sender.on_status(rig.links.key_of(rig.settings.data["peers"][0]), False, "Bee is unreachable")
        self.assertEqual(rig.sender.status, sender_module.NOT_CONNECTED_STATUS)


class SetWaysTests(unittest.TestCase):
    """Each machine's side and zones are written one machine at a time, under the settings lock, and
    never by a flat save."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "settings.json"
        settings = app_config.migrate(None, machine_id=protocol.id_text(harness.HERE))
        settings["peers"] = [
            harness.entry(B, "Bee", side="left", side_set_at=100, side_by=HIGH),
            harness.entry(C, "Sea", platform="windows"),
        ]
        settings["zones"] = [{"peer": BT, "kind": "edge"}]
        app_config.write_settings(self.path, settings)

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def peer(self, ident):
        return next(item for item in self.saved()["peers"] if item["id"] == ident)

    def zones(self, ident):
        return {zone["kind"]: zone for zone in self.saved()["zones"] if zone["peer"] == ident}

    def test_reading_the_settings_gives_a_machine_with_no_zone_its_edge_and_writes_it(self):
        # The rig's beta.3 file of 01-10-2026: the laptop paired second, its side set, no zone at all.
        settings = self.saved()
        settings["peers"][1]["side"] = "right"
        app_config.write_settings(self.path, settings)
        loaded = app_config.load_settings(self.path)
        self.assertIn({"peer": CT, "kind": "edge"}, loaded["zones"])
        self.assertIn({"peer": CT, "kind": "edge"}, self.saved()["zones"])

    def test_one_whose_side_is_taken_gets_its_edge_off_and_the_file_still_reads(self):
        settings = self.saved()
        settings["peers"][1]["side"] = "left"
        app_config.write_settings(self.path, settings)
        loaded = app_config.load_settings(self.path)
        self.assertIn({"peer": CT, "kind": "edge", "off": True}, loaded["zones"])
        self.assertIn("Sea has no way in", ways.blocked_sentence(loaded, CT, "this PC"))

    def test_one_machines_side_and_zones_are_written_and_the_others_left_alone(self):
        moved = app_config.set_ways(self.path, CT, side="right", methods=["part", "corner"], parts=["end"], corner="bottom_right")
        self.assertTrue(moved)
        entry = self.peer(CT)
        self.assertEqual((entry["side"], entry["side_by"]), ("right", self.saved()["machine_id"]))
        self.assertGreater(entry["side_set_at"], 0)
        zones = self.zones(CT)
        self.assertEqual(zones["part"]["parts"], ["end"])
        self.assertNotIn("off", zones["part"])
        self.assertTrue(zones["edge"]["off"])
        self.assertEqual((zones["corner"]["corner"], zones["corner"]["edge"]), ("bottom_right", "right"))
        self.assertEqual(self.peer(BT)["side"], "left")
        self.assertEqual(self.zones(BT), {"edge": {"peer": BT, "kind": "edge"}})

    def test_an_unchanged_side_is_not_a_move(self):
        self.assertFalse(app_config.set_ways(self.path, BT, side="left", methods=["edge"], parts=["middle"], corner="top_left"))
        self.assertEqual(self.peer(BT)["side_set_at"], 100)

    def test_no_side_given_keeps_the_side_held_whatever_the_window_last_read(self):
        settings = self.saved()
        settings["peers"][0]["side"] = "top"
        app_config.write_settings(self.path, settings)
        self.assertFalse(app_config.set_ways(self.path, BT, methods=["edge", "corner"], parts=["middle"], corner="top_left"))
        self.assertEqual(self.peer(BT)["side"], "top")
        self.assertEqual(self.zones(BT)["corner"]["edge"], "top")

    def test_a_clash_is_refused_with_a_sentence_naming_both_and_nothing_is_written(self):
        # On Bee's side with no way in yet: turning Sea's edge on would cover Bee's.
        app_config.set_ways(self.path, CT, side="left", methods=[], parts=["middle"], corner="top_left")
        before = self.path.read_bytes()
        with self.assertRaises(app_config.ConfigError) as raised:
            app_config.set_ways(self.path, CT, side="left", methods=["edge"], parts=["middle"], corner="top_left")
        self.assertIn("Bee", str(raised.exception))
        self.assertIn("Sea", str(raised.exception))
        self.assertIn("this PC", str(raised.exception))
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_machine_that_is_not_paired_is_an_error(self):
        with self.assertRaises(app_config.ConfigError):
            app_config.set_ways(self.path, DT, side="left", methods=[], parts=["middle"], corner="top_left")

    def test_a_flat_save_never_writes_a_side_or_a_zone(self):
        stale = app_config.load_config(self.path)
        settings = self.saved()
        settings["peers"][0].update(side="top", side_set_at=300, side_by=HIGH)
        app_config.write_settings(self.path, settings)
        app_config.save_config(self.path, app_config.replace(
            stale, crossing_methods=["part"], crossing_edge_parts=["start"], mac_return_edge="bottom",
            arrangement_set_at=400, pointer_speed=2.0))
        saved = self.saved()
        self.assertEqual((saved["peers"][0]["side"], saved["peers"][0]["side_set_at"], saved["peers"][0]["side_by"]),
                         ("top", 300, HIGH))
        # Sea's edge is the repair every read makes for a machine paired with no zone, not the save's.
        self.assertEqual(saved["zones"], [{"peer": BT, "kind": "edge"}, {"peer": CT, "kind": "edge"}])
        self.assertFalse(saved["shortcut"])
        self.assertEqual(saved["pointer_speed"], 2.0)

    def test_a_flat_save_keeps_a_name_and_hardware_address_learnt_since_the_window_read(self):
        stale = app_config.load_config(self.path)
        settings = self.saved()
        settings["peers"][0].update(name="Bee Studio", hw="aa:bb:cc:dd:ee:01")
        app_config.write_settings(self.path, settings)
        app_config.save_config(self.path, app_config.replace(stale, pointer_speed=2.0))
        self.assertEqual((self.peer(BT)["name"], self.peer(BT)["hw"]), ("Bee Studio", "aa:bb:cc:dd:ee:01"))
        app_config.save_config(self.path, app_config.replace(stale, mac_host="192.0.2.99", paired_with="",
                                                              mac_hardware_address=""))
        self.assertEqual((self.peer(BT)["host"], self.peer(BT)["name"], self.peer(BT)["hw"]), ("192.0.2.99", "", ""))

    def test_an_arrangement_rewrites_the_edge_that_machines_corner_crosses(self):
        settings = self.saved()
        settings["zones"].append({"peer": BT, "kind": "corner", "corner": "top_right", "edge": "left"})
        app_config.write_settings(self.path, settings)
        self.assertEqual(app_config.apply_arrangement(self.path, BT, "left", 500, HIGH), (True, []))
        self.assertEqual((self.peer(BT)["side"], self.zones(BT)["corner"]["edge"]), ("right", "right"))

    def test_an_arrangement_is_core_ways_and_names_both_machines(self):
        settings = self.saved()
        settings["peers"][1]["side"] = "right"
        settings["zones"].append({"peer": CT, "kind": "edge"})
        app_config.write_settings(self.path, settings)
        changed, notices = app_config.apply_arrangement(self.path, CT, "right", 500, HIGH)
        self.assertTrue(changed)
        self.assertEqual(self.peer(CT)["side"], "left")
        self.assertEqual(notices, ["Bee and Sea now lead from the same part of this PC's screen, so Sea's edge is off."])
        self.assertTrue(self.zones(CT)["edge"]["off"])


class DiagramGeometryTests(unittest.TestCase):
    SCREEN = (120.0, 75.0)

    def test_machines_sharing_a_side_are_ordered_by_their_thirds_then_the_list(self):
        slots = pages_win.side_slots([
            {"side": "right", "methods": ["part"], "parts": ["end"]},
            {"side": "right", "methods": ["part"], "parts": ["start"]},
            {"side": "top", "methods": [], "parts": ["middle"]},
            {"side": "", "methods": ["corner"], "parts": ["middle"]},
        ])
        self.assertEqual(slots, [("right", 0.5, 1.0), ("right", 0.0, 0.5), ("top", 0.0, 1.0), None])

    def test_one_machine_sits_where_the_pair_always_did(self):
        for side, offset in (("right", (134.0, 0.0)), ("left", (-134.0, 0.0)), ("top", (0.0, -89.0)), ("bottom", (0.0, 89.0))):
            pc, (other,), _height = pages_win.layout_rects(400.0, 0.0, [(pages_win.SIDE_ANGLE[side], 0.0, 1.0)], self.SCREEN, 14.0)
            self.assertEqual((other[0] - pc[0], other[1] - pc[1]), offset, side)
            self.assertEqual(other[2:], self.SCREEN)
        pc, (other,), height = pages_win.layout_rects(400.0, 10.0, [(0.0, 0.0, 1.0)], self.SCREEN, 14.0)
        self.assertEqual(height, 75.0)
        self.assertEqual((pc[0] + other[0] + other[2]) / 2.0, 200.0)
        self.assertEqual(pc[1], 10.0)
        _pc, _rects, stacked = pages_win.layout_rects(400.0, 0.0, [(90.0, 0.0, 1.0)], self.SCREEN, 14.0)
        self.assertEqual(stacked, 164.0)

    def test_a_row_puts_this_pc_in_the_middle(self):
        pc, (left, right), height = pages_win.layout_rects(
            500.0, 0.0, [(180.0, 0.0, 1.0), (0.0, 0.0, 1.0)], self.SCREEN, 14.0)
        self.assertEqual(pc[0] + pc[2] / 2.0, 250.0)
        self.assertEqual(left[0] + left[2] + 14.0, pc[0])
        self.assertEqual(right[0], pc[0] + pc[2] + 14.0)
        self.assertEqual(height, 75.0)

    def test_two_on_one_side_are_each_half_as_long_and_split_it(self):
        pc, (top, bottom), _height = pages_win.layout_rects(
            500.0, 0.0, [(0.0, 0.0, 0.5), (0.0, 0.5, 1.0)], self.SCREEN, 14.0)
        self.assertEqual(top[2:], (60.0, 37.5))
        self.assertEqual(bottom[2:], (60.0, 37.5))
        self.assertEqual(top[0], pc[0] + pc[2] + 14.0)
        self.assertEqual((top[1], bottom[1] + bottom[3]), (pc[1], pc[1] + pc[3]))

    def test_an_l_has_one_beside_and_one_above(self):
        pc, (right, above), height = pages_win.layout_rects(
            500.0, 0.0, [(0.0, 0.0, 1.0), (90.0, 0.0, 1.0)], self.SCREEN, 14.0)
        self.assertEqual((right[0], right[1]), (pc[0] + pc[2] + 14.0, pc[1]))
        self.assertEqual((above[0], above[1] + above[3] + 14.0), (pc[0], pc[1]))
        self.assertEqual(above[1], 0.0)
        self.assertEqual(height, 164.0)

    def test_with_several_machines_this_pc_stays_in_the_middle_whatever_the_sides(self):
        for placed in ([(0.0, 0.0, 1.0), (90.0, 0.0, 1.0)], [(0.0, 0.0, 0.5), (0.0, 0.5, 1.0)]):
            pc, _rects, _height = pages_win.layout_rects(500.0, 0.0, placed, self.SCREEN, 14.0, centre_pc=True)
            self.assertEqual(pc[0] + pc[2] / 2.0, 250.0, placed)
        pc, (_left, right), _height = pages_win.layout_rects(
            300.0, 0.0, [(0.0, 0.0, 1.0), (90.0, 0.0, 1.0)], self.SCREEN, 14.0, centre_pc=True)
        self.assertEqual(pc[0] + pc[2] / 2.0, 150.0)
        self.assertLessEqual(right[0] + right[2], 300.001)

    def test_a_narrow_width_shrinks_the_drawing_to_fit(self):
        pc, (left, right), _height = pages_win.layout_rects(
            300.0, 0.0, [(180.0, 0.0, 1.0), (0.0, 0.0, 1.0)], self.SCREEN, 14.0)
        self.assertGreaterEqual(left[0], -0.001)
        self.assertLessEqual(right[0] + right[2], 300.001)
        self.assertLess(pc[2], 120.0)

    def test_nobody_goes_through_this_pc_on_the_way_round(self):
        for start, end in ((0.0, 1.0), (0.0, 0.5), (0.5, 1.0)):
            for angle in range(0, 360, 5):
                pc, (other,), _height = pages_win.layout_rects(500.0, 0.0, [(float(angle), start, end)], self.SCREEN, 14.0)
                apart_x = other[0] >= pc[0] + pc[2] or other[0] + other[2] <= pc[0]
                apart_y = other[1] >= pc[1] + pc[3] or other[1] + other[3] <= pc[1]
                self.assertTrue(apart_x or apart_y, (angle, start, end))

    def test_the_description_names_every_machine_and_its_side(self):
        machines = [
            {"label": "Bee", "side": "right", "methods": ["edge"], "parts": ["middle"], "corner": "top_left", "chosen": True},
            {"label": "Sea", "side": "top", "methods": [], "parts": ["middle"], "corner": "top_left", "chosen": False},
            {"label": "Dee", "side": "", "methods": [], "parts": ["middle"], "corner": "top_left", "chosen": False},
        ]
        text = pages_win.diagram_description(machines, "Right Ctrl", "hold", True)
        self.assertIn("Bee is to the right of this PC", text)
        self.assertIn("Sea is above this PC", text)
        self.assertIn("Not placed yet: Dee", text)
        self.assertIn("Input moves to Bee when you push through the whole right edge", text)

    def test_the_unplaced_line_names_them_all(self):
        self.assertEqual(pages_win.unplaced_line(["Sea"]), "Not placed yet: Sea")
        self.assertEqual(pages_win.unplaced_line(["Sea", "Dee", "Eff"]), "Not placed yet: Sea, Dee and Eff")
        self.assertEqual(pages_win.unplaced_line([]), "")


class WordingTests(unittest.TestCase):
    def test_the_summary_and_the_unlearned_side_name_the_machine(self):
        self.assertEqual(pages_win.ways_summary(["edge"], "right", [], "top_left", "F13", "hold", "Sea"),
                         "Input moves to Sea when you push through the whole right edge.")
        self.assertIn("on Sea", pages_win.not_learned_edge("Sea"))
        self.assertIn("the other machine", pages_win.not_learned_edge())

    def test_no_page_purpose_pictures_exactly_one_other_machine(self):
        for _key, _name, purpose in pages_win.PAGES:
            self.assertNotIn("the other machine", purpose)
        for line in pages_win.SCOPE.values():
            self.assertNotIn("the other machine", line)


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class WindowTests(unittest.TestCase):
    """The real window, built offscreen from a settings file, which starts no receiver, hooks or announcer."""

    PEERS = None

    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "settings.json"
        settings = app_config.migrate(None, machine_id=protocol.id_text(harness.HERE))
        settings["peers"] = self.PEERS if self.PEERS is not None else [
            harness.entry(B, "Bee", side="left", side_set_at=100, side_by=HIGH, paired_with=[]),
            harness.entry(C, "Sea", platform="windows", allow_drive=False),
        ]
        settings["zones"] = [{"peer": BT, "kind": "edge"}]
        settings["port"] = harness.free_port()
        app_config.write_settings(self.path, settings)
        self.window = kvm_bridge_win.WindowsApplication(self.path)
        # Both linked: a machine with no link gives way instead of being offered thirds (core/ways.py give_way).
        self.linked = {BT, CT}
        self.window._live = lambda peer: peer in self.linked
        self.sent = []
        self.window._send_to = lambda peer, message: self.sent.append((peer, message)) or True
        self.window._refresh_window()

    def tearDown(self):
        self.window.server.stop()
        self.window.deleteLater()

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def peer(self, ident):
        return next(item for item in self.saved()["peers"] if item["id"] == ident)


class CrossingPageTests(WindowTests):
    def sharing_side(self):
        settings = app_config.load_settings(self.path)
        for peer in settings["peers"]:
            peer.update(side="right", side_set_at=100 if peer["id"] == BT else 200, side_by=HIGH)
        settings["zones"] = [{"peer": BT, "kind": "edge"}, {"peer": CT, "kind": "edge", "off": True}]
        app_config.write_settings(self.path, settings)
        self.window._pull_peer_fields()
        self.window._choose_machine(CT)
        return self.window

    def test_edge_turns_on_over_a_machine_with_no_link_and_that_machines_edge_goes_off(self):
        # 06-10-2026: a machine gone for good held the side, and Edge would not turn on until it was moved.
        window = self.sharing_side()
        self.linked = {CT}
        window._choose_machine(CT)
        self.assertTrue(window.share_module.isHidden())
        window.way_boxes["edge"].setChecked(True)
        self.assertTrue(window.way_boxes["edge"].isChecked())
        zones = {(zone["peer"], zone["kind"]): zone for zone in app_config.load_settings(self.path)["zones"]}
        self.assertTrue(zones[(BT, "edge")].get("off"))
        self.assertTrue(zones[(BT, "edge")].get("aside"))
        self.assertIsNone(zones[(CT, "edge")].get("off"))

    def lid_shut(self):
        """Sea given Bee's side while Bee, with its lid shut, has no link to this PC."""
        window = self.sharing_side()
        self.linked = {CT}
        window._choose_machine(CT)
        window.way_boxes["edge"].setChecked(True)
        self.alerts = []
        window._on_alert = lambda title, message: self.alerts.append(message)
        return window

    def bee_zone(self):
        return next(zone for zone in app_config.load_settings(self.path)["zones"]
                    if zone["peer"] == BT and zone["kind"] == "edge")

    def test_a_machine_with_its_lid_shut_has_its_edge_set_aside_and_gets_it_back_when_it_connects(self):
        window = self.lid_shut()
        self.assertEqual(self.bee_zone(), {"peer": BT, "kind": "edge", "off": True, "aside": True})
        window.way_boxes["edge"].setChecked(False)
        window._settle_presence()
        self.assertTrue(self.bee_zone().get("aside"))
        self.linked = {BT, CT}
        window._settle_presence()
        self.assertEqual(self.bee_zone(), {"peer": BT, "kind": "edge"})
        self.assertIn(protocol.read_id(BT), {peer for peer, _message in self.sent})
        self.assertEqual(self.alerts, [])

    def test_a_machine_back_over_a_side_another_holds_stays_off_and_the_page_offers_thirds(self):
        window = self.lid_shut()
        self.linked = {BT, CT}
        window._settle_presence()
        self.assertEqual(self.bee_zone(), {"peer": BT, "kind": "edge", "off": True})
        self.assertEqual(len(self.alerts), 1)
        window._choose_machine(BT)
        self.assertFalse(window.share_module.isHidden())

    def test_a_machine_that_removed_this_pairing_stays_off_for_good_and_the_page_says_so(self):
        window = self.lid_shut()
        window._gone = lambda peer: peer == BT
        window._settle_presence()
        self.assertEqual(self.bee_zone(), {"peer": BT, "kind": "edge", "off": True})
        self.assertEqual(self.alerts, ["Bee no longer has this pairing, so its edge stays off. Remove Bee here and "
                                       "pair the two again."])
        window._choose_machine(BT)
        self.assertFalse(window.blocked_note.isHidden())
        self.assertIn("Bee no longer has this pairing", window.blocked_note.text())

    def test_the_share_panel_offers_thirds_below_the_side_and_hides_the_blocked_sentence(self):
        window = self.sharing_side()
        self.assertFalse(window.share_module.isHidden())
        self.assertEqual([window.share_tiles[p].text() for p in ("start", "middle", "end")],
                         ["Top\nBee", "Middle\nBee", "Bottom\nSea"])
        self.assertTrue(window.blocked_note.isHidden())

    def test_a_tile_cycles_its_machine_and_share_saves_and_tells_each_one(self):
        window = self.sharing_side()
        window.share_tiles["start"].click()
        self.assertEqual(window.share_tiles["start"].text(), "Top\nSea")
        window.share_button.click()
        zones = {(zone["peer"], zone["kind"]): zone for zone in self.saved()["zones"]}
        self.assertEqual(zones[(BT, "part")]["parts"], ["middle"])
        self.assertEqual(zones[(CT, "part")]["parts"], ["start", "end"])
        self.assertTrue(zones[(BT, "edge")].get("off"))
        self.assertTrue(zones[(CT, "edge")].get("off"))
        self.assertEqual({peer for peer, _data in self.arrangements()}, {B, C})

    def test_turning_on_edge_keeps_it_off_and_focuses_the_share_button(self):
        window = self.sharing_side()
        # An offscreen window is never active, so the focus asked for is what can be checked.
        with mock.patch.object(window.share_button, "setFocus") as focus:
            window.way_boxes["edge"].setChecked(True)
        self.assertFalse(window.way_boxes["edge"].isChecked())
        self.assertFalse(window.share_module.isHidden())
        self.assertTrue(window.blocked_note.isHidden())
        focus.assert_called()

    def test_turning_on_an_overlapping_part_keeps_it_off_and_focuses_the_share_button(self):
        window = self.sharing_side()
        # An offscreen window is never active, so the focus asked for is what can be checked.
        with mock.patch.object(window.share_button, "setFocus") as focus:
            window.way_boxes["part"].setChecked(True)
        self.assertFalse(window.way_boxes["part"].isChecked())
        self.assertFalse(window.share_module.isHidden())
        self.assertTrue(window.blocked_note.isHidden())
        focus.assert_called()

    def test_with_two_machines_paired_the_page_offers_a_choice_of_machine(self):
        window = self.window
        self.assertFalse(window.machine_row.isHidden())
        self.assertEqual(set(window.machine_choice._buttons), {BT, CT})
        self.assertEqual([button.text() for button in window.machine_choice._buttons.values()], ["Bee", "Sea"])
        self.assertEqual(window.machine_choice.value, BT)
        self.assertEqual(window.edge_choice.value, "left")
        self.assertTrue(window.way_boxes["edge"].isChecked())
        self.assertEqual(window.edge_heading.text(), "Where Bee is")

    def test_choosing_a_machine_shows_its_ways(self):
        window = self.window
        window._choose_machine(CT)
        self.assertEqual(window.edge_choice.value, None)
        # Every machine paired starts with its whole edge, which waits for a side.
        self.assertTrue(window.way_boxes["edge"].isChecked())
        self.assertTrue(window.way_boxes["shortcut"].isChecked())
        self.assertEqual(window.edge_heading.text(), "Where Sea is")
        self.assertEqual(window.edge_unlearned.text(), pages_win.not_learned_edge("Sea"))
        self.assertIn("Sea", window.ways_summary.text())

    def test_a_side_set_here_is_that_machines_alone_and_is_sent_to_it(self):
        window = self.window
        window._choose_machine(CT)
        window._set_arrangement("right")
        self.assertEqual(self.peer(CT)["side"], "right")
        self.assertEqual(self.peer(BT)["side"], "left")
        (peer, message), = self.sent
        self.assertEqual(peer, C)
        self.assertEqual(message["data"]["edge"], "right")
        self.assertEqual(message["data"]["set_at"], self.peer(CT)["side_set_at"])

    def arrangements(self):
        return [(peer, message["data"]) for peer, message in self.sent if message["type"] == protocol.MSG_ARRANGEMENT]

    def test_turning_off_every_way_to_a_machine_tells_it_so_with_its_side_as_it_was(self):
        self.window.way_boxes["edge"].setChecked(False)
        (peer, data), = self.arrangements()
        self.assertEqual((peer, data["edge"], data["set_at"], data["by"], data["way_back"]), (B, "left", 100, HIGH, False))
        self.window.way_boxes["corner"].setChecked(True)
        self.assertIs(self.arrangements()[-1][1]["way_back"], True)

    def test_moving_one_machine_tells_every_machine_whose_way_back_it_changed(self):
        settings = self.saved()
        settings["peers"][0].update(side="left", side_set_at=100, side_by=HIGH)
        settings["peers"][1].update(side="left", side_set_at=200, side_by=HIGH)
        settings["zones"] = [{"peer": BT, "kind": "edge"}, {"peer": CT, "kind": "edge", "off": True}]
        app_config.write_settings(self.path, settings)
        self.window._pull_peer_fields()
        self.assertEqual(ways.way_back_by(self.window._crossing_settings(), CT), BT)
        self.window._choose_machine(BT)
        self.window._set_arrangement("right")
        self.assertCountEqual([peer for peer, _data in self.arrangements()], [B, C])

    def test_removing_the_machine_in_the_way_tells_the_one_it_blocked(self):
        settings = self.saved()
        settings["peers"][0].update(side="left", side_set_at=100, side_by=HIGH)
        settings["peers"][1].update(side="left", side_set_at=200, side_by=HIGH)
        settings["zones"] = [{"peer": BT, "kind": "edge", "off": True}, {"peer": CT, "kind": "edge"}]
        app_config.write_settings(self.path, settings)
        self.window._pull_peer_fields()
        self.assertEqual(ways.way_back_by(self.window._crossing_settings(), BT), CT)

        self.window._remove_peer(harness.TOKENS[C])

        told = [(peer, data) for peer, data in self.arrangements() if peer == B]
        self.assertEqual(len(told), 1)
        self.assertFalse(told[0][1]["way_back"])
        self.assertNotIn("way_back_by", told[0][1])

    def test_an_arrangement_that_changed_something_is_answered_with_this_pcs_own(self):
        window = self.window
        window.bridge.arrangement.emit(CT, "left", 500, HIGH, True, None)
        (peer, data), = self.arrangements()
        self.assertEqual((peer, data["edge"], data["set_at"], data["by"], data["way_back"]), (C, "right", 500, HIGH, True))
        self.assertIs(self.peer(CT)["way_back"], True)
        window.bridge.arrangement.emit(CT, "left", 500, HIGH, True, None)
        self.assertEqual(len(self.arrangements()), 1)
        window.bridge.arrangement.emit(CT, "left", 500, HIGH, False, None)
        self.assertEqual(len(self.arrangements()), 2)
        self.assertIs(self.peer(CT)["way_back"], False)

    def test_every_link_up_says_whether_a_way_leads_to_that_machine(self):
        told = lambda: next(m["data"] for m in self.window._announce(B) if m["type"] == protocol.MSG_ARRANGEMENT)
        self.assertIs(told()["way_back"], True)
        self.window.way_boxes["edge"].setChecked(False)
        self.assertIs(told()["way_back"], False)

    def test_either_link_hands_on_way_back(self):
        heard = []
        self.window.bridge.arrangement.connect(lambda *args: heard.append(args))
        read = {"edge": "left", "set_at": 500, "by": HIGH_ID, "way_back": False}
        self.window.server._callbacks["arrangement"](C, read)
        self.window.sender.arrangement_callback(C, {**read, "set_at": 501, "way_back": True})
        self.assertEqual([args[4] for args in heard], [False, True])

    def test_a_way_with_a_share_offer_keeps_its_box_off_and_shows_the_panel(self):
        window = self.window
        window._choose_machine(CT)
        window._set_arrangement("left")
        before = self.path.read_bytes()
        window.way_boxes["edge"].setChecked(True)
        self.assertFalse(window.way_boxes["edge"].isChecked())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(window.share_module.isHidden())
        self.assertIn("Bee already crosses from the left edge", window.share_sentence.text())
        self.assertTrue(window.blocked_note.isHidden())
        window._choose_machine(BT)
        self.assertTrue(window.share_module.isHidden())

    def test_ways_written_here_reach_that_machines_zones_only(self):
        window = self.window
        window._choose_machine(CT)
        window._set_arrangement("right")
        window.way_boxes["corner"].setChecked(True)
        window._set_corner("bottom_right")
        zones = {zone["kind"]: zone for zone in self.saved()["zones"] if zone["peer"] == CT}
        self.assertNotIn("off", zones["corner"])
        self.assertEqual(zones["corner"]["corner"], "bottom_right")
        self.assertEqual([zone for zone in self.saved()["zones"] if zone["peer"] == BT], [{"peer": BT, "kind": "edge"}])

    def test_the_shortcut_stays_this_pcs_own(self):
        window = self.window
        window._choose_machine(CT)
        window.way_boxes["shortcut"].setChecked(False)
        self.assertFalse(self.saved()["shortcut"])
        window._choose_machine(BT)
        self.assertFalse(window.way_boxes["shortcut"].isChecked())

    def test_a_machine_that_may_drive_this_pc_and_cannot_follow_the_way_is_named(self):
        window = self.window
        window._choose_machine(CT)
        self.assertFalse(window.missing_note.isHidden())
        self.assertIn("While Bee drives this PC, this way to Sea does nothing", window.missing_note.text())
        window._choose_machine(BT)
        self.assertTrue(window.missing_note.isHidden())

    def test_a_machine_whose_side_another_holds_is_named_in_the_share_panel(self):
        window = self.window
        window._on_arrangement(CT, "right", 500, HIGH)
        window._choose_machine(CT)
        self.assertFalse(window.share_module.isHidden())
        self.assertIn("Bee already crosses from the left edge", window.share_sentence.text())
        self.assertTrue(window.blocked_note.isHidden())
        window._choose_machine(BT)
        self.assertTrue(window.share_module.isHidden())

    def test_a_machine_that_said_no_way_leads_back_is_named_until_it_says_one_does(self):
        window = self.window
        self.assertTrue(window.no_way_back_note.isHidden())
        window._on_arrangement(BT, "right", 50, HIGH, False)
        self.assertFalse(window.no_way_back_note.isHidden())
        self.assertIn("Bee has no way back to this machine", window.no_way_back_note.text())
        window._on_arrangement(BT, "right", 50, HIGH, True)
        self.assertTrue(window.no_way_back_note.isHidden())

    def test_an_arrangement_names_the_machine_holding_the_way_back_side(self):
        window = self.window
        window._choose_machine(CT)
        window._on_arrangement(CT, "right", 500, HIGH, False, BT)
        sea = next(item for item in app_config.load_settings(self.path)["peers"] if item["id"] == CT)
        self.assertEqual(sea["way_back_by"], BT)
        self.assertIn("Bee on its right too", window.no_way_back_note.text())

    def test_an_arrangement_from_the_machine_not_shown_moves_only_its_side(self):
        window = self.window
        window._on_arrangement(CT, "left", 500, HIGH)
        self.assertEqual(window.edge_choice.value, "left")
        window._choose_machine(CT)
        self.assertEqual(window.edge_choice.value, "right")

    def test_the_diagram_draws_every_machine(self):
        window = self.window
        window._choose_machine(CT)
        window._set_arrangement("right")
        drawn = {machine["label"]: machine for machine in window.arrangement_diagram._machines}
        self.assertEqual((drawn["Bee"]["side"], drawn["Sea"]["side"]), ("left", "right"))
        self.assertTrue(drawn["Sea"]["chosen"])
        self.assertFalse(drawn["Bee"]["chosen"])
        self.assertIn("Bee is to the left of this PC", window.arrangement_diagram.accessibleDescription())

    def test_pause_counts_every_machines_zones(self):
        window = self.window
        window._choose_machine(BT)
        window.way_boxes["edge"].setChecked(False)
        self.assertFalse(window._crossing_state_args()[4])
        window._choose_machine(CT)
        window._set_arrangement("right")
        window.way_boxes["edge"].setChecked(True)
        self.assertTrue(window._crossing_state_args()[4])

    def test_the_same_switch_reaches_every_machine(self):
        self.assertEqual(self.window.same_switch.text(), "Same on all machines")


class OneMachineTests(WindowTests):
    PEERS = [harness.entry(B, "Bee", side="left")]

    def test_one_machine_has_no_choice_and_the_page_reads_as_before(self):
        window = self.window
        self.assertTrue(window.machine_row.isHidden())
        self.assertEqual(window.edge_heading.text(), "Where the other machine is")
        self.assertEqual(window.edge_choice.value, "left")
        self.assertTrue(window.way_boxes["edge"].isChecked())

    def test_the_tray_has_no_item_per_machine_and_the_button_names_it(self):
        window = self.window
        self.assertEqual(window.machine_actions, {})
        self.assertEqual(window.redirect_action.text(), "Send input to Bee")
        self.assertTrue(window.redirect_action.isVisible())


class TrayTests(WindowTests):
    def test_each_machine_this_pc_sends_to_has_its_own_tray_item(self):
        window = self.window
        self.assertEqual([action.text() for action in window.machine_actions.values()],
                         ["Send input to Bee", "Send input to Sea"])
        went = []
        window.sender.go = lambda peer, *args: went.append(peer) or True
        window.machine_actions[CT].trigger()
        self.assertEqual(went, [C])
        self.assertFalse(window.redirect_action.isVisible())

    def test_the_send_button_names_the_machine_the_shortcut_would_pick(self):
        window = self.window
        window.sender.shortcut_target = lambda: C
        window._refresh_window()
        self.assertEqual(window.redirect_button.text(), "Send input to Sea")


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class DiagramWidgetTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])

    def machine(self, key, label, side, methods=(), chosen=False, parts=("middle",)):
        return {"key": key, "label": label, "side": side, "methods": list(methods), "parts": list(parts),
                "corner": "top_left", "chosen": chosen}

    def test_each_machines_marks_are_its_own_and_the_unplaced_are_named_underneath(self):
        picture = diagram.ArrangementDiagram()
        picture.resize(500, picture.height())
        picture.set_machines([self.machine("b", "Bee", "left", ["edge"], chosen=True),
                              self.machine("c", "Sea", "right", ["part"], parts=["end"]),
                              self.machine("d", "Dee", "")], "Right Ctrl", "hold", True)
        self.assertEqual(picture._marks[("edge", "b", "left")], 1.0)
        self.assertEqual(picture._marks[("part", "c", "right", "end")], 1.0)
        self.assertEqual(picture._marks[("part", "c", "right", "start")], 0.0)
        self.assertEqual(picture._unplaced, "Not placed yet: Dee")
        self.assertFalse(picture.grab().isNull())

    def test_one_machine_with_no_side_is_drawn_on_the_right_but_no_edge_lights_until_a_side_is_held(self):
        # This PC's engine crosses no edge for a machine with no side, as the page's Not learned yet says.
        picture = diagram.ArrangementDiagram()
        picture.set_machines([self.machine("b", "Bee", "", ["edge", "part"], chosen=True)], "Right Ctrl", "hold", False)
        self.assertEqual(picture._unplaced, "")
        self.assertEqual(diagram._drawn_side({"side": ""}, True), "right")
        self.assertEqual(picture._marks[("edge", "b", "right")], 0.0)
        self.assertEqual(picture._marks[("track", "b", "right")], 0.0)
        self.assertEqual(picture._marks[("part", "b", "right", "middle")], 0.0)
        picture.set_machines([self.machine("b", "Bee", "right", ["edge"], chosen=True)], "Right Ctrl", "hold", False)
        self.assertEqual(picture._marks[("edge", "b", "right")], 1.0)


if __name__ == "__main__":
    unittest.main()
