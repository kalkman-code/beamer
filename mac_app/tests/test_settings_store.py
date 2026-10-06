import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config as config_module
from core import protocol
import settings_store
from settings_store import SettingsError, SettingsStore, config_to_raw


class SettingsStoreTests(unittest.TestCase):
    def test_all_original_saved_colours_keep_their_id_and_stops(self):
        import notch_beam
        fixture = Path(__file__).resolve().parents[2] / 'core/tests/colour_packs_rc3.json'
        palettes = json.loads(fixture.read_text())
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / 'settings.json')
            for value, stops in palettes.items():
                with self.subTest(value=value):
                    raw = {**self.valid_raw(), 'crossing': {'glow_colour': value}}
                    store.save(raw)
                    saved = store.load().crossing['glow_colour']
                    self.assertEqual(saved, value)
                    self.assertEqual(notch_beam.palette_values(saved), stops)

    def valid_raw(self):
        return {
            "host": "192.0.2.10",
            "port": 24820,
            "auth_token": "synthetic-token",
            "trigger_key": "alt_r",
            "double_tap_ms": 300,
            "key_map": {"cmd": "ctrl", "cmd_r": "ctrl"},
            "reconnect_interval_s": 2.0,
        }

    def test_save_is_private_and_round_trips_shared_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path)
            saved = store.save(self.valid_raw())
            loaded = store.load()
            self.assertEqual(saved, loaded)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["trigger_key"], "alt_r")

    def test_both_directions_default_on_and_each_switch_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            loaded = store.save(self.valid_raw())
            self.assertTrue(loaded.send_to_windows)
            self.assertTrue(loaded.allow_windows_to_drive)
            raw = config_to_raw(loaded)
            raw["send_to_windows"] = False
            loaded = store.save(raw)
            self.assertFalse(loaded.send_to_windows)
            self.assertTrue(loaded.allow_windows_to_drive)
            self.assertFalse(store.load().send_to_windows)

    def test_in_use_defaults_on_round_trips_and_rejects_non_booleans(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            store.save(self.valid_raw())
            entry = store.current()["peers"][0]
            self.assertTrue(entry["in_use"])
            store.set_peer(entry["token"], in_use=False)
            self.assertFalse(store.current()["peers"][0]["in_use"])
            with self.assertRaises(SettingsError):
                store.set_peer(entry["token"], in_use="no")

    def test_jump_key_round_trips_and_rejects_non_strings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path)
            store.save(self.valid_raw())
            settings = store.current()
            settings["peers"][0]["jump_key"] = "ctrl+shift+2"
            store.save_settings(settings)
            self.assertEqual(SettingsStore(path).current()["peers"][0]["jump_key"], "ctrl+shift+2")
            settings["peers"][0]["jump_key"] = []
            with self.assertRaises(SettingsError):
                store.save_settings(settings)

    def test_an_old_peer_without_a_jump_key_defaults_to_none(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            store.save(self.valid_raw())
            self.assertIsNone(store.current()["peers"][0].get("jump_key") or None)

    def test_trigger_clash_is_refused_without_changing_the_saved_value_and_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            store.save(self.valid_raw())
            peer = store.current()["peers"][0]
            store.set_jump_key(peer["id"], "ctrl+shift+2", "alt_r")
            with self.assertRaises(SettingsError):
                store.set_jump_key(peer["id"], "ctrl+f9", "f9")
            self.assertEqual(store.current()["peers"][0]["jump_key"], "ctrl+shift+2")
            store.set_jump_key(peer["id"], "", "alt_r")
            self.assertEqual(store.current()["peers"][0]["jump_key"], "")

    def test_the_full_screen_hold_defaults_off_and_preserves_each_saved_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            loaded = store.save(self.valid_raw())
            self.assertFalse(loaded.crossing["hold_full_screen"])
            for choice in (False, True):
                raw = config_to_raw(loaded)
                raw["crossing"]["hold_full_screen"] = choice
                self.assertEqual(store.save(raw).crossing["hold_full_screen"], choice)
            raw = config_to_raw(loaded)
            raw["crossing"]["hold_full_screen"] = "no"
            with self.assertRaises(SettingsError):
                store.save(raw)

    def test_appearance_defaults_to_system_and_each_choice_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            self.assertEqual(store.save(self.valid_raw()).appearance, "system")
            for choice in ("light", "dark", "system"):
                raw = config_to_raw(store.load())
                raw["appearance"] = choice
                store.save(raw)
                self.assertEqual(store.load().appearance, choice)
            raw = config_to_raw(store.load())
            raw["appearance"] = "sepia"
            self.assertEqual(store.save(raw).appearance, "system")

    def test_a_hand_edited_appearance_loads_as_system(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({**self.valid_raw(), "appearance": "sepia"}), encoding="utf-8")
            self.assertEqual(config_module.load_config(str(path)).appearance, "system")

    def test_appearances_match_the_shared_tokens(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        import tokens
        self.assertEqual(config_module.APPEARANCES, tokens.APPEARANCES)

    def test_legacy_positional_key_map_is_replaced_by_the_semantic_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            raw = self.valid_raw()
            raw["key_map"] = {
                "alt": "alt", "alt_r": "alt",
                "ctrl": "ctrl", "ctrl_r": "ctrl",
                "cmd": "cmd", "cmd_r": "cmd",
            }
            loaded = SettingsStore(path).save(raw)
            self.assertEqual(loaded.key_map["cmd"], "ctrl")
            self.assertEqual(loaded.key_map["ctrl"], "cmd")

    def test_hand_edited_key_map_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            raw = self.valid_raw()
            raw["key_map"] = {"cmd": "cmd", "ctrl": "alt"}
            loaded = SettingsStore(path).save(raw)
            self.assertEqual(loaded.key_map["cmd"], "cmd")
            self.assertEqual(loaded.key_map["ctrl"], "alt")

    def test_rejects_placeholder_token(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = self.valid_raw()
            raw["auth_token"] = "CHANGE_ME"
            with self.assertRaises(SettingsError):
                SettingsStore(Path(directory) / "settings.json").save(raw)

    def test_rejects_unknown_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = self.valid_raw()
            raw["trigger_key"] = "not_a_key"
            with self.assertRaises(SettingsError):
                SettingsStore(Path(directory) / "settings.json").save(raw)

    def test_config_without_crossing_keys_loads_the_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(self.valid_raw()), encoding="utf-8")
            loaded = SettingsStore(path).load()
            self.assertEqual(loaded.trigger_style, "double_tap")
            self.assertEqual(loaded.crossing, config_module.DEFAULT_CROSSING)
            self.assertIsNot(loaded.crossing, config_module.DEFAULT_CROSSING)

    def test_fresh_and_partial_crossing_use_colourful_but_keep_saved_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path)
            fresh = store.save(self.valid_raw())
            self.assertEqual(fresh.crossing["glow_colour"], "colourful")

            raw = config_to_raw(fresh)
            raw["crossing"]["glow_colour"] = "signal"
            store.save(raw)
            self.assertEqual(SettingsStore(path).load().crossing["glow_colour"], "signal")

    def test_showing_where_the_pointer_lands_is_on_for_an_old_config_and_round_trips_off(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            raw = self.valid_raw()
            raw["crossing"] = {"edge": "left", "glow_style": "rupture"}
            path.write_text(json.dumps(raw), encoding="utf-8")
            store = SettingsStore(path)
            loaded = store.load()
            self.assertTrue(loaded.crossing["shortcut_arrival"])
            raw = config_to_raw(loaded)
            raw["crossing"]["shortcut_arrival"] = False
            store.save(raw)
            self.assertFalse(store.load().crossing["shortcut_arrival"])
            self.assertEqual(store.load().crossing["shortcut_arrival_style"], "match")
            raw["crossing"]["shortcut_arrival_style"] = "discharge"
            store.save(raw)
            self.assertEqual(store.load().crossing["shortcut_arrival_style"], "discharge")
            self.assertEqual(store.load().crossing["glow_style"], "rupture")

    def test_the_length_defaults_to_normal_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            raw = config_to_raw(settings_store.editable_default_config())
            path.write_text(json.dumps(raw))
            store = SettingsStore(path)
            self.assertEqual(store.load().crossing["effect_length"], "normal")
            raw["crossing"]["effect_length"] = "short"
            store.save(raw)
            self.assertEqual(store.load().crossing["effect_length"], "short")
            raw["crossing"]["effect_length"] = "forever"
            with self.assertRaises(SettingsError):
                store.save(raw)

    def test_a_saved_warp_choice_reads_as_its_stand_in(self):
        # Warp is offered nowhere for now; a Mac that chose it keeps working.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            raw = config_to_raw(settings_store.editable_default_config())
            raw["crossing"].update(glow_style="hyperdrive", glow_colour="vaporwave", shortcut_arrival_style="light_slit")
            path.write_text(json.dumps(raw))
            crossing = SettingsStore(path).load().crossing
        self.assertEqual((crossing["glow_style"], crossing["glow_colour"], crossing["shortcut_arrival_style"]),
                         ("glow", "colourful", "match"))

    def test_partial_crossing_object_keeps_the_other_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = self.valid_raw()
            raw["crossing"] = {"edge": "left", "resistance_px": 40}
            raw["trigger_style"] = "hold"
            loaded = SettingsStore(Path(directory) / "settings.json").save(raw)
            self.assertEqual(loaded.crossing["edge"], "left")
            self.assertEqual(loaded.crossing["resistance_px"], 40)
            self.assertEqual(loaded.crossing["methods"], ["shortcut", "edge"])
            self.assertTrue(loaded.crossing["haptics"])
            self.assertEqual(loaded.trigger_style, "hold")

    def test_rejects_bad_crossing_values_by_field(self):
        cases = [
            ({"methods": ["edge", "telepathy"]}, "crossing.methods"),
            ({"edge": "sideways"}, "crossing.edge"),
            ({"corner": "middle"}, "crossing.corner"),
            ({"resistance_px": 501}, "crossing.resistance_px"),
            ({"resistance_px": -1}, "crossing.resistance_px"),
            ({"glow": "yes"}, "crossing.glow"),
            ({"shortcut_arrival": "yes"}, "crossing.shortcut_arrival"),
            ({"edge_parts": []}, "crossing.edge_parts"),
            ({"edge_parts": ["side"]}, "crossing.edge_parts"),
            ({"edge_parts": "middle"}, "crossing.edge_parts"),
            ({"edge_parts": [["middle"]]}, "crossing.edge_parts"),
            ({"edge_parts": [{}]}, "crossing.edge_parts"),
            ({"methods": [["edge"]]}, "crossing.methods"),
            ({"methods": ["part", "sideways"]}, "crossing.methods"),
            ({"shortcut_arrival_style": "sparkle"}, "crossing.shortcut_arrival_style"),
            ({"notch_style": "sparkle"}, "crossing.notch_style"),
            ({"notch_after_ms": 50}, "crossing.notch_after_ms"),
            ({"notch_after_ms": 1.5}, "crossing.notch_after_ms"),
            ({"haptic_feel": "thunderous"}, "crossing.haptic_feel"),
            ({"haptic_steps": "thirds"}, "crossing.haptic_steps"),
            ({"glow_style": "sparkle"}, "crossing.glow_style"),
            ({"glow_colour": "tartan"}, "crossing.glow_colour"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for crossing, field in cases:
                with self.subTest(field=field):
                    raw = self.valid_raw()
                    raw["crossing"] = crossing
                    with self.assertRaises(SettingsError) as caught:
                        SettingsStore(Path(directory) / "settings.json").save(raw)
                    self.assertIn(field, str(caught.exception))

    def test_every_crossing_effect_and_colour_pack_round_trips(self):
        from core import effects

        pairs = [(style, "signal") for style in effects.EFFECT_IDS]
        pairs += [(style, colour) for style in ("glow", "beam", "aperture") for colour in effects.PACK_IDS]
        pairs += list(zip(effects.EFFECT_IDS, effects.PACK_IDS))
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            for style, colour in pairs:
                with self.subTest(style=style, colour=colour):
                    raw = self.valid_raw()
                    raw["crossing"] = {"glow_style": style, "glow_colour": colour}
                    store.save(raw)
                    loaded = store.load()
                    self.assertEqual((loaded.crossing["glow_style"], loaded.crossing["glow_colour"]), (style, colour))

    def test_rejects_a_direction_or_a_pack_where_a_style_goes(self):
        cases = [
            ({"glow_style": "membrane"}, "crossing.glow_style"),
            ({"glow_style": "Skin"}, "crossing.glow_style"),
            ({"glow_style": "neon"}, "crossing.glow_style"),
            ({"glow_colour": "rupture"}, "crossing.glow_colour"),
            ({"glow_colour": "atmosphere"}, "crossing.glow_colour"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for crossing, field in cases:
                with self.subTest(crossing=crossing):
                    raw = self.valid_raw()
                    raw["crossing"] = crossing
                    with self.assertRaises(SettingsError) as caught:
                        SettingsStore(Path(directory) / "settings.json").save(raw)
                    self.assertIn(field, str(caught.exception))

    def test_rejects_unknown_trigger_style(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = self.valid_raw()
            raw["trigger_style"] = "triple_tap"
            with self.assertRaises(SettingsError):
                SettingsStore(Path(directory) / "settings.json").save(raw)

    def test_key_map_style_names_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            raw = self.valid_raw()
            raw["key_map"] = "positional"
            loaded = SettingsStore(path).save(raw)
            self.assertEqual(loaded.key_map, config_module.LEGACY_POSITIONAL_KEY_MAP)
            self.assertEqual(config_to_raw(loaded)["key_map"], "positional")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["key_map"], "positional")
            raw["key_map"] = "semantic"
            self.assertEqual(SettingsStore(path).save(raw).key_map, config_module.DEFAULT_KEY_MAP)
            raw["key_map"] = "chaotic"
            with self.assertRaises(SettingsError):
                SettingsStore(path).save(raw)



if __name__ == "__main__":
    unittest.main()


class LegacyPortTests(unittest.TestCase):
    def raw(self, port):
        return {
            "host": "192.0.2.10",
            "port": port,
            "auth_token": "synthetic-token",
            "trigger_key": "alt_r",
            "double_tap_ms": 300,
            "key_map": {"cmd": "ctrl", "cmd_r": "ctrl"},
            "reconnect_interval_s": 2.0,
        }

    def test_the_old_default_port_is_moved_and_the_move_is_written_down(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            (Path(directory) / "config.json").write_text(json.dumps(self.raw(protocol.LEGACY_DEFAULT_PORT)), encoding="utf-8")
            store = SettingsStore(path)
            self.assertEqual(store.load().port, protocol.DEFAULT_PORT)
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["port"], protocol.DEFAULT_PORT)
            self.assertEqual(on_disk["peers"][0]["port"], protocol.DEFAULT_PORT)

    def test_a_port_someone_chose_is_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = SettingsStore(path)
            store.save(self.raw(9000))
            self.assertEqual(store.load().port, 9000)

    def test_both_ports_are_below_the_range_the_system_hands_out(self):
        self.assertLess(protocol.DEFAULT_PORT, 49152)
        self.assertLess(protocol.PAIRING_PORT, 49152)
