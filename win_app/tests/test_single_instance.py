import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QWidget

import kvm_bridge_win


class SingleInstanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_hidden_second_launch_does_not_raise_the_existing_window(self):
        with mock.patch.object(kvm_bridge_win, "send_instance_activation") as activate:
            self.assertFalse(kvm_bridge_win.activate_existing_instance(hidden=True))
        activate.assert_not_called()

    def test_relaunched_instance_waits_until_the_holder_releases_the_lock(self):
        now = [0.0]
        holder = [True]

        def claim():
            return None if holder[0] else lambda: None

        def sleep(duration):
            now[0] += duration
            if now[0] >= 0.2:
                holder[0] = False

        release = kvm_bridge_win._claim_instance_waiting(
            1, claim=claim, sleep=sleep, clock=lambda: now[0]
        )
        self.assertIsNotNone(release)

    def test_relaunched_instance_gives_up_after_the_wait_window(self):
        now = [0.0]
        claims = []

        def claim():
            claims.append(now[0])
            return None

        def sleep(duration):
            now[0] += duration

        release = kvm_bridge_win._claim_instance_waiting(
            0.25, claim=claim, sleep=sleep, clock=lambda: now[0]
        )
        self.assertIsNone(release)
        self.assertAlmostEqual(now[0], 0.25)
        self.assertEqual(len(claims), 4)

    def test_relaunch_marks_replacement_and_quits_through_application(self):
        class SettingsPage:
            relaunch = kvm_bridge_win.WindowsApplication.relaunch

            def __init__(self):
                self.quit = mock.Mock()

        page = SettingsPage()
        with mock.patch.object(sys, "argv", ["beamer.exe", "--hidden", "--relaunched"]), \
                mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(kvm_bridge_win.QProcess, "startDetached", return_value=(True, 123)) as start, \
                mock.patch.object(kvm_bridge_win.QApplication, "quit") as app_quit:
            page.relaunch()
        start.assert_called_once_with(sys.executable, ["--hidden", "--relaunched"])
        page.quit.assert_called_once_with()
        app_quit.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows receiver mutex')
    def test_two_logon_tasks_cannot_claim_two_receivers(self):
        import ctypes
        import uuid

        name = 'Local\\Beamer.Autostart.Test.' + uuid.uuid4().hex
        kernel32 = kvm_bridge_win._windows_kernel32()
        create_mutex = kernel32.CreateMutexW

        def isolated_mutex(attributes, owner, _name):
            return create_mutex(attributes, owner, name)

        with mock.patch.object(kernel32, 'CreateMutexW', side_effect=isolated_mutex), \
             mock.patch.object(kvm_bridge_win, '_windows_kernel32', return_value=kernel32):
            release = kvm_bridge_win._claim_instance()
        self.assertIsNotNone(release)
        self.addCleanup(release)
        code = (
            'import ctypes, sys, kvm_bridge_win\n'
            'kernel32 = kvm_bridge_win._windows_kernel32()\n'
            'create_mutex = kernel32.CreateMutexW\n'
            'kernel32.CreateMutexW = lambda a, o, n: create_mutex(a, o, sys.argv[1])\n'
            'kvm_bridge_win._windows_kernel32 = lambda: kernel32\n'
            'release = kvm_bridge_win._claim_instance()\n'
            'if release is not None: release()\n'
            'sys.exit(0 if release is None else 1)\n'
        )
        result = subprocess.run([sys.executable, '-c', code, name], timeout=30,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_visible_second_launch_still_raises_the_existing_window(self):
        with mock.patch.object(kvm_bridge_win, "send_instance_activation", return_value=True) as activate:
            self.assertTrue(kvm_bridge_win.activate_existing_instance(hidden=False))
        activate.assert_called_once()

    def test_startup_migrates_the_legacy_task_before_single_instance_handling(self):
        order = []
        with mock.patch.object(kvm_bridge_win, "parse_args", return_value=type("Args", (), {"hidden": True, "relaunched": False, "identify_x11": None})()), \
             mock.patch.object(kvm_bridge_win, "prepare_windows_startup", side_effect=lambda: order.append("migrate")), \
             mock.patch.object(kvm_bridge_win, "_claim_instance", side_effect=lambda: order.append("claim") or None), \
             mock.patch.object(kvm_bridge_win, "activate_existing_instance") as activate:
            kvm_bridge_win.main()
        self.assertEqual(order, ["migrate", "claim"])
        activate.assert_called_once_with(True, mock.ANY)

    def test_failed_migration_still_reaches_instance_handling(self):
        with mock.patch.object(kvm_bridge_win, "parse_args", return_value=type("Args", (), {"hidden": True, "relaunched": False, "identify_x11": None})()), \
             mock.patch.object(kvm_bridge_win, "prepare_windows_startup", side_effect=OSError("Access is denied")), \
             mock.patch.object(kvm_bridge_win, "_claim_instance", return_value=None) as claim, \
             mock.patch.object(kvm_bridge_win, "activate_existing_instance"), \
             self.assertLogs(kvm_bridge_win.LOGGER, level="ERROR"):
            kvm_bridge_win.main()
        claim.assert_called_once()

    def test_second_launch_signals_the_running_instance_with_activation_token(self):
        name = str(Path(__file__).resolve().parents[1] / f"s{os.getpid()}")
        received = []
        server = kvm_bridge_win.InstanceSignal(lambda token: received.append(token), name=name)
        try:
            self.assertTrue(server.listening)
            # A second launch is another process, and a client in this one cannot finish its write
            # to a Windows named pipe while the server waits on this thread's event loop.
            code = (
                "import sys\n"
                "from PySide6.QtCore import QCoreApplication\n"
                "app = QCoreApplication([])\n"
                "import kvm_bridge_win\n"
                "ok = kvm_bridge_win.send_instance_activation(sys.argv[1], 'gnome-token', timeout_ms=3000)\n"
                "sys.exit(0 if ok else 1)\n"
            )
            client = subprocess.Popen(
                [sys.executable, "-c", code, name], cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            try:
                deadline = time.monotonic() + 30
                while client.poll() is None and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.01)
                self.assertEqual(client.poll(), 0)
            finally:
                if client.poll() is None:
                    client.kill()
                client.wait()
            deadline = time.monotonic() + 1
            while not received and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertEqual(received, ["gnome-token"])
        finally:
            server.close()

    def test_control_command_answers_with_one_json_line_and_does_not_activate(self):
        name = str(Path(__file__).resolve().parents[1] / f"c{os.getpid()}")
        activated = []
        server = kvm_bridge_win.InstanceSignal(
            lambda token: activated.append(token), name=name, commands={"status": lambda: {"paused": True}}
        )
        try:
            self.assertTrue(server.listening)
            # Another process for the same reason as the second-launch test above.
            code = (
                "import json, sys\n"
                "from PySide6.QtCore import QCoreApplication\n"
                "from PySide6.QtNetwork import QLocalSocket\n"
                "app = QCoreApplication([])\n"
                "socket = QLocalSocket()\n"
                "socket.connectToServer(sys.argv[1])\n"
                "assert socket.waitForConnected(3000)\n"
                "socket.write(b'{\"command\": \"status\"}\\n')\n"
                "socket.waitForBytesWritten(3000)\n"
                "while not socket.canReadLine() and socket.waitForReadyRead(3000):\n"
                "    pass\n"
                "reply = json.loads(bytes(socket.readLine()).decode('utf-8'))\n"
                "sys.exit(0 if reply == {'ok': True, 'paused': True} else 1)\n"
            )
            client = subprocess.Popen(
                [sys.executable, "-c", code, name], cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            try:
                deadline = time.monotonic() + 30
                while client.poll() is None and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.01)
                self.assertEqual(client.poll(), 0)
            finally:
                if client.poll() is None:
                    client.kill()
                client.wait()
            self.assertEqual(activated, [])
        finally:
            server.close()

    def test_control_command_unknown_or_failing_answers_not_ok(self):
        def broken():
            raise RuntimeError("no sender")

        server = kvm_bridge_win.InstanceSignal.__new__(kvm_bridge_win.InstanceSignal)
        server.commands = {"status": lambda: {"paused": False}, "broken": broken}
        self.assertEqual(server._run("status"), {"ok": True, "paused": False})
        unknown = server._run("explode")
        self.assertFalse(unknown["ok"])
        self.assertEqual(unknown["commands"], ["broken", "status"])
        self.assertFalse(server._run(["status"])["ok"])
        with self.assertLogs(kvm_bridge_win.LOGGER, level="ERROR"):
            self.assertEqual(server._run("broken"), {"ok": False, "error": "no sender"})

    def test_control_pause_come_home_and_centre_drive_the_sender(self):
        from core.return_edge import Rect

        App = kvm_bridge_win.WindowsApplication
        sender = mock.Mock(crossing_paused=False, redirecting=True, owner=b"laptop")
        app = mock.Mock(sender=sender, _label_of=lambda peer: "Laptop" if peer else "")
        app.toggle_pause.side_effect = lambda: setattr(sender, "crossing_paused", not sender.crossing_paused)
        app.control_status.side_effect = lambda: App.control_status(app)

        App._control_set_paused(app, True)
        App._control_set_paused(app, True)
        self.assertEqual(app.toggle_pause.call_count, 1)
        self.assertEqual(App.control_status(app)["paused"], True)
        self.assertEqual(App.control_status(app)["on"], "Laptop")

        with mock.patch.object(kvm_bridge_win.QTimer, "singleShot") as later:
            App.control_center_home(app)
        sender.set_redirecting.assert_called_with(False)
        later.assert_called_once_with(kvm_bridge_win.CENTER_HOME_DELAY_MS, app._center_pointer)

        desktop = mock.Mock()
        desktop.monitors.return_value = [Rect(-1920, 0, 1920, 1080), Rect(0, 0, 2560, 1440)]
        with mock.patch.object(kvm_bridge_win.platform_parts, "desktop", desktop):
            App._center_pointer(app)
        desktop.set_cursor_position.assert_called_once_with(1280, 720)

    def test_closing_without_a_tray_quits_instead_of_hiding(self):
        class Window(QWidget):
            closeEvent = kvm_bridge_win.WindowsApplication.closeEvent

            def __init__(self):
                super().__init__()
                self._closing = False
                self.tray = mock.Mock()
                self.tray.isVisible.return_value = False
                self.quits = 0

            def quit(self):
                self.quits += 1

        window = Window()
        with mock.patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
            window.close()
        self.assertEqual(window.quits, 1)

    def test_closing_with_a_tray_hides_instead_of_quitting(self):
        class Window(QWidget):
            closeEvent = kvm_bridge_win.WindowsApplication.closeEvent

            def __init__(self):
                super().__init__()
                self._closing = False
                self.tray = mock.Mock()
                self.tray.isVisible.return_value = True
                self.quits = 0

            def quit(self):
                self.quits += 1

        window = Window()
        window.show()
        with mock.patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
            window.close()
        self.assertFalse(window.isVisible())
        self.assertEqual(window.quits, 0)

    def test_closing_after_the_tray_disappears_quits(self):
        class Window(QWidget):
            closeEvent = kvm_bridge_win.WindowsApplication.closeEvent

            def __init__(self):
                super().__init__()
                self._closing = False
                self.tray = mock.Mock()
                self.tray.isVisible.return_value = True
                self.quits = 0

            def quit(self):
                self.quits += 1

        window = Window()
        with mock.patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
            window.close()
        self.assertEqual(window.quits, 1)

    def test_escape_quits_when_no_tray_icon_is_visible(self):
        class Window(QWidget):
            keyPressEvent = kvm_bridge_win.WindowsApplication.keyPressEvent

            def __init__(self):
                super().__init__()
                self.tray = mock.Mock()
                self.tray.isVisible.return_value = False
                self.quits = 0

            def quit(self):
                self.quits += 1

        window = Window()
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        with mock.patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
            window.keyPressEvent(event)
        self.assertEqual(window.quits, 1)

    def test_wayland_activation_token_is_removed_after_the_activation_request(self):
        class Window:
            show_window = kvm_bridge_win.WindowsApplication.show_window

            def __init__(self):
                self.calls = []

            def showNormal(self):
                self.calls.append("show")

            def raise_(self):
                self.calls.append("raise")

            def activateWindow(self):
                self.calls.append(("activate", os.environ.get("XDG_ACTIVATION_TOKEN")))

        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(kvm_bridge_win.platform_parts, "WAYLAND", True), \
                mock.patch.dict(os.environ, {}, clear=True):
            window = Window()
            window.show_window("one-shot-token")
            self.assertEqual(window.calls, ["show", "raise", ("activate", "one-shot-token")])
            self.assertNotIn("XDG_ACTIVATION_TOKEN", os.environ)


if __name__ == "__main__":
    unittest.main()
