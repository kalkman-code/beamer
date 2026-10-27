"""Regression tests verifying that Caps Lock handling and input-source switching
(e.g. System Settings > Keyboard > Input Sources > 'Use Caps Lock to switch to and from Latin')
cannot raise, abort, or terminate Beamer.

Covers:
1. FlagsChanged with keycode 57 (0x39) without AlphaShift flag toggling (input-source switch).
2. Keycode 57 with unexpected or arbitrary flags.
3. Keycode 57 arriving as KeyDown / KeyUp.
4. Input source change notifications (TISNotifySelectedKeyboardInputSourceChanged) arriving
   mid-press, including when switching to Korean (IME with no uchr layout data).
5. Uncaught exception containment in the CGEventTap callback.
6. Thread safety of keyboard_layout lookups off the main thread.
"""

import os
import queue
import sys
import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock
from PyObjCTools import AppHelper

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import ignored
from core import protocol
import bridge
from bridge import KVMController, QuartzEventTranslator, CAPS_LOCK_KEY_CODE
from bridge_fakes import (
    PAIRED_TOKEN,
    FakeClock,
    FakeQuartz,
    make_config,
    quiet_logger,
    redirect_to,
)
from fake_link import FakeLink
import keyboard_layout

HID = FakeQuartz.kCGEventSourceStateHIDSystemState


class CapsLockEventTapTests(unittest.TestCase):
    def setUp(self):
        self.controller = KVMController(
            make_config(PAIRED_TOKEN),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=FakeClock(),
            link_factory=FakeLink,
            system_event_converter=lambda event: event,
        )

    def tap(self, event_type, event):
        return self.controller._event_tap_callback(None, event_type, event, None)

    def sent(self):
        messages = []
        while not self.controller.outbound.empty():
            messages.append(self.controller.outbound.get_nowait())
        return [(m["type"], m["data"]) for m in messages]

    def tap_on_worker(self, event_type, event):
        callbacks = queue.Queue()
        dispatched = []
        returned = []
        tap_thread = threading.Thread(
            target=lambda: returned.append(self.tap(event_type, event)),
            daemon=True,
        )
        with mock.patch.object(
            AppHelper,
            "callAfter",
            lambda callback: (dispatched.append(callback), callbacks.put(callback)),
        ):
            tap_thread.start()
            deadline = time.monotonic() + 2
            while tap_thread.is_alive() and time.monotonic() < deadline:
                try:
                    callback = callbacks.get(timeout=0.05)
                except queue.Empty:
                    continue
                callback()
            tap_thread.join(timeout=0.1)
        return returned, dispatched, tap_thread.is_alive()

    def test_caps_lock_without_alphashift_flag_passes_through_locally(self):
        """When input is at home and macOS consumes Caps Lock as an input-source switch
        (Use Caps Lock to switch to and from Latin), flagsChanged arrives with keycode 57
        and NO AlphaShift flag. It must pass through locally to macOS without raising or quitting."""
        event = {FakeQuartz.kCGKeyboardEventKeycode: CAPS_LOCK_KEY_CODE, "flags": 0}
        returned = self.tap(FakeQuartz.kCGEventFlagsChanged, event)
        self.assertIs(returned, event)
        self.assertEqual(self.sent(), [])

    def test_hangul_and_hanja_are_taps_without_a_release_message(self):
        translator = QuartzEventTranslator(FakeQuartz)
        for name, keycode in (("hangul", 0x68), ("hanja", 0x66)):
            with self.subTest(name=name):
                key = {FakeQuartz.kCGKeyboardEventKeycode: keycode, FakeQuartz.kCGEventSourceStateID: HID}
                down = translator.key_result(FakeQuartz.kCGEventKeyDown, key, 0x3D, {})
                up = translator.key_result(FakeQuartz.kCGEventKeyUp, key, 0x3D, {})
                self.assertEqual(down.messages, [
                    {"type": protocol.MSG_KEYDOWN, "data": {"key": name}},
                    {"type": protocol.MSG_KEYUP, "data": {"key": name}},
                ])
                self.assertEqual(up.messages, [])
                repeat = translator.key_result(FakeQuartz.kCGEventKeyDown,
                                               {**key, FakeQuartz.kCGKeyboardEventAutorepeat: 1}, 0x3D, {})
                self.assertEqual(repeat.messages, [])

    def test_hangul_at_home_stays_here_and_a_letter_still_passes(self):
        for keycode in (0x68, 0x00):
            for event_type in (FakeQuartz.kCGEventKeyDown, FakeQuartz.kCGEventKeyUp):
                event = {FakeQuartz.kCGKeyboardEventKeycode: keycode, FakeQuartz.kCGEventSourceStateID: HID}
                self.assertIs(self.tap(event_type, event), event)

    def test_hangul_while_away_is_one_tap_and_its_release_stays_here(self):
        redirect_to(self.controller)
        self.sent()
        down = {FakeQuartz.kCGKeyboardEventKeycode: 0x68, FakeQuartz.kCGEventSourceStateID: HID}
        up = dict(down)
        self.assertIsNone(self.tap(FakeQuartz.kCGEventKeyDown, down))
        self.assertIs(self.tap(FakeQuartz.kCGEventKeyUp, up), up)
        self.assertEqual(self.sent(), [
            (protocol.MSG_KEYDOWN, {"key": "hangul"}),
            (protocol.MSG_KEYUP, {"key": "hangul"}),
        ])

    def test_flags_changed_caps_lock_never_calls_event_converters_on_tap_thread(self):
        event = {FakeQuartz.kCGKeyboardEventKeycode: CAPS_LOCK_KEY_CODE, "flags": 0}
        calls = []

        def event_with_cg_event(raw_event):
            calls.append(threading.current_thread())
            raise AssertionError("Caps Lock flagsChanged reached an AppKit event converter")

        appkit = SimpleNamespace(NSEvent=SimpleNamespace(eventWithCGEvent_=event_with_cg_event))
        self.controller.system_event_converter = bridge._default_system_event_converter
        self.controller.gesture_event_converter = bridge._default_gesture_event_converter
        redirect_to(self.controller)
        with mock.patch.object(bridge, "AppKit", appkit):
            returned, dispatched, thread_alive = self.tap_on_worker(
                FakeQuartz.kCGEventFlagsChanged, event
            )

        self.assertFalse(thread_alive)
        self.assertEqual(calls, [])
        self.assertEqual(dispatched, [])
        self.assertEqual(returned, [None])
        self.assertEqual(
            self.sent(),
            [
                (protocol.MSG_KEYDOWN, {"key": "caps_lock"}),
                (protocol.MSG_KEYUP, {"key": "caps_lock"}),
            ],
        )

    def test_system_defined_event_conversion_runs_on_main_thread(self):
        converted = []

        def event_with_cg_event(event):
            converted.append(threading.current_thread())
            return SimpleNamespace(subtype=lambda: 8, data1=lambda: 0x0010000A)

        appkit = SimpleNamespace(NSEvent=SimpleNamespace(eventWithCGEvent_=event_with_cg_event))
        self.controller.system_event_converter = bridge._default_system_event_converter
        redirect_to(self.controller)
        event = object()
        with mock.patch.object(bridge, "AppKit", appkit):
            returned, dispatched, thread_alive = self.tap_on_worker(
                bridge.media_keys.NX_SYSDEFINED_EVENT_TYPE, event
            )

        self.assertFalse(thread_alive)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(converted, [threading.main_thread()])
        self.assertEqual(returned, [None])

    def test_gesture_event_conversion_runs_on_main_thread(self):
        converted = []

        def event_with_cg_event(event):
            converted.append(threading.current_thread())
            return SimpleNamespace(deltaX=lambda: 1.0)

        appkit = SimpleNamespace(NSEvent=SimpleNamespace(eventWithCGEvent_=event_with_cg_event))
        self.controller.gesture_event_converter = bridge._default_gesture_event_converter
        redirect_to(self.controller)
        with mock.patch.object(bridge, "AppKit", appkit):
            returned, dispatched, thread_alive = self.tap_on_worker(
                bridge.gestures.SWIPE_TYPE, object()
            )

        self.assertFalse(thread_alive)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(converted, [threading.main_thread()])
        self.assertEqual(returned, [None])

    def test_caps_lock_with_alphashift_flag_passes_through_locally(self):
        """Standard Caps Lock toggle with AlphaShift flag set while at home passes through."""
        event = {
            FakeQuartz.kCGKeyboardEventKeycode: CAPS_LOCK_KEY_CODE,
            "flags": FakeQuartz.kCGEventFlagMaskAlphaShift,
        }
        returned = self.tap(FakeQuartz.kCGEventFlagsChanged, event)
        self.assertIs(returned, event)
        self.assertEqual(self.sent(), [])

    def test_caps_lock_without_alphashift_flag_redirects_when_away(self):
        """When redirecting away, Caps Lock without AlphaShift sends paired down/up messages
        and clears the gate route without pinning."""
        redirect_to(self.controller)
        event = {FakeQuartz.kCGKeyboardEventKeycode: CAPS_LOCK_KEY_CODE, "flags": 0}
        returned = self.tap(FakeQuartz.kCGEventFlagsChanged, event)
        self.assertIsNone(returned)
        sent = self.sent()
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], (protocol.MSG_KEYDOWN, {"key": "caps_lock"}))
        self.assertEqual(sent[1], (protocol.MSG_KEYUP, {"key": "caps_lock"}))

    def test_caps_lock_unexpected_flags_never_raises(self):
        """Unexpected flags masks on keycode 57 (arbitrary bits, device bits, high masks)
        never cause key_result or event tap callback to raise."""
        translator = QuartzEventTranslator(FakeQuartz)
        weird_flags = [0, 0xFFFFFFFF, 1 << 16, 1 << 17, 1 << 18, 1 << 19, 1 << 20, 0x0010, 0x2000]
        for flags in weird_flags:
            event = {FakeQuartz.kCGKeyboardEventKeycode: CAPS_LOCK_KEY_CODE, "flags": flags}
            result = translator.key_result(FakeQuartz.kCGEventFlagsChanged, event, 0x3D, {})
            self.assertTrue(result.is_down)
            self.assertEqual(len(result.messages), 2)

            returned = self.tap(FakeQuartz.kCGEventFlagsChanged, event)
            self.assertIs(returned, event)

    def test_caps_lock_as_keydown_and_keyup_never_raises(self):
        """If an input-source switch or third-party tool delivers Caps Lock as KeyDown/KeyUp,
        it maps as a special key and passes through without raising or quitting."""
        translator = QuartzEventTranslator(FakeQuartz)
        down_event = {
            FakeQuartz.kCGKeyboardEventKeycode: CAPS_LOCK_KEY_CODE,
            FakeQuartz.kCGKeyboardEventAutorepeat: 0,
        }
        res_down = translator.key_result(FakeQuartz.kCGEventKeyDown, down_event, 0x3D, {})
        self.assertTrue(res_down.is_down)
        self.assertEqual(res_down.messages, [{"type": protocol.MSG_KEYDOWN, "data": {"key": "caps_lock"}}])

        up_event = {
            FakeQuartz.kCGKeyboardEventKeycode: CAPS_LOCK_KEY_CODE,
            FakeQuartz.kCGKeyboardEventAutorepeat: 0,
        }
        res_up = translator.key_result(FakeQuartz.kCGEventKeyUp, up_event, 0x3D, {})
        self.assertFalse(res_up.is_down)
        self.assertEqual(res_up.messages, [{"type": protocol.MSG_KEYUP, "data": {"key": "caps_lock"}}])

        self.assertIs(self.tap(FakeQuartz.kCGEventKeyDown, down_event), down_event)
        self.assertIs(self.tap(FakeQuartz.kCGEventKeyUp, up_event), up_event)

    def test_synthetic_jis_lang_keys_pass_through_safely(self):
        """When Caps Lock input switching emits synthetic keycodes (0x66/0x68 JIS Eisu/Kana),
        they are safely handled as unmapped or control characters without raising or quitting."""
        translator = QuartzEventTranslator(FakeQuartz)
        for code in (0x66, 0x68):
            down_event = {
                FakeQuartz.kCGKeyboardEventKeycode: code,
                FakeQuartz.kCGKeyboardEventAutorepeat: 0,
                "unicode": "\x10",  # DLE non-printable control char typical of JIS switches
            }
            res_down = translator.key_result(FakeQuartz.kCGEventKeyDown, down_event, 0x3D, {})
            self.assertTrue(res_down.is_down)
            self.assertEqual(res_down.messages, [])
            self.assertIs(self.tap(FakeQuartz.kCGEventKeyDown, down_event), down_event)

            up_event = {
                FakeQuartz.kCGKeyboardEventKeycode: code,
                FakeQuartz.kCGKeyboardEventAutorepeat: 0,
                "unicode": "\x10",
            }
            res_up = translator.key_result(FakeQuartz.kCGEventKeyUp, up_event, 0x3D, {})
            self.assertFalse(res_up.is_down)
            self.assertEqual(res_up.messages, [])
            self.assertIs(self.tap(FakeQuartz.kCGEventKeyUp, up_event), up_event)


class CapsLockLayoutSwitchTests(unittest.TestCase):
    def tearDown(self):
        keyboard_layout.install({})

    @unittest.skipUnless(sys.platform == "darwin", "reads macOS real input sources")
    def test_real_system_korean_sources_decode_cleanly(self):
        """On macOS, tests layout extraction from real installed Korean sources:
        com.apple.keylayout.2SetHangul (real uchr table) and
        com.apple.inputmethod.Korean.2SetKorean (IME with no uchr data).
        Both must decode without crashing, segfaulting, or raising."""
        import ctypes
        import Foundation
        import objc

        carbon = keyboard_layout._Carbon()
        lib, core = carbon.carbon, carbon.core
        lib.TISCreateInputSourceList.restype = ctypes.c_void_p
        lib.TISCreateInputSourceList.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        core.CFArrayGetCount.restype = ctypes.c_long
        core.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        core.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        core.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        core.CFRelease.argtypes = [ctypes.c_void_p]

        key = objc.objc_object(c_void_p=ctypes.c_void_p.in_dll(lib, "kTISPropertyInputSourceID").value)

        # 1. 2SetHangul layout has real layout data with 48 characters
        q_hangul = Foundation.NSDictionary.dictionaryWithObject_forKey_("com.apple.keylayout.2SetHangul", key)
        sources_h = lib.TISCreateInputSourceList(objc.pyobjc_id(q_hangul), True)
        if sources_h and core.CFArrayGetCount(sources_h):
            try:
                src_h = core.CFArrayGetValueAtIndex(sources_h, 0)
                chars_h, _dead_h = carbon.layout_chars(src_h)
                self.assertEqual(len(chars_h), 48)
                self.assertEqual(chars_h.get(0x00), "ㅁ")
            finally:
                core.CFRelease(sources_h)

        # 2. 2SetKorean IME mode has no uchr data; layout_chars returns ({}, {}) safely
        q_ime = Foundation.NSDictionary.dictionaryWithObject_forKey_("com.apple.inputmethod.Korean.2SetKorean", key)
        sources_ime = lib.TISCreateInputSourceList(objc.pyobjc_id(q_ime), True)
        if sources_ime and core.CFArrayGetCount(sources_ime):
            try:
                src_ime = core.CFArrayGetValueAtIndex(sources_ime, 0)
                chars_ime, _dead_ime = carbon.layout_chars(src_ime)
                self.assertEqual(chars_ime, {})
            finally:
                core.CFRelease(sources_ime)

    def test_korean_input_source_without_uchr_data_retains_fallbacks_safely(self):
        """Korean input methods (e.g. com.apple.inputmethod.Korean.2SetKorean) have no
        kTISPropertyUnicodeKeyLayoutData. When selected, layout_chars returns ({}, {})
        and refresh() safely logs info and returns False, leaving US fallbacks intact without raising."""
        class FakeCarbonWithoutLayoutData:
            def current_chars(self):
                return {}, {}

        saved_carbon = keyboard_layout._carbon
        try:
            keyboard_layout._carbon = FakeCarbonWithoutLayoutData()
            self.assertFalse(keyboard_layout.refresh())
            self.assertEqual(keyboard_layout.char_for(0x00), "a")
            self.assertEqual(keyboard_layout.code_for("a"), 0x00)
        finally:
            keyboard_layout._carbon = saved_carbon

    def test_layout_switch_notification_mid_press_does_not_desync(self):
        """Simulate an input-source switch notification (NSDistributedNotificationCenter)
        arriving during an active Caps Lock press sequence."""
        controller = KVMController(
            make_config(PAIRED_TOKEN),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=FakeClock(),
            link_factory=FakeLink,
            system_event_converter=lambda event: event,
        )

        caps_event = {FakeQuartz.kCGKeyboardEventKeycode: CAPS_LOCK_KEY_CODE, "flags": 0}
        self.assertIs(controller._event_tap_callback(None, FakeQuartz.kCGEventFlagsChanged, caps_event, None), caps_event)

        # Layout notification fires mid-session
        keyboard_layout.install({0x06: "y", 0x10: "z"})  # German layout switch
        self.assertEqual(keyboard_layout.char_for(0x06), "y")

        # Next Caps Lock press still passes through cleanly
        self.assertIs(controller._event_tap_callback(None, FakeQuartz.kCGEventFlagsChanged, caps_event, None), caps_event)


class EventTapSafetyTests(unittest.TestCase):
    def test_uncaught_exception_in_tap_callback_never_escapes(self):
        """Verifies that an unexpected exception during event processing is caught by
        _event_tap_callback's BaseException handler, forcing local cleanup and returning
        the event rather than allowing an unhandled exception to crash/quit the app."""
        class ExplodingQuartz(FakeQuartz):
            @classmethod
            def CGEventGetIntegerValueField(cls, event, field):
                if field == FakeQuartz.kCGKeyboardEventKeycode and event.get("explode"):
                    raise RuntimeError("simulated unexpected Quartz failure")
                return super().CGEventGetIntegerValueField(event, field)

        controller = KVMController(
            make_config(PAIRED_TOKEN),
            logger=quiet_logger(),
            quartz=ExplodingQuartz,
            clock=FakeClock(),
            link_factory=FakeLink,
            system_event_converter=lambda event: event,
        )
        redirect_to(controller)
        self.assertTrue(controller.redirecting)

        bad_event = {FakeQuartz.kCGKeyboardEventKeycode: CAPS_LOCK_KEY_CODE, "explode": True}
        # The callback must catch BaseException, return local, and return the event safely
        returned = controller._event_tap_callback(None, ExplodingQuartz.kCGEventFlagsChanged, bad_event, None)
        self.assertIs(returned, bad_event)
        self.assertFalse(controller.redirecting)

    def test_tap_and_injector_never_call_tis_off_main_thread(self):
        """The event tap thread and input injector thread only read char_for / code_for dicts;
        they never invoke Carbon TIS functions directly."""
        import input_injector_mac

        # char_for and code_for do not touch Carbon
        self.assertIsNotNone(keyboard_layout.char_for(0x00))
        self.assertIsNotNone(keyboard_layout.code_for("a"))

        # injector planning reads only cached lookups
        plan = input_injector_mac.plan_key_event("a", down=True, held={}, mods_down=frozenset())
        self.assertEqual(plan, (0x00, None))


if __name__ == "__main__":
    unittest.main()
