"""Expanded offscreen states and native-menu measurements for mac_render.py."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mac_render import ROOT, AppKit, app, fixture, measure, peers, render, render_metadata, settle, settings_store, theme, widgets
from core import pairing


class FakePairing:
    def __init__(self, heard=5, code=None):
        self.code, self.seconds_left, self.tcp_port = code, 179, 24820
        self.error, self.tcp_error, self.outcome, self.known = None, None, None, None
        self.heard = [{"name": p["name"], "platform": p["platform"], "address": p["host"],
                       "port": 24820, "pairing": pairing.PAIRING_V3, "pair_id": bytes([i + 1]) * 16,
                       "id": bytes([i + 1]) * 16} for i, p in enumerate(peers(heard))]

    def machines(self):
        return self.heard

    def qr_text(self, address):
        return "beamer://pair?code=123456" if address and not self.tcp_error else None

    def begin_pairing(self):
        self.code = "123456"

    def cancel_pairing(self):
        self.code = None

    def pair(self, *args):
        raise pairing.PairingError(pairing.ERROR_NOT_PAIRING)

    pair_by_address = pair


def capture(control, page, state, results):
    control._select_page(page)
    for width in (int(theme.MIN_WINDOW[0]), 900, 1200, 1600, 1900):
        control.window.setContentSize_((width, int(theme.MIN_WINDOW[1]) if width == int(theme.MIN_WINDOW[0]) else 900))
        control.windowDidResize_(None)
        settle(control)
        for name, view in ((page, control.window.contentView()), (page + "-full", control.pages[page].documentView())):
            measured = measure(view)
            for scale in (1, 2):
                path = ROOT / "renders/mac" / f"{name}-{width}-{scale}x-{state}.png"
                render(view, path, scale)
                results.append({"page": name, "state": state, "width": width, "scale": scale, "path": str(path), **render_metadata(control), **measured})


def menus(control):
    import rumps
    tray = object.__new__(app.TrayApp)
    tray.controller, tray.control_window, tray.settings_store = control.controller, control, control.settings_store
    tray._send_keys = []
    tray.toggle_item = rumps.MenuItem(app.TOGGLE_ITEM, callback=lambda _: None)
    tray.send_item = rumps.MenuItem("This Mac drives other machines", callback=lambda _: None)
    tray.receive_item = rumps.MenuItem("Other machines drive this Mac", callback=lambda _: None)
    tray._menu = rumps.rumps.Menu()
    tray.menu = [rumps.MenuItem(f"Beamer {app.VERSION}"), None, rumps.MenuItem(app.link_state.describe(control.controller).word),
                           tray.toggle_item, rumps.MenuItem("Resume crossing" if control.controller.crossing_paused else "Pause crossing"), None, tray.send_item,
                           tray.receive_item, None, rumps.MenuItem("Settings…"), rumps.MenuItem("Reload configuration"),
                           rumps.MenuItem("Open log folder"), None, rumps.MenuItem("About Beamer"), rumps.MenuItem("Quit Beamer")]
    if control.controller.peer_label:
        tray.toggle_item.title = "Return input to Mac" if control.controller.redirecting else f"Send input to {control.controller.peer_label}"
    tray._machine_items()
    tray._direction_items()
    app.install_main_menu(control)
    roots = [("tray", tray.menu._menu), ("main", AppKit.NSApp.mainMenu())]
    output = []

    def inspect(name, menu):
        menu.update()
        width = menu.size().width
        font = menu.font() or AppKit.NSFont.menuFontOfSize_(0)
        screen = min(s.visibleFrame().size.width for s in AppKit.NSScreen.screens())
        for item in menu.itemArray():
            if item.isSeparatorItem():
                continue
            ink = AppKit.NSAttributedString.alloc().initWithString_attributes_(item.title(), {AppKit.NSFontAttributeName: font}).size().width
            shortcut = str(item.keyEquivalent())
            reserve = 32 + (24 if item.submenu() is not None else 0) + (44 if shortcut else 0)
            output.append({"menu": name, "title": str(item.title()), "ink_width": round(ink, 2),
                           "font": str(font.fontName()), "font_size": font.pointSize(),
                           "menu_width": width, "minimum_screen_width": screen, "reserve": reserve,
                           "submenu": bool(item.submenu()), "overflow_screen": ink + reserve > screen,
                           "possible_menu_clip": width > 0 and ink + reserve > width + 2,
                           "ellipsis": "…" in str(item.title())})
            if item.submenu() is not None:
                inspect(name + "/" + str(item.title()), item.submenu())

    for name, menu in roots:
        inspect(name, menu)
    return output


def main():
    output = ROOT / "renders/mac"
    output.mkdir(parents=True, exist_ok=True)
    results, menu_results = [], []
    with mock.patch.object(app, "accessibility_granted", return_value=True), mock.patch.object(app, "input_monitoring_granted", return_value=True), mock.patch.object(pairing, "pairing_address", return_value="192.0.2.20"):
        control = fixture(peers(5), True)
        control.panel.directions_open = {p["token"] for p in peers(5)}
        control.panel.refresh()
        capture(control, "overview", "5-directions", results)
        control.panel.removing = peers(5)[1]["token"]
        control.panel.refresh()
        capture(control, "overview", "5-remove-long", results)
        for mode in ("heard", "showing", "no-qr", "error", "older"):
            control = fixture(peers(1), True)
            control.panel.open = True
            service = FakePairing(code="123456" if mode in ("showing", "no-qr") else None)
            if mode == "no-qr":
                service.tcp_error = "Another application is already listening on this port; a very long translated error explanation."
            if mode == "error":
                service.heard, service.error = [], "The local network could not be reached; another application denied multicast service discovery."
            if mode == "older":
                for item in service.heard:
                    item["pairing"] = "old"
            control.panel.service = service
            control.panel.refresh()
            capture(control, "overview", "pair-" + mode, results)
        control = fixture(peers(5), True)
        control._machine_picked(peers(5)[1]["id"])
        for tile in control.method_boxes.values():
            tile.value = True
        control.has_notch = True
        control._reflect()
        control.jump_recorder.set_value("ctrl+alt+shift+cmd+f12")
        control._add_ignored("key:122")
        capture(control, "crossing", "all-ways-long-jump", results)
        capture(control, "keyboard", "ignored-added", results)
        control.host_field.setStringValue_("localhost-live.example.lan")
        capture(control, "connection", "long-host", results)
        control.show_update(("1.5.0-rc.999-long-translated-version", "https://example.invalid"))
        capture(control, "overview", "update-long-version", results)
        for count in (0, 1, 3, 5):
            for connected in (False, True):
                control = fixture(peers(count), connected)
                for row in menus(control):
                    menu_results.append({"state": f"{count}-{'connected' if connected else 'disconnected'}", **row})
        control = fixture(peers(1), False)
        with mock.patch.object(app, "accessibility_granted", return_value=False), mock.patch.object(app, "input_monitoring_granted", return_value=False):
            control.refresh()
            capture(control, "permissions", "denied", results)
        control.granted_at_launch = False
        control.refresh()
        capture(control, "permissions", "relaunch", results)
        for appearance in ("light", "dark"):
            control = fixture(peers(5), True, appearance=appearance)
            for page in control.pages:
                capture(control, page, "5-" + appearance, results)
        long = peers(1)
        long[0]["name"] = "A very long workstation name with translated length"[:app.protocol.MAX_NAME_CHARS]
        long[0]["host"] = "localhost-live.example.lan"
        control = fixture(long, True)
        for page in ("overview", "crossing", "connection"):
            capture(control, page, "1-long-name-host", results)
        for row in menus(control):
            menu_results.append({"state": "1-long-name-host", **row})
        for mode in ("sending", "receiving", "paused", "token-refused", "older"):
            control = fixture(long, True)
            if mode == "sending":
                control.controller.owner.on = long[0]["id"]
                control.controller.redirecting = True
            elif mode == "receiving":
                control.controller.driver = long[0]["id"]
                control.controller.receiving = True
            elif mode == "paused":
                control.controller.crossing_paused = True
            else:
                link = next(iter(control.controller.links.values()))
                link._live = False
                link.kind = "unauthenticated" if mode == "token-refused" else "older"
                link.status = "That machine refused the connection; remove this pairing and pair it again."
            control.refresh()
            capture(control, "overview", "long-" + mode, results)
            for row in menus(control):
                menu_results.append({"state": "long-" + mode, **row})
    (output / "state-measurements.json").write_text(json.dumps(results, indent=2))
    (output / "menu-measurements.json").write_text(json.dumps(menu_results, indent=2))
    print(json.dumps({"renders": len(results), "flagged": sum(bool(r["flags"]) for r in results), "menu_items": len(menu_results)}))


if __name__ == "__main__":
    main()
