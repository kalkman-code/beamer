"""The crossing effects on this PC: effects.Player's frames replayed into QPainter on one
click-through window, sized each frame to what the effect drew.

effects.py records every effect as operations in desktop points; `replay` turns them into Qt paths,
brushes and pens. The window is today's EdgeGlow pattern: frameless, translucent, input passes
through it, a tool window (so no taskbar button and no Alt-Tab entry), above everything, the
taskbar included, never activated. It repaints at 60Hz only while the Player is busy and hides as soon as it is not.

Any exception from an effect or the replay is logged once with the effect's id and turns the new
effects off for the rest of the run; the app then falls back to today's glow.
"""

from __future__ import annotations

import ctypes
import logging
import math
import sys
import time

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QConicalGradient,
    QCursor,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

import app_config
import edge_glow
from core import effects
from core import return_edge as crossing

LOGGER = logging.getLogger(__name__)

FRAME_MS = 16
# How long without a pressure update before the push is taken to have stopped and drains on its
# own clock, as EdgeGlow's does.
STALE_SECONDS = 0.08
MARGIN = 2
# canvas's miterLimit is 10 miter lengths, tip to inner corner; Qt measures from the join point in
# pen widths, which is half that.
MITER_LIMIT = 5.0

_CAPS = {"butt": Qt.PenCapStyle.FlatCap, "round": Qt.PenCapStyle.RoundCap, "square": Qt.PenCapStyle.SquareCap}
_JOINS = {"miter": Qt.PenJoinStyle.MiterJoin, "round": Qt.PenJoinStyle.RoundJoin, "bevel": Qt.PenJoinStyle.BevelJoin}
_COMPOSITE = {
    "source-over": QPainter.CompositionMode.CompositionMode_SourceOver,
    "lighter": QPainter.CompositionMode.CompositionMode_Plus,
}


def qcolour(rgba) -> QColor:
    r, g, b, a = (min(1.0, max(0.0, float(v))) for v in rgba)
    return QColor.fromRgbF(r, g, b, a)


def qpath(path, rule: str = "nonzero") -> QPainterPath:
    """A recorded path as a QPainterPath. After a close, canvas carries on from the subpath's start;
    Qt starts the next subpath at the origin, so a close not followed by a move gets one."""
    out = QPainterPath()
    out.setFillRule(Qt.FillRule.OddEvenFill if rule == "evenodd" else Qt.FillRule.WindingFill)
    start = None
    closed = False
    for segment in path:
        kind = segment[0]
        if closed and kind != "M" and start is not None:
            out.moveTo(*start)
        closed = False
        if kind == "M":
            start = (segment[1], segment[2])
            out.moveTo(*start)
        elif kind == "L":
            out.lineTo(segment[1], segment[2])
        elif kind == "Q":
            out.quadTo(segment[1], segment[2], segment[3], segment[4])
        elif kind == "C":
            out.cubicTo(segment[1], segment[2], segment[3], segment[4], segment[5], segment[6])
        elif kind == "Z":
            out.closeSubpath()
            closed = True
    return out


def _stops(stops, reverse: bool = False):
    """canvas stops as Qt's: sorted by offset keeping the order they were added, and a stop at
    the same offset as the one before nudged just past it, since Qt keeps only one stop per offset
    and canvas draws a hard edge there. `reverse` mirrors them, for the conic's opposite winding."""
    ordered = [(1.0 - offset if reverse else offset, colour) for offset, colour in stops]
    if reverse:
        ordered.reverse()
    ordered.sort(key=lambda stop: stop[0])
    out = []
    for offset, colour in ordered:
        if out and offset <= out[-1][0]:
            offset = min(1.0, out[-1][0] + 1e-6)
            if offset <= out[-1][0]:
                continue
        out.append((offset, colour))
    return out


def qgradient(gradient):
    """A recorded Gradient as a Qt gradient. Qt's two-circle radial takes the end circle first;
    its conic runs anticlockwise in degrees where canvas's runs clockwise in radians, both from
    three o'clock, so the angle is negated and the stops reversed."""
    g = gradient.geometry
    if gradient.kind == "linear":
        out = QLinearGradient(g[0], g[1], g[2], g[3])
        stops = _stops(gradient.stops)
    elif gradient.kind == "radial":
        out = QRadialGradient(QPointF(g[3], g[4]), g[5], QPointF(g[0], g[1]), g[2])
        stops = _stops(gradient.stops)
    else:
        out = QConicalGradient(g[1], g[2], -math.degrees(g[0]))
        stops = _stops(gradient.stops, reverse=True)
    for offset, colour in stops:
        out.setColorAt(offset, qcolour(colour))
    return out


def replay(painter, pens) -> None:
    """Draws each Pen's ops in order, inside its display when it names one. Every op sets its own
    opacity and composition and puts the painter back afterwards, so nothing leaks from one op to
    the next. Each Gradient becomes one Qt gradient for the frame, however many ops share it."""
    made = {}
    for pen in pens:
        clip = getattr(pen, "clip", None)
        if clip is not None:
            painter.save()
            painter.setClipRect(QRectF(clip[0], clip[1], clip[2] - clip[0], clip[3] - clip[1]))
        for op in pen.ops:
            paint = op[2]
            if isinstance(paint, effects.Gradient):
                key = id(paint)
                if key not in made:
                    made[key] = QBrush(qgradient(paint))
                brush = made[key]
            else:
                brush = QBrush(qcolour(paint))
            painter.save()
            painter.setOpacity(op[3])
            painter.setCompositionMode(_COMPOSITE.get(op[4], _COMPOSITE["source-over"]))
            if op[0] == "fill":
                painter.fillPath(qpath(op[1], op[5]), brush)
            else:
                stroke = QPen(brush, op[5], Qt.PenStyle.SolidLine, _CAPS.get(op[6], Qt.PenCapStyle.FlatCap),
                              _JOINS.get(op[7], Qt.PenJoinStyle.MiterJoin))
                stroke.setMiterLimit(MITER_LIMIT)
                painter.strokePath(qpath(op[1]), stroke)
            painter.restore()
        if clip is not None:
            painter.restore()


def union_bounds(pens, pad: float = 0.0):
    """The box round every Pen's bounds grown by `pad`, never past the display a Pen is clipped to,
    or None."""
    boxes = []
    for pen in pens:
        if pen.bounds is None:
            continue
        b = (pen.bounds[0] - pad, pen.bounds[1] - pad, pen.bounds[2] + pad, pen.bounds[3] + pad)
        clip = getattr(pen, "clip", None)
        if clip is not None:
            b = (max(b[0], clip[0]), max(b[1], clip[1]), min(b[2], clip[2]), min(b[3], clip[3]))
        boxes.append(b)
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def system_reduced_motion() -> bool:
    """Windows' "Show animations in Windows" switched off."""
    if sys.platform != "win32":
        return False
    SPI_GETCLIENTAREAANIMATION = 0x1042
    value = ctypes.c_int(1)
    try:
        if ctypes.windll.user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(value), 0):
            return not value.value
    except (AttributeError, OSError):
        pass
    return False


system_dark = edge_glow.system_dark


def desktop_box() -> QRect:
    """The bounding box of every display, in Qt's logical coordinates."""
    box = QRect()
    for screen in QGuiApplication.screens():
        box = box.united(screen.geometry())
    return box


def logical_point(x: float, y: float) -> QPointF:
    """A point in Windows' physical desktop pixels, as the receiver and sender place the pointer, in
    Qt's logical coordinates. Qt keeps each screen's top-left where Windows has it and scales
    everything else in the screen by its device pixel ratio."""
    for screen in QGuiApplication.screens():
        g = screen.geometry()
        ratio = screen.devicePixelRatio() or 1.0
        if g.x() <= x < g.x() + g.width() * ratio and g.y() <= y < g.y() + g.height() * ratio:
            return QPointF(g.x() + (x - g.x()) / ratio, g.y() + (y - g.y()) / ratio)
    return QPointF(x, y)


def corner_region(corner: str, edge: str, rects, origin, pointer) -> dict:
    """The effect's region for a push into `corner` of the display the pinned `pointer` is on: the
    corner's own box, as the Mac's engine reports one, relative to the desktop's top-left `origin`."""
    owner = next((r for r in rects if r.x <= pointer[0] < r.x + r.width and r.y <= pointer[1] < r.y + r.height),
                 None) or crossing.owning_monitor(rects, edge)
    vertical, horizontal = corner.split("_")
    size = crossing.CORNER_PX
    x = owner.x if horizontal == "left" else owner.x + owner.width - size
    y = owner.y if vertical == "top" else owner.y + owner.height - size
    return {"kind": "corner", "edge": edge, "corner": corner, "x": float(x - origin[0]), "y": float(y - origin[1]),
            "w": float(size), "h": float(size)}


def edge_region(edge: str, rects, origin, pointer=None, part=None) -> dict:
    """The effect's region for a push through `edge` of this PC: the whole of that edge on the
    display the pinned `pointer` is on, a point deep, relative to the desktop's top-left `origin`.
    That is not always the display at the outermost edge: a shorter display's exposed edge crosses
    too. With no pointer on any display, the one that owns the edge. For Part of the edge, `part`
    cuts it to the third being pushed."""
    owner = next((r for r in rects if pointer is not None and r.x <= pointer[0] < r.x + r.width
                  and r.y <= pointer[1] < r.y + r.height), None) or crossing.owning_monitor(rects, edge)
    owner = edge_glow.part_of_monitor(owner, edge, part)
    ox, oy = origin
    if edge in ("left", "right"):
        x = owner.x if edge == "left" else owner.x + owner.width - 1
        return {"kind": "edge", "edge": edge, "x": float(x - ox), "y": float(owner.y - oy), "w": 1.0, "h": float(owner.height)}
    y = owner.y if edge == "top" else owner.y + owner.height - 1
    return {"kind": "edge", "edge": edge, "x": float(owner.x - ox), "y": float(y - oy), "w": float(owner.width), "h": 1.0}


class EffectOverlay(QWidget):
    """One window for every effect on this PC, departures and arrivals alike."""

    def __init__(self, on_failure=None) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.failed = False
        self._on_failure = on_failure
        self.player = effects.Player()
        self._style = None
        self._fx = None
        self._palette = ()
        self._reduced = False
        self._dark = True
        self._desk = QRect()
        self._displays = []
        self._pens = []
        self._offset = (0.0, 0.0)
        self._push = None
        self._ticked_at = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._tick)

    def configure(self, style: str, colour: str, length: str = "normal") -> None:
        if style != self._style:
            self._style = style
            self.player = effects.Player()
            self._push = None
            try:
                # Glow and Beam are EdgeGlow's and have no arrival: all this window plays for them
                # is a switch, by the locator.
                self._fx = effects.effect(style) if style in effects.EFFECT_IDS else effects.LOCATOR
            except Exception:
                self._fx = None
                self._fail("loading")
                return
        # Read each time, like the colours; a Player already playing keeps the pace it started with.
        if not self.player.busy:
            self.player.pace = effects.pace(length)
        try:
            self._palette = app_config.palette_colours(colour)
        except Exception:
            self._fail("loading its colours")

    def push(self, edge: str, pressure: float, crossed: bool, cursor: QPointF | None = None, part=None) -> None:
        """A pressure update from either link, pushing out through `edge` of this PC, or only its
        third `part` for Part of the edge, or into the corner `part` names."""
        if self.failed or self._fx is None:
            return
        now = time.monotonic()
        started = self._start(now)
        cursor = QCursor.pos() if cursor is None else cursor
        at = self._relative(cursor.x(), cursor.y())
        push = self._push
        if push is None or push["edge"] != edge or push["part"] != part or push["crossed"]:
            rects = [crossing.Rect(s.geometry().x(), s.geometry().y(), s.geometry().width(), s.geometry().height())
                     for s in QGuiApplication.screens()]
            origin, pointer = (self._desk.x(), self._desk.y()), (cursor.x(), cursor.y())
            if part in crossing.CORNERS:
                region = corner_region(part, edge, rects, origin, pointer)
            else:
                region = edge_region(edge, rects, origin, pointer, part)
            push = self._push = {"edge": edge, "part": part, "region": region, "index": 0, "crossed": False}
        display, at = self._display(at)
        push.update(pressure=max(0.0, min(1.0, pressure)), at=at, updated=now, display=display)
        index = min(3, int(push["pressure"] * 4.0))
        tick = index if index > push["index"] else None
        push["index"] = index
        self.player.push(now, push["region"]["kind"], push["region"], at, 1.0 if crossed else push["pressure"], tick, display)
        if crossed:
            push["crossed"] = True
            self.player.cross(now)
        if started:
            self._tick()

    def arrive(self, method: str, edge: str, point: QPointF) -> None:
        """The pointer landed at `point` (logical desktop coordinates) on `edge` of this PC."""
        if self.failed or self._fx is None:
            return
        now = time.monotonic()
        started = self._start(now)
        display, at = self._display(self._relative(point.x(), point.y()))
        self.player.arrive(now, method, at, edge, display)
        if started:
            self._tick()

    def crossed_in(self) -> None:
        """A crossing landed here that EdgeGlow draws, not this window: a switch waiting to play
        gives way to it."""
        if not self.failed:
            self.player.crossed_in(time.monotonic())

    def switched(self, point: QPointF, choice: str = "match") -> None:
        """Input came to this PC without a crossing and the pointer is at `point` (logical desktop
        coordinates); `choice` is the Design page's shortcut_arrival_style."""
        if self.failed or self._fx is None:
            return
        try:
            fx = effects.switch_effect(choice, self._style)
        except Exception:
            self._fail("loading what a switch plays")
            return
        now = time.monotonic()
        self._start(now)
        display, at = self._display(self._relative(point.x(), point.y()))
        edge = effects.nearest_edge(at[0] - display[0], at[1] - display[1], display[2], display[3])
        self.player.switched(now, at, edge, fx, display)

    def stop(self) -> None:
        self._timer.stop()
        self.player = effects.Player()
        self._push = None
        self._pens = []
        self.hide()

    def _start(self, now: float) -> bool:
        """Starts the frame timer if it is not running, and says whether it did: the event that
        starts it draws the first frame itself rather than a timer interval later."""
        if self._timer.isActive():
            return False
        # Read once per effect rather than every frame.
        self._reduced = system_reduced_motion()
        self._dark = system_dark()
        self._desk = desktop_box()
        self._displays = [(g.x() - self._desk.x(), g.y() - self._desk.y(), g.width(), g.height())
                          for g in (screen.geometry() for screen in QGuiApplication.screens())]
        self._ticked_at = now
        self._timer.start()
        return True

    def _display(self, at):
        """The display `at` is on, as (x, y, w, h) relative to the desktop, and `at` kept on it."""
        return effects.display_for(at, self._displays or [(0.0, 0.0, float(self._desk.width()), float(self._desk.height()))])

    def _relative(self, x: float, y: float):
        return (min(max(0.0, x - self._desk.x()), self._desk.width() - 1.0),
                min(max(0.0, y - self._desk.y()), self._desk.height() - 1.0))

    def _drain(self, now: float) -> None:
        """Pressure only arrives while the pointer pushes, so once it stops the push drains on its
        own clock, as the model's does, and the Player is fed the falling value every frame."""
        push = self._push
        elapsed, self._ticked_at = now - self._ticked_at, now
        if push is None or push["crossed"]:
            return
        if now - push["updated"] > STALE_SECONDS:
            push["pressure"] = max(0.0, push["pressure"] - elapsed / crossing.DECAY_SECONDS)
            push["index"] = min(push["index"], int(push["pressure"] * 4.0))
            self.player.push(now, push["region"]["kind"], push["region"], push["at"], push["pressure"], display=push["display"])
            if push["pressure"] <= 0.0:
                self.player.release()
                self._push = None

    def _tick(self) -> None:
        now = time.monotonic()
        try:
            self._drain(now)
            pens = self.player.frames(now, self._fx, (float(self._desk.width()), float(self._desk.height()), "windows"),
                                      self._palette, self._reduced, self._dark, None)
        except Exception:
            self._fail("drawing")
            return
        box = union_bounds(pens, MARGIN)
        if box is None:
            self._pens = []
            if not self.player.busy:
                self._timer.stop()
            self.hide()
            return
        x0 = max(0, math.floor(box[0]))
        y0 = max(0, math.floor(box[1]))
        x1 = min(self._desk.width(), math.ceil(box[2]))
        y1 = min(self._desk.height(), math.ceil(box[3]))
        if x1 <= x0 or y1 <= y0:
            self._pens = []
            self.hide()
            return
        self._pens = pens
        self._offset = (float(x0), float(y0))
        frame = QRect(self._desk.x() + x0, self._desk.y() + y0, x1 - x0, y1 - y0)
        if frame != self.geometry():
            self.setGeometry(frame)
        if self.isHidden():
            self.show()
        edge_glow.keep_on_top(self)
        self.update()

    def paintEvent(self, _event) -> None:
        if not self._pens:
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.translate(-self._offset[0], -self._offset[1])
            replay(painter, self._pens)
        except Exception:
            painter.end()
            self._fail("replaying")
            return
        painter.end()

    def _fail(self, doing: str) -> None:
        if not self.failed:
            LOGGER.exception("Crossing effect %s failed while %s; the new effects are off until Beamer restarts", self._style, doing)
        self.failed = True
        self.stop()
        if self._on_failure is not None:
            self._on_failure()
