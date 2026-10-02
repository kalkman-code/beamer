"""The Vernier components the receiver window draws that a stylesheet cannot: module, eyebrow,
LED, the 60-segment countdown and the switch."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFontMetrics, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

import motion
import theme
import tokens
from core import ways as core_ways

LEFT_CTRL_VK = 0xA2
# The smallest a Segmented cell or small button is drawn, so each is a fair target.
MIN_TARGET = 28


def label(text: str, role: str, wrap: bool = False) -> QLabel:
    item = QLabel(text)
    item.setProperty("vernier", role)
    item.setWordWrap(wrap)
    return item


def set_role(widget: QWidget, role: str) -> None:
    if widget.property("vernier") != role:
        widget.setProperty("vernier", role)
        theme.repolish(widget)


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


class Module(QFrame):
    """One `panel` cell in the rack, eyebrow first."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("vernier", "module")
        self.body = QVBoxLayout(self)
        vertical, horizontal = (round(v) for v in tokens.MODULE_PADDING)
        self.body.setContentsMargins(horizontal, vertical, horizontal, horizontal)
        self.body.setSpacing(12)
        self.eyebrow = Eyebrow(title)
        self.body.addWidget(self.eyebrow)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)


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


class Choice:
    """A row of exclusive Vernier choice buttons, `columns` wide. Pick a divisor of
    `len(items)` so a wrap never orphans one. `on_change(value)` fires only on a real
    click -- never on `set_value`, so refreshing from an incoming setting cannot loop back."""

    def __init__(self, items, columns: int, current: str, on_change=None, parent: QWidget | None = None) -> None:
        self.view = QWidget(parent)
        self.view.setProperty("vernier", "plain")
        grid = QGridLayout(self.view)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)
        self._buttons: dict = {}
        for index, (value, text) in enumerate(items):
            button = QPushButton(text)
            button.setProperty("vernier", "choice")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumHeight(MIN_TARGET)
            button.setChecked(value == current)
            if on_change is not None:
                button.clicked.connect(lambda _checked=False, v=value: on_change(v))
            grid.addWidget(button, index // columns, index % columns)
            self._buttons[value] = button

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

    def set_names(self, prefix: str) -> None:
        """Each cell's accessible name leads with what the row chooses: "Where your Mac is, Left"."""
        for button in self._buttons.values():
            button.setAccessibleName(f"{prefix}, {button.text()}")


class _Tile(QFrame):
    """One tile of ChoiceTiles: the whole tile is the control, by click or by Space from Tab."""

    def __init__(self, choose, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._choose = choose
        self.setProperty("vernier", "tile")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._choose()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._choose()
            return
        super().keyPressEvent(event)


class ChoiceTiles:
    """One of several, as tiles side by side: each a live preview over its name and a line saying
    what it is. The chosen tile is ringed by the TileGroups it sits in, in ink, outside the tile, so
    the signal focus border inside it still shows. `on_change(value)` fires only on a real choice,
    never on `set_value`."""

    def __init__(self, items, current, on_change=None, parent: QWidget | None = None) -> None:
        self.on_change = on_change
        self.value = None
        self.view = QWidget(parent)
        self.view.setProperty("vernier", "plain")
        row = QHBoxLayout(self.view)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self._tiles: dict = {}
        self._titles: dict = {}
        for value, title, detail, preview in items:
            tile = _Tile(lambda v=value: self._chosen(v))
            self._titles[value] = title
            tile.setAccessibleName(title)
            tile.setAccessibleDescription(detail)
            column = QVBoxLayout(tile)
            column.setContentsMargins(8, 8, 8, 12)
            column.setSpacing(6)
            column.addWidget(preview)
            column.addSpacing(4)
            column.addWidget(label(title, "tile-name"))
            column.addWidget(label(detail, "small", wrap=True))
            column.addStretch(1)
            row.addWidget(tile, 1)
            self._tiles[value] = tile
        self.set_value(current)

    def set_value(self, value) -> None:
        self.value = value
        for candidate, tile in self._tiles.items():
            set_role(tile, "tile-on" if candidate == value else "tile")
            title = self._titles[candidate]
            tile.setAccessibleName(f"{title}, selected" if candidate == value else title)

    def tile(self, value):
        return self._tiles.get(value)

    def set_enabled(self, enabled: bool) -> None:
        # Faded whole, previews included, as the Mac's tiles are.
        fade = QGraphicsOpacityEffect(self.view)
        fade.setOpacity(1.0 if enabled else 0.45)
        self.view.setGraphicsEffect(None if enabled else fade)
        for tile in self._tiles.values():
            tile.setEnabled(enabled)

    def _chosen(self, value) -> None:
        if value == self.value:
            return
        self.set_value(value)
        if self.on_change is not None:
            self.on_change(value)


class _Swatch(QPushButton):
    """A colour chip over its name. The name wraps onto a second line rather than widen the grid:
    at the window's minimum width five chips a row have about 65px each."""

    CHIP = 24
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
        if self.hasFocus():
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


class TileGroups:
    """ChoiceTiles in titled groups with one value across all of them: `groups` is (title, items)
    with ChoiceTiles' items. `on_change(value)` fires only on a real choice. One ring marks the
    chosen tile and slides to the next, across groups."""

    RING_OUTSET = 3

    def __init__(self, groups, current, on_change=None, parent: QWidget | None = None) -> None:
        self.on_change = on_change
        self.value = current
        self.view = QWidget(parent)
        self.view.setProperty("vernier", "plain")
        column = QVBoxLayout(self.view)
        # Room for the ring outside the outermost tiles.
        room = self.RING_OUTSET + 1
        column.setContentsMargins(room, room, room, room)
        column.setSpacing(6)
        self.groups: dict = {}
        for index, (title, items) in enumerate(groups):
            if index:
                column.addSpacing(8)
            column.addWidget(label(title, "key"))
            tiles = ChoiceTiles(items, current, on_change=self._chosen)
            column.addWidget(tiles.view)
            self.groups[title] = tiles
        outset = self.RING_OUTSET
        self.ring = motion.Ring(
            self.view,
            lambda tile: QRectF(tile.rect()).adjusted(-outset + 0.5, -outset + 0.5, outset - 0.5, outset - 0.5),
            lambda: theme.colour("ink"),
            tokens.RADIUS["button"] + outset,
        )
        self.ring.follow(self.tile(current))

    def tile(self, value):
        return next((tile for tiles in self.groups.values() if (tile := tiles.tile(value)) is not None), None)

    def set_value(self, value) -> None:
        self.value = value
        for tiles in self.groups.values():
            tiles.set_value(value)
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
        grid = QGridLayout(self.view)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(2)
        self._group = QButtonGroup(self.view)
        self._group.setExclusive(True)
        self._swatches: dict = {}
        widest = max(len(items) for _title, items in groups)
        for row, (title, items) in enumerate(groups):
            heading = label(title, "key")
            if row:
                heading.setContentsMargins(0, 8, 0, 0)
            grid.addWidget(heading, row * 2, 0, 1, widest)
            for index, (value, name, colours) in enumerate(items):
                swatch = _Swatch(colours, name)
                swatch.setAutoExclusive(False)
                self._group.addButton(swatch)
                swatch.setChecked(value == current)
                swatch.toggled.connect(lambda on, s=swatch: on and self.ring.follow(s))
                if on_change is not None:
                    swatch.clicked.connect(lambda _checked=False, v=value: on_change(v))
                grid.addWidget(swatch, row * 2 + 1, index)
                self._swatches[value] = swatch
        for index in range(widest):
            grid.setColumnStretch(index, 1)
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
            swatch.setChecked(True)

    def set_enabled(self, enabled: bool) -> None:
        for swatch in self._swatches.values():
            swatch.setEnabled(enabled)


class InputRecorder(QPushButton):
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
        self.setFixedHeight(42 if mono else 36)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 12, 0)
        row.setSpacing(10)
        self.key = label(title, "keycap")
        self.key.setFont(theme.mono_font(tokens.TYPE["field_mono"]) if mono else theme.font(tokens.TYPE["body"]))
        self.hint = label(self.HINT, "small")
        for part in (self.key, self.hint):
            part.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.key, 1)
        row.addWidget(self.hint, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.clicked.connect(self._toggle)

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


class JumpKeyRecorder(QPushButton):
    """Records a modifier chord and keeps its local spelling for one peer."""

    def __init__(self, on_record, on_arm=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.on_record = on_record
        self.on_arm = on_arm
        self.value = ""
        self.armed = False
        self.setProperty("vernier", "keycap")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(42)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 12, 0)
        row.setSpacing(10)
        self.key = label("Choose a key combination", "keycap")
        self.hint = label("Click, then press a key combination", "small")
        self.key.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.key, 1)
        row.addWidget(self.hint, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.clicked.connect(self._toggle)
        self._names = {"ctrl": "Win", "cmd": "Ctrl", "alt": "Alt", "shift": "Shift"}

    def set_value(self, value: str) -> None:
        self.value = value or ""
        shown = " ".join(self._names.get(part, part.upper() if len(part) == 1 else part.replace("_", " ").title())
                         for part in self.value.split("+")) if self.value else "Choose a key combination"
        self.setAccessibleName(f"Jump straight here key, {shown}")
        if not self.armed:
            self.key.setText(shown)

    def cancel(self) -> None:
        if not self.armed:
            return
        self.armed = False
        QApplication.instance().removeEventFilter(self)
        if self.on_arm is not None:
            self.on_arm(False)
        self.set_value(self.value)
        set_role(self.key, "keycap")
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

    def set_dot(self, tone: str | None) -> None:
        if tone != self._dot:
            self._dot = tone
            self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._dot is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
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
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        link = QWidget()
        link.setProperty("vernier", "plain")
        link_layout = QHBoxLayout(link)
        link_layout.setContentsMargins(16, 16, 16, 16)
        link_layout.setSpacing(8)
        self.link_led = Led()
        link_layout.addWidget(self.link_led, 0, Qt.AlignmentFlag.AlignVCenter)
        self.link_word = label("", "sidebar-link")
        link_layout.addWidget(self.link_word, 1)
        outer.addWidget(link)
        outer.addWidget(rule(vertical=False))

        list_layout = QVBoxLayout()
        list_layout.setContentsMargins(8, 8, 8, 8)
        list_layout.setSpacing(2)
        self._buttons: dict = {}
        for key, name, _purpose in pages:
            button = PageButton(name)
            button.clicked.connect(lambda _checked=False, k=key: on_select(k))
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
            if on_foot is not None:
                self.foot.clicked.connect(on_foot)
            outer.addWidget(self.foot)

    def select(self, key: str) -> None:
        button = self._buttons.get(key)
        if button is not None and not button.isChecked():
            button.setChecked(True)

    def set_link(self, tone: str, word: str) -> None:
        self.link_led.set_tone(tone)
        if self.link_word.text() != word:
            self.link_word.setText(word)

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
        self.setMinimumHeight(24)

    def sizeHint(self) -> QSize:
        text = self.fontMetrics().horizontalAdvance(self.text())
        return QSize(text + 12 + self.TRACK.width() + 4, max(24, self.TRACK.height() + 6))

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
        if self.hasFocus():
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
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
