"""The keyboard layout as XKB has it, through libxkbcommon-x11 over ctypes: which keysym a key
gives under given modifiers and layout group, for the X11 capture (naming a key) and injection
(finding the key that types a character). On Wayland the same Keymap is made from the text of the
keymap the compositor hands a libei keyboard (Keymap.from_text), and only libxkbcommon is used.

XKB, not the core keymap: the core keymap holds only two groups in a fixed place, so a third or
fourth layout cannot be read from it, and it knows nothing of key types, so Caps Lock, Num Lock and
the keypad would have to be guessed. libxkbcommon-x11 reads the server's own keymap and answers
exactly as the server would. It and libxcb are what Qt's xcb plugin runs on, so they are on every
machine Beamer runs on under X11.

Each Keymap holds its own xcb connection and is used from one thread (or under one lock); call
reload() after a MappingNotify."""

import ctypes
import ctypes.util
from typing import Optional

_libs = {}


def _load(names=None):
    names = names or {"xcb": "xcb", "xkb": "xkbcommon", "xkb_x11": "xkbcommon-x11"}
    if any(key not in _libs for key in names):
        for key, name in names.items():
            if key in _libs:
                continue
            path = ctypes.util.find_library(name)
            if not path:
                raise RuntimeError(f"lib{name} is not installed; Beamer reads the keyboard layout through it")
            _libs[key] = ctypes.CDLL(path)
        u32, p = ctypes.c_uint32, ctypes.c_void_p
        if "xcb" in names:
            _bind_x11(_libs["xcb"], _libs["xkb_x11"])
        xkb = _libs["xkb"]
        xkb.xkb_keymap_new_from_string.restype = p
        xkb.xkb_keymap_new_from_string.argtypes = [p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        xkb.xkb_context_new.restype = p
        xkb.xkb_context_new.argtypes = [ctypes.c_int]
        xkb.xkb_context_unref.argtypes = [p]
        xkb.xkb_keymap_unref.argtypes = [p]
        xkb.xkb_state_new.restype = p
        xkb.xkb_state_new.argtypes = [p]
        xkb.xkb_state_unref.argtypes = [p]
        xkb.xkb_state_update_mask.argtypes = [p, u32, u32, u32, u32, u32, u32]
        xkb.xkb_state_key_get_one_sym.restype = u32
        xkb.xkb_state_key_get_one_sym.argtypes = [p, u32]
        xkb.xkb_keymap_min_keycode.restype = u32
        xkb.xkb_keymap_min_keycode.argtypes = [p]
        xkb.xkb_keymap_max_keycode.restype = u32
        xkb.xkb_keymap_max_keycode.argtypes = [p]
        xkb.xkb_keymap_num_layouts_for_key.restype = u32
        xkb.xkb_keymap_num_layouts_for_key.argtypes = [p, u32]
        xkb.xkb_keymap_num_layouts.restype = u32
        xkb.xkb_keymap_num_layouts.argtypes = [p]
        xkb.xkb_keymap_mod_get_index.restype = u32
        xkb.xkb_keymap_mod_get_index.argtypes = [p, ctypes.c_char_p]
        xkb.xkb_keysym_to_utf32.restype = u32
        xkb.xkb_keysym_to_utf32.argtypes = [u32]
    return tuple(_libs[key] for key in names)


def _bind_x11(xcb, x11) -> None:
    p = ctypes.c_void_p
    xcb.xcb_connect.restype = p
    xcb.xcb_connect.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
    xcb.xcb_connection_has_error.argtypes = [p]
    xcb.xcb_disconnect.argtypes = [p]
    x11.xkb_x11_setup_xkb_extension.argtypes = [p, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_int, p, p, p, p]
    x11.xkb_x11_get_core_keyboard_device_id.restype = ctypes.c_int32
    x11.xkb_x11_get_core_keyboard_device_id.argtypes = [p]
    x11.xkb_x11_keymap_new_from_device.restype = p
    x11.xkb_x11_keymap_new_from_device.argtypes = [p, p, ctypes.c_int32, ctypes.c_int]


# The core state's eight modifier bits, in XKB's names.
_REAL_MODS = ("Shift", "Lock", "Control", "Mod1", "Mod2", "Mod3", "Mod4", "Mod5")


_FORMAT_TEXT_V1 = 1


class Keymap:
    def __init__(self, display_name: Optional[str] = None) -> None:
        self._xcb, self._xkb, self._x11 = _load()
        self._conn = self._xcb.xcb_connect(display_name.encode() if display_name else None, None)
        self._context = self._keymap = self._state = None
        if not self._conn or self._xcb.xcb_connection_has_error(self._conn):
            self.close()
            raise RuntimeError("Could not open an xcb connection to read the keyboard layout")
        if self._x11.xkb_x11_setup_xkb_extension(self._conn, 1, 0, 0, None, None, None, None) != 1:
            self.close()
            raise RuntimeError("This X server has no XKB extension")
        self._context = self._xkb.xkb_context_new(0)
        self.reload()

    @classmethod
    def from_text(cls, text: str) -> "Keymap":
        """A keymap from its text, as a Wayland compositor hands one to a libei keyboard: no X
        connection, and reload() is not used."""
        self = cls.__new__(cls)
        (self._xkb,) = _load({"xkb": "xkbcommon"})
        self._xcb = self._x11 = self._conn = self._keymap = self._state = None
        self._context = self._xkb.xkb_context_new(0)
        keymap = self._xkb.xkb_keymap_new_from_string(self._context, text.encode(), _FORMAT_TEXT_V1, 0)
        if not keymap:
            self.close()
            raise RuntimeError("Could not read the keyboard layout the desktop gave")
        self._use(keymap)
        return self

    def reload(self) -> None:
        device = self._x11.xkb_x11_get_core_keyboard_device_id(self._conn)
        keymap = self._x11.xkb_x11_keymap_new_from_device(self._context, self._conn, device, 0)
        if not keymap:
            raise RuntimeError("Could not read the keyboard layout")
        self._use(keymap)

    def _use(self, keymap) -> None:
        state = self._xkb.xkb_state_new(keymap)
        if self._state:
            self._xkb.xkb_state_unref(self._state)
        if self._keymap:
            self._xkb.xkb_keymap_unref(self._keymap)
        self._keymap, self._state = keymap, state
        # The core state's bit n is the real modifier XKB names _REAL_MODS[n]; where XKB indexes it
        # is the keymap's business.
        self._mod_bits = []
        for bit, name in enumerate(_REAL_MODS):
            index = self._xkb.xkb_keymap_mod_get_index(keymap, name.encode())
            if index != 0xFFFFFFFF:
                self._mod_bits.append((1 << bit, 1 << index))
        self.min_keycode = self._xkb.xkb_keymap_min_keycode(keymap)
        self.max_keycode = self._xkb.xkb_keymap_max_keycode(keymap)

    def core_mods(self, xkb_mask: int) -> int:
        """XKB's modifier mask (as libei reports one) as the core state's bits."""
        return sum(core for core, index in self._mod_bits if xkb_mask & index)

    def keysym(self, keycode: int, mods: int, group: int) -> int:
        """What `keycode` gives with the core modifier bits `mods` held and layout `group` (0 based)
        active; XKB wraps a group the key does not have as the server does."""
        xkb_mods = 0
        for core, index in self._mod_bits:
            if mods & core:
                xkb_mods |= index
        self._xkb.xkb_state_update_mask(self._state, xkb_mods, 0, 0, 0, 0, group)
        return self._xkb.xkb_state_key_get_one_sym(self._state, keycode)

    def empty(self, keycode: int) -> bool:
        return self._xkb.xkb_keymap_num_layouts_for_key(self._keymap, keycode) == 0

    def text(self, keysym: int) -> Optional[str]:
        point = self._xkb.xkb_keysym_to_utf32(keysym) if keysym else 0
        return chr(point) if point else None

    def close(self) -> None:
        if self._state:
            self._xkb.xkb_state_unref(self._state)
        if self._keymap:
            self._xkb.xkb_keymap_unref(self._keymap)
        if self._context:
            self._xkb.xkb_context_unref(self._context)
        if self._conn and self._xcb:
            self._xcb.xcb_disconnect(self._conn)
        self._state = self._keymap = self._context = self._conn = None
