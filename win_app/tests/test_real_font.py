"""Keep the real-font full-window resize path safe in an isolated Qt process."""

import os
import subprocess
import sys
import unittest


def _run_real_font_window():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from pathlib import Path
    import tempfile

    root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(root / "win_app"), str(root)]
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    import app_config
    import kvm_bridge_win
    import theme
    import tokens
    from core import protocol
    from core.tests import responder_harness as harness

    class NullSignal:
        def connect(self, _callback):
            pass

    class NullAction:
        def __init__(self):
            self.triggered = NullSignal()

        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    class NullTray:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    class RenderWindow(kvm_bridge_win.WindowsApplication):
        def _build_tray(self):
            self.tray = NullTray()
            self.header_action = NullAction()
            self.open_action = NullAction()
            self.status_action = NullAction()
            self.redirect_action = NullAction()
            self.pause_action = NullAction()
            self.drive_action = NullAction()
            self.send_action = NullAction()
            self.tray_menu = NullTray()

    app = QApplication.instance() or QApplication([])
    theme.init_fonts()
    app.setFont(theme.font(theme.TYPE["body"]))
    assert theme.sans() == tokens.UI_FAMILY
    work_root = Path(__file__).resolve().parents[2] / "renders" / "test-working"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work_root) as directory:
        config_path = Path(directory) / "settings.json"
        settings = app_config.migrate(None, machine_id=protocol.id_text(harness.HERE))
        settings["port"] = harness.free_port()
        app_config.write_settings(config_path, settings)
        window = RenderWindow(config_path)
        window.refresh_timer.stop()
        window.full_screen_timer.stop()
        window._select_page("design")
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        try:
            window.show()
            window.resize(640, 760)
            app.processEvents()
        finally:
            window.server.stop()
            window.close()


@unittest.skipUnless(sys.platform == "win32", "the stack overflow is in Windows Qt offscreen")
class RealFontResizeTests(unittest.TestCase):
    def test_full_design_window_resizes_with_the_bundled_font(self):
        environment = os.environ.copy()
        environment["BEAMER_REAL_FONT_CHILD"] = "1"
        environment["QT_QPA_PLATFORM"] = "offscreen"
        result = subprocess.run(
            [sys.executable, __file__],
            env=environment,
            capture_output=True,
            text=True,
            timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    if os.environ.get("BEAMER_REAL_FONT_CHILD"):
        _run_real_font_window()
    else:
        unittest.main()
