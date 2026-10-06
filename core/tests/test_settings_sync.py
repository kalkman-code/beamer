"""Same on all machines, without AppKit or Qt: the message, the merge and the window's words."""

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
        "glow_style": "glow", "glow_colour": "signal", "effect_length": "normal", "effect_size": "medium",
        "shortcut_arrival": True,
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
        double_tap_ms=300, glow_style="glow", glow_colour="signal", effect_length="normal", effect_size="medium",
        shortcut_arrival=True, shortcut_arrival_style="match", mac_return_edge="left", edge_glow=True,
        appearance="light",
    )
    base.update(fields)
    return SimpleNamespace(**base)


class MessageTests(unittest.TestCase):
    def test_pc_modifier_style_is_local_and_not_part_of_same_on_all_machines(self):
        self.assertNotIn("modifier_style", settings_sync.CROSSING_KEYS + settings_sync.DESIGN_KEYS)

    def test_new_design_choices_survive_the_settings_message_round_trip(self):
        from core import effects

        styles = ("aperture", "crease", "pleat", "concertina", "thread", "weave", "jacquard")
        colours = ("vellum", "carbon_copy", "marbled", "flax", "madder", "tide")
        for style in styles:
            for colour in colours:
                with self.subTest(style=style, colour=colour):
                    values = settings_sync.mac_values(mac_raw(
                        glow_style=style, glow_colour=colour, shortcut_arrival_style=style,
                    ))
                    data = settings_sync.message_data(True, 7, values)
                    _on, _stamp, received = settings_sync.read(data, KEYS)
                    self.assertEqual(received["glow_style"], style)
                    self.assertEqual(received["glow_colour"], colour)
                    self.assertEqual(received["shortcut_arrival_style"], style)
        self.assertTrue(set(styles) <= set(effects.EFFECT_IDS))
        self.assertTrue(set(colours) <= set(effects.PACK_IDS))
    def test_an_optional_design_state_keeps_its_own_stamp_and_effect_size(self):
        values = {**settings_sync.mac_values(mac_raw()), "effect_size": "large"}
        design_state = {"set_at": 63, "by": b64(b"\x03" * 16), "values": values}
        data = settings_sync.message_data(False, 50, design_state=design_state)

        self.assertFalse(data["on"])
        self.assertEqual(data["design_sync"], design_state)

    def test_a_settings_message_without_a_design_state_remains_readable(self):
        data = settings_sync.message_data(False, 50)

        self.assertIsNone(settings_sync.read_design_state(data, KEYS))

    def test_a_design_state_is_validated_independently_of_same_on_all_machines(self):
        values = {"glow_style": "beam", "unknown": "ignored"}
        state = {"set_at": NOW, "by": b64(b"\x03" * 16), "values": values}

        self.assertEqual(settings_sync.read_design_state(
            {"on": False, "set_at": NOW, "by": b64(bytes(16)), "design_sync": state}, KEYS),
                         {"set_at": NOW, "by": state["by"],
                          "values": {"effect_size": "medium", "glow_style": "beam"}})

    def test_an_invalid_effect_size_is_ignored_but_a_missing_one_defaults_to_medium(self):
        state = {"set_at": NOW, "by": b64(bytes(16)),
                 "values": {"effect_size": "giant", "glow_style": "beam"}}

        self.assertEqual(settings_sync.read_design_state(
            {"on": False, "set_at": NOW, "by": b64(bytes(16)), "design_sync": state}, KEYS),
                         {"set_at": NOW, "by": state["by"], "values": {"glow_style": "beam"}})

    def test_a_design_state_in_a_malformed_settings_envelope_is_ignored(self):
        for outer in ({"on": "no"}, {"set_at": True}, {"by": "not an id"}):
            data = {"on": False, "set_at": NOW, "by": b64(bytes(16)), **outer,
                    "design_sync": {"set_at": NOW + 1, "by": b64(bytes([1]) * 16),
                                    "values": {"glow_style": "beam"}}}
            self.assertIsNone(settings_sync.read_design_state(data, KEYS), outer)

    def test_same_on_design_change_gets_a_new_local_design_stamp(self):
        changed = settings_sync.design_change_stamp(
            {"effect_length": "normal"}, {"effect_length": "long"}, 50,
            {"set_at": 90}, b64(b"\x02" * 16), now=100,
        )

        self.assertEqual(changed, {"set_at": 100, "by": b64(b"\x02" * 16)})
        self.assertIsNone(settings_sync.design_change_stamp(
            {"effect_length": "long"}, {"effect_length": "long"}, 50,
            {"set_at": 90}, b64(b"\x02" * 16), now=100,
        ))

    def test_design_states_use_the_same_stamp_order_as_same_on_all_machines(self):
        larger = {"set_at": 100, "by": b64(b"\x02" * 16), "values": {"glow_style": "beam"}}
        self.assertTrue(settings_sync.design_arrived(larger, 100, b64(b"\x01" * 16)))
        self.assertFalse(settings_sync.design_arrived(larger, 100, larger["by"]))
        self.assertFalse(settings_sync.design_arrived(larger, 101, ""))

    def test_design_follow_chain_keeps_the_leaders_stamp(self):
        a_id, b_id = (b64(bytes([byte]) * 16) for byte in (1, 2))
        a_state = {"set_at": 100, "by": a_id, "values": {"glow_style": "beam"}}

        b = settings_sync.followed_design({"glow_style": "glow"}, a_state, a_id, a_id, False,
                                          50, b64(bytes(16)))
        c = settings_sync.followed_design({"glow_style": "glow"},
                                          {"set_at": b["set_at"], "by": b["by"], "values": b["values"]},
                                          b_id, b_id, False, 20, b64(bytes(16)))

        self.assertTrue(b["changed"] and c["changed"])
        self.assertEqual(c["values"]["glow_style"], "beam")
        self.assertEqual((b["set_at"], b["by"], c["set_at"], c["by"]),
                         (100, a_id, 100, a_id))

    def test_explicit_follow_adopts_all_six_values_despite_a_newer_local_stamp(self):
        peer = b64(bytes([2]) * 16)
        current = settings_sync.mac_values(mac_raw())
        desired = dict(glow_style="beam", glow_colour="ocean", effect_length="long",
                       effect_size="large", shortcut_arrival=False, shortcut_arrival_style="locator")
        state = {"set_at": 10, "by": peer, "values": desired}
        taken = settings_sync.followed_design(current, state, peer, peer, False, 500, "", force=True)
        self.assertEqual({key: taken["values"][key] for key in settings_sync.DESIGN_KEYS}, desired)
        self.assertEqual(taken["values"]["resistance_px"], 120)

    def test_follow_path_detects_a_cycle_even_when_values_are_identical(self):
        own, peer = b64(bytes([1]) * 16), b64(bytes([2]) * 16)
        state = {"set_at": 10, "by": peer, "values": {"glow_style": "beam"}, "path": [own, peer]}
        taken = settings_sync.followed_design({"glow_style": "beam"}, state, peer, peer, False,
                                              0, "", own_id=own)
        self.assertTrue(taken["cycle"])

    def test_follow_path_survives_validation_and_invalid_paths_are_rejected(self):
        own, peer = b64(bytes([1]) * 16), b64(bytes([2]) * 16)
        for path in ([own, peer], [], ["garbage"], [own, own], [None]):
            data = settings_sync.message_data(False, 10, design_state={
                "set_at": 10, "by": peer, "values": {"glow_style": "beam"}, "path": path})
            state = settings_sync.read_design_state(data, KEYS)
            if path == [own, peer]:
                self.assertEqual(state["path"], path)
            else:
                self.assertIsNone(state)

    def test_legacy_padded_author_and_source_produce_a_valid_downstream_path(self):
        from core import protocol
        source, own = (protocol.id_text(bytes([value]) * 16) for value in (1, 2))
        state = {"set_at": 10, "by": source + "==", "values": {"glow_style": "beam"}}
        taken = settings_sync.followed_design({"glow_style": "glow"}, state, source, source, False,
                                              0, "", own_id=own)
        advertised = {"set_at": 20, "by": own, "values": taken["values"], "path": taken["path"]}
        received = settings_sync.read_design_state(settings_sync.message_data(False, 20, design_state=advertised), KEYS)
        self.assertIsNotNone(received)
        self.assertEqual(len(received["path"]), 2)

    def test_follower_accepts_the_leaders_next_stamp_after_reannouncing_it(self):
        a_id, b_id = b64(b"\x01" * 16), b64(b"\x02" * 16)
        current = {"glow_style": "glow"}
        first = {"set_at": 100, "by": a_id, "values": {"glow_style": "beam"}}
        applied = settings_sync.followed_design(current, first, a_id, a_id, False,
                                                50, "")
        next_from_a = {"set_at": 101, "by": a_id, "values": {"glow_style": "flint"}}

        updated = settings_sync.followed_design(
            applied["values"], next_from_a, a_id, a_id, False,
            applied["set_at"], applied["by"],
        )

        self.assertTrue(updated["changed"])
        self.assertEqual(updated["values"]["glow_style"], "flint")
        self.assertEqual((updated["set_at"], updated["by"]), (101, a_id))

    def test_mutual_design_follow_converges_without_a_second_announcement(self):
        a_id, b_id = b64(b"\x01" * 16), b64(b"\x02" * 16)
        b_after_a = settings_sync.followed_design(
            {"glow_style": "glow"}, {"set_at": 100, "by": a_id, "values": {"glow_style": "beam"}},
            a_id, a_id, False, 90, b64(bytes(16)),
        )
        a_after_b = settings_sync.followed_design(
            {"glow_style": "beam"},
            {"set_at": b_after_a["set_at"], "by": b_after_a["by"], "values": b_after_a["values"]},
            b_id, b_id, False, 100, a_id,
        )

        self.assertTrue(b_after_a["changed"])
        self.assertIsNone(a_after_b)

    def test_mutual_design_announcements_crossing_in_flight_converge_on_the_newer_stamp(self):
        a_id, b_id = b64(bytes([1]) * 16), b64(bytes([2]) * 16)
        ids = {"A": a_id, "B": b_id}
        held = {
            "A": {"values": {"glow_style": "beam"}, "set_at": 200, "by": a_id},
            "B": {"values": {"glow_style": "glow"}, "set_at": 100, "by": b_id},
        }
        # Both link-up announcements are already in flight before either side reacts.
        queue = [("A", "B", dict(held["A"])), ("B", "A", dict(held["B"]))]
        sent = 0
        while queue:
            source, follower, incoming = queue.pop(0)
            sent += 1
            self.assertLess(sent, 10, "mutual follows kept re-announcing")
            current = held[follower]
            applied = settings_sync.followed_design(
                current["values"], incoming,
                source, source, False, current["set_at"], current["by"],
            )
            if applied is not None and applied["changed"]:
                held[follower] = {"values": applied["values"], "set_at": applied["set_at"], "by": applied["by"]}
                queue.append((follower, source, dict(held[follower])))

        self.assertEqual(held["A"]["values"]["glow_style"], "beam")
        self.assertEqual(held["B"]["values"]["glow_style"], "beam")
        self.assertEqual((held["A"]["set_at"], held["A"]["by"]), (200, a_id))
        self.assertEqual((held["B"]["set_at"], held["B"]["by"]), (200, a_id))

    def test_design_follow_ignores_other_peers_and_waits_while_same_is_on(self):
        state = {"set_at": 100, "by": b64(b"\x02" * 16), "values": {"glow_style": "beam"}}
        peer = state["by"]
        current = {"glow_style": "glow"}

        self.assertIsNone(settings_sync.followed_design(current, state, peer, "", False, 0, ""))
        self.assertIsNone(settings_sync.followed_design(current, state, peer, peer, True, 0, ""))

    def test_a_newer_identical_design_keeps_its_stamp_without_an_announcement(self):
        peer = b64(bytes([2]) * 16)
        applied = settings_sync.followed_design(
            {"glow_style": "beam"}, {"set_at": 100, "by": peer, "values": {"glow_style": "beam"}},
            peer, peer, False, 50, "",
        )

        self.assertEqual(applied, {"changed": False, "values": {"glow_style": "beam"},
                                   "set_at": 100, "by": peer})

    def test_off_carries_no_values(self):
        data = settings_sync.message_data(False, 50, settings_sync.mac_values(mac_raw()))
        self.assertEqual(data, {"on": False, "set_at": 50, "by": ""})

    def test_on_carries_crossing_and_design_and_never_the_notch_or_this_machines_own(self):
        data = settings_sync.message_data(True, 50, settings_sync.mac_values(mac_raw()))
        self.assertEqual(set(data["crossing"]), set(settings_sync.CROSSING_KEYS))
        self.assertEqual(set(data["design"]), set(settings_sync.DESIGN_KEYS))
        self.assertEqual(data["design"]["effect_size"], "medium")
        self.assertIs(data["crossing"]["shortcut"], True)
        flat = {**data["crossing"], **data["design"]}
        for own in ("notch_style", "notch_after_ms", "haptics", "glow", "appearance", "edge", "hold_full_screen",
                    "methods", "edge_parts", "corner"):
            self.assertNotIn(own, flat)

    def test_jump_keys_are_not_sent_by_same_on_all_machines(self):
        values = settings_sync.mac_values(mac_raw())
        values["jump_key"] = "ctrl+alt+2"
        self.assertNotIn("jump_key", settings_sync.message_data(True, 50, values)["crossing"])

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
    def test_size_is_read_applied_and_transmitted_on_both_machines(self):
        mac = mac_raw(effect_size="large")
        values = settings_sync.mac_values(mac)
        self.assertEqual(values["effect_size"], "large")
        self.assertEqual(settings_sync.apply_mac(mac_raw(), values)["crossing"]["effect_size"], "large")
        message = settings_sync.message_data(True, 1, values)
        self.assertEqual(message["design"]["effect_size"], "large")
        self.assertEqual(settings_sync.read(message, KEYS)[2]["effect_size"], "large")

        pc = pc_config(effect_size="small")
        values = settings_sync.pc_values(pc)
        self.assertEqual(values["effect_size"], "small")
        target = pc_config()
        settings_sync.apply_pc(target, values)
        self.assertEqual(target.effect_size, "small")

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
                         "Style, colour, length, size and landing animation stay in step. Animation enable and window appearance stay local.")
        self.assertEqual(settings_sync.scope("keyboard", everyone, True, own), own)

    def test_one_machine_is_named_and_none_or_several_are_every_paired_machine(self):
        self.assertEqual(settings_sync.who(["Studio"]), "Studio")
        self.assertEqual(settings_sync.who([]), "every paired machine")
        self.assertEqual(settings_sync.who(["Studio", "Laptop"]), "every paired machine")
        self.assertEqual(settings_sync.scope("design", "Studio", True, "x"),
                         "Style, colour, length, size and landing animation stay in step. Animation enable and window appearance stay local.")

    def test_the_switch_note_names_shared_values_and_an_old_peer(self):
        self.assertIn("resistance, drag protection, Shortcut", settings_sync.switch_note("Studio", True, False))
        self.assertIn("previous separate values are not restored", settings_sync.switch_note("Studio", False, False))
        self.assertIn("compatible paired machines", settings_sync.switch_note("every paired machine", True, False))
        self.assertIn("catch up when they reconnect", settings_sync.switch_detail())
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


class FollowRowsTests(unittest.TestCase):
    PC, LIVE, AWAY, OLD = (b64(bytes([n]) * 16) for n in (1, 2, 3, 4))

    def entries(self, *ids):
        return [{"id": ident} for ident in ids]

    def test_only_connected_machines_are_listed_and_away_ones_never_set_the_note(self):
        caps = {self.PC: frozenset({"settings", "design_sync"})}
        rows = settings_sync.follow_rows(self.entries(self.PC, self.AWAY), caps, {self.PC: {}})
        self.assertEqual(rows, [({"id": self.PC}, None)])
        self.assertIsNone(settings_sync.follow_note(rows))

    def test_the_followed_machine_stays_listed_while_away(self):
        rows = settings_sync.follow_rows(self.entries(self.PC, self.AWAY), {}, {self.AWAY: {}}, following=self.AWAY)
        self.assertEqual(rows, [({"id": self.AWAY}, None)])

    def test_why_each_connected_machine_cannot_be_chosen(self):
        caps = {self.LIVE: frozenset({"settings", "design_sync"}), self.OLD: frozenset({"settings"})}
        rows = settings_sync.follow_rows(self.entries(self.LIVE, self.OLD), caps, {})
        self.assertEqual([why for _entry, why in rows], [settings_sync.DESIGN_UNAVAILABLE, settings_sync.NEEDS_DESIGN_SYNC])
        self.assertEqual(settings_sync.follow_note(rows), settings_sync.OLD_PEER_NOTE)
        self.assertEqual(settings_sync.follow_note(rows[:1]), settings_sync.DESIGN_UNAVAILABLE)

    def test_nothing_connected_is_one_quiet_line(self):
        rows = settings_sync.follow_rows(self.entries(self.AWAY), {}, {self.AWAY: {}})
        self.assertEqual(rows, [])
        self.assertEqual(settings_sync.follow_note(rows), settings_sync.NO_MACHINE_CONNECTED)


class FollowLockTests(unittest.TestCase):
    PC, OTHER = (b64(bytes([n]) * 16) for n in (1, 2))

    def test_following_a_paired_desktop_locks_its_design_controls(self):
        desktops = [{"id": self.OTHER}, {"id": self.PC, "name": "DESK-PC"}]
        self.assertEqual(settings_sync.follow_lock(self.PC, False, desktops), desktops[1])
        self.assertEqual(settings_sync.following_banner("DESK-PC"), "Following DESK-PC's design")

    def test_unlocked_with_no_selection_with_same_on_or_once_the_machine_is_unpaired(self):
        desktops = [{"id": self.PC}]
        self.assertIsNone(settings_sync.follow_lock("", False, desktops))
        self.assertIsNone(settings_sync.follow_lock(self.PC, True, desktops))
        self.assertIsNone(settings_sync.follow_lock(self.OTHER, False, desktops))


if __name__ == "__main__":
    unittest.main()
