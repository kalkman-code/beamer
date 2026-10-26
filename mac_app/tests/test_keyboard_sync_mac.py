import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import config
from settings_store import SettingsStore, config_to_raw, editable_default_config


class SettingsTests(unittest.TestCase):
    def test_russian_and_opt_in_survive_a_restart(self):
        cfg = replace(editable_default_config(), language="ru", keyboard_sync=True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            SettingsStore(path).save(config_to_raw(cfg))
            restored = SettingsStore(path).load()
            self.assertEqual(restored.language, "ru")
            self.assertTrue(restored.keyboard_sync)

    def test_old_settings_default_off_and_an_invalid_language_does_not_crash(self):
        raw = config_to_raw(editable_default_config())
        raw.pop("keyboard_sync")
        raw["language"] = []
        cfg = config.parse_config(raw)
        self.assertFalse(cfg.keyboard_sync)
        self.assertEqual(cfg.language, "auto")


if __name__ == "__main__":
    unittest.main()
