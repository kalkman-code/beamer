"""Keyboard and mouse injection on X11 through XTest: the twin of input_injector.py, with the same
functions, so the receiver drives it unchanged.

Named keys are pressed by evdev code (an X keycode is the code plus 8), the same codes the X11
capture reads. A character is typed as WIRE.md section 7 says: on the key that types it in the
current layout group, read through XKB (xkb_x11), with Shift pressed or let go around it as the
layout needs; when this layout has no key for it, on its US place under a chord or for a letter of
another script, as input_injector._by_place; otherwise on a spare keycode mapped to it, which stays
mapped so a second press needs no remap (every remap makes each client reread the keymap).

Auto-repeat is the server's, as for a key held on this machine's own keyboard: the server repeats
a key XTest holds (measured on Xvfb, 01-10-2026), so the peer's repeats of a held key are not
pressed again, or every held key would repeat twice as fast. The exception is a key typed with
Shift changed around it, whose server repeats would come out under the Shift the user holds: its
server repeat is turned off while it is held and each of the peer's repeats is typed again.

Planning is pure, as plan_key_inputs is on Windows: plan_key and plan_text take the layout and
the held-key state and return steps, so the rules are tested without a server.

One connection and one lock, as input_injector has one lock: a session thread injecting while a
reconnect's release_all clears would otherwise press a key nothing then remembers to release.
python-xlib and libxkbcommon are reached only when the first event is injected, so this module
imports anywhere."""

import atexit
import functools
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from capture_x11 import KEYCODE_OFFSET, LOCK_MASK, NUM_LOCK_MASK, SHIFT_MASK
from core import keytable
from input_injector import CHORD_KEYS, MODIFIER_KEYS, SHIFT_KEYS, _by_place

LOGGER = logging.getLogger(__name__)

SHIFT_L = 42 + KEYCODE_OFFSET

# Wire names to evdev codes: EVDEV_NAMES turned round, the main Enter for "enter".
NAME_TO_CODE: Dict[str, int] = {name: code for code, name in keytable.EVDEV_NAMES.items() if code != 96}
NAME_TO_CODE.update({"return": 28, "escape": 1})
US_TO_CODE: Dict[str, int] = {char: code for code, char in keytable.EVDEV_US.items()}

BUTTONS = {"left": 1, "middle": 2, "right": 3, "back": 8, "forward": 9}
# A wheel click scrolls about three lines, which the PC reckons as 40 pixels; a pixel delta from a
# trackpad becomes clicks at that rate, its remainder carried.
PIXELS_PER_CLICK = 40.0
# Before a spare keycode is mapped to another character, the clients get this long to read the
# press it just made: each reads the keymap again only when it handles the MappingNotify.
REMAP_PAUSE_SECONDS = 0.05

PRESS, RELEASE, MAP, NO_REPEAT, REPEAT = "press", "release", "map", "no_repeat", "repeat"


def repeat_state_file() -> Path:
    """Where the keycodes whose auto-repeat this module turned off are written: it is the X
    server's state and outlives the process, so a Beamer that was killed turns them on again when
    it next starts."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "beamer" / "x11-repeat-off"


def keysym_for(character: str) -> int:
    """The keysym for a character: Latin-1 as itself, anything else as its Unicode keysym."""
    point = ord(character)
    if 0x20 <= point <= 0x7E or 0xA0 <= point <= 0xFF:
        return point
    return 0x01000000 | point


class Layout:
    """The layout as typing reads it, over an xkb_x11.Keymap: which key types a character in a
    group and whether it needs Shift with the locks (Caps Lock, Num Lock) as they are, and what the
    key at a US place types there. `mapped` holds the spare keycodes this module mapped since the
    keymap was last read."""

    def __init__(self, keymap) -> None:
        self.keymap = keymap
        self.mapped: Dict[int, int] = {}
        self._found: Dict[Tuple[int, int], Dict[str, Tuple[int, bool]]] = {}

    def find(self, character: str, group: int, locks: int) -> Optional[Tuple[int, bool]]:
        """(keycode, shift) for the key typing `character`, without Shift preferred, or None."""
        table = self._found.get((group, locks))
        if table is None:
            table = {}
            keymap = self.keymap
            for keycode in range(keymap.max_keycode, keymap.min_keycode - 1, -1):
                for shift in (True, False):
                    text = keymap.text(keymap.keysym(keycode, locks | (SHIFT_MASK if shift else 0), group))
                    if text:
                        table[text] = (keycode, shift)
            self._found[(group, locks)] = table
        found = table.get(character)
        if found is None:
            keycode = next((code for code, keysym in self.mapped.items() if keysym == keysym_for(character)), None)
            if keycode is not None:
                return keycode, False
        return found

    def place(self, us: str, group: int) -> Optional[Tuple[int, Optional[str]]]:
        """The keycode at `us`'s place and what it types there unshifted, the shape
        input_injector._by_place asks for."""
        code = US_TO_CODE.get(us)
        if code is None:
            return None
        keycode = code + KEYCODE_OFFSET
        return keycode, self.keymap.text(self.keymap.keysym(keycode, 0, group))

    def holds(self, keycode: int, keysym: int) -> bool:
        """Whether a spare keycode still types `keysym`: a new keymap (setxkbmap, a layout switch
        that loads one) wipes what was mapped."""
        if self.mapped.get(keycode) == keysym:
            return True
        return self.keymap.keysym(keycode, 0, 0) == keysym

    def spare(self) -> List[int]:
        """Keycodes with nothing on them, highest first, as xdotool takes them: the low ones are
        real keys a layout may still name, and 8 is evdev's reserved code 0."""
        keymap = self.keymap
        return [code for code in range(keymap.max_keycode, KEYCODE_OFFSET, -1) if keymap.empty(code) and code not in self.mapped]

    def map(self, keycode: int, keysym: int) -> None:
        self.mapped[keycode] = keysym
        self._found.clear()


class KeyState:
    """What this machine holds down for the peer, and the spare keycodes it has mapped."""

    def __init__(self) -> None:
        self.mods_down: Set[str] = set()
        self.named_down: Set[int] = set()
        # A held character: its keycode, and for one typed with Shift changed around it, whether
        # it needs Shift, for each repeat to be typed again (None when the server repeats it).
        self.chars_down: Dict[str, Tuple[int, Optional[bool]]] = {}
        self.spares: Dict[int, int] = {}  # keysym to keycode, least recently used first

    def held_shift(self) -> List[int]:
        return [NAME_TO_CODE[name] + KEYCODE_OFFSET for name in sorted(self.mods_down & SHIFT_KEYS)]


def _spare_steps(character: str, layout: Layout, state: KeyState) -> Optional[Tuple[int, list]]:
    """A spare keycode typing `character` on both levels, and the steps that map it, or None when
    every spare is held."""
    keysym = keysym_for(character)
    keycode = state.spares.pop(keysym, None)
    if keycode is not None and layout.holds(keycode, keysym):
        state.spares[keysym] = keycode  # most recent last
        return keycode, []
    evicting = False
    if keycode is None:
        free = [code for code in layout.spare() if code not in state.spares.values()]
        if free:
            keycode = free[0]
        else:
            held = {code for code, _again in state.chars_down.values()}
            reusable = [(sym, code) for sym, code in state.spares.items() if code not in held]
            if not reusable:
                return None
            old_sym, keycode = reusable[0]
            del state.spares[old_sym]
            evicting = True
    # else a new keymap wiped it: it is mapped again where it was.
    state.spares[keysym] = keycode
    layout.map(keycode, keysym)
    return keycode, [(MAP, keycode, keysym, evicting)]


def _shift_around(keycode: int, shift: bool, state: KeyState) -> list:
    """Press `keycode` with Shift as `shift` says, pressing or letting go of it around the key."""
    held = state.held_shift()
    if shift and not held:
        return [(PRESS, SHIFT_L), (PRESS, keycode), (RELEASE, SHIFT_L)]
    if not shift and held:
        return [(RELEASE, code) for code in held] + [(PRESS, keycode)] + [(PRESS, code) for code in held]
    return [(PRESS, keycode)]


_warned: Set[str] = set()


def _warn_once(message: str, item: str) -> None:
    if item not in _warned:
        _warned.add(item)
        LOGGER.warning(message, item)


def plan_key(name: str, down: bool, us: Optional[str], state: KeyState, layout: Layout, group: int, locks: int) -> list:
    """Steps for one key message: (PRESS, keycode), (RELEASE, keycode), (MAP, keycode, keysym,
    evicting), (NO_REPEAT, keycode) or (REPEAT, keycode). `locks` is the Caps Lock and Num Lock
    bits as they are. Updates `state`. Empty for a name this machine cannot press, and for the
    peer's repeat of a key the server is already repeating."""
    lowered = name.lower() if len(name) > 1 else name
    if lowered in MODIFIER_KEYS:
        if down:
            state.mods_down.add(lowered)
        else:
            state.mods_down.discard(lowered)
    code = NAME_TO_CODE.get(lowered) if len(name) > 1 else None
    if code is not None:
        keycode = code + KEYCODE_OFFSET
        if not down:
            state.named_down.discard(keycode)
            return [(RELEASE, keycode)]
        if keycode in state.named_down:
            return []
        state.named_down.add(keycode)
        return [(PRESS, keycode)]
    if len(name) != 1:
        _warn_once("Unknown key name ignored: %r", name)
        return []
    character = name
    held = state.chars_down.get(character)
    if not down:
        if held is None:
            found = layout.find(character, group, locks)
            return [] if found is None else [(RELEASE, found[0])]
        del state.chars_down[character]
        keycode, again = held
        return [(RELEASE, keycode)] + ([(REPEAT, keycode)] if again is not None else [])
    if held is not None:
        keycode, shift = held
        # Shift is worked out again: the peer may have pressed or let go of its own since.
        return [] if shift is None else [(RELEASE, keycode)] + _shift_around(keycode, shift, state)
    chord = bool(state.mods_down & CHORD_KEYS)
    found = layout.find(character, group, locks)
    if found is not None:
        keycode, shift = found
        # Under a chord the peer's own Shift is part of the shortcut; it is never touched.
        steps = [(PRESS, keycode)] if chord else _shift_around(keycode, shift, state)
        if len(steps) == 1:
            state.chars_down[character] = (keycode, None)
            return steps
        state.chars_down[character] = (keycode, shift)
        return [(NO_REPEAT, keycode)] + steps
    place = _by_place(character, us, state.mods_down, lambda key: layout.place(key, group))
    if place is not None:
        state.chars_down[character] = (place[0], None)
        return [(PRESS, place[0])]
    spare = _spare_steps(character, layout, state)
    if spare is None:
        _warn_once("No spare key left to type %r", character)
        return []
    keycode, steps = spare
    state.chars_down[character] = (keycode, None)
    return steps + [(PRESS, keycode)]


def plan_text(text: str, state: KeyState, layout: Layout, group: int, locks: int) -> list:
    """Steps typing `text` as it stands (WIRE.md section 10): every modifier the peer holds is let
    go first and pressed again after, so neither they nor the layout play a part."""
    held = [NAME_TO_CODE[name] + KEYCODE_OFFSET for name in sorted(state.mods_down) if name in NAME_TO_CODE]
    steps = [(RELEASE, code) for code in held]
    bare = KeyState()
    bare.spares, bare.chars_down = state.spares, state.chars_down
    for character in text:
        found = layout.find(character, group, locks)
        if found is not None:
            keycode, shift = found
            steps += _shift_around(keycode, shift, bare)
        else:
            spare = _spare_steps(character, layout, bare)
            if spare is None:
                _warn_once("No spare key left to type %r", character)
                continue
            keycode, mapped = spare
            steps += mapped + [(PRESS, keycode)]
        steps.append((RELEASE, keycode))
    return steps + [(PRESS, code) for code in held]


def plan_scroll(dy, dx, mode: str, carry: List[float]) -> Tuple[int, int]:
    """Whole wheel clicks (vertical, horizontal) for one scroll message, the remainder carried in
    `carry` ([y, x]) as plan_scroll_units carries Windows' wheel units. Positive y is away from
    the hand, positive x is right, as on Windows."""
    scale = 1.0 / PIXELS_PER_CLICK if mode == "pixel" else 1.0
    carry[0] += float(dy) * scale
    carry[1] += float(dx) * scale
    whole_y, whole_x = int(carry[0]), int(carry[1])
    carry[0] -= whole_y
    carry[1] -= whole_x
    return whole_y, whole_x


def scroll_buttons(whole_y: int, whole_x: int) -> List[int]:
    buttons = [4 if whole_y > 0 else 5] * abs(whole_y)
    return buttons + [7 if whole_x > 0 else 6] * abs(whole_x)


class _Connection:
    """The injector's X connection: XTest, the keymap and per-key auto-repeat."""

    def __init__(self) -> None:
        from Xlib import X, display
        from Xlib.ext import xtest

        import xkb_x11

        self._X, self._xtest = X, xtest
        self.display = display.Display()
        try:
            if not self.display.has_extension("XTEST"):
                raise RuntimeError("This X server has no XTEST extension")
            self.keymap = xkb_x11.Keymap(self.display.get_display_name())
        except Exception:
            self.display.close()
            raise
        self.root = self.display.screen().root
        self._repeat_off: Set[int] = set()
        self._restore_left_over()
        atexit.register(self._restore_at_exit)

    def _restore_left_over(self) -> None:
        try:
            left = [int(word) for word in repeat_state_file().read_text().split()]
        except (OSError, ValueError):
            return
        for keycode in left:
            self.display.change_keyboard_control(key=keycode, auto_repeat_mode=self._X.AutoRepeatModeOn)
        self.display.sync()
        self._write_repeat_off()

    def _write_repeat_off(self) -> None:
        path = repeat_state_file()
        try:
            if self._repeat_off:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(" ".join(str(keycode) for keycode in sorted(self._repeat_off)))
            else:
                path.unlink(missing_ok=True)
        except OSError:
            LOGGER.exception("Could not record which keys have auto-repeat off")

    def _restore_at_exit(self) -> None:
        try:
            self.restore_repeat()
        except Exception:
            pass

    def layout(self) -> Layout:
        return Layout(self.keymap)

    def mapping_changed(self) -> bool:
        """Whether a MappingNotify has arrived since the last look; the keymap is read again."""
        changed = False
        while self.display.pending_events():
            event = self.display.next_event()
            if event.type == self._X.MappingNotify and event.request in (self._X.MappingKeyboard, self._X.MappingModifier):
                changed = True
        if changed:
            self.keymap.reload()
        return changed

    def state(self) -> Tuple[int, int]:
        """(layout group, the Caps Lock and Num Lock bits) now."""
        mask = self.root.query_pointer().mask
        return (mask >> 13) & 3, mask & (LOCK_MASK | NUM_LOCK_MASK)

    def run(self, steps) -> None:
        X, fake = self._X, self._xtest.fake_input
        for step in steps:
            kind = step[0]
            if kind == MAP:
                if step[3]:
                    self.display.sync()
                    time.sleep(REMAP_PAUSE_SECONDS)
                self.display.change_keyboard_mapping(step[1], [(step[2], step[2])])
                self.display.sync()
            elif kind == NO_REPEAT:
                repeats = self.display.get_keyboard_control().auto_repeats
                if repeats[step[1] // 8] >> (step[1] % 8) & 1:
                    self._repeat_off.add(step[1])
                    self._write_repeat_off()  # before the change, so a crash after it is undone next start
                    self.display.change_keyboard_control(key=step[1], auto_repeat_mode=X.AutoRepeatModeOff)
            elif kind == REPEAT:
                if step[1] in self._repeat_off:
                    self.display.change_keyboard_control(key=step[1], auto_repeat_mode=X.AutoRepeatModeOn)
                    self._repeat_off.discard(step[1])
                    self._write_repeat_off()
            else:
                fake(self.display, X.KeyPress if kind == PRESS else X.KeyRelease, step[1])
        self.display.flush()

    def restore_repeat(self) -> None:
        if not self._repeat_off:
            return
        for keycode in sorted(self._repeat_off):
            self.display.change_keyboard_control(key=keycode, auto_repeat_mode=self._X.AutoRepeatModeOn)
        self.display.sync()
        self._repeat_off.clear()
        self._write_repeat_off()

    def button(self, button: int, down: bool) -> None:
        self._xtest.fake_input(self.display, self._X.ButtonPress if down else self._X.ButtonRelease, button)
        self.display.flush()

    def move(self, dx: int, dy: int) -> None:
        self._xtest.fake_input(self.display, self._X.MotionNotify, detail=1, x=int(dx), y=int(dy))
        self.display.flush()

    def move_to(self, x: int, y: int) -> None:
        self._xtest.fake_input(self.display, self._X.MotionNotify, detail=0, root=self.root.id, x=int(x), y=int(y))
        self.display.flush()

    def close(self) -> None:
        atexit.unregister(self._restore_at_exit)
        try:
            self.restore_repeat()
            self.keymap.close()
        finally:
            self.display.close()


_connect = _Connection
_connection = None
_layout: Optional[Layout] = None
_state = KeyState()
_buttons_down: Set[str] = set()
_scroll_carry = [0.0, 0.0]
_lock = threading.RLock()


def _locked(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with _lock:
            return function(*args, **kwargs)

    return wrapper


def _open():
    global _connection, _layout
    if _connection is None:
        _connection = _connect()
        _layout = None
    return _connection


def _drop() -> None:
    """After a failed request the connection is closed and opened again on the next one: a dead X
    socket never recovers."""
    global _connection, _layout
    dead, _connection, _layout = _connection, None, None
    if dead is not None:
        try:
            dead.close()
        except Exception:
            pass


def _talk(function):
    """Runs `function(connection)` under the lock, dropping the connection if it fails."""
    with _lock:
        connection = _open()
        try:
            return function(connection)
        except Exception:
            _drop()
            raise


def _current_layout(connection) -> Layout:
    global _layout
    if connection.mapping_changed() or _layout is None:
        _layout = connection.layout()
    return _layout


@_locked
def inject_key(name: str, down: bool, us: Optional[str] = None) -> None:
    def go(connection):
        layout = _current_layout(connection)
        group, locks = connection.state() if len(name) == 1 else (0, 0)
        steps = plan_key(name, down, us, _state, layout, group, locks)
        if steps:
            connection.run(steps)

    _talk(go)


@_locked
def inject_text(text: str) -> None:
    def go(connection):
        layout = _current_layout(connection)
        group, locks = connection.state()
        steps = plan_text(text, _state, layout, group, locks)
        if steps:
            connection.run(steps)

    _talk(go)


def inject_mouse_move(dx: int, dy: int) -> None:
    _talk(lambda connection: connection.move(dx, dy))


def move_to(x: int, y: int) -> None:
    """An absolute XTest move, for the proof harness."""
    _talk(lambda connection: connection.move_to(x, y))


@_locked
def inject_mouse_button(button: str, down: bool) -> None:
    number = BUTTONS.get(button)
    if number is None:
        _warn_once("Unknown mouse button ignored: %r", button)
        return
    if down:
        _buttons_down.add(button)
    else:
        _buttons_down.discard(button)
    _talk(lambda connection: connection.button(number, down))


@_locked
def inject_scroll(dy, dx=0.0, mode: str = "line") -> None:
    buttons = scroll_buttons(*plan_scroll(dy, dx, mode, _scroll_carry))
    if buttons:
        def go(connection):
            for button in buttons:
                connection.button(button, True)
                connection.button(button, False)

        _talk(go)


def inject_gesture(name: str, swipe=None) -> None:
    """Linux desktops share no shortcut for a swipe, so none is pressed; a Linux machine does not
    offer the `gestures` capability, and a peer that sends one anyway gets this."""
    _warn_once("Gestures are not yet on Linux; %r dropped", name)


@_locked
def release_all() -> None:
    """Let go of every key and button held for the peer, the twin of input_injector.release_all:
    the receiver calls it when the peer's input goes home or its link dies."""
    for name in sorted(_state.mods_down):
        try:
            inject_key(name, down=False)
        except Exception:
            LOGGER.exception("Could not release %r", name)
    for keycode in sorted(_state.named_down):
        try:
            _talk(lambda connection, keycode=keycode: connection.run([(RELEASE, keycode)]))
        except Exception:
            LOGGER.exception("Could not release keycode %s", keycode)
    for character in sorted(_state.chars_down):
        try:
            inject_key(character, down=False)
        except Exception:
            LOGGER.exception("Could not release %r", character)
    for button in sorted(_buttons_down):
        try:
            inject_mouse_button(button, down=False)
        except Exception:
            LOGGER.exception("Could not release the %s mouse button", button)
    if _connection is not None:
        try:
            _connection.restore_repeat()
        except Exception:
            LOGGER.exception("Could not turn auto-repeat back on")
    _state.mods_down.clear()
    _state.named_down.clear()
    _state.chars_down.clear()
    _buttons_down.clear()
    _scroll_carry[0] = _scroll_carry[1] = 0.0
