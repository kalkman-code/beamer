"""Same on both machines in the real window: the Overview switch, a PC's settings arriving, and a
change here going out. Built as test_appearance builds it, never put on screen; the two links'
sends are recorded instead."""

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import AppKit  # noqa: E402

AppKit.NSApplication.sharedApplication()

import kvm_bridge_app  # noqa: E402
import pages  # noqa: E402
import settings_store  # noqa: E402
from bridge_fakes import PAIRED_TOKEN  # noqa: E402
from core import protocol, settings_sync  # noqa: E402
from fake_link import FakeLink, bring_up  # noqa: E402
from wake import WakingController  # noqa: E402

PEER = protocol.id_text(bytes(range(1, 17)))
LOW = protocol.id_text(bytes(15) + b"\x01")
HIGH = protocol.id_text(b"\xff" * 16)


def pc_message(on, set_at, by="", **values):
    base = dict(shortcut=False, resistance_px=60,
                block_while_dragging=False, trigger_key="cmd_r", trigger_style="hold", double_tap_ms=400,
                glow_style="beam", glow_colour="ocean", effect_length="long", shortcut_arrival=False,
                shortcut_arrival_style="locator")
    base.update(values)
    return settings_sync.message_data(on, set_at, base, by=by)


class SameOnBothTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "config.json"
        raw = settings_store.config_to_raw(settings_store.editable_default_config())
        raw.update(host="192.0.2.20", auth_token=PAIRED_TOKEN)
        raw["crossing"]["methods"] = ["shortcut", "edge", "notch"]
        path.write_text(json.dumps(raw))
        self.store = settings_store.SettingsStore(path)
        logger = logging.getLogger("test-same-on-both")
        with mock.patch.object(kvm_bridge_app, "accessibility_granted", return_value=True), \
                mock.patch.object(kvm_bridge_app, "input_monitoring_granted", return_value=True):
            self.window = kvm_bridge_app.ControlWindow.alloc().initWithController_settingsStore_logger_(
                WakingController(self.store.load(), logger=logger, link_factory=FakeLink), self.store, logger
            )
        self.addCleanup(self.window.appearance_watch.stop)
        self.sent = []
        self.sources = []
        self.window.controller.send_settings = lambda data, source=None: self.sent.append(data) or self.sources.append(source) or True
        self.window._show_same()

    def test_off_by_default_with_each_page_its_own(self):
        self.assertFalse(self.window.same_switch.value)
        self.assertEqual(self.window.scope_labels["design"].text, pages.SCOPE["design"])
        self.assertTrue(all(note.view.isHidden() for note in self.window.own_notes))

    def test_the_pcs_settings_arrive_and_this_macs_own_stay(self):
        self.window.apply_same(pc_message(True, 100))
        cfg = self.window.controller.cfg
        self.assertTrue(cfg.same_on_both)
        self.assertEqual(cfg.crossing["methods"], ["edge", "notch"])
        self.assertEqual((cfg.crossing["glow_style"], cfg.crossing["effect_length"]), ("beam", "long"))
        self.assertEqual((cfg.trigger_key, cfg.trigger_style, cfg.double_tap_ms), ("cmd_r", "hold", 400))
        self.assertEqual(self.store.load().crossing["glow_colour"], "ocean")
        self.assertEqual(self.window.glow_style_select.value, "beam")
        self.assertEqual(self.window.resistance_ruler.value, 60)
        self.assertTrue(self.window.same_switch.value)
        self.assertEqual(self.window.scope_labels["crossing"].text,
                         f"Kept the same as {self.window.controller.peer_label}. Change it on any machine.")

    def test_arriving_settings_leave_a_half_typed_address_and_the_pairing_card(self):
        self.window.host_field.setStringValue_("192.0.2.99")
        self.window.panel._say("That code was not accepted.", "fault")
        self.window.apply_same(pc_message(True, 100))
        self.assertEqual(self.window.host_field.stringValue(), "192.0.2.99")
        self.assertEqual(self.window.panel.pair_status.text, "That code was not accepted.")

    def test_the_hold_switch_reaches_the_controller_before_the_debounce(self):
        self.window.controller.full_screen_app = "Game"
        self.window.hold_box._toggle()
        self.assertIs(self.window.controller.cfg.crossing["hold_full_screen"], False)
        self.assertIsNone(self.window.controller.full_screen_app)
        self.window.hold_box._toggle()
        self.assertEqual(self.window.controller.full_screen_app, "Game")

    def test_settings_arriving_from_the_pc_leave_the_hold_switch_as_chosen(self):
        self.window.hold_box._toggle()
        self.window.apply_same(pc_message(True, 100))
        self.assertFalse(self.window.hold_box.value)
        self.window._apply_settings()
        self.assertFalse(self.store.load().crossing["hold_full_screen"])

    def test_an_older_message_changes_nothing(self):
        self.window.apply_same(pc_message(True, 100))
        self.window.apply_same(pc_message(True, 90, glow_style="glow"))
        self.assertEqual(self.window.controller.cfg.crossing["glow_style"], "beam")

    def test_turning_it_on_here_sends_this_macs_values(self):
        self.window._set_same(True)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0]["on"])
        self.assertIs(self.sent[0]["crossing"]["shortcut"], True)

    def test_a_change_here_while_on_is_sent_and_one_to_this_macs_own_is_not(self):
        self.window._set_same(True)
        self.sent.clear()
        self.window.notch_after_select.value = 3000
        self.window._apply_settings()
        self.assertEqual(self.sent, [])
        self.window.length_select.value = "short"
        self.window._apply_settings()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["design"]["effect_length"], "short")

    def test_a_change_here_while_off_goes_nowhere(self):
        self.window.length_select.value = "short"
        self.window._apply_settings()
        self.assertEqual(self.sent, [])

    def test_an_old_pc_turns_the_switch_off_and_says_why(self):
        self.window._set_same(True)
        bring_up(self.window.controller, caps=("clipboard",))
        self.assertIs(self.window.controller.peer_settings, False)
        self.window._show_same()
        self.assertFalse(self.window.same_switch.value)
        self.assertIn("too old", self.window.same_note.text)

    def test_a_peers_state_is_sent_on_unchanged_to_the_others_but_not_back_to_it(self):
        data = pc_message(True, 100, by=PEER)
        self.window.apply_same(data, peer=PEER)
        self.assertEqual(self.sent, [data])
        self.assertEqual(self.sources, [PEER])

    def test_who_made_the_change_is_kept_with_it(self):
        self.window.apply_same(pc_message(True, 100, by=PEER), peer=PEER)
        self.assertEqual(self.window.controller.cfg.same_by, PEER)
        self.assertEqual(self.store.load().same_by, PEER)

    def test_a_change_made_here_says_it_was_made_by_this_mac(self):
        self.window._set_same(True)
        own = self.store.current()["machine_id"]
        self.assertEqual(self.sent[0]["by"], own)
        self.assertEqual(self.window.controller.cfg.same_by, own)

    def test_the_same_second_from_a_larger_id_is_taken_and_from_a_smaller_is_not(self):
        self.window.apply_same(pc_message(True, 100, by=PEER, glow_style="beam"), peer=PEER)
        self.window.apply_same(pc_message(True, 100, by=LOW, glow_style="glow"), peer=LOW)
        self.assertEqual(self.window.controller.cfg.crossing["glow_style"], "beam")
        self.window.apply_same(pc_message(True, 100, by=HIGH, glow_style="glow"), peer=HIGH)
        self.assertEqual(self.window.controller.cfg.crossing["glow_style"], "glow")
        self.assertEqual(self.window.controller.cfg.same_by, HIGH)

    def test_the_copy_that_arrives_over_the_second_link_changes_nothing_and_is_not_sent_on(self):
        data = pc_message(True, 100, by=PEER)
        self.window.apply_same(data, peer=PEER)
        self.window.apply_same(data, peer=PEER)
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
