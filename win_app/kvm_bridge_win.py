import argparse
import ctypes
from dataclasses import replace
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Optional

from PySide6.QtCore import QEvent, QObject, QRectF, QSize, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

# From source, core/ sits beside this folder; the built app bundles it. Behind this folder, not
# before it: the root also holds the Mac's theme.py, which must not stand in for this app's.
sys.path.append(str(Path(__file__).resolve().parent.parent))

import app_config
from app_config import (
    Config,
    ConfigError,
    TRIGGER_KEYS,
    TRIGGER_VKS,
    UNRECORDABLE_TRIGGER_VKS,
    default_config,
    default_config_path,
    load_config,
    save_config,
)
import autostart_win
import capture_win
import desktop_win
from core import effects
from diagram import ArrangementDiagram, PushStrip
from edge_glow import EdgeGlow, PreviewLoop
from effect_overlay import EffectOverlay, logical_point
from effect_previews import EffectStill, SwitchStill, TileHover
import firewall_win
from core import ignored
import motion
from core import pairing
from core.pairing import PAIRING_PORT, Announcer, local_address_towards
import pages_win
from core import protocol
from core import receiver
from core.receiver import ReceiverServer, ServerState
from core import return_edge
import sender
from core import settings_sync
from sender import MacSender
import theme
import tokens
from core import updates
import widgets

LOGGER = logging.getLogger(__name__)

# The repo-root VERSION file is the one place the version is set; build.spec bundles it.
try:
    VERSION = (Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent.parent) / "VERSION").read_text().strip()
except OSError:
    VERSION = "dev"

HOME_PAGE = "https://kalkmancode.co.uk/beamer"
HOME_PAGE_TEXT = "Beamer's website"

IDLE_CODE = "––– –––"
PAIR_HINT = "Type this code into Beamer on the Mac."

STATUS_TITLES = {
    ServerState.STOPPED: "Receiver stopped",
    ServerState.WAITING: "Waiting for your Mac",
    ServerState.CONNECTED: "Mac connected",
    ServerState.ERROR: "Receiver needs attention",
}

# The short word beside the sidebar's link dot -- STATUS_TITLES is a sentence, this is one word.
SIDEBAR_LINK_WORDS = {
    ServerState.STOPPED: "Stopped",
    ServerState.WAITING: "Waiting",
    ServerState.CONNECTED: "Connected",
    ServerState.ERROR: "Error",
}

HEADING_ROLE = {
    "signal": "heading",
    "off": "heading",
    "amber": "heading",
    "fault": "heading-fault",
}

EDGE_CHOICES = pages_win.SIDE_CHOICES
CORNER_CHOICES = (
    ("top_left", "Top-left"),
    ("top_right", "Top-right"),
    ("bottom_left", "Bottom-left"),
    ("bottom_right", "Bottom-right"),
)
TRIGGER_STYLE_CHOICES = (("double_tap", "Double-tap"), ("hold", "Hold"))
MODIFIER_STYLE_CHOICES = (("semantic", "Same shortcuts"), ("positional", "Same positions"))
MODIFIER_NOTES = {
    "semantic": "Ctrl arrives on the Mac as Command and the Windows key as Control, so Ctrl+C "
    "copies there too.",
    "positional": "Each key arrives as the Mac key in the same place: Ctrl as Control, the Windows "
    "key as Command.",
}
FULL_SCREEN_CHECK_MS = 1000
# How long a dragged slider waits, still, before the value it settled on is written to disk.
SETTLE_MS = 300


def asset_path(name: str) -> Path:
    """Locate a bundled asset, honouring PyInstaller's onefile extraction dir."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    else:
        base = Path(__file__).resolve().parent
    return base / name


ICON_PATH = asset_path("Beamer.ico")


# Where configure_logging put the log, for the tray's Open log folder.
LOG_PATH: Optional[Path] = None


def configure_logging() -> Optional[Path]:
    global LOG_PATH
    candidates = [default_config_path().parent, Path(tempfile.gettempdir()) / "Beamer"]
    for directory in candidates:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            log_path = directory / "Beamer.log"
            handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
            root_logger = logging.getLogger()
            # BEAMER_DEBUG=1 logs every injected key by name, which is the only
            # way to tell a key the Mac never sent from one Windows swallowed.
            root_logger.setLevel(logging.DEBUG if os.environ.get("BEAMER_DEBUG") else logging.INFO)
            root_logger.addHandler(handler)
            LOG_PATH = log_path
            return log_path
        except OSError:
            continue
    logging.basicConfig(level=logging.INFO)
    return None


def status_icon(state: ServerState, size: int = 64) -> QPixmap:
    """The shipped colour mark with the state dot over its lower right corner: signal connected,
    amber waiting, fault needs attention, off stopped."""
    # Ratio pinned to 1: the plain pixmap(size, size) comes back at the screen's scale (96px at
    # 150%), which fails the width check below and leaves the tray showing only the dot.
    pixmap = QIcon(str(ICON_PATH)).pixmap(QSize(size, size), 1.0)
    if pixmap.isNull() or pixmap.width() != size:
        pixmap = QPixmap(size, size)
        pixmap.fill(QColor(theme.colour("panel")))
    unit = size / 16
    dot = 6 * unit
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(theme.colour("ground")))
    painter.drawEllipse(QRectF(size - dot - 2 * unit, size - dot - 2 * unit, dot + 2 * unit, dot + 2 * unit))
    painter.setBrush(QColor(theme.state_colour(state.value.lower())))
    painter.drawEllipse(QRectF(size - dot - unit, size - dot - unit, dot, dot))
    painter.end()
    return pixmap


class StatusBridge(QObject):
    """Marshals receiver-thread status callbacks onto the GUI thread."""

    changed = Signal(object, str)
    # The fourth names which third of the edge a Part of the edge push is in, else None.
    pressure = Signal(str, float, bool, object)
    # (method, edge, x, y): the pointer landed on this PC at desktop pixel (x, y).
    arrived = Signal(str, str, float, float)
    firewall = Signal(object)
    paired = Signal(str, str, str)
    # The other direction: this PC's own input going to the Mac.
    sending = Signal(bool, str)
    redirecting = Signal(bool)
    focus = Signal(str)
    learned = Signal(str, object, object)
    # Either link, telling us the two machines' arrangement changed at the other end.
    arrangement = Signal(str, int)
    # Either link, carrying the Mac's Same on both machines state.
    settings = Signal(object)
    alert = Signal(str, str)
    mac_learned = Signal(str)
    # (version, url) when a newer Beamer is out, else None.
    update = Signal(object)
    # The firewall rules are in place (or could not be), so the sockets may open.
    rules_ready = Signal()


class WindowsApplication(QWidget):
    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        self._status_lock = threading.RLock()
        self._status = ServerState.STOPPED
        self._status_detail = "Initialising"
        self._config = None
        self._closing = False
        self._page = "overview"
        self._apply_serial = 0
        self.bridge = StatusBridge()
        self.bridge.changed.connect(self._on_status)
        self.bridge.pressure.connect(self._on_pressure)
        self.bridge.arrived.connect(self._on_arrival)
        self.bridge.firewall.connect(self._on_firewall)
        self.bridge.paired.connect(self._on_paired)
        self.bridge.sending.connect(self._on_sending)
        self.bridge.redirecting.connect(self._on_redirecting)
        self.bridge.focus.connect(self._on_focus)
        self.bridge.learned.connect(self._on_learned)
        self.bridge.arrangement.connect(self._on_arrangement)
        self.bridge.settings.connect(self._on_settings)
        self.bridge.update.connect(self._on_update)
        self.bridge.rules_ready.connect(self._listen)
        self._update = None
        self._effects_failed = False
        # Announces this PC from launch, configured or not: pairing is how a fresh install
        # gets its token, so it cannot wait for the receiver to be listening.
        self.announcer = Announcer(self._announced_port, self.bridge.paired.emit, logger=LOGGER)
        self._code_shown = False
        self._code_addresses: list = []
        self._firewall_advice: Optional[firewall_win.Advice] = None
        self._firewall_status: Optional[firewall_win.FirewallStatus] = None
        self._firewall_tone: Optional[str] = None
        self._firewall_busy = False
        self._firewall_again = False
        self._firewall_auto_repaired = False
        self._host = default_config().host
        self._last_seen_state: Optional[ServerState] = None
        self.glow: Optional[EdgeGlow] = None
        self.effects: Optional[EffectOverlay] = None
        self.server = ReceiverServer(
            self._set_status,
            pressure_callback=lambda edge, pressure, crossed, part=None: self.bridge.pressure.emit(edge, pressure, crossed, part),
            # The Mac's notch crossing lands on this PC's bottom edge, and that is the only
            # arrival an effect draws differently. No edge is the Mac's shortcut or menu.
            arrival_callback=lambda edge, x, y: self.bridge.arrived.emit(
                "switch" if edge is None else "notch" if edge == "bottom" else "edge", edge or "", float(x), float(y)
            ),
            focus_callback=self.bridge.focus.emit,
            peer_callback=self.bridge.learned.emit,
            arrangement_callback=self.bridge.arrangement.emit,
        )
        # The second link, outwards: this PC's keyboard and mouse on the Mac.
        self.sender = MacSender(
            status_callback=self.bridge.sending.emit,
            redirect_callback=self.bridge.redirecting.emit,
            pressure_callback=self.bridge.pressure.emit,
            arrangement_callback=self.bridge.arrangement.emit,
            arrival_callback=lambda edge, x, y: self.bridge.arrived.emit(
                "switch" if edge is None else "edge", edge or "", float(x), float(y)
            ),
        )
        self.sender.send_peer_home = self.server.send_home
        # Same on both machines, over either link: applied on the GUI thread, and announced with
        # this PC's own arrangement the moment either link comes up.
        for link in (self.server, self.sender):
            link.settings_callback = self.bridge.settings.emit
            link.announce = self._announce
        self.scope_labels: dict = {}
        self.own_notes: list = []
        self._same_seen = None
        # Pause crossing and the full-screen hold are about this screen: the Mac's pointer does
        # not go home through a held edge either.
        self.server.edges_held = lambda: self.sender.edges_held
        self.server.return_model = self._return_model
        self.sender.on_alert = self.bridge.alert.emit
        self.sender.on_mac_learned = self.bridge.mac_learned.emit
        self.bridge.alert.connect(self._on_alert)
        self.bridge.mac_learned.connect(self._on_mac_learned)
        self.hooks = capture_win.Hooks(self._on_hook_key, self.sender.on_mouse, self.sender.on_motion)
        self._trigger = capture_win.Trigger()
        self._sending_detail = "Not connected to the Mac"
        self.update_checker = updates.Checker(
            VERSION, lambda: self._config is None or self._config.check_updates, self.bridge.update.emit, logger=LOGGER
        )
        try:
            self._config = load_config(config_path)
            self._same_seen = self._same_fields()
            self._apply_input_scale(self._config)
            self._host = self._config.host
            self._status = ServerState.WAITING
            self._status_detail = f"Ready on TCP port {self._config.port}"
        except ConfigError as exc:
            LOGGER.info("Configuration is not ready: %s", exc)
            self._status = ServerState.ERROR
            self._status_detail = "Not paired yet: press Pair a Mac on Overview"

        # Before any widget is built: every control takes its colours from the palette in use.
        appearance = self._config.appearance if self._config is not None else "system"
        theme.set_dark(theme.wants_dark(appearance, theme.system_dark()))

        self.setWindowTitle("Beamer")
        self.resize(820, 720)
        self.setMinimumSize(*tokens.MIN_WINDOW["windows"])
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.preview_loop = PreviewLoop(self)
        self.tile_hover = TileHover(self.preview_loop, self)
        self._build_window()
        self._apply_theme()
        self._build_tray()
        theme.watch_system(self._apply_appearance)

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(250)
        self.refresh_timer.timeout.connect(self._refresh_window)
        self.refresh_timer.start()
        self.full_screen_timer = QTimer(self)
        self.full_screen_timer.setInterval(FULL_SCREEN_CHECK_MS)
        self.full_screen_timer.timeout.connect(self._check_full_screen)
        self.full_screen_timer.start()
        # Otherwise the heading, the "Input:"/"Now:" readouts and the sidebar dots sit blank
        # for the first 250ms every launch, waiting for the timer's first tick.
        self._refresh_window()

    # -- window and pages -----------------------------------------------------------------

    def _build_window(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        row = QWidget()
        # The cross-fade's own widget when the appearance changes: sidebar and pages, everything
        # below the native title bar and above the footer, which is thin enough to just flip.
        self._body = row
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(0)

        self.sidebar = widgets.Sidebar(
            pages_win.PAGES, self._select_page, foot=f"Beamer {VERSION}\n{HOME_PAGE_TEXT}", on_foot=self.open_home_page,
            foot_tip=HOME_PAGE,
        )
        self.sidebar.setFixedWidth(theme.SIDEBAR_WIDTH)
        row_layout.addWidget(self.sidebar)
        row_layout.addWidget(widgets.rule())

        self.stack = QStackedWidget()
        row_layout.addWidget(self.stack, 1)
        outer.addWidget(row, 1)

        footer = QFrame()
        footer.setProperty("vernier", "commit")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(16, 10, 16, 10)
        self.footer_note = widgets.label(pages_win.footer("overview"), "note", wrap=True)
        footer_layout.addWidget(self.footer_note)
        outer.addWidget(footer)

        current = self._config or default_config()
        builders = {
            "overview": self._overview_page,
            "crossing": self._crossing_page,
            "design": self._design_page,
            "keyboard": self._keyboard_page,
            "connection": self._connection_page,
        }
        self._page_indexes: dict = {}
        for key, name, purpose in pages_win.PAGES:
            scroll, layout = self._page_shell(name, purpose, pages_win.SCOPE.get(key), key)
            builders[key](layout, current)
            layout.addStretch(1)
            self._page_indexes[key] = self.stack.addWidget(scroll)

        for index, key in enumerate(pages_win.KEYS[:9]):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self)
            shortcut.activated.connect(lambda k=key: self._select_page(k))

        self._select_page("overview")

    def _page_shell(self, title: str, purpose: str, scope: Optional[str] = None, key: Optional[str] = None):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # Nothing scrolls sideways, at any width.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        page = QWidget()
        page.setProperty("vernier", "plain")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        heading = widgets.label(title.upper(), "heading")
        heading.setFont(theme.font(theme.HEADING, 700))
        layout.addWidget(heading)
        layout.addWidget(widgets.label(purpose, "note", wrap=True))
        if scope is not None:
            # Whose settings these are, not something happening now, so not signal: on light,
            # signal reads as a link.
            self.scope_labels[key] = widgets.label(scope, "note-quiet", wrap=True)
            layout.addWidget(self.scope_labels[key])
        scroll.setWidget(page)
        return scroll, layout

    def _select_page(self, key: str) -> None:
        index = self._page_indexes.get(key)
        if index is None:
            return
        self._page = key
        self.stack.setCurrentIndex(index)
        self.sidebar.select(key)
        self.footer_note.setText(pages_win.footer(key))
        if key != "keyboard":
            self.ignored_recorder.cancel()
        if key != "crossing":
            self.trigger_recorder.cancel()
        self._run_previews()

    # -- Overview ---------------------------------------------------------------------------

    def _overview_page(self, layout, current) -> None:
        # Pairing leads until there is a Mac, since nothing else here works without one; once
        # paired it moves down beside the other once-only settings (_place_pairing).
        self.overview_layout = layout
        self.link_module = self._link_module()
        layout.addWidget(self.link_module)
        layout.addWidget(self._input_module())
        layout.addWidget(self._directions_module(current))
        layout.addWidget(self._same_module(current))
        self.sign_in_module = self._sign_in_module()
        layout.addWidget(self.sign_in_module)
        layout.addWidget(self._updates_module(current))
        self._pairing_block(layout, current)

    def _updates_module(self, current: Config) -> QWidget:
        module = widgets.Module("Updates")
        self.updates_switch = widgets.Switch("Check for updates")
        self.updates_switch.setFont(theme.font(theme.TYPE["body"]))
        self.updates_switch.setChecked(current.check_updates)
        self.updates_switch.toggled.connect(self._set_check_updates)
        module.body.addWidget(self.updates_switch)
        module.body.addWidget(widgets.label(
            "Asks GitHub once a day whether there is a newer Beamer. Nothing is sent but the request itself.",
            "note",
            wrap=True,
        ))
        self.update_button = QPushButton("Download")
        self.update_button.setProperty("vernier", "primary")
        self.update_button.clicked.connect(self.open_update)
        self.update_button.setVisible(False)
        module.body.addWidget(self.update_button)
        return module

    def _set_check_updates(self, enabled: bool) -> None:
        if self._config is None:
            return
        self._config.check_updates = bool(enabled)
        self._persist()
        if enabled:
            self.update_checker.check_now()
        else:
            self._on_update(None)

    def _on_update(self, found) -> None:
        """(version, url) for a newer release, or None; on the GUI thread."""
        self._update = found
        self.update_button.setText(f"Download Beamer {found[0]}" if found else "Download")
        motion.set_shown(self.update_button, found is not None)
        self.header_action.setText(f"Beamer {found[0]} is available…" if found else f"Beamer {VERSION}")
        self.header_action.setEnabled(found is not None)

    def open_update(self) -> None:
        if self._update:
            QDesktopServices.openUrl(QUrl(self._update[1]))

    def open_log_folder(self) -> None:
        folder = LOG_PATH.parent if LOG_PATH is not None else default_config_path().parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _link_module(self) -> QWidget:
        module = widgets.Module("Link")
        heading_row = QHBoxLayout()
        heading_row.setSpacing(10)
        self.led = widgets.Led()
        heading_row.addWidget(self.led, 0, Qt.AlignmentFlag.AlignVCenter)
        self.status_heading = widgets.label("", "heading", wrap=True)
        self.status_heading.setFont(theme.font(theme.HEADING, 700))
        heading_row.addWidget(self.status_heading, 1)
        module.body.addLayout(heading_row)
        self.status_detail = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.status_detail)
        self.outward_line = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.outward_line)
        where_row = QHBoxLayout()
        where_row.setSpacing(6)
        where_row.addWidget(widgets.label("Input:", "note"))
        self.location_readout = widgets.label("", "readout", wrap=True)
        where_row.addWidget(self.location_readout, 1)
        module.body.addLayout(where_row)
        # Shown only while there is a figure: the trip is measured while input is on the Mac.
        self.round_trip_row = QWidget()
        self.round_trip_row.setProperty("vernier", "plain")
        trip_row = QHBoxLayout(self.round_trip_row)
        trip_row.setContentsMargins(0, 0, 0, 0)
        trip_row.setSpacing(6)
        trip_row.addWidget(widgets.label("Delay:", "note"))
        self.round_trip_readout = widgets.label("", "readout", wrap=True)
        trip_row.addWidget(self.round_trip_readout, 1)
        self.round_trip_row.setVisible(False)
        module.body.addWidget(self.round_trip_row)
        return module

    def _input_module(self) -> QWidget:
        """The Mac's two everyday buttons: send input across without the shortcut or an edge, and
        hold the edges for a while."""
        module = widgets.Module("Keyboard and mouse")
        self.redirect_button = QPushButton("Send input to your Mac")
        self.redirect_button.setProperty("vernier", "primary")
        self.redirect_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.redirect_button.clicked.connect(self.toggle_redirect)
        module.body.addWidget(self.redirect_button)
        # Why the button above is dimmed, while it is.
        self.redirect_note = widgets.label("Switch on This PC drives your Mac, below, to send input from here.",
                                           "note", wrap=True)
        self.redirect_note.setVisible(False)
        module.body.addWidget(self.redirect_note)
        self.pause_button = QPushButton("Pause crossing")
        self.pause_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.crossing_state = widgets.label("", "note", wrap=True)
        # Hidden while only the shortcut is chosen: there is no edge to pause.
        self.pause_row = self._row(self.pause_button, self.crossing_state)
        module.body.addWidget(self.pause_row)
        self.hold_switch = widgets.Switch("Hold the edges while an app is full screen")
        self.hold_switch.setFont(theme.font(theme.TYPE["body"]))
        self.hold_switch.setChecked(self._config.hold_full_screen if self._config else True)
        self.hold_switch.toggled.connect(self._set_hold_full_screen)
        module.body.addWidget(self.hold_switch)
        module.body.addWidget(self._own_note())
        module.body.addWidget(widgets.label(
            "Off, your pointer can leave a full-screen game or video, and the Mac's pointer can come home "
            "through this PC's edge. Useful if your keyboard has no key for the shortcut.", "note", wrap=True))
        return module

    def toggle_redirect(self) -> None:
        self.sender.toggle()

    def toggle_pause(self) -> None:
        self.sender.crossing_paused = not self.sender.crossing_paused
        self._refresh_window()

    def _crossing_state_args(self) -> tuple:
        config = self._config
        methods = set(config.crossing_methods) if config is not None else set()
        return (
            config is not None, bool(config and config.mac_host), bool(config and config.send_to_mac),
            self.sender.connected, bool(methods & {"edge", "part", "corner"}),
            self.sender.crossing_paused, self.sender.full_screen_app,
        )

    def _crossing_state_sentence(self) -> str:
        return pages_win.crossing_state_sentence(*self._crossing_state_args())

    def _check_full_screen(self) -> None:
        """The Mac's rule: a full-screen app in front holds the edges, the shortcut still works.
        A failure stops the check for the run rather than logging once a second."""
        try:
            self.sender.full_screen_app = desktop_win.full_screen_app()
        except Exception:
            self.sender.full_screen_app = None
            self.full_screen_timer.stop()
            LOGGER.exception("Could not tell whether an app is full screen; crossing stays on")

    def _on_alert(self, title: str, message: str) -> None:
        if self._closing:
            return
        if message.startswith("Cannot switch"):
            QApplication.beep()
        self.tray.showMessage(title, self._shown(message), QIcon(str(ICON_PATH)), 4000)

    def _on_mac_learned(self, address: str) -> None:
        if self._config is None or self._config.mac_hardware_address == address:
            return
        self._config.mac_hardware_address = address
        self._persist()

    def _directions_module(self, current: Config) -> QWidget:
        module = widgets.Module("Directions")
        # The tray's words, so the two never disagree.
        self.allow_switch = widgets.Switch("Your Mac drives this PC")
        self.allow_switch.setFont(theme.font(theme.TYPE["body"]))
        self.allow_switch.setChecked(current.allow_mac_to_drive)
        self.allow_switch.toggled.connect(self._set_allow_drive)
        module.body.addWidget(self.allow_switch)
        self.send_switch = widgets.Switch("This PC drives your Mac")
        self.send_switch.setFont(theme.font(theme.TYPE["body"]))
        self.send_switch.setChecked(current.send_to_mac)
        self.send_switch.toggled.connect(self._toggle_sending)
        module.body.addWidget(self.send_switch)
        self.send_hint = widgets.label("", "note", wrap=True)
        self.send_hint.setVisible(False)
        module.body.addWidget(self.send_hint)
        if self._config is None:
            self.allow_switch.setEnabled(False)
            self.send_switch.setEnabled(False)
        return module

    def _same_module(self, current: Config) -> QWidget:
        module = widgets.Module("Settings")
        self.same_switch = widgets.Switch("Same on both machines")
        self.same_switch.setFont(theme.font(theme.TYPE["body"]))
        self.same_switch.setChecked(current.same_on_both)
        self.same_switch.toggled.connect(self._set_same)
        module.body.addWidget(self.same_switch)
        self.same_note = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.same_note)
        if self._config is None:
            self.same_switch.setEnabled(False)
        return module

    def _own_note(self) -> QWidget:
        """A note on a row this PC keeps to itself, shown only while the pages are kept in step."""
        note = widgets.label(pages_win.OWN_ROW, "note", wrap=True)
        note.setVisible(False)
        self.own_notes.append(note)
        return note

    def _peer_too_old(self) -> bool:
        """Whether a link is up to a Mac whose Beamer does not keep settings in step."""
        with self._status_lock:
            receiving = self._status == ServerState.CONNECTED
        links = ((receiving, self.server.peer_settings), (self.sender.connected, self.sender.peer_settings))
        return any(up and known is False for up, known in links)

    def _show_same(self) -> None:
        if self._config is None:
            return
        old = self._peer_too_old()
        on = self._config.same_on_both and not old
        if self.same_switch.isChecked() != on:
            self.same_switch.blockSignals(True)
            self.same_switch.setChecked(on)
            self.same_switch.blockSignals(False)
        self.same_switch.setEnabled(not old)
        self.same_note.setText(settings_sync.switch_note("Mac", self._config.same_on_both, old))
        widgets.set_role(self.same_note, "note-amber" if old else "note")
        for key, label in self.scope_labels.items():
            text = settings_sync.scope(key, "Mac", on, pages_win.SCOPE.get(key))
            if label.text() != text:
                label.setText(text)
        for note in self.own_notes:
            if motion.target_shown(note) != on:
                motion.set_shown(note, on)

    def _same_fields(self) -> tuple:
        return tuple(getattr(self._config, field) if not isinstance(getattr(self._config, field), list)
                     else tuple(getattr(self._config, field)) for field in settings_sync.PC_FIELDS.values())

    def _same_state(self) -> dict:
        config = self._config
        return settings_sync.message_data(config.same_on_both, config.same_set_at, settings_sync.pc_values(config))

    def _send_same(self) -> None:
        data = self._same_state()
        self.sender.send_settings(data)
        self.server.send_settings(data)

    def _announce(self) -> list:
        """What this PC tells the Mac on every new link: its arrangement, only once chosen here
        (one a hello filled in is the Mac's to name), and its settings state. On the link's thread."""
        config = self._config
        if config is None:
            return []
        messages = []
        if config.mac_return_edge in return_edge.OPPOSITE and config.arrangement_set_at:
            messages.append(protocol.arrangement_msg(return_edge.OPPOSITE[config.mac_return_edge], config.arrangement_set_at))
        messages.append(protocol.settings_msg(self._same_state()))
        return messages

    def _set_same(self, on: bool) -> None:
        """Turning it on carries this PC's Crossing and Design across; off, each end keeps what it
        has. Either way the Mac follows, now if a link is up, else when one next comes up."""
        if self._config is None:
            return
        self._config.same_on_both = bool(on)
        self._config.same_set_at = settings_sync.next_stamp(self._config.same_set_at, time.time())
        self._persist()
        self._send_same()
        self._show_same()

    def _on_settings(self, data) -> None:
        """The Mac's settings message, over either link. Its state stands only when it is newer
        than this PC's; then its values replace the shared ones here."""
        if self._config is None:
            return
        held = (self._config.same_on_both, settings_sync.pc_values(self._config))
        taken = settings_sync.arrived(data, self._config.same_set_at, TRIGGER_KEYS, held)
        if taken is None:
            return
        on, set_at, values = taken
        config = replace(self._config, crossing_methods=list(self._config.crossing_methods),
                         crossing_edge_parts=list(self._config.crossing_edge_parts))
        config.same_on_both = on
        config.same_set_at = set_at
        if on:
            settings_sync.apply_pc(config, values)
        try:
            app_config.validate_config(config)
        except ConfigError:
            LOGGER.exception("Settings from the Mac could not be applied")
            return
        self._config = config
        self._same_seen = self._same_fields()
        if not self._persist():
            return
        LOGGER.info("Settings from the Mac applied (same on both machines %s)", "on" if on else "off")
        self.sender.update_config(config)
        self._configure_trigger(config)
        self._reflect_config(config)
        self._show_same()

    def _sign_in_module(self) -> QWidget:
        module = widgets.Module("At sign-in")
        self.logon_switch = widgets.Switch("Start Beamer when you sign in")
        self.logon_switch.setFont(theme.font(theme.TYPE["body"]))
        exe = autostart_win.installed_exe()
        try:
            self.logon_switch.setChecked(exe is not None and autostart_win.is_enabled())
        except OSError:
            LOGGER.exception("Could not read the logon task")
        self.logon_switch.setEnabled(exe is not None)
        self.logon_switch.toggled.connect(self._set_start_at_logon)
        module.body.addWidget(self.logon_switch)
        self.logon_hint = widgets.label(
            "In the tray, with no window." if exe else "Only the installed app can start at sign-in.",
            "note",
            wrap=True,
        )
        module.body.addWidget(self.logon_hint)
        return module

    def _set_allow_drive(self, enabled: bool) -> None:
        """Starts or stops the receiver. There is no separate Start/Stop button: this switch is
        what persists."""
        if self._config is None:
            return
        self._config.allow_mac_to_drive = bool(enabled)
        self._persist()
        try:
            if enabled:
                self.server.start(self._config)
            else:
                self.server.stop()
        except Exception as exc:
            LOGGER.exception("Receiver action failed")
            self._set_status(ServerState.ERROR, f"Receiver failed: {exc}")

    def _set_start_at_logon(self, enabled: bool) -> None:
        exe = autostart_win.installed_exe()
        if exe is None:
            return
        try:
            autostart_win.set_enabled(enabled, exe)
        except OSError as exc:
            LOGGER.warning("Could not change the logon task: %s", exc)
            self.logon_switch.blockSignals(True)
            self.logon_switch.setChecked(not enabled)
            self.logon_switch.blockSignals(False)
            self.logon_hint.setText(f"Windows refused: {exc}")
            return
        self.logon_hint.setText("In the tray, with no window.")

    def _toggle_sending(self, enabled: bool) -> None:
        """Takes effect at once: it decides whether this PC's own keyboard is being
        watched, which is not something to leave a person guessing about."""
        if self._config is None:
            return
        self._config.send_to_mac = bool(enabled)
        self._persist()
        if enabled:
            self._start_sending(self._config)
        else:
            # Input comes home by this switch and shows where the pointer is, as any switch does;
            # stopping would bring it home too, silently, as it does for a reload or quitting.
            self.sender.set_redirecting(False)
            self._stop_sending()

    # -- Crossing -----------------------------------------------------------------------------

    def _crossing_page(self, layout, current: Config) -> None:
        layout.addWidget(self._ways_module(current))
        self.resistance_module = self._resistance_module(current)
        layout.addWidget(self.resistance_module)
        self.shortcut_module = self._shortcut_module(current)
        layout.addWidget(self.shortcut_module)
        self._reflect_ways()

    @staticmethod
    def _row(*items) -> QWidget:
        """Widgets and layouts that show and hide as one row of a module."""
        row = QWidget()
        row.setProperty("vernier", "plain")
        column = QVBoxLayout(row)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        for item in items:
            (column.addLayout if isinstance(item, QHBoxLayout) else column.addWidget)(item)
        return row

    def _ways_module(self, current: Config) -> QWidget:
        module = widgets.Module("Ways in")
        self.ways_summary = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.ways_summary)
        self.arrangement_diagram = ArrangementDiagram()
        module.body.addWidget(self.arrangement_diagram)
        self.way_boxes: dict = {}
        for value, text, detail in pages_win.WAY_ROWS:
            box = QCheckBox(text)
            box.setChecked(value in current.crossing_methods)
            box.toggled.connect(lambda on, v=value: self._ways_changed(v, on))
            self.way_boxes[value] = box
            if not detail:
                module.body.addWidget(box)
                continue
            box.setAccessibleDescription(detail)
            line = QHBoxLayout()
            line.setSpacing(8)
            line.addWidget(box)
            line.addWidget(widgets.label(detail, "small"))
            line.addStretch(1)
            module.body.addLayout(line)

        self.edge_choice = widgets.Choice(
            EDGE_CHOICES, columns=4, current=current.mac_return_edge, on_change=self._set_arrangement
        )
        self.edge_choice.set_names("Where your Mac is")
        self.edge_unlearned = widgets.label(pages_win.NOT_LEARNED_EDGE, "note", wrap=True)
        self.edge_unlearned.setVisible(not current.mac_return_edge)
        self.crossing_rows = {
            "edge": self._row(
                widgets.label("Where your Mac is", "key"),
                widgets.label(
                    "One border, walked both ways, so changing it here moves it on your Mac too.",
                    "note",
                    wrap=True,
                ),
                self.edge_choice.view,
                self.edge_unlearned,
            ),
        }

        chips = QHBoxLayout()
        chips.setSpacing(4)
        self.part_buttons: dict = {}
        for part in return_edge.PARTS:
            button = QPushButton()
            button.setProperty("vernier", "choice")
            button.setCheckable(True)
            button.setMinimumHeight(widgets.MIN_TARGET)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setChecked(part in current.crossing_edge_parts)
            button.clicked.connect(lambda on, p=part: self._set_part(p, on))
            chips.addWidget(button, 1)
            self.part_buttons[part] = button
        self.parts_note = widgets.label("", "note", wrap=True)
        self.crossing_rows["parts"] = self._row(widgets.label("Parts", "key"), chips, self.parts_note)

        self.corner_choice = widgets.Choice(
            CORNER_CHOICES, columns=2, current=current.crossing_corner, on_change=self._set_corner
        )
        self.corner_choice.set_names("Corner")
        self.crossing_rows["corner"] = self._row(widgets.label("Corner", "key"), self.corner_choice.view)

        self.dragging_switch = widgets.Switch("Don't cross while dragging")
        self.dragging_switch.setFont(theme.font(theme.TYPE["body"]))
        self.dragging_switch.setChecked(current.block_while_dragging)
        self.dragging_switch.toggled.connect(self._set_block_while_dragging)
        self.crossing_rows["dragging"] = self._row(self.dragging_switch)
        for key in ("edge", "parts", "corner", "dragging"):
            module.body.addWidget(self.crossing_rows[key])

        now_row = QHBoxLayout()
        now_row.setSpacing(6)
        # The Mac's half of the border -- the edge and push it asks for when it is the one
        # sending. Hidden until the Mac has said, rather than reading "not set yet".
        now_row.addWidget(widgets.label("Coming back from your Mac:", "note"))
        self.return_readout = widgets.label("", "readout", wrap=True)
        now_row.addWidget(self.return_readout, 1)
        self.return_row = self._row(now_row)
        self.return_row.setVisible(False)
        module.body.addWidget(self.return_row)
        return module

    def _ways_changed(self, way: str, on: bool) -> None:
        if self._config is None:
            return
        self._config.crossing_methods = pages_win.toggle_way(self._config.crossing_methods, way, on)
        self._persist()
        self.sender.update_config(self._config)
        self._reflect_ways()
        # The Design page's preview opens where the pointer crosses and says when a place is not a way in.
        self._reflect_look()

    def _set_part(self, part: str, on: bool) -> None:
        if self._config is None:
            return
        self._config.crossing_edge_parts = pages_win.toggle_part(self._config.crossing_edge_parts, part, on)
        self._persist()
        self.sender.update_config(self._config)
        self._reflect_ways()
        # The Design page's preview opens where the pointer crosses and says when a place is not a way in.
        self._reflect_look()

    def _reflect_ways(self) -> None:
        """The ways' boxes, the parts' chips and which rows show, from the config: a row a chosen
        way does not use is hidden, not dimmed, and comes back the moment one does."""
        config = self._config or default_config()
        for value, box in self.way_boxes.items():
            if box.isChecked() != (value in config.crossing_methods):
                box.blockSignals(True)
                box.setChecked(value in config.crossing_methods)
                box.blockSignals(False)
        edge = config.mac_return_edge or "right"
        names = pages_win.part_names(edge)
        for part, button in self.part_buttons.items():
            button.setText(names[part])
            button.setChecked(part in config.crossing_edge_parts)
        self.parts_note.setText(
            f"Crosses only along {pages_win.parts_phrase(edge, config.crossing_edge_parts)}; the rest of "
            "it is a wall. Choose any, but at least one."
        )
        shown = pages_win.crossing_rows(config.crossing_methods)
        for key, row in self.crossing_rows.items():
            motion.set_shown(row, key in shown)
        motion.set_shown(self.resistance_module, "resistance" in shown)
        motion.set_shown(self.shortcut_module, "shortcut" in shown)
        key_name = TRIGGER_KEYS.get(config.trigger_key, config.trigger_key)
        summary = pages_win.ways_summary(config.crossing_methods, config.mac_return_edge, config.crossing_edge_parts,
                                         config.crossing_corner, key_name, config.trigger_style)
        if summary != self.ways_summary.text():
            shot = motion.snapshot(self.ways_summary)
            self.ways_summary.setText(summary)
            motion.fade_from(self.ways_summary, shot)
        motion.set_shown(self.edge_unlearned, not config.mac_return_edge)
        self.arrangement_diagram.set_state(edge, config.crossing_methods, config.crossing_edge_parts,
                                           config.crossing_corner, key_name, config.trigger_style)
        self.resistance_strip.set_edge(edge)

    def _set_arrangement(self, pc_edge: str) -> None:
        """The edge of THIS PC that leads to the Mac -- one border, walked either way. Not an
        ordinary save: both machines have to agree on it, so this end's change is timestamped
        and sent over whichever link is up."""
        if self._config is None or pc_edge == self._config.mac_return_edge:
            return
        self._config.mac_return_edge = pc_edge
        self._config.arrangement_set_at = int(time.time())
        self._persist()
        mac_edge = return_edge.OPPOSITE[pc_edge]
        self.sender.send_arrangement(mac_edge, self._config.arrangement_set_at)
        self.server.send_arrangement(mac_edge, self._config.arrangement_set_at)
        self.sender.update_config(self._config)
        self._reflect_look()
        self._reflect_ways()

    def _set_hold_full_screen(self, enabled: bool) -> None:
        if self._config is None:
            return
        self._config.hold_full_screen = bool(enabled)
        self._persist()
        self.sender.update_config(self._config)

    def _set_block_while_dragging(self, enabled: bool) -> None:
        if self._config is None:
            return
        self._config.block_while_dragging = bool(enabled)
        self._persist()
        self.sender.update_config(self._config)

    def _set_corner(self, corner: str) -> None:
        if self._config is None:
            return
        self._config.crossing_corner = corner
        self._persist()
        self.sender.update_config(self._config)
        self._reflect_ways()

    def _on_arrangement(self, mac_edge: str, set_at: int) -> None:
        """The Mac changed the arrangement, over either link. `mac_edge` is always the edge of
        the MAC that leads here; an arrival older than what this end already holds is ignored."""
        if self._config is None:
            return
        pc_edge = return_edge.OPPOSITE.get(mac_edge)
        if pc_edge is None or pc_edge == self._config.mac_return_edge:
            # One change on the Mac reaches this PC over both links, so the second copy finds it applied.
            return
        if self._config.arrangement_set_at and not protocol.arrangement_wins(set_at, self._config.arrangement_set_at):
            LOGGER.info("Ignoring an arrangement from the Mac that is no newer than this PC's (%s vs %s)", set_at, self._config.arrangement_set_at)
            return
        self._config.mac_return_edge = pc_edge
        self._config.arrangement_set_at = int(set_at)
        self._persist()
        self.sender.update_config(self._config)
        self.edge_choice.set_value(pc_edge)
        self._reflect_look()
        self._reflect_ways()

    def _resistance_module(self, current: Config) -> QWidget:
        module = widgets.Module("Resistance")
        self.resistance_strip = PushStrip()
        self.resistance_strip.set_edge(current.mac_return_edge or "right")
        self.resistance_strip.set_value(current.crossing_resistance_px)
        module.body.addWidget(self.resistance_strip)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.resistance_slider = widgets.Ruler("Resistance", pages_win.RESISTANCE_MAX)
        self.resistance_slider.setValue(current.crossing_resistance_px)
        self.resistance_slider.valueChanged.connect(self._resistance_changed)
        row.addWidget(self.resistance_slider, 1)
        self.resistance_readout = widgets.label(f"{current.crossing_resistance_px} px", "readout")
        row.addWidget(self.resistance_readout)
        module.body.addLayout(row)
        self.resistance_hint = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.resistance_hint)
        self._update_resistance_hint(current.crossing_resistance_px)
        return module

    def _resistance_changed(self, value: int) -> None:
        self.resistance_readout.setText(f"{value} px")
        self.resistance_strip.set_value(value)
        self._update_resistance_hint(value)
        if self._config is None:
            return
        self._config.crossing_resistance_px = int(value)
        self.sender.update_config(self._config)
        self._debounce_save()

    def _update_resistance_hint(self, value: int) -> None:
        text = (
            "Switches the moment the pointer touches the edge."
            if value == 0
            else "How far to push past the edge before it gives."
        )
        if text != self.resistance_hint.text():
            self.resistance_hint.setText(text)

    # -- Keyboard -----------------------------------------------------------------------------

    def _keyboard_page(self, layout, current: Config) -> None:
        # The modifier keys are set once; the stays-here list is the one people come back to.
        layout.addWidget(self._modifier_module(current))
        layout.addWidget(self._ignored_module(current))
        layout.addWidget(self._speed_module(current))

    def _speed_module(self, current: Config) -> QWidget:
        """How the Mac's pointer feels on this PC: the Mac sends what its own acceleration made of
        the hand's movement, and this PC's settings decide the rest."""
        module = widgets.Module("The Mac's pointer here")
        self.speed_sliders = {}
        self.speed_readouts = {}
        for key, name, value in (("pointer_speed", "Pointer speed", current.pointer_speed),
                                 ("scroll_speed", "Scroll speed", current.scroll_speed)):
            module.body.addWidget(widgets.label(name, "body"))
            row = QHBoxLayout()
            row.setSpacing(10)
            slider = widgets.Ruler(name, 400, minimum=25)
            slider.setValue(round(value * 100))
            slider.valueChanged.connect(lambda percent, key=key: self._speed_changed(key, percent))
            row.addWidget(slider, 1)
            readout = widgets.label(f"{round(value * 100)}%", "readout")
            row.addWidget(readout)
            module.body.addLayout(row)
            self.speed_sliders[key], self.speed_readouts[key] = slider, readout
        module.body.addWidget(widgets.label(
            "For the Mac's trackpad or mouse while it drives this PC.", "note", wrap=True
        ))
        self.reverse_scroll_switch = widgets.Switch("Reverse the Mac's scrolling")
        self.reverse_scroll_switch.setFont(theme.font(theme.TYPE["body"]))
        self.reverse_scroll_switch.setChecked(current.reverse_scroll)
        self.reverse_scroll_switch.toggled.connect(self._reverse_scroll_changed)
        module.body.addWidget(self.reverse_scroll_switch)
        return module

    def _speed_changed(self, key: str, percent: int) -> None:
        self.speed_readouts[key].setText(f"{percent}%")
        if self._config is None:
            return
        setattr(self._config, key, percent / 100)
        self._apply_input_scale(self._config)
        self._debounce_save()

    def _reverse_scroll_changed(self, enabled: bool) -> None:
        if self._config is None:
            return
        self._config.reverse_scroll = bool(enabled)
        self._apply_input_scale(self._config)
        self._debounce_save()

    def _apply_input_scale(self, config: Config) -> None:
        self.server.input_scale = receiver.InputScale(config.pointer_speed, config.scroll_speed, config.reverse_scroll)

    def _modifier_module(self, current: Config) -> QWidget:
        module = widgets.Module("Modifier keys")
        self.modifier_choice = widgets.Choice(
            MODIFIER_STYLE_CHOICES, columns=2, current=current.modifier_style, on_change=self._set_modifier_style
        )
        self.modifier_choice.set_names("Modifier keys")
        module.body.addWidget(self.modifier_choice.view)
        self.modifier_note = widgets.label(MODIFIER_NOTES[current.modifier_style], "note", wrap=True)
        module.body.addWidget(self.modifier_note)
        return module

    def _set_modifier_style(self, value: str) -> None:
        self.modifier_note.setText(MODIFIER_NOTES[value])
        if self._config is None:
            return
        self._config.modifier_style = value
        self._persist()
        self.sender.update_config(self._config)

    def _ignored_module(self, current: Config) -> QWidget:
        module = widgets.Module("Stays on this PC")
        self.ignored_note = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.ignored_note)
        self.ignored_list = QVBoxLayout()
        self.ignored_list.setSpacing(4)
        module.body.addLayout(self.ignored_list)
        self.ignored_recorder = widgets.InputRecorder("Add a key or button", self._record_ignored, capture_win.hook_vk,
                                                      mono=False)
        module.body.addWidget(self.ignored_recorder)
        self._show_ignored(list(current.ignored_inputs))
        return module

    def _show_ignored(self, entries: list, refused: str = "") -> None:
        while self.ignored_list.count():
            item = self.ignored_list.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for entry in entries:
            row = QFrame()
            row.setProperty("vernier", "entry")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(12, 2, 4, 2)
            name = widgets.label(capture_win.input_title(entry), "readout")
            row_layout.addWidget(name, 1)
            remove = QPushButton("Remove")
            remove.setProperty("vernier", "remove")
            remove.setMinimumHeight(widgets.MIN_TARGET)
            remove.setCursor(Qt.CursorShape.PointingHandCursor)
            remove.setAccessibleName(f"Remove {capture_win.input_title(entry)}")
            remove.clicked.connect(lambda _checked=False, e=entry: self._remove_ignored(e))
            row_layout.addWidget(remove)
            self.ignored_list.addWidget(row)
        text = refused or (
            "These keep working on this PC while its input is on your Mac: a mouse's back button for "
            "this PC's browser, say, or a volume key for its speakers."
            if entries
            else "Nothing yet. Every key and button goes to your Mac while it has input. Add one to keep "
            "it here: a mouse's back button for this PC's browser, say, or a volume key for its speakers."
        )
        self.ignored_note.setText(text)
        widgets.set_role(self.ignored_note, "note-amber" if refused else "note")

    def _record_ignored(self, kind: str, value) -> None:
        if self._config is None:
            return
        entries = list(self._config.ignored_inputs)
        if kind == "key" and value == TRIGGER_VKS.get(self._config.trigger_key):
            self._show_ignored(entries, "That key is the shortcut; it always stays with Beamer.")
            return
        entry = ignored.key(value) if kind == "key" else ignored.button(value)
        if entry not in entries:
            entries.append(entry)
        self._set_ignored(entries)

    def _remove_ignored(self, entry: str) -> None:
        if self._config is None:
            return
        self._set_ignored([candidate for candidate in self._config.ignored_inputs if candidate != entry])

    def _set_ignored(self, entries: list) -> None:
        previous = self._config.ignored_inputs
        self._config.ignored_inputs = entries
        if not self._persist():
            # A list the file refuses, past ignored.MAX_ENTRIES, or a disk that would not take it:
            # the running app keeps what is saved, so the list shown is the one the next start uses.
            self._config.ignored_inputs = previous
            self._show_ignored(list(previous), "That could not be saved, so the list is unchanged.")
            return
        self.sender.update_config(self._config)
        self._show_ignored(entries)

    def _shortcut_module(self, current: Config) -> QWidget:
        module = widgets.Module("Shortcut")
        module.body.addWidget(
            widgets.label(
                "Use this key to send input to your Mac, and to bring it back.", "note", wrap=True
            )
        )
        self.trigger_recorder = widgets.InputRecorder(
            TRIGGER_KEYS.get(current.trigger_key, current.trigger_key),
            self._record_trigger,
            capture_win.hook_vk,
            keys_only=True,
            name="Shortcut key",
        )
        self.trigger_recorder.HINT = "Click, then press the key"
        self.trigger_recorder.hint.setText(self.trigger_recorder.HINT)
        module.body.addWidget(self.trigger_recorder)
        self.trigger_note = widgets.label("", "note-amber", wrap=True)
        self.trigger_note.setVisible(False)
        module.body.addWidget(self.trigger_note)
        self.trigger_style_choice = widgets.Choice(
            TRIGGER_STYLE_CHOICES, columns=2, current=current.trigger_style, on_change=self._set_trigger_style
        )
        self.trigger_style_choice.set_names("How you press it")
        module.body.addWidget(self._row(widgets.label("How you press it", "key"), self.trigger_style_choice.view))
        self.style_hint = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.style_hint)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(widgets.label("Time between taps", "key"))
        head.addStretch(1)
        self.double_tap_readout = widgets.label(f"{current.double_tap_ms} ms", "readout")
        head.addWidget(self.double_tap_readout)
        # The Mac's range: the store takes 50 to 2000 ms, but only about 150 to 600 is useful.
        self.double_tap_slider = widgets.Ruler("Time between taps", 1000, minimum=50)
        self.double_tap_slider.setValue(current.double_tap_ms)
        self.double_tap_slider.valueChanged.connect(self._double_tap_changed)
        self.double_tap_row = self._row(head, self.double_tap_slider)
        module.body.addWidget(self.double_tap_row)
        self.double_tap_row.setVisible(current.trigger_style != "hold")
        self._update_style_hint(current.trigger_style)
        return module

    def _record_trigger(self, kind: str, value) -> None:
        """Any key that types nothing, as on the Mac: a modifier, a function key, a navigation
        key. A key that types a character would stop typing it, so it is refused, and said so."""
        name = None
        if kind == "key" and value not in UNRECORDABLE_TRIGGER_VKS:
            name = next((n for n, vk in TRIGGER_VKS.items() if vk == value), None)
        if name is None:
            self.trigger_note.setText(
                "That key cannot be the shortcut. Choose a modifier such as Right Ctrl, a function "
                "key, or a key like Insert or Scroll Lock."
            )
            self.trigger_note.setVisible(True)
            return
        self.trigger_note.setVisible(False)
        self.trigger_recorder.set_title(TRIGGER_KEYS[name])
        if self._config is None:
            return
        self._config.trigger_key = name
        self._persist()
        self._configure_trigger(self._config)
        self._reflect_ways()
        if ignored.key(value) in self._config.ignored_inputs:
            # The shortcut always stays with Beamer, so it cannot also be on the stays-here list.
            self._set_ignored([entry for entry in self._config.ignored_inputs if entry != ignored.key(value)])

    def _set_trigger_style(self, value: str) -> None:
        if self._config is None:
            return
        self._config.trigger_style = value
        self._persist()
        self._configure_trigger(self._config)
        motion.set_shown(self.double_tap_row, value != "hold")
        self._update_style_hint(value)
        self._reflect_ways()

    def _update_style_hint(self, style: str) -> None:
        text = (
            "Input is on the Mac for as long as the key is held."
            if style == "hold"
            else "Tap twice to switch; tap twice again to come back."
        )
        if text != self.style_hint.text():
            self.style_hint.setText(text)

    def _double_tap_changed(self, value: int) -> None:
        self.double_tap_readout.setText(f"{value} ms")
        if self._config is None:
            return
        self._config.double_tap_ms = int(value)
        self._configure_trigger(self._config)
        self._debounce_save()

    # -- Design -------------------------------------------------------------------------------

    def _design_page(self, layout, current: Config) -> None:
        layout.addWidget(self._on_screen_module(current))
        self.look_module = self._edge_look_module(current)
        layout.addWidget(self.look_module)
        layout.addWidget(self._appearance_module(current))
        self._reflect_look()

    def _appearance_module(self, current: Config) -> QWidget:
        """The settings window's own palette. Last on the page, and never hidden with the
        crossing animations above it: it is chosen once, not part of what they show."""
        module = widgets.Module("Appearance")
        module.body.addWidget(self._own_note())
        self.appearance_choice = widgets.Choice(
            (("system", "System"), ("light", "Light"), ("dark", "Dark")), 3, current.appearance,
            on_change=self._appearance_chosen,
        )
        module.body.addWidget(self._row(widgets.label("This window", "key"), self.appearance_choice.view))
        module.body.addWidget(widgets.label("System follows Windows' light or dark setting.", "note", wrap=True))
        return module

    def _appearance_chosen(self, choice: str) -> None:
        # Applies the moment it is chosen, not after _debounce_save's pause: the choice is the change.
        self._apply_appearance(choice)
        if self._config is None:
            return
        self._config.appearance = choice
        self._persist()

    def _on_screen_module(self, current: Config) -> QWidget:
        module = widgets.Module("On screen")
        self.glow_toggle = widgets.Switch("Animate crossings on this PC")
        self.glow_toggle.setFont(theme.font(theme.TYPE["body"]))
        self.glow_toggle.setChecked(current.edge_glow)
        self.glow_toggle.toggled.connect(self._apply_look)
        module.body.addWidget(self.glow_toggle)
        module.body.addWidget(self._own_note())
        module.body.addWidget(
            widgets.label(
                "Lights this PC as you push toward your Mac. Switched off, crossing still works. "
                "Your Mac sets how its own edge and notch look.",
                "note",
                wrap=True,
            )
        )
        self.landing_toggle = widgets.Switch("Show where the pointer lands")
        self.landing_toggle.setFont(theme.font(theme.TYPE["body"]))
        self.landing_toggle.setChecked(current.shortcut_arrival)
        self.landing_toggle.toggled.connect(self._apply_look)
        # The landing plays through the same animations, so it goes with them when they are off.
        self.landing_row = self._row(
            self.landing_toggle,
            widgets.label(
                "When the shortcut or a menu brings input to this PC, an animation plays around the "
                "pointer. Choose it under Style for: Shortcut and menu.",
                "note",
                wrap=True,
            ),
        )
        self.landing_row.layout().setSpacing(12)
        module.body.addWidget(self.landing_row)
        return module

    def _edge_look_module(self, current: Config) -> QWidget:
        module = widgets.Module("Style and colour")
        edge = current.mac_return_edge or "right"
        colours = app_config.palette_colours(current.glow_colour)
        # Which animation the tiles below choose. Not saved: it only says which one is being worked on.
        self.design_mode_choice = widgets.Choice(
            (("crossing", "Crossing"), ("switch", "Shortcut and menu")), 2, "crossing", on_change=self._preview_method
        )
        self.design_mode_choice.set_names("Style for")
        self.style_for_row = self._row(widgets.label("Style for", "key"), self.design_mode_choice.view)
        module.body.addWidget(self.style_for_row)
        # Where every tile below plays its style: never switched off by the style, and what differs
        # there is said beneath it. The edge or a corner; a PC has no notch to show.
        self.effect_method_choice = widgets.Choice(
            (("edge", "Edge"), ("corner", "Corner")), 2, "edge", on_change=self._place_picked
        )
        self.effect_method_choice.set_names("Show at")
        self._place_chosen = False
        self.place_note = widgets.label("", "small", wrap=True)
        self.preview_at_row = self._row(widgets.label("Show at", "key"), self.effect_method_choice.view, self.place_note)
        module.body.addWidget(self.preview_at_row)
        module.body.addSpacing(6)
        self.effect_stills = {}
        groups = []
        for title, items in pages_win.style_groups():
            # Glow and Beam as stills of the same scene as the effects, so every tile reads alike.
            tiles = [(style, name, detail, self.effect_stills.setdefault(style, EffectStill(style, colours)))
                     for style, name, detail in items]
            groups.append((title, tiles))
        self.glow_style_choice = widgets.TileGroups(groups, current.glow_style, on_change=self._apply_look)
        module.body.addWidget(self.glow_style_choice.view)
        self.switch_stills = {}
        switch_groups = []
        for title, items in pages_win.switch_groups():
            tiles = []
            for choice, name, detail in items:
                picture = self.switch_stills[choice] = SwitchStill(choice, current.glow_style, colours)
                tiles.append((choice, name, detail, picture))
            switch_groups.append((title, tiles))
        self.switch_style_choice = widgets.TileGroups(switch_groups, current.shortcut_arrival_style, on_change=self._apply_look)
        module.body.addWidget(self.switch_style_choice.view)
        # Each tile plays its preview while the pointer is over it.
        for choices, pictures in ((self.glow_style_choice, self.effect_stills),
                                  (self.switch_style_choice, self.switch_stills)):
            for value, picture in pictures.items():
                self.tile_hover.track(choices.tile(value), picture)
        # The chosen style's name and what it does, under the tiles that chose it.
        self.effect_name = widgets.label("", "key")
        self.effect_note = widgets.label("", "note", wrap=True)
        caption = self._row(self.effect_name, self.effect_note)
        caption.layout().setSpacing(2)
        module.body.addWidget(caption)
        module.body.addSpacing(6)
        # Every style and every switch plays at this length, so it follows the tiles either way.
        self.length_choice = widgets.Choice(effects.LENGTHS, 3, current.effect_length, on_change=self._apply_look)
        self.length_choice.set_names("Length")
        module.body.addWidget(self._row(
            widgets.label("Length", "key"), self.length_choice.view,
            widgets.label("How long each animation takes to play through once the pointer crosses, and to land.",
                          "small", wrap=True),
        ))
        module.body.addSpacing(10)
        colour_head = QHBoxLayout()
        colour_head.setSpacing(8)
        colour_head.addWidget(widgets.label("Colour", "key"))
        self.colour_note = widgets.label("For the crossing and the shortcut alike", "small")
        colour_head.addWidget(self.colour_note)
        colour_head.addStretch(1)
        module.body.addLayout(colour_head)
        self.glow_colour_choice = widgets.SwatchGroups(
            [(title, [(value, name, app_config.palette_colours(value)) for value, name in items])
             for title, items in pages_win.colour_groups()],
            current.glow_colour,
            on_change=self._apply_look,
        )
        module.body.addWidget(self.glow_colour_choice.view)
        self._look = None
        return module

    def _preview_method(self, _method) -> None:
        self._reflect_look()

    def _place_picked(self, _place) -> None:
        self._place_chosen = True
        self._reflect_look()

    def _apply_look(self, *_ignored) -> None:
        """The switch, style and colour save and apply the moment they change. They only decide
        how crossing is drawn on this PC, so the receiver has no reason to restart for them."""
        self._reflect_look()
        if self._config is None:
            return
        self._config.edge_glow = self.glow_toggle.isChecked()
        self._config.shortcut_arrival = self.landing_toggle.isChecked()
        self._config.shortcut_arrival_style = self.switch_style_choice.value or "match"
        self._config.glow_style = self.glow_style_choice.value
        self._config.glow_colour = self.glow_colour_choice.value
        self._config.effect_length = self.length_choice.value or "normal"
        self._persist()
        self._hide_crossing()

    def _reflect_look(self) -> None:
        on = self.glow_toggle.isChecked()
        landing = self.landing_toggle.isChecked()
        # With the animations off, what they look like cannot apply, so it is hidden, not faded.
        motion.set_shown(self.landing_row, on)
        motion.set_shown(self.look_module, on)
        # Style for exists only while a switch plays anything; otherwise the module is Crossing.
        motion.set_shown(self.style_for_row, landing)
        self.colour_note.setVisible(landing)
        if not landing:
            self.design_mode_choice.set_value("crossing")
        switching = landing and self.design_mode_choice.value == "switch"
        style = self.glow_style_choice.value
        edge = (self._config.mac_return_edge if self._config is not None else "") or "right"
        colour = self.glow_colour_choice.value or "signal"
        methods = self._config.crossing_methods if self._config is not None else ()
        if not self._place_chosen:
            # Until a place is picked here, the preview opens where the pointer actually crosses.
            self.effect_method_choice.set_value(pages_win.preview_place(methods))
        method = self.effect_method_choice.value or "edge"
        switch_style = (self.switch_style_choice.value or "match") if switching else None
        length = self.length_choice.value or "normal"
        look = (style, colour, switching, switch_style, method, edge, length)
        previous, self._look = self._look, look
        # Each still redrawn by this change cross-fades: every one with the colour or the length, the
        # crossing's with the place, the Shortcut and menu stills with the crossing style beside them.
        stills = []
        if previous is not None and (previous[1], previous[6]) != (colour, length):
            stills = [*self.effect_stills.values(), *self.switch_stills.values()]
        elif previous is not None and previous[4] != method:
            stills = list(self.effect_stills.values())
        elif previous is not None and previous[0] != style:
            stills = list(self.switch_stills.values())
        still_shots = {still: shot for still in stills if (shot := motion.snapshot(still)) is not None}
        if previous is not None and previous[2] != switching:
            leaving = self.glow_style_choice.view if switching else self.switch_style_choice.view
            arriving = self.switch_style_choice.view if switching else self.glow_style_choice.view
            tiles_shot = motion.snapshot(leaving)
            leaving.setVisible(False)
            arriving.setVisible(True)
            motion.fade_from(arriving, tiles_shot, fade_in=True)
        else:
            self.glow_style_choice.view.setVisible(not switching)
            self.switch_style_choice.view.setVisible(switching)
        motion.set_shown(self.preview_at_row, not switching)
        note = pages_win.place_note(style, method, methods)
        self.place_note.setText(note)
        self.place_note.setVisible(bool(note))
        if switching and pages_win.effects_load_error() is None:
            fx = effects.switch_effect(switch_style, style)
            self.effect_name.setText(("Same as crossing: " if switch_style == "match" else "") + fx.name)
        else:
            fx = (effects.preview_effect(style) if pages_win.effects_load_error() is None
                  else effects.CLASSIC.get(style)) or effects.CLASSIC["glow"]
            self.effect_name.setText(f"{fx.name}, {fx.intensity}")
        self.effect_note.setText(
            "The effects stopped working and are off until Beamer restarts, so crossings show Glow in "
            "the same colours. The log says why."
            if self._effects_failed
            else f"{fx.blurb} It plays around the pointer when the shortcut or a menu brings input to this PC."
            if switching
            else fx.blurb
        )
        colours = app_config.palette_colours(colour)
        for still in (*self.effect_stills.values(), *self.switch_stills.values()):
            still.set_palette(colours)
        for still in self.switch_stills.values():
            still.set_crossing_style(style)
        pace = effects.pace(length)
        for still in (*self.effect_stills.values(), *self.switch_stills.values()):
            still.set_pace(pace)
        for still in self.effect_stills.values():
            still.set_place(method)
        for still, shot in still_shots.items():
            motion.fade_from(still, shot)
        self._run_previews()

    def _run_previews(self) -> None:
        """The previews play only while someone can see them: the Design page open in a visible
        window, with the glow switched on, and then a tile only while the pointer is over it."""
        wanted = self.isVisible() and not self.isMinimized() and self._page == "design" and self.glow_toggle.isChecked()
        if not wanted:
            self.tile_hover.stop()
        self.preview_loop.run(wanted)

    def _debounce_save(self) -> None:
        """A ruler being dragged writes once at the end rather than on every step."""
        self._apply_serial += 1
        serial = self._apply_serial
        QTimer.singleShot(SETTLE_MS, lambda: self._settle(serial))

    def _settle(self, serial: int) -> None:
        if serial == self._apply_serial:
            self._persist()

    def _persist(self) -> bool:
        if self._config is None:
            return False
        # A change to a shared setting while Same on both machines is on is stamped here, where
        # every page's changes are saved, and sent once saved.
        fields = self._same_fields()
        shared = self._same_seen is not None and fields != self._same_seen and self._config.same_on_both
        self._same_seen = fields
        if shared:
            self._config.same_set_at = settings_sync.next_stamp(self._config.same_set_at, time.time())
        try:
            save_config(self.config_path, self._config)
            # A change on the Crossing page reaches the Mac's pointer while it is here, not at its
            # next crossing: the way home is built from these settings when it arrives.
            self.server.rearm_return()
            if shared:
                self._send_same()
            return True
        except (ConfigError, OSError):
            LOGGER.exception("Setting could not be saved")
            return False

    # -- Pairing --------------------------------------------------------------------------

    def _pairing_block(self, layout, current: Config) -> None:
        module = widgets.Module("Your Mac")
        self.mac_module = module
        self.paired_heading = widgets.label("", "tile-name", wrap=True)
        module.body.addWidget(self.paired_heading)
        self.pair_intro = widgets.label(
            "Press Pair a Mac, then on your Mac choose this PC and type the six-digit code shown here. "
            "You only do this once.",
            "note",
            wrap=True,
        )
        module.body.addWidget(self.pair_intro)
        # The last pairing's outcome; takes no room while there is none.
        self.pair_note = widgets.label("", "note", wrap=True)
        self.pair_note.setVisible(False)
        module.body.addWidget(self.pair_note)
        self.pair_button = QPushButton()
        self.pair_button.setProperty("vernier", "primary")
        self.pair_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pair_button.clicked.connect(self._toggle_pairing)
        module.body.addWidget(self.pair_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(module)

        self.code_module = widgets.Module("Pairing code")
        code_row = QHBoxLayout()
        code_row.setSpacing(18)
        self.code_label = widgets.label(IDLE_CODE, "code-idle")
        self.code_label.setFont(theme.mono_font(theme.PAIRING_CODE))
        # Reachable by Tab and read out digit by digit, so a screen reader user can type it.
        self.code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByKeyboard
                                                | Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code_label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.code_label.setAccessibleName("Pairing code")
        # The digits have no descenders, so the line box can be trimmed to the ink.
        self.code_label.setFixedHeight(round(theme.PAIRING_CODE))
        code_row.addWidget(self.code_label, 1, Qt.AlignmentFlag.AlignVCenter)
        self.count_label = widgets.label("1:00", "count-idle")
        self.count_label.setFont(theme.mono_font(theme.COUNT))
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.count_label.setAccessibleName("Time left")
        code_row.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.code_module.body.addLayout(code_row)
        self.drain = widgets.Drain()
        self.code_module.body.addWidget(self.drain)
        self.code_module.body.addWidget(widgets.label(PAIR_HINT, "note", wrap=True))
        # Pairing by address, for a network whose broadcasts never reach the Mac.
        self.address_note = widgets.label("", "note", wrap=True)
        self.address_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code_module.body.addWidget(self.address_note)
        self.code_module.setVisible(False)
        layout.addWidget(self.code_module)
        self._show_paired(current.paired_with)

    def _show_paired(self, name: str) -> None:
        self.paired_heading.setText(f"Paired with {name}" if name else "Not paired yet")
        self.pair_intro.setVisible(not name)
        if self.announcer.code is None:
            self.pair_button.setText("Pair a different Mac" if name else "Pair a Mac")
        self._place_pairing(bool(name))

    def _place_pairing(self, paired: bool) -> None:
        """First of Overview's modules until a Mac is paired, then just above At sign-in. The page's
        title and purpose sit in the same layout, above the modules."""
        layout = getattr(self, "overview_layout", None)
        if layout is None or not hasattr(self, "code_module"):
            return
        for widget in (self.mac_module, self.code_module):
            layout.removeWidget(widget)
        at = layout.indexOf(self.sign_in_module if paired else self.link_module)
        layout.insertWidget(at, self.mac_module)
        layout.insertWidget(at + 1, self.code_module)

    def _toggle_pairing(self) -> None:
        if self.announcer.code is not None:
            self.announcer.cancel_pairing()
            return
        if self.announcer.error:
            self._say_pairing(f"Pairing is not available: {self.announcer.error}", "note-fault")
            return
        self._say_pairing("", "note")
        self.announcer.begin_pairing()
        self._refresh_pairing()

    def _pairing_addresses(self) -> list:
        """The one address worth typing on the Mac: this PC's on the network that reaches the Mac
        it last knew, else on the default route. Every adapter's address, virtual switches and
        the hotspot included, only when neither can be found."""
        mac = self._config.mac_host if self._config is not None else ""
        # Connecting a UDP socket only picks the interface; nothing is sent to either address.
        for target in (mac, "192.0.2.1"):
            if not target:
                continue
            try:
                address = local_address_towards(target)
            except OSError:
                continue
            if address and not address.startswith(("127.", "0.")):
                return [address]
        return pairing._local_ipv4_addresses()

    def _refresh_pairing(self) -> None:
        code = self.announcer.code
        if code is not None:
            seconds = self.announcer.seconds_left
            shown = f"{code[:3]} {code[3:]}"
            if self.code_label.text() != shown:
                self.code_label.setText(shown)
                self.code_label.setAccessibleName(f"Pairing code {' '.join(code)}")
            widgets.set_role(self.code_label, "code")
            self.count_label.setText(f"{seconds // 60}:{seconds % 60:02d}")
            widgets.set_role(self.count_label, "count")
            self.drain.set_remaining(seconds)
            self.pair_button.setText("Cancel")
            if not self._code_shown:
                self._code_addresses = self._pairing_addresses()
                self.address_note.setVisible(bool(self._code_addresses))
                motion.set_shown(self.code_module, True)
            # Every tick, so switching Hide addresses while a code is up applies at once.
            note = self._shown(
                f"Not listed on the Mac? Type this PC's address there: {', '.join(self._code_addresses)}"
            ) if self._code_addresses else ""
            if self.address_note.text() != note:
                self.address_note.setText(note)
            self._code_shown = True
            return
        if not self._code_shown:
            return
        self._code_shown = False
        motion.set_shown(self.code_module, False)
        self.code_label.setAccessibleName("Pairing code")
        self.drain.set_remaining(0)
        self._show_paired(self._config.paired_with if self._config is not None else "")
        outcome = self.announcer.outcome
        if outcome == "refused":
            self._say_pairing("A wrong code was entered, so that code is cancelled. Pair again for a fresh one.", "note-fault")
        elif outcome == "version":
            self._say_pairing("The Mac runs a different version of Beamer. Update Beamer on both machines, then pair again.", "note-fault")
        elif outcome == "expired":
            self._say_pairing("The code expired. Pair again for a fresh one.", "note-amber")
        elif outcome is None:
            self._say_pairing("Pairing cancelled.", "note")

    def _say_pairing(self, text: str, tone: str) -> None:
        if text != self.pair_note.text():
            self.pair_note.setText(text)
        self.pair_note.setVisible(bool(text))
        widgets.set_role(self.pair_note, tone)

    def _on_paired(self, token: str, mac_name: str, mac_address: str) -> None:
        current = self._config or default_config()
        host = self.host_entry.text().strip() or self._host
        if not host:
            # A fresh install knows no address of its own; the one facing the Mac is the one
            # to show.
            try:
                host = local_address_towards(mac_address)
            except OSError:
                pass
        try:
            candidate = replace(
                current,
                host=host,
                port=int(self.port_entry.text().strip() or current.port),
                auth_token=token,
                paired_with=mac_name,
            )
            save_config(self.config_path, candidate)
            self._apply_config(candidate)
        except (ConfigError, TypeError, ValueError, OSError) as exc:
            LOGGER.exception("Paired token could not be saved")
            self._say_pairing(f"Paired, but the token could not be saved: {exc}", "note-fault")
            return
        self._host = candidate.host
        self.host_entry.setText(candidate.host)
        self.token_entry.setText(token)
        self._refresh_pairing()
        who = mac_name or "your Mac"
        self._show_paired(candidate.paired_with)
        self._say_pairing("Paired. The receiver restarted with the new token.", "note-live")
        LOGGER.info("Paired with %s", who)

    # -- Connection -------------------------------------------------------------------------

    def _connection_page(self, layout, current: Config) -> None:
        self._firewall_module(layout)
        privacy = widgets.Module("Addresses")
        self.hide_switch = widgets.Switch("Hide addresses")
        self.hide_switch.setFont(theme.font(theme.TYPE["body"]))
        self.hide_switch.setChecked(current.hide_addresses)
        self.hide_switch.toggled.connect(self._set_hide_addresses)
        privacy.body.addWidget(self.hide_switch)
        privacy.body.addWidget(widgets.label(
            "Hides every IP and hardware address in this window and the tray.",
            "note",
            wrap=True,
        ))
        layout.addWidget(privacy)
        module = widgets.Module("This PC")
        fields = QGridLayout()
        fields.setHorizontalSpacing(10)
        fields.setVerticalSpacing(10)
        fields.setColumnStretch(1, 1)

        def field(row, caption, entry, span=2):
            # The caption is the field's buddy and its accessible name, so a screen reader and
            # Alt+letter both reach the field by what it is called.
            key = widgets.label(caption, "key")
            key.setBuddy(entry)
            entry.setAccessibleName(caption)
            fields.addWidget(key, row, 0)
            fields.addWidget(entry, row, 1, 1, span)

        # The address the Mac connects to. Pairing fills it in; a hand set-up copies it from here.
        self.host_entry = QLineEdit(self._host)
        field(0, "This PC's address", self.host_entry)
        self.port_entry = QLineEdit(str(current.port))
        field(1, "Port", self.port_entry)
        self.token_entry = QLineEdit(current.auth_token)
        self.token_entry.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_entry.setMinimumWidth(80)
        field(2, "Shared token", self.token_entry, span=1)
        self.show_token = QPushButton("Show")
        self.show_token.setProperty("vernier", "small")
        self.show_token.setCheckable(True)
        self.show_token.setMinimumHeight(widgets.MIN_TARGET)
        self.show_token.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_token.setToolTip("Show the shared token in this window")
        self.show_token.setAccessibleName("Show the shared token")
        self.show_token.toggled.connect(self._update_token_visibility)
        fields.addWidget(self.show_token, 2, 2)
        module.body.addLayout(fields)
        mac_row = QHBoxLayout()
        mac_row.setSpacing(6)
        mac_row.addWidget(widgets.label("Your Mac's IP address:", "note"))
        self.mac_host_readout = widgets.label(self._shown(current.mac_host) or "Not learned yet", "readout", wrap=True)
        self.mac_host_readout.setAccessibleName("Your Mac's IP address")
        mac_row.addWidget(self.mac_host_readout, 1)
        module.body.addLayout(mac_row)
        self._show_host_entry()
        self.save_message = widgets.label("", "note", wrap=True)
        self.save_message.setVisible(False)
        module.body.addWidget(self.save_message)
        self.save_button = QPushButton("Save and reconnect")
        self.save_button.setProperty("vernier", "primary")
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.clicked.connect(self.save)
        module.body.addWidget(self.save_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(module)

    def _shown(self, text: str) -> str:
        """`text` as the window may show it: with Hide addresses on, no address in it."""
        return pages_win.redact(text, bool(self._config is not None and self._config.hide_addresses))

    def _set_hide_addresses(self, enabled: bool) -> None:
        if self._config is None:
            return
        self._config.hide_addresses = bool(enabled)
        self._persist()
        self._show_host_entry()
        self.mac_host_readout.setText(self._shown(self._config.mac_host) or "Not learned yet")
        self._refresh_pairing()
        self._refresh_window()
        self.tray.setToolTip(self._title())
        self.status_action.setText(self._title())

    def _show_host_entry(self) -> None:
        """The address field as dots while addresses are hidden, still editable."""
        hide = bool(self._config is not None and self._config.hide_addresses)
        self.host_entry.setEchoMode(QLineEdit.EchoMode.Password if hide else QLineEdit.EchoMode.Normal)

    def _update_token_visibility(self, checked: bool) -> None:
        self.token_entry.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)
        self.show_token.setText("Hide" if checked else "Show")
        self.show_token.setAccessibleName("Hide the shared token" if checked else "Show the shared token")

    def save(self) -> None:
        current = self._config or default_config()
        try:
            candidate = replace(
                current,
                host=self.host_entry.text().strip() or self._host,
                port=int(self.port_entry.text().strip()),
                auth_token=self.token_entry.text(),
            )
            save_config(self.config_path, candidate)
            self._apply_config(candidate)
        except (ConfigError, TypeError, ValueError) as exc:
            self.save_message.setText(str(exc))
            self.save_message.setVisible(True)
            widgets.set_role(self.save_message, "note-fault")
            return
        except OSError as exc:
            LOGGER.exception("Configuration could not be saved")
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.save_message.setText("Saved.")
        self.save_message.setVisible(True)
        widgets.set_role(self.save_message, "note-live")

    def _apply_config(self, config: Config) -> None:
        if self.server.listening:
            self.server.stop()
        self._config = config
        self._same_seen = self._same_fields()
        self._apply_input_scale(config)
        if not config.edge_glow:
            self._hide_crossing()
        self.allow_switch.setEnabled(True)
        self.allow_switch.setChecked(config.allow_mac_to_drive)
        if config.allow_mac_to_drive:
            self.server.start(config)
        self.send_switch.setEnabled(True)
        self.send_switch.setChecked(config.send_to_mac)
        if config.send_to_mac:
            self.sender.update_config(config)
            self._start_sending(config)
        else:
            self._stop_sending()
        self.mac_host_readout.setText(self._shown(config.mac_host) or "Not learned yet")

    # -- Firewall, on Connection --------------------------------------------------------------

    def _firewall_module(self, layout) -> None:
        module = widgets.Module("Windows Firewall")
        self.firewall_note = widgets.label("Checking Windows Firewall…", "note", wrap=True)
        module.body.addWidget(self.firewall_note)
        self.firewall_button = QPushButton("Checking…")
        self.firewall_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.firewall_button.setEnabled(False)
        self.firewall_button.clicked.connect(self._firewall_action)
        module.body.addWidget(self.firewall_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(module)

    def _firewall_target(self) -> tuple:
        port = self._config.port if self._config is not None else protocol.DEFAULT_PORT
        return str(Path(sys.executable).resolve()), port

    def _check_firewall(self) -> None:
        self._run_firewall(firewall_win.status, "Checking…")

    def _firewall_action(self) -> None:
        # Only the built exe changes the firewall, by the button or on its own. From source the
        # executable is python.exe, and repair() replaces Beamer's rules by name, so a test run on
        # the PC took the installed Beamer's rules with it and cut the Mac's link (29-09-2026).
        if not getattr(sys, "frozen", False):
            LOGGER.info("Running from source; the firewall is left as it is")
            return
        advice = self._firewall_advice
        if advice is None or advice.action is None:
            return
        if advice.action == "repair":
            profiles = advice.rule_profiles

            def job(exe, port):
                firewall_win.repair(exe, port, profiles)
                return firewall_win.status(exe, port)

        elif advice.action == "trust":
            indexes = [index for index, _ in (self._firewall_status.public_interfaces if self._firewall_status else ())]

            def job(exe, port):
                firewall_win.trust_network(indexes)
                return firewall_win.status(exe, port)

        else:
            self._check_firewall()
            return
        self._run_firewall(job, "Fixing…")

    def _run_firewall(self, job, working: str) -> None:
        """One PowerShell at a time, off the GUI thread. A request arriving mid-run is folded
        into one further status read once the current one finishes."""
        if self._closing:
            return
        if self._firewall_busy:
            self._firewall_again = True
            return
        self._firewall_busy = True
        self.firewall_button.setEnabled(False)
        self.firewall_button.setText(working)
        exe, port = self._firewall_target()

        def work():
            try:
                result = job(exe, port)
            except Exception as exc:
                LOGGER.exception("Firewall job failed")
                result = firewall_win.FirewallStatus(port, False, False, (), (), False, (), firewall_win.is_elevated(), error=f"the change failed: {exc}")
            self.bridge.firewall.emit(result)

        threading.Thread(target=work, name="Beamer-firewall", daemon=True).start()

    def _on_firewall(self, status) -> None:
        self._firewall_busy = False
        if self._closing:
            return
        self._firewall_status = status
        advice = firewall_win.advise(status)
        self._firewall_advice = advice
        self.firewall_note.setText(advice.sentence)
        if status.error:
            tone = "note-fault"
        elif advice.action == "check":
            tone = "note"
        else:
            tone = "note-amber"
        # Remembered for the sidebar dot: "check" is the healthy end-state (its button just
        # offers a manual re-check), so `advice.action is not None` alone would mark Firewall
        # as needing attention even when everything is fine.
        self._firewall_tone = tone
        widgets.set_role(self.firewall_note, tone)
        self.firewall_button.setText(advice.button)
        self.firewall_button.setEnabled(advice.action is not None)
        if self._firewall_again:
            self._firewall_again = False
            self._check_firewall()
            return
        # A missing rule with the rights to write one is not a question for the
        # user: the release installer cannot add it, so a fresh install would
        # otherwise sit unreachable until someone found this page. A block rule
        # or a public network is still theirs to decide.
        if (
            advice.action == "repair"
            and status.elevated
            and not status.allowed
            and not status.blocked
            and not self._firewall_auto_repaired
        ):
            self._firewall_auto_repaired = True
            LOGGER.info("No firewall rule for this executable; adding one")
            self._firewall_action()

    # -- theming, tray, lifecycle ------------------------------------------------------------

    def _apply_theme(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.stylesheet())
        self._apply_title_bar()

    def _apply_title_bar(self) -> None:
        """Paint the native title bar in the Vernier ground instead of the user's Windows accent
        colour. Cosmetic only: any failure (older Windows without the attributes, missing dwmapi,
        etc.) is swallowed so the window still opens."""
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
        except Exception:
            LOGGER.debug("Title bar styling failed", exc_info=True)
            return
        theme.apply_titlebar(hwnd)

    def _apply_appearance(self, choice: Optional[str] = None) -> None:
        """Brings the window to the palette the appearance setting and Windows ask for, cross-faded
        while it is visible. Called for the control's own choice, on load, and by
        theme.watch_system whenever Windows' own light or dark setting changes while open."""
        if choice is None:
            choice = self.appearance_choice.value if hasattr(self, "appearance_choice") else None
        if choice is None:
            choice = self._config.appearance if self._config is not None else "system"
        dark = theme.wants_dark(choice, theme.system_dark())
        if dark == theme.is_dark():
            return
        shot = motion.snapshot(self._body)
        theme.set_dark(dark)
        self._apply_theme()
        widgets.refresh_colours(self)
        motion.fade_from(self._body, shot)

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(QIcon(status_icon(self._status)), self)
        self.tray.setToolTip(self._title())
        menu = QMenu()
        # The version line; it becomes the way to a newer release when there is one.
        self.header_action = menu.addAction(f"Beamer {VERSION}")
        self.header_action.setEnabled(False)
        self.header_action.triggered.connect(self.open_update)
        menu.addSeparator()
        self.open_action = menu.addAction("Open Beamer")
        self.open_action.triggered.connect(self.show_window)
        self.status_action = menu.addAction(self._title())
        self.status_action.setEnabled(False)
        self.redirect_action = menu.addAction("Send input to your Mac")
        self.redirect_action.triggered.connect(self.toggle_redirect)
        self.pause_action = menu.addAction("Pause crossing")
        self.pause_action.triggered.connect(self.toggle_pause)
        menu.addSeparator()
        # One tick per direction, each the same switch the Overview page shows, so either
        # direction can be turned off while the other keeps working and the two never disagree.
        self.drive_action = menu.addAction("Your Mac drives this PC")
        self.drive_action.setCheckable(True)
        self.drive_action.toggled.connect(self.allow_switch.setChecked)
        self.allow_switch.toggled.connect(self.drive_action.setChecked)
        self.send_action = menu.addAction("This PC drives your Mac")
        self.send_action.setCheckable(True)
        self.send_action.toggled.connect(self.send_switch.setChecked)
        self.send_switch.toggled.connect(self.send_action.setChecked)
        self.drive_action.setChecked(self.allow_switch.isChecked())
        self.send_action.setChecked(self.send_switch.isChecked())
        menu.addSeparator()
        menu.addAction("Reload configuration").triggered.connect(self.reload_config)
        menu.addAction("Open log folder").triggered.connect(self.open_log_folder)
        menu.addSeparator()
        menu.addAction("About Beamer").triggered.connect(self.show_about)
        menu.addAction("Quit Beamer").triggered.connect(self.quit)
        self.tray.setContextMenu(menu)
        # Left click opens the window; right click is the
        # context menu Qt already gives QSystemTrayIcon for free.
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def reload_config(self) -> None:
        """Re-reads config.json, for the times it was edited outside the window, and shows it."""
        try:
            config = load_config(self.config_path)
        except (ConfigError, OSError) as exc:
            LOGGER.warning("Configuration not reloaded: %s", exc)
            self._on_alert("Beamer", f"Could not reload the configuration: {exc}")
            return
        self._host = config.host
        self._apply_config(config)
        self._configure_trigger(config)
        self._reflect_config(config)
        LOGGER.info("Configuration reloaded from %s", self.config_path)

    def _reflect_config(self, config: Config) -> None:
        """Every control on every page set from `config`, without any of them saving back."""
        for box in (*self.way_boxes.values(), self.dragging_switch, self.hold_switch, self.glow_toggle,
                    self.landing_toggle,
                    self.resistance_slider, self.double_tap_slider):
            box.blockSignals(True)
        try:
            self.dragging_switch.setChecked(config.block_while_dragging)
            self.hold_switch.setChecked(config.hold_full_screen)
            self.glow_toggle.setChecked(config.edge_glow)
            self.landing_toggle.setChecked(config.shortcut_arrival)
            self.switch_style_choice.set_value(config.shortcut_arrival_style)
            self.resistance_slider.setValue(config.crossing_resistance_px)
            self.double_tap_slider.setValue(config.double_tap_ms)
        finally:
            for box in (*self.way_boxes.values(), self.dragging_switch, self.hold_switch, self.glow_toggle,
                        self.landing_toggle,
                        self.resistance_slider, self.double_tap_slider):
                box.blockSignals(False)
        self.resistance_readout.setText(f"{config.crossing_resistance_px} px")
        self._update_resistance_hint(config.crossing_resistance_px)
        self.double_tap_readout.setText(f"{config.double_tap_ms} ms")
        self.resistance_strip.set_value(config.crossing_resistance_px)
        motion.set_shown(self.double_tap_row, config.trigger_style != "hold")
        self.corner_choice.set_value(config.crossing_corner)
        self.edge_choice.set_value(config.mac_return_edge)
        self.trigger_recorder.set_title(TRIGGER_KEYS.get(config.trigger_key, config.trigger_key))
        self.trigger_style_choice.set_value(config.trigger_style)
        self._update_style_hint(config.trigger_style)
        self.modifier_choice.set_value(config.modifier_style)
        self.modifier_note.setText(MODIFIER_NOTES[config.modifier_style])
        self.glow_style_choice.set_value(config.glow_style)
        self.glow_colour_choice.set_value(config.glow_colour)
        self.length_choice.set_value(config.effect_length)
        self._reflect_look()
        self.appearance_choice.set_value(config.appearance)
        self._apply_appearance(config.appearance)
        self._reflect_ways()
        self._show_ignored(list(config.ignored_inputs))
        for key, slider in self.speed_sliders.items():
            slider.blockSignals(True)
            slider.setValue(round(getattr(config, key) * 100))
            slider.blockSignals(False)
            self.speed_readouts[key].setText(f"{slider.value()}%")
        for switch, value in ((self.reverse_scroll_switch, config.reverse_scroll),
                              (self.updates_switch, config.check_updates),
                              (self.hide_switch, config.hide_addresses)):
            switch.blockSignals(True)
            switch.setChecked(value)
            switch.blockSignals(False)
        self.host_entry.setText(config.host)
        # The switch above was set with its signal blocked, so its handler never ran.
        self._show_host_entry()
        self.port_entry.setText(str(config.port))
        self.token_entry.setText(config.auth_token)
        self._show_paired(config.paired_with)

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def start(self) -> None:
        self.update_checker.start()
        if getattr(sys, "frozen", False) and firewall_win.is_elevated():
            # Beamer's own rules before any socket opens: a listener Windows has no rule for makes
            # it ask, and a click on Allow there writes rules for every network, public ones too,
            # where Beamer's are for private networks only. Not from source: see _firewall_action.
            threading.Thread(target=self._rules_then_listen, name="Beamer-firewall-first", daemon=True).start()
        else:
            self._listen()

    def _rules_then_listen(self) -> None:
        try:
            exe, port = self._firewall_target()
            status = firewall_win.status(exe, port)
            if not status.error and not status.allowed and not status.blocked:
                LOGGER.info("Adding this PC's firewall rules before listening")
                firewall_win.repair(exe, port, ("Private",))
        except Exception:
            LOGGER.exception("The firewall rules could not be added before listening")
        self.bridge.rules_ready.emit()

    def _listen(self) -> None:
        if self._closing:
            return
        self.announcer.start()
        if self._config is not None:
            self.server.start(self._config)
            self._start_sending(self._config)
        # After the rules, so the firewall module reads what start() just wrote, and with no config
        # the receiver never starts, so nothing else would ever read it.
        self._check_firewall()

    def _configure_trigger(self, config: Config) -> None:
        self._trigger.configure(config.trigger_key, config.trigger_style, config.double_tap_ms)

    def _start_sending(self, config: Config) -> None:
        """The outward link and the hooks that feed it. The hooks go in only
        when sending is on: they are the one part of Beamer that can take this
        PC's own keyboard away, so an install that never sends never installs
        them."""
        if not config.send_to_mac:
            return
        self._configure_trigger(config)
        try:
            self.hooks.start()
        except Exception as exc:
            LOGGER.exception("The input hooks could not be installed")
            self._sending_detail = f"This PC's keyboard could not be captured: {exc}"
            return
        self.sender.start(config)

    def _on_hook_key(self, name: str, down: bool, vk: Optional[int] = None, us: Optional[str] = None) -> bool:
        """Every key, on the hook thread. The trigger is swallowed as it
        switches; everything else goes to the Mac only while the Mac has
        input, and a key on the ignored list not even then."""
        action = self._trigger.feed(name, down, time.monotonic())
        if action is not None:
            if not self.sender.shortcut_armed:
                # The key is still the trigger's, so it never reaches an app,
                # but with the shortcut switched off it moves nothing.
                return True
            if action == capture_win.TOGGLE:
                self.sender.toggle()
            else:
                self.sender.set_redirecting(action == capture_win.REDIRECT)
            return True
        if self._trigger.claims(name, down, self.sender.redirecting):
            return True
        return self.sender.on_key(name, down, vk, us)

    def _on_focus(self, target: str) -> None:
        """The Mac took input on this PC, or gave it back. Either way the
        outward edge follows: one of the two links owns the keyboard at a
        time, never both."""
        self.sender.set_receiving(target == "windows")

    def _on_learned(self, host, edge, resistance) -> None:
        """The Mac's address and the way home it named in its hello. Saved, so
        this PC can open its own link to the Mac before the Mac has crossed --
        or at all, if the Mac is asleep when Beamer starts here."""
        if self._config is None:
            return
        if sender.is_this_machine(host):
            # The Mac reached this PC through the macOS 27 localhost tunnel,
            # so its "address" is this PC's own. Saving it would point the
            # outward link at this PC's own receiver.
            LOGGER.info("Ignoring %s as the Mac's address: that is this PC", host)
            host = None
        changed = False
        if host and host != self._config.mac_host and self._config.mac_hardware_address:
            # A different Mac: the old one's hardware address would wake the wrong machine.
            self._config.mac_hardware_address = ""
            changed = True
        # The hello's way home only fills an edge this PC does not hold yet: one chosen here, or
        # one the Mac's stamped arrangement set, stands, and the Mac announces its arrangement on
        # every new link anyway.
        if self._config.mac_return_edge:
            edge = None
        for name, value in (("mac_host", host), ("mac_return_edge", edge or ""), ("mac_resistance_px", resistance)):
            if value in (None, "") or getattr(self._config, name) == value:
                continue
            setattr(self._config, name, value)
            changed = True
        if not changed:
            return
        LOGGER.info("Learned the Mac at %s, coming home through the %s edge", self._config.mac_host, self._config.mac_return_edge)
        if not self._persist():
            return
        self.sender.update_config(self._config)
        self._start_sending(self._config)
        shown = pages_win.redact(self._config.mac_host, self._config.hide_addresses)
        self.mac_host_readout.setText(shown or "Not learned yet")
        self.edge_choice.set_value(self._config.mac_return_edge)
        self._reflect_look()
        self._reflect_ways()

    def _on_sending(self, connected: bool, detail: str) -> None:
        self._sending_detail = detail

    def _on_redirecting(self, redirecting: bool) -> None:
        self._sending_detail = (
            "This PC's keyboard and mouse are on the Mac" if redirecting else self.sender.status
        )

    def _announced_port(self) -> int:
        return self._config.port if self._config is not None else default_config().port

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def open_home_page(self) -> None:
        QDesktopServices.openUrl(QUrl(HOME_PAGE))

    def show_about(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("About Beamer")
        box.setIconPixmap(QIcon(str(ICON_PATH)).pixmap(64, 64))
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            f"<b>Beamer {VERSION}</b><br>One keyboard and mouse for your Mac and PC.<br><br>"
            f'<a href="{HOME_PAGE}" style="color: {theme.colour("signal")};">{HOME_PAGE_TEXT}</a>'
        )
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        for text in box.findChildren(QLabel):
            text.setOpenExternalLinks(True)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.show()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._run_previews()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.ignored_recorder.cancel()
        self.trigger_recorder.cancel()
        self._run_previews()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._run_previews()

    def quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.refresh_timer.stop()
        self.update_checker.stop()
        self.announcer.stop()
        self.server.stop()
        self._stop_sending()
        self._hide_crossing()
        self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event) -> None:
        """Closing the window hides to the tray; the tray's Quit ends the process."""
        if self._closing:
            super().closeEvent(event)
            return
        event.ignore()
        self.hide()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._page == "connection":
            self.save()
            return
        super().keyPressEvent(event)

    def _stop_sending(self) -> None:
        self.sender.stop()
        self.hooks.stop()
        self._sending_detail = "Off"

    def _effect_overlay(self) -> Optional[EffectOverlay]:
        """The crossing effects' window when the chosen style is one of them and they have not
        failed this run; None means the plain glow draws instead."""
        config = self._config
        if self._closing or config is None or not config.edge_glow or not pages_win.is_effect(config.glow_style):
            return None
        if self.effects is None:
            self.effects = EffectOverlay(on_failure=self._on_effects_failed)
        if self.effects.failed:
            return None
        self.effects.configure(config.glow_style, config.glow_colour, config.effect_length)
        if self.effects.failed or self.effects._fx is None:
            return None
        return self.effects

    def _on_pressure(self, edge: str, pressure: float, crossed: bool, part=None) -> None:
        if self._closing or self._config is None or not self._config.edge_glow:
            return
        overlay = self._effect_overlay()
        if overlay is not None:
            overlay.push(edge, pressure, crossed, part=part)
            return
        style = self._config.glow_style
        if self.glow is None:
            self.glow = EdgeGlow()
        # An effect that failed earlier in the run falls back to the plain glow, in its colour.
        self.glow.configure(style if style in ("glow", "beam") else "glow", self._config.glow_colour,
                            self._config.effect_length)
        self.glow.set_pressure(edge, pressure, crossed, part)

    def _on_arrival(self, method: str, edge: str, x: float, y: float) -> None:
        """A crossing landed the pointer on `edge`, or with method "switch" input came here by a
        switch and the pointer is wherever it was left."""
        if method == "switch":
            overlay = self._switch_overlay()
            if overlay is not None:
                overlay.switched(logical_point(x, y), self._config.shortcut_arrival_style)
            return
        overlay = self._effect_overlay()
        if overlay is not None:
            overlay.arrive(method, edge, logical_point(x, y))
        elif self.effects is not None:
            self.effects.crossed_in()

    def _switch_overlay(self) -> Optional[EffectOverlay]:
        """The effects' window for a switch into this PC, when the Design page asks to show where
        the pointer lands: the chosen effect's arrival, or the locator with Glow and Beam."""
        config = self._config
        if self._closing or config is None or not config.edge_glow or not config.shortcut_arrival:
            return None
        if self.effects is None:
            self.effects = EffectOverlay(on_failure=self._on_effects_failed)
        if self.effects.failed:
            return None
        self.effects.configure(config.glow_style, config.glow_colour, config.effect_length)
        if self.effects.failed or self.effects._fx is None:
            return None
        return self.effects

    def _return_model(self, edge: str, resistance: int):
        """The way home for the Mac's pointer through `edge` of this screen, from this PC's own
        ways in, as for this PC's own mouse: only the chosen thirds, the whole edge, the corner
        when it sits on that edge, else none (the Mac's shortcut still switches). On the
        receiver's session thread."""
        config = self._config
        if config is None:
            return return_edge.ReturnEdge(edge, resistance)
        methods = set(config.crossing_methods)
        if "part" in methods:
            return return_edge.PartEdge(edge, config.crossing_edge_parts, resistance)
        if "edge" in methods:
            return return_edge.ReturnEdge(edge, resistance)
        corner = config.crossing_corner
        if "corner" in methods and edge in corner.split("_"):
            return return_edge.CornerPush(corner, edge, resistance)
        return None

    def _on_effects_failed(self) -> None:
        """The overlay turned the effects off for the run; the Design page says so."""
        self._effects_failed = True
        self._reflect_look()

    def _hide_crossing(self) -> None:
        if self.glow is not None:
            self.glow.hide()
        if self.effects is not None:
            self.effects.stop()

    def _set_status(self, state: ServerState, detail: str) -> None:
        """Called from the receiver's thread — hand off to the GUI thread."""
        with self._status_lock:
            self._status = state
            self._status_detail = detail
        self.bridge.changed.emit(state, detail)

    def _on_status(self, state, _detail: str) -> None:
        if self._closing:
            return
        try:
            self.tray.setIcon(QIcon(status_icon(state)))
            self.tray.setToolTip(self._title())
            self.status_action.setText(self._title())
        except Exception:
            LOGGER.exception("Tray status update failed")
        # Re-read the firewall each time the receiver starts listening, never on a timer: a
        # Mac dropping and reconnecting flips WAITING <-> CONNECTED and must not trigger it.
        if state is ServerState.WAITING and self._last_seen_state not in (ServerState.WAITING, ServerState.CONNECTED):
            self._check_firewall()
        self._last_seen_state = state

    def _refresh_window(self) -> None:
        if self._closing:
            return
        self._show_same()
        with self._status_lock:
            state = self._status
            detail = self._status_detail
        tone = theme.state_tone(state.value.lower())
        self.led.set_tone(tone)
        # The Mac's name goes in the detail, not the heading: a long name wrapped the heading onto
        # two lines at 640 wide and made the window scroll.
        if state is ServerState.CONNECTED and detail.startswith("Connected to "):
            who = (self._config.paired_with if self._config is not None else "") or "Your Mac"
            detail = f"{who} at {detail[len('Connected to '):]}"
        detail = self._shown(detail)
        heading = STATUS_TITLES[state]
        if heading != self.status_heading.text():
            first = not self.status_heading.text()
            shot = None if first else motion.snapshot(self.status_heading)
            self.status_heading.setText(heading)
            widgets.set_role(self.status_heading, HEADING_ROLE[tone])
            motion.fade_from(self.status_heading, shot, fade_in=True)
        widgets.set_role(self.status_heading, HEADING_ROLE[tone])
        if detail != self.status_detail.text():
            self.status_detail.setText(detail)
        widgets.set_role(self.status_detail, "note-fault" if state is ServerState.ERROR else "note")
        location = "On your Mac" if self.sender.redirecting else "On this PC"
        if location != self.location_readout.text():
            self.location_readout.setText(location)
        trip = self.sender.round_trip_ms
        trip_text = f"{trip} ms" if trip is not None else ""
        if trip_text != self.round_trip_readout.text():
            self.round_trip_readout.setText(trip_text)
            self.round_trip_row.setVisible(trip is not None)
        redirect_text = "Bring input back to this PC" if self.sender.redirecting else "Send input to your Mac"
        if redirect_text != self.redirect_button.text():
            self.redirect_button.setText(redirect_text)
            self.redirect_action.setText(redirect_text)
        sending = self._config is not None and self._config.send_to_mac
        self.redirect_button.setEnabled(sending)
        self.redirect_action.setEnabled(sending)
        self.redirect_note.setVisible(not sending and self._config is not None)
        pause_text = "Resume crossing" if self.sender.crossing_paused else "Pause crossing"
        if pause_text != self.pause_button.text():
            self.pause_button.setText(pause_text)
            self.pause_action.setText(pause_text)
            widgets.set_role(self.pause_button, "primary" if self.sender.crossing_paused else "")
        args = self._crossing_state_args()
        sentence = pages_win.crossing_state_sentence(*args)
        if sentence != self.crossing_state.text():
            shot = motion.snapshot(self.crossing_state)
            self.crossing_state.setText(sentence)
            motion.fade_from(self.crossing_state, shot)
        armed = args[4]
        self.pause_button.setVisible(armed)
        motion.set_shown(self.pause_row, armed or pages_win.crossing_state_blocked(*args[:4]))
        outward = pages_win.outward_link_line(self.sender.connected)
        if outward != self.outward_line.text():
            shot = motion.snapshot(self.outward_line)
            self.outward_line.setText(outward)
            motion.fade_from(self.outward_line, shot)
        # "On your Mac" above already says this while redirecting; the hint is for the rest --
        # not connected, or the hooks failing to install -- and stays quiet in the boring case.
        send_hint = "" if self.sender.redirecting or self._sending_detail in (
            "Off", "Not connected to the Mac"
        ) else self._shown(self._sending_detail)
        if send_hint != self.send_hint.text():
            self.send_hint.setText(send_hint)
            # Hidden while empty, or its spacing leaves a gap between the two switches.
            self.send_hint.setVisible(bool(send_hint))
        self.sidebar.set_link(tone, SIDEBAR_LINK_WORDS.get(state, "Unknown"))
        self.sidebar.set_dots(pages_win.dots(self._config is None, self._firewall_tone))
        self._refresh_pairing()
        edge = self.server.return_edge
        resistance = self.server.return_resistance
        known = bool(edge) and resistance is not None
        text = f"{edge} edge · {resistance} px" if known else ""
        if text != self.return_readout.text():
            self.return_readout.setText(text)
        motion.set_shown(self.return_row, known)

    def _title(self) -> str:
        with self._status_lock:
            return self._shown(f"Beamer — {self._status.value}: {self._status_detail}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Beamer Windows receiver")
    parser.add_argument("--config", type=Path, default=default_config_path(), help="path to config.json")
    parser.add_argument("--hidden", action="store_true", help="start in the tray without showing the window")
    return parser.parse_args()


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Beamer Windows receiver must run on Windows")
    # use_last_error, then ctypes.get_last_error(): a plain GetLastError() call
    # through windll can read an error ctypes' own marshalling set in between,
    # and 183 (already exists) is the whole single-instance check.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex = kernel32.CreateMutexW(None, False, "Local\\Beamer.Receiver")
    already_running = ctypes.get_last_error() == 183
    if not mutex:
        raise OSError("Beamer could not create its single-instance lock")
    if already_running:
        kernel32.CloseHandle(mutex)
        return
    arguments = parse_args()
    log_path = configure_logging()
    LOGGER.info("Starting Beamer Windows receiver")
    if log_path is not None:
        LOGGER.info("Logging to %s", log_path)
    # Above normal, so a busy machine cannot starve the hooks: Windows removes a
    # low-level hook whose callback misses its timeout, silently and for good,
    # and every Beamer thread must win the CPU for the hook thread to get the
    # GIL. Beamer idles near 0%, so the class costs the rest of the machine
    # nothing (for example, a video pipeline holding half the CPU once left
    # the PC's mouse stranded on the Mac).
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    if not kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x8000):  # ABOVE_NORMAL_PRIORITY_CLASS
        LOGGER.warning("Could not raise Beamer's priority: %s", ctypes.WinError(ctypes.get_last_error()))
    try:
        app = QApplication(sys.argv)
        app.setApplicationName("Beamer")
        app.setQuitOnLastWindowClosed(False)
        theme.init_fonts()
        app.setFont(theme.font(theme.TYPE["body"]))
        application = WindowsApplication(arguments.config.expanduser().resolve())
        if not arguments.hidden:
            application.show()
        application.start()
        sys.exit(app.exec())
    finally:
        kernel32.CloseHandle(mutex)


def import_probe(out_path: str, names: list) -> int:
    """tools/import_probe.py's way into the built exe, which has no interpreter of its own to lend:
    import each named module from inside the build, write each outcome to out_path (a windowed
    exe has no console), and stop at the first that fails. Touches no config, lock or window."""
    import importlib
    import traceback

    with open(out_path, "w", encoding="utf-8") as out:
        for name in names:
            try:
                module = importlib.import_module(name)
            except BaseException:
                out.write(f"FAIL {name}\n{traceback.format_exc()}")
                return 1
            out.write(f"ok {name} {getattr(module, '__file__', None) or 'built in'}\n")
    return 0


if __name__ == "__main__":
    # Before the single-instance lock, so a Beamer already running does not turn the probe away.
    if sys.argv[1:2] == ["--import-probe"]:
        sys.exit(import_probe(sys.argv[2], sys.argv[3:]))
    main()
