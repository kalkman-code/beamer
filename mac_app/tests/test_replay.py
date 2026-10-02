"""Whole-connection replay, with the Mac's real link code at one end and the version 6 responder at
the other.

An attacker on the LAN who recorded one connection opens a new one and sends the recorded
initiator bytes in order: the preamble, the hello and every input frame after it. Per-frame
replay inside a connection was always refused; this is the whole stream again, into a fresh
responder, and it must fail the first tag rather than inject what was recorded."""

import copy
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from bridge_fakes import PAIRED_TOKEN
from core import protocol
from core.tests.responder_harness import HERE, Machine
from two_machines import Duo, wait_for


class Recording:
    """A connected socket that keeps a copy of every byte each way."""

    def __init__(self, sock):
        self._sock = sock
        self.sent = bytearray()
        self.received = bytearray()

    def sendall(self, data, *args):
        self.sent += data
        return self._sock.sendall(data, *args)

    def send(self, data, *args):
        count = self._sock.send(data, *args)
        self.sent += data[:count]
        return count

    def recv(self, size, *args):
        data = self._sock.recv(size, *args)
        self.received += data
        return data

    def __getattr__(self, name):
        return getattr(self._sock, name)


def recording_factory(recordings):
    def connect(address, timeout=None, *args, **kwargs):
        recording = Recording(socket.create_connection(address, timeout))
        recordings.append(recording)
        return recording

    return connect


def recorder(recordings):
    def wrap(sock):
        recording = Recording(sock)
        recordings.append(recording)
        return recording

    return wrap


def replay(port, data):
    """Send `data` as a fresh connection and return every byte the responder sent back before it
    closed."""
    with socket.create_connection(("127.0.0.1", port), timeout=3.0) as attacker:
        # A responder that refuses early closes before the whole recording is sent; what it
        # said before closing is still in the buffer to read.
        try:
            attacker.sendall(bytes(data))
            attacker.shutdown(socket.SHUT_WR)
        except (BrokenPipeError, ConnectionResetError):
            pass
        reply = bytearray()
        while True:
            try:
                chunk = attacker.recv(65536)
            except (ConnectionResetError, socket.timeout):
                break
            if not chunk:
                break
            reply += chunk
    return bytes(reply)


def settle():
    # Anything a replayed frame would inject lands on the receiver's session thread; the reply
    # above has already been read to the close, so this only covers the last callback.
    time.sleep(0.2)


class WholeConnectionReplayTests(unittest.TestCase):
    def test_the_macs_recorded_link_replayed_into_the_pc_is_refused(self):
        recordings = []
        duo = Duo(crossing={"resistance_px": 40}, socket_factory=recording_factory(recordings)).listen()
        self.addCleanup(duo.close)
        pc = duo.pc_responder
        duo.link_mac_to_pc()
        self.assertTrue(duo.mac_push(1727, 500, 30), "the push never crossed")
        self.assertTrue(wait_for(lambda: pc.arrivals), "the take never placed the PC's pointer")
        duo.mac_move(1727, 500, 7)
        self.assertTrue(wait_for(lambda: ("move", 7, 0) in pc.injected()), "the Mac's input never arrived")
        self.assertTrue(duo.mac.send_arrangement(protocol.id_text(HERE), "left", 500))
        self.assertTrue(wait_for(lambda: any(read["set_at"] == 500 for _, read in pc.arrangements)), "the arrangement never arrived")
        duo.mac.stop()
        self.assertTrue(wait_for(lambda: pc.responder.owner is None and not pc.responder.links()))
        self.assertEqual(len(recordings), 1)
        self.assertGreater(len(recordings[0].sent), protocol.LINK_PREAMBLE_SIZE + 6 * protocol.MIN_FRAME_BYTES)

        pc.desktop.cursor = (900, 500)
        before = (len(pc.desktop.placed), len(pc.injected()), len(pc.arrangements), list(pc.owners), len(pc.arrivals))
        fresh = Machine(copy.deepcopy(pc.settings.data["peers"]), pc.settings.data["zones"]).start()
        self.addCleanup(fresh.stop)

        for name, target in (("the PC it was taken from", pc), ("a fresh responder", fresh)):
            with self.subTest(into=name):
                reply = replay(target.port, recordings[0].sent)
                settle()
                self.assertEqual(len(reply), protocol.LINK_PREAMBLE_SIZE, "the responder answered past its preamble")
                self.assertEqual(target.responder.links(), set())
                self.assertIsNone(target.responder.owner)

        self.assertEqual(pc.desktop.cursor, (900, 500), "a replayed take moved the PC's pointer")
        self.assertEqual((len(pc.desktop.placed), len(pc.injected()), len(pc.arrangements), list(pc.owners), len(pc.arrivals)), before)
        self.assertEqual((fresh.injected(), fresh.arrangements, fresh.owners, fresh.arrivals, fresh.desktop.placed), ([], [], [], [], []))

    def test_the_pcs_recorded_link_replayed_into_the_mac_is_refused(self):
        recordings = []
        duo = Duo().listen()
        self.addCleanup(duo.close)
        duo.link_pc_to_mac(wrap=recorder(recordings))
        duo.pc_drives("left", 0.5)
        self.assertTrue(wait_for(lambda: duo.mac_arrivals), "the take never placed the Mac's pointer")
        duo.pc.acked(duo.pc.move(5, 0))
        duo.pc.acked(duo.pc.key(protocol.MSG_KEYDOWN, "a"))
        duo.pc.send(protocol.arrangement_v6("top", 600, HERE))
        self.assertTrue(wait_for(lambda: any(item[1] == 600 for item in duo.mac_arrangements)), "the arrangement never arrived")
        duo.pc.let_go()
        self.assertTrue(wait_for(lambda: not duo.mac.receiving))
        duo.pc.sock.shutdown(socket.SHUT_RDWR)
        duo.pc.close()
        self.assertTrue(wait_for(lambda: not duo.mac_responder.links()))
        self.assertEqual(len(recordings), 1)
        self.assertGreater(len(recordings[0].sent), protocol.LINK_PREAMBLE_SIZE + 6 * protocol.MIN_FRAME_BYTES)
        self.assertTrue(any(call[0] == "move" for call in duo.mac_injector.seen()), "the recorded input was never injected")

        duo.mac_desktop.cursor = (900, 500)
        before = (len(duo.mac_desktop.placed), len(duo.mac_injector.seen()), len(duo.mac_arrangements), len(duo.mac_arrivals))

        reply = replay(duo.mac_port, recordings[0].sent)
        settle()

        self.assertEqual(duo.mac_desktop.cursor, (900, 500), "a replayed take moved the Mac's pointer")
        self.assertEqual((len(duo.mac_desktop.placed), len(duo.mac_injector.seen()), len(duo.mac_arrangements), len(duo.mac_arrivals)), before)
        self.assertFalse(duo.mac.receiving, "a replayed take made the Mac driven")
        self.assertEqual(duo.mac_responder.links(), set())
        self.assertEqual(len(reply), protocol.LINK_PREAMBLE_SIZE, "the Mac answered past its preamble")


class SessionKeyTests(unittest.TestCase):
    def pair(self):
        a = protocol.LinkSession(PAIRED_TOKEN, protocol.ROLE_INITIATOR)
        b = protocol.LinkSession(PAIRED_TOKEN, protocol.ROLE_RESPONDER)
        b.accept_preamble(a.preamble())
        a.accept_preamble(b.preamble())
        return a, b

    def test_nothing_is_sealed_before_the_peers_preamble(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.LinkSession(PAIRED_TOKEN, protocol.ROLE_INITIATOR).seal(protocol.ping_msg())

    def test_a_sides_own_frames_reflected_back_fail_the_tag(self):
        a, b = self.pair()
        with self.assertRaises(protocol.AuthenticationError):
            a.open(a.seal(protocol.ping_msg())[protocol.HEADER_SIZE:])

    def test_a_frame_from_another_connection_fails_the_tag(self):
        a, b = self.pair()
        frame = a.seal(protocol.ping_msg())
        fresh = protocol.LinkSession(PAIRED_TOKEN, protocol.ROLE_RESPONDER)
        fresh.accept_preamble(a.preamble())
        with self.assertRaises(protocol.AuthenticationError):
            fresh.open(frame[protocol.HEADER_SIZE:])
        self.assertEqual(b.open(frame[protocol.HEADER_SIZE:]), protocol.ping_msg())

    def test_a_second_preamble_is_refused(self):
        a, b = self.pair()
        other_initiator = protocol.LinkSession(PAIRED_TOKEN, protocol.ROLE_INITIATOR)
        other = protocol.LinkSession(PAIRED_TOKEN, protocol.ROLE_RESPONDER)
        other.accept_preamble(other_initiator.preamble())
        with self.assertRaises(protocol.ProtocolError):
            a.accept_preamble(other.preamble())


if __name__ == "__main__":
    unittest.main()
