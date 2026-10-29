"""Dead accents survive separate Mac tap events without native framework imports."""

from types import SimpleNamespace
import ctypes
import sys
import unittest
from unittest.mock import patch

from core import capture_types, protocol
from core.tests.test_keypad_identity import mac_translator
from core.tests.app_methods import load_methods


class MacDeadKeyTests(unittest.TestCase):
    def setUp(self):
        self.translator, self.injector = mac_translator()
        self.layout = self.injector.keyboard_layout
        self.namespace = self.translator.key_result.__globals__
        self.namespace.update(keyboard_layout=self.layout, CHORD_MODIFIER_KEY_CODES={54, 55, 58, 59, 61, 62})
        self.translator.modifier_down = set()
        self.translator._unicode_character = lambda event, code: event.get("unicode")
        self.translator.quartz.CGEventGetFlags = lambda event: event.get("flags", 0)
        # Spanish ISO: acute and shifted diaeresis share a key; grave is separate.
        self.translations = {(39, 0): capture_types.DeadKey("´"),
                             (39, 2): capture_types.DeadKey("¨"),
                             (50, 0): capture_types.DeadKey("`"),
                             (14, 0): "e", (14, 2): "E", (0, 0): "a",
                             (32, 0): "u", (34, 0): "i", (31, 0): "o", (49, 0): " ", (7, 0): "x"}
        self.layout.install({39: None, 50: None}, self.translations)
        self.addCleanup(self.layout.install, {})

    def feed(self, code, down=True, repeat=False, text=None, flags=0, compose=True):
        return self.translator.key_result(10 if down else 11,
            {9: code, 8: int(repeat), "unicode": text, "flags": flags}, None, {}, compose=compose).messages

    def accent(self, accent_code=39, flags=0):
        self.assertEqual(self.feed(accent_code, flags=flags), [])
        self.assertEqual(self.feed(accent_code, False, flags=flags), [])

    def test_separate_dead_press_and_release_compose_each_reported_accent(self):
        for dead, flags, base, want in ((39, 0, 0, "á"), (39, 0, 14, "é"),
                                      (39, 0, 34, "í"), (39, 0, 31, "ó"), (39, 0, 32, "ú"),
                                      (39, 1 << 17, 32, "ü"), (50, 0, 0, "à")):
            with self.subTest(want=want):
                # The tap can report just the base after a pause, or an already composed char.
                for raw in (self.translations[base, 0], want):
                    self.accent(dead, flags)
                    self.assertEqual(self.feed(base, text=raw), [{"type": "text", "data": {"text": want}}])
                    self.assertEqual(self.feed(base, False, text="x"), [])

    def test_space_repeated_dead_key_and_unsupported_base_preserve_visible_text(self):
        self.accent()
        self.assertEqual(self.feed(49), [{"type": "text", "data": {"text": "´"}}])
        self.feed(49, False)
        self.accent()
        self.assertEqual(self.feed(39), [{"type": "text", "data": {"text": "´"}}])
        self.feed(39, False)
        self.accent()
        self.assertEqual(self.feed(7, text="x"), [{"type": "text", "data": {"text": "´x"}}])
        self.assertEqual(self.feed(7, repeat=True, text="x"), [{"type": "text", "data": {"text": "x"}}])
        self.assertEqual(self.feed(7, False), [])

    def test_shortcut_cancellation_and_local_or_layout_changes_clear_the_pending_accent(self):
        self.accent()
        self.translator.modifier_down.add(55)
        self.assertEqual(self.feed(14, text="e")[0]["data"]["key"], "e")
        self.feed(14, False)
        self.translator.modifier_down.clear()
        self.accent()
        self.feed(0, compose=False, text="a")
        self.feed(0, False, compose=False, text="a")
        self.assertEqual(self.feed(14, text="e")[0]["data"]["key"], "e")
        self.feed(14, False)
        self.accent()
        self.layout.install({})
        self.assertEqual(self.feed(14, text="e")[0]["data"]["key"], "e")

    def test_composed_text_survives_the_wire_and_home_filter_without_a_key_lookup(self):
        self.accent()
        message = self.feed(14, text="e")[0]
        message["data"]["seq"] = 1
        read = protocol.read_input(protocol.parse_message(protocol.dumps_message(message)))
        self.assertEqual(read["text"], "é")
        cls = load_methods("mac_app/bridge.py", "KVMController", ["_after_home_presses"], {"protocol": protocol})
        controller = cls()
        controller._home_presses = set()
        controller.redirecting = False
        controller.keyboard_redirecting = False
        self.assertEqual(controller._after_home_presses([message]), [])
        controller.redirecting = True
        controller.keyboard_redirecting = True
        self.assertEqual(controller._after_home_presses([message]), [message])

    def test_option_pressed_during_an_ordinary_hold_cannot_swallow_its_release(self):
        self.assertEqual(self.feed(14, text="e")[0]["data"]["key"], "e")
        self.translations[14, 8] = capture_types.DeadKey("´")
        self.layout.install({39: None, 50: None}, self.translations)
        self.assertEqual(self.feed(14, repeat=True, flags=1 << 19)[0]["data"]["key"], "e")
        self.assertEqual(self.feed(14, False, flags=1 << 19)[0]["type"], "keyup")
        self.assertEqual(self.translator.printable_down, {})

    def test_ctrl_during_composed_hold_turns_repeats_into_balanced_shortcuts(self):
        self.accent()
        self.feed(14, text="e")
        self.translator.modifier_down.add(59)
        self.assertEqual(self.feed(14, repeat=True, text="e"),
                         [{"type": "keydown", "data": {"key": "e", "us": "e"}}])
        self.assertEqual(self.feed(14, False, text="e"),
                         [{"type": "keyup", "data": {"key": "e", "us": "e"}}])
        self.assertEqual(self.translator.printable_down, {})

    @unittest.skipUnless(sys.platform == "darwin", "reads installed macOS layouts without selecting them")
    def test_native_spanish_and_german_dead_key_tables_compose_without_mac_dead_state(self):
        carbon = self.layout._Carbon()
        lib, core = carbon.carbon, carbon.core
        lib.TISCreateInputSourceList.restype = ctypes.c_void_p
        lib.TISCreateInputSourceList.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        core.CFArrayGetCount.restype = ctypes.c_long
        core.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        core.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        core.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        core.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        id_key = ctypes.c_void_p.in_dll(lib, "kTISPropertyInputSourceID").value
        sources = lib.TISCreateInputSourceList(None, True)
        found = set()
        try:
            for index in range(core.CFArrayGetCount(sources)):
                source = core.CFArrayGetValueAtIndex(sources, index)
                name_ref = lib.TISGetInputSourceProperty(source, id_key)
                name = ctypes.create_string_buffer(256)
                if not name_ref or not core.CFStringGetCString(name_ref, name, len(name), 0x08000100):
                    continue
                source_id = name.value.decode()
                if source_id not in ("com.apple.keylayout.Spanish-ISO", "com.apple.keylayout.Spanish",
                                      "com.apple.keylayout.German"):
                    continue
                translations = carbon.layout_translations(source)
                self.layout.install(carbon.layout_chars(source), translations)
                accent = next(code for (code, mods), value in translations.items()
                              if mods == 0 and isinstance(value, capture_types.DeadKey) and value.character == "´")
                base = next(code for (code, mods), value in translations.items() if mods == 0 and value == "e")
                self.translator.dead_keys.reset()
                self.accent(accent)
                self.assertEqual(self.feed(base, text="e"), [{"type": "text", "data": {"text": "é"}}])
                self.feed(base, False)
                found.add(source_id)
        finally:
            core.CFRelease(sources)
        self.assertIn("com.apple.keylayout.Spanish-ISO", found)
        self.assertIn("com.apple.keylayout.German", found)

    def test_dead_key_with_a_combining_mark_composes_from_it(self):
        # US International's acute is an ASCII apostrophe with no "COMBINING APOSTROPHE"; the mark
        # read from the layout composes á where inferring from the spacing char would give "'a".
        self.layout.install({39: None}, {(39, 0): capture_types.DeadKey("'", "́"),
                                         (0, 0): "a", (7, 0): "x", (49, 0): " "})
        self.accent(39)
        self.assertEqual(self.feed(0, text="a"), [{"type": "text", "data": {"text": "á"}}])
        self.feed(0, False, text="x")
        # A base the mark cannot combine with keeps the spacing accent and the letter, visibly.
        self.accent(39)
        self.assertEqual(self.feed(7, text="x"), [{"type": "text", "data": {"text": "'x"}}])
        self.feed(7, False, text="x")
        self.accent(39)
        self.assertEqual(self.feed(49), [{"type": "text", "data": {"text": "'"}}])
        self.feed(49, False)

    @unittest.skipUnless(sys.platform == "darwin", "reads installed macOS layouts without selecting them")
    def test_us_international_dead_key_table_composes_despite_ascii_spacing(self):
        carbon = self.layout._Carbon()
        lib, core = carbon.carbon, carbon.core
        lib.TISCreateInputSourceList.restype = ctypes.c_void_p
        lib.TISCreateInputSourceList.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        core.CFArrayGetCount.restype = ctypes.c_long
        core.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        core.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        core.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        core.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        id_key = ctypes.c_void_p.in_dll(lib, "kTISPropertyInputSourceID").value
        sources = lib.TISCreateInputSourceList(None, True)
        found = False
        try:
            for index in range(core.CFArrayGetCount(sources)):
                source = core.CFArrayGetValueAtIndex(sources, index)
                name_ref = lib.TISGetInputSourceProperty(source, id_key)
                name = ctypes.create_string_buffer(256)
                if not name_ref or not core.CFStringGetCString(name_ref, name, len(name), 0x08000100):
                    continue
                if name.value.decode() != "com.apple.keylayout.USInternational-PC":
                    continue
                translations = carbon.layout_translations(source)
                self.layout.install(carbon.layout_chars(source), translations)
                # The ASCII-apostrophe acute, the one a US-International user presses, must compose á.
                acute = next(code for (code, mods), value in translations.items()
                             if mods == 0 and isinstance(value, capture_types.DeadKey) and value.character == "'")
                base = next(code for (code, mods), value in translations.items() if mods == 0 and value == "a")
                self.translator.dead_keys.reset()
                self.accent(acute)
                self.assertEqual(self.feed(base, text="a"), [{"type": "text", "data": {"text": "á"}}])
                self.feed(base, False)
                found = True
        finally:
            core.CFRelease(sources)
        if not found:
            self.skipTest("US International is not installed")


if __name__ == "__main__":
    unittest.main()
