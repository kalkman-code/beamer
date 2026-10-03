"""Live previews for the Design page: the notch and edge animations, played in the settings window by
the very classes that draw them on screen, so a preview cannot drift from the real thing. The crossing
effects' previews at the end of the file do the same through effects_overlay's replay.

Each renderer is hosted in a view rather than a screen-level panel. Only where it draws, and how it
is shown and hidden, is redirected; everything else runs as it does on screen. PreviewFeed stands in
for the controller: the live settings, so a change shows at once, with a scripted push instead of a
trackpad. The script builds pressure, goes through, keeps animating for the chosen time and rests,
on a loop. PreviewLoop plays a notch tile only while the pointer is over it, and only while the Design
page is showing.

Main thread only. A failing preview disables itself as the real renderer does; nothing depends on it.
"""

import time
import types

import AppKit
import Quartz
import objc

import crossing
from core import effects
import effects_overlay
import motion
import notch_beam
import notch_island
import theme

PUSH_S = 1.8
REST_S = 1.0
FALLBACK_NOTCH = (185.0, 32.0)


def preview_frame(elapsed, after):
    """(pressure, phase) `elapsed` seconds into the loop: pressure builds for PUSH_S and goes
    through, the crossing plays for `after` seconds, then everything rests for REST_S and it starts
    again. Phases are push, after and rest."""
    t = elapsed % (PUSH_S + after + REST_S)
    if t < PUSH_S:
        return (t / PUSH_S) ** 1.6, "push"
    return 0.0, "after" if t < PUSH_S + after else "rest"


def notch_size():
    """This Mac's notch as (width, height), or a typical MacBook's when it has none at the top of
    the desktop, so the Notch previews still show what the styles look like."""
    try:
        geometry = notch_island.notch_geometry()
    except Exception:
        geometry = None
    if geometry is None:
        return FALLBACK_NOTCH
    left, right, _top, height = geometry
    return (right - left, height)


class PreviewFeed:
    """What a hosted renderer reads from its controller. `overrides` pins crossing settings for one
    preview, such as the style a tile shows, while the rest follow the live settings."""

    def __init__(self, controller, **overrides):
        self.controller = controller
        self.overrides = overrides
        self.level = 0.0
        self.crossing = types.SimpleNamespace(touching=False)

    @property
    def cfg(self):
        cfg = self.controller.cfg
        if not self.overrides:
            return cfg
        return types.SimpleNamespace(crossing=dict(cfg.crossing, **self.overrides))

    def crossing_pressure_now(self):
        return self.level

    def _current_desktop_bounds(self):
        """A preview draws in its own small scene, not on the desktop, so there are no displays
        to cut its band to."""
        return None


class _Panel:
    """The panel calls the renderers make, answered by a view in the settings window."""

    def __init__(self, view, set_hidden):
        self.view = view
        self.set_hidden = set_hidden

    def contentView(self):
        return self.view

    def backingScaleFactor(self):
        window = self.view.window()
        return window.backingScaleFactor() if window is not None else 2.0

    def setFrame_display_(self, frame, _display):
        self.view.setFrame_(frame)

    def orderFrontRegardless(self):
        self.set_hidden(False)

    def orderOut_(self, _sender):
        self.set_hidden(True)


def hosted_notch(base):
    """NotchBeam or NotchIsland, drawing into a PreviewScreen instead of over the notch."""

    class HostedNotch(base):
        # On the window's screen, not the desktop: the window's palette decides which variant draws.
        dark_appearance = staticmethod(theme.is_dark)

        def __init__(self, feed, logger, screen):
            super().__init__(feed, logger)
            self.screen = screen

        def _show(self):
            if self.panel is None:
                width, height = self.screen.notch
                self._build((0.0, width, 0.0, height))
            self.panel.orderFrontRegardless()
            self.visible = True
            return True

        def _panel(self, frame):
            stage = Quartz.CALayer.layer()
            stage.setBounds_(((0.0, 0.0), (frame.size.width, frame.size.height)))
            stage.setGeometryFlipped_(True)
            stage.setAnchorPoint_((0.5, 1.0))
            self.screen.hold(stage)
            return _Panel(self.screen, stage.setHidden_), stage

    return HostedNotch


def hosted_edge(base):
    """The app's EdgeGlow, drawing its band along the right side of a PreviewScreen."""

    class HostedEdge(base):
        # On the window's screen, not the desktop: the window's palette decides which variant draws.
        dark_appearance = staticmethod(theme.is_dark)

        def __init__(self, feed, logger, screen):
            super().__init__(feed, logger)
            self.screen = screen

        def _ensure_panel(self):
            if self.panel is not None:
                return
            band = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 1, 1))
            band.setWantsLayer_(True)
            self.screen.addSubview_(band)
            self.fill = Quartz.CAGradientLayer.layer()
            band.layer().addSublayer_(self.fill)
            self.comet = Quartz.CAGradientLayer.layer()
            self.panel = _Panel(band, band.setHidden_)

        def _band_frame(self, band):
            bounds = self.screen.bounds()
            return AppKit.NSMakeRect(bounds.size.width - band, 0, band, bounds.size.height)

    return HostedEdge


class PreviewScreen(AppKit.NSView):
    """The little desktop a preview plays on: a wallpaper-toned ground so a black notch tab reads
    against it and, for a notch preview, the notch itself drawn over whatever the renderer puts
    there, as the hardware covers it. The renderer's stage is scaled so the notch takes about 60% of
    the width, and never larger than life."""

    def layout(self):
        objc.super(PreviewScreen, self).layout()
        self.arrange()

    @objc.python_method
    def setup(self, notch=None):
        self.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.setWantsLayer_(True)
        layer = self.layer()
        layer.setCornerRadius_(theme.RADIUS["field"])
        layer.setMasksToBounds_(True)
        layer.setBorderWidth_(1)
        theme.tint(layer, border="rule")
        self.ground = Quartz.CAGradientLayer.layer()
        theme.tint(self.ground, colours=["edge", "well"])
        self.ground.setStartPoint_((0.5, 1.0))
        self.ground.setEndPoint_((0.5, 0.0))
        layer.addSublayer_(self.ground)
        self.notch = notch
        self.stage = None
        self.cutout = None
        if notch is not None:
            self.cutout = Quartz.CAShapeLayer.layer()
            self.cutout.setFillColor_(AppKit.NSColor.blackColor().CGColor())
            layer.addSublayer_(self.cutout)
        return self

    @objc.python_method
    def hold(self, stage):
        if self.stage is not None:
            self.stage.removeFromSuperlayer()
        self.stage = stage
        if self.cutout is not None:
            self.layer().insertSublayer_below_(stage, self.cutout)
        else:
            self.layer().addSublayer_(stage)
        self.arrange()

    @objc.python_method
    def arrange(self):
        if getattr(self, "ground", None) is None:
            return
        bounds = self.bounds()
        width, height = bounds.size.width, bounds.size.height
        scale = min(1.0, width * 0.6 / self.notch[0]) if self.notch is not None else 1.0
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        try:
            self.ground.setFrame_(bounds)
            if self.stage is not None:
                self.stage.setPosition_((width / 2.0, height))
                self.stage.setAffineTransform_(Quartz.CGAffineTransformMakeScale(scale, scale))
            if self.cutout is not None:
                notch_width, notch_height = self.notch[0] * scale, self.notch[1] * scale
                radius = 7.0 * scale
                # Taller than the notch by its radius, so only the bottom corners show rounded.
                rect = ((width / 2.0 - notch_width / 2.0, height - notch_height), (notch_width, notch_height + radius))
                self.cutout.setPath_(Quartz.CGPathCreateWithRoundedRect(rect, radius, radius, None))
        finally:
            Quartz.CATransaction.commit()


FRAME_RATE = 60
HOVER_INTENT_S = 0.1


class _Fire(AppKit.NSObject):
    def fire_(self, _sender):
        self.callback()


def on_screen(view):
    """Whether any of `view` can be seen: in a window on screen and not covered, not hidden, and not
    scrolled out of its page."""
    window = view.window()
    if window is None or not window.isVisible() or view.isHiddenOrHasHiddenAncestor():
        return False
    if not window.occlusionState() & AppKit.NSWindowOcclusionStateVisible:
        return False
    visible = view.visibleRect()
    return visible.size.width > 0 and visible.size.height > 0


class FrameClock:
    """Calls `callback()` once a frame, timed to `view`'s display at up to FRAME_RATE where macOS
    offers a view's display link (14 and later), else by a timer. Either runs in the common run loop
    modes, as a default-mode timer, rumps.Timer's, stops dead while the page scrolls or a slider is
    dragged and then jumps."""

    def __init__(self, view, callback):
        self.view = view
        self.callback = callback
        self.source = None

    def start(self):
        if self.source is not None:
            return
        target = _Fire.alloc().init()
        target.callback = self.callback
        runloop = AppKit.NSRunLoop.mainRunLoop()
        if self.view.window() is not None and hasattr(self.view, "displayLinkWithTarget_selector_"):
            link = self.view.displayLinkWithTarget_selector_(target, "fire:")
            # 60 on a 120Hz panel lands every other refresh, evenly, where a timer beats against it.
            link.setPreferredFrameRateRange_(Quartz.CAFrameRateRangeMake(30, FRAME_RATE, FRAME_RATE))
            link.addToRunLoop_forMode_(runloop, AppKit.NSRunLoopCommonModes)
            self.source = link
        else:
            timer = AppKit.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0 / FRAME_RATE, target, "fire:", None, True
            )
            runloop.addTimer_forMode_(timer, AppKit.NSRunLoopCommonModes)
            self.source = timer

    def stop(self):
        source, self.source = self.source, None
        if source is not None:
            source.invalidate()

    def is_alive(self):
        return self.source is not None


class PreviewLoop:
    """Plays the page's hosted renderers from one clock: the tile the pointer is over, else the
    tiles set `resting`. Every other tile holds a still, the push part way in, and a tile that starts
    playing goes on from that push rather than from nothing."""

    HOLD = 0.7

    def __init__(self, controller, host=None):
        """`host` is a view on screen whenever the loop runs, the page itself, to time it by: a
        view's display link stops while the view is hidden, as a tile's is in the other mode."""
        self.controller = controller
        self.players = []
        self.running = False
        self.resting = ()
        self.hovered = None
        self.hovered_at = 0.0
        self.clock = None if host is None else FrameClock(host, self._tick)

    def add(self, feed, renderer, update, screen):
        """`update(kind, pressure)` passes an event to `renderer` the way the app's crossing
        feedback would. The renderer's own timer is swapped for a FrameClock on `screen`."""
        renderer.timer = FrameClock(screen, lambda: renderer._tick(None))
        self.players.append(types.SimpleNamespace(feed=feed, renderer=renderer, update=update, screen=screen,
                                                  started_at=None, phase=None, quarter=0))
        if self.clock is None:
            self.clock = FrameClock(screen, self._tick)

    def owns(self, view):
        return any(player.screen is view for player in self.players)

    def set_resting(self, screens):
        self.resting = tuple(screens)

    def hover(self, view):
        """`view` is the preview of the tile under the pointer, one of this loop's or not, or None."""
        self.hovered = view
        self.hovered_at = time.monotonic()

    def start(self):
        if self.running:
            return
        self.running = True
        self.repaint()
        if self.clock is not None:
            self.clock.start()

    def stop(self):
        if not self.running:
            return
        self.running = False
        if self.clock is not None:
            self.clock.stop()
        for player in self.players:
            player.started_at = None
            player.feed.level = 0.0
            player.feed.crossing.touching = False
            try:
                player.renderer._hide()
                player.renderer.timer.stop()
            except Exception:
                pass

    def repaint(self):
        """Draws every tile that is not playing again, as a colour or palette change needs."""
        for player in self.players:
            if player.started_at is None:
                self._hold(player)

    def _wanted(self, player):
        # A pointer only passing over, or any under Reduce Motion, leaves the resting tile playing.
        if (self.hovered is not None and time.monotonic() - self.hovered_at >= HOVER_INTENT_S
                and not effects_overlay.reduce_motion()):
            return player.screen is self.hovered
        return player.screen in self.resting

    def _hold(self, player):
        feed, renderer = player.feed, player.renderer
        feed.level = self.HOLD
        feed.crossing.touching = True
        try:
            # Left mid-breakthrough, a renderer would freeze its flash, halo and run-off into the
            # still and snap to the end of the crossing when it next played.
            for name, value in (("cross_at", None), ("halo_at", None), ("flash_at", None), ("flare", 0.0),
                                ("kick", 0.0), ("held", 0.0), ("finish", 0.0)):
                if hasattr(renderer, name):
                    setattr(renderer, name, value)
            player.update("pressure", self.HOLD)
            # Drawn once and left: a moving light part way along, at full strength rather than
            # easing up to it from nothing.
            if hasattr(renderer, "finish"):
                renderer.centre = 0.5
            if hasattr(renderer, "cross_travel"):
                renderer.position = notch_beam.LAP * 0.3
                renderer.strength = self.HOLD
            if hasattr(renderer, "drawn_at"):
                renderer.drawn_at = None
            renderer._draw()
            renderer.timer.stop()
        except Exception:
            pass

    def _tick(self):
        if not self.running:
            return
        now = time.monotonic()
        after = self.controller.cfg.crossing["notch_after_ms"] / 1000.0
        for player in self.players:
            playing = self._wanted(player) and on_screen(player.screen)
            if not playing:
                if player.started_at is not None:
                    player.started_at = None
                    motion.cross_fade(player.screen)
                    self._hold(player)
                continue
            if player.started_at is None:
                # Where the script's push reaches the held still's pressure.
                player.started_at = now - PUSH_S * self.HOLD ** (1 / 1.6)
                player.phase, player.quarter = "push", min(3, int(4 * self.HOLD))
            level, phase = preview_frame(now - player.started_at, after)
            quarter = min(3, int(4 * level)) if phase == "push" else 0
            crossed = phase == "after" and player.phase == "push"
            ticked = quarter > player.quarter
            player.phase, player.quarter = phase, quarter
            player.feed.level = level
            player.feed.crossing.touching = phase == "push"
            if crossed:
                player.update("cross", 1.0)
            elif phase == "push" and level > 0.0:
                player.update("tick" if ticked else "pressure", level)


class TileStack(AppKit.NSView):
    """A tile's picture that is one of several views laid over each other, only one shown: Glow and
    Beam's still, or at the notch, where the notch style plays in their place, that style's own
    hosted preview."""

    @objc.python_method
    def setup(self, children, height=84):
        self.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.heightAnchor().constraintEqualToConstant_(height).setActive_(True)
        self.children = list(children)
        for child in self.children:
            child.setTranslatesAutoresizingMaskIntoConstraints_(False)
            self.addSubview_(child)
            for a, b in ((child.leadingAnchor(), self.leadingAnchor()), (child.trailingAnchor(), self.trailingAnchor()),
                         (child.topAnchor(), self.topAnchor()), (child.bottomAnchor(), self.bottomAnchor())):
                a.constraintEqualToAnchor_(b).setActive_(True)
        self.shown = None
        self.show(self.children[0])
        return self

    @objc.python_method
    def show(self, child):
        if child is self.shown:
            return
        if self.shown is not None:
            motion.cross_fade(self)
        self.shown = child
        for other in self.children:
            other.setHidden_(other is not child)

    @objc.python_method
    def current(self):
        return self.shown


class _HoverOwner(AppKit.NSObject):
    def mouseEntered_(self, _event):
        self.entered()

    def mouseExited_(self, _event):
        self.exited()


class TileHover:
    """The one tile under the pointer plays its animation and settles back to its still when the
    pointer leaves; under Reduce Motion it keeps its still. A crossing effect's still plays on from
    the frame it shows, so the tile starts without a jump."""

    def __init__(self, loop):
        self.loop = loop
        self.owners = []
        self.current = None
        # What plays for the tile under the pointer: its preview, or the one a TileStack shows.
        self.target = None
        self.clock = None

    def track(self, tile, preview):
        owner = _HoverOwner.alloc().init()
        owner.entered = lambda: self.enter(preview)
        owner.exited = lambda: self.leave(preview)
        options = (AppKit.NSTrackingMouseEnteredAndExited | AppKit.NSTrackingActiveInActiveApp
                   | AppKit.NSTrackingInVisibleRect)
        tile.addTrackingArea_(AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            AppKit.NSZeroRect, options, owner, None))
        # A tracking area does not keep its owner.
        self.owners.append(owner)

    def enter(self, preview):
        if self.current is preview:
            return
        self.stop()
        self.current = preview
        target = self.target = preview.current() if isinstance(preview, TileStack) else preview
        self.loop.hover(target)
        if self.loop.owns(target) or effects_overlay.reduce_motion():
            return
        # A pointer passing over on its way elsewhere leaves the tiles it crosses still.
        target.playing_since = time.monotonic() + HOVER_INTENT_S
        self.clock = FrameClock(target, lambda: target.setNeedsDisplay_(True))
        self.clock.start()

    def leave(self, preview):
        if preview is self.current:
            self.stop()

    def stop(self):
        preview, self.current, self.target = self.target, None, None
        self.loop.hover(None)
        if self.clock is not None:
            self.clock.stop()
            self.clock = None
        if preview is not None and getattr(preview, "playing_since", None) is not None:
            if time.monotonic() > preview.playing_since:
                motion.cross_fade(preview)
            preview.playing_since = None
            preview.setNeedsDisplay_(True)


def edge_update(renderer):
    step = crossing.Step
    return lambda kind, level: renderer.update(kind, step(pressure=level, mac_edge="right", region=(0, 0, 1, 1)))


def notch_update(renderer):
    return lambda kind, _level: renderer.update(kind)


# The crossing effects' previews: effects.preview_scene drawn by effects_overlay.draw_scene, the same
# replay the overlay uses on screen. Every tile shows one still frame at the place the page's Show at
# chooses, and plays on from it while TileHover says the pointer is over it; nothing else animates.
# The still is part way into the push, where every style shows.
STILL_AT_S = 1.7
# The part of the scene a tile shows at each place, centred on where the pointer pushes so the
# departure fills the tile: the Mac's right-hand side, the gap and the PC's left edge; the Mac's
# top right corner with room above it; the notch and the Mac's screen under it, where the forms play.
STILL_CROPS = {"edge": (620.0, 142.0, 240.0, 120.0), "corner": (680.0, -50.0, 240.0, 120.0),
               "notch": (220.0, 470.0, 360.0, 180.0)}
STAGE_METHODS = (("edge", "Edge"), ("corner", "Corner"), ("notch", "Notch"))
SWITCH_STILL_CROP = (248.0, 138.0, 240.0, 120.0)
SWITCH_STILL_AT_S = 0.6


def _played(view, still_at):
    """The time into its loop a tile draws: its still, or on from it while it plays."""
    since = view.playing_since
    return still_at if since is None else still_at + max(0.0, time.monotonic() - since)


def effect_still(effect_id, colour, logger, height=84, place=lambda: "edge", pace=lambda: 1.0,
                 effect_size=lambda: "medium"):
    """A tile's still frame of `effect_id`, in the selected colour, place, length and size."""
    view = effects_overlay.EffectsCanvas.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 1, 1))
    view.setTranslatesAutoresizingMaskIntoConstraints_(False)
    view.heightAnchor().constraintEqualToConstant_(height).setActive_(True)
    view.setWantsLayer_(True)
    view.layer().setCornerRadius_(theme.RADIUS["field"])
    view.layer().setMasksToBounds_(True)
    view.layer().setBorderWidth_(1)
    theme.tint(view.layer(), border="rule")
    view.logger = logger
    view.playing_since = None

    def paint(ctx, width, height):
        fx = effects.preview_effect(effect_id)
        dark = theme.is_dark()
        where = place()
        scene = effects.preview_scene(fx, _played(view, STILL_AT_S), where, effects_overlay.palette(colour()), dark=dark,
                                      pace=pace(), effect_size=effect_size())
        # A small pointer, so the effect rather than the arrow is what a tile shows.
        effects_overlay.draw_scene(Quartz, ctx, scene, width, height, where, dark, crop=STILL_CROPS[where], pointer=11.0)

    view.painter = paint
    return view


def switch_still(fx, colour, logger, height=84, pace=lambda: 1.0, effect_size=lambda: "medium"):
    """A tile's still frame of what a switch plays: `fx()` is the Effect, asked when it draws, since
    Same as crossing follows the crossing style."""
    view = effect_still(None, colour, logger, height)

    def paint(ctx, width, height):
        dark = theme.is_dark()
        scene = effects.preview_switch_scene(fx(), _played(view, SWITCH_STILL_AT_S), effects_overlay.palette(colour()),
                                             dark=dark, pace=pace(), effect_size=effect_size())
        effects_overlay.draw_scene(Quartz, ctx, scene, width, height, "switch", dark, crop=SWITCH_STILL_CROP, pointer=11.0)

    view.painter = paint
    return view
