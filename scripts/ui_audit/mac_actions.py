"""Drive Mac controls offscreen with all OS and network effects replaced by fakes."""
import contextlib
import copy
import json
import traceback
from types import SimpleNamespace
from unittest import mock

from mac_render import ROOT, AppKit, app, fixture, measure, peers, render, render_metadata, settle, widgets, walk
from mac_states import FakePairing, menus
from core import pairing


PUBLIC = ("appearance", "check_updates", "hide_addresses", "same_on_both", "trigger_key", "trigger_style", "double_tap_ms", "pointer_speed", "scroll_speed", "reverse_scroll", "key_map", "crossing", "ignored_inputs", "port")
SAVED = {
    "same_switch": "same_on_both", "updates_switch": "check_updates", "hide_switch": "hide_addresses",
    "reverse_scroll_box": "reverse_scroll", "appearance_select": "appearance", "modifier_select": "key_map",
    "style_select": "trigger_style", "hold_box": "crossing.hold_full_screen",
    "dragging_box": "crossing.block_while_dragging", "glow_box": "crossing.glow",
    "landing_box": "crossing.shortcut_arrival", "haptics_box": "crossing.haptics",
    "length_select": "crossing.effect_length", "size_select": "crossing.effect_size",
    "notch_style_select": "crossing.notch_style", "notch_after_select": "crossing.notch_after_ms",
    "tick_steps_select": "crossing.haptic_steps", "glow_style_select": "crossing.glow_style",
    "switch_style_select": "crossing.shortcut_arrival_style", "glow_colour_select": "crossing.glow_colour",
}


def public_settings(control):
    raw = control.settings_store.current()
    effective = app.config_to_raw(control.settings_store.load())
    result = {key: copy.deepcopy(effective[key]) for key in PUBLIC if key in effective}
    result["peers"] = [{key: copy.deepcopy(peer[key]) for key in ("id", "name", "in_use", "send", "allow_drive", "side", "jump_key") if key in peer} for peer in raw["peers"]]
    result["zones"] = [{key: copy.deepcopy(zone[key]) for key in ("peer", "kind", "edge", "parts", "corner", "off") if key in zone} for zone in raw.get("zones", [])]
    return result


def expected_selection(control, view):
    callback = getattr(view, "callback", None)
    owner = getattr(callback, "__self__", None)
    if owner is None:
        for cell in getattr(callback, "__closure__", None) or ():
            candidate = cell.cell_contents
            if isinstance(candidate, (widgets.Segmented, widgets.ChoiceTiles, widgets.Swatches)):
                owner = candidate
                break
    if isinstance(owner, (widgets.Switch, widgets.WayTile)):
        expected = not owner.value
    elif isinstance(owner, (widgets.Segmented, widgets.ChoiceTiles, widgets.Swatches)):
        defaults = getattr(callback, "__defaults__", None)
        if not defaults:
            return None
        expected = defaults[0]
    else:
        return None
    name = next((key for key, value in control.__dict__.items() if value is owner), None)
    if name is None:
        name = next((key for key, value in control.__dict__.items() if isinstance(value, widgets.Linked) and owner in value.rows), None)
    return owner, name, expected


def capture(control, page, output, state):
    document = control.pages[page].documentView()
    geometry = measure(document)
    paths = []
    for target, suffix in ((control.window.contentView(), ""), (document, "-full")):
        path = output / f"{page}{suffix}-640-1x-{state}.png"
        render(target, path, 1)
        paths.append(str(path))
    if geometry["flags"]:
        path = output / f"{page}-full-640-2x-{state}.png"
        render(document, path, 2)
        paths.append(str(path))
    return geometry["flags"], paths


def main():
    output = ROOT / "renders/mac"
    output.mkdir(parents=True, exist_ok=True)
    actions = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(app, "accessibility_granted", return_value=False))
        stack.enter_context(mock.patch.object(app, "input_monitoring_granted", return_value=False))
        workspace = mock.Mock()
        stack.enter_context(mock.patch.object(AppKit, "NSWorkspace", SimpleNamespace(sharedWorkspace=lambda: workspace)))
        # PyObjC cannot delete a Python replacement for an inherited selector on
        # restoration. These process-local fakes disappear when the runner exits.
        AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_ = mock.Mock(return_value=object())
        AppKit.NSEvent.removeMonitor_ = mock.Mock()
        stack.enter_context(mock.patch.object(app.ApplicationServices, "AXIsProcessTrustedWithOptions", return_value=True))
        stack.enter_context(mock.patch.object(app.Quartz, "CGRequestListenEventAccess", return_value=True))
        stack.enter_context(mock.patch.object(app.subprocess, "Popen"))
        login = {"on": False}
        stack.enter_context(mock.patch.object(app.login_item, "status", side_effect=lambda: app.login_item.ENABLED if login["on"] else app.login_item.NOT_FOUND))
        stack.enter_context(mock.patch.object(app.login_item, "set_enabled", side_effect=lambda on: login.update(on=on)))
        stack.enter_context(mock.patch.object(pairing, "pairing_address", return_value="192.0.2.20"))
        for page in ("overview", "permissions", "crossing", "keyboard", "design", "connection"):
            control = fixture(peers(5), True)
            control.quit_handler = mock.Mock()
            control.haptics = mock.Mock()
            control.update_checker = mock.Mock()
            control.panel.service = FakePairing()
            def direction(token, **changes):
                control.settings_store.set_peer(token, **changes)
                control.panel.refresh()
            control.direction_handler = direction
            control.has_notch = True
            control.controller.notch_range = (100, 200)
            control.controller.event_tap = object()
            control._select_page(page)
            control.window.setContentSize_((640, 540))
            control.windowDidResize_(None)
            control.refresh()
            control._reflect()
            settle(control)
            seen = set()
            for round_number in range(3):
                if page == "design" and round_number:
                    control.glow_box.value = True
                    control.landing_box.value = True
                    control.style_for_select.value = "switch" if round_number == 1 else "crossing"
                    control._reflect()
                    settle(control)
                views = list(walk(control.pages[page].documentView()))
                for view in views:
                    label = str(view.accessibilityLabel() or "")
                    if isinstance(view, AppKit.NSButton):
                        label = str(view.title())
                        action = str(view.action() or "")
                        target = view.target()
                        if not view.isEnabled() or target is None or not action:
                            continue
                        callback = lambda view=view, target=target, action=action: getattr(target, action.replace(":", "_"))(view)
                    elif isinstance(view, widgets.Pressable) and getattr(view, "enabled", True) and getattr(view, "callback", None):
                        callback = view.press
                    else:
                        continue
                    key = (page, id(view))
                    if key in seen:
                        continue
                    seen.add(key)
                    row = {"page": page, "label": label, "class": view.className(), "round": round_number}
                    selection = expected_selection(control, view)
                    row["settings_before"] = public_settings(control)
                    redirected_before = control.controller.redirecting
                    paused_before = control.controller.crossing_paused
                    try:
                        callback()
                        control._flush()
                        settle(control)
                        row["result"] = "completed"
                    except Exception as exc:
                        row["result"] = "exception"
                        row["exception"] = repr(exc)
                        row["traceback"] = traceback.format_exc()
                    row["settings_after"] = public_settings(control)
                    row["checks"] = []
                    if isinstance(view, AppKit.NSButton) and action == "toggleRedirect:":
                        row["checks"].append({"effect": "redirecting", "expected": not redirected_before,
                                              "actual": control.controller.redirecting, "passed": control.controller.redirecting == (not redirected_before)})
                    if isinstance(view, AppKit.NSButton) and action == "togglePause:":
                        row["checks"].append({"effect": "crossing_paused", "expected": not paused_before,
                                              "actual": control.controller.crossing_paused, "passed": control.controller.crossing_paused == (not paused_before)})
                    if selection:
                        owner, name, expected = selection
                        row["checks"].append({"control": name, "expected": expected, "actual": owner.value, "passed": owner.value == expected})
                        saved = SAVED.get(name)
                        if saved:
                            actual = row["settings_after"]
                            for part in saved.split("."):
                                actual = actual.get(part) if isinstance(actual, dict) else None
                            row["checks"].append({"setting": saved, "expected": expected, "actual": actual, "passed": actual == expected})
                        if name == "login_switch":
                            row["checks"].append({"effect": "login_item.set_enabled", "expected": expected, "actual": login["on"], "passed": login["on"] == expected})
                    row["flags"], row["renders"] = capture(control, page, output, f"action-{len(actions):03d}")
                    row["render_metadata"] = render_metadata(control)
                    actions.append(row)
            for recorder in (control.key_recorder, control.jump_recorder, control.ignored_recorder):
                recorder.cancel()
            print(f"{page}: {sum(r['page'] == page for r in actions)} controls", flush=True)
        control = fixture(peers(5), True)
        control.window.setContentSize_((640, 540))
        control.windowDidResize_(None)
        for ruler_name in ("pointer_ruler", "scroll_ruler", "resistance_ruler", "double_tap_ruler"):
            ruler = getattr(control, ruler_name)
            page = "keyboard" if ruler_name in ("pointer_ruler", "scroll_ruler") else "crossing"
            control._select_page(page)
            for fraction in (0, 0.5, 1):
                row = {"page": page, "label": ruler_name, "fraction": fraction, "round": 0,
                       "settings_before": public_settings(control)}
                try:
                    ruler.move_to(fraction)
                    control._flush()
                    settle(control)
                    row.update(result="completed", value=ruler.value)
                    after = public_settings(control)
                    setting = {"pointer_ruler": "pointer_speed", "scroll_ruler": "scroll_speed",
                               "resistance_ruler": "crossing.resistance_px", "double_tap_ruler": "double_tap_ms"}[ruler_name]
                    actual = after
                    for part in setting.split("."):
                        actual = actual.get(part) if isinstance(actual, dict) else None
                    expected = ruler.value / 100 if ruler_name in ("pointer_ruler", "scroll_ruler") else ruler.value
                    row["checks"] = [{"setting": setting, "expected": expected, "actual": actual, "passed": actual == expected}]
                except Exception as exc:
                    row.update(result="exception", exception=repr(exc))
                row["settings_after"] = public_settings(control)
                row["flags"], row["renders"] = capture(control, page, output, f"action-{len(actions):03d}")
                row["render_metadata"] = render_metadata(control)
                actions.append(row)
    (output / "actions.json").write_text(json.dumps(actions, indent=2))
    print(json.dumps({"actions": len(actions), "exceptions": sum(r["result"] == "exception" for r in actions), "selection_checks": sum(len(r.get("checks", [])) for r in actions), "mismatches": sum(not check["passed"] for r in actions for check in r.get("checks", []))}))


if __name__ == "__main__":
    main()
