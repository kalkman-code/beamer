"""The responder's races and resource bounds, each found by the cold reviews of step 4a (a fresh
Opus as an attacker, and Gemini 3.1 Pro on the handshake), written before its fix."""

import os
import socket
import threading
import time
import unittest
from unittest import mock

from core import protocol, receiver
from core.tests.responder_harness import B, C, TOKENS, Initiator, Machine, entry, token, wait_for

ACCEPT, REFUSE = protocol.MSG_ACCEPT, protocol.MSG_REFUSE


class Case(unittest.TestCase):
    peers = None
    zones = ()

    def setUp(self):
        self.machine = Machine(self.peers or [entry(B, "Bee"), entry(C, "Sea")], self.zones).start()
        self.addCleanup(self.machine.stop)

    def link(self, peer=B, **kwargs):
        link = Initiator(self.machine, peer)
        self.addCleanup(link.close)
        link.handshake(**kwargs)
        return link

    def own(self, link, **fields):
        route = link.take(**fields)
        self.assertEqual(link.answer(route), (ACCEPT, {"route": route}))
        return route


class TheClipboardAndTheOwnerCallbacks(Case):
    def test_a_let_go_never_sends_the_next_owners_clipboard_and_the_callbacks_keep_their_order(self):
        clipboard = self.machine.clipboard
        b, c = self.link(B), self.link(C)
        self.own(b)
        clipboard.copy("copied here")
        clipboard.slow_get = 0.4
        b.let_go(target=C)
        self.assertTrue(wait_for(lambda: self.machine.responder.owner is None))
        self.own(c)
        c.send(protocol.clipboard_msg("C's secret"))
        self.assertTrue(wait_for(lambda: ("set", "C's secret") in self.machine.events))
        sent = b.expect(protocol.MSG_CLIPBOARD, timeout=1.0)
        self.assertNotEqual(sent, {"text": "C's secret"})
        self.assertTrue(wait_for(lambda: len(self.machine.owners) == 3))
        self.assertEqual(self.machine.owners, [B, None, C])

    def test_a_clipboard_is_never_set_after_its_senders_ownership_ended(self):
        b = self.link(B)
        self.own(b)
        self.machine.clipboard.slow_set = 0.4
        b.send(protocol.clipboard_msg("from b"))
        time.sleep(0.1)
        self.machine.settings.peer(TOKENS[B])["allow_drive"] = False
        self.machine.responder.peers_changed()
        self.assertTrue(wait_for(lambda: ("owner", None) in self.machine.events))
        events = [e for e in self.machine.events if e in (("set", "from b"), ("owner", None))]
        self.assertEqual(events, [("set", "from b"), ("owner", None)])


class TheNotice(Case):
    peers = [entry("", "Old PC", token=token(50), from_1_4=True, linked=False, paired_at=0)]

    def test_the_notice_shows_even_when_the_welcome_cannot_be_sent(self):
        real = protocol.send_msg

        def refuse_welcome(sock, session, message):
            if message.get("type") == protocol.MSG_WELCOME:
                raise ConnectionResetError("the claimer reset the connection")
            return real(sock, session, message)

        with mock.patch.object(protocol, "send_msg", refuse_welcome):
            link = Initiator(self.machine, B, key=token(50))
            self.addCleanup(link.close)
            with self.assertRaises((protocol.ConnectionClosed, OSError)):
                link.handshake(link.hello(name="Claimer"))
        self.assertEqual(self.machine.settings.peer(token(50))["id"], protocol.id_text(B))
        self.assertEqual(self.machine.notices, ["Linked with Claimer at 127.0.0.1 for the first time on Beamer 1.5.0."])


class ALargeFrameToASlowReader(Case):
    def test_a_slow_but_working_reader_keeps_the_link(self):
        with mock.patch.object(receiver, "LINK_READ_TIMEOUT_SECONDS", 0.5):
            b = Initiator(self.machine, B)
            self.addCleanup(b.close)
            b.handshake(reader=False)
            b.send(protocol.focus_v6(1, receiver_id(self.machine)))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if protocol.recv_msg(b.sock, b.session).get("type") == ACCEPT:
                    break
            self.machine.clipboard.image = protocol.PNG_SIGNATURE + os.urandom(3_000_000)
            self.machine.clipboard.stamp += 1
            b.send(protocol.focus_v6(2, B))
            received, started = 0, time.monotonic()
            b.sock.settimeout(0.05)
            while received < 4_000_000 and time.monotonic() - started < 10:
                try:
                    chunk = b.sock.recv(65536)
                except socket.timeout:
                    chunk = None
                if chunk == b"":
                    break
                received += len(chunk or b"")
                b.send(protocol.ping_msg())
                time.sleep(0.03)
            self.assertGreaterEqual(received, 4_000_000)
            self.assertIn(B, self.machine.responder.links())


def receiver_id(machine):
    return machine.responder._identity()["id"]


class BusyIsDecidedAfterTheClaimIsVisible(Case):
    def test_the_take_is_visible_to_the_app_before_away_is_read(self):
        seen = []

        def away():
            seen.append(self.machine.responder.driven)
            return False

        self.machine.responder._away = away
        b = self.link(B)
        self.own(b)
        self.assertEqual(seen, [True])
        self.assertTrue(self.machine.responder.driven)

    def test_a_busy_refusal_leaves_nothing_claimed(self):
        self.machine.away = True
        b = self.link(B)
        route = b.take()
        self.assertEqual(b.answer(route), (REFUSE, {"route": route, "why": "busy"}))
        self.assertFalse(self.machine.responder.driven)
        self.assertIsNone(self.machine.responder.owner)


class AnAppCallbackThatRaises(Case):
    def test_a_zones_callback_that_raises_leaves_the_link_working(self):
        def broken():
            raise RuntimeError("the settings are being rewritten")

        self.machine.responder._zones = broken
        b = self.link(B)
        self.own(b)
        self.assertEqual(self.machine.responder.links(), {B})


class Unlocking(Case):
    class Unlock:
        def __init__(self):
            self.locked = False

        def is_locked(self):
            return self.locked

        def ensure_unlocked(self):
            time.sleep(0.8)
            self.locked = False
            return True

    def test_releases_still_reach_the_injector_while_it_unlocks(self):
        unlock = self.Unlock()
        self.machine.responder._unlock = unlock
        b = self.link(B)
        self.own(b)
        b.key("keydown", "shift")
        self.assertTrue(wait_for(lambda: ("key", "shift", True) in self.machine.injected()))
        unlock.locked = True
        self.own(b, edge="left", offset=0.5)
        self.assertTrue(wait_for(lambda: self.machine.responder._unlocking.is_set()))
        b.key("keyup", "shift")
        b.key("keydown", "x")
        self.assertTrue(wait_for(lambda: ("key", "shift", False) in self.machine.injected(), timeout=0.5))
        self.assertNotIn(("key", "x", True), self.machine.injected())
        self.assertEqual(self.machine.responder.owner, B)

    def test_only_the_owner_hears_that_the_machine_is_locked(self):
        unlock = self.Unlock()
        unlock.locked = True
        self.machine.responder._unlock = unlock
        b, c = self.link(B), self.link(C)
        self.own(b, edge="left", offset=0.5)
        self.assertTrue(wait_for(lambda: self.machine.responder._unlocking.is_set()))
        self.assertTrue(b.expect(protocol.MSG_ACK, timeout=1.0).get("locked"))
        ack = c.expect(protocol.MSG_ACK, timeout=1.0)
        self.assertNotIn("locked", ack)


class TheLinkReplaced(Case):
    def test_a_take_on_a_replaced_link_is_never_accepted(self):
        first = self.link(B)
        old = self.machine.responder._links[B]
        real_close = receiver._Link.close

        def slow_close(link):
            time.sleep(0.4)
            real_close(link)

        with mock.patch.object(receiver._Link, "close", slow_close):
            second = Initiator(self.machine, B)
            self.addCleanup(second.close)
            done = threading.Thread(target=second.handshake)
            done.start()
            self.assertTrue(wait_for(lambda: self.machine.responder._links.get(B) not in (None, old)))
            route = first.take()
            self.assertIsNone(first.answer(route, timeout=1.0))
            done.join(3)
        self.assertNotIn(B, self.machine.owners)


class ZonesArmedFromFreshSettings(Case):
    zones = [{"peer": protocol.id_text(C), "kind": "edge", "off": True}]

    def setUp(self):
        super().setUp()
        for item in self.machine.settings.data["peers"]:
            item["side"] = "right" if item["token"] == TOKENS[C] else "left"

    def test_a_zone_change_during_a_take_is_not_lost(self):
        stale = [dict(zone) for zone in self.machine.settings.data["zones"]]
        calls = []

        def zones():
            calls.append(1)
            if len(calls) == 1:
                self.machine.settings.data["zones"][0]["off"] = False
                changer = threading.Thread(target=self.machine.responder.rearm)
                changer.start()
                changer.join(2)
                return stale
            return self.machine.settings.data["zones"]

        b = self.link(B)
        self.machine.responder._zones = zones
        route = self.own(b, reach=[C])
        self.machine.desktop.cursor = (1919, 500)
        b.move(200, 0)
        switch = b.expect(protocol.MSG_SWITCH, timeout=1.0)
        self.assertIsNotNone(switch)
        self.assertEqual((switch["route"], switch["next"]), (route, protocol.id_text(C)))


if __name__ == "__main__":
    unittest.main()
