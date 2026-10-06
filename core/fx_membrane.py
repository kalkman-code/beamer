"""Membrane: crossing is pushing through a skin, the boundary stretching thin under the pointer
until it tears, and the same skin on the other screen rippling out from where you came through and
healing behind you.
"""

import math
from types import SimpleNamespace

from . import effects

TAU = effects.TAU
clamp = effects.clamp
lerp = effects.lerp
ease_out = effects.ease_out


# The radius of a switch's ring round the pointer, before the effect's own gap from the edge.
POINT_RING = 14


def smooth(a, b, x):
    u = clamp((x - a) / (b - a))
    return u * u * (3 - 2 * u)


def bump(x):
    return math.exp(-x * x)


def rgb(h):
    return [int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)]


def hue(pal, u):
    n = len(pal)
    f = math.fmod(math.fmod(u, n) + n, n)
    i = int(math.floor(f)) % n
    k = f - math.floor(f)
    a, b = pal[i], pal[(i + 1) % n]
    return [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k]


def rgba(c, a):
    return f"rgba({math.trunc(c[0])},{math.trunc(c[1])},{math.trunc(c[2])},{clamp(a):.3f})"


def ink(a):
    return f"rgba(0,0,0,{clamp(a):.3f})"


def line(a, b):
    length = math.hypot(b[0] - a[0], b[1] - a[1])

    def at(u):
        return (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u)

    return SimpleNamespace(len=length, at=at)


def arc(cx, cy, r, a0, a1, mapper):
    length = abs(a1 - a0) * r

    def at(u):
        a = a0 + (a1 - a0) * u
        return mapper(cx + r * math.cos(a), cy + r * math.sin(a))

    return SimpleNamespace(len=length, at=at)


def same(x, y):
    return (x, y)


def sample(segs, anchor, ref):
    pts = []
    for j, sg in enumerate(segs):
        n = max(1, math.ceil(sg.len / 4))
        for i in range(1 if j else 0, n + 1):
            x, y = sg.at(i / n)
            pts.append(SimpleNamespace(x=x, y=y))
    t = 0.0
    i0 = 0
    best = math.inf
    for i, p in enumerate(pts):
        if i:
            t += math.hypot(p.x - pts[i - 1].x, p.y - pts[i - 1].y)
        p.t = t
        a = pts[max(0, i - 1)]
        b = pts[min(len(pts) - 1, i + 1)]
        tx, ty = b.x - a.x, b.y - a.y
        m = math.hypot(tx, ty) or 1.0
        p.nx, p.ny = -ty / m, tx / m
        dd = (p.x - anchor[0]) ** 2 + (p.y - anchor[1]) ** 2
        if dd < best:
            best, i0 = dd, i
    o = pts[i0]
    flip = -1.0 if o.nx * (o.x - ref[0]) + o.ny * (o.y - ref[1]) < 0 else 1.0
    for p in pts:
        p.nx *= flip
        p.ny *= flip
        p.d = p.t - o.t
        p.end = min(p.t, t - p.t)
    return SimpleNamespace(pts=pts, total=t)


def edge_geo(s, edge):
    W, H = s.screen.w, s.screen.h
    side = edge in ("left", "right")

    def mapper(u, v):
        if edge == "left":
            return (v, u)
        if edge == "right":
            return (W - v, u)
        if edge == "top":
            return (u, v)
        return (u, H - v)

    u0 = s.point.y if side else s.point.x
    return SimpleNamespace(kind="edge", map=mapper, u0=u0, centre=mapper(u0, 0))


def geo_of(s, leaving):
    W, H = s.screen.w, s.screen.h
    p = s.point
    n = getattr(s, "notch", None)
    if leaving:
        r = s.region
        if r.kind == "notch":
            return SimpleNamespace(kind="notch", anchor=(p.x, p.y), centre=(p.x, 0))
        if r.kind == "corner":
            cx = W if "right" in r.corner else 0
            cy = H if "bottom" in r.corner else 0
            return SimpleNamespace(kind="corner", cx=cx, cy=cy, centre=(cx, cy))
        return edge_geo(s, r.edge)
    if s.method == "switch":
        # A switch, away from any edge: the skin closes as a small ring round the pointer.
        return SimpleNamespace(kind="point", centre=(p.x, p.y))
    if n is not None and s.edge == "top" and n.x - 24 < p.x < n.x + n.w + 24:
        return SimpleNamespace(kind="notch", anchor=(p.x, p.y), centre=(p.x, p.y))
    cx = 0 if p.x < 20 else (W if p.x > W - 20 else -1)
    cy = 0 if p.y < 20 else (H if p.y > H - 20 else -1)
    if cx >= 0 and cy >= 0:
        return SimpleNamespace(kind="corner", cx=cx, cy=cy, centre=(cx, cy))
    return edge_geo(s, s.edge)


def contour(s, geo, g, L, o):
    if geo.kind == "edge":
        mapper, u0 = geo.map, geo.u0
        return sample([line(mapper(u0 - L, g), mapper(u0 + L, g))], mapper(u0, g), geo.centre)
    if geo.kind == "point":
        cx, cy = geo.centre
        R = POINT_RING + g
        # Starting opposite the top, so the ring's gap and bulge sit above the pointer's tip.
        return sample([arc(cx, cy, R, math.pi / 2, math.pi / 2 + TAU, same)], (cx, cy - R), geo.centre)
    if geo.kind == "corner":
        sx = -1 if geo.cx else 1
        sy = -1 if geo.cy else 1

        def M(a, b):
            return (geo.cx + sx * a, geo.cy + sy * b)

        rho = max(3, o["rho"])
        c = g + rho
        arm = max(c + 12, L)
        k = c - rho / math.sqrt(2)
        return sample([line(M(arm, g), M(c, g)), arc(c, c, rho, -math.pi / 2, -math.pi, M),
                       line(M(g, c), M(g, arm))], M(k, k), geo.centre)
    n = s.notch
    lo = n.h - n.r
    x0, x1 = n.x - g, n.x + n.w + g
    R = n.r + g
    arm = o.get("arm") or 0
    f = max(2, min(8, lo - g - 2))
    segs = []
    if arm:
        segs.append(line((x0 - f - arm, g), (x0 - f, g)))
        segs.append(arc(x0 - f, g + f, f, -math.pi / 2, 0, same))
        segs.append(line((x0, g + f), (x0, lo)))
    else:
        segs.append(line((x0, 0), (x0, lo)))
    segs.append(arc(n.x + n.r, lo, R, math.pi, math.pi / 2, same))
    segs.append(line((n.x + n.r, n.h + g), (n.x + n.w - n.r, n.h + g)))
    segs.append(arc(n.x + n.w - n.r, lo, R, math.pi / 2, 0, same))
    if arm:
        segs.append(line((x1, lo), (x1, g + f)))
        segs.append(arc(x1 + f, g + f, f, math.pi, math.pi * 1.5, same))
        segs.append(line((x1 + f, g), (x1 + f + arm, g)))
    else:
        segs.append(line((x1, lo), (x1, 0)))
    return sample(segs, geo.anchor, (n.x + n.w / 2, 0))


def trace(ctx, C, off, keep):
    ctx.beginPath()
    pen = False
    for p in C.pts:
        if keep and not keep(p):
            pen = False
            continue
        o = off(p)
        x, y = p.x + p.nx * o, p.y + p.ny * o
        if pen:
            ctx.lineTo(x, y)
        else:
            ctx.moveTo(x, y)
            pen = True


def along(ctx, C, stop):
    P = C.pts
    A, Z = P[0], P[-1]
    dx, dy = Z.x - A.x, Z.y - A.y
    l2 = (dx * dx + dy * dy) or 1.0
    g = ctx.createLinearGradient(A.x, A.y, Z.x, Z.y)
    every = max(1, math.ceil(len(P) / 30))
    idx = list(range(0, len(P), every))
    if idx[-1] != len(P) - 1:
        idx.append(len(P) - 1)
    last = 0.0
    for i in idx:
        p = P[i]
        q = clamp(((p.x - A.x) * dx + (p.y - A.y) * dy) / l2)
        if q < last:
            q = last
        last = q
        g.addColorStop(q, stop(p))
    return g


def film_layer(C, off, keep, lw, alpha, colour, halo=1):
    def path(ctx):
        trace(ctx, C, off, keep)

    def paint(ctx, U):
        if U:
            return along(ctx, C, lambda p: ink(alpha(p) * U))
        return along(ctx, C, lambda p: rgba(colour(p), alpha(p)))

    return SimpleNamespace(lw=lw, halo=halo, path=path, paint=paint)


def ring_layer(c, r, lw, a, pal, phase):
    def path(ctx):
        ctx.beginPath()
        ctx.arc(c[0], c[1], r, 0, TAU)

    def paint(ctx, U):
        if U:
            return ink(a * U)
        g = ctx.createConicGradient(phase, c[0], c[1])
        for j in range(13):
            g.addColorStop(j / 12, rgba(hue(pal, phase + (j / 12) * len(pal) * 2), a))
        return g

    return SimpleNamespace(lw=lw, halo=1, path=path, paint=paint)


def render(ctx, s, layers):
    ctx.lineJoin = "round"
    ctx.lineCap = "round"
    U = 0.32 if s.dark else 0.58
    pad = 1.6 if s.dark else 2.4
    ctx.globalCompositeOperation = "source-over"
    for l in layers:
        h = l.halo
        l.path(ctx)
        ctx.lineWidth = l.lw + pad * h
        ctx.strokeStyle = l.paint(ctx, U * h)
        ctx.stroke()
    ctx.globalCompositeOperation = "lighter" if s.dark else "source-over"
    for l in layers:
        l.path(ctx)
        ctx.lineWidth = l.lw
        ctx.strokeStyle = l.paint(ctx, 0)
        ctx.stroke()


def sheen(ctx, s, C, off, keep, base, alpha, colour):
    runs = []
    cur = None
    for p in C.pts:
        if keep and not keep(p):
            cur = None
            continue
        if cur is None:
            cur = []
            runs.append(cur)
        cur.append(p)
    ctx.globalCompositeOperation = "lighter" if s.dark else "source-over"
    ctx.fillStyle = along(ctx, C, lambda p: rgba(colour(p), alpha(p)))
    ctx.beginPath()
    for run in runs:
        if len(run) < 2:
            continue
        for i, p in enumerate(run):
            o = off(p)
            x, y = p.x + p.nx * o, p.y + p.ny * o
            if i:
                ctx.lineTo(x, y)
            else:
                ctx.moveTo(x, y)
        for i in range(len(run) - 1, -1, -1):
            p = run[i]
            ctx.lineTo(p.x - p.nx * base, p.y - p.ny * base)
        ctx.closePath()
    ctx.fill()


def _sign(x):
    return (x > 0) - (x < 0)


def _depart(ctx, s, P):
    broke = s.since is not None
    t = s.since if broke else 0.0
    secs = P["departSeconds"]
    if broke and t >= secs:
        return
    p = 1.0 if broke else s.pressure
    if not (p > 0):
        return
    red = s.reduced
    pal = [rgb(h) for h in s.palette]
    geo = geo_of(s, True)
    notch = geo.kind == "notch"
    corner = geo.kind == "corner"
    g = 5 if notch else P["g"]
    L = 250 if notch else (P["L"] * 0.8 if corner else P["L"])
    u = t / secs
    if broke:
        level = ((1 - u) ** 1.25) * (1 + 0.6 * math.exp(-t * 18))
    else:
        level = smooth(0, 0.14, p) * (0.42 + 0.58 * p)
    tick = getattr(s, "tick", None)
    age = tick.age if (tick and not broke) else -1
    wob = math.exp(-age * 10) * math.sin(age * 44) if (age >= 0 and not red) else 0.0
    pulse = 0.5 * math.exp(-age * 9) if (age >= 0 and red) else 0.0
    w = 44.0 if red else lerp(86, 34, p)
    D = min(g, 0.6 * g if red else g * (0.18 + 0.82 * p) * (1 + 0.4 * wob))
    if broke and not red:
        D *= math.exp(-7 * t) * math.cos(21 * t)
    gap = (18.0 if red else 3 + P["retract"] * t) if broke else -1.0
    curl = P["curl"] * min(1.0, t * 14) if (broke and not red) else 0.0
    keep = (lambda q: abs(q.d) >= gap) if broke else None
    phase = 0.0 if red else 1.3 * p + (2.4 * t if broke else 0.0)
    pS = 0.5 if red else p
    rho = 20.0 if red else lerp(44, 9, p)

    def wave(d):
        if not P["waves"] or age < 0 or red:
            return 0.0
        front = abs(d) - 240 * age
        return 3.2 * math.exp(-3.2 * age) * math.sin(front / 5) * bump(front / 24)

    layers = []
    main = None
    for k in range(P["fringes"]):
        vis = clamp((p - k / 4) / 0.04) if k else 1.0
        if vis <= 0:
            continue
        C = contour(s, geo, g + k * P["sp"], L, {"rho": rho, "arm": 60})
        crowd = k * P["sp"] * 0.62 * pS

        def off(q, crowd=crowd, k=k):
            base = -(D + crowd) * bump(q.d / w) + wave(q.d)
            if curl:
                base += curl * smooth(gap + 16, gap, abs(q.d))
            return base

        lw = (1.1 if s.dark else 1.4) if k else P["lw"] * (0.85 if red else 1 - 0.3 * p)

        def alpha(q, vis=vis, k=k):
            a = level * vis * (1 + pulse) * (1 - smooth(0.45 * L, L, abs(q.d))) * smooth(0, 24, q.end)
            if notch:
                a *= 0.55 + 0.45 * bump(q.d / 150)
            if not broke:
                a *= 1 - 0.5 * (p ** 4) * bump(q.d / (0.45 * w))
            return a * (0.8 if k else 1.0)

        def colour(q, k=k):
            return hue(pal, phase + q.d / 88 + k * 0.9 + 1.4 * pS * bump(q.d / w))

        layers.append(film_layer(C, off, keep, lw, alpha, colour, (0.7 if s.dark else 0.4) if k else 1))
        if not k:
            main = SimpleNamespace(C=C, off=off, alpha=alpha, colour=colour)

    if P["sheen"]:
        sheen(ctx, s, main.C, main.off, keep, g, lambda q: main.alpha(q) * (0.2 if s.dark else 0.3), main.colour)
    render(ctx, s, layers)
    if P["beads"] and broke and not red:
        U = 0.32 if s.dark else 0.58
        for sgn in (-1, 1):
            q = None
            best = math.inf
            for c in main.C.pts:
                if _sign(c.d) == sgn and abs(c.d) >= gap and abs(c.d) - gap < best:
                    best = abs(c.d) - gap
                    q = c
            if q is None:
                continue
            a = main.alpha(q)
            if a <= 0.01:
                continue
            o = main.off(q)
            x, y = q.x + q.nx * o, q.y + q.ny * o
            r = 1.4 + 2.2 * min(1.0, t * 6)
            ctx.globalCompositeOperation = "source-over"
            ctx.fillStyle = ink(a * U)
            ctx.beginPath()
            ctx.arc(x, y, r + 1.2, 0, TAU)
            ctx.fill()
            ctx.globalCompositeOperation = "lighter" if s.dark else "source-over"
            ctx.fillStyle = rgba(main.colour(q), a)
            ctx.beginPath()
            ctx.arc(x, y, r, 0, TAU)
            ctx.fill()


def _arrive(ctx, s, P):
    t = s.since
    secs = P["arriveSeconds"]
    if not (t < secs):
        return
    red = s.reduced
    pal = [rgb(h) for h in s.palette]
    geo = geo_of(s, False)
    u = t / secs
    notch = geo.kind == "notch"
    corner = geo.kind == "corner"
    g = 6 if notch else P["g"]
    L = P["aL"] * 0.85 if corner else P["aL"]
    if red:
        level = smooth(0, 0.05, t) * (1 - smooth(0.4, 1, u))
    else:
        level = 1 - smooth(0.45, 1, u)
    gap = -1.0 if red else P["gap0"] * (max(0.0, 1 - t / 0.2) ** 1.5)
    bul = 0.45 * P["bulge"] if red else max(-0.7 * g, P["bulge"] * math.exp(-6.5 * t) * math.cos(17 * t))

    def wave(d):
        if not P["aWave"] or red:
            return 0.0
        front = abs(d) - 300 * t
        return 2.6 * math.exp(-3 * t) * math.sin(front / 5) * bump(front / 26)

    C = contour(s, geo, g, L, {"rho": 22, "arm": 64})
    keep = (lambda q: abs(q.d) >= gap) if gap > 0 else None

    def off(q):
        return bul * bump(q.d / 30) + wave(q.d)

    phase = 0.0 if red else 2.6 - 1.8 * u

    def alpha(q):
        return level * (1 - smooth(0.5 * L, L, abs(q.d))) * smooth(0, 20, q.end)

    def colour(q):
        return hue(pal, phase + q.d / 88 + 1.2 * bump(q.d / 30))

    layers = [film_layer(C, off, keep, P["lw"], alpha, colour)]
    for i, R in enumerate(P["rings"]):
        tau = t - i * P["stagger"]
        if tau < 0 or tau >= P["ringDur"]:
            continue
        v = tau / P["ringDur"]
        r = P["still"][i] if red else 8 + (R - 8) * ease_out(v)
        a = (math.sin(math.pi * v) if red else (1 - v) ** 1.3) * (0.85 if i else 1.0)
        lw = 1.5 if red else lerp(2.6, 0.9, v)
        if notch:
            Cr = contour(s, geo, g + 0.6 * r, 0, {"arm": 0})
            layers.append(film_layer(
                Cr, lambda q: 0, None, lw,
                (lambda q, a=a: a * (1 - smooth(50, 140, abs(q.d)))),
                (lambda q, i=i: hue(pal, phase + i * 1.1 + q.d / 70)),
            ))
        else:
            layers.append(ring_layer(geo.centre, r, lw, a, pal, phase + i * 1.1))

    if P["disc"]:
        c = geo.anchor if notch else geo.centre
        Rd = 64.0 if red else 10 + 104 * ease_out(min(1.0, t / 0.55))
        aD = (0.5 * math.sin(math.pi * u) if red else 0.6 * ((1 - u) ** 1.2)) * (1.0 if s.dark else 0.7)
        rg = ctx.createRadialGradient(c[0], c[1], 0, c[0], c[1], Rd)
        for j in range(17):
            q = j / 16
            rg.addColorStop(q, rgba(
                hue(pal, phase + q * 3),
                aD * (0.5 + 0.5 * math.cos(q * TAU * 3.5)) * ((1 - q) ** 0.8) * smooth(0, 0.15, q),
            ))
        ctx.globalCompositeOperation = "lighter" if s.dark else "source-over"
        ctx.fillStyle = rg
        ctx.beginPath()
        ctx.arc(c[0], c[1], Rd, 0, TAU)
        ctx.fill()
    render(ctx, s, layers)


def _make(id_, name, intensity, blurb, depart_seconds, arrive_seconds, P):
    def depart(ctx, s):
        _depart(ctx, s, P)

    def arrive(ctx, s):
        _arrive(ctx, s, P)

    return effects.Effect(id_, name, intensity, blurb, depart, arrive, depart_seconds, arrive_seconds)


EFFECTS = [
    _make("skin", "Skin", "quiet",
          "A single iridescent hairline sits just inside the edge and dimples under the pointer as "
          "you push, thinning and shifting colour. It parts at the pointer and the two ends pull "
          "back; on the other screen the same hairline closes behind the pointer and one ripple "
          "spreads out.",
          0.45, 0.6,
          {"departSeconds": 0.45, "arriveSeconds": 0.6, "g": 6, "L": 110, "lw": 2.0, "fringes": 1,
           "sp": 0, "retract": 300, "curl": 3, "sheen": False, "beads": False, "waves": False,
           "aL": 84, "gap0": 16, "bulge": 9, "rings": [58], "still": [40], "stagger": 0,
           "ringDur": 0.5, "disc": False, "aWave": False}),
    _make("film", "Film", "medium",
          "A band of interference fringes, one added at each quarter tick, crowds and thins into the "
          "dent under the pointer, so the band itself is the pressure meter. At breakthrough the film "
          "tears and each fringe peels back with a curled rim; on the other side the skin bulges in "
          "around the arriving pointer, springs back, heals, and throws two ripples.",
          0.55, 0.7,
          {"departSeconds": 0.55, "arriveSeconds": 0.7, "g": 7, "L": 150, "lw": 2.2, "fringes": 4,
           "sp": 3.6, "retract": 330, "curl": 5, "sheen": False, "beads": False, "waves": False,
           "aL": 110, "gap0": 26, "bulge": 14, "rings": [74, 104], "still": [44, 80],
           "stagger": 0.09, "ringDur": 0.55, "disc": False, "aWave": False}),
    _make("rupture", "Rupture", "showpiece",
          "The whole edge becomes a visible soap film with a sheen behind its fringes; every tick "
          "sends a shudder along it, and at breakthrough it bursts, the torn rims racing away along "
          "the edge carrying beads of film. The arrival opens a set of Newton's rings around the "
          "entry point while the skin bulges in, heals and sends a surface wave along the edge.",
          0.7, 0.8,
          {"departSeconds": 0.7, "arriveSeconds": 0.8, "g": 8, "L": 220, "lw": 2.6, "fringes": 4,
           "sp": 4, "retract": 420, "curl": 6, "sheen": True, "beads": True, "waves": True,
           "aL": 118, "gap0": 34, "bulge": 18, "rings": [70, 96, 118], "still": [36, 64, 92],
           "stagger": 0.08, "ringDur": 0.6, "disc": True, "aWave": True}),
]

PACKS = {
    "opal": ("Opal", ["#527cda", "#ac8cd2", "#d985b0", "#7eade0"]),
    "sea_glass": ("Sea glass", ["#48b8a0", "#8bd6b5", "#388f91", "#b4dfb0"]),
    "oil_slick": ("Oil slick", ["#8a4dff", "#2f7dff", "#16c6b0", "#9fdc4a", "#f0c23c", "#e8477f"]),
    "soap_bubble": ("Soap bubble", ["#ffd98e", "#ff9cc2", "#c3a6ff", "#8fcfff", "#9ef2cf"]),
    "pearl": ("Pearl", ["#fbf4e8", "#f2c9d6", "#c6dcf2", "#d9f0dc", "#ffffff"]),
}
