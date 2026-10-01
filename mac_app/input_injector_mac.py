"""Quartz CGEvent keyboard and mouse injection, the Mac twin of
win_app/input_injector.py.

`receiver.py` calls the same five functions on either machine. The wire's key
names are already Mac-shaped -- "cmd" is Command here and the Windows key
there -- so the Windows capture does the semantic mapping before sending and
nothing is translated on arrival.

Every event posted from here carries INJECTED_MARK in its source user data,
which is how bridge.py's event tap tells Beamer's own injection apart from
a real hand on the Mac's own keyboard or trackpad: without that the tap
would read a pointer the PC is driving as a push against the Mac's crossing
edge and switch input straight back.

Quartz is imported at module level and guarded the same way clipboard_mac
guards AppKit, so this module stays importable, and its behaviour fakeable,
on a machine without it.
"""

import functools
import logging
import threading
import time
import unicodedata
from typing import Dict, List, Optional, Set, Tuple

import keyboard_layout
from key_codes import KEY_NAME_TO_CODE, US_KEY_CODES

try:
    import Quartz
except ImportError:  # pragma: no cover - exercised only off macOS
    Quartz = None

try:
    import AppKit
except ImportError:  # pragma: no cover - exercised only off macOS
    AppKit = None

from core import protocol

LOGGER = logging.getLogger("Beamer")

# Stamped into every injected event's kCGEventSourceUserData. Any non-zero
# value would do; this one is recognisable in a log.
INJECTED_MARK = 0xBEA3

MODIFIER_FLAG_NAMES = {
    "cmd": "kCGEventFlagMaskCommand",
    "cmd_r": "kCGEventFlagMaskCommand",
    "shift": "kCGEventFlagMaskShift",
    "shift_r": "kCGEventFlagMaskShift",
    "alt": "kCGEventFlagMaskAlternate",
    "alt_r": "kCGEventFlagMaskAlternate",
    "ctrl": "kCGEventFlagMaskControl",
    "ctrl_r": "kCGEventFlagMaskControl",
    "caps_lock": "kCGEventFlagMaskAlphaShift",
}
# The modifiers that make a keystroke a shortcut rather than text. Shift is
# not one of them: a shifted character arrives as the character it produced.
CHORD_MODIFIERS = {"cmd", "cmd_r", "alt", "alt_r", "ctrl", "ctrl_r"}

BUTTONS = ("left", "right", "middle", "back", "forward")
# How close together, in seconds and pixels, two clicks must be for the
# second to count as a double click. Matches macOS's own default interval;
# a click whose clickState is never set past 1 can never open a file.
DOUBLE_CLICK_SECONDS = 0.5
DOUBLE_CLICK_SLOP_PX = 5

# Wheel units per line of "line"-mode scroll. Both platforms call one wheel
# notch a line, so this is 1; it is named rather than inlined because it is
# the knob to turn if a mouse wheel feels wrong on the Mac.
LINES_PER_NOTCH = 1.0

# Media keys are not key events on a Mac: they arrive as NSSystemDefined events of subtype 8, the
# reverse of media_keys.decode. The NX key codes are IOKit's ev_keymap.h. There is no stop key on
# a Mac, and mapping it to play/pause would start playback that was paused, so it is dropped.
NX_SUBTYPE_AUX_CONTROL_BUTTONS = 8
NX_SYSDEFINED_EVENT_TYPE = 14
MEDIA_NX_KEYS = {
    "volume_up": 0,
    "volume_down": 1,
    "volume_mute": 7,
    "media_play_pause": 16,
    "media_next": 17,
    "media_prev": 18,
}
_MEDIA_STATE_DOWN = 0x0A
_MEDIA_STATE_UP = 0x0B

# Keys a PC has and a Mac keyboard does not, at the place an Apple extended keyboard puts them:
# F13, F14 and F15 are Print Screen, Scroll Lock and Pause, and the keypad's Clear is Num Lock.
WINDOWS_ONLY_KEY_CODES = {
    "print_screen": 0x69,
    "scroll_lock": 0x6B,
    "pause": 0x71,
    "num_lock": 0x47,
}
# The PC's browser back and forward keys, as the shortcut every Mac browser and Finder answers to.
BROWSER_KEY_CHORDS = {"browser_back": "[", "browser_forward": "]"}

# What each system gesture becomes: the shortcuts System Settings ships for them. Fingers moving
# left reveal the Space on the right, so swipe_left is Control+Right. Pinch was Launchpad, which
# macOS 26 removed along with its shortcut, so it has nothing to press.
GESTURE_CHORDS = {
    protocol.GESTURE_SWIPE_UP: ("ctrl", "up"),
    protocol.GESTURE_SWIPE_DOWN: ("ctrl", "down"),
    protocol.GESTURE_SWIPE_LEFT: ("ctrl", "right"),
    protocol.GESTURE_SWIPE_RIGHT: ("ctrl", "left"),
    protocol.GESTURE_SPREAD: ("f11",),
}

# A real arrow, function key, Home, End, Page Up, Page Down or forward delete event carries the Fn
# flag, and the arrows the numeric-pad flag too; system shortcuts (Control+arrows for Spaces, F11
# for Show Desktop) match on it, and a keyboard event made from nothing does not have it.
_ARROW_CODES = {KEY_NAME_TO_CODE[name] for name in ("left", "right", "up", "down")}
_FN_CODES = _ARROW_CODES | {KEY_NAME_TO_CODE[name] for name in ("home", "end", "page_up", "page_down", "delete")}
_FN_CODES |= {KEY_NAME_TO_CODE[f"f{number}"] for number in range(1, 21)}

_mods_down: Set[str] = set()
_held_codes: Dict[str, int] = {}
_buttons_down: Set[str] = set()
_last_click: Dict[str, Tuple[float, int, int, int]] = {}
_warned_names: Set[str] = set()
# The fraction of a wheel step not yet posted, per unit and axis (vertical, horizontal): a wheel
# event carries whole numbers only, so a scroll speed under one, or a trackpad's small deltas,
# would otherwise round to nothing every time.
_scroll_remainders: Dict[str, List[float]] = {"line": [0.0, 0.0], "pixel": [0.0, 0.0]}
# One lock over the held-key bookkeeping and the post it describes: a session
# thread injecting while a reconnect's release_all snapshots and clears would
# otherwise press a key that nothing then remembers to release.
_state_lock = threading.RLock()


def _locked(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with _state_lock:
            return function(*args, **kwargs)

    return wrapper


def _require():
    if Quartz is None:
        raise RuntimeError("Quartz event injection is only available on macOS")
    return Quartz


def current_flags(mods_down: Set[str]) -> int:
    quartz = _require()
    flags = 0
    for name in mods_down:
        attribute = MODIFIER_FLAG_NAMES.get(name)
        if attribute is not None:
            flags |= getattr(quartz, attribute)
    return flags


def key_flags(key_code: int) -> int:
    quartz = _require()
    flags = 0
    if key_code in _FN_CODES:
        flags |= quartz.kCGEventFlagMaskSecondaryFn
    if key_code in _ARROW_CODES:
        flags |= quartz.kCGEventFlagMaskNumericPad
    return flags


def _script(character: str) -> str:
    return unicodedata.name(character, "").split(" ")[0]


def _by_place(name: str, us: Optional[str], mods_down: Set[str]) -> Optional[int]:
    """The key at `us`, the place the sender's key sits on a US keyboard, for a character this
    layout cannot place: under a chord, which has to reach the app as a shortcut, and for a letter
    of another script than the one this layout types there, so a PC on Russian types this Mac's
    English and one on English types its Russian, as the Mac's own keyboard would. Anything else,
    an é from a French PC say, stays text: the key it sits on types something unrelated here."""
    code = US_KEY_CODES.get(us)
    if code is None:
        return None
    if mods_down & CHORD_MODIFIERS:
        return code
    here = keyboard_layout.char_for(code)
    if name.isalpha() and here and here.isalpha() and _script(name) != _script(here):
        return code
    return None


def plan_key_event(name: str, down: bool, mods_down: Set[str], held: Optional[Dict[str, int]] = None, us: Optional[str] = None) -> Optional[Tuple[int, Optional[str]]]:
    """Pure planning: (key code, unicode string or None) for one key, or None
    when there is nothing to send. Mutates `mods_down` exactly as inject_key
    does, so a test can inspect the bookkeeping. `held` keeps the key each
    character went down on, so its release lets go of that key even if the
    layout changed in between. `us` is where the key sits on a US keyboard,
    sent by a physical keyboard and absent from a phone's."""
    lowered = name.lower()
    if lowered in MODIFIER_FLAG_NAMES:
        # Recorded before the event is built, so the modifier's own event
        # already carries the flag it sets -- which is what a flagsChanged
        # event does on real hardware.
        if down:
            mods_down.add(lowered)
        else:
            mods_down.discard(lowered)
    key_code = KEY_NAME_TO_CODE.get(lowered)
    if key_code is None:
        key_code = WINDOWS_ONLY_KEY_CODES.get(lowered)
    if key_code is not None:
        return key_code, None
    if len(name) != 1:
        if name not in _warned_names:
            LOGGER.warning("Unknown key name ignored: %r", name)
            _warned_names.add(name)
        return None
    # The key that types it on this Mac's own layout, so a chord like Cmd+C lands on the right
    # key, and a plain "z" from the PC is not posted as the US Z key, which types "y" on a
    # German Mac. keyboard_layout is also what bridge.py reads keys with, so the two directions
    # agree about which key is which.
    if held is not None and name in held:
        # A repeat stays on the key the press went down on, even if the layout changed since, so
        # its release lets go of the key that is actually down.
        return (held[name] if down else held.pop(name)), None
    key_code = keyboard_layout.code_for(name.lower())
    if key_code is None:
        key_code = _by_place(name, us, mods_down)
        if key_code is not None and name.isupper() and not mods_down:
            # A capital from caps lock: posting the key would type this layout's small letter.
            return 0, keyboard_layout.char_for(key_code).upper()
    if key_code is not None and (name.islower() or not name.isalpha() or mods_down):
        if held is not None and down:
            held[name] = key_code
        return key_code, None
    # A character this layout table cannot place -- an accent, an em dash, a
    # capital with no shift held. Typed as text: it lands correctly in a text
    # field, which is all a character with no chord on it has to do.
    return 0, name


def handles_key(name: str) -> bool:
    """Whether `name` is a key this module posts by some route other than a key code."""
    lowered = name.lower()
    return lowered in MEDIA_NX_KEYS or lowered in BROWSER_KEY_CHORDS


def _inject_media_key(nx_key: int, down: bool) -> None:
    if AppKit is None:
        raise RuntimeError("Media keys need AppKit, which is only available on macOS")
    state = _MEDIA_STATE_DOWN if down else _MEDIA_STATE_UP
    ns_event = AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
        NX_SYSDEFINED_EVENT_TYPE,
        (0, 0),
        state << 8,
        0,
        0,
        None,
        NX_SUBTYPE_AUX_CONTROL_BUTTONS,
        (nx_key << 16) | (state << 8),
        -1,
    )
    event = None if ns_event is None else ns_event.CGEvent()
    if event is None:
        raise RuntimeError(f"NSEvent failed for media key {nx_key}")
    _post(event)


def _inject_browser_key(character: str) -> None:
    quartz = _require()
    # The US key, not this layout's: a German or French layout has no unshifted bracket, and its
    # plan would be key code 0, which under Command is Select All.
    code = US_KEY_CODES[character]
    flags = current_flags(_mods_down) | quartz.kCGEventFlagMaskCommand
    for down in (True, False):
        event = quartz.CGEventCreateKeyboardEvent(None, code, down)
        if event is None:
            raise RuntimeError(f"CGEventCreateKeyboardEvent failed for {character!r}")
        quartz.CGEventSetFlags(event, flags)
        _post(event)


@_locked
def inject_key(name: str, down: bool, us: Optional[str] = None) -> None:
    quartz = _require()
    lowered = name.lower()
    if lowered in MEDIA_NX_KEYS:
        _inject_media_key(MEDIA_NX_KEYS[lowered], bool(down))
        return
    if lowered in BROWSER_KEY_CHORDS:
        # One shortcut per press: the release has nothing left to let go of.
        if down:
            _inject_browser_key(BROWSER_KEY_CHORDS[lowered])
        return
    plan = plan_key_event(name, down, _mods_down, _held_codes, us)
    if plan is None:
        return
    key_code, text = plan
    event = quartz.CGEventCreateKeyboardEvent(None, key_code, bool(down))
    if event is None:
        raise RuntimeError(f"CGEventCreateKeyboardEvent failed for {name!r}")
    if text is None:
        quartz.CGEventSetFlags(event, current_flags(_mods_down) | key_flags(key_code))
    else:
        quartz.CGEventKeyboardSetUnicodeString(event, len(text), text)
        # Flags are deliberately cleared: the string says what to type, and
        # leaving shift set would have some apps type it twice over.
        quartz.CGEventSetFlags(event, 0)
    _post(event)


def clamp_to_displays(x: int, y: int, rects) -> Tuple[int, int]:
    """The nearest point inside a display to (x, y). A move is clamped to the
    display it is already on where it can be, so a pointer on a shorter
    monitor cannot be pushed into the empty space beside a taller one and
    stick there -- which is the same outer-edge rule the return edge uses."""
    if not rects:
        return x, y
    for rect in rects:
        if rect.x <= x <= rect.right and rect.y <= y <= rect.bottom:
            return x, y
    best = None
    for rect in rects:
        cx = min(max(x, rect.x), rect.right)
        cy = min(max(y, rect.y), rect.bottom)
        distance = (cx - x) ** 2 + (cy - y) ** 2
        if best is None or distance < best[0]:
            best = (distance, cx, cy)
    return best[1], best[2]


def plan_click_state(button: str, x: int, y: int, now: float, last_click: Dict) -> int:
    """Pure planning: the clickState for this press -- 2 for a double click,
    3 for a triple -- given where and when the last one landed. Mutates
    `last_click`, as inject_mouse_button does."""
    previous = last_click.get(button)
    state = 1
    if previous is not None:
        when, px, py, previous_state = previous
        if (
            now - when <= DOUBLE_CLICK_SECONDS
            and abs(px - x) <= DOUBLE_CLICK_SLOP_PX
            and abs(py - y) <= DOUBLE_CLICK_SLOP_PX
        ):
            state = min(previous_state + 1, 3)
    last_click[button] = (now, x, y, state)
    return state


MOVE_EVENTS = {
    None: "kCGEventMouseMoved",
    "left": "kCGEventLeftMouseDragged",
    "right": "kCGEventRightMouseDragged",
    "middle": "kCGEventOtherMouseDragged",
    "back": "kCGEventOtherMouseDragged",
    "forward": "kCGEventOtherMouseDragged",
}
BUTTON_EVENTS = {
    "left": ("kCGEventLeftMouseDown", "kCGEventLeftMouseUp", "kCGMouseButtonLeft"),
    "right": ("kCGEventRightMouseDown", "kCGEventRightMouseUp", "kCGMouseButtonRight"),
    "middle": ("kCGEventOtherMouseDown", "kCGEventOtherMouseUp", "kCGMouseButtonCenter"),
    # The side buttons have no CoreGraphics constant; they are other buttons 3 and 4.
    "back": ("kCGEventOtherMouseDown", "kCGEventOtherMouseUp", 3),
    "forward": ("kCGEventOtherMouseDown", "kCGEventOtherMouseUp", 4),
}


# CGEventKeyboardSetUnicodeString carries a short string; longer text goes as several events.
TEXT_CHUNK_UNITS = 16


def text_chunks(text: str) -> List[str]:
    """`text` cut so that no chunk is longer than TEXT_CHUNK_UNITS UTF-16 units and a character
    outside the BMP, which takes two, is never split."""
    chunks, current, units = [], [], 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > TEXT_CHUNK_UNITS:
            chunks.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += size
    if current:
        chunks.append("".join(current))
    return chunks


@_locked
def inject_text(text: str) -> None:
    """Type `text` as characters rather than keys (WIRE.md section 10): each chunk is a key event
    that carries its string, with no flags so a held modifier cannot turn it into a shortcut."""
    quartz = _require()
    for chunk in text_chunks(text):
        for down in (True, False):
            event = quartz.CGEventCreateKeyboardEvent(None, 0, down)
            if event is None:
                raise RuntimeError("CGEventCreateKeyboardEvent failed for text")
            quartz.CGEventKeyboardSetUnicodeString(event, len(chunk.encode("utf-16-le")) // 2, chunk)
            quartz.CGEventSetFlags(event, 0)
            _post(event)


def _held_button() -> Optional[str]:
    for button in BUTTONS:
        if button in _buttons_down:
            return button
    return None


def _post(event) -> None:
    quartz = _require()
    quartz.CGEventSetIntegerValueField(event, quartz.kCGEventSourceUserData, INJECTED_MARK)
    quartz.CGEventPost(quartz.kCGHIDEventTap, event)


def _mouse_event(event_name: str, x: int, y: int, button_name: Optional[str]):
    quartz = _require()
    if button_name is None:
        button = 0
    elif isinstance(button_name, int):
        button = button_name
    else:
        button = getattr(quartz, button_name)
    event = quartz.CGEventCreateMouseEvent(
        None, getattr(quartz, event_name), quartz.CGPointMake(float(x), float(y)), button
    )
    if event is None:
        raise RuntimeError(f"CGEventCreateMouseEvent failed for {event_name}")
    quartz.CGEventSetFlags(event, current_flags(_mods_down))
    return event


def inject_mouse_move(dx: int, dy: int) -> None:
    import desktop_mac

    quartz = _require()
    x, y = desktop_mac.cursor_position()
    x, y = clamp_to_displays(x + int(dx), y + int(dy), desktop_mac.monitors())
    held = _held_button()
    event = _mouse_event(MOVE_EVENTS[held], x, y, None if held is None else BUTTON_EVENTS[held][2])
    # The deltas as well as the point: anything reading raw mouse movement --
    # a game, a 3D viewport -- sees nothing without them.
    quartz.CGEventSetIntegerValueField(event, quartz.kCGMouseEventDeltaX, int(dx))
    quartz.CGEventSetIntegerValueField(event, quartz.kCGMouseEventDeltaY, int(dy))
    _post(event)


@_locked
def inject_mouse_button(button: str, down: bool) -> None:
    import desktop_mac

    quartz = _require()
    names = BUTTON_EVENTS.get(button)
    if names is None:
        LOGGER.warning("Unknown mouse button ignored: %r", button)
        return
    down_name, up_name, button_name = names
    x, y = desktop_mac.cursor_position()
    event = _mouse_event(down_name if down else up_name, x, y, button_name)
    if down:
        _buttons_down.add(button)
        state = plan_click_state(button, x, y, time.monotonic(), _last_click)
    else:
        _buttons_down.discard(button)
        previous = _last_click.get(button)
        state = previous[3] if previous is not None else 1
    quartz.CGEventSetIntegerValueField(event, quartz.kCGMouseEventClickState, state)
    _post(event)


def plan_scroll_units(dy, dx, mode: str, remainders: Dict[str, List[float]]) -> Tuple[int, int, int]:
    """Pure planning: (unit, whole vertical steps, whole horizontal steps) for one scroll, the
    fraction not yet whole kept in `remainders` for the next. The same arithmetic the PC's
    plan_scroll_units does, truncating toward zero so a remainder never reverses direction."""
    quartz = _require()
    if mode == "pixel":
        unit, key, scale = quartz.kCGScrollEventUnitPixel, "pixel", 1.0
    else:
        unit, key, scale = quartz.kCGScrollEventUnitLine, "line", LINES_PER_NOTCH
    accum = remainders[key]
    accum[0] += float(dy) * scale
    accum[1] += float(dx) * scale
    whole_y, whole_x = int(accum[0]), int(accum[1])
    accum[0] -= whole_y
    accum[1] -= whole_x
    return unit, whole_y, whole_x


@_locked
def inject_scroll(dy, dx=0.0, mode: str = "line") -> None:
    quartz = _require()
    unit, wheel_y, wheel_x = plan_scroll_units(dy, dx, mode, _scroll_remainders)
    if not wheel_y and not wheel_x:
        return
    event = quartz.CGEventCreateScrollWheelEvent(None, unit, 2, wheel_y, wheel_x)
    if event is None:
        raise RuntimeError("CGEventCreateScrollWheelEvent failed")
    quartz.CGEventSetFlags(event, current_flags(_mods_down))
    _post(event)


def reset_scroll_remainders() -> None:
    for accum in _scroll_remainders.values():
        accum[0] = accum[1] = 0.0


@_locked
def inject_gesture(name: str) -> None:
    """A system gesture the sender classified, pressed as the shortcut System Settings binds to
    it, so this Mac's own choices in Trackpad and Keyboard Shortcuts apply. A gesture with no
    shortcut is logged once and dropped."""
    chord = GESTURE_CHORDS.get(name)
    if chord is None:
        if name not in _warned_names:
            LOGGER.warning("Gesture %r has no shortcut on this Mac and is ignored", name)
            _warned_names.add(name)
        return
    # A modifier the sender is already holding is part of the chord and stays down after it.
    pressed = [key for key in chord if key not in _mods_down]
    for key in pressed:
        inject_key(key, down=True)
    for key in reversed(pressed):
        inject_key(key, down=False)


@_locked
def release_all() -> None:
    """Let go of every key and button this module is holding. Called when the
    link drops mid-chord, so a Command key held at the moment the PC went
    away does not stay down on the Mac."""
    for name in sorted(_mods_down) + sorted(_held_codes):
        try:
            inject_key(name, down=False)
        except Exception:
            LOGGER.exception("Could not release %r", name)
    for button in sorted(_buttons_down):
        try:
            inject_mouse_button(button, down=False)
        except Exception:
            LOGGER.exception("Could not release the %s mouse button", button)
    _mods_down.clear()
    _held_codes.clear()
    _buttons_down.clear()
    reset_scroll_remainders()
