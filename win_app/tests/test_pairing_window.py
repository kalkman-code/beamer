"""What the PC's window says when a code ends, in the real window built offscreen as
tools/readme_shots_win.py builds it, which starts no receiver, hooks or announcer."""

import json
import os
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import pairing

try:
    from PySide6.QtWidgets import QApplication

    import kvm_bridge_win
    import theme
except ImportError:  # PySide6 is only in the Windows venv
    kvm_bridge_win = None


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class PairingOutcomeWindowTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        config = Path(tempfile.mkdtemp()) / "config.json"
        config.write_text(json.dumps({"host": "192.0.2.20", "port": 24820}))
        self.window = kvm_bridge_win.WindowsApplication(config)
        self.host = self.window.announcer.host
        self.window.announcer.begin_pairing()
        self.window._refresh_pairing()

    def tearDown(self):
        self.window.deleteLater()

    def test_a_mac_on_the_old_exchange_is_named_as_out_of_date(self):
        self.host.handle({"type": "pair_request", "pair": self.host.pair_id, "pub": "AAAA", "proof": "BBBB"})
        self.window._refresh_pairing()
        self.assertEqual(
            self.window.pair_note.text(),
            "The Mac runs a different version of Beamer. Update Beamer on both machines, then pair again.",
        )

    def test_a_mac_that_had_the_wrong_code_ends_the_code(self):
        client = pairing.PairingClient(self.host.pair_id, "000000" if self.host.code != "000000" else "000001", "Test Mac")
        self.host.handle(client.start())
        self.assertIsNotNone(self.window.announcer.code)
        self.host.handle(client.abort())
        self.window._refresh_pairing()
        self.assertIsNone(self.window.announcer.code)
        self.assertEqual(
            self.window.pair_note.text(),
            "A wrong code was entered, so that code is cancelled. Pair again for a fresh one.",
        )


if __name__ == "__main__":
    unittest.main()
