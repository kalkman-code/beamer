"""Folio: a scored edge lifts into paper folds and lays itself flat on arrival."""

import math

from .effects import Effect, clamp, ease_out, effect_depth_scale, lerp
from .fx_material import stroke, surface


def _sheet(pen, s, level, departing):
    duration = (0.48, 0.62, 0.78)[level]
    t = s.since
    if t is not None and t >= duration:
        return
    pushing = departing and t is None
    p = clamp(s.pressure) if pushing else 1.0
    u = 0.0 if pushing else clamp(t / duration)
    size = effect_depth_scale(s)
    alpha = clamp(p * 5) if pushing else 1 - clamp((u - 0.58) / 0.42)
    motion = 0.65 if s.reduced else p
    at = surface(pen, s, departing)
    span = (48, 90, 144)[level]
    count = (2, 6, 12)[level]
    depth = (12, 28, 46)[level] * size
    base = 3 * size
    wall = 2 * size
    gap = 0.0
    if not pushing and not s.reduced:
        if departing:
            gap = span * ease_out(clamp(u / 0.8))
            motion = 1 - ease_out(u)
        else:
            gap = span * (1 - ease_out(clamp(u / 0.55))) * 0.5
            motion = (1 - ease_out(u)) + 0.22 * math.sin(math.pi * u)
    if s.reduced and not pushing:
        alpha = 1 - u
    step = 2 * span / count
    palette = s.palette
    facets = []
    for i in range(count):
        a, b = -span + i * step, -span + (i + 1) * step
        side = -1 if (a + b) < 0 else 1
        a, b = a + side * gap, b + side * gap
        def ridge(j):
            envelope = math.sin(math.pi * j / count) ** 0.7
            return base + depth * motion * envelope * (1 if j % 2 else 0.64)
        ra, rb = ridge(i), ridge(i + 1)
        facets.append((i, a, b, ra, rb))
    # Batch by pigment: the native replayers otherwise rebuild dozens of identical brushes.
    for pigment, colour in enumerate(palette):
        pen.beginPath()
        for i, a, b, ra, rb in facets:
            if i % len(palette) == pigment:
                pen.moveTo(*at(a, wall))
                pen.lineTo(*at(a, ra))
                pen.lineTo(*at(b, rb))
                pen.lineTo(*at(b, wall))
                pen.closePath()
        pen.fillStyle, pen.globalAlpha = colour, alpha * 0.9
        pen.fill()
    pen.beginPath()
    for i, a, b, ra, rb in facets:
        if i % 2:
            pen.moveTo(*at(a, wall))
            pen.lineTo(*at(a, ra))
            pen.lineTo(*at(b, rb))
            pen.lineTo(*at(b, wall))
            pen.closePath()
    pen.fillStyle, pen.globalAlpha = "#000000", alpha * 0.18
    pen.fill()
    for pigment, colour in enumerate(palette):
        pen.beginPath()
        for i, a, b, ra, rb in facets:
            if (i + 1) % len(palette) == pigment:
                pen.moveTo(*at(a, wall))
                pen.lineTo(*at(a, ra))
                pen.lineTo(*at(b, rb))
        stroke(pen, colour, 1.0, alpha * 0.9, s.dark)
    if level == 2:
        pen.beginPath()
        for i, a, b, ra, rb in facets:
            for k in (0.34, 0.67):
                pen.moveTo(*at(a, lerp(wall, ra, k)))
                pen.lineTo(*at(b, lerp(wall, rb, k)))
        stroke(pen, palette[-1], 0.65, alpha * 0.45, s.dark)


def _make(id_, name, level, blurb):
    return Effect(id_, name, ("quiet", "medium", "showpiece")[level], blurb,
                  lambda pen, s: _sheet(pen, s, level, True),
                  lambda pen, s: _sheet(pen, s, level, False),
                  (0.48, 0.62, 0.78)[level], (0.48, 0.62, 0.78)[level])


EFFECTS = [
    _make("crease", "Crease", 0, "A small paper crease lifts under pressure, parts, then settles flat where you arrive."),
    _make("pleat", "Pleat", 1, "Six alternating folds gather at the edge, slide apart and unfold on the other screen."),
    _make("concertina", "Concertina", 2, "A scored paper fan rises along the boundary, opens a passage and folds itself away behind you."),
]

PACKS = {
    "vellum": ("Vellum", ["#c69a60", "#f0ddac", "#88674c"]),
    "carbon_copy": ("Carbon copy", ["#6688ae", "#bbcad9", "#344d72"]),
    "marbled": ("Marbled", ["#bd6858", "#6c9691", "#e1bb78"]),
}
