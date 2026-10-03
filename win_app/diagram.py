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
NOTE_ROW = 22.0
LIT = 3.0
CORNER = 16.0
# The marks of the machines the page is not showing: there, but not the ones being edited.
QUIET = "ink_3"
QUIET_ALPHA = 0.55


def _colour(name: str, alpha: float = 1.0) -> QColor:
    colour = QColor(theme.colour(name))
    colour.setAlphaF(max(0.0, min(1.0, alpha)))
    return colour


def _lit(lead: float, level: float) -> QColor:
    """A mark on this PC's screen: the signal colour for the machine shown, quieter for the rest."""
    lead = max(0.0, min(1.0, lead))
    return _blend(QUIET, "signal", lead, level * (QUIET_ALPHA + (1.0 - QUIET_ALPHA) * lead))


def _blend(quiet: str, strong: str, level: float, alpha: float = 1.0) -> QColor:
    """`quiet` at 0, `strong` at 1, so a machine chosen or left eases between the two."""
    one, two = QColor(theme.colour(quiet)), QColor(theme.colour(strong))
    level = max(0.0, min(1.0, level))
    mixed = QColor.fromRgbF(*(a + (b - a) * level for a, b in zip(one.getRgbF()[:3], two.getRgbF()[:3])))
    mixed.setAlphaF(max(0.0, min(1.0, alpha)))
    return mixed


def _drawn_side(machine: dict, alone: bool) -> str:
    # One machine with no side is drawn on the right, as the one-machine page always drew it.
    return machine.get("side") or ("right" if alone else "")


def _marks(machines, shortcut) -> dict:
    """Each mark the diagram can draw, keyed (kind, machine, detail), at 1 when the settings light it;
    ("lead", machine) at 1 lights its marks in the signal colour, ("named", machine) its name in ink."""
    alone = len(machines) == 1
    marks = {("cap",): 1.0 if shortcut else 0.0}
    for machine in machines:
        key, methods = machine["key"], set(machine["methods"])
        edge = _drawn_side(machine, alone) if machine.get("side") else ""
        for side in pages_win.SIDE_ANGLE:
            marks[("edge", key, side)] = 1.0 if "edge" in methods and side == edge else 0.0
            marks[("track", key, side)] = 1.0 if "part" in methods and side == edge else 0.0
            for part in pages_win.PART_SPANS:
                lit = "part" in methods and side == edge and part in machine["parts"]
                marks[("part", key, side, part)] = 1.0 if lit else 0.0
        for name in pages_win.CORNER_NAMES:
            marks[("corner", key, name)] = 1.0 if "corner" in methods and name == machine["corner"] else 0.0
        marks[("lead", key)] = 1.0 if machine["chosen"] or alone else 0.0
        marks[("named", key)] = 1.0 if machine["chosen"] and not alone else 0.0
    return marks


class ArrangementDiagram(QWidget):
    """This PC's screen in the middle and each paired machine's beside the side it is on, two on one
    side sharing it; on this PC's screen, lit, whatever crosses to each, the chosen machine's in the
    signal colour and the rest quieter; the machines with no side named underneath; the shortcut as a
    key cap below that."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAccessibleName("Arrangement")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._state = None
        self._machines: list = []
        # Where each machine's screen is drawn now, by key: [angle, start, end, shown], moving between states.
        self._places: dict = {}
        self._labels: dict = {}
        self._marks = _marks([], False)
        self._unplaced = ""
        self._cap_text = ""
        self.setFixedHeight(self._height())

    def sizeHint(self) -> QSize:
        return QSize(2 * round(SCREEN[0]) + round(GAP) + 2 * round(PAD), self._height())

    def minimumSizeHint(self) -> QSize:
        return QSize(2 * round(SCREEN[0]) + round(GAP), self._height())

    def _drawing(self, width: float):
        keys = [key for key, place in self._places.items() if place[3] > 0.001]
        placed = [tuple(self._places[key][:3]) for key in keys]
        available = max(1.0, width - 2 * PAD)
        centre_pc = len(self._machines) > 1
        pc, rects, _height = pages_win.layout_rects(available, PAD, placed, SCREEN, GAP,
                                                    centre_pc=centre_pc)
        bounds_left = min([pc[0], *(rect[0] for rect in rects)])
        bounds_right = max([pc[0] + pc[2], *(rect[0] + rect[2] for rect in rects)])
        scale = max(1.0, available * 0.68 / max(1.0, bounds_right - bounds_left))
        screen = (SCREEN[0] * scale, SCREEN[1] * scale)
        gap = GAP * scale
        pc, rects, height = pages_win.layout_rects(available, PAD, placed, screen, gap,
                                                   centre_pc=centre_pc)
        return (pc[0] + PAD, pc[1], pc[2], pc[3]), {key: (rect[0] + PAD, *rect[1:]) for key, rect in zip(keys, rects)}, height

    def _height(self) -> int:
        _pc, _rects, height = self._drawing(float(self.width() or 2 * SCREEN[0] + GAP + 2 * PAD))
        return round(PAD + height + PAD + (NOTE_ROW if self._unplaced else 0.0) + CAP_ROW * self._marks[("cap",)])

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # A narrow page shrinks the drawing, and the widget's height follows it.
        height = self._height()
        if height != self.height():
            self.setFixedHeight(height)

    def set_machines(self, machines, key_name, style, shortcut) -> None:
        """Each machine a dict: `key`, `label` ("" for the placeholder of an unpaired PC), `side` (""
        when not placed), `methods`, `parts`, `corner` and `chosen`."""
        machines = [dict(machine, methods=list(machine["methods"]), parts=list(machine["parts"])) for machine in machines]
        state = (repr(machines), key_name, style, shortcut)
        if state == self._state:
            return
        first = self._state is None
        self._state = state
        self._machines = machines
        alone = len(machines) == 1
        self._labels = {machine["key"]: machine["label"] or OTHER for machine in machines}
        if shortcut:
            self._cap_text = pages_win.trigger_phrase(key_name, style)
        self._unplaced = "" if alone else pages_win.unplaced_line(
            [machine["label"] for machine in machines if not machine.get("side")])
        self.setAccessibleDescription(pages_win.diagram_description(
            [dict(machine, label=self._labels[machine["key"]]) for machine in machines],
            key_name, style, shortcut))
        targets = _marks(machines, shortcut)
        slots = pages_win.side_slots([dict(machine, side=_drawn_side(machine, alone)) for machine in machines])
        ends = {}
        for machine, slot in zip(machines, slots):
            held = self._places.get(machine["key"])
            if slot is None:
                ends[machine["key"]] = list(held[:3]) + [0.0] if held else [0.0, 0.0, 1.0, 0.0]
                continue
            side, start, end = slot
            # A machine already drawn goes round to its new side; one just placed fades in there.
            drawn = held is not None and held[3] > 0.0 and not first
            angle = pages_win.turn_to(held[0], side) if drawn else pages_win.SIDE_ANGLE[side]
            ends[machine["key"]] = [angle, start, end, 1.0]
        for key, held in self._places.items():
            # A machine removed or no longer placed fades where it was.
            ends.setdefault(key, list(held[:3]) + [0.0])
        starts = {}
        for key, end in ends.items():
            held = self._places.get(key)
            starts[key] = list(held) if held is not None and held[3] > 0.0 else end[:3] + [0.0]
        start_marks = dict(self._marks)

        def apply(t):
            self._places = {key: [a + (b - a) * t for a, b in zip(starts[key], end)] for key, end in ends.items()}
            self._marks = {key: start_marks.get(key, 0.0) + (targets.get(key, 0.0) - start_marks.get(key, 0.0)) * t
                           for key in set(targets) | set(start_marks)}
            self.setFixedHeight(self._height())
            self.update()

        def settle():
            self._places = {key: [end[0] % 360.0, end[1], end[2], end[3]] for key, end in ends.items() if end[3] > 0.0
                            or key in self._labels}
            self._marks = {key: level for key, level in targets.items()}
            self.setFixedHeight(self._height())
            self.update()

        if first:
            apply(1.0)
            settle()
            return
        motion.animate(self, "arrangement", 0.0, 1.0, apply, settle)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pc, rects, height = self._drawing(float(self.width()))
        radius = tokens.RADIUS["field"]
        for key, rect in rects.items():
            painter.setOpacity(self._places[key][3])
            named = self._marks.get(("named", key), 0.0)
            self._screen(painter, QRectF(*rect), self._labels.get(key, OTHER), "panel", _blend("edge", "ink_3", named),
                         _blend("ink_3", "ink", named), radius)
        painter.setOpacity(1.0)
        self._screen(painter, QRectF(*pc), "This PC", "well", _colour("edge"), _colour("ink_2"), radius)
        # The machines not shown first, so the chosen one's marks lie over theirs where they meet.
        for key, level in sorted(self._marks.items(), key=lambda item: self._marks.get(("lead", item[0][1]), 0.0)
                                 if len(item[0]) > 1 else 0.0):
            if level <= 0.001 or key[0] in ("cap", "lead", "named"):
                continue
            kind, lead = key[0], self._marks.get(("lead", key[1]), 0.0)
            if kind == "edge":
                self._segment(painter, pc, key[2], 0.0, 1.0, level, lead, LIT)
            elif kind == "track":
                self._track(painter, pc, key[2], level)
            elif kind == "part":
                start, end = pages_win.PART_SPANS[key[3]]
                self._segment(painter, pc, key[2], start, end, level, lead, LIT, gap=2.0)
            elif kind == "corner":
                self._corner(painter, pc, key[2], level, lead)
        top = PAD + height + PAD
        if self._unplaced:
            painter.setPen(_colour("ink_3"))
            painter.setFont(theme.font(tokens.TYPE["note"]))
            line = QRectF(PAD, top, self.width() - 2 * PAD, NOTE_ROW)
            painter.drawText(line, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                             painter.fontMetrics().elidedText(self._unplaced, Qt.TextElideMode.ElideRight, int(line.width())))
            top += NOTE_ROW
        level = self._marks[("cap",)]
        if level > 0.001 and self._cap_text:
            self._cap(painter, top, level)
        painter.end()

    def _screen(self, painter, rect: QRectF, name, fill, line: QColor, ink: QColor, radius) -> None:
        painter.setPen(QPen(line, 1.0))
        painter.setBrush(_colour(fill))
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        painter.setPen(ink)
        painter.setFont(theme.font(tokens.TYPE["small"], 600))
        metrics = painter.fontMetrics()
        room = rect.adjusted(4, 2, -4, -2)
        wrapped = Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap
        needed = metrics.boundingRect(room.toRect(), wrapped, name)
        if metrics.horizontalAdvance(name) > room.width() and needed.width() <= room.width() and needed.height() <= room.height():
            # A screen sharing its side is half as wide: a two-word name goes onto two lines there.
            painter.drawText(room, wrapped, name)
            return
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                         metrics.elidedText(name, Qt.TextElideMode.ElideRight, int(rect.width()) - 8))

    @staticmethod
    def _segment(painter, rect, side, start, end, level, lead, width, gap=0.0) -> None:
        # Grows from its middle as it comes in.
        middle = (start + end) / 2.0
        half = (end - start) / 2.0 * level
        x1, y1, x2, y2 = pages_win.side_segment(rect, side, middle - half, middle + half, inset=width / 2.0 + 1.0)
        if gap and level > 0:
            if x1 == x2:
                y1, y2 = y1 + gap, y2 - gap
            else:
                x1, x2 = x1 + gap, x2 - gap
        pen = QPen(_lit(lead, level), width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    def _track(self, painter, rect, side, level) -> None:
        """Part of the edge: the whole side as a dim track, with a divider between the thirds."""
        pen = QPen(_colour("off", level), LIT)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        x1, y1, x2, y2 = pages_win.side_segment(rect, side, 0.5 - 0.5 * level, 0.5 + 0.5 * level, inset=LIT / 2.0 + 1.0)
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        pen = QPen(_colour("edge", level), 1.0)
        painter.setPen(pen)
        for fraction in (1.0 / 3.0, 2.0 / 3.0):
            x1, y1, _x2, _y2 = pages_win.side_segment(rect, side, fraction, fraction, inset=LIT / 2.0 + 1.0)
            if side in ("left", "right"):
                painter.drawLine(QPointF(x1 - 5, y1), QPointF(x1 + 5, y1))
            else:
                painter.drawLine(QPointF(x1, y1 - 5), QPointF(x1, y1 + 5))

    @staticmethod
    def _corner(painter, rect, corner, level, lead) -> None:
        size = CORNER * (0.4 + 0.6 * level)
        x, y, w, h = pages_win.corner_box(rect, corner, size)
        box = QRectF(x, y, w, h).adjusted(2, 2, -2, -2)
        painter.setPen(QPen(_lit(lead, level), 1.5))
        painter.setBrush(_lit(lead, 0.35 * level))
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
