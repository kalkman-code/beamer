import argparse
import ctypes
from dataclasses import replace
import functools
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
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
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
    SettingsFileError,
    TRIGGER_KEYS,
    TRIGGER_VKS,
    UNRECORDABLE_TRIGGER_VKS,
    default_config,
    default_config_path,
    load_config,
    save_config,
)
from core import effects
from diagram import ArrangementDiagram, PushStrip
from edge_glow import EdgeGlow, PreviewLoop
from effect_overlay import EffectOverlay, logical_point
from effect_previews import EffectStill, SwitchStill, TileHover
from core import ignored
import motion
from core import pairing
from core import keytable
from core import peerlist
from core.pairing import PAIRING_PORT, PairingService, local_address_towards
import machines_win
import pages_win
import hardware_win
import peers_view
import platform_parts
import qr_code
from core import protocol
from core import receiver
from core.receiver import LinkResponder, ServerState
from core import return_edge
from core import settings_sync
from core import ways
from sender import OWN_PLATFORM, LinkSender
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

STATUS_TITLES = {
    ServerState.STOPPED: "Receiver stopped",
    ServerState.WAITING: "Waiting for a machine",
    ServerState.CONNECTED: "Machine connected",
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
    "semantic": "On a Mac, Ctrl arrives as Command and the Windows key as Control, so Ctrl+C "
    "copies there too. Between two PCs every key arrives as itself.",
    "positional": "On a Mac, each key arrives as the Mac key in the same place: Ctrl as Control, the "
    "Windows key as Command. Between two PCs every key arrives as itself.",
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

# The OS parts keep their Windows names so the code and its tests read as before; on Linux they are
# the X11 or the portal back ends (platform_parts).
autostart_win = platform_parts.autostart
capture_win = platform_parts.capture
desktop_win = platform_parts.desktop
firewall_win = platform_parts.firewall


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


def arrival_method(edge: Optional[str], driver_platform: str) -> str:
    """How input arriving at `edge` came: a Mac's notch crossing lands on this PC's bottom edge,
    and that is the only arrival an effect draws differently. No edge is a peer's shortcut or menu."""
    if edge is None:
        return "switch"
    return "notch" if edge == "bottom" and keytable.is_mac(driver_platform) else "edge"


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
    # A pairing this PC hosted finished and was stored: the new peer entry.
    paired = Signal(object)
    # A pairing this PC requested ended: (the entry stored or None, the error or None).
    pair_finished = Signal(object, object)
    # The other direction: this PC's own input going to the Mac.
    sending = Signal(bool, str)
    redirecting = Signal(bool)
    # A peer began or stopped driving this PC: its id as base64, "" when none does.
    owner = Signal(str)
    # A link learnt something about a peer and saved it, or came up or went down.
    peers_changed = Signal()
    # Either link, telling us a peer's side of the arrangement changed at its end: (peer id, the
    # peer's edge that faces this PC, stamp, the machine that made the change, its way_back or None).
    arrangement = Signal(str, str, int, str, object)
    # Either link, carrying a peer's Same on all machines state: (peer id, data).
    settings = Signal(str, object)
    alert = Signal(str, str)
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
        self.bridge.pair_finished.connect(self._on_pair_finished)
        self.bridge.sending.connect(self._on_sending)
        self.bridge.redirecting.connect(self._on_redirecting)
        self.bridge.owner.connect(self._on_owner)
        self.bridge.peers_changed.connect(self._on_peers_changed)
        self.bridge.arrangement.connect(self._on_arrangement)
        self.bridge.settings.connect(self._on_settings)
        self.bridge.update.connect(self._on_update)
        self.bridge.rules_ready.connect(self._listen)
        self._update = None
        self._effects_failed = False
        self._code_shown = False
        self._code_address = ""
        self._qr_cache = (None, None)
        self._pairing_open = False
        self._pairing_machine = None
        self._pairing_target = ""
        self._peer_entries: list = []
        self._zone_entries: list = []
        # The machine the Crossing page shows and edits, by id; the first paired one until a choice.
        self._chosen: Optional[str] = None
        self._crossing_seen = None
        self._machine_items: tuple = ()
        self.machine_actions: dict = {}
        self._labels_by_id: dict = {}
        self._peers_tick = 0
        self._firewall_advice: Optional[firewall_win.Advice] = None
        self._firewall_status: Optional[firewall_win.FirewallStatus] = None
        self._firewall_tone: Optional[str] = None
        self._firewall_busy = False
        self._firewall_again = False
        self._firewall_auto_repaired = False
        self._host = default_config().host
        self._last_seen_state: Optional[ServerState] = None
        self._receiver_port: Optional[int] = None
        self.glow: Optional[EdgeGlow] = None
        self.effects: Optional[EffectOverlay] = None
        # The peers as the link layer reads and writes them, over settings.json and its lock.
        self.book = app_config.ReadFallbackPeerBook(
            lambda: app_config.load_settings(self.config_path),
            lambda settings: app_config.write_settings(self.config_path, settings),
            app_config.SETTINGS_LOCK,
        )
        self.server = LinkResponder(
            self.book,
            self._identity,
            status_callback=self._set_status,
            pressure_callback=lambda edge, pressure, crossed, part=None: self.bridge.pressure.emit(edge, pressure, crossed, part),
            arrival_callback=lambda edge, x, y: self.bridge.arrived.emit(
                arrival_method(edge, self._driver_platform()), edge or "", float(x), float(y)
            ),
            owner_callback=lambda peer: self.bridge.owner.emit(protocol.id_text(peer) if peer else ""),
            arrangement_callback=lambda peer, read: self.bridge.arrangement.emit(
                protocol.id_text(peer), read["edge"], read["set_at"], protocol.id_text(read["by"]), read.get("way_back")
            ),
            settings_callback=lambda peer, data: self.bridge.settings.emit(protocol.id_text(peer), data),
            paired_callback=self._store_paired,
            link_callback=lambda peer, up: self.bridge.peers_changed.emit(),
            announce=self._announce,
            away=lambda: self.sender.redirecting,
            zones=self._zones,
            clipboard=platform_parts.clipboard,
            unlock=platform_parts.unlock,
            hardware=hardware_win.hardware_address_towards,
            desktop=platform_parts.desktop,
            injector=platform_parts.injector,
        )
        # The links outwards: this PC's keyboard and mouse on its peers.
        self.sender = LinkSender(
            self.book,
            self._identity,
            hardware=hardware_win.hardware_address_towards,
            zones=self._zones,
            status_callback=self.bridge.sending.emit,
            redirect_callback=self.bridge.redirecting.emit,
            pressure_callback=self.bridge.pressure.emit,
            arrival_callback=lambda edge, x, y: self.bridge.arrived.emit(
                "switch" if edge is None else "edge", edge or "", float(x), float(y)
            ),
            desktop=platform_parts.desktop,
            clipboard=platform_parts.clipboard,
        )
        self.sender.driven = lambda: self.server.driven
        self.sender.send_peer_home = self.server.send_home
        # Same on all machines, over either link: applied on the GUI thread, and announced with
        # this PC's own arrangement the moment a link comes up.
        self.sender.settings_callback = lambda peer, data: self.bridge.settings.emit(protocol.id_text(peer), data)
        self.sender.arrangement_callback = lambda peer, read: self.bridge.arrangement.emit(
            protocol.id_text(peer), read["edge"], read["set_at"], protocol.id_text(read["by"]), read.get("way_back")
        )
        self.sender.link_callback = lambda peer, up: self.bridge.peers_changed.emit()
        self.sender.paired_callback = self._store_paired
        self.sender.hw_learned = self._learn_peer_hardware
        self.sender.announce = self._announce
        self.scope_labels: dict = {}
        self.own_notes: list = []
        self._same_seen = None
        # Pause crossing and the full-screen hold are about this screen: a peer's pointer does
        # not go home through a held edge either.
        self.server.edges_held = lambda: self.sender.edges_held
        self.sender.on_alert = self.bridge.alert.emit
        self.bridge.alert.connect(self._on_alert)
        self.hooks = capture_win.Hooks(self._on_hook_key, self._on_hook_mouse, self.sender.on_motion)
        # X11 cannot swallow one event, only grab the devices; the capture thread holds the grab
        # for as long as input is away.
        grab_while = getattr(self.hooks, "grab_while", None)
        if grab_while is not None:
            grab_while(lambda: self.sender.redirecting)
            self.hooks.on_failure = self._capture_stopped
        # Wayland: only the desktop captures input, at barriers on this machine's zones, and it
        # may end a capture itself, which brings input home.
        if hasattr(self.hooks, "barriers_from"):
            self.hooks.barriers_from(self.sender.zone_models)
            self.hooks.on_home = lambda: self.sender.set_redirecting(False)
            self.sender.input_held = lambda: self.hooks.holding
        if hasattr(platform_parts.injector, "on_problem"):
            platform_parts.injector.on_problem = lambda sentence: self.bridge.alert.emit("Beamer", sentence)
        self._trigger = capture_win.Trigger()
        self._sending_detail = "Not connected"
        self.update_checker = updates.Checker(
            VERSION, lambda: self._config is None or self._config.check_updates, self.bridge.update.emit, logger=LOGGER
        )
        self._paired = False
        try:
            # With nothing paired the window still has this PC's own settings to show and keep.
            self._config = load_config(config_path, unpaired_ok=True)
            self._same_seen = self._same_fields()
            self._apply_input_scale(self._config)
            self._host = self._config.host
            self._paired = bool(self.book.peers())
            if self._paired:
                self._status = ServerState.WAITING
                self._status_detail = f"Ready on TCP port {self._config.port}"
            else:
                self._status = ServerState.ERROR
                self._status_detail = "Not paired yet: press Pair a machine on Overview"
        except ConfigError as exc:
            LOGGER.info("Configuration is not ready: %s", exc)
            self._status = ServerState.ERROR
            self._status_detail = (
                "Settings could not be read: see the log" if isinstance(exc, SettingsFileError)
                else "Not paired yet: press Pair a machine on Overview"
            )

        # Announces this PC from launch, configured or not: pairing is how a fresh install gets its
        # first peer, so it cannot wait for the receiver to be listening. Without a readable
        # settings file there is no machine id to pair under, and so no service.
        self._settings_port, self._kept_hide = self._read_kept()
        self.pairing = self._make_pairing()
        self.sender.machines = self.pairing.machines if self.pairing is not None else (lambda: [])

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
        self.machines = machines_win.MachinesModule(
            self._set_peer_send, self._set_peer_allow, self._remove_peer, self._toggle_pairing_sheet
        )
        layout.addWidget(self.machines)
        self.sheet = machines_win.PairingSheet(
            self._show_code, self._cancel_code, self._pair_clicked, self._pick_machine
        )
        self.sheet.setVisible(False)
        layout.addWidget(self.sheet)
        self.link_module = self._link_module()
        layout.addWidget(self.link_module)
        layout.addWidget(self._input_module())
        layout.addWidget(self._same_module(current))
        self.sign_in_module = self._sign_in_module()
        layout.addWidget(self.sign_in_module)
        layout.addWidget(self._updates_module(current))
        self._refresh_peers()

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
        """The everyday buttons: send input across without the shortcut or an edge, and hold the
        edges for a while."""
        module = widgets.Module("Keyboard and mouse")
        self.redirect_button = QPushButton("Send input across")
        self.redirect_button.setProperty("vernier", "primary")
        self.redirect_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.redirect_button.clicked.connect(self.toggle_redirect)
        module.body.addWidget(self.redirect_button)
        # Why the button above is dimmed, while it is.
        self.redirect_note = widgets.label("Switch on This PC drives it for a machine above to send input from here.",
                                           "note", wrap=True)
        self.redirect_note.setVisible(False)
        module.body.addWidget(self.redirect_note)
        self.send_hint = widgets.label("", "note", wrap=True)
        self.send_hint.setVisible(False)
        module.body.addWidget(self.send_hint)
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
        own_note = self._own_note()
        module.body.addWidget(own_note)
        hold_note = widgets.label(
            "Off, your pointer can leave a full-screen game or video, and another machine's pointer can come "
            "home through this PC's edge. Useful if your keyboard has no key for the shortcut.", "note", wrap=True)
        module.body.addWidget(hold_note)
        self.wayland_crossing_note = widgets.label(
            "On Wayland Beamer cannot see a full-screen app, or a mouse button held before the pointer reaches an "
            "edge, so the edges are never held for one and a drag can cross.", "note", wrap=True)
        module.body.addWidget(self.wayland_crossing_note)
        for widget in (self.hold_switch, own_note, hold_note):
            widget.setVisible(not platform_parts.WAYLAND)
        self.wayland_crossing_note.setVisible(platform_parts.WAYLAND)
        return module

    def _redirect_text(self) -> str:
        """Names the machine the button sends input to, which is the one the shortcut would pick."""
        target = self.sender.shortcut_target()
        if target is not None and target in self._labels_by_id:
            return f"Send input to {self._labels_by_id[target]}"
        sendable = [entry for entry in self._peer_entries if entry.get("send")]
        # The entry migrated from 1.4.x has no id to pick it by until its first link.
        return f"Send input to {self._shown(peerlist.labels(self._peer_entries)[sendable[0]['token']])}" if len(sendable) == 1 else "Send input across"

    def toggle_redirect(self) -> None:
        self.sender.toggle()

    def toggle_pause(self) -> None:
        self.sender.crossing_paused = not self.sender.crossing_paused
        self._refresh_window()

    def _crossing_state_args(self) -> tuple:
        entries = self._peer_entries
        return (
            bool(entries), any(entry.get("host") for entry in entries), self._any_send(),
            bool(self.sender.peers_up()), bool(self._ways_in()),
            self.sender.crossing_paused, self.sender.full_screen_app, self._machines_name(),
        )

    def _machines_name(self) -> str:
        entries = self._peer_entries
        return self._shown(peerlist.labels(entries)[entries[0]["token"]]) if len(entries) == 1 else "your machines"

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

    def _same_module(self, current: Config) -> QWidget:
        module = widgets.Module("Settings")
        self.same_switch = widgets.Switch("Same on all machines")
        self.same_switch.setFont(theme.font(theme.TYPE["body"]))
        self.same_switch.setChecked(current.same_on_both)
        self.same_switch.toggled.connect(self._set_same)
        module.body.addWidget(self.same_switch)
        self.same_note = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.same_note)
        if self._config is None or not self._paired:
            self.same_switch.setEnabled(False)
        return module

    def _own_note(self) -> QWidget:
        """A note on a row this PC keeps to itself, shown only while the pages are kept in step."""
        note = widgets.label(pages_win.OWN_ROW, "note", wrap=True)
        note.setVisible(False)
        self.own_notes.append(note)
        return note

    def _show_same(self) -> None:
        if self._config is None:
            return
        old = False
        on = self._config.same_on_both
        if self.same_switch.isChecked() != on:
            self.same_switch.blockSignals(True)
            self.same_switch.setChecked(on)
            self.same_switch.blockSignals(False)
        self.same_switch.setEnabled(self._paired and not old)
        who = settings_sync.who([self._shown(label) for label in peerlist.labels(self._peer_entries).values()])
        self.same_note.setText(settings_sync.switch_note(who, self._config.same_on_both, old))
        widgets.set_role(self.same_note, "note-amber" if old else "note")
        for key, label in self.scope_labels.items():
            text = settings_sync.scope(key, who, on, pages_win.SCOPE.get(key))
            if label.text() != text:
                label.setText(text)
        for note in self.own_notes:
            if motion.target_shown(note) != on:
                motion.set_shown(note, on)

    def _same_fields(self) -> tuple:
        return tuple(settings_sync.pc_values(self._config).values())

    def _same_state(self) -> dict:
        config = self._config
        return settings_sync.message_data(
            config.same_on_both, config.same_set_at, settings_sync.pc_values(config), by=config.same_by
        )

    def _send_same(self, source: Optional[bytes] = None, data: Optional[dict] = None) -> None:
        """This PC's state, or `data` unchanged when a newer one arrived and goes on (section 10), to
        every peer that takes it but the one it came from: on the link this PC opened when that is
        up, else on the one the peer opened."""
        message = protocol.settings_msg(data if data is not None else self._same_state())
        for peer, caps in self._peer_caps().items():
            if peer != source and "settings" in caps:
                self._send_to(peer, message)

    def _peer_caps(self) -> dict:
        """{peer id: capabilities} for every peer with a link up, in either direction."""
        caps = {peer: self.sender.links.caps(peer) for peer in self.sender.peers_up()}
        for peer in self.server.links():
            caps.setdefault(peer, self.server.caps_of(peer) or frozenset())
        return caps

    def _send_to(self, peer: bytes, message: dict) -> bool:
        return self.sender.send_to(peer, message) or self.server.send(peer, message)

    def _announce(self, peer: bytes) -> list:
        """What this PC tells a peer on every new link (section 3): the side it holds for the peer,
        once one has been set; the peers it may send to, when it may send to this one; and its
        Same on all machines state. On the link's thread."""
        config = self._config
        if config is None:
            return []
        try:
            peers = self.book.peers()
        except Exception:
            LOGGER.exception("The peers could not be read for an announcement")
            return []
        entry = next((item for item in peers if protocol.read_id(item.get("id")) == peer), None)
        if entry is None:
            return []
        messages = []
        author = protocol.read_id(entry.get("side_by")) or protocol.read_id(config.machine_id)
        if entry.get("side") in return_edge.EDGES and entry.get("side_set_at") and author is not None:
            way_back = ways.has_way({"peers": peers, "zones": self.book.zones()}, entry["id"])
            messages.append(protocol.arrangement_v6(entry["side"], entry["side_set_at"], author, way_back=way_back))
        if entry.get("send"):
            others = [protocol.read_id(item.get("id")) for item in peers if protocol.read_id(item.get("id")) not in (None, peer)]
            messages.append(protocol.paired_msg(others[: protocol.MAX_PEERS]))
        if "settings" in self._peer_caps().get(peer, ()):
            messages.append(protocol.settings_msg(self._same_state()))
        return messages

    def _driver_platform(self) -> str:
        """The platform of the machine driving this PC now, or "" when none is. From the peers the
        window last read, not settings.json: this runs on the responder's thread as input lands."""
        owner = self.server.owner
        if owner is None:
            return ""
        text = protocol.id_text(owner)
        return next((peer.get("platform") or "" for peer in self._peer_entries if peer.get("id") == text), "")

    def _identity(self) -> dict:
        """This machine as a `hello` and a `welcome` say it (section 2)."""
        config = self._config or default_config()
        name = (config.name or pairing.machine_name())[: protocol.MAX_NAME_CHARS]
        return {
            "id": protocol.read_id(config.machine_id) or b"",
            "name": name,
            "platform": OWN_PLATFORM,
            "app": VERSION[:32] if VERSION.isascii() and VERSION.isprintable() else "dev",
            # A swipe has no shortcut every Linux desktop shares, so Linux does not offer gestures.
            "caps": [cap for cap in ("clipboard", "clipboard_image", "gestures", "media_keys", "text", "settings")
                     if not (cap == "gestures" and OWN_PLATFORM == "linux")],
            "port": self._settings_port,
        }

    def _zones(self) -> list:
        try:
            return self.book.zones()
        except (ConfigError, OSError):
            return []

    def _set_same(self, on: bool) -> None:
        """Turning it on carries this PC's Crossing and Design across; off, each end keeps what it
        has. Either way the Mac follows, now if a link is up, else when one next comes up."""
        if self._config is None:
            return
        self._config.same_on_both = bool(on)
        self._config.same_set_at = settings_sync.next_stamp(self._config.same_set_at, time.time())
        self._config.same_by = self._config.machine_id
        self._persist()
        self._send_same()
        self._show_same()

    def _on_settings(self, peer: str, data) -> None:
        """A peer's settings message, over either link. Its state stands only when it is newer than
        this PC's; then its values replace the shared ones here, and it goes on to every other peer
        that takes it, which stops the first time it meets a copy it already holds."""
        if self._config is None:
            return
        taken = settings_sync.arrived(
            data, self._config.same_set_at, TRIGGER_KEYS, by_here=self._config.same_by
        )
        if taken is None:
            return
        on, set_at, values = taken
        config = replace(self._config, crossing_methods=list(self._config.crossing_methods),
                         crossing_edge_parts=list(self._config.crossing_edge_parts))
        config.same_on_both = on
        config.same_set_at = set_at
        config.same_by = data.get("by", "") if isinstance(data.get("by"), str) else ""
        if on:
            settings_sync.apply_pc(config, values)
        try:
            app_config.validate_config(config)
        except ConfigError:
            LOGGER.exception("Settings from a peer could not be applied")
            return
        self._config = config
        self._same_seen = self._same_fields()
        if not self._persist():
            return
        LOGGER.info("Settings from a peer applied (same on all machines %s)", "on" if on else "off")
        self.sender.update_config(config)
        self._configure_trigger(config)
        self._reflect_config(config)
        self._show_same()
        self._send_same(source=protocol.read_id(peer), data=data)

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

    # -- Crossing -----------------------------------------------------------------------------

    def _crossing_page(self, layout, current: Config) -> None:
        layout.addWidget(self._ways_module(current))
        self.resistance_module = self._resistance_module(current)
        layout.addWidget(self.resistance_module)
        self.shortcut_module = self._shortcut_module(current)
        layout.addWidget(self.shortcut_module)
        self._reflect_ways()
        self._reflect_other_name()

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
        # Which machine everything below shows and edits; only there with more than one paired.
        holder = QWidget()
        holder.setProperty("vernier", "plain")
        self._machine_slot = QVBoxLayout(holder)
        self._machine_slot.setContentsMargins(0, 0, 0, 0)
        self.machine_choice = widgets.Choice((), 1, "")
        self._machine_slot.addWidget(self.machine_choice.view)
        self.machine_row = self._row(widgets.label("Machine", "key"), holder)
        # A wider step under the choice than between the rows it governs, so it reads as their heading.
        self.machine_row.layout().setContentsMargins(0, 0, 0, 8)
        self.machine_row.setVisible(False)
        module.body.addWidget(self.machine_row)
        self.ways_summary = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.ways_summary)
        self.arrangement_diagram = ArrangementDiagram()
        module.body.addWidget(self.arrangement_diagram)
        self.way_boxes: dict = {}
        for value, text, detail in pages_win.WAY_ROWS:
            box = QCheckBox(text)
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
        # A way refused because it would share a stretch of this screen with another machine's.
        self.clash_note = widgets.label("", "note-amber", wrap=True)
        self.clash_note.setVisible(False)
        module.body.addWidget(self.clash_note)
        # What stops a way working, under the ways it is about: a side another machine's edge holds,
        # a machine that may drive this PC and is not paired with the chosen one, and the chosen one
        # saying none of its own ways leads back here.
        self.blocked_note = widgets.label("", "note-amber", wrap=True)
        self.missing_note = widgets.label("", "note", wrap=True)
        self.no_way_back_note = widgets.label("", "note", wrap=True)
        for note in (self.blocked_note, self.missing_note, self.no_way_back_note):
            note.setVisible(False)
            module.body.addWidget(note)

        self.edge_choice = widgets.Choice(EDGE_CHOICES, columns=4, current="", on_change=self._set_arrangement)
        self.edge_heading = widgets.label("", "key")
        self.edge_note = widgets.label("", "note", wrap=True)
        self.edge_unlearned = widgets.label(pages_win.not_learned_edge(), "note", wrap=True)
        self.crossing_rows = {
            "edge": self._row(self.edge_heading, self.edge_note, self.edge_choice.view, self.edge_unlearned),
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
            button.clicked.connect(lambda on, p=part: self._set_part(p, on))
            chips.addWidget(button, 1)
            self.part_buttons[part] = button
        self.parts_note = widgets.label("", "note", wrap=True)
        self.crossing_rows["parts"] = self._row(widgets.label("Parts", "key"), chips, self.parts_note)

        self.corner_choice = widgets.Choice(CORNER_CHOICES, columns=2, current="", on_change=self._set_corner)
        self.corner_choice.set_names("Corner")
        self.crossing_rows["corner"] = self._row(widgets.label("Corner", "key"), self.corner_choice.view)

        self.dragging_switch = widgets.Switch("Don't cross while dragging")
        self.dragging_switch.setFont(theme.font(theme.TYPE["body"]))
        self.dragging_switch.setChecked(current.block_while_dragging)
        self.dragging_switch.toggled.connect(self._set_block_while_dragging)
        self.crossing_rows["dragging"] = self._row(self.dragging_switch)
        for key in ("edge", "parts", "corner", "dragging"):
            module.body.addWidget(self.crossing_rows[key])

        return module

    # -- the machine the Crossing page shows --

    def _crossing_settings(self) -> dict:
        """The peers and zones as last read, in the shape core.ways reads them."""
        own = self._config.machine_id if self._config is not None else ""
        return {"machine_id": own, "peers": self._peer_entries, "zones": self._zone_entries}

    def _read_crossing(self) -> None:
        try:
            self._peer_entries, self._zone_entries = self.book.peers(), self.book.zones()
        except (ConfigError, OSError):
            LOGGER.exception("The peers could not be read")

    def _crossing_machines(self) -> list:
        return [(ident, self._shown(label)) for ident, label in pages_win.crossing_machines(self._peer_entries)]

    def _chosen_machine(self) -> Optional[str]:
        ids = [ident for ident, _label in self._crossing_machines()]
        return self._chosen if self._chosen in ids else (ids[0] if ids else None)

    def _ways_in(self) -> set:
        """The kinds of zone in use towards any machine: what Pause holds and the Design page previews."""
        settings = self._crossing_settings()
        found = set()
        for ident, _label in self._crossing_machines():
            if ways.has_way(settings, ident):
                found.update(ways.ways(settings, ident)["methods"])
        return found

    def _choose_machine(self, peer: str) -> None:
        self._chosen = peer
        self._show_clash("")
        self._reflect_ways()
        self._reflect_look()

    def _show_clash(self, sentence: str) -> None:
        self.clash_note.setText(sentence)
        motion.set_shown(self.clash_note, bool(sentence))

    def _rebuild_machine_choice(self, machines: list) -> None:
        """The machine choice again when the paired machines or their names change."""
        items = tuple(machines)
        if items == self._machine_items:
            return
        self._machine_items = items
        old = self.machine_choice.view
        # Three names fit a row at the narrowest window; four or more go two to a row.
        columns = len(items) if len(items) <= 3 else 2
        self.machine_choice = widgets.Choice(items, max(1, columns), "", on_change=self._choose_machine)
        self.machine_choice.set_names("Machine")
        self._machine_slot.replaceWidget(old, self.machine_choice.view)
        old.deleteLater()

    def _write_ways(self, **changes) -> bool:
        """One change to the chosen machine's ways, written for that machine alone. A side that moved,
        or a way there that opened or closed, goes to it as `arrangement` (`_tell`); a change that would
        share a stretch of this screen with another machine's zone is refused, said, and every control
        goes back."""
        peer = self._chosen_machine()
        if self._config is None or peer is None:
            self._reflect_ways()
            return False
        self._read_crossing()
        had_way = ways.has_way(self._crossing_settings(), peer)
        held = ways.ways(self._crossing_settings(), peer)
        held.update(changes)
        try:
            moved = app_config.set_ways(self.config_path, peer, side=changes.get("side"), methods=held["methods"],
                                        parts=held["parts"], corner=held["corner"])
        except app_config.ClashError as exc:
            self._show_clash(self._shown(str(exc)))
            self._reflect_ways()
            return False
        except (ConfigError, OSError):
            LOGGER.exception("A machine's ways could not be saved")
            self._reflect_ways()
            return False
        self._show_clash("")
        self._read_crossing()
        if moved or ways.has_way(self._crossing_settings(), peer) != had_way:
            # One border, walked both ways: the machine on the other side of it moves its own side to match.
            self._tell(peer)
        # A change on the Crossing page reaches a peer's pointer while it is here, not at its next crossing.
        self.server.peers_changed()
        self.sender.refresh()
        self._reflect_ways()
        # The Design page's preview opens where the pointer crosses and says when a place is not a way in.
        self._reflect_look()
        return True

    def _ways_changed(self, way: str, on: bool) -> None:
        if self._config is None:
            return
        if way == "shortcut":
            # The shortcut is this PC's own, whichever machine the page shows.
            self._config.crossing_methods = pages_win.toggle_way(self._config.crossing_methods, way, on)
            self._persist()
            self.sender.update_config(self._config)
            self._reflect_ways()
            self._reflect_look()
            return
        held = ways.ways(self._crossing_settings(), self._chosen_machine())["methods"]
        self._write_ways(methods=[kind for kind in pages_win.toggle_way(held, way, on) if kind != "shortcut"])

    def _set_part(self, part: str, on: bool) -> None:
        held = ways.ways(self._crossing_settings(), self._chosen_machine())["parts"]
        self._write_ways(parts=pages_win.toggle_part(held, part, on))

    def _reflect_ways(self) -> None:
        """The machine choice, the ways' boxes, the parts' chips and which rows show, for the chosen
        machine: a row a chosen way does not use is hidden, not dimmed, and comes back the moment one
        does."""
        config = self._config or default_config()
        settings = self._crossing_settings()
        machines = self._crossing_machines()
        chosen = self._chosen_machine()
        several = len(machines) > 1
        self._rebuild_machine_choice(machines if several else [])
        self.machine_choice.set_value(chosen)
        motion.set_shown(self.machine_row, several)
        name = dict(machines).get(chosen) if several else None
        name = name or "the other machine"
        held = ways.ways(settings, chosen)
        shortcut = "shortcut" in config.crossing_methods
        methods = held["methods"] + (["shortcut"] if shortcut else [])
        for value, box in self.way_boxes.items():
            if box.isChecked() != (value in methods):
                box.blockSignals(True)
                box.setChecked(value in methods)
                box.blockSignals(False)
        side = held["side"]
        self.edge_choice.set_value(side)
        self.edge_choice.set_names(f"Where {name} is")
        self.edge_heading.setText(f"Where {name} is")
        self.edge_note.setText(f"One border, walked both ways, so changing it here moves it on {name} too.")
        self.edge_unlearned.setText(pages_win.not_learned_edge(name))
        self.corner_choice.set_value(held["corner"])
        edge = side or "right"
        names = pages_win.part_names(edge)
        for part, button in self.part_buttons.items():
            button.setText(names[part])
            button.setChecked(part in held["parts"])
        self.parts_note.setText(
            f"Crosses only along {pages_win.parts_phrase(edge, held['parts'])}; the rest of "
            "it is a wall. Choose any, but at least one."
        )
        # Resistance and the drag guard are this PC's own, there while any machine has a zone.
        own = {"dragging", "resistance"} & pages_win.crossing_rows(self._ways_in() | set(methods))
        shown = (pages_win.crossing_rows(methods) - {"dragging", "resistance"}) | own
        for key, row in self.crossing_rows.items():
            # Wayland shows no button held before the pointer meets the edge (wayland_crossing_note).
            motion.set_shown(row, key in shown and not (key == "dragging" and platform_parts.WAYLAND))
        motion.set_shown(self.resistance_module, "resistance" in shown)
        motion.set_shown(self.shortcut_module, "shortcut" in shown)
        key_name = TRIGGER_KEYS.get(config.trigger_key, config.trigger_key)
        summary = pages_win.ways_summary(methods, side, held["parts"], held["corner"], key_name, config.trigger_style,
                                         name, several)
        if summary != self.ways_summary.text():
            shot = motion.snapshot(self.ways_summary)
            self.ways_summary.setText(summary)
            motion.fade_from(self.ways_summary, shot)
        motion.set_shown(self.edge_unlearned, not side)
        for note, sentence in (
                (self.blocked_note, ways.blocked_sentence(settings, chosen, "this PC") if chosen else ""),
                (self.missing_note, ways.missing_sentence(settings, chosen, "this PC") if chosen else ""),
                (self.no_way_back_note, ways.no_way_back_sentence(settings, chosen) if chosen else "")):
            sentence = self._shown(sentence)
            if sentence != note.text():
                note.setText(sentence)
            motion.set_shown(note, bool(sentence))
        drawn = []
        for ident, label in machines or [("", "")]:
            found = ways.ways(settings, ident) if ident or machines else {"side": "", "methods": [], "parts": ["middle"],
                                                                         "corner": "top_left"}
            drawn.append(dict(found, key=ident, label=label, chosen=ident == chosen or not machines))
        self.arrangement_diagram.set_machines(drawn, key_name, config.trigger_style, shortcut)
        self.resistance_strip.set_edge(edge)

    def _set_arrangement(self, pc_edge: str) -> None:
        """The edge of THIS PC that leads to the chosen machine -- one border, walked either way. Not
        an ordinary change: both machines have to agree on it, so this end's change is stamped and
        sent to that machine over whichever link is up."""
        if ways.ways(self._crossing_settings(), self._chosen_machine())["side"] == pc_edge:
            return
        self._write_ways(side=pc_edge)

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
        self._write_ways(corner=corner)

    def _tell(self, peer: str) -> None:
        """This PC's side for `peer` as held, stamped as held, with whether a way leads there (WIRE.md
        section 8), over whichever link is up. Nothing while no side is set."""
        settings = self._crossing_settings()
        entry = next((item for item in settings["peers"] if item.get("id") == peer), None)
        if entry is None or entry.get("side") not in return_edge.EDGES or not entry.get("side_set_at"):
            return
        author = protocol.read_id(entry.get("side_by")) or protocol.read_id(self._config.machine_id)
        target = protocol.read_id(peer)
        if author is None or target is None:
            return
        self._send_to(target, protocol.arrangement_v6(entry["side"], entry["side_set_at"], author,
                                                      way_back=ways.has_way(settings, peer)))

    def _on_arrangement(self, peer: str, edge: str, set_at: int, by: str, way_back=None) -> None:
        """A peer changed the arrangement, over either link. `edge` is the edge of the PEER that
        faces this PC; one older than what this end holds, by stamp and then by id, is ignored. A
        side that makes two zones cover one stretch turns the peer's clashing zones off. `way_back`
        is kept whatever the side; a message that changed anything here is answered with this PC's
        own, and one that changed nothing is not, so two machines never answer in turn."""
        if self._config is None:
            return
        try:
            changed, notices = app_config.apply_arrangement(self.config_path, peer, edge, set_at, by, way_back)
        except (ConfigError, OSError):
            LOGGER.exception("An arrangement could not be saved")
            return
        if not changed:
            return
        for notice in notices:
            self._on_alert("Beamer", notice)
        self._pull_peer_fields()
        self.sender.refresh()
        self.server.peers_changed()
        self._tell(peer)

    def _pull_peer_fields(self) -> None:
        """What links wrote into the first peer, into the window's flat view, and every machine's side
        and zones as links and arrangements left them, into the Crossing page."""
        if self._config is None:
            return
        try:
            learned = app_config.learned_fields(self.config_path)
        except (ConfigError, OSError):
            LOGGER.exception("The peers could not be read")
            return
        for name, value in learned.items():
            setattr(self._config, name, value)
        self.sender.update_config(self._config)
        self._read_crossing()
        self._reflect_look()
        self._reflect_ways()

    def _resistance_module(self, current: Config) -> QWidget:
        module = widgets.Module("Resistance")
        self.resistance_strip = PushStrip()
        self.resistance_strip.set_edge("right")
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
        """How a driving machine's pointer feels on this PC: it sends what its own acceleration made of
        the hand's movement, and this PC's settings decide the rest."""
        module = widgets.Module("Another machine's pointer here")
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
            "For the trackpad or mouse of whichever machine drives this PC.", "note", wrap=True
        ))
        self.reverse_scroll_switch = widgets.Switch("Reverse scrolling from other machines")
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
            "These keep working on this PC while its input is on another machine: a mouse's back button for "
            "this PC's browser, say, or a volume key for its speakers."
            if entries
            else "Nothing yet. Every key and button goes to the machine that has input. Add one to keep "
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
                "Use this key to send input to another machine, and to bring it back.", "note", wrap=True
            )
        )
        self.wayland_shortcut_note = widgets.label(
            "On Wayland the shortcut only brings input back: the desktop lets input go to another machine only "
            "when the pointer pushes through an edge, and keys on the Stays on this PC list cannot stay here "
            "while input is away.", "note", wrap=True)
        self.wayland_shortcut_note.setVisible(platform_parts.WAYLAND)
        module.body.addWidget(self.wayland_shortcut_note)
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
            "Input is on another machine for as long as the key is held."
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
                "Lights this PC as you push toward another machine. Switched off, crossing still works. "
                "Each machine sets how its own edge looks.",
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
        edge = ways.ways(self._crossing_settings(), self._chosen_machine())["side"] or "right"
        colour = self.glow_colour_choice.value or "signal"
        methods = sorted(self._ways_in())
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
            "On Wayland the desktop does not let Beamer draw at the edges of the screen, so crossings show no "
            "effect on this machine. The other machines still show theirs."
            if platform_parts.WAYLAND
            else "The effects stopped working and are off until Beamer restarts, so crossings show Glow in "
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
        # A change to a shared setting while Same on all machines is on is stamped here, where
        # every page's changes are saved, and sent once saved.
        fields = self._same_fields()
        shared = self._same_seen is not None and fields != self._same_seen and self._config.same_on_both
        self._same_seen = fields
        if shared:
            self._config.same_set_at = settings_sync.next_stamp(self._config.same_set_at, time.time())
            self._config.same_by = self._config.machine_id
        try:
            with app_config.SETTINGS_LOCK:
                # What links wrote since this was loaded (an address, a hardware address, a name)
                # is not this window's to overwrite with what it last read.
                learned = app_config.learned_fields(self.config_path)
                # A fold or a new pairing made another entry the first: the flat view follows it
                # whole, or this save would put the old one back.
                names = app_config.LEARNED if learned["auth_token"] != self._config.auth_token else app_config.LINK_WRITTEN
                for name in names:
                    setattr(self._config, name, learned[name])
                save_config(self.config_path, self._config)
            # A change on the Crossing page reaches a peer's pointer while it is here, not at its
            # next crossing: its way on is built from these settings when it arrives.
            self.server.peers_changed()
            self.sender.refresh()
            if shared:
                self._send_same()
            return True
        except (ConfigError, OSError):
            LOGGER.exception("Setting could not be saved")
            return False

    # -- Machines and pairing ---------------------------------------------------------------

    def _read_kept(self) -> tuple:
        """This PC's port and Hide addresses as settings.json keeps them, which hold with no machine paired."""
        try:
            settings = app_config.load_settings(self.config_path)
        except (ConfigError, OSError):
            return default_config().port, False
        return settings["port"], bool(settings.get("hide_addresses"))

    def _hide_addresses(self) -> bool:
        return self._config.hide_addresses if self._config is not None else self._kept_hide

    def _make_pairing(self) -> Optional[PairingService]:
        try:
            settings = app_config.load_settings(self.config_path)
        except (ConfigError, OSError):
            LOGGER.exception("Pairing is unavailable: the settings could not be read")
            return None
        identity = protocol.read_id(settings["machine_id"])
        if identity is None:
            return None
        name = settings.get("name") or pairing.machine_name()
        return PairingService(
            name, identity, OWN_PLATFORM, self._announced_port, self.book.peers, self._store_peer,
            on_paired=self.bridge.paired.emit, logger=LOGGER,
        )

    def _store_peer(self, entry: dict, replaced) -> None:
        """PairingService's write, on a socket thread with the service's lock held: the new peer takes
        the place of the entry it replaces (WIRE.md section 6, item 5). It must never wait on the GUI
        thread, and nothing here may call the service."""
        with app_config.SETTINGS_LOCK:
            settings = app_config.load_settings(self.config_path)
            if replaced is not None:
                # `replaced` was read before this lock: it may have changed or gone since
                # (WIRE.md section 6, item 4).
                held = next((peer for peer in settings["peers"] if peer.get("token") == replaced.get("token")), None)
                if held is None or held.get("id") != replaced.get("id") or held.get("linked") != replaced.get("linked"):
                    raise ConfigError("the machine being replaced changed while pairing")
            peerlist.add_peer(settings, entry, replaced)
            app_config.write_settings(self.config_path, settings)

    def _peer_items(self, peers: list) -> list:
        """(entry, label, state, where) for every paired machine, from what the links know of it now."""
        labels = peerlist.labels(peers)
        hide = self._hide_addresses()
        driver, here, live = self.server.owner, self.sender.owner, self.server.links()
        items = []
        self._labels_by_id = {}
        for entry in peers:
            peer = protocol.read_id(entry.get("id"))
            token = entry["token"]
            labels[token] = self._shown(labels[token])
            if peer is not None:
                self._labels_by_id[peer] = labels[token]
            state = peers_view.describe(
                entry, labels[token],
                up=peer is not None and (self.sender.links.up(peer) or peer in live),
                driving=peer is not None and peer == driver,
                input_there=peer is not None and peer == here,
                kind=self.sender.links.kind(token), status=self.sender.links.status(token),
                waking=peer is not None and self.sender.is_waking(peer),
            )
            state = replace(state, detail=self._shown(state.detail))
            where = f"{peers_view.platform_name(entry)} · {peers_view.address_line(entry, hide)}"
            items.append((entry, labels[token], state, where))
        return items

    def _refresh_peers(self) -> None:
        try:
            peers, zones = self.book.peers(), self.book.zones()
        except (ConfigError, OSError):
            LOGGER.exception("The peers could not be read")
            peers, zones = [], []
        self._peer_entries, self._zone_entries = peers, zones
        self.machines.set_peers(self._peer_items(peers))
        if hasattr(self, "arrangement_diagram"):
            self._reflect_other_name()
            # A machine paired, removed or renamed, or a side or zone a link wrote: the Crossing page follows.
            seen = repr((peers, zones, self._hide_addresses()))
            if seen != self._crossing_seen:
                self._crossing_seen = seen
                self._reflect_ways()

    def _reflect_other_name(self) -> None:
        name = self._machines_name() if len(self._peer_entries) == 1 else ""
        self.resistance_strip.set_other_name(name)

    def _refresh_peer_states(self) -> None:
        """The rows' states again from the peers last read: the links move faster than the file does."""
        self.machines.set_peers(self._peer_items(self._peer_entries))

    def _any_send(self) -> bool:
        return any(entry.get("send") and protocol.linkable(entry) for entry in self._peer_entries)

    def _label_of(self, peer: Optional[bytes]) -> str:
        return self._labels_by_id.get(peer, "another machine") if peer is not None else ""

    def _edit_peer(self, token: str, **fields) -> bool:
        try:
            with app_config.SETTINGS_LOCK:
                settings = app_config.load_settings(self.config_path)
                entry = next((item for item in settings["peers"] if item.get("token") == token), None)
                if entry is None:
                    return False
                entry.update(fields)
                app_config.write_settings(self.config_path, settings)
        except (ConfigError, OSError):
            LOGGER.exception("A machine's setting could not be saved")
            return False
        return True

    def _set_peer_send(self, token: str, enabled: bool) -> None:
        """This PC's input may go to that machine or not. Takes effect at once: the hooks that watch this
        PC's own keyboard are in only while some machine can be sent to."""
        # Home before the save: the links read `send` from the settings themselves, and one that read
        # it first would end with input still on its machine. Home after a failed save is harmless.
        if not enabled and self._input_is_on(token):
            self.sender.set_redirecting(False)
        if self._edit_peer(token, send=bool(enabled)):
            self._after_peers_edit()

    def _input_is_on(self, token: str) -> bool:
        """Whether this PC's input is on the machine paired under `token`."""
        on = self.sender.owner
        return on is not None and any(
            protocol.read_id(entry.get("id")) == on for entry in self._peer_entries if entry.get("token") == token
        )

    def _set_peer_allow(self, token: str, enabled: bool) -> None:
        """That machine may drive this PC or not. The listener stays up either way, since arrangements and
        settings still travel on its link; off ends its ownership and tells it (section 4)."""
        if self._edit_peer(token, allow_drive=bool(enabled)):
            self._after_peers_edit()

    def _set_every_peer(self, field: str, enabled: bool) -> None:
        """The tray's two ticks: one direction for every paired machine at once."""
        if field == "send" and not enabled and self.sender.owner is not None:
            # Home first, as _set_peer_send does: before the save the links read `send` from.
            self.sender.set_redirecting(False)
        try:
            with app_config.SETTINGS_LOCK:
                settings = app_config.load_settings(self.config_path)
                for entry in settings["peers"]:
                    entry[field] = bool(enabled)
                app_config.write_settings(self.config_path, settings)
        except (ConfigError, OSError):
            LOGGER.exception("The machines' settings could not be saved")
            return
        self._after_peers_edit()

    def _refresh_tray_directions(self) -> None:
        """The tray's ticks name who they cover and show whether every machine has the direction on."""
        entries = self._peer_entries
        labels = peerlist.labels(entries)
        who = self._shown(labels[entries[0]["token"]]) if len(entries) == 1 else "machines"
        for action, field, text in (
            (self.drive_action, "allow_drive", f"{who} drives this PC" if len(entries) == 1 else "Machines drive this PC"),
            (self.send_action, "send", f"This PC drives {who}"),
        ):
            action.setEnabled(bool(entries))
            if action.text() != text:
                action.setText(text)
            on = bool(entries) and all(entry.get(field) for entry in entries)
            if action.isChecked() != on:
                action.setChecked(on)

    def _refresh_tray_machines(self) -> None:
        """With more than one machine to send to, the tray has "Send input to" for each, and the toggle
        is only there to bring input back; with one, the toggle names it, as it always did."""
        labels = peerlist.labels(self._peer_entries)
        sendable = [(entry["id"], f"Send input to {self._shown(labels[entry['token']])}") for entry in self._peer_entries
                    if entry.get("send") and entry.get("port") and protocol.read_id(entry.get("id")) is not None]
        items = sendable if len(sendable) > 1 else []
        if items != [(ident, action.text()) for ident, action in self.machine_actions.items()]:
            for action in self.machine_actions.values():
                self.tray_menu.removeAction(action)
                action.deleteLater()
            self.machine_actions = {}
            for ident, text in items:
                action = QAction(text, self.tray_menu)
                action.triggered.connect(lambda _checked=False, peer=protocol.read_id(ident): self.sender.go(peer))
                self.tray_menu.insertAction(self.pause_action, action)
                self.machine_actions[ident] = action
        on = self.sender.owner
        for ident, action in self.machine_actions.items():
            action.setEnabled(protocol.read_id(ident) != on)
        self.redirect_action.setVisible(not self.machine_actions or self.sender.redirecting)

    def _after_peers_edit(self) -> None:
        """Whatever changed the peers list: the flat view and the links follow, and the machines are listed again."""
        if self._config is not None and not self._paired:
            try:
                config = load_config(self.config_path)
            except (ConfigError, OSError):
                config = None
            if config is not None:
                self._paired = True
                self._host = config.host
                self._apply_config(config)
                self._configure_trigger(config)
                self._reflect_config(config)
        elif self._config is not None:
            self._pull_peer_fields()
        self.sender.refresh()
        self.server.peers_changed()
        self._send_paired()
        self._refresh_peers()
        self._sync_sending()

    def _sync_sending(self) -> None:
        """The links and the hooks that feed them run while some machine can be sent to, whatever changed the list."""
        if self._config is None:
            return
        if self._any_send():
            self._start_sending(self._config)
        else:
            self.sender.set_redirecting(False)
            self._stop_sending()

    def _remove_peer(self, token: str) -> None:
        try:
            with app_config.SETTINGS_LOCK:
                settings = app_config.load_settings(self.config_path)
                removed = peerlist.remove_peer(settings, token)
                if removed is None:
                    return
                app_config.write_settings(self.config_path, settings)
                remaining = bool(settings["peers"])
        except (ConfigError, OSError):
            LOGGER.exception("A machine could not be removed")
            self._on_alert("Beamer", "Beamer could not save the removal. Try again in a moment.")
            return
        LOGGER.info("Removed %s from the paired machines", removed.get("name") or "a machine")
        if remaining:
            self._after_peers_edit()
            return
        self.sender.set_redirecting(False)
        self._stop_sending()
        self._paired = False
        try:
            self._config = load_config(self.config_path, unpaired_ok=True)
        except (ConfigError, OSError):
            self._config = None
        self._same_seen = self._same_fields() if self._config is not None else None
        self.sender.update_config(self._config)
        if self._config is not None:
            self._reflect_config(self._config)
        self.server.peers_changed()
        self.sender.refresh()
        self.same_switch.setEnabled(False)
        self._refresh_peers()

    def _toggle_pairing_sheet(self) -> None:
        self._pairing_open = not self._pairing_open
        if not self._pairing_open and self.pairing is not None:
            self.pairing.cancel_pairing()
        self.sheet.setVisible(self._pairing_open)
        self.machines.set_pairing_open(self._pairing_open)
        self._refresh_pairing()

    def _show_code(self) -> None:
        if self.pairing is None:
            self.sheet.say_host("Pairing is not available: this PC's settings could not be read.", "note-fault")
            return
        if self.pairing.error:
            self.sheet.say_host(f"Pairing is not available: {self.pairing.error}", "note-fault")
            return
        self.sheet.say_host("", "note")
        try:
            self.pairing.begin_pairing()
        except pairing.PairingError as exc:
            text = peerlist.host_outcome_text("full", None) if str(exc) == pairing.ERROR_FULL else f"Pairing is not available: {exc}"
            self.sheet.say_host(text, "note-fault")
            return
        self._code_address = self._pairing_address()
        self._refresh_pairing()

    def _cancel_code(self) -> None:
        if self.pairing is not None:
            self.pairing.cancel_pairing()
        self._refresh_pairing()

    def _pairing_address(self) -> str:
        """The one address worth typing on another machine: this PC's on the network that reaches the
        machine it last knew, else on the default route (WIRE.md section 6)."""
        for peer in self._peer_entries:
            try:
                address = local_address_towards(peer.get("host") or "192.0.2.1")
            except OSError:
                continue
            if address and not address.startswith(("127.", "0.")):
                return address
        return pairing.pairing_address() or ""

    def _refresh_pairing(self) -> None:
        service = self.pairing
        if service is None:
            return
        if self._pairing_open:
            self.sheet.set_machines(service.machines(), self._shown)
        code = service.code
        if code is not None:
            hide = self._hide_addresses()
            address = getattr(self, "_code_address", "") if self._code_shown else self._pairing_address()
            self._code_address = address
            rows, note = None, ""
            if hide:
                note = "The QR holds this PC's address, so it is not shown while Hide addresses is on."
            else:
                text = service.qr_text(address) if address else None
                if text is not None:
                    if self._qr_cache[0] != text:
                        self._qr_cache = (text, qr_code.modules(text))
                    rows = self._qr_cache[1]
            where = f"This PC's address: {address} · port {service.tcp_port}" if address else ""
            self.sheet.show_code(code, service.seconds_left, self._shown(where), rows, note)
            self._code_shown = True
            return
        if not self._code_shown:
            return
        self._code_shown = False
        self.sheet.hide_code()
        outcome = service.outcome
        if outcome == "paired":
            return
        text = self._shown(peerlist.host_outcome_text(outcome, service.known))
        self.sheet.say_host(text, "note-amber" if outcome == "expired" else "note-fault")

    def _pick_machine(self, machine: Optional[dict]) -> None:
        self._pairing_machine = machine
        if machine is not None and machine.get("pairing") != pairing.PAIRING_V3:
            self.sheet.say_pair(peerlist.pairing_error_text(pairing.PairingError(pairing.ERROR_VERSION), machine["name"]), "note-fault")
        else:
            self.sheet.say_pair("", "note")

    def _pair_clicked(self, machine: Optional[dict], address: str, code: str) -> None:
        service = self.pairing
        if service is None:
            self.sheet.say_pair("Pairing is not available: this PC's settings could not be read.", "note-fault")
            return
        self._pairing_target = machine["name"] if machine is not None else address
        self.sheet.say_pair("", "note")
        self.sheet.busy(True)

        def work() -> None:
            try:
                entry = service.pair(machine, code) if machine is not None else service.pair_by_address(address, code)
            except Exception as exc:
                if not isinstance(exc, pairing.PairingError):
                    LOGGER.exception("Pairing failed")
                self.bridge.pair_finished.emit(None, exc)
                return
            self.bridge.pair_finished.emit(entry, None)

        threading.Thread(target=work, name="Beamer-pair", daemon=True).start()

    def _on_pair_finished(self, entry, error) -> None:
        if self._closing:
            return
        self.sheet.busy(False)
        if error is not None:
            self.sheet.say_pair(self._shown(peerlist.pairing_error_text(error, self._pairing_target or "That machine")), "note-fault")
            return
        self.sheet.clear_code()
        self.sheet.say_pair(self._shown(f"Paired with {entry['name']}."), "note-live")
        LOGGER.info("Paired with %s", entry["name"])
        self._after_peers_edit()

    def _on_paired(self, entry) -> None:
        """A machine paired with the code this PC was showing, and its entry is stored."""
        if self._closing:
            return
        self.sheet.say_host(self._shown(peerlist.host_outcome_text("paired", entry)), "note-live")
        self._after_peers_edit()

    # -- Connection -------------------------------------------------------------------------

    def _connection_page(self, layout, current: Config) -> None:
        self._firewall_module(layout)
        privacy = widgets.Module("Addresses")
        self.hide_switch = widgets.Switch("Hide addresses")
        self.hide_switch.setFont(theme.font(theme.TYPE["body"]))
        self.hide_switch.setChecked(self._hide_addresses())
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

        # Read-only: the address that faces the machines this PC knows, which is what another machine
        # types to pair by address.
        self.host_readout = widgets.label(self._shown(self._host) or "Not known yet", "readout", wrap=True)
        self.host_readout.setAccessibleName("This PC's address")
        key = widgets.label("This PC's address", "key")
        key.setBuddy(self.host_readout)
        fields.addWidget(key, 0, 0)
        fields.addWidget(self.host_readout, 0, 1, 1, 2)
        self.port_entry = QLineEdit(str(current.port))
        field(1, "Port", self.port_entry)
        module.body.addLayout(fields)
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
        return pages_win.redact(text, self._hide_addresses())

    def _set_hide_addresses(self, enabled: bool) -> None:
        if self._config is None:
            self._kept_hide = bool(enabled)
            try:
                app_config.set_own(self.config_path, hide_addresses=self._kept_hide)
            except (ConfigError, OSError):
                LOGGER.exception("Hide addresses could not be saved")
        else:
            self._config.hide_addresses = bool(enabled)
            self._persist()
        self.host_readout.setText(self._shown(self._host) or "Not known yet")
        self._refresh_peers()
        self._refresh_pairing()
        self._refresh_window()
        self.tray.setToolTip(self._title())
        self.status_action.setText(self._title())

    def save(self) -> None:
        """The Connection page's one setting is this PC's own port. It never touches a peer's entry, whose
        port is that machine's own, and it works with nothing paired."""
        try:
            port = int(self.port_entry.text().strip())
            if not 1 <= port <= 65535:
                raise ValueError
            app_config.set_own(self.config_path, port=port)
        except (TypeError, ValueError):
            self.save_message.setText("port must be an integer from 1 to 65535")
            self.save_message.setVisible(True)
            widgets.set_role(self.save_message, "note-fault")
            return
        except (ConfigError, OSError) as exc:
            LOGGER.exception("Configuration could not be saved")
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self._settings_port = port
        if self._config is not None:
            config = replace(self._config, port=port)
            if self._paired:
                self._apply_config(config)
            else:
                self._config = config
        self.save_message.setText("Saved.")
        self.save_message.setVisible(True)
        widgets.set_role(self.save_message, "note-live")

    def _apply_config(self, config: Config) -> None:
        self._config = config
        self._settings_port = config.port
        self._same_seen = self._same_fields()
        self._apply_input_scale(config)
        if not config.edge_glow:
            self._hide_crossing()
        self.same_switch.setEnabled(self._paired)
        if self._paired:
            self._start_receiver(config)
        self.server.peers_changed()
        self._send_paired()
        self.sender.update_config(config)
        self._refresh_peers()
        if self._any_send():
            self._start_sending(config)
        else:
            self._stop_sending()
        self.host_readout.setText(self._shown(config.host) or "Not known yet")

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
            and firewall_win.missing_rule(status)
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
        self.tray_menu = menu
        self.redirect_action = menu.addAction("Send input across")
        self.redirect_action.triggered.connect(self.toggle_redirect)
        self.pause_action = menu.addAction("Pause crossing")
        self.pause_action.triggered.connect(self.toggle_pause)
        menu.addSeparator()
        # One tick per direction, each the same switch the Overview page shows, so either
        # direction can be turned off while the other keeps working and the two never disagree.
        self.drive_action = menu.addAction("Machines drive this PC")
        self.drive_action.setCheckable(True)
        self.drive_action.triggered.connect(lambda on: self._set_every_peer("allow_drive", on))
        self.send_action = menu.addAction("This PC drives machines")
        self.send_action.setCheckable(True)
        self.send_action.triggered.connect(lambda on: self._set_every_peer("send", on))
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
        """Re-reads settings.json, for the times it was edited outside the window, and shows it."""
        try:
            config = load_config(self.config_path, unpaired_ok=True)
        except (ConfigError, OSError) as exc:
            LOGGER.warning("Configuration not reloaded: %s", exc)
            self._on_alert("Beamer", f"Could not reload the configuration: {exc}")
            return
        self._host = config.host
        self._paired = bool(self.book.peers())
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
        self.host_readout.setText(self._shown(config.host) or "Not known yet")
        self.port_entry.setText(str(config.port))
        self._refresh_peers()

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def start(self) -> None:
        if not sys.platform.startswith("linux"):
            # No release has a Linux build yet, so a newer one is nothing this machine can install.
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
            if firewall_win.missing_rule(status):
                LOGGER.info("Adding this PC's firewall rules before listening")
                firewall_win.repair(exe, port, ("Private",))
        except Exception:
            LOGGER.exception("The firewall rules could not be added before listening")
        self.bridge.rules_ready.emit()

    def _listen(self) -> None:
        if self._closing:
            return
        if self.pairing is not None:
            self.pairing.start()
        if self._config is not None and self._paired:
            self._start_receiver(self._config)
            self._start_sending(self._config)
            # Wayland's remote control session, opened now so the desktop asks while someone is at
            # this machine; the clipboard rides on it.
            start_injector = getattr(platform_parts.injector, "start", None)
            if start_injector is not None:
                start_injector()
        # After the rules, so the firewall module reads what start() just wrote, and with no config
        # the receiver never starts, so nothing else would ever read it.
        self._check_firewall()

    def _configure_trigger(self, config: Config) -> None:
        self._trigger.configure(config.trigger_key, config.trigger_style, config.double_tap_ms)

    def _start_receiver(self, config: Config) -> None:
        if self.server.listening and self._receiver_port != config.port:
            self.server.stop()
        self._receiver_port = config.port
        self.server.start(config.port)

    def _start_sending(self, config: Config) -> None:
        """The outward link and the hooks that feed it. The hooks go in only
        when sending is on: they are the one part of Beamer that can take this
        PC's own keyboard away, so an install that never sends never installs
        them."""
        if not self._any_send():
            return
        self._configure_trigger(config)
        try:
            self.hooks.start()
        except Exception as exc:
            LOGGER.exception("The input hooks could not be installed")
            self._sending_detail = f"This PC's keyboard could not be captured: {exc}"
            return
        self.sender.start(config)

    def _capture_stopped(self, error: BaseException) -> None:
        """On the capture thread, when capture ended on its own (the X server gone, a stalled
        thread's connection ended, the desktop refusing or ending a portal session): nothing holds
        this machine's keyboard any more, so input comes home rather than staying on a peer no key
        can reach. A portal's error carries the whole sentence."""
        self.sender.set_redirecting(False)
        sentence = getattr(error, "sentence", None) or f"This machine's keyboard and mouse are no longer captured: {error}"
        self.bridge.alert.emit("Beamer", sentence)

    def _on_hook_mouse(self, message: int, x: int, y: int, mouse_data: int) -> bool:
        """Every mouse message, on the hook thread: what Windows said becomes the sender's neutral
        call. True when the event belongs to a peer and Windows must not see it."""
        event = capture_win.mouse_event(message, mouse_data)
        if event is None:
            return False
        if event[0] == "button":
            return self.sender.on_button(event[1], event[2])
        if event[0] == "wheel":
            return self.sender.on_wheel(event[1], event[2])
        return self.sender.on_pointer_move()

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

    def _store_paired(self, peer: bytes, ids: list) -> None:
        """The machines a peer says it is paired with, for the Crossing page, on the link's thread."""
        try:
            self.book.store_paired(protocol.id_text(peer), [protocol.id_text(item) for item in ids])
        except (ConfigError, OSError):
            LOGGER.exception("The peers a peer is paired with could not be saved")

    def _send_paired(self) -> None:
        """Every peer this PC may send to is told the others it has, when the list changes."""
        try:
            peers = self.book.peers()
        except Exception:
            return
        ids = [protocol.read_id(item.get("id")) for item in peers]
        for item, peer in zip(peers, ids):
            if peer is not None and item.get("send"):
                self._send_to(peer, protocol.paired_msg([other for other in ids if other not in (None, peer)][: protocol.MAX_PEERS]))

    def _learn_peer_hardware(self, peer: bytes, address: str) -> None:
        """A peer's hardware address from the ARP table, off the GUI thread."""
        try:
            changed = app_config.set_peer_hardware_address(self.config_path, protocol.id_text(peer), address)
        except (ConfigError, OSError):
            LOGGER.exception("A hardware address could not be saved")
            return
        if changed:
            self.bridge.peers_changed.emit()

    def _on_owner(self, peer: str) -> None:
        """A peer took input on this PC, or gave it back. Either way the outward edge follows:
        this PC's input is never here and elsewhere at once."""
        self.sender.set_receiving(bool(peer))

    def _on_peers_changed(self) -> None:
        """A link learnt something about a peer (an address, an id, a name, a hardware address) and
        saved it, or came up or went down: the window's flat view follows, and so do the links."""
        if self._config is None or self._closing:
            return
        self._pull_peer_fields()
        self.sender.refresh()
        self._refresh_peers()

    def _on_sending(self, connected: bool, detail: str) -> None:
        self._sending_detail = detail

    def _on_redirecting(self, redirecting: bool) -> None:
        self._sending_detail = (
            f"This PC's keyboard and mouse are on {self._label_of(self.sender.owner)}" if redirecting else self.sender.status
        )

    def _announced_port(self) -> int:
        return self._settings_port

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
            f"<b>Beamer {VERSION}</b><br>One keyboard and mouse for all your Macs and PCs.<br><br>"
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
        if self.pairing is not None:
            self.pairing.stop()
        self.server.stop()
        self._stop_sending()
        stop_injector = getattr(platform_parts.injector, "stop", None)
        if stop_injector is not None:
            stop_injector()
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
        if self._closing or self._config is None or not self._config.edge_glow or platform_parts.WAYLAND:
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
        if platform_parts.WAYLAND:
            return
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
        # The machines' names go in the detail, not the heading: a long name wrapped the heading onto
        # two lines at 640 wide and made the window scroll.
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
        location = f"On {self._label_of(self.sender.owner)}" if self.sender.redirecting else "On this PC"
        if location != self.location_readout.text():
            self.location_readout.setText(location)
        trip = self.sender.round_trip_ms
        trip_text = f"{trip} ms" if trip is not None else ""
        if trip_text != self.round_trip_readout.text():
            self.round_trip_readout.setText(trip_text)
            self.round_trip_row.setVisible(trip is not None)
        redirect_text = "Bring input back to this PC" if self.sender.redirecting else self._redirect_text()
        if redirect_text != self.redirect_button.text():
            self.redirect_button.setText(redirect_text)
            self.redirect_action.setText(redirect_text)
        sending = self._config is not None and self._any_send()
        self.redirect_button.setEnabled(sending)
        self.redirect_action.setEnabled(sending)
        self.redirect_note.setVisible(not sending and self._paired)
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
        # "On <machine>" above already says this while redirecting; the hint is for the rest --
        # not connected, or the hooks failing to install -- and stays quiet in the boring case.
        send_hint = "" if self.sender.redirecting or self._sending_detail in (
            "Off", "Not connected"
        ) else self._shown(self._sending_detail)
        if send_hint != self.send_hint.text():
            self.send_hint.setText(send_hint)
            # Hidden while empty, or its spacing leaves a gap between the two switches.
            self.send_hint.setVisible(bool(send_hint))
        self.sidebar.set_link(tone, SIDEBAR_LINK_WORDS.get(state, "Unknown"))
        self.sidebar.set_dots(pages_win.dots(self._config is None or not self._paired, self._firewall_tone))
        self._peers_tick += 1
        if self._peers_tick % 4 == 0:
            self._refresh_peers()
        else:
            self._refresh_peer_states()
        self._refresh_tray_directions()
        self._refresh_tray_machines()
        self._refresh_pairing()

    def _title(self) -> str:
        with self._status_lock:
            return self._shown(f"Beamer — {self._status.value}: {self._status_detail}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Beamer Windows receiver")
    parser.add_argument("--config", type=Path, default=default_config_path(), help="path to settings.json")
    parser.add_argument("--hidden", action="store_true", help="start in the tray without showing the window")
    return parser.parse_args()


@functools.lru_cache(maxsize=None)
def _windows_kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.SetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
    return kernel32


def _stay_unthrottled(kernel32) -> None:
    """Opts out of Windows' power throttling (EcoQoS), the counterpart of the Mac's App Nap
    activity. Beamer sits in the tray with no window in front, which is what Windows' heuristics
    throttle, and a throttled Beamer sends its pings and acks late enough for the far side to give
    the link up. ProcessPowerThrottling (4) with execution speed in the control mask and not in
    the state mask: never throttled, whatever the power plan."""
    state = (ctypes.c_uint32 * 3)(1, 0x1, 0)  # Version, ControlMask, StateMask
    if not kernel32.SetProcessInformation(kernel32.GetCurrentProcess(), 4, ctypes.byref(state), ctypes.sizeof(state)):
        LOGGER.warning("Could not opt Beamer out of power throttling: %s", ctypes.WinError(ctypes.get_last_error()))


def _claim_instance():
    """The single-instance lock: the function that gives it back, or None when another Beamer
    already holds it."""
    if sys.platform == "win32":
        # use_last_error, then ctypes.get_last_error(): a plain GetLastError() call
        # through windll can read an error ctypes' own marshalling set in between,
        # and 183 (already exists) is the whole single-instance check.
        kernel32 = _windows_kernel32()
        mutex = kernel32.CreateMutexW(None, False, "Local\\Beamer.Receiver")
        already_running = ctypes.get_last_error() == 183
        if not mutex:
            raise OSError("Beamer could not create its single-instance lock")
        if already_running:
            kernel32.CloseHandle(mutex)
            return None
        return lambda: kernel32.CloseHandle(mutex)
    from PySide6.QtCore import QLockFile

    lock = QLockFile(str(Path(tempfile.gettempdir()) / f"Beamer.Receiver.{os.getuid()}.lock"))
    lock.setStaleLockTime(0)
    return lock.unlock if lock.tryLock(0) else None


def main() -> None:
    release = _claim_instance()
    if release is None:
        return
    arguments = parse_args()
    log_path = configure_logging()
    LOGGER.info("Starting Beamer Windows receiver")
    if log_path is not None:
        LOGGER.info("Logging to %s", log_path)
    if sys.platform == "win32":
        # Above normal, so a busy machine cannot starve the hooks: Windows removes a
        # low-level hook whose callback misses its timeout, silently and for good,
        # and every Beamer thread must win the CPU for the hook thread to get the
        # GIL. Beamer idles near 0%, so the class costs the rest of the machine
        # nothing (for example, a video pipeline holding half the CPU once left
        # the PC's mouse stranded on the Mac).
        kernel32 = _windows_kernel32()
        if not kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x8000):  # ABOVE_NORMAL_PRIORITY_CLASS
            LOGGER.warning("Could not raise Beamer's priority: %s", ctypes.WinError(ctypes.get_last_error()))
        _stay_unthrottled(kernel32)
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
        release()


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
