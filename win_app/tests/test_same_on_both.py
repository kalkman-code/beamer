"""Same on all machines in the real window: the Overview switch, a Mac's settings arriving, and a
change here going out. Built offscreen from a synthetic config as test_style_for builds it, which
starts no receiver, hooks or announcer; the two links' sends are recorded instead."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def pair_peer(self, peer: str) -> None:
        settings = app_config.load_settings(self.path)
        settings["peers"][0]["id"] = peer
        app_config.write_settings(self.path, settings)
        self.window._refresh_peers()

    def test_off_by_default_with_each_page_its_own(self):
        self.assertFalse(self.window.same_switch.isChecked())
        self.assertEqual(self.window.scope_labels["design"].text(), pages_win.SCOPE["design"])

    def test_full_screen_hold_switch_names_this_pcs_edges(self):
        self.assertEqual(self.window.hold_switch.text(), "Hold this PC's edges while an app on it is full screen")
    def test_this_pcs_design_state_is_announced_when_same_is_off(self):
        data = self.window._same_state()

        self.assertFalse(data["on"])
        self.assertEqual(data["design_sync"]["values"]["effect_size"], "medium")
        self.assertEqual(set(data["design_sync"]["values"]), set(settings_sync.DESIGN_KEYS))

    def test_a_followed_design_is_applied_and_renamed_as_this_pcs_state(self):
        peer = protocol.id_text(harness.B)
        self.pair_peer(peer)
        self.window._config.design_follow_peer = peer
        received = settings_sync.message_data(
            False, 100, by=peer,
            design_state={"set_at": 200, "by": peer,
                          "values": {"effect_length": "long"}},
        )

        self.window._on_settings(peer, received)

        self.assertEqual(self.window._config.effect_length, "long")
        self.assertGreater(self.window._config.design_set_at, 200)
        self.assertEqual(self.window._config.design_by, self.window._config.machine_id)
        self.assertEqual(self.sent[-1][1]["design_sync"]["values"]["effect_length"], "long")

    def test_a_design_state_in_a_malformed_settings_envelope_is_dropped_whole(self):
        peer = protocol.id_text(harness.B)
        self.pair_peer(peer)
        self.window._config.design_follow_peer = peer
        state = {"set_at": 200, "by": peer, "values": {"effect_length": "long"}}

        for outer in ({"on": "no"}, {"set_at": True}, {"by": "not an id"}):
            received = settings_sync.message_data(False, 100, by=peer, design_state=state)
            received.update(outer)
            self.window._on_settings(peer, received)

        self.assertEqual(self.window._config.effect_length, "normal")
        self.assertNotIn(peer, self.window._design_states)
        self.assertEqual(self.sent, [])

    def test_follow_accepts_the_leaders_next_change_after_this_pc_reannounces(self):
        peer = protocol.id_text(harness.B)
        self.pair_peer(peer)
        config = self.window._config
        config.design_follow_peer = peer
        config.design_set_at = 50
        config.design_by = peer

        for stamp, length in ((100, "long"), (101, "short")):
            received = settings_sync.message_data(
                False, stamp - 50, by=peer,
                design_state={"set_at": stamp, "by": peer, "values": {"effect_length": length}},
            )
            self.window._on_settings(peer, received)

        self.assertEqual(self.window._config.effect_length, "short")
        self.assertGreater(self.window._config.design_set_at, 101)
        self.assertEqual(self.window._config.design_by, self.window._config.machine_id)
        self.assertEqual(self.sent[-1][1]["design_sync"]["values"]["effect_length"], "short")
        self.assertEqual((self.sent[-1][1]["design_sync"]["set_at"], self.sent[-1][1]["design_sync"]["by"]),
                         (self.window._config.design_set_at, self.window._config.machine_id))

    def test_followed_size_updates_the_control_without_saving_a_local_edit(self):
        peer = protocol.id_text(harness.B)
        self.pair_peer(peer)
        self.window._config.design_follow_peer = peer
        received = settings_sync.message_data(
            False, 100, by=peer,
            design_state={"set_at": 200, "by": peer, "values": {"effect_size": "large"}},
        )

        self.window._on_settings(peer, received)

        self.assertEqual(self.window.size_choice.value, "large")
        self.assertEqual(self.window._config.effect_size, "large")
        self.assertEqual(self.sent[-1][1]["design_sync"]["values"]["effect_size"], "large")

    def test_same_on_design_change_is_announced_with_a_new_local_design_stamp(self):
        source = protocol.id_text(harness.C)
        config = self.window._config
        config.same_on_both = True
        config.same_set_at = 10
        config.same_by = source
        config.design_set_at = 20
        config.design_by = config.machine_id
        config.effect_length = "short"
        received = mac_message(True, 11, by=source, effect_length="long")
        received["design_sync"] = {
            "set_at": 100, "by": source, "values": {"effect_length": "long"},
        }

        self.window._on_settings(source, received)

        self.assertEqual(self.window._config.effect_length, "long")
        self.assertGreater(self.window._config.design_set_at, 100)
        self.assertEqual(self.window._config.design_by, self.window._config.machine_id)
        self.assertEqual(self.sent[-1][1]["design_sync"]["set_at"], self.window._config.design_set_at)
        self.assertNotEqual(self.sent[-1][1]["design_sync"]["by"], source)

    def test_same_off_from_another_machine_resumes_the_cached_followed_design(self):
        leader = protocol.id_text(harness.B)
        other = protocol.id_text(harness.C)
        self.pair_peer(leader)
        config = self.window._config
        config.same_on_both = True
        config.same_set_at = 10
        config.same_by = other
        config.design_follow_peer = leader
        config.effect_length = "short"
        self.window._design_states[leader] = {
            "set_at": 100, "by": leader, "values": {"effect_length": "long"},
        }
        apply_followed = mock.Mock(wraps=self.window._apply_followed_design)
        self.window._apply_followed_design = apply_followed

        self.window._on_settings(other, settings_sync.message_data(False, 11, by=other))

        apply_followed.assert_called_once()
        self.assertEqual(self.window._config.effect_length, "long")
        self.assertEqual(self.sent[-1][1]["design_sync"]["values"]["effect_length"], "long")
        self.assertEqual((self.sent[-1][1]["design_sync"]["set_at"], self.sent[-1][1]["design_sync"]["by"]),
                         (self.window._config.design_set_at, self.window._config.machine_id))

    def follow(self, **values):
        peer = protocol.id_text(harness.B)
        self.pair_peer(peer)
        for key, value in values.items():
            setattr(self.window._config, key, value)
        self.window._config.design_follow_peer = peer
        self.window._reflect_config(self.window._config)
        self.window._design_seen = self.window._design_fields()
        self.window._refresh_design_follow()
        return peer

    def followed_controls(self):
        w = self.window
        return [*w.length_choice._buttons.values(), *w.size_choice._buttons.values(), w.landing_toggle,
                *(w.glow_style_choice.tile(value) for value in self.window.effect_stills),
                *(w.switch_style_choice.tile(value) for value in self.window.switch_stills),
                *w.glow_colour_choice._swatches.values()]

    def test_following_locks_the_followed_design_controls_under_a_banner(self):
        self.follow()

        self.assertFalse(any(control.isEnabled() for control in self.followed_controls()))
        for control in (self.window.length_choice.view, self.window.size_choice.view,
                        self.window.glow_colour_choice.view, self.window.landing_toggle):
            self.assertAlmostEqual(control.graphicsEffect().opacity(), 0.45)
        for choice in (self.window.glow_style_choice, self.window.switch_style_choice):
            for tiles in choice.groups.values():
                self.assertAlmostEqual(tiles.view.graphicsEffect().opacity(), 0.45)
        self.assertTrue(self.window.glow_toggle.isEnabled())
        self.assertFalse(self.window.follow_banner.isHidden())
        self.assertEqual(self.window.follow_banner_label.text(), "Following MacBook Pro's design")

    def test_a_click_on_a_locked_control_changes_nothing_and_keeps_following(self):
        peer = self.follow(effect_length="long")
        self.sent.clear()

        self.window.length_choice._buttons["short"].click()
        self.window.landing_toggle.click()

        self.assertEqual(self.window.length_choice.value, "long")
        self.assertEqual(self.window._config.effect_length, "long")
        self.assertEqual(self.window._config.design_follow_peer, peer)
        self.assertEqual(self.sent, [])

    def test_stop_following_unlocks_the_controls_and_keeps_the_shown_design(self):
        self.follow(effect_length="long", glow_colour="ocean")

        self.window.stop_follow_button.click()

        self.assertEqual(self.window._config.design_follow_peer, "")
        self.assertTrue(all(control.isEnabled() for control in self.followed_controls()))
        self.assertTrue(self.window.follow_banner.isHidden())
        self.assertEqual((self.window.length_choice.value, self.window._config.effect_length), ("long", "long"))
        self.assertEqual((self.window.glow_colour_choice.value, self.window._config.glow_colour), ("ocean", "ocean"))
        self.assertEqual(self.sent[-1][1]["design_sync"]["values"]["effect_length"], "long")

    def test_unpairing_the_followed_machine_resets_the_selection(self):
        peer = protocol.id_text(harness.B)
        self.window._config.design_follow_peer = peer
        with mock.patch.object(self.window.book, "peers", return_value=[]), \
                mock.patch.object(self.window.book, "zones", return_value=[]):
            self.window._refresh_peers()

        self.assertEqual(self.window._config.design_follow_peer, "")

    def test_same_on_all_machines_keeps_precedence_over_a_followed_design(self):
        peer = protocol.id_text(harness.B)
        self.pair_peer(peer)
        self.window._config.design_follow_peer = peer
        self.window._config.same_on_both = True
        self.window._config.same_set_at = 100
        received = settings_sync.message_data(
            True, 101, settings_sync.pc_values(self.window._config), by=peer,
            design_state={"set_at": 202, "by": peer,
                          "values": {"effect_length": "long"}},
        )

        self.window._on_settings(peer, received)

        self.assertEqual(self.window._config.effect_length, "normal")
        self.assertEqual(self.window._config.design_follow_peer, protocol.id_text(harness.B))

    def test_an_older_peer_is_disabled_in_match_design_with(self):
        self.window._peer_entries = [harness.entry(harness.B, "Old PC")]
        self.peers = {harness.B: {"settings"}}
        self.window._design_peer_caps = lambda: {protocol.id_text(harness.B): {"settings"}}

        self.window._refresh_design_follow()

        self.assertFalse(self.window.design_follow_choice.model().item(1).isEnabled())
        self.assertIn("Needs beta.6 or later", self.window.design_follow_choice.itemText(1))

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
                         "Resistance, drag protection and Shortcut stay in step. Ways and jump keys below are local; each side is shared with its paired machine.")
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

    def test_a_state_that_goes_on_is_forwarded_with_this_pcs_direct_design_state(self):
        self.peers = {harness.B: {"settings"}, harness.C: {"settings"}}
        message = mac_message(True, 100)
        self.window._on_settings(protocol.id_text(harness.B), message)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], harness.C)
        forwarded = self.sent[0][1]
        self.assertEqual({key: value for key, value in forwarded.items() if key != "design_sync"},
                         {key: value for key, value in message.items() if key != "design_sync"})
        self.assertEqual(forwarded["design_sync"]["by"], self.window._config.machine_id)

    def test_a_design_change_here_while_off_announces_its_local_state(self):
        self.window._config.effect_length = "short"
        self.window._persist()
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(message["design_sync"]["values"]["effect_length"] == "short"
                            for _peer, message in self.sent))

    def test_announcing_carries_a_chosen_arrangement_the_peers_it_may_send_to_and_the_settings(self):
        settings = app_config.load_settings(self.path)
        settings["peers"][0]["id"] = protocol.id_text(harness.B)
        inactive = dict(settings["peers"][0], id=protocol.id_text(harness.C), token="inactive", in_use=False)
        settings["peers"].append(inactive)
        app_config.write_settings(self.path, settings)
        self.peers = {harness.B: {"settings"}}
        messages = self.window._announce(harness.B)
        self.assertEqual([m["type"] for m in messages], ["paired", "settings"])
        self.assertEqual(messages[0]["data"]["ids"], [])
        settings["peers"][0].update(side="left", side_set_at=500, side_by=self.window._config.machine_id)
        app_config.write_settings(self.path, settings)
        messages = self.window._announce(harness.B)
        self.assertEqual(messages[0]["type"], "arrangement")
        self.assertEqual(messages[0]["data"]["edge"], "left")
        self.assertEqual(messages[0]["data"]["set_at"], 500)


if __name__ == "__main__":
    unittest.main()
