"""The fakes the Mac app's controller tests share: a Quartz that records, a clock that stands
still, a clipboard that never touches the pasteboard, and the Config a controller is made from."""

import base64
import logging
import time

import config
from core import protocol

class FakeQuartz:
    kCGEventKeyDown = 10
    kCGEventKeyUp = 11
    kCGEventFlagsChanged = 12
    kCGEventMouseMoved = 5
    kCGEventLeftMouseDragged = 6
    kCGEventRightMouseDragged = 7
    kCGEventOtherMouseDragged = 27
    kCGEventLeftMouseDown = 1
    kCGEventLeftMouseUp = 2
    kCGEventRightMouseDown = 3
    kCGEventRightMouseUp = 4
    kCGEventOtherMouseDown = 25
    kCGEventOtherMouseUp = 26
    kCGEventScrollWheel = 22
    kCGEventTapDisabledByTimeout = 0xFFFFFFFE
    kCGEventTapDisabledByUserInput = 0xFFFFFFFF
    kCGEventSourceUserData = 42
    kCGKeyboardEventKeycode = 9
    kCGKeyboardEventAutorepeat = 8
    kCGMouseEventDeltaX = 4
    kCGMouseEventDeltaY = 5
    kCGMouseEventButtonNumber = 3
    kCGScrollWheelEventDeltaAxis1 = 11
    kCGScrollWheelEventDeltaAxis2 = 12
    kCGScrollWheelEventIsContinuous = 13
    kCGScrollWheelEventPointDeltaAxis1 = 96
    kCGScrollWheelEventPointDeltaAxis2 = 97
    kCGEventFlagMaskAlphaShift = 1 << 16
    kCGEventFlagMaskShift = 1 << 17
    kCGEventFlagMaskControl = 1 << 18
    kCGEventFlagMaskAlternate = 1 << 19
    kCGEventFlagMaskCommand = 1 << 20

    @staticmethod
    def CGEventGetIntegerValueField(event, field):
        return event.get(field, 0)

    @staticmethod
    def CGEventGetDoubleValueField(event, field):
        return float(event.get(field, 0))

    @staticmethod
    def CGEventGetFlags(event):
        return event.get("flags", 0)

    @staticmethod
    def CGEventKeyboardGetUnicodeString(event, maximum, length, characters):
        value = event.get("unicode", "")
        return len(value.encode("utf-16-le")) // 2, value

    # -- Cursor pinning (Bug 1: belt-and-braces warp) --------------------
    # Call lists are class-level so every KVMController built against
    # FakeQuartz in a test shares them; tests that care about counts reset
    # these in setUp, matching how InjectScrollDispatchTests (win_app) resets
    # its own module-level accumulator.
    cursor_pin_location = (111.0, 222.0)
    associate_calls = []
    warp_calls = []
    tap_enable_calls = []

    @classmethod
    def reset_cursor_spies(cls):
        cls.associate_calls = []
        cls.warp_calls = []
        cls.tap_enable_calls = []

    @staticmethod
    def CGEventCreate(source):
        return {"_fake_probe_event": True}

    @classmethod
    def CGEventGetLocation(cls, event):
        return event.get("location", cls.cursor_pin_location)

    @classmethod
    def CGAssociateMouseAndMouseCursorPosition(cls, follows):
        cls.associate_calls.append(follows)
        return 0

    @classmethod
    def CGWarpMouseCursorPosition(cls, point):
        cls.warp_calls.append(point)
        return 0

    @classmethod
    def CGEventTapEnable(cls, tap, enabled):
        cls.tap_enable_calls.append((tap, enabled))


class FakeGestureEvent:
    """Stands in for bridge.py's `_GestureEventView` (the object a converted
    NSEvent is wrapped in): exposes exactly the plain attribute surface
    GestureTranslator reads, with no AppKit involved."""

    def __init__(self, magnification=0.0, deltaX=0.0, deltaY=0.0, phase=0):
        self.magnification = magnification
        self.deltaX = deltaX
        self.deltaY = deltaY
        self.phase = phase


class FakeClock:
    def __init__(self, value=10.0):
        self.value = value

    def __call__(self):
        return self.value


# A token the way pairing makes one: the b64 of 32 bytes, so a real link would dial under it.
PAIRED_TOKEN = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")


def make_config(token="synthetic-token"):
    return config.Config(
        host="192.0.2.10",
        port=51820,
        auth_token=token,
        trigger_key="alt_r",
        double_tap_ms=300,
        key_map=dict(config.DEFAULT_KEY_MAP),
        reconnect_interval_s=0.1,
    )


def crossing_config(token="synthetic-token", **overrides):
    cfg = make_config(token)
    cfg.crossing = dict(config.DEFAULT_CROSSING, **overrides)
    return cfg


def settle(controller, timeout=2.0):
    """Wait until the pasteboard work the controller started is done, and what waited behind it sent."""
    deadline = time.monotonic() + timeout
    while (controller._clip_wait or controller._pasteboard_busy) and time.monotonic() < deadline:
        time.sleep(0.002)
    assert not (controller._clip_wait or controller._pasteboard_busy), "pasteboard work never finished"


def quiet_logger():
    logger = logging.getLogger("kvm-bridge-tests")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


FAKE_PNG = protocol.PNG_SIGNATURE + b"\x00" * 64


class FakeClipboard:
    """Drop-in replacement for clipboard_mac, injected via
    KVMController(clipboard=...) so tests never touch the real macOS
    pasteboard."""

    def __init__(self, text=None, image=None):
        self.text = text
        self.image = image
        self.set_calls = []
        self.stamp = 1

    def change_stamp(self):
        return self.stamp

    def copy(self, text):
        """The person copies something: a change by their own hand."""
        self.text = text
        self.stamp += 1

    def get_contents(self):
        return self.text, self.image

    def changed_contents(self):
        return self.text, self.image

    def forget_sync(self):
        pass

    def set_contents(self, text, image):
        self.set_calls.append((text, image))
        self.text, self.image = text, image
        self.stamp += 1
        return True


