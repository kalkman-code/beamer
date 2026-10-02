"""Same on all machines in the real window: the Overview switch, a Mac's settings arriving, and a
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
    import app_config
    from app_config import load_config
    from core import protocol
    from core.tests import responder_harness as harness
except ImportError:  # PySide6 is only in the Windows venv
    kvm_bridge_win = None


def mac_message(on, set_at, by=None, **values):
    base = dict(shortcut=False, resistance_px=60,
                block_while_dragging=False, trigger_key="alt_r", trigger_style="hold", double_tap_ms=400,
                glow_style="beam", glow_colour="ocean", effect_length="long", shortcut_arrival=False,
                shortcut_arrival_style="locator")
    base.update(values)
    return settings_sync.message_data(on, set_at, base, by=by or protocol.id_text(harness.B))


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class SameOnBothTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        folder = Path(tempfile.mkdtemp())
        self.path = folder / "settings.json"
        (folder / "config.json").write_text(json.dumps({
            "host": "192.168.1.20", "port": harness.free_port(), "auth_token": "synthetic", "paired_with": "MacBook Pro",
            "mac_host": "192.168.1.10", "mac_return_edge": "left", "edge_glow": True,
        }))
        self.window = kvm_bridge_win.WindowsApplication(self.path)
        self.sent = []
        self.peers = {harness.B: {"settings"}, harness.C: {"settings"}}
        self.window._peer_caps = lambda: dict(self.peers)
        self.window._send_to = lambda peer, message: self.sent.append((peer, message["data"])) or True
        self.window._refresh_window()

    def tearDown(self):
        self.window.server.stop()
        self.window.deleteLater()

    def test_off_by_default_with_each_page_its_own(self):
        self.assertFalse(self.window.same_switch.isChecked())
        self.assertEqual(self.window.scope_labels["design"].text(), pages_win.SCOPE["design"])

    def test_the_macs_settings_arrive_on_this_pcs_fields_and_controls(self):
        self.window._on_settings(protocol.id_text(harness.B), mac_message(True, 100))
        config = self.window._config
        self.assertTrue(config.same_on_both)
        self.assertNotIn("shortcut", config.crossing_methods)
        self.assertEqual((config.glow_style, config.glow_colour, config.effect_length), ("beam", "ocean", "long"))
        self.assertEqual((config.trigger_style, config.double_tap_ms, config.shortcut_arrival), ("hold", 400, False))
        self.assertTrue(config.edge_glow)
        self.assertEqual(load_config(self.path).glow_style, "beam")
        self.assertEqual(self.window.resistance_slider.value(), 60)
        self.assertFalse(self.window.landing_toggle.isChecked())
        self.window._refresh_window()
        self.assertTrue(self.window.same_switch.isChecked())
        self.assertEqual(self.window.scope_labels["crossing"].text(),
                         "Kept the same as MacBook Pro. Change it on any machine.")
        # Applying what arrived is not a change made here: it goes on to the other peer and never back.
        self.assertEqual([peer for peer, _data in self.sent], [harness.C])

    def test_an_older_message_changes_nothing(self):
        self.window._on_settings(protocol.id_text(harness.B), mac_message(True, 100))
        self.window._on_settings(protocol.id_text(harness.B), mac_message(True, 90, glow_style="glow"))
        self.assertEqual(self.window._config.glow_style, "beam")

    def test_a_state_a_peer_already_holds_is_not_sent_on_again(self):
        self.window._on_settings(protocol.id_text(harness.B), mac_message(True, 100))
        self.sent.clear()
        self.window._on_settings(protocol.id_text(harness.C), mac_message(True, 100))
        self.assertEqual(self.sent, [])

    def test_a_peer_without_the_settings_capability_is_sent_none(self):
        self.peers[harness.C] = set()
        self.window.same_switch.setChecked(True)
        self.assertEqual([peer for peer, _data in self.sent], [harness.B])

    def test_turning_it_on_here_sends_this_pcs_values_to_every_peer_that_takes_them(self):
        self.window.same_switch.setChecked(True)
        self.assertEqual([peer for peer, _data in self.sent], [harness.B, harness.C])
        data = self.sent[0][1]
        self.assertTrue(data["on"])
        self.assertIs(data["crossing"]["shortcut"], True)

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
        self.assertEqual(self.window._config.same_by, self.window._config.machine_id)
        self.assertEqual(self.sent[0][1]["design"]["effect_length"], "short")

    def test_a_save_after_a_link_replaced_the_first_peer_does_not_bring_the_old_pairing_back(self):
        # A fold removed the entry migrated from 1.4.x; the window still holds its token.
        settings = app_config.load_settings(self.path)
        settings["peers"] = [harness.entry(harness.B, "Mac", linked=True, token=harness.TOKENS[harness.B])]
        settings["zones"] = []
        app_config.write_settings(self.path, settings)
        self.window._config.effect_length = "short"
        self.assertTrue(self.window._persist())
        saved = app_config.load_settings(self.path)
        self.assertEqual([peer["token"] for peer in saved["peers"]], [harness.TOKENS[harness.B]])
        self.assertEqual(self.window._config.auth_token, harness.TOKENS[harness.B])

    def test_a_state_that_goes_on_is_sent_as_it_arrived(self):
        self.peers = {harness.B: {"settings"}, harness.C: {"settings"}}
        message = mac_message(True, 100)
        self.window._on_settings(protocol.id_text(harness.B), message)
        self.assertEqual(self.sent, [(harness.C, message)])

    def test_a_change_here_while_off_goes_nowhere(self):
        self.window._config.effect_length = "short"
        self.window._persist()
        self.assertEqual(self.sent, [])

    def test_announcing_carries_a_chosen_arrangement_the_peers_it_may_send_to_and_the_settings(self):
        settings = app_config.load_settings(self.path)
        settings["peers"][0]["id"] = protocol.id_text(harness.B)
        app_config.write_settings(self.path, settings)
        self.peers = {harness.B: {"settings"}}
        self.assertEqual([m["type"] for m in self.window._announce(harness.B)], ["paired", "settings"])
        settings["peers"][0].update(side="left", side_set_at=500, side_by=self.window._config.machine_id)
        app_config.write_settings(self.path, settings)
        messages = self.window._announce(harness.B)
        self.assertEqual(messages[0]["type"], "arrangement")
        self.assertEqual(messages[0]["data"]["edge"], "left")
        self.assertEqual(messages[0]["data"]["set_at"], 500)


if __name__ == "__main__":
    unittest.main()
