"""This PC's hardware address for the handshake's `hw` field. The Win32 lookup runs only on Windows."""

import os
import re
import sys
import unittest
from unittest import mock

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hardware_win
from core import pairing

MAC_FORM = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


class FormatMacTest(unittest.TestCase):
    def test_six_bytes_become_lower_case_colon_groups(self):
        self.assertEqual(hardware_win.format_mac(b"\xaa\xbb\xcc\x0d\xee\xff"), "aa:bb:cc:0d:ee:ff")

    def test_wrong_length_is_unknown(self):
        self.assertEqual(hardware_win.format_mac(b"\xaa\xbb\xcc\xdd\xee"), "")
        self.assertEqual(hardware_win.format_mac(b"\xaa\xbb\xcc\xdd\xee\xff\x00\x01"), "")
        self.assertEqual(hardware_win.format_mac(b""), "")

    def test_all_zero_is_unknown(self):
        self.assertEqual(hardware_win.format_mac(bytes(6)), "")


class HardwareAddressTowardsTest(unittest.TestCase):
    def test_off_windows_is_unknown_without_touching_the_network(self):
        with mock.patch.object(hardware_win.sys, "platform", "darwin"), \
                mock.patch.object(pairing, "local_address_towards") as route:
            self.assertEqual(hardware_win.hardware_address_towards("192.168.77.5"), "")
        route.assert_not_called()

    def test_garbage_host_is_unknown(self):
        self.assertEqual(hardware_win.hardware_address_towards("not a host"), "")
        self.assertEqual(hardware_win.hardware_address_towards(""), "")

    def test_a_failing_lookup_never_raises(self):
        with mock.patch.object(hardware_win.sys, "platform", "win32"), \
                mock.patch.object(pairing, "local_address_towards", side_effect=OSError("no route")):
            self.assertEqual(hardware_win.hardware_address_towards("192.168.77.5"), "")

    def test_a_failing_adapter_walk_never_raises(self):
        with mock.patch.object(hardware_win.sys, "platform", "win32"), \
                mock.patch.object(pairing, "local_address_towards", return_value="192.168.77.3"), \
                mock.patch.object(hardware_win, "_adapter_table", side_effect=OSError("boom")):
            self.assertEqual(hardware_win.hardware_address_towards("192.168.77.5"), "")

    def test_the_adapter_that_owns_the_routed_address_wins(self):
        table = [("10.1.0.9", b"\x02\x00\x00\x00\x00\x01"), ("192.168.77.3", b"\xaa\xbb\xcc\xdd\xee\xff")]
        with mock.patch.object(hardware_win.sys, "platform", "win32"), \
                mock.patch.object(pairing, "local_address_towards", return_value="192.168.77.3"), \
                mock.patch.object(hardware_win, "_adapter_table", return_value=table):
            self.assertEqual(hardware_win.hardware_address_towards("192.168.77.5"), "aa:bb:cc:dd:ee:ff")

    def test_an_all_zero_adapter_is_skipped(self):
        table = [("192.168.77.3", bytes(6))]
        with mock.patch.object(hardware_win.sys, "platform", "win32"), \
                mock.patch.object(pairing, "local_address_towards", return_value="192.168.77.3"), \
                mock.patch.object(hardware_win, "_adapter_table", return_value=table):
            self.assertEqual(hardware_win.hardware_address_towards("192.168.77.5"), "")


@unittest.skipUnless(sys.platform == "win32", "GetAdaptersAddresses exists only on Windows")
class RealAdapterTest(unittest.TestCase):
    def test_result_is_unknown_or_well_formed(self):
        for host in ("192.0.2.1", "192.168.77.5"):
            got = hardware_win.hardware_address_towards(host)
            self.assertTrue(got == "" or MAC_FORM.match(got), got)


if __name__ == "__main__":
    unittest.main()
