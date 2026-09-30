import dataclasses
import errno
import json
import logging
import os
import queue
import struct
import threading
import types
import unittest
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config
import media_keys
from core import protocol
from bridge import (
    ACK_TIMEOUT_SECONDS,
    AUTH_FAILED_STATUS,
    CONNECT_TIMEOUT_SECONDS,
    CROSSING_RETRY_SECONDS,
    OLD_RECEIVER_STATUS,
    PING_INTERVAL_SECONDS,
    ROUND_TRIP_MAX_AGE_SECONDS,
    ROUND_TRIP_SAMPLES,
    SOCKET_IO_TIMEOUT_SECONDS,
    FrameDecoder,
    KVMController,
    QuartzEventTranslator,
    connection_error_message,
)
from gestures import DOCK_CONTROL_TYPE, MAGNIFY_TYPE, SWIPE_TYPE
from input_injector_mac import INJECTED_MARK


def link(controller, sock=None, token="synthetic-token"):
    """Hand `controller` a FakeSocket as _connect_once would: a new session
    with the preambles exchanged, so both ends hold this connection's keys."""
    sock = sock or FakeSocket()
    sock.peer = protocol.SecureSession(token)
    controller.session = protocol.SecureSession(controller.cfg.auth_token)
    controller.session.accept_preamble(sock.peer.preamble())
    sock.peer.accept_preamble(controller.session.preamble())
    sock.mac_preamble = controller.session.preamble()
    controller.sock = sock
    return sock


def sent_messages(controller, sock):
    """Open every frame `controller` sent over a FakeSocket, in order --
    counters must advance, so a frame can only be read in sequence. A fresh
    opener with the receiver's prefix each call, so this can be asked twice."""
    opener = protocol.SecureSession(sock.peer._token, prefix=sock.peer.prefix)
    opener.accept_preamble(sock.mac_preamble)
    return [opener.open(payload[protocol.HEADER_SIZE:]) for payload in sock.sent if payload != sock.mac_preamble]


def seed_receiver_reply(sock, *messages, token="synthetic-token"):
    """Queue a receiver's preamble on a FakeSocket, and replies it seals once
    the Mac's preamble has gone out, as a Windows receiver holding `token`
    would send them: a reply's key needs both prefixes."""
    sock.peer = protocol.SecureSession(token)
    sock.inbound.extend(sock.peer.preamble())
    sock.pending.extend(messages)
    return sock.peer


class FakeQuartz:
    kCGEventKeyDown = 10
    kCGEventKeyUp = 11
    kCGEventFlagsChanged = 12
    kCGEventMouseMoved = 5
    kCGEventLeftMouseDragged = 6
    kCGEventRightMouseDragged = 7
    kCGEventOtherMouseDragged = 27
    kCGEventLeftMouseDown = 1
    kCGEventLeftMouseUp = 2
    kCGEventRightMouseDown = 3
    kCGEventRightMouseUp = 4
    kCGEventOtherMouseDown = 25
    kCGEventOtherMouseUp = 26
    kCGEventScrollWheel = 22
    kCGEventTapDisabledByTimeout = 0xFFFFFFFE
    kCGEventTapDisabledByUserInput = 0xFFFFFFFF
    kCGEventSourceUserData = 42
    kCGKeyboardEventKeycode = 9
    kCGKeyboardEventAutorepeat = 8
    kCGMouseEventDeltaX = 4
    kCGMouseEventDeltaY = 5
    kCGMouseEventButtonNumber = 3
    kCGScrollWheelEventDeltaAxis1 = 11
    kCGScrollWheelEventDeltaAxis2 = 12
    kCGScrollWheelEventIsContinuous = 13
    kCGScrollWheelEventPointDeltaAxis1 = 96
    kCGScrollWheelEventPointDeltaAxis2 = 97
    kCGEventFlagMaskAlphaShift = 1 << 16
    kCGEventFlagMaskShift = 1 << 17
    kCGEventFlagMaskControl = 1 << 18
    kCGEventFlagMaskAlternate = 1 << 19
    kCGEventFlagMaskCommand = 1 << 20

    @staticmethod
    def CGEventGetIntegerValueField(event, field):
        return event.get(field, 0)

    @staticmethod
    def CGEventGetDoubleValueField(event, field):
        return float(event.get(field, 0))

    @staticmethod
    def CGEventGetFlags(event):
        return event.get("flags", 0)

    @staticmethod
    def CGEventKeyboardGetUnicodeString(event, maximum, length, characters):
        value = event.get("unicode", "")
        return len(value.encode("utf-16-le")) // 2, value

    # -- Cursor pinning (Bug 1: belt-and-braces warp) --------------------
    # Call lists are class-level so every KVMController built against
    # FakeQuartz in a test shares them; tests that care about counts reset
    # these in setUp, matching how InjectScrollDispatchTests (win_app) resets
    # its own module-level accumulator.
    cursor_pin_location = (111.0, 222.0)
    associate_calls = []
    warp_calls = []
    tap_enable_calls = []

    @classmethod
    def reset_cursor_spies(cls):
        cls.associate_calls = []
        cls.warp_calls = []
        cls.tap_enable_calls = []

    @staticmethod
    def CGEventCreate(source):
        return {"_fake_probe_event": True}

    @classmethod
    def CGEventGetLocation(cls, event):
        return event.get("location", cls.cursor_pin_location)

    @classmethod
    def CGAssociateMouseAndMouseCursorPosition(cls, follows):
        cls.associate_calls.append(follows)
        return 0

    @classmethod
    def CGWarpMouseCursorPosition(cls, point):
        cls.warp_calls.append(point)
        return 0

    @classmethod
    def CGEventTapEnable(cls, tap, enabled):
        cls.tap_enable_calls.append((tap, enabled))


class FakeGestureEvent:
    """Stands in for bridge.py's `_GestureEventView` (the object a converted
    NSEvent is wrapped in): exposes exactly the plain attribute surface
    GestureTranslator reads, with no AppKit involved."""

    def __init__(self, magnification=0.0, deltaX=0.0, deltaY=0.0, phase=0):
        self.magnification = magnification
        self.deltaX = deltaX
        self.deltaY = deltaY
        self.phase = phase


class FakeClock:
    def __init__(self, value=10.0):
        self.value = value

    def __call__(self):
        return self.value


class FakeSocket:
    def __init__(self):
        self.timeout = None
        self.sent = []
        self.closed = False
        self.shutdown_called = False
        self.inbound = bytearray()
        self.peer = None
        self.mac_preamble = None
        self.pending = []

    def settimeout(self, value):
        self.timeout = value

    def setsockopt(self, level, option, value):
        return None

    def sendall(self, payload):
        self.sent.append(payload)

    def recv(self, size):
        if self.peer is not None and self.peer.peer_prefix is None and self.sent:
            self.mac_preamble = self.sent[0]
            self.peer.accept_preamble(self.mac_preamble)
        if self.pending and self.mac_preamble is not None:
            for message in self.pending:
                self.inbound.extend(self.peer.seal(message))
            self.pending = []
        if not self.inbound:
            raise TimeoutError("no synthetic reply")
        chunk = bytes(self.inbound[:size])
        del self.inbound[:size]
        return chunk

    def shutdown(self, how):
        self.shutdown_called = True

    def close(self):
        self.closed = True


def make_config():
    return config.Config(
        host="192.0.2.10",
        port=51820,
        auth_token="synthetic-token",
        trigger_key="alt_r",
        double_tap_ms=300,
        key_map=dict(config.DEFAULT_KEY_MAP),
        reconnect_interval_s=0.1,
    )


def crossing_config(**overrides):
    cfg = make_config()
    cfg.crossing = dict(config.DEFAULT_CROSSING, **overrides)
    return cfg


def quiet_logger():
    logger = logging.getLogger("kvm-bridge-tests")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


FAKE_PNG = protocol.PNG_SIGNATURE + b"\x00" * 64


class FakeClipboard:
    """Drop-in replacement for clipboard_mac, injected via
    KVMController(clipboard=...) so tests never touch the real macOS
    pasteboard."""

    def __init__(self, text=None, image=None):
        self.text = text
        self.image = image
        self.set_calls = []

    def get_contents(self):
        return self.text, self.image

    def changed_contents(self):
        return self.text, self.image

    def forget_sync(self):
        pass

    def set_contents(self, text, image):
        self.set_calls.append((text, image))
        return True


class FrameDecoderTests(unittest.TestCase):
    def setUp(self):
        self.windows = protocol.SecureSession("synthetic-token")
        self.mac = protocol.SecureSession("synthetic-token")
        self.mac.accept_preamble(self.windows.preamble())
        self.windows.accept_preamble(self.mac.preamble())
        self.decoder = FrameDecoder(self.mac)

    def test_decodes_fragmented_frames(self):
        message = protocol.ack_msg(42)
        frame = self.windows.seal(message)
        self.assertEqual(self.decoder.feed(frame[:3]), [])
        self.assertEqual(self.decoder.feed(frame[3:8]), [])
        self.assertEqual(self.decoder.feed(frame[8:]), [message])

    def test_round_trip_through_the_framing(self):
        messages = [protocol.ack_msg(1), protocol.clipboard_msg("copied"), protocol.switch_msg("mac", "left", 0.25)]
        stream = b"".join(self.windows.seal(m) for m in messages)
        self.assertEqual(self.decoder.feed(stream), messages)
        self.assertNotIn(b"copied", stream)

    def test_largest_clipboard_fits_the_reply_stream(self):
        image = protocol.PNG_SIGNATURE + os.urandom(protocol.CLIPBOARD_IMAGE_MAX_BYTES - len(protocol.PNG_SIGNATURE))
        text = "x" * protocol.CLIPBOARD_MAX_BYTES
        message = protocol.clipboard_msg(text, image)
        self.assertEqual(self.decoder.feed(self.windows.seal(message)), [message])

    def test_wrong_token_fails_authentication(self):
        stranger = protocol.SecureSession("another-token", prefix=self.windows.prefix)
        stranger.accept_preamble(self.mac.preamble())
        with self.assertRaises(protocol.AuthenticationError):
            self.decoder.feed(stranger.seal(protocol.ack_msg(1)))

    def test_replayed_frame_is_rejected(self):
        frame = self.windows.seal(protocol.ack_msg(1))
        self.decoder.feed(frame)
        with self.assertRaises(protocol.ProtocolError) as caught:
            self.decoder.feed(frame)
        self.assertNotIsInstance(caught.exception, protocol.AuthenticationError)

    def test_counter_going_backwards_is_rejected(self):
        first = self.windows.seal(protocol.ack_msg(1))
        second = self.windows.seal(protocol.ack_msg(2))
        self.assertEqual(self.decoder.feed(first), [protocol.ack_msg(1)])
        self.assertEqual(self.decoder.feed(second), [protocol.ack_msg(2)])
        # Counter 1 arriving after counter 2 has been opened -- a rewound stream.
        with self.assertRaises(protocol.ProtocolError) as caught:
            self.decoder.feed(first)
        self.assertNotIsInstance(caught.exception, protocol.AuthenticationError)

    def test_rejects_oversized_frame(self):
        with self.assertRaises(protocol.ProtocolError):
            self.decoder.feed(struct.pack(">I", protocol.MAX_FRAME_BYTES + 1))


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
        self.controller = KVMController(
            make_config(),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=self.clock,
        )

    def test_local_event_passes_through(self):
        event = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x00,
            FakeQuartz.kCGKeyboardEventAutorepeat: 0,
            "unicode": "a",
        }
        returned = self.controller._event_tap_callback(
            None,
            FakeQuartz.kCGEventKeyDown,
            event,
            None,
        )
        self.assertIs(returned, event)
        self.assertTrue(self.controller.outbound.empty())

    def test_redirected_event_is_queued_and_swallowed(self):
        event = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x00,
            FakeQuartz.kCGKeyboardEventAutorepeat: 0,
            "unicode": "a",
        }
        self.controller.redirecting = True
        returned = self.controller._event_tap_callback(
            None,
            FakeQuartz.kCGEventKeyDown,
            event,
            None,
        )
        self.assertIsNone(returned)
        self.assertEqual(
            self.controller.outbound.get_nowait(),
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "a", "us": "a"}},
        )

    def test_queue_failure_restores_local_before_returning_event(self):
        event = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x00,
            FakeQuartz.kCGKeyboardEventAutorepeat: 0,
            "unicode": "a",
        }
        self.controller.outbound = queue.Queue(maxsize=1)
        self.controller.outbound.put_nowait({"full": True})
        self.controller.redirecting = True
        returned = self.controller._event_tap_callback(
            None,
            FakeQuartz.kCGEventKeyDown,
            event,
            None,
        )
        self.assertIs(returned, event)
        self.assertFalse(self.controller.redirecting)

    def test_double_tap_right_option_only_flips_redirect_flag(self):
        sock = FakeSocket()
        link(self.controller, sock)
        down = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x3D,
            "flags": FakeQuartz.kCGEventFlagMaskAlternate,
        }
        up = {FakeQuartz.kCGKeyboardEventKeycode: 0x3D, "flags": 0}
        first_down = self.controller._event_tap_callback(
            None, FakeQuartz.kCGEventFlagsChanged, down, None
        )
        first_up = self.controller._event_tap_callback(
            None, FakeQuartz.kCGEventFlagsChanged, up, None
        )
        self.clock.value = 10.2
        second_down = self.controller._event_tap_callback(
            None, FakeQuartz.kCGEventFlagsChanged, down, None
        )
        second_up = self.controller._event_tap_callback(
            None, FakeQuartz.kCGEventFlagsChanged, up, None
        )
        self.assertIs(first_down, down)
        self.assertIs(first_up, up)
        self.assertIsNone(second_down)
        self.assertIsNone(second_up)
        self.assertTrue(self.controller.redirecting)
        self.assertIs(self.controller.sock, sock)

    def test_sequence_is_added_once_per_outbound_event(self):
        first = self.controller._prepare_outbound(
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "a"}}
        )
        second = self.controller._prepare_outbound(
            {"type": protocol.MSG_KEYUP, "data": {"key": "a"}}
        )
        self.assertEqual(first["data"]["seq"], 1)
        self.assertEqual(second["data"]["seq"], 2)

    def test_watchdog_drops_connection_after_two_seconds_without_ack(self):
        alerts = []
        self.controller.on_user_alert = lambda title, message: alerts.append((title, message))
        sock = FakeSocket()
        link(self.controller, sock)
        self.controller.redirecting = True
        self.controller.connected_at = 10.0
        self.controller.last_ack_at = 10.0
        self.clock.value = 10.0 + ACK_TIMEOUT_SECONDS + 0.01
        self.assertTrue(self.controller._watchdog_tick())
        self.assertFalse(self.controller.redirecting)
        self.assertTrue(sock.closed)
        self.assertEqual(self.controller.connection_status, "Windows stopped responding")
        # Killing an active redirect must alert the user immediately, since
        # otherwise input would simply vanish into a dead connection.
        self.assertEqual(alerts, [("Beamer", "Input returned to this Mac")])

    def test_watchdog_detects_a_dead_socket_while_not_redirecting(self):
        # Previously the watchdog only ran while redirecting, so a half-open
        # connection was invisible while idle: the UI stayed green and a
        # double-tap would "succeed" into a void. It must now be detected and
        # torn down the same way regardless of redirect state.
        sock = FakeSocket()
        link(self.controller, sock)
        self.controller.redirecting = False
        self.controller.connected_at = 10.0
        self.controller.last_ack_at = 10.0
        self.clock.value = 10.0 + ACK_TIMEOUT_SECONDS + 0.01
        self.assertTrue(self.controller._watchdog_tick())
        self.assertFalse(self.controller.redirecting)
        self.assertIsNone(self.controller.sock)
        self.assertTrue(sock.closed)
        self.assertEqual(self.controller.connection_status, "Windows stopped responding")

    def test_watchdog_sends_idle_ping_after_a_second_of_silence(self):
        sock = FakeSocket()
        link(self.controller, sock)
        self.controller.connected_at = 10.0
        self.controller.last_ack_at = 10.0
        self.controller.last_send_at = 10.0
        self.clock.value = 10.0 + PING_INTERVAL_SECONDS
        self.assertFalse(self.controller._watchdog_tick())
        self.assertEqual(len(sock.sent), 1)
        self.assertEqual(sent_messages(self.controller, sock)[-1], protocol.ping_msg())
        self.assertEqual(self.controller.last_send_at, self.clock.value)

    def test_watchdog_suppresses_idle_ping_while_traffic_flows(self):
        sock = FakeSocket()
        link(self.controller, sock)
        self.controller.connected_at = 10.0
        self.controller.last_ack_at = 10.0
        self.controller.last_send_at = 11.5
        self.clock.value = 11.6
        self.assertFalse(self.controller._watchdog_tick())
        self.assertEqual(sock.sent, [])

    def test_set_redirecting_true_refuses_while_receiving(self):
        # Invariant 2: redirecting and receiving must never both be True.
        # set_receiving enforces this by turning redirecting off first
        # (bridge.py:709-710); set_redirecting has no matching check, so a
        # manual switch (the menu bar's toggleRedirect_, kvm_bridge_app.py:1703)
        # while Windows is already driving this Mac corrupts the pair. This
        # asserts the invariant, which the current code violates.
        sock = FakeSocket()
        link(self.controller, sock)
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
        link(self.controller)
        self.controller.receiving = True
        self.controller.send_peer_home = lambda: sent_home.append(True) or True
        self.assertTrue(self.controller.set_redirecting(True))
        self.assertEqual(sent_home, [True])
        self.assertFalse(self.controller.redirecting)

    def test_switching_while_receiving_with_the_pc_unreachable_is_refused(self):
        link(self.controller)
        self.controller.receiving = True
        self.controller.send_peer_home = lambda: False
        self.assertFalse(self.controller.set_redirecting(True))
        self.assertFalse(self.controller.redirecting)

    def test_this_macs_trigger_sends_the_pcs_input_home_while_receiving(self):
        sent_home = []
        link(self.controller)
        self.controller.receiving = True
        self.controller.send_peer_home = lambda: sent_home.append(True) or True
        down = {FakeQuartz.kCGKeyboardEventKeycode: 0x3D, "flags": FakeQuartz.kCGEventFlagMaskAlternate}
        up = {FakeQuartz.kCGKeyboardEventKeycode: 0x3D, "flags": 0}
        tap = self.controller._event_tap_callback
        tap(None, FakeQuartz.kCGEventFlagsChanged, down, None)
        tap(None, FakeQuartz.kCGEventFlagsChanged, up, None)
        self.clock.value = 10.2
        self.assertIsNone(tap(None, FakeQuartz.kCGEventFlagsChanged, down, None))
        self.assertIsNone(tap(None, FakeQuartz.kCGEventFlagsChanged, up, None))
        self.assertEqual(sent_home, [True])
        self.assertFalse(self.controller.redirecting)

    def test_the_pcs_injected_trigger_is_not_this_macs_while_receiving(self):
        sent_home = []
        link(self.controller)
        self.controller.receiving = True
        self.controller.send_peer_home = lambda: sent_home.append(True) or True
        down = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x3D,
            "flags": FakeQuartz.kCGEventFlagMaskAlternate,
            FakeQuartz.kCGEventSourceUserData: INJECTED_MARK,
        }
        tap = self.controller._event_tap_callback
        for _ in range(2):
            self.assertIs(tap(None, FakeQuartz.kCGEventFlagsChanged, down, None), down)
        self.assertEqual(sent_home, [])

    def test_sending_to_windows_switched_off_refuses_the_switch(self):
        link(self.controller)
        self.controller.cfg.send_to_windows = False
        self.assertFalse(self.controller.set_redirecting(True))
        self.assertFalse(self.controller.redirecting)

    def test_sending_to_windows_switched_off_still_sends_the_pcs_input_home(self):
        # Each switch covers one direction: the PC's input on this Mac still goes home.
        sent_home = []
        link(self.controller)
        self.controller.cfg.send_to_windows = False
        self.controller.receiving = True
        self.controller.send_peer_home = lambda: sent_home.append(True) or True
        self.assertTrue(self.controller.set_redirecting(True))
        self.assertEqual(sent_home, [True])

    def test_alert_fired_when_double_tap_cannot_switch_because_disconnected(self):
        alerts = []
        self.controller.on_user_alert = lambda title, message: alerts.append((title, message))
        self.controller.connection_status = "Waiting to connect"
        self.assertFalse(self.controller.set_redirecting(True))
        self.assertEqual(
            alerts,
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
        sock = FakeSocket()
        link(self.controller, sock)
        down = {
            FakeQuartz.kCGKeyboardEventKeycode: 0x3D,
            "flags": FakeQuartz.kCGEventFlagMaskAlternate | 0x0040,
        }
        # Simulate the effect of the missed key-up: stale bookkeeping left
        # over as if the trigger key were still held down.
        self.controller.translator.modifier_down.add(0x3D)
        first_down = self.controller._event_tap_callback(
            None, FakeQuartz.kCGEventFlagsChanged, down, None
        )
        self.clock.value = 10.2
        second_down = self.controller._event_tap_callback(
            None, FakeQuartz.kCGEventFlagsChanged, down, None
        )
        self.assertIs(first_down, down)
        self.assertIsNone(second_down)
        self.assertTrue(self.controller.redirecting)

    def test_handle_inbound_unknown_type_refreshes_liveness_and_is_ignored(self):
        self.controller.last_ack_at = 0.0
        self.clock.value = 42.0
        self.controller._handle_inbound({"type": protocol.MSG_FOCUS, "data": {"target": "windows"}})
        self.assertEqual(self.controller.last_ack_at, 42.0)
        # Bookkeeping used to validate real ACKs must be untouched.
        self.assertEqual(self.controller.last_ack_seq, -1)

    def test_handle_inbound_malformed_message_drops_connection(self):
        with self.assertRaises(protocol.ProtocolError):
            self.controller._handle_inbound({"data": {}})

    def test_worker_exception_forces_local(self):
        self.controller.redirecting = True

        def fail():
            raise RuntimeError("synthetic failure")

        self.controller._thread_entry("synthetic", fail)
        self.assertFalse(self.controller.redirecting)

    def test_outbound_worker_survives_a_bad_message(self):
        # The only thread that drains the queue is never restarted, so a raise
        # inside one message (the clipboard read every switch does) must force
        # input local and leave the worker running for the next message.
        self.controller.redirecting = True
        self.controller.clipboard = types.SimpleNamespace(
            changed_contents=lambda: (_ for _ in ()).throw(RuntimeError("synthetic clipboard failure"))
        )
        self.controller.outbound.put_nowait({"type": "_local_clipboard", "data": {}})
        self.controller.stop_event.clear()

        def stop_after_one():
            self.controller.outbound.join()
            self.controller.stop_event.set()

        threading.Thread(target=stop_after_one, daemon=True).start()
        self.controller._outbound_worker()
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(self.controller.outbound.qsize(), 0)

    def test_an_ack_without_an_object_body_drops_the_connection_not_the_reader(self):
        sock = FakeSocket()
        link(self.controller, sock)
        with self.assertRaises(protocol.ProtocolError):
            self.controller._handle_inbound({"type": protocol.MSG_ACK, "data": None})

    def test_every_way_home_tells_windows_while_the_link_is_up(self):
        # Windows keeps treating this Mac as driving, with whatever keys it was
        # holding still down, until it hears otherwise.
        for way_home in (
            lambda: self.controller._force_local("synthetic"),
            lambda: self.controller._event_tap_callback(None, FakeQuartz.kCGEventTapDisabledByTimeout, {}, None),
        ):
            with self.subTest(way_home=way_home):
                link(self.controller)
                self.controller.event_tap = object()
                self.controller.set_redirecting(True)
                self.controller._drain_outbound()
                way_home()
                queued = []
                while not self.controller.outbound.empty():
                    queued.append(self.controller.outbound.get_nowait())
                self.assertIn(protocol.focus_msg("mac"), queued)

    def test_every_way_home_reassociates_the_cursor(self):
        # A path that only cleared `redirecting` left the pointer decoupled
        # from the hand: keyboard back, pointer frozen.
        for way_home in (
            lambda: self.controller._connection_failed("synthetic drop"),
            lambda: self.controller._force_local("synthetic"),
            lambda: self.controller.update_config(crossing_config()),
            lambda: self.controller._thread_entry("synthetic", lambda: (_ for _ in ()).throw(RuntimeError())),
        ):
            with self.subTest(way_home=way_home):
                link(self.controller)
                self.controller.set_redirecting(True)
                FakeQuartz.reset_cursor_spies()
                way_home()
                self.assertFalse(self.controller.redirecting)
                self.assertIn(True, FakeQuartz.associate_calls)

    def test_connect_and_hello_use_explicit_timeouts(self):
        sock = FakeSocket()
        seed_receiver_reply(sock, protocol.welcome_msg())
        calls = []

        def socket_factory(address, timeout):
            calls.append((address, timeout))
            return sock

        controller = KVMController(
            make_config(),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=self.clock,
            socket_factory=socket_factory,
        )
        self.assertTrue(controller._connect_once())
        self.assertEqual(calls, [(('192.0.2.10', 51820), CONNECT_TIMEOUT_SECONDS)])
        self.assertEqual(sock.timeout, SOCKET_IO_TIMEOUT_SECONDS)
        self.assertEqual(sock.sent[0], controller.session.preamble())
        # The hello carries the way home, which is how Windows knows which of
        # its own edges leads back here before this Mac has ever crossed.
        self.assertEqual(
            sent_messages(controller, sock),
            [protocol.hello_msg(return_edge="left", resistance_px=120)],
        )
        self.assertNotIn(b"synthetic-token", b"".join(sock.sent))
        self.assertEqual(controller.connected_at, self.clock.value)
        self.assertEqual(controller.last_send_at, self.clock.value)

    def test_silent_receiver_after_connect_reports_old_receiver(self):
        # A pre-v4 receiver reads our preamble as a bogus frame length and waits
        # for a hello that never comes; it says nothing, and never will.
        sock = FakeSocket()
        controller = KVMController(
            make_config(),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=self.clock,
            socket_factory=lambda address, timeout: sock,
        )
        self.assertFalse(controller._connect_once())
        self.assertEqual(controller.connection_status, OLD_RECEIVER_STATUS)
        self.assertTrue(sock.closed)
        self.assertIsNone(controller.sock)

    def test_a_pc_that_answered_before_and_falls_silent_is_not_called_old(self):
        # Seen in practice: Windows stalled every app for 40s while it reconfigured its displays,
        # and the Mac wrongly prompted to update a Beamer that was already current.
        answering, silent = FakeSocket(), FakeSocket()
        seed_receiver_reply(answering, protocol.welcome_msg())
        sockets = iter([answering, silent])
        controller = KVMController(
            make_config(),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=self.clock,
            socket_factory=lambda address, timeout: next(sockets),
        )
        self.assertTrue(controller._connect_once())
        self.assertFalse(controller._connect_once())
        self.assertEqual(controller.connection_status, "Windows stopped responding")
        self.assertTrue(silent.closed)

    def test_a_re_pair_forgets_that_the_old_pc_answered(self):
        # A new token can be a different PC at the same address, and a silent one may be old.
        answering, silent = FakeSocket(), FakeSocket()
        seed_receiver_reply(answering, protocol.welcome_msg())
        sockets = iter([answering, silent])
        controller = KVMController(
            make_config(),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=self.clock,
            socket_factory=lambda address, timeout: next(sockets),
        )
        self.assertTrue(controller._connect_once())
        controller.update_config(dataclasses.replace(make_config(), auth_token="re-paired-token"))
        self.assertFalse(controller._connect_once())
        self.assertEqual(controller.connection_status, OLD_RECEIVER_STATUS)

    def test_other_wire_version_in_the_preamble_is_reported(self):
        sock = FakeSocket()
        sock.inbound.extend(protocol.WIRE_MAGIC + bytes([protocol.PROTOCOL_VERSION + 1]) + b"\x02" * protocol.NONCE_PREFIX_SIZE)
        controller = KVMController(
            make_config(),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=self.clock,
            socket_factory=lambda address, timeout: sock,
        )
        self.assertFalse(controller._connect_once())
        self.assertIn(f"v{protocol.PROTOCOL_VERSION + 1}", controller.connection_status)
        self.assertIn("update both apps", controller.connection_status)
        self.assertTrue(sock.closed)

    def test_wrong_token_reports_authentication_failure(self):
        sock = FakeSocket()
        seed_receiver_reply(sock, protocol.welcome_msg(), token="a-different-token")
        controller = KVMController(
            make_config(),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=self.clock,
            socket_factory=lambda address, timeout: sock,
        )
        self.assertFalse(controller._connect_once())
        self.assertEqual(controller.connection_status, AUTH_FAILED_STATUS)
        self.assertTrue(sock.closed)
        self.assertIsNone(controller.sock)

    def test_every_connection_draws_a_fresh_nonce_prefix(self):
        prefixes = []
        for _ in range(2):
            sock = FakeSocket()
            seed_receiver_reply(sock, protocol.welcome_msg())
            controller = KVMController(
                make_config(),
                logger=quiet_logger(),
                quartz=FakeQuartz,
                clock=self.clock,
                socket_factory=lambda address, timeout, sock=sock: sock,
            )
            initial = controller.session.prefix
            self.assertTrue(controller._connect_once())
            # _connect_once builds and installs a brand-new session, so the
            # prefix it connects with differs from the one it was born holding.
            self.assertNotEqual(controller.session.prefix, initial)
            prefixes.append(controller.session.prefix)
        self.assertNotEqual(prefixes[0], prefixes[1])

    def test_handshake_welcome_version_mismatch_reports_old_receiver(self):
        sock = FakeSocket()
        seed_receiver_reply(sock, protocol.welcome_msg(error="version_mismatch"))

        controller = KVMController(
            make_config(),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=self.clock,
            socket_factory=lambda address, timeout: sock,
        )
        self.assertFalse(controller._connect_once())
        self.assertEqual(controller.connection_status, OLD_RECEIVER_STATUS)

    def test_handshake_welcome_with_other_error_is_surfaced(self):
        sock = FakeSocket()
        seed_receiver_reply(sock, protocol.welcome_msg(error="rate_limited"))

        controller = KVMController(
            make_config(),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=self.clock,
            socket_factory=lambda address, timeout: sock,
        )
        self.assertFalse(controller._connect_once())
        self.assertIn("rate_limited", controller.connection_status)

    def test_no_route_uses_existing_ssh_fallback_and_authenticates_over_localhost(self):
        sock = FakeSocket()
        seed_receiver_reply(sock, protocol.welcome_msg())
        socket_calls = []

        def socket_factory(address, timeout):
            socket_calls.append((address, timeout))
            if len(socket_calls) == 1:
                raise OSError(errno.EHOSTUNREACH, "No route to host")
            return sock

        controller = KVMController(
            make_config(),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=self.clock,
            socket_factory=socket_factory,
        )
        self.assertTrue(controller._connect_once())
        self.assertEqual(socket_calls[1][0], ("127.0.0.1", 24822))
        self.assertEqual(
            controller.connection_status,
            "Connected to Windows via secure macOS 27 fallback",
        )

    def test_set_redirecting_off_resets_mouse_accumulators(self):
        sock = FakeSocket()
        link(self.controller, sock)
        self.controller.redirecting = True
        self.controller.translator._move_accum_x = 0.4
        self.controller.translator._move_accum_y = -0.4
        self.controller.set_redirecting(False)
        self.assertEqual(self.controller.translator._move_accum_x, 0.0)
        self.assertEqual(self.controller.translator._move_accum_y, 0.0)

    def test_no_route_error_explains_the_network_failure(self):
        error = OSError(65, "No route to host")
        self.assertEqual(
            connection_error_message(error),
            "Windows is unreachable from this Mac. Check that both devices can communicate on the local network.",
        )


class ClipboardSyncTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.controller = KVMController(
            make_config(),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=self.clock,
        )
        self.sock = FakeSocket()
        link(self.controller, self.sock)

    def _drain_outbound(self):
        while not self.controller.outbound.empty():
            self.controller._process_outbound(self.controller.outbound.get_nowait())

    def test_control_message_bypasses_the_gate_while_not_redirecting(self):
        self.controller.redirecting = False
        self.controller._process_outbound(protocol.focus_msg("mac"))
        self.assertEqual(sent_messages(self.controller, self.sock)[-1], protocol.focus_msg("mac"))

    def test_non_control_message_is_dropped_while_not_redirecting(self):
        self.controller.redirecting = False
        self.controller._process_outbound({"type": protocol.MSG_KEYDOWN, "data": {"key": "a"}})
        self.assertEqual(self.sock.sent, [])

    def test_control_messages_do_not_consume_a_sequence_number(self):
        self.controller.redirecting = True
        self.controller._process_outbound(protocol.focus_msg("windows", return_edge="left", resistance_px=120))
        self.assertEqual(self.controller.last_sent_seq, 0)
        self.assertNotIn("seq", sent_messages(self.controller, self.sock)[-1]["data"])

    def test_switch_to_windows_sends_clipboard_then_focus_in_order(self):
        self.controller.clipboard = FakeClipboard("hello from the Mac")
        self.assertTrue(self.controller.set_redirecting(True))
        self._drain_outbound()
        sent = sent_messages(self.controller, self.sock)
        self.assertEqual(
            sent,
            [protocol.clipboard_msg("hello from the Mac"), protocol.focus_msg("windows", return_edge="left", resistance_px=120)],
        )

    def test_switch_to_windows_with_empty_clipboard_sends_focus_only(self):
        self.controller.clipboard = FakeClipboard("")
        self.controller.set_redirecting(True)
        self._drain_outbound()
        sent = sent_messages(self.controller, self.sock)
        self.assertEqual(sent, [protocol.focus_msg("windows", return_edge="left", resistance_px=120)])

    def test_switch_to_windows_skips_oversized_clipboard_but_still_sends_focus(self):
        self.controller.clipboard = FakeClipboard("z" * (protocol.CLIPBOARD_MAX_BYTES + 1))
        self.controller.set_redirecting(True)
        self._drain_outbound()
        sent = sent_messages(self.controller, self.sock)
        self.assertEqual(sent, [protocol.focus_msg("windows", return_edge="left", resistance_px=120)])

    def test_switch_back_to_mac_queues_a_focus_message(self):
        self.controller.redirecting = True
        self.controller.set_redirecting(False)
        self.assertEqual(self.controller.outbound.get_nowait(), protocol.focus_msg("mac"))

    def test_switch_back_focus_message_survives_the_closed_gate(self):
        # set_redirecting(False) flips redirecting False before the worker
        # ever dequeues the focus message it just queued -- the gate must
        # still let this specific message through.
        self.controller.redirecting = True
        self.controller.set_redirecting(False)
        self.assertFalse(self.controller.redirecting)
        message = self.controller.outbound.get_nowait()
        self.controller._process_outbound(message)
        self.assertEqual(sent_messages(self.controller, self.sock)[-1], protocol.focus_msg("mac"))

    def test_handle_inbound_clipboard_sets_the_local_pasteboard(self):
        fake = FakeClipboard()
        self.controller.clipboard = fake
        self.controller._handle_inbound(protocol.clipboard_msg("copied on Windows"))
        self.assertEqual(fake.set_calls, [("copied on Windows", None)])

    def test_handle_inbound_clipboard_ignores_oversized_payload(self):
        fake = FakeClipboard()
        self.controller.clipboard = fake
        oversized = "y" * (protocol.CLIPBOARD_MAX_BYTES + 1)
        self.controller._handle_inbound(protocol.clipboard_msg(oversized))
        self.assertEqual(fake.set_calls, [])

    def test_handle_inbound_clipboard_refreshes_liveness(self):
        fake = FakeClipboard()
        self.controller.clipboard = fake
        self.controller.last_ack_at = 0.0
        self.clock.value = 42.0
        self.controller._handle_inbound(protocol.clipboard_msg("hi"))
        self.assertEqual(self.controller.last_ack_at, 42.0)

    def test_switch_to_windows_sends_text_and_image_together(self):
        self.controller.clipboard = FakeClipboard("shot.png", FAKE_PNG)
        self.controller.set_redirecting(True)
        self._drain_outbound()
        sent = sent_messages(self.controller, self.sock)
        self.assertEqual(sent[0], protocol.clipboard_msg("shot.png", FAKE_PNG))
        self.assertEqual(sent[1]["type"], protocol.MSG_FOCUS)

    def test_switch_to_windows_with_image_only_sends_no_text_key(self):
        self.controller.clipboard = FakeClipboard(None, FAKE_PNG)
        self.controller.set_redirecting(True)
        self._drain_outbound()
        self.assertEqual(sent_messages(self.controller, self.sock)[0], protocol.clipboard_msg(None, FAKE_PNG))

    def test_switch_to_windows_drops_an_oversized_image_but_sends_the_text(self):
        big = protocol.PNG_SIGNATURE + b"\x00" * protocol.CLIPBOARD_IMAGE_MAX_BYTES
        self.controller.clipboard = FakeClipboard("shot.png", big)
        self.controller.set_redirecting(True)
        self._drain_outbound()
        self.assertEqual(sent_messages(self.controller, self.sock)[0], protocol.clipboard_msg("shot.png"))

    def test_handle_inbound_clipboard_sets_text_and_image(self):
        fake = FakeClipboard()
        self.controller.clipboard = fake
        self.controller._handle_inbound(protocol.clipboard_msg("shot.png", FAKE_PNG))
        self.assertEqual(fake.set_calls, [("shot.png", FAKE_PNG)])

    def test_handle_inbound_clipboard_ignores_a_malformed_image_but_keeps_the_text(self):
        fake = FakeClipboard()
        self.controller.clipboard = fake
        for image in ("not base64!", 42, "", protocol.clipboard_msg(None, b"GIF89a")["data"]["image"]):
            message = {"type": protocol.MSG_CLIPBOARD, "data": {"text": "t", "image": image, "image_format": "png"}}
            self.controller._handle_inbound(message)
        self.controller._handle_inbound({"type": protocol.MSG_CLIPBOARD, "data": {"image": "AAAA", "image_format": "bmp"}})
        self.assertEqual(fake.set_calls, [("t", None)] * 4)


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
        self.controller = KVMController(
            make_config(),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=self.clock,
        )
        self.sock = FakeSocket()
        link(self.controller, self.sock)

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
        self.assertEqual(FakeQuartz.associate_calls, [False, True])

    def test_association_status_logged_once_not_every_toggle(self):
        logger = logging.getLogger("kvm-bridge-tests.cursor-assoc")
        logger.setLevel(logging.INFO)
        controller = KVMController(
            make_config(), logger=logger, quartz=FakeQuartz, clock=self.clock,
        )
        link(controller)
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
        self.assertEqual(FakeQuartz.associate_calls, [True])

    def test_warp_error_logged_once_not_spammed(self):
        logger = logging.getLogger("kvm-bridge-tests.warp-error")
        logger.setLevel(logging.WARNING)

        class ErroringQuartz(FakeQuartz):
            @classmethod
            def CGWarpMouseCursorPosition(cls, point):
                cls.warp_calls.append(point)
                return 1  # non-zero: a synthetic CGError

        ErroringQuartz.reset_cursor_spies()
        controller = KVMController(
            make_config(), logger=logger, quartz=ErroringQuartz, clock=self.clock,
        )
        link(controller)
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
        self.controller = KVMController(
            make_config(),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=self.clock,
        )
        self.sock = FakeSocket()
        link(self.controller, self.sock)

    def test_overlay_gesture_translated_and_enqueued_while_redirecting(self):
        self.controller.redirecting = True
        self.controller.handle_overlay_gesture(MAGNIFY_TYPE, FakeGestureEvent(magnification=0.05))
        drained = []
        while not self.controller.outbound.empty():
            drained.append(self.controller.outbound.get_nowait())
        self.assertEqual(drained, [
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "ctrl"}},
            protocol.scroll_msg(dy=1, dx=0, mode="line"),
            {"type": protocol.MSG_KEYUP, "data": {"key": "ctrl"}},
        ])
        # Fed back through the normal outbound path, these get a `seq` the
        # same way any other input event does.
        for message in drained:
            self.controller._process_outbound(message)
        sent = sent_messages(self.controller, self.sock)
        self.assertEqual([entry["data"]["seq"] for entry in sent], [1, 2, 3])

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
        controller = KVMController(
            make_config(),
            logger=logger,
            quartz=FakeQuartz,
            clock=self.clock,
        )
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
        self.assertEqual(drained, [
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "alt"}},
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "left"}},
            {"type": protocol.MSG_KEYUP, "data": {"key": "left"}},
            {"type": protocol.MSG_KEYUP, "data": {"key": "alt"}},
        ])


class MediaKeyWiringTests(unittest.TestCase):
    """Exercises _event_tap_callback's NSSystemDefined branch with an
    injected system_event_converter, standing in for the real AppKit
    conversion, exactly as GestureWiringTests does for gestures."""

    SYSDEFINED = media_keys.NX_SYSDEFINED_EVENT_TYPE

    def _controller(self, converter):
        return KVMController(
            make_config(),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=FakeClock(),
            system_event_converter=converter,
        )

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
        return KVMController(
            make_config(),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=self.clock,
            gesture_event_converter=converter,
        )

    def test_gesture_while_redirecting_is_translated_enqueued_and_swallowed(self):
        controller = self._controller(lambda event: FakeGestureEvent(deltaX=1.0))
        controller.redirecting = True
        sock = FakeSocket()
        link(controller, sock)
        returned = controller._event_tap_callback(None, SWIPE_TYPE, object(), None)
        self.assertIsNone(returned)
        drained = []
        while not controller.outbound.empty():
            drained.append(controller.outbound.get_nowait())
        self.assertEqual(drained, [
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "alt"}},
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "left"}},
            {"type": protocol.MSG_KEYUP, "data": {"key": "left"}},
            {"type": protocol.MSG_KEYUP, "data": {"key": "alt"}},
        ])
        # Fed back through the normal outbound path, these get a `seq` the
        # same way any other input event does.
        for message in drained:
            controller._process_outbound(message)
        sent = sent_messages(controller, sock)
        self.assertEqual([entry["data"]["seq"] for entry in sent], [1, 2, 3, 4])

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
        self.assertEqual(drained, [
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "alt"}},
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "left"}},
            {"type": protocol.MSG_KEYUP, "data": {"key": "left"}},
            {"type": protocol.MSG_KEYUP, "data": {"key": "alt"}},
        ])

    def test_gesture_capture_active_logged_once_on_first_success(self):
        logger = logging.getLogger("kvm-bridge-tests.gesture-capture")
        logger.setLevel(logging.INFO)
        controller = KVMController(
            make_config(),
            logger=logger,
            quartz=FakeQuartz,
            clock=self.clock,
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




class WindowsLockStatusTests(unittest.TestCase):
    """Windows sets `locked` on its ACKs while it clears the lock screen, so
    the Mac can explain the pause instead of just looking dead."""

    def setUp(self):
        self.controller = KVMController(
            make_config(),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=FakeClock(),
        )
        self.controller.connection_status = "Connected to 192.168.1.3:51820"

    def test_locked_ack_shows_the_unlock_and_clearing_it_restores_the_status(self):
        self.controller._note_windows_lock(True)
        self.assertTrue(self.controller.windows_locked)
        self.assertEqual(self.controller.connection_status, "Unlocking Windows…")
        self.controller._note_windows_lock(False)
        self.assertFalse(self.controller.windows_locked)
        self.assertEqual(self.controller.connection_status, "Connected to 192.168.1.3:51820")

    def test_the_ssh_fallback_wording_survives_an_unlock(self):
        """The status is restored verbatim, not rebuilt, so the macOS 27
        fallback does not silently become an ordinary connection."""
        self.controller.connection_status = "Connected to Windows via secure macOS 27 fallback"
        self.controller._note_windows_lock(True)
        self.controller._note_windows_lock(False)
        self.assertEqual(
            self.controller.connection_status,
            "Connected to Windows via secure macOS 27 fallback",
        )

    def test_repeated_locked_acks_do_not_overwrite_the_saved_status(self):
        """An ACK lands every 400ms; only transitions may touch the status, or
        the saved one becomes 'Unlocking Windows…' and is lost."""
        self.controller._note_windows_lock(True)
        self.controller._note_windows_lock(True)
        self.controller._note_windows_lock(False)
        self.assertEqual(self.controller.connection_status, "Connected to 192.168.1.3:51820")

    def test_an_ack_without_the_field_is_not_a_lock(self):
        self.controller._record_ack(protocol.ack_msg(0))
        self.assertFalse(self.controller.windows_locked)


class DockControlWiringTests(unittest.TestCase):
    """The tap's type-30 event is a system swipe (DockControl), read through
    an injected dock_event_reader standing in for the private CGEvent
    fields. While redirecting it is always swallowed, so Mission Control
    and Spaces stay shut on the Mac; classified or not."""

    def _controller(self, reader):
        return KVMController(
            make_config(),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=FakeClock(),
            dock_event_reader=reader,
            macos_major=26,
        )

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
        self.controller = self._controller(crossing_config())

    def _controller(self, cfg):
        controller = KVMController(
            cfg,
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=self.clock,
            desktop_bounds=lambda: self.BOUNDS,
        )
        link(controller)
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

    def _sent(self):
        messages = []
        while not self.controller.outbound.empty():
            messages.append(self.controller.outbound.get_nowait())
        return messages

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
        focus = [m for m in self._sent() if m["type"] == protocol.MSG_FOCUS]
        self.assertEqual(
            focus,
            [protocol.focus_msg("windows", edge="left", offset=0.5, return_edge="left", resistance_px=120)],
        )
        kinds = [kind for kind, _ in self.feedback]
        self.assertEqual(kinds[-1], "cross")
        self.assertEqual(kinds.count("tick"), 3)

    def test_this_macs_own_push_while_windows_drives_sends_the_pc_home_and_crosses(self):
        # Regression: this Mac's trackpad could not push back while the PC drove it.
        sent_home = []
        self.controller.receiving = True
        self.controller.send_peer_home = lambda: sent_home.append(True) or True
        _event, returned = self._push(10)
        self.assertIsNone(returned)
        self.assertEqual(sent_home, [True])
        self.assertFalse(self.controller.receiving)
        self.assertTrue(self.controller.redirecting)
        focus = [m for m in self._sent() if m["type"] == protocol.MSG_FOCUS]
        self.assertEqual(focus[0]["data"]["target"], "windows")

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
        self.controller.sock = None
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
        self.controller = self._controller(crossing_config(methods=["shortcut"]))
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
        self.controller.update_config(crossing_config())
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
        sock = self.controller.sock
        self.controller.apply_settings(crossing_config(resistance_px=40, methods=["shortcut", "notch"]))
        self.assertIs(self.controller.sock, sock)
        self.assertEqual(self.controller.crossing.resistance_px, 40)
        self.assertEqual(self.controller.crossing.home_edge(), "bottom")

    def test_shortcut_switch_sends_the_way_home_but_no_arrival(self):
        self.controller.set_redirecting(True)
        focus = [m for m in self._sent() if m["type"] == protocol.MSG_FOCUS]
        self.assertEqual(focus, [protocol.focus_msg("windows", return_edge="left", resistance_px=120)])

    def test_shortcut_switch_with_only_the_notch_on_sends_the_notch_way_home(self):
        self.controller = self._controller(crossing_config(methods=["shortcut", "notch"]))
        self.controller.set_redirecting(True)
        focus = [m for m in self._sent() if m["type"] == protocol.MSG_FOCUS]
        self.assertEqual(focus, [protocol.focus_msg("windows", return_edge="bottom", resistance_px=120)])

    def test_inbound_switch_returns_input_and_lands_the_pointer(self):
        self.controller.set_redirecting(True)
        self._sent()
        FakeQuartz.reset_cursor_spies()
        self.controller._handle_inbound(protocol.switch_msg("mac", "right", 0.5))
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(FakeQuartz.warp_calls, [(1725.0, 558.0)])
        self.assertEqual([m["type"] for m in self._sent()], [protocol.MSG_FOCUS])
        self.assertEqual(self.feedback[-1][0], "arrive")
        # Where it landed, for the arrival effect.
        self.assertEqual((self.feedback[-1][1].mac_edge, self.feedback[-1][1].pin), ("right", (1725.0, 558.0)))

    def test_inbound_switch_without_a_position_still_returns_input(self):
        self.controller.set_redirecting(True)
        FakeQuartz.reset_cursor_spies()
        self.controller._handle_inbound(protocol.switch_msg("mac"))
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
        self.controller._handle_inbound(protocol.switch_msg("mac", "right", 0.5))
        self.assertEqual(self._kinds(), ["arrive"])

    def test_the_pc_sending_input_home_by_its_switch_reports_it_once(self):
        self.controller.set_redirecting(True)
        self.feedback.clear()
        self.controller._handle_inbound(protocol.switch_msg("mac"))
        self.assertEqual(self._kinds().count("home"), 1)

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
        self.controller._handle_inbound(protocol.switch_msg("toaster", "right", 0.5))
        self.assertTrue(self.controller.redirecting)

    def test_hold_style_redirects_only_while_the_key_is_down(self):
        cfg = crossing_config()
        cfg.trigger_style = "hold"
        self.controller = self._controller(cfg)
        down = {FakeQuartz.kCGKeyboardEventKeycode: 0x3D, "flags": FakeQuartz.kCGEventFlagMaskAlternate}
        up = {FakeQuartz.kCGKeyboardEventKeycode: 0x3D, "flags": 0}
        self.assertIsNone(self.controller._event_tap_callback(None, FakeQuartz.kCGEventFlagsChanged, down, None))
        self.assertTrue(self.controller.redirecting)
        self.assertIsNone(self.controller._event_tap_callback(None, FakeQuartz.kCGEventFlagsChanged, up, None))
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(
            [m["type"] for m in self._sent()],
            ["_local_clipboard", protocol.MSG_FOCUS, protocol.MSG_FOCUS],
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

        controller = KVMController(crossing_config(), logger=quiet_logger(), quartz=DisplayQuartz, clock=self.clock)
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

        controller = KVMController(crossing_config(), logger=quiet_logger(), quartz=SleepingQuartz, clock=self.clock)
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


class RoundTripTests(unittest.TestCase):
    """The smoothed round trip shown in the control window: measured from the send of the
    event an ACK names, upper-median smoothed, and blank whenever it would be stale."""

    def setUp(self):
        self.clock = FakeClock(10.0)
        self.controller = KVMController(
            crossing_config(), logger=quiet_logger(), quartz=FakeQuartz, clock=self.clock
        )
        link(self.controller)
        self.controller.redirecting = True

    def _send(self):
        self.controller._process_outbound({"type": protocol.MSG_MOUSEMOVE, "data": {"dx": 1, "dy": 0}})
        return self.controller.last_sent_seq

    def _ack(self, seq, after):
        self.clock.value += after
        self.controller._handle_inbound(protocol.ack_msg(seq))

    def test_nothing_to_show_before_anything_is_acknowledged(self):
        self.assertIsNone(self.controller.round_trip_ms)
        self._send()
        self.assertIsNone(self.controller.round_trip_ms)

    def test_one_trip_is_measured_from_the_send_of_the_acknowledged_event(self):
        first = self._send()
        self.clock.value += 0.3
        second = self._send()
        self._ack(second, 0.05)
        self.assertEqual(self.controller.round_trip_ms, 50, "the ACK names seq 2, sent 50ms ago, not seq 1")
        self.assertNotEqual(first, second)

    def test_the_reading_is_the_upper_median_of_the_last_few_trips(self):
        for delay in (0.010, 0.040, 0.020, 0.030):
            seq = self._send()
            self._ack(seq, delay)
        self.assertEqual(self.controller.round_trip_ms, 30)

    def test_a_repeated_ack_of_the_same_seq_adds_no_sample(self):
        seq = self._send()
        self._ack(seq, 0.02)
        self._ack(seq, 1.0)
        self._ack(seq, 1.0)
        self.assertEqual(self.controller.round_trip_ms, 20)

    def test_only_the_last_handful_of_trips_count(self):
        for _ in range(ROUND_TRIP_SAMPLES):
            self._ack(self._send(), 0.5)
        for _ in range(ROUND_TRIP_SAMPLES):
            self._ack(self._send(), 0.01)
        self.assertEqual(self.controller.round_trip_ms, 10)

    def test_nothing_is_shown_when_input_is_not_on_windows(self):
        self._ack(self._send(), 0.02)
        self.controller.set_redirecting(False)
        self.assertIsNone(self.controller.round_trip_ms)
        self.controller.set_redirecting(True)
        self.assertIsNone(self.controller.round_trip_ms, "the last session's figure does not carry over")

    def test_nothing_is_shown_when_the_link_is_down(self):
        self._ack(self._send(), 0.02)
        self.controller._connection_failed("Windows stopped responding")
        link(self.controller)
        self.controller.redirecting = True
        self.assertIsNone(self.controller.round_trip_ms)

    def test_a_figure_goes_stale_after_a_few_seconds_without_a_fresh_ack(self):
        self._ack(self._send(), 0.02)
        self.clock.value += ROUND_TRIP_MAX_AGE_SECONDS - 0.1
        self.assertEqual(self.controller.round_trip_ms, 20)
        self.clock.value += 0.2
        self.assertIsNone(self.controller.round_trip_ms)


class FollowingAMovedPcTests(unittest.TestCase):
    """The PC's router hands it a new address; it still beacons under the name it paired with."""

    OLD, NEW = "192.0.2.10", "192.0.2.77"

    def make(self, reply_token="synthetic-token", beacons=None):
        sock = FakeSocket()
        seed_receiver_reply(sock, protocol.welcome_msg(), token=reply_token)
        tried = []

        def socket_factory(address, timeout):
            tried.append(address[0])
            if address[0] == self.OLD:
                raise ConnectionRefusedError("nothing there now")
            return sock

        controller = KVMController(
            dataclasses.replace(make_config(), pc_name="STUDIO-PC"),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=FakeClock(),
            socket_factory=socket_factory,
        )
        found = beacons if beacons is not None else [
            {"name": "STUDIO-PC", "address": self.NEW, "port": 51820, "reply_port": 24821, "pair_id": None}]
        controller.discovery = types.SimpleNamespace(pcs=lambda: found)
        learned = []
        controller.on_host_learned = learned.append
        return controller, tried, learned

    def test_it_follows_the_pc_and_saves_the_address_once_it_authenticates(self):
        controller, tried, learned = self.make()
        self.assertFalse(controller._connect_once())
        controller._follow_the_pc()
        self.assertEqual(tried, [self.OLD, self.NEW])
        self.assertEqual(controller.cfg.host, self.NEW)
        self.assertEqual(learned, [self.NEW])
        self.assertIsNotNone(controller.sock)

    def test_a_beacon_that_cannot_authenticate_changes_nothing(self):
        controller, tried, learned = self.make(reply_token="an-impostors-token")
        controller._connect_once()
        controller._follow_the_pc()
        self.assertEqual(tried, [self.OLD, self.NEW])
        self.assertEqual(controller.cfg.host, self.OLD)
        self.assertEqual(learned, [])

    def test_another_pcs_beacon_is_ignored(self):
        controller, tried, learned = self.make(beacons=[
            {"name": "SOMEONE-ELSE", "address": self.NEW, "port": 51820, "reply_port": 24821, "pair_id": None}])
        controller._connect_once()
        controller._follow_the_pc()
        self.assertEqual(tried, [self.OLD])
        self.assertEqual(learned, [])


if __name__ == "__main__":
    unittest.main()
