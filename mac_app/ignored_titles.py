"""Display names for ignored_inputs entries, and the pure decision behind recording one -- factored
out of the AppKit recorder in widgets.py so both are testable without it.
"""

from core import ignored
import media_keys
from bridge import NX_DEVICE_MODIFIER_BITS, WIRE_OTHER_BUTTONS
import keyboard_layout
from key_codes import SPECIAL_KEY_NAMES, key_title

BUTTON_TITLES = {
    "right": "Right button",
    "middle": "Middle button",
    "back": "Back button",
    "forward": "Forward button",
}

MEDIA_TITLES = {
    "volume_up": "Volume Up",
    "volume_down": "Volume Down",
    "volume_mute": "Mute",
    "media_play_pause": "Play/Pause",
    "media_next": "Next Track",
    "media_prev": "Previous Track",
}


def entry_title(entry):
    """The name Beamer shows for one entry in ignored_inputs."""
    kind, _, value = entry.partition(":")
    if kind == "key":
        code = int(value)
        name = SPECIAL_KEY_NAMES.get(code)
        if name is not None:
            return key_title(name)
        fallback = keyboard_layout.char_for(code)
        return fallback.upper() if fallback is not None else f"Key {code}"
    if kind == "button":
        return BUTTON_TITLES.get(value, f"Button {value}")
    if kind == "media":
        return MEDIA_TITLES.get(value, value)
    return entry


# Shift, Control, Option and Command, left and right, to the flag their family sets: the same
# numbers in NSEvent's modifierFlags and CoreGraphics' event flags.
FAMILY_FLAGS = {0x38: 1 << 17, 0x3C: 1 << 17, 0x3B: 1 << 18, 0x3E: 1 << 18, 0x3A: 1 << 19, 0x3D: 1 << 19, 0x37: 1 << 20, 0x36: 1 << 20}


def is_modifier_release(keycode, flags):
    """Whether a FlagsChanged event is a modifier going up, which the recorder listens past: a
    modifier already held when recording starts would otherwise be recorded on its release. Read
    the way the event tap reads it: the key's own device bit where the event carries any, else its
    family's bit, which is also what a release with nothing else held carries -- none."""
    keycode, flags = int(keycode), int(flags)
    family = FAMILY_FLAGS.get(keycode)
    if family is None:
        return False
    # The device bits are distinct, so their sum is their union.
    if flags & sum(NX_DEVICE_MODIFIER_BITS.values()):
        return not flags & NX_DEVICE_MODIFIER_BITS[keycode]
    return not flags & family


def recorded_entry(kind, value, trigger_code):
    """The ignored_inputs entry one captured press becomes, or None. `kind` is "key" (a keycode
    from a KeyDown or FlagsChanged event), "right" (the right mouse button, `value` unused),
    "other" (another mouse button, `value` its CoreGraphics button number) or "media" (a
    NX_SUBTYPE_AUX_CONTROL_BUTTONS system-defined event, `value` its data1). None means: the
    trigger key, which always stays with Beamer, or a media event that is not a recognised
    key-down, which the recorder keeps listening past rather than finishing on."""
    if kind == "key":
        code = int(value)
        return None if code == trigger_code else ignored.key(code)
    if kind == "right":
        return ignored.button("right")
    if kind == "other":
        number = int(value)
        return ignored.button(WIRE_OTHER_BUTTONS.get(number, str(number + 1)))
    if kind == "media":
        decoded = media_keys.decode(media_keys.NX_SUBTYPE_AUX_CONTROL_BUTTONS, int(value))
        if decoded is None:
            return None
        name, is_down, _is_repeat = decoded
        return ignored.media(name) if is_down else None
    return None
