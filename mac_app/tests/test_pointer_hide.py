"""Hiding the Mac's pointer while it drives the other machine. The invariant is one line: the
pointer is visible whenever input is local, by whichever path it came home, so a hide is never
left behind. A fake Quartz counts the hide and show calls, which must balance."""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from bridge import KVMController
from pointer_hide import PointerHider
from bridge_fakes import PAIRED_TOKEN, FakeClipboard, FakeClock, FakeQuartz, make_config, quiet_logger
from fake_link import FakeLink, bring_up


class CountingQuartz(FakeQuartz):
    hide_calls = []
    show_calls = []
    hide_status = 0

    @classmethod
    def reset_cursor_spies(cls):
        super().reset_cursor_spies()
        cls.hide_calls = []
        cls.show_calls = []
        cls.hide_status = 0

    @staticmethod
    def CGMainDisplayID():
        return 7

    @classmethod
    def CGDisplayHideCursor(cls, display):
        cls.hide_calls.append(display)
        return cls.hide_status

    @classmethod
    def CGDisplayShowCursor(cls, display):
        cls.show_calls.append(display)
        return 0


class PointerHiderTests(unittest.TestCase):
    def setUp(self):
        CountingQuartz.reset_cursor_spies()
        self.background = []
        self.hider = PointerHider(CountingQuartz, background=lambda: self.background.append(True), logger=quiet_logger())

    def test_hiding_twice_hides_once_so_one_show_undoes_it(self):
        self.hider.hide()
        self.hider.hide()
        self.hider.show()
        self.assertEqual((len(CountingQuartz.hide_calls), len(CountingQuartz.show_calls)), (1, 1))

    def test_show_without_a_hide_does_nothing(self):
        self.hider.show()
        self.assertEqual(CountingQuartz.show_calls, [])

    def test_the_background_permission_is_asked_for_once_before_the_first_hide(self):
        self.hider.hide()
        self.hider.show()
        self.hider.hide()
        self.assertEqual(self.background, [True])

    def test_a_hide_the_system_refused_is_not_counted_as_hidden(self):
        CountingQuartz.hide_status = 1001
        self.hider.hide()
        self.hider.show()
        self.assertEqual(CountingQuartz.show_calls, [])

    def test_a_failing_background_call_still_tries_to_hide_and_never_raises(self):
        def broken():
            raise OSError("no such symbol")

        hider = PointerHider(CountingQuartz, background=broken, logger=quiet_logger())
        hider.hide()
        hider.show()
        self.assertEqual((len(CountingQuartz.hide_calls), len(CountingQuartz.show_calls)), (1, 1))


class ControllerPointerTests(unittest.TestCase):
    def setUp(self):
        CountingQuartz.reset_cursor_spies()
        self.controller = KVMController(make_config(PAIRED_TOKEN), logger=quiet_logger(), quartz=CountingQuartz, clock=FakeClock(),
                                        clipboard=FakeClipboard(""), link_factory=FakeLink, desktop_bounds=lambda: (0, 0, 1920, 1080))
        self.controller.pointer = PointerHider(CountingQuartz, background=lambda: None, logger=quiet_logger())
        self.link = bring_up(self.controller)

    def hidden(self):
        return len(CountingQuartz.hide_calls) - len(CountingQuartz.show_calls)

    def test_redirecting_hides_the_pointer(self):
        self.assertTrue(self.controller.set_redirecting(True))
        self.assertEqual(self.hidden(), 1)

    def test_switching_home_shows_it(self):
        self.controller.set_redirecting(True)
        self.controller.set_redirecting(False)
        self.assertEqual(self.hidden(), 0)

    def test_a_dropped_link_shows_it(self):
        self.controller.set_redirecting(True)
        self.controller._connection_failed("the link dropped")
        self.assertEqual(self.hidden(), 0)

    def test_the_peer_going_silent_shows_it(self):
        self.controller.set_redirecting(True)
        self.link.down(self.controller)
        self.assertEqual(self.hidden(), 0)

    def test_a_forced_local_shows_it(self):
        self.controller.set_redirecting(True)
        self.controller._force_local("the tap died")
        self.assertEqual(self.hidden(), 0)

    def test_a_settings_change_shows_it(self):
        self.controller.set_redirecting(True)
        self.controller.update_config(self.controller.cfg)
        self.assertEqual(self.hidden(), 0)

    def test_the_pc_taking_this_mac_over_shows_it(self):
        self.controller.set_redirecting(True)
        self.controller.set_receiving(True)
        self.assertEqual(self.hidden(), 0)

    def test_stopping_shows_it(self):
        self.controller.set_redirecting(True)
        self.controller.stop()
        self.assertEqual(self.hidden(), 0)

    def test_going_local_by_any_assignment_shows_it(self):
        # The handshake-failure branches clear the flag directly; the flag itself is the gate.
        self.controller.set_redirecting(True)
        self.controller.redirecting = False
        self.assertEqual(self.hidden(), 0)

    def test_a_link_failing_as_the_hide_runs_leaves_it_shown(self):
        # The owner's effects run under one lock in the order they were decided, so a link going
        # down on its own thread waits for the hide and its show comes after it.
        real_hide = self.controller.pointer.hide
        failing = threading.Thread(target=lambda: self.link.down(self.controller))

        def hide_as_the_link_drops():
            failing.start()
            failing.join(0.05)
            real_hide()

        self.controller.pointer.hide = hide_as_the_link_drops
        self.controller.set_redirecting(True)
        failing.join(5)
        self.assertFalse(failing.is_alive())
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(self.hidden(), 0)

    def test_a_refused_redirect_hides_nothing(self):
        self.controller.cfg = self.controller.cfg.__class__(**{**self.controller.cfg.__dict__, "send_to_windows": False})
        self.assertFalse(self.controller.set_redirecting(True))
        self.assertEqual(CountingQuartz.hide_calls, [])

    def test_redirect_and_return_many_times_keeps_the_counts_equal(self):
        for _ in range(5):
            self.controller.set_redirecting(True)
            self.controller.set_redirecting(False)
        self.assertEqual((len(CountingQuartz.hide_calls), len(CountingQuartz.show_calls)), (5, 5))


if __name__ == "__main__":
    unittest.main()
