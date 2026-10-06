import argparse
import collections
import fcntl
import logging
import logging.handlers
import os
import subprocess
import sys
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
from core import crash_log
from core import effects
from core.locale import americanise
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
import notices
import pages
from core import updates
from core import protocol
from core import pairing
from core import peerlist
from core import settings_sync
from core import ways as core_ways
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
    "local": "Input on this Mac",
    "held": "Input held on this Mac; crossing is off",
    "windows": "Input on another machine",
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
    then redraws at 30Hz until both the pressure and the flash have faded, since nothing
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
        box = self.controller._current_desktop_bounds()
        display = effects_overlay.display_at((self.region[0] + self.region[2] / 2.0,
                                              self.region[1] + self.region[3] / 2.0), box)
        full_depth = effects.edge_depth(display[2] - display[0], display[3] - display[1],
                                        feel.get("effect_size", "medium"))
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
        entry = menu.addItemWithTitle_action_keyEquivalent_(americanise(title), action, key)
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


class FlippedView(AppKit.NSTableRowView):
    """A native flipped container avoids Python callbacks for every descendant's frame change.

    The row supplies native top-left coordinates; the settings column owns its layout.
    """


def window_fill_frame(window):
    screen = window.screen() or AppKit.NSScreen.mainScreen()
    return screen.visibleFrame()


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
        elif isinstance(responder, widgets._RulerView):
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
        self.window.setMinSize_((320, 300))
        self.window.keyboard_navigation = False
        # The window always carries the palette's own appearance, never nil: the title bar's
        # buttons and text then match the palette whatever the Mac is set to, and the transparent
        # title bar takes the window's ground.
        self.window.setAppearance_(theme.appearance_named(theme.is_dark()))
        self.window.setBackgroundColor_(theme.colour("ground"))
        self.window.setTitlebarAppearsTransparent_(True)
        screen = self.window.screen() or AppKit.NSScreen.mainScreen()
        x, y, width, height = settings_normal_frame(
            self.settings_store.current().get("settings_window_frame"), screen.visibleFrame())
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
            footer_text=f"Beamer {VERSION}\nBeamer's website",
            footer_label="Open Beamer's website, kalkmancode.co.uk/beamer",
            on_footer=self._open_beamer_site,
        )
        self.sidebar.footer_icon.setImage_(menu_bar_glyph("local"))
        divider = widgets.box("rule")
        pane = widgets.stack(spacing=0)
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
            scroll, body = self._page(name, purpose, pages.SCOPE.get(key), key)
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
            self.jump_recorder.cancel()
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
        self.method_boxes["shortcut"].value = "shortcut" in crossing_raw["methods"]
        self.resistance_ruler.value = crossing_raw["resistance_px"]
        self.landing_box.value = crossing_raw["shortcut_arrival"]
        self.switch_style_select.value = crossing_raw["shortcut_arrival_style"]
        self.dragging_box.value = crossing_raw["block_while_dragging"]
        self.glow_style_select.value = crossing_raw["glow_style"]
        self.length_select.value = crossing_raw.get("effect_length", "normal")
        self.size_select.value = crossing_raw.get("effect_size", "medium")
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
            visible = rows[key] or key == "shortcut" and bool(getattr(self, "_trigger_conflict", ""))
            motion.set_hidden(view, not visible)
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
        # colour, the place, the length, the size, the palette, and for Same as crossing the crossing style.
        stills = (colour, style, place, self.length_select.value, self.size_select.value, theme.is_dark())
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
            f"Once the pointer is through to another machine the notch keeps playing for {after:g} seconds. Outline and Island apply to Glow and Beam; other styles draw their own notch."
        )
        self._run_previews()
        notch_range = self.controller.notch_range
        notch.set_detail(
            f"Top edge, {round(notch_range[1] - notch_range[0])} pt wide" if self.has_notch and notch_range else "This Mac has no notch"
        )
        # Among the chosen machine's ways the shortcut is the one that is not that machine's: it is
        # this Mac's, and goes where input last went.
        several = len(self.settings_store.current()["peers"]) > 1
        self.method_boxes["shortcut"].set_detail(
            widgets.key_title(self.key_recorder.value) + (", to where input last went" if several else ""))
        self.ignored_recorder.set_trigger_code(KEY_NAME_TO_CODE.get(self.key_recorder.value))
        hold = self.style_select.value == "hold"
        motion.set_hidden(self.double_tap_head, hold)
        self.style_hint.set(
            getattr(self, "_trigger_conflict", "") or ("Input is on another machine for as long as the key is held. If you jump while holding the shortcut, releasing it brings input home."
            if hold
            else "Tap twice to switch; tap twice again to come back."),
            ink="amber" if getattr(self, "_trigger_conflict", "") else "ink_2",
        )
        resistance = self.resistance_ruler.value
        self.resistance_numeral.set(str(resistance))
        self.push_strip.show(resistance / 500.0, self._chosen_label())
        side = self.edge_select.value
        self.resistance_hint.set(
            "Switches the moment this Mac's pointer touches the edge. Each screen supplies its own zones."
            if resistance == 0
            else "Follows this Mac's mouse or trackpad on every screen; each screen supplies its own zones. "
            f"This screen's {side} edge lights to show the resistance."
            if side
            else "Follows this Mac's mouse or trackpad on every screen; each screen supplies its own zones."
        )
        self.modifier_note.set({
            "semantic": "On a PC, Command arrives as Control, so Command-C copies there too, and Control arrives as the Windows key. "
                        "Between two Macs every key arrives as itself.",
            "positional": "On a PC, each key arrives as the key in its place, so Command arrives as the Windows key. "
                          "Between two Macs every key arrives as itself.",
        }.get(self.modifier_select.value, "Custom: the key map in settings.json is kept as it is."))

    @objc.python_method
    def _load_ways(self, flat=None):
        """The chosen machine's ways into the page's controls, from the settings (core/ways.py);
        with nothing active, from `flat`, the crossing settings as 1.4.x kept them. A chosen machine
        no longer paired or in use gives way to the first active machine."""
        settings = self.settings_store.current()
        peers = peerlist.desktops(settings["peers"])
        available = [entry for entry in peers if entry.get("in_use", True) is True]
        if self.chosen_peer not in {entry["id"] for entry in available}:
            self.chosen_peer = available[0]["id"] if available else None
        if self.chosen_peer is None:
            flat = flat or self.controller.cfg.crossing
            held = {"side": flat["edge"], "methods": flat["methods"], "parts": flat["edge_parts"], "corner": flat["corner"]}
        else:
            held = core_ways.ways(settings, self.chosen_peer)
        self.edge_select.value = pages.shown_side(held["side"], len(peers))
        for name in core_ways.KIND_WORDS:
            self.method_boxes[name].value = name in held["methods"]
        for name, tile in self.part_boxes.items():
            tile.value = name in held["parts"]
        self.corner_select.value = held["corner"]
        self._show_machines()

    @objc.python_method
    def _show_machines(self):
        """The machine picker, shown with more than one machine paired and built again when the
        machines or their names change, and the words on the page that name the chosen one."""
        settings = self.settings_store.current()
        peers = peerlist.desktops(settings["peers"])
        available = [entry for entry in peers if entry.get("in_use", True) is True]
        if self.chosen_peer not in {entry["id"] for entry in available} and (available or self.chosen_peer is not None):
            self._load_ways()
            self._reflect()
            return
        labels = peerlist.labels(peers)
        choices = [(entry["id"], self._shown(labels[entry["token"]])) for entry in peers
                   if entry.get("in_use", True) is True]
        several = len(choices) > 1
        if choices != self._picker_choices:
            self._picker_choices = choices
            for view in list(self.machine_row.arrangedSubviews()):
                self.machine_row.removeArrangedSubview_(view)
                view.removeFromSuperview()
            self.machine_select = None
            if several:
                self.machine_select = widgets.Segmented(choices, columns=min(len(choices), 4), on_change=self._machine_picked)
                widgets.add(self.machine_row, widgets.field_row("Machine", self.machine_select.view)[0])
        motion.set_hidden(self.machine_row, not several)
        if self.machine_select is not None and self.machine_select.value != self.chosen_peer:
            self.machine_select.value = self.chosen_peer
        label = self._chosen_label() if several else None
        self.ways_heading.setStringValue_(f"Ways from this screen to {self._chosen_label()}" if self.chosen_peer else "Ways from this screen")
        caption = pages.where_caption(label)
        if self.edge_caption.text != caption:
            self.edge_caption.set(caption)
            self.edge_select.view.setAccessibilityLabel_(caption)
        self.edge_note.set(pages.notch_or_corner_note(label))
        chosen = self.chosen_peer
        offer = core_ways.share_offer(settings, chosen, self._live) if chosen else None
        holders = [self._shown(peerlist.label_for(peers, peer_id=peer) or peerlist.UNNAMED)
                   for peer in offer["holders"]] if offer else []
        identity = (offer["side"], tuple(offer["holders"]), tuple(offer["thirds"].items())) if offer else None
        if identity != getattr(self, "_share_identity", None):
            self._share_identity = identity
            self.share_thirds = dict(offer["thirds"]) if offer else {}
        motion.set_hidden(self.share_box, offer is None)
        if offer:
            side = offer["side"]
            self.share_title.set(f"Share the {side} edge")
            self.share_sentence.set(pages.share_sentence(holders, side, self._chosen_label()))
            self.share_button.set_title(f"Share the {side} edge")
            self.share_recipients = [chosen, *offer["holders"]]
            for part, tile in self.share_tiles.items():
                tile.name.set(pages.part_names(side)[part])
                owner = self.share_thirds[part]
                tile.set_detail(self._shown(peerlist.label_for(peers, peer_id=owner) or peerlist.UNNAMED))
                tile.value = owner == chosen
            self.share_button.set_enabled(chosen in self.share_thirds.values())
        for note, sentence in (
                (self.blocked_note, (core_ways.blocked_sentence(settings, chosen, "this Mac", self._live) if chosen and not offer else "")
                 or (core_ways.gone_sentence(settings, chosen, "this Mac", self._gone) if chosen else "")),
                (self.missing_note, core_ways.missing_sentence(settings, chosen, "this Mac") if chosen else ""),
                (self.no_way_back_note, core_ways.no_way_back_sentence(settings, chosen) if chosen else "")):
            note.set(self._shown(sentence))
            motion.set_hidden(note.view, not sentence)

        self._show_jump_key()

    @objc.python_method
    def _share_third(self, part):
        """Move one offered third to the next machine without writing until Share is pressed."""
        recipients = self.share_recipients
        current = recipients.index(self.share_thirds[part])
        self.share_thirds[part] = recipients[(current + 1) % len(recipients)]
        self._show_machines()

    @objc.python_method
    def _share_side(self):
        """Apply the proposed split, rebuild the live zones and tell every affected machine."""
        side, thirds = self._share_identity[0], dict(self.share_thirds)
        try:
            changed = self.settings_store.share_side(side, thirds)
            cfg = self.settings_store.load()
        except (SettingsError, ValueError):
            self.logger.exception("The shared side could not be saved")
            return
        self.controller.cfg = cfg
        self.controller.zones_changed()
        for peer in changed:
            self._tell(peer)
        self._load_ways()
        self._reflect()

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
    def _diagram_machines(self, methods):
        """Every machine as the drawing shows it: the chosen one as the controls have it, which may
        be a moment ahead of the settings, the rest as saved."""
        settings = self.settings_store.current()
        peers = peerlist.desktops(settings["peers"])
        live = {
            "side": self.edge_select.value or "",
            "methods": [name for name in methods if name != "shortcut"],
            "parts": [name for name, tile in self.part_boxes.items() if tile.value],
            "corner": self.corner_select.value or "top_right",
        }
        if not peers:
            return [{"key": "", "label": link_state.peer_name(self.controller.cfg), "chosen": True, **live}]
        labels = peerlist.labels(peers)
        machines = []
        for entry in peers:
            chosen = entry["id"] == self.chosen_peer
            held = live if chosen else core_ways.ways(settings, entry["id"])
            machines.append({"key": entry["id"], "label": self._shown(labels[entry["token"]]), "chosen": chosen,
                             "side": held["side"], "methods": list(held["methods"]), "parts": list(held["parts"]),
                             "corner": held["corner"]})
        return machines

    @objc.python_method
    def _show_arrangement(self, methods):
        held = self.style_select.value == "hold"
        sentence = self._ways_in_sentence()
        self.ways_note.set(sentence + "." if sentence else "No way in is switched on. Choose one below.")
        machines = self._diagram_machines(methods)
        self.arrangement_diagram.show(
            machines, widgets.key_cap(self.key_recorder.value), self.has_notch,
            pages.arrangement_description(machines, sentence), key_how="hold" if held else "double-tap",
            shortcut="shortcut" in methods,
        )

    @objc.python_method
    def _preview_resistance(self, value):
        # Applied to the live engine at once, so the edge can be felt mid-drag; the saved setting
        # follows a moment later, once the ruler stops moving.
        self.controller.crossing.resistance_px = float(value)
        self._changed()
        # A machine not placed yet has no edge to light, and the glow turns itself off for the run
        # on one it cannot draw.
        if self.preview is not None and self.glow_box.value and self.edge_select.value:
            self.preview(self.edge_select.value, min(1.0, value / 500.0))

    @objc.python_method
    def _show_peer(self):
        """The machine the status speaks of (where input is, else where the shortcut would send it), else
        the first one paired: its name and address."""
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
        wanted = self.controller._in_question()
        return next((peer for peer in peers if wanted is not None and peer.get("id") == wanted), peers[0] if peers else None)

    @objc.python_method
    def _machine_label(self):
        """What the window calls the machine its status is about: where input is, else the first."""
        controller = self.controller
        return controller.on_label or controller.peer_label or link_state.peer_name(controller.cfg)

    @objc.python_method
    def _live(self, peer):
        """Whether `peer` has a link up now, either way. One that has none gives way on the Crossing
        page and to an arrangement (core/ways.py give_way)."""
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
        """Puts back the zones set aside for a machine that has connected since, and keeps off for good,
        and says so, those of one that has removed this pairing (core/ways.py settle_presence). Run on
        every status tick, whether or not the window is shown."""
        if not core_ways.presence_due(self.settings_store.current(), self._live, self._gone):
            return
        self._flush()
        before = core_ways.way_back_state(self.settings_store.current())
        try:
            changed, notices = self.settings_store.settle_presence(self._live, self._gone)
            cfg = self.settings_store.load() if changed else None
        except SettingsError:
            self.logger.exception("could not settle the zones set aside")
            return
        if not changed:
            return
        self.controller.cfg = cfg
        self.controller.zones_changed()
        for notice in notices:
            self.controller._alert("Beamer", notice)
        self._load(config_to_raw(cfg))
        self.refresh()
        for target, state in core_ways.way_back_state(self.settings_store.current()).items():
            if before.get(target) != state:
                self._tell(target)

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
        self._save_pending = True
        self._apply_serial += 1
        serial = self._apply_serial
        AppHelper.callLater(0.3, lambda: serial == self._apply_serial and self._apply_settings())

    def windowWillClose_(self, _notification):
        self._remember_normal_frame()
        try:
            with self.settings_store.lock:
                settings = dict(self.settings_store.current())
                settings["settings_window_frame"] = self._normal_frame
                self.settings_store.save_settings(settings)
        except SettingsError as exc:
            self.logger.warning("Could not save settings window frame: %s", exc)
        self.key_recorder.cancel()
        self.jump_recorder.cancel()
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
        widgets.add(body, self.panel.sheet.view)
        widgets.add(body, self._status_module().view)
        widgets.add(body, self._keyboard_module().view)
        widgets.add(body, self._same_module().view)
        widgets.add(body, self._login_module().view)
        widgets.add(body, self._updates_module().view)

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
        module.add(widgets.eyebrow("Settings"))
        self.same_switch = widgets.Switch("Same on all machines", on_change=self._set_same)
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
        links = [(True, getattr(self.controller, "peer_settings", None))]
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
        who = settings_sync.who(list(peerlist.labels(peerlist.desktops(self.controller.book.peers())).values()))
        self.same_note.set(settings_sync.switch_note(who, cfg.same_on_both, old), ink="amber" if old else "ink_2")
        self.same_note.view.setToolTip_(settings_sync.switch_detail("mac"))
        for key, label in self.scope_labels.items():
            text = settings_sync.scope(key, who, on, pages.SCOPE.get(key), "mac")
            if label.text != text:
                label.set(text)
        for note in self.own_notes:
            if note.view.isHidden() == on:
                motion.set_hidden(note.view, not on)
        self._refresh_design_follow()

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
            self.logger.warning("Same on all machines not saved: %s", exc)
            return
        self.controller.apply_settings(cfg)
        self._send_same()
        if not on:
            self._apply_followed_design(force=True)
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
            "values": {key: values[key] for key in settings_sync.DESIGN_KEYS},
            "path": (self._design_path or [self.own_id()])
                    if cfg.design_follow_peer and not cfg.same_on_both else [self.own_id()],
        }
        return settings_sync.message_data(cfg.same_on_both, cfg.same_set_at,
                                          values, by=cfg.same_by or self.own_id(), design_state=design_state)

    @objc.python_method
    def _apply_followed_design(self, force=False):
        cfg = self.controller.cfg
        peer = cfg.design_follow_peer
        state = self._design_states.get(peer)
        if not peer or state is None or cfg.same_on_both:
            return
        current = settings_sync.mac_values(config_to_raw(cfg))
        cursor = self._followed_version
        stamp, author = cursor[1:] if cursor and cursor[0] == peer else (-1, "")
        taken = settings_sync.followed_design(current, state, peer, peer, cfg.same_on_both,
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
        cfg = self.controller.cfg
        was_on = cfg.same_on_both
        taken = settings_sync.arrived(data, cfg.same_set_at, KEY_NAME_TO_CODE, by_here=cfg.same_by)
        if taken is not None:
            on, set_at, values = taken
            raw = config_to_raw(cfg)
            design_stamp = None
            if on:
                values, conflict = core_ways.shared_trigger_values(self.settings_store.current(), values)
                if conflict is not None:
                    self._show_trigger_conflict(conflict)
                design_stamp = settings_sync.design_change_stamp(
                    settings_sync.mac_values(raw), values, cfg.design_set_at,
                    settings_sync.read_design_state(data, KEY_NAME_TO_CODE), self.own_id(),
                )
            raw["same_on_both"] = on
            raw["same_set_at"] = set_at
            raw["same_by"] = data.get("by", "")
            if on:
                raw = settings_sync.apply_mac(raw, values)
                if design_stamp is not None:
                    raw["design_set_at"] = design_stamp["set_at"]
                    raw["design_by"] = design_stamp["by"]
            try:
                cfg = self.settings_store.save(raw)
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
        resuming = taken is not None and was_on and not on
        self._receive_design(data, peer, apply=not resuming)
        if resuming:
            self._apply_followed_design(force=True)

    @objc.python_method
    def _login_module(self):
        module = widgets.Module()
        module.add(widgets.eyebrow("At login"))
        self.login_switch = widgets.Switch("Start Beamer when you log in", on_change=self._set_login)
        module.add(self.login_switch.view)
        self.login_note = widgets.note()
        module.add(self.login_note.view)
        self._show_login_state()
        return module

    @objc.python_method
    def _updates_module(self):
        module = widgets.Module()
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
        self.follow_module = widgets.Module()
        self.follow_module.add(widgets.eyebrow("Match design with"))
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
        for view in self.follow_host.arrangedSubviews():
            self.follow_host.removeArrangedSubview_(view)
            view.removeFromSuperview()
        peers = peerlist.desktops(self.settings_store.current()["peers"])
        labels = peerlist.labels(peers)
        choices = [("", "None")]
        caps = self._design_peer_caps()
        rows = settings_sync.follow_rows(peers, caps, self._design_states, self.controller.cfg.design_follow_peer)
        unsupported = {entry["id"]: why_not for entry, why_not in rows if why_not is not None}
        for entry, why_not in rows:
            title = labels.get(entry.get("token", ""), entry.get("name") or "Machine")
            if why_not == settings_sync.NEEDS_DESIGN_SYNC:
                title += f" — {why_not}"
            choices.append((entry["id"], title))
        self.follow_select = widgets.MenuChoice(choices, on_change=self._set_design_follow)
        self.follow_select.value = self.controller.cfg.design_follow_peer
        for value, cell in self.follow_select.items:
            if value in unsupported:
                cell.setEnabled_(False)
                cell.setToolTip_(unsupported[value])
        widgets.add(self.follow_host, self.follow_select.view, full_width=False)
        peer = self.controller.cfg.design_follow_peer
        entry = next((item for item in peers if item.get("id") == peer), {}) if peer else {}
        name = labels.get(entry.get("token", ""), entry.get("name") or "that machine")
        if peer:
            if self.controller.cfg.same_on_both:
                note = f"Following {name}, paused while Same on all machines is on."
            elif peer not in caps:
                note = f"Keeping the last design received from {name} until it reconnects."
            else:
                note = ""
        else:
            note = settings_sync.follow_note(rows) or "None: this machine keeps its own Design."
        self.follow_note.set(note)
        self.follow_note.view.setHidden_(not note)
        self._lock_followed_design(settings_sync.follow_lock(peer, self.controller.cfg.same_on_both, peers), name)

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
        # Modifier keys first: chosen once, while the list below grows as keys are added.
        widgets.add(body, self._modifier_module().view)
        widgets.add(body, self._ignored_module().view)
        widgets.add(body, self._speed_module().view)

    @objc.python_method
    def _speed_module(self):
        """How the other machine's mouse feels on this Mac: it sends what its own acceleration made of the
        hand's movement, and this Mac's settings decide the rest."""
        module = widgets.Module()
        figure, self.pointer_numeral = self._numeral("%")
        module.add(self._head("Another machine's pointer here", figure))
        self.pointer_ruler = widgets.Ruler(
            25, 400, (25, 100, 200, 300, 400), step=5, minor=25, on_change=self._speed_moved,
            title="Pointer speed", arrow_step=25,
        )
        module.add(self.pointer_ruler.view)
        module.add(widgets.note("Pointer speed for another machine's mouse or trackpad while it drives this Mac.").view)
        figure, self.scroll_numeral = self._numeral("%")
        module.add(self._head("Scrolling", figure))
        self.scroll_ruler = widgets.Ruler(
            25, 400, (25, 100, 200, 300, 400), step=5, minor=25, on_change=self._speed_moved,
            title="Scroll speed", arrow_step=25,
        )
        module.add(self.scroll_ruler.view)
        self.reverse_scroll_box = widgets.Switch("Reverse other machines' scrolling", on_change=self._changed)
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
        self.hold_box = widgets.Switch("Hold this Mac's edges while an app on it is full screen", on_change=self._hold_changed)
        module.add(self.hold_box.view)
        module.add(self._own_note())
        self.hold_note = widgets.note(
            "This screen decides, including when another machine drives it. On, a full-screen app here holds pointer crossings; shortcuts and jump keys still work. Off, another machine's pointer can go home or onward through the configured ways."
        )
        module.add(self.hold_note.view)
        self.hold_box.view.setToolTip_("This screen's setting applies to every pointer on it.")
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
        self.ways_heading = widgets.eyebrow("Ways from this screen")
        # With several machines paired, which one everything per machine below shows and writes.
        module.add(self.ways_heading)
        self.machine_row = widgets.stack(spacing=0)
        self.machine_row.setHidden_(True)
        module.add(self.machine_row)
        # The picker scopes everything under it, so it stands a step apart from what it scopes.
        module.body.setCustomSpacing_afterView_(18, self.machine_row)
        self.ways_note = widgets.note()
        module.add(self.ways_note.view)
        self.arrangement_diagram = diagram.ArrangementDiagram.alloc().init().setup()
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
        ways_list = widgets.stack(spacing=0)
        widgets.add(ways_list, widgets.grid([tile.view for tile in self.method_boxes.values()], 2))
        self.ways_pair = widgets.SideBySide(
            self.arrangement_diagram,
            ways_list,
            theme.tokens.CROSSING_ARRANGEMENT_MIN_WIDTH,
            theme.tokens.CROSSING_WAYS_MIN_WIDTH,
        )
        self.side_by_side.append(self.ways_pair)
        module.add(self.ways_pair.view)
        module.body.setCustomSpacing_afterView_(16, self.ways_pair.view)
        module.add(self._own_note(pages.OWN_NOTCH))
        # The values are the side of this Mac the chosen machine is on, which is also the edge that crosses.
        self.edge_select = widgets.Segmented(
            [("left", "Left"), ("right", "Right"), ("top", "Above"), ("bottom", "Below")], on_change=self._changed
        )
        self.edge_select.view.setToolTip_("Changing a side updates the other end's side; it can turn overlapping ways off, with a notice.")
        self.edge_row, self.edge_caption = widgets.field_row(pages.where_caption(None), self.edge_select.view)
        module.add(self.edge_row)
        # A group within Ways, never a panel of its own: a panel inside a panel also forced the
        # window's layout and squeezed the pairing sheet's code boxes to 28 pt.
        self.share_box = widgets.stack(spacing=8)
        self.share_title = widgets.Label("Share the edge", theme.TYPE["eyebrow"], 700, "ink_3",
                                         tracking=theme.TRACKING["eyebrow"], upper=True)
        widgets.add(self.share_box, self.share_title.view)
        self.share_sentence = widgets.note()
        widgets.add(self.share_box, self.share_sentence.view)
        self.share_thirds = {}
        self.share_tiles = {
            part: widgets.WayTile(pages.part_names("right")[part], on_change=lambda _on, part=part:
                                  self._share_third(part)) for part in return_edge.PARTS
        }
        widgets.add(self.share_box, widgets.grid([tile.view for tile in self.share_tiles.values()], 3))
        self.share_button = widgets.action_button("Share the right edge", self._share_side, style="primary")
        self.share_box.addArrangedSubview_(self.share_button.view)
        self.share_box.setHidden_(True)
        module.add(self.share_box)
        self.edge_note = widgets.note(pages.notch_or_corner_note(None))
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
        self.corner_select.view.setToolTip_(pages.corner_note())
        self.corner_row = widgets.field_row("Corner", self.corner_select.view)[0]
        module.add(self.corner_row)
        # What stops a way working, under the ways it is about: a side another machine's edge holds,
        # a machine that may drive this Mac and is not paired with the chosen one, and the chosen one
        # saying none of its own ways leads back here.
        self.blocked_note = widgets.note(ink="amber")
        self.missing_note = widgets.note(ink="amber")
        self.no_way_back_note = widgets.note()
        for note in (self.blocked_note, self.missing_note, self.no_way_back_note):
            note.view.setHidden_(True)
            module.add(note.view)
        self.dragging_box = widgets.Switch("Don't cross while dragging with this Mac's pointer", on_change=self._changed)
        self.dragging_box.view.setToolTip_("Applies when this physical pointer pushes through this screen's ways; visiting pointers are not protected by this switch.")
        module.add(self.dragging_box.view)
        self.jump_box = widgets.stack(spacing=8)
        self.jump_heading = widgets.eyebrow("Jump to this machine")
        widgets.add(self.jump_box, self.jump_heading)
        self.jump_recorder = widgets.JumpKeyRecorder(on_change=self._record_jump_key, on_arm=self._jump_arming)
        jump_controls = widgets.stack(vertical=False, spacing=8)
        jump_controls.addArrangedSubview_(self.jump_recorder.view)
        self.jump_clear = widgets.Button("Clear", self, "clearJumpKey:", scale="small")
        jump_controls.addArrangedSubview_(self.jump_clear.view)
        widgets.add(self.jump_box, jump_controls)
        self.jump_note = widgets.note()
        widgets.add(self.jump_box, self.jump_note.view)
        module.add(self.jump_box)
        module.body.setCustomSpacing_afterView_(18, self.dragging_box.view)
        return module

    @objc.python_method
    def _show_jump_key(self):
        peers = self.settings_store.current()["peers"]
        entry = next((peer for peer in peers if peer.get("id") == self.chosen_peer), None)
        self.jump_box.setHidden_(entry is None)
        if entry is None:
            self.jump_recorder.cancel()
            return
        self.jump_recorder.set_value(entry.get("jump_key", ""))
        name = self._shown(peerlist.label_for(peers, peer_id=self.chosen_peer) or "this machine")
        self.jump_heading.setStringValue_(f"Jump to {name}")
        self.jump_recorder.view.setAccessibilityLabel_(f"Jump to {name} key, {self.jump_recorder._title()}")
        chord = self.jump_recorder._title()
        if entry.get("jump_key"):
            self.jump_note.set(f"Press {chord} to send input to {name}; press again to bring it home.")
        else:
            self.jump_note.set(f"Choose a key combination to jump to {name} and back.")
        self.jump_note.view.setToolTip_(f"Works while edges are held. Needs In use, This Mac drives it and permission from {name} to drive it.")
        self.jump_clear.view.setAccessibilityLabel_(f"Clear jump key for {name}")

    @objc.python_method
    def _jump_arming(self, armed):
        self.controller.jump_recording = armed

    @objc.python_method
    def _record_jump_key(self, value):
        self._save_jump_key(value)

    @objc.python_method
    def _save_jump_key(self, value):
        try:
            self.settings_store.set_jump_key(self.chosen_peer, value, self.controller.cfg.trigger_key)
        except SettingsError as exc:
            peer = next((peer for peer in self.settings_store.current()["peers"] if peer.get("id") == self.chosen_peer), None)
            if peer is not None:
                self.jump_recorder.set_value(peer.get("jump_key", ""))
            self.jump_note.set(str(exc), ink="amber")
            return
        self._show_jump_key()

    def clearJumpKey_(self, _sender):
        self._save_jump_key("")

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
        self.key_recorder = widgets.KeyRecorder("alt_r", on_change=self._trigger_chosen)
        module.add(self.key_recorder.view)
        self.style_select = widgets.Segmented([("double_tap", "Double-tap"), ("hold", "Hold")], on_change=self._changed)
        module.add(widgets.field_row("How you press it", self.style_select.view)[0])
        # The figure sits on the ruler's own line, so it reads as the ruler's value rather than as
        # a heading for the whole module, and the two hide together under Hold.
        self.double_tap_head = widgets.stack(spacing=6)
        figure, self.double_tap_numeral = self._numeral("ms")
        caption, _ = widgets.field_row("Time between taps", figure)
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
        module.add(widgets.eyebrow("Stays on this Mac"))
        module.add(widgets.note(
            "These keys and buttons keep working on this Mac while its input is on another machine: a "
            "mouse's back button for this Mac's browser, say, or a volume key for its speakers."
        ).view)
        self.ignored_entries = []
        self.ignored_list = widgets.stack(spacing=6)
        module.add(self.ignored_list)
        self.ignored_empty = widgets.note("Nothing yet. Every key and button goes to the machine that has input.")
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
        module = widgets.Module()
        module.add(widgets.eyebrow("On screen"))
        self.glow_box = widgets.Switch("Animate crossings on this Mac", on_change=self._changed)
        module.add(self.glow_box.view)
        module.add(self._own_note())
        module.add(widgets.note(
            "Turns crossing and landing animations on or off on this screen. Saved style choices stay in place."
        ).view)
        self.landing_box = widgets.Switch("Show where the pointer lands", on_change=self._changed)
        self.landing_row = widgets.stack(spacing=8)
        widgets.add(self.landing_row, self.landing_box.view)
        widgets.add(self.landing_row, widgets.note(
            "For shortcut, menu and jump arrivals; requires Animate crossings on this screen."
        ).view)
        module.add(self.landing_row)
        return module

    @objc.python_method
    def _preview_tile(self, base, value, title, detail, notch, flexible=False, **overrides):
        """One tile's preview: its own screen, a feed pinned to the style the tile shows, and the
        real renderer hosted on it, registered with the page's loop."""
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
        widgets.add(row, widgets.label("Notch style", theme.TYPE["note"], 600, ink="ink_2"))
        notch = previews.notch_size()
        self.notch_style_select = widgets.ChoiceTiles(
            [
                self._preview_tile(NotchBeam, "beam", "Outline", "A line of colour runs round the notch, faster the harder you push.", notch),
                self._preview_tile(NotchIsland, "island", "Island", "The notch grows as you push, with a meter inside, and flashes as you go through.", notch),
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
        widgets.add(row, widgets.field_row("Keep animating", self.notch_after_select.view)[0])
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
                        self.length_select, self.size_select, self.landing_box):
            control.set_enabled(not locked)
        self.follow_banner_label.set(settings_sync.following_banner(name) if locked else "")
        self.follow_banner.setHidden_(not locked)

    @objc.python_method
    def _edge_module(self):
        """Every style, grouped as effects.DIRECTIONS groups them: today's Glow and Beam first with
        stills like the rest, then each direction's quiet, medium and showpiece effect; every tile shows
        its style at the place chosen above them, and plays while the pointer is over it."""
        module = widgets.Module()
        module.add(widgets.eyebrow("Style"))
        module.add(self._follow_banner())
        self.style_preview_hint = widgets.note("Hover to preview", ink="ink_2")
        module.add(self.style_preview_hint.view)
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
        module.body.setCustomSpacing_afterView_(6, self.effect_method_view)
        self.notch_row = self._notch_row()
        colour = lambda: self.glow_colour_select.value or "signal"
        place = lambda: self.place
        pace = lambda: effects.pace(self.length_select.value)
        effect_size = lambda: self.size_select.value or "medium"
        # The two sets of tiles, one per mode; only the chosen mode's shows.
        self.crossing_styles_box = widgets.stack(spacing=0)
        tile_sets = widgets.stack(spacing=6)
        widgets.add(tile_sets, self.crossing_styles_box)
        rows = []
        for group, choices in pages.style_groups(self.effects_ready):
            # Glow and Beam as stills of the same scene as the effects, so every tile reads alike.
            tiles = [(value, title, detail, previews.effect_still(value, colour, self.logger, place=place,
                                                                    pace=pace, effect_size=effect_size))
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
                    fx, colour, self.logger, pace=pace, effect_size=effect_size)))
            self.effect_stills.extend(tile[3] for tile in tiles)
            rows.append(self._style_group(self.switch_styles_box, group, tiles))
        self._style_grid(self.switch_styles_box)
        widgets.add(self.switch_styles_box, widgets.note(
            "What plays around the pointer for shortcut, menu and jump arrivals. Same as crossing uses this screen's crossing style."
        ).view)
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
        self.length_select = widgets.Segmented(effects.LENGTHS, on_change=self._changed)
        self.size_select = widgets.Segmented(effects.SIZES, on_change=self._changed)
        self.length_row = widgets.field_row("Length", self.length_select.view)[0]
        self.size_row = widgets.field_row("Size", self.size_select.view)[0]
        module.add(self.length_row)
        module.add(self.size_row)
        module.add(widgets.note(
            "Length changes effect playback time, not crossing resistance or key timing. Size changes depth for styles that support it; it does not change resistance."
        ).view)
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
        row = widgets.ChoiceTiles(tiles, vertical=True, responsive=True)
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
        module.add(widgets.eyebrow("Colour"))
        rows = []
        for group, choices in pages.colour_groups(crossing.GLOW_COLOURS, self.effects_ready):
            if group != pages.TODAY:
                choices = [(value, title, effects_overlay.palette(value)) for value, title in choices]
            heading = module.add(widgets.label(group, theme.TYPE["note"], 600, ink="ink_2"))
            module.body.setCustomSpacing_afterView_(8, heading)
            row = widgets.Swatches(choices, responsive=True)
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
        ticks, _caption = widgets.field_row("Ticks at", self.tick_steps_select.view)
        self.tick_steps_pair = ticks
        module.add(ticks)
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
        module = widgets.Module()
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
        module.add(widgets.field_row("First paired machine's port", port_box)[0])
        self.port_field.setAccessibilityLabel_("First paired machine's port")
        self.wake_state = widgets.Label("", theme.TYPE["small"], mono=True)
        module.add(widgets.field_row("Wake-on-LAN", self.wake_state.view)[0])
        self.wake_hint = widgets.note()
        module.add(self.wake_hint.view)
        module.add(widgets.hairline())
        line = widgets.stack(vertical=False, spacing=12)
        line.addArrangedSubview_(widgets.note("A new address or port takes effect when you connect.", reading=False).view)
        line.addArrangedSubview_(widgets.Button("Connect", self, "connect:", style="primary").view)
        module.add(line)
        return module

    @objc.python_method
    def _access_module(self):
        module = widgets.Module()
        self.access_status, self.access_button = self._permission_row(
            module, "Accessibility", "Lets Beamer move this Mac's pointer and type on it when another machine drives.",
            "requestAccessibility:",
        )
        module.add(widgets.hairline())
        self.input_status, self.input_button = self._permission_row(
            module, "Input Monitoring", "Lets Beamer read this keyboard and trackpad, to send them to another machine.",
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
        edge = self.edge_select.value
        if not edge:
            # A machine not placed yet: its edge and thirds lead nowhere until a side is chosen.
            methods -= {"edge", "part"}
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
            # With several machines the pointer's ways lead to the chosen one; the shortcut goes
            # wherever input last was, so it is not said to.
            several = len(self.settings_store.current()["peers"]) > 1
            parts.append(f"push through {joined}" + (f" to reach {self._chosen_label()}" if several else ""))
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
        """Writes every setting outside the Connection page and applies it without dropping the link:
        this Mac's own through the flat settings, the chosen machine's ways through `set_ways`."""
        self._save_pending = False
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
                # Read only while nothing is paired; a machine's side and zones go through set_ways.
                "edge": self.edge_select.value or self.controller.cfg.crossing["edge"],
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
                "effect_size": self.size_select.value or "medium",
                "block_while_dragging": self.dragging_box.value,
                "hold_full_screen": self.hold_box.value,
                "arrangement_set_at": self.controller.cfg.crossing.get("arrangement_set_at", 0),
            }
            before = settings_sync.mac_values(config_to_raw(self.controller.cfg))
            after = settings_sync.mac_values(raw)
            shared = self.controller.cfg.same_on_both and settings_sync.changed(before, after)
            design_changed = any(before.get(key) != after.get(key) for key in settings_sync.DESIGN_KEYS)
            if design_changed:
                raw["design_set_at"] = settings_sync.next_stamp(
                    max(self.controller.cfg.design_set_at, self.controller.cfg.same_set_at), time.time())
                raw["design_by"] = self.own_id()
            if shared:
                raw["same_set_at"] = settings_sync.next_stamp(
                    max(self.controller.cfg.same_set_at, raw.get("design_set_at", 0)), time.time())
                raw["same_by"] = self.own_id()
            cfg = self.settings_store.save(raw)
        except (SettingsError, TypeError, ValueError) as exc:
            self._say(str(exc), "fault")
            return
        self.controller.apply_settings(cfg)
        if self.previews is not None:
            # The tiles' renderers read the colour from the applied settings, which land only now.
            self.previews.repaint()
        if shared:
            self._send_same()
        elif design_changed:
            self._send_same()
        self._refresh_design_follow()
        if self._write_ways():
            self._say("Saved. Changes apply as you make them.", "ink_2")

    @objc.python_method
    def _write_ways(self):
        """The chosen machine's side and zones from the page's controls (core/ways.py), the engine
        built again from them, and every machine whose way back changed told, along with the chosen
        machine when its side or way changed. False, with the store's sentence said and the controls
        back at what is saved, when the store refuses them."""
        peer = self.chosen_peer
        current = self.settings_store.current()
        if peer not in {entry["id"] for entry in current["peers"]}:
            # None chosen, or one removed while its change waited: nothing of its to write.
            return True
        side = self.edge_select.value or ""
        methods = [name for name in core_ways.KIND_WORDS if self.method_boxes[name].value]
        parts = [name for name, tile in self.part_boxes.items() if tile.value] or ["middle"]
        offer = core_ways.share_offer(current, peer, self._live)
        occupied = {part for part, owner in offer["thirds"].items() if owner in offer["holders"]} if offer else set()
        if offer and side == offer["side"] and (
                "edge" in methods or "part" in methods and occupied.intersection(parts)):
            self._load_ways()
            self._reflect()
            self.window.makeFirstResponder_(self.share_button.view)
            return False
        before = core_ways.way_back_state(current)
        had_way = core_ways.has_way(current, peer)
        try:
            moved = self.settings_store.set_ways(
                peer, side=side, corner=self.corner_select.value,
                methods=methods, parts=parts, live=self._live)
            cfg = self.settings_store.load()
        except SettingsError as exc:
            self._say(str(exc), "fault")
            self._load_ways()
            self._reflect()
            return False
        # Straight onto the controller rather than through apply_settings, which would send the
        # first machine's side a second time when that is the machine chosen.
        self.controller.cfg = cfg
        self.controller.zones_changed()
        if moved:
            # A side moved onto another machine's turns this machine's ways there off: show what was kept.
            self._load_ways()
            self._reflect()
        updated = self.settings_store.current()
        changed_peers = [target for target, state in core_ways.way_back_state(updated).items()
                         if before.get(target) != state]
        if moved or core_ways.has_way(updated, peer) != had_way:
            changed_peers.append(peer)
        for target in dict.fromkeys(changed_peers):
            self._tell(target)
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
        """A machine changed which of its edges faces this Mac (`edge` is its own). Applied here
        rather than at either link, because this is the side that owns the settings file; kept only
        when newer than the side held for that machine, a tie going to the larger `by` (WIRE.md
        section 8). A side that would put two machines' zones over one stretch turns that machine's
        clashing zones off, and says so. `way_back` is kept whatever the side; a message that changed
        anything here is answered with this Mac's own, so that machine learns at once whether a way
        leads back to it, and one that changed nothing is not, so two machines never answer in turn."""
        # A change still waiting out the pause is written first: the page reloads from the
        # settings below, which would otherwise drop it.
        self._flush()
        before = core_ways.way_back_state(self.settings_store.current())
        try:
            changed, notices = self.settings_store.arrangement(peer, edge, set_at, by, way_back, way_back_by,
                                                               live=self._live)
            cfg = self.settings_store.load() if changed else None
        except (SettingsError, TypeError, ValueError):
            self.logger.exception("could not save the arrangement %s sent", peer)
            return
        if not changed:
            return
        self.controller.cfg = cfg
        self.controller.zones_changed()
        for notice in notices:
            self.controller._alert("Beamer", notice)
        self.logger.info("%s's arrangement: this Mac's %s edge", peer, crossing.OPPOSITE[edge])
        self._load(config_to_raw(cfg))
        self.refresh()
        changed_peers = [target for target, state in core_ways.way_back_state(self.settings_store.current()).items()
                         if before.get(target) != state]
        for target in dict.fromkeys([*changed_peers, peer]):
            self._tell(target)

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

        # Names arrive with each link's hello, and the pairings a zone needs with its `paired`.
        self._show_machines()
        self._show_arrangement([name for name, tile in self.method_boxes.items() if tile.value])
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
        # The first machine by name, which the Connection page edits; peer_label follows the
        # machine in question instead.
        peers = peerlist.desktops(controller.book.peers())
        first = peerlist.labels(peers)[peers[0]["token"]] if peers else None
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
            self._shown(f"Crossing to {first or 'the other machine'} while it sleeps sends a wake-up packet and waits for it.")
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
        self.tray.notices.ask()
        info = note.userInfo() or {}
        default = info.get("NSApplicationLaunchIsDefaultLaunchKey")
        if default is not None and not bool(default):
            self.tray.hidden = True


def _tray_item(title, **kwargs):
    return rumps.MenuItem(americanise(title), **kwargs)


def _set_tray_title(item, title):
    item.title = americanise(title)


class TrayApp(rumps.App):
    def __init__(self, controller, settings_store, logger, hidden=False):
        self.hidden = hidden
        self.notices = notices.Notices(logger)
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
        )
        self.control_window.update_checker = self.update_checker
        self._notch_failed = False
        self.header_item = header = _tray_item(f"Beamer {VERSION}", callback=None)
        self.update_url = None
        self.status_item = _tray_item("Starting", callback=None)
        self.toggle_item = _tray_item(TOGGLE_ITEM, callback=self.toggle_redirect)
        # With several machines to send to, one item each after the one above, keyed by machine id.
        self._send_keys = []
        self.pause_item = _tray_item("Pause crossing", callback=self.toggle_pause)
        # One tick per direction, so either can be switched off while the other keeps working.
        self.send_item = _tray_item("This Mac drives other machines", callback=self.toggle_send_to_windows)
        self.receive_item = _tray_item("Other machines drive this Mac", callback=self.toggle_windows_drives)
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
                _tray_item("Settings…", callback=self.open_window),
                _tray_item("Reload configuration", callback=self.reload_config),
                _tray_item("Open log folder", callback=self.open_log_folder),
                rumps.separator,
                _tray_item("About Beamer", callback=self.show_about),
                _tray_item("Quit Beamer", callback=self.quit_app),
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
            _set_tray_title(self.header_item, f"Beamer {found[0]} is available…")
            self.header_item.set_callback(self.open_update)
        else:
            _set_tray_title(self.header_item, f"Beamer {VERSION}")
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

    def _arrangement(self, peer, edge, set_at, by, way_back=None, way_back_by=None):
        """An arrangement from a machine, over either link, applied on the main
        thread -- it writes the settings file and redraws the window."""
        AppHelper.callAfter(self.control_window.apply_arrangement, peer, edge, set_at, by, way_back, way_back_by)

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
            _set_tray_title(self.toggle_item, "Return input to Mac")
        else:
            _set_tray_title(self.toggle_item, pages.redact(
                f"Send input to {controller.peer_label}" if controller.peer_label else "Send input", hide))
        _set_tray_title(self.pause_item, "Resume crossing" if controller.crossing_paused else "Pause crossing")
        self._machine_items()
        self._direction_items()
        _set_tray_title(self.status_item, pages.redact(link_state.describe(controller).word, hide))

    def _machine_items(self):
        """With more than one machine to send to, Send input to each of them in place of the one
        item, and Bring input back on the one input is on; with one, the one item as it always was."""
        controller = self.controller
        wanted = pages.send_items(controller.book.peers(), controller.owner.on if controller.redirecting else None,
                                  controller.cfg.hide_addresses)
        keys = [ident for ident, _title in wanted]
        if keys != self._send_keys:
            for key in self._send_keys:
                del self.menu[key]
            anchor = TOGGLE_ITEM
            for ident in keys:
                # Created under its id, which is its key in the menu, then given its words.
                self.menu.insert_after(anchor, _tray_item(ident, callback=lambda _sender, ident=ident: self._send_to(ident)))
                anchor = ident
            self._send_keys = keys
        for ident, title in wanted:
            _set_tray_title(self.menu[ident], title)
        self.toggle_item.hidden = bool(keys)

    def _send_to(self, peer):
        """One machine's item: input goes to it, straight from the machine it is on (WIRE.md section 5,
        "Moving input"), or home when it is already there."""
        controller = self.controller
        if not controller.input_ready:
            self.notify_user("Beamer", "Grant both Mac permissions first.")
            return
        if controller.redirecting and controller.owner.on == peer:
            controller.set_redirecting(False)
        else:
            controller.set_redirecting(True, peer=peer)
        self.refresh_status(None)

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
        _set_tray_title(self.send_item, send)
        _set_tray_title(self.receive_item, receive)
        desktops = peerlist.desktops(peers)
        send_labels = [pages.redact(label, hide) for label in peerlist.labels(desktops).values()]
        _set_tray_title(self.send_item,
                        f"This Mac drives {send_labels[0]}" if len(send_labels) == 1 else
                        "This Mac drives every machine" if send_labels else "This Mac drives other machines")
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
        if ((change.get("send") is False or change.get("in_use") is False)
                and any(entry.get("id") == controller.owner.on for entry in entries)):
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
            self.notices.post(title, pages.redact(message, self.controller.cfg.hide_addresses))
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
    book, identity = bridge.links_from_store(settings_store, VERSION)
    controller = WakingController(cfg, logger=logger, book=book, identity=identity, hardware=hardware_mac.hardware_address_towards)
    app = TrayApp(controller, settings_store, logger, hidden=args.hidden)
    awake = stay_awake()  # noqa: F841  (held until Beamer quits)
    controller.start()
    try:
        app.run()
    finally:
        controller.stop()
        instance_lock.close()


if __name__ == "__main__":
    main()
