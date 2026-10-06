import sys
import unittest
from pathlib import Path
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import effects
import pages


class PagesTest(unittest.TestCase):
    def test_order(self):
        self.assertEqual(
            pages.KEYS, ("overview", "permissions", "crossing", "keyboard", "design", "connection")
        )

    def test_the_footer_does_not_claim_connection_applies_as_you_type(self):
        self.assertTrue(pages.footer("design").startswith("Changes apply as you make them."))
        self.assertEqual(pages.footer("connection"), pages.FOOTER_RUNNING)

    def test_first_open(self):
        self.assertEqual(pages.opening_page(None, True, True), "overview")
        self.assertEqual(pages.opening_page(None, False, True), "permissions")
        self.assertEqual(pages.opening_page(None, True, False), "permissions")

    def test_later_opens_keep_the_last_page(self):
        self.assertEqual(pages.opening_page("keyboard", False, False), "keyboard")
        self.assertEqual(pages.opening_page("overview", False, True), "overview")

    def test_dots(self):
        self.assertEqual(pages.dots(True, True, "mac"), {})
        self.assertEqual(pages.dots(True, False, "mac"), {"permissions": "amber"})
        self.assertEqual(pages.dots(True, True, "token"), {"overview": "fault"})
        self.assertEqual(pages.dots(False, False, "token"), {"permissions": "amber", "overview": "fault"})
        self.assertEqual(pages.dots(True, True, "unreachable"), {})

    def test_step_stops_at_the_ends(self):
        self.assertEqual(pages.step("overview", -1), "overview")
        self.assertEqual(pages.step("overview", 1), "permissions")
        self.assertEqual(pages.step("connection", 1), "connection")
        self.assertEqual(pages.step("keyboard", -1), "crossing")


class DiagramTest(unittest.TestCase):
    def test_the_other_screen_sits_on_the_chosen_side_with_the_pair_centred(self):
        for side in ("left", "right", "top", "bottom"):
            with self.subTest(side=side):
                (tx, ty, tw, th), (ox, oy, ow, oh) = pages.arrangement(400, 158, side)
                if side == "right":
                    self.assertEqual(ox, tx + tw + pages.DIAGRAM_GAP)
                elif side == "left":
                    self.assertEqual(ox + ow + pages.DIAGRAM_GAP, tx)
                elif side == "top":
                    self.assertEqual(oy + oh + pages.DIAGRAM_GAP, ty)
                else:
                    self.assertEqual(oy, ty + th + pages.DIAGRAM_GAP)
                left, right = min(tx, ox), max(tx + tw, ox + ow)
                top, bottom = min(ty, oy), max(ty + th, oy + oh)
                self.assertAlmostEqual(left + right, 400)
                self.assertAlmostEqual(top + bottom, 158)
                self.assertGreaterEqual(top, 0)

    def test_the_thirds_run_start_to_end_along_the_side_strip(self):
        screen = (0, 0, 104, 65)
        thirds = pages.third_marks(screen, "right")
        self.assertEqual([frame[0] for frame in thirds.values()], [101] * 3)
        self.assertLess(thirds["start"][1], thirds["middle"][1])
        self.assertAlmostEqual(thirds["end"][1] + thirds["end"][3], 65)
        along_top = pages.third_marks(screen, "top")
        self.assertLess(along_top["start"][0], along_top["end"][0])

    def test_the_corner_mark_sits_in_its_corner(self):
        self.assertEqual(pages.corner_mark((10, 20, 104, 65), "bottom_right"), (102, 73, 12, 12))
        self.assertEqual(pages.corner_mark((10, 20, 104, 65), "top_left"), (10, 20, 12, 12))


class DesignChoicesTest(unittest.TestCase):
    def test_styles_are_today_then_each_direction_quiet_medium_showpiece(self):
        from core import effects

        groups = pages.style_groups()
        self.assertEqual([group for group, _ in groups], ["Light", "Membrane", "Sparks", "Instrument", "Folio", "Selvedge"])
        self.assertEqual([value for value, _title, _detail in groups[0][1]], ["glow", "beam", "aperture"])
        self.assertEqual([detail for _value, _title, detail in groups[0][1]], ["Quiet", "Medium", "Showpiece"])
        for group, choices in groups[1:]:
            with self.subTest(group=group):
                self.assertEqual([detail for _value, _title, detail in choices], ["Quiet", "Medium", "Showpiece"])
        values = [value for _group, choices in groups for value, _title, _detail in choices if value not in ("glow", "beam")]
        self.assertEqual(tuple(values), effects.EFFECT_IDS)
        self.assertEqual(groups[1][1][0][:2], ("skin", "Skin"))

    def test_colours_offer_five_per_group_including_mono(self):
        import crossing
        from core import effects

        groups = pages.colour_groups(crossing.GLOW_COLOURS)
        self.assertEqual([group for group, _ in groups], ['Light', 'Mono', 'Membrane', 'Sparks', 'Instrument', 'Folio', 'Selvedge', 'Bright'])
        self.assertEqual(groups[1][1], [(value, name) for value, (name, _stops) in effects.MONO_PACKS.items()])
        self.assertTrue(all(len(choices) == 5 for group, choices in groups))
        self.assertCountEqual([value for _group, choices in groups[1:] for value, _title in choices], effects.PACK_IDS)
        self.assertIn(("sodium", "Sodium"), groups[4][1])

    def test_without_the_effects_only_today_is_offered(self):
        import crossing

        self.assertIsNone(pages.effects_load_error())
        self.assertEqual([group for group, _ in pages.style_groups(False)], ["Light"])
        self.assertEqual([group for group, _ in pages.colour_groups(crossing.GLOW_COLOURS, False)], ["Light", "Mono"])
        from unittest import mock
        from core import effects

        with mock.patch.object(effects, "_load", side_effect=ModuleNotFoundError("fx_ink")):
            self.assertIsInstance(pages.effects_load_error(), ModuleNotFoundError)

    def test_the_preview_opens_where_the_pointer_crosses(self):
        self.assertEqual(pages.preview_place(["shortcut", "corner"]), "corner")
        self.assertEqual(pages.preview_place(["notch"]), "notch")
        self.assertEqual(pages.preview_place(["part", "corner"]), "edge")
        self.assertEqual(pages.preview_place(["shortcut"]), "edge")

    def test_every_place_plays_every_style_and_says_why_when_it_differs(self):
        self.assertEqual(pages.place_note("glow", "corner", ["corner"], True), "")
        self.assertEqual(pages.place_note("flint", "edge", ["edge"], True), "")
        self.assertIn("not one of your ways in", pages.place_note("glow", "corner", ["edge"], True))
        self.assertIn("notch style you choose here plays instead", pages.place_note("beam", "notch", ["notch"], True))
        self.assertIn("Flint draws its own form", pages.place_note("flint", "notch", ["notch"], True))
        self.assertIn("no notch", pages.place_note("glow", "notch", ["notch"], False))

    def test_glow_and_beam_tiles_read_as_the_effects_do(self):
        today = pages.style_groups()[0][1]
        self.assertEqual([detail for _value, _name, detail in today], ["Quiet", "Medium", "Showpiece"])

    def test_the_notch_style_only_matters_under_glow_and_beam(self):
        self.assertTrue(pages.notch_style_applies("glow"))
        self.assertTrue(pages.notch_style_applies("beam"))
        self.assertFalse(pages.notch_style_applies("rupture"))
        self.assertFalse(pages.notch_style_applies("discharge"))



class CrossingPageRulesTests(unittest.TestCase):
    def test_edge_and_part_of_the_edge_exclude_each_other(self):
        self.assertEqual(pages.toggle_way(["shortcut", "edge"], "part", True), ["shortcut", "part"])
        self.assertEqual(pages.toggle_way(["part", "corner"], "edge", True), ["edge", "corner"])
        self.assertEqual(pages.toggle_way(["edge", "corner"], "corner", False), ["edge"])

    def test_the_last_third_cannot_be_unticked(self):
        self.assertEqual(pages.toggle_part(["middle"], "middle", False), ["middle"])
        self.assertEqual(pages.toggle_part(["start", "middle"], "start", False), ["middle"])
        self.assertEqual(pages.toggle_part(["middle"], "end", True), ["middle", "end"])

    def test_rows_follow_the_ways(self):
        self.assertEqual(pages.crossing_rows(["shortcut"]),
                         {"edge": True, "parts": False, "corner": False, "dragging": False,
                          "resistance": False, "shortcut": True})
        rows = pages.crossing_rows(["part"])
        self.assertTrue(rows["edge"] and rows["parts"] and rows["resistance"])
        self.assertFalse(rows["shortcut"] or rows["corner"])
        self.assertTrue(pages.crossing_rows(["corner"])["edge"])
        self.assertTrue(pages.crossing_rows(["notch"])["resistance"])
        # Where the PC sits shows whatever the ways: the PC's edge facing this Mac always leads here.
        self.assertTrue(pages.crossing_rows(["notch"])["edge"])

    def test_the_switch_tiles_offer_every_switch_style_once(self):
        offered = [value for _group, choices in pages.switch_style_groups() for value, _name, _detail in choices]
        self.assertEqual(sorted(offered), sorted(effects.SWITCH_STYLES))
        self.assertEqual(pages.switch_style_groups()[0][0], pages.SIMPLE)

    def test_parts_are_named_along_the_edge(self):
        self.assertEqual(pages.part_names("right")["start"], "Top")
        self.assertEqual(pages.part_names("top")["end"], "Right")
        self.assertEqual(pages.parts_phrase("left", ["start", "middle"]), "the top and middle of the left edge")
        self.assertEqual(pages.parts_phrase("top", []), "the middle of the top edge")
        self.assertEqual(pages.parts_phrase("top", ["sideways"]), "the middle of the top edge")

    def test_a_shared_side_names_its_holder_and_uses_plural_when_needed(self):
        self.assertEqual(pages.share_sentence(["Bee"], "right", "Oak"),
                         "Bee already crosses from the right edge of this Mac. The thirds you pick go to Oak; "
                         "the rest stay with Bee.")
        self.assertEqual(pages.share_sentence(["Bee", "Sea"], "top", "Oak"),
                         "Bee and Sea already cross from the top edge of this Mac. The thirds you pick go to Oak; "
                         "the rest stay where they are.")

    def test_the_crossing_line_gives_the_first_reason_nothing_can_cross(self):
        line = pages.crossing_state_sentence
        self.assertTrue(line(False, False, False, True, False, None).startswith("Not paired yet"))
        self.assertTrue(line(True, False, False, True, False, None).startswith("No machine is driven"))
        self.assertTrue(line(True, True, False, True, False, None).startswith("Not connected to any machine"))
        self.assertEqual(line(True, True, True, False, False, None), "Only the shortcut is switched on; there is nothing to pause.")
        self.assertTrue(line(True, True, True, True, True, None).startswith("Pauses pointer crossing"))
        self.assertEqual(line(True, True, True, True, False, "Keynote"), "Held: an app is full screen.")
        self.assertEqual(line(True, True, True, True, False, None), "On. Pause it to lean on an edge without switching.")

    def test_a_reason_shows_even_when_only_the_shortcut_is_on(self):
        self.assertTrue(pages.crossing_state_blocked(False, True, True))
        self.assertTrue(pages.crossing_state_blocked(True, False, True))
        self.assertTrue(pages.crossing_state_blocked(True, True, False))
        self.assertFalse(pages.crossing_state_blocked(True, True, True))

    def test_the_notch_and_corner_note_shows_only_when_they_are_the_pointer_ways(self):
        self.assertTrue(pages.notch_or_corner_only(["notch", "shortcut"]))
        self.assertTrue(pages.notch_or_corner_only(["corner"]))
        self.assertTrue(pages.notch_or_corner_only(["corner", "notch"]))
        self.assertFalse(pages.notch_or_corner_only(["notch", "edge"]))
        self.assertFalse(pages.notch_or_corner_only(["corner", "part"]))
        self.assertFalse(pages.notch_or_corner_only(["shortcut"]))



class RedactTests(unittest.TestCase):
    def test_hides_ip_and_hardware_addresses_only_when_asked(self):
        line = "Desktop PC at 192.168.1.20:24820, woken by 02:1A:2B:3C:0D:4E"
        self.assertEqual(pages.redact(line, False), line)
        self.assertEqual(pages.redact(line, True), "Desktop PC at •••:24820, woken by •••")
        self.assertEqual(pages.redact("port 24820 and version 1.4.0", True), "port 24820 and version 1.4.0")
        self.assertEqual(pages.redact("", True), "")


def machine(label, side, methods=(), parts=("middle",), corner="top_left", chosen=False, key=None):
    return {"key": key or label, "label": label, "side": side, "methods": list(methods), "parts": list(parts),
            "corner": corner, "chosen": chosen}


class NeighboursTest(unittest.TestCase):
    """This Mac in the middle of the Crossing page's drawing, every placed machine beside its side."""

    W, H = 520.0, 186.0

    def inside(self, frame):
        x, y, w, h = frame
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + w, self.W + 1e-6)
        self.assertLessEqual(y + h, self.H + 1e-6)

    def test_one_machine_sits_where_the_one_machine_drawing_put_it(self):
        for side in ("left", "right", "top", "bottom"):
            with self.subTest(side=side):
                this, frames = pages.diagram_layout(self.W, self.H, [(side, None)])
                self.assertEqual((this, frames[0]), pages.arrangement(self.W, self.H, side))

    def test_a_machine_with_no_side_is_not_drawn_and_this_mac_stays_in_the_middle(self):
        this, frames = pages.diagram_layout(self.W, self.H, [("", None)])
        self.assertIsNone(frames[0])
        self.assertAlmostEqual(this[0] + this[2] / 2, self.W / 2)
        self.assertAlmostEqual(this[1] + this[3] / 2, self.H / 2)

    def test_a_row_puts_one_machine_each_side(self):
        this, (left, right) = pages.diagram_layout(self.W, self.H, [("left", None), ("right", None)])
        self.assertAlmostEqual(left[0] + left[2] + pages.DIAGRAM_GAP, this[0])
        self.assertAlmostEqual(this[0] + this[2] + pages.DIAGRAM_GAP, right[0])
        self.assertAlmostEqual(this[0] + this[2] / 2, self.W / 2)
        for frame in (this, left, right):
            self.inside(frame)

    def test_an_l_keeps_both_neighbours_on_their_sides_and_the_whole_inside_the_drawing(self):
        this, (right, top) = pages.diagram_layout(self.W, self.H, [("right", None), ("top", None)])
        self.assertGreater(right[0], this[0] + this[2])
        self.assertLess(top[1] + top[3], this[1])
        for frame in (this, right, top):
            self.inside(frame)

    def test_two_on_one_side_share_it_each_half_as_long_in_the_order_of_their_thirds(self):
        this, (lower, upper) = pages.diagram_layout(self.W, self.H, [("right", 2), ("right", 0)])
        _, (single,) = pages.diagram_layout(self.W, self.H, [("right", None)])
        self.assertAlmostEqual(upper[3], single[3] / 2)
        self.assertAlmostEqual(upper[2], single[2] / 2)
        self.assertLess(upper[1] + upper[3], lower[1])
        self.assertEqual(upper[0], lower[0])
        self.assertGreater(upper[0], this[0] + this[2])

    def test_two_on_one_side_without_thirds_follow_the_list(self):
        _, (first, second) = pages.diagram_layout(self.W, self.H, [("top", None), ("top", 0)])
        self.assertLess(first[0], second[0])

    def test_a_machine_is_placed_by_its_side_and_its_first_third(self):
        self.assertEqual(pages.placement(machine("Bee", "right", ["part"], parts=["end", "middle"])), ("right", 1))
        self.assertEqual(pages.placement(machine("Bee", "right", ["edge"], parts=["end"])), ("right", None))
        self.assertEqual(pages.placement(machine("Bee", "", ["part"])), ("", 1))

    def test_four_sides_shrink_to_fit(self):
        this, frames = pages.diagram_layout(self.W, self.H, [(side, None) for side in ("left", "right", "top", "bottom")])
        for frame in (this, *frames):
            self.inside(frame)
        self.assertLess(this[2], pages.DIAGRAM_THIS[0])


class MarksTest(unittest.TestCase):
    """What lights on this Mac: the chosen machine's ways strong, the others' quiet."""

    this = (100.0, 50.0, 120.0, 75.0)

    def test_the_chosen_machines_edge_is_strong_and_anothers_quiet(self):
        marks, notch = pages.diagram_marks(self.this, [
            machine("Bee", "right", ["edge"], chosen=True), machine("Sea", "left", ["edge"])])
        self.assertEqual(marks[("Bee", "edge")], (pages.side_mark(self.this, "right"), "chosen"))
        self.assertEqual(marks[("Sea", "edge")], (pages.side_mark(self.this, "left"), "other"))
        self.assertIsNone(notch)

    def test_a_way_that_is_off_or_has_no_side_lights_nothing(self):
        marks, _ = pages.diagram_marks(self.this, [machine("Bee", "", ["edge", "corner"], chosen=True)])
        self.assertEqual(marks[("Bee", "edge")], (None, None))
        self.assertEqual(marks[("Bee", "corner")], (pages.corner_mark(self.this, "top_left"), "chosen"))
        self.assertIsNone(marks[("Bee", "part", "start")][1])

    def test_unchosen_thirds_of_the_chosen_machine_are_a_track_unless_another_machine_has_them(self):
        marks, _ = pages.diagram_marks(self.this, [
            machine("Bee", "right", ["part"], parts=["start"], chosen=True),
            machine("Sea", "right", ["part"], parts=["end"])])
        thirds = pages.third_marks(self.this, "right")
        self.assertEqual(marks[("Bee", "part", "start")], (thirds["start"], "chosen"))
        self.assertEqual(marks[("Bee", "part", "middle")], (thirds["middle"], "track"))
        self.assertEqual(marks[("Bee", "part", "end")][1], None)
        self.assertEqual(marks[("Sea", "part", "end")], (thirds["end"], "other"))
        self.assertIsNone(marks[("Sea", "part", "middle")][1])

    def test_the_notch_lights_for_the_machine_it_leads_to(self):
        _, notch = pages.diagram_marks(self.this, [machine("Bee", "right", chosen=True), machine("Sea", "left", ["notch"])])
        self.assertEqual(notch, "other")
        _, notch = pages.diagram_marks(self.this, [machine("Bee", "right", ["notch"], chosen=True)])
        self.assertEqual(notch, "chosen")


class MachineWordsTest(unittest.TestCase):
    def test_machines_with_no_side_are_named_under_the_drawing(self):
        self.assertEqual(pages.not_placed([machine("Bee", "right"), machine("Sea", "")]), "Not placed yet: Sea")
        self.assertEqual(pages.not_placed([machine("Oak", ""), machine("Sea", "")]), "Not placed yet: Oak and Sea")
        self.assertEqual(pages.not_placed([machine("Bee", "left")]), "")

    def test_the_description_names_every_machine_and_its_side(self):
        text = pages.arrangement_description(
            [machine("Bee", "right", chosen=True), machine("Sea", "top"), machine("Oak", "")], "Push through the right edge")
        self.assertEqual(text, "Bee is to the right of this Mac. Sea is above this Mac. Oak is not placed yet. "
                               "Push through the right edge.")
        self.assertEqual(pages.arrangement_description([machine("Bee", "left", chosen=True)], ""),
                         "Bee is to the left of this Mac. No way in is on.")

    def test_one_machine_keeps_todays_words_and_several_name_the_one_chosen(self):
        self.assertEqual(pages.where_caption(None), "Where the other machine is")
        self.assertEqual(pages.where_caption("Bee"), "Where Bee is")
        self.assertEqual(pages.notch_or_corner_note(None), pages.notch_or_corner_note("the other machine"))
        self.assertIn("Bee's own push", pages.notch_or_corner_note("Bee"))

    def test_an_unplaced_machine_shows_no_side_unless_it_is_the_only_one(self):
        self.assertEqual(pages.shown_side("", 1), "right")
        self.assertEqual(pages.shown_side("", 0), "right")
        self.assertEqual(pages.shown_side("", 2), "")
        self.assertEqual(pages.shown_side("top", 3), "top")


class SendItemsTest(unittest.TestCase):
    """The menu bar's Send input, one per machine once there are several to send to."""

    @staticmethod
    def entry(ident, name, **extra):
        return {"id": ident, "name": name, "token": "t" + ident, "host": "192.0.2.4", "port": 24820, "send": True, **extra}

    def test_one_machine_keeps_the_one_item(self):
        self.assertEqual(pages.send_items([self.entry("A", "Bee")], None, False), [])
        self.assertEqual(pages.send_items([self.entry("A", "Bee"), self.entry("B", "Sea", send=False)], None, False), [])

    def test_each_machine_this_mac_sends_to_has_its_own_and_the_one_input_is_on_brings_it_back(self):
        peers = [self.entry("A", "Bee"), self.entry("B", "Sea"), self.entry("C", "Oak", send=False)]
        self.assertEqual(pages.send_items(peers, None, False), [("A", "Send input to Bee"), ("B", "Send input to Sea")])
        self.assertEqual(pages.send_items(peers, "B", False), [("A", "Send input to Bee"), ("B", "Bring input back")])

    def test_a_machine_not_in_use_has_no_send_item(self):
        peers = [self.entry("A", "Bee"), self.entry("B", "Sea"), self.entry("C", "Oak", in_use=False)]
        self.assertEqual(pages.send_items(peers, None, False), [("A", "Send input to Bee"), ("B", "Send input to Sea")])

    def test_a_nameless_machines_address_is_hidden_when_asked(self):
        peers = [self.entry("A", ""), self.entry("B", "Sea")]
        self.assertEqual(pages.send_items(peers, None, True)[0][1], "Send input to •••")


if __name__ == "__main__":
    unittest.main()
