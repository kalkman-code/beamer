"""The shell starts as it would on Linux: sys.platform is faked in a child process (PySide6 and the
stdlib are already loaded by then), the window is built offscreen and main() runs to a clean exit.
Nothing Windows-only may be imported, and nothing may listen. Skipped on Windows, where a faked
platform would break the stdlib; the rig proves the other half, that Windows is unchanged."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WINDOWS_ONLY = (
    "capture_win", "desktop_win", "clipboard_win", "autostart_win", "firewall_win", "unlock_win",
    "input_injector", "touchpad_injector", "winreg", "msvcrt",
)
PORT = 24993


def child(mode: str, config: Path) -> None:
    import socket
    import tempfile  # noqa: F401  (loaded before the platform changes)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    here = Path(__file__).resolve().parent.parent
    sys.path[:0] = [str(here), str(here.parent)]
    sys.platform = "linux"

    import kvm_bridge_win
    from core.receiver import ServerState

    if mode == "window":
        app = QApplication([])
        window = kvm_bridge_win.WindowsApplication(config)
        window.start()
        assert window.windowTitle() == "Beamer"
        assert window._status is ServerState.STOPPED, window._status
        assert "not built yet" in window._status_detail, window._status_detail
        assert "not yet on Linux" in window.firewall_note.text(), window.firewall_note.text()
        assert not window.logon_switch.isEnabled()
        window.reload_config()
        assert window.server._listener is None and not window.sender.connected
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.5).close()
        except OSError:
            pass
        else:
            raise AssertionError("something is listening")
        window.quit()
        window.deleteLater()
    else:
        original = kvm_bridge_win.WindowsApplication.start

        def start_then_quit(self):
            original(self)
            QTimer.singleShot(200, QApplication.quit)

        kvm_bridge_win.WindowsApplication.start = start_then_quit
        sys.argv = ["beamer", "--config", str(config), "--hidden"]
        try:
            kvm_bridge_win.main()
        except SystemExit as exit_:
            assert exit_.code in (0, None), exit_.code
    loaded = [name for name in WINDOWS_ONLY if name in sys.modules]
    assert not loaded, f"Windows-only modules imported: {loaded}"
    print("ok")


class LinuxStartTest(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "a faked platform breaks the Windows stdlib")
    def test_the_shell_starts_builds_its_window_and_quits_with_no_windows_module(self):
        try:
            import PySide6  # noqa: F401
        except ImportError:
            self.skipTest("needs PySide6")
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.json"
            (Path(directory) / "config.json").write_text(json.dumps({
                "host": "192.0.2.20", "port": PORT, "auth_token": "synthetic", "paired_with": "MacBook Pro",
                "mac_host": "192.0.2.10", "mac_return_edge": "left",
                "allow_mac_to_drive": True, "send_to_mac": True,
            }))
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen", XDG_CONFIG_HOME=directory)
            env.pop("LOCALAPPDATA", None)
            for mode in ("window", "main"):
                done = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), mode, str(config)],
                    env=env, capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(done.returncode, 0, mode + done.stdout + done.stderr)
                self.assertIn("ok", done.stdout)


if __name__ == "__main__":
    child(sys.argv[1], Path(sys.argv[2]))
