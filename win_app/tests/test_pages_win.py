import os
import unittest
import sys
import time
from unittest import mock

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app_config
import platform_parts
from core import effects
from core import ways
import pages_win

try:
    import widgets
except ImportError:  # PySide6 is only in the Windows venv
    widgets = None


def _contact(row):
    return next((chip for chip in row.edge_chips.chips.values() if chip.kind == "contact" and not chip.isHidden()), None)


def _picked(row):
    chip = _contact(row)
    return [third for third, segment in chip.segments.items() if segment.isChecked()] if chip else []


def _corners(row):
    return [corner for corner, button in row.corner_grid.buttons.items() if button.isChecked()]


class PagesWinTest(unittest.TestCase):
    def test_crossing_machine_picker_omits_peers_not_in_use(self):
        peers = [
            {"id": "A", "name": "Bee", "token": "ta", "port": 24820},
            {"id": "B", "name": "Sea", "token": "tb", "port": 24820, "in_use": False},
        ]
        self.assertEqual(pages_win.crossing_machines(peers), [("A", "Bee")])

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
        self.assertEqual([detail for _value, _name, detail in today], ["Quiet", "Medium", "Showpiece"])

    def test_styles_are_classic_then_each_direction_in_order(self):
        groups = pages_win.style_groups()
        self.assertEqual([title for title, _items in groups],
                         ["Light", "Membrane", "Sparks", "Instrument", "Folio", "Selvedge"])
        self.assertEqual([style for style, _n, _d in groups[0][1]], ["glow", "beam", "aperture"])
        effect_ids = tuple(style for _title, items in groups for style, _n, _d in items if style not in ("glow", "beam"))
        self.assertEqual(effect_ids, effects.EFFECT_IDS)

    def test_every_effect_tile_names_its_intensity(self):
        for _title, items in pages_win.style_groups()[1:]:
            self.assertEqual([detail for _s, _n, detail in items], ["Quiet", "Medium", "Showpiece"])
            for _style, name, _detail in items:
                self.assertTrue(name)

    def test_colours_are_grouped_as_the_styles_are(self):
        groups = pages_win.colour_groups()
        self.assertEqual([title for title, _items in groups], ['Light', 'Mono', 'Membrane', 'Sparks', 'Instrument', 'Folio', 'Selvedge', 'Bright'])
        self.assertEqual(dict(groups)['Mono'], tuple((key, name) for key, (name, _stops) in effects.MONO_PACKS.items()))
        self.assertTrue(all(len(items) == 5 for title, items in groups))
        self.assertEqual(set(value for _t, items in groups for value, _name in items), set(app_config.GLOW_COLOURS))

    def test_effects_that_cannot_load_leave_only_the_classic_choices(self):
        real = effects._load

        def missing():
            raise ImportError("No module named 'fx_membrane'")

        effects._load = missing
        try:
            styles, colours = pages_win.style_groups(), pages_win.colour_groups()
        finally:
            effects._load = real
        self.assertEqual([title for title, _items in styles], ["Light"])
        self.assertEqual([title for title, _items in colours], ["Light", "Mono"])

    def test_only_the_new_effects_count_as_effects(self):
        self.assertFalse(pages_win.is_effect("glow"))
        self.assertFalse(pages_win.is_effect("beam"))
        self.assertTrue(all(pages_win.is_effect(style) for style in effects.EFFECT_IDS))


class CrossingWaysTest(unittest.TestCase):
    def test_a_shared_side_names_its_holder_and_uses_plural_when_needed(self):
        self.assertEqual(pages_win.share_sentence(["Bee"], "right", "Oak"),
                         "Bee already crosses from the right edge of this PC. The thirds you pick go to Oak; "
                         "the rest stay with Bee.")
        self.assertEqual(pages_win.share_sentence(["Bee", "Sea"], "top", "Oak"),
                         "Bee and Sea already cross from the top edge of this PC. The thirds you pick go to Oak; "
                         "the rest stay where they are.")

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
                         "Input moves to the other machine when you push through the whole right edge.")
        self.assertEqual(
            summary(["shortcut", "part", "corner"], "left", ["start"], "bottom_right", "Right Ctrl", "hold"),
            "Input moves to the other machine when you push through the top of the left edge, push diagonally into "
            "the bottom-right corner or hold Right Ctrl.")
        self.assertIn("choose at least one way", summary([], "left", [], "top_left", "F13", "hold"))

    def test_with_no_side_learned_the_summary_still_names_the_shortcut_which_needs_none(self):
        summary = pages_win.ways_summary
        self.assertEqual(summary(["edge", "corner", "shortcut"], "", [], "top_left", "Right Ctrl", "double_tap", "Bee"),
                         "Input moves to Bee when you press Right Ctrl twice. Its edges and corner wait until this PC "
                         "knows which side Bee is on.")
        self.assertEqual(summary(["edge"], "", [], "top_left", "Right Ctrl", "double_tap", "Bee"),
                         "Nothing moves input to Bee until this PC knows which side it is on.")

    def test_the_key_cap_line_says_how_the_key_is_pressed(self):
        self.assertEqual(pages_win.trigger_phrase("Right Ctrl", "double_tap"), "Right Ctrl, twice")
        self.assertEqual(pages_win.trigger_phrase("Right Ctrl", "hold"), "Right Ctrl, held")


class ArrangementGeometryTest(unittest.TestCase):
    # Where each screen sits: test_pc_three_machines.DiagramGeometryTests, one machine and several.

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

    def test_simple_and_aperture_come_first_then_the_crossing_directions(self):
        groups = pages_win.switch_groups()
        self.assertEqual(groups[0], ("Simple", (("match", "Same as crossing", "Plays Glow"),
                                                ("locator", "Ring", "Closes onto the pointer"))))
        self.assertEqual(groups[1], ("Light", (("aperture", "Aperture", "Showpiece"),)))
        self.assertEqual(groups[2:], pages_win.style_groups()[1:])


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
        self.assertEqual(tiles.groups["Light"].value, "discharge")
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
    """The Crossing page's rows and chips, set from one machine's ways in the settings, in the real
    window built offscreen, which starts no receiver, hooks or announcer."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        import kvm_bridge_win
        import theme
        from core import protocol
        from core.tests import responder_harness as harness

        theme.init_fonts()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "settings.json"
        settings = app_config.migrate(None, machine_id=protocol.id_text(harness.HERE))
        self.peer = protocol.id_text(harness.B)
        settings["peers"] = [harness.entry(harness.B, "Mac", side="right")]
        settings["port"] = harness.free_port()
        app_config.write_settings(self.path, settings)
        self.page = kvm_bridge_win.WindowsApplication(self.path)
        self.page._send_to = lambda peer, message: True
        self.addCleanup(QCoreApplication.sendPostedEvents, None, QEvent.Type.DeferredDelete)
        self.addCleanup(self.page.deleteLater)
        self.addCleanup(self.page.server.stop)

    def ways(self, methods=(), parts=("middle",), side=None):
        app_config.set_ways(self.path, self.peer, side=side, methods=list(methods), parts=list(parts), corner="top_left")
        self.page._pull_peer_fields()

    def test_design_frame_rate_offers_and_saves_all_three_choices(self):
        import frame_clock
        self.addCleanup(frame_clock.set_divisor, 1)
        choice = self.page.frame_rate_choice
        self.assertEqual([(value, button.text()) for value, button in choice._buttons.items()],
                         [("display", "Match display"), ("half", "Half"), ("quarter", "Quarter")])
        choice._buttons["quarter"].click()
        self.assertEqual(app_config.load_config(self.path).frame_rate, "quarter")

    def test_mouse_only_switch_saves_applies_when_supported_and_reflects_without_resaving(self):
        from unittest import mock

        self.assertFalse(self.page.mouse_only_switch.isChecked())
        self.page.mouse_only_switch.click()
        self.assertTrue(app_config.load_config(self.path).mouse_only)
        wayland = sys.platform.startswith("linux") and platform_parts.session(os.environ) == "wayland"
        self.assertEqual(self.page.sender._ignore_gate.mouse_only, not wayland)
        config = app_config.load_config(self.path)
        config.mouse_only = False
        with mock.patch.object(self.page, "_persist") as persist:
            self.page._reflect_config(config)
            self.assertFalse(self.page.mouse_only_switch.isChecked())
            persist.assert_not_called()

    def test_mouse_only_switch_restores_state_if_saving_fails(self):
        from unittest import mock

        with mock.patch.object(self.page, "_persist", return_value=False):
            self.page.mouse_only_switch.click()
        self.assertFalse(self.page._config.mouse_only)
        self.assertFalse(self.page.mouse_only_switch.isChecked())
        self.assertFalse(self.page.sender._ignore_gate.mouse_only)

    def test_clipboard_switch_saves_and_applies_in_both_directions(self):
        self.page.sender.update_config(self.page._config)
        self.assertTrue(self.page.clipboard_switch.isChecked())
        self.assertFalse(self.page.clipboard_switch.isHidden())
        self.page.clipboard_switch.click()
        self.assertFalse(app_config.load_config(self.path).share_clipboard)
        self.assertFalse(self.page.sender._owner.allows_clipboard())
        self.assertFalse(self.page.server._share_clipboard)
        self.assertFalse(self.page._config.mouse_only)
        self.page.clipboard_switch.click()
        self.assertTrue(self.page.sender._owner.allows_clipboard())
        self.assertTrue(self.page.server._share_clipboard)

    def test_fixture_releases_window_before_the_next_case(self):
        from shiboken6 import isValid

        page = self.page
        self.doCleanups()
        self.assertFalse(isValid(page))

    def test_incoming_layout_keeps_a_locally_held_paired_tile_when_missing(self):
        from core import protocol, settings_sync
        from core.tests import responder_harness as harness

        settings = app_config.load_settings(self.path)
        paired = protocol.id_text(bytes(range(110, 126)))
        own = settings["machine_id"]
        settings["peers"][0]["paired_with"] = [paired]
        settings["layout"][own] = {"x": 0, "y": 0, "w": 160, "h": 100}
        settings["layout"][self.peer] = {"x": 172, "y": 0, "w": 160, "h": 100}
        settings["layout"][paired] = {"x": 500, "y": 100, "w": 160, "h": 100, "name": "Test box"}
        app_config.write_settings(self.path, settings)
        self.page._config = app_config.load_config(self.path)
        source_id = protocol.read_id(self.peer)
        self.page._peer_caps = lambda: {source_id: {"settings", "layout"}}
        self.page._design_peer_caps = lambda: {self.peer: {"settings", "layout"}}
        incoming = {
            settings["machine_id"]: dict(settings["layout"][settings["machine_id"]]),
            self.peer: dict(settings["layout"][self.peer]),
        }
        data = settings_sync.message_data(False, 0, by=self.peer,
                    layout_state={"set_at": self.page._config.layout_set_at + 1, "by": self.peer, "tiles": incoming})

        self.page._on_settings(self.peer, data)

        self.assertEqual(app_config.load_settings(self.path)["layout"][paired], settings["layout"][paired])

    def test_each_gap_has_its_own_ledger_place_instruction(self):
        from core import protocol
        from core.tests import responder_harness as harness
        settings = app_config.load_settings(self.path)
        second = protocol.id_text(harness.C)
        settings["peers"].append(harness.entry(harness.C, "Other", side="right"))
        settings["layout"][second] = dict(x=500, y=100, w=160, h=100)
        app_config.write_settings(self.path, settings)
        self.page._pull_peer_fields()
        row = self.page.crossing_ledger.rows[second]
        self.assertIsNone(_contact(row))
        self.assertEqual(_corners(row), [])
        self.assertFalse(any(button.isEnabled() for button in row.corner_grid.buttons.values()))

    def test_the_native_titlebar_can_maximise_and_has_no_width_cap(self):
        from PySide6.QtCore import Qt

        self.assertTrue(self.page.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint)
        self.assertEqual(self.page.maximumWidth(), 16777215)

    def test_crossing_controls_and_diagram_stay_in_a_capped_column(self):
        from PySide6.QtWidgets import QApplication

        import tokens

        self.page.resize(2560, 1440)
        self.page.show()
        self.page._select_page("crossing")
        QApplication.processEvents()

        self.assertLessEqual(self.page.atlas_canvas.width(), tokens.PAGE_CONTENT_MAX)

    def test_ledger_jump_arms_its_hint_in_place(self):
        row = self.page.crossing_ledger.rows[self.peer]
        self.assertEqual(row.jump.key.text(), "Set a key")
        self.assertTrue(row.jump.hint.isHidden())
        row.jump._toggle()
        self.assertFalse(row.jump.hint.isHidden())

    def test_ledger_rows_stay_in_place_while_ways_change(self):
        self.ways()
        row = self.page.crossing_ledger.rows[self.peer]
        self.assertEqual(_picked(row), [])
        self.assertEqual(_corners(row), [])
        self.ways(["corner"])
        self.assertEqual(_corners(row), ["top_left"])
        self.assertIs(row, self.page.crossing_ledger.rows[self.peer])

    def test_the_canvas_replaces_the_side_and_thirds_choices(self):
        self.assertTrue(hasattr(self.page, "atlas_canvas"))
        self.assertFalse(hasattr(self.page, "edge_choice"))
        self.assertFalse(hasattr(self.page, "parts_choice"))

    def test_display_gesture_hint_is_below_the_canvas(self):
        from PySide6.QtWidgets import QApplication

        self.page.resize(820, 700)
        self.page.show()
        self.page._select_page("crossing")
        for _ in range(8):
            QApplication.processEvents()

        self.assertEqual(self.page.atlas_canvas_hint.text(), "Alt-click a display to split or group · drag to move · click a third of a lit edge to leave it out · drag a corner to resize")
        self.assertIs(self.page.atlas_canvas_hint.parentWidget(), self.page.atlas_canvas.parentWidget())
        self.assertGreaterEqual(self.page.atlas_canvas_hint.y(),
                                self.page.atlas_canvas.y() + self.page.atlas_canvas.height())

    def test_an_edge_learned_from_a_peer_updates_its_canvas_tile(self):
        page = self.page
        self.ways(["part"])
        old_y = page.atlas_canvas.tiles[self.peer]["y"]
        page._design_peer_caps = lambda: {self.peer: {"settings"}}
        page._on_arrangement(self.peer, "bottom", int(time.time()), self.peer)
        self.assertIn(self.peer, page.atlas_canvas.tiles)
        saved_layout = app_config.load_settings(self.path)["layout"]
        self.assertNotEqual(page.atlas_canvas.tiles[self.peer]["y"], old_y)
        self.assertEqual(page.atlas_canvas.tiles[self.peer]["y"], saved_layout[self.peer]["y"])

    def test_older_peer_status_requires_known_capabilities_and_uses_peer_version(self):
        from PySide6.QtWidgets import QApplication

        settings = app_config.load_settings(self.path)
        settings["peers"][0]["app"] = "Beamer 1.5.2"
        settings["peers"][0]["platform"] = "windows"
        app_config.write_settings(self.path, settings)
        page = self.page
        page._live = lambda _peer: False
        page._pull_peer_fields()
        page._design_peer_caps = lambda: {}
        page._reflect_ways()
        self.assertNotIn("only sees which side", page.crossing_ledger.rows[self.peer].status.text())

        page._design_peer_caps = lambda: {self.peer: frozenset({"settings"})}
        page._reflect_ways()
        self.assertEqual(page.crossing_ledger.rows[self.peer].status.text(), "Beamer 1.5.2 · only sees which side")
        self.assertIn("Windows · Offline", page.atlas_canvas.states[self.peer]["detail"])
        QApplication.processEvents()

    def test_inactive_peer_keeps_a_ledger_row_without_crossing_zones(self):
        settings = app_config.load_settings(self.path)
        settings["peers"][0]["in_use"] = False
        app_config.write_settings(self.path, settings)
        self.page._pull_peer_fields()
        self.assertIn(self.peer, self.page.crossing_ledger.rows)
        self.assertFalse(ways.has_way(app_config.load_settings(self.path), self.peer))
        self.assertFalse(hasattr(self.page, "selected_machine_header"))

    def test_left_out_ledger_row_names_the_peer_and_puts_it_back(self):
        self.page._canvas_left_out(self.peer, True)
        row = self.page.crossing_ledger.rows[self.peer]
        self.assertEqual(row.name.full_text, "Mac")
        self.assertEqual(row.status.text(), "macOS · Left out")
        self.assertEqual(row.leave.text(), "Put back")
        row.leave.click()
        self.assertFalse(app_config.load_settings(self.path)["peers"][0].get("left_out", False))

    def test_tile_selection_and_saved_corner_are_reflected_in_its_ledger_row(self):
        self.ways(["corner"])
        self.page._choose_machine(self.peer)
        row = self.page.crossing_ledger.rows[self.peer]
        self.assertTrue(row.selected)
        self.assertEqual(_corners(row), ["top_left"])

    def test_crossing_refresh_accepts_an_ordinary_edge_without_a_sentence(self):
        self.ways(["edge"])
        self.page._reflect_ways()
        self.assertEqual(self.page.atlas_canvas.states[self.peer]["state"], "edge")

    def test_text_capabilities_allow_the_shared_contact_state(self):
        from core import protocol
        from core.tests import responder_harness as harness

        settings = app_config.load_settings(self.path)
        second = protocol.id_text(harness.C)
        settings["peers"].append(harness.entry(harness.C, "Linux box", side="right"))
        settings["layout"] = {
            self.page._config.machine_id: {"x": 0, "y": 0, "w": 160, "h": 100},
            self.peer: {"x": 172, "y": 0, "w": 160, "h": 100},
            second: {"x": 332, "y": 0, "w": 160, "h": 100},
        }
        app_config.write_settings(self.path, settings)
        self.page._pull_peer_fields()
        self.page._design_peer_caps = lambda: {self.peer: frozenset({"layout"}),
                                               second: frozenset({"layout"})}
        self.page._reflect_ways()

        self.assertEqual(self.page.atlas_canvas.states[self.peer]["state"], "between")
        self.assertEqual(self.page.atlas_canvas.states[second]["state"], "between")

    def test_corner_can_be_enabled_when_it_is_currently_off(self):
        self.ways(["edge"])
        row = self.page.crossing_ledger.rows[self.peer]
        self.assertEqual(_corners(row), [])
        row.corner_grid.buttons["top_left"].click()
        self.assertEqual(_corners(row), ["top_left"])
        self.assertTrue(app_config.load_settings(self.path)["peers"][0]["ways"]["corner"])

    def test_layout_drop_keeps_the_compiled_side_and_its_stamp(self):
        layout = {self.page._config.machine_id: {"x": 0, "y": 0, "w": 160, "h": 100},
                  self.peer: {"x": 0, "y": -100, "w": 160, "h": 100}}
        self.page._layout_dropped(layout, self.peer)
        saved = app_config.load_settings(self.path)
        entry = next(peer for peer in saved["peers"] if peer["id"] == self.peer)
        self.assertEqual(entry["side"], "top")
        self.assertGreater(entry["side_set_at"], 0)

    def test_edge_off_clears_ledger_segments_and_map_crossing_marks(self):
        self.ways([])
        self.assertEqual(self.page.atlas_canvas.states[self.peer]["state"], "gap")
        row = self.page.crossing_ledger.rows[self.peer]
        self.assertFalse(_contact(row).isChecked())
        self.assertEqual(_picked(row), [])
        self.assertFalse(ways.has_way(app_config.load_settings(self.path), self.peer))

    def test_ledger_edge_text_toggles_the_canonical_zone(self):
        self.ways(["edge"])
        row = self.page.crossing_ledger.rows[self.peer]
        self.assertEqual(_picked(row), list(ways.PARTS))
        _contact(row).click()
        self.assertFalse(ways.has_way(app_config.load_settings(self.path), self.peer))
        _contact(row).click()
        self.assertTrue(ways.has_way(app_config.load_settings(self.path), self.peer))

    def test_ledger_edge_text_turns_parts_off_and_remembers_them(self):
        self.ways(["part"], parts=["start"])
        row = self.page.crossing_ledger.rows[self.peer]
        self.assertEqual(_picked(row), ["start"])
        _contact(row).click()
        self.assertFalse(ways.has_way(app_config.load_settings(self.path), self.peer))
        self.assertEqual(app_config.load_settings(self.path)["peers"][0]["ways"]["thirds"], ["start"])

    def test_gap_ledger_disables_edge_editing_and_never_moves_the_tile(self):
        self.ways([])
        settings = app_config.load_settings(self.path)
        settings["layout"][self.peer] = dict(x=520, y=240, w=160, h=100)
        app_config.write_settings(self.path, settings)
        self.page._pull_peer_fields()
        row = self.page.crossing_ledger.rows[self.peer]
        self.assertIsNone(_contact(row))
        self.assertFalse(any(button.isEnabled() for button in row.corner_grid.buttons.values()))
        current = app_config.load_settings(self.path)
        self.assertEqual(current["layout"][self.peer], settings["layout"][self.peer])
        self.assertFalse(ways.has_way(current, self.peer))

    def test_ledger_and_map_light_the_picked_thirds(self):
        self.ways(["part"], parts=["start"])
        row = self.page.crossing_ledger.rows[self.peer]
        self.assertEqual(_picked(row), ["start"])
        segments = _contact(row).segments
        self.assertTrue(segments["start"].isChecked())
        self.assertFalse(segments["end"].isChecked())
        self.assertEqual(self.page.atlas_canvas.states[self.peer]["thirds"], ["start"])

    def test_overlapping_drop_persists_an_accepted_free_snap(self):
        from core import protocol
        from core.tests import responder_harness as harness
        settings = app_config.load_settings(self.path)
        second = protocol.id_text(harness.C)
        settings["peers"].append(harness.entry(harness.C, "Linux box", side="right"))
        app_config.write_settings(self.path, settings)
        self.page._pull_peer_fields()
        layout = {self.page._config.machine_id: dict(x=0, y=0, w=160, h=100),
                  self.peer: dict(x=160, y=0, w=160, h=100), second: dict(x=160, y=0, w=160, h=100)}
        self.assertTrue(self.page._layout_dropped(layout, second))
        saved = app_config.load_settings(self.path)["layout"]
        self.assertFalse(ways._overlaps(saved[self.peer], saved[second]))
        self.assertEqual(self.page.atlas_canvas.tiles[second], saved[second])

    def test_overlap_without_a_free_snap_restores_the_saved_picture(self):
        from unittest import mock
        from core import ways

        own = self.page._config.machine_id
        saved = app_config.load_settings(self.path)
        overlap = {"x": 0, "y": 0, "w": 160, "h": 100}
        layout = {own: dict(overlap), self.peer: dict(overlap)}
        with mock.patch.object(ways, "snap_position", return_value=dict(overlap)), \
                mock.patch.object(self.page.atlas_canvas, "animate_tile") as animate:
            self.assertFalse(self.page._layout_dropped(layout, self.peer))
        animate.assert_not_called()
        self.assertEqual(app_config.load_settings(self.path), saved)
        self.assertIn("cannot connect", self.page.atlas_canvas.accessibleDescription())

    def test_shelved_position_does_not_block_a_drop_and_is_remembered(self):
        from core import protocol
        from core.tests import responder_harness as harness

        settings = app_config.load_settings(self.path)
        shelf = protocol.id_text(harness.C)
        settings["peers"].append(harness.entry(harness.C, "Shelf", side="right", left_out=True))
        tile = {"x": 160, "y": 0, "w": 160, "h": 100}
        settings["layout"] = {self.page._config.machine_id: {"x": 0, "y": 0, "w": 160, "h": 100},
                              self.peer: {"x": -160, "y": 0, "w": 160, "h": 100}, shelf: tile}
        app_config.write_settings(self.path, settings)
        self.page._pull_peer_fields()
        layout = {key: dict(value) for key, value in settings["layout"].items()}
        layout[self.peer] = tile
        self.assertTrue(self.page._layout_dropped(layout, self.peer))
        saved = app_config.load_settings(self.path)
        self.assertEqual(saved["layout"][shelf], tile)
        self.assertEqual(saved["layout"][self.peer], tile)
        self.assertEqual(self.page.atlas_canvas.states[self.peer]["state"], "edge")

    def test_shared_layout_accepts_shelved_overlap_and_named_paired_third_party(self):
        from core import protocol, settings_sync
        from core.tests import responder_harness as harness

        settings = app_config.load_settings(self.path)
        shelf = protocol.id_text(harness.C)
        foreign = protocol.id_text(bytes(range(110, 126)))
        next(entry for entry in settings["peers"] if entry["id"] == self.peer)["paired_with"] = [foreign]
        settings["peers"].append(harness.entry(harness.C, "Shelf", side="right", left_out=True))
        app_config.write_settings(self.path, settings)
        self.page._pull_peer_fields()
        self.page._peer_caps = lambda: {harness.B: {"layout"}}
        tile = {"x": 160, "y": 0, "w": 160, "h": 100}
        layout = {self.page._config.machine_id: {"x": 0, "y": 0, "w": 160, "h": 100},
                  self.peer: tile, shelf: tile,
                  foreign: {"x": 320, "y": 0, "w": 160, "h": 100, "name": "Test box"}}
        layout_stamp = int(time.time()) + 100
        data = settings_sync.message_data(False, 100, {}, by=self.peer,
                    layout_state={"set_at": layout_stamp, "by": self.peer, "tiles": layout})
        self.page._on_settings(self.peer, data)
        saved = app_config.load_settings(self.path)
        self.assertEqual(set(saved["layout"]), set(layout))
        for peer, tile in layout.items():
            expected = {key: tile[key] for key in ("x", "y", "w", "h")}
            self.assertEqual({key: saved["layout"][peer][key] for key in expected}, expected)
        self.assertNotIn("vouched_by", saved["layout"][foreign])
        self.assertGreaterEqual(saved["layout_set_at"], layout_stamp)

    def test_shared_layout_gap_tells_an_older_peer_its_way_back_changed(self):
        from core import protocol, settings_sync
        from core.tests import responder_harness as harness

        settings = app_config.load_settings(self.path)
        source = protocol.id_text(harness.C)
        settings["peers"][0].update(side_set_at=100, side_by=self.page._config.machine_id)
        settings["peers"].append(harness.entry(harness.C, "Source", side="left"))
        app_config.write_settings(self.path, settings)
        self.ways(["edge"])
        self.page._peer_caps = lambda: {harness.C: {"layout"}, harness.B: {"settings"}}
        sent = []
        self.page._send_to = lambda peer, message: sent.append((peer, message)) or True
        layout = {self.page._config.machine_id: {"x": 0, "y": 0, "w": 160, "h": 100},
                  self.peer: {"x": 200, "y": 0, "w": 160, "h": 100},
                  source: {"x": -200, "y": 0, "w": 160, "h": 100}}
        data = settings_sync.message_data(False, 100, {}, by=source,
                    layout_state={"set_at": app_config.load_settings(self.path).get("layout_set_at", 0) + 1,
                                  "by": source, "tiles": layout})
        self.page._on_settings(source, data)
        arrangements = [message["data"] for peer, message in sent
                        if peer == harness.B and message["type"] == protocol.MSG_ARRANGEMENT]
        self.assertEqual(len(arrangements), 1)
        self.assertFalse(arrangements[0]["way_back"])

    def test_shared_picture_keeps_a_named_machine_listed_by_the_source_after_a_drop(self):
        from core import protocol, settings_sync
        from core.tests import responder_harness as harness

        foreign = protocol.id_text(harness.C)
        settings = app_config.load_settings(self.path)
        next(entry for entry in settings["peers"] if entry["id"] == self.peer)["paired_with"] = [foreign]
        app_config.write_settings(self.path, settings)
        self.page._peer_caps = lambda: {harness.B: {"layout"}}
        layout = {self.page._config.machine_id: {"x": 0, "y": 0, "w": 160, "h": 100},
                  self.peer: {"x": 160, "y": 0, "w": 160, "h": 100},
                  foreign: {"x": 320, "y": 0, "w": 160, "h": 100, "name": "Test box"}}
        data = settings_sync.message_data(False, 100, {}, by=self.peer,
                    layout_state={"set_at": settings.get("layout_set_at", 0) + 1,
                                  "by": self.peer, "tiles": layout})
        self.page._on_settings(self.peer, data)
        named_foreign = layout[foreign]
        self.assertEqual(self.page.atlas_canvas.tiles[foreign], named_foreign)
        self.assertEqual(self.page.atlas_canvas.labels[foreign], "Test box")
        dropped = {key: dict(tile) for key, tile in self.page.atlas_canvas.tiles.items()}
        dropped[self.peer]["x"] = 160
        self.assertTrue(self.page._layout_dropped(dropped, self.peer))
        self.assertEqual(app_config.load_settings(self.path)["layout"][foreign], named_foreign)

    def test_crossing_map_drops_the_rc7_ghost_tile_reproduction(self):
        from core import protocol, settings_sync
        from core.tests import responder_harness as harness

        settings = app_config.load_settings(self.path)
        own = settings["machine_id"]
        pc = self.peer
        laptop = protocol.id_text(harness.C)
        phone, box, old_pc = (protocol.id_text(bytes([value]) * 16) for value in (11, 12, 13))
        next(entry for entry in settings["peers"] if entry["id"] == pc).update(
            left_out=True, paired_with=[phone, box])
        settings["peers"].append(harness.entry(harness.C, "Laptop", paired_with=[old_pc]))
        settings["peers"].append(harness.entry(phone, "Phone", platform="ios", port=0, token="t" * 43))
        settings["layout"].update({
            laptop: {"x": 172, "y": 0, "w": 160, "h": 100},
            phone: {"x": 344, "y": 0, "w": 160, "h": 100, "vouched_by": pc},
            box: {"x": 516, "y": 0, "w": 160, "h": 100, "vouched_by": pc},
            old_pc: {"x": 688, "y": 0, "w": 160, "h": 100, "vouched_by": laptop},
        })
        app_config.write_settings(self.path, settings)
        self.page._pull_peer_fields()
        source_id = protocol.read_id(laptop)
        self.page._peer_caps = lambda: {source_id: {"settings", "layout"}}
        self.page._design_peer_caps = lambda: {laptop: {"settings", "layout"}}
        incoming = dict(settings["layout"])
        data = settings_sync.message_data(False, 100, {}, by=laptop,
                    layout_state={"set_at": settings.get("layout_set_at", 0) + 1,
                                  "by": laptop, "tiles": incoming})

        self.page._on_settings(laptop, data)
        self.page._reflect_ways()

        self.assertTrue({phone, box, old_pc}.isdisjoint(self.page.atlas_canvas.tiles))

    def test_drop_to_a_gap_tells_the_peer_its_way_back_changed(self):
        from core import protocol

        settings = app_config.load_settings(self.path)
        settings["peers"][0].update(side_set_at=100, side_by=self.page._config.machine_id)
        app_config.write_settings(self.path, settings)
        self.ways(["edge"])
        sent = []
        self.page._send_to = lambda peer, message: sent.append(message) or True
        layout = {self.page._config.machine_id: {"x": 0, "y": 0, "w": 160, "h": 100},
                  self.peer: {"x": 200, "y": 0, "w": 160, "h": 100}}
        self.page._layout_dropped(layout, self.peer)
        arrangements = [message["data"] for message in sent if message["type"] == protocol.MSG_ARRANGEMENT]
        self.assertEqual(len(arrangements), 1)
        self.assertFalse(arrangements[0]["way_back"])

    def test_snap_radius_is_twelve_display_pixels(self):
        self.page.atlas_canvas._scale = 0.5
        layout = {self.page._config.machine_id: {"x": 0, "y": 0, "w": 160, "h": 100},
                  self.peer: {"x": 180, "y": 17, "w": 160, "h": 100}}
        self.page._layout_dropped(layout, self.peer)
        self.assertEqual(app_config.load_settings(self.path)["layout"][self.peer]["x"], 160)

    def test_own_tile_rename_hint_is_visible(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        canvas = self.page.atlas_canvas
        canvas.resize(720, 260)
        own = canvas.tile_rect(self.page._config.machine_id).center().toPoint()
        QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=own)
        self.assertIn("Overview", canvas.accessibleDescription())

    def test_rename_does_not_save_a_duplicate_names_display_suffix(self):
        from core.tests import responder_harness as harness

        settings = app_config.load_settings(self.path)
        settings["peers"][0]["label"] = "Studio"
        settings["peers"].append(harness.entry(harness.C, "Other", label="Studio", side="left"))
        app_config.write_settings(self.path, settings)
        self.page._pull_peer_fields()
        self.page.atlas_canvas.begin_rename(self.peer)
        self.assertEqual(self.page.atlas_canvas.rename_edit.text(), "Studio")

    def test_left_out_ledger_keys_tick_can_leave_the_shortcut_cycle(self):
        self.page._canvas_left_out(self.peer, True)
        self.page._set_shortcut_goes_to("ticked")
        row = self.page.crossing_ledger.rows[self.peer]
        row.cycle.setChecked(False)
        self.assertFalse(app_config.load_settings(self.path)["peers"][0]["shortcut_cycle"])

    def test_machine_row_cycle_tick_saves_and_updates_the_tile(self):
        self.page._set_shortcut_goes_to("ticked")
        row = next(iter(self.page.machines.rows.values()))
        self.assertFalse(row.shortcut_cycle.isHidden())
        row.shortcut_cycle.setChecked(False)
        self.assertFalse(app_config.load_settings(self.path)["peers"][0]["shortcut_cycle"])
        self.assertFalse(self.page.atlas_canvas.cycle[self.peer])

    def test_dormant_map_tile_keeps_its_offline_ledger_row(self):
        self.page._gone = lambda peer: peer == self.peer
        self.page._reflect_ways()
        self.assertIn(self.peer, self.page.atlas_canvas.shelf_ids)
        row = self.page.crossing_ledger.rows[self.peer]
        self.assertIn("Offline, ways kept", row.status.text())
        self.assertFalse(hasattr(self.page, "left_out_shelf"))

    def test_layout_drop_refusal_persists_the_animated_free_position(self):
        from core import protocol, ways
        from core.tests import responder_harness as harness

        settings = app_config.load_settings(self.path)
        second = protocol.id_text(harness.C)
        settings["peers"].append(harness.entry(harness.C, "Linux box", side="right"))
        app_config.write_settings(self.path, settings)
        self.page._pull_peer_fields()
        layout = {self.page._config.machine_id: {"x": 0, "y": 0, "w": 160, "h": 100},
                  self.peer: {"x": 172, "y": 0, "w": 160, "h": 100},
                  second: {"x": 172, "y": 0, "w": 160, "h": 100}}
        self.page._layout_dropped(layout, second)
        saved = app_config.load_settings(self.path)["layout"]
        self.assertFalse(ways._overlaps(saved[self.peer], saved[second]))

    def test_the_crossing_line_gives_the_first_reason_nothing_can_cross(self):
        line = pages_win.crossing_state_sentence
        self.assertTrue(line(False, False, False, False, True, False, None).startswith("Not paired yet"))
        self.assertTrue(line(True, False, True, False, True, False, None).startswith("Waiting to hear from the other machine"))
        self.assertTrue(line(True, True, False, False, True, False, None).startswith("This PC does not drive the other machine"))
        self.assertTrue(line(True, True, True, False, True, False, None).startswith("Not connected to the other machine"))
        self.assertTrue(line(True, True, True, True, False, False, None).startswith("Only the shortcut"))
        self.assertTrue(line(True, True, True, True, True, True, None).startswith("Pauses pointer crossing"))
        self.assertEqual(line(True, True, True, True, True, False, "Keynote"),
                         "Held: an app is full screen.")
        self.assertEqual(line(True, True, True, True, True, False, None), "On. Pause it to lean on an edge without switching.")

    def test_the_crossing_line_names_the_machine_it_is_given(self):
        line = pages_win.crossing_state_sentence
        self.assertTrue(line(True, True, True, False, True, False, None, "Studio Mac").startswith("Not connected to Studio Mac"))
        self.assertIn("Studio Mac", line(True, False, True, False, True, False, None, "Studio Mac"))
        self.assertIn("Studio Mac", line(True, True, False, False, True, False, None, "Studio Mac"))
        self.assertIn("pair a machine", line(False, False, False, False, True, False, None, "Studio Mac"))

    def test_a_reason_shows_even_when_only_the_shortcut_is_on(self):
        self.assertTrue(pages_win.crossing_state_blocked(False, True, True, True))
        self.assertTrue(pages_win.crossing_state_blocked(True, False, True, True))
        self.assertTrue(pages_win.crossing_state_blocked(True, True, False, True))
        self.assertTrue(pages_win.crossing_state_blocked(True, True, True, False))
        self.assertFalse(pages_win.crossing_state_blocked(True, True, True, True))

    def test_no_learned_edge_is_not_described_as_the_right_edge(self):
        summary = pages_win.ways_summary(["edge"], "", ["middle"], "top_right", "Right Ctrl", "double_tap")
        self.assertNotIn("right edge", summary)
        self.assertTrue(summary.startswith("Nothing moves input to the other machine until"))
        self.assertIn("the whole right edge", pages_win.ways_summary(["edge"], "right", ["middle"], "top_right", "Right Ctrl", "double_tap"))



class RedactTests(unittest.TestCase):
    def test_hides_ip_and_hardware_addresses_only_when_asked(self):
        line = "MacBook Pro at 192.168.1.10:24820, woken by 02-1A-2B-3C-0D-4E"
        self.assertEqual(pages_win.redact(line, False), line)
        self.assertEqual(pages_win.redact(line, True), "MacBook Pro at •••:24820, woken by •••")
        self.assertEqual(pages_win.redact("Ready on TCP port 24820", True), "Ready on TCP port 24820")


if __name__ == "__main__":
    unittest.main()
