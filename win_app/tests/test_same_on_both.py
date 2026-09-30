"""Same on both machines in the real window: the Overview switch, a Mac's settings arriving, and a
change here going out. Built offscreen from a synthetic config as test_style_for builds it, which
starts no receiver, hooks or announcer; the two links' sends are recorded instead."""

import json
import os
import tempfile
import unittest
from pathlib import Path

try:
    from PySide6.QtWidgets import QApplication

    import kvm_bridge_win
    import pages_win
    from core import settings_sync
    import theme
    from app_config import load_config
except ImportError:  # PySide6 is only in the Windows venv
    kvm_bridge_win = None


def mac_message(on, set_at, **values):
    base = dict(methods=["corner"], edge_parts=["start"], corner="top_right", resistance_px=60,
                block_while_dragging=False, trigger_key="alt_r", trigger_style="hold", double_tap_ms=400,
                glow_style="beam", glow_colour="ocean", effect_length="long", shortcut_arrival=False,
                shortcut_arrival_style="locator")
    base.update(values)
    return settings_sync.message_data(on, set_at, base)


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class SameOnBothTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        self.path = Path(tempfile.mkdtemp()) / "config.json"
        self.path.write_text(json.dumps({
            "host": "192.168.1.20", "port": 24820, "auth_token": "synthetic", "paired_with": "MacBook Pro",
            "mac_host": "192.168.1.10", "mac_return_edge": "left", "edge_glow": True,
        }))
        self.window = kvm_bridge_win.WindowsApplication(self.path)
        self.sent = []
        self.window.sender.send_settings = lambda data: self.sent.append(("pc-to-mac", data)) or True
        self.window.server.send_settings = lambda data: self.sent.append(("mac-to-pc", data)) or True
        self.window._refresh_window()

    def tearDown(self):
        self.window.deleteLater()

    def test_off_by_default_with_each_page_its_own(self):
        self.assertFalse(self.window.same_switch.isChecked())
        self.assertEqual(self.window.scope_labels["design"].text(), pages_win.SCOPE["design"])

    def test_the_macs_settings_arrive_on_this_pcs_fields_and_controls(self):
        self.window._on_settings(mac_message(True, 100))
        config = self.window._config
        self.assertTrue(config.same_on_both)
        self.assertEqual((config.crossing_methods, config.crossing_edge_parts), (["corner"], ["start"]))
        # The Mac is on this PC's left, so its top right is this PC's top left.
        self.assertEqual(config.crossing_corner, "top_left")
        self.assertEqual((config.glow_style, config.glow_colour, config.effect_length), ("beam", "ocean", "long"))
        self.assertEqual((config.trigger_style, config.double_tap_ms, config.shortcut_arrival), ("hold", 400, False))
        self.assertTrue(config.edge_glow)
        self.assertEqual(load_config(self.path).glow_style, "beam")
        self.assertEqual(self.window.resistance_slider.value(), 60)
        self.assertFalse(self.window.landing_toggle.isChecked())
        self.window._refresh_window()
        self.assertTrue(self.window.same_switch.isChecked())
        self.assertEqual(self.window.scope_labels["crossing"].text(),
                         "Kept the same as your Mac. Change it on either machine.")
        # Applying what arrived is not a change made here, so nothing is sent back.
        self.assertEqual(self.sent, [])

    def test_an_older_message_changes_nothing(self):
        self.window._on_settings(mac_message(True, 100))
        self.window._on_settings(mac_message(True, 90, glow_style="glow"))
        self.assertEqual(self.window._config.glow_style, "beam")

    def test_turning_it_on_here_sends_this_pcs_values_on_both_links(self):
        self.window.same_switch.setChecked(True)
        self.assertEqual([link for link, _data in self.sent], ["pc-to-mac", "mac-to-pc"])
        data = self.sent[0][1]
        self.assertTrue(data["on"])
        self.assertEqual(data["crossing"]["corner"], "top_right")

    def test_a_change_here_while_on_is_stamped_and_sent_and_one_to_this_pcs_own_is_not(self):
        self.window.same_switch.setChecked(True)
        stamp = self.window._config.same_set_at
        self.sent.clear()
        self.window._config.same_set_at = stamp - 10
        self.window._config.edge_glow = False
        self.window._persist()
        self.assertEqual(self.sent, [])
        self.window._config.effect_length = "short"
        self.window._persist()
        self.assertEqual(len(self.sent), 2)
        self.assertGreaterEqual(self.window._config.same_set_at, stamp)
        self.assertEqual(self.sent[0][1]["design"]["effect_length"], "short")

    def test_a_change_here_while_off_goes_nowhere(self):
        self.window._config.effect_length = "short"
        self.window._persist()
        self.assertEqual(self.sent, [])

    def test_announcing_carries_a_chosen_arrangement_but_not_a_learned_one(self):
        self.assertEqual([m["type"] for m in self.window._announce()], ["settings"])
        self.window._config.arrangement_set_at = 500
        messages = self.window._announce()
        self.assertEqual(messages[0], {"type": "arrangement", "data": {"mac_edge": "right", "set_at": 500}})


if __name__ == "__main__":
    unittest.main()
