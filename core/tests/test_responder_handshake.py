"""The responder's half of the version 6 handshake (WIRE.md section 2, the handshake
and older peers), over real sockets on loopback against LinkResponder."""

import hashlib
import hmac
import json
import os
import select
import socket
import struct
import time
import unittest
from unittest import mock

from core import protocol, receiver
from core.tests import v5_link
from core.tests.responder_harness import (
    B, C, D, HERE, TOKENS, Initiator, Machine, entry, silent_close, token, wait_for,
)


def raw_preamble(key_id, share=None, prefix=None):
    return protocol.WIRE_MAGIC + bytes([protocol.LINK_VERSION]) + (prefix or os.urandom(8)) + key_id + (share or os.urandom(32))


class Case(unittest.TestCase):
    peers = None
    zones = ()

    def setUp(self):
        self.machine = Machine(self.peers or [entry(B, "Bee"), entry(C, "Sea")], self.zones).start()
        self.addCleanup(self.machine.stop)
        self.opened = []

    def initiator(self, *args, **kwargs):
        link = Initiator(self.machine, *args, **kwargs)
        self.opened.append(link)
        self.addCleanup(link.close)
        return link

    def raw(self):
        sock = socket.create_connection(("127.0.0.1", self.machine.port), timeout=5)
        self.addCleanup(sock.close)
        return sock


class TheWelcomesHardwareAddress(unittest.TestCase):
    """`hw` in the welcome is this machine's address on the interface the link came in by."""

    def welcome_of(self, **machine):
        machine = Machine([entry(B, "Bee")], **machine).start()
        self.addCleanup(machine.stop)
        link = Initiator(machine)
        self.addCleanup(link.close)
        return link.handshake()

    def test_the_hook_is_asked_with_the_address_the_link_came_from_and_its_answer_is_sent(self):
        asked = []
        welcome = self.welcome_of(hardware=lambda host: asked.append(host) or "aa:bb:cc:dd:ee:01", hw="11:11:11:11:11:11")
        self.assertEqual(asked, ["127.0.0.1"])
        self.assertEqual(welcome["hw"], "aa:bb:cc:dd:ee:01")

    def test_without_a_hook_the_identitys_hw_is_sent_as_before(self):
        self.assertEqual(self.welcome_of(hw="11:11:11:11:11:11")["hw"], "11:11:11:11:11:11")

    def test_a_hook_that_raises_sends_no_hw_and_the_link_still_comes_up(self):
        def broken(host):
            raise OSError("no adapter table")

        self.assertIsNone(self.welcome_of(hardware=broken, hw="11:11:11:11:11:11")["hw"])

    def test_a_hook_that_knows_nothing_sends_no_hw(self):
        self.assertIsNone(self.welcome_of(hardware=lambda host: "")["hw"])


class TheLookup(Case):
    def test_an_unknown_key_id_is_closed_with_nothing_sent(self):
        link = self.initiator(key=token(200))
        self.assertTrue(silent_close(link.sock))

    def test_a_known_key_id_is_welcomed(self):
        welcome = self.initiator().handshake()
        self.assertEqual(welcome["id"], HERE)
        self.assertEqual(welcome["name"], "Here")
        self.assertTrue(welcome["accepts"])
        self.assertIn("text", welcome["caps"])
        self.assertTrue(wait_for(lambda: self.machine.responder.links() == {B}))

    def test_welcome_accepts_is_false_without_allow_drive(self):
        self.machine.settings.peer(TOKENS[B])["allow_drive"] = False
        self.assertFalse(self.initiator().handshake()["accepts"])

    def test_in_use_off_refuses_the_handshake_and_on_accepts_it(self):
        self.machine.settings.peer(TOKENS[B])["in_use"] = False
        link = self.initiator()
        with self.assertRaises(protocol.HelloRefused) as caught:
            link.handshake()
        self.assertEqual(caught.exception.error, protocol.ERROR_INVALID_HELLO)
        self.assertTrue(wait_for(lambda: self.machine.responder.links() == set()))

        self.machine.settings.peer(TOKENS[B])["in_use"] = True
        self.assertEqual(self.initiator().handshake()["id"], HERE)

    def test_a_typed_token_is_never_matched_or_given_a_key_id(self):
        typed = "hunter2"
        self.machine.settings.data["peers"].append(entry("", "Typed", token=typed, from_1_4=True, linked=False))
        typed_key_id = hmac.new(
            hmac.new(protocol.LINK_SALT, typed.encode(), hashlib.sha256).digest(), protocol.KEY_ID_INFO + b"\x01", hashlib.sha256
        ).digest()[:16]
        with mock.patch.object(protocol, "key_id", wraps=protocol.key_id) as computed:
            sock = self.raw()
            sock.sendall(raw_preamble(typed_key_id))
            self.assertTrue(silent_close(sock))
        self.assertNotIn(typed, [call.args[0] for call in computed.call_args_list])

    def test_low_order_shares_are_closed_with_nothing_sent_not_even_a_preamble(self):
        for share in (bytes(32), b"\x01" + bytes(31), (2**255 - 19).to_bytes(32, "little")):
            sock = self.raw()
            sock.sendall(raw_preamble(protocol.key_id(TOKENS[B]), share))
            self.assertTrue(silent_close(sock), share.hex())

    def test_the_hello_updates_the_entry_and_is_saved(self):
        link = self.initiator()
        link.handshake(link.hello(name="Bee Two", platform="linux", hw="aa:bb:cc:dd:ee:ff", port=24830))
        saved = self.machine.settings.peer(TOKENS[B])
        self.assertEqual((saved["name"], saved["platform"], saved["hw"], saved["port"]), ("Bee Two", "linux", "aa:bb:cc:dd:ee:ff", 24830))
        # Loopback never changes a saved host.
        self.assertEqual(saved["host"], "192.168.77.9")

    def test_an_absent_hw_keeps_the_saved_one(self):
        self.machine.settings.peer(TOKENS[B])["hw"] = "11:22:33:44:55:66"
        self.initiator().handshake()
        self.assertEqual(self.machine.settings.peer(TOKENS[B])["hw"], "11:22:33:44:55:66")

    def test_which_addresses_teach_a_host(self):
        self.assertTrue(receiver.learnable_host("192.168.77.5", 24820))
        self.assertTrue(receiver.learnable_host("192.168.1.20", 1))
        self.assertFalse(receiver.learnable_host("192.168.77.5", 0))
        self.assertFalse(receiver.learnable_host("127.0.0.1", 24820))
        self.assertFalse(receiver.learnable_host("127.4.5.6", 24820))
        self.assertFalse(receiver.learnable_host("169.254.10.1", 24820))
        self.assertFalse(receiver.learnable_host("not an address", 24820))

    def test_a_first_link_marks_the_entry_linked(self):
        self.machine.settings.peer(TOKENS[B])["linked"] = False
        self.initiator().handshake()
        self.assertTrue(self.machine.settings.peer(TOKENS[B])["linked"])

    def test_a_peer_removed_during_its_handshake_is_closed(self):
        admit = self.machine.book.admit

        def admit_then_remove(*args):
            outcome = admit(*args)
            self.machine.settings.data["peers"] = [p for p in self.machine.settings.data["peers"] if p["token"] != TOKENS[B]]
            return outcome

        self.machine.book.admit = admit_then_remove
        link = self.initiator()
        link.handshake()
        self.assertTrue(link.closed.wait(2))
        self.assertTrue(wait_for(lambda: self.machine.responder.links() == set()))

    def test_an_unknown_platform_of_the_allowed_shape_is_accepted(self):
        link = self.initiator()
        link.handshake(link.hello(platform="freebsd_14"))
        self.assertEqual(self.machine.settings.peer(TOKENS[B])["platform"], "freebsd_14")


class TheMigratedEntry(Case):
    peers = [entry("", "Old PC", token=token(50), from_1_4=True, linked=False, paired_at=0), entry(C, "Sea")]
    zones = [{"peer": "", "kind": "edge"}, {"peer": protocol.id_text(C), "kind": "edge"}]

    def migrated(self):
        return self.machine.settings.peer(token(50))

    def test_its_token_is_never_matched_however_it_is_spelled(self):
        link = self.initiator(peer=B, key=token(50))
        self.assertTrue(silent_close(link.sock))
        self.assertEqual(self.migrated()["id"], "")
        self.assertFalse(self.migrated()["linked"])

    def test_nor_once_an_earlier_beta_linked_it(self):
        # Betas 1 and 2 let a migrated token link and learn its peer's id.
        self.migrated().update(id=protocol.id_text(B), linked=True)
        link = self.initiator(peer=B, key=token(50))
        self.assertTrue(silent_close(link.sock))


class TheIdsInHello(Case):
    def test_this_machines_own_id_is_answered_wrong_id(self):
        with self.assertRaises(protocol.HelloRefused) as refused:
            self.initiator(peer=HERE, key=TOKENS[B]).handshake()
        self.assertEqual(refused.exception.error, protocol.ERROR_WRONG_ID)

    def test_another_id_than_the_entrys_is_answered_wrong_id(self):
        with self.assertRaises(protocol.HelloRefused) as refused:
            self.initiator(peer=D, key=TOKENS[B]).handshake()
        self.assertEqual(refused.exception.error, protocol.ERROR_WRONG_ID)
        self.assertEqual(self.machine.settings.peer(TOKENS[B])["id"], protocol.id_text(B))

    def test_the_answer_is_followed_by_a_close(self):
        link = self.initiator(peer=D, key=TOKENS[B])
        with self.assertRaises(protocol.HelloRefused):
            link.handshake()
        self.assertTrue(silent_close(link.sock))


def _hello_text(**changes):
    data = {"version": 6, "id": protocol.id_text(B), "name": "Peer", "platform": "macos", "app": "1.5.0", "caps": [], "port": 24820}
    data.update(changes)
    return {"type": "hello", "data": data}


class TheFirstFrame(Case):
    def refused_with_invalid_hello(self, raw):
        link = self.initiator()
        with self.assertRaises(protocol.HelloRefused) as refused:
            link.handshake(raw=raw)
        self.assertEqual(refused.exception.error, protocol.ERROR_INVALID_HELLO)
        self.assertTrue(silent_close(link.sock))

    def test_not_json_not_an_object_or_another_type(self):
        for raw in (b"not json", b"[1, 2]", b'"hello"', json.dumps({"type": "focus", "data": {"route": 1}}).encode()):
            with self.subTest(raw=raw):
                self.refused_with_invalid_hello(raw)

    def test_hellos_that_are_not_valid(self):
        bad = [
            {"version": 5},
            {"id": None},
            {"id": protocol.id_text(bytes(range(15)))},
            {"id": protocol.id_text(B)[:-1] + chr(ord(protocol.id_text(B)[-1]) + 1)},
            {"id": protocol.id_text(bytes(16))},
            {"name": ""},
            {"name": "x" * 49},
            {"port": 70000},
            {"caps": ["c"] * 17},
            {"platform": "MacOS"},
        ]
        for changes in bad:
            with self.subTest(changes=changes):
                message = _hello_text(**changes)
                if changes.get("id", "") is None:
                    del message["data"]["id"]
                self.refused_with_invalid_hello(json.dumps(message).encode())

    def test_strict_json(self):
        good = json.dumps(_hello_text())
        texts = [
            good.replace('"port": 24820', '"port": 24820.0'),
            good.replace('"port": 24820', '"port": true'),
            good.replace('"caps": []', '"caps": [NaN]'),
            "﻿" + good,
            json.dumps({"type": "hello"}),
            good.replace('"port": 24820', '"port": 24820, "port": 24820'),
        ]
        for text in texts:
            with self.subTest(text=text):
                self.refused_with_invalid_hello(text.encode("utf-8"))

    def test_a_length_out_of_bounds_is_refused_before_it_is_read(self):
        for length in (4097, 20, 0):
            with self.subTest(length=length):
                link = self.initiator()
                link.session.accept_preamble(protocol.recv_link_preamble(link.sock, time.monotonic() + 5))
                link.sock.sendall(struct.pack(">I", length))
                self.assertTrue(silent_close(link.sock))

    def test_a_first_frame_that_fails_its_tag_is_not_answered(self):
        link = self.initiator()
        link.session.accept_preamble(protocol.recv_link_preamble(link.sock, time.monotonic() + 5))
        frame = bytearray(link.session.seal(link.hello()))
        frame[-1] ^= 1
        link.sock.sendall(bytes(frame))
        self.assertTrue(silent_close(link.sock))


class TheDeadlines(Case):
    def test_the_real_figures(self):
        self.assertEqual(receiver.PREAMBLE_SECONDS, 1.0)
        self.assertEqual(receiver.HANDSHAKE_SECONDS, 5.0)

    def closed_after(self, sock):
        started = time.monotonic()
        self.assertTrue(silent_close(sock, timeout=5))
        return time.monotonic() - started

    def test_61_bytes_then_nothing_is_dropped_at_the_preamble_deadline(self):
        with mock.patch.object(receiver, "PREAMBLE_SECONDS", 0.4):
            sock = self.raw()
            sock.sendall(raw_preamble(protocol.key_id(TOKENS[B]))[:61])
            self.assertLess(self.closed_after(sock), 0.9)

    def test_a_trickle_after_the_preamble_is_dropped_at_the_handshake_deadline(self):
        with mock.patch.object(receiver, "HANDSHAKE_SECONDS", 1.0):
            link = self.initiator()
            link.session.accept_preamble(protocol.recv_link_preamble(link.sock, time.monotonic() + 5))
            started = time.monotonic()
            frame = link.session.seal(link.hello())
            for byte in frame:
                readable, _, _ = select.select([link.sock], [], [], 0.1)
                if readable or time.monotonic() - started > 3:
                    break
                try:
                    link.sock.send(bytes([byte]))
                except OSError:
                    break
            self.assertTrue(silent_close(link.sock))
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2.0)
            self.assertGreater(elapsed, 0.8)


class TheSlots(unittest.TestCase):
    def test_a_second_connection_from_an_address_replaces_the_first(self):
        slots = receiver.HandshakeSlots()
        self.assertIsNone(slots.take("192.168.77.2", "one"))
        self.assertEqual(slots.take("192.168.77.2", "two"), "one")
        self.assertEqual(len(slots), 1)

    def test_a_fifth_address_replaces_the_oldest(self):
        slots = receiver.HandshakeSlots()
        for n in range(4):
            self.assertIsNone(slots.take(f"192.168.77.{n}", n))
        self.assertEqual(slots.take("192.168.77.9", 9), 0)
        self.assertEqual(len(slots), 4)

    def test_freeing_is_only_for_the_connection_holding_the_slot(self):
        slots = receiver.HandshakeSlots()
        slots.take("192.168.77.2", "one")
        slots.take("192.168.77.2", "two")
        slots.free("192.168.77.2", "one")
        self.assertEqual(len(slots), 1)
        slots.free("192.168.77.2", "two")
        self.assertEqual(len(slots), 0)


class TheSlotsLive(Case):
    def test_a_second_connection_from_the_same_address_closes_the_first(self):
        first = self.raw()
        first.sendall(raw_preamble(protocol.key_id(TOKENS[B]))[:10])
        self.assertTrue(wait_for(lambda: len(self.machine.responder._slots) == 1))
        second = self.raw()
        self.assertTrue(silent_close(first))
        second.close()

    def test_an_authenticated_link_never_counts(self):
        link = self.initiator()
        link.handshake()
        for _ in range(6):
            self.raw().sendall(b"BE")
        self.assertIsNotNone(link.expect(protocol.MSG_ACK, timeout=1.0))
        self.assertFalse(link.closed.is_set())
        self.assertEqual(self.machine.responder.links(), {B})


class TwoOfOneName(Case):
    peers = [entry(B, "Studio"), entry(C, "Studio")]

    def test_the_status_tells_two_machines_of_one_name_apart(self):
        first = self.initiator(B)
        first.handshake(first.hello(name="Studio"))
        second = self.initiator(C)
        second.handshake(second.hello(name="Studio"))
        self.assertTrue(wait_for(lambda: self.machine.responder.links() == {B, C}))
        expected = "Connected to " + ", ".join(sorted(f"Studio ({protocol.id_text(peer)[-4:]})" for peer in (B, C)))
        self.assertTrue(wait_for(lambda: any(detail == expected for _, detail in self.machine.statuses)), self.machine.statuses)


class OlderPeers(Case):
    def test_a_version_5_initiator_gets_the_short_reply_and_is_named(self):
        self.machine.settings.peer(TOKENS[B])["host"] = "127.0.0.1"
        sock = self.raw()
        session = v5_link.SecureSession("old-token")
        sock.sendall(session.preamble())
        with self.assertRaises(protocol.VersionMismatch) as mismatch:
            v5_link.recv_preamble(sock, session, deadline=time.monotonic() + 3)
        self.assertEqual(mismatch.exception.peer_version, 6)
        self.assertTrue(wait_for(lambda: any("Bee" in detail for _, detail in self.machine.statuses)))

    def test_the_reply_survives_bytes_the_peer_sent_after_its_preamble(self):
        sock = self.raw()
        sock.sendall(v5_link.SecureSession("old-token").preamble() + os.urandom(3000))
        time.sleep(0.2)
        reply = protocol._recv_exact(sock, protocol.SHORT_REPLY_SIZE, time.monotonic() + 3)
        self.assertEqual(reply[:6], b"BEAMY\x06")

    def test_a_beamer_from_before_version_4_gets_the_legacy_welcome(self):
        sock = self.raw()
        sock.sendall(protocol.legacy_frame({"type": "hello", "data": {"version": 3}}))
        (length,) = struct.unpack(">I", protocol._recv_exact(sock, 4, time.monotonic() + 3))
        body = json.loads(protocol._recv_exact(sock, length, time.monotonic() + 3))
        self.assertEqual(body, {"type": "welcome", "data": {"version": 6, "error": "version_mismatch"}})

    def test_a_newer_version_gets_the_short_reply(self):
        sock = self.raw()
        sock.sendall(b"BEAMY\x07" + os.urandom(56))
        reply = protocol._recv_exact(sock, protocol.SHORT_REPLY_SIZE, time.monotonic() + 3)
        self.assertEqual(reply[:6], b"BEAMY\x06")
        self.assertTrue(silent_close(sock))

    def test_the_drain_does_not_hold_a_slot(self):
        sock = self.raw()
        sock.sendall(v5_link.SecureSession("old-token").preamble())
        protocol._recv_exact(sock, protocol.SHORT_REPLY_SIZE, time.monotonic() + 3)
        self.assertEqual(len(self.machine.responder._slots), 0)

    def test_past_the_drain_cap_the_reply_still_survives_what_was_already_sent(self):
        # Gemini's review: with every drain busy the socket was closed over unread bytes, and the
        # reset threw the reply away.
        with mock.patch.object(receiver, "MAX_DRAINS", 0):
            sock = self.raw()
            sock.sendall(v5_link.SecureSession("old-token").preamble() + os.urandom(3000))
            time.sleep(0.2)
            reply = protocol._recv_exact(sock, protocol.SHORT_REPLY_SIZE, time.monotonic() + 3)
            self.assertEqual(reply[:6], b"BEAMY\x06")


class TheThreadCap(Case):
    def test_connections_past_the_cap_are_closed_without_a_thread_or_a_slot(self):
        # Gemini's review: every accepted connection had a thread, however fast they came.
        with mock.patch.object(receiver, "MAX_HANDSHAKE_THREADS", 1):
            first = self.raw()
            first.sendall(b"BEA")
            self.assertTrue(wait_for(lambda: len(self.machine.responder._slots) == 1))
            second = self.raw()
            self.assertTrue(silent_close(second))
            first.sendall(b"MY\x07")
            self.assertEqual(protocol._recv_exact(first, protocol.SHORT_REPLY_SIZE, time.monotonic() + 3)[:6], b"BEAMY\x06")


class ASecondLinkFromOnePeer(Case):
    def test_it_replaces_the_first_and_releases_the_owners_keys_before_serving(self):
        first = self.initiator()
        first.handshake()
        other = self.initiator(peer=C)
        other.handshake()
        route = first.take()
        self.assertEqual(first.answer(route)[0], protocol.MSG_ACCEPT)
        first.key("keydown", "shift")
        self.assertTrue(wait_for(lambda: ("key", "shift", True) in self.machine.injected()))
        second = self.initiator()
        second.handshake()
        self.assertTrue(first.closed.wait(2))
        route = second.take(route=1)
        self.assertEqual(second.answer(route)[0], protocol.MSG_ACCEPT)
        second.key("keydown", "a")
        self.assertTrue(wait_for(lambda: ("key", "a", True) in self.machine.injected()))
        calls = self.machine.injected()
        self.assertLess(calls.index(("key", "shift", False)), calls.index(("key", "a", True)))
        self.assertFalse(other.closed.is_set())
        self.assertEqual(self.machine.responder.links(), {B, C})


if __name__ == "__main__":
    unittest.main()
