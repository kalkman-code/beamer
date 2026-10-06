"""The Windows app's settings.json and its migration from 1.4.x's config.json (WIRE.md section 1
and section 8)."""

import base64
import hashlib
import hmac
import importlib.util
import itertools
import json
import os
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest import mock

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app_config
from app_config import ConfigError

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


LEGACY = json.loads((FIXTURES / "config_1_4_3.json").read_text(encoding="utf-8"))
PAIRED = LEGACY["auth_token"]
OTHER = b64(bytes(range(1, 33)))
THIRD = b64(bytes(range(2, 34)))
PEER_ID = b64(bytes([7]) * 16)
OTHER_ID = b64(bytes([8]) * 16)
OWN_ID = b64(bytes([9]) * 16)

# What a migrated entry is, for the fixture: every field of section 1's table.
MIGRATED_PEER = {
    "id": "", "name": "MacBook Pro", "platform": "macos", "host": "192.0.2.10", "port": 24820,
    "hw": "aa:bb:cc:dd:ee:ff", "send": True, "allow_drive": True, "in_use": True, "side": "left", "side_set_at": 1790000000,
    "paired_with": [], "paired_at": 0, "linked": False, "from_1_4": True,
}

MOVED_OR_DROPPED = (
    "host", "mac_host", "auth_token", "paired_with", "mac_hardware_address", "send_to_mac", "allow_mac_to_drive",
    "mac_return_edge", "arrangement_set_at", "crossing_methods", "crossing_edge_parts", "crossing_corner",
    "mac_resistance_px",
)


class Folder(unittest.TestCase):
    """A settings folder holding 1.4.3's config.json, and the path of the settings.json beside it."""

    legacy = LEGACY

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.folder = Path(self._directory.name)
        self.legacy_path = self.folder / "config.json"
        self.path = self.folder / "settings.json"
        if self.legacy is not None:
            self.write_legacy(self.legacy)

    def write_legacy(self, raw):
        self.legacy_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        os.utime(self.legacy_path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))

    def settings(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def put(self, settings):
        self.path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


class MigrationTests(Folder):
    def test_the_paths_are_settings_json_and_the_old_file_beside_it(self):
        self.assertEqual(app_config.default_config_path().name, "settings.json")
        self.assertEqual(app_config.legacy_config_path(self.path), self.legacy_path)

    def test_1_4_3s_config_becomes_the_settings_section_1_says_field_by_field(self):
        settings = app_config.load_settings(self.path)
        self.assertEqual(settings, self.settings())
        self.assertEqual(settings["schema"], 6)
        self.assertEqual(settings["name"], "")
        self.assertEqual(settings["port"], 24820)
        self.assertIs(settings["shortcut"], True)
        self.assertEqual(settings["migrated_token_sha256"], hashlib.sha256(PAIRED.encode()).hexdigest())
        self.assertEqual(len(settings["peers"]), 1)
        self.assertEqual(settings["peers"][0], {**MIGRATED_PEER, "token": PAIRED, "side_by": settings["machine_id"]})
        for key in MOVED_OR_DROPPED:
            self.assertNotIn(key, settings)
        for key, value in LEGACY.items():
            if key not in MOVED_OR_DROPPED and key != "port":
                self.assertEqual(settings[key], value, key)

    def test_peer_in_use_must_be_a_boolean(self):
        settings = app_config.load_settings(self.path)
        settings["peers"][0]["in_use"] = "yes"
        self.put(settings)
        with self.assertRaises(app_config.SettingsFileError):
            app_config.load_settings(self.path)

    def test_jump_key_must_be_a_recorded_chord_and_round_trips(self):
        settings = app_config.load_settings(self.path)
        settings["peers"][0]["jump_key"] = "ctrl+shift+2"
        app_config.write_settings(self.path, settings)
        self.assertEqual(app_config.load_settings(self.path)["peers"][0]["jump_key"], "ctrl+shift+2")
        settings["peers"][0]["jump_key"] = "nonsense"
        with self.assertRaises(app_config.SettingsFileError):
            app_config.write_settings(self.path, settings)

    def test_load_rejects_a_malformed_jump_key(self):
        settings = app_config.load_settings(self.path)
        settings["peers"][0]["jump_key"] = "ctrl"
        self.put(settings)
        with self.assertRaises(app_config.SettingsFileError):
            app_config.load_settings(self.path)

    def test_jump_key_clash_is_refused_without_saving_and_can_be_cleared(self):
        settings = app_config.load_settings(self.path)
        peer_id = settings["peers"][0]["id"]
        app_config.set_jump_key(self.path, peer_id, "ctrl+shift+2", "alt_r")
        with self.assertRaises(app_config.ConfigError):
            app_config.set_jump_key(self.path, peer_id, "ctrl+alt+2", "alt_r")
        self.assertEqual(app_config.load_settings(self.path)["peers"][0]["jump_key"], "ctrl+shift+2")
        app_config.set_jump_key(self.path, peer_id, "", "alt_r")
        self.assertEqual(app_config.load_settings(self.path)["peers"][0]["jump_key"], "")

    def test_the_machine_id_is_sixteen_random_bytes_in_b64_and_never_zero(self):
        ids = set()
        for _ in range(3):
            self.path.unlink(missing_ok=True)
            ids.add(app_config.load_settings(self.path)["machine_id"])
        self.assertEqual(len(ids), 3)
        for machine_id in ids:
            self.assertEqual(len(machine_id), 22)
            raw = base64.urlsafe_b64decode(machine_id + "==")
            self.assertEqual((len(raw), b64(raw)), (16, machine_id))
            self.assertNotEqual(raw, bytes(16))

    def test_an_id_of_sixteen_zero_bytes_is_drawn_again(self):
        draws = iter([bytes(16), bytes([5]) * 16])
        with mock.patch.object(app_config.os, "urandom", lambda count: next(draws)):
            self.assertEqual(app_config.new_machine_id(), b64(bytes([5]) * 16))

    def test_side_by_is_this_machines_id_only_when_a_side_was_ever_set(self):
        self.write_legacy({**LEGACY, "arrangement_set_at": 0})
        settings = app_config.load_settings(self.path)
        self.assertEqual((settings["peers"][0]["side_set_at"], settings["peers"][0]["side_by"]), (0, ""))

    def test_the_old_default_port_moves_down_and_a_chosen_one_stays(self):
        self.write_legacy({**LEGACY, "port": 51820})
        self.assertEqual(app_config.load_settings(self.path)["port"], 24820)
        self.assertEqual(app_config.load_settings(self.path)["peers"][0]["port"], 24820)
        self.path.unlink()
        self.write_legacy({**LEGACY, "port": 9000})
        settings = app_config.load_settings(self.path)
        self.assertEqual((settings["port"], settings["peers"][0]["port"]), (9000, 9000))

    def test_the_saved_key_style_survives_each_way(self):
        for style in app_config.MODIFIER_STYLES:
            with self.subTest(style=style):
                self.path.unlink(missing_ok=True)
                self.write_legacy({**LEGACY, "modifier_style": style})
                self.assertEqual(app_config.load_settings(self.path)["modifier_style"], style)
                self.assertEqual(app_config.load_config(self.path).modifier_style, style)

    def test_a_config_from_main_before_milestone_2_migrates_with_its_same_on_both(self):
        self.write_legacy({**LEGACY, "same_on_both": True, "same_set_at": 1790000500, "hold_full_screen": False})
        settings = app_config.load_settings(self.path)
        self.assertEqual((settings["same_on_both"], settings["same_set_at"], settings["hold_full_screen"]),
                         (True, 1790000500, False))
        config = app_config.load_config(self.path)
        self.assertEqual((config.same_on_both, config.same_set_at, config.hold_full_screen), (True, 1790000500, False))

    def test_a_field_1_4_x_did_not_have_takes_its_default(self):
        self.write_legacy({key: value for key, value in LEGACY.items() if key != "hide_addresses"})
        self.assertNotIn("hide_addresses", app_config.load_settings(self.path))
        self.assertIs(app_config.load_config(self.path).hide_addresses, False)

    def test_the_old_file_is_kept_byte_for_byte_and_its_time_unchanged(self):
        before = (self.legacy_path.read_bytes(), self.legacy_path.stat().st_mtime_ns)
        app_config.load_settings(self.path)
        config = app_config.load_config(self.path)
        for day in range(3):
            app_config.save_config(self.path, replace(config, pointer_speed=1.0 + day / 10))
        self.assertEqual((self.legacy_path.read_bytes(), self.legacy_path.stat().st_mtime_ns), before)

    def test_starting_again_changes_nothing(self):
        first = app_config.load_settings(self.path)
        raw = self.path.read_bytes()
        mtime = self.path.stat().st_mtime_ns
        self.assertEqual(app_config.load_settings(self.path), first)
        app_config.load_config(self.path)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), (raw, mtime))

    def test_a_failure_before_the_rename_leaves_no_settings_and_the_next_start_migrates(self):
        with mock.patch.object(app_config.os, "replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                app_config.load_settings(self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(sorted(entry.name for entry in self.folder.iterdir()), ["config.json"])
        self.assertEqual(len(app_config.load_settings(self.path)["peers"]), 1)

    def test_a_failure_while_loading_the_config_is_an_error_the_app_can_show(self):
        with mock.patch.object(app_config.os, "replace", side_effect=OSError("disk gone")):
            with self.assertRaises(ConfigError):
                app_config.load_config(self.path)

    def test_1_4_3s_own_loader_reads_the_kept_file_and_gets_the_pairing_back(self):
        app_config.load_config(self.path)
        app_config.save_config(self.path, replace(app_config.load_config(self.path), glow_style="glow"))
        spec = importlib.util.spec_from_file_location("app_config_1_4_3", FIXTURES / "app_config_1_4_3.py")
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
        config = old.load_config(self.legacy_path)
        self.assertEqual((config.auth_token, config.mac_host, config.paired_with, config.mac_return_edge),
                         (PAIRED, "192.0.2.10", "MacBook Pro", "left"))
        self.assertEqual(config.glow_style, "beam")

    def test_a_typed_token_migrates_and_is_not_a_paired_one(self):
        self.write_legacy({**LEGACY, "auth_token": "a word typed into 1.4.x"})
        peer = app_config.load_settings(self.path)["peers"][0]
        self.assertEqual(peer["token"], "a word typed into 1.4.x")
        self.assertFalse(app_config.is_paired_token(peer["token"]))
        self.assertTrue(app_config.is_paired_token(PAIRED))

    def test_a_token_is_paired_only_when_it_decodes_to_32_bytes_and_encodes_back(self):
        self.assertFalse(app_config.is_paired_token(PAIRED + "="))
        self.assertFalse(app_config.is_paired_token(PAIRED[:-1]))
        self.assertFalse(app_config.is_paired_token(PAIRED[:-1] + "9"))
        self.assertFalse(app_config.is_paired_token(""))
        self.assertFalse(app_config.is_paired_token(None))

    def test_the_key_id_is_the_first_16_bytes_of_hkdf_of_the_token(self):
        prk = hmac.new(b"beamer-link-v6", PAIRED.encode(), hashlib.sha256).digest()
        expected = hmac.new(prk, b"beamer-key-id" + b"\x01", hashlib.sha256).digest()[:16]
        self.assertEqual(app_config.key_id(PAIRED), expected)


class NeverPairedTests(Folder):
    legacy = {**LEGACY, "auth_token": ""}

    def test_an_empty_token_makes_no_peer_and_no_zone(self):
        settings = app_config.load_settings(self.path)
        self.assertEqual((settings["peers"], settings["zones"], settings["migrated_token_sha256"]), ([], [], ""))

    def test_the_app_sees_an_unpaired_config(self):
        with self.assertRaises(ConfigError):
            app_config.load_config(self.path)

    def test_an_unpaired_machine_can_still_save_its_own_settings(self):
        config = app_config.load_config(self.path, unpaired_ok=True)
        self.assertEqual(config.auth_token, "")
        app_config.save_config(self.path, replace(config, appearance="dark", pointer_speed=1.5, hide_addresses=True))
        settings = self.settings()
        self.assertEqual((settings["peers"], settings["zones"]), ([], []))
        self.assertEqual((settings["appearance"], settings["pointer_speed"], settings["hide_addresses"]), ("dark", 1.5, True))
        again = app_config.load_config(self.path, unpaired_ok=True)
        self.assertEqual((again.appearance, again.pointer_speed, again.hide_addresses), ("dark", 1.5, True))
        with self.assertRaises(ConfigError):
            app_config.load_config(self.path)

    def test_a_save_with_no_token_never_makes_a_peer_or_touches_one(self):
        paired = replace(app_config.default_config(), auth_token=PAIRED, mac_host="192.0.2.10", paired_with="Mac")
        app_config.save_config(self.path, paired)
        before = self.settings()["peers"]
        app_config.save_config(self.path, replace(app_config.default_config(), appearance="light"))
        after = self.settings()
        self.assertEqual(after["peers"], before)
        self.assertEqual(after["appearance"], "light")

    def test_pairing_then_keeps_the_machine_id_and_makes_the_peer(self):
        machine_id = app_config.load_settings(self.path)["machine_id"]
        config = replace(app_config.default_config(), auth_token=PAIRED, mac_host="192.0.2.10", paired_with="Mac")
        app_config.save_config(self.path, config)
        settings = self.settings()
        self.assertEqual(settings["machine_id"], machine_id)
        self.assertEqual((settings["peers"][0]["token"], settings["peers"][0]["host"], settings["peers"][0]["name"]),
                         (PAIRED, "192.0.2.10", "Mac"))
        self.assertEqual(app_config.load_config(self.path).machine_id, machine_id)


class NoOldFileTests(Folder):
    legacy = None

    def test_a_missing_config_gives_defaults_and_no_peer(self):
        settings = app_config.load_settings(self.path)
        self.assertEqual(settings["peers"], [])
        self.assertEqual(settings["schema"], 6)
        self.assertEqual(settings["port"], app_config.protocol.DEFAULT_PORT)
        self.assertTrue(self.path.exists())

    def test_an_unparseable_config_gives_defaults_and_no_crash(self):
        for text in ("{not json", "[1, 2]", "", "null"):
            with self.subTest(text=text):
                self.path.unlink(missing_ok=True)
                self.legacy_path.write_text(text, encoding="utf-8")
                self.assertEqual(app_config.load_settings(self.path)["peers"], [])

    def test_a_settings_file_that_does_not_parse_is_an_error_and_is_left_alone(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ConfigError):
            app_config.load_settings(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")

    def test_a_settings_file_for_another_schema_is_refused_and_left_alone(self):
        self.path.write_text(json.dumps({"schema": 7, "peers": []}), encoding="utf-8")
        with self.assertRaises(ConfigError):
            app_config.load_settings(self.path)


class ReadingAnOddFileTests(Folder):
    def test_a_file_the_app_cannot_use_is_a_settings_error_with_the_path_and_is_left_alone(self):
        app_config.load_settings(self.path)
        good = self.settings()
        for bad in ({**good, "schema": 7}, {**good, "peers": [1]}, {**good, "zones": ["edge"]}, {**good, "peers": {}}):
            with self.subTest(bad=bad):
                self.put(bad)
                raw = self.path.read_bytes()
                with self.assertRaises(app_config.SettingsFileError):
                    app_config.load_config(self.path)
                with self.assertRaises(app_config.SettingsFileError):
                    app_config.save_config(self.path, replace(app_config.default_config(), auth_token=PAIRED))
                self.assertEqual(self.path.read_bytes(), raw)
        self.assertTrue(issubclass(app_config.SettingsFileError, ConfigError))

    def test_a_zone_with_no_parts_or_a_bad_corner_reads_as_the_defaults(self):
        settings = app_config.load_settings(self.path)
        settings["zones"] = [{"peer": "", "kind": "part"}, {"peer": "", "kind": "corner", "corner": "middle"}]
        self.put(settings)
        config = app_config.load_config(self.path)
        self.assertEqual((config.crossing_edge_parts, config.crossing_corner), (["middle"], "top_left"))

    def test_a_config_that_cannot_be_read_is_not_taken_for_a_missing_one(self):
        real = Path.read_text

        def refuse(path, *args, **kwargs):
            if path.name == "config.json":
                raise PermissionError("locked")
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", refuse):
            with self.assertRaises(ConfigError):
                app_config.load_config(self.path)
        self.assertFalse(self.path.exists())

    def test_a_settings_path_named_config_json_is_refused_not_overwritten(self):
        with self.assertRaises(ConfigError):
            app_config.load_settings(self.folder / "config.json")
        self.assertEqual(json.loads(self.legacy_path.read_text(encoding="utf-8")), LEGACY)

    def test_fields_this_version_does_not_know_survive_a_save(self):
        settings = app_config.load_settings(self.path)
        settings["later"] = {"a": 1}
        settings["peers"][0]["later"] = 2
        settings["zones"][0]["later"] = 3
        self.put(settings)
        app_config.save_config(self.path, replace(app_config.load_config(self.path), pointer_speed=2.0))
        again = self.settings()
        self.assertEqual((again["later"], again["peers"][0]["later"], again["zones"][0]["later"]), ({"a": 1}, 2, 3))

    def test_the_machine_name_is_saved(self):
        app_config.load_settings(self.path)
        app_config.save_config(self.path, replace(app_config.load_config(self.path), name="Rig"))
        self.assertEqual(app_config.load_config(self.path).name, "Rig")


class OverlapTests(Folder):
    """Two zones in use never cover one stretch (WIRE.md section 8), as on the Mac; a corner inside an edge is fine."""

    def refused(self, change):
        settings = app_config.load_settings(self.path)
        change(settings)
        self.put(settings)
        raw = self.path.read_bytes()
        with self.assertRaises(app_config.SettingsFileError):
            app_config.load_config(self.path)
        with self.assertRaises(app_config.SettingsFileError):
            app_config.save_config(self.path, replace(app_config.default_config(), auth_token=PAIRED))
        self.assertEqual(self.path.read_bytes(), raw)

    def zone(self, settings, kind):
        return next(zone for zone in settings["zones"] if zone["kind"] == kind)

    def test_the_edge_and_its_thirds_in_use_together_are_refused(self):
        self.refused(lambda s: self.zone(s, "part").pop("off", None) or self.zone(s, "edge").pop("off", None))

    def test_two_part_zones_sharing_a_third_are_refused_and_disjoint_ones_are_not(self):
        def two_parts(s, second):
            self.zone(s, "edge")["off"] = True
            self.zone(s, "part").pop("off", None)
            self.zone(s, "part")["parts"] = ["start"]
            s["zones"].append({"peer": "", "kind": "part", "parts": second})
        self.refused(lambda s: two_parts(s, ["start", "end"]))
        self.path.unlink()
        settings = app_config.load_settings(self.path)
        two_parts(settings, ["end"])
        self.put(settings)
        app_config.load_config(self.path)

    def test_zones_turned_off_cover_nothing(self):
        settings = app_config.load_settings(self.path)
        settings["zones"].append({"peer": "", "kind": "edge", "off": True})
        self.put(settings)
        app_config.load_config(self.path)

    def test_two_peers_whose_edges_face_the_same_side_cannot_both_have_the_edge_zone_on(self):
        def second_peer(s):
            s["peers"][0]["id"] = PEER_ID
            s["zones"] = [{**zone, "peer": PEER_ID} for zone in s["zones"]]
            self.zone(s, "edge").pop("off", None)
            self.zone(s, "part")["off"] = True
            s["peers"].append({**s["peers"][0], "id": OTHER_ID, "token": OTHER, "from_1_4": False})
            s["zones"].append({"peer": OTHER_ID, "kind": "edge"})
        self.refused(second_peer)

    def test_a_corner_inside_the_edge_is_fine_and_two_of_one_corner_are_not(self):
        settings = app_config.load_settings(self.path)
        self.zone(settings, "edge").pop("off", None)
        self.zone(settings, "corner").pop("off", None)
        self.put(settings)
        app_config.load_config(self.path)
        self.refused(lambda s: s["zones"].append({**self.zone(s, "corner")}))


class ZonesFromMethodsTests(Folder):
    def migrated(self, **fields):
        self.path.unlink(missing_ok=True)
        self.write_legacy({**LEGACY, **fields})
        return app_config.load_settings(self.path)

    def test_every_combination_of_methods_gives_its_zones_and_off_flags(self):
        for count in range(5):
            for methods in itertools.combinations(app_config.METHODS, count):
                with self.subTest(methods=methods):
                    settings = self.migrated(crossing_methods=list(methods), crossing_corner="top_left",
                                             crossing_edge_parts=["middle", "end"], mac_return_edge="left")
                    self.assertIs(settings["shortcut"], "shortcut" in methods)
                    self.assertEqual([zone["kind"] for zone in settings["zones"]], ["edge", "part", "corner"])
                    # The whole edge covers its thirds, so with both on the thirds stand down (WIRE.md section 8).
                    in_use = [m for m in methods if not (m == "part" and "edge" in methods)]
                    for zone in settings["zones"]:
                        self.assertEqual(zone["peer"], "")
                        self.assertEqual(zone.get("off", False), zone["kind"] not in in_use, zone)
                    self.assertEqual(settings["zones"][1]["parts"], ["middle", "end"])
                    self.assertEqual(settings["zones"][2]["corner"], "top_left")

    def test_a_corner_crosses_the_side_as_1_4_x_did_whichever_edges_it_touches(self):
        # 1.4.x built the PC's corner push with the return edge (sender.py: CornerPush(corner, self._edge)).
        for corner, side in (("top_left", "left"), ("top_left", "top"), ("bottom_right", "bottom"),
                             ("top_right", "left"), ("bottom_left", "right"), ("top_left", "bottom"),
                             ("bottom_right", "top")):
            with self.subTest(corner=corner, side=side):
                settings = self.migrated(crossing_corner=corner, mac_return_edge=side)
                self.assertEqual(settings["zones"][2]["edge"], side)

    def test_a_corner_with_no_side_yet_crosses_its_own_left_or_right_edge(self):
        for corner, edge in (("top_left", "left"), ("bottom_right", "right")):
            with self.subTest(corner=corner):
                self.assertEqual(self.migrated(crossing_corner=corner, mac_return_edge="")["zones"][2]["edge"], edge)

    def test_there_is_no_notch_zone_on_windows(self):
        self.assertNotIn("notch", [zone["kind"] for zone in self.migrated()["zones"]])


class ConfigViewTests(Folder):
    def test_the_app_sees_the_same_config_as_before_but_for_what_the_spec_drops(self):
        config = app_config.load_config(self.path)
        expected = app_config.config_from_dict(dict(LEGACY))
        skipped = {"host", "mac_resistance_px", "machine_id", "name"}
        self.assertEqual({k: v for k, v in asdict(config).items() if k not in skipped},
                         {k: v for k, v in asdict(expected).items() if k not in skipped})

    def test_the_config_carries_the_machine_id_and_name(self):
        config = app_config.load_config(self.path)
        self.assertEqual(config.machine_id, self.settings()["machine_id"])
        self.assertEqual(config.name, "")

    def test_this_pcs_address_is_read_live_not_saved(self):
        with mock.patch.object(app_config, "local_address_towards", return_value="192.0.2.20") as towards:
            self.assertEqual(app_config.load_config(self.path).host, "192.0.2.20")
        towards.assert_called_once_with("192.0.2.10")
        with mock.patch.object(app_config, "local_address_towards", side_effect=OSError):
            self.assertEqual(app_config.load_config(self.path).host, "")
        self.assertNotIn("host", self.settings())

    def test_a_config_loaded_with_no_zones_names_no_ways_but_the_shortcut(self):
        settings = app_config.load_settings(self.path)
        settings["zones"] = []
        self.put(settings)
        self.assertEqual(app_config.load_config(self.path).crossing_methods, ["shortcut"])


class SavingTests(Folder):
    def setUp(self):
        super().setUp()
        self.config = app_config.load_config(self.path)

    def test_trigger_changes_cannot_overwrite_an_existing_jump_key(self):
        settings = app_config.load_settings(self.path)
        settings["peers"][0]["jump_key"] = "alt+f13"
        app_config.write_settings(self.path, settings)
        before = self.path.read_bytes()
        for key in ("alt_r", "f13"):
            with self.subTest(key=key), self.assertRaises(app_config.ConfigError):
                app_config.save_config(self.path, replace(self.config, trigger_key=key))
            self.assertEqual(self.path.read_bytes(), before)

    def test_a_save_round_trips_every_field_the_app_keeps(self):
        changed = replace(self.config, glow_style="glow", glow_colour="mono",
                          crossing_methods=["part", "shortcut"], crossing_edge_parts=["middle"],
                          crossing_corner="bottom_left", crossing_resistance_px=40, mac_return_edge="right",
                          send_to_mac=False, allow_mac_to_drive=False, paired_with="Studio", mac_host="192.0.2.11",
                          mac_hardware_address="11:22:33:44:55:66", modifier_style="semantic", hide_addresses=True)
        app_config.save_config(self.path, changed)
        again = app_config.load_config(self.path)
        # The ways across are each peer's, written by set_ways, never by a save of the flat view.
        ways = {"crossing_methods", "crossing_edge_parts", "crossing_corner", "mac_return_edge", "arrangement_set_at"}
        skipped = {"host", "mac_resistance_px"} | ways
        self.assertEqual({k: v for k, v in asdict(again).items() if k not in skipped},
                         {k: v for k, v in asdict(changed).items() if k not in skipped})
        self.assertEqual({k: getattr(again, k) for k in ways}, {k: getattr(self.config, k) for k in ways})

    def test_a_save_writes_the_shortcut_and_no_zone_and_not_the_old_names(self):
        before = self.settings()["zones"]
        app_config.save_config(self.path, replace(self.config, crossing_methods=["edge"], crossing_corner="bottom_left"))
        settings = self.settings()
        for key in MOVED_OR_DROPPED:
            self.assertNotIn(key, settings)
        self.assertIs(settings["shortcut"], False)
        self.assertEqual(settings["zones"], before)

    def test_turning_a_way_off_keeps_its_zone_and_its_setting(self):
        app_config.set_ways(self.path, "", methods=["edge"], parts=["start", "end"], corner="top_right")
        app_config.set_ways(self.path, "", methods=["part"], parts=["start", "end"], corner="top_right")
        settings = self.settings()
        self.assertEqual([(zone["kind"], zone.get("off", False)) for zone in settings["zones"]],
                         [("edge", True), ("part", False), ("corner", True)])
        again = app_config.load_config(self.path)
        self.assertEqual((again.crossing_methods, again.crossing_edge_parts), (["part", "shortcut"], ["start", "end"]))

    def test_the_machine_id_and_the_migrated_hash_never_change_in_a_save(self):
        before = self.settings()
        app_config.save_config(self.path, replace(self.config, pointer_speed=2.0))
        after = self.settings()
        self.assertEqual((after["machine_id"], after["migrated_token_sha256"], after["schema"]),
                         (before["machine_id"], before["migrated_token_sha256"], 6))
        app_config.save_config(self.path, replace(self.config, machine_id=OWN_ID))
        self.assertEqual(self.settings()["machine_id"], before["machine_id"])

    def test_this_pcs_address_and_the_macs_resistance_are_not_saved(self):
        app_config.save_config(self.path, replace(self.config, host="192.0.2.99", mac_resistance_px=77))
        settings = self.settings()
        self.assertNotIn("host", settings)
        self.assertNotIn("mac_resistance_px", settings)

    def test_a_side_is_stamped_by_this_machine_only_when_set_here_never_by_a_save(self):
        settings = self.settings()
        settings["peers"][0]["side_by"] = OTHER_ID
        self.put(settings)
        app_config.save_config(self.path, replace(self.config, pointer_speed=2.0, mac_return_edge="top",
                                                  arrangement_set_at=1790000005))
        self.assertEqual((self.settings()["peers"][0]["side"], self.settings()["peers"][0]["side_by"]), ("left", OTHER_ID))
        app_config.set_ways(self.path, "", side="top", methods=["edge"], parts=["middle"], corner="top_left")
        self.assertEqual((self.settings()["peers"][0]["side"], self.settings()["peers"][0]["side_by"]),
                         ("top", settings["machine_id"]))

    def test_a_changed_port_moves_this_machines_and_the_one_peers_together(self):
        app_config.save_config(self.path, replace(self.config, port=25000))
        settings = self.settings()
        self.assertEqual((settings["port"], settings["peers"][0]["port"]), (25000, 25000))

    def test_a_peers_own_port_is_left_alone_when_the_port_did_not_change(self):
        settings = self.settings()
        settings["peers"][0]["port"] = 24900
        self.put(settings)
        app_config.save_config(self.path, replace(app_config.load_config(self.path), pointer_speed=2.0))
        self.assertEqual(self.settings()["peers"][0]["port"], 24900)

    def test_a_new_pairing_replaces_the_first_peer_and_points_its_zones_at_the_empty_id(self):
        settings = self.settings()
        app_config.learn_peer_id(settings, 0, PEER_ID)
        self.put(settings)
        app_config.save_config(self.path, replace(app_config.load_config(self.path), auth_token=OTHER, paired_with="Other",
                                                  mac_host="192.0.2.30"))
        peer = self.settings()["peers"][0]
        self.assertEqual((peer["token"], peer["name"], peer["host"], peer["id"]), (OTHER, "Other", "192.0.2.30", ""))
        self.assertEqual((peer["linked"], peer["from_1_4"], peer["paired_at"], peer["paired_with"]), (False, True, 0, []))
        self.assertEqual({zone["peer"] for zone in self.settings()["zones"]}, {""})

    def test_other_peers_and_their_zones_survive_every_save(self):
        settings = self.settings()
        extra = {**MIGRATED_PEER, "id": OTHER_ID, "name": "Laptop", "platform": "windows", "token": THIRD,
                 "from_1_4": False, "linked": True, "side": "right", "side_by": OTHER_ID}
        settings["peers"].append(extra)
        settings["zones"].append({"peer": OTHER_ID, "kind": "edge"})
        self.put(settings)
        config = app_config.load_config(self.path)
        app_config.save_config(self.path, replace(config, crossing_methods=[], glow_style="glow"))
        app_config.save_config(self.path, replace(config, auth_token=OTHER))
        after = self.settings()
        self.assertEqual(after["peers"][1], extra)
        self.assertIn({"peer": OTHER_ID, "kind": "edge"}, after["zones"])
        self.assertEqual(len(after["peers"]), 2)

    def test_a_save_is_atomic_a_new_file_renamed_over_the_old(self):
        before = self.path.read_bytes()
        with mock.patch.object(app_config.os, "replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                app_config.save_config(self.path, replace(self.config, pointer_speed=2.0))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(entry.name for entry in self.folder.iterdir()), ["config.json", "settings.json"])


class LearningTheIdTests(Folder):
    def setUp(self):
        super().setUp()
        self.settings_ = app_config.load_settings(self.path)

    def test_the_first_link_fills_the_id_in_and_rewrites_the_zones(self):
        app_config.learn_peer_id(self.settings_, 0, PEER_ID)
        self.assertEqual(self.settings_["peers"][0]["id"], PEER_ID)
        self.assertEqual({zone["peer"] for zone in self.settings_["zones"]}, {PEER_ID})

    def test_zones_of_other_peers_are_not_touched(self):
        self.settings_["peers"].append({**MIGRATED_PEER, "id": OTHER_ID, "from_1_4": False, "token": THIRD})
        self.settings_["zones"].append({"peer": OTHER_ID, "kind": "edge"})
        app_config.learn_peer_id(self.settings_, 0, PEER_ID)
        self.assertEqual([zone["peer"] for zone in self.settings_["zones"]], [PEER_ID] * 3 + [OTHER_ID])

    def test_it_refuses_this_machines_own_id_a_duplicate_a_bad_one_and_an_entry_that_has_one(self):
        self.settings_["peers"].append({**MIGRATED_PEER, "id": OTHER_ID, "from_1_4": False, "token": THIRD})
        for bad in (self.settings_["machine_id"], OTHER_ID, bytes(16).hex(), b64(bytes(16)), PEER_ID + "="):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                app_config.learn_peer_id(self.settings_, 0, bad)
        app_config.learn_peer_id(self.settings_, 0, PEER_ID)
        with self.assertRaises(ConfigError):
            app_config.learn_peer_id(self.settings_, 0, b64(bytes([4]) * 16))
        self.assertEqual(self.settings_["peers"][0]["id"], PEER_ID)


class PairingAgainUnder1_4Tests(Folder):
    def start(self):
        return app_config.load_settings(self.path)

    def test_a_changed_token_replaces_the_migrated_entry_and_updates_the_hash(self):
        settings = self.start()
        app_config.learn_peer_id(settings, 0, PEER_ID)
        settings["peers"][0].update(linked=True, paired_with=[OWN_ID], paired_at=5)
        self.put(settings)
        self.write_legacy({**LEGACY, "auth_token": OTHER, "paired_with": "Studio", "mac_host": "192.0.2.40"})
        again = self.start()
        peer = again["peers"][0]
        self.assertEqual((peer["token"], peer["name"], peer["host"]), (OTHER, "Studio", "192.0.2.40"))
        self.assertEqual((peer["id"], peer["linked"], peer["paired_with"], peer["from_1_4"]), ("", False, [], True))
        self.assertEqual({zone["peer"] for zone in again["zones"]}, {""})
        self.assertEqual(again["migrated_token_sha256"], hashlib.sha256(OTHER.encode()).hexdigest())
        self.assertEqual(again["machine_id"], settings["machine_id"])
        self.assertEqual(self.settings(), again)

    def test_an_unchanged_token_does_nothing(self):
        self.start()
        raw = self.path.read_bytes()
        self.write_legacy({**LEGACY, "pointer_speed": 3.0})
        self.start()
        self.assertEqual(self.path.read_bytes(), raw)

    def test_peers_added_under_1_5_0_are_untouched(self):
        settings = self.start()
        extra = {**MIGRATED_PEER, "id": OTHER_ID, "name": "Laptop", "token": THIRD, "from_1_4": False, "side_by": "", "side": "bottom"}
        settings["peers"].append(extra)
        settings["zones"].append({"peer": OTHER_ID, "kind": "edge"})
        self.put(settings)
        self.write_legacy({**LEGACY, "auth_token": OTHER})
        again = self.start()
        self.assertEqual([peer["token"] for peer in again["peers"]], [OTHER, THIRD])
        self.assertEqual(again["peers"][1], extra)
        self.assertIn({"peer": OTHER_ID, "kind": "edge"}, again["zones"])

    def test_a_peer_the_user_removed_stays_removed_while_the_old_file_is_unchanged(self):
        settings = self.start()
        settings["peers"], settings["zones"] = [], []
        self.put(settings)
        for _ in range(3):
            self.assertEqual(self.start()["peers"], [])

    def test_a_removed_peer_comes_back_when_the_user_pairs_again_under_1_4_x(self):
        settings = self.start()
        settings["peers"], settings["zones"] = [], []
        self.put(settings)
        self.write_legacy({**LEGACY, "auth_token": OTHER})
        again = self.start()
        self.assertEqual([peer["token"] for peer in again["peers"]], [OTHER])
        self.assertEqual([zone["kind"] for zone in again["zones"]], ["edge", "part", "corner"])
        self.assertEqual({zone["peer"] for zone in again["zones"]}, {""})

    def test_a_token_whose_key_id_is_another_entrys_is_not_migrated_but_the_hash_moves_on(self):
        settings = self.start()
        settings["peers"].append({**MIGRATED_PEER, "id": OTHER_ID, "token": OTHER, "from_1_4": False, "side_by": ""})
        self.put(settings)
        self.write_legacy({**LEGACY, "auth_token": OTHER})
        again = self.start()
        self.assertEqual([peer["token"] for peer in again["peers"]], [PAIRED, OTHER])
        self.assertEqual(again["migrated_token_sha256"], hashlib.sha256(OTHER.encode()).hexdigest())

    def test_a_typed_token_is_migrated_even_beside_another_typed_one(self):
        self.write_legacy({**LEGACY, "auth_token": "typed one"})
        self.start()
        self.write_legacy({**LEGACY, "auth_token": "typed two"})
        self.assertEqual(self.start()["peers"][0]["token"], "typed two")

    def test_an_emptied_token_migrates_nothing(self):
        self.start()
        raw = self.path.read_bytes()
        self.write_legacy({**LEGACY, "auth_token": ""})
        self.start()
        self.assertEqual(self.path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
