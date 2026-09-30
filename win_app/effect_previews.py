"""The Design page's pictures of the crossing styles: a still for each tile at the place the page's
Show at chooses, played on from while the pointer is over it, drawn from effects.preview_scene, the
same loop the Mac's Design page plays, replayed by the overlay's own `replay`."""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

import edge_glow
from core import effects
import motion
import theme
import tokens
from effect_overlay import replay, system_reduced_motion

LOGGER = logging.getLogger(__name__)

# Part way into the push, where every style shows.
STILL_AT_S = 1.7
# One effect layer, reused while the preview's size holds, rather than a new image every frame.
_LAYERS = {}
STILL_HEIGHT = 56
# A tile shows where the style plays rather than the whole scene too small to see: this many scene
# points across, centred on the crossing at each place, the corner framed with room above the screens.
STILL_VIEW_W = 480.0
STILL_CENTRE = {"edge": (effects.PREVIEW_SCREEN[0] + effects.PREVIEW_GAP / 2.0, effects.PREVIEW_SCREEN[1] * 0.45),
                "corner": (effects.PREVIEW_SCREEN[0] + effects.PREVIEW_GAP / 2.0, 40.0)}
# A switch plays round a still pointer on one screen: its tile shows that part of the scene part-way
# through, and the stage the screen less the empty strips above and below, as the Mac's do.
SWITCH_STILL_AT_S = 0.6
SWITCH_STILL_VIEW = QRectF(248.0, 138.0, 240.0, 120.0)
_SCREEN_NAMES = {"mac": "MAC", "windows": "PC"}
FRAME_MS = 16
# A pointer passing over tiles on its way elsewhere leaves the tiles it crosses still.
HOVER_INTENT_S = 0.1


def frame_timer(parent: QObject, tick) -> QTimer:
    """A timer for one frame of animation. Precise: a coarse timer may fire up to 5% either side,
    which on Windows' clock lands frames unevenly."""
    timer = QTimer(parent)
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.setInterval(FRAME_MS)
    timer.timeout.connect(tick)
    return timer


def on_screen(widget: QWidget) -> bool:
    """Whether any of `widget` can be seen: shown in a window that is not minimised, and not
    scrolled out of its page."""
    return widget.isVisible() and not widget.window().isMinimized() and not widget.visibleRegion().isEmpty()


def _pointer_path(size: float) -> QPainterPath:
    """An arrow pointer `size` tall with its tip at the origin."""
    k = size / 16.0
    path = QPainterPath()
    for index, (x, y) in enumerate(((0, 0), (0, 14), (3.6, 10.8), (6.2, 16), (8.4, 15), (5.9, 9.9), (10.4, 9.9))):
        (path.lineTo if index else path.moveTo)(x * k, y * k)
    path.closeSubpath()
    return path


def paint_scene(painter: QPainter, rect: QRectF, view: QRectF, scene: dict, pointer_size: float, labels: bool) -> None:
    """Draws the `view` part of a preview_scene (in scene points) into `rect`, scaled to fit and
    centred: the screens, then the pens, then the pointer, then the notch, the order the scene
    asks for."""
    scale = min(rect.width() / view.width(), rect.height() / view.height())
    width, height = view.width(), view.height()
    left = rect.x() + (rect.width() - width * scale) / 2.0
    top = rect.y() + (rect.height() - height * scale) / 2.0
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setClipRect(QRectF(left, top, width * scale, height * scale))
    painter.translate(left, top)
    painter.scale(scale, scale)
    painter.translate(-view.x(), -view.y())
    radius = tokens.RADIUS["field"] / scale
    for x, y, w, h, os_name in scene["screens"]:
        painter.setPen(QPen(QColor(theme.colour("rule")), 1.0 / scale))
        painter.setBrush(QColor(theme.colour("well")))
        painter.drawRoundedRect(QRectF(x, y, w, h), radius, radius)
        if labels:
            painter.setPen(QColor(theme.colour("ink_3")))
            painter.setFont(theme.eyebrow_font())
            # Bottom left of the part of the screen in view, which no crossing in the loop comes near.
            shown = QRectF(x, y, w, h).intersected(view)
            painter.save()
            painter.translate(shown.x(), shown.y())
            painter.scale(1.0 / scale, 1.0 / scale)
            corner = QRectF(10, shown.height() * scale - 26, shown.width() * scale - 20, 18)
            painter.drawText(corner, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft, _SCREEN_NAMES.get(os_name, ""))
            painter.restore()
    # The effects draw on their own layer, as the overlay does over the desktop, so "lighter"
    # adds to the effect's own light rather than to the screens drawn under it.
    if not scene["pens"]:
        _paint_pointer_and_notch(painter, scene, scale, pointer_size)
        painter.restore()
        return
    ratio = painter.device().devicePixelRatioF() if painter.device() is not None else 1.0
    size = (int(width * scale * ratio) + 1, int(height * scale * ratio) + 1, ratio)
    layer = _LAYERS.get(size)
    if layer is None:
        _LAYERS.clear()
        layer = _LAYERS[size] = QImage(size[0], size[1], QImage.Format.Format_ARGB32_Premultiplied)
        layer.setDevicePixelRatio(ratio)
    layer.fill(Qt.GlobalColor.transparent)
    layer_painter = QPainter(layer)
    layer_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    layer_painter.scale(scale, scale)
    layer_painter.translate(-view.x(), -view.y())
    try:
        replay(layer_painter, scene["pens"])
    finally:
        layer_painter.end()
    painter.save()
    painter.resetTransform()
    painter.translate(left, top)
    painter.drawImage(0, 0, layer)
    painter.restore()
    _paint_pointer_and_notch(painter, scene, scale, pointer_size)
    painter.restore()


def _paint_pointer_and_notch(painter: QPainter, scene: dict, scale: float, pointer_size: float) -> None:
    if scene["pointer"] is not None:
        px, py, _os = scene["pointer"]
        painter.save()
        painter.translate(px, py)
        painter.scale(1.0 / scale, 1.0 / scale)
        arrow = _pointer_path(pointer_size)
        painter.setPen(QPen(QColor("#000000"), 1.0))
        painter.setBrush(QColor("#ffffff"))
        painter.drawPath(arrow)
        painter.restore()
    if scene["notch"] is not None:
        nx, ny, nw, nh, nr = scene["notch"]
        notch = QPainterPath()
        notch.addRoundedRect(QRectF(nx, ny - nr, nw, nh + nr), nr, nr)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#000000"))
        painter.drawPath(notch)


class EffectStill(QWidget):
    """A tile's picture: its effect part-way through a push, in the chosen colour. While TileHover
    has it playing it draws each frame on from that still instead of its cached image."""

    STILL_AT = STILL_AT_S

    def __init__(self, effect_id: str, palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.effect_id = effect_id
        self.palette = tuple(palette)
        self.place = "edge"
        self.pace = 1.0
        self._image = None
        self.playing_since = None
        self.setFixedHeight(STILL_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_palette(self, palette) -> None:
        if tuple(palette) != self.palette:
            self.palette = tuple(palette)
            self._image = None
            self.update()

    def set_place(self, place: str) -> None:
        if place != self.place:
            self.place = place
            self._image = None
            self.update()

    def set_pace(self, pace: float) -> None:
        if pace != self.pace:
            self.pace = pace
            self._image = None
            self.update()

    def refresh_colours(self) -> None:
        # A cached image, not a live paint: without this an appearance switch leaves it showing
        # the old palette until something else invalidates the cache.
        self._image = None

    def paintEvent(self, _event) -> None:
        if self.playing_since is not None:
            painter = QPainter(self)
            try:
                self._paint_still(painter, self.STILL_AT + max(0.0, time.monotonic() - self.playing_since))
            except Exception:
                LOGGER.exception("Could not draw crossing effect %s", self.effect_id)
                self.playing_since = None
            painter.end()
            return
        ratio = self.devicePixelRatioF()
        size = self.size() * ratio
        if self._image is None or self._image.size() != size:
            self._image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
            self._image.setDevicePixelRatio(ratio)
            self._image.fill(Qt.GlobalColor.transparent)
            image_painter = QPainter(self._image)
            try:
                self._paint_still(image_painter, self.STILL_AT)
            except Exception:
                LOGGER.exception("Could not draw the still of crossing effect %s", self.effect_id)
            finally:
                image_painter.end()
        painter = QPainter(self)
        painter.drawImage(0, 0, self._image)
        painter.end()

    def _paint_still(self, painter: QPainter, t: float) -> None:
        scene = effects.preview_scene(effects.preview_effect(self.effect_id), t, self.place, self.palette,
                                      system_reduced_motion(), theme.is_dark(), self.pace)
        width = STILL_VIEW_W
        view_h = min(effects.PREVIEW_SCREEN[1], width * self.height() / max(1, self.width()))
        centre = STILL_CENTRE[self.place]
        view = QRectF(centre[0] - width / 2.0, centre[1] - view_h / 2.0, width, view_h)
        paint_scene(painter, QRectF(self.rect()), view, scene, 10.0, labels=False)


class SwitchStill(EffectStill):
    """A Switch tile's picture: what a switch plays for `choice` beside the crossing style, round a
    still pointer, part-way through."""

    STILL_AT = SWITCH_STILL_AT_S

    def __init__(self, choice: str, crossing_style: str, palette, parent: QWidget | None = None) -> None:
        super().__init__(choice, palette, parent)
        self.crossing_style = crossing_style

    def set_crossing_style(self, style: str) -> None:
        if style != self.crossing_style:
            self.crossing_style = style
            self._image = None
            self.update()

    def _paint_still(self, painter: QPainter, t: float) -> None:
        fx = effects.switch_effect(self.effect_id, self.crossing_style)
        scene = effects.preview_switch_scene(fx, t, self.palette, "windows", system_reduced_motion(), theme.is_dark(),
                                             self.pace)
        paint_scene(painter, QRectF(self.rect()), SWITCH_STILL_VIEW, scene, 10.0, labels=False)


class TileHover(QObject):
    """The one tile under the pointer plays its animation and settles back to its still when the
    pointer leaves; under Reduce Motion it keeps its still. An effect's still plays on from the
    frame it shows, and Glow and Beam from their held push, so a tile starts without a jump."""

    def __init__(self, loop: edge_glow.PreviewLoop, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.loop = loop
        self._pictures: dict = {}
        self.current = None
        self._timer = frame_timer(self, self._tick)

    def track(self, tile: QWidget, picture: QWidget) -> None:
        self._pictures[tile] = picture
        tile.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Enter and watched in self._pictures:
            self.enter(self._pictures[watched])
        elif kind == QEvent.Type.Leave and watched in self._pictures and self._pictures[watched] is self.current:
            self.stop()
        return False

    def enter(self, picture: QWidget) -> None:
        if picture is self.current:
            return
        self.stop()
        self.current = picture
        if system_reduced_motion():
            return
        if isinstance(picture, edge_glow.GlowPreview):
            self.loop.hover(picture)
            return
        picture.playing_since = time.monotonic() + HOVER_INTENT_S
        self._timer.start()

    def stop(self) -> None:
        picture, self.current = self.current, None
        self._timer.stop()
        if picture is None:
            return
        played = (self.loop.hovered is picture and self.loop.playing(picture)) or (
            getattr(picture, "playing_since", None) is not None and time.monotonic() > picture.playing_since
        )
        shot = motion.snapshot(picture) if played else None
        if isinstance(picture, edge_glow.GlowPreview):
            self.loop.hover(None)
        else:
            picture.playing_since = None
            picture.update()
        motion.fade_from(picture, shot)

    def _tick(self) -> None:
        if self.current is not None and on_screen(self.current):
            self.current.update()
