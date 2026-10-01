"""The Crossing page's two pictures: the arrangement diagram at the top of Ways in, and the push
strip in Resistance. Geometry is pages_win's, pure and tested; this file only paints it."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

import motion
import pages_win
import theme
import tokens

OTHER = "Other machine"
SCREEN = (120.0, 75.0)
GAP = 14.0
PAD = 8.0
CAP_ROW = 34.0
LIT = 3.0
CORNER = 16.0


def _colour(name: str, alpha: float = 1.0) -> QColor:
    colour = QColor(theme.colour(name))
    colour.setAlphaF(max(0.0, min(1.0, alpha)))
    return colour


def _marks(edge, methods, parts, corner, shortcut) -> dict:
    """Each mark the diagram can draw, keyed (kind, detail), at 1 when the settings light it."""
    methods = set(methods)
    marks = {}
    for side in pages_win.SIDE_ANGLE:
        marks[("edge", side)] = 1.0 if "edge" in methods and side == edge else 0.0
        marks[("track", side)] = 1.0 if "part" in methods and side == edge else 0.0
        for part in pages_win.PART_SPANS:
            marks[("part", side, part)] = 1.0 if "part" in methods and side == edge and part in parts else 0.0
    for name in pages_win.CORNER_NAMES:
        marks[("corner", name)] = 1.0 if "corner" in methods and name == corner else 0.0
    marks[("cap",)] = 1.0 if shortcut else 0.0
    return marks


class ArrangementDiagram(QWidget):
    """This PC's screen with the other machine's beside it on the chosen side, and on this PC's screen,
    lit, whatever crosses; the shortcut as a key cap underneath."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAccessibleName("Arrangement")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._state = None
        self._angle = pages_win.SIDE_ANGLE["right"]
        self._marks = _marks("right", (), (), "", False)
        self._cap_text = ""
        self._other = OTHER
        self.setFixedHeight(self._height())

    def set_other_name(self, name: str) -> None:
        name = name or OTHER
        if name != self._other:
            self._other = name
            self.update()

    def sizeHint(self) -> QSize:
        return QSize(2 * round(SCREEN[0]) + round(GAP) + 2 * round(PAD), self._height())

    def minimumSizeHint(self) -> QSize:
        return QSize(2 * round(SCREEN[0]) + round(GAP), self._height())

    def _height(self) -> int:
        _pc, _mac, pair_h = pages_win.arrangement_rects(100.0, 0.0, self._angle, SCREEN, GAP)
        return round(PAD + pair_h + PAD + CAP_ROW * self._marks[("cap",)])

    def set_state(self, edge, methods, parts, corner, key_name, style) -> None:
        state = (edge, tuple(methods), tuple(parts), corner, key_name, style)
        if state == self._state:
            return
        first = self._state is None
        self._state = state
        shortcut = "shortcut" in methods
        if shortcut:
            self._cap_text = pages_win.trigger_phrase(key_name, style)
        self.setAccessibleDescription(pages_win.ways_summary(methods, edge, parts, corner, key_name, style))
        targets = _marks(edge, methods, parts, corner, shortcut)
        start_marks = dict(self._marks)
        start_angle = self._angle
        end_angle = pages_win.turn_to(start_angle, edge)
        if first:
            end_angle = pages_win.SIDE_ANGLE[edge]

        def apply(t):
            self._angle = start_angle + (end_angle - start_angle) * t
            self._marks = {key: start_marks[key] + (targets[key] - start_marks[key]) * t for key in targets}
            self.setFixedHeight(self._height())
            self.update()

        def settle():
            self._angle = end_angle % 360.0
            self.update()

        if first:
            apply(1.0)
            settle()
            return
        motion.animate(self, "arrangement", 0.0, 1.0, apply, settle)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pc, mac, pair_h = pages_win.arrangement_rects(self.width(), PAD, self._angle, SCREEN, GAP)
        radius = tokens.RADIUS["field"]
        self._screen(painter, QRectF(*mac), self._other, "panel", "edge", "ink_3", radius)
        self._screen(painter, QRectF(*pc), "This PC", "well", "edge", "ink_2", radius)
        for key, level in self._marks.items():
            if level <= 0.001:
                continue
            kind = key[0]
            if kind == "edge":
                self._segment(painter, pc, key[1], 0.0, 1.0, level, "signal", LIT)
            elif kind == "track":
                self._track(painter, pc, key[1], level)
            elif kind == "part":
                start, end = pages_win.PART_SPANS[key[2]]
                self._segment(painter, pc, key[1], start, end, level, "signal", LIT, gap=2.0)
            elif kind == "corner":
                self._corner(painter, pc, key[1], level)
        level = self._marks[("cap",)]
        if level > 0.001 and self._cap_text:
            self._cap(painter, PAD + pair_h + PAD, level)
        painter.end()

    def _screen(self, painter, rect: QRectF, name, fill, line, ink, radius) -> None:
        painter.setPen(QPen(_colour(line), 1.0))
        painter.setBrush(_colour(fill))
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        painter.setPen(_colour(ink))
        painter.setFont(theme.font(tokens.TYPE["small"], 600))
        name = painter.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, int(rect.width()) - 8)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, name)

    @staticmethod
    def _segment(painter, rect, side, start, end, level, colour, width, gap=0.0) -> None:
        # Grows from its middle as it comes in.
        middle = (start + end) / 2.0
        half = (end - start) / 2.0 * level
        x1, y1, x2, y2 = pages_win.side_segment(rect, side, middle - half, middle + half, inset=width / 2.0 + 1.0)
        if gap and level > 0:
            if x1 == x2:
                y1, y2 = y1 + gap, y2 - gap
            else:
                x1, x2 = x1 + gap, x2 - gap
        pen = QPen(_colour(colour, level), width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    def _track(self, painter, rect, side, level) -> None:
        """Part of the edge: the whole side as a dim track, with a divider between the thirds."""
        self._segment(painter, rect, side, 0.0, 1.0, level, "off", LIT)
        pen = QPen(_colour("edge", level), 1.0)
        painter.setPen(pen)
        for fraction in (1.0 / 3.0, 2.0 / 3.0):
            x1, y1, _x2, _y2 = pages_win.side_segment(rect, side, fraction, fraction, inset=LIT / 2.0 + 1.0)
            if side in ("left", "right"):
                painter.drawLine(QPointF(x1 - 5, y1), QPointF(x1 + 5, y1))
            else:
                painter.drawLine(QPointF(x1, y1 - 5), QPointF(x1, y1 + 5))

    @staticmethod
    def _corner(painter, rect, corner, level) -> None:
        size = CORNER * (0.4 + 0.6 * level)
        x, y, w, h = pages_win.corner_box(rect, corner, size)
        box = QRectF(x, y, w, h).adjusted(2, 2, -2, -2)
        painter.setPen(QPen(_colour("signal", level), 1.5))
        painter.setBrush(_colour("signal", 0.35 * level))
        painter.drawRoundedRect(box, 2, 2)

    def _cap(self, painter, top, level) -> None:
        key, _sep, how = self._cap_text.partition(", ")
        face = theme.mono_font(tokens.TYPE["small"], 700)
        painter.setFont(face)
        metrics = painter.fontMetrics()
        key_w = metrics.horizontalAdvance(key) + 18
        rest = f", {how}"
        body = theme.font(tokens.TYPE["note"])
        painter.setFont(body)
        rest_w = painter.fontMetrics().horizontalAdvance(rest)
        total = key_w + 2 + rest_w
        left = (self.width() - total) / 2.0
        cap = QRectF(left, top + 2 + (1.0 - level) * 6.0, key_w, 24)
        painter.setOpacity(level)
        painter.setPen(QPen(_colour("edge"), 1.0))
        painter.setBrush(_colour("ground"))
        painter.drawRoundedRect(cap.adjusted(0.5, 0.5, -0.5, -0.5), tokens.RADIUS["keycap"], tokens.RADIUS["keycap"])
        painter.setPen(QPen(_colour("edge"), 2.0))
        painter.drawLine(QPointF(cap.left() + 3, cap.bottom() - 1), QPointF(cap.right() - 3, cap.bottom() - 1))
        painter.setPen(_colour("signal"))
        painter.setFont(face)
        painter.drawText(cap.adjusted(0, 0, 0, -2), Qt.AlignmentFlag.AlignCenter, key)
        painter.setPen(_colour("ink_2"))
        painter.setFont(body)
        painter.drawText(QRectF(cap.right() + 2, cap.top(), rest_w + 4, cap.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, rest)
        painter.setOpacity(1.0)


class PushStrip(QWidget):
    """A band of this screen's edge: the edge line, the other machine beyond it, and a `signal` fill from the
    edge as deep as the resistance, with the pointer at its end. A change slides the fill and the
    pointer to the new depth, lit bright, and the light then settles."""

    HEIGHT = 44
    MAC = 64.0
    SETTLE_MS = 700

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._other = OTHER
        self._value = None
        self._shown = 0.0
        self._glow = 0.0
        self._mac_left = False

    def minimumSizeHint(self) -> QSize:
        return QSize(160, self.HEIGHT)

    def set_other_name(self, name: str) -> None:
        name = name or OTHER
        if name != self._other:
            self._other = name
            self.update()

    def set_edge(self, edge: str) -> None:
        mac_left = edge == "left"
        if mac_left != self._mac_left:
            self._mac_left = mac_left
            self.update()

    def set_value(self, value: int) -> None:
        value = float(value)
        if value == self._value:
            return
        first = self._value is None
        self._value = value
        self.setAccessibleDescription(f"{int(value)} px")
        if first:
            self._shown = value
            self.update()
            return
        start = self._shown

        def apply(depth):
            self._shown = depth
            self.update()

        motion.animate(self, "depth", start, value, apply)
        if motion.should_animate(self):
            def glow(level):
                self._glow = level
                self.update()

            motion.animate(self, "glow", 1.0, 0.0, glow, duration=self.SETTLE_MS)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Drawn with the other machine on the right; flipped when it is on the left.
        if self._mac_left:
            painter.translate(self.width(), 0)
            painter.scale(-1, 1)
        band = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        radius = tokens.RADIUS["field"]
        edge_x = band.right() - self.MAC
        painter.setPen(QPen(_colour("rule"), 1.0))
        painter.setBrush(_colour("well"))
        painter.drawRoundedRect(band, radius, radius)
        mac = QRectF(edge_x, band.top(), self.MAC, band.height())
        path = QPainterPath()
        path.addRoundedRect(mac, radius, radius)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_colour("panel"))
        painter.drawPath(path)
        track = edge_x - band.left() - 14.0
        depth = pages_win.push_depth(self._shown or 0.0, track)
        if depth > 0.5:
            fill = QRectF(edge_x - depth, band.top() + 8, depth, band.height() - 16)
            painter.setBrush(_colour("signal", 0.28 + 0.42 * self._glow))
            painter.drawRect(fill)
        painter.setPen(QPen(_colour("signal" if depth > 0.5 or self._glow else "edge"), 2.0))
        painter.drawLine(QPointF(edge_x, band.top() + 4), QPointF(edge_x, band.bottom() - 4))
        tip = QPointF(edge_x - depth - (2.0 if depth > 0.5 else 1.0), band.center().y())
        self._pointer(painter, tip)
        painter.setPen(_colour("ink_3"))
        painter.setFont(theme.font(tokens.TYPE["small"], 600))
        label = QRectF(edge_x, band.top(), self.MAC, band.height())
        if self._mac_left:
            # Text drawn in the flipped frame would read backwards.
            painter.resetTransform()
            label = QRectF(self.width() - label.right(), label.top(), label.width(), label.height())
        painter.drawText(
            label, Qt.AlignmentFlag.AlignCenter,
            painter.fontMetrics().elidedText(self._other, Qt.TextElideMode.ElideRight, int(label.width()) - 4),
        )
        painter.end()

    def _pointer(self, painter, tip: QPointF) -> None:
        # An arrow pointing at the edge, its tip where the push has reached.
        path = QPainterPath()
        k = 0.9
        points = ((0, 0), (-14, -8), (-11, 0), (-14, 8))
        for index, (x, y) in enumerate(points):
            (path.lineTo if index else path.moveTo)(tip.x() + x * k, tip.y() + y * k)
        path.closeSubpath()
        painter.setPen(QPen(_colour("ground"), 1.0))
        painter.setBrush(_colour("ink"))
        painter.drawPath(path)
