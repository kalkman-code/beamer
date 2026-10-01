"""The Mac controller's event tap, its pointer and its local effects, with the links faked. The
wire itself is core/tests/test_link.py's; where input goes, and the clipboard, are also
test_mac_links.py's. The names re-exported from bridge_fakes below are still imported from here by
the test modules that have not moved to bridge_fakes."""

import errno
import logging
import os
import queue
import sys
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config
import media_keys
from core import protocol
from bridge import (
    CONNECT_TIMEOUT_SECONDS,
    CROSSING_RETRY_SECONDS,
    KVMController,
    QuartzEventTranslator,
)
from bridge_fakes import (  # noqa: F401  (re-exported)
    FAKE_PNG,
    PAIRED_TOKEN,
    FakeClipboard,
    FakeClock,
    FakeGestureEvent,
    FakeQuartz,
    crossing_config,
    make_config,
    quiet_logger,
    settle,
)
from fake_link import OTHER_ID, PEER_ID, FakeLink, bring_up
from gestures import CHORD, DOCK_CONTROL_TYPE, MAGNIFY_TYPE, SWIPE_TYPE
from input_injector_mac import INJECTED_MARK


def make_controller(clock, cfg=None, **overrides):
    """A controller whose links are FakeLinks and whose clipboard is not the pasteboard."""
    options = dict(
        logger=quiet_logger(), quartz=FakeQuartz, clock=clock, clipboard=FakeClipboard(),
        link_factory=FakeLink, desktop_bounds=lambda: (0, 0, 1920, 1080),
    )
    options.update(overrides)
    return KVMController(cfg or make_config(PAIRED_TOKEN), **options)


def drain(controller):
    messages = []
    while not controller.outbound.empty():
        messages.append(controller.outbound.get_nowait())
    return messages



class TranslatorTests(unittest.TestCase):
    def setUp(self):
        self.translator = QuartzEventTranslator(FakeQuartz)
        self.key_map = dict(config.DEFAULT_KEY_MAP)

    def test_option_held_sends_the_key_not_the_composed_character(self):
        # Option+V composes U+221A on macOS; Windows needs "v" or the Alt+V
        # chord does not exist there.
        self.translator.modifier_down.add(0x3A)
        event = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x09,
            FakeQuartz.kCGKeyboardEventAutorepeat: 0,
            "unicode": "\u221a",
        }
        result = self.translator.key_result(
            FakeQuartz.kCGEventKeyDown, event, 0x3D, self.key_map
        )
        self.assertEqual(result.messages, [
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "v", "us": "v"}}
        ])

    def test_shift_still_composes_its_character(self):
        self.translator.modifier_down.add(0x38)
        event = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x09,
            FakeQuartz.kCGKeyboardEventAutorepeat: 0,
            "unicode": "V",
        }
        result = self.translator.key_result(
            FakeQuartz.kCGEventKeyDown, event, 0x3D, self.key_map
        )
        self.assertEqual(result.messages, [
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "V", "us": "v"}}
        ])

    def test_printable_key_uses_unicode(self):
        event = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x00,
            FakeQuartz.kCGKeyboardEventAutorepeat: 0,
            "unicode": "å",
        }
        result = self.translator.key_result(
            FakeQuartz.kCGEventKeyDown,
            event,
            0x3D,
            self.key_map,
        )
        self.assertEqual(result.messages, [
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "å", "us": "a"}}
        ])

    def test_printable_keyup_reuses_keydown_character(self):
        down = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x00,
            FakeQuartz.kCGKeyboardEventAutorepeat: 0,
            "unicode": "A",
        }
        up = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x00,
            FakeQuartz.kCGKeyboardEventAutorepeat: 0,
            "unicode": "",
        }
        self.translator.key_result(FakeQuartz.kCGEventKeyDown, down, 0x3D, self.key_map)
        result = self.translator.key_result(FakeQuartz.kCGEventKeyUp, up, 0x3D, self.key_map)
        self.assertEqual(result.messages, [
            {"type": protocol.MSG_KEYUP, "data": {"key": "A", "us": "a"}}
        ])

    def test_command_modifier_uses_configured_mapping(self):
        event = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x37,
            "flags": FakeQuartz.kCGEventFlagMaskCommand,
        }
        result = self.translator.key_result(
            FakeQuartz.kCGEventFlagsChanged,
            event,
            0x3D,
            self.key_map,
        )
        self.assertTrue(result.is_down)
        self.assertEqual(result.messages, [
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "ctrl"}}
        ])

    def test_control_modifier_sends_the_windows_key(self):
        event = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x3B,
            "flags": FakeQuartz.kCGEventFlagMaskControl,
        }
        result = self.translator.key_result(
            FakeQuartz.kCGEventFlagsChanged,
            event,
            0x3D,
            self.key_map,
        )
        self.assertTrue(result.is_down)
        self.assertEqual(result.messages, [
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "cmd"}}
        ])

    def test_right_option_flags_changed_is_the_trigger(self):
        event = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x3D,
            "flags": FakeQuartz.kCGEventFlagMaskAlternate,
        }
        result = self.translator.key_result(
            FakeQuartz.kCGEventFlagsChanged,
            event,
            0x3D,
            self.key_map,
        )
        self.assertTrue(result.is_trigger)
        self.assertTrue(result.is_down)
        self.assertEqual(result.messages, [])

    def test_missed_keyup_does_not_desync_the_next_keydown(self):
        # The old toggle-based tracking flipped is_down by membership in
        # modifier_down; if a key-up was ever missed (tap timeout/restart),
        # every subsequent transition for that key was inverted. Detection
        # must now be derived from the event's own flags instead.
        down_event = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x3D,
            "flags": FakeQuartz.kCGEventFlagMaskAlternate | 0x0040,  # + right-alt device bit
        }
        first = self.translator.key_result(
            FakeQuartz.kCGEventFlagsChanged, down_event, 0x00, self.key_map
        )
        self.assertTrue(first.is_down)
        # Simulate the missed key-up: bookkeeping is left stuck as if the key
        # were still held.
        self.assertIn(0x3D, self.translator.modifier_down)
        second = self.translator.key_result(
            FakeQuartz.kCGEventFlagsChanged, down_event, 0x00, self.key_map
        )
        self.assertTrue(second.is_down)

    def test_mouse_delta_and_middle_button_match_wire_vocabulary(self):
        movement = {
            FakeQuartz.kCGMouseEventDeltaX: 4,
            FakeQuartz.kCGMouseEventDeltaY: -3,
        }
        self.assertEqual(
            self.translator.mouse_messages(FakeQuartz.kCGEventMouseMoved, movement),
            [{"type": protocol.MSG_MOUSEMOVE, "data": {"dx": 4, "dy": -3}}],
        )
        middle = {FakeQuartz.kCGMouseEventButtonNumber: 2}
        self.assertEqual(
            self.translator.mouse_messages(FakeQuartz.kCGEventOtherMouseDown, middle),
            [{"type": protocol.MSG_MOUSEDOWN, "data": {"button": "middle"}}],
        )

    def test_subpixel_mouse_deltas_accumulate_and_carry_remainder(self):
        # Slow, precise trackpad motion reports fractional per-event deltas.
        # Reading them as integers (the old behaviour) truncates every one of
        # these to zero and the motion is lost outright.
        tiny_move = {
            FakeQuartz.kCGMouseEventDeltaX: 0.4,
            FakeQuartz.kCGMouseEventDeltaY: 0.0,
        }
        self.assertEqual(
            self.translator.mouse_messages(FakeQuartz.kCGEventMouseMoved, tiny_move), []
        )
        self.assertEqual(
            self.translator.mouse_messages(FakeQuartz.kCGEventMouseMoved, tiny_move), []
        )
        # 0.4 + 0.4 + 0.4 = 1.2 -> emits the whole pixel, carries 0.2.
        self.assertEqual(
            self.translator.mouse_messages(FakeQuartz.kCGEventMouseMoved, tiny_move),
            [{"type": protocol.MSG_MOUSEMOVE, "data": {"dx": 1, "dy": 0}}],
        )
        # 0.2 + 0.4 = 0.6 -> still below a whole pixel.
        self.assertEqual(
            self.translator.mouse_messages(FakeQuartz.kCGEventMouseMoved, tiny_move), []
        )

    def test_reset_mouse_accumulators_clears_carried_remainder(self):
        tiny_move = {
            FakeQuartz.kCGMouseEventDeltaX: 0.4,
            FakeQuartz.kCGMouseEventDeltaY: -0.4,
        }
        self.translator.mouse_messages(FakeQuartz.kCGEventMouseMoved, tiny_move)
        self.assertNotEqual(self.translator._move_accum_x, 0.0)
        self.assertNotEqual(self.translator._move_accum_y, 0.0)
        self.translator.reset_mouse_accumulators()
        self.assertEqual(self.translator._move_accum_x, 0.0)
        self.assertEqual(self.translator._move_accum_y, 0.0)
        # The carried 0.4 is gone: another 0.4 alone must not yet round up.
        self.assertEqual(
            self.translator.mouse_messages(FakeQuartz.kCGEventMouseMoved, tiny_move), []
        )

    def test_discrete_scroll_wheel_is_line_mode_with_horizontal_delta(self):
        event = {
            FakeQuartz.kCGScrollWheelEventIsContinuous: 0,
            FakeQuartz.kCGScrollWheelEventDeltaAxis1: 2,
            FakeQuartz.kCGScrollWheelEventDeltaAxis2: -1,
        }
        self.assertEqual(
            self.translator.mouse_messages(FakeQuartz.kCGEventScrollWheel, event),
            [{"type": protocol.MSG_SCROLL, "data": {"dy": 2, "dx": -1, "mode": "line"}}],
        )

    def test_continuous_scroll_wheel_is_pixel_mode_with_float_deltas(self):
        event = {
            FakeQuartz.kCGScrollWheelEventIsContinuous: 1,
            FakeQuartz.kCGScrollWheelEventPointDeltaAxis1: 12.5,
            FakeQuartz.kCGScrollWheelEventPointDeltaAxis2: -3.25,
        }
        self.assertEqual(
            self.translator.mouse_messages(FakeQuartz.kCGEventScrollWheel, event),
            [{"type": protocol.MSG_SCROLL, "data": {"dy": 12.5, "dx": -3.25, "mode": "pixel"}}],
        )

    def test_scroll_wheel_with_no_motion_on_either_axis_is_dropped(self):
        event = {
            FakeQuartz.kCGScrollWheelEventIsContinuous: 0,
            FakeQuartz.kCGScrollWheelEventDeltaAxis1: 0,
            FakeQuartz.kCGScrollWheelEventDeltaAxis2: 0,
        }
        self.assertEqual(
            self.translator.mouse_messages(FakeQuartz.kCGEventScrollWheel, event), []
        )


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.controller = make_controller(self.clock)
        self.addCleanup(self.controller.stop_event.set)
        self.alerts = []
        self.controller.on_user_alert = lambda title, message: self.alerts.append((title, message))
        self.own = protocol.id_text(self.controller.identity()["id"])

    def _tap(self, event_type, event):
        return self.controller._event_tap_callback(None, event_type, event, None)

    def _double_tap(self):
        down = {FakeQuartz.kCGKeyboardEventKeycode: 0x3D, "flags": FakeQuartz.kCGEventFlagMaskAlternate}
        up = {FakeQuartz.kCGKeyboardEventKeycode: 0x3D, "flags": 0}
        self._tap(FakeQuartz.kCGEventFlagsChanged, down)
        self._tap(FakeQuartz.kCGEventFlagsChanged, up)
        self.clock.value = 10.2
        return (
            self._tap(FakeQuartz.kCGEventFlagsChanged, down),
            self._tap(FakeQuartz.kCGEventFlagsChanged, up),
        )

    def test_local_event_passes_through(self):
        event = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x00,
            FakeQuartz.kCGKeyboardEventAutorepeat: 0,
            "unicode": "a",
        }
        returned = self._tap(FakeQuartz.kCGEventKeyDown, event)
        self.assertIs(returned, event)
        self.assertTrue(self.controller.outbound.empty())

    def test_redirected_event_is_queued_and_swallowed(self):
        event = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x00,
            FakeQuartz.kCGKeyboardEventAutorepeat: 0,
            "unicode": "a",
        }
        self.controller.redirecting = True
        returned = self._tap(FakeQuartz.kCGEventKeyDown, event)
        self.assertIsNone(returned)
        self.assertEqual(
            self.controller.outbound.get_nowait(),
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "a", "us": "a"}},
        )

    def test_the_tap_queues_physical_key_names_and_the_link_gets_the_peers(self):
        link = bring_up(self.controller)
        self.assertTrue(self.controller.set_redirecting(True))
        command = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x37,
            "flags": FakeQuartz.kCGEventFlagMaskCommand,
        }
        self.assertIsNone(self._tap(FakeQuartz.kCGEventFlagsChanged, command))
        queued = drain(self.controller)
        self.assertEqual(queued, [{"type": protocol.MSG_KEYDOWN, "data": {"key": "cmd"}}])
        for message in queued:
            self.controller._process_outbound(message)
        self.assertEqual(link.sent, [{"type": protocol.MSG_KEYDOWN, "data": {"key": "ctrl"}}])

    def test_queue_failure_restores_local_before_returning_event(self):
        event = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x00,
            FakeQuartz.kCGKeyboardEventAutorepeat: 0,
            "unicode": "a",
        }
        self.controller.outbound = queue.Queue(maxsize=1)
        self.controller.outbound.put_nowait({"full": True})
        self.controller.redirecting = True
        returned = self._tap(FakeQuartz.kCGEventKeyDown, event)
        self.assertIs(returned, event)
        self.assertFalse(self.controller.redirecting)

    def test_double_tap_right_option_only_flips_redirect_flag(self):
        link = bring_up(self.controller)
        down = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x3D,
            "flags": FakeQuartz.kCGEventFlagMaskAlternate,
        }
        up = {FakeQuartz.kCGKeyboardEventKeycode: 0x3D, "flags": 0}
        first_down = self._tap(FakeQuartz.kCGEventFlagsChanged, down)
        first_up = self._tap(FakeQuartz.kCGEventFlagsChanged, up)
        self.clock.value = 10.2
        second_down = self._tap(FakeQuartz.kCGEventFlagsChanged, down)
        second_up = self._tap(FakeQuartz.kCGEventFlagsChanged, up)
        self.assertIs(first_down, down)
        self.assertIs(first_up, up)
        self.assertIsNone(second_down)
        self.assertIsNone(second_up)
        self.assertTrue(self.controller.redirecting)
        self.assertTrue(link.live())
        self.assertEqual(link.dropped, [])
        self.assertFalse(link.stopped)

    def test_a_failed_connection_while_redirecting_brings_input_home_and_alerts_at_once(self):
        # Otherwise input would simply vanish into a dead connection.
        link = bring_up(self.controller)
        self.assertTrue(self.controller.set_redirecting(True))
        self.controller._connection_failed("Windows stopped responding")
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(link.dropped, ["Windows stopped responding"])
        self.assertEqual(self.controller.connection_status, "Windows stopped responding")
        self.assertEqual(self.alerts, [("Beamer", "Input returned to this Mac")])

    def test_a_failed_connection_while_idle_drops_the_link_without_an_alert(self):
        link = bring_up(self.controller)
        self.controller._connection_failed("Windows stopped responding")
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(link.dropped, ["Windows stopped responding"])
        self.assertEqual(self.controller.connection_status, "Windows stopped responding")
        self.assertEqual(self.alerts, [])

    def test_input_is_dropped_while_not_redirecting(self):
        link = bring_up(self.controller)
        self.controller._process_outbound({"type": protocol.MSG_KEYDOWN, "data": {"key": "a"}})
        self.assertEqual(link.sent, [])

    def test_set_redirecting_true_refuses_while_receiving(self):
        # Redirecting and receiving must never both be True: a Mac driven by another machine
        # and driving one at once is a machine with two owners.
        bring_up(self.controller)
        self.controller.receiving = True
        self.controller.set_redirecting(True)
        self.assertFalse(
            self.controller.redirecting and self.controller.receiving,
            "redirecting and receiving are both True at once",
        )

    def test_switching_while_receiving_sends_the_pcs_input_home(self):
        # Seen in practice: the PC's mouse was on this Mac, its push back through
        # the edge never fired, and the menu's switch only said Windows was
        # already driving. Switching now sends the PC's input home instead.
        sent_home = []
        bring_up(self.controller)
        self.controller.receiving = True
        self.controller.send_peer_home = lambda: sent_home.append(True) or True
        self.assertTrue(self.controller.set_redirecting(True))
        self.assertEqual(sent_home, [True])
        self.assertFalse(self.controller.redirecting)

    def test_switching_while_receiving_with_the_pc_unreachable_is_refused(self):
        bring_up(self.controller)
        self.controller.receiving = True
        self.controller.send_peer_home = lambda: False
        self.assertFalse(self.controller.set_redirecting(True))
        self.assertFalse(self.controller.redirecting)

    def test_this_macs_trigger_sends_the_pcs_input_home_while_receiving(self):
        sent_home = []
        bring_up(self.controller)
        self.controller.receiving = True
        self.controller.send_peer_home = lambda: sent_home.append(True) or True
        second_down, second_up = self._double_tap()
        self.assertIsNone(second_down)
        self.assertIsNone(second_up)
        self.assertEqual(sent_home, [True])
        self.assertFalse(self.controller.redirecting)

    def test_the_pcs_injected_trigger_is_not_this_macs_while_receiving(self):
        sent_home = []
        bring_up(self.controller)
        self.controller.receiving = True
        self.controller.send_peer_home = lambda: sent_home.append(True) or True
        down = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x3D,
            "flags": FakeQuartz.kCGEventFlagMaskAlternate,
            FakeQuartz.kCGEventSourceUserData: INJECTED_MARK,
        }
        for _ in range(2):
            self.assertIs(self._tap(FakeQuartz.kCGEventFlagsChanged, down), down)
        self.assertEqual(sent_home, [])

    def test_sending_to_windows_switched_off_refuses_the_switch(self):
        bring_up(self.controller)
        self.controller.cfg.send_to_windows = False
        self.assertFalse(self.controller.set_redirecting(True))
        self.assertFalse(self.controller.redirecting)

    def test_sending_to_windows_switched_off_still_sends_the_pcs_input_home(self):
        # Each switch covers one direction: the PC's input on this Mac still goes home.
        sent_home = []
        bring_up(self.controller)
        self.controller.cfg.send_to_windows = False
        self.controller.receiving = True
        self.controller.send_peer_home = lambda: sent_home.append(True) or True
        self.assertTrue(self.controller.set_redirecting(True))
        self.assertEqual(sent_home, [True])

    def test_alert_fired_when_double_tap_cannot_switch_because_disconnected(self):
        self.controller.connection_status = "Waiting to connect"
        self.assertFalse(self.controller.set_redirecting(True))
        self.assertEqual(
            self.alerts,
            [("Beamer", "Cannot switch — Waiting to connect")],
        )

    def test_alert_callback_exception_does_not_propagate(self):
        def boom(title, message):
            raise RuntimeError("synthetic notification failure")

        self.controller.on_user_alert = boom
        self.controller.connection_status = "Waiting to connect"
        # Must not raise even though the callback is broken.
        self.assertFalse(self.controller.set_redirecting(True))

    def test_double_tap_survives_a_missed_key_up(self):
        # Regression: the old toggle-based modifier tracking desynced when a
        # key-up went missing (e.g. a tap restart never delivered it), which
        # silently killed the double-tap trigger on the very next press.
        bring_up(self.controller)
        down = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x3D,
            "flags": FakeQuartz.kCGEventFlagMaskAlternate | 0x0040,
        }
        # Simulate the effect of the missed key-up: stale bookkeeping left
        # over as if the trigger key were still held down.
        self.controller.translator.modifier_down.add(0x3D)
        first_down = self._tap(FakeQuartz.kCGEventFlagsChanged, down)
        self.clock.value = 10.2
        second_down = self._tap(FakeQuartz.kCGEventFlagsChanged, down)
        self.assertIs(first_down, down)
        self.assertIsNone(second_down)
        self.assertTrue(self.controller.redirecting)

    def test_worker_exception_forces_local(self):
        self.controller.redirecting = True

        def fail():
            raise RuntimeError("synthetic failure")

        self.controller._thread_entry("synthetic", fail)
        self.assertFalse(self.controller.redirecting)

    def test_outbound_worker_survives_a_bad_message(self):
        # The only thread that drains the queue is never restarted, so a raise
        # inside one message must force input local and leave the worker
        # running for the next message.
        bring_up(self.controller)
        self.assertTrue(self.controller.set_redirecting(True))

        def fail(message):
            raise RuntimeError("synthetic routing failure")

        self.controller.owner.input = fail
        self.controller.outbound.put_nowait({"type": protocol.MSG_KEYDOWN, "data": {"key": "a"}})
        self.controller.stop_event.clear()

        def stop_after_one():
            self.controller.outbound.join()
            self.controller.stop_event.set()

        threading.Thread(target=stop_after_one, daemon=True).start()
        self.controller._outbound_worker()
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(self.controller.outbound.qsize(), 0)

    def test_every_way_home_tells_windows_while_the_link_is_up(self):
        # Windows keeps treating this Mac as driving, with whatever keys it was
        # holding still down, until it hears otherwise.
        link = bring_up(self.controller)
        for way_home in (
            lambda: self.controller._force_local("synthetic"),
            lambda: self._tap(FakeQuartz.kCGEventTapDisabledByTimeout, {}),
        ):
            with self.subTest(way_home=way_home):
                self.controller.event_tap = object()
                self.assertTrue(self.controller.set_redirecting(True))
                link.posted.clear()
                way_home()
                lets_go = [
                    message for message in link.posted
                    if message["type"] == protocol.MSG_FOCUS and message["data"]["target"] == self.own
                ]
                self.assertEqual(len(lets_go), 1)

    def test_every_way_home_reassociates_the_cursor(self):
        # A path that only cleared `redirecting` left the pointer decoupled
        # from the hand: keyboard back, pointer frozen.
        bring_up(self.controller)
        for way_home in (
            lambda: self.controller._connection_failed("synthetic drop"),
            lambda: self.controller._force_local("synthetic"),
            lambda: self.controller.update_config(crossing_config(PAIRED_TOKEN)),
            lambda: self.controller._thread_entry("synthetic", lambda: (_ for _ in ()).throw(RuntimeError())),
        ):
            with self.subTest(way_home=way_home):
                self.assertTrue(self.controller.set_redirecting(True))
                FakeQuartz.reset_cursor_spies()
                way_home()
                self.assertFalse(self.controller.redirecting)
                self.assertIn(True, FakeQuartz.associate_calls)

    def test_the_primary_link_gets_the_ssh_fallback_and_it_dials_the_local_tunnel(self):
        dialled = []
        sock = object()

        def socket_factory(address, timeout):
            dialled.append((address, timeout))
            return sock

        controller = make_controller(self.clock, socket_factory=socket_factory)
        link = controller._primary_link
        self.assertEqual(link.tunnel, controller._connect_via_ssh_fallback)
        self.assertIs(link.tunnel(), sock)
        self.assertEqual(dialled, [(("127.0.0.1", 24822), CONNECT_TIMEOUT_SECONDS)])

    def test_a_missing_ssh_tunnel_says_where_to_open_it(self):
        def socket_factory(address, timeout):
            raise ConnectionRefusedError("nothing listening")

        controller = make_controller(self.clock, socket_factory=socket_factory)
        with self.assertRaises(OSError) as caught:
            controller._primary_link.tunnel()
        self.assertEqual(caught.exception.errno, errno.EHOSTUNREACH)
        self.assertEqual(caught.exception.strerror, "Open /Applications/Beamer Tunnel.command")

    def test_set_redirecting_off_resets_mouse_accumulators(self):
        bring_up(self.controller)
        self.controller.redirecting = True
        self.controller.translator._move_accum_x = 0.4
        self.controller.translator._move_accum_y = -0.4
        self.controller.set_redirecting(False)
        self.assertEqual(self.controller.translator._move_accum_x, 0.0)
        self.assertEqual(self.controller.translator._move_accum_y, 0.0)


class ClipboardSyncTests(unittest.TestCase):
    """What the controller reads from and writes to the pasteboard. When the owner sends the
    clipboard and whether it takes a peer's is test_mac_links.py's ClipboardTests."""

    def setUp(self):
        self.clock = FakeClock()
        self.controller = make_controller(self.clock)
        self.link = bring_up(self.controller)
        self.link.posted.clear()

    def _take_with(self, clipboard):
        self.controller.clipboard = clipboard
        self.assertTrue(self.controller.set_redirecting(True))
        settle(self.controller)

    def _arrives_from_a_machine_just_let_go(self, message):
        self.controller.set_redirecting(True)
        self.controller.set_redirecting(False)
        self.link.arrives(self.controller, message, began=self.clock.value)

    def test_the_clipboard_follows_the_focus(self):
        self._take_with(FakeClipboard("hello from the Mac"))
        self.assertEqual(self.link.types(), [protocol.MSG_FOCUS, protocol.MSG_CLIPBOARD])
        self.assertEqual(self.link.posted[1], protocol.clipboard_msg("hello from the Mac"))

    def test_an_empty_clipboard_sends_the_focus_only(self):
        self._take_with(FakeClipboard(""))
        self.assertEqual(self.link.types(), [protocol.MSG_FOCUS])

    def test_an_oversized_text_is_skipped_but_the_focus_still_goes(self):
        self._take_with(FakeClipboard("z" * (protocol.CLIPBOARD_MAX_BYTES + 1)))
        self.assertEqual(self.link.types(), [protocol.MSG_FOCUS])

    def test_text_and_image_go_together(self):
        self._take_with(FakeClipboard("shot.png", FAKE_PNG))
        self.assertEqual(self.link.types(), [protocol.MSG_FOCUS, protocol.MSG_CLIPBOARD])
        self.assertEqual(self.link.posted[1], protocol.clipboard_msg("shot.png", FAKE_PNG))

    def test_an_image_alone_sends_no_text_key(self):
        self._take_with(FakeClipboard(None, FAKE_PNG))
        self.assertEqual(self.link.posted[1], protocol.clipboard_msg(None, FAKE_PNG))

    def test_an_oversized_image_is_dropped_but_the_text_is_sent(self):
        big = protocol.PNG_SIGNATURE + b"\x00" * protocol.CLIPBOARD_IMAGE_MAX_BYTES
        self._take_with(FakeClipboard("shot.png", big))
        self.assertEqual(self.link.posted[1], protocol.clipboard_msg("shot.png"))

    def test_an_inbound_clipboard_sets_text_and_image(self):
        fake = FakeClipboard()
        self.controller.clipboard = fake
        self._arrives_from_a_machine_just_let_go(protocol.clipboard_msg("shot.png", FAKE_PNG))
        self.assertEqual(fake.set_calls, [("shot.png", FAKE_PNG)])

    def test_an_oversized_inbound_clipboard_is_ignored(self):
        fake = FakeClipboard()
        self.controller.clipboard = fake
        self._arrives_from_a_machine_just_let_go(protocol.clipboard_msg("y" * (protocol.CLIPBOARD_MAX_BYTES + 1)))
        self.assertEqual(fake.set_calls, [])

    def test_a_malformed_inbound_image_is_ignored_but_the_text_is_kept(self):
        fake = FakeClipboard()
        self.controller.clipboard = fake
        for image in ("not base64!", "", protocol.clipboard_msg(None, b"GIF89a")["data"]["image"]):
            message = {"type": protocol.MSG_CLIPBOARD, "data": {"text": "t", "image": image, "image_format": "png"}}
            self._arrives_from_a_machine_just_let_go(message)
        self._arrives_from_a_machine_just_let_go(
            {"type": protocol.MSG_CLIPBOARD, "data": {"image": "AAAA", "image_format": "bmp"}}
        )
        self.assertEqual(fake.set_calls, [("t", None)] * 3)

    def test_an_inbound_clipboard_whose_image_is_not_a_string_is_malformed_and_ignored_whole(self):
        # An image that is not text makes the whole message malformed (protocol.read_clipboard),
        # so the text beside it is not kept, unlike an image that is only unreadable.
        fake = FakeClipboard()
        self.controller.clipboard = fake
        message = {"type": protocol.MSG_CLIPBOARD, "data": {"text": "t", "image": 42, "image_format": "png"}}
        self._arrives_from_a_machine_just_let_go(message)
        self.assertEqual(fake.set_calls, [])


class CursorPinningTests(unittest.TestCase):
    """Bug: on some macOS versions (seen on 27 beta)
    CGAssociateMouseAndMouseCursorPosition(False) doesn't hold, so the Mac
    cursor keeps moving even while input is redirected to Windows.
    KVMController compensates belt-and-braces: it pins the cursor location
    captured at redirect start and warps back to it on every captured mouse
    move/drag while redirecting."""

    def setUp(self):
        FakeQuartz.reset_cursor_spies()
        self.clock = FakeClock()
        self.controller = make_controller(self.clock)
        bring_up(self.controller)

    def test_pin_captured_and_association_disabled_at_redirect_start(self):
        self.assertTrue(self.controller.set_redirecting(True))
        self.assertEqual(self.controller.cursor_pin_point, FakeQuartz.cursor_pin_location)
        self.assertEqual(FakeQuartz.associate_calls, [False])

    def test_warp_called_on_every_mouse_move_while_redirecting(self):
        self.controller.set_redirecting(True)
        move_event = {
            FakeQuartz.kCGMouseEventDeltaX: 4,
            FakeQuartz.kCGMouseEventDeltaY: -3,
        }
        self.controller._event_tap_callback(None, FakeQuartz.kCGEventMouseMoved, move_event, None)
        self.controller._event_tap_callback(None, FakeQuartz.kCGEventMouseMoved, move_event, None)
        self.assertEqual(FakeQuartz.warp_calls, [FakeQuartz.cursor_pin_location] * 2)

    def test_warp_called_on_mouse_drag_while_redirecting(self):
        self.controller.set_redirecting(True)
        drag_event = {
            FakeQuartz.kCGMouseEventDeltaX: 1,
            FakeQuartz.kCGMouseEventDeltaY: 1,
        }
        self.controller._event_tap_callback(
            None, FakeQuartz.kCGEventLeftMouseDragged, drag_event, None
        )
        self.assertEqual(FakeQuartz.warp_calls, [FakeQuartz.cursor_pin_location])

    def test_warp_not_called_when_local(self):
        move_event = {
            FakeQuartz.kCGMouseEventDeltaX: 4,
            FakeQuartz.kCGMouseEventDeltaY: -3,
        }
        self.controller._event_tap_callback(None, FakeQuartz.kCGEventMouseMoved, move_event, None)
        self.assertEqual(FakeQuartz.warp_calls, [])

    def test_warp_not_called_for_key_events_while_redirecting(self):
        self.controller.set_redirecting(True)
        key_event = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x00,
            FakeQuartz.kCGKeyboardEventAutorepeat: 0,
            "unicode": "a",
        }
        self.controller._event_tap_callback(None, FakeQuartz.kCGEventKeyDown, key_event, None)
        self.assertEqual(FakeQuartz.warp_calls, [])

    def test_pin_and_association_cleared_on_redirect_end(self):
        self.controller.set_redirecting(True)
        self.controller.set_redirecting(False)
        self.assertIsNone(self.controller.cursor_pin_point)
        # The way home re-associates once as the owner's move is applied and once more in
        # _return_local, which is what every other way home ends in.
        self.assertEqual(FakeQuartz.associate_calls[0], False)
        self.assertEqual(set(FakeQuartz.associate_calls[1:]), {True})

    def test_association_status_logged_once_not_every_toggle(self):
        logger = logging.getLogger("kvm-bridge-tests.cursor-assoc")
        logger.setLevel(logging.INFO)
        controller = make_controller(self.clock, logger=logger)
        bring_up(controller)
        with self.assertLogs(logger, level="INFO") as captured:
            controller.set_redirecting(True)
            controller.set_redirecting(False)
        assoc_logs = [
            line for line in captured.output
            if "CGAssociateMouseAndMouseCursorPosition" in line
        ]
        self.assertEqual(len(assoc_logs), 1)

    def test_tap_disabled_while_redirecting_gives_the_cursor_back(self):
        # Input is forced local when macOS disables the tap, so the cursor
        # follows the hand again; it used to be re-pinned, which left the
        # pointer frozen while the keyboard already worked here.
        self.controller.set_redirecting(True)
        FakeQuartz.reset_cursor_spies()
        self.controller.event_tap = object()
        self.controller._event_tap_callback(
            None, FakeQuartz.kCGEventTapDisabledByTimeout, {}, None
        )
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(FakeQuartz.tap_enable_calls, [(self.controller.event_tap, True)])
        self.assertTrue(FakeQuartz.associate_calls)
        self.assertEqual(set(FakeQuartz.associate_calls), {True})

    def test_warp_error_logged_once_not_spammed(self):
        logger = logging.getLogger("kvm-bridge-tests.warp-error")
        logger.setLevel(logging.WARNING)

        class ErroringQuartz(FakeQuartz):
            @classmethod
            def CGWarpMouseCursorPosition(cls, point):
                cls.warp_calls.append(point)
                return 1  # non-zero: a synthetic CGError

        ErroringQuartz.reset_cursor_spies()
        controller = make_controller(self.clock, logger=logger, quartz=ErroringQuartz)
        bring_up(controller)
        controller.set_redirecting(True)
        move_event = {
            ErroringQuartz.kCGMouseEventDeltaX: 4,
            ErroringQuartz.kCGMouseEventDeltaY: -3,
        }
        with self.assertLogs(logger, level="WARNING") as captured:
            controller._event_tap_callback(None, ErroringQuartz.kCGEventMouseMoved, move_event, None)
            controller._event_tap_callback(None, ErroringQuartz.kCGEventMouseMoved, move_event, None)
        warp_warnings = [line for line in captured.output if "CGWarpMouseCursorPosition" in line]
        self.assertEqual(len(warp_warnings), 1)
        self.assertEqual(len(ErroringQuartz.warp_calls), 2)


class OverlayGestureTests(unittest.TestCase):
    """Bug 3: the CGEventTap-based gesture path (event types 29-32) is
    best-effort and was observed to never deliver on macOS 27 beta at all.
    KVMController.handle_overlay_gesture is the counterpart entry point fed
    by the invisible overlay NSPanel's ordinary AppKit responder methods
    (see kvm_bridge_app.py's GestureOverlay/_GestureCaptureView), which is
    fully-supported API and actually receives gestures."""

    def setUp(self):
        self.clock = FakeClock()
        self.controller = make_controller(self.clock)
        self.link = bring_up(self.controller)

    def test_overlay_gesture_translated_and_enqueued_while_redirecting(self):
        self.assertTrue(self.controller.set_redirecting(True))
        self.controller.handle_overlay_gesture(MAGNIFY_TYPE, FakeGestureEvent(magnification=0.05))
        drained = drain(self.controller)
        self.assertEqual(drained, [{"type": CHORD, "data": {"action": "zoom_in"}}])
        # Fed back through the normal outbound path, the chord reaches the
        # link in the shape the PC at its end answers to.
        for message in drained:
            self.controller._process_outbound(message)
        self.assertEqual(
            [message["type"] for message in self.link.sent],
            [protocol.MSG_KEYDOWN, protocol.MSG_SCROLL, protocol.MSG_KEYUP],
        )
        self.assertEqual([m["data"]["key"] for m in self.link.sent if "key" in m["data"]], ["ctrl", "ctrl"])

    def test_overlay_gesture_ignored_when_not_redirecting(self):
        self.controller.redirecting = False
        self.controller.handle_overlay_gesture(MAGNIFY_TYPE, FakeGestureEvent(magnification=0.05))
        self.assertTrue(self.controller.outbound.empty())

    def test_overlay_gesture_translator_exception_does_not_raise(self):
        self.controller.redirecting = True

        def boom(event_type, ns_event):
            raise RuntimeError("synthetic overlay translation failure")

        self.controller.gesture_translator.translate = boom
        # Must not raise even though the translator is broken.
        self.controller.handle_overlay_gesture(MAGNIFY_TYPE, FakeGestureEvent(magnification=0.05))
        self.assertTrue(self.controller.outbound.empty())

    def test_overlay_gesture_capture_logged_once_on_first_gesture(self):
        logger = logging.getLogger("kvm-bridge-tests.overlay-gesture-capture")
        logger.setLevel(logging.INFO)
        controller = make_controller(self.clock, logger=logger)
        controller.redirecting = True
        with self.assertLogs(logger, level="INFO") as captured:
            controller.handle_overlay_gesture(MAGNIFY_TYPE, FakeGestureEvent(magnification=0.01))
            controller.handle_overlay_gesture(MAGNIFY_TYPE, FakeGestureEvent(magnification=0.01))
        overlay_logs = [
            line for line in captured.output if "gesture capture active (overlay)" in line
        ]
        self.assertEqual(len(overlay_logs), 1)

    def test_overlay_gesture_swipe_translated_while_redirecting(self):
        self.controller.redirecting = True
        self.controller.handle_overlay_gesture(SWIPE_TYPE, FakeGestureEvent(deltaX=1.0))
        drained = []
        while not self.controller.outbound.empty():
            drained.append(self.controller.outbound.get_nowait())
        self.assertEqual(drained, [{"type": CHORD, "data": {"action": "back"}}])


class MediaKeyWiringTests(unittest.TestCase):
    """Exercises _event_tap_callback's NSSystemDefined branch with an
    injected system_event_converter, standing in for the real AppKit
    conversion, exactly as GestureWiringTests does for gestures."""

    SYSDEFINED = media_keys.NX_SYSDEFINED_EVENT_TYPE

    def _controller(self, converter):
        return make_controller(FakeClock(), system_event_converter=converter)

    @staticmethod
    def _data1(nx_code, down=True):
        return (nx_code << 16) | ((0x0A if down else 0x0B) << 8)

    def test_play_pause_is_forwarded_and_swallowed(self):
        controller = self._controller(lambda event: (8, self._data1(16)))
        controller.redirecting = True
        self.assertIsNone(
            controller._event_tap_callback(None, self.SYSDEFINED, object(), None)
        )
        self.assertEqual(
            controller.outbound.get_nowait(),
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "media_play_pause"}},
        )

    def test_volume_key_up_sends_keyup(self):
        controller = self._controller(lambda event: (8, self._data1(0, down=False)))
        controller.redirecting = True
        controller._event_tap_callback(None, self.SYSDEFINED, object(), None)
        self.assertEqual(
            controller.outbound.get_nowait(),
            {"type": protocol.MSG_KEYUP, "data": {"key": "volume_up"}},
        )

    def test_non_media_system_event_passes_through(self):
        # NX code 3 is brightness-up, which Windows has no equivalent for.
        controller = self._controller(lambda event: (8, self._data1(3)))
        controller.redirecting = True
        raw_event = object()
        self.assertIs(
            controller._event_tap_callback(None, self.SYSDEFINED, raw_event, None),
            raw_event,
        )
        self.assertTrue(controller.outbound.empty())

    def test_not_redirecting_passes_through_without_converting(self):
        calls = []

        def converter(event):
            calls.append(event)
            return (8, self._data1(16))

        controller = self._controller(converter)
        controller.redirecting = False
        raw_event = object()
        self.assertIs(
            controller._event_tap_callback(None, self.SYSDEFINED, raw_event, None),
            raw_event,
        )
        self.assertEqual(calls, [])

    def test_failed_conversion_passes_through_rather_than_disconnecting(self):
        def converter(event):
            raise RuntimeError("boom")

        controller = self._controller(converter)
        controller.redirecting = True
        raw_event = object()
        self.assertIs(
            controller._event_tap_callback(None, self.SYSDEFINED, raw_event, None),
            raw_event,
        )
        self.assertTrue(controller.redirecting)


class GestureWiringTests(unittest.TestCase):
    """Exercises _event_tap_callback's gesture branch with an injected
    gesture_event_converter, standing in for the real AppKit conversion.
    Never touches AppKit/Quartz gesture APIs, matching how the rest of this
    file fakes Quartz."""

    def setUp(self):
        self.clock = FakeClock()

    def _controller(self, converter):
        return make_controller(self.clock, gesture_event_converter=converter)

    def test_gesture_while_redirecting_is_translated_enqueued_and_swallowed(self):
        controller = self._controller(lambda event: FakeGestureEvent(deltaX=1.0))
        link = bring_up(controller)
        self.assertTrue(controller.set_redirecting(True))
        returned = controller._event_tap_callback(None, SWIPE_TYPE, object(), None)
        self.assertIsNone(returned)
        drained = drain(controller)
        self.assertEqual(drained, [{"type": CHORD, "data": {"action": "back"}}])
        # Fed back through the normal outbound path, the chord reaches the
        # link in the shape the PC at its end answers to.
        for message in drained:
            controller._process_outbound(message)
        self.assertEqual(link.sent, [
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "alt"}},
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "left"}},
            {"type": protocol.MSG_KEYUP, "data": {"key": "left"}},
            {"type": protocol.MSG_KEYUP, "data": {"key": "alt"}},
        ])

    def test_gesture_while_not_redirecting_is_passed_through_untouched(self):
        calls = []

        def converter(event):
            calls.append(event)
            return FakeGestureEvent(deltaX=1.0)

        controller = self._controller(converter)
        controller.redirecting = False
        raw_event = object()
        returned = controller._event_tap_callback(None, SWIPE_TYPE, raw_event, None)
        self.assertIs(returned, raw_event)
        self.assertTrue(controller.outbound.empty())
        # "Passed through untouched" means conversion is never even
        # attempted while not redirecting.
        self.assertEqual(calls, [])

    def test_gesture_conversion_failure_passes_through_and_capture_continues(self):
        def converter(event):
            raise RuntimeError("synthetic AppKit failure")

        controller = self._controller(converter)
        controller.redirecting = True
        raw_event = object()
        returned = controller._event_tap_callback(None, SWIPE_TYPE, raw_event, None)
        self.assertIs(returned, raw_event)
        self.assertTrue(controller.outbound.empty())
        # Fail open: a broken conversion must not force a full disconnect.
        self.assertTrue(controller.redirecting)
        # An ordinary keyboard event right afterwards proves capture as a
        # whole is unaffected by the failed gesture conversion.
        key_event = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x00,
            FakeQuartz.kCGKeyboardEventAutorepeat: 0,
            "unicode": "a",
        }
        keyboard_returned = controller._event_tap_callback(
            None, FakeQuartz.kCGEventKeyDown, key_event, None
        )
        self.assertIsNone(keyboard_returned)
        self.assertEqual(
            controller.outbound.get_nowait(),
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "a", "us": "a"}},
        )

    def test_swipe_while_redirecting_is_translated_and_swallowed(self):
        controller = self._controller(lambda event: FakeGestureEvent(deltaX=1.0))
        controller.redirecting = True
        returned = controller._event_tap_callback(None, SWIPE_TYPE, object(), None)
        self.assertIsNone(returned)
        drained = []
        while not controller.outbound.empty():
            drained.append(controller.outbound.get_nowait())
        self.assertEqual(drained, [{"type": CHORD, "data": {"action": "back"}}])

    def test_gesture_capture_active_logged_once_on_first_success(self):
        logger = logging.getLogger("kvm-bridge-tests.gesture-capture")
        logger.setLevel(logging.INFO)
        controller = make_controller(
            self.clock, logger=logger,
            gesture_event_converter=lambda event: FakeGestureEvent(magnification=0.01),
        )
        controller.redirecting = True
        with self.assertLogs(logger, level="INFO") as captured:
            # 0.01 stays below the 0.05 zoom step both times, so no
            # keydown/scroll/keyup messages are emitted at all -- the log
            # must still fire, since it only proves a gesture event arrived
            # and converted, not that it produced output.
            controller._event_tap_callback(None, SWIPE_TYPE, object(), None)
            controller._event_tap_callback(None, SWIPE_TYPE, object(), None)
        active_logs = [line for line in captured.output if "gesture capture active" in line]
        self.assertEqual(len(active_logs), 1)
        self.assertTrue(controller.outbound.empty())




class DockControlWiringTests(unittest.TestCase):
    """The tap's type-30 event is a system swipe (DockControl), read through
    an injected dock_event_reader standing in for the private CGEvent
    fields. While redirecting it is always swallowed, so Mission Control
    and Spaces stay shut on the Mac; classified or not."""

    def _controller(self, reader):
        return make_controller(FakeClock(), dock_event_reader=reader, macos_major=26)

    def _ended(self, motion, progress):
        return types.SimpleNamespace(hid=23, motion=motion, phase=4, progress=progress, velocity=0.0)

    def test_swipe_up_while_redirecting_becomes_one_gesture_message_and_is_swallowed(self):
        controller = self._controller(lambda event: self._ended(motion=2, progress=-0.4))
        controller.redirecting = True
        returned = controller._event_tap_callback(None, DOCK_CONTROL_TYPE, object(), None)
        self.assertIsNone(returned)
        self.assertEqual(controller.outbound.get_nowait(), protocol.gesture_msg("swipe_up"))
        self.assertTrue(controller.outbound.empty())

    def test_dock_event_while_not_redirecting_is_passed_through_unread(self):
        calls = []

        def reader(event):
            calls.append(event)
            return self._ended(motion=2, progress=-0.4)

        controller = self._controller(reader)
        controller.redirecting = False
        raw_event = object()
        self.assertIs(controller._event_tap_callback(None, DOCK_CONTROL_TYPE, raw_event, None), raw_event)
        self.assertEqual(calls, [])
        self.assertTrue(controller.outbound.empty())

    def test_unreadable_dock_event_is_still_swallowed_and_capture_continues(self):
        def reader(event):
            raise RuntimeError("synthetic field failure")

        controller = self._controller(reader)
        controller.redirecting = True
        self.assertIsNone(controller._event_tap_callback(None, DOCK_CONTROL_TYPE, object(), None))
        self.assertTrue(controller.outbound.empty())
        self.assertTrue(controller.redirecting)


class CrossingWiringTests(unittest.TestCase):
    """The engine is exercised in test_crossing.py; these prove the bridge
    feeds it the right things at the right moment and acts on the answer."""

    BOUNDS = (0, 0, 1728, 1117)

    def setUp(self):
        FakeQuartz.reset_cursor_spies()
        self.clock = FakeClock()
        self.feedback = []
        self.controller = self._controller(crossing_config(PAIRED_TOKEN))

    def _controller(self, cfg):
        controller = make_controller(
            self.clock, cfg, clipboard=FakeClipboard("copied here"), desktop_bounds=lambda: self.BOUNDS,
        )
        self.addCleanup(controller.stop_event.set)
        self.link = bring_up(controller)
        self.link.posted.clear()
        controller.on_crossing = lambda kind, step: self.feedback.append((kind, step))
        return controller

    def _move(self, x, y, dx, dy=0, event_type=FakeQuartz.kCGEventMouseMoved):
        event = {
            "location": (x, y),
            FakeQuartz.kCGMouseEventDeltaX: dx,
            FakeQuartz.kCGMouseEventDeltaY: dy,
        }
        self.clock.value += 0.01
        return event, self.controller._event_tap_callback(None, event_type, event, None)

    def _push(self, times, x=1727.0, y=558.0, dx=30, dy=0, event_type=FakeQuartz.kCGEventMouseMoved):
        returned = None
        for _ in range(times):
            event, returned = self._move(x, y, dx, dy, event_type)
            if self.controller.redirecting:
                break
        return event, returned

    def _focuses(self):
        return [message for message in self.link.posted if message["type"] == protocol.MSG_FOCUS]

    def _own(self):
        return protocol.read_id(protocol.id_text(self.controller.identity()["id"]))

    def test_movement_away_from_the_edge_passes_through(self):
        event, returned = self._move(800.0, 500.0, 30)
        self.assertIs(returned, event)
        self.assertEqual(FakeQuartz.warp_calls, [])
        self.assertFalse(self.controller.redirecting)

    def test_pushing_the_edge_swallows_and_warps_then_crosses_with_the_offset(self):
        self._move(1727.0, 558.0, 30)
        event, returned = self._move(1727.0, 558.0, 30)
        self.assertIsNone(returned, "the push is swallowed")
        self.assertEqual(FakeQuartz.warp_calls, [(1727.0, 558.0)])
        self.assertFalse(self.controller.redirecting)
        _event, returned = self._push(10)
        self.assertIsNone(returned)
        self.assertTrue(self.controller.redirecting)
        self.assertEqual(
            self._focuses(),
            [protocol.focus_v6(1, PEER_ID, edge="left", offset=0.5, resistance_px=120, reach=[])],
        )
        kinds = [kind for kind, _ in self.feedback]
        self.assertEqual(kinds[-1], "cross")
        self.assertEqual(kinds.count("tick"), 3)

    def test_a_machine_that_just_refused_is_not_pushed_at_until_its_back_off_ends(self):
        # A push that cannot take it must not pin the pointer and glow as if it would.
        self._push(12)
        self.link.arrives(self.controller, protocol.refuse_msg(self.controller.owner.route, "owned"))
        self.assertFalse(self.controller.redirecting)
        self.feedback.clear()
        FakeQuartz.reset_cursor_spies()
        event, returned = self._move(1727.0, 558.0, 30)
        event, returned = self._move(1727.0, 558.0, 30)
        self.assertIs(returned, event)
        self.assertEqual((FakeQuartz.warp_calls, self.feedback), ([], []))

    def test_this_macs_own_push_while_windows_drives_sends_the_pc_home_and_crosses(self):
        # Regression: this Mac's trackpad could not push back while the PC drove it. The push
        # itself is let through; this Mac's input follows once the PC's has let go.
        sent_home = []
        self.controller.receiving = True
        self.controller.send_peer_home = lambda: sent_home.append(True) or True
        for _ in range(10):
            _event, returned = self._move(1727.0, 558.0, 30)
            if sent_home:
                break
        self.assertIs(returned, _event)
        self.assertEqual(sent_home, [True])
        self.assertFalse(self.controller.redirecting)
        self.controller.set_receiving(False)
        deadline = time.monotonic() + 2
        while not self.controller.redirecting and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.controller.redirecting)
        focus = self._focuses()[0]["data"]
        self.assertEqual((focus["target"], focus["edge"], focus["offset"]), (protocol.id_text(PEER_ID), "left", 0.5))
        self.assertEqual(self.feedback[-1][0], "cross")

    def test_the_pcs_own_moves_never_push_while_it_drives(self):
        sent_home = []
        self.controller.receiving = True
        self.controller.send_peer_home = lambda: sent_home.append(True) or True
        for _ in range(10):
            event = {
                "location": (1727.0, 558.0),
                FakeQuartz.kCGMouseEventDeltaX: 30,
                FakeQuartz.kCGMouseEventDeltaY: 0,
                FakeQuartz.kCGEventSourceUserData: INJECTED_MARK,
            }
            self.assertIs(self.controller._event_tap_callback(None, FakeQuartz.kCGEventMouseMoved, event, None), event)
        self.assertEqual(sent_home, [])
        self.assertTrue(self.controller.receiving)
        self.assertEqual(FakeQuartz.warp_calls, [])

    def test_a_click_resets_this_macs_push_while_windows_drives(self):
        self.controller.receiving = True
        self.controller.send_peer_home = lambda: True
        self._push(3)
        self.assertGreater(self.controller.crossing.pressure, 0)
        event = {"location": (1727.0, 558.0)}
        self.assertIs(self.controller._event_tap_callback(None, FakeQuartz.kCGEventLeftMouseDown, event, None), event)
        self.assertEqual(self.controller.crossing.pressure, 0)

    def test_no_crossing_while_windows_drives_and_cannot_be_reached(self):
        self.controller.receiving = True
        self.controller.send_peer_home = lambda: False
        event, returned = self._push(10)
        self.assertIs(returned, event)
        self.assertTrue(self.controller.receiving)
        self.assertFalse(self.controller.redirecting)

    def test_no_crossing_while_disconnected(self):
        self.link.down(self.controller)
        event, returned = self._push(10)
        self.assertIs(returned, event)
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(FakeQuartz.warp_calls, [])

    def test_no_crossing_while_a_button_is_held(self):
        event, returned = self._push(10, event_type=FakeQuartz.kCGEventLeftMouseDragged)
        self.assertIs(returned, event)
        self.assertFalse(self.controller.redirecting)

    def test_a_mouse_button_resets_the_push(self):
        self._push(3)
        self.assertGreater(self.controller.crossing.pressure, 0)
        event = {"location": (1727.0, 558.0)}
        returned = self.controller._event_tap_callback(None, FakeQuartz.kCGEventLeftMouseDown, event, None)
        self.assertIs(returned, event)
        self.assertEqual(self.controller.crossing.pressure, 0)

    def test_shortcut_only_leaves_the_edge_alone(self):
        self.controller = self._controller(crossing_config(PAIRED_TOKEN, methods=["shortcut"]))
        event, returned = self._push(10)
        self.assertIs(returned, event)
        self.assertFalse(self.controller.redirecting)

    def test_a_failure_in_the_crossing_path_returns_the_event_and_disarms(self):
        self.controller.desktop_bounds = lambda: (_ for _ in ()).throw(RuntimeError("no displays"))
        self.controller._bounds_cache = None
        event, returned = self._move(1727.0, 558.0, 30)
        self.assertIs(returned, event)
        self.assertFalse(self.controller.redirecting)
        self.assertTrue(self.controller._crossing_failed)
        event, returned = self._move(1727.0, 558.0, 30)
        self.assertIs(returned, event)
        self.controller.update_config(crossing_config(PAIRED_TOKEN))
        self.assertFalse(self.controller._crossing_failed)

    def test_a_passing_failure_in_the_crossing_path_heals_by_itself(self):
        self.controller.desktop_bounds = lambda: (_ for _ in ()).throw(RuntimeError("no displays"))
        self.controller._bounds_cache = None
        self._move(1727.0, 558.0, 30)
        self.assertTrue(self.controller._crossing_failed)
        self.controller.desktop_bounds = lambda: self.BOUNDS
        self.controller._bounds_cache = None
        self.clock.value += CROSSING_RETRY_SECONDS
        _event, returned = self._push(10)
        self.assertIsNone(returned)
        self.assertTrue(self.controller.redirecting)

    def test_applying_settings_keeps_the_link(self):
        link = self.controller._primary_link
        self.controller.apply_settings(crossing_config(PAIRED_TOKEN, resistance_px=40, methods=["shortcut", "notch"]))
        self.assertIs(self.controller._primary_link, link)
        self.assertTrue(link.live())
        self.assertEqual(link.dropped, [])
        self.assertFalse(link.stopped)
        self.assertEqual(self.controller.crossing.resistance_px, 40)

    def test_shortcut_switch_takes_with_no_position_and_shows_no_arrival(self):
        self.controller.set_redirecting(True)
        self.assertEqual(self._focuses(), [protocol.focus_v6(1, PEER_ID, resistance_px=120, reach=[])])
        self.assertEqual(self.feedback, [])

    def test_inbound_switch_returns_input_and_lands_the_pointer(self):
        self.controller.set_redirecting(True)
        taken = len(self.link.posted)
        FakeQuartz.reset_cursor_spies()
        self.link.arrives(self.controller, protocol.switch_v6(1, self._own(), "right", 0.5))
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(FakeQuartz.warp_calls, [(1725.0, 558.0)])
        self.assertEqual([m["type"] for m in self.link.posted[taken:]], [protocol.MSG_FOCUS])
        self.assertEqual(self.feedback[-1][0], "arrive")
        # Where it landed, for the arrival effect.
        self.assertEqual((self.feedback[-1][1].mac_edge, self.feedback[-1][1].pin), ("right", (1725.0, 558.0)))

    def test_inbound_switch_without_a_position_still_returns_input(self):
        self.controller.set_redirecting(True)
        FakeQuartz.reset_cursor_spies()
        self.link.arrives(self.controller, protocol.switch_v6(1, self._own()))
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(FakeQuartz.warp_calls, [])
        self.assertIsNone(self.feedback[-1][1].pin)

    def _kinds(self):
        return [kind for kind, _step in self.feedback]

    def test_the_shortcut_home_reports_where_the_pointer_is(self):
        self.controller.set_redirecting(True)
        self.feedback.clear()
        self.controller.set_redirecting(False)
        self.assertEqual(self._kinds(), ["home"])
        self.assertEqual(self.feedback[0][1].pin, FakeQuartz.cursor_pin_location)

    def test_a_crossing_home_reports_only_its_own_arrival(self):
        self.controller.set_redirecting(True)
        self.feedback.clear()
        self.link.arrives(self.controller, protocol.switch_v6(1, self._own(), "right", 0.5))
        self.assertEqual(self._kinds(), ["arrive"])

    def test_the_pc_sending_input_home_by_its_switch_reports_it_once(self):
        self.controller.set_redirecting(True)
        self.feedback.clear()
        self.link.arrives(self.controller, protocol.switch_v6(1, self._own()))
        self.assertEqual(self._kinds(), ["arrive"])

    def test_the_pc_taking_this_mac_over_is_not_a_switch_home(self):
        self.controller.set_redirecting(True)
        self.feedback.clear()
        self.controller.set_receiving(True)
        self.assertNotIn("home", self._kinds())

    def test_a_dropped_link_shows_nothing(self):
        self.controller.set_redirecting(True)
        self.feedback.clear()
        self.controller._connection_failed("Windows stopped responding")
        self.assertEqual(self._kinds(), [])

    def test_switching_home_while_already_home_shows_nothing(self):
        self.controller.set_redirecting(False)
        self.assertEqual(self._kinds(), [])

    def test_inbound_switch_to_an_unknown_target_is_ignored(self):
        self.controller.set_redirecting(True)
        self.link.arrives(self.controller, protocol.switch_v6(1, OTHER_ID, "right", 0.5))
        self.assertTrue(self.controller.redirecting)

    def test_hold_style_redirects_only_while_the_key_is_down(self):
        cfg = crossing_config(PAIRED_TOKEN)
        cfg.trigger_style = "hold"
        self.controller = self._controller(cfg)
        down = {FakeQuartz.kCGKeyboardEventKeycode: 0x3D, "flags": FakeQuartz.kCGEventFlagMaskAlternate}
        up = {FakeQuartz.kCGKeyboardEventKeycode: 0x3D, "flags": 0}
        self.assertIsNone(self.controller._event_tap_callback(None, FakeQuartz.kCGEventFlagsChanged, down, None))
        self.assertTrue(self.controller.redirecting)
        self.assertIsNone(self.controller._event_tap_callback(None, FakeQuartz.kCGEventFlagsChanged, up, None))
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(
            self.link.types(),
            [protocol.MSG_FOCUS, protocol.MSG_CLIPBOARD, protocol.MSG_FOCUS],
        )

    def test_default_desktop_bounds_is_the_union_of_every_display(self):
        class DisplayQuartz(FakeQuartz):
            @staticmethod
            def CGGetActiveDisplayList(limit, displays, count):
                return 0, [1, 2], 2

            @staticmethod
            def CGDisplayBounds(display):
                rects = {
                    1: types.SimpleNamespace(
                        origin=types.SimpleNamespace(x=0.0, y=0.0),
                        size=types.SimpleNamespace(width=1728.0, height=1117.0),
                    ),
                    2: types.SimpleNamespace(
                        origin=types.SimpleNamespace(x=1728.0, y=-300.0),
                        size=types.SimpleNamespace(width=1920.0, height=1080.0),
                    ),
                }
                return rects[display]

        controller = make_controller(self.clock, crossing_config(PAIRED_TOKEN), quartz=DisplayQuartz, desktop_bounds=None)
        self.assertEqual(controller._default_desktop_bounds(), (0.0, -300.0, 3648.0, 1117.0))

    def test_a_sleeping_display_still_bounds_the_desktop(self):
        # Seen in practice: the active list is empty while the display sleeps; the first push after
        # waking raised and turned crossing off until settings were next saved.
        class SleepingQuartz(FakeQuartz):
            @staticmethod
            def CGGetActiveDisplayList(limit, displays, count):
                return 0, (), 0

            @staticmethod
            def CGGetOnlineDisplayList(limit, displays, count):
                return 0, [1], 1

            @staticmethod
            def CGDisplayBounds(display):
                return types.SimpleNamespace(
                    origin=types.SimpleNamespace(x=0.0, y=0.0),
                    size=types.SimpleNamespace(width=1728.0, height=1117.0),
                )

        controller = make_controller(self.clock, crossing_config(PAIRED_TOKEN), quartz=SleepingQuartz, desktop_bounds=None)
        self.assertEqual(controller._default_desktop_bounds(), (0.0, 0.0, 1728.0, 1117.0))

    def test_paused_crossing_leaves_the_edge_alone_while_the_shortcut_still_switches(self):
        self.controller.crossing_paused = True
        event, returned = self._push(10)
        self.assertIs(returned, event)
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(FakeQuartz.warp_calls, [])
        self._double_tap()
        self.assertTrue(self.controller.redirecting)
        self.controller.set_redirecting(False)
        self.controller.crossing_paused = False
        self._push(10)
        self.assertTrue(self.controller.redirecting, "resuming re-arms the edge with no other change")

    def test_a_full_screen_app_inhibits_the_pointer_methods_but_not_the_shortcut(self):
        self.controller.full_screen_app = "Steam"
        event, returned = self._push(10)
        self.assertIs(returned, event)
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(FakeQuartz.warp_calls, [])
        self._double_tap()
        self.assertTrue(self.controller.redirecting)
        self.controller.set_redirecting(False)
        self.controller.full_screen_app = None
        self._push(10)
        self.assertTrue(self.controller.redirecting)

    def test_with_the_hold_off_a_full_screen_app_does_not_hold_the_edges(self):
        self.controller.cfg.crossing["hold_full_screen"] = False
        self.controller.full_screen_app = "Steam"
        self.assertIsNone(self.controller.full_screen_app)
        self._push(10)
        self.assertTrue(self.controller.redirecting)

    def test_turning_the_hold_back_on_holds_for_the_app_still_in_front(self):
        self.controller.cfg.crossing["hold_full_screen"] = False
        self.controller.full_screen_app = "Steam"
        self.controller.cfg.crossing["hold_full_screen"] = True
        self.assertEqual(self.controller.full_screen_app, "Steam")

    def _double_tap(self):
        down = {FakeQuartz.kCGKeyboardEventKeycode: 0x3D, "flags": FakeQuartz.kCGEventFlagMaskAlternate}
        up = {FakeQuartz.kCGKeyboardEventKeycode: 0x3D, "flags": 0}
        for event in (down, up, down, up):
            self.clock.value += 0.05
            self.controller._event_tap_callback(None, FakeQuartz.kCGEventFlagsChanged, event, None)


class WindowsLockStatusTests(unittest.TestCase):
    """Windows says on its acks that it is clearing the lock screen, which the link reads as
    `peer_locked`, so the Mac can explain the pause instead of just looking dead."""

    def setUp(self):
        self.controller = make_controller(FakeClock())
        self.link = bring_up(self.controller)
        self.controller.connection_status = "Connected to 192.168.1.3:51820"

    def test_a_locked_peer_shows_the_unlock_and_clearing_it_restores_the_status(self):
        self.link.peer_locked = True
        self.assertTrue(self.controller.windows_locked)
        self.assertEqual(self.controller.connection_status, "Unlocking 192.0.2.10…")
        # Read again while still locked: the saved status must not be lost to the unlock text.
        self.assertEqual(self.controller.connection_status, "Unlocking 192.0.2.10…")
        self.link.peer_locked = False
        self.assertFalse(self.controller.windows_locked)
        self.assertEqual(self.controller.connection_status, "Connected to 192.168.1.3:51820")

    def test_the_ssh_fallback_wording_survives_an_unlock(self):
        """The status is restored verbatim, not rebuilt, so the macOS 27
        fallback does not silently become an ordinary connection."""
        self.controller.connection_status = "Connected to Windows via secure macOS 27 fallback"
        self.link.peer_locked = True
        self.link.peer_locked = False
        self.assertEqual(
            self.controller.connection_status,
            "Connected to Windows via secure macOS 27 fallback",
        )

    def test_a_peer_that_does_not_say_it_is_locked_is_not_locked(self):
        self.assertFalse(self.controller.windows_locked)
        self.assertEqual(self.controller.connection_status, "Connected to 192.168.1.3:51820")


class RoundTripTests(unittest.TestCase):
    """The figure shown in the control window is the link's samples (their smoothing and how stale
    they may be are core/tests/test_link.py's; the upper median is test_mac_links.py's); this is
    when the controller shows one at all."""

    def setUp(self):
        self.controller = make_controller(FakeClock())
        self.link = bring_up(self.controller)
        self.link.samples = [0.02]

    def test_nothing_is_shown_when_there_are_no_samples(self):
        self.link.samples = []
        self.assertTrue(self.controller.set_redirecting(True))
        self.assertIsNone(self.controller.round_trip_ms)

    def test_a_sample_is_shown_while_input_is_on_the_other_machine(self):
        self.assertTrue(self.controller.set_redirecting(True))
        self.assertEqual(self.controller.round_trip_ms, 20)

    def test_nothing_is_shown_when_input_is_not_on_the_other_machine(self):
        self.assertIsNone(self.controller.round_trip_ms)
        self.controller.set_redirecting(True)
        self.controller.set_redirecting(False)
        self.assertIsNone(self.controller.round_trip_ms)
        self.controller.set_redirecting(True)
        self.assertEqual(self.controller.round_trip_ms, 20)

    def test_nothing_is_shown_when_the_link_is_down(self):
        self.controller.set_redirecting(True)
        self.link.down(self.controller)
        self.assertIsNone(self.controller.round_trip_ms)


if __name__ == "__main__":
    unittest.main()
