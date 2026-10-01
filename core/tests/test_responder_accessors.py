"""Two small readers the apps need from the responder and its book: the zones as the book holds
them, and the capabilities a linked peer announced."""

import unittest

from core import protocol
from core.tests.responder_harness import B, Initiator, Machine, entry, wait_for


class AccessorTests(unittest.TestCase):
    def test_the_book_gives_the_zones_it_holds(self):
        machine = Machine([entry(B, "Peer")], zones=[{"peer": protocol.id_text(B), "kind": "edge"}])
        self.assertEqual(machine.book.zones(), [{"peer": protocol.id_text(B), "kind": "edge"}])
        machine.book.zones().append({"peer": "x"})
        self.assertEqual(len(machine.book.zones()), 1)

    def test_the_book_stores_a_peers_paired_list_with_its_entry_and_only_when_it_changed(self):
        machine = Machine([entry(B, "Peer")])
        ids = [protocol.id_text(bytes(range(1, 17)))]
        machine.book.store_paired(protocol.id_text(B), ids)
        self.assertEqual(machine.settings.peer(protocol.id_text(B))["paired_with"], ids)
        saves = machine.settings.saves
        machine.book.store_paired(protocol.id_text(B), ids)
        self.assertEqual(machine.settings.saves, saves)
        machine.book.store_paired("nobody", ids)
        self.assertEqual(machine.settings.saves, saves)

    def test_the_responder_gives_a_linked_peers_capabilities(self):
        machine = Machine([entry(B, "Peer")]).start()
        self.addCleanup(machine.stop)
        self.assertIsNone(machine.responder.caps_of(B))
        initiator = Initiator(machine)
        self.addCleanup(initiator.close)
        initiator.handshake(initiator.hello(caps=["settings", "text"]))
        self.assertTrue(wait_for(lambda: machine.responder.caps_of(B) is not None))
        self.assertEqual(machine.responder.caps_of(B), frozenset({"settings", "text"}))


if __name__ == "__main__":
    unittest.main()
