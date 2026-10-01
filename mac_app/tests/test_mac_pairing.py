"""The Mac's settings store behind a real PairingService, both ways, over loopback: the store and
peers the app hands the service are the ones the pairing is checked and kept in."""

import json
import logging
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import bridge
import settings_store
from bridge_fakes import PAIRED_TOKEN
from core import pairing
from core.tests.test_pairing_service import Machine, free_port, wait_until
from core.tests.test_pairing_v3 import entry

PC_ID = bytes(range(101, 117))
MAC_ID = bytes(range(1, 17))


class MacPairingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "config.json"
        raw = settings_store.config_to_raw(settings_store.editable_default_config())
        raw.update(host="127.0.0.1", auth_token=PAIRED_TOKEN, pc_name="Old PC")
        path.write_text(json.dumps(raw))
        self.store = settings_store.SettingsStore(path)
        self.store.load()
        self.book, self.identity = bridge.links_from_store(self.store, "1.5.0")
        self.paired = []
        udp, tcp = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_STREAM)
        self.tcp_port = tcp
        ident = self.identity()
        logger = logging.getLogger("test-mac-pairing")
        logger.handlers = [logging.NullHandler()]
        self.service = pairing.PairingService(
            ident["name"], bytes(ident["id"]), ident["platform"], lambda: 24820, self.book.peers, self.store.add_peer,
            on_paired=self.paired.append, logger=logger, bind_port=udp, tcp_port=tcp,
            announce_to=("127.0.0.1", free_port(socket.SOCK_DGRAM)),
        )
        self.service.start()
        self.addCleanup(self.service.stop)
        self.pc = Machine(self, "Desk PC", PC_ID, "windows")

    def test_a_pairing_this_mac_enters_replaces_the_1_4_entry_at_that_address_in_the_store(self):
        before = self.store.current()["peers"][0]
        self.assertTrue(before["from_1_4"])
        code = self.pc.service.begin_pairing()
        saved = self.service.pair_by_address("127.0.0.1", code, port=self.pc.tcp_port)
        peers = self.store.current()["peers"]
        self.assertEqual([peer["token"] for peer in peers], [saved["token"]])
        self.assertEqual((peers[0]["name"], peers[0]["platform"]), ("Desk PC", "windows"))
        self.assertEqual(self.store.load().auth_token, saved["token"])
        self.assertEqual(self.store.current()["zones"], [])
        self.assertTrue(wait_until(lambda: self.pc.paired))

    def test_a_pairing_this_mac_hosts_is_stored_after_the_machines_it_has(self):
        # A 1.4.x entry at the requester's own address would be replaced by it; this one is elsewhere.
        settings = self.store.current()
        settings["peers"][0]["host"] = "192.0.2.20"
        self.store.save_settings(settings)
        code = self.service.begin_pairing()
        saved = self.pc.service.pair_by_address("127.0.0.1", code, port=self.tcp_port)
        self.assertTrue(wait_until(lambda: self.paired))
        peers = self.store.current()["peers"]
        self.assertEqual([peer["token"] for peer in peers], [PAIRED_TOKEN, saved["token"]])
        self.assertEqual(self.store.load().auth_token, PAIRED_TOKEN)
        self.assertEqual(self.service.outcome, "paired")

    def test_a_machine_already_paired_is_refused_by_this_mac_and_the_store_is_untouched(self):
        self.store.add_peer({**settings_store.PEER_DEFAULTS, **entry(PC_ID, name="Desk PC", paired_at=1_700_000_000), "port": 24820, "host": "192.0.2.77"})
        before = self.store.current()["peers"]
        code = self.pc.service.begin_pairing()
        with self.assertRaises(pairing.AlreadyPaired) as caught:
            self.service.pair_by_address("127.0.0.1", code, port=self.pc.tcp_port)
        self.assertEqual(caught.exception.entry["name"], "Desk PC")
        self.assertEqual(self.store.current()["peers"], before)


if __name__ == "__main__":
    unittest.main()
