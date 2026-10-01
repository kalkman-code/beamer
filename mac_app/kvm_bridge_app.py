import argparse
import collections
import fcntl
import logging
import logging.handlers
import os
import re
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
import config as config_module
import crossing
from core import return_edge
import desktop_mac
from core import effects
import effects_overlay
import gestures
import hardware_mac
import ignored_titles
import keyboard_layout
import notch_beam
import previews
import diagram
import motion
from notch_beam import NotchBeam
from notch_island import NotchIsland
from bridge import _GestureEventView
from key_codes import KEY_NAME_TO_CODE
import link_state
import machines_panel
import login_item
import pages
from core import updates
from core import protocol
from core import pairing
from core import peerlist
from core import settings_sync
from settings_store import (
    SettingsError,
    SettingsStore,
    config_to_raw,
    editable_default_config,
)
import theme
from wake import WakingController, lookup_mac
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
    "local": "Input on this Mac",
    "held": "Input held on this Mac; crossing is off",
    "windows": "Input on another machine",
}


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
    image.setAccessibilityDescription_(MENU_BAR_STATES[state])
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
        panel.setBackgroundColor_(AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.02))
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
    name = app.localizedName() or "An app"
    if _ax_full_screen(app.processIdentifier()):
        return name
    error, displays, count = Quartz.CGGetActiveDisplayList(16, None, None)
    if error != 0:
        raise RuntimeError(f"CGGetActiveDisplayList failed: {error}")
    display_frames = []
    for display in list(displays)[:count]:
        rect = Quartz.CGDisplayBounds(display)
        display_frames.append((rect.origin.x, rect.origin.y, rect.size.width, rect.size.height))
    windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
    if _covers_a_display(windows or (), app.processIdentifier(), display_frames):
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
    then redraws at 30Hz until both the pressure and the flash have faded, since nothing
    arrives from the tap once the push stops. Any failure disables the glow for the run."""

    dark_appearance = staticmethod(notch_beam.dark_appearance)

    BAND_MAX = 18.0
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
        self.timer = rumps.Timer(self._tick, 1 / 30)

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
        pace = effects.pace(feel.get("effect_length"))
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
        band = 3.0 + 3.0 * strength if beam else 2.0 + self.BAND_MAX * strength
        if self._corner() is not None:
            self._draw_corner(feel, beam, band, level, flash, strength)
            return
        if self.side is not None:
            self.side.setHidden_(True)
        if self.mac_edge == "top":
            x, y, width, height = self.region
            band = self._top_band(band, beam, effects_overlay.display_at(
                (x + width / 2.0, y + height / 2.0), self.controller._current_desktop_bounds()))
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
            elif fade is not None:
                self.comet.setLocations_([0.0, fade, 1.0 - fade, 1.0])
                self.comet.setColors_([AppKit.NSColor.colorWithWhite_alpha_(1.0, a).CGColor() for a in (0.0, 1.0, 1.0, 0.0)])
                self.fill.setMask_(self.comet)
                self.fill.setOpacity_(min(1.0, 0.3 + 0.7 * level + 0.6 * flash))
            else:
                self.fill.setMask_(None)
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

    def _top_band(self, band, beam, display):
        """How deep the glow reaches down from the top of `display`. Over a menu bar it is scaled
        to fill the bar at full strength: the bar is about twice the band, so at the band's own
        depth it lit only the bar's top half, under the status items and the clock, and a corner's
        top arm read as a faint stripe beside the full side arm. The beam is a line and stays one."""
        bar = menu_bar_height(display)
        full = 2.0 + self.BAND_MAX
        if beam or bar <= full:
            return band
        return band * bar / full

    def _draw_corner(self, feel, beam, band, level, flash, strength):
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
        across = self._top_band(band, beam, (left, top, right, bottom)) if at_top else band
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
            for layer, mask, frame, (start, end), path in walls:
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
    item(application_menu, "About Beamer", "showAbout:", "", target=control_window)
    application_menu.addItem_(AppKit.NSMenuItem.separatorItem())
    item(application_menu, "Hide Beamer", "hide:", "h")
    item(
        application_menu,
        "Hide Others",
        "hideOtherApplications:",
        "h",
        AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagOption,
    )
    application_menu.addItem_(AppKit.NSMenuItem.separatorItem())
    item(application_menu, "Quit Beamer", "quitApp:", "q", target=control_window)
    main_menu.setSubmenu_forItem_(application_menu, application_item)

    edit_item = main_menu.addItemWithTitle_action_keyEquivalent_("Edit", None, "")
    edit_menu = AppKit.NSMenu.alloc().initWithTitle_("Edit")
    item(edit_menu, "Undo", "undo:", "z")
    item(
        edit_menu,
        "Redo",
        "redo:",
        "z",
        AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagShift,
    )
    edit_menu.addItem_(AppKit.NSMenuItem.separatorItem())
    item(edit_menu, "Cut", "cut:", "x")
    item(edit_menu, "Copy", "copy:", "c")
    item(edit_menu, "Paste", "paste:", "v")
    item(edit_menu, "Select All", "selectAll:", "a")
    main_menu.setSubmenu_forItem_(edit_menu, edit_item)

    view_item = main_menu.addItemWithTitle_action_keyEquivalent_("View", None, "")
    view_menu = AppKit.NSMenu.alloc().initWithTitle_("View")
    for index, page in enumerate(pages.PAGES):
        item(view_menu, page[1], "showPage:", str(index + 1), target=control_window).setTag_(index)
    main_menu.setSubmenu_forItem_(view_menu, view_item)

    window_item = main_menu.addItemWithTitle_action_keyEquivalent_("Window", None, "")
    window_menu = AppKit.NSMenu.alloc().initWithTitle_("Window")
    # No target, so these travel the responder chain to whichever window is key rather than
    # being bound to one. Closing is safe to offer because the control window is created with
    # setReleasedWhenClosed_(False) and the menu-bar item reopens it.
    item(window_menu, "Close", "performClose:", "w")
    item(window_menu, "Minimise", "performMiniaturize:", "m")
    main_menu.setSubmenu_forItem_(window_menu, window_item)
    AppKit.NSApp.setWindowsMenu_(window_menu)

    AppKit.NSApp.setMainMenu_(main_menu)


class FlippedView(AppKit.NSView):
    """A plain view that lays out top-down, so the scrolling page starts at the top of the window
    rather than the bottom of the document."""

    def isFlipped(self):
        return True


class ControlWindow(AppKit.NSObject):
    def initWithController_settingsStore_logger_(self, controller, settings_store, logger):
        self = objc.super(ControlWindow, self).init()
        if self is None:
            return None
        self.controller = controller
        self.settings_store = settings_store
        self.logger = logger
        # Before anything is built: every control takes its colours from the palette in use.
        theme.set_dark(theme.wants_dark(controller.cfg.appearance, theme.system_dark()))
        # Set by TrayApp once it exists: the listener for the PC's input, and
        # the second way an arrangement changed here can reach the PC.
        self.windows_input = None
        # Set by TrayApp: saves one machine's direction switch (token, send= or allow_drive=).
        self.direction_handler = None
        self.last_capture_attempt = 0.0
        # macOS passes keys only to a process started after Input Monitoring is granted, so a grant
        # made during this run needs a relaunch before the keyboard crosses, however ready the tap looks.
        self.granted_at_launch = accessibility_granted() and input_monitoring_granted()
        self.update_checker = None
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
        self.page = None
        self.opened = False
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
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
        self.window.setDelegate_(self)
        self.window.setContentMinSize_(AppKit.NSMakeSize(*theme.MIN_WINDOW))
        # The window always carries the palette's own appearance, never nil: the title bar's
        # buttons and text then match the palette whatever the Mac is set to, and the transparent
        # title bar takes the window's ground.
        self.window.setAppearance_(theme.appearance_named(theme.is_dark()))
        self.window.setBackgroundColor_(theme.colour("ground"))
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.center()
        content = self.window.contentView()
        content.setWantsLayer_(True)
        theme.tint(content.layer(), background="ground")
        self.appearance_watch = theme.AppearanceWatch.alloc().initWithCallback_(lambda: self._apply_appearance())

        top = widgets.hairline()
        self.sidebar = widgets.Sidebar(
            pages.PAGES,
            self._select_page,
            # Words rather than the address, which wraps mid-path at the sidebar's narrowest; the
            # address is in the tooltip.
            footer_text=f"Beamer {VERSION}\nBeamer's website",
            footer_label="Open Beamer's website, kalkmancode.co.uk/beamer",
            on_footer=self._open_beamer_site,
        )
        divider = widgets.box("rule")
        pane = widgets.stack(spacing=0)
        for view in (top, self.sidebar.view, divider, pane):
            content.addSubview_(view)
        self.sidebar_width = self.sidebar.view.widthAnchor().constraintEqualToConstant_(theme.SIDEBAR_WIDTH[1])
        AppKit.NSLayoutConstraint.activateConstraints_([
            top.topAnchor().constraintEqualToAnchor_(content.topAnchor()),
            top.leadingAnchor().constraintEqualToAnchor_(content.leadingAnchor()),
            top.trailingAnchor().constraintEqualToAnchor_(content.trailingAnchor()),
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
        # The page host is the only part of the pane that gives way when the window is resized;
        # the commit bar keeps its height on every page.
        host = widgets.box()
        host.setContentHuggingPriority_forOrientation_(
            AppKit.NSLayoutPriorityDefaultLow, AppKit.NSLayoutConstraintOrientationVertical
        )
        host.setContentCompressionResistancePriority_forOrientation_(
            AppKit.NSLayoutPriorityDefaultLow, AppKit.NSLayoutConstraintOrientationVertical
        )
        widgets.add(pane, host)
        widgets.add(pane, widgets.hairline())
        widgets.add(pane, self._commit())

        self.page_titles = []
        # Same on both machines: each shared page's scope line, and the notes on this Mac's own rows.
        self.scope_labels = {}
        self.own_notes = []
        self.page_paddings = []
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
            scroll, body = self._page(name, purpose, pages.SCOPE.get(key), key)
            host.addSubview_(scroll)
            widgets.pin(scroll, host)
            scroll.setHidden_(True)
            self.pages[key] = scroll
            builders[key](body)

        self._load(config_to_raw(controller.cfg))
        self._select_page(pages.opening_page(None, accessibility_granted(), input_monitoring_granted()))
        self.windowDidResize_(None)
        self.refresh()
        return self

    def windowDidResize_(self, _notification):
        width = self.window.contentView().frame().size.width
        low, high = theme.SIDEBAR_WIDTH
        sidebar = min(high, max(low, width * 0.25))
        self.sidebar_width.setConstant_(sidebar)
        self._apply_width(width - sidebar - 1)

    @objc.python_method
    def _apply_width(self, width):
        """Two layouts, not a continuous reflow. Wide, Pairing puts the machine list beside the code;
        narrow, it stacks and the large figures step down a size."""
        wide = width >= WIDE_WIDTH
        if wide == self.wide:
            return
        self.wide = wide
        narrow = not wide
        top, leading, bottom, trailing = theme.PAGE_PADDING_NARROW if narrow else theme.PAGE_PADDING
        for constraints in self.page_paddings:
            for constraint, constant in zip(constraints, (top, leading, bottom, trailing)):
                constraint.setConstant_(constant)
        for title in self.page_titles:
            title.set(size=theme.PAGE_TITLE_NARROW if narrow else theme.PAGE_TITLE)
        self.state_word.set(size=theme.TYPE["status_word_narrow" if narrow else "status_word"])
        for readout in (self.round_trip, self.peer):
            readout.set_narrow(narrow)
        numeral = theme.TYPE["numeral_narrow" if narrow else "numeral"]
        self.resistance_numeral.set(size=numeral)
        self.double_tap_numeral.set(size=numeral)
        # Stacked, each half takes the full width; side by side, FillEqually shares it. The
        # distribution runs along the orientation, so stacked it must go back to Fill.
        self._show_peer()
        panel = self.panel
        panel.wide = wide
        AppKit.NSLayoutConstraint.deactivateConstraints_(panel.pair_stacked)
        panel.pair_grid.setOrientation_(
            AppKit.NSUserInterfaceLayoutOrientationHorizontal if wide else AppKit.NSUserInterfaceLayoutOrientationVertical
        )
        panel.pair_grid.setDistribution_(
            AppKit.NSStackViewDistributionFillEqually if wide else AppKit.NSStackViewDistributionFill
        )
        if narrow:
            AppKit.NSLayoutConstraint.activateConstraints_(panel.pair_stacked)

    @objc.python_method
    def _select_page(self, key):
        self.page = key
        for name, scroll in self.pages.items():
            scroll.setHidden_(name != key)
        self.sidebar.select(key)
        self._say(pages.footer(key))
        # Both recorders listen application-wide; left armed, they would take the first key typed on
        # another page.
        if key != "crossing":
            self.key_recorder.cancel()
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
        self.appearance_select.value = raw["appearance"]
        self._apply_appearance(raw["appearance"])
        self.updates_switch.value = raw["check_updates"]
        self.modifier_select.value = raw["key_map"] if isinstance(raw["key_map"], str) else "custom"
        crossing_raw = raw["crossing"]
        self.edge_select.value = crossing_raw["edge"]
        self.haptics_box.value = crossing_raw["haptics"]
        self.glow_box.value = crossing_raw["glow"]
        self.notch_style_select.value = crossing_raw["notch_style"]
        self.notch_after_select.value = crossing_raw["notch_after_ms"]
        self.tick_steps_select.value = crossing_raw["haptic_steps"]
        self.hold_box.value = crossing_raw["hold_full_screen"]
        self._load_shared(raw)

    @objc.python_method
    def _load_shared(self, raw):
        """The controls Same on both machines keeps in step, and nothing else: settings arriving
        from the PC must not reset a half-typed address or the pairing card."""
        self.key_recorder.set_value(raw["trigger_key"])
        self.style_select.value = raw["trigger_style"]
        self.double_tap_ruler.value = raw["double_tap_ms"]
        self.double_tap_numeral.set(str(raw["double_tap_ms"]))
        crossing_raw = raw["crossing"]
        for name, tile in self.method_boxes.items():
            tile.value = name in crossing_raw["methods"]
        for name, tile in self.part_boxes.items():
            tile.value = name in crossing_raw["edge_parts"]
        self.corner_select.value = crossing_raw["corner"]
        self.resistance_ruler.value = crossing_raw["resistance_px"]
        self.landing_box.value = crossing_raw["shortcut_arrival"]
        self.switch_style_select.value = crossing_raw["shortcut_arrival_style"]
        self.dragging_box.value = crossing_raw["block_while_dragging"]
        self.glow_style_select.value = crossing_raw["glow_style"]
        self.length_select.value = crossing_raw.get("effect_length", "normal")
        self.glow_colour_select.value = crossing_raw["glow_colour"]
        self._reflect()

    @objc.python_method
    def _reflect(self, *_ignored):
        """Enables each control only when the setting it edits is in play, and keeps the figures
        beside the rulers in step with them."""
        methods = [name for name, tile in self.method_boxes.items() if tile.value]
        rows = pages.crossing_rows(methods)
        for key, view in (("edge", self.edge_row), ("parts", self.parts_row), ("corner", self.corner_row),
                          ("dragging", self.dragging_box.view), ("resistance", self.resistance_module.view),
                          ("shortcut", self.shortcut_module.view)):
            motion.set_hidden(view, not rows[key])
        if not rows["shortcut"]:
            # Hidden, an armed recorder would still take the next key typed anywhere on the page.
            self.key_recorder.cancel()
        for name, label in pages.part_names(self.edge_select.value).items():
            self.part_boxes[name].name.set(label)
        notch = self.method_boxes["notch"]
        notch.set_enabled(self.has_notch)
        self._show_arrangement(methods)
        motion.set_hidden(self.edge_note.view, not pages.notch_or_corner_only(methods))
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
            prefix = "Same as crossing: " if choice == "match" else ""
            self.effect_name.set(f"{prefix}{fx.name}")
        else:
            fx = effects.preview_effect(style) if self.effects_ready else effects.CLASSIC.get(style)
            if fx is not None:
                self.effect_name.set(f"{fx.name}, {pages.INTENSITIES.get(fx.intensity, fx.intensity).lower()}")
        motion.set_hidden(self.chosen_box, fx is None)
        if fx is not None:
            self.effect_blurb.set(fx.blurb)
        # Drawing every still is tens of milliseconds, so only what they show redraws them: the
        # colour, the place, the length, the palette, and for Same as crossing the crossing style.
        stills = (colour, style, place, self.length_select.value, theme.is_dark())
        if stills != self._stills:
            self._stills = stills
            for still in self.effect_stills:
                still.setNeedsDisplay_(True)
            self.previews.repaint()
        self.tick_note.set(
            {
                "quarters": "A tick at a quarter, half and three quarters of the push, then a double tick as the pointer goes through.",
                "halves": "One tick halfway through the push, then a double tick as the pointer goes through.",
                "breakthrough": "Nothing on the way in, then a double tick as the pointer goes through.",
            }.get(self.tick_steps_select.value, "")
            + " macOS plays them only while a finger is on the trackpad, so press and hold Try it."
            if haptics
            else "Off: the trackpad stays still while you push."
        )
        after = (self.notch_after_select.value or 1200) / 1000
        self.notch_note.set(
            f"Once the pointer is through to Windows the notch keeps playing for {after:g} seconds, so the "
            "animation finishes instead of cutting off."
        )
        self._run_previews()
        notch_range = self.controller.notch_range
        notch.set_detail(
            f"Top edge, {round(notch_range[1] - notch_range[0])} pt wide" if self.has_notch and notch_range else "This Mac has no notch"
        )
        self.method_boxes["shortcut"].set_detail(widgets.key_title(self.key_recorder.value))
        self.ignored_recorder.set_trigger_code(KEY_NAME_TO_CODE.get(self.key_recorder.value))
        hold = self.style_select.value == "hold"
        motion.set_hidden(self.double_tap_head, hold)
        self.style_hint.set(
            "Input is on Windows for as long as the key is held."
            if hold
            else "Tap twice to switch; tap twice again to come back."
        )
        resistance = self.resistance_ruler.value
        self.resistance_numeral.set(str(resistance))
        self.push_strip.show(resistance / 500.0, link_state.peer_name(self.controller.cfg))
        self.resistance_hint.set(
            "Switches the moment the pointer touches the edge."
            if resistance == 0
            else "How far to push past the edge before it gives. It applies as you drag, and lights the "
            f"{self.edge_select.value} edge of this screen so you can feel the size of it."
        )
        self.modifier_note.set({
            "semantic": "Command arrives on Windows as Control, so Command-C copies there too, and Control arrives as the Windows key.",
            "positional": "Each key arrives as the key in its place, so Command arrives as the Windows key.",
        }.get(self.modifier_select.value, "Custom: the key map in settings.json is kept as it is."))

    @objc.python_method
    def _show_arrangement(self, methods):
        cfg = self.controller.cfg
        key = widgets.key_title(self.key_recorder.value)
        held = self.style_select.value == "hold"
        side = self.edge_select.value or "right"
        pc = link_state.peer_name(cfg)
        where = {"left": "to the left of", "right": "to the right of", "top": "above", "bottom": "below"}[side]
        sentence = self._ways_in_sentence()
        self.ways_note.set(sentence + "." if sentence else "No way in is switched on. Choose one below.")
        self.arrangement_diagram.show(
            side, methods, [name for name, tile in self.part_boxes.items() if tile.value],
            self.corner_select.value or "top_right", widgets.key_cap(self.key_recorder.value), pc,
            self.has_notch, f"{pc} is {where} this Mac. {sentence + '.' if sentence else 'No way in is on.'}",
            key_how="hold" if held else "double-tap",
        )

    @objc.python_method
    def _preview_resistance(self, value):
        # Applied to the live engine at once, so the edge can be felt mid-drag; the saved setting
        # follows a moment later, once the ruler stops moving.
        self.controller.crossing.resistance_px = float(value)
        self._changed()
        if self.preview is not None and self.glow_box.value:
            self.preview(self.edge_select.value, min(1.0, value / 500.0))

    @objc.python_method
    def _show_peer(self):
        """The machine input is on, else the first one paired: its name and address."""
        entry = self._focus_entry()
        if entry is None:
            self.peer.value.set("—")
            self.peer_footer.set("Not paired")
            return
        self.peer.value.set(self._shown(self._machine_label()))
        host, port = entry.get("host"), entry.get("port")
        if not host:
            self.peer_footer.set("No address")
        else:
            # The port does not fit a third of the status module at the narrow grid.
            self.peer_footer.set(self._shown(f"{host}:{port}" if self.wide else host))

    @objc.python_method
    def _first_label(self):
        return self.controller.peer_label or "the other machine"

    @objc.python_method
    def _focus_entry(self):
        peers = self.controller.book.peers()
        on = self.controller.owner.on
        return next((peer for peer in peers if on is not None and peer.get("id") == on), peers[0] if peers else None)

    @objc.python_method
    def _machine_label(self):
        """What the window calls the machine its status is about: where input is, else the first."""
        controller = self.controller
        return controller.on_label or controller.peer_label or link_state.peer_name(controller.cfg)

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
        first = peers[0]["token"] if peers else ""
        if first != (self.controller.cfg.auth_token or ""):
            self.peers_changed()

    @objc.python_method
    def peers_changed(self):
        """A machine was paired or removed: the controller reads the settings again (without
        `update_config`, which would bring input home), the links follow the peers, and the
        address fields show the first machine."""
        try:
            cfg = self.settings_store.load()
        except SettingsError as exc:
            self.logger.warning("settings not read after the peers changed: %s", exc)
            return
        # The links first: apply_settings tells the primary link where this Mac's edge is, and that
        # must be the new first machine's link, not one that is about to be removed.
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
        frame, start, end = frames.get(self.edge_select.value, frames["right"])
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
        self.controller.cfg.crossing["hold_full_screen"] = value
        self._changed()

    @objc.python_method
    def _changed(self, *_ignored):
        """Every control outside the Connection page calls this. The settings are written and
        applied once the control has been still for a moment, so a ruler being dragged writes once
        at the end rather than on every step."""
        self._reflect()
        self._apply_serial += 1
        serial = self._apply_serial
        AppHelper.callLater(0.3, lambda: serial == self._apply_serial and self._apply_settings())

    def windowWillClose_(self, _notification):
        self.key_recorder.cancel()
        self.ignored_recorder.cancel()
        # A code that stays up with nobody watching would still accept a pairing.
        self.panel.stop_showing()
        if self.previews is not None:
            self.tile_hover.stop()
            self.previews.stop()

    @objc.python_method
    def _head(self, title, figure):
        line = widgets.stack(vertical=False, spacing=12)
        line.setAlignment_(AppKit.NSLayoutAttributeTop)
        line.addArrangedSubview_(widgets.hug(widgets.eyebrow(title), AppKit.NSLayoutPriorityDefaultLow))
        line.addArrangedSubview_(figure)
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
        scroll.setAutohidesScrollers_(True)
        page = FlippedView.alloc().init()
        page.setTranslatesAutoresizingMaskIntoConstraints_(False)
        body = widgets.stack(spacing=theme.MODULE_GAP)
        page.addSubview_(body)
        top, leading, bottom, trailing = theme.PAGE_PADDING
        padding = [
            body.topAnchor().constraintEqualToAnchor_constant_(page.topAnchor(), top),
            body.leadingAnchor().constraintEqualToAnchor_constant_(page.leadingAnchor(), leading),
            page.bottomAnchor().constraintGreaterThanOrEqualToAnchor_constant_(body.bottomAnchor(), bottom),
            page.trailingAnchor().constraintEqualToAnchor_constant_(body.trailingAnchor(), trailing),
        ]
        # The page ends where its body does, unless motion props it up with the floor while a
        # module folds away, so the scroll offset eases down rather than snapping. Only the
        # page is pulled short; pulling the body would stretch a module into the held space.
        page.floor = page.heightAnchor().constraintGreaterThanOrEqualToConstant_(0.0)
        page.floor.setActive_(True)
        shrink = page.heightAnchor().constraintEqualToConstant_(0.0)
        shrink.setPriority_(1)
        shrink.setActive_(True)
        AppKit.NSLayoutConstraint.activateConstraints_(padding)
        self.page_paddings.append(padding)
        scroll.setDocumentView_(page)
        clip = scroll.contentView()
        AppKit.NSLayoutConstraint.activateConstraints_([
            page.topAnchor().constraintEqualToAnchor_(clip.topAnchor()),
            page.leadingAnchor().constraintEqualToAnchor_(clip.leadingAnchor()),
            page.widthAnchor().constraintEqualToAnchor_(clip.widthAnchor()),
        ])
        header = widgets.stack(spacing=6)
        heading = widgets.Label(title, theme.PAGE_TITLE, 700, tracking=-0.01)
        self.page_titles.append(heading)
        widgets.add(header, heading.view)
        purpose_label = widgets.Label(purpose, theme.TYPE["body"], ink="ink_2", wrap=True)
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
        # The machines and the pairing sheet lead until there is one, since nothing else here works
        # without it; once paired they sit under the controls (see _place_machines).
        self.overview_body = body
        widgets.add(body, self._status_module().view)
        widgets.add(body, self._keyboard_module().view)
        widgets.add(body, self.panel.machines_module.view)
        widgets.add(body, self.panel.sheet.view)
        widgets.add(body, self._same_module().view)
        widgets.add(body, self._login_module().view)
        widgets.add(body, self._updates_module().view)

    @objc.python_method
    def _place_machines(self, first):
        """The machines list and the pairing sheet at the top of Overview while nothing is paired,
        under Keyboard and pointer once something is."""
        if first == self._machines_first:
            return
        self._machines_first = first
        body = self.overview_body
        views = (self.panel.machines_module.view, self.panel.sheet.view)
        for view in views:
            body.removeArrangedSubview_(view)
        # Under the page's title block, which is the first view; after Keyboard and pointer otherwise.
        at = 1 if first else 3
        for offset, view in enumerate(views):
            body.insertArrangedSubview_atIndex_(view, at + offset)

    @objc.python_method
    def _same_module(self):
        module = widgets.Module(spacing=10)
        module.add(widgets.eyebrow("Settings"))
        self.same_switch = widgets.Switch("Same on both machines", on_change=self._set_same)
        module.add(self.same_switch.view)
        self.same_note = widgets.note()
        module.add(self.same_note.view)
        return module

    @objc.python_method
    def _own_note(self, text=pages.OWN_ROW):
        """A note on a row this Mac keeps to itself, shown only while the pages are kept in step."""
        note = widgets.note(text)
        note.view.setHidden_(True)
        self.own_notes.append(note)
        return note.view

    @objc.python_method
    def _peer_too_old(self):
        """Whether a link is up to a PC whose Beamer does not keep settings in step."""
        links = [(self.controller.connected, getattr(self.controller, "peer_settings", None))]
        if self.windows_input is not None:
            links.append((self.windows_input.state == ServerState.CONNECTED, self.windows_input.peer_settings))
        return any(up and known is False for up, known in links)

    @objc.python_method
    def _show_same(self):
        cfg = self.controller.cfg
        old = self._peer_too_old()
        on = cfg.same_on_both and not old
        if self.same_switch.value != on:
            self.same_switch.value = on
        self.same_switch.set_enabled(not old)
        who = settings_sync.who(list(peerlist.labels(self.controller.book.peers()).values()))
        self.same_note.set(settings_sync.switch_note(who, cfg.same_on_both, old), ink="amber" if old else "ink_2")
        for key, label in self.scope_labels.items():
            text = settings_sync.scope(key, who, on, pages.SCOPE.get(key))
            if label.text != text:
                label.set(text)
        for note in self.own_notes:
            if note.view.isHidden() == on:
                motion.set_hidden(note.view, not on)

    @objc.python_method
    def _set_same(self, on):
        """Turning it on carries this Mac's Crossing and Design across; off, each end keeps what it
        has. Either way the PC follows, now if a link is up, else when one next comes up."""
        raw = config_to_raw(self.controller.cfg)
        raw["same_on_both"] = bool(on)
        raw["same_set_at"] = settings_sync.next_stamp(raw["same_set_at"], time.time())
        raw["same_by"] = self.own_id()
        try:
            cfg = self.settings_store.save(raw)
        except SettingsError as exc:
            self.logger.warning("Same on both machines not saved: %s", exc)
            return
        self.controller.apply_settings(cfg)
        self._send_same()
        self._show_same()

    @objc.python_method
    def same_state(self):
        """This Mac's settings message, for a change here and for announcing on a new link. Called
        from the links' threads as well, so it reads the settings once."""
        cfg = self.controller.cfg
        return settings_sync.message_data(cfg.same_on_both, cfg.same_set_at,
                                          settings_sync.mac_values(config_to_raw(cfg)), by=cfg.same_by or self.own_id())

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
        cfg = self.controller.cfg
        taken = settings_sync.arrived(data, cfg.same_set_at, KEY_NAME_TO_CODE, by_here=cfg.same_by)
        if taken is None:
            return
        on, set_at, values = taken
        raw = config_to_raw(cfg)
        raw["same_on_both"] = on
        raw["same_set_at"] = set_at
        raw["same_by"] = data.get("by", "")
        if on:
            raw = settings_sync.apply_mac(raw, values)
        try:
            cfg = self.settings_store.save(raw)
        except (SettingsError, TypeError, ValueError):
            self.logger.exception("could not save the settings the PC sent")
            return
        self.controller.apply_settings(cfg)
        self._send_same(data, source=peer)
        if self.previews is not None:
            self.previews.repaint()
        self.logger.info("settings from the PC applied (same on both machines %s)", "on" if on else "off")
        self._load_shared(config_to_raw(cfg))
        self.refresh()

    @objc.python_method
    def _login_module(self):
        module = widgets.Module(spacing=10)
        module.add(widgets.eyebrow("At login"))
        self.login_switch = widgets.Switch("Start Beamer when you log in", on_change=self._set_login)
        module.add(self.login_switch.view)
        self.login_note = widgets.note()
        module.add(self.login_note.view)
        self._show_login_state()
        return module

    @objc.python_method
    def _updates_module(self):
        module = widgets.Module(spacing=10)
        module.add(widgets.eyebrow("Updates"))
        self.updates_switch = widgets.Switch("Check for updates", on_change=self._set_check_updates)
        self.updates_switch.value = self.controller.cfg.check_updates
        module.add(self.updates_switch.view)
        module.add(widgets.note(
            "Asks GitHub once a day whether there is a newer Beamer. Nothing is sent but the request itself."
        ).view)
        self.update_button = widgets.Button("Download", self, "downloadUpdate:", style="primary", full_width=True)
        self.update_button.view.setHidden_(True)
        module.add(self.update_button.view)
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
            self.show_update(None)

    @objc.python_method
    def show_update(self, found):
        """`found` is (version, url) for a newer release, or None."""
        self.update_url = found[1] if found else None
        if found:
            self.update_button.set_title(f"Download Beamer {found[0]}")
        motion.set_hidden(self.update_button.view, found is None)

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
        if refused:
            self.login_note.set(f"macOS refused: {refused}")
        elif state == login_item.REQUIRES_APPROVAL:
            self.login_note.set("Waiting for you to allow it in System Settings, General, Login Items.")
        else:
            self.login_note.set("In the menu bar, with no window.")

    @objc.python_method
    def _set_login(self, enabled):
        try:
            login_item.set_enabled(enabled)
        except OSError as exc:
            self.logger.warning("could not change the login item: %s", exc)
            self._show_login_state(refused=exc)
            return
        self._show_login_state()

    @objc.python_method
    def _crossing_page(self, body):
        # The shortcut's module is built first: the ways' tiles name its key.
        self.shortcut_module = self._shortcut_module()
        self.resistance_module = self._resistance_module()
        for module in (self._ways_module(), self.resistance_module, self.shortcut_module):
            widgets.add(body, module.view)

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

    @objc.python_method
    def _appearance_module(self):
        """The settings window's own palette. Last on the page: it is chosen once, and the modules
        above are what the page is for."""
        module = widgets.Module()
        module.add(widgets.eyebrow("Appearance"))
        module.add(self._own_note())
        self.appearance_select = widgets.Segmented(
            [("system", "System"), ("light", "Light"), ("dark", "Dark")], on_change=self._appearance_chosen
        )
        module.add(widgets.field_row("This window", self.appearance_select.view)[0])
        module.add(widgets.note("System follows your Mac's light or dark setting.").view)
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
        Quartz.CATransaction.commit()
        if self.previews is not None:
            # The renderers take the palette as they draw, which a held still does not do again.
            self.previews.repaint()

    @objc.python_method
    def _keyboard_page(self, body):
        # Modifier keys first: chosen once, while the list below grows as keys are added.
        widgets.add(body, self._modifier_module().view)
        widgets.add(body, self._ignored_module().view)
        widgets.add(body, self._speed_module().view)

    @objc.python_method
    def _speed_module(self):
        """How the PC's mouse feels on this Mac: the PC sends what its own acceleration made of the
        hand's movement, and this Mac's settings decide the rest."""
        module = widgets.Module()
        figure, self.pointer_numeral = self._numeral("%")
        module.add(self._head("The PC's pointer here", figure))
        self.pointer_ruler = widgets.Ruler(
            25, 400, (25, 100, 200, 300, 400), step=5, minor=25, on_change=self._speed_moved,
            title="Pointer speed", arrow_step=25,
        )
        module.add(self.pointer_ruler.view)
        module.add(widgets.note("Pointer speed for the PC's mouse or trackpad while it drives this Mac.").view)
        figure, self.scroll_numeral = self._numeral("%")
        module.add(self._head("Scrolling", figure))
        self.scroll_ruler = widgets.Ruler(
            25, 400, (25, 100, 200, 300, 400), step=5, minor=25, on_change=self._speed_moved,
            title="Scroll speed", arrow_step=25,
        )
        module.add(self.scroll_ruler.view)
        self.reverse_scroll_box = widgets.Switch("Reverse the PC's scrolling", on_change=self._changed)
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
        module = widgets.Module(spacing=10)
        head = widgets.stack(vertical=False, spacing=7)
        head.addArrangedSubview_(widgets.hug(widgets.eyebrow("Link"), AppKit.NSLayoutPriorityDefaultLow))
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
        self.round_trip = widgets.Readout("Delay", "ms", self.spark)
        self.peer_footer = widgets.Label("", theme.TYPE["small"], mono=True, ink="ink_3")
        widgets.squeeze(self.peer_footer.view)
        self.peer = widgets.Readout("Machine", "", self.peer_footer.view, sizes=(theme.PEER_SIZE, theme.PEER_SIZE_NARROW))
        module.add(widgets.grid([self.round_trip.view, self.peer.view], 2))
        return module

    @objc.python_method
    def _keyboard_module(self):
        """Sending input by hand and pausing the edge, together as on the PC. Where input is now is
        the status word above; a MAC / WINDOWS indicator here only said it twice."""
        module = widgets.Module(spacing=12)
        module.add(widgets.eyebrow("Keyboard and pointer"))
        self.toggle_button = widgets.Button(
            "Send input", self, "toggleRedirect:", style="primary", scale="big", full_width=True
        )
        module.add(self.toggle_button.view)
        self.pause_row = widgets.stack(spacing=8)
        self.pause_button = widgets.Button("Pause crossing", self, "togglePause:", full_width=True)
        widgets.add(self.pause_row, self.pause_button.view)
        self.crossing_state = widgets.note()
        widgets.add(self.pause_row, self.crossing_state.view)
        module.add(self.pause_row)
        module.body.setCustomSpacing_afterView_(14, self.toggle_button.view)
        self.hold_box = widgets.Switch("Hold the edges while an app is full screen", on_change=self._hold_changed)
        module.add(self.hold_box.view)
        module.add(self._own_note())
        module.add(widgets.note(
            "Off, your pointer can leave a full-screen game or video, and the PC's pointer can come home "
            "through this Mac's edge. Useful if your keyboard has no key for the shortcut."
        ).view)
        return module

    @objc.python_method
    def _resistance_module(self):
        module = widgets.Module()
        figure, self.resistance_numeral = self._numeral("px")
        module.add(self._head("Resistance", figure))
        self.push_strip = diagram.PushStrip.alloc().init().setup()
        module.add(self.push_strip)
        self.resistance_ruler = widgets.Ruler(
            0, 500, (0, 100, 200, 300, 400, 500), step=1, minor=25, on_change=self._preview_resistance,
            title="Resistance", arrow_step=10,
        )
        module.add(self.resistance_ruler.view)
        self.resistance_hint = widgets.note()
        module.add(self.resistance_hint.view)
        return module

    @objc.python_method
    def _ways_module(self):
        module = widgets.Module()
        module.add(widgets.eyebrow("Ways in"))
        self.ways_note = widgets.note()
        module.add(self.ways_note.view)
        self.arrangement_diagram = diagram.ArrangementDiagram.alloc().init().setup()
        module.add(self.arrangement_diagram)
        module.body.setCustomSpacing_afterView_(16, self.arrangement_diagram)
        # The pointer's ways first, the shortcut on its own row above its settings.
        ways = (
            ("edge", "Edge", "One whole side"),
            ("part", "Part of the edge", "Only the thirds you pick"),
            ("corner", "Corner", "Push diagonally into a corner"),
            ("notch", "Notch", ""),
            ("shortcut", "Shortcut", ""),
        )
        self.method_boxes = {
            way: widgets.WayTile(title, detail, on_change=lambda on, way=way: self._way_toggled(way, on))
            for way, title, detail in ways
        }
        module.add(widgets.grid([tile.view for tile in self.method_boxes.values()], 2))
        module.add(self._own_note(pages.OWN_NOTCH))
        # The values are the side of this Mac the PC is on, which is also the edge that crosses.
        self.edge_select = widgets.Segmented(
            [("left", "Left"), ("right", "Right"), ("top", "Above"), ("bottom", "Below")], on_change=self._changed
        )
        self.edge_row = widgets.field_row("Where the PC is", self.edge_select.view)[0]
        module.add(self.edge_row)
        self.edge_note = widgets.note(pages.NOTCH_OR_CORNER_NOTE)
        self.edge_note.view.setHidden_(True)
        module.add(self.edge_note.view)
        self.part_boxes = {
            part: widgets.WayTile(part.capitalize(), on_change=lambda on, part=part: self._part_toggled(part, on))
            for part in return_edge.PARTS
        }
        self.parts_row = widgets.field_row("Parts", widgets.grid([tile.view for tile in self.part_boxes.values()], 3))[0]
        module.add(self.parts_row)
        self.corner_select = widgets.Segmented(
            [
                ("top_left", "Top left"),
                ("top_right", "Top right"),
                ("bottom_left", "Bottom left"),
                ("bottom_right", "Bottom right"),
            ],
            columns=2,
            on_change=self._changed,
        )
        self.corner_row = widgets.field_row("Corner", self.corner_select.view)[0]
        module.add(self.corner_row)
        self.dragging_box = widgets.Switch("Don't cross while dragging", on_change=self._changed)
        module.add(self.dragging_box.view)
        return module

    @objc.python_method
    def _way_toggled(self, way, on):
        methods = pages.toggle_way([name for name, tile in self.method_boxes.items() if tile.value], way, on)
        for name, tile in self.method_boxes.items():
            tile.value = name in methods
        self._changed()

    @objc.python_method
    def _part_toggled(self, part, on):
        # The tile has already flipped itself; the last-third rule needs the thirds from before.
        before = [name for name, tile in self.part_boxes.items() if (not on if name == part else tile.value)]
        parts = pages.toggle_part(before, part, on)
        for name, tile in self.part_boxes.items():
            tile.value = name in parts
        self._changed()

    @objc.python_method
    def _shortcut_module(self):
        module = widgets.Module()
        module.add(widgets.eyebrow("Shortcut"))
        self.key_recorder = widgets.KeyRecorder("alt_r", on_change=self._changed)
        module.add(self.key_recorder.view)
        self.style_select = widgets.Segmented([("double_tap", "Double-tap"), ("hold", "Hold")], on_change=self._changed)
        module.add(widgets.field_row("How you press it", self.style_select.view)[0])
        # The figure sits on the ruler's own line, so it reads as the ruler's value rather than as
        # a heading for the whole module, and the two hide together under Hold.
        self.double_tap_head = widgets.stack(spacing=6)
        caption = widgets.stack(vertical=False, spacing=12)
        caption.setAlignment_(AppKit.NSLayoutAttributeLastBaseline)
        caption.addArrangedSubview_(widgets.hug(widgets.label("Time between taps", theme.TYPE["note"], ink="ink_2"),
                                                AppKit.NSLayoutPriorityDefaultLow))
        figure, self.double_tap_numeral = self._numeral("ms")
        caption.addArrangedSubview_(figure)
        widgets.add(self.double_tap_head, caption)
        # The stored window runs 50 to 2000 ms, but only about 150 to 600 is useful, so the ruler
        # shows 50 to 1000 on a square-root scale; a stored value past 1000 pins the thumb.
        self.double_tap_ruler = widgets.Ruler(
            50, 1000, (50, 150, 300, 600, 1000), step=10, scale="sqrt", minor=20, on_change=self._double_tap_moved,
            title="Time between taps",
        )
        widgets.add(self.double_tap_head, self.double_tap_ruler.view)
        module.add(self.double_tap_head)
        self.style_hint = widgets.note()
        module.add(self.style_hint.view)
        return module

    @objc.python_method
    def _ignored_module(self):
        module = widgets.Module()
        module.add(widgets.eyebrow("Stays on this Mac"))
        module.add(widgets.note(
            "These keys and buttons keep working on this Mac while its input is on Windows: a "
            "mouse's back button for this Mac's browser, say, or a volume key for its speakers."
        ).view)
        self.ignored_entries = []
        self.ignored_list = widgets.stack(spacing=6)
        module.add(self.ignored_list)
        self.ignored_empty = widgets.note("Nothing yet. Every key and button goes to Windows while it has input.")
        module.add(self.ignored_empty.view)
        self.ignored_recorder = widgets.IgnoredRecorder(
            KEY_NAME_TO_CODE.get(self.key_recorder.value), on_recorded=self._add_ignored
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
            remove.setAccessibilityLabel_(f"Remove {title}")
            remove_word = widgets.Label("Remove", theme.TYPE["small"], ink="ink_2")
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
            self._ignored_said("That key is the shortcut; it always stays with Beamer.")
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
        module = widgets.Module(spacing=8)
        module.add(widgets.eyebrow("On screen"))
        self.glow_box = widgets.Switch("Animate crossings on this Mac", on_change=self._changed)
        module.add(self.glow_box.view)
        module.add(self._own_note())
        module.add(widgets.note(
            "Lights the edge, the corner or the notch as you push toward Windows. Switched off, crossing "
            "still works; you feel it rather than see it. Windows sets how its own edge looks."
        ).view)
        self.landing_box = widgets.Switch("Show where the pointer lands", on_change=self._changed)
        self.landing_row = widgets.stack(spacing=8)
        widgets.add(self.landing_row, self.landing_box.view)
        widgets.add(self.landing_row, widgets.note(
            "When the shortcut or the menu brings input to this Mac, an animation plays around the "
            "pointer. Choose it under Style, for Shortcut and menu."
        ).view)
        module.add(self.landing_row)
        return module

    @objc.python_method
    def _preview_tile(self, base, value, title, detail, notch, **overrides):
        """One tile's preview: its own screen, a feed pinned to the style the tile shows, and the
        real renderer hosted on it, registered with the page's loop."""
        screen = widgets.size(previews.PreviewScreen.alloc().init().setup(notch), height=84)
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
        widgets.add(row, widgets.label("Notch style", theme.TYPE["note"], 600, ink="ink_2"))
        notch = previews.notch_size()
        self.notch_style_select = widgets.ChoiceTiles(
            [
                self._preview_tile(NotchBeam, "beam", "Outline", "A line of colour runs round the notch, faster the harder you push.", notch),
                self._preview_tile(NotchIsland, "island", "Island", "The notch grows as you push, with a meter inside, and flashes as you go through.", notch),
            ],
            on_change=self._changed,
        )
        self._hover(self.notch_style_select)
        widgets.add(row, self.notch_style_select.view)
        self.notch_after_select = widgets.Segmented(
            [(600, "0.6 s"), (1200, "1.2 s"), (2000, "2 s"), (3000, "3 s")], on_change=self._changed
        )
        widgets.add(row, widgets.field_row("Keep animating", self.notch_after_select.view)[0])
        widgets.add(row, self._own_note())
        self.notch_note = widgets.note()
        widgets.add(row, self.notch_note.view)
        return row

    @objc.python_method
    def _edge_module(self):
        """Every style, grouped as effects.DIRECTIONS groups them: today's Glow and Beam first with
        stills like the rest, then each direction's quiet, medium and showpiece effect; every tile shows
        its style at the place chosen above them, and plays while the pointer is over it."""
        module = widgets.Module()
        module.add(widgets.eyebrow("Style"))
        self.style_for_select = widgets.Segmented(
            [("crossing", "Crossing"), ("switch", "Shortcut and menu")], on_change=self._changed
        )
        self.style_for_select.value = "crossing"
        self.style_for_row = widgets.field_row("Style for", self.style_for_select.view)[0]
        module.add(self.style_for_row)
        # Where every tile below plays its style: never switched off by the style, and what differs
        # there is said beneath it.
        self.effect_method_select = widgets.Segmented(previews.STAGE_METHODS, on_change=self._place_picked)
        self.effect_method_select.value = self.place = "edge"
        self.effect_method_view = widgets.field_row("Show at", self.effect_method_select.view)[0]
        module.add(self.effect_method_view)
        self.place_note = widgets.note()
        module.add(self.place_note.view)
        module.body.setCustomSpacing_afterView_(6, self.effect_method_view)
        self.notch_row = self._notch_row()
        module.add(self.notch_row)
        module.body.setCustomSpacing_afterView_(22, self.notch_row)
        colour = lambda: self.glow_colour_select.value or "signal"
        place = lambda: self.place
        pace = lambda: effects.pace(self.length_select.value)
        # The two sets of tiles, one per mode; only the chosen mode's shows.
        self.crossing_styles_box = widgets.stack(spacing=0)
        rows = []
        for group, choices in pages.style_groups(self.effects_ready):
            # Glow and Beam as stills of the same scene as the effects, so every tile reads alike.
            tiles = [(value, title, detail, previews.effect_still(value, colour, self.logger, place=place, pace=pace))
                     for value, title, detail in choices]
            self.effect_stills.extend(tile[3] for tile in tiles)
            if group == pages.TODAY:
                # At the notch Glow and Beam do not draw: the notch style plays instead, so there
                # their tiles show it, played by its own renderer as its tiles are.
                notch = previews.notch_size()
                stacked = []
                for value, title, detail, still in tiles:
                    screens = {style: self._preview_tile(base, style, "", "", notch)[3]
                               for style, base in (("beam", NotchBeam), ("island", NotchIsland))}
                    stack = previews.TileStack.alloc().init().setup([still, *screens.values()])
                    self.classic_pictures.append((stack, still, screens))
                    stacked.append((value, title, detail, stack))
                tiles = stacked
            rows.append(self._style_group(self.crossing_styles_box, group, tiles))
        module.add(self.crossing_styles_box)
        self.glow_style_select = widgets.Linked(rows, on_change=self._changed, host=self.crossing_styles_box)
        self.switch_styles_box = widgets.stack(spacing=0)
        rows = []
        for group, choices in pages.switch_style_groups(self.effects_ready):
            tiles = []
            for value, title, detail in choices:
                fx = lambda value=value: effects.switch_effect(value, self.glow_style_select.value or "glow")
                tiles.append((value, title, detail, previews.switch_still(fx, colour, self.logger, pace=pace)))
            self.effect_stills.extend(tile[3] for tile in tiles)
            rows.append(self._style_group(self.switch_styles_box, group, tiles))
        widgets.add(self.switch_styles_box, widgets.note(
            "What plays around the pointer when the shortcut or the menu brings input to this Mac. "
            "Same as crossing follows the style you chose for crossing, with a ring for Glow and Beam."
        ).view)
        module.add(self.switch_styles_box)
        self.switch_style_select = widgets.Linked(rows, on_change=self._changed, host=self.switch_styles_box)
        # The chosen style's name and what it does, under the tiles that chose it.
        self.chosen_box = widgets.stack(spacing=6)
        self.effect_name = widgets.Label("", theme.TYPE["body"], 600)
        widgets.add(self.chosen_box, self.effect_name.view)
        self.effect_blurb = widgets.note()
        widgets.add(self.chosen_box, self.effect_blurb.view)
        module.add(self.chosen_box)
        module.body.setCustomSpacing_afterView_(18, self.chosen_box)
        # Every style and every switch plays at this length, so it follows the tiles either way.
        self.length_select = widgets.Segmented(effects.LENGTHS, on_change=self._changed)
        module.add(widgets.field_row("Length", self.length_select.view)[0])
        module.add(widgets.note(
            "How long each animation takes to play through once the pointer crosses, and to land."
        ).view)
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
        heading = widgets.label(group, theme.TYPE["note"], 600, ink="ink_2")
        widgets.add(box, heading)
        row = widgets.ChoiceTiles(tiles, columns=3)
        self._hover(row)
        widgets.add(box, row.view)
        box.setCustomSpacing_afterView_(8, heading)
        box.setCustomSpacing_afterView_(22, row.view)
        return row

    @objc.python_method
    def _colour_module(self):
        module = widgets.Module()
        module.add(widgets.eyebrow("Colour"))
        rows = []
        for group, choices in pages.colour_groups(crossing.GLOW_COLOURS, self.effects_ready):
            if group != pages.TODAY:
                choices = [(value, title, effects_overlay.palette(value)) for value, title in choices]
            heading = module.add(widgets.label(group, theme.TYPE["note"], 600, ink="ink_2"))
            module.body.setCustomSpacing_afterView_(8, heading)
            row = widgets.Swatches(choices, columns=len(crossing.GLOW_COLOURS))
            module.add(row.view)
            module.body.setCustomSpacing_afterView_(18, row.view)
            rows.append(row)
        self.glow_colour_select = widgets.Linked(rows, on_change=self._changed, host=module.body)
        module.add(widgets.note("Any colour works with any style. The notch's Outline uses it too; Island keeps Beamer's own cyan.").view)
        return module

    @objc.python_method
    def _trackpad_module(self):
        module = widgets.Module()
        module.add(widgets.eyebrow("Trackpad"))
        module.add(self._own_note())
        self.haptics_box = widgets.Switch("Tick as the push builds", on_change=self._changed)
        module.add(self.haptics_box.view)
        self.tick_steps_select = widgets.Segmented(
            [("quarters", "Every quarter"), ("halves", "Halfway"), ("breakthrough", "Only when through")],
            on_change=self._changed,
        )
        module.add(widgets.field_row("Ticks at", self.tick_steps_select.view)[0])
        # On the press, not the release: macOS plays nothing once the finger has left the trackpad.
        self.try_button = widgets.Button("Try it", self, "tryTick:", scale="small")
        self.try_button.view.sendActionOn_(AppKit.NSEventMaskLeftMouseDown)
        feel = widgets.stack(vertical=False, spacing=10)
        feel.addArrangedSubview_(self.try_button.view)
        feel.addArrangedSubview_(widgets.hug(widgets.box(), AppKit.NSLayoutPriorityDefaultLow))
        module.add(widgets.field_row("Feel it", feel)[0])
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
        module.add(widgets.eyebrow("Modifier keys"))
        self.modifier_select = widgets.Segmented(
            [("semantic", "Same shortcuts"), ("positional", "Same positions")], on_change=self._changed
        )
        self.modifier_select.view.setAccessibilityLabel_("Modifier keys")
        module.add(self.modifier_select.view)
        self.modifier_note = widgets.note()
        module.add(self.modifier_note.view)
        return module

    @objc.python_method
    def _connection_module(self):
        module = widgets.Module(spacing=10)
        # The page edits the first machine paired; any other is changed by pairing it again.
        self.connection_note = widgets.note()
        module.add(self.connection_note.view)
        self.hide_switch = widgets.Switch("Hide addresses", on_change=self._set_hide_addresses)
        module.add(self.hide_switch.view)
        module.add(widgets.note(
            "Hides every IP and hardware address in this window and its menu."
        ).view)
        host_box, self.host_field = widgets.field()
        secret_box, self.host_secret = widgets.field(secure=True)
        secret_box.setHidden_(True)
        self.host_boxes = (host_box, secret_box)
        host_line = widgets.stack(vertical=False, spacing=0)
        for box in self.host_boxes:
            host_line.addArrangedSubview_(box)
            widgets.hug(box, AppKit.NSLayoutPriorityDefaultLow)
        module.add(widgets.field_row("Address", host_line)[0])
        for control in (self.host_field, self.host_secret):
            control.setAccessibilityLabel_("Address")
        port_box, self.port_field = widgets.field()
        module.add(widgets.field_row("Port", port_box)[0])
        self.port_field.setAccessibilityLabel_("Port")
        self.wake_state = widgets.Label("", theme.TYPE["small"], mono=True)
        module.add(widgets.field_row("Wake-on-LAN", self.wake_state.view)[0])
        self.wake_hint = widgets.note()
        module.add(self.wake_hint.view)
        module.add(widgets.hairline())
        line = widgets.stack(vertical=False, spacing=12)
        line.addArrangedSubview_(widgets.note("A new address or port takes effect when you connect.").view)
        line.addArrangedSubview_(widgets.Button("Connect", self, "connect:", style="primary").view)
        module.add(line)
        return module

    @objc.python_method
    def _access_module(self):
        module = widgets.Module(spacing=10)
        self.access_status, self.access_button = self._permission_row(
            module, "Accessibility", "Lets Beamer move this Mac's pointer and type on it when the PC drives.",
            "requestAccessibility:",
        )
        module.add(widgets.hairline())
        self.input_status, self.input_button = self._permission_row(
            module, "Input Monitoring", "Lets Beamer read this keyboard and trackpad, to send them to the PC.",
            "requestInputMonitoring:",
        )
        self.capture_status = widgets.note()
        module.add(self.capture_status.view)
        self.relaunch_button = widgets.Button("Relaunch Beamer", self, "relaunch:", style="primary", full_width=True)
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
        status = widgets.Label("Required", theme.TYPE["small"], mono=True, ink="amber", tracking=theme.STATE_TRACKING, upper=True)
        widgets.add(words, status.view)
        line.addArrangedSubview_(widgets.hug(words, AppKit.NSLayoutPriorityDefaultLow))
        button = widgets.Button("Grant", self, action)
        line.addArrangedSubview_(button.view)
        module.add(line)
        return status, button

    @objc.python_method
    def _ways_in_sentence(self):
        # Read from the controls, not the saved config, which lands a moment after a change: the
        # sentence and the drawing's description would otherwise be one change behind.
        key = widgets.key_title(self.key_recorder.value)
        methods = {name for name, tile in self.method_boxes.items() if tile.value}
        edge = self.edge_select.value or "right"
        parts = []
        if "shortcut" in methods:
            parts.append(f"{'Hold' if self.style_select.value == 'hold' else 'Double-tap'} {key}")
        ways = []
        if "edge" in methods:
            ways.append(f"the {edge} edge")
        elif "part" in methods:
            ways.append(pages.parts_phrase(edge, [name for name, tile in self.part_boxes.items() if tile.value]))
        if "corner" in methods:
            ways.append(f"the {(self.corner_select.value or 'top_right').replace('_', ' ')} corner")
        if "notch" in methods:
            ways.append("the notch")
        if ways:
            joined = ways[0] if len(ways) == 1 else ", ".join(ways[:-1]) + " or " + ways[-1]
            parts.append(f"push through {joined}")
        if not parts:
            return ""
        sentence = ", or ".join(parts)
        return sentence[0].upper() + sentence[1:]

    @objc.python_method
    def _commit(self):
        bar = widgets.box("ground")
        self.message_label = widgets.note(pages.footer(None))
        line = widgets.stack(vertical=False, spacing=16)
        line.addArrangedSubview_(self.message_label.view)
        bar.addSubview_(line)
        widgets.pin(line, bar, (12, 16, 12, 16))
        return bar

    @objc.python_method
    def _say(self, message, ink="ink_2"):
        self.message_label.set(self._shown(message), ink=ink)

    @objc.python_method
    def show(self):
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
            cfg = self.settings_store.save(raw)
        except (SettingsError, TypeError, ValueError) as exc:
            self._say(str(exc), "fault")
            return
        self._say("Saved. Connecting…", "signal")
        self.controller.update_config(cfg)

    @objc.python_method
    def _apply_settings(self):
        """Writes every setting outside the Connection page and applies it without dropping the link."""
        raw = config_to_raw(self.controller.cfg)
        try:
            raw["trigger_key"] = self.key_recorder.value
            raw["ignored_inputs"] = list(self.ignored_entries)
            raw["trigger_style"] = self.style_select.value
            raw["double_tap_ms"] = self.double_tap_ruler.value
            raw["pointer_speed"] = self.pointer_ruler.value / 100
            raw["scroll_speed"] = self.scroll_ruler.value / 100
            raw["reverse_scroll"] = self.reverse_scroll_box.value
            raw["appearance"] = self.appearance_select.value or "system"
            if self.modifier_select.value in config_module.KEY_MAP_STYLES:
                raw["key_map"] = self.modifier_select.value
            raw["crossing"] = {
                "methods": [name for name, tile in self.method_boxes.items() if tile.value],
                "edge_parts": [name for name, tile in self.part_boxes.items() if tile.value] or ["middle"],
                "edge": self.edge_select.value,
                "corner": self.corner_select.value,
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
                "block_while_dragging": self.dragging_box.value,
                "hold_full_screen": self.hold_box.value,
                "arrangement_set_at": self.controller.cfg.crossing.get("arrangement_set_at", 0),
            }
            shared = self.controller.cfg.same_on_both and settings_sync.changed(
                settings_sync.mac_values(config_to_raw(self.controller.cfg)), settings_sync.mac_values(raw))
            if shared:
                raw["same_set_at"] = settings_sync.next_stamp(self.controller.cfg.same_set_at, time.time())
                raw["same_by"] = self.own_id()
            moved = raw["crossing"]["edge"] != self.controller.cfg.crossing.get("edge")
            if moved:
                # The edge is half of a value Windows holds too, so a change
                # here is stamped with the moment it was made. When the two
                # ends meet holding different answers -- one changed while the
                # other was asleep -- the newer stamp is the one that stands.
                raw["crossing"]["arrangement_set_at"] = settings_sync.next_stamp(self.controller.cfg.crossing.get("arrangement_set_at", 0), time.time())
            cfg = self.settings_store.save(raw)
        except (SettingsError, TypeError, ValueError) as exc:
            self._say(str(exc), "fault")
            return
        # apply_settings sends it over this Mac's own link; the PC's link is
        # the other way it can be reached, and either may be the one that is
        # up. Both are best-effort and say so by returning False.
        self.controller.apply_settings(cfg)
        if self.previews is not None:
            # The tiles' renderers read the colour from the applied settings, which land only now.
            self.previews.repaint()
        if moved and self.windows_input is not None:
            self.windows_input.send_arrangement(
                cfg.crossing["edge"], cfg.crossing.get("arrangement_set_at", 0)
            )
        if shared:
            self._send_same()
        self._say("Saved. Changes apply as you make them.", "ink_2")

    @objc.python_method
    def apply_arrangement(self, mac_edge, set_at, by=None):
        """The peer changed which edge of this Mac leads to it. Applied here
        rather than at either link, because this is the side that owns the
        settings file. An arrangement that is not newer than this Mac's own is ignored:
        both ends stamp their changes, and the newer one stands, a tie going to the
        larger `by` (WIRE.md section 8)."""
        held = self.settings_store.current()["peers"]
        held_by = protocol.read_id(held[0].get("side_by")) if held else None
        theirs = protocol.read_id(by) if by else None
        mine = self.controller.cfg.crossing.get("arrangement_set_at", 0)
        if mac_edge == self.controller.cfg.crossing.get("edge") and set_at == mine:
            return
        newer = set_at > mine or (set_at == mine and (theirs or b"") > (held_by or b""))
        if not newer:
            self.logger.info("ignoring an older arrangement from the peer (%s vs %s)", set_at, mine)
            return
        raw = config_to_raw(self.controller.cfg)
        raw["crossing"] = {**raw["crossing"], "edge": mac_edge, "arrangement_set_at": int(set_at)}
        try:
            cfg = self.settings_store.save(raw, side_by=by)
        except (SettingsError, TypeError, ValueError):
            self.logger.exception("could not save the arrangement the PC sent")
            return
        self.controller.cfg = cfg
        self.controller.crossing = crossing.CrossingEngine.from_config(cfg.crossing)
        self.logger.info("the PC moved the crossing to this Mac's %s edge", mac_edge)
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
            self.capture_status.set("Quit Beamer and start it again.", ink="amber")
            return
        subprocess.Popen(
            ["/bin/sh", "-c", 'while kill -0 "$0" 2>/dev/null; do sleep 0.2; done; exec /usr/bin/open "$1"', str(os.getpid()), bundle],
            start_new_session=True,
        )
        self.quit_handler()

    def toggleRedirect_(self, _sender):
        if not self.controller.input_ready:
            self._say("Grant both Mac permissions first.", "fault")
            return
        if not self.controller.redirecting and self.controller.can_wake:
            self.controller.wake()
            self._say(f"Waking {self._first_label()}. It connects on its own once it is up.", "ink_2")
        elif not self.controller.set_redirecting(not self.controller.redirecting):
            self._say(self.controller.connection_status, "fault")
        self.refresh()

    def togglePause_(self, _sender):
        self.controller.crossing_paused = not self.controller.crossing_paused
        self.refresh()

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
    def _permission(self, status, button, granted):
        status.set("Granted" if granted else "Required", ink="signal" if granted else "amber")
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
        self._show_same()
        access = accessibility_granted()
        listening = input_monitoring_granted()
        self._permission(self.access_status, self.access_button, access)
        self._permission(self.input_status, self.input_button, listening)
        self.retry_capture(access, listening)
        needs_relaunch = access and listening and not self.granted_at_launch
        self.relaunch_button.view.setHidden_(not needs_relaunch)
        if needs_relaunch:
            self.capture_status.set("Both granted. Relaunch Beamer so macOS passes it the keyboard.", ink="amber")
        elif controller.input_ready:
            self.capture_status.set("Input capture ready.", ink="signal")
        elif access and listening:
            detail = controller.input_error
            self.capture_status.set(f"Retrying input capture… {detail}" if detail else "Retrying input capture…", ink="fault")
        else:
            self.capture_status.set(
                "Beamer needs both, granted separately. macOS only delivers keyboard events to a process "
                "started after the grant, so relaunch Beamer once you have given them.",
                ink="ink_2",
            )

        sentence = self._ways_in_sentence()
        self.ways_note.set(sentence + "." if sentence else "No way in is switched on. Choose one below.")
        state = link_state.describe(controller)
        rows = self.panel.rows()
        self._place_machines(not rows)
        refused = any(row.state.key == "token" for row in rows)
        self.sidebar.set_link(replace(state, word=self._shown(state.word)), self._shown(self._machine_label()) if rows else "—")
        self.sidebar.set_dots(pages.dots(access, listening, "token" if refused else state.key))
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
        first = controller.peer_label
        self.connection_note.set(self._shown(
            f"The address and port of {first}, the first machine paired. Another machine is changed by pairing it again."
            if first else "Nothing is paired yet. Pairing fills the address and port in."))

        self.pause_button.set_title("Resume crossing" if controller.crossing_paused else "Pause crossing")
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
            self.toggle_button.set_title("Return input to Mac")
        elif controller.waking:
            self.toggle_button.set_title(self._shown(f"Waking {self._first_label()}…"))
        elif controller.can_wake:
            self.toggle_button.set_title(self._shown(f"Wake {self._first_label()}"))
        else:
            self.toggle_button.set_title(
                self._shown(f"Send input to {self._first_label()}") if controller.peer_label else "Send input")
        self.toggle_button.set_style("live" if controller.redirecting or controller.windows_locked else "primary")
        self.toggle_button.set_enabled(
            controller.input_ready
            and not controller.waking
            and (controller.connected or controller.can_wake)
        )
        mac = cfg.mac_address
        self.wake_state.set(self._shown(mac) if mac else "Not yet learned", ink="ink" if mac else "ink_3")
        self.wake_hint.set(
            "Crossing to the PC while it sleeps sends a wake-up packet and waits for it."
            if mac
            else "Read from the network the first time this Mac connects; nothing to type."
        )
        self.panel.refresh()


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
    """A login item takes no arguments, so a login launch is recognised instead: macOS marks it
    as not the default launch, and that is the cue to stay in the menu bar."""

    def initWithTray_(self, tray):
        self = objc.super(_LaunchWatch, self).init()
        self.tray = tray
        AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "launched:", AppKit.NSApplicationDidFinishLaunchingNotification, None
        )
        return self

    def launched_(self, note):
        info = note.userInfo() or {}
        default = info.get("NSApplicationLaunchIsDefaultLaunchKey")
        if default is not None and not bool(default):
            self.tray.hidden = True


class TrayApp(rumps.App):
    def __init__(self, controller, settings_store, logger, hidden=False):
        self.hidden = hidden
        self.launch_watch = _LaunchWatch.alloc().initWithTray_(self)
        self.controller = controller
        self.settings_store = settings_store
        self.logger = logger
        # Before anything reads or posts a key: the table follows this Mac's layout from here on.
        self.layout_observer = keyboard_layout.watch()
        self.control_window = ControlWindow.alloc().initWithController_settingsStore_logger_(
            controller, settings_store, logger
        )
        self.control_window.quit_handler = self.quit_app
        self.control_window.direction_handler = self._set_direction
        install_main_menu(self.control_window)
        self.gesture_overlay = GestureOverlay(controller, logger)
        self.edge_glow = EdgeGlow(controller, logger)
        self.notch_island = NotchIsland(controller, logger)
        self.notch_beam = NotchBeam(controller, logger)
        self.effects_overlay = effects_overlay.EffectsOverlay(controller, logger)
        self.haptics = Haptics(logger)
        self.control_window.preview = self.edge_glow.preview
        self.control_window.haptics = self.haptics
        self.controller.on_user_alert = self.notify_user
        self.controller.on_crossing = self.crossing_feedback
        self.controller.on_arrangement = self._arrangement
        self.controller.on_mac_learned = lambda mac: AppHelper.callAfter(self.control_window._persist_mac, mac)
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
        # Same on both machines, over either link: applied on the main thread, and announced
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
        )
        self.control_window.update_checker = self.update_checker
        self._notch_failed = False
        self.header_item = header = rumps.MenuItem(f"Beamer {VERSION}", callback=None)
        self.update_url = None
        self.status_item = rumps.MenuItem("Starting", callback=None)
        self.toggle_item = rumps.MenuItem("Send input", callback=self.toggle_redirect)
        self.pause_item = rumps.MenuItem("Pause crossing", callback=self.toggle_pause)
        # One tick per direction, so either can be switched off while the other keeps working.
        self.send_item = rumps.MenuItem("This Mac drives other machines", callback=self.toggle_send_to_windows)
        self.receive_item = rumps.MenuItem("Other machines drive this Mac", callback=self.toggle_windows_drives)
        self._symbol = None
        super().__init__(
            "Beamer",
            title="Beamer • Local",
            menu=[
                header,
                rumps.separator,
                self.status_item,
                self.toggle_item,
                self.pause_item,
                rumps.separator,
                self.send_item,
                self.receive_item,
                rumps.separator,
                rumps.MenuItem("Settings…", callback=self.open_window),
                rumps.MenuItem("Reload configuration", callback=self.reload_config),
                rumps.MenuItem("Open log folder", callback=self.open_log_folder),
                rumps.separator,
                rumps.MenuItem("About Beamer", callback=self.show_about),
                rumps.MenuItem("Quit Beamer", callback=self.quit_app),
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

    def _update_found(self, found):
        """On the main thread. The version line in the menu becomes the way to the download."""
        self.update_url = found[1] if found else None
        if found:
            self.header_item.title = f"Beamer {found[0]} is available…"
            self.header_item.set_callback(self.open_update)
        else:
            self.header_item.title = f"Beamer {VERSION}"
            self.header_item.set_callback(None)
        self.control_window.show_update(found)

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

    def _arrangement(self, mac_edge, set_at, by=None):
        """An arrangement from the PC, over either link, applied on the main
        thread -- it writes the settings file and redraws the window."""
        AppHelper.callAfter(self.control_window.apply_arrangement, mac_edge, set_at, by)

    def show_on_startup(self, _timer):
        self.startup_timer.stop()
        self.split_clicks()
        if not self.hidden:
            self.control_window.show()

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
            self.toggle_item.title = "Return input to Mac"
        else:
            self.toggle_item.title = pages.redact(
                f"Send input to {controller.peer_label}" if controller.peer_label else "Send input", hide)
        self.pause_item.title = "Resume crossing" if controller.crossing_paused else "Pause crossing"
        self._direction_items()
        self.status_item.title = pages.redact(link_state.describe(controller).word, hide)

    def _direction_items(self):
        """The two direction ticks, named for the machine when there is one, for every machine
        when there are several; mixed when the machines differ."""
        peers = self.controller.book.peers()
        hide = self.controller.cfg.hide_addresses
        labels = [pages.redact(label, hide) for label in peerlist.labels(peers).values()]
        if len(labels) == 1:
            send, receive = f"This Mac drives {labels[0]}", f"{labels[0]} drives this Mac"
        elif labels:
            send, receive = "This Mac drives every machine", "Every machine drives this Mac"
        else:
            send, receive = "This Mac drives other machines", "Other machines drive this Mac"
        self.send_item.title, self.receive_item.title = send, receive
        for item, name in ((self.send_item, "send"), (self.receive_item, "allow_drive")):
            on = [peer.get(name) is True for peer in peers]
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

    def toggle_send_to_windows(self, _sender):
        """The menu's tick covers every machine: all on turns them all off, anything else all on."""
        peers = self.controller.book.peers()
        self._switch([peer["token"] for peer in peers], send=not all(peer.get("send") is True for peer in peers))

    def toggle_windows_drives(self, _sender):
        peers = self.controller.book.peers()
        self._switch([peer["token"] for peer in peers], allow_drive=not all(peer.get("allow_drive") is True for peer in peers))

    def _set_direction(self, token, **change):
        """One machine's switch from its row on Overview: `send=` or `allow_drive=`."""
        self._switch([token], **change)

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
            self.notify_user("Beamer", f"Could not save the change: {exc}")
            # Some machines may have been saved before this one failed, and a switch the person
            # flipped shows a state that is not saved: read it all again.
            self.control_window.panel.invalidate()
            self.control_window.peers_changed()
            return
        if change.get("send") is False and any(entry.get("id") == controller.owner.on for entry in entries):
            # Input comes home by this switch, and shows where the pointer is as any switch does.
            controller.set_redirecting(False)
        self.control_window.peers_changed()
        self.logger.info("directions: %s", ", ".join(
            f"{entry.get('name')} send {'on' if entry['send'] else 'off'} drive {'on' if entry['allow_drive'] else 'off'}"
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
            self.notify_user("Configuration not reloaded", str(exc))
            return
        # The controller first: the window's address fields read Hide addresses from it.
        self.controller.update_config(cfg)
        self.control_window._load(config_to_raw(cfg))
        self.notify_user("Configuration reloaded", "Beamer is using the config on disk.")

    def show_about(self, _sender):
        show_about_panel()

    def measure_notch(self):
        """Re-read on every tick so plugging a display in above the MacBook, which moves the
        notch off the top of the desktop, is noticed without a restart."""
        if self._notch_failed:
            return
        try:
            self.controller.notch_range = notch_x_range()
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
        try:
            AppKit.NSBeep()
        except Exception:
            self.logger.exception("failed to beep for a user alert")
        try:
            rumps.notification("Beamer", title, pages.redact(message, self.controller.cfg.hide_addresses))
        except Exception:
            self.logger.exception("failed to show a user notification")

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


def main(argv=None):
    args = parse_args(argv)
    if sys.platform != "darwin":
        raise SystemExit("Beamer must run on macOS")
    instance_lock = acquire_instance_lock()
    if instance_lock is None:
        return
    AppKit.NSApplication.sharedApplication()
    theme.init_fonts()
    logger = configure_logging()
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
    book, identity = bridge.links_from_store(settings_store, VERSION)
    controller = WakingController(cfg, logger=logger, book=book, identity=identity, hardware=hardware_mac.hardware_address_towards)
    app = TrayApp(controller, settings_store, logger, hidden=args.hidden)
    controller.start()
    try:
        app.run()
    finally:
        controller.stop()
        instance_lock.close()


if __name__ == "__main__":
    main()
