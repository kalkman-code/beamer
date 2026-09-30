import os
import unittest
from types import SimpleNamespace
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app_config
from core import effects
import pages_win

try:
    import widgets
except ImportError:  # PySide6 is only in the Windows venv
    widgets = None


class PagesWinTest(unittest.TestCase):
    def test_keys_match_pages_in_order(self):
        self.assertEqual(pages_win.KEYS, tuple(page[0] for page in pages_win.PAGES))

    def test_the_sidebar_runs_in_setup_order(self):
        self.assertEqual(pages_win.KEYS,
                         ("overview", "crossing", "keyboard", "design", "connection"))

    def test_the_footer_drops_apply_as_you_go_on_connection(self):
        self.assertEqual(pages_win.footer("design"),
                         "Changes apply as you make them. Closing this window keeps Beamer running in the tray.")
        self.assertEqual(pages_win.footer("connection"), "Closing this window keeps Beamer running in the tray.")

    def test_every_page_has_a_name_and_purpose(self):
        for key, name, purpose in pages_win.PAGES:
            self.assertTrue(name)
            self.assertTrue(purpose)
            self.assertEqual(key, key.lower())

    def test_no_dots_when_all_clear(self):
        self.assertEqual(pages_win.dots(False, None), {})

    def test_no_dot_for_the_healthy_firewall_tone(self):
        # "note" is Firewall's own healthy end-state -- its button just offers a manual
        # re-check, so it must not earn a dot the way an amber or fault tone does.
        self.assertEqual(pages_win.dots(False, "note"), {})

    def test_config_error_marks_connection(self):
        self.assertEqual(pages_win.dots(True, None), {"connection": "fault"})

    def test_firewall_amber_marks_connection(self):
        self.assertEqual(pages_win.dots(False, "note-amber"), {"connection": "amber"})

    def test_firewall_fault_marks_connection(self):
        self.assertEqual(pages_win.dots(False, "note-fault"), {"connection": "fault"})

    def test_a_fault_outranks_a_firewall_amber(self):
        self.assertEqual(pages_win.dots(True, "note-amber"), {"connection": "fault"})


class DesignGroupsTest(unittest.TestCase):
    def test_the_preview_opens_where_the_pointer_crosses(self):
        self.assertEqual(pages_win.preview_place(["shortcut", "corner"]), "corner")
        self.assertEqual(pages_win.preview_place(["part", "corner"]), "edge")
        self.assertEqual(pages_win.preview_place(["shortcut"]), "edge")

    def test_every_place_plays_every_style_and_says_why_when_it_differs(self):
        self.assertEqual(pages_win.place_note("glow", "corner", ["corner"]), "")
        self.assertIn("not one of this PC's ways in", pages_win.place_note("beam", "edge", ["corner"]))

    def test_glow_and_beam_tiles_read_as_the_effects_do(self):
        today = pages_win.style_groups()[0][1]
        self.assertEqual([detail for _value, _name, detail in today], ["Classic", "Classic"])

    def test_styles_are_classic_then_each_direction_in_order(self):
        groups = pages_win.style_groups()
        self.assertEqual([title for title, _items in groups],
                         ["Classic", "Membrane", "Sparks", "Instrument"])
        self.assertEqual([style for style, _n, _d in groups[0][1]], ["glow", "beam"])
        effect_ids = tuple(style for _title, items in groups[1:] for style, _n, _d in items)
        self.assertEqual(effect_ids, effects.EFFECT_IDS)

    def test_every_effect_tile_names_its_intensity(self):
        for _title, items in pages_win.style_groups()[1:]:
            self.assertEqual([detail for _s, _n, detail in items], ["Quiet", "Medium", "Showpiece"])
            for _style, name, _detail in items:
                self.assertTrue(name)

    def test_colours_are_grouped_as_the_styles_are(self):
        groups = pages_win.colour_groups()
        self.assertEqual([title for title, _items in groups], [title for title, _items in pages_win.style_groups()])
        self.assertEqual(tuple(value for _t, items in groups for value, _name in items), app_config.GLOW_COLOURS)

    def test_effects_that_cannot_load_leave_only_the_classic_choices(self):
        real = effects._load

        def missing():
            raise ImportError("No module named 'fx_membrane'")

        effects._load = missing
        try:
            styles, colours = pages_win.style_groups(), pages_win.colour_groups()
        finally:
            effects._load = real
        self.assertEqual([title for title, _items in styles], ["Classic"])
        self.assertEqual([title for title, _items in colours], ["Classic"])

    def test_only_the_new_effects_count_as_effects(self):
        self.assertFalse(pages_win.is_effect("glow"))
        self.assertFalse(pages_win.is_effect("beam"))
        self.assertTrue(all(pages_win.is_effect(style) for style in effects.EFFECT_IDS))


class CrossingWaysTest(unittest.TestCase):
    def test_edge_and_part_of_the_edge_exclude_each_other(self):
        self.assertEqual(pages_win.toggle_way(["shortcut", "edge"], "part", True), ["shortcut", "part"])
        self.assertEqual(pages_win.toggle_way(["part", "corner"], "edge", True), ["edge", "corner"])
        self.assertEqual(pages_win.toggle_way(["edge"], "edge", False), [])
        self.assertEqual(pages_win.toggle_way(["corner"], "shortcut", True), ["shortcut", "corner"])

    def test_turning_on_a_way_already_on_keeps_one_copy(self):
        self.assertEqual(pages_win.toggle_way(["edge", "edge"], "edge", True), ["edge"])

    def test_any_parts_can_be_on_but_never_none(self):
        self.assertEqual(pages_win.toggle_part(["middle"], "start", True), ["start", "middle"])
        self.assertEqual(pages_win.toggle_part(["start", "end"], "start", False), ["end"])
        self.assertEqual(pages_win.toggle_part(["end"], "end", False), ["end"])

    def test_the_parts_are_named_along_the_edge(self):
        self.assertEqual([pages_win.part_names("right")[p] for p in ("start", "middle", "end")], ["Top", "Middle", "Bottom"])
        self.assertEqual([pages_win.part_names("bottom")[p] for p in ("start", "middle", "end")], ["Left", "Middle", "Right"])
        self.assertEqual(pages_win.parts_phrase("left", ["end", "start"]), "the top and bottom of the left edge")

    def test_no_parts_reads_as_the_middle_rather_than_failing(self):
        self.assertEqual(pages_win.parts_phrase("right", []), "the middle of the right edge")
        self.assertEqual(pages_win.parts_phrase("top", ["nowhere"]), "the middle of the top edge")

    def test_each_row_shows_for_the_ways_that_use_it(self):
        rows = pages_win.crossing_rows
        # Where the Mac sits shows whatever the ways: it is where this PC's edge leads.
        self.assertEqual(rows([]), {"edge"})
        self.assertEqual(rows(["shortcut"]), {"edge", "shortcut"})
        self.assertEqual(rows(["edge"]), {"edge", "dragging", "resistance"})
        self.assertEqual(rows(["part"]), {"edge", "parts", "dragging", "resistance"})
        self.assertEqual(rows(["corner"]), {"edge", "corner", "dragging", "resistance"})
        self.assertEqual(rows(["shortcut", "part", "corner"]),
                         {"shortcut", "edge", "parts", "corner", "dragging", "resistance"})

    def test_the_keyboard_page_no_longer_claims_the_switch_key(self):
        purposes = {key: purpose for key, _name, purpose in pages_win.PAGES}
        self.assertNotIn("key that", purposes["keyboard"])
        self.assertNotIn("shortcut", pages_win.SCOPE["keyboard"].lower())
        self.assertIn("a key moves input", purposes["crossing"])

    def test_the_mac_is_placed_left_right_above_or_below(self):
        self.assertEqual(pages_win.SIDE_CHOICES,
                         (("left", "Left"), ("right", "Right"), ("top", "Above"), ("bottom", "Below")))

    def test_the_summary_names_every_way_in_one_sentence(self):
        summary = pages_win.ways_summary
        self.assertEqual(summary(["edge"], "right", [], "top_left", "Right Ctrl", "double_tap"),
                         "Input moves to your Mac when you push through the whole right edge.")
        self.assertEqual(
            summary(["shortcut", "part", "corner"], "left", ["start"], "bottom_right", "Right Ctrl", "hold"),
            "Input moves to your Mac when you push through the top of the left edge, push diagonally into "
            "the bottom-right corner or hold Right Ctrl.")
        self.assertIn("choose at least one way", summary([], "left", [], "top_left", "F13", "hold"))

    def test_the_key_cap_line_says_how_the_key_is_pressed(self):
        self.assertEqual(pages_win.trigger_phrase("Right Ctrl", "double_tap"), "Right Ctrl, twice")
        self.assertEqual(pages_win.trigger_phrase("Right Ctrl", "hold"), "Right Ctrl, held")


class ArrangementGeometryTest(unittest.TestCase):
    SCREEN = (120.0, 75.0)

    def test_the_mac_sits_on_the_chosen_side_of_this_pc(self):
        for side, check in (
            ("right", lambda pc, mac: mac[0] == pc[0] + 134 and mac[1] == pc[1]),
            ("left", lambda pc, mac: mac[0] == pc[0] - 134 and mac[1] == pc[1]),
            ("top", lambda pc, mac: mac[1] == pc[1] - 89 and mac[0] == pc[0]),
            ("bottom", lambda pc, mac: mac[1] == pc[1] + 89 and mac[0] == pc[0]),
        ):
            pc, mac, _height = pages_win.arrangement_rects(400.0, 0.0, pages_win.SIDE_ANGLE[side], self.SCREEN, 14.0)
            self.assertTrue(check(pc, mac), (side, pc, mac))

    def test_the_pair_is_centred_and_only_as_tall_as_it_needs(self):
        pc, mac, height = pages_win.arrangement_rects(400.0, 10.0, 0.0, self.SCREEN, 14.0)
        self.assertEqual(height, 75.0)
        self.assertEqual((pc[0] + mac[0] + mac[2]) / 2.0, 200.0)
        self.assertEqual(pc[1], 10.0)
        _pc, _mac, stacked = pages_win.arrangement_rects(400.0, 0.0, 90.0, self.SCREEN, 14.0)
        self.assertEqual(stacked, 164.0)

    def test_the_mac_goes_round_this_pc_never_through_it(self):
        for angle in range(0, 360, 5):
            pc, mac, _height = pages_win.arrangement_rects(400.0, 0.0, float(angle), self.SCREEN, 14.0)
            apart_x = mac[0] >= pc[0] + pc[2] or mac[0] + mac[2] <= pc[0]
            apart_y = mac[1] >= pc[1] + pc[3] or mac[1] + mac[3] <= pc[1]
            self.assertTrue(apart_x or apart_y, angle)

    def test_a_turn_takes_the_short_way_and_half_turns_go_round_the_top(self):
        turn = pages_win.turn_to
        self.assertEqual(turn(0.0, "top"), 90.0)
        self.assertEqual(turn(0.0, "bottom"), -90.0)
        self.assertEqual(turn(0.0, "left"), 180.0)
        self.assertEqual(turn(180.0, "right"), 0.0)
        self.assertEqual(turn(90.0, "bottom"), 270.0)
        self.assertEqual(turn(270.0, "top"), 90.0)
        self.assertEqual(turn(45.0, "right"), 0.0)

    def test_the_lit_marks_sit_on_the_side_that_leads_to_the_mac(self):
        rect = (0.0, 0.0, 120.0, 90.0)
        self.assertEqual(pages_win.side_segment(rect, "right"), (120.0, 0.0, 120.0, 90.0))
        self.assertEqual(pages_win.side_segment(rect, "top", *pages_win.PART_SPANS["end"]), (80.0, 0.0, 120.0, 0.0))
        self.assertEqual(pages_win.corner_box(rect, "bottom_left", 10.0), (0.0, 80.0, 10.0, 10.0))

    def test_the_push_strip_fills_to_the_resistance(self):
        self.assertEqual(pages_win.push_depth(0, 300.0), 0.0)
        self.assertEqual(pages_win.push_depth(250, 300.0), 150.0)
        self.assertEqual(pages_win.push_depth(900, 300.0), 300.0)


class SwitchGroupsTest(unittest.TestCase):
    def test_the_switch_tiles_offer_exactly_the_switch_styles(self):
        values = [value for _title, items in pages_win.switch_groups() for value, _n, _d in items]
        self.assertEqual(sorted(values), sorted(effects.SWITCH_STYLES))
        self.assertEqual(len(values), len(set(values)))

    def test_simple_comes_first_then_the_crossing_directions(self):
        groups = pages_win.switch_groups()
        self.assertEqual(groups[0], ("Simple", (("match", "Same as crossing", "Plays the crossing style"),
                                                ("locator", "Ring", "Closes onto the pointer"))))
        self.assertEqual(groups[1:], pages_win.style_groups()[1:])


@unittest.skipIf(widgets is None, "needs PySide6")
class DesignControlsTest(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication, QWidget

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        self.QWidget = QWidget

    def test_one_style_across_every_group(self):
        chosen = []
        groups = [(title, [(style, name, detail, self.QWidget()) for style, name, detail in items])
                  for title, items in pages_win.style_groups()]
        tiles = widgets.TileGroups(groups, "glow", on_change=chosen.append)
        tiles.groups["Sparks"]._chosen("discharge")
        self.assertEqual((tiles.value, chosen), ("discharge", ["discharge"]))
        self.assertEqual(tiles.groups["Classic"].value, "discharge")
        tiles.set_value("skin")
        self.assertEqual(chosen, ["discharge"], "set_value must not report a change")
        self.assertTrue(all(group.value == "skin" for group in tiles.groups.values()))

    def test_one_colour_across_every_row(self):
        import effect_previews  # noqa: F401 -- the page's pictures import cleanly

        chosen = []
        groups = [(title, [(value, name, app_config.palette_colours(value)) for value, name in items])
                  for title, items in pages_win.colour_groups()]
        swatches = widgets.SwatchGroups(groups, "signal", on_change=chosen.append)
        swatches._swatches["ember"].click()
        self.assertEqual((swatches.value, chosen), ("ember", ["ember"]))
        self.assertFalse(swatches._swatches["signal"].isChecked())
        swatches.set_value("mono")
        self.assertEqual((swatches.value, chosen), ("mono", ["ember"]))

    def test_every_tile_draws_its_style_at_the_chosen_place_and_length(self):
        import effect_previews
        from PySide6.QtGui import QImage

        for style in ("glow", "beam", "rupture", "gauge"):
            still = effect_previews.EffectStill(style, ("#ff0000", "#0000ff"))
            still.resize(180, effect_previews.STILL_HEIGHT)
            pictures = []
            for place in ("edge", "corner"):
                still.set_place(place)
                still.set_pace(1.5)
                with self.subTest(style=style, place=place):
                    image = still.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
                    pictures.append(bytes(image.constBits()))
            self.assertEqual(len(set(pictures)), 2, style)

    def test_a_switch_tile_plays_at_the_chosen_length(self):
        import effect_previews

        still = effect_previews.SwitchStill("match", "rupture", ("#ff0000",))
        still.resize(180, effect_previews.STILL_HEIGHT)
        still.set_pace(0.75)
        still.playing_since = 0.0
        still.grab()
        self.assertIsNotNone(still.playing_since)

    def test_a_switch_still_follows_the_crossing_style(self):
        import effect_previews

        still = effect_previews.SwitchStill("match", "glow", ("#ffffff",))
        still.resize(160, effect_previews.STILL_HEIGHT)
        still.grab()
        drawn = still._image
        still.set_crossing_style("rupture")
        self.assertIsNone(still._image)
        still.grab()
        self.assertIsNot(still._image, drawn)


@unittest.skipIf(widgets is None, "needs PySide6")
class CrossingPageTest(unittest.TestCase):
    """The Crossing page's rows and chips, set from the config without building the whole window."""

    def setUp(self):
        from types import SimpleNamespace

        from PySide6.QtWidgets import QApplication, QCheckBox, QPushButton, QWidget

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        import diagram
        import kvm_bridge_win

        self.reflect = kvm_bridge_win.WindowsApplication._reflect_ways
        self.host = QWidget()
        self.page = SimpleNamespace(
            _config=app_config.default_config(),
            way_boxes={way: QCheckBox(way) for way in pages_win.WAYS},
            part_buttons={part: QPushButton() for part in ("start", "middle", "end")},
            parts_note=widgets.label("", "note"),
            # Inside a window that is never shown, as on a page before the window opens.
            crossing_rows={key: QWidget(self.host) for key in ("edge", "parts", "corner", "dragging")},
            resistance_module=QWidget(self.host),
            shortcut_module=QWidget(self.host),
            pause_row=QWidget(self.host),
            ways_summary=widgets.label("", "note"),
            edge_unlearned=QWidget(self.host),
            arrangement_diagram=diagram.ArrangementDiagram(),
            resistance_strip=diagram.PushStrip(),
        )
        for button in self.page.part_buttons.values():
            button.setCheckable(True)

    def shown(self):
        page = self.page
        rows = {key for key, row in page.crossing_rows.items() if not row.isHidden()}
        rows |= {"resistance"} - ({"resistance"} if page.resistance_module.isHidden() else set())
        rows |= {"shortcut"} - ({"shortcut"} if page.shortcut_module.isHidden() else set())
        return rows

    def test_rows_hide_and_come_back_with_the_ways(self):
        self.page._config.crossing_methods = ["shortcut"]
        self.reflect(self.page)
        self.assertEqual(self.shown(), {"edge", "shortcut"})
        self.page._config.crossing_methods = ["part"]
        self.reflect(self.page)
        self.assertEqual(self.shown(), {"edge", "parts", "dragging", "resistance"})
        self.assertTrue(self.page.way_boxes["part"].isChecked())
        self.assertFalse(self.page.way_boxes["shortcut"].isChecked())

    def test_the_chips_are_named_for_the_current_edge(self):
        self.page._config.mac_return_edge = "right"
        self.page._config.crossing_edge_parts = ["start", "end"]
        self.reflect(self.page)
        buttons = self.page.part_buttons
        self.assertEqual([buttons[p].text() for p in ("start", "middle", "end")], ["Top", "Middle", "Bottom"])
        self.assertEqual([buttons[p].isChecked() for p in ("start", "middle", "end")], [True, False, True])
        self.page._config.mac_return_edge = "top"
        self.reflect(self.page)
        self.assertEqual([buttons[p].text() for p in ("start", "middle", "end")], ["Left", "Middle", "Right"])

    def test_an_edge_learned_from_the_mac_renames_the_chips(self):
        import kvm_bridge_win

        page = self.page
        page._config.crossing_methods = ["part"]
        page._config.mac_return_edge = "right"
        self.reflect(page)
        page._persist = lambda: True
        page._start_sending = lambda config: None
        page._reflect_look = lambda: None
        page._reflect_ways = lambda: self.reflect(page)
        page.sender = SimpleNamespace(update_config=lambda config: None)
        page.mac_host_readout = widgets.label("", "readout")
        page.edge_choice = SimpleNamespace(set_value=lambda value: None)
        # A hello fills only an edge the PC does not hold yet (G1).
        page._config.mac_return_edge = ""
        real = kvm_bridge_win.sender.is_this_machine
        kvm_bridge_win.sender.is_this_machine = lambda host: False
        try:
            kvm_bridge_win.WindowsApplication._on_learned(page, "192.168.1.10", "top", 120)
        finally:
            kvm_bridge_win.sender.is_this_machine = real
        buttons = page.part_buttons
        self.assertEqual([buttons[p].text() for p in ("start", "middle", "end")], ["Left", "Middle", "Right"])
        self.assertIn("top edge", page.parts_note.text())

    def test_unticking_the_last_part_leaves_it_on(self):
        import kvm_bridge_win

        page = self.page
        page._config.crossing_edge_parts = ["middle"]
        page._persist = lambda: True
        page.sender = SimpleNamespace(update_config=lambda config: None)
        page._reflect_ways = lambda: self.reflect(page)
        page._reflect_look = lambda: None
        page.part_buttons["middle"].setChecked(False)
        kvm_bridge_win.WindowsApplication._set_part(page, "middle", False)
        self.assertEqual(page._config.crossing_edge_parts, ["middle"])
        self.assertTrue(page.part_buttons["middle"].isChecked())

    def test_the_crossing_line_gives_the_first_reason_nothing_can_cross(self):
        line = pages_win.crossing_state_sentence
        self.assertTrue(line(False, False, False, False, True, False, None).startswith("Not paired yet"))
        self.assertTrue(line(True, False, True, False, True, False, None).startswith("Waiting to hear from your Mac"))
        self.assertTrue(line(True, True, False, False, True, False, None).startswith("This PC drives your Mac is off"))
        self.assertTrue(line(True, True, True, False, True, False, None).startswith("Not connected to your Mac"))
        self.assertTrue(line(True, True, True, True, False, False, None).startswith("Only the shortcut"))
        self.assertTrue(line(True, True, True, True, True, True, None).startswith("Paused."))
        self.assertTrue(line(True, True, True, True, True, False, "Keynote").startswith("Off while Keynote"))
        self.assertEqual(line(True, True, True, True, True, False, None), "On. Pause it to lean on an edge without switching.")

    def test_a_reason_shows_even_when_only_the_shortcut_is_on(self):
        self.assertTrue(pages_win.crossing_state_blocked(False, True, True, True))
        self.assertTrue(pages_win.crossing_state_blocked(True, False, True, True))
        self.assertTrue(pages_win.crossing_state_blocked(True, True, False, True))
        self.assertTrue(pages_win.crossing_state_blocked(True, True, True, False))
        self.assertFalse(pages_win.crossing_state_blocked(True, True, True, True))

    def test_the_overview_says_whether_this_pcs_own_link_is_up(self):
        self.assertEqual(pages_win.outward_link_line(True), "This PC to your Mac: Linked")
        self.assertEqual(pages_win.outward_link_line(False),
                         "This PC to your Mac: not connected, so pushing an edge does nothing")

    def test_no_learned_edge_is_not_described_as_the_right_edge(self):
        summary = pages_win.ways_summary(["edge"], "", ["middle"], "top_right", "Right Ctrl", "double_tap")
        self.assertNotIn("right edge", summary)
        self.assertTrue(summary.startswith("Nothing moves input to your Mac until"))
        self.assertIn("the whole right edge", pages_win.ways_summary(["edge"], "right", ["middle"], "top_right", "Right Ctrl", "double_tap"))



class RedactTests(unittest.TestCase):
    def test_hides_ip_and_hardware_addresses_only_when_asked(self):
        line = "MacBook Pro at 192.168.1.10:24820, woken by 02-1A-2B-3C-0D-4E"
        self.assertEqual(pages_win.redact(line, False), line)
        self.assertEqual(pages_win.redact(line, True), "MacBook Pro at •••:24820, woken by •••")
        self.assertEqual(pages_win.redact("Ready on TCP port 24820", True), "Ready on TCP port 24820")


if __name__ == "__main__":
    unittest.main()
