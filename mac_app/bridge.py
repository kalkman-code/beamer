import collections
import copy
import errno
import logging
import platform
import queue
import socket
import threading
import time
import types
import dataclasses
from dataclasses import dataclass

import objc
import Quartz

import clipboard_mac
import config as config_module
import crossing
import desktop_mac
from input_injector_mac import INJECTED_MARK
import gestures
from core import ignored
import keyboard_layout
import media_keys
from core import keytable
from core import link as link_module
from core import owner as owner_module
from core import peerlist
from core import protocol
from core import receiver
from core import ways
from pointer_hide import PointerHider
from key_codes import (
    KEY_NAME_TO_CODE,
    MODIFIER_KEY_CODES,
    PRINTABLE_KEY_FALLBACKS,
    SPECIAL_KEY_NAMES,
)

# AppKit is only needed to read gesture-event fields (see
# _default_gesture_event_converter below); guarded the same way
# clipboard_mac guards it, so this module stays importable -- and its
# gesture behaviour fakeable in tests -- on a machine without it.
try:
    import AppKit
except ImportError:  # pragma: no cover - exercised only off macOS
    AppKit = None


CONNECT_TIMEOUT_SECONDS = link_module.CONNECT_SECONDS
SOCKET_IO_TIMEOUT_SECONDS = 0.5
OUTBOUND_QUEUE_SIZE = 2048
SSH_FALLBACK_HOST = "127.0.0.1"
# Below 49152 for the same reason the listening ports are: macOS hands out
# 49152-65535 itself, and a forward whose local port is taken never comes up.
SSH_FALLBACK_PORT = 24822
DESKTOP_BOUNDS_MAX_AGE_SECONDS = 1.0
CROSSING_RETRY_SECONDS = 30.0
ROUND_TRIP_SAMPLES = 8
ROUND_TRIP_MAX_AGE_SECONDS = 5.0
# A driven Mac that is asked to drive waits this long for its owner to let go (the responder ends
# the ownership by force after one second), then goes ahead.
LET_GO_WAIT_SECONDS = 1.3
OWN_PLATFORM = "macos"
CAPABILITIES = ("clipboard", "clipboard_image", "gestures", "media_keys", "text", "settings")
MEDIA_KEY_NAMES = frozenset({"volume_mute", "volume_down", "volume_up", "media_next", "media_prev", "media_stop", "media_play_pause"})

# Why input came home, in words, for the alert. A why that is not here comes home quietly.
WHY_TEXT = {
    "owned": "{name} is being driven from another machine",
    "not_allowed": "{name} does not accept input from this Mac",
    "busy": "{name} is driving another machine",
    "malformed": "{name} could not read the request to take it",
    "refused": "{name} refused the input",
    "no_answer": "{name} did not answer",
    "link_lost": "Lost the link to {name}",
}


# NX device-specific modifier bits (IOLLEvent.h), keyed by keycode. Unlike the
# generic kCGEventFlagMask* bits, these distinguish left/right and are set
# directly from the current flags — deriving is_down from them makes
# modifier tracking stateless (no dependency on a remembered down/up toggle
# that a missed event could desync).
NX_DEVICE_MODIFIER_BITS = {
    0x36: 0x0010,
    0x37: 0x0008,
    0x38: 0x0002,
    0x3A: 0x0020,
    0x3B: 0x0001,
    0x3C: 0x0004,
    0x3D: 0x0040,
    0x3E: 0x2000,
}
CAPS_LOCK_KEY_CODE = 0x39
# Command, Option and Control, left and right. Shift and caps lock are
# excluded on purpose -- see _unicode_character.
CHORD_MODIFIER_KEY_CODES = frozenset({0x36, 0x37, 0x3A, 0x3B, 0x3D, 0x3E})

# CoreGraphics numbers the other buttons from 2; 3 and 4 are a mouse's back and forward side
# buttons, which Windows calls XBUTTON1 and XBUTTON2.
WIRE_OTHER_BUTTONS = {2: "middle", 3: "back", 4: "forward"}

_ALL_NX_DEVICE_MODIFIER_BITS = 0
for _bit in NX_DEVICE_MODIFIER_BITS.values():
    _ALL_NX_DEVICE_MODIFIER_BITS |= _bit


@dataclass
class KeyResult:
    is_trigger: bool
    is_down: bool
    is_repeat: bool
    messages: list


class QuartzEventTranslator:
    def __init__(self, quartz=Quartz):
        self.quartz = quartz
        self.modifier_down = set()
        self.printable_down = {}
        # Sub-pixel remainder carried across mousemove events: slow, precise
        # trackpad motion reports fractional per-event deltas that truncate
        # to zero if read as integers and dropped outright. Accumulating as
        # floats and emitting only the whole-number part (toward zero) keeps
        # that motion instead of losing it.
        self._move_accum_x = 0.0
        self._move_accum_y = 0.0
        # Shared with KVMController: which raw event types are mouse
        # move/drag (as opposed to buttons or scroll) -- both mouse_messages
        # below and the cursor-pin warp in _event_tap_callback need exactly
        # this set.
        self.movement_event_types = {
            quartz.kCGEventMouseMoved,
            quartz.kCGEventLeftMouseDragged,
            quartz.kCGEventRightMouseDragged,
            quartz.kCGEventOtherMouseDragged,
        }
        self.modifier_flags = {
            0x36: quartz.kCGEventFlagMaskCommand,
            0x37: quartz.kCGEventFlagMaskCommand,
            0x38: quartz.kCGEventFlagMaskShift,
            0x39: quartz.kCGEventFlagMaskAlphaShift,
            0x3A: quartz.kCGEventFlagMaskAlternate,
            0x3B: quartz.kCGEventFlagMaskControl,
            0x3C: quartz.kCGEventFlagMaskShift,
            0x3D: quartz.kCGEventFlagMaskAlternate,
            0x3E: quartz.kCGEventFlagMaskControl,
        }

    def key_result(self, event_type, event, trigger_code, key_map):
        quartz = self.quartz
        keycode = int(quartz.CGEventGetIntegerValueField(event, quartz.kCGKeyboardEventKeycode))
        is_trigger = keycode == trigger_code
        if event_type == quartz.kCGEventFlagsChanged:
            if keycode not in MODIFIER_KEY_CODES:
                return KeyResult(is_trigger, False, False, [])
            if keycode == 0x39:
                messages = [] if is_trigger else self._caps_lock_messages(key_map)
                return KeyResult(is_trigger, True, False, messages)
            is_down = self._modifier_transition(keycode, event)
            messages = [] if is_trigger else self._special_key_messages(keycode, is_down, key_map)
            return KeyResult(is_trigger, is_down, False, messages)
        if event_type not in (quartz.kCGEventKeyDown, quartz.kCGEventKeyUp):
            return KeyResult(is_trigger, False, False, [])
        is_down = event_type == quartz.kCGEventKeyDown
        is_repeat = bool(
            quartz.CGEventGetIntegerValueField(event, quartz.kCGKeyboardEventAutorepeat)
        )
        if is_trigger:
            return KeyResult(True, is_down, is_repeat, [])
        special_messages = self._special_key_messages(keycode, is_down, key_map)
        if special_messages:
            return KeyResult(False, is_down, is_repeat, special_messages)
        if is_down:
            # A repeat sends what the first press sent, so switching layout mid-hold cannot start
            # a second key on Windows that the release, which sends the saved one, never lets go.
            character = self.printable_down.get(keycode) if is_repeat else None
            if character is None:
                character = self._unicode_character(event, keycode)
                if character is not None and not is_repeat:
                    self.printable_down[keycode] = character
        else:
            character = self.printable_down.pop(keycode, None)
            if character is None:
                character = self._unicode_character(event, keycode)
        if character is None:
            return KeyResult(False, is_down, is_repeat, [])
        message_type = protocol.MSG_KEYDOWN if is_down else protocol.MSG_KEYUP
        data = {"key": character}
        # Where the key sits on a US keyboard, for a PC whose layout cannot type the character.
        if keycode in PRINTABLE_KEY_FALLBACKS:
            data["us"] = PRINTABLE_KEY_FALLBACKS[keycode]
        return KeyResult(False, is_down, is_repeat, [{"type": message_type, "data": data}])

    def mouse_messages(self, event_type, event):
        quartz = self.quartz
        if event_type in self.movement_event_types:
            dx = quartz.CGEventGetDoubleValueField(event, quartz.kCGMouseEventDeltaX)
            dy = quartz.CGEventGetDoubleValueField(event, quartz.kCGMouseEventDeltaY)
            self._move_accum_x += dx
            self._move_accum_y += dy
            int_x = int(self._move_accum_x)  # truncates toward zero
            int_y = int(self._move_accum_y)
            self._move_accum_x -= int_x
            self._move_accum_y -= int_y
            if int_x == 0 and int_y == 0:
                return []
            return [{"type": protocol.MSG_MOUSEMOVE, "data": {"dx": int_x, "dy": int_y}}]
        button_events = {
            quartz.kCGEventLeftMouseDown: (protocol.MSG_MOUSEDOWN, "left"),
            quartz.kCGEventLeftMouseUp: (protocol.MSG_MOUSEUP, "left"),
            quartz.kCGEventRightMouseDown: (protocol.MSG_MOUSEDOWN, "right"),
            quartz.kCGEventRightMouseUp: (protocol.MSG_MOUSEUP, "right"),
        }
        if event_type in button_events:
            message_type, button = button_events[event_type]
            return [{"type": message_type, "data": {"button": button}}]
        if event_type in (quartz.kCGEventOtherMouseDown, quartz.kCGEventOtherMouseUp):
            name, is_down = self.button_of(event_type, event)
            if name not in WIRE_OTHER_BUTTONS.values():
                return []
            message_type = protocol.MSG_MOUSEDOWN if is_down else protocol.MSG_MOUSEUP
            return [{"type": message_type, "data": {"button": name}}]
        if event_type == quartz.kCGEventScrollWheel:
            is_continuous = bool(
                quartz.CGEventGetIntegerValueField(event, quartz.kCGScrollWheelEventIsContinuous)
            )
            if is_continuous:
                # Trackpad (and momentum from a trackpad swipe): reported in
                # points, as floats, so smooth motion stays smooth end to
                # end instead of being quantized into wheel "clicks" early.
                dy = quartz.CGEventGetDoubleValueField(
                    event, quartz.kCGScrollWheelEventPointDeltaAxis1
                )
                dx = quartz.CGEventGetDoubleValueField(
                    event, quartz.kCGScrollWheelEventPointDeltaAxis2
                )
                if dx == 0 and dy == 0:
                    return []
                return [{"type": protocol.MSG_SCROLL, "data": {"dy": dy, "dx": dx, "mode": "pixel"}}]
            # A real (discrete) wheel: whole line/click deltas.
            dy = int(
                quartz.CGEventGetIntegerValueField(event, quartz.kCGScrollWheelEventDeltaAxis1)
            )
            dx = int(
                quartz.CGEventGetIntegerValueField(event, quartz.kCGScrollWheelEventDeltaAxis2)
            )
            if dx == 0 and dy == 0:
                return []
            return [{"type": protocol.MSG_SCROLL, "data": {"dy": dy, "dx": dx, "mode": "line"}}]
        return []

    def button_of(self, event_type, event):
        """(name, is_down) for a button event, else None. Left, right and the other buttons by
        name where the wire has one -- middle, back, forward -- and by their 1-based number
        where it does not, so a sixth button can still be kept on this Mac."""
        quartz = self.quartz
        fixed = {
            quartz.kCGEventLeftMouseDown: ("left", True),
            quartz.kCGEventLeftMouseUp: ("left", False),
            quartz.kCGEventRightMouseDown: ("right", True),
            quartz.kCGEventRightMouseUp: ("right", False),
        }
        if event_type in fixed:
            return fixed[event_type]
        if event_type not in (quartz.kCGEventOtherMouseDown, quartz.kCGEventOtherMouseUp):
            return None
        number = int(quartz.CGEventGetIntegerValueField(event, quartz.kCGMouseEventButtonNumber))
        return WIRE_OTHER_BUTTONS.get(number, str(number + 1)), event_type == quartz.kCGEventOtherMouseDown

    def reset_mouse_accumulators(self):
        """Drop any sub-pixel remainder carried across mousemove events.
        Called when redirecting turns off so a stale fractional carry from
        one redirect session doesn't nudge the pointer at the start of the
        next one."""
        self._move_accum_x = 0.0
        self._move_accum_y = 0.0

    def _modifier_transition(self, keycode, event):
        """Derive is_down from the event's own flags rather than toggling a
        remembered state. A missed key-up (tap timeout/restart) can no longer
        desync detection, since every event is judged independently.

        `modifier_down` is still maintained (add/discard) for bookkeeping and
        so update_config can clear stale state, but nothing here depends on
        its prior contents to decide is_down.
        """
        flags = self.quartz.CGEventGetFlags(event)
        device_bit = NX_DEVICE_MODIFIER_BITS.get(keycode, 0)
        if device_bit and (flags & _ALL_NX_DEVICE_MODIFIER_BITS):
            is_down = bool(flags & device_bit)
        else:
            # No device-specific bits present in these flags at all (seen from
            # non-standard event sources, e.g. tests) — fall back to the
            # generic mask for this modifier's family.
            generic_mask = self.modifier_flags.get(keycode, 0)
            is_down = bool(generic_mask and flags & generic_mask)
        if is_down:
            self.modifier_down.add(keycode)
        else:
            self.modifier_down.discard(keycode)
        return is_down

    def _special_key_messages(self, keycode, is_down, key_map):
        name = SPECIAL_KEY_NAMES.get(keycode)
        if name is None:
            return []
        wire_name = key_map.get(name, name)
        message_type = protocol.MSG_KEYDOWN if is_down else protocol.MSG_KEYUP
        return [{"type": message_type, "data": {"key": wire_name}}]

    def _caps_lock_messages(self, key_map):
        wire_name = key_map.get("caps_lock", "caps_lock")
        return [
            {"type": protocol.MSG_KEYDOWN, "data": {"key": wire_name}},
            {"type": protocol.MSG_KEYUP, "data": {"key": wire_name}},
        ]

    def _unicode_character(self, event, keycode):
        # While a chord modifier is held, what macOS composes is not the key
        # the chord is made of: Option+V is "\u221a", not "v", so forwarding the
        # composed character sends Windows a square-root sign and the Alt+V
        # chord never exists on the other side. Ctrl behaves the same way with
        # control characters, and only survives today because those are not
        # printable and already fall through. So while one of those is down,
        # the key's own unshifted character is what Windows needs, read from
        # this Mac's layout so a German Cmd+Z is Ctrl+Z, not Ctrl+Y. Shift and
        # caps lock are deliberately not in that set -- they are meant to
        # change the character, and the receiver reconciles them.
        if self.modifier_down & CHORD_MODIFIER_KEY_CODES:
            base = keyboard_layout.char_for(keycode)
            if base is not None:
                return base
        length, characters = self.quartz.CGEventKeyboardGetUnicodeString(
            event, 16, None, None
        )
        if length and isinstance(characters, str) and len(characters) == 1 and characters.isprintable():
            return characters
        return keyboard_layout.char_for(keycode)


class _GestureEventView:
    """Lazy, defensive adapter from a real AppKit NSEvent to the plain
    `.magnification`/`.deltaX`/`.deltaY`/`.phase` surface GestureTranslator
    expects. Each accessor is only invoked when actually read -- gesture.py
    reads just the one or two fields relevant to the event's own type, so
    this never calls an accessor (e.g. `.magnification()`) that a different
    gesture subtype (e.g. a swipe) may not support. Any exception raised by
    the underlying NSEvent method propagates to the caller, which is exactly
    what GestureTranslator.translate already treats as "no gesture"."""

    def __init__(self, ns_event):
        self._ns_event = ns_event

    @property
    def magnification(self):
        return float(self._ns_event.magnification())

    @property
    def deltaX(self):
        return float(self._ns_event.deltaX())

    @property
    def deltaY(self):
        return float(self._ns_event.deltaY())

    @property
    def phase(self):
        return int(self._ns_event.phase())


def _macos_major():
    """The running macOS major version, which keys the DockControl sign
    table in gestures.py. Falls back to the newest measured version when
    the string cannot be read, which is what the table does anyway."""
    try:
        return int(platform.mac_ver()[0].split(".")[0])
    except (ValueError, IndexError):
        return 26


def _default_gesture_event_converter(cg_event):
    """Convert a raw CGEvent carrying a gesture (magnify/swipe/etc.) into the
    `_GestureEventView` surface GestureTranslator consumes. Returns None if
    AppKit is unavailable or the conversion itself fails -- both treated by
    the caller as "gesture data unreadable; let this one event pass through
    locally" rather than an error. This is unsupported API surface (see
    gestures.py): NSEvent.eventWithCGEvent_ is not documented to accept a
    gesture-type CGEvent at all, only observed to work in practice."""
    if AppKit is None:
        return None
    ns_event = AppKit.NSEvent.eventWithCGEvent_(cg_event)
    if ns_event is None:
        return None
    return _GestureEventView(ns_event)


def _default_system_event_converter(cg_event):
    """Read (subtype, data1) off a system-defined CGEvent. Returns None if
    AppKit is unavailable or the event does not carry those fields, which
    the caller treats as "not a media key; pass it through locally"."""
    if AppKit is None:
        return None
    ns_event = AppKit.NSEvent.eventWithCGEvent_(cg_event)
    if ns_event is None:
        return None
    return int(ns_event.subtype()), int(ns_event.data1())


def links_from_store(settings_store, app_version):
    """The book and the identity the links run on, from the settings store; (None, None) when the
    settings cannot be read, so the controller starts from the defaults instead of failing."""
    try:
        settings_store.current()
    except Exception:
        logging.getLogger("Beamer").exception("the links start from the defaults, as the settings could not be read")
        return None, None

    def identity():
        settings = settings_store.current()
        name = (settings.get("name") or socket.gethostname().split(".")[0] or "Mac")[:48]
        return {"id": protocol.read_id(settings["machine_id"]), "name": name, "platform": OWN_PLATFORM, "app": app_version,
                "caps": list(CAPABILITIES), "port": settings["port"]}

    return settings_store.book(), identity


class _MemoryBook(receiver.PeerBook):
    """The peers a controller built from a Config alone has: peers[0] from the flat fields, nothing
    on disk. The app hands in the settings store's book instead; this is what the controller has
    when it is given none, and what keeps `KVMController(cfg)` working for a caller with no store."""

    def __init__(self, cfg):
        self.data = {"schema": 6, "machine_id": protocol.id_text(_random_id()), "peers": [], "zones": []}
        super().__init__(lambda: self.data, self._store)
        self.follow(cfg)

    def _store(self, data):
        self.data = copy.deepcopy(data)

    def follow(self, cfg):
        """Keep peers[0] in step with the flat Config the app still speaks."""
        with self.lock:
            peers = self.data["peers"]
            if not cfg.auth_token:
                peers.clear()
                return
            entry = peers[0] if peers and peers[0].get("token") == cfg.auth_token else None
            if entry is None:
                entry = {
                    "id": "", "name": "", "platform": "windows", "token": cfg.auth_token, "host": "", "port": 0, "hw": "",
                    "send": True, "allow_drive": True, "side": "", "side_set_at": 0, "side_by": "", "paired_with": [],
                    "paired_at": 0, "linked": False, "from_1_4": True,
                }
                peers[:1] = [entry]
            entry.update(host=cfg.host, port=cfg.port, name=cfg.pc_name or entry["name"], hw=cfg.mac_address,
                         send=cfg.send_to_windows, allow_drive=cfg.allow_windows_to_drive)


def _random_id():
    import secrets
    while True:
        data = secrets.token_bytes(protocol.MACHINE_ID_SIZE)
        if any(data):
            return data


class KVMController:
    @property
    def redirecting(self):
        return self._redirecting

    @redirecting.setter
    def redirecting(self, value):
        self._redirecting = bool(value)
        if not self._redirecting:
            self.pointer.show()

    def __init__(
        self,
        cfg,
        logger=None,
        quartz=Quartz,
        clock=time.monotonic,
        socket_factory=socket.create_connection,
        clipboard=clipboard_mac,
        gesture_event_converter=_default_gesture_event_converter,
        system_event_converter=_default_system_event_converter,
        dock_event_reader=None,
        macos_major=None,
        desktop_bounds=None,
        book=None,
        identity=None,
        link_factory=link_module.OutboundLink,
        hardware=None,
    ):
        self.cfg = cfg
        self.logger = logger or logging.getLogger("Beamer")
        self.quartz = quartz
        self.clock = clock
        self.socket_factory = socket_factory
        self.clipboard = clipboard
        self.gesture_event_converter = gesture_event_converter
        self.system_event_converter = system_event_converter
        self.dock_event_reader = dock_event_reader or self._default_dock_event_reader
        self.gesture_translator = gestures.GestureTranslator(logger=self.logger)
        self.dock_classifier = gestures.DockSwipeClassifier(
            macos_major=_macos_major() if macos_major is None else macos_major,
            logger=self.logger,
        )
        self._gesture_capture_logged = False
        self._overlay_gesture_capture_logged = False
        # Belt-and-braces cursor pinning (see _warp_cursor_to_pin): on some
        # macOS versions CGAssociateMouseAndMouseCursorPosition(False) is
        # silently ineffective, so the Mac cursor is additionally warped
        # back to this point on every captured mouse move/drag while
        # redirecting. None whenever not redirecting.
        self.cursor_pin_point = None
        # Hidden for as long as input is on the other machine. Made before `redirecting`, whose
        # setter shows it: input being local is the one condition under which it is never hidden.
        self.pointer = PointerHider(quartz, logger=self.logger)
        self._cursor_assoc_status_logged = False
        self._cursor_warp_error_logged = False
        self.redirecting = False
        # True while another machine is driving this Mac over a link it opened. The tap passes
        # everything through while it is, except this Mac's own trigger key, so the two directions
        # can never both own the keyboard. Set by the app that owns both halves, which also hands
        # in `send_peer_home`: the responder's way of sending its owner back.
        self.receiving = False
        self.driver = None               # the id text of the machine driving this Mac, set by the app
        self.send_peer_home = None
        # The responder, once WindowsInput has made it: `driven` is how a take it is deciding is
        # seen from here (WIRE.md section 4, "a machine never drives and is driven at once").
        self.responder = None
        self.stop_event = threading.Event()
        self.outbound = queue.Queue(maxsize=OUTBOUND_QUEUE_SIZE)
        self.on_user_alert = None
        # The peer's arrangement (mac_edge, set_at, by), its settings (data, peer id) and its
        # `paired` list (peer id, ids), handed to the app, which owns the settings file.
        self.on_arrangement = None
        self.on_settings = None
        self.on_paired = None
        # What to send a peer the moment its link is up: the app's own messages, of which only the
        # settings state goes on to a peer that keeps it (the arrangement and `paired` are made here).
        self.announce = lambda: []
        self.threads = []
        self.started = False
        self.capture_lock = threading.Lock()
        self.capture_thread = None
        self.input_error = None
        self._connection_status = "Waiting to connect"
        self.tap_ready = threading.Event()
        self.event_tap = None
        self.tap_source = None
        self.tap_run_loop = None
        self.tap_callback_ref = self._event_tap_callback
        self.translator = QuartzEventTranslator(quartz)
        self.trigger_code = KEY_NAME_TO_CODE[cfg.trigger_key]
        self.last_trigger_down = 0.0
        self.trigger_suppressed = False
        self.redirect_started_at = 0.0
        self.ignore_gate = ignored.Gate(cfg.ignored_inputs)
        # Crossing: the engine is pure and runs on the event-tap thread; the
        # desktop bounds come from CoreGraphics (already in the top-left
        # global space CGEventGetLocation uses, so nothing is flipped) and are
        # cached because they are read on every local mouse move. The notch
        # range needs NSScreen, so the app measures it on the main thread and
        # assigns it here. on_crossing is fed (kind, step) off the tap and
        # link threads for haptics and the glow; the app marshals it.
        # A PC whose address changed, after a router restart hands it a new lease, still beacons
        # under its name. With `discovery` set (anything with `pcs()` or `machines()` giving dicts
        # of `name` and `address`), a link down for ten seconds tries the address its peer's name
        # is heard at, and only once that address has passed the authenticated handshake is it
        # saved (by the link, through the book) and `on_host_learned(host)` called for the app.
        self.discovery = None
        self.on_host_learned = None
        self.crossing = crossing.CrossingEngine.from_config(cfg.crossing)
        self.desktop_bounds = desktop_bounds or self._default_desktop_bounds
        self.notch_range = None
        self.on_crossing = None
        self._crossing_failed = False
        self._crossing_failed_at = 0.0
        self._bounds_cache = None
        self._bounds_at = 0.0
        self._displays_cache = None
        self._displays_at = 0.0
        # Both hold the pointer methods off while the shortcut keeps working. Paused is the
        # person's own choice from the menu and deliberately not a setting, so a pause nobody
        # remembers cannot survive a restart; full_screen_app is the name of the frontmost app
        # while it is full screen, measured by the app on the main thread like notch_range.
        self.crossing_paused = False
        self._full_screen_app = None
        # The links: one outbound link per peer with `send` on, and the owner (WIRE.md sections 4
        # and 5) that decides where this Mac's input is. Every call into the owner is made under
        # `_owner_lock`; what a call asks for locally (the pointer, an alert) is done under
        # `_effects_lock`, taken before the owner's is let go so it runs in the order it was decided.
        self._memory = None
        if book is None:
            self._memory = book = _MemoryBook(cfg)
        self.book = book
        self._identity_fn = identity
        self._hardware = hardware        # host: this Mac's hardware address on the interface towards it
        self.link_factory = link_factory
        self.links = {}                  # token: OutboundLink
        self._peers_up = {}              # peer id: link, while its handshake is done
        self._up_names = {}
        self._accepts = {}               # peer id: whether it lets this Mac drive it
        self._primary_token = None
        self._primary_link = None
        self._links_lock = threading.RLock()
        # Whoever assigns `cfg` or builds a copy of it from the current one: a link thread learning
        # an address must not put back a settings change the main thread made meanwhile.
        self._cfg_lock = threading.Lock()
        self._owner_lock = threading.RLock()
        self._effects_lock = threading.RLock()
        # A peer whose clipboard is being read, and what was decided for it meanwhile, in order; and
        # the pasteboard's reads and writes, done one at a time in order on a thread of their own.
        self._clip_lock = threading.Lock()
        self._clip_wait = {}
        self._pasteboard_jobs = collections.deque()
        self._pasteboard_busy = False
        self.owner = owner_module.Owner(protocol.id_text(self.identity()["id"]), clock, resistance_px=int(self.crossing.resistance_px))
        self._clipboard_stamp = None
        self._paired_ids = ()
        self._go_generation = 0
        self._home_notify = True
        self._home_presses = set()
        self._letting_go = False
        # The machine this Mac's input was last on, which the shortcut goes back to.
        self._last_peer = None
        self._sync_links()
        self.crossing = self._crossing_engine()

    # The machines, and which one a shortcut or a status means

    def _crossing_engine(self):
        """This Mac's zones for every machine it sends to (WIRE.md section 8). A controller made from a
        Config alone has the flat crossing settings for its one machine instead."""
        if self._memory is not None:
            if not self.cfg.send_to_windows:
                return crossing.CrossingEngine(resistance_px=self.cfg.crossing["resistance_px"],
                                               block_while_dragging=self.cfg.crossing["block_while_dragging"], ways=[])
            return crossing.CrossingEngine.from_config(self.cfg.crossing)
        peers = [entry for entry in self.book.peers() if entry.get("send") is True]
        return crossing.CrossingEngine.from_zones(self.cfg.crossing, self.book.zones(), peers)

    def zones_changed(self):
        """A machine's side or zones changed in the settings: the engine is built again from them."""
        self.crossing = self._crossing_engine()

    def _ready(self, peer):
        """Whether `peer` can take this Mac's input now: its link is up and it accepts input."""
        link = self._peers_up.get(peer) if peer else None
        return link is not None and link.live() and self._accepts.get(peer, False)

    def _crossing_ready(self, peer):
        """Whether a zone to `peer` has a wall now: its link is up, this Mac sends to it, and it is not
        held back after a refusal, as Windows' zones are, since a push that cannot take it must not pin
        the pointer and glow as if it would. One that does not accept input keeps its wall, so the push
        says why it cannot cross. The one machine of a Config-built controller is its primary link's."""
        if peer is None:
            link = self._primary_link
            peer = link.peer_id if link is not None else None
        link = self._peers_up.get(peer) if peer else None
        if link is None or not link.live() or not self._may_send(peer):
            return False
        with self._owner_lock:
            return not self.owner.held_back(peer)

    def _may_send(self, peer):
        """Whether this Mac's direction switch for `peer` is on, as the settings have it this moment:
        its link goes only at the next sync."""
        if self._memory is not None:
            return bool(self.cfg.send_to_windows)
        return any(entry.get("id") == peer and entry.get("send") is True for entry in self.book.peers())

    def _zone_peer(self, step):
        """The machine a push through a zone goes to; a Config-built controller's one machine has no id in its zones."""
        if step.peer is not None:
            return step.peer
        link = self._primary_link
        return link.peer_id if link is not None else None

    def _shortcut_peer(self):
        """The machine the shortcut, the Send button and the menu's own item send input to (core/ways.py)."""
        if self._memory is not None:
            link = self._primary_link
            return link.peer_id if link is not None and link.peer_id else None
        return ways.shortcut_peer(self.book.peers(), self._last_peer, self._ready)

    def _link_of(self, peer):
        """The link to `peer`, up or not; None when this Mac has none to it."""
        link = self._peers_up.get(peer) if peer else None
        if link is not None:
            return link
        for candidate in list(self.links.values()):
            if candidate.peer_id == peer or (candidate.entry() or {}).get("id") == peer:
                return candidate
        return None

    def _in_question(self):
        """The machine the window's status speaks of: where input is, else where the shortcut would send it."""
        return self.owner.on or self._shortcut_peer()

    def _status_link(self):
        link = self._link_of(self._in_question())
        return link if link is not None else self._primary_link

    # The machine, as its links say it

    def identity(self):
        """This machine as a `hello` says it: the app's, or a synthetic one for a controller made
        from a Config alone."""
        if self._identity_fn is not None:
            return self._identity_fn()
        settings = self.book._load() or {}
        ident = protocol.read_id(settings.get("machine_id")) or _random_id()
        return {"id": ident, "name": socket.gethostname().split(".")[0] or "Mac", "platform": OWN_PLATFORM,
                "app": "1.5.0", "caps": list(CAPABILITIES), "port": self.cfg.port}

    @property
    def own_port(self):
        """The port this Mac listens on and announces: its own setting, never a machine's."""
        return int(self.identity()["port"])

    @property
    def peer_label(self):
        """The machine the window's status speaks of (where input is, else where the shortcut would
        send it), as the window shows it: its name, with the end of its id where two share one; the
        first machine when neither is known, None when nothing is paired."""
        peer = self._in_question()
        if peer:
            label = self._label(lambda entry: entry.get("id") == peer)
            if label:
                return label
        return self._label(lambda entry: entry.get("token") == self._primary_token)

    @property
    def on_label(self):
        """The machine this Mac's input is on, or None at home."""
        on = self.owner.on
        return self._label(lambda peer: on is not None and peer.get("id") == on)

    @property
    def driver_label(self):
        """The machine driving this Mac, or None."""
        driver = self.driver
        return self._label(lambda peer: driver is not None and peer.get("id") == driver)

    def _label(self, wanted):
        peers = self.book.peers()
        labels = peerlist.labels(peers)
        return next((labels[peer["token"]] for peer in peers if wanted(peer)), None)

    @property
    def connection_status(self):
        link = self._status_link()
        if link is not None and link.peer_locked:
            return f"Unlocking {self.peer_label or 'the other machine'}…"
        if link is not None and link is not self._primary_link and link.status:
            return link.status
        return self._connection_status

    @connection_status.setter
    def connection_status(self, value):
        self._connection_status = value

    @property
    def status_kind(self):
        """What the link to the machine in question last did, as core.link.OutboundLink.kind says it."""
        link = self._status_link()
        return link.kind if link is not None else "none"

    @property
    def via_tunnel(self):
        link = self._status_link()
        return bool(link is not None and link.via_tunnel and link.live())

    @property
    def windows_locked(self):
        link = self._status_link()
        return bool(link is not None and link.peer_locked)

    @property
    def peer_settings(self):
        """Whether every machine with a link up keeps Same on all machines, None while no link is up:
        one that does not is left out of it, and the window says so."""
        live = [link for link in list(self._peers_up.values()) if link.live()]
        if not live:
            return None
        return all("settings" in link.caps for link in live)

    @property
    def full_screen_app(self):
        """The full-screen app holding the edges, or None: also None while this Mac's own setting
        has the hold off, read here so every reader of the hold agrees and a change applies at once."""
        if self.cfg.crossing.get("hold_full_screen", True):
            return self._full_screen_app
        return None

    @full_screen_app.setter
    def full_screen_app(self, name):
        self._full_screen_app = name

    @property
    def connected(self):
        link = self._status_link()
        return link is not None and link.live()

    @property
    def input_ready(self):
        return self.event_tap is not None and self.input_error is None

    @property
    def round_trip_ms(self):
        """The smoothed input round trip, or None whenever a figure would not be honest: input
        is not on another machine, the link is down, or nothing fresh has been acknowledged for a
        few seconds. The upper median of the last few trips, so an even count rounds towards the
        slower one. The responder holds each `ack` for however long it waited to batch it and says
        so (`held_us`), which the link has already taken off."""
        link = self._peers_up.get(self.owner.on) if self.redirecting else None
        if link is None or not link.live():
            return None
        trips = sorted(link.round_trips(ROUND_TRIP_MAX_AGE_SECONDS)[-ROUND_TRIP_SAMPLES:])
        if not trips:
            return None
        return int(round(trips[len(trips) // 2] * 1000))

    def start(self):
        if self.started:
            return
        self.started = True
        workers = (
            ("connection", self._connection_worker),
            ("outbound", self._outbound_worker),
        )
        for name, target in workers:
            thread = threading.Thread(
                target=self._thread_entry,
                args=(name, target),
                name=f"Beamer-{name}",
                daemon=True,
            )
            self.threads.append(thread)
            thread.start()
        for link in list(self.links.values()):
            link.start()

    def start_input_capture(self):
        """Spawn the event-tap thread and return immediately. This must never
        block, since it is called from the AppKit main thread. A separate
        watcher thread waits for tap_ready and records failure so a slow or
        failed tap creation can't freeze the UI."""
        with self.capture_lock:
            if self.stop_event.is_set():
                return False
            if self.capture_thread is not None and self.capture_thread.is_alive():
                return self.input_ready
            self.tap_ready.clear()
            self.input_error = None
            thread = threading.Thread(
                target=self._input_thread_entry,
                name="Beamer-event-tap",
                daemon=True,
            )
            self.capture_thread = thread
            self.threads.append(thread)
            thread.start()
            watcher = threading.Thread(
                target=self._capture_ready_watcher,
                name="Beamer-event-tap-watcher",
                daemon=True,
            )
            self.threads.append(watcher)
            watcher.start()
        return True

    def _capture_ready_watcher(self):
        if not self.tap_ready.wait(timeout=2.0):
            self.input_error = "Input capture did not start within two seconds."
            self._force_local(self.input_error)

    def stop(self):
        self._return_local()
        # The let-go and what is ahead of it may be waiting behind a clipboard read: sent first.
        deadline = time.monotonic() + 1.0
        while (self._clip_wait or self._pasteboard_busy) and time.monotonic() < deadline:
            time.sleep(0.01)
        for link in list(self.links.values()):
            link.flush(0.3)
        self.stop_event.set()
        for link in list(self.links.values()):
            link.stop()
        run_loop = self.tap_run_loop
        if run_loop is not None:
            try:
                self.quartz.CFRunLoopStop(run_loop)
            except Exception:
                self.logger.exception("failed to stop the event-tap run loop")
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1.5)

    def update_config(self, cfg):
        self._return_local()
        with self._cfg_lock:
            self.cfg = cfg
        self.trigger_code = KEY_NAME_TO_CODE[cfg.trigger_key]
        self.last_trigger_down = 0.0
        self.trigger_suppressed = False
        self.translator.modifier_down.clear()
        self.translator.printable_down.clear()
        self.translator.reset_mouse_accumulators()
        self.ignore_gate.configure(cfg.ignored_inputs)
        if self._memory is not None:
            self._memory.follow(cfg)
        self.crossing = self._crossing_engine()
        self._crossing_failed = False
        self._drain_outbound()
        self._sync_links()
        # A link made to an address that is no longer the saved one goes, and comes back to the new.
        for link in list(self.links.values()):
            entry = link.entry()
            if link.live() and entry is not None and link.dialled != (entry.get("host"), entry.get("port")):
                link.drop("Settings saved; reconnecting")
            link.refresh()
        self.connection_status = "Settings saved; reconnecting"
        self.logger.info("settings updated; redirect mode returned to local")

    def apply_settings(self, cfg):
        """Everything that does not change which PC this is or how it is reached, applied without
        touching the link: the trigger, key map, crossing and how it looks and feels. The settings
        window calls this as each control changes; update_config is for a new address or token,
        which has to reconnect."""
        with self._cfg_lock:
            self.cfg = cfg
        # No machine's side goes from here: the flat settings only mirror the first machine's, and a
        # new first machine's would be the default. Sides go from the window, one machine at a time
        # (`send_arrangement`), and with every link that comes up.
        self.trigger_code = KEY_NAME_TO_CODE[cfg.trigger_key]
        self.ignore_gate.configure(cfg.ignored_inputs)
        if self._memory is not None:
            self._memory.follow(cfg)
        self.crossing = self._crossing_engine()
        self._crossing_failed = False

    def set_redirecting(self, value, edge=None, offset=None, came_home=True, peer=None):
        """`edge` and `offset` are the edge of the other machine and fraction along it a
        crossing arrives at; absent for the shortcut, when it leaves its pointer where it is.
        `peer` is the machine to send input to: a zone's, or one a menu names; without it, the
        machine the shortcut picks (core/ways.py).

        Input coming home this way is a switch, the shortcut or the menu, and says so through
        on_crossing as "home" with where the pointer is, for the arrival that shows it.
        `came_home` is False for the ways back that show their own: a crossing that lands here
        (it arrives through the owner as a `switch`), and another machine taking this Mac over."""
        value = bool(value)
        if not value:
            self._go_generation += 1
        onward = value and self.redirecting and peer is not None and peer != self.owner.on
        if value == self.redirecting and not onward:
            return False
        if value:
            # A menu naming another machine while input is away moves it straight there (WIRE.md
            # section 5, "Moving input"): the owner lets go of the one it leaves in the same route.
            return self._redirect(edge, offset, peer)
        self._return_local()
        self.logger.info("input returned to this Mac")
        if came_home:
            pin = self._capture_cursor_pin_point()
            if pin is not None:
                self._notify_crossing("home", crossing.Step(pin=(pin[0], pin[1])))
        return True

    def _cannot_reach(self, peer):
        """Why a switch to `peer` cannot start, in the words its link last used."""
        link = self._link_of(peer)
        if link is None or link is self._primary_link:
            status = self.connection_status
        else:
            status = link.status or "Waiting to connect"
        name = self._name_of(peer) if peer else None
        return status if not name or name in status else f"{name}: {status}"

    def _redirect(self, edge, offset, peer=None):
        peer = peer or self._shortcut_peer()
        link = self._peers_up.get(peer) if peer else None
        if link is None or not link.live():
            self.logger.warning("cannot redirect: %s is not connected", self._name_of(peer) if peer else "the other machine")
            self._alert("Beamer", f"Cannot switch — {self._cannot_reach(peer)}")
            return False
        if self.receiving:
            # Driven, so this Mac sends its owner home first and drives once the owner has let go
            # (the responder ends it by force after a second): a machine never does both.
            send_home = self.send_peer_home
            if send_home is not None and send_home():
                self._go_after_let_go(edge, offset, peer=peer)
                return True
            self.logger.warning("cannot redirect: another machine is driving this Mac and cannot be reached")
            return False
        if not self._may_send(peer):
            self.logger.info("cannot redirect: sending this Mac's input to %s is switched off", self._name_of(peer))
            return False
        self.redirect_started_at = self.clock()
        self._note_clipboard()
        self._step(lambda owner: owner.go(peer, edge, offset))
        if not self.redirecting or self.owner.on != peer:
            return False
        responder = self.responder
        if responder is not None and responder.driven:
            # A take on this Mac was being decided as this Mac's own began: it won the race, so
            # this Mac's input goes home (WIRE.md section 4, "a machine never drives and is driven at once").
            self.logger.warning("another machine took this Mac as it began to drive; input comes home")
            self._return_local()
            return False
        return True

    def _go_after_let_go(self, edge, offset, step=None, peer=None):
        if self._letting_go:
            return
        self._letting_go = True
        generation = self._go_generation

        def wait_then_go():
            try:
                end = time.monotonic() + LET_GO_WAIT_SECONDS
                while self.receiving and not self.stop_event.is_set() and time.monotonic() < end:
                    self.stop_event.wait(0.02)
                if generation != self._go_generation:
                    return
                if self.receiving or self.stop_event.is_set():
                    self.logger.warning("the machine driving this Mac did not let go; the move is dropped")
                    return
                if self.set_redirecting(True, edge, offset, peer=peer) and step is not None:
                    self._notify_crossing("cross", step)
            finally:
                self._letting_go = False

        threading.Thread(target=wait_then_go, name="Beamer-let-go", daemon=True).start()

    def _return_local(self):
        """Every way input comes back to this Mac ends here: the switch home,
        a dropped link, a crashed worker, a disabled tap, a settings change.
        The cursor association is the part that must not be skipped -- a
        path that only cleared `redirecting` left the pointer decoupled from
        the hand, frozen on screen while the keyboard already worked. The machine the
        input was on is told too, while its link is up: it keeps treating this Mac as
        driving, with whatever keys it was holding still down, until it hears
        otherwise. On a dead link the responder hands back by itself."""
        effects = []
        taken = False
        try:
            with self._owner_lock:
                effects = self._execute(self.owner.go(None) if self.owner.away else [])
                self._effects_lock.acquire()
                taken = True
        except Exception:
            self.logger.exception("telling the other machine its input is home failed")
        if not taken:
            self._effects_lock.acquire()
        try:
            self._apply_effects(effects)
        finally:
            try:
                self._local_cleanup()
            finally:
                self._effects_lock.release()

    def _local_cleanup(self):
        self.redirecting = False
        self._set_cursor_follows_mouse(True)
        self.cursor_pin_point = None
        self.translator.reset_mouse_accumulators()
        self.ignore_gate.reset()
        self.crossing.reset()

    def set_receiving(self, value):
        """Another machine has taken input on this Mac, or given it back. Input is
        returned to this Mac's own hardware first: whatever the owner is about to
        do with the pointer, it must not read as a push against the edge."""
        value = bool(value)
        if value == self.receiving:
            return
        if value and self.redirecting:
            self.set_redirecting(False, came_home=False)
        self.receiving = value
        self.crossing.reset()

    def _enqueue_control(self, message):
        """Queue a message the event tap made itself, which must never block. Best-effort: a
        momentarily full queue drops it (logged)."""
        try:
            self.outbound.put_nowait(message)
        except queue.Full:
            self.logger.error("outbound queue full; dropped control message %r", message.get("type"))

    def _capture_cursor_pin_point(self):
        """Snapshot the cursor's current location as the point that will be
        warped back to for the rest of this redirect session (see
        _warp_cursor_to_pin). Returns None -- logged once -- if Quartz can't
        tell us where the cursor is; callers must treat that as "no pinning
        available" rather than raise, the same fail-open posture as the rest
        of this module's Quartz calls."""
        try:
            probe = self.quartz.CGEventCreate(None)
            return self.quartz.CGEventGetLocation(probe)
        except Exception:
            self.logger.exception("failed to capture the cursor pin point")
            return None

    def _set_cursor_follows_mouse(self, follows):
        try:
            status = self.quartz.CGAssociateMouseAndMouseCursorPosition(follows)
        except Exception:
            self.logger.exception("failed to toggle local cursor association")
            return
        # CGAssociateMouseAndMouseCursorPosition's return value was
        # previously ignored outright, which is how a macOS version where
        # this call is silently ineffective (see _warp_cursor_to_pin, the
        # belt-and-braces compensation for exactly that) went unnoticed.
        # Logged once -- not every redirect toggle -- purely as a
        # diagnostic breadcrumb.
        if not self._cursor_assoc_status_logged:
            self._cursor_assoc_status_logged = True
            self.logger.info(
                "CGAssociateMouseAndMouseCursorPosition(%s) returned status %r (0 == success)",
                follows,
                status,
            )

    def _warp_cursor_to_pin(self):
        """Belt-and-braces compensation for CGAssociateMouseAndMouseCursorPosition
        not holding on every macOS version: forcibly snap the (still
        Association-decoupled, in theory) local cursor back to the pinned
        point after every captured mouse move/drag while redirecting, so it
        cannot visibly wander even when the association call was a no-op."""
        point = self.cursor_pin_point
        if point is None:
            return
        try:
            status = self.quartz.CGWarpMouseCursorPosition(point)
        except Exception:
            self.logger.exception("failed to warp the cursor back to the pin point")
            return
        if status != 0 and not self._cursor_warp_error_logged:
            self._cursor_warp_error_logged = True
            self.logger.warning("CGWarpMouseCursorPosition returned error %r", status)

    def _alert(self, title, message):
        callback = self.on_user_alert
        if callback is None:
            return
        try:
            callback(title, message)
        except Exception:
            self.logger.exception("on_user_alert callback failed")

    def _thread_entry(self, name, target):
        try:
            target()
        except BaseException:
            self._return_local()
            self.logger.exception("background thread %s crashed; input forced local", name)

    def _event_tap_worker(self):
        with objc.autorelease_pool():
            self._run_event_tap()

    def _input_thread_entry(self):
        try:
            self._event_tap_worker()
        except BaseException as exc:
            self._return_local()
            self.input_error = str(exc)
            self.logger.exception("input capture stopped; input forced local")
        finally:
            with self.capture_lock:
                if self.capture_thread is threading.current_thread():
                    self.capture_thread = None

    def _run_event_tap(self):
        quartz = self.quartz
        event_types = (
            quartz.kCGEventKeyDown,
            quartz.kCGEventKeyUp,
            quartz.kCGEventFlagsChanged,
            quartz.kCGEventMouseMoved,
            quartz.kCGEventLeftMouseDragged,
            quartz.kCGEventRightMouseDragged,
            quartz.kCGEventOtherMouseDragged,
            quartz.kCGEventLeftMouseDown,
            quartz.kCGEventLeftMouseUp,
            quartz.kCGEventRightMouseDown,
            quartz.kCGEventRightMouseUp,
            quartz.kCGEventOtherMouseDown,
            quartz.kCGEventOtherMouseUp,
            quartz.kCGEventScrollWheel,
        )
        event_mask = 0
        for event_type in event_types:
            event_mask |= quartz.CGEventMaskBit(event_type)
        # Gesture events (pinch/swipe/etc., see gestures.py) have no public
        # kCGEventType* constant, so there is nothing to pass to
        # CGEventMaskBit -- these are the private NSEventType raw values,
        # bit-shifted directly. Harmless to request even if this macOS
        # version never delivers them (best-effort only); SmartMagnify's
        # value (32) needs the shift done in Python's arbitrary-precision
        # ints rather than a 32-bit C bitwise-or, hence not routed through
        # CGEventMaskBit here.
        for gesture_type in gestures.GESTURE_EVENT_TYPES:
            event_mask |= 1 << gesture_type
        # Media/volume keys arrive as NSSystemDefined, which likewise has no
        # public kCGEventType constant (see media_keys.py).
        event_mask |= 1 << media_keys.NX_SYSDEFINED_EVENT_TYPE
        try:
            tap = quartz.CGEventTapCreate(
                quartz.kCGSessionEventTap,
                quartz.kCGHeadInsertEventTap,
                quartz.kCGEventTapOptionDefault,
                event_mask,
                self.tap_callback_ref,
                None,
            )
            if tap is None:
                raise RuntimeError(
                    "CGEventTapCreate failed; grant Accessibility and Input Monitoring permission, then relaunch"
                )
            source = quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            if source is None:
                raise RuntimeError("CFMachPortCreateRunLoopSource failed")
            run_loop = quartz.CFRunLoopGetCurrent()
            self.event_tap = tap
            self.tap_source = source
            self.tap_run_loop = run_loop
            quartz.CFRunLoopAddSource(run_loop, source, quartz.kCFRunLoopCommonModes)
            quartz.CGEventTapEnable(tap, True)
            self.input_error = None
            self.tap_ready.set()
            quartz.CFRunLoopRun()
            if not self.stop_event.is_set():
                raise RuntimeError("event-tap run loop exited unexpectedly")
        except BaseException:
            self.tap_ready.set()
            raise

    def _event_tap_callback(self, proxy, event_type, event, refcon):
        try:
            if event_type in (
                self.quartz.kCGEventTapDisabledByTimeout,
                self.quartz.kCGEventTapDisabledByUserInput,
            ):
                # Input is local from here, so the cursor follows the hand
                # again; a re-enabled tap is never a reason to keep it pinned.
                self._return_local()
                self.logger.error("macOS disabled the event tap; input forced local")
                if self.event_tap is not None:
                    self.quartz.CGEventTapEnable(self.event_tap, True)
                return event
            if self.receiving:
                # The PC is driving this Mac over the other link. Everything
                # passes through untouched: crossing must not read a pointer
                # the PC is moving as a push against this Mac's own edge,
                # which would send input straight back where it came from.
                # The one exception is this Mac's own trigger key, which
                # sends the PC's input home -- so a return edge that never
                # fires cannot strand the PC's mouse here.
                if (
                    event_type in (self.quartz.kCGEventKeyDown, self.quartz.kCGEventKeyUp, self.quartz.kCGEventFlagsChanged)
                    and not self._was_injected(event)
                    and self.quartz.CGEventGetIntegerValueField(event, self.quartz.kCGKeyboardEventKeycode) == self.trigger_code
                ):
                    result = self.translator.key_result(event_type, event, self.trigger_code, {})
                    if result.is_trigger:
                        return self._handle_trigger(result, event)
                # And this Mac's own pointer, which can push through the edge to take it back
                # across; the PC's events carry the injected mark and never reach the edge. Its
                # clicks go the same way, since a click is what resets a push.
                if (
                    event_type not in (self.quartz.kCGEventKeyDown, self.quartz.kCGEventKeyUp, self.quartz.kCGEventFlagsChanged)
                    and event_type != media_keys.NX_SYSDEFINED_EVENT_TYPE
                    and not self.gesture_translator.wants(event_type)
                    and not self._was_injected(event)
                ):
                    return self._handle_local_mouse(event_type, event)
                return event
            if self.gesture_translator.wants(event_type):
                return self._handle_gesture_event(event_type, event)
            if event_type == media_keys.NX_SYSDEFINED_EVENT_TYPE:
                return self._handle_system_event(event)
            if self._was_injected(event):
                # One Beamer itself posted for the PC, on its way to an app
                # here. Only keys and pointer events are ever injected, which
                # is why this sits below the gesture and media branches.
                return event
            if event_type in (
                self.quartz.kCGEventKeyDown,
                self.quartz.kCGEventKeyUp,
                self.quartz.kCGEventFlagsChanged,
            ):
                result = self.translator.key_result(
                    event_type,
                    event,
                    self.trigger_code,
                    {},
                )
                if result.is_trigger:
                    return self._handle_trigger(result, event)
                had = len(result.messages)
                result.messages = self._after_home_presses(result.messages)
                if self.redirecting:
                    keycode = self.quartz.CGEventGetIntegerValueField(event, self.quartz.kCGKeyboardEventKeycode)
                    entry = ignored.key(keycode)
                    kept = self.ignore_gate.keeps(entry, result.is_down)
                    if keycode == CAPS_LOCK_KEY_CODE:
                        # One event per toggle, always read as down and sent as a press and its
                        # release, so it is a whole press to the gate too: a lone down would pin
                        # its route until input came home, whatever the list said by then.
                        self.ignore_gate.keeps(entry, False)
                    if kept:
                        return event
                messages = result.messages
                if had and not messages:
                    # The release of a key pressed here, while the input was home, stays here.
                    return event
            else:
                button = self.translator.button_of(event_type, event)
                if not self.redirecting:
                    if button is not None:
                        if button[1]:
                            self._home_presses.add(("button", button[0]))
                        elif ("button", button[0]) in self._home_presses:
                            self._home_presses.discard(("button", button[0]))
                        else:
                            self._enqueue_control({"type": protocol.MSG_MOUSEUP, "data": {"button": button[0]}})
                    return self._handle_local_mouse(event_type, event)
                if button is not None and not button[1] and ("button", button[0]) in self._home_presses:
                    self._home_presses.discard(("button", button[0]))
                    return event
                if button is not None and button[1]:
                    self._home_presses.discard(("button", button[0]))
                if button is not None and self.ignore_gate.keeps(ignored.button(button[0]), button[1]):
                    return event
                messages = self.translator.mouse_messages(event_type, event)
                if button is not None and not messages:
                    # A button the wire has no name for stays on this Mac, as one does on the PC,
                    # rather than being swallowed and reaching neither.
                    return event
            if not self.redirecting:
                for message in messages:
                    self._enqueue_control(message)
                return event
            for message in messages:
                try:
                    self.outbound.put_nowait(message)
                except queue.Full:
                    self._connection_failed("outbound event queue filled")
                    return event
            if self.redirecting and event_type in self.translator.movement_event_types:
                self._warp_cursor_to_pin()
            if not self.redirecting:
                return event
            return None
        except BaseException:
            self._return_local()
            self.logger.exception("event-tap callback failed; input forced local")
            return event

    def _after_home_presses(self, messages):
        """A press made while the input was home is released at home: at home its key is noted,
        and once the input has left, the release of a noted key is taken out of what is sent
        (WIRE.md section 4, "a key or button pressed on the owner while its input was at home is
        released at home")."""
        if not self.redirecting:
            # What comes back is the releases of keys that were not pressed here: pressed while the
            # input was away, they are the owner's to match against what it swallows.
            unmatched = []
            for message in messages:
                ident = ("key", message["data"]["key"])
                if message["type"] == protocol.MSG_KEYDOWN:
                    self._home_presses.add(ident)
                elif ident in self._home_presses:
                    self._home_presses.discard(ident)
                else:
                    unmatched.append(message)
            return unmatched
        kept = []
        for message in messages:
            ident = ("key", message["data"].get("key"))
            if message["type"] == protocol.MSG_KEYUP and ident in self._home_presses:
                self._home_presses.discard(ident)
                continue
            if message["type"] == protocol.MSG_KEYDOWN:
                # Pressed now, with the input away: any earlier press of it at home is over.
                self._home_presses.discard(ident)
            kept.append(message)
        return kept

    def _was_injected(self, event):
        """Whether this event carries the mark input_injector_mac stamps into
        every event it posts. Never raises: an event whose source cannot be
        read is treated as the hand's, which is the safe way round -- the
        worst case is a crossing that should not have fired, not a keyboard
        that stops working."""
        try:
            return self.quartz.CGEventGetIntegerValueField(
                event, self.quartz.kCGEventSourceUserData
            ) == INJECTED_MARK
        except Exception:
            return False

    def _handle_local_mouse(self, event_type, event):
        """Crossing lives on the local side of the tap: while the pointer is
        pushing against an armed edge, the event is swallowed and the pointer
        warped back to where the push began, which is the resistance; at
        breakthrough input moves to Windows. Everything else passes through.
        Fails open twice over: the caller's handler already returns the event
        on any exception, and a failure here also switches crossing off for
        CROSSING_RETRY_SECONDS or until the next settings save, so one broken
        geometry call cannot log an exception per mouse move -- and a passing
        one cannot leave the edge dead until someone thinks to save a setting."""
        if self._crossing_failed and self.clock() - self._crossing_failed_at < CROSSING_RETRY_SECONDS:
            return event
        self._crossing_failed = False
        if not self.crossing.armed:
            return event
        if self.crossing_paused or self.full_screen_app is not None:
            return event
        if event_type not in self.translator.movement_event_types:
            self.crossing.reset()
            return event
        if not any(self._crossing_ready(peer) for peer in self.crossing.peers):
            self.crossing.reset()
            return event
        try:
            quartz = self.quartz
            location = quartz.CGEventGetLocation(event)
            x, y = location[0], location[1]
            dx = quartz.CGEventGetDoubleValueField(event, quartz.kCGMouseEventDeltaX)
            dy = quartz.CGEventGetDoubleValueField(event, quartz.kCGMouseEventDeltaY)
            step = self.crossing.feed(
                x,
                y,
                dx,
                dy,
                self._current_desktop_bounds(),
                self.clock(),
                notch_range=self.notch_range,
                dragging=event_type != quartz.kCGEventMouseMoved,
                # Each display's own edges count where nothing lies beyond them, and Part of the
                # edge measures its thirds along the pointer's display.
                displays=self._current_displays(),
                ready=self._crossing_ready,
            )
        except Exception:
            self._crossing_failed = True
            self._crossing_failed_at = self.clock()
            self.crossing.reset()
            self.logger.exception("crossing failed; edge switching is off for %.0fs", CROSSING_RETRY_SECONDS)
            return event
        if step.crossed:
            if self.receiving:
                # Another machine is driving this Mac and this Mac's own pointer pushed through:
                # that machine's input goes home first, and this Mac's follows it across once it
                # has let go (WIRE.md section 4).
                send_home = self.send_peer_home
                if send_home is None or not send_home():
                    self.logger.warning("cannot cross: another machine is driving this Mac and cannot be reached")
                    return event
                self._go_after_let_go(step.edge, step.offset, step, peer=self._zone_peer(step))
                return event
            if self.set_redirecting(True, edge=step.edge, offset=step.offset, peer=self._zone_peer(step)):
                self._notify_crossing("cross", step)
                return None
            return event
        if step.hold:
            self._warp_cursor_to(step.pin)
            self._notify_crossing("tick" if step.tick else "pressure", step)
            return None
        if step.pressure > 0:
            self._notify_crossing("tick" if step.tick else "pressure", step)
        return event

    def _current_desktop_bounds(self):
        now = self.clock()
        if self._bounds_cache is None or now - self._bounds_at > DESKTOP_BOUNDS_MAX_AGE_SECONDS:
            self._bounds_cache = self.desktop_bounds()
            self._bounds_at = now
        return self._bounds_cache

    def _current_displays(self):
        now = self.clock()
        if self._displays_cache is None or now - self._displays_at > DESKTOP_BOUNDS_MAX_AGE_SECONDS:
            quartz = self.quartz
            try:
                displays = [
                    (rect.origin.x, rect.origin.y, rect.origin.x + rect.size.width, rect.origin.y + rect.size.height)
                    for rect in (quartz.CGDisplayBounds(display) for display in desktop_mac.display_ids(quartz))
                ]
            except Exception:
                # Without them the engine measures against the whole desktop, as it always could.
                return None
            self._displays_cache = displays or None
            self._displays_at = now
        return self._displays_cache

    def _default_desktop_bounds(self):
        quartz = self.quartz
        left = top = float("inf")
        right = bottom = float("-inf")
        for display in desktop_mac.display_ids(quartz):
            rect = quartz.CGDisplayBounds(display)
            left = min(left, rect.origin.x)
            top = min(top, rect.origin.y)
            right = max(right, rect.origin.x + rect.size.width)
            bottom = max(bottom, rect.origin.y + rect.size.height)
        return (left, top, right, bottom)

    def _warp_cursor_to(self, point):
        try:
            status = self.quartz.CGWarpMouseCursorPosition(point)
        except Exception:
            self.logger.exception("failed to warp the cursor")
            return
        if status != 0 and not self._cursor_warp_error_logged:
            self._cursor_warp_error_logged = True
            self.logger.warning("CGWarpMouseCursorPosition returned error %r", status)

    def _notify_crossing(self, kind, step):
        callback = self.on_crossing
        if callback is None:
            return
        try:
            callback(kind, step)
        except Exception:
            self.logger.exception("on_crossing callback failed")

    def crossing_pressure_now(self):
        """Current pressure as a fraction, decayed to now, for the glow to fade
        against after the last mouse event."""
        return self.crossing.pressure_at(self.clock())

    def _handle_system_event(self, event):
        """Forward a media/volume key press. Anything that is not one --
        brightness, keyboard backlight, and the undocumented subtypes that
        share NSSystemDefined -- passes through to macOS untouched, as does a
        media key kept on this Mac and everything when not redirecting."""
        if not self.redirecting:
            return event
        try:
            decoded = self.system_event_converter(event)
        except Exception:
            self.logger.debug("failed to convert a system-defined event", exc_info=True)
            return event
        if decoded is None:
            return event
        media = media_keys.decode(*decoded)
        if media is None:
            return event
        name, is_down, _is_repeat = media
        if self.ignore_gate.keeps(ignored.media(name), is_down):
            return event
        message_type = protocol.MSG_KEYDOWN if is_down else protocol.MSG_KEYUP
        wire_name = name
        try:
            self.outbound.put_nowait({"type": message_type, "data": {"key": wire_name}})
        except queue.Full:
            self._connection_failed("outbound event queue filled")
            return event
        if not self.redirecting:
            return event
        return None

    def _handle_gesture_event(self, event_type, event):
        """Handle one gesture-family event (magnify/swipe/etc., see
        gestures.py). When not redirecting, passed through untouched -- no
        conversion is even attempted, matching every other event type's
        local behaviour. While redirecting, converting to a readable form
        can fail (AppKit missing, or the conversion itself raising, since
        this is unsupported API surface): that failure fails open, passing
        this one event through locally rather than forcing a full
        disconnect, and capture continues for the next event either way."""
        if not self.redirecting:
            return event
        if event_type == gestures.DOCK_CONTROL_TYPE:
            # Swallowed whichever way classification goes: passing it on
            # would open Mission Control or switch Spaces on the Mac while
            # the user is looking at Windows.
            fields = self._read_dock_event(event)
            messages = self.dock_classifier.translate(fields) if fields is not None else []
        else:
            gesture_view = self._convert_gesture_event(event)
            if gesture_view is None:
                return event
            messages = self.gesture_translator.translate(event_type, gesture_view)
        if not self._gesture_capture_logged:
            self._gesture_capture_logged = True
            self.logger.info("gesture capture active")
        for message in messages:
            try:
                self.outbound.put_nowait(message)
            except queue.Full:
                self._connection_failed("outbound event queue filled")
                return event
        if not self.redirecting:
            return event
        return None

    def handle_overlay_gesture(self, event_type, gesture_view):
        """Counterpart to _handle_gesture_event for gestures captured by the
        overlay NSPanel's ordinary AppKit responder methods (magnifyWithEvent_
        / swipeWithEvent_, see kvm_bridge_app.py) rather than the CGEventTap
        -- macOS 27 beta was observed to never deliver gesture event types
        29-32 to the tap at all, so this is the path that actually works.

        Called from the AppKit main thread while the tap thread runs
        concurrently; thread-safe for the same reason the tap path is: it
        only translates the event and put_nowait()s onto the outbound
        queue, never blocking and never touching anything the tap thread
        also mutates apart from that queue. Silently a no-op when not
        redirecting, and never raises -- a broken conversion or translation
        must not be able to crash the AppKit main thread."""
        if not self.redirecting:
            return
        if not self._overlay_gesture_capture_logged:
            self._overlay_gesture_capture_logged = True
            self.logger.info("gesture capture active (overlay)")
        try:
            messages = self.gesture_translator.translate(event_type, gesture_view)
        except Exception:
            self.logger.exception("overlay gesture translation failed")
            return
        for message in messages:
            try:
                self.outbound.put_nowait(message)
            except queue.Full:
                self._connection_failed("outbound event queue filled")
                return

    def _convert_gesture_event(self, event):
        try:
            return self.gesture_event_converter(event)
        except Exception:
            self.logger.debug("failed to convert a gesture event", exc_info=True)
            return None

    def _read_dock_event(self, event):
        try:
            return self.dock_event_reader(event)
        except Exception:
            self.logger.debug("failed to read a DockControl event", exc_info=True)
            return None

    def _default_dock_event_reader(self, event):
        quartz = self.quartz
        return types.SimpleNamespace(
            hid=quartz.CGEventGetIntegerValueField(event, gestures.DOCK_FIELD_HID_TYPE),
            motion=quartz.CGEventGetIntegerValueField(event, gestures.DOCK_FIELD_MOTION),
            phase=quartz.CGEventGetIntegerValueField(event, gestures.DOCK_FIELD_PHASE),
            progress=quartz.CGEventGetDoubleValueField(event, gestures.DOCK_FIELD_PROGRESS),
            # Both axes carry the same value on the ended event; either is the direction.
            velocity=quartz.CGEventGetDoubleValueField(event, gestures.DOCK_FIELD_VELOCITY_X),
        )

    def _handle_trigger(self, result, event):
        was_redirecting = self.redirecting
        suppress = was_redirecting or self.trigger_suppressed
        changed = False
        if self.cfg.trigger_style == "hold":
            # Input is on Windows for exactly as long as the key is down. The
            # key itself never reaches either machine: it would be a stray
            # modifier on Windows, and on the Mac a press with nothing else is
            # what people use to switch for a single keystroke.
            if result.is_repeat:
                return None
            if result.is_down:
                self.set_redirecting(True)
            else:
                self.set_redirecting(False)
            return None
        if result.is_down and not result.is_repeat:
            now = self.clock()
            threshold = self.cfg.double_tap_ms / 1000.0
            if self.last_trigger_down and now - self.last_trigger_down <= threshold:
                changed = self.set_redirecting(not was_redirecting)
                self.last_trigger_down = 0.0
            else:
                self.last_trigger_down = now
            suppress = was_redirecting or changed
            self.trigger_suppressed = suppress
        elif not result.is_down:
            suppress = was_redirecting or self.trigger_suppressed
            self.trigger_suppressed = False
        return None if suppress else event

    # The links, and what the owner decides

    def _sync_links(self):
        """One link per peer with `send` on, made and ended to match the peers as the book has them
        now. Cheap, and called every couple of seconds as well as when the settings change."""
        peers = self.book.peers()
        wanted = {peer["token"] for peer in peers if peer.get("token") and peer.get("send") is True}
        first = peers[0].get("token") if peers else None
        with self._links_lock:
            gone = [self.links.pop(token) for token in [token for token in self.links if token not in wanted]]
            if not self.stop_event.is_set():
                hardware = {"hardware": self._hardware} if self._hardware is not None else {}
                for token in wanted - set(self.links):
                    link = self.link_factory(
                        token, self.book, self.identity,
                        state=self._link_state, up=self._link_up, message=self._link_message,
                        socket_factory=self.socket_factory,
                        tunnel=self._connect_via_ssh_fallback if token == first else None,
                        clock=self.clock, reconnect_seconds=self.cfg.reconnect_interval_s, **hardware,
                        large=lambda link, began: self.owner.expects_clipboard(link.peer_id, began),
                    )
                    self.links[token] = link
                    if self.started:
                        link.start()
            self._primary_token = first
            self._primary_link = self.links.get(first)
        for link in gone:
            # Without waiting: a link thread can be in a 3 second connect, and this runs on the
            # main thread when a window removes or switches off machines.
            link.stop(wait=False)
            self._forget_link(link)

    def peers_changed(self):
        """The peers changed under the links (a pairing made or removed): bring the links in step,
        and tell every peer we can send to who else this machine has (`paired`)."""
        self._sync_links()
        self.zones_changed()
        for link in list(self.links.values()):
            link.refresh()
            if link.live():
                self._announce_paired(link)
        self._paired_ids = self._pairing_ids()

    def _forget_link(self, link):
        self._drop_up(link)

    def _drop_up(self, link):
        """The link is no longer one this Mac can send on: forget it and tell the owner."""
        peer = link.peer_id
        if peer and self._peers_up.get(peer) is link:
            self._peers_up.pop(peer, None)
            self._forget_held(peer)
            self._step(lambda owner: owner.link_down(peer))

    def _connection_worker(self):
        while not self.stop_event.is_set():
            try:
                self._sync_links()
                self._announce_changes()
                self._follow_the_peers()
            except Exception:
                self.logger.exception("keeping the links in step failed")
            self.stop_event.wait(self.cfg.reconnect_interval_s)

    def _follow_the_peers(self):
        """A peer that changed address after a router restart still beacons under its name: the link
        itself decides whether to try that address (down for ten seconds, once in thirty)."""
        discovery = self.discovery
        if discovery is None or not self.links:
            return
        machines = list(discovery.pcs())
        for link in list(self.links.values()):
            link.follow(machines)

    def _connect_via_ssh_fallback(self):
        try:
            return self.socket_factory(
                (SSH_FALLBACK_HOST, SSH_FALLBACK_PORT),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except OSError as exc:
            raise OSError(
                errno.EHOSTUNREACH,
                "Open /Applications/Beamer Tunnel.command",
            ) from exc

    def _name_of(self, peer):
        return self._label(lambda entry: entry.get("id") == peer) or self._up_names.get(peer) or "the other machine"

    # Link callbacks: each on a link's own thread

    def _link_state(self, link, up, text):
        if link.token == self._primary_token:
            self.connection_status = text
        if up:
            return
        self.logger.warning("link to %s: %s", link.peer_name or link.token[:6], text)
        self._drop_up(link)

    def _link_up(self, link, fields):
        if self.links.get(link.token) is not link:
            # Its machine went while the handshake finished: it must not take a place in the owner.
            return
        peer = protocol.id_text(fields["id"])
        self._up_names[peer] = fields["name"]
        self._accepts[peer] = bool(fields["accepts"])
        self._peers_up[peer] = link
        forget = getattr(self.clipboard, "forget_sync", None)
        if forget is not None:
            forget()
        self._step(lambda owner: owner.link_up(peer, fields["accepts"]))
        if self.links.get(link.token) is not link:
            # Its machine went after the check above, and `_sync_links` ran its `_drop_up` before
            # this registered the link: undo what that could not see.
            if self._peers_up.get(peer) is link:
                del self._peers_up[peer]
            self._forget_held(peer)
            self._step(lambda owner: owner.link_down(peer))
            return
        self.logger.info("connected to %s", fields["name"])
        # The handshake may have just given a migrated machine its id, and its zones with it.
        self.zones_changed()
        self._announce_to(link, fields)
        self._peer_up(link, fields)
        if link.token == self._primary_token:
            self._primary_up(link, fields)

    def _peer_up(self, link, fields):
        """Any machine's link is up: a hook for WakingController, which learns its hardware address."""

    def _primary_up(self, link, fields):
        """The primary peer's link is up: its saved address is the one now in use. A hook for
        WakingController, which learns the hardware address."""
        entry = link.entry() or {}
        host = entry.get("host")
        with self._cfg_lock:
            changed = bool(host) and host != self.cfg.host
            if changed:
                self.cfg = dataclasses.replace(self.cfg, host=host)
        if changed:
            callback = self.on_host_learned
            if callback is not None:
                try:
                    callback(host)
                except Exception:
                    self.logger.exception("saving the peer's new address failed")

    def _announce_to(self, link, fields):
        """What this Mac tells a peer the moment the link is up (WIRE.md section 3)."""
        entry = link.entry() or {}
        own = bytes(self.identity()["id"])
        if entry.get("side") in crossing.EDGES:
            by = protocol.read_id(entry.get("side_by")) or own
            link.post(protocol.arrangement_v6(entry["side"], entry.get("side_set_at", 0), by,
                                              way_back=self.way_back(link.peer_id)))
        if "settings" in link.caps:
            try:
                announcements = list(self.announce())
            except Exception:
                self.logger.exception("announce failed")
                announcements = []
            for message in announcements:
                if message.get("type") == protocol.MSG_SETTINGS:
                    link.post(message)
        self._announce_paired(link)

    def _pairing_ids(self):
        return tuple(peer.get("id") for peer in self.book.peers() if protocol.read_id(peer.get("id")) is not None)

    def _announce_paired(self, link):
        ids = [protocol.read_id(peer.get("id")) for peer in self.book.peers() if peer.get("id") != link.peer_id]
        ids = [ident for ident in ids if ident is not None][:protocol.MAX_PEERS]
        link.post(protocol.paired_msg(ids))

    def _announce_changes(self):
        """Who this Mac has changed (a pairing made or removed): every live peer is told again."""
        ids = self._pairing_ids()
        if ids == self._paired_ids:
            return
        self._paired_ids = ids
        for link in list(self.links.values()):
            if link.live():
                self._announce_paired(link)

    def _link_message(self, link, message):
        kind = message.get("type")
        peer = link.peer_id
        try:
            if kind == protocol.MSG_SWITCH:
                read = protocol.read_switch(message)
                if read is not None:
                    data = {"route": read["route"], "next": protocol.id_text(read["next"]),
                            "edge": read["edge"], "offset": read["offset"]}
                    self._step(lambda owner: owner.switch(peer, data))
            elif kind == protocol.MSG_ACCEPT:
                read = protocol.read_accept(message)
                if read is not None:
                    self._step(lambda owner: owner.accept(peer, read))
            elif kind == protocol.MSG_REFUSE:
                read = protocol.read_refuse(message)
                if read is not None:
                    self._step(lambda owner: owner.refuse(peer, read))
            elif kind == protocol.MSG_ACCEPTS:
                read = protocol.read_accepts(message)
                if read is not None:
                    self._accepts[peer] = read["accepts"]
                    self._step(lambda owner: owner.accepts(peer, read["accepts"]))
            elif kind == protocol.MSG_CLIPBOARD:
                began = getattr(message, "began_at", self.clock())
                read = protocol.read_clipboard(message)
                if read is not None:
                    # Rebuilt from what was read: the owner sends it on to the machine input is on.
                    clean = protocol.clipboard_msg(read["text"], read["image"])
                    self._step(lambda owner: owner.clipboard_arrived(peer, began, clean))
            elif kind == protocol.MSG_ARRANGEMENT:
                self._handle_arrangement(message, peer)
            elif kind == protocol.MSG_SETTINGS:
                data = message.get("data")
                if isinstance(data, dict) and "settings" in link.caps and self.on_settings is not None:
                    self.on_settings(data, peer)
            elif kind == protocol.MSG_PAIRED:
                ids = protocol.read_paired(message)
                if ids is not None:
                    texts = [protocol.id_text(ident) for ident in ids]
                    self.book.store_paired(peer, texts)
                    if self.on_paired is not None:
                        self.on_paired(peer, texts)
            else:
                self.logger.debug("ignoring inbound message of type %r", kind)
        except Exception:
            self.logger.exception("handling a %r from the other machine failed", kind)

    def _handle_arrangement(self, message, peer):
        """A machine changed which edge of its screen faces this one. Handed to the app, which owns
        the settings file, as (that machine's id, its edge, the stamp, who made it); nothing is
        applied here."""
        read = protocol.read_arrangement_v6(message, time.time())
        if read is None or self.on_arrangement is None or not peer:
            return
        self.on_arrangement(peer, read["edge"], read["set_at"], protocol.id_text(read["by"]), read.get("way_back"))

    def way_back(self, peer):
        """Whether one of this Mac's zones in use leads to `peer` and can fire: what every `arrangement`
        to it says as `way_back` (WIRE.md section 8)."""
        with self.book.lock:
            return ways.has_way({"peers": self.book.peers(), "zones": self.book.zones()}, peer)

    def send_arrangement(self, peer, mac_edge, set_at, by=None):
        """Tell `peer` which edge of this Mac faces it, stamped `set_at` by `by` (a b64 id, this Mac when
        None), with whether a way leads there, over this Mac's own link to it. Sent straight out: it is
        not input, and it goes whether or not input is redirected. False when that link is not up; the
        app then tries the link the peer opened."""
        link = self._peers_up.get(peer) if peer else None
        if link is None or not link.live():
            return False
        author = protocol.read_id(by) or bytes(self.identity()["id"])
        return link.post(protocol.arrangement_v6(mac_edge, int(set_at), author, way_back=self.way_back(peer)))

    def send_settings(self, data, source=None):
        """Tell every peer that keeps Same on all machines this Mac's state, but `source` (the peer
        it came from, when this is a newer state passed on)."""
        sent = False
        for peer, link in list(self._peers_up.items()):
            if peer != source and "settings" in link.caps and link.live():
                sent = link.post(protocol.settings_msg(data)) or sent
        return sent

    # The owner

    def _step(self, call):
        """One call into the owner, its messages sent, and then what it asks for here (the
        pointer, an alert) done in the order it was decided in, whichever thread decided it."""
        with self._owner_lock:
            self.owner.resistance_px = int(self.crossing.resistance_px)
            effects = self._execute(call(self.owner))
            self._effects_lock.acquire()
        try:
            self._apply_effects(effects)
        finally:
            self._effects_lock.release()

    def _execute(self, actions):
        """Under `_owner_lock`: send what the owner says to send, and return the rest."""
        effects = []
        for action in actions:
            if not isinstance(action, (owner_module.Send, owner_module.SendClipboard, owner_module.SetClipboard, owner_module.Drop)):
                effects.append(action)
                continue
            try:
                if isinstance(action, owner_module.Drop):
                    self._drop(action.peer)
                elif isinstance(action, (owner_module.Send, owner_module.SendClipboard)):
                    self._deliver(action)
                else:
                    self._on_pasteboard(lambda message=action.message: self._set_clipboard(message))
            except Exception:
                # One message that cannot go must not stop the move it belongs to: its Moved is next.
                self.logger.exception("carrying out %s failed", type(action).__name__)
        return effects

    def _apply_effects(self, effects):
        for effect in effects:
            if isinstance(effect, owner_module.Moved):
                self._on_moved(effect)
            elif isinstance(effect, owner_module.Unreachable):
                self._on_unreachable(effect)

    def _deliver(self, action):
        """Sends in the order the owner gave them, except that reading the pasteboard (which can take
        seconds, while the event tap waits for the owner's lock) is done on a thread of its own:
        what follows a clipboard read for the same peer waits for it, and everything else goes on."""
        with self._clip_lock:
            waiting = self._clip_wait.get(action.peer)
            if waiting is not None:
                waiting.append(action)
                return
            if isinstance(action, owner_module.SendClipboard):
                waiting = self._clip_wait[action.peer] = collections.deque()
                try:
                    self._on_pasteboard_locked(lambda peer=action.peer: self._clipboard_then_rest(peer, waiting))
                except RuntimeError:
                    # No thread at shutdown: nothing must wait behind a read that never starts.
                    del self._clip_wait[action.peer]
                    raise
                return
        self._send(action)

    def _on_pasteboard(self, job):
        with self._clip_lock:
            self._on_pasteboard_locked(job)

    def _on_pasteboard_locked(self, job):
        """Under `_clip_lock`: `job` done after every pasteboard job before it, on the one thread
        that does them, started when there is none."""
        self._pasteboard_jobs.append(job)
        if self._pasteboard_busy:
            return
        try:
            threading.Thread(target=self._pasteboard_worker, name="Beamer-clipboard", daemon=True).start()
        except RuntimeError:
            self._pasteboard_jobs.pop()
            raise
        self._pasteboard_busy = True

    def _pasteboard_worker(self):
        while True:
            with self._clip_lock:
                if not self._pasteboard_jobs:
                    self._pasteboard_busy = False
                    return
                job = self._pasteboard_jobs.popleft()
            try:
                job()
            except Exception:
                self.logger.exception("pasteboard work failed")

    def _clipboard_then_rest(self, peer, waiting):
        """The clipboard for `peer`, then what `waiting` held behind it, in order. `waiting` belongs to
        the link it was made on: once that link is lost (`_forget_held`) none of it is sent, as the
        machine that reconnects will be taken afresh. A clipboard asked for again takes its turn
        among the pasteboard's other work, behind any write decided before it."""
        try:
            self._send_clipboard(peer, waiting)
        except Exception:
            self.logger.exception("the clipboard could not be sent")
        while True:
            with self._clip_lock:
                if self._clip_wait.get(peer) is not waiting:
                    return
                if not waiting:
                    del self._clip_wait[peer]
                    return
                action = waiting.popleft()
                if isinstance(action, owner_module.SendClipboard):
                    self._on_pasteboard_locked(lambda: self._clipboard_then_rest(peer, waiting))
                    return
            try:
                self._send(action)
            except Exception:
                self.logger.exception("a message held behind the clipboard could not be sent")

    def _drop(self, peer):
        """A hand-over given up before its answer: that machine's link is closed, which ends any
        ownership a late accept began there, and it reconnects (WIRE.md section 5)."""
        self._forget_held(peer)
        link = self._peers_up.get(peer)
        if link is not None:
            link.drop(owner_module.DROPPED)

    def _forget_held(self, peer):
        """`peer`'s link is lost: what waited for its clipboard is for a link that is gone."""
        with self._clip_lock:
            self._clip_wait.pop(peer, None)

    def _send(self, send):
        link = self._peers_up.get(send.peer)
        if link is None:
            return
        message = send.message
        if message.get("type") == gestures.CHORD:
            receiver = "mac" if keytable.is_mac(link.peer_platform) else "windows"
            for part in gestures.chord_for(message["data"]["action"], receiver):
                if not link.send_input(part):
                    if link.live():
                        link.drop("The outbound queue filled")
                    return
            return
        if message.get("type") in protocol.INPUT_TYPES:
            message = self._for_peer(message, link)
            if message is None:
                return
            sent = link.send_input(message)
        else:
            sent = link.post(message)
        if not sent and link.live():
            link.drop("The outbound queue filled")

    def _key_style(self):
        return config_module.key_map_style(self.cfg.key_map) or self.cfg.key_map

    def _for_peer(self, message, link):
        """A captured input message as the peer it is going to names it: modifiers by the key
        table (WIRE.md section 7), and media keys only to a peer that acts on them."""
        if message.get("type") not in (protocol.MSG_KEYDOWN, protocol.MSG_KEYUP):
            return message
        data = dict(message["data"])
        key = data.get("key")
        if key in MEDIA_KEY_NAMES:
            return message if "media_keys" in link.caps else None
        data["key"] = keytable.wire_name(key, OWN_PLATFORM, link.peer_platform, self._key_style())
        return {"type": message["type"], "data": data}

    def _on_moved(self, moved):
        if moved.to is not None:
            if not self.redirecting:
                self.redirect_started_at = self.clock()
                self.redirecting = True
                self.cursor_pin_point = self._capture_cursor_pin_point()
                self._set_cursor_follows_mouse(False)
                self.pointer.hide()
            self._last_peer = moved.to
            self.logger.info("redirecting input to %s", self._name_of(moved.to))
            return
        self._local_cleanup()
        why = WHY_TEXT.get(moved.why)
        if why is not None:
            text = why.format(name=self._name_of(moved.left))
            self.logger.warning("%s; input returned to this Mac", text)
            self._alert("Beamer", text)
        if moved.why == "switch":
            self._arrive(moved.edge, moved.offset)

    def _arrive(self, edge, offset):
        """The other machine sent input back: land the pointer where it left, `edge` being the edge
        of this Mac to arrive at and `offset` the fraction along it. Without an edge the pointer
        stays where it was and the arrival says so."""
        crossed = edge in crossing.EDGES and isinstance(offset, (int, float)) and not isinstance(offset, bool)
        point = None
        if crossed:
            try:
                point = crossing.CrossingEngine.arrival_point(
                    edge, offset, self._current_desktop_bounds(), displays=self._current_displays())
            except Exception:
                self.logger.exception("failed to place the pointer on arrival")
            else:
                self._warp_cursor_to(point)
        # `pin` is where the pointer landed, for the arrival effect; None when it was not placed.
        self._notify_crossing("arrive", crossing.Step(mac_edge=edge if edge in crossing.EDGES else None, pin=point))

    def _on_unreachable(self, unreachable):
        name = self._name_of(unreachable.peer)
        why = unreachable.why
        if why == "unreachable" and self._accepts.get(unreachable.peer) is False:
            why = "not_allowed"
        text = WHY_TEXT.get(why, "{name} could not be reached").format(name=name)
        self.logger.warning("%s", text)
        self._alert("Beamer", text)

    # The clipboard (WIRE.md section 5)

    def _clipboard_stamp_now(self):
        stamp = getattr(self.clipboard, "change_stamp", None)
        if stamp is None:
            return None
        try:
            return stamp()
        except Exception:
            self.logger.exception("could not read the clipboard's change stamp")
            return None

    def _note_clipboard(self):
        """Before this Mac takes a peer: if the clipboard changed by its owner's hand since this
        Mac last wrote it from a peer, no peer holds what it holds now."""
        stamp = self._clipboard_stamp_now()
        if stamp != self._clipboard_stamp:
            self._clipboard_stamp = stamp
            with self._owner_lock:
                self.owner.clipboard_changed()

    def _send_clipboard(self, peer, waiting):
        """Read this Mac's clipboard and send it as a clipboard message. Text over CLIPBOARD_MAX_BYTES
        and an image protocol.png_fits refuses are each dropped on their own, so an oversized
        screenshot still lets its text through; with nothing left nothing is sent. Nothing is sent
        either when `waiting` was dropped with its link while the pasteboard was read."""
        if self._peers_up.get(peer) is None:
            return
        text, image = self.clipboard.get_contents()
        with self._clip_lock:
            if self._clip_wait.get(peer) is not waiting:
                return
        link = self._peers_up.get(peer)
        if link is None:
            return
        if text and len(text.encode("utf-8")) > protocol.CLIPBOARD_MAX_BYTES:
            self.logger.warning("local clipboard text is too large; skipping the text")
            text = None
        if image is not None and not protocol.png_fits(image):
            self.logger.warning("local clipboard image (%d bytes) is past what a clipboard may carry; skipping the image", len(image))
            image = None
        if not text and image is None:
            self.logger.debug("local clipboard is empty; not sending it")
            return
        link.post(protocol.clipboard_msg(text or None, image))

    def _set_clipboard(self, message):
        read = protocol.read_clipboard(message)
        if read is None or (read["text"] is None and read["image"] is None):
            return
        if not self.clipboard.set_contents(read["text"], read["image"]):
            self.logger.warning("failed to set the local clipboard from an inbound message")
        self._clipboard_stamp = self._clipboard_stamp_now()

    # The outbound worker

    def _outbound_worker(self):
        while not self.stop_event.is_set():
            message = None
            try:
                try:
                    message = self.outbound.get(timeout=0.05)
                except queue.Empty:
                    self._owner_tick()
                    continue
                try:
                    self._process_outbound(message)
                finally:
                    self.outbound.task_done()
                self._owner_tick()
            except Exception:
                # One bad message or tick must not end the only thread that drains this queue:
                # nothing restarts it, and the tap would keep swallowing input into a queue nobody reads.
                self._force_local(f"outbound message {message.get('type')!r} failed" if message else "the owner's timer failed")
                self.logger.exception("outbound worker recovered")

    def _owner_tick(self):
        deadline = self.owner.deadline()
        if deadline is not None and self.clock() >= deadline:
            self._step(lambda owner: owner.tick())

    def _process_outbound(self, message):
        """Handle one dequeued item: input the tap captured, which the owner routes (to the machine
        it is on, held for a hand-over, or dropped when it has already come home)."""
        kind = message.get("type")
        if kind in (protocol.MSG_KEYUP, protocol.MSG_MOUSEUP):
            # Whether or not the input is away: the release of a key pressed there that arrives
            # after it came home is what lets the owner stop swallowing it.
            self._step(lambda owner: owner.input(message))
            return
        if not self.redirecting:
            return
        self._step(lambda owner: owner.input(message) if owner.away else [])

    def _force_local(self, reason):
        self._return_local()
        self.logger.error("%s; input forced local", reason)

    def _connection_failed(self, reason):
        was_redirecting = self.redirecting
        link = self._peers_up.get(self.owner.on) or self._primary_link
        self._return_local()
        self.connection_status = reason
        if link is not None:
            link.drop(reason)
        self.logger.error("%s; connection dropped and input forced local", reason)
        if was_redirecting:
            self._alert("Beamer", "Input returned to this Mac")

    def _drain_outbound(self):
        while True:
            try:
                self.outbound.get_nowait()
            except queue.Empty:
                return
            else:
                self.outbound.task_done()
