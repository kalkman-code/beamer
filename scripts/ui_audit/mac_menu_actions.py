"""Invoke the production tray callbacks without a status item or OS side effects."""
import contextlib
import json
import logging
from types import SimpleNamespace
from unittest import mock

from mac_render import ROOT, AppKit, app, fixture, peers, settle
from mac_states import FakePairing
from mac_actions import public_settings


def menu_items(menu, prefix="tray"):
    for item in list(menu.values()):
        if getattr(item, "hidden", False) or not hasattr(item, "title"):
            continue
        path = prefix + "/" + str(item.title)
        yield path, item
        if hasattr(item, "values"):
            yield from menu_items(item, path)


def main():
    actions = []
    with contextlib.ExitStack() as stack:
        for target, name in ((app.keyboard_layout, "watch"), (app, "GestureOverlay"), (app, "EdgeGlow"),
                             (app, "NotchIsland"), (app, "NotchBeam"), (app.effects_overlay, "EffectsOverlay"),
                             (app, "Haptics"), (app, "WindowsInput"), (app.updates, "Checker"),
                             (app.rumps, "Timer"), (app.notices, "Notices"), (app.rumps, "quit_application"),
                             (app, "show_about_panel")):
            stack.enter_context(mock.patch.object(target, name))
        workspace = mock.Mock()
        stack.enter_context(mock.patch.object(AppKit, "NSWorkspace", SimpleNamespace(sharedWorkspace=lambda: workspace)))
        stack.enter_context(mock.patch.object(AppKit, "NSBeep"))
        stack.enter_context(mock.patch.object(app, "LOG_DIRECTORY", ROOT / "renders/mac/synthetic-logs"))
        service = FakePairing()
        service.start, service.stop = mock.Mock(), mock.Mock()
        stack.enter_context(mock.patch.object(app.pairing, "PairingService", return_value=service))
        stack.enter_context(mock.patch.object(app, "accessibility_granted", return_value=True))
        stack.enter_context(mock.patch.object(app, "input_monitoring_granted", return_value=True))
        for count in (0, 1, 3, 5):
            control = fixture(peers(count), True)
            control.controller.stop = mock.Mock()
            tray = app.TrayApp(control.controller, control.settings_store, logging.getLogger("audit-menu"), hidden=True)
            tray.control_window.show = mock.Mock()
            tray.measure_notch = mock.Mock()
            tray.refresh_status(None)
            for menu_path, item in menu_items(tray.menu):
                if not getattr(item, "callback", None):
                    continue
                title = item.title
                before = public_settings(tray.control_window)
                paused = tray.controller.crossing_paused
                redirected_before = tray.controller.redirecting
                on_before = tray.controller.owner.on
                expected = None
                if item is tray.send_item:
                    chosen = [p for p in before["peers"] if p["send"] or p["side"]]
                    expected = ("send", not all(p["send"] is True for p in chosen))
                elif item is tray.receive_item:
                    expected = ("allow_drive", not all(p["allow_drive"] is True for p in before["peers"]))
                try:
                    item.callback(item)
                    settle(tray.control_window)
                    after = public_settings(tray.control_window)
                    checks = []
                    if expected:
                        key, value = expected
                        applicable = [p for p in after["peers"] if key != "send" or p["side"]]
                        checks.append({"setting": key, "expected": value, "actual": [p[key] for p in applicable],
                                       "passed": all(p[key] == value for p in applicable)})
                    if item is tray.pause_item:
                        checks.append({"effect": "crossing_paused", "expected": not paused,
                                       "actual": tray.controller.crossing_paused, "passed": tray.controller.crossing_paused == (not paused)})
                    if item is tray.toggle_item and count:
                        checks.append({"effect": "redirecting", "expected": not redirected_before,
                                       "actual": tray.controller.redirecting, "passed": tray.controller.redirecting == (not redirected_before)})
                    defaults = getattr(item.callback, "__defaults__", None)
                    if defaults and defaults[-1] in {p["id"] for p in before["peers"]}:
                        peer = defaults[-1]
                        expected_on = app.protocol.id_text(tray.controller.identity()["id"]) if redirected_before and on_before == peer else peer
                        checks.append({"effect": "input owner", "expected": expected_on, "actual": tray.controller.owner.on,
                                       "passed": tray.controller.owner.on == expected_on})
                    actions.append({"state": count, "title": title, "path": menu_path, "result": "completed",
                                    "settings_before": before, "settings_after": after, "checks": checks})
                except Exception as exc:
                    actions.append({"state": count, "title": item.title, "result": "exception", "exception": repr(exc)})
            for index, page in enumerate(app.pages.PAGES):
                sender = SimpleNamespace(tag=lambda index=index: index)
                tray.control_window.showPage_(sender)
                actions.append({"state": count, "title": "View/" + page[1], "result": "completed",
                                "selected_page": tray.control_window.page})
            for parent in AppKit.NSApp.mainMenu().itemArray():
                for item in parent.submenu().itemArray():
                    if item.isSeparatorItem():
                        continue
                    selector = str(item.action() or "")
                    target = item.target()
                    if target is not None:
                        supported = bool(target.respondsToSelector_(selector))
                    else:
                        supported = any(obj.respondsToSelector_(selector) for obj in
                                        (AppKit.NSApp, tray.control_window.window, tray.control_window.host_field))
                    actions.append({"state": count, "title": str(parent.title()) + "/" + str(item.title()),
                                    "selector": selector, "result": "selector-present" if supported else "responder-chain-only",
                                    "note": "Native Edit/Hide/Close/Minimise effects are not invoked on Toby's desktop."})
    path = ROOT / "renders/mac/menu-actions.json"
    path.write_text(json.dumps(actions, indent=2))
    print(json.dumps({"menu_actions": len(actions), "exceptions": sum(row["result"] == "exception" for row in actions),
                      "checks": sum(len(row.get("checks", [])) for row in actions),
                      "mismatches": sum(not check["passed"] for row in actions for check in row.get("checks", []))}))


if __name__ == "__main__":
    main()
