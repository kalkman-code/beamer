"""The owner's state machine (WIRE.md sections 4 and 5), without sockets: the messages it sends, on
which link, and what it does with this machine's input, for every event. A scripted clock drives
the timers."""

import unittest

from core import owner
from core.owner import LOCAL, Drop, Moved, Send, SendClipboard, SetClipboard, Unreachable


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def key(kind, name, us=None):
    data = {"key": name}
    if us is not None:
        data["us"] = us
    return {"type": kind, "data": data}


def button(kind, name="left"):
    return {"type": kind, "data": {"button": name}}


MOVE = {"type": "mousemove", "data": {"dx": 3, "dy": -1}}


def focuses(actions, peer=None):
    return [a.message["data"] for a in actions
            if isinstance(a, Send) and a.message["type"] == "focus" and (peer is None or a.peer == peer)]


def sent(actions, peer=None):
    return [(a.peer, a.message) for a in actions if isinstance(a, Send) and (peer is None or a.peer == peer)]


def moved(actions):
    found = [a for a in actions if isinstance(a, Moved)]
    return found[-1] if found else None


class ByteIds(unittest.TestCase):
    """The apps name machines by their 16-byte ids, not by strings."""

    def test_a_peer_that_refused_a_hand_over_stays_out_of_reach_for_three_seconds(self):
        clock = Clock()
        o = owner.Owner(b"A" * 16, clock)
        b, c = b"B" * 16, b"C" * 16
        for peer in (b, c):
            o.link_up(peer, True)
        o.go(b)
        o.accept(b, {"route": o.route})
        o.switch(b, {"route": o.route, "next": c})
        actions = o.refuse(c, {"route": o.route, "why": "owned"})
        stay = [a.message["data"] for a in actions if isinstance(a, Send) and a.message["data"].get("stay")]
        self.assertEqual(stay[0]["reach"], [])
        clock.now += owner.LEFT_OUT + 0.1
        self.assertEqual(o.reach_for(b), [c])


class OwnerCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.o = owner.Owner("A", self.clock)
        for peer in ("B", "C", "D"):
            self.assertEqual(self.o.link_up(peer, True), [])

    def take(self, peer, edge=None, offset=None):
        actions = self.o.go(peer, edge, offset)
        self.assertEqual(self.o.accept(peer, {"route": self.o.route}), [])
        return actions

    def switch(self, frm, nxt, route=None, **where):
        return self.o.switch(frm, {"route": self.o.route if route is None else route, "next": nxt, **where})


class TakeFromHomeTests(OwnerCase):
    def test_a_take_by_a_zone_carries_the_route_the_landing_the_resistance_and_the_reach(self):
        actions = self.o.go("B", "left", 0.5)
        self.assertEqual(focuses(actions), [{"route": 1, "target": "B", "edge": "left", "offset": 0.5,
                                             "resistance_px": 120, "reach": ["C", "D"]}])
        self.assertEqual(moved(actions), Moved("B", None, "asked", "left", 0.5))
        self.assertEqual(self.o.on, "B")
        self.assertEqual(self.o.reach_sent, ["C", "D"])

    def test_a_take_by_the_shortcut_has_no_landing(self):
        self.assertEqual(focuses(self.o.go("B")), [{"route": 1, "target": "B", "resistance_px": 120, "reach": ["C", "D"]}])

    def test_a_phone_leaves_resistance_out(self):
        phone = owner.Owner("P", self.clock, resistance_px=None)
        phone.link_up("B", True)
        self.assertEqual(focuses(phone.go("B")), [{"route": 1, "target": "B", "reach": []}])

    def test_input_goes_at_once_without_waiting_for_the_answer(self):
        self.o.go("B")
        self.assertEqual(self.o.input(MOVE), [Send("B", MOVE)])

    def test_at_home_input_stays_here(self):
        self.assertEqual(self.o.input(MOVE), [LOCAL])
        self.assertEqual(self.o.input(key("keydown", "a", "a")), [LOCAL])

    def test_no_answer_within_a_second_brings_the_input_home(self):
        self.o.go("B")
        self.assertEqual(self.o.deadline(), 1001.0)
        self.clock.now = 1000.99
        self.assertEqual(self.o.tick(), [])
        self.clock.now = 1001.0
        actions = self.o.tick()
        self.assertEqual(focuses(actions, "B"), [{"route": 2, "target": "A"}])
        self.assertEqual(moved(actions), Moved(None, "B", "no_answer"))
        self.assertFalse(self.o.away)

    def test_an_accept_ends_the_wait(self):
        self.take("B")
        self.assertIsNone(self.o.deadline())
        self.clock.now += 5
        self.assertEqual(self.o.tick(), [])
        self.assertEqual(self.o.on, "B")

    def test_a_refusal_for_the_current_route_sends_the_input_home_saying_why(self):
        for why in ("owned", "not_allowed", "busy", "sent_home", "malformed", "something_new"):
            with self.subTest(why=why):
                self.o.go("B")
                actions = self.o.refuse("B", {"route": self.o.route, "why": why})
                self.assertEqual(moved(actions).to, None)
                self.assertEqual(moved(actions).why, why if why != "something_new" else "refused")
                self.assertFalse(self.o.away)
                self.clock.now += owner.SENT_HOME_FOR

    def test_a_machine_that_refused_a_take_is_not_taken_again_for_a_while(self):
        # The live check, 01-10-2026: every push at the laptop's edge took the rig again while the
        # Mac drove it, about 30 refusals and 30 notices in 20 seconds.
        for why in ("owned", "busy", "not_allowed"):
            with self.subTest(why=why):
                self.o.go("B")
                self.o.refuse("B", {"route": self.o.route, "why": why})
                self.clock.now += owner.LEFT_OUT / 2
                self.assertEqual(self.o.go("B"), [])
                self.assertFalse(self.o.away)
                self.clock.now += owner.LEFT_OUT / 2
                self.assertEqual(moved(self.o.go("B")).to, "B")
                self.o.go(None)

    def test_a_machine_that_sent_this_one_home_is_left_alone_for_as_long_as_it_would_refuse(self):
        # The live check, 01-10-2026: thirty "sent_home" refusals in three seconds between two machines.
        self.o.go("B")
        self.o.refuse("B", {"route": self.o.route, "why": "sent_home"})
        self.clock.now += owner.SENT_HOME_FOR - 0.1
        self.assertEqual(self.o.go("B"), [])
        self.clock.now += 0.1
        self.assertEqual(moved(self.o.go("B")).to, "B")

    def test_the_sent_home_window_is_the_responders_own(self):
        from core import receiver
        self.assertEqual(owner.SENT_HOME_FOR, receiver.SENT_HOME_SECONDS)

    def test_a_machine_held_back_does_not_hold_back_the_others(self):
        self.o.go("B")
        self.o.refuse("B", {"route": self.o.route, "why": "owned"})
        self.assertEqual(moved(self.o.go("C")).to, "C")

    def test_asking_for_a_machine_held_back_from_another_changes_nothing(self):
        self.o.go("B")
        self.o.refuse("B", {"route": self.o.route, "why": "owned"})
        self.take("C")
        route = self.o.route
        self.assertEqual(self.o.go("B"), [])
        self.assertEqual((self.o.on, self.o.route), ("C", route))

    def test_a_hand_over_refused_as_sent_home_holds_back_as_long_as_the_refusal(self):
        self.take("B")
        self.switch("B", "C")
        self.o.refuse("C", {"route": self.o.route, "why": "sent_home"})
        self.clock.now += owner.SENT_HOME_FOR - 0.1
        self.assertNotIn("C", self.o.reach_for(None))
        self.clock.now += 0.1
        self.assertIn("C", self.o.reach_for(None))

    def test_a_refusal_for_an_old_route_is_ignored(self):
        self.take("B")
        self.o.link_up("E", True)  # a stay, route 2
        self.assertEqual(self.o.refuse("B", {"route": 1, "why": "owned"}), [])
        self.assertEqual(self.o.on, "B")
        self.assertEqual(moved(self.o.refuse("B", {"route": 2, "why": "owned"})).to, None)

    def test_a_refusal_from_a_machine_the_input_is_not_on_is_ignored(self):
        self.take("B")
        self.assertEqual(self.o.refuse("C", {"route": self.o.route, "why": "owned"}), [])

    def test_away_from_the_take_until_it_is_refused(self):
        self.assertFalse(self.o.away)
        self.o.go("B")
        self.assertTrue(self.o.away)
        self.o.refuse("B", {"route": 1, "why": "busy"})
        self.assertFalse(self.o.away)

    def test_two_desktops_taking_each_other_both_end_at_home_and_the_next_try_works(self):
        self.o.go("B")
        self.o.refuse("B", {"route": 1, "why": "busy"})
        self.assertIsNone(self.o.on)
        self.clock.now += owner.LEFT_OUT
        self.take("B")
        self.assertEqual(self.o.on, "B")

    def test_a_peer_without_a_link_or_that_does_not_accept_is_not_taken(self):
        self.o.link_up("E", False)
        for peer in ("E", "Z"):
            with self.subTest(peer=peer):
                self.assertEqual(self.o.go(peer), [Unreachable(peer, "unreachable")])
                self.assertFalse(self.o.away)
                self.assertEqual(self.o.route, 0)

    def test_going_where_the_input_already_is_does_nothing(self):
        self.take("B")
        self.assertEqual(self.o.go("B"), [])
        self.o.go(None)
        self.assertEqual(self.o.go(None), [])
        self.assertEqual(self.o.go("A"), [])


class ReachTests(OwnerCase):
    def test_reach_leaves_out_the_owner_the_target_and_peers_that_do_not_accept(self):
        self.o.link_up("E", False)
        self.assertEqual(focuses(self.o.go("B"))[0]["reach"], ["C", "D"])

    def test_the_reach_is_kept_true_while_the_input_is_on_a_machine(self):
        self.take("B")
        self.assertEqual(focuses(self.o.link_up("E", True), "B"),
                         [{"route": 2, "target": "B", "stay": True, "resistance_px": 120, "reach": ["C", "D", "E"]}])
        self.assertEqual(focuses(self.o.link_down("C"), "B")[0]["reach"], ["D", "E"])
        self.assertEqual(focuses(self.o.accepts("D", False), "B"), [{"route": 4, "target": "B", "stay": True,
                                                                     "resistance_px": 120, "reach": ["E"]}])
        self.assertEqual(self.o.accepts("D", False), [])
        self.assertEqual(self.o.reach_sent, ["E"])

    def test_at_home_a_link_changing_sends_nothing(self):
        self.assertEqual(self.o.link_up("E", True), [])
        self.assertEqual(self.o.link_down("E"), [])
        self.assertEqual(self.o.route, 0)

    def test_accepts_for_a_peer_with_no_link_is_ignored(self):
        self.take("B")
        self.assertEqual(self.o.accepts("E", True), [])


class SwitchTests(OwnerCase):
    def test_a_switch_naming_the_owner_brings_the_input_home_landing_where_it_says(self):
        self.take("B")
        actions = self.switch("B", "A", edge="right", offset=0.25)
        self.assertEqual(focuses(actions, "B"), [{"route": 2, "target": "A"}])
        self.assertEqual(moved(actions), Moved(None, "B", "switch", "right", 0.25))

    def test_a_send_home_with_an_old_route_is_honoured_within_the_visit(self):
        self.take("B")
        self.o.link_up("E", True)  # a re-arm crossing B's send-home
        self.assertEqual(moved(self.switch("B", "A", route=1)).to, None)

    def test_a_send_home_from_an_earlier_visit_or_another_machine_is_ignored(self):
        self.take("B")          # route 1
        self.take("C")          # route 2
        self.take("B")          # route 3
        for route in (1, 2):
            self.assertEqual(self.switch("B", "A", route=route), [])
        self.assertEqual(self.switch("C", "A", route=3), [])
        self.assertEqual(self.o.on, "B")

    def test_a_stale_switch_is_ignored(self):
        self.take("B")
        for _ in range(2):      # four re-arms, to route 5
            self.o.link_up("E", True)
            self.o.link_down("E")
        self.assertEqual(self.o.route, 5)
        self.o.go("C")          # the shortcut, route 6
        self.assertEqual(self.switch("B", "D", route=5), [])
        self.assertEqual(self.o.on, "C")

    def test_a_switch_older_than_a_re_arm_is_ignored(self):
        self.take("B")
        self.o.link_up("E", True)
        self.assertEqual(self.switch("B", "C", route=1), [])
        self.assertEqual(self.o.on, "B")

    def test_a_switch_from_a_machine_the_input_is_not_on_is_ignored(self):
        self.take("B")
        self.assertEqual(self.switch("C", "D"), [])
        self.assertEqual(self.switch("C", "A"), [])

    def test_the_chain_hand_over_waits_for_the_accept_holding_the_input(self):
        self.take("B")
        self.assertEqual(self.o.input(key("keydown", "a", "a")), [Send("B", key("keydown", "a", "a"))])
        actions = self.switch("B", "C", edge="left", offset=0.5)
        self.assertEqual(sent(actions), [("C", {"type": "focus", "data": {
            "route": 2, "target": "C", "edge": "left", "offset": 0.5, "resistance_px": 120, "reach": ["B", "D"]}})])
        self.assertEqual(self.o.on, "B")
        self.assertTrue(self.o.away)
        self.assertEqual(self.o.input(MOVE), [])
        self.assertEqual(self.o.input(key("keyup", "a", "a")), [])

        actions = self.o.accept("C", {"route": 2})
        self.assertEqual(sent(actions), [
            ("B", key("keyup", "a", "a")),
            ("B", {"type": "focus", "data": {"route": 2, "target": "C"}}),
            ("C", MOVE),
        ])
        self.assertEqual([a for a in actions if isinstance(a, SendClipboard)], [SendClipboard("C")])
        clip_at = actions.index(SendClipboard("C"))
        self.assertLess(actions.index(Send("B", {"type": "focus", "data": {"route": 2, "target": "C"}})), clip_at)
        self.assertLess(clip_at, actions.index(Send("C", MOVE)))
        self.assertEqual(moved(actions), Moved("C", "B", "switch", "left", 0.5))
        self.assertEqual(self.o.on, "C")
        self.assertEqual(self.o.reach_sent, ["B", "D"])

    def test_a_refusal_from_the_next_machine_re_arms_the_one_left_leaving_it_out_for_three_seconds(self):
        self.take("B")
        self.switch("B", "C")
        self.o.input(MOVE)
        actions = self.o.refuse("C", {"route": 2, "why": "owned"})
        self.assertEqual(sent(actions), [
            ("B", {"type": "focus", "data": {"route": 3, "target": "B", "stay": True, "resistance_px": 120, "reach": ["D"]}}),
            ("B", MOVE),
        ])
        self.assertIn(Unreachable("C", "owned"), actions)
        self.assertIsNone(moved(actions))
        self.assertEqual(self.o.on, "B")
        self.assertEqual(self.o.deadline(), 1003.0)
        self.clock.now = 1003.0
        self.assertEqual(focuses(self.o.tick(), "B"), [{"route": 4, "target": "B", "stay": True, "resistance_px": 120,
                                                        "reach": ["C", "D"]}])

    def test_no_answer_from_the_next_machine_re_arms_the_one_left(self):
        self.take("B")
        self.switch("B", "C")
        self.clock.now = 1000.99
        self.assertEqual(self.o.tick(), [])
        self.clock.now = 1001.0
        actions = self.o.tick()
        self.assertEqual(focuses(actions, "B")[0]["reach"], ["D"])
        self.assertIn(Unreachable("C", "no_answer"), actions)
        self.assertEqual(focuses(actions, "C"), [])

    def test_a_late_accept_is_let_go_naming_where_the_input_is_now(self):
        # A hand-over given up closes its link, so what is still let go late is a take from home
        # or from the owner's own shortcut, on a link that stayed up.
        self.take("B")
        self.o.go("C")          # route 2, eager: B let go, C taken
        self.o.go("D")          # route 3, before C answered: C let go, D taken
        self.assertEqual(sent(self.o.accept("C", {"route": 2})),
                         [("C", {"type": "focus", "data": {"route": 3, "target": "D"}})])
        self.assertEqual(self.o.on, "D")

    def test_a_late_accept_on_a_link_closed_for_it_is_ignored(self):
        # Read before the link closed, and acted on after: the close already ended that ownership.
        self.take("B")
        self.switch("B", "C")
        self.clock.now = 1001.0
        self.assertIn(Drop("C"), self.o.tick())
        self.assertEqual(self.o.accept("C", {"route": 2}), [])
        self.assertEqual(self.o.on, "B")

    def test_a_late_accept_after_coming_home_names_the_owner(self):
        self.o.go("C")
        self.clock.now = 1001.0
        self.o.tick()           # home, route 2
        self.assertEqual(sent(self.o.accept("C", {"route": 1})),
                         [("C", {"type": "focus", "data": {"route": 2, "target": "A"}})])

    def test_an_abandoned_hand_over_closes_the_next_machines_link_before_the_input_goes_anywhere(self):
        # Its `accept` may still be on the way: closing the link is what ends an ownership nobody
        # will use, however late that accept is or however long the owner remembers the take.
        abandon = {
            "no answer": lambda: (setattr(self.clock, "now", 1001.0), self.o.tick())[1],
            "the shortcut home": lambda: self.o.go(None),
            "the shortcut to the machine it is on": lambda: self.o.go("B"),
            "the shortcut to a third machine": lambda: self.o.go("D"),
            "a send-home from the machine left": lambda: self.switch("B", "A", route=1),
            "the link to the machine left lost": lambda: self.o.link_down("B"),
        }
        for how, event in abandon.items():
            with self.subTest(how=how):
                self.setUp()
                self.take("B")
                self.switch("B", "C")
                actions = event()
                self.assertEqual([a for a in actions if isinstance(a, Drop)], [Drop("C")])
                self.assertIsInstance(actions[0], Drop)
                self.assertNotIn("C", self.o.reach_for(self.o.on))

    def test_a_hand_over_answered_or_already_gone_closes_nothing(self):
        answered = {
            "refused": lambda: self.o.refuse("C", {"route": 2, "why": "owned"}),
            "accepted": lambda: self.o.accept("C", {"route": 2}),
            "its link lost": lambda: self.o.link_down("C"),
            "taken again from here, on the same link": lambda: self.o.go("C"),
        }
        for how, event in answered.items():
            with self.subTest(how=how):
                self.setUp()
                self.take("B")
                self.switch("B", "C")
                self.assertEqual([a for a in event() if isinstance(a, Drop)], [])

    def test_a_take_from_home_with_no_answer_is_let_go_on_its_own_link_and_closes_nothing(self):
        self.o.go("C")
        self.clock.now = 1001.0
        actions = self.o.tick()
        self.assertEqual([a for a in actions if isinstance(a, Drop)], [])
        self.assertEqual(focuses(actions, "C"), [{"route": 2, "target": "A"}])

    def test_an_accept_for_a_route_never_taken_there_is_ignored(self):
        self.take("B")
        self.switch("B", "C")
        self.assertEqual(self.o.accept("C", {"route": 99}), [])
        self.assertEqual(self.o.accept("D", {"route": 2}), [])
        self.assertEqual(self.o.accept("C", {"route": 1}), [])
        self.assertEqual(self.o.on, "B")

    def test_a_send_home_while_waiting_ends_the_wait_and_no_stay_follows(self):
        self.take("B")
        self.switch("B", "C")
        self.assertEqual(moved(self.switch("B", "A", route=1)).to, None)
        self.clock.now += 5
        self.assertEqual(self.o.tick(), [])

    def test_losing_the_link_to_the_machine_left_while_waiting_brings_the_input_home(self):
        self.take("B")
        self.switch("B", "C")
        actions = self.o.link_down("B")
        self.assertEqual(sent(actions), [])
        self.assertEqual(moved(actions), Moved(None, "B", "link_lost"))
        self.clock.now += 5
        self.assertEqual(self.o.tick(), [])

    def test_the_shortcut_while_waiting_ends_the_wait(self):
        self.take("B")
        self.switch("B", "C")
        actions = self.o.go("D")
        self.assertEqual(focuses(actions), [{"route": 3, "target": "D"},
                                            {"route": 3, "target": "D", "resistance_px": 120, "reach": ["B"]}])
        self.assertEqual(sent(actions)[0][0], "B")
        self.o.accept("D", {"route": 3})
        self.clock.now = 1001.0
        self.assertEqual(self.o.tick(), [])
        self.assertEqual(self.o.on, "D")

    def test_the_shortcut_to_the_machine_waited_on_from_re_arms_it(self):
        self.take("B")
        self.switch("B", "C")
        self.o.input(MOVE)
        actions = self.o.go("B")
        self.assertEqual(sent(actions), [
            ("B", {"type": "focus", "data": {"route": 3, "target": "B", "stay": True, "resistance_px": 120,
                                             "reach": ["D"]}}),
            ("B", MOVE),
        ])
        self.clock.now = 1001.0
        self.assertEqual(self.o.tick(), [])

    def test_losing_the_link_to_the_next_machine_while_waiting_is_a_refusal(self):
        self.take("B")
        self.switch("B", "C")
        actions = self.o.link_down("C")
        self.assertEqual(focuses(actions, "B")[0]["stay"], True)
        self.assertIn(Unreachable("C", "link_lost"), actions)

    def test_a_switch_to_a_peer_outside_the_reach_last_sent_is_unreachable_even_with_a_link(self):
        self.take("B")
        self.switch("B", "C")
        self.o.refuse("C", {"route": 2, "why": "owned"})     # C left out; the stay is route 3
        self.assertNotIn("C", self.o.reach_sent)
        actions = self.switch("B", "C")
        self.assertEqual(focuses(actions, "C"), [])
        self.assertEqual(focuses(actions, "B")[0]["route"], 4)
        self.assertIn(Unreachable("C", "unreachable"), actions)

    def test_a_switch_to_a_peer_whose_link_dropped_is_unreachable_without_asking_it(self):
        self.take("B")
        self.assertEqual(focuses(self.o.link_down("C"), "B")[0]["reach"], ["D"])
        actions = self.switch("B", "C")
        self.assertEqual(sent(actions, "C"), [])
        self.assertTrue(focuses(actions, "B")[0]["stay"])
        self.assertIn(Unreachable("C", "unreachable"), actions)

    def test_a_switch_without_a_landing_takes_without_one(self):
        self.take("B")
        self.assertNotIn("edge", focuses(self.switch("B", "C"), "C")[0])


class ComingHomeTests(OwnerCase):
    def test_every_trigger_brings_the_input_home_saying_why(self):
        triggers = {
            "asked": lambda: self.o.go(None),
            "switch": lambda: self.switch("B", "A"),
            "owned": lambda: self.o.refuse("B", {"route": self.o.route, "why": "owned"}),
            "link_lost": lambda: self.o.link_down("B"),
        }
        for why, trigger in triggers.items():
            with self.subTest(why=why):
                self.o.link_up("B", True)
                self.take("B")
                self.o.input(key("keydown", "shift"))
                actions = trigger()
                self.assertEqual(moved(actions), Moved(None, "B", why))
                if why == "link_lost":
                    self.assertEqual(sent(actions), [])
                else:
                    self.assertEqual(sent(actions), [("B", key("keyup", "shift")),
                                                     ("B", {"type": "focus", "data": {"route": self.o.route, "target": "A"}})])
                self.assertEqual(self.o.input(key("keyup", "shift")), [])
                self.assertEqual(self.o.input(key("keyup", "shift")), [LOCAL])
                self.clock.now += owner.LEFT_OUT

    def test_the_owner_releases_what_it_pressed_there_before_letting_go(self):
        self.take("B")
        self.o.input(key("keydown", "shift"))
        self.o.input(button("mousedown", "left"))
        self.o.input(key("keydown", "A", "a"))
        self.o.input(key("keyup", "A", "a"))
        actions = self.o.go("C")
        self.assertEqual(sent(actions, "B"), [
            ("B", key("keyup", "shift")),
            ("B", button("mouseup", "left")),
            ("B", {"type": "focus", "data": {"route": 2, "target": "C"}}),
        ])
        self.assertEqual(self.o.input(key("keyup", "shift")), [])
        self.assertEqual(self.o.input(button("mouseup", "left")), [])
        self.assertEqual(self.o.input(key("keyup", "shift")), [Send("C", key("keyup", "shift"))])

    def test_a_key_still_held_from_the_machine_left_repeats_nowhere(self):
        self.take("B")
        self.o.input(key("keydown", "a", "a"))
        self.o.go(None)
        self.assertEqual(self.o.input(key("keydown", "a", "a")), [])
        self.assertEqual(self.o.input(key("keyup", "a", "a")), [])
        self.assertEqual(self.o.input(key("keydown", "a", "a")), [LOCAL])

    def test_input_held_for_a_hand_over_that_ends_at_home_goes_nowhere(self):
        self.take("B")
        self.switch("B", "C")
        self.o.input(key("keydown", "x", "x"))
        actions = self.switch("B", "A", route=1)
        self.assertEqual(sent(actions, "C"), [])
        self.assertEqual([m for p, m in sent(actions, "B") if m["type"] != "focus"], [])
        self.assertEqual(self.o.input(key("keyup", "x", "x")), [])


class PressedHereTests(OwnerCase):
    def test_a_key_pressed_here_is_released_here_after_the_input_leaves(self):
        self.assertEqual(self.o.input(key("keydown", "ctrl")), [LOCAL])
        self.take("B")
        self.assertEqual(self.o.input(key("keydown", "ctrl")), [])
        self.assertEqual(self.o.input(key("keyup", "ctrl")), [LOCAL])
        self.assertEqual(self.o.input(key("keydown", "ctrl")), [Send("B", key("keydown", "ctrl"))])

    def test_a_key_pressed_here_is_released_here_during_a_hand_over(self):
        self.o.input(key("keydown", "ctrl"))
        self.take("B")
        self.switch("B", "C")
        self.assertEqual(self.o.input(key("keyup", "ctrl")), [LOCAL])

    def test_a_button_pressed_here_is_released_here(self):
        self.o.input(button("mousedown"))
        self.take("B")
        self.assertEqual(self.o.input(button("mouseup")), [LOCAL])


class LinkTests(OwnerCase):
    def test_a_second_link_from_the_peer_the_input_is_on_ends_the_visit(self):
        self.take("B")
        self.assertEqual(moved(self.o.link_up("B", True)), Moved(None, "B", "link_lost"))
        self.assertIn("B", self.o.reach_for("C"))

    def test_a_refusal_of_the_take_counts_while_a_re_arm_is_crossing_it(self):
        self.o.go("C")
        self.o.link_up("E", True)       # a stay, route 2, before C answers the take
        self.assertEqual(moved(self.o.refuse("C", {"route": 1, "why": "owned"})), Moved(None, "C", "owned"))

    def test_a_take_is_forgotten_after_a_while(self):
        self.o.go("B")
        self.clock.now = 1001.0
        self.o.tick()
        self.clock.now = 1040.0
        self.take("C")
        self.assertEqual(self.o.accept("B", {"route": 1}), [])


class ClipboardTests(OwnerCase):
    def test_a_refused_take_does_not_count_as_having_the_clipboard(self):
        self.o.go("B")
        self.o.refuse("B", {"route": 1, "why": "owned"})
        self.clock.now += owner.LEFT_OUT
        self.assertIn(SendClipboard("B"), self.take("B"))

    def test_the_machine_left_keeps_its_clipboard_when_the_next_one_refuses_first(self):
        self.take("B")
        self.o.go("C")
        self.o.refuse("C", {"route": 2, "why": "owned"})
        message = {"type": "clipboard", "data": {"text": "copied on B"}}
        self.assertEqual(self.o.clipboard_arrived("B", 1000.25, message), [SetClipboard(message)])

    def test_a_machine_whose_link_was_lost_is_sent_the_clipboard_again(self):
        # Its clipboard may still have been on the way, or waiting behind a slow read, when the link went.
        self.take("B")
        self.o.link_down("B")
        self.o.link_up("B", True)
        self.assertIn(SendClipboard("B"), self.take("B"))

    def test_the_clipboard_goes_after_the_taking_focus_and_before_any_input(self):
        actions = self.o.go("B")
        focus_at = next(i for i, a in enumerate(actions) if isinstance(a, Send))
        self.assertEqual(actions.index(SendClipboard("B")), focus_at + 1)

    def test_the_clipboard_goes_only_where_it_is_not_already(self):
        self.take("B")
        self.o.go(None)
        self.assertNotIn(SendClipboard("B"), self.take("B"))
        self.o.go(None)
        self.o.clipboard_changed()
        self.assertIn(SendClipboard("B"), self.take("B"))

    def test_the_machine_let_go_may_send_its_clipboard_once_within_ten_seconds(self):
        self.take("B")
        self.o.go(None)
        message = {"type": "clipboard", "data": {"text": "copied on B"}}
        self.assertEqual(self.o.clipboard_arrived("C", 1000.5, message), [])
        self.assertEqual(self.o.clipboard_arrived("B", 1010.0, message), [SetClipboard(message)])
        self.assertEqual(self.o.clipboard_arrived("B", 1010.0, message), [])

    def test_only_a_machine_just_let_go_is_expected_to_send_a_clipboard(self):
        # What a link asks before it reads a frame too large for anything but a clipboard.
        self.assertFalse(self.o.expects_clipboard("B", 1000.0))
        self.take("B")
        self.assertFalse(self.o.expects_clipboard("B", 1000.0))
        self.o.go(None)
        self.assertTrue(self.o.expects_clipboard("B", 1000.5))
        self.assertFalse(self.o.expects_clipboard("C", 1000.5))
        # By when the frame began, as clipboard_arrived judges it: a large one on a slow link that
        # began in time and finished a minute later is still the clipboard it would take.
        self.clock.now += 60
        self.assertTrue(self.o.expects_clipboard("B", 1010.0))
        self.assertFalse(self.o.expects_clipboard("B", 1010.01))

    def test_a_machine_whose_clipboard_came_is_not_expected_again(self):
        self.take("B")
        self.o.go(None)
        self.o.clipboard_arrived("B", 1000.5, {"type": "clipboard", "data": {"text": "x"}})
        self.assertFalse(self.o.expects_clipboard("B", 1000.6))

    def test_a_clipboard_beginning_more_than_ten_seconds_after_the_let_go_is_dropped(self):
        self.take("B")
        self.o.go(None)
        self.assertEqual(self.o.clipboard_arrived("B", 1010.01, {"type": "clipboard", "data": {"text": "x"}}), [])

    def test_in_a_chain_the_clipboard_of_the_machine_left_goes_on_to_the_next(self):
        self.take("B")
        self.switch("B", "C")
        self.o.accept("C", {"route": 2})
        message = {"type": "clipboard", "data": {"text": "copied on B"}}
        self.assertEqual(self.o.clipboard_arrived("B", 1000.1, message), [SetClipboard(message), Send("C", message)])

    def test_a_clipboard_brought_home_goes_to_the_next_machine_taken_and_not_back(self):
        self.take("B")
        self.take("C")
        self.o.go(None)
        self.o.clipboard_arrived("C", 1000.1, {"type": "clipboard", "data": {"text": "copied on C"}})
        self.assertIn(SendClipboard("B"), self.take("B"))
        self.o.go(None)
        self.assertNotIn(SendClipboard("C"), self.take("C"))


class RouteTests(OwnerCase):
    def test_routes_only_rise_and_each_hand_over_shares_one(self):
        self.take("B")
        seen = [1]
        ring = ["B", "C", "D"]
        for n in range(1000):
            here, there = ring[n % 3], ring[(n + 1) % 3]
            if n % 2:
                actions = self.switch(here, there)
                taking = focuses(actions, there)
                actions = self.o.accept(there, {"route": taking[0]["route"]})
                letting_go = focuses(actions, here)
            else:
                actions = self.o.go(there)
                taking, letting_go = focuses(actions, there), focuses(actions, here)
                self.o.accept(there, {"route": taking[0]["route"]})
            self.assertEqual(letting_go[0]["target"], there)
            self.assertEqual(taking[0]["route"], letting_go[0]["route"])
            self.assertGreater(taking[0]["route"], seen[-1])
            seen.append(taking[0]["route"])
            self.assertEqual(self.o.on, there)
        self.assertEqual(self.o.route, seen[-1])


if __name__ == "__main__":
    unittest.main()
