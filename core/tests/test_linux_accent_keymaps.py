"""Pure libxkbcommon proof against installed layouts and compose data; no display or GUI."""

import ctypes
import ctypes.util
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "win_app"))

import inject_portal
import inject_x11
import xkb_x11


@unittest.skipUnless(sys.platform.startswith("linux"),
                     "requires Linux's installed XKB layouts and compose data")
class InstalledLayoutTests(unittest.TestCase):
    def keymap(self, symbols):
        keymap = xkb_x11.Keymap.from_text('''xkb_keymap {
            xkb_keycodes { include "evdev+aliases(qwerty)" };
            xkb_types { include "complete" };
            xkb_compat { include "complete" };
            xkb_symbols { include "pc+%s+inet(evdev)" };
        };''' % symbols)
        self.addCleanup(keymap.close)
        return keymap

    def typed(self, steps, keymap, locks):
        lib = keymap._xkb
        p, u32 = ctypes.c_void_p, ctypes.c_uint32
        lib.xkb_compose_table_new_from_locale.restype = p
        lib.xkb_compose_table_new_from_locale.argtypes = [p, ctypes.c_char_p, ctypes.c_int]
        lib.xkb_compose_state_new.restype = p
        lib.xkb_compose_state_new.argtypes = [p, ctypes.c_int]
        lib.xkb_compose_state_feed.argtypes = [p, u32]
        lib.xkb_compose_state_get_status.argtypes = [p]
        lib.xkb_compose_state_get_utf8.argtypes = [p, ctypes.c_char_p, ctypes.c_size_t]
        lib.xkb_compose_state_reset.argtypes = [p]
        lib.xkb_compose_state_unref.argtypes = [p]
        lib.xkb_compose_table_unref.argtypes = [p]
        table = lib.xkb_compose_table_new_from_locale(keymap._context, b"en_US.UTF-8", 0)
        self.assertTrue(table)
        compose = lib.xkb_compose_state_new(table, 0)
        held, output = set(), []
        try:
            for code, down in inject_portal.wayland_steps(steps):
                code += 8
                if not down:
                    held.discard(code)
                    continue
                held.add(code)
                mask = locks
                for modifier in held:
                    mask |= keymap.modifier_mask(modifier)
                symbol = keymap.keysym(code, mask, 0)
                lib.xkb_compose_state_feed(compose, symbol)
                status = lib.xkb_compose_state_get_status(compose)
                if status == 2:  # XKB_COMPOSE_COMPOSED
                    buffer = ctypes.create_string_buffer(64)
                    lib.xkb_compose_state_get_utf8(compose, buffer, len(buffer))
                    output.append(buffer.value.decode())
                    lib.xkb_compose_state_reset(compose)
                elif status == 0:
                    text = keymap.text(symbol)
                    if text and text.isprintable():
                        output.append(text)
                elif status == 3:
                    self.fail("planned strokes cancelled XKB compose")
            self.assertEqual(held, set(), "input planning left physical keys held")
            return "".join(output)
        finally:
            lib.xkb_compose_state_unref(compose)
            lib.xkb_compose_table_unref(table)

    def test_spanish_latin_american_and_international_layouts_type_accents(self):
        for symbols in ("es", "latam", "us(intl)"):
            for layout_class in (inject_x11.Layout, inject_portal.WaylandLayout):
                for locks in (0, 2, 16, 18):
                    with self.subTest(symbols=symbols, backend=layout_class.__name__, locks=locks):
                        keymap = self.keymap(symbols)
                        state = inject_x11.KeyState()
                        layout = layout_class(keymap)
                        text = "Agustín Cómo imágenes éôü ÉÔÜ €"
                        steps = inject_x11.plan_text(text, state, layout, 0, locks)
                        self.assertFalse(any(step[0] == inject_x11.MAP for step in steps))
                        self.assertEqual(self.typed(steps, keymap, locks), text)

    def test_plain_us_keeps_the_explicit_wayland_limit_and_x11_unicode_remap(self):
        keymap = self.keymap("us")
        self.assertEqual(inject_x11.plan_text("é", inject_x11.KeyState(), inject_portal.WaylandLayout(keymap), 0, 0), [])
        steps = inject_x11.plan_text("é", inject_x11.KeyState(), inject_x11.Layout(keymap), 0, 0)
        self.assertTrue(any(step[0] == inject_x11.MAP and step[2] == 0xe9 for step in steps))


if __name__ == "__main__":
    unittest.main()
