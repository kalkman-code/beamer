import socket
import struct
import threading
import time
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import protocol
from core import receiver
from core.receiver import ReceiverServer, ServerState


class HandshakeDeadlineTests(unittest.TestCase):
    """The handshake is bounded as a whole, and only handshakes count against
    the pending cap, so a stranger trickling bytes cannot hold a slot for
    long and the real peer never finds every slot taken by strangers."""

    def test_a_trickled_read_ends_at_the_deadline_not_per_byte(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b.settimeout(1.0)
        deadline = time.monotonic() + 0.3

        def trickle():
            for _ in range(6):
                time.sleep(0.1)
                try:
                    a.send(b"\0")
                except OSError:
                    return

        threading.Thread(target=trickle, daemon=True).start()
        started = time.monotonic()
        with self.assertRaises(socket.timeout):
            protocol._recv_exact(b, 64, deadline)
        self.assertLess(time.monotonic() - started, 0.9)

    def test_strangers_free_their_slots_and_the_peer_gets_in(self):
        statuses = []
        server = ReceiverServer(lambda state, detail: statuses.append((state, detail)))
        self.addCleanup(server.stop)
        with unittest_patch(receiver, "HELLO_TIMEOUT_SECONDS", 0.3):
            server.start(_config(port=0))
            port = _wait_for_port(server)
            strangers = []
            for _ in range(receiver.MAX_PENDING_CONNECTIONS):
                s = socket.create_connection(("127.0.0.1", port), timeout=2.0)
                s.sendall(b"\0")
                strangers.append(s)
            self.addCleanup(lambda: [s.close() for s in strangers])
            # Every slot is a stranger's, so a fifth connection is refused at once.
            refused = socket.create_connection(("127.0.0.1", port), timeout=2.0)
            self.addCleanup(refused.close)
            refused.settimeout(2.0)
            self.assertEqual(refused.recv(1), b"")
            # Their handshakes time out, and the next connection is served.
            time.sleep(0.6)
            served = socket.create_connection(("127.0.0.1", port), timeout=2.0)
            self.addCleanup(served.close)
            session = protocol.SecureSession("shared-token")
            served.sendall(session.preamble())
            served.settimeout(2.0)
            protocol.recv_preamble(served, session)


class unittest_patch:
    def __init__(self, module, name, value):
        self.module, self.name, self.value = module, name, value

    def __enter__(self):
        self.old = getattr(self.module, self.name)
        setattr(self.module, self.name, self.value)

    def __exit__(self, *exc):
        setattr(self.module, self.name, self.old)


def _config(port):
    class Config:
        auth_token = "shared-token"

    Config.port = port
    return Config()


def _wait_for_port(server, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        listener = server._listener
        if listener is not None:
            return listener.getsockname()[1]
        time.sleep(0.01)
    raise AssertionError("the listener did not come up")


if __name__ == "__main__":
    unittest.main()
