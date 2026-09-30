"""Instrument: crossing is a measurement taken, read off a scale one click at a time, closed by a
shutter where you left and locked on by the same instrument where you land.

Effects: Rule (quiet), Gauge (medium), Rangefinder (showpiece). Packs: Phosphor, Sodium, Contrast.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

from .effects import Effect, clamp, lerp

PI = math.pi
TAU = PI * 2.0
SQ = math.sqrt(0.5)


def roles(s):
    p = s.palette
    n = len(p)
    return SimpleNamespace(lit=p[0], dim=p[1 % n], hot=p[n - 1])


# Pen.arc's own one-cubic-Bezier-per-pi/2 tessellation reads as visibly faceted next to the design's
# native canvas arc at the dial and reticle radii drawn here. Doubling the density (pi/4 a piece)
# is the best trade-off found by measurement: finer still fixes some states and reopens others
# elsewhere in the same sweep, so this is the port's fix, not a tuned-away symptom.
def detent(u, n, slide=0.4):
    """n equal detents, each move taking the first `slide` of its slot and then holding."""
    if u <= 0:
        return 0.0
    if u >= 1:
        return 1.0
    k = math.floor(u * n)
    f = u * n - k
    return min(1.0, (k + clamp(f / slide)) / n)


def ink(ctx, s, colour, w, alpha, build):
    if alpha <= 0.003:
        return
    ctx.beginPath()
    build()
    ctx.lineCap = "butt"
    ctx.lineJoin = "miter"
    if s.dark:
        ctx.globalAlpha = alpha * 0.14
        ctx.strokeStyle = colour
        ctx.lineWidth = w + 5
        ctx.stroke()
    ctx.globalAlpha = alpha * (0.5 if s.dark else 0.62)
    ctx.strokeStyle = "#000"
    ctx.lineWidth = w + 2
    ctx.stroke()
    ctx.globalAlpha = alpha
    ctx.strokeStyle = colour
    ctx.lineWidth = w
    ctx.stroke()


def fill_with(ctx, colour, alpha, build):
    if alpha <= 0.003:
        return
    ctx.beginPath()
    build()
    ctx.globalAlpha = alpha
    ctx.fillStyle = colour
    ctx.fill()


def tick_to(ctx, ray, d, o1, o2):
    p = ray.at(d)
    ctx.moveTo(p.x + p.nx * o1, p.y + p.ny * o1)
    ctx.lineTo(p.x + p.nx * o2, p.y + p.ny * o2)


def rail_to(ctx, ray, d0, d1, o, move=True):
    n = max(1, math.ceil(abs(d1 - d0) / 3))
    for i in range(n + 1):
        p = ray.at(d0 + (d1 - d0) * i / n)
        x, y = p.x + p.nx * o, p.y + p.ny * o
        if i == 0 and move:
            ctx.moveTo(x, y)
        else:
            ctx.lineTo(x, y)


def band_to(ctx, ray, d0, d1, o1, o2):
    rail_to(ctx, ray, d0, d1, o1, True)
    rail_to(ctx, ray, d1, d0, o2, False)
    ctx.closePath()


# The hardware notch as a rail: down its left side, round the bottom, up its right side, and out
# along the top edge either way, so a scale can hug it and never draw inside the black.
def notch_track(n):
    r, v, q, b = n.r, n.h - n.r, PI / 2 * n.r, n.w - 2 * n.r
    L = 2 * v + 2 * q + b

    def at(t):
        if t < -1e-6:
            return SimpleNamespace(x=n.x + t, y=0, nx=0, ny=1)
        if t < v:
            return SimpleNamespace(x=n.x, y=max(0, t), nx=-1, ny=0)
        if t < v + q:
            a = PI - (t - v) / q * PI / 2
            c, si = math.cos(a), math.sin(a)
            return SimpleNamespace(x=n.x + r + r * c, y=v + r * si, nx=c, ny=si)
        if t < v + q + b:
            return SimpleNamespace(x=n.x + r + (t - v - q), y=n.h, nx=0, ny=1)
        if t < v + 2 * q + b:
            a = PI / 2 - (t - v - q - b) / q * PI / 2
            c, si = math.cos(a), math.sin(a)
            return SimpleNamespace(x=n.x + n.w - r + r * c, y=v + r * si, nx=c, ny=si)
        if t <= L + 1e-6:
            return SimpleNamespace(x=n.x + n.w, y=max(0, L - t), nx=1, ny=0)
        return SimpleNamespace(x=n.x + n.w + (t - L), y=0, nx=0, ny=1)

    def t_of(px):
        lx, rx = n.x + r, n.x + n.w - r
        if lx <= px <= rx:
            return v + q + (px - lx)
        if px < lx:
            return v + (PI - math.acos(clamp((px - lx) / r, -1, 0))) / (PI / 2) * q
        return v + q + b + (PI / 2 - math.acos(clamp((px - rx) / r, 0, 1))) / (PI / 2) * q

    return SimpleNamespace(L=L, at=at, tOf=t_of)


# Sets up the drawing frame for where the crossing happens and returns rays running outward from
# the pointer along the boundary. Edge and corner transform the context so local x runs along
# the edge and local y points into the screen; the notch stays in screen points.
def frame(ctx, s, depart):
    W, H, P = s.screen.w, s.screen.h, s.point
    right = bottom = False
    if depart:
        r = s.region
        kind, edge = r.kind, r.edge
        if kind == "corner":
            right = "right" in r.corner
            bottom = "bottom" in r.corner
    else:
        edge, kind = s.edge, "edge"
        n = getattr(s, "notch", None)
        if n and edge == "top" and n.x - 2 <= P.x <= n.x + n.w + 2:
            kind = "notch"
        else:
            cx = 0 if P.x < 24 else (1 if P.x > W - 24 else -1)
            cy = 0 if P.y < 24 else (1 if P.y > H - 24 else -1)
            if cx >= 0 and cy >= 0:
                kind = "corner"
                right, bottom = cx == 1, cy == 1
    if kind == "notch":
        tr = notch_track(s.notch)
        tp = tr.tOf(P.x)
        c = tr.at(tp)
        return SimpleNamespace(
            kind=kind, track=tr, tp=tp, centre=c, tangent=SimpleNamespace(x=-c.ny, y=c.nx),
            rays=[SimpleNamespace(at=lambda d: tr.at(tp + d), max=tr.L - tp),
                  SimpleNamespace(at=lambda d: tr.at(tp - d), max=tp)],
        )
    if kind == "corner":
        ctx.transform(-1 if right else 1, 0, 0, -1 if bottom else 1, W if right else 0, H if bottom else 0)
        return SimpleNamespace(
            kind=kind, centre=SimpleNamespace(x=0, y=0, nx=SQ, ny=SQ), tangent=SimpleNamespace(x=SQ, y=-SQ),
            rays=[SimpleNamespace(at=lambda d: SimpleNamespace(x=d, y=0, nx=0, ny=1), max=400),
                  SimpleNamespace(at=lambda d: SimpleNamespace(x=0, y=d, nx=1, ny=0), max=400)],
        )
    if edge == "right":
        ctx.transform(0, 1, -1, 0, W, P.y)
        lo, hi = P.y, H - P.y
    elif edge == "left":
        ctx.transform(0, 1, 1, 0, 0, P.y)
        lo, hi = P.y, H - P.y
    elif edge == "top":
        ctx.transform(1, 0, 0, 1, P.x, 0)
        lo, hi = P.x, W - P.x
    else:
        ctx.transform(1, 0, 0, -1, P.x, H)
        lo, hi = P.x, W - P.x
    return SimpleNamespace(
        kind="edge", centre=SimpleNamespace(x=0, y=0, nx=0, ny=1), tangent=SimpleNamespace(x=1, y=0),
        rays=[SimpleNamespace(at=lambda d: SimpleNamespace(x=d, y=0, nx=0, ny=1), max=hi),
              SimpleNamespace(at=lambda d: SimpleNamespace(x=-d, y=0, nx=0, ny=1), max=lo)],
    )


def tick_flash(s, span=0.22):
    return 1 - s.tick.age / span if s.tick is not None and s.tick.age < span else 0


def comb(ctx, items):
    def build():
        for ray, d, a, b in items:
            tick_to(ctx, ray, d, a, b)
    return build


def normal_line(ctx, c, o1, o2):
    def build():
        ctx.moveTo(c.x + c.nx * o1, c.y + c.ny * o1)
        ctx.lineTo(c.x + c.nx * o2, c.y + c.ny * o2)
    return build


def rule_depart(ctx, s):
    C, SP, N = roles(s), 6, 12
    p, reach, done = s.pressure, SP * N, False
    if s.since is None:
        alpha = clamp(p * 3)
    else:
        u = s.since / 0.45
        if u >= 1:
            return
        p, done = 1, True
        if s.reduced:
            alpha = 1 - detent(u, 4, 0.25)
        else:
            reach = SP * N * (1 - detent(u, 4))
            alpha = 1 - clamp((u - 0.75) / 0.25)
    if alpha <= 0:
        return
    flash, k = tick_flash(s), frame(ctx, s, True)
    off, on, hot = [], [], []
    for ray in k.rays:
        for i in range(1, N + 1):
            d = i * SP
            if d > ray.max or d > reach + 0.01:
                break
            major = i % 3 == 0
            length = 10 if major else 5
            (on if (done or i / N <= p + 1e-6) else off).append((ray, d, 2, 2 + length))
            if flash and major and i == s.tick.index * 3:
                hot.append((ray, d, 2, 2 + length + (0 if s.reduced else 7 * flash)))
    if reach > 0.5:
        def rails():
            for ray in k.rays:
                rail_to(ctx, ray, 0, min(reach, ray.max), 2)
        ink(ctx, s, C.dim, 1, alpha * 0.45, rails)
    ink(ctx, s, C.dim, 1.25, alpha * 0.5, comb(ctx, off))
    ink(ctx, s, C.hot if done else C.lit, 1.25, alpha, comb(ctx, on))
    ink(ctx, s, C.hot, 1.5, alpha * flash, comb(ctx, hot))
    ink(ctx, s, C.hot if done else C.lit, 1.5, alpha, normal_line(ctx, k.centre, 2, 17))


def rule_arrive(ctx, s):
    t = s.since
    if t >= 0.6:
        return
    C = roles(s)
    c = 1 if s.reduced else detent(t / 0.28, 3)
    locked = t >= 0.26
    flash = clamp(1 - (t - 0.26) / 0.12) if locked else 0
    alpha = 1 if t < 0.38 else 1 - (t - 0.38) / 0.22
    if s.method == "switch":
        # A switch came in by no edge, so there is no register to mark: only the cross.
        rule_cross(ctx, s, C, c, locked, flash, alpha)
        return
    ctx.save()
    k = frame(ctx, s, False)

    def ticks():
        for ray in k.rays:
            for i in range(1, 5):
                if i * 6 <= ray.max:
                    tick_to(ctx, ray, i * 6, 2, 10 if i == 4 else 6)
    ink(ctx, s, C.lit, 1.25, alpha * 0.9, ticks)
    ink(ctx, s, C.lit, 1.5, alpha, normal_line(ctx, k.centre, 2, 12))
    if k.kind == "notch":
        o = lerp(28, 12, c)

        def hug():
            for ray in k.rays:
                rail_to(ctx, ray, 0, min(38, ray.max), o)
        ink(ctx, s, C.lit if locked else C.dim, 1.25, alpha, hug)
        ink(ctx, s, C.hot, 2, alpha * flash, hug)
        ink(ctx, s, C.hot, 1.5, alpha, lambda: tick_to(ctx, k.rays[0], 0, o, o + 10))
        ctx.restore()
        return
    ctx.restore()
    rule_cross(ctx, s, C, c, locked, flash, alpha)


def rule_cross(ctx, s, C, c, locked, flash, alpha):
    r, P = lerp(40, 22, c), s.point

    def arms():
        for a in range(4):
            dx, dy = math.cos(a * PI / 2), math.sin(a * PI / 2)
            ctx.moveTo(P.x + dx * r, P.y + dy * r)
            ctx.lineTo(P.x + dx * (r + 9), P.y + dy * (r + 9))
    ink(ctx, s, C.lit if locked else C.dim, 1.5, alpha, arms)
    ink(ctx, s, C.hot, 2, alpha * flash, arms)


def gauge_notch(ctx, s, k, C):
    tr, J = k.track, 32
    L = tr.L
    ray = SimpleNamespace(at=tr.at)
    if s.since is None:
        p = s.pressure
        alpha = clamp(p * 3)
        q = detent(p, 16, 0.5)
        lit_to = (math.floor(p * 16 + 1e-6) / 16) if s.reduced else q
        flash = tick_flash(s)
        off, on, hot = [], [], []
        for j in range(J + 1):
            major = j % 8 == 0
            length = 10 if major else 5
            (on if j / J <= lit_to + 1e-6 else off).append((ray, j * L / J, 4, 4 + length))
            if flash and j == s.tick.index * 8:
                hot.append((ray, j * L / J, 4, 4 + length + (0 if s.reduced else 7 * flash)))
        ink(ctx, s, C.dim, 1, alpha * 0.45, lambda: rail_to(ctx, ray, 0, L, 2))
        if lit_to > 0:
            ink(ctx, s, C.lit, 2, alpha, lambda: rail_to(ctx, ray, 0, L * lit_to, 2))
        ink(ctx, s, C.dim, 1.25, alpha * 0.5, comb(ctx, off))
        ink(ctx, s, C.lit, 1.25, alpha, comb(ctx, on))
        ink(ctx, s, C.hot, 1.5, alpha * flash, comb(ctx, hot))
        if not s.reduced:
            ink(ctx, s, C.lit, 2, alpha, lambda: tick_to(ctx, ray, L * q, 2, 22))
        return
    u = s.since
    if u >= 0.6:
        return
    fade = 1 if u < 0.4 else 1 - (u - 0.4) / 0.2
    shut = 1 if s.reduced else detent(u / 0.36, 4)
    a, b = k.tp * shut, L - (L - k.tp) * shut

    def blades():
        band_to(ctx, ray, 0, max(a, 0.5), 2, 20)
        band_to(ctx, ray, min(b, L - 0.5), L, 2, 20)
    fill_with(ctx, "#000", 0.5 * fade, blades)

    def edges():
        tick_to(ctx, ray, a, 2, 20)
        tick_to(ctx, ray, b, 2, 20)
    ink(ctx, s, C.lit, 1.5, fade, edges)

    def teeth():
        for j in range(J + 1):
            d = j * L / J
            if a < d < b:
                tick_to(ctx, ray, d, 4, 14 if j % 8 == 0 else 9)
    ink(ctx, s, C.hot, 1.25, fade * 0.9, teeth)
    ink(ctx, s, C.hot, 2, fade, lambda: rail_to(ctx, ray, 0, L, 2))


def gauge_depart(ctx, s):
    C, G = roles(s), 16
    k = frame(ctx, s, True)
    if k.kind == "notch":
        return gauge_notch(ctx, s, k, C)
    R0 = 58 if k.kind == "corner" else 46
    sweep = PI / 2 if k.kind == "corner" else PI

    def grads(o, lit_to, flash_k, flash_len):
        off, on, hot = [], [], []
        for j in range(G + 1):
            ang = sweep * j / G
            cs, sn = math.cos(ang), math.sin(ang)
            length = 10 if j % 4 == 0 else 5
            (on if j / G <= lit_to + 1e-6 else off).append((cs, sn, R0 - length, R0))
            if j == flash_k:
                hot.append((cs, sn, R0 - length, R0 + flash_len))

        def draw(items):
            def build():
                for cs, sn, a, b in items:
                    ctx.moveTo(cs * a, sn * a)
                    ctx.lineTo(cs * b, sn * b)
            return build
        return SimpleNamespace(off=draw(off), on=draw(on), hot=draw(hot))

    if s.since is None:
        p = s.pressure
        alpha = clamp(p * 3)
        q = detent(p, G, 0.5)
        lit_to = (math.floor(p * G + 1e-6) / G) if s.reduced else q
        flash = tick_flash(s)
        g = grads(0, lit_to, s.tick.index * 4 if flash else -1, 0 if s.reduced else 8 * flash)
        ink(ctx, s, C.dim, 1, alpha * 0.45, lambda: ctx.arc(0, 0, R0 + 3, 0, sweep))
        if lit_to > 0:
            ink(ctx, s, C.lit, 2.2, alpha, lambda: ctx.arc(0, 0, R0 + 3, 0, sweep * lit_to))
        ink(ctx, s, C.dim, 1.25, alpha * 0.5, g.off)
        ink(ctx, s, C.lit, 1.25, alpha, g.on)
        ink(ctx, s, C.hot, 1.5, alpha * flash, g.hot)
        if not s.reduced:
            ang = sweep * q

            def needle():
                ctx.moveTo(math.cos(ang) * 9, math.sin(ang) * 9)
                ctx.lineTo(math.cos(ang) * (R0 - 13), math.sin(ang) * (R0 - 13))
            ink(ctx, s, C.lit, 2, alpha, needle)
        ink(ctx, s, C.lit, 1.25, alpha, lambda: ctx.arc(0, 0, 5, 0, sweep))
        return
    u = s.since
    if u >= 0.6:
        return
    fade = (1 - detent(u / 0.6, 4, 0.25)) if s.reduced else (1 if u < 0.4 else 1 - (u - 0.4) / 0.2)
    shut = 1 if s.reduced else detent(u / 0.36, 4)
    a = R0 * (1 - shut)
    rot = shut * PI / 6

    def blade():
        ctx.moveTo(R0, 0)
        ctx.arc(0, 0, R0, 0, sweep)
        if a > 0.5:
            ctx.lineTo(a * math.cos(sweep), a * math.sin(sweep))
            ctx.arc(0, 0, a, sweep, 0, True)
        else:
            ctx.lineTo(0, 0)
        ctx.closePath()
    fill_with(ctx, "#000", 0.5 * fade, blade)
    length = math.sqrt(max(0, R0 * R0 - a * a))
    if length > 0.5:
        def spokes():
            for i in range(6):
                f = i * PI / 3 + rot
                px, py = a * math.cos(f), a * math.sin(f)
                ctx.moveTo(px, py)
                ctx.lineTo(px - math.sin(f) * length, py + math.cos(f) * length)
        ink(ctx, s, C.lit, 1.25, fade, spokes)
    g = grads(0, 1, -1, 0)
    ink(ctx, s, C.hot, 1.25, fade * 0.9, g.on)
    ink(ctx, s, C.hot, 2, fade, lambda: ctx.arc(0, 0, R0 + 3, 0, sweep))


def gauge_arrive(ctx, s):
    t = s.since
    if t >= 0.7:
        return
    C = roles(s)
    c = 1 if s.reduced else detent(t / 0.34, 4)
    lock_at = 0.3
    locked = t >= lock_at
    flash = clamp(1 - (t - lock_at) / 0.14) if locked else 0
    alpha = 1 if t < 0.46 else 1 - (t - 0.46) / 0.24
    ctx.save()
    k = frame(ctx, s, False)
    if k.kind == "notch":
        tr, L = k.track, k.track.L
        o = lerp(34, 10, c)
        ray = SimpleNamespace(at=tr.at)

        def ticks():
            for j in range(25):
                tick_to(ctx, ray, j * L / 24, o, o + (8 if j % 6 == 0 else 4))
        ink(ctx, s, C.lit, 1.5, alpha, lambda: rail_to(ctx, ray, 0, L, o))
        ink(ctx, s, C.lit if locked else C.dim, 1.25, alpha, ticks)
        ink(ctx, s, C.hot, 1.75, alpha * flash, ticks)
        ink(ctx, s, C.hot, 2, alpha, lambda: tick_to(ctx, ray, k.tp, o, o + 14))
        ctx.restore()
        return
    if k.kind == "corner" and s.method != "switch":
        # A switch came in by no edge, so a pointer that happens to sit near a corner has no
        # corner register to mark, as Rule and Rangefinder skip theirs.
        J = lerp(96, 30, c)

        def bracket():
            ctx.moveTo(J, 1)
            ctx.lineTo(1, 1)
            ctx.lineTo(1, J)
        ink(ctx, s, C.lit, 1.75, alpha, bracket)
    ctx.restore()
    P, r, rot = s.point, lerp(96, 30, c), c * PI / 3

    def ticks():
        for j in range(24):
            a = rot + j * TAU / 24
            length = 8 if j % 6 == 0 else 4
            ctx.moveTo(P.x + math.cos(a) * r, P.y + math.sin(a) * r)
            ctx.lineTo(P.x + math.cos(a) * (r - length), P.y + math.sin(a) * (r - length))
    ink(ctx, s, C.lit, 1.5, alpha, lambda: ctx.arc(P.x, P.y, r, 0, TAU))
    ink(ctx, s, C.lit if locked else C.dim, 1.25, alpha, ticks)
    ink(ctx, s, C.hot, 1.75, alpha * flash, ticks)

    def cross():
        for q in range(4):
            dx, dy = math.cos(q * PI / 2), math.sin(q * PI / 2)
            ctx.moveTo(P.x + dx * (r + 3), P.y + dy * (r + 3))
            ctx.lineTo(P.x + dx * (r + 15), P.y + dy * (r + 15))
    ink(ctx, s, C.lit, 1.5, alpha, cross)


def lamps(ctx, s, k, C, count, flash, alpha):
    c, t = k.centre, k.tangent
    spots = []
    for i in range(3):
        if k.kind == "corner":
            o = 32 + 11 * i
            spots.append((c.x + c.nx * o, c.y + c.ny * o))
        else:
            spots.append((c.x + c.nx * 38 + t.x * 10 * (i - 1), c.y + c.ny * 38 + t.y * 10 * (i - 1)))

    def box(items):
        def build():
            for x, y in items:
                ctx.rect(x - 2.5, y - 2.5, 5, 5)
        return build
    lit, dark = spots[:count], spots[count:]
    ink(ctx, s, C.dim, 1, alpha * 0.6, box(dark))
    if lit:
        def halo():
            for x, y in lit:
                ctx.rect(x - 3.5, y - 3.5, 7, 7)
        fill_with(ctx, "#000", alpha * 0.6, halo)
        fill_with(ctx, C.lit, alpha, box(lit))
        if flash:
            fill_with(ctx, C.hot, alpha * flash, box(lit[-1:]))


def rangefinder_depart(ctx, s):
    C = roles(s)
    k = frame(ctx, s, True)
    SP = 6
    SPAN = 108 if k.kind == "notch" else 150
    if s.since is None:
        p = s.pressure
        alpha = clamp(p * 3)
        flash = tick_flash(s)
        close = 0 if s.reduced else detent(p, 16, 0.45)
        off, on, jaws = [], [], []
        for ray in k.rays:
            lim = min(SPAN, ray.max)
            J = lerp(lim, 8, close)
            lit_from = lim * (1 - p) if s.reduced else J
            jaws.append((ray, J))
            i = 1
            while i * SP <= lim:
                (on if i * SP >= lit_from - 0.01 else off).append((ray, i * SP, 1, 1 + (12 if i % 5 == 0 else 5)))
                i += 1

        def rails():
            for ray in k.rays:
                rail_to(ctx, ray, 0, min(SPAN, ray.max), 1)
        ink(ctx, s, C.dim, 1, alpha * 0.45, rails)
        ink(ctx, s, C.dim, 1.25, alpha * 0.5, comb(ctx, off))
        ink(ctx, s, C.lit, 1.25, alpha, comb(ctx, on))
        ink(ctx, s, C.dim, 1, alpha * 0.7, normal_line(ctx, k.centre, 1, 28))

        def jaw_marks():
            for ray, J in jaws:
                tick_to(ctx, ray, J, 0, 24)
                rail_to(ctx, ray, J, J + 7, 24)
        ink(ctx, s, C.lit, 2, alpha, jaw_marks)

        def jaw_fill():
            for ray, J in jaws:
                a, b, c = ray.at(J), ray.at(J - 3), ray.at(J + 3)
                ctx.moveTo(a.x + a.nx * 1, a.y + a.ny * 1)
                ctx.lineTo(b.x + b.nx * 8, b.y + b.ny * 8)
                ctx.lineTo(c.x + c.nx * 8, c.y + c.ny * 8)
                ctx.closePath()
        fill_with(ctx, C.lit, alpha, jaw_fill)

        def jaw_flash():
            for ray, J in jaws:
                tick_to(ctx, ray, J, 0, 24 if s.reduced else 24 + 16 * flash)
        ink(ctx, s, C.hot, 1.25, alpha * flash, jaw_flash)
        lamps(ctx, s, k, C, s.tick.index if s.tick else 0, flash, alpha)
        return
    u = s.since
    if u >= 0.8:
        return
    fade = 1 if u < 0.55 else 1 - (u - 0.55) / 0.25
    A = 0 if s.reduced else SPAN * (1 - detent(u / 0.42, 4))
    slit = 70 if s.reduced else 70 * (1 - detent(u / 0.55, 5))
    blades, on = [], []
    for ray in k.rays:
        lim = min(SPAN, ray.max)
        if A < lim:
            blades.append((ray, A, lim))
        i = 1
        while i * SP <= min(A, lim):
            on.append((ray, i * SP, 1, 1 + (12 if i % 5 == 0 else 5)))
            i += 1

    def band():
        for ray, a, b in blades:
            band_to(ctx, ray, a, b, 0, 26)
    fill_with(ctx, "#000", 0.5 * fade, band)

    def rails2():
        for ray, a, b in blades:
            rail_to(ctx, ray, a, b, 26)
    ink(ctx, s, C.dim, 1, fade * 0.7, rails2)

    def edges():
        for ray, a, _b in blades:
            tick_to(ctx, ray, a, 0, 26)
    ink(ctx, s, C.lit, 2, fade, edges)
    ink(ctx, s, C.hot, 1.25, fade, comb(ctx, on))
    if slit > 0.5:
        ink(ctx, s, C.hot, 2.5, fade, normal_line(ctx, k.centre, 0, slit))
    lamps(ctx, s, k, C, 3, 0, fade)


def rangefinder_arrive(ctx, s):
    t = s.since
    if t >= 0.8:
        return
    C = roles(s)
    c = 1 if s.reduced else detent(t / 0.4, 4)
    LOCK = 0.34
    locked = t >= LOCK
    flash = clamp(1 - (t - LOCK) / 0.18) if locked else 0
    alpha = 1 if t < 0.55 else 1 - (t - 0.55) / 0.25
    if s.method == "switch":
        # A switch came in by no edge, so there are no jaws to close along it: only the dial.
        rangefinder_dial(ctx, s, C, c, locked, flash, alpha)
        return
    ctx.save()
    k = frame(ctx, s, False)
    JM = 60 if k.kind == "notch" else 104
    on, jaws = [], []
    for ray in k.rays:
        J = min(JM, ray.max) * c
        if J > 1:
            jaws.append((ray, J))
        i = 1
        while i * 6 <= J:
            on.append((ray, i * 6, 1, 1 + (12 if i % 5 == 0 else 5)))
            i += 1
    ink(ctx, s, C.lit, 1.25, alpha, comb(ctx, on))

    def jaw_marks():
        for ray, J in jaws:
            tick_to(ctx, ray, J, 0, 22)
            rail_to(ctx, ray, J, J + 7, 22)
    ink(ctx, s, C.lit, 2, alpha, jaw_marks)
    if k.kind == "notch":
        tr, L = k.track, k.track.L
        o = lerp(28, 9, c)
        ray = SimpleNamespace(at=tr.at)

        def outline():
            rail_to(ctx, ray, 0, L, o)
        ink(ctx, s, C.lit if locked else C.dim, 1.5, alpha, outline)
        ink(ctx, s, C.hot, 2.25, alpha * flash, outline)

        def teeth():
            for j in range(37):
                tick_to(ctx, ray, j * L / 36, o, o + (7 if j % 9 == 0 else 3))
        ink(ctx, s, C.dim, 1, alpha * 0.8, teeth)
        ink(ctx, s, C.hot, 2, alpha, lambda: tick_to(ctx, ray, k.tp, o, o + 16))
        ctx.restore()
        return
    ctx.restore()
    rangefinder_dial(ctx, s, C, c, locked, flash, alpha)


def rangefinder_dial(ctx, s, C, c, locked, flash, alpha):
    P = s.point
    r = lerp(96, 34, c)
    rot = c * 4 * (TAU / 36) * 2
    ink(ctx, s, C.lit, 1.5, alpha, lambda: ctx.arc(P.x, P.y, r, 0, TAU))

    def grads():
        for j in range(36):
            a = rot + j * TAU / 36
            length = 9 if j % 9 == 0 else 4
            ctx.moveTo(P.x + math.cos(a) * r, P.y + math.sin(a) * r)
            ctx.lineTo(P.x + math.cos(a) * (r + length), P.y + math.sin(a) * (r + length))
    ink(ctx, s, C.dim, 1.1, alpha * 0.85, grads)
    ink(ctx, s, C.hot, 1.5, alpha * flash, grads)

    def outer_ticks():
        for q in range(4):
            dx, dy = math.cos(q * PI / 2), math.sin(q * PI / 2)
            ctx.moveTo(P.x + dx * (r + 12), P.y + dy * (r + 12))
            ctx.lineTo(P.x + dx * (r + 22), P.y + dy * (r + 22))
    ink(ctx, s, C.lit, 1.25, alpha, outer_ticks)

    def spokes():
        for q in range(4):
            dx, dy = math.cos(q * PI / 2), math.sin(q * PI / 2)
            a = max(20, 0.6 * r)
            ctx.moveTo(P.x + dx * a, P.y + dy * a)
            ctx.lineTo(P.x + dx * (r - 6), P.y + dy * (r - 6))
    ink(ctx, s, C.dim, 1, alpha * 0.8, spokes)

    b = r * 0.66

    def brackets():
        for sx, sy in ((1, 1), (-1, 1), (-1, -1), (1, -1)):
            ctx.moveTo(P.x + sx * (b - 9), P.y + sy * b)
            ctx.lineTo(P.x + sx * b, P.y + sy * b)
            ctx.lineTo(P.x + sx * b, P.y + sy * (b - 9))
    ink(ctx, s, C.lit if locked else C.dim, 1.75, alpha, brackets)
    ink(ctx, s, C.hot, 2.25, alpha * flash, brackets)
    if locked:
        def corners():
            for q in range(4):
                dx, dy = math.cos(q * PI / 2), math.sin(q * PI / 2)
                ctx.rect(P.x + dx * (r + 6) - 2, P.y + dy * (r + 6) - 2, 4, 4)
        fill_with(ctx, C.hot, alpha, corners)


EFFECTS = [
    Effect(
        "rule", "Rule", "quiet",
        "A fine ruler rises along the edge round the pointer and lights outward one division at a "
        "time as the push builds, the three quarter detents marked long. At breakthrough it closes "
        "on the pointer in four clicks; on the other screen a short register mark sits where you "
        "came in and a four-arm cross steps in three clicks onto the landing point.",
        rule_depart, rule_arrive, 0.45, 0.6,
    ),
    Effect(
        "gauge", "Gauge", "medium",
        "A half dial seated on the edge at the pointer, a quarter dial in the corner and a "
        "graduated collar round the notch, whose needle clicks round sixteen detents as the push "
        "builds. At breakthrough an iris shuts over the dial in four steps; on the other screen a "
        "graduated ring contracts onto the landing point, turning one graduation per click, and "
        "flashes as it locks.",
        gauge_depart, gauge_arrive, 0.6, 0.7,
    ),
    Effect(
        "rangefinder", "Rangefinder", "showpiece",
        "A long scale runs along the edge and a pair of caliper jaws closes on the pointer in "
        "detents, lighting the divisions it passes, while three tally lamps light at each quarter. "
        "At breakthrough shutter blades slide in from both ends and a bright slit at the pointer "
        "shuts; on the far screen the jaws open outward from the entry point as a graduated "
        "reticle with corner brackets steps in, turns and locks onto the landing point.",
        rangefinder_depart, rangefinder_arrive, 0.8, 0.8,
    ),
]

PACKS = {
    "phosphor": ("Phosphor", ["#3dff7f", "#1f9e52", "#caffdc"]),
    "sodium": ("Sodium", ["#ffa51f", "#c46a12", "#ffe0a3"]),
    "contrast": ("Contrast", ["#f0e442", "#56b4e9", "#ffffff"]),
}
