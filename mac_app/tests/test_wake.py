import threading
import types
import unittest
import unittest.mock
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config
import wake
from bridge_fakes import PAIRED_TOKEN, FakeQuartz, quiet_logger
from core import protocol
from fake_link import FakeLink, bring_up
from wake import REFUSED_KINDS, WakingController, not_woken_status, waking_status, mac_from_arp_output, magic_packet, parse_mac


WINDOWS_ARP = """
Interface: 192.168.1.5 --- 0x10
  Internet Address      Physical Address      Type
  192.168.1.1           02-00-00-00-00-01     dynamic
  192.168.1.3           02-1a-2b-3c-0d-4e     dynamic
  192.168.1.30          00-11-22-33-44-55     dynamic
  224.0.0.22            01-00-5e-00-00-16     static
"""

MAC_ARP = "? (192.168.1.3) at 2:1a:2b:3c:d:4e on en0 ifscope [ethernet]\n"


class FakeClock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


PEER = "192.0.2.10"


def make_config(mac="02:1A:2B:3C:0D:4E"):
    return config.Config(
        host="192.0.2.10",
        port=51820,
        auth_token=PAIRED_TOKEN,
        reconnect_interval_s=0.1,
        mac_address=mac,
    )


class ParsingTests(unittest.TestCase):
    def test_parse_mac_normalises_both_platforms(self):
        self.assertEqual(parse_mac("02-1a-2b-3c-0d-4e"), "02:1A:2B:3C:0D:4E")
        self.assertEqual(parse_mac("2:1a:2b:3c:d:4e"), "02:1A:2B:3C:0D:4E")
        self.assertEqual(parse_mac(" 02:1A:2B:3C:0D:4E "), "02:1A:2B:3C:0D:4E")
        self.assertIsNone(parse_mac("02:1a:2b:3c:0d"))
        self.assertIsNone(parse_mac("not a mac"))
        self.assertIsNone(parse_mac(""))
        self.assertIsNone(parse_mac(None))

    def test_windows_arp_line_yields_the_right_host(self):
        self.assertEqual(mac_from_arp_output(WINDOWS_ARP, "192.168.1.3"), "02:1A:2B:3C:0D:4E")
        # 192.168.1.3 is a prefix of 192.168.1.30; the match must be the whole address.
        self.assertEqual(mac_from_arp_output(WINDOWS_ARP, "192.168.1.30"), "00:11:22:33:44:55")
        self.assertIsNone(mac_from_arp_output(WINDOWS_ARP, "192.168.1.9"))

    def test_macos_arp_line_is_padded(self):
        self.assertEqual(mac_from_arp_output(MAC_ARP, "192.168.1.3"), "02:1A:2B:3C:0D:4E")
        self.assertIsNone(mac_from_arp_output("? (192.168.1.3) at (incomplete) on en0 ifscope [ethernet]\n", "192.168.1.3"))


class MagicPacketTests(unittest.TestCase):
    def test_layout_is_six_ff_then_the_address_sixteen_times(self):
        packet = magic_packet("02-1a-2b-3c-0d-4e")
        address = bytes.fromhex("021a2b3c0d4e")
        self.assertEqual(len(packet), 102)
        self.assertEqual(packet[:6], b"\xff" * 6)
        self.assertEqual(packet[6:], address * 16)
        for index in range(16):
            self.assertEqual(packet[6 + index * 6 : 12 + index * 6], address)

    def test_rejects_a_bad_address(self):
        with self.assertRaises(ValueError):
            magic_packet("nope")


class WakingControllerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.sent = []
        self.learned = []

        self.controller = WakingController(
            make_config(),
            wake_sender=lambda mac, host: self.sent.append((mac, host)),
            mac_lookup=lambda host: "AA:BB:CC:DD:EE:FF",
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=self.clock,
            link_factory=FakeLink,
        )
        self.link = self.controller._primary_link
        self.controller.WAKE_POLL_SECONDS = 0.01
        self.alerts = []
        self.controller.on_user_alert = lambda title, message: self.alerts.append(message)
        self.controller.on_mac_learned = self.learned.append

    def _wait_for_wake_thread(self):
        for thread in threading.enumerate():
            if thread.name == "wake":
                thread.join(timeout=5)

    def test_a_pc_that_answered_and_refused_is_not_woken(self):
        for kind in REFUSED_KINDS:
            with self.subTest(kind=kind):
                self.link.kind = kind
                self.assertFalse(self.controller.set_redirecting(True))
                self._wait_for_wake_thread()
                self.assertEqual(self.sent, [])
                self.assertFalse(self.controller.waking)

    def test_a_pc_that_did_not_answer_is_woken_for_every_other_kind(self):
        self.controller.stop_event.set()
        for kind in ("unreachable", "timeout", "stopped", "closed", "failed"):
            with self.subTest(kind=kind):
                self.sent.clear()
                self.link.kind = kind
                self.assertFalse(self.controller.set_redirecting(True))
                self._wait_for_wake_thread()
                self.assertEqual(len(self.sent), 1)

    def test_switch_while_unreachable_sends_the_packet_and_shows_waking(self):
        # The wake thread must see the packet as sent before the window is checked.
        self.controller.stop_event.set()
        self.assertFalse(self.controller.set_redirecting(True))
        self._wait_for_wake_thread()
        self.assertEqual(self.sent, [("02:1A:2B:3C:0D:4E", "192.0.2.10")])
        self.assertFalse(self.controller.redirecting)
        self.assertTrue(self.controller.outbound.empty())
        self.assertIn(waking_status(PEER), self.alerts)

    def test_gives_up_after_the_window_with_nothing_queued(self):
        self.assertTrue(self.controller.wake())
        self.assertTrue(self.controller.waking)
        self.assertEqual(self.controller.connection_status, waking_status(PEER))
        self.assertFalse(self.controller.wake(), "a second wake while one is in flight is refused")
        self.clock.value += wake.WAKE_WINDOW_SECONDS
        self._wait_for_wake_thread()
        self.assertFalse(self.controller.waking)
        self.assertEqual(self.controller.connection_status, not_woken_status(PEER))
        self.assertEqual(self.alerts[-1], not_woken_status(PEER))
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.controller.outbound.empty())

    def test_status_written_underneath_a_wake_shows_through_afterwards(self):
        self.controller.wake()
        self.controller.connection_status = "Windows is unreachable from this Mac"
        self.assertEqual(self.controller.connection_status, waking_status(PEER))
        self.clock.value += wake.WAKE_WINDOW_SECONDS
        self._wait_for_wake_thread()
        self.assertEqual(self.controller.connection_status, not_woken_status(PEER))

    def test_without_a_hardware_address_the_switch_fails_as_before(self):
        controller = WakingController(
            make_config(mac=""),
            wake_sender=lambda mac, host: self.sent.append((mac, host)),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=self.clock,
            link_factory=FakeLink,
        )
        controller.on_user_alert = lambda title, message: self.alerts.append(message)
        self.assertFalse(controller.set_redirecting(True))
        self.assertEqual(self.sent, [])
        self.assertFalse(controller.can_wake)
        self.assertTrue(self.alerts[0].startswith("Cannot switch"))

    def test_a_slow_address_lookup_does_not_hold_the_link_up_callback(self):
        release = threading.Event()
        self.controller.mac_lookup = lambda host: release.wait(5) and None
        done = threading.Event()
        threading.Thread(target=lambda: (bring_up(self.controller), done.set()), daemon=True).start()
        try:
            self.assertTrue(done.wait(1.0), "the link's up callback waited for the address lookup")
        finally:
            release.set()

    def test_a_primary_link_up_cannot_overwrite_a_settings_change_made_meanwhile(self):
        newer = make_config()
        newer.reconnect_interval_s = 7.0
        import bridge as bridge_module

        def racing_replace(cfg, **changes):
            result = real(cfg, **changes)
            thread = threading.Thread(target=lambda: self.controller.apply_settings(newer))
            thread.start()
            thread.join(0.3)
            return result

        real = bridge_module.dataclasses.replace
        with unittest.mock.patch.object(bridge_module.dataclasses, "replace", racing_replace):
            with self.controller.book.lock:
                self.controller.book.data["peers"][0]["host"] = "192.0.2.88"
            bring_up(self.controller)
            self._wait_for_learning()
        self.assertIs(self.controller.cfg, newer)

    def _wait_for_learning(self):
        for thread in threading.enumerate():
            if thread.name == "Beamer-learn-mac":
                thread.join(timeout=5)

    def test_successful_connection_learns_and_reports_a_new_address(self):
        bring_up(self.controller)
        self._wait_for_learning()
        self.assertEqual(self.learned, ["AA:BB:CC:DD:EE:FF"])
        self.assertEqual(self.controller.cfg.mac_address, "AA:BB:CC:DD:EE:FF")
        self.assertTrue(self.controller.connected)
        self.assertFalse(self.controller.can_wake)

    def test_the_address_is_looked_up_at_the_host_the_link_dialled(self):
        asked = []
        self.controller.mac_lookup = lambda host: asked.append(host)
        with self.controller.book.lock:
            self.controller.book.data["peers"][0]["host"] = "192.0.2.99"
        bring_up(self.controller)
        self._wait_for_learning()
        self.assertEqual(asked, ["192.0.2.99"])

    def test_an_address_the_table_already_agrees_with_is_not_reported(self):
        self.controller.mac_lookup = lambda host: "02:1A:2B:3C:0D:4E"
        bring_up(self.controller)
        self._wait_for_learning()
        self.assertEqual(self.learned, [])


class ComingHomeThroughTheAppsControllerTests(unittest.TestCase):
    """The app runs WakingController, so every way home the base class takes must pass through it."""

    def make(self):
        controller = WakingController(make_config(), logger=quiet_logger(), quartz=FakeQuartz, clock=FakeClock(),
                                      link_factory=FakeLink, desktop_bounds=lambda: (0, 0, 1920, 1080))
        link = bring_up(controller)
        self.assertTrue(controller.set_redirecting(True))
        return controller, link

    def test_the_pc_sending_input_home_returns_it(self):
        controller, link = self.make()
        link.arrives(controller, protocol.switch_v6(1, bytes(controller.identity()["id"])))
        self.assertFalse(controller.redirecting)

    def test_the_pc_taking_this_mac_over_ends_the_redirect(self):
        controller, _ = self.make()
        controller.set_receiving(True)
        self.assertFalse(controller.redirecting)
        self.assertTrue(controller.receiving)

    def test_a_dropped_link_returns_input_without_waking_anything(self):
        woken = []
        controller, link = self.make()
        controller.wake_sender = lambda mac, host: woken.append(mac)
        link.down(controller)
        self.assertFalse(controller.redirecting)
        self.assertEqual(woken, [])


class FollowingAMovedPcThroughTheAppsControllerTests(unittest.TestCase):
    def test_the_app_controller_offers_the_beacons_and_takes_the_new_address(self):
        cfg = make_config()
        cfg.pc_name = "STUDIO-PC"
        controller = WakingController(cfg, logger=quiet_logger(), quartz=FakeQuartz, clock=FakeClock(),
                                      link_factory=FakeLink, mac_lookup=lambda host: None)
        beacons = [{"name": "STUDIO-PC", "address": "192.0.2.77", "port": cfg.port, "reply_port": 24821, "pair_id": None}]
        controller.discovery = types.SimpleNamespace(pcs=lambda: beacons)
        learned = []
        controller.on_host_learned = learned.append
        controller._follow_the_peers()
        self.assertEqual(controller._primary_link.followed, [beacons])
        with controller.book.lock:
            controller.book.data["peers"][0]["host"] = "192.0.2.77"
        bring_up(controller)
        self.assertEqual(learned, ["192.0.2.77"])
        self.assertEqual(controller.cfg.host, "192.0.2.77")


if __name__ == "__main__":
    unittest.main()
