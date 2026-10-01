"""Same on both machines, without AppKit or Qt: the message, the merge and the window's words."""

import base64
import unittest
from types import SimpleNamespace

from core import settings_sync

KEYS = {"alt_r", "cmd_r", "f13"}
NOW = 1790000000


def b64(raw):
    return base64.b64encode(raw).decode()


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


class MessageTests(unittest.TestCase):
    def test_off_carries_no_values(self):
        data = settings_sync.message_data(False, 50, settings_sync.mac_values(mac_raw()))
        self.assertEqual(data, {"on": False, "set_at": 50, "by": ""})

    def test_on_carries_crossing_and_design_and_never_the_notch_or_this_machines_own(self):
        data = settings_sync.message_data(True, 50, settings_sync.mac_values(mac_raw()))
        self.assertEqual(set(data["crossing"]), set(settings_sync.CROSSING_KEYS))
        self.assertEqual(set(data["design"]), set(settings_sync.DESIGN_KEYS))
        self.assertIs(data["crossing"]["shortcut"], True)
        flat = {**data["crossing"], **data["design"]}
        for own in ("notch_style", "notch_after_ms", "haptics", "glow", "appearance", "edge", "hold_full_screen",
                    "methods", "edge_parts", "corner"):
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
        data["crossing"]["shortcut"] = "yes"
        data["crossing"]["methods"] = ["edge"]
        _on, _set_at, values = settings_sync.read(data, KEYS)
        for dropped in ("trigger_key", "resistance_px", "glow_style", "shortcut", "methods"):
            self.assertNotIn(dropped, values)
        self.assertEqual(values["trigger_style"], "double_tap")

    def test_a_key_that_cannot_be_the_trigger_is_left_out(self):
        for name in ("browser_back", "browser_forward", "backspace", "tab", "enter", "esc", "space", "print_screen"):
            data = settings_sync.message_data(True, 7, settings_sync.mac_values(mac_raw()))
            data["crossing"]["trigger_key"] = name
            _on, _set_at, values = settings_sync.read(data, KEYS | {name})
            self.assertNotIn("trigger_key", values, name)

    def test_the_message_names_who_made_the_change(self):
        self.assertEqual(settings_sync.message_data(False, 5, by="QUJD"), {"on": False, "set_at": 5, "by": "QUJD"})
        self.assertEqual(settings_sync.message_data(False, 5), {"on": False, "set_at": 5, "by": ""})

    def test_malformed_data_is_not_a_message(self):
        for data in (None, [], {}, {"on": "yes", "set_at": 1}, {"on": True}, {"on": True, "set_at": True}):
            self.assertIsNone(settings_sync.read(data, KEYS))

    def test_a_change_here_always_stamps_newer_than_what_is_held(self):
        self.assertEqual(settings_sync.next_stamp(0, 1000.7), 1000)
        self.assertEqual(settings_sync.next_stamp(1000, 1000.2), 1001)
        # A peer's clock ahead of this one's: the change made here still wins.
        self.assertEqual(settings_sync.next_stamp(5000, 1000), 5001)

    def test_a_tie_goes_to_the_larger_id_as_bytes_and_a_lower_one_changes_nothing(self):
        values = settings_sync.mac_values(mac_raw(glow_style="beam"))
        big, small = b64(b"\x02" * 16), b64(b"\x01" * 16)
        theirs = settings_sync.message_data(True, 100, values, by=big)
        self.assertIsNotNone(settings_sync.arrived(theirs, 100, KEYS, by_here=small, now=NOW))
        self.assertIsNone(settings_sync.arrived(theirs, 100, KEYS, by_here=big, now=NOW))
        lower = settings_sync.message_data(True, 100, values, by=small)
        self.assertIsNone(settings_sync.arrived(lower, 100, KEYS, by_here=big, now=NOW))

    def test_compared_as_bytes_not_as_text(self):
        # As text "AAAA..." sorts above "////...", but as bytes 0x00.. is below 0xff..
        low, high = b64(b"\x00" * 16), b64(b"\xff" * 16)
        self.assertGreater(low, high)
        theirs = settings_sync.message_data(False, 9, by=high)
        self.assertIsNotNone(settings_sync.arrived(theirs, 9, KEYS, by_here=low, now=NOW))
        self.assertIsNone(settings_sync.arrived(settings_sync.message_data(False, 9, by=low), 9, KEYS, by_here=high, now=NOW))

    def test_a_newer_stamp_wins_whatever_the_ids(self):
        theirs = settings_sync.message_data(False, 101, by=b64(b"\x00" * 16))
        self.assertEqual(settings_sync.arrived(theirs, 100, KEYS, by_here=b64(b"\xff" * 16), now=NOW)[:2], (False, 101))

    def test_a_bad_by_is_not_a_message_and_a_missing_one_is_empty(self):
        for by in (5, "not base64!", None, True):
            data = {"on": False, "set_at": 9, "by": by}
            self.assertIsNone(settings_sync.arrived(data, 0, KEYS, now=NOW), by)
        self.assertIsNotNone(settings_sync.arrived({"on": False, "set_at": 9}, 0, KEYS, now=NOW))

    def test_a_stamp_that_is_not_an_integer_is_not_a_message(self):
        for set_at in (9.5, 9.0, "9", None, [9]):
            data = {"on": False, "set_at": set_at, "by": b64(b"\x01" * 16)}
            self.assertIsNone(settings_sync.read(data, KEYS), set_at)
            self.assertIsNone(settings_sync.arrived(data, 0, KEYS, now=NOW), set_at)

    def test_a_by_that_is_not_a_machine_id_in_length_is_not_a_message(self):
        for raw in (b"", b"\x01", b"\x01" * 15, b"\x01" * 17, b"\x01" * 64):
            data = {"on": False, "set_at": 9, "by": b64(raw)}
            if raw:
                self.assertIsNone(settings_sync.arrived(data, 0, KEYS, now=NOW), raw)

    def test_a_stamp_more_than_a_day_ahead_or_past_2_to_the_53_is_ignored(self):
        self.assertIsNotNone(settings_sync.arrived(settings_sync.message_data(False, NOW + 86400), 0, KEYS, now=NOW))
        self.assertIsNone(settings_sync.arrived(settings_sync.message_data(False, NOW + 86401), 0, KEYS, now=NOW))
        self.assertIsNone(settings_sync.arrived(settings_sync.message_data(False, 2 ** 53 - 1), 0, KEYS, now=2 ** 54))
        self.assertIsNotNone(settings_sync.arrived(settings_sync.message_data(False, 2 ** 53 - 2), 0, KEYS, now=2 ** 54))

    def test_a_local_change_after_a_far_ahead_stamp_still_stamps_held_plus_one(self):
        self.assertEqual(settings_sync.next_stamp(NOW + 5000, NOW), NOW + 5001)


class AdapterTests(unittest.TestCase):
    def test_the_mac_keeps_its_notch_its_zones_and_its_own_rows(self):
        raw = mac_raw()
        values = settings_sync.mac_values(mac_raw(glow_style="beam", resistance_px=40))
        values["trigger_key"] = "f13"
        values["shortcut"] = False
        applied = settings_sync.apply_mac(raw, values)
        self.assertEqual(applied["crossing"]["methods"], ["edge", "notch"])
        self.assertEqual((applied["crossing"]["glow_style"], applied["crossing"]["resistance_px"]), ("beam", 40))
        self.assertEqual(applied["trigger_key"], "f13")
        for own in ("notch_style", "notch_after_ms", "haptics", "glow", "edge", "edge_parts", "corner"):
            self.assertEqual(applied["crossing"][own], raw["crossing"][own])
        self.assertEqual(applied["appearance"], "dark")
        self.assertEqual(raw["crossing"]["methods"], ["shortcut", "edge", "notch"])

    def test_the_shortcut_is_added_to_the_ways_when_the_peer_has_it_on(self):
        applied = settings_sync.apply_mac(mac_raw(methods=["corner"]), {"shortcut": True})
        self.assertEqual(applied["crossing"]["methods"], ["corner", "shortcut"])
        self.assertEqual(settings_sync.apply_mac(mac_raw(methods=["shortcut"]), {"shortcut": True})["crossing"]["methods"], ["shortcut"])

    def test_the_shortcut_is_read_from_the_ways(self):
        self.assertIs(settings_sync.mac_values(mac_raw(methods=["edge"]))["shortcut"], False)
        self.assertIs(settings_sync.pc_values(pc_config(crossing_methods=["edge", "shortcut"]))["shortcut"], True)

    def test_a_values_dict_has_no_zones(self):
        for values in (settings_sync.mac_values(mac_raw()), settings_sync.pc_values(pc_config())):
            for zone in ("methods", "edge_parts", "corner"):
                self.assertNotIn(zone, values)

    def test_values_from_the_mac_land_on_the_pcs_fields_and_leave_its_own_and_its_zones(self):
        config = pc_config()
        values = settings_sync.mac_values(mac_raw(glow_colour="ocean", methods=["edge"]))
        settings_sync.apply_pc(config, values)
        self.assertEqual(config.crossing_methods, ["edge"])
        self.assertEqual((config.crossing_edge_parts, config.crossing_corner), (["middle"], "top_left"))
        self.assertEqual((config.glow_colour, config.trigger_key), ("ocean", "alt_r"))
        self.assertEqual((config.edge_glow, config.appearance, config.mac_return_edge), (True, "light", "left"))

    def test_the_pc_turns_its_shortcut_on_and_keeps_the_other_ways(self):
        config = pc_config(crossing_methods=["edge"])
        settings_sync.apply_pc(config, {"shortcut": True})
        self.assertEqual(config.crossing_methods, ["edge", "shortcut"])
        settings_sync.apply_pc(config, {"shortcut": False})
        self.assertEqual(config.crossing_methods, ["edge"])

    def test_a_change_is_seen_only_in_a_shared_value(self):
        before = settings_sync.mac_values(mac_raw())
        self.assertFalse(settings_sync.changed(before, settings_sync.mac_values(mac_raw(notch_style="island", glow=False, corner="bottom_left", edge_parts=["end"]))))
        self.assertTrue(settings_sync.changed(before, settings_sync.mac_values(mac_raw(effect_length="long"))))
        self.assertTrue(settings_sync.changed(before, settings_sync.mac_values(mac_raw(methods=["edge"]))))

    def test_hold_full_screen_is_never_shared(self):
        for values in (settings_sync.mac_values(mac_raw(hold_full_screen=True)), settings_sync.pc_values(pc_config())):
            self.assertNotIn("hold_full_screen", values)
        data = settings_sync.message_data(True, 1, settings_sync.mac_values(mac_raw(hold_full_screen=True)))
        self.assertNotIn("hold_full_screen", {**data["crossing"], **data["design"]})


class RelayTests(unittest.TestCase):
    PEERS = {"a": {"settings", "text"}, "b": {"settings"}, "c": set(), "d": {"settings"}}

    def test_a_newer_state_goes_to_every_other_peer_that_takes_settings(self):
        self.assertEqual(settings_sync.recipients(self.PEERS, source="a"), ["b", "d"])

    def test_a_state_made_here_goes_to_every_peer_that_takes_settings(self):
        self.assertEqual(settings_sync.recipients(self.PEERS), ["a", "b", "d"])

    def test_a_peer_without_settings_is_never_a_recipient(self):
        self.assertNotIn("c", settings_sync.recipients(self.PEERS))

    def test_three_machines_in_a_line_all_reach_the_first_ones_change_and_nothing_loops(self):
        # A - B - C; A and C are not linked. Each holds (set_at, by); B sends on what it takes.
        links = {"A": {"B": {"settings"}}, "B": {"A": {"settings"}, "C": {"settings"}}, "C": {"B": {"settings"}}}
        ids = {"A": b64(b"\x01" * 16), "B": b64(b"\x02" * 16), "C": b64(b"\x03" * 16)}
        held = {name: (100, ids[name]) for name in links}
        held["A"] = (200, ids["A"])
        sent = []
        queue = [("A", "B", settings_sync.message_data(True, 200, settings_sync.mac_values(mac_raw(glow_style="beam")), by=ids["A"]))]
        while queue:
            src, dst, data = queue.pop(0)
            sent.append((src, dst))
            assert len(sent) < 10, "looping"
            taken = settings_sync.arrived(data, held[dst][0], KEYS, by_here=held[dst][1], now=NOW)
            if taken is None:
                continue
            held[dst] = (taken[1], data["by"])
            for other in settings_sync.recipients(links[dst], source=src):
                queue.append((dst, other, data))
        self.assertEqual([held[name][0] for name in "ABC"], [200, 200, 200])
        self.assertEqual(sent, [("A", "B"), ("B", "C")])


class WordsTests(unittest.TestCase):
    def test_a_shared_page_says_so_only_while_on(self):
        own = "For this Mac only; the other machine keeps its own."
        everyone = settings_sync.who(["Studio", "Laptop"])
        self.assertEqual(settings_sync.scope("crossing", everyone, False, own), own)
        self.assertEqual(settings_sync.scope("design", everyone, True, own),
                         "Kept the same as every paired machine. Change it on any machine.")
        self.assertEqual(settings_sync.scope("keyboard", everyone, True, own), own)

    def test_one_machine_is_named_and_none_or_several_are_every_paired_machine(self):
        self.assertEqual(settings_sync.who(["Studio"]), "Studio")
        self.assertEqual(settings_sync.who([]), "every paired machine")
        self.assertEqual(settings_sync.who(["Studio", "Laptop"]), "every paired machine")
        self.assertEqual(settings_sync.scope("design", "Studio", True, "x"), "Kept the same as Studio. Change it on any machine.")

    def test_the_switch_note_names_both_pages_and_an_old_peer(self):
        self.assertIn("Crossing and Design", settings_sync.switch_note("Studio", True, False))
        self.assertIn("match Studio's", settings_sync.switch_note("Studio", False, False))
        self.assertNotIn("Mac", settings_sync.switch_note("every paired machine", True, False))
        self.assertIn("Studio's Beamer is too old", settings_sync.switch_note("Studio", True, True))


class RealIdTests(unittest.TestCase):
    def test_a_by_in_the_unpadded_urlsafe_spelling_of_a_machine_id_is_read(self):
        from core import protocol
        ident = bytes(range(1, 17))
        text = protocol.id_text(ident)
        self.assertEqual(settings_sync._id(text), ident)
        data = settings_sync.message_data(True, 100, {}, by=text)
        self.assertIsNotNone(settings_sync.arrived(data, 0, {}))

    def test_garbage_is_still_not_an_id(self):
        self.assertIsNone(settings_sync._id("not base64!"))
        self.assertIsNone(settings_sync._id(5))


if __name__ == "__main__":
    unittest.main()
