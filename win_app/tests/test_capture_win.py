"""What this PC's keys become on the wire. The translation runs against the
real keyboard layout through ToUnicodeEx, which reads a keyboard state handed
to it rather than the live one, so nothing here touches what is actually being
typed."""

import ctypes
import sys
import unittest
from unittest.mock import Mock, patch

import capture_win
from capture_win import REDIRECT, RETURN, TOGGLE, KeyboardState, Trigger, key_name, wheel_notches
from input_injector import INJECTED_MARK

VK_A = 0x41
VK_1 = 0x31
VK_NUMPAD5 = 0x65


def names_only(vk, scan, state):
    """A translate function for the tests that never touch Windows."""
    return {VK_A: "a"}.get(vk)


class NameTests(unittest.TestCase):
    def test_ctrl_leaves_this_pc_as_command(self):
        # The semantic swap happens here and nowhere else: the Mac injects
        # exactly the name it is given.
        state = KeyboardState()
        self.assertEqual(key_name(0xA2, 0, state, names_only), "cmd")
        self.assertEqual(key_name(0xA3, 0, state, names_only), "cmd_r")

    def test_the_windows_key_leaves_as_control(self):
        state = KeyboardState()
        self.assertEqual(key_name(0x5B, 0, state, names_only), "ctrl")

    def test_alt_stays_alt(self):
        state = KeyboardState()
        self.assertEqual(key_name(0xA4, 0, state, names_only), "alt")

    def test_a_key_with_no_character_sends_nothing(self):
        state = KeyboardState()
        self.assertIsNone(key_name(0xFF, 0, state, names_only))

    def test_a_dead_key_is_composed_without_leaving_state_for_the_next_translation(self):
        class FrenchLayout:
            def __init__(self):
                self.pending = False
                self.flags = []

            def GetKeyboardLayout(self, _thread):
                return 1

            def ToUnicodeEx(self, vk, scan, key_state, buffer, size, flags, layout):
                self.flags.append(flags)
                if vk == 0xDD:
                    if not flags & 0x4:
                        self.pending = True
                    buffer[0] = "^"
                    return -1
                self.assert_no_pending = not self.pending
                self.pending = False
                buffer[0] = "ê" if self.pending else "e"
                return 1

        layout = FrenchLayout()
        state = KeyboardState()
        with patch.object(capture_win, "user32", layout):
            dead_key = key_name(0xDD, 0, state, capture_win._to_unicode)
            self.assertEqual(getattr(dead_key, "character", None), "^")
            self.assertEqual(key_name(0x45, 0, state, capture_win._to_unicode), "ê")
        self.assertEqual(layout.flags, [0x4, 0x4])
        self.assertTrue(layout.assert_no_pending)

    def test_a_dead_key_followed_by_space_types_its_spacing_character(self):
        state = KeyboardState()

        def translate(vk, scan, key_state):
            return capture_win.DeadKey("^") if vk == 0xDD else "space"

        self.assertEqual(getattr(key_name(0xDD, 0, state, translate), "character", None), "^")
        self.assertEqual(key_name(0x20, 0, state, translate), "^")

    def test_a_dead_key_without_a_unicode_composition_becomes_text(self):
        state = KeyboardState()

        def translate(vk, scan, key_state):
            return capture_win.DeadKey("§") if vk == 0xDD else "a"

        key_name(0xDD, 0, state, translate)
        text = key_name(0x41, 0, state, translate)
        self.assertEqual(type(text).__name__, "TextInput")
        self.assertEqual(text.text, "§a")

    def test_circumflex_followed_by_x_becomes_text_and_by_e_composes(self):
        state = KeyboardState()

        def translate(vk, scan, key_state):
            return capture_win.DeadKey("^") if vk == 0xDD else {0x58: "x", 0x45: "e"}.get(vk)

        key_name(0xDD, 0, state, translate)
        text = key_name(0x58, 0, state, translate)
        self.assertIsInstance(text, capture_win.TextInput)
        self.assertEqual(text.text, "^x")

        key_name(0xDD, 0, state, translate)
        self.assertEqual(key_name(0x45, 0, state, translate), "ê")

    def test_consumed_text_input_swallows_the_physical_key_release(self):
        class HookData(ctypes.Structure):
            _fields_ = [
                ("vkCode", ctypes.c_ulong),
                ("scanCode", ctypes.c_ulong),
                ("flags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        on_key = Mock(return_value=True)
        hooks = capture_win.Hooks(on_key, Mock(), Mock())
        data = HookData(0x41, 0x1E, 0, 0, 0)
        user32 = Mock()
        user32.CallNextHookEx.return_value = 99

        with patch.object(capture_win, "KBDLLHOOKSTRUCT", HookData, create=True), \
                patch.object(capture_win, "user32", user32), \
                patch.object(capture_win, "key_name", return_value=capture_win.TextInput("§a")):
            key_down = hooks._keyboard_proc(capture_win.HC_ACTION, capture_win.WM_KEYDOWN, ctypes.byref(data))
            key_up = hooks._keyboard_proc(capture_win.HC_ACTION, capture_win.WM_KEYUP, ctypes.byref(data))

        self.assertEqual((key_down, key_up), (1, 1))
        on_key.assert_called_once()
        self.assertIsInstance(on_key.call_args.args[0], capture_win.TextInput)
        self.assertEqual(on_key.call_args.args[1:], (True, 0x41, None))
        self.assertEqual(hooks._text_keyups_swallowed, set())

    def test_stopping_hooks_clears_pending_text_key_releases(self):
        hooks = capture_win.Hooks(Mock(), Mock(), Mock())
        hooks._text_keyups_swallowed.add(0x41)

        hooks.stop()

        self.assertEqual(hooks._text_keyups_swallowed, set())

    def test_a_repeated_base_key_after_text_fallback_repeats_as_text(self):
        class HookData(ctypes.Structure):
            _fields_ = [
                ("vkCode", ctypes.c_ulong),
                ("scanCode", ctypes.c_ulong),
                ("flags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        on_key = Mock(return_value=True)
        hooks = capture_win.Hooks(on_key, Mock(), Mock())
        hooks._text_keyups_swallowed.add(0x41)
        data = HookData(0x41, 0x1E, 0, 0, 0)
        user32 = Mock()

        with patch.object(capture_win, "KBDLLHOOKSTRUCT", HookData, create=True), \
                patch.object(capture_win, "user32", user32), \
                patch.object(capture_win, "key_name", return_value="a"):
            repeated_down = hooks._keyboard_proc(capture_win.HC_ACTION, capture_win.WM_KEYDOWN, ctypes.byref(data))
            key_up = hooks._keyboard_proc(capture_win.HC_ACTION, capture_win.WM_KEYUP, ctypes.byref(data))

        self.assertEqual((repeated_down, key_up), (1, 1))
        on_key.assert_called_once()
        self.assertEqual(on_key.call_args.args[0].text, "a")
        self.assertIsInstance(on_key.call_args.args[0], capture_win.TextInput)

    def test_a_key_release_does_not_consume_a_pending_dead_key(self):
        state = KeyboardState()

        def translate(vk, scan, key_state):
            return {0x41: "a", 0x45: "e"}.get(vk)

        self.assertEqual(key_name(0x41, 0, state, translate, down=True), "a")
        self.assertEqual(getattr(key_name(0xDD, 0, state, lambda *_: capture_win.DeadKey("^")), "character", None), "^")
        self.assertEqual(key_name(0x41, 0, state, translate, down=False), "a")
        self.assertEqual(state.pending_dead_key, "^")
        self.assertEqual(key_name(0x45, 0, state, translate, down=True), "ê")


@unittest.skipUnless(sys.platform == "win32", "ToUnicodeEx needs Windows")
class LayoutTests(unittest.TestCase):
    """Against the real layout. The state passed in is the one the hook keeps,
    not the live keyboard, so these translate a key without pressing it."""

    def translate(self, vk, shift=False, caps=False):
        state = KeyboardState()
        state.shift_down = shift
        state.caps_lock = caps
        return key_name(vk, 0, state, capture_win._to_unicode)

    def test_a_letter_comes_back_as_itself(self):
        self.assertEqual(self.translate(VK_A), "a")

    def test_shift_is_applied(self):
        self.assertEqual(self.translate(VK_A, shift=True), "A")

    def test_caps_lock_is_applied(self):
        self.assertEqual(self.translate(VK_A, caps=True), "A")

    def test_a_shifted_digit_gives_the_symbol_the_layout_prints(self):
        self.assertEqual(self.translate(VK_1), "1")
        self.assertEqual(self.translate(VK_1, shift=True), "!")

    def test_the_numpad_types_its_digit(self):
        self.assertEqual(self.translate(VK_NUMPAD5), "5")


class WheelTests(unittest.TestCase):
    def test_one_notch_each_way(self):
        self.assertEqual(wheel_notches(120 << 16), 1.0)
        self.assertEqual(wheel_notches((0x10000 - 120) << 16), -1.0)


class TriggerTests(unittest.TestCase):
    def test_two_taps_inside_the_window_fire_once(self):
        trigger = Trigger("cmd_r", "double_tap", 300)
        self.assertIsNone(trigger.feed("cmd_r", True, 1.00))
        self.assertEqual(trigger.feed("cmd_r", True, 1.20), TOGGLE)
        # The pair is spent: a third tap starts a new one rather than firing.
        self.assertIsNone(trigger.feed("cmd_r", True, 1.30))

    def test_a_slow_second_tap_does_not_fire(self):
        trigger = Trigger("cmd_r", "double_tap", 300)
        trigger.feed("cmd_r", True, 1.0)
        self.assertIsNone(trigger.feed("cmd_r", True, 2.0))

    def test_another_key_is_not_the_trigger(self):
        trigger = Trigger("cmd_r", "double_tap", 300)
        trigger.feed("cmd_r", True, 1.0)
        self.assertIsNone(trigger.feed("a", True, 1.1))

    def test_holding_sends_input_over_and_releasing_brings_it_back(self):
        trigger = Trigger("alt_r", "hold", 300)
        self.assertEqual(trigger.feed("alt_r", True, 1.0), REDIRECT)
        # Key repeat while held changes nothing, and is still swallowed.
        self.assertIsNone(trigger.feed("alt_r", True, 1.1))
        self.assertEqual(trigger.feed("alt_r", False, 1.4), RETURN)

    def test_the_key_and_the_style_can_be_changed(self):
        trigger = Trigger("cmd_r", "double_tap", 300)
        trigger.configure("alt_r", "hold", 300)
        self.assertIsNone(trigger.feed("cmd_r", True, 1.0))
        self.assertEqual(trigger.feed("alt_r", True, 1.0), REDIRECT)


class HandMotionTests(unittest.TestCase):
    """What the WM_INPUT reader keeps. Raw Input reports a SendInput move
    exactly as it reports the mouse, so the injector's stamp is the only
    thing keeping the Mac's pointer out of this PC's own edge."""

    def test_the_hands_counts_pass(self):
        self.assertEqual(capture_win.hand_motion(0, -20, 3, 0), (-20, 3))

    def test_a_move_beamer_injected_is_not_the_hands(self):
        self.assertIsNone(capture_win.hand_motion(0, -20, 3, INJECTED_MARK))

    def test_another_devices_own_signature_still_passes(self):
        # A pen stamps its own signature into the same field; only Beamer's
        # mark is Beamer's.
        self.assertEqual(capture_win.hand_motion(0, -20, 3, 0xFF515700), (-20, 3))

    def test_an_absolute_reading_is_not_measured(self):
        self.assertIsNone(capture_win.hand_motion(capture_win.MOUSE_MOVE_ABSOLUTE, 500, 500, 0))

    def test_no_movement_is_nothing(self):
        self.assertIsNone(capture_win.hand_motion(0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
