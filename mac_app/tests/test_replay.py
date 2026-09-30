"""Whole-connection replay, over both links with the real link code at each end.

An attacker on the LAN who recorded one connection opens a new one and sends the recorded
initiator bytes in order: the preamble, the hello and every input frame after it. Per-frame
replay inside a connection was always refused; this is the whole stream again, into a fresh
responder, and it must fail the tag rather than inject what was recorded."""

import socket
import time
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import protocol
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
        duo = Duo(crossing={"resistance_px": 40}).listen()
        self.addCleanup(duo.close)
        recordings = []
        duo.mac.socket_factory = recording_factory(recordings)
        duo.link_mac_to_pc()
        self.assertTrue(duo.mac_push(1727, 500, 30), "the push never crossed")
        self.assertTrue(wait_for(lambda: duo.pc_desktop.cursor[0] == 0), "the switch never placed the PC's pointer")
        self.assertTrue(duo.mac.send_arrangement("left", 500))
        self.assertTrue(wait_for(lambda: duo.pc_arrangements), "the arrangement never arrived")
        duo.mac.stop()
        duo.pc_desktop.cursor = (900, 500)
        arrangements = len(duo.pc_arrangements)
        self.assertEqual(len(recordings), 1)

        reply = replay(duo.pc_port, recordings[0].sent)
        settle()

        self.assertEqual(duo.pc_desktop.cursor, (900, 500), "a replayed switch moved the PC's pointer")
        self.assertEqual(len(duo.pc_arrangements), arrangements, "a replayed arrangement was applied")
        self.assertEqual(len(reply), protocol.PREAMBLE_SIZE, "the PC answered past its preamble")

    def test_the_pcs_recorded_link_replayed_into_the_mac_is_refused(self):
        duo = Duo().listen()
        self.addCleanup(duo.close)
        recordings = []
        duo.pc._socket_factory = recording_factory(recordings)
        duo.link_pc_to_mac()
        self.assertTrue(duo.pc.send_arrangement("top", 600))
        self.assertTrue(wait_for(lambda: duo.mac_arrangements), "the arrangement never arrived")
        duo.pc.stop()
        arrangements = len(duo.mac_arrangements)
        self.assertEqual(len(recordings), 1)

        reply = replay(duo.mac_port, recordings[0].sent)
        settle()

        self.assertEqual(len(duo.mac_arrangements), arrangements, "a replayed arrangement was applied")
        self.assertEqual(len(reply), protocol.PREAMBLE_SIZE, "the Mac answered past its preamble")


class SessionKeyTests(unittest.TestCase):
    def pair(self):
        a, b = protocol.SecureSession("t"), protocol.SecureSession("t")
        a.accept_preamble(b.preamble())
        b.accept_preamble(a.preamble())
        return a, b

    def test_nothing_is_sealed_before_the_peers_preamble(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.SecureSession("t").seal(protocol.ping_msg())

    def test_a_sides_own_frames_reflected_back_fail_the_tag(self):
        a, b = self.pair()
        with self.assertRaises(protocol.AuthenticationError):
            a.open(a.seal(protocol.ping_msg())[protocol.HEADER_SIZE:])

    def test_a_frame_from_another_connection_fails_the_tag(self):
        a, b = self.pair()
        frame = a.seal(protocol.ping_msg())
        fresh = protocol.SecureSession("t")
        fresh.accept_preamble(a.preamble())
        with self.assertRaises(protocol.AuthenticationError):
            fresh.open(frame[protocol.HEADER_SIZE:])
        self.assertEqual(b.open(frame[protocol.HEADER_SIZE:]), protocol.ping_msg())

    def test_a_second_preamble_is_refused(self):
        a, b = self.pair()
        with self.assertRaises(protocol.ProtocolError):
            a.accept_preamble(protocol.SecureSession("t").preamble())


if __name__ == "__main__":
    unittest.main()
