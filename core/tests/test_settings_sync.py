"""Same on both machines, without AppKit or Qt: the message, the merge and the window's words."""

import unittest
from types import SimpleNamespace

from core import settings_sync

KEYS = {"alt_r", "cmd_r", "f13"}


def mac_raw(**crossing):
    base = {
        "methods": ["shortcut", "edge", "notch"], "edge": "right", "edge_parts": ["middle"],
        "corner": "top_right", "resistance_px": 120, "block_while_dragging": True, "glow": True,
        "glow_style": "glow", "glow_colour": "signal", "effect_length": "normal", "shortcut_arrival": True,
        "shortcut_arrival_style": "match", "notch_style": "beam", "notch_after_ms": 1200, "haptics": True,
        "hold_full_screen": False,
    }
    base.update(crossing)
    return {"trigger_key": "alt_r", "trigger_style": "double_tap", "double_tap_ms": 300,
            "appearance": "dark", "crossing": base}


def pc_config(**fields):
    base = dict(
        crossing_methods=["edge", "shortcut"], crossing_edge_parts=["middle"], crossing_corner="top_left",
        crossing_resistance_px=120, block_while_dragging=True, trigger_key="cmd_r", trigger_style="double_tap",
        double_tap_ms=300, glow_style="glow", glow_colour="signal", effect_length="normal",
        shortcut_arrival=True, shortcut_arrival_style="match", mac_return_edge="left", edge_glow=True,
        appearance="light",
    )
    base.update(fields)
    return SimpleNamespace(**base)


class CornerTests(unittest.TestCase):
    def test_a_corner_flips_across_the_border_and_keeps_its_place_along_it(self):
        self.assertEqual(settings_sync.mirror_corner("top_right", "right"), "top_left")
        self.assertEqual(settings_sync.mirror_corner("bottom_left", "left"), "bottom_right")
        self.assertEqual(settings_sync.mirror_corner("top_right", "top"), "bottom_right")

    def test_mirroring_is_its_own_inverse(self):
        for corner in settings_sync.CORNERS:
            for edge in ("left", "right", "top", "bottom"):
                self.assertEqual(settings_sync.mirror_corner(settings_sync.mirror_corner(corner, edge), edge), corner)


class MessageTests(unittest.TestCase):
    def test_off_carries_no_values(self):
        data = settings_sync.message_data(False, 50, settings_sync.mac_values(mac_raw()))
        self.assertEqual(data, {"on": False, "set_at": 50})

    def test_on_carries_crossing_and_design_and_never_the_notch_or_this_machines_own(self):
        data = settings_sync.message_data(True, 50, settings_sync.mac_values(mac_raw()))
        self.assertEqual(set(data["crossing"]), set(settings_sync.CROSSING_KEYS))
        self.assertEqual(set(data["design"]), set(settings_sync.DESIGN_KEYS))
        self.assertEqual(data["crossing"]["methods"], ["shortcut", "edge"])
        flat = {**data["crossing"], **data["design"]}
        for own in ("notch_style", "notch_after_ms", "haptics", "glow", "appearance", "edge", "hold_full_screen"):
            self.assertNotIn(own, flat)

    def test_a_round_trip_reads_back_what_was_sent(self):
        values = settings_sync.mac_values(mac_raw())
        on, set_at, read = settings_sync.read(settings_sync.message_data(True, 7, values), KEYS)
        self.assertEqual((on, set_at), (True, 7))
        self.assertEqual(read, values)

    def test_a_value_this_end_cannot_hold_is_left_out_and_the_rest_kept(self):
        data = settings_sync.message_data(True, 7, settings_sync.mac_values(mac_raw()))
        data["crossing"]["trigger_key"] = "hyper"
        data["crossing"]["resistance_px"] = 9000
        data["design"]["glow_style"] = "from-the-future"
        data["crossing"]["methods"] = ["edge", "notch"]
        _on, _set_at, values = settings_sync.read(data, KEYS)
        for dropped in ("trigger_key", "resistance_px", "glow_style", "methods"):
            self.assertNotIn(dropped, values)
        self.assertEqual(values["corner"], "top_right")

    def test_malformed_data_is_not_a_message(self):
        for data in (None, [], {}, {"on": "yes", "set_at": 1}, {"on": True}, {"on": True, "set_at": True}):
            self.assertIsNone(settings_sync.read(data, KEYS))

    def test_a_change_here_always_stamps_newer_than_what_is_held(self):
        self.assertEqual(settings_sync.next_stamp(0, 1000.7), 1000)
        self.assertEqual(settings_sync.next_stamp(1000, 1000.2), 1001)
        # A peer's clock ahead of this one's: the change made here still wins.
        self.assertEqual(settings_sync.next_stamp(5000, 1000), 5001)

    def test_a_tie_goes_to_the_mac_and_only_when_it_differs(self):
        mac = settings_sync.message_data(True, 100, settings_sync.mac_values(mac_raw(glow_style="beam")))
        pc_same = (True, settings_sync.mac_values(mac_raw(glow_style="beam")))
        pc_other = (True, settings_sync.mac_values(mac_raw(glow_style="glow")))
        self.assertIsNone(settings_sync.arrived(mac, 100, KEYS))
        self.assertIsNone(settings_sync.arrived(mac, 100, KEYS, pc_same))
        self.assertEqual(settings_sync.arrived(mac, 100, KEYS, pc_other)[2]["glow_style"], "beam")
        self.assertIsNone(settings_sync.arrived(settings_sync.message_data(False, 0), 0, KEYS, (True, {})))

    def test_only_a_newer_stamp_wins(self):
        self.assertTrue(settings_sync.wins(2, 1))
        self.assertFalse(settings_sync.wins(1, 1))
        self.assertFalse(settings_sync.wins(0, 0))


class AdapterTests(unittest.TestCase):
    def test_the_mac_keeps_its_notch_and_its_own_rows(self):
        raw = mac_raw()
        values = settings_sync.mac_values(mac_raw(methods=["corner"], glow_style="beam", resistance_px=40))
        values["trigger_key"] = "f13"
        applied = settings_sync.apply_mac(raw, values)
        self.assertEqual(applied["crossing"]["methods"], ["corner", "notch"])
        self.assertEqual((applied["crossing"]["glow_style"], applied["crossing"]["resistance_px"]), ("beam", 40))
        self.assertEqual(applied["trigger_key"], "f13")
        for own in ("notch_style", "notch_after_ms", "haptics", "glow", "edge"):
            self.assertEqual(applied["crossing"][own], raw["crossing"][own])
        self.assertEqual(applied["appearance"], "dark")
        self.assertEqual(raw["crossing"]["methods"], ["shortcut", "edge", "notch"])

    def test_a_mac_without_the_notch_gains_none(self):
        applied = settings_sync.apply_mac(mac_raw(methods=["edge"]), {"methods": ["corner"]})
        self.assertEqual(applied["crossing"]["methods"], ["corner"])

    def test_the_pc_sends_its_corner_in_the_macs_frame_and_takes_one_back_in_its_own(self):
        config = pc_config(crossing_corner="top_left", mac_return_edge="left")
        self.assertEqual(settings_sync.pc_values(config)["corner"], "top_right")
        settings_sync.apply_pc(config, {"corner": "bottom_right"})
        self.assertEqual(config.crossing_corner, "bottom_left")

    def test_values_from_the_mac_land_on_the_pcs_fields_and_leave_its_own(self):
        config = pc_config()
        values = settings_sync.mac_values(mac_raw(glow_colour="ocean", edge_parts=["start", "end"]))
        settings_sync.apply_pc(config, values)
        self.assertEqual(config.crossing_methods, ["shortcut", "edge"])
        self.assertEqual(config.crossing_edge_parts, ["start", "end"])
        self.assertEqual((config.glow_colour, config.trigger_key), ("ocean", "alt_r"))
        self.assertEqual((config.edge_glow, config.appearance, config.mac_return_edge), (True, "light", "left"))

    def test_a_change_is_seen_only_in_a_shared_value(self):
        before = settings_sync.mac_values(mac_raw())
        self.assertFalse(settings_sync.changed(before, settings_sync.mac_values(mac_raw(notch_style="island", glow=False))))
        self.assertTrue(settings_sync.changed(before, settings_sync.mac_values(mac_raw(effect_length="long"))))


class WordsTests(unittest.TestCase):
    def test_a_shared_page_says_so_only_while_on(self):
        own = "For this Mac only; the PC keeps its own."
        self.assertEqual(settings_sync.scope("crossing", "PC", False, own), own)
        self.assertEqual(settings_sync.scope("design", "PC", True, own),
                         "Kept the same as your PC. Change it on either machine.")
        self.assertEqual(settings_sync.scope("keyboard", "Mac", True, own), own)

    def test_the_switch_note_names_both_pages_and_an_old_peer(self):
        self.assertIn("Crossing and Design", settings_sync.switch_note("Mac", True, False))
        self.assertIn("Crossing and Design", settings_sync.switch_note("Mac", False, False))
        self.assertIn("too old", settings_sync.switch_note("PC", True, True))


if __name__ == "__main__":
    unittest.main()
