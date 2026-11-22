"""core/ways.py: a machine's zones read and written one peer at a time, the side settled with each
peer, the machine the shortcut picks, and the pairings a zone needs (WIRE.md sections 5 and 8)."""

import itertools
import time
import unittest

from core import crossing_state, protocol, ways

HERE = protocol.id_text(bytes(range(1, 17)))
A = protocol.id_text(bytes(range(20, 36)))
B = protocol.id_text(bytes(range(40, 56)))
C = protocol.id_text(bytes(range(60, 76)))
PHONE = protocol.id_text(bytes(range(80, 96)))


def peer(ident, name, **fields):
    data = {"id": ident, "name": name, "platform": "windows", "host": "10.9.9.9", "port": 24820, "hw": "",
            "send": True, "allow_drive": True, "side": "", "side_set_at": 0, "side_by": "", "paired_with": [],
            "paired_at": 1, "linked": True, "from_1_4": False, "token": "k" + ident}
    data.update(fields)
    return data


def settings(*peers, zones=()):
    return {"machine_id": HERE, "shortcut": True, "peers": list(peers), "zones": list(zones)}


def mac_corner_edge(corner, side):
    return corner.split("_")[1]


def pc_corner_edge(corner, side):
    return side if side in ways.EDGES else corner.split("_")[1]


MAC = ("edge", "part", "corner", "notch")
PC = ("edge", "part", "corner")


class ReadingOnePeer(unittest.TestCase):
    def test_new_edges_and_corners_require_the_legacy_corner_keys_to_agree(self):
        held = settings(peer(A, "Ay", ways={
            "edge": True, "thirds": list(ways.PARTS), "edges": ["bottom", "left"],
            "corner": True, "corner_at": "top_left", "corners": ["top_left", "bottom_right"],
            "notch": False,
        }))
        self.assertEqual(ways.ways(held, A)["edges"], ["left", "bottom"])
        self.assertEqual(ways.ways(held, A)["corners"], ["top_left", "bottom_right"])

        held["peers"][0]["ways"]["corner_at"] = "top_right"
        found = ways.ways(held, A)
        self.assertEqual(found["corners"], ["top_right"])

    def test_152_zones_migrate_as_one_corner_without_new_keys(self):
        held = settings(peer(A, "Ay", ways={
            "edge": True, "thirds": list(ways.PARTS), "corner": True,
            "corner_at": "bottom_left", "notch": False,
        }))
        found = ways.ways(held, A)
        self.assertEqual(found["corners"], ["bottom_left"])
        self.assertEqual(found["edges"], [])

    def test_a_peer_with_no_zones_reads_as_nothing_in_use_and_the_defaults(self):
        found = ways.ways(settings(peer(A, "Ay", side="left")), A)
        self.assertEqual(found, {"side": "left", "methods": ["edge"],
                                 "parts": ["start", "middle", "end"], "corner": "top_left",
                                 "edges": [], "corners": []})

    def test_only_that_peers_zones_in_use_count(self):
        held = settings(peer(A, "Ay", side="left"), peer(B, "Bee", side="right"), zones=[
            {"peer": A, "kind": "edge"},
            {"peer": A, "kind": "corner", "corner": "bottom_left", "edge": "left", "off": True},
            {"peer": B, "kind": "part", "parts": ["start", "end"]},
            {"peer": B, "kind": "corner", "corner": "top_right", "edge": "right"},
        ])
        self.assertEqual(ways.ways(held, A), {"side": "left", "methods": ["edge"], "parts": ["start", "middle", "end"],
                                              "corner": "bottom_left", "edges": [], "corners": []})
        self.assertEqual(ways.ways(held, B), {"side": "right", "methods": ["part", "corner"], "parts": ["start", "end"],
                                              "corner": "top_right", "edges": [], "corners": ["top_right"]})

    def test_an_unknown_peer_reads_as_nothing(self):
        self.assertEqual(ways.ways(settings(), A)["methods"], [])


class PageOffZones(unittest.TestCase):
    def edit(self, held, methods):
        return ways.edit(held, A, side="right", methods=methods, parts=[], corner="top_left", kinds=MAC,
                         corner_edge=mac_corner_edge, now=30)

    def contact(self, *, peer_y=0, own_displays=False, peer_displays=False):
        own = {"x": 0, "y": 0, "w": 160, "h": 100}
        other = {"x": 160, "y": peer_y, "w": 160, "h": 100}
        if own_displays:
            own["displays"] = {"main": {"x": 0, "y": 0, "w": 160, "h": 100}}
        if peer_displays:
            other["displays"] = {"main": {"x": 0, "y": 0, "w": 160, "h": 100}}
        return {HERE: own, A: other}

    def test_unticking_edge_turns_off_unscoped_and_display_scoped_edge_zones(self):
        held = settings(peer(A, "Ay", side="right"), zones=[{"peer": A, "kind": "edge"}])
        held["layout"] = self.contact(own_displays=True, peer_displays=True)

        self.edit(held, ["shortcut"])

        self.assertEqual(ways.ways(held, A)["methods"], [])
        self.assertEqual(held["zones"], [])


        self.assertFalse(ways.has_way(held, A))
        self.assertFalse(ways.crosses_on_map(held, A))

        self.edit(held, ["edge"])
        zone = held["zones"][0]
        self.assertEqual((zone["kind"], zone["display"], zone["side"]), ("edge", "main", "right"))
        self.assertTrue(ways.has_way(held, A))

    def test_page_does_not_turn_on_a_stale_scoped_zone(self):
        held = settings(peer(A, "Ay", side="right"), zones=[
            {"peer": A, "kind": "edge", "display": "0", "side": "right", "off": True},
        ])

        self.edit(held, ["edge"])

        self.assertEqual(ways.ways(held, A)["methods"], ["edge"])
        self.assertEqual(held["zones"][0]["kind"], "edge")
        self.assertNotIn("off", held["zones"][0])

    def test_compile_layout_keeps_a_page_off_scoped_zone_off(self):
        held = settings(peer(A, "Ay", side="right"), zones=[
            {"peer": A, "kind": "edge", "display": "main", "side": "right", "off": True, "page_off": True},
        ])
        layout = {
            HERE: {"x": 0, "y": 0, "w": 160, "h": 100,
                   "displays": {"main": {"x": 0, "y": 0, "w": 160, "h": 100}}},
            A: {"x": 160, "y": 0, "w": 160, "h": 100,
                "displays": {"main": {"x": 160, "y": 0, "w": 160, "h": 100}}},
        }

        result = ways.compile_layout(held, layout)

        self.assertTrue(result["accepted"])
        self.assertEqual(result["settings"]["peers"][0]["ways"]["edge"], False)
        self.assertEqual(result["settings"]["zones"], [])

    def test_unticked_edge_survives_compile_with_or_without_display_metadata(self):
        for scoped in (False, True):
            for own_displays in (False, True):
                for peer_displays in (False, True):
                    for y in (0, 50):
                        with self.subTest(scoped=scoped, own_displays=own_displays,
                                          peer_displays=peer_displays, y=y):
                            zone = {"peer": A, "kind": "edge", "off": True} if scoped else {"peer": A, "kind": "edge"}
                            held = settings(peer(A, "Ay", side="right"), zones=[zone])
                            layout = self.contact(peer_y=y, own_displays=own_displays, peer_displays=peer_displays)
                            held["layout"] = layout
                            self.edit(held, ["shortcut"])

                            for _ in range(2):
                                result = ways.compile_layout(held, layout, moved=A)
                                self.assertTrue(result["accepted"])
                                held = result["settings"]
                                self.assertFalse(ways.crosses_on_map(held, A))
                                self.assertNotIn("edge", ways.ways(held, A)["methods"])
                                self.assertNotIn("part", ways.ways(held, A)["methods"])
                                self.assertEqual(held["zones"], [])

                            self.edit(held, ["edge"])
                            self.assertTrue(ways.crosses_on_map(held, A))
                            self.assertTrue(all("page_off" not in zone and "off" not in zone
                                                for zone in held["zones"]))

    def test_a_page_off_mark_goes_with_a_contact_the_tile_has_left(self):
        held = settings(peer(A, "Ay", side="top"), zones=[
            {"peer": A, "kind": "edge", "display": "main", "side": "top", "off": True, "page_off": True},
        ])
        layout = {
            HERE: {"x": 0, "y": 0, "w": 160, "h": 100,
                   "displays": {"main": {"x": 0, "y": 0, "w": 160, "h": 100}}},
            A: {"x": 160, "y": 0, "w": 160, "h": 100,
                "displays": {"main": {"x": 160, "y": 0, "w": 160, "h": 100}}},
        }

        compiled = ways.compile_layout(held, layout)["settings"]
        self.edit(compiled, ["shortcut"])
        self.edit(compiled, ["edge"])

        self.assertEqual(ways.ways(compiled, A)["methods"], ["edge"])
        self.assertTrue(all(zone.get("side") != "top" for zone in compiled["zones"]))

    def test_unticked_edge_survives_display_metadata_disappearing_and_returning(self):
        held = settings(peer(A, "Ay", side="right"), zones=[
            {"peer": A, "kind": "edge", "display": "main", "side": "right"},
        ])
        self.edit(held, ["shortcut"])
        for display in ("main", None, "replacement", None):
            layout = self.contact(own_displays=display is not None)
            if display not in (None, "main"):
                layout[HERE]["displays"] = {display: {"x": 0, "y": 0, "w": 160, "h": 100}}
            held = ways.compile_layout(held, layout)["settings"]
            self.assertFalse(ways.crosses_on_map(held, A))
            self.assertEqual(held["zones"], [])

    def test_compile_without_display_metadata_does_not_record_a_page_untick_for_a_scoped_corner(self):
        held = settings(peer(A, "Ay", side="right"), zones=[
            {"peer": A, "kind": "corner", "display": "main", "side": "right",
             "corner": "top_right", "edge": "right", "off": True, "aside": True},
        ])
        held["layout"] = self.contact(own_displays=True, peer_displays=True)
        held["peers"][0]["ways"] = {"edge": True, "thirds": list(ways.PARTS), "corner": True,
                                      "corner_at": "top_right", "notch": False}
        layout = self.contact()
        compiled = ways.compile_layout(held, layout)["settings"]
        corner = next(zone for zone in compiled["zones"] if zone["kind"] == "corner")
        self.assertEqual((corner["kind"], corner["corner"], corner["side"]), ("corner", "top_right", "right"))
        self.assertEqual(compiled["peers"][0]["ways"]["corner_at"], "top_right")
        self.assertNotIn("page_off", corner)

    def test_unscoped_page_off_mark_leaves_when_the_contact_changes_side(self):
        held = settings(peer(A, "Ay", side="right"), zones=[{"peer": A, "kind": "edge"}])
        self.edit(held, ["shortcut"])
        self.assertEqual(held["peers"][0]["ways"]["edge"], False)
        held["layout"] = self.contact()
        self.edit(held, ["edge"])
        self.assertEqual(ways.ways(held, A)["methods"], ["edge"])
        self.assertTrue(ways.crosses_on_map(held, A))

    def test_a_scoped_zone_set_aside_stays_off_once_the_page_turns_its_way_off(self):
        held = settings(peer(A, "Ay", side="right"), zones=[
            {"peer": A, "kind": "edge", "display": "0", "side": "right", "off": True, "aside": True},
        ])

        self.edit(held, ["shortcut"])
        before = held["peers"][0]["ways"].copy()
        self.assertEqual(ways.settle_presence(held, "this Mac", lambda _peer: True, lambda _peer: False), [])
        self.assertEqual(held["peers"][0]["ways"], before)
        self.assertEqual(held["zones"], [])

    def test_notch_does_not_light_the_map_contact(self):
        held = settings(peer(A, "Ay", side="right"), zones=[{"peer": A, "kind": "notch"}])
        held["peers"][0]["ways"] = {"edge": False, "thirds": list(ways.PARTS), "corner": False,
                                      "corner_at": "top_left", "notch": True}

        self.assertTrue(ways.has_way(held, A))
        self.assertFalse(ways.crosses_on_map(held, A))

    def test_compiling_an_unticked_edge_keeps_the_notch_in_use(self):
        held = settings(peer(A, "Ay", side="right"), zones=[{"peer": A, "kind": "edge"}])
        held["peers"][0]["ways"] = {"edge": False, "thirds": list(ways.PARTS), "corner": False,
                                      "corner_at": "top_left", "notch": True}
        layout = self.contact()
        held["layout"] = layout
        compiled = ways.compile_layout(held, layout, platform="mac")["settings"]
        self.assertFalse(ways.crosses_on_map(compiled, A))
        self.assertTrue(ways.has_way(compiled, A))
        self.assertIn("notch", ways.ways(compiled, A)["methods"])


class WrapAroundWays(unittest.TestCase):
    def test_wrap_edge_uses_free_thirds_and_records_the_map_entry(self):
        held = settings(peer(A, "Ay", side="right", ways={
            "edge": True, "thirds": list(ways.PARTS), "edges": ["left"],
            "corner": False, "corner_at": "top_left", "corners": [], "notch": False,
        }))
        layout = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                  A: {"x": 160, "y": 0, "w": 160, "h": 100}}

        out = ways.compile_layout(held, layout, capable={HERE, A}, now=100)

        zone = next(z for z in out["settings"]["zones"] if z["side"] == "left")
        self.assertEqual((zone["kind"], zone.get("parts"), zone.get("target_side")),
                         ("edge", None, None))
        self.assertEqual(out["tiles"][A]["wraps"], [
            {"display": None, "side": "left", "parts": list(ways.PARTS)}])

    def test_wrap_target_uses_the_outermost_peer_display_and_overlap_tie_break(self):
        for displays, expected in (
                ({"main": {"x": 0, "y": 0, "w": 160, "h": 100}}, "main"),
                ({"a": {"x": 0, "y": 0, "w": 160, "h": 50},
                  "b": {"x": 0, "y": 50, "w": 160, "h": 50}}, "a"),
                ({"a": {"x": 0, "y": 0, "w": 80, "h": 100},
                  "b": {"x": 80, "y": 0, "w": 80, "h": 100}}, "b")):
            with self.subTest(expected=expected):
                held = settings(peer(A, "Ay", side="right", ways={
                    "edge": True, "thirds": list(ways.PARTS), "edges": ["left"],
                    "corner": False, "corner_at": "top_left", "corners": [], "notch": False,
                }))
                held["layout"] = {
                    HERE: {"x": 0, "y": 0, "w": 160, "h": 100,
                           "displays": {"own": {"x": 0, "y": 0, "w": 160, "h": 100}}},
                    A: {"x": 160, "y": 0, "w": 160, "h": 100, "displays": displays},
                }
                out = ways.compile_layout(held, held["layout"], capable={HERE, A}, now=100)
                zone = next(z for z in out["settings"]["zones"] if z.get("side") == "left")
                self.assertEqual((zone["target_display"], zone["target_side"]), (expected, "right"))

    def test_wrap_edge_does_not_use_an_internal_display_edge(self):
        held = settings(peer(A, "Ay", side="right", ways={
            "edge": True, "thirds": list(ways.PARTS), "edges": ["left"],
            "corner": False, "corner_at": "top_left", "corners": [], "notch": False,
        }))
        held["layout"] = {
            HERE: {"x": 0, "y": 0, "w": 160, "h": 100,
                   "displays": {"left": {"x": 0, "y": 0, "w": 80, "h": 100},
                                "right": {"x": 80, "y": 0, "w": 80, "h": 100}}},
            A: {"x": 160, "y": 0, "w": 160, "h": 100},
        }
        out = ways.compile_layout(held, held["layout"], capable={HERE, A}, now=100)
        self.assertEqual(out["tiles"][A]["wraps"], [
            {"display": "left", "side": "left", "parts": list(ways.PARTS)}])

    def test_reach_reports_contact_holders_and_old_or_missing_tiles(self):
        held = settings(peer(A, "Ay", side="right", ways={
            "edge": True, "thirds": list(ways.PARTS), "edges": ["left"],
            "corner": False, "corner_at": "top_left", "corners": [], "notch": False,
        }), peer(B, "Bee", side="left"))
        held["layout"] = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                          A: {"x": 160, "y": 0, "w": 160, "h": 100},
                          B: {"x": -160, "y": 0, "w": 160, "h": 100}}
        held["peers"][0]["ways"]["edges"] = []
        reach = ways.reach(held, A, platform="pc", capable={HERE, A, B})
        self.assertEqual(reach["sides"]["left"], {"state": "held", "by": B})
        self.assertEqual(reach["sides"]["right"], {"state": "contact", "by": A})
        held["peers"][0]["caps"] = []
        self.assertEqual(ways.reach(held, A, platform="pc", capable={HERE, B})["sides"]["left"],
                         {"state": "unavailable", "by": None})
        self.assertEqual(ways.reach(held, A, platform="pc", capable={HERE, A, B})["sides"]["right"],
                         {"state": "contact", "by": A}, "an old peer keeps its contact chip, to turn Edge off")
        held["layout"].pop(A)
        self.assertEqual(ways.reach(held, A, platform="pc", capable={HERE, A, B})["sides"]["right"],
                         {"state": "contact", "by": A}, "no tile yet: placed by its side, as the compile does")
        held["peers"][0].pop("caps")
        held["layout"][A] = {"x": 40, "y": 0, "w": 160, "h": 100}
        self.assertTrue(all(item == {"state": "unavailable", "by": None}
                            for item in ways.reach(held, A, platform="pc", capable={HERE, A, B})["sides"].values()))

    def test_reach_uses_the_compiled_contact_and_wrap_assignment(self):
        held = settings(peer(A, "Ay", side="right", ways={
            "edge": True, "thirds": list(ways.PARTS), "edges": ["left"],
            "corner": True, "corner_at": "top_left", "corners": ["top_left"], "notch": False,
        }))
        held["layout"] = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                          A: {"x": 160, "y": 0, "w": 160, "h": 100}}
        reach = ways.reach(held, A, platform="mac", capable={HERE, A})
        self.assertEqual(set(reach), {"sides", "corners"})
        self.assertTrue(all(set(item) == {"state", "by"}
                            for section in reach.values() for item in section.values()))
        self.assertEqual(reach["sides"]["right"], {"state": "contact", "by": A})
        self.assertEqual(reach["sides"]["left"], {"state": "on", "by": None})
        self.assertEqual(reach["sides"]["top"], {"state": "free", "by": None})
        self.assertEqual(reach["sides"]["bottom"], {"state": "free", "by": None})
        self.assertEqual(reach["corners"]["top_left"], {"state": "on", "by": None})
        self.assertEqual(reach["corners"]["top_right"], {"state": "free", "by": None})

        held["peers"][0]["ways"]["edge"] = False
        reach = ways.reach(held, A, platform="mac", capable={HERE, A},
                           live=lambda _peer: True, gone=lambda _peer: False)
        self.assertEqual(reach["sides"]["left"], {"state": "kept", "by": None})

    def test_a_contact_holds_its_side_against_wraps_even_with_its_edge_off(self):
        held = settings(peer(A, "Ay", side="right", ways={
            "edge": True, "thirds": list(ways.PARTS), "edges": ["left"],
            "corner": False, "corner_at": "top_left", "corners": [], "notch": False,
        }), peer(B, "Bee", side="left", ways={
            "edge": False, "thirds": list(ways.PARTS), "edges": [],
            "corner": False, "corner_at": "top_left", "corners": [], "notch": False,
        }))
        held["layout"] = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                          A: {"x": 160, "y": 0, "w": 160, "h": 100},
                          B: {"x": -160, "y": 0, "w": 160, "h": 100}}
        out = ways.compile_layout(held, held["layout"], capable={HERE, A, B}, now=100)
        self.assertEqual(out["tiles"][A]["wraps"], [])
        self.assertEqual(ways.reach(held, A, platform="pc", capable={HERE, A, B})["sides"]["left"],
                         {"state": "kept", "by": None})
        held["peers"][0]["ways"]["edges"] = []
        self.assertEqual(ways.reach(held, A, platform="pc", capable={HERE, A, B})["sides"]["left"],
                         {"state": "held", "by": B})

    def test_reach_places_a_sideless_tileless_peer_and_answers_a_not_in_use_row_as_put_back(self):
        held = settings(peer(A, "Ay", ways={
            "edge": True, "thirds": list(ways.PARTS), "edges": ["left"],
            "corner": False, "corner_at": "top_left", "corners": [], "notch": False,
        }))
        held["layout"] = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100}}
        reach = ways.reach(held, A, platform="pc", capable={HERE, A})
        self.assertIn("contact", {item["state"] for item in reach["sides"].values()})
        held["layout"][A] = {"x": 160, "y": 0, "w": 160, "h": 100}
        held["peers"][0]["in_use"] = False
        reach = ways.reach(held, A, platform="pc", capable={HERE, A})
        self.assertEqual(reach["sides"]["right"], {"state": "contact", "by": A})
        self.assertEqual(reach["sides"]["left"], {"state": "on", "by": None})

    def test_an_overlapping_peer_gets_no_wrap_zones(self):
        held = settings(peer(A, "Ay", side="right", ways={
            "edge": True, "thirds": list(ways.PARTS), "edges": ["left"],
            "corner": False, "corner_at": "top_left", "corners": [], "notch": False,
        }))
        held["layout"] = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                          A: {"x": 100, "y": 0, "w": 160, "h": 100}}
        out = ways.compile_layout(held, held["layout"], capable={HERE, A}, now=100)
        self.assertEqual([zone for zone in out["settings"]["zones"] if zone["peer"] == A], [])

    def test_a_wrap_lands_on_the_display_at_the_same_height_as_a_fraction_of_each_machine(self):
        frames = [{"display": "a", "x": 0, "y": 100, "w": 100, "h": 50},
                  {"display": "z", "x": 0, "y": 150, "w": 100, "h": 50}]
        leaving = {"display": "0", "x": 300, "y": 50, "w": 100, "h": 50}
        own_tile = {"x": 300, "y": 0, "w": 100, "h": 100}
        self.assertEqual(ways._wrap_target(frames, "right", leaving, own_tile)["display"], "z")

    def test_the_one_corner_form_refuses_a_held_corner_without_a_write(self):
        held = settings(peer(A, "Ay", side="right", ways={
            "edge": True, "thirds": list(ways.PARTS), "edges": [],
            "corner": True, "corner_at": "top_right", "corners": ["top_right"], "notch": False,
        }), peer(B, "Bee", side="left", ways={
            "edge": True, "thirds": list(ways.PARTS), "edges": [],
            "corner": True, "corner_at": "bottom_left", "corners": ["bottom_left"], "notch": False,
        }))
        held["layout"] = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                          A: {"x": 160, "y": 0, "w": 160, "h": 100},
                          B: {"x": -160, "y": 0, "w": 160, "h": 100}}
        out = crossing_state.edit_ways(held, A, platform="pc", corner=True, corner_at="bottom_left",
                                       live=None, gone=None, now=100, capable={HERE, A, B})
        self.assertFalse(out["changed"])
        self.assertEqual(ways.ways(out["settings"], A)["corners"], ["top_right"])

    def test_reach_shows_a_left_out_rows_kept_ways_and_a_corner_held_only_by_intent(self):
        held = settings(peer(A, "Ay", side="right", ways={
            "edge": True, "thirds": list(ways.PARTS), "edges": ["left"],
            "corner": True, "corner_at": "top_left", "corners": ["top_left"], "notch": False,
        }), peer(B, "Bee", ways={
            "edge": True, "thirds": list(ways.PARTS), "edges": [],
            "corner": True, "corner_at": "bottom_right", "corners": ["bottom_right"], "notch": False,
        }))
        held["layout"] = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                          A: {"x": 160, "y": 0, "w": 160, "h": 100},
                          B: {"x": 0, "y": 300, "w": 160, "h": 100}}
        held["peers"][0]["left_out"] = True
        reach = ways.reach(held, A, platform="pc", capable={HERE, A, B})
        self.assertEqual(reach["sides"]["right"], {"state": "contact", "by": A})
        self.assertEqual(reach["sides"]["left"], {"state": "on", "by": None})
        self.assertEqual(reach["corners"]["top_left"], {"state": "on", "by": None})
        self.assertEqual(reach["corners"]["bottom_right"], {"state": "held", "by": B})
        self.assertEqual(reach["sides"]["right"], {"state": "contact", "by": A})
        unavailable = ways.reach(held, A, platform="mac", capable={HERE, A}, gone=lambda _peer: True)
        self.assertTrue(all(item == {"state": "unavailable", "by": None}
                            for section in unavailable.values() for item in section.values()))

    def test_corner_screen_measures_left_and_top_from_the_tile_vertex(self):
        screens = [
            {"display": "far", "x": 500, "y": 500, "w": 100, "h": 100},
            {"display": "near", "x": 1000, "y": 1000, "w": 100, "h": 100},
        ]
        tile = {"x": 1000, "y": 1000, "w": 200, "h": 200}

        self.assertEqual(ways._corner_screen(screens, "top_left", tile)["display"], "near")

    def test_reach_marks_corners_unavailable_without_a_side_contact(self):
        held = settings(peer(A, "Ay", ways={
            "edge": True, "thirds": list(ways.PARTS), "edges": [],
            "corner": False, "corner_at": "top_left", "corners": [], "notch": False,
        }), peer(B, "Bee", side="right", ways={
            "edge": True, "thirds": list(ways.PARTS), "edges": [],
            "corner": True, "corner_at": "top_left", "corners": ["top_left"], "notch": False,
        }))
        held["layout"] = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                          A: {"x": 400, "y": 0, "w": 160, "h": 100},
                          B: {"x": 160, "y": 0, "w": 160, "h": 100}}

        reach = ways.reach(held, A, platform="pc", capable={HERE, A, B})

        self.assertEqual(reach["corners"]["top_left"], {"state": "unavailable", "by": None})


class LeavingOut(unittest.TestCase):
    def test_leave_out_turns_every_zone_off_and_put_back_restores_only_the_recorded_kinds(self):
        held = settings(peer(A, "Ay", side="right"), zones=[
            {"peer": A, "kind": "edge"},
            {"peer": A, "kind": "part", "parts": ["start"], "off": True},
            {"peer": A, "kind": "corner", "corner": "bottom_left", "off": True},
        ])

        held["layout"] = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                           A: {"x": 160, "y": 0, "w": 160, "h": 100}}
        ways.leave_out(held, A)

        self.assertTrue(held["peers"][0]["left_out"])
        self.assertNotIn("left_out_ways", held["peers"][0])
        self.assertEqual(held["peers"][0]["ways"], {"edge": True, "thirds": list(ways.PARTS),
                                                      "edges": [], "corner": False, "corner_at": "bottom_left",
                                                      "corners": [], "notch": False})
        self.assertEqual(held["zones"], [])
        self.assertFalse(ways.has_way(held, A))

        ways.put_back(held, A)

        self.assertNotIn("left_out", held["peers"][0])
        self.assertNotIn("left_out_ways", held["peers"][0])
        self.assertEqual(held["peers"][0]["ways"]["edge"], True)
        self.assertEqual(held["peers"][0]["ways"]["thirds"], list(ways.PARTS))
        self.assertTrue(ways.has_way(held, A))

    def test_left_out_peer_arrangement_has_no_way_back_and_restoring_zones_restores_it(self):
        held = settings(peer(A, "Ay", side="left"), zones=[{"peer": A, "kind": "edge"}])

        ways.leave_out(held, A)
        self.assertFalse(ways.has_way(held, A))
        message = protocol.arrangement_v6("right", 1, protocol.read_id(HERE), way_back=ways.has_way(held, A))
        self.assertIs(message["data"]["way_back"], False)
        self.assertFalse(ways.way_back_state(held)[A][0])
        ways.put_back(held, A)
        self.assertTrue(ways.has_way(held, A))
        message = protocol.arrangement_v6("right", 1, protocol.read_id(HERE), way_back=ways.has_way(held, A))
        self.assertIs(message["data"]["way_back"], True)
        self.assertTrue(ways.way_back_state(held)[A][0])

    def test_leave_out_records_zones_temporarily_set_aside(self):
        held = settings(peer(A, "Ay"), zones=[{"peer": A, "kind": "edge", "off": True, "aside": True}])
        held["layout"] = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                           A: {"x": 160, "y": 0, "w": 160, "h": 100}}

        ways.leave_out(held, A)

        # A way set aside while its machine slept was still in use; Put back brings it back.
        self.assertEqual(held["peers"][0]["ways"]["edge"], True)

        ways.put_back(held, A)

        self.assertEqual(held["peers"][0]["ways"]["edge"], True)
        self.assertTrue(ways.has_way(held, A))
        self.assertEqual(held["zones"], [{"peer": A, "kind": "edge", "side": "right",
                                          "contact_span": [0.0, 1.0], "target_span": [0.0, 1.0]}])


class InactiveZones(unittest.TestCase):
    def overlapping(self, inactive):
        held = settings(
            peer(A, "Ay", side="right", **inactive), peer(B, "Bee", side="right"),
            zones=[{"peer": A, "kind": "edge"}],
        )
        held["layout"] = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                           A: {"x": 160, "y": 0, "w": 160, "h": 100},
                           B: {"x": 160, "y": 0, "w": 160, "h": 100}}
        return held

    def test_left_out_and_not_in_use_zones_do_not_clash(self):
        for inactive in ({"left_out": True}, {"in_use": False}):
            with self.subTest(inactive=inactive):
                held = ways.compile_layout(self.overlapping(inactive), self.overlapping(inactive)["layout"])["settings"]
                self.assertEqual([zone["peer"] for zone in held["zones"]], [B])
                self.assertEqual(held["peers"][0]["ways"]["edge"], True)

    def test_inactive_zone_does_not_block_a_missing_zone_in_ensure_zones(self):
        for inactive in ({"left_out": True}, {"in_use": False}):
            with self.subTest(inactive=inactive):
                held = self.overlapping(inactive)
                result = ways.compile_layout(held, held["layout"])
                self.assertNotIn(A, [zone["peer"] for zone in result["settings"]["zones"]])
                self.assertIn(B, [zone["peer"] for zone in result["settings"]["zones"]])

    def test_a_not_in_use_peer_gets_an_off_zone_when_zones_are_ensured(self):
        held = settings(peer(A, "Ay", in_use=False))
        held["layout"] = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                           A: {"x": 160, "y": 0, "w": 160, "h": 100}}
        result = ways.compile_layout(held, held["layout"])
        self.assertEqual(result["settings"]["zones"], [])
        self.assertEqual(result["settings"]["peers"][0]["ways"]["edge"], True)

    def test_a_not_in_use_zone_set_aside_stays_off_when_its_peer_connects(self):
        held = settings(peer(A, "Ay", side="right", in_use=False), zones=[{"peer": A, "kind": "edge"}])
        before = ways.ways(held, A)
        self.assertEqual(ways.settle_presence(held, "this PC", lambda _peer: True, lambda _peer: False), [])
        self.assertEqual(ways.ways(held, A), before)
        self.assertEqual(held["zones"], [{"peer": A, "kind": "edge"}])

    def test_inactive_zone_does_not_make_a_share_offer(self):
        for inactive in ({"left_out": True}, {"in_use": False}):
            with self.subTest(inactive=inactive):
                self.assertIsNone(ways.share_offer(self.overlapping(inactive), B))
                self.assertEqual(ways.share(self.overlapping(inactive), "right",
                                            {"start": B, "middle": B, "end": B}, kinds=MAC), [])

    def test_arrangement_does_not_turn_off_a_zone_for_clashing_with_an_inactive_peer(self):
        for inactive in ({"left_out": True}, {"in_use": False}):
            with self.subTest(inactive=inactive):
                held = self.overlapping(inactive)
                held["peers"][1]["caps"] = ["layout"]
                before = copy_settings = {key: value for key, value in held["layout"].items()}
                changed, notices = ways.arrangement(held, B, "left", 2, B, "this PC")
                self.assertFalse(changed)
                self.assertEqual(notices, [])
                self.assertEqual(held["layout"], before)

    def test_put_back_keeps_restored_zones_off_when_another_peer_took_the_stretch(self):
        held = settings(peer(A, "Ay", side="right"), peer(B, "Bee", side="right"), zones=[{"peer": A, "kind": "edge"}])
        held["layout"] = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                           A: {"x": 160, "y": 0, "w": 160, "h": 100},
                           B: {"x": 160, "y": 0, "w": 160, "h": 100}}
        ways.leave_out(held, A)
        ways.put_back(held, A)
        owners = [zone["peer"] for zone in held["zones"] if zone["kind"] in ("edge", "part")]
        self.assertEqual(len(owners), len(set(owners)))
        self.assertTrue(held["peers"][0]["ways"]["edge"])


class JumpKeys(unittest.TestCase):
    def test_changing_trigger_rejects_keys_used_in_an_existing_jump_chord(self):
        held = settings(peer(A, "Ay", jump_key="ctrl+shift+f13"))
        for trigger in ("ctrl", "ctrl_r", "shift_r", "f13"):
            with self.subTest(trigger=trigger), self.assertRaises(ValueError):
                ways.check_trigger_key(held, trigger)
        ways.check_trigger_key(held, "alt_r")

    def test_missing_jump_key_is_none(self):
        self.assertIsNone(ways.jump_key(peer(A, "Ay")))

    def test_trigger_key_cannot_be_recorded_as_a_jump_key(self):
        held = settings(peer(A, "Ay"))
        with self.assertRaisesRegex(ValueError, "trigger"):
            ways.check_jump_key(held, A, "ctrl+f9", "f9")

    def test_a_chord_with_the_trigger_key_or_its_modifier_is_refused(self):
        # A chord holding the trigger's modifier would start its double-tap or hold, and leave the
        # modifier held here once input had jumped away.
        held = settings(peer(A, "Ay"))
        for value, trigger in (("ctrl+f9", "f9"), ("ctrl+alt+2", "alt_r"), ("alt+2", "alt_r")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "trigger"):
                ways.check_jump_key(held, A, value, trigger)
        self.assertEqual(ways.check_jump_key(held, A, "ctrl+cmd+2", "alt_r"), "ctrl+cmd+2")

    def test_space_is_recorded_by_its_name_not_as_a_blank(self):
        self.assertFalse(ways.valid_jump_key("cmd+ "))
        self.assertEqual(ways.jump_chord(["cmd"], "space"), "cmd+space")

    def test_shift_alone_is_not_enough_because_it_would_swallow_typing(self):
        for value in ("shift+a", "shift+2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ways.check_jump_key(settings(peer(A, "Ay")), A, value, "alt_r")
        self.assertEqual(ways.jump_chord(["shift"], "a"), None)

    def test_bare_modifiers_and_unrecognised_words_are_not_jump_chords(self):
        for value in ("ctrl", "nonsense+2", "shift+ctrl+2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ways.check_jump_key(settings(peer(A, "Ay")), A, value, "alt_r")
        with self.assertRaises(ValueError):
            ways.check_jump_key(settings(peer(A, "Ay")), A, 2, "alt_r")

    def test_shifted_printable_keys_and_plus_have_stable_chords(self):
        self.assertEqual(ways.jump_chord(["ctrl", "shift"], "@"), "ctrl+shift+@")
        self.assertEqual(ways.jump_chord(["ctrl"], "+"), "ctrl+plus")
        self.assertEqual(ways.jump_chord(["ctrl_r"], "2"), "ctrl+2")

    def test_another_peers_jump_key_cannot_be_reused(self):
        held = settings(peer(A, "Ay", jump_key="ctrl+shift+2"), peer(B, "Bee"))
        with self.assertRaisesRegex(ValueError, "already"):
            ways.check_jump_key(held, B, "ctrl+shift+2", "alt_r")

    def test_clearing_a_jump_key_removes_it(self):
        entry = peer(A, "Ay", jump_key="ctrl+shift+2")
        self.assertIsNone(ways.check_jump_key(settings(entry), A, "", "alt_r"))

    def test_a_chord_finds_its_in_use_machine(self):
        held = settings(peer(A, "Ay", jump_key="ctrl+shift+2"), peer(B, "Bee", jump_key="ctrl+shift+3"))
        self.assertEqual(ways.jump_peer(held["peers"], "ctrl+shift+3"), B)

    def test_a_not_in_use_machines_jump_key_is_ignored(self):
        held = settings(peer(A, "Ay", jump_key="ctrl+shift+2", in_use=False))
        self.assertIsNone(ways.jump_peer(held["peers"], "ctrl+shift+2"))


class WritingOnePeer(unittest.TestCase):
    def test_one_zone_of_each_kind_is_written_for_that_peer_and_the_others_are_left_alone(self):
        other = {"peer": B, "kind": "edge"}
        held = settings(peer(A, "Ay"), peer(B, "Bee", side="right"), zones=[other])
        changed = ways.edit(held, A, side="left", methods=["corner"], parts=["start"], corner="top_left",
                            kinds=MAC, corner_edge=mac_corner_edge, now=1000)
        self.assertTrue(changed)
        mine = [zone for zone in held["zones"] if zone["peer"] == A]
        self.assertEqual([zone["kind"] for zone in mine], ["corner"])
        self.assertEqual(next(zone for zone in mine if zone["kind"] == "corner")["edge"], "left")
        self.assertEqual(ways.ways(held, A)["parts"], ["start"])
        self.assertEqual(ways.ways(held, B)["methods"], ["edge"])

    def test_a_new_side_is_stamped_by_this_machine_and_never_below_the_last_stamp(self):
        held = settings(peer(A, "Ay", side="right", side_set_at=5000, side_by=A))
        self.assertTrue(ways.edit(held, A, side="top", methods=["edge"], parts=["middle"], corner="top_left",
                                  kinds=PC, corner_edge=pc_corner_edge, now=100))
        entry = held["peers"][0]
        self.assertEqual((entry["side"], entry["side_set_at"], entry["side_by"]), ("top", 100, HERE))

    def test_an_unchanged_side_keeps_its_stamp_and_says_so(self):
        held = settings(peer(A, "Ay", side="right", side_set_at=5000, side_by=A))
        self.assertFalse(ways.edit(held, A, side="right", methods=["part"], parts=["end"], corner="top_left",
                                   kinds=PC, corner_edge=pc_corner_edge, now=9000))
        self.assertEqual((held["peers"][0]["side_set_at"], held["peers"][0]["side_by"]), (5000, A))

    def test_the_pc_corner_crosses_the_side(self):
        held = settings(peer(A, "Ay"))
        ways.edit(held, A, side="top", methods=["corner"], parts=["middle"], corner="top_left",
                  kinds=PC, corner_edge=pc_corner_edge, now=1)
        corner = next(zone for zone in held["zones"] if zone["kind"] == "corner")
        self.assertEqual(corner["edge"], "top")

    def test_edge_and_its_thirds_both_on_write_the_thirds_off(self):
        held = settings(peer(A, "Ay"))
        ways.edit(held, A, side="left", methods=["edge", "part"], parts=["start"], corner="top_left",
                  kinds=PC, corner_edge=pc_corner_edge, now=1)
        self.assertEqual(ways.ways(held, A)["methods"], ["edge"])

    def test_an_unknown_peer_raises(self):
        with self.assertRaises(KeyError):
            ways.edit(settings(), A, side="left", methods=[], parts=["middle"], corner="top_left",
                      kinds=PC, corner_edge=pc_corner_edge, now=1)

    def test_a_zone_the_user_kept_keeps_its_other_fields(self):
        held = settings(peer(A, "Ay", side="left"), zones=[{"peer": A, "kind": "edge", "off": True, "later": 1}])
        ways.edit(held, A, side="left", methods=["edge"], parts=["middle"], corner="top_left",
                  kinds=PC, corner_edge=pc_corner_edge, now=1)
        edge = next(zone for zone in held["zones"] if zone["kind"] == "edge")
        self.assertEqual((edge["peer"], edge["side"]), (A, "left"))
        self.assertNotIn("later", edge)


class Clashes(unittest.TestCase):
    def compile(self, held, layout=None, platform="pc"):
        if layout is None:
            layout = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                      A: {"x": 160, "y": 0, "w": 160, "h": 50},
                      B: {"x": 160, "y": 50, "w": 160, "h": 50}}
        result = ways.compile_layout(held, layout, platform=platform)
        self.assertTrue(result["accepted"])
        return result["settings"], result["tiles"]

    def test_two_machines_on_one_whole_edge_clash_and_the_sentence_names_both(self):
        held = settings(peer(A, "Ay", side="right"), peer(B, "Bee", side="right"),
                        zones=[{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge"}])
        compiled, _ = self.compile(held)
        self.assertIsNone(ways.clash(compiled, "this Mac"))
        self.assertEqual(crossing_thirds(compiled, A), ["start"])
        self.assertEqual(crossing_thirds(compiled, B), ["middle", "end"])

    def test_two_machines_on_different_thirds_of_one_edge_do_not_clash(self):
        held = settings(peer(A, "Ay", side="right"), peer(B, "Bee", side="right"), zones=[
            {"peer": A, "kind": "part", "parts": ["start"]}, {"peer": B, "kind": "part", "parts": ["middle", "end"]}])
        compiled, _ = self.compile(held)
        self.assertIsNone(ways.clash(compiled, "this PC"))
        self.assertEqual(crossing_thirds(compiled, A), ["start"])
        self.assertEqual(crossing_thirds(compiled, B), ["middle", "end"])

    def test_a_corner_inside_another_machines_edge_does_not_clash(self):
        held = settings(peer(A, "Ay", side="right"), peer(B, "Bee", side="top"), zones=[
            {"peer": A, "kind": "edge"}, {"peer": B, "kind": "corner", "corner": "top_right", "edge": "top"}])
        layout = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                  A: {"x": 160, "y": 0, "w": 160, "h": 100},
                  B: {"x": 160, "y": -100, "w": 100, "h": 100}}
        compiled, _ = self.compile(held, layout)
        self.assertIsNone(ways.clash(compiled, "this PC"))
        self.assertIn("corner", [zone["kind"] for zone in compiled["zones"] if zone["peer"] == B])

    def test_one_corner_for_two_machines_clashes(self):
        held = settings(peer(A, "Ay", side="right"), peer(B, "Bee", side="top"), zones=[
            {"peer": A, "kind": "corner", "corner": "top_right", "edge": "right"},
            {"peer": B, "kind": "corner", "corner": "top_right", "edge": "top"}])
        layout = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                  A: {"x": 160, "y": 0, "w": 100, "h": 100},
                  B: {"x": 160, "y": 0, "w": 100, "h": 100}}
        compiled, tiles = self.compile(held, layout)
        self.assertIsNone(ways.clash(compiled, "this PC"))
        self.assertEqual(tiles[A]["state"], "overlap")
        self.assertFalse(compiled["zones"])

    def test_one_notch_for_two_machines_clashes(self):
        held = settings(peer(A, "Ay", side="right"), peer(B, "Bee", side="top"), zones=[
            {"peer": A, "kind": "notch"}, {"peer": B, "kind": "notch"}])
        compiled, _ = self.compile(held, platform="mac")
        self.assertIsNone(ways.clash(compiled, "this Mac"))
        self.assertEqual([zone["peer"] for zone in compiled["zones"] if zone["kind"] == "notch"], [A])

    def test_two_of_one_machines_own_zones_over_one_stretch_clash(self):
        held = settings(peer(A, "Ay", side="right"), zones=[
            {"peer": A, "kind": "edge"}, {"peer": A, "kind": "part", "parts": ["middle"]}])
        compiled, _ = self.compile(held, {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                                          A: {"x": 160, "y": 0, "w": 160, "h": 100}})
        self.assertIsNone(ways.clash(compiled, "this Mac"))
        self.assertEqual(len([zone for zone in compiled["zones"] if zone["peer"] == A]), 1)

    def test_zones_that_are_off_never_clash(self):
        held = settings(peer(A, "Ay", side="right"), peer(B, "Bee", side="right"),
                        zones=[{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge", "off": True}])
        compiled, _ = self.compile(held)
        self.assertIsNone(ways.clash(compiled, "this PC"))
        self.assertFalse(any(zone["peer"] == B for zone in compiled["zones"]))


class EveryMachineHasAWayIn(unittest.TestCase):
    """A machine paired, or one whose side arrives, crosses by its whole edge unless that edge is
    another machine's already (the rig's beta.3 file of 01-10: the laptop's side set, no zone)."""

    def test_a_machine_with_no_zone_gets_its_whole_edge(self):
        held = settings(peer(A, "Ay", side="left"), peer(B, "Bee", side="right"), zones=[{"peer": A, "kind": "edge"}])
        self.assertTrue(ways.ensure_zones(held))
        self.assertTrue(any(zone["peer"] == B and zone["kind"] == "edge" and zone["side"] == "right"
                            for zone in held["zones"]))
        self.assertFalse(ways.ensure_zones(held))

    def test_one_whose_side_is_another_machines_edge_gets_it_off_so_the_settings_stay_readable(self):
        held = settings(peer(A, "Ay", side="left"), peer(B, "Bee", side="left"), zones=[{"peer": A, "kind": "edge"}])
        ways.ensure_zones(held)
        self.assertTrue(all("off" not in zone for zone in held["zones"]))
        self.assertEqual(ways.ways(held, B)["methods"], ["edge"])
        self.assertIsNone(ways.clash(held, "this PC"))

    def test_a_machine_with_its_zones_all_off_keeps_them_as_they_are(self):
        held = settings(peer(A, "Ay", side="left"), zones=[{"peer": A, "kind": "edge", "off": True}])
        self.assertTrue(ways.ensure_zones(held))
        self.assertEqual(held["zones"], [])
        self.assertNotIn("edge", ways.ways(held, A)["methods"])

    def test_a_phone_and_an_entry_with_no_id_get_none(self):
        held = settings(peer(PHONE, "Phone", port=0, host="", send=False), peer("", "Old", from_1_4=True))
        self.assertTrue(ways.ensure_zones(held))
        self.assertEqual(held["zones"], [])

    def test_an_arrangement_for_a_machine_with_no_zone_gives_it_its_edge(self):
        held = settings(peer(A, "Ay", side="left"), peer(B, "Bee"), zones=[{"peer": A, "kind": "edge"}])
        self.assertEqual(ways.arrangement(held, B, "left", 20, B, "this PC"), (True, []))
        self.assertTrue(any(zone["peer"] == B and zone["kind"] == "edge" for zone in held["zones"]))

    def test_an_arrangement_onto_another_machines_edge_gives_it_the_edge_off_and_says_so(self):
        held = settings(peer(A, "MacBook Pro", side="left"), peer(B, "Laptop"), zones=[{"peer": A, "kind": "edge"}])
        changed, notices = ways.arrangement(held, B, "right", 20, B, "this PC")
        self.assertTrue(changed)
        self.assertEqual(notices, [])
        self.assertEqual(held["peers"][1]["side"], "left")
        self.assertTrue(all("off" not in zone for zone in held["zones"]))


class Blocked(unittest.TestCase):
    def test_a_machine_whose_side_is_taken_and_has_no_way_in_is_told_which_machine_has_it(self):
        held = settings(peer(A, "MacBook Pro", side="left"), peer(B, "Laptop", side="left"),
                        zones=[{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge", "off": True}])
        self.assertEqual(ways.blocked_sentence(held, B, "this PC"),
                         "Laptop has no way in: MacBook Pro already leads from the left edge of this PC. "
                         "Give each its own thirds of that edge by moving the machines on the map, or move one to another side.")
        self.assertEqual(ways.blocked_sentence(held, A, "this PC"), "")

    def test_thirds_left_free_by_another_machine_are_no_block(self):
        held = settings(peer(A, "MacBook Pro", side="left"), peer(B, "Laptop", side="left"), zones=[
            {"peer": A, "kind": "part", "parts": ["start"]}, {"peer": B, "kind": "part", "parts": ["end"]}])
        self.assertEqual(ways.blocked_sentence(held, B, "this PC"), "")

    def test_a_machine_with_a_corner_in_use_is_not_blocked(self):
        held = settings(peer(A, "MacBook Pro", side="left"), peer(B, "Laptop", side="left"), zones=[
            {"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge", "off": True},
            {"peer": B, "kind": "corner", "corner": "top_left", "edge": "left"}])
        self.assertEqual(ways.blocked_sentence(held, B, "this PC"), "")

    def test_a_machine_with_no_side_is_not_blocked(self):
        held = settings(peer(A, "MacBook Pro", side="left"), peer(B, "Laptop"), zones=[{"peer": A, "kind": "edge"}])
        self.assertEqual(ways.blocked_sentence(held, B, "this PC"), "")


class WayBack(unittest.TestCase):
    """What a machine tells another with its `arrangement`: whether it has a zone in use leading there."""

    def test_a_zone_in_use_with_a_side_or_a_corner_is_a_way(self):
        held = settings(peer(A, "Ay", side="left"), peer(B, "Bee"), peer(C, "Sea", side="top"), zones=[
            {"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge"},
            {"peer": C, "kind": "edge", "off": True}, {"peer": C, "kind": "corner", "corner": "top_left", "edge": "top"}])
        self.assertEqual([ways.has_way(held, ident) for ident in (A, B, C)], [True, False, True])

    def test_an_arrangement_keeps_what_the_sender_said_whether_or_not_its_side_is_newer(self):
        held = settings(peer(A, "Ay", side="left", side_set_at=50, side_by=A))
        self.assertEqual(ways.arrangement(held, A, "right", 50, A, "this PC", way_back=False), (True, []))
        self.assertIs(held["peers"][0]["way_back"], False)
        self.assertEqual(ways.arrangement(held, A, "right", 50, A, "this PC", way_back=False), (False, []))
        self.assertEqual(ways.arrangement(held, A, "right", 50, A, "this PC"), (False, []))
        self.assertIs(held["peers"][0]["way_back"], False)
        self.assertEqual(ways.arrangement(held, A, "right", 50, A, "this PC", way_back=True), (True, []))
        self.assertIs(held["peers"][0]["way_back"], True)

    def test_the_sentence_names_the_machine_with_no_way_back(self):
        held = settings(peer(A, "Desk-PC", way_back=False), peer(B, "Bee", way_back=True), peer(C, "Sea"))
        self.assertEqual(ways.no_way_back_sentence(held, A),
                         "Desk-PC has no way back to this machine: none of its edges leads here. Its own "
                         "Crossing page says why.")
        self.assertEqual(ways.no_way_back_sentence(held, B), "")
        self.assertEqual(ways.no_way_back_sentence(held, C), "")


    def test_with_no_way_back_the_sender_names_the_machine_whose_zone_holds_that_side(self):
        # The laptop on 02-10-2026: the Mac and the rig both on its left, the rig's edge in use.
        held = settings(peer(A, "Mac", side="left"), peer(B, "Rig", side="left"),
                        zones=[{"peer": A, "kind": "edge", "off": True}, {"peer": B, "kind": "edge"}])
        self.assertEqual(ways.way_back_by(held, A), B)
        self.assertIsNone(ways.way_back_by(held, B))
        held["zones"][0].pop("off")
        held["zones"][1]["off"] = True
        self.assertIsNone(ways.way_back_by(held, A))
        # Off by choice, with nobody else on that side: no way back, and nobody in the way.
        held["zones"][0]["off"] = True
        self.assertIsNone(ways.way_back_by(held, A))

    def test_what_each_machine_is_told_of_its_way_back_so_a_change_reaches_every_machine_it_touches(self):
        # Moving one machine can open or close another's way back: Sol's review, 02-10-2026.
        held = settings(peer(A, "Mac", side="left"), peer(B, "Rig", side="left"),
                        zones=[{"peer": A, "kind": "edge", "off": True}, {"peer": B, "kind": "edge"}])
        before = ways.way_back_state(held)
        self.assertEqual(before, {A: (False, B), B: (True, None)})
        held["peers"][1]["side"] = "right"
        held["zones"][0].pop("off")
        after = ways.way_back_state(held)
        self.assertEqual(sorted(peer for peer in after if after[peer] != before.get(peer)), [A])

    def test_an_arrangement_keeps_who_is_in_the_way_only_beside_a_way_back_of_false(self):
        held = settings(peer(A, "Ay", side="left", side_set_at=50, side_by=A))
        self.assertEqual(ways.arrangement(held, A, "right", 50, A, "this PC", way_back=False, way_back_by=C), (True, []))
        self.assertEqual(held["peers"][0]["way_back_by"], C)
        ways.arrangement(held, A, "right", 50, A, "this PC", way_back=False)
        self.assertNotIn("way_back_by", held["peers"][0])
        ways.arrangement(held, A, "right", 50, A, "this PC", way_back=False, way_back_by=C)
        ways.arrangement(held, A, "right", 50, A, "this PC", way_back=True, way_back_by=C)
        self.assertNotIn("way_back_by", held["peers"][0])
        # This machine's own id, or one that is not an id, is ignored as if absent.
        for odd in (HERE, "x"):
            ways.arrangement(held, A, "right", 50, A, "this PC", way_back=False, way_back_by=odd)
            self.assertNotIn("way_back_by", held["peers"][0])

    def test_the_sentence_names_the_machine_in_the_way_and_where_to_change_it(self):
        held = settings(peer(A, "Laptop-PC", side="right", way_back=False, way_back_by=B), peer(B, "Desk-PC"))
        self.assertEqual(ways.no_way_back_sentence(held, A),
                         "Laptop-PC has Desk-PC on its left too, so none of its edges leads back here. On "
                         "Laptop-PC's Crossing page, share that side by thirds or move one of them.")
        held["peers"][0]["way_back_by"] = C
        self.assertEqual(ways.no_way_back_sentence(held, A),
                         "Laptop-PC has another machine on its left too, so none of its edges leads back here. On "
                         "Laptop-PC's Crossing page, share that side by thirds or move one of them.")


class Arrangement(unittest.TestCase):
    def test_a_newer_arrangement_sets_the_opposite_side_for_that_peer_only(self):
        held = settings(peer(A, "Ay", side="left", side_set_at=10, side_by=HERE), peer(B, "Bee", side="right"))
        changed, notices = ways.arrangement(held, A, "left", 20, A, "this PC")
        self.assertTrue(changed)
        self.assertEqual(notices, [])
        self.assertEqual((held["peers"][0]["side"], held["peers"][0]["side_set_at"], held["peers"][0]["side_by"]),
                         ("right", 20, A))
        self.assertEqual(held["peers"][1]["side"], "right")

    def test_an_older_one_or_a_tie_lost_on_the_id_changes_nothing(self):
        held = settings(peer(A, "Ay", side="left", side_set_at=10, side_by=C))
        self.assertEqual(ways.arrangement(held, A, "left", 9, A, "this PC"), (False, []))
        self.assertEqual(ways.arrangement(held, A, "left", 10, A, "this PC"), (False, []))
        self.assertEqual(held["peers"][0]["side"], "left")

    def test_a_tie_won_on_the_id_applies(self):
        held = settings(peer(A, "Ay", side="left", side_set_at=10, side_by=A))
        self.assertEqual(ways.arrangement(held, A, "left", 10, C, "this PC"), (True, []))

    def test_a_side_that_now_overlaps_turns_the_senders_zones_off_and_names_both(self):
        held = settings(peer(A, "Ay", side="left"), peer(B, "Bee", side="right"),
                        zones=[{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge"}])
        held["peers"][0]["caps"] = ["layout"]
        held["peers"][1]["caps"] = ["layout"]
        held["layout"] = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                           A: {"x": -160, "y": 0, "w": 160, "h": 100},
                           B: {"x": 160, "y": 0, "w": 160, "h": 100}}
        before = [dict(zone) for zone in held["zones"]]
        changed, notices = ways.arrangement(held, A, "left", 20, A, "this Mac")
        self.assertFalse(changed)
        self.assertEqual(notices, [])
        self.assertEqual(held["zones"], before)

    def test_an_unknown_peer_bad_edge_or_bad_by_changes_nothing(self):
        held = settings(peer(A, "Ay", side="left"))
        self.assertEqual(ways.arrangement(held, B, "left", 20, B, "this PC"), (False, []))
        self.assertEqual(ways.arrangement(held, A, "middle", 20, A, "this PC"), (False, []))
        self.assertEqual(ways.arrangement(held, A, "left", 20, "nope", "this PC"), (False, []))


class TheShortcutsMachine(unittest.TestCase):
    def test_the_machine_input_was_last_on_while_it_can_take_input(self):
        peers = [peer(A, "Ay"), peer(B, "Bee")]
        self.assertEqual(ways.shortcut_peer(peers, B, lambda ident: True), B)

    def test_else_the_first_that_can_take_input(self):
        peers = [peer(A, "Ay"), peer(B, "Bee"), peer(C, "Sea")]
        self.assertEqual(ways.shortcut_peer(peers, A, lambda ident: ident == C), C)
        self.assertEqual(ways.shortcut_peer(peers, None, lambda ident: ident != A), B)

    def test_else_the_first_this_machine_sends_to_so_the_refusal_or_the_wake_names_it(self):
        peers = [peer(A, "Ay", send=False), peer(B, "Bee"), peer(C, "Sea")]
        self.assertEqual(ways.shortcut_peer(peers, None, lambda ident: False), B)

    def test_a_machine_this_one_does_not_send_to_is_never_picked(self):
        peers = [peer(A, "Ay", send=False)]
        self.assertIsNone(ways.shortcut_peer(peers, A, lambda ident: True))

    def test_a_machine_not_in_use_is_never_picked(self):
        peers = [peer(A, "Ay", in_use=False), peer(B, "Bee")]
        self.assertEqual(ways.shortcut_peer(peers, A, lambda ident: True), B)

    def test_a_phone_or_an_entry_without_an_id_is_never_picked(self):
        peers = [peer(PHONE, "Phone", port=0, host="", send=False), peer("", "Old", from_1_4=True)]
        self.assertIsNone(ways.shortcut_peer(peers, None, lambda ident: True))

    def test_nothing_paired_picks_nothing(self):
        self.assertIsNone(ways.shortcut_peer([], None, lambda ident: True))


class MissingPairings(unittest.TestCase):
    def test_a_machine_that_may_drive_this_one_and_is_not_paired_with_the_zones_peer_is_named(self):
        held = settings(peer(A, "Ay", paired_with=[B]), peer(B, "Bee", paired_with=[A]), peer(C, "Sea", paired_with=[A]))
        self.assertEqual([entry["id"] for entry in ways.missing_pairings(held, B)], [C])
        self.assertEqual([entry["id"] for entry in ways.missing_pairings(held, A)], [])

    def test_a_machine_not_allowed_to_drive_this_one_needs_no_pairing(self):
        held = settings(peer(A, "Ay"), peer(B, "Bee", allow_drive=False))
        self.assertEqual(ways.missing_pairings(held, A), [])

    def test_a_machine_not_in_use_needs_no_pairing(self):
        held = settings(peer(A, "Ay"), peer(B, "Bee", in_use=False))
        self.assertEqual(ways.missing_pairings(held, A), [])

    def test_a_machine_that_has_never_linked_is_not_judged(self):
        held = settings(peer(A, "Ay"), peer(B, "Bee", linked=False))
        self.assertEqual(ways.missing_pairings(held, A), [])

    def test_a_phone_that_may_drive_this_one_counts(self):
        held = settings(peer(A, "Ay"), peer(PHONE, "Phone", port=0, host="", send=False))
        self.assertEqual([entry["id"] for entry in ways.missing_pairings(held, A)], [PHONE])

    def test_the_sentence_names_each_machine_and_the_one_it_needs(self):
        held = settings(peer(A, "Ay"), peer(B, "Bee", paired_with=[]))
        self.assertEqual(ways.missing_sentence(held, A, "this Mac"),
                         "While Bee drives this Mac, this way to Ay does nothing: Bee is not paired with Ay. "
                         "Pair them to use it from there too.")
        held["peers"].append(peer(C, "Sea"))
        self.assertEqual(ways.missing_sentence(held, A, "this Mac"),
                         "While Bee or Sea drives this Mac, this way to Ay does nothing: neither is paired "
                         "with Ay. Pair them to use it from there too.")
        self.assertEqual(ways.missing_sentence(settings(peer(A, "Ay")), A, "this Mac"), "")



class TheShownDefault(unittest.TestCase):
    """The Mac's page shows Right for its one machine before a side is set, so the settings hold it
    from the moment that machine is paired, not from the first save (beta.4: Edge did nothing until
    a save of another way wrote it)."""

    def test_the_one_machine_not_placed_holds_right_unstamped_so_its_edge_fires(self):
        held = settings(peer(A, "Ay"), zones=[{"peer": A, "kind": "edge"}])
        self.assertFalse(ways.has_way(held, A))
        self.assertTrue(ways.default_side(held))
        entry = held["peers"][0]
        self.assertEqual((entry["side"], entry["side_set_at"], entry["side_by"]), ("right", 0, ""))
        self.assertEqual(ways.ways(held, A)["methods"], ["edge"])
        self.assertFalse(ways.default_side(held))

    def test_the_held_default_loses_to_a_side_the_peer_chooses(self):
        held = settings(peer(A, "Ay"), zones=[{"peer": A, "kind": "edge"}])
        ways.default_side(held)
        self.assertEqual(ways.arrangement(held, A, "bottom", int(time.time()) + 1, A, "this Mac"), (True, []))
        self.assertEqual(held["peers"][0]["side"], "top")

    def test_a_default_never_moves_a_side_already_set(self):
        held = settings(peer(A, "Ay", side="left", side_set_at=5, side_by=A), zones=[{"peer": A, "kind": "edge"}])
        self.assertFalse(ways.default_side(held))
        self.assertEqual(held["peers"][0]["side"], "left")

    def test_with_several_machines_none_is_given_a_side_nobody_chose(self):
        held = settings(peer(A, "Ay"), peer(B, "Bee"),
                        zones=[{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge", "off": True}])
        self.assertFalse(ways.default_side(held))
        self.assertEqual([entry["side"] for entry in held["peers"]], ["", ""])

    def test_a_machine_paired_beside_the_one_migrated_from_1_4_is_given_no_side(self):
        # The migrated entry has no id and holds the 1.4 side: Right as well would clash with it.
        held = settings(peer("", "Studio", side="right", from_1_4=True), peer(A, "Ay"),
                        zones=[{"peer": "", "kind": "edge"}, {"peer": A, "kind": "edge"}])
        self.assertFalse(ways.default_side(held))
        self.assertEqual(held["peers"][1]["side"], "")

    def test_a_phone_alone_is_given_no_side(self):
        held = settings(peer(PHONE, "Phone", port=0, host="", send=False))
        self.assertFalse(ways.default_side(held))


class TheCornerFollowsAnArrivingSide(unittest.TestCase):
    def test_a_corner_that_crosses_the_side_is_rewritten_for_the_side_that_arrives(self):
        held = settings(peer(A, "Ay", side="top", side_set_at=5, side_by=A),
                        zones=[{"peer": A, "kind": "corner", "corner": "top_right", "edge": "top"}])
        self.assertEqual(ways.arrangement(held, A, "left", 10, A, "this PC", corner_edge=pc_corner_edge), (True, []))
        self.assertEqual((held["peers"][0]["side"], held["zones"][0]["edge"]), ("right", "right"))



class SharingASide(unittest.TestCase):
    """Shared stretches come from geometry; the old offer/share entry points do not mutate intent."""

    def two_on_the_right(self, rig_zone=None):
        return settings(peer(A, "Desk-PC", side="right"), peer(B, "Laptop-PC", side="right"),
                        zones=[rig_zone or {"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge", "off": True}])

    def test_a_machine_whose_side_another_holds_whole_is_offered_the_end_third(self):
        held = self.two_on_the_right()
        self.assertIsNone(ways.share_offer(held, B))
        self.assertEqual(ways.ways(held, A)["parts"], list(ways.PARTS))
        self.assertEqual(ways.ways(held, B)["methods"], [])

    def test_free_thirds_go_to_the_machine_asking(self):
        held = self.two_on_the_right({"peer": A, "kind": "part", "parts": ["end"]})
        self.assertIsNone(ways.share_offer(held, B))
        self.assertEqual(ways.ways(held, A)["parts"], ["end"])

    def test_nothing_is_offered_where_nothing_is_in_the_way(self):
        held = settings(peer(A, "Ay", side="left"), peer(B, "Bee", side="right"),
                        zones=[{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge", "off": True}])
        self.assertIsNone(ways.share_offer(held, B))
        self.assertIsNone(ways.share_offer(self.two_on_the_right(), A))
        self.assertEqual(ways.share(self.two_on_the_right(), "right",
                                    {"start": A, "middle": A, "end": A}, kinds=MAC), [])

    def test_sharing_writes_each_machines_thirds_and_turns_their_whole_edges_off(self):
        held = self.two_on_the_right()
        before = [dict(zone) for zone in held["zones"]]
        self.assertEqual(ways.share(held, "right", {"start": A, "middle": A, "end": B}, kinds=MAC), [])
        self.assertEqual(held["zones"], before)
        held["layout"] = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                           A: {"x": 160, "y": 0, "w": 160, "h": 33},
                           B: {"x": 160, "y": 67, "w": 160, "h": 33}}
        held["zones"] = [{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge"}]
        compiled = ways.compile_layout(held, held["layout"])["settings"]
        self.assertEqual(crossing_thirds(compiled, A), ["start"])
        self.assertEqual(crossing_thirds(compiled, B), ["end"])

    def test_sharing_replaces_an_older_page_pick(self):
        held = self.two_on_the_right({"peer": A, "kind": "part", "parts": ["start"], "page_parts": ["start"]})
        ways.share(held, "right", {"start": B, "middle": A, "end": B}, kinds=MAC)
        self.assertEqual(ways.ways(held, A)["parts"], ["start"])
        held["peers"][1]["ways"] = {"edge": True, "thirds": ["end"], "corner": False,
                                     "corner_at": "top_left", "notch": False}
        compiled = ways.compile_layout(held, beside(own_displays=True), moved=A)["settings"]
        self.assertEqual(ways.ways(compiled, B)["parts"], ["end"])
        self.assertTrue(all("page_parts" not in zone and "page_off" not in zone for zone in compiled["zones"]))

    def compiled(self):
        held = settings(peer(A, "Desk-PC", side="right"), peer(B, "Laptop-PC", side="right"),
                        zones=[{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge", "off": True}])
        return ways.compile_layout(held, beside(own_displays=True), moved=A)["settings"]

    def test_on_a_compiled_map_a_machine_left_with_no_third_stays_off_through_the_next_compile(self):
        held = self.compiled()
        intent = dict(held["peers"][0]["ways"])
        self.assertEqual(ways.share(held, "right", {"start": B, "middle": B, "end": B}, kinds=MAC), [])
        self.assertEqual(held["peers"][0]["ways"], intent)
        held = ways.compile_layout(held, beside(own_displays=True), moved=A)["settings"]
        self.assertEqual(ways.ways(held, A)["parts"], list(ways.PARTS))
        self.assertTrue(ways.crosses_on_map(held, A))

    def test_on_a_compiled_map_a_machine_given_a_third_keeps_it_through_the_next_compile(self):
        held = self.compiled()
        ways.share(held, "right", {"start": A, "middle": B, "end": B}, kinds=MAC)
        self.assertEqual(ways.ways(held, A)["parts"], list(ways.PARTS))
        held = ways.compile_layout(held, beside(own_displays=True), moved=A)["settings"]
        self.assertEqual(ways.ways(held, A)["parts"], list(ways.PARTS))

    def test_a_share_that_gives_a_third_back_relights_an_edge_it_turned_off(self):
        held = self.compiled()
        ways.share(held, "right", {"start": B, "middle": B, "end": B}, kinds=MAC)
        ways.share(held, "right", {"start": B, "middle": A, "end": B}, kinds=MAC)
        held = ways.compile_layout(held, beside(own_displays=True), moved=A)["settings"]
        self.assertEqual(ways.ways(held, A)["parts"], list(ways.PARTS))

    def test_a_machine_on_that_side_left_with_no_third_crosses_there_no_more(self):
        held = self.two_on_the_right()
        ways.share(held, "right", {"start": B, "middle": B, "end": B}, kinds=MAC)
        self.assertTrue(ways.has_way(held, A))
        self.assertEqual(ways.ways(held, A)["parts"], list(ways.PARTS))

    def test_a_machine_left_with_no_third_keeps_a_part_zone_the_settings_accept(self):
        # Sol's review: an empty `parts` made the Mac's store refuse the whole share.
        held = settings(peer(A, "Ay", side="right"), peer(B, "Bee", side="right"), peer(C, "Sea", side="right"),
                        zones=[{"peer": A, "kind": "part", "parts": ["start", "middle"]},
                               {"peer": C, "kind": "part", "parts": ["end"]},
                               {"peer": B, "kind": "edge", "off": True}])
        ways.share(held, "right", {"start": A, "middle": A, "end": B}, kinds=MAC)
        sea = next(zone for zone in held["zones"] if zone["peer"] == C and zone["kind"] == "part")
        self.assertEqual(sea["parts"], ["end"])
        self.assertNotIn("off", sea)

    def test_sharing_reports_every_machine_whose_way_back_it_changed_not_only_whose_zones(self):
        # Sol's review: Sea's blocker moved from Ay to Bee, and only Ay and Bee were told.
        held = settings(peer(A, "Ay", side="right"), peer(B, "Bee", side="right"), peer(C, "Sea", side="right"),
                        zones=[{"peer": B, "kind": "part", "parts": ["middle"], "off": True},
                               {"peer": A, "kind": "edge"},
                               {"peer": B, "kind": "edge", "off": True},
                               {"peer": C, "kind": "edge", "off": True}])
        before = ways.way_back_state(held)
        changed = ways.share(held, "right", {"start": A, "middle": A, "end": B}, kinds=MAC)
        self.assertEqual(changed, [])
        self.assertEqual(ways.way_back_state(held), before)

    def test_a_split_naming_a_machine_not_on_that_side_or_leaving_a_third_out_is_refused(self):
        held = self.two_on_the_right()
        held["peers"].append(peer(C, "Sea", side="left"))
        before = [dict(zone) for zone in held["zones"]]
        for thirds in ({"start": A, "middle": A, "end": C}, {"start": A, "middle": B}):
            with self.subTest(thirds=thirds):
                with self.assertRaises(ValueError):
                    ways.share(held, "right", thirds, kinds=MAC)
                self.assertEqual(held["zones"], before)


class AMachineThatIsGone(unittest.TestCase):
    """Unavailable peers are omitted from compiled zones without changing their saved intent."""

    def pc_with_a_gone_machine_on_the_right(self):
        return settings(peer(A, "MacBook Pro", side="bottom"), peer(B, "live-usb", side="right"),
                        zones=[{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge"}])

    @staticmethod
    def only(*idents):
        return lambda ident: ident in idents

    def test_an_arrangement_from_a_machine_that_is_here_takes_the_side_from_one_that_is_not(self):
        held = self.pc_with_a_gone_machine_on_the_right()
        held["peers"][0]["caps"] = ["layout"]
        before = dict(held.get("layout", {}))
        changed, notices = ways.arrangement(held, A, "left", 20, A, "this PC", live=self.only(A))
        self.assertFalse(changed)
        self.assertEqual(notices, [])
        self.assertEqual(held["peers"][0]["side"], "bottom")
        self.assertEqual(held.get("layout", {}), before)
        self.assertEqual(ways.give_way(held, B, "this PC", self.only(A)), [])
        self.assertTrue(ways.has_way(held, B))

    def test_without_liveness_the_machine_arriving_still_gives_way_as_before(self):
        held = self.pc_with_a_gone_machine_on_the_right()
        before = [dict(zone) for zone in held["zones"]]
        self.assertEqual(ways.give_way(held, A, "this PC"), [])
        self.assertEqual(held["zones"], before)
        self.assertTrue(ways.ways(held, A)["methods"] == ["edge"])

    def test_two_machines_that_are_both_here_keep_the_rule_and_the_share_offer(self):
        held = self.pc_with_a_gone_machine_on_the_right()
        held["peers"][0]["caps"] = ["layout"]
        self.assertEqual(ways.arrangement(held, A, "left", 20, A, "this PC", live=self.only(A, B)), (False, []))
        self.assertIsNone(ways.share_offer(held, A, live=self.only(A, B)))
        self.assertTrue(ways.has_way(held, B))

    def test_a_machine_that_is_not_here_is_offered_no_third_and_blocks_nothing(self):
        held = settings(peer(A, "MacBook Pro", side="right"), peer(B, "live-usb", side="right"),
                        zones=[{"peer": A, "kind": "edge", "off": True}, {"peer": B, "kind": "edge"}])
        layout = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                  A: {"x": 160, "y": 0, "w": 160, "h": 100},
                  B: {"x": 160, "y": 0, "w": 160, "h": 100}}
        result = ways.compile_layout(held, layout, gone=self.only(B))
        self.assertNotIn(B, [zone["peer"] for zone in result["settings"]["zones"]])
        self.assertTrue(result["settings"]["peers"][1]["ways"]["edge"])
        self.assertIsNone(ways.share_offer(held, A, live=self.only(A)))

    def test_choosing_the_edge_on_the_crossing_page_takes_it_from_a_machine_that_is_not_here(self):
        # "I tried to set up edge but it wasn't clicking": the edit, then the clash check the stores make.
        held = settings(peer(A, "MacBook Pro", side="right"), peer(B, "live-usb", side="right"),
                        zones=[{"peer": A, "kind": "edge", "off": True}, {"peer": B, "kind": "edge"}])
        held["layout"] = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                           A: {"x": 160, "y": 0, "w": 160, "h": 100},
                           B: {"x": 160, "y": 0, "w": 160, "h": 100}}
        ways.edit(held, A, side="right", methods=["edge"], parts=[], corner="top_left", kinds=PC,
                  corner_edge=pc_corner_edge, now=30)
        self.assertIsNone(ways.clash(held, "this PC"))
        self.assertEqual(ways.give_way(held, A, "this PC", self.only(A)), [])
        self.assertEqual(ways.ways(held, A)["methods"], ["edge"])

    def test_giving_way_leaves_zones_elsewhere_and_machines_that_are_here_alone(self):
        held = settings(peer(A, "Mac", side="right"), peer(B, "Gone", side="left"), peer(C, "Here", side="top"),
                        zones=[{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge"}, {"peer": C, "kind": "edge"}])
        before = [dict(zone) for zone in held["zones"]]
        self.assertEqual(ways.give_way(held, A, "this PC", self.only(A, C)), [])
        self.assertEqual(ways.give_way(held, A, "this PC", None), [])
        self.assertEqual(held["zones"], before)


class AMachineThatIsAsleep(unittest.TestCase):
    """Sleep and reconnect events preserve intent; zones are regenerated from current contacts."""

    def asleep(self):
        held = settings(peer(A, "MacBook Pro", side="right"), peer(B, "Laptop", side="right"),
                        zones=[{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge", "off": True}])
        ways.edit(held, B, side="right", methods=["edge"], parts=[], corner="top_left", kinds=PC,
                  corner_edge=pc_corner_edge, now=30)
        return held

    @staticmethod
    def zone(held, peer_id):
        return next(zone for zone in held["zones"] if zone["peer"] == peer_id and zone["kind"] == "edge")

    def test_a_machine_with_no_link_has_its_zone_set_aside_not_lost(self):
        held = self.asleep()
        notices = ways.give_way(held, B, "this PC", lambda ident: ident == B)
        self.assertEqual(notices, [])
        self.assertTrue(ways.has_way(held, A))
        self.assertEqual(held["peers"][0]["ways"]["edge"], True)
        self.assertFalse(any("aside" in zone for zone in held["zones"]))

    def test_the_zone_comes_back_when_the_machine_connects_and_the_stretch_is_free(self):
        held = self.asleep()
        ways.give_way(held, B, "this PC", lambda ident: ident == B)
        live, gone = (lambda ident: True), (lambda ident: False)
        self.assertFalse(ways.presence_due(held, live, gone))
        self.assertEqual(ways.settle_presence(held, "this PC", live, gone), [])
        self.assertTrue(ways.has_way(held, A))
        self.assertFalse(ways.presence_due(held, live, gone))

    def test_a_left_out_zone_stays_off_when_its_machine_connects(self):
        held = self.asleep()
        ways.give_way(held, B, "this PC", lambda ident: ident == B)
        ways.leave_out(held, A)
        self.assertEqual(ways.settle_presence(held, "this PC", lambda _ident: True, lambda _ident: False), [])
        self.assertNotIn(A, [zone["peer"] for zone in held["zones"]])
        self.assertTrue(held["peers"][0]["ways"]["edge"])
        self.assertFalse(ways.has_way(held, A))

    def test_it_waits_while_the_machine_is_still_asleep(self):
        held = self.asleep()
        ways.give_way(held, B, "this PC", lambda ident: ident == B)
        before = [dict(zone) for zone in held["zones"]]
        self.assertFalse(ways.presence_due(held, lambda ident: ident == B, lambda ident: False))
        self.assertEqual(ways.settle_presence(held, "this PC", lambda ident: ident == B, lambda ident: False), [])
        self.assertEqual(held["zones"], before)

    def test_a_machine_back_over_a_stretch_another_holds_stays_off_and_says_so(self):
        held = self.asleep()
        ways.give_way(held, B, "this PC", lambda ident: ident == B)
        notices = ways.settle_presence(held, "this PC", lambda ident: True, lambda ident: False)
        self.assertEqual(notices, [])
        self.assertTrue(ways.has_way(held, A))
        self.assertIsNone(ways.share_offer(held, A, live=lambda ident: True))

    def test_a_machine_that_removed_this_pairing_stays_off_for_good_and_the_page_says_so(self):
        held = self.asleep()
        ways.give_way(held, B, "this PC", lambda ident: ident == B)
        gone = lambda ident: ident == A
        notices = ways.settle_presence(held, "this PC", lambda ident: ident == B, gone)
        self.assertEqual(notices, [])
        self.assertEqual(held["peers"][0]["ways"]["edge"], True)
        self.assertFalse(ways.presence_due(held, lambda ident: True, lambda ident: False))
        self.assertIn("no longer has this pairing", ways.gone_sentence(held, A, "this PC", gone))
        self.assertEqual(ways.gone_sentence(held, B, "this PC", gone), "")

    def test_a_zone_the_user_changes_while_it_is_set_aside_is_no_longer_set_aside(self):
        for methods in (["edge"], []):
            with self.subTest(methods=methods):
                held = self.asleep()
                ways.give_way(held, B, "this PC", lambda ident: ident == B)
                before = dict(held["peers"][0]["ways"])
                ways.edit(held, A, side="right", methods=methods, parts=[], corner="top_left", kinds=PC,
                          corner_edge=pc_corner_edge, now=40)
                self.assertNotIn("aside", str(held["zones"]))
                expected_edge = bool(methods)
                self.assertEqual(held["peers"][0]["ways"]["edge"], expected_edge)
                self.assertEqual(ways.settle_presence(held, "this PC", lambda ident: True, lambda ident: False), [])
                if methods:
                    self.assertEqual(held["peers"][0]["ways"]["edge"], before["edge"])


class TheShortcutIsNotTakenByAStaleEntry(unittest.TestCase):
    def test_with_nothing_ready_the_machine_last_used_is_named_before_an_older_entry(self):
        # The lent laptop listed the PC's old pairing first; the shortcut named it rather than the Mac.
        peers = [peer(A, "Old PC"), peer(B, "MacBook Pro")]
        self.assertEqual(ways.shortcut_peer(peers, B, lambda ident: False), B)
        self.assertEqual(ways.shortcut_peer(peers, None, lambda ident: False), A)


THIRDS_COMBINATIONS = [list(combo) for size in (1, 2, 3)
                       for combo in itertools.combinations(ways.PARTS, size)]


def beside(own_displays=False, peer_displays=False, peer_y=0, peer_h=100):
    layout = {HERE: {"x": 0, "y": 0, "w": 150, "h": 99},
              A: {"x": 150, "y": peer_y, "w": 150, "h": peer_h}}
    for ident, displays in ((HERE, own_displays), (A, peer_displays)):
        if displays:
            layout[ident]["displays"] = {"main": {"x": 0, "y": 0, "w": layout[ident]["w"], "h": layout[ident]["h"]}}
    return layout


def crossing_thirds(held, peer_id=A):
    """The thirds of this machine's side a push actually crosses by, from the zones in use."""
    found = set()
    for zone in held["zones"]:
        if zone.get("peer") != peer_id or zone.get("off") is True:
            continue
        if zone["kind"] == "edge":
            found.update(ways.PARTS)
        elif zone["kind"] == "part":
            found.update(zone["parts"])
    return [part for part in ways.PARTS if part in found]


class PartOfTheEdgeIsOneChoiceWithTheEdge(unittest.TestCase):
    """Edge and Part of the edge are one choice in two forms: every third, or the thirds picked."""

    def page(self, held, thirds, extra=()):
        methods, parts = ways.pick_thirds(list(extra), thirds)
        result = crossing_state.edit_ways(held, A, platform="mac", edge=bool(set(methods) & {"edge", "part"}),
                    thirds=parts, corner="corner" in methods, corner_at="top_left",
                    notch="notch" in methods, live=None, gone=None, now=30)
        held.clear()
        held.update(result["settings"])

    def test_picking_thirds_names_the_form(self):
        for thirds in THIRDS_COMBINATIONS:
            with self.subTest(thirds=thirds):
                methods, parts = ways.pick_thirds(["shortcut", "corner"], thirds)
                self.assertEqual(parts, thirds)
                self.assertEqual(methods, ["shortcut", "edge" if len(thirds) == 3 else "part", "corner"])
                self.assertEqual(ways.picked_thirds(methods, parts), thirds)
        self.assertEqual(ways.pick_thirds(["edge", "notch"], []), (["notch"], ["middle"]))

    def test_a_click_on_a_third_toggles_it_the_last_turns_edge_off_and_one_while_off_lights_it_alone(self):
        # The Ledger spec (docs/spec-crossing-page-a-ledger-10-10-2026.md section 3.3), map and ledger alike.
        self.assertEqual(ways.toggle_third(["edge"], [], "middle"), (["part"], ["start", "end"]))
        self.assertEqual(ways.toggle_third(["part"], ["start", "end"], "middle"), (["edge"], list(ways.PARTS)))
        self.assertEqual(ways.toggle_third(["part"], ["middle"], "middle"), ([], ["middle"]))
        self.assertEqual(ways.toggle_third(["part", "corner"], ["end"], "end"), (["corner"], ["end"]))
        self.assertEqual(ways.toggle_third(["corner"], ["start", "end"], "middle"), (["part", "corner"], ["middle"]))
        for thirds in THIRDS_COMBINATIONS:
            for part in ways.PARTS:
                with self.subTest(thirds=thirds, part=part):
                    methods, parts = ways.pick_thirds([], thirds)
                    after = ways.picked_thirds(*ways.toggle_third(methods, parts, part))
                    self.assertEqual(after, sorted(set(thirds) ^ {part}, key=ways.PARTS.index))

    def test_the_edge_text_turns_off_keeping_the_thirds_and_on_as_the_whole_edge(self):
        self.assertEqual(ways.toggle_edge(["part", "corner"], ["start", "end"]), (["corner"], ["start", "end"]))
        self.assertEqual(ways.toggle_edge(["corner"], ["start", "end"]), (["edge", "corner"], list(ways.PARTS)))
        self.assertEqual(ways.toggle_edge(["edge"], list(ways.PARTS)), ([], list(ways.PARTS)))

    def test_a_corner_is_held_by_one_machine_and_refused_to_the_others(self):
        held = settings(peer(A, "Ay", side="right"), peer(B, "Bee", side="left"), zones=[])
        held["peers"][0]["ways"] = {"edge": True, "thirds": list(ways.PARTS), "corner": True, "corner_at": "top_left"}
        self.assertEqual(ways.corner_holder(held, "top_left", exclude=B), A)
        self.assertIsNone(ways.corner_holder(held, "top_left", exclude=A))
        self.assertIsNone(ways.corner_holder(held, "top_right", exclude=B))
        held["peers"][0]["left_out"] = True
        self.assertIsNone(ways.corner_holder(held, "top_left", exclude=B))

    def test_a_diagonal_corner_contact_holds_the_corner_too(self):
        held = settings(peer(A, "Ay"), peer(B, "Bee"))
        held["layout"] = {HERE: {"x": 0, "y": 0, "w": 160, "h": 100},
                          A: {"x": -160, "y": -100, "w": 160, "h": 100},
                          B: {"x": 160, "y": 100, "w": 160, "h": 100}}
        self.assertEqual(ways.corner_holder(held, "top_left", exclude=B), A)

    def test_every_combination_round_trips_through_the_settings(self):
        for thirds in THIRDS_COMBINATIONS:
            with self.subTest(thirds=thirds):
                held = settings(peer(A, "Ay", side="right"), zones=[{"peer": A, "kind": "edge"}])
                self.page(held, thirds)
                read = ways.ways(held, A)
                self.assertEqual(ways.picked_thirds(read["methods"], read["parts"]), thirds)
                self.assertEqual(crossing_thirds(held), thirds)

    def test_every_combination_survives_compiling_the_map_with_or_without_display_metadata(self):
        for thirds in THIRDS_COMBINATIONS:
            for own_displays in (False, True):
                for peer_displays in (False, True):
                    with self.subTest(thirds=thirds, own_displays=own_displays, peer_displays=peer_displays):
                        held = settings(peer(A, "Ay", side="right"), zones=[{"peer": A, "kind": "edge"}])
                        self.page(held, thirds)
                        layout = beside(own_displays, peer_displays)
                        for _ in range(2):
                            result = ways.compile_layout(held, layout, moved=A)
                            held = result["settings"]
                            read = ways.ways(held, A)
                            self.assertEqual(ways.picked_thirds(read["methods"], read["parts"]), thirds)
                            self.assertTrue(ways.crosses_on_map(held, A))
                            self.assertEqual(result["tiles"][A]["state"], "edge" if len(thirds) == 3 else "part")
                        scoped = [zone for zone in held["zones"]
                                  if zone.get("display") is not None and zone.get("off") is not True]
                        if own_displays:
                            # The display's own contact crosses only by the picked thirds.
                            self.assertEqual(crossing_thirds({"zones": scoped}), thirds)

    def test_a_pick_made_after_the_map_compiled_narrows_every_contact_and_widens_back(self):
        held = settings(peer(A, "Ay", side="right"), zones=[{"peer": A, "kind": "edge"}])
        held = ways.compile_layout(held, beside(own_displays=True), moved=A)["settings"]
        self.page(held, ["start", "end"])
        scoped = [zone for zone in held["zones"] if zone.get("display") == "main"]
        self.assertEqual(crossing_thirds({"zones": scoped}), ["start", "end"])
        self.page(held, list(ways.PARTS))
        scoped = [zone for zone in held["zones"] if zone.get("display") == "main"]
        self.assertEqual(crossing_thirds({"zones": scoped}), list(ways.PARTS))
        self.assertEqual(ways.ways(held, A)["methods"], ["edge"])
        self.assertFalse(any("page_parts" in zone for zone in held["zones"]))

    def test_a_tile_touching_only_some_thirds_crosses_by_the_picked_ones_it_touches(self):
        held = settings(peer(A, "Ay", side="right"), zones=[{"peer": A, "kind": "edge"}])
        self.page(held, ["start", "end"])
        # Against the top two thirds only: the bottom third is picked but not touched.
        layout = beside(own_displays=True, peer_y=-34, peer_h=100)
        result = ways.compile_layout(held, layout, moved=A)
        held = result["settings"]
        scoped = [zone for zone in held["zones"] if zone.get("display") == "main"]
        self.assertEqual(crossing_thirds({"zones": scoped}), ["start"])
        self.assertEqual(ways.ways(held, A)["parts"], ["start", "end"])
        # Against the middle only: nothing picked is touched, and the map says so.
        result = ways.compile_layout(held, beside(own_displays=True, peer_y=33, peer_h=33), moved=A)
        self.assertEqual(result["tiles"][A]["state"], "gap")
        self.assertEqual(result["tiles"][A]["sentence_keys"], ["unpicked"])
        self.assertTrue(result["tiles"][A]["unpicked"])
        self.assertIn("{name}", ways.ATLAS_SENTENCES["unpicked"])

    def test_a_pick_a_clash_turned_off_reads_as_off_and_an_untouched_pick_as_on(self):
        held = settings(peer(A, "Ay", side="right"), zones=[{"peer": A, "kind": "edge"}])
        self.page(held, ["start"])
        held["zones"][0]["off"] = True
        self.assertIn("part", ways.ways(held, A)["methods"])
        self.assertEqual(ways.ways(held, A)["parts"], ["start"])
        self.page(held, ["start"])
        held = ways.compile_layout(held, beside(own_displays=True, peer_y=33, peer_h=33), moved=A)["settings"]
        self.assertFalse(ways.crosses_on_map(held, A))
        self.assertIn("part", ways.ways(held, A)["methods"])
        self.assertTrue(all("off" not in zone for zone in held["zones"]))
        self.page(held, ["start", "middle"])
        self.assertTrue(ways.crosses_on_map(held, A))
        self.assertFalse(any(zone.get("unpicked") for zone in held["zones"]))

    def test_leave_out_and_put_back_keep_the_pick_whether_or_not_it_touches(self):
        for peer_y, peer_h, crossing in ((0, 100, ["start"]), (33, 33, [])):
            with self.subTest(peer_y=peer_y):
                held = settings(peer(A, "Ay", side="right"), zones=[{"peer": A, "kind": "edge"}])
                self.page(held, ["start"])
                held = ways.compile_layout(held, beside(own_displays=True, peer_y=peer_y, peer_h=peer_h),
                                           moved=A)["settings"]
                ways.leave_out(held, A)
                self.assertFalse(ways.crosses_on_map(held, A))
                ways.put_back(held, A)
                self.assertIn("part", ways.ways(held, A)["methods"])
                self.assertEqual(ways.ways(held, A)["parts"], ["start"])
                scoped = [zone for zone in held["zones"] if zone.get("display") == "main"]
                self.assertEqual(crossing_thirds({"zones": scoped}), crossing)

    def test_put_back_leaves_off_a_third_another_machine_took_meanwhile(self):
        held = settings(peer(A, "Ay", side="right"), peer(B, "Bee", side="right"),
                        zones=[{"peer": A, "kind": "edge"}])
        self.page(held, ["start"])
        layout = beside(own_displays=True)
        held = ways.compile_layout(held, layout, moved=A)["settings"]
        ways.leave_out(held, A)
        ways.put_back(held, A)
        scoped = [zone for zone in held["zones"] if zone.get("peer") == A and zone.get("display") == "main"]
        self.assertEqual(crossing_thirds({"zones": scoped}), ["start"])
        self.assertIsNone(ways.clash(held, "here"))

    def test_a_contact_the_tile_has_left_is_not_revived_by_a_later_pick(self):
        held = settings(peer(A, "Ay", side="right"), zones=[{"peer": A, "kind": "edge"}])
        self.page(held, ["start"])
        held = ways.compile_layout(held, beside(own_displays=True), moved=A)["settings"]
        away = beside(own_displays=True)
        away[A]["x"] = 400
        held = ways.compile_layout(held, away, moved=A)["settings"]
        self.page(held, ["start", "middle"])
        scoped = [zone for zone in held["zones"] if zone.get("display") == "main"]
        self.assertEqual(crossing_thirds({"zones": scoped}), [])

    def test_edge_off_still_holds_with_thirds_picked_and_comes_back_whole(self):
        for own_displays in (False, True):
            with self.subTest(own_displays=own_displays):
                held = settings(peer(A, "Ay", side="right"), zones=[{"peer": A, "kind": "edge"}])
                self.page(held, ["start", "end"])
                held = ways.compile_layout(held, beside(own_displays), moved=A)["settings"]
                self.page(held, [], extra=["shortcut"])
                for _ in range(2):
                    held = ways.compile_layout(held, beside(own_displays), moved=A)["settings"]
                    self.assertFalse(ways.crosses_on_map(held, A))
                    self.assertEqual(ways.picked_thirds(**{k: ways.ways(held, A)[k] for k in ("methods", "parts")}), [])
                self.page(held, list(ways.PARTS))
                self.assertTrue(ways.crosses_on_map(held, A))
                self.assertEqual(ways.ways(held, A)["methods"], ["edge"])

    def test_the_edge_tile_names_its_form(self):
        cases = {("start", "end"): "Top and bottom thirds", ("middle",): "Middle third",
                 ("start", "middle", "end"): "Whole edge", (): "Whole edge or the thirds you pick"}
        for thirds, words in cases.items():
            with self.subTest(thirds=thirds):
                self.assertEqual(ways.edge_form_detail("right", *ways.pick_thirds([], thirds)), words)
        self.assertEqual(ways.edge_form_detail("top", ["part"], ["start", "middle"]), "Left and middle thirds")
        self.assertEqual(ways.edge_form_detail("", ["edge"], [], here="This PC"), "Place it beside This PC on the map")

    def test_the_map_click_targets_are_the_thirds_a_contact_covers(self):
        own = {"x": 0, "y": 0, "w": 150, "h": 99}
        self.assertEqual([third for third, *_ in ways.third_stretches(("right", 0, 99), own)], list(ways.PARTS))
        self.assertEqual(ways.third_stretches(("right", 0, 50), own),
                         [("start", "right", 0, 33.0), ("middle", "right", 33.0, 50)])
        self.assertEqual(ways.third_stretches(("corner", "top_right", ""), own), [])


if __name__ == "__main__":
    unittest.main()


class SmallerDisplayContacts(unittest.TestCase):
    """A smaller second screen scaled into its tile still meets the machine beside it."""

    def compiled(self, own, other):
        result = ways.compile_layout(settings(peer(A, "Ay")), {HERE: own, A: other})
        self.assertTrue(result["accepted"])
        return [(zone["kind"], zone.get("display"), zone.get("side")) for zone in result["settings"]["zones"]]

    def test_a_rounding_error_on_a_smaller_screens_outer_edge_still_touches(self):
        # 1920 + 1366 scaled into 255 units puts the small screen's right edge at 254.99999999999997.
        own = {"x": 0, "y": 0, "w": 255, "h": 84, "displays": {
            "main": {"x": 0, "y": 0, "w": 1920, "h": 1080}, "small": {"x": 1920, "y": 0, "w": 1366, "h": 768}}}
        self.assertEqual(self.compiled(own, {"x": 255, "y": 0, "w": 100, "h": 50}), [("edge", "small", "right")])

    def test_a_smaller_screen_on_the_peer_side_is_reached(self):
        other = {"x": -255, "y": 0, "w": 255, "h": 84, "displays": {
            "main": {"x": 0, "y": 0, "w": 1920, "h": 1080}, "small": {"x": 1920, "y": 0, "w": 1366, "h": 768}}}
        contacts = ways.display_contacts({"x": 0, "y": 0, "w": 160, "h": 90}, other)
        self.assertEqual([(target["display"], contact[0]) for _screen, contact, target in contacts], [("small", "left")])

    def test_a_peer_snaps_flush_under_a_shorter_screen_and_crosses(self):
        own = {"x": 0, "y": 0, "w": 240, "h": 77, "displays": {
            "main": {"x": 0, "y": 0, "w": 2560, "h": 1440}, "small": {"x": 2560, "y": 0, "w": 1920, "h": 1080}}}
        snapped = ways.snap_position({"x": 150, "y": 60, "w": 80, "h": 50}, HERE, {HERE: own})
        self.assertEqual(snapped["y"], 58)  # the small screen's bottom is at 57.75
        self.assertEqual(self.compiled(own, snapped), [("edge", "small", "bottom")])

    def test_integer_tiles_a_unit_apart_still_do_not_touch(self):
        self.assertIsNone(ways._rect_contact({"x": 0, "y": 0, "w": 10, "h": 10}, {"x": 11, "y": 0, "w": 10, "h": 10}))
        self.assertTrue(ways._overlaps({"x": 0, "y": 0, "w": 10, "h": 10}, {"x": 9, "y": 0, "w": 10, "h": 10}))
