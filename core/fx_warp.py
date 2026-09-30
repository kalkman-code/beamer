"""Warp: crossing is travel through a portal, the edge opening as a slot that stretches light and
the pointer's trail into speed lines, arriving as a streak that decelerates into the pointer.
"""

import math

from . import effects

PACKS = {
    "neon": ("Neon", ["#ff0055", "#00ffcc", "#ffcc00"]),
    "vaporwave": ("Vaporwave", ["#ff71ce", "#01cdfe", "#05ffa1", "#b967ff", "#fffb96"]),
    "cyber": ("Cyber", ["#fcee0a", "#00f0ff", "#ff003c"]),
}


def _normal_for_depart(s):
    nx, ny = 0, 0
    if s.region.edge == "right":
        nx = 1
    elif s.region.edge == "left":
        nx = -1
    elif s.region.edge == "top":
        ny = -1
    elif s.region.edge == "bottom":
        ny = 1

    if s.region.kind == "corner":
        # From the corner's name: the region's edge is the side the other machine is on, which
        # for a corner push is often the vertical side.
        corner = getattr(s.region, "corner", "")
        nx = 1 if "right" in corner else -1
        ny = -1 if "top" in corner else 1
        length = math.hypot(nx, ny)
        nx /= length
        ny /= length
    return nx, ny


def _normal_for_arrive(s):
    nx, ny = 0, 0
    if s.edge == "right":
        nx = -1
    elif s.edge == "left":
        nx = 1
    elif s.edge == "top":
        ny = 1
    elif s.edge == "bottom":
        ny = -1

    if s.edge in ("left", "right") and (s.point.y < 20 or s.point.y > s.screen.h - 20):
        ny = 1 if s.point.y < 20 else -1
        length = math.hypot(nx, ny)
        nx /= length
        ny /= length
    return nx, ny


def _draw_slot(ctx, s, p, thickness):
    r = s.region
    col = s.palette[0]

    if r.kind == "notch":
        n = s.notch
        out = thickness * p
        ctx.globalAlpha = p * 0.8
        ctx.fillStyle = col
        effects.round_rect(ctx, n.x - out, -20, n.w + out * 2, n.h + 20 + out, 14)
        ctx.fill()
        ctx.globalAlpha = p * 0.4
        ctx.fillStyle = "#fff"
        effects.round_rect(ctx, n.x - out / 2, -20, n.w + out, n.h + 20 + out / 2, 14)
        ctx.fill()
        return

    w = thickness * p
    length = 40 + 120 * p
    x = y = 0

    if r.kind == "corner":
        size = 8 + w
        corner = getattr(r, "corner", "top_right")
        right, bottom = "right" in corner, "bottom" in corner
        x0 = s.screen.w - size if right else 0
        y0 = s.screen.h - size if bottom else 0
        ctx.globalAlpha = p * 0.8
        ctx.fillStyle = col
        ctx.fillRect(x0, y0, size, size)
        ctx.fillStyle = "#fff"
        ctx.globalAlpha = p * 0.4
        ctx.fillRect(s.screen.w - size / 2 if right else 0, s.screen.h - size / 2 if bottom else 0, size / 2, size / 2)
        return

    ctx.globalAlpha = p * 0.8
    ctx.fillStyle = col
    if r.edge == "right":
        x, y = s.screen.w - w, s.point.y - length / 2
        ctx.fillRect(x, y, w, length)
    elif r.edge == "left":
        x, y = 0, s.point.y - length / 2
        ctx.fillRect(x, y, w, length)
    elif r.edge == "top":
        x, y = s.point.x - length / 2, 0
        ctx.fillRect(x, y, length, w)
    elif r.edge == "bottom":
        x, y = s.point.x - length / 2, s.screen.h - w
        ctx.fillRect(x, y, length, w)

    ctx.fillStyle = "#fff"
    ctx.globalAlpha = p * 0.4
    w = w / 2
    if r.edge == "right":
        x, y = s.screen.w - w, s.point.y - length / 2
        ctx.fillRect(x, y, w, length)
    elif r.edge == "left":
        x, y = 0, s.point.y - length / 2
        ctx.fillRect(x, y, w, length)
    elif r.edge == "top":
        x, y = s.point.x - length / 2, 0
        ctx.fillRect(x, y, length, w)
    elif r.edge == "bottom":
        x, y = s.point.x - length / 2, s.screen.h - w
        ctx.fillRect(x, y, length, w)


def _draw_converge(ctx, s, p, max_len, thickness, num_lines, colour):
    """A switch, away from any edge: the streaks come in from all round and decelerate onto the
    pointer. Reduce motion holds them still round it and only fades them."""
    count = max(6, num_lines)
    ctx.lineCap = "round"
    for i in range(count):
        rand = s.hash(i)
        angle = math.pi * 2 * (i + 0.35 * (rand - 0.5)) / count
        dx, dy = math.cos(angle), math.sin(angle)
        if s.reduced:
            inner, length = 14, max_len * 0.25 * (0.6 + 0.4 * rand)
        else:
            inner, length = 12 + max_len * 0.45 * p * p, max_len * 0.45 * p * (0.5 + 0.5 * rand)
        ctx.globalAlpha = p * (0.5 + 0.5 * rand)
        ctx.strokeStyle = colour
        ctx.lineWidth = thickness * (0.5 + 0.5 * rand)
        ctx.beginPath()
        ctx.moveTo(s.point.x + dx * (inner + length), s.point.y + dy * (inner + length))
        ctx.lineTo(s.point.x + dx * inner, s.point.y + dy * inner)
        ctx.stroke()


def _draw_arrival(ctx, s, p, max_len, thickness, num_lines, colour):
    if s.method == "switch":
        _draw_converge(ctx, s, p, max_len, thickness, num_lines, colour)
        return
    if s.reduced:
        # A fixed slot along the edge where the pointer came in, so Reduce motion still marks the
        # landing; the design's lone dot, a few points across, did not.
        nx, ny = _normal_for_arrive(s)
        tx, ty = -ny, nx
        half = 28 + thickness * 2
        ctx.globalAlpha = p
        ctx.lineCap = "round"
        ctx.strokeStyle = "rgba(0,0,0,0.45)" if not s.dark else "rgba(0,0,0,0.25)"
        ctx.lineWidth = thickness + 3
        ctx.beginPath()
        ctx.moveTo(s.point.x - tx * half, s.point.y - ty * half)
        ctx.lineTo(s.point.x + tx * half, s.point.y + ty * half)
        ctx.stroke()
        ctx.strokeStyle = colour
        ctx.lineWidth = thickness
        ctx.stroke()
        ctx.fillStyle = colour
        ctx.beginPath()
        ctx.arc(s.point.x + nx * 10, s.point.y + ny * 10, thickness, 0, math.pi * 2)
        ctx.fill()
        return
    nx, ny = _normal_for_arrive(s)
    dx, dy = nx, ny

    for i in range(num_lines):
        rand = s.hash(i)
        offset = (rand - 0.5) * 40
        ox = dy * offset
        oy = -dx * offset
        length = max_len * p * (0.5 + 0.5 * rand)

        ctx.globalAlpha = p * (0.5 + 0.5 * rand)
        ctx.strokeStyle = colour
        ctx.lineWidth = thickness * (0.5 + 0.5 * rand)
        ctx.lineCap = "round"
        ctx.beginPath()
        ctx.moveTo(s.point.x + ox, s.point.y + oy)
        ctx.lineTo(s.point.x + ox + dx * length, s.point.y + oy + dy * length)
        ctx.stroke()

    ctx.globalAlpha = p
    ctx.strokeStyle = "#fff"
    ctx.lineWidth = thickness * 0.5
    ctx.beginPath()
    ctx.moveTo(s.point.x, s.point.y)
    ctx.lineTo(s.point.x + dx * max_len * p, s.point.y + dy * max_len * p)
    ctx.stroke()


def _draw_depart_trails(ctx, s, p, max_len, thickness, num_lines, colour):
    if s.reduced:
        return
    nx, ny = _normal_for_depart(s)
    dx, dy = -nx, -ny
    for i in range(num_lines):
        rand = s.hash(i + 100)
        offset = (rand - 0.5) * (140 if s.region.kind == "notch" else 60)
        ox = dy * offset
        oy = -dx * offset
        length = max_len * p * (0.5 + 0.5 * rand)

        ctx.globalAlpha = p * (0.5 + 0.5 * rand)
        ctx.strokeStyle = colour
        ctx.lineWidth = thickness * rand
        ctx.lineCap = "round"
        ctx.beginPath()
        ctx.moveTo(s.point.x + ox, s.point.y + oy)
        ctx.lineTo(s.point.x + ox + dx * length, s.point.y + oy + dy * length)
        ctx.stroke()


def _light_slit_depart(ctx, s):
    p = s.pressure
    t = s.since
    if t is not None:
        p = max(0.0, 1 - (t / 0.4))
    if p <= 0:
        return
    _draw_slot(ctx, s, p, 10)
    if t is not None:
        _draw_depart_trails(ctx, s, max(0.0, 1 - t / 0.4), 60, 4, 3, s.palette[0])


def _light_slit_arrive(ctx, s):
    p = max(0.0, 1 - (s.since / 0.4))
    if p <= 0:
        return
    _draw_arrival(ctx, s, p, 60, 4, 3, s.palette[len(s.palette) - 1])


def _hyperdrive_depart(ctx, s):
    p = s.pressure
    t = s.since
    if t is not None:
        p = max(0.0, 1 - (t / 0.6))
    if p <= 0:
        return
    _draw_slot(ctx, s, p, 20)
    if t is not None:
        _draw_depart_trails(ctx, s, max(0.0, 1 - t / 0.6), 100, 6, 8, s.palette[1 % len(s.palette)])
    elif p > 0.1:
        _draw_depart_trails(ctx, s, p, 40, 2, 4, s.palette[0])


def _hyperdrive_arrive(ctx, s):
    p = max(0.0, 1 - (s.since / 0.6))
    if p <= 0:
        return
    _draw_arrival(ctx, s, p, 100, 6, 8, s.palette[len(s.palette) - 1])


def _wormhole_depart(ctx, s):
    p = s.pressure
    t = s.since
    if t is not None:
        p = max(0.0, 1 - (t / 0.8))
    if p <= 0:
        return
    _draw_slot(ctx, s, p, 32)
    if t is not None:
        _draw_depart_trails(ctx, s, max(0.0, 1 - t / 0.8), 160, 8, 15, s.palette[2 % len(s.palette)])
    elif p > 0.1:
        _draw_depart_trails(ctx, s, p, 80, 4, 8, s.palette[1 % len(s.palette)])


def _wormhole_arrive(ctx, s):
    p = max(0.0, 1 - (s.since / 0.8))
    if p <= 0:
        return
    _draw_arrival(ctx, s, p, 160, 8, 15, s.palette[len(s.palette) - 1])


EFFECTS = [
    effects.Effect("light_slit", "Light slit", "quiet", "A subtle slit opens, pulling in faint trails.",
                    _light_slit_depart, _light_slit_arrive, 0.4, 0.4),
    effects.Effect("hyperdrive", "Hyperdrive", "medium", "Speed lines accelerate towards the portal.",
                    _hyperdrive_depart, _hyperdrive_arrive, 0.6, 0.6),
    effects.Effect("wormhole", "Wormhole", "showpiece", "A dynamic stretching grid of light.",
                    _wormhole_depart, _wormhole_arrive, 0.8, 0.8),
]
