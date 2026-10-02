"""The Crossing page with several machines paired, in the real settings window (WIRE.md sections 5
and 8): a machine is chosen at the top of Ways in, everything per machine below it shows and writes
that machine's ways, the drawing places every machine, and the menu bar sends input to each. Built
as the README's screenshots build it, never put on screen, starting no network."""

import base64
import copy
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import AppKit  # noqa: E402
import rumps.rumps as rumps_module  # noqa: E402

AppKit.NSApplication.sharedApplication()

import bridge  # noqa: E402
import kvm_bridge_app  # noqa: E402
import settings_store  # noqa: E402
from bridge_fakes import PAIRED_TOKEN  # noqa: E402
from core import protocol, ways  # noqa: E402
from fake_link import FakeLink  # noqa: E402
from wake import WakingController  # noqa: E402

BEE = protocol.id_text(bytes(range(40, 56)))
SEA = protocol.id_text(bytes(range(70, 86)))
OAK = protocol.id_text(bytes(range(90, 106)))


def token(start):
    return base64.urlsafe_b64encode(bytes(range(start, start + 32))).decode("ascii").rstrip("=")


def entry(ident, secret, name, side="", own="", **extra):
    return {**settings_store.PEER_DEFAULTS, "id": ident, "name": name, "platform": "windows", "token": secret,
            "host": "192.0.2.30", "port": 24820, "paired_at": 1_700_000_000, "linked": True, "side": side,
            "side_set_at": 100 if side else 0, "side_by": own if side else "", **extra}


class Page(unittest.TestCase):
    """This Mac with Bee on its right by its edge, and Sea on its left by its top third."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "config.json"
        raw = settings_store.config_to_raw(settings_store.editable_default_config())
        raw.update(host="192.0.2.20", auth_token=PAIRED_TOKEN)
        path.write_text(json.dumps(raw))
        self.store = settings_store.SettingsStore(path)
        self.later = []
        patcher = mock.patch.object(kvm_bridge_app.AppHelper, "callLater", lambda _delay, call: self.later.append(call))
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, peers, zones=()):
        settings = copy.deepcopy(self.store.current())
        own = settings["machine_id"]
        settings["peers"] = [entry(*peer[:3], **{"own": own, **(peer[3] if len(peer) > 3 else {})}) for peer in peers]
        settings["zones"] = list(zones)
        self.store.save_settings(settings)
        logger = logging.getLogger("test-crossing-page-machines")
        book, identity = bridge.links_from_store(self.store, "1.5.0")
        self.controller = WakingController(self.store.load(), logger=logger, book=book, identity=identity,
                                           link_factory=FakeLink)
        with mock.patch.object(kvm_bridge_app, "accessibility_granted", return_value=True), \
                mock.patch.object(kvm_bridge_app, "input_monitoring_granted", return_value=True):
            self.window = kvm_bridge_app.ControlWindow.alloc().initWithController_settingsStore_logger_(
                self.controller, self.store, logger)
        self.addCleanup(self.window.appearance_watch.stop)
        self.window.windows_input = mock.Mock()
        self.window.windows_input.send_arrangement.return_value = True
        self.window.windows_input.server.links.return_value = []
        self.sent = []
        self.sent_by = []
        self.controller.send_arrangement = lambda peer, edge, set_at, by=None: (
            self.sent.append((peer, edge, set_at)) or self.sent_by.append(by) or True)
        return self.window

    def two(self):
        return self.build(
            [(BEE, token(100), "Bee", {"side": "right"}), (SEA, token(150), "Sea", {"side": "left"})],
            [{"peer": BEE, "kind": "edge"}, {"peer": SEA, "kind": "part", "parts": ["start"]},
             {"peer": SEA, "kind": "corner", "corner": "bottom_left", "edge": "left"}])

    def ways_of(self, peer):
        return ways.ways(self.store.current(), peer)

    def methods(self):
        return [name for name, tile in self.window.method_boxes.items() if tile.value]

    def toggle(self, way):
        self.window.method_boxes[way]._toggle()

    def pick(self, peer):
        self.window.machine_select._choose(peer)


class OneOrNoMachine(Page):
    def test_with_one_machine_there_is_no_picker_and_the_page_reads_as_today(self):
        window = self.build([(BEE, token(100), "Bee", {"side": "left"})], [{"peer": BEE, "kind": "edge"}])
        self.assertTrue(window.machine_row.isHidden())
        self.assertEqual(window.edge_caption.text, "Where the other machine is")
        self.assertEqual(window.edge_select.value, "left")
        self.assertEqual(self.methods(), ["edge", "shortcut"])

    def test_one_machine_with_no_side_shows_right_as_it_always_has(self):
        window = self.build([(BEE, token(100), "Bee")])
        self.assertEqual(window.edge_select.value, "right")

    def test_a_save_with_one_machine_not_placed_writes_the_shown_right_unstamped_so_its_own_side_wins(self):
        window = self.build([(BEE, token(100), "Bee")], [{"peer": BEE, "kind": "edge"}])
        window._apply_settings()
        held = self.store.current()["peers"][0]
        self.assertEqual((held["side"], held["side_set_at"], held["side_by"]), ("right", 0, ""))
        self.assertEqual(self.sent, [(BEE, "right", 0)])
        window.apply_arrangement(BEE, "bottom", 1_790_000_000, BEE, True)
        self.assertEqual(self.ways_of(BEE)["side"], "top")

    def test_with_one_machine_a_change_writes_its_ways_and_sends_it_the_side(self):
        window = self.build([(BEE, token(100), "Bee", {"side": "left"})], [{"peer": BEE, "kind": "edge"}])
        window.edge_select._choose("top")
        window._apply_settings()
        self.assertEqual(self.ways_of(BEE)["side"], "top")
        self.assertEqual([(peer, edge) for peer, edge, _at in self.sent], [(BEE, "top")])

    def test_with_none_paired_the_page_writes_the_flat_settings_as_today(self):
        window = self.build([])
        self.assertTrue(window.machine_row.isHidden())
        self.toggle("corner")
        window._apply_settings()
        self.assertIn("corner", self.store.load().crossing["methods"])


class ChoosingAMachine(Page):
    def test_with_two_machines_the_picker_names_each_and_starts_on_the_first(self):
        window = self.two()
        self.assertFalse(window.machine_row.isHidden())
        self.assertEqual([title for _value, title in window.machine_select.choices], ["Bee", "Sea"])
        self.assertEqual(window.machine_select.value, BEE)
        self.assertEqual(window.edge_caption.text, "Where Bee is")
        self.assertEqual((window.edge_select.value, self.methods()), ("right", ["edge", "shortcut"]))

    def test_choosing_a_machine_shows_its_ways(self):
        window = self.two()
        self.pick(SEA)
        self.assertEqual(window.edge_caption.text, "Where Sea is")
        self.assertEqual(window.edge_select.value, "left")
        self.assertEqual(self.methods(), ["part", "corner", "shortcut"])
        self.assertEqual([name for name, tile in window.part_boxes.items() if tile.value], ["start"])
        self.assertEqual(window.corner_select.value, "bottom_left")
        self.assertEqual(window.push_strip.other_name, "Sea")

    def test_a_change_writes_the_chosen_machines_ways_and_leaves_the_others_alone(self):
        window = self.two()
        bee = self.ways_of(BEE)
        self.pick(SEA)
        self.toggle("notch")
        window._apply_settings()
        self.assertIn("notch", self.ways_of(SEA)["methods"])
        self.assertEqual(self.ways_of(BEE), bee)
        self.assertEqual(self.sent, [])

    def test_a_new_side_is_sent_to_that_machine_and_over_its_own_link_when_this_macs_is_down(self):
        window = self.two()
        self.pick(SEA)
        window.edge_select._choose("top")
        window._apply_settings()
        stamp = next(peer for peer in self.store.current()["peers"] if peer["id"] == SEA)["side_set_at"]
        self.assertEqual(self.sent, [(SEA, "top", stamp)])
        window.windows_input.send_arrangement.assert_not_called()
        self.controller.send_arrangement = lambda *_args: False
        window.edge_select._choose("bottom")
        window._apply_settings()
        window.windows_input.send_arrangement.assert_called_once()
        self.assertEqual(window.windows_input.send_arrangement.call_args.args[:2], (SEA, "bottom"))

    def test_the_engine_is_built_again_once_a_machines_ways_change(self):
        window = self.two()
        self.pick(SEA)
        with mock.patch.object(self.controller, "zones_changed") as rebuilt:
            self.toggle("notch")
            window._apply_settings()
        rebuilt.assert_called()

    def test_a_clash_is_said_naming_both_and_the_controls_go_back_to_what_is_saved(self):
        window = self.two()
        self.pick(SEA)
        # Moved onto Bee's side, Sea keeps the side and its ways there go off (core/ways.py settle).
        window.edge_select._choose("right")
        window._apply_settings()
        sea = next(entry for entry in self.store.current()["peers"] if entry["id"] == SEA)
        self.assertEqual(sea["side"], "right")
        before = copy.deepcopy(self.store.current())
        methods = self.methods()
        self.toggle("edge")
        window._apply_settings()
        self.assertEqual(window.message_label.ink, "fault")
        self.assertIn("Bee", window.message_label.text)
        self.assertIn("Sea", window.message_label.text)
        self.assertEqual(window.edge_select.value, "right")
        self.assertEqual(self.methods(), methods)
        self.assertEqual(self.store.current()["zones"], before["zones"])

    def test_switching_machines_saves_a_change_still_waiting_first(self):
        window = self.two()
        self.toggle("corner")
        self.assertTrue(self.later)
        self.pick(SEA)
        self.assertIn("corner", self.ways_of(BEE)["methods"])
        sea = self.ways_of(SEA)
        for call in self.later:
            call()
        self.assertEqual(self.ways_of(SEA), sea)
        self.assertEqual(self.methods(), ["part", "corner", "shortcut"])

    def test_the_shortcut_resistance_and_drag_guard_stay_this_macs_own(self):
        window = self.two()
        self.pick(SEA)
        held = (self.ways_of(BEE), self.ways_of(SEA))
        dragging = self.store.load().crossing["block_while_dragging"]
        self.toggle("shortcut")
        window.dragging_box._toggle()
        window._apply_settings()
        self.assertFalse(self.store.current()["shortcut"])
        self.assertNotIn("shortcut", self.store.load().crossing["methods"])
        self.assertEqual(self.store.load().crossing["block_while_dragging"], not dragging)
        self.assertEqual((self.ways_of(BEE), self.ways_of(SEA)), held)
        self.pick(BEE)
        self.assertFalse(window.method_boxes["shortcut"].value)

    def test_a_machine_with_no_side_shows_none_with_several_paired(self):
        window = self.build([(BEE, token(100), "Bee", {"side": "right"}), (OAK, token(200), "Oak")],
                            [{"peer": BEE, "kind": "edge"}])
        self.pick(OAK)
        self.assertIsNone(window.edge_select.value or None)
        window._apply_settings()
        self.assertEqual(self.ways_of(OAK)["side"], "")

    def test_removing_the_chosen_machine_falls_back_to_the_first_and_one_left_hides_the_picker(self):
        window = self.two()
        self.pick(SEA)
        self.store.remove_peer(token(150))
        window.peers_changed()
        self.assertTrue(window.machine_row.isHidden())
        self.assertEqual(window.edge_select.value, "right")
        self.assertEqual(window.edge_caption.text, "Where the other machine is")

    def test_a_third_machine_paired_joins_the_picker(self):
        window = self.two()
        self.store.add_peer(entry(OAK, token(200), "Oak"))
        window.peers_changed()
        self.assertEqual([title for _value, title in window.machine_select.choices], ["Bee", "Sea", "Oak"])

    def test_a_name_learnt_on_a_link_reaches_the_picker_on_the_next_tick(self):
        window = self.two()
        settings = copy.deepcopy(self.store.current())
        settings["peers"][1]["name"] = "Sea Studio"
        self.store.save_settings(settings)
        window.refresh()
        self.assertEqual([title for _value, title in window.machine_select.choices], ["Bee", "Sea Studio"])

    def test_an_arrangement_from_the_chosen_machine_moves_its_control(self):
        window = self.two()
        self.pick(SEA)
        window.apply_arrangement(SEA, "top", 5000, SEA)
        self.assertEqual(window.edge_select.value, "bottom")
        self.assertEqual(window.machine_select.value, SEA)

    def test_an_arrangement_arriving_keeps_a_change_still_waiting(self):
        window = self.two()
        self.pick(SEA)
        self.toggle("notch")
        window.apply_arrangement(BEE, "right", 5000, BEE)
        self.assertIn("notch", self.ways_of(SEA)["methods"])
        self.assertTrue(window.method_boxes["notch"].value)
        self.assertEqual(self.ways_of(BEE)["side"], "left")

    def test_a_machine_paired_keeps_a_change_still_waiting(self):
        window = self.two()
        self.pick(SEA)
        self.toggle("notch")
        self.store.add_peer(entry(OAK, token(200), "Oak"))
        window.peers_changed()
        self.assertIn("notch", self.ways_of(SEA)["methods"])
        self.assertTrue(window.method_boxes["notch"].value)

    def test_a_change_waiting_for_a_machine_just_removed_is_dropped_without_a_fault(self):
        window = self.two()
        self.pick(SEA)
        self.toggle("notch")
        self.store.remove_peer(token(150))
        window.peers_changed()
        self.assertNotIn(SEA, [item["id"] for item in self.store.current()["peers"]])
        self.assertEqual(window.machine_select, None)

    def test_a_missing_pairing_is_noted_under_the_ways(self):
        window = self.build([(BEE, token(100), "Bee", {"side": "right", "allow_drive": True, "paired_with": []}),
                             (SEA, token(150), "Sea", {"side": "left", "allow_drive": True, "paired_with": [BEE]})],
                            [{"peer": BEE, "kind": "edge"}])
        expected = ways.missing_sentence(self.store.current(), SEA, "this Mac")
        self.assertTrue(expected)
        self.assertEqual(window.missing_note.view.isHidden(), not ways.missing_sentence(self.store.current(), BEE, "this Mac"))
        self.pick(SEA)
        self.assertFalse(window.missing_note.view.isHidden())
        self.assertEqual(window.missing_note.text, expected)


class WayBack(Page):
    """`arrangement`'s `way_back` (WIRE.md section 8): what each machine is told, and what it said."""

    def entry_of(self, peer):
        return next(item for item in self.store.current()["peers"] if item["id"] == peer)

    def test_turning_off_every_way_to_a_machine_tells_it_so_with_its_side_as_it_was(self):
        window = self.two()
        self.pick(SEA)
        self.toggle("part")
        self.toggle("corner")
        window._apply_settings()
        self.assertEqual(self.sent, [(SEA, "left", 100)])
        self.assertEqual(self.sent_by, [self.store.current()["machine_id"]])
        self.toggle("corner")
        window._apply_settings()
        self.assertEqual(len(self.sent), 2)

    def test_an_arrangement_that_changed_something_is_answered_with_this_macs_own(self):
        window = self.two()
        window.apply_arrangement(SEA, "top", 5000, SEA, True)
        self.assertEqual(self.sent, [(SEA, "bottom", 5000)])
        self.assertEqual(self.sent_by, [SEA])
        self.assertIs(self.entry_of(SEA)["way_back"], True)
        window.apply_arrangement(SEA, "top", 5000, SEA, True)
        self.assertEqual(len(self.sent), 1)
        window.apply_arrangement(SEA, "top", 5000, SEA, False)
        self.assertEqual(len(self.sent), 2)
        self.assertIs(self.entry_of(SEA)["way_back"], False)

    def test_the_answer_goes_over_the_link_the_machine_opened_when_this_macs_is_down(self):
        window = self.two()
        self.controller.send_arrangement = lambda *_args: False
        window.apply_arrangement(SEA, "top", 5000, SEA, True)
        window.windows_input.send_arrangement.assert_called_once_with(SEA, "bottom", 5000, SEA)


class TheNotes(Page):
    """What the page says under the ways about a machine it cannot reach, or that cannot reach back."""

    def test_a_machine_whose_side_another_holds_is_named_with_the_one_that_holds_it(self):
        window = self.build([(BEE, token(100), "Bee", {"side": "right"}), (SEA, token(150), "Sea", {"side": "right"})],
                            [{"peer": BEE, "kind": "edge"}, {"peer": SEA, "kind": "edge", "off": True}])
        self.pick(SEA)
        expected = ways.blocked_sentence(self.store.current(), SEA, "this Mac")
        self.assertIn("Sea has no way in: Bee already leads from the right edge", expected)
        self.assertFalse(window.blocked_note.view.isHidden())
        self.assertEqual(window.blocked_note.text, expected)
        self.pick(BEE)
        self.assertTrue(window.blocked_note.view.isHidden())

    def test_a_machine_that_said_no_way_leads_back_is_named_until_it_says_one_does(self):
        window = self.build([(BEE, token(100), "Bee", {"side": "right"}),
                             (SEA, token(150), "Sea", {"side": "left", "way_back": False})],
                            [{"peer": BEE, "kind": "edge"}, {"peer": SEA, "kind": "edge"}])
        self.assertTrue(window.no_way_back_note.view.isHidden())
        self.pick(SEA)
        self.assertFalse(window.no_way_back_note.view.isHidden())
        self.assertEqual(window.no_way_back_note.text, ways.no_way_back_sentence(self.store.current(), SEA))
        window.apply_arrangement(SEA, "right", 5000, SEA, True)
        self.assertTrue(window.no_way_back_note.view.isHidden())


class TheOverview(Page):
    def test_the_address_shown_is_the_machine_the_status_speaks_of(self):
        window = self.build([(BEE, token(100), "Bee", {"host": "192.0.2.31"}), (SEA, token(150), "Sea", {"host": "192.0.2.32"})])
        with mock.patch.object(self.controller, "_in_question", return_value=SEA):
            window._show_peer()
        self.assertIn("192.0.2.32", window.peer_footer.text)


class TheDrawing(Page):
    def test_the_drawing_has_every_machine_with_the_chosen_one_marked(self):
        window = self.build([(BEE, token(100), "Bee", {"side": "right"}), (SEA, token(150), "Sea", {"side": "top"}),
                             (OAK, token(200), "Oak")], [{"peer": BEE, "kind": "edge"}])
        self.pick(SEA)
        machines = window.arrangement_diagram.machines
        self.assertEqual([(m["label"], m["side"], m["chosen"]) for m in machines],
                         [("Bee", "right", False), ("Sea", "top", True), ("Oak", "", False)])
        self.assertEqual(machines[0]["methods"], ["edge"])
        label = window.arrangement_diagram.accessibilityLabel()
        for words in ("Bee is to the right of this Mac.", "Sea is above this Mac.", "Oak is not placed yet."):
            self.assertIn(words, label)
        drawing = window.arrangement_diagram
        drawing.setFrame_(((0, 0), (480, drawing.HEIGHT)))
        drawing._draw(False)
        self.assertEqual(drawing.not_placed.string(), "Not placed yet: Oak")
        self.assertTrue(drawing.screens[OAK][0].isHidden())
        self.assertFalse(drawing.screens[SEA][0].isHidden())

    def test_the_chosen_machines_drawing_follows_the_controls_before_the_save(self):
        window = self.two()
        self.pick(SEA)
        window.edge_select._choose("bottom")
        sea = window.arrangement_diagram.machines[1]
        self.assertEqual((sea["side"], sea["chosen"]), ("bottom", True))


class Wording(Page):
    def test_the_switch_is_same_on_all_machines(self):
        window = self.two()
        self.assertEqual(window.same_switch.words.text, "Same on all machines")


class TheMenuBar(Page):
    def tray(self):
        tray = mock.Mock()
        tray.controller = self.controller
        tray.control_window = self.window
        return tray

    def test_a_machines_item_sends_input_to_it(self):
        self.two()
        self.controller.event_tap = object()
        self.controller.input_error = None
        with mock.patch.object(self.controller, "set_redirecting", return_value=True) as switch:
            kvm_bridge_app.TrayApp._send_to(self.tray(), SEA)
        switch.assert_called_once_with(True, peer=SEA)

    def test_the_item_of_the_machine_input_is_on_brings_it_back(self):
        self.two()
        self.controller.event_tap = object()
        self.controller.input_error = None
        self.controller.owner.on = SEA
        self.controller.redirecting = True
        with mock.patch.object(self.controller, "set_redirecting", return_value=True) as switch:
            kvm_bridge_app.TrayApp._send_to(self.tray(), SEA)
        switch.assert_called_once_with(False)

    def test_another_machines_item_moves_input_straight_there(self):
        self.two()
        self.controller.event_tap = object()
        self.controller.input_error = None
        self.controller.owner.on = BEE
        self.controller.redirecting = True
        with mock.patch.object(self.controller, "set_redirecting", return_value=True) as switch:
            kvm_bridge_app.TrayApp._send_to(self.tray(), SEA)
        self.assertEqual(switch.call_args_list, [mock.call(True, peer=SEA)])

    def menu_tray(self):
        tray = self.tray()
        # The menu rumps.App keeps, which rumps does not export.
        tray.menu = rumps_module.Menu()
        tray.toggle_item = kvm_bridge_app.rumps.MenuItem(kvm_bridge_app.TOGGLE_ITEM)
        tray.menu.add(tray.toggle_item)
        tray.menu.add(kvm_bridge_app.rumps.MenuItem("Pause crossing"))
        tray._send_keys = []
        return tray

    def titles(self, tray):
        return [item.title for item in tray.menu.values() if not item.hidden]

    def test_with_two_machines_the_menu_has_an_item_each_in_place_of_the_one(self):
        self.two()
        tray = self.menu_tray()
        kvm_bridge_app.TrayApp._machine_items(tray)
        self.assertEqual(self.titles(tray), ["Send input to Bee", "Send input to Sea", "Pause crossing"])
        self.controller.owner.on = SEA
        self.controller.redirecting = True
        kvm_bridge_app.TrayApp._machine_items(tray)
        self.assertEqual(self.titles(tray), ["Send input to Bee", "Bring input back", "Pause crossing"])

    def test_with_one_machine_the_menu_keeps_its_one_item(self):
        self.build([(BEE, token(100), "Bee", {"side": "left"})])
        tray = self.menu_tray()
        kvm_bridge_app.TrayApp._machine_items(tray)
        self.assertEqual(self.titles(tray), [kvm_bridge_app.TOGGLE_ITEM, "Pause crossing"])

    def test_a_machine_switched_off_leaves_the_menu(self):
        self.two()
        tray = self.menu_tray()
        kvm_bridge_app.TrayApp._machine_items(tray)
        self.store.set_peer(token(150), send=False)
        kvm_bridge_app.TrayApp._machine_items(tray)
        self.assertEqual(self.titles(tray), [kvm_bridge_app.TOGGLE_ITEM, "Pause crossing"])

    def test_without_the_permissions_nothing_is_sent_and_it_says_why(self):
        self.two()
        self.controller.event_tap = None
        tray = self.tray()
        with mock.patch.object(self.controller, "set_redirecting") as switch:
            kvm_bridge_app.TrayApp._send_to(tray, SEA)
        switch.assert_not_called()
        tray.notify_user.assert_called_once()


if __name__ == "__main__":
    unittest.main()
