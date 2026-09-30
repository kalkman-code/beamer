import collections
import errno
import logging
import platform
import queue
import socket
import struct
import threading
import time
import types
import dataclasses
from dataclasses import dataclass

import objc
import Quartz

import clipboard_mac
import crossing
import desktop_mac
from input_injector_mac import INJECTED_MARK
import gestures
from core import ignored
import keyboard_layout
import media_keys
from core import protocol
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


ACK_TIMEOUT_SECONDS = 2.0
CONNECT_TIMEOUT_SECONDS = 1.0
AUTH_TIMEOUT_SECONDS = 2.0
SOCKET_IO_TIMEOUT_SECONDS = 0.5
OUTBOUND_QUEUE_SIZE = 2048
SSH_FALLBACK_HOST = "127.0.0.1"
# Below 49152 for the same reason the listening ports are: macOS hands out
# 49152-65535 itself, and a forward whose local port is taken never comes up.
SSH_FALLBACK_PORT = 24822
PING_INTERVAL_SECONDS = 1.0
DESKTOP_BOUNDS_MAX_AGE_SECONDS = 1.0
CROSSING_RETRY_SECONDS = 30.0
ROUND_TRIP_SAMPLES = 8
ROUND_TRIP_MAX_AGE_SECONDS = 5.0
# A receiver that accepts the TCP connection but never sends a preamble is, in practice, one
# running the pre-v4 cleartext protocol: it sits waiting for a hello frame it will never get.
OLD_RECEIVER_STATUS = "Windows did not answer the handshake — it is probably running an older Beamer; update it"
AUTH_FAILED_STATUS = "Windows could not be authenticated — check the shared token matches on both sides"

# Messages that must bypass both the outbound gate (they still need to go
# out when redirecting has just turned off, e.g. the switch-back focus
# message) and sequence assignment (like ping, they carry no `seq` and are
# never recorded by the receiver's ProcessedSequence). The sentinel is a
# purely local marker -- never sent on the wire -- that the outbound worker
# expands into a real clipboard message (or drops) at send time.
CONTROL_MESSAGE_TYPES = frozenset({protocol.MSG_FOCUS, protocol.MSG_CLIPBOARD, protocol.MSG_ARRANGEMENT})
_LOCAL_CLIPBOARD_SENTINEL_TYPE = "_local_clipboard"
_GATE_EXEMPT_TYPES = CONTROL_MESSAGE_TYPES | {_LOCAL_CLIPBOARD_SENTINEL_TYPE}


class _HandshakeError(protocol.ProtocolError):
    """Raised for a handshake that completed but was rejected; carries the
    exact user-facing status so the generic OSError/ProtocolError handler
    doesn't wrap it with a less specific message."""

    def __init__(self, status):
        super().__init__(status)
        self.status = status


def connection_error_message(exc):
    error_number = getattr(exc, "errno", None)
    if error_number in {51, 64, 65}:
        if "Beamer Tunnel.command" in str(exc):
            return "macOS 27 blocked direct LAN access. Open /Applications/Beamer Tunnel.command."
        return "Windows is unreachable from this Mac. Check that both devices can communicate on the local network."
    if error_number == 61:
        return "Windows is reachable, but Beamer is not listening on this port."
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "Windows did not answer before the connection timed out."
    return f"Connection failed: {exc}"


class FrameDecoder:
    """Reassembles and opens frames from the raw chunks the reader thread
    receives. One per connection, like the session it opens frames with."""

    def __init__(self, session):
        self.session = session
        self.buffer = bytearray()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        messages = []
        while len(self.buffer) >= protocol.HEADER_SIZE:
            length = struct.unpack_from(">I", self.buffer)[0]
            if length < 1 or length > protocol.MAX_FRAME_BYTES:
                raise protocol.ProtocolError(f"invalid message length: {length}")
            frame_length = protocol.HEADER_SIZE + length
            if len(self.buffer) < frame_length:
                break
            body = bytes(self.buffer[protocol.HEADER_SIZE:frame_length])
            del self.buffer[:frame_length]
            messages.append(self.session.open(body))
        return messages


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


class KVMController:
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
        self._cursor_assoc_status_logged = False
        self._cursor_warp_error_logged = False
        self.redirecting = False
        # True while the PC is driving this Mac over the other link. The tap
        # passes everything through while it is, except this Mac's own trigger
        # key, so the two directions can never both own the keyboard. Set by
        # the app that owns both halves, which also hands in `send_peer_home`:
        # the receiver's way of sending the PC's input back to it.
        self.receiving = False
        self.send_peer_home = None
        self.stop_event = threading.Event()
        self.outbound = queue.Queue(maxsize=OUTBOUND_QUEUE_SIZE)
        self.socket_lock = threading.RLock()
        self.sequence_lock = threading.Lock()
        self.sock = None
        # Replaced by every _connect_once, so each connection gets a fresh nonce prefix and
        # counters that start from zero. Sealing happens on the outbound, watchdog and reader
        # threads only -- the event tap enqueues and never touches it.
        self.session = protocol.SecureSession(cfg.auth_token)
        self.last_sent_seq = 0
        self.last_ack_seq = -1
        self.last_ack_at = 0.0
        self.connected_at = 0.0
        self.last_send_at = 0.0
        self.redirect_started_at = 0.0
        self.on_user_alert = None
        # Set by the app: called when Windows says the two machines have moved
        # in relation to each other.
        self.on_arrangement = None
        # Same on both machines, as the receiver's: `on_settings(data)` for each settings message
        # from Windows, `announce()` for what to send the moment the link is up, `peer_settings`
        # whether Windows's welcome said it keeps settings in step.
        self.on_settings = None
        self.announce = lambda: []
        self.peer_settings = None
        self.threads = []
        self.started = False
        self.capture_lock = threading.Lock()
        self.capture_thread = None
        self.input_error = None
        self.connection_status = "Waiting to connect"
        # The host, port and token that last completed a handshake. Silence after the preamble
        # from a PC that has answered this run is a PC too busy to answer (Windows stalls every
        # app for half a minute while it reconfigures its displays), not an older Beamer. The
        # token is part of it so a re-pair, even to another PC at the same address, starts clean.
        self.answered = None
        # Set while Windows reports it is clearing its lock screen. Beamer
        # cannot type on the secure desktop, so Windows unlocks first and
        # drops input meanwhile; without this the switch just looks dead for
        # the few seconds that takes.
        self.windows_locked = False
        self._status_before_unlock = None
        self.tap_ready = threading.Event()
        self.event_tap = None
        self.tap_source = None
        self.tap_run_loop = None
        self.tap_callback_ref = self._event_tap_callback
        self.translator = QuartzEventTranslator(quartz)
        self.trigger_code = KEY_NAME_TO_CODE[cfg.trigger_key]
        self.last_trigger_down = 0.0
        self.trigger_suppressed = False
        self.ignore_gate = ignored.Gate(cfg.ignored_inputs)
        # Crossing: the engine is pure and runs on the event-tap thread; the
        # desktop bounds come from CoreGraphics (already in the top-left
        # global space CGEventGetLocation uses, so nothing is flipped) and are
        # cached because they are read on every local mouse move. The notch
        # range needs NSScreen, so the app measures it on the main thread and
        # assigns it here. on_crossing is fed (kind, step) off the tap and
        # ack threads for haptics and the glow; the app marshals it.
        # A PC whose address changed, after a router restart hands it a new lease, still beacons
        # under its name. With `discovery` set, a failed connect tries the address the paired PC's
        # name is heard at, and only once that address has passed the authenticated handshake does
        # it become cfg.host and reach `on_host_learned(host)` on the connection thread, for the app
        # to save. A spoofed beacon costs one failed attempt and changes nothing.
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
        # Round trip: (seq, sent at) for every input event still unacknowledged, oldest first,
        # and the last few measured trips. Both under sequence_lock.
        self._unacked_sent_at = collections.deque()
        self._round_trips = collections.deque(maxlen=ROUND_TRIP_SAMPLES)
        self._round_trip_at = 0.0

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
        with self.socket_lock:
            return self.sock is not None

    @property
    def input_ready(self):
        return self.event_tap is not None and self.input_error is None

    @property
    def round_trip_ms(self):
        """The smoothed input round trip, or None whenever a figure would not be honest: input
        is not on Windows, the link is down, or nothing fresh has been acknowledged for a few
        seconds. The upper median of the last few trips, so an even count rounds towards the
        slower one. The receiver acknowledges on a timer rather than per event, so each trip
        includes however long the last event waited for that timer -- pessimistic by design."""
        if not self.redirecting or not self.connected:
            return None
        with self.sequence_lock:
            trips = sorted(self._round_trips)
            measured_at = self._round_trip_at
        if not trips or self.clock() - measured_at > ROUND_TRIP_MAX_AGE_SECONDS:
            return None
        return int(round(trips[len(trips) // 2] * 1000))

    def start(self):
        if self.started:
            return
        self.started = True
        workers = (
            ("connection", self._connection_worker),
            ("outbound", self._outbound_worker),
            ("ack-receiver", self._ack_worker),
            ("ack-watchdog", self._watchdog_worker),
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
        self.stop_event.set()
        self._drop_connection()
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
        self.cfg = cfg
        self.trigger_code = KEY_NAME_TO_CODE[cfg.trigger_key]
        self.last_trigger_down = 0.0
        self.trigger_suppressed = False
        self.translator.modifier_down.clear()
        self.translator.printable_down.clear()
        self.translator.reset_mouse_accumulators()
        self.ignore_gate.configure(cfg.ignored_inputs)
        self.crossing = crossing.CrossingEngine.from_config(cfg.crossing)
        self._crossing_failed = False
        self._drop_connection()
        self._drain_outbound()
        self.connection_status = "Settings saved; reconnecting"
        self.logger.info("settings updated; redirect mode returned to local")

    def apply_settings(self, cfg):
        """Everything that does not change which PC this is or how it is reached, applied without
        touching the link: the trigger, key map, crossing and how it looks and feels. The settings
        window calls this as each control changes; update_config is for a new address or token,
        which has to reconnect."""
        was = self.cfg.crossing.get("edge") if self.cfg is not None else None
        self.cfg = cfg
        edge = cfg.crossing.get("edge")
        if edge != was and edge in crossing.EDGES:
            # The arrangement is one value both machines hold. Changing it
            # here is a change for Windows too, so it travels the moment it
            # changes rather than waiting for the next reconnect.
            self.send_arrangement(edge, cfg.crossing.get("arrangement_set_at", 0))
        self.trigger_code = KEY_NAME_TO_CODE[cfg.trigger_key]
        self.ignore_gate.configure(cfg.ignored_inputs)
        self.crossing = crossing.CrossingEngine.from_config(cfg.crossing)
        self._crossing_failed = False

    def set_redirecting(self, value, edge=None, offset=None, came_home=True):
        """`edge` and `offset` are the Windows edge and fraction along it a
        crossing arrives at; absent for the shortcut, when Windows leaves its
        pointer where it is. The way home is sent on every switch.

        Input coming home this way is a switch, the shortcut, the menu or the
        PC sending it back, and says so through on_crossing as "home" with
        where the pointer is, for the arrival that shows it. `came_home` is
        False for the two ways back that show their own: a crossing that lands
        here, and the PC taking this Mac over."""
        value = bool(value)
        if value == self.redirecting:
            return False
        if value:
            if not self.connected:
                self.logger.warning("cannot redirect: the Windows receiver is not connected")
                self._alert("Beamer", f"Cannot switch — {self.connection_status}")
                return False
            if self.receiving:
                # The PC is driving this Mac over the other link, so switching
                # to Windows means sending the PC's input home: the one way
                # back that does not depend on the PC or on the return edge.
                send_home = self.send_peer_home
                if send_home is not None and send_home():
                    return True
                self.logger.warning("cannot redirect: Windows is driving this Mac and cannot be reached")
                return False
            if not self.cfg.send_to_windows:
                self.logger.info("cannot redirect: sending this Mac's input to Windows is switched off")
                return False
            self.redirect_started_at = self.clock()
            self.redirecting = True
            self.cursor_pin_point = self._capture_cursor_pin_point()
            self._set_cursor_follows_mouse(False)
            if not self.redirecting:
                # A link failure on another thread went local in the gap
                # above; its reassociation must not be the one overwritten.
                self._set_cursor_follows_mouse(True)
                self.cursor_pin_point = None
                return False
            # The sentinel is expanded into a real clipboard message (or
            # dropped) by the outbound worker at send time, not here -- this
            # runs on the event-tap thread and must never block on reading
            # the pasteboard.
            self._enqueue_control({"type": _LOCAL_CLIPBOARD_SENTINEL_TYPE, "data": {}})
            self._enqueue_control(
                protocol.focus_msg(
                    "windows",
                    edge=edge,
                    offset=offset,
                    return_edge=edge or self.crossing.home_edge(),
                    resistance_px=int(self.crossing.resistance_px),
                )
            )
            self.logger.info("redirecting input to Windows")
            return True
        self._return_local()
        self.logger.info("input returned to this Mac")
        if came_home:
            pin = self._capture_cursor_pin_point()
            if pin is not None:
                self._notify_crossing("home", crossing.Step(pin=(pin[0], pin[1])))
        return True

    def _return_local(self):
        """Every way input comes back to this Mac ends here: the switch home,
        a dropped link, a crashed worker, a disabled tap, a settings change.
        The cursor association is the part that must not be skipped -- a
        path that only cleared `redirecting` left the pointer decoupled from
        the hand, frozen on screen while the keyboard already worked. Windows
        is told too, while the link is up: it keeps treating this Mac as
        driving, with whatever keys it was holding still down, until it hears
        otherwise. On a dead link the receiver hands back by itself."""
        was_redirecting = self.redirecting
        self.redirecting = False
        self._set_cursor_follows_mouse(True)
        self.cursor_pin_point = None
        self.translator.reset_mouse_accumulators()
        self.ignore_gate.reset()
        self.crossing.reset()
        self._forget_round_trips()
        if was_redirecting:
            # Queued after redirecting has already flipped False: only reaches
            # the wire because the outbound gate exempts control messages.
            self._enqueue_control(protocol.focus_msg("mac"))

    def set_receiving(self, value):
        """The PC has taken input on this Mac, or given it back. Input is
        returned to this Mac's own hardware first: whatever the PC is about to
        do with the pointer, it must not read as a push against the edge."""
        value = bool(value)
        if value == self.receiving:
            return
        if value and self.redirecting:
            self.set_redirecting(False, came_home=False)
        self.receiving = value
        self.crossing.reset()

    def _enqueue_control(self, message):
        """Enqueue a control message (focus, or the local-clipboard sentinel)
        from set_redirecting, which runs on the event-tap thread rather than
        the outbound worker. Best-effort: a momentarily full queue drops the
        message (logged) instead of blocking the tap thread."""
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
                    result = self.translator.key_result(event_type, event, self.trigger_code, self.cfg.key_map)
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
                    self.cfg.key_map,
                )
                if result.is_trigger:
                    return self._handle_trigger(result, event)
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
            else:
                if not self.redirecting:
                    return self._handle_local_mouse(event_type, event)
                button = self.translator.button_of(event_type, event)
                if button is not None and self.ignore_gate.keeps(ignored.button(button[0]), button[1]):
                    return event
                messages = self.translator.mouse_messages(event_type, event)
                if button is not None and not messages:
                    # A button the wire has no name for stays on this Mac, as one does on the PC,
                    # rather than being swallowed and reaching neither.
                    return event
            if not self.redirecting:
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
        if self.crossing_paused or self.full_screen_app is not None or not self.cfg.send_to_windows:
            return event
        if event_type not in self.translator.movement_event_types:
            self.crossing.reset()
            return event
        if not self.connected:
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
            )
        except Exception:
            self._crossing_failed = True
            self._crossing_failed_at = self.clock()
            self.crossing.reset()
            self.logger.exception("crossing failed; edge switching is off for %.0fs", CROSSING_RETRY_SECONDS)
            return event
        if step.crossed:
            if self.receiving:
                # The PC is driving this Mac and this Mac's own pointer pushed through: the PC's
                # input goes home first, over its own link, and this Mac's follows it across. The
                # PC's focus home clears receiving here too, later, and finds it already clear.
                send_home = self.send_peer_home
                if send_home is None or not send_home():
                    self.logger.warning("cannot cross: Windows is driving this Mac and cannot be reached")
                    return event
                self.receiving = False
            if self.set_redirecting(True, edge=step.edge, offset=step.offset):
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

    def _handle_switch(self, data):
        """Windows asked for input back, having pushed through its return
        edge. Stop redirecting first, then land the pointer where it left
        Windows: `edge` is the Mac edge to arrive at and `offset` the fraction
        along it. A malformed or unknown request is ignored; a switch that
        arrives while already local still gets the haptic, since the pointer
        genuinely came home."""
        if not isinstance(data, dict) or data.get("target") != "mac":
            return
        edge = data.get("edge")
        offset = data.get("offset")
        crossed = edge in crossing.EDGES and isinstance(offset, (int, float)) and not isinstance(offset, bool)
        # Without an edge the PC sent this Mac's input home by its own switch: that is a switch
        # arriving here, and shows where the pointer is.
        self.set_redirecting(False, came_home=not crossed)
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
        wire_name = self.cfg.key_map.get(name, name)
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

    def _connection_worker(self):
        while not self.stop_event.is_set():
            if not self.connected and self._config_ready():
                if not self._connect_once():
                    self._follow_the_pc()
            self.stop_event.wait(self.cfg.reconnect_interval_s)

    def _follow_the_pc(self):
        """Try the address the paired PC is beaconing from now, if it is not the saved one."""
        discovery, cfg = self.discovery, self.cfg
        if discovery is None or not cfg.pc_name:
            return
        moved = next((pc["address"] for pc in discovery.pcs()
                      if pc["name"] == cfg.pc_name and pc["port"] == cfg.port and pc["address"] != cfg.host), None)
        if moved is None or not self._connect_once(host=moved):
            return
        self.logger.info("%s moved from %s to %s", cfg.pc_name, cfg.host, moved)
        self.cfg = dataclasses.replace(self.cfg, host=moved)
        if self.on_host_learned is not None:
            try:
                self.on_host_learned(moved)
            except Exception:
                self.logger.exception("saving the PC's new address failed")

    def _config_ready(self):
        return bool(self.cfg.host and self.cfg.auth_token and 1 <= self.cfg.port <= 65535)

    def _connect_once(self, host=None):
        sock = None
        fallback = False
        # Read once: update_config can replace self.cfg from the AppKit thread mid-handshake.
        endpoint = (host or self.cfg.host, self.cfg.port, self.cfg.auth_token)
        # The tunnel leads to the saved address, so a new one is tried directly or not at all.
        tunnel_allowed = host is None
        host, port = endpoint[0], endpoint[1]
        self.connection_status = f"Connecting to {host}:{port}"
        try:
            try:
                sock = self.socket_factory(
                    (host, port),
                    timeout=CONNECT_TIMEOUT_SECONDS,
                )
            except OSError as exc:
                if exc.errno != errno.EHOSTUNREACH or not tunnel_allowed:
                    raise
                sock = self._connect_via_ssh_fallback()
                fallback = True
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            session = protocol.SecureSession(endpoint[2])
            sock.sendall(session.preamble())
            sock.settimeout(AUTH_TIMEOUT_SECONDS)
            try:
                protocol.recv_preamble(sock, session)
            except socket.timeout:
                if self.answered == endpoint:
                    raise _HandshakeError("Windows stopped responding") from None
                raise _HandshakeError(OLD_RECEIVER_STATUS) from None
            except protocol.VersionMismatch as exc:
                raise _HandshakeError(
                    f"Windows receiver speaks Beamer protocol v{exc.peer_version}, this Mac v{protocol.PROTOCOL_VERSION} — update both apps"
                ) from None
            # The way home travels in the hello as well as on every switch,
            # so Windows knows which of its edges leads back here before this
            # Mac has ever crossed -- which is what lets the PC push its own
            # pointer out across that same border first.
            protocol.send_msg(
                sock,
                session,
                protocol.hello_msg(
                    return_edge=self.crossing.home_edge(),
                    resistance_px=int(self.crossing.resistance_px),
                ),
            )
            try:
                reply = protocol.recv_msg(sock, session)
            except protocol.ConnectionClosed:
                # The receiver closes without a word when the first frame fails to authenticate.
                raise _HandshakeError(AUTH_FAILED_STATUS) from None
            except protocol.AuthenticationError:
                raise _HandshakeError(AUTH_FAILED_STATUS) from None
            reply_type = reply.get("type")
            if reply_type != protocol.MSG_WELCOME:
                raise protocol.ProtocolError("receiver did not confirm authentication")
            welcome_data = reply.get("data")
            if not isinstance(welcome_data, dict):
                raise protocol.ProtocolError("welcome message missing data")
            error = welcome_data.get("error")
            if error == "version_mismatch":
                raise _HandshakeError(OLD_RECEIVER_STATUS)
            if error:
                raise _HandshakeError(f"Windows rejected the connection: {error}")
            if welcome_data.get("version") != protocol.PROTOCOL_VERSION:
                raise _HandshakeError(OLD_RECEIVER_STATUS)
            self.peer_settings = protocol.keeps_settings(welcome_data)
            sock.settimeout(SOCKET_IO_TIMEOUT_SECONDS)
        except _HandshakeError as exc:
            self.redirecting = False
            self.connection_status = exc.status
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            self.logger.warning("connection to Windows failed: %s", exc.status)
            return False
        except protocol.ConnectionClosed:
            self.redirecting = False
            self.connection_status = "Windows closed the connection during the handshake."
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            self.logger.warning(self.connection_status)
            return False
        except (OSError, protocol.ProtocolError) as exc:
            self.redirecting = False
            self.connection_status = connection_error_message(exc)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            self.logger.warning("connection to Windows failed: %s", exc)
            return False
        with self.socket_lock:
            if self.stop_event.is_set() or self.sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
                return False
            self.sock = sock
            self.session = session
        self.clipboard.forget_sync()
        now = self.clock()
        with self.sequence_lock:
            self.last_ack_seq = -1
            self.last_ack_at = now
        self.connected_at = now
        self.last_send_at = now
        self.answered = endpoint
        if fallback:
            self.connection_status = "Connected to Windows via secure macOS 27 fallback"
            self.logger.info("connected to Windows through the SSH fallback")
        else:
            self.connection_status = f"Connected to {host}:{port}"
            self.logger.info("connected to Windows at %s:%s", host, port)
        try:
            announcements = list(self.announce())
        except Exception:
            self.logger.exception("announce failed")
            announcements = []
        for announcement in announcements:
            self._send_raw(announcement)
        return True

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

    def _outbound_worker(self):
        while not self.stop_event.is_set():
            try:
                message = self.outbound.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._process_outbound(message)
            except Exception:
                # One bad message must not end the only thread that drains
                # this queue: nothing restarts it, and the tap would keep
                # swallowing input into a queue nobody reads.
                self._force_local(f"outbound message {message.get('type')!r} failed")
                self.logger.exception("outbound worker recovered")
            finally:
                self.outbound.task_done()

    def _process_outbound(self, message):
        """Handle one dequeued outbound item. Control messages (focus,
        clipboard, and the local-clipboard sentinel) are exempt from the
        `not redirecting` gate -- otherwise the switch-back focus message
        would be dropped, since set_redirecting(False) closes the gate
        before this worker gets to dequeue it -- and bypass sequence
        assignment entirely, the same way ping does via _send_raw."""
        message_type = message.get("type")
        if message_type not in _GATE_EXEMPT_TYPES and not self.redirecting:
            return
        if message_type == _LOCAL_CLIPBOARD_SENTINEL_TYPE:
            self._send_local_clipboard()
            return
        if message_type in CONTROL_MESSAGE_TYPES:
            self._send_raw(message)
            return
        prepared = self._prepare_outbound(message)
        self._send_prepared(prepared)

    def _send_local_clipboard(self):
        """Expand the local-clipboard sentinel: read this Mac's clipboard and
        send it as a clipboard message, unless it is unchanged since it was
        last sent or written from the PC. Text over CLIPBOARD_MAX_BYTES and an
        image over CLIPBOARD_IMAGE_MAX_BYTES are each dropped on their own,
        so an oversized screenshot still lets its text through; with nothing
        left the message is skipped and the focus message still goes out."""
        text, image = self.clipboard.changed_contents()
        if text and len(text.encode("utf-8")) > protocol.CLIPBOARD_MAX_BYTES:
            self.logger.warning("local clipboard text is too large; skipping the text")
            text = None
        if image is not None and len(image) > protocol.CLIPBOARD_IMAGE_MAX_BYTES:
            self.logger.warning(
                "local clipboard image is %d bytes, over the %d cap; skipping the image",
                len(image),
                protocol.CLIPBOARD_IMAGE_MAX_BYTES,
            )
            image = None
        if not text and image is None:
            self.logger.debug("local clipboard is empty or unchanged since the last sync; not sending it")
            return
        self._send_raw(protocol.clipboard_msg(text or None, image))

    def _prepare_outbound(self, message):
        with self.sequence_lock:
            self.last_sent_seq += 1
            seq = self.last_sent_seq
        data = dict(message.get("data", {}))
        data["seq"] = seq
        return {"type": message["type"], "data": data}

    def _send_prepared(self, message):
        with self.socket_lock:
            sock = self.sock
            if sock is None:
                self._connection_failed("connection disappeared before an input event could be sent")
                return False
            try:
                protocol.send_msg(sock, self.session, message)
                self.last_send_at = self.clock()
            except (OSError, protocol.ProtocolError) as exc:
                self._connection_failed(f"send failed: {exc}", expected_socket=sock)
                return False
            with self.sequence_lock:
                self._unacked_sent_at.append((message["data"]["seq"], self.last_send_at))
            return True

    def _send_raw(self, message):
        """Serialize and send `message` directly under socket_lock, bypassing
        the outbound queue and its redirecting gate. Used for the idle
        heartbeat ping, which must go out whether or not input is redirected."""
        with self.socket_lock:
            sock = self.sock
            if sock is None:
                return False
            try:
                protocol.send_msg(sock, self.session, message)
                self.last_send_at = self.clock()
                return True
            except (OSError, protocol.ProtocolError) as exc:
                self._connection_failed(f"send failed: {exc}", expected_socket=sock)
                return False

    def _ack_worker(self):
        active_socket = None
        decoder = None
        while not self.stop_event.is_set():
            with self.socket_lock:
                sock = self.sock
                session = self.session
            if sock is None:
                active_socket = None
                decoder = None
                self.stop_event.wait(0.1)
                continue
            if sock is not active_socket:
                active_socket = sock
                decoder = FrameDecoder(session)
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                self._connection_failed(f"ACK receive failed: {exc}", expected_socket=sock)
                active_socket = None
                continue
            if not chunk:
                self._connection_failed("Windows closed the connection", expected_socket=sock)
                active_socket = None
                continue
            try:
                messages = decoder.feed(chunk)
                for message in messages:
                    self._handle_inbound(message)
            except protocol.AuthenticationError:
                self._connection_failed(AUTH_FAILED_STATUS, expected_socket=sock)
                active_socket = None
            except protocol.ProtocolError as exc:
                self._connection_failed(f"invalid ACK stream: {exc}", expected_socket=sock)
                active_socket = None
            except Exception as exc:
                # The same outcome as a malformed frame: this connection goes,
                # the thread stays, and the next connection gets a reader.
                self.logger.exception("inbound handling failed")
                self._connection_failed(f"inbound message failed: {exc}", expected_socket=sock)
                active_socket = None

    def _handle_inbound(self, message):
        """Dispatch one inbound message. `ack` updates the sequence bookkeeping
        the watchdog relies on; any other well-formed message (a dict with a
        known "type" string) still refreshes liveness — bytes arriving at all
        prove the connection is alive — and is otherwise ignored, except for
        `clipboard`, which is applied to the local pasteboard. Malformed
        frames still drop the connection, since they mean the stream can no
        longer be trusted."""
        if message.get("type") == protocol.MSG_ACK:
            self._record_ack(message)
            return
        message_type = message.get("type")
        if not isinstance(message_type, str) or not message_type:
            raise protocol.ProtocolError(f"malformed inbound message: {message!r}")
        with self.sequence_lock:
            self.last_ack_at = self.clock()
        if message_type == protocol.MSG_CLIPBOARD:
            self._apply_inbound_clipboard(message.get("data", {}))
            return
        if message_type == protocol.MSG_SWITCH:
            self._handle_switch(message.get("data", {}))
            return
        if message_type == protocol.MSG_ARRANGEMENT:
            self._handle_arrangement(message.get("data", {}))
            return
        if message_type == protocol.MSG_SETTINGS:
            data = message.get("data")
            if isinstance(data, dict) and self.on_settings is not None:
                try:
                    self.on_settings(data)
                except Exception:
                    self.logger.exception("settings handler failed")
            return
        self.logger.debug("ignoring inbound message of type %r", message_type)

    def _handle_arrangement(self, data):
        """Windows changed which edge of this Mac leads to it. Handed to the
        app, which owns the settings file; nothing is applied here."""
        read = protocol.read_arrangement(data)
        if read is None or self.on_arrangement is None:
            return
        try:
            self.on_arrangement(*read)
        except Exception:
            self.logger.exception("arrangement handler failed")

    def send_arrangement(self, mac_edge, set_at):
        """Tell Windows where the machines are, when the change was made here.
        Sent straight out rather than queued: it is not input, and it must go
        whether or not input is currently redirected."""
        return self._send_raw(protocol.arrangement_msg(mac_edge, int(set_at)))

    def send_settings(self, data):
        """Tell Windows this Mac's settings state, straight out like the arrangement."""
        return self._send_raw(protocol.settings_msg(data))

    def _apply_inbound_clipboard(self, data):
        if not isinstance(data, dict):
            return
        text = data.get("text")
        if not isinstance(text, str) or not text:
            text = None
        elif len(text.encode("utf-8")) > protocol.CLIPBOARD_MAX_BYTES:
            self.logger.warning("ignoring oversized inbound clipboard text")
            text = None
        image = protocol.clipboard_image(data)
        if text is None and image is None:
            return
        if not self.clipboard.set_contents(text, image):
            self.logger.warning("failed to set the local clipboard from an inbound message")

    def _record_ack(self, message):
        if message.get("type") != protocol.MSG_ACK:
            raise protocol.ProtocolError(f"unexpected inbound message type: {message.get('type')!r}")
        data = message.get("data")
        if not isinstance(data, dict):
            raise protocol.ProtocolError("ACK data must be a JSON object")
        seq = data.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise protocol.ProtocolError("ACK seq must be a non-negative integer")
        with self.sequence_lock:
            if seq < self.last_ack_seq:
                raise protocol.ProtocolError("ACK seq moved backwards")
            if seq > self.last_sent_seq:
                raise protocol.ProtocolError("ACK seq is ahead of the sender")
            advanced = seq > self.last_ack_seq
            self.last_ack_seq = seq
            self.last_ack_at = self.clock()
            if advanced:
                self._measure_round_trip(seq, self.last_ack_at)
        self._note_windows_lock(data.get("locked") is True)

    def _measure_round_trip(self, seq, now):
        """Under sequence_lock. Measured from the send of the event the ACK names, not the
        oldest one it covers: the receiver acknowledges the highest seq it has processed on a
        timer, so the oldest would only ever measure that timer. An ACK that repeats a seq --
        every idle heartbeat does -- finds nothing left to measure and adds no sample."""
        sent_at = None
        while self._unacked_sent_at and self._unacked_sent_at[0][0] <= seq:
            sent_seq, at = self._unacked_sent_at.popleft()
            if sent_seq == seq:
                sent_at = at
        if sent_at is not None:
            self._round_trips.append(now - sent_at)
            self._round_trip_at = now

    def _forget_round_trips(self):
        with self.sequence_lock:
            self._unacked_sent_at.clear()
            self._round_trips.clear()
            self._round_trip_at = 0.0

    def _note_windows_lock(self, locked):
        """Surface Windows clearing its lock screen, on the transitions only:
        an ACK arrives every 400ms and must not restate the status each time.
        The previous status is restored verbatim rather than rebuilt, so the
        macOS 27 SSH-fallback wording survives an unlock."""
        if locked == self.windows_locked:
            return
        self.windows_locked = locked
        if locked:
            self._status_before_unlock = self.connection_status
            self.connection_status = "Unlocking Windows…"
            self.logger.info("Windows is on the lock screen; it is unlocking itself")
        else:
            if self._status_before_unlock is not None:
                self.connection_status = self._status_before_unlock
                self._status_before_unlock = None

    def _watchdog_worker(self):
        while not self.stop_event.wait(0.1):
            self._watchdog_tick()

    def _watchdog_tick(self):
        """Runs at all times a socket exists, not just while redirecting, so a
        half-open connection can't hide behind an idle UI: it is detected and
        torn down the same way whether or not input is currently redirected.
        Also drives the idle heartbeat ping so a silent connection is proven
        alive well before the peer's own read timeout could fire."""
        with self.socket_lock:
            sock = self.sock
        if sock is None:
            return False
        now = self.clock()
        with self.sequence_lock:
            last_heartbeat = max(self.last_ack_at, self.connected_at)
        if now - last_heartbeat > ACK_TIMEOUT_SECONDS:
            self._connection_failed("Windows stopped responding")
            return True
        if now - self.last_send_at >= PING_INTERVAL_SECONDS:
            self._send_raw(protocol.ping_msg())
        return False

    def _force_local(self, reason):
        self._return_local()
        self.logger.error("%s; input forced local", reason)

    def _connection_failed(self, reason, expected_socket=None):
        was_redirecting = self.redirecting
        self._return_local()
        self.connection_status = reason
        self._drop_connection(expected_socket)
        self.logger.error("%s; connection dropped and input forced local", reason)
        if was_redirecting:
            self._alert("Beamer", "Input returned to this Mac")

    def _drop_connection(self, expected_socket=None):
        with self.socket_lock:
            if expected_socket is not None and self.sock is not expected_socket:
                return
            sock = self.sock
            self.sock = None
        self._forget_round_trips()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _drain_outbound(self):
        while True:
            try:
                self.outbound.get_nowait()
            except queue.Empty:
                return
            else:
                self.outbound.task_done()
