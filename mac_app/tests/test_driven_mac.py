"""The Mac as a machine that is driven, whichever machine drives it: media keys, gestures, the
keys only a PC has, and fractional scroll. Each test reads the event the injector posts, through
a fake Quartz and AppKit that record it, so nothing here touches the real pointer or keyboard."""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import input_injector_mac as injector
from core import protocol
from gestures import GestureTranslator


class FakeEvent:
    def __init__(self, kind, **fields):
        self.kind = kind
        self.fields = dict(fields)


class FakeQuartz:
    kCGEventSourceUserData = 42
    kCGScrollEventUnitLine = 1
    kCGScrollEventUnitPixel = 0
    kCGEventFlagMaskCommand = 1 << 20
    kCGEventFlagMaskControl = 1 << 18
    kCGEventFlagMaskShift = 1 << 17
    kCGEventFlagMaskAlternate = 1 << 19
    kCGEventFlagMaskAlphaShift = 1 << 16
    kCGEventFlagMaskSecondaryFn = 1 << 23
    kCGEventFlagMaskNumericPad = 1 << 21
    kCGHIDEventTap = 0

    def __init__(self):
        self.posted = []

    def CGEventCreateKeyboardEvent(self, source, code, down):
        return FakeEvent("key", code=code, down=down, flags=0)

    def CGEventKeyboardSetUnicodeString(self, event, length, text):
        event.fields["text"] = text

    def CGEventCreateScrollWheelEvent(self, source, unit, count, wheel_y, wheel_x):
        return FakeEvent("scroll", unit=unit, wheel_y=wheel_y, wheel_x=wheel_x, flags=0)

    def CGEventSetFlags(self, event, flags):
        event.fields["flags"] = flags

    def CGEventSetIntegerValueField(self, event, field, value):
        event.fields[field] = value

    def CGEventPost(self, tap, event):
        self.posted.append(event)


class FakeNSEvent:
    def __init__(self, data):
        self.data = data

    def CGEvent(self):
        return FakeEvent("system", **self.data)


class FakeAppKit:
    NSEventTypeSystemDefined = 14

    class NSEvent:
        @staticmethod
        def otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            kind, location, flags, timestamp, window, context, subtype, data1, data2
        ):
            return FakeNSEvent({"type": kind, "flags": flags, "subtype": subtype, "data1": data1, "data2": data2})


FN = FakeQuartz.kCGEventFlagMaskSecondaryFn
ARROW_FLAGS = FN | FakeQuartz.kCGEventFlagMaskNumericPad


class InjectorCase(unittest.TestCase):
    def setUp(self):
        self.quartz = FakeQuartz()
        self._saved = (injector.Quartz, getattr(injector, "AppKit", None))
        injector.Quartz = self.quartz
        injector.AppKit = FakeAppKit
        injector.release_all()
        injector._warned_names.clear()
        injector.reset_scroll_remainders()

    def tearDown(self):
        injector.release_all()
        injector.Quartz, injector.AppKit = self._saved

    def keys(self):
        return [(e.fields["code"], e.fields["down"], e.fields["flags"]) for e in self.quartz.posted if e.kind == "key"]


class NavigationKeyFlagTests(InjectorCase):
    """A real arrow, F key, Home, End, Page Up, Page Down or forward delete carries the Fn flag
    (arrows the numeric-pad flag too), and system shortcuts match on it, so every such key the PC
    sends carries it, up as well as down. Ordinary keys carry neither."""

    def flags_of(self, name, down=True):
        self.quartz.posted.clear()
        injector.inject_key(name, down=down)
        return self.keys()[0][2]

    def test_arrows_carry_fn_and_numeric_pad(self):
        for name in ("left", "right", "up", "down"):
            self.assertEqual(self.flags_of(name), ARROW_FLAGS, name)
            self.assertEqual(self.flags_of(name, down=False), ARROW_FLAGS, name)

    def test_function_keys_carry_fn(self):
        for number in range(1, 21):
            self.assertEqual(self.flags_of(f"f{number}"), FN, f"f{number}")

    def test_home_end_paging_and_forward_delete_carry_fn(self):
        for name in ("home", "end", "page_up", "page_down", "delete"):
            self.assertEqual(self.flags_of(name), FN, name)

    def test_the_keys_the_pc_alone_has_carry_fn_when_they_land_on_a_function_key(self):
        for name in ("print_screen", "scroll_lock", "pause"):
            self.assertEqual(self.flags_of(name), FN, name)

    def test_fn_joins_a_held_modifier_rather_than_replacing_it(self):
        injector.inject_key("shift", down=True)
        self.assertEqual(self.flags_of("left"), FakeQuartz.kCGEventFlagMaskShift | ARROW_FLAGS)

    def test_ordinary_keys_carry_neither(self):
        for name in ("a", "enter", "space", "backspace", "tab", "esc"):
            self.assertEqual(self.flags_of(name), 0, name)


class MediaKeyTests(InjectorCase):
    def system(self):
        return [e for e in self.quartz.posted if e.kind == "system"]

    def test_each_media_name_is_posted_as_an_aux_control_event(self):
        # NX_KEYTYPE_SOUND_UP 0, SOUND_DOWN 1, MUTE 7, PLAY 16, NEXT 17, PREVIOUS 18.
        expected = {
            "volume_up": 0,
            "volume_down": 1,
            "volume_mute": 7,
            "media_play_pause": 16,
            "media_next": 17,
            "media_prev": 18,
        }
        for name, nx_key in expected.items():
            self.quartz.posted.clear()
            injector.inject_key(name, down=True)
            (event,) = self.system()
            self.assertEqual(event.fields["subtype"], 8, name)
            self.assertEqual(event.fields["data1"] >> 16, nx_key, name)

    def test_down_and_up_carry_the_states_media_keys_decodes(self):
        import media_keys

        injector.inject_key("media_next", down=True)
        injector.inject_key("media_next", down=False)
        down, up = self.system()
        self.assertEqual(media_keys.decode(down.fields["subtype"], down.fields["data1"]), ("media_next", True, False))
        self.assertEqual(media_keys.decode(up.fields["subtype"], up.fields["data1"]), ("media_next", False, False))
        self.assertEqual(down.fields["flags"], 0xA00)
        self.assertEqual(up.fields["flags"], 0xB00)

    def test_the_event_is_marked_as_beamers_own(self):
        injector.inject_key("volume_up", down=True)
        (event,) = self.system()
        self.assertEqual(event.fields[FakeQuartz.kCGEventSourceUserData], injector.INJECTED_MARK)

    def test_media_keys_never_type_or_press_an_ordinary_key(self):
        injector.inject_key("media_play_pause", down=True)
        self.assertEqual(self.keys(), [])

    def test_stop_is_dropped_and_logged_once(self):
        # macOS has no stop key; mapping it to play/pause would start playback that was paused.
        with self.assertLogs("Beamer", level="WARNING") as logged:
            injector.inject_key("media_stop", down=True)
            injector.inject_key("media_stop", down=False)
        self.assertEqual(len(logged.records), 1)
        self.assertEqual(self.quartz.posted, [])


class WindowsOnlyKeyTests(InjectorCase):
    def test_print_screen_scroll_lock_and_pause_land_on_the_keys_a_mac_keyboard_puts_there(self):
        # An Apple extended keyboard's F13, F14 and F15 are Print Screen, Scroll Lock and Pause.
        for name, code in (("print_screen", 0x69), ("scroll_lock", 0x6B), ("pause", 0x71)):
            self.quartz.posted.clear()
            injector.inject_key(name, down=True)
            self.assertEqual(self.keys(), [(code, True, FN)], name)

    def test_num_lock_lands_on_the_keypad_clear_key(self):
        injector.inject_key("num_lock", down=True)
        self.assertEqual(self.keys(), [(0x47, True, 0)])

    def test_browser_back_and_forward_are_command_bracket(self):
        for name, character in (("browser_back", "["), ("browser_forward", "]")):
            self.quartz.posted.clear()
            injector.inject_key(name, down=True)
            injector.inject_key(name, down=False)
            code = {"[": 0x21, "]": 0x1E}[character]
            command = FakeQuartz.kCGEventFlagMaskCommand
            self.assertEqual(self.keys(), [(code, True, command), (code, False, command)], name)

    def test_browser_keys_stay_on_the_bracket_key_on_a_layout_without_one(self):
        # A German or French layout has no unshifted bracket; planning it there gives key code 0,
        # which under Command is Select All.
        saved = injector.keyboard_layout.code_for
        injector.keyboard_layout.code_for = lambda character: None
        try:
            injector.inject_key("browser_back", down=True)
        finally:
            injector.keyboard_layout.code_for = saved
        self.assertEqual([code for code, _, _ in self.keys()], [0x21, 0x21])

    def test_browser_keys_keep_a_held_modifier_on_the_chord(self):
        injector.inject_key("shift", down=True)
        self.quartz.posted.clear()
        injector.inject_key("browser_back", down=True)
        flags = self.keys()[0][2]
        self.assertEqual(flags, FakeQuartz.kCGEventFlagMaskCommand | FakeQuartz.kCGEventFlagMaskShift)

    def test_menu_has_no_mac_key_and_is_logged_once(self):
        with self.assertLogs("Beamer", level="WARNING") as logged:
            injector.inject_key("menu", down=True)
            injector.inject_key("menu", down=False)
        self.assertEqual(len(logged.records), 1)
        self.assertEqual(self.quartz.posted, [])

    def test_every_name_the_pc_sends_is_placed_or_knowingly_dropped(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, "win_app", "capture_win.py"), encoding="utf-8") as source:
            block = source.read().split("VK_TO_NAME: Dict[int, str] = {", 1)[1].split("\n}\n", 1)[0]
        names = {part.split('"')[1] for part in block.splitlines() if ': "' in part}
        unplaced = {
            name
            for name in names
            if name not in injector.KEY_NAME_TO_CODE
            and name not in injector.WINDOWS_ONLY_KEY_CODES
            and not injector.handles_key(name)
        }
        self.assertEqual(unplaced, {"media_stop", "menu"})


class ScrollRemainderTests(InjectorCase):
    def scrolls(self):
        return [(e.fields["unit"], e.fields["wheel_y"], e.fields["wheel_x"]) for e in self.quartz.posted if e.kind == "scroll"]

    def test_small_notches_add_up_to_a_click_instead_of_rounding_to_nothing(self):
        for _ in range(10):
            injector.inject_scroll(0.25)
        # Ten quarter notches are two whole clicks and a half, posted as each one completes.
        self.assertEqual([y for _, y, _ in self.scrolls()], [1, 1])

    def test_the_remainder_is_carried_per_direction(self):
        injector.inject_scroll(0.6)
        injector.inject_scroll(-0.6)
        self.assertEqual(sum(y for _, y, _ in self.scrolls()), 0)

    def test_a_whole_notch_posts_at_once(self):
        injector.inject_scroll(2)
        self.assertEqual(self.scrolls(), [(FakeQuartz.kCGScrollEventUnitLine, 2, 0)])

    def test_horizontal_carries_its_own_remainder(self):
        for _ in range(3):
            injector.inject_scroll(0, 0.5)
        self.assertEqual(sum(x for _, _, x in self.scrolls()), 1)

    def test_pixel_mode_carries_fractions_and_does_not_share_the_line_remainder(self):
        injector.inject_scroll(0.5)
        injector.inject_scroll(0.25, mode="pixel")
        injector.inject_scroll(0.25, mode="pixel")
        injector.inject_scroll(0.5)
        posted = self.scrolls()
        self.assertEqual([p for p in posted if p[0] == FakeQuartz.kCGScrollEventUnitLine], [(FakeQuartz.kCGScrollEventUnitLine, 1, 0)])
        # Pixel 0.25 + 0.25 is half a pixel: nothing yet, and the line remainder stayed put.
        self.assertEqual([p for p in posted if p[0] == FakeQuartz.kCGScrollEventUnitPixel], [])

    def test_release_all_drops_a_remainder_so_it_cannot_tick_after_a_reconnect(self):
        injector.inject_scroll(0.5)
        injector.release_all()
        injector.inject_scroll(0.25)
        injector.inject_scroll(0.25)
        self.assertEqual(self.scrolls(), [])


class GestureInjectionTests(InjectorCase):
    def chord(self):
        return [(code, down) for code, down, _ in self.keys()]

    def test_the_system_swipes_press_the_control_arrow_shortcuts(self):
        ctrl = injector.KEY_NAME_TO_CODE["ctrl"]
        arrows = {
            protocol.GESTURE_SWIPE_UP: "up",  # Mission Control
            protocol.GESTURE_SWIPE_DOWN: "down",  # App Expose
            protocol.GESTURE_SWIPE_LEFT: "right",  # fingers left: the next Space, on the right
            protocol.GESTURE_SWIPE_RIGHT: "left",
        }
        for gesture, arrow in arrows.items():
            self.quartz.posted.clear()
            injector.inject_gesture(gesture)
            code = injector.KEY_NAME_TO_CODE[arrow]
            self.assertEqual(self.chord(), [(ctrl, True), (code, True), (code, False), (ctrl, False)], gesture)

    def test_the_arrow_goes_down_with_control_in_its_flags(self):
        injector.inject_gesture(protocol.GESTURE_SWIPE_UP)
        arrow_down = [k for k in self.keys() if k[0] == injector.KEY_NAME_TO_CODE["up"]][0]
        self.assertEqual(arrow_down[2], FakeQuartz.kCGEventFlagMaskControl | ARROW_FLAGS)

    def test_spread_shows_the_desktop(self):
        injector.inject_gesture(protocol.GESTURE_SPREAD)
        code = injector.KEY_NAME_TO_CODE["f11"]
        self.assertEqual(self.chord(), [(code, True), (code, False)])

    def test_pinch_has_no_shortcut_and_is_logged_once(self):
        # Launchpad left macOS in 26 and its shortcut went with it.
        with self.assertLogs("Beamer", level="WARNING") as logged:
            injector.inject_gesture(protocol.GESTURE_PINCH)
            injector.inject_gesture(protocol.GESTURE_PINCH)
        self.assertEqual(len(logged.records), 1)
        self.assertEqual(self.quartz.posted, [])

    def test_an_unknown_gesture_is_logged_and_ignored(self):
        with self.assertLogs("Beamer", level="WARNING"):
            injector.inject_gesture("wave")
        self.assertEqual(self.quartz.posted, [])

    def test_the_chord_leaves_no_modifier_held(self):
        injector.inject_gesture(protocol.GESTURE_SWIPE_DOWN)
        self.assertEqual(injector._mods_down, set())

    def test_a_modifier_the_sender_holds_is_not_released_by_the_chord(self):
        injector.inject_key("ctrl", down=True)
        self.quartz.posted.clear()
        injector.inject_gesture(protocol.GESTURE_SWIPE_UP)
        code = injector.KEY_NAME_TO_CODE["up"]
        self.assertEqual(self.chord(), [(code, True), (code, False)])
        self.assertEqual(injector._mods_down, {"ctrl"})

    def test_every_gesture_the_wire_names_is_handled_or_knowingly_dropped(self):
        for name in protocol.GESTURE_NAMES:
            self.quartz.posted.clear()
            if name == protocol.GESTURE_PINCH:
                with self.assertLogs("Beamer", level="WARNING"):
                    injector.inject_gesture(name)
            else:
                injector.inject_gesture(name)
                self.assertTrue(self.quartz.posted, name)


class FakeNSGesture:
    def __init__(self, magnification=0.0, delta_x=0.0, phase=0):
        self.magnification = magnification
        self.deltaX = delta_x
        self.deltaY = 0.0
        self.phase = phase


class MacShapedGestureTranslatorTests(unittest.TestCase):
    def keys(self, messages):
        return [(m["type"], m["data"]["key"]) for m in messages if m["type"] in (protocol.MSG_KEYDOWN, protocol.MSG_KEYUP)]

    def test_the_default_is_still_the_windows_shaped_chord(self):
        translator = GestureTranslator()
        swipe = translator.translate(31, FakeNSGesture(delta_x=1))
        self.assertEqual(
            self.keys(swipe),
            [(protocol.MSG_KEYDOWN, "alt"), (protocol.MSG_KEYDOWN, "left"), (protocol.MSG_KEYUP, "left"), (protocol.MSG_KEYUP, "alt")],
        )

    def test_a_page_swipe_to_a_mac_is_command_bracket(self):
        translator = GestureTranslator(receiver="mac")
        back = translator.translate(31, FakeNSGesture(delta_x=1))
        forward = translator.translate(31, FakeNSGesture(delta_x=-1))
        self.assertEqual(
            self.keys(back),
            [(protocol.MSG_KEYDOWN, "cmd"), (protocol.MSG_KEYDOWN, "["), (protocol.MSG_KEYUP, "["), (protocol.MSG_KEYUP, "cmd")],
        )
        self.assertEqual(self.keys(forward)[1], (protocol.MSG_KEYDOWN, "]"))

    def test_a_pinch_to_a_mac_zooms_with_command_plus_and_minus(self):
        translator = GestureTranslator(receiver="mac")
        zoom_in = translator.translate(30, FakeNSGesture(magnification=0.06))
        zoom_out = translator.translate(30, FakeNSGesture(magnification=-0.06))
        self.assertEqual(self.keys(zoom_in)[:2], [(protocol.MSG_KEYDOWN, "cmd"), (protocol.MSG_KEYDOWN, "=")])
        self.assertEqual(self.keys(zoom_out)[:2], [(protocol.MSG_KEYDOWN, "cmd"), (protocol.MSG_KEYDOWN, "-")])
        self.assertFalse([m for m in zoom_in if m["type"] == protocol.MSG_SCROLL])

    def test_the_mac_chord_names_its_us_key_so_another_layout_still_presses_a_key(self):
        translator = GestureTranslator(receiver="mac")
        keys = [m["data"] for m in translator.translate(31, FakeNSGesture(delta_x=1)) if m["type"] == protocol.MSG_KEYDOWN]
        self.assertEqual(keys, [{"key": "cmd"}, {"key": "[", "us": "["}])
        down = injector.plan_key_event("[", True, {"cmd"}, us="[")
        self.assertEqual(down, (0x21, None))

    def test_a_pinch_to_windows_is_still_control_wheel(self):
        zoom = GestureTranslator().translate(30, FakeNSGesture(magnification=0.06))
        self.assertTrue([m for m in zoom if m["type"] == protocol.MSG_SCROLL])

    def test_an_unknown_receiver_is_refused(self):
        with self.assertRaises(ValueError):
            GestureTranslator(receiver="linux")


if __name__ == "__main__":
    unittest.main()
