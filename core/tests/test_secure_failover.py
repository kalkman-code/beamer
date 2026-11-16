"""Secure endpoint failover and receiver handoff, with synthetic keys on loopback."""

import errno
import socket
import threading
import time
import unittest

from core import protocol, receiver
from core.link import LinkEnded
from core.tests.responder_harness import B, HERE, Initiator, Machine, entry, wait_for
from core.tests.test_link import Side, Scripted, scripted_session


class SecureFailoverTests(unittest.TestCase):
    def machine(self, own=HERE):
        machine = Machine([entry(B, "Driver")], own=own).start()
        self.addCleanup(machine.stop)
        return machine

    def connect(self, side, endpoints):
        attempts = []

        def dial(target, timeout):
            attempts.append(target)
            endpoint = endpoints.get(target[1])
            if endpoint is None:
                raise ConnectionRefusedError(errno.ECONNREFUSED, "synthetic refusal")
            return socket.create_connection(("127.0.0.1", endpoint.port), timeout=timeout)

        link = side.link(socket_factory=dial, reconnect_seconds=30)
        self.addCleanup(link.stop)
        link.start()
        return link, attempts

    def test_refused_normal_endpoint_tries_secure_without_reconnect_delay_or_port_change(self):
        secure = self.machine()
        side = Side(platform="windows")
        link, attempts = self.connect(side, {24822: secure})
        self.assertTrue(wait_for(link.live))
        self.assertEqual(attempts, [("127.0.0.1", 24820), ("127.0.0.1", 24822)])
        self.assertEqual(side.peer()["port"], 24820)
        self.assertEqual(link.dialled, ("127.0.0.1", 24822))

    def test_a_healthy_normal_endpoint_never_tries_secure(self):
        normal = self.machine()
        side = Side(platform="windows")
        link, attempts = self.connect(side, {24820: normal})
        self.assertTrue(wait_for(link.live))
        self.assertEqual(attempts, [("127.0.0.1", 24820)])

    def test_normal_receiver_on_secure_port_keeps_clipboard_announcements_and_text(self):
        normal = self.machine()
        side = Side(platform="windows")
        side.peer()["port"] = 24822
        link, attempts = self.connect(side, {24822: normal})
        self.assertTrue(wait_for(link.live))
        link.post(protocol.focus_v6(1, HERE))
        self.assertTrue(wait_for(lambda: normal.responder.owner == B))
        link.post(protocol.paired_msg([bytes(range(16))]))
        link.post(protocol.clipboard_msg("ordinary clipboard"))
        link.send_input({"type": protocol.MSG_TEXT, "data": {"text": "ordinary text"}})
        self.assertTrue(wait_for(lambda: bool(normal.paired)))
        self.assertTrue(wait_for(lambda: bool(normal.clipboard.set_calls)))
        self.assertTrue(wait_for(lambda: ("text", "ordinary text") in normal.injected()))
        self.assertEqual(attempts, [("127.0.0.1", 24822)])

    def test_normal_receiver_on_secure_port_can_use_macos_tunnel(self):
        normal = self.machine()
        side = Side(platform="windows")
        side.peer()["port"] = 24822

        def unreachable(target, timeout):
            raise OSError(errno.EHOSTUNREACH, "synthetic blocked LAN")

        link = side.link(socket_factory=unreachable, tunnel=lambda: socket.create_connection(
            ("127.0.0.1", normal.port), timeout=1))
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(link.live))
        self.assertTrue(link.via_tunnel)

    def test_unpinned_non_windows_and_migrated_peers_never_try_secure(self):
        for fields in ({"platform": "macos"}, {"platform": "linux"},
                       {"platform": "windows", "id": ""},
                       {"platform": "windows", "from_1_4": True}):
            with self.subTest(fields=fields):
                side = Side(platform="windows")
                side.peer().update(fields)
                link, attempts = self.connect(side, {})
                self.assertTrue(wait_for(lambda: bool(side.states) or not protocol.linkable(side.peer())))
                self.assertNotIn(("127.0.0.1", 24822), attempts)
                link.stop()

    def test_wrong_authenticated_identity_does_not_enable_secure_fallback(self):
        normal = self.machine(own=bytes(range(16)))
        side = Side(platform="windows")
        link, attempts = self.connect(side, {24820: normal})
        self.assertTrue(wait_for(lambda: link.kind == "different"))
        self.assertEqual(attempts, [("127.0.0.1", 24820)])

    def test_tampered_welcome_does_not_enable_secure_fallback(self):
        def tampered(connection):
            session, _ = scripted_session(connection, Side().peer()["token"])
            welcome = protocol.welcome_v6(HERE, "Windows", "windows", "1.5.0", [], True)
            frame = bytearray(session.seal(welcome))
            frame[-1] ^= 1
            connection.sendall(frame)

        normal = Scripted(tampered)
        self.addCleanup(normal.stop)
        side = Side(platform="windows")
        link, attempts = self.connect(side, {24820: normal})
        self.assertTrue(wait_for(lambda: link.kind == "unauthenticated"))
        self.assertEqual(attempts, [("127.0.0.1", 24820)])

    def test_secure_endpoint_keeps_identity_check(self):
        secure = self.machine(own=bytes(range(16)))
        side = Side(platform="windows")
        link, attempts = self.connect(side, {24822: secure})
        self.assertTrue(wait_for(lambda: link.kind == "different"))
        self.assertFalse(link.live())
        self.assertEqual(side.peer()["id"], protocol.id_text(HERE))
        self.assertEqual(len(attempts), 2)

    def test_a_discovery_address_never_enables_secure_fallback(self):
        side = Side(platform="windows")
        attempts = []

        def dial(target, timeout):
            attempts.append(target)
            raise ConnectionRefusedError(errno.ECONNREFUSED, "synthetic refusal")

        link = side.link(socket_factory=dial, reconnect_seconds=30)
        self.addCleanup(link.stop)
        with self.assertRaises(LinkEnded):
            link._connect_endpoints(side.peer(), "127.0.0.2", 24820, direct=True)
        self.assertEqual(attempts, [("127.0.0.2", 24820)])

    def test_secure_transport_drops_announcements_clipboard_and_non_basic_input(self):
        secure = self.machine()
        side = Side(platform="windows")
        link, _ = self.connect(side, {24822: secure})
        self.assertTrue(wait_for(link.live))
        link.post(protocol.focus_v6(1, HERE))
        self.assertTrue(wait_for(lambda: secure.responder.owner == B))
        link.post(protocol.paired_msg([bytes(range(16))]))
        link.post(protocol.clipboard_msg("must stay on the driver"))
        link.send_input({"type": protocol.MSG_TEXT, "data": {"text": "private"}})
        link.send_input({"type": protocol.MSG_GESTURE, "data": {"name": "swipe_left"}})
        link.send_input(protocol.mousemove_msg(7, 9))
        self.assertTrue(wait_for(lambda: ("move", 7, 9) in secure.injected()))
        self.assertEqual(secure.paired, [])
        self.assertEqual(secure.clipboard.set_calls, [])
        self.assertEqual(secure.injected(), [("move", 7, 9)])

    def test_a_protocol_version_failure_never_enables_secure_fallback(self):
        def older(connection):
            connection.recv(62)
            connection.sendall(b"BEAMY\x05" + bytes(8))

        normal = Scripted(older)
        self.addCleanup(normal.stop)
        side = Side(platform="windows")
        link, attempts = self.connect(side, {24820: normal})
        self.assertTrue(wait_for(lambda: link.kind in {"older", "not_beamer"}))
        self.assertEqual(attempts, [("127.0.0.1", 24820)])

    def test_a_secure_disconnect_tries_normal_first_and_requires_fresh_focus(self):
        secure, normal = self.machine(), self.machine()
        side = Side(platform="windows")
        available_normal = threading.Event()
        attempts = []

        def dial(target, timeout):
            attempts.append(target[1])
            if target[1] == 24820 and not available_normal.is_set():
                raise ConnectionRefusedError(errno.ECONNREFUSED, "synthetic refusal")
            machine = normal if target[1] == 24820 else secure
            return socket.create_connection(("127.0.0.1", machine.port), timeout=timeout)

        link = side.link(socket_factory=dial)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(link.live))
        link.post(protocol.focus_v6(1, HERE))
        self.assertTrue(wait_for(lambda: secure.responder.owner == B))
        link.send_input(protocol.key_msg(protocol.MSG_KEYDOWN, "a"))
        self.assertTrue(wait_for(lambda: ("key", "a", True) in secure.injected()))
        available_normal.set()
        secure.stop()
        self.assertTrue(wait_for(lambda: link.live() and link.dialled[1] == 24820))
        self.assertEqual(attempts[:3], [24820, 24822, 24820])
        self.assertIsNone(normal.responder.owner)
        self.assertEqual(normal.injected(), [])
        self.assertIn(("key", "a", False), secure.injected())
        link.post(protocol.focus_v6(2, HERE))
        self.assertTrue(wait_for(lambda: normal.responder.owner == B))
        link.post(protocol.clipboard_msg("ordinary clipboard after secure input"))
        link.send_input({"type": protocol.MSG_TEXT, "data": {"text": "ordinary text after secure input"}})
        self.assertTrue(wait_for(lambda: bool(normal.clipboard.set_calls)))
        self.assertTrue(wait_for(lambda: ("text", "ordinary text after secure input") in normal.injected()))


class DesktopAvailabilityTests(unittest.TestCase):
    def machine(self, available, limit=None):
        machine = Machine([entry(B, "Driver")])
        options = {"available": available}
        if limit is not None:
            options["max_frame_bytes"] = limit
        machine.responder = receiver.LinkResponder(
            machine.book,
            lambda: {"id": HERE, "name": "Here", "platform": "windows", "app": "1.5.0", "caps": []},
            status_callback=lambda *_: None, injector=machine.injector, desktop=machine.desktop,
            clipboard=machine.clipboard, **options)
        machine.start()
        self.addCleanup(machine.stop)
        return machine

    def test_unavailable_or_unknown_desktop_refuses_the_handshake(self):
        for value in (False, None, 1):
            with self.subTest(value=value):
                machine = self.machine(lambda: value)
                peer = Initiator(machine)
                self.addCleanup(peer.close)
                with self.assertRaises((protocol.ConnectionClosed, OSError)):
                    peer.handshake()
                self.assertEqual(machine.responder.links(), set())

    def test_idle_desktop_transition_closes_links_and_releases_held_input(self):
        available = threading.Event()
        available.set()
        machine = self.machine(available.is_set)
        peer = Initiator(machine)
        self.addCleanup(peer.close)
        peer.handshake()
        route = peer.take()
        self.assertIsNotNone(peer.answer(route))
        peer.key(protocol.MSG_KEYDOWN, "a")
        self.assertTrue(wait_for(lambda: ("key", "a", True) in machine.injected()))
        available.clear()
        self.assertTrue(wait_for(peer.closed.is_set))
        self.assertIsNone(machine.responder.owner)
        self.assertIn(("key", "a", False), machine.injected())

    def test_desktop_probe_failure_is_fail_closed(self):
        def broken():
            raise OSError("unknown desktop")

        machine = self.machine(broken)
        peer = Initiator(machine)
        self.addCleanup(peer.close)
        with self.assertRaises((protocol.ConnectionClosed, OSError)):
            peer.handshake()

    def test_input_after_transition_is_discarded_and_return_requires_new_ownership(self):
        available = threading.Event()
        available.set()
        machine = self.machine(available.is_set)
        peer = Initiator(machine)
        self.addCleanup(peer.close)
        peer.handshake()
        self.assertIsNotNone(peer.answer(peer.take()))
        available.clear()
        try:
            peer.move(91, 37)
        except OSError:
            pass
        self.assertTrue(wait_for(peer.closed.is_set))
        self.assertNotIn(("move", 91, 37), machine.injected())
        available.set()
        replacement = Initiator(machine)
        self.addCleanup(replacement.close)
        replacement.handshake()
        seq = replacement.move(17, 23)
        self.assertIsNotNone(replacement.acked(seq))
        self.assertIsNone(machine.responder.owner)
        self.assertNotIn(("move", 17, 23), machine.injected())

    def test_oversized_live_frame_header_closes_before_reading_its_body(self):
        machine = self.machine(lambda: True, limit=4096)
        peer = Initiator(machine)
        self.addCleanup(peer.close)
        peer.handshake()
        peer.sock.sendall((4097).to_bytes(4, "big"))
        self.assertTrue(wait_for(peer.closed.is_set))

    def test_first_frame_obeys_the_smaller_secure_limit(self):
        machine = self.machine(lambda: True, limit=512)
        peer = Initiator(machine)
        self.addCleanup(peer.close)
        peer.session.accept_preamble(protocol.recv_link_preamble(peer.sock, time.monotonic() + 2))
        peer.sock.sendall((513).to_bytes(4, "big"))
        peer.sock.settimeout(1)
        try:
            self.assertEqual(peer.sock.recv(1), b"")
        except ConnectionResetError:
            pass
