"""Pairing over a machine with more than one way to the other (beta.4).

On 01-10-2026 the spare laptop took a VPN's subnet route for its own home network, so its UDP answer
left from the VPN's address rather than the Wi-Fi address the request had come in on, and the
requester, which took an answer only from the address it asked, ignored it: both directions said
"did not answer". Now the host answers from the address on the requester's own network where it has
one, and the requester takes an answer by its pairing id and its crypto, from any address.

The beacons' directed broadcasts used to assume a /24; they now follow each interface's netmask.
"""

import ipaddress
import socket
import threading
import unittest
from unittest import mock

from core import pairing
from core.pairing import PairingError

from .test_pairing_service import MAC_ID, PC_ID, Machine, free_port, wait_until


def _can_bind(address):
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((address, 0))
        return True
    except OSError:
        return False
    finally:
        probe.close()


class BroadcastTargetTests(unittest.TestCase):
    def test_each_interface_is_sent_the_broadcast_of_its_own_netmask(self):
        interfaces = [("192.168.1.20", "255.255.255.0"), ("169.254.3.7", "255.255.0.0"),
                      ("10.1.2.5", "255.255.255.252"), ("172.20.9.9", "255.240.0.0")]
        self.assertEqual(pairing.broadcast_targets(47000, interfaces), [
            ("255.255.255.255", 47000), ("192.168.1.255", 47000), ("169.254.255.255", 47000),
            ("10.1.2.7", 47000), ("172.31.255.255", 47000),
        ])

    def test_two_addresses_on_one_network_send_it_one_broadcast(self):
        interfaces = [("192.168.1.20", "255.255.255.0"), ("192.168.1.21", "255.255.255.0")]
        self.assertEqual(pairing.broadcast_targets(1, interfaces), [("255.255.255.255", 1), ("192.168.1.255", 1)])

    def test_with_no_interfaces_known_it_falls_back_to_a_24(self):
        with mock.patch.object(pairing, "_local_ipv4_addresses", return_value=["10.9.8.7"]):
            self.assertEqual(pairing.broadcast_targets(1, []), [("255.255.255.255", 1), ("10.9.8.255", 1)])


class InterfaceTests(unittest.TestCase):
    def test_this_machine_s_interfaces_are_read_with_real_netmasks(self):
        found = pairing._interfaces()
        if not found:
            self.skipTest("no IPv4 network here")
        for address, netmask in found:
            network = ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)
            self.assertFalse(ipaddress.IPv4Address(address).is_loopback, address)
            self.assertLess(network.prefixlen, 31, (address, netmask))
        # Checked against the routing table, which knows nothing of how the list was read.
        routed = pairing.pairing_address()
        if routed is not None:
            self.assertIn(routed, [address for address, _netmask in found])

    def test_a_platform_that_will_not_say_gives_none(self):
        failing = mock.patch.object(pairing, "_posix_interfaces" if pairing.sys.platform != "win32" else "_windows_interfaces",
                                    side_effect=OSError("no"))
        with failing:
            self.assertEqual(pairing._interfaces(), [])


class AnswerSourceTests(unittest.TestCase):
    INTERFACES = [("192.168.50.22", "255.255.255.0"), ("192.168.56.1", "255.255.255.0")]

    def test_a_requester_on_one_of_this_machine_s_networks_is_answered_from_that_network(self):
        source = pairing._answer_source("192.168.50.5", self.INTERFACES, towards=lambda host: "100.64.0.7")
        self.assertEqual(source, "192.168.50.22")

    def test_where_the_routing_table_agrees_nothing_changes(self):
        self.assertIsNone(pairing._answer_source("192.168.50.5", self.INTERFACES, towards=lambda host: "192.168.50.22"))

    def test_two_addresses_on_the_requester_s_network_leave_the_routing_table_s_choice_alone(self):
        interfaces = [("192.168.50.10", "255.255.255.0"), ("192.168.50.20", "255.255.255.0")]
        self.assertIsNone(pairing._answer_source("192.168.50.5", interfaces, towards=lambda host: "192.168.50.20"))
        self.assertEqual(pairing._answer_source("192.168.50.5", interfaces, towards=lambda host: "100.64.0.7"), "192.168.50.10")

    def test_a_requester_on_no_network_of_this_machine_s_is_routed_as_ever(self):
        self.assertIsNone(pairing._answer_source("203.0.113.9", self.INTERFACES, towards=lambda host: "100.64.0.7"))
        self.assertIsNone(pairing._answer_source("not an address", self.INTERFACES, towards=lambda host: "x"))


class RequesterTakesAnAnswerFromAnyAddressTests(unittest.TestCase):
    def setUp(self):
        mac_udp, pc_udp = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
        self.mac = Machine(self, "Loop Mac", MAC_ID, "macos", peer_udp=pc_udp, udp_port=mac_udp)
        self.pc = Machine(self, "Loop PC", PC_ID, "windows", peer_udp=mac_udp, udp_port=pc_udp)

    def _answer_from(self, address):
        """The PC's answers leave from `address`, as a VPN's route sent the laptop's."""
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        out.bind((address, 0))
        self.addCleanup(out.close)
        sent = []

        def answer(sock, data, to):
            sent.append(to)
            out.sendto(data, to)

        self.pc.service._answer = answer
        return sent

    def _pair(self):
        code = self.pc.service.begin_pairing()
        self.assertTrue(wait_until(lambda: any(m["pair_id"] for m in self.mac.service.machines())))
        seen = [m for m in self.mac.service.machines() if m["pair_id"]][0]
        return seen, code

    def test_an_answer_from_another_address_still_pairs(self):
        # macOS's loopback holds 127.0.0.1 alone; a LAN address reaches it as well.
        stand_in = "127.0.0.2" if _can_bind("127.0.0.2") else pairing.pairing_address()
        if stand_in is None:
            self.skipTest("no second address to answer from")
        sent = self._answer_from(stand_in)
        seen, code = self._pair()
        saved = self.mac.service.pair(seen, code)
        self.assertTrue(sent, "the PC answered through the stand-in")
        self.assertEqual(saved["host"], "127.0.0.1", "the entry keeps the address the beacon came from")
        self.assertTrue(wait_until(lambda: self.pc.paired))
        self.assertEqual(self.pc.paired[0]["token"], saved["token"])

    def test_an_answer_for_another_pairing_id_is_not_taken(self):
        service = self.mac.service
        service._awaiting = "ours"
        for message in ({"type": "pair_answer", "pair": "theirs"}, {"type": "pair_answer"}):
            service._dispatch(None, message, ("192.0.2.9", 1), 0.0, {}, {})
        self.assertTrue(service._replies.empty())
        service._dispatch(None, {"type": "pair_answer", "pair": "ours"}, ("192.0.2.9", 1), 0.0, {}, {})
        self.assertEqual(service._replies.get_nowait()[1], ("192.0.2.9", 1))

    def test_nothing_is_taken_while_no_attempt_is_under_way(self):
        service = self.mac.service
        service._dispatch(None, {"type": "pair_answer"}, ("192.0.2.9", 1), 0.0, {}, {})
        service._dispatch(None, {"type": "pair_answer", "pair": None}, ("192.0.2.9", 1), 0.0, {}, {})
        self.assertTrue(service._replies.empty())

    def _forge(self, forged):
        """Each reply in `forged`, by type, reaches the requester from a stranger's address just
        before the host's own."""
        service = self.mac.service
        real_ask = service._ask

        def ask(send, receive, target, pair_id, message, reply_type, proves=None):
            for reply in forged.get(reply_type, ()):
                service._replies.put((dict(reply, pair=pair_id), ("192.0.2.9", 4)))
            return real_ask(send, receive, target, pair_id, message, reply_type, proves)

        service._ask = ask

    def test_forged_answers_from_another_address_lose_to_the_host_s_own(self):
        seen, code = self._pair()
        good = {"type": "pair_answer", "ok": True, "share": pairing._b64(bytes(32)), "tag": pairing._b64(bytes(32)),
                "id": pairing._b64(PC_ID), "name": "Loop PC", "platform": "windows", "port": 24820}
        self._forge({"pair_answer": [good, {"type": "pair_answer", "ok": False, "error": "refused"}]})
        saved = self.mac.service.pair(seen, code)
        self.assertNotIn(code, self.mac.service._failed, "no forgery was judged")
        self.assertTrue(wait_until(lambda: self.pc.paired))
        self.assertEqual(self.pc.paired[0]["token"], saved["token"])

    def test_a_forged_receipt_from_any_address_leaves_no_half_pairing(self):
        seen, code = self._pair()
        self._forge({"pair_done": [{"type": "pair_done", "ok": True, "tag": pairing._b64(bytes(32))},
                                   {"type": "pair_done", "ok": True, "tag": "not base64!"}]})
        saved = self.mac.service.pair(seen, code)
        self.assertEqual(len(self.mac.book.entries), 1)
        self.assertTrue(wait_until(lambda: self.pc.paired))
        self.assertEqual(self.pc.paired[0]["token"], saved["token"])

    def test_with_nothing_from_the_host_a_refusal_from_elsewhere_still_ends_the_attempt(self):
        service = self.mac.service
        service.clock = iter(range(0, 10**6, 1)).__next__
        replies = iter([({"type": "pair_answer", "pair": "p", "ok": False, "error": "busy"}, ("192.0.2.9", 4)), None])
        reply = service._ask(lambda data: None, lambda timeout: next(replies, None), ("127.0.0.1", 1), "p",
                             {"type": "pair_start"}, "pair_answer")
        self.assertEqual(reply["error"], "busy")


class AskTests(unittest.TestCase):
    """_ask on its own, with replies handed in as they would arrive."""

    TARGET = ("192.168.50.3", 47000)
    ELSEWHERE = ("192.0.2.9", 4)

    def ask(self, arrivals, reply_type="pair_answer", proves=None, windows=None):
        """`arrivals`: per window, the (reply, address) pairs that come in it; the window then ends."""
        service = pairing.Discovery(logger=None)
        now = [0.0]
        service.clock = lambda: now[0]
        windows = [list(window) for window in arrivals]
        queued = []

        def send(data):
            if windows:
                queued.extend(windows.pop(0))

        def receive(timeout):
            if queued:
                return queued.pop(0)
            now[0] += timeout
            return None

        return service._ask(send, receive, self.TARGET, "p", {"type": "pair_start"}, reply_type, proves)

    def reply(self, ok=True, **fields):
        return dict({"type": "pair_answer", "pair": "p", "ok": ok}, **fields)

    def test_the_asked_address_wins_even_when_its_reply_is_queued_behind_the_window_s_end(self):
        service = pairing.Discovery(logger=None)
        now = [0.0]
        service.clock = lambda: now[0]
        queued = [(self.reply(name="forged"), self.ELSEWHERE), (self.reply(name="real"), self.TARGET)]

        def receive(timeout):
            # The forgery is read just as the window closes; the real answer is already waiting.
            now[0] += pairing.PAIR_REPLY_TIMEOUT_SECONDS
            return queued.pop(0) if queued else None

        reply = service._ask(lambda data: None, receive, self.TARGET, "p", {"type": "pair_start"}, "pair_answer")
        self.assertEqual(reply["name"], "real")

    def test_with_nothing_from_the_asked_address_an_ok_reply_from_elsewhere_beats_a_refusal(self):
        reply = self.ask([[(self.reply(ok=False, error="busy"), self.ELSEWHERE), (self.reply(name="ok"), self.ELSEWHERE)]])
        self.assertEqual(reply["name"], "ok")

    def test_only_the_first_of_each_kind_from_elsewhere_is_kept(self):
        flood = [(self.reply(name=str(n)), self.ELSEWHERE) for n in range(500)]
        reply = self.ask([flood])
        self.assertEqual(reply["name"], "0")

    def test_a_bad_receipt_from_elsewhere_is_ignored_and_one_from_the_asked_address_decides(self):
        done = lambda ok=True, tag="bad": {"type": "pair_done", "pair": "p", "ok": ok, "tag": tag}
        proves = lambda reply: reply["tag"] == "good"
        reply = self.ask([[(done(), self.ELSEWHERE), (done(tag="good"), self.ELSEWHERE)]], "pair_done", proves)
        self.assertEqual(reply["tag"], "good")
        reply = self.ask([[(done(), self.TARGET), (done(tag="good"), self.ELSEWHERE)]], "pair_done", proves)
        self.assertEqual(reply["tag"], "bad", "the asked address decides, and finish() refuses it")
        with self.assertRaises(PairingError):
            self.ask([[(done(), self.ELSEWHERE)], [], []], "pair_done", proves)


class ReceiveTests(unittest.TestCase):
    class Socket:
        def __init__(self, error):
            self.error = error

        def settimeout(self, timeout):
            pass

        def recvfrom(self, size):
            raise self.error

    def test_an_oversized_datagram_on_windows_is_passed_over(self):
        error = OSError(10040, "message too long")
        error.winerror = 10040
        self.assertEqual(pairing._receive_on(self.Socket(error), 1.0), ({}, ("", 0)))

    def test_any_other_failure_ends_the_attempt_as_no_answer(self):
        with self.assertRaises(PairingError) as caught:
            pairing._receive_on(self.Socket(OSError(9, "bad file descriptor")), 1.0)
        self.assertEqual(str(caught.exception), "no_answer")


class OversizedDatagramTests(unittest.TestCase):
    def test_a_datagram_too_large_for_the_buffer_does_not_stop_the_service(self):
        mac_udp, pc_udp = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
        mac = Machine(self, "Loop Mac", MAC_ID, "macos", peer_udp=pc_udp, udp_port=mac_udp)
        Machine(self, "Loop PC", PC_ID, "windows", peer_udp=mac_udp, udp_port=pc_udp)
        self.assertTrue(wait_until(lambda: mac.service.machines()))
        stranger = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(stranger.close)
        for _ in range(3):
            stranger.sendto(b"x" * 4000, ("127.0.0.1", mac_udp))
        mac.service._seen.clear()
        self.assertTrue(wait_until(lambda: mac.service.machines()), "the PC's beacons are still heard")
        self.assertIsNone(mac.service.error)


class HostAnswersFromTheRequesterSNetworkTests(unittest.TestCase):
    def test_both_ends_send_from_the_address_on_the_other_s_network(self):
        mac_udp, pc_udp = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
        mac = Machine(self, "Loop Mac", MAC_ID, "macos", peer_udp=pc_udp, udp_port=mac_udp)
        pc = Machine(self, "Loop PC", PC_ID, "windows", peer_udp=mac_udp, udp_port=pc_udp)
        sent = {}
        real_sendto = socket.socket.sendto

        def sendto(sock, data, address):
            result = real_sendto(sock, data, address)
            kind = (pairing.decode(data) or {}).get("type")
            if kind in ("pair_start", "pair_confirm", "pair_answer", "pair_done"):
                sent.setdefault(kind, []).append((sock.getsockname(), address))
            return result

        code = pc.service.begin_pairing()
        self.assertTrue(wait_until(lambda: any(m["pair_id"] for m in mac.service.machines())))
        seen = [m for m in mac.service.machines() if m["pair_id"]][0]
        # Each machine's routing table would send to 127.0.0.1 from elsewhere; its loopback holds it.
        with mock.patch.object(pairing, "_interfaces", return_value=[("127.0.0.1", "255.0.0.0")]), \
                mock.patch.object(pairing, "local_address_towards", return_value="192.0.2.1"), \
                mock.patch.object(socket.socket, "sendto", sendto):
            saved = mac.service.pair(seen, code)
        (start_from, start_to), = sent["pair_start"][:1]
        self.assertEqual(start_to, ("127.0.0.1", pc_udp))
        self.assertEqual(start_from[0], "127.0.0.1")
        self.assertNotEqual(start_from[1], mac_udp, "the request went on a socket of its own")
        self.assertEqual({source for source, _to in sent["pair_confirm"]}, {start_from})
        for kind in ("pair_answer", "pair_done"):
            for (address, port), to in sent[kind]:
                self.assertEqual((address, to), ("127.0.0.1", start_from), kind)
                self.assertNotEqual(port, pc_udp, "never bound beside the main socket, whose datagrams it could take")
        self.assertTrue(wait_until(lambda: pc.paired))
        self.assertEqual(pc.paired[0]["token"], saved["token"])


if __name__ == "__main__":
    unittest.main()
