"""A phone written from WIRE.md and PAIRING.md, against the real desktop sockets."""

import logging
import unittest

from core.pairing import PairingService
from core.tests.responder_harness import Machine, entry, ident, wait_for
from core.tests.spec_phone import Phone, PhoneLink, b64


PHONE, MAC, PC = ident(130), ident(160), ident(190)
LOGGER = logging.getLogger("phone-contract-tests")
LOGGER.handlers = [logging.NullHandler()]
LOGGER.propagate = False


class PhoneContractTests(unittest.TestCase):
    def desktop(self, own, name, platform, peers=(), zones=()):
        machine = Machine(peers, zones, own=own, name=name).start()
        self.addCleanup(machine.stop)

        def store(peer, replaces):
            held = machine.settings.load()
            if replaces is not None:
                held["peers"] = [p for p in held["peers"] if p["id"] != replaces["id"]]
            held["peers"].append(dict(peer))
            machine.settings.save(held)

        service = PairingService(
            name, own, platform, lambda: machine.port,
            lambda: machine.settings.load()["peers"], store,
            logger=LOGGER,
            bind_port=0, tcp_port=0, announce_to=("127.0.0.1", 0),
        )
        service.start()
        self.addCleanup(service.stop)
        self.assertTrue(wait_for(lambda: service.listening_port is not None))
        machine.pairing = service
        return machine

    def pair(self, phone, machine, qr=False):
        code = machine.pairing.begin_pairing()
        secret = machine.pairing.host.qr_secret if qr else None
        peer = phone.pair(machine.pairing.tcp_port, code, qr_secret=secret)
        saved = next(p for p in machine.settings.load()["peers"] if p["id"] == b64(PHONE))
        self.assertEqual(peer["token"], saved["token"])
        self.assertEqual((saved["port"], saved["host"], saved["send"], saved["allow_drive"]),
                         (0, "", False, True))
        self.assertEqual((saved["name"], saved["platform"]), (phone.name, phone.platform))
        return peer

    def link(self, phone, machine):
        link = phone.connect(b64(machine.own))
        self.addCleanup(link.close)
        self.assertTrue(link.welcome["accepts"])
        return link

    def test_phone_pairs_drives_switches_and_is_revoked_over_real_tcp(self):
        for platform in ("ios", "android"):
            with self.subTest(platform=platform):
                phone = Phone(PHONE, "Alex's phone", platform)
                mac = self.desktop(MAC, "Loop Mac", "macos", [
                    entry(PC, "Loop PC", side="right", token=b64(bytes(range(32)))),
                ], [{"peer": b64(PC), "kind": "edge"}])
                pc = self.desktop(PC, "Loop PC", "windows")
                self.pair(phone, mac)
                self.pair(phone, pc)
                mac_link, pc_link = self.link(phone, mac), self.link(phone, pc)

                # Input before ownership is dropped and acknowledged.
                mac_link.input("text", text="not owned")
                self.assertIsNotNone(mac_link.expect("ack"))
                self.assertNotIn(("text", "not owned"), mac.injected())
                phone.take(b64(MAC))
                self.assertEqual(mac_link.expect("accept"), {"route": 1})
                self.assertTrue(wait_for(lambda: mac.responder.owner == PHONE))
                mac_link.input("text", text="Phone input")
                mac_link.input("keydown", key="a")
                mac_link.input("mousemove", dx=3, dy=4)
                self.assertTrue(wait_for(lambda: ("text", "Phone input") in mac.injected()))
                self.assertTrue(wait_for(lambda: ("key", "a", True) in mac.injected()))
                self.assertTrue(wait_for(lambda: ("move", 3, 4) in mac.injected()))

                # A desktop's own zone asks the phone to move to its direct link to the PC.
                mac.desktop.cursor = (1919, 540)
                for _ in range(5):
                    mac_link.input("mousemove", dx=200, dy=0)
                switch = mac_link.expect("switch")
                self.assertIsNotNone(switch)
                self.assertEqual((switch["route"], switch["next"]), (1, b64(PC)))
                phone.follow_switch(b64(MAC), switch)
                self.assertTrue(wait_for(lambda: mac.responder.owner is None))
                self.assertTrue(wait_for(lambda: pc.responder.owner == PHONE))
                self.assertTrue(wait_for(lambda: ("key", "a", False) in mac.injected()))
                pc_link.input("text", text="After switch")
                pc_link.input("keydown", key="b")
                self.assertTrue(wait_for(lambda: ("text", "After switch") in pc.injected()))
                self.assertTrue(wait_for(lambda: ("key", "b", True) in pc.injected()))

                # Removing the entry ends ownership and makes the old key id unknown.
                pc.settings.data["peers"] = [p for p in pc.settings.data["peers"] if p["id"] != b64(PHONE)]
                pc.responder.peers_changed()
                self.assertTrue(wait_for(lambda: pc.responder.owner is None))
                self.assertTrue(wait_for(lambda: ("key", "b", False) in pc.injected()))
                self.assertTrue(pc_link.closed.wait(2))
                with self.assertRaises((EOFError, ConnectionError)):
                    PhoneLink(phone, phone.peers[b64(PC)])
                mac_link.close()
                pc_link.close()
                mac.pairing.stop()
                pc.pairing.stop()
                mac.stop()
                pc.stop()

    def test_phone_pairs_using_the_qr_password_without_production_pairing_helpers(self):
        phone = Phone(PHONE, "QR phone", "ios")
        mac = self.desktop(MAC, "Loop Mac", "macos")
        self.pair(phone, mac, qr=True)
        link = self.link(phone, mac)
        phone.take(b64(MAC))
        self.assertEqual(link.expect("accept"), {"route": 1})
        link.input("mousedown", button="left")
        self.assertTrue(wait_for(lambda: ("button", "left", True) in mac.injected()))
        self.assertTrue(mac.responder.send_home())
        switch = link.expect("switch")
        self.assertEqual(switch, {"route": 1, "next": b64(PHONE)})
        phone.follow_switch(b64(MAC), switch)
        self.assertIsNone(phone.on)
        self.assertTrue(wait_for(lambda: mac.responder.owner is None))
        self.assertTrue(wait_for(lambda: ("button", "left", False) in mac.injected()))

    def test_phone_in_use_and_drive_permissions_are_enforced(self):
        phone = Phone(PHONE, "Phone", "ios")
        mac = self.desktop(MAC, "Loop Mac", "macos")
        self.pair(phone, mac)
        link = self.link(phone, mac)
        phone.take(b64(MAC))
        self.assertEqual(link.expect("accept"), {"route": 1})
        saved = mac.settings.data["peers"][0]
        saved["allow_drive"] = False
        mac.responder.peers_changed()
        self.assertEqual(link.expect("accepts"), {"accepts": False})
        self.assertEqual(link.expect("refuse"), {"route": 1, "why": "not_allowed"})
        self.assertTrue(wait_for(lambda: mac.responder.owner is None))
        phone.take(b64(MAC))
        self.assertEqual(link.expect("refuse"), {"route": 2, "why": "not_allowed"})
        saved["in_use"] = False
        mac.responder.peers_changed()
        self.assertTrue(link.closed.wait(2))
        with self.assertRaisesRegex(ValueError, "invalid_hello"):
            PhoneLink(phone, phone.peers[b64(MAC)])


if __name__ == "__main__":
    unittest.main()
