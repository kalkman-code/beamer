"""A local test app with its own settings and logs; quit regular Beamer first."""

import sys
import os
from pathlib import Path

# The regular Mac packager supplies RESOURCEPATH; PyInstaller supplies _MEIPASS.
if getattr(sys, "frozen", False):
    os.environ.setdefault("RESOURCEPATH", sys._MEIPASS)

import kvm_bridge_app


def self_test(report_path):
    import json
    import keyboard_layout
    from core import i18n
    assert i18n.install("ru") == "ru"
    assert i18n.tr("Settings") == "Настройки"
    assert keyboard_layout.refresh()
    catalogue = i18n.parse_po((i18n.locales_dir() / "ru.po").read_text(encoding="utf-8"))
    import tempfile
    import logging
    import theme
    import settings_store
    from wake import WakingController
    kvm_bridge_app.AppKit.NSApplication.sharedApplication()
    theme.init_fonts()
    with tempfile.TemporaryDirectory(prefix="beamer-smoke-", dir=str(Path(report_path).parent)) as folder:
        store = settings_store.SettingsStore(Path(folder) / "settings.json")
        cfg = settings_store.editable_default_config()
        controller = WakingController(cfg, logger=logging.getLogger("beamer-smoke"))
        controller.start_input_capture = lambda: None
        control = kvm_bridge_app.ControlWindow.alloc().initWithController_settingsStore_logger_(
            controller, store, logging.getLogger("beamer-smoke"))
        for page in control.pages:
            control._select_page(page)
            control.window.contentView().layoutSubtreeIfNeeded()
        assert not control.window.isVisible()
        assert hasattr(control, "keyboard_sync_box")
        ui_pages = list(control.pages)
    Path(report_path).write_text(json.dumps({
        "version": kvm_bridge_app.VERSION,
        "russian_entries": sum(bool(entry["msgid"]) for entry in catalogue),
        "russian_settings_label": i18n.tr("Settings"),
        "native_input_language_read": keyboard_layout.current_language(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "native_ui_pages_created_offscreen": ui_pages,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    raise SystemExit(0)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        self_test(sys.argv[2])
    kvm_bridge_app.LOG_DIRECTORY = Path.home() / "Library" / "Logs" / "Beamer RU Test"
    kvm_bridge_app.LOG_PATH = kvm_bridge_app.LOG_DIRECTORY / "Beamer.log"
    arguments = sys.argv[1:]
    if "--config" not in arguments:
        arguments += ["--config", str(Path.home() / "Library" / "Application Support" /
                                    "Beamer RU Test" / "settings.json")]
    kvm_bridge_app.main(arguments)
