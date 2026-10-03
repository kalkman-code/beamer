"""Crossing effects: the departure and arrival animations, drawn once here and replayed natively by
each app.

An effect draws into a Pen, which offers the part of the HTML canvas API the original designs were
written against, so each fx_*.py module is a line-for-line port of its design's animation logic. The
Pen does not draw: it records operations in screen points with every transform already applied, arcs
already turned into curves, and colours already parsed. The Mac replays them into Core Graphics and
Windows into QPainter, both checked against the original animation for fidelity.

`Player` owns the timing both apps share: pressure, the quarter ticks, breakthrough, the departure
finishing and the arrival playing out, so an effect looks the same on both machines.
"""

from __future__ import annotations

import colorsys
import functools
import math
import re
from types import SimpleNamespace

TAU = math.pi * 2.0
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def hash_n(n):
    """The harness's stable pseudo-random number in [0, 1) for integer `n`; the designs' only
    randomness, so every frame is a function of its state."""
    x = math.sin(n * 127.1 + 311.7) * 43758.5453
    return x - math.floor(x)


def clamp(v, a=0.0, b=1.0):
    return max(a, min(b, v))


def lerp(a, b, u):
    return a + (b - a) * u


def ease_out(u):
    return 1.0 - (1.0 - u) ** 3


def ease_in_out(u):
    return 2.0 * u * u if u < 0.5 else 1.0 - ((-2.0 * u + 2.0) ** 2) / 2.0


_NAMED = {"black": (0.0, 0.0, 0.0, 1.0), "white": (1.0, 1.0, 1.0, 1.0), "transparent": (0.0, 0.0, 0.0, 0.0)}
_RGB = re.compile(r"rgba?\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*(?:,\s*([-\d.]+)\s*)?\)$")


def parse_colour(value):
    """A CSS colour as the designs write them, "#rgb", "#rrggbb", "#rrggbbaa", "rgb()" or "rgba()",
    or an (r, g, b[, a]) tuple of 0 to 1 floats, as (r, g, b, a) floats. Anything else is black, as
    a canvas would leave the previous style; a design that relies on that is a bug to fix there."""
    if isinstance(value, tuple):
        return value if len(value) == 4 else (value[0], value[1], value[2], 1.0)
    return _parse_text(str(value))


@functools.lru_cache(maxsize=4096)
def _parse_text(value):
    """parse_colour for text. Cached: the heavier effects parse the same few colours a hundred
    times a frame."""
    text = value.strip().lower()
    if text in _NAMED:
        return _NAMED[text]
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) in (3, 4):
            digits = "".join(c * 2 for c in digits)
        if len(digits) in (6, 8):
            parts = [int(digits[i:i + 2], 16) / 255.0 for i in range(0, len(digits), 2)]
            return tuple(parts) if len(parts) == 4 else (parts[0], parts[1], parts[2], 1.0)
    match = _RGB.match(text)
    if match:
        r, g, b = (clamp(float(match.group(i)) / 255.0) for i in (1, 2, 3))
        a = clamp(float(match.group(4))) if match.group(4) is not None else 1.0
        return (r, g, b, a)
    return (0.0, 0.0, 0.0, 1.0)


# The least HLS lightness a colour is drawn at on a dark wallpaper, and the most on a light one
# by the effects in LIGHT_AS_GIVEN.
DARK_FLOOR = 0.45
LIGHT_CEILING = 0.6


def legible(palette, dark, fx=None):
    """`palette` as `fx` draws it. On a dark wallpaper every colour darker than DARK_FLOOR is
    lifted to it, keeping its hue and saturation: the Ink packs lead with near-black inks and Ember
    ends on a deep red, and effects that drew those, additively on dark most of all, all but
    vanished. On a light one the effects that draw a pack's colours as given have every colour
    lighter than LIGHT_CEILING brought down to it, as Mono, Pearl and Contrast all but vanished."""
    if dark:
        low, high = DARK_FLOOR, 1.0
    elif getattr(fx, "id", None) in LIGHT_AS_GIVEN:
        low, high = 0.0, LIGHT_CEILING
    else:
        return list(palette)
    out = []
    for colour in palette:
        r, g, b, _a = parse_colour(colour)
        h, lightness, sat = colorsys.rgb_to_hls(r, g, b)
        if not low <= lightness <= high:
            r, g, b = colorsys.hls_to_rgb(h, clamp(lightness, low, high), sat)
        out.append("#%02x%02x%02x" % tuple(int(round(v * 255)) for v in (r, g, b)))
    return out


def _apply(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def _multiply(m, n):
    """m then n in canvas order: the result maps a point through n first, then m."""
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + c * b2, b * a2 + d * b2, a * c2 + c * d2, b * c2 + d * d2, a * e2 + c * f2 + e, b * e2 + d * f2 + f)


def _scale_of(m):
    return math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))


class Gradient:
    """A canvas gradient, its geometry already in screen points. `kind` is "linear" (x0, y0, x1, y1),
    "radial" (x0, y0, r0, x1, y1, r1) or "conic" (start angle, cx, cy); stops are (offset, rgba)."""

    def __init__(self, kind, geometry):
        self.kind = kind
        self.geometry = geometry
        self.stops = []

    def addColorStop(self, offset, colour):
        self.stops.append((clamp(float(offset)), parse_colour(colour)))


class Pen:
    """The recording canvas. Style properties keep their canvas names so ports stay line-for-line.

    `ops` is a list of ("fill", path, paint, alpha, composite, rule) and
    ("stroke", path, paint, alpha, composite, width, cap, join), where path is a list of
    ("M", x, y), ("L", x, y), ("Q", cx, cy, x, y), ("C", c1x, c1y, c2x, c2y, x, y) and ("Z",),
    and paint is an rgba tuple or a Gradient. `bounds` is a conservative box round everything drawn."""

    def __init__(self):
        self.ops = []
        self.bounds = None
        self._stack = []
        self._path = []
        self._start = None
        self._current = None
        self.fillStyle = "#000"
        self.strokeStyle = "#000"
        self.lineWidth = 1.0
        self.lineCap = "butt"
        self.lineJoin = "miter"
        self.globalAlpha = 1.0
        self.globalCompositeOperation = "source-over"
        self._matrix = IDENTITY
        # (left, top, right, bottom) of the display this Pen is for, which the replay clips to.
        self.clip = None

    def save(self):
        self._stack.append((self.fillStyle, self.strokeStyle, self.lineWidth, self.lineCap, self.lineJoin,
                            self.globalAlpha, self.globalCompositeOperation, self._matrix))

    def restore(self):
        if self._stack:
            (self.fillStyle, self.strokeStyle, self.lineWidth, self.lineCap, self.lineJoin,
             self.globalAlpha, self.globalCompositeOperation, self._matrix) = self._stack.pop()

    def transform(self, a, b, c, d, e, f):
        self._matrix = _multiply(self._matrix, (a, b, c, d, e, f))

    def setTransform(self, a, b, c, d, e, f):
        self._matrix = (a, b, c, d, e, f)

    def translate(self, x, y):
        self.transform(1, 0, 0, 1, x, y)

    def scale(self, x, y):
        self.transform(x, 0, 0, y, 0, 0)

    def rotate(self, angle):
        cos, sin = math.cos(angle), math.sin(angle)
        self.transform(cos, sin, -sin, cos, 0, 0)

    def beginPath(self):
        self._path = []
        self._start = None
        self._current = None

    def moveTo(self, x, y):
        p = _apply(self._matrix, x, y)
        self._path.append(("M",) + p)
        self._start = self._current = p

    def lineTo(self, x, y):
        if self._current is None:
            self.moveTo(x, y)
            return
        p = _apply(self._matrix, x, y)
        self._path.append(("L",) + p)
        self._current = p

    def quadraticCurveTo(self, cx, cy, x, y):
        if self._current is None:
            self.moveTo(cx, cy)
        c = _apply(self._matrix, cx, cy)
        p = _apply(self._matrix, x, y)
        self._path.append(("Q",) + c + p)
        self._current = p

    def bezierCurveTo(self, c1x, c1y, c2x, c2y, x, y):
        if self._current is None:
            self.moveTo(c1x, c1y)
        c1 = _apply(self._matrix, c1x, c1y)
        c2 = _apply(self._matrix, c2x, c2y)
        p = _apply(self._matrix, x, y)
        self._path.append(("C",) + c1 + c2 + p)
        self._current = p

    def closePath(self):
        if self._current is not None:
            self._path.append(("Z",))
            self._current = self._start

    def rect(self, x, y, w, h):
        self.moveTo(x, y)
        self.lineTo(x + w, y)
        self.lineTo(x + w, y + h)
        self.lineTo(x, y + h)
        self.closePath()
        self.moveTo(x, y)

    def arc(self, x, y, r, a0, a1, anticlockwise=False):
        """Canvas arc semantics: a line from the current point to the arc's start, then the sweep,
        drawn as cubic curves in user space so any transform, mirrors included, stays exact."""
        r = abs(r)
        if anticlockwise:
            sweep = -TAU if a0 - a1 >= TAU else -((a0 - a1) % TAU)
        else:
            sweep = TAU if a1 - a0 >= TAU else (a1 - a0) % TAU
        sx, sy = x + r * math.cos(a0), y + r * math.sin(a0)
        if self._current is None:
            self.moveTo(sx, sy)
        else:
            self.lineTo(sx, sy)
        if sweep == 0.0 or r == 0.0:
            return
        # An eighth of a turn per curve: a quarter's error, 0.03% of the radius at its midpoint,
        # moved the antialiasing of thin stroked rings visibly against a native arc.
        pieces = max(1, int(math.ceil(abs(sweep) / (math.pi / 4.0) - 1e-9)))
        step = sweep / pieces
        k = 4.0 / 3.0 * math.tan(step / 4.0)
        angle = a0
        for _ in range(pieces):
            end = angle + step
            c0, s0, c1, s1 = math.cos(angle), math.sin(angle), math.cos(end), math.sin(end)
            self.bezierCurveTo(x + r * (c0 - k * s0), y + r * (s0 + k * c0),
                               x + r * (c1 + k * s1), y + r * (s1 - k * c1),
                               x + r * c1, y + r * s1)
            angle = end

    def createLinearGradient(self, x0, y0, x1, y1):
        return Gradient("linear", _apply(self._matrix, x0, y0) + _apply(self._matrix, x1, y1))

    def createRadialGradient(self, x0, y0, r0, x1, y1, r1):
        k = _scale_of(self._matrix)
        return Gradient("radial", _apply(self._matrix, x0, y0) + (r0 * k,) + _apply(self._matrix, x1, y1) + (r1 * k,))

    def createConicGradient(self, angle, x, y):
        m = self._matrix
        return Gradient("conic", (angle + math.atan2(m[1], m[0]),) + _apply(m, x, y))

    def fill(self, rule="nonzero"):
        if self._path:
            self._record(("fill", list(self._path), self._paint(self.fillStyle), clamp(self.globalAlpha),
                          self.globalCompositeOperation, rule), 0.0)

    def stroke(self):
        if self._path and self.lineWidth > 0:
            width = self.lineWidth * _scale_of(self._matrix)
            self._record(("stroke", list(self._path), self._paint(self.strokeStyle), clamp(self.globalAlpha),
                          self.globalCompositeOperation, width, self.lineCap, self.lineJoin), width)

    def fillRect(self, x, y, w, h):
        saved = (self._path, self._start, self._current)
        self.beginPath()
        self.rect(x, y, w, h)
        self.fill()
        self._path, self._start, self._current = saved

    @staticmethod
    def _paint(style):
        return style if isinstance(style, Gradient) else parse_colour(style)

    def _record(self, op, width):
        if op[3] <= 0.0:
            return
        paint = op[2]
        if not isinstance(paint, Gradient) and paint[3] <= 0.0:
            return
        xs, ys = [], []
        for segment in op[1]:
            xs.extend(segment[1::2])
            ys.extend(segment[2::2])
        if not xs:
            return
        pad = width / 2.0 + (width * 5.0 if op[0] == "stroke" and op[7] == "miter" else 0.0) + 2.0
        box = (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)
        if self.bounds is None:
            self.bounds = box
        else:
            b = self.bounds
            self.bounds = (min(b[0], box[0]), min(b[1], box[1]), max(b[2], box[2]), max(b[3], box[3]))
        self.ops.append(op)


def round_rect(pen, x, y, w, h, r):
    """The harness's BeamerDraw.roundRect: a new path, not a fill."""
    pen.beginPath()
    pen.moveTo(x + r, y)
    _arc_to(pen, x + w, y, x + w, y + h, r)
    _arc_to(pen, x + w, y + h, x, y + h, r)
    _arc_to(pen, x, y + h, x, y, r)
    _arc_to(pen, x, y, x + w, y, r)
    pen.closePath()


def _arc_to(pen, x1, y1, x2, y2, r):
    """canvas arcTo from the pen's current point, in user space. Only round_rect uses it, whose
    corners are always right angles, so the tangent points are r along each side."""
    m = pen._matrix
    inverse = _invert(m)
    x0, y0 = _apply(inverse, *pen._current)
    d1 = math.hypot(x1 - x0, y1 - y0) or 1.0
    d2 = math.hypot(x2 - x1, y2 - y1) or 1.0
    ax, ay = x1 - (x1 - x0) / d1 * r, y1 - (y1 - y0) / d1 * r
    bx, by = x1 + (x2 - x1) / d2 * r, y1 + (y2 - y1) / d2 * r
    pen.lineTo(ax, ay)
    k = 0.5522847498
    pen.bezierCurveTo(ax + (x1 - ax) * k, ay + (y1 - ay) * k, bx + (x1 - bx) * k, by + (y1 - by) * k, bx, by)


def _invert(m):
    a, b, c, d, e, f = m
    det = a * d - b * c or 1.0
    return (d / det, -b / det, -c / det, a / det, (c * f - d * e) / det, (b * e - a * f) / det)


def point(x, y):
    return SimpleNamespace(x=float(x), y=float(y))


def state(screen_w, screen_h, os_name, method, palette, *, reduced=False, dark=True, notch=None,
          effect_size="medium", **fields):
    """The `s` an effect reads, with the harness's field names. Departure passes region, point,
    along (the fraction along the edge), pressure, tick (the last quarter tick, or None) and since
    (seconds since breakthrough); arrival passes point, edge and since (seconds since landing)."""
    s = SimpleNamespace(
        screen=SimpleNamespace(w=float(screen_w), h=float(screen_h), os=os_name),
        method=method, palette=list(palette), reduced=bool(reduced), scale=1.0, dark=bool(dark),
        effect_size=effect_size, hash=hash_n, memo={},
    )
    if notch is not None:
        s.notch = SimpleNamespace(**notch)
    for key, value in fields.items():
        setattr(s, key, value)
    return s


def nearest_edge(x, y, w, h):
    """The screen edge closest to (x, y). A switch arrival is oriented by it: every effect reads
    `s.edge`, and the ones that would draw at the edge itself look for method "switch" instead."""
    return min((("left", x), ("right", w - x), ("top", y), ("bottom", h - y)), key=lambda pair: pair[1])[0]


def display_for(at, displays):
    """The display in `displays`, each (x, y, w, h), that holds the point `at`, else the nearest,
    and `at` moved onto it: a point on no display is where the OS puts the pointer, on the nearest."""
    x, y = at

    def gap(rect):
        rx, ry, rw, rh = rect
        return math.hypot(max(rx - x, 0.0, x - (rx + rw - 1.0)), max(ry - y, 0.0, y - (ry + rh - 1.0)))

    rx, ry, rw, rh = (float(v) for v in min(displays, key=gap))
    return (rx, ry, rw, rh), (min(max(float(x), rx), rx + rw - 1.0), min(max(float(y), ry), ry + rh - 1.0))


class Effect:
    def __init__(self, id, name, intensity, blurb, depart, arrive, depart_seconds, arrive_seconds):
        self.id = id
        self.name = name
        self.intensity = intensity
        self.blurb = blurb
        self.depart = depart
        self.arrive = arrive
        self.depart_seconds = min(0.8, float(depart_seconds))
        self.arrive_seconds = min(0.8, float(arrive_seconds))


def _light_aperture(pen, state, departing):
    from .fx_light import _aperture
    _aperture(pen, state, departing)


# Every direction built, in the order the Design pages list them: each direction's quiet, medium
# and showpiece effect, and its three colour packs. Ids are the settings values, so never rename one.
ALL_DIRECTIONS = (
    ("fx_membrane", "Membrane", ("skin", "film", "rupture"), ("oil_slick", "soap_bubble", "pearl")),
    ("fx_sparks", "Sparks", ("flint", "filings", "discharge"), ("ember", "forge", "sparkler_gold")),
    ("fx_instrument", "Instrument", ("rule", "gauge", "rangefinder"), ("phosphor", "sodium", "contrast")),
    ("fx_folio", "Folio", ("crease", "pleat", "concertina"), ("vellum", "carbon_copy", "marbled")),
    ("fx_selvedge", "Selvedge", ("thread", "weave", "jacquard"), ("flax", "madder", "tide")),
    ("fx_ink", "Ink", ("capillary", "viscous_drop", "sumi_bloom"), ("indigo_ink", "matcha", "terracotta")),
    ("fx_warp", "Warp", ("light_slit", "hyperdrive", "wormhole"), ("neon", "vaporwave", "cyber")),
)
# Directions kept in the code but offered nowhere: not on the Design pages, not as a setting. A
# setting that chose one of their effects or packs becomes its stand-in when it is read, and a
# switch that played one plays whatever the crossing plays.
HIDDEN = ("fx_ink", "fx_warp")
STAND_INS = {"capillary": "glow", "viscous_drop": "glow", "sumi_bloom": "glow",
             "indigo_ink": "colourful", "matcha": "colourful", "terracotta": "colourful",
             "light_slit": "beam", "hyperdrive": "glow", "wormhole": "glow",
             "neon": "colourful", "vaporwave": "colourful", "cyber": "colourful"}
DIRECTIONS = tuple(direction for direction in ALL_DIRECTIONS if direction[0] not in HIDDEN)
EFFECT_IDS = ("aperture",) + tuple(effect for _module, _name, effects, _packs in DIRECTIONS for effect in effects)
# The effects whose designs draw a pack's colours as given on a light wallpaper; the others shade
# them for light themselves.
LIGHT_AS_GIVEN = ("aperture", "crease", "pleat", "concertina", "thread", "weave", "jacquard") + tuple(
    effect for module, _name, effects, _packs in ALL_DIRECTIONS if module in ("fx_ink", "fx_warp")
    for effect in effects
)
PACK_IDS = tuple(pack for _module, _name, _effects, packs in DIRECTIONS for pack in packs)

_loaded = None


def _load():
    """Imports the fx_* modules on first use, hidden ones included; each has EFFECTS (Effect list)
    and PACKS (dict of id to (name, colours)), in ALL_DIRECTIONS order."""
    global _loaded
    if _loaded is None:
        import importlib
        effects, packs = {}, {}
        effects["aperture"] = CLASSIC["aperture"]
        for module_name, _name, effect_ids, pack_ids in ALL_DIRECTIONS:
            module = importlib.import_module(f"{__package__}.{module_name}")
            by_id = {effect.id: effect for effect in module.EFFECTS}
            for effect_id in effect_ids:
                effects[effect_id] = by_id[effect_id]
            for pack_id in pack_ids:
                packs[pack_id] = module.PACKS[pack_id]
        _loaded = (effects, packs)
    return _loaded


def effect(effect_id):
    return _load()[0].get(effect_id)


def _locator_arrive(pen, s):
    """Glow and Beam have no arrival, so a switch into this machine shows where the pointer is with
    one ring in the chosen colours closing onto its tip: 220pt across to 32pt in 0.45s, decelerating,
    thickening as it closes, then a soft glow at the tip and gone by 0.72s. A dark halo under the
    colour keeps it legible on a light wallpaper. Reduce motion keeps the size fixed: two still
    rings fading in and out."""
    t = s.since
    x, y = s.point.x, s.point.y
    colours = s.palette or ["#5fd4f4"]
    if s.reduced:
        a = clamp(t / 0.1) * (1.0 - clamp((t - 0.35) / 0.35))
        rings = ((16.0, 3.0, a), (30.0, 2.0, 0.55 * a))
    else:
        # A cubic ease-out: a quintic closed the ring within 0.15s, too fast for the eye to follow in.
        k = ease_out(clamp(t / 0.45))
        a = clamp(t / 0.08) * (1.0 - clamp((t - 0.52) / 0.2))
        rings = ((lerp(110.0, 16.0, k), lerp(2.0, 3.0, k), a),)
        if t > 0.38:
            bloom = clamp((t - 0.38) / 0.12) * (1.0 - clamp((t - 0.5) / 0.22))
            glow = pen.createRadialGradient(x, y, 0.0, x, y, 26.0)
            r, g, b, _a = parse_colour(colours[0])
            glow.addColorStop(0.0, (r, g, b, 0.45 * bloom))
            glow.addColorStop(1.0, (r, g, b, 0.0))
            pen.globalCompositeOperation = "lighter" if s.dark else "source-over"
            pen.fillStyle = glow
            pen.beginPath()
            pen.arc(x, y, 26.0, 0.0, TAU)
            pen.fill()
            pen.globalCompositeOperation = "source-over"
    pen.lineJoin = "round"
    for radius, width, alpha in rings:
        if alpha <= 0.0:
            continue
        pen.globalAlpha = alpha
        pen.beginPath()
        pen.arc(x, y, radius, 0.0, TAU)
        pen.strokeStyle = "rgba(0,0,0,0.3)" if s.dark else "rgba(0,0,0,0.5)"
        pen.lineWidth = width + 3.0
        pen.stroke()
        if len(colours) == 1:
            pen.strokeStyle = colours[0]
        else:
            ring = pen.createConicGradient(-math.pi / 2.0, x, y)
            for index, colour in enumerate(list(colours) + [colours[0]]):
                ring.addColorStop(index / len(colours), colour)
            pen.strokeStyle = ring
        pen.lineWidth = width
        pen.stroke()
    pen.globalAlpha = 1.0


# What a switch plays when the chosen style is Glow or Beam, which have no arrival of their own.
LOCATOR = Effect("locator", "Ring", "quiet", "A ring in the chosen colours closes onto the pointer.",
                 lambda pen, s: None, _locator_arrive, 0.0, 0.72)


# Glow and Beam as the Design pages' large previews draw them, in the same scene as the effects. On
# screen each app draws them with its own glow window, which these follow: a band along the edge,
# deepening with the push and flashing through at breakthrough, or a thin line with a comet running
# along it that runs off the end; in a corner, the band along both walls, brightest where they meet.
CLASSIC_ARM = 200.0
CLASSIC_FLASH_S = 0.35
CLASSIC_FINISH_S = 0.6
_CLASSIC_COMET = 0.3
_CLASSIC_STOPS = 24
_CLASSIC_LAYERS = 10


def _palette_at(palette, u):
    colours = [parse_colour(c) for c in (palette or ["#5fd4f4"])]
    if len(colours) == 1:
        return colours[0]
    at = clamp(u) * (len(colours) - 1)
    i = min(len(colours) - 2, int(at))
    a, b = colours[i], colours[i + 1]
    k = at - i
    return tuple(lerp(a[n], b[n], k) for n in range(4))


def _classic_wall(pen, s, beam, x, y, length, inward, horizontal, strength, depth, alpha_at):
    """One wall's light: `length` along from (x, y), horizontally or not, falling off over `depth`
    in the direction `inward` (+1 or -1). One gradient along the wall carries the palette and how lit
    each point is; canvas has no mask to cut it with a falloff inward, so it is stacked in layers of
    shrinking depth, which add up to the falloff."""
    along = pen.createLinearGradient(x, y, x + length, y) if horizontal else pen.createLinearGradient(x, y, x, y + length)
    for i in range(_CLASSIC_STOPS + 1):
        u = i / _CLASSIC_STOPS
        r, g, b, _a = _palette_at(s.palette, u)
        along.addColorStop(u, (r, g, b, clamp(strength * alpha_at(u))))
    pen.fillStyle = along
    # The beam is a thin line with a short glow behind it; the glow a band that falls off evenly.
    profile = ((0.05, 0.75), (0.16, 0.3), (0.45, 0.12)) if beam else tuple(((k + 1) / _CLASSIC_LAYERS, 1.0 / _CLASSIC_LAYERS)
                                                                       for k in range(_CLASSIC_LAYERS))
    for reach, alpha in profile:
        d = depth * reach
        pen.globalAlpha = alpha
        if horizontal:
            pen.fillRect(x, y if inward > 0 else y - d, length, d)
        else:
            pen.fillRect(x if inward > 0 else x - d, y, d, length)
    pen.globalAlpha = 1.0


def _comet(position, centre, flash):
    base = max(0.2, min(1.0, flash))
    reach = max(0.0, 1.0 - abs(position - centre) / (_CLASSIC_COMET / 2.0))
    return base + (1.0 - base) * reach * reach


def _classic_depart(beam):
    def depart(pen, s):
        since = getattr(s, "since", None)
        pressure = 0.0 if since is not None else s.pressure
        flash = 0.0 if since is None else max(0.0, 1.0 - since / CLASSIC_FLASH_S)
        finish = 0.0 if since is None else max(0.0, 1.0 - since / CLASSIC_FINISH_S)
        strength = max(pressure * 0.85, flash, finish if beam else 0.0)
        if strength <= 0.0:
            return
        w, h = s.screen.w, s.screen.h
        full_depth = edge_depth(w, h, getattr(s, "effect_size", "medium"))
        depth = full_depth if beam else full_depth * (0.35 + 0.65 * max(pressure, flash))
        # The comet sweeps as the push builds, then runs off the far end after breakthrough.
        centre = -0.15 + 1.1 * pressure if since is None else 0.95 + (1.0 + _CLASSIC_COMET) * (1.0 - finish)
        lit = (lambda u: _comet(u, centre, flash)) if beam else (lambda u: 1.0)
        r = s.region
        pen.globalCompositeOperation = "lighter" if s.dark else "source-over"
        if r.kind == "corner":
            vertical, horizontal = getattr(r, "corner", "top_right").split("_")
            arm = min(CLASSIC_ARM, w, h)
            right, bottom = horizontal == "right", vertical == "bottom"
            fade = lambda d: max(0.0, 1.0 - d) ** 1.4
            # The comet runs in along the top or bottom wall and out along the side.
            wall_y = h if bottom else 0.0
            x0 = w - arm if right else 0.0
            along_h = (lambda u: fade(1.0 - u) * (_comet(0.5 * u, centre, flash) if beam else 1.0)) if right else \
                (lambda u: fade(u) * (_comet(0.5 * (1.0 - u), centre, flash) if beam else 1.0))
            _classic_wall(pen, s, beam, x0, wall_y, arm, -1 if bottom else 1, True, strength, depth, along_h)
            wall_x = w if right else 0.0
            y0 = h - arm if bottom else 0.0
            along_v = (lambda u: fade(1.0 - u) * (_comet(1.0 - 0.5 * u, centre, flash) if beam else 1.0)) if bottom else \
                (lambda u: fade(u) * (_comet(0.5 + 0.5 * u, centre, flash) if beam else 1.0))
            _classic_wall(pen, s, beam, wall_x, y0, arm, -1 if right else 1, False, strength, depth, along_v)
        elif r.kind == "notch":
            # At the notch Glow and Beam do not draw: the Mac's notch style plays there instead.
            pass
        elif r.edge in ("left", "right"):
            x = r.x if r.edge == "left" else r.x + r.w
            _classic_wall(pen, s, beam, x, r.y, r.h, 1 if r.edge == "left" else -1, False, strength, depth, lit)
        else:
            y = r.y if r.edge == "top" else r.y + r.h
            _classic_wall(pen, s, beam, r.x, y, r.w, 1 if r.edge == "top" else -1, True, strength, depth, lit)
        pen.globalCompositeOperation = "source-over"
    return depart


CLASSIC = {
    "glow": Effect("glow", "Glow", "quiet", "A band of light that deepens the harder you push.",
                   _classic_depart(False), lambda pen, s: None, CLASSIC_FLASH_S, 0.0),
    "beam": Effect("beam", "Beam", "medium", "A thin line with a comet of light running along it.",
                   _classic_depart(True), lambda pen, s: None, CLASSIC_FINISH_S, 0.0),
    "aperture": Effect(
        "aperture", "Aperture", "showpiece",
        "A fine lens of light opens under pressure and closes softly behind the arriving pointer.",
        lambda pen, state: _light_aperture(pen, state, True),
        lambda pen, state: _light_aperture(pen, state, False), 0.62, 0.62,
    ),
}


def preview_effect(style):
    """The Effect the Design pages' previews play for `style`: one of the effects, or Glow and Beam
    as CLASSIC draws them. None for anything else."""
    return CLASSIC.get(style) or (effect(style) if style in EFFECT_IDS else None)


# What a switch into a machine may play, the Design pages' own choice for it: whatever the crossing
# style plays ("match", which is the ring for Glow and Beam), the ring, or any one effect.
SWITCH_STYLES = ("match", "locator") + EFFECT_IDS


def offered(style, colour, switch):
    """A saved crossing style, colour and switch style, each hidden one swapped for its stand-in."""
    hidden = {effect for module, _name, effects, _packs in ALL_DIRECTIONS if module in HIDDEN for effect in effects}
    return STAND_INS.get(style, style), STAND_INS.get(colour, colour), "match" if switch in hidden else switch


def switch_style_groups():
    """The Design pages' choices for SWITCH_STYLES, as (group title or None, ((value, name), ...)),
    the effects grouped by direction as the style tiles are. Only the first group when the effects
    cannot load; the overlay has already said why."""
    groups = [(None, (("match", "Same as crossing"), ("locator", "Ring")))]
    try:
        _load()
        light = (("Light", (("aperture", CLASSIC["aperture"].name),)),)
        found = [(title, tuple((effect_id, effect(effect_id).name) for effect_id in effect_ids))
                 for _module, title, effect_ids, _packs in DIRECTIONS]
    except Exception:
        return groups
    return groups + list(light) + found


def switch_effect(choice, crossing_style):
    """The Effect a switch plays for the `choice` setting beside the crossing's `crossing_style`."""
    style = crossing_style if choice == "match" else choice
    return effect(style) if style in EFFECT_IDS else LOCATOR


def pack(pack_id):
    """(name, colours) for a pack id, or None."""
    return _load()[1].get(pack_id)


def draw(effect_obj, kind, s, origin=(0.0, 0.0)):
    """One frame of `kind` ("depart" or "arrive") into a new Pen, or None when it draws nothing.
    `origin` shifts everything drawn, for a scene holding more than one screen. A failing effect
    raises; the caller logs it once and turns the effects off for the run."""
    limit = effect_obj.depart_seconds if kind == "depart" else effect_obj.arrive_seconds
    since = getattr(s, "since", None)
    if since is not None and since > limit + 0.05:
        return None
    if kind == "depart" and since is None and s.pressure <= 0.0:
        return None
    pen = Pen()
    if origin != (0.0, 0.0):
        pen.translate(*origin)
    (effect_obj.depart if kind == "depart" else effect_obj.arrive)(pen, s)
    if kind == "depart" and since is None and s.pressure < PUSH_FADE:
        # Several designs' reduced-motion pushes draw at a floor opacity however light the push, so
        # the first touch and the drain to nothing each popped by that floor.
        k = s.pressure / PUSH_FADE
        pen.ops = [op[:3] + (op[3] * k,) + op[4:] for op in pen.ops]
    return pen if pen.ops else None


# The pressure below which a push fades in and out as a whole.
PUSH_FADE = 0.1


PREVIEW_SCREEN = (800.0, 450.0)
PREVIEW_GAP = 40.0
PREVIEW_NOTCH = {"x": 307.5, "y": 0.0, "w": 185.0, "h": 32.0, "r": 10.0}
PREVIEW_LOOP_S = 4.4
_P_GLIDE, _P_PUSH, _P_CROSS, _P_REST = 0.4, 0.6, 2.2, 3.4


def preview_size(method):
    """The preview scene's size in its own points: two screens side by side for an edge or a
    corner, stacked with the PC above for the notch. The Design pages scale it to fit."""
    w, h = PREVIEW_SCREEN
    if method == "switch":
        return (w, h)
    return (w, h * 2 + PREVIEW_GAP) if method == "notch" else (w * 2 + PREVIEW_GAP, h)


def preview_scene(fx, t, method, palette, reduced=False, dark=True, pace=1.0, effect_size="medium"):
    """The Design pages' loop, identical on both apps: the pointer glides to the Mac's boundary,
    pushes through with three ticks, lands on the PC, and rests. Returns dict(screens=[(x, y, w,
    h, os)], notch=(x, y, w, h, r) or None, pointer=(x, y, os) or None, pens=[Pen]) for time `t`
    seconds; the caller draws the screens, then the pens, then the pointer, then the notch."""
    t = t % PREVIEW_LOOP_S
    w, h = PREVIEW_SCREEN
    if method == "notch":
        pc_origin, mac_origin = (0.0, 0.0), (0.0, h + PREVIEW_GAP)
        region = {"kind": "notch", "edge": "top", "x": PREVIEW_NOTCH["x"], "y": 0.0, "w": PREVIEW_NOTCH["w"], "h": 1.0}
        at, start = (w / 2.0, 0.0), (w / 2.0 - 90.0, 220.0)
        land, land_edge, inside = (w / 2.0 + 10.0, h - 2.0), "bottom", (w / 2.0 + 40.0, h - 140.0)
        notch = PREVIEW_NOTCH
    else:
        mac_origin, pc_origin = (0.0, 0.0), (w + PREVIEW_GAP, 0.0)
        if method == "corner":
            region = {"kind": "corner", "edge": "right", "corner": "top_right", "x": w - 8.0, "y": 0.0, "w": 8.0, "h": 8.0}
            at, start, land, inside = (w - 1.0, 0.0), (w - 220.0, 160.0), (2.0, 4.0), (200.0, 140.0)
        else:
            region = {"kind": "edge", "edge": "right", "x": w - 1.0, "y": 0.0, "w": 1.0, "h": h}
            at, start, land, inside = (w - 1.0, h * 0.45), (w - 260.0, h * 0.55), (2.0, h * 0.45), (240.0, h * 0.5)
        land_edge, notch = "left", None
    scene = {
        "screens": [mac_origin + (w, h, "mac"), pc_origin + (w, h, "windows")],
        "notch": None if notch is None else (mac_origin[0] + notch["x"], mac_origin[1], notch["w"], notch["h"], notch["r"]),
        "pointer": None,
        "pens": [],
    }
    common = dict(reduced=reduced, dark=dark)
    palette = legible(palette, dark, fx)
    mac_notch = notch if method == "notch" else None
    if t < _P_CROSS:
        if t < _P_GLIDE:
            u = ease_in_out(t / _P_GLIDE)
            p = (lerp(start[0], at[0], u), lerp(start[1], at[1], u))
        else:
            p = at
        scene["pointer"] = (mac_origin[0] + p[0], mac_origin[1] + p[1], "mac")
        if t >= _P_PUSH:
            u = (t - _P_PUSH) / (_P_CROSS - _P_PUSH)
            pressure = clamp(0.5 - 0.5 * math.cos(math.pi * u))
            index = min(3, int(pressure * 4.0))
            tick = None
            if index:
                crossed_at = _P_PUSH + (_P_CROSS - _P_PUSH) * math.acos(1.0 - 2.0 * index / 4.0) / math.pi
                tick = SimpleNamespace(index=index, age=t - crossed_at)
            s = state(w, h, "mac", method, palette, notch=mac_notch, region=SimpleNamespace(**region),
                      point=point(*at), along=at[1] / h if region["edge"] == "right" else at[0] / w,
                      pressure=pressure, tick=tick, since=None, effect_size=effect_size, **common)
            pen = draw(fx, "depart", s, mac_origin)
            if pen is not None:
                scene["pens"].append(pen)
        return scene
    since = (t - _P_CROSS) / pace
    s = state(w, h, "mac", method, palette, notch=mac_notch, region=SimpleNamespace(**region), point=point(*at),
              along=0.5, pressure=0.0, tick=None, since=since, effect_size=effect_size, **common)
    pen = draw(fx, "depart", s, mac_origin)
    if pen is not None:
        scene["pens"].append(pen)
    s = state(w, h, "windows", method, palette, point=point(*land), edge=land_edge, since=since,
              effect_size=effect_size, **common)
    pen = draw(fx, "arrive", s, pc_origin)
    if pen is not None:
        scene["pens"].append(pen)
    u = ease_in_out(clamp((t - _P_CROSS - 0.1) / (_P_REST - _P_CROSS - 0.1)))
    p = (lerp(land[0], inside[0], u), lerp(land[1], inside[1], u))
    scene["pointer"] = (pc_origin[0] + p[0], pc_origin[1] + p[1], "windows")
    return scene


# How long a switch arrival waits for a crossing arrival that would replace it. Both come in over
# the LAN within a few milliseconds of each other.
SWITCH_SETTLE_S = 0.12


SWITCH_LOOP_S = 2.0
_S_START = 0.35


def preview_switch_scene(fx, t, palette, os_name="mac", reduced=False, dark=True, pace=1.0, effect_size="medium"):
    """The Design pages' loop for what a switch plays, in preview_scene's shape: one screen, the
    pointer still near its middle, and `fx`'s switch arrival round it every SWITCH_LOOP_S."""
    t = t % SWITCH_LOOP_S
    w, h = PREVIEW_SCREEN
    at = (w * 0.46, h * 0.44)
    scene = {"screens": [(0.0, 0.0, w, h, os_name)], "notch": None, "pointer": (at[0], at[1], os_name), "pens": []}
    since = (t - _S_START) / pace
    if since >= 0.0:
        s = state(w, h, os_name, "switch", legible(palette, dark, fx), reduced=reduced, dark=dark,
                  effect_size=effect_size, point=point(*at),
                  edge=nearest_edge(at[0], at[1], w, h), since=since)
        pen = draw(fx, "arrive", s)
        if pen is not None:
            scene["pens"].append(pen)
    return scene


# The Design pages' Length: how long an effect takes to play through once the pointer crosses, and
# to land, as a multiple of its own timing. The push itself follows the hand, so it has no pace.
LENGTHS = (("short", "Short"), ("normal", "Normal"), ("long", "Long"))
SIZES = (("small", "Small"), ("medium", "Medium"), ("large", "Large"))
PACES = {"short": 0.5, "normal": 1.0, "long": 2.0}
_SIZE_FRACTIONS = {"small": 0.03, "medium": 0.045, "large": 0.06}
_MIN_EDGE_DEPTH = 24.0
_MAX_EDGE_DEPTH = 144.0
_EFFECT_REFERENCE_DEPTH = 36.0


def edge_depth(screen_w, screen_h, size="medium"):
    """The edge band's depth in screen units, scaled to the shorter side of its display."""
    fraction = _SIZE_FRACTIONS.get(size, _SIZE_FRACTIONS["medium"])
    return max(_MIN_EDGE_DEPTH, min(_MAX_EDGE_DEPTH, min(float(screen_w), float(screen_h)) * fraction))


def effect_depth_scale(s):
    """Scale material depths from their 1200 by 800 reference canvas to this screen and Size."""
    return edge_depth(s.screen.w, s.screen.h, getattr(s, "effect_size", "medium")) / _EFFECT_REFERENCE_DEPTH


def pace(length):
    return PACES.get(length, 1.0)


class Player:
    """The timeline of one machine's effects, fed the same events the glow is fed today.

    `push` while pressure builds against a boundary, `cross` when it gives, `arrive` when the
    pointer lands on this machine by a crossing, `switched` when input arrives any other way, then
    `frames(now)` returns the Pens to draw, or an empty list once everything has played out. Both a
    departure (finishing after a cross) and an arrival can be in flight at once. Coordinates are
    points relative to the desktop's top-left corner.

    Every event may name its `display`, (x, y, w, h) in the same coordinates: the effect then plays
    on that display alone, as the designs were drawn for one screen, and its Pen is clipped to it.
    Without one it plays on the whole `screen` given to `frames`."""

    def __init__(self, pace=1.0):
        self.depart = None
        self.arrival = None
        self.crossed_at = None
        # How long everything after the push takes, as a multiple of each effect's own timing.
        self.pace = pace
        self.effect_size = "medium"

    def push(self, now, method, region, at, pressure, tick_index=None, display=None):
        """`region` is dict(kind, edge, corner?, x, y, w, h); `at` is (x, y) of the pinned pointer.
        `tick_index` is the quarter just passed (1 to 3), if one was."""
        d = self.depart
        if d is None or d["crossed_at"] is not None:
            d = self.depart = {"method": method, "tick": None, "crossed_at": None, "display": None}
        # A push that goes on without naming its display, as a drain between events does, stays
        # on the one it started on.
        d.update(region=dict(region), at=at, pressure=clamp(pressure), method=method,
                 display=d["display"] if display is None else display)
        if tick_index:
            d["tick"] = (int(tick_index), now)

    def cross(self, now):
        if self.depart is not None:
            self.depart.update(crossed_at=now, pressure=0.0)

    def release(self):
        """The push stopped short: pressure is already draining through `push` calls, so this
        only forgets a departure that has drained to nothing."""
        if self.depart is not None and self.depart["crossed_at"] is None and self.depart["pressure"] <= 0.0:
            self.depart = None

    def arrive(self, now, method, at, edge, display=None):
        """A crossing landed the pointer here. It replaces a switch arrival still waiting to start."""
        self.arrival = {"method": method, "at": at, "edge": edge, "started": now, "switch": False, "display": display}
        self.crossed_at = now

    def crossed_in(self, now):
        """A crossing landed the pointer here and something else draws it, as Glow and Beam do: a
        switch arrival gives way to it all the same."""
        if self.arrival is not None and self.arrival["switch"]:
            self.arrival = None
        self.crossed_at = now

    def switched(self, now, at, edge, fx=None, display=None):
        """Input came here by the shortcut, a menu or a switch rather than a crossing. It starts
        SWITCH_SETTLE_S later and gives way to a crossing in that time: one machine's crossing can
        send this machine's own input home over one link while its pointer lands here over the
        other, and only the crossing should show. `fx` is what it plays when that is not the
        effect `frames` is given, which the Design pages let differ from the crossing's."""
        if self.crossed_at is not None and now - self.crossed_at < SWITCH_SETTLE_S:
            return
        self.arrival = {"method": "switch", "at": at, "edge": edge, "started": now + SWITCH_SETTLE_S, "switch": True,
                        "fx": fx, "display": display}

    def frames(self, now, fx, screen, palette, reduced, dark, notch=None):
        """Pens for this frame. `screen` is (w, h, os name); `notch` the Mac's dict(x, y, w, h, r)."""
        pens = []
        w, h, os_name = screen
        d = self.depart
        if d is not None:
            since = None if d["crossed_at"] is None else (now - d["crossed_at"]) / self.pace
            if since is not None and since > fx.depart_seconds + 0.05:
                self.depart = None
            elif since is not None or d["pressure"] > 0.0:
                frame, local_notch = _frame(d.get("display"), w, h, notch)
                ox, oy, fw, fh = frame
                region = _region_on(d["region"], frame)
                x, y = d["at"][0] - ox, d["at"][1] - oy
                along = clamp(y / fh if region["edge"] in ("left", "right") else x / fw)
                # A tick is the push's: the designs' own departures play with none, and one carried
                # past the crossing flashed a quarter mark into Rule's shutter.
                tick = None
                if d["tick"] is not None and since is None:
                    tick = SimpleNamespace(index=d["tick"][0], age=now - d["tick"][1])
                s = state(fw, fh, os_name, d["method"], legible(palette, dark, fx), reduced=reduced, dark=dark,
                          notch=local_notch, effect_size=self.effect_size, region=SimpleNamespace(**region), point=point(x, y), along=along,
                          pressure=d["pressure"], tick=tick, since=since)
                pen = _clipped(draw(fx, "depart", s, (ox, oy)), frame)
                if pen is not None:
                    pens.append(pen)
        a = self.arrival
        if a is not None:
            since = (now - a["started"]) / self.pace
            arrival_fx = a.get("fx") or fx
            if since > arrival_fx.arrive_seconds + 0.05:
                self.arrival = None
            elif since >= 0.0:
                frame, local_notch = _frame(a.get("display"), w, h, notch)
                ox, oy, fw, fh = frame
                # A switch lands wherever the pointer was, often below the notch but nowhere near it,
                # and effects that see a notch above the pointer draw round the notch.
                s = state(fw, fh, os_name, a["method"], legible(palette, dark, arrival_fx), reduced=reduced,
                          dark=dark, notch=None if a["switch"] else local_notch, effect_size=self.effect_size,
                          point=point(a["at"][0] - ox, a["at"][1] - oy), edge=a["edge"], since=since)
                pen = _clipped(draw(arrival_fx, "arrive", s, (ox, oy)), frame)
                if pen is not None:
                    pens.append(pen)
        return pens

    @property
    def busy(self):
        return self.depart is not None or self.arrival is not None


def _frame(display, w, h, notch):
    """The (x, y, w, h) an effect plays in, and the notch in that frame's own coordinates, or None
    when the notch is not on it."""
    if display is None:
        return (0.0, 0.0, float(w), float(h)), notch
    x, y, dw, dh = (float(v) for v in display)
    if notch is not None and x <= notch["x"] and notch["x"] + notch["w"] <= x + dw and y <= notch["y"] < y + dh:
        notch = dict(notch, x=notch["x"] - x, y=notch["y"] - y)
    else:
        notch = None
    return (x, y, dw, dh), notch


def _region_on(region, frame):
    """`region` in `frame`'s coordinates and cut to it: an edge strip measured along the whole
    desktop runs past a shorter display, and an effect drawing along its strip would draw there."""
    x, y, w, h = frame
    left, top = max(region["x"] - x, 0.0), max(region["y"] - y, 0.0)
    right, bottom = min(region["x"] - x + region["w"], w), min(region["y"] - y + region["h"], h)
    return dict(region, x=left, y=top, w=max(right - left, 1.0), h=max(bottom - top, 1.0))


def _clipped(pen, frame):
    """`pen` clipped to `frame`, its bounds cut to match, or None when nothing is left inside."""
    if pen is None or pen.bounds is None:
        return pen
    x, y, w, h = frame
    b = pen.bounds
    box = (max(b[0], x), max(b[1], y), min(b[2], x + w), min(b[3], y + h))
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    pen.bounds = box
    pen.clip = (x, y, x + w, y + h)
    return pen
