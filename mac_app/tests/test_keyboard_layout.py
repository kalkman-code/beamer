"""Keys follow this Mac's own layout in both directions. UK and US share letter positions, so every
test here uses a layout that moves them: German swaps Y and Z and puts ü where US has [, and French
puts A where US has Q. The tables are what UCKeyTranslate reads from macOS's own German and French
layouts; the last class reads them from the system to prove that."""

import ctypes
import sys
import unittest
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config
import keyboard_layout
import input_injector_mac as injector
from core import protocol
from bridge import QuartzEventTranslator
from bridge_fakes import FakeQuartz

GERMAN = {0x00: "a", 0x06: "y", 0x07: "x", 0x08: "c", 0x09: "v", 0x0C: "q", 0x10: "z", 0x18: None, 0x21: "ü", 0x29: "ö", 0x32: "<"}
FRENCH = {0x00: "q", 0x06: "w", 0x0C: "a", 0x0D: "z", 0x10: "y", 0x12: "&", 0x29: "m"}
RUSSIAN = {0x00: "ф", 0x06: "я", 0x08: "с", 0x09: "м", 0x0C: "й", 0x10: "н", 0x12: "1", 0x13: "2", 0x21: "х", 0x2F: "ю"}


class LayoutTest(unittest.TestCase):
    def tearDown(self):
        keyboard_layout.install({})


class TableTests(LayoutTest):
    def test_us_positions_until_a_layout_is_read(self):
        self.assertEqual(keyboard_layout.char_for(0x06), "z")
        self.assertEqual(keyboard_layout.code_for("z"), 0x06)

    def test_a_german_layout_swaps_y_and_z_both_ways(self):
        keyboard_layout.install(GERMAN)
        self.assertEqual(keyboard_layout.char_for(0x06), "y")
        self.assertEqual(keyboard_layout.code_for("z"), 0x10)
        self.assertEqual(keyboard_layout.code_for("ü"), 0x21)

    def test_a_us_character_on_a_key_the_layout_retypes_has_no_key(self):
        # US [ is ü on a German Mac, so posting that key would type ü.
        keyboard_layout.install(GERMAN)
        self.assertIsNone(keyboard_layout.code_for("["))

    def test_a_dead_key_has_no_character_and_its_us_one_has_no_key(self):
        # German 0x18 is the acute accent; US keeps "=" there. Posting it would start an accent.
        keyboard_layout.install(GERMAN)
        self.assertIsNone(keyboard_layout.char_for(0x18))
        self.assertIsNone(keyboard_layout.code_for("="))

    def test_a_key_the_layout_leaves_out_keeps_its_us_character(self):
        keyboard_layout.install(GERMAN)
        self.assertEqual(keyboard_layout.char_for(0x0E), "e")
        self.assertEqual(keyboard_layout.code_for("e"), 0x0E)


class ShortcutToWindowsTests(LayoutTest):
    def press(self, keycode, unicode, modifier=0x37):
        translator = QuartzEventTranslator(FakeQuartz)
        translator.modifier_down.add(modifier)
        event = {FakeQuartz.kCGKeyboardEventKeycode: keycode, FakeQuartz.kCGKeyboardEventAutorepeat: 0, "unicode": unicode}
        result = translator.key_result(FakeQuartz.kCGEventKeyDown, event, 0x3D, dict(config.DEFAULT_KEY_MAP))
        return result.messages

    def test_german_cmd_z_is_undo_not_redo(self):
        keyboard_layout.install(GERMAN)
        self.assertEqual(self.press(0x10, "z"), [{"type": protocol.MSG_KEYDOWN, "data": {"key": "z", "us": "y"}}])

    def test_french_cmd_a_selects_all(self):
        keyboard_layout.install(FRENCH)
        self.assertEqual(self.press(0x0C, "a"), [{"type": protocol.MSG_KEYDOWN, "data": {"key": "a", "us": "q"}}])

    def test_a_repeat_after_a_layout_switch_sends_the_first_press_character(self):
        translator = QuartzEventTranslator(FakeQuartz)
        key_map = dict(config.DEFAULT_KEY_MAP)

        def key(event_type, repeat):
            event = {FakeQuartz.kCGKeyboardEventKeycode: 0x06, FakeQuartz.kCGKeyboardEventAutorepeat: repeat, "unicode": keyboard_layout.char_for(0x06)}
            return translator.key_result(event_type, event, 0x3D, key_map).messages

        self.assertEqual(key(FakeQuartz.kCGEventKeyDown, 0), [{"type": protocol.MSG_KEYDOWN, "data": {"key": "z", "us": "z"}}])
        keyboard_layout.install(GERMAN)
        self.assertEqual(key(FakeQuartz.kCGEventKeyDown, 1), [{"type": protocol.MSG_KEYDOWN, "data": {"key": "z", "us": "z"}}])
        self.assertEqual(key(FakeQuartz.kCGEventKeyUp, 0), [{"type": protocol.MSG_KEYUP, "data": {"key": "z", "us": "z"}}])

    def test_option_still_sends_the_key_not_the_composed_character(self):
        keyboard_layout.install(GERMAN)
        self.assertEqual(self.press(0x06, "¥", modifier=0x3A), [{"type": protocol.MSG_KEYDOWN, "data": {"key": "y", "us": "z"}}])


class TypingOntoThisMacTests(LayoutTest):
    def plan(self, name, mods=None):
        return injector.plan_key_event(name, True, set() if mods is None else mods)

    def test_z_from_the_pc_is_z_on_a_german_mac(self):
        keyboard_layout.install(GERMAN)
        self.assertEqual(self.plan("z"), (0x10, None))

    def test_ctrl_a_from_the_pc_is_cmd_a_on_a_french_mac(self):
        keyboard_layout.install(FRENCH)
        self.assertEqual(self.plan("a", {"cmd"}), (0x0C, None))

    def test_an_accented_letter_the_layout_has_lands_on_its_key(self):
        keyboard_layout.install(GERMAN)
        self.assertEqual(self.plan("ü"), (0x21, None))

    def test_an_equals_sign_is_typed_not_posted_on_the_german_accent_key(self):
        keyboard_layout.install(GERMAN)
        self.assertEqual(self.plan("="), (0, "="))

    def test_a_release_lets_go_of_the_key_its_press_went_down_on(self):
        held, mods = {}, set()
        self.assertEqual(injector.plan_key_event("z", True, mods, held), (0x06, None))
        keyboard_layout.install(GERMAN)
        self.assertEqual(injector.plan_key_event("z", False, mods, held), (0x06, None))
        self.assertEqual(held, {})

    def test_a_character_the_layout_cannot_place_is_typed_as_text(self):
        keyboard_layout.install(GERMAN)
        self.assertEqual(self.plan("["), (0, "["))


class TwoScriptTests(LayoutTest):
    """GitHub issue 3: a PC switched to Russian sends the C key as "с", which this Mac on English
    cannot place, so it was typed as text with Ctrl cleared, and Ctrl+C typed "с". Keys now carry
    where they sit on a US keyboard, and a key lands there when a shortcut needs it or its letter
    is from a script this layout does not type there."""

    def plan(self, name, mods=None, us=None, down=True, held=None):
        return injector.plan_key_event(name, down, set() if mods is None else mods, held, us=us)

    def test_ctrl_c_from_a_pc_on_russian_is_cmd_c_on_an_english_mac(self):
        self.assertEqual(self.plan("с", {"cmd"}, us="c"), (0x08, None))

    def test_a_russian_letter_from_the_pc_types_this_macs_letter(self):
        self.assertEqual(self.plan("с", us="c"), (0x08, None))
        self.assertEqual(self.plan("С", {"shift"}, us="c"), (0x08, None))

    def test_a_capital_without_shift_is_this_macs_capital(self):
        self.assertEqual(self.plan("С", us="c"), (0, "C"))

    def test_a_capital_without_shift_lets_go_of_the_character_it_typed(self):
        self.assertEqual(self.plan("С", us="c", down=False, held={}), (0, "C"))

    def test_a_repeat_after_a_layout_switch_stays_on_the_key_that_is_down(self):
        held = {}
        self.assertEqual(self.plan("z", us="z", held=held), (0x06, None))
        keyboard_layout.install(GERMAN)
        self.assertEqual(self.plan("z", us="z", held=held), (0x06, None))
        self.assertEqual(self.plan("z", us="z", down=False, held=held), (0x06, None))
        self.assertEqual(held, {})

    def test_a_pc_on_english_types_russian_on_a_mac_on_russian(self):
        keyboard_layout.install(RUSSIAN)
        self.assertEqual(self.plan("c", us="c"), (0x08, None))
        self.assertEqual(self.plan("c", {"cmd"}, us="c"), (0x08, None))

    def test_matching_layouts_still_place_by_character(self):
        keyboard_layout.install(RUSSIAN)
        self.assertEqual(self.plan("с", us="c"), (0x08, None))

    def test_an_accent_from_the_same_script_is_still_typed_as_text(self):
        # French é sits on the US 2 key; a US Mac has no é, and 2 is not what was meant.
        self.assertEqual(self.plan("é", us="2"), (0, "é"))

    def test_a_shortcut_on_an_accented_key_lands_on_its_place(self):
        self.assertEqual(self.plan("é", {"cmd"}, us="2"), (0x13, None))

    def test_a_key_with_no_place_keeps_the_old_answer(self):
        # A phone's keyboard, or a PC on an older Beamer, sends no place.
        self.assertEqual(self.plan("с", {"cmd"}), (0, "с"))

    def test_the_release_lets_go_of_the_key_the_press_went_down_on(self):
        held = {}
        self.assertEqual(self.plan("с", us="c", held=held), (0x08, None))
        keyboard_layout.install(RUSSIAN)
        self.assertEqual(self.plan("с", us="c", down=False, held=held), (0x08, None))
        self.assertEqual(held, {})

    def test_the_mac_sends_where_its_key_sits(self):
        keyboard_layout.install(RUSSIAN)
        translator = QuartzEventTranslator(FakeQuartz)
        event = {FakeQuartz.kCGKeyboardEventKeycode: 0x08, FakeQuartz.kCGKeyboardEventAutorepeat: 0, "unicode": "с"}
        messages = translator.key_result(FakeQuartz.kCGEventKeyDown, event, 0x3D, dict(config.DEFAULT_KEY_MAP)).messages
        self.assertEqual(messages, [{"type": protocol.MSG_KEYDOWN, "data": {"key": "с", "us": "c"}}])


@unittest.skipUnless(sys.platform == "darwin", "reads macOS's own layouts")
class SystemLayoutTests(unittest.TestCase):
    """The same answers read from the system's German and French layouts through UCKeyTranslate,
    without selecting either, so the machine running the tests keeps its own."""

    def chars(self, source_id):
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
        key = objc.objc_object(c_void_p=ctypes.c_void_p.in_dll(lib, "kTISPropertyInputSourceID").value)
        query = Foundation.NSDictionary.dictionaryWithObject_forKey_(source_id, key)
        sources = lib.TISCreateInputSourceList(objc.pyobjc_id(query), True)
        if not sources or not core.CFArrayGetCount(sources):
            self.skipTest(f"{source_id} is not installed")
        try:
            return carbon.layout_chars(core.CFArrayGetValueAtIndex(sources, 0))
        finally:
            core.CFRelease(sources)

    def test_german(self):
        chars = self.chars("com.apple.keylayout.German")
        for code, char in GERMAN.items():
            if code != 0x32:  # the key by Shift differs between ISO and ANSI hardware
                self.assertEqual(chars.get(code), char, hex(code))

    def test_french(self):
        chars = self.chars("com.apple.keylayout.French")
        for code, char in FRENCH.items():
            self.assertEqual(chars.get(code), char, hex(code))

    def test_russian(self):
        for source_id in ("com.apple.keylayout.Russian", "com.apple.keylayout.RussianWin"):
            chars = self.chars(source_id)
            for code, char in RUSSIAN.items():
                self.assertEqual(chars.get(code), char, f"{source_id} {code:#x}")

    def test_dead_keys_are_marked(self):
        # German's 0x18 is ´, which starts an accent rather than typing.
        chars = self.chars("com.apple.keylayout.German")
        self.assertIn(0x18, chars)
        self.assertIsNone(chars[0x18])

    def test_this_macs_own_layout_reads(self):
        self.assertTrue(keyboard_layout.refresh())
        keyboard_layout.install({})
