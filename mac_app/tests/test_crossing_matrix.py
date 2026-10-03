"""Every way in on this Mac, on each arrangement and in each state, both directions, against a
version 6 PC over loopback: the Mac's real controller driving a real LinkResponder (the PC), and
the Mac's real responder driven by a scripted initiator (the PC's own pointer and take). The v6
differences are named where a test pins one."""

import itertools
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from bridge_fakes import PAIRED_TOKEN, FakeQuartz, crossing_config
from core import protocol
from core.tests.responder_harness import HERE
from crossing import CORNERS, EDGES, OPPOSITE, CrossingEngine
from settings_store import config_to_raw
from two_machines import Duo, wait_for

BOUNDS = (0, 0, 1728, 1117)
NOTCH = (782.0, 946.0)
THIRDS = {"start": 0.17, "middle": 0.5, "end": 0.83}
PC_EDGE_POINT = {"left": (0, 540), "right": (1919, 540), "top": (960, 0), "bottom": (960, 1079)}
MAC_EDGE_POINT = {"left": (0, 558), "right": (1727, 558), "top": (863, 0), "bottom": (863, 1116)}


def side_point(side, third):
    f = THIRDS[third]
    return {
        "left": (0.0, f * 1116),
        "right": (1727.0, f * 1116),
        "top": (f * 1727, 0.0),
        "bottom": (f * 1727, 1116.0),
    }[side]


def outward(side, n=30):
    return {"left": (-n, 0), "right": (n, 0), "top": (0, -n), "bottom": (0, n)}[side]


def corner_point(corner):
    vertical, horizontal = corner.split("_")
    return (0.0 if horizontal == "left" else 1727.0), (0.0 if vertical == "top" else 1116.0)


def push_on_engine(engine, x, y, dx, dy):
    """The Windows edge the push arrives at, or None when it never crosses."""
    engine.reset()
    now = 0.0
    for _ in range(12):
        now += 0.01
        step = engine.feed(x, y, dx, dy, BOUNDS, now, notch_range=NOTCH)
        if step.crossed:
            return step
    return None


def probes():
    """(name, x, y, dx, dy, kind, where, third) for a push at every side's three thirds, at each
    corner diagonally and along each of its two sides, and at the notch."""
    for side in EDGES:
        for third in THIRDS:
            x, y = side_point(side, third)
            yield f"{side}/{third}", x, y, *outward(side), "edge", side, third
    for corner in CORNERS:
        vertical, horizontal = corner.split("_")
        x, y = corner_point(corner)
        yield f"{corner} diagonal", x, y, outward(horizontal)[0], outward(vertical)[1], "diagonal", corner, None
        inset = 4 if vertical == "top" else -4
        yield f"{corner} along {horizontal}", x, y + inset, *outward(horizontal), "straight", corner, horizontal
        inset = 4 if horizontal == "left" else -4
        yield f"{corner} along {vertical}", x + inset, y, *outward(vertical), "straight", corner, vertical
    yield "notch", 850.0, 0.0, 0, -30, "notch", None, None


def third_at_corner(corner, side):
    vertical, horizontal = corner.split("_")
    if side in ("left", "right"):
        return "start" if vertical == "top" else "end"
    return "start" if horizontal == "left" else "end"


def expected(methods, edge, corner, parts, probe):
    """The Windows edge a push arrives at, from the rules the Crossing page states: Edge is the
    whole arrangement side, Part of the edge its chosen thirds, Corner its one corner and it leaves
    by that corner's own horizontal side, the notch the top."""
    _, _, _, _, _, kind, where, third = probe

    def by_edge(side, part):
        if side != edge:
            return None
        if "edge" in methods or ("part" in methods and part in parts):
            return OPPOSITE[edge]
        return None

    if kind == "notch":
        return OPPOSITE["top"] if "notch" in methods or by_edge("top", "middle") else None
    if kind == "edge":
        if where == "top" and third == "middle" and "notch" in methods:
            return OPPOSITE["top"]
        return by_edge(where, third)
    vertical, horizontal = where.split("_")
    if kind == "diagonal":
        if "corner" in methods and where == corner:
            return OPPOSITE[horizontal]
        return by_edge(horizontal, third_at_corner(where, horizontal)) or by_edge(vertical, third_at_corner(where, vertical))
    return by_edge(third, third_at_corner(where, third))


def method_sets():
    for shortcut, pointer, corner, notch in itertools.product((False, True), ("", "edge", "part"), (False, True), (False, True)):
        methods = {name for name, on in (("shortcut", shortcut), (pointer, bool(pointer)), ("corner", corner), ("notch", notch)) if on and name}
        if methods:
            yield frozenset(methods)


class MacWaysInTests(unittest.TestCase):
    def test_every_way_crosses_where_it_says_and_nowhere_else(self):
        for methods in method_sets():
            for edge in EDGES:
                for corner in CORNERS:
                    for parts in (("middle",), ("start", "end"), ("start", "middle", "end")):
                        engine = CrossingEngine(methods=methods, edge=edge, corner=corner, resistance_px=40, parts=parts)
                        for probe in probes():
                            step = push_on_engine(engine, *probe[1:5])
                            arrived = None if step is None else step.edge
                            with self.subTest(methods=sorted(methods), edge=edge, corner=corner, parts=parts, probe=probe[0]):
                                self.assertEqual(arrived, expected(methods, edge, corner, parts, probe))

    def test_the_shortcut_alone_leaves_every_edge_a_wall(self):
        engine = CrossingEngine(methods=("shortcut",), edge="right")
        self.assertFalse(engine.armed)
        for probe in probes():
            self.assertIsNone(push_on_engine(engine, *probe[1:5]), probe[0])

    def test_where_the_pointer_lands_is_the_far_edge_at_the_same_fraction(self):
        for edge in EDGES:
            for third, fraction in THIRDS.items():
                engine = CrossingEngine(methods=("edge",), edge=edge, resistance_px=40)
                x, y = side_point(edge, third)
                step = push_on_engine(engine, x, y, *outward(edge))
                self.assertEqual(step.edge, OPPOSITE[edge])
                self.assertAlmostEqual(step.offset, fraction, places=2)


class ArrangementFromEitherSideTests(unittest.TestCase):
    """The arrangement is one value both machines hold: which Mac edge leads to the PC. One that
    arrives from the PC is applied by ControlWindow.apply_arrangement, the side that owns the
    settings file."""

    def setUp(self):
        import tempfile

        from kvm_bridge_app import ControlWindow
        from settings_store import SettingsStore

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.apply = ControlWindow.apply_arrangement
        self.store = SettingsStore(os.path.join(self.dir.name, "settings.json"))
        self.logged = []

    BY = protocol.id_text(b"\xff" * 16)
    SMALL = protocol.id_text(bytes(15) + b"\x01")

    def window(self, edge="right", stamp=0):
        cfg = crossing_config(edge=edge, arrangement_set_at=stamp)
        self.store.save(config_to_raw(cfg))
        cfg = self.store.load()
        self.rebuilt = 0
        self.told = []

        def zones_changed():
            self.rebuilt += 1

        return types.SimpleNamespace(
            controller=types.SimpleNamespace(cfg=cfg, crossing=None, zones_changed=zones_changed, _alert=lambda *a: None),
            settings_store=self.store,
            logger=types.SimpleNamespace(info=lambda *a: self.logged.append(a), exception=lambda *a: None),
            refresh=lambda: None,
            _load=lambda raw: None,
            _flush=lambda: None,
            _tell=lambda peer: self.told.append(peer),
        )

    def test_a_newer_arrangement_from_the_pc_is_applied_with_its_stamp_and_arms_the_new_edge(self):
        window = self.window("right", stamp=100)
        self.apply(window, "", "right", 200, self.BY)
        self.assertEqual(window.controller.cfg.crossing["edge"], "left")
        self.assertEqual(window.controller.cfg.crossing["arrangement_set_at"], 200)
        self.assertEqual(self.rebuilt, 1)
        # Answered with this Mac's own, so the PC learns whether a way leads back (WIRE.md section 8).
        self.assertEqual(len(self.told), 1)

    def test_an_older_one_is_ignored(self):
        window = self.window("right", stamp=200)
        self.apply(window, "", "right", 100, self.BY)
        self.assertEqual(window.controller.cfg.crossing["edge"], "right")
        self.assertEqual(self.rebuilt, 0)

    def test_the_same_one_over_the_second_link_changes_nothing_the_second_time(self):
        window = self.window("left", stamp=300)
        self.apply(window, "", "left", 400, self.BY)
        self.apply(window, "", "left", 400, self.BY)
        self.assertEqual(self.rebuilt, 1)

    def test_two_different_arrangements_stamped_in_the_same_second_settle_on_the_larger_id(self):
        window = self.window("right", stamp=300)
        self.apply(window, "", "right", 300, self.SMALL)
        self.assertEqual(window.controller.cfg.crossing["edge"], "right")
        self.apply(window, "", "right", 300, self.BY)
        self.assertEqual(window.controller.cfg.crossing["edge"], "left")


class ArrangementOverTheLinksTests(unittest.TestCase):
    """One change reaches the other machine over whichever link is up, never over both: the link a
    machine opened carries what it says, and the one the other opened carries it only while it has
    none of its own open."""

    def duo(self, **options):
        duo = Duo(**options).listen()
        self.addCleanup(duo.close)
        return duo

    @staticmethod
    def heard(duo, set_at):
        return [(peer, read) for peer, read in duo.pc_responder.arrangements if read["set_at"] == set_at]

    def test_a_change_made_here_goes_over_this_macs_own_link(self):
        duo = self.duo()
        duo.link_mac_to_pc()
        self.assertTrue(duo.mac.send_arrangement(protocol.id_text(HERE), "left", 500))
        self.assertTrue(wait_for(lambda: self.heard(duo, 500)), "the PC never heard the arrangement")
        peer, read = self.heard(duo, 500)[0]
        self.assertEqual((peer, read["edge"], read["by"]), (duo.mac_id, "left", duo.mac_id))

    def test_with_no_link_of_its_own_open_it_goes_over_the_link_the_pc_opened(self):
        duo = self.duo()
        duo.link_pc_to_mac()
        self.assertTrue(duo.mac_input.send_arrangement(protocol.id_text(HERE), "left", 500))
        message = duo.pc.expect(protocol.MSG_ARRANGEMENT, where=lambda data: data["set_at"] == 500)
        self.assertEqual((message["edge"], message["by"]), ("left", duo.mac_text))

    def test_with_both_links_open_it_goes_over_this_macs_own_only(self):
        duo = self.duo()
        duo.link_mac_to_pc()
        duo.link_pc_to_mac()
        self.assertFalse(duo.mac_input.send_arrangement(protocol.id_text(HERE), "left", 500))
        self.assertTrue(duo.mac.send_arrangement(protocol.id_text(HERE), "left", 500))
        self.assertTrue(wait_for(lambda: self.heard(duo, 500)))
        self.assertIsNone(duo.pc.expect(protocol.MSG_ARRANGEMENT, 0.3))

    def test_a_change_made_on_the_pc_reaches_the_app_as_this_macs_edge_over_either_link(self):
        duo = self.duo()
        duo.link_mac_to_pc()
        duo.link_pc_to_mac()
        # Over the link the PC opened, and over the one this Mac opened to it.
        duo.pc.send(protocol.arrangement_v6("right", 600, HERE))
        self.assertTrue(duo.pc_responder.responder.send(duo.mac_id, protocol.arrangement_v6("right", 700, HERE)))
        self.assertTrue(wait_for(lambda: len(duo.mac_arrangements) == 2), duo.mac_arrangements)
        self.assertEqual(sorted(duo.mac_arrangements), [("left", 600, protocol.id_text(HERE)), ("left", 700, protocol.id_text(HERE))])

    def test_a_link_coming_up_announces_the_saved_side_with_its_stamp_and_who_made_it(self):
        duo = self.duo(side="top", side_stamp=300, side_by=protocol.id_text(HERE))
        duo.link_mac_to_pc()
        self.assertTrue(wait_for(lambda: duo.pc_responder.arrangements))
        peer, read = duo.pc_responder.arrangements[0]
        self.assertEqual((read["edge"], read["set_at"], read["by"]), ("top", 300, HERE))

    def test_the_one_machine_never_placed_is_announced_on_the_right_unstamped_as_the_page_shows_it(self):
        duo = self.duo(side="")
        duo.link_mac_to_pc()
        # What a link announces goes before anything sent after it, so this one is the fence.
        self.assertTrue(duo.mac.send_arrangement(protocol.id_text(HERE), "left", 9))
        self.assertTrue(wait_for(lambda: self.heard(duo, 9)))
        self.assertEqual([(read["edge"], read["set_at"]) for _, read in duo.pc_responder.arrangements],
                         [("right", 0), ("left", 9)])


class WhatIsSharedTests(unittest.TestCase):
    def test_the_crossing_page_shares_only_the_side_the_resistance_stays_each_machines_own(self):
        import pages

        line = pages.SCOPE["crossing"]
        self.assertNotIn("Two are shared", line)
        self.assertIn("Only which side each machine is on is shared", line)


def pc_at_edge(duo, edge):
    x, y = duo.pc_desktop.cursor
    return {"left": x == 0, "right": x == 1919, "top": y == 0, "bottom": y == 1079}[edge]


class MacToPcTests(unittest.TestCase):
    """This Mac's own pointer out to the PC, and home again through the PC's zone while it drives.
    The PC's way home is the zones it keeps for this Mac (WIRE.md section 8), not anything this
    Mac chose."""

    def duo(self, crossing=None, **options):
        duo = Duo(crossing={"resistance_px": 40, **(crossing or {})}, **options).listen()
        self.addCleanup(duo.close)
        return duo

    def arrival(self, duo):
        """The step the pointer's arrival home was shown with."""
        self.assertTrue(wait_for(lambda: duo.feedback and duo.feedback[-1][0] == "arrive"), "no arrival was shown")
        return duo.feedback[-1][1]

    def cross(self, duo, edge, times=12):
        x, y = side_point(edge, "middle")
        self.assertTrue(duo.mac_push(x, y, *outward(edge), times=times), "the push never crossed")
        self.assertTrue(wait_for(lambda: duo.pc_responder.responder.owner == duo.mac_id), "the PC was never taken")
        self.assertTrue(wait_for(lambda: duo.pc_responder.arrivals), "the pointer never landed on the PC")

    def test_each_arrangement_crosses_to_the_opposite_edge_and_that_edge_leads_home(self):
        for edge in EDGES:
            with self.subTest(pc_is=edge):
                duo = self.duo(crossing={"edge": edge})
                duo.link_mac_to_pc()
                self.cross(duo, edge)
                self.assertTrue(pc_at_edge(duo, OPPOSITE[edge]))
                self.assertEqual(duo.pc_responder.arrivals[-1][0], OPPOSITE[edge])
                self.assertTrue(duo.mac_lean(*outward(OPPOSITE[edge])), "the PC's edge facing this Mac never led home")
                self.assertEqual(self.arrival(duo).mac_edge, edge)
                duo.close()

    def test_the_way_home_uses_the_resistance_this_mac_sends_with_the_take(self):
        # 50 and 400 sit either side of the 120 the PC assumes when none is sent, so neither
        # passing nor failing to pass could be the default.
        duo = self.duo(crossing={"resistance_px": 50})
        duo.link_mac_to_pc()
        self.cross(duo, "right")
        self.assertTrue(duo.mac_lean(-30, times=4), "50 px of resistance did not give within four pushes")
        duo = self.duo(crossing={"resistance_px": 400})
        duo.link_mac_to_pc()
        self.cross(duo, "right", times=60)
        self.assertFalse(duo.mac_lean(-30, times=6), "400 px of resistance gave to 180 px of push")
        self.assertTrue(duo.mac.redirecting)

    def test_home_through_any_third_of_an_edge_zone(self):
        for third in THIRDS:
            with self.subTest(third=third):
                duo = self.duo()
                duo.link_mac_to_pc()
                self.cross(duo, "right")
                duo.pc_desktop.cursor = (0, int(THIRDS[third] * 1079))
                self.assertTrue(duo.mac_lean(-30), "the push through the PC's left edge never came home")
                duo.close()

    def test_a_part_zone_leads_home_only_through_its_thirds(self):
        duo = self.duo(pc_zones=[{"peer": "mac", "kind": "part", "parts": ["middle"]}])
        duo.link_mac_to_pc()
        self.cross(duo, "right")
        duo.pc_desktop.cursor = (0, int(THIRDS["start"] * 1079))
        self.assertFalse(duo.mac_lean(-30, times=4), "the start third is outside the zone and still led home")
        duo.pc_desktop.cursor = (0, int(THIRDS["middle"] * 1079))
        self.assertTrue(duo.mac_lean(-30), "the middle third did not lead home")

    def test_a_corner_zone_leads_home_through_its_corner_and_only_by_a_diagonal_push(self):
        duo = self.duo(pc_zones=[{"peer": "mac", "kind": "corner", "corner": "top_left", "edge": "left"}])
        duo.link_mac_to_pc()
        self.cross(duo, "right")
        duo.pc_desktop.cursor = (0, 0)
        self.assertFalse(duo.mac_lean(-30, 0, times=4), "a straight push into the corner led home")
        self.assertTrue(duo.mac_lean(-30, -30), "the diagonal push into the corner did not lead home")

    def test_a_shortcut_take_names_no_position_and_the_pcs_zone_still_leads_home(self):
        # Whatever the Mac's own ways are: its engine arms nothing here, the PC's zone does.
        duo = self.duo(crossing={"methods": ["shortcut"], "edge": "top"})
        duo.link_mac_to_pc()
        self.assertTrue(duo.mac.set_redirecting(True))
        self.assertTrue(wait_for(lambda: duo.pc_responder.responder.owner == duo.mac_id))
        self.assertTrue(wait_for(lambda: duo.pc_responder.arrivals))
        self.assertEqual(duo.pc_responder.arrivals[-1][0], None)
        duo.pc_desktop.cursor = (960, 1079)
        self.assertTrue(duo.mac_lean(0, 30), "the PC's bottom edge, facing this Mac, never led home")
        self.assertEqual(self.arrival(duo).mac_edge, "top")

    def test_a_notch_crossing_lands_on_the_pcs_bottom_edge_and_the_way_home_is_still_the_zone(self):
        # Replaces the 1.4.x gap G6: the way home no longer depends on how the pointer left. It is
        # the PC's zone for this Mac, so the edge the pointer arrived by is only where it landed.
        duo = self.duo(crossing={"methods": ["edge", "notch"], "edge": "right"}, notch=NOTCH)
        duo.link_mac_to_pc()
        self.assertTrue(duo.mac_push(850.0, 0.0, 0, -30), "the push into the notch never crossed")
        self.assertTrue(wait_for(lambda: duo.pc_responder.arrivals))
        self.assertEqual(duo.pc_responder.arrivals[-1][0], "bottom")
        duo.pc_desktop.cursor = (960, 1079)
        self.assertFalse(duo.mac_lean(0, 30, times=6), "the edge it landed by led home")
        duo.pc_desktop.cursor = (0, 540)
        self.assertTrue(duo.mac_lean(-30), "the zone's edge did not lead home")

    def test_nothing_crosses_while_the_link_is_down_and_nothing_is_swallowed(self):
        duo = self.duo(crossing={"edge": "right"})
        for _ in range(12):
            event = {"location": (1727.0, 558.0), FakeQuartz.kCGMouseEventDeltaX: 30, FakeQuartz.kCGMouseEventDeltaY: 0}
            self.assertIs(duo.mac._event_tap_callback(None, FakeQuartz.kCGEventMouseMoved, event, None), event)
        self.assertFalse(duo.mac.redirecting)
        self.assertEqual(FakeQuartz.warp_calls, [])

    def test_this_macs_direction_switch_off_stops_every_way_out(self):
        duo = self.duo(crossing={"edge": "right"})
        duo.link_mac_to_pc()
        # As the switch does: written to the settings, before the link it no longer wants has gone.
        duo.store.set_peer(PAIRED_TOKEN, send=False)
        x, y = side_point("right", "middle")
        self.assertFalse(duo.mac_push(x, y, 30))
        self.assertFalse(duo.mac.set_redirecting(True))
        self.assertIsNone(duo.pc_responder.responder.owner)

    def test_a_pc_that_does_not_allow_this_mac_to_drive_it_is_never_taken(self):
        duo = self.duo(crossing={"edge": "right"})
        duo.pc_responder.settings.peer(duo.mac_text)["allow_drive"] = False
        duo.link_mac_to_pc()
        self.assertTrue(wait_for(lambda: duo.mac._accepts.get(protocol.id_text(HERE)) is False))
        x, y = side_point("right", "middle")
        self.assertFalse(duo.mac_push(x, y, 30))
        self.assertIsNone(duo.pc_responder.responder.owner)
        self.assertIn("does not accept input from this Mac", duo.alerts[-1])

    def test_paused_and_full_screen_hold_the_mac_pointer_but_not_the_pc_pointer_coming_home(self):
        for held in ("crossing_paused", "full_screen_app"):
            with self.subTest(held=held):
                duo = self.duo(crossing={"edge": "right"})
                duo.link_mac_to_pc()
                self.cross(duo, "right")
                setattr(duo.mac, held, True if held == "crossing_paused" else "Steam")
                duo.pc_desktop.cursor = (0, 540)
                self.assertTrue(duo.mac_lean(-30), "the PC's edge stopped leading home while held")
                duo.close()


class MacDrivenTests(unittest.TestCase):
    """The PC's pointer and take arriving on this Mac, which a scripted initiator stands in for,
    and home again through this Mac's zone for the PC. The PC's own zones, its resistance and its
    settings are the PC's; what is proved here is what this Mac's responder does with them."""

    def duo(self, crossing=None, **options):
        duo = Duo(crossing={"resistance_px": 40, **(crossing or {})}, **options).listen()
        self.addCleanup(duo.close)
        return duo

    def drive(self, duo, side):
        duo.link_pc_to_mac()
        duo.pc_drives(side, 0.5)
        self.assertTrue(wait_for(lambda: duo.mac_arrivals), "the pointer never landed on the Mac")

    def at_edge(self, duo, edge):
        x, y = duo.mac_desktop.cursor
        return {"left": x == 0, "right": x == 1727, "top": y == 0, "bottom": y == 1116}[edge]

    def test_each_arrangement_lands_on_the_edge_facing_the_pc_and_that_edge_leads_home(self):
        for side in EDGES:
            with self.subTest(pc_is=side):
                duo = self.duo(crossing={"edge": side})
                self.drive(duo, side)
                self.assertTrue(self.at_edge(duo, side))
                self.assertEqual(duo.mac_responder.owner, HERE)
                duo.pc_leans(MAC_EDGE_POINT[side], *outward(side), times=3)
                switch = duo.pc_switch()
                self.assertEqual((switch["next"], switch["edge"]), (protocol.id_text(HERE), OPPOSITE[side]))
                duo.pc.let_go()
                self.assertTrue(wait_for(lambda: not duo.mac.receiving))
                duo.close()

    def test_the_way_home_uses_the_resistance_the_pc_sends_with_the_take(self):
        # 50 against this Mac's own 400, and the 120 assumed when none is sent.
        duo = self.duo(crossing={"resistance_px": 400})
        duo.link_pc_to_mac()
        duo.pc_drives("right", 0.5, resistance_px=50)
        duo.pc_leans(MAC_EDGE_POINT["right"], 30)
        self.assertIsNone(duo.pc_switch(0.05), "30 px gave against 50 px of resistance")
        duo.pc_leans(MAC_EDGE_POINT["right"], 30, times=2)
        self.assertIsNotNone(duo.pc_switch(), "90 px did not give against 50 px of resistance")

    def test_home_through_this_macs_zone_whatever_this_macs_own_ways_and_arrangement_are(self):
        for methods in (["notch"], ["corner"], ["part"], ["shortcut"]):
            with self.subTest(macs_ways=methods):
                # The edge zone named apart from the methods: its side, not the flat crossing edge,
                # is where the PC's pointer comes home.
                duo = self.duo(crossing={"methods": methods, "edge": "top"}, side="left",
                               mac_zones=[{"peer": "pc", "kind": "edge"}])
                self.drive(duo, "left")
                duo.pc_leans(MAC_EDGE_POINT["left"], -30, times=3)
                self.assertIsNotNone(duo.pc_switch(), "this Mac's left edge never led home")
                duo.close()

    def test_a_mac_whose_one_machine_was_never_placed_leads_home_through_the_right_it_shows(self):
        duo = self.duo(side="")
        duo.link_pc_to_mac()
        duo.pc_drives("left", 0.5)
        for edge in [edge for edge in EDGES if edge != "right"]:
            duo.pc_leans(MAC_EDGE_POINT[edge], *outward(edge), times=3)
            self.assertIsNone(duo.pc_switch(0.2), edge)
        self.assertTrue(duo.mac.receiving)
        duo.pc_leans(MAC_EDGE_POINT["right"], *outward("right"), times=3)
        self.assertIsNotNone(duo.pc_switch(), "the right edge the page shows never led home")

    def test_a_pc_this_mac_does_not_allow_to_drive_it_is_refused(self):
        duo = self.duo(allow_drive=False)
        duo.link_pc_to_mac()
        route = duo.pc_takes("left", 0.5)
        self.assertEqual(duo.pc.answer(route), (protocol.MSG_REFUSE, {"route": route, "why": "not_allowed"}))
        self.assertFalse(duo.mac.receiving)

    def test_the_macs_pause_and_full_screen_hold_its_edges_against_the_pcs_pointer(self):
        # 1.5.0 differs from 1.4.x here: the Mac's responder takes the hold, so the PC's pointer
        # no longer passes a held edge. Lifted, the same push leads home.
        for held in ("crossing_paused", "full_screen_app"):
            with self.subTest(held=held):
                # The full-screen hold is off by default since 02-10-2026.
                duo = self.duo(crossing={"edge": "right", "hold_full_screen": True})
                self.drive(duo, "right")
                setattr(duo.mac, held, True if held == "crossing_paused" else "Steam")
                duo.pc_leans(MAC_EDGE_POINT["right"], 30, times=4)
                self.assertIsNone(duo.pc_switch(0.2), "a held edge led home")
                setattr(duo.mac, held, False if held == "crossing_paused" else None)
                duo.pc_leans(MAC_EDGE_POINT["right"], 30, times=3)
                self.assertIsNotNone(duo.pc_switch(), "the edge did not lead home once released")
                duo.close()

    def test_this_macs_full_screen_setting_alone_decides_its_edges_while_the_pc_drives_it(self):
        for enabled in (False, True):
            with self.subTest(hold_full_screen=enabled):
                duo = self.duo(crossing={"edge": "right", "hold_full_screen": enabled})
                self.drive(duo, "right")
                duo.mac.full_screen_app = "Steam"
                duo.pc_leans(MAC_EDGE_POINT["right"], 30, times=4)
                if enabled:
                    self.assertIsNone(duo.pc_switch(0.2), "the Mac's enabled hold let the PC cross its edge")
                else:
                    self.assertIsNotNone(duo.pc_switch(), "the Mac's disabled hold held its edge")
                duo.close()

    def test_this_macs_own_pointer_can_take_input_back_only_with_its_own_link_to_the_pc_up(self):
        # Pinned known gap (G7 in the report): the push back needs the Mac's own link.
        duo = self.duo(crossing={"edge": "right"})
        self.drive(duo, "right")
        self.assertFalse(duo.mac_push(1727.0, 558.0, 30, times=6), "crossed with no link of its own")
        self.assertTrue(duo.mac.receiving)
        self.assertIsNone(duo.pc_switch(0.2))
        duo.link_mac_to_pc()
        duo.mac_push(1727.0, 558.0, 30)
        switch = duo.pc_switch()
        self.assertEqual(switch, {"route": switch["route"], "next": protocol.id_text(HERE)}, "the PC was sent home with a position")
        duo.pc.let_go()
        self.assertTrue(wait_for(lambda: duo.mac.redirecting), "the Mac's pointer could not follow the PC home")
        self.assertTrue(wait_for(lambda: duo.pc_responder.responder.owner == duo.mac_id))
        self.assertFalse(duo.mac.receiving)

    def test_a_shortcut_while_the_other_machine_drives_sends_its_input_home_and_then_drives(self):
        duo = self.duo(crossing={"edge": "right"})
        duo.link_mac_to_pc()
        duo.link_pc_to_mac()
        duo.pc_drives()
        self.assertTrue(duo.mac.set_redirecting(True))
        switch = duo.pc_switch()
        self.assertEqual(switch["next"], protocol.id_text(HERE))
        self.assertFalse(duo.mac.redirecting, "this Mac drove before the PC had let go")
        duo.pc.let_go()
        self.assertTrue(wait_for(lambda: duo.mac.redirecting), "this Mac never took over once the PC let go")
        self.assertTrue(wait_for(lambda: duo.pc_responder.responder.owner == duo.mac_id))
        # And while it drives, the PC cannot take it: one machine never drives and is driven at once.
        route = duo.pc_takes()
        # The refusal names the machine this Mac drives, which is the PC itself here.
        self.assertEqual(duo.pc.answer(route), (protocol.MSG_REFUSE, {"route": route, "why": "busy",
                                                                       "other": protocol.id_text(HERE)}))
        self.assertTrue(duo.mac.redirecting)


if __name__ == "__main__":
    unittest.main()
