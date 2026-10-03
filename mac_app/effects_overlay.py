"""The crossing effects on the Mac: effects.py's recorded Pens replayed into Core Graphics, over the
desktop while a crossing plays and in the Design page's previews.

`replay` is the whole port of the Pen to Quartz, and takes the Quartz module as an argument so the
tests can hand it a recording fake. `EffectsOverlay` owns one effects.Player, fed the same events
today's glow is fed, and shows its frames in click-through panels at 60Hz only while something is
playing. It keeps one panel per display the frame touches, because a window spanning two displays
shows on only one of them while displays have separate Spaces, which is macOS's default.

Main thread only. Any failure logs once with the effect's id and turns the effects off for the run;
the app then falls back to today's glow, which never depended on this.
"""

import math
import time

import AppKit
import Quartz
import objc
import rumps

import crossing
import desktop_mac
from core import effects
import notch_beam
import notch_island

# Canvas's line caps and joins, as Core Graphics names them.
_CAPS = {"butt": "kCGLineCapButt", "round": "kCGLineCapRound", "square": "kCGLineCapSquare"}
_JOINS = {"miter": "kCGLineJoinMiter", "round": "kCGLineJoinRound", "bevel": "kCGLineJoinBevel"}
CONIC_WEDGES = 72
NOTCH_RADIUS = 10.0


def replay(cg, ctx, pens):
    """Draws `pens` into `ctx` in their own coordinates, each inside its display when it names one.
    Each op runs inside its own saved graphics state, so its alpha, blend mode and clip end with
    it. At most one CGGradient is built per Gradient per call."""
    space = cg.CGColorSpaceCreateWithName(cg.kCGColorSpaceSRGB)
    gradients = {}
    for pen in pens:
        clip = getattr(pen, "clip", None)
        if clip is not None:
            cg.CGContextSaveGState(ctx)
            cg.CGContextClipToRect(ctx, ((clip[0], clip[1]), (clip[2] - clip[0], clip[3] - clip[1])))
        try:
            for op in pen.ops:
                _replay_op(cg, ctx, op, space, gradients)
        finally:
            if clip is not None:
                cg.CGContextRestoreGState(ctx)


def _replay_op(cg, ctx, op, space, gradients):
    kind, path, paint, alpha, composite = op[:5]
    cg.CGContextSaveGState(ctx)
    try:
        cg.CGContextSetAlpha(ctx, alpha)
        cg.CGContextSetBlendMode(ctx, cg.kCGBlendModePlusLighter if composite == "lighter" else cg.kCGBlendModeNormal)
        _add_path(cg, ctx, path)
        gradient = isinstance(paint, effects.Gradient)
        if kind == "stroke":
            width, cap, join = op[5:8]
            cg.CGContextSetLineWidth(ctx, width)
            cg.CGContextSetLineCap(ctx, getattr(cg, _CAPS.get(cap, "kCGLineCapButt")))
            cg.CGContextSetLineJoin(ctx, getattr(cg, _JOINS.get(join, "kCGLineJoinMiter")))
            if gradient:
                cg.CGContextReplacePathWithStrokedPath(ctx)
                cg.CGContextClip(ctx)
                _draw_gradient(cg, ctx, paint, space, gradients)
            else:
                cg.CGContextSetRGBStrokeColor(ctx, *paint)
                cg.CGContextStrokePath(ctx)
        else:
            evenodd = op[5] == "evenodd"
            if gradient:
                (cg.CGContextEOClip if evenodd else cg.CGContextClip)(ctx)
                _draw_gradient(cg, ctx, paint, space, gradients)
            else:
                cg.CGContextSetRGBFillColor(ctx, *paint)
                (cg.CGContextEOFillPath if evenodd else cg.CGContextFillPath)(ctx)
    finally:
        cg.CGContextRestoreGState(ctx)


def _add_path(cg, ctx, path):
    cg.CGContextBeginPath(ctx)
    for segment in path:
        verb = segment[0]
        if verb == "M":
            cg.CGContextMoveToPoint(ctx, segment[1], segment[2])
        elif verb == "L":
            cg.CGContextAddLineToPoint(ctx, segment[1], segment[2])
        elif verb == "Q":
            cg.CGContextAddQuadCurveToPoint(ctx, *segment[1:5])
        elif verb == "C":
            cg.CGContextAddCurveToPoint(ctx, *segment[1:7])
        elif verb == "Z":
            cg.CGContextClosePath(ctx)


def _stops(paint):
    """Canvas sorts stops by offset, keeping insertion order among equals; one stop is a flat
    colour, which CGGradient needs as two."""
    stops = sorted(paint.stops, key=lambda stop: stop[0])
    if len(stops) == 1:
        stops = [(0.0, stops[0][1]), (1.0, stops[0][1])]
    return stops


def _draw_gradient(cg, ctx, paint, space, gradients):
    stops = _stops(paint)
    if not stops:
        return
    held = gradients.get(id(paint))
    if held is None:
        components = [value for _offset, rgba in stops for value in rgba]
        held = gradients[id(paint)] = (
            paint,
            cg.CGGradientCreateWithColorComponents(space, components, [offset for offset, _rgba in stops], len(stops)),
        )
    made = held[1]
    extend = cg.kCGGradientDrawsBeforeStartLocation | cg.kCGGradientDrawsAfterEndLocation
    g = paint.geometry
    if paint.kind == "linear":
        cg.CGContextDrawLinearGradient(ctx, made, (g[0], g[1]), (g[2], g[3]), extend)
    elif paint.kind == "radial":
        cg.CGContextDrawRadialGradient(ctx, made, (g[0], g[1]), g[2], (g[3], g[4]), g[5], extend)
    elif hasattr(cg, "CGContextDrawConicGradient"):
        # In a flipped context this starts at the angle and turns clockwise on screen, as canvas does.
        cg.CGContextDrawConicGradient(ctx, made, (g[1], g[2]), g[0])
    else:
        _conic_wedges(cg, ctx, g, stops)


def _colour_at(stops, offset):
    if offset <= stops[0][0]:
        return stops[0][1]
    for (o0, c0), (o1, c1) in zip(stops, stops[1:]):
        if offset <= o1:
            u = 0.0 if o1 <= o0 else (offset - o0) / (o1 - o0)
            return tuple(a + (b - a) * u for a, b in zip(c0, c1))
    return stops[-1][1]


def _conic_wedges(cg, ctx, geometry, stops):
    """A conic gradient on a macOS without CGContextDrawConicGradient: filled wedges out to the
    clip's farthest corner, each the colour at its middle."""
    start, cx, cy = geometry
    box = cg.CGContextGetClipBoundingBox(ctx)
    (x, y), (w, h) = box
    reach = max(math.hypot(px - cx, py - cy) for px in (x, x + w) for py in (y, y + h)) + 2.0
    step = effects.TAU / CONIC_WEDGES
    # The wedges overlap so no seam shows, and are copied, not blended, inside a layer that takes
    # the op's alpha and blend mode as a whole: blended, every overlap doubles a translucent colour
    # into a spoke.
    cg.CGContextBeginTransparencyLayer(ctx, None)
    cg.CGContextSetAlpha(ctx, 1.0)
    cg.CGContextSetBlendMode(ctx, cg.kCGBlendModeCopy)
    for index in range(CONIC_WEDGES):
        a0 = start + index * step
        a1 = a0 + step * 1.02
        cg.CGContextSetRGBFillColor(ctx, *_colour_at(stops, (index + 0.5) / CONIC_WEDGES))
        cg.CGContextBeginPath(ctx)
        cg.CGContextMoveToPoint(ctx, cx, cy)
        cg.CGContextAddLineToPoint(ctx, cx + reach * math.cos(a0), cy + reach * math.sin(a0))
        cg.CGContextAddLineToPoint(ctx, cx + reach * math.cos(a1), cy + reach * math.sin(a1))
        cg.CGContextClosePath(ctx)
        cg.CGContextFillPath(ctx)
    cg.CGContextEndTransparencyLayer(ctx)


def palette(name):
    """The colours of a glow_colour setting, today's palettes and the effects' packs alike."""
    return notch_beam.palette_values(name)


def style_is_effect(style):
    return style in effects.EFFECT_IDS


def reduce_motion():
    try:
        return bool(AppKit.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
    except Exception:
        return False


dark_appearance = notch_beam.dark_appearance


def corner_name(region, box):
    """Which corner of the desktop a corner region's box sits in, as crossing.CORNERS names them."""
    x, y, w, h = region
    left, top, right, bottom = box
    vertical = "top" if y - top <= bottom - (y + h) else "bottom"
    horizontal = "left" if x - left <= right - (x + w) else "right"
    return f"{vertical}_{horizontal}"


def region_of(via, mac_edge, region, box):
    """effects.Player's region dict for an engine strip, relative to the desktop's top-left."""
    x, y, w, h = region
    found = {"kind": via or "edge", "edge": mac_edge, "x": x - box[0], "y": y - box[1], "w": w, "h": h}
    if via == "corner":
        found["corner"] = corner_name(region, box)
    return found


# The screens in a preview, drawn the way the settings window draws everything else.
_SCREEN_DARK = ("#2a2b26", "#161714")
_SCREEN_LIGHT = ("#d9d8cf", "#b9b8ae")


def _rgb(hex_value, alpha=1.0):
    value = hex_value.lstrip("#")
    return tuple(int(value[index:index + 2], 16) / 255.0 for index in (0, 2, 4)) + (alpha,)


def draw_scene(cg, ctx, scene, width, height, method, dark, crop=None, pointer=18.0):
    """One effects.preview_scene frame fitted into `width` x `height`: the screens, the pens, the
    pointer (`pointer` points tall however far the scene is scaled), then the notch. `crop`
    (x, y, w, h) shows only that part of the scene."""
    scene_w, scene_h = effects.preview_size(method) if crop is None else crop[2:]
    ox, oy = (0.0, 0.0) if crop is None else crop[:2]
    scale = min(width / scene_w, height / scene_h)
    cg.CGContextSaveGState(ctx)
    try:
        cg.CGContextTranslateCTM(ctx, (width - scene_w * scale) / 2.0, (height - scene_h * scale) / 2.0)
        cg.CGContextScaleCTM(ctx, scale, scale)
        cg.CGContextTranslateCTM(ctx, -ox, -oy)
        cg.CGContextClipToRect(ctx, ((ox, oy), (scene_w, scene_h)))
        top, bottom = _SCREEN_DARK if dark else _SCREEN_LIGHT
        space = cg.CGColorSpaceCreateWithName(cg.kCGColorSpaceSRGB)
        ground = cg.CGGradientCreateWithColorComponents(space, list(_rgb(top) + _rgb(bottom)), [0.0, 1.0], 2)
        for x, y, w, h, _os in scene["screens"]:
            cg.CGContextSaveGState(ctx)
            path = cg.CGPathCreateWithRoundedRect(((x, y), (w, h)), 14.0, 14.0, None)
            cg.CGContextAddPath(ctx, path)
            cg.CGContextClip(ctx)
            cg.CGContextDrawLinearGradient(ctx, ground, (x, y), (x, y + h), 0)
            cg.CGContextRestoreGState(ctx)
        replay(cg, ctx, scene["pens"])
        if scene["pointer"] is not None:
            _pointer(cg, ctx, scene["pointer"][0], scene["pointer"][1], pointer / scale)
        if scene["notch"] is not None:
            x, y, w, h, r = scene["notch"]
            cg.CGContextSetRGBFillColor(ctx, 0.0, 0.0, 0.0, 1.0)
            cg.CGContextAddPath(ctx, cg.CGPathCreateWithRoundedRect(((x, y - r), (w, h + r)), r, r, None))
            cg.CGContextFillPath(ctx)
    finally:
        cg.CGContextRestoreGState(ctx)


def _pointer(cg, ctx, x, y, size):
    """The arrow pointer, `size` tall in the scene's own units."""
    unit = size / 18.0
    shape = ((0.0, 0.0), (0.0, 1.0), (0.26, 0.76), (0.44, 1.12), (0.56, 1.06), (0.39, 0.7), (0.72, 0.7))
    cg.CGContextBeginPath(ctx)
    for index, (px, py) in enumerate(shape):
        (cg.CGContextMoveToPoint if index == 0 else cg.CGContextAddLineToPoint)(ctx, x + px * size, y + py * size)
    cg.CGContextClosePath(ctx)
    cg.CGContextSetRGBFillColor(ctx, 1.0, 1.0, 1.0, 1.0)
    cg.CGContextSetRGBStrokeColor(ctx, 0.0, 0.0, 0.0, 1.0)
    cg.CGContextSetLineWidth(ctx, 1.2 * unit)
    cg.CGContextSetLineJoin(ctx, cg.kCGLineJoinRound)
    cg.CGContextDrawPath(ctx, cg.kCGPathFillStroke)


class EffectsCanvas(AppKit.NSView):
    """A flipped view that draws through a Python callable, `painter(ctx, width, height)`. A failure
    is logged to `logger` here, where its traceback is, and then reported to `on_error` from the
    main queue rather than raised inside AppKit's drawing."""

    def isFlipped(self):
        return True

    def drawRect_(self, _rect):
        painter = getattr(self, "painter", None)
        if painter is None:
            return
        try:
            ctx = AppKit.NSGraphicsContext.currentContext().CGContext()
            bounds = self.bounds()
            painter(ctx, bounds.size.width, bounds.size.height)
        except Exception:
            self.painter = None
            logger = getattr(self, "logger", None)
            if logger is not None:
                logger.exception("drawing a crossing effect failed")
            on_error = getattr(self, "on_error", None)
            if on_error is not None:
                AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(on_error)


class EffectsOverlay:
    """The chosen effect over the desktop, fed by the app: `departure` with the Mac's own crossing
    events, `return_push` with the PC-driven return edge's pressure, `arrival` whenever a
    crossing lands the pointer on this Mac, and `switched` when input arrives any other way.
    Coordinates in are Quartz global points."""

    FRAME_S = 1 / 60
    PAD = 2.0

    def __init__(self, controller, logger):
        self.controller = controller
        self.logger = logger
        self.player = effects.Player()
        self.disabled = False
        self.fx = None
        self.palette = None
        self.reduced = False
        self.dark = True
        self.notch = None
        self.box = None
        self.displays = []
        self.pressure_now = None
        self.panels = {}
        self.timer = rumps.Timer(self._tick, self.FRAME_S)
        # Every module up front: a bundle without them (build 329) turns the effects off here, once,
        # and today's glow draws everything, instead of the first crossing finding out.
        try:
            effects._load()
        except Exception:
            self._fail()

    def wanted(self, feel):
        return not self.disabled and bool(feel["glow"]) and style_is_effect(feel["glow_style"])

    def wanted_switch(self, feel):
        """Whether a switch into this Mac shows where the pointer is: any style, Glow and Beam by
        the locator, while crossings are animated at all and the switch for it is on."""
        return not self.disabled and bool(feel["glow"]) and bool(feel.get("shortcut_arrival", True))

    def departure(self, kind, step):
        """One of the Mac engine's pressure, tick or cross events, as EdgeGlow gets them."""
        self._guard(self._departure, kind, step)

    def _departure(self, kind, step):
        if step.region is None or kind not in ("pressure", "tick", "cross"):
            return
        now = time.monotonic()
        self._begin()
        # The notch as the engine measured it can disagree with the geometry read at the start; an
        # effect told it is at the notch with no notch to draw against plays it as the top edge.
        via = step.via or "edge"
        if via == "notch" and self.notch is None:
            via = "edge"
        region = region_of(via, step.mac_edge, step.region, self.box)
        at = _centre(region) if step.pin is None else (step.pin[0] - self.box[0], step.pin[1] - self.box[1])
        display, at = self._display(at)
        if kind == "cross":
            depart = self.player.depart
            if depart is None or depart["crossed_at"] is not None:
                # Crossed on its first event: the push starts and gives at once, where it gave.
                self.player.push(now, via, region, at, 1.0, display=display)
            self.player.cross(now)
        else:
            tick = min(3, int(step.pressure * 4)) if step.tick else None
            self.player.push(now, via, region, at, step.pressure, tick, display=display)
            self.pressure_now = self.controller.crossing_pressure_now
        self._run()

    def return_push(self, edge, pressure, crossed, cursor, corner=None):
        """The PC is driving this Mac and pushing at the way home, through `edge` or into `corner`.
        The receiver reports pressure per movement, not between them, so it drains here at the
        engine's own rate."""
        self._guard(self._return_push, edge, pressure, crossed, cursor, corner)

    def _return_push(self, edge, pressure, crossed, cursor, corner=None):
        now = time.monotonic()
        self._begin()
        bounds = display_at(cursor, self.box)
        if corner is not None:
            region = dict(region_of("corner", edge, corner_box(corner, bounds), self.box), corner=corner)
        else:
            region = region_of("edge", edge, crossing.CrossingEngine._strip(edge, bounds), self.box)
        via = region["kind"]
        display, at = self._display((cursor[0] - self.box[0], cursor[1] - self.box[1]))
        if crossed:
            depart = self.player.depart
            if depart is None or depart["crossed_at"] is not None:
                self.player.push(now, via, region, at, 1.0, display=display)
            self.player.cross(now)
        else:
            self.player.push(now, via, region, at, pressure, display=display)
            level, since = float(pressure), now
            self.pressure_now = lambda: max(0.0, level - (time.monotonic() - since) / crossing.DECAY_S)
        self._run()

    def arrival(self, method, x, y, edge):
        self._guard(self._arrival, method, x, y, edge)

    def _arrival(self, method, x, y, edge):
        self._begin()
        display, at = self._display((x - self.box[0], y - self.box[1]))
        self.player.arrive(time.monotonic(), method, at, edge, display)
        self._run()

    def crossed_in(self):
        """A crossing landed here that today's glow draws, not this overlay: a switch waiting to
        play gives way to it."""
        self._guard(lambda: self.player.crossed_in(time.monotonic()))

    def switched(self, x, y):
        """Input came to this Mac without a crossing and the pointer is at (x, y)."""
        self._guard(self._switched, x, y)

    def _switched(self, x, y):
        self._begin()
        display, at = self._display((x - self.box[0], y - self.box[1]))
        edge = effects.nearest_edge(at[0] - display[0], at[1] - display[1], display[2], display[3])
        feel = self.controller.cfg.crossing
        fx = effects.switch_effect(feel.get("shortcut_arrival_style", "match"), feel["glow_style"])
        self.player.switched(time.monotonic(), at, edge, fx, display)
        self._run()

    def _begin(self):
        """Settles what plays when nothing is playing yet: the effect, its colours, reduced motion,
        the appearance and the notch are read as an effect starts, never per frame."""
        if self.player.busy and self.fx is not None:
            return
        feel = self.controller.cfg.crossing
        # Glow and Beam are drawn elsewhere and have no arrival: all this overlay plays for them is
        # a switch, by the locator.
        style = feel["glow_style"]
        self.fx = effects.effect(style) if style_is_effect(style) else effects.LOCATOR
        if self.fx is None:
            raise ValueError(f"no crossing effect called {feel['glow_style']!r}")
        self.palette = palette(feel["glow_colour"])
        self.player.pace = effects.pace(feel.get("effect_length"))
        self.player.effect_size = feel.get("effect_size", "medium")
        self.reduced = reduce_motion()
        self.dark = dark_appearance()
        self.box = tuple(self.controller._current_desktop_bounds())
        self.displays = [(r.x - self.box[0], r.y - self.box[1], r.width, r.height) for r in desktop_mac.monitors()]
        self.notch = self._notch()

    def _display(self, at):
        """The display `at` is on, as (x, y, w, h) relative to the desktop box, and `at` itself kept
        inside it: a point in the box that no display covers, as an arrival along a staggered
        edge can be, is where macOS puts the pointer, on the nearest display."""
        return effects.display_for(at, self.displays or [(0.0, 0.0, self.box[2] - self.box[0], self.box[3] - self.box[1])])

    def _notch(self):
        geometry = notch_island.notch_geometry()
        if geometry is None:
            return None
        left, right, top, height = geometry
        primary_height = AppKit.NSScreen.screens()[0].frame().size.height
        return {"x": left - self.box[0], "y": primary_height - top - self.box[1], "w": right - left, "h": height,
                "r": NOTCH_RADIUS}

    def _run(self):
        """Events only move the Player on; the timer draws. Drawing here would put a frame on the
        main queue for every movement event, and at a 1000Hz mouse the ticks and the thud queue
        behind them."""
        if not getattr(self.timer, "is_alive", lambda: False)():
            self.timer.start()

    def _tick(self, _timer):
        self._guard(self._draw)

    def _draw(self):
        now = time.monotonic()
        depart = self.player.depart
        if depart is not None and depart["crossed_at"] is None and self.pressure_now is not None:
            level = self.pressure_now()
            self.player.push(now, depart["method"], depart["region"], depart["at"], level)
            if level <= 0.0:
                self.player.release()
        pens = []
        if self.fx is not None:
            w, h = self.box[2] - self.box[0], self.box[3] - self.box[1]
            pens = self.player.frames(now, self.fx, (w, h, "mac"), self.palette, self.reduced, self.dark, self.notch)
        if not pens:
            self._hide()
            if not self.player.busy:
                self.timer.stop()
                self.fx = None
                self.pressure_now = None
            return
        self._show(pens)

    def _show(self, pens):
        boxes = [padded(pen, self.PAD) for pen in pens]
        x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
        x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
        screens = AppKit.NSScreen.screens()
        primary_height = screens[0].frame().size.height
        shown = set()
        for index, screen in enumerate(screens):
            frame = screen.frame()
            # This display in the effect's space: Quartz global, then relative to the desktop box.
            sx = frame.origin.x - self.box[0]
            sy = primary_height - (frame.origin.y + frame.size.height) - self.box[1]
            left, top = max(x0, sx), max(y0, sy)
            right, bottom = min(x1, sx + frame.size.width), min(y1, sy + frame.size.height)
            if right - left < 1.0 or bottom - top < 1.0:
                continue
            panel, view = self._panel(index)
            origin = (left, top)
            panel.setFrame_display_(AppKit.NSMakeRect(
                left + self.box[0], primary_height - (top + self.box[1]) - (bottom - top), right - left, bottom - top,
            ), False)
            view.painter = lambda ctx, _w, _h, origin=origin: self._paint(ctx, pens, origin)
            view.setNeedsDisplay_(True)
            if not panel.isVisible():
                panel.orderFrontRegardless()
            shown.add(index)
        for index, (panel, _view) in self.panels.items():
            if index not in shown and panel.isVisible():
                panel.orderOut_(None)

    @staticmethod
    def _paint(ctx, pens, origin):
        Quartz.CGContextTranslateCTM(ctx, -origin[0], -origin[1])
        replay(Quartz, ctx, pens)

    def _panel(self, index):
        found = self.panels.get(index)
        if found is not None:
            return found
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
        panel.setHidesOnDeactivate_(False)
        panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
            | AppKit.NSWindowCollectionBehaviorIgnoresCycle
            | AppKit.NSWindowCollectionBehaviorStationary
        )
        view = EffectsCanvas.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 1, 1))
        view.logger = self.logger
        view.on_error = lambda: self._fail(logged=True)
        panel.setContentView_(view)
        self.panels[index] = (panel, view)
        return panel, view

    def _hide(self):
        for panel, _view in self.panels.values():
            if panel.isVisible():
                panel.orderOut_(None)

    def _guard(self, work, *args):
        if self.disabled:
            return
        try:
            work(*args)
        except Exception:
            self._fail()

    def _fail(self, logged=False):
        if self.disabled:
            return
        self.disabled = True
        effect_id = getattr(self.fx, "id", None) or self.controller.cfg.crossing.get("glow_style")
        if logged:
            self.logger.error("crossing effect %r failed; effects off for this run", effect_id)
        else:
            self.logger.exception("crossing effect %r failed; effects off for this run", effect_id)
        self.player = effects.Player()
        try:
            self.timer.stop()
            self._hide()
        except Exception:
            pass


def _centre(region):
    return (region["x"] + region["w"] / 2.0, region["y"] + region["h"] / 2.0)


def padded(pen, pad):
    """`pen`'s bounds grown by `pad`, but never past the display it is clipped to, where the pad
    would open an empty panel on the display beside it."""
    x0, y0, x1, y1 = pen.bounds[0] - pad, pen.bounds[1] - pad, pen.bounds[2] + pad, pen.bounds[3] + pad
    clip = getattr(pen, "clip", None)
    if clip is not None:
        x0, y0, x1, y1 = max(x0, clip[0]), max(y0, clip[1]), min(x1, clip[2]), min(y1, clip[3])
    return (x0, y0, x1, y1)


def strip_on_display(region, point, box):
    """A Quartz (x, y, w, h) strip cut to the display holding `point`: the engine measures an edge
    along the whole desktop, and a band along all of it spans displays one panel cannot show on,
    and empty space beside a shorter display."""
    if point is None or box is None:
        return region
    left, top, right, bottom = display_at(point, box)
    x, y, w, h = region
    x0, y0, x1, y1 = max(x, left), max(y, top), min(x + w, right), min(y + h, bottom)
    return region if x1 <= x0 or y1 <= y0 else (x0, y0, x1 - x0, y1 - y0)


def corner_box(corner, display):
    """The Quartz (x, y, w, h) box of `corner` of `display` (left, top, right, bottom), as the
    engine reports a corner push."""
    left, top, right, bottom = display
    vertical, horizontal = corner.split("_")
    size = crossing.CORNER_PX
    return (left if horizontal == "left" else right - size, top if vertical == "top" else bottom - size, size, size)


def display_at(point, box):
    """(left, top, right, bottom) of the display under `point` in Quartz points, else the desktop
    box: on displays of different sizes the way home runs along the pointer's own display, not
    along empty space in the box."""
    x, y = point
    for rect in desktop_mac.monitors():
        if rect.x <= x < rect.x + rect.width and rect.y <= y < rect.y + rect.height:
            return (rect.x, rect.y, rect.x + rect.width, rect.y + rect.height)
    return box
