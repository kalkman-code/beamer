"""The third Light effect: a narrow lens opens at the crossing, then closes behind it."""

import math

from .effects import clamp, ease_out, effect_depth_scale, parse_colour
from .fx_material import stroke, surface


def _aperture(pen, s, departing):
    t = s.since
    if t is not None and t >= 0.62:
        return
    pushing = departing and t is None
    p = clamp(s.pressure) if pushing else 1.0
    size = effect_depth_scale(s)
    u = 0.0 if pushing else clamp(t / 0.62)
    alpha = clamp(p * 4) if pushing else 1 - u
    opening = 0.65 if s.reduced else p
    if not pushing and not s.reduced:
        opening = (1 - ease_out(u)) if not departing else 1 + math.sin(math.pi * u) * 0.8
    at = surface(pen, s, departing)
    span = 38 + 52 * opening
    depth = (3 + 13 * opening) * size
    pen.beginPath()
    # A fixed path budget also carries the lens cleanly round corners and the notch.
    for half in (0, 1):
        for j in range(17):
            x = (-span + 2 * span * j / 16) * (1 if half == 0 else -1)
            envelope = max(0, 1 - (x / span) ** 2)
            y = 3 * size + envelope * depth * (1 if half == 0 else 0.18)
            (pen.moveTo if half == 0 and j == 0 else pen.lineTo)(*at(x, y))
    pen.closePath()
    a, b = at(-span, 0), at(span, 0)
    gradient = pen.createLinearGradient(*a, *b)
    palette = s.palette
    for i, colour in enumerate(palette):
        r, g, blue, _ = parse_colour(colour)
        gradient.addColorStop(i / max(1, len(palette) - 1), (r, g, blue, 0.24))
    pen.fillStyle, pen.globalAlpha = gradient, alpha
    pen.fill()
    stroke(pen, palette[len(palette) // 2], 1.4, alpha, s.dark)
