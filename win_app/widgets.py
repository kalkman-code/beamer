"""The Vernier components the receiver window draws that a stylesheet cannot: module, eyebrow,
LED, the 60-segment countdown and the switch."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QRect, QRectF, QSignalBlocker, QSize, Qt
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QBoxLayout,
    QButtonGroup,
    QCheckBox,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSystemTrayIcon,
    QToolTip,
    QStyle,
    QStyleOptionButton,
    QStyleOptionFrame,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)

import motion
import theme
import tokens
from core.locale import americanise
from core import ways as core_ways

LEFT_CTRL_VK = 0xA2
# The smallest a Segmented cell or small button is drawn, so each is a fair target.
MIN_TARGET = 30


def set_dimmed(view: QWidget, dimmed: bool) -> None:
    if dimmed:
        current = view.graphicsEffect()
        if isinstance(current, QGraphicsOpacityEffect) and current.opacity() == 0.45:
            return
        effect = QGraphicsOpacityEffect(view)
        effect.setOpacity(0.45)
        view.setGraphicsEffect(effect)
    elif isinstance(view.graphicsEffect(), QGraphicsOpacityEffect):
        view.setGraphicsEffect(None)

_NAVIGATION_KEYS = {Qt.Key.Key_Tab, Qt.Key.Key_Backtab, Qt.Key.Key_Left, Qt.Key.Key_Right,
                    Qt.Key.Key_Up, Qt.Key.Key_Down}
_SHORTCUT_MODIFIERS = (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
                       | Qt.KeyboardModifier.MetaModifier)


def is_navigation_key(key, modifiers) -> bool:
    """Tab, Shift-Tab and the arrows turn focus rings on (spec section 6), as on the Mac. Any
    other key, a shortcut included, leaves them as the last click left them: off."""
    return key in _NAVIGATION_KEYS and not modifiers & _SHORTCUT_MODIFIERS


class _KeyboardNavigation(QObject):
    """The one app-wide answer to "is the user moving focus by keyboard": on for a navigation
    key, off for any mouse press. The stylesheet's :focus rings match only a widget whose
    `keyboard_ring` property is true, which is kept in step on the focused widget alone, so a
    change repolishes one widget rather than the window."""

    def __init__(self, app) -> None:
        super().__init__(app)
        self.active = False
        app.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.KeyPress and is_navigation_key(event.key(), event.modifiers()):
            self.set(True)
        elif kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            self.set(False)
        elif kind == QEvent.Type.FocusIn and isinstance(watched, QWidget):
            self._mark(watched)
        return False

    def set(self, active: bool) -> None:
        if active == self.active:
            return
        self.active = active
        focused = QApplication.focusWidget()
        if focused is not None:
            self._mark(focused)

    def _mark(self, widget) -> None:
        # Qt's own focus rectangle keys off this window attribute, which Tab sets and only a
        # click that moves focus clears; a click on a label left it set, so it follows the state.
        widget.window().setAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange, self.active)
        if bool(widget.property("keyboard_ring")) != self.active:
            widget.setProperty("keyboard_ring", self.active)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        widget.update()
        for child in widget.findChildren(QWidget):
            child.update()


def keyboard_navigation() -> _KeyboardNavigation:
    app = QApplication.instance()
    navigation = app.findChild(_KeyboardNavigation)
    return navigation if navigation is not None else _KeyboardNavigation(app)


def focus_ring_visible(widget) -> bool:
    return widget.hasFocus() and keyboard_navigation().active


def label(text: str, role: str, wrap: bool = False) -> QLabel:
    item = QLabel(americanise(text))
    item.setProperty("vernier", role)
    item.setWordWrap(wrap)
    return item


def install_text_localisation() -> None:
    """Route Qt's visible text setters through the shared spelling map."""
    from PySide6.QtWidgets import QMessageBox, QMenu

    def wrap(cls, method, positions=(0,)):
        original = getattr(cls, method)
        if getattr(original, "_beamer_localised", False):
            return

        def setter(self, *args, **kwargs):
            args = list(args)
            for position in positions:
                if position < len(args) and isinstance(args[position], str):
                    args[position] = americanise(args[position])
            return original(self, *args, **kwargs)

        setter._beamer_localised = True
        setattr(cls, method, setter)

    def wrap_static(cls, method, positions):
        original = getattr(cls, method)
        if getattr(original, "_beamer_localised", False):
            return

        def setter(*args, **kwargs):
            args = list(args)
            for position in positions:
                if position < len(args) and isinstance(args[position], str):
                    args[position] = americanise(args[position])
            return original(*args, **kwargs)

        setter._beamer_localised = True
        setattr(cls, method, staticmethod(setter))

    for cls, method in (
        (QLabel, "setText"), (QAbstractButton, "setText"), (QGroupBox, "setTitle"),
        (QMenu, "setTitle"), (QMenu, "addAction"), (QWidget, "setToolTip"),
        (QAction, "setText"), (QMessageBox, "setText"), (QMessageBox, "setInformativeText"),
        (QMessageBox, "setDetailedText"),
        (QSystemTrayIcon, "setToolTip"), (QSystemTrayIcon, "showMessage"),
    ):
        wrap(cls, method, (0, 1) if method == "showMessage" else (0,))
    for method in ("critical", "information", "question", "warning"):
        wrap_static(QMessageBox, method, (1, 2))
    wrap_static(QToolTip, "showText", (1,))


def set_role(widget: QWidget, role: str) -> None:
    if widget.property("vernier") != role:
        widget.setProperty("vernier", role)
        theme.repolish(widget)


def _elide_lines(text: str, metrics: QFontMetrics, width: int) -> str:
    """Elide each painted line independently, preserving line structure and the source string."""
    return "\n".join(metrics.elidedText(line, Qt.TextElideMode.ElideRight, max(0, width))
                     for line in text.split("\n"))


def _elide_with_suffix(text: str, suffix: str, metrics: QFontMetrics, width: int) -> str:
    """Keep a machine row's platform/status suffix visible while its leading name is elided."""
    suffix_width = metrics.horizontalAdvance(suffix)
    if not suffix or not text.endswith(suffix) or suffix_width >= width:
        return _elide_lines(text, metrics, width)
    leading = text[:-len(suffix)]
    return _elide_lines(leading, metrics, width - suffix_width) + suffix


def _elision_tooltip(widget: QWidget, source: str, displayed: str) -> None:
    """Expose the complete text on hover when this widget has to elide it, without replacing a
    deliberate tooltip supplied by its caller."""
    previous = getattr(widget, "_elision_tooltip", None)
    current = widget.toolTip()
    if current and current != previous:
        return
    tooltip = source if displayed != source else ""
    if current != tooltip:
        widget.setToolTip(tooltip)
    widget._elision_tooltip = tooltip or None


class ElidingButton(QPushButton):
    """A native QPushButton that paints an end ellipsis while retaining its complete Qt text."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._elision_suffix = ""
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
        self.setSizePolicy(policy)

    def set_elision_suffix(self, suffix: str) -> None:
        if suffix != self._elision_suffix:
            self._elision_suffix = suffix
            self.updateGeometry()
            self.update()

    def displayed_text(self) -> str:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        content = self.style().subElementRect(
            QStyle.SubElement.SE_PushButtonContents, option, self)
        displayed = _elide_with_suffix(self.text(), self._elision_suffix,
                                        self.fontMetrics(), content.width())
        _elision_tooltip(self, self.text(), displayed)
        return displayed

    def minimumSizeHint(self) -> QSize:
        size = super().minimumSizeHint()
        metrics = self.fontMetrics()
        text_width = max((metrics.horizontalAdvance(line.replace("&", ""))
                          for line in self.text().split("\n")), default=0)
        ellipsis_width = metrics.horizontalAdvance("…")
        size.setWidth(max(ellipsis_width, size.width() - text_width + ellipsis_width))
        return size

    def paintEvent(self, _event) -> None:
        painter = QStylePainter(self)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = self.displayed_text()
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)


class ElidingLabel(QLabel):
    """A single-line label that paints an end ellipsis while retaining its complete Qt text."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(False)

    def displayed_text(self) -> str:
        content = self.contentsRect().adjusted(self.margin(), self.margin(),
                                               -self.margin(), -self.margin())
        indent = self.indent()
        if indent > 0:
            content.adjust(indent, 0, 0, 0)
        displayed = _elide_lines(self.text(), self.fontMetrics(), content.width())
        _elision_tooltip(self, self.text(), displayed)
        return displayed

    def minimumSizeHint(self) -> QSize:
        size = super().minimumSizeHint()
        metrics = self.fontMetrics()
        text_width = max((metrics.horizontalAdvance(line) for line in self.text().split("\n")),
                         default=0)
        ellipsis_width = metrics.horizontalAdvance("…")
        size.setWidth(max(ellipsis_width, size.width() - text_width + ellipsis_width))
        return size

    def paintEvent(self, _event) -> None:
        option = QStyleOptionFrame()
        self.initStyleOption(option)
        painter = QStylePainter(self)
        painter.drawPrimitive(QStyle.PrimitiveElement.PE_Widget, option)
        content = self.contentsRect().adjusted(self.margin(), self.margin(),
                                               -self.margin(), -self.margin())
        indent = self.indent()
        if indent > 0:
            content.adjust(indent, 0, 0, 0)
        alignment = QStyle.visualAlignment(self.layoutDirection(), self.alignment())
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(content, alignment, self.displayed_text())


def refresh_colours(root: QWidget) -> None:
    """Re-paints every colour under `root` after theme.set_dark() swaps the palette: `update()`
    for a widget that reads theme.colour() at paint time (nearly all of them), plus that widget's
    own `refresh_colours()` for one that instead baked a colour into a stylesheet or rich text at
    construction."""
    for widget in (root, *root.findChildren(QWidget)):
        own_refresh = getattr(widget, "refresh_colours", None)
        if callable(own_refresh):
            own_refresh()
        widget.update()


class Eyebrow(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setProperty("vernier", "eyebrow")
        self.setFont(theme.eyebrow_font())
        self.setWordWrap(True)
        self.setMinimumWidth(0)


class Module(QFrame):
    """One `panel` cell in the rack, eyebrow first."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("vernier", "module")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 16, 16, 16)
        self.body.setSpacing(12)
        self.eyebrow = Eyebrow(title)
        self.body.addWidget(self.eyebrow)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)


class ActionButton(QPushButton):
    """An intrinsic action whose full label remains readable in a narrow column."""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setMaximumWidth(240)
        self.setMinimumHeight(30)
        policy = QSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def _chrome(self):
        """Width the style adds round the label (border and the stylesheet's padding, which differs
        per `vernier` kind). A fixed 24 ignored the 2 px border, so the label was measured 2 px short
        of the room it painted in and wrapped inside a 30 px button."""
        option = QStyleOptionButton()
        self.initStyleOption(option)
        grown = self.style().sizeFromContents(QStyle.ContentsType.CT_PushButton, option, QSize(100, 20), self)
        return grown.width() - 100

    def heightForWidth(self, width):
        width = min(width, self.maximumWidth())
        room = max(1, width - self._chrome())
        metrics = self.fontMetrics()
        if metrics.horizontalAdvance(self.text()) < room:
            # boundingRect wraps a label that exactly fills its room, so a button at its own
            # sizeHint asked for two lines.
            return max(30, metrics.height() + 12)
        bounds = metrics.boundingRect(QRect(0, 0, room, 10000), int(Qt.TextFlag.TextWordWrap), self.text())
        return max(30, bounds.height() + 12)

    def sizeHint(self):
        width = min(240, max(64, self.fontMetrics().horizontalAdvance(self.text()) + self._chrome() + 2))
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self):
        return QSize(0, 30)

    def displayed_text(self):
        return self.text()

    def paintEvent(self, event):
        painter = QStylePainter(self)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)
        content = self.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, self)
        if self.property("vernier") == "choice" and self.isChecked() and self.isEnabled():
            painter.setPen(QColor(tokens.PALETTE["ground"]))
        elif self.property("vernier") in ("primary", "live") and self.isEnabled():
            painter.setPen(QColor(theme.colour("ground")))
        else:
            painter.setPen(option.palette.color(QPalette.ColorRole.ButtonText))
        painter.drawText(content, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.text())


class FormRow(QWidget):
    """The shared label track stacks before it would compress a setting's control."""

    def __init__(self, heading, control, notes=(), parent=None):
        super().__init__(parent)
        self.setProperty("vernier", "plain")
        self.heading = heading
        heading.setProperty("form_label", True)
        heading.setWordWrap(False)
        heading.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._box = QBoxLayout(QBoxLayout.Direction.TopToBottom, self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(8)
        self._box.addWidget(heading, 0, Qt.AlignmentFlag.AlignTop)
        holder = QWidget()
        self.holder = holder
        holder.setProperty("vernier", "plain")
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        # A wrapping label as an aligned item would get its size hint's width, narrower than the
        # height the row reserved for it (InputRecorder's hint); its text is left-aligned anyway.
        wraps = isinstance(control, QLabel) and control.wordWrap()
        column.addWidget(control, 0, Qt.AlignmentFlag(0) if wraps else Qt.AlignmentFlag.AlignLeft)
        for note in notes:
            column.addWidget(note)
        self._box.addWidget(holder, 1)
        self._inline = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        inline = self.width() + 32 >= 560
        if inline != self._inline:
            self._inline = inline
            self._box.setDirection(QBoxLayout.Direction.LeftToRight if inline else QBoxLayout.Direction.TopToBottom)
            self._box.setSpacing(16 if inline else 8)
            self.heading.setMinimumWidth(180 if inline else 0)
            self.heading.setMaximumWidth(180 if inline else 16777215)


class _SegmentedView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("vernier", "plain")
        self.setProperty("segmented_choice", True)
        self.buttons = []
        self.cap = 360
        self.required = 0
        self.columns = 0
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def set_cap(self, width):
        self.cap = width
        self.setProperty("segment_cap", width)
        self.setMaximumWidth(width)
        self.updateGeometry()

    def _horizontal(self, width):
        return width >= self.required

    def cell_width(self):
        widths = []
        for button in self.buttons:
            for weight in (400, 600):
                metrics = QFontMetrics(theme.font(theme.TYPE["small"], weight))
                widths.append(max(metrics.horizontalAdvance(button.text()),
                                  metrics.boundingRect(button.text()).width()) + 24)
        return max(widths, default=0)

    def refresh_metrics(self):
        self.required = self.cell_width() * len(self.buttons) + 4 * max(0, len(self.buttons) - 1)
        columns = self.columns or len(self.buttons)
        self.set_cap(self.cell_width() * columns + 4 * max(0, columns - 1))

    def row_columns(self, width):
        columns = self.columns or max(1, len(self.buttons))
        return columns if width >= self.cell_width() * columns + 4 * max(0, columns - 1) else 1

    def heightForWidth(self, width):
        if not self.buttons:
            return 0
        width = min(width, self.cap)
        columns = self.row_columns(width)
        segment_width = min(self.cell_width(), max(1, (width - 4 * (columns - 1)) // columns))
        height = max(button.heightForWidth(segment_width) for button in self.buttons)
        rows = (len(self.buttons) + columns - 1) // columns
        return rows * height + 4 * (rows - 1)

    def sizeHint(self):
        return QSize(self.cap, self.heightForWidth(self.cap))

    def minimumSizeHint(self):
        return QSize(0, 30 if self.buttons else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # The machine choice is empty with fewer than two machines. An exception raised
        # here during a layout pass segfaults PySide6 6.11 at the next Python override.
        if not self.buttons:
            return
        columns = self.row_columns(self.width())
        segment_width = min(self.cell_width(), max(1, (self.width() - 4 * (columns - 1)) // columns))
        row_height = max((button.heightForWidth(segment_width) for button in self.buttons), default=30)
        for index, button in enumerate(self.buttons):
            col, row = index % columns, index // columns
            left = round(col * (self.width() + 4) / columns)
            right = round((col + 1) * (self.width() + 4) / columns) - 4
            button.setGeometry(left, row * (row_height + 4), min(self.cell_width(), right - left), row_height)


class Led(QWidget):
    def __init__(self, tone: str = "off", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tone = tone
        self.setFixedSize(QSize(8, 8))

    def set_tone(self, tone: str) -> None:
        if tone != self.tone:
            self.tone = tone
            self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.colour(self.tone)))
        painter.drawEllipse(0, 0, 8, 8)


class Drain(QWidget):
    """Sixty segments, one per second of the code's life, emptying left to right in `rule`."""

    SEGMENTS = 60

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.remaining = 0
        self.setFixedHeight(12)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_remaining(self, seconds: int) -> None:
        seconds = max(0, min(self.SEGMENTS, int(seconds)))
        if seconds != self.remaining:
            self.remaining = seconds
            self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        gap = 1.0
        width = (self.width() - gap * (self.SEGMENTS - 1)) / self.SEGMENTS
        lit, spent = QColor(theme.colour("signal")), QColor(theme.colour("rule"))
        for index in range(self.SEGMENTS):
            painter.fillRect(
                QRectF(index * (width + gap), 0, width, self.height()),
                lit if index < self.remaining else spent,
            )


def rule(vertical: bool = True) -> QWidget:
    """A one-pixel divider in `rule`, the sidebar/content seam or a horizontal seam under it."""
    widget = QWidget()
    widget.setProperty("vernier", "rule")
    if vertical:
        widget.setFixedWidth(1)
    else:
        widget.setFixedHeight(1)
    return widget


class SideBySide(QWidget):
    """Two blocks beside each other once the width holds both at their minimums, one above the other
    below that. Only the direction of one box layout changes, so the widgets never re-parent."""

    def __init__(self, first: QWidget, second: QWidget, gap: int = 24, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("vernier", "plain")
        self._box = QBoxLayout(QBoxLayout.Direction.TopToBottom, self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(gap)
        self._box.addWidget(first, 1)
        self._box.addWidget(second, 1)
        self.first = first
        self.second = second
        self.gap = gap
        self._beside = False
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def heightForWidth(self, width: int) -> int:
        beside = width >= self.needed()
        room = max(1, (width - self.gap) // 2 if beside else width)
        heights = [child.heightForWidth(room) if child.hasHeightForWidth() else child.sizeHint().height()
                   for child in (self.first, self.second)]
        return max(heights) if beside else sum(heights) + self.gap

    def needed(self) -> int:
        return self.first.minimumSizeHint().width() + self.second.minimumSizeHint().width() + self.gap

    def beside(self) -> bool:
        return self._beside

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        beside = self.width() >= self.needed()
        if beside != self._beside:
            self._beside = beside
            self._box.setDirection(QBoxLayout.Direction.LeftToRight if beside else QBoxLayout.Direction.TopToBottom)


class Choice:
    """A row of exclusive Vernier choice buttons, `columns` wide. Pick a divisor of
    `len(items)` so a wrap never orphans one. `on_change(value)` fires only on a real
    click -- never on `set_value`, so refreshing from an incoming setting cannot loop back."""

    def __init__(self, items, columns: int, current: str, on_change=None, parent: QWidget | None = None,
                 exclusive=True) -> None:
        self.view = _SegmentedView(parent)
        self._buttons: dict = {}
        for index, (value, text) in enumerate(items):
            button = ActionButton(text, self.view)
            button.setMaximumWidth(16777215)
            button.setProperty("vernier", "choice")
            button.setCheckable(True)
            button.setAutoExclusive(exclusive)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumHeight(MIN_TARGET)
            button.setChecked(value == current)
            if on_change is not None:
                button.clicked.connect(lambda _checked=False, v=value: on_change(v))
            self.view.buttons.append(button)
            self._buttons[value] = button
        self.view.columns = columns if columns < len(self.view.buttons) else 0
        self.view.refresh_metrics()

    def set_width(self, width):
        self.view.refresh_metrics()

    @property
    def value(self):
        return next((value for value, button in self._buttons.items() if button.isChecked()), None)

    def set_value(self, value) -> None:
        """A value that is not a choice selects nothing."""
        button = self._buttons.get(value)
        if button is None:
            for other in self._buttons.values():
                if other.isChecked():
                    other.setAutoExclusive(False)
                    other.setChecked(False)
                    other.setAutoExclusive(True)
        elif not button.isChecked():
            button.setChecked(True)

    def set_enabled(self, enabled: bool) -> None:
        for button in self._buttons.values():
            button.setEnabled(enabled)
        set_dimmed(self.view, not enabled)

    def set_names(self, prefix: str) -> None:
        """Each cell's accessible name leads with what the row chooses: "Where your Mac is, Left"."""
        for button in self._buttons.values():
            button.setAccessibleName(f"{prefix}, {button.text()}")


class _TileOutline(QWidget):
    def __init__(self, tile):
        super().__init__(tile)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setProperty("vernier", "plain")

    def paintEvent(self, event):
        tile = self.parentWidget()
        selected = tile.isChecked()
        focused = focus_ring_visible(tile)
        if not selected and not focused:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if selected:
            painter.setPen(QPen(QColor(theme.colour("signal")), 2))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1),
                                    tokens.RADIUS["button"], tokens.RADIUS["button"])
            x = self.width() - 20
            painter.drawLine(x, 12, x + 3, 15)
            painter.drawLine(x + 3, 15, x + 9, 8)
        if focused:
            painter.setPen(QPen(QColor(theme.colour("ink")), 2))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(5, 5, -5, -5), 2, 2)


class _Tile(QPushButton):
    """One tile of ChoiceTiles: the whole tile is the control, by click or by Space from Tab."""

    def __init__(self, choose, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._choose = choose
        self._hovered = False
        self.setCheckable(True)
        self.toggled.connect(lambda _checked=False: choose())
        self.setProperty("vernier", "tile")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.outline = _TileOutline(self)

    def paintEvent(self, event):
        painter = QStylePainter(self)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""
        if self._hovered:
            option.state |= QStyle.StateFlag.State_MouseOver
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.outline.update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.outline.update()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._choose()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.outline.setGeometry(self.rect())
        self.outline.raise_()

    def hasHeightForWidth(self) -> bool:
        layout = self.layout()
        return layout is not None and layout.hasHeightForWidth()

    def heightForWidth(self, width: int) -> int:
        layout = self.layout()
        return layout.totalHeightForWidth(width) if layout is not None else self.sizeHint().height()


class ChoiceTiles:
    """One of several, as tiles side by side: each a live preview over its name and a line saying
    what it is. The chosen tile is ringed by the TileGroups it sits in, in ink, outside the tile, so
    the signal focus border inside it still shows. `on_change(value)` fires only on a real choice,
    never on `set_value`."""

    def __init__(self, items, current, on_change=None, parent: QWidget | None = None,
                 tile_width: int | None = None, vertical: bool = False, responsive: bool = False) -> None:
        self.on_change = on_change
        self.value = None
        self.view = _FamilyChoices("style", parent) if responsive else QWidget(parent)
        self.view.setProperty("vernier", "plain")
        row = None
        if not responsive:
            row = (QVBoxLayout if vertical else QHBoxLayout)(self.view)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8 if vertical else 10)
            if tile_width is not None:
                row.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._tiles: dict = {}
        self._titles: dict = {}
        for value, title, detail, preview in items:
            tile = _Tile(lambda v=value: self._chosen(v))
            if vertical:
                policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
                policy.setHeightForWidth(True)
                tile.setSizePolicy(policy)
            if tile_width is not None:
                tile.setFixedWidth(tile_width)
            self._titles[value] = title
            tile.setAccessibleName(title)
            tile.setAccessibleDescription(detail)
            column = QVBoxLayout(tile)
            column.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
            column.setContentsMargins(8, 8, 8, 12)
            column.setSpacing(6)
            column.addWidget(preview)
            if not vertical:
                column.addSpacing(4)
            column.addWidget(label(title, "tile-name", wrap=tile_width is not None))
            column.addWidget(label(detail, "small", wrap=True))
            if vertical:
                column.setSpacing(6)
                column.insertSpacing(1, 4)
            else:
                column.addStretch(1)
            if responsive:
                self.view.add_cell(tile)
            else:
                row.addWidget(tile, 0 if vertical or tile_width is not None else 1)
            self._tiles[value] = tile
        self.set_value(current)

    def set_value(self, value) -> None:
        self.value = value
        for candidate, tile in self._tiles.items():
            with QSignalBlocker(tile):
                tile.setChecked(candidate == value)
            set_role(tile, "tile-on" if candidate == value else "tile")
            tile.outline.update()
            title = self._titles[candidate]
            tile.setAccessibleName(f"{title}, selected" if candidate == value else title)

    def tile(self, value):
        return self._tiles.get(value)

    def set_enabled(self, enabled: bool) -> None:
        fade = QGraphicsOpacityEffect(self.view)
        fade.setOpacity(1.0 if enabled else 0.45)
        self.view.setGraphicsEffect(None if enabled else fade)
        for tile in self._tiles.values():
            tile.setEnabled(enabled)

    def _chosen(self, value) -> None:
        if value == self.value:
            self.set_value(value)
            return
        self.set_value(value)
        if self.on_change is not None:
            self.on_change(value)


class _FamilyChoices(QWidget):
    """A family's logical tracks; height measurement never mutates its children."""

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.cells = []
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def add_cell(self, cell):
        cell.setParent(self)
        self.cells.append(cell)

    def _tracks(self, width):
        outer = width + 32
        columns = ((3 if outer >= 600 else 2 if outer >= 400 else 1) if self.kind == "style"
                   else (5 if outer >= 600 else 3 if outer >= 400 else 2))
        gap = 12 if self.kind == "style" else 8
        cell_width = max(1, (width - gap * (columns - 1) + columns - 1) // columns)
        if len(self.cells) == 1:
            columns, cell_width = 1, min(width, 152)
        if self.kind == "style":
            height = max(round(cell_width * 116 / 152), round((cell_width - 16) / 2) + 64)
        else:
            height = max((cell.heightForWidth(cell_width) for cell in self.cells), default=60)
            height = max(60, height)
        return columns, gap, cell_width, height

    def heightForWidth(self, width):
        columns, gap, _, height = self._tracks(width)
        rows = (len(self.cells) + columns - 1) // columns
        return rows * height + max(0, rows - 1) * gap

    def sizeHint(self):
        return QSize(600, self.heightForWidth(600))

    def minimumSizeHint(self):
        return QSize(0, 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        columns, gap, width, height = self._tracks(self.width())
        for index, cell in enumerate(self.cells):
            track = index % columns
            room = self.width() - gap * (columns - 1)
            left = round(track * room / columns) + track * gap
            right = round((track + 1) * room / columns) + track * gap
            if len(self.cells) == 1:
                left, right = 0, width
            cell.setGeometry(left, (index // columns) * (height + gap), right - left, height)
            if self.kind == "style":
                cell.layout().itemAt(0).widget().setFixedHeight(
                    max(1, round((cell.contentsRect().width() - 16) / 2)))

class _Swatch(QPushButton):
    """A colour chip over its name. The name wraps onto a second line rather than widen the grid:
    at the window's minimum width five chips a row have about 65px each."""

    CHIP = 28
    MIN_WIDTH = 44
    NAME_GAP = 4

    def __init__(self, colours, title: str, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self.colours = list(colours) * (2 if len(colours) == 1 else 1)
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(title)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def _face(self, chosen: bool):
        return theme.font(tokens.TYPE["small"], 600 if chosen else 400)

    def _name_height(self, width: int) -> int:
        # Measured at the heavier weight so choosing a colour never re-wraps its name.
        metrics = QFontMetrics(self._face(True))
        return metrics.boundingRect(QRect(0, 0, max(1, width), 1000), int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignHCenter),
                                    self.text()).height()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return 3 + self.CHIP + self.NAME_GAP + self._name_height(width) + 2

    def sizeHint(self) -> QSize:
        width = max(56, QFontMetrics(self._face(True)).horizontalAdvance(self.text()) + 8)
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:
        return QSize(self.MIN_WIDTH, self.heightForWidth(self.MIN_WIDTH))

    def chip_rect(self) -> QRectF:
        return QRectF(3, 3, self.width() - 6, self.CHIP)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        chosen = self.isChecked()
        enabled = self.isEnabled()
        chip = self.chip_rect()
        gradient = QLinearGradient(chip.left(), 0, chip.right(), 0)
        for index, value in enumerate(self.colours):
            gradient.setColorAt(index / (len(self.colours) - 1), QColor(value))
        painter.setOpacity(1.0 if enabled else 0.45)
        painter.setBrush(gradient)
        radius = tokens.RADIUS["field"]
        # The chosen chip's ink ring is SwatchGroups', outside the chip; focus is this signal
        # border on the chip itself, so the two can be told apart when they fall on one chip.
        if focus_ring_visible(self):
            painter.setPen(QPen(QColor(theme.colour("signal")), 2))
            painter.drawRoundedRect(chip.adjusted(1, 1, -1, -1), radius, radius)
        else:
            painter.setPen(QPen(QColor(theme.colour("rule")), 1))
            painter.drawRoundedRect(chip.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        painter.setPen(QColor(theme.colour("ink" if chosen else "ink_2")))
        painter.setFont(self._face(chosen))
        painter.drawText(
            QRectF(0, chip.bottom() + self.NAME_GAP, self.width(), self.height() - chip.bottom() - self.NAME_GAP),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
            self.text(),
        )
        if chosen:
            painter.setPen(QPen(QColor("#161714"), 4))
            x, y = round(chip.right() - 16), round(chip.center().y() - 3)
            painter.drawLine(x, y + 3, x + 3, y + 6)
            painter.drawLine(x + 3, y + 6, x + 9, y)
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.drawLine(x, y + 3, x + 3, y + 6)
            painter.drawLine(x + 3, y + 6, x + 9, y)


class TileGroups:
    """ChoiceTiles in titled groups with one value across all of them: `groups` is (title, items)
    with ChoiceTiles' items. `on_change(value)` fires only on a real choice. Each selected tile
    draws its own accent border."""

    def __init__(self, groups, current, on_change=None, parent: QWidget | None = None) -> None:
        self.on_change = on_change
        self.value = current
        self.view = QWidget(parent)
        self.view.setProperty("vernier", "plain")
        column = QVBoxLayout(self.view)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(20)
        self.groups: dict = {}
        self.group_views: dict = {}
        group_views = []
        for index, (title, items) in enumerate(groups):
            group_view = QWidget()
            group_view.setProperty("vernier", "plain")
            group_column = QVBoxLayout(group_view)
            group_column.setContentsMargins(0, 0, 0, 0)
            group_column.setSpacing(6)
            group_column.addWidget(label(title, "key"))
            tiles = ChoiceTiles(items, current, on_change=self._chosen, responsive=True)
            group_column.addWidget(tiles.view)
            self.groups[title] = tiles
            self.group_views[title] = group_view
            group_views.append((title, group_view))
            column.addWidget(group_view)
        self.grid = self.view
        self.grid._rows = [view for _, view in group_views]
        self.ring = None

    def tile(self, value):
        return next((tile for tiles in self.groups.values() if (tile := tiles.tile(value)) is not None), None)

    def set_value(self, value) -> None:
        self.value = value
        for tiles in self.groups.values():
            tiles.set_value(value)
        if self.ring is not None:
            self.ring.follow(self.tile(value))

    def set_enabled(self, enabled: bool) -> None:
        for tiles in self.groups.values():
            tiles.set_enabled(enabled)

    def _chosen(self, value) -> None:
        self.set_value(value)
        if self.on_change is not None:
            self.on_change(value)


class SwatchGroups:
    """One colour of several in titled rows, each group's name over its chips as the style tiles'
    are: `groups` is (title, ((value, name, colours), ...)). The chips share one exclusive group
    across the rows and line up in equal columns, so a row of three sits under the first three of
    a row of five. One ink ring marks the chosen chip and slides to the next.

    The names sit over the chips, not in a column beside them: beside, the widest name plus five
    chips outgrew the page at the window's minimum width and clipped the whole Design page."""

    def __init__(self, groups, current, on_change=None, parent: QWidget | None = None) -> None:
        self.view = QWidget(parent)
        self.view.setProperty("vernier", "plain")
        column = QVBoxLayout(self.view)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(20)
        self._group = QButtonGroup(self.view)
        self._group.setExclusive(True)
        self._swatches: dict = {}
        self.group_views = {}
        for row, (title, items) in enumerate(groups):
            family = QWidget()
            family.setProperty("vernier", "plain")
            body = QVBoxLayout(family)
            body.setContentsMargins(0, 0, 0, 0)
            body.setSpacing(8)
            heading = label(title, "key")
            body.addWidget(heading)
            choices = _FamilyChoices("colour")
            choices.setProperty("vernier", "plain")
            body.addWidget(choices)
            self.group_views[title] = choices
            column.addWidget(family)
            for index, (value, name, colours) in enumerate(items):
                swatch = _Swatch(colours, name)
                swatch.setAutoExclusive(False)
                self._group.addButton(swatch)
                swatch.setChecked(value == current)
                swatch.toggled.connect(lambda on, s=swatch: on and self.ring.follow(s))
                if on_change is not None:
                    swatch.toggled.connect(lambda on, v=value: on and on_change(v))
                choices.add_cell(swatch)
                self._swatches[value] = swatch
        self.ring = motion.Ring(
            self.view,
            lambda swatch: swatch.chip_rect().adjusted(-2.5, -2.5, 2.5, 2.5),
            lambda: theme.colour("ink"),
            tokens.RADIUS["field"] + 2.5,
        )
        self.ring.follow(self._swatches.get(current))

    @property
    def value(self):
        return next((value for value, swatch in self._swatches.items() if swatch.isChecked()), None)

    def set_value(self, value) -> None:
        swatch = self._swatches.get(value)
        if swatch is not None and not swatch.isChecked():
            with QSignalBlocker(swatch):
                swatch.setChecked(True)
            self.ring.follow(swatch)

    def set_enabled(self, enabled: bool) -> None:
        for swatch in self._swatches.values():
            swatch.setEnabled(enabled)
        set_dimmed(self.view, not enabled)


class InputRecorder(ActionButton):
    """A keycap that records the next key or mouse button pressed. Clicking arms it; while armed an
    application-wide filter takes the next key or non-left button and hands `on_record` a
    ("key", virtual key) or ("button", name) pair, and a left click anywhere disarms it. The key is
    read as the low-level hook will report it, so what is recorded is what the hook matches."""

    HINT = "Click, then press it"
    RECORDING_HINT = "Click again to cancel"
    BUTTONS = {
        Qt.MouseButton.RightButton: "right",
        Qt.MouseButton.MiddleButton: "middle",
        Qt.MouseButton.BackButton: "back",
        Qt.MouseButton.ForwardButton: "forward",
    }

    def __init__(self, title: str, on_record, hook_vk, parent: QWidget | None = None, keys_only: bool = False,
                 name: str = "", mono: bool = True) -> None:
        super().__init__(parent)
        # What the recorder is, before what it shows: "Shortcut key, Right Ctrl".
        self.name = name
        self.title = title
        self.on_record = on_record
        self.hook_vk = hook_vk
        # The trigger is a key: a mouse button while it records cancels, as on the Mac.
        self.keys_only = keys_only
        self.armed = False
        self.left_ctrl_held = False
        self.setProperty("vernier", "keycap")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(self._spoken(title))
        self.setMaximumWidth(16777215)
        self.setMinimumHeight(42 if mono else 36)
        row = QBoxLayout(QBoxLayout.Direction.TopToBottom, self)
        self._content = row
        # Vertical margins matter: a layout-owning widget is sized from its layout's own height for
        # width, not from heightForWidth below, so with none the key sat on the top border and the
        # hint's last pixels were cut by the bottom one.
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(10)
        self.key = label(title, "keycap", wrap=True)
        self.key.setFont(theme.mono_font(tokens.TYPE["field_mono"]) if mono else theme.font(tokens.TYPE["body"]))
        self.hint = label(self.HINT, "small", wrap=True)
        for part in (self.key, self.hint):
            part.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.key, 1)
        # Right-aligned as text, never as a layout item: an aligned item is given its size hint's
        # width, which for a wrapping label is about 20 average characters, so "Click, then press
        # the key" wrapped there on Windows' fonts while the box had reserved one line at full width.
        self.hint.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.hint)
        self.clicked.connect(self._toggle)

    def heightForWidth(self, width):
        room = max(1, width - 24)
        return max(self.minimumHeight(), sum(item.heightForWidth(room) for item in (self.key, self.hint)) + 16 + 10)

    def sizeHint(self):
        return QSize(280, self.heightForWidth(280))

    def set_title(self, title: str) -> None:
        """What the keycap says while it is not recording: the trigger recorder shows its key."""
        self.title = title
        self.setAccessibleName(self._spoken(title))
        if not self.armed:
            self.key.setText(title)

    def _spoken(self, title: str) -> str:
        return f"{self.name}, {title}" if self.name else title

    def cancel(self) -> None:
        self.left_ctrl_held = False
        if not self.armed:
            return
        self.armed = False
        QApplication.instance().removeEventFilter(self)
        self.key.setText(self.title)
        set_role(self.key, "keycap")
        self.hint.setText(self.HINT)
        set_role(self, "keycap")

    def _toggle(self) -> None:
        if self.armed:
            self.cancel()
            return
        self.armed = True
        self.key.setText("Press a key or button…")
        set_role(self.key, "keycap-live")
        self.hint.setText(self.RECORDING_HINT)
        set_role(self, "keycap-live")
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if not self.armed:
            return False
        kind = event.type()
        if kind == QEvent.Type.KeyPress:
            vk = self.hook_vk(event.nativeVirtualKey(), event.nativeScanCode())
            if not vk:
                return True
            if vk == LEFT_CTRL_VK:
                # AltGr arrives as a Left Ctrl Windows makes up, then the Right Alt pressed, so a
                # Left Ctrl waits: let go, it is what was pressed; followed by another key, that
                # key is what was pressed (without this wait, AltGr records as Left Ctrl).
                self.left_ctrl_held = True
                return True
            self.cancel()
            self.on_record("key", vk)
            return True
        if kind == QEvent.Type.KeyRelease:
            if self.left_ctrl_held and self.hook_vk(event.nativeVirtualKey(), event.nativeScanCode()) == LEFT_CTRL_VK:
                self.cancel()
                self.on_record("key", LEFT_CTRL_VK)
            return True
        if kind == QEvent.Type.MouseButtonPress:
            name = self.BUTTONS.get(event.button())
            if name is None or self.keys_only:
                # The left button disarms, and the press goes where it was aimed -- except onto
                # this keycap, where it would arm it again.
                inside = isinstance(watched, QWidget) and (watched is self or self.isAncestorOf(watched))
                self.cancel()
                return inside
            self.cancel()
            self.on_record("button", name)
            return True
        return False


class JumpKeyRecorder(ActionButton):
    """Records a modifier chord and keeps its local spelling for one peer."""

    def __init__(self, on_record, on_arm=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.on_record = on_record
        self.setMaximumWidth(16777215)
        self.on_arm = on_arm
        self.value = ""
        self.armed = False
        self.setProperty("vernier", "keycap")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(52)
        row = QVBoxLayout(self)
        row.setContentsMargins(12, 5, 12, 5)
        row.setSpacing(2)
        self.key = label("Choose a key combination", "body", wrap=True)
        self.hint = label("Click, then press a key combination", "small", wrap=True)
        self.key.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.key)
        row.addWidget(self.hint)
        self.clicked.connect(self._toggle)
        self._names = {"ctrl": "Win", "cmd": "Ctrl", "alt": "Alt", "shift": "Shift"}

    def heightForWidth(self, width):
        room = max(1, width - 24)
        return max(52, sum(item.heightForWidth(room) for item in (self.key, self.hint)) + 12)

    def sizeHint(self):
        return QSize(280, self.heightForWidth(280))

    def set_value(self, value: str) -> None:
        self.value = value or ""
        shown = " ".join(self._names.get(part, part.upper() if len(part) == 1 else part.replace("_", " ").title())
                         for part in self.value.split("+")) if self.value else "Choose a key combination"
        self.setAccessibleName(f"Jump straight here key, {shown}")
        if not self.armed:
            self.key.setText(shown)
            set_role(self.key, "keycap" if self.value else "body")

    def cancel(self) -> None:
        if not self.armed:
            return
        self.armed = False
        QApplication.instance().removeEventFilter(self)
        if self.on_arm is not None:
            self.on_arm(False)
        self.set_value(self.value)
        set_role(self.key, "keycap" if self.value else "body")
        self.hint.setText("Click, then press a key combination")
        set_role(self, "keycap")

    def _toggle(self) -> None:
        if self.armed:
            self.cancel()
            return
        self.armed = True
        self.key.setText("Press a key combination…")
        set_role(self.key, "keycap-live")
        self.hint.setText("Click again to cancel")
        set_role(self, "keycap-live")
        QApplication.instance().installEventFilter(self)
        if self.on_arm is not None:
            self.on_arm(True)

    def eventFilter(self, watched, event) -> bool:
        if not self.armed:
            return False
        if event.type() == QEvent.Type.KeyPress:
            modifiers = event.modifiers()
            held = []
            for flag, name in ((Qt.KeyboardModifier.ControlModifier, "cmd"),
                               (Qt.KeyboardModifier.AltModifier, "alt"),
                               (Qt.KeyboardModifier.ShiftModifier, "shift"),
                               (Qt.KeyboardModifier.MetaModifier, "ctrl")):
                if modifiers & flag:
                    held.append(name)
            key = event.text().lower()
            if not key or len(key) != 1 or not key.isprintable() or key == " ":
                value = event.key()
                key = next((f"f{i}" for i in range(1, 25) if value == getattr(Qt.Key, f"Key_F{i}")), "")
                names = {Qt.Key.Key_Backspace: "backspace", Qt.Key.Key_Delete: "delete", Qt.Key.Key_Left: "left",
                         Qt.Key.Key_Right: "right", Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down",
                         Qt.Key.Key_Home: "home", Qt.Key.Key_End: "end", Qt.Key.Key_PageUp: "page_up",
                         Qt.Key.Key_PageDown: "page_down", Qt.Key.Key_Insert: "insert", Qt.Key.Key_Tab: "tab",
                         Qt.Key.Key_Return: "enter", Qt.Key.Key_Enter: "enter", Qt.Key.Key_Escape: "esc",
                         Qt.Key.Key_Space: "space"}
                key = key or names.get(value, "")
            chord = core_ways.jump_chord(held, key)
            if chord:
                self.cancel()
                self.set_value(chord)
                self.on_record(chord)
            return True
        if event.type() == QEvent.Type.KeyRelease:
            return True
        if event.type() == QEvent.Type.MouseButtonPress:
            inside = isinstance(watched, QWidget) and (watched is self or self.isAncestorOf(watched))
            self.cancel()
            return inside
        return False


class PageButton(QPushButton):
    """One entry in the sidebar's page list, with an optional tone dot for a page that
    needs attention."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setProperty("vernier", "page")
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dot = None
        self._rail = False
        self._keyboard_ring = False
        self._page_key = ""
        self.setMinimumHeight(36)
        self.setAccessibleName(text)

    def minimumSizeHint(self):
        return QSize(0, 36)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Left, Qt.Key.Key_Right):
            direction = -1 if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Left) else 1
            self._sidebar.navigate(self._page_key, direction)
            return
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._sidebar.navigate(self._page_key, 0)
            return
        super().keyPressEvent(event)

    def _icon(self, painter):
        x = (self.width() - 18) / 2 if self._rail else 12
        y = (self.height() - 18) / 2
        painter.save()
        painter.translate(x, y)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(theme.colour("ink" if self.isChecked() else "ink_2")), 1.4))
        key = self._page_key
        if key == "overview":
            painter.drawRoundedRect(QRectF(1, 2, 12, 9), 1, 1)
            painter.drawLine(7, 11, 7, 15)
            painter.drawLine(3, 15, 11, 15)
            painter.drawRoundedRect(QRectF(11, 8, 6, 7), 1, 1)
        elif key == "crossing":
            painter.drawLine(1, 6, 16, 6)
            painter.drawLine(12, 2, 16, 6)
            painter.drawLine(12, 10, 16, 6)
            painter.drawLine(16, 13, 1, 13)
            painter.drawLine(5, 9, 1, 13)
            painter.drawLine(5, 17, 1, 13)
        elif key == "keyboard":
            painter.drawRoundedRect(QRectF(0, 3, 18, 12), 2, 2)
            for row in (6, 9):
                for col in (4, 8, 12, 16):
                    painter.drawPoint(col, row)
            painter.drawLine(5, 12, 13, 12)
        elif key == "design":
            painter.drawEllipse(QRectF(2, 2, 14, 14))
            painter.drawLine(9, 2, 9, 16)
            painter.drawLine(2, 9, 16, 9)
        else:
            painter.drawRoundedRect(QRectF(1, 5, 10, 7), 3, 3)
            painter.drawRoundedRect(QRectF(7, 7, 10, 7), 3, 3)
        painter.restore()

    def set_dot(self, tone: str | None) -> None:
        if tone != self._dot:
            self._dot = tone
            self.update()

    def paintEvent(self, event) -> None:
        native = QStylePainter(self)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        if self._rail:
            option.text = ""
        native.drawControl(QStyle.ControlElement.CE_PushButton, option)
        native.end()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._icon(painter)
        if self.isChecked():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.colour("signal")))
            painter.drawRect(QRectF(0, 7, 2, self.height() - 14))
        if self._keyboard_ring and self.isChecked() and self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(theme.colour("signal")), 2))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(2, 2, -2, -2), 4, 4)
        if self._dot is None:
            return
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.colour(self._dot)))
        size = 6
        painter.drawEllipse(QRectF(self.width() - size - 10, (self.height() - size) / 2, size, size))


class Sidebar(QWidget):
    """The page list, with the link state pinned above it. `on_select(key)` fires on a
    click; the window drives `select`/`set_link`/`set_dots` the rest of the time."""

    def __init__(self, pages, on_select, foot: str = "", on_foot=None, foot_tip: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("vernier", "sidebar")
        self._on_select = on_select
        self._selected = ""
        keyboard_navigation()
        QApplication.instance().installEventFilter(self)
        QApplication.instance().focusChanged.connect(self._focus_changed)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        link = QWidget()
        link.setProperty("vernier", "plain")
        self._link_layout = QHBoxLayout(link)
        self._link_layout.setContentsMargins(16, 16, 16, 16)
        self._link_layout.setSpacing(8)
        self.link_led = Led()
        self._link_layout.addWidget(self.link_led, 0, Qt.AlignmentFlag.AlignVCenter)
        self.link_word = label("", "sidebar-link")
        self._link_layout.addWidget(self.link_word, 1)
        outer.addWidget(link)
        outer.addWidget(rule(vertical=False))

        list_layout = QVBoxLayout()
        self._list_layout = list_layout
        list_layout.setContentsMargins(8, 8, 8, 8)
        list_layout.setSpacing(2)
        self._buttons: dict = {}
        for key, name, _purpose in pages:
            button = PageButton(name)
            button._sidebar = self
            button._page_key = key
            button.setToolTip(name)
            button.clicked.connect(lambda _checked=False, k=key: self._pointer_select(k))
            list_layout.addWidget(button)
            self._buttons[key] = button
        list_layout.addStretch(1)
        outer.addLayout(list_layout, 1)
        if foot:
            # The version and the way back to Beamer's page: one quiet line, never a control
            # anyone has to look past to reach a setting.
            self.foot = QPushButton(foot)
            self.foot.setProperty("vernier", "foot")
            self.foot.setCursor(Qt.CursorShape.PointingHandCursor)
            self.foot.setToolTip(foot_tip or "Open in your browser")
            self.foot.setAccessibleName(foot.replace("\n", ", "))
            self._foot_text = foot
            if on_foot is not None:
                self.foot.clicked.connect(on_foot)
            outer.addWidget(self.foot)

    @property
    def keyboard_mode(self) -> bool:
        return keyboard_navigation().active

    @keyboard_mode.setter
    def keyboard_mode(self, active: bool) -> None:
        keyboard_navigation().set(active)

    def select(self, key: str) -> None:
        self._selected = key
        button = self._buttons.get(key)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        focused = QApplication.focusWidget()
        if button is not None and self.keyboard_mode and focused in self._buttons.values():
            button.setFocus(Qt.FocusReason.ShortcutFocusReason)
        elif not self.keyboard_mode:
            for other in self._buttons.values():
                other.clearFocus()
        self._update_rings()

    def _update_rings(self):
        for key, button in self._buttons.items():
            button._keyboard_ring = self.keyboard_mode and key == self._selected
            button.update()
        for tile in self.window().findChildren(_Tile):
            tile.outline.update()

    def _focus_changed(self, old, new):
        if new in self._buttons.values():
            selected = self._buttons.get(self._selected)
            if not self.keyboard_mode:
                new.clearFocus()
            elif selected is not None and new is not selected:
                selected.setFocus(Qt.FocusReason.TabFocusReason)
        self._update_rings()

    def _pointer_select(self, key):
        self.keyboard_mode = False
        self._on_select(key)
        self.select(key)

    def navigate(self, key, direction):
        keys = list(self._buttons)
        self.keyboard_mode = True
        target = keys[(keys.index(key) + direction) % len(keys)]
        self._on_select(target)
        self._buttons[target].setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.select(target)

    def eventFilter(self, watched, event):
        if isinstance(watched, QWidget) and watched.window() is self.window():
            if event.type() == QEvent.Type.MouseButtonPress:
                self.keyboard_mode = False
                for button in self._buttons.values():
                    button.clearFocus()
                self._update_rings()
            elif event.type() == QEvent.Type.KeyPress and is_navigation_key(event.key(), event.modifiers()):
                self.keyboard_mode = True
                self._update_rings()
        return False

    def set_window_width(self, width):
        rail = width < 640
        self.setFixedWidth(56 if rail else 144 if width < 800 else 176)
        self.link_word.setVisible(not rail)
        self._link_layout.setContentsMargins(24 if rail else 16, 4 if rail else 16,
                                            24 if rail else 16, 4 if rail else 16)
        self._list_layout.setContentsMargins(6 if rail else 8, 3 if rail else 8,
                                            6 if rail else 8, 3 if rail else 8)
        for button in self._buttons.values():
            button._rail = rail
            button.update()
        if hasattr(self, "foot"):
            self.foot.setProperty("rail", rail)
            self.foot.setText("" if rail else self._foot_text)
            self.foot.setFixedHeight(20 if rail else 58)
            if rail:
                from pathlib import Path
                from PySide6.QtGui import QIcon
                self.foot.setIcon(QIcon(str(Path(__file__).resolve().parent / "Beamer.ico")))
            else:
                from PySide6.QtGui import QIcon
                self.foot.setIcon(QIcon())
            theme.repolish(self.foot)

    def set_link(self, tone: str, word: str) -> None:
        self.link_led.set_tone(tone)
        if self.link_word.text() != word:
            self.link_word.setText(word)
        self.link_led.setToolTip(word)

    def set_dots(self, marks: dict) -> None:
        for key, button in self._buttons.items():
            button.set_dot(marks.get(key))


class Ruler(QSlider):
    """A horizontal ruler, named for screen readers, stepping 10 on the arrow keys and 1 with Alt
    held, as the Mac's steps 1 with Option."""

    STEP = 10

    def __init__(self, name: str, maximum: int, minimum: int = 0, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setRange(minimum, maximum)
        self.setSingleStep(self.STEP)
        self.setPageStep(self.STEP * 5)
        self.setAccessibleName(name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def keyPressEvent(self, event) -> None:
        steps = {Qt.Key.Key_Left: -1, Qt.Key.Key_Down: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Up: 1}
        if event.modifiers() & Qt.KeyboardModifier.AltModifier and event.key() in steps:
            self.setValue(self.value() + steps[event.key()])
            return
        super().keyPressEvent(event)


class Switch(QCheckBox):
    """A QCheckBox drawn as Vernier's switch: label left, track right."""

    TRACK = QSize(34, 18)

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(30)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def sizeHint(self) -> QSize:
        text = self.fontMetrics().horizontalAdvance(self.text())
        return QSize(text + 12 + self.TRACK.width() + 4, 30)

    def minimumSizeHint(self) -> QSize:
        return QSize(44, 30)

    def heightForWidth(self, width):
        bounds = self.fontMetrics().boundingRect(
            QRect(0, 0, max(1, width - 50), 10000), int(Qt.TextFlag.TextWordWrap), self.text())
        return max(30, bounds.height())

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.45)
        on = self.isChecked()
        track = QRectF(
            self.width() - self.TRACK.width() - 2.5,
            (self.height() - self.TRACK.height()) / 2 + 0.5,
            self.TRACK.width(),
            self.TRACK.height() - 1,
        )
        if focus_ring_visible(self):
            painter.setPen(QColor(theme.colour("signal")))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(track.adjusted(-2, -2, 2, 2), 11, 11)
        painter.setPen(QColor(theme.colour("signal" if on else "edge")))
        painter.setBrush(QColor(theme.colour("well")))
        painter.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.colour("signal" if on else "ink_3")))
        knob = 12.0
        x = track.right() - knob - 2.5 if on else track.left() + 2.5
        painter.drawEllipse(QRectF(x, track.center().y() - knob / 2, knob, knob))
        painter.setPen(QColor(theme.colour("ink")))
        painter.setFont(self.font())
        text_rect = QRectF(0, 0, track.left() - 12, self.height())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft |
                         Qt.TextFlag.TextWordWrap, self.text())
