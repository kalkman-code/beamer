"""Pairing version 3 on the sockets (WIRE.md section 6): every desktop announces and discovers on
one UDP socket, hosts over UDP and TCP with one PairingHost, and requests over either.

Loopback only: the beacons go to a named port on 127.0.0.1, never the broadcast address.
"""

import json
import logging
import socket
import struct
import threading
import time
import unittest
from unittest import mock

from core import pairing
from core.pairing import AlreadyPaired, PairingClient, PairingError, PairingService

from .test_pairing_v3 import Book, entry

MAC_ID = bytes(range(1, 17))
PC_ID = bytes(range(101, 117))


def quiet_logger():
    logger = logging.getLogger("pairing-service-tests")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


def free_port(kind):
    sock = socket.socket(socket.AF_INET, kind)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def send_frame(sock, message):
    body = pairing.encode(message)
    sock.sendall(struct.pack(">I", len(body)) + body)


def read_frame(sock):
    """The next framed message, or None when the connection closed first."""
    header = b""
    while len(header) < 4:
        chunk = sock.recv(4 - len(header))
        if not chunk:
            return None
        header += chunk
    (length,) = struct.unpack(">I", header)
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body)


def closed_without_a_byte(sock, timeout=2.0):
    sock.settimeout(timeout)
    try:
        return sock.recv(1) == b""
    except ConnectionResetError:
        return True


class Machine:
    def __init__(self, test, name, identity, platform, peer_udp=None, entries=(), udp_port=None):
        self.book = Book(entries)
        self.udp_port = udp_port or free_port(socket.SOCK_DGRAM)
        self.tcp_port = free_port(socket.SOCK_STREAM)
        self.paired = []
        self.service = PairingService(
            name=name, identity=identity, platform=platform, port_getter=lambda: 24820,
            peers=self.book.peers, store=self.book.store, on_paired=self.paired.append,
            logger=quiet_logger(), bind_port=self.udp_port, tcp_port=self.tcp_port,
            announce_to=("127.0.0.1", peer_udp or free_port(socket.SOCK_DGRAM)),
        )
        self.service.start()
        test.addCleanup(self.service.stop)

    def connect(self):
        sock = socket.create_connection(("127.0.0.1", self.tcp_port), timeout=3.0)
        sock.settimeout(3.0)
        return sock


class BothEndsTests(unittest.TestCase):
    def setUp(self):
        mac_udp, pc_udp = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
        self.mac = Machine(self, "Loop Mac", MAC_ID, "macos", peer_udp=pc_udp, udp_port=mac_udp)
        self.pc = Machine(self, "Loop PC", PC_ID, "windows", peer_udp=mac_udp, udp_port=pc_udp)

    def _pair_over_udp(self, host, requester):
        code = host.service.begin_pairing()
        self.assertTrue(wait_until(lambda: any(m["pair_id"] for m in requester.service.machines())))
        seen = [m for m in requester.service.machines() if m["pair_id"]][0]
        saved = requester.service.pair(seen, code)
        self.assertTrue(wait_until(lambda: host.paired))
        return seen, saved

    def test_each_hears_the_other_and_either_can_host(self):
        self.assertTrue(wait_until(lambda: self.mac.service.machines() and self.pc.service.machines()))
        heard = self.mac.service.machines()[0]
        self.assertEqual((heard["name"], heard["platform"], heard["pairing"], heard["port"], heard["id"]), ("Loop PC", "windows", 3, 24820, None))
        self.assertEqual(self.pc.service.machines()[0]["name"], "Loop Mac", "a machine does not list itself")
        self.assertEqual(len(self.pc.service.machines()), 1)

        seen, saved = self._pair_over_udp(self.pc, self.mac)
        self.assertEqual(seen["id"], pairing._b64(PC_ID))
        self.assertEqual((saved["id"], saved["name"], saved["platform"], saved["host"]), (pairing._b64(PC_ID), "Loop PC", "windows", "127.0.0.1"))
        self.assertEqual(self.pc.paired[0]["token"], saved["token"])
        self.assertEqual(self.pc.book.entries[0]["id"], pairing._b64(MAC_ID))

        # The other way round, once each has forgotten the other.
        self.mac.book.entries, self.pc.book.entries = [], []
        _seen, saved = self._pair_over_udp(self.mac, self.pc)
        self.assertEqual((saved["name"], saved["platform"]), ("Loop Mac", "macos"))
        self.assertEqual(self.mac.book.entries[0]["token"], saved["token"])

    def test_an_already_paired_host_is_refused_by_the_requester_over_udp(self):
        self.mac.book.entries.append(entry(PC_ID, name="Loop PC", paired_at=1_700_000_000))
        code = self.pc.service.begin_pairing()
        self.assertTrue(wait_until(lambda: any(m["pair_id"] for m in self.mac.service.machines())))
        seen = [m for m in self.mac.service.machines() if m["pair_id"]][0]
        with self.assertRaises(AlreadyPaired) as caught:
            self.mac.service.pair(seen, code)
        self.assertEqual(caught.exception.entry["paired_at"], 1_700_000_000)
        self.assertTrue(wait_until(lambda: self.pc.service.outcome == "known_there"))
        self.assertEqual(self.pc.book.entries, [])


class OwnBeaconTests(unittest.TestCase):
    def test_a_machine_that_hears_its_own_beacons_does_not_list_itself(self):
        port = free_port(socket.SOCK_DGRAM)
        alone = Machine(self, "Alone", MAC_ID, "macos", peer_udp=port, udp_port=port)
        arrived = []
        original = alone.service._dispatch
        alone.service._dispatch = lambda sock, message, *rest: (arrived.append(message.get("type")), original(sock, message, *rest))
        # Its beacons reach its own socket: the filter, not the addressing, keeps it off the list.
        self.assertTrue(wait_until(lambda: "beacon" in arrived))
        time.sleep(0.2)
        self.assertEqual(alone.service.machines(), [])


class BeaconTests(unittest.TestCase):
    def setUp(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.settimeout(3.0)
        self.addCleanup(self.listener.close)
        self.pc = Machine(self, "Beacon PC", PC_ID, "windows", peer_udp=self.listener.getsockname()[1])

    def _next_beacon(self):
        while True:
            data, _address = self.listener.recvfrom(2048)
            message = pairing.decode(data)
            if message and message.get("type") == "beacon":
                return message

    def test_id_and_pair_only_while_a_code_is_up(self):
        idle = self._next_beacon()
        self.assertEqual(idle, {"beamy": 1, "type": "beacon", "name": "Beacon PC", "port": 24820, "pairing": 3, "platform": "windows"})
        self.pc.service.begin_pairing()
        live = self._next_beacon()
        while "id" not in live:
            live = self._next_beacon()
        self.assertEqual(live["id"], pairing._b64(PC_ID))
        self.pc.service.cancel_pairing()
        after = self._next_beacon()
        while "id" in after:
            after = self._next_beacon()
        self.assertNotIn("pair", after)

    def test_a_pairing_2_beacon_is_reported_older_and_sent_nothing(self):
        mac = Machine(self, "Mac", MAC_ID, "macos")
        old = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        old.bind(("127.0.0.1", 0))
        old.settimeout(0.5)
        self.addCleanup(old.close)
        beacon = pairing.encode({"type": "beacon", "name": "Old PC", "port": 24820, "pairing": 2, "pair": "ab" * 16})
        # Sent until heard, since the service binds its socket on its own thread.
        self.assertTrue(wait_until(lambda: old.sendto(beacon, ("127.0.0.1", mac.udp_port)) and mac.service.machines()))
        seen = mac.service.machines()[0]
        self.assertEqual(seen["pairing"], 2)
        with self.assertRaises(PairingError) as caught:
            mac.service.pair(seen, "123456")
        self.assertEqual(str(caught.exception), pairing.ERROR_VERSION)
        while True:
            try:
                old.recvfrom(2048)
            except socket.timeout:
                break
            except ConnectionResetError:
                # Windows: the port-unreachable of a beacon sent before the service had bound.
                continue
            self.fail("the older machine was sent something")


class TcpTests(unittest.TestCase):
    def setUp(self):
        self.pc = Machine(self, "TCP PC", PC_ID, "windows")
        self.mac = Machine(self, "TCP Mac", MAC_ID, "macos")

    def test_a_whole_exchange_over_tcp_agrees_one_token(self):
        code = self.pc.service.begin_pairing()
        saved = self.mac.service.pair_by_address("127.0.0.1", code, port=self.pc.tcp_port)
        self.assertTrue(wait_until(lambda: self.pc.paired))
        self.assertEqual(self.pc.paired[0]["token"], saved["token"])
        self.assertEqual((saved["host"], saved["port"], saved["id"]), ("127.0.0.1", 24820, pairing._b64(PC_ID)))
        self.assertEqual(self.pc.book.entries[0]["host"], "127.0.0.1")
        self.assertEqual(self.pc.service.outcome, "paired")

    def test_the_requester_replaces_the_1_4_entry_held_at_that_address(self):
        old = {"id": "", "name": "Old PC", "platform": "windows", "token": "typed-in-1.4", "host": "127.0.0.1",
               "port": 24820, "linked": False, "paired_at": 0, "from_1_4": True}
        self.mac.book.entries.append(old)
        code = self.pc.service.begin_pairing()
        saved = self.mac.service.pair_by_address("127.0.0.1", code, port=self.pc.tcp_port)
        self.assertEqual(self.mac.book.entries, [saved])
        self.assertEqual(self.mac.book.stored[-1][1]["from_1_4"], True)

    def test_a_qr_exchange_sends_qr_and_pairs(self):
        self.pc.service.begin_pairing()
        text = self.pc.service.qr_text("127.0.0.1")
        self.assertEqual(pairing.read_qr(text)["port"], self.pc.tcp_port)
        saved = self.mac.service.pair_by_qr(text)
        self.assertTrue(wait_until(lambda: self.pc.paired))
        self.assertEqual(self.pc.paired[0]["token"], saved["token"])

    def test_scanning_the_qr_of_a_machine_already_paired_does_not_pair(self):
        self.mac.book.entries.append(entry(PC_ID, name="TCP PC", paired_at=1_700_000_000))
        self.pc.service.begin_pairing()
        with self.assertRaises(AlreadyPaired) as caught:
            self.mac.service.pair_by_qr(self.pc.service.qr_text("127.0.0.1"))
        self.assertEqual(caught.exception.entry["id"], pairing._b64(PC_ID))
        # Nothing was sent: the host's code is still up and unspent.
        self.assertIsNotNone(self.pc.service.code)
        self.assertIsNone(self.pc.service.outcome)

    def test_rule_1_holds_across_the_two_transports(self):
        # UDP first: its pair_start binds the code, and the TCP one is refused.
        code = self.pc.service.begin_pairing()
        client = PairingClient(self.pc.service.host.pair_id, code, "Stranger", bytes([5]) * 16, version=3, platform="linux", port=24820, peers=list)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(("127.0.0.1", 0))
        udp.settimeout(3.0)
        self.addCleanup(udp.close)
        udp.sendto(pairing.encode(client.start()), ("127.0.0.1", self.pc.udp_port))
        self.assertTrue(pairing.decode(udp.recvfrom(2048)[0])["ok"])
        with self.assertRaises(PairingError) as caught:
            self.mac.service.pair_by_address("127.0.0.1", code, port=self.pc.tcp_port)
        self.assertEqual(str(caught.exception), pairing.ERROR_REFUSED)

        # TCP first: the UDP one is refused.
        code = self.pc.service.begin_pairing()
        sock = self.pc.connect()
        self.addCleanup(sock.close)
        beacon = read_frame(sock)
        client = PairingClient(beacon["pair"], code, "Stranger", bytes([5]) * 16, version=3, platform="linux", port=24820, peers=list)
        send_frame(sock, client.start())
        self.assertTrue(read_frame(sock)["ok"])
        other = PairingClient(beacon["pair"], code, "Other", bytes([6]) * 16, version=3, platform="linux", port=24820, peers=list)
        udp.sendto(pairing.encode(other.start()), ("127.0.0.1", self.pc.udp_port))
        self.assertEqual(pairing.decode(udp.recvfrom(2048)[0])["error"], pairing.ERROR_REFUSED)

    def test_the_host_s_beacon_comes_first_with_pair_and_id(self):
        self.pc.service.begin_pairing()
        sock = self.pc.connect()
        self.addCleanup(sock.close)
        beacon = read_frame(sock)
        self.assertEqual((beacon["type"], beacon["pairing"], beacon["pair"], beacon["id"], beacon["platform"]),
                         ("beacon", 3, self.pc.service.host.pair_id, pairing._b64(PC_ID), "windows"))

    def test_a_length_of_0_or_over_1024_closes_before_the_body(self):
        for length in (0, 1025):
            with self.subTest(length=length):
                self.pc.service.begin_pairing()
                sock = self.pc.connect()
                self.addCleanup(sock.close)
                read_frame(sock)
                sock.sendall(struct.pack(">I", length))
                self.assertTrue(closed_without_a_byte(sock))
                self.assertIsNotNone(self.pc.service.code, "a bad frame costs the connection, not the code")

    def test_out_of_order_messages_close_with_nothing_sent(self):
        code = self.pc.service.begin_pairing()
        sock = self.pc.connect()
        self.addCleanup(sock.close)
        beacon = read_frame(sock)
        send_frame(sock, {"type": "pair_confirm", "pair": beacon["pair"], "tag": ""})
        self.assertTrue(closed_without_a_byte(sock))

        sock = self.pc.connect()
        self.addCleanup(sock.close)
        read_frame(sock)
        client = PairingClient(beacon["pair"], code, "Stranger", bytes([5]) * 16, version=3, platform="linux", port=24820, peers=list)
        start = client.start()
        send_frame(sock, start)
        self.assertTrue(read_frame(sock)["ok"])
        send_frame(sock, start)
        self.assertTrue(closed_without_a_byte(sock))

    def test_a_refusal_is_sent_before_the_close(self):
        code = self.pc.service.begin_pairing()
        sock = self.pc.connect()
        self.addCleanup(sock.close)
        beacon = read_frame(sock)
        client = PairingClient(beacon["pair"], code, "Stranger", bytes([5]) * 16, version=3, platform="linux", port=24820, peers=list)
        send_frame(sock, dict(client.start(), v=2))
        self.assertEqual(read_frame(sock), {"beamy": 1, "type": "pair_answer", "pair": beacon["pair"], "ok": False, "error": "version"})
        self.assertIsNone(read_frame(sock))

    def test_a_tcp_beacon_of_pairing_2_stops_the_requester_before_it_sends(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        received = []

        def old_host():
            conn, _address = server.accept()
            with conn:
                send_frame(conn, {"type": "beacon", "name": "Old", "port": 24820, "pairing": 2, "pair": "ab" * 16})
                conn.settimeout(3.0)
                received.append(conn.recv(1))

        thread = threading.Thread(target=old_host)
        thread.start()
        with self.assertRaises(PairingError) as caught:
            self.mac.service.pair_by_address("127.0.0.1", "123456", port=server.getsockname()[1])
        thread.join(5)
        self.assertEqual(str(caught.exception), pairing.ERROR_VERSION)
        self.assertEqual(received, [b""])

    def test_the_listener_is_up_only_while_a_code_is(self):
        with self.assertRaises(OSError):
            self.pc.connect().close()
        self.pc.service.begin_pairing()
        self.pc.connect().close()
        self.pc.service.cancel_pairing()
        self.assertTrue(wait_until(lambda: not self._can_connect()))

    def test_the_listener_closes_when_the_code_expires(self):
        clock = [1000.0]
        self.pc.service.host.clock = lambda: clock[0]
        self.pc.service.begin_pairing()
        self.pc.connect().close()
        clock[0] += pairing.CODE_LIFETIME_SECONDS
        self.assertTrue(wait_until(lambda: not self._can_connect()))

    def _can_connect(self):
        try:
            self.pc.connect().close()
            return True
        except OSError:
            return False

    def test_a_connection_has_ten_seconds(self):
        with mock.patch.object(pairing, "TCP_EXCHANGE_SECONDS", 0.5):
            self.pc.service.begin_pairing()
            sock = self.pc.connect()
            self.addCleanup(sock.close)
            read_frame(sock)
            started = time.monotonic()
            self.assertTrue(closed_without_a_byte(sock, timeout=3.0))
            self.assertLess(time.monotonic() - started, 2.0)

    def test_a_second_connection_from_one_address_replaces_the_first(self):
        self.pc.service.begin_pairing()
        first = self.pc.connect()
        self.addCleanup(first.close)
        read_frame(first)
        second = self.pc.connect()
        self.addCleanup(second.close)
        self.assertEqual(read_frame(second)["type"], "beacon")
        self.assertTrue(closed_without_a_byte(first))

    def test_the_answer_leaves_a_fixed_time_after_its_start(self):
        code = self.pc.service.begin_pairing()
        sock = self.pc.connect()
        self.addCleanup(sock.close)
        beacon = read_frame(sock)
        client = PairingClient(beacon["pair"], code, "Timing", bytes([5]) * 16, version=3, platform="linux", port=24820, peers=list)
        sent = time.monotonic()
        send_frame(sock, client.start())
        self.assertTrue(read_frame(sock)["ok"])
        self.assertGreaterEqual(time.monotonic() - sent, pairing.PAIR_ANSWER_SECONDS - 0.005)


class SlotTests(unittest.TestCase):
    """The four handshake slots, held to the link's terms: one per address, the oldest gives way."""

    def test_one_per_address_and_the_oldest_gives_way(self):
        slots = pairing._Slots(4)
        closed = []
        keys = [slots.take(f"192.168.77.{n}", lambda n=n: closed.append(n)) for n in range(1, 5)]
        self.assertEqual(closed, [])
        slots.take("192.168.77.2", lambda: closed.append("2b"))
        self.assertEqual(closed, [2])
        slots.take("192.168.77.9", lambda: closed.append(9))
        self.assertEqual(closed, [2, 1])
        slots.release(keys[2])
        slots.take("192.168.77.10", lambda: closed.append(10))
        self.assertEqual(closed, [2, 1], "a freed slot is used before anyone is replaced")

    def test_the_connection_that_bound_the_code_is_never_replaced(self):
        slots = pairing._Slots(4)
        closed = []
        bound = slots.take("192.168.77.1", lambda: closed.append("bound"))
        slots.pin(bound)
        for n in range(2, 12):
            slots.take(f"192.168.77.{n}", lambda n=n: closed.append(n))
        slots.take("192.168.77.1", lambda: closed.append("again"))
        self.assertNotIn("bound", closed)
        slots.close_all()
        self.assertIn("bound", closed)

    def test_with_every_slot_bound_a_new_connection_is_turned_away(self):
        slots = pairing._Slots(1)
        closed = []
        slots.pin(slots.take("192.168.77.1", lambda: closed.append("bound")))
        self.assertIsNone(slots.take("192.168.77.2", lambda: closed.append("new")))
        self.assertEqual(closed, ["new"])
        self.assertEqual(len(slots._held), 1)


    def test_pinning_a_connection_already_replaced_says_so(self):
        slots = pairing._Slots(1)
        first = slots.take("10.9.9.1", lambda: None)
        second = slots.take("10.9.9.2", lambda: None)
        self.assertIs(slots.pin(first), False)
        self.assertIs(slots.pin(second), True)


class BoundToAGoneConnectionTests(unittest.TestCase):
    def test_a_code_bound_by_a_connection_already_replaced_ends_rather_than_waiting_on_it(self):
        # The review of the fixes: a newcomer could replace the connection in the instant between
        # its pair_start binding the code and its pin, leaving the code bound to nobody.
        pc = Machine(self, "Gone PC", PC_ID, "windows")
        code = pc.service.begin_pairing()
        start = PairingClient(pc.service.host.pair_id, code, "Gone", bytes([7]) * 16, version=3, platform="linux", port=24820, peers=list).start()
        reply, paired, fresh = pc.service._host_handle(start, "10.9.9.1", ("tcp", 99), lambda: False)
        self.assertIsNone(reply)
        self.assertFalse(pc.service.host.active)
        self.assertEqual(pc.service.host.outcome, "refused")


class ReplayedStartTests(unittest.TestCase):
    """The review's probe: one pair_start replayed on new connections drew the cached answer and
    was pinned each time, so the four slots grew without limit."""

    def test_replays_of_the_bound_start_keep_to_the_four_slots(self):
        pc = Machine(self, "Replay PC", PC_ID, "windows")
        code = pc.service.begin_pairing()
        held = []

        def start(frame=None):
            sock = pc.connect()
            held.append(sock)
            beacon = read_frame(sock)
            if frame is None:
                frame = PairingClient(beacon["pair"], code, "Replayer", bytes([9]) * 16, version=3, platform="linux", port=24820, peers=list).start()
            send_frame(sock, frame)
            self.assertTrue(read_frame(sock)["ok"])
            return frame

        frame = start()
        for _ in range(12):
            start(frame)
        self.addCleanup(lambda: [sock.close() for sock in held])
        self.assertTrue(wait_until(lambda: len(pc.service._slots._held) <= pairing.TCP_MAX_CONNECTIONS))
        self.assertLessEqual(len(pc.service._slots._pinned), 1)


class RequesterLimitTests(unittest.TestCase):
    def test_a_requester_with_32_peers_does_not_start(self):
        mac = Machine(self, "Full Mac", MAC_ID, "macos", entries=[entry(bytes([n]) * 16) for n in range(1, 33)])
        with self.assertRaises(PairingError) as caught:
            mac.service.pair_by_address("127.0.0.1", "123456", port=free_port(socket.SOCK_STREAM))
        self.assertEqual(str(caught.exception), pairing.ERROR_FULL)

    def test_a_host_with_32_peers_shows_no_code(self):
        pc = Machine(self, "Full PC", PC_ID, "windows", entries=[entry(bytes([n]) * 16) for n in range(1, 33)])
        with self.assertRaises(PairingError):
            pc.service.begin_pairing()
        self.assertIsNone(pc.service.code)

    def test_the_failed_code_memory_covers_both_transports(self):
        mac_udp, pc_udp = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
        pc = Machine(self, "PC", PC_ID, "windows", peer_udp=mac_udp, udp_port=pc_udp)
        mac = Machine(self, "Mac", MAC_ID, "macos", peer_udp=pc_udp, udp_port=mac_udp)

        def over_udp(code):
            self.assertTrue(wait_until(lambda: any(m["pair_id"] for m in mac.service.machines())))
            return mac.service.pair([m for m in mac.service.machines() if m["pair_id"]][0], code)

        def over_tcp(code):
            return mac.service.pair_by_address("127.0.0.1", code, port=pc.tcp_port)

        for first, second in ((over_udp, over_tcp), (over_tcp, over_udp)):
            with self.subTest(first=first.__name__):
                code = pc.service.begin_pairing()
                wrong = str((int(code) + 1) % 10**6).zfill(6)
                with self.assertRaises(PairingError):
                    first(wrong)
                self.assertTrue(wait_until(lambda: pc.service.outcome == "refused"))
                pc.service.begin_pairing()
                pc.service.host.code = wrong
                with self.assertRaises(PairingError) as caught:
                    second(wrong)
                self.assertEqual(str(caught.exception), pairing.ERROR_REFUSED)
                self.assertIsNone(pc.service.outcome, "the second try sent nothing")
                self.assertEqual(pc.book.entries, [])
                mac.service._failed.clear()

    def test_the_requester_checks_again_just_before_it_stores(self):
        pc = Machine(self, "PC", PC_ID, "windows")
        mac = Machine(self, "Mac", MAC_ID, "macos")
        code = pc.service.begin_pairing()
        real = PairingClient.finish

        def finish(client, done):
            token = real(client, done)
            # Another pairing with the same machine landed while this one was finishing.
            mac.book.entries.append(entry(PC_ID, name="PC"))
            return token

        with mock.patch.object(PairingClient, "finish", finish):
            with self.assertRaises(AlreadyPaired):
                mac.service.pair_by_address("127.0.0.1", code, port=pc.tcp_port)
        self.assertEqual(mac.book.stored, [])

    def test_stop_ends_the_code_and_its_listener(self):
        pc = Machine(self, "PC", PC_ID, "windows")
        pc.service.begin_pairing()
        held = pc.connect()
        self.addCleanup(held.close)
        read_frame(held)
        pc.service.stop()
        self.assertIsNone(pc.service.code)
        self.assertTrue(closed_without_a_byte(held))
        with self.assertRaises(OSError):
            pc.connect().close()

    def test_a_listener_that_cannot_bind_gives_no_qr(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("", 0))
        blocker.listen(1)
        self.addCleanup(blocker.close)
        pc = Machine(self, "PC", PC_ID, "windows")
        pc.service.tcp_port = blocker.getsockname()[1]
        self.assertIsNotNone(pc.service.begin_pairing())
        self.assertIsNotNone(pc.service.tcp_error)
        self.assertIsNone(pc.service.qr_text("127.0.0.1"))


if __name__ == "__main__":
    unittest.main()
