"""Beamer's own small ctypes layer over libei, the input emulation library the Wayland portals hand
input over: a receiver for InputCapture (this machine's own keys and pointer while they are away)
and a sender for RemoteDesktop (a peer's input played here). Only the calls Beamer makes are bound;
Peter Hutterer's snegg is the reference for the shapes, not a dependency (its API is not stable and
it has no release).

`Context.events()` turns libei's events into plain tuples, so the capture and injection modules
read them without ctypes and their tests feed tuples of their own:

    ("connect",) ("disconnect",)
    ("seat",)                              a seat, already bound to every capability it has
    ("device_added", device) ("device_removed", device)
    ("device_resumed", device) ("device_paused", device)
    ("start", device, sequence) ("stop", device)
    ("frame", device, time_us)
    ("motion", device, dx, dy)
    ("absolute", device, x, y)
    ("button", device, code, down)         an evdev button code (BTN_LEFT is 0x110)
    ("scroll", device, dx, dy)             in logical pixels
    ("scroll_discrete", device, dx, dy)    in 120ths of a notch, as libei counts them
    ("scroll_stop", device)
    ("key", device, code, down)            an evdev key code
    ("modifiers", device, depressed, latched, locked, group)

A device is a `Device`, which holds its own reference and says its name, capabilities, regions and
keymap; a removed one lets go of its reference at the next events(), once its removal has been
read. A seat is bound here, while its event still holds it, so no seat pointer leaves this module.
libei is reached only when a Context is made, so this module imports anywhere."""

import ctypes
import ctypes.util
import logging
import os
from typing import List, Optional, Tuple

LOGGER = logging.getLogger(__name__)

CAP_POINTER = 1 << 0
CAP_POINTER_ABSOLUTE = 1 << 1
CAP_KEYBOARD = 1 << 2
CAP_TOUCH = 1 << 3
CAP_SCROLL = 1 << 4
CAP_BUTTON = 1 << 5
CAPS = (CAP_POINTER, CAP_POINTER_ABSOLUTE, CAP_KEYBOARD, CAP_TOUCH, CAP_SCROLL, CAP_BUTTON)

KEYMAP_XKB = 1

EV_CONNECT = 1
EV_DISCONNECT = 2
EV_SEAT_ADDED = 3
EV_SEAT_REMOVED = 4
EV_DEVICE_ADDED = 5
EV_DEVICE_REMOVED = 6
EV_DEVICE_PAUSED = 7
EV_DEVICE_RESUMED = 8
EV_KEYBOARD_MODIFIERS = 9
EV_FRAME = 100
EV_START_EMULATING = 200
EV_STOP_EMULATING = 201
EV_POINTER_MOTION = 300
EV_POINTER_MOTION_ABSOLUTE = 400
EV_BUTTON = 500
EV_SCROLL_DELTA = 600
EV_SCROLL_STOP = 601
EV_SCROLL_CANCEL = 602
EV_SCROLL_DISCRETE = 603
EV_KEYBOARD_KEY = 700

_lib = None


def _load():
    global _lib
    if _lib is not None:
        return _lib
    path = ctypes.util.find_library("ei") or "libei.so.1"
    try:
        lib = ctypes.CDLL(path)
    except OSError as exc:
        raise RuntimeError("libei is not installed; Beamer needs it to capture and play input on Wayland") from exc
    p, u32, i32, dbl, b = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32, ctypes.c_double, ctypes.c_bool
    signatures = {
        "ei_new_sender": (p, [p]),
        "ei_new_receiver": (p, [p]),
        "ei_unref": (p, [p]),
        "ei_configure_name": (None, [p, ctypes.c_char_p]),
        "ei_setup_backend_fd": (ctypes.c_int, [p, ctypes.c_int]),
        "ei_get_fd": (ctypes.c_int, [p]),
        "ei_dispatch": (None, [p]),
        "ei_get_event": (p, [p]),
        "ei_now": (ctypes.c_uint64, [p]),
        "ei_disconnect": (None, [p]),
        "ei_event_unref": (p, [p]),
        "ei_event_get_type": (ctypes.c_int, [p]),
        "ei_event_get_device": (p, [p]),
        "ei_event_get_seat": (p, [p]),
        "ei_event_get_time": (ctypes.c_uint64, [p]),
        "ei_seat_ref": (p, [p]),
        "ei_seat_unref": (p, [p]),
        "ei_seat_has_capability": (b, [p, ctypes.c_int]),
        "ei_device_ref": (p, [p]),
        "ei_device_unref": (p, [p]),
        "ei_device_get_name": (ctypes.c_char_p, [p]),
        "ei_device_has_capability": (b, [p, ctypes.c_int]),
        "ei_device_get_region": (p, [p, ctypes.c_size_t]),
        "ei_device_keyboard_get_keymap": (p, [p]),
        "ei_device_start_emulating": (None, [p, u32]),
        "ei_device_stop_emulating": (None, [p]),
        "ei_device_frame": (None, [p, ctypes.c_uint64]),
        "ei_device_pointer_motion": (None, [p, dbl, dbl]),
        "ei_device_pointer_motion_absolute": (None, [p, dbl, dbl]),
        "ei_device_button_button": (None, [p, u32, b]),
        "ei_device_scroll_delta": (None, [p, dbl, dbl]),
        "ei_device_scroll_discrete": (None, [p, i32, i32]),
        "ei_device_scroll_stop": (None, [p, b, b]),
        "ei_device_keyboard_key": (None, [p, u32, b]),
        "ei_region_get_x": (u32, [p]),
        "ei_region_get_y": (u32, [p]),
        "ei_region_get_width": (u32, [p]),
        "ei_region_get_height": (u32, [p]),
        "ei_region_get_physical_scale": (dbl, [p]),
        "ei_keymap_get_size": (ctypes.c_size_t, [p]),
        "ei_keymap_get_type": (ctypes.c_int, [p]),
        "ei_keymap_get_fd": (ctypes.c_int, [p]),
        "ei_event_emulating_get_sequence": (u32, [p]),
        "ei_event_keyboard_get_xkb_mods_depressed": (u32, [p]),
        "ei_event_keyboard_get_xkb_mods_latched": (u32, [p]),
        "ei_event_keyboard_get_xkb_mods_locked": (u32, [p]),
        "ei_event_keyboard_get_xkb_group": (u32, [p]),
        "ei_event_pointer_get_dx": (dbl, [p]),
        "ei_event_pointer_get_dy": (dbl, [p]),
        "ei_event_pointer_get_absolute_x": (dbl, [p]),
        "ei_event_pointer_get_absolute_y": (dbl, [p]),
        "ei_event_button_get_button": (u32, [p]),
        "ei_event_button_get_is_press": (b, [p]),
        "ei_event_scroll_get_dx": (dbl, [p]),
        "ei_event_scroll_get_dy": (dbl, [p]),
        "ei_event_scroll_get_discrete_dx": (i32, [p]),
        "ei_event_scroll_get_discrete_dy": (i32, [p]),
        "ei_event_keyboard_get_key": (u32, [p]),
        "ei_event_keyboard_get_key_is_press": (b, [p]),
    }
    for name, (restype, argtypes) in signatures.items():
        function = getattr(lib, name)
        function.restype = restype
        function.argtypes = argtypes
    # Variadic, ended by a zero: its arguments are given types at the call, never in argtypes.
    lib.ei_seat_bind_capabilities.restype = None
    _lib = lib
    return lib


class Device:
    """A libei device, holding a reference of its own until close()."""

    def __init__(self, lib, pointer: int) -> None:
        self._lib = lib
        self.pointer = lib.ei_device_ref(pointer)
        raw = lib.ei_device_get_name(self.pointer)
        self.name = raw.decode("utf-8", "replace") if raw else ""
        self.caps = {cap for cap in CAPS if lib.ei_device_has_capability(self.pointer, cap)}

    def has(self, cap: int) -> bool:
        return cap in self.caps

    def regions(self) -> List[Tuple[int, int, int, int, float]]:
        """(x, y, width, height, scale) for each region of a virtual device, in the compositor's
        logical pixels: where an absolute position lands."""
        found = []
        index = 0
        while True:
            region = self._lib.ei_device_get_region(self.pointer, index)
            if not region:
                return found
            found.append((self._lib.ei_region_get_x(region), self._lib.ei_region_get_y(region),
                          self._lib.ei_region_get_width(region), self._lib.ei_region_get_height(region),
                          self._lib.ei_region_get_physical_scale(region)))
            index += 1

    def keymap(self) -> Optional[str]:
        """The XKB keymap the compositor gave this keyboard, as text, or None. The fd is libei's and
        stays open; it is read with pread so its offset is never moved."""
        keymap = self._lib.ei_device_keyboard_get_keymap(self.pointer)
        if not keymap or self._lib.ei_keymap_get_type(keymap) != KEYMAP_XKB:
            return None
        size = self._lib.ei_keymap_get_size(keymap)
        fd = self._lib.ei_keymap_get_fd(keymap)
        if fd < 0 or size <= 0:
            return None
        data = os.pread(fd, size, 0)
        return data.rstrip(b"\0").decode("utf-8", "replace")

    def close(self) -> None:
        if self.pointer:
            self._lib.ei_device_unref(self.pointer)
            self.pointer = None


class Context:
    """One libei connection over an fd a portal's ConnectToEIS gave. `sender` False for capture.
    Not thread-safe: one thread dispatches and sends."""

    def __init__(self, fd: int, sender: bool, name: str = "Beamer") -> None:
        lib = self._lib = _load()
        self.sender = sender
        self._ei = lib.ei_new_sender(None) if sender else lib.ei_new_receiver(None)
        if not self._ei:
            raise RuntimeError("libei could not make a context")
        lib.ei_configure_name(self._ei, name.encode())
        result = lib.ei_setup_backend_fd(self._ei, fd)
        if result != 0:
            lib.ei_unref(self._ei)
            self._ei = None
            raise OSError(-result, f"libei refused the portal's connection: {os.strerror(-result)}")
        self._devices = {}
        self._removed = []

    def fileno(self) -> int:
        return self._lib.ei_get_fd(self._ei)

    def now(self) -> int:
        return self._lib.ei_now(self._ei)

    def _bind(self, seat: int) -> None:
        """Ask the seat for every capability it has: a receiver gets whatever the compositor
        captures, a sender every device it may emulate."""
        lib = self._lib
        caps = [ctypes.c_int(cap) for cap in CAPS if lib.ei_seat_has_capability(seat, cap)]
        lib.ei_seat_bind_capabilities(ctypes.c_void_p(seat), *caps, ctypes.c_void_p(None))

    def _device(self, pointer: int) -> Device:
        device = self._devices.get(pointer)
        if device is None:
            device = self._devices[pointer] = Device(self._lib, pointer)
        return device

    def events(self) -> list:
        """Dispatch what the fd holds and return every event as a tuple (see the module's)."""
        lib = self._lib
        for device in self._removed:
            device.close()
        self._removed = []
        lib.ei_dispatch(self._ei)
        found = []
        while True:
            event = lib.ei_get_event(self._ei)
            if not event:
                return found
            try:
                item = self._read(event)
            finally:
                lib.ei_event_unref(event)
            if item is not None:
                found.append(item)

    def _read(self, event) -> Optional[tuple]:
        lib = self._lib
        kind = lib.ei_event_get_type(event)
        if kind == EV_CONNECT:
            return ("connect",)
        if kind == EV_DISCONNECT:
            return ("disconnect",)
        if kind == EV_SEAT_ADDED:
            self._bind(lib.ei_event_get_seat(event))
            return ("seat",)
        if kind == EV_SEAT_REMOVED:
            return None
        pointer = lib.ei_event_get_device(event)
        if not pointer:
            return None
        device = self._device(pointer)
        if kind == EV_DEVICE_ADDED:
            return ("device_added", device)
        if kind == EV_DEVICE_REMOVED:
            self._removed.append(self._devices.pop(pointer, device))
            return ("device_removed", device)
        if kind == EV_DEVICE_RESUMED:
            return ("device_resumed", device)
        if kind == EV_DEVICE_PAUSED:
            return ("device_paused", device)
        if kind == EV_START_EMULATING:
            return ("start", device, lib.ei_event_emulating_get_sequence(event))
        if kind == EV_STOP_EMULATING:
            return ("stop", device)
        if kind == EV_FRAME:
            return ("frame", device, lib.ei_event_get_time(event))
        if kind == EV_POINTER_MOTION:
            return ("motion", device, lib.ei_event_pointer_get_dx(event), lib.ei_event_pointer_get_dy(event))
        if kind == EV_POINTER_MOTION_ABSOLUTE:
            return ("absolute", device, lib.ei_event_pointer_get_absolute_x(event), lib.ei_event_pointer_get_absolute_y(event))
        if kind == EV_BUTTON:
            return ("button", device, lib.ei_event_button_get_button(event), bool(lib.ei_event_button_get_is_press(event)))
        if kind == EV_SCROLL_DELTA:
            return ("scroll", device, lib.ei_event_scroll_get_dx(event), lib.ei_event_scroll_get_dy(event))
        if kind == EV_SCROLL_DISCRETE:
            return ("scroll_discrete", device, lib.ei_event_scroll_get_discrete_dx(event), lib.ei_event_scroll_get_discrete_dy(event))
        if kind in (EV_SCROLL_STOP, EV_SCROLL_CANCEL):
            return ("scroll_stop", device)
        if kind == EV_KEYBOARD_KEY:
            return ("key", device, lib.ei_event_keyboard_get_key(event), bool(lib.ei_event_keyboard_get_key_is_press(event)))
        if kind == EV_KEYBOARD_MODIFIERS:
            return ("modifiers", device, lib.ei_event_keyboard_get_xkb_mods_depressed(event),
                    lib.ei_event_keyboard_get_xkb_mods_latched(event), lib.ei_event_keyboard_get_xkb_mods_locked(event),
                    lib.ei_event_keyboard_get_xkb_group(event))
        return None

    # Sending. A device the desktop removed has let go of its reference; sending on it does nothing.

    def start(self, device: Device, sequence: int) -> None:
        if not device.pointer:
            return
        self._lib.ei_device_start_emulating(device.pointer, sequence)

    def stop(self, device: Device) -> None:
        if not device.pointer:
            return
        self._lib.ei_device_stop_emulating(device.pointer)

    def frame(self, device: Device) -> None:
        if not device.pointer:
            return
        self._lib.ei_device_frame(device.pointer, self.now())

    def motion(self, device: Device, dx: float, dy: float) -> None:
        if not device.pointer:
            return
        self._lib.ei_device_pointer_motion(device.pointer, dx, dy)

    def absolute(self, device: Device, x: float, y: float) -> None:
        if not device.pointer:
            return
        self._lib.ei_device_pointer_motion_absolute(device.pointer, x, y)

    def button(self, device: Device, code: int, down: bool) -> None:
        if not device.pointer:
            return
        self._lib.ei_device_button_button(device.pointer, code, down)

    def scroll(self, device: Device, dx: float, dy: float) -> None:
        if not device.pointer:
            return
        self._lib.ei_device_scroll_delta(device.pointer, dx, dy)

    def scroll_discrete(self, device: Device, dx: int, dy: int) -> None:
        if not device.pointer:
            return
        self._lib.ei_device_scroll_discrete(device.pointer, dx, dy)

    def scroll_stop(self, device: Device) -> None:
        if not device.pointer:
            return
        self._lib.ei_device_scroll_stop(device.pointer, True, True)

    def key(self, device: Device, code: int, down: bool) -> None:
        if not device.pointer:
            return
        self._lib.ei_device_keyboard_key(device.pointer, code, down)

    def close(self) -> None:
        if self._ei:
            for device in list(self._devices.values()) + self._removed:
                device.close()
            self._devices.clear()
            self._removed = []
            self._lib.ei_disconnect(self._ei)
            self._lib.ei_unref(self._ei)
            self._ei = None
