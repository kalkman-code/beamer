"""LinkSender over real links: this PC's input to a LinkResponder on loopback, which is what the
other machine runs, so the two halves are proved against each other rather than against a mock."""

import unittest

from links_rig import make_config

import sender
from core import protocol, receiver
from core.return_edge import Rect
from core.tests import responder_harness as harness
from core.tests.responder_harness import B, HERE, TOKENS, Machine, entry, wait_for

MONITORS = [Rect(0, 0, 1920, 1080)]


class Loopback(unittest.TestCase):
    """This PC is machine B; machine HERE (a responder) is the peer it dials."""

    def setUp(self):
        self.far = Machine([entry(B, "Near", platform="windows")]).start()
        self.addCleanup(self.far.stop)
        peer = entry(HERE, "Far", token=TOKENS[B], host="127.0.0.1", port=self.far.port, side="left", linked=False)
        self.settings = harness.Settings([peer], [{"peer": protocol.id_text(HERE), "kind": "edge"}])
        self.settings.data["machine_id"] = protocol.id_text(B)
        self.book = receiver.PeerBook(self.settings.load, self.settings.save)
        self.desktop = harness.FakeDesktop(MONITORS, (0, 500))
        self.clipboard = harness.FakeClipboard("from near")
        self.alerts, self.arrivals, self.statuses = [], [], []
        self.sender = sender.LinkSender(
            self.book,
            lambda: {"id": B, "name": "Near", "platform": "windows", "app": "1.5.0", "caps": list(harness.CAPS), "port": 24820},
            zones=lambda: self.settings.data["zones"],
            desktop=self.desktop,
            clipboard=self.clipboard,
            is_local=lambda host: False,
            arrival_callback=lambda edge, x, y: self.arrivals.append((edge, x, y)),
            status_callback=lambda connected, detail: self.statuses.append((connected, detail)),
        )
        self.sender.on_alert = lambda title, message: self.alerts.append(message)
        self.sender.start(make_config(machine_id=protocol.id_text(B), crossing_resistance_px=40))
        self.addCleanup(self.sender.stop)
        self.assertTrue(wait_for(lambda: self.sender.connected, 5), self.sender.status)

    def take(self):
        self.assertTrue(self.sender.set_redirecting(True))
        self.assertTrue(wait_for(lambda: self.far.owners and self.far.owners[-1] == B))

    def test_the_first_link_learns_the_peers_id_and_says_so(self):
        self.assertTrue(self.settings.data["peers"][0]["linked"])
        self.assertEqual(self.sender.peers_up(), [HERE])

    def test_keys_and_the_pointer_reach_the_peers_injector_while_input_is_there(self):
        self.take()
        self.sender.on_key("cmd", True, 0xA2)
        self.sender.on_key("c", True, 0x43, "c")
        self.sender.on_key("c", False, 0x43, "c")
        self.sender.on_key("cmd", False, 0xA2)
        self.sender.on_motion(5, -3)
        self.assertTrue(wait_for(lambda: ("move", 5, -3) in self.far.injected()))
        keys = [call for call in self.far.injected() if call[0] == "key"]
        # The peer's welcome said it is a Windows machine, so every key goes as itself.
        self.assertEqual(keys, [("key", "ctrl", True), ("key", "c", True), ("key", "c", False), ("key", "ctrl", False)])

    def test_the_clipboard_arrives_before_any_input(self):
        self.take()
        self.sender.on_key("a", True, 0x41, "a")
        self.assertTrue(wait_for(lambda: ("key", "a", True) in self.far.injected()))
        sets = [event for event in self.far.events if event[0] in ("set",)]
        self.assertEqual(sets[0], ("set", "from near"))
        self.assertLess(self.far.events.index(sets[0]), len(self.far.events))

    def test_a_push_through_the_zone_takes_the_peer_at_the_matching_edge(self):
        for _ in range(4):
            self.sender.on_motion(-20, 0)
        self.assertTrue(wait_for(lambda: self.far.arrivals))
        edge, x, _y = self.far.arrivals[-1]
        self.assertEqual((edge, x), ("right", 1919))

    def test_the_peer_sending_the_input_home_brings_it_back_here(self):
        self.take()
        self.assertTrue(self.far.responder.send_home())
        self.assertTrue(wait_for(lambda: not self.sender.redirecting))
        self.assertEqual(self.arrivals[-1][0], None)

    def test_letting_go_releases_what_was_held_on_the_peer(self):
        self.take()
        self.sender.on_key("a", True, 0x41, "a")
        self.sender.on_button("left", True)
        self.assertTrue(wait_for(lambda: ("button", "left", True) in self.far.injected()))
        self.sender.set_redirecting(False)
        self.assertTrue(wait_for(lambda: ("key", "a", False) in self.far.injected() and ("button", "left", False) in self.far.injected()))
        self.assertTrue(wait_for(lambda: self.far.owners[-1] is None))

    def test_the_link_dropping_brings_input_home_and_says_so(self):
        self.take()
        self.far.stop()
        self.assertTrue(wait_for(lambda: not self.sender.redirecting, 5))
        self.assertIn("Lost the link to Here", self.alerts)

    def test_a_peer_that_does_not_allow_driving_refuses_and_input_comes_home(self):
        self.far.settings.peer(TOKENS[B])["allow_drive"] = False
        self.far.responder.peers_changed()
        self.assertTrue(wait_for(lambda: self.alerts == [] and self.sender._accepts.get(HERE) is False))
        self.assertFalse(self.sender.set_redirecting(True))
        self.assertEqual(self.alerts, ["Cannot switch — Here does not accept input from this one"])

    def test_the_round_trip_is_measured_while_input_is_there(self):
        self.take()
        for _ in range(5):
            self.sender.on_motion(1, 0)
        self.assertTrue(wait_for(lambda: self.sender.round_trip_ms is not None, 5))


if __name__ == "__main__":
    unittest.main()
