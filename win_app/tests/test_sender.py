"""The Windows-to-Mac direction, driven over a real loopback socket against
the same receiver.py the Mac runs, so the two halves are proved against each
other rather than against a mock of each other."""

import socket
import threading
import time
import unittest
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import capture_win
from core import protocol
from core import receiver
import sender
from app_config import Config
from fakes import FakeClipboard, FakeDesktop, FakeInjector, wait_for_calls
from core.return_edge import Rect

MONITORS = [Rect(0, 0, 1920, 1080)]


def make_config(**overrides):
    values = dict(
        host="127.0.0.1",
        port=0,
        auth_token="shared-token",
        mac_host="127.0.0.1",
        mac_return_edge="left",
        mac_resistance_px=40,
        crossing_resistance_px=40,
    )
    values.update(overrides)
    return Config(**values)


class EdgeTests(unittest.TestCase):
    """The outward crossing, with no link in the way: the model, the gate and
    the pin, which is everything the hook thread does per mouse move."""

    def setUp(self):
        self.desktop = FakeDesktop(MONITORS, cursor=(0, 500))
        self.link = MacSenderWithLink(desktop=self.desktop)

    def tearDown(self):
        self.link.close()

    def push(self, times, dx=-20):
        """The hand still moving left with the cursor already stopped at the
        left edge: the position never changes, which is exactly why the
        movement has to come from raw input rather than from the hook."""
        for _ in range(times):
            self.link.sender.on_motion(dx, 0)

    def test_a_push_against_the_edge_crosses(self):
        self.push(4)
        self.assertTrue(self.link.sender.redirecting, "a sustained push against the left edge never crossed")

    def test_the_push_lights_the_edge_it_is_pressing(self):
        # The app once built its sender with no pressure callback, so the PC's own push out to
        # the Mac crossed in the dark while the same edge lit for the Mac's push home.
        seen = []
        self.link.sender._pressure_callback = lambda edge, pressure, crossed, part: seen.append((edge, crossed))
        self.push(4)
        self.assertTrue(seen, "a push against the edge reported no pressure")
        self.assertTrue(all(edge == "left" for edge, _ in seen))
        self.assertEqual(seen[-1][1], True)

    def test_part_of_the_edge_lights_only_the_third_it_is_pushing(self):
        seen = []
        self.link.sender.update_config(make_config(crossing_methods=["part"], crossing_edge_parts=["middle", "end"]))
        self.link.sender._pressure_callback = lambda edge, pressure, crossed, part: seen.append((edge, part))
        self.desktop.cursor = (0, 900)
        self.push(4)
        self.assertTrue(seen, "a push along a chosen third reported no pressure")
        self.assertEqual(set(seen), {("left", "end")})

    def test_the_third_is_measured_on_the_pointers_own_display(self):
        # The left display runs 200 to 1280 of a 1440-tall desktop: 1000 is its end third, though
        # on the desktop as a whole it would be the middle one.
        desktop = FakeDesktop([Rect(0, 200, 1920, 1080), Rect(1920, 0, 2560, 1440)], cursor=(0, 1000))
        link = MacSenderWithLink(desktop=desktop)
        self.addCleanup(link.close)
        seen = []
        link.sender.update_config(make_config(crossing_methods=["part"], crossing_edge_parts=["end"]))
        link.sender._pressure_callback = lambda edge, pressure, crossed, part: seen.append(part)
        for _ in range(4):
            link.sender.on_motion(-20, 0)
        self.assertEqual(set(seen), {"end"})

    def test_the_whole_edge_names_no_third(self):
        seen = []
        self.link.sender._pressure_callback = lambda edge, pressure, crossed, part: seen.append(part)
        self.push(4)
        self.assertEqual(set(seen), {None})

    def test_coming_home_reports_where_the_pointer_landed(self):
        # The crossing effects play their arrival where the pointer is placed.
        arrivals = []
        self.link.sender._arrival_callback = lambda edge, x, y: arrivals.append((edge, x, y))
        self.link.sender._handle_switch({"target": "windows", "edge": "left", "offset": 0.5})
        self.assertEqual(arrivals, [("left",) + self.desktop.set_calls[-1]])

    def test_a_raising_arrival_callback_only_logs(self):
        def boom(edge, x, y):
            raise RuntimeError("effect fell over")

        self.link.sender._arrival_callback = boom
        with self.assertLogs(sender.LOGGER, "ERROR"):
            self.link.sender._handle_switch({"target": "windows", "edge": "left", "offset": 0.5})
        self.assertTrue(self.desktop.set_calls)

    def test_a_switch_without_an_edge_while_already_home_reports_no_arrival(self):
        arrivals = []
        self.link.sender._arrival_callback = lambda edge, x, y: arrivals.append(edge)
        self.link.sender._handle_switch({"target": "windows"})
        self.assertEqual(arrivals, [])

    def _home_by(self, action):
        """Input on the Mac, then brought home by `action`; what the arrival callback heard."""
        arrivals = []
        self.link.sender._arrival_callback = lambda edge, x, y: arrivals.append((edge, x, y))
        self.link.sender.redirecting = True
        action()
        self.assertFalse(self.link.sender.redirecting)
        return arrivals

    def test_the_shortcut_home_reports_where_the_pointer_is(self):
        self.assertEqual(self._home_by(self.link.sender.toggle), [(None, 0, 500)])

    def test_the_mac_sending_input_home_by_its_switch_reports_it(self):
        arrivals = self._home_by(lambda: self.link.sender._handle_switch({"target": "windows"}))
        self.assertEqual(arrivals, [(None, 0, 500)])

    def test_a_crossing_home_reports_only_its_own_arrival(self):
        arrivals = self._home_by(
            lambda: self.link.sender._handle_switch({"target": "windows", "edge": "left", "offset": 0.5}))
        self.assertEqual([edge for edge, _x, _y in arrivals], ["left"])

    def test_the_mac_taking_this_pc_over_is_not_a_switch_home(self):
        self.assertEqual(self._home_by(lambda: self.link.sender.set_receiving(True)), [])

    def test_a_dropped_link_shows_nothing(self):
        self.assertEqual(self._home_by(lambda: self.link.sender._force_local("The Mac stopped responding")), [])

    def test_this_pcs_own_mouse_pushing_out_while_the_mac_drives_takes_the_pointer_across(self):
        # Before this, the Mac's trackpad could cross to the PC but the PC's own mouse could not
        # push back out. The Mac's injected moves carry INJECTED_MARK and never reach on_motion,
        # so a push here is the hand's: the Mac gets its input back and this mouse follows it across.
        sent_home = []
        self.link.sender.send_peer_home = lambda: sent_home.append(True) or True
        self.link.sender.set_receiving(True)
        self.push(4)
        self.assertEqual(sent_home, [True])
        self.assertTrue(self.link.sender.redirecting)

    def test_a_push_while_the_mac_drives_and_cannot_be_sent_home_stays_here(self):
        self.link.sender.send_peer_home = lambda: False
        self.link.sender.set_receiving(True)
        self.push(10)
        self.assertFalse(self.link.sender.redirecting)

    def test_the_shortcut_while_the_mac_is_driving_sends_its_input_home(self):
        sent_home = []
        self.link.sender.send_peer_home = lambda: sent_home.append(True) or True
        self.link.sender.set_receiving(True)
        self.assertTrue(self.link.sender.set_redirecting(True))
        self.assertEqual(sent_home, [True])
        self.assertFalse(self.link.sender.redirecting)

    def test_a_push_away_from_the_edge_never_crosses(self):
        self.push(10, dx=20)
        self.assertFalse(self.link.sender.redirecting)

    def test_a_pointer_off_the_edge_never_crosses(self):
        self.desktop.cursor = (900, 500)
        self.push(10)
        self.assertFalse(self.link.sender.redirecting)

    def test_the_edge_can_be_turned_off(self):
        self.link.sender.update_config(make_config(crossing_methods=["shortcut"]))
        self.push(10)
        self.assertFalse(self.link.sender.redirecting)
        self.assertTrue(self.link.sender.shortcut_armed)

    def test_the_shortcut_can_be_turned_off_on_its_own(self):
        self.link.sender.update_config(make_config(crossing_methods=["edge"]))
        self.assertFalse(self.link.sender.shortcut_armed)
        self.push(4)
        self.assertTrue(self.link.sender.redirecting)


class CornerTests(unittest.TestCase):
    """The corner is for a machine whose whole edge is busy: it wants a
    diagonal push in an 8-pixel box, and nothing else."""

    def setUp(self):
        self.desktop = FakeDesktop(MONITORS, cursor=(0, 0))
        self.link = MacSenderWithLink(desktop=self.desktop)
        self.link.sender.update_config(
            make_config(crossing_methods=["corner"], crossing_corner="top_left")
        )

    def tearDown(self):
        self.link.close()

    def test_a_diagonal_push_in_the_corner_crosses(self):
        for _ in range(4):
            self.link.sender.on_motion(-20, -20)
        self.assertTrue(self.link.sender.redirecting)

    def test_the_push_names_its_corner_for_the_effects(self):
        seen = []
        self.link.sender._pressure_callback = lambda edge, pressure, crossed, part: seen.append((edge, part))
        for _ in range(4):
            self.link.sender.on_motion(-20, -20)
        self.assertTrue(seen)
        self.assertEqual(set(part for _edge, part in seen), {"top_left"})

    def test_a_straight_push_along_the_edge_does_not(self):
        for _ in range(10):
            self.link.sender.on_motion(-20, 0)
        self.assertFalse(self.link.sender.redirecting)

    def test_the_same_diagonal_away_from_the_corner_does_not(self):
        self.desktop.cursor = (900, 500)
        for _ in range(10):
            self.link.sender.on_motion(-20, -20)
        self.assertFalse(self.link.sender.redirecting)


class SelfConnectionTests(unittest.TestCase):
    """The one way this PC could take its own daily link down: learning its
    own address as the Mac's, connecting to its own receiver with the shared
    token, and having the preempt rule close the real Mac's session."""

    def test_loopback_and_this_pcs_own_addresses_are_refused(self):
        self.assertTrue(sender.is_this_machine("127.0.0.1"))
        self.assertTrue(sender.is_this_machine("0.0.0.0"))
        self.assertTrue(sender.is_this_machine(""))
        self.assertTrue(sender.is_this_machine("192.168.1.3", ["192.168.1.3"]))

    def test_the_macs_address_is_not(self):
        self.assertFalse(sender.is_this_machine("192.168.1.5", ["192.168.1.3"], address_towards=lambda host: "192.168.1.3"))

    def test_this_pcs_own_real_address_is_not_missed_when_enumeration_fails(self):
        # local_addresses=[] is what pairing._local_ipv4_addresses() returns
        # when the hostname does not resolve; the route probe then answers,
        # and for this PC's own address it answers with that address.
        self.assertTrue(sender.is_this_machine("192.168.1.3", [], address_towards=lambda host: host))
        self.assertFalse(sender.is_this_machine("192.168.1.5", [], address_towards=lambda host: "192.168.1.3"))

    def test_a_route_probe_that_fails_does_not_stop_the_link(self):
        def refuse(host):
            raise OSError("network is unreachable")

        self.assertFalse(sender.is_this_machine("192.168.1.5", [], address_towards=refuse))

    def test_a_sender_pointed_at_this_pc_never_opens_a_link(self):
        attempts = []

        def refuse(address, timeout):
            attempts.append(address)
            raise AssertionError("the sender tried to connect to this PC")

        link = sender.MacSender(socket_factory=refuse, desktop=FakeDesktop(MONITORS))
        link.update_config(make_config(mac_host="127.0.0.1", port=51820))
        self.assertIsNone(link._ready_config())
        self.assertEqual(attempts, [])


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.injector = FakeInjector()
        self.clipboard = FakeClipboard()
        self.mac_desktop = FakeDesktop(MONITORS, cursor=(900, 500))
        self.statuses = []
        self.arrangements = []
        self.server = receiver.ReceiverServer(
            status_callback=lambda state, detail: self.statuses.append((state, detail)),
            arrangement_callback=lambda edge, set_at: self.arrangements.append((edge, set_at)),
            clipboard=self.clipboard,
            unlock=NoUnlock(),
            desktop=self.mac_desktop,
            injector=self.injector,
            self_name="Mac",
            peer_name="PC",
            self_target="mac",
            peer_target="windows",
        )
        self.port = free_port()
        self.server.start(Config(host="127.0.0.1", port=self.port, auth_token="shared-token"))
        self.desktop = FakeDesktop(MONITORS, cursor=(0, 500))
        # The loopback address is this PC's own, which the real guard refuses
        # to connect to; here it is the Mac at the other end of the test.
        self.sender = sender.MacSender(
            desktop=self.desktop, clipboard=FakeClipboard("copied"), is_local=lambda host: False
        )
        self.sender.start(make_config(port=self.port))
        self.assertTrue(wait_for(lambda: self.sender.connected), f"never connected: {self.sender.status}")

    def tearDown(self):
        self.sender.stop()
        self.server.stop()

    def test_keys_reach_the_mac_injector(self):
        self.sender.set_redirecting(True, arrival_edge="right", offset=0.5)
        self.sender.on_key("cmd", True)
        self.sender.on_key("c", True)
        self.sender.on_key("c", False)
        self.sender.on_key("cmd", False)
        wait_for_calls(self.injector.calls, 4)
        self.assertEqual(
            self.injector.calls,
            [
                ("key", ("cmd", True)),
                ("key", ("c", True)),
                ("key", ("c", False)),
                ("key", ("cmd", False)),
            ],
        )

    def test_the_switch_places_the_mac_pointer_and_arms_its_way_home(self):
        self.sender.set_redirecting(True, arrival_edge="right", offset=0.25)
        self.assertTrue(wait_for(lambda: self.server.return_edge == "right"))
        self.assertEqual(self.mac_desktop.cursor[0], MONITORS[0].right)

    def test_nothing_is_sent_while_input_is_this_pcs(self):
        self.sender.on_key("a", True)
        time.sleep(0.2)
        self.assertEqual(self.injector.calls, [])

    def test_the_mac_pushing_home_takes_input_back(self):
        self.sender.set_redirecting(True, arrival_edge="right", offset=0.5)
        self.assertTrue(wait_for(lambda: self.server.return_edge == "right"))
        # The Mac's own pointer, pushed off its right edge: the receiver
        # answers with a switch, which must stop this PC sending.
        self.mac_desktop.cursor = (MONITORS[0].right, 500)
        for _ in range(20):
            self.sender.on_motion(40, 0)
            self.mac_desktop.cursor = (MONITORS[0].right, 500)
            if not self.sender.redirecting:
                break
        self.assertTrue(wait_for(lambda: not self.sender.redirecting), "the Mac's push home was ignored")

    def test_the_arrangement_travels_to_the_mac(self):
        self.sender.send_arrangement("right", 1758100000)
        wait_for_calls(self.arrangements, 1)
        self.assertEqual(self.arrangements[0], ("right", 1758100000))

    def test_the_arrangement_travels_back_from_the_mac(self):
        heard = []
        self.sender._arrangement_callback = lambda edge, set_at: heard.append((edge, set_at))
        self.assertTrue(self.server.send_arrangement("top", 1758100001))
        wait_for_calls(heard, 1)
        self.assertEqual(heard[0], ("top", 1758100001))

    def test_a_malformed_arrangement_does_not_take_the_link_down(self):
        self.sender._send_raw({"type": protocol.MSG_ARRANGEMENT, "data": {"mac_edge": "sideways"}})
        time.sleep(0.2)
        self.assertEqual(self.arrangements, [])
        self.assertTrue(self.sender.connected)

    def test_the_clipboard_travels_with_the_switch(self):
        self.sender.set_redirecting(True, arrival_edge="right", offset=0.5)
        wait_for_calls(self.clipboard.set_calls, 1)
        self.assertEqual(self.clipboard.set_calls[0][0], "copied")


class ConnectionFailedTests(unittest.TestCase):
    """A deliberate stop drops the socket before the reader thread's blocked
    recv() notices, so the OSError it then raises names a socket nobody owns
    any more -- Windows' WinError 10038 on the off switch, reproduced here
    without a real stop()."""

    def setUp(self):
        self.statuses = []
        self.redirected = []
        self.sender = sender.MacSender(
            status_callback=lambda connected, detail: self.statuses.append(detail),
            redirect_callback=self.redirected.append,
        )

    def test_an_error_on_an_already_dropped_socket_is_not_a_failure(self):
        old_sock, peer = socket.socketpair()
        self.addCleanup(old_sock.close)
        self.addCleanup(peer.close)
        self.sender._sock = old_sock
        self.sender.redirecting = True
        self.sender._sock = None  # what stop()/_drop_connection() does first
        self.sender._connection_failed("Receiving from the Mac failed: boom", expected_socket=old_sock)
        self.assertEqual(self.statuses, [])
        self.assertEqual(self.redirected, [])
        self.assertTrue(self.sender.redirecting, "a stale error forced input back to this PC")

    def test_an_error_on_the_live_socket_is_still_reported(self):
        sock, peer = socket.socketpair()
        self.addCleanup(sock.close)
        self.addCleanup(peer.close)
        self.sender._sock = sock
        self.sender.redirecting = True
        self.sender._connection_failed("Receiving from the Mac failed: boom", expected_socket=sock)
        self.assertEqual(self.statuses, ["Receiving from the Mac failed: boom"])
        self.assertFalse(self.sender.redirecting)


class NoUnlock:
    def is_locked(self):
        return None


class MacSenderWithLink:
    """A sender with no socket at all, for the edge tests: `connected` is
    forced true so the gate opens, and nothing is ever sent."""

    def __init__(self, desktop):
        self.sender = sender.MacSender(desktop=desktop, clipboard=FakeClipboard(), is_local=lambda host: False)
        self.sender.update_config(make_config())
        self.sender._sock = object()
        self.sender._connected_at = time.monotonic()
        self.sender._last_ack_at = time.monotonic()

    def close(self):
        self.sender._sock = None


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False



class LinkDropTests(unittest.TestCase):
    def test_the_edge_crosses_again_after_the_link_dropped_while_redirecting(self):
        desktop = FakeDesktop(MONITORS, cursor=(0, 500))
        link = MacSenderWithLink(desktop=desktop)
        self.addCleanup(link.close)
        sender = link.sender
        for _ in range(4):
            sender.on_motion(-20, 0)
        self.assertTrue(sender.redirecting)
        sender._force_local("the link to the Mac went quiet")
        sender._sock = object()
        sender._connected_at = sender._last_ack_at = time.monotonic()
        desktop.cursor = (0, 500)
        for _ in range(6):
            sender.on_motion(-20, 0)
        self.assertTrue(sender.redirecting)


if __name__ == "__main__":
    unittest.main()
