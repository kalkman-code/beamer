"""GameInput recovery delegates only a fixed installer-owned task."""

import ctypes
import os
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import desktop_win
import input_injector
from input_blocker import ForegroundBlocker
from tests.test_input_blocker import LORBY_CLASSES


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


GAMEINPUT, EXPLORER = 0x5403CC, 0xD0002E
CLASSES = {GAMEINPUT: input_injector.GAMEINPUT_WINDOW_CLASS, EXPLORER: "CabinetWClass"}


class GameInputForegroundTests(unittest.TestCase):
    def setUp(self):
        self.user32 = FakeUser32(CLASSES)
        self.clock = [1000.0]
        patches = [
            mock.patch.object(input_injector, "user32", self.user32),
            mock.patch.object(desktop_win, "user32", self.user32),
            mock.patch.object(input_injector.time, "monotonic", lambda: self.clock[0]),
            mock.patch.object(ctypes, "get_last_error", create=True, return_value=0),
            mock.patch.object(input_injector, "_seen_foreground", [None]),
            mock.patch.object(input_injector, "_problem_at", float("-inf")),
            mock.patch.object(input_injector, "_problem_notified", set(), create=True),
            mock.patch.object(input_injector, "_problem_logged", None),
            mock.patch.object(desktop_win, "_refused_under", [None]),
            mock.patch.object(input_injector, "foreground_blocker", side_effect=lambda: ForegroundBlocker(
                hwnd=self.user32.foreground or 0, pid=10,
                window_class=self.user32.classes.get(self.user32.foreground, ""), process_name="test.exe")),
            mock.patch.object(input_injector, "_gameinput_restarted_at", float("-inf")),
            mock.patch.object(input_injector, "_gameinput_running", False),
            mock.patch.object(input_injector, "_gameinput_afters", []),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_gameinput_foreground_requests_recovery_off_the_input_thread(self):
        self.user32.foreground = GAMEINPUT
        notice = mock.Mock()
        with mock.patch.object(input_injector, "on_problem", notice), \
                mock.patch.object(subprocess, "run") as run, \
                mock.patch.object(subprocess, "Popen") as popen, \
                mock.patch.object(os, "system") as system, \
                mock.patch.object(input_injector.threading, "Thread") as thread:
            input_injector.watch_foreground()

        notice.assert_called_once()
        thread.return_value.start.assert_called_once()
        run.assert_not_called()
        popen.assert_not_called()
        system.assert_not_called()

    def test_recovery_retries_placement_only_while_visit_is_current(self):
        self.user32.foreground = GAMEINPUT
        self.user32.set_results = [0, 1]
        callbacks = []
        placed = mock.Mock()
        current = [True]
        with mock.patch.object(input_injector, "release_gameinput_foreground", side_effect=lambda after=None: callbacks.append(after) or True), \
                mock.patch.object(input_injector, "report_input_problem"):
            desktop_win.set_cursor_position_with_callback(10, 20, placed,
                place_if_current=lambda action: action() if current[0] else None)
        self.assertEqual(len(callbacks), 1)
        current[0] = False
        callbacks[0]()
        self.assertEqual(self.user32.set_calls, [(10, 20)])
        placed.assert_not_called()

    def test_recovery_places_pointer_and_acknowledges_current_visit(self):
        self.user32.foreground = GAMEINPUT
        self.user32.set_results = [0, 1]
        callbacks = []
        placed = mock.Mock()
        with mock.patch.object(input_injector, "release_gameinput_foreground", side_effect=lambda after=None: callbacks.append(after) or True), \
                mock.patch.object(input_injector, "report_input_problem"):
            desktop_win.set_cursor_position_with_callback(10, 20, placed,
                place_if_current=lambda action: action())
        self.assertEqual(len(callbacks), 1)
        self.user32.foreground = EXPLORER
        callbacks[0]()
        self.assertEqual(self.user32.set_calls, [(10, 20), (10, 20)])
        placed.assert_called_once_with()

    def test_unguarded_sender_pin_cannot_warp_after_recovery(self):
        self.user32.foreground = GAMEINPUT
        self.user32.set_results = [0]
        with mock.patch.object(input_injector, "release_gameinput_foreground") as recovery, \
                mock.patch.object(input_injector, "report_input_problem"):
            desktop_win.set_cursor_position(10, 20)
        recovery.assert_called_once_with(after=None)

    def test_input_problem_notice_is_throttled_for_thirty_seconds(self):
        notices = []
        with mock.patch.object(input_injector, "on_problem", notices.append):
            input_injector.report_input_problem()
            self.clock[0] += 29
            self.user32.foreground = EXPLORER
            input_injector.report_input_problem()
            self.clock[0] += 1
            input_injector.report_input_problem()

        self.assertEqual(len(notices), 2)
        self.assertIn("Click a normal window on this PC", notices[0])

    def test_lorby_57_refusals_only_notify_once_for_each_of_three_instances(self):
        notices = []
        self.user32.set_results = [0] * 57
        with mock.patch.object(input_injector, "on_problem", notices.append), \
                mock.patch.object(desktop_win.LOGGER, "warning") as log:
            for instance, window_class in enumerate(LORBY_CLASSES, start=1):
                self.user32.foreground = instance
                self.user32.classes[instance] = window_class
                for _ in range(19):
                    self.clock[0] += 31
                    desktop_win.set_cursor_position(10, 20)
            self.assertEqual(log.call_count, 3)
            self.assertEqual(len(notices), 3)
            # A successful placement or another blocker must not re-arm an old instance.
            self.user32.foreground = 1
            self.user32.set_results = [1, 0]
            desktop_win.set_cursor_position(10, 20)
            self.clock[0] += 31
            desktop_win.set_cursor_position(10, 20)
            input_injector.report_input_problem(operation="SendInput")
        self.assertEqual(len(notices), 3)

    def test_placement_discards_stale_error_and_captures_fresh_error_before_guard_returns(self):
        for native_error in (0, 5):
            with self.subTest(native_error=native_error):
                last_error = [18]
                def place(x, y):
                    self.assertEqual(last_error[0], 0)
                    last_error[0] = native_error
                    return 0
                def guard(action):
                    result = action()
                    last_error[0] = 18
                    return result
                desktop_win._refused_under[0] = None
                with mock.patch.object(ctypes, "set_last_error", side_effect=lambda value: last_error.__setitem__(0, value)), \
                        mock.patch.object(ctypes, "get_last_error", side_effect=lambda: last_error[0]), \
                        mock.patch.object(self.user32, "SetCursorPos", side_effect=place), \
                        mock.patch.object(input_injector, "report_input_problem") as report:
                    desktop_win.set_cursor_position_with_callback(10, 20, place_if_current=guard)
                self.assertEqual(report.call_args.kwargs["error"], native_error or None)

    def test_sendinput_does_not_report_a_stale_last_error(self):
        last_error = [18]
        with mock.patch.object(ctypes, "set_last_error", side_effect=lambda value: last_error.__setitem__(0, value)), \
                mock.patch.object(ctypes, "get_last_error", side_effect=lambda: last_error[0]), \
                mock.patch.object(self.user32, "SendInput", create=True, return_value=0), \
                mock.patch.object(input_injector, "report_input_problem") as report:
            with self.assertRaises(OSError):
                input_injector._send_input(input_injector.INPUT())
        self.assertIsNone(report.call_args.kwargs["error"])

    def test_ordinary_foreground_does_not_report_a_problem(self):
        self.user32.foreground = EXPLORER
        notice = mock.Mock()
        with mock.patch.object(input_injector, "on_problem", notice):
            input_injector.watch_foreground()
        notice.assert_not_called()

    def test_recovery_is_rate_limited_and_keeps_guarded_placements(self):
        self.user32.foreground = GAMEINPUT
        first, latest = mock.Mock(), mock.Mock()
        with mock.patch.object(input_injector.threading, "Thread") as thread, \
                mock.patch.object(input_injector, "report_input_problem"):
            self.assertTrue(input_injector.release_gameinput_foreground(first))
            self.assertTrue(input_injector.release_gameinput_foreground(latest))
            thread.assert_called_once()
            self.assertEqual(input_injector._gameinput_afters, [first, latest])
            input_injector._gameinput_running = False
            self.clock[0] += 59
            self.assertFalse(input_injector.release_gameinput_foreground())
            self.clock[0] += 1
            self.assertTrue(input_injector.release_gameinput_foreground())
            self.assertEqual(thread.call_count, 2)

    def _recovery_worker(self, after):
        self.user32.foreground = GAMEINPUT
        with mock.patch.object(input_injector.threading, "Thread") as thread, \
                mock.patch.object(input_injector, "report_input_problem"):
            self.assertTrue(input_injector.release_gameinput_foreground(after))
        return thread.call_args.kwargs["target"]

    def test_task_dispatch_is_fixed_hidden_and_waits_for_foreground_release(self):
        placed = mock.Mock()
        current_visit = [2]
        worker = self._recovery_worker(lambda: placed() if current_visit[0] == 2 else None)
        stale = mock.Mock()
        # An older link can finish diagnostics after the current link has queued its placement.
        input_injector.release_gameinput_foreground(after=lambda: stale() if current_visit[0] == 1 else None)
        kernel = mock.Mock()
        def system_directory(buffer, size):
            buffer.value = r"C:\Windows\System32"
            return len(buffer.value)
        kernel.GetSystemDirectoryW.side_effect = system_directory
        def release(_):
            placed.assert_not_called()
            self.user32.foreground = EXPLORER
        with mock.patch.object(ctypes, "WinDLL", create=True, return_value=kernel), \
                mock.patch.object(input_injector.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run, \
                mock.patch.object(input_injector.time, "sleep", side_effect=release):
            worker()
        self.assertEqual(run.call_args.args[0], [os.path.join(r"C:\Windows\System32", "schtasks.exe"),
            "/Run", "/TN", "Beamer-GameInputRecovery"])
        self.assertEqual(run.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        placed.assert_called_once_with()
        stale.assert_not_called()
        self.assertFalse(input_injector._gameinput_running)

    def test_missing_task_or_stuck_foreground_never_acknowledges_arrival(self):
        for returncode in (1, 0):
            input_injector._gameinput_restarted_at = float("-inf")
            placed = mock.Mock()
            worker = self._recovery_worker(placed)
            with mock.patch.object(ctypes, "WinDLL", create=True) as dll, \
                    mock.patch.object(input_injector.subprocess, "run", return_value=SimpleNamespace(returncode=returncode)), \
                    mock.patch.object(input_injector.time, "sleep"):
                dll.return_value.GetSystemDirectoryW.return_value = 20
                worker()
            placed.assert_not_called()
            self.assertFalse(input_injector._gameinput_running)

    def test_sendinput_gameinput_refusal_starts_recovery(self):
        self.user32.foreground = GAMEINPUT
        with mock.patch.object(self.user32, "SendInput", create=True, return_value=0), \
                mock.patch.object(input_injector, "release_gameinput_foreground") as recovery, \
                mock.patch.object(input_injector, "report_input_problem"):
            with self.assertRaises(OSError):
                input_injector.inject_mouse_move(1, 2)
        recovery.assert_called_once_with()

    def test_successful_cursor_placement_runs_the_arrival_callback(self):
        self.user32.foreground = EXPLORER
        placed = mock.Mock()

        desktop_win.set_cursor_position_with_callback(
            1818, 2159, placed, place_if_current=lambda action: action(),
        )

        placed.assert_called_once_with()
        self.assertEqual(self.user32.set_calls, [(1818, 2159)])

    def test_failed_cursor_placement_does_not_claim_arrival_and_warns_once_per_blocker(self):
        self.user32.foreground = EXPLORER
        self.user32.set_results = [0, 0, 1, 0]
        placed = mock.Mock()
        notice = mock.Mock()
        with mock.patch.object(input_injector, "on_problem", notice), \
                self.assertLogs(desktop_win.LOGGER, "WARNING") as logs:
            for _ in range(4):
                desktop_win.set_cursor_position_with_callback(10, 20, placed)

        placed.assert_called_once_with()
        self.assertEqual(notice.call_count, 1)
        self.assertEqual(len(logs.records), 2)
        self.assertIn("CabinetWClass", logs.output[0])

    def test_stale_visit_neither_moves_the_cursor_nor_calls_arrival(self):
        placed = mock.Mock()
        current = [False]

        desktop_win.set_cursor_position_with_callback(
            1818, 2159, placed,
            place_if_current=lambda action: action() if current[0] else None,
        )

        self.assertEqual(self.user32.set_calls, [])
        placed.assert_not_called()

    def test_refused_placement_does_not_call_arrival_even_for_a_current_visit(self):
        self.user32.foreground = EXPLORER
        self.user32.set_results = [0]
        placed = mock.Mock()
        with mock.patch.object(input_injector, "report_input_problem"):
            desktop_win.set_cursor_position_with_callback(
                1818, 2159, placed,
                place_if_current=lambda action: action(),
            )

        self.assertEqual(self.user32.set_calls, [(1818, 2159)])
        placed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
