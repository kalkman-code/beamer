"""Sender-only phones survive the Windows/Linux settings and legacy flat view."""

import copy
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "win_app"))

import app_config
from core import protocol


def peer(number, phone=False, **fields):
    return {
        "id": protocol.id_text(bytes([number]) * 16), "name": "Phone" if phone else "Desktop",
        "platform": "ios" if phone else "macos", "token": app_config._b64(bytes([number]) * 32),
        "host": "" if phone else "192.0.2.20", "port": 0 if phone else 24820, "hw": "",
        "send": not phone, "allow_drive": True, "in_use": True, "side": "", "side_set_at": 0,
        "side_by": "", "jump_key": "", "paired_with": [], "paired_at": 1, "linked": True,
        "from_1_4": False, **fields,
    }


class PhoneSettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "settings.json"
        self.phone = peer(2, phone=True)
        self.desktop = peer(3)
        self.other = peer(4)

    def put(self, peers, zones=()):
        settings = app_config.migrate(None, machine_id=protocol.id_text(bytes([1]) * 16))
        settings.update(peers=copy.deepcopy(peers), zones=copy.deepcopy(list(zones)))
        app_config._write_json(self.path, settings)
        return settings

    def positions(self):
        return ([self.phone, self.desktop, self.other], [self.desktop, self.phone, self.other],
                [self.desktop, self.other, self.phone], [self.phone])

    def test_flat_round_trip_skips_phones_in_every_position(self):
        for peers in self.positions():
            with self.subTest(order=[p["port"] for p in peers]):
                before = self.put(peers)
                config = app_config.load_config(self.path)
                desktop = next((p for p in peers if p["port"]), None)
                self.assertEqual(config.auth_token, desktop["token"] if desktop else "")
                self.assertEqual(config.mac_host, desktop["host"] if desktop else "")
                app_config.save_config(self.path, replace(config, hide_addresses=True))
                after = app_config.load_settings(self.path)
                self.assertEqual(after["peers"], before["peers"])
                self.assertTrue(after["hide_addresses"])

    def test_new_flat_pairing_replaces_first_desktop_and_keeps_phone(self):
        for peers in self.positions():
            with self.subTest(order=[p["port"] for p in peers]):
                self.put(peers)
                config = app_config.load_config(self.path)
                app_config.save_config(self.path, replace(config, auth_token="new legacy pair", mac_host="192.0.2.30"))
                after = app_config.load_settings(self.path)["peers"]
                self.assertEqual(next(p for p in after if p["port"] == 0), self.phone)
                self.assertEqual(next(p for p in after if p["port"] != 0)["token"], "new legacy pair")

    def test_legacy_reimport_never_replaces_a_zero_port_entry(self):
        for peers in self.positions():
            with self.subTest(order=[p["port"] for p in peers]):
                peers = copy.deepcopy(peers)
                next(p for p in peers if p["port"] == 0)["from_1_4"] = True
                settings = self.put(peers)
                legacy = app_config.config_to_dict(app_config.default_config())
                legacy.update(auth_token="legacy re-pair", mac_host="192.0.2.30")
                app_config.remigrate(settings, legacy)
                self.assertEqual(next(p for p in settings["peers"] if p["port"] == 0), next(p for p in peers if p["port"] == 0))
                self.assertTrue(any(p["token"] == "legacy re-pair" and p["port"] != 0 for p in settings["peers"]))

    def test_load_and_write_clear_phone_targets_and_keep_permissions(self):
        forbidden = {"host": "192.0.2.40", "send": True, "hw": "aa:bb:cc:dd:ee:ff",
                     "side": "right", "side_set_at": 50, "side_by": self.desktop["id"], "jump_key": "ctrl+f2"}
        for writing in (False, True):
            with self.subTest(writing=writing):
                phone = dict(self.phone, allow_drive=False, in_use=False, **forbidden)
                settings = self.put([phone, self.desktop], [{"peer": phone["id"], "kind": "edge"}])
                if writing:
                    app_config.write_settings(self.path, settings)
                result = app_config.load_settings(self.path)
                self.assertEqual(result["peers"][0], dict(self.phone, allow_drive=False, in_use=False))
                self.assertFalse(any(zone.get("peer") == phone["id"] for zone in result["zones"]))
                self.assertEqual(json.loads(self.path.read_text())["peers"][0], result["peers"][0])

    def test_phone_cannot_gain_a_hardware_address(self):
        self.put([self.phone])
        self.assertFalse(app_config.set_peer_hardware_address(self.path, self.phone["id"], "aa:bb:cc:dd:ee:ff"))
        self.assertEqual(app_config.load_settings(self.path)["peers"], [self.phone])


if __name__ == "__main__":
    unittest.main()
