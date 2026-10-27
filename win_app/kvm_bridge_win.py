import time

PROCESS_STARTED = time.perf_counter()

import argparse
import ctypes
import copy
from dataclasses import replace
import functools
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import platform
from pathlib import Path
import sys
import tempfile
import threading
from typing import Optional

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, QStandardPaths, QTimer, Qt, QUrl, Signal, QProcess
from PySide6.QtGui import QAction, QColor, QCursor, QDesktopServices, QIcon, QKeySequence, QPainter, QPixmap, QShortcut, QStandardItem, QStandardItemModel
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QInputDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QLayout,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
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
from core import crash_log
from core import portable_runtime
from core.portable_store import PortableStore, PortableStoreError
from core.capture_types import DeadKey, TextInput
from core.i18n import tr
from core import effects
from diagram import AtlasCanvas, PushStrip
from edge_glow import EdgeGlow, PreviewLoop
from frame_clock import set_divisor
from effect_overlay import EffectOverlay, desktop_box, logical_point
from effect_overlay_x11 import EffectOverlayX11Proxy, available as xwayland_available
from identify_overlay import IdentifyScreens
from effect_previews import EffectStill, SwitchStill, TileHover
from core import idle_pointer
from core import ignored
import motion
from core import pairing
from core import keytable
from core import peerlist
from core.pairing import PairingService, local_address_towards
import machines_win
import linux_desktop
import pages_win
import hardware_win
import peers_view
import platform_parts
import qr_code
import quit_signals as quit_signals_module
import global_shortcut_portal
import portal
from core import protocol
from core import settings_sync
from core import i18n
from core.i18n import matches_translation, N_, joined, tr, trc
from core import receiver
from core.receiver import LinkResponder, ServerState
from core import return_edge
from core import settings_sync
from core import ways, crossing_state, profiles
from core.identify_screens import display_numbers
from sender import OWN_PLATFORM, LinkSender
from shake_stroke import shake_stroke
import theme
import tray_glyph
import tokens
from core import updates
import widgets

QPushButton = widgets.ActionButton

LOGGER = logging.getLogger(__name__)
VEIL_SIZE = 16

# The repo-root VERSION file is the one place the version is set; build.spec bundles it.
try:
    VERSION = (Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent.parent) / "VERSION").read_text().strip()
except OSError:
    VERSION = "dev"

# How long the control line's center_home waits after bringing input home before it places the pointer.
CENTER_HOME_DELAY_MS = 80

HOME_PAGE = "https://kalkmancode.co.uk/beamer"
HOME_PAGE_TEXT = N_("Beamer's website")

STATUS_TITLES = {
    ServerState.STOPPED: N_("Receiver stopped"),
    ServerState.WAITING: N_("Waiting for a machine"),
    ServerState.CONNECTED: N_("Machine connected"),
    ServerState.ERROR: N_("Receiver needs attention"),
}

# The short word beside the sidebar's link dot -- STATUS_TITLES is a sentence, this is one word.
SIDEBAR_LINK_WORDS = {
    ServerState.STOPPED: N_("Stopped"),
    ServerState.WAITING: N_("Waiting"),
    ServerState.CONNECTED: N_("Connected"),
    ServerState.ERROR: N_("Error"),
}

HEADING_ROLE = {
    "signal": "heading",
    "off": "heading",
    "amber": "heading",
    "fault": "heading-fault",
}

EDGE_CHOICES = pages_win.SIDE_CHOICES
CORNER_CHOICES = (
    ("top_left", N_("Top-left")),
    ("top_right", N_("Top-right")),
    ("bottom_left", N_("Bottom-left")),
    ("bottom_right", N_("Bottom-right")),
)
TRIGGER_STYLE_CHOICES = (("double_tap", N_("Double-tap")), ("hold", N_("Hold")))
MODIFIER_STYLE_CHOICES = (
    ("semantic", N_("Same shortcuts")),
    ("positional", N_("Same positions")),
    ("mac_layout", N_("Mac keyboard layout")),
)
MODIFIER_NOTES = {
    "semantic": N_("On a Mac, Ctrl arrives as Command and the Windows key as Control, so Ctrl+C "
    "copies there too. Between two PCs every key arrives as itself."),
    "positional": N_("On a Mac, each key arrives as the Mac key in the same place: Ctrl as Control, the "
    "Windows key as Command. Between two PCs every key arrives as itself."),
    "mac_layout": N_("To a Mac: Alt becomes Command and Win becomes Option; Ctrl stays Control."),
}
IGNORED_NOTES = {
    "filled": N_("These keep working on this PC while its input is on another machine: a mouse's back button for "
                 "this PC's browser, say, or a volume key for its speakers."),
    "empty": N_("Nothing yet. Every key and button goes to the machine that has input. Add one to keep "
                "it here: a mouse's back button for this PC's browser, say, or a volume key for its speakers."),
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


def xwayland_effects_available() -> bool:
    return xwayland_available(platform_parts.WAYLAND, os.environ)


ICON_PATH = asset_path("Beamer.ico")

# The OS parts keep their Windows names so the code and its tests read as before; on Linux they are
# the X11 or the portal back ends (platform_parts).
autostart_win = platform_parts.autostart
capture_win = platform_parts.capture
desktop_win = platform_parts.desktop
firewall_win = platform_parts.firewall


# Where configure_logging put the log, for the tray's Open log folder.
LOG_PATH: Optional[Path] = None


def machine_name(name=""):
    if name:
        return name[:protocol.MAX_NAME_CHARS]
    suffix = " (Portable)" if portable_runtime.enabled() else ""
    return pairing.machine_name()[:protocol.MAX_NAME_CHARS - len(suffix)] + suffix


def configure_logging() -> Optional[Path]:
    global LOG_PATH
    candidates = [default_config_path().parent]
    if not portable_runtime.enabled():
        candidates.append(Path(tempfile.gettempdir()) / "Beamer")
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
            if portable_runtime.enabled():
                raise
            continue
    logging.basicConfig(level=logging.INFO)
    return None


def arrival_method(edge: Optional[str], driver_platform: str) -> str:
    """How input arriving at `edge` came: a Mac's notch crossing lands on this PC's bottom edge,
    and that is the only arrival an effect draws differently. No edge is a peer's shortcut or menu."""
    if edge is None:
        return "switch"
    return "notch" if edge == "bottom" and keytable.is_mac(driver_platform) else "edge"


def page_column_width(free: float) -> float:
    """One centred section column, with a 24 DIP margin on either side."""
    return min(880, max(1, free - 48))


class _ElidedLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.full_text = ""
        self.setWordWrap(False)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text: str) -> None:
        self.full_text = str(text)
        self.setToolTip(self.full_text)
        self.setAccessibleName(self.full_text)
        self._refresh_elided_text()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_elided_text()

    def _refresh_elided_text(self) -> None:
        shown = self.fontMetrics().elidedText(
            self.full_text, Qt.TextElideMode.ElideRight, self.contentsRect().width()
        )
        QLabel.setText(self, shown)


class ColumnFit(QObject):
    """Keeps a page's column at page_column_width() of the free width, on every resize of its scroll
    area. The free width is the scroll area less a scrollbar's width whether or not one shows: the
    viewport's own width would move with the column, since a column's wrapped text sets whether the
    page needs a scrollbar, so the column would chase its own tail."""

    def __init__(self, scroll: QScrollArea, column: QWidget) -> None:
        super().__init__(scroll)
        self.scroll = scroll
        self.column = column
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.fit)
        scroll.installEventFilter(self)

    def fit(self) -> None:
        width = round(page_column_width(self.scroll.width() - 16))
        if width != self.column.width():
            self.column.setFixedWidth(width)
        for label in self.column.findChildren(QLabel):
            if label.isVisible() and label.wordWrap():
                label.setMinimumHeight(label.heightForWidth(label.width()))
        layout = self.column.layout()
        if layout is not None:
            layout.activate()
            self.column.setMinimumHeight(layout.sizeHint().height())
            self.scroll.widget().updateGeometry()

    def eventFilter(self, watched, event) -> bool:
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            # QScrollArea must finish resizing its document before its width constraints change.
            self.timer.start(0)
        return False


class StatusBridge(QObject):
    """Marshals receiver-thread status callbacks onto the GUI thread."""

    changed = Signal(object, str)
    # The fourth names which third of the edge a Part of the edge push is in, else None.
    pressure = Signal(str, float, bool, object)
    local_pressure = Signal(str, float, bool, object)
    # (method, edge, x, y): the pointer landed on this PC at desktop pixel (x, y).
    arrived = Signal(str, str, float, float)
    firewall = Signal(object)
    admin_changed = Signal(object)
    # A pairing this PC hosted finished and was stored: the new peer entry.
    paired = Signal(object)
    # A pairing this PC requested ended: (the entry stored or None, the error or None).
    pair_finished = Signal(object, object)
    # The other direction: this PC's own input going to the Mac.
    sending = Signal(bool, str)
    redirecting = Signal(bool)
    veil = Signal(bool)
    # A peer began or stopped driving this PC: its id as base64, "" when none does.
    owner = Signal(str)
    # A link learnt something about a peer and saved it, or came up or went down.
    peers_changed = Signal()
    # Either link, telling us a peer's side changed: peer, edge, stamp, author, way_back, way_back_by.
    arrangement = Signal(str, str, int, str, object, object)
    # Either link, carrying a peer's Same on all machines state: (peer id, data).
    settings = Signal(str, object)
    identify = Signal(object)
    shook_home = Signal(object)
    escaped_home = Signal(object)
    alert = Signal(str, str)
    # (version, url) when a newer Beamer is out, else None.
    update = Signal(object)
    update_check = Signal(str, object)
    permission = Signal(str, str, str, bool)


def _instance_server_name() -> str:
    if sys.platform == "win32":
        kernel32 = _windows_kernel32()
        session = ctypes.c_ulong()
        if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session)):
            raise OSError("Beamer could not identify its Windows session")
        return f"Beamer.Receiver.{session.value}{portable_runtime.instance_suffix()}"
    suffix = f"Beamer.Receiver.{os.getuid()}"
    if sys.platform.startswith("linux"):
        runtime_location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.RuntimeLocation)
        directory = Path(runtime_location) if runtime_location else Path(tempfile.gettempdir())
        directory.mkdir(parents=True, exist_ok=True)
        return str(directory / f"{suffix}.sock")
    return suffix


def send_instance_activation(name: Optional[str] = None, activation_token: Optional[str] = None,
                             timeout_ms: int = 2000) -> bool:
    name = name or _instance_server_name()
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        socket = QLocalSocket()
        socket.connectToServer(name)
        if socket.waitForConnected(100):
            payload = json.dumps({"activation_token": activation_token or ""}).encode("utf-8") + b"\n"
            socket.write(payload)
            return socket.waitForBytesWritten(max(1, int((deadline - time.monotonic()) * 1000)))
        socket.abort()
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


class InstanceSignal(QObject):
    """The second-launch listener, and the local control line for tools on this machine (a Stream
    Deck, a script): one JSON line in, `{"command": "pause"}` and the like, and for a command one
    JSON line back. The pipe is this user's alone (UserAccessOption), so nothing off this machine
    or under another account reaches it. A line with no command is a second launch, as before."""

    def __init__(self, activate, name: Optional[str] = None, parent=None, commands=None):
        super().__init__(parent)
        self.name = name or _instance_server_name()
        self.activate = activate
        self.commands = commands or {}
        QLocalServer.removeServer(self.name)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self._accept)
        self.listening = self.server.listen(self.name)

    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            connection = self.server.nextPendingConnection()
            connection.readyRead.connect(lambda socket=connection: self._read(socket))

    def _read(self, connection) -> None:
        if not connection.canReadLine():
            return
        try:
            request = json.loads(bytes(connection.readLine()).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            request = {}
        if not isinstance(request, dict):
            request = {}
        if "command" in request:
            connection.write(json.dumps(self._run(request.get("command"))).encode("utf-8") + b"\n")
            connection.flush()
        else:
            self.activate(request.get("activation_token") or None)
        connection.disconnectFromServer()

    def _run(self, command) -> dict:
        handler = self.commands.get(command) if isinstance(command, str) else None
        if handler is None:
            return {"ok": False, "error": f"unknown command: {command!r}", "commands": sorted(self.commands)}
        try:
            return {"ok": True, **(handler() or {})}
        except Exception as exc:
            LOGGER.exception("Control command %r failed", command)
            return {"ok": False, "error": str(exc)}

    def close(self) -> None:
        self.server.close()
        QLocalServer.removeServer(self.name)


def escape_home_on(settings: dict) -> bool:
    """Three presses of Esc bring input home unless this machine turned it off."""
    return settings.get("escape_home", True) is not False


def first_notice(config_path, key: str) -> bool:
    """Whether a way home's notice is still among its first three, counting this one."""
    with app_config.SETTINGS_LOCK:
        settings = app_config.load_settings(config_path)
        count = settings.get(key, 0)
        count = count if type(count) is int and count >= 0 else 0
        if count >= 3:
            return False
        settings[key] = count + 1
        app_config.write_settings(config_path, settings)
    return True


def ways_summary_for_label(label: QLabel, *args) -> str:
    """Build the Crossing sentence with its peer name elided to the label's line width."""
    summary_args = list(args)
    width = label.contentsRect().width()
    # Before the first layout the label has no width yet; the resize that follows elides it.
    if width > 0:
        summary_args[6] = label.fontMetrics().elidedText(summary_args[6], Qt.TextElideMode.ElideRight, width)
    return pages_win.ways_summary(*summary_args)


def _crossing_refresh(method):
    @functools.wraps(method)
    def refresh(self, *args, **kwargs):
        document = getattr(self, "crossing_document", None)
        depth = getattr(self, "_crossing_update_depth", 0)
        enabled = document.updatesEnabled() if document is not None else False
        if document is not None and depth == 0:
            document.setUpdatesEnabled(False)
        self._crossing_update_depth = depth + 1
        try:
            return method(self, *args, **kwargs)
        finally:
            self._crossing_update_depth = depth
            if document is not None and depth == 0:
                document.setUpdatesEnabled(enabled)
    return refresh


class _OnResize(QObject):
    def __init__(self, widget, callback) -> None:
        super().__init__(widget)
        self._callback = callback
        widget.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.Resize:
            self._callback()
        return False


def design_readout():
    label = widgets.label("", "key")
    label.setFixedWidth(effects.DESIGN_READOUT_WIDTH)
    label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return label


class _DesignChoiceSlider:
    def __init__(self, name, minimum, maximum, step, scale, stops, preset_names):
        self.is_length = name.lower() == "length"
        self.scale = scale
        self.stops = stops
        self.preset_names = preset_names
        self.description_template = (N_("Sets the effect's playback time.") if self.is_length
                                     else N_("Sets effect depth relative to the shorter screen edge."))
        self.value_description_template = (N_("Sets the effect's playback time. Current value: {value}.") if self.is_length
                                           else N_("Sets effect depth relative to the shorter screen edge. Current value: {value}."))
        self.description = tr(self.description_template)
        self.slider = widgets.Ruler(tr("Length") if self.is_length else tr("Size"), maximum, minimum, step=step, page_step=step * 5)
        self.slider.setMaximumWidth(240)
        self.view = self.slider
        self.slider.setAccessibleDescription(self.description)
        self.slider.sliderMoved.connect(self._snap)

    @property
    def value(self):
        numeric = self.slider.value() / self.scale
        return effects.length_preset(numeric) if self.is_length else effects.size_preset(numeric)

    def set_value(self, preset):
        index = next((i for i, (key, _name) in enumerate(self.preset_names) if key == preset), 1)
        self.set_numeric(self.stops[index])

    def numeric_value(self):
        return self.slider.value() / self.scale

    def set_numeric(self, value):
        # Like the choice controls, only the user's own move reaches the change path: a value shown
        # from the config or a followed design must not save back and announce itself.
        blocked = self.slider.blockSignals(True)
        try:
            self.slider.setValue(round(value * self.scale))
        finally:
            self.slider.blockSignals(blocked)

    def set_accessible_value(self, value):
        self.slider.setAccessibleDescription(tr(self.value_description_template).format(value=value))

    def set_enabled(self, enabled: bool) -> None:
        self.slider.setEnabled(bool(enabled))
        widgets.set_dimmed(self.slider, not enabled)

    def _snap(self, value):
        snapped = effects.snap_detent(value, self.slider.minimum(), self.slider.maximum(),
                                      tuple(round(stop * self.scale) for stop in self.stops))
        if snapped != value:
            self.slider.setValue(round(snapped))


class WindowsApplication(QWidget):
    # What the tray icon was last drawn from (drive state, receiver state, taskbar ink); unset until the first draw.
    _tray_icon_key = None

    def eventFilter(self, watched, event):
        if watched in (getattr(self, "profile_header", None), getattr(self, "profile_override_row", None)) and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            narrow = watched.width() < 420
            watched.layout().setDirection(QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight)
            if watched is self.profile_header:
                watched.layout().setAlignment(self.same_ways_switch, Qt.AlignmentFlag.AlignLeft if narrow else Qt.AlignmentFlag.AlignRight)
        return super().eventFilter(watched, event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._start_after_first_paint and not self._startup_painted:
            self._startup_painted = True
            LOGGER.info("Window painted %.3f seconds after process start", time.perf_counter() - PROCESS_STARTED)
            QTimer.singleShot(0, self.start)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "sidebar"):
            self.sidebar.set_window_width(self.width())
        if hasattr(self, "footer_note"):
            self.footer_note.setVisible(self.width() >= 640)

    def __init__(self, config_path: Path, start_after_first_paint: bool = False,
                 input_controller_available: bool = True) -> None:
        super().__init__()
        self._input_controller_blocked = not input_controller_available
        self._input_controller_release = None
        self.config_path = config_path
        self._status_lock = threading.RLock()
        self._status = ServerState.STOPPED
        self._status_detail = "Initialising"
        self._last_refusal = ""
        self._refusal_notices = peerlist.RefusalThrottle()
        self._config = None
        self._closing = False
        self._same_settings_signature = object()
        self._same_before_sync = None
        self._profile_last_heard = {}
        self._shake_enabled = None
        self._escape_enabled = None
        self._shake_sensitivity = None
        self._displays_signature = object()
        self._design_follow_signature = object()
        self.tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        self._start_after_first_paint = start_after_first_paint
        self._startup_painted = False
        self._page = "overview"
        self._apply_serial = 0
        self.bridge = StatusBridge()
        self.permission_state = None
        if pages_win.linux_permissions_visible(sys.platform, platform_parts.WAYLAND):
            self.permission_state = pages_win.LinuxPermissionState()
            self.permission_state.on_change = self.bridge.permission.emit
        self.bridge.changed.connect(self._on_status)
        self.bridge.pressure.connect(self._on_pressure)
        self.bridge.local_pressure.connect(self._on_local_pressure)
        self.bridge.arrived.connect(self._on_arrival)
        self.bridge.firewall.connect(self._on_firewall)
        self.bridge.admin_changed.connect(self._on_admin_mode_changed)
        self.bridge.paired.connect(self._on_paired)
        self.bridge.pair_finished.connect(self._on_pair_finished)
        self.bridge.sending.connect(self._on_sending)
        self.bridge.redirecting.connect(self._on_redirecting)
        self.bridge.owner.connect(self._on_owner)
        self.bridge.veil.connect(self._show_veil)
        self._veil = None
        self._veil_hwnd = 0
        self._driven = False
        self._idle_pointer = self._make_idle_pointer()
        self.bridge.peers_changed.connect(self._on_peers_changed)
        self.bridge.arrangement.connect(self._on_arrangement)
        self.bridge.settings.connect(self._on_settings)
        self.bridge.identify.connect(self._on_identify_screens)
        self.identify_screens = IdentifyScreens(self)
        self.bridge.shook_home.connect(self._on_shake)
        self.bridge.escaped_home.connect(self._on_escape_home)
        self.bridge.update.connect(self._on_update)
        self.bridge.update_check.connect(self._on_update_check)
        self.bridge.permission.connect(self._on_permission)
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
        self.machine_actions: dict = {}
        self._labels_by_id: dict = {}
        self._peers_tick = 0
        self._firewall_advice: Optional[firewall_win.Advice] = None
        self._firewall_status: Optional[firewall_win.FirewallStatus] = None
        self._firewall_tone: Optional[str] = None
        self._firewall_busy = False
        self._firewall_again = False
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
                protocol.id_text(peer), read["edge"], read["set_at"], protocol.id_text(read["by"]), read.get("way_back"),
                protocol.id_text(read["way_back_by"]) if read.get("way_back_by") else None
            ),
            settings_callback=lambda peer, data: self.bridge.settings.emit(protocol.id_text(peer), data),
            identify_callback=self._receive_identify_screens,
            paired_callback=self._store_paired,
            link_callback=lambda peer, up: self.bridge.peers_changed.emit(),
            announce=self._announce,
            away=lambda: self.sender.owner,
            zones=self._zones,
            clipboard=platform_parts.clipboard,
            unlock=None if portable_runtime.enabled() else platform_parts.unlock,
            listener=portable_runtime.listener if portable_runtime.enabled() else None,
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
            redirect_callback=self._on_sender_redirecting,
            pressure_callback=self._relay_sender_pressure,
            arrival_callback=lambda edge, x, y: self.bridge.arrived.emit(
                "switch" if edge is None else "edge", edge or "", float(x), float(y)
            ),
            desktop=platform_parts.desktop,
            clipboard=platform_parts.clipboard,
            mouse_only_supported=not platform_parts.WAYLAND,
        )
        self._own_display_map = {}
        app = QApplication.instance()
        for screen in app.screens():
            screen.geometryChanged.connect(self._displays_changed)
        app.screenAdded.connect(self._screen_added)
        app.screenRemoved.connect(self._displays_changed)
        self._refresh_own_displays()
        self.sender.driven = lambda: self.server.driven
        self.resistance_pressure_timer = QTimer(self)
        self.resistance_pressure_timer.setInterval(33)
        self.resistance_pressure_timer.timeout.connect(self._refresh_local_pressure)
        self.sender.send_peer_home = self.server.send_home
        # Same on all machines, over either link: applied on the GUI thread, and announced with
        # this PC's own arrangement the moment a link comes up.
        self.sender.settings_callback = lambda peer, data: self.bridge.settings.emit(protocol.id_text(peer), data)
        self.sender.identify_callback = self._receive_identify_screens
        self.sender.arrangement_callback = lambda peer, read: self.bridge.arrangement.emit(
            protocol.id_text(peer), read["edge"], read["set_at"], protocol.id_text(read["by"]), read.get("way_back"),
            protocol.id_text(read["way_back_by"]) if read.get("way_back_by") else None
        )
        self.sender.link_callback = lambda peer, up: self.bridge.peers_changed.emit()
        self.sender.paired_callback = self._store_paired
        self.sender.hw_learned = self._learn_peer_hardware
        self.sender.announce = self._announce
        self.scope_labels: dict = {}
        self.own_notes: list = []
        self._same_seen = None
        self._design_states = {}
        self._followed_version = None
        self._design_path = []
        # Pause crossing and the full-screen hold are about this screen: a peer's pointer does
        # not go home through a held edge either.
        self.server.edges_held = lambda: self.sender.edges_held
        self.sender.on_alert = self.bridge.alert.emit
        self.bridge.alert.connect(self._on_alert)
        hook_callbacks = (self._on_hook_key, self._on_hook_mouse, self.sender.on_motion)
        if sys.platform.startswith("win"):
            self.hooks = capture_win.Hooks(*hook_callbacks, on_seam=self.sender.seam_clip_position)
        else:
            self.hooks = capture_win.Hooks(*hook_callbacks)
        if self.permission_state is not None and hasattr(self.hooks, "on_permission"):
            self.hooks.on_permission = lambda status, detail="", retryable=False: self.permission_state.report(
                "capture", status, detail, retryable,
            )
            self.permission_state.bind_requester("capture", self.hooks.request_again)
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
            self.sender.push_out = lambda peer, on_captured, on_failed: self.hooks.push_through(
                self.sender.models_toward(peer), on_captured, on_failed,
            )
            self.global_shortcut = global_shortcut_portal.GlobalShortcut(
                portal.JeepneyBus,
                lambda: self.sender.toggle_shortcut() if not self.sender.redirecting else None,
            )
        if hasattr(platform_parts.injector, "on_problem"):
            platform_parts.injector.on_problem = lambda sentence: self.bridge.alert.emit("Beamer", sentence)
        if self.permission_state is not None and hasattr(platform_parts.injector, "on_permission"):
            platform_parts.injector.on_permission = lambda status, detail="", retryable=False: self.permission_state.report(
                "remote", status, detail, retryable,
            )
            self.permission_state.bind_requester("remote", platform_parts.injector.request_again)
        self._trigger = capture_win.Trigger()
        self._sending_detail = "Not connected"
        self.update_checker = updates.Checker(
            VERSION, lambda: self._config is None or self._config.check_updates, self.bridge.update.emit,
            logger=LOGGER, portable=portable_runtime.enabled(),
            on_check=self.bridge.update_check.emit,
        )
        self._paired = False
        try:
            # With nothing paired the window still has this PC's own settings to show and keep.
            self._config = load_config(config_path, unpaired_ok=True)
            set_divisor(effects.frame_divisor(self._config.frame_rate))
            self._same_seen = self._same_fields()
            self._design_seen = self._design_fields()
            self._apply_input_scale(self._config)
            self._host = self._config.host
            if self._config.home_shortcut and getattr(self, "global_shortcut", None) is not None:
                self.global_shortcut.start()
            self._paired = bool(self.book.peers())
            if self._paired:
                self._status = ServerState.WAITING
                self._status_detail = tr("Ready on TCP port {port}").format(port=self._config.port)
            else:
                self._status = ServerState.ERROR
                self._status_detail = "Not paired yet: press Pair a machine on Overview"
        except ConfigError as exc:
            LOGGER.info("Configuration is not ready: %s", exc)
            self._status = ServerState.ERROR
            if isinstance(exc, SettingsFileError):
                # _config stays None: that is what every page reads as "no settings to show".
                self._status_detail = (
                    "Settings could not be read. The file may be from a newer Beamer; it is left as it is. "
                    "Install the newer Beamer, or move settings.json aside to start fresh."
                )
            else:
                self._status_detail = "Not paired yet: press Pair a machine on Overview"

        # Announces this PC from launch, configured or not: pairing is how a fresh install gets its
        # first peer, so it cannot wait for the receiver to be listening. Without a readable
        # settings file there is no machine id to pair under, and so no service.
        self._settings_port, self._kept_hide = self._read_kept()
        self.pairing = self._make_pairing()
        self.sender.machines = self.pairing.machines if self.pairing is not None else (lambda: [])

        # Before any widget is built: every control takes its colours from the palette in use.
        appearance = self._config.appearance if self._config is not None else "system"
        theme.set_dark(theme.wants_dark(appearance, theme.system_dark()))

        self.setWindowTitle(tr("Beamer"))
        self.resize(900, 640)
        self.setMinimumSize(320, 300)
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.preview_loop = PreviewLoop(self)
        self.tile_hover = TileHover(self.preview_loop, self)
        self._build_window()
        self._apply_theme()
        self._build_tray()
        theme.watch_system(self._apply_appearance)

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(250)
        self.refresh_timer.timeout.connect(self._check_input_controller)
        self.refresh_timer.timeout.connect(lambda: self._refresh_window(skip_hidden=True))
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
            pages_win.PAGES, self._select_page, foot=tr("Beamer {version}\n{link}").format(version=VERSION, link=tr(HOME_PAGE_TEXT)), on_foot=self.open_home_page,
            foot_tip=HOME_PAGE,
        )
        self.sidebar.setFixedWidth(theme.SIDEBAR_WIDTH)
        self.sidebar.set_window_width(self.width())
        row_layout.addWidget(self.sidebar)
        row_layout.addWidget(widgets.rule())

        self.stack = QStackedWidget()
        pane = QWidget()
        pane_layout = QVBoxLayout(pane)
        pane_layout.setContentsMargins(0, 0, 0, 0)
        pane_layout.setSpacing(0)
        self.update_notice = widgets.Module("", heading=True)
        self.update_notice_text = self.update_notice.eyebrow
        actions = QHBoxLayout()
        self.update_action = QPushButton(tr("Get update"))
        self.update_action.setProperty("vernier", "primary")
        self.update_action.clicked.connect(
            lambda: self.open_update() if portal.sandboxed() else self.download_update())
        actions.addWidget(self.update_action)
        self.update_notes = QPushButton(tr("What's new"))
        self.update_notes.clicked.connect(self.open_update)
        actions.addWidget(self.update_notes)
        self.update_dismiss = QPushButton(tr("Dismiss"))
        self.update_dismiss.clicked.connect(self._dismiss_update)
        actions.addWidget(self.update_dismiss)
        actions.addStretch(1)
        self.update_notice.body.addLayout(actions)
        self.update_notice.hide()
        pane_layout.addWidget(self.update_notice)
        pane_layout.addWidget(self.stack, 1)
        row_layout.addWidget(pane, 1)
        outer.addWidget(row, 1)

        footer = QFrame()
        footer.setProperty("vernier", "commit")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(16, 10, 16, 10)
        self.footer_note = widgets.label(pages_win.footer("overview"), "note", wrap=True)
        footer_layout.addWidget(self.footer_note)
        footer_layout.addStretch(1)
        self.quit_button = QPushButton(tr("Quit Beamer"))
        self.quit_button.clicked.connect(self.quit)
        footer_layout.addWidget(self.quit_button)
        outer.addWidget(footer)

        self.quit_shortcut = QShortcut(QKeySequence("Ctrl+Q"), self)
        self.quit_shortcut.activated.connect(self.quit)

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
            scroll, layout = self._page_shell(tr(name), tr(purpose), tr(pages_win.SCOPE[key]) if key in pages_win.SCOPE else None, key)
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
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        scroll.verticalScrollBar().setFixedWidth(16)
        page = QWidget()
        page.setProperty("vernier", "plain")
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        page_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        centred = QHBoxLayout()
        centred.setContentsMargins(0, 0, 0, 0)
        centred.addStretch(1)
        content = QWidget()
        content.setObjectName("page_column")
        content.setProperty("vernier", "plain")
        # Its width is set by ColumnFit on every resize; the stretches either side only centre it.
        ColumnFit(scroll, content)
        centred.addWidget(content)
        centred.addStretch(1)
        page_layout.addLayout(centred)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 24, 0, 24)
        layout.setSpacing(20)
        heading = widgets.label(title if key == "crossing" else title.upper(), "heading")
        heading.setFont(theme.font(theme.HEADING, 700))
        layout.addWidget(heading)
        if key != "crossing":
            layout.addWidget(widgets.label(purpose, "note", wrap=True))
        if scope is not None and key != "crossing":
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
            self.crossing_ledger.cancel_recording()
        self._run_previews()

    # -- Overview ---------------------------------------------------------------------------

    def _overview_page(self, layout, current) -> None:
        self.machines = machines_win.MachinesModule(
            self._set_peer_send, self._set_peer_allow, self._set_peer_in_use, self._set_peer_left_out,
            self._remove_peer, self._toggle_pairing_sheet, self._rename_peer,
            on_cycle=self._set_shortcut_cycle,
            on_repair=self._re_pair,
            on_pair_phone=self._pair_phone,
            on_pair_nearby=self._start_nearby,
            on_pin=self._set_peer_pin,
        )
        layout.addWidget(self.machines)
        layout.addWidget(self._name_module(current))
        self.sheet = machines_win.PairingSheet(
            self._show_code, self._cancel_code, self._pair_clicked, self._pick_machine,
            self._choose_nearby, self._confirm_nearby, self._cancel_nearby,
        )
        self.sheet.setVisible(False)
        layout.addWidget(self.sheet)
        self.link_module = self._link_module()
        layout.addWidget(self.link_module)
        layout.addWidget(self._input_module())
        layout.addWidget(self._same_module(current))
        self.permissions_module = None
        if pages_win.linux_permissions_visible(sys.platform, platform_parts.WAYLAND):
            self.permissions_module = self._permissions_module()
            layout.addWidget(self.permissions_module)
        self.sign_in_module = self._sign_in_module()
        layout.addWidget(self.sign_in_module)
        layout.addWidget(self._tray_module(current))
        layout.addWidget(self._updates_module(current))
        self._refresh_peers()

    def _name_module(self, current: Config) -> QWidget:
        module = widgets.Module(tr("This machine"))
        line = QHBoxLayout()
        self.own_name_label = widgets.label(current.name or platform.node(), "readout", wrap=True)
        self.own_name_label.setMinimumWidth(0)
        self.own_name_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        line.addWidget(self.own_name_label, 1)
        self.own_name_rename = widgets.ActionButton(tr("Rename"))
        self.own_name_rename.setProperty("vernier", "small")
        self.own_name_rename.setMinimumWidth(self.own_name_rename.sizeHint().width())
        self.own_name_rename.clicked.connect(self._begin_own_rename)
        line.addWidget(self.own_name_rename)
        module.body.addLayout(line)
        self.own_name_edit = machines_win.RenameEdit()
        self.own_name_edit.setMaxLength(protocol.MAX_NAME_CHARS)
        self.own_name_edit.setText(current.name)
        self.own_name_edit.saved.connect(self._save_own_rename)
        self.own_name_edit.cancelled.connect(self._cancel_own_rename)
        self.own_name_edit.setVisible(False)
        module.body.addWidget(self.own_name_edit)
        self.own_name_note = widgets.label("", "note-quiet", wrap=True)
        self.own_name_note.setVisible(False)
        module.body.addWidget(self.own_name_note)
        return module

    def _begin_own_rename(self) -> None:
        self.own_name_edit.setText(self._config.name)
        self.own_name_label.setVisible(False)
        self.own_name_rename.setVisible(False)
        self.own_name_edit.setVisible(True)
        self.own_name_note.setText(tr("Only on this computer" if sys.platform.startswith("linux") else "Only on this PC"))
        self.own_name_note.setVisible(not settings_sync.group_on(self._config, "names"))
        self.own_name_edit.setFocus()
        self.own_name_edit.selectAll()

    def _save_own_rename(self, text: str) -> None:
        label = text.strip()[:protocol.MAX_NAME_CHARS]
        settings = app_config.load_settings(self.config_path)
        settings["name"] = label
        settings["name_set_at"] = settings_sync.next_stamp(settings.get("name_set_at", 0), time.time())
        settings["name_by"] = settings["machine_id"]
        try:
            app_config.write_settings(self.config_path, settings)
        except (ConfigError, OSError) as exc:
            LOGGER.exception("This PC's name could not be saved")
            self.footer_note.setText(tr("Could not save this PC's name: {error}").format(error=exc))
            self._cancel_own_rename()
            return
        self._config = replace(self._config, name=label)
        self.own_name_label.setText(label or platform.node())
        if settings_sync.group_on(self._config, "names"):
            self._send_same()
        self._cancel_own_rename()

    def _cancel_own_rename(self) -> None:
        self.own_name_edit.setVisible(False)
        self.own_name_note.setVisible(False)
        self.own_name_label.setVisible(True)
        self.own_name_rename.setVisible(True)

    def _rename_peer(self, token: str, text: str) -> None:
        settings = app_config.load_settings(self.config_path)
        entry = next((item for item in settings["peers"] if item.get("token") == token), None)
        if entry is None:
            return
        entry.update(settings_sync.rename_peer(entry, text, settings["machine_id"], time.time()))
        try:
            app_config.write_settings(self.config_path, settings)
        except (ConfigError, OSError) as exc:
            LOGGER.exception("A machine's name could not be saved")
            self.footer_note.setText(tr("Could not save the machine's name: {error}").format(error=exc))
            return
        self._refresh_peers()
        if settings_sync.group_on(self._config, "names"):
            self._send_same()

    def _permissions_module(self) -> QWidget:
        module = widgets.Module(tr("Linux permissions"))
        module.body.addWidget(widgets.label(
            tr("Beamer requests keyboard and pointer access when each Wayland session starts."), "note", wrap=True,
        ))
        self.permission_rows = {}
        titles = {"capture": N_("Send input — Input Capture"), "remote": N_("Receive input — Remote Desktop")}
        for direction, title in titles.items():
            row = QVBoxLayout()
            line = QHBoxLayout()
            line.setSpacing(10)
            name = widgets.label(tr(title), "body", wrap=True)
            name.setMinimumWidth(0)
            line.addWidget(name, 1)
            status = widgets.label(tr("Not requested"), "note")
            # As wide as the longest status in this language, so a change of state never moves the button.
            status.setMinimumWidth(max(status.fontMetrics().horizontalAdvance(tr(word))
                                       for word in pages_win.PERMISSION_STATUS_LABELS.values()))
            line.addWidget(status)
            retry = QPushButton(tr("Ask again"))
            retry.clicked.connect(lambda _checked=False, key=direction: self._ask_permission_again(key))
            line.addWidget(retry)
            row.addLayout(line)
            detail = widgets.label("", "note", wrap=True)
            detail.setVisible(False)
            row.addWidget(detail)
            module.body.addLayout(row)
            self.permission_rows[direction] = (status, detail, retry)
            self._on_permission(direction, *self.permission_state.status(direction),
                                self.permission_state.can_ask_again(direction))
        return module

    def _on_permission(self, direction: str, status: str, detail: str, retryable: bool) -> None:
        rows = getattr(self, "permission_rows", {})
        if direction not in rows:
            return
        status_label, detail_label, retry = rows[direction]
        status_label.setText(tr(pages_win.PERMISSION_STATUS_LABELS[status]))
        detail_label.setText(detail)
        detail_label.setVisible(bool(detail))
        retry.setVisible(retryable)

    def _ask_permission_again(self, direction: str) -> None:
        try:
            self.permission_state.ask_again(direction)
        except Exception as exc:
            self.permission_state.report(direction, "unavailable", str(exc))

    def _tray_module(self, current: Config) -> QWidget:
        module = widgets.Module(tr("Tray"))
        self.tray_switch = widgets.Switch(tr("Show in tray"))
        self.tray_switch.setFont(theme.font(theme.TYPE["body"]))
        self.tray_switch.setChecked(current.show_in_tray)
        self.tray_switch.setEnabled(self.tray_available)
        self.tray_switch.toggled.connect(self._set_show_in_tray)
        module.body.addWidget(self.tray_switch)
        module.body.addWidget(widgets.label(
            tr("With it off, closing the window quits Beamer. Open Beamer again to bring the window back.")
            if self.tray_available else tr("This desktop has no system tray, so Beamer's window is all there is: closing it quits Beamer."),
            "note", wrap=True))
        return module

    def _set_show_in_tray(self, enabled: bool) -> None:
        if self._config is None:
            return
        self._config.show_in_tray = bool(enabled)
        self._persist()
        self.tray.setVisible(self.tray_shown())

    def _updates_module(self, current: Config) -> QWidget:
        module = widgets.Module(tr("Updates"))
        self.updates_switch = widgets.Switch(tr("Automatically check for updates"))
        self.updates_switch.setFont(theme.font(theme.TYPE["body"]))
        self.updates_switch.setChecked(current.check_updates)
        self.updates_switch.toggled.connect(self._set_check_updates)
        module.body.addWidget(self.updates_switch)
        module.body.addWidget(widgets.label(
            tr("Checks GitHub at startup and once a day. Updates are downloaded and installed by you."),
            "note",
            wrap=True,
        ))
        self.check_updates_button = QPushButton(tr("Check for updates"))
        self.check_updates_button.clicked.connect(self.check_for_updates)
        module.body.addWidget(self.check_updates_button)
        self.update_status = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.update_status)
        self.update_button = QPushButton(tr("Download"))
        self.update_button.setProperty("vernier", "primary")
        self.update_button.clicked.connect(self.download_update)
        self.update_button.setVisible(False)
        module.body.addWidget(self.update_button)
        self.update_page_notes = QPushButton(tr("What's new"))
        self.update_page_notes.clicked.connect(self.open_update)
        self.update_page_notes.setVisible(False)
        module.body.addWidget(self.update_page_notes)
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

    def check_for_updates(self) -> None:
        self._on_update_check("checking", None)
        self.update_checker.check_now(manual=True)

    def _on_update_check(self, state, found) -> None:
        self.check_updates_button.setEnabled(state != "checking")
        text = {"checking": tr("Checking for updates…"),
                "current": tr("Beamer is up to date."),
                "available": tr("Update available."),
                "failed": tr("Could not check for updates. Check your connection and try again.")}[state]
        self.update_status.setText(text)
        if state in ("available", "current"):
            self._on_update(found, manual=True)
            if state == "current":
                self.update_status.setText(text)

    def _on_update(self, found, manual=False) -> None:
        """A core Release or None; on the GUI thread."""
        if self._config is None or (not manual and not self._config.check_updates):
            found = None
        self._manual_update = manual
        self._update = found
        self.update_status.setText(tr("Beamer {version} is available.").format(version=found[0]) if found else "")
        if found and portal.sandboxed():
            self.update_status.setText(self.update_status.text() + " " + tr(
                "Update through your software store, or download the Flatpak from the release page and open it."))
        elif found and portable_runtime.enabled() and not self.update_checker.download_url:
            self.update_status.setText(self.update_status.text() + " " + tr(
                "No portable download is available for this release. See What's new."))
        self.update_page_notes.setVisible(found is not None)
        self.update_button.setText(tr("Download Beamer {version}").format(version=found[0]) if found else tr("Download"))
        if portable_runtime.enabled() and found:
            self.update_button.setText(tr("Download portable zip"))
        can_download = found is not None and not portal.sandboxed() and (
            not portable_runtime.enabled() or bool(self.update_checker.download_url))
        motion.set_shown(self.update_button, can_download)
        self.update_action.setVisible(found is not None and (portal.sandboxed() or can_download))
        self._refresh_update_notice()

    def _refresh_update_notice(self) -> None:
        try:
            settings = app_config.load_settings(self.config_path)
        except (ConfigError, OSError):
            LOGGER.exception("Update notice settings could not be read")
            self.update_notice.hide()
            self.header_action.setText(tr("Beamer {version}").format(version=VERSION))
            self.header_action.setEnabled(False)
            return
        found = updates.visible_notice(self._update, self._config is not None and self._config.check_updates,
                                       settings.get("dismissed_update_version", ""),
                                       requested=getattr(self, "_manual_update", False))
        self.header_action.setText(tr("Beamer {version} is available…").format(version=found[0]) if found else tr("Beamer {version}").format(version=VERSION))
        self.header_action.setEnabled(found is not None and not portal.sandboxed())
        if found:
            text = tr("Beamer {version} is available.").format(version=found[0])
            if portal.sandboxed():
                text += " " + tr("Update through your software store, or download the Flatpak from the release page and open it.")
            elif portable_runtime.enabled() and not self.update_checker.download_url:
                text += " " + tr("No portable download is available for this release. See What's new.")
            self.update_notice_text.setText(text)
        self.update_notice.setVisible(bool(found))

    def _dismiss_update(self) -> None:
        if not self._update:
            return
        try:
            with app_config.SETTINGS_LOCK:
                settings = app_config.load_settings(self.config_path)
                settings["dismissed_update_version"] = self._update[0]
                app_config.write_settings(self.config_path, settings)
        except (ConfigError, OSError):
            LOGGER.exception("Update dismissal could not be saved")
            return
        self._manual_update = False
        self._refresh_update_notice()

    def open_update(self) -> None:
        if self._update:
            QDesktopServices.openUrl(QUrl(self._update[1]))

    def download_update(self) -> None:
        if portal.sandboxed():
            return
        if portable_runtime.enabled():
            if self._update and self.update_checker.download_url:
                QDesktopServices.openUrl(QUrl(self.update_checker.download_url))
        else:
            self.open_update()

    def open_log_folder(self) -> None:
        folder = LOG_PATH.parent if LOG_PATH is not None else default_config_path().parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _link_module(self) -> QWidget:
        module = widgets.Module(tr("Link"))
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
        where_row.addWidget(widgets.label(tr("Input:"), "note"))
        self.location_readout = widgets.label("", "readout", wrap=True)
        where_row.addWidget(self.location_readout, 1)
        module.body.addLayout(where_row)
        # Shown only while there is a figure: the trip is measured while input is on the Mac.
        self.round_trip_row = QWidget()
        self.round_trip_row.setProperty("vernier", "plain")
        trip_row = QHBoxLayout(self.round_trip_row)
        trip_row.setContentsMargins(0, 0, 0, 0)
        trip_row.setSpacing(6)
        trip_row.addWidget(widgets.label(tr("Delay:"), "note"))
        self.round_trip_readout = widgets.label("", "readout", wrap=True)
        trip_row.addWidget(self.round_trip_readout, 1)
        self.round_trip_row.setVisible(False)
        module.body.addWidget(self.round_trip_row)
        return module

    def _input_module(self) -> QWidget:
        """The everyday buttons: send input across without the shortcut or an edge, and hold the
        edges for a while."""
        module = widgets.Module(tr("Keyboard and mouse"))
        self.redirect_button = widgets.ElidingActionButton(tr("Send input across"))
        self.redirect_button.setProperty("vernier", "primary")
        self.redirect_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.redirect_button.clicked.connect(self.toggle_redirect)
        module.body.addWidget(self.redirect_button)
        # Why the button above is dimmed, while it is.
        self.redirect_note = widgets.label(tr("Switch on This PC drives it for a machine above to send input from here."),
                                           "note", wrap=True)
        self.redirect_note.setVisible(False)
        module.body.addWidget(self.redirect_note)
        self.wayland_send_note = widgets.label(
            tr("On Wayland the desktop lets input go to another machine only when the pointer pushes through an "
            "edge, so this button cannot send it."), "note", wrap=True)
        self.wayland_send_note.setVisible(False)
        module.body.addWidget(self.wayland_send_note)
        self.send_hint = widgets.label("", "note", wrap=True)
        self.send_hint.setVisible(False)
        module.body.addWidget(self.send_hint)
        self.pause_button = QPushButton(tr("Pause crossing"))
        self.pause_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.crossing_state = widgets.label("", "note", wrap=True)
        # Hidden while only the shortcut is chosen: there is no edge to pause.
        self.pause_row = self._row(self.pause_button, self.crossing_state)
        module.body.addWidget(self.pause_row)
        self.hold_switch = widgets.Switch(tr("Hold this PC's edges while an app on it is full screen"))
        self.hold_switch.setFont(theme.font(theme.TYPE["body"]))
        self.hold_switch.setChecked(self._config.hold_full_screen if self._config else False)
        self.hold_switch.toggled.connect(self._set_hold_full_screen)
        module.body.addWidget(self.hold_switch)
        own_note = self._own_note()
        module.body.addWidget(own_note)
        hold_note = widgets.label(
            tr("This screen decides, including when another machine drives it. On, a full-screen app here holds pointer crossings; shortcuts and jump keys still work. Off, another machine's pointer can go home or onward through the configured ways."), "note", wrap=True)
        module.body.addWidget(hold_note)
        self.hold_switch.setToolTip(tr("This screen's setting applies to every pointer on it."))
        self.pointer_switch = widgets.Switch(tr("Hide the pointer on the machine not in use"))
        self.pointer_switch.setFont(theme.font(theme.TYPE["body"]))
        self.pointer_switch.setChecked(self._config.hide_idle_pointer if self._config else True)
        self.pointer_switch.toggled.connect(self._set_hide_idle_pointer)
        module.body.addWidget(self.pointer_switch)
        pointer_note = widgets.label(
            tr("This PC's pointer hides while it drives another machine, and after another machine stops driving it, "
            "until its own mouse moves."), "note", wrap=True)
        module.body.addWidget(pointer_note)
        wayland_pointer_note = widgets.label(
            tr("On Wayland your desktop controls pointer hiding while this PC drives another machine. "
               "Beamer cannot hide the pointer after another machine stops driving this PC."), "note", wrap=True)
        wayland_pointer_note.setVisible(platform_parts.WAYLAND)
        module.body.addWidget(wayland_pointer_note)
        self.wayland_crossing_note = widgets.label(
            tr("On Wayland Beamer cannot see a full-screen app, or a mouse button held before the pointer reaches an "
            "edge, so the edges are never held for one and a drag can cross."), "note", wrap=True)
        module.body.addWidget(self.wayland_crossing_note)
        if sys.platform == "win32":
            self._admin_control(module)
        for widget in (self.hold_switch, own_note, hold_note):
            widget.setVisible(not platform_parts.WAYLAND)
        # Wayland owns hiding during capture; it offers no global idle-pointer hide.
        for widget in (self.pointer_switch, pointer_note):
            widget.setVisible(self._idle_pointer is not None)
        self.wayland_crossing_note.setVisible(platform_parts.WAYLAND)
        return module

    def _admin_control(self, module) -> None:
        self.admin_switch = widgets.Switch(tr("Control administrator windows"))
        self.admin_switch.setFont(theme.font(theme.TYPE["body"]))
        self.admin_switch.toggled.connect(self._set_admin_mode)
        module.body.addWidget(self.admin_switch)
        self.admin_note = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.admin_note)
        module.body.addWidget(widgets.label(
            tr("Use Windows permission prompts locally."), "note", wrap=True))
        self._refresh_admin_mode()

    def _refresh_admin_mode(self) -> None:
        import admin_mode
        try:
            self._admin_mode_enabled = admin_mode.enabled()
            reason = admin_mode.unavailable_reason()
        except OSError:
            self._admin_mode_enabled = False
            reason = tr("Beamer could not read this PC's administrator setting. Restart Beamer to try again.")
        self.admin_switch.blockSignals(True)
        self.admin_switch.setChecked(self._admin_mode_enabled)
        self.admin_switch.blockSignals(False)
        self.admin_switch.setEnabled(not reason)
        self.admin_note.setText(reason or tr(
            "Beamer will run as administrator on this PC. Changing this restarts Beamer; Windows may ask for permission."))

    def _set_admin_mode(self, enabled: bool) -> None:
        import admin_mode
        self.admin_switch.blockSignals(True)
        self.admin_switch.setChecked(self._admin_mode_enabled)
        self.admin_switch.blockSignals(False)
        self.admin_switch.setEnabled(False)
        self.admin_note.setText(tr("Waiting for Windows permission…"))

        def work():
            result = None
            try:
                if not admin_mode.change(enabled, config_path=self.config_path):
                    result = False
            except Exception as exc:
                LOGGER.exception("Could not change administrator mode")
                result = exc
            self.bridge.admin_changed.emit(result)

        threading.Thread(target=work, name="Beamer-administrator", daemon=True).start()

    def _on_admin_mode_changed(self, error) -> None:
        if error is False:
            self._refresh_admin_mode()
            return
        if error is None:
            self.quit()
            return
        self.admin_switch.blockSignals(True)
        self.admin_switch.setChecked(self._admin_mode_enabled)
        self.admin_switch.blockSignals(False)
        self.admin_switch.setEnabled(True)
        self.admin_note.setText(str(error))

    def _redirect_text(self) -> str:
        """Names the machine the button sends input to, which is the one the shortcut would pick."""
        target = self.sender.shortcut_target()
        if target is not None and target in self._labels_by_id:
            return tr("Send input to {name}").format(name=self._labels_by_id[target])
        sendable = [entry for entry in peerlist.desktops(self._peer_entries)
                    if entry.get("in_use", True) is True and entry.get("send")]
        # The entry migrated from 1.4.x has no id to pick it by until its first link.
        return tr("Send input to {name}").format(name=self._shown(peerlist.labels(self._peer_entries)[sendable[0]['token']])) if len(sendable) == 1 else tr("Send input across")

    def toggle_redirect(self) -> None:
        self.sender.toggle()

    def _can_send(self) -> bool:
        """Whether the Send button and the tray can move input now. On Wayland only bringing it back:
        there the desktop starts no capture for an app's button, and Send only beeped (KDE report,
        13-10-2026)."""
        if self._config is None or not self._any_send():
            return False
        return not platform_parts.WAYLAND or self.sender.redirecting

    def toggle_pause(self) -> None:
        self.sender.crossing_paused = not self.sender.crossing_paused
        self._refresh_window()

    # -- the control line (InstanceSignal): each answers what the status command does ---------

    def control_commands(self) -> dict:
        return {
            "status": self.control_status,
            "pause": lambda: self._control_set_paused(True),
            "resume": lambda: self._control_set_paused(False),
            "toggle_pause": lambda: self._control_set_paused(not self.sender.crossing_paused),
            "come_home": self.control_come_home,
            "center_home": self.control_center_home,
        }

    def control_status(self) -> dict:
        return {
            "version": VERSION,
            "paused": bool(self.sender.crossing_paused),
            "redirecting": bool(self.sender.redirecting),
            "on": self._label_of(self.sender.owner) or None,
        }

    def _control_set_paused(self, paused: bool) -> dict:
        if bool(self.sender.crossing_paused) != paused:
            self.toggle_pause()
        return self.control_status()

    def control_come_home(self) -> dict:
        """Brings this machine's input back from whichever machine it is on, the way a shake does:
        this machine decides, so a peer with its own crossing paused cannot keep it."""
        self.sender.set_redirecting(False)
        return self.control_status()

    def control_center_home(self) -> dict:
        """The escape hatch: input home, then the pointer to the middle of the main display. The
        placement waits a moment, so the return's own pointer restore lands first."""
        self.sender.set_redirecting(False)
        QTimer.singleShot(CENTER_HOME_DELAY_MS, self._center_pointer)
        return self.control_status()

    def _center_pointer(self) -> None:
        try:
            desktop = platform_parts.desktop
            screens = desktop.monitors()
            if not screens:
                return
            # The main display holds the desktop's origin on Windows and X11.
            main = next((r for r in screens if r.x <= 0 < r.x + r.width and r.y <= 0 < r.y + r.height), screens[0])
            desktop.set_cursor_position(main.x + main.width // 2, main.y + main.height // 2)
        except Exception:
            LOGGER.exception("Could not centre the pointer")

    def _crossing_state_args(self) -> tuple:
        entries = peerlist.desktops(self._peer_entries)
        return (
            bool(entries), any(entry.get("host") for entry in entries), self._any_send(),
            bool(self.sender.peers_up()), bool(self._ways_in()),
            self.sender.crossing_paused, self.sender.full_screen_app, self._machines_name(),
        )

    def _machines_name(self) -> str:
        entries = peerlist.desktops(self._peer_entries)
        return self._shown(peerlist.labels(entries)[entries[0]["token"]]) if len(entries) == 1 else "your machines"

    def _crossing_state_sentence(self) -> str:
        return pages_win.crossing_state_sentence(*self._crossing_state_args())

    def _check_full_screen(self) -> None:
        """This PC's rule: a full-screen app in front holds its edges, the shortcut still works.
        A failure stops the check for the run rather than logging once a second."""
        if platform.system() != "Windows" or self._config is None or not self._config.hold_full_screen:
            self.sender.full_screen_app = None
            return
        try:
            self.sender.full_screen_app = desktop_win.full_screen_app()
        except Exception:
            self.sender.full_screen_app = None
            self.full_screen_timer.stop()
            LOGGER.exception("Could not tell whether an app is full screen; crossing stays on")

    def _on_alert(self, title: str, message: str) -> None:
        if self._closing:
            return
        if matches_translation(
                message, "Can't cross to {name}: it is being driven from {other}.",
                "Can't cross to {name}: another machine is driving it.",
                "Can't cross to {name}: it is driving {other}.",
                "Can't cross to {name}: it is driving another machine."):
            self._last_refusal = message
            self._refresh_tray_state()
            if not self._refusal_notices.allow(message):
                return
        if matches_translation(
                message, "Cannot switch — {status}", "Cannot switch — {reason}",
                "Cannot switch — {name} could not be reached while this PC is being driven",
                "Cannot switch — {name} could not be reached",
                "Cannot switch — on this desktop input goes to another machine only by pushing the pointer through an edge"):
            QApplication.beep()
        if sys.platform.startswith("linux"):
            import portal

            if portal.sandboxed():
                portal.notify(title, self._shown(message))
                return
        self.tray.showMessage(title, self._shown(message), QIcon(str(ICON_PATH)), 4000)

    def _same_module(self, current: Config) -> QWidget:
        module = widgets.Module(tr("Settings"))
        module.body.addWidget(widgets.label(tr("Same on all machines"), "body", wrap=True))
        self.same_checks = {}
        self.same_ways_summary = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.same_ways_summary)
        for key, title in (("crossing", N_("Crossing")), ("design", N_("Design")),
                           ("shortcut", N_("Shortcut")), ("names", N_("Names"))):
            check = widgets.QCheckBox(tr(title))
            check.setFont(theme.font(theme.TYPE["body"]))
            check.setChecked(key in current.same_groups if key != "everything" else len(current.same_groups) == 4)
            check.toggled.connect(lambda on, name=key: self._same_group_toggled(name, on))
            module.body.addWidget(check)
            self.same_checks[key] = check
        self.same_content_note = widgets.label(
            tr("Crossing shares resistance and drag protection. Design shares effects, colour, length, size and arrival animation. "
            "Shortcut shares shortcut on/off, trigger key and style, and destination. Names shares this machine's name and paired-machine labels."),
            "note", wrap=True)
        module.body.addWidget(self.same_content_note)
        check = widgets.QCheckBox(tr("Everything"))
        check.setFont(theme.font(theme.TYPE["body"]))
        check.setChecked(len(current.same_groups) == 4)
        check.toggled.connect(lambda on: self._same_group_toggled("everything", on))
        module.body.addWidget(check)
        self.same_checks["everything"] = check
        self.same_everything_note = widgets.label(
            tr("Shares every group and can replace local values on this machine."), "note", wrap=True)
        module.body.addWidget(self.same_everything_note)
        self.same_old_note = widgets.label(
            tr("Older Beamer versions share known Crossing and Design settings without group choices."), "note-amber", wrap=True)
        module.body.addWidget(self.same_old_note)
        self.same_revert = QPushButton(tr("Revert"))
        self.same_revert.setProperty("vernier", "small")
        self.same_revert.clicked.connect(self._revert_same)
        module.body.addWidget(self.same_revert)
        self.same_note = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.same_note)
        return module

    def _same_group_toggled(self, group: str, on: bool) -> None:
        groups = set(self._config.same_groups)
        if group == "everything":
            groups = set(settings_sync.groups_for_choice(["everything"] if on else []))
        elif on:
            groups.add(group)
        else:
            groups.discard(group)
        self._set_same_groups(groups)

    def _own_note(self) -> QWidget:
        """A note on a row this PC keeps to itself, shown only while the pages are kept in step."""
        note = widgets.label(pages_win.OWN_ROW, "note", wrap=True)
        note.setVisible(False)
        self.own_notes.append(note)
        return note

    def _show_same(self) -> None:
        if self._config is None:
            return
        old = any("layout" not in caps for caps in self._peer_caps().values())
        groups = settings_sync.normalise_groups(self._config.same_groups)
        signature = self._settings_file_signature()
        if signature != self._same_settings_signature:
            stored = self._window_settings(force=True)
            if not getattr(self, "_window_settings_stale", False):
                self._same_settings_signature = signature
        else:
            stored = self._window_settings()
        self.same_ways_summary.setText(tr("Ways: set on the Crossing page · {state}").format(
            state=tr("On") if profiles.same_ways_on(stored) else tr("Off")))
        self._same_before_sync = stored.get("same_before_sync")
        revert = settings_sync.snapshot_available(self._same_before_sync)
        desktops = peerlist.desktops(self._peer_entries)
        on = self._config.same_on_both
        who = settings_sync.who([self._shown(label) for label in peerlist.labels(desktops).values()])
        self._refresh_design_follow()
        seen = (tuple(groups), old, revert, on, who, self._paired)
        if seen == getattr(self, "_same_controls_seen", None):
            return
        self._same_controls_seen = seen
        for key in settings_sync.ALL_GROUPS:
            self.same_checks[key].blockSignals(True)
            self.same_checks[key].setChecked(key in groups)
            self.same_checks[key].blockSignals(False)
            self.same_checks[key].setEnabled(self._paired)
        self.same_checks["everything"].blockSignals(True)
        self.same_checks["everything"].setChecked(len(groups) == len(settings_sync.ALL_GROUPS))
        self.same_checks["everything"].blockSignals(False)
        self.same_checks["everything"].setEnabled(self._paired)
        self.same_everything_note.setVisible(len(groups) == len(settings_sync.ALL_GROUPS))
        self.same_old_note.setVisible(old)
        self.same_revert.setVisible(settings_sync.snapshot_available(self._same_before_sync))
        desktops = peerlist.desktops(self._peer_entries)
        on = self._config.same_on_both
        who = settings_sync.who([self._shown(label) for label in peerlist.labels(desktops).values()])
        self.same_note.setText(settings_sync.group_note(groups, on))
        self.same_note.setToolTip(settings_sync.switch_detail())
        widgets.set_role(self.same_note, "note")
        for key, label in self.scope_labels.items():
            text = settings_sync.scope(key, who, on, pages_win.SCOPE.get(key), groups=groups)
            if label.text() != text:
                label.setText(text)
        for note in self.own_notes:
            if motion.target_shown(note) != on:
                motion.set_shown(note, on)

    def _set_same_groups(self, groups) -> None:
        if self._config is None:
            return
        groups = list(settings_sync.normalise_groups(groups))
        config = self._config
        # Each value keeps the stamp it held, never the new one: a choice of groups changes no value.
        held = settings_sync.held_stamps(config.same_stamps, config.same_set_at, config.same_by)
        config.same_stamps = settings_sync.stored_stamps(held)
        config.same_groups = groups
        config.same_on_both = bool(groups)
        config.same_set_at = settings_sync.next_stamp(config.same_set_at, time.time())
        config.same_by = config.machine_id
        if self._persist():
            self._send_same()
            if not settings_sync.group_on(self._config, "design"):
                self._apply_followed_design(force=True)
            self._show_same()

    def _revert_same(self) -> None:
        settings = app_config.load_settings(self.config_path)
        restored = settings_sync.restore_snapshot(settings.get("same_before_sync"))
        if restored is None:
            return
        config = replace(self._config)
        values, conflict = ways.shared_trigger_values(settings, restored["values"])
        if conflict:
            self._show_trigger_conflict(conflict)
        held = settings_sync.held_stamps(config.same_stamps, config.same_set_at, config.same_by)
        before = settings_sync.pc_values(config)
        design_stamp = settings_sync.design_change_stamp(before, values,
                                                         config.design_set_at, None, config.machine_id)
        settings_sync.apply_pc(config, values)
        if design_stamp is not None:
            config.design_set_at, config.design_by = design_stamp["set_at"], design_stamp["by"]
        # The values put back are a change made here: each is stamped, and nothing else is.
        held, _stamp = settings_sync.stamp_changes(held, before, settings_sync.pc_values(config), config.machine_id,
                                                   floor=max(config.same_set_at, config.design_set_at))
        config.same_stamps = settings_sync.stored_stamps(held)
        names = settings_sync.restamp_names(settings, restored.get("names", {}), config.machine_id)
        settings_sync.apply_names(settings, names)
        config.name = settings.get("name", config.name)
        config.same_groups = settings_sync.revert_groups(settings["same_before_sync"], config.same_groups)
        config.same_on_both = bool(config.same_groups)
        config.same_set_at = settings_sync.next_stamp(max(config.same_set_at, config.design_set_at), time.time())
        config.same_by = config.machine_id
        settings.pop("same_before_sync", None)
        settings.update(app_config.config_to_dict(config))
        try:
            app_config.write_settings(self.config_path, settings)
        except (ConfigError, OSError):
            LOGGER.exception("Same settings could not be reverted")
            return
        self._config = config
        self._apply_config(config)
        self._reflect_config(config)
        self.own_name_label.setText(config.name or platform.node())
        self._send_same()
        self._show_same()

    def _same_fields(self) -> dict:
        return settings_sync.pc_values(self._config)

    def _design_fields(self) -> tuple:
        values = settings_sync.pc_values(self._config)
        return tuple(values.get(key) for key in settings_sync.DESIGN_KEYS)

    def _same_state(self) -> dict:
        import sys

        config = self._config
        settings = app_config.load_settings(self.config_path)
        configure_seams = getattr(self, "_configure_capture_seams", None)
        if callable(configure_seams):
            configure_seams(config)
        values = settings_sync.pc_values(config)
        design_state = {
            "set_at": config.design_set_at,
            "by": config.design_by or config.machine_id,
            "values": {key: values[key] for key in settings_sync.DESIGN_KEYS if key in values},
            "path": (self._design_path or [config.machine_id])
                    if config.design_follow_peer and not settings_sync.group_on(config, "design") else [config.machine_id],
        }
        current_settings = app_config.load_settings(self.config_path)
        layout_tiles = copy.deepcopy(config.layout)
        for entry in peerlist.desktops(current_settings.get("peers", [])):
            tile = layout_tiles.get(entry.get("id"))
            name = entry.get("name", "")
            if isinstance(tile, dict) and isinstance(name, str) and name.strip():
                tile["name"] = name.strip()[:64]
        layout_state = {"set_at": config.layout_set_at, "by": config.layout_by or config.machine_id,
                        "tiles": layout_tiles}
        return settings_sync.message_data(
            config.same_on_both, config.same_set_at, values, by=config.same_by, design_state=design_state,
            names=settings_sync.names_from_settings(current_settings),
            layout_state=layout_state,
            groups=config.same_groups,
            stamps=settings_sync.held_stamps(config.same_stamps, config.same_set_at, config.same_by),
        )

    def _live(self, peer: str) -> bool:
        """Whether `peer` has a link up now, either way. One that has none gives way on the Crossing
        page and to an arrangement (core/ways.py give_way)."""
        ident = protocol.read_id(peer)
        return ident is not None and (self.sender.links.up(ident) or ident in self.server.links())

    def _gone(self, peer: str) -> bool:
        """Whether `peer` has removed this pairing: the link this PC dials to it was closed unanswered
        for a minute (core/link.py). Its zones set aside by `give_way` then stay off for good."""
        entry = next((item for item in self._peer_entries if item.get("id") == peer), None)
        return entry is not None and self.sender.links.kind(entry.get("token")) == "forgotten"

    def _settle_presence(self) -> None:
        return

    def _design_peer_caps(self) -> dict:
        desktops = {entry.get("id") for entry in peerlist.desktops(self._peer_entries)}
        caps = {protocol.id_text(peer): self.sender.links.caps(peer) for peer in self.sender.peers_up()}
        for peer in self.server.links():
            known = self.server.caps_of(peer)
            if known is not None:
                caps[protocol.id_text(peer)] = known
        return {peer: known for peer, known in caps.items() if peer in desktops}

    def _settings_file_signature(self):
        version = []
        for path in (self.config_path, app_config.legacy_config_path(self.config_path)):
            try:
                stat = path.stat()
                version.append((stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino))
            except OSError:
                version.append(None)
        return tuple(version)

    def _refresh_design_follow(self) -> None:
        if not hasattr(self, "design_follow_choice") or self._config is None:
            return
        choice = self.design_follow_choice
        caps = self._design_peer_caps()
        desktops = [entry for entry in peerlist.desktops(self._peer_entries) if protocol.read_id(entry.get("id")) is not None]
        rows = settings_sync.follow_rows(desktops, caps, self._design_states, self._config.design_follow_peer)
        signature = (repr(desktops), repr(caps), repr(self._design_states), self._config.design_follow_peer,
                     settings_sync.group_on(self._config, "design"), self._hide_addresses())
        if signature == self._design_follow_signature:
            return
        choice.blockSignals(True)
        model = getattr(self, "_design_follow_model", None)
        if model is None:
            model = QStandardItemModel(choice)
            self._design_follow_model = model
            choice.setModel(model)
        model.clear()
        none = QStandardItem(tr("None"))
        none.setData("", Qt.ItemDataRole.UserRole)
        model.appendRow(none)
        for entry, why_not in rows:
            name = self._shown(peerlist.label_for(desktops, peer_id=entry.get("id") or "")
                               or entry.get("platform") or "Paired machine")
            item = QStandardItem(f"{name} — {why_not}" if why_not == settings_sync.NEEDS_DESIGN_SYNC else name)
            item.setData(entry["id"], Qt.ItemDataRole.UserRole)
            if why_not is not None:
                item.setEnabled(False)
                item.setToolTip(why_not)
            model.appendRow(item)
        selected = choice.findData(self._config.design_follow_peer, Qt.ItemDataRole.UserRole)
        choice.setCurrentIndex(max(0, selected))
        choice.blockSignals(False)
        self._design_follow_signature = signature
        peer = next((item for item in desktops if item.get("id") == self._config.design_follow_peer), None)
        name = self._shown(peerlist.label_for(desktops, peer_id=peer.get("id") or "") or "that machine") if peer else ""
        if peer:
            if settings_sync.group_on(self._config, "design"):
                note = f"Following {name}, paused while Same on all machines is on."
            elif self._config.design_follow_peer not in caps:
                note = f"Keeping the last design received from {name} until it reconnects."
            else:
                note = ""
        else:
            note = settings_sync.follow_note(rows) or "None: this PC keeps its own Design."
        self.design_follow_note.setText(note)
        self.design_follow_note.setVisible(bool(note))
        self._lock_followed_design(
            settings_sync.follow_lock(self._config.design_follow_peer, settings_sync.group_on(self._config, "design"), desktops), name)

    def _lock_followed_design(self, entry, name: str) -> None:
        """Followed controls dimmed and deaf while `entry` is followed; the local ones stay live."""
        if not hasattr(self, "follow_banner"):
            return
        locked = entry is not None
        for control in (self.glow_style_choice, self.switch_style_choice, self.glow_colour_choice,
                        self.length_choice, self.size_choice, self.frame_rate_choice):
            control.set_enabled(not locked)
        self.landing_toggle.setEnabled(not locked)
        widgets.set_dimmed(self.landing_toggle, locked)
        self.follow_banner_label.setText(settings_sync.following_banner(name) if locked else "")
        self.follow_banner.setVisible(locked)

    def _follow_banner(self) -> QWidget:
        """Shown at the top of Style while this PC follows another machine's design, whose followed
        controls are locked until Stop following (settings_sync.follow_lock)."""
        self.follow_banner = QFrame()
        self.follow_banner.setProperty("vernier", "banner")
        row = QVBoxLayout(self.follow_banner)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(8)
        self.follow_banner_label = widgets.label("", "heading", wrap=True)
        row.addWidget(self.follow_banner_label)
        self.stop_follow_button = widgets.ActionButton(settings_sync.STOP_FOLLOWING)
        self.stop_follow_button.setProperty("vernier", "small")
        self.stop_follow_button.clicked.connect(self._stop_following)
        row.addWidget(self.stop_follow_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.follow_banner.setVisible(False)
        return self.follow_banner

    def _stop_following(self) -> None:
        self.design_follow_choice.setCurrentIndex(0)

    def _design_follow_chosen(self, _index: int) -> None:
        if self._config is None:
            return
        self._config.design_follow_peer = self.design_follow_choice.currentData(Qt.ItemDataRole.UserRole) or ""
        self._followed_version = None
        if not self._config.design_follow_peer:
            self._design_path = [self._config.machine_id]
            self._config.design_set_at = settings_sync.next_stamp(self._config.design_set_at, time.time())
            self._config.design_by = self._config.machine_id
        if not self._persist():
            return
        if not self._config.design_follow_peer:
            self._send_same()
        self._refresh_design_follow()
        self._apply_followed_design(force=True)

    def _apply_followed_design(self, force=False) -> None:
        config = self._config
        peer = config.design_follow_peer
        state = self._design_states.get(peer)
        if not peer or state is None or settings_sync.group_on(config, "design"):
            return
        current = settings_sync.pc_values(config)
        cursor = self._followed_version
        stamp, author = cursor[1:] if cursor and cursor[0] == peer else (-1, "")
        taken = settings_sync.followed_design(
            current, state, peer, peer, settings_sync.group_on(config, "design"), stamp,
            author, own_id=config.machine_id, force=force,
        )
        if taken is None:
            return
        cycle = taken.get("cycle", False)
        path = [config.machine_id] if cycle else taken["path"]
        announce = cycle or taken["changed"] or path != self._design_path or force
        if cycle:
            config.design_follow_peer = ""
        else:
            settings_sync.apply_pc(config, taken["values"])
        if announce:
            config.design_set_at = settings_sync.next_stamp(config.design_set_at, time.time())
            config.design_by = config.machine_id
        self._design_seen = tuple(getattr(config, key) for key in settings_sync.DESIGN_KEYS)
        # A followed design is not a choice made here: no value of it is stamped, as on the Mac.
        self._same_seen = self._same_fields()
        self._reflect_config(config)
        if not self._persist():
            return
        self._design_path = path
        self._followed_version = None if cycle else (peer, taken["set_at"], taken["by"])
        if announce:
            self._send_same()
        self._refresh_design_follow()

    def _send_same(self, source: Optional[bytes] = None, data: Optional[dict] = None, *, layout_only=False) -> None:
        """This PC's state, or `data` unchanged when a newer one arrived and goes on (section 10), to
        every peer that takes it but the one it came from: on the link this PC opened when that is
        up, else on the one the peer opened."""
        state = data if data is not None else self._same_state()
        for peer, caps in self._peer_caps().items():
            if peer != source and ("layout" if layout_only else "settings") in caps:
                self._send_to(peer, protocol.settings_msg(settings_sync.for_capabilities(state, caps)))

    def _peer_caps(self) -> dict:
        """{peer id: capabilities} for every peer with a link up, in either direction."""
        desktops = {protocol.read_id(entry.get("id")) for entry in peerlist.desktops(self._peer_entries)}
        caps = {peer: self.sender.links.caps(peer) for peer in self.sender.peers_up()}
        for peer in self.server.links():
            caps.setdefault(peer, self.server.caps_of(peer) or frozenset())
        return {peer: known for peer, known in caps.items() if peer in desktops}

    def _send_to(self, peer: bytes, message: dict) -> bool:
        return self.sender.send_to(peer, message) or self.server.send(peer, message)

    def _identify_screens(self) -> None:
        self._displays_changed()
        tiles = {**self._config.layout, **ways.derive_layout(self._crossing_view_settings(), gap=0),
                 **self.atlas_canvas.tiles}
        self._on_identify_screens(display_numbers(tiles.get(self._config.machine_id)))
        for peer, caps in self._peer_caps().items():
            if "identify_screens" in caps:
                self._send_to(peer, protocol.identify_screens_msg(display_numbers(tiles.get(protocol.id_text(peer)))))

    def _receive_identify_screens(self, peer, labels) -> None:
        if isinstance(peer, bytes):
            peer = protocol.id_text(peer)
        if peer in {entry.get("id") for entry in peerlist.desktops(self.book.peers())}:
            self.bridge.identify.emit(labels)

    def _on_identify_screens(self, labels) -> None:
        self._refresh_own_displays()
        displays = self._own_displays()
        if displays and set(labels) != set(displays):
            physical = {key: ({"x": rect.x, "y": rect.y, "w": rect.width, "h": rect.height}
                              if not isinstance(rect, dict) else rect) for key, rect in displays.items()}
            labels = display_numbers({"x": 0, "y": 0, "w": 160, "h": 100, "displays": physical})
        self.identify_screens.show_numbers(labels, displays)

    def _desk_messages(self, desk: dict, caps) -> list:
        if "profiles" not in caps:
            return []
        envelope = self._same_state()
        return [protocol.settings_msg(settings_sync.for_capabilities(
            settings_sync.desk_message_data(envelope, frame), caps)) for frame in profiles.frames(desk)]

    def _carry_desk(self, outcome, *, source=None) -> None:
        source_id = protocol.read_id(source) if isinstance(source, str) else source
        self._publish_desk(outcome["settings"])
        for peer, caps in self._peer_caps().items():
            desk = outcome["forward"] if peer != source_id else outcome["answer"]
            if desk:
                for message in self._desk_messages(desk, caps):
                    self._send_to(peer, message)

    def _publish_desk(self, settings=None) -> None:
        """Send every desk register changed since the last publish, whatever changed it: a crossing
        event, a pairing, a peer removed, a load that reconciled. Receivers ignore one they hold."""
        if settings is None:
            try:
                with app_config.SETTINGS_LOCK:
                    settings = app_config.load_settings(self.config_path)
            except (OSError, ValueError, app_config.SettingsFileError):
                LOGGER.exception("could not read the settings to publish the desk")
                return
        desk = profiles.desk_message(settings)
        # The first publish sends the whole desk: it is what any linked peer was last told.
        held, self._desk_published = getattr(self, "_desk_published", {}), desk
        paths = profiles.changed_paths(held, desk)
        if paths:
            changed = profiles.desk_message({"crossing_desk": desk}, paths)
            for peer, caps in self._peer_caps().items():
                for message in self._desk_messages(changed, caps):
                    self._send_to(peer, message)

    def _announce(self, peer: bytes) -> list:
        """What this PC tells a peer on every new link (section 3): the side it holds for the peer,
        once one has been set; the peers it may send to, when it may send to this one; and its
        Same on all machines state. On the link's thread."""
        if self._config is None:
            return []
        if any(protocol.read_id(item.get("id")) == peer and item.get("port") == 0 for item in self.book.peers()):
            return []
        self._profile_last_heard[protocol.id_text(peer)] = time.time()
        with app_config.SETTINGS_LOCK:
            settings = app_config.load_settings(self.config_path)
            held_settings = copy.deepcopy(settings)
            for entry in settings["peers"]:
                known = self._peer_caps().get(protocol.read_id(entry.get("id")))
                if known is not None:
                    entry["caps"] = sorted(known)
            outcome = crossing_state.link_up(
                settings, protocol.id_text(peer), platform="pc", own_displays=self._own_displays(),
                now=time.time(), capable=self._layout_capable())
            outcome["changed"] = outcome["settings"] != held_settings
            # The opening link sends the returned list after this callback; sending its
            # picture here would overtake the arrangement, especially for a gap peer.
            carry = dict(outcome, tell=[ident for ident in outcome["tell"] if ident != protocol.id_text(peer)],
                         send_picture=False)
            self._carry_crossing(carry, source=protocol.id_text(peer), redraw=False)
            if outcome["send_picture"]:
                self._send_same(source=peer, layout_only=True)
        peers = outcome["settings"]["peers"]
        entry = next((item for item in peers if protocol.read_id(item.get("id")) == peer), None)
        if entry is None:
            return []
        messages = []
        author = protocol.read_id(entry.get("side_by")) or protocol.read_id(self._config.machine_id)
        if (entry["id"] in outcome["tell"] or entry["id"] in self._layout_capable()) and author is not None:
            messages.append(protocol.arrangement_v6(
                entry.get("side") or "right", int(entry.get("side_set_at", 0)), author,
                way_back=ways.has_way(outcome["settings"], entry["id"]),
                way_back_by=protocol.read_id(ways.way_back_by(outcome["settings"], entry["id"]))))
        if entry.get("in_use", True) is True and entry.get("send"):
            others = [protocol.read_id(ident) for ident in peerlist.paired_ids(
                peers, exclude=protocol.id_text(peer))]
            messages.append(protocol.paired_msg(others))
        if entry.get("port") != 0 and set(self._peer_caps().get(peer, ())) & {"settings", "layout"}:
            messages.append(protocol.settings_msg(settings_sync.for_capabilities(
                self._same_state(), self._peer_caps().get(peer, ()))))
        messages.extend(self._desk_messages(profiles.desk_message(outcome["settings"]),
                                            self._peer_caps().get(peer, ())))
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
        name = machine_name(config.name)
        return {
            "id": protocol.read_id(config.machine_id) or b"",
            "name": name,
            "platform": OWN_PLATFORM,
            "app": VERSION[:32] if VERSION.isascii() and VERSION.isprintable() else "dev",
            # A swipe has no shortcut every Linux desktop shares, so Linux does not offer gestures.
            "caps": [cap for cap in ("clipboard", "clipboard_image", "gestures", "media_keys", "text", "settings", "design_sync", "layout", "profiles", "displays", "identify_screens", "ping_rtt", "motion_path")
                     if not (cap == "gestures" and OWN_PLATFORM == "linux")],
            "port": self._settings_port,
        }

    def _zones(self) -> list:
        try:
            return self.book.zones()
        except (ConfigError, OSError):
            return []

    def _set_same(self, on: bool) -> None:
        """Turning it on carries this PC's Crossing and Design across, each value under its own
        stamp, so the latest choice of each wins wherever it was made; off, each end keeps what it
        has. Either way the Mac follows, now if a link is up, else when one next comes up."""
        if self._config is None:
            return
        config = self._config
        # Each value keeps the stamp it held, never the new one: turning it on or off changes no value.
        held = settings_sync.held_stamps(config.same_stamps, config.same_set_at, config.same_by)
        config.same_stamps = settings_sync.stored_stamps(held)
        if on and not config.same_groups:
            config.same_groups = list(settings_sync.ALL_GROUPS)
        if not on:
            config.same_groups = []
        config.same_on_both = bool(on)
        config.same_set_at = settings_sync.next_stamp(config.same_set_at, time.time())
        config.same_by = config.machine_id
        self._persist()
        self._send_same()
        if not on:
            self._apply_followed_design(force=True)
        self._show_same()

    def _on_settings(self, peer: str, data) -> None:
        """A peer's settings message, over either link. Its state stands only when it is newer than
        this PC's; then its values replace the shared ones here, and it goes on to every other peer
        that takes it, which stops the first time it meets a copy it already holds."""
        if self._config is None or any(entry.get("id") == peer and entry.get("port") == 0 for entry in self.book.peers()):
            return
        settings = app_config.load_settings(self.config_path)
        config = self._config
        taken = settings_sync.arrived(data, config.same_set_at, TRIGGER_KEYS, by_here=config.same_by,
                                      current_groups=config.same_groups)
        policy_groups = settings_sync.arrival_groups(data, config.same_groups) if taken is not None else config.same_groups
        policy_on = taken[0] if taken is not None else config.same_on_both
        current_names = settings_sync.names_from_settings(settings)
        updates, own_name = settings_sync.names_arrived(
            data, current_names, self._config.machine_id,
            current_groups=config.same_groups, policy_groups=policy_groups if policy_on else (),
        )
        names_changed = settings_sync.apply_names(settings, updates)
        name_snapshot = None
        if names_changed:
            labels_changed = any(current_names.get(ident, {}).get("label", "") != entry["label"]
                                 for ident, entry in updates.items())
            name_snapshot = settings_sync.same_snapshot(
                self._config.same_groups, settings_sync.pc_values(self._config), value_groups=(), names=current_names,
            ) if labels_changed else None
            if name_snapshot is not None:
                settings["same_before_sync"] = name_snapshot
            app_config.write_settings(self.config_path, settings)
            if own_name is not None:
                self._config = replace(self._config, name=own_name)
                self.own_name_label.setText(own_name or platform.node())
            self._refresh_peers()
        source_id = protocol.read_id(peer)
        if source_id is not None and "profiles" in self._peer_caps().get(source_id, ()):
            desk_state = profiles.read_desk_state(data)
            if desk_state is not None:
                with app_config.SETTINGS_LOCK:
                    outcome = crossing_state.desk_arrived(
                        app_config.load_settings(self.config_path), desk_state, source=peer,
                        linked=self._live(peer), **self._crossing_event_args())
                    self._profile_last_heard[peer] = None
                    self._carry_crossing(outcome, source=peer)
        if isinstance(data, dict) and "desk" in data:
            data = {key: value for key, value in data.items() if key != "desk"}
        if source_id is not None and "layout" in self._peer_caps().get(source_id, ()):
            layout_state = settings_sync.read_layout_state(data)
            if layout_state is not None:
                with app_config.SETTINGS_LOCK:
                    outcome = crossing_state.receive_picture(
                        app_config.load_settings(self.config_path), layout_state, source=peer,
                        own_displays=self._own_displays(), **self._crossing_event_args())
                    self._carry_crossing(outcome, source=peer, arrived=data)
        was_design_on = settings_sync.group_on(self._config, "design")
        state = settings_sync.read_design_state(data, TRIGGER_KEYS)
        if state is not None:
            held = self._design_states.get(peer)
            if held is None or settings_sync.design_arrived(state, held["set_at"], held["by"]):
                self._design_states[peer] = state
        source = protocol.read_id(peer)
        # Key by key (rc14, 10-10-2026): a value is taken only when its own stamp is newer than the
        # one held here, and the whole state, newer or not, decides only on, off and the groups.
        merged = settings_sync.merge(
            data, self._config.same_stamps, self._config.same_set_at, TRIGGER_KEYS, by_here=self._config.same_by,
            on_here=self._config.same_on_both, current_groups=self._config.same_groups,
        )
        policy = merged["policy"] if merged is not None else None
        values, taken = {}, False
        if merged is not None and merged["values"]:
            values, conflict = ways.shared_trigger_values({"peers": self.book.peers()}, merged["values"])
            if conflict is not None:
                self._show_trigger_conflict(conflict)
            # A value kept here over a clash is not taken: counted, every copy of the message would be
            # taken again and sent on, round three machines for ever.
            taken = any(values.get(key) == value for key, value in merged["values"].items())
        if merged is not None and (policy is not None or taken):
            old_groups = settings_sync.normalise_groups(self._config.same_groups)
            before_values = settings_sync.pc_values(self._config)
            snapshot = settings_sync.snapshot_for_arrival(
                data, self._config.same_set_at, TRIGGER_KEYS, self._config.same_by,
                old_groups, before_values, applied_values=values, merged=merged,
            )
            if snapshot is not None:
                if name_snapshot is not None:
                    snapshot["names"] = name_snapshot["names"]
                    snapshot.pop("selection_only", None)
                settings = app_config.load_settings(self.config_path)
                settings["same_before_sync"] = snapshot
                app_config.write_settings(self.config_path, settings)
            config = replace(self._config, crossing_methods=list(self._config.crossing_methods),
                             crossing_edge_parts=list(self._config.crossing_edge_parts))
            # Every key's stamp is fixed before the whole state's moves; a value taken brings its own,
            # unless the local trigger was kept over it.
            held = settings_sync.held_stamps(config.same_stamps, config.same_set_at, config.same_by)
            held.update({key: stamp for key, stamp in merged["stamps"].items()
                         if key in values and values[key] == merged["values"][key]})
            config.same_stamps = settings_sync.stored_stamps(held)
            if policy is not None:
                config.same_groups = list(merged["groups"])
                config.same_on_both = merged["on"]
                config.same_set_at = policy[1]
                config.same_by = data.get("by", "") if isinstance(data.get("by"), str) else ""
            if values:
                design_stamp = settings_sync.design_change_stamp(
                    settings_sync.pc_values(config), values, config.design_set_at,
                    state, config.machine_id,
                )
                settings_sync.apply_pc(config, values)
                if design_stamp is not None:
                    config.design_set_at = design_stamp["set_at"]
                    config.design_by = design_stamp["by"]
            try:
                app_config.validate_config(config)
            except ConfigError:
                LOGGER.exception("Settings from a peer could not be applied")
                return
            self._config = config
            self._same_seen = self._same_fields()
            self._design_seen = self._design_fields()
            if not self._persist():
                return
            LOGGER.info("Settings from a peer applied (same on all machines %s)", "on" if config.same_on_both else "off")
            self.sender.update_config(config)
            self._configure_trigger(config)
            self._reflect_config(config)
            self._show_same()
            forwarded = dict(data)
            forwarded["design_sync"] = self._same_state()["design_sync"]
            self._send_same(source=source, data=forwarded)
        elif names_changed:
            self._send_same(source=source, data=data)
        if merged is not None and merged["answer"]:
            # This PC holds a value newer than the sender's: its own state is how the sender learns it.
            self._send_same()
        if policy is not None and was_design_on and not settings_sync.group_on(self._config, "design"):
            self._apply_followed_design(force=True)
        elif state is not None:
            self._apply_followed_design()
        if state is not None:
            self._refresh_design_follow()

    def _sign_in_module(self) -> QWidget:
        module = widgets.Module(tr("At sign-in"))
        self.logon_switch = widgets.Switch(tr("Start Beamer when you sign in"))
        self.logon_switch.setFont(theme.font(theme.TYPE["body"]))
        exe = autostart_win.installed_exe()
        account_known = True
        try:
            can_start = exe is not None and autostart_win.can_run_for_current_user()
        except OSError:
            LOGGER.exception("Could not identify the signed-in account")
            can_start = False
            account_known = False
        import admin_mode
        try:
            admin_on = admin_mode.enabled()
        except OSError:
            admin_on = False
        enabled = False
        try:
            enabled = exe is not None and autostart_win.is_enabled()
        except OSError:
            LOGGER.exception("Could not read the logon task")
        self.logon_switch.setChecked(enabled)
        self.logon_switch.setEnabled(exe is not None and (can_start or enabled))
        self.logon_switch.toggled.connect(self._set_start_at_logon)
        module.body.addWidget(self.logon_switch)
        if exe and not account_known:
            hint = tr("Windows could not identify the signed-in account, so start at sign-in is unavailable.")
        elif exe and not can_start:
            hint = tr("Start at sign-in needs Beamer to run as the signed-in account.")
        elif enabled:
            hint = (tr("Starts in the tray when you sign in, as administrator.") if admin_on
                    else tr("Starts in the tray when you sign in, without an administrator prompt."))
        elif exe:
            hint = (tr("When enabled, Beamer starts in the tray as administrator.") if admin_on
                    else tr("When enabled, Beamer starts in the tray with your normal permissions."))
        else:
            hint = tr("Only the installed app can start at sign-in.")
        self.logon_hint = widgets.label(hint, "note", wrap=True)
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
            self.logon_hint.setText(tr("Windows refused: {error}").format(error=exc))
            return
        self.logon_hint.setText(tr("In the tray, with no window."))

    # -- Crossing -----------------------------------------------------------------------------

    def _crossing_page(self, layout, current: Config) -> None:
        self.crossing_document = layout.parentWidget().parentWidget()
        intro = widgets.label("", "note", wrap=True)
        self.profile_intro = intro
        intro.setMaximumWidth(560)
        # A VBox measures capped labels at the full column width, losing a wrapped line.
        intro_line = QHBoxLayout()
        intro_line.setContentsMargins(0, 0, 0, 0)
        intro_line.addWidget(intro, 1)
        intro_line.addStretch()
        layout.addLayout(intro_line)
        layout.addWidget(self._ways_module(current))
        layout.addWidget(self._profile_override_module())
        layout.addWidget(self._profile_ledger_module())
        layout.addWidget(self._way_home_module())
        self.pointer_module = self._pointer_module(current)
        layout.addWidget(self.pointer_module)
        desktop = linux_desktop.desktop() if sys.platform.startswith("linux") else ""
        note = ("On KDE Plasma, the tray uses the system tray. KWin virtual desktop shortcuts are separate "
                "from Beamer's shortcut; check their bindings in System Settings → Shortcuts."
                if desktop == "kde" else "On GNOME, the tray needs the AppIndicator extension.")
        self.linux_tray_note = widgets.label(note, "note", wrap=True)
        self.linux_tray_note.setVisible(desktop in ("kde", "gnome"))
        layout.addWidget(self.linux_tray_note)
        self.resistance_module = self._resistance_module(current)
        layout.addWidget(self.resistance_module)
        self.shortcut_module = self._shortcut_module(current)
        layout.addWidget(self.shortcut_module)
        self._reflect_ways()
        self._reflect_other_name()

    @staticmethod
    def _row(*items) -> QWidget:
        """Widgets and layouts that show and hide as one row of a module."""
        if len(items) >= 2 and isinstance(items[0], QLabel) and items[0].property("vernier") == "key":
            controls = [item for item in items[1:]
                        if isinstance(item, QLayout) or isinstance(item, QWidget) and not isinstance(item, QLabel)]
            if len(controls) == 1:
                control = controls[0]
                if isinstance(control, QLayout):
                    holder = QWidget()
                    control.setContentsMargins(0, 0, 0, 0)
                    holder.setLayout(control)
                    holder.setMaximumWidth(360)
                    control = holder
                notes = [item for item in items[1:] if isinstance(item, QLabel)]
                return widgets.FormRow(items[0], control, notes)
        row = QWidget()
        row.setProperty("vernier", "plain")
        column = QVBoxLayout(row)
        column.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        for item in items:
            (column.addLayout if isinstance(item, QHBoxLayout) else column.addWidget)(item)
        return row

    def _ways_module(self, current: Config) -> QWidget:
        from crossing_ledger import _Flow
        module = widgets.Module("", heading=True)
        module.eyebrow.hide()
        self.ways_module = module
        self.profile_header = QWidget()
        self.profile_header.setProperty("vernier", "plain")
        header = QHBoxLayout(self.profile_header)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(12)
        self.profile_tabs = QWidget()
        self.profile_tabs.setProperty("vernier", "plain")
        self.profile_tabs_layout = _Flow(self.profile_tabs)
        self.profile_buttons = {}
        header.addWidget(self.profile_tabs, 1)
        self.same_ways_switch = widgets.Switch(tr("Same ways on every machine"))
        self.same_ways_switch.toggled.connect(lambda on: self._profile_event(crossing_state.set_same_ways, on))
        header.addWidget(self.same_ways_switch)
        self.profile_header.installEventFilter(self)
        module.body.addWidget(self.profile_header)
        self.profile_actions = QWidget()
        self.profile_actions.setProperty("vernier", "plain")
        actions = _Flow(self.profile_actions)
        self.profile_new = QPushButton(tr("New profile…"))
        self.profile_new.clicked.connect(lambda: self._ask_profile_name())
        # A click on a tab switches the whole desk, so a tab cannot be picked for an action: Rename
        # names the active profile, and Delete offers the others (the active one never goes).
        self.profile_rename = QPushButton(tr("Rename"))
        self.profile_rename.clicked.connect(
            lambda: self._ask_profile_name(self._profile_settings()["crossing_desk"]["active"]["value"]))
        self.profile_delete = QPushButton(tr("Delete…"))
        self.profile_delete.clicked.connect(self._show_profile_delete_menu)
        for button in (self.profile_new, self.profile_rename, self.profile_delete):
            button.setProperty("vernier", "small")
            button.setFlat(True)
            actions.addWidget(button)
        module.body.addWidget(self.profile_actions)
        self.profile_off_note = widgets.label(tr("Set every machine's ways from this screen. Turning it on changes nothing until you do; each machine's own ways are kept."), "note", wrap=True)
        module.body.addWidget(self.profile_off_note)
        self.profile_name_row = QWidget()
        name_line = QHBoxLayout(self.profile_name_row)
        name_line.setContentsMargins(0, 0, 0, 0)
        self.profile_name = QLineEdit()
        self.profile_name.setMaxLength(32)
        self.profile_name.setAccessibleName(tr("Profile name"))
        name_line.addWidget(self.profile_name, 1)
        save = QPushButton(tr("Save"))
        save.clicked.connect(self._save_profile_name)
        self.profile_name.returnPressed.connect(self._save_profile_name)
        name_line.addWidget(save)
        cancel = QPushButton(tr("Cancel"))
        cancel.clicked.connect(self.profile_name_row.hide)
        name_line.addWidget(cancel)
        self.profile_name_row.hide()
        module.body.addWidget(self.profile_name_row)
        self.profile_note = widgets.label("", "note-amber", wrap=True)
        self.profile_note.hide()
        module.body.addWidget(self.profile_note)
        self.atlas_canvas = AtlasCanvas(
            on_drop=self._layout_dropped, on_rename=self._rename_canvas_peer,
            on_leave_out=self._canvas_left_out, on_cycle=self._set_shortcut_cycle,
            on_select=self._choose_machine, on_third=self._canvas_third,
            on_edit_machine=self._edit_profile_machine)
        self.atlas_canvas.on_hint = self.atlas_canvas.announce
        self.atlas_canvas.setMinimumHeight(260)
        self.atlas_canvas.setMaximumHeight(260)
        self.atlas_canvas.ledger_height = True
        module.body.addWidget(self.atlas_canvas)
        self.atlas_canvas_hint = widgets.label(
            tr("Alt-click a display to split or group · drag to move · click a third of a lit edge to leave it out · drag a corner to resize"),
            "note", wrap=True)
        module.body.addWidget(self.atlas_canvas_hint)
        if sys.platform.startswith("linux"):
            module.body.addWidget(widgets.label(tr("Seam crossings need X11 on Linux."), "note", wrap=True))
        legend = QWidget()
        legend.setProperty("vernier", "plain")
        line = QHBoxLayout(legend)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(6)
        for tone, text in (("signal", N_("Crosses")), ("ink_3", N_("Offline, ways kept"))):
            dot = QLabel()
            dot.setFixedSize(6, 6)
            dot.setStyleSheet(f"background: {theme.colour(tone)}; border-radius: 3px")
            line.addWidget(dot)
            line.addWidget(widgets.label(tr(text), "note", wrap=True))
            line.addSpacing(12)
        line.addStretch()
        self.identify_button = QPushButton(tr("Identify screens"))
        self.identify_button.setProperty("vernier", "small")
        self.identify_button.setToolTip(tr("Show these numbers on every linked machine's screens for 3 seconds."))
        self.identify_button.clicked.connect(self._identify_screens)
        module.body.addWidget(legend)
        module.body.addWidget(self.identify_button, 0, Qt.AlignmentFlag.AlignRight)
        self.profile_switch_note = widgets.label(tr("Switching moves every machine to this profile's places and ways."), "note", wrap=True)
        module.body.addWidget(self.profile_switch_note)
        return module

    def _profile_ledger_module(self) -> QWidget:
        from crossing_ledger import CrossingLedger
        module = widgets.Module("", heading=True)
        self.profile_ledger_module = module
        module.eyebrow.setProperty("profile_heading", True)
        self.profile_editing_row = QWidget()
        self.profile_editing_row.setProperty("vernier", "plain")
        editing = QHBoxLayout(self.profile_editing_row)
        editing.setContentsMargins(0, 0, 0, 0)
        editing.setSpacing(6)
        self.profile_editing = widgets.ElidingLabel("")
        self.profile_editing.setProperty("vernier", "body")
        editing.addWidget(self.profile_editing)
        self.profile_back = QPushButton(tr("Back to This PC"))
        self.profile_back.setProperty("vernier", "small")
        self.profile_back.setFlat(True)
        self.profile_back.clicked.connect(lambda: self._edit_profile_machine(self._config.machine_id))
        editing.addWidget(self.profile_back)
        editing.addStretch()
        module.body.addWidget(self.profile_editing_row)
        self.profile_machine_status = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.profile_machine_status)
        self.profile_remote_note = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.profile_remote_note)
        self.profile_permissions_note = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.profile_permissions_note)
        self.crossing_ledger = CrossingLedger(self)
        module.body.addWidget(self.crossing_ledger)
        module.body.addWidget(widgets.label(
            tr("Each chip is a side of this screen; click one to add or remove it. The side a machine touches "
            "crosses on its lit thirds; any other free side wraps round to it, arriving on its far side. "
            "A corner is a diagonal push, shown as a square on the map."), "note", wrap=True))
        return module

    def _profile_override_module(self) -> QWidget:
        from crossing_ledger import _Flow
        module = widgets.Module("", heading=True)
        self.profile_override = module
        self.profile_override_row = QWidget()
        self.profile_override_row.setProperty("vernier", "plain")
        own_line = QHBoxLayout(self.profile_override_row)
        own_line.setContentsMargins(0, 0, 0, 0)
        self.profile_keep_own = QPushButton(tr("Keep this machine's own ways"))
        self.profile_keep_own.setProperty("vernier", "small")
        self.profile_keep_own.clicked.connect(lambda: self._profile_event(crossing_state.set_own_ways, True))
        own_line.addWidget(self.profile_keep_own)
        self.profile_archive_toggle = QPushButton(tr("Earlier ways"))
        self.profile_archive_toggle.setProperty("vernier", "small")
        self.profile_archive_toggle.setCheckable(True)
        own_line.addWidget(self.profile_archive_toggle)
        own_line.addStretch()
        self.profile_override_row.installEventFilter(self)
        module.body.addWidget(self.profile_override_row)
        actions = QWidget()
        actions.setProperty("vernier", "plain")
        self.profile_own_actions = actions
        line = _Flow(actions)
        self.profile_use_desk = QPushButton(tr("Use the desk's ways"))
        self.profile_use_desk.clicked.connect(lambda: self._profile_event(crossing_state.set_own_ways, False))
        line.addWidget(self.profile_use_desk)
        self.profile_share_mine = QPushButton(tr("Share mine with the desk"))
        self.profile_share_mine.clicked.connect(lambda: self._profile_event(crossing_state.share_mine))
        line.addWidget(self.profile_share_mine)
        module.body.insertWidget(1, actions)
        self.profile_archive = QWidget()
        self.profile_archive.setProperty("vernier", "plain")
        self.profile_archive_rows = QVBoxLayout(self.profile_archive)
        self.profile_archive_rows.setContentsMargins(0, 0, 0, 0)
        self.profile_archive_toggle.toggled.connect(self._profile_show_archive)
        self.profile_archive.hide()
        module.body.addWidget(self.profile_archive)
        self.profile_load_note = widgets.label(tr("This machine kept the ways set while it ran an older Beamer."), "note-amber", wrap=True)
        self.profile_load_note.hide()
        module.body.addWidget(self.profile_load_note)
        return module

    def _profile_show_archive(self, opened):
        self.profile_archive.setVisible(opened)
        self.profile_archive_toggle.setText(tr("Earlier ways ▴") if opened else tr("Earlier ways ▾"))

    def _way_home_module(self) -> QWidget:
        module = widgets.Module(tr("Way home"), heading=True)
        self.way_home_module = module
        module.eyebrow.setProperty("profile_heading", True)
        self.shake_home_switch = widgets.Switch(tr("Shake to come home"))
        self.shake_home_switch.toggled.connect(lambda on: self._profile_event(crossing_state.set_shake, on))
        module.body.addWidget(self.shake_home_switch)
        self.shake_sensitivity = widgets.Ruler(tr("Shake sensitivity"), 200, minimum=50,
                                               step=10, ticks=(50, 100, 150, 200))
        self.shake_sensitivity.valueChanged.connect(self._shake_sensitivity_changed)
        self.shake_sensitivity_figure = widgets.Figure("100 %")
        self.shake_sensitivity_row = widgets.FormRow(
            widgets.label(tr("Shake sensitivity"), "key"), self.shake_sensitivity_figure)
        module.body.addWidget(self.shake_sensitivity_row)
        module.body.addWidget(self.shake_sensitivity)
        module.body.addWidget(widgets.label(tr("Shake the mouse or trackpad left and right a few times while you drive another machine, and input comes straight back to the machine you are using."), "note", wrap=True))
        self.escape_home_switch = widgets.Switch(tr("Press Esc three times to come home"))
        self.escape_home_switch.toggled.connect(self._escape_home_toggled)
        module.body.addWidget(self.escape_home_switch)
        module.body.addWidget(widgets.label(tr("Tap Esc three times quickly while you drive another machine. The first two taps still reach that machine; the third brings input home."), "note", wrap=True))
        self.profile_shortcut = widgets.label("", "note", wrap=True)
        self.profile_shortcut.setTextFormat(Qt.TextFormat.RichText)
        self.profile_shortcut.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.profile_shortcut.linkActivated.connect(self._profile_open_shortcut)
        module.body.addWidget(self.profile_shortcut)
        module.body.addWidget(widgets.label(tr("Push back the way you came: where an edge leads back to This PC"), "note", wrap=True))
        self.profile_no_return = widgets.label("", "note", wrap=True)
        self.profile_no_return.setProperty("profile_warning", True)
        module.body.addWidget(self.profile_no_return)
        return module

    def _profile_open_shortcut(self) -> None:
        self._select_page("overview")
        shortcut = self.same_checks["shortcut"]
        shortcut.setFocus()
        area = shortcut.parentWidget()
        while area is not None and not isinstance(area, QScrollArea):
            area = area.parentWidget()
        if area is not None:
            area.ensureWidgetVisible(shortcut)

    def _profile_settings(self) -> dict:
        try:
            return self._window_settings()
        except (ConfigError, OSError):
            # Keep the settings-error window usable without touching the unreadable file.
            if not hasattr(self, "_profile_empty_settings"):
                config = self._config or default_config()
                empty = app_config.migrate(None, machine_id=config.machine_id)
                self._profile_empty_settings, _ = profiles.before_load(empty)
            return self._profile_empty_settings

    def _profile_machine(self) -> str:
        settings = self._profile_settings()
        chosen = getattr(self, "_profile_viewed", None)
        if not profiles.same_ways_on(settings) or chosen not in profiles.machines_on_desk(settings):
            return settings["machine_id"]
        return chosen

    def _profile_remote(self) -> bool:
        return self._profile_machine() != self._profile_settings()["machine_id"]

    def _profile_caps(self, machine, settings=None) -> set:
        settings = settings or self._profile_settings()
        if machine == settings["machine_id"]:
            return {"layout", "profiles"}
        known = self._design_peer_caps()
        if machine in known:
            return set(known[machine])
        entry = ways._entry(settings, machine) or {}
        if "caps" in entry:
            return set(entry["caps"])
        # Relayed machines have no local handshake; their own status proves profiles support.
        return {"layout", "profiles"} if machine in settings["crossing_desk"].get("status", {}) else set()

    def _profile_read_only(self) -> bool:
        return self._profile_remote() and "profiles" not in self._profile_caps(self._profile_machine())

    def _crossing_view_settings(self) -> dict:
        settings = self._profile_settings()
        if profiles.same_ways_on(settings):
            viewed = profiles.view_settings(settings, self._profile_machine())
            platforms = {entry["id"]: entry.get("platform") for entry in settings["peers"]}
            platforms[settings["machine_id"]] = "linux" if sys.platform.startswith("linux") else "windows"
            for entry in viewed["peers"]:
                if platforms.get(entry["id"]):
                    entry["platform"] = platforms[entry["id"]]
            return viewed
        return self._crossing_settings()

    @_crossing_refresh
    def _edit_profile_machine(self, machine) -> None:
        self._profile_viewed = machine
        self._chosen = None
        self.atlas_canvas.selected = machine
        self._reflect_ways()

    @_crossing_refresh
    def _profile_event(self, event, *args) -> bool:
        if self._config is None:
            return False
        full_args = (crossing_state.set_same_ways, crossing_state.set_own_ways,
                     crossing_state.switch_profile, crossing_state.restore_saved)
        options = self._crossing_event_args() if event in full_args else dict(platform="pc", now=time.time())
        try:
            with app_config.SETTINGS_LOCK:
                settings = app_config.load_settings(self.config_path)
                outcome = event(settings, *args, **options)
                if not outcome.get("accepted", True):
                    message = {"desk_full": tr("This desk is full. Remove an unused profile before adding more ways."),
                               "invalid_name": tr("Give the profile a name of 1 to 32 characters.")}.get(
                                   outcome.get("reason"), tr("These ways could not be changed."))
                    self.profile_note.setText(message)
                    self.profile_note.show()
                    return False
                self.profile_note.hide()
                self._carry_crossing(outcome)
            self._show_same()
            if hasattr(self, "profile_menu"):
                self._refresh_tray_profiles()
            return True
        except (ConfigError, OSError):
            LOGGER.exception("The profile could not be saved")
            self.profile_note.setText(tr("These ways could not be saved. Try again."))
            self.profile_note.show()
            return False

    def _ask_profile_name(self, profile_id=None) -> None:
        self._profile_naming = profile_id
        held = next((item for item in profiles.profile_list(self._profile_settings()) if item["id"] == profile_id), None)
        self.profile_name.setText(held["name"] if held else "")
        self.profile_name_row.show()
        self.profile_name.setFocus()
        self.profile_name.selectAll()

    def _save_profile_name(self) -> None:
        profile_id = getattr(self, "_profile_naming", None)
        args = (profile_id, self.profile_name.text()) if profile_id else (self.profile_name.text(),)
        if self._profile_event(crossing_state.rename_profile if profile_id else crossing_state.new_profile, *args):
            self.profile_name_row.hide()

    def _profile_delete_menu(self) -> QMenu:
        """The profiles Delete… offers: every one but the active profile."""
        settings = self._profile_settings()
        active = settings["crossing_desk"]["active"]["value"]
        menu = QMenu(self.profile_delete)
        for item in profiles.profile_list(settings):
            if item["id"] != active:
                menu.addAction(item["label"], lambda p=item["id"]: self._profile_event(crossing_state.remove_profile, p))
        return menu

    def _show_profile_delete_menu(self) -> None:
        self._profile_delete_menu().exec(self.profile_delete.mapToGlobal(self.profile_delete.rect().bottomLeft()))

    def _profile_tab_menu(self, profile_id, button, point) -> None:
        menu = QMenu(button)
        menu.addAction(tr("Rename"), lambda: self._ask_profile_name(profile_id))
        remove = menu.addAction(tr("Delete"), lambda: self._profile_event(crossing_state.remove_profile, profile_id))
        settings = self._profile_settings()
        remove.setEnabled(len(profiles.profile_list(settings)) > 1 and
                          settings["crossing_desk"]["active"]["value"] != profile_id)
        menu.exec(button.mapToGlobal(point))

    def _reflect_profiles(self, settings) -> None:
        config = self._config or default_config()
        on = profiles.same_ways_on(settings)
        remote = self._profile_remote()
        machine = self._profile_machine()
        name = self._profile_machine_name(settings, machine)
        self.profile_intro.setText(tr("Click a machine on the map to set its ways. The ledger says how it reaches each other machine.") if on else
                                   tr("Drag a machine on the map to where it sits. The ledger says how each one is reached; click a row to pick it on the map."))
        self.same_ways_switch.blockSignals(True)
        self.same_ways_switch.setChecked(on)
        self.same_ways_switch.blockSignals(False)
        self.same_ways_switch.setEnabled(self._config is not None)
        self.profile_tabs.setVisible(on)
        self.profile_actions.setVisible(on)
        self.atlas_canvas_hint.setText(tr("Click a machine to set its ways · Tab and Return pick one from the keyboard") if on else
                                     tr("Alt-click a display to split or group · drag to move · click a third of a lit edge to leave it out · drag a corner to resize"))
        self.atlas_canvas_hint.show()
        self.profile_off_note.setVisible(not on)
        self.profile_switch_note.setVisible(on and len(profiles.profile_list(settings)) > 1)
        if not on:
            self.profile_name_row.hide()
        tab_items = profiles.profile_list(settings)
        tab_signature = tuple((item["id"], item["label"]) for item in tab_items)
        if tab_signature != getattr(self, "_profile_tabs_seen", None):
            self._profile_tabs_seen = tab_signature
            while self.profile_tabs_layout.count():
                item = self.profile_tabs_layout.takeAt(0)
                item.widget().deleteLater()
            self.profile_buttons = {}
            for item in tab_items:
                pid = item["id"]
                button = QPushButton(tr("Desk") if pid == profiles.DESK_ID and item["name"] == "Desk" else item["label"])
                button.setCheckable(True)
                button.setProperty("vernier", "choice")
                button.setMinimumWidth(button.sizeHint().width())
                button.installEventFilter(self)
                button.clicked.connect(lambda _checked=False, p=pid: self._profile_event(crossing_state.switch_profile, p))
                button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                button.customContextMenuRequested.connect(lambda point, p=pid, b=button: self._profile_tab_menu(p, b, point))
                self.profile_tabs_layout.addWidget(button)
                self.profile_buttons[pid] = button
            add = QPushButton(tr("+"))
            add.setProperty("vernier", "small")
            add.setFlat(True)
            add.setFixedWidth(24)
            add.setAccessibleName(tr("New profile"))
            add.clicked.connect(lambda: self._ask_profile_name())
            self.profile_tabs_layout.addWidget(add)
        for pid, button in self.profile_buttons.items():
            button.setChecked(pid == settings["crossing_desk"]["active"]["value"])
        self.profile_delete.setEnabled(len(tab_items) > 1)
        own = settings["crossing_own"].get("use_own") is True
        saved = profiles.saved_list(settings)
        notes = app_config.load_notes(self.config_path)
        self._profile_older_note = getattr(self, "_profile_older_note", False) or "older_version" in notes
        self.profile_override.setVisible(not remote and (on or self._profile_older_note))
        self.profile_override.eyebrow.setText(tr("This PC keeps its own ways") if own and on else "")
        self.profile_override.eyebrow.setVisible(own and on)
        self.profile_keep_own.setVisible(on and not own)
        self.profile_use_desk.setVisible(on and own)
        self.profile_own_actions.setVisible(on and own)
        desk = settings["crossing_desk"]
        desk_row = desk["profiles"][desk["active"]["value"]].get("rows", {}).get(settings["machine_id"], {}).get("value", {})
        self.profile_share_mine.setVisible(on and own and settings["crossing_own"]["row"] != desk_row)
        self.profile_archive_toggle.setVisible(on and bool(saved))
        self.profile_archive_toggle.setText(tr("Earlier ways ▴") if self.profile_archive_toggle.isChecked() else tr("Earlier ways ▾"))
        self.profile_override_row.setVisible(on and (not own or bool(saved)))
        signature = repr(saved)
        if signature != getattr(self, "_profile_saved_seen", None):
            self._profile_saved_seen = signature
            while self.profile_archive_rows.count():
                self.profile_archive_rows.takeAt(0).widget().deleteLater()
            for item in saved:
                row = QWidget()
                row.setProperty("vernier", "plain")
                line = QHBoxLayout(row)
                line.setContentsMargins(0, 0, 0, 0)
                line.addWidget(widgets.label(pages_win.saved_ways_label(item, settings), "note", wrap=True), 1)
                for title, event in ((tr("Restore"), crossing_state.restore_saved), (tr("Delete"), crossing_state.delete_saved)):
                    button = QPushButton(title)
                    button.setProperty("vernier", "small")
                    button.clicked.connect(lambda _checked=False, e=event, ident=item["id"]: self._profile_event(e, ident))
                    line.addWidget(button)
                self.profile_archive_rows.addWidget(row)
        self.profile_archive.setVisible(on and bool(saved) and self.profile_archive_toggle.isChecked())
        self.profile_load_note.setVisible(self._profile_older_note)
        self.profile_ledger_module.eyebrow.setText(tr("{name}'s ways").format(name=name))
        self.profile_editing.setText(tr("Editing {name}'s ways ·").format(name=name))
        self.profile_editing_row.setVisible(remote)
        self.profile_editing.setVisible(remote)
        self.profile_back.setVisible(remote)
        self.profile_remote_note.setText(tr("Changes go to {name} at once.").format(name=name))
        self.profile_remote_note.setVisible(remote and not self._profile_read_only())
        self.profile_permissions_note.setText(tr("Jump keys and permissions are set on {name}.").format(name=name))
        self.profile_permissions_note.setVisible(remote)
        if "crossing" in self.scope_labels:
            self.scope_labels["crossing"].setVisible(not on)
        self.profile_machine_status.setVisible(remote)
        if remote:
            self.profile_machine_status.setText(profiles.machine_line(settings, machine, linked=self._live(machine),
                caps=self._profile_caps(machine, settings), last_heard=getattr(self, "_profile_last_heard", {}).get(machine)))
        self.shake_home_switch.blockSignals(True)
        self.shake_home_switch.setChecked(profiles.shake_on(settings))
        self.shake_home_switch.blockSignals(False)
        self.shake_home_switch.setEnabled(self._config is not None)
        self.escape_home_switch.blockSignals(True)
        self.escape_home_switch.setChecked(escape_home_on(settings))
        self.escape_home_switch.blockSignals(False)
        self.escape_home_switch.setEnabled(self._config is not None)
        self.shake_sensitivity.blockSignals(True)
        self.shake_sensitivity.setValue(profiles.shake_sensitivity(settings))
        self.shake_sensitivity_figure.setText(f"{self.shake_sensitivity.value()} %")
        self.shake_sensitivity.blockSignals(False)
        self.shake_sensitivity.setEnabled(self._config is not None)
        key = TRIGGER_KEYS.get(config.trigger_key, config.trigger_key)
        style = tr("hold {key}") if config.trigger_style == "hold" else tr("double-tap {key}")
        keycap = '<span style="font-family: {}">{}</span>'.format(html.escape(theme.mono_font(theme.TYPE["note"]).family(), quote=True), html.escape(key))
        link = '<a href="shortcut" style="color: {}">{}</a>'.format(theme.colour("ink_2"), html.escape(tr("Same → Shortcut")))
        self.profile_shortcut.setText(tr("Shortcut: {shortcut} (set in {link})").format(shortcut=style.format(key=keycap), link=link))
        names = profiles.no_way_back(settings, machine)
        self.profile_no_return.setText("\n".join(tr("{name} has no way back to {machine} except the shortcut and a shake.").format(name=other, machine=name) for other in names))
        self.profile_no_return.setVisible(bool(names))

    def _profile_machine_name(self, settings, machine) -> str:
        if machine == settings["machine_id"]:
            return tr("This PC")
        return self._shown(peerlist.label_for(settings["peers"], peer_id=machine) or
                           settings.get("layout", {}).get(machine, {}).get("name") or tr("Machine"))

    def _pointer_module(self, current: Config) -> QWidget:
        module = widgets.Module(tr("This machine's pointer"), heading=True)
        self.dragging_switch = widgets.Switch(tr("Don't cross while dragging with this PC's pointer"))
        note = tr("Applies when this physical pointer pushes through this screen's ways; visiting pointers are not protected by this switch.")
        self.dragging_switch.setToolTip(note)
        self.dragging_switch.setFont(theme.font(theme.TYPE["body"]))
        self.dragging_switch.setChecked(current.block_while_dragging)
        self.dragging_switch.toggled.connect(self._set_block_while_dragging)
        module.body.addWidget(self.dragging_switch)
        module.body.addWidget(widgets.label(note, "note", wrap=True))
        return module

    # -- the machine the Crossing page shows --

    def _crossing_settings(self) -> dict:
        config = self._config or default_config()
        desktops = peerlist.desktops(self._peer_entries)
        ids = {entry.get("id") for entry in desktops}
        return {"machine_id": config.machine_id, "peers": desktops,
                "zones": [zone for zone in self._zone_entries if zone.get("peer") in ids],
                "layout": config.layout, "layout_set_at": config.layout_set_at, "layout_by": config.layout_by}

    @_crossing_refresh
    def _read_crossing(self) -> None:
        try:
            self._peer_entries, self._zone_entries = self.book.peers(), self.book.zones()
            if self._config is not None:
                saved = app_config.load_settings(self.config_path)
                self._config.layout = saved.get("layout", {})
                self._config.layout_set_at = saved.get("layout_set_at", 0)
                self._config.layout_by = saved.get("layout_by", "")
        except (ConfigError, OSError):
            LOGGER.exception("The peers could not be read")

    def _crossing_machines(self) -> list:
        return [(ident, self._shown(label)) for ident, label in pages_win.crossing_machines(self._peer_entries)]

    def _chosen_machine(self) -> Optional[str]:
        entries = self._crossing_view_settings()["peers"] if self._profile_remote() else peerlist.desktops(self._peer_entries)
        ids = [entry.get("id") for entry in entries]
        return self._chosen if self._chosen in ids else (ids[0] if ids else None)

    def _ways_in(self) -> set:
        """The kinds of zone in use towards any machine: what Pause holds and the Design page previews."""
        settings = self._crossing_settings()
        found = set()
        for ident, _label in self._crossing_machines():
            if ways.has_way(settings, ident):
                found.update(ways.ways(settings, ident)["methods"])
        return found

    @_crossing_refresh
    def _choose_machine(self, peer: str) -> None:
        self._chosen = peer
        self.atlas_canvas.selected = peer
        self._reflect_ways()
        self._reflect_look()

    @_crossing_refresh
    def _layout_dropped(self, layout: dict, moved: str) -> bool:
        if not self._config or moved not in layout:
            self._reflect_ways()
            return False
        with app_config.SETTINGS_LOCK:
            settings = app_config.load_settings(self.config_path)
            if moved == self._config.machine_id:
                held = ways.derive_layout(settings)[moved]
                if (not isinstance(layout[moved], dict)
                        or any(layout[moved].get(axis) != held[axis] for axis in ("x", "y"))):
                    self._reflect_ways()
                    return False
            outcome = crossing_state.drop(settings, layout, moved, snap_radius=12 / max(self.atlas_canvas._scale, 0.001),
                                          **self._crossing_event_args())
            if not outcome["accepted"]:
                self._reflect_ways()
                self.atlas_canvas.announce(tr("That position cannot connect this machine."))
                return False
            self._carry_crossing(outcome)
        return True

    def _crossing_event_args(self) -> dict:
        return dict(platform="pc", capable=self._layout_capable(), live=self._live, gone=self._gone, now=time.time())

    def _layout_capable(self) -> set:
        if self._config is None:
            return set()
        held = {entry.get("id") for entry in self._peer_entries
                if "layout" in entry.get("caps", ()) or "caps" not in entry}
        known = self._design_peer_caps()
        return {self._config.machine_id} | (held - set(known)) | {
            ident for ident, caps in known.items() if "layout" in caps}

    def _own_displays(self) -> dict:
        return copy.deepcopy(self._own_display_map)

    def _refresh_own_displays(self) -> None:
        if hasattr(self.sender, "_desktop_module") and platform.system() in ("Windows", "Linux"):
            desktop = self.sender._desktop_module()
            refresh = getattr(desktop, "refresh_qt_screens", None)
            if callable(refresh):
                refresh()
            self._own_display_map = desktop.display_map()

    def _screen_added(self, screen) -> None:
        screen.geometryChanged.connect(self._displays_changed)
        self._displays_changed()

    def _carry_crossing(self, outcome, *, source=None, arrived=None, redraw=True) -> None:
        if outcome["changed"]:
            saved = outcome["settings"]
            app_config.write_settings(self.config_path, saved)
            self._config = replace(self._config, layout=saved.get("layout", {}),
                                   layout_set_at=saved.get("layout_set_at", 0), layout_by=saved.get("layout_by", ""))
            self._peer_entries, self._zone_entries = saved["peers"], saved["zones"]
            self.sender.update_config(self._config)
            self.sender.refresh()
            self.server.peers_changed()
            self.server.rearm()
            self._configure_shake(saved)
        self._carry_desk(outcome, source=source)
        for peer in outcome["tell"]:
            self._tell(peer)
        if outcome["send_picture"]:
            self._send_same(layout_only=True)
        if outcome["forward_picture"] and arrived is not None:
            self._send_same(source=protocol.read_id(source), data=arrived, layout_only=True)
        if redraw and outcome["changed"]:
            self._reflect_ways()
            self._reflect_look()
            self._refresh_tray_profiles(outcome["settings"])

    def _configure_shake(self, settings=None) -> None:
        settings = app_config.load_settings(self.config_path) if settings is None else settings
        enabled = profiles.shake_on(settings)
        sensitivity = profiles.shake_sensitivity(settings)
        if (enabled, sensitivity) != (self._shake_enabled, self._shake_sensitivity):
            self.sender.set_shake(enabled, shake_stroke(capture_win, sensitivity), on_shake=self.bridge.shook_home.emit)
            self._shake_enabled = enabled
            self._shake_sensitivity = sensitivity
        escape = escape_home_on(settings)
        if escape != self._escape_enabled:
            self.sender.set_escape_home(escape, on_escape=self.bridge.escaped_home.emit)
            self._escape_enabled = escape

    def _escape_home_toggled(self, on: bool) -> None:
        """This machine's own keyboard, so the switch stays here and is not one of the ways Same on
        all machines shares."""
        with app_config.SETTINGS_LOCK:
            settings = app_config.load_settings(self.config_path)
            settings["escape_home"] = bool(on)
            app_config.write_settings(self.config_path, settings)
        self._configure_shake(settings)

    def _shake_sensitivity_changed(self, sensitivity: int) -> None:
        self._profile_event(crossing_state.set_shake_sensitivity, sensitivity)

    def _on_shake(self, peer) -> None:
        if first_notice(self.config_path, "shake_notices"):
            self._on_alert(tr("Beamer"), tr("Shook home from {name}. Turn this off in Crossing → Way home.").format(
                name=self._label_of(peer)))

    def _on_escape_home(self, peer) -> None:
        if first_notice(self.config_path, "escape_notices"):
            self._on_alert(tr("Beamer"), tr("Came home from {name} on three presses of Esc. Turn this off in Crossing → Way home.").format(
                name=self._label_of(peer)))

    def _displays_changed(self) -> None:
        self._refresh_own_displays()
        self._reconcile_displays()

    def _reconcile_displays(self) -> None:
        if self._config is None:
            return
        file_signature = self._settings_file_signature()
        signature = (file_signature, self._own_display_map)
        if signature == self._displays_signature:
            return
        displays = self._own_displays()
        with app_config.SETTINGS_LOCK:
            settings = self._window_settings()
            if self._window_settings_stale:
                return
            outcome = crossing_state.displays_changed(
                settings, platform="pc", own_displays=displays,
                now=time.time(), capable=self._layout_capable())
            self._carry_crossing(outcome)
            self._displays_signature = (file_signature, copy.copy(self._own_display_map))



    def _rename_canvas_peer(self, peer: str, text: str) -> None:
        entry = next((item for item in self._peer_entries if item.get("id") == peer), None)
        if entry:
            self._rename_peer(entry.get("token"), text)




    @_crossing_refresh
    def _canvas_left_out(self, peer: str, left_out: bool) -> None:
        entry = next((item for item in self._peer_entries if item.get("id") == peer), None)
        if entry:
            self._set_peer_left_out(entry.get("token"), left_out)

    def _open_atlas_pairing(self) -> None:
        self._select_page("overview")
        if not self._pairing_open:
            self._toggle_pairing_sheet()
        self.sheet.address.setFocus()

    def _open_atlas_forget(self, peer: str) -> None:
        entry = next((item for item in self._peer_entries if item.get("id") == peer), None)
        if entry is not None:
            self._select_page("overview")
            self._refresh_peers()
            row = self.machines.rows.get(entry.get("token"))
            if row is not None:
                row._ask(True)

    @_crossing_refresh
    def _set_shortcut_goes_to(self, mode: str) -> None:
        if not self._config or mode not in ("last", "next", "ticked"):
            return
        self._config.shortcut_goes_to = mode
        self._persist()
        self._refresh_peers()
        self._reflect_ways()

    @_crossing_refresh
    def _set_shortcut_cycle(self, peer: str, enabled: bool) -> None:
        try:
            with app_config.SETTINGS_LOCK:
                settings = app_config.load_settings(self.config_path)
                entry = next((item for item in settings["peers"] if item.get("id") == peer), None)
                if entry is None:
                    return
                entry["shortcut_cycle"] = bool(enabled)
                app_config.write_settings(self.config_path, settings)
        except (ConfigError, OSError):
            LOGGER.exception("Shortcut cycle setting could not be saved")
            return
        self._read_crossing()
        self.sender.refresh()
        self._refresh_peers()
        self._reflect_ways()


    @_crossing_refresh
    def _write_ways(self, peer=None, **changes) -> bool:
        peer = peer or self._chosen_machine()
        if self._config is None or peer is None:
            return False
        if self._profile_read_only():
            return False
        with app_config.SETTINGS_LOCK:
            settings = app_config.load_settings(self.config_path)
            viewed = profiles.view_settings(settings, self._profile_machine()) if self._profile_remote() else settings
            entry = ways._entry(viewed, peer)
            if entry is None or entry.get("left_out") is True:
                return False
            if "methods" in changes:
                methods = changes.pop("methods")
                changes.update(edge=bool(set(methods) & {"edge", "part"}))
                # Corner only when its on/off changes, so an Edge write leaves several corners as they are.
                if ("corner" in methods) != bool(ways.ways(viewed, peer)["corners"]):
                    changes["corner"] = "corner" in methods
                if "edge" in methods:
                    changes["thirds"] = list(ways.PARTS)
            if "parts" in changes:
                changes["thirds"] = changes.pop("parts")
            if isinstance(changes.get("corner"), str):
                changes["corner_at"] = changes.pop("corner")
            changes.pop("side", None)
            try:
                if self._profile_remote():
                    outcome = crossing_state.edit_other(settings, self._profile_machine(), peer,
                                                        **changes, platform="pc", now=time.time())
                else:
                    outcome = crossing_state.edit_ways(settings, peer, **changes, **self._crossing_event_args())
                self._carry_crossing(outcome)
                if outcome.get("reason") == "desk_full":
                    self.profile_note.setText(tr("This desk is full. Remove an unused profile before adding more ways."))
                    self.profile_note.show()
                if not outcome.get("accepted", True):
                    return False
            except (ConfigError, OSError):
                LOGGER.exception("A machine's ways could not be saved")
                self._reflect_ways()
                return False
        return True

    def _ledger_edge(self, peer):
        held = ways.ways(self._crossing_view_settings(), peer)
        methods, parts = ways.toggle_edge(held["methods"], held["parts"])
        self._write_ways(peer=peer, methods=methods, parts=parts)

    def _ledger_third(self, peer, third):
        held = ways.ways(self._crossing_view_settings(), peer)
        if held["side"] not in ways.EDGES:
            return
        methods, parts = ways.toggle_third(held["methods"], held["parts"], third)
        self._write_ways(peer=peer, methods=methods, parts=parts)

    def _reach(self, settings: dict, peer: str) -> dict:
        options = {k: v for k, v in self._crossing_event_args().items() if k != "now"}
        if settings.get("machine_id") != self._profile_settings()["machine_id"]:
            options.update(platform=profiles.view_platform(self._profile_settings(), settings["machine_id"]),
                           capable=set(settings.get("layout", {})), live=self._profile_live, gone=lambda _peer: False)
        return ways.reach(settings, peer, **options)

    def _profile_live(self, peer):
        return peer == self._profile_settings()["machine_id"] or self._live(peer)

    def _ledger_side(self, peer, side):
        """A wrap-around chip: free adds the side (turning Edge on), on removes it, kept with Edge
        off turns Edge on and otherwise removes it. Held and unavailable sides change nothing."""
        settings = self._crossing_view_settings()
        if self._reach(settings, peer)["sides"][side]["state"] not in ("free", "on", "kept"):
            return
        held = ways.ways(settings, peer)
        edge_on = bool(ways.picked_thirds(held["methods"], held["parts"]))
        edges = list(held["edges"])
        if side not in edges:
            self._write_ways(peer=peer, edges=edges + [side], edge=True)
        elif not edge_on:
            self._write_ways(peer=peer, edge=True)
        else:
            self._write_ways(peer=peer, edges=[kept for kept in edges if kept != side])

    def _ledger_corner(self, peer, corner):
        settings = self._crossing_view_settings()
        corners = list(ways.ways(settings, peer)["corners"])
        # A corner already on can always be taken off; a new one needs a side contact and nobody else holding it.
        if corner not in corners and self._reach(settings, peer)["corners"][corner]["state"] != "free":
            return
        self._write_ways(peer=peer, corners=[kept for kept in corners if kept != corner] if corner in corners
                         else corners + [corner])

    def _ledger_jump(self, peer, value):
        if self._config is None or self._profile_remote():
            return
        try:
            app_config.set_jump_key(self.config_path, peer, value, self._config.trigger_key)
        except ConfigError as exc:
            self.atlas_canvas.announce(self._shown(str(exc)))
        self._read_crossing()
        self.sender.refresh()
        self._reflect_ways()

    @_crossing_refresh
    def _ways_changed(self, way: str, on: bool) -> None:
        if self._config is None:
            return
        if way == "shortcut":
            self._config.crossing_methods = pages_win.toggle_way(self._config.crossing_methods, way, on)
            self._persist()
            self.sender.update_config(self._config)
            self._reflect_ways()
            self._reflect_look()
            return
        held = ways.ways(self._crossing_settings(), self._chosen_machine())["methods"]
        methods = pages_win.toggle_way(held, way, on)
        if way == "edge" and not on:
            methods = [kind for kind in methods if kind != "part"]
        self._write_ways(methods=[kind for kind in methods if kind != "shortcut"])

    @staticmethod
    def _crossing_text(widget, text):
        if widget.text() != text:
            widget.setText(text)

    @_crossing_refresh
    def _third_changed(self, third: str) -> None:
        held = ways.ways(self._crossing_settings(), self._chosen_machine())
        methods, parts = ways.toggle_third(held["methods"], held["parts"], third)
        self._write_ways(methods=methods, parts=parts)

    @_crossing_refresh
    def _canvas_third(self, peer: str, third: str) -> None:
        self._ledger_third(peer, third)

    @_crossing_refresh
    def _reflect_ways(self) -> None:
        """Refresh the map and existing ledger cells without replacing the page."""
        config = self._config or default_config()
        full = self._profile_settings()
        self._reflect_profiles(full)
        settings = self._crossing_view_settings()
        remote = self._profile_remote()
        viewed_id = settings["machine_id"]
        chosen = self._chosen_machine()
        # Another machine's row lists its peers from its view; this machine's own, the cached entries.
        entries = peerlist.desktops(settings["peers"] if remote else self._peer_entries)
        labels = {config.machine_id: tr("This PC")}
        labels.update({entry["id"]: self._shown(peerlist.label_for(entries, peer_id=entry["id"])
                                               or f"Machine · {entry['id'][-6:]}")
                       for entry in entries})
        all_layout = ways.derive_layout(settings, gap=0)
        labels.update({peer: tile.get("name") or f"Machine · {peer[-6:]}"
                       for peer, tile in all_layout.items() if peer not in labels})
        if profiles.same_ways_on(full):
            labels.update({peer: self._profile_machine_name(full, peer) for peer in all_layout})
        left_out = {entry["id"] for entry in entries if entry.get("left_out")}
        dormant = {entry["id"] for entry in entries if peerlist.dormant_reason(entry) is not None or self._gone(entry["id"])}
        in_use = {entry["id"]: entry.get("in_use", True) for entry in entries}
        shelved = left_out | dormant | {peer for peer, enabled in in_use.items() if not enabled}
        layout = {peer: tile for peer, tile in all_layout.items() if peer not in shelved}
        known_caps = self._design_peer_caps()
        capable = {peer for peer, caps in known_caps.items() if "layout" in caps}
        active = {**settings, "peers": [entry for entry in entries if entry["id"] not in shelved]}
        if remote:
            capable = set(layout)
        result = ways.compile_layout(active, layout, capable=capable,
                                     platform=profiles.view_platform(full, viewed_id))
        tile_states = result.get("tiles", {})
        tile_states[viewed_id] = {"state": "this", "detail": self._profile_machine_name(full, viewed_id)}
        legacy = {}
        ledger_reach = {entry["id"]: self._reach(settings, entry["id"]) for entry in entries}
        for entry in entries:
            peer = entry["id"]
            state = tile_states.setdefault(peer, {})
            held = ways.ways(settings, peer)
            picked = ways.picked_thirds(held["methods"], held["parts"])
            touched = state.get("thirds")
            own, tile = layout.get(viewed_id), layout.get(peer)
            contact = ways._rect_contact(own, tile) if own and tile else None
            edge_on = bool(set(held["methods"]) & {"edge", "part"})
            # Pads for the corners that cross now, from the compile, never the saved intent alone.
            corners = list(state.get("corners", [])) if peer not in shelved else []
            reached = ledger_reach[peer]["sides"]
            state.update(methods=held["methods"], side=held["side"], corner=held["corner"], corners=corners,
                         kept_wraps=[side for side in ways.EDGES if reached[side]["state"] == "kept"],
                         thirds=picked, lit=[part for part in picked if touched is None or part in touched],
                         crosses=bool((edge_on and state.get("state") in ("edge", "part")) or state.get("wraps") or corners),
                         edge_off=not edge_on and not corners and bool(contact and contact[0] != "corner"),
                         linked=self._profile_live(peer))
            if state["edge_off"]:
                state["state"] = "gap"
            platform_name = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}.get(
                entry.get("platform"), "Computer")
            state["detail"] = f"{platform_name} · {'Linked' if state['linked'] else 'Offline'}"
            version = entry.get("app", "")
            if peer in known_caps and peer not in capable and version:
                version = version if version.startswith("Beamer ") else f"Beamer {version}"
                legacy[peer] = tr("{version} · only sees which side").format(version=version)
                state.update(legacy=True, legacy_label=legacy[peer])
        on = profiles.same_ways_on(full)
        for peer in layout:
            tile_states.setdefault(peer, {})["profile_word"] = profiles.tile_word(
                full, peer, linked=self._profile_live(peer), caps=self._profile_caps(peer, full))
        self.atlas_canvas.profile_mode = on
        self.atlas_canvas.remote = remote
        self.atlas_canvas.read_only = self._profile_read_only()
        canvas_chosen = self.atlas_canvas.selected if on and self.atlas_canvas.selected in layout else chosen
        self.atlas_canvas.set_tiles(layout, labels, tile_states, canvas_chosen, config.shortcut_goes_to,
                                    {entry["id"]: entry.get("shortcut_cycle", True) for entry in entries},
                                    capable, own_id=viewed_id,
                                    rename_values={entry["id"]: self._shown(entry.get("label") or entry.get("name") or "")
                                                   for entry in entries})
        self.atlas_canvas.set_left_out(left_out, in_use, dormant)
        key = TRIGGER_KEYS.get(config.trigger_key, config.trigger_key)
        self.crossing_ledger.reflect(settings, all_layout, labels, chosen, self._profile_live, key, legacy, ledger_reach)
        self.dragging_switch.setEnabled(bool(self._ways_in() & {"edge", "part", "corner"}))
        self.pointer_module.setVisible(not remote and not platform_parts.WAYLAND)
        self.resistance_module.setVisible(not remote)
        self.shortcut_module.setVisible(not remote)
        self.shortcut_tile.blockSignals(True)
        try:
            self.shortcut_tile.setChecked("shortcut" in config.crossing_methods)
        finally:
            self.shortcut_tile.blockSignals(False)
        self.shortcut_tile.set_detail(ways.shortcut_detail(key, config.shortcut_goes_to))
        self.shortcut_target_choice.set_value(config.shortcut_goes_to)
        self._crossing_text(self.shortcut_target_note, pages_win.shortcut_destination_note(
            config.shortcut_goes_to, config.trigger_style == "hold"))
        held = ways.ways(settings, chosen)
        self.resistance_strip.set_edge(held["side"] or "right")
        self._update_resistance_hint(config.crossing_resistance_px)

    def _jump_arming(self, armed: bool) -> None:
        self.sender.jump_recording = armed

    def _record_jump_key(self, value: str) -> None:
        self._ledger_jump(self._chosen_machine(), value)

    def _set_hold_full_screen(self, enabled: bool) -> None:
        if self._config is None:
            return
        self._config.hold_full_screen = bool(enabled)
        self._persist()
        self.sender.update_config(self._config)

    def _set_hide_idle_pointer(self, enabled: bool) -> None:
        if self._config is None:
            return
        self._config.hide_idle_pointer = bool(enabled)
        self._persist()
        if not enabled and self._idle_pointer is not None:
            self._idle_pointer.cancel()
        self._set_driving_pointer(bool(enabled) and self.sender.redirecting)

    def _set_home_shortcut(self, enabled: bool) -> None:
        """Wayland only: binding the shortcut asks the desktop, so it starts only when chosen."""
        shortcut = getattr(self, "global_shortcut", None)
        if self._config is None or shortcut is None:
            return
        self._config.home_shortcut = bool(enabled)
        self._persist()
        if enabled:
            shortcut.start()
        else:
            shortcut.stop()

    def _hiding_pointer(self) -> bool:
        return self._idle_pointer is not None and (self._config is None or self._config.hide_idle_pointer)

    def _set_block_while_dragging(self, enabled: bool) -> None:
        if self._config is None:
            return
        self._config.block_while_dragging = bool(enabled)
        self._persist()
        self.sender.update_config(self._config)


    def _tell(self, peer: str) -> None:
        """This PC's side for `peer` as held, stamped as held, with whether a way leads there (WIRE.md
        section 8), over whichever link is up. Nothing while no side is set."""
        settings = self._crossing_settings()
        entry = next((item for item in settings["peers"] if item.get("id") == peer), None)
        if entry is None:
            return
        edge = entry.get("side")
        if edge not in return_edge.EDGES:
            if peer not in self._layout_capable():
                return
            # Picture peers ignore the wire edge; a gap must still report its return-way status.
            edge = "right"
        author = protocol.read_id(entry.get("side_by")) or protocol.read_id(self._config.machine_id)
        target = protocol.read_id(peer)
        if author is None or target is None:
            return
        self._send_to(target, protocol.arrangement_v6(
            edge, int(entry.get("side_set_at", 0)), author, way_back=ways.has_way(settings, peer),
            way_back_by=protocol.read_id(ways.way_back_by(settings, peer)),
        ))

    def _on_arrangement(self, peer: str, edge: str, set_at: int, by: str, way_back=None, way_back_by=None) -> None:
        if self._config is None:
            return
        with app_config.SETTINGS_LOCK:
            outcome = crossing_state.receive_arrangement(
                app_config.load_settings(self.config_path), peer,
                {"edge": edge, "set_at": set_at, "by": protocol.read_id(by),
                 "way_back": way_back, "way_back_by": protocol.read_id(way_back_by)},
                **self._crossing_event_args())
            self._carry_crossing(outcome)

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
        module = widgets.Module(tr("Resistance"))
        self.resistance_readout = widgets.Figure(f"{current.crossing_resistance_px} px")
        module.body.addWidget(self.resistance_readout)
        self.resistance_strip = PushStrip()
        self.resistance_strip.set_edge("right")
        self.resistance_strip.set_value(current.crossing_resistance_px)
        self.resistance_slider = self.resistance_strip
        self.resistance_strip.valueChanged.connect(self._resistance_changed)
        module.body.addWidget(self.resistance_strip)
        self.resistance_hint = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.resistance_hint)
        self._update_resistance_hint(current.crossing_resistance_px)
        return module

    def _resistance_changed(self, value: int) -> None:
        self.resistance_readout.setText(tr("{value} px").format(value=value))
        self._update_resistance_hint(value)
        if self._config is None:
            return
        self._config.crossing_resistance_px = int(value)
        self.sender.update_config(self._config)
        configure_seams = getattr(self, "_configure_capture_seams", None)
        if callable(configure_seams):
            configure_seams(self._config)
        self._debounce_save()

    def _update_resistance_hint(self, value: int) -> None:
        side = ways.ways(self._crossing_settings(), self._chosen_machine())["side"]
        if value == 0:
            text = tr("Switches the moment this PC's pointer touches the edge. Each screen supplies its own zones.")
        elif side:
            # Translators: {side} is the direction of this screen's edge.
            text = tr("Follows this PC's mouse or trackpad on every screen; each screen supplies its own zones. This screen's {side} edge lights to show the resistance.").format(side=tr(ways.EDGE_NAMES[side]))
        else:
            text = tr("Follows this PC's mouse or trackpad on every screen; each screen supplies its own zones.")
        if text != self.resistance_hint.text():
            self.resistance_hint.setText(text)

    def _on_local_pressure(self, edge: str, pressure: float, crossed: bool, _part=None) -> None:
        if self._closing or not hasattr(self, "resistance_strip"):
            return
        resistance = self.resistance_strip.value()
        if crossed:
            self.resistance_pressure_timer.stop()
            self.resistance_strip.set_pressure(resistance, edge, crossed=True)
            return
        self.resistance_strip.set_pressure(pressure * resistance, edge)
        if pressure > 0:
            self.resistance_pressure_timer.start()
        else:
            self.resistance_pressure_timer.stop()

    def _relay_sender_pressure(self, edge: str, pressure: float, crossed: bool, part=None) -> None:
        self.bridge.pressure.emit(edge, pressure, crossed, part)
        self.bridge.local_pressure.emit(edge, pressure, crossed, part)

    def _refresh_local_pressure(self) -> None:
        pressure = self.sender.pressure_at(time.monotonic())
        resistance = self.resistance_strip.value()
        self.resistance_strip.set_pressure(pressure * resistance)
        if pressure <= 0:
            self.resistance_pressure_timer.stop()

    # -- Keyboard -----------------------------------------------------------------------------

    def _keyboard_page(self, layout, current: Config) -> None:
        module = widgets.Module(tr("Keyboard sharing"))
        self.mouse_only_switch = widgets.Switch(tr("Mouse only"))
        self.mouse_only_switch.setChecked(current.mouse_only)
        self.mouse_only_switch.toggled.connect(self._mouse_only_changed)
        module.body.addWidget(self.mouse_only_switch)
        module.body.addWidget(widgets.label(
            tr("Keep this machine's keyboard here. Pointer and clipboard sharing stay on."), "note", wrap=True
        ))
        # InputCapture owns the keyboard too, so retained keys cannot reach local Wayland apps.
        module.setVisible(not platform_parts.WAYLAND)
        layout.addWidget(module)
        layout.addWidget(self._modifier_module(current))
        layout.addWidget(self._ignored_module(current))
        layout.addWidget(self._speed_module(current))

    def _mouse_only_changed(self, enabled: bool) -> None:
        if self._config is None:
            return
        previous = self._config.mouse_only
        self._config.mouse_only = bool(enabled)
        if not self._persist():
            self._config.mouse_only = previous
            self.mouse_only_switch.blockSignals(True)
            self.mouse_only_switch.setChecked(previous)
            self.mouse_only_switch.blockSignals(False)
            return
        self.sender.update_config(self._config)

    def _speed_module(self, current: Config) -> QWidget:
        """How a driving machine's pointer feels on this PC: it sends what its own acceleration made of
        the hand's movement, and this PC's settings decide the rest."""
        module = widgets.Module(tr("Another machine's pointer here"))
        self.speed_sliders = {}
        self.speed_readouts = {}
        for key, name, value in (("pointer_speed", N_("Pointer speed"), current.pointer_speed),
                                 ("scroll_speed", N_("Scroll speed"), current.scroll_speed)):
            module.body.addWidget(widgets.label(tr(name), "body"))
            row = QHBoxLayout()
            row.setSpacing(10)
            slider = widgets.Ruler(tr(name), 400, minimum=25)
            slider.setValue(round(value * 100))
            slider.valueChanged.connect(lambda percent, key=key: self._speed_changed(key, percent))
            row.addWidget(slider, 1)
            readout = widgets.label(f"{round(value * 100)}%", "readout")
            row.addWidget(readout)
            module.body.addLayout(row)
            self.speed_sliders[key], self.speed_readouts[key] = slider, readout
        module.body.addWidget(widgets.label(
            tr("For the trackpad or mouse of whichever machine drives this PC."), "note", wrap=True
        ))
        self.reverse_scroll_switch = widgets.Switch(tr("Reverse scrolling from other machines"))
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
        module = widgets.Module(tr("Modifier keys"))
        self.modifier_choice = widgets.Choice(
            tuple((value, tr(title)) for value, title in MODIFIER_STYLE_CHOICES),
            columns=3, current=current.modifier_style, on_change=self._set_modifier_style
        )
        self.modifier_choice.set_names(tr("Modifier keys"))
        module.body.addWidget(self.modifier_choice.view)
        self.modifier_note = widgets.label(tr(MODIFIER_NOTES[current.modifier_style]), "note", wrap=True)
        module.body.addWidget(self.modifier_note)
        return module

    def _set_modifier_style(self, value: str) -> None:
        self.modifier_note.setText(tr(MODIFIER_NOTES[value]))
        if self._config is None:
            return
        self._config.modifier_style = value
        self._persist()
        self.sender.update_config(self._config)

    def _ignored_module(self, current: Config) -> QWidget:
        module = widgets.Module(tr("Stays on this PC"))
        self.ignored_note = widgets.label("", "note", wrap=True)
        module.body.addWidget(self.ignored_note)
        self.ignored_list = QVBoxLayout()
        self.ignored_list.setSpacing(4)
        module.body.addLayout(self.ignored_list)
        self.ignored_recorder = widgets.InputRecorder(
            tr("Add a key or button"), self._record_ignored, capture_win.hook_vk,
            mono=False, hint=tr("Click, then press it"), recording_hint=tr("Click again to cancel"),
        )
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
            row_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
            row_layout.setContentsMargins(12, 2, 4, 2)
            name = widgets.label(capture_win.input_title(entry), "readout")
            row_layout.addWidget(name, 1)
            remove = QPushButton(tr("Remove"))
            remove.setProperty("vernier", "remove")
            remove.setMinimumHeight(widgets.MIN_TARGET)
            remove.setCursor(Qt.CursorShape.PointingHandCursor)
            remove.setAccessibleName(tr("Remove {key}").format(key=capture_win.input_title(entry)))
            remove.clicked.connect(lambda _checked=False, e=entry: self._remove_ignored(e))
            row_layout.addWidget(remove)
            self.ignored_list.addWidget(row)
        text = refused or tr(IGNORED_NOTES["filled" if entries else "empty"])
        self.ignored_note.setText(text)
        widgets.set_role(self.ignored_note, "note-amber" if refused else "note")

    def _record_ignored(self, kind: str, value) -> None:
        if self._config is None:
            return
        entries = list(self._config.ignored_inputs)
        if kind == "key" and value == TRIGGER_VKS.get(self._config.trigger_key):
            self._show_ignored(entries, tr("That key is the shortcut; it always stays with Beamer."))
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
            self._show_ignored(list(previous), tr("That could not be saved, so the list is unchanged."))
            return
        self.sender.update_config(self._config)
        self._show_ignored(entries)

    def _shortcut_module(self, current: Config) -> QWidget:
        module = widgets.Module(tr("Shortcut"))
        key = TRIGGER_KEYS.get(current.trigger_key, current.trigger_key)
        self.shortcut_tile = widgets.WayTile(tr("Shortcut"), ways.shortcut_detail(key, current.shortcut_goes_to))
        self.shortcut_tile.setChecked("shortcut" in current.crossing_methods)
        self.shortcut_tile.toggled.connect(lambda on: self._ways_changed("shortcut", on))
        module.body.addWidget(self.shortcut_tile)
        self.wayland_shortcut_note = widgets.label(
            tr("On Wayland the shortcut only brings input back: the desktop lets input go to another machine only "
            "when the pointer pushes through an edge, and keys on the Stays on this PC list cannot stay here "
            "while input is away."), "note", wrap=True)
        self.wayland_shortcut_note.setVisible(platform_parts.WAYLAND)
        module.body.addWidget(self.wayland_shortcut_note)
        self.home_shortcut_switch = widgets.Switch(tr("Send input out with Super+Shift+B"))
        self.home_shortcut_switch.setChecked(current.home_shortcut)
        self.home_shortcut_switch.toggled.connect(self._set_home_shortcut)
        self.home_shortcut_note = widgets.label(
            tr("From here, where the double-tap cannot be seen, the key combination pushes the pointer through the "
               "edge to the machine the shortcut goes to. The desktop asks once to allow it, and its keyboard "
               "settings can change the keys."), "note", wrap=True)
        for widget in (self.home_shortcut_switch, self.home_shortcut_note):
            widget.setVisible(platform_parts.WAYLAND)
            module.body.addWidget(widget)
        self.shortcut_target_choice = widgets.Choice(
            (("last", tr("Last machine")), ("next", tr("Next in order")), ("ticked", tr("Next among ticked"))),
            3, current.shortcut_goes_to, on_change=self._set_shortcut_goes_to,
        )
        self.shortcut_target_choice.set_names(tr("Shortcut goes to"))
        self.shortcut_target_row = self._row(widgets.label(tr("Shortcut goes to"), "key"), self.shortcut_target_choice.view)
        self.shortcut_target_note = widgets.label("", "note", wrap=True)
        note_policy = self.shortcut_target_note.sizePolicy()
        note_policy.setVerticalPolicy(QSizePolicy.Policy.Minimum)
        self.shortcut_target_note.setSizePolicy(note_policy)
        self.trigger_recorder = widgets.InputRecorder(
            TRIGGER_KEYS.get(current.trigger_key, current.trigger_key),
            self._record_trigger,
            capture_win.hook_vk,
            keys_only=True,
            name=tr("Shortcut key"),
            hint=tr("Click, then press the key"), recording_hint=tr("Click again to cancel"),
        )
        self.trigger_recorder.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.trigger_recorder.key.setFont(theme.mono_font(theme.TYPE["keycap"]))
        self.trigger_recorder.hint.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.trigger_recorder._content.setSpacing(6)
        module.body.addWidget(self.trigger_recorder)
        self.trigger_note = widgets.label("", "note-amber", wrap=True)
        self.trigger_note.setVisible(False)
        module.body.addWidget(self.trigger_note)
        self.trigger_style_choice = widgets.Choice(
            tuple((value, tr(title)) for value, title in TRIGGER_STYLE_CHOICES),
            columns=2, current=current.trigger_style, on_change=self._set_trigger_style
        )
        self.trigger_style_choice.set_names(tr("How you press it"))
        self.trigger_style_row = self._row(widgets.label(tr("How you press it"), "key"), self.trigger_style_choice.view)
        module.body.addWidget(self.trigger_style_row)
        module.body.addWidget(self.shortcut_target_row)
        module.body.addWidget(self.shortcut_target_note)
        self.style_hint = widgets.label("", "note", wrap=True)
        self.double_tap_readout = widgets.Figure(f"{current.double_tap_ms} ms")
        head = widgets.FormRow(widgets.label(tr("Time between taps"), "key"), self.double_tap_readout)
        # The Mac's range: the store takes 50 to 2000 ms, but only about 150 to 600 is useful.
        self.double_tap_slider = widgets.Ruler(tr("Time between taps"), 1000, minimum=50,
                                              ticks=(50, 150, 300, 600, 1000), scale="sqrt")
        self.double_tap_slider.setValue(current.double_tap_ms)
        self.double_tap_slider.valueChanged.connect(self._double_tap_changed)
        self.double_tap_row = self._row(head, self.double_tap_slider)
        module.body.addWidget(self.double_tap_row)
        module.body.addWidget(self.style_hint)
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
                tr("That key cannot be the shortcut. Choose a modifier such as Right Ctrl, a function "
                "key, or a key like Insert or Scroll Lock.")
            )
            self.trigger_note.setVisible(True)
            return
        if self._config is None:
            return
        try:
            ways.check_trigger_key({"peers": self.book.peers()}, name)
        except ValueError as exc:
            self._show_trigger_conflict(str(exc))
            return
        self._show_trigger_conflict("")
        self.trigger_recorder.set_title(TRIGGER_KEYS[name])
        self._config.trigger_key = name
        self._persist()
        self._configure_trigger(self._config)
        self._reflect_ways()
        if ignored.key(value) in self._config.ignored_inputs:
            # The shortcut always stays with Beamer, so it cannot also be on the stays-here list.
            self._set_ignored([entry for entry in self._config.ignored_inputs if entry != ignored.key(value)])

    def _show_trigger_conflict(self, note) -> None:
        self._trigger_conflict = note
        self.trigger_note.setText(note)
        self.trigger_note.setVisible(bool(note))

    @_crossing_refresh
    def _set_trigger_style(self, value: str) -> None:
        if self._config is None:
            return
        self._config.trigger_style = value
        self._persist()
        self._configure_trigger(self._config)
        motion.set_shown(self.double_tap_row, value != "hold", fade=False)
        self._update_style_hint(value)
        self._reflect_ways()

    def _update_style_hint(self, style: str) -> None:
        text = tr(
            "Input is on another machine for as long as the key is held. If you jump while holding the shortcut, releasing it brings input home."
            if style == "hold"
            else "Tap twice to switch; tap twice again to come back."
        )
        if text != self.style_hint.text():
            self.style_hint.setText(text)

    def _double_tap_changed(self, value: int) -> None:
        self.double_tap_readout.setText(tr("{value} ms").format(value=value))
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
        layout.addWidget(self.colour_module)
        layout.addWidget(self._appearance_module(current))
        follow = widgets.Module(tr("Match design with"))
        self.design_follow_choice = QComboBox()
        self.design_follow_choice.setMaximumWidth(320)
        self.design_follow_choice.currentIndexChanged.connect(self._design_follow_chosen)
        follow.body.addWidget(self._row(widgets.label(tr("Machine"), "key"), self.design_follow_choice))
        self.design_follow_note = widgets.label(tr("None: this PC keeps its own Design."), "note", wrap=True)
        follow.body.addWidget(self.design_follow_note)
        layout.addWidget(follow)
        self._refresh_design_follow()
        self._reflect_look()

    def _appearance_module(self, current: Config) -> QWidget:
        """The settings window's own palette. Last on the page, and never hidden with the
        crossing animations above it: it is chosen once, not part of what they show."""
        module = widgets.Module(tr("Appearance"))
        module.body.addWidget(self._own_note())
        self.appearance_choice = widgets.Choice(
            (("system", tr("System")), ("light", trc("appearance", "Light")), ("dark", tr("Dark"))), 3, current.appearance,
            on_change=self._appearance_chosen,
        )
        self.appearance_choice.set_width(252)
        module.body.addWidget(self._row(widgets.label(tr("This window"), "key"), self.appearance_choice.view))
        if sys.platform.startswith("linux"):
            appearance_note = tr("System follows your Linux desktop's light or dark setting.")
        else:
            appearance_note = tr("System follows Windows' light or dark setting.")
        module.body.addWidget(widgets.label(appearance_note, "note", wrap=True))
        return module

    def _appearance_chosen(self, choice: str) -> None:
        # Applies the moment it is chosen, not after _debounce_save's pause: the choice is the change.
        self._apply_appearance(choice)
        if self._config is None:
            return
        self._config.appearance = choice
        self._persist()

    def _on_screen_module(self, current: Config) -> QWidget:
        module = widgets.Module(tr("On screen"))
        self.glow_toggle = widgets.Switch(tr("Animate crossings on this PC"))
        self.glow_toggle.setFont(theme.font(theme.TYPE["body"]))
        self.glow_toggle.setChecked(current.edge_glow)
        self.glow_toggle.toggled.connect(self._apply_look)
        module.body.addWidget(self.glow_toggle)
        module.body.addWidget(self._own_note())
        module.body.addWidget(
            widgets.label(
                tr("Turns crossing and landing animations on or off on this screen. Saved style choices stay in place."),
                "note",
                wrap=True,
            )
        )
        self.landing_toggle = widgets.Switch(tr("Show where the pointer lands"))
        self.landing_toggle.setFont(theme.font(theme.TYPE["body"]))
        self.landing_toggle.setChecked(current.shortcut_arrival)
        self.landing_toggle.toggled.connect(self._apply_look)
        # The landing plays through the same animations, so it goes with them when they are off.
        self.landing_row = self._row(
            self.landing_toggle,
            widgets.label(
                tr("For shortcut, menu and jump arrivals; requires Animate crossings on this screen."),
                "note",
                wrap=True,
            ),
        )
        self.landing_row.layout().setSpacing(12)
        module.body.addWidget(self.landing_row)
        self.landing_toggle.setToolTip(tr("Jump arrivals use this screen's landing animation too."))
        return module

    def _edge_look_module(self, current: Config) -> QWidget:
        module = widgets.Module(tr("Style"))
        module.body.addWidget(self._follow_banner())
        colours = app_config.palette_colours(current.glow_colour)
        self.style_preview_hint = widgets.label(tr("Hover to preview"), "preview-hint")
        module.body.addWidget(self.style_preview_hint)
        # Which animation the tiles below choose. Not saved: it only says which one is being worked on.
        self.design_mode_choice = widgets.Choice(
            (("crossing", tr("Crossing")), ("switch", tr("Shortcut and menu"))), 2, "crossing", on_change=self._preview_method
        )
        self.design_mode_choice.set_names(tr("Style for"))
        self.design_mode_choice.set_width(240)
        self.style_for_row = self._row(widgets.label(tr("Style for"), "key"), self.design_mode_choice.view)
        # Where every tile below plays its style: never switched off by the style, and what differs
        # there is said beneath it. The edge or a corner; a PC has no notch to show.
        self.effect_method_choice = widgets.Choice(
            (("edge", tr("Edge")), ("corner", tr("Corner"))), 2, "edge", on_change=self._place_picked
        )
        self.effect_method_choice.set_names(tr("Show at"))
        self.effect_method_choice.set_width(176)
        self._place_chosen = False
        self.place_note = widgets.label("", "small", wrap=True)
        self.preview_at_row = self._row(widgets.label(tr("Show at"), "key"), self.effect_method_choice.view, self.place_note)
        module.body.addWidget(self.style_for_row)
        module.body.addWidget(self.preview_at_row)
        self.effect_stills = {}
        groups = []
        for title, items in pages_win.style_groups():
            # Glow and Beam as stills of the same scene as the effects, so every tile reads alike.
            tiles = [(style, tr(name), tr(detail), self.effect_stills.setdefault(style, EffectStill(style, colours)))
                     for style, name, detail in items]
            groups.append((tr(title), tiles))
        self.glow_style_choice = widgets.TileGroups(groups, current.glow_style, on_change=self._apply_look)
        module.body.addWidget(self.glow_style_choice.view)
        self.switch_stills = {}
        switch_groups = []
        for title, items in pages_win.switch_groups():
            tiles = []
            for choice, name, detail in items:
                picture = self.switch_stills[choice] = SwitchStill(choice, current.glow_style, colours)
                tiles.append((choice, tr(name), tr(detail), picture))
            switch_groups.append((tr(title), tiles))
        self.switch_style_choice = widgets.TileGroups(switch_groups, current.shortcut_arrival_style, on_change=self._apply_look)
        self.switch_style_choice.view.setToolTip(tr("For shortcut, menu and jump arrivals. Same as crossing uses this screen's crossing style."))
        module.body.addWidget(self.switch_style_choice.view)
        module.body.addSpacing(6)
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
        self.length_choice = _DesignChoiceSlider(
            "length", 25, 250, 5, 100, (0.5, 1.0, 2.0), effects.LENGTHS)
        self.size_choice = _DesignChoiceSlider(
            "size", 20, 250, 1, 1000, (0.03, 0.045, 0.06), effects.SIZES)
        self.frame_rate_choice = widgets.Choice(
            tuple((value, tr(name)) for value, name, _divisor in effects.FRAME_RATES),
            3, current.frame_rate, on_change=self._apply_look)
        self.frame_rate_choice.set_names(tr("Frame rate"))
        self.frame_rate_choice.set_width(320)
        self.length_readout = design_readout()
        self.size_readout = design_readout()
        for title, control, readout, description in (
                (N_("Length"), self.length_choice, self.length_readout, N_("Sets the effect's playback time.")),
                (N_("Size"), self.size_choice, self.size_readout,
                 N_("Sets depth relative to the shorter screen edge."))):
            title, description = tr(title), tr(description)
            row = self._row(widgets.label(title, "key"), control.view, readout)
            module.body.addWidget(row)
            module.body.addWidget(widgets.label(description, "small", wrap=True))
        module.body.addWidget(self._row(widgets.label(tr("Frame rate"), "key"), self.frame_rate_choice.view))
        self.length_choice.slider.valueChanged.connect(self._apply_look)
        self.size_choice.slider.valueChanged.connect(self._apply_look)
        self.length_choice.slider.sliderReleased.connect(self._apply_look)
        self.size_choice.slider.sliderReleased.connect(self._apply_look)
        self.colour_module = widgets.Module(tr("Colour"))
        self.colour_note = widgets.label(tr("For the crossing and the shortcut alike"), "small", wrap=True)
        self.colour_module.body.addWidget(self.colour_note)
        self.glow_colour_choice = widgets.SwatchGroups(
            [(tr(title), [(value, tr(name), app_config.palette_colours(value)) for value, name in items])
             for title, items in pages_win.colour_groups()],
            current.glow_colour,
            on_change=self._apply_look,
        )
        self.colour_module.body.addWidget(self.glow_colour_choice.view)
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
        if self.length_choice.slider.isSliderDown() or self.size_choice.slider.isSliderDown():
            return
        self._config.edge_glow = self.glow_toggle.isChecked()
        self._config.shortcut_arrival = self.landing_toggle.isChecked()
        self._config.shortcut_arrival_style = self.switch_style_choice.value or "match"
        self._config.glow_style = self.glow_style_choice.value
        self._config.glow_colour = self.glow_colour_choice.value
        self._config.effect_length = self.length_choice.value or "normal"
        self._config.effect_size = self.size_choice.value or "medium"
        self._config.effect_pace = self.length_choice.numeric_value()
        self._config.effect_size_fraction = self.size_choice.numeric_value()
        self._config.frame_rate = self.frame_rate_choice.value or "display"
        set_divisor(effects.frame_divisor(self._config.frame_rate))
        self._persist()
        self._hide_crossing()

    def _reflect_look(self) -> None:
        on = self.glow_toggle.isChecked()
        landing = self.landing_toggle.isChecked()
        # With the animations off, what they look like cannot apply, so it is hidden, not faded.
        motion.set_shown(self.landing_row, on)
        motion.set_shown(self.look_module, on)
        motion.set_shown(self.colour_module, on)
        # Style for exists only while a switch plays anything; otherwise the module is Crossing.
        motion.set_shown(self.style_for_row, landing)
        self.colour_note.setVisible(landing)
        if not landing:
            self.design_mode_choice.set_value("crossing")
        switching = landing and self.design_mode_choice.value == "switch"
        style = self.glow_style_choice.value
        crossing_fx = (effects.preview_effect(style) if pages_win.effects_load_error() is None
                       else effects.CLASSIC.get(style))
        if crossing_fx is not None:
            self.switch_style_choice.set_detail("match", tr("Plays {name}").format(name=tr(crossing_fx.name)))
        edge = ways.ways(self._crossing_settings(), self._chosen_machine())["side"] or "right"
        colour = self.glow_colour_choice.value or "signal"
        methods = sorted(self._ways_in())
        if not self._place_chosen:
            # Until a place is picked here, the preview opens where the pointer actually crosses.
            self.effect_method_choice.set_value(pages_win.preview_place(methods))
        method = self.effect_method_choice.value or "edge"
        switch_style = (self.switch_style_choice.value or "match") if switching else None
        length = self.length_choice.numeric_value()
        size = self.size_choice.numeric_value()
        look = (style, colour, switching, switch_style, method, edge, length, size)
        previous, self._look = self._look, look
        # Each still redrawn by this change cross-fades: every one with the colour or the size, the
        # crossing's with the place, the Shortcut and menu stills with the crossing style beside them.
        # The length redraws none: a still is the same frame of its effect at every length.
        stills = []
        if previous is not None and (previous[1], previous[7]) != (colour, size):
            stills = [*self.effect_stills.values(), *self.switch_stills.values()]
        elif previous is not None and previous[7] != size:
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
            self.effect_name.setText(tr("Same as crossing: {name}").format(name=tr(fx.name)) if switch_style == "match" else tr(fx.name))
        else:
            fx = (effects.preview_effect(style) if pages_win.effects_load_error() is None
                  else effects.CLASSIC.get(style)) or effects.CLASSIC["glow"]
            self.effect_name.setText(tr("{name}, {intensity}").format(name=tr(fx.name), intensity=tr(fx.intensity)))
        wayland_effects_unavailable = platform_parts.WAYLAND and (
            not os.environ.get("DISPLAY") or self._effects_failed
        )
        self.effect_note.setText(
            tr("On Wayland the desktop does not let Beamer draw at the edges of the screen, so crossings show no "
            "effect on this machine. The other machines still show theirs.")
            if wayland_effects_unavailable
            else tr("The effects stopped working and are off until Beamer restarts, so crossings show Glow in "
            "the same colours. The log says why.")
            if self._effects_failed
            else tr("{blurb} It plays around the pointer when the shortcut or a menu brings input to this PC.").format(blurb=tr(fx.blurb))
            if switching
            else tr(fx.blurb)
        )
        colours = app_config.palette_colours(colour)
        for still in (*self.effect_stills.values(), *self.switch_stills.values()):
            still.set_palette(colours)
        for still in self.switch_stills.values():
            still.set_crossing_style(style)
        pace = length
        screen = QApplication.primaryScreen()
        geometry = screen.availableGeometry() if screen is not None else None
        width, height = (geometry.width(), geometry.height()) if geometry is not None else (1920, 1080)
        readout_style = fx.id if switching else style
        length_text, size_text = effects.design_slider_readouts(readout_style, pace, size, width, height)
        self.length_readout.setText(length_text)
        self.size_readout.setText(size_text)
        self.length_choice.set_accessible_value(length_text)
        self.size_choice.set_accessible_value(size_text)
        for still in (*self.effect_stills.values(), *self.switch_stills.values()):
            still.set_pace(pace)
            still.set_effect_size(self.size_choice.value, size)
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
        seen = self._same_seen
        shared = (seen is not None and self._config.same_on_both
                  and settings_sync.changed(seen, fields, self._config.same_groups))
        self._same_seen = fields
        design_fields = self._design_fields()
        design_changed = self._design_seen is not None and design_fields != self._design_seen
        self._design_seen = design_fields
        if design_changed:
            self._config.design_set_at = settings_sync.next_stamp(
                max(self._config.design_set_at, self._config.same_set_at), time.time())
            self._config.design_by = self._config.machine_id
        # Each shared value changed here gets its own stamp, with Same on or off, so the latest
        # choice of each wins when it is turned on later; the values left alone keep theirs.
        stamp = None
        if seen is not None:
            config = self._config
            held = settings_sync.held_stamps(config.same_stamps, config.same_set_at, config.same_by)
            held, stamp = settings_sync.stamp_changes(held, seen, fields, config.machine_id,
                                                      floor=max(config.same_set_at, config.design_set_at))
            if stamp is not None or shared:
                config.same_stamps = settings_sync.stored_stamps(held)
        if shared:
            self._config.same_set_at = max(stamp or 0, settings_sync.next_stamp(
                max(self._config.same_set_at, self._config.design_set_at), time.time()))
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
            self._configure_shake()
            if shared or design_changed:
                self._send_same()
            self._refresh_design_follow()
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
        name = machine_name(settings.get("name"))
        service = PairingService(
            name, identity, OWN_PLATFORM, self._announced_port, self.book.peers, self._store_peer,
            on_paired=self.bridge.paired.emit, logger=LOGGER,
            **({"bind_port": 0, "tcp_port": 0} if portable_runtime.enabled() else {}),
        )
        service.active_discovery = portable_runtime.enabled()
        return service

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
            if replaced is not None and (held.get("id") == entry.get("id") or (
                    not held.get("id") and held.get("from_1_4")
                    and held.get("host") == entry.get("host")
                    and held.get("platform") == entry.get("platform"))):
                for field in ("ways", "shortcut_cycle", "jump_key"):
                    if field in held:
                        entry[field] = copy.deepcopy(held[field])
            peerlist.add_peer(settings, entry, replaced)
            settings["layout"] = ways.derive_layout(settings, gap=0)
            outcome = crossing_state.paired(settings, platform="pc", own_displays=self._own_displays(),
                                             now=time.time(), capable=self._layout_capable() | {entry["id"]})
            outcome["changed"] = True
            self._carry_crossing(outcome, redraw=False)

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
            where = peers_view.platform_name(entry)
            if entry.get("port") != 0:
                where += f" · {peers_view.address_line(entry, hide)}"
            items.append((entry, labels[token], state, where))
        return items

    def _window_settings(self, *, force: bool = False) -> dict:
        """GUI-only snapshot; link read/modify/write paths continue to read under their lock."""
        with app_config.SETTINGS_LOCK:
            version = []
            for path in (self.config_path, app_config.legacy_config_path(self.config_path)):
                try:
                    stat = path.stat()
                    version.append((stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino))
                except FileNotFoundError:
                    version.append(None)
            version = tuple(version)
            if force or version != getattr(self, "_window_settings_version", None):
                try:
                    saved = app_config.load_settings(self.config_path)
                    self._window_settings_stale = False
                except (ConfigError, OSError):
                    self._window_settings_stale = True
                    if hasattr(self, "_window_settings_cache"):
                        return self._window_settings_cache
                    raise
                self._window_settings_cache = saved
                # Keep the pre-read version so a concurrent replacement is seen next tick.
                self._window_settings_version = version
            return self._window_settings_cache

    def _refresh_peers(self) -> None:
        try:
            snapshot = getattr(self.book, "snapshot", None)
            peers, zones = snapshot() if callable(snapshot) else (self.book.peers(), self.book.zones())
        except (ConfigError, OSError):
            LOGGER.exception("The peers could not be read")
            peers, zones = [], []
        self._peer_entries, self._zone_entries = peers, zones
        if self._config is not None and self._config.design_follow_peer not in {p.get("id") for p in peerlist.desktops(peers)}:
            if self._config.design_follow_peer:
                self._config.design_follow_peer = ""
                self._persist()
        self._refresh_design_follow()
        self._set_machine_peers(peers)
        if hasattr(self, "atlas_canvas"):
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
        self._set_machine_peers(self._peer_entries)

    def _set_machine_peers(self, peers: list) -> None:
        group_on = settings_sync.group_on(self._config, "names") if self._config else False
        shortcut_goes_to = self._config.shortcut_goes_to if self._config else "last"
        self.machines.set_peers(self._peer_items(peers), same_on=group_on, cycle_mode=shortcut_goes_to)

    def _any_send(self) -> bool:
        return any(entry.get("in_use", True) is True and entry.get("send") and protocol.linkable(entry)
                   for entry in peerlist.desktops(self._peer_entries))

    def _label_of(self, peer: Optional[bytes]) -> str:
        return self._labels_by_id.get(peer, "another machine") if peer is not None else ""

    def _header_response_time(self):
        outgoing = {peer for entry in self._peer_entries
                    if (peer := protocol.read_id(entry.get("id"))) is not None and self.sender.links.up(peer)}
        incoming = self.server.links()
        peer = self.sender.owner if self.sender.owner in outgoing else None
        if peer is None and self.server.owner in incoming:
            peer = self.server.owner
        if peer is None:
            candidates = outgoing | incoming
            peer = min(candidates) if candidates else None
        if peer is None:
            return "", None, False
        link = self.sender.links._link(peer)
        response = getattr(link, "response_time_ms", None) if link is not None and link.live() else None
        if response is None and peer in incoming:
            lookup = getattr(self.server, "response_time_ms", None)
            response = lookup(peer) if lookup is not None else None
        return self._label_of(peer), response, True

    def _drive_status(self) -> str:
        if self.sender.redirecting:
            return tr("Driving {name}").format(name=self._label_of(self.sender.owner))
        driver = self.server.owner
        return tr("Driven by {name}").format(name=self._label_of(driver)) if driver is not None else tr("Idle")

    def _refresh_tray_state(self) -> None:
        state = self._shown(self._drive_status())
        previous = getattr(self, "_last_drive_status", None)
        if previous is not None and previous != state:
            self._last_refusal = ""
            self._refusal_notices.reset()
        self._last_drive_status = state
        tooltip = f"{state}\n{self._shown(self._last_refusal)}" if self._last_refusal else state
        if hasattr(self, "tray"):
            self.tray.setToolTip(tooltip)
            if hasattr(self, "status_action"):
                self.status_action.setText(state)
            self._refresh_tray_icon()

    def tray_shown(self) -> bool:
        """Whether the tray icon is up: there is a tray and this PC's Show in tray is on."""
        return self.tray_available and (self._config is None or self._config.show_in_tray)

    def _drive_glyph(self) -> str:
        held = self.sender.edges_held and bool(self._ways_in())
        return tray_glyph.drive_state(self.sender.redirecting, self.server.owner is not None, held)

    def _refresh_tray_icon(self) -> None:
        """The tray's glyph for who holds input, with the receiver's state as its badge, set only
        when one of the three things it draws from changes."""
        if not hasattr(self, "tray"):
            return
        with self._status_lock:
            status = self._status.value.lower()
        dark = tray_glyph.taskbar_is_dark(theme.system_dark())
        drive = self._drive_glyph()
        key = (drive, status, dark)
        if key == self._tray_icon_key:
            return
        self._tray_icon_key = key
        self.tray.setIcon(tray_glyph.icon(drive, status, dark))

    def _current_chain_sentence(self) -> str:
        route = self.sender.route_chain
        if len(route) <= 1:
            driver = self.server.owner
            route = (driver, self._identity()["id"]) if driver is not None else ()
        if not route:
            return tr("Current chain: Idle")
        identity = self._identity()
        own_id = protocol.id_text(identity["id"])
        names = []
        for ident in route:
            key = ident if isinstance(ident, str) else protocol.id_text(ident)
            if key == own_id:
                names.append(identity["name"])
            else:
                names.append(self._labels_by_id.get(protocol.read_id(key), "another machine"))
        return tr("Current chain: {names}").format(names=" → ".join(names))

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

    def _set_peer_pin(self, token: str, enabled: bool) -> None:
        if self._edit_peer(token, host_pinned=bool(enabled)):
            self._after_peers_edit()

    def _set_peer_in_use(self, token: str, enabled: bool) -> None:
        """Takes input home before closing this machine's links when it leaves use."""
        if not enabled and self._input_is_on(token):
            self.sender.set_redirecting(False)
        if self._edit_peer(token, in_use=bool(enabled)):
            self._after_peers_edit()

    def _set_peer_left_out(self, token: str, enabled: bool) -> None:
        try:
            with app_config.SETTINGS_LOCK:
                settings = app_config.load_settings(self.config_path)
                entry = next((item for item in settings["peers"] if item.get("token") == token), None)
                if entry is None or entry.get("port") == 0:
                    return
                event = crossing_state.leave_out if enabled else crossing_state.put_back
                self._carry_crossing(event(settings, entry["id"], **self._crossing_event_args()))
        except (ConfigError, OSError):
            LOGGER.exception("A machine's layout state could not be saved")
            return
        self._refresh_peers()

    def _set_every_peer(self, field: str, enabled: bool) -> None:
        """The tray's two ticks: one direction for every paired machine at once."""
        if field == "send" and not enabled and self.sender.owner is not None:
            # Home first, as _set_peer_send does: before the save the links read `send` from.
            self.sender.set_redirecting(False)
        try:
            with app_config.SETTINGS_LOCK:
                settings = app_config.load_settings(self.config_path)
                for entry in peerlist.desktops(settings["peers"]) if field == "send" else settings["peers"]:
                    entry[field] = bool(enabled)
                app_config.write_settings(self.config_path, settings)
        except (ConfigError, OSError):
            LOGGER.exception("The machines' settings could not be saved")
            return
        self._after_peers_edit()

    def _refresh_tray_directions(self) -> None:
        """Name each direction's scope and distinguish none, some and all machines being enabled."""
        for action, field, text in (
            (self.send_action, "send", "This PC drives machines"),
            (self.drive_action, "allow_drive", "Machines drive this PC"),
        ):
            entries = peerlist.desktops(self._peer_entries) if field == "send" else self._peer_entries
            if len(entries) == 1:
                who = self._shown(peerlist.labels(entries)[entries[0]["token"]])
                text = f"{who} drives this PC" if field == "allow_drive" else f"This PC drives {who}"
            elif len(entries) > 1:
                text = "Every machine drives this PC" if field == "allow_drive" else "This PC drives every machine"
            action.setEnabled(bool(entries))
            enabled = [entry.get(field) is True for entry in entries]
            if enabled and any(enabled) and not all(enabled):
                text += " (some enabled)"
            if action.text() != text:
                action.setText(text)
            on = bool(enabled) and all(enabled)
            if action.isChecked() != on:
                action.setChecked(on)

    def _refresh_tray_machines(self) -> None:
        """List each send target; the active target's row returns input to this PC."""
        labels = peerlist.labels(self._peer_entries)
        sendable = [(entry["id"], self._shown(labels[entry["token"]])) for entry in self._peer_entries
                    if entry.get("in_use", True) is True and entry.get("send") and entry.get("port")
                    and protocol.read_id(entry.get("id")) is not None]
        items = sendable if len(sendable) > 1 else []
        if [ident for ident, _label in items] != list(self.machine_actions):
            for action in self.machine_actions.values():
                self.tray_menu.removeAction(action)
                action.deleteLater()
            self.machine_actions = {}
            for ident, _label in items:
                action = QAction("", self.tray_menu)
                action.triggered.connect(
                    lambda _checked=False, peer=protocol.read_id(ident): self._send_to_from_tray(peer)
                )
                self.tray_menu.insertAction(self.pause_action, action)
                self.machine_actions[ident] = action
        owner = self.sender.owner if self.sender.redirecting else None
        for ident, label in items:
            action = self.machine_actions[ident]
            peer = protocol.read_id(ident)
            # A machine whose link is down cannot take input: Beamer is not running there, or it is
            # off or away. Dimmed, so the row says so before a click does.
            live = peer == owner or self._live(ident)
            text = ("Bring input back" if peer == owner else f"Send input to {label}" if live
                    else f"{label} is unavailable")
            if action.text() != text:
                action.setText(text)
            action.setEnabled(live and (peer == owner or not platform_parts.WAYLAND))
        self.redirect_action.setVisible(not self.machine_actions)

    def _send_to_from_tray(self, peer: bytes) -> None:
        """Return input when its active target is chosen; otherwise switch to that machine."""
        if self.sender.redirecting and self.sender.owner == peer:
            self.sender.set_redirecting(False)
        else:
            self.sender.go(peer)

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
        # A pairing or a removal changes this machine's status ("waiting"): every linked peer hears.
        self._publish_desk()

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
                before = ways.way_back_state(settings)
                removed = peerlist.remove_peer(settings, token)
                if removed is None:
                    return
                if settings.get("design_follow_peer") == removed.get("id"):
                    settings["design_follow_peer"] = ""
                after = ways.way_back_state(settings)
                app_config.write_settings(self.config_path, settings)
                remaining = bool(settings["peers"])
        except (ConfigError, OSError):
            LOGGER.exception("A machine could not be removed")
            self._on_alert("Beamer", tr("Beamer could not save the removal. Try again in a moment."))
            return
        LOGGER.info("Removed %s from the paired machines",
                    peerlist.label_for([removed], token=token) or "a machine")
        if remaining:
            self._after_peers_edit()
            for peer, state in after.items():
                if before.get(peer) != state:
                    self._tell(peer)
            return
        self.sender.set_redirecting(False)
        self._stop_sending()
        self._paired = False
        try:
            self._config = load_config(self.config_path, unpaired_ok=True)
        except (ConfigError, OSError):
            self._config = None
        self._same_seen = self._same_fields() if self._config is not None else None
        self._design_seen = self._design_fields() if self._config is not None else None
        self.sender.update_config(self._config)
        if self._config is not None:
            self._reflect_config(self._config)
        self.server.peers_changed()
        self.sender.refresh()
        for check in self.same_checks.values():
            check.setEnabled(False)
        self._refresh_peers()

    def _toggle_pairing_sheet(self) -> None:
        self._pairing_open = not self._pairing_open
        if not self._pairing_open:
            if self.pairing is not None:
                self.pairing.cancel_pairing()
                self._cancel_nearby()
        self.sheet.setVisible(self._pairing_open)
        self.machines.set_pairing_open(self._pairing_open)
        self._refresh_pairing()

    def _open_pairing_sheet(self) -> None:
        if not self._pairing_open:
            self._pairing_open = True
            self.sheet.setVisible(True)
            self.machines.set_pairing_open(True)

    def _pair_phone(self) -> None:
        self._open_pairing_sheet()
        self._show_code()

    def _start_nearby(self) -> None:
        self._open_pairing_sheet()
        service = self.pairing
        if service is None:
            self.sheet.say_host(tr("Pairing is not available: this PC's settings could not be read."), "note-fault")
            return
        if service.error:
            self.sheet.say_host(tr("Pairing is not available: {error}").format(error=service.error), "note-fault")
            return
        self._cancel_code()
        self.sheet.say_host("", "note")
        try:
            service.begin_nearby()
        except pairing.PairingError as exc:
            self.sheet.say_host(tr("Nearby pairing is not available: {error}").format(error=exc), "note-fault")
        self._refresh_pairing()

    def _choose_nearby(self, machine: dict) -> None:
        service = self.pairing
        if service is None:
            return
        try:
            service.choose_nearby(machine)
        except pairing.PairingError as exc:
            self.sheet.say_host(tr("Nearby pairing could not continue: {error}").format(error=exc), "note-fault")
        self._refresh_pairing()

    def _confirm_nearby(self, sas: str, attempt: str) -> None:
        service = self.pairing
        if service is None:
            return
        try:
            service.confirm_nearby(sas, attempt)
        except pairing.PairingError as exc:
            self.sheet.say_host(tr("Nearby pairing could not continue: {error}").format(error=exc), "note-fault")
        self._refresh_pairing()

    def _cancel_nearby(self) -> None:
        service = self.pairing
        if service is not None:
            service.cancel_nearby()
            if getattr(self, "sheet", None) is not None:
                self._refresh_pairing()

    def _re_pair(self, token: str) -> None:
        entry = next((item for item in self._peer_entries if item.get("token") == token), None)
        if entry is None:
            return
        self._cancel_nearby()
        self._select_page("overview")
        self._pairing_open = True
        address = entry.get("host") or ""
        if self._hide_addresses():
            self.sheet.address.clear()
            self._refresh_pairing()
            if self.pairing is not None and any(item.get("address") == address for item in self.pairing.machines()):
                self.sheet._pick(address)
        else:
            self.sheet.address.setText(address)
        self.sheet.setVisible(True)
        self.machines.set_pairing_open(True)
        self._refresh_pairing()

    def _show_code(self) -> None:
        if self.pairing is None:
            self.sheet.say_host(tr("Pairing is not available: this PC's settings could not be read."), "note-fault")
            return
        if self.pairing.error:
            self.sheet.say_host(tr("Pairing is not available: {error}").format(error=self.pairing.error), "note-fault")
            return
        self.sheet.say_host("", "note")
        self.pairing.cancel_nearby()
        try:
            self.pairing.begin_pairing()
        except pairing.PairingError as exc:
            text = peerlist.host_outcome_text("full", None) if str(exc) == pairing.ERROR_FULL else tr("Pairing is not available: {error}").format(error=exc)
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
        self.sheet.set_nearby(service.nearby_status, self._shown)
        if self._pairing_open:
            self.sheet.set_machines(service.machines(), self._shown)
        code = service.code
        if code is not None:
            hide = self._hide_addresses()
            address = getattr(self, "_code_address", "") if self._code_shown else self._pairing_address()
            self._code_address = address
            rows, note = None, ""
            if hide:
                note = tr("The QR holds this PC's address, so it is not shown while Hide addresses is on.")
            else:
                text = service.qr_text(address) if address else None
                if text is not None:
                    if self._qr_cache[0] != text:
                        self._qr_cache = (text, qr_code.modules(text))
                    rows = self._qr_cache[1]
            where = tr("This PC's address: {address} · port {port}").format(address=address, port=service.tcp_port) if address else ""
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
            self.sheet.say_pair(tr("Pairing is not available: this PC's settings could not be read."), "note-fault")
            return
        service.cancel_nearby()
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
        label = peerlist.label_for(self.book.peers(), token=entry.get("token")) or "that machine"
        self.sheet.say_pair(self._shown(tr("Paired with {name}.").format(name=label)), "note-live")
        LOGGER.info("Paired with %s", label)
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
        privacy = widgets.Module(tr("Addresses"))
        self.hide_switch = widgets.Switch(tr("Hide addresses"))
        self.hide_switch.setFont(theme.font(theme.TYPE["body"]))
        self.hide_switch.setChecked(self._hide_addresses())
        self.hide_switch.toggled.connect(self._set_hide_addresses)
        privacy.body.addWidget(self.hide_switch)
        privacy.body.addWidget(widgets.label(
            tr("Hides every IP and hardware address in this window and the tray."),
            "note",
            wrap=True,
        ))
        layout.addWidget(privacy)
        language = widgets.Module(tr("Language"))
        language_choice = widgets.Choice(
            [("auto", tr("Automatic"))] + [(code, name) for code, name in i18n.LANGUAGES.items()],
            3,
            current.language,
            self._set_language,
        )
        self.language_choice = language_choice
        language.body.addWidget(language_choice.view)
        self.language_note = widgets.label(tr("Beamer changes language when it next opens."), "note", wrap=True)
        self.language_note.setVisible(False)
        language.body.addWidget(self.language_note)
        self.language_relaunch = QPushButton(tr("Relaunch Beamer"))
        self.language_relaunch.clicked.connect(self.relaunch)
        self.language_relaunch.setVisible(False)
        language.body.addWidget(self.language_relaunch, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(language)
        module = widgets.Module(tr("This PC"))
        def field(caption, entry):
            # The caption is the field's buddy and its accessible name, so a screen reader and
            # Alt+letter both reach the field by what it is called.
            key = widgets.label(caption, "key")
            key.setBuddy(entry)
            entry.setAccessibleName(caption)
            module.body.addWidget(widgets.FormRow(key, entry))

        # Read-only: the address that faces the machines this PC knows, which is what another machine
        # types to pair by address.
        self.host_readout = widgets.label(self._shown(self._host) or tr("Not known yet"), "readout", wrap=True)
        self.host_readout.setAccessibleName(tr("This PC's address"))
        field(tr("This PC's address"), self.host_readout)
        self.port_entry = QLineEdit(str(current.port))
        self.port_entry.setReadOnly(portable_runtime.enabled())
        self.port_entry.setMaximumWidth(240)
        field(tr("This PC's listening port"), self.port_entry)
        self.save_message = widgets.label("", "note", wrap=True)
        self.save_message.setVisible(False)
        module.body.addWidget(self.save_message)
        self.save_button = QPushButton(tr("Save and reconnect"))
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
        self._refresh_tray_state()

    def _set_language(self, language: str) -> None:
        if language != "auto" and language not in i18n.LANGUAGES:
            return
        if self._config is None:
            try:
                app_config.set_own(self.config_path, language=language)
            except (ConfigError, OSError):
                LOGGER.exception("Language setting could not be saved")
                return
            self._kept_language = language
        else:
            self._config.language = language
            self._persist()
        self.language_note.setVisible(True)
        self.language_relaunch.setVisible(True)

    def relaunch(self) -> None:
        executable = sys.executable
        arguments = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
        if "--relaunched" not in arguments:
            arguments = [*arguments, "--relaunched"]
        started, _process_id = QProcess.startDetached(executable, arguments)
        if started:
            self.quit()

    def save(self) -> None:
        """The Connection page's one setting is this PC's own port. It never touches a peer's entry, whose
        port is that machine's own, and it works with nothing paired."""
        try:
            port = int(self.port_entry.text().strip())
            if not 1 <= port <= 65535:
                raise ValueError
            app_config.set_own(self.config_path, port=port)
        except (TypeError, ValueError):
            self.save_message.setText(tr("port must be an integer from 1 to 65535"))
            self.save_message.setVisible(True)
            widgets.set_role(self.save_message, "note-fault")
            return
        except (ConfigError, OSError) as exc:
            LOGGER.exception("Configuration could not be saved")
            QMessageBox.critical(self, tr("Save failed"), str(exc))
            return
        self._settings_port = port
        if self._config is not None:
            config = replace(self._config, port=port)
            if self._paired:
                self._apply_config(config)
            else:
                self._config = config
        self.save_message.setText(tr("Saved."))
        self.save_message.setVisible(True)
        widgets.set_role(self.save_message, "note-live")

    @_crossing_refresh
    def _apply_config(self, config: Config) -> None:
        self._config = config
        set_divisor(effects.frame_divisor(config.frame_rate))
        self._settings_port = config.port
        self._same_seen = self._same_fields()
        self._design_seen = self._design_fields()
        self._apply_input_scale(config)
        if not config.edge_glow:
            self._hide_crossing()
        for check in self.same_checks.values():
            check.setEnabled(self._paired)
        if self._paired:
            self._start_receiver(config)
        self.server.peers_changed()
        self._send_paired()
        self.sender.update_config(config)
        self._configure_shake()
        configure_seams = getattr(self, "_configure_capture_seams", None)
        if callable(configure_seams):
            configure_seams(config)
        self._refresh_peers()
        if self._any_send():
            self._start_sending(config)
        else:
            self._stop_sending()
        self.host_readout.setText(self._shown(config.host) or "Not known yet")

    def _configure_capture_seams(self, config: Config) -> None:
        configure = getattr(getattr(self, "hooks", None), "configure_seams", None)
        if not callable(configure):
            return
        try:
            configure(self.sender._cached_monitors, config.crossing_resistance_px,
                      display_map=self.sender._cached_display_map, models=self.sender.zone_models)
        except Exception:
            LOGGER.exception("Could not configure X11 display seam holds")

    # -- Firewall, on Connection --------------------------------------------------------------

    def _firewall_module(self, layout) -> None:
        if sys.platform.startswith("linux"):
            title = tr("Linux firewall")
            checking = tr("Beamer's firewall check is not yet on Linux.")
        else:
            title = tr("Windows Firewall")
            checking = tr("Checking Windows Firewall…")
        module = widgets.Module(title)
        self.firewall_note = widgets.label(checking, "note", wrap=True)
        module.body.addWidget(self.firewall_note)
        self.firewall_button = QPushButton(tr("Checking…"))
        self.firewall_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.firewall_button.setEnabled(False)
        self.firewall_button.clicked.connect(self._firewall_action)
        module.body.addWidget(self.firewall_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(module)

    def _firewall_target(self) -> tuple:
        port = self._config.port if self._config is not None else protocol.DEFAULT_PORT
        return str(Path(sys.executable).resolve()), port

    def _check_firewall(self) -> None:
        if portable_runtime.enabled():
            self.firewall_note.setText(tr("Portable mode leaves Windows Firewall unchanged. Your network must already allow Beamer."))
            self.firewall_button.hide()
            return
        self._run_firewall(firewall_win.status, tr("Checking…"))

    def _firewall_action(self) -> None:
        if portable_runtime.enabled():
            return
        self._check_firewall()

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
                result = firewall_win.FirewallStatus(port, False, False, (), (), False, (), firewall_win.is_elevated(), error=tr("the change failed: {error}").format(error=exc))
            self.bridge.firewall.emit(result)

        threading.Thread(target=work, name="Beamer-firewall", daemon=True).start()

    def _on_firewall(self, status) -> None:
        if portable_runtime.enabled():
            return
        self._firewall_busy = False
        if self._closing:
            return
        self._firewall_status = status
        advice = firewall_win.advise(status)
        self._firewall_advice = advice
        self.firewall_note.setText(advice.sentence)
        if status.error:
            tone = "note-fault"
        elif (not status.blocked and status.allowed and status.covered
              and status.pairing_allowed and status.pairing_covered
              and status.pairing_tcp_allowed and status.pairing_tcp_covered):
            tone = "note"
        else:
            tone = "note-amber"
        # Every action now rechecks; the sidebar reflects rule coverage, not button wording.
        self._firewall_tone = tone
        widgets.set_role(self.firewall_note, tone)
        self.firewall_button.setText(advice.button)
        self.firewall_button.setEnabled(advice.action is not None)
        if self._firewall_again:
            self._firewall_again = False
            self._check_firewall()
            return

    # -- theming, tray, lifecycle ------------------------------------------------------------

    def _apply_theme(self) -> None:
        app = QApplication.instance()
        if app is not None:
            theme.apply_stylesheet(app)
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
        self.tray = QSystemTrayIcon(self)
        self._refresh_tray_icon()
        self.tray.setToolTip(self._drive_status())
        menu = QMenu()
        self.status_action = menu.addAction(self._drive_status())
        self.status_action.setEnabled(False)
        menu.addSeparator()
        # The version line; it becomes the way to a newer release when there is one.
        self.header_action = menu.addAction(tr("Beamer {version}").format(version=VERSION))
        self.header_action.setEnabled(False)
        self.header_action.triggered.connect(self.open_update)
        menu.addSeparator()
        self.redirect_action = menu.addAction(tr("Send input across"))
        self.redirect_action.triggered.connect(self.toggle_redirect)
        self.pause_action = menu.addAction(tr("Pause crossing"))
        self.pause_action.triggered.connect(self.toggle_pause)
        menu.addSeparator()
        self.tray_menu = menu
        # One tick per direction, each the same switch the Overview page shows, so either
        # direction can be turned off while the other keeps working and the two never disagree.
        self.send_action = menu.addAction(tr("This PC drives machines"))
        self.send_action.setCheckable(True)
        self.send_action.triggered.connect(lambda on: self._set_every_peer("send", on))
        self.drive_action = menu.addAction(tr("Machines drive this PC"))
        self.drive_action.setCheckable(True)
        self.drive_action.triggered.connect(lambda on: self._set_every_peer("allow_drive", on))
        self.profile_menu = menu.addMenu(tr("Profile"))
        self._refresh_tray_profiles()
        menu.addSeparator()
        self.open_action = menu.addAction(tr("Settings…"))
        self.open_action.triggered.connect(lambda _checked=False: self.show_window())
        menu.addAction(tr("Reload configuration")).triggered.connect(self.reload_config)
        menu.addAction(tr("Open log folder")).triggered.connect(self.open_log_folder)
        menu.addSeparator()
        menu.addAction(tr("About Beamer")).triggered.connect(self.show_about)
        menu.addAction(tr("Quit Beamer")).triggered.connect(self.quit)
        self.tray.setContextMenu(menu)
        # Left click opens the window; right click is the
        # context menu Qt already gives QSystemTrayIcon for free.
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.setVisible(self.tray_shown())
        self._refresh_tray_state()

    def _refresh_tray_profiles(self, settings=None) -> None:
        if not hasattr(self, "profile_menu"):
            return
        self.profile_menu.menuAction().setVisible(False)
        self.profile_menu.clear()
        if self._config is None:
            return
        try:
            settings = app_config.load_settings(self.config_path) if settings is None else settings
        except (ConfigError, OSError):
            return
        entries = profiles.profile_list(settings)
        visible = profiles.same_ways_on(settings) and len(entries) >= 2
        self.profile_menu.menuAction().setVisible(visible)
        if not visible:
            return
        active = settings["crossing_desk"]["active"]["value"]
        for entry in entries:
            label = tr("Desk") if entry["id"] == profiles.DESK_ID and entry["name"] == "Desk" else entry["label"]
            action = self.profile_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(entry["id"] == active)
            action.triggered.connect(lambda _checked=False, pid=entry["id"]: self._switch_tray_profile(pid))

    def _switch_tray_profile(self, profile_id: str) -> None:
        if self._config is None:
            return
        try:
            with app_config.SETTINGS_LOCK:
                settings = app_config.load_settings(self.config_path)
                self._carry_crossing(crossing_state.switch_profile(
                    settings, profile_id, **self._crossing_event_args()))
        except (ConfigError, OSError):
            LOGGER.exception("The profile could not be saved")
            self._refresh_tray_profiles()

    def reload_config(self) -> None:
        """Re-reads settings.json, for the times it was edited outside the window, and shows it."""
        try:
            config = load_config(self.config_path, unpaired_ok=True)
        except (ConfigError, OSError) as exc:
            LOGGER.warning("Configuration not reloaded: %s", exc)
            self._on_alert("Beamer", tr("Could not reload the configuration: {error}").format(error=exc))
            return
        self._host = config.host
        self._paired = bool(self.book.peers())
        self._apply_config(config)
        self._configure_trigger(config)
        self._reflect_config(config)
        LOGGER.info("Configuration reloaded from %s", self.config_path)

    @_crossing_refresh
    def _reflect_config(self, config: Config) -> None:
        """Every control on every page set from `config`, without any of them saving back."""
        set_divisor(effects.frame_divisor(config.frame_rate))
        for box in ( self.dragging_switch, self.hold_switch, self.glow_toggle,
                    self.landing_toggle, self.tray_switch, self.pointer_switch,
                    self.resistance_slider, self.double_tap_slider):
            box.blockSignals(True)
        try:
            self.tray_switch.setChecked(config.show_in_tray)
            self.tray.setVisible(self.tray_shown())
            self.pointer_switch.setChecked(config.hide_idle_pointer)
            self.dragging_switch.setChecked(config.block_while_dragging)
            self.hold_switch.setChecked(config.hold_full_screen)
            self.glow_toggle.setChecked(config.edge_glow)
            self.landing_toggle.setChecked(config.shortcut_arrival)
            self.switch_style_choice.set_value(config.shortcut_arrival_style)
            self.resistance_slider.setValue(config.crossing_resistance_px)
            self.double_tap_slider.setValue(config.double_tap_ms)
        finally:
            for box in ( self.dragging_switch, self.hold_switch, self.glow_toggle,
                        self.landing_toggle, self.tray_switch, self.pointer_switch,
                        self.resistance_slider, self.double_tap_slider):
                box.blockSignals(False)
        self._crossing_text(self.resistance_readout, f"{config.crossing_resistance_px} px")
        self._update_resistance_hint(config.crossing_resistance_px)
        self._crossing_text(self.double_tap_readout, f"{config.double_tap_ms} ms")
        self.resistance_strip.set_value(config.crossing_resistance_px)
        motion.set_shown(self.double_tap_row, config.trigger_style != "hold", fade=False)
        self.trigger_recorder.set_title(TRIGGER_KEYS.get(config.trigger_key, config.trigger_key))
        self.trigger_style_choice.set_value(config.trigger_style)
        self._update_style_hint(config.trigger_style)
        self.modifier_choice.set_value(config.modifier_style)
        self.modifier_note.setText(MODIFIER_NOTES[config.modifier_style])
        self.glow_style_choice.set_value(config.glow_style)
        self.glow_colour_choice.set_value(config.glow_colour)
        self.length_choice.set_numeric(config.effect_pace)
        self.size_choice.set_numeric(config.effect_size_fraction)
        self.frame_rate_choice.set_value(config.frame_rate)
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
                              (self.mouse_only_switch, config.mouse_only),
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
        self.update_checker.start()
        self._listen()

    def _listen(self) -> None:
        if self._closing:
            return
        if self.pairing is not None:
            self.pairing.start()
        if sys.platform == "win32" and self._input_controller_blocked:
            self._input_controller_release = _claim_input_controller()
            if self._input_controller_release is None:
                self._set_status(ServerState.ERROR, tr("Another Beamer copy controls input on this PC. Quit it to use input in this copy."))
                QTimer.singleShot(1000, self._listen)
                return
            self._input_controller_blocked = False
            self._idle_pointer = self._make_idle_pointer()
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

    def _check_input_controller(self):
        if (self._closing or not portable_runtime.enabled()
                or not self._input_controller_release or not _installed_instance_running()):
            return
        # A legacy installed copy does not know the shared lease. Yield before the next
        # refresh rather than letting its hooks and this copy's hooks keep running together.
        self._input_controller_blocked = True
        self.server.stop()
        self._stop_sending()
        release, self._input_controller_release = self._input_controller_release, None
        release()
        self._listen()

    def _configure_trigger(self, config: Config) -> None:
        self._trigger.configure(config.trigger_key, config.trigger_style, config.double_tap_ms)

    def _start_receiver(self, config: Config) -> None:
        if getattr(self, "_input_controller_blocked", False):
            return
        if self.server.listening and self._receiver_port != config.port:
            self.server.stop()
        self._receiver_port = config.port
        self.server.start(config.port)

    def _start_sending(self, config: Config) -> None:
        """The outward link and the hooks that feed it. The hooks go in only
        when sending is on: they are the one part of Beamer that can take this
        PC's own keyboard away, so an install that never sends never installs
        them."""
        if getattr(self, "_input_controller_blocked", False):
            return
        if not self._any_send():
            return
        self._configure_trigger(config)
        self._configure_shake()
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
        if isinstance(name, TextInput):
            return self.sender.on_text(name.text, vk, down)
        if isinstance(name, DeadKey):
            return self.sender.on_dead_key(vk, down)
        if self.sender.handle_jump_key(name, down, vk):
            return True
        if not self.sender.shortcut_armed:
            self._trigger.disarmed(name, down)
            return self.sender.on_key(name, down, vk, us)
        action = self._trigger.feed(name, down, time.monotonic())
        if action is not None:
            if action == capture_win.TOGGLE:
                self.sender.toggle_shortcut()
            else:
                self.sender.hold_shortcut(action == capture_win.REDIRECT)
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
        available = [item for item in peers if item.get("in_use", True) is True]
        for item in available:
            peer = protocol.read_id(item.get("id"))
            if peer is not None and item.get("in_use", True) is True and item.get("send"):
                ids = [protocol.read_id(ident) for ident in peerlist.paired_ids(
                    peers, exclude=protocol.id_text(peer))]
                self._send_to(peer, protocol.paired_msg(ids))

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
        was_driven, self._driven = self._driven, bool(peer)
        if self._idle_pointer is not None:
            if self._driven:
                self._idle_pointer.cancel()
            elif was_driven and not self._closing and self._hiding_pointer():
                self._idle_pointer.driver_left()
        self._refresh_tray_state()

    def _on_peers_changed(self) -> None:
        """A link learnt something about a peer (an address, an id, a name, a hardware address) and
        saved it, or came up or went down: the window's flat view follows, and so do the links."""
        if self._config is None or self._closing:
            return
        self._pull_peer_fields()
        linked = {protocol.id_text(peer) for peer in self._peer_caps()}
        self._profile_last_heard = {peer: heard for peer, heard in self._profile_last_heard.items() if peer in linked}
        for peer in linked:
            self._profile_last_heard.setdefault(peer, time.time())
        self.sender.refresh()
        self._refresh_peers()
        self._publish_desk()

    def _on_sending(self, connected: bool, detail: str) -> None:
        self._sending_detail = detail

    def _make_idle_pointer(self):
        """The pointer hiding of this desktop, or None where it cannot hide another app's pointer
        (Wayland). A run that died with the pointer hidden is undone first."""
        if portable_runtime.enabled() or self._input_controller_blocked or not getattr(desktop_win, "CAN_HIDE_POINTER", False):
            return None
        try:
            desktop_win.restore_pointer()
        except Exception:
            LOGGER.exception("Could not give back a pointer an earlier run left hidden")
        return idle_pointer.IdlePointer(
            hide=lambda: desktop_win.hide_pointer("idle"),
            show=lambda: desktop_win.show_pointer("idle"),
            position=desktop_win.cursor_position,
            logger=LOGGER,
        )

    def _set_driving_pointer(self, redirecting: bool) -> None:
        """While this PC drives another machine its own pointer sits pinned at the edge it left by;
        hidden, so only the machine in use shows one (rufusmp, 12-10-2026). The Mac does the same."""
        if self._idle_pointer is None:
            return
        hide = redirecting and self._hiding_pointer()
        try:
            if hide:
                desktop_win.hide_pointer("driving")
            else:
                desktop_win.show_pointer("driving")
        except Exception:
            LOGGER.exception("Could not %s the pointer", "hide" if redirecting else "show")
        if sys.platform == "win32":
            veil = self._veil_hwnd
            if not hide and veil:
                # Input is local from this moment, on this thread; the queued hide below may run
                # later, so the veil stops taking clicks now.
                try:
                    desktop_win.set_click_through(veil, True)
                except Exception:
                    LOGGER.exception("Could not let clicks through the pointer veil")
            self.bridge.veil.emit(hide)

    def _show_veil(self, shown: bool) -> None:
        """Blanking the system cursors leaves an app's own cursor (a browser's `cursor: url()`, a
        game's) showing where the pointer was pinned. A tiny window under the pin owns the cursor
        there instead; the sender's warp back to the pin on every move makes Windows ask it."""
        if not shown:
            if self._veil is not None:
                self._veil.hide()
            return
        if self._veil is None:
            self._veil = QWidget(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                                 | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
            self._veil.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            # Nearly invisible, not invisible: a fully transparent layered window is hit-test
            # transparent, and the cursor would come from the window beneath after all.
            self._veil.setWindowOpacity(0.01)
            self._veil.setCursor(Qt.CursorShape.BlankCursor)
            self._veil.resize(VEIL_SIZE, VEIL_SIZE)
        point = QCursor.pos()
        self._veil.move(point.x() - VEIL_SIZE // 2, point.y() - VEIL_SIZE // 2)
        self._veil.show()
        self._veil.raise_()
        if sys.platform == "win32":
            self._veil_hwnd = int(self._veil.winId())
            try:
                desktop_win.set_click_through(self._veil_hwnd, False)
            except Exception:
                LOGGER.exception("Could not make the pointer veil take the cursor")

    def _on_sender_redirecting(self, redirecting: bool) -> None:
        # On the sender's thread, not through the window's: input coming home must show the pointer
        # even while the window is busy.
        self._set_driving_pointer(redirecting)
        self.bridge.redirecting.emit(redirecting)

    def _on_redirecting(self, redirecting: bool) -> None:
        self._sending_detail = (
            f"This PC's keyboard and mouse are on {self._label_of(self.sender.owner)}" if redirecting else self.sender.status
        )
        self._refresh_tray_state()

    def _announced_port(self) -> int:
        return self._settings_port

    def show_window(self, activation_token: Optional[str] = None) -> None:
        use_activation_token = bool(
            activation_token and sys.platform.startswith("linux") and platform_parts.WAYLAND
        )
        if use_activation_token:
            os.environ["XDG_ACTIVATION_TOKEN"] = activation_token
        self.showNormal()
        self.raise_()
        try:
            self.activateWindow()
        finally:
            if use_activation_token and os.environ.get("XDG_ACTIVATION_TOKEN") == activation_token:
                os.environ.pop("XDG_ACTIVATION_TOKEN", None)

    def open_home_page(self) -> None:
        QDesktopServices.openUrl(QUrl(HOME_PAGE))

    def show_about(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(tr("About Beamer"))
        box.setIconPixmap(QIcon(str(ICON_PATH)).pixmap(QSize(64, 64), 1.0))
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(tr(
            '<b>Beamer {version}</b><br>One keyboard and mouse for all your Macs and PCs.<br><br><a href="{url}" style="color: {colour};">{link}</a>'
        ).format(version=VERSION, url=HOME_PAGE, colour=theme.colour("signal"), link=tr(HOME_PAGE_TEXT)))
        if portable_runtime.enabled():
            box.setInformativeText(tr("Portable — settings and pairing keys stay in this folder, protected by your passphrase. Administrator apps cannot be controlled."))
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        for text in box.findChildren(QLabel):
            text.setOpenExternalLinks(True)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.show()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if hasattr(self, "update_notice"):
            self._refresh_update_notice()
        if hasattr(self, "refresh_timer"):
            self._refresh_window()
        self._run_previews()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.ignored_recorder.cancel()
        self.trigger_recorder.cancel()
        self.crossing_ledger.cancel_recording()
        service = getattr(self, "pairing", None)
        if service is not None and not getattr(self, "_closing", False):
            service.cancel_nearby()
            if getattr(self, "sheet", None) is not None:
                self._refresh_pairing()
        self._run_previews()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            if hasattr(self, "refresh_timer") and not self.isMinimized():
                self._refresh_window()
            self._run_previews()

    def quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.identify_screens.stop()
        self.refresh_timer.stop()
        self.update_checker.stop()
        if self.pairing is not None:
            self.pairing.cancel_nearby()
            self.pairing.stop()
        self.server.stop()
        self._stop_sending()
        shortcut = getattr(self, "global_shortcut", None)
        if shortcut is not None:
            shortcut.stop()
        stop_injector = getattr(platform_parts.injector, "stop", None)
        if stop_injector is not None:
            stop_injector()
        self._hide_crossing()
        if self._idle_pointer is not None:
            self._idle_pointer.cancel()
            try:
                desktop_win.restore_pointer()
            except Exception:
                LOGGER.exception("Could not show the pointer on the way out")
        if hasattr(self, "tray"):
            self.tray.hide()
        release = getattr(self, "_input_controller_release", None)
        if release is not None:
            self._input_controller_release = None
            release()
        QApplication.quit()

    def closeEvent(self, event) -> None:
        if self._closing:
            super().closeEvent(event)
            return
        tray = getattr(self, "tray", None)
        tray_visible = tray is not None and getattr(tray, "isVisible", lambda: False)()
        if not tray_visible or not QSystemTrayIcon.isSystemTrayAvailable():
            self.quit()
            event.accept()
            return
        event.ignore()
        self.hide()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            if QSystemTrayIcon.isSystemTrayAvailable() and self.tray.isVisible():
                self.hide()
            else:
                self.quit()
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
            self.effects = (EffectOverlayX11Proxy(on_failure=self._on_effects_failed)
                            if xwayland_effects_available() else EffectOverlay(on_failure=self._on_effects_failed))
            if isinstance(self.effects, EffectOverlayX11Proxy):
                self.glow = self.effects
        if self.effects.failed:
            return None
        self.effects.configure(config.glow_style, config.glow_colour, config.effect_length, config.effect_size,
                               config.effect_pace, config.effect_size_fraction)
        ready = (self.effects.effect_ready if isinstance(self.effects, EffectOverlayX11Proxy)
                 else self.effects._fx is not None)
        if self.effects.failed or not ready:
            return None
        return self.effects

    def _on_pressure(self, edge: str, pressure: float, crossed: bool, part=None) -> None:
        if (self._closing or self._config is None or not self._config.edge_glow
                or (platform_parts.WAYLAND and not os.environ.get("DISPLAY"))):
            return
        overlay = self._effect_overlay()
        if overlay is not None:
            if platform_parts.WAYLAND:
                # InputCapture keeps the authoritative pointer pinned at the edge.
                x, y = platform_parts.desktop.cursor_position()
                point = QPointF(x, y)
                if isinstance(overlay, EffectOverlayX11Proxy):
                    box = desktop_box()
                    overlay.push(edge, pressure, crossed, cursor=point, part=part, desk=box)
                else:
                    overlay.push(edge, pressure, crossed, cursor=point, part=part)
            else:
                overlay.push(edge, pressure, crossed, part=part)
            return
        style = self._config.glow_style
        if self.glow is None:
            self.glow = (EffectOverlayX11Proxy(on_failure=self._on_effects_failed)
                         if xwayland_effects_available() else EdgeGlow())
            if isinstance(self.glow, EffectOverlayX11Proxy):
                self.effects = self.glow
        # An effect that failed earlier in the run falls back to the plain glow, in its colour.
        glow_style = style if style in ("glow", "beam") else "glow"
        if isinstance(self.glow, EffectOverlayX11Proxy):
            self.glow.configure_glow(glow_style, self._config.glow_colour,
                                     self._config.effect_length, self._config.effect_size,
                                     self._config.effect_pace, self._config.effect_size_fraction)
        else:
            self.glow.configure(glow_style, self._config.glow_colour,
                                 self._config.effect_length, self._config.effect_size,
                                 self._config.effect_pace, self._config.effect_size_fraction)
        self.glow.set_pressure(edge, pressure, crossed, part)

    def _on_arrival(self, method: str, edge: str, x: float, y: float) -> None:
        """A crossing landed the pointer on `edge`, or with method "switch" input came here by a
        switch and the pointer is wherever it was left."""
        if platform_parts.WAYLAND and not os.environ.get("DISPLAY"):
            return
        if method == "switch":
            overlay = self._switch_overlay()
            if overlay is not None:
                if isinstance(overlay, EffectOverlayX11Proxy):
                    box = desktop_box()
                    overlay.switched(x, y, [box.x(), box.y(), box.width(), box.height()],
                                     self._config.shortcut_arrival_style)
                else:
                    overlay.switched(logical_point(x, y), self._config.shortcut_arrival_style)
            return
        overlay = self._effect_overlay()
        if overlay is not None:
            if isinstance(overlay, EffectOverlayX11Proxy):
                box = desktop_box()
                overlay.arrive(method, edge, x, y, [box.x(), box.y(), box.width(), box.height()])
            else:
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
            self.effects = (EffectOverlayX11Proxy(on_failure=self._on_effects_failed)
                            if xwayland_effects_available() else EffectOverlay(on_failure=self._on_effects_failed))
            if isinstance(self.effects, EffectOverlayX11Proxy):
                self.glow = self.effects
        if self.effects.failed:
            return None
        self.effects.configure(config.glow_style, config.glow_colour, config.effect_length, config.effect_size,
                               config.effect_pace, config.effect_size_fraction)
        ready = (self.effects.effect_ready if isinstance(self.effects, EffectOverlayX11Proxy)
                 else self.effects._fx is not None)
        if self.effects.failed or not ready:
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
            self._refresh_tray_state()
        except Exception:
            LOGGER.exception("Tray status update failed")
        # Re-read the firewall each time the receiver starts listening, never on a timer: a
        # Mac dropping and reconnecting flips WAITING <-> CONNECTED and must not trigger it.
        if state is ServerState.WAITING and self._last_seen_state not in (ServerState.WAITING, ServerState.CONNECTED):
            self._check_firewall()
        self._last_seen_state = state

    def _refresh_window(self, *, skip_hidden=False) -> None:
        if self._closing:
            return
        try:
            saved = self._window_settings()
        except (ConfigError, OSError):
            saved = {}
        if saved is not getattr(self, "_tick_settings", None):
            self._tick_settings = saved
            self._refresh_peers()
        self._reconcile_displays()
        if skip_hidden and (not self.isVisible() or self.isMinimized()):
            self._refresh_tray_state()
            self._refresh_tray_directions()
            self._refresh_tray_machines()
            pause = tr("Resume crossing" if self.sender.crossing_paused else "Pause crossing")
            if self.pause_action.text() != pause:
                self.pause_action.setText(pause)
            if self.pause_button.text() != pause:
                self.pause_button.setText(pause)
                widgets.set_role(self.pause_button, "primary" if self.sender.crossing_paused else "")
            redirect = "Bring input back to this PC" if self.sender.redirecting else self._redirect_text()
            if self.redirect_action.text() != redirect:
                self.redirect_action.setText(redirect)
            if self.redirect_button.full_text != redirect:
                self.redirect_button.set_full_text(redirect)
            self.redirect_action.setEnabled(self._can_send())
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
        heading = tr(STATUS_TITLES[state])
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
        location = tr("On {name}").format(name=self._label_of(self.sender.owner)) if self.sender.redirecting else tr("On this PC")
        if location != self.location_readout.text():
            self.location_readout.setText(location)
        trip = self.sender.round_trip_ms
        trip_text = f"{trip} ms" if trip is not None else ""
        if trip_text != self.round_trip_readout.text():
            self.round_trip_readout.setText(trip_text)
            self.round_trip_row.setVisible(trip is not None)
        redirect_text = "Bring input back to this PC" if self.sender.redirecting else self._redirect_text()
        if redirect_text != self.redirect_button.full_text:
            self.redirect_button.set_full_text(redirect_text)
            self.redirect_action.setText(redirect_text)
        sending = self._can_send()
        self.redirect_button.setEnabled(sending)
        self.redirect_action.setEnabled(sending)
        allowed = self._config is not None and self._any_send()
        self.redirect_note.setVisible(not allowed and self._paired)
        self.wayland_send_note.setVisible(allowed and not sending)
        pause_text = tr("Resume crossing" if self.sender.crossing_paused else "Pause crossing")
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
        self._refresh_tray_state()
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
        response_peer, response_time, response_connected = self._header_response_time()
        self.sidebar.set_link(tone, tr(SIDEBAR_LINK_WORDS.get(state, N_("Unknown"))), response_peer,
                              response_time, response_connected)
        dormant = any(peerlist.dormant_reason(entry) for entry in self._peer_entries)
        dots = pages_win.dots(self._config is None or not self._paired, self._firewall_tone, dormant)
        self.sidebar.set_dots(dots)
        self._peers_tick += 1
        self._refresh_peer_states()
        self._refresh_tray_directions()
        self._refresh_tray_machines()
        self._refresh_pairing()

    def _title(self) -> str:
        with self._status_lock:
            return self._shown(tr("Beamer — {status}: {detail}").format(
                status=tr(SIDEBAR_LINK_WORDS[self._status]), detail=self._status_detail))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Beamer Windows receiver")
    parser.add_argument("--config", type=Path, default=default_config_path(), help="path to settings.json")
    parser.add_argument("--hidden", action="store_true", help="start in the tray without showing the window")
    parser.add_argument("--relaunched", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--identify-x11", help=argparse.SUPPRESS)
    return parser.parse_args()


@functools.lru_cache(maxsize=None)
def _windows_kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    kernel32.OpenMutexW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetCurrentProcessId.restype = ctypes.c_ulong
    kernel32.ProcessIdToSessionId.argtypes = [ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
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
        mutex = kernel32.CreateMutexW(None, False, "Local\\Beamer.Receiver" + portable_runtime.instance_suffix())
        error = ctypes.get_last_error()
        already_running = error == 183
        if not mutex:
            # A medium replacement cannot open the high instance's mutex with all-access.
            # Treat it as occupied so --relaunched waits until the old process has quit.
            if error == 5:
                return None
            raise OSError("Beamer could not create its single-instance lock")
        if already_running:
            kernel32.CloseHandle(mutex)
            return None
        return lambda: kernel32.CloseHandle(mutex)
    from PySide6.QtCore import QLockFile

    lock = QLockFile(str(Path(tempfile.gettempdir()) / f"Beamer.Receiver.{os.getuid()}.lock"))
    lock.setStaleLockTime(0)
    return lock.unlock if lock.tryLock(0) else None


def _claim_input_controller():
    """App windows may coexist; session-wide hooks and injected input have one owner."""
    kernel32 = _windows_kernel32()
    if portable_runtime.enabled() and _installed_instance_running():
        # Older installed versions predate this lease. Their instance marker is enough to
        # keep portable input inactive, without activating or stopping the installed app.
        return None
    mutex = kernel32.CreateMutexW(None, False, "Local\\Beamer.InputController")
    already_running = ctypes.get_last_error() == 183
    if not mutex:
        return None
    if already_running:
        kernel32.CloseHandle(mutex)
        return None
    return lambda: kernel32.CloseHandle(mutex)


def _installed_instance_running():
    kernel32 = _windows_kernel32()
    installed = kernel32.OpenMutexW(0x00100000, False, "Local\\Beamer.Receiver")
    error = ctypes.get_last_error()
    if installed:
        kernel32.CloseHandle(installed)
        return True
    return error == 5


# A relaunched Beamer starts while the one it replaces still holds the lock, and waits this long
# for it to quit rather than handing over to it.
RELAUNCH_WAIT_SECONDS = 15


def _claim_instance_waiting(seconds, claim=_claim_instance, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + seconds
    while True:
        release = claim()
        if release is not None:
            return release
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        sleep(min(0.1, remaining))


def activate_existing_instance(hidden: bool, activation_token: Optional[str] = None) -> bool:
    if hidden:
        return False
    return send_instance_activation(activation_token=activation_token)


def prepare_windows_startup() -> None:
    if sys.platform == "win32" and not portable_runtime.enabled():
        autostart_win.migrate_legacy_task()


def unlock_portable(parent=None) -> bool:
    vault = PortableStore(portable_runtime.settings_path())
    creating = not vault.exists
    while True:
        title = tr("Protect Beamer Portable") if creating else tr("Unlock Beamer Portable")
        prompt = (tr("Choose a passphrase of at least 12 characters. You need it each time Beamer starts. "
                     "It protects pairing keys if this folder is lost. A forgotten passphrase cannot be recovered.")
                  if creating else tr("Enter the passphrase for this portable folder."))
        password, accepted = QInputDialog.getText(parent, title, prompt, QLineEdit.EchoMode.Password)
        if not accepted:
            vault.lock()
            return False
        if creating:
            if len(password) < 12:
                password = ""
                QMessageBox.warning(parent, title, tr("Use at least 12 characters for the passphrase."))
                continue
            confirmation, accepted = QInputDialog.getText(
                parent, title, tr("Enter the passphrase again."), QLineEdit.EchoMode.Password)
            if not accepted:
                password = ""
                vault.lock()
                return False
            if password != confirmation:
                password = confirmation = ""
                QMessageBox.warning(parent, title, tr("The passphrases do not match. Try again."))
                continue
            confirmation = ""
        try:
            vault.unlock(password)
        except (PortableStoreError, OSError, ValueError):
            QMessageBox.warning(parent, title, tr("The portable settings could not be unlocked or saved. "
                                                 "Check the passphrase and that the folder is writable. Existing pairing keys have been kept."))
        else:
            portable_runtime.store = vault
            return True
        finally:
            password = ""


def main() -> None:
    portable_runtime.initialise()
    arguments = parse_args()
    if portable_runtime.enabled() and arguments.config.resolve() != portable_runtime.settings_path().resolve():
        raise ConfigError("Portable mode cannot use a settings file outside its folder")
    if arguments.identify_x11 is not None:
        from identify_overlay import run_x11_helper
        sys.exit(run_x11_helper(arguments.identify_x11))
    try:
        prepare_windows_startup()
    except OSError:
        LOGGER.exception("Could not migrate the previous Windows sign-in task; keeping it for another attempt")
    release = _claim_instance()
    if release is None and arguments.relaunched:
        release = _claim_instance_waiting(RELAUNCH_WAIT_SECONDS, claim=_claim_instance)
    if release is None:
        activate_existing_instance(arguments.hidden, os.environ.get("XDG_ACTIVATION_TOKEN"))
        return
    startup_admin_error = None
    if sys.platform == "win32":
        import admin_mode
        try:
            relaunched_as_admin = admin_mode.relaunch_if_required(
                arguments.config.expanduser().resolve(), hidden=arguments.hidden)
        except OSError as exc:
            startup_admin_error = exc
        else:
            if relaunched_as_admin:
                release()
                return
    log_path = configure_logging()
    if log_path is not None:
        crash_log.install(log_path.parent, LOGGER)
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
    instance_signal = quit_signals = application = controller_release = None
    try:
        app = QApplication(sys.argv)
        app.setApplicationName("Beamer")
        app.setDesktopFileName("uk.co.kalkman.beamer")
        app.setQuitOnLastWindowClosed(False)
        theme.init_fonts()
        app.setFont(theme.font(theme.TYPE["body"]))
        if portable_runtime.enabled():
            if not unlock_portable():
                return
        try:
            startup_config = app_config.load_config(arguments.config.expanduser().resolve(), unpaired_ok=True)
            i18n.install(startup_config.language)
        except ConfigError:
            i18n.install("auto")
        if sys.platform == "win32":
            controller_release = _claim_input_controller()
        application = WindowsApplication(
            arguments.config.expanduser().resolve(), start_after_first_paint=not arguments.hidden,
            input_controller_available=sys.platform != "win32" or controller_release is not None,
        )
        application._input_controller_release = controller_release
        controller_release = None
        if startup_admin_error is not None:
            application._on_admin_mode_changed(startup_admin_error)
        quit_signals = quit_signals_module.QuitSignals(application.quit, parent=app)
        instance_signal = InstanceSignal(application.show_window, parent=app, commands=application.control_commands())
        if not instance_signal.listening:
            LOGGER.warning("Could not start the second-launch listener: %s", instance_signal.server.errorString())
        if not arguments.hidden or not application.tray_shown():
            application.show()
        if arguments.hidden:
            QTimer.singleShot(0, application.start)
        sys.exit(app.exec())
    finally:
        if quit_signals is not None:
            quit_signals.close()
        if instance_signal is not None:
            instance_signal.close()
        if application is not None:
            application.quit()
        if controller_release is not None:
            controller_release()
        release()
        portable_runtime.close()


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


def register_autostart_for_setup() -> int:
    if portable_runtime.enabled():
        return 1
    try:
        exe = autostart_win.installed_exe()
        if exe is None:
            raise OSError("Only the installed Beamer app can start at sign-in")
        autostart_win.set_enabled(True, exe)
    except OSError as exc:
        LOGGER.warning("Could not enable start at sign-in from setup: %s", exc)
        return 1
    return 0


def cleanup_for_uninstall() -> int:
    if portable_runtime.enabled():
        return 1
    failed = False
    exe = autostart_win.installed_exe()
    if exe is None:
        LOGGER.warning("Could not remove start-at-sign-in tasks outside the installed app")
        failed = True
    else:
        try:
            autostart_win.set_enabled(False, exe)
        except OSError as exc:
            LOGGER.warning("Could not remove the per-user logon task: %s", exc)
            failed = True
    try:
        autostart_win.remove_legacy_task()
    except OSError as exc:
        LOGGER.warning("Could not remove the legacy logon task: %s", exc)
        failed = True
    for rule in (
        "Beamer Receiver (TCP-In)",
        "Beamer Pairing (UDP-In)",
        "Beamer Pairing (TCP-In)",
    ):
        try:
            result = autostart_win._run(
                ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={rule}"]
            )
            if result.returncode != 0:
                LOGGER.warning("Could not remove firewall rule %s: %s", rule, result.stderr or result.stdout)
                failed = True
        except OSError as exc:
            LOGGER.warning("Could not remove firewall rule %s: %s", rule, exc)
            failed = True
    return int(failed)


if __name__ == "__main__":
    if sys.platform == "win32" and sys.argv[1:2] in (["--set-admin-mode"], ["--set-admin-startup"]):
        import admin_mode
        sys.exit(admin_mode.run_helper(sys.argv[1:]))
    # Before the single-instance lock, so a Beamer already running does not turn the probe away.
    if sys.argv[1:2] == ["--import-probe"]:
        sys.exit(import_probe(sys.argv[2], sys.argv[3:]))
    if sys.argv[1:2] == ["--enable-autostart"]:
        sys.exit(register_autostart_for_setup())
    if sys.argv[1:2] == ["--uninstall-cleanup"]:
        sys.exit(cleanup_for_uninstall())
    main()
