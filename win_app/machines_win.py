"""Overview's two pieces for paired machines: the list with each one's state and switches, and the
pairing sheet. Plain widgets that hold no settings and make no calls of their own: the window feeds
them and gives each the one function it calls back."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QRectF, QRegularExpression, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import peers_view
import theme
import widgets
from core import pairing, peerlist

KEEP_NOTE = "It stays paired on that machine until you remove this one there too."
EMPTY = "No machines paired yet."
OLDER = "Older Beamer"
CODE_DIGITS = 6
QR_SCALE = 5


def _plain() -> QWidget:
    item = QWidget()
    item.setProperty("vernier", "plain")
    return item


def _button(text: str, role: str = "") -> QPushButton:
    button = QPushButton(text)
    if role:
        button.setProperty("vernier", role)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class PeerRow(QWidget):
    """One paired machine: its name and state, one sentence, the two direction switches and Remove."""

    def __init__(self, token: str, on_send: Callable, on_allow: Callable, on_remove: Callable) -> None:
        super().__init__()
        self.setProperty("vernier", "plain")
        self.token = token
        self._on_remove = on_remove
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 8)
        column.setSpacing(6)
        self.divider = widgets.rule(False)
        column.addWidget(self.divider)
        column.addSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(10)
        self.led = widgets.Led()
        head.addWidget(self.led, 0, Qt.AlignmentFlag.AlignVCenter)
        self.name = widgets.label("", "tile-name", wrap=True)
        head.addWidget(self.name, 1)
        self.word = widgets.label("", "readout")
        head.addWidget(self.word, 0, Qt.AlignmentFlag.AlignRight)
        column.addLayout(head)
        self.where = widgets.label("", "note-quiet", wrap=True)
        column.addWidget(self.where)
        self.detail = widgets.label("", "note", wrap=True)
        column.addWidget(self.detail)
        self.send = widgets.Switch("This PC drives it")
        self.allow = widgets.Switch("It drives this PC")
        for switch in (self.send, self.allow):
            switch.setFont(theme.font(theme.TYPE["body"]))
        self.send.toggled.connect(lambda on: on_send(self.token, on))
        self.allow.toggled.connect(lambda on: on_allow(self.token, on))
        self.remove_button = _button("Remove", "small")
        self.remove_button.setMinimumHeight(widgets.MIN_TARGET)
        self.remove_button.clicked.connect(lambda: self._ask(True))
        controls = QHBoxLayout()
        controls.setSpacing(16)
        switches = QVBoxLayout()
        switches.setSpacing(2)
        switches.addWidget(self.send)
        switches.addWidget(self.allow)
        for switch in (self.send, self.allow):
            switch.setMaximumWidth(300)
        controls.addLayout(switches)
        controls.addStretch(1)
        controls.addWidget(self.remove_button, 0, Qt.AlignmentFlag.AlignBottom)
        self.controls = controls
        column.addLayout(controls)
        self.confirm = _plain()
        sure = QVBoxLayout(self.confirm)
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

    def _ask(self, asking: bool) -> None:
        self.confirm.setVisible(asking)
        self.remove_button.setVisible(not asking)
        self.send.setVisible(not asking)
        self.allow.setVisible(not asking)

    def refresh(self, entry: dict, label: str, state, where: str) -> None:
        self.led.set_tone(state.tone)
        for item, text in ((self.name, label), (self.word, state.word), (self.where, where), (self.detail, state.detail),
                           (self.confirm_text, f"Remove {label}? {KEEP_NOTE}")):
            if item.text() != text:
                item.setText(text)
        for switch, value in ((self.send, bool(entry.get("send"))), (self.allow, bool(entry.get("allow_drive")))):
            if switch.isChecked() != value:
                switch.blockSignals(True)
                switch.setChecked(value)
                switch.blockSignals(False)
        self.word.setAccessibleName(f"{label}: {state.word}")
        self.remove_button.setAccessibleName(f"Remove {label}")


class MachinesModule(widgets.Module):
    """The paired machines, one row each, and the button that opens the pairing sheet."""

    def __init__(self, on_send: Callable, on_allow: Callable, on_remove: Callable, on_pair: Callable) -> None:
        super().__init__("Machines")
        self._callbacks = (on_send, on_allow, on_remove)
        self.rows: dict = {}
        self.rows_layout = QVBoxLayout()
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(0)
        self.body.addLayout(self.rows_layout)
        self.empty = widgets.label(EMPTY, "note", wrap=True)
        self.body.addWidget(self.empty)
        self.pair_button = _button("Pair a machine", "primary")
        self.pair_button.clicked.connect(on_pair)
        self.body.addWidget(self.pair_button, 0, Qt.AlignmentFlag.AlignLeft)

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
        self.code_label = widgets.label("", "code")
        self.code_label.setFont(theme.mono_font(theme.PAIRING_CODE))
        self.code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByKeyboard
                                                | Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code_label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.code_label.setFixedHeight(round(theme.PAIRING_CODE))
        row.addWidget(self.code_label, 1, Qt.AlignmentFlag.AlignVCenter)
        self.count_label = widgets.label("", "count")
        self.count_label.setFont(theme.mono_font(theme.TYPE["body"]))
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.count_label.setAccessibleName("Time left")
        row.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignVCenter)
        box.addLayout(row)
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
        self.code = QLineEdit()
        self.code.setAccessibleName("Code")
        self.code.setPlaceholderText("Code")
        self.code.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9 ]{0,12}"), self.code))
        self.code.setFont(theme.mono_font(theme.TYPE["body"]))
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
            button = _button(text, "choice")
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

