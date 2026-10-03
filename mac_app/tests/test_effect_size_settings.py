import unittest
from unittest import mock

import config as config_module
import kvm_bridge_app
import settings_store


class EffectSizeSettingsTests(unittest.TestCase):
    def test_top_edge_band_never_exceeds_the_selected_depth(self):
        with mock.patch.object(kvm_bridge_app, "menu_bar_height", return_value=36):
            band = kvm_bridge_app.EdgeGlow._top_band(None, 40, False, (0, 0, 1512, 982), 24)
        self.assertEqual(band, 24)

    def test_missing_size_defaults_to_medium_and_round_trips(self):
        raw = settings_store.config_to_raw(settings_store.editable_default_config())
        raw["crossing"].pop("effect_size", None)
        self.assertEqual(config_module.parse_config(raw).crossing["effect_size"], "medium")

        raw["crossing"]["effect_size"] = "large"
        loaded = config_module.parse_config(raw)
        settings_store.SettingsStore("/unused")._validate(loaded)
        saved = settings_store.config_to_raw(loaded)
        self.assertEqual(saved["crossing"]["effect_size"], "large")


if __name__ == "__main__":
    unittest.main()
