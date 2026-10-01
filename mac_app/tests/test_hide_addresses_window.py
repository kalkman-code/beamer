"""Hide addresses in the real settings window: the pairing sheet's typed address, the machines list
and what they say. Built as the README's screenshots build it, never put on screen, starting no network."""

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
import settings_store  # noqa: E402
from wake import WakingController  # noqa: E402


class HideAddressesWindowTests(unittest.TestCase):
    """Codex, the final pass on 28-09-2026: the address typed under "PC not listed?" stayed plain,
    the no-answer message repeated it, and a found PC's screen-reader label carried its address."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "config.json"
        raw = settings_store.config_to_raw(settings_store.editable_default_config())
        raw.update(host="192.0.2.20", auth_token="synthetic", hide_addresses=True)
        path.write_text(json.dumps(raw))
        store = settings_store.SettingsStore(path)
        logger = logging.getLogger("test-hide-addresses")
        with mock.patch.object(kvm_bridge_app, "accessibility_granted", return_value=True), \
                mock.patch.object(kvm_bridge_app, "input_monitoring_granted", return_value=True):
            self.window = kvm_bridge_app.ControlWindow.alloc().initWithController_settingsStore_logger_(
                WakingController(store.load(), logger=logger), store, logger
            )
        self.service = mock.Mock(machines=mock.Mock(return_value=[]), error=None, code=None)
        self.window.panel.service = self.service

    def tearDown(self):
        self.window.appearance_watch.stop()
        self.directory.cleanup()

    def test_the_typed_address_is_dots_and_is_never_said_back(self):
        panel = self.window.panel
        plain_box, secret_box = panel.find_boxes
        self.assertTrue(plain_box.isHidden())
        self.assertFalse(secret_box.isHidden())
        panel.find_secret.setStringValue_("192.0.2.10")
        for field, digit in zip(panel.code_boxes.fields, "123456"):
            field.setStringValue_(digit)
        with mock.patch.object(kvm_bridge_app.machines_panel.threading, "Thread"):
            panel._pair()
        self.assertIn("Pairing with", panel.pair_status.text)
        self.assertNotIn("192.0.2.10", panel.pair_status.text)

    def test_switching_it_off_shows_the_typed_address_again(self):
        panel = self.window.panel
        panel.find_secret.setStringValue_("192.0.2.10")
        self.window._set_hide_addresses(False)
        self.assertEqual(panel.find_field.stringValue(), "192.0.2.10")
        self.assertFalse(panel.find_boxes[0].isHidden())

    def test_the_footer_hides_an_address_in_a_failed_switch(self):
        # Codex, 29-09-2026: a failed switch put the connection status, which names the PC's
        # address, in the footer as it was.
        self.window._say("Connecting to 192.0.2.20:24820", "fault")
        self.assertNotIn("192.0.2.20", self.window.message_label.view.stringValue())

    def test_an_unnamed_machine_is_not_named_by_its_address(self):
        # labels() falls back to the address when a machine has no name.
        self.window.controller.cfg.pc_name = ""
        peer = self.window.controller.book.peers()[0]
        self.window.settings_store.set_peer(peer["token"], send=True)
        settings = self.window.settings_store.current()
        settings["peers"][0]["name"] = ""
        self.window.settings_store.save_settings(settings)
        self.window.panel.refresh()
        texts = []

        def walk(view):
            if isinstance(view, kvm_bridge_app.AppKit.NSTextField):
                texts.append(view.stringValue())
            texts.append(view.accessibilityLabel() or "")
            for sub in view.subviews():
                walk(sub)

        for row in self.window.panel.list.arrangedSubviews():
            walk(row)
        self.assertTrue(texts)
        self.assertNotIn("192.0.2.20", " ".join(texts))


class ReloadTests(unittest.TestCase):
    def test_a_reload_sets_the_controller_before_the_window_reads_it(self):
        # Codex, 28-09-2026: the window was filled before the controller had the reloaded config,
        # so both address fields followed the old Hide addresses.
        calls = []
        tray = mock.Mock()
        tray.settings_store.load.return_value = "cfg"
        tray.controller.update_config.side_effect = lambda cfg: calls.append("controller")
        tray.control_window._load.side_effect = lambda raw: calls.append("window")
        with mock.patch.object(kvm_bridge_app, "config_to_raw", return_value={}):
            kvm_bridge_app.TrayApp.reload_config(tray, None)
        self.assertEqual(calls, ["controller", "window"])


class NotificationTests(unittest.TestCase):
    def test_a_notification_hides_the_pcs_address(self):
        tray = mock.Mock()
        tray.controller.cfg.hide_addresses = True
        with mock.patch.object(kvm_bridge_app.rumps, "notification") as notification, \
                mock.patch.object(kvm_bridge_app.AppKit, "NSBeep"):
            kvm_bridge_app.TrayApp._notify_user_main(tray, "Cannot switch", "Connecting to 192.0.2.20:24820")
        self.assertNotIn("192.0.2.20", notification.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
