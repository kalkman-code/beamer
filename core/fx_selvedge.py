"""Selvedge: tension opens a woven seam; the threads stay continuous and mend behind you."""

import math

from .effects import Effect, clamp, ease_out, effect_depth_scale
from .fx_material import stroke, surface


def _cloth(pen, s, level, departing):
    duration = (0.48, 0.64, 0.78)[level]
    t = s.since
    if t is not None and t >= duration:
        return
    pushing = departing and t is None
    p = clamp(s.pressure) if pushing else 1.0
    u = 0.0 if pushing else clamp(t / duration)
    size = effect_depth_scale(s)
    alpha = clamp(p * 5) if pushing else 1 - clamp((u - 0.55) / 0.45)
    tension = 0.55 if s.reduced else p
    opening = 0.0
    if not pushing:
        if s.reduced:
            alpha = 1 - u
        elif departing:
            opening = math.sin(math.pi * ease_out(u)) * 29 * size
            tension = 1 - ease_out(u)
        else:
            opening = 24 * size * (1 - ease_out(clamp(u / 0.65)))
            tension = 0.65 * (1 - ease_out(u))
    at = surface(pen, s, departing)
    span = (48, 88, 132)[level]
    rows = (2, 4, 6)[level]
    samples = (8, 10, 12)[level]
    palette = s.palette
    # The warp stays anchored at the wall while the weft tightens across it.
    if level:
        pen.beginPath()
        for i in range(-8 if level == 2 else -5, 9 if level == 2 else 6):
            x = i * 14
            envelope = max(0, 1 - abs(x) / span)
            y = (5 + (rows - 1) * 4 + 13 * tension * envelope) * size
            pen.moveTo(*at(x, 2 * size))
            pen.lineTo(*at(x + math.copysign(opening * envelope, x or 1), y))
        stroke(pen, palette[1 % len(palette)], 1.0, alpha * 0.62, s.dark)
    for pigment, colour in enumerate(palette):
        pen.beginPath()
        for row in range(rows):
            if row % len(palette) != pigment:
                continue
            def yarn(x):
                envelope = max(0, 1 - (x / span) ** 2)
                lift = (10 + level * 5) * tension * envelope * size
                split = opening * envelope * (1 if row % 2 else -0.12)
                return max(2 * size, 7 * size + row * 4 * size + lift + split)
            pen.moveTo(*at(-span, yarn(-span)))
            step = 2 * span / samples
            for j in range(samples):
                a, b = -span + j * step, -span + (j + 1) * step
                # One cubic per half weave replaces a polyline's dense sampled geometry.
                weave = (3 if level < 2 else 7) * (1 - 0.65 * tension) * size * (-1 if (j + row) % 2 else 1)
                c1, c2 = a + step / 3, b - step / 3
                pen.bezierCurveTo(*at(c1, max(2, yarn(c1) + weave)),
                                  *at(c2, max(2, yarn(c2) + weave)), *at(b, yarn(b)))
        stroke(pen, colour, 1.8 if level == 0 else 1.6, alpha, s.dark)
    if level:
        # Short overpasses hide the warp at alternating crossings, making an actual weave.
        pen.beginPath()
        for i in range(-4, 5):
            x = i * 14
            envelope = max(0, 1 - (x / span) ** 2)
            y = (9 + (10 + level * 5) * tension * envelope) * size + opening * envelope
            pen.moveTo(*at(x - 2, y - size))
            pen.lineTo(*at(x + 2, y + size))
        stroke(pen, palette[-1], 2.1, alpha * 0.85, s.dark)


def _make(id_, name, level, blurb):
    return Effect(id_, name, ("quiet", "medium", "showpiece")[level], blurb,
                  lambda pen, s: _cloth(pen, s, level, True),
                  lambda pen, s: _cloth(pen, s, level, False),
                  (0.48, 0.64, 0.78)[level], (0.48, 0.64, 0.78)[level])


EFFECTS = [
    _make("thread", "Thread", 0, "Two fine yarns tighten, part for the pointer and stitch the seam closed behind it."),
    _make("weave", "Weave", 1, "A woven border gathers tension, opens between its strands and gently mends on arrival."),
    _make("jacquard", "Jacquard", 2, "A diamond-patterned textile tightens across the edge, opens its weave and settles behind you."),
]

PACKS = {
    "woad": ("Woad", ["#8277b3", "#7268a4", "#c3b9df"]),
    "saffron": ("Saffron", ["#d48238", "#976025", "#efc18a"]),
    "flax": ("Flax", ["#cfb885", "#8a9c84", "#efdfb8"]),
    "madder": ("Madder", ["#c45b68", "#873d57", "#e5b09b"]),
    "tide": ("Tide", ["#58a9a3", "#547498", "#b1d1c1"]),
}
