"""Phone settings use synthetic files only; the flat view belongs to a desktop."""
import copy
import json
import tempfile
import unittest
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import protocol
from settings_store import SettingsStore, SettingsError, PEER_DEFAULTS, config_to_raw


def peer(number, port=24820):
    return {**PEER_DEFAULTS, "id": protocol.id_text(bytes([number]) * 16),
            "token": protocol.id_text(bytes([number]) * 32), "name": f"Peer {number}",
            "platform": "ios" if port == 0 else "windows", "port": port,
            "host": "" if port == 0 else "192.0.2.20", "send": port != 0}


class PhoneSettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "settings.json"
        self.store = SettingsStore(self.path)
        self.store.load()

    def test_phones_first_middle_last_and_alone_round_trip(self):
        phone, first, second = peer(1, 0), peer(2), peer(3)
        for peers in ([phone, first, second], [first, phone, second], [first, second, phone], [phone]):
            with self.subTest(order=[p["port"] for p in peers]):
                settings = self.store.current()
                self.store.save_settings({**settings, "peers": copy.deepcopy(peers), "zones": []})
                before = copy.deepcopy(self.store.current()["peers"])
                cfg = self.store.load()
                desktop = next((p for p in peers if p["port"]), None)
                self.assertEqual(cfg.auth_token, desktop["token"] if desktop else "")
                self.assertGreater(cfg.port, 0)
                self.store.save(config_to_raw(replace(cfg, appearance="dark")))
                again = SettingsStore(self.path)
                again.load()
                self.assertEqual(again.current()["peers"], before)
                self.assertFalse(any(z["peer"] == phone["id"] for z in again.current()["zones"]))

    def test_flat_pairing_replacement_and_clear_preserve_phone_first(self):
        phone, desktop = peer(1, 0), peer(2)
        self.store.save_settings({**self.store.current(), "peers": [phone, desktop], "zones": []})
        raw = config_to_raw(self.store.load())
        self.store.save({**raw, "auth_token": peer(3)["token"]})
        self.assertEqual(self.store.current()["peers"][0], phone)
        self.store.save({**config_to_raw(self.store.load()), "auth_token": ""})
        self.assertEqual(self.store.current()["peers"], [phone])

    def test_phone_target_fields_are_cleared_but_permissions_are_preserved(self):
        phone = peer(1, 0)
        phone.update(send=True, host="192.0.2.40", hw="aa:bb:cc:dd:ee:ff", side="left",
                     side_set_at=1, side_by=phone["id"], jump_key="ctrl+shift+2", allow_drive=False, in_use=False)
        self.store.save_settings({**self.store.current(), "peers": [phone],
                                  "zones": [{"peer": phone["id"], "kind": "edge"}],
                                  "design_follow_peer": phone["id"]})
        saved = self.store.current()
        self.assertEqual(saved["zones"], [])
        self.assertEqual(saved["design_follow_peer"], "")
        phone = saved["peers"][0]
        self.assertFalse(phone["send"])
        self.assertFalse(phone["allow_drive"])
        self.assertFalse(phone["in_use"])
        for name in ("host", "hw", "side", "side_by", "jump_key"):
            self.assertEqual(phone[name], "")
        self.assertEqual(phone["side_set_at"], 0)

    def test_new_legacy_pairing_is_added_beside_phone_without_replacing_it(self):
        phone = peer(1, 0)
        self.store.add_peer(phone)
        legacy = config_to_raw(self.store.load())
        legacy.update(auth_token=peer(2)["token"], host="192.0.2.20")
        self.path.with_name("config.json").write_text(json.dumps(legacy), encoding="utf-8")
        cfg = SettingsStore(self.path).load()
        self.assertEqual(cfg.auth_token, legacy["auth_token"])
        self.assertEqual(self.store.load_settings()["peers"][0], phone)

    def test_invalid_peer_or_zone_still_raises_a_settings_error(self):
        for field in ("peers", "zones"):
            with self.subTest(field=field), self.assertRaises(SettingsError):
                self.store.save_settings({**self.store.current(), field: [5]})

    def test_invalid_phone_identity_still_raises_a_settings_error(self):
        for ident in ([], {}):
            with self.subTest(ident=ident), self.assertRaises(SettingsError):
                self.store.save_settings({**self.store.current(), "peers": [{**peer(1, 0), "id": ident}]})
