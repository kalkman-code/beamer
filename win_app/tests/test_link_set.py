"""LinkSet keeps one link per paired entry; a removed machine's link must not hold the window's thread."""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sender
from core import protocol

TOKEN = protocol.id_text(bytes(range(32)))


class FakeLink:
    stops = []
    peer_id = ""

    def __init__(self, token, book, identity, **callbacks):
        self.token = token
        self.callbacks = callbacks

    def start(self):
        pass

    def stop(self, wait=True):
        FakeLink.stops.append(wait)


class LinkSetTest(unittest.TestCase):
    def test_a_removed_machines_link_stops_without_waiting(self):
        FakeLink.stops = []
        links = sender.LinkSet(SimpleNamespace(_book=None, _identity=None, _hardware=None, _peers={}), is_local=lambda host: False, link_class=FakeLink)
        links.start()
        links.sync([{"token": TOKEN, "host": "192.0.2.10"}])
        links.sync([])
        self.assertEqual(FakeLink.stops, [False])

    def test_a_removed_machine_that_was_linked_goes_down_in_the_owner(self):
        peer = bytes(range(1, 17))
        told = []

        class Linked(FakeLink):
            peer_id = protocol.id_text(peer)

        owner = SimpleNamespace(
            _book=None, _identity=None, _hardware=None, _peers={},
            on_link=lambda who, up, accepts=False: told.append((who, up)),
            announce=lambda who: [],
        )
        links = sender.LinkSet(owner, is_local=lambda host: False, link_class=Linked)
        links.start()
        links.sync([{"token": TOKEN, "host": "192.0.2.10"}])
        link = links._links[TOKEN]
        links._up(link, {"id": peer, "accepts": True})
        told.clear()
        links.sync([])
        self.assertEqual(told, [(peer, False)])
        self.assertIsNone(links._link(peer))

    def test_a_link_that_comes_up_after_its_machine_was_removed_is_not_registered(self):
        peer = bytes(range(1, 17))
        told = []
        owner = SimpleNamespace(
            _book=None, _identity=None, _hardware=None, _peers={},
            on_link=lambda who, up, accepts=False: told.append((who, up)),
            announce=lambda who: [],
        )
        links = sender.LinkSet(owner, is_local=lambda host: False, link_class=FakeLink)
        links.start()
        links.sync([{"token": TOKEN, "host": "192.0.2.10"}])
        link = links._links[TOKEN]
        links.sync([])
        links._up(link, {"id": peer, "accepts": True})
        self.assertEqual(told, [])
        self.assertIsNone(links._link(peer))

    def test_a_machine_removed_while_it_is_being_told_up_ends_down_in_the_owner(self):
        peer = bytes(range(1, 17))
        told = []
        holder = {}

        def on_link(who, up, accepts=False):
            # The True is recorded after the removal's False, as when sync runs in the gap between
            # _up's lock and its call.
            if up and "sync" in holder:
                holder.pop("sync")()
            told.append((who, up))

        owner = SimpleNamespace(
            _book=None, _identity=None, _hardware=None, _peers={},
            on_link=on_link, announce=lambda who: [],
        )

        class Linked(FakeLink):
            peer_id = protocol.id_text(peer)

        links = sender.LinkSet(owner, is_local=lambda host: False, link_class=Linked)
        links.start()
        links.sync([{"token": TOKEN, "host": "192.0.2.10"}])
        link = links._links[TOKEN]
        holder["sync"] = lambda: links.sync([])
        links._up(link, {"id": peer, "accepts": True})
        self.assertEqual(told, [(peer, False), (peer, True), (peer, False)])
        self.assertIsNone(links._link(peer))

    def test_each_link_is_given_the_senders_hardware_hook(self):
        hook = lambda host: "aa:bb:cc:dd:ee:ff"
        made = []

        class Recording(FakeLink):
            def __init__(self, *args, **callbacks):
                super().__init__(*args, **callbacks)
                made.append(self)

        links = sender.LinkSet(SimpleNamespace(_book=None, _identity=None, _hardware=hook, _peers={}), is_local=lambda host: False, link_class=Recording)
        links.sync([{"token": TOKEN, "host": "192.0.2.10"}])
        self.assertIs(made[0].callbacks["hardware"], hook)

    def test_each_link_reads_a_large_frame_only_while_the_sender_expects_its_clipboard(self):
        made = []

        class Recording(FakeLink):
            def __init__(self, *args, **callbacks):
                super().__init__(*args, **callbacks)
                made.append(self)

        expected = []
        owner = SimpleNamespace(_book=None, _identity=None, _hardware=None, _peers={}, expects_clipboard=lambda link, began: expected.append((link, began)) or True)
        links = sender.LinkSet(owner, is_local=lambda host: False, link_class=Recording)
        links.sync([{"token": TOKEN, "host": "192.0.2.10"}])
        self.assertTrue(made[0].callbacks["large"](made[0], 5.0))
        self.assertEqual(expected, [(made[0], 5.0)])

    def test_no_hook_leaves_the_link_without_one(self):
        made = []

        class Recording(FakeLink):
            def __init__(self, *args, **callbacks):
                super().__init__(*args, **callbacks)
                made.append(self)

        links = sender.LinkSet(SimpleNamespace(_book=None, _identity=None, _hardware=None, _peers={}), is_local=lambda host: False, link_class=Recording)
        links.sync([{"token": TOKEN, "host": "192.0.2.10"}])
        self.assertNotIn("hardware", made[0].callbacks)


if __name__ == "__main__":
    unittest.main()
