"""Keyboard and mouse capture on X11: the twin of capture_win.py, with the same Hooks, Trigger and
names, so the bridge and the sender drive it unchanged.

X has no per-event swallow, so capture is two things. A listener: XInput2 raw events, selected on
the root window, report every key, button and movement whoever has focus. And, while input is on
another machine, an active grab of every master device, which keeps their events from every other
client until it is let go. The grab is the capture thread's alone, taken and let go between
bounded batches of events as `grab_while`'s predicate says; it lives on this thread's own
connection, so a process that dies takes it with its socket, and a watchdog closes that socket if
the thread stops answering while it holds the grab, as Windows removes a hook that stops answering.

Raw events reach a client that holds a grab only through the grab's own event mask (proved on
Xvfb on 01-10-2026: with an empty mask, a grabbed pointer's motion stopped arriving), so each grab
asks for them again.

Keys from inside a grab come from the grab's XI2 key events, which carry the state at the moment
of the key: modifiers, layout group and the auto-repeat flag. Raw key events are used for keys
from outside one. Which side of a grab an event came from is read from its sequence number against
the grab's request, not from whether the grab is held when the event is read: a release queued
just before the grab would otherwise be lost, and one queued inside it read twice. A key's release
and repeats go out under the name its press went out under.

An event inside a grab that the sender hands back (an input on the stays-here list, a key at the
moment input came home) is played to this machine through XTest with the grab let go around it,
since the grab has already kept it from every app.

Movement is the mouse's own unaccelerated counts from raw motion, as Windows reads WM_INPUT, and
each one is also reported as a WM_MOUSEMOVE so the sender's pin warps the pointer back while input
is away: XWarpPointer makes no raw event, as SetCursorPos makes no WM_INPUT. No confine_to: a
confined pointer would swallow the sender's arrival warp when input comes home by a crossing. The
wheel is the scroll valuators of a smooth-scrolling device (libinput), whose emulated buttons 4 to
7 are skipped, and buttons 4 to 7 of any other.

Events from the XTEST devices are this machine's own injection, dropped as capture_win drops
LLKHF_INJECTED. A pointer that reports absolute positions (a tablet, a VM's pointer) moves the pin
but gives no pressure, as on Windows.

python-xlib and libxkbcommon are reached only when the hooks start, so this module imports
anywhere."""

import collections
import ctypes
import ctypes.util
import logging
import select
import socket
import struct
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

# The pure pieces are Windows' own: the sender and the bridge read Windows mouse messages, and the
# trigger is platform-free.
from capture_win import (  # noqa: F401  (re-exported for the bridge, which reads them from here)
    BUTTON_TITLES,
    MODIFIER_NAMES,
    REDIRECT,
    RETURN,
    TOGGLE,
    WHEEL_DELTA,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_MBUTTONDOWN,
    WM_MBUTTONUP,
    WM_MOUSEHWHEEL,
    WM_MOUSEMOVE,
    WM_MOUSEWHEEL,
    WM_RBUTTONDOWN,
    WM_RBUTTONUP,
    WM_XBUTTONDOWN,
    WM_XBUTTONUP,
    Trigger,
    button_of,
    mouse_event,
    wheel_notches,
)
from core import keytable

LOGGER = logging.getLogger(__name__)

# XI2.h, so nothing here needs Xlib to import.
XI_KEY_PRESS, XI_KEY_RELEASE = 2, 3
XI_HIERARCHY_CHANGED = 11
XI_RAW_KEY_PRESS, XI_RAW_KEY_RELEASE = 13, 14
XI_RAW_BUTTON_PRESS, XI_RAW_BUTTON_RELEASE = 15, 16
XI_RAW_MOTION = 17
XI_ALL_DEVICES, XI_ALL_MASTER_DEVICES = 0, 1
XI_MASTER_POINTER, XI_MASTER_KEYBOARD, XI_SLAVE_POINTER = 1, 2, 3
XI_KEY_REPEAT = 1 << 16
XI_POINTER_EMULATED = 1 << 16
XI_MODE_ABSOLUTE = 1
XI_SCROLL_VERTICAL = 1
GENERIC_EVENT = 35
MAPPING_NOTIFY = 34
MAPPING_POINTER = 2

RAW_MASK = sum(1 << kind for kind in (XI_RAW_KEY_PRESS, XI_RAW_KEY_RELEASE, XI_RAW_BUTTON_PRESS, XI_RAW_BUTTON_RELEASE, XI_RAW_MOTION))
POINTER_GRAB_MASK = sum(1 << kind for kind in (XI_RAW_BUTTON_PRESS, XI_RAW_BUTTON_RELEASE, XI_RAW_MOTION))
KEYBOARD_GRAB_MASK = sum(1 << kind for kind in (XI_KEY_PRESS, XI_KEY_RELEASE, XI_RAW_KEY_PRESS, XI_RAW_KEY_RELEASE))

# Core modifier bits. Control, Mod1 (Alt) and Mod4 (Super) are where every desktop maps them.
SHIFT_MASK, LOCK_MASK, CONTROL_MASK, MOD1_MASK, MOD2_MASK, MOD4_MASK = 1, 2, 4, 8, 16, 64
CHORD_MASK = CONTROL_MASK | MOD1_MASK | MOD4_MASK
NUM_LOCK_MASK = MOD2_MASK

KEYCODE_OFFSET = 8  # an X keycode is the evdev code plus 8, on every server that uses evdev or libinput
TICK_SECONDS = 0.05
MAX_BATCH = 128  # events read before the grab is looked at again, however fast they come
STALL_SECONDS = 2.0
# How long a let-go grab's span is kept: every event from inside it has been read long before, and a
# span kept for ever would match again once the 16-bit sequence number came round.
SPAN_KEEP_SECONDS = 5.0

# Wire names by evdev code, Mac-shaped as capture_win's: this keyboard's Ctrl is "cmd" and its Super
# key "ctrl", the Semantic style the sender swaps back to physical names.
VK_TO_NAME: Dict[int, str] = {
    code: keytable.wire_name(physical, "linux", "macos", "semantic") for code, physical in keytable.EVDEV_NAMES.items()
}

# The keypad with Num Lock off moves, as Windows' VK_HOME and the rest do.
KEYPAD_NAMES = {0xFF95: "home", 0xFF96: "left", 0xFF97: "up", 0xFF98: "right", 0xFF99: "down",
                0xFF9A: "page_up", 0xFF9B: "page_down", 0xFF9C: "end", 0xFF9E: "insert", 0xFF9F: "delete"}

_TITLES = {
    "shift": "Left Shift", "shift_r": "Right Shift", "ctrl": "Left Ctrl", "ctrl_r": "Right Ctrl",
    "alt": "Left Alt", "alt_r": "Right Alt", "cmd": "Left Super", "cmd_r": "Right Super", "caps_lock": "Caps Lock",
    "backspace": "Backspace", "tab": "Tab", "enter": "Enter", "esc": "Esc", "space": "Space", "delete": "Delete",
    "insert": "Insert", "home": "Home", "end": "End", "page_up": "Page Up", "page_down": "Page Down", "left": "Left",
    "right": "Right", "up": "Up", "down": "Down", "menu": "Menu", "print_screen": "Print Screen", "pause": "Pause",
    "num_lock": "Num Lock", "scroll_lock": "Scroll Lock", "browser_back": "Browser Back",
    "browser_forward": "Browser Forward", "volume_mute": "Mute", "volume_down": "Volume Down", "volume_up": "Volume Up",
    "media_next": "Next Track", "media_prev": "Previous Track", "media_stop": "Stop", "media_play_pause": "Play/Pause",
}
_TITLES.update({f"f{index}": f"F{index}" for index in range(1, 25)})
VK_TITLES: Dict[int, str] = {code: _TITLES[physical] for code, physical in keytable.EVDEV_NAMES.items()}
VK_TITLES[96] = "Num Enter"

# Offered by the recorder or not, as app_config reads capture_win's: media and browser keys are for
# the stays-here list, and typing keys a double-tap or a hold would take from every app. On X the
# trigger also reaches apps at home, so Print Screen stays out as on Windows.
NOT_TRIGGER_VKS = {code for code, physical in keytable.EVDEV_NAMES.items() if physical.startswith(("media_", "volume_", "browser_"))}
UNRECORDABLE_TRIGGER_VKS = {code for code, physical in keytable.EVDEV_NAMES.items()
                            if physical in ("backspace", "tab", "enter", "esc", "space", "print_screen")}


def hook_vk(vk: int, scan: int) -> int:
    """The evdev code of a key a Qt window saw: Qt on X11 gives the X keycode as the native scan
    code, and the native virtual key is a keysym, which names the character rather than the key."""
    return scan - KEYCODE_OFFSET if scan > KEYCODE_OFFSET else 0


def input_title(entry: str) -> str:
    """The name an ignored-inputs entry is shown under."""
    kind, _, value = entry.partition(":")
    if kind == "button":
        return BUTTON_TITLES.get(value, f"Button {value}")
    if kind == "key" and value.isdigit():
        code = int(value)
        if code in VK_TITLES:
            return VK_TITLES[code]
        if code in keytable.EVDEV_US:
            return keytable.EVDEV_US[code].upper()
        return f"Key {code}"
    return entry


def parse_raw(data: bytes) -> Tuple[int, int, int, int, Dict[int, float]]:
    """(deviceid, detail, sourceid, flags, raw valuators) from the bytes of an XI2 raw event after
    its first ten, which is what python-xlib hands over: it registers no parser for raw events.
    The raw values are the device's own, unaccelerated; the accelerated ones before them are
    skipped."""
    deviceid, _time, detail, sourceid, mask_len, flags = struct.unpack_from("=HIIHHI", data, 0)
    offset = 22
    mask = data[offset:offset + mask_len * 4]
    offset += mask_len * 4
    numbers = [bit for bit in range(mask_len * 32) if mask[bit // 8] >> (bit % 8) & 1]
    offset += 8 * len(numbers)
    raw = {}
    for number in numbers:
        integral, fraction = struct.unpack_from("=iI", data, offset)
        offset += 8
        raw[number] = integral + fraction / 2 ** 32
    return deviceid, detail, sourceid, flags, raw


_xkb = []


def _keysym_to_utf32():
    if not _xkb:
        function = None
        name = ctypes.util.find_library("xkbcommon")
        if name:
            try:
                function = ctypes.CDLL(name).xkb_keysym_to_utf32
                function.restype = ctypes.c_uint32
                function.argtypes = [ctypes.c_uint32]
            except (OSError, AttributeError):
                function = None
        _xkb.append(function)
    return _xkb[0]


_KEYPAD_TEXT = {0xFFAA: "*", 0xFFAB: "+", 0xFFAC: ",", 0xFFAD: "-", 0xFFAE: ".", 0xFFAF: "/"}
_KEYPAD_TEXT.update({0xFFB0 + digit: str(digit) for digit in range(10)})


def keysym_text(keysym: int) -> Optional[str]:
    """The character a keysym types, or None. libxkbcommon knows every keysym; without it (the
    tests on the Mac), Latin-1, the keypad and the Unicode keysyms."""
    if not keysym:
        return None
    convert = _keysym_to_utf32()
    if convert is not None:
        point = convert(keysym)
        return chr(point) if point else None
    if 0x20 <= keysym <= 0x7E or 0xA0 <= keysym <= 0xFF:
        return chr(keysym)
    if keysym in _KEYPAD_TEXT:
        return _KEYPAD_TEXT[keysym]
    if keysym & 0xFF000000 == 0x01000000:
        return chr(keysym & 0x00FFFFFF)
    return None


def character(keycode: int, mods: int, group: int, keymap) -> Optional[str]:
    """What a key with no fixed name sends (WIRE.md section 7): the character with Shift, Caps Lock
    and Num Lock applied as the layout applies them, or, while Ctrl, Alt or Super is held, the
    key's own unshifted character, so a chord arrives as a modifier and the plain key. A keypad
    key with Num Lock off is the key it moves by. None for a key that types nothing printable (a
    dead key). AltGr selects nothing here, as on Windows. `keymap` is an xkb_x11.Keymap."""
    if mods & CHORD_MASK:
        use = mods & NUM_LOCK_MASK
    else:
        use = mods & (SHIFT_MASK | LOCK_MASK | NUM_LOCK_MASK)
    keysym = keymap.keysym(keycode, use, group)
    named = KEYPAD_NAMES.get(keysym)
    if named is not None:
        return named
    text = keymap.text(keysym)
    return text if text and text.isprintable() else None


# A button's logical number to the Windows messages the bridge reads. 4 to 7 are the wheel.
_BUTTONS = {
    1: (WM_LBUTTONDOWN, WM_LBUTTONUP, 0),
    2: (WM_MBUTTONDOWN, WM_MBUTTONUP, 0),
    3: (WM_RBUTTONDOWN, WM_RBUTTONUP, 0),
    8: (WM_XBUTTONDOWN, WM_XBUTTONUP, 1 << 16),
    9: (WM_XBUTTONDOWN, WM_XBUTTONUP, 2 << 16),
}
_WHEEL = {4: (WM_MOUSEWHEEL, WHEEL_DELTA), 5: (WM_MOUSEWHEEL, -WHEEL_DELTA),
          6: (WM_MOUSEHWHEEL, -WHEEL_DELTA), 7: (WM_MOUSEHWHEEL, WHEEL_DELTA)}


def _wheel_data(units: int) -> int:
    return (max(-0x8000, min(0x7FFF, units)) & 0xFFFF) << 16


def _at_or_after(sequence: int, serial: int) -> bool:
    """Sequence numbers are 16 bits and wrap: `sequence` is at or after `serial` when it is less
    than half the circle ahead of it."""
    return ((sequence - serial) & 0xFFFF) < 0x8000


class Device:
    def __init__(self, deviceid: int, use: int, name: str, absolute: bool = False, scrolls=None) -> None:
        self.deviceid, self.use, self.name, self.absolute = deviceid, use, name, absolute
        # Valuator number to (vertical, increment) for a smooth-scrolling device.
        self.scrolls: Dict[int, Tuple[bool, float]] = scrolls or {}


class _Connection:
    """The capture thread's own X connection, and an XKB view of the layout beside it: everything
    here speaks to the server, so what the hooks decide can be driven by a fake in tests. Events
    come out as plain tuples."""

    def __init__(self) -> None:
        from Xlib import X, display
        from Xlib.ext import xinput, xtest

        import xkb_x11

        self._X, self._xinput, self._xtest = X, xinput, xtest
        self.display = display.Display()
        try:
            if not self.display.has_extension("XInputExtension"):
                raise RuntimeError("This X server has no XInput extension")
            if not self.display.has_extension("XTEST"):
                raise RuntimeError("This X server has no XTEST extension")
            self._opcode = self.display.display.get_extension_major("XInputExtension")
            # python-xlib's xinput_query_version announces 2.0, and a 2.0 client is sent raw events
            # only while nothing is grabbed.
            reply = xinput.XIQueryVersion(display=self.display.display, opcode=self._opcode, major_version=2, minor_version=2)
            if (reply.major_version, reply.minor_version) < (2, 1):
                raise RuntimeError(f"This X server has XInput {reply.major_version}.{reply.minor_version}; Beamer needs 2.1")
            self.keymap = xkb_x11.Keymap(self.display.get_display_name())
            self.root = self.display.screen().root
            self.root.xinput_select_events([(XI_ALL_MASTER_DEVICES, RAW_MASK), (XI_ALL_DEVICES, 1 << XI_HIERARCHY_CHANGED)])
            # An invisible cursor for the pointer grab: a 1x1 pixmap with an empty mask.
            pixmap = self.root.create_pixmap(1, 1, 1)
            gc = pixmap.create_gc(foreground=0)
            pixmap.fill_rectangle(gc, 0, 0, 1, 1)
            self._cursor = pixmap.create_cursor(pixmap, (0, 0, 0), (0, 0, 0), 0, 0)
            gc.free()
            pixmap.free()
            self.display.sync()
        except Exception:
            self.display.close()
            raise

    def events(self, timeout: float) -> list:
        display = self.display
        if not display.pending_events():
            select.select([display.fileno()], [], [], timeout)
        found = []
        while len(found) < MAX_BATCH and display.pending_events():
            event = display.next_event()
            kind = event.type
            if kind == MAPPING_NOTIFY:
                display.refresh_keyboard_mapping(event)
                if event.request != MAPPING_POINTER:
                    try:
                        self.keymap.reload()
                    except Exception:
                        # A keymap changing as it is read: the old one serves until the next change.
                        LOGGER.exception("The keyboard layout could not be read again; keeping the last one")
                found.append(("mapping", event.request))
            elif kind == GENERIC_EVENT and event.extension == self._opcode:
                found.extend(self._generic(event))
        return found

    def _generic(self, event) -> list:
        kind = event.evtype
        if XI_RAW_KEY_PRESS <= kind <= XI_RAW_MOTION:
            deviceid, detail, sourceid, flags, raw = parse_raw(event.data)
            return [("raw", kind, detail, sourceid, flags, raw, deviceid, event.sequence_number)]
        if kind in (XI_KEY_PRESS, XI_KEY_RELEASE):
            data = event.data
            return [("key", kind == XI_KEY_PRESS, data.detail, data.sourceid, data.flags,
                     data.mods.effective_mods, data.groups.effective_group, data.deviceid, event.sequence_number)]
        if kind == XI_HIERARCHY_CHANGED:
            return [("hierarchy",)]
        return []

    def devices(self) -> List[Device]:
        xinput = self._xinput
        found = []
        for info in self.display.xinput_query_device(XI_ALL_DEVICES).devices:
            name = info.name.decode("utf-8", "replace") if isinstance(info.name, bytes) else str(info.name)
            absolute = False
            scrolls = {}
            for item in info.classes:
                kind = getattr(item, "type", None)
                if kind == xinput.ValuatorClass and item.number in (0, 1) and item.mode == XI_MODE_ABSOLUTE:
                    absolute = True
                elif kind == xinput.ScrollClass and item.increment:
                    scrolls[item.number] = (item.scroll_type == XI_SCROLL_VERTICAL, float(item.increment))
            found.append(Device(info.deviceid, info.use, name, absolute, scrolls))
        return found

    def grab(self, device: Device) -> Optional[int]:
        """The grab request's serial when it was granted, None when another client holds the device."""
        keyboard = device.use == XI_MASTER_KEYBOARD
        request = self._xinput.XIGrabDevice(
            display=self.display.display, opcode=self._opcode, deviceid=device.deviceid, grab_window=self.root,
            time=self._X.CurrentTime, cursor=self._X.NONE if keyboard else self._cursor,
            grab_mode=self._xinput.GrabModeAsync, paired_device_mode=self._xinput.GrabModeAsync, owner_events=False,
            mask=KEYBOARD_GRAB_MASK if keyboard else POINTER_GRAB_MASK,
        )
        return request._serial if request.status == 0 else None

    def ungrab(self, device: Device) -> int:
        request = self.display.xinput_ungrab_device(device.deviceid, self._X.CurrentTime)
        self.display.sync()
        return request._serial

    def fake_key(self, keycode: int, down: bool) -> None:
        self._xtest.fake_input(self.display, self._X.KeyPress if down else self._X.KeyRelease, keycode)
        self.display.sync()

    def fake_button(self, button: int, down: bool) -> None:
        self._xtest.fake_input(self.display, self._X.ButtonPress if down else self._X.ButtonRelease, button)
        self.display.sync()

    def pointer_mapping(self) -> List[int]:
        return list(self.display.get_pointer_mapping())

    def state(self) -> Tuple[int, int]:
        """(modifier bits, layout group) now. The core state carries the group in bits 13 and 14
        for any client, XKB-aware or not."""
        mask = self.root.query_pointer().mask
        return mask & 0xFF, (mask >> 13) & 3

    def abort(self) -> None:
        """From another thread: end the connection under the capture thread, which the server
        answers by releasing every grab it holds."""
        try:
            self.display.display.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self) -> None:
        try:
            self.keymap.close()
        finally:
            self.display.close()


class Hooks:
    """The listener, the grab and the thread that owns both, with capture_win.Hooks' callbacks:
    `on_key(name, down, vk, us)`, `on_mouse(message, x, y, mouse_data)` and `on_motion(dx, dy)`.
    `vk` is the evdev code. An answer of False to a key or button from inside a grab means it was
    this machine's, and it is played here."""

    def __init__(self, on_key, on_mouse, on_motion, connect: Optional[Callable[[], object]] = None) -> None:
        self._on_key = on_key
        self._on_mouse = on_mouse
        self._on_motion = on_motion
        self._connect = connect or _Connection
        self._connection = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._failure: Optional[BaseException] = None
        self._predicate: Optional[Callable[[], bool]] = None
        # The proof harness turns this off to read its own injection back.
        self.drop_injected = True
        self._masters: List[Device] = []
        self._xtest: set = set()
        self._absolute: set = set()
        self._scrolls: Dict[int, Dict[int, Tuple[bool, float]]] = {}
        self._buttons: List[int] = []
        self._grabbed: List[Device] = []
        self._taking = False
        # Per master device, the (first, after-last) request serials of its recent grabs; the
        # after-last is None while it is held.
        self._spans: Dict[int, collections.deque] = {}
        self._names_down: Dict[int, str] = {}
        # Keys and buttons this machine was given through XTest after a grab kept them from it,
        # held until their own release arrives, whichever way it comes.
        self._played_keys: set = set()
        self._played_buttons: set = set()
        # Called on the capture thread with the exception when capture stops on its own after it
        # started (the X server gone, the watchdog), so input can come home.
        self.on_failure: Optional[Callable[[BaseException], None]] = None
        self._refused_said = False
        self._carry = [0.0, 0.0]
        self._scroll_carry: Dict[Tuple[int, int], float] = {}
        self._beat = time.monotonic()

    def grab_while(self, predicate: Callable[[], bool]) -> None:
        """Hold every master device for as long as `predicate` is true: asked after every batch of
        events and at least every TICK_SECONDS. It runs on the capture thread, so it must be cheap
        and never block."""
        self._predicate = predicate

    @property
    def grabbed(self) -> bool:
        return bool(self._grabbed)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            if self._stopping.is_set():
                # A stop that timed out: a second thread would share this one's bookkeeping while
                # its connection may still hold the grab.
                raise RuntimeError("The last capture thread has not stopped yet")
            return
        self._ready.clear()
        self._stopping.clear()
        self._failure = None
        self._thread = threading.Thread(target=self._run, name="Beamer-hooks", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("The input hooks did not start within five seconds")
        if self._failure is not None:
            raise self._failure

    def stop(self) -> None:
        self._stopping.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=2.0)
        if thread.is_alive():
            LOGGER.error("The capture thread did not stop within two seconds; closing its connection")
            connection = self._connection
            if connection is not None:
                connection.abort()
            thread.join(timeout=1.0)
        if not thread.is_alive():
            self._thread = None

    def _run(self) -> None:
        connection = None
        try:
            connection = self._connection = self._connect()
            self._read_devices()
            self._buttons = connection.pointer_mapping()
            self._beat = time.monotonic()
            threading.Thread(target=self._watch, args=(connection,), name="Beamer-hooks-watch", daemon=True).start()
            self._ready.set()
            while not self._stopping.is_set():
                self._beat = time.monotonic()
                for event in connection.events(TICK_SECONDS):
                    self._beat = time.monotonic()
                    try:
                        self._handle(event)
                    except Exception:
                        LOGGER.exception("An input event could not be read; carrying on")
                self._beat = time.monotonic()
                self._tick()
        except Exception as exc:
            LOGGER.exception("Keyboard and mouse capture stopped")
            started = self._ready.is_set()
            self._failure = exc
            self._ready.set()
            if started and not self._stopping.is_set() and self.on_failure is not None:
                try:
                    self.on_failure(exc)
                except Exception:
                    LOGGER.exception("Could not report that capture stopped")
        finally:
            self._let_go()
            self._release_played()
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    LOGGER.exception("The capture connection did not close cleanly")
            self._connection = None
            self._stopping.set()

    def _watch(self, connection) -> None:
        """The safety net under a capture thread stuck in a callback or a round trip while it
        holds the grab: nothing else can let go of it, so its connection is ended."""
        while not self._stopping.wait(STALL_SECONDS / 8):
            holding = self._grabbed or self._taking
            if holding and time.monotonic() - self._beat > STALL_SECONDS:
                LOGGER.error("The capture thread has not answered for %s s while holding the keyboard and mouse; "
                             "closing its connection to free them", STALL_SECONDS)
                connection.abort()
                return

    # The grab

    def _tick(self) -> None:
        want = False
        predicate = self._predicate
        if predicate is not None:
            try:
                want = bool(predicate())
            except Exception:
                LOGGER.exception("Could not tell whether input is away; letting go")
        if want and not self._grabbed:
            self._take()
        elif not want and self._grabbed:
            self._let_go()

    def _take(self) -> None:
        self._taking = True
        try:
            for device in self._masters:
                if not self._grab_one(device):
                    # All or nothing: half a grab sends keys away while the pointer works here.
                    # Another client's grab (an open menu) refuses ours; it is asked again next tick.
                    if not self._refused_said:
                        self._refused_said = True
                        LOGGER.warning("Another app holds %s; trying again until it lets go", device.name)
                    self._let_go()
                    return
            self._refused_said = False
        finally:
            self._taking = False

    def _grab_one(self, device: Device) -> bool:
        self._beat = time.monotonic()
        try:
            serial = self._connection.grab(device)
        except Exception:
            LOGGER.exception("Grabbing %s failed", device.name)
            return False
        if serial is None:
            return False
        spans = self._spans.setdefault(device.deviceid, collections.deque(maxlen=8))
        spans.append([serial, None, None])
        self._grabbed.append(device)
        return True

    def _let_go_of(self, device: Device) -> None:
        self._beat = time.monotonic()
        try:
            serial = self._connection.ungrab(device)
        except Exception:
            LOGGER.exception("Letting go of %s failed", device.name)
            serial = None
        # Only now: the watchdog watches for as long as anything is held, the ungrab's round trip too.
        if device in self._grabbed:
            self._grabbed.remove(device)
        spans = self._spans.get(device.deviceid)
        if spans and spans[-1][1] is None:
            spans[-1][1] = serial
            spans[-1][2] = time.monotonic()

    def _let_go(self) -> None:
        for device in list(self._grabbed):
            self._let_go_of(device)

    def _inside(self, master: int, sequence: int) -> bool:
        """Whether an event from `master` stamped `sequence` was made while this client held it."""
        spans = self._spans.get(master)
        if not spans:
            return False
        now = time.monotonic()
        while spans and spans[0][2] is not None and now - spans[0][2] > SPAN_KEEP_SECONDS:
            spans.popleft()
        for first, after, _closed in spans:
            if _at_or_after(sequence, first) and (after is None or not _at_or_after(sequence, after)):
                return True
        return False

    def _play_key(self, keycode: int, down: bool) -> None:
        if down:
            self._played_keys.add(keycode)
        else:
            self._played_keys.discard(keycode)
        self._replay(XI_MASTER_KEYBOARD, lambda connection: connection.fake_key(keycode, down))

    def _play_button(self, button: int, down: bool) -> None:
        if down:
            self._played_buttons.add(button)
        else:
            self._played_buttons.discard(button)
        self._replay(XI_MASTER_POINTER, lambda connection: connection.fake_button(button, down))

    def _release_played(self) -> None:
        """XTest holds a played key until told otherwise, and the server would repeat it for ever."""
        for keycode in sorted(self._played_keys):
            self._play_key(keycode, False)
        for button in sorted(self._played_buttons):
            self._play_button(button, False)

    def _replay(self, device_use: int, play: Callable[[object], None]) -> None:
        """Play one event here that a grab kept from every app: the grab is let go around it, so
        XTest's copy reaches the focused window, then taken again, all of it or none."""
        held = next((device for device in self._grabbed if device.use == device_use), None)
        if held is not None:
            self._let_go_of(held)
        try:
            play(self._connection)
        except Exception:
            LOGGER.exception("Could not play an input on this machine")
        finally:
            if held is not None and not self._grab_one(held):
                self._let_go()

    # Events

    def _read_devices(self) -> None:
        devices = self._connection.devices()
        before = {device.deviceid for device in self._masters}
        self._masters = [device for device in devices if device.use in (XI_MASTER_POINTER, XI_MASTER_KEYBOARD)]
        self._xtest = {device.deviceid for device in devices if "XTEST" in device.name}
        self._absolute = {device.deviceid for device in devices if device.use == XI_SLAVE_POINTER and device.absolute}
        self._scrolls = {device.deviceid: device.scrolls for device in devices if device.scrolls}
        if self._grabbed and {device.deviceid for device in self._masters} != before:
            # A master added or gone: what is held is no longer every device; the next tick takes them all.
            self._let_go()

    def _handle(self, event: tuple) -> None:
        kind = event[0]
        if kind == "raw":
            _, evtype, detail, sourceid, flags, raw, master, sequence = event
            if self.drop_injected and sourceid in self._xtest:
                return
            if evtype in (XI_RAW_KEY_PRESS, XI_RAW_KEY_RELEASE):
                if not self._inside(master, sequence):  # inside a grab, its key event says it
                    self._key(detail, evtype == XI_RAW_KEY_PRESS, None, None, False)
            elif evtype in (XI_RAW_BUTTON_PRESS, XI_RAW_BUTTON_RELEASE):
                self._button(detail, evtype == XI_RAW_BUTTON_PRESS, flags, self._inside(master, sequence))
            elif evtype == XI_RAW_MOTION:
                self._motion(sourceid, raw)
        elif kind == "key":
            _, down, detail, sourceid, _flags, mods, group, _master, _sequence = event
            if self.drop_injected and sourceid in self._xtest:
                return
            self._key(detail, down, mods, group, True)
        elif kind == "hierarchy":
            self._read_devices()
        elif kind == "mapping" and event[1] == MAPPING_POINTER:
            self._buttons = self._connection.pointer_mapping()

    def _key(self, keycode: int, down: bool, mods: Optional[int], group: Optional[int], swallowed: bool) -> None:
        code = keycode - KEYCODE_OFFSET
        name = self._names_down.get(code) if down else self._names_down.pop(code, None)
        if name is None:
            name = VK_TO_NAME.get(code)
        if name is None:
            if mods is None:
                # Outside a grab an event carries no state: one round trip, for character keys only.
                mods, group = self._connection.state()
            name = character(keycode, mods, group, self._connection.keymap)
            if name is None:
                return
        if down:
            self._names_down[code] = name
        answer = self._on_key(name, down, code, keytable.EVDEV_US.get(code))
        if not down and keycode in self._played_keys:
            self._play_key(keycode, False)  # its press was played here, whatever is answered now
        elif answer is False and swallowed:
            self._play_key(keycode, down)

    def _button(self, physical: int, down: bool, flags: int, swallowed: bool) -> None:
        buttons = self._buttons
        logical = buttons[physical - 1] if 0 < physical <= len(buttons) else physical
        wheel = _WHEEL.get(logical)
        if wheel is not None:
            if not down or flags & XI_POINTER_EMULATED:
                return  # a smooth-scrolling device's wheel is its scroll valuators
            message, delta = wheel
            answer = self._on_mouse(message, 0, 0, _wheel_data(delta))
            if answer is False and swallowed:
                self._play_button(logical, True)
                self._play_button(logical, False)
            return
        fixed = _BUTTONS.get(logical)
        if fixed is not None:
            press, release, data = fixed
            answer = self._on_mouse(press if down else release, 0, 0, data)
            if not down and logical in self._played_buttons:
                self._play_button(logical, False)
            elif answer is False and swallowed:
                self._play_button(logical, down)

    def _motion(self, sourceid: int, raw: Dict[int, float]) -> None:
        scrolls = self._scrolls.get(sourceid)
        if scrolls:
            for number, value in raw.items():
                axis = scrolls.get(number)
                if axis is None:
                    continue
                vertical, increment = axis
                key = (sourceid, number)
                # One increment is one notch, positive down and right; Windows' wheel is positive
                # up, its horizontal wheel positive right.
                carry = self._scroll_carry.get(key, 0.0) + (-value if vertical else value) / increment * WHEEL_DELTA
                whole = int(carry)
                self._scroll_carry[key] = carry - whole
                if whole:
                    self._on_mouse(WM_MOUSEWHEEL if vertical else WM_MOUSEHWHEEL, 0, 0, _wheel_data(whole))
        if 0 not in raw and 1 not in raw:
            return
        self._on_mouse(WM_MOUSEMOVE, 0, 0, 0)
        if sourceid in self._absolute:
            return
        carry = self._carry
        carry[0] += raw.get(0, 0.0)
        carry[1] += raw.get(1, 0.0)
        dx, dy = int(carry[0]), int(carry[1])
        carry[0] -= dx
        carry[1] -= dy
        if dx or dy:
            self._on_motion(dx, dy)
