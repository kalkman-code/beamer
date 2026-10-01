"""What version 5's ReceiverServer tests pinned and no version 6 test did, ported to LinkResponder
over real sockets on loopback (step 8's port brief, groups A to M). Each class is one group. A
check that something did NOT happen waits on the link's own ordering (an `ack` for the next input
arrives after anything that input queued) rather than on a sleep, wherever the wire allows it."""

import os
import socket
import struct
import threading
import time
import unittest
from unittest import mock

from core import protocol, receiver
from core.receiver import ServerState
from core.tests.responder_harness import (
    B, C, HERE, TOKENS, Initiator, Machine, entry, ident, silent_close, wait_for,
)
from core.tests.test_responder_handshake import SlotPerConnection

ACCEPT, ACK, SWITCH, CLIPBOARD = protocol.MSG_ACCEPT, protocol.MSG_ACK, protocol.MSG_SWITCH, protocol.MSG_CLIPBOARD
PNG = protocol.PNG_SIGNATURE + b"\x00" * 64


class Case(unittest.TestCase):
    zones = ()
    sides = {}
    caps = None

    def setUp(self):
        self.machine = self.start()

    def start(self, unlock=None):
        kwargs = {} if self.caps is None else {"caps": self.caps}
        peers = [entry(B, "Bee", side=self.sides.get(B, "")), entry(C, "Sea", side=self.sides.get(C, ""))]
        machine = Machine(peers, self.zones, unlock=unlock, **kwargs).start()
        self.addCleanup(machine.stop)
        return machine

    def link(self, peer=B, machine=None, **hello):
        # The hello teaches the entry its name, so a link says the one the test reads back.
        hello.setdefault("name", {B: "Bee", C: "Sea"}[peer])
        link = Initiator(machine or self.machine, peer)
        self.addCleanup(link.close)
        link.handshake(link.hello(**hello))
        return link

    def owner(self, peer=B, machine=None, **fields):
        link = self.link(peer, machine)
        route = link.take(**fields)
        self.assertEqual(link.answer(route)[0], ACCEPT)
        return link

    def raw(self):
        sock = socket.create_connection(("127.0.0.1", self.machine.port), timeout=5)
        self.addCleanup(sock.close)
        return sock

    def last_status(self):
        return self.machine.statuses[-1] if self.machine.statuses else None

    def waiting(self):
        return (ServerState.WAITING, f"Waiting on port {self.machine.port}")


def acks(link):
    return [m["data"] for m in link.seen if m["type"] == ACK]


class StatusText(Case):
    """A: what the status line says."""

    def test_after_start_it_waits_on_the_port(self):
        self.assertTrue(wait_for(lambda: self.waiting() in self.machine.statuses), self.machine.statuses)

    def test_a_handshake_reports_connected_by_label(self):
        self.link()
        self.assertTrue(wait_for(lambda: self.last_status() == (ServerState.CONNECTED, "Connected to Bee")), self.machine.statuses)

    def test_a_second_link_from_the_same_peer_never_shows_waiting_between(self):
        first = self.link()
        self.assertTrue(wait_for(lambda: self.last_status() == (ServerState.CONNECTED, "Connected to Bee")))
        mark = len(self.machine.statuses)
        second = self.link()
        self.assertTrue(first.closed.wait(2))
        self.assertTrue(wait_for(lambda: ServerState.CONNECTED in [state for state, _ in self.machine.statuses[mark:]]))
        second.close()
        # The one WAITING after the handover is the second link's own end; a stale first link's
        # exit would have made two.
        self.assertTrue(wait_for(lambda: self.last_status() == self.waiting()))
        self.assertEqual([state for state, _ in self.machine.statuses[mark:]].count(ServerState.WAITING), 1)

    def test_closing_the_link_ends_in_waiting(self):
        link = self.link()
        self.assertTrue(wait_for(lambda: self.last_status() == (ServerState.CONNECTED, "Connected to Bee")))
        link.close()
        self.assertTrue(wait_for(lambda: self.last_status() == self.waiting()), self.machine.statuses)

    def test_silence_ends_in_waiting(self):
        with mock.patch.object(receiver, "LINK_READ_TIMEOUT_SECONDS", 0.3):
            self.link()
            self.assertTrue(wait_for(lambda: (ServerState.CONNECTED, "Connected to Bee") in self.machine.statuses))
            self.assertTrue(wait_for(lambda: self.last_status() == self.waiting(), 3.0), self.machine.statuses)

    def test_a_version_5_preamble_names_the_peer_as_older(self):
        self.machine.settings.peer(TOKENS[B])["host"] = "127.0.0.1"
        self.raw().sendall(b"BEAMY\x05" + os.urandom(8))
        expected = (ServerState.ERROR, "Update Beamer on Bee: it is older than this one")
        self.assertTrue(wait_for(lambda: expected in self.machine.statuses), self.machine.statuses)

    def test_a_version_7_preamble_names_the_peer_as_newer(self):
        self.machine.settings.peer(TOKENS[B])["host"] = "127.0.0.1"
        self.raw().sendall(b"BEAMY\x07" + os.urandom(8))
        expected = (ServerState.ERROR, "Bee runs a newer Beamer: update Beamer on this machine")
        self.assertTrue(wait_for(lambda: expected in self.machine.statuses), self.machine.statuses)

    def test_a_cleartext_frame_from_before_version_4_names_the_peer(self):
        self.machine.settings.peer(TOKENS[B])["host"] = "127.0.0.1"
        self.raw().sendall(protocol.legacy_frame({"type": "hello", "data": {"token": "x", "version": 3}}))
        expected = (ServerState.ERROR, "Update Beamer on Bee: it is older than this one")
        self.assertTrue(wait_for(lambda: expected in self.machine.statuses), self.machine.statuses)

    def test_an_address_no_peer_has_is_named_as_it_is(self):
        self.raw().sendall(b"BEAMY\x05" + os.urandom(8))
        expected = (ServerState.ERROR, "Update Beamer on 127.0.0.1: it is older than this one")
        self.assertTrue(wait_for(lambda: expected in self.machine.statuses), self.machine.statuses)


class NothingThatMovesTheSeq(Case):
    """B: messages that are not input are never acknowledged as input."""

    def test_ping_unknown_let_go_and_clipboard_leave_the_ack_at_seq_zero(self):
        link = self.owner()
        link.send(protocol.ping_msg())
        link.send({"type": "bogus", "data": {}})
        link.send(protocol.clipboard_msg("from the owner"))
        self.assertTrue(wait_for(lambda: self.machine.clipboard.set_calls))
        link.let_go()
        self.assertTrue(wait_for(lambda: self.machine.responder.owner is None))
        for _ in range(2):
            self.assertIsNotNone(link.expect(ACK, timeout=2.0))
        self.assertTrue(all(ack == {"seq": 0, "held_us": 0} for ack in acks(link)), acks(link))
        self.assertEqual(self.machine.injected(), [])
        self.assertFalse(link.closed.is_set())
        self.assertEqual(self.machine.responder.links(), {B})


class TheKeysPlace(Case):
    """C: GitHub issue 3, `us` reaches the injector."""

    def test_the_press_hands_its_place_on_and_a_release_without_one_hands_none(self):
        link = self.owner()
        link.input(protocol.key_msg("keydown", "с", us="c"))
        seq = link.input(protocol.key_msg("keyup", "с"))
        self.assertIsNotNone(link.acked(seq))
        self.assertEqual(self.machine.injector.keys_us, ["c", None])
        self.assertEqual(self.machine.injected(), [("key", "с", True), ("key", "с", False)])

    def test_a_let_go_releases_a_held_key_with_the_presss_place(self):
        link = self.owner()
        seq = link.input(protocol.key_msg("keydown", "с", us="c"))
        self.assertIsNotNone(link.acked(seq))
        link.let_go()
        self.assertTrue(wait_for(lambda: self.machine.responder.owner is None))
        self.assertTrue(wait_for(lambda: len(self.machine.injector.keys_us) == 2))
        self.assertEqual(self.machine.injector.keys_us, ["c", "c"])
        self.assertEqual(self.machine.injected(), [("key", "с", True), ("key", "с", False)])


class TheClipboardOnLettingGo(Case):
    """D: what a let-go owner is sent back, and what it brings."""

    def visit(self, link, text, image=None):
        route = link.take()
        self.assertEqual(link.answer(route)[0], ACCEPT)
        self.assertTrue(wait_for(lambda: self.machine.responder.owner == B))
        self.machine.clipboard.image = image
        self.machine.clipboard.copy(text)
        link.let_go()
        self.assertTrue(wait_for(lambda: self.machine.responder.owner is None))

    def nothing_was_sent(self, link):
        # Frames reach the link in the order they were queued, so the marker is the first clipboard
        # it hears if the visit before it sent none.
        self.visit(link, "marker")
        self.assertEqual(link.expect(CLIPBOARD), {"text": "marker"})

    def test_empty_text_sends_nothing(self):
        link = self.link()
        self.visit(link, "")
        self.nothing_was_sent(link)

    def test_text_over_the_limit_is_not_sent(self):
        link = self.link()
        self.visit(link, "z" * (protocol.CLIPBOARD_MAX_BYTES + 1))
        self.nothing_was_sent(link)

    def test_text_and_a_png_are_both_sent(self):
        link = self.link()
        self.visit(link, "shot.png", PNG)
        self.assertEqual(link.expect(CLIPBOARD), protocol.clipboard_msg("shot.png", PNG)["data"])

    def test_an_oversized_image_is_dropped_and_the_text_kept(self):
        link = self.link()
        self.visit(link, "shot.png", protocol.PNG_SIGNATURE + b"\x00" * protocol.CLIPBOARD_IMAGE_MAX_BYTES)
        self.assertEqual(link.expect(CLIPBOARD), {"text": "shot.png"})

    def test_inbound_text_and_a_png_set_both(self):
        link = self.owner()
        link.send(protocol.clipboard_msg("shot.png", PNG))
        self.assertTrue(wait_for(lambda: self.machine.clipboard.set_calls == [("shot.png", PNG)]), self.machine.clipboard.set_calls)

    def test_inbound_a_malformed_image_keeps_the_text(self):
        link = self.owner()
        link.send({"type": CLIPBOARD, "data": {"text": "bad", "image": "not base64!", "image_format": "png"}})
        link.send(protocol.clipboard_msg("shot.png", PNG))
        self.assertTrue(wait_for(lambda: self.machine.clipboard.set_calls == [("bad", None), ("shot.png", PNG)]), self.machine.clipboard.set_calls)

    def test_inbound_oversized_text_is_ignored_and_a_later_good_one_is_set(self):
        link = self.owner()
        link.send(protocol.clipboard_msg("y" * (protocol.CLIPBOARD_MAX_BYTES + 1)))
        link.send(protocol.clipboard_msg("ok"))
        self.assertTrue(wait_for(lambda: self.machine.clipboard.set_calls == [("ok", None)]), self.machine.clipboard.set_calls)


class Unlock:
    """The Windows unlock provider as the responder uses it. `gate`, when given, holds
    ensure_unlocked until the test releases it."""

    def __init__(self, locked=True, unlocks=True, gate=None):
        self.locked, self.unlocks, self.gate = locked, unlocks, gate
        self.asked = 0

    def is_locked(self):
        return self.locked

    def ensure_unlocked(self):
        self.asked += 1
        if self.gate is not None:
            self.gate.wait(10)
        if self.unlocks:
            self.locked = False
        return self.unlocks


def finish_unlock():
    for thread in threading.enumerate():
        if thread.name == "Beamer-v6-unlock":
            thread.join(3)


class LockScreen(Case):
    """E: Windows' lock screen, with the unlock provider faked."""

    def setUp(self):
        # These wait seconds on a link that says nothing, which the responder's 2.5 s silence limit
        # would otherwise end.
        patch = mock.patch.object(receiver, "LINK_READ_TIMEOUT_SECONDS", 60.0)
        patch.start()
        self.addCleanup(patch.stop)

    def switches(self, link):
        return [m["data"] for m in link.seen if m["type"] == SWITCH]

    def test_a_machine_that_locks_mid_drive_sends_its_owner_home_once(self):
        # Issue 5, 30-09-2026: the owner's pointer was left stuck on the lock screen.
        lock = Unlock(locked=False)
        self.machine = self.start(lock)
        with mock.patch.object(receiver, "FORCED_END_SECONDS", 2.0):
            link = self.owner()
            # Heartbeats are the lock checks' clock: three of them are two checks, and it is unlocked.
            for _ in range(3):
                self.assertIsNotNone(link.expect(ACK, timeout=3.0))
            self.assertEqual(self.switches(link), [])
            lock.locked = True
            switch = link.expect(SWITCH, timeout=5.0)
            self.assertEqual(switch["next"], protocol.id_text(B))
            self.assertNotIn("edge", switch)
            # The owner stays until the forced end's `refuse`, two seconds of heartbeats in which a
            # second send-home could have been made; any would be on the wire before it.
            self.assertIsNotNone(link.expect(protocol.MSG_REFUSE, timeout=6.0))
            self.assertEqual(len(self.switches(link)), 1)
            self.assertEqual(lock.asked, 0)

    def test_a_slow_arrival_on_a_locked_machine_still_gets_its_unlock_and_is_not_sent_home(self):
        # The lock check runs on the ack thread: an arrival that itself took 1.4 s saw a locked machine
        # with its owner driving and sent the owner home before the unlock was asked for.
        lock = Unlock(locked=True)
        self.machine = self.start(lock)
        self.machine.desktop.slow = 1.4
        link = self.link()
        route = link.take(edge="left", offset=0.5, resistance_px=120, reach=[])
        self.assertEqual(link.answer(route)[0], ACCEPT)
        self.assertIsNone(link.expect(SWITCH, timeout=3.0))
        self.assertTrue(wait_for(lambda: lock.asked == 1))
        self.assertEqual(lock.asked, 1)
        self.assertEqual(self.machine.responder.owner, B)
        self.assertEqual(self.machine.arrivals, [("left", 0, 540)])

    def test_an_unlocked_machine_makes_no_unlock_attempt(self):
        lock = Unlock(locked=False)
        self.machine = self.start(lock)
        link = self.owner(edge="left", offset=0.5, resistance_px=120, reach=[])
        # The landing runs on this link's reader thread, so an acknowledged input is after it.
        self.assertIsNotNone(link.acked(link.key("keydown", "a")))
        self.assertEqual(lock.asked, 0)
        self.assertFalse(self.machine.responder._unlocking.is_set())

    def test_the_statuses_run_unlocking_then_connected(self):
        lock = Unlock(locked=True)
        self.machine = self.start(lock)
        self.owner()
        self.assertTrue(wait_for(lambda: (ServerState.CONNECTED, "Unlocking Windows…") in self.machine.statuses))
        finish_unlock()
        self.assertEqual(lock.asked, 1)
        self.assertTrue(wait_for(lambda: self.last_status() == (ServerState.CONNECTED, "Connected to Bee")), self.machine.statuses)

    def test_a_failed_unlock_says_so_rather_than_claiming_a_connection(self):
        lock = Unlock(locked=True, unlocks=False)
        self.machine = self.start(lock)
        self.owner()
        self.assertTrue(wait_for(lambda: self.last_status() == (ServerState.CONNECTED, "Windows is locked — unlock it at the PC")), self.machine.statuses)

    def test_a_link_closed_mid_unlock_does_not_report_a_connection(self):
        # An unlock outlives an owner that disconnects during it; the link's own end has already said
        # WAITING and the unlock finishing must not overwrite it, whether it worked or not.
        for unlocks in (True, False):
            with self.subTest(unlocks=unlocks):
                gate = threading.Event()
                lock = Unlock(locked=True, unlocks=unlocks, gate=gate)
                machine = self.start(lock)
                link = self.owner(machine=machine)
                self.assertTrue(wait_for(lambda: (ServerState.CONNECTED, "Unlocking Windows…") in machine.statuses))
                link.close()
                waiting = (ServerState.WAITING, f"Waiting on port {machine.port}")
                self.assertTrue(wait_for(lambda: machine.statuses[-1] == waiting), machine.statuses)
                mark = len(machine.statuses)
                gate.set()
                finish_unlock()
                self.assertNotIn(ServerState.CONNECTED, [state for state, _ in machine.statuses[mark:]])
                self.assertEqual(machine.statuses[-1], waiting)

    def test_the_ack_says_locked_only_while_the_unlock_runs(self):
        gate = threading.Event()
        self.machine = self.start(Unlock(locked=True, gate=gate))
        link = self.owner()
        self.assertIsNotNone(link.expect(ACK, timeout=3.0, where=lambda data: data.get("locked") is True))
        gate.set()
        self.assertIsNotNone(link.expect(ACK, timeout=3.0, where=lambda data: "locked" not in data))


class TheHandBack(Case):
    """F: the injector's keys are let go of however ownership ends."""

    def test_release_all_follows_a_let_go(self):
        link = self.owner()
        link.let_go()
        self.assertTrue(wait_for(lambda: self.machine.responder.owner is None))
        self.assertIn(("release_all",), self.machine.injector.seen())

    def test_release_all_follows_a_link_that_closes(self):
        link = self.owner()
        link.close()
        self.assertTrue(wait_for(lambda: ("release_all",) in self.machine.injector.seen()))
        self.assertTrue(wait_for(lambda: self.machine.owners == [B, None]))

    def test_stopping_the_responder_while_driven_ends_the_ownership_and_says_stopped(self):
        self.owner()
        self.machine.responder.stop()
        self.assertEqual(self.machine.owners, [B, None])
        self.assertIn(("release_all",), self.machine.injector.seen())
        self.assertEqual(self.last_status()[0], ServerState.STOPPED)

    def test_a_let_go_then_a_close_reports_no_second_end(self):
        link = self.owner()
        link.let_go()
        self.assertTrue(wait_for(lambda: self.machine.responder.owner is None))
        link.close()
        self.assertTrue(wait_for(lambda: (B, False) in self.machine.links))
        self.assertEqual(self.machine.owners, [B, None])


class Pressure(Case):
    """G: the pressure callback the crossing effects are fed from."""

    sides = {B: "left"}
    zones = [{"peer": protocol.id_text(B), "kind": "edge"}]

    def test_a_push_short_of_the_resistance_holds_and_breaking_through_crosses(self):
        link = self.owner(resistance_px=100, reach=[])
        self.machine.desktop.cursor = (0, 540)
        link.move(-60, 0)
        self.assertTrue(wait_for(lambda: len(self.machine.pressures) == 1), self.machine.pressures)
        edge, pressure, crossed, part = self.machine.pressures[0]
        self.assertEqual((edge, crossed, part), ("left", False, None))
        self.assertAlmostEqual(pressure, 0.6)
        self.assertLess(pressure, 1)
        self.assertEqual(self.machine.desktop.placed[-1], (0, 540))
        self.assertEqual(self.machine.responder.owner, B)
        link.move(-60, 0)
        self.assertEqual(link.expect(SWITCH)["next"], protocol.id_text(B))
        self.assertTrue(wait_for(lambda: len(self.machine.pressures) == 2), self.machine.pressures)
        self.assertEqual(self.machine.pressures[1], ("left", 1.0, True, None))
        self.assertNotIn(("move", -60, 0), self.machine.injected())


class RearmNeverRevives(Case):
    """H: a settings change re-arms only zones that are live; and a replayed frame ends the link."""

    sides = {B: "left", C: "right"}
    zones = [{"peer": protocol.id_text(B), "kind": "edge"}, {"peer": protocol.id_text(C), "kind": "edge"}]

    def switches(self, link):
        return [m["data"] for m in link.seen if m["type"] == SWITCH]

    def push(self, link, cursor, dx):
        self.machine.desktop.cursor = cursor
        self.assertIsNotNone(link.acked(link.move(dx, 0)))

    def test_after_a_switch(self):
        link = self.owner(reach=[C])
        self.push(link, (1919, 500), 200)
        self.assertIsNotNone(link.expect(SWITCH))
        self.machine.responder.rearm()
        self.push(link, (1919, 500), 200)
        self.assertEqual(len(self.switches(link)), 1)
        self.assertIn(("move", 200, 0), self.machine.injected())

    def test_after_send_home(self):
        with mock.patch.object(receiver, "FORCED_END_SECONDS", 30.0):
            link = self.owner(reach=[C])
            self.assertTrue(self.machine.responder.send_home())
            self.assertIsNotNone(link.expect(SWITCH))
            self.machine.responder.rearm()
            self.push(link, (0, 500), -200)
            self.push(link, (1919, 500), 200)
            self.assertEqual(len(self.switches(link)), 1)

    def test_with_no_owner(self):
        link = self.owner(reach=[C])
        link.let_go()
        self.assertTrue(wait_for(lambda: self.machine.responder.owner is None))
        self.machine.responder.rearm()
        self.assertEqual(self.machine.responder._armed, [])
        self.assertFalse(self.machine.responder._zones_live)

    def test_a_frame_replayed_whole_ends_the_link(self):
        link = self.owner()
        frame = link.session.seal(protocol.key_msg("keydown", "a", seq=1))
        with link.session.send_lock:
            link.sock.sendall(frame + frame)
        self.assertTrue(link.closed.wait(3))
        self.assertTrue(wait_for(lambda: self.machine.responder.links() == set()))
        # The owner's key went down once, and its ending released it; the replay was not injected twice.
        self.assertEqual(self.machine.injected(), [("key", "a", True), ("key", "a", False)])


class Binding(unittest.TestCase):
    """I: a port something else holds is waited out rather than ending the listener."""

    def hold(self, reuse):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if reuse:
            holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # The responder listens on 0.0.0.0, and SO_REUSEADDR only collides with a holder on the same
        # address, so 127.0.0.1 here would bind happily.
        holder.bind(("0.0.0.0", 0))
        holder.listen(1)
        self.addCleanup(holder.close)
        return holder, holder.getsockname()[1]

    def test_a_held_port_is_an_error_naming_it_then_waiting_once_it_is_free(self):
        holder, port = self.hold(reuse=False)
        machine = Machine([entry(B, "Bee")])
        self.addCleanup(machine.stop)
        with mock.patch.object(receiver, "BIND_RETRY_SECONDS", 0.05):
            machine.responder.start(port)
            self.assertTrue(wait_for(lambda: any(state == ServerState.ERROR for state, _ in machine.statuses), 3.0), machine.statuses)
            detail = next(detail for state, detail in machine.statuses if state == ServerState.ERROR)
            self.assertIn(f"port {port}", detail)
            self.assertIn("Connection page", detail)
            holder.close()
            self.assertTrue(wait_for(lambda: (ServerState.WAITING, f"Waiting on port {port}") in machine.statuses, 3.0), machine.statuses)

    def test_a_listener_that_set_reuseaddr_still_blocks_the_bind(self):
        # A zombie copy of Beamer itself opened its listener with SO_REUSEADDR too.
        holder, port = self.hold(reuse=True)
        machine = Machine([entry(B, "Bee")])
        self.addCleanup(machine.stop)
        with mock.patch.object(receiver, "BIND_RETRY_SECONDS", 0.05):
            machine.responder.start(port)
            self.assertTrue(wait_for(lambda: any(state == ServerState.ERROR for state, _ in machine.statuses), 3.0), machine.statuses)
            self.assertTrue(any(f"port {port}" in detail for state, detail in machine.statuses if state == ServerState.ERROR))


class InputScale(Case):
    """J: this machine's pointer and scroll speed, as the apps set it on the responder."""

    def test_a_slower_pointer_carries_its_fractions_into_the_injector(self):
        self.machine.responder.input_scale = receiver.InputScale(pointer=0.5)
        link = self.owner()
        link.move(1, 0)
        seq = link.move(1, 0)
        self.assertIsNotNone(link.acked(seq))
        self.assertEqual(self.machine.injected(), [("move", 1, 0)])

    def test_reverse_scroll_turns_a_scroll_round(self):
        self.machine.responder.input_scale = receiver.InputScale(reverse=True)
        link = self.owner()
        seq = link.input(protocol.scroll_msg(2, 0))
        self.assertIsNotNone(link.acked(seq))
        self.assertEqual(self.machine.injected(), [("scroll", -2.0, -0.0, "line")])


class StrangersAndTheSlots(Case):
    """K: four connections that say a byte and stall do not keep the real peer out."""

    def test_the_peer_gets_in_and_the_strangers_are_closed_silently(self):
        self.machine.responder._slots = SlotPerConnection()
        strangers = []
        for _ in range(receiver.MAX_PENDING_CONNECTIONS):
            sock = self.raw()
            sock.sendall(b"B")
            strangers.append(sock)
        self.assertTrue(wait_for(lambda: len(self.machine.responder._slots) == receiver.MAX_PENDING_CONNECTIONS))
        self.link()
        self.assertTrue(wait_for(lambda: self.machine.responder.links() == {B}))
        for sock in strangers:
            self.assertTrue(silent_close(sock, timeout=5.0))
        self.assertTrue(wait_for(lambda: len(self.machine.responder._slots) == 0, 5.0), len(self.machine.responder._slots))


class HelloAtItsLargest(unittest.TestCase):
    """L: the largest valid `hello` and `welcome` fit the first frame's limit."""

    def pair(self):
        token = TOKENS[B]
        initiator = protocol.LinkSession(token, protocol.ROLE_INITIATOR)
        responder = protocol.LinkSession(token, protocol.ROLE_RESPONDER)
        responder.accept_preamble(initiator.preamble())
        initiator.accept_preamble(responder.preamble())
        return initiator, responder

    def largest(self):
        return dict(
            machine_id=ident(10), name="😀" * protocol.MAX_NAME_CHARS, platform="p" * 16, app="a" * protocol.MAX_ASCII_FIELD,
            caps=[f"{index:02d}".ljust(protocol.MAX_ASCII_FIELD, "c") for index in range(protocol.MAX_CAPS)],
        )

    def test_a_hello_at_every_maximum_fits_and_reads_back(self):
        initiator, responder = self.pair()
        hello = protocol.hello_v6(port=65535, hw="aa:bb:cc:dd:ee:ff", **self.largest())
        frame = initiator.seal(hello)
        self.assertLessEqual(len(frame) - protocol.HEADER_SIZE, protocol.HELLO_MAX_BYTES)
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        near.sendall(frame)
        read = protocol.read_hello(protocol.recv_first_msg(far, responder, time.monotonic() + 3))
        self.assertEqual((len(read["name"]), len(read["caps"]), read["port"]), (protocol.MAX_NAME_CHARS, protocol.MAX_CAPS, 65535))

    def test_a_welcome_at_every_maximum_fits_and_reads_back(self):
        initiator, responder = self.pair()
        welcome = protocol.welcome_v6(accepts=True, hw="aa:bb:cc:dd:ee:ff", **self.largest())
        frame = responder.seal(welcome)
        self.assertLessEqual(len(frame) - protocol.HEADER_SIZE, protocol.HELLO_MAX_BYTES)
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        near.sendall(frame)
        read = protocol.read_welcome(protocol.recv_first_msg(far, initiator, time.monotonic() + 3))
        self.assertTrue(read["accepts"])


class InjectionErrors(Case):
    """M: an injector that raises says so in the status, once per interval, naming the peer."""

    def fail(self, error=RuntimeError("the desktop refused the key")):
        def inject(*args, **kwargs):
            raise error
        self.machine.injector.inject_key = inject

    def said(self):
        return [s for s in self.machine.statuses if s == (ServerState.CONNECTED, "Input error from Bee; see log")]

    def test_a_failing_injection_sets_the_status_by_label_and_the_link_stays(self):
        self.fail()
        link = self.owner()
        seq = link.key("keydown", "a")
        self.assertIsNotNone(link.acked(seq))
        self.assertTrue(wait_for(lambda: len(self.said()) == 1), self.machine.statuses)
        self.assertFalse(link.closed.is_set())

    def test_it_is_said_once_per_interval(self):
        self.fail()
        link = self.owner()
        with mock.patch.object(receiver, "INJECTION_ERROR_STATUS_INTERVAL_SECONDS", 3600.0):
            for name in "abc":
                self.assertIsNotNone(link.acked(link.key("keydown", name)))
            self.assertTrue(wait_for(lambda: len(self.said()) == 1))
            self.assertEqual(len(self.said()), 1)
        with mock.patch.object(receiver, "INJECTION_ERROR_STATUS_INTERVAL_SECONDS", 0.0):
            self.assertIsNotNone(link.acked(link.key("keydown", "d")))
            self.assertTrue(wait_for(lambda: len(self.said()) == 2), self.machine.statuses)


if __name__ == "__main__":
    unittest.main()
