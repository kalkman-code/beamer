"""Input ownership and the responder's half of routing (WIRE.md sections 4 and
5), over real sockets on loopback against
LinkResponder, with scripted owners."""

import time
import unittest
from unittest import mock

from core import protocol, receiver
from core.return_edge import CornerPush, PartEdge, ReturnEdge, SpanEdge
from core.tests.responder_harness import B, C, D, HERE, TOKENS, Initiator, Machine, entry, wait_for

ACCEPT, REFUSE = protocol.MSG_ACCEPT, protocol.MSG_REFUSE


def ids(*peers):
    return [protocol.id_text(peer) for peer in peers]


class Case(unittest.TestCase):
    zones = ()
    sides = {}

    def setUp(self):
        peers = [entry(peer, name, side=self.sides.get(peer, "")) for peer, name in ((B, "Bee"), (C, "Sea"), (D, "Dee"))]
        self.machine = Machine(peers, self.zones).start()
        self.addCleanup(self.machine.stop)

    def link(self, peer=B, **hello):
        link = Initiator(self.machine, peer)
        self.addCleanup(link.close)
        link.handshake(link.hello(**hello) if hello else None)
        self.assertTrue(wait_for(lambda: peer in self.machine.responder.links()))
        return link

    def own(self, link, **fields):
        route = link.take(**fields)
        self.assertEqual(link.answer(route), (ACCEPT, {"route": route}))
        self.assertTrue(wait_for(lambda: self.machine.responder.owner == link.peer))
        return route

    def refused(self, link, why, **fields):
        route = link.take(**fields)
        self.assertEqual(link.answer(route), (REFUSE, {"route": route, "why": why}))

    def injected_from(self, count=None):
        return self.machine.injected()

    def peer_entry(self, peer):
        return self.machine.settings.peer(TOKENS[peer])


class Taking(Case):
    def test_two_peers_compete_and_the_second_gets_in_after_the_first_lets_go(self):
        b, c = self.link(B), self.link(C)
        self.own(b)
        self.refused(c, "owned")
        b.let_go()
        self.assertTrue(wait_for(lambda: self.machine.responder.owner is None))
        self.own(c)
        self.assertEqual(self.machine.owners, [B, None, C])

    def test_a_route_not_higher_than_the_last_accepted_gets_no_answer(self):
        b = self.link(B)
        self.own(b, route=5)
        for route in (5, 4):
            b.take(route=route)
            self.assertIsNone(b.answer(route, timeout=0.3))

    def test_the_decision_order(self):
        b, c = self.link(B), self.link(C)
        self.peer_entry(C)["allow_drive"] = False
        self.machine.responder.peers_changed()
        self.machine.away = True
        # Malformed before everything.
        self.refused(c, "malformed", resistance_px=5000)
        # Then not allowed before busy.
        self.refused(c, "not_allowed")
        self.peer_entry(C)["allow_drive"] = True
        self.machine.responder.peers_changed()
        self.refused(c, "busy")
        self.machine.away = False
        with mock.patch.object(receiver, "SENT_HOME_SECONDS", 0.6):
            self.own(b)
            self.assertTrue(self.machine.responder.send_home())
            b.let_go()
            self.assertTrue(wait_for(lambda: self.machine.responder.owner is None))
            self.own(c)
            # Sent home before owned.
            self.refused(b, "sent_home")
            time.sleep(0.7)
            self.refused(b, "owned")

    def test_a_stay_from_a_machine_that_is_not_the_owner_is_never_accepted(self):
        b, c = self.link(B), self.link(C)
        self.refused(c, "malformed", stay=True, resistance_px=120, reach=[])
        self.own(b)
        route = c.take(stay=True, resistance_px=120, reach=[])
        self.assertEqual(c.answer(route)[0], REFUSE)
        self.assertEqual(self.machine.responder.owner, B)

    def test_a_driven_desktop_is_busy(self):
        self.machine.away = True
        self.refused(self.link(B), "busy")
        self.assertIsNone(self.machine.responder.owner)

    def test_the_answer_comes_before_the_pointer_is_placed(self):
        self.machine.desktop.slow = 1.0
        b = self.link(B)
        started = time.monotonic()
        route = b.take(edge="left", offset=0.5, resistance_px=120, reach=[])
        self.assertEqual(b.answer(route)[0], ACCEPT)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(wait_for(lambda: self.machine.desktop.placed == [(0, 540)]))
        self.assertEqual(self.machine.arrivals, [("left", 0, 540)])

    def test_a_take_by_the_shortcut_lands_nothing_and_says_where_the_pointer_is(self):
        self.own(self.link(B))
        self.assertTrue(wait_for(lambda: self.machine.arrivals == [(None, 960, 540)]))
        self.assertEqual(self.machine.desktop.placed, [])

    def test_the_owners_own_take_moves_the_pointer_in_place(self):
        b = self.link(B)
        self.own(b)
        self.own(b, edge="top", offset=0.25)
        self.assertTrue(wait_for(lambda: self.machine.desktop.placed == [(480, 0)]))
        self.assertEqual(self.machine.owners, [B])

    def test_a_focus_whose_route_or_target_is_malformed_is_ignored(self):
        b = self.link(B)
        b.send({"type": "focus", "data": {"route": 1.0, "target": protocol.id_text(HERE)}})
        b.send({"type": "focus", "data": {"route": 2, "target": "nope"}})
        self.assertIsNone(b.expect(ACCEPT, timeout=0.3))
        self.assertIsNone(b.expect(REFUSE, timeout=0.1))

    def test_strict_json_on_a_later_focus_is_malformed(self):
        b = self.link(B)
        text = '{"type":"focus","data":{"route":1,"target":"%s","resistance_px":%s}}'
        for value in ("120.0", "true", "NaN"):
            with self.subTest(value=value):
                route = b.route + 1
                b.route = route
                raw = (text % (protocol.id_text(HERE), value)).replace('"route":1', '"route":%d' % route).encode()
                with b.session.send_lock:
                    b.sock.sendall(b.session.seal_raw(raw))
                self.assertEqual(b.answer(route), (REFUSE, {"route": route, "why": "malformed"}))


class NonOwners(Case):
    def test_a_non_owners_input_is_dropped_and_acknowledged(self):
        b, c = self.link(B), self.link(C)
        self.own(b)
        seq = c.key("keydown", "x")
        self.assertIsNotNone(c.acked(seq))
        self.assertEqual(self.machine.injected(), [])

    def test_a_non_owners_clipboard_is_dropped_and_the_owners_is_set(self):
        b, c = self.link(B), self.link(C)
        self.own(b)
        c.send(protocol.clipboard_msg("from c"))
        b.send(protocol.clipboard_msg("from b"))
        self.assertTrue(wait_for(lambda: self.machine.clipboard.set_calls == [("from b", None)]))
        time.sleep(0.1)
        self.assertEqual(self.machine.clipboard.set_calls, [("from b", None)])

    def send_big(self, link, filler, size=protocol.SMALL_MESSAGE_BYTES + 1000):
        """A frame whose plaintext is `size` bytes, sealed for real so its counter advances."""
        body = ('{"type":"%s","data":{},"z":"' % filler).encode() + b"a" * size + b'"}'
        with link.session.send_lock:
            frame = link.session.seal_raw(body)
        link.sock.sendall(frame)

    def test_a_non_owners_large_frame_is_authenticated_and_dropped_without_being_parsed(self):
        b, c = self.link(B), self.link(C)
        self.own(b)
        parsed = []
        real = protocol.parse_message

        def watching(plain):
            parsed.append(len(plain))
            return real(plain)

        with mock.patch.object(protocol, "parse_message", watching):
            self.send_big(c, "x")
            self.send_big(c, "clipboard")
            c.send(protocol.paired_msg([D]))
            self.assertTrue(wait_for(lambda: self.machine.paired == [(C, [D])]), "the link did not survive")
        self.assertTrue(all(size < protocol.SMALL_MESSAGE_BYTES for size in parsed), parsed)

    def test_a_take_refused_as_owned_then_a_clipboard_does_not_end_the_link(self):
        b, c = self.link(B), self.link(C)
        self.own(b)
        self.refused(c, "owned")
        c.send(protocol.clipboard_msg("c" * 200_000))
        seq = c.key("keydown", "x")
        self.assertIsNotNone(c.acked(seq))
        self.assertEqual(self.machine.clipboard.set_calls, [])

    def test_the_owners_large_clipboard_still_arrives(self):
        b = self.link(B)
        self.own(b)
        text = "b" * 200_000
        b.send(protocol.clipboard_msg(text))
        self.assertTrue(wait_for(lambda: self.machine.clipboard.set_calls == [(text, None)]))

    def test_a_non_owners_arrangement_settings_and_paired_are_handled(self):
        self.own(self.link(B))
        c = self.link(C)
        c.send(protocol.arrangement_v6("left", 1790000000, C))
        c.send(protocol.settings_msg({"on": True, "set_at": 1790000000, "by": protocol.id_text(C)}))
        c.send(protocol.paired_msg([D]))
        self.assertTrue(wait_for(lambda: self.machine.paired == [(C, [D])]))
        self.assertEqual(self.machine.arrangements, [(C, {"edge": "left", "set_at": 1790000000, "by": C})])
        self.assertEqual([peer for peer, _ in self.machine.settings_seen], [C])

    def test_settings_from_a_peer_without_the_capability_are_ignored(self):
        d = self.link(D, caps=["clipboard"])
        d.send(protocol.settings_msg({"on": False, "set_at": 1790000000, "by": protocol.id_text(D)}))
        d.send(protocol.paired_msg([B]))
        self.assertTrue(wait_for(lambda: self.machine.paired == [(D, [B])]))
        self.assertEqual(self.machine.settings_seen, [])

    def test_a_non_owners_let_go_is_ignored(self):
        b, c = self.link(B), self.link(C)
        self.own(b)
        c.let_go(target=D)
        time.sleep(0.1)
        self.assertEqual(self.machine.responder.owner, B)
        b.key("keydown", "q")
        self.assertTrue(wait_for(lambda: ("key", "q", True) in self.machine.injected()))

    def test_not_allowed_keeps_the_link_for_arrangement(self):
        self.peer_entry(B)["allow_drive"] = False
        b = self.link(B)
        self.refused(b, "not_allowed")
        b.send(protocol.arrangement_v6("top", 1790000001, B))
        self.assertTrue(wait_for(lambda: len(self.machine.arrangements) == 1))
        self.assertFalse(b.closed.is_set())


class Ending(Case):
    def hold(self, link):
        link.key("keydown", "shift")
        link.input({"type": "mousedown", "data": {"button": "left"}})
        self.assertTrue(wait_for(lambda: ("button", "left", True) in self.machine.injected()))

    def released(self):
        calls = self.machine.injected()
        return ("key", "shift", False) in calls and ("button", "left", False) in calls

    def test_a_let_go_releases_what_the_owner_held(self):
        b = self.link(B)
        self.own(b)
        self.hold(b)
        b.let_go(target=C)
        self.assertTrue(wait_for(self.released))
        self.assertTrue(wait_for(lambda: self.machine.responder.owner is None))
        self.assertEqual(self.machine.owners, [B, None])

    def test_the_link_closing_releases(self):
        b = self.link(B)
        self.own(b)
        self.hold(b)
        b.close()
        self.assertTrue(wait_for(self.released))
        self.assertTrue(wait_for(lambda: self.machine.responder.owner is None and B not in self.machine.responder.links()))

    def test_the_link_timing_out_releases(self):
        with mock.patch.object(receiver, "LINK_READ_TIMEOUT_SECONDS", 0.4):
            b = self.link(B)
            self.own(b)
            self.hold(b)
            self.assertTrue(wait_for(self.released, timeout=2))
            self.assertTrue(b.closed.wait(2))

    def test_removing_the_owner_releases_and_closes_its_links(self):
        b, c = self.link(B), self.link(C)
        self.own(b)
        self.hold(b)
        self.machine.settings.data["peers"] = [p for p in self.machine.settings.data["peers"] if p["token"] != TOKENS[B]]
        self.machine.responder.peers_changed()
        self.assertTrue(wait_for(self.released))
        self.assertTrue(b.closed.wait(2))
        self.assertIsNone(self.machine.responder.owner)
        self.assertFalse(c.closed.is_set())

    def test_turning_allow_drive_off_and_on(self):
        b = self.link(B)
        route = self.own(b)
        self.hold(b)
        self.peer_entry(B)["allow_drive"] = False
        self.machine.responder.peers_changed()
        self.assertEqual(b.expect(REFUSE), {"route": route, "why": "not_allowed"})
        self.assertEqual(b.expect(protocol.MSG_ACCEPTS), {"accepts": False})
        self.assertTrue(self.released())
        before = len(self.machine.injected())
        seq = b.key("keydown", "z")
        self.assertIsNotNone(b.acked(seq))
        self.assertEqual(len(self.machine.injected()), before)
        self.peer_entry(B)["allow_drive"] = True
        self.machine.responder.peers_changed()
        self.assertEqual(b.expect(protocol.MSG_ACCEPTS), {"accepts": True})
        self.own(b)

    def test_the_forced_end_after_a_send_home(self):
        with mock.patch.object(receiver, "FORCED_END_SECONDS", 0.3), mock.patch.object(receiver, "SENT_HOME_SECONDS", 1.0):
            b = self.link(B)
            route = self.own(b)
            self.hold(b)
            self.assertTrue(self.machine.responder.send_home())
            self.assertEqual(b.expect(protocol.MSG_SWITCH), {"route": route, "next": protocol.id_text(B)})
            self.assertEqual(b.expect(REFUSE, timeout=1.0), {"route": route, "why": "sent_home"})
            self.assertTrue(self.released())
            self.assertIsNone(self.machine.responder.owner)
            before = len(self.machine.injected())
            seq = b.key("keydown", "z")
            self.assertIsNotNone(b.acked(seq))
            self.assertEqual(len(self.machine.injected()), before)
            self.refused(b, "sent_home")
            time.sleep(1.0)
            self.own(b)

    def test_an_owner_that_obeys_a_send_home_is_refused_for_a_while_too(self):
        with mock.patch.object(receiver, "SENT_HOME_SECONDS", 0.5):
            b = self.link(B)
            self.own(b)
            self.machine.responder.send_home()
            b.expect(protocol.MSG_SWITCH)
            b.let_go()
            self.assertTrue(wait_for(lambda: self.machine.responder.owner is None))
            self.refused(b, "sent_home")
            time.sleep(0.6)
            self.own(b)

    def test_nothing_to_send_home(self):
        self.assertFalse(self.machine.responder.send_home())

    def test_each_link_keeps_its_own_seq(self):
        b, c = self.link(B), self.link(C)
        self.own(b)
        for name in "abc":
            b.key("keydown", name)
        self.assertIsNotNone(b.acked(3))
        b.let_go()
        self.assertTrue(wait_for(lambda: self.machine.responder.owner is None))
        self.own(c)
        c.key("keydown", "d")
        self.assertIsNotNone(c.acked(1))
        time.sleep(0.5)
        c.expect("nothing", timeout=0.1)
        self.assertEqual(max(m["data"]["seq"] for m in c.seen if m["type"] == protocol.MSG_ACK), 1)


class TheClipboardOnLettingGo(Case):
    def test_an_unchanged_clipboard_is_not_sent(self):
        self.machine.clipboard.copy("copied before anyone drove")
        b = self.link(B)
        self.own(b)
        b.let_go(target=C)
        self.assertIsNone(b.expect(protocol.MSG_CLIPBOARD, timeout=0.4))

    def test_a_change_made_here_is_sent(self):
        b = self.link(B)
        self.own(b)
        self.machine.clipboard.copy("copied here")
        b.let_go(target=C)
        self.assertEqual(b.expect(protocol.MSG_CLIPBOARD), {"text": "copied here"})

    def test_what_the_owner_brought_is_not_echoed(self):
        b = self.link(B)
        self.own(b)
        b.send(protocol.clipboard_msg("brought"))
        self.assertTrue(wait_for(lambda: self.machine.clipboard.set_calls))
        b.let_go(target=C)
        self.assertIsNone(b.expect(protocol.MSG_CLIPBOARD, timeout=0.4))

    def test_nothing_is_sent_when_ownership_ends_any_other_way(self):
        with mock.patch.object(receiver, "FORCED_END_SECONDS", 0.2):
            b = self.link(B)
            self.own(b)
            self.machine.clipboard.copy("copied here")
            self.machine.responder.send_home()
            self.assertIsNotNone(b.expect(REFUSE))
            self.assertIsNone(b.expect(protocol.MSG_CLIPBOARD, timeout=0.3))


class Zones(Case):
    sides = {B: "left", C: "right", D: "top"}
    zones = [{"peer": protocol.id_text(B), "kind": "edge"},
             {"peer": protocol.id_text(C), "kind": "edge"},
             {"peer": protocol.id_text(D), "kind": "edge"}]

    def push(self, link, cursor, dx, dy):
        self.machine.desktop.cursor = cursor
        seq = link.move(dx, dy)
        self.assertIsNotNone(link.acked(seq))

    def test_reach_limits_the_zones_armed(self):
        b = self.link(B)
        route = self.own(b, resistance_px=120, reach=[C])
        self.push(b, (960, 0), 0, -200)
        self.assertIn(("move", 0, -200), self.machine.injected())
        self.push(b, (1919, 500), 200, 0)
        switch = b.expect(protocol.MSG_SWITCH)
        self.assertEqual((switch["route"], switch["next"], switch["edge"]), (route, protocol.id_text(C), "left"))
        self.assertAlmostEqual(switch["offset"], 500 / 1080, places=2)

    def test_a_take_without_reach_arms_only_the_way_home(self):
        b = self.link(B)
        self.own(b)
        self.push(b, (1919, 500), 200, 0)
        self.assertIn(("move", 200, 0), self.machine.injected())
        self.push(b, (0, 500), -200, 0)
        self.assertEqual(b.expect(protocol.MSG_SWITCH)["next"], protocol.id_text(B))

    def test_the_owners_resistance_is_used(self):
        b = self.link(B)
        self.own(b, resistance_px=500, reach=[])
        self.push(b, (0, 500), -200, 0)
        self.assertIsNone(b.expect(protocol.MSG_SWITCH, timeout=0.2))
        self.assertEqual(self.machine.desktop.placed[-1], (0, 500))
        self.assertNotIn(("move", -200, 0), self.machine.injected())

    def test_resistance_out_of_range_is_malformed(self):
        self.refused(self.link(B), "malformed", resistance_px=1001)

    def test_after_a_switch_the_zones_are_disarmed_until_a_stay(self):
        b = self.link(B)
        self.own(b, reach=[C])
        self.push(b, (1919, 500), 200, 0)
        self.assertIsNotNone(b.expect(protocol.MSG_SWITCH))
        self.push(b, (1919, 500), 200, 0)
        self.assertIsNone(b.expect(protocol.MSG_SWITCH, timeout=0.2))
        self.assertIn(("move", 200, 0), self.machine.injected())
        self.own(b, stay=True, resistance_px=120, reach=[C])
        self.push(b, (1919, 500), 200, 0)
        self.assertIsNotNone(b.expect(protocol.MSG_SWITCH))
        self.assertEqual(self.machine.arrivals, [(None, 960, 540)])

    def test_held_edges_do_not_cross(self):
        b = self.link(B)
        self.own(b, reach=[C])
        self.machine.responder.edges_held = lambda: True
        self.push(b, (1919, 500), 200, 0)
        self.assertIsNone(b.expect(protocol.MSG_SWITCH, timeout=0.2))

    def test_the_send_home_names_no_position(self):
        b = self.link(B)
        route = self.own(b, reach=[C])
        self.machine.responder.send_home()
        self.assertEqual(b.expect(protocol.MSG_SWITCH), {"route": route, "next": protocol.id_text(B)})
        # Its zones are disarmed with it.
        self.push(b, (1919, 500), 200, 0)
        self.assertIsNone(b.expect(protocol.MSG_SWITCH, timeout=0.2))

    def test_a_zone_change_rearms_while_driven(self):
        b = self.link(B)
        self.own(b, reach=[C])
        self.machine.settings.data["zones"][1]["off"] = True
        self.machine.responder.rearm()
        self.push(b, (1919, 500), 200, 0)
        self.assertIsNone(b.expect(protocol.MSG_SWITCH, timeout=0.2))


class ZoneModels(unittest.TestCase):
    peers = [entry(B, "Bee", side="left"), entry(C, "Sea", side="top"), entry(D, "Dee", side="")]

    def models(self, zones, allowed=(B, C, D), resistance=150, notch=None):
        return receiver.zone_models(zones, self.peers, set(allowed), resistance, notch)

    def test_each_kind(self):
        zones = [
            {"peer": protocol.id_text(B), "kind": "edge"},
            {"peer": protocol.id_text(C), "kind": "part", "parts": ["start", "end"]},
            {"peer": protocol.id_text(B), "kind": "corner", "corner": "top_right", "edge": "right"},
            {"peer": protocol.id_text(C), "kind": "notch"},
        ]
        found = self.models(zones, notch=lambda: (100, 200))
        kinds = [(peer, type(model)) for peer, model in found]
        # Corners first, then edges, then parts, then the notch.
        self.assertEqual(kinds, [(B, CornerPush), (B, ReturnEdge), (C, PartEdge), (C, SpanEdge)])
        self.assertEqual([model.edge for _, model in found], ["right", "left", "top", "top"])
        self.assertEqual({model.resistance_px for _, model in found}, {150})
        self.assertEqual(found[2][1].parts, frozenset({"start", "end"}))

    def test_what_is_left_out(self):
        zones = [
            {"peer": protocol.id_text(B), "kind": "edge", "off": True},
            {"peer": "", "kind": "edge"},
            {"peer": protocol.id_text(D), "kind": "edge"},
            {"peer": protocol.id_text(C), "kind": "notch"},
            {"peer": protocol.id_text(C), "kind": "edge"},
            {"peer": "junk", "kind": "edge"},
            {"peer": protocol.id_text(B), "kind": "sideways"},
        ]
        self.assertEqual([peer for peer, _ in self.models(zones, allowed=(B, D))], [])
        self.assertEqual([peer for peer, _ in self.models(zones)], [C])

    def test_a_corner_lands_at_the_end_nearest_it(self):
        self.assertEqual(receiver.corner_offset("top_right", "right"), 0.0)
        self.assertEqual(receiver.corner_offset("top_right", "top"), 1.0)
        self.assertEqual(receiver.corner_offset("bottom_left", "left"), 1.0)
        self.assertEqual(receiver.corner_offset("bottom_left", "bottom"), 0.0)


if __name__ == "__main__":
    unittest.main()


class DrivenNeverWaits(unittest.TestCase):
    def test_driven_is_answered_while_the_responders_lock_is_held_by_another_thread(self):
        import threading

        machine = Machine([entry(B, "Bee")]).start()
        self.addCleanup(machine.stop)
        held, release, answered = threading.Event(), threading.Event(), []

        def hold():
            with machine.responder._lock:
                held.set()
                release.wait(3)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        held.wait(3)
        asker = threading.Thread(target=lambda: answered.append(machine.responder.driven), daemon=True)
        asker.start()
        asker.join(0.5)
        release.set()
        self.assertEqual(answered, [False], "driven waited for the lock")
