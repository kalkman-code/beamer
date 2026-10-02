"""The Mac's controller with two machines paired (WIRE.md sections 4, 5 and 8), links faked: a push
through a zone takes the machine that zone leads to, the shortcut goes where input was last or to
the first that can take it, each machine's arrangement is its own, and the window's status follows
the machine in question rather than the first in the list."""

import copy
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from bridge import KVMController
from core import protocol, receiver
from bridge_fakes import FakeClipboard, FakeClock, FakeQuartz, make_config, quiet_logger, settle
from fake_link import CAPS, FakeLink
from windows_input import WindowsInput

OWN = bytes(range(1, 17))
BEE, SEA = bytes(range(40, 56)), bytes(range(70, 86))


def entry(ident, name, secret, side):
    return {"id": protocol.id_text(ident), "name": name, "platform": "windows", "token": secret, "host": "192.0.2.10",
            "port": 24820, "hw": "", "send": True, "allow_drive": True, "side": side, "side_set_at": 100,
            "side_by": protocol.id_text(OWN), "paired_with": [], "paired_at": 1, "linked": True, "from_1_4": False}


class Three(unittest.TestCase):
    """This Mac with Bee on its right and Sea on its left, an edge zone to each."""

    controller_class = KVMController

    def setUp(self):
        FakeQuartz.reset_cursor_spies()
        self.data = {
            "schema": 6, "machine_id": protocol.id_text(OWN), "name": "This Mac", "port": 24820, "shortcut": True,
            "peers": [entry(BEE, "Bee", "b" * 43, "right"), entry(SEA, "Sea", "s" * 43, "left")],
            "zones": [{"peer": protocol.id_text(BEE), "kind": "edge"}, {"peer": protocol.id_text(SEA), "kind": "edge"}],
        }
        self.saved = []
        book = receiver.PeerBook(lambda: self.data, self._store, threading.RLock())
        identity = lambda: {"id": OWN, "name": "This Mac", "platform": "macos", "app": "1.5.0", "caps": list(CAPS), "port": 24820}
        self.clock = FakeClock()
        self.controller = self.controller_class(
            make_config("b" * 43), logger=quiet_logger(), quartz=FakeQuartz, clock=self.clock,
            clipboard=FakeClipboard("hello"), link_factory=FakeLink, desktop_bounds=lambda: (0, 0, 1920, 1080),
            book=book, identity=identity,
        )
        self.alerts = []
        self.controller.on_user_alert = lambda title, message: self.alerts.append(message)
        self.bee = self.controller.links["b" * 43]
        self.sea = self.controller.links["s" * 43]

    def _store(self, data):
        self.data = copy.deepcopy(data)
        self.saved.append(self.data)

    def up(self, link, ident, name, accepts=True):
        link.up(self.controller, ident=ident, name=name, accepts=accepts)
        settle(self.controller)

    def push(self, x, y, dx, dy=0, times=20):
        for _ in range(times):
            self.clock.value += 0.01
            event = {"location": (x, y), FakeQuartz.kCGMouseEventDeltaX: dx, FakeQuartz.kCGMouseEventDeltaY: dy}
            self.controller._handle_local_mouse(FakeQuartz.kCGEventMouseMoved, event)
            if self.controller.redirecting:
                break
        settle(self.controller)

    def takes(self, link):
        return [m["data"] for m in link.posted if m["type"] == protocol.MSG_FOCUS and m["data"]["target"] == link.peer_id]


class EachZoneTakesItsMachine(Three):
    def test_the_right_edge_takes_bee_and_the_left_edge_takes_sea(self):
        self.up(self.bee, BEE, "Bee")
        self.up(self.sea, SEA, "Sea")
        self.push(1919, 500, 30)
        self.assertEqual(self.controller.owner.on, protocol.id_text(BEE))
        self.assertEqual(self.takes(self.bee)[-1]["edge"], "left")
        self.assertEqual(self.takes(self.sea), [])
        self.controller.set_redirecting(False)
        settle(self.controller)
        self.push(0, 500, -30)
        self.assertEqual(self.controller.owner.on, protocol.id_text(SEA))
        self.assertEqual(self.takes(self.sea)[-1]["edge"], "right")

    def test_a_machine_whose_link_is_down_has_no_wall_and_the_other_still_crosses(self):
        self.up(self.sea, SEA, "Sea")
        self.push(1919, 500, 30)
        self.assertFalse(self.controller.redirecting)
        self.push(0, 500, -30)
        self.assertEqual(self.controller.owner.on, protocol.id_text(SEA))

    def test_a_machine_that_does_not_accept_input_is_not_taken_and_the_push_says_so(self):
        self.up(self.bee, BEE, "Bee", accepts=False)
        self.up(self.sea, SEA, "Sea")
        self.push(1919, 500, 30)
        self.assertIsNone(self.controller.owner.on)
        self.assertEqual(self.takes(self.bee), [])
        self.assertIn("Bee does not accept input from this Mac", self.alerts[-1])

    def test_a_machine_this_mac_does_not_send_to_has_no_wall(self):
        self.data["peers"][0]["send"] = False
        self.controller.peers_changed()
        self.up(self.sea, SEA, "Sea")
        self.push(1919, 500, 30)
        self.assertFalse(self.controller.redirecting)
        self.push(0, 500, -30)
        self.assertEqual(self.controller.owner.on, protocol.id_text(SEA))


    def test_a_machine_that_just_refused_stands_down_and_the_other_still_crosses(self):
        self.up(self.bee, BEE, "Bee")
        self.up(self.sea, SEA, "Sea")
        self.push(1919, 500, 30)
        self.bee.arrives(self.controller, protocol.refuse_msg(self.controller.owner.route, "owned"))
        settle(self.controller)
        self.assertFalse(self.controller.redirecting)
        FakeQuartz.reset_cursor_spies()
        self.push(1919, 500, 30)
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(FakeQuartz.warp_calls, [])
        self.push(0, 500, -30)
        self.assertEqual(self.controller.owner.on, protocol.id_text(SEA))


class TheShortcut(Three):
    def test_it_goes_where_input_was_last(self):
        self.up(self.bee, BEE, "Bee")
        self.up(self.sea, SEA, "Sea")
        self.push(0, 500, -30)
        self.controller.set_redirecting(False)
        settle(self.controller)
        self.assertTrue(self.controller.set_redirecting(True))
        settle(self.controller)
        self.assertEqual(self.controller.owner.on, protocol.id_text(SEA))

    def test_it_goes_to_the_first_that_can_take_input_when_the_first_in_the_list_is_down(self):
        self.up(self.sea, SEA, "Sea")
        self.assertTrue(self.controller.set_redirecting(True))
        settle(self.controller)
        self.assertEqual(self.controller.owner.on, protocol.id_text(SEA))

    def test_a_menu_names_its_machine(self):
        self.up(self.bee, BEE, "Bee")
        self.up(self.sea, SEA, "Sea")
        self.assertTrue(self.controller.set_redirecting(True, peer=protocol.id_text(SEA)))
        settle(self.controller)
        self.assertEqual(self.controller.owner.on, protocol.id_text(SEA))

    def test_a_menu_moves_input_straight_from_one_machine_to_another(self):
        self.up(self.bee, BEE, "Bee")
        self.up(self.sea, SEA, "Sea")
        self.assertTrue(self.controller.set_redirecting(True, peer=protocol.id_text(BEE)))
        settle(self.controller)
        self.assertTrue(self.controller.set_redirecting(True, peer=protocol.id_text(SEA)))
        settle(self.controller)
        self.assertEqual(self.controller.owner.on, protocol.id_text(SEA))
        self.assertTrue(self.controller.redirecting)
        let_go = [m["data"] for m in self.bee.posted if m["type"] == protocol.MSG_FOCUS][-1]
        self.assertEqual(let_go["target"], protocol.id_text(SEA))
        self.assertFalse(self.controller.set_redirecting(True, peer=protocol.id_text(SEA)))

    def test_nothing_up_says_which_machine_it_tried(self):
        self.assertFalse(self.controller.set_redirecting(True))
        self.assertTrue(any("Bee" in alert for alert in self.alerts), self.alerts)


class TheStatusFollowsTheMachineInQuestion(Three):
    def test_connected_and_the_label_speak_of_the_machine_the_shortcut_would_take(self):
        self.up(self.sea, SEA, "Sea")
        self.assertTrue(self.controller.connected)
        self.assertEqual(self.controller.peer_label, "Sea")

    def test_while_input_is_on_a_machine_the_label_is_that_machine(self):
        self.up(self.bee, BEE, "Bee")
        self.up(self.sea, SEA, "Sea")
        self.controller.set_redirecting(True, peer=protocol.id_text(SEA))
        settle(self.controller)
        self.assertEqual(self.controller.on_label, "Sea")
        self.assertEqual(self.controller.peer_label, "Sea")


class EachMachinesArrangement(Three):
    def test_an_arrangement_from_the_second_machine_is_handed_on_with_its_id(self):
        heard = []
        self.controller.on_arrangement = lambda peer, edge, set_at, by, way_back: heard.append((peer, edge, set_at, by, way_back))
        self.up(self.sea, SEA, "Sea")
        self.sea.arrives(self.controller, protocol.arrangement_v6("top", 2000, SEA))
        self.sea.arrives(self.controller, protocol.arrangement_v6("top", 2000, SEA, way_back=False))
        self.assertEqual(heard, [(protocol.id_text(SEA), "top", 2000, protocol.id_text(SEA), None),
                                 (protocol.id_text(SEA), "top", 2000, protocol.id_text(SEA), False)])

    def arrangements(self, link):
        return [m["data"] for m in link.posted if m["type"] == protocol.MSG_ARRANGEMENT]

    def test_each_link_up_says_whether_a_way_leads_to_that_machine(self):
        self.data["zones"][1]["off"] = True
        self.up(self.bee, BEE, "Bee")
        self.up(self.sea, SEA, "Sea")
        self.assertIs(self.arrangements(self.bee)[-1]["way_back"], True)
        self.assertIs(self.arrangements(self.sea)[-1]["way_back"], False)

    def test_a_side_sent_from_here_says_whether_a_way_leads_there_and_who_set_it(self):
        self.up(self.sea, SEA, "Sea")
        self.assertTrue(self.controller.send_arrangement(protocol.id_text(SEA), "top", 3000, protocol.id_text(SEA)))
        sent = self.arrangements(self.sea)[-1]
        self.assertEqual((sent["by"], sent["way_back"]), (protocol.id_text(SEA), True))
        self.data["zones"][1]["off"] = True
        self.assertTrue(self.controller.send_arrangement(protocol.id_text(SEA), "top", 3001))
        sent = self.arrangements(self.sea)[-1]
        self.assertEqual((sent["by"], sent["way_back"]), (protocol.id_text(OWN), False))


class TheLinkInArrangement(Three):
    """The same over the link a machine opened to this Mac (windows_input.py)."""

    def setUp(self):
        super().setUp()
        self.heard = []
        self.input = WindowsInput(self.controller, arrangement_callback=lambda *args: self.heard.append(args))
        self.addCleanup(self.input.stop)

    def test_a_machine_linked_in_is_told_whether_a_way_leads_to_it(self):
        self.data["zones"][1]["off"] = True
        self.input.server.caps_of = lambda _peer: frozenset()
        told = {name: [m["data"] for m in self.input._announce(ident) if m["type"] == protocol.MSG_ARRANGEMENT]
                for name, ident in (("bee", BEE), ("sea", SEA))}
        self.assertIs(told["bee"][0]["way_back"], True)
        self.assertIs(told["sea"][0]["way_back"], False)

    def test_a_side_sent_over_its_link_says_whether_a_way_leads_there_and_who_set_it(self):
        sent = []
        self.input.server.send = lambda peer, message: sent.append(message["data"]) or True
        self.assertTrue(self.input.send_arrangement(protocol.id_text(SEA), "top", 3000, protocol.id_text(SEA)))
        self.assertEqual((sent[-1]["by"], sent[-1]["way_back"]), (protocol.id_text(SEA), True))

    def test_an_arrangement_over_its_link_reaches_the_app_with_way_back(self):
        self.input._on_arrangement(SEA, {"edge": "top", "set_at": 5, "by": SEA, "way_back": False})
        self.input._on_arrangement(SEA, {"edge": "top", "set_at": 6, "by": SEA})
        self.assertEqual(self.heard, [(protocol.id_text(SEA), "top", 5, protocol.id_text(SEA), False),
                                      (protocol.id_text(SEA), "top", 6, protocol.id_text(SEA), None)])

    def test_a_side_changed_here_goes_to_that_machine_only(self):
        self.up(self.bee, BEE, "Bee")
        self.up(self.sea, SEA, "Sea")
        before = len(self.bee.posted)
        self.assertTrue(self.controller.send_arrangement(protocol.id_text(SEA), "top", 3000))
        sent = [m for m in self.sea.posted if m["type"] == protocol.MSG_ARRANGEMENT]
        self.assertEqual(sent[-1]["data"]["edge"], "top")
        self.assertEqual(len(self.bee.posted), before)

    def test_the_zones_follow_a_side_that_changed_in_the_settings(self):
        self.up(self.bee, BEE, "Bee")
        self.up(self.sea, SEA, "Sea")
        self.data["peers"][0]["side"] = "top"
        self.controller.zones_changed()
        self.push(1919, 500, 30)
        self.assertFalse(self.controller.redirecting)
        self.push(900, 0, 0, -30)
        self.assertEqual(self.controller.owner.on, protocol.id_text(BEE))


if __name__ == "__main__":
    unittest.main()
