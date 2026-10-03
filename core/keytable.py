"""The key table of WIRE.md section 7: which wire name a physical key sends to a peer.

Pure. A physical name says which key was pressed on the sender's own keyboard, not what 1.4.x's
Windows capture called it: "ctrl" is the Control key and "cmd" the Command, Windows or Super key,
each with an "_r" for the right-hand one. Everything else goes out as itself."""

from typing import Mapping, Optional, Union

MAC_PLATFORMS = frozenset({"macos", "ios"})

STYLES = ("semantic", "positional", "mac_layout")

# Across families, Semantic: the everyday shortcuts line up (Command-C arrives as Control-C).
# 1.4.x's DEFAULT_KEY_MAP on the Mac and the Windows capture names on Windows.
_MAC_SEMANTIC = {"ctrl": "cmd", "ctrl_r": "cmd", "cmd": "ctrl", "cmd_r": "ctrl", "alt": "alt", "alt_r": "alt"}
_MAC_POSITIONAL = {"ctrl": "ctrl", "ctrl_r": "ctrl", "cmd": "cmd", "cmd_r": "cmd", "alt": "alt", "alt_r": "alt"}
_PC_SEMANTIC = {"ctrl": "cmd", "ctrl_r": "cmd_r", "cmd": "ctrl", "cmd_r": "ctrl_r"}
_PC_POSITIONAL: dict = {}
_PC_MAC_LAYOUT = {
    "ctrl": "ctrl", "ctrl_r": "ctrl_r",
    "cmd": "alt", "cmd_r": "alt_r",
    "alt": "cmd", "alt_r": "cmd_r",
}

_ACROSS = {
    (True, "semantic"): _MAC_SEMANTIC,
    (True, "positional"): _MAC_POSITIONAL,
    (True, "mac_layout"): _MAC_SEMANTIC,
    (False, "semantic"): _PC_SEMANTIC,
    (False, "positional"): _PC_POSITIONAL,
    (False, "mac_layout"): _PC_MAC_LAYOUT,
}


def is_mac(platform: str) -> bool:
    return platform in MAC_PLATFORMS


def wire_name(physical: str, own_platform: str, peer_platform: str, style: Union[str, Mapping[str, str]]) -> str:
    """The name `physical` travels as from `own_platform` to `peer_platform`.

    `style` is "semantic" or "positional", or a map a user wrote by hand (the Mac's `key_map`),
    which applies across families only."""
    own_mac = is_mac(own_platform)
    if isinstance(style, str):
        if style not in STYLES:
            raise ValueError(f"key style must be one of: {', '.join(STYLES)}")
        table = _ACROSS[(own_mac, style)]
    else:
        table = style
    if own_mac == is_mac(peer_platform):
        return physical
    return table.get(physical, physical)


# A Linux sender reads evdev codes (linux/input-event-codes.h). Modifiers first, then the named
# keys of section 7 that have a code. The numpad Enter is `enter`, so two codes share it.
EVDEV_NAMES = {
    29: "ctrl", 97: "ctrl_r",
    125: "cmd", 126: "cmd_r",
    56: "alt", 100: "alt_r",
    42: "shift", 54: "shift_r",
    58: "caps_lock",
    14: "backspace", 15: "tab", 28: "enter", 96: "enter", 1: "esc", 57: "space",
    111: "delete", 110: "insert", 102: "home", 107: "end", 104: "page_up", 109: "page_down",
    105: "left", 106: "right", 103: "up", 108: "down",
    127: "menu", 99: "print_screen", 119: "pause", 69: "num_lock", 70: "scroll_lock",
    158: "browser_back", 159: "browser_forward",
    113: "volume_mute", 114: "volume_down", 115: "volume_up",
    163: "media_next", 165: "media_prev", 166: "media_stop", 164: "media_play_pause",
}
EVDEV_NAMES.update({59 + i: f"f{i + 1}" for i in range(10)})
EVDEV_NAMES.update({87: "f11", 88: "f12"})
EVDEV_NAMES.update({183 + i: f"f{13 + i}" for i in range(12)})

# What each key types on a US keyboard, by evdev code, which names the key's place rather than its
# label: `us` in a key message. Codes 1 to 88 are the PC's set-1 scan codes, so Windows reads this
# table by scan code.
EVDEV_US = dict(zip(range(0x02, 0x0E), "1234567890-="))
EVDEV_US.update(zip(range(0x10, 0x1C), "qwertyuiop[]"))
EVDEV_US.update(zip(range(0x1E, 0x2A), "asdfghjkl;'`"))
EVDEV_US.update(zip(range(0x2B, 0x36), "\\zxcvbnm,./"))

SPEC_NAMES = frozenset(
    {"shift", "shift_r", "ctrl", "ctrl_r", "alt", "alt_r", "cmd", "cmd_r", "caps_lock",
     "backspace", "tab", "enter", "esc", "space", "delete", "insert", "home", "end", "page_up", "page_down",
     "left", "right", "up", "down", "menu", "print_screen", "pause", "num_lock", "scroll_lock",
     "browser_back", "browser_forward",
     "volume_mute", "volume_down", "volume_up", "media_next", "media_prev", "media_stop", "media_play_pause"}
    | {f"f{i}" for i in range(1, 25)}
)


def evdev_physical(code: int) -> Optional[str]:
    """The physical name of an evdev key code, or None for a character key or a code with no name."""
    return EVDEV_NAMES.get(code)
