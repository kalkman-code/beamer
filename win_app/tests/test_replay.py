"""Whole-connection replay: the PC's real link recorded, then its bytes sent again as a new
connection to the same receiver.

An attacker on the LAN who recorded one connection opens a new one and sends the recorded
initiator bytes in order: the preamble, the hello and every input frame after it. Per-frame
replay inside a connection was always refused; this is the whole stream again, into a fresh
responder, and it must fail the tag rather than type what was recorded."""

import functools
import socket
import time
import unittest

from links_rig import make_config

import sender
from core import protocol
from core import receiver
from core.link import OutboundLink
from core.return_edge import Rect
from core.tests import responder_harness as harness
from core.tests.responder_harness import B, HERE, TOKENS, Machine, entry, wait_for

MONITORS = [Rect(0, 0, 1920, 1080)]
PREAMBLE = protocol.LINK_PREAMBLE_SIZE


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


def replay(port, data):
    """Send `data` as a fresh connection and return every byte the responder sent back before it
    closed."""
    data = bytes(data)
    with socket.create_connection(("127.0.0.1", port), timeout=3.0) as attacker:
        reply = bytearray()
        # The responder's preamble is read before the frames go: on Windows a socket closed with
        # bytes still unread resets the connection, and the reset throws away whatever this end
        # had not read yet, so sending everything at once lost the preamble about a run in five.
        attacker.sendall(data[:PREAMBLE])
        while len(reply) < PREAMBLE:
            chunk = attacker.recv(PREAMBLE - len(reply))
            if not chunk:
                return bytes(reply)
            reply += chunk
        try:
            attacker.sendall(data[PREAMBLE:])
            attacker.shutdown(socket.SHUT_WR)
        except (ConnectionResetError, ConnectionAbortedError):
            return bytes(reply)
        while True:
            try:
                chunk = attacker.recv(65536)
            except (ConnectionResetError, ConnectionAbortedError, socket.timeout):
                break
            if not chunk:
                break
            reply += chunk
    return bytes(reply)


class WholeConnectionReplayTests(unittest.TestCase):
    def test_the_pcs_recorded_keystrokes_replayed_into_the_peer_are_refused(self):
        far = Machine([entry(B, "Near", platform="windows")]).start()
        self.addCleanup(far.stop)
        recordings = []

        def connect(address, timeout=None):
            recordings.append(Recording(socket.create_connection(address, timeout)))
            return recordings[-1]

        peer = entry(HERE, "Far", token=TOKENS[B], host="127.0.0.1", port=far.port, side="left")
        settings = harness.Settings([peer], [{"peer": protocol.id_text(HERE), "kind": "edge"}])
        link = sender.LinkSender(
            receiver.PeerBook(settings.load, settings.save),
            lambda: {"id": B, "name": "Near", "platform": "windows", "app": "1.5.0", "caps": list(harness.CAPS), "port": 24820},
            zones=lambda: settings.data["zones"],
            desktop=harness.FakeDesktop(MONITORS, (0, 500)),
            clipboard=harness.FakeClipboard(),
            is_local=lambda host: False,
            links=lambda owner: sender.LinkSet(
                owner, lambda host: False, functools.partial(OutboundLink, socket_factory=connect)
            ),
        )
        link.start(make_config(machine_id=protocol.id_text(B)))
        self.addCleanup(link.stop)
        self.assertTrue(wait_for(lambda: link.connected, 5), f"never connected: {link.status}")
        link.set_redirecting(True)
        self.assertTrue(wait_for(lambda: far.owners and far.owners[-1] == B))
        link.on_key("a", True, 0x41, "a")
        link.on_key("a", False, 0x41, "a")
        self.assertTrue(wait_for(lambda: len([call for call in far.injected() if call[0] == "key"]) == 2))
        typed = [call for call in far.injected() if call[0] == "key"]
        link.stop()
        self.assertEqual(typed, [("key", "a", True), ("key", "a", False)])
        self.assertEqual(len(recordings), 1)

        reply = replay(far.port, recordings[0].sent)
        time.sleep(0.2)

        self.assertEqual([call for call in far.injected() if call[0] == "key"], typed, "replayed keys reached the peer")
        self.assertEqual(len(reply), PREAMBLE, "the peer answered past its preamble")


if __name__ == "__main__":
    unittest.main()
