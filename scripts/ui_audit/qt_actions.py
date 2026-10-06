"""Drive the real Qt controls after qt_audit has isolated all OS/network effects.

Only the caller's explicit fixture settings path is read. Reports whitelist public
settings fields, so synthetic pairing credentials never enter an audit report.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import time


def exercise(window, app, output=None, snapshot=None):
    """Return action results; optionally write JSON and call snapshot(name) after each.

    The caller must neutralise receiver/capture, pairing transport, autostart,
    firewall, update-checker, injector shutdown and QDesktopServices before calling.
    `snapshot` runs on the GUI thread with the current page and control state.
    """
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QKeyEvent, QMouseEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

    import app_config
    import widgets

    results = []
    signal_errors = []
    previous_hook = sys.excepthook
    public_fields = (
        "appearance", "check_updates", "hold_full_screen", "block_while_dragging",
        "hide_addresses", "same_on_both", "modifier_style", "pointer_speed",
        "scroll_speed", "reverse_scroll", "trigger_key", "trigger_style",
        "double_tap_ms", "crossing_resistance_px", "edge_glow", "shortcut_arrival",
        "shortcut_arrival_style", "glow_style", "glow_colour", "effect_length",
        "effect_size", "design_follow_peer", "ignored_inputs", "port",
    )

    def exception_hook(kind, value, trace):
        signal_errors.append(f"{kind.__name__}: {value}")

    def settings():
        data = app_config.load_settings(window.config_path)
        return {key: data.get(key) for key in public_fields}

    def settle():
        # Slider persistence waits 300ms; animations also alter control geometry.
        deadline = time.monotonic() + 0.36
        while time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(5)
        window._refresh_window()
        app.processEvents()

    def run(name, function, expected=None):
        before = settings()
        signal_errors.clear()
        row = {"action": name, "result": "pass"}
        try:
            function()
            settle()
            after = settings()
            row["changed"] = {key: after[key] for key in public_fields if before[key] != after[key]}
            if expected:
                wrong = {key: {"expected": value, "actual": after.get(key)}
                         for key, value in expected.items() if after.get(key) != value}
                if wrong:
                    row["result"] = "fail"
                    row["mismatch"] = wrong
            if signal_errors:
                row["result"] = "fail"
                row["exceptions"] = list(signal_errors)
            if snapshot:
                snapshot(re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-"))
        except Exception as exc:
            row["result"] = "fail"
            row["exception"] = f"{type(exc).__name__}: {exc}"
        results.append(row)

    def click(name, control, expected=None):
        if not control.isEnabled():
            results.append({"action": name, "result": "disabled"})
            return
        run(name, control.click, expected)

    def toggle(name, control, field=None):
        initial = control.isChecked()
        for value in (not initial, initial):
            expected = {field: value} if field else None
            click(f"{name}-{str(value).lower()}", control, expected)

    def choice(name, group, field=None):
        for value, button in list(group._buttons.items()):
            click(f"{name}-{value}", button, {field: value} if field else None)

    sys.excepthook = exception_hook
    try:
        for key, button in window.sidebar._buttons.items():
            click(f"sidebar-{key}", button)
            if window._page != key:
                results[-1].update(result="fail", mismatch={"selected_page": window._page})
        if isinstance(window.sidebar.foot, QPushButton):
            click("sidebar-website", window.sidebar.foot)

        window._select_page("overview")
        for name, field in (("updates_switch", "check_updates"),
                            ("hold_switch", "hold_full_screen"),
                            ("same_switch", "same_on_both"), ("logon_switch", None)):
            toggle(name, getattr(window, name), field)
        click("redirect", window.redirect_button)
        click("redirect-return", window.redirect_button)
        click("pause", window.pause_button)
        click("resume", window.pause_button)
        window._on_update(("9.9.9-audit", "https://example.invalid/audit-release"))
        click("update-download", window.update_button)
        run("tray-update-download", window.header_action.trigger)
        window._on_update(None)

        window._select_page("crossing")
        choice("crossing-machine", window.machine_choice)
        for value, box in window.way_boxes.items():
            toggle(f"way-{value}", box)
        choice("edge", window.edge_choice)
        choice("corner", window.corner_choice)
        for value, button in window.part_buttons.items():
            toggle(f"part-{value}", button)
        for value, button in window.share_tiles.items():
            if window.share_module.isVisibleTo(window):
                click(f"share-part-{value}", button)
        if window.share_module.isVisibleTo(window):
            click("share-edge", window.share_button)
        toggle("drag-protection", window.dragging_switch, "block_while_dragging")
        for value in (0, 25, window.resistance_slider.maximum()):
            run(f"resistance-{value}", lambda v=value: window.resistance_slider.setValue(v),
                {"crossing_resistance_px": value})
        choice("shortcut-style", window.trigger_style_choice, "trigger_style")
        click("double-tap-enable-for-slider", window.trigger_style_choice._buttons["double_tap"],
              {"trigger_style": "double_tap"})
        for value in (50, 500, 1000):
            run(f"double-tap-{value}", lambda v=value: window.double_tap_slider.setValue(v),
                {"double_tap_ms": value})
        for recorder_name in ("trigger_recorder", "jump_recorder"):
            recorder = getattr(window, recorder_name)
            click(f"{recorder_name}-arm", recorder)
            click(f"{recorder_name}-cancel", recorder)

        recorder = window.trigger_recorder
        for native, key in ((0x7B, Qt.Key.Key_F12), (0x41, Qt.Key.Key_A)):
            def record_trigger(native=native, key=key):
                recorder.click()
                original = recorder.hook_vk
                recorder.hook_vk = lambda virtual, scan: virtual
                try:
                    event = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier,
                                      0, native, 0, "a" if native == 0x41 else "")
                    recorder.eventFilter(recorder, event)
                finally:
                    recorder.hook_vk = original
                    recorder.cancel()
            run(f"shortcut-record-{native}", record_trigger)
        def record_jump():
            window.jump_recorder.click()
            event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_J,
                              Qt.KeyboardModifier.ControlModifier, "j")
            window.jump_recorder.eventFilter(window.jump_recorder, event)
            window.jump_recorder.cancel()
        run("jump-record-ctrl-j", record_jump)
        click("jump-clear", window.jump_clear)

        window._select_page("keyboard")
        choice("modifier", window.modifier_choice, "modifier_style")
        for field, slider in window.speed_sliders.items():
            for value in (25, 100, 400):
                run(f"{field}-{value}", lambda s=slider, v=value: s.setValue(v), {field: value / 100})
        toggle("reverse-scroll", window.reverse_scroll_switch, "reverse_scroll")
        click("ignored-arm", window.ignored_recorder)
        click("ignored-cancel", window.ignored_recorder)
        def record_mouse():
            recorder = window.ignored_recorder
            recorder.click()
            event = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(1, 1), QPointF(1, 1),
                                Qt.MouseButton.BackButton, Qt.MouseButton.BackButton,
                                Qt.KeyboardModifier.NoModifier)
            recorder.eventFilter(recorder, event)
            recorder.cancel()
        run("ignored-record-back-button", record_mouse)
        for index in range(window.ignored_list.count() - 1, -1, -1):
            row_widget = window.ignored_list.itemAt(index).widget()
            if row_widget is not None:
                for button in row_widget.findChildren(QPushButton):
                    if button.accessibleName().startswith("Remove "):
                        click(f"ignored-{button.accessibleName()}", button)

        window._select_page("design")
        if not window.glow_toggle.isChecked():
            click("animate-enable-for-controls", window.glow_toggle, {"edge_glow": True})
        toggle("animate", window.glow_toggle, "edge_glow")
        if not window.glow_toggle.isChecked():
            click("animate-enable", window.glow_toggle, {"edge_glow": True})
        toggle("landing", window.landing_toggle, "shortcut_arrival")
        for mode, group, field in (("crossing", window.glow_style_choice, "glow_style"),
                                   ("switch", window.switch_style_choice, "shortcut_arrival_style")):
            click(f"design-mode-{mode}", window.design_mode_choice._buttons[mode])
            for title, tiles in group.groups.items():
                for value, tile in tiles._tiles.items():
                    def tile_press(tile=tile):
                        event = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(4, 4), QPointF(4, 4),
                                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                            Qt.KeyboardModifier.NoModifier)
                        tile.mousePressEvent(event)
                    run(f"{mode}-tile-{value}", tile_press, {field: value})
        choice("preview-place", window.effect_method_choice)
        choice("length", window.length_choice, "effect_length")
        choice("size", window.size_choice, "effect_size")
        for value, swatch in window.glow_colour_choice._swatches.items():
            click(f"colour-{value}", swatch, {"glow_colour": value})
        for index in range(window.design_follow_choice.count()):
            run(f"design-follow-{index}", lambda i=index: window.design_follow_choice.setCurrentIndex(i))
        choice("appearance", window.appearance_choice, "appearance")

        window._select_page("connection")
        toggle("hide-addresses", window.hide_switch, "hide_addresses")
        original_port = window.port_entry.text()
        for value in ("bad", "0", "65536", "24829", original_port):
            def save_port(value=value):
                window.port_entry.setText(value)
                window.save_button.click()
            run(f"save-port-{value}", save_port,
                {"port": int(value)} if value.isdecimal() and 1 <= int(value) <= 65535 else None)
        click("firewall-action", window.firewall_button)

        window._select_page("overview")
        for token in list(window.machines.rows):
            row = window.machines.rows.get(token)
            if row is None:
                continue
            # Tokens stay internal: names and numerical row indices are sufficient in reports.
            index = list(window.machines.rows).index(token)
            toggle(f"peer-{index}-in-use", row.in_use)
            if not row._phone:
                toggle(f"peer-{index}-directions", row.directions)
                toggle(f"peer-{index}-send", row.send)
                toggle(f"peer-{index}-allow", row.allow)
            click(f"peer-{index}-remove-confirm", row.remove_button)
            click(f"peer-{index}-keep", row.keep)

        if not window._pairing_open:
            click("pairing-open", window.machines.pair_button)
        click("pairing-show-code", window.sheet.show_button)
        click("pairing-cancel-code", window.sheet.cancel_button)
        for index, button in enumerate(window.sheet.group.buttons()):
            click(f"pairing-discovery-{index}", button)
        def manual_pair():
            window.sheet.address.setText("localhost-live.example.lan")
            window.sheet._typed_address(window.sheet.address.text())
            window.sheet.code.setText("123456")
            window.sheet._typed_code(window.sheet.code.text())
            window.sheet.pair_button.click()
        run("pairing-manual-address", manual_pair)
        run("pairing-result-error", lambda: window._on_pair_finished(None, ValueError("Audit fixture failure")))
        click("pairing-close", window.machines.pair_button)

        for index, action in enumerate(list(window.tray_menu.actions())):
            if action.isSeparator():
                continue
            if action.text() == "Quit Beamer":
                continue
            if not action.isEnabled():
                results.append({"action": f"tray-{index}-{action.text()}", "result": "disabled"})
                continue
            run(f"tray-{index}-{action.text()}", action.trigger)
            if action.text() == "About Beamer":
                for box in QApplication.topLevelWidgets():
                    if isinstance(box, QMessageBox) and box.windowTitle() == "About Beamer":
                        for index, button in enumerate(box.buttons()):
                            click(f"about-button-{index}", button)

        # Destructive to this fixture only, after every other peer-dependent surface.
        for index, token in enumerate(list(window.machines.rows)):
            row = window.machines.rows.get(token)
            if row is not None:
                click(f"peer-{index}-remove-confirm-final", row.remove_button)
                click(f"peer-{index}-remove-final", row.really)
        quit_action = next((a for a in window.tray_menu.actions() if a.text() == "Quit Beamer"), None)
        if quit_action is not None:
            run("tray-quit", quit_action.trigger)
    finally:
        sys.excepthook = previous_hook
        for name in ("trigger_recorder", "jump_recorder", "ignored_recorder"):
            getattr(window, name).cancel()

    report = {"actions": results, "counts": {kind: sum(row["result"] == kind for row in results)
                                             for kind in ("pass", "fail", "disabled")}}
    if output is not None:
        path = Path(output)
        if path.suffix != ".json":
            path = path / "actions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
