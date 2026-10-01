"""This PC's ways in on each arrangement, and the arrangement as both machines hold it. The two-
machine cases, which need the Mac's code too, are in mac_app/tests/test_crossing_matrix.py.  Since 1.5.0 a way in is a zone
of the peer's (WIRE.md section 8): an edge zone is the peer's whole side, a part zone some thirds
of it, a corner zone names its corner and the edge it crosses, whatever the side is."""

import itertools
import json
import logging
import types
import unittest
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app_config
from app_config import default_config
from core import protocol
from core.receiver import corner_offset
from core.return_edge import CORNERS, EDGES, OPPOSITE
from core.tests import responder_harness as harness
from links_rig import B, Rig, edge_zone, make_config

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


def corner_edges(corner):
    return corner.split("_")


def zones_for(methods, corner, corner_edge, parts):
    zones = []
    if "edge" in methods:
        zones.append(edge_zone(B))
    if "part" in methods:
        zones.append({"peer": protocol.id_text(B), "kind": "part", "parts": list(parts)})
    if "corner" in methods:
        zones.append({"peer": protocol.id_text(B), "kind": "corner", "corner": corner, "edge": corner_edge})
    return zones


def make_rig(methods, side, corner="top_left", corner_edge="left", parts=("middle",), **more):
    more.setdefault("crossing_resistance_px", 40)
    return Rig(
        entries=[harness.entry(B, "Mac", side=side)],
        zones=zones_for(methods, corner, corner_edge, parts),
        crossing_methods=sorted(methods),
        **more,
    )


def push(methods, side, probe, corner="top_left", corner_edge="left", parts=("middle",)):
    """The focus message a push sends, or None when the push never crosses."""
    rig = make_rig(methods, side, corner, corner_edge, parts)
    _, x, y, dx, dy = probe[:5]
    rig.desktop.cursor = (x, y)
    for _ in range(12):
        rig.sender.on_motion(dx, dy)
        if rig.sender.redirecting:
            break
    if not rig.sender.redirecting:
        return None
    return rig.sent(B, "focus")[-1]["data"]


def expected(methods, side, corner, corner_edge, parts, probe):
    """The edge of the peer a push arrives at. An edge zone is the whole side the peer is on and a
    part zone its thirds; a corner zone is one corner of any of the four and crosses the edge it
    names, whatever side the peer is on."""
    _, _, _, _, _, kind, where, third = probe

    def by_edge(edge, part):
        if edge != side:
            return None
        return OPPOSITE[side] if "edge" in methods or ("part" in methods and part in parts) else None

    if kind == "edge":
        return by_edge(where, third)
    vertical, horizontal = where.split("_")
    if kind == "diagonal":
        if "corner" in methods and where == corner:
            return OPPOSITE[corner_edge]
        return by_edge(horizontal, third_at_corner(where, horizontal)) or by_edge(vertical, third_at_corner(where, vertical))
    return by_edge(third, third_at_corner(where, third))


def method_sets():
    for shortcut, pointer, corner in itertools.product((False, True), ("", "edge", "part"), (False, True)):
        methods = {name for name, on in (("shortcut", shortcut), (pointer, bool(pointer)), ("corner", corner)) if on and name}
        if methods:
            yield frozenset(methods)


def corner_choices(methods):
    if "corner" not in methods:
        return [("top_left", "left")]
    return [(corner, edge) for corner in CORNERS for edge in corner_edges(corner)]


def parts_choices(methods):
    if "part" not in methods:
        return [("middle",)]
    return [("middle",), ("start", "end"), ("start", "middle", "end")]


class PcWaysOutTests(unittest.TestCase):
    def test_every_way_crosses_where_it_says_and_nowhere_else(self):
        for methods in method_sets():
            for side in EDGES + ("",):
                for corner, corner_edge in corner_choices(methods):
                    for parts in parts_choices(methods):
                        for probe in probes():
                            focus = push(methods, side, probe, corner, corner_edge, parts)
                            arrived = None if focus is None else focus.get("edge")
                            with self.subTest(methods=sorted(methods), mac_is=side, corner=corner, corner_edge=corner_edge, parts=parts, probe=probe[0]):
                                self.assertEqual(arrived, expected(methods, side, corner, corner_edge, parts, probe))

    def test_the_shortcut_alone_leaves_every_edge_a_wall(self):
        for side in EDGES:
            for probe in probes():
                self.assertIsNone(push(("shortcut",), side, probe), probe[0])

    def test_the_peer_lands_at_the_same_fraction_along_the_far_edge(self):
        for side in EDGES:
            for third, fraction in THIRDS.items():
                probe = (f"{side}/{third}", *side_point(side, third), *outward(side))
                focus = push(("edge",), side, probe)
                self.assertEqual(focus["edge"], OPPOSITE[side])
                self.assertAlmostEqual(focus["offset"], fraction, places=2)

    def test_a_corner_arrives_at_the_end_of_its_crossing_edge_nearest_it(self):
        cases = [
            ("top_left", "left", "right", 0.0), ("bottom_left", "left", "right", 1.0),
            ("top_left", "top", "bottom", 0.0), ("top_right", "top", "bottom", 1.0),
            ("bottom_right", "right", "left", 1.0), ("bottom_left", "bottom", "top", 0.0),
        ]
        for corner, edge, arrives, offset in cases:
            vertical, horizontal = corner.split("_")
            x, y = corner_point(corner)
            probe = (corner, x, y, outward(horizontal)[0], outward(vertical)[1])
            for side in EDGES + ("",):
                focus = push(("corner",), side, probe, corner=corner, corner_edge=edge)
                with self.subTest(corner=corner, edge=edge, side=side):
                    self.assertEqual(focus["edge"], arrives)
                    self.assertAlmostEqual(focus["offset"], offset, places=2)
                    self.assertEqual(focus["offset"], corner_offset(corner, edge))

    def test_a_pc_that_holds_no_side_has_no_edge_or_part_way_out_but_its_corner_still_crosses(self):
        for methods in method_sets():
            for probe in probes():
                focus = push(methods, "", probe)
                if "corner" in methods and probe[0] == "top_left diagonal":
                    self.assertEqual(focus["edge"], "right", (methods, probe[0]))
                else:
                    self.assertIsNone(focus, (methods, probe[0]))


class PcWayOutNamedToThePeerTests(unittest.TestCase):
    def test_a_crossing_names_the_edge_of_the_peer_it_arrives_by_and_the_pcs_resistance(self):
        for side in EDGES:
            probe = ("push", *side_point(side, "middle"), *outward(side))
            focus = push(("edge",), side, probe)
            self.assertEqual(focus["edge"], OPPOSITE[side])
            self.assertEqual(focus["resistance_px"], 40)
            self.assertNotIn("return_edge", focus)

    def test_the_pcs_resistance_is_what_it_asks_the_peer_for_never_the_peers_own(self):
        rig = make_rig(("edge",), "left", crossing_resistance_px=40)
        rig.sender.update_config(make_config(crossing_resistance_px=200))
        self.assertEqual(rig.sender._resistance(), 200)
        rig.desktop.cursor = (0, 500)
        rig.push(4)
        self.assertFalse(rig.sender.redirecting, "80 pixels is under the new 200 of resistance")
        rig.push(8)
        self.assertTrue(rig.sender.redirecting)
        self.assertEqual(rig.sent(B, "focus")[-1]["data"]["resistance_px"], 200)

    def test_a_resistance_the_peer_reported_is_never_read(self):
        rig = make_rig(("edge",), "left", crossing_resistance_px=40, mac_resistance_px=5)
        self.assertEqual(rig.sender._resistance(), 40)
        rig.desktop.cursor = (0, 500)
        rig.push(1)
        self.assertFalse(rig.sender.redirecting)


class WhatIsSharedTests(unittest.TestCase):
    def test_the_crossing_page_shares_only_the_side_the_resistance_stays_each_machines_own(self):
        import pages_win

        line = pages_win.SCOPE["crossing"]
        self.assertNotIn("Two are shared", line)
        self.assertIn("Only which side the other machine is on is shared", line)


try:
    import kvm_bridge_win
except ImportError:  # PySide6 is only in the Windows venv
    kvm_bridge_win = None


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class ArrangementHeldByBothTests(unittest.TestCase):
    """`_on_arrangement` is how a peer's idea of where the machines are reaches this PC: a stamped
    message over either link, settled by stamp and then by id (section 8)."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        from core import protocol
        from core.tests import responder_harness as harness

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "settings.json"
        self.peer = protocol.id_text(harness.B)
        self.own = protocol.id_text(harness.HERE)
        settings = app_config.migrate(None, machine_id=self.own)
        settings["peers"] = [harness.entry(harness.B, "Mac", side="right", side_set_at=100, side_by=self.own)]
        app_config.write_settings(self.path, settings)
        self.refreshed = []
        self.page = types.SimpleNamespace(
            _config=default_config(), config_path=self.path, _on_alert=lambda title, message: None,
            _pull_peer_fields=lambda: self.refreshed.append("pulled"),
            sender=types.SimpleNamespace(refresh=lambda: self.refreshed.append("sender")),
            server=types.SimpleNamespace(peers_changed=lambda: self.refreshed.append("server")),
        )

    def arrive(self, edge, stamp, by=None):
        kvm_bridge_win.WindowsApplication._on_arrangement(self.page, self.peer, edge, stamp, by or self.peer)

    def side(self):
        return json.loads(self.path.read_text(encoding="utf-8"))["peers"][0]["side"]

    def test_a_newer_arrangement_is_applied_as_this_pcs_opposite_edge(self):
        self.arrive("top", 200)
        self.assertEqual(self.side(), "bottom")
        self.assertEqual(self.refreshed, ["pulled", "sender", "server"])

    def test_an_older_arrangement_is_ignored(self):
        self.arrive("top", 50)
        self.assertEqual(self.side(), "right")
        self.assertEqual(self.refreshed, [])

    def test_the_same_arrangement_over_the_second_link_changes_nothing_and_logs_nothing(self):
        self.arrive("top", 200)
        self.refreshed.clear()
        with self.assertNoLogs(kvm_bridge_win.LOGGER, logging.INFO):
            self.arrive("top", 200)
        self.assertEqual(self.refreshed, [])

    def test_a_change_made_here_is_stamped_and_sent_to_the_first_peer(self):
        from core import protocol
        from core.tests import responder_harness as harness

        config = default_config()
        config.mac_return_edge, config.machine_id = "right", self.own
        told = []
        page = types.SimpleNamespace(
            _config=config, _persist=lambda: True, _first_peer=lambda: harness.B,
            _send_to=lambda peer, message: told.append((peer, message)) or False,
            sender=types.SimpleNamespace(update_config=lambda config: None),
            _reflect_look=lambda: None, _reflect_ways=lambda: None,
        )
        kvm_bridge_win.WindowsApplication._set_arrangement(page, "top")
        self.assertEqual(config.mac_return_edge, "top")
        self.assertGreater(config.arrangement_set_at, 0)
        peer, message = told[0]
        self.assertEqual(peer, harness.B)
        self.assertEqual(message["data"], {"edge": "top", "set_at": config.arrangement_set_at, "by": self.own})

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
