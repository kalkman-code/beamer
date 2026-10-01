"""Translation of macOS trackpad gestures into wire messages.

Two numberings share the values 29-32 and must not be confused:

At the CGEvent tap (bridge.py), measured on macOS 26.6.2:

    29  Gesture      -- everything two-finger, discriminated by field 110
    30  DockControl  -- a three/four-finger system swipe or thumb-and-three
                        pinch, the events Mission Control, Spaces, Show
                        desktop and Launchpad are driven by

At the AppKit level (kvm_bridge_app.py's overlay panel), the NSEventType
raw values:

    29  NSEventTypeGesture      (reserved; not translated)
    30  NSEventTypeMagnify      (pinch-to-zoom)
    31  NSEventTypeSwipe        (two-finger swipe between pages)
    32  NSEventTypeSmartMagnify (reserved; not translated)

`DockSwipeClassifier` handles the tap's DockControl events and emits one
`gesture` message per completed movement. `GestureTranslator` handles the
overlay's NSEvents, turning a pinch into Ctrl+wheel and a page swipe into
Alt+Arrow with no Windows-side change. A zoom gesture was never observed to
reach the tap, so the overlay is the only route for it, and it remains
best-effort.

Everything here is defensive: an unreadable field is logged once and treated
as "no gesture" rather than raised, so a bug or a changed private field on a
future macOS can never take down keyboard/mouse capture. Neither class needs
AppKit or Quartz -- `ns_event` only has to expose `.magnification`,
`.deltaX`, `.deltaY` and `.phase`, and dock `fields` only `.hid`, `.motion`,
`.phase`, `.progress` and `.velocity` -- which keeps the tests free of both.
"""

import logging

from core import protocol

GESTURE_TYPE = 29
MAGNIFY_TYPE = 30
DOCK_CONTROL_TYPE = 30
SWIPE_TYPE = 31
SMART_MAGNIFY_TYPE = 32

GESTURE_EVENT_TYPES = frozenset({GESTURE_TYPE, MAGNIFY_TYPE, SWIPE_TYPE, SMART_MAGNIFY_TYPE})

# Private CGEvent fields on a DockControl event (numbers from iss, verified by
# probe on macOS 26.6.2): 110 IOHID type, 123 motion axis, 132 phase,
# 124 cumulative signed progress, 129/130 velocity on the ended event.
DOCK_FIELD_HID_TYPE = 110
DOCK_FIELD_MOTION = 123
DOCK_FIELD_PHASE = 132
DOCK_FIELD_PROGRESS = 124
DOCK_FIELD_VELOCITY_X = 129
DOCK_FIELD_VELOCITY_Y = 130

DOCK_SWIPE_HID_TYPE = 23
MOTION_HORIZONTAL = 1
MOTION_VERTICAL = 2
MOTION_PINCH = 3
DOCK_PHASE_ENDED = 4

# A full swipe ends between |0.3| and |0.9|; below this it was a twitch. A
# discrete flick can end with progress still near zero, in which case the
# ended event's velocity carries the direction instead, as iss found.
SWIPE_PROGRESS_THRESHOLD = 0.15

# (name when the sign is negative, name when positive), per motion axis.
# Measured on macOS 26.6.2. iss records the horizontal sign flipping in
# macOS 26 and again in 27, so the table is keyed by major version and an
# unknown version takes the newest entry. The 27 row is iss's report, not a
# measurement here.
_SIGN_TABLES = {
    26: {
        MOTION_VERTICAL: (protocol.GESTURE_SWIPE_UP, protocol.GESTURE_SWIPE_DOWN),
        MOTION_HORIZONTAL: (protocol.GESTURE_SWIPE_RIGHT, protocol.GESTURE_SWIPE_LEFT),
        MOTION_PINCH: (protocol.GESTURE_SPREAD, protocol.GESTURE_PINCH),
    },
    27: {
        MOTION_VERTICAL: (protocol.GESTURE_SWIPE_UP, protocol.GESTURE_SWIPE_DOWN),
        MOTION_HORIZONTAL: (protocol.GESTURE_SWIPE_LEFT, protocol.GESTURE_SWIPE_RIGHT),
        MOTION_PINCH: (protocol.GESTURE_SPREAD, protocol.GESTURE_PINCH),
    },
}


def sign_table_for(macos_major):
    known = sorted(_SIGN_TABLES)
    if macos_major in _SIGN_TABLES:
        return _SIGN_TABLES[macos_major]
    return _SIGN_TABLES[known[-1]]


class DockSwipeClassifier:
    """Turns the ended event of a DockControl gesture into one gesture
    message. Stateless on purpose: began/changed events carry nothing the
    ended one does not, and a swipe that straddles the moment redirecting
    turns on is not worth tracking."""

    def __init__(self, macos_major=26, logger=None):
        self.logger = logger or logging.getLogger("Beamer")
        self.signs = sign_table_for(macos_major)
        self._warned = False

    def translate(self, fields):
        try:
            if int(fields.hid) != DOCK_SWIPE_HID_TYPE:
                return []
            if int(fields.phase) != DOCK_PHASE_ENDED:
                return []
            names = self.signs.get(int(fields.motion))
            if names is None:
                return []
            value = float(fields.progress)
            if abs(value) < SWIPE_PROGRESS_THRESHOLD:
                value = float(fields.velocity)
            if value == 0.0:
                return []
            return [protocol.gesture_msg(names[0] if value < 0 else names[1])]
        except Exception:
            if not self._warned:
                self._warned = True
                self.logger.debug("failed to read a DockControl event; ignoring", exc_info=True)
            return []

# NSEventPhase bits (AppKit.h). Read defensively via getattr(..., 0): a
# converted event is not guaranteed to expose `.phase` meaningfully (or at
# all) on every macOS version.
NS_EVENT_PHASE_ENDED = 8
NS_EVENT_PHASE_CANCELLED = 16
_PHASE_RESET_MASK = NS_EVENT_PHASE_ENDED | NS_EVENT_PHASE_CANCELLED

# Accumulated `.magnification` needed before one Ctrl+wheel zoom step fires.
# Positive magnification (fingers spreading -- zooming in) maps to a
# positive wheel delta, matching the usual "Ctrl+scroll up zooms in"
# convention in browsers and other zoomable apps.
MAGNIFY_STEP = 0.05


RECEIVERS = ("windows", "mac")


class GestureTranslator:
    """`receiver` is the platform of the machine being driven: a zoom or a page swipe is the
    chord that machine answers to. The wire's key names are the receiver's own words, so a Mac's
    "cmd" is Command and Windows' "ctrl" is Control."""

    def __init__(self, logger=None, receiver="windows"):
        if receiver not in RECEIVERS:
            raise ValueError(f"unknown receiver platform: {receiver!r}")
        self.receiver = receiver
        self.logger = logger or logging.getLogger("Beamer")
        self._magnify_accum = 0.0
        self._warned_types = set()

    def wants(self, event_type):
        return event_type in GESTURE_EVENT_TYPES

    def translate(self, event_type, ns_event):
        """Return a (possibly empty) list of wire-message dicts for one
        gesture event. Never raises: any failure reading `ns_event` is
        logged once per event type and treated as no gesture."""
        try:
            if event_type == MAGNIFY_TYPE:
                return self._translate_magnify(ns_event)
            if event_type == SWIPE_TYPE:
                return self._translate_swipe(ns_event)
            # Gesture (29) and SmartMagnify (32) are reserved -- no
            # translation defined for them yet.
            return []
        except Exception:
            self._warn_once(event_type)
            return []

    def _warn_once(self, event_type):
        if event_type in self._warned_types:
            return
        self._warned_types.add(event_type)
        self.logger.debug(
            "failed to read gesture event type %r; ignoring", event_type, exc_info=True
        )

    def _translate_magnify(self, ns_event):
        self._magnify_accum += float(ns_event.magnification)
        messages = []
        while abs(self._magnify_accum) >= MAGNIFY_STEP:
            direction = 1 if self._magnify_accum > 0 else -1
            messages.extend(self._zoom_step_messages(direction))
            self._magnify_accum -= direction * MAGNIFY_STEP
        phase = int(getattr(ns_event, "phase", 0) or 0)
        if phase & _PHASE_RESET_MASK:
            self._magnify_accum = 0.0
        return messages

    def _zoom_step_messages(self, direction):
        if self.receiver == "mac":
            return _chord("cmd", "=" if direction > 0 else "-")
        return [
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "ctrl"}},
            protocol.scroll_msg(dy=direction, dx=0, mode="line"),
            {"type": protocol.MSG_KEYUP, "data": {"key": "ctrl"}},
        ]

    def _translate_swipe(self, ns_event):
        delta_x = float(ns_event.deltaX)
        if delta_x == 0:
            return []
        back = delta_x > 0
        if self.receiver == "mac":
            return _chord("cmd", "[" if back else "]")
        return _chord("alt", "left" if back else "right")


def _chord(modifier, key):
    # A character key names where it sits on a US keyboard, so a receiving Mac whose layout has
    # no such key still presses a key under the modifier rather than typing the character.
    data = {"key": key, "us": key} if len(key) == 1 else {"key": key}
    return [
        {"type": protocol.MSG_KEYDOWN, "data": {"key": modifier}},
        {"type": protocol.MSG_KEYDOWN, "data": dict(data)},
        {"type": protocol.MSG_KEYUP, "data": dict(data)},
        {"type": protocol.MSG_KEYUP, "data": {"key": modifier}},
    ]
