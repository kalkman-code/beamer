"""The shell starts as it would on Linux: sys.platform is faked in a child process (PySide6 and the
stdlib are already loaded by then) and the window is built offscreen. On Wayland the portal parts
are chosen, the capture is wired to the sender's zones and its way home, and main() also runs to a
clean exit with no desktop portal to reach. On X11 the X11 parts are chosen, the capture starts on
a fake X connection holding the grab predicate, and the receiver listens. Either way, of Windows'
modules only the pure pieces capture_x11 borrows are loaded. Skipped on
Windows, where a faked platform would break the stdlib; the rig proves the other half, that
Windows is unchanged."""

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
# capture_x11 borrows Windows' pure pieces (the trigger, the mouse messages, the US places).
X11_ALLOWED = {"capture_win", "input_injector"}


class FakeX:
    """Just enough of capture_x11's connection for the hooks to start and idle."""

    def __init__(self):
        import capture_x11

        self.devices_read = 0
        self.masters = [capture_x11.Device(2, capture_x11.XI_MASTER_POINTER, "Virtual core pointer"),
                        capture_x11.Device(3, capture_x11.XI_MASTER_KEYBOARD, "Virtual core keyboard")]

    def events(self, timeout):
        import time

        time.sleep(timeout)
        return []

    def devices(self):
        self.devices_read += 1
        return list(self.masters)

    def pointer_mapping(self):
        return list(range(1, 11))

    def close(self):
        pass


def child(mode: str, config: Path) -> None:
    import socket
    import tempfile  # noqa: F401  (loaded before the platform changes)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    here = Path(__file__).resolve().parent.parent
    sys.path[:0] = [str(here), str(here.parent)]
    sys.platform = "linux"

    if mode == "x11":
        x11_child()
        return

    import kvm_bridge_win
    from core.receiver import ServerState

    if mode == "window":
        import platform_parts

        assert [platform_parts.capture.__name__, platform_parts.injector.__name__, platform_parts.clipboard.__name__,
                platform_parts.desktop.__name__, platform_parts.autostart.__name__] == [
            "capture_portal", "inject_portal", "clipboard_portal", "desktop_portal", "autostart_x11"]
        app = QApplication([])
        window = kvm_bridge_win.WindowsApplication(config)
        window.start()
        assert window.windowTitle() == "Beamer"
        assert "Wayland support is coming" not in window._status_detail, window._status_detail
        import time

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "not yet on Linux" not in window.firewall_note.text():
            app.processEvents()
            time.sleep(0.05)
        assert "not yet on Linux" in window.firewall_note.text(), window.firewall_note.text()
        assert not window.logon_switch.isEnabled()
        assert window.hooks._predicate is not None and window.hooks._predicate() is False
        assert window.hooks._models == window.sender.zone_models
        assert window.hooks.on_home is not None
        assert platform_parts.injector.on_problem is not None
        # Nothing is captured, so the shortcut cannot send input away.
        assert window.sender.input_held() is False
        assert window._identity()["platform"] == "linux"
        # What Wayland takes away, said where it applies, and no window drawn at an edge.
        assert platform_parts.WAYLAND is True
        assert "only brings input back" in window.wayland_shortcut_note.text()
        assert not window.wayland_shortcut_note.isHidden()
        assert window.hold_switch.isHidden() and window.crossing_rows["dragging"].isHidden()
        assert "full-screen" in window.wayland_crossing_note.text() and not window.wayland_crossing_note.isHidden()
        window._on_pressure("right", 0.7, False)
        window._on_arrival("edge", "left", 10.0, 10.0)
        assert window.glow is None and window.effects is None
        window._reflect_look()
        assert "Wayland" in window.effect_note.text(), window.effect_note.text()
        window.reload_config()
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
    loaded = [name for name in WINDOWS_ONLY if name in sys.modules and name not in X11_ALLOWED]
    assert not loaded, f"Windows-only modules imported: {loaded}"
    print("ok")


def x11_child() -> None:
    import socket
    import time

    from PySide6.QtWidgets import QApplication

    import capture_x11
    import desktop_x11

    fake = FakeX()
    capture_x11._Connection = lambda: fake

    def no_server():
        raise RuntimeError("no X server in this test")

    desktop_x11._connect = no_server

    import kvm_bridge_win
    import platform_parts

    assert platform_parts.capture is capture_x11
    assert platform_parts.injector.__name__ == "inject_x11"
    assert platform_parts.clipboard.__name__ == "clipboard_x11"
    assert platform_parts.autostart.__name__ == "autostart_x11"
    assert platform_parts.desktop is desktop_x11
    app = QApplication([])
    window = kvm_bridge_win.WindowsApplication(Path(sys.argv[2]))
    window.start()
    assert "Wayland" not in window._status_detail, window._status_detail
    assert window._identity()["platform"] == "linux"
    assert "gestures" not in window._identity()["caps"] and "text" in window._identity()["caps"]
    assert platform_parts.WAYLAND is False and window.wayland_shortcut_note.isHidden() and not window.hold_switch.isHidden()
    assert window.hooks._predicate is not None and window.hooks._predicate() is False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not window.server.listening:
        app.processEvents()
        time.sleep(0.05)
    assert window.server.listening
    socket.create_connection(("127.0.0.1", PORT), timeout=2).close()
    assert fake.devices_read >= 1, "the capture never started"
    window.quit()
    window.deleteLater()
    loaded = [name for name in WINDOWS_ONLY if name in sys.modules and name not in X11_ALLOWED]
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
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen", XDG_CONFIG_HOME=directory, XDG_SESSION_TYPE="wayland")
            env.pop("LOCALAPPDATA", None)
            for mode in ("window", "main"):
                done = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), mode, str(config)],
                    env=env, capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(done.returncode, 0, mode + done.stdout + done.stderr)
                self.assertIn("ok", done.stdout)

    @unittest.skipIf(sys.platform == "win32", "a faked platform breaks the Windows stdlib")
    def test_on_x11_the_x11_parts_start_and_the_receiver_listens(self):
        try:
            import PySide6  # noqa: F401
        except ImportError:
            self.skipTest("needs PySide6")
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from core import protocol

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import app_config

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.json"
            # A machine paired by 1.5.0, so the hooks go in: neither a typed token nor one migrated
            # from 1.4.x links (protocol.linkable).
            settings = app_config.migrate({
                "host": "192.0.2.20", "port": PORT, "auth_token": protocol.id_text(bytes(range(32))), "paired_with": "MacBook Pro",
                "mac_host": "192.0.2.10", "mac_return_edge": "left",
                "allow_mac_to_drive": True, "send_to_mac": True,
            })
            settings["peers"][0]["from_1_4"] = False
            config.write_text(json.dumps(settings))
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen", XDG_CONFIG_HOME=directory, XDG_SESSION_TYPE="x11", DISPLAY=":0")
            env.pop("LOCALAPPDATA", None)
            env.pop("WAYLAND_DISPLAY", None)
            done = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "x11", str(config)],
                env=env, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("ok", done.stdout)


class SessionTest(unittest.TestCase):
    def test_which_display_server(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import platform_parts

        session = platform_parts.session
        self.assertEqual(session({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}), "x11")
        self.assertEqual(session({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}), "wayland")
        self.assertEqual(session({"XDG_SESSION_TYPE": "tty", "DISPLAY": ":0"}), "x11")  # startx
        self.assertEqual(session({"DISPLAY": ":1", "WAYLAND_DISPLAY": "wayland-0"}), "wayland")
        self.assertEqual(session({}), "wayland")


if __name__ == "__main__":
    child(sys.argv[1], Path(sys.argv[2]))
