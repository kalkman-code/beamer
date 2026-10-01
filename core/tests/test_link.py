"""core.link.OutboundLink, the initiator's side of WIRE.md sections 2, 3 and 9, against a real
LinkResponder on loopback (responder_harness.Machine) and a scripted responder where a test needs
a peer that misbehaves."""

import socket
import threading
import time
import unittest
from unittest import mock

from core import protocol, receiver
from core import link as link_module
from core.link import OutboundLink
from core.tests.responder_harness import B, C, CAPS, HERE, TOKENS, Machine, Settings, entry, wait_for

LOOPBACK = "127.0.0.1"


class Side:
    """The initiator's own settings, with one peer entry for the machine it dials."""

    def __init__(self, machine=None, peer=HERE, own=B, token=None, **fields):
        port = machine.port if machine is not None else 24820
        data = entry(peer, "Far", token=token or TOKENS[B], host=LOOPBACK, port=port, send=True, **fields)
        self.settings = Settings([data])
        self.settings.data["machine_id"] = protocol.id_text(own)
        self.own = own
        self.book = receiver.PeerBook(self.settings.load, self.settings.save)
        self.states, self.ups, self.messages, self.notices = [], [], [], []

    def identity(self, **fields):
        data = {"id": self.own, "name": "Near", "platform": "macos", "app": "1.5.0", "caps": list(CAPS), "port": 24820}
        data.update(fields)
        return data

    def link(self, **kwargs):
        kwargs.setdefault("reconnect_seconds", 0.05)
        link = OutboundLink(
            self.settings.data["peers"][0]["token"], self.book, self.identity,
            state=lambda link, up, text: self.states.append((up, text)),
            up=lambda link, fields: self.ups.append(fields),
            message=lambda link, message: self.messages.append(message),
            notice=self.notices.append,
            **kwargs,
        )
        return link

    def peer(self):
        return self.settings.data["peers"][0]

    def last_text(self):
        return self.states[-1][1] if self.states else None


def machine_with_peer(**kwargs):
    from core.tests.responder_harness import entry as make
    return Machine([make(B, "Peer")], **kwargs).start()


class Scripted:
    """A listener that runs `script(connection)` for each connection."""

    def __init__(self, script):
        self.script = script
        self.listener = socket.socket()
        self.listener.bind((LOOPBACK, 0))
        self.listener.listen(4)
        self.port = self.listener.getsockname()[1]
        self.connections = 0
        self.stopped = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stopped:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._one, args=(connection,), daemon=True).start()

    def _one(self, connection):
        try:
            self.script(connection)
        except Exception:
            pass
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def stop(self):
        self.stopped = True
        self.listener.close()


def scripted_session(connection, token):
    """The responder's half of the handshake up to its own preamble; the session and hello."""
    session = protocol.LinkSession(token, protocol.ROLE_RESPONDER)
    session.accept_preamble(protocol.recv_link_preamble(connection, time.monotonic() + 5))
    connection.sendall(session.preamble())
    hello = protocol.read_hello(protocol.recv_first_msg(connection, session, time.monotonic() + 5))
    return session, hello


class LinkHandshakeTests(unittest.TestCase):
    def setUp(self):
        self.machine = machine_with_peer()
        self.addCleanup(self.machine.stop)

    def start(self, side, **kwargs):
        link = side.link(**kwargs)
        self.addCleanup(link.stop)
        link.start()
        return link

    def test_links_and_saves_what_the_welcome_taught(self):
        side = Side(self.machine)
        link = self.start(side)
        self.assertTrue(wait_for(lambda: side.ups), side.states)
        fields = side.ups[0]
        self.assertEqual(fields["id"], HERE)
        self.assertTrue(fields["accepts"])
        self.assertEqual(fields["platform"], "windows")
        self.assertIn("clipboard", fields["caps"])
        self.assertTrue(link.live())
        self.assertTrue(side.peer()["linked"])
        self.assertEqual(side.peer()["name"], "Here")
        self.assertEqual(link.peer_id, protocol.id_text(HERE))
        self.assertTrue(wait_for(lambda: self.machine.links))

    def test_a_migrated_entry_learns_the_peer_id(self):
        side = Side(self.machine, linked=False, from_1_4=True)
        side.peer()["id"] = ""
        self.start(side)
        self.assertTrue(wait_for(lambda: side.ups), side.states)
        self.assertEqual(side.peer()["id"], protocol.id_text(HERE))
        self.assertTrue(side.peer()["linked"])
        self.assertTrue(any("for the first time on Beamer 1.5.0" in text for text in side.notices), side.notices)

    def test_accepts_is_false_when_the_peer_does_not_let_this_machine_drive(self):
        self.machine.settings.peer(TOKENS[B])["allow_drive"] = False
        side = Side(self.machine)
        self.start(side)
        self.assertTrue(wait_for(lambda: side.ups), side.states)
        self.assertFalse(side.ups[0]["accepts"])

    def test_a_different_id_than_the_entry_has_is_refused_and_taught_nothing(self):
        side = Side(self.machine, peer=ident_other())
        self.start(side)
        self.assertTrue(wait_for(lambda: side.states and not side.states[-1][0]), side.states)
        self.assertEqual(side.ups, [])
        self.assertIn("different Beamer", side.last_text())

    def test_a_typed_token_is_never_dialled(self):
        side = Side(self.machine, token="hunter2")
        link = self.start(side)
        time.sleep(0.4)
        self.assertEqual(self.machine.links, [])
        self.assertFalse(link.live())

    def test_send_off_is_not_dialled(self):
        side = Side(self.machine)
        side.peer()["send"] = False
        self.start(side)
        time.sleep(0.3)
        self.assertEqual(self.machine.links, [])

    def test_a_peer_that_does_not_know_the_key_id_closes_and_the_text_follows_linked(self):
        for linked, expected in ((False, "Update Beamer on Far to 1.5.0"), (True, "closed the connection")):
            side = Side(self.machine, token=other_token(), linked=linked)
            link = self.start(side)
            self.assertTrue(wait_for(lambda: side.states and not side.states[-1][0]), side.states)
            self.assertIn(expected, side.last_text())
            link.stop()

    def test_an_entry_removed_stops_the_link(self):
        side = Side(self.machine)
        link = self.start(side)
        self.assertTrue(wait_for(lambda: side.ups))
        side.settings.data["peers"].clear()
        self.assertTrue(wait_for(lambda: not link.live() and not link.running, 5.0))

    def test_one_failed_read_of_the_settings_does_not_end_the_link(self):
        side = Side(self.machine)
        reads = []
        load = side.settings.load

        def flaky():
            reads.append(1)
            if len(reads) == 1:
                raise PermissionError("settings.json is locked")
            return load()

        side.book = receiver.PeerBook(flaky, side.settings.save)
        link = self.start(side)
        self.assertTrue(wait_for(lambda: side.ups, 5.0), side.states)
        self.assertTrue(link.running)


def ident_other():
    return bytes((200 + i) % 256 for i in range(1, 17))


def other_token():
    import base64
    return base64.urlsafe_b64encode(bytes((9 + i) % 256 for i in range(32))).decode("ascii").rstrip("=")


class LinkRefusalTests(unittest.TestCase):
    def serve(self, script):
        server = Scripted(script)
        self.addCleanup(server.stop)
        return server

    def link(self, side, **kwargs):
        link = side.link(**kwargs)
        self.addCleanup(link.stop)
        link.start()
        return link

    def test_a_version_5_peer_is_named_as_older(self):
        def script(connection):
            connection.recv(62)
            connection.sendall(b"BEAMY\x05" + bytes(8))
            time.sleep(0.5)

        server = self.serve(script)
        side = Side(None)
        side.peer()["port"] = server.port
        self.link(side)
        self.assertTrue(wait_for(lambda: side.states), side.states)
        self.assertIn("Update Beamer on Far: it is older than this one", side.last_text())

    def test_two_machines_of_one_name_are_told_apart_in_what_the_link_says(self):
        def script(connection):
            connection.recv(62)
            connection.sendall(b"BEAMY\x05" + bytes(8))
            time.sleep(0.5)

        server = self.serve(script)
        side = Side(None)
        side.peer()["port"] = server.port
        side.peer()["name"] = "Studio"
        twin = entry(C, "studio", token=TOKENS[C], host="192.0.2.9", port=24820, send=True)
        side.settings.data["peers"].append(twin)
        self.link(side)
        self.assertTrue(wait_for(lambda: side.states), side.states)
        self.assertIn(f"Update Beamer on Studio ({protocol.id_text(HERE)[-4:]}): it is older than this one", side.last_text())

    def test_a_newer_peer_says_this_machine_is_older(self):
        def script(connection):
            connection.recv(62)
            connection.sendall(b"BEAMY\x07" + bytes(8))
            time.sleep(0.5)

        server = self.serve(script)
        side = Side(None)
        side.peer()["port"] = server.port
        self.link(side)
        self.assertTrue(wait_for(lambda: side.states))
        self.assertIn("newer Beamer", side.last_text())

    def test_a_peer_that_is_not_a_beamer_is_said_to_be_one_from_before_version_4(self):
        def script(connection):
            connection.recv(62)
            time.sleep(6)

        server = self.serve(script)
        side = Side(None)
        side.peer()["port"] = server.port
        self.link(side)
        self.assertTrue(wait_for(lambda: side.states, 8.0), side.states)
        self.assertIn("before version 4", side.last_text())

    def test_a_peer_that_answers_with_something_else_is_said_not_to_be_a_beamer(self):
        def script(connection):
            connection.recv(62)
            connection.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            time.sleep(0.5)

        server = self.serve(script)
        side = Side(None)
        side.peer()["port"] = server.port
        self.link(side)
        self.assertTrue(wait_for(lambda: side.states), side.states)
        self.assertEqual(side.last_text(), "Far did not answer as a Beamer")

    def test_a_responder_that_names_another_key_id_is_a_different_beamer(self):
        def script(connection):
            connection.recv(62)
            connection.sendall(b"BEAMY\x06" + bytes(8) + bytes(16) + bytes(32))
            time.sleep(0.5)

        server = self.serve(script)
        side = Side(None)
        side.peer()["port"] = server.port
        self.link(side)
        self.assertTrue(wait_for(lambda: side.states))
        self.assertIn("different Beamer", side.last_text())

    def test_wrong_id_stops_dialling_until_the_entry_changes(self):
        def script(connection):
            session, _ = scripted_session(connection, TOKENS[B])
            protocol.send_msg(connection, session, protocol.wrong_id_v6())
            time.sleep(0.3)

        server = self.serve(script)
        side = Side(None)
        side.peer()["port"] = server.port
        link = self.link(side)
        self.assertTrue(wait_for(lambda: side.states))
        self.assertIn("under another machine: remove it on both and pair again", side.last_text())
        seen = server.connections
        time.sleep(0.5)
        self.assertEqual(server.connections, seen)
        side.peer()["host"] = "localhost"
        self.assertTrue(wait_for(lambda: server.connections > seen, 3.0))
        link.stop()

    def test_invalid_hello_says_this_app_sent_something_unreadable(self):
        def script(connection):
            session, _ = scripted_session(connection, TOKENS[B])
            protocol.send_msg(connection, session, protocol.invalid_hello_v6())
            time.sleep(0.3)

        server = self.serve(script)
        side = Side(None)
        side.peer()["port"] = server.port
        self.link(side)
        self.assertTrue(wait_for(lambda: side.states))
        self.assertIn("could not read", side.last_text())

    def test_a_welcome_naming_this_machine_is_closed_with_nothing_taught(self):
        def script(connection):
            session, _ = scripted_session(connection, TOKENS[B])
            protocol.send_msg(connection, session, protocol.welcome_v6(B, "Me", "macos", "1.5.0", CAPS, True))
            time.sleep(0.3)

        server = self.serve(script)
        side = Side(None)
        side.peer()["port"] = server.port
        side.peer()["id"] = ""
        self.link(side)
        self.assertTrue(wait_for(lambda: side.states))
        self.assertIn("different Beamer", side.last_text())
        self.assertEqual(side.peer()["id"], "")
        self.assertEqual(side.ups, [])


class LinkLiveTests(unittest.TestCase):
    def setUp(self):
        self.machine = machine_with_peer()
        self.addCleanup(self.machine.stop)
        self.side = Side(self.machine)
        self.link = self.side.link()
        self.addCleanup(self.link.stop)
        self.link.start()
        self.assertTrue(wait_for(lambda: self.side.ups), self.side.states)

    def take(self, route=1):
        self.assertTrue(self.link.post(protocol.focus_v6(route, HERE, resistance_px=120, reach=[])))
        self.assertTrue(wait_for(lambda: any(m["type"] == protocol.MSG_ACCEPT for m in self.side.messages)), self.side.messages)

    def test_send_input_numbers_from_one_and_the_responder_injects_for_its_owner(self):
        self.take()
        self.assertTrue(self.link.send_input(protocol.key_msg("keydown", "a")))
        self.assertTrue(self.link.send_input(protocol.mousemove_msg(3, 4)))
        self.assertTrue(wait_for(lambda: ("move", 3, 4) in self.machine.injected()))
        self.assertIn(("key", "a", True), self.machine.injected())
        self.assertEqual(self.link.last_sent_seq, 2)

    def test_acks_give_round_trip_samples_that_are_never_negative(self):
        self.take()
        for _ in range(5):
            self.link.send_input(protocol.mousemove_msg(1, 1))
            time.sleep(0.03)
        self.assertTrue(wait_for(lambda: self.link.round_trips()), "no round trip measured")
        self.assertTrue(all(sample >= 0 for sample in self.link.round_trips()))
        self.assertIsNotNone(self.link.last_ack_at)

    def test_inbound_messages_other_than_acks_reach_the_callback(self):
        self.link.post(protocol.focus_v6(1, HERE, resistance_px=120, reach=[]))
        self.assertTrue(wait_for(lambda: self.side.messages))
        self.assertEqual(self.side.messages[0]["type"], protocol.MSG_ACCEPT)
        self.assertNotIn(protocol.MSG_ACK, [m["type"] for m in self.side.messages])

    def test_a_clipboard_is_dropped_when_the_peer_lacks_the_capability(self):
        self.link._caps = frozenset()
        self.assertTrue(self.link.post(protocol.clipboard_msg("hello")))
        time.sleep(0.2)
        self.assertEqual(self.machine.clipboard.set_calls, [])

    def test_settings_are_not_sent_to_a_peer_without_the_capability(self):
        self.link._caps = frozenset()
        self.assertTrue(self.link.post(protocol.settings_msg({"a": 1})))
        time.sleep(0.2)
        self.assertEqual(self.machine.settings_seen, [])

    def test_the_link_reconnects_after_the_responder_closes_it(self):
        first = len(self.side.ups)
        self.machine.responder.peers_changed()
        self.side.peer()["linked"] = True
        for link in list(self.machine.responder._links.values()):
            link.close()
        self.assertTrue(wait_for(lambda: len(self.side.ups) > first, 5.0), self.side.states)

    def test_stop_ends_the_link_and_the_responder_sees_it_go(self):
        self.link.stop()
        self.assertTrue(wait_for(lambda: (protocol_id(B), False) in self.machine.links))
        self.assertFalse(self.link.live())

    def test_a_full_queue_reports_failure(self):
        self.link._stall.set()
        results = [self.link.send_input(protocol.mousemove_msg(1, 1)) for _ in range(2100)]
        self.link._stall.clear()
        self.assertIn(False, results)
        self.assertEqual(results.count(True), 2048)


def protocol_id(ident):
    return ident


class LinkLivenessTests(unittest.TestCase):
    def test_a_silent_peer_ends_the_link_after_two_seconds(self):
        def script(connection):
            session, hello = scripted_session(connection, TOKENS[B])
            protocol.send_msg(connection, session, protocol.welcome_v6(HERE, "Here", "windows", "1.5.0", CAPS, True))
            time.sleep(6)

        server = Scripted(script)
        self.addCleanup(server.stop)
        side = Side(None)
        side.peer()["port"] = server.port
        link = side.link(reconnect_seconds=30)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(lambda: side.ups))
        began = time.monotonic()
        self.assertTrue(wait_for(lambda: "stopped responding" in (side.last_text() or ""), 5.0), side.states)
        self.assertGreaterEqual(time.monotonic() - began, 1.5)
        self.assertFalse(link.live())

    def test_an_idle_link_pings_each_second(self):
        pings = []

        def script(connection):
            session, hello = scripted_session(connection, TOKENS[B])
            protocol.send_msg(connection, session, protocol.welcome_v6(HERE, "Here", "windows", "1.5.0", CAPS, True))
            connection.settimeout(3)
            started = time.monotonic()
            while time.monotonic() - started < 2.6:
                try:
                    message = protocol.recv_msg(connection, session)
                except OSError:
                    return
                if message["type"] == protocol.MSG_PING:
                    pings.append(message)
                protocol.send_msg(connection, session, protocol.ack_v6(0, 0))

        server = Scripted(script)
        self.addCleanup(server.stop)
        side = Side(None)
        side.peer()["port"] = server.port
        link = side.link()
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(lambda: len(pings) >= 2, 4.0), pings)

    def test_an_ack_ahead_of_anything_sent_ends_the_link(self):
        def script(connection):
            session, hello = scripted_session(connection, TOKENS[B])
            protocol.send_msg(connection, session, protocol.welcome_v6(HERE, "Here", "windows", "1.5.0", CAPS, True))
            protocol.send_msg(connection, session, protocol.ack_v6(5, 0))
            time.sleep(1)

        server = Scripted(script)
        self.addCleanup(server.stop)
        side = Side(None)
        side.peer()["port"] = server.port
        link = side.link(reconnect_seconds=30)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(lambda: side.ups))
        self.assertTrue(wait_for(lambda: not link.live(), 3.0))

    def test_an_ack_with_a_negative_held_us_ends_the_link(self):
        def script(connection):
            session, hello = scripted_session(connection, TOKENS[B])
            protocol.send_msg(connection, session, protocol.welcome_v6(HERE, "Here", "windows", "1.5.0", CAPS, True))
            protocol.send_msg(connection, session, {"type": "ack", "data": {"seq": 0, "held_us": -1}})
            time.sleep(1)

        server = Scripted(script)
        self.addCleanup(server.stop)
        side = Side(None)
        side.peer()["port"] = server.port
        link = side.link(reconnect_seconds=30)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(lambda: side.ups))
        self.assertTrue(wait_for(lambda: not link.live(), 3.0))

    def test_an_ack_that_says_locked_sets_peer_locked_until_one_that_does_not(self):
        cleared = threading.Event()

        def script(connection):
            session, hello = scripted_session(connection, TOKENS[B])
            protocol.send_msg(connection, session, protocol.welcome_v6(HERE, "Here", "windows", "1.5.0", CAPS, True))
            protocol.send_msg(connection, session, protocol.ack_v6(0, 0, locked=True))
            cleared.wait(5)
            protocol.send_msg(connection, session, protocol.ack_v6(0, 0))
            time.sleep(2)

        server = Scripted(script)
        self.addCleanup(server.stop)
        side = Side(None)
        side.peer()["port"] = server.port
        link = side.link(reconnect_seconds=30)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(lambda: side.ups))
        self.assertTrue(wait_for(lambda: link.peer_locked), side.states)
        cleared.set()
        self.assertTrue(wait_for(lambda: not link.peer_locked), side.states)
        self.assertTrue(link.live())


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class LinkFollowTests(unittest.TestCase):
    def link(self, clock, tried):
        side = Side(None)
        side.peer()["host"] = "192.168.77.7"

        def factory(address, timeout=None):
            tried.append(address)
            raise OSError("no route")

        link = side.link(socket_factory=factory, clock=clock, reconnect_seconds=0.02)
        self.addCleanup(link.stop)
        return side, link

    def test_a_beacon_with_the_exact_name_is_tried_once_the_link_has_been_down_ten_seconds(self):
        clock, tried = FakeClock(), []
        side, link = self.link(clock, tried)
        link.start()
        self.assertTrue(wait_for(lambda: tried))
        tried.clear()
        beacon = [{"name": "Far", "address": "192.168.77.9"}, {"name": "Other", "address": "192.168.77.10"}]
        link.follow(beacon)
        time.sleep(0.2)
        self.assertNotIn(("192.168.77.9", 24820), tried)
        clock.now += 11
        link.follow(beacon)
        self.assertTrue(wait_for(lambda: ("192.168.77.9", 24820) in tried), tried)
        self.assertNotIn(("192.168.77.10", 24820), tried)
        tried.clear()
        clock.now += 5
        link.follow(beacon)
        time.sleep(0.2)
        self.assertNotIn(("192.168.77.9", 24820), tried)
        clock.now += 30
        link.follow(beacon)
        self.assertTrue(wait_for(lambda: ("192.168.77.9", 24820) in tried), tried)

    def test_a_failed_try_shows_nothing_and_saves_nothing(self):
        clock, tried = FakeClock(), []
        side, link = self.link(clock, tried)
        link.start()
        self.assertTrue(wait_for(lambda: tried))
        clock.now += 11
        link.follow([{"name": "Far", "address": "192.168.77.9"}])
        self.assertTrue(wait_for(lambda: ("192.168.77.9", 24820) in tried))
        self.assertEqual(side.peer()["host"], "192.168.77.7")

    def test_the_address_is_saved_once_the_link_authenticates_there(self):
        machine = machine_with_peer()
        self.addCleanup(machine.stop)
        clock = FakeClock()
        side = Side(machine)
        side.peer()["host"] = "10.255.255.1"

        real = socket.create_connection

        def factory(address, timeout=None):
            if address[0] == "10.255.255.1":
                raise OSError("no route")
            return real(address, timeout=timeout)

        link = side.link(socket_factory=factory, clock=clock)
        self.addCleanup(link.stop)
        link.start()
        time.sleep(0.2)
        clock.now += 11
        link.follow([{"name": "Far", "address": LOOPBACK}])
        self.assertTrue(wait_for(lambda: side.ups), side.states)
        self.assertEqual(side.peer()["host"], LOOPBACK)


class LinkTunnelTests(unittest.TestCase):
    def test_an_unreachable_host_falls_back_to_the_tunnel(self):
        import errno
        machine = machine_with_peer()
        self.addCleanup(machine.stop)
        side = Side(machine)

        def factory(address, timeout=None):
            raise OSError(errno.EHOSTUNREACH, "no route")

        link = side.link(socket_factory=factory, tunnel=lambda: socket.create_connection((LOOPBACK, machine.port), timeout=1))
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(lambda: side.ups), side.states)
        self.assertTrue(link.via_tunnel)


class LinkFoldAndHeldTests(unittest.TestCase):
    def test_a_migrated_entry_whose_peer_is_already_paired_is_folded_and_the_link_ends(self):
        machine = machine_with_peer()
        self.addCleanup(machine.stop)
        side = Side(machine, linked=False, from_1_4=True)
        side.peer()["id"] = ""
        side.peer()["platform"] = "windows"
        other = entry(HERE, "Far again", token=other_token(), host="192.168.77.9", port=24820, platform="windows")
        side.settings.data["peers"].append(other)
        link = side.link(reconnect_seconds=30)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(lambda: side.notices), side.states)
        self.assertIn("which is now part of", side.notices[0])
        self.assertEqual([peer["name"] for peer in side.settings.data["peers"]], ["Far again"])
        self.assertEqual(side.ups, [])

    def test_held_us_is_taken_off_the_sample_and_never_goes_below_zero(self):
        def script(connection):
            session, hello = scripted_session(connection, TOKENS[B])
            protocol.send_msg(connection, session, protocol.welcome_v6(HERE, "Here", "windows", "1.5.0", CAPS, True))
            message = protocol.recv_msg(connection, session)
            while message["type"] == protocol.MSG_PING:
                message = protocol.recv_msg(connection, session)
            time.sleep(0.05)
            protocol.send_msg(connection, session, protocol.ack_v6(message["data"]["seq"], 5_000_000))
            time.sleep(0.5)

        server = Scripted(script)
        self.addCleanup(server.stop)
        side = Side(None)
        side.peer()["port"] = server.port
        link = side.link()
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(lambda: side.ups))
        link.send_input(protocol.mousemove_msg(1, 1))
        self.assertTrue(wait_for(lambda: link.round_trips()))
        self.assertEqual(link.round_trips(), [0.0])


class LinkReviewFindingsTests(unittest.TestCase):
    """What the cold review of the first version found, each shown failing before it was fixed."""

    def test_an_ack_that_beats_the_writers_bookkeeping_does_not_end_the_link(self):
        def script(connection):
            session, hello = scripted_session(connection, TOKENS[B])
            protocol.send_msg(connection, session, protocol.welcome_v6(HERE, "Here", "windows", "1.5.0", CAPS, True))
            connection.settimeout(5)
            while True:
                message = protocol.recv_msg(connection, session)
                seq = message["data"].get("seq")
                if seq:
                    protocol.send_msg(connection, session, protocol.ack_v6(seq, 0))

        server = Scripted(script)
        self.addCleanup(server.stop)
        side = Side(None)
        side.peer()["port"] = server.port

        def slow_clock():
            time.sleep(0.003)
            return time.monotonic()

        link = side.link(clock=slow_clock)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(lambda: side.ups))
        for _ in range(30):
            self.assertTrue(link.send_input(protocol.mousemove_msg(1, 1)))
            time.sleep(0.01)
        time.sleep(0.3)
        self.assertTrue(link.live(), side.states)

    def test_stop_during_a_handshake_ends_the_thread_and_start_runs_it_again(self):
        def script(connection):
            time.sleep(6)

        server = Scripted(script)
        self.addCleanup(server.stop)
        side = Side(None)
        side.peer()["port"] = server.port
        link = side.link(reconnect_seconds=0.05)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(lambda: server.connections))
        began = time.monotonic()
        link.stop()
        self.assertLess(time.monotonic() - began, 1.0)
        self.assertFalse(link.running)
        before = server.connections
        link.start()
        self.assertTrue(wait_for(lambda: server.connections > before), "the link did not dial again")

    def test_stop_without_waiting_returns_at_once_reports_nothing_after_and_start_runs_it_again(self):
        # A window removing machines must not wait on a link thread that is in a handshake.
        def script(connection):
            session, hello = scripted_session(connection, TOKENS[B])
            time.sleep(0.5)
            protocol.send_msg(connection, session, protocol.welcome_v6(HERE, "Here", "windows", "1.5.0", CAPS, True))
            time.sleep(1)

        server = Scripted(script)
        self.addCleanup(server.stop)
        side = Side(None)
        side.peer()["port"] = server.port
        link = side.link()
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(lambda: server.connections))
        time.sleep(0.1)
        began = time.monotonic()
        link.stop(wait=False)
        self.assertLess(time.monotonic() - began, 0.05)
        states, ups = list(side.states), list(side.ups)
        time.sleep(1.2)
        self.assertEqual((side.states, side.ups, side.notices), (states, ups, []))
        self.assertFalse(link.running)
        before = server.connections
        link.start()
        self.assertTrue(wait_for(lambda: server.connections > before), "the link did not dial again")

    def test_nothing_is_reported_after_stop_returns(self):
        def script(connection):
            session, hello = scripted_session(connection, TOKENS[B])
            time.sleep(0.8)
            protocol.send_msg(connection, session, protocol.welcome_v6(HERE, "Here", "windows", "1.5.0", CAPS, True))
            time.sleep(1)

        server = Scripted(script)
        self.addCleanup(server.stop)
        side = Side(None)
        side.peer()["port"] = server.port
        link = side.link()
        link.start()
        self.assertTrue(wait_for(lambda: server.connections))
        time.sleep(0.2)
        link.stop()
        states, ups, saved = list(side.states), list(side.ups), side.settings.saves
        time.sleep(1.5)
        self.assertEqual((side.states, side.ups, side.settings.saves), (states, ups, saved))

    def test_a_failed_try_at_a_beacons_address_shows_nothing_and_blocks_nothing(self):
        def script(connection):
            connection.close()

        server = Scripted(script)
        self.addCleanup(server.stop)
        clock = FakeClock()
        side = Side(None)
        side.peer()["host"] = "10.255.255.1"
        side.peer()["port"] = server.port
        real = socket.create_connection

        def factory(address, timeout=None):
            if address[0] == "10.255.255.1":
                raise OSError("no route")
            return real(address, timeout=timeout)

        link = side.link(socket_factory=factory, clock=clock)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(lambda: side.states))
        clock.now += 11
        shown = list(side.states)
        link.follow([{"name": "Far", "address": LOOPBACK}])
        self.assertTrue(wait_for(lambda: server.connections))
        time.sleep(0.3)
        self.assertEqual(side.states[: len(shown)], shown)
        self.assertFalse(any("closed the connection" in text for _, text in side.states[len(shown):]))

    def test_a_try_never_dials_a_peer_with_send_off(self):
        tried = []

        def factory(address, timeout=None):
            tried.append(address)
            raise OSError("no route")

        clock = FakeClock()
        side = Side(None)
        link = side.link(socket_factory=factory, clock=clock, reconnect_seconds=0.02)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(lambda: tried))
        clock.now += 11
        side.peer()["send"] = False
        link._try_host = "192.168.77.9"
        link._wake.set()
        tried.clear()
        time.sleep(0.4)
        self.assertEqual(tried, [])

    def test_a_peer_that_stops_reading_ends_the_link(self):
        def script(connection):
            session, hello = scripted_session(connection, TOKENS[B])
            protocol.send_msg(connection, session, protocol.welcome_v6(HERE, "Here", "windows", "1.5.0", CAPS, True))
            for _ in range(60):
                protocol.send_msg(connection, session, protocol.ack_v6(0, 0))
                time.sleep(0.2)

        server = Scripted(script)
        self.addCleanup(server.stop)
        side = Side(None)
        side.peer()["port"] = server.port
        link = side.link(reconnect_seconds=30)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(lambda: side.ups))
        big = protocol.clipboard_msg("x" * 200_000)
        for _ in range(100):
            if not link.post(big):
                break
        self.assertTrue(wait_for(lambda: not link.live(), 8.0), "a wedged writer kept the link up")

    def test_only_one_ping_waits_in_the_queue(self):
        side = Side(None)
        link = side.link()
        link._live = True
        link._sock = object()
        for _ in range(20):
            link._tick_ping()
        self.assertEqual(sum(1 for m in link._outbox if m["type"] == protocol.MSG_PING), 1)

    def test_send_input_takes_only_input_and_filters_media_keys(self):
        side = Side(None)
        link = side.link()
        link._live = True
        link._sock = object()
        link._caps = frozenset()
        self.assertFalse(link.send_input(protocol.settings_msg({})))
        self.assertTrue(link.send_input(protocol.key_msg("keydown", "volume_up")))
        self.assertEqual(len(link._outbox), 0)
        link._caps = frozenset({"media_keys"})
        self.assertTrue(link.send_input(protocol.key_msg("keydown", "volume_up")))
        self.assertEqual(len(link._outbox), 1)

    def test_flush_waits_for_the_queue_to_be_written(self):
        machine = machine_with_peer()
        self.addCleanup(machine.stop)
        side = Side(machine)
        link = side.link()
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(lambda: side.ups))
        link.post(protocol.focus_v6(1, HERE, resistance_px=120, reach=[]))
        link.flush(2.0)
        self.assertEqual(len(link._outbox), 0)
        self.assertTrue(wait_for(lambda: machine.responder.owner == B))

    def test_peer_locked_is_forgotten_on_a_new_link(self):
        side = Side(None)
        link = side.link()
        link._locked = True
        link._live = True
        self.assertTrue(link.peer_locked)
        link._reset_link_state()
        self.assertFalse(link.peer_locked)


class LinkFramingTests(unittest.TestCase):
    def serve(self, after_welcome):
        def script(connection):
            session, hello = scripted_session(connection, TOKENS[B])
            protocol.send_msg(connection, session, protocol.welcome_v6(HERE, "Here", "windows", "1.5.0", CAPS, True))
            after_welcome(connection, session)

        server = Scripted(script)
        self.addCleanup(server.stop)
        side = Side(None)
        side.peer()["port"] = server.port
        link = side.link(reconnect_seconds=30)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(lambda: side.ups))
        return side, link

    def test_a_frame_that_arrives_a_byte_at_a_time_is_one_message(self):
        def after(connection, session):
            frame = session.seal(protocol.accept_msg(3))
            for index in range(len(frame)):
                connection.sendall(frame[index:index + 1])
                time.sleep(0.002)
            time.sleep(1)

        side, link = self.serve(after)
        self.assertTrue(wait_for(lambda: side.messages), side.states)
        self.assertEqual(side.messages[0]["data"]["route"], 3)

    def test_two_frames_in_one_segment_are_two_messages(self):
        def after(connection, session):
            connection.sendall(session.seal(protocol.accept_msg(3)) + session.seal(protocol.accept_msg(4)))
            time.sleep(1)

        side, link = self.serve(after)
        self.assertTrue(wait_for(lambda: len(side.messages) == 2))

    def big(self, allowed):
        """A frame past SMALL_MESSAGE_BYTES, then a small one, to a link whose `large` hook says
        `allowed`; what the link hands on, once the small one is through."""
        parsed = []
        real_parse = protocol.parse_message
        filler = {"type": protocol.MSG_CLIPBOARD, "data": {"text": "x" * (protocol.SMALL_MESSAGE_BYTES + 10)}}

        def after(connection, session):
            connection.sendall(session.seal(filler) + session.seal(protocol.accept_msg(3)))
            time.sleep(1)

        def script(connection):
            session, hello = scripted_session(connection, TOKENS[B])
            protocol.send_msg(connection, session, protocol.welcome_v6(HERE, "Here", "windows", "1.5.0", CAPS, True))
            after(connection, session)

        server = Scripted(script)
        self.addCleanup(server.stop)
        side = Side(None)
        side.peer()["port"] = server.port
        asked = []
        link = side.link(reconnect_seconds=30, large=lambda link_, began: asked.append((link_, began)) or allowed)
        self.addCleanup(link.stop)
        with mock.patch.object(protocol, "parse_message", side_effect=lambda plain: parsed.append(len(plain)) or real_parse(plain)):
            link.start()
            self.assertTrue(wait_for(lambda: any(m.get("type") == protocol.MSG_ACCEPT for m in side.messages)), side.states)
        self.assertEqual([who for who, _ in asked], [link])
        self.assertIsInstance(asked[0][1], float)
        self.assertTrue(link.live())
        return side, parsed

    def test_a_frame_too_large_for_anything_but_a_clipboard_is_never_parsed_unless_one_is_expected(self):
        side, parsed = self.big(False)
        self.assertEqual([m["type"] for m in side.messages], [protocol.MSG_ACCEPT])
        self.assertTrue(all(size <= protocol.SMALL_MESSAGE_BYTES for size in parsed), parsed)

    def test_an_expected_clipboard_of_any_size_is_handed_on(self):
        side, parsed = self.big(True)
        self.assertEqual([m["type"] for m in side.messages], [protocol.MSG_CLIPBOARD, protocol.MSG_ACCEPT])

    def test_a_frame_claiming_more_than_the_maximum_ends_the_link(self):
        def after(connection, session):
            connection.sendall((protocol.MAX_FRAME_BYTES + 1).to_bytes(4, "big"))
            time.sleep(1)

        side, link = self.serve(after)
        self.assertTrue(wait_for(lambda: not link.live(), 3.0))

    def test_a_replayed_frame_ends_the_link(self):
        def after(connection, session):
            frame = session.seal(protocol.accept_msg(3))
            connection.sendall(frame + frame)
            time.sleep(1)

        side, link = self.serve(after)
        self.assertTrue(wait_for(lambda: not link.live(), 3.0))
        self.assertEqual(len(side.messages), 1)


class GeminiFindingsTests(unittest.TestCase):
    def test_a_link_that_is_down_refuses_what_its_capabilities_would_have_dropped(self):
        side = Side(None)
        link = side.link()
        self.assertFalse(link.post(protocol.clipboard_msg("x")))
        self.assertFalse(link.post(protocol.settings_msg({})))
        self.assertFalse(link.send_input(protocol.gesture_msg("pinch")))

    def test_a_drop_reason_is_forgotten_with_the_link(self):
        side = Side(None)
        link = side.link()
        link._drop_reason = "stale"
        link._reset_link_state()
        self.assertIsNone(link._drop_reason)

    def test_a_peer_that_never_acknowledges_cannot_grow_the_send_record_without_end(self):
        side = Side(None)
        link = side.link()
        link._live = True
        link._sock = object()
        for index in range(link_module.SENT_RECORD_LIMIT + 1):
            link._sent_at.append((index, 0.0))
        with self.assertRaises(protocol.ProtocolError):
            link._check_sent_record()


class RoundTripLogTests(unittest.TestCase):
    """Section 9: every 60 seconds while input is on the link, the count, median, 95th percentile,
    largest and mean held_us of the round trips measured since the last line."""

    def setUp(self):
        self.now = [100.0]
        self.side = Side(None)
        self.link = self.side.link(clock=lambda: self.now[0])
        self.link.peer_name = "Far"
        self.link._logged_at = 100.0

    def acks(self, count, held_us=1000):
        self.link._written_seq = count
        for seq in range(1, count + 1):
            self.link._sent_at.append((seq, 100.0))
        self.now[0] = 100.2
        for seq in range(1, count + 1):
            self.link._ack({"seq": seq, "held_us": held_us * seq, "locked": False})

    def test_the_minute_gives_count_median_95th_percentile_largest_and_mean_held(self):
        self.acks(20)
        with self.assertLogs("Beamer", "INFO") as logged:
            self.now[0] = 160.1
            self.link._tick(160.1)
        text = logged.output[-1]
        self.assertIn("round trip to Far", text)
        self.assertIn("20 samples", text)
        for word in ("median", "95th percentile", "largest", "mean held_us 10500"):
            self.assertIn(word, text)

    def test_a_minute_without_input_logs_nothing(self):
        with self.assertNoLogs("Beamer", "INFO"):
            self.now[0] = 160.1
            self.link._tick(160.1)

    def test_what_was_logged_is_not_counted_again(self):
        self.acks(5)
        self.now[0] = 160.1
        self.link._tick(160.1)
        with self.assertNoLogs("Beamer", "INFO"):
            self.now[0] = 220.2
            self.link._tick(220.2)


class LinkHardwareTests(unittest.TestCase):
    """`hw` in the hello is the address on the interface towards the host this link dials."""

    def hello_of(self, side, **kwargs):
        seen = []

        def script(connection):
            session, hello = scripted_session(connection, TOKENS[B])
            seen.append(hello)
            protocol.send_msg(connection, session, protocol.welcome_v6(HERE, "Here", "windows", "1.5.0", CAPS, True))
            time.sleep(1)

        server = Scripted(script)
        self.addCleanup(server.stop)
        side.peer()["port"] = server.port
        link = side.link(reconnect_seconds=30, **kwargs)
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(lambda: seen), side.states)
        return seen[0]

    def test_the_hook_is_asked_with_the_host_this_link_dials(self):
        asked = []
        side = Side(None)
        hello = self.hello_of(side, hardware=lambda host: asked.append(host) or "aa:bb:cc:dd:ee:01")
        self.assertEqual(asked, [LOOPBACK])
        self.assertEqual(hello["hw"], "aa:bb:cc:dd:ee:01")

    def test_the_hook_wins_over_the_identitys_own_hw(self):
        side = Side(None)
        side.identity = lambda: {"id": B, "name": "Near", "platform": "macos", "app": "1.5.0", "caps": list(CAPS),
                                 "port": 24820, "hw": "11:11:11:11:11:11"}
        hello = self.hello_of(side, hardware=lambda host: "aa:bb:cc:dd:ee:02")
        self.assertEqual(hello["hw"], "aa:bb:cc:dd:ee:02")

    def test_without_a_hook_the_identitys_hw_is_sent_as_before(self):
        side = Side(None)
        side.identity = lambda: {"id": B, "name": "Near", "platform": "macos", "app": "1.5.0", "caps": list(CAPS),
                                 "port": 24820, "hw": "11:11:11:11:11:11"}
        self.assertEqual(self.hello_of(side)["hw"], "11:11:11:11:11:11")

    def test_a_hook_that_raises_sends_no_hw_and_the_link_still_comes_up(self):
        def broken(host):
            raise OSError("no adapter table")

        side = Side(None)
        hello = self.hello_of(side, hardware=broken)
        self.assertIsNone(hello["hw"])
        self.assertTrue(wait_for(lambda: side.ups), side.states)

    def test_a_hook_that_knows_nothing_sends_no_hw(self):
        side = Side(None)
        self.assertIsNone(self.hello_of(side, hardware=lambda host: "")["hw"])


if __name__ == "__main__":
    unittest.main()
