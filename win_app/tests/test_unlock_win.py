"""Unlock orchestration, exercised on any platform.

unlock_win talks to kernel32, so every test here passes a fake `mem32` in its
place -- the same dependency-injection trick clipboard_win.py and
handle_message use, so this suite runs on the Mac.
"""

import unittest
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import protocol
from core import receiver
import unlock_win
from core.receiver import ReceiverServer, ServerState


class FakeKernel32:
    """Stands in for kernel32. `locked_sequence` is consumed one entry per
    is_locked() call so a test can script the console unlocking part-way
    through the poll loop."""

    def __init__(self, locked_sequence, event_opens=True, lock_screen_session=1):
        self.locked_sequence = list(locked_sequence)
        self.event_opens = event_opens
        self.lock_screen_session = lock_screen_session
        self.opened_names = []
        self.set_events = 0
        self.closed = 0

    # is_locked() reaches the process list through this pair.
    def CreateToolhelp32Snapshot(self, flags, pid):
        return 1234

    def Process32FirstW(self, snapshot, entry):
        locked = self.locked_sequence.pop(0) if self.locked_sequence else False
        # The production call passes ctypes.byref(entry); a CArgObject exposes
        # the structure it wraps as _obj.
        entry._obj.szExeFile = "logonui.exe" if locked else "explorer.exe"
        return 1

    def Process32NextW(self, snapshot, entry):
        return 0

    def WTSGetActiveConsoleSessionId(self):
        return 1

    def ProcessIdToSessionId(self, pid, session):
        session._obj.value = self.lock_screen_session
        return 1

    def OpenEventW(self, access, inherit, name):
        self.opened_names.append(name)
        return 99 if self.event_opens else None

    def SetEvent(self, handle):
        self.set_events += 1
        return 1

    def CloseHandle(self, handle):
        self.closed += 1
        return 1


class IsLockedTests(unittest.TestCase):
    def test_logonui_running_means_locked(self):
        self.assertIs(unlock_win.is_locked(FakeKernel32([True])), True)

    def test_a_lock_screen_in_another_session_is_not_this_consoles(self):
        # The fix for issue #5 sends the Mac home on a lock, so a second account's lock screen or
        # an RDP logon must not read as the console's: the PC could not be driven at all.
        self.assertIs(unlock_win.is_locked(FakeKernel32([True], lock_screen_session=2)), False)

    def test_no_logonui_means_unlocked(self):
        self.assertIs(unlock_win.is_locked(FakeKernel32([False])), False)

    def test_failed_snapshot_is_unknown_not_unlocked(self):
        """None, never False: reporting an unknown state as unlocked would
        silently skip an unlock the machine actually needed."""
        fake = FakeKernel32([False])
        fake.CreateToolhelp32Snapshot = lambda flags, pid: None
        self.assertIsNone(unlock_win.is_locked(fake))


class SignalUnlockTests(unittest.TestCase):
    def test_signals_the_event_for_the_console_session(self):
        fake = FakeKernel32([True])
        self.assertTrue(unlock_win.signal_unlock(fake))
        self.assertEqual(fake.opened_names, ["Global\\RoomDeckUnlock.1"])
        self.assertEqual(fake.set_events, 1)
        self.assertEqual(fake.closed, 1)

    def test_missing_provider_fails_cleanly(self):
        fake = FakeKernel32([True], event_opens=False)
        self.assertFalse(unlock_win.signal_unlock(fake))
        self.assertEqual(fake.set_events, 0)


class EnsureUnlockedTests(unittest.TestCase):
    def test_already_unlocked_never_signals(self):
        fake = FakeKernel32([False])
        self.assertTrue(unlock_win.ensure_unlocked(fake, sleep=lambda _: None))
        self.assertEqual(fake.set_events, 0)

    def test_unknown_state_is_treated_as_unlocked(self):
        fake = FakeKernel32([False])
        fake.CreateToolhelp32Snapshot = lambda flags, pid: None
        self.assertTrue(unlock_win.ensure_unlocked(fake, sleep=lambda _: None))
        self.assertEqual(fake.set_events, 0)

    def test_locked_console_is_signalled_and_polled_until_it_clears(self):
        # Locked on the initial test and the first poll, unlocked on the next.
        fake = FakeKernel32([True, True, False])
        self.assertTrue(unlock_win.ensure_unlocked(fake, sleep=lambda _: None))
        self.assertEqual(fake.set_events, 1)

    def test_console_that_never_unlocks_reports_failure(self):
        fake = FakeKernel32([True] * 100)
        self.assertFalse(
            unlock_win.ensure_unlocked(fake, timeout=1.0, poll=0.5, sleep=lambda _: None)
        )

    def test_missing_provider_reports_failure_without_polling(self):
        fake = FakeKernel32([True], event_opens=False)
        self.assertFalse(unlock_win.ensure_unlocked(fake, sleep=lambda _: None))


class FakeUnlockModule:
    def __init__(self, locked, unlocks=True):
        self.locked = locked
        self.unlocks = unlocks
        self.ensure_calls = 0

    def is_locked(self):
        return self.locked

    def ensure_unlocked(self):
        self.ensure_calls += 1
        return self.unlocks


class ReceiverUnlockTests(unittest.TestCase):
    """focus{target:"windows"} is the moment input starts arriving here, so it
    is where the lock screen has to be cleared out of its way."""

    def _server(self, unlock):
        statuses = []
        server = ReceiverServer(lambda state, detail: statuses.append((state, detail)), unlock=unlock)
        return server, statuses

    def test_unlocked_pc_does_no_work_on_focus(self):
        unlock = FakeUnlockModule(locked=False)
        server, _ = self._server(unlock)
        server._begin_unlock("192.168.1.5:1", "192.168.1.5")
        self.assertEqual(unlock.ensure_calls, 0)
        self.assertFalse(server._unlocking.is_set())

    def test_locked_pc_unlocks_and_restores_the_connected_status(self):
        unlock = FakeUnlockModule(locked=True)
        server, statuses = self._server(unlock)
        server._begin_unlock("192.168.1.5:1", "192.168.1.5")
        for _ in range(200):
            if not server._unlocking.is_set():
                break
            import time
            time.sleep(0.01)
        self.assertEqual(unlock.ensure_calls, 1)
        self.assertIn((ServerState.CONNECTED, "Unlocking Windows…"), statuses)
        self.assertEqual(statuses[-1], (ServerState.CONNECTED, "Connected to 192.168.1.5"))

    def test_failed_unlock_says_so_rather_than_claiming_a_connection(self):
        unlock = FakeUnlockModule(locked=True, unlocks=False)
        server, statuses = self._server(unlock)
        server._begin_unlock("192.168.1.5:1", "192.168.1.5")
        for _ in range(200):
            if not server._unlocking.is_set():
                break
            import time
            time.sleep(0.01)
        self.assertEqual(
            statuses[-1],
            (ServerState.CONNECTED, "Windows is locked — unlock it at the PC"),
        )

    def test_a_session_that_ended_mid_unlock_does_not_report_a_connection(self):
        """An unlock outlives a Mac that disconnects during it; the session's
        own exit has already set WAITING and must not be overwritten."""
        unlock = FakeUnlockModule(locked=True)
        server, statuses = self._server(unlock)
        server._unlock_worker(unlock, "192.168.1.5:1", "192.168.1.5", generation=-1)
        self.assertEqual(statuses, [])

    def test_ack_advertises_the_unlock_only_while_it_is_running(self):
        """The Mac reads this to explain the pause; it must not linger, or the
        Mac shows 'Unlocking Windows…' forever."""
        self.assertNotIn("locked", protocol.ack_msg(7)["data"])
        self.assertIs(protocol.ack_msg(7, locked=True)["data"]["locked"], True)


if __name__ == "__main__":
    unittest.main()
