"""Offscreen rc.2 UI audit. Run each scale in a fresh Python process.

Example: QT_QPA_PLATFORM=offscreen QT_SCALE_FACTOR=1.5 python scripts/ui_audit/qt_audit.py
 --platform windows --output renders/windows --actions
No production settings, input hooks, listener, tray icon or desktop window is used.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "win_app"))


class FakePairing:
    def __init__(self):
        self.code = None
        self.error = ""
        self.seconds_left = 120
        self.tcp_port = 24820
        self.outcome = ""
        self.known = None
        self.discovered = []

    def machines(self):
        return self.discovered

    def begin_pairing(self):
        self.code = "123456"

    def cancel_pairing(self):
        self.code = None

    def qr_text(self, address):
        return f"beamer://pair?address={address}&port=24820&code=123456"

    def pair(self, machine, code):
        from core import pairing
        raise pairing.PairingError("wrong_code")

    def pair_by_address(self, address, code):
        return self.pair(None, code)

    def stop(self):
        pass


def fixtures(count, phone=False, long=False):
    from core import pairing, protocol
    names = ["Studio Mac", "Desk", "Linux", "Office", "Travel"]
    if long:
        names[0] = "Studio MacBook Pro with an exceptionally long machine name"
        names[2] = "Laboratory Linux workstation with the external display"
    peers = []
    for i in range(count):
        entry = pairing.peer_entry(bytes([i + 1]) * 16, names[i][:protocol.MAX_NAME_CHARS], ("macos", "windows", "linux")[i % 3],
                                   24820, protocol.id_text(bytes([i + 1]) * 32),
                                   "localhost-live.example.lan" if i == 0 else f"192.0.2.{10+i}", 1780000000)
        entry.update(send=True, allow_drive=True, in_use=True)
        peers.append(entry)
    if phone:
        entry = pairing.peer_entry(bytes([9]) * 16, "Alex's iPhone with a longer device name", "ios", 0,
                                   protocol.id_text(bytes([9]) * 32), "", 1780000000)
        entry.update(send=False, allow_drive=True, in_use=True)
        peers.append(entry)
    return peers


def visible(widget, root):
    return widget.isVisibleTo(root) and widget.width() > 0 and widget.height() > 0


def text_measure(widget):
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QTextDocument, QImage, QPainter, QColor
    from PySide6.QtWidgets import QLabel, QPushButton, QCheckBox, QStyle, QStyleOptionButton
    import widgets
    source_text = widget.text() if hasattr(widget, "text") else ""
    displayed_text = getattr(widget, "displayed_text", None)
    text = displayed_text() if callable(displayed_text) else source_text
    if not text:
        if not source_text or not callable(displayed_text):
            return None
        rect = widget.contentsRect()
        if isinstance(widget, QPushButton):
            option = QStyleOptionButton()
            option.initFrom(widget)
            rect = widget.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, widget)
        return dict(kind="elided-empty", text=text, source_text=source_text,
                    available=[rect.width(), rect.height()])

    def finding(**values):
        result = dict(values)
        if source_text != text:
            result["source_text"] = source_text
        return result
    fm = widget.fontMetrics()
    rect = widget.contentsRect()
    kind = "text"
    if isinstance(widget, widgets.Switch):
        width = widget.width() - widget.TRACK.width() - 14.5
        needed = fm.horizontalAdvance(text)
        return finding(kind="switch-ink", text=text, needed=needed, available=width,
                       excess=round(needed - width, 2)) if needed > width + 1 else None
    if isinstance(widget, widgets._Swatch):
        from PySide6.QtGui import QFontMetrics
        metrics = QFontMetrics(widget._face(widget.isChecked()))
        available = widget.height() - widget.chip_rect().bottom() - widget.NAME_GAP
        bounds = metrics.boundingRect(QRect(0, 0, widget.width(), 10000),
                                      int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), text)
        ink_height = bounds.height() - metrics.height() + metrics.tightBoundingRect(text).height()
        return finding(kind="swatch-ink", text=text, needed=ink_height, available=available,
                       excess=ink_height-available) if ink_height > available+2 else None
    if isinstance(widget, QLabel):
        rect.adjust(widget.margin(), widget.margin(), -widget.margin(), -widget.margin())
        indent = widget.indent()
        if indent > 0:
            rect.adjust(indent, 0, 0, 0)
        flags = int(widget.alignment())
        if widget.wordWrap():
            flags |= int(Qt.TextFlag.TextWordWrap)
        if widget.textFormat() == Qt.TextFormat.RichText or "<br" in text or "<a " in text:
            doc = QTextDocument()
            doc.setDefaultFont(widget.font())
            doc.setDocumentMargin(0)
            doc.setHtml(text)
            doc.setTextWidth(rect.width())
            height = doc.size().height()
            return finding(kind="rich-text-height", text=text, needed=height, available=rect.height(),
                           excess=round(height - rect.height(), 2)) if height > rect.height() + 2 else None
        bounds = fm.boundingRect(QRect(0, 0, rect.width(), 100000), flags, text)
        excess = max(bounds.height() - rect.height(), bounds.width() - rect.width())
        if excess > 2:
            # QFontMetrics includes leading; only rasterised ink outside the actual
            # text rectangle is a clipping candidate.
            pad = 256
            canvas = QImage(max(1, rect.width()+2*pad), max(1, rect.height()+2*pad), QImage.Format.Format_ARGB32)
            canvas.fill(Qt.GlobalColor.transparent)
            painter = QPainter(canvas)
            painter.setFont(widget.font())
            painter.setPen(QColor("white"))
            painter.drawText(QRect(pad, pad, rect.width(), rect.height()), flags | int(Qt.TextFlag.TextDontClip), text)
            painter.end()
            alpha = canvas.convertToFormat(QImage.Format.Format_Alpha8)
            data = bytes(alpha.constBits())
            stride = alpha.bytesPerLine()
            ink_points = []
            for y in range(alpha.height()):
                line = data[y*stride:y*stride+alpha.width()]
                nz = [x for x, value in enumerate(line) if value > 12]
                if nz:
                    ink_points.append((min(nz), max(nz), y))
            if ink_points:
                left = min(p[0] for p in ink_points)-pad
                right = max(p[1] for p in ink_points)-pad
                top = min(p[2] for p in ink_points)-pad
                bottom = max(p[2] for p in ink_points)-pad
                if left < -1 or right >= rect.width()+1 or top < -1 or bottom >= rect.height()+1:
                    return finding(kind="label-ink", text=text, ink=[left,top,right,bottom],
                                   ink_format="inclusive-left-top-right-bottom",
                                   ink_size=[right-left+1, bottom-top+1],
                                   available=[rect.width(), rect.height()], excess=excess)
    elif isinstance(widget, (QPushButton, QCheckBox)):
        option = QStyleOptionButton()
        option.initFrom(widget)
        option.text = text
        element = QStyle.SubElement.SE_CheckBoxContents if isinstance(widget, QCheckBox) else QStyle.SubElement.SE_PushButtonContents
        rect = widget.style().subElementRect(element, option, widget)
        needed = max(fm.horizontalAdvance(line.replace("&", "")) for line in text.splitlines())
        if needed > rect.width() + 2:
            return finding(kind="button-ink", text=text, needed=needed, available=rect.width(), excess=needed-rect.width())
    return None


def measure(root):
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtWidgets import QWidget, QScrollArea, QLabel, QAbstractButton
    checks = []
    for w in root.findChildren(QWidget):
        if not visible(w, root):
            continue
        found = text_measure(w)
        origin = w.mapTo(root, QPoint(0, 0))
        info = dict(widget=w.metaObject().className(), name=w.objectName(),
                    rect=[origin.x(), origin.y(), w.width(), w.height()])
        if found:
            checks.append(dict(info, **found))
        parent = w.parentWidget()
        if parent and not isinstance(parent, QScrollArea) and parent.objectName() != "qt_scrollarea_viewport":
            box = QRect(w.pos(), w.size())
            outside = box.intersected(parent.rect()) != box
            if outside and w.width() > 2 and w.height() > 2:
                checks.append(dict(info, kind="outside-parent", parent=parent.metaObject().className(),
                                   parent_size=[parent.width(), parent.height()]))
    for scroll in root.findChildren(QScrollArea):
        if not visible(scroll, root):
            continue
        if scroll.horizontalScrollBar().maximum() > 0:
            checks.append(dict(kind="sideways-scroll", maximum=scroll.horizontalScrollBar().maximum()))
    by_parent = {}
    for child in root.findChildren(QWidget):
        if visible(child, root) and isinstance(child, (QLabel, QAbstractButton)):
            by_parent.setdefault(child.parentWidget(), []).append(child)
    for siblings in by_parent.values():
        for i, first in enumerate(siblings):
            for second in siblings[i+1:]:
                overlap = first.geometry().intersected(second.geometry())
                if overlap.width() > 2 and overlap.height() > 2:
                    origin = first.parentWidget().mapTo(root, overlap.topLeft())
                    checks.append(dict(kind="sibling-overlap", first=first.text(), second=second.text(),
                                       overlap=[overlap.x(),overlap.y(),overlap.width(),overlap.height()],
                                       rect=[origin.x(),origin.y(),overlap.width(),overlap.height()]))
    return checks


def layout_snapshot(root, window):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QWidget, QScrollArea, QGridLayout

    def size(value):
        return [value.width(), value.height()]

    def rect(value):
        return [value.x(), value.y(), value.width(), value.height()]

    def layout_tree(layout):
        if layout is None:
            return None
        children = []
        for index in range(layout.count()):
            item = layout.itemAt(index)
            widget = item.widget()
            child = dict(index=index, geometry=rect(item.geometry()), size_hint=size(item.sizeHint()),
                         minimum_size=size(item.minimumSize()), maximum_size=size(item.maximumSize()),
                         widget_class=type(widget).__name__ if widget else None,
                         object_name=widget.objectName() if widget else None,
                         alignment=int(item.alignment()))
            if isinstance(layout, QGridLayout):
                child["grid_position"] = list(layout.getItemPosition(index))
            if item.layout():
                child["layout"] = layout_tree(item.layout())
            children.append(child)
        margins = layout.contentsMargins()
        return dict(class_name=type(layout).__name__, geometry=rect(layout.geometry()),
                    size_hint=size(layout.sizeHint()), minimum_size=size(layout.minimumSize()),
                    maximum_size=size(layout.maximumSize()), spacing=layout.spacing(),
                    margins=[margins.left(), margins.top(), margins.right(), margins.bottom()], children=children)

    def widget_tree(widget):
        origin = widget.mapTo(window, QPoint())
        policy = widget.sizePolicy()
        record = dict(class_name=type(widget).__name__, object_name=widget.objectName(),
                      visible=widget.isVisibleTo(window), geometry=rect(widget.geometry()),
                      window_rect=[origin.x(), origin.y(), widget.width(), widget.height()],
                      size_hint=size(widget.sizeHint()), minimum_size_hint=size(widget.minimumSizeHint()),
                      minimum_size=size(widget.minimumSize()), maximum_size=size(widget.maximumSize()),
                      size_policy=[policy.horizontalPolicy().name, policy.verticalPolicy().name],
                      has_height_for_width=widget.hasHeightForWidth(),
                      height_for_width=widget.heightForWidth(widget.width()), layout=layout_tree(widget.layout()))
        if isinstance(widget, QScrollArea):
            record["scroll"] = dict(viewport=rect(widget.viewport().geometry()),
                                    horizontal=[widget.horizontalScrollBar().value(), widget.horizontalScrollBar().maximum()],
                                    vertical=[widget.verticalScrollBar().value(), widget.verticalScrollBar().maximum()])
        record["children"] = [widget_tree(child) for child in widget.children() if isinstance(child, QWidget)]
        return record

    return widget_tree(root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=["windows", "linux-x11", "linux-wayland"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--actions", action="store_true")
    parser.add_argument("--states", help="Comma-separated fixture names; omit for the full matrix")
    parser.add_argument("--sizes", help="Comma-separated logical sizes, e.g. 640x600,1000x800")
    parser.add_argument("--record-layout", action="store_true", help="Include geometry-only Connection layout snapshots")
    args = parser.parse_args()
    if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
        raise SystemExit("Refusing to run without QT_QPA_PLATFORM=offscreen")
    args.output.mkdir(parents=True, exist_ok=True)
    fake_root = args.output / "fixtures"
    fake_root.mkdir(exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="settings-", dir=fake_root)
    settings_path = Path(temporary.name) / "settings.json"
    if args.platform.startswith("linux-"):
        os.environ["XDG_SESSION_TYPE"] = args.platform.removeprefix("linux-")
    os.environ["XDG_CONFIG_HOME"] = str(settings_path.parent / "xdg-config")
    os.environ["XDG_STATE_HOME"] = str(settings_path.parent / "xdg-state")
    def guard(event, values):
        if event in ("socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg"):
            raise RuntimeError(f"Network operation forbidden in UI audit: {event}")
        if event in ("subprocess.Popen", "os.system"):
            raise RuntimeError(f"OS process forbidden in UI audit: {event}")
        if event == "open" and values and isinstance(values[0], (str, bytes, os.PathLike)):
            p = Path(os.fsdecode(values[0]))
            if p.name.lower() in ("settings.json", "config.json"):
                if not p.resolve().is_relative_to(fake_root.resolve()):
                    raise RuntimeError(f"Forbidden settings access: {p}")
    sys.addaudithook(guard)
    from PySide6.QtCore import QPoint, QSize, Qt, qVersion, QCoreApplication, QEvent
    from PySide6.QtGui import QDesktopServices, QFontInfo
    from PySide6.QtWidgets import QApplication, QWidget, QSystemTrayIcon, QLabel, QMessageBox, QStyle, QStyleOptionMenuItem
    import app_config
    import kvm_bridge_win as kvm
    import theme
    import motion
    import widgets
    from core import protocol
    from core.receiver import ServerState
    import linux_stand_ins
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    theme.init_fonts()
    app.setFont(theme.font(theme.TYPE["body"]))
    metadata = dict(native_platform=sys.platform, requested_platform=args.platform,
                    session_type=os.environ.get("XDG_SESSION_TYPE"), qt_version=qVersion(),
                    qt_platform=app.platformName(), qt_scale_factor=os.environ.get("QT_SCALE_FACTOR", "1.0"),
                    qt_device_pixel_ratio=app.devicePixelRatio(),
                    body_font=dict(requested=app.font().family(), resolved=QFontInfo(app.font()).family(),
                                   pixel_size=app.font().pixelSize(), point_size=app.font().pointSizeF()),
                    sans_font=theme.sans(), mono_font=theme.mono(),
                    parts={name: getattr(kvm.platform_parts, name).__name__
                           if hasattr(getattr(kvm.platform_parts, name), "__name__") else "stand-in"
                           for name in kvm.platform_parts.PARTS})
    branch_platform = "win32" if args.platform == "windows" else "linux"
    kvm.sys = SimpleNamespace(platform=branch_platform, executable=sys.executable, frozen=False)
    kvm.platform_parts.WAYLAND = args.platform == "linux-wayland"
    kvm.OWN_PLATFORM = "windows" if args.platform == "windows" else "linux"
    if args.platform != "windows":
        kvm.firewall_win = linux_stand_ins.firewall
    app_config.default_config_path = lambda: settings_path
    kvm.default_config_path = lambda: settings_path
    kvm.local_address_towards = lambda *_: "192.0.2.1"
    kvm.pairing.local_address_towards = lambda *_: "192.0.2.1"
    kvm.pairing.pairing_address = lambda: "192.0.2.1"
    kvm.hardware_win.hardware_address_towards = lambda *_: ""
    kvm.autostart_win.is_enabled = lambda: False
    kvm.autostart_win.installed_exe = lambda: str(settings_path.parent / "Beamer-audit.exe")
    kvm.platform_parts.injector.stop = lambda: None
    theme.watch_system = lambda *_: None
    motion.fade_from = lambda *_args, **_kw: None
    motion.snapshot = lambda *_: None
    motion.set_shown = lambda w, shown: w.setVisible(shown)
    motion.should_animate = lambda *_: False
    effects_log = []
    kvm.autostart_win.set_enabled = lambda *a: effects_log.append(["autostart", bool(a[0])])
    QDesktopServices.openUrl = lambda url: effects_log.append(["url", url.toString()]) or True
    QSystemTrayIcon.show = lambda *_: None
    QSystemTrayIcon.showMessage = lambda _self, title, message, *_: effects_log.append(["notification", title, message])
    app.beep = lambda: None
    kvm.WindowsApplication._make_pairing = lambda *_: FakePairing()
    kvm.WindowsApplication._apply_title_bar = lambda *_: None
    kvm.WindowsApplication._check_full_screen = lambda *_: None
    kvm.WindowsApplication._run_firewall = lambda *_: effects_log.append(["firewall"])
    kvm.WindowsApplication._run_previews = lambda *_: None
    kvm.WindowsApplication._effect_overlay = lambda *_: None
    kvm.WindowsApplication._switch_overlay = lambda *_: None
    kvm.WindowsApplication._start_receiver = lambda *_: effects_log.append(["receiver"])
    kvm.WindowsApplication._start_sending = lambda *_: effects_log.append(["sender"])
    kvm.WindowsApplication._stop_sending = lambda *_: effects_log.append(["sender-off"])
    kvm.LOG_PATH = settings_path.parent / "Beamer.log"
    sizes = [(640, 600), (1000, 800), (1600, 1000)]
    states = [("none", 0, False, False, False), ("one-short", 1, False, False, False),
              ("one-long", 1, False, True, False), ("three-long", 3, False, True, False),
              ("five-long", 5, False, True, False), ("phone", 1, True, True, False),
              ("three-connected", 3, False, True, True), ("five-connected", 5, False, True, True),
              ("one-light", 1, False, True, False), ("three-all-ways", 3, False, True, True),
              ("five-shared-edge", 5, False, True, True)]
    if args.quick:
        sizes = sizes[:1]
        states = [states[2], states[4]]
    if args.states:
        requested = set(args.states.split(","))
        states = [state for state in states if state[0] in requested]
        if {state[0] for state in states} != requested:
            raise SystemExit("Unrecognised fixture name in --states")
    if args.sizes:
        sizes = [tuple(int(n) for n in value.split("x")) for value in args.sizes.split(",")]
        if any(len(size) != 2 or min(size) <= 0 for size in sizes):
            raise SystemExit("Sizes must be positive WIDTHxHEIGHT values")
    records = []
    scale = os.environ.get("QT_SCALE_FACTOR", "1.0")
    current_case = {}
    metadata.update(selected_wayland=kvm.platform_parts.WAYLAND, sizes=sizes,
                    states=[state[0] for state in states], actions=args.actions,
                    animations=False, operating_system_effects="mocked", network_effects="mocked")

    def settle(window):
        for _ in range(5):
            window.layout().activate()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
            app.processEvents()

    def capture(window, page, size, state, suffix=""):
        settle(window)
        safe_page = "".join(c if c.isalnum() or c in "-_." else "-" for c in page)
        filename = f"{safe_page}-{size[0]}x{size[1]}-{scale}-{state}{suffix}.png"
        path = args.output / filename
        bitmap = window.grab()
        if bitmap.isNull() or not bitmap.save(str(path)):
            raise RuntimeError(f"Could not save offscreen render: {path}")
        flagged = measure(window)
        record = dict(platform=args.platform, page=page, size=list(size), scale=scale, state=state,
                      path=str(path), actual_size=[window.width(), window.height()],
                      pixels=[bitmap.width(), bitmap.height()], device_pixel_ratio=bitmap.devicePixelRatio(), flags=flagged)
        if args.record_layout and page == "connection":
            record["layout_snapshot"] = layout_snapshot(window.stack.currentWidget(), window)
        records.append(record)
        return path

    for state, count, phone, long, connected in states:
        entries = fixtures(count, phone, long)
        settings = app_config.migrate(None, machine_id=protocol.id_text(bytes([200])*16))
        settings.update(peers=entries, zones=[], appearance="light" if state.endswith("light") else "dark", hide_addresses=False,
                        trigger_key="f13", trigger_style="double_tap", ignored_inputs=["key:135", "button:middle"])
        app_config.write_settings(settings_path, settings)
        if state.endswith("all-ways") or state.endswith("shared-edge"):
            for i, entry in enumerate(entries):
                side = ("right", "left", "top", "bottom", "right")[i]
                if state.endswith("shared-edge") and i == 1:
                    side = "right"
                methods = ["part", "corner"] if i < 3 else ["part"]
                app_config.set_ways(settings_path, entry["id"], side=side, methods=methods,
                                    parts=[("start", "middle", "end")[i%3]],
                                    corner=("top_right", "top_left", "bottom_left")[i%3])
        app_config.load_config(settings_path, unpaired_ok=True)
        window = kvm.WindowsApplication(settings_path)
        window.refresh_timer.stop()
        window.full_screen_timer.stop()
        window.update_checker.check_now = lambda: effects_log.append(["update-check"])
        window.update_checker.stop = lambda: None
        window.hooks.start = lambda *_: None
        window.hooks.stop = lambda *_: None
        window.sender.start = lambda *_: None
        window.sender.stop = lambda *_: None
        window.sender.refresh = lambda *_: None
        window.sender.links.up = lambda *_: connected
        window.sender.links.status = lambda *_: ""
        window.sender.links.kind = lambda *_: "connected" if connected else "unreachable"
        window.sender.go = lambda peer: effects_log.append(["go", protocol.id_text(peer)]) or True
        window.sender.send_to = lambda *_: True
        window.server.send_to = lambda *_: True
        window.server.peers_changed = lambda *_: None
        window._send_to = lambda *_: True
        window._status = ServerState.CONNECTED if connected else (ServerState.WAITING if entries else ServerState.ERROR)
        window._status_detail = ("Connected to " + entries[0]["name"]) if connected else "Waiting for localhost-live.example.lan"
        window._on_status(window._status, window._status_detail)
        window._host = "localhost-live.example.lan"
        window.host_readout.setText(window._host)
        firewall_status = kvm.firewall_win.FirewallStatus(24820, True, False, ("Private",), ("Private",), True, (), False)
        window._on_firewall(firewall_status)
        if state == "five-shared-edge":
            window._choose_machine(entries[1]["id"])
        window._refresh_window()
        window.show()
        for size in sizes:
            window.resize(*size)
            settle(window)
            for page in kvm.pages_win.KEYS:
                window._select_page(page)
                settle(window)
                scroll = window.stack.currentWidget()
                scroll.verticalScrollBar().setValue(0)
                capture(window, page, size, state)
                maximum = scroll.verticalScrollBar().maximum()
                step = max(1, scroll.viewport().height() - 40)
                for y in range(step, maximum + step, step):
                    scroll.verticalScrollBar().setValue(min(y, maximum))
                    capture(window, page, size, state, f"-scroll{min(y, maximum)}")
                scroll.verticalScrollBar().setValue(0)
            window._select_page("overview")
            for row in window.machines.rows.values():
                row.directions.setChecked(True)
                row._ask(True)
            capture(window, "remove-confirmation", size, state)
            for row in window.machines.rows.values():
                row._ask(False)
            capture(window, "directions", size, state)
            window._pairing_open = True
            window.sheet.setVisible(True)
            window.machines.set_pairing_open(True)
            window.pairing.discovered = [dict(name=e["name"], address=e.get("host") or "192.0.2.99", platform=e["platform"],
                                              pairing=kvm.pairing.PAIRING_V3, pair_id="fixture") for e in entries]
            window._refresh_pairing()
            sheet_size = QSize(max(100, window.sheet.width()), max(100, window.sheet.sizeHint().height()))
            window.sheet.resize(sheet_size)
            for mode in ("discovery", "code", "error", "working", "expired", "success"):
                if mode == "code":
                    window._show_code()
                elif mode == "error":
                    window.sheet.say_pair("The code did not match on localhost-live.example.lan. Show a fresh code there and try again.", "note-fault")
                elif mode == "working":
                    window.sheet.busy(True)
                elif mode == "expired":
                    window.sheet.busy(False)
                    window._cancel_code()
                    window.sheet.say_host("That code expired. Show a new code and enter it on the other machine.", "note-amber")
                elif mode == "success":
                    paired_name = entries[0]["name"] if entries else "Studio Mac"
                    window.sheet.say_pair(f"Paired with {paired_name}.", "note-live")
                settle(window)
                capture(window, f"pairing-{mode}", size, state)
                sheet_path = args.output / f"pairing-{mode}-panel-{size[0]}x{size[1]}-{scale}-{state}.png"
                window.sheet.grab().save(str(sheet_path))
                records.append(dict(platform=args.platform, page=f"pairing-{mode}-panel", size=list(size), scale=scale,
                                    state=state, path=str(sheet_path), flags=measure(window.sheet)))
            window._pairing_open = False
            window.sheet.setVisible(False)
            window.machines.set_pairing_open(False)
            window.tray_menu.ensurePolished()
            window.tray_menu.adjustSize()
            menu_path = args.output / f"tray-{size[0]}x{size[1]}-{scale}-{state}.png"
            window.tray_menu.grab().save(str(menu_path))
            menu_items = []
            menu_flags = []
            for action in window.tray_menu.actions():
                rect = window.tray_menu.actionGeometry(action)
                option = QStyleOptionMenuItem()
                window.tray_menu.initStyleOption(option, action)
                metrics = option.fontMetrics
                title = action.text().replace("&&", "\0").replace("&", "").replace("\0", "&")
                text_width = metrics.horizontalAdvance(title)
                required = window.tray_menu.style().sizeFromContents(QStyle.ContentsType.CT_MenuItem, option,
                                                                      QSize(text_width, metrics.height()), window.tray_menu)
                option.text = ""
                decoration = window.tray_menu.style().sizeFromContents(QStyle.ContentsType.CT_MenuItem, option,
                                                                        QSize(0, metrics.height()), window.tray_menu)
                available = max(0, rect.width() - decoration.width())
                item = dict(title=action.text(), rect=[rect.x(), rect.y(), rect.width(), rect.height()],
                            text_width=text_width, available_text_width=available, required_width=required.width(),
                            decoration_width=decoration.width(), checkable=action.isCheckable(), submenu=action.menu() is not None,
                            visible=action.isVisible())
                menu_items.append(item)
                if action.isVisible() and not rect.isEmpty() and not action.isSeparator() and text_width > available + 2:
                    menu_flags.append(dict(kind="menu-title-ink", text=action.text(), needed=text_width,
                                           available=available, rect=item["rect"]))
            screen = app.primaryScreen().availableGeometry()
            if window.tray_menu.width() > screen.width() or window.tray_menu.height() > screen.height():
                menu_flags.append(dict(kind="menu-exceeds-offscreen-screen", menu_size=[window.tray_menu.width(), window.tray_menu.height()],
                                       screen=[screen.width(), screen.height()]))
            records.append(dict(platform=args.platform, page="tray", size=list(size), scale=scale, state=state,
                                path=str(menu_path), menu_size=[window.tray_menu.width(), window.tray_menu.height()],
                                screen=[app.primaryScreen().geometry().width(), app.primaryScreen().geometry().height()],
                                screen_is_offscreen=True, menu_items=menu_items, flags=menu_flags))
            if state == "five-shared-edge":
                window._select_page("crossing")
                for part, button in window.share_tiles.items():
                    if window.share_module.isVisibleTo(window):
                        button.click()
                        capture(window, f"share-{part}", size, state)
                if window.share_module.isVisibleTo(window) and window.share_button.isEnabled():
                    window.share_button.click()
                    capture(window, "share-apply", size, state)
            if state == "none":
                window._select_page("connection")
                original_save = kvm.app_config.write_settings
                original_critical = QMessageBox.critical
                dialogs = []
                def critical(parent, title, message):
                    dialog = QMessageBox(QMessageBox.Icon.Critical, title, message, QMessageBox.StandardButton.Ok, parent)
                    dialog.setWindowModality(Qt.WindowModality.NonModal)
                    dialog.show()
                    settle(window)
                    path = args.output / f"save-failed-{size[0]}x{size[1]}-{scale}-{state}.png"
                    dialog.grab().save(str(path))
                    records.append(dict(platform=args.platform,page="save-failed",size=list(size),scale=scale,state=state,
                                        path=str(path),flags=measure(dialog)))
                    dialogs.append(dialog)
                    return QMessageBox.StandardButton.Ok
                def failed_write(*_args, **_kw):
                    raise OSError("Audit fixture: this settings folder is read-only. No settings were changed.")
                QMessageBox.critical = critical
                kvm.app_config.write_settings = failed_write
                try:
                    window.save_button.click()
                finally:
                    kvm.app_config.write_settings = original_save
                    QMessageBox.critical = original_critical
                    for dialog in dialogs:
                        dialog.close()
        if args.actions and state == "three-long":
            sys.path.insert(0, str(Path(__file__).parent))
            from qt_actions import exercise
            def action_snapshot(name):
                window.resize(*sizes[0])
                settle(window)
                capture(window, name, sizes[0], "action")
                for dialog in app.topLevelWidgets():
                    if dialog is not window and isinstance(dialog, QMessageBox):
                        dialog_path = args.output / f"dialog-{name}-{scale}.png"
                        dialog.grab().save(str(dialog_path))
                        records.append(dict(platform=args.platform,page=f"dialog-{name}",size=[dialog.width(),dialog.height()],
                                            scale=scale,state="action",path=str(dialog_path),flags=measure(dialog)))
            actions = exercise(window, app, snapshot=action_snapshot)
            (args.output / f"actions-{scale}.json").write_text(json.dumps(actions, indent=2), encoding="utf-8")
        window._closing = True
        window.tray.hide()
        window.hide()
        window.deleteLater()
        app.processEvents()
    (args.output / f"measurements-{scale}.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    (args.output / f"effects-{scale}.json").write_text(json.dumps(effects_log, indent=2), encoding="utf-8")
    (args.output / f"metadata-{scale}.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(dict(platform=args.platform, scale=scale, renders=len(records),
                          flagged=sum(bool(r["flags"]) for r in records), flags=sum(len(r["flags"]) for r in records))))
    temporary.cleanup()


if __name__ == "__main__":
    main()
