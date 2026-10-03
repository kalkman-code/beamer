"""This PC's input going to its peers (WIRE.md sections 3 to 5): the zones, the owner's decisions
carried out, the key names, the clipboard and the way home, against links that only record."""

import queue
import threading
import time
import unittest

from links_rig import B, C, CAPS, HERE, Rig, edge_zone, make_config

import capture_win
import sender
from core import owner as owner_module
from core import protocol
from core.return_edge import Rect
from core.tests import pngs
from core.tests import responder_harness as harness


def target_of(message):
    return protocol.read_id(message["data"]["target"])


class DeadKeyRoutingTests(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.sender = self.rig.sender

    def test_a_dead_key_is_swallowed_only_while_its_peer_is_live(self):
        self.sender.set_redirecting(True)
        sent = list(self.rig.sent(B))
        self.assertTrue(self.sender.on_dead_key())
        self.assertEqual(self.rig.sent(B), sent)

        self.rig.links.up_set.discard(B)
        self.assertFalse(self.sender.on_dead_key())
        self.assertFalse(self.sender.redirecting)


class EdgeTests(unittest.TestCase):
    """The outward crossing: the zone's model, the gate and the pin, which is everything the hook
    thread does per mouse move."""

    def setUp(self):
        self.rig = Rig()
        self.sender = self.rig.sender

    def test_a_push_against_the_edge_takes_the_peer_with_the_edge_and_offset(self):
        self.rig.push(4)
        self.assertTrue(self.sender.redirecting)
        focus = self.rig.sent(B, "focus")[0]["data"]
        self.assertEqual(target_of(focus and {"data": focus}), B)
        self.assertEqual(focus["edge"], "right")
        self.assertAlmostEqual(focus["offset"], 500 / 1080, places=3)
        self.assertEqual(focus["resistance_px"], 40)
        self.assertEqual(focus["reach"], [])

    def test_the_push_lights_the_edge_it_is_pressing(self):
        self.rig.push(4)
        self.assertTrue(self.rig.pressure)
        self.assertTrue(all(edge == "left" for edge, _p, _c, _part in self.rig.pressure))
        self.assertTrue(self.rig.pressure[-1][2])

    def test_part_of_the_edge_lights_only_the_third_it_is_pushing(self):
        rig = Rig(cursor=(0, 900), zones=[edge_zone(B, kind="part", parts=["middle", "end"])])
        rig.push(4)
        self.assertEqual({(edge, part) for edge, _p, _c, part in rig.pressure}, {("left", "end")})

    def test_the_third_is_measured_on_the_pointers_own_display(self):
        # The left display runs 200 to 1280 of a 1440-tall desktop: 1000 is its end third, though
        # on the desktop as a whole it would be the middle one.
        rig = Rig(cursor=(0, 1000), monitors=[Rect(0, 200, 1920, 1080), Rect(1920, 0, 2560, 1440)],
                  zones=[edge_zone(B, kind="part", parts=["end"])])
        rig.push(4)
        self.assertEqual({part for _e, _p, _c, part in rig.pressure}, {"end"})

    def test_the_whole_edge_names_no_third(self):
        self.rig.push(4)
        self.assertEqual({part for _e, _p, _c, part in self.rig.pressure}, {None})

    def test_a_push_away_from_the_edge_never_crosses(self):
        self.rig.push(10, dx=20)
        self.assertFalse(self.sender.redirecting)

    def test_a_pointer_off_the_edge_never_crosses(self):
        self.rig.desktop.cursor = (900, 500)
        self.rig.push(10)
        self.assertFalse(self.sender.redirecting)

    def test_a_zone_that_is_off_never_crosses(self):
        rig = Rig(zones=[edge_zone(B, off=True)])
        rig.push(10)
        self.assertFalse(rig.sender.redirecting)

    def test_a_peer_that_does_not_accept_input_is_not_a_zone_to_cross_into(self):
        rig = Rig(up=())
        rig.bring_up(B, accepts=False)
        rig.push(10)
        self.assertFalse(rig.sender.redirecting)

    def test_a_zone_leads_to_its_own_peer(self):
        rig = Rig(
            entries=[harness.entry(B, "Mac", side="left"), harness.entry(C, "Other", side="top")],
            zones=[edge_zone(B), edge_zone(C)], up=(B, C), cursor=(900, 0),
        )
        rig.push(4, dx=0, dy=-20)
        self.assertEqual(rig.sender.owner, C)
        self.assertEqual(rig.sent(B, "focus"), [])

    def test_the_shortcut_can_be_turned_off_on_its_own(self):
        self.sender.update_config(make_config(crossing_methods=["edge"]))
        self.assertFalse(self.sender.shortcut_armed)
        self.sender.update_config(make_config(crossing_methods=["edge", "shortcut"]))
        self.assertTrue(self.sender.shortcut_armed)

    def test_a_held_edge_does_not_cross(self):
        self.sender.crossing_paused = True
        self.rig.push(10)
        self.assertFalse(self.sender.redirecting)

    def test_a_drag_to_the_edge_is_not_a_crossing(self):
        self.sender.on_button("left", True)
        self.rig.push(10)
        self.assertFalse(self.sender.redirecting)


class CornerTests(unittest.TestCase):
    """The corner is for a machine whose whole edge is busy: it wants a diagonal push in an
    8-pixel box, and nothing else."""

    def setUp(self):
        zone = {"peer": harness.protocol.id_text(B), "kind": "corner", "corner": "top_left", "edge": "left"}
        self.rig = Rig(cursor=(0, 0), zones=[zone])

    def test_a_diagonal_push_in_the_corner_crosses_at_the_end_nearest_it(self):
        self.rig.push(4, -20, -20)
        self.assertTrue(self.rig.sender.redirecting)
        focus = self.rig.sent(B, "focus")[0]["data"]
        self.assertEqual((focus["edge"], focus["offset"]), ("right", 0.0))

    def test_the_push_names_its_corner_for_the_effects(self):
        self.rig.push(4, -20, -20)
        self.assertEqual({part for _e, _p, _c, part in self.rig.pressure}, {"top_left"})

    def test_a_straight_push_along_the_edge_does_not(self):
        self.rig.push(10, -20, 0)
        self.assertFalse(self.rig.sender.redirecting)

    def test_the_same_diagonal_away_from_the_corner_does_not(self):
        self.rig.desktop.cursor = (900, 500)
        self.rig.push(10, -20, -20)
        self.assertFalse(self.rig.sender.redirecting)


class TakingTests(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.sender = self.rig.sender

    def test_the_shortcut_takes_the_first_peer_without_a_position(self):
        self.assertTrue(self.sender.set_redirecting(True))
        focus = self.rig.sent(B, "focus")[0]["data"]
        self.assertNotIn("edge", focus)
        self.assertEqual(focus["route"], 1)
        self.assertEqual(self.rig.redirects, [True])

    def test_the_clipboard_follows_the_take_and_goes_before_any_input(self):
        self.sender.set_redirecting(True)
        self.sender.on_key("a", True, 0x41, "a")
        kinds = [message["type"] for message in self.rig.sent(B)]
        self.assertEqual(kinds, ["focus", "clipboard", "keydown"])
        self.assertEqual(self.rig.sent(B, "clipboard")[0]["data"]["text"], "here")

    def test_noncomposable_dead_key_sequence_is_sent_as_text(self):
        self.sender.set_redirecting(True)
        self.assertTrue(self.sender.on_text("§a", 0x41))
        self.assertEqual(self.rig.sent(B, protocol.MSG_TEXT)[0]["data"]["text"], "§a")

    def test_noncomposable_dead_key_text_releases_and_restores_remote_modifiers(self):
        self.sender.set_redirecting(True)
        self.rig.flush()
        self.rig.links.sent.clear()
        self.assertTrue(self.sender.on_key("shift", True, 0x10))

        self.assertTrue(self.sender.on_text("§A", 0x41))

        self.assertEqual(
            [message["type"] for message in self.rig.sent(B)],
            [protocol.MSG_KEYDOWN, protocol.MSG_KEYUP, protocol.MSG_TEXT, protocol.MSG_KEYDOWN],
        )

    def test_temporary_text_release_keeps_the_held_modifier_wire_name_if_style_changes(self):
        self.sender.set_redirecting(True)
        self.rig.accept_take()
        self.sender.on_key("cmd", True, 0xA2)
        self.rig.flush()
        original = self.rig.sent(B, protocol.MSG_KEYDOWN)[0]["data"]["key"]
        physical = self.sender._keys_down[0xA2]

        released = self.sender._key_message(B, {"type": protocol.MSG_KEYUP, "data": dict(physical)}, preserve=True)
        self.sender.update_config(make_config(modifier_style="positional"))
        restored = self.sender._key_message(B, {"type": protocol.MSG_KEYDOWN, "data": dict(physical)})

        self.assertEqual((released["data"]["key"], restored["data"]["key"]), (original, original))

    def test_an_image_too_large_to_decode_is_not_sent_but_the_text_is(self):
        for image, sent in ((pngs.png(), True), (pngs.png(16384, 16384), False)):
            with self.subTest(sent=sent):
                rig = Rig()
                rig.clipboard.image = image
                rig.sender.set_redirecting(True)
                data = rig.sent(B, "clipboard")[0]["data"]
                self.assertEqual(data["text"], "here")
                self.assertEqual("image" in data, sent)

    def test_a_peer_without_the_clipboard_capability_is_sent_none(self):
        self.rig.links.caps_of[B] = frozenset()
        self.sender.set_redirecting(True)
        self.assertEqual(self.rig.sent(B, "clipboard"), [])

    def test_the_clipboard_is_not_sent_again_when_nothing_changed_here(self):
        self.sender.set_redirecting(True)
        self.rig.accept_take()
        self.sender.set_redirecting(False)
        self.sender.set_redirecting(True)
        self.rig.accept_take()
        self.assertEqual(len(self.rig.sent(B, "clipboard")), 1)
        self.rig.clipboard.text = "copied"
        self.rig.clipboard.stamp += 1
        self.sender.set_redirecting(False)
        self.sender.set_redirecting(True)
        self.assertEqual([m["data"]["text"] for m in self.rig.sent(B, "clipboard")], ["here", "copied"])

    def test_the_shortcut_with_no_link_up_says_so_and_stays(self):
        rig = Rig(up=())
        self.assertFalse(rig.sender.set_redirecting(True))
        self.assertFalse(rig.sender.redirecting)
        self.assertTrue(rig.alerts[-1].startswith("Cannot switch"))

    def test_a_refusal_brings_input_home_and_says_who_refused(self):
        self.sender.set_redirecting(True)
        self.rig.inbound(B, protocol.refuse_msg(1, "busy"))
        self.assertFalse(self.sender.redirecting)
        self.assertEqual(self.rig.alerts, ["Can't cross to Mac: it is driving another machine."])
        self.assertEqual(self.rig.redirects, [True, False])
        self.assertEqual(self.rig.arrivals, [])

    def test_a_refusal_names_the_machine_this_pcs_input_is_on(self):
        rig = Rig(entries=[harness.entry(B, "Mac", side="left"), harness.entry(C, "Rig", side="top")])
        rig.sender.set_redirecting(True)
        rig.inbound(B, protocol.refuse_msg(1, "busy", other=C))
        self.assertEqual(rig.alerts, ["Can't cross to Mac: it is driving Rig."])

    def test_the_shortcut_home_reports_where_the_pointer_is_and_lets_go(self):
        self.sender.set_redirecting(True)
        self.sender.set_redirecting(False)
        self.assertEqual(self.rig.arrivals, [(None, 0, 500)])
        targets = [target_of(m) for m in self.rig.sent(B, "focus")]
        self.assertEqual(targets, [B, HERE])
        self.assertEqual(self.rig.alerts, [])

    def test_no_answer_within_a_second_brings_input_home(self):
        clock = [1000.0]
        rig = Rig()
        rig.sender._clock = rig.sender._owner._clock = lambda: clock[0]
        rig.sender.set_redirecting(True)
        clock[0] += 1.1
        rig.sender.tick()
        self.assertFalse(rig.sender.redirecting)
        self.assertEqual(rig.alerts, ["Mac did not answer"])

    def test_the_link_dropping_brings_input_home(self):
        self.sender.set_redirecting(True)
        self.rig.drop(B)
        self.assertFalse(self.sender.redirecting)
        self.assertEqual(self.rig.alerts, ["Lost the link to Mac"])

    def test_a_link_gone_quiet_is_noticed_by_the_next_keystroke(self):
        self.sender.set_redirecting(True)
        self.rig.links.up_set.discard(B)
        self.assertFalse(self.sender.on_key("a", True, 0x41, "a"))
        self.assertFalse(self.sender.redirecting)
        self.assertIn(B, self.rig.links.closed)

    def test_a_send_that_fails_brings_input_home(self):
        self.sender.set_redirecting(True)
        self.rig.links.fail_input = True
        self.sender.on_key("a", True, 0x41, "a")
        self.rig.flush()
        self.assertFalse(self.sender.redirecting)

    def test_a_driven_machine_sends_its_driver_home_before_it_drives(self):
        self.rig.driven = True
        self.assertTrue(self.sender.set_redirecting(True))
        self.assertEqual(self.rig.sent_home, [True])
        self.assertFalse(self.sender.redirecting)
        self.rig.driven = False
        self.sender._stop_event.wait(0.3)
        self.assertTrue(self.sender.redirecting)

    def test_a_push_while_driven_sends_the_driver_home_and_then_crosses(self):
        self.rig.driven = True
        self.rig.push(4)
        self.assertEqual(self.rig.sent_home, [True])
        self.rig.driven = False
        self.sender._stop_event.wait(0.3)
        self.assertTrue(self.sender.redirecting)
        self.assertEqual(self.rig.sent(B, "focus")[0]["data"]["edge"], "right")

    def test_a_take_that_lost_the_race_to_a_peers_take_stays_home(self):
        # Away is set before `driven` is read: when the peer's take was decided in between, both
        # machines go home.
        self.sender.driven = lambda: self.sender.redirecting
        self.assertFalse(self.sender._go(B))
        self.assertFalse(self.sender.redirecting)

    def test_a_peer_taking_this_pc_while_input_is_away_brings_it_home(self):
        self.sender.set_redirecting(True)
        self.sender.set_receiving(True)
        self.assertFalse(self.sender.redirecting)


class NamingTests(unittest.TestCase):
    def test_two_machines_of_one_name_are_told_apart_in_what_the_sender_says(self):
        rig = Rig(entries=[harness.entry(B, "Studio", side="left"), harness.entry(C, "Studio")], zones=[edge_zone(B)], up=())
        first, second = rig.sender._name(B), rig.sender._name(C)
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("Studio (") and second.startswith("Studio ("))

    def test_a_machine_nobody_knows_is_the_other_machine(self):
        rig = Rig(up=())
        self.assertEqual(rig.sender._name(C), "The other machine")


class OnwardTests(unittest.TestCase):
    """A hand-over from the machine this PC's input is on (section 5)."""

    def setUp(self):
        self.rig = Rig(
            entries=[harness.entry(B, "Mac", side="left"), harness.entry(C, "Other", side="top")],
            zones=[edge_zone(B), edge_zone(C)], up=(B, C),
        )
        self.sender = self.rig.sender
        self.sender.set_redirecting(True)
        self.rig.accept_take(B)

    def switch(self, to, route=None, **where):
        route = self.sender._owner.route if route is None else route
        self.rig.inbound(B, protocol.switch_v6(route, to, **where))

    def test_the_reach_names_the_peers_this_pc_can_follow_to(self):
        focus = self.rig.sent(B, "focus")[0]["data"]
        self.assertEqual(focus["reach"], [protocol.id_text(C)])

    def test_a_switch_to_another_peer_takes_it_then_lets_the_first_go(self):
        self.switch(C, edge="right", offset=0.25)
        takes = self.rig.sent(C, "focus")
        self.assertEqual((takes[0]["data"]["edge"], takes[0]["data"]["offset"]), ("right", 0.25))
        self.assertEqual(self.sender.owner, B)
        self.rig.inbound(C, protocol.accept_msg(self.sender._owner.route))
        self.assertEqual(self.sender.owner, C)
        lets_go = [target_of(m) for m in self.rig.sent(B, "focus")]
        self.assertEqual(lets_go[-1], C)

    def test_input_typed_while_a_peer_is_asked_waits_and_goes_to_the_new_machine(self):
        self.switch(C)
        self.sender.on_key("a", True, 0x41, "a")
        self.assertEqual([m["type"] for m in self.rig.sent(C) if m["type"] != "focus"], [])
        self.rig.inbound(C, protocol.accept_msg(self.sender._owner.route))
        self.assertEqual([m["data"]["key"] for m in self.rig.sent(C, "keydown")], ["a"])

    def test_text_during_chained_handover_brackets_a_modifier_waiting_to_be_replayed(self):
        self.switch(C)
        self.assertTrue(self.sender.on_key("shift", True, 0x10))
        self.assertTrue(self.sender.on_text("A", 0x41))

        self.rig.inbound(C, protocol.accept_msg(self.sender._owner.route))

        self.assertEqual(
            [message["type"] for message in self.rig.sent(C) if message["type"] in protocol.INPUT_TYPES],
            [protocol.MSG_KEYDOWN, protocol.MSG_KEYUP, protocol.MSG_TEXT, protocol.MSG_KEYDOWN],
        )

    def test_text_replayed_after_a_refused_handover_brackets_the_current_machines_modifier(self):
        self.sender.on_key("shift", True, 0x10)
        self.rig.flush()
        self.switch(C)
        self.assertTrue(self.sender.on_text("^x", 0x58))

        self.rig.inbound(C, protocol.refuse_msg(self.sender._owner.route, "owned"))

        self.assertEqual(
            [message["type"] for message in self.rig.sent(B) if message["type"] in protocol.INPUT_TYPES],
            [protocol.MSG_KEYDOWN, protocol.MSG_KEYUP, protocol.MSG_TEXT, protocol.MSG_KEYDOWN],
        )

    def test_text_replay_keeps_a_modifier_bracket_when_the_key_repeats_during_handover(self):
        self.sender.on_key("shift", True, 0x10)
        self.rig.flush()
        self.switch(C)
        self.assertTrue(self.sender.on_text("^x", 0x58))
        self.sender.on_key("shift", True, 0x10)

        self.rig.inbound(C, protocol.refuse_msg(self.sender._owner.route, "owned"))

        self.assertEqual(
            [message["type"] for message in self.rig.sent(B) if message["type"] in protocol.INPUT_TYPES],
            [protocol.MSG_KEYDOWN, protocol.MSG_KEYUP, protocol.MSG_TEXT, protocol.MSG_KEYDOWN, protocol.MSG_KEYDOWN],
        )

    def test_a_switch_home_lands_the_pointer_and_reports_the_arrival(self):
        self.switch(HERE, edge="left", offset=0.5)
        self.assertFalse(self.sender.redirecting)
        self.assertEqual(self.rig.desktop.placed, [(0, 540)])
        self.assertEqual(self.rig.arrivals, [("left", 0, 540)])

    def test_the_log_names_the_edge_input_lands_at_both_ways(self):
        with self.assertLogs(sender.LOGGER, "INFO") as said:
            self.switch(C, edge="right", offset=0.25)
            self.rig.inbound(C, protocol.accept_msg(self.sender._owner.route))
            self.rig.inbound(C, protocol.switch_v6(self.sender._owner.route, HERE, edge="left", offset=0.5))
        self.assertTrue(any("input is on Other, at its right edge" in line for line in said.output), said.output)
        self.assertTrue(any("input returned to this PC (switch), at its left edge" in line for line in said.output), said.output)

    def test_a_switch_home_with_no_position_reports_where_the_pointer_is(self):
        self.switch(HERE)
        self.assertEqual(self.rig.arrivals, [(None, 0, 500)])

    def test_the_peer_sending_the_input_back_by_its_edge_is_not_an_alert(self):
        self.switch(HERE, edge="left", offset=0.5)
        self.assertEqual(self.rig.alerts, [])

    def test_a_raising_arrival_callback_only_logs(self):
        def boom(edge, x, y):
            raise RuntimeError("effect fell over")

        self.sender._arrival_callback = boom
        with self.assertLogs(sender.LOGGER, "ERROR"):
            self.switch(HERE, edge="left", offset=0.5)
        self.assertTrue(self.rig.desktop.placed)

    def test_a_hand_over_given_up_before_its_answer_closes_that_peers_link(self):
        # Its accept may still be on the way: the close is what ends that ownership (WIRE.md
        # section 5). Abandoned by the shortcut home here; the owner's tests cover every way.
        self.switch(C)
        self.assertEqual(self.rig.links.closed, [])
        self.sender.set_redirecting(False)
        self.rig.flush()
        self.assertEqual(self.rig.links.closed, [C])
        self.assertFalse(self.sender.redirecting)

    def test_a_hand_over_given_up_when_the_outbound_queue_is_full_still_closes_that_peers_link(self):
        # The close cannot wait in a queue that has no room: C would stay owned by nobody driving.
        self.switch(C)
        self.sender._outbound = queue.Queue(maxsize=1)
        self.sender._outbound.put_nowait(owner_module.Send(B, protocol.ping_msg()))
        self.sender.set_redirecting(False)
        self.assertIn(C, self.rig.links.closed)

    def test_a_peer_that_cannot_be_reached_leaves_the_input_where_it_is(self):
        self.switch(C)
        self.rig.inbound(C, protocol.refuse_msg(self.sender._owner.route, "owned"))
        self.assertEqual(self.sender.owner, B)
        self.assertEqual(self.rig.alerts[-1], "Can't cross to Other: another machine is driving it.")
        # And the machine in the way, named when the refusal says which one.
        self.sender._owner._left_out.clear()
        self.rig.drop(C)
        self.rig.bring_up(C)
        self.switch(C)
        self.rig.inbound(C, protocol.refuse_msg(self.sender._owner.route, "owned", other=B))
        self.assertEqual(self.rig.alerts[-1], "Can't cross to Other: it is being driven from Mac.")


class KeyTests(unittest.TestCase):
    """Keys leave as the key table names them for the peer's platform (WIRE.md section 7)."""

    def setUp(self):
        self.rig = Rig()
        self.sender = self.rig.sender
        self.sender.set_redirecting(True)
        self.rig.accept_take()

    def keys(self):
        return [(m["type"], m["data"]["key"]) for m in self.rig.sent(B) if m["type"] in ("keydown", "keyup")]

    def test_ctrl_goes_to_a_mac_as_cmd_in_the_semantic_style(self):
        # The capture calls the physical Ctrl "cmd".
        self.sender.on_key("cmd", True, 0xA2)
        self.sender.on_key("c", True, 0x43, "c")
        self.sender.on_key("c", False, 0x43, "c")
        self.sender.on_key("cmd", False, 0xA2)
        self.assertEqual(self.keys(), [("keydown", "cmd"), ("keydown", "c"), ("keyup", "c"), ("keyup", "cmd")])

    def test_the_windows_key_goes_to_a_mac_as_ctrl_in_the_semantic_style(self):
        self.sender.on_key("ctrl", True, 0x5B)
        self.assertEqual(self.keys(), [("keydown", "ctrl")])

    def test_positional_keeps_each_key_in_its_place(self):
        self.sender.update_config(make_config(modifier_style="positional"))
        self.sender.on_key("cmd", True, 0xA2)
        self.sender.on_key("ctrl", True, 0x5B)
        self.assertEqual(self.keys(), [("keydown", "ctrl"), ("keydown", "cmd")])

    def test_mac_layout_sends_both_sides_of_each_modifier_to_the_mac_key(self):
        self.sender.update_config(make_config(modifier_style="mac_layout"))
        for name, vk in (("cmd", 0xA2), ("cmd_r", 0xA3), ("ctrl", 0x5B), ("ctrl_r", 0x5C),
                         ("alt", 0xA4), ("alt_r", 0xA5)):
            self.sender.on_key(name, True, vk)
            self.sender.on_key(name, False, vk)
        self.assertEqual(
            self.keys(),
            [(kind, key) for key in ("ctrl", "ctrl_r", "alt", "alt_r", "cmd", "cmd_r")
             for kind in ("keydown", "keyup")],
        )

    def test_to_another_pc_every_key_goes_as_itself(self):
        rig = Rig(entries=[harness.entry(B, "Other PC", platform="windows", side="left")])
        rig.sender.set_redirecting(True)
        rig.accept_take()
        rig.sender.on_key("cmd", True, 0xA2)
        rig.sender.on_key("ctrl", True, 0x5B)
        self.assertEqual([m["data"]["key"] for m in rig.sent(B, "keydown")], ["ctrl", "cmd"])

    def test_a_peer_of_no_known_platform_is_a_pc_and_gets_every_key_as_itself(self):
        # WIRE.md section 7: the PC family is Windows, Linux, Android and any unknown platform.
        unknown = harness.entry(B, "Something", side="left")
        del unknown["platform"]
        rig = Rig(entries=[unknown])
        rig.sender.set_redirecting(True)
        rig.accept_take()
        rig.sender.on_key("cmd", True, 0xA2)
        rig.sender.on_key("ctrl", True, 0x5B)
        self.assertEqual([m["data"]["key"] for m in rig.sent(B, "keydown")], ["ctrl", "cmd"])

    def test_a_release_repeats_the_name_its_press_went_under(self):
        self.sender.on_key("A", True, 0x41, "a")
        self.sender.on_key("a", False, 0x41, "a")
        self.assertEqual(self.keys(), [("keydown", "A"), ("keyup", "A")])

    def test_us_travels_with_a_character(self):
        self.sender.on_key("с", True, 0x43, "c")
        self.assertEqual(self.rig.sent(B, "keydown")[0]["data"], {"key": "с", "us": "c"})

    def test_a_media_key_is_not_sent_to_a_peer_that_does_not_take_them(self):
        self.rig.links.caps_of[B] = CAPS - {"media_keys"}
        self.sender.on_key("volume_up", True, 0xAF)
        self.assertEqual(self.keys(), [])
        self.rig.links.caps_of[B] = CAPS
        self.sender.on_key("volume_up", True, 0xAF)
        self.assertEqual(self.keys(), [("keydown", "volume_up")])

    def test_a_key_pressed_at_home_is_released_at_home(self):
        rig = Rig()
        self.assertFalse(rig.sender.on_key("a", True, 0x41, "a"))
        rig.sender.set_redirecting(True)
        self.assertFalse(rig.sender.on_key("a", False, 0x41, "a"))
        self.assertEqual(rig.sent(B, "keyup"), [])

    def test_a_key_held_on_the_peer_is_released_there_and_its_physical_release_swallowed(self):
        self.sender.on_key("a", True, 0x41, "a")
        self.sender.set_redirecting(False)
        self.assertEqual(self.keys(), [("keydown", "a"), ("keyup", "a")])
        self.assertTrue(self.sender.on_key("a", False, 0x41, "a"))

    def test_a_key_on_the_ignored_list_stays_on_this_pc(self):
        self.sender.update_config(make_config(ignored_inputs=["key:65"]))
        self.assertFalse(self.sender.on_key("a", True, 0x41, "a"))
        self.assertEqual(self.keys(), [])


class ReviewTests(unittest.TestCase):
    """What the cold reviews of step 5b found."""

    def two(self, **config):
        return Rig(
            entries=[harness.entry(B, "Mac", side="left"), harness.entry(C, "Other", side="top")],
            zones=[edge_zone(B), edge_zone(C)], up=(B, C), **config,
        )

    def start_worker(self, rig):
        import threading

        rig.sender._stop_event.clear()
        thread = threading.Thread(target=rig.sender._outbound_worker, daemon=True)
        thread.start()
        self.addCleanup(rig.sender._stop_event.set)

    def wait(self, predicate, timeout=3.0):
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def test_a_slow_clipboard_read_does_not_hold_up_the_take_of_another_peer(self):
        rig = self.two()
        rig.clipboard.slow_get = 1.0
        self.start_worker(rig)
        rig.sender.set_redirecting(True)
        rig.sender.on_message(B, protocol.accept_msg(rig.sender._owner.route))
        rig.sender.on_message(B, protocol.switch_v6(rig.sender._owner.route, C))
        self.assertTrue(self.wait(lambda: [m for t, m in rig.links.sent if t == C and m["type"] == "focus"], 0.4),
                        "the take of the next peer waited for the clipboard read")

    def test_a_read_asked_for_again_waits_its_turn_behind_a_write_decided_before_it(self):
        rig = self.two()
        rig.clipboard.block_get = True
        rig.clipboard.block_set = True
        rig.sender._deliver(owner_module.SendClipboard(B))
        self.assertTrue(rig.clipboard.get_started.wait(1), "the first clipboard read never started")

        write = threading.Thread(
            target=rig.sender._deliver,
            args=(owner_module.SetClipboard(protocol.clipboard_msg("from there")),),
            daemon=True,
        )
        write.start()
        time.sleep(0.1)

        key = {"type": protocol.MSG_KEYDOWN, "data": {"key": "v"}}
        rig.sender._deliver(owner_module.SendClipboard(B))
        rig.sender._deliver(owner_module.Send(B, key))
        rig.clipboard.release_get.set()
        self.assertTrue(rig.clipboard.set_started.wait(1), "the clipboard write never started")
        rig.clipboard.release_set.set()
        write.join(1)
        self.assertFalse(write.is_alive(), "the clipboard write did not finish")
        self.assertTrue(self.wait(lambda: not rig.sender._clip_wait), "the peer's queued messages did not finish")

        sent = [m for peer, m in rig.links.sent if peer == B]
        clipboards = [m["data"]["text"] for m in sent if m["type"] == protocol.MSG_CLIPBOARD]
        self.assertEqual(clipboards, ["here", "from there"])
        self.assertEqual([m["type"] for m in sent[-3:]], [protocol.MSG_CLIPBOARD, protocol.MSG_CLIPBOARD, protocol.MSG_KEYDOWN])

    def test_what_follows_a_clipboard_read_for_that_peer_still_goes_after_it(self):
        rig = self.two()
        rig.clipboard.slow_get = 0.2
        self.start_worker(rig)
        rig.sender.set_redirecting(True)
        rig.sender.on_key("a", True, 0x41, "a")
        self.assertTrue(self.wait(lambda: [m for t, m in rig.links.sent if t == B and m["type"] == "keydown"]))
        self.assertEqual([m["type"] for t, m in rig.links.sent if t == B], ["focus", "clipboard", "keydown"])

    def test_pointer_moves_made_during_a_clipboard_read_go_one_by_one_as_made(self):
        # Never summed: the far machine's edge feels each move where its pointer is (return_edge).
        rig = self.two()
        rig.clipboard.slow_get = 0.3
        self.start_worker(rig)
        rig.sender.set_redirecting(True)
        for dx, dy in ((3, 1), (4, -2), (5, 0)):
            rig.sender.on_motion(dx, dy)
        self.assertTrue(self.wait(lambda: [m for t, m in rig.links.sent if t == B and m["type"] == "mousemove"]))
        self.assertTrue(self.wait(lambda: not rig.sender._clip_wait))
        moves = [m["data"] for t, m in rig.links.sent if t == B and m["type"] == "mousemove"]
        self.assertEqual([(m["dx"], m["dy"]) for m in moves], [(3, 1), (4, -2), (5, 0)])

    def test_a_link_lost_during_a_clipboard_read_drops_what_waited_and_a_retake_sends_the_clipboard_after_its_focus(self):
        rig = self.two()
        rig.clipboard.slow_get = 0.4
        self.start_worker(rig)
        rig.sender.set_redirecting(True)
        self.assertTrue(self.wait(lambda: rig.sender._clip_wait))
        rig.sender.on_key("a", True, 0x41, "a")
        self.assertTrue(self.wait(lambda: rig.sender._clip_wait.get(B)))
        rig.sender.on_link(B, False)
        rig.sender.on_link(B, True, accepts=True)
        mark = len(rig.links.sent)
        rig.sender.set_redirecting(True)
        self.assertTrue(self.wait(lambda: [m for t, m in rig.links.sent[mark:] if t == B and m["type"] == "clipboard"], 2.0))
        self.assertTrue(self.wait(lambda: not rig.sender._clip_wait))
        time.sleep(0.5)
        after = [m["type"] for t, m in rig.links.sent[mark:] if t == B]
        self.assertEqual(after, ["focus", "clipboard"])

    def test_a_peer_that_starts_accepting_input_arms_its_zone_at_once(self):
        rig = Rig(up=())
        rig.bring_up(B, accepts=False)
        rig.push(10)
        self.assertFalse(rig.sender.redirecting)
        rig.inbound(B, protocol.accepts_msg(True))
        rig.sender.tick()
        rig.push(4)
        self.assertTrue(rig.sender.redirecting)

    def test_every_auto_repeat_of_a_key_keeps_the_name_its_press_went_under(self):
        rig = Rig(modifier_style="positional")
        rig.sender.set_redirecting(True)
        rig.accept_take()
        rig.sender.on_key("cmd", True, 0xA2)
        rig.flush()
        rig.sender.update_config(make_config(modifier_style="semantic"))
        rig.sender.on_key("cmd", True, 0xA2)
        rig.sender.on_key("cmd", False, 0xA2)
        self.assertEqual([m["data"]["key"] for m in rig.sent(B) if m["type"] in ("keydown", "keyup")], ["ctrl"] * 3)

    def test_settings_from_a_peer_that_does_not_list_the_capability_are_ignored(self):
        rig = Rig()
        seen = []
        rig.sender.settings_callback = lambda peer, data: seen.append(data)
        rig.links.caps_of[B] = frozenset({"clipboard"})
        rig.inbound(B, protocol.settings_msg({"on": False, "set_at": 5, "by": ""}))
        self.assertEqual(seen, [])
        rig.links.caps_of[B] = CAPS
        rig.inbound(B, protocol.settings_msg({"on": False, "set_at": 5, "by": ""}))
        self.assertEqual(len(seen), 1)

    def test_a_peer_that_is_down_is_named_with_its_own_status_not_the_first_peers(self):
        rig = Rig(
            entries=[harness.entry(B, "Mac", side="left"), harness.entry(C, "Other", side="top")],
            zones=[edge_zone(B), edge_zone(C)], up=(B,),
        )
        rig.sender.on_status(rig.links.key_of(rig.settings.data["peers"][0]), True, "Connected to Mac")
        rig.sender.on_status(rig.links.key_of(rig.settings.data["peers"][1]), False, "Other is unreachable")
        rig.sender.go(C)
        self.assertEqual(rig.alerts, ["Cannot switch — Other is unreachable"])

    def test_stopping_sends_the_let_go_and_the_releases_first(self):
        rig = Rig()
        rig.sender._threads = []
        rig.sender.start(make_config(crossing_resistance_px=40))
        rig.sender.set_redirecting(True)
        rig.accept_take()
        rig.sender.on_key("a", True, 0x41, "a")
        rig.sender.stop()
        kinds = [m["type"] for _peer, m in rig.links.sent if m["type"] in ("keyup", "focus")]
        self.assertEqual(kinds[-2:], ["keyup", "focus"])

    def test_a_driver_that_cannot_be_sent_home_is_said_so_and_the_move_is_not_made(self):
        rig = Rig()
        rig.driven = True
        rig.sender.send_peer_home = lambda: False
        rig.sender.set_redirecting(True)
        self.assertTrue(rig.sender._stop_event.wait(0.2) or True)
        self.assertFalse(rig.sender.redirecting)
        self.assertTrue(self.wait(lambda: rig.alerts))
        self.assertTrue(rig.alerts[0].startswith("Cannot switch"))


class MouseTests(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.sender = self.rig.sender

    def test_nothing_is_swallowed_or_sent_while_input_is_this_pcs(self):
        self.assertFalse(self.sender.on_button("left", True))
        self.assertFalse(self.sender.on_wheel(1.0, 0.0))
        self.assertFalse(self.sender.on_pointer_move())
        self.sender.on_motion(5, 5)
        self.assertEqual(self.rig.sent(B), [])

    def test_everything_goes_to_the_peer_and_off_this_pc_while_it_has_input(self):
        self.sender.set_redirecting(True)
        self.rig.accept_take()
        self.rig.desktop.cursor = (0, 500)
        self.rig.desktop.cursor = (40, 60)
        self.assertTrue(self.sender.on_pointer_move())
        self.assertEqual(self.rig.desktop.placed[-1], (0, 500))
        self.sender.on_motion(3, -2)
        self.assertTrue(self.sender.on_button("left", True))
        self.assertTrue(self.sender.on_wheel(-1.0, 0.0))
        kinds = [m["type"] for m in self.rig.sent(B) if m["type"] not in ("focus", "clipboard")]
        self.assertEqual(kinds, ["mousemove", "mousedown", "scroll"])
        self.assertEqual(self.rig.sent(B, "mousemove")[0]["data"], {"dx": 3, "dy": -2})

    def test_a_button_pressed_here_is_released_here_after_input_left(self):
        self.assertFalse(self.sender.on_button("left", True))
        self.sender.set_redirecting(True)
        self.assertFalse(self.sender.on_button("left", False))

    def test_a_button_held_on_the_peer_is_released_there(self):
        self.sender.set_redirecting(True)
        self.sender.on_button("right", True)
        self.sender.set_redirecting(False)
        self.assertEqual([m["data"]["button"] for m in self.rig.sent(B, "mouseup")], ["right"])
        self.assertTrue(self.sender.on_button("right", False))

    def test_the_round_trip_is_the_links_while_input_is_there_and_none_at_home(self):
        self.rig.links.trip = 7
        self.assertIsNone(self.sender.round_trip_ms)
        self.sender.set_redirecting(True)
        self.assertEqual(self.sender.round_trip_ms, 7)


class SettingsTests(unittest.TestCase):
    def test_a_machine_without_an_id_drives_nothing(self):
        rig = Rig(up=(), machine_id="")
        self.assertFalse(rig.sender.on_key("a", True, 0x41, "a"))
        self.assertFalse(rig.sender.set_redirecting(True))

    def test_the_links_are_told_the_peers_whenever_they_are_read(self):
        rig = Rig()
        rig.sender.refresh()
        self.assertEqual(len(rig.links.synced[-1]), 1)

    def test_a_peer_that_learns_its_id_gives_the_zones_a_target(self):
        migrated = harness.entry("", "Mac", side="left", from_1_4=True, linked=False)
        rig = Rig(entries=[migrated], zones=[edge_zone(B)], up=())
        rig.settings.data["zones"] = [{"peer": "", "kind": "edge"}]
        rig.settings.data["peers"][0]["id"] = protocol.id_text(B)
        rig.settings.data["zones"][0]["peer"] = protocol.id_text(B)
        rig.bring_up(B)
        rig.push(4)
        self.assertTrue(rig.sender.redirecting)

    def test_a_clipboard_set_by_a_peer_reaches_the_clipboard_only_from_a_machine_just_let_go(self):
        rig = Rig()
        rig.sender.set_redirecting(True)
        rig.accept_take()
        rig.sender.set_redirecting(False)
        rig.inbound(B, protocol.clipboard_msg("from the mac"))
        self.assertEqual(rig.clipboard.set_calls, [("from the mac", None)])
        rig.inbound(B, protocol.clipboard_msg("again"))
        self.assertEqual(len(rig.clipboard.set_calls), 1)

    def let_go_of_b(self):
        rig = Rig()
        rig.sender.set_redirecting(True)
        rig.accept_take()
        rig.sender.set_redirecting(False)
        return rig

    def test_a_clipboard_with_a_text_that_is_not_a_string_costs_nothing(self):
        rig = self.let_go_of_b()
        rig.bring_up(C)
        rig.sender._owner.on = C
        rig.inbound(B, {"type": protocol.MSG_CLIPBOARD, "data": {"text": 5}})
        self.assertEqual(rig.clipboard.set_calls, [])
        self.assertEqual(rig.sent(C, "clipboard"), [])
        self.assertEqual(rig.links.closed, [])
        self.assertTrue(rig.links.up(C))

    def test_a_clipboard_is_handed_to_the_owner_as_rebuilt_from_what_was_read(self):
        rig = self.let_go_of_b()
        taken = []
        rig.sender._owner.clipboard_arrived = lambda peer, began, message: taken.append(message) or []
        rig.inbound(B, {"type": protocol.MSG_CLIPBOARD, "data": {"text": "hi", "z": [{}, {}]}, "extra": 1})
        self.assertEqual(taken, [protocol.clipboard_msg("hi", None)])
        rig.inbound(B, {"type": protocol.MSG_CLIPBOARD, "data": {"text": 5}})
        self.assertEqual(len(taken), 1)


class ArrangementTests(unittest.TestCase):
    def test_this_pcs_edge_and_stamp_travel_to_a_peer_with_this_pc_as_the_author(self):
        rig = Rig()
        self.assertTrue(rig.sender.send_arrangement(B, "left", 1790000001))
        message = rig.sent(B, "arrangement")[0]
        # way_back too, as every arrangement carries it (WIRE.md section 8).
        self.assertEqual({key: message["data"][key] for key in ("edge", "set_at", "by")},
                         {"edge": "left", "set_at": 1790000001, "by": protocol.id_text(HERE)})
        self.assertIsInstance(message["data"]["way_back"], bool)

    def test_no_link_up_means_the_app_must_use_the_peers_own(self):
        rig = Rig(up=())
        self.assertFalse(rig.sender.send_arrangement(B, "left", 1790000001))

    def test_a_peers_arrangement_reaches_the_app(self):
        rig = Rig()
        seen = []
        rig.sender.arrangement_callback = lambda peer, read: seen.append((peer, read["edge"]))
        rig.inbound(B, protocol.arrangement_v6("right", 5, B))
        self.assertEqual(seen, [(B, "right")])

    def test_a_malformed_arrangement_is_ignored(self):
        rig = Rig()
        seen = []
        rig.sender.arrangement_callback = lambda peer, read: seen.append(read)
        rig.inbound(B, {"type": "arrangement", "data": {"edge": "up", "set_at": "x"}})
        self.assertEqual(seen, [])


class WakeTests(unittest.TestCase):
    def setUp(self):
        self.woken = []
        self.rig = Rig(entries=[harness.entry(B, "Mac", side="left", hw="aa:bb:cc:dd:ee:ff")], up=())
        self.sender = self.rig.sender
        self.sender._wake_sender = lambda address, host=None: self.woken.append((address, host))

    def test_switching_with_the_peer_asleep_wakes_it(self):
        self.sender.set_redirecting(True)
        self.sender._stop_event.wait(0.2)
        self.assertEqual(self.woken, [("aa:bb:cc:dd:ee:ff", "192.168.77.9")])
        self.sender._stop_event.set()

    def test_a_peer_that_answered_and_refused_is_not_woken(self):
        self.rig.links.refusing.add(B)
        self.assertFalse(self.sender.wake(B))

    def test_a_peer_without_a_hardware_address_is_not_woken(self):
        self.rig.settings.data["peers"][0]["hw"] = ""
        self.sender.refresh()
        self.assertFalse(self.sender.wake(B))


class SelfConnectionTests(unittest.TestCase):
    """The one way this PC could take its own daily link down: learning its own address as a peer's."""

    def test_loopback_and_this_pcs_own_addresses_are_refused(self):
        local = ["192.168.77.3"]
        for host in ("127.0.0.1", "", "0.0.0.0", "192.168.77.3"):
            self.assertTrue(sender.is_this_machine(host, local_addresses=local, address_towards=lambda h: "192.168.77.3"), host)

    def test_a_peers_address_is_not(self):
        self.assertFalse(sender.is_this_machine("192.168.77.5", local_addresses=["192.168.77.3"], address_towards=lambda h: "192.168.77.3"))

    def test_this_pcs_own_real_address_is_not_missed_when_enumeration_fails(self):
        self.assertTrue(sender.is_this_machine("192.168.77.3", local_addresses=[], address_towards=lambda h: h))

    def test_a_route_probe_that_fails_does_not_stop_the_link(self):
        def fail(host):
            raise OSError("no route")

        self.assertFalse(sender.is_this_machine("192.168.77.5", local_addresses=[], address_towards=fail))


class CaptureTests(unittest.TestCase):
    def test_the_hook_translates_a_windows_message_into_the_neutral_calls(self):
        self.assertEqual(capture_win.mouse_event(capture_win.WM_LBUTTONDOWN, 0), ("button", "left", True))
        self.assertEqual(capture_win.mouse_event(capture_win.WM_MOUSEMOVE, 0), ("move",))
        self.assertEqual(capture_win.mouse_event(capture_win.WM_MOUSEWHEEL, 120 << 16), ("wheel", 1.0, 0.0))
        self.assertEqual(capture_win.mouse_event(capture_win.WM_MOUSEHWHEEL, 120 << 16), ("wheel", 0.0, 1.0))
        self.assertIsNone(capture_win.mouse_event(0x9999, 0))


if __name__ == "__main__":
    unittest.main()
