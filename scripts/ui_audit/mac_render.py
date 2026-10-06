"""Offscreen AppKit UI audit; never starts Beamer or orders a window front.

Run with Beamer's PyObjC interpreter from the repository root. All settings are
in memory. Geometry JSON is a candidate list requiring visual review.
"""
from __future__ import annotations

import argparse
import builtins
import copy
import json
import logging
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "mac_app"), str(ROOT)]
import AppKit
from Foundation import NSDate, NSRunLoop
import kvm_bridge_app as app
import settings_store
import widgets
import theme
from wake import WakingController
from core.link import OutboundLink

AppKit.NSApplication.sharedApplication()
theme.init_fonts()


def deny_real_settings(event, args):
    if event == "open" and isinstance(args[0], (str, bytes, Path)):
        path = str(args[0])
        if "/Library/Application Support/Beamer/" in path:
            raise RuntimeError("UI audit blocked a real Beamer settings access")


sys.addaudithook(deny_real_settings)


def walk(view, hidden=False):
    hidden = hidden or bool(view.isHidden())
    if hidden:
        return
    yield view
    for child in view.subviews():
        yield from walk(child, hidden)


def settle(control):
    control.window.contentView().layoutSubtreeIfNeeded()
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.025))
    control.window.contentView().layoutSubtreeIfNeeded()
    if control.window.isVisible():
        raise RuntimeError("Offscreen window unexpectedly became visible")


def peers(count):
    names = ["PC", "A very long workstation name with translated length", "Linux", "localhost-live.example.lan", "Phone"]
    return [{**settings_store.PEER_DEFAULTS, "id": settings_store._b64(bytes([i + 1]) * 16),
             "name": names[i][:app.protocol.MAX_NAME_CHARS], "platform": "android" if i == 4 else "windows" if i % 2 == 0 else "linux",
             "token": settings_store._b64(bytes([i + 11]) * 32), "host": "localhost-live.example.lan" if i == 3 else f"192.0.2.{i + 10}",
             "port": 0 if i == 4 else 24820, "send": i != 4, "allow_drive": True,
             "side": ("left", "right", "top", "bottom", "")[i], "linked": True,
             "paired_at": i + 1} for i in range(count)]


def fixture(entries, connected, appearance="system"):
    AppKit.NSApplication.sharedApplication()
    store = object.__new__(settings_store.SettingsStore)
    store.lock = threading.RLock()
    store._handed_token = ""
    store._cache = store._complete({**store._fresh(), "name": "Mac audit", "appearance": appearance,
                                   "machine_id": settings_store._b64(bytes([100]) * 16), "peers": entries,
                                   "zones": [{"peer": p["id"], "kind": "edge"} for p in entries if p["port"] and p["side"]]})
    store._check(store._cache)
    store.path = ROOT / "renders/mac/synthetic-settings.json"
    store._write = lambda *args, **kwargs: None
    store._read = lambda: copy.deepcopy(store._cache)
    store.load_settings = lambda: copy.deepcopy(store._cache)
    store.load = lambda: store._config(store._cache)
    cfg = store.load()
    store._handed_token = cfg.auth_token
    logger = logging.getLogger("ui-audit")
    book, identity = app.bridge.links_from_store(store, app.VERSION)
    controller = WakingController(cfg, logger=logger, book=book, identity=identity, hardware=lambda _host: "")
    controller.start_input_capture = lambda: None
    controller.peers_changed = lambda: None
    controller._sync_links = lambda: None
    controller.links = {}
    for p in entries:
        if p["port"] == 0:
            continue
        link = OutboundLink(p["token"], controller.book, lambda: {})
        link._live = connected
        link.kind = "connected" if connected else "unreachable"
        link.status = "Connected" if connected else "Disconnected"
        link.peer_id, link.peer_name, link.peer_platform = p["id"], p["name"], p["platform"]
        link.dialled = (p["host"], p["port"])
        link._caps = frozenset(("settings", "design_sync"))
        link.entry = lambda p=p: copy.deepcopy(p)
        link.start = lambda: None
        link.refresh = lambda: None
        link.send = lambda *args, **kwargs: None
        controller.links[p["token"]] = link
    controller._primary_link = next(iter(controller.links.values()), None)
    controller._primary_token = entries[0]["token"] if entries else ""
    controller._peers_up = {link.peer_id: link for link in controller.links.values()} if connected else {}
    controller._accepts = {p["id"]: True for p in entries if p["port"]} if connected else {}
    controller._return_local = lambda *args: None
    def redirect(value, *args, peer=None, **kwargs):
        target = peer or controller._shortcut_peer()
        if value and target is None:
            return False
        controller._redirecting = bool(value)
        controller.receiving = False
        controller.owner.on = target if value else app.protocol.id_text(identity()["id"])
        return True
    controller.set_redirecting = redirect
    controller.event_tap = object()
    controller.retry_capture = lambda *args: None
    controller.send_to_peer = lambda *args: None
    with mock.patch.object(app, "accessibility_granted", return_value=True), mock.patch.object(app, "input_monitoring_granted", return_value=True):
        control = app.ControlWindow.alloc().initWithController_settingsStore_logger_(controller, store, logger)
    control.retry_capture = lambda *args, **kwargs: None
    control.inbound_ids = lambda: {p["id"] for p in store._cache["peers"]} if connected else set()
    control.refresh()
    control.previews = mock.Mock()
    return control


def render_metadata(control):
    return {"sans_font": str(theme.font(theme.TYPE["body"]).fontName()),
            "mono_font": str(theme.mono_font(theme.TYPE["small"]).fontName()),
            "appearance": control.appearance_select.value, "dark": bool(theme.is_dark()),
            "backing_scale_factor": float(control.window.backingScaleFactor()),
            "scale_method": "bitmap pixel density; physical display backing scale unchanged"}


def rect(value):
    return [round(value.origin.x, 2), round(value.origin.y, 2), round(value.size.width, 2), round(value.size.height, 2)]


def measure(root):
    rows, flags = [], []
    for view in walk(root):
        frame = view.convertRect_toView_(view.bounds(), root)
        entry = {"class": view.className(), "frame": rect(frame), "label": str(view.accessibilityLabel() or "")}
        if isinstance(view, AppKit.NSTextField):
            text = str(view.stringValue())
            entry["text"] = text
            attributed = view.attributedStringValue()
            bounds = view.bounds()
            cell_rect = view.cell().drawingRectForBounds_(bounds)
            entry["cell"] = rect(cell_rect)
            required = attributed.boundingRectWithSize_options_((max(1, cell_rect.size.width), 100000),
                            AppKit.NSStringDrawingUsesLineFragmentOrigin | AppKit.NSStringDrawingUsesFontLeading)
            entry["required"] = rect(required)
            if text and required.size.height > cell_rect.size.height + 2:
                flags.append({**entry, "kind": "text-height"})
            if text and not view.cell().wraps() and attributed.size().width > cell_rect.size.width + 2:
                paragraph = attributed.attribute_atIndex_effectiveRange_(
                    AppKit.NSParagraphStyleAttributeName, 0, None)[0]
                line_break_mode = paragraph.lineBreakMode() if paragraph is not None else view.cell().lineBreakMode()
                tooltip = str(view.toolTip() or "")
                accessibility_text = str(view.accessibilityLabel() or "")
                intentional = (line_break_mode == AppKit.NSLineBreakByTruncatingTail
                               and tooltip == text and accessibility_text == text)
                flags.append({**entry, "kind": "text-width",
                              "ink_width": round(attributed.size().width, 2),
                              "source_ink_width": round(attributed.size().width, 2),
                              "visible_width": round(cell_rect.size.width, 2),
                              "line_break_mode": int(line_break_mode),
                              "tooltip": tooltip, "accessibility_text": accessibility_text,
                              "classification": "intentional-tail-elision" if intentional else "review-source-width",
                              "intentional_elision": intentional})
        elif isinstance(view, AppKit.NSButton):
            title = view.attributedTitle()
            title_rect = view.cell().titleRectForBounds_(view.bounds())
            entry.update(text=str(view.title()), cell=rect(title_rect),
                         ink_width=round(title.size().width, 2), ink_height=round(title.size().height, 2))
            if title.size().width > title_rect.size.width + 2:
                flags.append({**entry, "kind": "button-title-width"})
            if title.size().height > title_rect.size.height + 2:
                flags.append({**entry, "kind": "button-title-height"})
        elif isinstance(view, widgets._RulerView):
            owner = view.owner
            span, base = view.bounds().size.width - 3, view.TOP + view.TRACK
            attributes = {AppKit.NSFontAttributeName: theme.mono_font(theme.TYPE["eyebrow"])}
            entry["drawn_text"] = []
            for index, value in enumerate(owner.labels):
                text = AppKit.NSString.stringWithString_(str(value))
                ink = text.sizeWithAttributes_(attributes)
                x = 1.5 + widgets.scale_fraction(value, owner.low, owner.high, owner.scale) * span
                left = 0 if index == 0 else view.bounds().size.width - ink.width if index == len(owner.labels) - 1 else x - ink.width / 2
                entry["drawn_text"].append({"text": str(value), "left": left, "top": base + 5, "width": ink.width, "height": ink.height})
                if left < -2 or left + ink.width > view.bounds().size.width + 2 or base + 5 + ink.height > view.bounds().size.height + 2:
                    flags.append({**entry, "text": str(value), "kind": "drawn-ruler-text"})
        parent = view.superview()
        if parent is not None and not isinstance(parent, AppKit.NSClipView) and view.className() != "NSButtonTextField":
            f, b = view.frame(), parent.bounds()
            if f.size.width > 1 and f.size.height > 1 and (f.origin.x < b.origin.x - 2 or f.origin.x + f.size.width > b.origin.x + b.size.width + 2):
                flags.append({**entry, "kind": "outside-parent-horizontal", "parent": parent.className(), "parent_bounds": rect(b)})
        if isinstance(view, AppKit.NSStackView):
            children = [child for child in view.arrangedSubviews() if not child.isHidden()]
            vertical = view.orientation() == AppKit.NSUserInterfaceLayoutOrientationVertical
            for first, second in zip(children, children[1:]):
                a, b = first.alignmentRectForFrame_(first.frame()), second.alignmentRectForFrame_(second.frame())
                if vertical:
                    gap = b.origin.y - a.origin.y - a.size.height if view.isFlipped() else a.origin.y - b.origin.y - b.size.height
                else:
                    gap = b.origin.x - a.origin.x - a.size.width
                wanted = view.customSpacingAfterView_(first)
                if abs(wanted) > 100000:
                    wanted = view.spacing()
                if gap < -2:
                    flags.append({**entry, "kind": "stack-overlap", "actual_gap": round(gap, 2),
                                  "expected_gap": wanted, "first": first.className(), "second": second.className()})
                elif abs(gap - wanted) > 2 and view.distribution() == AppKit.NSStackViewDistributionFill:
                    flags.append({**entry, "kind": "uneven-stack-gap", "actual_gap": round(gap, 2), "expected_gap": wanted})
        rows.append(entry)
    return {"size": [root.bounds().size.width, root.bounds().size.height], "is_flipped": bool(root.isFlipped()), "views": rows, "flags": flags}


def render(view, path, scale):
    width, height = view.bounds().size
    bitmap = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, round(width * scale), round(height * scale), 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0)
    bitmap.setSize_((width, height))
    view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), bitmap)
    data = bitmap.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    data.writeToFile_atomically_(str(path), True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "renders/mac")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    counts = [0] if args.quick else [0, 1, 3, 5]
    widths = [900] if args.quick else [int(theme.MIN_WINDOW[0]), 900, 1200, 1600, 1900]
    with mock.patch.object(app, "accessibility_granted", return_value=True), mock.patch.object(app, "input_monitoring_granted", return_value=True):
        for count in counts:
            for connected in ([False] if args.quick else [False, True]):
                control = fixture(peers(count), connected)
                state = f"{count}-{'connected' if connected else 'disconnected'}"
                for width in widths:
                    control.window.setContentSize_((width, int(theme.MIN_WINDOW[1]) if width == int(theme.MIN_WINDOW[0]) else 900))
                    control.windowDidResize_(None)
                    settle(control)
                    for page in control.pages:
                        control._select_page(page)
                        settle(control)
                        targets = [(page, control.window.contentView()), (f"{page}-full", control.pages[page].documentView())]
                        for name, target in targets:
                            measurements = measure(target)
                            for scale in ([1] if args.quick else [1, 2]):
                                path = args.output / f"{name}-{width}-{scale}x-{state}.png"
                                render(target, path, scale)
                                results.append({"page": name, "width": width, "scale": scale, "state": state,
                                                "path": str(path), **render_metadata(control), **measurements})
                    print(f"rendered {state} {width}", flush=True)
    (args.output / "measurements.json").write_text(json.dumps(results, indent=2))
    width_flags = [flag for result in results for flag in result["flags"] if flag["kind"] == "text-width"]
    print(json.dumps({"renders": len(results), "flagged": sum(bool(r["flags"]) for r in results),
                      "raw_width_flags": len(width_flags),
                      "intentional_elisions": sum(flag["intentional_elision"] for flag in width_flags),
                      "unclassified_source_width_flags": sum(not flag["intentional_elision"] for flag in width_flags)}))


if __name__ == "__main__":
    main()
