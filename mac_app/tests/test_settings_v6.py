"""The Mac's settings.json (WIRE.md section 1) and the migration from 1.4.x's config.json.

Every test runs in a temporary folder: nothing here reads or writes the installed app's files.
"""

import base64
import copy
import hashlib
import importlib
import itertools
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config as config_module
import settings_store
from core import protocol, receiver, ways
from settings_store import SettingsError, SettingsStore, config_to_raw

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

TOKEN = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
OTHER_TOKEN = base64.urlsafe_b64encode(bytes(range(100, 132))).decode("ascii").rstrip("=")
THIRD_TOKEN = base64.urlsafe_b64encode(bytes(range(200, 232))).decode("ascii").rstrip("=")
PEER_ID = base64.urlsafe_b64encode(bytes(range(1, 17))).decode("ascii").rstrip("=")
OTHER_ID_FOR_TIE = base64.urlsafe_b64encode(bytes(range(60, 76))).decode("ascii").rstrip("=")
SECOND_ID = base64.urlsafe_b64encode(bytes(range(21, 37))).decode("ascii").rstrip("=")


def legacy_1_4_3():
    """1.4.3's config.json in the shape a real install writes, with the token replaced."""
    return {
        "host": "192.0.2.20",
        "port": 24820,
        "auth_token": TOKEN,
        "trigger_key": "alt_r",
        "double_tap_ms": 300,
        "key_map": "semantic",
        "reconnect_interval_s": 2.0,
        "trigger_style": "double_tap",
        "crossing": {
            "methods": ["shortcut", "edge", "corner"],
            "edge": "left",
            "corner": "top_right",
            "resistance_px": 140,
            "haptics": True,
            "glow": True,
            "notch_style": "beam",
            "notch_after_ms": 1200,
            "haptic_feel": "medium",
            "haptic_steps": "quarters",
            "glow_style": "glow",
            "glow_colour": "signal",
            "block_while_dragging": True,
            "edge_parts": ["start", "end"],
            "shortcut_arrival": True,
            "shortcut_arrival_style": "match",
            "effect_length": "normal",
            "arrangement_set_at": 1790000000,
        },
        "pc_name": "Office PC",
        "mac_address": "02:1a:2b:3c:0d:4e",
        "send_to_windows": True,
        "allow_windows_to_drive": False,
        "check_updates": True,
        "hide_addresses": False,
        "pointer_speed": 1.5,
        "scroll_speed": 1.0,
        "reverse_scroll": False,
        "appearance": "dark",
        "ignored_inputs": [],
    }


def legacy_before_milestone_2():
    raw = legacy_1_4_3()
    raw["same_on_both"] = True
    raw["same_set_at"] = 1790000100
    return raw


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.legacy = self.dir / "config.json"
        self.path = self.dir / "settings.json"
        self.store = SettingsStore(self.path)

    def write_legacy(self, raw):
        self.legacy.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        os.utime(self.legacy, (1_700_000_000, 1_700_000_000))

    def settings(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write_settings(self, settings):
        self.path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


class MigrationTests(Base):
    def test_the_macs_1_4_3_config_becomes_the_settings_the_tables_say(self):
        self.write_legacy(legacy_1_4_3())
        cfg = self.store.load()
        settings = self.settings()
        self.assertEqual(settings["schema"], 6)
        self.assertEqual(settings["port"], 24820)
        self.assertEqual(settings["name"], "")
        self.assertTrue(settings["shortcut"])
        self.assertEqual(settings["migrated_token_sha256"], sha(TOKEN))
        machine_id = settings["machine_id"]
        self.assertEqual(len(base64.urlsafe_b64decode(machine_id + "==")), 16)
        self.assertEqual(settings["peers"], [{
            "id": "",
            "name": "Office PC",
            "platform": "windows",
            "token": TOKEN,
            "host": "192.0.2.20",
            "port": 24820,
            "hw": "02:1a:2b:3c:0d:4e",
            "send": True,
            "allow_drive": False,
            "in_use": True,
            "side": "left",
            "side_set_at": 1790000000,
            "side_by": machine_id,
            "paired_with": [],
            "paired_at": 0,
            "linked": False,
            "from_1_4": True,
            "jump_key": "",
        }])
        self.assertEqual(cfg.host, "192.0.2.20")
        self.assertEqual(cfg.port, 24820)
        self.assertEqual(cfg.auth_token, TOKEN)
        self.assertEqual(cfg.pc_name, "Office PC")
        self.assertEqual(cfg.mac_address, "02:1a:2b:3c:0d:4e")
        self.assertTrue(cfg.send_to_windows)
        self.assertFalse(cfg.allow_windows_to_drive)
        self.assertEqual(cfg.crossing["methods"], ["shortcut", "edge", "corner"])
        self.assertEqual(cfg.crossing["edge"], "left")
        self.assertEqual(cfg.crossing["edge_parts"], ["start", "end"])
        self.assertEqual(cfg.crossing["corner"], "top_right")
        self.assertEqual(cfg.crossing["arrangement_set_at"], 1790000000)
        self.assertEqual(cfg.crossing["resistance_px"], 140)
        self.assertEqual(cfg.pointer_speed, 1.5)
        self.assertEqual(cfg.appearance, "dark")

    def test_every_other_field_keeps_its_1_4_name_and_the_moved_ones_leave_crossing(self):
        self.write_legacy(legacy_1_4_3())
        self.store.load()
        settings = self.settings()
        for name in ("trigger_key", "double_tap_ms", "key_map", "reconnect_interval_s", "trigger_style",
                     "check_updates", "hide_addresses", "pointer_speed", "scroll_speed", "reverse_scroll",
                     "appearance", "ignored_inputs"):
            self.assertIn(name, settings)
        for name in ("host", "auth_token", "pc_name", "mac_address", "send_to_windows", "allow_windows_to_drive"):
            self.assertNotIn(name, settings)
        for name in ("methods", "edge", "edge_parts", "corner", "arrangement_set_at"):
            self.assertNotIn(name, settings["crossing"])
        self.assertEqual(settings["crossing"]["resistance_px"], 140)

    def test_zones_of_the_migrated_entry_name_the_empty_id(self):
        self.write_legacy(legacy_1_4_3())
        self.store.load()
        self.assertEqual(self.settings()["zones"], [
            {"peer": "", "kind": "edge"},
            {"peer": "", "kind": "part", "parts": ["start", "end"], "off": True},
            {"peer": "", "kind": "corner", "corner": "top_right", "edge": "right"},
            {"peer": "", "kind": "notch", "off": True},
        ])

    def test_a_config_from_main_before_milestone_2_migrates_too(self):
        self.write_legacy(legacy_before_milestone_2())
        cfg = self.store.load()
        self.assertTrue(cfg.same_on_both)
        self.assertEqual(cfg.same_set_at, 1790000100)
        self.assertTrue(self.settings()["same_on_both"])

    def test_never_paired_has_no_peer_and_keeps_the_crossing_choices(self):
        raw = legacy_1_4_3()
        raw.update(auth_token="", host="", pc_name="", mac_address="")
        self.write_legacy(raw)
        cfg = self.store.load()
        settings = self.settings()
        self.assertEqual(settings["peers"], [])
        self.assertEqual(settings["zones"], [])
        self.assertEqual(settings["migrated_token_sha256"], "")
        self.assertEqual(cfg.auth_token, "")
        self.assertEqual(cfg.crossing["methods"], ["shortcut", "edge", "corner"])
        self.assertEqual(cfg.crossing["edge"], "left")
        self.assertEqual(cfg.crossing["edge_parts"], ["start", "end"])

    def test_missing_and_unparseable_config_give_defaults_and_no_peer(self):
        cfg = self.store.load()
        self.assertEqual(cfg.auth_token, "")
        self.assertEqual(self.settings()["peers"], [])
        self.path.unlink()
        self.legacy.write_text("{not json", encoding="utf-8")
        cfg = self.store.load()
        self.assertEqual(cfg.auth_token, "")
        self.assertEqual(self.settings()["peers"], [])
        self.assertEqual(self.legacy.read_text(encoding="utf-8"), "{not json")

    def test_a_second_start_leaves_settings_byte_for_byte_the_same(self):
        self.write_legacy(legacy_1_4_3())
        self.store.load()
        first = self.path.read_bytes()
        stamp = self.path.stat().st_mtime_ns
        SettingsStore(self.path).load()
        self.assertEqual(self.path.read_bytes(), first)
        self.assertEqual(self.path.stat().st_mtime_ns, stamp)

    def test_config_json_is_never_written_again(self):
        self.write_legacy(legacy_1_4_3())
        before = (self.legacy.read_bytes(), self.legacy.stat().st_mtime_ns)
        cfg = self.store.load()
        for change in ({"hide_addresses": True}, {"pointer_speed": 2.0}, {"appearance": "light"}):
            cfg = self.store.save(config_to_raw(replace(cfg, **change)))
        self.assertEqual((self.legacy.read_bytes(), self.legacy.stat().st_mtime_ns), before)

    def test_a_failure_before_the_rename_leaves_no_settings_and_the_next_start_migrates(self):
        self.write_legacy(legacy_1_4_3())
        with mock.patch("settings_store.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(SettingsError):
                self.store.load()
        self.assertFalse(self.path.exists())
        self.assertEqual([p.name for p in self.dir.iterdir()], ["config.json"])
        cfg = self.store.load()
        self.assertEqual(cfg.auth_token, TOKEN)
        self.assertTrue(self.path.exists())

    def test_1_4_3s_own_loader_reads_the_kept_config_and_gets_the_pairing_back(self):
        self.write_legacy(legacy_1_4_3())
        cfg = self.store.load()
        self.store.save(config_to_raw(replace(cfg, hide_addresses=True)))
        sys.path.insert(0, FIXTURES)
        self.addCleanup(sys.path.remove, FIXTURES)
        for name in ("config_1_4_3", "settings_store_1_4_3"):
            sys.modules.pop(name, None)
        old_store = importlib.import_module("settings_store_1_4_3")
        self.addCleanup(sys.modules.pop, "config_1_4_3", None)
        self.addCleanup(sys.modules.pop, "settings_store_1_4_3", None)
        old = old_store.SettingsStore(self.legacy).load()
        self.assertEqual(old.host, "192.0.2.20")
        self.assertEqual(old.auth_token, TOKEN)
        self.assertEqual(old.pc_name, "Office PC")
        self.assertEqual(old.mac_address, "02:1a:2b:3c:0d:4e")
        self.assertEqual(old.crossing["methods"], ["shortcut", "edge", "corner"])
        self.assertEqual(old.crossing["edge"], "left")
        self.assertFalse(old.hide_addresses)

    def test_the_saved_key_style_survives(self):
        for style in ("semantic", "positional", {"alt": "alt", "cmd": "cmd"}):
            with self.subTest(style=style):
                self.path.unlink(missing_ok=True)
                raw = legacy_1_4_3()
                raw["key_map"] = style
                self.write_legacy(raw)
                cfg = self.store.load()
                again = self.store.save(config_to_raw(cfg))
                self.assertEqual(again.key_map, cfg.key_map)
                self.assertEqual(SettingsStore(self.path).load().key_map, cfg.key_map)
                saved = self.settings()["key_map"]
                self.assertEqual(saved, style if isinstance(style, str) else {**config_module.DEFAULT_KEY_MAP, **style})

    def test_a_typed_token_migrates_and_is_not_a_pairing_token(self):
        raw = legacy_1_4_3()
        raw["auth_token"] = "correct horse battery"
        self.write_legacy(raw)
        cfg = self.store.load()
        self.assertEqual(cfg.auth_token, "correct horse battery")
        self.assertEqual(self.settings()["peers"][0]["token"], "correct horse battery")
        self.assertFalse(protocol.is_paired_token("correct horse battery"))
        self.assertFalse(protocol.is_paired_token(TOKEN[:-1]))
        self.assertFalse(protocol.is_paired_token(TOKEN[:-1] + "="))
        self.assertTrue(protocol.is_paired_token(TOKEN))

    def test_side_by_is_empty_when_the_arrangement_was_never_changed(self):
        raw = legacy_1_4_3()
        raw["crossing"]["arrangement_set_at"] = 0
        self.write_legacy(raw)
        self.store.load()
        peer = self.settings()["peers"][0]
        self.assertEqual((peer["side_set_at"], peer["side_by"]), (0, ""))

    def test_the_legacy_default_port_moves_as_it_always_did(self):
        raw = legacy_1_4_3()
        raw["port"] = 51820
        self.write_legacy(raw)
        self.assertEqual(self.store.load().port, 24820)


class ZonesFromMethodsTests(Base):
    def test_each_combination_of_methods_gives_its_zones_and_off_flags(self):
        for count in range(6):
            for methods in itertools.combinations(("shortcut", "edge", "part", "corner", "notch"), count):
                with self.subTest(methods=methods):
                    self.path.unlink(missing_ok=True)
                    raw = legacy_1_4_3()
                    raw["crossing"]["methods"] = list(methods)
                    self.write_legacy(raw)
                    cfg = self.store.load()
                    settings = self.settings()
                    self.assertEqual(settings["shortcut"], "shortcut" in methods)
                    zones = {zone["kind"]: zone for zone in settings["zones"]}
                    self.assertEqual(list(zones), ["edge", "part", "corner", "notch"])
                    # The whole edge covers its thirds, so with both on the thirds stand down (WIRE.md section 8).
                    in_use = [m for m in methods if not (m == "part" and "edge" in methods)]
                    for kind in ("edge", "part", "corner", "notch"):
                        self.assertEqual(zones[kind].get("off", False), kind not in in_use)
                    self.assertEqual(cfg.crossing["methods"], in_use)

    def test_a_corner_crosses_by_its_own_left_or_right_edge_whatever_the_side_as_1_4_x_did(self):
        # crossing.py: the Mac's corner crossing returns the corner's horizontal edge; only the way home uses the side.
        for corner, edge in (("top_right", "right"), ("top_left", "left"), ("bottom_left", "left"), ("bottom_right", "right")):
            for side in ("left", "right", "top", "bottom"):
                with self.subTest(corner=corner, side=side):
                    self.path.unlink(missing_ok=True)
                    raw = legacy_1_4_3()
                    raw["crossing"].update(corner=corner, edge=side)
                    self.write_legacy(raw)
                    self.store.load()
                    zone = [z for z in self.settings()["zones"] if z["kind"] == "corner"][0]
                    self.assertEqual(zone["edge"], edge)


class ReimportTests(Base):
    def migrated(self):
        self.write_legacy(legacy_1_4_3())
        self.store.load()

    def test_a_changed_token_replaces_the_1_4_entry_and_points_its_zones_at_the_empty_id(self):
        self.migrated()
        settings = self.settings()
        settings["peers"][0].update(id=PEER_ID, linked=True, paired_with=[SECOND_ID], paired_at=5)
        for zone in settings["zones"]:
            zone["peer"] = PEER_ID
        self.write_settings(settings)
        raw = legacy_1_4_3()
        raw.update(auth_token=OTHER_TOKEN, host="192.0.2.77", pc_name="New PC")
        self.write_legacy(raw)
        cfg = self.store.load()
        settings = self.settings()
        peer = settings["peers"][0]
        self.assertEqual((peer["id"], peer["linked"], peer["paired_with"]), ("", False, []))
        self.assertEqual((peer["token"], peer["host"], peer["name"], peer["from_1_4"]), (OTHER_TOKEN, "192.0.2.77", "New PC", True))
        self.assertEqual({zone["peer"] for zone in settings["zones"]}, {""})
        self.assertEqual(settings["migrated_token_sha256"], sha(OTHER_TOKEN))
        self.assertEqual((cfg.auth_token, cfg.host), (OTHER_TOKEN, "192.0.2.77"))

    def test_an_unchanged_token_does_nothing_and_peers_added_under_1_5_are_untouched(self):
        self.migrated()
        settings = self.settings()
        added = copy.deepcopy(settings["peers"][0])
        added.update(id=SECOND_ID, token=THIRD_TOKEN, from_1_4=False, name="Laptop")
        settings["peers"].append(added)
        self.write_settings(settings)
        before = self.path.read_bytes()
        self.store.load()
        self.assertEqual(self.path.read_bytes(), before)
        raw = legacy_1_4_3()
        raw["auth_token"] = OTHER_TOKEN
        self.write_legacy(raw)
        self.store.load()
        peers = self.settings()["peers"]
        self.assertEqual(peers[1], added)
        self.assertEqual(peers[0]["token"], OTHER_TOKEN)

    def test_a_migrated_peer_the_user_removed_stays_removed_while_config_is_unchanged(self):
        self.migrated()
        settings = self.settings()
        settings["peers"] = []
        settings["zones"] = []
        self.write_settings(settings)
        for _ in range(3):
            SettingsStore(self.path).load()
        self.assertEqual(self.settings()["peers"], [])

    def test_a_removed_peer_paired_again_under_1_4_comes_back_with_its_zones(self):
        self.migrated()
        settings = self.settings()
        settings["peers"] = []
        settings["zones"] = []
        self.write_settings(settings)
        raw = legacy_1_4_3()
        raw["auth_token"] = OTHER_TOKEN
        self.write_legacy(raw)
        self.store.load()
        settings = self.settings()
        self.assertEqual(len(settings["peers"]), 1)
        self.assertEqual(settings["peers"][0]["token"], OTHER_TOKEN)
        self.assertEqual([zone["kind"] for zone in settings["zones"]], ["edge", "part", "corner", "notch"])

    def test_a_token_whose_key_id_is_another_entrys_is_not_migrated_and_is_not_tried_again(self):
        self.migrated()
        settings = self.settings()
        added = copy.deepcopy(settings["peers"][0])
        added.update(id=SECOND_ID, token=OTHER_TOKEN, from_1_4=False, name="Laptop")
        settings["peers"].append(added)
        self.write_settings(settings)
        raw = legacy_1_4_3()
        raw["auth_token"] = OTHER_TOKEN
        self.write_legacy(raw)
        self.store.load()
        settings = self.settings()
        self.assertEqual(settings["peers"][0]["token"], TOKEN)
        self.assertEqual(len(settings["peers"]), 2)
        self.assertEqual(settings["migrated_token_sha256"], sha(OTHER_TOKEN))

    def test_a_typed_token_is_migrated_even_though_it_has_no_key_id(self):
        self.migrated()
        raw = legacy_1_4_3()
        raw["auth_token"] = "swordfish"
        self.write_legacy(raw)
        self.store.load()
        self.assertEqual(self.settings()["peers"][0]["token"], "swordfish")

    def test_an_emptied_token_is_not_a_migration(self):
        self.migrated()
        raw = legacy_1_4_3()
        raw["auth_token"] = ""
        self.write_legacy(raw)
        before = self.path.read_bytes()
        self.store.load()
        self.assertEqual(self.path.read_bytes(), before)


class SavingTests(Base):
    def setUp(self):
        super().setUp()
        self.write_legacy(legacy_1_4_3())
        self.cfg = self.store.load()

    def test_saving_keeps_the_machine_id_and_the_id_of_the_peer_and_writes_privately(self):
        settings = self.settings()
        settings["peers"][0].update(id=PEER_ID, linked=True)
        for zone in settings["zones"]:
            zone["peer"] = PEER_ID
        self.write_settings(settings)
        machine_id = settings["machine_id"]
        saved = self.store.save(config_to_raw(replace(self.cfg, hide_addresses=True)))
        self.assertEqual(saved, self.store.load())
        after = self.settings()
        self.assertEqual(after["machine_id"], machine_id)
        self.assertEqual(after["peers"][0]["id"], PEER_ID)
        self.assertTrue(after["peers"][0]["linked"])
        self.assertEqual({zone["peer"] for zone in after["zones"]}, {PEER_ID})
        self.assertTrue(after["hide_addresses"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.dir.stat().st_mode & 0o777, 0o700)

    def test_the_save_that_learns_the_peers_id_rewrites_the_zones_naming_the_empty_id(self):
        settings = self.settings()
        settings["peers"][0]["id"] = PEER_ID
        self.store.save_settings(settings)
        self.assertEqual({zone["peer"] for zone in self.settings()["zones"]}, {PEER_ID})

    def test_saving_the_flat_config_updates_peer_zero_and_leaves_the_other_peers_alone(self):
        settings = self.settings()
        added = copy.deepcopy(settings["peers"][0])
        added.update(id=SECOND_ID, token=THIRD_TOKEN, from_1_4=False, name="Laptop", host="192.0.2.30")
        settings["peers"].append(added)
        self.write_settings(settings)
        raw = config_to_raw(self.cfg)
        raw.update(host="192.0.2.99", pc_name="Renamed", send_to_windows=False)
        raw["crossing"]["edge"] = "bottom"
        raw["crossing"]["arrangement_set_at"] = 1790000500
        cfg = self.store.save(raw)
        after = self.settings()
        self.assertEqual(after["peers"][1], added)
        first = after["peers"][0]
        self.assertEqual((first["host"], first["name"], first["send"]), ("192.0.2.99", "Renamed", False))
        # The side is written by set_ways and arrangement only: a flat view read before another
        # machine's arrangement landed must not put the old side back.
        self.assertEqual((first["side"], first["side_set_at"]), ("left", 1790000000))
        self.assertEqual((cfg.host, cfg.crossing["edge"]), ("192.0.2.99", "left"))

    def test_a_flat_save_keeps_a_name_and_hardware_address_learnt_since_the_window_read(self):
        settings = self.settings()
        settings["peers"][0].update(name="Learnt PC", hw="aa:bb:cc:dd:ee:01")
        self.store.save_settings(settings)
        self.store.save(config_to_raw(replace(self.cfg, pointer_speed=2.0)))
        first = self.settings()["peers"][0]
        self.assertEqual((first["name"], first["hw"]), ("Learnt PC", "aa:bb:cc:dd:ee:01"))
        self.assertEqual(self.settings()["pointer_speed"], 2.0)

    def test_a_new_address_clears_what_was_learnt_at_the_old_one(self):
        settings = self.settings()
        settings["peers"][0].update(name="Learnt PC", hw="aa:bb:cc:dd:ee:01")
        self.store.save_settings(settings)
        self.store.save(config_to_raw(replace(self.cfg, host="192.0.2.99", pc_name="", mac_address="")))
        first = self.settings()["peers"][0]
        self.assertEqual((first["host"], first["name"], first["hw"]), ("192.0.2.99", "", ""))

    def with_id(self):
        settings = self.settings()
        settings["peers"][0]["id"] = PEER_ID
        for zone in settings["zones"]:
            zone["peer"] = PEER_ID
        self.write_settings(settings)
        self.store = SettingsStore(self.path)
        return settings

    def test_the_side_stamp_keeps_its_author_until_it_changes(self):
        settings = self.with_id()
        settings["peers"][0].update(side_by=SECOND_ID)
        self.write_settings(settings)
        self.store = SettingsStore(self.path)
        self.store.save(config_to_raw(self.cfg))
        self.assertEqual(self.settings()["peers"][0]["side_by"], SECOND_ID)
        self.assertFalse(self.store.set_ways(PEER_ID, side="left", methods=["edge"], parts=["middle"], corner="top_right"))
        self.assertEqual(self.settings()["peers"][0]["side_by"], SECOND_ID)
        self.assertTrue(self.store.set_ways(PEER_ID, side="top", methods=["edge"], parts=["middle"], corner="top_right"))
        first = self.settings()["peers"][0]
        self.assertEqual((first["side"], first["side_by"]), ("top", self.settings()["machine_id"]))
        self.assertGreater(first["side_set_at"], 1790000000)

    def test_an_arrangement_from_the_peer_is_stamped_with_the_peers_id_not_this_machines(self):
        self.with_id()
        self.assertEqual(self.store.arrangement(PEER_ID, "top", 1790000500, SECOND_ID), (True, []))
        first = self.settings()["peers"][0]
        self.assertEqual((first["side"], first["side_set_at"], first["side_by"]), ("bottom", 1790000500, SECOND_ID))

    def test_a_tie_won_on_the_larger_by_stores_the_winners_by(self):
        settings = self.with_id()
        stamp = settings["peers"][0]["side_set_at"]
        settings["peers"][0].update(side_by=OTHER_ID_FOR_TIE)
        self.write_settings(settings)
        self.store = SettingsStore(self.path)
        self.assertEqual(self.store.arrangement(PEER_ID, "left", stamp, SECOND_ID), (False, []))
        self.assertEqual(self.store.arrangement(PEER_ID, "left", stamp, OTHER_ID_FOR_TIE), (False, []))
        larger = base64.urlsafe_b64encode(bytes(range(200, 216))).decode("ascii").rstrip("=")
        self.assertEqual(self.store.arrangement(PEER_ID, "left", stamp, larger), (True, []))
        self.assertEqual(self.settings()["peers"][0]["side_by"], larger)
        self.assertEqual(self.settings()["peers"][0]["side_set_at"], stamp)

    def test_an_arrangement_for_one_machine_leaves_the_others_side_alone(self):
        settings = self.with_id()
        second = {**copy.deepcopy(settings["peers"][0]), "id": SECOND_ID, "side": "right", "name": "Second"}
        second["token"] = second["token"][::-1]
        settings["peers"].append(second)
        self.write_settings(settings)
        self.store = SettingsStore(self.path)
        self.store.arrangement(SECOND_ID, "right", 1790000900, SECOND_ID)
        after = self.settings()["peers"]
        self.assertEqual((after[0]["side"], after[1]["side"]), ("left", "left"))

    def test_the_store_serves_the_settings_it_holds_to_a_peer_book_and_keeps_them_after_a_save(self):
        book = self.store.book()
        self.assertEqual(book.peers()[0]["token"], TOKEN)
        raw = config_to_raw(self.cfg)
        raw["host"] = "192.0.2.98"
        self.store.save(raw)
        self.assertEqual(book.peers()[0]["host"], "192.0.2.98")
        # The migrated entry never links; as a pairing made on 1.5.0 it does, and what it learns is kept.
        own = protocol.read_id(self.settings()["machine_id"])
        self.assertEqual(book.admit(protocol.key_id(TOKEN), own, protocol.read_id(PEER_ID), {"name": "Learnt"})[0], receiver.GONE)
        settings = self.store.current()
        settings["peers"][0].update(id=PEER_ID, from_1_4=False)
        self.store.save_settings(settings)
        self.assertEqual(book.admit(protocol.key_id(TOKEN), own, protocol.read_id(PEER_ID), {"name": "Learnt"})[0], receiver.ADMITTED)
        self.assertEqual(self.settings()["peers"][0]["name"], "Learnt")
        self.assertEqual(self.store.current()["peers"][0]["name"], "Learnt")

    def test_changing_the_methods_turns_zones_on_and_off(self):
        self.with_id()
        raw = config_to_raw(self.cfg)
        raw["crossing"]["methods"] = ["edge", "corner"]
        self.store.save(raw)
        self.assertFalse(self.settings()["shortcut"])
        self.store.set_ways(PEER_ID, side="left", methods=["part", "notch"], parts=["middle"], corner="top_right")
        zones = {zone["kind"]: zone for zone in self.settings()["zones"]}
        self.assertTrue(zones["edge"]["off"])
        self.assertNotIn("off", zones["part"])
        self.assertEqual(zones["part"]["parts"], ["middle"])
        self.assertNotIn("off", zones["notch"])
        self.assertEqual(self.store.load().crossing["methods"], ["part", "notch"])

    def test_the_corner_zones_edge_follows_the_corner_not_the_side(self):
        self.with_id()
        self.store.set_ways(PEER_ID, side="top", methods=["corner"], parts=["middle"], corner="top_right")
        corner = [z for z in self.settings()["zones"] if z["kind"] == "corner"][0]
        self.assertEqual((corner["corner"], corner["edge"]), ("top_right", "right"))
        self.store.set_ways(PEER_ID, side="top", methods=["corner"], parts=["middle"], corner="bottom_left")
        corner = [z for z in self.settings()["zones"] if z["kind"] == "corner"][0]
        self.assertEqual((corner["corner"], corner["edge"]), ("bottom_left", "left"))

    def test_ways_that_would_clash_with_another_machines_are_refused_with_both_named_and_nothing_written(self):
        settings = self.with_id()
        second = {**copy.deepcopy(settings["peers"][0]), "id": SECOND_ID, "side": "right", "name": "Second"}
        second["token"] = second["token"][::-1]
        settings["peers"].append(second)
        settings["zones"].append({"peer": SECOND_ID, "kind": "edge"})
        self.write_settings(settings)
        self.store = SettingsStore(self.path)
        # On the same side as Second, with no way in yet: turning its edge on would cover Second's.
        self.store.set_ways(PEER_ID, side="right", methods=[], parts=["middle"], corner="top_right")
        before = self.settings()
        with self.assertRaises(SettingsError) as raised:
            self.store.set_ways(PEER_ID, side="right", methods=["edge"], parts=["middle"], corner="top_right")
        self.assertIn("Second", str(raised.exception))
        self.assertIn("right edge of this Mac", str(raised.exception))
        self.assertEqual(self.settings(), before)

    def second_machine(self, side, zones=None):
        settings = self.with_id()
        second = {**copy.deepcopy(settings["peers"][0]), "id": SECOND_ID, "side": side, "name": "Second"}
        second["token"] = second["token"][::-1]
        settings["peers"].append(second)
        if zones is not None:
            settings["zones"] += zones
        self.write_settings(settings)
        self.store = SettingsStore(self.path)

    def test_a_machine_paired_with_no_zone_gets_its_edge_when_the_settings_are_read(self):
        # The rig's beta.3 file of 01-10-2026: a second machine's side set, and no zone to cross by.
        self.second_machine("right")
        self.assertIn({"peer": SECOND_ID, "kind": "edge"}, self.store.current()["zones"])

    def test_one_whose_side_is_taken_gets_its_edge_off_and_the_settings_still_read(self):
        self.second_machine("left")
        self.assertIn({"peer": SECOND_ID, "kind": "edge", "off": True}, self.store.current()["zones"])
        self.assertIn("Second has no way in", ways.blocked_sentence(self.store.current(), SECOND_ID, "this Mac"))

    def test_moving_a_machine_onto_a_taken_side_keeps_the_side_and_turns_its_way_there_off(self):
        self.second_machine("right", [{"peer": SECOND_ID, "kind": "edge"}])
        self.assertTrue(self.store.set_ways(SECOND_ID, side="left", methods=["edge"], parts=["middle"], corner="top_right"))
        second = next(entry for entry in self.settings()["peers"] if entry["id"] == SECOND_ID)
        self.assertEqual(second["side"], "left")
        self.assertIn({"peer": SECOND_ID, "kind": "edge", "off": True}, self.settings()["zones"])

    def test_ways_for_a_machine_no_longer_paired_are_refused(self):
        with self.assertRaises(SettingsError):
            self.store.set_ways(SECOND_ID, side="right", methods=["edge"], parts=["middle"], corner="top_right")

    def test_the_first_machines_port_is_its_own_and_never_moves_this_macs(self):
        raw = config_to_raw(self.cfg)
        raw["port"] = 9000
        self.store.save(raw)
        settings = self.settings()
        self.assertEqual((settings["port"], settings["peers"][0]["port"]), (24820, 9000))

    def test_removing_or_adding_machines_leaves_this_macs_port_where_it_was(self):
        self.store.add_peer({**settings_store.PEER_DEFAULTS, "id": SECOND_ID, "name": "Laptop", "platform": "macos",
                             "token": OTHER_TOKEN, "host": "192.0.2.30", "port": 9100, "paired_at": 1_700_000_000})
        self.store.remove_peer(TOKEN)
        self.assertEqual(self.store.load().port, 9100)
        self.assertEqual(self.settings()["port"], 24820)
        self.store.save(config_to_raw(self.store.load()))
        self.assertEqual(self.settings()["port"], 24820)

    def test_clearing_the_token_removes_the_peer_and_its_zones_and_keeps_the_choices(self):
        raw = config_to_raw(self.cfg)
        raw.update(auth_token="", host="", pc_name="", mac_address="")
        cfg = self.store.save(raw)
        settings = self.settings()
        self.assertEqual((settings["peers"], settings["zones"]), ([], []))
        self.assertEqual(cfg.crossing["methods"], ["shortcut", "edge", "corner"])
        self.assertEqual(SettingsStore(self.path).load().crossing["edge_parts"], ["start", "end"])

    def test_pairing_again_after_clearing_makes_a_peer_with_zones_from_the_choices(self):
        raw = config_to_raw(self.cfg)
        raw.update(auth_token="", host="", pc_name="", mac_address="")
        self.store.save(raw)
        raw.update(auth_token=OTHER_TOKEN, host="192.0.2.50", pc_name="Desk")
        self.store.save(raw)
        settings = self.settings()
        peer = settings["peers"][0]
        self.assertEqual((peer["id"], peer["platform"], peer["token"], peer["from_1_4"], peer["linked"]),
                         ("", "windows", OTHER_TOKEN, True, False))
        self.assertEqual(peer["side"], "left")
        self.assertEqual([zone["kind"] for zone in settings["zones"]], ["edge", "part", "corner", "notch"])
        self.assertEqual(settings["migrated_token_sha256"], sha(TOKEN))

    def test_a_first_save_with_nothing_on_disk_makes_a_machine(self):
        self.path.unlink()
        self.legacy.unlink()
        cfg = SettingsStore(self.path).save(config_to_raw(settings_store.editable_default_config()))
        self.assertEqual(cfg.auth_token, "")
        self.assertEqual(self.settings()["schema"], 6)
        self.assertTrue(self.settings()["machine_id"])


class PairedAndRemovedTests(Base):
    """The two writes the window makes to the peers: a pairing stored, a machine removed."""

    def setUp(self):
        super().setUp()
        self.write_legacy(legacy_1_4_3())
        self.cfg = self.store.load()

    def entry(self, ident, token, name="Laptop", **extra):
        return {**settings_store.PEER_DEFAULTS, "id": ident, "name": name, "platform": "macos", "token": token,
                "host": "192.0.2.30", "port": 24820, "paired_at": 1_700_000_000, **extra}

    def test_a_pairing_is_stored_after_the_peers_it_joins_and_the_flat_settings_still_read_the_first(self):
        self.store.add_peer(self.entry(SECOND_ID, OTHER_TOKEN))
        self.assertEqual([peer["token"] for peer in self.settings()["peers"]], [TOKEN, OTHER_TOKEN])
        self.assertEqual(self.store.load().auth_token, TOKEN)

    def test_the_peer_the_book_serves_is_the_one_just_stored(self):
        self.store.add_peer(self.entry(SECOND_ID, OTHER_TOKEN))
        self.assertEqual([peer["token"] for peer in self.store.book().peers()], [TOKEN, OTHER_TOKEN])

    def test_a_pairing_replacing_the_1_4_entry_takes_its_place_and_its_zones_go(self):
        self.assertTrue(self.settings()["zones"])
        replaced = copy.deepcopy(self.settings()["peers"][0])
        self.store.add_peer(self.entry(PEER_ID, OTHER_TOKEN, name="Studio"), replaced)
        after = self.settings()
        self.assertEqual([peer["id"] for peer in after["peers"]], [PEER_ID])
        # Only the new machine's own edge, which every machine paired starts with.
        self.assertEqual(after["zones"], [{"peer": PEER_ID, "kind": "edge"}])
        self.assertEqual(self.store.load().auth_token, OTHER_TOKEN)

    def test_a_pairing_that_cannot_be_written_raises_and_changes_nothing(self):
        before = self.path.read_bytes()
        with mock.patch.object(self.store, "_write", side_effect=SettingsError("disk full")):
            with self.assertRaises(SettingsError):
                self.store.add_peer(self.entry(SECOND_ID, OTHER_TOKEN))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.store.current()["peers"]), 1)

    def test_removing_a_machine_takes_its_zones_and_leaves_the_others(self):
        self.store.add_peer(self.entry(SECOND_ID, OTHER_TOKEN))
        removed = self.store.remove_peer(OTHER_TOKEN)
        self.assertEqual(removed["id"], SECOND_ID)
        self.assertEqual([peer["token"] for peer in self.settings()["peers"]], [TOKEN])
        self.assertTrue(self.settings()["zones"])

    def test_removing_the_first_machine_makes_the_next_the_one_the_flat_settings_read(self):
        self.store.add_peer(self.entry(SECOND_ID, OTHER_TOKEN))
        self.store.remove_peer(TOKEN)
        self.assertEqual(self.store.load().auth_token, OTHER_TOKEN)
        self.assertEqual(self.settings()["zones"], [{"peer": SECOND_ID, "kind": "edge"}])

    def test_removing_the_only_machine_leaves_this_one_unpaired_and_it_stays_so(self):
        self.store.remove_peer(TOKEN)
        self.assertEqual(self.store.load().auth_token, "")
        self.assertEqual(self.settings()["peers"], [])
        self.assertEqual(SettingsStore(self.path).load().auth_token, "")

    def test_with_nothing_paired_appearance_and_speed_still_save(self):
        self.store.remove_peer(TOKEN)
        cfg = self.store.load()
        saved = self.store.save(config_to_raw(replace(cfg, appearance="dark", pointer_speed=1.5, hide_addresses=True)))
        self.assertEqual((saved.appearance, saved.pointer_speed, saved.hide_addresses), ("dark", 1.5, True))
        self.assertEqual(self.settings()["peers"], [])
        again = SettingsStore(self.path).load()
        self.assertEqual((again.appearance, again.pointer_speed, again.hide_addresses, again.auth_token), ("dark", 1.5, True, ""))

    def test_removing_a_machine_that_is_not_there_is_nothing(self):
        self.assertIsNone(self.store.remove_peer("not-a-token"))
        self.assertEqual(len(self.settings()["peers"]), 1)

    def test_one_machines_directions_are_its_own_switches(self):
        self.store.add_peer(self.entry(SECOND_ID, OTHER_TOKEN))
        first = self.settings()["peers"][0]
        entry = self.store.set_peer(OTHER_TOKEN, send=False)
        self.assertEqual((entry["send"], entry["allow_drive"]), (False, True))
        peers = self.settings()["peers"]
        self.assertEqual(peers[0], first)
        self.assertEqual((peers[1]["send"], peers[1]["allow_drive"]), (False, True))
        self.store.set_peer(OTHER_TOKEN, allow_drive=False)
        self.assertFalse(self.settings()["peers"][1]["allow_drive"])

    def test_the_first_machines_switches_reach_the_flat_settings(self):
        self.store.set_peer(TOKEN, send=False, allow_drive=False)
        cfg = self.store.load()
        self.assertEqual((cfg.send_to_windows, cfg.allow_windows_to_drive), (False, False))

    def test_only_the_two_switches_can_be_changed_this_way(self):
        with self.assertRaises(SettingsError):
            self.store.set_peer(TOKEN, host="elsewhere")
        with self.assertRaises(SettingsError):
            self.store.set_peer(TOKEN, send="yes")

    def test_a_machine_that_is_not_there_has_no_switches_to_change(self):
        self.assertIsNone(self.store.set_peer("not-a-token", send=False))

    def test_a_save_made_from_settings_read_before_a_pairing_does_not_undo_it(self):
        # The window holds the flat settings it last loaded; a pairing stored from the service's
        # thread changes which machine they describe before the window has read them again.
        stale = config_to_raw(self.cfg)
        self.store.remove_peer(TOKEN)
        self.store.add_peer(self.entry(SECOND_ID, OTHER_TOKEN))
        self.store.save(dict(stale, hide_addresses=True))
        peers = self.settings()["peers"]
        self.assertEqual([peer["token"] for peer in peers], [OTHER_TOKEN])
        self.assertEqual(peers[0]["name"], "Laptop")
        self.assertTrue(self.settings()["hide_addresses"])

    def test_a_stale_save_with_nothing_paired_does_not_pop_the_machine_just_paired(self):
        self.store.remove_peer(TOKEN)
        empty = config_to_raw(self.store.load())
        self.store.add_peer(self.entry(SECOND_ID, OTHER_TOKEN))
        self.store.save(dict(empty, hide_addresses=True))
        self.assertEqual([peer["token"] for peer in self.settings()["peers"]], [OTHER_TOKEN])

    def test_a_phone_is_not_paired_on_a_desktop_in_this_version(self):
        # A port-0 first peer would make the flat settings, which read its port, unloadable.
        with self.assertRaises(SettingsError):
            self.store.add_peer(self.entry(SECOND_ID, OTHER_TOKEN, port=0, host=""))
        self.assertEqual(len(self.settings()["peers"]), 1)
        self.store.load()

    def test_a_replaced_entry_that_has_linked_since_the_pairing_began_is_not_replaced(self):
        replaced = copy.deepcopy(self.settings()["peers"][0])
        settings = self.settings()
        settings["peers"][0].update(id=PEER_ID, linked=True)
        self.store.save_settings(settings)
        with self.assertRaises(SettingsError):
            self.store.add_peer(self.entry(SECOND_ID, OTHER_TOKEN), replaced)
        self.assertEqual([peer["token"] for peer in self.settings()["peers"]], [TOKEN])

    def test_a_save_that_names_a_new_token_still_replaces_the_first_machine(self):
        raw = config_to_raw(self.cfg)
        self.store.save(dict(raw, auth_token=THIRD_TOKEN))
        self.assertEqual([peer["token"] for peer in self.settings()["peers"]], [THIRD_TOKEN])

    def test_the_book_the_links_read_sees_a_removal_at_once(self):
        book = self.store.book()
        self.store.add_peer(self.entry(SECOND_ID, OTHER_TOKEN))
        self.store.remove_peer(OTHER_TOKEN)
        self.assertEqual([peer["token"] for peer in book.peers()], [TOKEN])


class ValidationTests(Base):
    def setUp(self):
        super().setUp()
        self.write_legacy(legacy_1_4_3())
        self.store.load()

    def broken(self, change):
        settings = self.settings()
        change(settings)
        self.write_settings(settings)
        with self.assertRaises(SettingsError):
            SettingsStore(self.path).load()

    def test_a_bad_machine_id_is_refused(self):
        self.broken(lambda s: s.update(machine_id="not an id"))
        self.broken(lambda s: s.update(machine_id=base64.urlsafe_b64encode(bytes(16)).decode().rstrip("=")))
        self.broken(lambda s: s.update(machine_id=""))

    def test_two_entries_with_one_id_or_our_own_id_are_refused(self):
        def twin(s):
            s["peers"][0]["id"] = PEER_ID
            s["peers"].append({**s["peers"][0], "token": OTHER_TOKEN, "from_1_4": False})
        self.broken(twin)
        self.broken(lambda s: s["peers"][0].update(id=s["machine_id"]))

    def test_two_entries_with_one_key_id_are_refused(self):
        def same_token(s):
            s["peers"][0]["id"] = PEER_ID
            s["peers"].append({**s["peers"][0], "id": SECOND_ID, "from_1_4": False})
        self.broken(same_token)

    def test_more_than_one_empty_id_or_an_empty_id_that_is_not_from_1_4_is_refused(self):
        self.broken(lambda s: s["peers"].append({**s["peers"][0], "token": OTHER_TOKEN}))
        self.broken(lambda s: s["peers"][0].update(from_1_4=False))

    def test_more_than_thirty_two_peers_are_refused(self):
        def many(s):
            s["peers"] = [{**s["peers"][0], "id": base64.urlsafe_b64encode(bytes([n]) * 16).decode().rstrip("="),
                           "token": base64.urlsafe_b64encode(bytes([n, 7]) * 16).decode().rstrip("="),
                           "from_1_4": False} for n in range(1, 34)]
        self.broken(many)

    def test_an_unknown_schema_is_refused_rather_than_migrated_over(self):
        self.broken(lambda s: s.update(schema=7))

    def test_a_file_that_does_not_parse_is_refused_and_left_alone(self):
        self.path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(SettingsError):
            SettingsStore(self.path).load()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{oops")


class RefusingToStartOverTests(Base):
    """A settings file that exists is never replaced by a fresh one: that would change the machine id."""

    def setUp(self):
        super().setUp()
        self.write_legacy(legacy_1_4_3())
        self.cfg = self.store.load()
        self.machine_id = self.settings()["machine_id"]

    def assert_untouched(self, before):
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.dir.iterdir()].count("settings.invalid.json"), 0)

    def test_a_future_schema_is_refused_on_save_and_left_as_it_is(self):
        settings = self.settings()
        settings["schema"] = 7
        self.write_settings(settings)
        before = self.path.read_bytes()
        with self.assertRaises(SettingsError):
            self.store.save(config_to_raw(self.cfg))
        self.assert_untouched(before)

    def test_a_file_without_a_machine_id_is_refused_and_never_given_one(self):
        settings = self.settings()
        del settings["machine_id"]
        self.write_settings(settings)
        before = self.path.read_bytes()
        with self.assertRaises(SettingsError):
            self.store.load()
        with self.assertRaises(SettingsError):
            self.store.save(config_to_raw(self.cfg))
        self.assert_untouched(before)

    def test_a_file_without_peers_or_zones_is_refused(self):
        for name in ("peers", "zones"):
            settings = self.settings()
            del settings[name]
            self.write_settings(settings)
            with self.assertRaises(SettingsError):
                self.store.load()
            self.write_settings({**settings, name: []})

    def test_a_value_that_fails_validation_stops_saves_instead_of_dropping_the_pairing(self):
        settings = self.settings()
        settings["pointer_speed"] = 9
        self.write_settings(settings)
        before = self.path.read_bytes()
        with self.assertRaises(SettingsError):
            self.store.load()
        with self.assertRaises(SettingsError):
            self.store.save(config_to_raw(settings_store.editable_default_config()))
        self.assert_untouched(before)

    def test_infinity_in_the_file_is_a_settings_error(self):
        self.path.write_text(self.path.read_text(encoding="utf-8").replace('"double_tap_ms": 300', '"double_tap_ms": Infinity'), encoding="utf-8")
        with self.assertRaises(SettingsError):
            self.store.load()

    def test_a_failed_write_while_re_importing_does_not_stop_the_app_opening(self):
        raw = legacy_1_4_3()
        raw["auth_token"] = OTHER_TOKEN
        self.write_legacy(raw)
        with mock.patch("settings_store.os.replace", side_effect=OSError("read only")):
            cfg = self.store.load()
        self.assertEqual(cfg.auth_token, OTHER_TOKEN)
        self.assertEqual(self.settings()["machine_id"], self.machine_id)
        self.assertEqual(self.store.load().auth_token, OTHER_TOKEN)


class ReimportRefusalsTests(Base):
    def setUp(self):
        super().setUp()
        self.write_legacy(legacy_1_4_3())
        self.store.load()

    def test_a_config_that_fails_validation_is_not_migrated_but_is_not_tried_again(self):
        raw = legacy_1_4_3()
        raw["auth_token"] = OTHER_TOKEN
        raw["crossing"]["corner"] = "top"
        self.write_legacy(raw)
        cfg = self.store.load()
        self.assertEqual(cfg.auth_token, TOKEN)
        self.assertEqual(self.settings()["migrated_token_sha256"], sha(OTHER_TOKEN))

    def test_a_thirty_third_peer_is_not_added(self):
        settings = self.settings()
        settings["peers"] = [{**settings["peers"][0], "id": base64.urlsafe_b64encode(bytes([n]) * 16).decode().rstrip("="),
                              "token": base64.urlsafe_b64encode(bytes([n, 7]) * 16).decode().rstrip("="),
                              "from_1_4": False} for n in range(1, 33)]
        settings["zones"] = []
        self.write_settings(settings)
        raw = legacy_1_4_3()
        raw["auth_token"] = OTHER_TOKEN
        self.write_legacy(raw)
        self.store.load()
        self.assertEqual(len(self.settings()["peers"]), 32)

    def test_a_fractional_arrangement_stamp_migrates(self):
        self.path.unlink()
        raw = legacy_1_4_3()
        raw["crossing"]["arrangement_set_at"] = 1790000000.5
        self.write_legacy(raw)
        self.assertEqual(self.store.load().crossing["arrangement_set_at"], 1790000000)

    def test_a_zone_with_a_bad_corner_or_an_unknown_peer_is_refused(self):
        for change in (lambda s: s["zones"][2].update(corner="top"),
                       lambda s: s["zones"][2].update(edge="middle"),
                       lambda s: s["zones"][1].update(parts=[]),
                       lambda s: s["zones"][0].update(off="yes"),
                       lambda s: s["zones"][0].update(peer=PEER_ID)):
            settings = self.settings()
            change(settings)
            self.write_settings(settings)
            with self.assertRaises(SettingsError):
                SettingsStore(self.path).load()
            self.path.unlink()
            self.write_legacy(legacy_1_4_3())
            self.store.load()

    def test_the_id_a_peer_learns_renames_only_the_zones_of_the_entry_with_that_token(self):
        settings = self.settings()
        added = {**settings["peers"][0], "id": SECOND_ID, "token": OTHER_TOKEN, "from_1_4": False}
        settings["peers"].append(added)
        self.write_settings(settings)
        settings["peers"] = [added]
        settings["zones"] = []
        self.store.save_settings(settings)
        self.assertEqual(self.settings()["zones"], [{"peer": SECOND_ID, "kind": "edge"}])


class OverlapTests(Base):
    def setUp(self):
        super().setUp()
        self.write_legacy(legacy_1_4_3())
        self.store.load()

    def refused(self, change):
        settings = self.settings()
        change(settings)
        self.write_settings(settings)
        with self.assertRaises(SettingsError):
            SettingsStore(self.path).load()

    def test_the_edge_and_its_thirds_in_use_together_are_refused(self):
        self.refused(lambda s: s["zones"][1].pop("off"))

    def test_the_edge_and_the_notch_stay_together_as_1_4_x_let_them(self):
        settings = self.settings()
        settings["zones"][3].pop("off")
        self.write_settings(settings)
        self.assertIn("notch", SettingsStore(self.path).load().crossing["methods"])

    def test_two_part_zones_sharing_a_third_are_refused_and_disjoint_ones_are_not(self):
        def two_parts(s, second):
            s["zones"][0]["off"] = True
            s["zones"][1].pop("off")
            s["zones"].append({"peer": "", "kind": "part", "parts": second})
        self.refused(lambda s: two_parts(s, ["end", "middle"]))
        self.path.unlink()
        self.write_legacy(legacy_1_4_3())
        self.store.load()
        settings = self.settings()
        two_parts(settings, ["middle"])
        self.write_settings(settings)
        SettingsStore(self.path).load()

    def test_two_peers_whose_edges_face_the_same_side_cannot_both_have_the_edge_zone_on(self):
        def second_peer(s):
            s["peers"][0]["id"] = PEER_ID
            s["zones"] = [{**z, "peer": PEER_ID} for z in s["zones"]]
            s["peers"].append({**s["peers"][0], "id": SECOND_ID, "token": OTHER_TOKEN, "from_1_4": False})
            s["zones"].append({"peer": SECOND_ID, "kind": "edge"})
        self.refused(second_peer)

    def test_a_corner_inside_the_edge_is_fine_and_two_of_one_corner_are_not(self):
        settings = self.settings()
        settings["zones"][2]["corner"] = "top_left"
        settings["zones"][2]["edge"] = "left"
        self.write_settings(settings)
        SettingsStore(self.path).load()
        self.refused(lambda s: s["zones"].append({"peer": "", "kind": "corner", "corner": "top_left", "edge": "left"}))

    def test_ways_with_the_edge_and_the_thirds_on_write_the_thirds_off(self):
        self.store.set_ways("", side="left", methods=["edge", "part"], parts=["middle"], corner="top_right")
        self.assertEqual(self.store.load().crossing["methods"], ["shortcut", "edge"])
        zones = {z["kind"]: z for z in self.settings()["zones"]}
        self.assertTrue(zones["part"]["off"])

    def test_two_machines_on_one_notch_are_refused(self):
        def two_notches(s):
            s["peers"][0]["id"] = PEER_ID
            s["zones"] = [{**z, "peer": PEER_ID} for z in s["zones"]]
            s["zones"][3].pop("off", None)
            second = {**copy.deepcopy(s["peers"][0]), "id": SECOND_ID, "side": "right"}
            second["token"] = second["token"][::-1]
            s["peers"].append(second)
            s["zones"].append({"peer": SECOND_ID, "kind": "notch"})
        self.refused(two_notches)


if __name__ == "__main__":
    unittest.main()
