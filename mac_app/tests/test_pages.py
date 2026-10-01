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
        self.assertEqual([group for group, _ in groups], ["Classic", "Membrane", "Sparks", "Instrument"])
        self.assertEqual([value for value, _title, _detail in groups[0][1]], ["glow", "beam"])
        for group, choices in groups[1:]:
            with self.subTest(group=group):
                self.assertEqual([detail for _value, _title, detail in choices], ["Quiet", "Medium", "Showpiece"])
        values = [value for _group, choices in groups[1:] for value, _title, _detail in choices]
        self.assertEqual(tuple(values), effects.EFFECT_IDS)
        self.assertEqual(groups[1][1][0][:2], ("skin", "Skin"))

    def test_colours_are_today_then_each_direction_three_packs(self):
        import crossing
        from core import effects

        groups = pages.colour_groups(crossing.GLOW_COLOURS)
        self.assertEqual(groups[0], ("Classic", [(name, name.capitalize()) for name in crossing.GLOW_COLOURS]))
        self.assertEqual([group for group, _ in groups[1:]], ["Membrane", "Sparks", "Instrument"])
        self.assertEqual(tuple(value for _group, choices in groups[1:] for value, _title in choices), effects.PACK_IDS)
        self.assertIn(("sodium", "Sodium"), groups[3][1])

    def test_without_the_effects_only_today_is_offered(self):
        import crossing

        self.assertIsNone(pages.effects_load_error())
        self.assertEqual([group for group, _ in pages.style_groups(False)], ["Classic"])
        self.assertEqual([group for group, _ in pages.colour_groups(crossing.GLOW_COLOURS, False)], ["Classic"])
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
        self.assertEqual([detail for _value, _name, detail in today], ["Classic", "Classic"])

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

    def test_the_crossing_line_gives_the_first_reason_nothing_can_cross(self):
        line = pages.crossing_state_sentence
        self.assertTrue(line(False, False, False, True, False, None).startswith("Not paired yet"))
        self.assertTrue(line(True, False, False, True, False, None).startswith("No machine is driven"))
        self.assertTrue(line(True, True, False, True, False, None).startswith("Not connected to any machine"))
        self.assertEqual(line(True, True, True, False, False, None), "Only the shortcut is switched on; there is nothing to pause.")
        self.assertTrue(line(True, True, True, True, True, None).startswith("Paused."))
        self.assertTrue(line(True, True, True, True, False, "Keynote").startswith("Off while Keynote is full screen"))
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


if __name__ == "__main__":
    unittest.main()
