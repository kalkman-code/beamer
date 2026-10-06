"""Sparks: crossing is a charge that discharges, sparks gathering at the pointer, sucked through
the point at breakthrough, and thrown back out where it lands."""

from __future__ import annotations

import math
from types import SimpleNamespace

from . import effects
from .effects import TAU, clamp, lerp

INWARD = {"right": (-1, 0), "left": (1, 0), "top": (0, 1), "bottom": (0, -1)}


def ease_out(u):
    return 1 - (1 - clamp(u)) ** 3


def ease_in(u):
    return clamp(u) * clamp(u)


def rot(n, a):
    c, k = math.cos(a), math.sin(a)
    return (n[0] * c - n[1] * k, n[0] * k + n[1] * c)


def turn(a, b):
    return math.atan2(math.sin(a - b), math.cos(a - b))


def rgba(hex_colour, a):
    v = int(hex_colour[1:], 16)
    return f"rgba({(v >> 16) & 255},{(v >> 8) & 255},{v & 255},{clamp(a):.3f})"


def heat(s, pal, h):
    k = clamp(h) if s.dark else 0.46 * clamp(h)
    return pal[min(len(pal) - 1, math.floor((1 - k) * len(pal)))]


def shade(s, a):
    return f"rgba(0,0,0,{clamp(a * (0.3 if s.dark else 0.62)):.3f})"


def glow_mode(ctx, s):
    ctx.globalCompositeOperation = "lighter" if s.dark else "source-over"


def depart_frame(s):
    r, p, W, H = s.region, s.point, s.screen.w, s.screen.h
    if r.kind == "notch" and getattr(s, "notch", None):
        n = s.notch
        x = clamp(p.x, n.x + n.r + 6, n.x + n.w - n.r - 6)
        return SimpleNamespace(kind="notch", o=SimpleNamespace(x=x, y=n.y + n.h + 1), n=(0, 1),
                                spread=1.25, sink=SimpleNamespace(x=x, y=n.y + n.h - 12))
    if r.kind == "corner":
        c = SimpleNamespace(x=W if "right" in r.corner else 0, y=0 if "top" in r.corner else H)
        e = (-1 if c.x else 1, -1 if c.y else 1)
        return SimpleNamespace(kind="corner", c=c, e=e,
                                o=SimpleNamespace(x=c.x + e[0] * 2, y=c.y + e[1] * 2),
                                n=(e[0] * math.sqrt(0.5), e[1] * math.sqrt(0.5)), spread=0.66,
                                sink=SimpleNamespace(x=c.x - e[0] * 8, y=c.y - e[1] * 8))
    n = INWARD[r.edge]
    return SimpleNamespace(kind="edge", o=SimpleNamespace(x=p.x + n[0], y=p.y + n[1]), n=n,
                            spread=1.34, sink=SimpleNamespace(x=p.x - n[0] * 8, y=p.y - n[1] * 8))


def arrive_frame(s):
    p, W, H = s.point, s.screen.w, s.screen.h
    if s.method == "switch":
        # A switch, away from any edge: the sparks go off all round the pointer, thrown up and falling.
        return SimpleNamespace(kind="point", o=SimpleNamespace(x=p.x, y=p.y), n=(0, -1), spread=math.pi)
    nt = getattr(s, "notch", None)
    if nt and s.edge == "top" and nt.x - 4 <= p.x <= nt.x + nt.w + 4:
        x = clamp(p.x, nt.x + nt.r + 4, nt.x + nt.w - nt.r - 4)
        return SimpleNamespace(kind="notch", o=SimpleNamespace(x=x, y=nt.y + nt.h + 1),
                                src=SimpleNamespace(x=x, y=nt.y + nt.h - 10), n=(0, 1), spread=1.3)
    n0, near = INWARD[s.edge], 30
    ex, ey, corner = n0[0], n0[1], False
    if ex != 0:
        if p.y < near:
            ey, corner = 1, True
        elif p.y > H - near:
            ey, corner = -1, True
    else:
        if p.x < near:
            ex, corner = 1, True
        elif p.x > W - near:
            ex, corner = -1, True
    if corner:
        c = SimpleNamespace(x=0 if ex > 0 else W, y=0 if ey > 0 else H)
        return SimpleNamespace(kind="corner", c=c, e=(ex, ey),
                                o=SimpleNamespace(x=c.x + ex * 3, y=c.y + ey * 3),
                                n=(ex * math.sqrt(0.5), ey * math.sqrt(0.5)), spread=0.66)
    return SimpleNamespace(kind="edge", o=SimpleNamespace(x=p.x, y=p.y), n=(ex, ey), spread=1.34)


def boundary(ctx, s, f, length, a, colour, width):
    if a <= 0.01 or length <= 0.5 or f.kind == "point":
        return
    W, H = s.screen.w, s.screen.h
    ctx.beginPath()
    if f.kind == "notch":
        n, d = s.notch, 1.6
        ctx.moveTo(n.x - d, n.y)
        ctx.lineTo(n.x - d, n.y + n.h - n.r)
        ctx.quadraticCurveTo(n.x - d, n.y + n.h + d, n.x + n.r, n.y + n.h + d)
        ctx.lineTo(n.x + n.w - n.r, n.y + n.h + d)
        ctx.quadraticCurveTo(n.x + n.w + d, n.y + n.h + d, n.x + n.w + d, n.y + n.h - n.r)
        ctx.lineTo(n.x + n.w + d, n.y)

        def grad(colour_fn):
            g = ctx.createLinearGradient(f.o.x - length, 0, f.o.x + length, 0)
            g.addColorStop(0, colour_fn(0))
            g.addColorStop(0.5, colour_fn(1))
            g.addColorStop(1, colour_fn(0))
            return g
    elif f.kind == "corner":
        c, e = f.c, f.e
        ix, iy = c.x + e[0] * 1, c.y + e[1] * 1
        ctx.moveTo(c.x + e[0] * length, iy)
        ctx.lineTo(ix, iy)
        ctx.lineTo(ix, c.y + e[1] * length)

        def grad(colour_fn):
            g = ctx.createRadialGradient(c.x, c.y, 0, c.x, c.y, length)
            g.addColorStop(0, colour_fn(1))
            g.addColorStop(1, colour_fn(0))
            return g
    else:
        n = f.n
        t = (-n[1], n[0])
        if n[0] != 0:
            b = SimpleNamespace(x=1 if n[0] > 0 else W - 1, y=f.o.y)
        else:
            b = SimpleNamespace(x=f.o.x, y=1 if n[1] > 0 else H - 1)
        x0, y0 = b.x - t[0] * length, b.y - t[1] * length
        x1, y1 = b.x + t[0] * length, b.y + t[1] * length
        ctx.moveTo(x0, y0)
        ctx.lineTo(x1, y1)

        def grad(colour_fn):
            g = ctx.createLinearGradient(x0, y0, x1, y1)
            g.addColorStop(0, colour_fn(0))
            g.addColorStop(0.5, colour_fn(1))
            g.addColorStop(1, colour_fn(0))
            return g
    ctx.lineCap = "round"
    ctx.lineJoin = "round"
    ctx.globalCompositeOperation = "source-over"
    ctx.lineWidth = width + 2
    ctx.strokeStyle = grad(lambda x: shade(s, x * a))
    ctx.stroke()
    glow_mode(ctx, s)
    ctx.lineWidth = width
    ctx.strokeStyle = grad(lambda x: rgba(colour, x * a))
    ctx.stroke()


def streak(ctx, s, x0, y0, x1, y1, w, colour, a):
    if a <= 0.01:
        return
    ctx.beginPath()
    ctx.moveTo(x0, y0)
    ctx.lineTo(x1, y1)
    ctx.lineCap = "round"
    ctx.globalCompositeOperation = "source-over"
    ctx.strokeStyle = shade(s, a)
    ctx.lineWidth = w + (1.8 if s.dark else 1.4)
    ctx.stroke()
    glow_mode(ctx, s)
    ctx.strokeStyle = rgba(colour, a)
    ctx.lineWidth = w
    ctx.stroke()


def glow(ctx, s, x, y, r, colour, a):
    if a <= 0.01 or r <= 0.3:
        return
    if not s.dark:
        d = ctx.createRadialGradient(x, y, 0, x, y, r * 0.75)
        d.addColorStop(0, f"rgba(0,0,0,{clamp(0.4 * a):.3f})")
        d.addColorStop(1, "rgba(0,0,0,0)")
        ctx.globalCompositeOperation = "source-over"
        ctx.fillStyle = d
        ctx.beginPath()
        ctx.arc(x, y, r * 0.75, 0, TAU)
        ctx.fill()
    g = ctx.createRadialGradient(x, y, 0, x, y, r)
    g.addColorStop(0, rgba(colour, a))
    g.addColorStop(0.28, rgba(colour, a * 0.5))
    g.addColorStop(1, rgba(colour, 0))
    glow_mode(ctx, s)
    ctx.fillStyle = g
    ctx.beginPath()
    ctx.arc(x, y, r, 0, TAU)
    ctx.fill()


def dot(ctx, s, x, y, r, colour, a):
    if a <= 0.01 or r <= 0.2:
        return
    ctx.globalCompositeOperation = "source-over"
    ctx.fillStyle = shade(s, a)
    ctx.beginPath()
    ctx.arc(x, y, r + 1, 0, TAU)
    ctx.fill()
    glow_mode(ctx, s)
    ctx.fillStyle = rgba(colour, a)
    ctx.beginPath()
    ctx.arc(x, y, r, 0, TAU)
    ctx.fill()


def crackle(ctx, s, ax, ay, bx, by, seed, colour, a):
    if a <= 0.01:
        return
    dx, dy = bx - ax, by - ay
    L = math.hypot(dx, dy) or 1
    nx, ny = -dy / L, dx / L
    ctx.beginPath()
    ctx.moveTo(ax, ay)
    for k in range(1, 7):
        u = k / 7
        j = (s.hash(seed * 13 + k) * 2 - 1) * min(14, L * 0.2) * math.sin(math.pi * u)
        ctx.lineTo(ax + dx * u + nx * j, ay + dy * u + ny * j)
    ctx.lineTo(bx, by)
    ctx.lineJoin = "miter"
    ctx.lineCap = "round"
    ctx.globalCompositeOperation = "source-over"
    ctx.strokeStyle = shade(s, a)
    ctx.lineWidth = 2.8
    ctx.stroke()
    glow_mode(ctx, s)
    ctx.strokeStyle = rgba(colour, a)
    ctx.lineWidth = 1.1
    ctx.stroke()


def filing(s, f, i, R, p, align):
    h1, h2, h3, h4 = s.hash(i * 7 + 1), s.hash(i * 7 + 2), s.hash(i * 7 + 3), s.hash(i * 7 + 4)
    direction = rot(f.n, (h1 * 2 - 1) * f.spread)
    r0 = R * (0.3 + 0.7 * math.sqrt(h2))
    r = r0 * (1 - 0.6 * ease_in(p))
    radial, loose = math.atan2(-direction[1], -direction[0]), h4 * TAU
    k = clamp(p * 1.8) if align else 1
    orient = loose + turn(radial, loose) * k + math.sin(p * 29 + i * 1.7) * 0.35 * (1 - k)
    return SimpleNamespace(x=f.o.x + direction[0] * r, y=f.o.y + direction[1] * r, orient=orient,
                            appear=0.02 + h3 * 0.7, near=1 - r / R)


def crawler_at(s, f, side, dist):
    if f.kind == "notch":
        n = s.notch
        x = f.o.x + side * dist
        cx = clamp(x, n.x - 2.5, n.x + n.w + 2.5)
        over = abs(x - cx)
        return SimpleNamespace(x=cx, y=max(1, n.y + n.h + 2.5 - over))
    if f.kind == "corner":
        if side > 0:
            return SimpleNamespace(x=f.c.x + f.e[0] * (dist + 3), y=f.c.y + f.e[1] * 2.5)
        return SimpleNamespace(x=f.c.x + f.e[0] * 2.5, y=f.c.y + f.e[1] * (dist + 3))
    t = (-f.n[1], f.n[0])
    return SimpleNamespace(x=f.o.x + t[0] * side * dist + f.n[0] * 2.5,
                            y=f.o.y + t[1] * side * dist + f.n[1] * 2.5)


def make_depart(cfg):
    def depart(ctx, s):
        t, pal = s.since, s.palette
        if t is not None and t >= cfg.departSeconds:
            return
        if t is None and s.pressure <= 0:
            return
        f = depart_frame(s)
        if s.reduced:
            depart_still(ctx, s, f, cfg)
            return
        p = s.pressure if t is None else 1
        tick = 1 - s.tick.age / 0.16 if (t is None and s.tick and s.tick.age < 0.16) else 0

        def length(q):
            return cfg.len0 + cfg.len1 * q

        if t is None:
            if cfg.flash:
                boundary(ctx, s, f, cfg.R * (0.45 + 0.75 * p), (0.2 + 0.55 * p + 0.3 * tick) * clamp(p * 4),
                          heat(s, pal, 0.55 + 0.45 * p), 1.4 + 0.6 * p)
            for i in range(cfg.N):
                q = filing(s, f, i, cfg.R, p, cfg.align)
                vis = clamp((p - q.appear) / 0.1)
                if vis <= 0:
                    continue
                pull = tick * 0.16
                x, y = lerp(q.x, f.o.x, pull), lerp(q.y, f.o.y, pull)
                l = length(p) * (1 + tick * 0.6)
                cx, cy = math.cos(q.orient) * l / 2, math.sin(q.orient) * l / 2
                streak(ctx, s, x - cx, y - cy, x + cx, y + cy, 1 + 0.6 * p,
                       heat(s, pal, 0.2 + 0.45 * p + 0.35 * q.near), vis * (0.35 + 0.65 * p))
            for j in range(cfg.crawl):
                side = 1 if j % 2 else -1
                u = (s.hash(j * 5 + 200) + p * 1.7) % 1
                a = clamp((p - 0.18) / 0.2) * (0.4 + 0.6 * p) * clamp(u * 5) * clamp((1 - u) * 6)
                L = cfg.R * 1.3
                d = L * (1 - u)
                head, tail = crawler_at(s, f, side, d), crawler_at(s, f, side, d + 7)
                streak(ctx, s, tail.x, tail.y, head.x, head.y, 1.3, heat(s, pal, 0.8), a)
            if tick and cfg.arcs:
                for k in range(2):
                    q = filing(s, f, (s.tick.index * 11 + k * 17) % cfg.N, cfg.R, p, True)
                    crackle(ctx, s, q.x, q.y, f.o.x, f.o.y, s.tick.index * 3 + k, heat(s, pal, 1), tick)
            glow(ctx, s, f.o.x, f.o.y, cfg.core * (0.35 + 0.65 * p) * (1 + 0.4 * tick),
                 heat(s, pal, 0.6 + 0.4 * p), 0.25 + 0.6 * p)
            dot(ctx, s, f.o.x, f.o.y, 1.2 + 1.6 * p + tick, heat(s, pal, 1), 0.5 + 0.5 * p)
            return

        u = clamp(t / cfg.implode)
        if u < 1:
            for i in range(cfg.N):
                q = filing(s, f, i, cfg.R, 1, cfg.align)
                e1, e0 = ease_in(u), ease_in(u - 0.32)
                dx, dy = f.sink.x - q.x, f.sink.y - q.y
                dl = math.hypot(dx, dy) or 1
                half = length(1) * 0.5 * (1 - u)
                ax, ay = dx / dl * half, dy / dl * half
                streak(ctx, s, lerp(q.x, f.sink.x, e0) - ax, lerp(q.y, f.sink.y, e0) - ay,
                       lerp(q.x, f.sink.x, e1) + ax, lerp(q.y, f.sink.y, e1) + ay,
                       1.3 + 0.9 * u, heat(s, pal, 0.7 + 0.3 * u), 1)
            for j in range(cfg.crawl):
                side = 1 if j % 2 else -1
                d0 = cfg.R * 1.3 * (1 - (s.hash(j * 5 + 200) + 1.7) % 1)
                head = crawler_at(s, f, side, d0 * (1 - ease_in(u)))
                tail = crawler_at(s, f, side, d0 * (1 - ease_in(u - 0.35)) + 4)
                streak(ctx, s, tail.x, tail.y, head.x, head.y, 1.5, heat(s, pal, 0.9), 1 - u * 0.5)
        if cfg.arcs and t < 0.14:
            if f.kind == "notch":
                ends = [crawler_at(s, f, -1, 200), crawler_at(s, f, 1, 200)]
            else:
                ends = [crawler_at(s, f, -1, cfg.flash * 0.8), crawler_at(s, f, 1, cfg.flash * 0.8)]
            for k, e in enumerate(ends):
                crackle(ctx, s, e.x, e.y, f.o.x, f.o.y, 40 + k, heat(s, pal, 1), 1 - t / 0.14)
        fade = 1 - t / cfg.departSeconds
        if cfg.flash:
            boundary(ctx, s, f, cfg.flash * (1 - ease_out(t / (cfg.departSeconds * 0.85))), fade,
                      heat(s, pal, 1), 2)
        glow(ctx, s, f.o.x, f.o.y, cfg.core * 1.3 * (1 - ease_in(t / (cfg.implode + 0.1))), heat(s, pal, 1), 1)
        dot(ctx, s, f.o.x, f.o.y, 3 * fade, heat(s, pal, 1), fade)
    return depart


def spark_pos(src, direction, v, drag, g, tt):
    d = v / drag * (1 - math.exp(-drag * tt))
    return SimpleNamespace(x=src.x + direction[0] * d, y=src.y + direction[1] * d + 0.5 * g * tt * tt)


def make_arrive(cfg):
    def arrive(ctx, s):
        t, pal = s.since, s.palette
        if t >= cfg.arriveSeconds:
            return
        f = arrive_frame(s)
        if s.reduced:
            arrive_still(ctx, s, f, cfg)
            return
        src = getattr(f, "src", None) or f.o
        if cfg.flash:
            boundary(ctx, s, f, 4 + cfg.flash * ease_out(t / 0.2),
                      0.95 * (1 - clamp(t / (cfg.arriveSeconds * 0.7))), heat(s, pal, 1), 1.8)
        pv = clamp(t / 0.16)
        glow(ctx, s, f.o.x, f.o.y, cfg.pop * (0.35 + 0.65 * ease_out(pv)), heat(s, pal, 1), 1 - pv)
        for i in range(cfg.M):
            h1, h2, h3, h4 = s.hash(i * 5 + 101), s.hash(i * 5 + 102), s.hash(i * 5 + 103), s.hash(i * 5 + 104)
            direction = rot(f.n, (h1 * 2 - 1) * f.spread * (0.86 if f.kind == "edge" else 1))
            v = cfg.V * (0.5 + 0.5 * h2)
            life = cfg.life * (0.5 + 0.5 * h3)
            tt = t - h4 * 0.04
            if tt <= 0:
                continue
            if tt < life:
                k = tt / life
                head = spark_pos(src, direction, v, cfg.drag, cfg.G, tt)
                tail = spark_pos(src, direction, v, cfg.drag, cfg.G, max(0, tt - 0.045))
                flick = 1 if i % 3 else 0.7 + 0.3 * math.cos(tt * 70 + i)
                streak(ctx, s, tail.x, tail.y, head.x, head.y, 0.9 + 1.1 * (1 - k),
                       heat(s, pal, 1 - k * 0.6), (1 - k) ** 0.45 * flick)
            if i < cfg.branch:
                ts = life * 0.42
                bt = tt - ts
                bl = life * 0.55
                if 0 < bt < bl:
                    o = spark_pos(src, direction, v, cfg.drag, cfg.G, ts)
                    d = v / cfg.drag * math.exp(-cfg.drag * ts) * cfg.drag
                    for sgn in (-1, 1):
                        bd = rot(direction, sgn * (0.5 + 0.4 * h4))
                        k = bt / bl
                        head = spark_pos(o, bd, d * 0.45, cfg.drag, cfg.G, bt)
                        tail = spark_pos(o, bd, d * 0.45, cfg.drag, cfg.G, max(0, bt - 0.03))
                        streak(ctx, s, tail.x, tail.y, head.x, head.y, 0.8 + 0.6 * (1 - k),
                               heat(s, pal, 0.9 - k * 0.8), 1 - k)
        dot(ctx, s, f.o.x, f.o.y, 2.6 * (1 - clamp(t / 0.3)), heat(s, pal, 1), 1 - clamp(t / 0.3))
    return arrive


def still_fan(ctx, s, f, count, r0, r1, pal, alpha_of):
    for i in range(count):
        a = alpha_of(i)
        if a <= 0.01:
            continue
        direction = rot(f.n, lerp(-f.spread * 0.92, f.spread * 0.92, 0.5 if count == 1 else i / (count - 1)))
        k = 0.72 if i % 2 else 1
        streak(ctx, s, f.o.x + direction[0] * r0, f.o.y + direction[1] * r0,
               f.o.x + direction[0] * (r0 + (r1 - r0) * k), f.o.y + direction[1] * (r0 + (r1 - r0) * k),
               1.6, heat(s, pal, 0.55 if i % 2 else 0.95), a)


def depart_still(ctx, s, f, cfg):
    t, pal = s.since, s.palette
    p = s.pressure if t is None else 1
    A = 1 if t is None else 1 - clamp(t / cfg.departSeconds)
    tick = 1 - s.tick.age / 0.2 if (t is None and s.tick and s.tick.age < 0.2) else 0

    def lit(i):
        return A * clamp((p - (i % 4) * 0.25) / 0.06) * (0.45 + 0.55 * p)

    if cfg.flash:
        boundary(ctx, s, f, cfg.R * 0.9, A * (0.2 + 0.6 * p), heat(s, pal, 1), 1.6)
    still_fan(ctx, s, f, cfg.still, cfg.stillR[0], cfg.stillR[1], pal, lit)
    glow(ctx, s, f.o.x, f.o.y, cfg.core * 0.7, heat(s, pal, 0.6 + 0.4 * p), A * clamp(0.25 + 0.55 * p + 0.3 * tick))
    dot(ctx, s, f.o.x, f.o.y, 2.2, heat(s, pal, 1), A * (0.5 + 0.5 * p))


def arrive_still(ctx, s, f, cfg):
    t, pal = s.since, s.palette
    A = 0.35 + 0.65 * t / 0.08 if t < 0.08 else 1 - clamp((t - 0.08) / (cfg.arriveSeconds - 0.08))
    if cfg.flash:
        boundary(ctx, s, f, cfg.flash * 0.6, A * 0.8, heat(s, pal, 1), 1.8)
    still_fan(ctx, s, f, cfg.still, cfg.stillR[0] + 4, cfg.stillR[1] + 8, pal, lambda i: A)
    glow(ctx, s, f.o.x, f.o.y, cfg.pop * 0.8, heat(s, pal, 1), A * 0.9)
    dot(ctx, s, f.o.x, f.o.y, 2.4, heat(s, pal, 1), A)


def effect(id_, name, intensity, blurb, depart_cfg, arrive_cfg):
    cfg = SimpleNamespace(**depart_cfg, **arrive_cfg)
    return effects.Effect(id_, name, intensity, blurb, make_depart(cfg), make_arrive(cfg),
                           depart_cfg["departSeconds"], arrive_cfg["arriveSeconds"])


EFFECTS = [
    effect(
        "flint", "Flint", "quiet",
        "A single hot point at the pointer draws in a few close sparks as you push, snaps them "
        "through the edge, and a pinch of sparks pops out where you land. Small enough to leave on "
        "all day.",
        dict(N=8, R=40, align=False, len0=2, len1=5, crawl=0, arcs=False, flash=0, core=13,
             implode=0.14, departSeconds=0.4, still=5, stillR=(8, 16)),
        dict(M=12, V=420, drag=7, G=260, life=0.45, branch=0, pop=11, arriveSeconds=0.45),
    ),
    effect(
        "filings", "Filings", "medium",
        "Scattered sparks swing round to point at the pointer and creep in like iron filings to a "
        "magnet, the edge warming beside it; at breakthrough they are sucked through the point, and "
        "on the other screen the same sparks are thrown back out and fall.",
        dict(N=24, R=72, align=True, len0=2.5, len1=8, crawl=0, arcs=False, flash=70, core=18,
             implode=0.2, departSeconds=0.55, still=9, stillR=(12, 26)),
        dict(M=26, V=500, drag=6.5, G=320, life=0.62, branch=0, pop=20, arriveSeconds=0.65),
    ),
    effect(
        "discharge", "Discharge", "showpiece",
        "The boundary charges up: sparks crawl along it and forty filings wheel in, with a crackle "
        "arcing to the pointer at every tick. At breakthrough the whole charge implodes through the "
        "point with a last arc; where it lands it goes off like a sparkler, stars splitting as they "
        "fall.",
        dict(N=40, R=104, align=True, len0=3, len1=11, crawl=8, arcs=True, flash=120, core=26,
             implode=0.24, departSeconds=0.75, still=13, stillR=(14, 34)),
        dict(M=30, V=600, drag=6, G=260, life=0.72, branch=9, pop=30, arriveSeconds=0.78),
    ),
]

PACKS = {
    "arc": ("Arc", ["#69c8ff", "#269de8", "#b5e5ff", "#456ad0"]),
    "magnesium": ("Magnesium", ["#c98bff", "#9255da", "#ead2ff", "#b064e8"]),
    "ember": ("Ember", ["#ffe4ad", "#ffb04a", "#ff6a24", "#d8321a", "#8e1a10"]),
    "forge": ("Forge", ["#fff4dc", "#ffc15e", "#ff7f2a", "#b8c4d2", "#5d7896"]),
    "sparkler_gold": ("Sparkler gold", ["#ffffff", "#fff1bf", "#ffd35c", "#f0a531"]),
}
