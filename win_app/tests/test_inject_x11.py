"""inject_x11's planning on a fake layout, and its functions against a fake connection.
tools/x11_proof.sh types through the same module on a real X server and reads it back."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

import capture_x11  # noqa: E402
import inject_x11  # noqa: E402
from inject_x11 import MAP, NO_REPEAT, PRESS, RELEASE, REPEAT, KeyState, Layout  # noqa: E402

U = 0x01000000
SHIFT_L, SHIFT_R, CTRL_L = 50, 62, 37
A, ONE, C, E, SEVEN = 38, 10, 54, 26, 16
CAPS = capture_x11.LOCK_MASK


class FakeKeymap:
    """A US layout in group one and Russian in group two, the 7 key typing / with Shift as on a
    German keyboard, and two empty keycodes. Shift picks level 2, Caps Lock flips a letter."""

    min_keycode, max_keycode = 8, 202

    def __init__(self):
        self.rows = {
            A: [[ord("a"), ord("A")], [U | 0x444, U | 0x424]],  # ф Ф
            C: [[ord("c"), ord("C")], [U | 0x441, U | 0x421]],  # с С
            E: [[ord("e"), ord("E")], [U | 0x443, U | 0x423]],  # у У
            ONE: [[ord("1"), ord("!")]],
            SEVEN: [[ord("7"), ord("/")]],
            SHIFT_L: [[0xFFE1, 0xFFE1]],
        }
        for keycode in range(self.min_keycode, self.max_keycode + 1):
            if keycode not in self.rows and keycode not in (200, 201):
                self.rows[keycode] = [[0xFF00 + keycode, 0xFF00 + keycode]]  # something with no text

    def keysym(self, keycode, mods, group):
        groups = self.rows.get(keycode)
        if not groups:
            return 0
        levels = groups[group % len(groups)]
        level = 1 if mods & capture_x11.SHIFT_MASK else 0
        if mods & CAPS and (capture_x11.keysym_text(levels[1]) or "").isalpha():
            level ^= 1
        return levels[level]

    def text(self, keysym):
        return capture_x11.keysym_text(keysym) if keysym < 0xFF00 or keysym > 0xFFFF else None

    def empty(self, keycode):
        return keycode not in self.rows


def plan(name, down=True, us=None, state=None, layout=None, group=0, locks=0):
    state = state if state is not None else KeyState()
    return inject_x11.plan_key(name, down, us, state, layout or Layout(FakeKeymap()), group, locks)


class NamedKeyTest(unittest.TestCase):
    def test_named_keys_by_evdev_code(self):
        self.assertEqual(plan("enter"), [(PRESS, 36)])
        self.assertEqual(plan("return", down=False), [(RELEASE, 36)])
        self.assertEqual(plan("cmd"), [(PRESS, 133)])  # Super
        self.assertEqual(plan("ctrl_r"), [(PRESS, 105)])
        self.assertEqual(plan("alt_r"), [(PRESS, 108)])
        self.assertEqual(plan("f24"), [(PRESS, 202)])
        self.assertEqual(plan("media_play_pause"), [(PRESS, 172)])

    def test_the_server_repeats_a_held_named_key(self):
        state = KeyState()
        self.assertEqual(plan("backspace", state=state), [(PRESS, 22)])
        self.assertEqual(plan("backspace", state=state), [])
        self.assertEqual(plan("backspace", down=False, state=state), [(RELEASE, 22)])
        self.assertEqual(plan("backspace", state=state), [(PRESS, 22)])

    def test_modifiers_are_tracked(self):
        state = KeyState()
        plan("ctrl", state=state)
        plan("shift_r", state=state)
        self.assertEqual(state.mods_down, {"ctrl", "shift_r"})
        plan("ctrl", down=False, state=state)
        self.assertEqual(state.mods_down, {"shift_r"})

    def test_an_unknown_name_is_dropped(self):
        with self.assertLogs(inject_x11.LOGGER, "WARNING"):
            self.assertEqual(plan("hyper_q"), [])


class CharacterTest(unittest.TestCase):
    def test_a_plain_character_and_the_server_s_repeat(self):
        state = KeyState()
        self.assertEqual(plan("a", state=state), [(PRESS, A)])
        self.assertEqual(plan("a", state=state), [])  # the peer's repeat: the server is repeating it
        self.assertEqual(plan("a", down=False, state=state), [(RELEASE, A)])
        self.assertEqual(state.chars_down, {})

    def test_shift_is_pressed_around_a_character_that_needs_it(self):
        state = KeyState()
        self.assertEqual(plan("!", state=state), [(NO_REPEAT, ONE), (PRESS, SHIFT_L), (PRESS, ONE), (RELEASE, SHIFT_L)])
        # Each of the peer's repeats is typed again with its Shift, the server's being off for it.
        self.assertEqual(plan("!", state=state), [(RELEASE, ONE), (PRESS, SHIFT_L), (PRESS, ONE), (RELEASE, SHIFT_L)])
        self.assertEqual(plan("!", down=False, state=state), [(RELEASE, ONE), (REPEAT, ONE)])

    def test_a_german_slash_repeats_as_a_slash(self):
        state = KeyState()
        plan("/", state=state)
        self.assertEqual(plan("/", state=state), [(RELEASE, SEVEN), (PRESS, SHIFT_L), (PRESS, SEVEN), (RELEASE, SHIFT_L)])

    def test_a_repeat_works_shift_out_again_when_the_peer_s_shift_changed(self):
        state = KeyState()
        plan("shift", state=state)
        self.assertEqual(plan("1", state=state), [(NO_REPEAT, ONE), (RELEASE, SHIFT_L), (PRESS, ONE), (PRESS, SHIFT_L)])
        plan("shift", down=False, state=state)
        self.assertEqual(plan("1", state=state), [(RELEASE, ONE), (PRESS, ONE)])  # no Shift pressed for nobody

    def test_a_held_shift_is_let_go_around_a_character_that_must_not_have_it(self):
        state = KeyState()
        plan("shift_r", state=state)
        self.assertEqual(plan("1", state=state), [(NO_REPEAT, ONE), (RELEASE, SHIFT_R), (PRESS, ONE), (PRESS, SHIFT_R)])
        self.assertEqual(plan("A", state=state), [(PRESS, A)])

    def test_caps_lock_as_the_layout_applies_it(self):
        self.assertEqual(plan("A", locks=CAPS), [(PRESS, A)])
        self.assertEqual(plan("a", locks=CAPS), [(NO_REPEAT, A), (PRESS, SHIFT_L), (PRESS, A), (RELEASE, SHIFT_L)])
        self.assertEqual(plan("!", locks=CAPS), [(NO_REPEAT, ONE), (PRESS, SHIFT_L), (PRESS, ONE), (RELEASE, SHIFT_L)])

    def test_under_a_chord_shift_is_the_peer_s(self):
        state = KeyState()
        plan("ctrl", state=state)
        plan("shift", state=state)
        self.assertEqual(plan("a", state=state), [(PRESS, A)])

    def test_the_current_group(self):
        self.assertEqual(plan("ф", group=1), [(PRESS, A)])
        self.assertEqual(plan("Ф", group=1), [(NO_REPEAT, A), (PRESS, SHIFT_L), (PRESS, A), (RELEASE, SHIFT_L)])
        self.assertEqual(plan("1", group=1), [(PRESS, ONE)])  # a key without the group wraps
        self.assertEqual(plan("ф", group=3), [(PRESS, A)])  # a fourth group, wrapped as XKB wraps it

    def test_a_repeat_and_its_release_stay_on_the_first_key(self):
        state = KeyState()
        layout = Layout(FakeKeymap())
        plan("a", state=state, layout=layout)
        self.assertEqual(plan("a", state=state, layout=layout, group=1), [])
        self.assertEqual(plan("a", down=False, state=state, layout=layout, group=1), [(RELEASE, A)])

    def test_by_place_under_a_chord(self):
        state = KeyState()
        plan("ctrl", state=state)
        # Ctrl-C from an English Mac to Russian: no key types "c", so the key in C's place.
        self.assertEqual(plan("c", us="c", state=state, group=1), [(PRESS, C)])

    def test_by_place_for_another_script_s_letter(self):
        # A Russian Mac types "у" on its E key; on this English layout that is the E key too.
        self.assertEqual(plan("у", us="e"), [(PRESS, E)])

    def test_a_character_on_no_key_gets_a_spare_that_stays_mapped(self):
        state = KeyState()
        layout = Layout(FakeKeymap())
        self.assertEqual(plan("é", state=state, layout=layout), [(MAP, 201, 0xE9, False), (PRESS, 201)])
        plan("é", down=False, state=state, layout=layout)
        self.assertEqual(plan("é", state=state, layout=layout), [(PRESS, 201)])
        self.assertEqual(plan("€", state=state, layout=layout), [(MAP, 200, U | 0x20AC, False), (PRESS, 200)])

    def test_a_spare_wiped_by_a_new_keymap_is_mapped_again(self):
        state = KeyState()
        plan("é", state=state, layout=Layout(FakeKeymap()))
        plan("é", down=False, state=state, layout=Layout(FakeKeymap()))
        self.assertEqual(plan("é", state=state, layout=Layout(FakeKeymap())), [(MAP, 201, 0xE9, False), (PRESS, 201)])

    def test_the_least_recent_free_spare_is_reused_after_a_pause(self):
        state = KeyState()
        layout = Layout(FakeKeymap())
        plan("é", state=state, layout=layout)
        plan("é", down=False, state=state, layout=layout)
        plan("€", state=state, layout=layout)  # held
        self.assertEqual(plan("ß", state=state, layout=layout), [(MAP, 201, 0xDF, True), (PRESS, 201)])
        with self.assertLogs(inject_x11.LOGGER, "WARNING"):
            self.assertEqual(plan("ñ", state=state, layout=layout), [])  # both spares held


class TextTest(unittest.TestCase):
    def test_text_lets_held_modifiers_go_and_presses_them_again(self):
        state = KeyState()
        plan("ctrl", state=state)
        plan("shift", state=state)
        steps = inject_x11.plan_text("a!", state, Layout(FakeKeymap()), 0, 0)
        self.assertEqual(steps, [
            (RELEASE, CTRL_L), (RELEASE, SHIFT_L),
            (PRESS, A), (RELEASE, A),
            (PRESS, SHIFT_L), (PRESS, ONE), (RELEASE, SHIFT_L), (RELEASE, ONE),
            (PRESS, CTRL_L), (PRESS, SHIFT_L),
        ])
        self.assertEqual(state.mods_down, {"ctrl", "shift"})

    def test_text_off_the_layout_uses_a_spare(self):
        self.assertEqual(inject_x11.plan_text("é", KeyState(), Layout(FakeKeymap()), 0, 0),
                         [(MAP, 201, 0xE9, False), (PRESS, 201), (RELEASE, 201)])


class ScrollTest(unittest.TestCase):
    def test_line_clicks(self):
        carry = [0.0, 0.0]
        self.assertEqual(inject_x11.plan_scroll(2, -1, "line", carry), (2, -1))
        self.assertEqual(inject_x11.scroll_buttons(2, -1), [4, 4, 6])
        self.assertEqual(inject_x11.scroll_buttons(-1, 1), [5, 7])

    def test_pixels_carry_their_remainder(self):
        carry = [0.0, 0.0]
        self.assertEqual(inject_x11.plan_scroll(30, 0, "pixel", carry), (0, 0))
        self.assertEqual(inject_x11.plan_scroll(30, 0, "pixel", carry), (1, 0))
        self.assertAlmostEqual(carry[0], 0.5)

    def test_a_fractional_line_carries(self):
        carry = [0.0, 0.0]
        self.assertEqual(inject_x11.plan_scroll(0.5, 0, "line", carry), (0, 0))
        self.assertEqual(inject_x11.plan_scroll(0.5, 0, "line", carry), (1, 0))


class FakeConnection:
    def __init__(self):
        self.sent = []
        self.changed = False
        self.group, self.locks = 0, 0
        self.closed = False
        self.fail = None
        self.repeat_restored = 0

    def layout(self):
        return Layout(FakeKeymap())

    def mapping_changed(self):
        changed, self.changed = self.changed, False
        return changed

    def state(self):
        return self.group, self.locks

    def run(self, steps):
        if self.fail is not None:
            raise self.fail
        self.sent.extend(steps)

    def restore_repeat(self):
        self.repeat_restored += 1

    def button(self, number, down):
        self.sent.append(("button", number, down))

    def move(self, dx, dy):
        self.sent.append(("move", dx, dy))

    def move_to(self, x, y):
        self.sent.append(("move_to", x, y))

    def close(self):
        self.closed = True


class ModuleTest(unittest.TestCase):
    def setUp(self):
        self.connection = FakeConnection()
        self.made = 0

        def connect():
            self.made += 1
            return self.connection

        self.real = inject_x11._connect
        inject_x11._connect = connect
        inject_x11._drop()
        inject_x11._state.__init__()
        inject_x11._buttons_down.clear()
        inject_x11._scroll_carry[:] = [0.0, 0.0]

    def tearDown(self):
        inject_x11._drop()
        inject_x11._connect = self.real

    def test_keys_buttons_moves_and_scrolls_reach_the_connection(self):
        inject_x11.inject_key("ctrl", True)
        inject_x11.inject_key("c", True, us="c")
        inject_x11.inject_mouse_button("back", True)
        inject_x11.inject_mouse_move(3, -2)
        inject_x11.inject_scroll(-1, 0, "line")
        self.assertEqual(self.connection.sent, [
            (PRESS, CTRL_L), (PRESS, C), ("button", 8, True), ("move", 3, -2), ("button", 5, True), ("button", 5, False),
        ])
        self.assertEqual(self.made, 1)

    def test_the_layout_is_read_again_after_a_mapping_change(self):
        self.connection.group = 1
        inject_x11.inject_key("ф", True)
        layout = inject_x11._layout
        inject_x11.inject_key("ф", False)
        self.assertIs(inject_x11._layout, layout)
        self.connection.changed = True
        inject_x11.inject_key("a", True)
        self.assertIsNot(inject_x11._layout, layout)

    def test_release_all_lets_go_of_everything_held(self):
        inject_x11.inject_key("shift", True)
        inject_x11.inject_key("A", True)
        inject_x11.inject_key("backspace", True)
        inject_x11.inject_mouse_button("left", True)
        inject_x11.inject_scroll(0.5, 0, "line")
        self.connection.sent.clear()
        inject_x11.release_all()
        self.assertEqual(self.connection.sent, [(RELEASE, SHIFT_L), (RELEASE, 22), (RELEASE, A), ("button", 1, False)])
        self.assertEqual(self.connection.repeat_restored, 1)
        state = inject_x11._state
        self.assertEqual((state.mods_down, state.named_down, state.chars_down, inject_x11._buttons_down), (set(), set(), {}, set()))
        self.assertEqual(inject_x11._scroll_carry, [0.0, 0.0])

    def test_a_failed_request_reconnects_next_time(self):
        self.connection.fail = ConnectionError("gone")
        with self.assertRaises(ConnectionError):
            inject_x11.inject_key("a", True)
        self.assertTrue(self.connection.closed)
        self.connection.fail = None
        inject_x11.inject_key("enter", True)
        self.assertEqual(self.made, 2)

    def test_a_gesture_is_dropped_with_one_line(self):
        with self.assertLogs(inject_x11.LOGGER, "WARNING") as logs:
            inject_x11.inject_gesture("swipe_left_once_test")
            inject_x11.inject_gesture("swipe_left_once_test")
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(self.connection.sent, [])


if __name__ == "__main__":
    unittest.main()
