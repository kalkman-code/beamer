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


class HookPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class NameTests(unittest.TestCase):
    def test_input_method_keys_are_captured_and_not_offered_as_triggers(self):
        state = KeyboardState()
        self.assertEqual(key_name(0x15, 0, state, names_only), "hangul")
        self.assertEqual(key_name(0x19, 0, state, names_only), "hanja")
        self.assertTrue({0x15, 0x19} <= capture_win.NOT_TRIGGER_VKS)

    def test_every_named_key_but_the_undifferentiated_modifiers_has_a_title(self):
        # 1.4.3's loader, kept as a fixture, titles every named key but these three.
        self.assertEqual(set(capture_win.VK_TO_NAME) - set(capture_win.VK_TITLES), {0x10, 0x11, 0x12})

    def test_browser_search_and_app_launch_keys_are_captured(self):
        state = KeyboardState()
        self.assertEqual(key_name(0xAA, 0, state, names_only), "browser_search")
        self.assertEqual(key_name(0xB4, 0, state, names_only), "mail")
        self.assertEqual(key_name(0xB6, 0, state, names_only), "launch_app1")

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
            def GetForegroundWindow(self):
                return 0

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
        self.assertEqual(on_key.call_count, 2)
        self.assertIsInstance(on_key.call_args_list[0].args[0], capture_win.TextInput)
        self.assertEqual(on_key.call_args_list[0].args[1:], (True, 0x41, None))
        self.assertEqual(on_key.call_args_list[1].args[0].text, "")
        self.assertEqual(on_key.call_args_list[1].args[1:], (False, 0x41, None))
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
        self.assertEqual(on_key.call_count, 2)
        self.assertEqual(on_key.call_args_list[0].args[0].text, "a")
        self.assertIsInstance(on_key.call_args_list[0].args[0], capture_win.TextInput)
        self.assertEqual(on_key.call_args_list[1].args[1:], (False, 0x41, None))

    def test_a_key_release_does_not_consume_a_pending_dead_key(self):
        state = KeyboardState()

        def translate(vk, scan, key_state):
            return {0x41: "a", 0x45: "e"}.get(vk)

        self.assertEqual(key_name(0x41, 0, state, translate, down=True), "a")
        self.assertEqual(getattr(key_name(0xDD, 0, state, lambda *_: capture_win.DeadKey("^")), "character", None), "^")
        self.assertEqual(key_name(0x41, 0, state, translate, down=False), "a")
        self.assertEqual(state.pending_dead_key, "^")
        self.assertEqual(key_name(0x45, 0, state, translate, down=True), "ê")


class InjectedHookTests(unittest.TestCase):
    class HookData(ctypes.Structure):
        _fields_ = [
            ("vkCode", ctypes.c_ulong),
            ("scanCode", ctypes.c_ulong),
            ("flags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class MouseData(ctypes.Structure):
        _fields_ = [
            ("pt", HookPoint),
            ("mouseData", ctypes.c_ulong),
            ("flags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    def test_only_beamer_injected_keys_are_ignored(self):
        on_key = Mock(return_value=True)
        hooks = capture_win.Hooks(on_key, Mock(), Mock())
        user32 = Mock()
        user32.CallNextHookEx.return_value = 99
        marked = self.HookData(0x41, 0x1E, capture_win.LLKHF_INJECTED, 0, INJECTED_MARK)
        foreign = self.HookData(0x41, 0x1E, capture_win.LLKHF_INJECTED, 0, 123)

        with patch.object(capture_win, "KBDLLHOOKSTRUCT", self.HookData, create=True), \
                patch.object(capture_win, "user32", user32), \
                patch.object(capture_win, "key_name", return_value="a"):
            marked_result = hooks._keyboard_proc(capture_win.HC_ACTION, capture_win.WM_KEYDOWN, ctypes.byref(marked))
            foreign_result = hooks._keyboard_proc(capture_win.HC_ACTION, capture_win.WM_KEYDOWN, ctypes.byref(foreign))

        self.assertEqual((marked_result, foreign_result), (99, 1))
        on_key.assert_called_once_with("a", True, 0x41, "a")

    def test_foreign_injected_mouse_buttons_and_wheel_pass_but_moves_do_not(self):
        on_mouse = Mock(return_value=True)
        hooks = capture_win.Hooks(Mock(), on_mouse, Mock())
        user32 = Mock()
        user32.CallNextHookEx.return_value = 99
        marked_button = self.MouseData(HookPoint(5, 7), 0, capture_win.LLMHF_INJECTED, 0, INJECTED_MARK)
        foreign_button = self.MouseData(HookPoint(5, 7), 0, capture_win.LLMHF_INJECTED, 0, 123)
        foreign_wheel = self.MouseData(HookPoint(5, 7), 120 << 16, capture_win.LLMHF_INJECTED, 0, 123)
        foreign_move = self.MouseData(HookPoint(5, 7), 0, capture_win.LLMHF_INJECTED, 0, 123)

        with patch.object(capture_win, "MSLLHOOKSTRUCT", self.MouseData, create=True), \
                patch.object(capture_win, "user32", user32):
            results = (
                hooks._mouse_proc(capture_win.HC_ACTION, 0x0201, ctypes.byref(marked_button)),
                hooks._mouse_proc(capture_win.HC_ACTION, 0x0201, ctypes.byref(foreign_button)),
                hooks._mouse_proc(capture_win.HC_ACTION, 0x020A, ctypes.byref(foreign_wheel)),
                hooks._mouse_proc(capture_win.HC_ACTION, capture_win.WM_MOUSEMOVE, ctypes.byref(foreign_move)),
            )

        self.assertEqual(results, (99, 1, 1, 99))
        self.assertEqual(on_mouse.call_count, 2)

    def test_releasing_one_of_two_alt_keys_keeps_the_mac_switcher_open(self):
        from links_rig import B, Rig

        rig = Rig()
        rig.sender.set_redirecting(True)
        rig.flush()
        rig.links.sent.clear()
        hooks = capture_win.Hooks(rig.sender.on_key, Mock(), Mock())
        user32 = Mock()
        user32.CallNextHookEx.return_value = 99
        events = [
            (0xA4, capture_win.WM_KEYDOWN),
            (0xA5, capture_win.WM_KEYDOWN),
            (0x09, capture_win.WM_KEYDOWN),
            (0xA4, capture_win.WM_KEYUP),
            (0x09, capture_win.WM_KEYUP),
            (0xA5, capture_win.WM_KEYUP),
        ]
        data = [self.HookData(vk, 0, 0, 0, 0) for vk, _kind in events]
        key_names = {0xA4: "alt", 0xA5: "alt_r", 0x09: "tab"}
        with patch.object(capture_win, "KBDLLHOOKSTRUCT", self.HookData, create=True), \
                patch.object(capture_win, "user32", user32), \
                patch.object(capture_win, "key_name", side_effect=lambda vk, *_args: key_names[vk]):
            for item, (_vk, kind) in zip(data, events):
                hooks._keyboard_proc(capture_win.HC_ACTION, kind, ctypes.byref(item))

        keys = [(message["type"], message["data"].get("key")) for message in rig.sent(B)
                if message["type"] in ("keydown", "keyup")]
        self.assertEqual(keys.count(("keyup", "cmd")), 1)
        self.assertEqual(keys[-1], ("keyup", "cmd"))

    def test_alt_tab_to_mac_is_swallowed_and_uses_command_for_the_hold(self):
        from links_rig import B, Rig

        rig = Rig()
        rig.sender.set_redirecting(True)
        hooks = capture_win.Hooks(rig.sender.on_key, Mock(), Mock())
        user32 = Mock()
        user32.CallNextHookEx.return_value = 99
        events = [
            (0xA4, 0, capture_win.WM_KEYDOWN),
            (0x09, 0, capture_win.WM_KEYDOWN),
            (0x09, 0, capture_win.WM_KEYDOWN),
            (0xA0, 0, capture_win.WM_KEYDOWN),
            (0x09, 0, capture_win.WM_KEYDOWN),
            (0x09, 0, capture_win.WM_KEYUP),
            (0xA0, 0, capture_win.WM_KEYUP),
            (0xA4, 0, capture_win.WM_KEYUP),
        ]
        data = [self.HookData(vk, 0, flags, 0, 0) for vk, flags, _kind in events]
        key_names = {0xA4: "alt", 0x09: "tab", 0xA0: "shift"}
        with patch.object(capture_win, "KBDLLHOOKSTRUCT", self.HookData, create=True), \
                patch.object(capture_win, "user32", user32), \
                patch.object(capture_win, "key_name", side_effect=lambda vk, *_args: key_names[vk]):
            results = [
                hooks._keyboard_proc(capture_win.HC_ACTION, kind, ctypes.byref(item))
                for item, (_vk, _flags, kind) in zip(data, events)
            ]

        self.assertEqual(results, [1] * len(events))
        keys = [(message["type"], message["data"].get("key")) for message in rig.sent(B)
                if message["type"] in ("keydown", "keyup")]
        self.assertEqual(keys, [
            ("keydown", "alt"), ("keyup", "alt"), ("keydown", "cmd"),
            ("keydown", "tab"), ("keydown", "tab"), ("keydown", "shift"),
            ("keydown", "tab"), ("keyup", "tab"), ("keyup", "shift"), ("keyup", "cmd"),
        ])


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

    def test_the_numpad_keeps_its_physical_identity(self):
        self.assertEqual(self.translate(VK_NUMPAD5), "numpad_5")


class WheelTests(unittest.TestCase):
    def test_one_notch_each_way(self):
        self.assertEqual(wheel_notches(120 << 16), 1.0)
        self.assertEqual(wheel_notches((0x10000 - 120) << 16), -1.0)


class TriggerTests(unittest.TestCase):
    def test_two_taps_inside_the_window_fire_once(self):
        trigger = Trigger("cmd_r", "double_tap", 300)
        self.assertIsNone(trigger.feed("cmd_r", True, 1.00))
        self.assertIsNone(trigger.feed("cmd_r", False, 1.05))
        self.assertEqual(trigger.feed("cmd_r", True, 1.20), TOGGLE)
        self.assertIsNone(trigger.feed("cmd_r", False, 1.25))
        # The pair is spent: a third tap starts a new one rather than firing.
        self.assertIsNone(trigger.feed("cmd_r", True, 1.30))

    def test_a_slow_second_tap_does_not_fire(self):
        trigger = Trigger("cmd_r", "double_tap", 300)
        trigger.feed("cmd_r", True, 1.0)
        trigger.feed("cmd_r", False, 1.1)
        self.assertIsNone(trigger.feed("cmd_r", True, 2.0))

    def test_holding_the_key_never_toggles(self):
        # Windows' default autorepeat: the first repeat at 0.5 s, then every 33 ms. Before 1.5.2
        # this returned TOGGLE at 0.533, 0.599, 0.665 ... while the key was held.
        trigger = Trigger("cmd_r", "double_tap", 300)
        self.assertIsNone(trigger.feed("cmd_r", True, 0.0))
        held = [0.5 + 0.033 * n for n in range(60)]
        self.assertEqual([trigger.feed("cmd_r", True, at) for at in held], [None] * len(held))
        self.assertIsNone(trigger.feed("cmd_r", False, 2.5))
        # After the release, a real double-tap still fires.
        self.assertIsNone(trigger.feed("cmd_r", True, 3.0))
        self.assertIsNone(trigger.feed("cmd_r", False, 3.05))
        self.assertEqual(trigger.feed("cmd_r", True, 3.2), TOGGLE)

    def test_a_held_second_tap_toggles_once(self):
        trigger = Trigger("cmd_r", "double_tap", 300)
        trigger.feed("cmd_r", True, 1.0)
        trigger.feed("cmd_r", False, 1.05)
        self.assertEqual(trigger.feed("cmd_r", True, 1.2), TOGGLE)
        self.assertEqual([trigger.feed("cmd_r", True, 1.7 + 0.033 * n) for n in range(30)], [None] * 30)

    def test_a_key_held_while_the_shortcut_is_rearmed_does_not_toggle(self):
        # The bridge reconfigures the trigger on every key while the shortcut is off.
        trigger = Trigger("cmd_r", "double_tap", 300)
        trigger.feed("cmd_r", True, 1.0)
        trigger.configure("cmd_r", "double_tap", 300)
        self.assertEqual([trigger.feed("cmd_r", True, 1.5 + 0.033 * n) for n in range(30)], [None] * 30)

    def test_a_key_pressed_while_disarmed_and_held_into_arming_is_not_a_tap(self):
        # Pressed with the shortcut off, so feed never saw the press; its autorepeat after
        # arming must not count as the first tap of a pair a later single tap would complete.
        trigger = Trigger("cmd_r", "double_tap", 300)
        trigger.disarmed("cmd_r", True)
        self.assertIsNone(trigger.feed("cmd_r", True, 1.5))
        self.assertIsNone(trigger.feed("cmd_r", False, 1.55))
        self.assertIsNone(trigger.feed("cmd_r", True, 1.7))

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


class LockKeyHookTests(unittest.TestCase):
    HookData = InjectedHookTests.HookData

    def key(self, hooks, vk, down, native, injected=False):
        data = self.HookData(vk, 0x3A if vk == 0x14 else 0x45,
                             capture_win.LLKHF_INJECTED if injected else 0, 0,
                             INJECTED_MARK if injected else 0)

        def accept(_handle, _code, _message, _data):
            if down and vk not in native["down"]:
                native[vk] = not native[vk]
            if down:
                native["down"].add(vk)
            else:
                native["down"].discard(vk)
            return 99

        with patch.object(capture_win, "KBDLLHOOKSTRUCT", self.HookData, create=True), \
                patch.object(capture_win, "user32", Mock(CallNextHookEx=accept)):
            return hooks._keyboard_proc(capture_win.HC_ACTION,
                capture_win.WM_KEYDOWN if down else capture_win.WM_KEYUP, ctypes.byref(data))

    def test_remote_lock_presses_change_only_the_recipient_and_its_translation_state(self):
        source_native = {0x14: False, 0x90: True, "down": set()}
        target_native = {0x14: True, 0x90: False, "down": set()}
        target_key = Mock(return_value=False)
        target = capture_win.Hooks(target_key, Mock(), Mock())
        target._state.caps_lock = True

        def forward(_name, down, vk, _us):
            self.key(target, vk, down, target_native, injected=True)
            return True

        source = capture_win.Hooks(forward, Mock(), Mock())
        for vk in (0x14, 0x90):
            for down in (True, True, False):
                self.assertEqual(self.key(source, vk, down, source_native), 1)
        self.assertFalse(source_native[0x14])
        self.assertTrue(source_native[0x90])
        self.assertFalse(target_native[0x14])
        self.assertTrue(target_native[0x90])
        self.assertFalse(source._state.caps_lock)
        self.assertFalse(target._state.caps_lock)
        target_key.assert_not_called()

    def test_local_caps_lock_changes_once_per_press_and_survives_a_swallowed_press(self):
        native = {0x14: False, "down": set()}
        on_key = Mock(return_value=False)
        hooks = capture_win.Hooks(on_key, Mock(), Mock())
        for down in (True, True, True, False):
            self.assertEqual(self.key(hooks, 0x14, down, native), 99)
            self.assertEqual(hooks._state.caps_lock, native[0x14])
        on_key.return_value = True
        for down in (True, True, False):
            self.key(hooks, 0x14, down, native)
            self.assertEqual(hooks._state.caps_lock, native[0x14])
        on_key.return_value = False
        for down in (True, False):
            self.key(hooks, 0x14, down, native)
        self.assertFalse(hooks._state.caps_lock)
        self.assertFalse(native[0x14])


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
