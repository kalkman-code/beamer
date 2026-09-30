"""The Windows half of the edge glow, along the return edge, on the monitor that owns it. Click-through,
never focused, above other windows.

Two styles, chosen in this app's own settings with a colour: `glow` is a band whose depth and opacity
track pressure with a flash at breakthrough; `beam` is a thin line along the edge with a comet of
light travelling it, faster as the push builds, the whole edge flashing at breakthrough and the comet
running on off the end rather than stopping where it was, after the Mac's notch beam.

Every frame paints the palette along the edge, then cuts it with alpha masks: a falloff inward from
the edge, and for the beam a profile along it. `signal` with `glow` is the original look.
"""

from __future__ import annotations

import copy
import ctypes
import time

from PySide6.QtCore import QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QSizePolicy, QWidget

import app_config
from core import effects
from core import return_edge as crossing
import theme
import tokens

BAND_PX = 44
# How far a corner's light reaches along each of its two walls.
CORNER_ARM_PX = 200
# Over how much of its length a third of the edge fades in and out at each end, rather than stopping
# dead where the third does.
TAPER_PX = 72
FLASH_SECONDS = tokens.SPRING["flash_seconds"]
STALE_SECONDS = 0.08

COMET = 0.3
BEAM_BASE = 0.2
BEAM_FALLOFF = ((0.0, 1.0), (0.08, 0.85), (0.3, 0.25), (1.0, 0.0))
SLOW_TRAVERSE_S = 2.4
FAST_TRAVERSE_S = 0.8
FINISH_SECONDS = 0.6
FINISH_TRAVERSE_S = 0.35
PROFILE_SAMPLES = 24

# The Design page's scripted push: pressure builds for PUSH_S and goes through, the breakthrough
# plays out, then everything rests before the next one. The Mac's previews run the same script.
PUSH_S = 1.8
AFTER_S = FINISH_SECONDS + 0.4
REST_S = 1.0
PREVIEW_HEIGHT = 96


def traverse_seconds(strength: float) -> float:
    """One run of the comet along the whole edge: unhurried at a light lean, quick near breakthrough."""
    strength = max(0.0, min(1.0, strength))
    return SLOW_TRAVERSE_S + (FAST_TRAVERSE_S - SLOW_TRAVERSE_S) * strength


def comet_alpha(position: float, centre: float, flash: float) -> float:
    """How lit the edge is at `position` (0 to 1 along it) with the comet's head at `centre`: a dim
    base everywhere, raised to `flash` at breakthrough, and a peak that falls away over half the
    comet's length either side."""
    base = max(BEAM_BASE, min(1.0, flash))
    reach = max(0.0, 1.0 - abs(position - centre) / (COMET / 2.0))
    return base + (1.0 - base) * reach * reach


def preview_frame(elapsed: float):
    """(pressure, phase) `elapsed` seconds into the preview's loop; phases are push, after and
    rest."""
    t = elapsed % (PUSH_S + AFTER_S + REST_S)
    if t < PUSH_S:
        return (t / PUSH_S) ** 1.6, "push"
    return 0.0, "after" if t < PUSH_S + AFTER_S else "rest"


class GlowState:
    """What the glow shows at one moment -- the pressure, the breakthrough flash, the beam's run-off
    and where the comet is -- and how it moves on between frames. The screen's glow and the Design
    page's previews both run one, so a preview cannot drift from the real thing."""

    def __init__(self) -> None:
        self.pressure = 0.0
        self.flash = 0.0
        self.finish = 0.0
        self.centre = -COMET / 2.0
        # The Design page's Length: how long the flash and the beam's run-off take, as a multiple.
        self.pace = 1.0

    def restart(self) -> None:
        self.centre = -COMET / 2.0

    def push(self, pressure: float, crossed: bool) -> None:
        self.pressure = 0.0 if crossed else max(0.0, min(1.0, pressure))
        if crossed:
            self.flash = 1.0
            self.finish = 1.0

    def step(self, elapsed: float, stale: bool) -> bool:
        """Moves on by `elapsed` seconds and answers whether anything is still lit. `stale` is
        whether the push has stopped arriving, which is when the pressure fades on its own clock."""
        if stale:
            self.pressure = max(0.0, self.pressure - elapsed / crossing.DECAY_SECONDS)
        self.flash = max(0.0, self.flash - elapsed / (FLASH_SECONDS * self.pace))
        if self.finish > 0.0:
            # Run on off the end of the edge instead of stopping, and never wrap back to the start.
            self.centre = min(1.0 + COMET, self.centre + elapsed / (FINISH_TRAVERSE_S * self.pace))
            self.finish = max(0.0, self.finish - elapsed / (FINISH_SECONDS * self.pace))
        else:
            self.centre += elapsed / traverse_seconds(self.pressure)
            if self.centre > 1.0 + COMET / 2.0:
                self.centre = -COMET / 2.0
        return self.pressure > 0.0 or self.flash > 0.0 or self.finish > 0.0


def system_dark() -> bool:
    """Apps in dark mode. The overlay cannot see the wallpaper, so this is the best it has."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            # The apps' mode, not the taskbar's (SystemUsesLightTheme), which Windows lets differ:
            # windows are what the effects draw over, and the Mac reads its apps' appearance too.
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except (ImportError, OSError):
        return True


def part_of_monitor(owner, edge: str, part) -> "crossing.Rect":
    """`owner` cut down along `edge` to its own third `part`, the thirds PartEdge crosses in
    (return_edge.display_fraction); all of `owner` for no part."""
    if part not in crossing.PARTS:
        return owner
    index = crossing.PARTS.index(part)
    if edge in ("left", "right"):
        low, high = owner.y + owner.height * index // 3, owner.y + owner.height * (index + 1) // 3
        return crossing.Rect(owner.x, low, owner.width, high - low)
    low, high = owner.x + owner.width * index // 3, owner.x + owner.width * (index + 1) // 3
    return crossing.Rect(low, owner.y, high - low, owner.height)


def corner_of_monitor(owner, corner: str, arm: int = CORNER_ARM_PX) -> "crossing.Rect":
    """The square at `corner` of `owner` that a corner's light fills, `arm` a side."""
    vertical, horizontal = corner.split("_")
    x = owner.x if horizontal == "left" else owner.x + owner.width - arm
    y = owner.y if vertical == "top" else owner.y + owner.height - arm
    return crossing.Rect(x, y, arm, arm)


HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_NOOWNERZORDER = 0x0001, 0x0002, 0x0010, 0x0200


def keep_on_top(widget: QWidget) -> None:
    """Puts `widget`'s window back at the top of the topmost windows. The taskbar is topmost too and
    raises itself whenever it is hovered or clicked, and a push into a corner or along its edge is
    right over it, so the topmost flag Qt sets once at creation leaves the light under it."""
    try:
        ctypes.windll.user32.SetWindowPos(int(widget.winId()), HWND_TOPMOST, 0, 0, 0, 0,
                                          SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_NOOWNERZORDER)
    except (AttributeError, OSError):
        pass


def paint(painter: QPainter, rect: QRect, edge: str, style: str, colour: str, state: GlowState, band: float = BAND_PX,
          dark: bool = False, taper: bool = False) -> None:
    """Draws the glow along `edge` of `rect`, `band` deep: the palette along the edge, cut by a
    falloff inward from it and, for the beam, the comet's profile along it. `dark` lifts the
    darkest colours as the crossing effects do on a dark appearance; `taper` fades both ends, for a
    third of the edge."""
    beam = style == "beam"
    strength = max(state.pressure * 0.85, state.flash, state.finish if beam else 0.0)
    if strength <= 0.0:
        return
    along = edge in ("left", "right")
    length = rect.height() if along else rect.width()
    painter.save()
    painter.translate(rect.topLeft())
    local = QRect(0, 0, rect.width(), rect.height())
    painter.setOpacity(strength)
    painter.fillRect(local, _colours(colour, along, length, dark))
    painter.setOpacity(1.0)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    if beam:
        painter.fillRect(local, _falloff(edge, local, band, BEAM_FALLOFF))
        painter.fillRect(local, _comet(along, length, state))
    else:
        depth = band * (0.35 + 0.65 * max(state.pressure, state.flash))
        painter.fillRect(local, _falloff(edge, local, depth, ((0.0, 1.0), (1.0, 0.0))))
    if taper and length > 0:
        ends = _gradient_along(along, length)
        fade = min(0.25, TAPER_PX / length)
        for at, alpha in ((0.0, 0.0), (fade, 1.0), (1.0 - fade, 1.0), (1.0, 0.0)):
            ends.setColorAt(at, QColor(0, 0, 0, round(255 * alpha)))
        painter.fillRect(local, ends)
    painter.restore()


def paint_corner(painter: QPainter, rect: QRect, corner: str, style: str, colour: str, state: GlowState,
                 band: float = BAND_PX, dark: bool = False) -> None:
    """Draws the glow into `corner` of `rect`: the band along both of the corner's walls, brightest
    where they meet and fading out along each, so it reads as the corner rather than two edges.
    The beam's comet runs in along one wall and out along the other. Each wall is drawn on a layer
    of its own, as its masks would otherwise cut the other wall's light."""
    vertical, horizontal = corner.split("_")
    ratio = painter.device().devicePixelRatioF() if painter.device() is not None else 1.0
    for wall in (vertical, horizontal):
        layer = QImage(rect.size() * ratio, QImage.Format.Format_ARGB32_Premultiplied)
        layer.setDevicePixelRatio(ratio)
        layer.fill(Qt.GlobalColor.transparent)
        local = QRect(0, 0, rect.width(), rect.height())
        side = copy.copy(state)
        # Along the L the comet travels 0 to 1: the top or bottom wall from its
        # far end into the corner over the first half, the other wall out of it over the second.
        if wall == vertical:
            side.centre = 2.0 * state.centre if horizontal == "right" else 1.0 - 2.0 * state.centre
        else:
            side.centre = 2.0 - 2.0 * state.centre if vertical == "bottom" else 2.0 * state.centre - 1.0
        layer_painter = QPainter(layer)
        paint(layer_painter, local, wall, style, colour, side, band, dark)
        layer_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        along = wall in ("left", "right")
        at_corner = (vertical == "bottom") if along else (horizontal == "right")
        length = local.height() if along else local.width()
        fade = _gradient_along(along, length)
        for at, alpha in ((0.0, 1.0), (0.3, 0.75), (1.0, 0.0)):
            fade.setColorAt(1.0 - at if at_corner else at, QColor(0, 0, 0, round(255 * alpha)))
        layer_painter.fillRect(local, fade)
        layer_painter.end()
        painter.drawImage(rect.topLeft(), layer)


def _gradient_along(along: bool, length: int) -> QLinearGradient:
    return QLinearGradient(0, 0, 0, length) if along else QLinearGradient(0, 0, length, 0)


def _colours(colour: str, along: bool, length: int, dark: bool = False) -> QLinearGradient:
    gradient = _gradient_along(along, length)
    palette = effects.legible(app_config.palette_colours(colour), dark)
    if len(palette) == 1:
        palette = palette * 2
    for index, value in enumerate(palette):
        gradient.setColorAt(index / (len(palette) - 1), QColor(value))
    return gradient


def _falloff(edge: str, rect: QRect, depth: float, stops) -> QLinearGradient:
    width, height = rect.width(), rect.height()
    gradient = {
        "left": lambda: QLinearGradient(0, 0, depth, 0),
        "right": lambda: QLinearGradient(width, 0, width - depth, 0),
        "top": lambda: QLinearGradient(0, 0, 0, depth),
    }.get(edge, lambda: QLinearGradient(0, height, 0, height - depth))()
    for at, alpha in stops:
        gradient.setColorAt(at, QColor(0, 0, 0, round(255 * alpha)))
    return gradient


def _comet(along: bool, length: int, state: GlowState) -> QLinearGradient:
    gradient = _gradient_along(along, length)
    for index in range(PROFILE_SAMPLES + 1):
        at = index / PROFILE_SAMPLES
        gradient.setColorAt(at, QColor(0, 0, 0, round(255 * comet_alpha(at, state.centre, state.flash))))
    return gradient


class EdgeGlow(QWidget):
    def __init__(self) -> None:
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
        self._edge = "left"
        self._part = None
        self._style = "glow"
        self._colour = "signal"
        self._state = GlowState()
        self._dark = True
        self._updated_at = 0.0
        self._ticked_at = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def configure(self, style: str, colour: str, length: str = "normal") -> None:
        self._style = style
        self._colour = colour
        self._state.pace = effects.pace(length)

    def set_pressure(self, edge: str, pressure: float, crossed: bool, part=None) -> None:
        """`part` is the third of the edge being pushed for Part of the edge, which lights only
        that third, or the corner being pushed into, which lights that corner; None lights the
        whole edge."""
        now = time.monotonic()
        if edge != self._edge or part != self._part or self.isHidden():
            self._edge, self._part = edge, part
            self._place()
        self._state.push(pressure, crossed)
        self._updated_at = now
        if self.isHidden():
            self._ticked_at = now
            self._dark = system_dark()
            self._state.restart()
            self.show()
            self._timer.start()
        keep_on_top(self)
        self.update()

    def _place(self) -> None:
        screens = QApplication.screens()
        if not screens:
            return
        rects = [crossing.Rect(s.geometry().x(), s.geometry().y(), s.geometry().width(), s.geometry().height()) for s in screens]
        # The display the pointer is pushing on, as the effects' region takes it: on staggered
        # displays the outermost one may not be it, and a third of the wrong display would light.
        cursor = QCursor.pos()
        owner = next((r for r in rects if r.x <= cursor.x() < r.x + r.width and r.y <= cursor.y() < r.y + r.height),
                     None) or crossing.owning_monitor(rects, self._edge)
        if self._part in crossing.CORNERS:
            box = corner_of_monitor(owner, self._part, min(CORNER_ARM_PX, owner.width, owner.height))
            self.setGeometry(QRect(box.x, box.y, box.width, box.height))
            return
        owner = part_of_monitor(owner, self._edge, self._part)
        if self._edge == "left":
            self.setGeometry(QRect(owner.x, owner.y, BAND_PX, owner.height))
        elif self._edge == "right":
            self.setGeometry(QRect(owner.right - BAND_PX + 1, owner.y, BAND_PX, owner.height))
        elif self._edge == "top":
            self.setGeometry(QRect(owner.x, owner.y, owner.width, BAND_PX))
        else:
            self.setGeometry(QRect(owner.x, owner.bottom - BAND_PX + 1, owner.width, BAND_PX))

    def _tick(self) -> None:
        now = time.monotonic()
        elapsed = now - self._ticked_at
        self._ticked_at = now
        # The model only decays when a delta arrives, so once the push stops
        # the glow fades on its own clock, matching the model's 400ms.
        if not self._state.step(elapsed, now - self._updated_at > STALE_SECONDS):
            self._timer.stop()
            self.hide()
            return
        keep_on_top(self)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        if self._part in crossing.CORNERS:
            paint_corner(painter, self.rect(), self._part, self._style, self._colour, self._state, dark=self._dark)
        else:
            paint(painter, self.rect(), self._edge, self._style, self._colour, self._state, dark=self._dark,
                  taper=self._part in crossing.PARTS)
        painter.end()


class GlowPreview(QWidget):
    """A little desktop with the glow playing along the edge that leads to the Mac, painted by the
    same `paint` and `GlowState` as the real one, at its real depth. `style` is fixed per preview;
    the colour and the edge follow the settings."""

    def __init__(self, style: str, colour: str = "signal", edge: str = "right", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.style = style
        self.colour = colour
        self.edge = edge
        self.state = GlowState()
        self.setFixedHeight(PREVIEW_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_look(self, colour: str, edge: str) -> None:
        if (colour, edge) != (self.colour, self.edge):
            self.colour, self.edge = colour, edge
            self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = tokens.RADIUS["field"]
        outline = QPainterPath()
        outline.addRoundedRect(frame, radius, radius)
        ground = QLinearGradient(0, 0, 0, self.height())
        ground.setColorAt(0.0, QColor(theme.colour("edge")))
        ground.setColorAt(1.0, QColor(theme.colour("well")))
        painter.fillPath(outline, ground)
        # Painted on a layer of its own, as the real glow is on its own window: its masks cut the
        # glow, and on the widget itself they would cut the ground under it too.
        ratio = self.devicePixelRatioF()
        layer = QImage(self.size() * ratio, QImage.Format.Format_ARGB32_Premultiplied)
        layer.setDevicePixelRatio(ratio)
        layer.fill(Qt.GlobalColor.transparent)
        layer_painter = QPainter(layer)
        paint(layer_painter, self.rect(), self.edge, self.style, self.colour, self.state, dark=theme.is_dark())
        layer_painter.end()
        painter.save()
        painter.setClipPath(outline)
        painter.drawImage(0, 0, layer)
        painter.restore()
        painter.setPen(QPen(QColor(theme.colour("rule")), 1))
        painter.drawPath(outline)
        painter.end()


class PreviewLoop:
    """Plays Glow and Beam's tiles from one clock: only the one the pointer is over, from the moment
    that looks like intent, and only while someone can see it. Every other tile holds a still, the
    push part way in, which a tile that starts playing goes on from."""

    HOLD = 0.7
    INTENT_S = 0.1

    def __init__(self, parent: QWidget) -> None:
        self.previews: list = []
        self.running = False
        self.hovered = None
        self._hovered_at = 0.0
        self._started_at = None
        self._ticked_at = 0.0
        self._phase = None
        self._timer = QTimer(parent)
        # Precise: a coarse timer may fire up to 5% either side, which lands frames unevenly.
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def add(self, preview: GlowPreview) -> None:
        self.previews.append(preview)
        self._hold(preview)

    def run(self, wanted: bool) -> None:
        if wanted == self.running:
            return
        self.running = wanted
        self._settle()
        if wanted:
            self._timer.start()
        else:
            self._timer.stop()

    def hover(self, preview) -> None:
        """`preview`, one of this loop's, plays while the pointer is over it; None stops it."""
        if preview is self.hovered:
            return
        self._settle()
        self.hovered, self._hovered_at = preview, time.monotonic()

    def playing(self, preview) -> bool:
        return preview is self.hovered and self._started_at is not None

    def _hold(self, preview: GlowPreview) -> None:
        preview.state = GlowState()
        preview.state.push(self.HOLD, False)
        preview.state.centre = 0.5
        preview.update()

    def _settle(self) -> None:
        if self.hovered is not None and self._started_at is not None:
            self._hold(self.hovered)
        self._started_at = None

    def _tick(self) -> None:
        preview = self.hovered
        now = time.monotonic()
        if preview is None or now - self._hovered_at < self.INTENT_S:
            return
        if not preview.isVisible() or preview.visibleRegion().isEmpty():
            self._settle()
            return
        if self._started_at is None:
            # Where the script's push reaches the held still's pressure.
            self._started_at = now - PUSH_S * self.HOLD ** (1 / 1.6)
            self._ticked_at, self._phase = now, "push"
        elapsed, self._ticked_at = now - self._ticked_at, now
        level, phase = preview_frame(now - self._started_at)
        crossed = phase == "after" and self._phase == "push"
        self._phase = phase
        if crossed:
            preview.state.push(1.0, True)
        elif phase == "push":
            preview.state.push(level, False)
        preview.state.step(elapsed, phase != "push")
        preview.update()
