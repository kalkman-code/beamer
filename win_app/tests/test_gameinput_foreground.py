"""GameInput's SYSTEM window taking the foreground, exercised on any platform.

While GameInputSvc's hidden window is in front, Windows refuses Beamer's
SetCursorPos and drops its SendInput, and the only way out Beamer has is to
restart the service. user32, subprocess.run and the restart thread are all
faked here, so this suite runs on the Mac.
"""

import ctypes
import subprocess
import unittest
from unittest import mock
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import desktop_win
import input_injector


class FakeUser32:
    def __init__(self, classes, foreground=None, set_results=()):
        self.classes = classes
        self.foreground = foreground
        self.set_results = list(set_results)
        self.set_calls = []

    def GetForegroundWindow(self):
        return self.foreground

    def GetClassNameW(self, hwnd, buffer, size):
        buffer.value = self.classes.get(hwnd, "")
        return len(buffer.value)

    def SetCursorPos(self, x, y):
        self.set_calls.append((x, y))
        return self.set_results.pop(0) if self.set_results else 1


class InlineThread:
    """Runs the restart on start(), so a test sees its effects at once."""

    def __init__(self, target, name=None, daemon=None):
        self.target = target

    def start(self):
        self.target()


GAMEINPUT, EXPLORER = 0x5403CC, 0xD0002E
CLASSES = {GAMEINPUT: input_injector.GAMEINPUT_WINDOW_CLASS, EXPLORER: "CabinetWClass"}


class GameInputForegroundTests(unittest.TestCase):
    def setUp(self):
        self.user32 = FakeUser32(CLASSES)
        self.restarts = []

        def restart(args, **kwargs):
            self.restarts.append((args, kwargs))
            self.user32.foreground = EXPLORER
            return subprocess.CompletedProcess(args, 0, "", "")

        self.clock = [1000.0]
        patches = [
            mock.patch.object(input_injector, "user32", self.user32),
            mock.patch.object(desktop_win, "user32", self.user32),
            mock.patch.object(input_injector.subprocess, "run", side_effect=restart),
            mock.patch.object(input_injector.threading, "Thread", InlineThread),
            mock.patch.object(input_injector.time, "monotonic", lambda: self.clock[0]),
            mock.patch.object(ctypes, "get_last_error", create=True, return_value=0),
            mock.patch.object(input_injector, "_seen_foreground", [None]),
            mock.patch.object(input_injector, "_gameinput_restarted_at", [float("-inf")]),
            mock.patch.object(desktop_win, "_refused_under", [None]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_move_under_gameinput_restarts_the_service_without_a_console(self):
        self.user32.foreground = GAMEINPUT
        input_injector.watch_foreground()
        self.assertEqual(len(self.restarts), 1)
        args, kwargs = self.restarts[0]
        self.assertEqual(args, input_injector.GAMEINPUT_RESTART)
        self.assertIn("Restart-Service GameInputSvc", args[-1])
        self.assertEqual(kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_an_ordinary_foreground_is_left_alone(self):
        self.user32.foreground = EXPLORER
        for _ in range(3):
            input_injector.watch_foreground()
        self.assertEqual(self.restarts, [])

    def test_the_class_is_read_only_when_the_foreground_changes(self):
        self.user32.foreground = EXPLORER
        with mock.patch.object(input_injector, "_window_class", wraps=input_injector._window_class) as lookup:
            for _ in range(5):
                input_injector.watch_foreground()
        self.assertEqual(lookup.call_count, 1)

    def test_a_restart_that_did_not_free_it_waits_out_the_cooldown_then_tries_again(self):
        self.restarts_leave_gameinput_in_front()
        self.user32.foreground = GAMEINPUT
        input_injector.watch_foreground()
        self.clock[0] += 5
        input_injector.watch_foreground()
        self.assertEqual(len(self.restarts), 1)
        self.clock[0] += input_injector.GAMEINPUT_RESTART_COOLDOWN_SECONDS
        input_injector.watch_foreground()
        self.assertEqual(len(self.restarts), 2)

    def test_a_refused_arrival_is_placed_again_once_gameinput_is_gone(self):
        self.user32.foreground = GAMEINPUT
        self.user32.set_results = [0]
        desktop_win.set_cursor_position(1818, 2159)
        self.assertEqual(len(self.restarts), 1)
        self.assertEqual(self.user32.set_calls, [(1818, 2159), (1818, 2159)])

    def test_a_refusal_under_anything_else_is_logged_once_per_blocker(self):
        self.user32.foreground = EXPLORER
        self.user32.set_results = [0, 0, 0, 1, 0]
        with self.assertLogs(desktop_win.LOGGER, "WARNING") as logs:
            for _ in range(3):
                desktop_win.set_cursor_position(10, 10)
            desktop_win.set_cursor_position(10, 10)
            desktop_win.set_cursor_position(10, 10)
        self.assertEqual(len(logs.records), 2)
        self.assertIn("CabinetWClass", logs.output[0])
        self.assertEqual(self.restarts, [])

    def restarts_leave_gameinput_in_front(self):
        def restart(args, **kwargs):
            self.restarts.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, "", "")

        input_injector.subprocess.run.side_effect = restart


if __name__ == "__main__":
    unittest.main()
