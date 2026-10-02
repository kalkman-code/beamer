"""Three desktops over real sockets on loopback, each its own owner and its own responder, paired as a
full mesh (WIRE.md sections 4, 5 and 8): in a row and in an L, every border crossed both ways with
the keyboard on each machine in turn, a chain out and back, and a machine dropping in the middle of
one. A machine's own pointer crosses by core/receiver.zone_models, the models the PC's sender uses;
a driven pointer crosses by the responder's zones and a `switch`, all core/owner.py's decisions."""

import time
import unittest

from core import owner as owner_module
from core import protocol
from core import receiver
from core import return_edge
from core import ways
from core.tests.responder_harness import Machine, entry, ident, wait_for
from core.tests.test_three_machines import NAMES, ScriptedOwner, key, moves, text

A, B, C = ident(190), ident(220), ident(250)
POINT = {"left": (0, 540), "right": (1919, 540), "top": (960, 0), "bottom": (960, 1079)}
PUSH = {"left": (-200, 0), "right": (200, 0), "top": (0, -200), "bottom": (0, 200)}


class Desk:
    """One desktop: `machine` is its responder (what drives it from elsewhere lands there, and its
    screen), `owner` its own keyboard and mouse, linked to the other two."""

    def __init__(self, test, own, sides, zones):
        self.own = own
        self.machine = Machine(
            [entry(peer, NAMES[peer], token=key(own, peer), side=side) for peer, side in sides.items()],
            [{"peer": text(peer), **zone} for peer, zone in zones], own=own, name=NAMES[own],
            away=lambda: self.owner is not None and self.owner.owner.away,
        ).start()
        test.addCleanup(self.machine.stop)
        self.owner = None

    def connect(self, test, others):
        self.owner = ScriptedOwner(test, self.own, [desk.machine for desk in others])


class Desks(unittest.TestCase):
    """Subclasses set `LAYOUT`: for each machine its peers' sides and its zones."""

    LAYOUT = {}

    def setUp(self):
        self.desks = {own: Desk(self, own, sides, zones) for own, (sides, zones) in self.LAYOUT.items()}
        for own, desk in self.desks.items():
            desk.connect(self, [other for ident_, other in self.desks.items() if ident_ != own])

    def at(self, holder):
        """The machine whose screen `holder`'s pointer is on."""
        on = self.desks[holder].owner.on
        return holder if on is None else next(own for own in self.desks if text(own) == on)

    def push(self, holder, side):
        """`holder`'s hand pushes its pointer through `side` of the screen it is on."""
        owner = self.desks[holder].owner
        here = self.at(holder)
        if here == holder:
            desk = self.desks[holder]
            entries = desk.machine.settings.data["peers"]
            allowed = {protocol.read_id(item["id"]) for item in entries}
            models = receiver.zone_models(desk.machine.settings.data["zones"], entries, allowed, 120)
            desk.machine.desktop.cursor = POINT[side]
            for _ in range(5):
                for peer, model in models:
                    outcome = model.feed(desk.machine.desktop.monitors(), POINT[side], *PUSH[side])
                    if outcome.action == return_edge.CROSS:
                        owner.carry_out(owner.owner.go(text(peer), outcome.edge, outcome.offset))
                        return
            return
        self.desks[here].machine.desktop.cursor = POINT[side]
        owner.move(*PUSH[side])

    def assert_on(self, holder, machine):
        """`holder`'s input is on `machine` (home when it is `holder`), and only there."""
        owner = self.desks[holder].owner
        want = None if machine == holder else text(machine)
        self.assertTrue(owner.until(lambda: owner.on == want), (NAMES[holder], owner.on, NAMES[machine]))
        for own, desk in self.desks.items():
            expected = holder if own == machine and machine != holder else None
            self.assertTrue(wait_for(lambda: desk.machine.responder.owner == expected),
                            (NAMES[own], desk.machine.responder.owner, expected))

    def walk(self, holder, steps):
        """Each step: push through a side of the screen the pointer is on, and land on a machine."""
        for side, machine in steps:
            self.push(holder, side)
            self.assert_on(holder, machine)
            # The machine reached takes this holder's input and nobody else's.
            if machine != holder:
                target = self.desks[machine].machine
                before = len(moves(target))
                target.desktop.cursor = (960, 540)
                self.desks[holder].owner.move(3, 4)
                self.assertTrue(self.desks[holder].owner.until(lambda: len(moves(target)) == before + 1))


ROW = {
    A: ({B: "right", C: ""}, [(B, {"kind": "edge"})]),
    B: ({A: "left", C: "right"}, [(A, {"kind": "edge"}), (C, {"kind": "edge"})]),
    C: ({B: "left", A: ""}, [(B, {"kind": "edge"})]),
}


class ARow(Desks):
    """Ay | Bee | Sea."""

    LAYOUT = ROW

    def test_with_the_keyboard_on_ay_every_border_both_ways_out_and_back(self):
        self.walk(A, [("right", B), ("right", C), ("left", B), ("left", A)])

    def test_with_the_keyboard_on_bee_both_neighbours_and_home_from_each(self):
        self.walk(B, [("left", A), ("right", B), ("right", C), ("left", B)])

    def test_with_the_keyboard_on_sea_the_chain_the_other_way(self):
        self.walk(C, [("left", B), ("left", A), ("right", B), ("right", C)])

    def test_the_far_end_of_the_chain_comes_home_through_the_machine_between(self):
        self.walk(A, [("right", B), ("right", C), ("left", B), ("left", A), ("right", B)])
        hand = self.desks[A].owner
        takes = [peer for peer, data in hand.sent if data["target"] == peer]
        self.assertEqual([NAMES[next(own for own in self.desks if text(own) == peer)] for peer in takes],
                         ["Bee", "Sea", "Bee", "Bee"])

    def test_an_edge_with_no_zone_leads_nowhere(self):
        self.push(A, "left")
        self.assertFalse(self.desks[A].owner.until(lambda: self.desks[A].owner.on is not None, timeout=0.3))
        self.walk(A, [("right", B), ("right", C)])
        self.push(A, "right")
        time.sleep(0.2)
        self.assertEqual(self.desks[A].owner.on, text(C))

    def test_two_keyboards_never_share_a_machine(self):
        self.walk(A, [("right", B)])
        hand = self.desks[C].owner
        hand.go(self.desks[B].machine)
        self.assertTrue(hand.until(lambda: hand.moved and hand.moved[-1].why == "owned"))
        self.assertIsNone(hand.on)
        self.assertEqual(self.desks[B].machine.responder.owner, A)


class ARowLosingAMachine(Desks):
    LAYOUT = ROW

    def drop(self, gone):
        """`gone` stops, and every other machine's link to it ends, as its watchdog would end it."""
        self.desks[gone].machine.stop()
        for own, desk in self.desks.items():
            if own != gone:
                desk.owner.carry_out(desk.owner.owner.link_down(text(gone)))

    def test_the_far_end_dropping_brings_the_input_home_and_leaves_no_machine_owned(self):
        self.walk(A, [("right", B), ("right", C)])
        self.drop(C)
        hand = self.desks[A].owner
        self.assertTrue(hand.until(lambda: hand.on is None))
        self.assertTrue(wait_for(lambda: self.desks[B].machine.responder.owner is None))
        self.walk(A, [("right", B)])

    def test_the_next_machine_dropping_mid_chain_keeps_the_input_where_it_is_and_out_of_its_reach(self):
        self.walk(A, [("right", B)])
        hand = self.desks[A].owner
        stays = len([data for peer, data in hand.sent if data.get("stay")])
        self.drop(C)
        # The owner re-arms Bee at once with a reach that leaves Sea out, so Bee's zone to Sea is
        # dead and a push through it is not even asked about.
        self.assertTrue(hand.until(lambda: len([data for peer, data in hand.sent if data.get("stay")]) > stays))
        self.assertNotIn(text(C), [data for peer, data in hand.sent if data.get("stay")][-1]["reach"])
        takes = len([peer for peer, data in hand.sent if data["target"] == peer])
        self.push(A, "right")
        time.sleep(0.3)
        hand.pump()
        self.assertEqual(hand.on, text(B))
        self.assertEqual(len([peer for peer, data in hand.sent if data["target"] == peer]), takes)
        self.walk(A, [("left", A)])

    def test_the_machine_between_dropping_takes_the_far_one_out_of_the_way_home_but_not_out_of_reach(self):
        self.walk(A, [("right", B), ("right", C)])
        self.drop(B)
        hand = self.desks[A].owner
        # Still on Sea: its link to Ay is its own, so losing Bee costs only the way back through Bee.
        time.sleep(0.2)
        hand.pump()
        self.assertEqual(hand.on, text(C))
        self.push(A, "left")
        time.sleep(0.3)
        hand.pump()
        self.assertEqual(hand.on, text(C))
        hand.go(None)
        self.assertIsNone(hand.on)
        self.assertTrue(wait_for(lambda: self.desks[C].machine.responder.owner is None))


class AMachinePairedLate(Desks):
    """The rig of 01-10-2026: Bee paired first with its edge, Sea paired second with no zone at all.
    Sea's side arrives over its link, as `arrangement` brings it, and has to give Ay a way to it."""

    LAYOUT = {
        A: ({B: "left", C: ""}, [(B, {"kind": "edge"})]),
        B: ({A: "right", C: ""}, [(A, {"kind": "edge"})]),
        C: ({A: "left", B: ""}, [(A, {"kind": "edge"})]),
    }

    def arrives(self, here, sender, edge, set_at, by=None, way_back=None):
        """`sender`'s arrangement over its own link to `here`, applied as both apps apply it."""
        link = self.desks[sender].owner.links[text(here)]
        machine = self.desks[here].machine
        seen = len(machine.arrangements)
        link.send(protocol.arrangement_v6(edge, set_at, by or sender, way_back=way_back))
        self.assertTrue(wait_for(lambda: len(machine.arrangements) > seen))
        peer, read = machine.arrangements[seen]
        return ways.arrangement(machine.settings.data, text(peer), read["edge"], read["set_at"],
                                text(read["by"]), "this PC", read.get("way_back"))

    def answers(self, here, to):
        """`here`'s own arrangement for `to`, as both apps send it after one that changed something."""
        settings = self.desks[here].machine.settings.data
        held = next(item for item in settings["peers"] if item["id"] == text(to))
        by = protocol.read_id(held["side_by"])
        return self.arrives(to, here, held["side"], held["side_set_at"], by, ways.has_way(settings, text(to)))

    def test_a_side_arriving_for_a_machine_with_no_zone_gives_it_its_edge_and_it_can_be_crossed_to(self):
        self.assertEqual(self.arrives(A, C, "left", 1_790_000_100), (True, []))
        self.walk(A, [("right", C), ("left", A), ("left", B), ("right", A)])

    def test_a_side_arriving_onto_another_machines_edge_is_refused_out_loud_and_the_thirds_share_it(self):
        changed, notices = self.arrives(A, C, "right", 1_790_000_100)
        self.assertTrue(changed)
        # The harness's handshakes name every machine "Peer", so the sentences are held by their shape.
        self.assertEqual(len(notices), 1)
        self.assertIn("now lead from the same part of this PC's screen", notices[0])
        self.assertTrue(notices[0].endswith("edge is off."))
        settings = self.desks[A].machine.settings.data
        self.assertIn("has no way in", ways.blocked_sentence(settings, text(C), "this PC"))
        self.assertFalse(ways.has_way(settings, text(C)))
        # Ay answers, and Sea learns at once that its side was taken but leads nowhere.
        on_sea = self.desks[C].machine.settings.data
        self.assertEqual(self.answers(A, C), (True, []))
        self.assertIn("has no way back", ways.no_way_back_sentence(on_sea, text(A)))
        self.assertEqual(self.answers(A, C), (False, []))
        # What the Crossing page then offers: each its own thirds of the left edge.
        ways.edit(settings, text(B), side="left", methods=["part"], parts=["start"], corner="top_left",
                  kinds=("edge", "part", "corner"), corner_edge=lambda corner, side: side, now=time.time())
        ways.edit(settings, text(C), side="left", methods=["part"], parts=["end"], corner="top_left",
                  kinds=("edge", "part", "corner"), corner_edge=lambda corner, side: side, now=time.time())
        self.assertIsNone(ways.clash(settings, "this PC"))
        self.assertTrue(ways.has_way(settings, text(C)))
        self.assertEqual(self.answers(A, C), (True, []))
        self.assertEqual(ways.no_way_back_sentence(on_sea, text(A)), "")
        POINT["left"] = (0, 100)
        self.addCleanup(POINT.__setitem__, "left", (0, 540))
        self.walk(A, [("left", B)])
        self.desks[A].owner.go(None)
        POINT["left"] = (0, 1000)
        self.walk(A, [("left", C)])


L_SHAPE = {
    A: ({B: "right", C: "top"}, [(B, {"kind": "edge"}), (C, {"kind": "edge"})]),
    B: ({A: "left", C: ""}, [(A, {"kind": "edge"})]),
    C: ({A: "bottom", B: ""}, [(A, {"kind": "edge"})]),
}


class AnL(Desks):
    """Sea above Ay, Bee to Ay's right: Ay has peers on two edges."""

    LAYOUT = L_SHAPE

    def test_from_the_corner_machine_out_to_each_and_back(self):
        self.walk(A, [("right", B), ("left", A), ("top", C), ("bottom", A)])

    def test_from_one_arm_to_the_other_through_the_corner_machine(self):
        self.walk(B, [("left", A), ("top", C), ("bottom", A), ("right", B)])
        self.walk(C, [("bottom", A), ("right", B), ("left", A), ("top", C)])

    def test_the_arms_have_no_border_of_their_own(self):
        self.walk(B, [("left", A), ("top", C)])
        for side in ("left", "right", "top"):
            self.push(B, side)
        time.sleep(0.2)
        self.desks[B].owner.pump()
        self.assertEqual(self.desks[B].owner.on, text(C))


class SharedEdge(Desks):
    """Bee on the top third of Ay's right edge and Sea on the bottom third: two machines on one side."""

    LAYOUT = {
        A: ({B: "right", C: "right"}, [(B, {"kind": "part", "parts": ["start"]}), (C, {"kind": "part", "parts": ["end"]})]),
        B: ({A: "left", C: ""}, [(A, {"kind": "edge"})]),
        C: ({A: "left", B: ""}, [(A, {"kind": "edge"})]),
    }

    def test_each_third_leads_to_its_own_machine(self):
        POINT["right"] = (1919, 100)
        self.addCleanup(POINT.__setitem__, "right", (1919, 540))
        self.walk(A, [("right", B), ("left", A)])
        POINT["right"] = (1919, 1000)
        self.walk(A, [("right", C), ("left", A)])
        POINT["right"] = (1919, 540)
        self.push(A, "right")
        self.assertFalse(self.desks[A].owner.until(lambda: self.desks[A].owner.on is not None, timeout=0.3))


if __name__ == "__main__":
    unittest.main()
