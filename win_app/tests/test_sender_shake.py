import unittest
from unittest import mock
from types import SimpleNamespace

from core.tests.test_shake import qualifying_strokes, trace
from core.shake import STROKE_WINDOWS, STROKE_X11, STROKE_WAYLAND
from core.return_edge import Rect

import sender
import capture_portal
import desktop_portal


PEER = b"peer-peer-peer-01"


def qualifying_windows_trace(start=0.31):
    strokes = qualifying_strokes(length=STROKE_WINDOWS * 1.25)
    strokes.append((1, STROKE_WINDOWS * 0.25 + 0.001, 0.02))
    return trace(strokes, start=start)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Owner:
    def __init__(self):
        self.away = True
        self.on = PEER
        self.homes = 0

    def go(self, peer):
        if peer is None and self.away:
            self.away = False
            self.on = None
            self.homes += 1
        return []


class ShakeSenderTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.sender = sender.LinkSender.__new__(sender.LinkSender)
        self.sender._clock = self.clock
        self.sender._owner = Owner()
        self.sender._reported_redirecting = False
        self.sender._redirect_callback = None
        self.sender._lock = __import__("threading").RLock()
        self.sender._buttons_held = set()
        self.sender._shake_home_pending = False
        self.sender._shake_detector = None
        self.sender._escape_taps = None
        self.sender._escape_callback = None
        self.sender._keys_down = {}
        self.sender._peers = {}
        self.sender._intent_windows_up = set()
        self.sender._intent_keys_up = set()
        self.sender._pending_windows_key = None
        self.sender._ignore_gate = sender.ignored.Gate()
        self.sender._config_lock = __import__("threading").RLock()
        self.sender._config = None
        self.sender._input_messages = []
        self.sender._input = lambda message: self.sender._input_messages.append(message) or True
        self.sender._queue = lambda actions: None
        self.sender._report = lambda actions: None
        self.sender.links = type("Links", (), {"live": lambda _self, _peer: True})()
        self.edge_calls = []
        self.sender._press_edge = lambda dx, dy: self.edge_calls.append((dx, dy))

    def arm_away(self, on_shake=None):
        self.sender.set_shake(True, STROKE_WINDOWS, on_shake)
        self.sender._report_redirecting(True)

    def test_sensitivity_change_preserves_existing_refractory_period(self):
        self.arm_away()
        self.feed(qualifying_windows_trace())
        detector = self.sender._shake_detector
        cooldown = detector._cooldown_until
        self.sender.set_shake(True, STROKE_WINDOWS / 2)
        self.assertIs(self.sender._shake_detector, detector)
        self.assertEqual(detector._cooldown_until, cooldown)

    def feed(self, events):
        for t, dx, dy in events:
            self.clock.now = t
            self.sender.on_motion(dx, dy)

    def test_qualifying_shake_returns_home_once_notifies_peer_and_drops_triggering_motion(self):
        notices = []
        self.arm_away(notices.append)
        events = qualifying_windows_trace()

        self.feed(events)

        self.assertEqual(self.sender._owner.homes, 1)
        self.assertEqual(notices, [PEER])
        self.assertEqual(len(self.sender._input_messages), len(events) - 1)
        self.assertEqual(self.sender._input_messages[-1]["data"],
                         {"dx": int(events[-2][1]), "dy": int(events[-2][2])})

    def test_disabled_shake_never_returns_home(self):
        self.sender.set_shake(False, STROKE_WINDOWS)
        self.sender._report_redirecting(True)

        self.feed(qualifying_windows_trace())

        self.assertEqual(self.sender._owner.homes, 0)

    def test_enabling_shake_while_away_arms_it(self):
        notices = []
        self.sender.set_shake(True, STROKE_WINDOWS, notices.append)

        self.feed(qualifying_windows_trace())

        self.assertEqual(self.sender._owner.homes, 1)
        self.assertEqual(notices, [PEER])

    def test_arm_respects_a_button_already_held_when_input_leaves(self):
        self.sender._buttons_held.add("left")
        self.arm_away()

        self.feed(qualifying_windows_trace())

        self.assertEqual(self.sender._owner.homes, 0)

    def test_button_or_key_held_and_recent_scroll_reset_the_shake(self):
        for kind in ("button", "key", "scroll"):
            with self.subTest(kind=kind):
                self.setUp()
                self.arm_away()
                events = qualifying_windows_trace()
                if kind == "button":
                    self.sender.on_button("left", True)
                elif kind == "key":
                    self.sender.on_key("a", True, 0x41)
                else:
                    self.clock.now = 0.30
                    self.sender.on_wheel(1, 0)
                self.feed(events)
                self.assertEqual(self.sender._owner.homes, 0)

    def test_motion_in_the_first_300_ms_after_leaving_does_not_count(self):
        self.arm_away()
        events = qualifying_windows_trace(start=0.20)

        self.feed(events)

        self.assertEqual(self.sender._owner.homes, 0)

    def test_home_edge_is_held_for_200_ms_after_shake_motion_then_resumes(self):
        self.arm_away()
        events = qualifying_windows_trace()
        self.feed(events)
        self.assertEqual(self.sender._owner.homes, 1)

        self.clock.now = events[-1][0] + 0.199
        self.sender.on_motion(4, 0)
        self.assertEqual(self.edge_calls, [])
        self.clock.now += 0.201
        self.sender.on_motion(4, 0)
        self.assertEqual(self.edge_calls, [(4, 0)])

    def test_home_edge_is_not_held_without_a_shake(self):
        self.sender.set_shake(True, STROKE_WINDOWS)
        self.sender._report_redirecting(False)
        self.sender._owner.away = False

        self.sender.on_motion(4, 0)

        self.assertEqual(self.edge_calls, [(4, 0)])

    def test_capture_backend_selects_its_stroke_length(self):
        self.sender.set_shake(False, STROKE_WINDOWS)
        from shake_stroke import shake_stroke

        self.assertEqual(shake_stroke("capture_win"), STROKE_WINDOWS)
        self.assertEqual(shake_stroke("capture_x11"), STROKE_X11)
        self.assertEqual(shake_stroke("capture_portal"), STROKE_WAYLAND)
        self.assertEqual(shake_stroke("capture_win", 200), STROKE_WINDOWS / 2)

    def test_wayland_shake_release_moves_inward_and_skips_landing_wait(self):
        hooks = capture_portal.Hooks(None, None, self.sender.on_motion, clock=self.clock)
        hooks._activation = 7
        hooks._session = SimpleNamespace()
        hooks._predicate = lambda: False
        hooks._zones = [Rect(0, 0, 1920, 1080)]
        hooks._placed = [("right", (1920, 0, 1920, 1079))]
        hooks._last_wall = "right"
        released = []
        hooks._let_go = released.append
        self.sender._shake_home_pending = True

        with mock.patch.object(desktop_portal, "cursor_position", return_value=(1920, 500)):
            hooks._tick()

        self.assertEqual(released, [(1896, 500)])
        self.assertFalse(self.sender._shake_home_pending)

    def test_wayland_non_shake_release_keeps_landing_wait_and_original_point(self):
        hooks = capture_portal.Hooks(None, None, self.sender.on_motion, clock=self.clock)
        hooks._activation = 7
        hooks._session = SimpleNamespace()
        hooks._predicate = lambda: False
        hooks._zones = [Rect(0, 0, 1920, 1080)]
        hooks._placed = [("right", (1920, 0, 1920, 1079))]
        hooks._last_wall = "right"
        released = []
        hooks._let_go = released.append
        self.clock.now = 1.0

        with mock.patch.object(desktop_portal, "cursor_position", return_value=(1920, 500)):
            hooks._tick()
            self.clock.now += capture_portal.LANDING_SECONDS - 0.001
            hooks._tick()
            self.assertEqual(released, [])
            self.clock.now += 0.002
            hooks._tick()

        self.assertEqual(released, [(1920, 500)])


if __name__ == "__main__":
    unittest.main()
