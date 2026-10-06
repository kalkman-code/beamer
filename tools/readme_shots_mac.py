"""The README's Mac screenshots and a layout check: the real settings window, built from a synthetic
config (documentation addresses, no house detail) and drawn page by page into a bitmap without
ever being put on screen. Starts nothing: no event tap, no link, no discovery."""

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(sys.argv[1])
OUT = Path(sys.argv[2])
PAGES = sys.argv[3:] or ["overview", "design", "keyboard"]
sys.path.insert(0, str(REPO / "mac_app"))

import AppKit  # noqa: E402
from Foundation import NSDate, NSRunLoop  # noqa: E402

AppKit.NSApplication.sharedApplication()

import kvm_bridge_app  # noqa: E402
import settings_store  # noqa: E402
from wake import WakingController as KVMController  # noqa: E402

kvm_bridge_app.VERSION = os.environ.get("SHOT_VERSION", kvm_bridge_app.VERSION)
kvm_bridge_app.accessibility_granted = lambda: True
kvm_bridge_app.input_monitoring_granted = lambda: True

path = Path(tempfile.mkdtemp()) / "config.json"
raw = settings_store.config_to_raw(settings_store.editable_default_config())
raw.update(host="192.168.1.20", auth_token="synthetic", pc_name="Desktop PC",
           ignored_inputs=["button:back", "button:forward", "media:volume_up", "key:105"],
           appearance=os.environ.get("APPEARANCE", "dark"))
path.write_text(json.dumps(raw))
store = settings_store.SettingsStore(path)
logger = logging.getLogger("shots")
controller = KVMController(store.load(), logger=logger)
if os.environ.get("CONNECTED"):
    controller.sock = object()
    controller.connected_at = controller.last_ack_at = time.monotonic()
    controller.tap_ready.set()
    controller.event_tap = object()
window = kvm_bridge_app.ControlWindow.alloc().initWithController_settingsStore_logger_(controller, store, logger)
window.has_notch = True
controller.notch_range = (656.0, 856.0)
width, height = (int(v) for v in os.environ.get("SIZE", "900x640").split("x"))
# SWITCH_TO builds the window in APPEARANCE and switches it live, the way a choice or the Mac
# switching does, so a shot proves the repaint rather than a fresh build.
if os.environ.get("SWITCH_TO"):
    window._apply_appearance(os.environ["SWITCH_TO"])
window.window.setContentSize_((width, height))
OUT.mkdir(parents=True, exist_ok=True)
for key in PAGES:
    window._select_page(key)
    window.refresh()
    if key == "design" and window.previews is not None:
        loop = window.previews
        loop.started_at = time.monotonic() - float(os.environ.get("PREVIEW_AT", "1.6"))
        loop._tick()
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.3))
    view = window.window.contentView().superview() or window.window.contentView()
    view.layoutSubtreeIfNeeded()
    rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
    view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    suffix = os.environ.get("SUFFIX", "")
    data.writeToFile_atomically_(str(OUT / f"mac-{key}{suffix}.png"), True)
print(OUT)
