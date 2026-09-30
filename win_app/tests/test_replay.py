"""Whole-connection replay: the PC's real link recorded, then its bytes sent again as a new
connection to the same receiver.

An attacker on the LAN who recorded one connection opens a new one and sends the recorded
initiator bytes in order: the preamble, the hello and every input frame after it. Per-frame
replay inside a connection was always refused; this is the whole stream again, into a fresh
responder, and it must fail the tag rather than type what was recorded."""

import socket
import time
import unittest
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import protocol
from core import receiver
import sender
from app_config import Config
from fakes import FakeClipboard, FakeDesktop, FakeInjector, wait_for_calls
from core.return_edge import Rect
from test_sender import NoUnlock, free_port, make_config, wait_for

MONITORS = [Rect(0, 0, 1920, 1080)]


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
        attacker.sendall(data[:protocol.PREAMBLE_SIZE])
        while len(reply) < protocol.PREAMBLE_SIZE:
            chunk = attacker.recv(protocol.PREAMBLE_SIZE - len(reply))
            if not chunk:
                return bytes(reply)
            reply += chunk
        try:
            attacker.sendall(data[protocol.PREAMBLE_SIZE:])
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
    def test_the_pcs_recorded_keystrokes_replayed_into_the_mac_are_refused(self):
        injector = FakeInjector()
        server = receiver.ReceiverServer(
            status_callback=lambda state, detail: None,
            clipboard=FakeClipboard(),
            unlock=NoUnlock(),
            desktop=FakeDesktop(MONITORS, cursor=(900, 500)),
            injector=injector,
            self_name="Mac",
            peer_name="PC",
            self_target="mac",
            peer_target="windows",
        )
        port = free_port()
        server.start(Config(host="127.0.0.1", port=port, auth_token="shared-token"))
        self.addCleanup(server.stop)
        recordings = []

        def connect(address, timeout):
            recordings.append(Recording(socket.create_connection(address, timeout)))
            return recordings[-1]

        link = sender.MacSender(
            desktop=FakeDesktop(MONITORS, cursor=(0, 500)),
            clipboard=FakeClipboard(),
            is_local=lambda host: False,
            socket_factory=connect,
        )
        link.start(make_config(port=port))
        self.assertTrue(wait_for(lambda: link.connected), f"never connected: {link.status}")
        link.set_redirecting(True, arrival_edge="right", offset=0.5)
        link.on_key("a", True)
        link.on_key("a", False)
        wait_for_calls(injector.calls, 2)
        link.stop()
        typed = [call for call in injector.calls if call[0] == "key"]
        self.assertEqual(typed, [("key", ("a", True)), ("key", ("a", False))])
        self.assertEqual(len(recordings), 1)

        reply = replay(port, recordings[0].sent)
        time.sleep(0.2)

        self.assertEqual([call for call in injector.calls if call[0] == "key"], typed, "replayed keys reached the Mac")
        self.assertEqual(len(reply), protocol.PREAMBLE_SIZE, "the Mac answered past its preamble")


if __name__ == "__main__":
    unittest.main()
