import ctypes
import unittest
from unittest import mock

import input_injector
from input_injector import (
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    MOUSEEVENTF_HWHEEL,
    MOUSEEVENTF_WHEEL,
    WHEEL_UNITS_PER_PIXEL,
    plan_key_inputs,
    plan_scroll_units,
)


# Fake VkKeyScanW: maps a character to a 16-bit (shift_state << 8) | vk value,
# or -1 if the character can't be produced by the "layout" at all.
FAKE_VK_SCAN = {
    "c": 0x0043,  # plain, no shift needed
    "e": 0x0045,  # plain
    "C": 0x0143,  # shift + VK_C (upper-case via shift)
    "k": 0x004B,  # plain, no shift needed
    "K": 0x014B,  # shift + VK_K (upper-case via shift)
    "@": 0x0602,  # requires ctrl+alt (AltGr) bits -> unsupported combo
}

# Fake MapVirtualKeyW: vk -> hardware scan code.
FAKE_SCAN_CODES = {
    0x43: 0x2E,  # 'C' key
    0x45: 0x12,  # 'E' key
    0x4B: 0x25,  # 'K' key
    0x0D: 0x1C,  # enter
    0x26: 0x48,  # up arrow
    0xA3: 0x1D,  # ctrl_r
    0x5B: 0x5B,  # cmd (left win)
    0x08: 0x0E,  # backspace
    0x2E: 0x53,  # delete
}


def fake_vk_lookup(ch: str) -> int:
    return FAKE_VK_SCAN.get(ch, -1)


def fake_scan_lookup(vk: int) -> int:
    return FAKE_SCAN_CODES.get(vk, 0)


class BackspaceTests(unittest.TestCase):
    """Backspace is an ordinary named key under every modifier. It used to be
    remapped to Delete under Ctrl+Alt to stand in for Ctrl+Alt+Del; that went
    with the SAS feature, so Ctrl+Alt+Backspace now reaches Windows as the
    plain chord it is."""

    def plan(self, down, mods_down, char_vk_down):
        return plan_key_inputs(
            "backspace", down, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup
        )

    def test_plain_backspace_stays_backspace(self):
        char_vk_down = {}
        self.assertEqual(self.plan(True, set(), char_vk_down), [(0x08, 0x0E, 0)])
        self.assertEqual(
            self.plan(False, set(), char_vk_down), [(0x08, 0x0E, KEYEVENTF_KEYUP)]
        )

    def test_ctrl_only_backspace_stays_backspace(self):
        self.assertEqual(self.plan(True, {"ctrl"}, {}), [(0x08, 0x0E, 0)])

    def test_ctrl_alt_backspace_is_an_ordinary_chord(self):
        char_vk_down = {}
        for mods in ({"ctrl", "alt"}, {"ctrl_r", "alt_r"}):
            self.assertEqual(self.plan(True, mods, char_vk_down), [(0x08, 0x0E, 0)])
            self.assertEqual(
                self.plan(False, mods, char_vk_down), [(0x08, 0x0E, KEYEVENTF_KEYUP)]
            )
        self.assertEqual(char_vk_down, {})


class PlanKeyInputsChordTests(unittest.TestCase):
    def setUp(self):
        input_injector._warned_chars.clear()

    def test_ctrl_held_char_uses_vk_keystroke_and_mirrors_on_keyup(self):
        mods_down = {"ctrl"}
        char_vk_down = {}

        down_plan = plan_key_inputs("c", True, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)
        self.assertEqual(down_plan, [(0x43, 0x2E, 0)])
        self.assertEqual(char_vk_down, {"c": 0x43})

        up_plan = plan_key_inputs("c", False, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)
        self.assertEqual(up_plan, [(0x43, 0x2E, KEYEVENTF_KEYUP)])
        self.assertEqual(char_vk_down, {})

    def test_win_held_char_uses_vk_path(self):
        mods_down = {"cmd"}
        char_vk_down = {}

        down_plan = plan_key_inputs("e", True, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)
        self.assertEqual(down_plan, [(0x45, 0x12, 0)])
        self.assertEqual(char_vk_down, {"e": 0x45})

    def test_keyup_mirrors_vk_even_if_modifier_released_first(self):
        mods_down = {"ctrl"}
        char_vk_down = {}

        plan_key_inputs("c", True, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)
        self.assertEqual(char_vk_down, {"c": 0x43})

        # Modifier released before the char keyup arrives.
        plan_key_inputs("ctrl", False, mods_down, {}, fake_vk_lookup, fake_scan_lookup)
        self.assertNotIn("ctrl", mods_down)

        up_plan = plan_key_inputs("c", False, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)
        self.assertEqual(up_plan, [(0x43, 0x2E, KEYEVENTF_KEYUP)])
        self.assertEqual(char_vk_down, {})

    def test_no_modifier_and_unresolvable_char_uses_unicode_path(self):
        # 'a' has no entry in FAKE_VK_SCAN, standing in for a character this
        # "layout" can't resolve at all -- vk_lookup is still consulted (no
        # longer gated on a chord modifier being held), but comes back -1,
        # so this falls back to Unicode exactly like before.
        mods_down = set()
        char_vk_down = {}

        with self.assertLogs("input_injector", level="WARNING"):
            down_plan = plan_key_inputs("a", True, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)
        self.assertEqual(down_plan, [(0, ord("a"), KEYEVENTF_UNICODE)])

        up_plan = plan_key_inputs("a", False, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)
        self.assertEqual(up_plan, [(0, ord("a"), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)])
        self.assertEqual(char_vk_down, {})

    def test_altgr_char_falls_back_to_unicode(self):
        mods_down = {"ctrl"}
        char_vk_down = {}

        with self.assertLogs("input_injector", level="WARNING"):
            down_plan = plan_key_inputs("@", True, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)

        self.assertEqual(down_plan, [(0, ord("@"), KEYEVENTF_UNICODE)])
        self.assertEqual(char_vk_down, {})

    def test_unresolvable_char_falls_back_to_unicode(self):
        mods_down = {"alt"}
        char_vk_down = {}

        with self.assertLogs("input_injector", level="WARNING"):
            down_plan = plan_key_inputs("$", True, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)

        self.assertEqual(down_plan, [(0, ord("$"), KEYEVENTF_UNICODE)])
        self.assertEqual(char_vk_down, {})

    def test_shift_only_vk_scan_result_adds_no_synthetic_shift(self):
        # Ctrl+Shift+C: the chord modifier alone doesn't satisfy a
        # shift-only VkKeyScanW result -- shift must actually be held too
        # (it is here), and when it is, no extra synthetic shift keydown is
        # injected on top of the real VK+scan keystroke.
        mods_down = {"ctrl", "shift"}
        char_vk_down = {}

        down_plan = plan_key_inputs("C", True, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)
        self.assertEqual(down_plan, [(0x43, 0x2E, 0)])
        self.assertEqual(char_vk_down, {"C": 0x43})

    def test_shift_only_result_without_shift_held_falls_back_even_with_a_chord(self):
        # A chord modifier being held is not enough on its own: Ctrl+C
        # where "C" only resolves via a shift bit that isn't actually down
        # (e.g. caps-lock) must not be sent as the plain 'c' VK -- that
        # would silently turn Ctrl+Shift+C into Ctrl+C on Windows.
        mods_down = {"ctrl"}
        char_vk_down = {}

        down_plan = plan_key_inputs("C", True, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)
        self.assertEqual(down_plan, [(0, ord("C"), KEYEVENTF_UNICODE)])
        self.assertEqual(char_vk_down, {})


class PlanKeyInputsBareCharacterTests(unittest.TestCase):
    """Bug: a bare character (no chord modifier held) used to always go out
    as KEYEVENTF_UNICODE, which delivers text (WM_CHAR-style) but no usable
    keyCode -- apps and web pages listening for a real keydown (e.g.
    YouTube's 'k' to pause) never saw it, even though typing into a text
    field worked fine. The VK path is now preferred for any character the
    layout can resolve, chord or not; Unicode is only the fallback."""

    def setUp(self):
        input_injector._warned_chars.clear()

    def test_bare_character_uses_vk_path_with_scan_code(self):
        mods_down = set()
        char_vk_down = {}
        down_plan = plan_key_inputs("k", True, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)
        self.assertEqual(down_plan, [(0x4B, 0x25, 0)])
        self.assertEqual(char_vk_down, {"k": 0x4B})

    def test_bare_character_keyup_mirrors_the_recorded_vk(self):
        mods_down = set()
        char_vk_down = {}
        plan_key_inputs("k", True, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)
        up_plan = plan_key_inputs("k", False, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)
        self.assertEqual(up_plan, [(0x4B, 0x25, KEYEVENTF_KEYUP)])
        self.assertEqual(char_vk_down, {})

    def test_shift_required_and_held_uses_vk_path_with_no_synthetic_shift(self):
        mods_down = {"shift"}
        char_vk_down = {}
        down_plan = plan_key_inputs("K", True, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)
        # A single tuple only: the Mac is already forwarding shift as its
        # own physical keydown, so nothing synthetic is added here.
        self.assertEqual(down_plan, [(0x4B, 0x25, 0)])
        self.assertEqual(char_vk_down, {"K": 0x4B})

    def test_shift_r_also_satisfies_the_shift_only_requirement(self):
        mods_down = {"shift_r"}
        char_vk_down = {}
        down_plan = plan_key_inputs("K", True, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)
        self.assertEqual(down_plan, [(0x4B, 0x25, 0)])

    def test_shift_required_but_not_held_falls_back_to_unicode(self):
        # Caps-lock case: VkKeyScanW says shift is needed to produce 'K',
        # but no physical shift is currently down. Trusting the VK here
        # would desync from the real keyboard state, so this must stay
        # Unicode -- and silently, since it isn't a broken layout.
        mods_down = set()
        char_vk_down = {}
        down_plan = plan_key_inputs("K", True, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)
        self.assertEqual(down_plan, [(0, ord("K"), KEYEVENTF_UNICODE)])
        self.assertEqual(char_vk_down, {})

    def test_altgr_character_without_any_chord_falls_back_to_unicode(self):
        mods_down = set()
        char_vk_down = {}
        with self.assertLogs("input_injector", level="WARNING"):
            down_plan = plan_key_inputs("@", True, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)
        self.assertEqual(down_plan, [(0, ord("@"), KEYEVENTF_UNICODE)])
        self.assertEqual(char_vk_down, {})

    def test_unresolvable_character_without_any_chord_falls_back_to_unicode(self):
        mods_down = set()
        char_vk_down = {}
        with self.assertLogs("input_injector", level="WARNING"):
            down_plan = plan_key_inputs("$", True, mods_down, char_vk_down, fake_vk_lookup, fake_scan_lookup)
        self.assertEqual(down_plan, [(0, ord("$"), KEYEVENTF_UNICODE)])
        self.assertEqual(char_vk_down, {})


class PlanKeyInputsNamedKeyTests(unittest.TestCase):
    def test_named_keys_use_scan_lookup(self):
        plan = plan_key_inputs("enter", True, set(), {}, fake_vk_lookup, fake_scan_lookup)
        self.assertEqual(plan, [(0x0D, 0x1C, 0)])

    def test_extended_keys_carry_extended_flag(self):
        for name, vk in (("up", 0x26), ("ctrl_r", 0xA3), ("cmd", 0x5B)):
            with self.subTest(name=name):
                plan = plan_key_inputs(name, True, set(), {}, fake_vk_lookup, fake_scan_lookup)
                self.assertEqual(len(plan), 1)
                _, _, flags = plan[0]
                self.assertTrue(flags & KEYEVENTF_EXTENDEDKEY)

    def test_non_extended_named_key_has_no_extended_flag(self):
        plan = plan_key_inputs("ctrl", True, set(), {}, fake_vk_lookup, fake_scan_lookup)
        self.assertEqual(len(plan), 1)
        _, _, flags = plan[0]
        self.assertFalse(flags & KEYEVENTF_EXTENDEDKEY)

    def test_named_modifier_key_updates_mods_down(self):
        mods_down = set()
        plan_key_inputs("ctrl", True, mods_down, {}, fake_vk_lookup, fake_scan_lookup)
        self.assertIn("ctrl", mods_down)
        plan_key_inputs("ctrl", False, mods_down, {}, fake_vk_lookup, fake_scan_lookup)
        self.assertNotIn("ctrl", mods_down)

    def test_unknown_key_name_is_dropped(self):
        plan = plan_key_inputs("not_a_real_key", True, set(), {}, fake_vk_lookup, fake_scan_lookup)
        self.assertEqual(plan, [])

    def test_release_all_releases_a_held_named_non_modifier_key(self):
        sent = []
        input_injector._mods_down.clear()
        input_injector._char_vk_down.clear()
        input_injector._named_down.clear()
        input_injector._buttons_down.clear()
        with mock.patch.object(input_injector, "_vk_key_scan", lambda _ch: -1), \
             mock.patch.object(input_injector, "_map_virtual_key", lambda _vk: 0x48), \
             mock.patch.object(input_injector, "_send_input", lambda *items: sent.extend(items)):
            input_injector.inject_key("left", True)
            input_injector.release_all()
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0].union.ki.dwFlags, KEYEVENTF_EXTENDEDKEY)
        self.assertEqual(sent[1].union.ki.dwFlags, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP)


class PlanScrollUnitsLineModeTests(unittest.TestCase):
    def test_line_mode_scales_by_120_and_never_carries_a_remainder(self):
        accum = [0.0, 0.0]
        self.assertEqual(plan_scroll_units(1, 0, "line", accum), (120, 0))
        self.assertEqual(accum, [0.0, 0.0])
        self.assertEqual(plan_scroll_units(-2, 3, "line", accum), (-240, 360))
        self.assertEqual(accum, [0.0, 0.0])

    def test_line_mode_defaults_to_no_horizontal_movement(self):
        accum = [0.0, 0.0]
        self.assertEqual(plan_scroll_units(1, 0, "line", accum), (120, 0))


class PlanScrollUnitsPixelModeTests(unittest.TestCase):
    def test_pixel_mode_small_delta_does_not_flush_immediately(self):
        # WHEEL_UNITS_PER_PIXEL is 3.0 by default: 0.1px is 0.3 wheel units,
        # under a whole unit, so a single small delta must not flush yet.
        accum = [0.0, 0.0]
        self.assertEqual(plan_scroll_units(0.1, 0.0, "pixel", accum), (0, 0))
        self.assertAlmostEqual(accum[0], 0.3, places=6)

    def test_pixel_mode_flushes_once_enough_small_deltas_accumulate(self):
        accum = [0.0, 0.0]
        for _ in range(3):
            plan_scroll_units(0.1, 0.0, "pixel", accum)
        # 0.3 units/call * 4 calls = 1.2 -> flush 1, carry 0.2.
        whole_y, whole_x = plan_scroll_units(0.1, 0.0, "pixel", accum)
        self.assertEqual((whole_y, whole_x), (1, 0))
        self.assertAlmostEqual(accum[0], 0.2, places=6)

    def test_pixel_mode_carries_residue_across_multiple_small_deltas(self):
        accum = [0.0, 0.0]
        # 0.4px * 3.0 units/px = 1.2 units per call.
        self.assertEqual(plan_scroll_units(0.4, 0.0, "pixel", accum), (1, 0))
        self.assertAlmostEqual(accum[0], 0.2, places=6)
        # 0.2 + 1.2 = 1.4 -> flush 1, carry 0.4.
        self.assertEqual(plan_scroll_units(0.4, 0.0, "pixel", accum), (1, 0))
        self.assertAlmostEqual(accum[0], 0.4, places=6)

    def test_pixel_mode_tracks_horizontal_axis_independently(self):
        accum = [0.0, 0.0]
        whole_y, whole_x = plan_scroll_units(0.0, 0.4, "pixel", accum)
        self.assertEqual((whole_y, whole_x), (0, 1))
        self.assertAlmostEqual(accum[0], 0.0)
        self.assertAlmostEqual(accum[1], 0.2, places=6)

    def test_pixel_mode_negative_deltas_carry_a_negative_remainder(self):
        accum = [0.0, 0.0]
        whole_y, _ = plan_scroll_units(-0.1, 0.0, "pixel", accum)
        self.assertEqual(whole_y, 0)
        self.assertAlmostEqual(accum[0], -0.1 * WHEEL_UNITS_PER_PIXEL, places=6)


class InjectScrollDispatchTests(unittest.TestCase):
    """inject_scroll's real _send_input call requires Win32 SendInput, which
    only exists on Windows. These tests fake that boundary (patch
    _send_input) so the dispatch logic -- which flags to use, whether an
    axis with nothing to flush is skipped -- can be verified on any
    platform, matching how plan_key_inputs/inject_key are already split for
    testability.
    """

    def setUp(self):
        # inject_scroll accumulates in module-level state; reset it so tests
        # don't leak residue into each other.
        input_injector._scroll_accum[:] = [0.0, 0.0]

    def test_line_mode_sends_wheel_and_hwheel_with_expected_flags(self):
        with mock.patch.object(input_injector, "_send_input") as fake_send:
            input_injector.inject_scroll(1, -2, "line")
        calls = [call.args[0] for call in fake_send.call_args_list]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].union.mi.mouseData, 120)
        self.assertEqual(calls[0].union.mi.dwFlags, MOUSEEVENTF_WHEEL)
        self.assertEqual(calls[1].union.mi.mouseData, ctypes.c_ulong(-240).value)
        self.assertEqual(calls[1].union.mi.dwFlags, MOUSEEVENTF_HWHEEL)

    def test_zero_delta_axis_sends_nothing(self):
        with mock.patch.object(input_injector, "_send_input") as fake_send:
            input_injector.inject_scroll(1, 0, "line")
        calls = [call.args[0] for call in fake_send.call_args_list]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].union.mi.dwFlags, MOUSEEVENTF_WHEEL)

    def test_pixel_mode_small_delta_flushes_nothing_yet(self):
        with mock.patch.object(input_injector, "_send_input") as fake_send:
            input_injector.inject_scroll(0.1, 0.0, "pixel")
        fake_send.assert_not_called()
        self.assertAlmostEqual(input_injector._scroll_accum[0], 0.1 * WHEEL_UNITS_PER_PIXEL, places=6)


class InjectSideButtonTests(unittest.TestCase):
    def tearDown(self):
        input_injector._buttons_down.clear()

    def test_back_and_forward_are_x_buttons_one_and_two(self):
        with mock.patch.object(input_injector, "_send_input") as fake_send:
            input_injector.inject_mouse_button("back", True)
            input_injector.inject_mouse_button("forward", False)
        sent = [call.args[0].union.mi for call in fake_send.call_args_list]
        self.assertEqual((sent[0].dwFlags, sent[0].mouseData), (input_injector.MOUSEEVENTF_XDOWN, 1))
        self.assertEqual((sent[1].dwFlags, sent[1].mouseData), (input_injector.MOUSEEVENTF_XUP, 2))



class FractionalWheelTests(unittest.TestCase):
    def test_half_clicks_add_up_to_whole_ones(self):
        accum = [0.0, 0.0]
        units = [input_injector.plan_scroll_units(0.5, 0.0, "line", accum)[0] for _ in range(4)]
        self.assertEqual(sum(units), 240)
        self.assertEqual(input_injector.plan_scroll_units(2, 0, "line", [0.0, 0.0]), (240, 0))


if __name__ == "__main__":
    unittest.main()


class InjectGestureTests(unittest.TestCase):
    def setUp(self):
        self.pressed = []
        patcher = mock.patch.object(
            input_injector, "inject_key", side_effect=lambda key, down: self.pressed.append((key, down))
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_touchpad_swipe_is_preferred_and_ends_the_gesture(self):
        swipes = []
        input_injector.inject_gesture("swipe_up", swipe=lambda fingers, direction: swipes.append((fingers, direction)) or True)
        self.assertEqual(swipes, [(3, "up")])
        self.assertEqual(self.pressed, [])

    def test_refused_swipe_falls_back_to_the_shortcut_chord(self):
        input_injector.inject_gesture("swipe_left", swipe=lambda fingers, direction: False)
        self.assertEqual(
            self.pressed,
            [("cmd", True), ("ctrl", True), ("right", True), ("right", False), ("ctrl", False), ("cmd", False)],
        )

    def test_spread_and_swipe_down_both_show_the_desktop(self):
        for name in ("spread", "swipe_down"):
            swipes = []
            input_injector.inject_gesture(name, swipe=lambda fingers, direction: swipes.append((fingers, direction)) or True)
            self.assertEqual(swipes, [(3, "down")], name)

    def test_pinch_has_no_touchpad_gesture_and_opens_start(self):
        input_injector.inject_gesture("pinch", swipe=lambda fingers, direction: self.fail("no swipe expected"))
        self.assertEqual(self.pressed, [("cmd", True), ("cmd", False)])

    def test_unknown_gesture_does_nothing(self):
        input_injector.inject_gesture("wiggle", swipe=lambda fingers, direction: self.fail("no swipe expected"))
        self.assertEqual(self.pressed, [])


class TouchpadContactFramesTests(unittest.TestCase):
    def test_fingers_move_together_by_the_axis_distance_and_stay_on_the_pad(self):
        import touchpad_injector

        frames = touchpad_injector.contact_frames(3, "up")
        self.assertEqual(len(frames), touchpad_injector.SWIPE_STEPS + 1)
        first, last = frames[0], frames[-1]
        for (x0, y0), (x1, y1) in zip(first, last):
            self.assertEqual(x1, x0)
            self.assertEqual(y0 - y1, 3000)
        for frame in frames:
            for x, y in frame:
                self.assertTrue(0 <= x <= touchpad_injector.PAD_WIDTH)
                self.assertTrue(0 <= y <= touchpad_injector.PAD_HEIGHT)

    def test_four_fingers_sideways_keep_their_spacing(self):
        import touchpad_injector

        frames = touchpad_injector.contact_frames(4, "left")
        xs = [x for x, _ in frames[-1]]
        self.assertEqual([b - a for a, b in zip(xs, xs[1:])], [touchpad_injector.FINGER_SPACING] * 3)


class InjectedMarkTests(unittest.TestCase):
    """Every INPUT carries the mark. The raw-input reader in capture_win
    reads it back out of ulExtraInformation to tell the Mac's pointer from
    the hand; nothing else in Windows does."""

    def test_a_mouse_input_is_stamped(self):
        item = input_injector._mouse_input(5, 0, 0, input_injector.MOUSEEVENTF_MOVE)
        self.assertEqual(item.union.mi.dwExtraInfo, input_injector.INJECTED_MARK)

    def test_a_key_input_is_stamped(self):
        item = input_injector._keybd_input(0x41, 0x1E, 0)
        self.assertEqual(item.union.ki.dwExtraInfo, input_injector.INJECTED_MARK)


class InjectTextTests(unittest.TestCase):
    """WIRE.md section 10: a `text` message is typed as characters, never as keys."""

    def test_each_character_is_a_unicode_down_and_up_pair(self):
        plan = input_injector.plan_text_inputs("aé")
        down, up = KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
        self.assertEqual(plan, [(0, ord("a"), down), (0, ord("a"), up), (0, ord("é"), down), (0, ord("é"), up)])

    def test_a_character_outside_the_bmp_goes_as_its_surrogate_pair(self):
        plan = input_injector.plan_text_inputs("\U0001F600")
        self.assertEqual([scan for _vk, scan, _flags in plan], [0xD83D, 0xD83D, 0xDE00, 0xDE00])

    def test_injecting_text_sends_one_batch_stamped_with_the_mark(self):
        sent = []
        with mock.patch.object(input_injector, "_send_input", lambda *items: sent.extend(items)):
            input_injector.inject_text("hi")
        self.assertEqual(len(sent), 4)
        self.assertTrue(all(item.union.ki.dwExtraInfo == input_injector.INJECTED_MARK for item in sent))
        self.assertEqual([item.union.ki.wScan for item in sent], [ord("h"), ord("h"), ord("i"), ord("i")])

    def test_empty_text_sends_nothing(self):
        with mock.patch.object(input_injector, "_send_input", lambda *items: self.fail("nothing to send")):
            input_injector.inject_text("")
