"""The Design page's Style for switch shows the tiles it names, in the real window: built offscreen
from a synthetic config as the README's screenshots build it, which starts no receiver, hooks or
announcer."""

import json
import os
import tempfile
import unittest
from pathlib import Path

try:
    from PySide6.QtWidgets import QApplication

    import kvm_bridge_win
    from core.tests import responder_harness as harness
    import theme
except ImportError:  # PySide6 is only in the Windows venv
    kvm_bridge_win = None


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class StyleForTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        folder = Path(tempfile.mkdtemp())
        (folder / "config.json").write_text(json.dumps({
            "host": "192.168.1.20", "port": harness.free_port(), "auth_token": "synthetic", "paired_with": "MacBook Pro",
            "mac_host": "192.168.1.10", "mac_return_edge": "left", "shortcut_arrival": True,
        }))
        self.window = kvm_bridge_win.WindowsApplication(folder / "settings.json")
        self.window._select_page("design")
        self.window._refresh_window()

    def tearDown(self):
        self.window.server.stop()
        self.window.deleteLater()

    def shown(self):
        return {name for name, choice in (("crossing", self.window.glow_style_choice),
                                          ("switch", self.window.switch_style_choice))
                if not choice.view.isHidden()}

    def test_each_choice_shows_its_own_tiles_every_time(self):
        self.assertEqual(self.shown(), {"crossing"})
        for choice in ("switch", "crossing", "switch", "crossing"):
            self.window.design_mode_choice._buttons[choice].click()
            self.app.processEvents()
            self.assertEqual(self.window.design_mode_choice.value, choice)
            self.assertEqual(self.shown(), {choice})


if __name__ == "__main__":
    unittest.main()
