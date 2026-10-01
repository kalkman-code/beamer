"""This Mac's hardware address for the handshake's `hw` field. The getifaddrs walk runs only on macOS."""

import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hardware_mac
from core import pairing

MAC_FORM = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
TABLE = ({"192.168.1.5": "en0", "10.9.0.2": "utun3"}, {"en0": b"\xaa\xbb\xcc\x0d\xee\xff", "utun3": b""})


class FormatMacTest(unittest.TestCase):
    def test_six_bytes_become_lower_case_colon_groups(self):
        self.assertEqual(hardware_mac.format_mac(b"\xaa\xbb\xcc\x0d\xee\xff"), "aa:bb:cc:0d:ee:ff")

    def test_wrong_length_and_all_zero_are_unknown(self):
        self.assertEqual(hardware_mac.format_mac(b"\xaa\xbb\xcc\xdd\xee"), "")
        self.assertEqual(hardware_mac.format_mac(b""), "")
        self.assertEqual(hardware_mac.format_mac(bytes(6)), "")


class HardwareAddressTowardsTest(unittest.TestCase):
    def towards(self, host, local, table=TABLE):
        with mock.patch.object(pairing, "local_address_towards", return_value=local), \
                mock.patch.object(hardware_mac, "_interface_table", return_value=table):
            return hardware_mac.hardware_address_towards(host)

    def test_the_adapter_that_carries_the_route_names_the_address(self):
        self.assertEqual(self.towards("192.168.1.9", "192.168.1.5"), "aa:bb:cc:0d:ee:ff")

    def test_an_interface_with_no_link_layer_address_is_unknown(self):
        self.assertEqual(self.towards("10.9.0.1", "10.9.0.2"), "")

    def test_an_address_on_no_interface_is_unknown(self):
        self.assertEqual(self.towards("172.16.0.1", "172.16.0.2"), "")

    def test_failures_are_unknown_not_raised(self):
        with mock.patch.object(hardware_mac, "_interface_table", side_effect=OSError("getifaddrs")):
            self.assertEqual(hardware_mac.hardware_address_towards("192.168.1.9"), "")
        with mock.patch.object(pairing, "local_address_towards", side_effect=OSError("unreachable")):
            self.assertEqual(hardware_mac.hardware_address_towards("192.168.1.9"), "")

    def test_no_host_is_unknown_without_touching_the_network(self):
        with mock.patch.object(pairing, "local_address_towards") as route:
            self.assertEqual(hardware_mac.hardware_address_towards(""), "")
        route.assert_not_called()

    def test_off_macos_is_unknown_without_touching_the_network(self):
        with mock.patch.object(hardware_mac.sys, "platform", "win32"), \
                mock.patch.object(pairing, "local_address_towards") as route:
            self.assertEqual(hardware_mac.hardware_address_towards("192.168.1.9"), "")
        route.assert_not_called()


@unittest.skipUnless(sys.platform == "darwin", "getifaddrs layout is macOS's")
class LiveInterfaceTableTest(unittest.TestCase):
    def test_the_table_reads_this_macs_interfaces(self):
        ips, macs = hardware_mac._interface_table()
        self.assertIn("127.0.0.1", ips)
        self.assertTrue(macs)
        for raw in macs.values():
            self.assertIn(len(raw), (0, 6, 8))

    def test_every_real_address_is_well_formed(self):
        ips, macs = hardware_mac._interface_table()
        for name in ips.values():
            mac = hardware_mac.format_mac(macs.get(name, b""))
            self.assertTrue(mac == "" or MAC_FORM.match(mac), mac)


if __name__ == "__main__":
    unittest.main()
