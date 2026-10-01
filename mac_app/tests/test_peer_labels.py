"""The controller names each machine as the window shows it, so a status can say which one it is about."""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from bridge import KVMController
from bridge_fakes import PAIRED_TOKEN, FakeClock, FakeQuartz, make_config, quiet_logger
from fake_link import FakeLink

FIRST = "AAAAAAAAAAAAAAAAAAAAAA"
SECOND = "BBBBBBBBBBBBBBBBBBBBBw"


class PeerLabelTests(unittest.TestCase):
    def setUp(self):
        self.controller = KVMController(
            make_config(PAIRED_TOKEN), logger=quiet_logger(), quartz=FakeQuartz, clock=FakeClock(),
            link_factory=FakeLink, desktop_bounds=lambda: (0, 0, 1920, 1080),
        )
        first = self.controller.book.data["peers"][0]
        first.update(id=FIRST, name="Desk")
        second = copy.deepcopy(first)
        second.update(id=SECOND, name="Desk", token="other-token")
        self.controller.book.data["peers"].append(second)

    def test_the_first_machine_is_the_peer_label(self):
        self.assertEqual(self.controller.peer_label, "Desk (AAAA)")

    def test_the_machine_input_is_on_is_named_by_its_own_label(self):
        self.assertIsNone(self.controller.on_label)
        self.controller.owner.on = SECOND
        self.assertEqual(self.controller.on_label, "Desk (BBBw)")

    def test_the_machine_driving_this_mac_is_named_by_its_own_label(self):
        self.assertIsNone(self.controller.driver_label)
        self.controller.driver = SECOND
        self.assertEqual(self.controller.driver_label, "Desk (BBBw)")

    def test_a_machine_no_longer_in_the_list_has_no_label(self):
        self.controller.driver = "CCCCCCCCCCCCCCCCCCCCCg"
        self.assertIsNone(self.controller.driver_label)

    def test_an_alert_about_a_machine_names_it_by_its_label(self):
        self.controller._up_names[SECOND] = "Desk"
        self.assertEqual(self.controller._name_of(SECOND), "Desk (BBBw)")
        self.assertEqual(self.controller._name_of("CCCCCCCCCCCCCCCCCCCCCg"), "the other machine")

    def test_no_peers_no_label(self):
        self.controller.book.data["peers"].clear()
        self.assertIsNone(self.controller.peer_label)


if __name__ == "__main__":
    unittest.main()
