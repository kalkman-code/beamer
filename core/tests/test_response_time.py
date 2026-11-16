import unittest
from types import SimpleNamespace

from core import protocol, receiver
from core.response_time import ResponseTime


class ResponseTimeTests(unittest.TestCase):
    def test_responder_probe_is_not_starved_by_queued_traffic(self):
        sent = []
        sock = SimpleNamespace(send=lambda frame: len(frame))
        def seal(message):
            sent.append(message)
            link.closed.set()
            return b"frame"
        link = receiver._Link(sock, SimpleNamespace(seal=seal), b"p" * 16, "key", "localhost",
                              "Peer", ["ping_rtt"], b"o" * 16, ["ping_rtt"], True)
        link.next_response_probe = 0.0
        queued = protocol.clipboard_msg("queued traffic")
        link.outbox.append(queued)
        receiver.LinkResponder._writer(SimpleNamespace(_check_available=lambda: True), link)
        self.assertEqual(sent[0]["type"], protocol.MSG_PING)
        self.assertIn("probe", sent[0]["data"])
        self.assertEqual(list(link.outbox), [queued])

    def test_only_a_live_matching_echo_produces_a_fresh_sample(self):
        now = [10.0]
        samples = ResponseTime(clock=lambda: now[0])

        probe = samples.new_probe()
        now[0] = 10.010
        self.assertFalse(samples.echoed(probe))
        samples.sent(probe)
        now[0] = 11.210
        self.assertIsNone(samples.new_probe())
        self.assertFalse(samples.echoed(2))
        self.assertTrue(samples.echoed(probe))
        self.assertFalse(samples.echoed(probe))
        self.assertEqual(samples.response_time_ms, 1200)

        now[0] = 14.211
        self.assertIsNone(samples.response_time_ms)

    def test_late_echo_and_reset_do_not_restore_a_sample(self):
        now = [1.0]
        samples = ResponseTime(clock=lambda: now[0])
        probe = samples.new_probe()
        samples.sent(probe)
        now[0] = 5.0
        self.assertFalse(samples.echoed(probe))
        self.assertIsNone(samples.response_time_ms)

        next_probe = samples.new_probe()
        samples.sent(next_probe)
        samples.reset()
        now[0] = 5.01
        self.assertFalse(samples.echoed(next_probe))
        self.assertIsNone(samples.response_time_ms)


if __name__ == "__main__":
    unittest.main()
