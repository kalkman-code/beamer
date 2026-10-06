"""The notch beam: crossing feedback for a push through the notch, after Border Beam
(libraries.dev/beam, the `border-beam` package's `colorful` palette on a dark ground).

The other notch style, the island, is notch_island.py; which one plays is `crossing.notch_style`,
chosen on the Crossing page. This shares the island's panel, geometry, breakthrough timing and
lifecycle by subclassing it, and replaces what is drawn.

A small black tab extends the notch while pressure builds, so there is a border below the camera
housing for light to ride. A comet travels that border: the palette is seen through strokes of the
border's own path, a faint one all the way along and a few short stacked segments at the comet's
head, and a blurred copy through wider strokes is the bloom. Pressure sets how bright it is and how
fast it travels, a trackpad tick flares the bloom, and breakthrough lights the whole border in a halo
while the comet runs on off the end.

The comet moves by distance along the path (strokeStart and strokeEnd), in at the left shoulder and
out at the right, with the notch itself hiding the way back. The package turns a conic mask instead,
and on a card that is fine; on this tab, far wider than it is deep, it was not: half of every turn
pointed up into the camera cutout, which has no pixels, the short sides lit all at once as the sweep
crossed them, and the long bottom shrank the comet to a spot: the net effect reads as flashing.

Movement is integrated on the frame timer rather than left to a CABasicAnimation, because its speed
follows pressure and retiming a running animation jumps. Reduce Motion parks the comet mid-border.

Main thread only. Any failure disables it for the run; crossing never depends on it.
"""

import time

import AppKit
import Quartz

from core import effects
import tokens
from notch_island import (
    NOTCH_FALLBACK_HEIGHT,
    PAD,
    SHOULDER,
    NotchIsland,
    _colour,
    _no_actions,
    after_seconds,
    breakthrough_phase,
    ease_out,
)
# The comet as stacked stroke segments, (half length as a fraction of the border, opacity): stacked,
# they give the package's soft falloff, near 0.95 at the head and 0.2 at the tips.
SEGMENTS = ((0.16, 0.22), (0.10, 0.30), (0.05, 0.45), (0.02, 0.70))
COMET_HALF = SEGMENTS[0][0]
# One lap in head positions: from fully off the start of the border to fully off its end.
LAP = 1.0 + 2 * COMET_HALF
BASE = 0.16

GROW_W = 10.0
GROW_D = 9.0
STROKE = 1.5
BLOOM_STROKE = 6.0
BLUR = 7.0
SLOW_TURN_S = 2.8
FAST_TURN_S = 0.9
FADE_IN_S = 0.12
FADE_OUT_S = 0.35
TICK_S = 0.18
HALO_S = 0.6

# The edge glow's beam style, kvm_bridge_app.EdgeGlow: a comet running along a straight edge.
EDGE_COMET = 0.3
EDGE_BASE = 0.2
EDGE_SLOW_S = 2.4
EDGE_FAST_S = 0.8
EDGE_FINISH_S = 0.6
EDGE_FINISH_TRAVERSE_S = 0.35
EDGE_FALLOFF = ((0.0, 1.0), (0.08, 0.85), (0.3, 0.25), (1.0, 0.0))


def mask_edge_falloff(layer, edge, beam):
    """Cut the along-edge mask inward as Windows' live Beam does. Layer coordinates run up."""
    if not beam:
        layer.setMask_(None)
        return
    falloff = layer.mask()
    if falloff is None:
        falloff = Quartz.CAGradientLayer.layer()
        falloff.setLocations_([at for at, _alpha in EDGE_FALLOFF])
        falloff.setColors_([AppKit.NSColor.colorWithWhite_alpha_(1.0, alpha).CGColor()
                           for _at, alpha in EDGE_FALLOFF])
        layer.setMask_(falloff)
    falloff.setFrame_(layer.bounds())
    start, end = {
        "left": ((0.0, 0.5), (1.0, 0.5)),
        "right": ((1.0, 0.5), (0.0, 0.5)),
        "top": ((0.5, 1.0), (0.5, 0.0)),
        "bottom": ((0.5, 0.0), (0.5, 1.0)),
    }[edge]
    falloff.setStartPoint_(start)
    falloff.setEndPoint_(end)


def palette_values(name):
    """The hex colours of a glow_colour setting: one of tokens.PALETTES, else a crossing-effects
    pack, which any style may use, else Beamer's own signal."""
    if name in tokens.PALETTES:
        return list(tokens.PALETTES[name])
    try:
        found = effects.pack(name) if name in effects.PACK_IDS else None
    except Exception:
        # The effects' modules failed to load; the overlay has already said so.
        found = None
    return list(found[1]) if found is not None else list(tokens.PALETTES["signal"])


def dark_appearance():
    style = AppKit.NSUserDefaults.standardUserDefaults().stringForKey_("AppleInterfaceStyle")
    return str(style or "").lower() == "dark"


def palette_colours(name, closed=False, dark=False):
    """CGColors for a glow_colour setting (see palette_values), a lone colour repeated so a gradient has two stops, and
    with `closed` ending on the first colour so a conic ring has no seam. `dark` lifts the darkest colours as the
    crossing effects do on a dark appearance, where the Ink packs all but vanish."""
    values = effects.legible(palette_values(name), dark)
    if len(values) == 1:
        values *= 2
    if closed:
        values.append(values[0])
    return [_colour(value) for value in values]


def edge_traverse_seconds(strength):
    """One run of the edge comet from end to end: unhurried at a light lean, quick near breakthrough."""
    strength = max(0.0, min(1.0, strength))
    return EDGE_SLOW_S + (EDGE_FAST_S - EDGE_SLOW_S) * strength


def edge_comet_alpha(position, centre, flash):
    """How lit a straight edge is at `position` (0 to 1 along it) with the comet's head at `centre`:
    a dim base, raised to `flash` at breakthrough, and a peak falling away over half the comet."""
    base = max(EDGE_BASE, min(1.0, flash))
    reach = max(0.0, 1.0 - abs(position - centre) / (EDGE_COMET / 2.0))
    return base + (1.0 - base) * reach * reach


def lap_finish(position, after):
    """How far the comet travels once the push has gone through: off the end of the lap it is on,
    plus as many whole laps as fit at its fastest pace in `after` seconds, so it leaves by the far
    shoulder instead of stopping wherever the crossing caught it."""
    remaining = LAP - position % LAP
    extra = max(0, round(after / FAST_TURN_S - remaining / LAP))
    return remaining + LAP * extra


def comet_segments(position):
    """(strokeStart, strokeEnd) for each of SEGMENTS with the lap at `position`, clamped to the
    border; a segment wholly off either end comes back empty, start equal to end."""
    head = position - COMET_HALF
    spans = []
    for half, _opacity in SEGMENTS:
        start = min(1.0, max(0.0, head - half))
        end = min(1.0, max(0.0, head + half))
        spans.append((start, max(start, end)))
    return spans


def turn_seconds(strength):
    """One lap of the border: an unhurried drift at a light lean, quick near breakthrough."""
    strength = max(0.0, min(1.0, strength))
    return SLOW_TURN_S + (FAST_TURN_S - SLOW_TURN_S) * strength


def approach(current, target, elapsed):
    """Strength moves toward pressure at a fixed rate, quicker in than out, so a push that stops
    fades the beam like the package's fade-out instead of cutting it."""
    if target > current:
        return min(target, current + elapsed / FADE_IN_S)
    return max(target, current - elapsed / FADE_OUT_S)


def _rgb(red, green, blue, alpha=1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(red / 255, green / 255, blue / 255, alpha).CGColor()


class NotchBeam(NotchIsland):
    dark_appearance = staticmethod(dark_appearance)

    def __init__(self, controller, logger):
        super().__init__(controller, logger)
        self.strength = 0.0
        self.position = 0.0
        self.flare = 0.0
        self.drawn_at = None
        self.comets = []
        self.gradients = []
        self.painted = None
        self.cross_position = 0.0
        self.cross_travel = 0.0
        self.halo_at = None

    def update(self, kind):
        if self.disabled or kind not in ("pressure", "tick", "cross"):
            return
        try:
            if kind == "tick":
                self.flare = 1.0
            elif kind == "cross":
                self.cross_at = self.halo_at = time.monotonic()
                self.held = 0.0
                self.cross_position = self.position
                self.cross_travel = lap_finish(self.position, after_seconds(self.controller))
            self._draw()
        except Exception:
            self._fail()

    def _draw(self):
        now = time.monotonic()
        elapsed = 0.0 if self.drawn_at is None else min(0.1, now - self.drawn_at)
        self.drawn_at = now
        level = self._held(self.controller.crossing_pressure_now(), now)
        flash = 0.0
        after = after_seconds(self.controller)
        if self.cross_at is not None:
            phase = breakthrough_phase(now - self.cross_at, after)
            if phase is None:
                self.cross_at = None
                self.position = (self.cross_position + self.cross_travel) % LAP
            else:
                level, flash = max(level, phase[0]), phase[1]
        halo = 0.0 if self.halo_at is None else max(0.0, 1.0 - (now - self.halo_at) / HALO_S)
        if not self.visible:
            if level <= 0.0:
                self.drawn_at = None
                return
            if not self._show():
                return
        self.strength = approach(self.strength, level, elapsed)
        self.flare = max(0.0, self.flare - elapsed / TICK_S)
        if AppKit.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion():
            self.position = LAP / 2.0
        elif self.cross_at is not None:
            travelled = self.cross_travel * ease_out((now - self.cross_at) / after)
            self.position = (self.cross_position + travelled) % LAP
        elif level > 0.0:
            # Only while pushing: fading out after a crossing it would otherwise start a fresh lap
            # in from the near shoulder, a new flash on the way out.
            self.position = (self.position + LAP * elapsed / turn_seconds(self.strength)) % LAP
        spans = comet_segments(self.position)
        colour = (self.controller.cfg.crossing["glow_colour"], self.dark_appearance())
        with _no_actions():
            if colour != self.painted:
                for gradient in self.gradients:
                    gradient.setColors_(palette_colours(colour[0], closed=True, dark=colour[1]))
                self.painted = colour
            for segments in self.comets:
                for layer, (start, end) in zip(segments, spans):
                    # A round cap draws a dot even for an empty stroke, so a segment all but gone
                    # off an end is hidden rather than left blinking at the shoulder.
                    layer.setHidden_(end - start < 0.002)
                    layer.setStrokeStart_(start)
                    layer.setStrokeEnd_(end)
            # The tab is only somewhere for light to ride, so it is there at once rather than
            # greying in with pressure, which read as a dim flash under a slow lean.
            self.island.setOpacity_(min(1.0, self.strength * 20.0))
            self.beam.setOpacity_(min(1.0, self.strength + flash))
            self.bloom.setOpacity_(min(1.0, self.strength * 0.8 + self.flare * 0.35 + flash * 0.6))
            self.halo.setOpacity_(halo * 0.9)
        if level <= 0.0 and self.cross_at is None and self.strength <= 0.0:
            self.drawn_at = None
            self._hide()
            return
        if not getattr(self.timer, "is_alive", lambda: False)():
            self.timer.start()

    def _build(self, geometry):
        left, right, top, height = geometry
        self.notch_width = right - left
        self.notch_height = height or NOTCH_FALLBACK_HEIGHT
        spare = GROW_W + SHOULDER + PAD
        width = self.notch_width + 2 * spare
        panel_height = self.notch_height + GROW_D + PAD
        self.centre = width / 2.0
        panel, stage = self._panel(AppKit.NSMakeRect(left - spare, top - panel_height, width, panel_height))
        panel.contentView().setLayerUsesCoreImageFilters_(True)
        bounds = stage.bounds()

        island = Quartz.CAShapeLayer.layer()
        island.setFrame_(bounds)
        island.setFillColor_(AppKit.NSColor.blackColor().CGColor())
        island.setPath_(self._island_path(GROW_W, GROW_D))
        island.setOpacity_(0.0)
        stage.addSublayer_(island)

        border = self._outline(GROW_W, GROW_D, closed=False, shoulders=True)
        middle = (self.centre, (self.notch_height + GROW_D) / 2.0)
        side = 2.0 * max(width, panel_height)
        self.comets = []
        self.gradients = []
        self.painted = None

        bloom = self._blurred(bounds, BLUR)
        bloom.addSublayer_(self._ring(border, BLOOM_STROKE, bounds, middle, side))
        stage.addSublayer_(bloom)

        beam = self._ring(border, STROKE, bounds, middle, side)
        beam.setOpacity_(0.0)
        stage.addSublayer_(beam)

        # The breakthrough: the whole border lights at once and blooms out, like the package's
        # pulse-outside, while the comet runs on off the end.
        halo = self._blurred(bounds, BLUR * 1.6)
        halo.addSublayer_(self._ring(border, BLOOM_STROKE * 1.5, bounds, middle, side, comet=False))
        stage.addSublayer_(halo)

        self.panel = panel
        self.island = island
        self.bloom = bloom
        self.beam = beam
        self.halo = halo

    @staticmethod
    def _blurred(bounds, radius):
        layer = Quartz.CALayer.layer()
        layer.setFrame_(bounds)
        blur = Quartz.CIFilter.filterWithName_("CIGaussianBlur")
        blur.setDefaults()
        blur.setValue_forKey_(radius, "inputRadius")
        layer.setFilters_([blur])
        layer.setOpacity_(0.0)
        return layer

    def _ring(self, path, line_width, bounds, middle, side, comet=True):
        """The palette seen through strokes of `path`: a faint line the whole way with the comet's
        segments over it, or the whole path at full strength with `comet` off."""
        ring = Quartz.CALayer.layer()
        ring.setFrame_(bounds)
        mask = Quartz.CALayer.layer()
        mask.setFrame_(bounds)
        mask.addSublayer_(self._stroke(path, line_width, bounds, BASE if comet else 1.0))
        if comet:
            segments = []
            for _half, opacity in SEGMENTS:
                segment = self._stroke(path, line_width, bounds, opacity)
                segment.setHidden_(True)
                mask.addSublayer_(segment)
                segments.append(segment)
            self.comets.append(segments)
        ring.setMask_(mask)

        colours = Quartz.CAGradientLayer.layer()
        colours.setType_(Quartz.kCAGradientLayerConic)
        colours.setFrame_(((middle[0] - side / 2.0, middle[1] - side / 2.0), (side, side)))
        colours.setStartPoint_((0.5, 0.5))
        colours.setEndPoint_((0.5, 0.0))
        colours.setColors_(palette_colours("signal", closed=True))
        self.gradients.append(colours)
        ring.addSublayer_(colours)
        return ring

    @staticmethod
    def _stroke(path, line_width, bounds, opacity):
        stroke = Quartz.CAShapeLayer.layer()
        stroke.setFrame_(bounds)
        stroke.setPath_(path)
        stroke.setFillColor_(None)
        stroke.setStrokeColor_(_rgb(255, 255, 255))
        stroke.setLineWidth_(line_width)
        stroke.setLineCap_(Quartz.kCALineCapRound)
        stroke.setLineJoin_(Quartz.kCALineJoinRound)
        stroke.setOpacity_(opacity)
        return stroke
