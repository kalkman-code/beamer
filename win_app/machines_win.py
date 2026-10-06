"""Overview's two pieces for paired machines: the list with each one's state and switches, and the
pairing sheet. Plain widgets that hold no settings and make no calls of their own: the window feeds
them and gives each the one function it calls back."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QPointF, QRectF, QRegularExpression, QSize, Qt
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPen, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QButtonGroup,
    QBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QLayout,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import peers_view
import theme
import widgets
from core import feature_flags, pairing, peerlist

KEEP_NOTE = "It stays paired on that machine until you remove this one there too."
EMPTY = "No machines paired yet."
OLDER = "Older Beamer"
CODE_DIGITS = 6
QR_SCALE = 5
CODE_CELL_GAP = 6
CODE_GROUP_GAP = 12
CODE_CELL_PAD_X = 4
CODE_CELL_PAD_Y = 6


def _plain() -> QWidget:
    item = QWidget()
    item.setProperty("vernier", "plain")
    return item


def _button(text: str, role: str = "") -> QPushButton:
    button = QPushButton(text)
    button.setMinimumHeight(widgets.MIN_TARGET)
    if role:
        button.setProperty("vernier", role)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class _PairCodeCells:
    """The six digits, each centred by its measured ink bounds inside its own cell."""

    def _cell_rectangles(self) -> list[QRectF]:
        metrics = QFontMetricsF(self.font())
        width = max(34.0, metrics.horizontalAdvance("0") + CODE_CELL_PAD_X * 2)
        height = min(max(40.0, metrics.height() + CODE_CELL_PAD_Y * 2), float(self.height()))
        advances = [width + CODE_CELL_GAP] * 6
        advances[2] += CODE_GROUP_GAP
        group_width = sum(advances) - CODE_CELL_GAP
        x = (self.width() - group_width) / 2
        y = (self.height() - height) / 2
        cells = []
        for index in range(6):
            cells.append(QRectF(x, y, width, height))
            x += advances[index]
        return cells

    def digit_cells(self) -> list[QRectF]:
        return [cell.adjusted(1, 1, -1, -1) for cell in self._cell_rectangles()]

    def _paint_digits(self, painter: QPainter, text: str, selected: tuple[int, int] = (0, 0), focused: bool = False) -> None:
        metrics = QFontMetricsF(self.font())
        digits = "".join(character for character in text if character.isdigit())[:6]
        cells = self._cell_rectangles()
        inner = self.digit_cells()
        start, length = selected
        for index, (cell, ink_box) in enumerate(zip(cells, inner)):
            painter.setBrush(QColor(theme.colour("ground")))
            painter.setPen(QPen(QColor(theme.colour("signal" if focused and index == min(5, len(digits)) else "edge")), 1))
            painter.drawRoundedRect(cell, 4, 4)
            if start <= index < start + length:
                painter.fillRect(ink_box, QColor(theme.colour("signal_dim")))
            if index >= len(digits):
                continue
            character = digits[index]
            bounds = metrics.tightBoundingRect(character)
            centre = ink_box.center()
            baseline = QPointF(centre.x() - bounds.center().x(), centre.y() - bounds.center().y())
            painter.setPen(QColor(theme.colour("ink")))
            painter.drawText(baseline, character)
        if focused and length == 0:
            index = min(5, max(0, self.cursorPosition()))
            cell = cells[index]
            cursor_x = cell.left() + (cell.width() + metrics.horizontalAdvance("0")) / 2
            if index < len(digits):
                cursor_x = cell.center().x() - metrics.horizontalAdvance(digits[index]) / 2
            painter.setPen(QPen(QColor(theme.colour("signal")), 1))
            painter.drawLine(QPointF(cursor_x, cell.top() + 8), QPointF(cursor_x, cell.bottom() - 8))


class PairCodeEdit(QLineEdit, _PairCodeCells):
    """A QLineEdit with the usual paste, selection and deletion behaviour, painted as six cells."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setProperty("vernier", "pair-code")
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setTextMargins(0, 0, 0, 0)
        self._mouse_anchor = 0
        self._mouse_selecting = False
        self._double_clicked = False

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        self._paint_digits(painter, self.text(), (self.selectionStart(), len(self.selectedText())), self.hasFocus())

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        old_cursor = self.cursorPosition()
        if self.hasSelectedText():
            start = self.selectionStart()
            end = start + len(self.selectedText())
            anchor = end if old_cursor == start else start
        else:
            anchor = old_cursor
        super().mousePressEvent(event)
        target = self._cursor_at(event.position().x())
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self._mouse_anchor = anchor
            self._select_to(target)
        else:
            self._mouse_anchor = target
            self.setCursorPosition(target)
        self._mouse_selecting = True

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        if self._mouse_selecting and event.buttons() & Qt.MouseButton.LeftButton:
            self._select_to(self._cursor_at(event.position().x()))

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            if self._double_clicked:
                self._double_clicked = False
            else:
                self._select_to(self._cursor_at(event.position().x()))
            self._mouse_selecting = False

    def mouseDoubleClickEvent(self, event) -> None:
        super().mouseDoubleClickEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            slot = next((index for index, cell in enumerate(self._cell_rectangles())
                         if cell.left() <= event.position().x() <= cell.right()), CODE_DIGITS - 1)
            self._mouse_anchor = slot
            self.setSelection(slot, 1)
            self._double_clicked = True

    def _cursor_at(self, x: float) -> int:
        for index, cell in enumerate(self._cell_rectangles()):
            if cell.left() <= x <= cell.right():
                return index + (x >= cell.center().x())
            if x < cell.left():
                return index
        return CODE_DIGITS

    def _select_to(self, position: int) -> None:
        if position == self._mouse_anchor:
            self.setCursorPosition(position)
        else:
            self.setSelection(self._mouse_anchor, position - self._mouse_anchor)


class PairCodeLabel(QLabel, _PairCodeCells):
    """Read-only pairing digits in the same six-cell layout as the entry control."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAccessibleName("Pairing code")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._mouse_anchor = 0
        self._cursor_position = 0
        self._mouse_selecting = False
        self._double_clicked = False

    def sizeHint(self) -> QSize:
        metrics = QFontMetricsF(self.font())
        height = max(40.0, metrics.height() + CODE_CELL_PAD_Y * 2) + 2
        return QSize(6 * 42 + 5 * CODE_CELL_GAP + CODE_GROUP_GAP, round(height))

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        selected = (0, 0)
        if self.hasSelectedText():
            start = self.selectionStart()
            end = start + len(self.selectedText())
            positions = (0, 1, 2, 4, 5, 6)
            indexes = [index for index, position in enumerate(positions) if start <= position < end]
            if indexes:
                selected = (indexes[0], len(indexes))
        self._paint_digits(painter, self.text(), selected)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        if self.hasSelectedText():
            start = self.selectionStart()
            end = start + len(self.selectedText())
            anchor = end if self._cursor_position == start else start
        else:
            anchor = self._cursor_position
        super().mousePressEvent(event)
        target = self._text_position_at(event.position().x())
        self._mouse_anchor = (anchor if event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                              else target)
        self._cursor_position = target
        self._select_to(target)
        self._mouse_selecting = True

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        if self._mouse_selecting and event.buttons() & Qt.MouseButton.LeftButton:
            self._select_to(self._text_position_at(event.position().x()))

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            if self._double_clicked:
                self._double_clicked = False
            else:
                target = self._text_position_at(event.position().x())
                self._cursor_position = target
                self._select_to(target)
            self._mouse_selecting = False

    def mouseDoubleClickEvent(self, event) -> None:
        super().mouseDoubleClickEvent(event)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        slot = min(5, self._slot_at(event.position().x()))
        positions = (0, 1, 2, 4, 5, 6)
        self._cursor_position = positions[slot] + 1
        self._mouse_anchor = positions[slot]
        self.setSelection(self._mouse_anchor, 1)
        self._double_clicked = True

    def _slot_at(self, x: float) -> int:
        for index, cell in enumerate(self._cell_rectangles()):
            if x <= cell.center().x():
                return index
        return CODE_DIGITS

    def _text_position_at(self, x: float) -> int:
        digit_positions = (0, 1, 2, 4, 5, 6)
        for index, cell in enumerate(self._cell_rectangles()):
            if cell.left() <= x <= cell.right():
                return digit_positions[index] + (x >= cell.center().x())
            if x < cell.left():
                return digit_positions[index]
        return len(self.text())

    def _select_to(self, target: int) -> None:
        if target == self._mouse_anchor:
            self.setSelection(self._mouse_anchor, 0)
        else:
            self.setSelection(self._mouse_anchor, target - self._mouse_anchor)


class PeerRow(QWidget):
    """One paired machine: its name and state, use switch, direction disclosure and Remove."""

    def __init__(self, token: str, on_send: Callable, on_allow: Callable, on_in_use: Callable, on_remove: Callable) -> None:
        super().__init__()
        self.setProperty("vernier", "plain")
        self.token = token
        self._phone = False
        self._on_remove = on_remove
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 8)
        column.setSpacing(6)
        self.divider = widgets.rule(False)
        column.addWidget(self.divider)
        column.addSpacing(4)
        head = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self.head = head
        head.setSpacing(10)
        self.led = widgets.Led()
        name_line = QWidget()
        name_line.setProperty("vernier", "plain")
        name_layout = QHBoxLayout(name_line)
        name_layout.setContentsMargins(0, 0, 0, 0)
        name_layout.setSpacing(10)
        name_layout.addWidget(self.led, 0, Qt.AlignmentFlag.AlignVCenter)
        self.name = widgets.label("", "tile-name", wrap=True)
        name_layout.addWidget(self.name, 1)
        head.addWidget(name_line, 1)
        self.word = widgets.label("", "readout")
        head.addWidget(self.word)
        column.addLayout(head)
        self.where = widgets.label("", "note-quiet", wrap=True)
        column.addWidget(self.where)
        self.detail = widgets.label("", "note", wrap=True)
        column.addWidget(self.detail)
        self.in_use = widgets.Switch("In use")
        self.in_use.toggled.connect(lambda on: on_in_use(self.token, on))
        self.in_use.setToolTip("Off on this PC: no input or settings link to this machine. Pairing, directions, ways and jump key are kept. Turn it back on here to reconnect.")
        self.send = widgets.Switch("This PC drives it")
        self.send.setToolTip("Input moves only when this PC enables this switch and the other machine enables It drives this PC.")
        self.allow = widgets.Switch("It drives this PC")
        self.allow.setToolTip("Input moves only when the other machine enables This machine drives it and this PC enables this switch.")
        for switch in (self.send, self.allow):
            switch.setFont(theme.font(theme.TYPE["body"]))
        self.send.toggled.connect(lambda on: on_send(self.token, on))
        self.allow.toggled.connect(lambda on: on_allow(self.token, on))
        self.remove_button = _button("Remove", "small")
        self.remove_button.setMinimumHeight(widgets.MIN_TARGET)
        self.remove_button.clicked.connect(lambda: self._ask(True))
        controls = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        controls.setSpacing(16)
        directions = QVBoxLayout()
        directions.setContentsMargins(0, 0, 0, 0)
        directions.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        directions.setSpacing(2)
        directions.addWidget(self.send)
        directions.addWidget(self.allow)
        self.direction_group = QWidget()
        self.direction_group.setLayout(directions)
        self.direction_group.setVisible(False)
        self.directions = _button("Directions", "small")
        self.directions.setCheckable(True)
        self.directions.toggled.connect(self.direction_group.setVisible)
        self.directions.toggled.connect(lambda on: self.directions.setText("Hide directions" if on else "Directions"))
        for switch in (self.send, self.allow):
            switch.setMaximumWidth(300)
        controls.addStretch(1)
        controls.addWidget(self.in_use)
        controls.addWidget(self.directions)
        controls.addWidget(self.remove_button, 0, Qt.AlignmentFlag.AlignBottom)
        self.controls = controls
        column.addLayout(controls)
        column.addWidget(self.direction_group)
        self.confirm = _plain()
        sure = QVBoxLayout(self.confirm)
        sure.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        sure.setContentsMargins(0, 0, 0, 0)
        sure.setSpacing(8)
        self.confirm_text = widgets.label("", "note", wrap=True)
        sure.addWidget(self.confirm_text)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.really = _button("Remove", "small")
        self.really.setMinimumHeight(widgets.MIN_TARGET)
        self.really.clicked.connect(lambda: self._on_remove(self.token))
        self.keep = _button("Keep", "small")
        self.keep.setMinimumHeight(widgets.MIN_TARGET)
        self.keep.clicked.connect(lambda: self._ask(False))
        buttons.addWidget(self.really)
        buttons.addWidget(self.keep)
        buttons.addStretch(1)
        sure.addLayout(buttons)
        self.confirm.setVisible(False)
        column.addWidget(self.confirm)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        wide = self.width() >= 400
        direction = QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom
        self.head.setDirection(direction)
        self.head.setAlignment(self.word, Qt.AlignmentFlag.AlignRight if wide else Qt.AlignmentFlag.AlignLeft)
        self.controls.setDirection(direction)
        for control in (self.in_use, self.directions, self.remove_button):
            self.controls.setAlignment(control, Qt.AlignmentFlag.AlignRight if wide else Qt.AlignmentFlag.AlignLeft)

    def _ask(self, asking: bool) -> None:
        self.confirm.setVisible(asking)
        self.remove_button.setVisible(not asking)
        self.in_use.setVisible(not asking)
        self.directions.setVisible(not asking and not self._phone)
        self.direction_group.setVisible(not asking and not self._phone and self.directions.isChecked())

    def refresh(self, entry: dict, label: str, state, where: str) -> None:
        self._phone = entry.get("port") == 0
        asking = self.confirm.isVisibleTo(self)
        self.directions.setVisible(not self._phone and not asking)
        self.direction_group.setVisible(not self._phone and not asking and self.directions.isChecked())
        self.led.set_tone(state.tone)
        for item, text in ((self.name, label), (self.word, state.word), (self.where, where), (self.detail, state.detail),
                           (self.confirm_text, f"Remove {label}? {KEEP_NOTE}")):
            if item.text() != text:
                item.setText(text)
        for switch, value in ((self.in_use, entry.get("in_use", True) is True),
                              (self.send, bool(entry.get("send"))), (self.allow, bool(entry.get("allow_drive")))):
            if switch.isChecked() != value:
                switch.blockSignals(True)
                switch.setChecked(value)
                switch.blockSignals(False)
        self.word.setAccessibleName(f"{label}: {state.word}")
        self.remove_button.setAccessibleName(f"Remove {label}")
        self.in_use.setToolTip(f"Off on this PC: no input or settings link to {label}. Pairing, directions, ways and jump key are kept. Turn it back on here to reconnect.")
        if self._phone:
            self.in_use.setToolTip(f"Off on this PC: {label} cannot connect or drive it. Turn it back on here to reconnect.")
        self.send.setToolTip(f"Input moves only when this PC enables this switch and {label} enables It drives this PC.")
        self.allow.setToolTip(f"Input moves only when {label} enables This PC drives it and this PC enables this switch.")


class MachinesModule(widgets.Module):
    """The paired machines, one row each, and the button that opens the pairing sheet."""

    def __init__(self, on_send: Callable, on_allow: Callable, on_in_use: Callable, on_remove: Callable, on_pair: Callable) -> None:
        super().__init__("Machines")
        self._callbacks = (on_send, on_allow, on_in_use, on_remove)
        self.rows: dict = {}
        self.rows_layout = QVBoxLayout()
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(0)
        self.pair_button = _button("Pair a machine", "primary")
        self.pair_button.clicked.connect(on_pair)
        self.body.addWidget(self.pair_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.body.addLayout(self.rows_layout)
        self.empty = widgets.label(EMPTY, "note", wrap=True)
        self.body.addWidget(self.empty)

    def set_peers(self, items: list) -> None:
        """`items`: (entry, label, state, where) for each paired machine, in the order to show them."""
        tokens = [entry["token"] for entry, *_ in items]
        for token in [held for held in self.rows if held not in tokens]:
            row = self.rows.pop(token)
            self.rows_layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        for index, (entry, label, state, where) in enumerate(items):
            row = self.rows.get(entry["token"])
            if row is None:
                row = self.rows[entry["token"]] = PeerRow(entry["token"], *self._callbacks)
                self.rows_layout.insertWidget(index, row)
            elif self.rows_layout.indexOf(row) != index:
                self.rows_layout.removeWidget(row)
                self.rows_layout.insertWidget(index, row)
            row.refresh(entry, label, state, where)
            row.divider.setVisible(index > 0)
        self.empty.setVisible(not items)

    def set_pairing_open(self, opened: bool) -> None:
        text = "Cancel" if opened else "Pair a machine"
        if self.pair_button.text() != text:
            self.pair_button.setText(text)
            widgets.set_role(self.pair_button, "" if opened else "primary")


class QrView(QWidget):
    """The pairing QR: dark modules on a light ground whatever the theme, since a phone reads
    contrast, not colour."""

    def __init__(self) -> None:
        super().__init__()
        self._rows: list = []
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("QR code for the pairing code")

    def set_rows(self, rows: list) -> None:
        if rows != self._rows:
            self._rows = rows
            side = len(rows) * QR_SCALE
            self.setFixedSize(side, side)
            self.update()

    def sizeHint(self) -> QSize:
        side = len(self._rows) * QR_SCALE
        return QSize(side, side)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#000000"))
        for y, row in enumerate(self._rows):
            for x, dark in enumerate(row):
                if dark:
                    painter.drawRect(QRectF(x * QR_SCALE, y * QR_SCALE, QR_SCALE, QR_SCALE))


class PairingSheet(widgets.Module):
    """Both halves of pairing: showing a code on this PC, and entering one from another machine."""

    def __init__(self, on_show: Callable, on_cancel: Callable, on_pair: Callable, on_pick: Callable) -> None:
        super().__init__("Pair a machine")
        self._on_pair = on_pair
        self._on_pick = on_pick
        self._machines: list = []
        self._listed: tuple = ()
        self._working = False
        self.body.addWidget(widgets.label("Show a code on this PC", "tile-name"))
        self.show_button = _button("Show a code", "primary")
        self.show_button.clicked.connect(on_show)
        self.cancel_button = _button("Cancel", "small")
        self.cancel_button.setMinimumHeight(widgets.MIN_TARGET)
        self.cancel_button.clicked.connect(on_cancel)
        self.cancel_button.setVisible(False)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self.show_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        self.body.addLayout(buttons)
        self.host_note = widgets.label("", "note", wrap=True)
        self.host_note.setVisible(False)
        self.body.addWidget(self.host_note)
        self.code_box = _plain()
        box = QVBoxLayout(self.code_box)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)
        row = QHBoxLayout()
        row.setSpacing(18)
        self.code_label = PairCodeLabel()
        self.code_label.setFont(theme.mono_font(theme.PAIRING_CODE))
        self.code_label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self.code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByKeyboard
                                                | Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code_label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.code_label.setFixedHeight(self.code_label.sizeHint().height())
        row.addWidget(self.code_label, 1, Qt.AlignmentFlag.AlignVCenter)
        self.count_label = widgets.label("", "count")
        self.count_label.setFont(theme.mono_font(theme.TYPE["body"]))
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.count_label.setAccessibleName("Time left")
        box.addLayout(row)
        count_row = QHBoxLayout()
        count_row.addStretch(1)
        count_row.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        box.addLayout(count_row)
        self.drain = widgets.Drain()
        box.addWidget(self.drain)
        self.address_note = widgets.label("", "note", wrap=True)
        self.address_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.address_note)
        self.qr = QrView()
        self.qr.setVisible(False)
        box.addWidget(self.qr, 0, Qt.AlignmentFlag.AlignLeft)
        self.qr_caption = widgets.label("", "note-quiet", wrap=True)
        box.addWidget(self.qr_caption)
        self.code_box.setVisible(False)
        self.body.addWidget(self.code_box)

        self.body.addWidget(widgets.rule(False))
        self.body.addWidget(widgets.label("Enter a code from another machine", "tile-name"))
        self.body.addWidget(widgets.label("On this network", "note-quiet"))
        self.heard_layout = QVBoxLayout()
        self.heard_layout.setContentsMargins(0, 0, 0, 0)
        self.heard_layout.setSpacing(6)
        self.body.addLayout(self.heard_layout)
        self.none_heard = widgets.label("No other machine is showing a code yet.", "note", wrap=True)
        self.body.addWidget(self.none_heard)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.address = QLineEdit()
        self.address.setAccessibleName("Address of the other machine")
        self.address.setPlaceholderText("Address")
        self.address.textEdited.connect(self._typed_address)
        self.body.addWidget(self.address)
        self.body.addWidget(widgets.label("Not listed? Type its address.", "note-quiet"))
        self.code = PairCodeEdit()
        self.code.setAccessibleName("Code")
        self.code.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9 ]{0,12}"), self.code))
        self.code.setFont(theme.mono_font(20))
        self.code.setFixedHeight(self.code.fontMetrics().height() + CODE_CELL_PAD_Y * 2 + 4)
        self.code.textEdited.connect(self._typed_code)
        self.code.returnPressed.connect(self._pair)
        self.body.addWidget(self.code)
        self.pair_button = _button("Pair", "primary")
        self.pair_button.clicked.connect(self._pair)
        self.body.addWidget(self.pair_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.pair_note = widgets.label("", "note", wrap=True)
        self.pair_note.setVisible(False)
        self.body.addWidget(self.pair_note)
        self._refresh_pair_button()

    # The host half.

    def show_code(self, code: str, seconds: int, address: str, qr_rows: Optional[list], qr_note: str) -> None:
        shown = f"{code[:3]} {code[3:]}"
        if self.code_label.text() != shown:
            self.code_label.setText(shown)
            self.code_label.setAccessibleName(f"Pairing code {' '.join(code)}")
        self.count_label.setText(f"Expires in {seconds} s")
        self.drain.set_remaining(seconds)
        if self.address_note.text() != address:
            self.address_note.setText(address)
        self.address_note.setVisible(bool(address))
        if not feature_flags.PAIRING_QR_VISIBLE:
            qr_rows, qr_note = None, ""
        self.qr.setVisible(qr_rows is not None)
        if qr_rows is not None:
            self.qr.set_rows(qr_rows)
        caption = "The same code as a QR, for a phone to scan." if qr_rows is not None else qr_note
        if self.qr_caption.text() != caption:
            self.qr_caption.setText(caption)
        self.qr_caption.setVisible(bool(caption))
        self.cancel_button.setVisible(True)
        self.show_button.setVisible(False)
        self.code_box.setVisible(True)

    def hide_code(self) -> None:
        self.cancel_button.setVisible(False)
        self.show_button.setVisible(True)
        self.drain.set_remaining(0)
        self.code_label.setAccessibleName("Pairing code")
        self.code_box.setVisible(False)

    @property
    def code_showing(self) -> bool:
        return self.cancel_button.isVisibleTo(self)

    def say_host(self, text: str, tone: str) -> None:
        if self.host_note.text() != text:
            self.host_note.setText(text)
        self.host_note.setVisible(bool(text))
        widgets.set_role(self.host_note, tone)

    # The requester half.

    def set_machines(self, machines: list, shown: Callable[[str], str] = lambda text: text) -> None:
        """What discovery hears: dicts with name, address, platform and pairing, and `pair_id` while a
        code is up. Only a machine showing a code is worth choosing, but one that speaks the old exchange
        is listed too, as Older Beamer, so it is not mistaken for missing. A machine heard on two
        interfaces is two rows of one name, so where names are shared each row says its address, as
        `shown` (the window's Hide addresses) lets it."""
        listed = [item for item in machines if item.get("pair_id") or item.get("pairing") != pairing.PAIRING_V3]
        wheres = [shown(item["address"]) if shared else "" for item, shared in zip(listed, peerlist.sharing_a_name(listed))]
        key = tuple((item["address"], item["name"], item.get("platform"), item.get("pairing"), item.get("pair_id"), where)
                    for item, where in zip(listed, wheres))
        if key == self._listed:
            return
        self._listed = key
        chosen = self.chosen_machine()
        self._machines = listed
        for button in list(self.group.buttons()):
            self.group.removeButton(button)
            self.heard_layout.removeWidget(button)
            button.hide()
            button.deleteLater()
        for item, where in zip(listed, wheres):
            version = item.get("pairing") == pairing.PAIRING_V3
            parts = (item["name"], peers_view.platform_name(item), where, "showing a code" if version else OLDER)
            text = "  ·  ".join(part for part in parts if part)
            button = widgets.ElidingButton(text)
            button.setProperty("vernier", "choice")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.set_elision_suffix("  ·  " + "  ·  ".join(part for part in parts[1:] if part))
            button.setCheckable(True)
            button.setMinimumHeight(widgets.MIN_TARGET)
            # A list of machines reads down its left edge; the theme centres a choice.
            button.setStyleSheet("text-align: left; padding-left: 12px;")
            button.setProperty("address", item["address"])
            button.clicked.connect(lambda _checked=False, address=item["address"]: self._pick(address))
            self.group.addButton(button)
            self.heard_layout.addWidget(button)
            if chosen is not None and chosen["address"] == item["address"]:
                button.setChecked(True)
        if chosen is not None and not any(b.isChecked() for b in self.group.buttons()):
            self._on_pick(None)
        self.none_heard.setVisible(not listed)
        self._refresh_pair_button()

    def chosen_machine(self) -> Optional[dict]:
        for button in self.group.buttons():
            if button.isChecked():
                return next((item for item in self._machines if item["address"] == button.property("address")), None)
        return None

    def _pick(self, address: str) -> None:
        self.address.clear()
        self._on_pick(next((item for item in self._machines if item["address"] == address), None))
        self._refresh_pair_button()

    def _typed_address(self, _text: str) -> None:
        checked = self.group.checkedButton()
        if checked is not None:
            self.group.setExclusive(False)
            checked.setChecked(False)
            self.group.setExclusive(True)
            self._on_pick(None)
        self._refresh_pair_button()

    def _typed_code(self, text: str) -> None:
        digits = "".join(char for char in text if char.isdigit())[:CODE_DIGITS]
        if digits != text:
            self.code.setText(digits)
        self._refresh_pair_button()

    def _refresh_pair_button(self) -> None:
        chosen = self.chosen_machine()
        usable = (chosen is not None and chosen.get("pairing") == pairing.PAIRING_V3) or bool(self.address.text().strip())
        self.pair_button.setEnabled(usable and len(self.code.text()) == CODE_DIGITS and not self._working)

    def _pair(self) -> None:
        if self.pair_button.isEnabled():
            self._on_pair(self.chosen_machine(), self.address.text().strip(), self.code.text())

    def say_pair(self, text: str, tone: str) -> None:
        if self.pair_note.text() != text:
            self.pair_note.setText(text)
        self.pair_note.setVisible(bool(text))
        widgets.set_role(self.pair_note, tone)

    def busy(self, working: bool) -> None:
        self._working = working
        self.pair_button.setText("Pairing…" if working else "Pair")
        self._refresh_pair_button()

    def clear_code(self) -> None:
        self.code.clear()
        self._refresh_pair_button()
