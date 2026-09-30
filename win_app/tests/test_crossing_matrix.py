"""This PC's ways in on each arrangement, and the arrangement as both machines hold it. The two-
machine cases, which need the Mac's code too, are in mac_app/tests/test_crossing_matrix.py; what
each row proves is written up in docs/crossing-matrix-28-09-2026.md."""

import itertools
import logging
import time
import types
import unittest
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import return_edge
import sender
from app_config import Config, default_config
from fakes import FakeClipboard, FakeDesktop
from core.return_edge import CORNERS, EDGES, OPPOSITE, Rect

MONITORS = [Rect(0, 0, 1920, 1080)]
THIRDS = {"start": 0.17, "middle": 0.5, "end": 0.83}
LAST_X, LAST_Y = 1919, 1079


def side_point(side, third):
    f = THIRDS[third]
    return {
        "left": (0, int(f * LAST_Y)),
        "right": (LAST_X, int(f * LAST_Y)),
        "top": (int(f * LAST_X), 0),
        "bottom": (int(f * LAST_X), LAST_Y),
    }[side]


def outward(side, n=30):
    return {"left": (-n, 0), "right": (n, 0), "top": (0, -n), "bottom": (0, n)}[side]


def corner_point(corner):
    vertical, horizontal = corner.split("_")
    return (0 if horizontal == "left" else LAST_X), (0 if vertical == "top" else LAST_Y)


def probes():
    for side in EDGES:
        for third in THIRDS:
            x, y = side_point(side, third)
            yield f"{side}/{third}", x, y, *outward(side), "edge", side, third
    for corner in CORNERS:
        vertical, horizontal = corner.split("_")
        x, y = corner_point(corner)
        yield f"{corner} diagonal", x, y, outward(horizontal)[0], outward(vertical)[1], "diagonal", corner, None
        yield f"{corner} along {horizontal}", x, y + (4 if vertical == "top" else -4), *outward(horizontal), "straight", corner, horizontal
        yield f"{corner} along {vertical}", x + (4 if horizontal == "left" else -4), y, *outward(vertical), "straight", corner, vertical


def third_at_corner(corner, side):
    vertical, horizontal = corner.split("_")
    if side in ("left", "right"):
        return "start" if vertical == "top" else "end"
    return "start" if horizontal == "left" else "end"


def make_sender(methods, edge, corner="top_left", parts=("middle",), **more):
    more.setdefault("crossing_resistance_px", 40)
    desktop = FakeDesktop(MONITORS)
    link = sender.MacSender(desktop=desktop, clipboard=FakeClipboard(), is_local=lambda host: False)
    link.update_config(
        Config(
            host="127.0.0.1",
            port=1,
            auth_token="matrix-token",
            mac_host="127.0.0.1",
            mac_return_edge=edge,
            crossing_methods=list(methods),
            crossing_corner=corner,
            crossing_edge_parts=list(parts),
            **more,
        )
    )
    link._sock = object()
    link._connected_at = link._last_ack_at = time.monotonic()
    return link, desktop


def push(methods, edge, probe, corner="top_left", parts=("middle",)):
    """The focus message a push sends, or None when the push never crosses."""
    link, desktop = make_sender(methods, edge, corner, parts)
    _, x, y, dx, dy = probe[:5]
    desktop.cursor = (x, y)
    for _ in range(12):
        link.on_motion(dx, dy)
        if link.redirecting:
            break
    if not link.redirecting:
        return None
    return [m for m in list(link._outbound.queue) if m["type"] == "focus"][-1]["data"]


def expected(methods, edge, corner, parts, probe):
    """The Mac edge a push arrives at. Edge is the whole arrangement side and Part of the edge its
    thirds; Corner is one corner, any of the four, and always arrives by the arrangement side."""
    _, _, _, _, _, kind, where, third = probe

    def by_edge(side, part):
        if side != edge:
            return None
        return OPPOSITE[edge] if "edge" in methods or ("part" in methods and part in parts) else None

    if kind == "edge":
        return by_edge(where, third)
    vertical, horizontal = where.split("_")
    if kind == "diagonal":
        if "corner" in methods and where == corner:
            return OPPOSITE[edge]
        return by_edge(horizontal, third_at_corner(where, horizontal)) or by_edge(vertical, third_at_corner(where, vertical))
    return by_edge(third, third_at_corner(where, third))


def method_sets():
    for shortcut, pointer, corner in itertools.product((False, True), ("", "edge", "part"), (False, True)):
        methods = {name for name, on in (("shortcut", shortcut), (pointer, bool(pointer)), ("corner", corner)) if on and name}
        if methods:
            yield frozenset(methods)


class PcWaysOutTests(unittest.TestCase):
    def test_every_way_crosses_where_it_says_and_nowhere_else(self):
        for methods in method_sets():
            for edge in EDGES:
                for corner in CORNERS:
                    for parts in (("middle",), ("start", "end"), ("start", "middle", "end")):
                        for probe in probes():
                            focus = push(methods, edge, probe, corner, parts)
                            arrived = None if focus is None else focus.get("edge")
                            with self.subTest(methods=sorted(methods), mac_is=edge, corner=corner, parts=parts, probe=probe[0]):
                                self.assertEqual(arrived, expected(methods, edge, corner, parts, probe))

    def test_the_shortcut_alone_leaves_every_edge_a_wall(self):
        for edge in EDGES:
            for probe in probes():
                self.assertIsNone(push(("shortcut",), edge, probe), probe[0])

    def test_the_mac_lands_at_the_same_fraction_along_the_far_edge(self):
        for edge in EDGES:
            for third, fraction in THIRDS.items():
                probe = (f"{edge}/{third}", *side_point(edge, third), *outward(edge))
                focus = push(("edge",), edge, probe)
                self.assertEqual(focus["edge"], OPPOSITE[edge])
                self.assertAlmostEqual(focus["offset"], fraction, places=2)

    def test_a_corner_arrives_at_its_position_along_the_arrangement_side_not_along_its_own(self):
        probe = ("top_left diagonal", 0, 0, -30, -30)
        self.assertEqual(push(("corner",), "right", probe, corner="top_left")["offset"], 0.0)
        probe = ("bottom_left diagonal", 0, LAST_Y, -30, 30)
        self.assertAlmostEqual(push(("corner",), "right", probe, corner="bottom_left")["offset"], 1.0, places=2)
        probe = ("top_left diagonal", 0, 0, -30, -30)
        self.assertEqual(push(("corner",), "bottom", probe, corner="top_left")["offset"], 0.0)
        probe = ("top_right diagonal", LAST_X, 0, 30, -30)
        self.assertAlmostEqual(push(("corner",), "bottom", probe, corner="top_right")["offset"], 1.0, places=2)

    def test_a_pc_that_holds_no_edge_has_no_way_out(self):
        for methods in method_sets():
            for probe in probes():
                self.assertIsNone(push(methods, "", probe), (methods, probe[0]))


class PcWayHomeNamedToTheMacTests(unittest.TestCase):
    def test_the_hello_and_a_shortcut_switch_name_the_mac_edge_opposite_the_pcs(self):
        for edge in EDGES:
            link, _ = make_sender(("shortcut",), edge)
            self.assertEqual(link._mac_arrival_edge(), OPPOSITE[edge])

    def test_a_crossing_names_the_edge_it_arrived_by(self):
        for edge in EDGES:
            probe = ("push", *side_point(edge, "middle"), *outward(edge))
            focus = push(("edge",), edge, probe)
            self.assertEqual(focus["return_edge"], focus["edge"])
            self.assertEqual(focus["resistance_px"], 40)

    def test_the_pcs_resistance_is_what_it_asks_the_mac_for_never_the_macs_own(self):
        link, _ = make_sender(("edge",), "right", crossing_resistance_px=40)
        link._config.crossing_resistance_px = 200
        link.update_config(link._config)
        self.assertEqual(link._resistance(), 200)
        link._config.mac_resistance_px = 5
        self.assertEqual(link._resistance(), 200, "the resistance the Mac reported is never read")


class WhatIsSharedTests(unittest.TestCase):
    def test_the_crossing_page_shares_only_the_side_the_resistance_stays_each_machines_own(self):
        import pages_win

        line = pages_win.SCOPE["crossing"]
        self.assertNotIn("Two are shared", line)
        self.assertIn("Only which side your Mac is on is shared", line)


try:
    import kvm_bridge_win
except ImportError:  # PySide6 is only in the Windows venv
    kvm_bridge_win = None


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class ArrangementHeldByBothTests(unittest.TestCase):
    """`_on_arrangement` and `_on_learned` are the two ways a Mac's idea of where the machines are
    reaches this PC: a stamped message over either link, and the unstamped way home the hello
    carries."""

    def page(self, edge="right", stamp=0):
        config = default_config()
        config.mac_return_edge = edge
        config.arrangement_set_at = stamp
        saved = []
        page = types.SimpleNamespace(
            _config=config,
            _persist=lambda: saved.append((config.mac_return_edge, config.arrangement_set_at)) or True,
            _start_sending=lambda config: None,
            sender=types.SimpleNamespace(update_config=lambda config: None),
            edge_choice=types.SimpleNamespace(set_value=lambda value: None),
            mac_host_readout=types.SimpleNamespace(setText=lambda text: None),
            _reflect_look=lambda: None,
            _reflect_ways=lambda: None,
        )
        return page, saved

    def arrive(self, page, mac_edge, stamp):
        kvm_bridge_win.WindowsApplication._on_arrangement(page, mac_edge, stamp)

    def learn(self, page, edge, host="192.168.1.10"):
        real = kvm_bridge_win.sender.is_this_machine
        kvm_bridge_win.sender.is_this_machine = lambda h: False
        try:
            kvm_bridge_win.WindowsApplication._on_learned(page, host, edge, 40)
        finally:
            kvm_bridge_win.sender.is_this_machine = real

    def test_a_newer_arrangement_from_the_mac_is_applied_as_this_pcs_opposite_edge_with_its_stamp(self):
        page, saved = self.page("right", stamp=100)
        self.arrive(page, "top", 200)
        self.assertEqual((page._config.mac_return_edge, page._config.arrangement_set_at), ("bottom", 200))

    def test_the_same_edge_with_a_newer_stamp_leaves_the_stored_stamp_as_the_mac_does(self):
        page, saved = self.page("left", stamp=100)
        self.arrive(page, "right", 200)
        self.assertEqual((page._config.mac_return_edge, page._config.arrangement_set_at), ("left", 100))

    def test_an_older_arrangement_is_ignored(self):
        page, saved = self.page("right", stamp=200)
        self.arrive(page, "top", 100)
        self.assertEqual(page._config.mac_return_edge, "right")
        self.assertEqual(saved, [])

    def test_the_same_arrangement_over_the_second_link_changes_nothing_and_logs_nothing(self):
        # One change on the Mac reaches this PC over both links, so the second copy carries the
        # stamp the first already stored.
        page, saved = self.page("left", stamp=300)
        with self.assertNoLogs(kvm_bridge_win.LOGGER, logging.INFO):
            self.arrive(page, "right", 300)
        self.assertEqual(saved, [])

    def test_the_macs_hello_names_the_edge_of_a_pc_that_has_none(self):
        page, saved = self.page("", stamp=0)
        self.learn(page, "left")
        self.assertEqual(page._config.mac_return_edge, "left")

    def test_a_change_made_here_with_no_link_is_stored_and_told_to_nobody(self):
        page, saved = self.page("right", stamp=0)
        told = []
        page.sender = types.SimpleNamespace(send_arrangement=lambda *a: told.append(("pc-to-mac", a)) or False, update_config=lambda c: None)
        page.server = types.SimpleNamespace(send_arrangement=lambda *a: told.append(("mac-to-pc", a)) or False)
        kvm_bridge_win.WindowsApplication._set_arrangement(page, "top")
        self.assertEqual(page._config.mac_return_edge, "top")
        self.assertGreater(page._config.arrangement_set_at, 0)
        self.assertEqual([who for who, _ in told], ["pc-to-mac", "mac-to-pc"])
        self.assertEqual(told[0][1][0], "bottom")

    def test_the_macs_hello_leaves_an_edge_set_here(self):
        # G1 in the report, fixed: a PC set up before pairing keeps the side chosen here, and the
        # Mac's first hello only fills an edge the PC does not hold.
        page, saved = self.page("top", stamp=5000)
        self.learn(page, "left")
        self.assertEqual((page._config.mac_return_edge, page._config.arrangement_set_at), ("top", 5000))

    def test_the_macs_hello_leaves_an_edge_even_a_hello_filled(self):
        # The Mac's announced arrangement, not its next hello, is what moves it after that.
        page, saved = self.page("left", stamp=0)
        self.learn(page, "bottom")
        self.assertEqual(page._config.mac_return_edge, "left")

    def test_a_fresh_config_holds_no_edge_though_the_crossing_page_offers_right(self):
        # Pinned known gap (G3 in the report).
        self.assertEqual(default_config().mac_return_edge, "")


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class WaysReachTheDesignPageTests(unittest.TestCase):
    def test_changing_a_way_in_moves_the_design_preview(self):
        config = default_config()
        looked = []
        page = types.SimpleNamespace(
            _config=config, _persist=lambda: True, sender=types.SimpleNamespace(update_config=lambda config: None),
            _reflect_ways=lambda: None, _reflect_look=lambda: looked.append(tuple(config.crossing_methods)),
        )
        kvm_bridge_win.WindowsApplication._ways_changed(page, "corner", True)
        kvm_bridge_win.WindowsApplication._set_part(page, "start", True)
        self.assertEqual(len(looked), 2)
        self.assertIn("corner", looked[0])
