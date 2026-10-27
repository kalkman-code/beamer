import argparse
import collections
import copy
import fcntl
import logging
import logging.handlers
import math
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import AppKit
import ApplicationServices
import Quartz
import objc
import rumps
from PyObjCTools import AppHelper

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge
from instance_signal import InstanceSignalServer, instance_socket_path, request_running_instance
import config as config_module
import crossing
from core import return_edge
import desktop_mac
from frame_clock import FrameClock, set_divisor
import dock_visibility
from core import crash_log
from core import effects
from core.i18n import matches_translation, LANGUAGES, N_, tr, trc, install
import effects_overlay
import identify_overlay
import gestures
import hardware_mac
import ignored_titles
import keyboard_layout
import notch_beam
import previews
import diagram
import ledger
import motion
from notch_beam import NotchBeam
from notch_island import NotchIsland
from bridge import _GestureEventView
from key_codes import KEY_NAME_TO_CODE
import link_state
import machines_panel
import login_item
import machines
import notices
import pages
from core import updates
from core import protocol
from core import pairing
from core import peerlist
from core import settings_sync
from core import ways as core_ways
from core import crossing_state
from core import profiles
from settings_store import (
    SettingsError,
    SettingsStore,
    config_to_raw,
    editable_default_config,
)
import theme
from wake import WakingController
import widgets
from windows_input import WindowsInput
from core.receiver import ServerState


LOG_DIRECTORY = Path.home() / "Library" / "Logs" / "Beamer"
LOG_PATH = LOG_DIRECTORY / "Beamer.log"
CAPTURE_RETRY_INTERVAL_SECONDS = 5.0
# The repo-root VERSION file is the one place the version is set; py2app copies it into Resources.
try:
    VERSION = (Path(os.environ.get("RESOURCEPATH") or Path(__file__).resolve().parent.parent) / "VERSION").read_text().strip()
except OSError:
    VERSION = "dev"
FULL_SCREEN_CHECK_INTERVAL_SECONDS = 1.0
# The pages switch between their two layouts at this width of the content pane, beside the sidebar.
WIDE_WIDTH = 600.0
# Accessibility description per menu-bar state. "held" is input on this Mac with the pointer ways
# in off, by a pause or a full-screen app.
MENU_BAR_STATES = {
    "local": N_("Input on this Mac"),
    "held": N_("Input held on this Mac; crossing is off"),
    "windows": N_("Input on another machine"),
}
CORNER_NAMES = {
    "top_left": N_("top left"), "top_right": N_("top right"),
    "bottom_left": N_("bottom left"), "bottom_right": N_("bottom right"),
}
# The menu's one Send input item, by the key rumps files it under, its first title.
TOGGLE_ITEM = "Send input"


def menu_bar_glyph(state):
    """Vernier's template glyph, 22 by 16pt: two screens, the filled one where input is, a chevron
    pointing at it for the way input went, and for crossing held a solid bar closing the gap
    between two outlines. Drawn in black and marked as a template, so macOS tints it for the bar."""

    def draw(_rect):
        AppKit.NSColor.blackColor().set()
        for x, filled in ((1.0, state == "local"), (13.0, state == "windows")):
            if filled:
                AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(((x, 2), (8, 12)), 1.5, 1.5).fill()
            else:
                outline = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(((x + 0.75, 2.75), (6.5, 10.5)), 1, 1)
                outline.setLineWidth_(1.5)
                outline.stroke()
        if state == "held":
            AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(((10, 0), (2, 16)), 1, 1).fill()
            return True
        tip, tail = (10.0, 12.0) if state == "local" else (12.0, 10.0)
        chevron = AppKit.NSBezierPath.bezierPath()
        chevron.moveToPoint_((tail, 5.5))
        chevron.lineToPoint_((tip, 8.0))
        chevron.lineToPoint_((tail, 10.5))
        chevron.setLineWidth_(1.4)
        chevron.setLineCapStyle_(AppKit.NSLineCapStyleRound)
        chevron.setLineJoinStyle_(AppKit.NSLineJoinStyleRound)
        chevron.stroke()
        return True

    image = AppKit.NSImage.imageWithSize_flipped_drawingHandler_((22, 16), True, draw)
    image.setTemplate_(True)
    image.setAccessibilityDescription_(tr(MENU_BAR_STATES[state]))
    return image


def configure_logging():
    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(LOG_DIRECTORY, 0o700)
    except OSError:
        pass
    # The handlers go on the root logger, not "Beamer": the shared modules --
    # receiver, return_edge, pairing -- log under their own names, and with
    # the handlers on "Beamer" everything the PC's input did on this Mac would
    # go nowhere, leaving a stranded mouse with no record to diagnose it by.
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    logger = logging.getLogger("Beamer")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH,
        maxBytes=1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
    return logger


def accessibility_granted():
    return bool(
        ApplicationServices.AXIsProcessTrustedWithOptions(
            {ApplicationServices.kAXTrustedCheckOptionPrompt: False}
        )
    )


def input_monitoring_granted():
    return bool(Quartz.CGPreflightListenEventAccess())


def acquire_instance_lock():
    lock_path = Path.home() / "Library" / "Application Support" / "Beamer" / "Beamer.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


class _GestureCaptureView(AppKit.NSView):
    """Content view for the invisible gesture-capture overlay panel (see
    GestureOverlay below).

    Bug 3: the CGEventTap-based gesture path (bridge.py's
    _handle_gesture_event, fed by private NSEventType 29-32) is best-effort
    and was observed to never fire at all on macOS 27 beta -- no "gesture
    capture active" log line ever appeared. macOS instead routes trackpad
    gestures to whichever window sits directly under the cursor through
    ordinary, fully-supported NSResponder methods. Since Bug 1's cursor pin
    keeps that location fixed while redirecting, placing this (otherwise
    invisible, input-transparent-looking but not actually
    ignoresMouseEvents) panel there lets it receive gestures the normal way
    -- and because this view consumes them, the local Mac gesture no longer
    fires either.

    NOTE: three/four-finger system gestures (Mission Control, Spaces,
    App Exposé) are consumed by the system compositor before any
    application-level responder -- including this one -- ever sees them.
    Nothing running as a normal app can capture those; they remain local to
    the Mac by OS design, regardless of this overlay.
    """

    def magnifyWithEvent_(self, event):
        self._forward(gestures.MAGNIFY_TYPE, event)

    def swipeWithEvent_(self, event):
        self._forward(gestures.SWIPE_TYPE, event)

    def smartMagnifyWithEvent_(self, event):
        # Reserved, like the tap path's SMART_MAGNIFY_TYPE -- overridden
        # only so this doesn't fall through to a default AppKit handler.
        pass

    @objc.python_method
    def _forward(self, event_type, event):
        controller = getattr(self, "controller", None)
        if controller is None:
            return
        try:
            controller.handle_overlay_gesture(event_type, _GestureEventView(event))
        except Exception:
            # handle_overlay_gesture already guards its own body; this is
            # one more layer of "never let a gesture crash the app".
            logging.getLogger("Beamer").exception("overlay gesture forwarding failed")


class GestureOverlay:
    """Owns the invisible NSPanel used to capture trackpad gestures while
    redirecting (see _GestureCaptureView above). Every method here touches
    AppKit and must only ever run on the main thread -- it is driven
    exclusively from TrayApp's periodic status-refresh timer (see
    refresh_status), which already runs there, so no locking is needed.

    Creation is attempted lazily, on first use, and any failure disables the
    overlay permanently for this run: gesture capture then simply falls back
    to whatever the (best-effort, possibly nonfunctional) event tap manages
    -- no crash, no regression versus today's behaviour.
    """

    SIZE = 400.0
    PANEL_ALPHA = 1.0 / 255.0

    def __init__(self, controller, logger):
        self.controller = controller
        self.logger = logger
        self.panel = None
        self.view = None
        self.visible = False
        self.disabled = False

    def sync(self):
        """Call on every status-refresh tick. Shows the panel, repositioned
        on the controller's current cursor pin point, while redirecting;
        hides it the rest of the time. A show can lag the redirect flip by
        up to one tick of that timer (currently 0.4s), which is acceptable."""
        if self.disabled:
            return
        try:
            if self.controller.redirecting:
                self._show()
            else:
                self._hide()
        except Exception:
            self.disabled = True
            self.logger.exception(
                "gesture-capture overlay failed; falling back to tap-only gesture capture"
            )

    def _ensure_panel(self):
        if self.panel is not None:
            return True
        style = (
            AppKit.NSWindowStyleMaskBorderless
            | AppKit.NSWindowStyleMaskNonactivatingPanel
        )
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, self.SIZE, self.SIZE),
            style,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        panel.setLevel_(AppKit.NSStatusWindowLevel)
        panel.setOpaque_(False)
        panel.setHasShadow_(False)
        # The faintest fill there is, one step in 255: a clear window lets events through to the app
        # beneath, and 0.02 darkened a 400-point square round the pointer enough to read as a grey box.
        panel.setBackgroundColor_(AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.0, self.PANEL_ALPHA))
        panel.setIgnoresMouseEvents_(False)
        panel.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces)
        view = _GestureCaptureView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, self.SIZE, self.SIZE)
        )
        view.controller = self.controller
        panel.setContentView_(view)
        self.panel = panel
        self.view = view
        return True

    def _show(self):
        self._ensure_panel()
        pin = self.controller.cursor_pin_point
        if pin is not None:
            half = self.SIZE / 2.0
            screen = AppKit.NSScreen.mainScreen()
            # CGEventGetLocation reports a top-left-origin Quartz point;
            # AppKit screen coordinates are bottom-left-origin. Flipping
            # against the main screen's height is only exactly right for an
            # un-rotated single-display setup, which is an acceptable
            # approximation for this best-effort overlay -- worst case it's
            # centered a bit off on an unusual multi-display arrangement,
            # not that gestures stop working.
            screen_height = screen.frame().size.height if screen is not None else pin.y * 2
            frame = AppKit.NSMakeRect(pin.x - half, screen_height - pin.y - half, self.SIZE, self.SIZE)
            self.panel.setFrame_display_(frame, False)
        # Never makeKey/makeMain -- this must not steal focus from whatever
        # the user is actually looking at.
        self.panel.orderFrontRegardless()
        self.visible = True

    def _hide(self):
        if self.panel is None or not self.visible:
            return
        self.panel.orderOut_(None)
        self.visible = False


def notch_x_range():
    """The notch's x-range in global points, or None on a Mac without one. Derived from the
    two auxiliary top areas rather than hard-coded, and only when the notched screen forms the
    top of the desktop, because the engine tests it against the top of the bounding box. Only x
    is read, and x is the one axis AppKit and Quartz agree on, so nothing is flipped."""
    screens = AppKit.NSScreen.screens()
    if not screens:
        return None
    desktop_top = max(screen.frame().origin.y + screen.frame().size.height for screen in screens)
    for screen in screens:
        frame = screen.frame()
        if frame.origin.y + frame.size.height != desktop_top:
            continue
        try:
            if screen.safeAreaInsets().top <= 0:
                continue
            left = screen.auxiliaryTopLeftArea()
            right = screen.auxiliaryTopRightArea()
        except AttributeError:
            continue
        if left is None or right is None or left.size.width <= 0 or right.size.width <= 0:
            continue
        return (left.origin.x + left.size.width, right.origin.x)
    return None


def notch_depth():
    """How far the notch reaches down from the top of the desktop, in points: the safe-area inset
    of the screen notch_x_range found it on."""
    return max((screen.safeAreaInsets().top for screen in AppKit.NSScreen.screens()
                if hasattr(screen, "safeAreaInsets")), default=0.0)


def menu_bar_height(display):
    """The height in points of the menu bar across the top of `display`, a Quartz (left, top,
    right, bottom), or 0 where it has none or hides itself: the gap between the screen's top and
    its visible frame's, which the Dock never takes."""
    screens = AppKit.NSScreen.screens()
    if not screens or display is None:
        return 0.0
    left, top, _right, _bottom = display
    primary_height = screens[0].frame().size.height
    for screen in screens:
        frame, visible = screen.frame(), screen.visibleFrame()
        screen_top = frame.origin.y + frame.size.height
        if abs(frame.origin.x - left) < 1 and abs(primary_height - screen_top - top) < 1:
            return max(0.0, screen_top - (visible.origin.y + visible.size.height))
    return 0.0


def full_screen_app():
    """The frontmost app's name while it is full screen, else None.

    The signal that works is Accessibility's `AXFullScreen` on the frontmost app's focused
    window. Beamer already holds Accessibility for its event tap, so this costs no new
    permission. Verified against Chrome driven by the OS's own Enter Full Screen
    shortcut: False windowed, True full screen, False again on leaving.

    ⚠ Three plausible signals were measured and do NOT work, so nobody should try them again:
    `currentSystemPresentationOptions()` stays 0, because that bit is for apps that request
    full-screen presentation explicitly rather than the green button; `NSMenu.menuBarVisible()`
    stays True throughout; and the window server reports a full-screen app's windows as
    fragments (1728x33, 1728x41, 1728x962 ...) rather than one window the size of the display,
    so a bounds comparison never matches. The bounds test is kept only as a fallback for a
    borderless full-screen game, which really is one window sized to the display and may not
    expose AXFullScreen at all.

    ⚠ Also measured: Chrome's AppleScript `fullscreen` property does not enter full screen, and
    believing it does produces a confident wrong answer. Drive the OS shortcut when testing.

    Main thread, once a second."""
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return None
    pid = app.processIdentifier()
    if pid == os.getpid():
        return None
    name = app.localizedName() or "An app"
    if _ax_full_screen(pid):
        return name
    error, displays, count = Quartz.CGGetActiveDisplayList(16, None, None)
    if error != 0:
        raise RuntimeError(f"CGGetActiveDisplayList failed: {error}")
    display_frames = []
    for display in list(displays)[:count]:
        rect = Quartz.CGDisplayBounds(display)
        display_frames.append((rect.origin.x, rect.origin.y, rect.size.width, rect.size.height))
    windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
    if _covers_a_display(windows or (), pid, display_frames):
        return name
    return None


def _ax_full_screen(pid) -> bool:
    """AXFullScreen on that process's focused window. False on any Accessibility error --
    a process with no focused window, or one that exposes no such attribute, is not full
    screen as far as crossing is concerned, and this must never raise into the tap's path."""
    try:
        element = ApplicationServices.AXUIElementCreateApplication(pid)
        error, window = ApplicationServices.AXUIElementCopyAttributeValue(
            element, "AXFocusedWindow", None)
        if error != 0 or window is None:
            return False
        error, value = ApplicationServices.AXUIElementCopyAttributeValue(
            window, "AXFullScreen", None)
        return error == 0 and bool(value)
    except Exception:
        return False


def _covers_a_display(windows, pid, display_frames):
    """Whether that process has an ordinary window sized to a whole display — the borderless
    full-screen game that exposes no AXFullScreen. Both halves of this are load-bearing.

    ⚠ Only layer 0. Finder's desktop window, WindowManager's wallpaper and the window server's
    backstop are all exactly display-sized, so without the layer test clicking the desktop put
    Finder frontmost and switched crossing off.

    ⚠ The window must start at the display's own top and match its full height. There used to be
    slack of the screen's safe-area inset here, for a game that sits under the menu bar; on a
    notched Mac that inset IS the menu bar height, so every ordinary zoomed window (0, 33,
    1728x1084 on this MacBook) matched it and crossing died the moment any other app came
    forward. A game that respects the safe area is indistinguishable from a zoomed window, and
    losing crossing daily is the worse of the two errors."""
    for info in windows:
        if info.get("kCGWindowOwnerPID") != pid or info.get("kCGWindowLayer") != 0:
            continue
        bounds = info.get("kCGWindowBounds") or {}
        for x, y, width, height in display_frames:
            if (
                abs((bounds.get("X") or 0) - x) <= 1
                and abs((bounds.get("Y") or 0) - y) <= 1
                and abs((bounds.get("Width") or 0) - width) <= 1
                and abs((bounds.get("Height") or 0) - height) <= 1
            ):
                return True
    return False


class Haptics:
    """Force Touch trackpad feedback for crossing. A Mac with a mouse or an older trackpad gets
    a performer that does nothing, and any failure switches haptics off for the run rather than
    ever reaching the crossing path."""

    def __init__(self, logger):
        self.logger = logger
        try:
            self.performer = AppKit.NSHapticFeedbackManager.defaultPerformer()
        except Exception:
            self.performer = None
            self.logger.exception("haptic feedback unavailable")

    def tick(self):
        """One click of the push. macOS offers three patterns that differ in meaning, not
        strength, and no intensity at all; level change is the one for discrete steps of pressure.
        Nothing is felt unless a finger is on the trackpad at that moment."""
        self._perform(AppKit.NSHapticFeedbackPatternLevelChange)

    def thud(self):
        """The breakthrough: two ticks close together, the one way to tell it from a step."""
        self._perform(AppKit.NSHapticFeedbackPatternLevelChange)
        AppHelper.callLater(0.06, self._perform, AppKit.NSHapticFeedbackPatternLevelChange)

    def _perform(self, pattern):
        if self.performer is None:
            return
        try:
            self.performer.performFeedbackPattern_performanceTime_(
                pattern, AppKit.NSHapticFeedbackPerformanceTimeNow
            )
        except Exception:
            self.performer = None
            self.logger.exception("haptic feedback failed; haptics off for this run")


class EdgeGlow:
    """A band along the edge being pushed, in `crossing.glow_colour`: the `glow` style's width and
    opacity track pressure, the `beam` style is a thin line with a comet travelling it that runs on
    off the end at breakthrough, and both flash when the push goes through. The same borderless non-activating panel
    as GestureOverlay, with two differences that matter: it ignores mouse events, or it would
    eat the very push it is drawing, and it joins full-screen spaces as an auxiliary window so
    it sits above a full-screen app.

    Main thread only, like GestureOverlay. Pressure changes arrive through `update`; a timer
    then redraws on each display refresh until both the pressure and the flash have faded, since nothing
    arrives from the tap once the push stops. Any failure disables the glow for the run."""

    dark_appearance = staticmethod(notch_beam.dark_appearance)

    FLASH_SECONDS = 0.18
    PREVIEW_SECONDS = 0.6
    # How far a corner's light reaches along each of its two walls.
    CORNER_ARM = 200.0
    # Over how much of its length a strip shorter than its display's edge, a third of it, fades in and
    # out at each end, rather than stopping dead where the strip does.
    TAPER = 72.0

    def __init__(self, controller, logger):
        self.controller = controller
        self.logger = logger
        self.panel = None
        self.fill = None
        self.comet = None
        # A corner's second wall, and each wall's fade along it, used only in a corner.
        self.side = None
        self.masks = None
        self.visible = False
        self.disabled = False
        self.mac_edge = None
        self.region = None
        self.flash_at = None
        self.finish = 0.0
        self.centre = -notch_beam.EDGE_COMET / 2.0
        self.drawn_at = None
        self.preview_level = 0.0
        self.preview_until = 0.0
        # The pressure of a push home while the PC drives, which the Mac's own engine never sees.
        self.level_now = None
        self.timer = FrameClock(None, lambda: self._tick(None))

    def update(self, kind, step):
        if self.disabled:
            return
        try:
            if kind == "cross":
                self.flash_at = time.monotonic()
                self.finish = 1.0
            elif kind not in ("pressure", "tick"):
                return
            elif not self.visible:
                self.centre = -notch_beam.EDGE_COMET / 2.0
            self.level_now = None
            self.mac_edge = step.mac_edge
            self.region = effects_overlay.strip_on_display(step.region, step.pin,
                                                          self.controller._current_desktop_bounds())
            self._draw()
        except Exception:
            self._fail()

    def driven(self, edge, pressure, crossed, cursor, corner=None):
        """The PC is driving this Mac and pushing at the way home through `edge`, or into `corner`,
        the pointer at `cursor`. The receiver reports pressure per movement, so it drains here at
        the engine's own rate, as the PC's glow drains the Mac's push through it."""
        if self.disabled:
            return
        try:
            now = time.monotonic()
            box = self.controller._current_desktop_bounds()
            if crossed:
                self.flash_at = now
                self.finish = 1.0
                self.level_now = lambda: 0.0
            else:
                if not self.visible:
                    self.centre = -notch_beam.EDGE_COMET / 2.0
                level = float(pressure)
                self.level_now = lambda: max(0.0, level - (time.monotonic() - now) / crossing.DECAY_S)
            self.mac_edge = edge
            display = effects_overlay.display_at(cursor, box)
            self.region = (effects_overlay.corner_box(corner, display) if corner is not None
                           else crossing.CrossingEngine._strip(edge, display))
            self._draw()
        except Exception:
            self._fail()

    def preview(self, mac_edge, level):
        """Lights the configured edge at `level` for a moment, for the resistance slider."""
        if self.disabled:
            return
        try:
            bounds = self.controller._current_desktop_bounds()
            self.mac_edge = mac_edge
            self.region = crossing.CrossingEngine._strip(mac_edge, bounds)
            self.preview_level = level
            self.preview_until = time.monotonic() + self.PREVIEW_SECONDS
            self._draw()
        except Exception:
            self._fail()

    def _fail(self):
        self.disabled = True
        self.logger.exception("edge glow failed; glow off for this run")
        try:
            self._hide()
        except Exception:
            pass

    def _tick(self, _timer):
        if self.disabled:
            self.timer.stop()
            return
        try:
            self._draw()
        except Exception:
            self._fail()
            self.timer.stop()

    def _draw(self):
        now = time.monotonic()
        elapsed = 0.0 if self.drawn_at is None else min(0.1, now - self.drawn_at)
        self.drawn_at = now
        feel = self.controller.cfg.crossing
        beam = feel["glow_style"] == "beam"
        pace = feel.get("effect_pace", effects.pace(feel.get("effect_length")))
        level = (self.level_now or self.controller.crossing_pressure_now)()
        if now < self.preview_until:
            level = max(level, self.preview_level)
        flash = 0.0
        if self.flash_at is not None:
            flash = max(0.0, 1.0 - (now - self.flash_at) / (self.FLASH_SECONDS * pace))
            if flash <= 0.0:
                self.flash_at = None
        comet = notch_beam.EDGE_COMET
        if self.finish > 0.0:
            # Run on off the end of the edge instead of stopping, and never wrap back to the start.
            self.centre = min(1.0 + comet, self.centre + elapsed / (notch_beam.EDGE_FINISH_TRAVERSE_S * pace))
            self.finish = max(0.0, self.finish - elapsed / (notch_beam.EDGE_FINISH_S * pace))
        elif level > 0.0:
            self.centre += elapsed / notch_beam.edge_traverse_seconds(level)
            if self.centre > 1.0 + comet / 2.0:
                self.centre = -comet / 2.0
        lingering = self.finish if beam else 0.0
        if (level <= 0.0 and flash <= 0.0 and lingering <= 0.0) or self.region is None or self.mac_edge is None:
            self._hide()
            self.timer.stop()
            self.drawn_at = None
            return
        self._ensure_panel()
        strength = max(level, flash, lingering)
        box = self.controller._current_desktop_bounds()
        display = effects_overlay.display_at((self.region[0] + self.region[2] / 2.0,
                                              self.region[1] + self.region[3] / 2.0), box)
        full_depth = effects.edge_depth(display[2] - display[0], display[3] - display[1],
                                        feel.get("effect_size", "medium"), feel.get("effect_size_fraction"))
        band = full_depth if beam else full_depth * (0.35 + 0.65 * max(level, flash))
        if self._corner() is not None:
            self._draw_corner(feel, beam, band, full_depth, level, flash, strength)
            return
        if self.side is not None:
            self.side.setHidden_(True)
        if self.mac_edge == "top":
            band = self._top_band(band, beam, display, full_depth)
        self.panel.setFrame_display_(self._band_frame(band), False)
        # Colour runs along the edge: top to bottom on a side, left to right along the top or bottom.
        start, end = ((0.5, 1.0), (0.5, 0.0)) if self.mac_edge in ("left", "right") else ((0.0, 0.5), (1.0, 0.5))
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        try:
            bounds = self.panel.contentView().bounds()
            for layer in (self.fill, self.comet):
                layer.setFrame_(bounds)
                layer.setStartPoint_(start)
                layer.setEndPoint_(end)
            self.fill.setColors_(notch_beam.palette_colours(feel["glow_colour"], dark=self.dark_appearance()))
            fade = self._taper()
            ends = (lambda at: 1.0) if fade is None else (lambda at: min(1.0, at / fade, (1.0 - at) / fade))
            notch_beam.mask_edge_falloff(self.comet, self.mac_edge, beam)
            if beam:
                stops = [index / 24 for index in range(25)]
                self.comet.setLocations_(stops)
                self.comet.setColors_([
                    AppKit.NSColor.colorWithWhite_alpha_(
                        1.0, notch_beam.edge_comet_alpha(at, self.centre, flash) * ends(at)).CGColor()
                    for at in stops
                ])
                self.fill.setMask_(self.comet)
                self.fill.setOpacity_(min(1.0, strength))
            else:
                self.comet.setLocations_([0.0, 1.0] if fade is None else [0.0, fade, 1.0 - fade, 1.0])
                self.comet.setColors_([AppKit.NSColor.colorWithWhite_alpha_(1.0, a).CGColor()
                                       for a in ((1.0, 1.0) if fade is None else (0.0, 1.0, 1.0, 0.0))])
                self.fill.setMask_(self.comet)
                self.fill.setOpacity_(min(1.0, 0.3 + 0.7 * level + 0.6 * flash))
        finally:
            Quartz.CATransaction.commit()
        if not self.visible:
            self.panel.orderFrontRegardless()
            self.visible = True
        if not getattr(self.timer, "is_alive", lambda: False)():
            self.timer.start()

    def _taper(self):
        """The fraction of the strip each end fades over when it is shorter than its display's edge,
        as a third of it is, else None."""
        box = self.controller._current_desktop_bounds()
        if box is None or self.region is None:
            return None
        x, y, width, height = self.region
        left, top, right, bottom = effects_overlay.display_at((x + width / 2.0, y + height / 2.0), box)
        length, full = (height, bottom - top) if self.mac_edge in ("left", "right") else (width, right - left)
        if length <= 0 or length >= full - 2:
            return None
        return min(0.25, self.TAPER / length)

    def _corner(self):
        """(corner name, the display's (left, top, right, bottom)) for a push into a corner's box,
        else None."""
        x, y, width, height = self.region
        if width > crossing.CORNER_PX or height > crossing.CORNER_PX:
            return None
        box = self.controller._current_desktop_bounds()
        if box is None:
            return None
        display = effects_overlay.display_at((x + width / 2.0, y + height / 2.0), box)
        return effects_overlay.corner_name(self.region, display), display

    def _top_band(self, band, beam, display, full_depth):
        """How deep the glow reaches down from the top of `display`. Over a menu bar it is scaled
        to fill the bar at full strength: the bar is about twice the band, so at the band's own
        depth it lit only the bar's top half, under the status items and the clock, and a corner's
        top arm read as a faint stripe beside the full side arm. The beam is a line and stays one."""
        bar = menu_bar_height(display)
        if beam or bar <= 0:
            return band
        if bar >= full_depth:
            return min(full_depth, band)
        return min(bar, band * bar / full_depth)

    def _draw_corner(self, feel, beam, band, full_depth, level, flash, strength):
        """The band along both of the corner's walls, brightest where they meet and fading out along
        each, so it reads as the corner rather than two edges; the beam's comet runs in along the top
        or bottom wall and out along the side. One gradient layer per wall, each masked by its fade."""
        name, (left, top, right, bottom) = self._corner()
        vertical, horizontal = name.split("_")
        arm = min(self.CORNER_ARM, right - left, bottom - top)
        x = left if horizontal == "left" else right - arm
        y = top if vertical == "top" else bottom - arm
        primary_height = AppKit.NSScreen.screens()[0].frame().size.height
        self.panel.setFrame_display_(AppKit.NSMakeRect(x, primary_height - (y + arm), arm, arm), False)
        view = self.panel.contentView()
        if self.side is None:
            self.side = Quartz.CAGradientLayer.layer()
            view.layer().addSublayer_(self.side)
            self.masks = (Quartz.CAGradientLayer.layer(), Quartz.CAGradientLayer.layer())
        colours = notch_beam.palette_colours(feel["glow_colour"], dark=self.dark_appearance())
        stops = [index / 24 for index in range(25)]
        fade = lambda u: max(0.0, 1.0 - u) ** 1.4
        # AppKit's layer space runs up from the bottom; u runs from the corner outward.
        at_top = vertical == "top"
        across = self._top_band(band, beam, (left, top, right, bottom), full_depth) if at_top else band
        walls = (
            (self.fill, self.masks[0], ((0.0, arm - across) if at_top else (0.0, 0.0), (arm, across)),
             ((1.0, 0.5), (0.0, 0.5)) if horizontal == "right" else ((0.0, 0.5), (1.0, 0.5)),
             lambda u: 0.5 * (1.0 - u)),
            (self.side, self.masks[1], ((arm - band, 0.0) if horizontal == "right" else (0.0, 0.0), (band, arm)),
             ((0.5, 1.0), (0.5, 0.0)) if at_top else ((0.5, 0.0), (0.5, 1.0)),
             lambda u: 0.5 + 0.5 * u),
        )
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        try:
            for wall, (layer, mask, frame, (start, end), path) in zip((vertical, horizontal), walls):
                layer.setHidden_(False)
                layer.setFrame_(frame)
                layer.setStartPoint_(start)
                layer.setEndPoint_(end)
                layer.setColors_(colours)
                mask.setFrame_(((0.0, 0.0), frame[1]))
                mask.setStartPoint_(start)
                mask.setEndPoint_(end)
                mask.setLocations_(stops)
                mask.setColors_([
                    AppKit.NSColor.colorWithWhite_alpha_(
                        1.0, fade(u) * (notch_beam.edge_comet_alpha(path(u), self.centre, flash) if beam else 1.0)
                    ).CGColor()
                    for u in stops
                ])
                notch_beam.mask_edge_falloff(mask, wall, beam)
                layer.setMask_(mask)
                layer.setOpacity_(min(1.0, strength) if beam else min(1.0, 0.3 + 0.7 * level + 0.6 * flash))
        finally:
            Quartz.CATransaction.commit()
        if not self.visible:
            self.panel.orderFrontRegardless()
            self.visible = True
        if not getattr(self.timer, "is_alive", lambda: False)():
            self.timer.start()

    def _band_frame(self, band):
        """The band grows inward from the strip the engine reported. Quartz rects are top-left
        origin and AppKit frames bottom-left; the flip is about the primary screen's height,
        which is exact for every arrangement because every screen is placed relative to it."""
        x, y, width, height = self.region
        edge = self.mac_edge
        if edge == "right":
            x, width = x + width - band, band
        elif edge == "left":
            width = band
        elif edge == "top":
            height = band
        else:
            y, height = y + height - band, band
        primary_height = AppKit.NSScreen.screens()[0].frame().size.height
        return AppKit.NSMakeRect(x, primary_height - (y + height), width, height)

    def _ensure_panel(self):
        if self.panel is not None:
            return
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, 1, 1),
            AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        panel.setLevel_(AppKit.NSStatusWindowLevel)
        panel.setOpaque_(False)
        panel.setHasShadow_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setIgnoresMouseEvents_(True)
        panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 1, 1))
        view.setWantsLayer_(True)
        panel.setContentView_(view)
        self.fill = Quartz.CAGradientLayer.layer()
        view.layer().addSublayer_(self.fill)
        self.comet = Quartz.CAGradientLayer.layer()
        self.panel = panel

    def _hide(self):
        if self.panel is None or not self.visible:
            return
        self.panel.orderOut_(None)
        self.visible = False


BEAMER_SITE_URL = "https://kalkmancode.co.uk/beamer"


def show_about_panel():
    """The standard About panel, with a centred credits line linking to the project's page. The
    version shown is Info.plist's own CFBundleShortVersionString, already the VERSION file's
    contents (see setup.py) -- nothing to pass here for that. Activates the app first: without it,
    a panel asked for from the menu-bar item alone can open behind everything else."""
    AppKit.NSApp.activateIgnoringOtherApps_(True)
    paragraph = AppKit.NSMutableParagraphStyle.alloc().init()
    paragraph.setAlignment_(AppKit.NSTextAlignmentCenter)
    credits = AppKit.NSAttributedString.alloc().initWithString_attributes_(
        "kalkmancode.co.uk/beamer",
        {
            AppKit.NSLinkAttributeName: AppKit.NSURL.URLWithString_(BEAMER_SITE_URL),
            AppKit.NSParagraphStyleAttributeName: paragraph,
        },
    )
    AppKit.NSApp.orderFrontStandardAboutPanelWithOptions_({AppKit.NSAboutPanelOptionCredits: credits})


def install_main_menu(control_window):
    """rumps builds a status-bar menu and no main menu at all, and Beamer is not a status-bar-only
    app: it shows a real window with text fields in it. Cmd+Q is not built into AppKit — it is the
    key equivalent of the Quit item in the application menu — so with no main menu the app could
    not be quit from the keyboard, and Cmd+C/V/A in the token field did nothing either."""
    def item(menu, title, action, key, modifiers=None, target=None):
        entry = menu.addItemWithTitle_action_keyEquivalent_(title, action, key)
        if modifiers is not None:
            entry.setKeyEquivalentModifierMask_(modifiers)
        if target is not None:
            entry.setTarget_(target)
        return entry

    main_menu = AppKit.NSMenu.alloc().init()

    application_item = main_menu.addItemWithTitle_action_keyEquivalent_("Beamer", None, "")
    application_menu = AppKit.NSMenu.alloc().initWithTitle_("Beamer")
    item(application_menu, tr("About Beamer"), "showAbout:", "", target=control_window)
    application_menu.addItem_(AppKit.NSMenuItem.separatorItem())
    item(application_menu, tr("Hide Beamer"), "hide:", "h")
    item(
        application_menu,
        tr("Hide Others"),
        "hideOtherApplications:",
        "h",
        AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagOption,
    )
    application_menu.addItem_(AppKit.NSMenuItem.separatorItem())
    item(application_menu, tr("Quit Beamer"), "quitApp:", "q", target=control_window)
    main_menu.setSubmenu_forItem_(application_menu, application_item)

    edit_item = main_menu.addItemWithTitle_action_keyEquivalent_(tr("Edit"), None, "")
    edit_menu = AppKit.NSMenu.alloc().initWithTitle_(tr("Edit"))
    item(edit_menu, tr("Undo"), "undo:", "z")
    item(
        edit_menu,
        tr("Redo"),
        "redo:",
        "z",
        AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagShift,
    )
    edit_menu.addItem_(AppKit.NSMenuItem.separatorItem())
    item(edit_menu, tr("Cut"), "cut:", "x")
    item(edit_menu, tr("Copy"), "copy:", "c")
    item(edit_menu, tr("Paste"), "paste:", "v")
    item(edit_menu, tr("Select All"), "selectAll:", "a")
    main_menu.setSubmenu_forItem_(edit_menu, edit_item)

    view_item = main_menu.addItemWithTitle_action_keyEquivalent_(tr("View"), None, "")
    view_menu = AppKit.NSMenu.alloc().initWithTitle_(tr("View"))
    for index, page in enumerate(pages.PAGES):
        title = tr(page[1])
        item(view_menu, title, "showPage:", str(index + 1), target=control_window).setTag_(index)
    main_menu.setSubmenu_forItem_(view_menu, view_item)

    window_item = main_menu.addItemWithTitle_action_keyEquivalent_(tr("Window"), None, "")
    window_menu = AppKit.NSMenu.alloc().initWithTitle_(tr("Window"))
    # No target, so these travel the responder chain to whichever window is key rather than
    # being bound to one. Closing is safe to offer because the control window is created with
    # setReleasedWhenClosed_(False) and the menu-bar item reopens it.
    item(window_menu, tr("Close"), "performClose:", "w")
    item(window_menu, tr("Minimise"), "performMiniaturize:", "m")
    main_menu.setSubmenu_forItem_(window_menu, window_item)
    AppKit.NSApp.setWindowsMenu_(window_menu)

    AppKit.NSApp.setMainMenu_(main_menu)


class FlippedView(AppKit.NSTableRowView):
    """A native flipped container avoids Python callbacks for every descendant's frame change.

    The row supplies native top-left coordinates; the settings column owns its layout.
    """


def window_fill_frame(window):
    screen = window.screen() or AppKit.NSScreen.mainScreen()
    return screen.visibleFrame() if screen else AppKit.NSMakeRect(0, 0, 1728, 1084)


def page_column_width(free):
    return max(0, min(880, free - 48))


def settings_normal_frame(saved, work):
    import math
    try:
        x, y, width, height = map(float, saved)
        if not all(math.isfinite(value) for value in (x, y, width, height)):
            raise ValueError
    except (TypeError, ValueError):
        width, height = 900, 640
        x = work.origin.x + (work.size.width - min(width, work.size.width)) / 2
        y = work.origin.y + (work.size.height - min(height, work.size.height)) / 2
    width = min(work.size.width, max(320, width))
    height = min(work.size.height, max(300, height))
    x = min(max(x, work.origin.x), work.origin.x + work.size.width - width)
    y = min(max(y, work.origin.y), work.origin.y + work.size.height - height)
    return x, y, width, height


def ordinary_window_frame(window):
    if (not window.isVisible() or window.isZoomed()
            or window.styleMask() & AppKit.NSWindowStyleMaskFullScreen
            or '"tilingState"' in window.stringWithSavedFrame()):
        return None
    frame = window.frame()
    return [frame.origin.x, frame.origin.y, frame.size.width, frame.size.height]


def titlebar_action(setting):
    value = (setting or "").strip().casefold()
    if value in {"fill", "zoom", "maximize"}:
        return "fill"
    if value in {"minimise", "minimize"}:
        return "minimise"
    if value in {"none", "do nothing"}:
        return None
    return "fill"


def perform_titlebar_double_click(window, setting=None):
    if setting is None:
        domain = AppKit.NSUserDefaults.standardUserDefaults().persistentDomainForName_("NSGlobalDomain") or {}
        setting = domain.get("AppleActionOnDoubleClick")
    action = titlebar_action(setting)
    if action == "fill":
        window.zoom_(None)
    elif action == "minimise":
        window.miniaturize_(None)


def perform_titlebar_drag(window, event):
    window.performWindowDragWithEvent_(event)


class TitlebarStrip(AppKit.NSView):
    def mouseDown_(self, event):
        if event.clickCount() == 1:
            perform_titlebar_drag(self.window(), event)


class SettingsWindow(AppKit.NSWindow):
    @objc.python_method
    def refresh_input_focus(self):
        responder = self.firstResponder()
        if isinstance(responder, widgets.Pressable):
            responder._ring(True)
        elif isinstance(responder, (widgets._RulerView, diagram.PushStrip, diagram.AtlasCanvas)):
            responder.refresh_keyboard_focus()
        delegate = self.delegate()
        if delegate is not None and hasattr(delegate, 'sidebar'):
            delegate.sidebar.refresh_focus()

    @objc.python_method
    def navigate_key_view(self, backwards, sender):
        self.keyboard_navigation = True
        sidebar = self.delegate().sidebar
        sidebar_rows = [values[1] for values in sidebar.rows.values()]
        previous = self.firstResponder()
        advance = (objc.super(SettingsWindow, self).selectPreviousKeyView_ if backwards
                   else objc.super(SettingsWindow, self).selectNextKeyView_)
        advance(sender)
        current = self.firstResponder()
        if previous == self or (current in sidebar_rows and previous not in sidebar_rows):
            self.makeFirstResponder_(sidebar.rows[sidebar.selected][1])
        elif previous in sidebar_rows:
            for _ in sidebar_rows:
                if self.firstResponder() not in sidebar_rows:
                    break
                advance(sender)
        self.refresh_input_focus()

    def selectNextKeyView_(self, sender):
        self.navigate_key_view(False, sender)

    def selectPreviousKeyView_(self, sender):
        self.navigate_key_view(True, sender)

    @objc.python_method
    def titlebar_background(self, point):
        content = self.contentView()
        height = content.bounds().size.height
        in_top = (0 <= point.x <= content.bounds().size.width
                  and height - 18 <= point.y <= self.frame().size.height)
        delegate = self.delegate()
        header = getattr(getattr(delegate, "sidebar", None), "header", None)
        in_header = header is not None and AppKit.NSPointInRect(
            header.convertPoint_fromView_(point, None), header.bounds()
        )
        if not (in_top or in_header):
            return False
        # Title text is a non-editable NSTextField. Traffic lights and other controls keep
        # their own clicks even when they lie inside one of the draggable backgrounds.
        hit = content.superview().hitTest_(point)
        while hit is not None:
            if isinstance(hit, (AppKit.NSButton, widgets.Pressable)):
                return False
            if isinstance(hit, AppKit.NSTextField) and hit.isEditable():
                return False
            hit = hit.superview()
        return True

    def sendEvent_(self, event):
        if event.type() == AppKit.NSEventTypeKeyDown and widgets.is_navigation_key(event):
            self.keyboard_navigation = True
        elif event.type() in (AppKit.NSEventTypeLeftMouseDown, AppKit.NSEventTypeRightMouseDown):
            self.keyboard_navigation = False
        self.refresh_input_focus()
        # Native titlebar and sidebar events never visit TitlebarStrip. All three
        # backgrounds must use the same preference fallback, once, on mouse-up.
        if (event.type() in (AppKit.NSEventTypeLeftMouseDown, AppKit.NSEventTypeLeftMouseUp)
                and event.clickCount() == 2
                and self.titlebar_background(event.locationInWindow())):
            # Consume the second down too: native tracking may otherwise handle the up itself
            # before it reaches sendEvent:, or apply its own action as well as this one.
            if event.type() == AppKit.NSEventTypeLeftMouseUp:
                perform_titlebar_double_click(self)
            return
        objc.super(SettingsWindow, self).sendEvent_(event)


def design_slider_key_value(key, current, minimum, maximum, step, page_step):
    movement = {123: -step, 124: step, 125: -step, 126: step,
                116: page_step, 121: -page_step, 115: minimum, 119: maximum}
    if key not in movement:
        return None
    if key in (115, 119):
        return movement[key]
    return min(maximum, max(minimum, current + movement[key]))


def design_readout():
    label = widgets.Label("", theme.TYPE["readout_key"], mono=True,
                          align=AppKit.NSTextAlignmentRight)
    label.width_constraint = label.view.widthAnchor().constraintEqualToConstant_(effects.DESIGN_READOUT_WIDTH)
    label.width_constraint.setActive_(True)
    return label


class _DesignSlider(AppKit.NSSlider):
    def keyDown_(self, event):
        if not self.isEnabled():
            return
        value = design_slider_key_value(int(event.keyCode()), self.doubleValue(),
                                        self._design_minimum, self._design_maximum,
                                        self._design_step, self._design_page_step)
        if value is None:
            return objc.super(_DesignSlider, self).keyDown_(event)
        self.setDoubleValue_(value)
        self.target().fire_(self)


class _DesignSliderChoice:
    def __init__(self, title, minimum, maximum, step, scale, stops, presets, callback, description):
        self.scale, self.stops, self.presets = scale, stops, presets
        self.description = description
        self.slider = _DesignSlider.alloc().initWithFrame_(((0, 0), (240, 26)))
        self.slider.setMinValue_(minimum)
        self.slider.setMaxValue_(maximum)
        self.slider.setDoubleValue_(stops[1] * scale)
        self.slider.setContinuous_(True)
        self.slider._design_step = step
        self.slider._design_page_step = step * 5
        self.slider._design_minimum = minimum
        self.slider._design_maximum = maximum
        self.slider.setAccessibilityLabel_(title)
        self.slider.setAccessibilityHelp_(description)
        self.target = widgets.callback_target(lambda: callback(self, self.slider))
        self.slider.setTarget_(self.target)
        self.slider.setAction_("fire:")
        widgets.squeeze(self.slider)
        self.view = self.slider
        self.focusable_views = (self.slider,)

    @property
    def enabled(self):
        return self.slider.isEnabled()

    @property
    def value(self):
        numeric = self.slider.doubleValue() / self.scale
        return effects.length_preset(numeric) if self.presets is effects.LENGTHS else effects.size_preset(numeric)

    @value.setter
    def value(self, preset):
        self.set_value(preset)

    def set_numeric(self, value):
        self.slider.setDoubleValue_(round(value * self.scale))

    def set_value(self, preset):
        index = next((i for i, (key, _name) in enumerate(self.presets) if key == preset), 1)
        self.set_numeric(self.stops[index])

    def numeric_value(self):
        return self.slider.doubleValue() / self.scale

    def set_enabled(self, enabled):
        self.slider.setEnabled_(bool(enabled))

    def set_accessible_value(self, value):
        self.slider.setAccessibilityValueDescription_(value)


class ControlWindow(AppKit.NSObject):
    def initWithController_settingsStore_logger_(self, controller, settings_store, logger):
        self = objc.super(ControlWindow, self).init()
        if self is None:
            return None
        self.controller = controller
        self.settings_store = settings_store
        self.logger = logger
        self.activation_policy_handler = None
        # Before anything is built: every control takes its colours from the palette in use.
        theme.set_dark(theme.wants_dark(controller.cfg.appearance, theme.system_dark()))
        # Set by TrayApp once it exists: the listener for the PC's input, and
        # the second way an arrangement changed here can reach the PC.
        self.windows_input = None
        # Set by TrayApp: saves one machine's direction switch (token, send= or allow_drive=).
        self.direction_handler = None
        self.left_out_handler = None
        self.last_capture_attempt = 0.0
        # macOS passes keys only to a process started after Input Monitoring is granted, so a grant
        # made during this run needs a relaunch before the keyboard crosses, however ready the tap looks.
        self.granted_at_launch = accessibility_granted() and input_monitoring_granted()
        self.update_checker = None
        self.update_handler = None
        self._update = None
        # The list of paired machines and the pairing sheet; TrayApp gives it the pairing service.
        self.panel = machines_panel.MachinesPanel(controller, settings_store, logger, self)
        self._machines_first = None
        self._preview_flashes = 0
        self.latency = collections.deque(maxlen=widgets.Spark.SAMPLES)
        self.wide = None
        # Set by TrayApp: lights the chosen edge while the resistance ruler moves.
        self.preview = None
        self.has_notch = False
        # Set by TrayApp, for the Design page's Try it; the page builds its own preview loop.
        self.haptics = None
        self.previews = None
        self._apply_serial = 0
        self._save_pending = False
        self._design_states = {}
        self._followed_version = None
        self._design_path = []
        # The machine the Crossing page shows and writes the ways of: an id ("" is the entry
        # migrated from 1.4.x), None while nothing is paired.
        self.chosen_peer = None
        self._picker_choices = None
        self.machine_select = None
        self.page = None
        self.opened = False
        self.side_by_side = []
        self.window = SettingsWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (900, 640)),
            AppKit.NSWindowStyleMaskTitled
            | AppKit.NSWindowStyleMaskClosable
            | AppKit.NSWindowStyleMaskMiniaturizable
            | AppKit.NSWindowStyleMaskResizable,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_("Beamer")
        self.window.setReleasedWhenClosed_(False)
        self.window.setMinSize_((640, 540))
        self.window.keyboard_navigation = False
        # The window always carries the palette's own appearance, never nil: the title bar's
        # buttons and text then match the palette whatever the Mac is set to, and the transparent
        # title bar takes the window's ground.
        self.window.setAppearance_(theme.appearance_named(theme.is_dark()))
        self.window.setBackgroundColor_(theme.colour("ground"))
        self.window.setTitlebarAppearsTransparent_(True)
        screen = self.window.screen() or AppKit.NSScreen.mainScreen()
        x, y, width, height = settings_normal_frame(
            self.settings_store.current().get("settings_window_frame"),
            screen.visibleFrame() if screen else AppKit.NSMakeRect(0, 0, 1728, 1084))
        self.window.setFrame_display_(((x, y), (width, height)), False)
        self._normal_frame = [x, y, width, height]
        self._frame_transition = False
        content = self.window.contentView()
        content.setWantsLayer_(True)
        theme.tint(content.layer(), background="ground")
        self.appearance_watch = theme.AppearanceWatch.alloc().initWithCallback_(lambda: self._apply_appearance())

        top = TitlebarStrip.alloc().initWithFrame_(((0, 0), (900, 18)))
        top.setTranslatesAutoresizingMaskIntoConstraints_(False)
        top.setWantsLayer_(True)
        theme.tint(top.layer(), background="ground")
        top_rule = widgets.box("rule")
        top.addSubview_(top_rule)
        self.sidebar = widgets.Sidebar(
            pages.PAGES,
            self._select_page,
            # Words rather than the address, which wraps mid-path at the sidebar's narrowest; the
            # address is in the tooltip.
            footer_text=tr("Beamer {version}\nBeamer's website").format(version=VERSION),
            footer_label=tr("Open Beamer's website, kalkmancode.co.uk/beamer"),
            on_footer=self._open_beamer_site,
        )
        self.sidebar.footer_icon.setImage_(menu_bar_glyph("local"))
        divider = widgets.box("rule")
        pane = widgets.stack(spacing=0)
        pane.setDetachesHiddenViews_(True)
        for view in (top, self.sidebar.view, divider, pane):
            content.addSubview_(view)
        self.sidebar_width = self.sidebar.view.widthAnchor().constraintEqualToConstant_(theme.SIDEBAR_WIDTH[1])
        AppKit.NSLayoutConstraint.activateConstraints_([
            top.topAnchor().constraintEqualToAnchor_(content.topAnchor()),
            top.leadingAnchor().constraintEqualToAnchor_(content.leadingAnchor()),
            top.trailingAnchor().constraintEqualToAnchor_(content.trailingAnchor()),
            top.heightAnchor().constraintEqualToConstant_(18),
            top_rule.leadingAnchor().constraintEqualToAnchor_(top.leadingAnchor()),
            top_rule.trailingAnchor().constraintEqualToAnchor_(top.trailingAnchor()),
            top_rule.bottomAnchor().constraintEqualToAnchor_(top.bottomAnchor()),
            top_rule.heightAnchor().constraintEqualToConstant_(1),
            self.sidebar_width,
            self.sidebar.view.topAnchor().constraintEqualToAnchor_(top.bottomAnchor()),
            self.sidebar.view.bottomAnchor().constraintEqualToAnchor_(content.bottomAnchor()),
            self.sidebar.view.leadingAnchor().constraintEqualToAnchor_(content.leadingAnchor()),
            divider.widthAnchor().constraintEqualToConstant_(1),
            divider.topAnchor().constraintEqualToAnchor_(top.bottomAnchor()),
            divider.bottomAnchor().constraintEqualToAnchor_(content.bottomAnchor()),
            divider.leadingAnchor().constraintEqualToAnchor_(self.sidebar.view.trailingAnchor()),
            pane.topAnchor().constraintEqualToAnchor_(top.bottomAnchor()),
            pane.bottomAnchor().constraintEqualToAnchor_(content.bottomAnchor()),
            pane.leadingAnchor().constraintEqualToAnchor_(divider.trailingAnchor()),
            pane.trailingAnchor().constraintEqualToAnchor_(content.trailingAnchor()),
        ])
        self.update_notice = widgets.Module()
        self.update_notice_text = widgets.Label("", theme.TYPE["body"], wrap=True)
        self.update_notice.add(self.update_notice_text.view)
        actions = widgets.stack(vertical=False, spacing=12)
        self.update_action = widgets.Button(tr("Get update"), self, "downloadUpdate:", style="primary")
        widgets.add(actions, self.update_action.view, full_width=False)
        self.update_notes = widgets.Button(tr("What's new"), self, "downloadUpdate:")
        self.update_dismiss = widgets.Button(tr("Dismiss"), self, "dismissUpdate:")
        widgets.add(actions, self.update_notes.view, full_width=False)
        widgets.add(actions, self.update_dismiss.view, full_width=False)
        self.update_notice.add(actions)
        self.update_notice.view.setHidden_(True)
        pane.addArrangedSubview_(self.update_notice.view)
        self.update_notice_width = self.update_notice.view.widthAnchor().constraintEqualToAnchor_(pane.widthAnchor())
        self.update_notice_width.setPriority_(AppKit.NSLayoutPriorityDragThatCannotResizeWindow)
        # The page host is the only part of the pane that gives way when the window is resized;
        # the commit bar keeps its height on every page.
        host = widgets.stack(spacing=0)
        host.setAlignment_(AppKit.NSLayoutAttributeWidth)
        host.setDetachesHiddenViews_(True)
        host.setContentHuggingPriority_forOrientation_(
            AppKit.NSLayoutPriorityDefaultLow, AppKit.NSLayoutConstraintOrientationVertical
        )
        host.setContentCompressionResistancePriority_forOrientation_(
            AppKit.NSLayoutPriorityDefaultLow, AppKit.NSLayoutConstraintOrientationVertical
        )
        widgets.add(pane, host)
        self.page_host = host
        widgets.add(pane, widgets.hairline())
        widgets.add(pane, self._commit())

        self.page_titles = {}
        # Same on all machines: each shared page's scope line, and the notes on this Mac's own rows.
        self.scope_labels = {}
        self.own_notes = []
        self.page_paddings = {}
        self.page_width_constraints = {}
        self.pages = {}
        builders = {
            "overview": self._overview_page,
            "crossing": self._crossing_page,
            "design": self._design_page,
            "keyboard": self._keyboard_page,
            "connection": self._connection_page,
            "permissions": self._permissions_page,
        }
        for key, name, _symbol, purpose in pages.PAGES:
            title = tr(name)
            page_purpose = (tr("Click a machine on the map to set its ways. The ledger says how it reaches each other machine.")
                            if key == "crossing" and profiles.same_ways_on(self.settings_store.current())
                            else tr(purpose))
            scope = tr(pages.SCOPE[key]) if key in pages.SCOPE and key != "crossing" else None
            scroll, body = self._page(title, page_purpose, scope, key)
            host.addArrangedSubview_(scroll)
            scroll.setHidden_(True)
            self.pages[key] = scroll
            builders[key](body)

        self._load(config_to_raw(controller.cfg))
        self._select_page(pages.opening_page(None, accessibility_granted(), input_monitoring_granted()))
        self.window.setDelegate_(self)
        self.windowDidResize_(None)
        self.refresh()
        return self

    def windowDidResize_(self, _notification):
        self._remember_normal_frame()
        width = self.window.contentView().frame().size.width
        if width == getattr(self, "_last_resize_width", None):
            return
        self._last_resize_width = width
        sidebar = 56 if width < 640 else 144 if width < 800 else 176
        self.sidebar_width.setConstant_(sidebar)
        self.sidebar.set_collapsed(width < 640)
        page_width = width - sidebar - 1 - 16
        self._apply_width(page_width)
        body_width = page_column_width(page_width)
        if self.page == "crossing":
            for constraint in self.atlas_canvas.constraints():
                if (constraint.firstItem() == self.atlas_canvas and constraint.secondItem() is None
                        and constraint.firstAttribute() == AppKit.NSLayoutAttributeHeight):
                    constraint.setConstant_(220 if width <= 640 else 260)
        if self.page == "design":
            inner_width = body_width - 32
            for choices in (self.glow_style_select, self.switch_style_select, self.glow_colour_select):
                for row in choices.rows:
                    if not row.view.isHiddenOrHasHiddenAncestor():
                        row.grid.arrange(inner_width)
            for row in (self.length_row, self.size_row, self.style_for_row, self.effect_method_view,
                        self.tick_steps_pair):
                if not row.isHiddenOrHasHiddenAncestor():
                    row.field_owner.arrange(inner_width)
        self.page_width_constraints[self.page].setConstant_(body_width)
        self.page_width_constraints[self.page].setPriority_(499)
        for pair in self.side_by_side:
            if not pair.view.enclosingScrollView().isHidden():
                pair.update_orientation(body_width - 2 * theme.MODULE_PADDING[1])

    @objc.python_method
    def _remember_normal_frame(self):
        if self._frame_transition:
            return
        frame = ordinary_window_frame(self.window)
        if frame is not None:
            self._normal_frame = frame

    def windowDidMove_(self, _notification):
        self._remember_normal_frame()

    def windowWillEnterFullScreen_(self, _notification):
        self._remember_normal_frame()
        self._frame_transition = True

    def windowWillExitFullScreen_(self, _notification):
        self._frame_transition = True

    def windowDidExitFullScreen_(self, _notification):
        screen = self.window.screen() or AppKit.NSScreen.mainScreen()
        x, y, width, height = settings_normal_frame(self._normal_frame, screen.visibleFrame())
        self.window.setFrame_display_(((x, y), (width, height)), False)
        self._frame_transition = False

    def windowDidFailToEnterFullScreen_(self, _window):
        self._frame_transition = False

    def windowDidFailToExitFullScreen_(self, _window):
        self._frame_transition = False

    def windowDidBecomeKey_(self, _notification):
        self.sidebar.refresh_focus()
        self._show_login_state()

    def windowWillUseStandardFrame_defaultFrame_(self, window, _default_frame):
        return window_fill_frame(window)

    @objc.python_method
    def _apply_width(self, width):
        """Two layouts, not a continuous reflow. Wide, Pairing puts the machine list beside the code;
        narrow, it stacks and the large figures step down a size."""
        threshold = WIDE_WIDTH if self.wide is None else WIDE_WIDTH + (-16 if self.wide else 16)
        wide = width >= threshold
        if wide == self.wide:
            return
        self.wide = wide
        self._apply_page_width(self.page, wide)

    @objc.python_method
    def _apply_page_width(self, key, wide):
        if key == "design":
            return
        narrow = not wide
        top, leading, bottom, trailing = (24, 24, 24, 24)
        constraints = self.page_paddings[key]
        for constraint, constant in zip(constraints, (top, leading, bottom, trailing)):
            constraint.setConstant_(constant)
        constraints[-1].setConstant_(-(leading + trailing))
        self.page_titles[key].set(size=theme.PAGE_TITLE_NARROW if narrow else theme.PAGE_TITLE)
        if key == "overview":
            self.state_word.set(size=theme.TYPE["status_word_narrow" if narrow else "status_word"])
            for readout in (self.round_trip, self.peer):
                readout.set_narrow(narrow)
            self._show_peer()
            panel = self.panel
            if panel.wide != wide:
                panel.wide = wide
                # Stacked, each half takes the full width; side by side, FillEqually shares it.
                AppKit.NSLayoutConstraint.deactivateConstraints_(panel.pair_stacked)
                panel.pair_grid.setOrientation_(
                    AppKit.NSUserInterfaceLayoutOrientationHorizontal if wide else AppKit.NSUserInterfaceLayoutOrientationVertical
                )
                panel.pair_grid.setDistribution_(
                    AppKit.NSStackViewDistributionFillEqually if wide else AppKit.NSStackViewDistributionFill
                )
                if narrow:
                    AppKit.NSLayoutConstraint.activateConstraints_(panel.pair_stacked)
                panel.refresh()
        elif key == "crossing":
            self.resistance_numeral.set(size=theme.TYPE["numeral_narrow" if narrow else "numeral"])
            self.double_tap_numeral.set(size=theme.TYPE["numeral_narrow" if narrow else "numeral"])

    @objc.python_method
    def _select_page(self, key):
        self.page = key
        for name, scroll in self.pages.items():
            scroll.setHidden_(name != key)
            if name != key and scroll.superview() is not None:
                self.page_host.removeArrangedSubview_(scroll)
                scroll.removeFromSuperview()
        if self.pages[key].superview() is None:
            self.page_host.addArrangedSubview_(self.pages[key])
        self._last_resize_width = None
        self.windowDidResize_(None)
        self._apply_page_width(key, self.wide)
        self.sidebar.select(key)
        self._say(pages.footer(key))
        # Both recorders listen application-wide; left armed, they would take the first key typed on
        # another page.
        if key != "crossing":
            self.key_recorder.cancel()
            for row in self.crossing_ledger.rows.values():
                row.jump.cancel()
        if key != "keyboard":
            self.ignored_recorder.cancel()
        self._run_previews()

    @objc.python_method
    def _open_beamer_site(self):
        AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.URLWithString_(BEAMER_SITE_URL))

    @objc.python_method
    def _run_previews(self):
        """The previews play only while someone can see them: the Design page open in a visible
        window, with crossing animations switched on."""
        if self.previews is None:
            return
        if self.page == "design" and self.window.isVisible() and self.glow_box.value:
            self.previews.start()
            self.tile_hover.sync_clock()
        else:
            self.tile_hover.stop()
            self.previews.stop()

    def showPage_(self, sender):
        self.opened = True
        self._select_page(pages.KEYS[sender.tag()])
        self.show()
    @objc.python_method
    def _load(self, raw):
        self.host_field.setStringValue_(str(raw["host"]))
        self.host_secret.setStringValue_(str(raw["host"]))
        self._show_host_field()
        self.port_field.setStringValue_(str(raw["port"]))
        self.ignored_entries = list(raw["ignored_inputs"])
        self._render_ignored()
        self.pointer_ruler.value = round(raw["pointer_speed"] * 100)
        self.scroll_ruler.value = round(raw["scroll_speed"] * 100)
        self.pointer_numeral.set(str(self.pointer_ruler.value))
        self.scroll_numeral.set(str(self.scroll_ruler.value))
        self.reverse_scroll_box.value = raw["reverse_scroll"]
        self.mouse_only_box.value = raw["mouse_only"]
        self.keyboard_sync_box.value = raw["keyboard_sync"]
        self.appearance_select.value = raw["appearance"]
        self.language_select.value = raw["language"]
        self._apply_appearance(raw["appearance"])
        self.updates_switch.value = raw["check_updates"]
        self.pointer_box.value = raw["hide_idle_pointer"]
        self.modifier_select.value = raw["key_map"] if isinstance(raw["key_map"], str) else "custom"
        crossing_raw = raw["crossing"]
        self._load_ways(crossing_raw)
        self.haptics_box.value = crossing_raw["haptics"]
        self.glow_box.value = crossing_raw["glow"]
        self.notch_style_select.value = crossing_raw["notch_style"]
        self.notch_after_select.value = crossing_raw["notch_after_ms"]
        self.tick_steps_select.value = crossing_raw["haptic_steps"]
        self.hold_box.value = crossing_raw["hold_full_screen"]
        self._load_shared(raw)
        self._refresh_design_follow()

    @objc.python_method
    def _load_shared(self, raw):
        """The controls Same on all machines keeps in step, and nothing else: settings arriving
        from another machine must not reset a half-typed address, the pairing card, or the chosen
        machine's ways, which are never shared."""
        self.key_recorder.set_value(raw["trigger_key"])
        self.style_select.value = raw["trigger_style"]
        self.double_tap_ruler.value = raw["double_tap_ms"]
        self.double_tap_numeral.set(str(raw["double_tap_ms"]))
        crossing_raw = raw["crossing"]
        self.shortcut_goes_to.value = crossing_raw.get("shortcut_goes_to", "last")
        self._show_shortcut_destination_note()
        self.method_boxes["shortcut"].value = "shortcut" in crossing_raw["methods"]
        self.resistance_ruler.value = crossing_raw["resistance_px"]
        self.landing_box.value = crossing_raw["shortcut_arrival"]
        self.switch_style_select.value = crossing_raw["shortcut_arrival_style"]
        self.dragging_box.value = crossing_raw["block_while_dragging"]
        self.glow_style_select.value = crossing_raw["glow_style"]
        self.frame_rate_select.value = crossing_raw.get("frame_rate", "display")
        set_divisor(effects.frame_divisor(self.frame_rate_select.value))
        self.length_select.set_numeric(crossing_raw.get("effect_pace", 1.0))
        self.size_select.set_numeric(crossing_raw.get("effect_size_fraction", 0.045))
        self._refresh_design_readouts()
        self.glow_colour_select.value = crossing_raw["glow_colour"]
        self._reflect()

    @objc.python_method
    def _reflect(self, *_ignored):
        """Enables each control only when the setting it edits is in play, and keeps the figures
        beside the rulers in step with them."""
        methods = self._methods()
        rows = pages.crossing_rows(methods)
        for key, view in (("resistance", self.resistance_module.view),
                          ("shortcut", self.shortcut_module.view)):
            visible = rows[key] or key == "shortcut" and bool(getattr(self, "_trigger_conflict", ""))
            motion.set_hidden(view, not visible)
        self.dragging_box.set_enabled(bool(set(methods) & {"edge", "part", "corner", "notch"}))
        if not rows["shortcut"]:
            # Hidden, an armed recorder would still take the next key typed anywhere on the page.
            self.key_recorder.cancel()
        self._show_arrangement(methods)
        self._show_shortcut_destination_note()
        haptics, glow = self.haptics_box.value, self.glow_box.value
        for control in (self.tick_steps_select, self.try_button):
            control.set_enabled(haptics)
        # Off, the look of crossing cannot apply, so its modules go rather than sit faded a screen
        # each, and the landing plays through the same animations; the note under the switch
        # says crossing still works.
        for view in (self.landing_row, self.style_module.view, self.colour_module.view):
            motion.set_hidden(view, not glow)
        style = self.glow_style_select.value
        # Without the effects, today's glow and notch styles draw whatever is chosen.
        notch_applies = pages.notch_style_applies(style) or not self.effects_ready
        if not self._place_chosen:
            # Until a place is picked here, the tiles show where the pointer actually crosses.
            self.place = pages.preview_place(methods)
            self.effect_method_select.value = self.place
        place = self.place
        switching = glow and self.landing_box.value
        motion.set_hidden(self.style_for_row, not switching)
        mode = self.style_for_select.value if switching else "crossing"
        # One set of tiles gives way before the other comes in; both at once would double the
        # page's height for the length of the fade.
        showing, leaving = (
            (self.switch_styles_box, self.crossing_styles_box) if mode == "switch"
            else (self.crossing_styles_box, self.switch_styles_box)
        )
        if not motion.heading(leaving):
            motion.set_hidden(leaving, True, done=lambda: motion.set_hidden(showing, False))
        elif not motion.moving(leaving):
            motion.set_hidden(showing, False)
        motion.set_hidden(self.effect_method_view, mode == "switch")
        motion.set_hidden(self.place_note.view, mode == "switch" or not self.place_note_text(style, place, methods))
        self.place_note.set(self.place_note_text(style, place, methods))
        motion.set_hidden(self.notch_row, not (mode == "crossing" and place == "notch" and notch_applies))
        for stack, still, screens in self.classic_pictures:
            stack.show(screens.get(self.notch_style_select.value, screens["beam"]) if place == "notch" else still)
        colour = self.glow_colour_select.value
        # Every still redraws, cross-fading, for a new colour, place or length.
        look = (colour, place, self.length_select.value)
        if self._look is not None and look != self._look:
            for still in self.effect_stills:
                motion.cross_fade(still)
        self._look = look
        if mode == "switch":
            choice = self.switch_style_select.value or "match"
            fx = effects.switch_effect(choice, style or "glow")
            self.effect_name.set(tr("Same as crossing: {name}").format(name=tr(fx.name)) if choice == "match" else tr(fx.name))
        else:
            fx = effects.preview_effect(style) if self.effects_ready else effects.CLASSIC.get(style)
            if fx is not None:
                self.effect_name.set(tr("{name}, {intensity}").format(
                    name=tr(fx.name), intensity=tr(pages.INTENSITIES.get(fx.intensity, fx.intensity.capitalize())).lower()))
        crossing_fx = effects.preview_effect(style) if self.effects_ready else effects.CLASSIC.get(style)
        if crossing_fx is not None:
            self.switch_style_select.set_detail("match", tr("Plays {name}").format(name=tr(crossing_fx.name)))
        motion.set_hidden(self.chosen_box, fx is None)
        if fx is not None:
            self.effect_blurb.set(tr(fx.blurb))
        # Drawing every still is tens of milliseconds, so only what they show redraws them: the
        # colour, the place, the length, the size, the palette, and for Same as crossing the crossing style.
        stills = (colour, style, place, self.length_select.value, self.size_select.value,
                  self.notch_after_select.value, theme.is_dark())
        if stills != self._stills:
            self._stills = stills
            for still in self.effect_stills:
                still.setNeedsDisplay_(True)
            self.previews.repaint()
        self.tick_note.set(
            ({
                "quarters": tr("A tick at a quarter, half and three quarters of the push, then a double tick as the pointer goes through. macOS plays them only while a finger is on the trackpad, so press and hold Try it."),
                "halves": tr("One tick halfway through the push, then a double tick as the pointer goes through. macOS plays them only while a finger is on the trackpad, so press and hold Try it."),
                "breakthrough": tr("Nothing on the way in, then a double tick as the pointer goes through. macOS plays them only while a finger is on the trackpad, so press and hold Try it."),
            }.get(self.tick_steps_select.value, tr(" macOS plays them only while a finger is on the trackpad, so press and hold Try it.")))
            if haptics else tr("Off: the trackpad stays still while you push.")
        )
        after = (self.notch_after_select.value or 1200) / 1000
        self.notch_note.set(
            tr("Once the pointer is through to another machine the notch keeps playing for {seconds} seconds. Outline and Island apply to Glow and Beam; other styles draw their own notch.").format(seconds=f"{after:g}")
        )
        self._run_previews()
        notch_range = self.controller.notch_range
        settings = self.settings_store.current()
        layout = core_ways.derive_layout(settings, gap=0)
        own = settings["machine_id"]
        contact = (core_ways._rect_contact(layout[own], layout[self.chosen_peer])
                   if own in layout and self.chosen_peer in layout else None)
        side = contact[0] if contact and contact[0] != "corner" else None
        self.method_boxes["shortcut"].set_detail(core_ways.shortcut_detail(
            widgets.key_title(self.key_recorder.value), self.shortcut_goes_to.value or "last"))
        self.ignored_recorder.set_trigger_code(KEY_NAME_TO_CODE.get(self.key_recorder.value))
        hold = self.style_select.value == "hold"
        motion.set_hidden(self.double_tap_head, hold)
        self.style_hint.set(
            getattr(self, "_trigger_conflict", "") or (tr("Input is on another machine for as long as the key is held. If you jump while holding the shortcut, releasing it brings input home.")
            if hold
            else tr("Tap twice to switch; tap twice again to come back.")),
            ink="amber" if getattr(self, "_trigger_conflict", "") else "ink_2",
        )
        resistance = self.resistance_ruler.value
        self.resistance_numeral.set(str(resistance))
        self.push_strip.set_other_name(self._chosen_label())
        side = self._selected_ways()["side"]
        self.resistance_hint.set(
            tr("Switches the moment this Mac's pointer touches the edge. Each screen supplies its own zones.")
            if resistance == 0
            else tr("Follows this Mac's mouse or trackpad on every screen; each screen supplies its own zones. This screen's {side} edge lights to show the resistance.").format(side=tr(core_ways.EDGE_NAMES[side]))
            if side
            else tr("Follows this Mac's mouse or trackpad on every screen; each screen supplies its own zones.")
        )
        self.modifier_note.set({
            "semantic": tr("On a PC, Command arrives as Control, so Command-C copies there too, and Control arrives as the Windows key. Between two Macs every key arrives as itself."),
            "positional": tr("On a PC, each key arrives as the key in its place, so Command arrives as the Windows key. Between two Macs every key arrives as itself."),
        }.get(self.modifier_select.value, tr("Custom: the key map in settings.json is kept as it is.")))

    @objc.python_method
    def _selected_ways(self):
        if self.chosen_peer is not None:
            return core_ways.ways(self._view_settings(), self.chosen_peer)
        flat = self.controller.cfg.crossing
        return dict(side=flat["edge"], methods=flat["methods"], parts=flat["edge_parts"], corner=flat["corner"])

    @objc.python_method
    def _methods(self):
        methods = [way for way in self._selected_ways()["methods"] if way != "shortcut"]
        if self.method_boxes["shortcut"].value:
            methods.append("shortcut")
        return methods

    @objc.python_method
    def _load_ways(self, flat=None):
        peers = peerlist.desktops(self._view_settings()["peers"])
        if self.chosen_peer not in {entry["id"] for entry in peers}:
            self.chosen_peer = peers[0]["id"] if peers else None
        self._show_machines()
    @objc.python_method
    def _show_machines(self):
        if hasattr(self, "crossing_ledger"):
            remote = self._editing_other()
            viewed = self._view_settings()
            self.crossing_ledger.refresh(viewed, self.chosen_peer, remote=remote,
                read_only=remote and "profiles" not in self._profile_caps().get(self.viewed_machine, ()),
                has_notch=(self.settings_store.current()["crossing_desk"].get("status", {}).get(
                           self.viewed_machine, {}).get("value", {}).get("platform") == "mac"
                           if remote else self.has_notch))
    @objc.python_method
    def _machine_picked(self, peer):
        # A change still waiting out the pause belongs to the machine being left.
        self._flush()
        self.chosen_peer = peer
        self._load_ways()
        self._reflect()

    @objc.python_method
    def _flush(self):
        """Writes a change still waiting out _changed's pause now, and the pause then writes nothing."""
        if self._save_pending:
            self._apply_serial += 1
            self._apply_settings()

    @objc.python_method
    def _chosen_label(self):
        """What the page calls the chosen machine; with nothing paired, what it always has."""
        peers = self.settings_store.current()["peers"]
        labels = peerlist.labels(peers)
        found = next((labels[entry["token"]] for entry in peers if entry["id"] == self.chosen_peer), None)
        return self._shown(found) if found else link_state.peer_name(self.controller.cfg)

    @objc.python_method
    def _show_arrangement(self, methods=None):
        settings = self._view_settings()
        own = settings.get("machine_id", "")
        entries = {entry["id"]: entry for entry in peerlist.desktops(settings["peers"])}
        stored = self._atlas_layout_for(settings)
        excluded = {peer for peer, entry in entries.items() if entry.get("left_out")
                    or entry.get("dormant") or entry.get("in_use", True) is False}
        tiles = {key: dict(tile) for key, tile in stored.items() if key not in excluded}
        caps = self._design_peer_caps()
        capable = self._crossing_args()["capable"]
        if self._editing_other():
            capable = set(tiles)
        result = core_ways.compile_layout(settings, tiles, capable=capable,
            platform=profiles.view_platform(self.settings_store.current(), own) if self._editing_other() else "mac")
        states = result.get("tiles", {})
        names = {peer: self._shown(peerlist.label_for(settings["peers"], peer_id=peer) or peerlist.UNNAMED)
                 for peer in entries}
        names[self.own_id()] = tr("This Mac")
        if self._editing_other():
            names[own] = settings["name"]
        older = {peer for peer, values in caps.items() if "layout" not in values}
        for peer, entry in entries.items():
            held = core_ways.ways(settings, peer)
            picked = core_ways.picked_thirds(held["methods"], held["parts"])
            state = dict(states.get(peer, {"state": "gap", "thirds": []}))
            state.update(in_use=entry.get("in_use", True) is True, linked=self._live(peer),
                         platform={"macos": "macOS"}.get(entry.get("platform", ""), machines.platform_name(entry.get("platform", ""))),
                         version=entry.get("app", ""), crosses=core_ways.crosses_on_map(settings, peer),
                         picked_thirds=picked,
                         lit_thirds=[part for part in picked if part in state.get("thirds", picked)]
                         if "part" in held["methods"] else [],
                         edge_off=not bool(picked))
            wraps = [wrap["side"] for wrap in state.get("wraps", [])]
            state["wrap_sides"] = [side for side in core_ways.EDGES if side in wraps] if peer not in excluded else []
            state["corner_ways"] = ([corner for corner in core_ways.CORNERS if corner in state.get("corners", [])]
                                    if peer not in excluded else [])
            state["kept_sides"] = []
            if peer not in excluded and held["edges"]:
                sides = self._reach(peer, settings)["sides"]
                state["kept_sides"] = [side for side in core_ways.EDGES if sides[side]["state"] == "kept"]
            has_notch = (profiles.view_platform(self.settings_store.current(), own) == "mac"
                         if self._editing_other() else self.has_notch)
            state["notch_way"] = bool(peer not in excluded and has_notch and "notch" in held["methods"])
            states[peer] = state
        try:
            notch = diagram.notch_span(self.controller.notch_range, self.controller.crossing_display_map())
        except Exception:
            notch = None
        if self._editing_other():
            notch = None
        self.atlas_canvas.show_layout(
            tiles, {own: {"state": "this", "platform": ("macOS" if not self._editing_other()
                         or profiles.view_platform(self.settings_store.current(), own) == "mac" else "PC"), "notch_span": notch,
                         "machine_name": settings.get("name", "") or socket.gethostname().split(".")[0]}, **states},
            names, own, own if profiles.same_ways_on(self.settings_store.current()) else self.chosen_peer or own,
            cycle={peer: entry.get("shortcut_cycle") is not False for peer, entry in entries.items()},
            cycle_enabled=not self._editing_other() and (self.shortcut_goes_to.value or "last") == "ticked", older=older,
            animate=motion.live(self.atlas_canvas),
            tile_words={machine: profiles.tile_word(self.settings_store.current(), machine,
                linked=machine == self.own_id() or self._live(machine),
                caps=("profiles",) if machine == self.own_id() else self._profile_caps().get(machine, ()))
                for machine in tiles},
            machine_picker=profiles.same_ways_on(self.settings_store.current()),
            fixed_machine_id=self.own_id(),
            read_only=self._editing_other() and "profiles" not in self._profile_caps().get(own, ()))
        self._show_machines()
        self._show_profiles()
    @objc.python_method
    def _atlas_default_layout(self, settings):
        return core_ways.derive_layout(settings, gap=0)

    @objc.python_method
    def _atlas_layout_for(self, settings):
        return self._atlas_default_layout(settings)

    @objc.python_method
    def _atlas_selected(self, peer):
        if profiles.same_ways_on(self.settings_store.current()):
            self._flush()
            self.viewed_machine = peer
            self.chosen_peer = None
            self._load_ways()
            self._show_arrangement()
            return
        if peer == self.own_id():
            self.atlas_canvas.selected = self.chosen_peer or self.own_id()
            self.atlas_canvas._draw(False)
            return
        self._machine_picked(peer)

    @objc.python_method
    def _open_pairing(self):
        self._select_page("overview")
        self.window.makeFirstResponder_(self.panel.sheet.show_button.view)

    @objc.python_method
    def _re_pair_peer(self, peer):
        entry = next((item for item in self.settings_store.current()["peers"] if item.get("id") == peer), None)
        if entry is not None:
            self._select_page("overview")
            self.panel.re_pair(entry["token"])

    @objc.python_method
    def _open_forget(self, peer):
        entry = next((item for item in self.settings_store.current()["peers"] if item.get("id") == peer), None)
        if entry is None:
            return
        self._select_page("overview")
        self.panel._ask_to_remove(entry.get("token"))

    @objc.python_method
    def _atlas_layout_changed(self, tiles, moved, snap=True):
        self._flush()
        held = self.settings_store.current()
        previous = self._atlas_layout_for(held)
        radius = 0
        if snap:
            _frames, scale = self.atlas_canvas._map()
            radius = self.atlas_canvas.snap_radius_for_scale(scale)
        try:
            outcome = self.controller.crossing_event(crossing_state.drop, tiles, moved,
                                                    snap_radius=radius, **self._crossing_args())
        except (SettingsError, OSError, TypeError, ValueError):
            self.logger.exception("could not save the desk layout")
            self.atlas_canvas.show_layout(previous, {}, {}, held.get("machine_id", ""), self.chosen_peer, animate=False)
            return
        if not outcome["accepted"]:
            labels = {entry.get("id"): peerlist.label_for(held["peers"], peer_id=entry.get("id")) or tr("Machine")
                      for entry in peerlist.desktops(held["peers"])}
            self.atlas_canvas.show_layout(previous, {}, labels,
                                          held.get("machine_id", ""), self.chosen_peer, animate=True)
            self._say(tr("Refused: that position is already occupied."), "fault")
            return
        self._load_ways()
        self._reflect()
        if moved in outcome["settings"]["layout"]:
            tile_state = dict(outcome["tiles"].get(moved, {}))
            tile_state["crosses"] = core_ways.crosses_on_map(self.settings_store.current(), moved)
            before = previous.get(moved, {})
            after = outcome["settings"]["layout"][moved]
            if (before.get("w"), before.get("h")) != (after["w"], after["h"]):
                self.atlas_canvas.announce_resize(moved)
            else:
                self.atlas_canvas.announce_move(moved, after, tile_state)

    @objc.python_method
    def _begin_atlas_rename(self, peer):
        if self._editing_other():
            return
        if peer in self.crossing_ledger.rows:
            self.crossing_ledger.rows[peer].begin_rename()
    def control_textView_doCommandBySelector_(self, control, _text_view, selector):
        for row in self.crossing_ledger.rows.values():
            if control is row.name_field:
                if selector == "insertNewline:":
                    row.finish_rename(True)
                    return True
                if selector == "cancelOperation:":
                    row.finish_rename(False)
                    return True
        return False
    @objc.python_method
    def _save_atlas_rename(self):
        for row in self.crossing_ledger.rows.values():
            if row.renaming:
                row.finish_rename(True)
    @objc.python_method
    def _cancel_atlas_rename(self):
        for row in self.crossing_ledger.rows.values():
            if row.renaming:
                row.finish_rename(False)
    @objc.python_method
    def _set_atlas_left_out(self, peer, enabled):
        if self._editing_other():
            return
        self._flush()
        settings = self.settings_store.current()
        entry = next((item for item in settings["peers"] if item.get("id") == peer), None)
        if entry is None:
            return
        if not enabled and entry.get("in_use", True) is False:
            self.direction_handler(entry.get("token"), in_use=True)
            settings = self.settings_store.current()
        try:
            self.controller.crossing_event(
                crossing_state.leave_out if enabled else crossing_state.put_back, peer, **self._crossing_args())
        except (SettingsError, OSError, TypeError, ValueError):
            self.logger.exception("could not save whether the machine is left out")
            return
        if enabled and self.chosen_peer == peer:
            self.chosen_peer = None
        self._load_ways()
        self._reflect()

    @objc.python_method
    def _set_shortcut_cycle(self, peer, enabled):
        if hasattr(self, "viewed_machine") and self._editing_other():
            return
        self._flush()
        try:
            with self.settings_store.lock:
                settings = copy.deepcopy(self.settings_store.current())
                entry = next((item for item in settings["peers"] if item.get("id") == peer), None)
                if entry is None:
                    return
                entry["shortcut_cycle"] = bool(enabled)
                self.settings_store.save_settings(settings)
                self.controller.cfg = self.settings_store.load()
        except (SettingsError, OSError, TypeError, ValueError):
            self.logger.exception("could not save a shortcut cycle choice")
            return
        self._show_arrangement()

    @objc.python_method
    def _preview_resistance(self, value):
        # Applied to the live engine at once, so the edge can be felt mid-drag; the saved setting
        # follows a moment later, once the ruler stops moving.
        self.controller.crossing.resistance_px = float(value)
        self._changed()
        # A machine not placed yet has no edge to light, and the glow turns itself off for the run
        # on one it cannot draw.
        if self.preview is not None and self.glow_box.value and self._selected_ways()["side"]:
            self.preview(self._selected_ways()["side"], min(1.0, value / 500.0))

    @objc.python_method
    def _show_peer(self):
        """The machine the status speaks of (where input is, else where the shortcut would send it), else
        the first one paired: its name and address."""
        entry = self._focus_entry()
        if entry is None:
            self.peer.value.set("—")
            self.peer_footer.set(tr("Not paired"))
            return
        self.peer.value.set(self._shown(self._machine_label()))
        host, port = entry.get("host"), entry.get("port")
        if not host:
            self.peer_footer.set(tr("No address"))
        else:
            # The port does not fit a third of the status module at the narrow grid.
            self.peer_footer.set(self._shown(f"{host}:{port}" if self.wide else host))

    @objc.python_method
    def _first_label(self):
        return self.controller.peer_label or tr("the other machine")

    @objc.python_method
    def _focus_entry(self):
        peers = self.controller.book.peers()
        wanted = self.controller._in_question()
        return next((peer for peer in peers if wanted is not None and peer.get("id") == wanted), peers[0] if peers else None)

    @objc.python_method
    def _machine_label(self):
        """What the window calls the machine its status is about: where input is, else the first."""
        controller = self.controller
        return controller.on_label or controller.peer_label or link_state.peer_name(controller.cfg)

    @objc.python_method
    def _header_response_time(self):
        controller = self.controller
        server = self.windows_input.server if self.windows_input is not None else None
        incoming_by_text = {}
        if server is not None:
            for remote_id in server.links():
                text_id = remote_id if isinstance(remote_id, str) else protocol.id_text(remote_id)
                incoming_by_text[text_id] = remote_id
        incoming = set(incoming_by_text)
        with controller._owner_lock:
            outgoing_links = list(controller._peers_up.items())
        outgoing = {peer for peer, link in outgoing_links if link.live()}
        driver = server.owner if server is not None else None
        if driver is not None and not isinstance(driver, str):
            driver = protocol.id_text(driver)
        active = (controller.owner.on, driver)
        peer = next((candidate for candidate in active if candidate in outgoing or candidate in incoming), None)
        if peer is None:
            current = controller._status_link()
            if current is not None and current.live():
                peer = current.peer_id
        if peer is None:
            candidates = outgoing | incoming
            peer = min(candidates) if candidates else None
        if peer is None:
            return "—", None, False
        link = controller._link_of(peer)
        response = getattr(link, "response_time_ms", None) if link is not None and link.live() else None
        if response is None and server is not None and peer in incoming:
            lookup = getattr(server, "response_time_ms", None)
            response = lookup(incoming_by_text[peer]) if lookup is not None else None
        label = controller._label(lambda entry: entry.get("id") == peer) or "another machine"
        return label, response, True

    @objc.python_method
    def _live(self, peer):
        """Whether `peer` has a link up now, either way. One that has none gives way on the Crossing
        page and to an arrangement (core/ways.py give_way)."""
        if peer == self.own_id():
            return True
        link = self.controller._peers_up.get(peer)
        return (link is not None and link.live()) or peer in self.inbound_ids()

    @objc.python_method
    def _gone(self, peer):
        """Whether `peer` has removed this pairing: the link this Mac dials to it was closed unanswered
        for a minute (core/link.py). Its zones set aside by `give_way` then stay off for good."""
        entry = next((item for item in self.settings_store.current()["peers"] if item.get("id") == peer), None)
        link = self.controller.links.get(entry["token"]) if entry else None
        return link is not None and link.kind == "forgotten"

    @objc.python_method
    def settle_presence(self):
        """Picture peers retain intent independently of presence."""
        return

    @objc.python_method
    def inbound_ids(self):
        """The ids of the machines that have a link up to this Mac."""
        windows_input = self.windows_input
        if windows_input is None:
            return set()
        return {protocol.id_text(peer) for peer in windows_input.server.links()}

    @objc.python_method
    def _follow_first_peer(self):
        """A pairing stored from the pairing service's thread can change which machine the flat
        settings read; until the controller has them, a save from this window would write the old
        first machine back over it."""
        peers = self.controller.book.peers()
        desktop = peerlist.first_desktop(peers)
        first = desktop["token"] if desktop is not None else ""
        if first != (self.controller.cfg.auth_token or ""):
            self.peers_changed()

    @objc.python_method
    def peers_changed(self):
        """A machine was paired or removed: the controller reads the settings again (without
        `update_config`, which would bring input home), the links follow the peers, and the
        address fields show the first machine. A change still waiting out the pause is written
        first: the page reloads from the settings below, which would otherwise drop it."""
        self._flush()
        try:
            cfg = self.settings_store.load()
        except SettingsError as exc:
            self.logger.warning("settings not read after the peers changed: %s", exc)
            return
        peer_ids = {entry.get("id") for entry in self.settings_store.current()["peers"]}
        if cfg.design_follow_peer and cfg.design_follow_peer not in peer_ids:
            raw = config_to_raw(cfg)
            raw["design_follow_peer"] = ""
            cfg = self.settings_store.save(raw)
        self.controller.peers_changed()
        self.controller.apply_settings(cfg)
        if self.windows_input is not None:
            self.windows_input.sync(cfg)
        self._load(config_to_raw(cfg))
        self.refresh()

    @objc.python_method
    def _shown(self, text):
        """`text` as the window may show it: with Hide addresses on, no address in it."""
        return pages.redact(text, self.controller.cfg.hide_addresses)

    @objc.python_method
    def _flash_preview(self, level):
        self._place_preview(0.3 + 0.7 * level)
        self._preview_flashes += 1
        flash = self._preview_flashes

        def fade():
            if flash != self._preview_flashes:
                return
            Quartz.CATransaction.begin()
            Quartz.CATransaction.setAnimationDuration_(0.6)
            self.preview_glow.setOpacity_(theme.PREVIEW_REST)
            Quartz.CATransaction.commit()

        AppHelper.callLater(0.5, fade)

    @objc.python_method
    def _place_preview(self, opacity):
        width, height = 44.0, 28.0
        frames = {
            "left": (((0, 0), (7, height)), (1, 0.5), (0, 0.5)),
            "right": (((width - 7, 0), (7, height)), (0, 0.5), (1, 0.5)),
            "top": (((0, height - 7), (width, 7)), (0.5, 0), (0.5, 1)),
            "bottom": (((0, 0), (width, 7)), (0.5, 1), (0.5, 0)),
        }
        frame, start, end = frames.get(self._selected_ways()["side"], frames["right"])
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.preview_glow.setFrame_(frame)
        self.preview_glow.setStartPoint_(start)
        self.preview_glow.setEndPoint_(end)
        self.preview_glow.setOpacity_(opacity)
        Quartz.CATransaction.commit()

    @objc.python_method
    def _double_tap_moved(self, value):
        self.double_tap_numeral.set(str(value))
        self._changed()

    @objc.python_method
    def _hold_changed(self, value):
        # The controller reads this at every crossing, so it must not wait out the debounce; the
        # controller's config is the one the pending save builds on.
        with self.controller._cfg_lock:
            self.controller.cfg.crossing["hold_full_screen"] = value
        self._changed()

    @objc.python_method
    def _changed(self, *_ignored):
        """Every control outside the Connection page calls this. The settings are written and
        applied once the control has been still for a moment, so a ruler being dragged writes once
        at the end rather than on every step."""
        self._reflect()
        if hasattr(self, "length_select"):
            self._refresh_design_readouts()
        self._save_pending = True
        self._apply_serial += 1
        serial = self._apply_serial
        AppHelper.callLater(0.3, lambda: serial == self._apply_serial and self._apply_settings())

    @objc.python_method
    def _design_slider_changed(self, control, sender):
        event = AppKit.NSApp.currentEvent()
        if event is not None and event.type() == AppKit.NSEventTypeLeftMouseDragged:
            stops = tuple(stop * control.scale for stop in control.stops)
            value = effects.snap_detent(sender.doubleValue(), sender.minValue(), sender.maxValue(), stops)
            sender.setDoubleValue_(value)
        self._refresh_design_readouts()
        # A still is the same frame of its effect at every Length, so only Size redraws the tiles.
        if control is not self.length_select:
            for preview in self.effect_stills:
                preview.setNeedsDisplay_(True)
        self._changed()

    @objc.python_method
    def _refresh_design_readouts(self):
        pace = self.length_select.numeric_value()
        fraction = self.size_select.numeric_value()
        style = self.glow_style_select.value or "glow"
        if self.style_for_select.value == "switch" and self.landing_box.value:
            style = effects.switch_effect(self.switch_style_select.value or "match", style).id
        main_screen = AppKit.NSScreen.mainScreen()
        screen = main_screen.frame().size if main_screen else AppKit.NSMakeSize(1728, 1084)
        length_text, size_text = effects.design_slider_readouts(style, pace, fraction, screen.width, screen.height)
        self.length_readout.set(length_text)
        self.size_readout.set(size_text)
        self.length_select.set_accessible_value(length_text)
        self.size_select.set_accessible_value(size_text)

    def windowWillClose_(self, _notification):
        self._remember_normal_frame()
        if self.activation_policy_handler is not None:
            self.activation_policy_handler(window_open=False)
        try:
            with self.settings_store.lock:
                settings = dict(self.settings_store.current())
                settings["settings_window_frame"] = self._normal_frame
                self.settings_store.save_settings(settings)
        except SettingsError as exc:
            self.logger.warning("Could not save settings window frame: %s", exc)
        self.key_recorder.cancel()
        for row in self.crossing_ledger.rows.values():
            row.jump.cancel()
        self.ignored_recorder.cancel()
        # A code that stays up with nobody watching would still accept a pairing.
        self.panel.stop_showing()
        if self.previews is not None:
            self.tile_hover.stop()
            self.previews.stop()

    @objc.python_method
    def _head(self, title, figure):
        line = widgets.stack(spacing=12)
        widgets.add(line, widgets.eyebrow(title))
        widgets.add(line, figure, full_width=False)
        return line

    @objc.python_method
    def _numeral(self, unit):
        """A big mono figure and its unit. Returns (view, Figure)."""
        figure = widgets.Figure("0", theme.TYPE["numeral"], unit, tracking=theme.TIGHT["numeral"])
        return widgets.hug(figure.view), figure

    @objc.python_method
    def _page(self, title, purpose, scope=None, key=None):
        """One page: a vertical scroller holding the title, the sentence saying what the page is
        for, `scope` saying whose settings they are where that needs saying, then the modules the
        builder adds to the returned body. Returns (scroll, body)."""
        scroll = AppKit.NSScrollView.alloc().init()
        scroll.setTranslatesAutoresizingMaskIntoConstraints_(False)
        scroll.setDrawsBackground_(False)
        scroll.setHasVerticalScroller_(True)
        # Nothing scrolls sideways, at any width.
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutohidesScrollers_(False)
        scroll.setScrollerStyle_(AppKit.NSScrollerStyleLegacy)
        page = FlippedView.alloc().init()
        page.setBackgroundColor_(AppKit.NSColor.clearColor())
        page.setSelectionHighlightStyle_(AppKit.NSTableViewSelectionHighlightStyleNone)
        page.setAccessibilityRole_(AppKit.NSAccessibilityGroupRole)
        page.setAccessibilityLabel_(title)
        page.setTranslatesAutoresizingMaskIntoConstraints_(False)
        body = widgets.stack(spacing=20)
        page.addSubview_(body)
        top, leading, bottom, trailing = (24, 24, 24, 24)
        padding = [
            body.topAnchor().constraintEqualToAnchor_constant_(page.topAnchor(), top),
            body.leadingAnchor().constraintGreaterThanOrEqualToAnchor_constant_(page.leadingAnchor(), leading),
            page.bottomAnchor().constraintGreaterThanOrEqualToAnchor_constant_(body.bottomAnchor(), bottom),
            page.trailingAnchor().constraintGreaterThanOrEqualToAnchor_constant_(body.trailingAnchor(), trailing),
            body.centerXAnchor().constraintEqualToAnchor_(page.centerXAnchor()),
            body.widthAnchor().constraintLessThanOrEqualToConstant_(880),
        ]
        available_width = body.widthAnchor().constraintLessThanOrEqualToAnchor_constant_(
            page.widthAnchor(), -(leading + trailing)
        )
        column_width = body.widthAnchor().constraintEqualToConstant_(640)
        # Keep the fill preference below WindowSizeStayPut so page contents cannot cap the window.
        # Stronger than child fills (490), below native frame preservation (500).
        column_width.setPriority_(499)
        self.page_width_constraints[key] = column_width
        padding.append(available_width)
        padding.append(column_width)
        # The page ends where its body does, unless motion props it up with the floor while a
        # module folds away, so the scroll offset eases down rather than snapping. Only the
        # page is pulled short; pulling the body would stretch a module into the held space.
        page.floor = page.heightAnchor().constraintGreaterThanOrEqualToConstant_(0.0)
        page.floor.setActive_(True)
        shrink = page.heightAnchor().constraintEqualToConstant_(0.0)
        shrink.setPriority_(1)
        shrink.setActive_(True)
        AppKit.NSLayoutConstraint.activateConstraints_(padding)
        self.page_paddings[key] = padding[:4] + [available_width]
        scroll.setDocumentView_(page)
        clip = scroll.contentView()
        document_bridge = [
            page.topAnchor().constraintEqualToAnchor_(clip.topAnchor()),
            page.leadingAnchor().constraintEqualToAnchor_(clip.leadingAnchor()),
            page.widthAnchor().constraintEqualToAnchor_(clip.widthAnchor()),
        ]
        for constraint in document_bridge:
            # A scrolling document must not contribute its fitting size to the outer window.
            constraint.setPriority_(490)
        AppKit.NSLayoutConstraint.activateConstraints_(document_bridge)
        header = widgets.stack(spacing=6)
        heading = widgets.Label(title, theme.PAGE_TITLE, 700, tracking=-0.01)
        self.page_titles[key] = heading
        widgets.add(header, heading.view)
        purpose_label = widgets.Label(purpose, theme.TYPE["body"], ink="ink_2", wrap=True)
        if key == "crossing":
            self.crossing_intro = purpose_label
        purpose_label.view.widthAnchor().constraintLessThanOrEqualToConstant_(theme.READING_WIDTH).setActive_(True)
        widgets.add(header, purpose_label.view)
        if scope is not None:
            # ink_3, not signal: signal means something is happening, and on the light palette a
            # line of it reads as a link.
            scope_label = widgets.Label(scope, theme.TYPE["note"], ink="ink_3", wrap=True)
            scope_label.view.widthAnchor().constraintLessThanOrEqualToConstant_(theme.READING_WIDTH).setActive_(True)
            widgets.add(header, scope_label.view)
            self.scope_labels[key] = scope_label
        widgets.add(body, header)
        body.setCustomSpacing_afterView_(32, header)
        return scroll, body

    @objc.python_method
    def _overview_page(self, body):
        # Pairing stays at the top even when machines are already paired.
        self.overview_body = body
        widgets.add(body, self.panel.machines_module.view)
        widgets.add(body, self.panel.own_name_module().view)
        widgets.add(body, self.panel.sheet.view)
        widgets.add(body, self._dock_module().view)
        widgets.add(body, self._status_module().view)
        widgets.add(body, self._keyboard_module().view)
        widgets.add(body, self._same_module().view)
        widgets.add(body, self._login_module().view)
        widgets.add(body, self._updates_module().view)

    @objc.python_method
    def _rename_own(self, text):
        raw = copy.deepcopy(self.settings_store.current())
        raw["name"] = text.strip()[:protocol.MAX_NAME_CHARS]
        raw["name_set_at"] = settings_sync.next_stamp(raw.get("name_set_at", 0), time.time())
        raw["name_by"] = self.own_id()
        try:
            saved = self.settings_store.save_settings(raw)
        except (SettingsError, TypeError, ValueError) as exc:
            self.logger.exception("This Mac's name could not be saved")
            self._say(tr("Could not save this Mac's name: {error}").format(error=exc), "fault")
            return
        cfg = self.settings_store._config(saved)
        self.controller.apply_settings(cfg)
        display = saved.get("name", "") or socket.gethostname().split(".")[0] or tr("Mac")
        self.panel._own_rename_views[0].setStringValue_(display)
        if settings_sync.group_on(cfg, "names"):
            self._send_same()

    @objc.python_method
    def _rename_peer(self, token, text):
        raw = copy.deepcopy(self.settings_store.current())
        entry = next((item for item in raw["peers"] if item.get("token") == token), None)
        if entry is None:
            return
        entry.update(settings_sync.rename_peer(entry, text, self.own_id(), time.time()))
        try:
            saved = self.settings_store.save_settings(raw)
        except (SettingsError, TypeError, ValueError) as exc:
            self.logger.exception("A machine's name could not be saved")
            self._say(tr("Could not save the machine's name: {error}").format(error=exc), "fault")
            return
        self.controller.apply_settings(self.settings_store._config(saved))
        self.panel.refresh()
        if settings_sync.group_on(self.controller.cfg, "names"):
            self._send_same()

    @objc.python_method
    def _place_machines(self, first):
        """Keep the primary pairing action immediately below the Overview title."""
        if self._machines_first is True:
            return
        self._machines_first = True
        body = self.overview_body
        views = (self.panel.machines_module.view, self.panel.sheet.view)
        for view in views:
            body.removeArrangedSubview_(view)
        at = 1
        for offset, view in enumerate(views):
            body.insertArrangedSubview_atIndex_(view, at + offset)

    @objc.python_method
    def _same_module(self):
        module = widgets.Module()
        module.add(widgets.eyebrow(tr("Settings")))
        module.add(widgets.label(tr("Same on all machines"), theme.TYPE["body"]))
        self.same_checks = {}
        for key, title in (("crossing", N_("Crossing")), ("design", N_("Design")),
                           ("shortcut", N_("Shortcut")), ("names", N_("Names"))):
            button = AppKit.NSButton.alloc().initWithFrame_(((0, 0), (0, 0)))
            button.setTranslatesAutoresizingMaskIntoConstraints_(False)
            button.setFont_(theme.font(theme.TYPE["body"]))
            button.setButtonType_(AppKit.NSButtonTypeSwitch)
            button.setTitle_(tr(title))
            button.setTarget_(self)
            button.setAction_("sameGroupsChanged:")
            button.setTag_(("crossing", "design", "shortcut", "names", "everything").index(key))
            module.add(button, full_width=False)
            self.same_checks[key] = button
        module.add(widgets.note(tr(
            "Crossing shares resistance and drag protection. Design shares effects, colour, length, size and arrival animation. "
            "Shortcut shares shortcut on/off, trigger key and style, and destination. Names shares this machine's name and paired-machine labels."
        )).view)
        button = AppKit.NSButton.alloc().initWithFrame_(((0, 0), (0, 0)))
        button.setTranslatesAutoresizingMaskIntoConstraints_(False)
        button.setFont_(theme.font(theme.TYPE["body"]))
        button.setButtonType_(AppKit.NSButtonTypeSwitch)
        button.setTitle_(tr("Everything"))
        button.setTarget_(self)
        button.setAction_("sameGroupsChanged:")
        button.setTag_(4)
        module.add(button, full_width=False)
        self.same_checks["everything"] = button
        self.same_everything_note = widgets.note(tr("Shares every group and can replace local values on this machine."))
        module.add(self.same_everything_note.view)
        self.same_old_note = widgets.note(tr("Older Beamer versions share known Crossing and Design settings without group choices."))
        module.add(self.same_old_note.view)
        self.same_revert = widgets.Button(tr("Revert"), self, "revertSame:", scale="small")
        module.add(self.same_revert.view)
        self.same_note = widgets.note()
        module.add(self.same_note.view)
        self.same_ways_note = widgets.note()
        module.add(self.same_ways_note.view)
        return module

    def sameGroupsChanged_(self, sender):
        keys = ("crossing", "design", "shortcut", "names", "everything")
        selected = [key for key in keys[:4] if self.same_checks[key].state() == AppKit.NSControlStateValueOn]
        if sender.tag() == 4:
            selected = list(settings_sync.groups_for_choice(
                ["everything"] if sender.state() == AppKit.NSControlStateValueOn else []))
        self._set_same_groups(selected)

    @objc.python_method
    def _own_note(self):
        """A note on a row this Mac keeps to itself, shown only while the pages are kept in step."""
        note = widgets.note(tr(pages.OWN_ROW))
        note.view.setHidden_(True)
        self.own_notes.append(note)
        return note.view

    @objc.python_method
    def _peer_too_old(self):
        """Whether a connected peer predates group-aware settings messages."""
        return (getattr(self.controller, "peer_settings", None) is False
                or self.windows_input is not None and self.windows_input.state == ServerState.CONNECTED
                and self.windows_input.peer_settings is False
                or any("layout" not in caps for caps in self._design_peer_caps().values()))

    @objc.python_method
    def _show_same(self):
        cfg = self.controller.cfg
        old = self._peer_too_old()
        groups = settings_sync.normalise_groups(cfg.same_groups)
        for key in settings_sync.ALL_GROUPS:
            self.same_checks[key].setState_(AppKit.NSControlStateValueOn if key in groups else AppKit.NSControlStateValueOff)
        self.same_checks["everything"].setState_(AppKit.NSControlStateValueOn if len(groups) == len(settings_sync.ALL_GROUPS) else AppKit.NSControlStateValueOff)
        self.same_everything_note.view.setHidden_(len(groups) != len(settings_sync.ALL_GROUPS))
        self.same_old_note.view.setHidden_(not old)
        snapshot = self.settings_store.current().get("same_before_sync")
        self.same_revert.view.setHidden_(not settings_sync.snapshot_available(snapshot))
        who = settings_sync.who(list(peerlist.labels(peerlist.desktops(self.controller.book.peers())).values()))
        on = cfg.same_on_both
        self.same_note.set(settings_sync.group_note(groups, on), ink="ink_2")
        self.same_note.view.setToolTip_(settings_sync.switch_detail("mac"))
        self.same_ways_note.set(tr("Ways: set on the Crossing page · {state}").format(
            state=tr("On") if profiles.same_ways_on(self.settings_store.current()) else tr("Off")))
        for key, label in self.scope_labels.items():
            text = settings_sync.scope(key, who, on, pages.SCOPE.get(key), "mac", groups=groups)
            if label.text != text:
                label.set(text)
        for note in self.own_notes:
            if note.view.isHidden() == on:
                motion.set_hidden(note.view, not on)
        self._refresh_design_follow()

    @objc.python_method
    def _set_same(self, on):
        """Turning it on carries across each of this Mac's Crossing and Design values that was chosen
        more recently than the group's; off, each end keeps what it has. Either way the PC follows,
        now if a link is up, else when one next comes up. A change of policy stamps no value."""
        cfg = self.controller.cfg
        raw = config_to_raw(cfg)
        # Each value keeps the stamp it held before this change of policy, taken once and stored.
        raw["same_stamps"] = settings_sync.stored_stamps(
            settings_sync.held_stamps(cfg.same_stamps, cfg.same_set_at, cfg.same_by))
        raw["same_set_at"] = settings_sync.next_stamp(raw["same_set_at"], time.time())
        raw["same_by"] = self.own_id()
        raw["same_groups"] = list(settings_sync.ALL_GROUPS) if on else []
        raw["same_on_both"] = bool(on)
        try:
            cfg = self.settings_store.save(raw)
        except SettingsError as exc:
            self.logger.warning("Same on all machines not saved: %s", exc)
            return
        self.controller.apply_settings(cfg)
        self._send_same()
        if not on:
            self._apply_followed_design(force=True)
        self._show_same()

    @objc.python_method
    def _set_same_groups(self, groups):
        cfg = self.controller.cfg
        raw = config_to_raw(cfg)
        # Each value keeps the stamp it held before this change of policy, taken once and stored.
        raw["same_stamps"] = settings_sync.stored_stamps(
            settings_sync.held_stamps(cfg.same_stamps, cfg.same_set_at, cfg.same_by))
        raw["same_set_at"] = settings_sync.next_stamp(raw["same_set_at"], time.time())
        raw["same_by"] = self.own_id()
        raw["same_groups"] = list(settings_sync.normalise_groups(groups))
        raw["same_on_both"] = bool(raw["same_groups"])
        try:
            cfg = self.settings_store.save(raw)
        except SettingsError as exc:
            self.logger.warning("Same groups not saved: %s", exc)
            return
        self.controller.apply_settings(cfg)
        self._send_same()
        if not settings_sync.group_on(cfg, "design"):
            self._apply_followed_design(force=True)
        self._show_same()

    def revertSame_(self, _sender):
        stored = copy.deepcopy(self.settings_store.current())
        restored = settings_sync.restore_snapshot(stored.get("same_before_sync"))
        if restored is None:
            return
        values, conflict = core_ways.shared_trigger_values(stored, restored["values"])
        if conflict:
            self._show_trigger_conflict(conflict)
        cfg = self.controller.cfg
        raw = config_to_raw(cfg)
        before = settings_sync.mac_values(raw)
        design_stamp = settings_sync.design_change_stamp(before, values, cfg.design_set_at, None, self.own_id())
        raw = settings_sync.apply_mac(raw, values)
        if design_stamp is not None:
            raw["design_set_at"], raw["design_by"] = design_stamp["set_at"], design_stamp["by"]
        # The values put back are a choice made here now: each is stamped, the rest keep theirs.
        held = settings_sync.held_stamps(cfg.same_stamps, cfg.same_set_at, cfg.same_by)
        stamps, _stamp = settings_sync.stamp_changes(held, before, settings_sync.mac_values(raw), self.own_id(),
                                                     floor=max(cfg.same_set_at, cfg.design_set_at))
        raw["same_stamps"] = settings_sync.stored_stamps(stamps)
        names = settings_sync.restamp_names(stored, restored.get("names", {}), self.own_id())
        settings_sync.apply_names(stored, names)
        raw["same_groups"] = settings_sync.revert_groups(stored["same_before_sync"], cfg.same_groups)
        raw["same_on_both"] = bool(raw["same_groups"])
        raw["same_set_at"] = settings_sync.next_stamp(max(raw["same_set_at"], raw["design_set_at"]), time.time())
        raw["same_by"] = self.own_id()
        try:
            cfg = self.settings_store.save(raw, settings_changes=stored,
                                           remove_settings=("same_before_sync",))
        except (SettingsError, TypeError, ValueError):
            self.logger.exception("Same settings could not be reverted")
            return
        self.controller.apply_settings(cfg)
        self._load_shared(config_to_raw(cfg))
        self.panel.refresh()
        self._send_same()
        self._show_same()

    @objc.python_method
    def same_state(self):
        """This Mac's settings message, for a change here and for announcing on a new link. Called
        from the links' threads as well, so it reads the settings once."""
        cfg = self.controller.cfg
        values = settings_sync.mac_values(config_to_raw(cfg))
        design_state = {
            "set_at": cfg.design_set_at,
            "by": cfg.design_by or self.own_id(),
            "values": {key: values[key] for key in settings_sync.DESIGN_KEYS if key in values},
            "path": (self._design_path or [self.own_id()])
                    if cfg.design_follow_peer and not settings_sync.group_on(cfg, "design") else [self.own_id()],
        }
        stored = self.settings_store.current()
        layout_tiles = copy.deepcopy(stored.get("layout", {}))
        for entry in peerlist.desktops(stored.get("peers", [])):
            tile = layout_tiles.get(entry.get("id"))
            name = entry.get("name", "")
            if isinstance(tile, dict) and isinstance(name, str) and name.strip():
                tile["name"] = name.strip()[:64]
        layout_state = {"set_at": stored.get("layout_set_at", 0),
                        "by": stored.get("layout_by") or self.own_id(),
                        "tiles": layout_tiles}
        return settings_sync.message_data(cfg.same_on_both, cfg.same_set_at,
                                          values, by=cfg.same_by or self.own_id(), design_state=design_state,
                                          names=settings_sync.names_from_settings(self.settings_store.current()),
                                          layout_state=layout_state, groups=cfg.same_groups,
                                          stamps=settings_sync.held_stamps(cfg.same_stamps, cfg.same_set_at, cfg.same_by))

    @objc.python_method
    def _apply_followed_design(self, force=False):
        cfg = self.controller.cfg
        peer = cfg.design_follow_peer
        state = self._design_states.get(peer)
        if not peer or state is None or settings_sync.group_on(cfg, "design"):
            return
        current = settings_sync.mac_values(config_to_raw(cfg))
        cursor = self._followed_version
        stamp, author = cursor[1:] if cursor and cursor[0] == peer else (-1, "")
        taken = settings_sync.followed_design(current, state, peer, peer, settings_sync.group_on(cfg, "design"),
                                              stamp, author, own_id=self.own_id(), force=force)
        if taken is None:
            return
        cycle = taken.get("cycle", False)
        path = [self.own_id()] if cycle else taken["path"]
        announce = cycle or taken["changed"] or path != self._design_path or force
        raw = config_to_raw(cfg) if cycle else settings_sync.apply_mac(config_to_raw(cfg), taken["values"])
        if cycle:
            raw["design_follow_peer"] = ""
        if announce:
            raw["design_set_at"] = settings_sync.next_stamp(cfg.design_set_at, time.time())
            raw["design_by"] = self.own_id()
        try:
            cfg = self.settings_store.save(raw)
        except (SettingsError, TypeError, ValueError):
            self.logger.exception("could not save followed Design from %s", peer)
            return
        self.controller.apply_settings(cfg)
        self._design_path = path
        self._followed_version = None if cycle else (peer, taken["set_at"], taken["by"])
        self._load_shared(config_to_raw(cfg))
        if announce:
            self._send_same()
        self._refresh_design_follow()

    @objc.python_method
    def _receive_design(self, data, peer, apply=True):
        if peer is None:
            return
        peer_id = protocol.id_text(peer) if isinstance(peer, bytes) else peer
        if peer_id not in {entry.get("id") for entry in peerlist.desktops(self.controller.book.peers())}:
            return
        state = settings_sync.read_design_state(data, KEY_NAME_TO_CODE)
        if state is None:
            return
        held = self._design_states.get(peer_id)
        if held is None or settings_sync.design_arrived(state, held["set_at"], held["by"]):
            self._design_states[peer_id] = state
            self._refresh_design_follow()
        if apply and peer_id == self.controller.cfg.design_follow_peer:
            self._apply_followed_design()

    @objc.python_method
    def own_id(self):
        """This machine's id as a b64 text: who made a change to what is kept in step."""
        return self.settings_store.current()["machine_id"]

    @objc.python_method
    def _send_same(self, data=None, source=None):
        """This Mac's state to every machine that keeps it, or a newer state taken from `source`
        passed on, unchanged, to all the others."""
        data = data if data is not None else self.same_state()
        self.controller.send_settings(data, source=source)
        if self.windows_input is not None:
            self.windows_input.send_settings(data, source=source)

    @objc.python_method
    def apply_same(self, data, peer=None):
        """A peer's settings message, over either link, on the main thread. Its state stands only
        when it is newer than this Mac's; then its values replace the shared ones here, and it is
        sent on, unchanged, to every other machine that keeps it."""
        peer_id = protocol.id_text(peer) if isinstance(peer, bytes) else peer
        if peer_id is not None and peer_id not in {entry.get("id") for entry in peerlist.desktops(self.controller.book.peers())}:
            return
        if "desk" in data:
            self._receive_desk(data, peer_id)
            return
        settings = copy.deepcopy(self.settings_store.current())
        cfg = self.controller.cfg
        taken = settings_sync.arrived(data, cfg.same_set_at, KEY_NAME_TO_CODE, by_here=cfg.same_by,
                                      current_groups=cfg.same_groups)
        policy_groups = settings_sync.arrival_groups(data, cfg.same_groups) if taken is not None else cfg.same_groups
        policy_on = taken[0] if taken is not None else cfg.same_on_both
        current_names = settings_sync.names_from_settings(settings)
        updates, own_name = settings_sync.names_arrived(
            data, current_names, self.own_id(),
            current_groups=cfg.same_groups, policy_groups=policy_groups if policy_on else (),
        )
        names_changed = settings_sync.apply_names(settings, updates)
        name_snapshot = None
        if names_changed:
            labels_changed = any(current_names.get(ident, {}).get("label", "") != entry["label"]
                                 for ident, entry in updates.items())
            name_snapshot = settings_sync.same_snapshot(
                self.controller.cfg.same_groups, settings_sync.mac_values(config_to_raw(self.controller.cfg)),
                value_groups=(), names=current_names,
            ) if labels_changed else None
            if name_snapshot is not None:
                settings["same_before_sync"] = name_snapshot
            try:
                saved = self.settings_store.save_settings(settings)
            except (SettingsError, TypeError, ValueError):
                self.logger.exception("could not save machine names sent by %s", peer)
                return
            self.controller.apply_settings(self.settings_store._config(saved))
            self.panel.refresh()
            if own_name is not None and hasattr(self.panel, "_own_rename_views"):
                display = own_name or socket.gethostname().split(".")[0] or tr("Mac")
                self.panel._own_rename_views[0].setStringValue_(display)
        cfg = self.controller.cfg
        layout_applied = self._receive_layout(data, peer_id)
        was_design_on = settings_sync.group_on(cfg, "design")
        # Value by value: each shared value is taken when its own stamp is newer than the one held
        # here, whether or not the whole state is; the whole state decides only on, off and groups.
        merged = settings_sync.merge(data, cfg.same_stamps, cfg.same_set_at, KEY_NAME_TO_CODE, by_here=cfg.same_by,
                                     on_here=cfg.same_on_both, current_groups=cfg.same_groups)
        policy = merged["policy"] if merged is not None else None
        values, taken = {}, False
        if merged is not None and merged["values"]:
            values, conflict = core_ways.shared_trigger_values(self.settings_store.current(), merged["values"])
            if conflict is not None:
                self._show_trigger_conflict(conflict)
            # A value kept here over a clash is not taken: counted, every copy of the message would be
            # taken again and sent on, round three machines for ever.
            taken = any(values.get(key) == value for key, value in merged["values"].items())
        if merged is not None and (policy is not None or taken):
            on = merged["on"]
            raw = config_to_raw(cfg)
            old_groups = settings_sync.normalise_groups(cfg.same_groups)
            before_values = settings_sync.mac_values(raw)
            snapshot = settings_sync.snapshot_for_arrival(
                data, cfg.same_set_at, KEY_NAME_TO_CODE, cfg.same_by, old_groups, before_values,
                applied_values=values, merged=merged,
            )
            if snapshot is not None:
                if name_snapshot is not None:
                    snapshot["names"] = name_snapshot["names"]
                    snapshot.pop("selection_only", None)
                raw["same_before_sync"] = snapshot
            # A value kept here on a clash keeps its own stamp, so it is not taken as the sender's.
            held = settings_sync.held_stamps(cfg.same_stamps, cfg.same_set_at, cfg.same_by)
            held.update({key: stamp for key, stamp in merged["stamps"].items()
                         if key in values and values[key] == merged["values"][key]})
            raw["same_stamps"] = settings_sync.stored_stamps(held)
            if policy is not None:
                raw["same_groups"] = list(merged["groups"])
                raw["same_on_both"] = on
                raw["same_set_at"] = policy[1]
                raw["same_by"] = data.get("by", "")
            design_stamp = None
            if values:
                design_stamp = settings_sync.design_change_stamp(
                    settings_sync.mac_values(raw), values, cfg.design_set_at,
                    settings_sync.read_design_state(data, KEY_NAME_TO_CODE), self.own_id(),
                )
                raw = settings_sync.apply_mac(raw, values)
                if design_stamp is not None:
                    raw["design_set_at"] = design_stamp["set_at"]
                    raw["design_by"] = design_stamp["by"]
            try:
                cfg = self.settings_store.save(
                    raw, settings_changes={"same_before_sync": snapshot} if snapshot is not None else None,
                )
            except (SettingsError, TypeError, ValueError):
                self.logger.exception("could not save the settings %s sent", peer)
                return
            self.controller.apply_settings(cfg)
            # Keep global settings' existing forwarding, but replace the optional direct state
            # with this machine's own state instead of relaying the sender's state.
            forwarded = dict(data)
            forwarded["design_sync"] = self.same_state()["design_sync"]
            self._send_same(forwarded, source=peer)
            if self.previews is not None:
                self.previews.repaint()
            self.logger.info("settings from %s applied (same on all machines %s)", peer, "on" if on else "off")
            self._load_shared(config_to_raw(cfg))
            self.refresh()
        elif names_changed:
            self._send_same(data, source=peer)
        if merged is not None and merged["answer"]:
            # This Mac holds a value newer than the sender's: its own state, so the sender learns it.
            self._send_same()
        resuming = policy is not None and was_design_on and not settings_sync.group_on(cfg, "design")
        self._receive_design(data, peer, apply=not resuming)
        if resuming:
            self._apply_followed_design(force=True)

    @objc.python_method
    def _receive_layout(self, data, peer):
        if peer is None or "layout" not in self._design_peer_caps().get(peer, ()):
            return False
        state = settings_sync.read_layout_state(data)
        if state is None:
            return False
        outcome = self.controller.crossing_event(crossing_state.receive_picture, state, source=peer,
            arrived=data, own_displays=self.controller.crossing_display_map(), **self._crossing_args())
        if not outcome["changed"]:
            return False
        self._load_ways()
        self.refresh()
        return True

    @objc.python_method
    def _receive_desk(self, data, peer):
        if peer is None or "profiles" not in self._design_peer_caps().get(peer, ()):
            return False
        outcome = self.controller.receive_desk(data, peer, linked=self._live(peer),
            live=self._live, gone=self._gone, now=time.time())
        if outcome is None:
            return False
        self._load_ways()
        self._show_arrangement()
        self.refresh()
        return outcome["changed"]

    @objc.python_method
    def _login_module(self):
        module = widgets.Module()
        module.add(widgets.eyebrow(tr("At login")))
        self.login_switch = widgets.Switch(tr("Start Beamer when you log in"), on_change=self._set_login)
        module.add(self.login_switch.view)
        self.login_note = widgets.note()
        module.add(self.login_note.view)
        self.login_approval_button = widgets.Button(
            tr("Open Login Items settings"), self, "openLoginItemsSettings:", full_width=True
        )
        self.login_approval_button.view.setHidden_(True)
        module.add(self.login_approval_button.view)
        self.login_bundle_path = AppKit.NSBundle.mainBundle().bundlePath()
        self._show_login_state()
        return module

    @objc.python_method
    def _dock_module(self):
        module = widgets.Module()
        module.add(widgets.eyebrow(tr("Dock and menu bar")))
        self.dock_switch = widgets.Switch(tr("Show in Dock"), on_change=self._set_show_in_dock)
        self.tray_switch = widgets.Switch(tr("Show in menu bar"), on_change=self._set_show_in_tray)
        module.add(self.dock_switch.view)
        module.add(self.tray_switch.view)
        module.add(widgets.note(tr(
            "For menu-bar-only use, turn off Show in Dock and close this window. "
            "Beamer stays in the menu bar. Its Dock icon appears while this window is open."
        )).view)
        self._reflect_visibility()
        return module

    @objc.python_method
    def _reflect_visibility(self):
        """Both switches from the saved choice. The menu bar item shows whenever the Dock icon is
        hidden, so with the Dock off its switch reads on and is dimmed; with the menu bar off the
        Dock's is."""
        settings = self.settings_store.current()
        show_in_dock = settings.get("show_in_dock", True)
        show_in_menu_bar = dock_visibility.menu_bar_visible(show_in_dock, settings.get("show_in_tray", True))
        self.dock_switch.value = show_in_dock
        self.tray_switch.value = show_in_menu_bar
        self.dock_switch.set_enabled(show_in_menu_bar)
        self.tray_switch.set_enabled(show_in_dock)

    @objc.python_method
    def _save_visibility(self, name, on):
        """Writes the Dock or menu bar choice, refusing the one that would hide both."""
        try:
            with self.settings_store.lock:
                settings = copy.deepcopy(self.settings_store.current())
                # A hand-edited file with both off already reads as the menu bar on.
                settings["show_in_tray"] = dock_visibility.menu_bar_visible(settings["show_in_dock"], settings["show_in_tray"])
                settings[name] = bool(on)
                if not settings["show_in_dock"] and not settings["show_in_tray"]:
                    self._reflect_visibility()
                    return
                self.settings_store.save_settings(settings)
        except (SettingsError, TypeError, ValueError) as exc:
            self.logger.warning("%s not saved: %s", name, exc)
        self._reflect_visibility()
        if self.activation_policy_handler is not None:
            self.activation_policy_handler()

    @objc.python_method
    def _set_show_in_dock(self, on):
        self._save_visibility("show_in_dock", on)

    @objc.python_method
    def _set_show_in_tray(self, on):
        self._save_visibility("show_in_tray", on)

    @objc.python_method
    def _updates_module(self):
        module = widgets.Module()
        module.add(widgets.eyebrow(tr("Updates")))
        self.updates_switch = widgets.Switch(tr("Automatically check for updates"), on_change=self._set_check_updates)
        self.updates_switch.value = self.controller.cfg.check_updates
        module.add(self.updates_switch.view)
        module.add(widgets.note(tr(
            "Checks GitHub at startup and once a day. Updates are downloaded and installed by you."
        )).view)
        self.check_updates_button = widgets.Button(tr("Check for updates"), self, "checkForUpdates:", full_width=True)
        module.add(self.check_updates_button.view)
        self.update_status = widgets.note("")
        module.add(self.update_status.view)
        self.update_button = widgets.Button(tr("Download"), self, "downloadUpdate:", style="primary", full_width=True)
        self.update_button.view.setHidden_(True)
        module.add(self.update_button.view)
        self.update_page_notes = widgets.Button(tr("What's new"), self, "downloadUpdate:", full_width=True)
        self.update_page_notes.view.setHidden_(True)
        module.add(self.update_page_notes.view)
        self.update_url = None
        return module

    @objc.python_method
    def _set_check_updates(self, on):
        try:
            cfg = self.settings_store.save(config_to_raw(replace(self.controller.cfg, check_updates=bool(on))))
        except SettingsError as exc:
            self.logger.warning("update checking not saved: %s", exc)
            self.updates_switch.value = self.controller.cfg.check_updates
            return
        self.controller.update_config(cfg)
        if on and self.update_checker is not None:
            self.update_checker.check_now()
        if not on:
            (self.update_handler or self.show_update)(None)

    @objc.python_method
    def show_update(self, found, manual=False):
        """`found` is a core Release for a newer version, or None."""
        if not manual and not self.controller.cfg.check_updates:
            found = None
        self._manual_update = manual
        self._update = found
        self.update_url = found[1] if found else None
        self.update_status.set(tr("Beamer {version} is available.").format(version=found[0]) if found else "")
        motion.set_hidden(self.update_page_notes.view, found is None)
        if found:
            self.update_button.set_title(tr("Download Beamer {version}").format(version=found[0]))
        motion.set_hidden(self.update_button.view, found is None)
        self._refresh_update_notice()

    def checkForUpdates_(self, _sender):
        if self.update_checker is not None:
            self.show_update_check("checking", None)
            self.update_checker.check_now(manual=True)

    @objc.python_method
    def show_update_check(self, state, found):
        self.check_updates_button.set_enabled(state != "checking")
        text = {"checking": tr("Checking for updates…"),
                "current": tr("Beamer is up to date."),
                "available": tr("Update available."),
                "failed": tr("Could not check for updates. Check your connection and try again.")}[state]
        self.update_status.set(text)
        if state in ("available", "current"):
            (self.update_handler or self.show_update)(found, manual=True)
            if state == "current":
                self.update_status.set(text)

    @objc.python_method
    def _refresh_update_notice(self):
        found = updates.visible_notice(self._update, self.controller.cfg.check_updates,
                                       self.settings_store.current().get("dismissed_update_version", ""),
                                       requested=getattr(self, "_manual_update", False))
        if found:
            self.update_notice_text.set(tr("Beamer {version} is available.").format(version=found[0]))
        self.update_notice_width.setActive_(bool(found))
        self.update_notice.view.setHidden_(not bool(found))

    def dismissUpdate_(self, _sender):
        if not self._update:
            return
        try:
            cfg = self.settings_store.save(config_to_raw(self.controller.cfg),
                                           settings_changes={"dismissed_update_version": self._update[0]})
        except SettingsError as exc:
            self.logger.warning("Update dismissal could not be saved: %s", exc)
            return
        self._manual_update = False
        self.controller.update_config(cfg)
        if self.update_handler:
            self.update_handler(self._update)
        else:
            self._refresh_update_notice()

    def downloadUpdate_(self, _sender):
        if self.update_url:
            AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.URLWithString_(self.update_url))

    @objc.python_method
    def _show_login_state(self, refused=None):
        try:
            state = login_item.status()
        except Exception:
            self.logger.exception("could not read the login item")
            state = login_item.NOT_FOUND
        self.login_switch.value = state in (login_item.ENABLED, login_item.REQUIRES_APPROVAL)
        in_applications = login_item.installed_in_applications(self.login_bundle_path)
        self.login_switch.set_enabled(login_item.can_toggle(state, self.login_bundle_path))
        if refused:
            # Translators: {reason} is the system's reason for refusing the login item.
            self.login_note.set(tr("macOS refused: {reason}").format(reason=refused))
        elif state == login_item.REQUIRES_APPROVAL:
            if in_applications:
                self.login_note.set(
                    tr("Login item status: Requires approval. Allow Beamer in System Settings, General, Login Items.")
                )
            else:
                self.login_note.set(
                    tr("Login item status: Requires approval. Move Beamer to Applications and reopen it before allowing it in Login Items.")
                )
        elif not in_applications:
            self.login_note.set(
                tr("Login item status: {status}. Move Beamer to Applications and open it there to start at login.").format(status=login_item.status_text(state))
            )
        elif state == login_item.NOT_FOUND:
            self.login_note.set(
                tr("Login item status: Not found. Check Beamer is installed in Applications, then turn it on again.")
            )
        elif state == login_item.ENABLED:
            self.login_note.set(tr("Login item status: Enabled. Beamer will start when you log in."))
        elif state == login_item.NOT_REGISTERED:
            self.login_note.set(
                tr("Login item status: Not registered. Turn on Start Beamer when you log in.")
            )
        else:
            self.login_note.set(tr("Login item status: {status}.").format(status=login_item.status_text(state)))
        self.login_approval_button.view.setHidden_(state != login_item.REQUIRES_APPROVAL)

    def openLoginItemsSettings_(self, _sender):
        try:
            login_item.open_system_settings()
        except Exception as exc:
            self.logger.warning("could not open Login Items settings: %s", exc)
            self.login_note.set(tr("Could not open Login Items settings: {reason}").format(reason=exc))

    @objc.python_method
    def _set_login(self, enabled):
        if enabled and not login_item.installed_in_applications(self.login_bundle_path):
            self._show_login_state()
            return
        try:
            login_item.set_enabled(enabled)
        except OSError as exc:
            self.logger.warning("could not change the login item: %s", exc)
            self._show_login_state(refused=exc)
            return
        self._show_login_state()

    @objc.python_method
    def _crossing_page(self, body):
        # The ledger's Keys cells use the global shortcut's recorder.
        self.shortcut_module = self._shortcut_module()
        self.resistance_module = self._resistance_module()
        self.viewed_machine = self.own_id()
        machines = self._ways_module()
        self.pointer_module = self._pointer_module()
        for module in (machines, self.own_ways_module, self.ways_ledger_module, self._home_module(), self.pointer_module,
                       self.resistance_module, self.shortcut_module):
            widgets.add(body, module.view)
        own_width = self.own_ways_module.view.widthAnchor().constraintEqualToAnchor_(self.ways_ledger_module.view.widthAnchor())
        own_width.setPriority_(999)
        own_width.setActive_(True)
        # The table must fill the page before its notes' preferred reading width is solved.
        for constraint in body.constraints():
            if constraint.firstItem() == machines.view and constraint.firstAttribute() == AppKit.NSLayoutAttributeWidth:
                constraint.setPriority_(499)

    @objc.python_method
    def _design_page(self, body):
        self.previews = previews.PreviewLoop(self.controller, body)
        self.tile_hover = previews.TileHover(self.previews)
        self.effect_stills = []
        self.classic_pictures = []
        self._stills = None
        # A bundle missing the effects' modules still gets a working Design page, with today's
        # styles and colours only; the overlay logs the failure and today's glow draws everything.
        self.effects_ready = pages.effects_load_error() is None
        self._look = None
        self._place_chosen = False
        self.style_module = self._edge_module()
        self.colour_module = self._colour_module()
        for module in (self._on_screen_module(), self.style_module, self.colour_module,
                       self._trackpad_module(), self._appearance_module()):
            widgets.add(body, module.view)
        self.follow_module = widgets.Module()
        self.follow_module.add(widgets.eyebrow(tr("Match design with")))
        self.follow_host = widgets.stack(spacing=0)
        self.follow_module.add(self.follow_host)
        self.follow_note = widgets.note()
        self.follow_module.add(self.follow_note.view)
        widgets.add(body, self.follow_module.view)
        self._refresh_design_follow()

    @objc.python_method
    def _design_peer_caps(self):
        desktops = {entry.get("id") for entry in peerlist.desktops(self.controller.book.peers())}
        caps = {peer: link.caps for peer, link in list(self.controller._peers_up.items()) if peer in desktops and link.live()}
        if self.windows_input is not None:
            server = self.windows_input.server
            for peer in server.links():
                if protocol.id_text(peer) not in desktops:
                    continue
                known = server.caps_of(peer)
                if known is not None:
                    caps[protocol.id_text(peer)] = known
        return caps

    @objc.python_method
    def _refresh_design_follow(self):
        if not hasattr(self, "follow_host"):
            return
        peers = peerlist.desktops(self.settings_store.current()["peers"])
        labels = peerlist.labels(peers)
        choices = [("", tr("None"))]
        caps = self._design_peer_caps()
        rows = settings_sync.follow_rows(peers, caps, self._design_states, self.controller.cfg.design_follow_peer)
        unsupported = {entry["id"]: why_not for entry, why_not in rows if why_not is not None}
        for entry, why_not in rows:
            title = labels.get(entry.get("token", "")) or tr("Machine")
            if why_not == settings_sync.NEEDS_DESIGN_SYNC:
                title += f" — {why_not}"
            choices.append((entry["id"], title))
        menu_key = (tuple(choices), tuple(sorted(unsupported.items())))
        if menu_key != getattr(self, "_follow_menu_key", None):
            for view in self.follow_host.arrangedSubviews():
                self.follow_host.removeArrangedSubview_(view)
                view.removeFromSuperview()
            self.follow_select = widgets.MenuChoice(choices, on_change=self._set_design_follow)
            for value, cell in self.follow_select.items:
                cell.setEnabled_(value not in unsupported)
                cell.setToolTip_(unsupported[value] if value in unsupported else cell.title())
            widgets.add(self.follow_host, self.follow_select.view, full_width=False)
            self._follow_menu_key = menu_key
        self.follow_select.value = self.controller.cfg.design_follow_peer
        peer = self.controller.cfg.design_follow_peer
        entry = next((item for item in peers if item.get("id") == peer), {}) if peer else {}
        name = labels.get(entry.get("token", "")) or "that machine"
        if peer:
            if settings_sync.group_on(self.controller.cfg, "design"):
                note = f"Following {name}, paused while Same on all machines is on."
            elif peer not in caps:
                note = f"Keeping the last design received from {name} until it reconnects."
            else:
                note = ""
        else:
            note = settings_sync.follow_note(rows) or "None: this machine keeps its own Design."
        self.follow_note.set(note)
        self.follow_note.view.setHidden_(not note)
        self._lock_followed_design(settings_sync.follow_lock(peer, settings_sync.group_on(self.controller.cfg, "design"), peers), name)

    @objc.python_method
    def _set_design_follow(self, peer):
        raw = config_to_raw(self.controller.cfg)
        raw["design_follow_peer"] = peer or ""
        if not peer:
            raw["design_set_at"] = settings_sync.next_stamp(raw["design_set_at"], time.time())
            raw["design_by"] = self.own_id()
        try:
            cfg = self.settings_store.save(raw)
        except (SettingsError, TypeError, ValueError):
            self.logger.exception("could not save Match design with")
            return
        self.controller.apply_settings(cfg)
        self._followed_version = None
        if not peer:
            self._design_path = [self.own_id()]
            self._send_same()
        self._refresh_design_follow()
        self._apply_followed_design(force=True)

    @objc.python_method
    def _appearance_module(self):
        """The settings window's own palette. Last on the page: it is chosen once, and the modules
        above are what the page is for."""
        module = widgets.Module()
        module.add(widgets.eyebrow(tr("Appearance")))
        module.add(self._own_note())
        self.appearance_select = widgets.Segmented(
            [("system", tr("System")), ("light", trc("appearance", "Light")), ("dark", tr("Dark"))], on_change=self._appearance_chosen
        )
        module.add(widgets.field_row(tr("This window"), self.appearance_select.view)[0])
        module.add(widgets.note(tr("System follows your Mac's light or dark setting.")).view)
        return module

    @objc.python_method
    def _appearance_chosen(self, *_ignored):
        # At once, not after _changed's pause for the settings to settle: the choice is the change.
        self._apply_appearance(self.appearance_select.value)
        self._changed()

    @objc.python_method
    def _apply_appearance(self, choice=None):
        """Brings the window to the palette the appearance setting and the Mac ask for, cross-faded
        while it is on screen. Called for the setting and whenever the Mac switches."""
        choice = choice or self.appearance_select.value or self.controller.cfg.appearance
        dark = theme.wants_dark(choice, theme.system_dark())
        if dark == theme.is_dark():
            return
        content = self.window.contentView()
        motion.cross_fade(content)
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        theme.set_dark(dark)
        self.window.setAppearance_(theme.appearance_named(dark))
        theme.repaint(content)
        # _select_page takes every page but the open one out of the window, so the walk above
        # never reaches them; their layers would keep the old palette's CGColors while their text,
        # which resolves as it draws, took the new one (05-10-2026, Light over dark cards).
        for scroll in self.pages.values():
            if scroll.superview() is None:
                theme.repaint(scroll)
        Quartz.CATransaction.commit()
        if self.previews is not None:
            # The renderers take the palette as they draw, which a held still does not do again.
            self.previews.repaint()

    @objc.python_method
    def _keyboard_page(self, body):
        module = widgets.Module()
        self.mouse_only_box = widgets.Switch(tr("Mouse only"), on_change=self._changed)
        module.add(widgets.eyebrow(tr("Keyboard sharing")))
        module.add(self.mouse_only_box.view)
        module.add(widgets.note(tr("Keep this machine's keyboard here. Pointer and clipboard sharing stay on.")).view)
        widgets.add(body, module.view)
        self.keyboard_sync_box = widgets.Switch(tr("Synchronise Russian and English keyboard layouts"), on_change=self._changed)
        module.add(self.keyboard_sync_box.view)
        module.add(widgets.note(tr("Enable on both computers. A bare Fn tap on this Mac switches RU/EN while controlling Windows. Alt+Shift also works.")).view)
        widgets.add(body, self._modifier_module().view)
        widgets.add(body, self._ignored_module().view)
        widgets.add(body, self._speed_module().view)

    @objc.python_method
    def _speed_module(self):
        """How the other machine's mouse feels on this Mac: it sends what its own acceleration made of the
        hand's movement, and this Mac's settings decide the rest."""
        module = widgets.Module()
        figure, self.pointer_numeral = self._numeral("%")
        module.add(self._head(tr("Another machine's pointer here"), figure))
        self.pointer_ruler = widgets.Ruler(
            25, 400, (25, 100, 200, 300, 400), step=5, minor=25, on_change=self._speed_moved,
            title=tr("Pointer speed"), arrow_step=25,
        )
        module.add(self.pointer_ruler.view)
        module.add(widgets.note(tr("Pointer speed for another machine's mouse or trackpad while it drives this Mac.")).view)
        figure, self.scroll_numeral = self._numeral("%")
        module.add(self._head(tr("Scrolling"), figure))
        self.scroll_ruler = widgets.Ruler(
            25, 400, (25, 100, 200, 300, 400), step=5, minor=25, on_change=self._speed_moved,
            title=tr("Scroll speed"), arrow_step=25,
        )
        module.add(self.scroll_ruler.view)
        self.reverse_scroll_box = widgets.Switch(tr("Reverse other machines' scrolling"), on_change=self._changed)
        module.add(self.reverse_scroll_box.view)
        return module

    @objc.python_method
    def _speed_moved(self, _value=None):
        self.pointer_numeral.set(str(self.pointer_ruler.value))
        self.scroll_numeral.set(str(self.scroll_ruler.value))
        self._changed()

    @objc.python_method
    def _connection_page(self, body):
        widgets.add(body, self._connection_module().view)

    @objc.python_method
    def _permissions_page(self, body):
        widgets.add(body, self._access_module().view)

    @objc.python_method
    def _status_module(self):
        module = widgets.Module()
        head = widgets.stack(vertical=False, spacing=7)
        head.addArrangedSubview_(widgets.hug(widgets.eyebrow(tr("Link")), AppKit.NSLayoutPriorityDefaultLow))
        self.link_led = widgets.LED()
        head.addArrangedSubview_(self.link_led.view)
        self.link_tag = widgets.Label("", theme.TYPE["eyebrow"], 700, "ink_3", tracking=theme.TRACKING["eyebrow"], upper=True)
        head.addArrangedSubview_(self.link_tag.view)
        module.add(head)
        self.state_word = widgets.Label("", theme.TYPE["status_word"], 700, tracking=theme.TIGHT["status_word"])
        widgets.squeeze(self.state_word.view)
        module.add(self.state_word.view)
        self.state_detail = widgets.Label("", theme.TYPE["body"], ink="ink_2", wrap=True)
        # Two lines reserved, so a one-line state does not pull the readouts up and down.
        self.state_detail.view.heightAnchor().constraintGreaterThanOrEqualToConstant_(36).setActive_(True)
        module.add(self.state_detail.view)
        self.spark = widgets.size(widgets.flipped(widgets.Spark), height=14)
        # The time a move takes to reach the PC and be answered, while input is there.
        self.round_trip = widgets.Readout(tr("Delay"), "ms", self.spark)
        self.peer_footer = widgets.Label("", theme.TYPE["small"], mono=True, ink="ink_3")
        widgets.squeeze(self.peer_footer.view)
        self.peer = widgets.Readout(tr("Machine"), "", self.peer_footer.view, sizes=(theme.PEER_SIZE, theme.PEER_SIZE_NARROW))
        module.add(widgets.grid([self.round_trip.view, self.peer.view], 2))
        return module

    @objc.python_method
    def _keyboard_module(self):
        """Sending input by hand and pausing the edge, together as on the PC. Where input is now is
        the status word above; a MAC / WINDOWS indicator here only said it twice."""
        module = widgets.Module(spacing=12)
        module.add(widgets.eyebrow(tr("Keyboard and pointer")))
        self.toggle_button = widgets.Button(
            tr("Send input"), self, "toggleRedirect:", style="primary", scale="big", full_width=True
        )
        module.add(self.toggle_button.view)
        self.pause_row = widgets.stack(spacing=8)
        self.pause_button = widgets.Button(tr("Pause crossing"), self, "togglePause:", full_width=True)
        widgets.add(self.pause_row, self.pause_button.view)
        self.crossing_state = widgets.note()
        widgets.add(self.pause_row, self.crossing_state.view)
        module.add(self.pause_row)
        module.body.setCustomSpacing_afterView_(14, self.toggle_button.view)
        self.hold_box = widgets.Switch(tr("Hold this Mac's edges while an app on it is full screen"), on_change=self._hold_changed)
        module.add(self.hold_box.view)
        module.add(self._own_note())
        self.hold_note = widgets.note(tr(
            "This screen decides, including when another machine drives it. On, a full-screen app here holds pointer crossings; shortcuts and jump keys still work. Off, another machine's pointer can go home or onward through the configured ways."
        ))
        module.add(self.hold_note.view)
        self.hold_box.view.setToolTip_(tr("This screen's setting applies to every pointer on it."))
        self.pointer_box = widgets.Switch(tr("Hide the pointer on the machine not in use"), on_change=self._set_hide_idle_pointer)
        self.pointer_box.value = self.controller.cfg.hide_idle_pointer
        module.add(self.pointer_box.view)
        module.add(widgets.note(tr(
            "This Mac's pointer always hides while it drives another machine. On, it also hides after another "
            "machine stops driving it, until its own mouse or trackpad moves."
        )).view)
        return module

    @objc.python_method
    def _set_hide_idle_pointer(self, on):
        try:
            cfg = self.settings_store.save(config_to_raw(replace(self.controller.cfg, hide_idle_pointer=bool(on))))
        except SettingsError as exc:
            self.logger.warning("pointer hiding not saved: %s", exc)
            self.pointer_box.value = self.controller.cfg.hide_idle_pointer
            return
        self.controller.update_config(cfg)
        if not on:
            self.controller.idle_pointer.cancel()

    @objc.python_method
    def _resistance_module(self):
        module = widgets.Module()
        figure, self.resistance_numeral = self._numeral("px")
        module.add(self._head(tr("Resistance"), figure))
        self.push_strip = diagram.PushStrip.alloc().init().setup()
        self.push_strip.on_change = self._preview_resistance
        self.resistance_ruler = self.push_strip
        module.add(self.push_strip)
        self.resistance_hint = widgets.note()
        module.add(self.resistance_hint.view)
        return module

    @objc.python_method
    def _ways_module(self):
        module = widgets.Module()
        self.profile_tabs = widgets.stack(vertical=False, spacing=6)
        self.profile_tabs.setDetachesHiddenViews_(True)
        self.profile_buttons = {}
        self._profile_tabs_key = None
        self.ways_switch = widgets.Switch(tr("Same ways on every machine"),
            on_change=lambda on: self._profile_event(crossing_state.set_same_ways, on))
        widgets.hug(self.ways_switch.view)
        widgets.hug(self.ways_switch.words.view)
        widgets.hug(self.ways_switch.view.subviews()[0], AppKit.NSLayoutPriorityDefaultHigh)
        widgets.hug(self.profile_tabs, AppKit.NSLayoutPriorityDefaultLow)
        self.profile_top = widgets.SideBySide(self.profile_tabs, self.ways_switch.view, 120, 260, gap=16)
        self.side_by_side.append(self.profile_top)
        module.add(self.profile_top.view)
        self.profile_actions = widgets.stack(vertical=False, spacing=12)
        self.new_profile_button = widgets._autolayout(ledger.quiet_button(tr("New profile…"), lambda: self._profile_name()))
        self.rename_profile_button = widgets._autolayout(ledger.quiet_button(tr("Rename"),
            lambda: self._profile_name(self.settings_store.current()["crossing_desk"]["active"]["value"])))
        # A click on a tab switches the whole desk, so a tab cannot be picked for an action: Rename
        # names the active profile, and Delete offers the others (the active one never goes).
        self.delete_profile_button = widgets._autolayout(ledger.quiet_button(tr("Delete…"),
            self._show_delete_profile_menu))
        for button in (self.new_profile_button, self.rename_profile_button, self.delete_profile_button):
            self.profile_actions.addArrangedSubview_(widgets.hug(button))
        module.add(self.profile_actions, full_width=False)
        self.ways_off_note = widgets.note(tr("Set every machine's ways from this screen. Turning it on changes nothing until you do; each machine's own ways are kept."))
        module.add(self.ways_off_note.view)
        self.older_ways_notice = widgets.note()
        if self.settings_store.take_profile_load_note("older_version"):
            self.older_ways_notice.set(tr("This machine kept the ways set while it ran an older Beamer."))
        self.older_ways_notice.view.setHidden_(not bool(self.older_ways_notice.text))
        self.atlas_canvas = diagram.AtlasCanvas.alloc().init().setup()
        self.arrangement_diagram = self.atlas_canvas
        self.atlas_canvas.on_layout_change = self._atlas_layout_changed
        self.atlas_canvas.on_select = self._atlas_selected
        self.atlas_canvas.on_third = self._atlas_third
        self.atlas_canvas.on_rename = self._begin_atlas_rename
        self.atlas_canvas.on_cycle_change = self._set_shortcut_cycle
        self.atlas_canvas.on_leave_out = lambda peer: self._set_atlas_left_out(peer, True)
        module.add(self.atlas_canvas)
        self.identify_screens_button = widgets.action_button(
            tr("Identify screens"), self.controller.identify_screens, scale="small")
        self.identify_screens_button.view.setToolTip_(
            tr("Show these numbers on every linked machine's screens for 3 seconds."))
        module.add(self.identify_screens_button.view, full_width=False)
        self.atlas_canvas_hint = widgets.note(tr("Option-click a display to split or group · drag to move · click a third of a lit edge to leave it out · drag a corner to resize"))
        module.add(self.atlas_canvas_hint.view)
        module.body.setCustomSpacing_afterView_(8, self.atlas_canvas)
        crosses = widgets.Label(tr("●  Crosses"), theme.TYPE["note"], 500, "signal").view
        offline = widgets.Label(tr("●  Offline, ways kept"), theme.TYPE["note"], 500, "ink_3").view
        self.atlas_legend_pair = widgets.SideBySide(crosses, offline, 80, 125, gap=18)
        for view, width in ((crosses, 80), (offline, 125)):
            preferred = view.widthAnchor().constraintEqualToConstant_(width)
            preferred.setPriority_(490)
            preferred.setActive_(True)
        self.side_by_side.append(self.atlas_legend_pair)
        self.atlas_legend = self.atlas_legend_pair.view
        module.add(self.atlas_legend)
        self.profile_switch_note = widgets.note(tr("Switching moves every machine to this profile's places and ways."))
        module.add(self.profile_switch_note.view)
        self.ways_ledger_module = widgets.Module()
        self.ledger_heading = widgets.Label(tr("Machines"), theme.TYPE["body"], 600)
        self.ways_ledger_module.add(self.ledger_heading.view)
        self.edit_header = widgets.stack(vertical=False, spacing=8)
        self.edit_notice = widgets.note(reading=False)
        widgets.hug(self.edit_notice.view, AppKit.NSLayoutPriorityDefaultLow)
        self.back_ways_button = widgets._autolayout(ledger.quiet_button(tr("Back to This Mac"), lambda: self._atlas_selected(self.own_id())))
        self.edit_header.addArrangedSubview_(self.edit_notice.view)
        self.edit_header.addArrangedSubview_(widgets.hug(self.back_ways_button))
        self.ways_ledger_module.add(self.edit_header, full_width=False)
        self.profile_status = widgets.note()
        self.ways_ledger_module.add(self.profile_status.view)
        self.remote_changes_note = widgets.note()
        self.ways_ledger_module.add(self.remote_changes_note.view)
        self.remote_permissions_note = widgets.note()
        self.ways_ledger_module.add(self.remote_permissions_note.view)
        self.own_ways_module = widgets.Module()
        self.own_ways_button = widgets.action_button(tr("Keep this machine's own ways"),
            lambda: self._profile_event(crossing_state.set_own_ways, True), scale="small")
        self.use_desk_button = widgets.action_button(tr("Use the desk's ways"),
            lambda: self._profile_event(crossing_state.set_own_ways, False), scale="small")
        self.share_ways_button = widgets.action_button(tr("Share mine with the desk"),
            lambda: self._profile_event(crossing_state.share_mine), scale="small")
        self.own_ways_title = widgets.stack(vertical=False, spacing=8)
        self.own_ways_dot = widgets.box("amber", radius=3)
        widgets.size(self.own_ways_dot, 6, 6)
        self.own_ways_note = widgets.Label(tr("This Mac keeps its own ways"), theme.TYPE["body"], 600, wrap=True)
        self.own_ways_title.addArrangedSubview_(self.own_ways_dot)
        self.own_ways_title.addArrangedSubview_(self.own_ways_note.view)
        self.own_ways_module.add(self.own_ways_title)
        self.own_actions_pair = widgets.SideBySide(self.use_desk_button.view, self.share_ways_button.view,
                                                 160, 190, gap=8)
        self.side_by_side.append(self.own_actions_pair)
        self.own_ways_module.add(self.own_actions_pair.view)
        self.saved_toggle = widgets._autolayout(ledger.quiet_button(tr("Earlier ways ▾"), self._toggle_earlier))
        self.own_toggle_row = widgets.SideBySide(self.own_ways_button.view, self.saved_toggle, 210, 100, gap=16)
        widgets.hug(self.saved_toggle)
        self.side_by_side.append(self.own_toggle_row)
        self.own_ways_module.add(self.own_toggle_row.view)
        self.earlier_host = widgets.stack(spacing=8)
        self.earlier_host.setHidden_(True)
        self.earlier_buttons = {}
        self._earlier_pairs = []
        self._earlier_key = None
        self.own_ways_module.add(self.earlier_host)
        self.own_ways_module.add(self.older_ways_notice.view)
        self.crossing_ledger = ledger.Ledger.alloc().init().setup(self)
        self.ways_ledger_module.add(self.crossing_ledger)
        self.ways_ledger_module.add(widgets.note(tr("Each chip is a side of this screen; click one to add or remove it. The side a machine touches crosses on its lit thirds; any other free side wraps round to it, arriving on its far side. A corner is a diagonal push, shown as a square on the map. The notch is this Mac only.")).view)
        return module

    @objc.python_method
    def _pointer_module(self):
        module = widgets.Module()
        module.add(widgets.Label(tr("This machine's pointer"), theme.TYPE["body"], 600).view)
        self.dragging_box = widgets.Switch(tr("Don't cross while dragging with this Mac's pointer"), on_change=self._changed)
        module.add(self.dragging_box.view)
        module.add(widgets.note(tr("Applies when this physical pointer pushes through this screen's ways; visiting pointers are not protected by this switch.")).view)
        return module

    @objc.python_method
    def _editing_other(self):
        return (profiles.same_ways_on(self.settings_store.current())
                and getattr(self, "viewed_machine", self.own_id()) != self.own_id())

    @objc.python_method
    def _view_settings(self):
        settings = self.settings_store.current()
        machine = getattr(self, "viewed_machine", settings["machine_id"])
        if not profiles.same_ways_on(settings) or machine not in profiles.machines_on_desk(settings):
            machine = settings["machine_id"]
            self.viewed_machine = machine
        viewed = profiles.view_settings(settings, machine)
        if machine != settings["machine_id"]:
            for peer in viewed["peers"]:
                if peer["id"] == settings["machine_id"]:
                    peer["name"] = tr("This Mac")
                    peer["platform"] = "macos"
                else:
                    original = next((p for p in settings["peers"] if p["id"] == peer["id"]), {})
                    peer["platform"] = original.get("platform", "")
        return viewed

    @objc.python_method
    def _profile_caps(self):
        known = getattr(self, "_profile_known_caps", {})
        for machine in self.settings_store.current().get("crossing_desk", {}).get("status", {}):
            known.setdefault(machine, frozenset(("layout", "profiles")))
        known.update(getattr(self.controller, "_profiles_known_caps", {}))
        known.update(self._design_peer_caps())
        self._profile_known_caps = known
        return known

    @objc.python_method
    def _profile_event(self, event, *args, **changes):
        self._flush()
        keywords = dict(platform="mac", now=time.time(), **changes)
        if event in (crossing_state.set_same_ways, crossing_state.set_own_ways,
                     crossing_state.switch_profile, crossing_state.restore_saved):
            keywords.update(capable=self.controller.crossing_capable(), live=self._live, gone=self._gone)
        try:
            outcome = self.controller.crossing_event(event, *args, **keywords)
        except (SettingsError, OSError, KeyError, TypeError, ValueError) as exc:
            self._say(str(exc), "fault")
            self._show_profiles()
            return None
        if outcome.get("reason") == "desk_full":
            self._say(tr("The desk is full. Remove an unused profile before adding more ways."), "amber")
        self._load_ways()
        self._show_arrangement()
        self._show_same()
        return outcome

    @objc.python_method
    def _shake_sensitivity_changed(self, value):
        self.shake_sensitivity_figure.set(text=str(value))
        self._profile_event(crossing_state.set_shake_sensitivity, value)

    @objc.python_method
    def _home_module(self):
        module = widgets.Module()
        module.add(widgets.Label(tr("Way home"), theme.TYPE["body"], 600).view)
        self.shake_switch = widgets.Switch(tr("Shake to come home"),
            on_change=lambda on: self._profile_event(crossing_state.set_shake, on))
        module.add(self.shake_switch.view)
        sensitivity_head = widgets.stack(vertical=False, spacing=6)
        sensitivity_head.addArrangedSubview_(
            widgets.Label(tr("Shake sensitivity"), theme.TYPE["body"], wrap=True).view)
        self.shake_sensitivity_figure = widgets.Figure("100", theme.TYPE["numeral"], "%")
        self.shake_sensitivity_figure.view.setContentCompressionResistancePriority_forOrientation_(
            AppKit.NSLayoutPriorityRequired, AppKit.NSLayoutConstraintOrientationHorizontal)
        widgets.add(sensitivity_head, self.shake_sensitivity_figure.view, full_width=False)
        module.add(sensitivity_head)
        self.shake_sensitivity = widgets.Ruler(
            50, 200, (50, 100, 150, 200), step=1, arrow_step=10, minor=15,
            on_change=self._shake_sensitivity_changed, title=tr("Shake sensitivity"),
        )
        module.add(self.shake_sensitivity.view)
        module.add(widgets.note(tr("Shake the mouse or trackpad left and right a few times while you drive another machine, and input comes straight back to the machine you are using.")).view)
        self.home_shortcut_row = widgets.stack(vertical=False, spacing=6)
        self.home_shortcut_note = widgets.note(reading=False)
        widgets.hug(self.home_shortcut_note.view, AppKit.NSLayoutPriorityDefaultLow)
        self.home_shortcut_button = widgets._autolayout(ledger.quiet_button(tr("Same → Shortcut"), self._home_shortcut))
        self.home_shortcut_row.addArrangedSubview_(self.home_shortcut_note.view)
        self.home_shortcut_row.addArrangedSubview_(widgets.hug(self.home_shortcut_button))
        module.add(self.home_shortcut_row, full_width=False)
        module.add(widgets.note(tr("Push back the way you came: where an edge leads back to This Mac")).view)
        self.home_warning = widgets.note(reading=False)
        self.home_warning_box = widgets.box(stroke="rule")
        self.home_warning_box.addSubview_(self.home_warning.view)
        widgets.pin(self.home_warning.view, self.home_warning_box, (8, 12, 8, 12))
        module.add(self.home_warning_box)
        return module

    @objc.python_method
    def _home_shortcut(self):
        self._select_page("overview")
        shortcut = self.same_checks["shortcut"]
        shortcut.scrollRectToVisible_(shortcut.bounds())
        self.window.makeFirstResponder_(shortcut)

    @objc.python_method
    def _toggle_earlier(self):
        self.earlier_host.setHidden_(not self.earlier_host.isHidden())
        self._show_profiles()

    @objc.python_method
    def _delete_profile_menu(self):
        """The profiles Delete… offers: every one but the active profile."""
        settings = self.settings_store.current()
        active = settings["crossing_desk"]["active"]["value"]
        menu = AppKit.NSMenu.alloc().initWithTitle_(tr("Delete…"))
        menu.setAutoenablesItems_(False)
        self._delete_menu_targets = []
        for choice in profiles.profile_list(settings):
            if choice["id"] == active:
                continue
            target = widgets.callback_target(
                lambda pid=choice["id"]: self._profile_event(crossing_state.remove_profile, pid))
            self._delete_menu_targets.append(target)
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(choice["label"], "fire:", "")
            item.setTarget_(target)
            menu.addItem_(item)
        return menu

    @objc.python_method
    def _show_delete_profile_menu(self):
        view = self.delete_profile_button
        self._delete_profile_menu().popUpMenuPositioningItem_atLocation_inView_(
            None, AppKit.NSMakePoint(0, view.bounds().size.height + 4), view)

    @objc.python_method
    def _profile_name(self, profile_id=None):
        settings = self.settings_store.current()
        held = next((p for p in profiles.profile_list(settings) if p["id"] == profile_id), None)
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(tr("Rename profile") if held else tr("New profile"))
        field = AppKit.NSTextField.alloc().initWithFrame_(((0, 0), (260, 24)))
        field.setStringValue_(held["name"] if held else "")
        field.setAccessibilityLabel_(tr("Profile name"))
        alert.setAccessoryView_(field)
        alert.addButtonWithTitle_(tr("Save") if held else tr("Add"))
        alert.addButtonWithTitle_(tr("Cancel"))
        def finished(response):
            if response == AppKit.NSAlertFirstButtonReturn:
                name = field.stringValue().strip()
                if name:
                    if held:
                        self._profile_event(crossing_state.rename_profile, profile_id, name)
                    else:
                        self._profile_event(crossing_state.new_profile, name)
        alert.beginSheetModalForWindow_completionHandler_(self.window, finished)

    @objc.python_method
    def _show_profiles(self):
        if not hasattr(self, "ways_switch"):
            return
        settings = self.settings_store.current()
        on = profiles.same_ways_on(settings)
        remote = self._editing_other()
        machine = self.viewed_machine if remote else self.own_id()
        name = tr("This Mac") if machine == self.own_id() else profiles.view_settings(settings, machine)["name"]
        self.ways_switch.value = on
        self.crossing_intro.set(
            tr("Click a machine on the map to set its ways. The ledger says how it reaches each other machine.") if on else
            tr("Drag a machine on the map to where it sits. The ledger says how each one is reached; click a row to pick it on the map."))
        self.ways_off_note.view.setHidden_(on)
        self.profile_tabs.setHidden_(not on)
        self.profile_actions.setHidden_(not on)
        self.atlas_canvas_hint.set(
            tr("Click a machine to set its ways · Tab and Return pick one from the keyboard") if on else
            tr("Option-click a display to split or group · drag to move · click a third of a lit edge to leave it out · drag a corner to resize"))
        self.atlas_canvas_hint.view.setHidden_(False)
        self.ledger_heading.set(tr("{name}'s ways").format(name=name) if on else tr("Machines"))
        self.edit_notice.set(tr("Editing {name}'s ways ·").format(name=name) if remote else "")
        self.edit_header.setHidden_(not remote)
        self.edit_notice.view.setHidden_(not remote)
        self.back_ways_button.setHidden_(not remote)
        self.remote_permissions_note.set(tr("Jump keys and permissions are set on {name}.").format(name=name) if remote else "")
        self.remote_permissions_note.view.setHidden_(not remote)
        caps = self._profile_caps().get(machine, ()) if remote else ("profiles",)
        linked = self._live(machine) if remote else True
        link_times = getattr(self.controller, "desk_link_times", {})
        last_heard = None if getattr(self.controller, "desk_last_heard", {}).get(machine) else link_times.get(machine)
        status = profiles.machine_line(settings, machine, linked=linked, caps=caps, last_heard=last_heard)
        self.profile_status.set(status)
        self.profile_status.view.setHidden_(not on)
        self.remote_changes_note.set(tr("Changes go to {name} at once.").format(name=name) if remote else "")
        self.remote_changes_note.view.setHidden_(not remote or not linked or "profiles" not in caps)
        own = on and not remote and not profiles.in_force(settings)[1]
        self.own_ways_title.setHidden_(not own)
        self.own_ways_button.view.setHidden_(not on or remote or own)
        self.use_desk_button.view.setHidden_(not own)
        desk = settings["crossing_desk"]
        desk_row = desk["profiles"].get(desk["active"]["value"], {}).get("rows", {}).get(self.own_id(), {}).get("value", {})
        self.share_ways_button.view.setHidden_(not own or settings["crossing_own"]["row"] == desk_row)
        self.own_actions_pair.view.setHidden_(not own)
        self.own_ways_module.view.setHidden_(remote or not on and not bool(self.older_ways_notice.text))
        saved = profiles.saved_list(settings)
        self.saved_toggle.setHidden_(not on or remote or not saved)
        self.own_toggle_row.view.setHidden_(not on or remote or own and not saved)
        if not on or remote or not saved:
            self.earlier_host.setHidden_(True)
        earlier_title = tr("Earlier ways ▾") if self.earlier_host.isHidden() else tr("Earlier ways ▴")
        self.saved_toggle.setTitle_(earlier_title)
        self.saved_toggle.setAttributedTitle_(widgets.attributed(earlier_title, 12.5, 500, "ink_2"))
        self.saved_toggle.setAccessibilityLabel_(earlier_title)
        self.shake_switch.value = profiles.shake_on(settings)
        self.shake_sensitivity.value = profiles.shake_sensitivity(settings)
        self.shake_sensitivity_figure.set(text=str(self.shake_sensitivity.value))
        warnings = profiles.no_way_back(settings, machine)
        self.home_warning.set("\n".join(tr("{name} has no way back to {machine} except the shortcut and a shake.").format(
            name=target, machine=name) for target in warnings))
        self.home_warning.view.setHidden_(not warnings)
        self.home_warning_box.setHidden_(not warnings)
        style = tr("hold") if self.controller.cfg.trigger_style == "hold" else tr("double-tap")
        key_title = widgets.key_title(self.controller.cfg.trigger_key)
        shortcut_template = tr("Shortcut: {style} {key} ·")
        shortcut_text = shortcut_template.format(style=style, key=key_title)
        self.home_shortcut_note.set(shortcut_text)
        before_key, _marker, after_key = shortcut_template.partition("{key}")
        shortcut_ink = AppKit.NSMutableAttributedString.alloc().initWithAttributedString_(
            widgets.attributed(before_key.format(style=style), theme.TYPE["note"], ink="ink_2", wrap=True))
        shortcut_ink.appendAttributedString_(widgets.attributed(key_title, theme.TYPE["note"], ink="ink_2", mono=True))
        shortcut_ink.appendAttributedString_(widgets.attributed(after_key.format(style=style), theme.TYPE["note"], ink="ink_2", wrap=True))
        self.home_shortcut_note.view.setAttributedStringValue_(shortcut_ink)
        rows = pages.crossing_rows(self._methods())
        self.pointer_module.view.setHidden_(remote)
        self.resistance_module.view.setHidden_(remote or not rows["resistance"])
        self.shortcut_module.view.setHidden_(remote or not rows["shortcut"] and not bool(getattr(self, "_trigger_conflict", "")))
        choices = profiles.profile_list(settings)
        self.profile_switch_note.view.setHidden_(not on or len(choices) < 2)
        self.delete_profile_button.setEnabled_(len(choices) > 1)
        self.delete_profile_button.setAttributedTitle_(widgets.attributed(
            tr("Delete…"), 12.5, 500, "ink_2" if len(choices) > 1 else "ink_3"))
        wrap_tabs = len(choices) > 2
        key = (tuple((p["id"], p["label"]) for p in choices), desk["active"]["value"], wrap_tabs)
        if key != self._profile_tabs_key:
            self._profile_tabs_key = key
            for view in list(self.profile_tabs.arrangedSubviews()):
                self.profile_tabs.removeArrangedSubview_(view)
                view.removeFromSuperview()
            self.profile_buttons = {}
            self.profile_tabs.setOrientation_(AppKit.NSUserInterfaceLayoutOrientationVertical if wrap_tabs
                                              else AppKit.NSUserInterfaceLayoutOrientationHorizontal)
            tab_row = self.profile_tabs
            for choice in choices:
                pid = choice["id"]
                button = widgets.action_button(choice["label"],
                    lambda pid=pid: self._profile_event(crossing_state.switch_profile, pid),
                    style="plain", scale="small")
                if pid == desk["active"]["value"]:
                    widgets.paint(button.view, "panel", "ink_2")
                menu = AppKit.NSMenu.alloc().initWithTitle_(tr("Profile"))
                targets = []
                for title, callback, enabled in (
                    (tr("Rename"), lambda pid=pid: self._profile_name(pid), True),
                    (tr("Remove"), lambda pid=pid: self._profile_event(crossing_state.remove_profile, pid),
                     len(choices) > 1 and pid != desk["active"]["value"])):
                    target = widgets.callback_target(callback)
                    targets.append(target)
                    item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, "fire:", "")
                    item.setTarget_(target)
                    item.setEnabled_(enabled)
                    menu.addItem_(item)
                menu.setAutoenablesItems_(False)
                button.menu_targets = targets
                button.view.setMenu_(menu)
                if wrap_tabs:
                    tab_row = widgets.stack(vertical=False, spacing=6)
                    self.profile_tabs.addArrangedSubview_(tab_row)
                tab_row.addArrangedSubview_(button.view)
                self.profile_buttons[pid] = button
            self.add_profile_button = widgets._autolayout(ledger.quiet_button(tr("+"), self._profile_name))
            widgets.size(self.add_profile_button, 24, 24)
            self.add_profile_button.setAccessibilityLabel_(tr("New profile"))
            self.profile_tabs.addArrangedSubview_(self.add_profile_button)
        if repr(saved) != self._earlier_key:
            self._earlier_key = repr(saved)
            self.side_by_side[:] = [pair for pair in self.side_by_side if pair not in self._earlier_pairs]
            self._earlier_pairs = []
            for view in list(self.earlier_host.arrangedSubviews()):
                self.earlier_host.removeArrangedSubview_(view)
                view.removeFromSuperview()
            self.earlier_buttons = {}
            reasons = {"upgrade": tr("Before Beamer 1.5.5"), "older_version": tr("From an older Beamer"),
                "sync_off": tr("When Same ways went off"), "seeded": tr("When Same ways went on"),
                "edited": tr("Before you changed it"), "restored": tr("Before a restore")}
            for entry in saved:
                sid = entry["id"]
                why = reasons.get(entry["why"], tr("Before you changed it"))
                if entry["why"] == "replaced_by":
                    changed_by = entry.get("by", "")
                    who = tr("This Mac") if changed_by == self.own_id() else peerlist.label_for(settings["peers"], peer_id=changed_by) or tr("another machine")
                    why = tr("Before {name} changed it").format(name=who)
                caption = widgets.note(tr("{date} · {reason}").format(
                    date=time.strftime("%d-%m-%Y", time.localtime(entry["at"])), reason=why), reading=False)
                widgets.hug(caption.view, AppKit.NSLayoutPriorityDefaultLow)
                restore = widgets._autolayout(ledger.quiet_button(tr("Restore"), lambda sid=sid: self._profile_event(crossing_state.restore_saved, sid)))
                delete = widgets._autolayout(ledger.quiet_button(tr("Delete"), lambda sid=sid: self._profile_event(crossing_state.delete_saved, sid)))
                actions = widgets.stack(vertical=False, spacing=8)
                widgets.hug(actions, AppKit.NSLayoutPriorityDefaultHigh)
                for button in (restore, delete):
                    actions.addArrangedSubview_(button)
                pair = widgets.SideBySide(caption.view, actions, 190, 150, gap=16)
                # A stack has no intrinsic width to hug: give spare row width to the caption.
                preferred = actions.widthAnchor().constraintEqualToConstant_(150)
                preferred.setPriority_(489)
                preferred.setActive_(True)
                self.side_by_side.append(pair)
                self._earlier_pairs.append(pair)
                widgets.add(self.earlier_host, pair.view)
                self.earlier_buttons[sid] = (restore, delete)
    @objc.python_method
    def _show_jump_key(self):
        self._show_machines()
    @objc.python_method
    def _jump_arming(self, armed):
        self.controller.jump_recording = armed

    @objc.python_method
    def _record_jump_key(self, value):
        self._save_jump_key(value)

    @objc.python_method
    def _save_jump_key(self, value, peer=None):
        peer = peer or self.chosen_peer
        try:
            self.settings_store.set_jump_key(peer, value, self.controller.cfg.trigger_key)
            self.controller.cfg = self.settings_store.load()
        except SettingsError as exc:
            self._say(str(exc), "amber")
        self._show_machines()
    def clearJumpKey_(self, _sender):
        self._save_jump_key("")

    @objc.python_method
    def _way_toggled(self, way, on):
        if way == "shortcut":
            self.method_boxes["shortcut"].value = on
            self._changed()

    @objc.python_method
    def _ledger_edge(self, peer, part=None):
        settings = self._view_settings() if hasattr(self, "viewed_machine") else self.settings_store.current()
        entry = next((item for item in settings["peers"] if item["id"] == peer), None)
        held = core_ways.ways(settings, peer)
        own = settings.get("machine_id")
        tiles = core_ways.derive_layout(settings, gap=0)
        contact = core_ways._rect_contact(tiles[own], tiles[peer]) if own in tiles and peer in tiles else None
        if (entry is None or entry.get("left_out") or entry.get("in_use", True) is False
                or not contact or contact[0] == "corner"):
            return
        methods, parts = (core_ways.toggle_third(held["methods"], held["parts"], part) if part
                          else core_ways.toggle_edge(held["methods"], held["parts"]))
        self._write_ways(peer, edge=bool(set(methods) & {"edge", "part"}), thirds=parts)
        self._load_ways()
        self._reflect()

    @objc.python_method
    def _reach(self, peer, settings=None):
        """`core.ways.reach` for one machine, kept until the settings or who is live change: the status
        tick refreshes the ledger and the map every 0.4 s, and each answer is several compiles."""
        settings = settings or self._view_settings()
        args = self._crossing_args()
        if self._editing_other():
            args["platform"] = profiles.view_platform(self.settings_store.current(), self.viewed_machine)
            args["capable"] = set(settings.get("layout", {}))
        ids = [entry.get("id") for entry in settings.get("peers", [])]
        stamp = (repr(settings), args["platform"], frozenset(args["capable"] or ()),
                 tuple(bool(args["live"](ident)) for ident in ids), tuple(bool(args["gone"](ident)) for ident in ids))
        if getattr(self, "_reach_stamp", None) != stamp:
            self._reach_stamp, self._reach_held = stamp, {}
        if peer not in self._reach_held:
            self._reach_held[peer] = core_ways.reach(settings, peer, platform=args["platform"],
                                                     capable=args["capable"], live=args["live"], gone=args["gone"])
        return copy.deepcopy(self._reach_held[peer])

    @objc.python_method
    def _ledger_side(self, peer, side):
        """A side chip: the contact side toggles Edge as a whole; a free side is added (turning Edge on
        if it was off), one that wraps is removed, and a kept one turns Edge back on or is removed."""
        settings = self._view_settings()
        entry = next((item for item in settings["peers"] if item["id"] == peer), None)
        if entry is None or entry.get("left_out") or entry.get("in_use", True) is False:
            return
        state = self._reach(peer, settings)["sides"][side]["state"]
        if state == "contact":
            return self._ledger_edge(peer)
        held = core_ways.ways(settings, peer)
        edge_on = bool(set(held["methods"]) & {"edge", "part"})
        edges = [item for item in core_ways.EDGES if item in held["edges"] and item != side]
        if state == "kept" and not edge_on:
            self._write_ways(peer, edge=True)
        elif state == "free":
            self._write_ways(peer, edges=[item for item in core_ways.EDGES if item in edges + [side]],
                             **({} if edge_on else {"edge": True}))
        elif state in ("on", "kept"):
            self._write_ways(peer, edges=edges)

    @objc.python_method
    def _ledger_corner(self, peer, corner):
        """A corner button: several may be on; another machine's, or one with no side contact to leave
        from, is refused without a write (a corner already on can always be taken off)."""
        settings = self._view_settings() if hasattr(self, "viewed_machine") else self.settings_store.current()
        entry = next((item for item in settings["peers"] if item["id"] == peer), None)
        if entry is None or entry.get("left_out") or entry.get("in_use", True) is False:
            return
        held = core_ways.ways(settings, peer)
        if corner not in held["corners"]:
            if self._reach(peer, settings)["corners"][corner]["state"] != "free":
                return
        chosen = [item for item in core_ways.CORNERS if (item in held["corners"]) != (item == corner)]
        self._write_ways(peer, corners=chosen)

    @objc.python_method
    def _ledger_notch(self, peer, enabled):
        settings = self._view_settings() if hasattr(self, "viewed_machine") else self.settings_store.current()
        entry = next((item for item in settings["peers"] if item["id"] == peer), None)
        has_notch = (self.settings_store.current()["crossing_desk"].get("status", {}).get(
                     self.viewed_machine, {}).get("value", {}).get("platform") == "mac"
                     if hasattr(self, "viewed_machine") and self._editing_other() else self.has_notch)
        if (has_notch and entry and not entry.get("left_out")
                and entry.get("in_use", True) is not False):
            self._write_ways(peer, notch=enabled)
            self._reflect()
    @objc.python_method
    def _ledger_notch_return(self, peer, enabled):
        if self.has_notch or self._editing_other():
            self._write_ways(peer, notch_return=enabled)

    @objc.python_method
    def _atlas_third(self, peer, part):
        self._ledger_edge(peer, part)
    @objc.python_method
    def _third_toggled(self, part):
        if self.chosen_peer:
            self._ledger_edge(self.chosen_peer, part)
    @objc.python_method
    def _shortcut_module(self):
        module = widgets.Module()
        module.add(widgets.eyebrow(tr("Shortcut")))
        self.method_boxes = {"shortcut": widgets.WayTile(tr("Shortcut"), on_change=lambda on: self._way_toggled("shortcut", on))}
        module.add(self.method_boxes["shortcut"].view)
        self.key_recorder = widgets.KeyRecorder(
            "alt_r", on_change=self._trigger_chosen, hint=tr(widgets.KeyRecorder.HINT),
            recording_title=tr(widgets.KeyRecorder.RECORDING_TITLE),
            recording_hint=tr(widgets.KeyRecorder.RECORDING_HINT),
        )
        module.add(self.key_recorder.view)
        self.style_select = widgets.Segmented([("double_tap", tr("Double-tap")), ("hold", tr("Hold"))], on_change=self._changed)
        module.add(widgets.field_row(tr("How you press it"), self.style_select.view)[0])
        self.shortcut_goes_to = widgets.Segmented(
            [("last", tr("Last machine")), ("next", tr("Next in order")), ("ticked", tr("Next among ticked"))],
            columns=3, on_change=self._shortcut_destination_changed,
        )
        self.shortcut_goes_to.cap = max(widgets.attributed(title, theme.TYPE["small"]).size().width + 24
                                       for _value, title in self.shortcut_goes_to.choices) * 3
        self.shortcut_goes_to.cap_constraint.setConstant_(self.shortcut_goes_to.cap)
        self.shortcut_goes_to.preference.setConstant_(self.shortcut_goes_to.cap)
        for _value, _cell, words in self.shortcut_goes_to.cells:
            words.view.setUsesSingleLineMode_(True)
        module.add(widgets.field_row(tr("Shortcut goes to"), self.shortcut_goes_to.view)[0])
        self.shortcut_cycle_note = widgets.note()
        module.add(self.shortcut_cycle_note.view)
        # The figure sits on the ruler's own line, so it reads as the ruler's value rather than as
        # a heading for the whole module, and the two hide together under Hold.
        self.double_tap_head = widgets.stack(spacing=6)
        figure, self.double_tap_numeral = self._numeral("ms")
        caption, _ = widgets.field_row(tr("Time between taps"), figure)
        widgets.add(self.double_tap_head, caption)
        # The stored window runs 50 to 2000 ms, but only about 150 to 600 is useful, so the ruler
        # shows 50 to 1000 on a square-root scale; a stored value past 1000 pins the thumb.
        self.double_tap_ruler = widgets.Ruler(
            50, 1000, (50, 150, 300, 600, 1000), step=10, scale="sqrt", minor=20, on_change=self._double_tap_moved,
            title=tr("Time between taps"),
        )
        widgets.add(self.double_tap_head, self.double_tap_ruler.view)
        module.add(self.double_tap_head)
        self.style_hint = widgets.note()
        module.add(self.style_hint.view)
        return module

    @objc.python_method
    def _shortcut_destination_changed(self, *_ignored):
        self._show_shortcut_destination_note()
        self._changed()

    @objc.python_method
    def _show_shortcut_destination_note(self):
        if not hasattr(self, "shortcut_cycle_note"):
            return
        mode = self.shortcut_goes_to.value or "last"
        hold = self.style_select.value == "hold"
        if mode == "last":
            note = tr("With Hold, input goes to the last machine.")
        elif hold and mode == "ticked":
            note = tr("With Hold, input goes to the first ready machine in order. Choose which machines join the cycle with the tick boxes on their tiles.")
        elif hold:
            note = tr("With Hold, input goes to the first ready machine in order.")
        elif mode == "ticked":
            note = tr("The order follows the tiles from top to bottom, then left to right. This Mac is always in the cycle. Choose which machines join the cycle with the tick boxes on their tiles.")
        else:
            note = tr("The order follows the tiles from top to bottom, then left to right. This Mac is always in the cycle.")
        self.shortcut_cycle_note.set(note)

    @objc.python_method
    def _show_trigger_conflict(self, note):
        self._trigger_conflict = note
        if note:
            self.style_hint.set(note, ink="amber")

    @objc.python_method
    def _trigger_chosen(self, key):
        try:
            core_ways.check_trigger_key(self.settings_store.current(), key)
        except ValueError as exc:
            self.key_recorder.set_value(self.controller.cfg.trigger_key)
            self._show_trigger_conflict(str(exc))
            return
        self._show_trigger_conflict("")
        self._changed()

    @objc.python_method
    def _ignored_module(self):
        module = widgets.Module()
        module.add(widgets.eyebrow(tr("Stays on this Mac")))
        module.add(widgets.note(tr(
            "These keys and buttons keep working on this Mac while its input is on another machine: a "
            "mouse's back button for this Mac's browser, say, or a volume key for its speakers."
        )).view)
        self.ignored_entries = []
        self.ignored_list = widgets.stack(spacing=6)
        module.add(self.ignored_list)
        self.ignored_empty = widgets.note(tr("Nothing yet. Every key and button goes to the machine that has input."))
        module.add(self.ignored_empty.view)
        self.ignored_recorder = widgets.IgnoredRecorder(
            KEY_NAME_TO_CODE.get(self.key_recorder.value), on_recorded=self._add_ignored,
            title=tr(widgets.IgnoredRecorder.TITLE),
            recording_title=tr(widgets.IgnoredRecorder.RECORDING_TITLE),
            hint=tr(widgets.IgnoredRecorder.HINT),
            recording_hint=tr(widgets.IgnoredRecorder.RECORDING_HINT),
        )
        module.add(self.ignored_recorder.view)
        self.ignored_status = widgets.note()
        module.add(self.ignored_status.view)
        self.ignored_status.view.setHidden_(True)
        self._render_ignored()
        return module

    @objc.python_method
    def _render_ignored(self):
        for view in list(self.ignored_list.arrangedSubviews()):
            self.ignored_list.removeArrangedSubview_(view)
            view.removeFromSuperview()
        for entry in self.ignored_entries:
            # One quiet row per entry, as on Windows: the name, and a small Remove that does not
            # outweigh it.
            title = ignored_titles.entry_title(entry)
            chip = widgets.box("well", "rule", theme.RADIUS["field"])
            name = widgets.Label(title, theme.TYPE["small"], 600, mono=True)
            remove = widgets.pressable(lambda entry=entry: self._remove_ignored(entry), radius=theme.RADIUS["field"])
            # Translators: {name} is the machine name.
            remove.setAccessibilityLabel_(tr("Remove {name}").format(name=title))
            remove_word = widgets.Label(tr("Remove"), theme.TYPE["small"], ink="ink_2")
            remove.addSubview_(remove_word.view)
            widgets.pin(remove_word.view, remove, (7, 10, 7, 10))
            line = widgets.stack(vertical=False, spacing=10)
            line.addArrangedSubview_(name.view)
            line.addArrangedSubview_(remove)
            widgets.hug(name.view, AppKit.NSLayoutPriorityDefaultLow)
            chip.addSubview_(line)
            widgets.pin(line, chip, (2, 12, 2, 2))
            widgets.add(self.ignored_list, chip)
        self.ignored_empty.view.setHidden_(bool(self.ignored_entries))

    @objc.python_method
    def _ignored_said(self, message):
        self.ignored_status.set(message)
        motion.set_hidden(self.ignored_status.view, not message)

    @objc.python_method
    def _add_ignored(self, entry):
        if entry is None:
            self._ignored_said(tr("That key is the shortcut; it always stays with Beamer."))
            return
        if entry in self.ignored_entries:
            return
        self.ignored_entries.append(entry)
        self._ignored_said("")
        self._render_ignored()
        self._changed()

    @objc.python_method
    def _remove_ignored(self, entry):
        self.ignored_entries.remove(entry)
        self._ignored_said("")
        self._render_ignored()
        self._changed()

    @objc.python_method
    def _on_screen_module(self):
        module = widgets.Module()
        module.add(widgets.eyebrow(tr("On screen")))
        self.glow_box = widgets.Switch(tr("Animate crossings on this Mac"), on_change=self._changed)
        module.add(self.glow_box.view)
        module.add(self._own_note())
        module.add(widgets.note(tr(
            "Turns crossing and landing animations on or off on this screen. Saved style choices stay in place."
        )).view)
        self.landing_box = widgets.Switch(tr("Show where the pointer lands"), on_change=self._changed)
        self.landing_row = widgets.stack(spacing=8)
        widgets.add(self.landing_row, self.landing_box.view)
        widgets.add(self.landing_row, widgets.note(tr(
            "For shortcut, menu and jump arrivals; requires Animate crossings on this screen."
        )).view)
        module.add(self.landing_row)
        return module

    @objc.python_method
    def _preview_tile(self, base, value, title, detail, notch, flexible=False, **overrides):
        """One tile's preview: its own screen, a feed pinned to the style the tile shows, and the
        real renderer hosted on it, registered with the page's loop."""
        if notch is not None:
            colour = lambda: (self.glow_colour_select.value if hasattr(self, "glow_colour_select") else None) or "signal"
            view = previews.notch_crossing_still(base, colour, self.logger, self.controller, notch)
            self.effect_stills.append(view)
            return value, title, detail, view
        screen = previews.PreviewScreen.alloc().init().setup(notch)
        if flexible:
            screen.widthAnchor().constraintEqualToAnchor_multiplier_(screen.heightAnchor(), 2.0).setActive_(True)
        else:
            screen = widgets.size(screen, height=84)
        feed = previews.PreviewFeed(self.controller, **overrides)
        if notch is None:
            renderer = previews.hosted_edge(base)(feed, self.logger, screen)
            update = previews.edge_update(renderer)
        else:
            renderer = previews.hosted_notch(base)(feed, self.logger, screen)
            update = previews.notch_update(renderer)
        self.previews.add(feed, renderer, update, screen)
        return value, title, detail, screen

    @objc.python_method
    def _notch_row(self):
        """The notch's own styles, which play at the notch in place of Glow and Beam: under Show at,
        shown while it is at the notch. Named apart from Classic Beam, which is the edge's line."""
        row = widgets.stack(spacing=12)
        widgets.add(row, widgets.label(tr("Notch style"), theme.TYPE["note"], 600, ink="ink_2"))
        notch = previews.notch_size()
        self.notch_style_select = widgets.ChoiceTiles(
            [
                self._preview_tile(NotchBeam, "beam", tr("Outline"), tr("A line of colour runs round the notch, faster the harder you push."), notch),
                self._preview_tile(NotchIsland, "island", tr("Island"), tr("The notch grows as you push, with a meter inside, and flashes as you go through."), notch),
            ],
            on_change=self._changed,
            vertical=True,
            responsive=True,
            aspect=False,
        )
        self._hover(self.notch_style_select)
        widgets.add(row, self.notch_style_select.view)
        self.notch_after_select = widgets.Segmented(
            [(600, "0.6 s"), (1200, "1.2 s"), (2000, "2 s"), (3000, "3 s")], on_change=self._changed
        )
        widgets.add(row, widgets.field_row(tr("Keep animating"), self.notch_after_select.view)[0])
        widgets.add(row, self._own_note())
        self.notch_note = widgets.note()
        widgets.add(row, self.notch_note.view)
        return row

    @objc.python_method
    def _follow_banner(self):
        """Shown at the top of Style while this Mac follows another machine's design, whose
        followed controls are locked until Stop following (settings_sync.follow_lock)."""
        self.follow_banner = widgets.stack(spacing=8)
        self.follow_banner.setWantsLayer_(True)
        widgets.paint(self.follow_banner, "well", "rule", theme.RADIUS["button"])
        self.follow_banner.setEdgeInsets_(AppKit.NSEdgeInsetsMake(10, 12, 10, 12))
        self.follow_banner_label = widgets.Label("", theme.TYPE["body"], 600, wrap=True)
        widgets.add(self.follow_banner, self.follow_banner_label.view)
        self.stop_follow_button = widgets.Button(settings_sync.STOP_FOLLOWING, self, "stopFollowing:", scale="small")
        widgets.add(self.follow_banner, self.stop_follow_button.view, full_width=False)
        self.follow_banner.setHidden_(True)
        return self.follow_banner

    def stopFollowing_(self, _sender):
        self._set_design_follow("")

    @objc.python_method
    def _lock_followed_design(self, entry, name):
        """Followed controls dimmed and deaf while `entry` is followed; the local ones stay live."""
        if not hasattr(self, "follow_banner"):
            return
        locked = entry is not None
        for control in (self.glow_style_select, self.switch_style_select, self.glow_colour_select,
                        self.length_select, self.size_select, self.frame_rate_select, self.landing_box):
            control.set_enabled(not locked)
        self.follow_banner_label.set(settings_sync.following_banner(name) if locked else "")
        self.follow_banner.setHidden_(not locked)

    @objc.python_method
    def _edge_module(self):
        """Every style, grouped as effects.DIRECTIONS groups them: today's Glow and Beam first with
        stills like the rest, then each direction's quiet, medium and showpiece effect; every tile shows
        its style at the place chosen above them, and plays while the pointer is over it."""
        module = widgets.Module()
        module.add(widgets.eyebrow(tr("Style")))
        module.add(self._follow_banner())
        self.style_preview_hint = widgets.note(tr("Hover to preview"), ink="ink_2")
        module.add(self.style_preview_hint.view)
        self.style_for_select = widgets.Segmented(
            [("crossing", tr("Crossing")), ("switch", tr("Shortcut and menu"))], on_change=self._changed
        )
        self.style_for_select.value = "crossing"
        self.style_for_row = widgets.field_row(tr("Style for"), self.style_for_select.view)[0]
        module.add(self.style_for_row)
        # Where every tile below plays its style: never switched off by the style, and what differs
        # there is said beneath it.
        self.effect_method_select = widgets.Segmented([(key, tr(name)) for key, name in previews.STAGE_METHODS], on_change=self._place_picked)
        self.effect_method_select.value = self.place = "edge"
        self.effect_method_view = widgets.field_row(tr("Show at"), self.effect_method_select.view)[0]
        module.add(self.effect_method_view)
        self.place_note = widgets.note()
        module.body.setCustomSpacing_afterView_(6, self.effect_method_view)
        self.notch_row = self._notch_row()
        colour = lambda: self.glow_colour_select.value or "signal"
        place = lambda: self.place
        pace = lambda: self.length_select.numeric_value()
        effect_size = lambda: self.size_select.value or "medium"
        size_fraction = lambda: self.size_select.numeric_value()
        # The two sets of tiles, one per mode; only the chosen mode's shows.
        self.crossing_styles_box = widgets.stack(spacing=0)
        tile_sets = widgets.stack(spacing=6)
        widgets.add(tile_sets, self.crossing_styles_box)
        rows = []
        for group, choices in pages.style_groups(self.effects_ready):
            # Glow and Beam as stills of the same scene as the effects, so every tile reads alike.
            tiles = [(value, title, detail, previews.effect_still(value, colour, self.logger, place=place,
                                                                    pace=pace, effect_size=effect_size,
                                                                    effect_size_fraction=size_fraction))
                     for value, title, detail in choices]
            self.effect_stills.extend(tile[3] for tile in tiles)
            if group == pages.TODAY:
                # At the notch Glow and Beam do not draw: the notch style plays instead, so there
                # their tiles show it, played by its own renderer as its tiles are.
                notch = previews.notch_size()
                stacked = []
                for value, title, detail, still in tiles:
                    screens = {style: self._preview_tile(base, style, "", "", notch, flexible=True)[3]
                               for style, base in (("beam", NotchBeam), ("island", NotchIsland))}
                    stack = previews.TileStack.alloc().init().setup([still, *screens.values()])
                    self.classic_pictures.append((stack, still, screens))
                    stacked.append((value, title, detail, stack))
                tiles = stacked
            rows.append(self._style_group(self.crossing_styles_box, group, tiles))
        self.glow_style_select = widgets.Linked(rows, on_change=self._changed, host=self.crossing_styles_box)
        self._style_grid(self.crossing_styles_box)
        self.switch_styles_box = widgets.stack(spacing=0)
        widgets.add(tile_sets, self.switch_styles_box)
        rows = []
        for group, choices in pages.switch_style_groups(self.effects_ready):
            tiles = []
            for value, title, detail in choices:
                fx = lambda value=value: effects.switch_effect(value, self.glow_style_select.value or "glow")
                tiles.append((value, title, detail, previews.switch_still(
                    fx, colour, self.logger, pace=pace, effect_size=effect_size,
                    effect_size_fraction=size_fraction)))
            self.effect_stills.extend(tile[3] for tile in tiles)
            rows.append(self._style_group(self.switch_styles_box, group, tiles))
        self._style_grid(self.switch_styles_box)
        widgets.add(self.switch_styles_box, widgets.note(tr(
            "What plays around the pointer for shortcut, menu and jump arrivals. Same as crossing uses this screen's crossing style."
        )).view)
        self.switch_style_select = widgets.Linked(rows, on_change=self._changed, host=self.switch_styles_box)
        module.add(tile_sets)
        # The chosen style's name and what it does, under the tiles that chose it.
        self.chosen_box = widgets.stack(spacing=6)
        self.effect_name = widgets.Label("", theme.TYPE["body"], 600)
        widgets.add(self.chosen_box, self.effect_name.view)
        self.effect_blurb = widgets.note()
        widgets.add(self.chosen_box, self.effect_blurb.view)
        module.add(self.chosen_box)
        module.body.setCustomSpacing_afterView_(12, self.chosen_box)
        # Every style and every switch plays at this length, so it follows the tiles either way.
        self.length_select = _DesignSliderChoice(tr("Length"), 25, 250, 5, 100, (0.5, 1.0, 2.0),
                                                 effects.LENGTHS, self._design_slider_changed,
                                                 tr("Sets the effect's playback time."))
        self.size_select = _DesignSliderChoice(tr("Size"), 20, 250, 1, 1000, (0.03, 0.045, 0.06),
                                               effects.SIZES, self._design_slider_changed,
                                               tr("Sets effect depth relative to the shorter screen edge."))
        self.length_readout = design_readout()
        self.size_readout = design_readout()
        length_control = widgets.stack(False, spacing=10)
        widgets.add(length_control, self.length_select.view)
        widgets.add(length_control, self.length_readout.view, full_width=False)
        size_control = widgets.stack(False, spacing=10)
        widgets.add(size_control, self.size_select.view)
        widgets.add(size_control, self.size_readout.view, full_width=False)
        self.length_row = widgets.field_row(tr("Length"), length_control)[0]
        self.size_row = widgets.field_row(tr("Size"), size_control)[0]
        self.frame_rate_select = widgets.Segmented(
            tuple((value, tr(name)) for value, name, _divisor in effects.FRAME_RATES),
            on_change=self._changed,
        )
        self.frame_rate_row = widgets.field_row(tr("Frame rate"), self.frame_rate_select.view)[0]
        module.add(self.length_row)
        module.add(widgets.note(tr("Sets the effect's playback time."), ink="ink_2").view)
        module.add(self.size_row)
        module.add(widgets.note(tr("Sets effect depth relative to the shorter screen edge."), ink="ink_2").view)
        module.add(self.frame_rate_row)
        module.add(self.place_note.view)
        module.add(self.notch_row)
        return module

    @objc.python_method
    def place_note_text(self, style, place, methods):
        return pages.place_note(style if self.effects_ready else "glow", place, methods, self.has_notch)

    @objc.python_method
    def _place_picked(self, place):
        """Every tile shows its style at `place`."""
        self._place_chosen = True
        self.place = place
        self._reflect()

    @objc.python_method
    def _hover(self, choices):
        """Each tile of `choices` plays its preview while the pointer is over it."""
        for (_value, tile, _name), preview in zip(choices.tiles, choices.previews):
            self.tile_hover.track(tile, preview)

    @objc.python_method
    def _style_group(self, box, group, tiles):
        group_box = widgets.stack(spacing=8)
        group_box.setAlignment_(AppKit.NSLayoutAttributeWidth)
        heading = widgets.label(group, theme.TYPE["note"], 600, ink="ink_2")
        widgets.add(group_box, heading)
        row = widgets.ChoiceTiles(tiles, vertical=True, responsive=True, keep_columns=True)
        widgets.add(group_box, row.view)
        self._hover(row)
        widgets.add(box, group_box)
        return row

    @objc.python_method
    def _style_grid(self, box):
        box.setSpacing_(20)

    @objc.python_method
    def _colour_module(self):
        module = widgets.Module()
        module.add(widgets.eyebrow(tr("Colour")))
        rows = []
        for group, choices in pages.colour_groups(crossing.GLOW_COLOURS, self.effects_ready):
            if group != pages.TODAY:
                choices = [(value, tr(title), effects_overlay.palette(value)) for value, title in choices]
            else:
                choices = [(value, tr(title)) for value, title in choices]
            heading = module.add(widgets.label(tr(group), theme.TYPE["note"], 600, ink="ink_2"))
            module.body.setCustomSpacing_afterView_(8, heading)
            row = widgets.Swatches(choices, responsive=True)
            module.add(row.view)
            module.body.setCustomSpacing_afterView_(18, row.view)
            rows.append(row)
        self.glow_colour_select = widgets.Linked(rows, on_change=self._changed, host=module.body)
        module.add(widgets.note(tr("Any colour works with any style. The notch's Outline uses it too; Island keeps Beamer's own cyan.")).view)
        return module

    @objc.python_method
    def _trackpad_module(self):
        module = widgets.Module()
        module.add(widgets.eyebrow(tr("Trackpad")))
        module.add(self._own_note())
        self.haptics_box = widgets.Switch(tr("Tick as the push builds"), on_change=self._changed)
        module.add(self.haptics_box.view)
        self.tick_steps_select = widgets.Segmented(
            [("quarters", tr("Every quarter")), ("halves", tr("Halfway")), ("breakthrough", tr("Only when through"))],
            on_change=self._changed,
        )
        ticks, _caption = widgets.field_row(tr("Ticks at"), self.tick_steps_select.view)
        self.tick_steps_pair = ticks
        module.add(ticks)
        # On the press, not the release: macOS plays nothing once the finger has left the trackpad.
        self.try_button = widgets.Button(tr("Try it"), self, "tryTick:", scale="small")
        self.try_button.view.sendActionOn_(AppKit.NSEventMaskLeftMouseDown)
        feel = widgets.stack(vertical=False, spacing=10)
        feel.addArrangedSubview_(self.try_button.view)
        feel.addArrangedSubview_(widgets.hug(widgets.box(), AppKit.NSLayoutPriorityDefaultLow))
        module.add(widgets.field_row(tr("Feel it"), feel)[0])
        self.tick_note = widgets.note()
        module.add(self.tick_note.view)
        return module

    def tryTick_(self, _sender):
        """A whole push in half a second, the ticks the chosen steps give and then the
        breakthrough, for as long as the finger stays on the trackpad."""
        if self.haptics is None:
            return
        ticks = {"quarters": 3, "halves": 1}.get(self.tick_steps_select.value, 0)
        for index in range(ticks):
            AppHelper.callLater(0.12 * index, self.haptics.tick)
        AppHelper.callLater(0.12 * ticks + 0.08, self.haptics.thud)

    @objc.python_method
    def _modifier_module(self):
        module = widgets.Module()
        module.add(widgets.eyebrow(tr("Modifier keys")))
        self.modifier_select = widgets.Segmented(
            [("semantic", tr("Same shortcuts")), ("positional", tr("Same positions"))], on_change=self._changed
        )
        self.modifier_select.view.setAccessibilityLabel_(tr("Modifier keys"))
        module.add(self.modifier_select.view)
        self.modifier_note = widgets.note()
        module.add(self.modifier_note.view)
        return module

    @objc.python_method
    def _connection_module(self):
        module = widgets.Module()
        # The page edits the first machine paired; any other is changed by pairing it again.
        self.connection_note = widgets.note()
        module.add(self.connection_note.view)
        self.hide_switch = widgets.Switch(tr("Hide addresses"), on_change=self._set_hide_addresses)
        module.add(self.hide_switch.view)
        module.add(widgets.note(
            tr("Hides every IP and hardware address in this window and its menu.")
        ).view)
        self.language_select = widgets.MenuChoice(
            [("auto", tr("Automatic")), *LANGUAGES.items()], on_change=self._set_language
        )
        module.add(widgets.field_row(tr("Language"), self.language_select.view)[0])
        self.language_note = widgets.note(tr("Beamer changes language when it next opens."))
        self.language_note.view.setHidden_(True)
        module.add(self.language_note.view)
        self.language_relaunch_button = widgets.Button(tr("Relaunch Beamer"), self, "relaunch:",
                                                       style="primary", full_width=True)
        self.language_relaunch_button.view.setHidden_(True)
        module.add(self.language_relaunch_button.view)
        host_box, self.host_field = widgets.field()
        secret_box, self.host_secret = widgets.field(secure=True)
        secret_box.setHidden_(True)
        self.host_boxes = (host_box, secret_box)
        host_line = widgets.stack(vertical=False, spacing=0)
        for box in self.host_boxes:
            host_line.addArrangedSubview_(box)
            widgets.hug(box, AppKit.NSLayoutPriorityDefaultLow)
        module.add(widgets.field_row(tr("Address"), host_line)[0])
        for control in (self.host_field, self.host_secret):
            control.setAccessibilityLabel_(tr("Address"))
        port_box, self.port_field = widgets.field()
        module.add(widgets.field_row(tr("First paired machine's port"), port_box)[0])
        self.port_field.setAccessibilityLabel_(tr("First paired machine's port"))
        self.wake_state = widgets.Label("", theme.TYPE["small"], mono=True)
        module.add(widgets.field_row(tr("Wake-on-LAN"), self.wake_state.view)[0])
        self.wake_hint = widgets.note()
        module.add(self.wake_hint.view)
        module.add(widgets.hairline())
        line = widgets.stack(vertical=False, spacing=12)
        line.addArrangedSubview_(widgets.note(tr("A new address or port takes effect when you connect."), reading=False).view)
        line.addArrangedSubview_(widgets.Button(tr("Connect"), self, "connect:", style="primary").view)
        module.add(line)
        return module

    @objc.python_method
    def _access_module(self):
        module = widgets.Module()
        self.access_status, self.access_button = self._permission_row(
            module, tr("Accessibility"), tr("Lets Beamer move this Mac's pointer and type on it when another machine drives."),
            "requestAccessibility:",
        )
        module.add(widgets.hairline())
        self.input_status, self.input_button = self._permission_row(
            module, tr("Input Monitoring"), tr("Lets Beamer read this keyboard and trackpad, to send them to another machine."),
            "requestInputMonitoring:",
        )
        module.add(widgets.hairline())
        module.add(widgets.eyebrow(tr("At login")))
        module.add(widgets.note(tr(
            "On Overview, turn on Start Beamer when you log in. If macOS needs approval, that page shows the status and opens Login Items settings."
        )).view)
        self.capture_status = widgets.note()
        module.add(self.capture_status.view)
        self.relaunch_button = widgets.Button(tr("Relaunch Beamer"), self, "relaunch:", style="primary", full_width=True)
        self.relaunch_button.view.setHidden_(True)
        module.add(self.relaunch_button.view)
        return module

    @objc.python_method
    def _permission_row(self, module, name, purpose, action):
        line = widgets.stack(vertical=False, spacing=10)
        line.setAlignment_(AppKit.NSLayoutAttributeCenterY)
        words = widgets.stack(spacing=4)
        widgets.add(words, widgets.Label(name, theme.TYPE["body"], 600).view)
        widgets.add(words, widgets.note(purpose).view)
        status = widgets.Label(tr("Required"), theme.TYPE["small"], mono=True, ink="amber", tracking=theme.STATE_TRACKING, upper=True)
        widgets.add(words, status.view)
        line.addArrangedSubview_(widgets.hug(words, AppKit.NSLayoutPriorityDefaultLow))
        button = widgets.Button(tr("Grant"), self, action)
        line.addArrangedSubview_(button.view)
        module.add(line)
        return status, button

    @objc.python_method
    def _ways_in_sentence(self):
        held = self._selected_ways()
        methods = set(self._methods())
        edge = held["side"]
        key = None
        if "shortcut" in methods:
            key = widgets.key_title(self.key_recorder.value)
        pushes = []
        if edge and "edge" in methods:
            pushes.append(tr("the {side} edge").format(side=tr(core_ways.EDGE_NAMES[edge])))
        elif edge and "part" in methods:
            pushes.append(pages.parts_phrase(edge, held["parts"]))
        if "corner" in methods:
            corner = tr(CORNER_NAMES[held["corner"]])
            pushes.append(tr("the {corner} corner").format(corner=corner))
        if "notch" in methods:
            pushes.append(tr("the notch"))
        ways = trc("list", " or ").join(pushes)
        hold = self.style_select.value == "hold"
        if key and pushes:
            template = tr("Hold {key}, or push through {ways}") if hold else tr("Double-tap {key}, or push through {ways}")
            return template.format(key=key, ways=ways)
        if key:
            return (tr("Hold {key}") if hold else tr("Double-tap {key}")).format(key=key)
        return tr("Push through {ways}").format(ways=ways) if pushes else ""
    @objc.python_method
    def _commit(self):
        bar = widgets.box("ground")
        self.message_label = widgets.note(pages.footer(None))
        line = widgets.stack(vertical=False, spacing=16)
        line.addArrangedSubview_(self.message_label.view)
        # Without a slack view the line is as wide as the note (at most 560 pt), the bar and the
        # pane follow it, and the window can never be wider than the sidebar plus that: 773 pt.
        line.addArrangedSubview_(widgets.hug(widgets.box(), AppKit.NSLayoutPriorityDefaultLow))
        bar.addSubview_(line)
        widgets.pin(line, bar, (12, 16, 12, 16))
        return bar

    @objc.python_method
    def _say(self, message, ink="ink_2"):
        self.message_label.set(self._shown(message), ink=ink)

    @objc.python_method
    def show(self):
        self._refresh_update_notice()
        if self.activation_policy_handler is not None:
            self.activation_policy_handler(window_open=True)
        self._select_page(pages.opening_page(
            self.page if self.opened else None, accessibility_granted(), input_monitoring_granted()
        ))
        self.opened = True
        # Closed, the window stopped refreshing; bring it up to date before it is seen.
        self.refresh()
        self.window.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        self._run_previews()

    @objc.python_method
    def _set_hide_addresses(self, on):
        try:
            cfg = self.settings_store.save(config_to_raw(replace(self.controller.cfg, hide_addresses=bool(on))))
        except SettingsError as exc:
            self.logger.warning("Hide addresses not saved: %s", exc)
            return
        self.controller.update_config(cfg)
        self._show_host_field()
        self._pcs_key = None
        self.refresh()

    @objc.python_method
    def _set_language(self, language):
        try:
            cfg = self.settings_store.save(config_to_raw(replace(self.controller.cfg, language=language)))
        except SettingsError as exc:
            self.logger.warning("language setting not saved: %s", exc)
            return
        self.controller.update_config(cfg)
        self.language_note.view.setHidden_(False)
        self.language_relaunch_button.view.setHidden_(False)

    @objc.python_method
    def _show_host_field(self):
        """The address fields as dots while addresses are hidden, still editable."""
        hide = self.controller.cfg.hide_addresses
        for (plain_box, secret_box), plain, secret in ((self.host_boxes, self.host_field, self.host_secret),
                                                       (self.panel.find_boxes, self.panel.find_field, self.panel.find_secret)):
            if hide == plain_box.isHidden():
                continue
            if hide:
                secret.setStringValue_(plain.stringValue())
            else:
                plain.setStringValue_(secret.stringValue())
            plain_box.setHidden_(hide)
            secret_box.setHidden_(not hide)
        if self.hide_switch.value != hide:
            self.hide_switch.value = hide
        self.language_select.value = self.controller.cfg.language

    @objc.python_method
    def _host_text(self):
        return (self.host_secret if self.controller.cfg.hide_addresses else self.host_field).stringValue()

    def connect_(self, _sender):
        """The Connection page's own commit: a new address, port or token has to reconnect, so it
        waits for the button rather than applying as it is typed."""
        raw = config_to_raw(self.controller.cfg)
        try:
            raw["host"] = self._host_text().strip()
            raw["port"] = int(self.port_field.stringValue().strip())
            if raw["host"] != self.controller.cfg.host:
                # What was learned belonged to the old address.
                raw["pc_name"] = ""
                raw["mac_address"] = ""
            cfg = self.settings_store.save(raw, pin_address=True)
        except (SettingsError, TypeError, ValueError) as exc:
            self._say(str(exc), "fault")
            return
        self._say(tr("Saved. Connecting…"), "signal")
        self.controller.update_config(cfg)

    @objc.python_method
    def _apply_settings(self):
        """Writes every setting outside the Connection page and applies it without dropping the link:
        this Mac's own through the flat settings, the chosen machine's ways through `set_ways`."""
        self._save_pending = False
        raw = config_to_raw(self.controller.cfg)
        try:
            raw["trigger_key"] = self.key_recorder.value
            raw["ignored_inputs"] = list(self.ignored_entries)
            raw["mouse_only"] = self.mouse_only_box.value
            raw["keyboard_sync"] = self.keyboard_sync_box.value
            raw["trigger_style"] = self.style_select.value
            raw["double_tap_ms"] = self.double_tap_ruler.value
            raw["pointer_speed"] = self.pointer_ruler.value / 100
            raw["scroll_speed"] = self.scroll_ruler.value / 100
            raw["reverse_scroll"] = self.reverse_scroll_box.value
            raw["appearance"] = self.appearance_select.value or "system"
            if self.modifier_select.value in config_module.KEY_MAP_STYLES:
                raw["key_map"] = self.modifier_select.value
            methods = [name for name in raw["crossing"]["methods"] if name != "shortcut"]
            if self.method_boxes["shortcut"].value:
                methods.append("shortcut")
            raw["crossing"] = {
                "methods": methods,
                "edge_parts": raw["crossing"]["edge_parts"],
                "edge": raw["crossing"]["edge"],
                "corner": raw["crossing"]["corner"],
                "resistance_px": self.resistance_ruler.value,
                "haptics": self.haptics_box.value,
                "glow": self.glow_box.value,
                "shortcut_arrival": self.landing_box.value,
                "shortcut_arrival_style": self.switch_style_select.value or "match",
                "notch_style": self.notch_style_select.value,
                "notch_after_ms": self.notch_after_select.value,
                # Kept so a saved file stays valid; macOS has no strength to choose.
                "haptic_feel": self.controller.cfg.crossing.get("haptic_feel", "medium"),
                "haptic_steps": self.tick_steps_select.value,
                "glow_style": self.glow_style_select.value,
                "glow_colour": self.glow_colour_select.value,
                "effect_length": self.length_select.value or "normal",
                "effect_size": self.size_select.value or "medium",
                "effect_pace": self.length_select.numeric_value(),
                "effect_size_fraction": self.size_select.numeric_value(),
                "frame_rate": self.frame_rate_select.value or "display",
                "block_while_dragging": self.dragging_box.value,
                "shortcut_goes_to": self.shortcut_goes_to.value or "last",
                "hold_full_screen": self.hold_box.value,
                "arrangement_set_at": self.controller.cfg.crossing.get("arrangement_set_at", 0),
            }
            cfg = self.controller.cfg
            before = settings_sync.mac_values(config_to_raw(cfg))
            after = settings_sync.mac_values(raw)
            # Each shared value a person changed here gets its own stamp, Same on or off, so the
            # latest real choice is the one that wins when it is on.
            held = settings_sync.held_stamps(cfg.same_stamps, cfg.same_set_at, cfg.same_by)
            stamps, stamp = settings_sync.stamp_changes(held, before, after, self.own_id(),
                                                        floor=max(cfg.same_set_at, cfg.design_set_at))
            shared = cfg.same_on_both and settings_sync.changed(before, after, cfg.same_groups)
            # Stored before the outer stamp can move, so no unstamped value takes the new one.
            if stamp is not None or shared:
                raw["same_stamps"] = settings_sync.stored_stamps(stamps)
            design_changed = any(before.get(key) != after.get(key) for key in settings_sync.DESIGN_KEYS)
            if design_changed:
                raw["design_set_at"] = settings_sync.next_stamp(max(cfg.design_set_at, cfg.same_set_at), time.time())
                raw["design_by"] = self.own_id()
            if shared:
                raw["same_set_at"] = max(settings_sync.next_stamp(
                    max(cfg.same_set_at, raw.get("design_set_at", 0)), time.time()), stamp or 0)
                raw["same_by"] = self.own_id()
            cfg = self.settings_store.save(raw)
        except (SettingsError, TypeError, ValueError) as exc:
            self._say(str(exc), "fault")
            return
        self.controller.apply_settings(cfg)
        set_divisor(effects.frame_divisor(cfg.crossing.get("frame_rate", "display")))
        if self.previews is not None:
            # The tiles' renderers read the colour from the applied settings, which land only now.
            self.previews.repaint()
        if shared:
            self._send_same()
        elif design_changed:
            self._send_same()
        self._refresh_design_follow()
        if self._write_ways():
            self._say(tr("Saved. Changes apply as you make them."), "ink_2")

    @objc.python_method
    def _crossing_args(self):
        return dict(platform="mac", capable=self.controller.crossing_capable(),
                    live=self._live, gone=self._gone, now=time.time())

    @objc.python_method
    def _write_ways(self, peer=None, **changes):
        """Write an explicit ledger or map choice and apply its shared crossing outcome."""
        if not changes:
            return True
        peer = peer or self.chosen_peer
        current = self._view_settings()
        if peer not in {entry["id"] for entry in current["peers"]}:
            return True
        try:
            if self._editing_other():
                if "profiles" not in self._profile_caps().get(self.viewed_machine, ()):
                    return False
                outcome = self.controller.crossing_event(crossing_state.edit_other, self.viewed_machine, peer,
                    **changes, platform="mac", now=time.time())
                if not outcome.get("accepted", True):
                    if outcome.get("reason") == "desk_full":
                        self._say(tr("The desk is full. Remove an unused profile before adding more ways."), "amber")
                    return False
            else:
                self.controller.crossing_event(crossing_state.edit_ways, peer, **changes, **self._crossing_args())
        except (SettingsError, OSError, KeyError, TypeError, ValueError) as exc:
            self._say(str(exc), "fault")
            self._load_ways()
            self._reflect()
            return False
        self._load_ways()
        self._reflect()
        return True

    @objc.python_method
    def _tell(self, peer):
        """This Mac's side for `peer` as held, with whether a way leads there (WIRE.md section 8), over
        this Mac's link to it, else the one it opened here. Nothing while no side is set."""
        entry = next((item for item in self.settings_store.current()["peers"] if item["id"] == peer), None)
        if entry is None or entry.get("side") not in crossing.EDGES:
            return
        side, at, by = entry["side"], entry.get("side_set_at", 0), entry.get("side_by") or None
        if not self.controller.send_arrangement(peer, side, at, by) and self.windows_input is not None:
            self.windows_input.send_arrangement(peer, side, at, by)

    @objc.python_method
    def apply_arrangement(self, peer, edge, set_at, by, way_back=None, way_back_by=None):
        """Picture peers supply return status; old peers still settle a side by stamp."""
        self._flush()
        try:
            outcome = self.controller.crossing_event(crossing_state.receive_arrangement, peer,
                dict(edge=edge, set_at=set_at, by=protocol.read_id(by), way_back=way_back,
                     way_back_by=protocol.read_id(way_back_by)), **self._crossing_args())
        except (SettingsError, TypeError, ValueError):
            self.logger.exception("could not save the arrangement %s sent", peer)
            return
        if not outcome["changed"]:
            return
        self._load(config_to_raw(self.controller.cfg))
        self.refresh()

    def quitApp_(self, _sender):
        self.quit_handler()

    def showAbout_(self, _sender):
        show_about_panel()

    def requestAccessibility_(self, _sender):
        ApplicationServices.AXIsProcessTrustedWithOptions(
            {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
        )
        self.refresh()

    def requestInputMonitoring_(self, _sender):
        Quartz.CGRequestListenEventAccess()
        self.refresh()

    def relaunch_(self, _sender):
        """Opens this app again once this process has gone, which the single-instance lock needs,
        then quits. Run from source there is no bundle to reopen, so it only says what to do."""
        bundle = AppKit.NSBundle.mainBundle().bundlePath()
        if not bundle.endswith(".app"):
            self.capture_status.set(tr("Quit Beamer and start it again."), ink="amber")
            return
        subprocess.Popen(
            ["/bin/sh", "-c", 'while kill -0 "$0" 2>/dev/null; do sleep 0.2; done; exec /usr/bin/open "$1"', str(os.getpid()), bundle],
            start_new_session=True,
        )
        self.quit_handler()

    def toggleRedirect_(self, _sender):
        if not self.controller.input_ready:
            self._say(tr("Grant both Mac permissions first."), "fault")
            return
        if not self.controller.redirecting and self.controller.can_wake:
            self.controller.wake()
            self._say(tr("Waking {name}. It connects on its own once it is up.").format(name=self._first_label()), "ink_2")
        elif not self.controller.set_redirecting(not self.controller.redirecting, reason="menu"):
            self._say(self.controller.connection_status, "fault")
        self.refresh()

    def togglePause_(self, _sender):
        self.controller.crossing_paused = not self.controller.crossing_paused
        self.refresh()

    @objc.python_method
    def _persist_hardware(self, peer, mac):
        """A machine's hardware address, learnt from the ARP table when its link came up."""
        try:
            self.settings_store.set_hardware(peer, mac)
        except SettingsError as exc:
            self.logger.warning("hardware address not saved: %s", exc)

    @objc.python_method
    def _persist_mac(self, _mac):
        """The controller has already put the learned address on cfg; this writes it down."""
        try:
            self.settings_store.save(config_to_raw(self.controller.cfg))
        except SettingsError as exc:
            self.logger.warning("hardware address not saved: %s", exc)

    @objc.python_method
    def _crossing_state_args(self):
        controller = self.controller
        cfg = controller.cfg
        return (
            bool(cfg.host and cfg.auth_token), cfg.send_to_windows, controller.connected,
            controller.crossing.armed, controller.crossing_paused, controller.full_screen_app,
        )

    @objc.python_method
    def _crossing_state_sentence(self):
        return pages.crossing_state_sentence(*self._crossing_state_args())

    @objc.python_method
    def _current_chain_sentence(self):
        controller = self.controller
        identity = controller.identity()
        own_id = protocol.id_text(identity["id"])
        with controller._owner_lock:
            route = controller.owner.chain
        if len(route) <= 1:
            driver = self.windows_input.server.owner if self.windows_input is not None else None
            route = (protocol.id_text(driver), own_id) if driver is not None else ()
        if not route:
            return tr("Current chain: Idle")
        entries = controller.book.peers()
        labels = peerlist.labels(entries)
        names_by_id = {entry.get("id"): labels.get(entry.get("token", ""), "another machine")
                       for entry in entries if entry.get("id")}
        names = [identity["name"] if ident == own_id else names_by_id.get(ident, "another machine")
                 for ident in route]
        return tr("Current chain: {route}").format(route=" → ".join(names))

    @objc.python_method
    def _permission(self, status, button, granted):
        status.set(tr("Granted") if granted else tr("Required"), ink="signal" if granted else "amber")
        # Granted, the state says so; a dead "Granted" button beside it only said it twice.
        motion.set_hidden(button.view, granted)

    @objc.python_method
    def retry_capture(self, access=None, listening=None):
        """Start input capture once both grants are in. The one part of refresh that must run
        while the window is closed; the rest redraws what nobody can see."""
        controller = self.controller
        if controller.input_ready:
            return
        now = time.monotonic()
        if now - self.last_capture_attempt < CAPTURE_RETRY_INTERVAL_SECONDS:
            return
        if access is None:
            access, listening = accessibility_granted(), input_monitoring_granted()
        if access and listening:
            self.last_capture_attempt = now
            controller.start_input_capture()

    @objc.python_method
    def refresh(self):
        controller = self.controller
        self._follow_first_peer()
        access = accessibility_granted()
        listening = input_monitoring_granted()
        self.retry_capture(access, listening)
        self._run_previews()
        # Pairing has its own inputs and countdown; it must keep polling even while the
        # rest of the window is unchanged. Copies detect link-thread and in-place edits.
        self.panel.refresh()
        state = link_state.describe(controller)
        rows = self.panel.rows()
        response = self._header_response_time()
        key = (controller.cfg, self.settings_store.current(), state, rows, response,
               access, listening, controller.input_ready, controller.input_error,
               self.granted_at_launch, controller.redirecting, controller.waking,
               controller.can_wake, controller.windows_locked, controller.round_trip_ms,
               controller.notch_range, self._crossing_state_args(), self.chosen_peer,
               self.viewed_machine, getattr(controller, "desk_last_heard", {}),
               getattr(controller, "desk_link_times", {}),
               self.wide, self._design_peer_caps(), self._design_states,
               self._peer_too_old(), self.shortcut_goes_to.value,
               frozenset(controller.crossing_capable()), controller.crossing_display_map())
        if key == getattr(self, "_refresh_key", None) and not controller.redirecting:
            return
        self._refresh_key = copy.deepcopy(key)
        self._show_same()
        self._permission(self.access_status, self.access_button, access)
        self._permission(self.input_status, self.input_button, listening)
        needs_relaunch = access and listening and not self.granted_at_launch
        self.relaunch_button.view.setHidden_(not needs_relaunch)
        if needs_relaunch:
            self.capture_status.set(tr("Both granted. Relaunch Beamer so macOS passes it the keyboard."), ink="amber")
        elif controller.input_ready:
            self.capture_status.set(tr("Input capture ready."), ink="signal")
        elif access and listening:
            detail = controller.input_error
            # Translators: {detail} is a diagnostic reason supplied by macOS.
            self.capture_status.set(tr("Retrying input capture… {detail}").format(detail=detail) if detail else tr("Retrying input capture…"), ink="fault")
        else:
            self.capture_status.set(
                tr("Beamer needs both, granted separately. macOS only delivers keyboard events to a process started after the grant, so relaunch Beamer once you have given them."),
                ink="ink_2",
            )

        # Names arrive with each link's hello, and the pairings a zone needs with its `paired`.
        self._show_machines()
        self._show_arrangement()
        self._place_machines(not rows)
        refused = any(row.state.key == "token" for row in rows)
        response_peer, response_time, response_connected = response
        self.sidebar.set_link(replace(state, word=self._shown(state.word)),
                              self._shown(response_peer if response_connected else self._machine_label()) if rows else "—",
                              response_time_ms=response_time, connected=response_connected)
        dormant = any(peerlist.dormant_reason(entry) for entry in controller.book.peers())
        dots = pages.dots(access, listening, "token" if refused else state.key, dormant)
        self.sidebar.set_dots(dots)
        self.link_led.set(state.led, state.blink)
        self.link_tag.set(state.tag)
        if (self._shown(state.word), state.tone) != (self.state_word.text, self.state_word.ink):
            motion.cross_fade(self.state_word.view)
        self.state_word.set(self._shown(state.word), ink=state.tone)
        self.state_detail.set(self._shown(state.detail))

        round_trip = controller.round_trip_ms
        if controller.redirecting:
            if round_trip is not None:
                self.latency.append(round_trip)
        else:
            self.latency.clear()
        live = controller.redirecting and round_trip is not None
        self.round_trip.value.set(str(round_trip) if live else "—", ink="signal" if live else "ink")
        self.spark.samples = list(self.latency)
        self.spark.live = controller.redirecting
        self.spark.setNeedsDisplay_(True)
        cfg = controller.cfg
        self._show_peer()
        # The first machine by name, which the Connection page edits; peer_label follows the
        # machine in question instead.
        peers = peerlist.desktops(controller.book.peers())
        first = peerlist.labels(peers)[peers[0]["token"]] if peers else None
        self.connection_note.set(self._shown(
            tr("The address and port of {name}, the first machine paired. Another machine is changed by pairing it again.").format(name=first)
            if first else tr("Nothing is paired yet. Pairing fills the address and port in.")))

        self.pause_button.set_title(tr("Resume crossing") if controller.crossing_paused else tr("Pause crossing"))
        self.pause_button.set_style("primary" if controller.crossing_paused else "plain")
        # With only the shortcut on there is no edge to pause, so there is no button either.
        args = self._crossing_state_args()
        blocked = pages.crossing_state_blocked(*args[:3])
        self.pause_button.view.setHidden_(not controller.crossing.armed)
        motion.set_hidden(self.pause_row, not (controller.crossing.armed or blocked))
        sentence = pages.crossing_state_sentence(*args)
        if sentence != self.crossing_state.text:
            motion.cross_fade(self.crossing_state.view)
        self.crossing_state.set(sentence)
        has_notch = controller.notch_range is not None
        if has_notch != self.has_notch:
            self.has_notch = has_notch
            self._reflect()
        if controller.redirecting:
            self.toggle_button.set_title(tr("Return input to Mac"))
        elif controller.waking:
            self.toggle_button.set_title(self._shown(tr("Waking {name}…").format(name=self._first_label())))
        elif controller.can_wake:
            self.toggle_button.set_title(self._shown(tr("Wake {name}").format(name=self._first_label())))
        else:
            self.toggle_button.set_title(
                self._shown(tr("Send input to {name}").format(name=self._first_label())) if controller.peer_label else tr("Send input"))
        self.toggle_button.set_style("live" if controller.redirecting or controller.windows_locked else "primary")
        self.toggle_button.set_enabled(
            controller.input_ready
            and not controller.waking
            and (controller.connected or controller.can_wake)
        )
        mac = cfg.mac_address
        self.wake_state.set(self._shown(mac) if mac else tr("Not yet learned"), ink="ink" if mac else "ink_3")
        self.wake_hint.set(
            self._shown(tr("Crossing to {name} while it sleeps sends a wake-up packet and waits for it.").format(name=first or tr("the other machine")))
            if mac
            else tr("Read from the network the first time this Mac connects; nothing to type.")
        )


class StatusItemClick(AppKit.NSObject):
    """Splits the menu bar item's clicks: left opens the window, right opens the menu.

    An NSStatusItem with a menu attached hands it every click, and rumps attaches one, so the
    menu is detached at startup and put back for the moment a right click needs it."""

    def initWithTray_(self, tray):
        self = objc.super(StatusItemClick, self).init()
        if self is None:
            return None
        self.tray = tray
        return self

    def clicked_(self, _sender):
        item = self.tray.status_item_view
        event = AppKit.NSApp.currentEvent()
        right = event is not None and (
            event.type() == AppKit.NSEventTypeRightMouseUp
            or event.modifierFlags() & AppKit.NSEventModifierFlagControl
        )
        if not right:
            self.tray.control_window.show()
            return
        item.setMenu_(self.tray.detached_menu)
        item.button().performClick_(None)
        item.setMenu_(None)


class _LaunchWatch(AppKit.NSObject):
    """Keeps non-default launches in the menu bar without opening a window."""

    def initWithTray_(self, tray):
        self = objc.super(_LaunchWatch, self).init()
        self.tray = tray
        AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "launched:", AppKit.NSApplicationDidFinishLaunchingNotification, None
        )
        return self

    def launched_(self, note):
        self.tray.notices.ask()
        info = note.userInfo() or {}
        default_launch = info.get("NSApplicationLaunchIsDefaultLaunchKey")
        if login_item.is_background_launch(default_launch):
            self.tray.hidden = True
            self.tray.apply_activation_policy(window_open=False)


def _tray_item(title, **kwargs):
    return rumps.MenuItem(title, **kwargs)


def _set_tray_title(item, title):
    item.title = title


# What a click on the Dock icon does while no window is open. With the menu bar item switched off
# the Dock is the only way back to Beamer, and rumps' delegate has no answer to the reopen event.
_reopen_window = None


def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, has_visible_windows):
    if not has_visible_windows and _reopen_window is not None:
        _reopen_window()
    return True


objc.classAddMethods(rumps.rumps.NSApp, [objc.selector(
    applicationShouldHandleReopen_hasVisibleWindows_, selector=b"applicationShouldHandleReopen:hasVisibleWindows:",
    signature=b"Z@:@Z")])


class TrayApp(rumps.App):
    def __init__(self, controller, settings_store, logger, hidden=False):
        self.hidden = hidden
        self.notices = notices.Notices(logger)
        self._last_refusal = ""
        self._refusal_notices = peerlist.RefusalThrottle()
        self.launch_watch = _LaunchWatch.alloc().initWithTray_(self)
        self.controller = controller
        self.settings_store = settings_store
        self.logger = logger
        self.apply_activation_policy(window_open=False)
        # Before anything reads or posts a key: the table follows this Mac's layout from here on.
        self.layout_observer = keyboard_layout.watch()
        self.control_window = ControlWindow.alloc().initWithController_settingsStore_logger_(
            controller, settings_store, logger
        )
        self.control_window.activation_policy_handler = self.apply_activation_policy
        global _reopen_window
        _reopen_window = self.control_window.show
        self.control_window.quit_handler = self.quit_app
        self.control_window.direction_handler = self._set_direction
        self.control_window.left_out_handler = self._set_left_out
        install_main_menu(self.control_window)
        self.gesture_overlay = GestureOverlay(controller, logger)
        self.edge_glow = EdgeGlow(controller, logger)
        self.notch_island = NotchIsland(controller, logger)
        self.notch_beam = NotchBeam(controller, logger)
        self.effects_overlay = effects_overlay.EffectsOverlay(controller, logger)
        self.identify_overlay = identify_overlay.IdentifyOverlay(logger)
        self.controller.on_identify_screens = lambda labels: AppHelper.callAfter(self.identify_overlay.show, labels)
        self.haptics = Haptics(logger)
        self._resistance_pressure_timer = None
        self.control_window.preview = self.edge_glow.preview
        self.control_window.haptics = self.haptics
        self.controller.on_user_alert = self.notify_user
        self.controller.on_crossing = self.crossing_feedback
        self.controller.on_arrangement = self._arrangement
        self.controller.on_mac_learned = lambda peer, mac: AppHelper.callAfter(self.control_window._persist_hardware, peer, mac)
        # The other direction, listening from the moment Beamer opens: the PC
        # may want to send its own keyboard here before this Mac has ever
        # crossed the other way.
        self.windows_input = WindowsInput(
            controller,
            logger,
            arrangement_callback=self._arrangement,
            pressure_callback=self._driven_pressure,
            arrival_callback=self._driven_arrival,
        )
        self.control_window.windows_input = self.windows_input
        # Same on all machines, over either link: applied on the main thread, and announced
        # with the arrangement the moment either link comes up.
        controller.announce = self._announce
        self.windows_input.settings_callback = self._settings
        controller.on_settings = self._settings
        self.windows_input.sync(controller.cfg)
        # Pairing, both ways: this Mac shows a code or enters one. `peers` and `store` run on the
        # service's socket threads under its lock, so they read and write through the settings
        # store's own lock and never wait on the main thread.
        identity = controller.identity()
        self.pairing = pairing.PairingService(
            identity["name"], bytes(identity["id"]), identity["platform"], lambda: controller.own_port,
            controller.book.peers, settings_store.add_peer, on_paired=self._hosted_pairing, logger=logger,
        )
        self.control_window.panel.service = self.pairing
        self.pairing.start()
        # A machine that changed address is followed by its beacon; bridge sets cfg.host, this saves it.
        controller.discovery = self.pairing
        controller.on_host_learned = lambda host: AppHelper.callAfter(self.control_window._persist_mac, None)
        self.update_checker = updates.Checker(
            VERSION, lambda: self.controller.cfg.check_updates,
            lambda found: AppHelper.callAfter(self._update_found, found), logger=logger,
            on_check=lambda state, found: AppHelper.callAfter(self.control_window.show_update_check, state, found),
        )
        self.control_window.update_checker = self.update_checker
        self.control_window.update_handler = self._update_found
        self._notch_failed = False
        self.header_item = header = _tray_item(f"Beamer {VERSION}", callback=None)
        self.update_url = None
        self.status_item = _tray_item(self._drive_status(), callback=None)
        self.toggle_item = _tray_item(TOGGLE_ITEM, callback=self.toggle_redirect)
        # With several machines to send to, one item each after the one above, keyed by machine id.
        self._send_keys = []
        self.pause_item = _tray_item(tr("Pause crossing"), callback=self.toggle_pause)
        # One tick per direction, so either can be switched off while the other keeps working.
        self.send_item = _tray_item(tr("This Mac drives other machines"), callback=self.toggle_send_to_windows)
        self.receive_item = _tray_item(tr("Other machines drive this Mac"), callback=self.toggle_windows_drives)
        self.dock_item = _tray_item(tr("Show in Dock"), callback=self.toggle_dock)
        self.dock_item.state = self.settings_store.current().get("show_in_dock", True)
        self._symbol = None
        super().__init__(
            "Beamer",
            title=tr("Beamer • Local"),
            menu=[
                self.status_item,
                rumps.separator,
                header,
                rumps.separator,
                self.toggle_item,
                self.pause_item,
                rumps.separator,
                self.send_item,
                self.receive_item,
                rumps.separator,
                _tray_item(tr("Settings…"), callback=self.open_window),
                self.dock_item,
                _tray_item(tr("Reload configuration"), callback=self.reload_config),
                _tray_item(tr("Open log folder"), callback=self.open_log_folder),
                rumps.separator,
                _tray_item(tr("About Beamer"), callback=self.show_about),
                _tray_item(tr("Quit Beamer"), callback=self.quit_app),
            ],
            quit_button=None,
        )
        self.status_timer = rumps.Timer(self.refresh_status, 0.4)
        self.startup_timer = rumps.Timer(self.show_on_startup, 0.2)
        # Its own timer, slower than the status tick: the window list is a window-server call,
        # and once a second is the cache the tap thread reads from.
        self.full_screen_timer = rumps.Timer(self.check_full_screen, FULL_SCREEN_CHECK_INTERVAL_SECONDS)
        self.status_timer.start()
        self.startup_timer.start()
        self.full_screen_timer.start()
        self.update_checker.start()

    def _update_found(self, found, manual=False):
        """On the main thread. The version line in the menu becomes the way to the download."""
        if not manual and not self.controller.cfg.check_updates:
            found = None
        self.update_url = found[1] if found else None
        notice = updates.visible_notice(found, self.controller.cfg.check_updates,
                                       self.control_window.settings_store.current().get("dismissed_update_version", ""),
                                       requested=manual)
        if notice:
            _set_tray_title(self.header_item, tr("Beamer {version} is available…").format(version=notice[0]))
            self.header_item.set_callback(self.open_update)
        else:
            _set_tray_title(self.header_item, tr("Beamer {version}").format(version=VERSION))
            self.header_item.set_callback(None)
        self.control_window.show_update(found, manual=manual)

    def open_update(self, _sender):
        if self.update_url:
            AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.URLWithString_(self.update_url))

    def open_log_folder(self, _sender):
        LOG_DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
        AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.fileURLWithPath_(str(LOG_DIRECTORY)))

    def _announce(self):
        """What this Mac tells a machine on every new link beyond what the link itself announces
        (where the two sit, who else this Mac has): its settings state. On the link's own thread."""
        return [protocol.settings_msg(self.control_window.same_state())]

    def _settings(self, data, peer=None):
        AppHelper.callAfter(self.control_window.apply_same, data, peer)

    def _arrangement(self, peer, edge, set_at, by, way_back=None, way_back_by=None):
        """An arrangement from a machine, over either link, applied on the main
        thread -- it writes the settings file and redraws the window."""
        AppHelper.callAfter(self.control_window.apply_arrangement, peer, edge, set_at, by, way_back, way_back_by)

    def show_on_startup(self, _timer):
        self.startup_timer.stop()
        self.split_clicks()
        if not self.hidden:
            self.control_window.show()
        else:
            self.apply_activation_policy(window_open=False)

    def split_clicks(self):
        """Runs once the status item exists, which is after rumps has started the app."""
        self.status_item_view = self._nsapp.nsstatusitem
        self.detached_menu = self.status_item_view.menu()
        self.status_item_view.setMenu_(None)
        self.click_handler = StatusItemClick.alloc().initWithTray_(self)
        button = self.status_item_view.button()
        button.setTarget_(self.click_handler)
        button.setAction_("clicked:")
        button.sendActionOn_(AppKit.NSEventMaskLeftMouseUp | AppKit.NSEventMaskRightMouseUp)

    def refresh_status(self, _timer):
        self.windows_input.sync(self.controller.cfg)
        self.control_window.settle_presence()
        self.measure_notch()
        if self.control_window.window.isVisible():
            self.control_window.refresh()
        else:
            self.control_window.retry_capture()
        self.gesture_overlay.sync()
        controller = self.controller
        held = controller.crossing.armed and (controller.crossing_paused or controller.full_screen_app is not None)
        self.set_menu_bar_symbol(
            "windows" if controller.redirecting or controller.receiving else "held" if held else "local"
        )
        hide = controller.cfg.hide_addresses
        if controller.redirecting:
            _set_tray_title(self.toggle_item, tr("Return input to Mac"))
        else:
            _set_tray_title(self.toggle_item, pages.redact(
                tr("Send input to {name}").format(name=controller.peer_label) if controller.peer_label else tr("Send input"), hide))
        _set_tray_title(self.pause_item, tr("Resume crossing") if controller.crossing_paused else tr("Pause crossing"))
        self._machine_items()
        self._profile_items()
        self._direction_items()
        self._refresh_tray_state()

    def _machine_items(self):
        """With more than one machine to send to, Send input to each of them in place of the one
        item, and Bring input back on the one input is on; with one, the one item as it always was."""
        controller = self.controller
        wanted = pages.send_items(controller.book.peers(), controller.owner.on if controller.redirecting else None,
                                  controller.cfg.hide_addresses, live=self._peer_live)
        keys = [ident for ident, _title, _enabled in wanted]
        if keys != self._send_keys:
            for key in self._send_keys:
                del self.menu[key]
            anchor = TOGGLE_ITEM
            for ident in keys:
                # Created under its id, which is its key in the menu, then given its words.
                self.menu.insert_after(anchor, _tray_item(ident, callback=lambda _sender, ident=ident: self._send_to(ident)))
                anchor = ident
            self._send_keys = keys
        for ident, title, enabled in wanted:
            item = self.menu[ident]
            _set_tray_title(item, title)
            # rumps dims an item with no callback.
            item.set_callback((lambda _sender, ident=ident: self._send_to(ident)) if enabled else None)
        self.toggle_item.hidden = bool(keys)

    def _profile_items(self):
        settings = self.settings_store.current()
        choices = profiles.profile_list(settings)
        active = settings["crossing_desk"]["active"]["value"]
        key = (profiles.same_ways_on(settings), tuple((p["id"], p["label"]) for p in choices), active)
        if key == getattr(self, "_profile_menu_key", None):
            return
        self._profile_menu_key = key
        previous = getattr(self, "profile_item", None)
        if previous is not None:
            del self.menu[previous.title]
        self.profile_item = None
        if not key[0] or len(choices) < 2:
            return
        item = _tray_item(tr("Profile"), callback=None)
        for choice in choices:
            child = _tray_item(choice["id"], callback=lambda _sender, pid=choice["id"]:
                self.control_window._profile_event(crossing_state.switch_profile, pid))
            child.state = choice["id"] == active
            item.add(child)
            _set_tray_title(child, choice["label"])
        self.menu.add(item)
        self.profile_item = item

    def _peer_live(self, entry):
        link = self.controller.links.get(entry.get("token"))
        return link is not None and link.live()

    def _send_to(self, peer):
        """One machine's item: input goes to it, straight from the machine it is on (WIRE.md section 5,
        "Moving input"), or home when it is already there."""
        controller = self.controller
        if not controller.input_ready:
            self.notify_user("Beamer", tr("Grant both Mac permissions first."))
            return
        if controller.redirecting and controller.owner.on == peer:
            controller.set_redirecting(False)
        else:
            controller.set_redirecting(True, peer=peer, reason="menu")
        self.refresh_status(None)

    def _direction_items(self):
        """The two direction ticks, named for the machine when there is one, for every machine
        when there are several; mixed when the machines differ."""
        peers = self.controller.book.peers()
        hide = self.controller.cfg.hide_addresses
        labels = [pages.redact(label, hide) for label in peerlist.labels(peers).values()]
        if len(labels) == 1:
            send, receive = (tr("This Mac drives {name}").format(name=labels[0]),
                             tr("{name} drives this Mac").format(name=labels[0]))
        elif labels:
            send, receive = tr("This Mac drives every machine"), tr("Every machine drives this Mac")
        else:
            send, receive = tr("This Mac drives other machines"), tr("Other machines drive this Mac")
        _set_tray_title(self.send_item, send)
        _set_tray_title(self.receive_item, receive)
        desktops = peerlist.desktops(peers)
        send_labels = [pages.redact(label, hide) for label in peerlist.labels(desktops).values()]
        _set_tray_title(self.send_item,
                        tr("This Mac drives {name}").format(name=send_labels[0]) if len(send_labels) == 1 else
                        tr("This Mac drives every machine") if send_labels else tr("This Mac drives other machines"))
        for item, name in ((self.send_item, "send"), (self.receive_item, "allow_drive")):
            on = [peer.get(name) is True for peer in (desktops if name == "send" else peers)]
            item.state = 1 if on and all(on) else -1 if any(on) else 0

    def set_menu_bar_symbol(self, state):
        """One template glyph per state instead of words. rumps only takes an icon as a file
        path, so the NSImage goes in by hand; _icon_nsimage is what it reads when the status
        item is first built, before nsstatusitem exists."""
        if state != self._symbol:
            self._symbol = state
            self._icon_nsimage = menu_bar_glyph(state)
            self._title = ""
            try:
                self._nsapp.setStatusBarIcon()
            except AttributeError:
                pass
        # rumps decides the status item is empty by reading the deprecated
        # NSStatusItem.image(), which is nil even while the image is on screen,
        # so it keeps putting the app name back as the title. Clear it instead
        # of fighting it.
        try:
            item = self._nsapp.nsstatusitem
        except AttributeError:
            return
        if item.title():
            item.setTitle_("")

    def open_window(self, _sender):
        self.control_window.show()

    def toggle_dock(self, _sender):
        self.control_window._set_show_in_dock(not self.settings_store.current().get("show_in_dock", True))

    def apply_activation_policy(self, window_open=None):
        if window_open is None:
            window_open = self.control_window.window.isVisible()
        settings = self.settings_store.current()
        show_in_dock = settings.get("show_in_dock", True)
        dock_item = getattr(self, "dock_item", None)
        if dock_item is not None:
            dock_item.state = show_in_dock
        policy = dock_visibility.activation_policy(show_in_dock, window_open)
        AppKit.NSApp.setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyRegular
            if policy == "regular" else AppKit.NSApplicationActivationPolicyAccessory
        )
        item = getattr(self, "status_item_view", None)
        if item is not None:
            item.setVisible_(dock_visibility.menu_bar_visible(show_in_dock, settings.get("show_in_tray", True)))

    def toggle_send_to_windows(self, _sender):
        """The menu's tick covers every machine: all on turns them all off, anything else all on."""
        peers = peerlist.desktops(self.controller.book.peers())
        self._switch([peer["token"] for peer in peers], send=not all(peer.get("send") is True for peer in peers))

    def toggle_windows_drives(self, _sender):
        peers = self.controller.book.peers()
        self._switch([peer["token"] for peer in peers], allow_drive=not all(peer.get("allow_drive") is True for peer in peers))

    def _set_direction(self, token, **change):
        """One machine's switch from its row on Overview."""
        self._switch([token], **change)

    def _set_left_out(self, token, enabled):
        """Changes one desktop peer's local layout state and tells peers whose way back changed."""
        store = self.settings_store
        try:
            with store.lock:
                settings = copy.deepcopy(store.current())
                entry = next((item for item in settings["peers"] if item.get("token") == token), None)
                if entry is None or entry.get("port") == 0:
                    return
            self.controller.crossing_event(
                crossing_state.leave_out if enabled else crossing_state.put_back,
                entry["id"], **self.control_window._crossing_args())
        except (SettingsError, OSError, KeyError) as exc:
            self.logger.warning("layout state not saved: %s", exc)
            # Translators: {error} is the system error message.
            self.notify_user("Beamer", tr("Could not save the change: {error}").format(error=exc))
            self.control_window.panel.invalidate()
            self.control_window.peers_changed()
            return
        self.control_window.peers_changed()

    def _switch(self, tokens, **change):
        controller = self.controller
        entries = []
        try:
            for token in tokens:
                entry = self.settings_store.set_peer(token, **change)
                if entry is not None:
                    entries.append(entry)
        except SettingsError as exc:
            self.logger.warning("direction not saved: %s", exc)
            # Translators: {error} is the system error message.
            self.notify_user("Beamer", tr("Could not save the change: {error}").format(error=exc))
            # Some machines may have been saved before this one failed, and a switch the person
            # flipped shows a state that is not saved: read it all again.
            self.control_window.panel.invalidate()
            self.control_window.peers_changed()
            return
        if ((change.get("send") is False or change.get("in_use") is False)
                and any(entry.get("id") == controller.owner.on for entry in entries)):
            # Input comes home by this switch, and shows where the pointer is as any switch does.
            controller.set_redirecting(False)
        self.control_window.peers_changed()
        self.logger.info("directions: %s", ", ".join(
            f"{peerlist.label_for(self.controller.book.peers(), token=entry.get('token')) or 'Machine'} "
            f"send {'on' if entry['send'] else 'off'} drive {'on' if entry['allow_drive'] else 'off'}"
            for entry in entries))
        self.refresh_status(None)

    def _hosted_pairing(self, entry):
        """A machine paired with the code this Mac showed. On the pairing thread, after the entry
        was stored: the rest happens on the main thread."""
        AppHelper.callAfter(self.control_window.panel.hosted_pairing, entry)

    def reload_config(self, _sender):
        """Re-reads settings.json, for the times it was edited outside the window."""
        try:
            cfg = self.settings_store.load()
        except SettingsError as exc:
            self.logger.warning("configuration not reloaded: %s", exc)
            self.notify_user(tr("Configuration not reloaded"), tr("{error}").format(error=exc))
            return
        # The controller first: the window's address fields read Hide addresses from it.
        self.controller.update_config(cfg)
        self.control_window._load(config_to_raw(cfg))
        self.notify_user(tr("Configuration reloaded"), tr("Beamer is using the config on disk."))

    def show_about(self, _sender):
        show_about_panel()

    def measure_notch(self):
        """Re-read on every tick so plugging a display in above the MacBook, which moves the
        notch off the top of the desktop, is noticed without a restart."""
        if self._notch_failed:
            return
        try:
            self.controller.notch_range = notch_x_range()
            self.controller.notch_depth = notch_depth() if self.controller.notch_range is not None else 0.0
        except Exception:
            self._notch_failed = True
            self.controller.notch_range = None
            self.logger.exception("could not measure the notch; the notch method is off")

    def check_full_screen(self, _timer):
        """Feeds the controller the name of a full-screen frontmost app, or None. A failure
        stops the check for the run rather than logging once a second; crossing then simply
        stays on, as it was before this existed."""
        try:
            self.controller.full_screen_app = full_screen_app()
        except Exception:
            self.controller.full_screen_app = None
            self.full_screen_timer.stop()
            self.logger.exception("could not tell whether an app is full screen; crossing stays on")

    def toggle_pause(self, _sender):
        self.controller.crossing_paused = not self.controller.crossing_paused
        self.refresh_status(None)

    def crossing_feedback(self, kind, step):
        """Wired to controller.on_crossing, called off the event-tap and ack threads. Only the
        hop to the main thread happens here; haptics and the glow both touch AppKit."""
        AppHelper.callAfter(self._crossing_feedback_main, kind, step)

    def _crossing_feedback_main(self, kind, step):
        try:
            if kind in ("pressure", "tick", "cross"):
                update_strip = getattr(self, "_update_resistance_strip", None)
                if update_strip is not None:
                    try:
                        update_strip(kind, step)
                    except Exception:
                        self.logger.exception("Resistance strip feedback failed")
            if kind in ("home", "arrive"):
                # Input is home: the gesture panel goes now, not on the next status tick up to 0.4 s
                # later, when it would still sit over the landing animation and take the first click.
                self.gesture_overlay.sync()
            if kind == "home":
                self._show_landing(step.pin[0], step.pin[1])
                return
            feel = self.controller.cfg.crossing
            if kind == "arrive" and step.pin is not None and not self.effects_overlay.wanted(feel):
                self.effects_overlay.crossed_in()
            if feel["haptics"]:
                if kind == "tick":
                    if crossing.tick_fires(feel["haptic_steps"], step.pressure):
                        self.haptics.tick()
                elif kind in ("cross", "arrive"):
                    self.haptics.thud()
            if feel["glow"]:
                drawn = False
                if self.effects_overlay.wanted(feel):
                    # A crossing effect draws edge, corner and notch alike; the notch style is not used.
                    if kind == "arrive":
                        if step.pin is not None and step.mac_edge is not None:
                            self.effects_overlay.arrival("edge", step.pin[0], step.pin[1], step.mac_edge)
                    else:
                        self.effects_overlay.departure(kind, step)
                    # An effect that failed on this very event has turned itself off: today's glow
                    # draws it instead, so a breakthrough is never lost.
                    drawn = not self.effects_overlay.disabled
                if not drawn and step.via == "notch":
                    notch = self.notch_beam if feel["notch_style"] == "beam" else self.notch_island
                    notch.update(kind)
                elif not drawn:
                    self.edge_glow.update(kind, step)
        except Exception:
            self.logger.exception("crossing feedback failed")

    def _update_resistance_strip(self, kind, step):
        control = getattr(self, "control_window", None)
        strip = getattr(control, "push_strip", None)
        if strip is None:
            return
        resistance = strip.value
        edge = step.mac_edge or self.controller.cfg.crossing["edge"] or "right"
        if kind == "cross":
            if self._resistance_pressure_timer is not None:
                self._resistance_pressure_timer.invalidate()
                self._resistance_pressure_timer = None
            strip.set_pressure(resistance, edge, crossed=True)
            return
        pressure = step.pressure * resistance
        strip.set_pressure(pressure, edge)
        if pressure > 0 and self._resistance_pressure_timer is None:
            timer = AppKit.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                1 / 30, self, "refreshResistancePressure:", None, True
            )
            AppKit.NSRunLoop.mainRunLoop().addTimer_forMode_(timer, AppKit.NSRunLoopCommonModes)
            self._resistance_pressure_timer = timer
        elif pressure <= 0 and self._resistance_pressure_timer is not None:
            self._resistance_pressure_timer.invalidate()
            self._resistance_pressure_timer = None

    def refreshResistancePressure_(self, _timer):
        strip = self.control_window.push_strip
        pressure = self.controller.crossing.pressure_at(time.monotonic()) * strip.value
        strip.set_pressure(pressure)
        if pressure <= 0 and self._resistance_pressure_timer is not None:
            self._resistance_pressure_timer.invalidate()
            self._resistance_pressure_timer = None

    def _driven_pressure(self, edge, pressure, crossed, part=None):
        """The receiver's return edge while the PC drives this Mac, off its session thread: the push
        home plays the departure effect. Today's glow never drew this direction."""
        # Read here, beside the pressure it goes with: by the time the main thread runs, the
        # receiver may have warped the pointer again.
        try:
            cursor = desktop_mac.cursor_position()
        except Exception:
            self.logger.exception("could not read the pointer for the return edge")
            return
        AppHelper.callAfter(self._driven_pressure_main, edge, pressure, crossed, cursor, part)

    def _driven_pressure_main(self, edge, pressure, crossed, cursor, part=None):
        """`part` names the corner when the way home is one, so it plays its corner form."""
        corner = part if part in return_edge.CORNERS else None
        try:
            feel = self.controller.cfg.crossing
            if self.effects_overlay.wanted(feel):
                self.effects_overlay.return_push(edge, pressure, crossed, cursor, corner)
            elif feel["glow"]:
                self.edge_glow.driven(edge, pressure, crossed, cursor, corner)
        except Exception:
            self.logger.exception("return edge feedback failed")

    def _driven_arrival(self, edge, x, y):
        """The PC's input has just come to this Mac: placed at `edge` by a crossing, or left where
        it was by a switch, when `edge` is None."""
        AppHelper.callAfter(self._driven_arrival_main, edge, x, y)

    def _driven_arrival_main(self, edge, x, y):
        try:
            if edge is None:
                self._show_landing(x, y)
            elif self.effects_overlay.wanted(self.controller.cfg.crossing):
                self.effects_overlay.arrival("edge", x, y, edge)
            else:
                self.effects_overlay.crossed_in()
        except Exception:
            self.logger.exception("arrival feedback failed")

    def _show_landing(self, x, y):
        """Input came to this Mac by a switch rather than a crossing, and the pointer is at (x, y)
        in Quartz points: show where, if the Design page says to."""
        try:
            if self.effects_overlay.wanted_switch(self.controller.cfg.crossing):
                self.effects_overlay.switched(x, y)
        except Exception:
            self.logger.exception("switch feedback failed")

    def notify_user(self, title, message):
        """Wired to controller.on_user_alert, which fires on the event-tap and
        connection threads. AppKit is main-thread only, so the work hops
        there like every other controller callback in this class."""
        AppHelper.callAfter(self._notify_user_main, title, message)

    def _notify_user_main(self, title, message):
        if matches_translation(
                message, "Can't cross to {name}: it is being driven from {other}.",
                "Can't cross to {name}: another machine is driving it.",
                "Can't cross to {name}: it is driving {other}.",
                "Can't cross to {name}: it is driving another machine."):
            self._last_refusal = message
            refresh = getattr(self, "_refresh_tray_state", None)
            if refresh is not None:
                refresh()
            throttle = getattr(self, "_refusal_notices", None)
            if throttle is not None and not throttle.allow(message):
                return
        try:
            AppKit.NSBeep()
        except Exception:
            self.logger.exception("failed to beep for a user alert")
        try:
            self.notices.post(title, pages.redact(message, self.controller.cfg.hide_addresses))
        except Exception:
            self.logger.exception("failed to show a user notification")

    def _drive_status(self):
        if self.controller.redirecting:
            return tr("Driving {name}").format(name=self.controller.on_label or tr("another machine"))
        if self.controller.driver is not None:
            return tr("Driven by {name}").format(name=self.controller.driver_label or tr("another machine"))
        return tr("Idle")

    def _refresh_tray_state(self):
        state = self._drive_status()
        previous = getattr(self, "_last_drive_status", None)
        if previous is not None and previous != state:
            self._last_refusal = ""
            self._refusal_notices.reset()
        self._last_drive_status = state
        tooltip = f"{state}\n{self._last_refusal}" if self._last_refusal else state
        if hasattr(self, "status_item"):
            _set_tray_title(self.status_item, state)
        item = getattr(self, "status_item_view", None)
        if item is not None:
            item.button().setToolTip_(pages.redact(tooltip, self.controller.cfg.hide_addresses))

    def toggle_redirect(self, _sender):
        self.control_window.toggleRedirect_(None)

    def quit_app(self, _sender=None):
        self.status_timer.stop()
        self.update_checker.stop()
        self.pairing.stop()
        self.windows_input.stop()
        self.controller.stop()
        rumps.quit_application()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Beamer macOS sender")
    parser.add_argument(
        "--config",
        default=None,
        help="settings path; defaults to ~/Library/Application Support/Beamer/settings.json",
    )
    parser.add_argument("--hidden", action="store_true", help="start in the menu bar without opening the window")
    return parser.parse_args(argv)


def stay_awake(process_info=None):
    """An activity that keeps macOS from napping Beamer or coalescing its timers, which made its
    links answer seconds late and drop (test_app_nap.py); idle system sleep is still allowed. The
    token returned must be kept for as long as Beamer runs."""
    process_info = process_info or AppKit.NSProcessInfo.processInfo()
    return process_info.beginActivityWithOptions_reason_(
        AppKit.NSActivityUserInitiatedAllowingIdleSystemSleep | AppKit.NSActivityLatencyCritical,
        "Beamer's links answer the other machines within two seconds",
    )


def main(argv=None):
    args = parse_args(argv)
    if sys.platform != "darwin":
        raise SystemExit("Beamer must run on macOS")
    instance_lock = acquire_instance_lock()
    if instance_lock is None:
        request_running_instance(instance_socket_path())
        return
    AppKit.NSApplication.sharedApplication()
    theme.init_fonts()
    logger = configure_logging()
    crash_log.install(LOG_DIRECTORY, logger)
    # Which faces actually carried the window: the only way to tell the bundled copies failed to
    # register is to say what was used instead.
    logger.info("type: %s / %s", theme.sans(), theme.mono())
    settings_store = SettingsStore(args.config)
    try:
        cfg = settings_store.load()
    except SettingsError as exc:
        if Path(settings_store.path).exists():
            logger.warning("settings not loaded: %s", exc)
        else:
            logger.info("first run: no settings yet, starting from the defaults")
        cfg = editable_default_config()
    install(cfg.language)
    book, identity = bridge.links_from_store(settings_store, VERSION)
    controller = WakingController(cfg, logger=logger, book=book, identity=identity, hardware=hardware_mac.hardware_address_towards)
    app = TrayApp(controller, settings_store, logger, hidden=args.hidden)
    instance_server = InstanceSignalServer(
        instance_socket_path(), lambda: AppHelper.callAfter(app.open_window, None)
    )
    instance_thread = threading.Thread(target=instance_server.serve_forever, daemon=True)
    instance_thread.start()
    awake = stay_awake()  # noqa: F841  (held until Beamer quits)
    controller.start()
    try:
        app.run()
    finally:
        instance_server.close()
        instance_thread.join(timeout=1)
        controller.stop()
        instance_lock.close()


if __name__ == "__main__":
    main()
