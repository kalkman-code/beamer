"""Wayland capture on fakes: the barriers that stand for each kind of zone, the press at a barrier
(push, slide, back into the screen), and the capture thread against a scripted InputCapture portal
and libei: a push that crosses, input while away, and every way home (the sender bringing it home,
a move back, a key at the edge, a pause, the desktop ending it, a stop, a dead connection, a
stalled thread)."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import capture_portal
import desktop_portal
import portal
from core import return_edge
from core.return_edge import Rect
from portal_fakes import FakeBus, FakeDevice, FakeEi, vardict, wait_for

ONE = [Rect(0, 0, 1920, 1080)]
SESSION = "/org/freedesktop/portal/desktop/session/1_42/beamer2"


class BarrierTest(unittest.TestCase):
    def test_a_whole_edge_on_one_display(self):
        self.assertEqual(capture_portal.barriers([return_edge.ReturnEdge("right")], ONE), [("right", (1920, 0, 1920, 1079))])
        self.assertEqual(capture_portal.barriers([return_edge.ReturnEdge("top")], ONE), [("top", (0, 0, 1919, 0))])

    def test_an_edge_between_two_displays_has_no_barrier(self):
        side_by_side = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1920, 1080)]
        self.assertEqual(capture_portal.barriers([return_edge.ReturnEdge("right")], side_by_side), [("right", (3840, 0, 3840, 1079))])
        self.assertEqual(capture_portal.barriers([return_edge.ReturnEdge("top")], side_by_side),
                         [("top", (0, 0, 1919, 0)), ("top", (1920, 0, 3839, 0))])

    def test_a_staggered_display_leaves_the_uncovered_stretch_of_its_neighbour(self):
        staggered = [Rect(0, 0, 1920, 1080), Rect(1920, 200, 1280, 1024)]
        self.assertEqual(capture_portal.barriers([return_edge.ReturnEdge("right")], staggered),
                         [("right", (1920, 0, 1920, 199)), ("right", (3200, 200, 3200, 1223))])

    def test_thirds_are_split_where_part_of_splits_them(self):
        found = capture_portal.barriers([return_edge.PartEdge("left", {"middle"})], ONE)
        self.assertEqual(found, [("left", (0, 360, 0, 719))])
        self.assertEqual(return_edge.part_of(359 / 1080), "start")
        self.assertEqual(return_edge.part_of(360 / 1080), "middle")
        self.assertEqual(return_edge.part_of(720 / 1080), "end")
        both = capture_portal.barriers([return_edge.PartEdge("bottom", {"start", "end"})], ONE)
        self.assertEqual(both, [("bottom", (0, 1080, 639, 1080)), ("bottom", (1280, 1080, 1919, 1080))])

    def test_a_corner_is_its_two_walls_as_far_as_the_corner_push_reaches(self):
        found = capture_portal.barriers([return_edge.CornerPush("top_right", "right")], ONE)
        self.assertEqual(sorted(found), [("right", (1920, 0, 1920, 8)), ("top", (1911, 0, 1919, 0))])
        model = return_edge.CornerPush("top_right", "right")
        self.assertTrue(model._at_edge(ONE, (1919, 8)) and model._at_edge(ONE, (1911, 0)))
        self.assertFalse(model._at_edge(ONE, (1919, 9)) or model._at_edge(ONE, (1910, 0)))

    def test_a_notch_stretch_has_none_and_a_repeat_is_placed_once(self):
        models = [return_edge.SpanEdge("top", lambda: (100, 200)), return_edge.ReturnEdge("left"), return_edge.ReturnEdge("left")]
        self.assertEqual(capture_portal.barriers(models, ONE), [("left", (0, 0, 0, 1079))])


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class PressTest(unittest.TestCase):
    def test_a_push_holds_a_slide_moves_along_and_a_move_back_lets_go_where_it_ends(self):
        clock = Clock()
        press = capture_portal.Press("right", (1920.0, 500.0), ONE, clock)
        self.assertEqual(press.point, (1919, 500))
        self.assertTrue(press.feed(6, 0))
        self.assertEqual(press.point, (1919, 500))
        self.assertTrue(press.feed(3, -10))
        self.assertEqual(press.point, (1919, 490))
        self.assertTrue(press.feed(0, 4))
        self.assertEqual(press.point, (1919, 494))
        self.assertFalse(press.feed(-5, 2))
        self.assertEqual(press.point, (1914, 496))

    def test_travel_past_the_display_is_not_kept(self):
        press = capture_portal.Press("right", (1920.0, 1075.0), ONE, Clock())
        press.feed(0, 100)
        self.assertEqual(press.point, (1919, 1079))
        press.feed(0, -10)
        self.assertEqual(press.point, (1919, 1069))

    def test_a_pause_in_the_push_is_idle(self):
        clock = Clock()
        press = capture_portal.Press("left", (0.0, 10.0), ONE, clock)
        press.feed(-4, 0)
        clock.now += capture_portal.IDLE_SECONDS / 2
        self.assertFalse(press.idle())
        press.feed(0, 3)  # a slide is not a push
        clock.now += capture_portal.IDLE_SECONDS
        self.assertTrue(press.idle())


class MemoryTokens:
    def __init__(self, token=""):
        self.token = token

    def read(self):
        return self.token

    def write(self, token):
        self.token = token


class Harness:
    """A Hooks on a fake portal, its callbacks recorded, and a sender stand-in that crosses after
    `push_to_cross` pushes, as the zone model does."""

    def __init__(self, version=1, models=None, push_to_cross=2, tokens=None):
        self.bus = FakeBus(versions={capture_portal.INTERFACE: version})
        self.bus.answers["CreateSession"] = (0, {"session_handle": SESSION, "capabilities": 3})
        self.bus.answers["GetZones"] = (0, {"zones": [(1920, 1080, 0, 0)], "zone_set": 7})
        self.bus.answers["SetPointerBarriers"] = (0, {"failed_barriers": []})
        self.bus.returns["CreateSession2"] = (vardict({"session_handle": SESSION}),)
        self.bus.returns["ConnectToEIS"] = (99,)
        self.ei = FakeEi()
        self.keys, self.mice, self.motions, self.homes, self.failures = [], [], [], [], []
        self.away = False
        self.models = models if models is not None else [return_edge.ReturnEdge("right")]
        self.push_to_cross = push_to_cross
        self.tokens = tokens or MemoryTokens()
        self.hooks = capture_portal.Hooks(self._key, self._mouse, self._motion, connect=lambda: self.bus,
                                          ei=lambda fd: self.ei, tokens=self.tokens)
        self.hooks.grab_while(lambda: self.away)
        self.hooks.barriers_from(lambda: self.models)
        self.hooks.on_home = lambda: self.homes.append(True) or setattr(self, "away", False)
        self.hooks.on_failure = self.failures.append
        self.pointer = FakeDevice("captured relative pointer", {1, 16, 32})
        self.keyboard = FakeDevice("captured keyboard", {4})

    def _key(self, name, down, vk, us):
        self.keys.append((name, down, vk))
        return True

    def _mouse(self, message, x, y, data):
        self.mice.append((message, data))
        return True

    def _motion(self, dx, dy):
        self.motions.append((dx, dy))
        if len(self.motions) >= self.push_to_cross:
            self.away = True

    def start(self):
        self.hooks.start()
        wait_for(lambda: self.bus.made("Enable"), what="the session to be enabled")

    def activate(self, x=1920.0, y=500.0, barrier=1, activation=3):
        # libei's start sequence is the portal's activation id.
        self.ei.feed(("start", self.pointer, activation), ("start", self.keyboard, activation))
        self.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Activated",
                      (SESSION, vardict({"activation_id": activation, "cursor_position": (x, y), "barrier_id": barrier})))
        wait_for(lambda: self.hooks.holding, what="the capture to begin")

    def cross(self):
        self.activate()
        self.ei.feed(*[("motion", self.pointer, 10.0, 0.0)] * self.push_to_cross)
        wait_for(lambda: self.away and self.hooks._away, what="input to go away")

    def releases(self):
        return [(body[1]["activation_id"][1], body[1]["cursor_position"][1]) for body in self.bus.made("Release")]


@unittest.skipIf(sys.platform == "win32", "the Wayland back ends select on pipes, which Windows cannot; they run only on Linux")
class HooksTest(unittest.TestCase):
    def setUp(self):
        desktop_portal._position = None
        desktop_portal._zones = []
        desktop_portal._regions = []
        desktop_portal._captured = False
        self.harness = None

    def tearDown(self):
        if self.harness is not None:
            self.harness.hooks.stop()

    def make(self, **kwargs):
        self.harness = Harness(**kwargs)
        return self.harness

    def test_the_barriers_go_on_the_zones_and_the_session_is_enabled(self):
        h = self.make()
        h.start()
        listed = h.bus.made("SetPointerBarriers")[0][2]
        self.assertEqual([item["position"] for item in listed], [("(iiii)", (1920, 0, 1920, 1079))])
        self.assertEqual(h.bus.made("SetPointerBarriers")[0][3], 7)
        self.assertEqual(desktop_portal.monitors(), ONE)

    def test_a_push_crosses_input_goes_away_and_comes_home_where_the_sender_lands_it(self):
        h = self.make()
        h.start()
        h.cross()
        self.assertEqual(h.motions, [(10, 0), (10, 0)])
        self.assertEqual(desktop_portal.position(), (1919, 500))
        h.ei.feed(("key", h.keyboard, 1, True), ("frame", h.keyboard, 0), ("key", h.keyboard, 1, False), ("frame", h.keyboard, 0),
                  ("button", h.pointer, 0x110, True), ("scroll_discrete", h.pointer, 0, 120), ("frame", h.pointer, 0),
                  ("motion", h.pointer, 2.5, -1.5), ("motion", h.pointer, 0.5, 0.5), ("frame", h.pointer, 0))
        wait_for(lambda: len(h.motions) >= 3 and len(h.keys) == 2, what="the input away")
        self.assertEqual(h.keys, [("esc", True, 1), ("esc", False, 1)])
        self.assertEqual(h.mice, [(capture_portal._BUTTONS[1][0], 0),
                                  (capture_portal.WM_MOUSEWHEEL, capture_portal._wheel_data(-capture_portal.WHEEL_DELTA))])
        self.assertEqual(h.motions[2:], [(2, -1), (1, 0)])
        self.assertEqual(h.releases(), [])
        desktop_portal.set_cursor_position(0, 300)
        h.away = False
        wait_for(lambda: h.releases(), what="the let-go")
        self.assertEqual(h.releases(), [(3, (0.0, 300.0))])
        self.assertFalse(h.hooks.holding)

    def test_a_let_go_waits_a_moment_for_the_landing_the_sender_reports_after_input_comes_home(self):
        h = self.make()
        h.start()
        h.cross()
        h.away = False
        time.sleep(capture_portal.LANDING_SECONDS / 3)
        desktop_portal.set_cursor_position(0, 777)
        wait_for(lambda: h.releases(), what="the let-go")
        self.assertEqual(h.releases(), [(3, (0.0, 777.0))])

    def test_a_paused_keyboard_releases_keys_held_by_that_device(self):
        h = self.make()
        h.start()
        h.cross()
        h.ei.feed(("key", h.keyboard, 29, True), ("frame", h.keyboard, 0))
        wait_for(lambda: h.keys, what="the held key to be forwarded")
        h.ei.feed(("device_paused", h.keyboard))
        wait_for(lambda: len(h.keys) == 2, what="the paused device's held key to be released")
        self.assertEqual([(name, down) for name, down, _vk in h.keys], [("cmd", True), ("cmd", False)])
        self.assertEqual(h.hooks._names_down, {})

    def test_moving_back_into_the_screen_lets_go_there_and_sends_nothing(self):
        h = self.make()
        h.start()
        h.cross()
        h.ei.feed(("key", h.keyboard, 29, True), ("frame", h.keyboard, 0))
        wait_for(lambda: h.keys, what="the held key to be forwarded")
        h.away = False
        desktop_portal.set_cursor_position(0, 502)
        wait_for(lambda: h.releases(), what="the let-go")
        self.assertEqual(h.releases(), [(3, (0.0, 502.0))])
        self.assertFalse(h.hooks.holding)
        self.assertEqual(h.hooks._names_down, {})

    def test_a_key_at_the_edge_lets_go_and_reaches_nobody(self):
        h = self.make()
        h.start()
        h.activate()
        h.ei.feed(("key", h.keyboard, 30, True), ("frame", h.keyboard, 0))
        wait_for(lambda: h.releases(), what="the let-go")
        self.assertEqual(h.keys, [])

    def test_a_pause_in_the_push_lets_go_where_it_rests(self):
        h = self.make(push_to_cross=99)
        h.start()
        h.activate(y=40.0)
        h.ei.feed(("motion", h.pointer, 4.0, 0.0))
        wait_for(lambda: h.releases(), timeout=capture_portal.IDLE_SECONDS + 1, what="the let-go")
        self.assertEqual(h.releases(), [(3, (1919.0, 40.0))])
        self.assertEqual(h.motions, [(4, 0)])

    def test_events_ahead_of_the_activation_are_read_once_it_says_which_barrier(self):
        h = self.make()
        h.start()
        h.ei.feed(("start", h.pointer, 5), ("motion", h.pointer, 10.0, 0.0))
        time.sleep(0.15)
        self.assertEqual(h.motions, [])
        h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Activated",
                   (SESSION, vardict({"activation_id": 5, "cursor_position": (1920.0, 9.0), "barrier_id": 1})))
        wait_for(lambda: h.motions, what="the early push")
        self.assertEqual(h.motions, [(10, 0)])

    def test_what_arrives_after_a_let_go_is_not_replayed_into_the_next_capture(self):
        h = self.make()
        h.start()
        h.activate()
        h.ei.feed(("motion", h.pointer, -5.0, 0.0))
        wait_for(lambda: h.releases(), what="the let-go")
        h.ei.feed(("motion", h.pointer, 7.0, 0.0), ("stop", h.pointer), ("stop", h.keyboard))
        time.sleep(0.15)
        h.activate(activation=4)
        time.sleep(0.15)
        self.assertEqual(h.motions, [])

    def test_a_barrier_it_does_not_know_is_let_go_at_once(self):
        h = self.make()
        h.start()
        h.ei.feed(("start", h.pointer, 1))
        h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Activated",
                   (SESSION, vardict({"activation_id": 8, "cursor_position": (1920.0, 9.0), "barrier_id": 77})))
        wait_for(lambda: h.releases(), what="the let-go")
        self.assertEqual(h.releases(), [(8, (1919.0, 9.0))])

    def test_the_desktop_ending_a_capture_while_input_is_away_brings_it_home(self):
        h = self.make()
        h.start()
        h.cross()
        h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Deactivated",
                   (SESSION, vardict({"activation_id": 3, "cursor_position": (1000.0, 10.0)})))
        wait_for(lambda: h.homes, what="input home")
        self.assertFalse(h.hooks.holding)
        self.assertEqual(desktop_portal.position(), (1000, 10))
        self.assertEqual(h.releases(), [])

    def test_disabled_sets_the_barriers_again_and_enables_the_session(self):
        h = self.make()
        h.start()
        h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Disabled", (SESSION, {}))
        wait_for(lambda: len(h.bus.made("Enable")) >= 2, what="the session enabled again")
        self.assertEqual(len(h.bus.made("SetPointerBarriers")), 2)

    def test_held_edges_take_the_barriers_away_and_their_return_puts_them_back(self):
        h = self.make()
        h.start()
        models = h.models
        h.models = []
        wait_for(lambda: len(h.bus.made("SetPointerBarriers")) == 2, what="the barriers removed")
        self.assertEqual(h.bus.made("SetPointerBarriers")[1][2], [])
        enables = len(h.bus.made("Enable"))
        h.models = models
        wait_for(lambda: len(h.bus.made("SetPointerBarriers")) == 3, what="the barriers back")
        wait_for(lambda: len(h.bus.made("Enable")) == enables + 1, what="enabled again")

    def test_a_stop_while_input_is_away_lets_go_and_ends_the_connection(self):
        h = self.make()
        h.start()
        h.cross()
        h.hooks.stop()
        self.assertEqual(len(h.releases()), 1)
        self.assertTrue(h.bus.closed and h.ei.closed)
        self.assertFalse(h.hooks.holding)

    def test_a_dead_input_connection_lets_go_ends_the_session_and_says_why(self):
        h = self.make()
        h.start()
        h.cross()
        h.ei.feed(("disconnect",))
        wait_for(lambda: h.failures, what="the failure")
        self.assertIn("stopped letting Beamer capture", h.failures[0].sentence)
        wait_for(lambda: h.bus.closed, what="the connection closed")
        self.assertEqual(len(h.releases()), 1)

    def test_a_stalled_thread_holding_input_has_its_connection_ended(self):
        saved = capture_portal.STALL_SECONDS
        capture_portal.STALL_SECONDS = 0.3
        try:
            h = self.make()
            h.start()
            h.activate()
            stalled = h.hooks._on_motion

            def stall(dx, dy):
                time.sleep(1.0)
                stalled(dx, dy)

            h.hooks._on_motion = stall
            h.ei.feed(("motion", h.pointer, 10.0, 0.0))
            wait_for(lambda: h.bus.aborted, timeout=2, what="the watchdog")
        finally:
            capture_portal.STALL_SECONDS = saved

    def test_a_refusal_is_said_in_the_apps_words_after_start_returns(self):
        h = self.make()
        h.bus.answers["CreateSession"] = (1, {})
        h.hooks.start()
        wait_for(lambda: h.failures, what="the refusal")
        self.assertEqual(h.failures[0].sentence, capture_portal.REFUSED)

    def test_an_input_capture_portal_reporting_zero_uses_v1_session_creation(self):
        h = self.make(version=0)
        with self.assertLogs(capture_portal.LOGGER, level="INFO") as logs:
            h.start()
        self.assertEqual(len(h.bus.made("CreateSession")), 1)
        self.assertEqual(h.bus.made("CreateSession2"), [])
        self.assertIn("Requesting InputCapture session", "\n".join(logs.output))

    def test_a_zero_version_input_capture_portal_reports_its_create_error(self):
        h = self.make(version=0)
        h.bus.failures = {"CreateSession": portal.PortalError("CreateSession: UnknownMethod")}
        with self.assertLogs(capture_portal.LOGGER, level="ERROR") as logs:
            h.hooks.start()
            wait_for(lambda: h.failures, what="the failed CreateSession")
        self.assertIsInstance(h.failures[0], capture_portal.CaptureUnavailable)
        self.assertIn("CreateSession: UnknownMethod", h.failures[0].sentence)
        self.assertIn("CreateSession: UnknownMethod", "\n".join(logs.output))

    # Review fixes (Codex Sol and a cold Opus pass, 01-10-2026)

    def test_a_release_that_fails_ends_the_session_rather_than_leaving_input_captured(self):
        h = self.make()
        h.start()
        h.activate()

        def broken(body):
            raise portal.PortalError("Release failed")

        h.bus.returns["Release"] = broken
        h.ei.feed(("motion", h.pointer, -5.0, 0.0))
        wait_for(lambda: h.failures, what="the failure")
        wait_for(lambda: h.bus.closed, what="the connection closed")

    def test_input_sent_away_with_nothing_captured_is_brought_home(self):
        h = self.make()
        h.start()
        h.away = True  # the tray, or a driven machine's move that passed input_held() a moment too early
        wait_for(lambda: h.homes, what="input home")
        self.assertFalse(h.away)

    def test_input_sent_away_from_another_thread_during_a_press_keeps_the_key_that_follows(self):
        h = self.make(push_to_cross=99)
        h.start()
        h.activate()
        h.away = True
        h.ei.feed(("key", h.keyboard, 1, True), ("frame", h.keyboard, 0))
        wait_for(lambda: h.keys, what="the key away")
        self.assertEqual(h.keys, [("esc", True, 1)])
        self.assertEqual(h.releases(), [])

    def test_the_desktop_ending_a_press_the_sender_already_took_away_brings_it_home(self):
        h = self.make(push_to_cross=99)
        h.start()
        h.activate()
        h.away = True
        h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Deactivated", (SESSION, vardict({"activation_id": 3})))
        wait_for(lambda: h.homes, what="input home")

    def test_a_barrier_request_the_portal_never_answers_ends_the_capture(self):
        saved = capture_portal.REQUEST_SECONDS
        capture_portal.REQUEST_SECONDS = 0.3
        try:
            h = self.make()
            h.bus.answers["SetPointerBarriers"] = None
            h.hooks.start()
            wait_for(lambda: h.failures, what="the failure")
        finally:
            capture_portal.REQUEST_SECONDS = saved

    def test_an_activation_with_no_id_is_still_let_go(self):
        h = self.make(push_to_cross=99)
        h.start()
        h.ei.feed(("start", h.pointer, 1))
        h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Activated",
                   (SESSION, vardict({"cursor_position": (1920.0, 500.0), "barrier_id": 1})))
        wait_for(lambda: h.hooks.holding, what="the capture")
        h.ei.feed(("motion", h.pointer, -5.0, 0.0))
        wait_for(lambda: h.bus.made("Release"), what="the let-go")
        self.assertNotIn("activation_id", h.bus.made("Release")[0][1])

    def test_a_push_that_meets_two_barriers_at_once_finds_its_wall_from_the_pointer(self):
        h = self.make(models=[return_edge.CornerPush("top_right", "right")], push_to_cross=99)
        h.start()
        h.activate(x=1920.0, y=3.0, barrier=0)
        h.ei.feed(("motion", h.pointer, 4.0, -4.0))
        wait_for(lambda: h.motions, what="the corner push")
        self.assertTrue(h.hooks.holding)

    def test_input_from_an_earlier_activation_never_feeds_a_later_one(self):
        h = self.make()
        h.start()
        h.ei.feed(("start", h.pointer, 3), ("motion", h.pointer, 10.0, 0.0), ("motion", h.pointer, 10.0, 0.0),
                  ("stop", h.pointer), ("start", h.pointer, 4))
        time.sleep(0.15)
        h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Activated",
                   (SESSION, vardict({"activation_id": 4, "cursor_position": (1920.0, 9.0), "barrier_id": 1})))
        wait_for(lambda: h.hooks.holding, what="the capture")
        time.sleep(0.15)
        self.assertEqual(h.motions, [])

    def test_a_landing_reported_while_the_release_is_in_flight_still_places_the_pointer(self):
        h = self.make()
        h.start()
        h.cross()
        moved = []
        desktop_portal.set_mover(lambda x, y: moved.append((x, y)))

        def late_landing(body):
            desktop_portal.note_position(0, 640)
            return ()

        h.bus.returns["Release"] = late_landing
        h.away = False
        wait_for(lambda: moved, timeout=2, what="the late landing")
        self.assertEqual(moved, [(0, 640)])
        desktop_portal.set_mover(None)

    # Second review (Opus, 01-10-2026)

    def test_a_corner_push_reported_off_the_barrier_line_finds_its_wall(self):
        for corner, point, push in (("bottom_right", (1919.0, 1079.0), (4.0, 4.0)), ("top_right", (1950.0, 3.0), (4.0, -4.0))):
            h = self.make(models=[return_edge.CornerPush(corner, "right")], push_to_cross=99)
            h.start()
            h.activate(x=point[0], y=point[1], barrier=0)
            h.ei.feed(("motion", h.pointer, *push))
            wait_for(lambda: h.motions, what=f"the {corner} push")
            self.assertTrue(h.hooks.holding, corner)
            h.hooks.stop()

    def test_input_away_with_nothing_captured_keeps_being_brought_home(self):
        h = self.make()
        h.hooks.on_home = lambda: h.homes.append(True)  # a sender slow to come home
        h.start()
        h.away = True
        wait_for(lambda: len(h.homes) >= 2, timeout=3, what="a second try")

    def test_a_newer_captures_input_ahead_of_its_signal_is_kept_for_it(self):
        h = self.make()
        h.start()
        h.activate(activation=3)
        h.ei.feed(("motion", h.pointer, -5.0, 0.0))
        wait_for(lambda: h.releases(), what="the let-go")
        h.ei.feed(("stop", h.pointer), ("stop", h.keyboard))
        time.sleep(0.1)
        # The next capture's input arrives before its Activated signal.
        h.ei.feed(("start", h.pointer, 4), ("motion", h.pointer, 10.0, 0.0))
        time.sleep(0.1)
        h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Activated",
                   (SESSION, vardict({"activation_id": 4, "cursor_position": (1920.0, 9.0), "barrier_id": 1})))
        wait_for(lambda: h.motions, what="the newer capture's push")

    def test_a_capture_the_desktop_never_announces_is_let_go(self):
        h = self.make()
        h.start()
        h.ei.feed(("start", h.pointer, 6), ("motion", h.pointer, 10.0, 0.0))
        wait_for(lambda: h.releases(), timeout=capture_portal.EARLY_SECONDS + 1.5, what="the let-go")
        self.assertEqual(h.releases()[0][0], 6)

    def test_repeated_disabled_signals_do_not_spin(self):
        h = self.make()
        h.start()
        for _ in range(5):
            h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Disabled", (SESSION, {}))
        time.sleep(0.4)
        self.assertLessEqual(len(h.bus.made("SetPointerBarriers")), 2)

    def test_version_two_starts_with_the_kept_token_and_keeps_the_new_one(self):
        h = self.make(version=2, tokens=MemoryTokens("old"))
        h.bus.answers["Start"] = (0, {"capabilities": 3, "restore_token": "new"})
        h.start()
        self.assertEqual(h.bus.made("CreateSession"), [])
        options = h.bus.made("Start")[0][2]
        self.assertEqual((options["restore_token"], options["persist_mode"]), (("s", "old"), ("u", 2)))
        self.assertEqual(h.tokens.token, "new")

    def test_a_refusal_under_version_two_forgets_the_token(self):
        h = self.make(version=2, tokens=MemoryTokens("old"))
        h.bus.answers["Start"] = (1, {})
        h.hooks.start()
        wait_for(lambda: h.failures, what="the refusal")
        self.assertEqual(h.tokens.token, "")


if __name__ == "__main__":
    unittest.main()
