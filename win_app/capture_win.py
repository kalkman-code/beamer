"""Low-level keyboard and mouse capture on Windows: the mirror of the Mac's
Quartz event tap.

Two WH_*_LL hooks on a thread of their own, which is the only place a
low-level hook can live -- it needs a message loop, and the Qt main thread's
belongs to the UI. While input is redirected to the Mac the hook callbacks
swallow every event (return 1) and hand it to the sender instead; while it is
not, they pass everything through untouched except a pointer being pushed
against the edge that leads to the Mac, which the same return-edge model the
Mac's own crossing uses holds and finally lets through.

Three things here are safety, not features, and none of them may move off the
callback thread:

- A hook that returns 1 while whatever consumes its events is wedged takes
  the user's keyboard and mouse away with no way back. So the callback itself
  checks whether redirecting is still live, and the moment the sender says it
  is not -- a dropped link, a missed acknowledgement -- it passes the event
  through. Nothing it does can block.
- Injected events are ignored (LLKHF_INJECTED / LLMHF_INJECTED). Without that,
  every SendInput this PC makes for the Mac's own input would come straight
  back round as a captured event. Raw Input has no such flag, so the injector
  stamps INJECTED_MARK into every SendInput's dwExtraInfo and the WM_INPUT
  reader drops a move carrying it. Without that, the Mac's last deltas after
  a return crossing -- injected here, then reported by Raw Input as the mouse
  moving -- read as this PC's own push against the same edge and sent this
  PC's input straight out to the Mac, which left the Mac's own notch and
  shortcut dead until someone pressed Send input to Windows.
- The character a key produces is read with only shift and caps lock applied,
  never Ctrl/Alt/Win, exactly as the Mac's translator does it, so a chord
  arrives as a modifier plus the plain key rather than as a control character.

Pointer movement comes from Raw Input, not from the mouse hook, and that is
not a preference. A low-level hook reports the cursor's position, which
Windows has already clamped to the desktop, so at the very edge -- the one
place a crossing has to measure a push -- every further move reports the same
point and the pressure never builds. WM_INPUT carries the mouse's own
relative counts, which the edge does not touch. The hook still owns buttons,
the wheel and swallowing; Raw Input owns how far the hand moved.
"""

import ctypes
import logging
import sys
import threading
import time
import unicodedata
from typing import Callable, Dict, List, Optional, Set, Tuple, Union

from input_injector import INJECTED_MARK, SCAN_TO_US

LOGGER = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"
user32 = ctypes.WinDLL("user32", use_last_error=True) if _IS_WINDOWS else None
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if _IS_WINDOWS else None

WM_INPUT = 0x00FF
RID_INPUT = 0x10000003
RIDEV_INPUTSINK = 0x00000100
RIM_TYPEMOUSE = 0
MOUSE_MOVE_ABSOLUTE = 0x01
HWND_MESSAGE = -3

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0
WM_QUIT = 0x0012
THREAD_PRIORITY_HIGHEST = 2

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
WM_MOUSEHWHEEL = 0x020E

LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01

WHEEL_DELTA = 120

KEY_DOWN_MESSAGES = {WM_KEYDOWN, WM_SYSKEYDOWN}
KEY_UP_MESSAGES = {WM_KEYUP, WM_SYSKEYUP}

BUTTON_MESSAGES = {
    WM_LBUTTONDOWN: ("left", True),
    WM_LBUTTONUP: ("left", False),
    WM_RBUTTONDOWN: ("right", True),
    WM_RBUTTONUP: ("right", False),
    WM_MBUTTONDOWN: ("middle", True),
    WM_MBUTTONUP: ("middle", False),
}

# The side buttons share one pair of messages; the high word of mouseData says which.
X_BUTTONS = {1: "back", 2: "forward"}


def button_of(message: int, mouse_data: int) -> Optional[Tuple[str, bool]]:
    """(name, down) for a button message, or None for a move or the wheel."""
    fixed = BUTTON_MESSAGES.get(message)
    if fixed is not None:
        return fixed
    if message in (WM_XBUTTONDOWN, WM_XBUTTONUP):
        name = X_BUTTONS.get((mouse_data >> 16) & 0xFFFF)
        if name is not None:
            return name, message == WM_XBUTTONDOWN
    return None


def mouse_event(message: int, mouse_data: int):
    """What a low-level mouse message says, in the sender's neutral terms: ("button", name, down),
    ("wheel", dy, dx) in notches, ("move",), or None for a message the Mac has no name for."""
    button = button_of(message, mouse_data)
    if button is not None:
        return ("button",) + button
    if message == WM_MOUSEWHEEL:
        return ("wheel", wheel_notches(mouse_data), 0.0)
    if message == WM_MOUSEHWHEEL:
        return ("wheel", 0.0, wheel_notches(mouse_data))
    if message == WM_MOUSEMOVE:
        return ("move",)
    return None


# What the Keyboard page calls a recorded key, in Windows' own words.
VK_TITLES: Dict[int, str] = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x13: "Pause", 0x14: "Caps Lock", 0x1B: "Esc",
    0x20: "Space", 0x21: "Page Up", 0x22: "Page Down", 0x23: "End", 0x24: "Home", 0x25: "Left",
    0x26: "Up", 0x27: "Right", 0x28: "Down", 0x2C: "Print Screen", 0x2D: "Insert", 0x2E: "Delete",
    0x5B: "Left Windows", 0x5C: "Right Windows", 0x5D: "Menu", 0x6A: "Num *", 0x6B: "Num +",
    0x6D: "Num -", 0x6E: "Num .", 0x6F: "Num /", 0x90: "Num Lock", 0x91: "Scroll Lock",
    0xA0: "Left Shift", 0xA1: "Right Shift", 0xA2: "Left Ctrl", 0xA3: "Right Ctrl",
    0xA4: "Left Alt", 0xA5: "Right Alt", 0xA6: "Browser Back", 0xA7: "Browser Forward",
    0xAD: "Mute", 0xAE: "Volume Down", 0xAF: "Volume Up", 0xB0: "Next Track",
    0xB1: "Previous Track", 0xB2: "Stop", 0xB3: "Play/Pause", 0xBA: ";", 0xBB: "=", 0xBC: ",",
    0xBD: "-", 0xBE: ".", 0xBF: "/", 0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
}
for _index in range(24):
    VK_TITLES[0x70 + _index] = f"F{_index + 1}"
for _index in range(10):
    VK_TITLES[0x60 + _index] = f"Num {_index}"
BUTTON_TITLES = {"right": "Right button", "middle": "Middle button", "back": "Back button", "forward": "Forward button"}


def input_title(entry: str) -> str:
    """The name an ignored-inputs entry is shown under."""
    kind, _, value = entry.partition(":")
    if kind == "button":
        return BUTTON_TITLES.get(value, f"Button {value}")
    if kind == "key" and value.isdigit():
        vk = int(value)
        if vk in VK_TITLES:
            return VK_TITLES[vk]
        if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
            return chr(vk)
        return f"Key {vk}"
    return entry


VK_SHIFT_LEFT, VK_SHIFT_RIGHT = 0xA0, 0xA1
VK_CONTROL_LEFT, VK_CONTROL_RIGHT = 0xA2, 0xA3
VK_MENU_LEFT, VK_MENU_RIGHT = 0xA4, 0xA5
_RIGHT_SHIFT_SCAN = 0x36
_EXTENDED_SCAN = 0x100
_EXTENDED_PREFIX = 0xE000


def hook_vk(vk: int, scan: int) -> int:
    """The virtual key the low-level hook reports for a key a window saw as `vk` and `scan`. A
    window is told only Shift, Ctrl or Alt; the hook always knows which side. An extended key is
    what marks the right Ctrl and the right Alt: Qt 6 reports one with an 0xE0 prefix (0xE01D is
    the right Ctrl), and the extended bit at 0x100 is read too."""
    extended = bool(scan & _EXTENDED_SCAN) or scan & 0xFF00 == _EXTENDED_PREFIX
    if vk == 0x10:
        return VK_SHIFT_RIGHT if scan & 0xFF == _RIGHT_SHIFT_SCAN else VK_SHIFT_LEFT
    if vk == 0x11:
        return VK_CONTROL_RIGHT if extended else VK_CONTROL_LEFT
    if vk == 0x12:
        return VK_MENU_RIGHT if extended else VK_MENU_LEFT
    return vk

# Windows virtual keys to the wire's key names, which are Mac-shaped: the
# semantic mapping happens here, once, so the Mac injects what it is given
# and nothing translates on arrival. Ctrl becomes Command and the Windows key
# becomes Control, the exact inverse of the Mac's own key_map, so familiar
# shortcuts keep working in both directions -- Ctrl+C on this keyboard is
# Cmd+C on the Mac.
VK_TO_NAME: Dict[int, str] = {
    0x08: "backspace",
    0x09: "tab",
    0x0D: "enter",
    0x13: "pause",
    0x14: "caps_lock",
    0x1B: "esc",
    0x20: "space",
    0x21: "page_up",
    0x22: "page_down",
    0x23: "end",
    0x24: "home",
    0x25: "left",
    0x26: "up",
    0x27: "right",
    0x28: "down",
    0x2C: "print_screen",
    0x2D: "insert",
    0x2E: "delete",
    0x5B: "ctrl",
    0x5C: "ctrl_r",
    0x5D: "menu",
    0x90: "num_lock",
    0x91: "scroll_lock",
    0xA0: "shift",
    0xA1: "shift_r",
    0xA2: "cmd",
    0xA3: "cmd_r",
    0xA4: "alt",
    0xA5: "alt_r",
    0xA6: "browser_back",
    0xA7: "browser_forward",
    0xAD: "volume_mute",
    0xAE: "volume_down",
    0xAF: "volume_up",
    0xB0: "media_next",
    0xB1: "media_prev",
    0xB2: "media_stop",
    0xB3: "media_play_pause",
    # The undifferentiated modifiers, which a low-level hook does not normally
    # report but a synthetic event can.
    0x10: "shift",
    0x11: "cmd",
    0x12: "alt",
}

for _index in range(1, 13):
    VK_TO_NAME[0x70 + (_index - 1)] = f"f{_index}"
for _index in range(13, 21):
    VK_TO_NAME[0x7C + (_index - 13)] = f"f{_index}"

# Media and browser keys are never the trigger: they are what the stays-on-this-PC list is for.
# The undifferentiated 0x10-0x12 never reach the hook.
NOT_TRIGGER_VKS = {0x10, 0x11, 0x12} | set(range(0xA6, 0xB4))
# Loaded if a config names them, never offered by the recorder, as on the Mac: Backspace, Tab,
# Enter, Esc and Space are typing keys a double-tap or a hold would take from every app, and
# Windows gives a window no key-down for Print Screen, so it cannot be recorded at all.
UNRECORDABLE_TRIGGER_VKS = {0x08, 0x09, 0x0D, 0x1B, 0x20, 0x2C}

MODIFIER_NAMES = {
    "shift",
    "shift_r",
    "cmd",
    "cmd_r",
    "alt",
    "alt_r",
    "ctrl",
    "ctrl_r",
    "caps_lock",
}

VK_SHIFT = 0x10
VK_CAPITAL = 0x14


class KeyboardState:
    """The keyboard bytes ToUnicodeEx is asked to translate against: shift and
    caps lock as they really are, every other modifier cleared. Held as a
    plain dict of virtual key to byte so the translation is testable without
    Windows. Dead accents stay here in software, never in Windows' keyboard state."""

    def __init__(self) -> None:
        self.shift_down = False
        self.caps_lock = False
        self.pending_dead_key: Optional[str] = None

    def bytes_for(self) -> Dict[int, int]:
        state = {VK_SHIFT: 0x80 if self.shift_down else 0x00, VK_CAPITAL: 0x01 if self.caps_lock else 0x00}
        return state


class DeadKey:
    def __init__(self, character: str) -> None:
        self.character = character


class TextInput:
    def __init__(self, text: str) -> None:
        self.text = text


def _compose_dead_key(dead_key: str, character: str) -> Union[str, TextInput]:
    name = unicodedata.name(dead_key, "")
    if name.startswith("MODIFIER LETTER "):
        name = name[len("MODIFIER LETTER "):]
    try:
        combining = unicodedata.lookup("COMBINING " + name)
    except KeyError:
        return TextInput(dead_key + character)
    composed = unicodedata.normalize("NFC", character + combining)
    return composed if len(composed) == 1 else TextInput(dead_key + character)


def key_name(
    vk: int,
    scan: int,
    state: KeyboardState,
    translate: Callable[[int, int, Dict[int, int]], Optional[Union[str, DeadKey]]],
    down: bool = True,
) -> Optional[Union[str, DeadKey, TextInput]]:
    """The wire name for one key press: a named key where there is one, else
    the character it types with only shift and caps lock applied. A dead key
    waits for the next printable character; a key with no usable character is
    ignored."""
    named = VK_TO_NAME.get(vk)
    if named is not None:
        if state.pending_dead_key and named == "space":
            character, state.pending_dead_key = state.pending_dead_key, None
            return character
        if state.pending_dead_key and named not in MODIFIER_NAMES:
            state.pending_dead_key = None
        return named
    character = translate(vk, scan, state.bytes_for())
    if isinstance(character, DeadKey):
        if down:
            state.pending_dead_key = character.character
        return DeadKey(character.character)
    if not character or not character.isprintable():
        state.pending_dead_key = None
        return None
    if down and state.pending_dead_key:
        character = _compose_dead_key(state.pending_dead_key, character)
        state.pending_dead_key = None
    return character


def wheel_notches(mouse_data: int) -> float:
    """The signed wheel delta a low-level mouse hook packs into the high word
    of mouseData, in notches. Positive is away from the hand on both
    platforms, so the sign travels unchanged."""
    delta = (mouse_data >> 16) & 0xFFFF
    if delta >= 0x8000:
        delta -= 0x10000
    return delta / WHEEL_DELTA


TOGGLE = "toggle"
REDIRECT = "redirect"
RETURN = "return"


class Trigger:
    """The shortcut that moves input by hand rather than by pushing the
    pointer, in either of the two styles the Mac offers. `feed` answers with
    TOGGLE, REDIRECT, RETURN or None; the caller decides what to do, and
    swallows the key whenever the answer is not None.

    Double-tap: two presses of `key` inside `window_ms` flip which machine has
    input. Hold: input is on the other machine for exactly as long as the key
    is held, which is the style for reaching across for one keystroke.

    The key is a wire name, so "cmd_r" is the right Ctrl key on this keyboard
    -- the same name the Mac would inject it under. That is deliberate: the
    name the capture produces and the name the setting holds are one thing,
    and cannot drift apart."""

    def __init__(self, key: str = "cmd_r", style: str = "double_tap", window_ms: int = 300) -> None:
        self.key = key
        self.style = style
        self.window_ms = window_ms
        self._last_at: Optional[float] = None
        self._held = False
        self._release_owed = False

    def configure(self, key: str, style: str, window_ms: int) -> None:
        self.key, self.style, self.window_ms = key, style, int(window_ms)
        self._last_at = None
        self._held = False
        self._release_owed = False

    def claims(self, name: str, down: bool, redirecting: bool) -> bool:
        """Whether an event of `name` that `feed` did not act on is still the
        trigger's to swallow. Under hold, every event of the key is: its
        autorepeats and a stray release would otherwise fall through to the
        sender as ordinary keystrokes and reach the Mac as a held modifier
        with no release. Under double-tap the key is the trigger's while
        input is away, the same rule the Mac applies, and for the release of
        the tap that just moved input, whichever way it went; at home a lone
        tap is an ordinary key and passes."""
        if name != self.key:
            return False
        if self.style == "hold":
            # Except a release at home that the trigger never pressed: the key went down before
            # it became the trigger -- recorded in Hold, it is still held as it is recorded -- so
            # Windows has its key-down and must see it let go.
            return down or self._held or redirecting
        if not down and self._release_owed:
            self._release_owed = False
            return True
        return redirecting

    def feed(self, name: str, down: bool, now: float) -> Optional[str]:
        if name != self.key:
            return None
        if self.style == "hold":
            if down == self._held:
                # Key repeat while held, or a stray release: nothing changes,
                # but the key is still the trigger's and never reaches an app.
                return None
            self._held = down
            return REDIRECT if down else RETURN
        if not down:
            return None
        last = self._last_at
        self._last_at = now
        if last is not None and (now - last) * 1000.0 <= self.window_ms:
            self._last_at = None
            # The second tap's release is the trigger's too, whichever way
            # input just went: at home it would reach Windows as a key-up
            # with no key-down.
            self._release_owed = True
            return TOGGLE
        return None


class Hooks:
    """The two low-level hooks, the raw-input sink, and the thread that owns
    all three.

    `on_key(name, down, vk, us)` and `on_mouse(message, x, y, mouse_data)` are called
    on the hook thread and return True when the event has been consumed and
    must not reach the rest of Windows. `on_motion(dx, dy)` is called with
    the mouse's own relative counts and consumes nothing -- raw input is a
    listener, never a filter, which is why the hook is still what swallows a
    move. None of the three may block.
    """

    def __init__(self, on_key, on_mouse, on_motion) -> None:
        self._on_key = on_key
        self._on_mouse = on_mouse
        self._on_motion = on_motion
        self._window = None
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._ready = threading.Event()
        self._state = KeyboardState()
        self._dead_keys_swallowed: Set[int] = set()
        self._text_keyups_swallowed: Set[int] = set()
        self._failure: Optional[BaseException] = None
        self._handles: List[int] = []
        # Kept alive for as long as the hooks are installed: a callback that
        # is garbage collected while Windows still holds its address crashes
        # the process the next time a key is pressed.
        self._callbacks: List = []

    def start(self) -> None:
        if user32 is None:
            raise RuntimeError("Low-level hooks are only available on Windows")
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._failure = None
        self._thread = threading.Thread(target=self._run, name="Beamer-hooks", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("The input hooks did not start within five seconds")
        # The hook thread signals ready whether it succeeded or failed, so
        # that an installation that cannot work does not hang the caller for
        # five seconds. It must still be the caller that hears about it:
        # silently returning here is a Beamer that says it is connected, takes
        # the shortcut, and does nothing at all.
        if self._failure is not None:
            raise self._failure

    def stop(self) -> None:
        thread_id = self._thread_id
        if thread_id is not None:
            user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            if thread.is_alive():
                LOGGER.error("The hook thread did not stop within two seconds")
            else:
                self._text_keyups_swallowed.clear()
        else:
            self._text_keyups_swallowed.clear()
        self._thread = None
        self._thread_id = None

    def _run(self) -> None:
        keyboard = _LL_HOOK_PROC(self._keyboard_proc)
        mouse = _LL_HOOK_PROC(self._mouse_proc)
        window_proc = _WNDPROC(self._window_proc)
        self._callbacks = [keyboard, mouse, window_proc]
        module = kernel32.GetModuleHandleW(None)
        self._thread_id = kernel32.GetCurrentThreadId()
        # Highest within Beamer's above-normal class: this thread answers the
        # hooks, and a callback that misses LowLevelHooksTimeout is removed by
        # Windows without a word, taking the shortcut and the swallow with it.
        if not kernel32.SetThreadPriority(kernel32.GetCurrentThread(), THREAD_PRIORITY_HIGHEST):
            LOGGER.warning("Could not raise the hook thread's priority: %s", ctypes.WinError(ctypes.get_last_error()))
        try:
            self._window = _create_message_window(window_proc, module)
            _register_raw_mouse(self._window)
            for hook_id, proc in ((WH_KEYBOARD_LL, keyboard), (WH_MOUSE_LL, mouse)):
                # NULL, not this process's module handle: a low-level hook
                # whose procedure lives in the calling process takes no
                # module, and naming one that does not contain the procedure
                # -- which a ctypes callback never does -- fails with
                # "The specified module could not be found".
                handle = user32.SetWindowsHookExW(hook_id, proc, None, 0)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                self._handles.append(handle)
            self._state.caps_lock = read_caps_lock()
            self._ready.set()
            message = MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            LOGGER.exception("The input hooks stopped")
            self._failure = exc
            self._ready.set()
        finally:
            for handle in self._handles:
                user32.UnhookWindowsHookEx(handle)
            self._handles = []
            if self._window:
                user32.DestroyWindow(self._window)
                self._window = None
            # The class goes with it. A window class holds the address of the
            # procedure it was registered with, and these callbacks are
            # released on the next line: a second start would skip
            # registration, build a window on the old class, and crash inside
            # a freed callback on its first WM_INPUT.
            _unregister_window_class(module)
            self._callbacks = []
            self._text_keyups_swallowed.clear()

    def _window_proc(self, hwnd, message, wparam, lparam):
        if message == WM_INPUT:
            try:
                motion = _raw_mouse_motion(lparam)
                if motion is not None:
                    self._on_motion(*motion)
            except Exception:
                LOGGER.exception("Raw mouse input failed")
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _keyboard_proc(self, code, wparam, lparam):
        if code != HC_ACTION:
            return user32.CallNextHookEx(None, code, wparam, lparam)
        try:
            data = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if data.flags & LLKHF_INJECTED:
                return user32.CallNextHookEx(None, code, wparam, lparam)
            down = wparam in KEY_DOWN_MESSAGES
            if not down and wparam not in KEY_UP_MESSAGES:
                return user32.CallNextHookEx(None, code, wparam, lparam)
            if data.vkCode in (0xA0, 0xA1, 0x10):  # noqa: shift, either side
                self._state.shift_down = down
            elif data.vkCode == VK_CAPITAL and down:
                self._state.caps_lock = not self._state.caps_lock
            if data.vkCode in self._text_keyups_swallowed:
                if not down:
                    self._text_keyups_swallowed.discard(data.vkCode)
                else:
                    repeated = key_name(data.vkCode, data.scanCode, self._state, _to_unicode, True)
                    if isinstance(repeated, str) and len(repeated) == 1:
                        self._on_key(TextInput(repeated), True, data.vkCode, None)
                return 1
            name = key_name(data.vkCode, data.scanCode, self._state, _to_unicode, down)
            if isinstance(name, TextInput):
                if down and self._on_key(name, down, data.vkCode, None):
                    self._text_keyups_swallowed.add(data.vkCode)
                    return 1
                return user32.CallNextHookEx(None, code, wparam, lparam)
            if isinstance(name, DeadKey):
                if not down and data.vkCode in self._dead_keys_swallowed:
                    self._dead_keys_swallowed.discard(data.vkCode)
                    return 1
                if down and self._on_key(name, down, data.vkCode, None):
                    self._dead_keys_swallowed.add(data.vkCode)
                    return 1
                if down:
                    self._state.pending_dead_key = None
                return user32.CallNextHookEx(None, code, wparam, lparam)
            if name is None:
                return user32.CallNextHookEx(None, code, wparam, lparam)
            us = None if data.flags & LLKHF_EXTENDED else SCAN_TO_US.get(data.scanCode)
            if self._on_key(name, down, data.vkCode, us):
                return 1
        except Exception:
            # Never let an exception here swallow a key: fail open, log once
            # per event, and let Windows have it.
            LOGGER.exception("Keyboard hook failed; passing the key through")
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _mouse_proc(self, code, wparam, lparam):
        if code != HC_ACTION:
            return user32.CallNextHookEx(None, code, wparam, lparam)
        try:
            data = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            if data.flags & LLMHF_INJECTED:
                return user32.CallNextHookEx(None, code, wparam, lparam)
            if self._on_mouse(wparam, data.pt.x, data.pt.y, data.mouseData):
                return 1
        except Exception:
            LOGGER.exception("Mouse hook failed; passing the event through")
        return user32.CallNextHookEx(None, code, wparam, lparam)


if _IS_WINDOWS:
    ULONG_PTR = ctypes.c_size_t

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", ctypes.c_ulong),
            ("scanCode", ctypes.c_ulong),
            ("flags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", POINT),
            ("mouseData", ctypes.c_ulong),
            ("flags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class RAWINPUTHEADER(ctypes.Structure):
        _fields_ = [
            ("dwType", ctypes.c_ulong),
            ("dwSize", ctypes.c_ulong),
            ("hDevice", ctypes.c_void_p),
            ("wParam", ctypes.c_size_t),
        ]

    class RAWMOUSE(ctypes.Structure):
        _fields_ = [
            ("usFlags", ctypes.c_ushort),
            ("usButtonFlags", ctypes.c_ushort),
            ("usButtonData", ctypes.c_short),
            ("ulRawButtons", ctypes.c_ulong),
            ("lLastX", ctypes.c_long),
            ("lLastY", ctypes.c_long),
            ("ulExtraInformation", ctypes.c_ulong),
        ]

    class RAWINPUT(ctypes.Structure):
        _fields_ = [("header", RAWINPUTHEADER), ("mouse", RAWMOUSE)]

    class RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [
            ("usUsagePage", ctypes.c_ushort),
            ("usUsage", ctypes.c_ushort),
            ("dwFlags", ctypes.c_ulong),
            ("hwndTarget", ctypes.c_void_p),
        ]

    class WNDCLASS(ctypes.Structure):
        _fields_ = [
            ("style", ctypes.c_uint),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", ctypes.c_void_p),
            ("hIcon", ctypes.c_void_p),
            ("hCursor", ctypes.c_void_p),
            ("hbrBackground", ctypes.c_void_p),
            ("lpszMenuName", ctypes.c_wchar_p),
            ("lpszClassName", ctypes.c_wchar_p),
        ]

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("message", ctypes.c_uint),
            ("wParam", ctypes.c_size_t),
            ("lParam", ctypes.c_ssize_t),
            ("time", ctypes.c_ulong),
            ("pt", POINT),
        ]

    _LL_HOOK_PROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t
    )
    _WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t
    )

    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _LL_HOOK_PROC, ctypes.c_void_p, ctypes.c_ulong]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    user32.ToUnicodeEx.restype = ctypes.c_int
    user32.ToUnicodeEx.argtypes = [
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_char * 256,
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_void_p,
    ]
    user32.GetKeyboardLayout.restype = ctypes.c_void_p
    # Handles are pointers: without these, ctypes truncates them to a 32-bit
    # int and a 64-bit module handle comes back as rubbish.
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
    kernel32.GetCurrentThread.restype = ctypes.c_void_p
    kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.PostThreadMessageW.argtypes = [ctypes.c_ulong, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
    user32.UnregisterClassW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
    user32.GetKeyState.restype = ctypes.c_short
    user32.GetKeyState.argtypes = [ctypes.c_int]
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.CreateWindowExW.argtypes = [
        ctypes.c_ulong,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    user32.DestroyWindow.argtypes = [ctypes.c_void_p]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), ctypes.c_uint, ctypes.c_uint]
    user32.GetRawInputData.argtypes = [
        ctypes.c_ssize_t,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.c_uint,
    ]
else:  # pragma: no cover - the structures above are only used on Windows
    POINT = KBDLLHOOKSTRUCT = MSLLHOOKSTRUCT = MSG = None
    RAWINPUT = RAWINPUTDEVICE = WNDCLASS = None
    _LL_HOOK_PROC = _WNDPROC = None


_WINDOW_CLASS_NAME = "BeamerRawInput"
_window_class_registered = False


def read_caps_lock() -> bool:
    """Caps lock as Windows has it right now. Read when the hooks go in: the
    hook only ever sees the key being pressed, so starting from a guess means
    a caps-on keyboard types in the wrong case on the Mac until it is toggled
    twice."""
    if user32 is None:
        return False
    return bool(user32.GetKeyState(VK_CAPITAL) & 1)


def _create_message_window(window_proc, module):
    """A message-only window, which exists for one reason: RIDEV_INPUTSINK
    needs a window to deliver WM_INPUT to, and only a window can receive raw
    input while another application is in the foreground -- which is every
    moment that matters here."""
    global _window_class_registered
    if not _window_class_registered:
        window_class = WNDCLASS()
        window_class.lpfnWndProc = ctypes.cast(window_proc, ctypes.c_void_p)
        window_class.hInstance = module
        window_class.lpszClassName = _WINDOW_CLASS_NAME
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())
        _window_class_registered = True
    window = user32.CreateWindowExW(
        0, _WINDOW_CLASS_NAME, _WINDOW_CLASS_NAME, 0, 0, 0, 0, 0, HWND_MESSAGE, None, module, None
    )
    if not window:
        raise ctypes.WinError(ctypes.get_last_error())
    return window


def _unregister_window_class(module) -> None:
    global _window_class_registered
    if not _window_class_registered:
        return
    if user32.UnregisterClassW(_WINDOW_CLASS_NAME, module):
        _window_class_registered = False
    else:
        LOGGER.warning("The raw-input window class could not be unregistered")


def _register_raw_mouse(window):
    device = RAWINPUTDEVICE(usUsagePage=0x01, usUsage=0x02, dwFlags=RIDEV_INPUTSINK, hwndTarget=window)
    if not user32.RegisterRawInputDevices(ctypes.byref(device), 1, ctypes.sizeof(RAWINPUTDEVICE)):
        raise ctypes.WinError(ctypes.get_last_error())


def _raw_mouse_motion(lparam):
    """The hand's relative counts in one WM_INPUT, or None."""
    size = ctypes.c_uint(ctypes.sizeof(RAWINPUT))
    raw = RAWINPUT()
    read = user32.GetRawInputData(
        lparam, RID_INPUT, ctypes.byref(raw), ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER)
    )
    if read == ctypes.c_uint(-1).value or raw.header.dwType != RIM_TYPEMOUSE:
        return None
    return hand_motion(raw.mouse.usFlags, raw.mouse.lLastX, raw.mouse.lLastY, raw.mouse.ulExtraInformation)


def hand_motion(flags: int, last_x: int, last_y: int, extra_information: int) -> Optional[Tuple[int, int]]:
    """What one RAWMOUSE says the hand did, or None when it did nothing that
    an edge can measure. A tablet or a remote-desktop pointer reports absolute
    positions instead; there is nothing to measure in those, and the hook's
    own point is the honest reading for them. A move carrying the injector's
    stamp is the Mac's pointer, not the hand's: Raw Input reports a SendInput
    move exactly as it reports the mouse, and the stamp in ulExtraInformation
    is the only difference."""
    if flags & MOUSE_MOVE_ABSOLUTE:
        return None
    if extra_information == INJECTED_MARK:
        return None
    if not last_x and not last_y:
        return None
    return last_x, last_y


def _to_unicode(vk: int, scan: int, state: Dict[int, int]) -> Optional[Union[str, DeadKey]]:
    """The character `vk` types under `state`, or None. Called with the
    non-shift modifiers already cleared, so a chord yields its plain key.

    ToUnicodeEx is asked not to change the keyboard state (the 1<<2 flag):
    the dead accent is tracked in KeyboardState instead.
    """
    if user32 is None:
        raise RuntimeError("ToUnicodeEx is only available on Windows")
    key_state = (ctypes.c_char * 256)()
    for key, value in state.items():
        key_state[key] = bytes([value])
    buffer = ctypes.create_unicode_buffer(8)
    layout = user32.GetKeyboardLayout(0)
    count = user32.ToUnicodeEx(vk, scan, key_state, buffer, len(buffer), 1 << 2, layout)
    if count < 0:
        return DeadKey(buffer.value[:abs(count)]) if buffer.value else None
    if count == 0:
        return None
    return buffer.value[:count]
