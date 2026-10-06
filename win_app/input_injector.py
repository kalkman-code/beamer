"""Win32 SendInput keyboard and mouse injection."""

import ctypes
import functools
import logging
import subprocess
import sys
import threading
import time
import unicodedata
from typing import Callable, Dict, List, Optional, Set, Tuple

from core.keytable import EVDEV_US


LOGGER = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

user32 = ctypes.WinDLL("user32", use_last_error=True) if _IS_WINDOWS else None

if _IS_WINDOWS:
    user32.VkKeyScanW.restype = ctypes.c_short
    user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
    user32.MapVirtualKeyW.restype = ctypes.c_uint
    user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
    user32.GetKeyboardLayout.restype = ctypes.c_void_p
    user32.GetKeyboardLayout.argtypes = [ctypes.c_ulong]
    user32.ToUnicodeEx.restype = ctypes.c_int
    user32.ToUnicodeEx.argtypes = [
        ctypes.c_uint, ctypes.c_uint, ctypes.c_char * 256, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p,
    ]
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

# Stamped into the dwExtraInfo of every INPUT this module sends, and the same
# value the Mac stamps into kCGEventSourceUserData. Raw Input has no injected
# flag: a SendInput move reaches a WM_INPUT sink exactly as the hand's does,
# hDevice NULL, and this value coming back in RAWMOUSE.ulExtraInformation is
# the only thing that tells the two apart.
INJECTED_MARK = 0xBEA3

# What each key types on a US keyboard, by scan code, which names the key's place rather than its
# label. Key messages carry it as `us` both ways, and a character this layout cannot type lands on
# its place when it has to (_by_place). Set-1 scan codes are evdev codes, so it is the key table's.
SCAN_TO_US: Dict[int, str] = EVDEV_US
US_SCAN_CODES = {char: scan for scan, char in SCAN_TO_US.items()}
MAPVK_VSC_TO_VK = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

MAPVK_VK_TO_VSC = 0

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
XBUTTON1 = 0x0001
XBUTTON2 = 0x0002
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTUNION)]


def _send_input(*inputs: INPUT) -> None:
    if user32 is None:
        raise RuntimeError("Win32 SendInput is only available on Windows")
    count = len(inputs)
    array = (INPUT * count)(*inputs)
    sent = user32.SendInput(count, array, ctypes.sizeof(INPUT))
    if sent != count:
        raise ctypes.WinError(ctypes.get_last_error())


def _vk_key_scan(ch: str) -> int:
    """Look up the VK + shift-state byte for a character via VkKeyScanW.

    Returns the raw 16-bit result (low byte VK, high byte shift state) or
    -1 if the character can't be produced by the current keyboard layout.
    """
    if user32 is None:
        raise RuntimeError("VkKeyScanW is only available on Windows")
    return user32.VkKeyScanW(ch)


def _map_virtual_key(vk: int) -> int:
    """Look up the hardware scan code for a virtual-key code."""
    if user32 is None:
        raise RuntimeError("MapVirtualKeyW is only available on Windows")
    return user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)


def _key_at(us: str) -> Optional[Tuple[int, Optional[str]]]:
    """The virtual key at `us`'s place on this layout and the character it types there
    unshifted (None for a dead key or none), or None for a place with no scan code."""
    if user32 is None:
        raise RuntimeError("MapVirtualKeyW is only available on Windows")
    scan = US_SCAN_CODES.get(us)
    if scan is None:
        return None
    vk = user32.MapVirtualKeyW(scan, MAPVK_VSC_TO_VK)
    if not vk:
        return None
    # Not MapVirtualKeyW's MAPVK_VK_TO_CHAR, which answers A to Z for the letter keys on every
    # layout: on Russian the C key would read as "c" and a letter from an English Mac stay text.
    # Flag 1<<2 leaves the keyboard state alone, so a dead key here eats no accent being typed.
    buffer = ctypes.create_unicode_buffer(8)
    count = user32.ToUnicodeEx(vk, scan, (ctypes.c_char * 256)(), buffer, len(buffer), 1 << 2, user32.GetKeyboardLayout(0))
    return vk, buffer.value[:count] if count == 1 else None


VK_MAP = {
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "page_up": 0x21,
    "page_down": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "shift": 0xA0,
    "shift_r": 0xA1,
    "ctrl": 0xA2,
    "ctrl_r": 0xA3,
    "alt": 0xA4,
    "alt_r": 0xA5,
    "cmd": 0x5B,
    "cmd_r": 0x5C,
    "caps_lock": 0x14,
    "insert": 0x2D,
    "menu": 0x5D,
    "num_lock": 0x90,
    "scroll_lock": 0x91,
    "print_screen": 0x2C,
    "pause": 0x13,
    "media_play_pause": 0xB3,
    "media_next": 0xB0,
    "media_prev": 0xB1,
    "media_stop": 0xB2,
    "volume_mute": 0xAD,
    "volume_down": 0xAE,
    "volume_up": 0xAF,
    "browser_back": 0xA6,
    "browser_forward": 0xA7,
}

for _index in range(1, 13):
    VK_MAP[f"f{_index}"] = 0x70 + (_index - 1)
for _index in range(13, 21):
    VK_MAP[f"f{_index}"] = 0x7C + (_index - 13)

# Named keys that require the extended-key flag (E0 scan prefix) so games,
# RDP sessions and low-level keyboard hooks read them correctly.
EXTENDED_KEYS = {
    "up",
    "down",
    "left",
    "right",
    "insert",
    "delete",
    "home",
    "end",
    "page_up",
    "page_down",
    "ctrl_r",
    "alt_r",
    "cmd",
    "cmd_r",
    "num_lock",
    "print_screen",
    "media_play_pause",
    "media_next",
    "media_prev",
    "media_stop",
    "volume_mute",
    "volume_down",
    "volume_up",
    "browser_back",
    "browser_forward",
}

# Modifier key names that participate in chording with character keys.
MODIFIER_KEYS = {"ctrl", "ctrl_r", "alt", "alt_r", "cmd", "cmd_r", "shift", "shift_r"}

# Either shift key satisfies a VkKeyScanW "shift required" result: the Mac
# forwards shift physically as its own keydown/keyup, so if one is currently
# held, the keyboard state agrees with what VkKeyScanW resolved the
# character from.
SHIFT_KEYS = {"shift", "shift_r"}

CHORD_KEYS = MODIFIER_KEYS - SHIFT_KEYS

VkLookup = Callable[[str], int]
ScanLookup = Callable[[int], int]
PlaceLookup = Callable[[str], Optional[Tuple[int, Optional[str]]]]

# Characters we've already logged an unsupported-combo warning for, so we
# don't spam the log for every repeat keypress.
_warned_chars: Set[str] = set()


def _keybd_input(vk: int, scan: int, flags: int) -> INPUT:
    key_input = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=INJECTED_MARK)
    return INPUT(type=INPUT_KEYBOARD, union=_INPUTUNION(ki=key_input))


def _script(character: str) -> str:
    return unicodedata.name(character, "").split(" ")[0]


def _by_place(ch: str, us: Optional[str], mods_down: Set[str], place_lookup: Optional[PlaceLookup]) -> Optional[Tuple[int, Optional[str]]]:
    """The virtual key at `us`, the place the sender's key sits on a US keyboard, and what it
    types here, for a character this layout cannot type: under a chord, which has to reach the
    app as a shortcut, and for a letter of another script than the one this layout types there,
    so a Mac on Russian types this PC's English and one on English its Russian, as the PC's own
    keyboard would. Anything else, an é from a French Mac say, stays text: the key it sits on
    types something unrelated here. The twin of input_injector_mac._by_place."""
    if us is None or place_lookup is None:
        return None
    place = place_lookup(us)
    if place is None:
        return None
    here = place[1]
    if mods_down & CHORD_KEYS:
        return place
    if ch.isalpha() and here and here.isalpha() and _script(ch) != _script(here):
        return place
    return None


def plan_key_inputs(
    name: str,
    down: bool,
    mods_down: Set[str],
    char_vk_down: Dict[str, int],
    vk_lookup: VkLookup,
    scan_lookup: ScanLookup,
    us: Optional[str] = None,
    place_lookup: Optional[PlaceLookup] = None,
) -> List[Tuple[int, int, int]]:
    """Pure planning logic: decide which (wVk, wScan, flags) tuples to send.

    Mutates mods_down / char_vk_down to track state across calls, exactly
    like inject_key does, so callers (tests) can inspect the bookkeeping.
    Returns an empty list if the key should be dropped. `us` is where the
    key sits on a US keyboard, sent by a physical keyboard and absent from
    a phone's.
    """
    keyup_flag = 0 if down else KEYEVENTF_KEYUP
    lowered = name.lower()

    if lowered in MODIFIER_KEYS:
        if down:
            mods_down.add(lowered)
        else:
            mods_down.discard(lowered)

    # Named key (arrows, modifiers, function keys, etc.)
    if lowered in VK_MAP:
        vk = VK_MAP[lowered]
        scan = scan_lookup(vk)
        flags = keyup_flag
        if lowered in EXTENDED_KEYS:
            flags |= KEYEVENTF_EXTENDEDKEY
        return [(vk, scan, flags)]

    # Single character. Prefer the real VK+scan keystroke path whenever the
    # current keyboard layout can produce it plainly, or with only the shift
    # state the Mac is already physically forwarding -- not just when a
    # chord modifier (ctrl/alt/win) happens to be held. A bare character
    # sent purely as KEYEVENTF_UNICODE produces WM_CHAR-style text but no
    # usable keyCode, so anything listening for a real keydown (games, media
    # shortcuts like YouTube's "k" to pause) never sees it, even though a
    # text field happily receives the character. Unicode remains the
    # fallback for whatever the VK path can't safely cover.
    if len(name) == 1:
        ch = name
        if ch in char_vk_down:
            # A repeat stays on the key the press went down on, even if the layout changed since,
            # so its release lets go of the key that is actually down.
            vk = char_vk_down[ch] if down else char_vk_down.pop(ch)
            return [(vk, scan_lookup(vk), keyup_flag)]

        result = vk_lookup(ch) if down else -1
        place = _by_place(ch, us, mods_down, place_lookup) if result == -1 else None
        if place is not None and ch.isupper() and not mods_down & (SHIFT_KEYS | CHORD_KEYS):
            # A capital from caps lock: the key would type this layout's small letter. The
            # release is worked out the same way, so it lets go of the same character.
            return [(0, ord(place[1].upper()), KEYEVENTF_UNICODE | keyup_flag)]
        if not down:
            return [(0, ord(ch), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
        if result == -1:
            if place is not None:
                vk = place[0]
                char_vk_down[ch] = vk
                return [(vk, scan_lookup(vk), keyup_flag)]
            if ch not in _warned_chars:
                LOGGER.warning("Unsupported character for this keyboard layout: %r", ch)
                _warned_chars.add(ch)
            return [(0, ord(ch), KEYEVENTF_UNICODE | keyup_flag)]

        vk = result & 0xFF
        shift_state = (result >> 8) & 0xFF
        shift_only = shift_state == 0x01
        if shift_state == 0 or (shift_only and mods_down & SHIFT_KEYS):
            scan = scan_lookup(vk)
            char_vk_down[ch] = vk
            return [(vk, scan, keyup_flag)]
        if shift_state & 0xFE:
            # Needs ctrl/alt (AltGr) or some other combo bit we can't
            # fabricate without also toggling a modifier the user isn't
            # actually holding.
            if ch not in _warned_chars:
                LOGGER.warning("Combo not possible for this character on this layout: %r", ch)
                _warned_chars.add(ch)
        # else: shift is required but not currently held -- e.g. a capital
        # produced by caps-lock rather than a physical shift press. Trusting
        # the VK here would desync from the real keyboard state, so this
        # falls back to Unicode silently (it's not a broken layout, just a
        # state VkKeyScanW can't be told about); typing stays correct.
        return [(0, ord(ch), KEYEVENTF_UNICODE | keyup_flag)]

    LOGGER.warning("Unknown key name ignored: %r", name)
    return []


_mods_down: Set[str] = set()
_char_vk_down: Dict[str, int] = {}
_named_down: Dict[str, Tuple[int, int, int]] = {}
# One lock over the held-key bookkeeping and the send it describes: a session
# thread injecting while a reconnect's release_all snapshots and clears would
# otherwise press a key that nothing then remembers to release.
_state_lock = threading.RLock()


def _locked(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with _state_lock:
            return function(*args, **kwargs)

    return wrapper


@_locked
def inject_key(name: str, down: bool, us: Optional[str] = None) -> None:
    LOGGER.debug("key %s %s (mods held: %s)", name, "down" if down else "up", sorted(_mods_down))
    lowered = name.lower()
    held = _named_down.get(lowered) if lowered in VK_MAP and lowered not in MODIFIER_KEYS else None
    if held is not None:
        vk, scan, flags = held
        plan = [(vk, scan, flags if down else flags | KEYEVENTF_KEYUP)]
        if not down:
            _named_down.pop(lowered, None)
    else:
        plan = plan_key_inputs(name, down, _mods_down, _char_vk_down, _vk_key_scan, _map_virtual_key, us, _key_at)
        if down and plan and lowered in VK_MAP and lowered not in MODIFIER_KEYS:
            _named_down[lowered] = plan[0]
    if not plan:
        return
    _send_input(*(_keybd_input(vk, scan, flags) for vk, scan, flags in plan))


def plan_text_inputs(text: str) -> List[Tuple[int, int, int]]:
    """(vk, scan, flags) for typing `text` as characters: a Unicode down and up for each UTF-16 code
    unit, so a character outside the Basic Multilingual Plane goes as its surrogate pair."""
    plan = []
    data = text.encode("utf-16-le")
    for index in range(0, len(data), 2):
        unit = int.from_bytes(data[index:index + 2], "little")
        plan.append((0, unit, KEYEVENTF_UNICODE))
        plan.append((0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return plan


@_locked
def inject_text(text: str) -> None:
    """Types `text` as it stands (WIRE.md section 10): the layout and any modifier held play no part."""
    plan = plan_text_inputs(text)
    if plan:
        _send_input(*(_keybd_input(vk, scan, flags) for vk, scan, flags in plan))


def _mouse_input(dx: int, dy: int, data: int, flags: int) -> INPUT:
    mouse_input = MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags, time=0, dwExtraInfo=INJECTED_MARK)
    return INPUT(type=INPUT_MOUSE, union=_INPUTUNION(mi=mouse_input))


def inject_mouse_move(dx: int, dy: int) -> None:
    _send_input(_mouse_input(dx, dy, 0, MOUSEEVENTF_MOVE))
    watch_foreground()


# GameInputSvc.exe runs as SYSTEM in the console session and keeps a hidden
# window of this class, and Windows can hand that window the foreground when
# the foreground app closes (closing a Hyper-V VM Connection window did it).
# While it is in front, UIPI refuses every SetCursorPos
# and silently drops every SendInput from Beamer, which is elevated but not
# SYSTEM, so the Mac's pointer goes dead on the PC. Nothing Beamer can call
# takes the foreground back -- SetForegroundWindow, SwitchToThisWindow and an
# injected Alt tap were all refused -- and moving the PC's own mouse does not
# either; a click on the PC does, and so does restarting the service, which
# takes the window with it and leaves Windows to activate an ordinary one.
GAMEINPUT_WINDOW_CLASS = "GameInputServiceWindow"
GAMEINPUT_RESTART = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Restart-Service GameInputSvc -Force"]
# A restart that did not free the pointer must not become a restart loop.
GAMEINPUT_RESTART_COOLDOWN_SECONDS = 60.0

_seen_foreground = [None]
_gameinput_lock = threading.Lock()
_gameinput_restarted_at = [float("-inf")]


def _window_class(hwnd) -> str:
    name = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, name, 128)
    return name.value


def foreground_class() -> str:
    hwnd = user32.GetForegroundWindow()
    return _window_class(hwnd) if hwnd else ""


def watch_foreground() -> None:
    """Called after every move the peer sends: one GetForegroundWindow, and a
    class lookup only when the foreground has changed since the last move or
    GameInput's window still holds it."""
    hwnd = user32.GetForegroundWindow()
    if hwnd == _seen_foreground[0]:
        return
    if hwnd and _window_class(hwnd) == GAMEINPUT_WINDOW_CLASS:
        # Never marked seen, so a restart that failed or is cooling down is
        # tried again on a later move.
        release_gameinput_foreground()
        return
    _seen_foreground[0] = hwnd


def release_gameinput_foreground(after: Optional[Callable[[], None]] = None) -> bool:
    """If GameInput's window holds the foreground, restart GameInputSvc on a
    thread of its own -- it takes a second or two, and the caller is the
    session thread that must keep draining pings -- then run `after`. False
    when GameInput is not in front, or a restart is already running or ran
    within the cooldown."""
    if foreground_class() != GAMEINPUT_WINDOW_CLASS:
        return False
    if time.monotonic() - _gameinput_restarted_at[0] < GAMEINPUT_RESTART_COOLDOWN_SECONDS:
        return False
    if not _gameinput_lock.acquire(blocking=False):
        return False
    _gameinput_restarted_at[0] = time.monotonic()

    def restart() -> None:
        try:
            LOGGER.warning(
                "GameInput's service window has the foreground, where Windows refuses Beamer's input; restarting GameInputSvc to free it"
            )
            try:
                completed = subprocess.run(
                    GAMEINPUT_RESTART, capture_output=True, text=True, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                LOGGER.exception("Could not restart GameInputSvc; a click on this PC frees the pointer")
                return
            if completed.returncode != 0:
                LOGGER.error("Restarting GameInputSvc failed (%s): %s", completed.returncode, completed.stderr.strip())
                return
            LOGGER.info("GameInputSvc restarted; %r now has the foreground", foreground_class())
            if after is not None:
                after()
        except Exception:
            LOGGER.exception("Freeing the foreground from GameInput failed")
        finally:
            _gameinput_lock.release()

    threading.Thread(target=restart, name="Beamer-gameinput", daemon=True).start()
    return True


BUTTON_DOWN_FLAGS = {
    "left": MOUSEEVENTF_LEFTDOWN,
    "right": MOUSEEVENTF_RIGHTDOWN,
    "middle": MOUSEEVENTF_MIDDLEDOWN,
    "back": MOUSEEVENTF_XDOWN,
    "forward": MOUSEEVENTF_XDOWN,
}

BUTTON_UP_FLAGS = {
    "left": MOUSEEVENTF_LEFTUP,
    "right": MOUSEEVENTF_RIGHTUP,
    "middle": MOUSEEVENTF_MIDDLEUP,
    "back": MOUSEEVENTF_XUP,
    "forward": MOUSEEVENTF_XUP,
}

# Which X button, carried in mouseData: the side buttons share one pair of flags.
BUTTON_DATA = {"back": XBUTTON1, "forward": XBUTTON2}


_buttons_down: Set[str] = set()


@_locked
def inject_mouse_button(button: str, down: bool) -> None:
    flags = (BUTTON_DOWN_FLAGS if down else BUTTON_UP_FLAGS).get(button)
    if flags is None:
        LOGGER.warning("Unknown mouse button ignored: %r", button)
        return
    if down:
        _buttons_down.add(button)
    else:
        _buttons_down.discard(button)
    _send_input(_mouse_input(0, 0, BUTTON_DATA.get(button, 0), flags))


@_locked
def release_all() -> None:
    """Let go of every key and button this module is holding, the twin of
    input_injector_mac.release_all: the receiver calls it when the peer's
    input goes home or its link dies, so a modifier held through a switch
    does not stay down on this PC."""
    for name in sorted(_named_down):
        try:
            inject_key(name, down=False)
        except Exception:
            LOGGER.exception("Could not release %r", name)
    for name in sorted(_mods_down):
        try:
            inject_key(name, down=False)
        except Exception:
            LOGGER.exception("Could not release %r", name)
    for ch in sorted(_char_vk_down):
        try:
            inject_key(ch, down=False)
        except Exception:
            LOGGER.exception("Could not release %r", ch)
    for button in sorted(_buttons_down):
        try:
            inject_mouse_button(button, down=False)
        except Exception:
            LOGGER.exception("Could not release the %s mouse button", button)
    _mods_down.clear()
    _char_vk_down.clear()
    _named_down.clear()
    _buttons_down.clear()


# Continuous (trackpad) scroll deltas arrive in points, not wheel "clicks".
# This converts px -> the same 120-per-notch wheel-unit scale a discrete
# click uses, so pixel-mode motion feels proportionate to line-mode motion.
# Tunable: raise it to make trackpad scrolling slower, lower it for faster.
WHEEL_UNITS_PER_PIXEL = 120 / 40  # 3.0

# Fractional wheel units carried across calls, per axis, for pixel-mode
# scroll (see plan_scroll_units). Module state, like _mods_down/_char_vk_down
# above: there is one physical scroll wheel to drive, regardless of how many
# scroll messages arrive.
_scroll_accum = [0.0, 0.0]  # [y, x]


def plan_scroll_units(dy, dx, mode: str, accum: List[float]) -> Tuple[int, int]:
    """Pure planning logic: decide the whole wheel-unit deltas to send for one
    scroll event, given accumulator state `accum` (a mutable [y, x] pair,
    mutated in place so callers/tests can inspect the carried residue exactly
    like inject_scroll does). Returns (whole_y, whole_x); either may be 0,
    meaning nothing to send on that axis for this event.

    mode "line": dy/dx are whole wheel clicks (as sent by a discrete mouse
    wheel); each maps to +/-120 wheel units -- unchanged from before dx/mode
    existed.

    mode "pixel": dy/dx are continuous trackpad point deltas; converted to
    wheel units via WHEEL_UNITS_PER_PIXEL and accumulated across calls so
    slow, precise motion isn't lost to integer truncation.

    Sign convention: the vertical mapping below (dy -> +120 per unit) is
    intentionally unchanged from today's dy*120 behaviour, whatever that
    already works out to with Mac natural scrolling. MOUSEEVENTF_HWHEEL's
    positive mouseData means "scroll right" (the mirror of
    MOUSEEVENTF_WHEEL, where positive means "scroll up/away"); dx is
    forwarded without an extra sign flip, on the assumption that a rightward
    two-finger swipe on the Mac should scroll content right on Windows, the
    same direction it would on the Mac itself. That assumption -- and the
    WHEEL_UNITS_PER_PIXEL constant above -- still needs confirming on real
    trackpad/mouse hardware; both are easy to retune if the felt direction or
    speed is wrong.
    """
    if mode == "pixel":
        accum[0] += dy * WHEEL_UNITS_PER_PIXEL
        accum[1] += dx * WHEEL_UNITS_PER_PIXEL
    else:
        # float, not int: a receiver's scroll speed can make a click a fraction of one.
        accum[0] += float(dy) * 120
        accum[1] += float(dx) * 120
    whole_y = int(accum[0])
    whole_x = int(accum[1])
    accum[0] -= whole_y
    accum[1] -= whole_x
    return whole_y, whole_x


def inject_scroll(dy, dx=0.0, mode: str = "line") -> None:
    whole_y, whole_x = plan_scroll_units(dy, dx, mode, _scroll_accum)
    if whole_y:
        _send_input(_mouse_input(0, 0, whole_y, MOUSEEVENTF_WHEEL))
    if whole_x:
        _send_input(_mouse_input(0, 0, whole_x, MOUSEEVENTF_HWHEEL))


# What each Mac gesture becomes here: first a real touchpad swipe, replayed
# through a synthetic Precision Touchpad so this PC's own Settings > Touchpad
# choices apply and Task View animates as it would under fingers; if that
# device cannot be made, the shortcut that means the same thing by default.
# Finger direction maps 1:1 because content follows the fingers on both
# systems: fingers moving left reveal the desktop on the right, which is
# Win+Ctrl+Right. Launchpad has no touchpad gesture on Windows, so it opens
# Start. "cmd" is the Windows key in VK_MAP.
GESTURE_PLAN: Dict[str, Tuple[Optional[Tuple[int, str]], Tuple[str, ...]]] = {
    "swipe_up": ((3, "up"), ("cmd", "tab")),
    "swipe_down": ((3, "down"), ("cmd", "d")),
    "spread": ((3, "down"), ("cmd", "d")),
    "swipe_left": ((4, "left"), ("cmd", "ctrl", "right")),
    "swipe_right": ((4, "right"), ("cmd", "ctrl", "left")),
    "pinch": (None, ("cmd",)),
}


def inject_gesture(name: str, swipe=None) -> None:
    """`swipe` is touchpad_injector.swipe unless a test passes its own; it
    returns False when Windows refuses, and the chord is pressed instead."""
    plan = GESTURE_PLAN.get(name)
    if plan is None:
        LOGGER.warning("Unknown gesture ignored: %r", name)
        return
    touchpad, chord = plan
    if touchpad is not None:
        if swipe is None:
            import touchpad_injector

            swipe = touchpad_injector.swipe
        if swipe(*touchpad):
            return
    for key in chord:
        inject_key(key, down=True)
    for key in reversed(chord):
        inject_key(key, down=False)
