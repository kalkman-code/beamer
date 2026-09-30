"""Every way in on this Mac, on each arrangement and in each state, both directions, against the
PC's own code over loopback. What each row proves, and which setting governs it, is written up in
docs/crossing-matrix-28-09-2026.md."""

import itertools
import os
import types
import unittest
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from crossing import CORNERS, EDGES, OPPOSITE, CrossingEngine
from settings_store import config_to_raw
from test_bridge import FakeQuartz, crossing_config
from two_machines import Duo, wait_for

BOUNDS = (0, 0, 1728, 1117)
NOTCH = (782.0, 946.0)
THIRDS = {"start": 0.17, "middle": 0.5, "end": 0.83}


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


class MacWayHomeNamedInTheHelloTests(unittest.TestCase):
    """`home_edge` is the PC edge facing this Mac: what the hello tells the PC, and what a
    shortcut switch arms as the PC's way home."""

    def home(self, methods, edge="right", corner="top_right"):
        return CrossingEngine(methods=methods, edge=edge, corner=corner).home_edge()

    def test_the_arrangement_side_is_the_way_home_for_edge_part_and_shortcut(self):
        for methods in (("edge",), ("part",), ("shortcut",), ("shortcut", "edge"), ("shortcut", "part"), ("edge", "corner")):
            for edge in EDGES:
                with self.subTest(methods=methods, edge=edge):
                    self.assertEqual(self.home(methods, edge), OPPOSITE[edge])

    def test_the_notch_alone_names_the_pcs_bottom_edge_whatever_the_arrangement_says(self):
        for edge in EDGES:
            self.assertEqual(self.home(("notch",), edge), "bottom")
            self.assertEqual(self.home(("notch", "shortcut"), edge), "bottom")

    def test_a_corner_alone_names_the_side_of_the_corner_whatever_the_arrangement_says(self):
        for edge in EDGES:
            for corner in CORNERS:
                self.assertEqual(self.home(("corner",), edge, corner), OPPOSITE[corner.split("_")[1]])

    def test_with_the_edge_on_too_the_arrangement_wins_and_the_notch_and_corner_arrive_elsewhere(self):
        # Pinned known gap (G6 in the report): the way home differs by how the pointer left.
        engine = CrossingEngine(methods=("edge", "notch", "corner"), edge="right", corner="top_left", resistance_px=40)
        self.assertEqual(engine.home_edge(), "left")
        by_notch = push_on_engine(engine, 850.0, 0.0, 0, -30)
        by_corner = push_on_engine(engine, 0.0, 0.0, -30, -30)
        self.assertEqual((by_notch.edge, by_corner.edge), ("bottom", "right"))


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

    def window(self, edge="right", stamp=0):
        cfg = crossing_config(edge=edge, arrangement_set_at=stamp)
        self.store.save(config_to_raw(cfg))
        cfg = self.store.load()
        return types.SimpleNamespace(
            controller=types.SimpleNamespace(cfg=cfg, crossing=None),
            settings_store=self.store,
            logger=types.SimpleNamespace(info=lambda *a: self.logged.append(a), exception=lambda *a: None),
            refresh=lambda: None,
        )

    def test_a_newer_arrangement_from_the_pc_is_applied_with_its_stamp_and_arms_the_new_edge(self):
        window = self.window("right", stamp=100)
        self.apply(window, "left", 200)
        self.assertEqual(window.controller.cfg.crossing["edge"], "left")
        self.assertEqual(window.controller.cfg.crossing["arrangement_set_at"], 200)
        self.assertEqual(window.controller.crossing.edge, "left")

    def test_an_older_one_is_ignored(self):
        window = self.window("right", stamp=200)
        self.apply(window, "left", 100)
        self.assertEqual(window.controller.cfg.crossing["edge"], "right")

    def test_the_same_one_over_the_second_link_changes_nothing_and_logs_nothing(self):
        window = self.window("left", stamp=300)
        self.apply(window, "left", 300)
        self.assertEqual(self.logged, [])

    def test_two_different_arrangements_stamped_in_the_same_second_each_keep_their_own(self):
        # Pinned known gap, not a rule: a tie-break would flip this.
        # Nothing breaks the tie, so the machines stay apart until one is changed again; two
        # people changing the same setting on two machines in one second is the only way in.
        window = self.window("right", stamp=300)
        self.apply(window, "left", 300)
        self.assertEqual(window.controller.cfg.crossing["edge"], "right")


class ArrangementOverTheLinksTests(unittest.TestCase):
    def test_a_change_reaches_the_other_machine_over_either_link(self):
        duo = Duo().listen()
        self.addCleanup(duo.close)
        duo.link_mac_to_pc()
        duo.link_pc_to_mac()
        # From the Mac: over its own link, and over the link the PC opened to it.
        self.assertTrue(duo.mac.send_arrangement("left", 500))
        self.assertTrue(duo.mac_server.send_arrangement("left", 500))
        self.assertTrue(wait_for(lambda: len(duo.pc_arrangements) == 2), duo.pc_arrangements)
        self.assertEqual(set(duo.pc_arrangements), {("left", 500)})
        # From the PC, the same two ways.
        self.assertTrue(duo.pc.send_arrangement("top", 600))
        self.assertTrue(duo.pc_server.send_arrangement("top", 600))
        self.assertTrue(wait_for(lambda: len(duo.mac_arrangements) == 2), duo.mac_arrangements)
        self.assertEqual(set(duo.mac_arrangements), {("top", 600)})

    def test_the_hello_carries_the_way_home_and_the_resistance_and_no_stamp(self):
        duo = Duo(crossing={"methods": ["notch", "shortcut"], "edge": "top", "resistance_px": 96}).listen()
        self.addCleanup(duo.close)
        duo.link_mac_to_pc()
        self.assertTrue(wait_for(lambda: duo.pc_peer))
        self.assertEqual(duo.pc_peer[0][1:], ("bottom", 96))


class WhatIsSharedTests(unittest.TestCase):
    def test_the_crossing_page_shares_only_the_side_the_resistance_stays_each_machines_own(self):
        import pages

        line = pages.SCOPE["crossing"]
        self.assertNotIn("Two are shared", line)
        self.assertIn("Only which side the PC is on is shared", line)


def pc_at_edge(duo, edge):
    x, y = duo.pc_desktop.cursor
    return {"left": x == 0, "right": x == 1919, "top": y == 0, "bottom": y == 1079}[edge]


class MacToPcTests(unittest.TestCase):
    """This Mac's own pointer out to the PC, and home again through the PC's edge while it drives."""

    def duo(self, **crossing):
        crossing.setdefault("resistance_px", 40)
        duo = Duo(crossing=crossing).listen()
        self.addCleanup(duo.close)
        return duo

    def cross(self, duo, edge):
        x, y = side_point(edge, "middle")
        self.assertTrue(duo.mac_push(x, y, *outward(edge)), "the push never crossed")
        self.assertTrue(wait_for(lambda: duo.pc_server.return_edge is not None), "the PC was never armed")

    def test_each_arrangement_crosses_to_the_opposite_edge_and_arms_that_edge_as_the_way_home(self):
        for edge in EDGES:
            with self.subTest(pc_is=edge):
                duo = self.duo(edge=edge)
                duo.link_mac_to_pc()
                self.cross(duo, edge)
                self.assertTrue(pc_at_edge(duo, OPPOSITE[edge]))
                self.assertEqual(duo.pc_server.return_edge, OPPOSITE[edge])
                duo.close()

    def test_the_way_home_uses_the_resistance_this_mac_sends_with_the_switch(self):
        # 77 is neither the default nor the PC's own 400: the PC's setting is never consulted.
        duo = Duo(crossing={"resistance_px": 77}, pc={"crossing_resistance_px": 400}).listen()
        self.addCleanup(duo.close)
        duo.link_mac_to_pc()
        self.cross(duo, "right")
        self.assertEqual(duo.pc_server.return_resistance, 77)

    def test_home_through_any_part_of_the_pcs_edge_whatever_the_pcs_own_ways_are(self):
        pc = {"crossing_methods": ["corner"], "crossing_corner": "bottom_right", "mac_return_edge": "top"}
        for third in THIRDS:
            with self.subTest(third=third):
                duo = Duo(crossing={"resistance_px": 40}, pc=pc).listen()
                self.addCleanup(duo.close)
                duo.link_mac_to_pc()
                self.cross(duo, "right")
                x, y = 0, int(THIRDS[third] * 1079)
                duo.pc_desktop.cursor = (x, y)
                for _ in range(6):
                    duo.mac_move(1727.0, 558.0, -30)
                    if not duo.mac.redirecting:
                        break
                    wait_for(lambda: False, 0.02)
                self.assertTrue(wait_for(lambda: not duo.mac.redirecting), "the push through the PC's left edge never came home")
                duo.close()

    def test_a_push_along_the_way_home_is_the_pcs_edge_facing_this_mac_even_with_only_the_shortcut_on(self):
        duo = self.duo(methods=["shortcut"], edge="top")
        duo.link_mac_to_pc()
        duo.mac.set_redirecting(True)
        self.assertTrue(wait_for(lambda: duo.pc_server.return_edge == "bottom"))

    def test_nothing_crosses_while_the_link_is_down_and_nothing_is_swallowed(self):
        duo = self.duo(edge="right")
        for _ in range(12):
            event = {"location": (1727.0, 558.0), FakeQuartz.kCGMouseEventDeltaX: 30, FakeQuartz.kCGMouseEventDeltaY: 0}
            self.assertIs(duo.mac._event_tap_callback(None, FakeQuartz.kCGEventMouseMoved, event, None), event)
        self.assertFalse(duo.mac.redirecting)
        self.assertEqual(FakeQuartz.warp_calls, [])

    def test_this_macs_direction_switch_off_stops_every_way_out_but_not_the_way_back_by_shortcut(self):
        duo = self.duo(edge="right")
        duo.link_mac_to_pc()
        duo.mac.cfg.send_to_windows = False
        x, y = side_point("right", "middle")
        self.assertFalse(duo.mac_push(x, y, 30))
        self.assertFalse(duo.mac.set_redirecting(True))

    def test_paused_and_full_screen_hold_the_mac_pointer_but_not_the_pc_pointer_coming_home(self):
        for held in ("crossing_paused", "full_screen_app"):
            with self.subTest(held=held):
                duo = self.duo(edge="right")
                duo.link_mac_to_pc()
                self.cross(duo, "right")
                setattr(duo.mac, held, True if held == "crossing_paused" else "Steam")
                duo.pc_desktop.cursor = (0, 540)
                for _ in range(6):
                    duo.mac_move(1727.0, 558.0, -30)
                    wait_for(lambda: False, 0.02)
                    if not duo.mac.redirecting:
                        break
                self.assertTrue(wait_for(lambda: not duo.mac.redirecting), "the PC's edge stopped leading home while held")
                duo.close()


def mac_at_edge(duo, edge):
    x, y = duo.mac_desktop.cursor
    return {"left": x == 0, "right": x == 1727, "top": y == 0, "bottom": y == 1116}[edge]


def pc_settings(edge, **more):
    return dict(mac_return_edge=edge, crossing_methods=["edge"], **more)


class PcToMacTests(unittest.TestCase):
    """The PC's own mouse out to this Mac, and home again through this Mac's edge while the PC
    drives."""

    def duo(self, pc=None, **crossing):
        crossing.setdefault("resistance_px", 40)
        duo = Duo(crossing=crossing, pc=pc).listen()
        self.addCleanup(duo.close)
        return duo

    def cross(self, duo, pc_edge):
        x, y = {"left": (0, 540), "right": (1919, 540), "top": (960, 0), "bottom": (960, 1079)}[pc_edge]
        self.assertTrue(duo.pc_push(x, y, *outward(pc_edge)), "the PC's push never crossed")
        self.assertTrue(wait_for(lambda: duo.mac_server.return_edge is not None), "the Mac was never armed")

    def test_each_arrangement_crosses_to_the_opposite_mac_edge_and_arms_it_as_the_way_home(self):
        for edge in EDGES:
            with self.subTest(mac_is=edge):
                duo = self.duo(pc=pc_settings(edge, crossing_resistance_px=40))
                duo.link_pc_to_mac()
                self.cross(duo, edge)
                self.assertTrue(wait_for(lambda: mac_at_edge(duo, OPPOSITE[edge])))
                self.assertEqual(duo.mac_server.return_edge, OPPOSITE[edge])
                duo.close()

    def test_the_way_home_uses_the_resistance_the_pc_sends_with_the_switch(self):
        duo = self.duo(pc=pc_settings("right", crossing_resistance_px=77, mac_resistance_px=5), resistance_px=400)
        duo.link_pc_to_mac()
        self.cross(duo, "right")
        self.assertEqual(duo.mac_server.return_resistance, 77)

    def test_home_through_this_macs_arrival_edge_whatever_this_macs_own_ways_and_arrangement_are(self):
        for methods in (["notch"], ["corner"], ["part"], ["shortcut"]):
            with self.subTest(macs_ways=methods):
                duo = self.duo(pc=pc_settings("right", crossing_resistance_px=40), methods=methods, edge="top")
                duo.link_pc_to_mac()
                self.cross(duo, "right")
                duo.mac_desktop.cursor = (0.0, 558.0)
                for _ in range(6):
                    duo.pc.on_motion(-30, 0)
                    wait_for(lambda: False, 0.02)
                    if not duo.pc.redirecting:
                        break
                self.assertTrue(wait_for(lambda: not duo.pc.redirecting), "the Mac's left edge never led home")
                duo.close()

    def test_a_pc_that_has_learned_no_edge_has_no_way_out_however_it_is_pushed(self):
        duo = self.duo(pc=pc_settings("", crossing_resistance_px=40))
        duo.link_pc_to_mac()
        for edge in EDGES:
            x, y = {"left": (0, 540), "right": (1919, 540), "top": (960, 0), "bottom": (960, 1079)}[edge]
            self.assertFalse(duo.pc_push(x, y, *outward(edge)), edge)
        self.assertFalse(duo.pc.redirecting)

    def test_no_link_no_crossing_and_the_pc_says_nothing(self):
        duo = self.duo(pc=pc_settings("right"))
        self.assertFalse(duo.pc_push(1919, 540, 30))
        self.assertFalse(duo.pc.redirecting)
        self.assertEqual(duo.pc.status, "Not connected to the Mac")

    def test_a_mac_that_is_not_listening_leaves_the_pc_with_no_link(self):
        duo = Duo(pc=pc_settings("right"))
        self.addCleanup(duo.close)
        duo.pc.start(duo.pc_config)
        self.assertFalse(wait_for(lambda: duo.pc.connected, 0.5))
        self.assertFalse(duo.pc_push(1919, 540, 30))

    def test_the_pcs_pause_and_full_screen_hold_its_own_edges(self):
        for held in ("crossing_paused", "full_screen_app"):
            with self.subTest(held=held):
                duo = self.duo(pc=pc_settings("right"))
                duo.link_pc_to_mac()
                setattr(duo.pc, held, True if held == "crossing_paused" else "Steam")
                self.assertFalse(duo.pc_push(1919, 540, 30))
                duo.close()

    def test_this_macs_own_pointer_can_take_input_back_only_with_its_own_link_to_the_pc_up(self):
        # Pinned known gap (G7 in the report): the push back needs the Mac's own link.
        duo = self.duo(pc=pc_settings("right", crossing_resistance_px=40), edge="right")
        duo.link_pc_to_mac()
        self.cross(duo, "right")
        self.assertTrue(wait_for(lambda: duo.mac.receiving))
        self.assertFalse(duo.mac_push(1727.0, 558.0, 30, times=6), "crossed with no link of its own")
        self.assertTrue(duo.mac.receiving)
        duo.link_mac_to_pc()
        self.assertTrue(duo.mac_push(1727.0, 558.0, 30), "the Mac's pointer could not take input back")
        self.assertTrue(wait_for(lambda: not duo.pc.redirecting))

    def test_a_shortcut_while_the_other_machine_drives_only_sends_its_input_home(self):
        duo = self.duo(pc=pc_settings("right"))
        duo.link_pc_to_mac()
        duo.link_mac_to_pc()
        self.assertTrue(duo.pc.set_redirecting(True))
        self.assertTrue(wait_for(lambda: duo.mac.receiving))
        self.assertTrue(duo.mac.set_redirecting(True))
        self.assertTrue(wait_for(lambda: not duo.pc.redirecting))
        self.assertFalse(duo.mac.redirecting, "the shortcut also crossed this Mac's input, which only a push does")
        self.assertTrue(duo.mac.set_redirecting(True))
        self.assertTrue(wait_for(lambda: duo.pc_server.return_edge is not None))
        self.assertTrue(duo.pc.set_redirecting(True))
        self.assertTrue(wait_for(lambda: not duo.mac.redirecting))
        self.assertFalse(duo.pc.redirecting)


if __name__ == "__main__":
    unittest.main()
