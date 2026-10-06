import logging
import socket
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.pairing import Announcer, Discovery


def quiet_logger():
    logger = logging.getLogger("test-announcer-error")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


class _ScriptedSocket:
    """Stands in for the bound UDP socket: `recvfrom` raises each entry of
    `script` in turn, then the announcer is asked to stop."""

    def __init__(self, script, stop):
        self.script = list(script)
        self.stop = stop
        self.calls = 0
        self.sent = 0
        self.closed = False

    def sendto(self, data, addr):
        self.sent += 1
        return len(data)

    def getsockname(self):
        return "0.0.0.0", 24821

    def recvfrom(self, bufsize):
        self.calls += 1
        if self.script:
            raise self.script.pop(0)
        self.stop.set()
        raise socket.timeout()

    def close(self):
        self.closed = True


def run_announcer(script):
    announcer = Announcer(
        lambda: 24820,
        lambda token, name, address: None,
        logger=quiet_logger(),
        bind_port=0,
        announce_to=("127.0.0.1", 1),
        name="TEST-PC",
    )
    sock = _ScriptedSocket(script, announcer._stop)
    announcer._run.__globals__["_udp_socket"] = lambda bind_port: sock
    try:
        announcer._run()
    finally:
        del announcer._run.__globals__["_udp_socket"]
    return announcer, sock


class AnnouncerSocketErrorTests(unittest.TestCase):
    def test_a_datagram_reset_is_survived(self):
        # Windows reports an ICMP port-unreachable for a datagram this socket
        # sent as ConnectionResetError on the next receive. The socket is fine.
        announcer, sock = run_announcer(
            [socket.timeout(), ConnectionResetError(10054, "An existing connection was forcibly closed")]
        )
        self.assertIsNone(announcer.error)
        self.assertGreaterEqual(sock.calls, 3, "the loop stopped at the reset instead of carrying on")
        self.assertTrue(sock.closed)

    def test_any_other_socket_error_is_reported_not_swallowed(self):
        announcer, sock = run_announcer([socket.timeout(), OSError(10022, "An invalid argument was supplied")])
        self.assertFalse(announcer._stop.is_set())
        self.assertIsNotNone(announcer.error, "a dead announcer must not look like an idle one")
        self.assertIn("invalid argument", announcer.error)
        self.assertTrue(sock.closed)


class DiscoverySocketErrorTests(unittest.TestCase):
    def _run(self, script):
        discovery = Discovery(logger=quiet_logger(), bind_port=0)
        sock = _ScriptedSocket(script, discovery._stop)
        discovery._run.__globals__["_udp_socket"] = lambda bind_port: sock
        try:
            discovery._run()
        finally:
            del discovery._run.__globals__["_udp_socket"]
        return discovery, sock

    def test_a_datagram_reset_is_survived(self):
        discovery, sock = self._run([ConnectionResetError(54, "Connection reset by peer")])
        self.assertIsNone(discovery.error)
        self.assertGreaterEqual(sock.calls, 2)

    def test_any_other_socket_error_is_reported(self):
        discovery, sock = self._run([OSError(9, "Bad file descriptor")])
        self.assertIsNotNone(discovery.error)
        self.assertIn("Bad file descriptor", discovery.error)


if __name__ == "__main__":
    unittest.main()
