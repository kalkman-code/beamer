import ctypes
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import desktop_win


class FullScreenAppTest(unittest.TestCase):
    def test_beamer_is_not_a_full_screen_app_to_itself(self):
        state = ctypes.c_int(3)

        def query_user_notification_state(value):
            value._obj.value = state.value
            return 0

        process = ctypes.c_ulong(os.getpid())
        foreground = SimpleNamespace(
            GetForegroundWindow=lambda: 42,
            GetWindowThreadProcessId=lambda _hwnd, value: setattr(value._obj, "value", process.value),
        )
        shell = SimpleNamespace(SHQueryUserNotificationState=query_user_notification_state)
        with mock.patch.object(desktop_win, "_require"), \
                mock.patch.object(desktop_win, "user32", foreground), \
                mock.patch.object(desktop_win.ctypes, "windll", SimpleNamespace(shell32=shell), create=True), \
                mock.patch.object(desktop_win, "_foreground_covers_its_monitor") as covers, \
                mock.patch.object(desktop_win, "_foreground_app_name") as name:
            self.assertIsNone(desktop_win.full_screen_app())
        covers.assert_not_called()
        name.assert_not_called()


if __name__ == "__main__":
    unittest.main()
