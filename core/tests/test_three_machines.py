"""Three machines and more over real sockets on loopback (WIRE.md sections 4 and
5, routing and input ownership): LinkResponders driven by scripted owners whose every
decision is core/owner.py's, so the owner's state machine and the responder are held to each other
with nothing faked but the platform edges. The links are the harness's scripted initiators."""

import time
import unittest

from core import owner as owner_module
from core import protocol
from core.tests.responder_harness import Initiator, Machine, entry, ident, token, wait_for

O, O2 = ident(130), ident(160)
A, B, C = ident(190), ident(220), ident(250)
NAMES = {A: "Ay", B: "Bee", C: "Sea", O: "Owner", O2: "Other"}


def key(one, other):
    """The token one pair shares, the same whichever end asks."""
    low, high = sorted((one, other))
    return token(low[0] * 3 + high[0])


def text(peer):
    return protocol.id_text(peer)


class ScriptedOwner:
    """A machine's input, routed by core/owner.Owner over one scripted link per responder. `pump`
    reads what the responders sent and carries out the owner's actions; `sent` keeps every
    `focus` the owner sent, by peer."""

    def __init__(self, test, own, machines):
        self.test = test
        self.own = own
        self.owner = owner_module.Owner(text(own), time.monotonic)
        self.links = {}
        self.sent = []
        self.moved = []
        self.clipboard = "owner's"
        self.clipboards = []
        for machine in machines:
            link = Initiator(machine, own, key(own, machine.own))
            test.addCleanup(link.close)
            welcome = link.handshake()
            self.links[text(machine.own)] = link
            self.carry_out(self.owner.link_up(text(machine.own), welcome["accepts"]))

    def carry_out(self, actions):
        for action in actions:
            if isinstance(action, owner_module.Send):
                message = action.message
                if message["type"] == protocol.MSG_FOCUS:
                    self.sent.append((action.peer, dict(message["data"])))
                if message["type"] in protocol.INPUT_TYPES:
                    self.links[action.peer].input(message)
                else:
                    self.links[action.peer].send(message)
            elif isinstance(action, owner_module.SendClipboard):
                self.links[action.peer].send(protocol.clipboard_msg(text=self.clipboard))
            elif isinstance(action, owner_module.SetClipboard):
                self.clipboard = action.message["data"].get("text")
                self.clipboards.append(self.clipboard)
            elif isinstance(action, owner_module.Moved):
                self.moved.append(action)

    def pump(self):
        for peer, link in self.links.items():
            while not link.inbox.empty():
                message = link.inbox.get_nowait()
                kind, data = message.get("type"), message.get("data") or {}
                if kind == protocol.MSG_SWITCH:
                    self.carry_out(self.owner.switch(peer, data))
                elif kind == protocol.MSG_ACCEPT:
                    self.carry_out(self.owner.accept(peer, data))
                elif kind == protocol.MSG_REFUSE:
                    self.carry_out(self.owner.refuse(peer, data))
                elif kind == protocol.MSG_ACCEPTS:
                    self.carry_out(self.owner.accepts(peer, data.get("accepts")))
                elif kind == protocol.MSG_CLIPBOARD:
                    self.carry_out(self.owner.clipboard_arrived(peer, time.monotonic(), message))
        self.carry_out(self.owner.tick())

    def until(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def go(self, machine):
        self.carry_out(self.owner.go(None if machine is None else text(machine.own)))

    def move(self, dx, dy):
        self.carry_out(self.owner.input(protocol.mousemove_msg(dx, dy)))

    @property
    def on(self):
        return self.owner.on


def responder(test, own, peers, zones=(), away=False):
    """A machine `own` whose peers are (id, side) pairs and whose zones lead to `zones`."""
    entries = [entry(peer, NAMES[peer], token=key(own, peer), side=side) for peer, side in peers]
    machine = Machine(entries, [{"peer": text(peer), "kind": "edge"} for peer in zones], away=away, own=own, name=NAMES[own]).start()
    test.addCleanup(machine.stop)
    return machine


def moves(machine):
    return [call for call in machine.injected() if call[0] == "move"]


class Chain(unittest.TestCase):
    """Owner to A, A's zone to B, B's zone to C, C's zone back to A, over four real links."""

    def setUp(self):
        self.a = responder(self, A, [(O, "bottom"), (B, "right"), (C, "left")], zones=(O, B))
        self.b = responder(self, B, [(O, "bottom"), (A, "left"), (C, "right")], zones=(O, C))
        self.c = responder(self, C, [(O, "bottom"), (B, "left"), (A, "right")], zones=(O, A))
        self.machines = {A: self.a, B: self.b, C: self.c}
        self.o = ScriptedOwner(self, O, [self.a, self.b, self.c])
        self.o.go(self.a)
        self.assertTrue(self.o.until(lambda: self.a.responder.owner == O))

    def push_right(self, machine):
        """The pointer at `machine`'s right edge, pushed on through it."""
        machine.desktop.cursor = (1919, 500)
        self.o.move(200, 0)

    def push_left(self, machine):
        machine.desktop.cursor = (0, 500)
        self.o.move(-200, 0)

    def hand_over(self, push, here, there):
        before = len(self.o.sent)
        push(self.machines[here])
        self.assertTrue(self.o.until(lambda: self.o.on == text(there) and self.machines[there].responder.owner == O))
        self.assertTrue(wait_for(lambda: self.machines[here].responder.owner is None))
        return self.o.sent[before:]


class AChain(Chain):
    def test_every_hand_over_has_one_route_and_every_machine_left_is_let_go(self):
        routes = []
        for push, here, there in ((self.push_right, A, B), (self.push_right, B, C), (self.push_right, C, A)):
            sent = self.hand_over(push, here, there)
            take = [data for peer, data in sent if peer == text(there) and data["target"] == text(there)]
            let_go = [data for peer, data in sent if peer == text(here) and data["target"] != text(here)]
            self.assertEqual(len(take), 1, sent)
            self.assertEqual(len(let_go), 1, sent)
            self.assertEqual(take[0]["route"], let_go[0]["route"])
            self.assertEqual(let_go[0]["target"], text(there))
            routes.append(take[0]["route"])
        self.assertEqual(routes, sorted(set(routes)))
        self.assertEqual([self.machines[peer].responder.owner for peer in (A, B, C)], [O, None, None])

    def test_no_machine_but_the_current_one_injects(self):
        for push, here, there in ((self.push_right, A, B), (self.push_right, B, C), (self.push_right, C, A), (self.push_right, A, B)):
            self.hand_over(push, here, there)
            counts = {peer: len(moves(machine)) for peer, machine in self.machines.items()}
            self.machines[there].desktop.cursor = (960, 540)
            self.o.move(3, 4)
            self.assertTrue(self.o.until(lambda: len(moves(self.machines[there])) == counts[there] + 1))
            time.sleep(0.05)
            self.o.pump()
            self.assertEqual({peer: len(moves(machine)) for peer, machine in self.machines.items()},
                             {**counts, there: counts[there] + 1})
            self.assertEqual(moves(self.machines[there])[-1], ("move", 3, 4))

    def test_the_way_home_from_the_far_end_of_the_chain(self):
        self.hand_over(self.push_right, A, B)
        self.hand_over(self.push_right, B, C)
        self.c.desktop.cursor = (960, 1079)
        self.o.move(0, 200)
        self.assertTrue(self.o.until(lambda: self.o.on is None))
        self.assertTrue(wait_for(lambda: self.c.responder.owner is None))
        self.assertEqual(self.o.moved[-1].why, "switch")
        self.assertEqual([machine.responder.owner for machine in self.machines.values()], [None, None, None])

    def test_a_clipboard_copied_on_b_reaches_c_through_the_owner_when_b_is_left(self):
        self.hand_over(self.push_right, A, B)
        # The owner's clipboard follows its take; a copy made before it lands is overwritten by it.
        self.assertTrue(wait_for(lambda: ("owner's", None) in self.b.clipboard.set_calls))
        self.b.clipboard.copy("copied on Bee")
        self.hand_over(self.push_right, B, C)
        self.assertTrue(self.o.until(lambda: ("copied on Bee", None) in self.c.clipboard.set_calls))
        self.assertEqual(self.o.clipboard, "copied on Bee")
        # A machine left with its clipboard untouched sends none back.
        self.assertTrue(self.o.until(lambda: self.o.on == text(C)))
        self.hand_over(self.push_right, C, A)
        time.sleep(0.2)
        self.o.pump()
        self.assertEqual(self.o.clipboards, ["copied on Bee"])

    def test_a_late_switch_from_the_machine_left_is_ignored(self):
        # A's zone asks for B, but the owner's shortcut takes the input home before it reads that.
        self.push_right(self.a)
        late = self.o.links[text(A)].expect(protocol.MSG_SWITCH)
        self.assertEqual(late["next"], text(B))
        self.o.go(None)
        self.assertIsNone(self.o.on)
        self.o.carry_out(self.o.owner.switch(text(A), late))
        self.assertFalse(self.o.until(lambda: self.o.on is not None, timeout=0.5))
        self.assertEqual([peer for peer, data in self.o.sent if peer == text(B)], [])
        self.assertIsNone(self.b.responder.owner)
        self.assertTrue(wait_for(lambda: self.a.responder.owner is None))


class WhereTheRoutesMustAgree(Chain):
    """The cases the milestone's review found untested over real links: a hand-over refused, a
    machine that sends its owner home, and a machine that stops letting the owner drive."""

    def test_a_refused_hand_over_leaves_the_input_where_it_was_and_the_refuser_out_of_reach(self):
        self.b.away = True
        self.push_right(self.a)
        self.assertTrue(self.o.until(lambda: any(data.get("stay") for peer, data in self.o.sent if peer == text(A))))
        stay = [data for peer, data in self.o.sent if peer == text(A) and data.get("stay")][-1]
        self.assertNotIn(text(B), stay["reach"])
        self.assertEqual(self.o.on, text(A))
        self.assertTrue(wait_for(lambda: self.a.responder.owner == O))
        self.assertIsNone(self.b.responder.owner)
        # Still driving A: the stay was accepted on its own route.
        self.a.desktop.cursor = (960, 540)
        before = len(moves(self.a))
        self.o.move(5, 0)
        self.assertTrue(self.o.until(lambda: len(moves(self.a)) == before + 1))

    def test_a_machine_that_sends_its_owner_home_refuses_it_for_a_while(self):
        self.assertTrue(self.a.responder.send_home())
        self.assertTrue(self.o.until(lambda: self.o.on is None))
        self.assertTrue(wait_for(lambda: self.a.responder.owner is None))
        self.o.go(self.a)
        self.assertTrue(self.o.until(lambda: self.o.moved[-1].why == "sent_home"))
        self.assertIsNone(self.o.on)
        self.assertIsNone(self.a.responder.owner)

    def test_turning_allow_drive_off_sends_the_input_home_and_takes_the_machine_out_of_reach(self):
        self.a.settings.peer(key(A, O))["allow_drive"] = False
        self.a.responder.peers_changed()
        self.assertTrue(self.o.until(lambda: self.o.on is None))
        self.assertTrue(wait_for(lambda: self.a.responder.owner is None))
        self.assertEqual(self.o.owner.go(text(A)), [owner_module.Unreachable(text(A), "unreachable")])
        self.o.go(self.b)
        self.assertTrue(self.o.until(lambda: self.b.responder.owner == O))
        self.assertNotIn(text(A), [data for peer, data in self.o.sent if peer == text(B)][-1]["reach"])


class Competing(unittest.TestCase):
    def setUp(self):
        self.a = responder(self, A, [(O, "left"), (O2, "right")], zones=(O, O2))
        self.first = ScriptedOwner(self, O, [self.a])
        self.second = ScriptedOwner(self, O2, [self.a])

    def test_two_owners_compete_and_the_second_gets_in_once_the_first_lets_go(self):
        self.first.go(self.a)
        self.assertTrue(self.first.until(lambda: self.a.responder.owner == O))
        self.second.go(self.a)
        self.assertTrue(self.second.until(lambda: self.second.on is None))
        self.assertEqual(self.second.moved[-1].why, "owned")
        self.assertEqual(self.a.responder.owner, O)
        self.first.go(None)
        self.assertTrue(wait_for(lambda: self.a.responder.owner is None))
        self.second.go(self.a)
        self.assertTrue(self.second.until(lambda: self.a.responder.owner == O2))
        self.assertEqual(self.second.on, text(A))
        self.assertIsNone(self.first.on)

    def test_only_the_owner_injects(self):
        self.first.go(self.a)
        self.assertTrue(self.first.until(lambda: self.a.responder.owner == O))
        self.second.go(self.a)
        self.assertTrue(self.second.until(lambda: self.second.on is None))
        # The second's input stays at home, so nothing it moves reaches A.
        self.assertEqual(self.second.owner.input(protocol.mousemove_msg(9, 9)), [owner_module.LOCAL])
        self.first.move(1, 2)
        self.assertTrue(wait_for(lambda: moves(self.a) == [("move", 1, 2)]))


class TwoDesktopsTakeEachOther(unittest.TestCase):
    """Each runs a responder and an owner, and its responder's `busy` reads its own owner."""

    def test_both_end_at_home_and_neither_is_driven(self):
        owners = {}
        x = responder(self, A, [(B, "right")], zones=(B,), away=lambda: owners[A].owner.away)
        y = responder(self, B, [(A, "left")], zones=(A,), away=lambda: owners[B].owner.away)
        owners[A] = ScriptedOwner(self, A, [y])
        owners[B] = ScriptedOwner(self, B, [x])
        # Both decide before either take is on the wire: the same instant.
        takes = [owners[A].owner.go(text(B)), owners[B].owner.go(text(A))]
        owners[A].carry_out(takes[0])
        owners[B].carry_out(takes[1])
        # Both answers are in before either owner reads one: an owner that took its refusal first
        # would be home, no longer busy, and the other take could rightly get in.
        refusals = {here: owners[here].links[text(there)].expect(protocol.MSG_REFUSE) for here, there in ((A, B), (B, A))}
        for here, there in ((A, B), (B, A)):
            self.assertEqual(refusals[here]["why"], "busy")
            owners[here].carry_out(owners[here].owner.refuse(text(there), refusals[here]))
            self.assertIsNone(owners[here].on)
            self.assertEqual(owners[here].moved[-1].why, "busy")
        self.assertIsNone(x.responder.owner)
        self.assertIsNone(y.responder.owner)
        # Neither is stuck: one take now gets in.
        owners[A].go(y)
        self.assertTrue(owners[A].until(lambda: y.responder.owner == A))


if __name__ == "__main__":
    unittest.main()
