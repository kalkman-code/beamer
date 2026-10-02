"""capture_x11 against a fake X connection: names, the raw-event parser, XTEST and absolute
filtering, buttons and the wheel as Windows messages, the grab taken and let go as the predicate
says, which side of a grab an event came from, events handed back played here, and the safety net
under a stuck thread. tools/x11_proof.sh runs the same module against a real X server."""

import struct
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

import capture_x11  # noqa: E402
from capture_x11 import Device  # noqa: E402

POINTER = Device(2, capture_x11.XI_MASTER_POINTER, "Virtual core pointer")
KEYBOARD = Device(3, capture_x11.XI_MASTER_KEYBOARD, "Virtual core keyboard")
XTEST_POINTER = Device(4, capture_x11.XI_SLAVE_POINTER, "Virtual core XTEST pointer")
XTEST_KEYBOARD = Device(5, 4, "Virtual core XTEST keyboard")
MOUSE = Device(6, capture_x11.XI_SLAVE_POINTER, "Logitech USB Receiver Mouse", scrolls={2: (False, 15.0), 3: (True, 15.0)})
TABLET = Device(7, capture_x11.XI_SLAVE_POINTER, "Wacom Intuos Pen", absolute=True)
KEYS = Device(8, 4, "AT Translated Set 2 keyboard")

U = 0x01000000
# keycode to its groups, each [level 1, level 2]
KEYMAP = {
    38: [[ord("a"), ord("A")], [U | 0x444, U | 0x424], [ord("a"), ord("A")], [U | 0x3B1, U | 0x391]],  # a; ф; a; α
    10: [[ord("1"), ord("!")]],
    48: [[ord("'"), ord('"')]],
    87: [[0xFF9C, 0xFFB1]],  # KP_End, KP_1
    65: [[ord(" "), ord(" ")]],
    34: [[0xFE52, 0xFE52]],  # dead circumflex
}


class FakeKeymap:
    """XKB as far as these tests need it: Shift picks level 2, Caps Lock flips a letter, Num Lock
    flips a keypad key, and a group the key does not have wraps."""

    min_keycode, max_keycode = 8, 255

    def keysym(self, keycode, mods, group):
        groups = KEYMAP.get(keycode)
        if not groups:
            return 0
        levels = groups[group % len(groups)]
        level = 1 if mods & capture_x11.SHIFT_MASK else 0
        upper = capture_x11.keysym_text(levels[1]) or ""
        if mods & capture_x11.LOCK_MASK and upper.isalpha():
            level ^= 1
        if mods & capture_x11.NUM_LOCK_MASK and 0xFFAA <= levels[1] <= 0xFFB9:
            level ^= 1
        return levels[level]

    def text(self, keysym):
        return capture_x11.keysym_text(keysym)

    def empty(self, keycode):
        return keycode not in KEYMAP

    def reload(self):
        pass


def raw_bytes(detail=0, sourceid=6, flags=0, raw=None, deviceid=2):
    """An XI2 raw event's bytes after its first ten, as python-xlib hands them over."""
    raw = raw or {}
    numbers = sorted(raw)
    mask_len = 1 if not numbers else max(numbers) // 32 + 1
    mask = bytearray(mask_len * 4)
    for number in numbers:
        mask[number // 8] |= 1 << (number % 8)
    data = struct.pack("=HIIHHI4x", deviceid, 1234, detail, sourceid, mask_len, flags) + bytes(mask)
    for number in numbers:  # accelerated, then raw: the accelerated ones doubled so a mix-up shows
        value = raw[number] * 2
        data += struct.pack("=iI", int(value // 1), int((value % 1) * 2 ** 32))
    for number in numbers:
        value = raw[number]
        data += struct.pack("=iI", int(value // 1), int((value % 1) * 2 ** 32))
    return data


class FakeConnection:
    def __init__(self, devices=None):
        self.device_list = devices or [POINTER, KEYBOARD, XTEST_POINTER, XTEST_KEYBOARD, MOUSE, TABLET, KEYS]
        self.queue = []
        self.grabs = []
        self.refuse = set()
        self.calls = []
        self.mods, self.group = 0, 0
        self.mapping = list(range(1, 11))
        self.closed = False
        self.aborted = False
        self.raise_in_events = None
        self.keymap = FakeKeymap()
        self.serial = 100
        self.lock = threading.Lock()

    def next_serial(self):
        self.serial += 1
        return self.serial

    def events(self, timeout):
        if self.aborted:
            raise ConnectionError("aborted")
        if self.raise_in_events is not None:
            raise self.raise_in_events
        with self.lock:
            found, self.queue = self.queue, []
        if not found:
            time.sleep(timeout / 10)
        return found

    def push(self, *events):
        with self.lock:
            self.queue.extend(events)

    def devices(self):
        return list(self.device_list)

    def grab(self, device):
        self.calls.append(("grab", device.deviceid))
        if device.deviceid in self.refuse:
            return None
        self.grabs.append(device.deviceid)
        return self.next_serial()

    def ungrab(self, device):
        self.calls.append(("ungrab", device.deviceid, self.held_while_ungrabbing()))
        self.grabs.remove(device.deviceid)
        return self.next_serial()

    def fake_key(self, keycode, down):
        self.calls.append(("fake_key", keycode, down, tuple(self.grabs)))

    def fake_button(self, button, down):
        self.calls.append(("fake_button", button, down, tuple(self.grabs)))

    def pointer_mapping(self):
        return list(self.mapping)

    def state(self):
        self.calls.append(("state",))
        return self.mods, self.group

    def abort(self):
        self.aborted = True

    held_while_ungrabbing = staticmethod(lambda: None)

    def close(self):
        self.closed = True


def raw(evtype, detail=0, sourceid=6, values=None, flags=0, sequence=0, master=None):
    if master is None:
        master = 3 if evtype in (capture_x11.XI_RAW_KEY_PRESS, capture_x11.XI_RAW_KEY_RELEASE) else 2
    _deviceid, detail, sourceid, flags, parsed = capture_x11.parse_raw(raw_bytes(detail, sourceid, flags, values))
    return ("raw", evtype, detail, sourceid, flags, parsed, master, sequence)


def key(down, keycode, mods=0, group=0, sourceid=8, flags=0, sequence=0):
    return ("key", down, keycode, sourceid, flags, mods, group, 3, sequence)


class Recorder:
    def __init__(self):
        self.keys, self.mice, self.motions = [], [], []
        self.answer = True

    def on_key(self, name, down, vk, us):
        self.keys.append((name, down, vk, us))
        return self.answer

    def on_mouse(self, message, x, y, data):
        self.mice.append((message, data))
        return self.answer

    def on_motion(self, dx, dy):
        self.motions.append((dx, dy))


class Harness(unittest.TestCase):
    """Drives Hooks' handlers directly on a fake connection, with no thread."""

    def setUp(self):
        self.connection = FakeConnection()
        self.seen = Recorder()
        self.hooks = capture_x11.Hooks(self.seen.on_key, self.seen.on_mouse, self.seen.on_motion, connect=lambda: self.connection)
        self.hooks._connection = self.connection
        self.hooks._read_devices()
        self.hooks._buttons = self.connection.pointer_mapping()

    def feed(self, *events):
        for event in events:
            self.hooks._handle(event)

    def messages(self):
        return [capture_x11.mouse_event(message, data) for message, data in self.seen.mice]


class ParseRawTest(unittest.TestCase):
    def test_reads_the_raw_values_not_the_accelerated_ones(self):
        deviceid, detail, sourceid, flags, values = capture_x11.parse_raw(raw_bytes(0, 6, 0, {0: 3.5, 1: -2.25}))
        self.assertEqual((deviceid, detail, sourceid, flags), (2, 0, 6, 0))
        self.assertEqual(values, {0: 3.5, 1: -2.25})

    def test_reads_a_sparse_mask(self):
        self.assertEqual(capture_x11.parse_raw(raw_bytes(raw={1: 4.0, 3: -1.0}))[4], {1: 4.0, 3: -1.0})

    def test_a_key_event_has_no_valuators(self):
        self.assertEqual(capture_x11.parse_raw(raw_bytes(detail=38, sourceid=8))[1:], (38, 8, 0, {}))


class NamesTest(unittest.TestCase):
    def test_modifiers_are_named_as_windows_names_them(self):
        names = capture_x11.VK_TO_NAME
        self.assertEqual((names[29], names[97]), ("cmd", "cmd_r"))  # Ctrl is "cmd", as on Windows
        self.assertEqual((names[125], names[126]), ("ctrl", "ctrl_r"))  # Super is "ctrl"
        self.assertEqual((names[56], names[100], names[42], names[54]), ("alt", "alt_r", "shift", "shift_r"))
        self.assertEqual((names[1], names[28], names[96], names[59], names[88], names[194]), ("esc", "enter", "enter", "f1", "f12", "f24"))

    def test_the_trigger_names_and_titles_cover_the_default(self):
        self.assertEqual(capture_x11.VK_TITLES[97], "Right Ctrl")
        self.assertEqual(capture_x11.VK_TITLES[125], "Left Super")
        self.assertNotIn(163, set(capture_x11.VK_TO_NAME) - capture_x11.NOT_TRIGGER_VKS)  # media keys are not triggers
        self.assertIn(57, capture_x11.UNRECORDABLE_TRIGGER_VKS)

    def test_hook_vk_is_the_keycode_less_eight(self):
        self.assertEqual(capture_x11.hook_vk(0xFFE4, 105), 97)
        self.assertEqual(capture_x11.hook_vk(0, 0), 0)

    def test_input_titles(self):
        self.assertEqual(capture_x11.input_title("key:97"), "Right Ctrl")
        self.assertEqual(capture_x11.input_title("key:30"), "A")
        self.assertEqual(capture_x11.input_title("key:240"), "Key 240")
        self.assertEqual(capture_x11.input_title("button:back"), "Back button")

    def test_keysym_text(self):
        self.assertEqual(capture_x11.keysym_text(ord("a")), "a")
        self.assertEqual(capture_x11.keysym_text(0xE9), "é")
        self.assertEqual(capture_x11.keysym_text(0x010020AC), "€")
        self.assertIsNone(capture_x11.keysym_text(0))


class CharacterTest(unittest.TestCase):
    def char(self, keycode, mods=0, group=0):
        return capture_x11.character(keycode, mods, group, FakeKeymap())

    def test_shift_and_caps_lock(self):
        self.assertEqual(self.char(38), "a")
        self.assertEqual(self.char(38, capture_x11.SHIFT_MASK), "A")
        self.assertEqual(self.char(38, capture_x11.LOCK_MASK), "A")
        self.assertEqual(self.char(38, capture_x11.LOCK_MASK | capture_x11.SHIFT_MASK), "a")
        self.assertEqual(self.char(10, capture_x11.SHIFT_MASK), "!")
        self.assertEqual(self.char(10, capture_x11.LOCK_MASK), "1")

    def test_a_chord_sends_the_plain_key(self):
        self.assertEqual(self.char(38, capture_x11.CONTROL_MASK | capture_x11.SHIFT_MASK), "a")
        self.assertEqual(self.char(38, capture_x11.MOD1_MASK | capture_x11.LOCK_MASK), "a")
        self.assertEqual(self.char(10, capture_x11.MOD4_MASK | capture_x11.SHIFT_MASK), "1")

    def test_every_group_the_layout_has(self):
        self.assertEqual(self.char(38, 0, 1), "ф")
        self.assertEqual(self.char(38, capture_x11.SHIFT_MASK, 1), "Ф")
        self.assertEqual(self.char(38, 0, 3), "α")  # a fourth group is read, not folded onto the first
        self.assertEqual(self.char(10, 0, 1), "1")  # a key without the group wraps

    def test_the_keypad(self):
        self.assertEqual(self.char(87), "end")  # Num Lock off: it moves, as Windows' VK_END
        self.assertEqual(self.char(87, capture_x11.NUM_LOCK_MASK), "1")
        self.assertEqual(self.char(87, capture_x11.NUM_LOCK_MASK | capture_x11.CONTROL_MASK), "1")

    def test_a_dead_key_types_nothing(self):
        self.assertIsNone(self.char(34))


class KeyTest(Harness):
    def test_a_named_key_outside_a_grab_from_a_raw_event(self):
        self.feed(raw(capture_x11.XI_RAW_KEY_PRESS, 105, sourceid=8), raw(capture_x11.XI_RAW_KEY_RELEASE, 105, sourceid=8))
        self.assertEqual(self.seen.keys, [("cmd_r", True, 97, None), ("cmd_r", False, 97, None)])
        self.assertNotIn(("state",), self.connection.calls)

    def test_a_character_outside_a_grab_reads_the_state_once(self):
        self.connection.mods = capture_x11.SHIFT_MASK
        self.feed(raw(capture_x11.XI_RAW_KEY_PRESS, 38, sourceid=8))
        self.assertEqual(self.seen.keys, [("A", True, 30, "a")])
        self.assertEqual(self.connection.calls.count(("state",)), 1)

    def test_a_release_goes_out_under_its_press_s_name(self):
        self.connection.mods = capture_x11.NUM_LOCK_MASK
        self.feed(raw(capture_x11.XI_RAW_KEY_PRESS, 87, sourceid=8))
        self.connection.mods = 0  # Num Lock turned off while 1 is held
        self.feed(raw(capture_x11.XI_RAW_KEY_RELEASE, 87, sourceid=8))
        self.assertEqual(self.seen.keys, [("1", True, 79, None), ("1", False, 79, None)])

    def test_keys_from_inside_a_grab_come_from_the_grab(self):
        self.hooks.grab_while(lambda: True)
        self.hooks._tick()
        inside = self.connection.serial
        self.feed(raw(capture_x11.XI_RAW_KEY_PRESS, 38, sourceid=8, sequence=inside), key(True, 38, capture_x11.SHIFT_MASK, sequence=inside),
                  key(True, 38, capture_x11.SHIFT_MASK, flags=capture_x11.XI_KEY_REPEAT, sequence=inside),
                  raw(capture_x11.XI_RAW_KEY_RELEASE, 38, sourceid=8, sequence=inside), key(False, 38, sequence=inside))
        self.assertEqual(self.seen.keys, [("A", True, 30, "a"), ("A", True, 30, "a"), ("A", False, 30, "a")])
        self.assertNotIn(("state",), self.connection.calls)

    def test_a_raw_key_from_before_the_grab_still_counts_after_it(self):
        # Hold style, a quick tap: the release is queued before the grab and read after it.
        before = self.connection.serial
        self.hooks.grab_while(lambda: True)
        self.hooks._tick()
        self.feed(raw(capture_x11.XI_RAW_KEY_RELEASE, 105, sourceid=8, sequence=before))
        self.assertEqual(self.seen.keys, [("cmd_r", False, 97, None)])

    def test_a_raw_key_from_inside_a_grab_read_after_it_is_not_read_twice(self):
        away = [True]
        self.hooks.grab_while(lambda: away[0])
        self.hooks._tick()
        inside = self.connection.serial
        away[0] = False
        self.hooks._tick()
        self.feed(raw(capture_x11.XI_RAW_KEY_PRESS, 105, sourceid=8, sequence=inside), key(True, 105, sequence=inside))
        self.assertEqual(self.seen.keys, [("cmd_r", True, 97, None)])
        after = self.connection.serial
        self.feed(raw(capture_x11.XI_RAW_KEY_RELEASE, 105, sourceid=8, sequence=after))
        self.assertEqual(self.seen.keys[-1], ("cmd_r", False, 97, None))

    def test_sequence_numbers_wrap(self):
        self.assertTrue(capture_x11._at_or_after(3, 0xFFFE))
        self.assertFalse(capture_x11._at_or_after(0xFFFE, 3))

    def test_injected_keys_are_dropped_unless_asked_for(self):
        self.feed(raw(capture_x11.XI_RAW_KEY_PRESS, 105, sourceid=5), key(True, 105, sourceid=5))
        self.assertEqual(self.seen.keys, [])
        self.hooks.drop_injected = False
        self.feed(raw(capture_x11.XI_RAW_KEY_PRESS, 105, sourceid=5))
        self.assertEqual(self.seen.keys, [("cmd_r", True, 97, None)])

    def test_a_dead_key_is_not_reported(self):
        self.feed(raw(capture_x11.XI_RAW_KEY_PRESS, 34, sourceid=8))
        self.assertEqual(self.seen.keys, [])


class HandedBackTest(Harness):
    """An input the sender hands back from inside a grab reached no app; it is played here."""

    def setUp(self):
        super().setUp()
        self.hooks.grab_while(lambda: True)
        self.hooks._tick()
        self.inside = self.connection.serial
        self.seen.answer = False

    def test_a_key_is_played_with_the_keyboard_let_go_around_it(self):
        self.feed(key(True, 123, sequence=self.inside))  # volume up, on the stays-here list
        self.assertEqual([call for call in self.connection.calls if call[0] == "fake_key"], [("fake_key", 123, True, (2,))])
        self.assertEqual(sorted(self.connection.grabs), [2, 3])

    def test_a_button_and_the_wheel_are_played_with_the_pointer_let_go(self):
        self.feed(raw(capture_x11.XI_RAW_BUTTON_PRESS, 8, sequence=self.inside), raw(capture_x11.XI_RAW_BUTTON_PRESS, 4, sequence=self.inside))
        fakes = [call for call in self.connection.calls if call[0] == "fake_button"]
        self.assertEqual(fakes, [("fake_button", 8, True, (3,)), ("fake_button", 4, True, (3,)), ("fake_button", 4, False, (3,))])
        self.assertEqual(sorted(self.connection.grabs), [2, 3])

    def test_nothing_from_outside_a_grab_is_played(self):
        self.feed(raw(capture_x11.XI_RAW_KEY_PRESS, 123, sourceid=8, sequence=self.inside - 1),
                  raw(capture_x11.XI_RAW_BUTTON_PRESS, 8, sequence=self.inside - 2))
        self.assertEqual([call for call in self.connection.calls if call[0].startswith("fake")], [])

    def test_a_grab_refused_after_playing_lets_go_of_everything(self):
        self.connection.refuse = {3}
        self.feed(key(True, 123, sequence=self.inside))
        self.assertEqual(self.connection.grabs, [])
        self.assertFalse(self.hooks.grabbed)


class PlayedTest(Harness):
    """What was played here stays held only until its own release, whichever way that comes."""

    def setUp(self):
        super().setUp()
        self.away = True
        self.hooks.grab_while(lambda: self.away)
        self.hooks._tick()
        self.inside = self.connection.serial

    def fakes(self):
        return [call[:3] for call in self.connection.calls if call[0].startswith("fake")]

    def test_a_played_key_is_let_go_when_its_release_comes_from_outside_the_grab(self):
        self.seen.answer = False
        self.feed(key(True, 123, sequence=self.inside))
        self.away = False
        self.hooks._tick()
        self.seen.answer = True  # at home the sender keeps nothing
        self.feed(raw(capture_x11.XI_RAW_KEY_RELEASE, 123, sourceid=8, sequence=self.connection.serial))
        self.assertEqual(self.fakes(), [("fake_key", 123, True), ("fake_key", 123, False)])

    def test_a_played_button_is_let_go_by_its_release(self):
        self.seen.answer = False
        self.feed(raw(capture_x11.XI_RAW_BUTTON_PRESS, 9, sequence=self.inside))
        self.seen.answer = True
        self.feed(raw(capture_x11.XI_RAW_BUTTON_RELEASE, 9, sequence=self.inside))
        self.assertEqual(self.fakes(), [("fake_button", 9, True), ("fake_button", 9, False)])

    def test_whatever_is_still_played_is_let_go_when_capture_ends(self):
        self.seen.answer = False
        self.feed(key(True, 123, sequence=self.inside), raw(capture_x11.XI_RAW_BUTTON_PRESS, 8, sequence=self.inside))
        self.hooks._release_played()
        self.assertEqual(self.fakes()[-2:], [("fake_key", 123, False), ("fake_button", 8, False)])


class SpanTest(Harness):
    def test_a_let_go_span_is_forgotten_before_the_sequence_comes_round(self):
        away = [True]
        self.hooks.grab_while(lambda: away[0])
        self.hooks._tick()
        inside = self.connection.serial
        away[0] = False
        self.hooks._tick()
        self.assertTrue(self.hooks._inside(3, inside))
        with mock.patch.object(capture_x11, "SPAN_KEEP_SECONDS", -1):
            self.assertFalse(self.hooks._inside(3, inside))  # one lap on, the same number is outside

    def test_the_grab_counts_as_held_through_the_ungrab_s_round_trip(self):
        self.connection.held_while_ungrabbing = lambda: self.hooks.grabbed
        self.hooks.grab_while(lambda: True)
        self.hooks._tick()
        self.hooks._let_go()
        self.assertEqual([call[2] for call in self.connection.calls if call[0] == "ungrab"], [True, True])


class MouseTest(Harness):
    def test_buttons_as_windows_messages(self):
        for number in (1, 2, 3, 8, 9):
            self.feed(raw(capture_x11.XI_RAW_BUTTON_PRESS, number), raw(capture_x11.XI_RAW_BUTTON_RELEASE, number))
        self.assertEqual(self.messages(), [("button", name, down) for name in ("left", "middle", "right", "back", "forward") for down in (True, False)])

    def test_buttons_follow_the_pointer_mapping(self):
        self.connection.mapping = [3, 2, 1] + list(range(4, 11))  # left-handed
        self.feed(("mapping", capture_x11.MAPPING_POINTER), raw(capture_x11.XI_RAW_BUTTON_PRESS, 1))
        self.assertEqual(self.messages(), [("button", "right", True)])

    def test_the_wheel_buttons_on_press_only(self):
        for number in (4, 5, 6, 7):
            self.feed(raw(capture_x11.XI_RAW_BUTTON_PRESS, number, sourceid=9), raw(capture_x11.XI_RAW_BUTTON_RELEASE, number, sourceid=9))
        self.assertEqual(self.messages(), [("wheel", 1.0, 0.0), ("wheel", -1.0, 0.0), ("wheel", 0.0, -1.0), ("wheel", 0.0, 1.0)])

    def test_a_smooth_scrolling_wheel_is_its_valuators_and_its_emulated_buttons_are_skipped(self):
        self.feed(raw(capture_x11.XI_RAW_BUTTON_PRESS, 5, flags=capture_x11.XI_POINTER_EMULATED),
                  raw(capture_x11.XI_RAW_MOTION, values={3: 15.0}), raw(capture_x11.XI_RAW_MOTION, values={3: -7.5}),
                  raw(capture_x11.XI_RAW_MOTION, values={3: -7.5}), raw(capture_x11.XI_RAW_MOTION, values={2: 15.0}))
        self.assertEqual(self.messages(), [("wheel", -1.0, 0.0), ("wheel", 0.5, 0.0), ("wheel", 0.5, 0.0), ("wheel", 0.0, 1.0)])
        self.assertEqual(self.seen.motions, [])

    def test_motion_carries_its_fraction_and_moves_the_pin(self):
        self.feed(raw(capture_x11.XI_RAW_MOTION, values={0: 0.6, 1: -1.5}), raw(capture_x11.XI_RAW_MOTION, values={0: 0.6, 1: -0.5}))
        self.assertEqual(self.seen.motions, [(0, -1), (1, -1)])
        self.assertEqual(self.seen.mice, [(capture_x11.WM_MOUSEMOVE, 0)] * 2)

    def test_injected_motion_is_dropped(self):
        self.feed(raw(capture_x11.XI_RAW_MOTION, sourceid=4, values={0: 5.0, 1: 0.0}), raw(capture_x11.XI_RAW_BUTTON_PRESS, 1, sourceid=4))
        self.assertEqual((self.seen.motions, self.seen.mice), ([], []))

    def test_an_absolute_pointer_moves_the_pin_but_gives_no_pressure(self):
        self.feed(raw(capture_x11.XI_RAW_MOTION, sourceid=7, values={0: 900.0, 1: 300.0}))
        self.assertEqual((self.seen.motions, self.seen.mice), ([], [(capture_x11.WM_MOUSEMOVE, 0)]))

    def test_a_new_device_is_read_on_a_hierarchy_change(self):
        self.connection.device_list.append(Device(9, capture_x11.XI_SLAVE_POINTER, "Virtual XTEST pointer 2"))
        self.feed(("hierarchy",), raw(capture_x11.XI_RAW_MOTION, sourceid=9, values={0: 5.0}))
        self.assertEqual(self.seen.motions, [])


class GrabTest(Harness):
    def setUp(self):
        super().setUp()
        self.away = False
        self.hooks.grab_while(lambda: self.away)

    def test_grabs_every_master_while_away_and_lets_go_at_home(self):
        self.hooks._tick()
        self.assertEqual(self.connection.grabs, [])
        self.away = True
        self.hooks._tick()
        self.hooks._tick()
        self.assertEqual(self.connection.grabs, [2, 3])
        self.assertEqual(self.connection.calls.count(("grab", 2)), 1)
        self.away = False
        self.hooks._tick()
        self.assertEqual(self.connection.grabs, [])
        self.assertFalse(self.hooks.grabbed)

    def test_a_refused_grab_holds_nothing_and_is_tried_again(self):
        self.connection.refuse = {3}
        self.away = True
        with self.assertLogs(capture_x11.LOGGER, "WARNING") as logs:
            self.hooks._tick()
            self.hooks._tick()
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(self.connection.grabs, [])
        self.connection.refuse = set()
        self.hooks._tick()
        self.assertEqual(self.connection.grabs, [2, 3])

    def test_a_predicate_that_raises_lets_go(self):
        self.away = True
        self.hooks._tick()

        def broken():
            raise RuntimeError("no")

        self.hooks.grab_while(broken)
        with self.assertLogs(capture_x11.LOGGER, "ERROR"):
            self.hooks._tick()
        self.assertEqual(self.connection.grabs, [])

    def test_a_new_master_while_away_is_grabbed_too(self):
        self.away = True
        self.hooks._tick()
        self.connection.device_list += [Device(10, capture_x11.XI_MASTER_POINTER, "Second pointer"),
                                        Device(11, capture_x11.XI_MASTER_KEYBOARD, "Second keyboard")]
        self.feed(("hierarchy",))
        self.assertEqual(self.connection.grabs, [])
        self.hooks._tick()
        self.assertEqual(sorted(self.connection.grabs), [2, 3, 10, 11])

    def test_raw_events_still_count_while_grabbed(self):
        self.away = True
        self.hooks._tick()
        inside = self.connection.serial
        self.feed(raw(capture_x11.XI_RAW_MOTION, values={0: 4.0, 1: 0.0}, sequence=inside), raw(capture_x11.XI_RAW_BUTTON_PRESS, 1, sequence=inside))
        self.assertEqual(self.seen.motions, [(4, 0)])
        self.assertEqual(self.messages()[-1], ("button", "left", True))


class ThreadTest(unittest.TestCase):
    def make(self, connection, on_key=None):
        self.seen = Recorder()
        return capture_x11.Hooks(on_key or self.seen.on_key, self.seen.on_mouse, self.seen.on_motion, connect=lambda: connection)

    def wait_for(self, condition, timeout=2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return True
            time.sleep(0.01)
        return condition()

    def test_stop_lets_go_and_closes(self):
        connection = FakeConnection()
        hooks = self.make(connection)
        hooks.grab_while(lambda: True)
        hooks.start()
        self.assertTrue(self.wait_for(lambda: connection.grabs == [2, 3]))
        hooks.stop()
        self.assertEqual(connection.grabs, [])
        self.assertTrue(connection.closed)

    def test_the_loop_dying_lets_go(self):
        connection = FakeConnection()
        hooks = self.make(connection)
        hooks.grab_while(lambda: True)
        hooks.start()
        self.assertTrue(self.wait_for(lambda: connection.grabs == [2, 3]))
        with self.assertLogs(capture_x11.LOGGER, "ERROR"):
            connection.raise_in_events = ConnectionError("the X server went away")
            self.assertTrue(self.wait_for(lambda: connection.closed))
        self.assertEqual(connection.grabs, [])
        hooks.stop()

    def test_an_event_that_fails_does_not_stop_capture(self):
        connection = FakeConnection()
        hooks = self.make(connection)
        hooks.start()
        with self.assertLogs(capture_x11.LOGGER, "ERROR"):
            connection.push(("raw", capture_x11.XI_RAW_MOTION, 0, 6, 0, None, 2, 0))  # a malformed event
            connection.push(raw(capture_x11.XI_RAW_KEY_PRESS, 105, sourceid=8))
            self.assertTrue(self.wait_for(lambda: self.seen.keys))
        hooks.stop()
        self.assertEqual(self.seen.keys, [("cmd_r", True, 97, None)])

    def test_capture_ending_on_its_own_is_reported_and_a_stop_is_not(self):
        connection = FakeConnection()
        hooks = self.make(connection)
        failures = []
        hooks.on_failure = failures.append
        hooks.start()
        with self.assertLogs(capture_x11.LOGGER, "ERROR"):
            connection.raise_in_events = ConnectionError("the X server went away")
            self.assertTrue(self.wait_for(lambda: failures))
        hooks.stop()
        self.assertIsInstance(failures[0], ConnectionError)
        quiet = FakeConnection()
        hooks = self.make(quiet)
        hooks.on_failure = failures.append
        hooks.start()
        hooks.stop()
        self.assertEqual(len(failures), 1)

    def test_a_connection_that_cannot_open_is_the_caller_s_error(self):
        def refuse():
            raise RuntimeError("This X server has XInput 2.0; Beamer needs 2.1")

        hooks = capture_x11.Hooks(None, None, None, connect=refuse)
        with self.assertLogs(capture_x11.LOGGER, "ERROR"), self.assertRaisesRegex(RuntimeError, "needs 2.1"):
            hooks.start()

    def test_a_thread_stuck_while_grabbed_has_its_connection_ended(self):
        connection = FakeConnection()
        release = threading.Event()

        def stuck(*args):
            release.wait(5)
            return True

        hooks = self.make(connection, on_key=stuck)
        hooks.grab_while(lambda: True)
        with mock.patch.object(capture_x11, "STALL_SECONDS", 0.3):
            hooks.start()
            self.assertTrue(self.wait_for(lambda: connection.grabs == [2, 3]))
            with self.assertLogs(capture_x11.LOGGER, "ERROR") as logs:
                connection.push(key(True, 38, sequence=connection.serial))
                self.assertTrue(self.wait_for(lambda: connection.aborted))
                release.set()
                self.assertTrue(self.wait_for(lambda: connection.closed))
        self.assertIn("has not answered", logs.output[0])
        hooks.stop()

    def test_a_stop_that_times_out_ends_the_connection_and_a_start_waits_for_it(self):
        connection = FakeConnection()
        release = threading.Event()

        def stuck(*args):
            release.wait(10)
            return True

        hooks = self.make(connection, on_key=stuck)
        hooks.start()
        connection.push(raw(capture_x11.XI_RAW_KEY_PRESS, 105, sourceid=8))
        time.sleep(0.2)
        with mock.patch.object(threading.Thread, "join", lambda self, timeout=None: None), \
                self.assertLogs(capture_x11.LOGGER, "ERROR"):
            hooks.stop()
        self.assertTrue(connection.aborted)
        with self.assertRaisesRegex(RuntimeError, "has not stopped"):
            hooks.start()
        release.set()
        self.assertTrue(self.wait_for(lambda: connection.closed))


if __name__ == "__main__":
    unittest.main()
