"""Hide addresses in the real window, built offscreen as the README's screenshots build it, which
starts no receiver, hooks or announcer."""

import json
import os
import tempfile
import unittest
from pathlib import Path

try:
    from PySide6.QtWidgets import QApplication

    import kvm_bridge_win
    import theme
except ImportError:  # PySide6 is only in the Windows venv
    kvm_bridge_win = None


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class HideAddressesWindowTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        folder = Path(tempfile.mkdtemp())
        self.config = folder / "settings.json"
        self.raw = {"host": "192.0.2.20", "port": 24820, "auth_token": "synthetic", "paired_with": "MacBook Pro",
                    "mac_host": "192.0.2.10", "mac_return_edge": "left"}
        (folder / "config.json").write_text(json.dumps(self.raw))
        self.window = kvm_bridge_win.WindowsApplication(self.config)

    def tearDown(self):
        self.window.deleteLater()

    def edit_settings(self, **changes):
        self.config.write_text(json.dumps({**json.loads(self.config.read_text()), **changes}))

    def test_a_reload_that_turns_it_on_hides_this_pcs_and_each_machines_address(self):
        # The final bug pass, 28-09-2026: the tray's Reload configuration set the switch on but left
        # the address readable, as the switch's handler never ran.
        self.edit_settings(hide_addresses=True)
        self.window.reload_config()
        self.assertTrue(self.window.hide_switch.isChecked())
        self.assertNotIn("192.0.2", self.window.host_readout.text())
        row = next(iter(self.window.machines.rows.values()))
        self.assertNotIn("192.0.2.10", row.where.text())

    def test_the_directions_note_hides_the_macs_address(self):
        # The feature shoot, 29-09-2026: with it on, the note under Send input across still
        # read "Connected to the Mac at" and its address.
        self.edit_settings(hide_addresses=True)
        self.window.reload_config()
        self.window._sending_detail = "Connected to the Mac at 192.0.2.10"
        self.window._refresh_window()
        self.assertIn("Connected to the Mac at", self.window.send_hint.text())
        self.assertNotIn("192.0.2.10", self.window.send_hint.text())

    def test_a_tray_alert_hides_the_macs_address(self):
        # "Cannot switch — <status>" carries the sender's status, which can name the Mac's address.
        self.edit_settings(hide_addresses=True)
        self.window.reload_config()
        shown = []
        self.window.tray.showMessage = lambda title, message, *rest: shown.append(message)
        self.window._on_alert("Beamer", "Cannot switch — Connected to the Mac at 192.0.2.10")
        self.assertEqual(shown, ["Cannot switch — Connected to the Mac at •••"])


if __name__ == "__main__":
    unittest.main()
