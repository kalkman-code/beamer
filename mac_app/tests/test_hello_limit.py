import socket
import struct
import threading
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import protocol


class HelloFrameLimitTests(unittest.TestCase):
    """Before the first frame's tag is checked the peer has proved nothing, so
    the receiver reads that frame under HELLO_MAX_BYTES rather than the
    clipboard-sized MAX_FRAME_BYTES a stranger could otherwise make it buffer
    per connection."""

    def _pair(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        return a, b

    def test_an_oversized_first_frame_is_refused_before_it_is_read(self):
        sender, receiver = self._pair()
        session = protocol.SecureSession("t")
        sender.sendall(struct.pack(">I", protocol.HELLO_MAX_BYTES + 1))
        with self.assertRaises(protocol.ProtocolError):
            protocol.recv_msg(receiver, session, limit=protocol.HELLO_MAX_BYTES)

    def test_a_real_hello_fits_under_the_limit(self):
        sender, receiver = self._pair()
        theirs = protocol.SecureSession("t")
        mine = protocol.SecureSession("t")
        mine.accept_preamble(theirs.preamble())
        theirs.accept_preamble(mine.preamble())
        frame = theirs.seal(protocol.hello_msg(return_edge="left", resistance_px=120))
        self.assertLess(len(frame) - protocol.HEADER_SIZE, protocol.HELLO_MAX_BYTES)
        sender.sendall(frame)
        self.assertEqual(protocol.recv_msg(receiver, mine, limit=protocol.HELLO_MAX_BYTES)["type"], protocol.MSG_HELLO)

    def test_the_default_limit_is_still_the_frame_cap(self):
        sender, receiver = self._pair()
        session = protocol.SecureSession("t")
        sender.sendall(struct.pack(">I", protocol.MAX_FRAME_BYTES + 1))
        with self.assertRaises(protocol.ProtocolError):
            protocol.recv_msg(receiver, session)


if __name__ == "__main__":
    unittest.main()
