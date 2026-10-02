"""Wake-on-LAN with two machines paired: a switch to a machine that is asleep wakes that machine, at
its own hardware address and host, and a link coming up learns that machine's address for it."""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import protocol
from bridge_fakes import settle
from test_mac_three import BEE, SEA, Three
from wake import WakingController


class WakingThree(Three):
    controller_class = WakingController

    def setUp(self):
        self.woken = []
        self.looked_up = []
        self.learnt = []
        super().setUp()
        self.controller.wake_sender = lambda mac, host: self.woken.append((mac, host))
        self.controller.mac_lookup = lambda host: self.looked_up.append(host) or "aa:bb:cc:00:00:0" + ("1" if host == "192.0.2.11" else "2")
        self.controller.on_mac_learned = lambda peer, mac: self.learnt.append((peer, mac))
        self.data["peers"][0].update(hw="aa:bb:cc:dd:ee:01", host="192.0.2.11")
        self.data["peers"][1].update(hw="aa:bb:cc:dd:ee:02", host="192.0.2.12")
        self.controller.WAKE_POLL_SECONDS = 0.01

    def test_a_switch_to_a_sleeping_machine_wakes_that_machine(self):
        self.up(self.bee, BEE, "Bee")
        self.assertFalse(self.controller.set_redirecting(True, peer=protocol.id_text(SEA)))
        self.assertTrue(self.controller.waking)
        self.assertTrue(_wait(lambda: self.woken))
        self.assertEqual(self.woken, [("aa:bb:cc:dd:ee:02", "192.0.2.12")])
        self.assertIn("Sea", self.controller.connection_status)
        self.controller.stop_event.set()

    def test_moving_input_on_to_a_sleeping_machine_wakes_it_and_leaves_input_where_it_is(self):
        self.up(self.bee, BEE, "Bee")
        self.assertTrue(self.controller.set_redirecting(True, peer=protocol.id_text(BEE)))
        settle(self.controller)
        self.assertFalse(self.controller.set_redirecting(True, peer=protocol.id_text(SEA)))
        self.assertTrue(_wait(lambda: self.woken))
        self.assertEqual(self.woken, [("aa:bb:cc:dd:ee:02", "192.0.2.12")])
        self.assertTrue(self.controller.redirecting)
        self.controller.stop_event.set()

    def test_the_shortcut_to_nothing_up_wakes_the_first_it_would_send_to(self):
        self.assertFalse(self.controller.set_redirecting(True))
        self.assertTrue(_wait(lambda: self.woken))
        self.assertEqual(self.woken, [("aa:bb:cc:dd:ee:01", "192.0.2.11")])
        self.controller.stop_event.set()

    def test_can_wake_speaks_of_the_machine_in_question(self):
        self.up(self.bee, BEE, "Bee")
        self.assertFalse(self.controller.can_wake)
        self.data["peers"][0]["send"] = False
        self.controller.peers_changed()
        self.assertTrue(self.controller.can_wake)

    def test_every_link_coming_up_learns_its_own_machines_address(self):
        self.bee.dialled = ("192.0.2.11", 24820)
        self.up(self.bee, BEE, "Bee")
        self.up(self.sea, SEA, "Sea")
        self.assertTrue(_wait(lambda: len(self.learnt) == 2), self.learnt)
        self.assertEqual(sorted(self.learnt), sorted([(protocol.id_text(BEE), "aa:bb:cc:00:00:01"),
                                                      (protocol.id_text(SEA), "aa:bb:cc:00:00:02")]))


def _wait(predicate, timeout=2.0):
    done = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return True
        done.wait(0.01)
    return predicate()


if __name__ == "__main__":
    unittest.main()
