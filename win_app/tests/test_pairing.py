import json
import unittest
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import pairing
from core.pairing import PairingHost, beacon_msg, decode, encode


class FakeClock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class BeaconTests(unittest.TestCase):
    def test_beacon_carries_only_name_port_version_and_pairing_id(self):
        host = PairingHost(FakeClock())
        code = host.begin()
        raw = encode(beacon_msg("TEST-PC", 51820, host.pair_id))
        message = json.loads(raw)
        self.assertEqual(set(message), {"beamy", "type", "name", "port", "pairing", "pair"})
        self.assertEqual(message["pair"], host.pair_id)
        self.assertNotIn(code.encode(), raw)
        self.assertEqual(decode(raw), message)

    def test_beacon_without_a_code_has_no_pairing_id(self):
        self.assertNotIn("pair", beacon_msg("TEST-PC", 51820, None))

    def test_decode_ignores_foreign_datagrams(self):
        self.assertIsNone(decode(b"\xff\x00not json"))
        self.assertIsNone(decode(b'{"beamy": 99, "type": "beacon"}'))
        self.assertIsNone(decode(b"[]"))
        self.assertIsNone(decode(b"x" * (pairing.MAX_DATAGRAM_BYTES + 1)))


class CodeLifetimeTests(unittest.TestCase):
    """The exchange itself is covered by test_pairing_cpace.py, which both apps share."""

    def setUp(self):
        self.clock = FakeClock()
        self.host = PairingHost(self.clock)

    def test_each_code_gets_its_own_pairing_id(self):
        self.host.begin()
        first = self.host.pair_id
        self.host.begin()
        self.assertNotEqual(first, self.host.pair_id)
        self.assertEqual(len(bytes.fromhex(first)), 16)

    def test_seconds_left_counts_down_to_zero(self):
        self.host.begin()
        self.assertEqual(self.host.seconds_left, int(pairing.CODE_LIFETIME_SECONDS))
        self.clock.advance(10)
        self.assertEqual(self.host.seconds_left, int(pairing.CODE_LIFETIME_SECONDS) - 10)
        self.clock.advance(100)
        self.assertEqual(self.host.seconds_left, 0)
        self.assertIsNone(self.host.code)
        self.assertEqual(self.host.outcome, "expired")

    def test_cancelling_leaves_no_outcome(self):
        self.host.begin()
        self.host.cancel()
        self.assertFalse(self.host.active)
        self.assertIsNone(self.host.outcome)


if __name__ == "__main__":
    unittest.main()
