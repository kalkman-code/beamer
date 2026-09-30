"""Ink: liquid moving between vessels. A viscous drop swells, necks and drips through the boundary,
blooming into fluid wash on arrival.
"""

import math

from . import effects


def _colour_at(pal, i):
    """pal[i]; past the pack's length JS reads undefined there, and a colour assignment from
    undefined is invalid CSS, so canvas leaves fillStyle/strokeStyle at whatever it already was."""
    return pal[i] if i < len(pal) else None


def get_depart_geometry(s):
    r = s.region
    bx, by = s.point.x, s.point.y
    nx = ny = tx = ty = 0.0
    is_corner = r.kind == "corner"
    is_notch = r.kind == "notch"

    if is_notch:
        n = getattr(s, "notch", None)
        if n is None:
            from types import SimpleNamespace
            n = SimpleNamespace(x=771, y=0, w=185, h=32, r=10)
        bx = effects.clamp(s.point.x, n.x + 24, n.x + n.w - 24)
        by = n.h  # Bottom lip of the hardware notch
        nx, ny = 0, 1  # Inward into Mac screen space below notch
        tx, ty = 1, 0  # Along bottom lip
    elif is_corner:
        corner = getattr(r, "corner", "top_right")
        right, top = "right" in corner, "top" in corner
        bx, by = (s.screen.w if right else 0), (0 if top else s.screen.h)
        nx, ny = (-0.7071 if right else 0.7071), (0.7071 if top else -0.7071)
        tx, ty = ny, -nx
    else:
        edge = r.edge
        if edge == "right":
            bx, by = s.screen.w, s.point.y
            nx, ny, tx, ty = -1, 0, 0, 1
        elif edge == "left":
            bx, by = 0, s.point.y
            nx, ny, tx, ty = 1, 0, 0, 1
        elif edge == "top":
            bx, by = s.point.x, 0
            nx, ny, tx, ty = 0, 1, 1, 0
        else:  # bottom
            bx, by = s.point.x, s.screen.h
            nx, ny, tx, ty = 0, -1, 1, 0

    return bx, by, nx, ny, tx, ty, is_corner, is_notch


def get_arrive_geometry(s):
    if s.edge == "right":
        nx, ny = -1, 0
    elif s.edge == "left":
        nx, ny = 1, 0
    elif s.edge == "top":
        nx, ny = 0, 1
    else:
        nx, ny = 0, -1

    near_top, near_bottom = s.point.y < 50, s.point.y > s.screen.h - 50
    is_corner = s.method == "corner" and s.edge in ("left", "right") and (near_top or near_bottom)
    if is_corner:
        nx, ny = 0.7071 * nx, 0.7071 if near_top else -0.7071
    is_notch = s.method == "notch"
    return nx, ny, is_corner, is_notch


def inward_quarter(nx, ny):
    """The start and end angles of the quarter turn a corner's wedge fills, centred on the corner's
    inward normal: the design drew the top right corner's, pi/2 to pi, for every corner."""
    middle = math.atan2(ny, nx)
    return middle - math.pi / 4, middle + math.pi / 4


def tick_ripple(tick):
    if tick is None:
        return 0.0
    return math.sin(tick.age * 28) * math.exp(-tick.age * 7)


def draw_halo(ctx, s, alpha):
    if s.dark:
        ctx.strokeStyle = "#ffffff"
        ctx.globalAlpha = 0.28 * alpha
    else:
        ctx.strokeStyle = "#000000"
        ctx.globalAlpha = 0.16 * alpha


# ----------------------------------------------------
# Effect 1: Capillary (Quiet)
# ----------------------------------------------------

def _capillary_depart(ctx, s):
    pal = s.palette
    bx, by, nx, ny, tx, ty, is_corner, _is_notch = get_depart_geometry(s)

    since = getattr(s, "since", None)
    if since is not None:
        if since >= 0.45:
            return
        t = since / 0.45
        alpha = max(0.0, 1 - t * 1.5)
        if alpha <= 0.001:
            return

        if s.reduced:
            ctx.globalAlpha = alpha * 0.4
            ctx.fillStyle = pal[0]
            ctx.beginPath()
            ctx.arc(bx + nx * 4, by + ny * 4, 16, 0, math.pi * 2)
            ctx.fill()
            return

        # Snapped bead moving forward into the boundary
        bead_dist = t * 16
        bead_r = max(0.0, 4.5 * (1 - t))
        if bead_r > 0.5:
            ctx.globalAlpha = alpha * 0.85
            ctx.fillStyle = pal[0]
            ctx.beginPath()
            ctx.arc(bx - nx * bead_dist, by - ny * bead_dist, bead_r, 0, math.pi * 2)
            ctx.fill()

        # Meniscus recoil against wall
        recoil = max(0.0, 1 - t * 2.2)
        if recoil > 0.01:
            span = 36 * recoil
            depth = 8 * recoil
            ctx.globalAlpha = alpha * 0.5
            ctx.fillStyle = pal[min(1, len(pal) - 1)]
            ctx.beginPath()
            ctx.moveTo(bx - tx * span, by - ty * span)
            ctx.quadraticCurveTo(bx + nx * depth, by + ny * depth, bx + tx * span, by + ty * span)
            ctx.closePath()
            ctx.fill()
        return

    p = s.pressure
    if p <= 0.001:
        return

    if s.reduced:
        alpha = 0.15 + 0.65 * p
        if is_corner:
            ctx.globalAlpha = alpha * 0.4
            ctx.fillStyle = pal[len(pal) - 1]
            ctx.beginPath()
            ctx.arc(bx, by, 36, *inward_quarter(nx, ny))
            ctx.lineTo(bx, by)
            ctx.closePath()
            ctx.fill()

            ctx.globalAlpha = alpha * 0.85
            ctx.fillStyle = pal[0]
            ctx.beginPath()
            ctx.arc(bx, by, 22, *inward_quarter(nx, ny))
            ctx.lineTo(bx, by)
            ctx.closePath()
            ctx.fill()
        else:
            span = 42
            depth = 14
            ctx.globalAlpha = alpha * 0.4
            ctx.fillStyle = pal[len(pal) - 1]
            ctx.beginPath()
            ctx.moveTo(bx - tx * (span + 4), by - ty * (span + 4))
            ctx.quadraticCurveTo(bx + nx * (depth + 4), by + ny * (depth + 4), bx + tx * (span + 4), by + ty * (span + 4))
            ctx.closePath()
            ctx.fill()

            ctx.globalAlpha = alpha * 0.85
            ctx.fillStyle = pal[0]
            ctx.beginPath()
            ctx.moveTo(bx - tx * span, by - ty * span)
            ctx.quadraticCurveTo(bx + nx * depth, by + ny * depth, bx + tx * span, by + ty * span)
            ctx.closePath()
            ctx.fill()
        return

    ripple = tick_ripple(getattr(s, "tick", None))
    vis_p = effects.clamp(p ** 1.2 + ripple * 0.08, 0.0, 1.2)
    span = 24 + 48 * vis_p
    depth = 6 + 22 * vis_p

    if is_corner:
        cr = 8 + 36 * vis_p
        # Soft outer wash
        ctx.globalAlpha = 0.3 * p
        ctx.fillStyle = pal[len(pal) - 1]
        ctx.beginPath()
        ctx.moveTo(bx, by)
        ctx.arc(bx, by, cr + 6, *inward_quarter(nx, ny))
        ctx.closePath()
        ctx.fill()

        # Main pigment body
        ctx.globalAlpha = 0.7 * p
        ctx.fillStyle = pal[0]
        ctx.beginPath()
        ctx.moveTo(bx, by)
        ctx.arc(bx, by, cr, *inward_quarter(nx, ny))
        ctx.closePath()
        ctx.fill()

        # Dense inner core
        ctx.globalAlpha = 0.9 * p
        ctx.beginPath()
        ctx.moveTo(bx, by)
        ctx.arc(bx, by, cr * 0.55, *inward_quarter(nx, ny))
        ctx.closePath()
        ctx.fill()
        return

    # Straight edge and notch meniscus
    draw_halo(ctx, s, p * 0.4)
    ctx.lineWidth = 1.5 / s.scale
    ctx.beginPath()
    ctx.moveTo(bx - tx * (span + 4), by - ty * (span + 4))
    ctx.quadraticCurveTo(bx + nx * (depth + 4), by + ny * (depth + 4), bx + tx * (span + 4), by + ty * (span + 4))
    ctx.stroke()

    # Layer 1: Outer wash
    ctx.globalAlpha = 0.3 * p
    ctx.fillStyle = pal[min(2, len(pal) - 1)]
    ctx.beginPath()
    ctx.moveTo(bx - tx * (span + 6), by - ty * (span + 6))
    ctx.quadraticCurveTo(bx + nx * (depth + 5), by + ny * (depth + 5), bx + tx * (span + 6), by + ty * (span + 6))
    ctx.closePath()
    ctx.fill()

    # Layer 2: Main fluid body with wetting fillets
    ctx.globalAlpha = 0.7 * p
    ctx.fillStyle = pal[0]
    ctx.beginPath()
    ctx.moveTo(bx - tx * span, by - ty * span)
    ctx.bezierCurveTo(
        bx - tx * span * 0.5 + nx * depth * 0.4, by - ty * span * 0.5 + ny * depth * 0.4,
        bx - tx * span * 0.2 + nx * depth, by - ty * span * 0.2 + ny * depth,
        bx + nx * depth, by + ny * depth,
    )
    ctx.bezierCurveTo(
        bx + tx * span * 0.2 + nx * depth, by + ty * span * 0.2 + ny * depth,
        bx + tx * span * 0.5 + nx * depth * 0.4, by + ty * span * 0.5 + ny * depth * 0.4,
        bx + tx * span, by + ty * span,
    )
    ctx.closePath()
    ctx.fill()

    # Layer 3: Concentrated core near wall
    ctx.globalAlpha = 0.9 * p
    ctx.fillStyle = pal[0]
    ctx.beginPath()
    ctx.moveTo(bx - tx * (span * 0.6), by - ty * (span * 0.6))
    ctx.quadraticCurveTo(bx + nx * (depth * 0.5), by + ny * (depth * 0.5), bx + tx * (span * 0.6), by + ty * (span * 0.6))
    ctx.closePath()
    ctx.fill()


def _capillary_arrive(ctx, s):
    since = getattr(s, "since", None)
    if since is None or since >= 0.50 or since < 0:
        return
    pal = s.palette
    t = since / 0.50
    alpha = max(0.0, 1 - t)
    if alpha <= 0.001:
        return

    nx, ny, _is_corner, _is_notch = get_arrive_geometry(s)

    if s.reduced:
        a = math.sin(t * math.pi)
        ctx.globalAlpha = a * 0.4
        ctx.fillStyle = pal[len(pal) - 1]
        ctx.beginPath()
        ctx.arc(s.point.x, s.point.y, 32, 0, math.pi * 2)
        ctx.fill()

        ctx.globalAlpha = a * 0.8
        ctx.fillStyle = pal[0]
        ctx.beginPath()
        ctx.arc(s.point.x, s.point.y, 18, 0, math.pi * 2)
        ctx.fill()
        return

    u = effects.ease_out(t)
    radius = 10 + 44 * u
    cx = s.point.x + nx * (8 * u)
    cy = s.point.y + ny * (8 * u)

    # Halo for dark background contrast
    draw_halo(ctx, s, alpha * 0.6)
    ctx.lineWidth = 1.5 / s.scale
    ctx.beginPath()
    ctx.arc(cx, cy, radius + 2, 0, math.pi * 2)
    ctx.stroke()

    # Outer soft wash
    ctx.globalAlpha = alpha * 0.3
    ctx.fillStyle = pal[min(2, len(pal) - 1)]
    ctx.beginPath()
    ctx.arc(cx, cy, radius, 0, math.pi * 2)
    ctx.fill()

    # Mid pigment bleed
    ctx.globalAlpha = alpha * 0.6
    ctx.fillStyle = pal[min(1, len(pal) - 1)]
    ctx.beginPath()
    ctx.arc(cx, cy, radius * 0.62, 0, math.pi * 2)
    ctx.fill()

    # Dense landing core
    ctx.globalAlpha = alpha * 0.9
    ctx.fillStyle = pal[0]
    ctx.beginPath()
    ctx.arc(cx, cy, radius * 0.28, 0, math.pi * 2)
    ctx.fill()

    # Subtle capillary perimeter rim
    ctx.globalAlpha = alpha * 0.45
    ctx.strokeStyle = pal[0]
    ctx.lineWidth = 1 / s.scale
    ctx.beginPath()
    ctx.arc(cx, cy, radius, 0, math.pi * 2)
    ctx.stroke()


# ----------------------------------------------------
# Effect 2: Viscous Drop (Medium)
# ----------------------------------------------------

def _viscous_drop_depart(ctx, s):
    pal = s.palette
    bx, by, nx, ny, tx, ty, is_corner, _is_notch = get_depart_geometry(s)

    since = getattr(s, "since", None)
    if since is not None:
        if since >= 0.55:
            return
        t = since / 0.55
        alpha = max(0.0, 1 - t * 1.3)
        if alpha <= 0.001:
            return

        if s.reduced:
            ctx.globalAlpha = alpha * 0.5
            ctx.fillStyle = pal[0]
            ctx.beginPath()
            ctx.arc(bx + nx * 6, by + ny * 6, 22, 0, math.pi * 2)
            ctx.fill()
            return

        # Detached droplet travelling across boundary
        drop_dist = t * 24
        drop_r = max(0.0, 6 * (1 - t * 0.8))
        if drop_r > 0.5:
            ctx.globalAlpha = alpha * 0.9
            ctx.fillStyle = pal[0]
            ctx.beginPath()
            ctx.arc(bx - nx * drop_dist, by - ny * drop_dist, drop_r, 0, math.pi * 2)
            ctx.fill()

            # Secondary satellite droplet (capillary pinch-off)
            sat_dist = t * 12
            sat_r = max(0.0, 2.2 * (1 - t))
            if sat_r > 0.5:
                ctx.globalAlpha = alpha * 0.7
                ctx.beginPath()
                ctx.arc(bx - nx * sat_dist, by - ny * sat_dist, sat_r, 0, math.pi * 2)
                ctx.fill()

        # Snapped meniscus recoiling and rippling
        recoil = max(0.0, 1 - t * 1.8) * math.cos(t * 12)
        if abs(recoil) > 0.01:
            span = 42 * (1 - t)
            depth = 12 * recoil
            ctx.globalAlpha = alpha * 0.55
            ctx.fillStyle = pal[min(1, len(pal) - 1)]
            ctx.beginPath()
            ctx.moveTo(bx - tx * span, by - ty * span)
            ctx.quadraticCurveTo(bx + nx * depth, by + ny * depth, bx + tx * span, by + ty * span)
            ctx.closePath()
            ctx.fill()
        return

    p = s.pressure
    if p <= 0.001:
        return

    if s.reduced:
        alpha = 0.15 + 0.75 * p
        if is_corner:
            ctx.globalAlpha = alpha * 0.4
            ctx.fillStyle = pal[len(pal) - 1]
            ctx.beginPath()
            ctx.arc(bx, by, 44, *inward_quarter(nx, ny))
            ctx.lineTo(bx, by)
            ctx.closePath()
            ctx.fill()

            ctx.globalAlpha = alpha * 0.9
            ctx.fillStyle = pal[0]
            ctx.beginPath()
            ctx.arc(bx, by, 26, *inward_quarter(nx, ny))
            ctx.lineTo(bx, by)
            ctx.closePath()
            ctx.fill()
        else:
            span = 52
            depth = 18
            ctx.globalAlpha = alpha * 0.4
            ctx.fillStyle = pal[len(pal) - 1]
            ctx.beginPath()
            ctx.moveTo(bx - tx * (span + 4), by - ty * (span + 4))
            ctx.quadraticCurveTo(bx + nx * (depth + 4), by + ny * (depth + 4), bx + tx * (span + 4), by + ty * (span + 4))
            ctx.closePath()
            ctx.fill()

            ctx.globalAlpha = alpha * 0.9
            ctx.fillStyle = pal[0]
            ctx.beginPath()
            ctx.moveTo(bx - tx * span, by - ty * span)
            ctx.quadraticCurveTo(bx + nx * depth, by + ny * depth, bx + tx * span, by + ty * span)
            ctx.closePath()
            ctx.fill()
        return

    ripple = tick_ripple(getattr(s, "tick", None))
    vis_p = effects.clamp(p ** 1.25 + ripple * 0.12, 0.0, 1.3)
    span = 28 + 44 * vis_p
    depth = 8 + 32 * vis_p
    neck_w = span * (1 - vis_p * 0.38)

    if is_corner:
        cr = 10 + 44 * vis_p
        ctx.globalAlpha = 0.3 * p
        ctx.fillStyle = pal[len(pal) - 1]
        ctx.beginPath()
        ctx.moveTo(bx, by)
        ctx.arc(bx, by, cr + 8, *inward_quarter(nx, ny))
        ctx.closePath()
        ctx.fill()

        ctx.globalAlpha = 0.75 * p
        ctx.fillStyle = pal[0]
        ctx.beginPath()
        ctx.moveTo(bx, by)
        ctx.arc(bx, by, cr, *inward_quarter(nx, ny))
        ctx.closePath()
        ctx.fill()

        # Bulging droplet apex in corner
        hx, hy = bx + nx * (cr * 0.75), by + ny * (cr * 0.75)
        ctx.globalAlpha = 0.95 * p
        ctx.beginPath()
        ctx.arc(hx, hy, 10 + 12 * vis_p, 0, math.pi * 2)
        ctx.fill()
        return

    # Straight edge and notch necked droplet
    draw_halo(ctx, s, p * 0.45)
    ctx.lineWidth = 2 / s.scale
    ctx.beginPath()
    ctx.moveTo(bx - tx * (span + 6), by - ty * (span + 6))
    ctx.quadraticCurveTo(bx + nx * (depth + 6), by + ny * (depth + 6), bx + tx * (span + 6), by + ty * (span + 6))
    ctx.stroke()

    # Outer soft wash
    ctx.globalAlpha = 0.32 * p
    ctx.fillStyle = pal[min(2, len(pal) - 1)]
    ctx.beginPath()
    ctx.moveTo(bx - tx * (span + 5), by - ty * (span + 5))
    ctx.bezierCurveTo(
        bx - tx * neck_w + nx * (depth * 0.4), by - ty * neck_w + ny * (depth * 0.4),
        bx - tx * (span * 0.3) + nx * (depth + 4), by - ty * (span * 0.3) + ny * (depth + 4),
        bx + nx * (depth + 4), by + ny * (depth + 4),
    )
    ctx.bezierCurveTo(
        bx + tx * (span * 0.3) + nx * (depth + 4), by + ty * (span * 0.3) + ny * (depth + 4),
        bx + tx * neck_w + nx * (depth * 0.4), by + ty * neck_w + ny * (depth * 0.4),
        bx + tx * (span + 5), by + ty * (span + 5),
    )
    ctx.closePath()
    ctx.fill()

    # Main viscous droplet with necking
    ctx.globalAlpha = 0.8 * p
    ctx.fillStyle = pal[0]
    ctx.beginPath()
    ctx.moveTo(bx - tx * span, by - ty * span)
    ctx.bezierCurveTo(
        bx - tx * neck_w * 0.8 + nx * (depth * 0.3), by - ty * neck_w * 0.8 + ny * (depth * 0.3),
        bx - tx * (neck_w * 0.4) + nx * (depth * 0.8), by - ty * (neck_w * 0.4) + ny * (depth * 0.8),
        bx + nx * depth, by + ny * depth,
    )
    ctx.bezierCurveTo(
        bx + tx * (neck_w * 0.4) + nx * (depth * 0.8), by + ty * (neck_w * 0.4) + ny * (depth * 0.8),
        bx + tx * neck_w * 0.8 + nx * (depth * 0.3), by + ty * neck_w * 0.8 + ny * (depth * 0.3),
        bx + tx * span, by + ty * span,
    )
    ctx.closePath()
    ctx.fill()

    # Dense concentrated core droplet at apex
    ctx.globalAlpha = 0.95 * p
    ctx.fillStyle = pal[0]
    ctx.beginPath()
    ctx.arc(bx + nx * (depth * 0.72), by + ny * (depth * 0.72), 6 + 10 * vis_p, 0, math.pi * 2)
    ctx.fill()


def _viscous_drop_arrive(ctx, s):
    since = getattr(s, "since", None)
    if since is None or since >= 0.60 or since < 0:
        return
    pal = s.palette
    t = since / 0.60
    alpha = max(0.0, 1 - t)
    if alpha <= 0.001:
        return

    nx, ny, _is_corner, _is_notch = get_arrive_geometry(s)

    if s.reduced:
        a = math.sin(t * math.pi)
        cx, cy = s.point.x, s.point.y
        ctx.globalAlpha = a * 0.42
        ctx.fillStyle = pal[len(pal) - 1]
        ctx.beginPath()
        for i in range(0, 37):
            ang = (i / 36) * math.pi * 2
            r = 38 * (1 + 0.2 * math.sin(6 * ang))
            px, py = cx + math.cos(ang) * r, cy + math.sin(ang) * r
            if i == 0:
                ctx.moveTo(px, py)
            else:
                ctx.lineTo(px, py)
        ctx.closePath()
        ctx.fill()

        ctx.globalAlpha = a * 0.85
        ctx.fillStyle = pal[0]
        ctx.beginPath()
        ctx.arc(cx, cy, 20, 0, math.pi * 2)
        ctx.fill()
        return

    u = effects.ease_out(t)
    base_r = 12 + 62 * u
    cx = s.point.x + nx * (12 * u)
    cy = s.point.y + ny * (12 * u)

    # Organic 6-lobed bloom boundary
    draw_halo(ctx, s, alpha * 0.65)
    ctx.lineWidth = 1.5 / s.scale
    ctx.beginPath()
    for i in range(0, 41):
        a = (i / 40) * math.pi * 2
        r = (base_r + 2) * (1 + 0.25 * math.sin(6 * a + 0.4) + 0.08 * math.cos(3 * a))
        px, py = cx + math.cos(a) * r, cy + math.sin(a) * r
        if i == 0:
            ctx.moveTo(px, py)
        else:
            ctx.lineTo(px, py)
    ctx.closePath()
    ctx.stroke()

    # Layer 1: Outer wash bloom
    ctx.globalAlpha = alpha * 0.32
    ctx.fillStyle = pal[min(2, len(pal) - 1)]
    ctx.beginPath()
    for i in range(0, 41):
        a = (i / 40) * math.pi * 2
        r = base_r * (1 + 0.26 * math.sin(6 * a + 0.4) + 0.08 * math.cos(3 * a))
        px, py = cx + math.cos(a) * r, cy + math.sin(a) * r
        if i == 0:
            ctx.moveTo(px, py)
        else:
            ctx.lineTo(px, py)
    ctx.closePath()
    ctx.fill()

    # Layer 2: Mid-tone pigment diffusion
    ctx.globalAlpha = alpha * 0.62
    ctx.fillStyle = pal[min(1, len(pal) - 1)]
    ctx.beginPath()
    for i in range(0, 33):
        a = (i / 32) * math.pi * 2
        r = (base_r * 0.62) * (1 + 0.22 * math.sin(5 * a + 1.2))
        px, py = cx + math.cos(a) * r, cy + math.sin(a) * r
        if i == 0:
            ctx.moveTo(px, py)
        else:
            ctx.lineTo(px, py)
    ctx.closePath()
    ctx.fill()

    # Layer 3: Central dense impact droplet
    ctx.globalAlpha = alpha * 0.92
    ctx.fillStyle = pal[0]
    ctx.beginPath()
    ctx.arc(cx, cy, base_r * 0.28, 0, math.pi * 2)
    ctx.fill()

    # 8 micro-satellite droplets radiating outward
    num_sats = 8
    for j in range(num_sats):
        ang = (j / num_sats) * math.pi * 2 + s.hash(j) * 0.4
        dist = base_r * 0.7 + 36 * u * s.hash(j + 10)
        sat_r = max(0.0, (2.8 - 2.2 * u) * s.hash(j + 20))
        if sat_r > 0.4:
            ctx.globalAlpha = alpha * 0.75 * (1 - u * 0.6)
            colour = _colour_at(pal, 0 if j % 2 == 0 else 1)
            if colour is not None:
                ctx.fillStyle = colour
            ctx.beginPath()
            ctx.arc(cx + math.cos(ang) * dist, cy + math.sin(ang) * dist, sat_r, 0, math.pi * 2)
            ctx.fill()


# ----------------------------------------------------
# Effect 3: Sumi Bloom (Showpiece)
# ----------------------------------------------------

def _sumi_bloom_depart(ctx, s):
    pal = s.palette
    bx, by, nx, ny, tx, ty, is_corner, _is_notch = get_depart_geometry(s)

    since = getattr(s, "since", None)
    if since is not None:
        if since >= 0.70:
            return
        t = since / 0.70
        alpha = max(0.0, 1 - t * 1.25)
        if alpha <= 0.001:
            return

        if s.reduced:
            ctx.globalAlpha = alpha * 0.6
            ctx.fillStyle = pal[0]
            ctx.beginPath()
            ctx.arc(bx + nx * 8, by + ny * 8, 28, 0, math.pi * 2)
            ctx.fill()
            return

        # Rayleigh-Plateau droplet train breaking into seam
        num_drops = 4
        for k in range(num_drops):
            lead = t * (28 + k * 10)
            r_drop = max(0.0, (5.5 - k * 1.1) * (1 - t * 0.9))
            if r_drop > 0.4:
                ctx.globalAlpha = alpha * (0.92 - k * 0.15)
                colour = _colour_at(pal, 0 if k % 2 == 0 else 1)
                if colour is not None:
                    ctx.fillStyle = colour
                ctx.beginPath()
                ctx.arc(bx - nx * lead + tx * ((s.hash(k + 30) - 0.5) * 8), by - ny * lead + ty * ((s.hash(k + 30) - 0.5) * 8), r_drop, 0, math.pi * 2)
                ctx.fill()

        # Parent meniscus recoil & damped oscillation
        recoil = max(0.0, 1 - t * 1.6) * math.cos(t * 14)
        if abs(recoil) > 0.01:
            span = 56 * (1 - t)
            depth = 16 * recoil
            ctx.globalAlpha = alpha * 0.65
            ctx.fillStyle = pal[0]
            ctx.beginPath()
            ctx.moveTo(bx - tx * span, by - ty * span)
            ctx.bezierCurveTo(
                bx - tx * (span * 0.4) + nx * depth, by - ty * (span * 0.4) + ny * depth,
                bx + tx * (span * 0.4) + nx * depth, by + ty * (span * 0.4) + ny * depth,
                bx + tx * span, by + ty * span,
            )
            ctx.closePath()
            ctx.fill()
        return

    p = s.pressure
    if p <= 0.001:
        return

    if s.reduced:
        alpha = 0.2 + 0.8 * p
        if is_corner:
            ctx.globalAlpha = alpha * 0.45
            ctx.fillStyle = pal[len(pal) - 1]
            ctx.beginPath()
            ctx.arc(bx, by, 54, *inward_quarter(nx, ny))
            ctx.lineTo(bx, by)
            ctx.closePath()
            ctx.fill()

            ctx.globalAlpha = alpha * 0.95
            ctx.fillStyle = pal[0]
            ctx.beginPath()
            ctx.arc(bx, by, 32, *inward_quarter(nx, ny))
            ctx.lineTo(bx, by)
            ctx.closePath()
            ctx.fill()
        else:
            span = 64
            depth = 24
            ctx.globalAlpha = alpha * 0.45
            ctx.fillStyle = pal[len(pal) - 1]
            ctx.beginPath()
            ctx.moveTo(bx - tx * (span + 6), by - ty * (span + 6))
            ctx.quadraticCurveTo(bx + nx * (depth + 6), by + ny * (depth + 6), bx + tx * (span + 6), by + ty * (span + 6))
            ctx.closePath()
            ctx.fill()

            ctx.globalAlpha = alpha * 0.95
            ctx.fillStyle = pal[0]
            ctx.beginPath()
            ctx.moveTo(bx - tx * span, by - ty * span)
            ctx.quadraticCurveTo(bx + nx * depth, by + ny * depth, bx + tx * span, by + ty * span)
            ctx.closePath()
            ctx.fill()
        return

    ripple = tick_ripple(getattr(s, "tick", None))
    vis_p = effects.clamp(p ** 1.3 + ripple * 0.16, 0.0, 1.35)
    span = 36 + 54 * vis_p
    depth = 12 + 42 * vis_p
    neck_w = span * (1 - vis_p * 0.46)

    if is_corner:
        cr = 14 + 56 * vis_p
        ctx.globalAlpha = 0.35 * p
        ctx.fillStyle = pal[len(pal) - 1]
        ctx.beginPath()
        ctx.moveTo(bx, by)
        ctx.arc(bx, by, cr + 10, *inward_quarter(nx, ny))
        ctx.closePath()
        ctx.fill()

        ctx.globalAlpha = 0.8 * p
        ctx.fillStyle = pal[0]
        ctx.beginPath()
        ctx.moveTo(bx, by)
        ctx.arc(bx, by, cr, *inward_quarter(nx, ny))
        ctx.closePath()
        ctx.fill()

        # Fluid bridge head pulling diagonally inward
        hx, hy = bx + nx * (cr * 0.8), by + ny * (cr * 0.8)
        ctx.globalAlpha = 0.95 * p
        ctx.beginPath()
        ctx.arc(hx, hy, 12 + 16 * vis_p, 0, math.pi * 2)
        ctx.fill()
        return

    # Straight edge and notch: full fluid bridge with Rayleigh instability ripples
    draw_halo(ctx, s, p * 0.55)
    ctx.lineWidth = 2 / s.scale
    ctx.beginPath()
    ctx.moveTo(bx - tx * (span + 8), by - ty * (span + 8))
    ctx.quadraticCurveTo(bx + nx * (depth + 8), by + ny * (depth + 8), bx + tx * (span + 8), by + ty * (span + 8))
    ctx.stroke()

    # Layer 1: Wide pigment seepage
    ctx.globalAlpha = 0.35 * p
    ctx.fillStyle = pal[min(2, len(pal) - 1)]
    ctx.beginPath()
    ctx.moveTo(bx - tx * (span + 8), by - ty * (span + 8))
    ctx.bezierCurveTo(
        bx - tx * neck_w + nx * (depth * 0.4), by - ty * neck_w + ny * (depth * 0.4),
        bx - tx * (span * 0.25) + nx * (depth + 6), by - ty * (span * 0.25) + ny * (depth + 6),
        bx + nx * (depth + 6), by + ny * (depth + 6),
    )
    ctx.bezierCurveTo(
        bx + tx * (span * 0.25) + nx * (depth + 6), by + ty * (span * 0.25) + ny * (depth + 6),
        bx + tx * neck_w + nx * (depth * 0.4), by + ty * neck_w + ny * (depth * 0.4),
        bx + tx * (span + 8), by + ty * (span + 8),
    )
    ctx.closePath()
    ctx.fill()

    # Layer 2: Main calligraphic fluid bridge with necking
    ctx.globalAlpha = 0.82 * p
    ctx.fillStyle = pal[0]
    ctx.beginPath()
    ctx.moveTo(bx - tx * span, by - ty * span)
    ctx.bezierCurveTo(
        bx - tx * neck_w * 0.85 + nx * (depth * 0.35), by - ty * neck_w * 0.85 + ny * (depth * 0.35),
        bx - tx * (neck_w * 0.35) + nx * (depth * 0.85), by - ty * (neck_w * 0.35) + ny * (depth * 0.85),
        bx + nx * depth, by + ny * depth,
    )
    ctx.bezierCurveTo(
        bx + tx * (neck_w * 0.35) + nx * (depth * 0.85), by + ty * (neck_w * 0.35) + ny * (depth * 0.85),
        bx + tx * neck_w * 0.85 + nx * (depth * 0.35), by + ty * neck_w * 0.85 + ny * (depth * 0.35),
        bx + tx * span, by + ty * span,
    )
    ctx.closePath()
    ctx.fill()

    # Layer 3: Pendent droplet bulb with intense Sumi density
    ctx.globalAlpha = 0.95 * p
    ctx.fillStyle = pal[0]
    ctx.beginPath()
    ctx.arc(bx + nx * (depth * 0.76), by + ny * (depth * 0.76), 8 + 14 * vis_p, 0, math.pi * 2)
    ctx.fill()


def _sumi_bloom_arrive(ctx, s):
    since = getattr(s, "since", None)
    if since is None or since >= 0.75 or since < 0:
        return
    pal = s.palette
    t = since / 0.75
    alpha = max(0.0, 1 - t)
    if alpha <= 0.001:
        return

    nx, ny, _is_corner, _is_notch = get_arrive_geometry(s)

    if s.reduced:
        a = math.sin(t * math.pi)
        cx, cy = s.point.x, s.point.y
        ctx.globalAlpha = a * 0.45
        ctx.fillStyle = pal[len(pal) - 1]
        ctx.beginPath()
        ctx.arc(cx, cy, 42, 0, math.pi * 2)
        ctx.fill()

        ctx.globalAlpha = a * 0.75
        colour = _colour_at(pal, 1)
        if colour is not None:
            ctx.fillStyle = colour
        ctx.beginPath()
        ctx.arc(cx, cy, 26, 0, math.pi * 2)
        ctx.fill()

        ctx.globalAlpha = a * 0.95
        ctx.fillStyle = pal[0]
        ctx.beginPath()
        ctx.arc(cx, cy, 14, 0, math.pi * 2)
        ctx.fill()
        return

    u = effects.ease_out(t)
    base_r = 14 + 76 * u
    cx = s.point.x + nx * (14 * u)
    cy = s.point.y + ny * (14 * u)

    # Halo for contrast
    draw_halo(ctx, s, alpha * 0.75)
    ctx.lineWidth = 1.5 / s.scale
    ctx.beginPath()
    ctx.arc(cx, cy, base_r + 2, 0, math.pi * 2)
    ctx.stroke()

    # Layer 1: Outermost dilute pigment wash
    ctx.globalAlpha = alpha * 0.28
    ctx.fillStyle = pal[len(pal) - 1]
    ctx.beginPath()
    for i in range(0, 45):
        a = (i / 44) * math.pi * 2
        r = base_r * (1 + 0.22 * math.sin(7 * a + 0.5) + 0.1 * math.cos(3 * a))
        px, py = cx + math.cos(a) * r, cy + math.sin(a) * r
        if i == 0:
            ctx.moveTo(px, py)
        else:
            ctx.lineTo(px, py)
    ctx.closePath()
    ctx.fill()

    # Layer 2: Mid-tone pigment diffusion
    ctx.globalAlpha = alpha * 0.55
    ctx.fillStyle = pal[min(2, len(pal) - 1)]
    ctx.beginPath()
    for i in range(0, 37):
        a = (i / 36) * math.pi * 2
        r = (base_r * 0.68) * (1 + 0.24 * math.sin(5 * a + 1.1))
        px, py = cx + math.cos(a) * r, cy + math.sin(a) * r
        if i == 0:
            ctx.moveTo(px, py)
        else:
            ctx.lineTo(px, py)
    ctx.closePath()
    ctx.fill()

    # Layer 3: Inner dense wash
    ctx.globalAlpha = alpha * 0.8
    ctx.fillStyle = pal[min(1, len(pal) - 1)]
    ctx.beginPath()
    ctx.arc(cx, cy, base_r * 0.38, 0, math.pi * 2)
    ctx.fill()

    # Layer 4: Deep Sumi stone core
    ctx.globalAlpha = alpha * 0.95
    ctx.fillStyle = pal[0]
    ctx.beginPath()
    ctx.arc(cx, cy, base_r * 0.18, 0, math.pi * 2)
    ctx.fill()

    # 16 curling ink-in-water filament tendrils
    num_filaments = 16
    ctx.lineWidth = (1.5 - 0.8 * u) / s.scale
    for m in range(num_filaments):
        start_ang = (m / num_filaments) * math.pi * 2 + s.hash(m + 50) * 0.3
        curl_dir = 1 if m % 2 == 0 else -1
        r1 = base_r * 0.35
        r2 = base_r * (0.8 + 0.35 * s.hash(m + 70))
        x1 = cx + math.cos(start_ang) * r1
        y1 = cy + math.sin(start_ang) * r1
        mid_ang = start_ang + curl_dir * (0.4 + 0.3 * u)
        x_mid = cx + math.cos(mid_ang) * (r1 + (r2 - r1) * 0.55)
        y_mid = cy + math.sin(mid_ang) * (r1 + (r2 - r1) * 0.55)
        end_ang = mid_ang + curl_dir * 0.25
        x2 = cx + math.cos(end_ang) * r2
        y2 = cy + math.sin(end_ang) * r2

        ctx.globalAlpha = alpha * 0.7 * (1 - u * 0.4)
        colour = _colour_at(pal, m % 3)
        if colour is not None:
            ctx.strokeStyle = colour
        ctx.beginPath()
        ctx.moveTo(x1, y1)
        ctx.quadraticCurveTo(x_mid, y_mid, x2, y2)
        ctx.stroke()


EFFECTS = [
    effects.Effect(
        "capillary", "Capillary", "quiet",
        "A soft pigment meniscus swells against the boundary with viscous surface tension. On "
        "breakthrough, it releases cleanly and diffuses as a calm, translucent wash on the "
        "arrival screen.",
        _capillary_depart, _capillary_arrive, 0.45, 0.50,
    ),
    effects.Effect(
        "viscous_drop", "Viscous drop", "medium",
        "An organic ink droplet gathers at the pointer, flattening against the glass edge before "
        "necking through the seam. On arrival, it splashes into a multi-lobed pigment bloom that "
        "bleeds outward.",
        _viscous_drop_depart, _viscous_drop_arrive, 0.55, 0.60,
    ),
    effects.Effect(
        "sumi_bloom", "Sumi bloom", "showpiece",
        "Dense calligraphic ink pulls from the boundary into a viscous bridge that necks and "
        "snaps through the seam. It arrives as an ink-in-water vortex bloom, unfurling delicate "
        "pigment filaments that dissolve into the screen.",
        _sumi_bloom_depart, _sumi_bloom_arrive, 0.70, 0.75,
    ),
]

PACKS = {
    "indigo_ink": ("Indigo ink", ["#16243b", "#253d66", "#3f639e", "#6891ce", "#a8c5ed"]),
    "matcha": ("Matcha", ["#233818", "#3e5c2c", "#638a44", "#92b86e", "#cde6b0"]),
    "terracotta": ("Terracotta", ["#4d1f16", "#7c3423", "#b35338", "#d97b5d", "#f3b59f"]),
}
