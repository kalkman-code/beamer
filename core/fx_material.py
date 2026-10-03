"""Boundary coordinates shared by the folded, woven and aperture effects."""

from .fx_instrument import frame


def surface(pen, s, departing):
    """Map along/inward coordinates round corners and outside the hardware notch."""
    if not departing and s.method == "switch":
        pen.translate(s.point.x, max(2, min(s.screen.h - 60, s.point.y - 18)))
        return lambda u, v: (u, v)
    k = frame(pen, s, departing)
    if k.kind == "corner":
        return lambda u, v: (u + v, v) if u >= 0 else (v, -u + v)
    if k.kind == "notch":
        def mapped(u, v):
            p = k.track.at(k.tp + u)
            return p.x + p.nx * v, p.y + p.ny * v
        return mapped
    return lambda u, v: (u, v)


def stroke(pen, colour, width, alpha, dark):
    """A narrow contrasting keyline preserves the material against busy wallpaper."""
    pen.lineJoin, pen.lineCap = "round", "round"
    pen.strokeStyle = "#000000"
    pen.lineWidth, pen.globalAlpha = width + 1.6, alpha * (0.4 if dark else 0.62)
    pen.stroke()
    pen.strokeStyle = colour
    pen.lineWidth, pen.globalAlpha = width, alpha
    pen.stroke()
