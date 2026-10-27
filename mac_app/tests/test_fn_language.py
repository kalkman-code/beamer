import unittest
from dataclasses import replace
from unittest import mock

import bridge
import fn_language
from core import protocol
from bridge_fakes import PAIRED_TOKEN, FakeClock, FakeQuartz as Q, make_config, quiet_logger, redirect_to
from fake_link import FakeLink, bring_up


def fn(down):
    return {Q.kCGKeyboardEventKeycode: fn_language.FN_CODE,
            "flags": fn_language.FN_FLAG if down else 0}


class FnTapTests(unittest.TestCase):
    def setUp(self):
        self.tap = fn_language.FnTap(Q)

    def feed(self, event, enabled=True, kind=Q.kCGEventFlagsChanged):
        return self.tap.feed(kind, event, enabled=enabled)

    def test_one_bare_tap_toggles_once_on_release_even_with_duplicate_down(self):
        self.assertEqual(self.feed(fn(True)), (True, False))
        self.assertEqual(self.feed(fn(True)), (True, False))
        self.assertEqual(self.feed(fn(False)), (True, True))
        self.assertEqual(self.feed(fn(False)), (False, False))

    def test_local_press_or_changed_target_cannot_toggle(self):
        for start, end in ((False, True), (True, False), (("pc", 1), ("pc", 2))):
            with self.subTest(start=start, end=end):
                self.feed(fn(True), start)
                self.assertFalse(self.feed(fn(False), end)[1])

    def test_function_flag_on_arrow_is_not_a_physical_fn_press(self):
        for code in (0x7B, 0x7C, 0x7D, 0x7E, 0x7A):
            self.feed({Q.kCGKeyboardEventKeycode: code, "flags": fn_language.FN_FLAG}, kind=Q.kCGEventKeyDown)
            self.feed({Q.kCGKeyboardEventKeycode: code, "flags": fn_language.FN_FLAG}, kind=Q.kCGEventKeyUp)
        self.assertEqual(self.feed(fn(False)), (False, False))

    def test_fn_chords_and_mouse_buttons_do_not_toggle(self):
        cases = [(Q.kCGEventKeyDown, {Q.kCGKeyboardEventKeycode: 0x7B}),
                 (Q.kCGEventFlagsChanged, {Q.kCGKeyboardEventKeycode: 0x37, "flags": Q.kCGEventFlagMaskCommand}),
                 (Q.kCGEventLeftMouseDown, {}), (Q.kCGEventScrollWheel, {}), (14, {})]
        for kind, event in cases:
            with self.subTest(kind=kind):
                self.tap = fn_language.FnTap(Q)
                self.feed(fn(True))
                self.assertEqual(self.feed(event, kind=kind), (False, False))
                self.assertEqual(self.feed(fn(False)), (True, False))

    def test_keys_or_modifiers_already_held_do_not_toggle(self):
        self.feed({Q.kCGKeyboardEventKeycode: 0}, kind=Q.kCGEventKeyDown)
        self.feed(fn(True))
        self.assertEqual(self.feed(fn(False)), (True, False))
        self.tap = fn_language.FnTap(Q)
        self.feed({**fn(True), "flags": fn_language.FN_FLAG | Q.kCGEventFlagMaskAlternate})
        self.assertEqual(self.feed(fn(False)), (True, False))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.controller = bridge.KVMController(
            replace(make_config(PAIRED_TOKEN), keyboard_sync=True),
            logger=quiet_logger(), quartz=Q, clock=FakeClock(), link_factory=FakeLink)
        self.link = bring_up(self.controller, caps=("input_language",))
        redirect_to(self.controller)
        self.link._sent.clear()

    def capture(self, event, kind=Q.kCGEventFlagsChanged):
        return self.controller._event_tap_callback(None, kind, event, None)

    def drain(self):
        while not self.controller.outbound.empty():
            self.controller._process_outbound(self.controller.outbound.get_nowait())

    def test_fn_reaches_existing_windows_protocol_and_next_key_carries_new_language(self):
        language = ["ru"]
        def select(value):
            language[0] = value
            return True
        with mock.patch.object(bridge.keyboard_layout, "current_language", side_effect=lambda: language[0]), \
                mock.patch.object(bridge.keyboard_layout, "select_language", side_effect=select) as selected:
            self.assertIsNone(self.capture(fn(True)))
            self.assertIsNone(self.capture(fn(False)))
            self.drain()
            selected.assert_called_once_with("en")
            self.assertEqual(self.link._sent, [{"type": protocol.MSG_INPUT_LANGUAGE, "data": {"language": "en"}}])
            self.capture({Q.kCGKeyboardEventKeycode: 0, "unicode": "a"}, Q.kCGEventKeyDown)
            self.drain()
            self.assertEqual(self.link._sent[-1]["data"]["input_language"], "en")
            protocol.read_input(self.link._sent[0])

    def test_fn_arrow_forwards_arrow_without_language_change(self):
        with mock.patch.object(bridge.keyboard_layout, "select_language") as selected:
            self.capture(fn(True))
            self.capture({Q.kCGKeyboardEventKeycode: 0x7B, "flags": fn_language.FN_FLAG}, Q.kCGEventKeyDown)
            self.capture({Q.kCGKeyboardEventKeycode: 0x7B, "flags": fn_language.FN_FLAG}, Q.kCGEventKeyUp)
            self.capture(fn(False))
            self.drain()
            selected.assert_not_called()
            self.assertEqual([m["data"]["key"] for m in self.link._sent], ["left", "left"])

    def test_queued_toggle_dropped_after_returning_home(self):
        self.capture(fn(True))
        self.capture(fn(False))
        self.controller.redirecting = False
        with mock.patch.object(bridge.keyboard_layout, "select_language") as selected:
            self.drain()
            selected.assert_not_called()
        self.assertEqual(self.link._sent, [])

    def test_missing_source_does_not_send_a_toggle(self):
        with mock.patch.object(bridge.keyboard_layout, "current_language", return_value="en"), \
                mock.patch.object(bridge.keyboard_layout, "select_language", return_value=False):
            self.capture(fn(True))
            self.capture(fn(False))
            self.drain()
        self.assertEqual(self.link._sent, [])

    def test_gate_requires_sync_keyboard_windows_peer_and_capability(self):
        for change in ({"keyboard_sync": False}, {"mouse_only": True},
                       {"ignored_inputs": ["key:63"]}):
            with self.subTest(change=change):
                saved = self.controller.cfg
                self.controller.cfg = replace(saved, **change)
                self.assertIsNone(self.controller._fn_sync_target())
                self.controller.cfg = saved
        self.link.peer_platform = "macos"
        self.assertIsNone(self.controller._fn_sync_target())
        self.link.peer_platform = "windows"
        self.link.caps = set()
        self.assertIsNone(self.controller._fn_sync_target())


if __name__ == "__main__":
    unittest.main()
