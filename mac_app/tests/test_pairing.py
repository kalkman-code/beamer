import logging
import socket
import threading
import time
import unittest
from unittest import mock
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import pairing
from core.pairing import Announcer, Discovery, PairingHost




def quiet_logger():
    logger = logging.getLogger("pairing-tests")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class LoopbackTests(unittest.TestCase):
    """The two socket loops against each other on 127.0.0.1, with the broadcast address
    replaced so nothing leaves this machine."""

    def setUp(self):
        self.pc_port = 0
        self.mac_port = 0
        self.paired = []
        self.announcer = Announcer(
            lambda: 51820,
            lambda token, name, address: self.paired.append((token, name, address)),
            logger=quiet_logger(),
            bind_port=0,
            announce_to=("127.0.0.1", 0),
            name="TEST-PC",
        )
        self.discovery = Discovery(logger=quiet_logger(), bind_port=0)
        self.discovery.start()
        self.announcer.start()
        self.addCleanup(self.announcer.stop)
        self.addCleanup(self.discovery.stop)
        self.assertTrue(wait_until(lambda: self.announcer.listening_port is not None))
        self.assertTrue(wait_until(lambda: self.discovery.listening_port is not None))
        self.pc_port = self.announcer.listening_port
        self.mac_port = self.discovery.listening_port
        self.announcer.announce_to = ("127.0.0.1", self.mac_port)

    def test_discovers_pairs_and_stores_one_token_each_side(self):
        self.assertTrue(wait_until(lambda: self.discovery.pcs()))
        pc = self.discovery.pcs()[0]
        self.assertEqual((pc["name"], pc["port"], pc["address"], pc["reply_port"]), ("TEST-PC", 51820, "127.0.0.1", self.pc_port))
        self.assertIsNone(pc["pair_id"])
        code = self.announcer.begin_pairing()
        self.assertTrue(wait_until(lambda: self.discovery.pcs()[0]["pair_id"] is not None))
        pc = self.discovery.pcs()[0]
        token, host_name = self.discovery.pair(pc, code, name="Loopback Mac")
        self.assertEqual(host_name, "TEST-PC")
        self.assertTrue(wait_until(lambda: self.paired))
        self.assertEqual(self.paired[0], (token, "Loopback Mac", "127.0.0.1"))
        self.assertIsNone(self.announcer.code)

    def test_wrong_code_over_the_wire_is_refused(self):
        self.assertTrue(wait_until(lambda: self.discovery.pcs()))
        code = self.announcer.begin_pairing()
        self.assertTrue(wait_until(lambda: self.discovery.pcs()[0]["pair_id"] is not None))
        wrong = str((int(code) + 1) % 10**6).zfill(6)
        with self.assertRaises(pairing.PairingError) as caught:
            self.discovery.pair(self.discovery.pcs()[0], wrong)
        self.assertEqual(str(caught.exception), pairing.ERROR_REFUSED)
        # The Mac's abort is what tells the PC, so its window can say a wrong code was entered.
        self.assertTrue(wait_until(lambda: self.announcer.outcome == "refused"))
        self.assertEqual(self.paired, [])
        self.assertIsNone(self.announcer.code)

    def test_a_used_code_does_not_pair_a_second_mac(self):
        self.assertTrue(wait_until(lambda: self.discovery.pcs()))
        code = self.announcer.begin_pairing()
        self.assertTrue(wait_until(lambda: self.discovery.pcs()[0]["pair_id"] is not None))
        pc = self.discovery.pcs()[0]
        self.discovery.pair(pc, code, name="First Mac")
        with self.assertRaises(pairing.PairingError) as caught:
            self.discovery.pair(pc, code, name="Second Mac")
        self.assertEqual(str(caught.exception), pairing.ERROR_NOT_PAIRING)
        self.assertTrue(wait_until(lambda: self.paired))
        self.assertEqual([name for _token, name, _address in self.paired], ["First Mac"])

    def test_pairing_without_a_code_on_the_pc_says_so(self):
        self.assertTrue(wait_until(lambda: self.discovery.pcs()))
        pc = dict(self.discovery.pcs()[0], pair_id="00000000")
        with self.assertRaises(pairing.PairingError) as caught:
            self.discovery.pair(pc, "123456")
        self.assertEqual(str(caught.exception), pairing.ERROR_NOT_PAIRING)

    def test_a_good_answer_leaves_a_fixed_time_after_its_start(self):
        # However quickly the code's generator was worked out, the answer says nothing of it.
        self.assertTrue(wait_until(lambda: self.discovery.pcs()))
        code = self.announcer.begin_pairing()
        requester = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        requester.bind(("127.0.0.1", 0))
        requester.settimeout(2.0)
        self.addCleanup(requester.close)
        client = pairing.PairingClient(self.announcer.host.pair_id, code, "Timing Mac")
        sent = time.monotonic()
        requester.sendto(pairing.encode(client.start()), ("127.0.0.1", self.pc_port))
        data, _address = requester.recvfrom(2048)
        self.assertGreaterEqual(time.monotonic() - sent, pairing.PAIR_ANSWER_SECONDS * 0.9)
        self.assertTrue(pairing.decode(data)["ok"])
        # The same datagram again is answered from memory, and those are not held back: a
        # stream of replays would otherwise stall the loop that has the confirmation to read.
        sent = time.monotonic()
        for _ in range(40):
            requester.sendto(pairing.encode(client.start()), ("127.0.0.1", self.pc_port))
        for _ in range(40):
            requester.recvfrom(2048)
        self.assertLess(time.monotonic() - sent, pairing.PAIR_ANSWER_SECONDS * 20)

    def test_refusals_are_rationed_so_the_port_is_no_amplifier(self):
        # A beacon heard is proof both loops have bound their sockets.
        self.assertTrue(wait_until(lambda: self.discovery.pcs()))
        stranger = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        stranger.bind(("127.0.0.1", 0))
        stranger.settimeout(1.0)
        self.addCleanup(stranger.close)
        junk = pairing.encode({"type": "pair_start", "pair": "nobody"})
        for _ in range(20):
            stranger.sendto(junk, ("127.0.0.1", self.pc_port))
        replies = 0
        while True:
            try:
                stranger.recvfrom(2048)
            except socket.timeout:
                break
            replies += 1
        self.assertEqual(replies, 1)

    def test_replies_nobody_is_waiting_for_are_not_kept(self):
        self.assertTrue(wait_until(lambda: self.discovery.pcs()))
        stranger = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(stranger.close)
        for _ in range(50):
            stranger.sendto(pairing.encode({"type": "pair_answer", "pair": "nobody", "ok": True}), ("127.0.0.1", self.mac_port))
        # A beacon sent after them is proof the loop has read them all.
        stranger.sendto(pairing.encode(pairing.beacon_msg("LATER-PC", 51820)), ("127.0.0.1", self.mac_port))
        self.assertTrue(wait_until(lambda: any(pc["name"] == "LATER-PC" for pc in self.discovery.pcs())))
        self.assertEqual(self.discovery._replies.qsize(), 0)


class ImpostorTests(unittest.TestCase):
    """Something on the network answering in the PC's place, over a real socket."""

    def setUp(self):
        self.impostor = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.impostor.bind(("127.0.0.1", 0))
        self.impostor.settimeout(5.0)
        self.addCleanup(self.impostor.close)
        self.discovery = Discovery(logger=quiet_logger(), bind_port=0)
        self.discovery.start()
        self.addCleanup(self.discovery.stop)
        self.assertTrue(wait_until(lambda: self.discovery.listening_port is not None))
        self.real = PairingHost(name="REAL-PC")
        self.code = self.real.begin()
        self.pc = {
            # Not the name the host will prove: a beacon can say anything.
            "name": "BEACON-NAME",
            "address": "127.0.0.1",
            "port": 51820,
            "reply_port": self.impostor.getsockname()[1],
            "pair_id": self.real.pair_id,
            "pairing": pairing.PAIRING_VERSION,
        }

    def _pair_in_background(self):
        results = []

        def work():
            try:
                results.append(self.discovery.pair(self.pc, self.code, name="Test Mac"))
            except pairing.PairingError as exc:
                results.append(exc)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return thread, results

    def test_the_first_answer_decides_and_a_wrong_one_is_never_confirmed(self):
        thread, results = self._pair_in_background()
        data, mac = self.impostor.recvfrom(2048)
        good = self.real.handle(pairing.decode(data))
        forged = dict(good, tag=pairing._b64(bytes(pairing.TAG_BYTES)))
        # The forgery first, then the answer that would have passed: the Mac must not wait
        # for it, or every answer sent would be another guess at the code.
        self.impostor.sendto(pairing.encode(forged), mac)
        self.impostor.sendto(pairing.encode(good), mac)
        thread.join(5.0)
        self.assertEqual([str(result) for result in results], [pairing.ERROR_REFUSED])
        data, _mac = self.impostor.recvfrom(2048)
        abort = pairing.decode(data)
        self.assertEqual((abort["type"], abort["pair"], abort["tag"]), ("pair_confirm", self.real.pair_id, ""))
        # That code is now spent on this Mac: trying it again sends nothing, so whoever answered
        # gets no second guess out of a second try, even behind a beacon with a new pairing id.
        for pc in (self.pc, dict(self.pc, pair_id="ab" * 16)):
            with self.assertRaises(pairing.PairingError) as caught:
                self.discovery.pair(pc, self.code, name="Test Mac")
            self.assertEqual(str(caught.exception), pairing.ERROR_REFUSED)
        self.impostor.settimeout(0.5)
        with self.assertRaises(socket.timeout):
            self.impostor.recvfrom(2048)

    def test_a_refusal_from_the_pc_does_not_spend_the_code(self):
        # A stale pairing id draws not_pairing from an honest PC, and tests no guess.
        thread, results = self._pair_in_background()
        data, mac = self.impostor.recvfrom(2048)
        self.impostor.sendto(pairing.encode({"type": "pair_answer", "pair": self.real.pair_id, "ok": False, "error": "not_pairing"}), mac)
        thread.join(5.0)
        self.assertEqual([str(result) for result in results], [pairing.ERROR_NOT_PAIRING])
        thread, results = self._pair_in_background()
        data, mac = self.impostor.recvfrom(2048)
        self.assertEqual(pairing.decode(data)["type"], "pair_start")
        self.impostor.sendto(pairing.encode(self.real.handle(pairing.decode(data))), mac)
        data, mac = self.impostor.recvfrom(2048)
        self.impostor.sendto(pairing.encode(self.real.handle(pairing.decode(data))), mac)
        thread.join(5.0)
        self.assertEqual(results, [(self.real.paired[0], "REAL-PC")])

    def test_a_spent_code_may_be_sent_again_once_it_could_no_longer_be_live(self):
        self.discovery._failed[self.code] = self.discovery.clock() - 1.0
        thread, _results = self._pair_in_background()
        data, _mac = self.impostor.recvfrom(2048)
        self.assertEqual(pairing.decode(data)["type"], "pair_start")
        self.assertEqual(self.discovery._failed, {})

    def test_a_forged_receipt_does_not_yield_a_token(self):
        thread, results = self._pair_in_background()
        data, mac = self.impostor.recvfrom(2048)
        self.impostor.sendto(pairing.encode(self.real.handle(pairing.decode(data))), mac)
        data, mac = self.impostor.recvfrom(2048)
        confirm = pairing.decode(data)
        self.assertEqual(confirm["type"], "pair_confirm")
        forged = {"type": "pair_done", "pair": confirm["pair"], "ok": True, "tag": confirm["tag"]}
        self.impostor.sendto(pairing.encode(forged), mac)
        thread.join(5.0)
        self.assertEqual([str(result) for result in results], [pairing.ERROR_REFUSED])


class FindTests(unittest.TestCase):
    """A PC whose beacons never reach the Mac, found by its address instead."""

    def setUp(self):
        self.paired = []
        self.announcer = Announcer(
            lambda: 51820,
            lambda token, name, address: self.paired.append((token, name, address)),
            logger=quiet_logger(),
            bind_port=0,
            announce_to=("127.0.0.1", 0),
            name="FAR-PC",
        )
        self.discovery = Discovery(logger=quiet_logger(), bind_port=0)
        self.discovery.start()
        self.announcer.start()
        self.addCleanup(self.announcer.stop)
        self.addCleanup(self.discovery.stop)
        self.assertTrue(wait_until(lambda: self.announcer.listening_port is not None))
        self.assertTrue(wait_until(lambda: self.discovery.listening_port is not None))
        self.pc_port = self.announcer.listening_port
        self.mac_port = self.discovery.listening_port

    def test_a_pc_no_beacon_reaches_is_found_by_address_and_pairs(self):
        self.assertFalse(wait_until(lambda: self.discovery.pcs(), timeout=0.6))
        self.discovery.find("127.0.0.1", self.pc_port)
        self.assertTrue(wait_until(lambda: self.discovery.pcs()))
        self.assertEqual(self.discovery.pcs()[0]["name"], "FAR-PC")
        code = self.announcer.begin_pairing()
        self.assertTrue(wait_until(lambda: self.discovery.pcs()[0]["pair_id"] is not None, timeout=4.0))
        token, _host_name = self.discovery.pair(self.discovery.pcs()[0], code, name="Far Mac")
        self.assertTrue(wait_until(lambda: self.paired))
        self.assertEqual(self.paired[0], (token, "Far Mac", "127.0.0.1"))

    def test_an_empty_address_stops_asking(self):
        self.discovery.find("127.0.0.1", self.pc_port)
        self.assertTrue(wait_until(lambda: self.discovery._finding))
        self.discovery.find("  ")
        self.assertIsNone(self.discovery._finding)

    def test_a_name_that_never_resolves_does_not_hold_up_beacons(self):
        # The beacon below is sent once, so the loop must have its socket before it goes.
        self.assertTrue(wait_until(lambda: self.discovery.listening_port is not None))
        resolving = threading.Event()
        real = socket.getaddrinfo

        def slow(host, *args, **kwargs):
            if host == "slow.invalid":
                resolving.set()
                time.sleep(3.0)
                raise socket.gaierror("no such name")
            return real(host, *args, **kwargs)

        with mock.patch.object(pairing.socket, "getaddrinfo", slow):
            self.discovery.find("slow.invalid")
            self.assertTrue(resolving.wait(1.0))
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.addCleanup(sender.close)
            sender.sendto(pairing.encode(pairing.beacon_msg("NEAR-PC", 51820)), ("127.0.0.1", self.mac_port))
            self.assertTrue(wait_until(lambda: self.discovery.pcs(), timeout=1.0))


if __name__ == "__main__":
    unittest.main()
