"""A peer's `arrangement` and what the links write into the first peer, as the settings keep them
(WIRE.md sections 1 and 8)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app_config
from core import protocol
from core.tests import responder_harness as harness

B, C = harness.B, harness.C
BT, CT = protocol.id_text(B), protocol.id_text(C)
LOW, HIGH = protocol.id_text(bytes([1]) * 16), protocol.id_text(bytes([200]) * 16)


class Folder(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "settings.json"
        settings = app_config.migrate(None, machine_id=protocol.id_text(harness.HERE))
        settings["peers"] = [
            harness.entry(B, "Mac", side="left", side_set_at=100, side_by=LOW),
            harness.entry(C, "Other", platform="windows", side="top"),
        ]
        settings["zones"] = [
            {"peer": BT, "kind": "edge"},
            {"peer": BT, "kind": "corner", "corner": "top_left", "edge": "left", "off": True},
            {"peer": CT, "kind": "edge"},
        ]
        app_config.write_settings(self.path, settings)

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def peer(self, ident):
        return next(item for item in self.saved()["peers"] if item["id"] == ident)


class ArrangementTests(Folder):
    def test_a_newer_stamp_sets_the_opposite_side_and_the_stamp_and_author(self):
        changed, notices = app_config.apply_arrangement(self.path, BT, "left", 200, HIGH)
        self.assertTrue(changed)
        entry = self.peer(BT)
        self.assertEqual((entry["side"], entry["side_set_at"], entry["side_by"]), ("right", 200, HIGH))
        self.assertEqual(notices, [])

    def test_an_older_or_equal_stamp_changes_nothing(self):
        for set_at, by in ((99, HIGH), (100, LOW)):
            self.assertEqual(app_config.apply_arrangement(self.path, BT, "left", set_at, by), (False, []))
        self.assertEqual(self.peer(BT)["side"], "left")

    def test_the_same_second_goes_to_the_larger_id(self):
        self.assertTrue(app_config.apply_arrangement(self.path, BT, "left", 100, HIGH)[0])
        self.assertEqual(self.peer(BT)["side"], "right")

    def test_a_peer_that_is_not_in_the_list_or_an_edge_that_is_not_one_is_ignored(self):
        self.assertEqual(app_config.apply_arrangement(self.path, protocol.id_text(harness.D), "left", 200, HIGH), (False, []))
        self.assertEqual(app_config.apply_arrangement(self.path, BT, "up", 200, HIGH), (False, []))
        self.assertEqual(app_config.apply_arrangement(self.path, BT, "left", 200, "not an id"), (False, []))

    def test_a_side_that_clashes_with_another_peers_zone_turns_the_senders_zone_off_and_says_so(self):
        changed, notices = app_config.apply_arrangement(self.path, BT, "bottom", 200, HIGH)
        self.assertTrue(changed)
        self.assertEqual(self.peer(BT)["side"], "top")
        zones = [zone for zone in self.saved()["zones"] if zone["peer"] == BT and zone["kind"] == "edge"]
        self.assertTrue(zones[0]["off"])
        self.assertTrue(notices and "Mac" in notices[0] and "Other" in notices[0])
        other = [zone for zone in self.saved()["zones"] if zone["peer"] == CT][0]
        self.assertNotIn("off", other)


class LearnedTests(Folder):
    def test_what_the_links_write_comes_back_in_the_flat_names(self):
        settings = self.saved()
        settings["peers"][0].update(host="192.168.77.9", hw="aa:bb:cc:dd:ee:ff", name="Studio")
        app_config.write_settings(self.path, settings)
        learned = app_config.learned_fields(self.path)
        self.assertEqual(learned["mac_host"], "192.168.77.9")
        self.assertEqual(learned["mac_hardware_address"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(learned["paired_with"], "Studio")
        # A side is no longer the flat view's: no save writes one back (set_ways and apply_arrangement do).
        self.assertNotIn("mac_return_edge", learned)

    def test_the_token_comes_back_too_so_a_fold_cannot_be_undone_by_a_save(self):
        settings = self.saved()
        settings["peers"] = settings["peers"][1:]
        app_config.write_settings(self.path, settings)
        learned = app_config.learned_fields(self.path)
        self.assertEqual(learned["auth_token"], harness.entry(C, "Other")["token"])
        self.assertEqual(learned["paired_with"], "Other")

    def test_the_stamps_author_for_same_on_all_machines_is_kept(self):
        config = app_config.load_config(self.path)
        self.assertEqual(config.same_by, "")
        app_config.save_config(self.path, app_config.replace(config, same_by=HIGH, same_set_at=5))
        self.assertEqual(app_config.load_config(self.path).same_by, HIGH)


if __name__ == "__main__":
    unittest.main()
