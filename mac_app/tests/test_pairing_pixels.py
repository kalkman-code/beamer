"""Real AppKit pairing views, cached without ordering a window onto the desktop."""

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mac_app"))

import AppKit

import machines
import machines_panel
import kvm_bridge_app
import theme
import widgets


def bitmap(view, scale):
    view.layoutSubtreeIfNeeded()
    bounds = view.bounds()
    width, height = round(bounds.size.width), round(bounds.size.height)
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, width * scale, height * scale, 8, 4, True, False,
        AppKit.NSCalibratedRGBColorSpace, 0, 0,
    )
    rep.setSize_((width, height))
    view.cacheDisplayInRect_toBitmapImageRep_(bounds, rep)
    return rep


def save(rep, path):
    rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(path), True)


def colour(rep, x, y):
    c = rep.colorAtX_y_(x, y).colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
    return c.redComponent(), c.greenComponent(), c.blueComponent()


def offsets(rep, boxes, scale):
    measured = []
    for field in boxes.fields:
        container = field.superview()
        rect = container.convertRect_toView_(container.bounds(), boxes.view)
        # Bitmap rows run top-down; NSStackView's coordinates run bottom-up.
        x, y, w, h = rect.origin.x, boxes.view.bounds().size.height - rect.origin.y - rect.size.height, rect.size.width, rect.size.height
        left, right = round((x + 4) * scale), round((x + w - 4) * scale)
        top, bottom = round((y + 4) * scale), round((y + h - 4) * scale)
        foreground = theme.rgb(theme.T["ink"])
        ink = [(px, py) for py in range(top, bottom) for px in range(left, right)
               if sum(abs(a - b) for a, b in zip(colour(rep, px, py), foreground)) < 0.45]
        if not ink:
            measured.append(None)
            continue
        lx, rx = min(p[0] for p in ink), max(p[0] for p in ink) + 1
        ty, by = min(p[1] for p in ink), max(p[1] for p in ink) + 1
        measured.append({"digit": field.stringValue(), "dx": round((lx + rx) / 2 - (x + w / 2) * scale, 3),
                         "dy": round((ty + by) / 2 - (y + h / 2) * scale, 3),
                         "ink": [lx, ty, rx, by], "inner": [(x + 1) * scale, (y + 1) * scale, (x + w - 1) * scale, (y + h - 1) * scale]})
    return measured


def atlas(reps, path, columns=1):
    width, height = reps[0].pixelsWide(), reps[0].pixelsHigh()
    rows = (len(reps) + columns - 1) // columns
    combined = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, width * columns, height * rows, 8, 4, True, False, AppKit.NSCalibratedRGBColorSpace, 0, 0)
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(combined))
    for index, rep in enumerate(reps):
        image = AppKit.NSImage.alloc().initWithSize_(rep.size())
        image.addRepresentation_(rep)
        image.drawInRect_fromRect_operation_fraction_(((index % columns * width, (rows - index // columns - 1) * height), (width, height)),
                                                      AppKit.NSZeroRect, AppKit.NSCompositingOperationCopy, 1)
    AppKit.NSGraphicsContext.restoreGraphicsState()
    save(combined, path)


class PairingPixelsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        AppKit.NSApplication.sharedApplication()
        theme.init_fonts()
        cls.renders = Path(os.environ.get("BEAMER_PAIRUI_RENDERS", ROOT / "renders"))
        cls.renders.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, 720, 920), AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False)
        self.window.setReleasedWhenClosed_(False)
        self.window.setAppearance_(theme.appearance_named(True))
        owner = SimpleNamespace(window=self.window, _shown=lambda text: text)
        controller = SimpleNamespace(cfg=SimpleNamespace(hide_addresses=False))
        self.panel = machines_panel.MachinesPanel(controller, mock.Mock(), mock.Mock(), owner)
        self.panel.rows = lambda: []
        self.panel.service = SimpleNamespace(code=None, machines=lambda: [], seconds_left=60, outcome=None,
                                              known=None, error=None, tcp_error=None, tcp_port=24821,
                                              qr_text=lambda address: None)
        self.address_patch = mock.patch.object(machines_panel.pairing, "pairing_address", return_value="192.0.2.20")
        self.address_patch.start()
        self.addCleanup(self.address_patch.stop)
        self.body = widgets.stack(spacing=20)
        widgets.add(self.body, self.panel.machines_module.view)
        widgets.add(self.body, self.panel.sheet.view)
        self.window.contentView().addSubview_(self.body)
        content = self.window.contentView()
        AppKit.NSLayoutConstraint.activateConstraints_([
            self.body.topAnchor().constraintEqualToAnchor_constant_(content.topAnchor(), 20),
            self.body.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), 20),
            self.body.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -20),
            self.body.bottomAnchor().constraintLessThanOrEqualToAnchor_constant_(content.bottomAnchor(), -20),
        ])
        self.panel.refresh()
        self.window.contentView().layoutSubtreeIfNeeded()

    def tearDown(self):
        self.window.makeFirstResponder_(None)
        self.window.close()

    def test_digits_are_centred_from_rendered_pixels_at_both_scales(self):
        report = []
        failures = []
        for scale in (1, 2):
            for state in ("typed", "shown"):
                boxes = self.panel.code_boxes if state == "typed" else self.panel.shown_code_boxes
                if state == "shown":
                    self.panel.service.code = "012345"
                    self.panel.refresh()
                else:
                    self.panel.service.code = None
                    self.panel.refresh()
                strips = []
                for digit in "0123456789":
                    self.window.makeFirstResponder_(None)
                    if state == "typed":
                        boxes.clear()
                        boxes.focus(self.window)
                        boxes.fields[0].selectText_(None)
                        self.window.firstResponder().insertText_(digit * 6)
                        self.assertEqual(boxes.value, digit * 6)
                        editor = boxes.fields[-1].currentEditor()
                        self.assertIsNotNone(editor)
                        editor.setSelectedRange_((1, 0))
                        editor.setInsertionPointColor_(AppKit.NSColor.clearColor())
                    else:
                        self.panel.service.code = digit * 6
                        self.panel.refresh()
                    self.window.contentView().layoutSubtreeIfNeeded()
                    rep = bitmap(boxes.view, scale)
                    strips.append(rep)
                    rows = offsets(rep, boxes, scale)
                    report.append({"scale": scale, "state": state, "digit": digit, "boxes": rows})
                    for index, row in enumerate(rows):
                        if row is None or abs(row["dx"]) > 1 or abs(row["dy"]) > 1:
                            failures.append((scale, state, digit, index, row))
                atlas(strips, self.renders / f"mac-digits-{state}-{scale}x.png")
                if state == "typed":
                    self.window.makeFirstResponder_(None)
                    for field, digit in zip(boxes.fields, "482913"):
                        field.setStringValue_(digit)
                    save(bitmap(self.panel.sheet.view, scale), self.renders / f"mac-sheet-typed-{scale}x.png")
        (self.renders / "mac-measurements.json").write_text(json.dumps(report, indent=2))
        self.assertEqual(failures, [], f"ink must centre within one physical pixel ({len(failures)} failures): {failures[:6]}")
        self.assertTrue(hasattr(self.panel, "shown_code_boxes"), "the hosted code must use the same six boxes")

    def test_pairing_action_leads_the_machines_and_uses_primary_style(self):
        panel = self.panel
        for scale in (1, 2):
            save(bitmap(panel.machines_module.view, scale), self.renders / f"mac-machines-empty-{scale}x.png")
            save(bitmap(panel.sheet.view, scale), self.renders / f"mac-sheet-empty-{scale}x.png")
        order = list(panel.machines_module.body.arrangedSubviews())
        self.assertLess(order.index(panel.pair_button.view.superview()), order.index(panel.list))
        self.assertEqual(panel.pair_button.style, "primary")
        self.assertFalse(panel.pair_button.view.isHidden())
        entry = {"token": "synthetic-test-token", "id": "test-pc", "name": "Studio PC", "platform": "windows",
                 "host": "192.0.2.30", "port": 24820, "send": True, "allow_drive": True, "linked": True}
        row = machines.row(entry, "Studio PC", live=True, kind="connected", status="", here=False,
                           driving=False, inbound=False, has_link=True)
        panel.rows = lambda: [row]
        panel.refresh()
        for scale in (1, 2):
            save(bitmap(panel.machines_module.view, scale), self.renders / f"mac-machines-filled-{scale}x.png")

    def test_machine_addresses_use_the_reading_face_without_mono_dot_spacing(self):
        panel = self.panel
        panel.service.code = "482913"
        panel.refresh()
        address_font = panel.code_address.view.attributedStringValue().attribute_atIndex_effectiveRange_(
            AppKit.NSFontAttributeName, 0, None)[0]
        entry = {"token": "synthetic-test-token", "id": "test-pc", "name": "Studio PC",
                 "platform": "windows", "host": "192.0.2.30", "port": 24820,
                 "send": True, "allow_drive": True, "linked": True}
        row = machines.row(entry, "Studio PC", live=True, kind="connected", status="", here=False,
                           driving=False, inbound=False, has_link=True)
        rendered = panel._row_view(row)
        machine_address = rendered.subviews()[0].arrangedSubviews()[1]
        machine_font = machine_address.attributedStringValue().attribute_atIndex_effectiveRange_(
            AppKit.NSFontAttributeName, 0, None)[0]
        heard = {"address": "192.0.2.30", "name": "Studio PC", "platform": "windows",
                 "pairing": machines_panel.pairing.PAIRING_V3, "pair_id": None}
        panel._heard_row(0, heard, picked=False, shared_name=True)
        heard_row = panel.heard_list.arrangedSubviews()[-1]
        heard_detail = heard_row.subviews()[0].arrangedSubviews()[-1]
        heard_font = heard_detail.attributedStringValue().attribute_atIndex_effectiveRange_(
            AppKit.NSFontAttributeName, 0, None)[0]
        expected = theme.font(theme.TYPE["small"]).fontName()
        self.assertEqual(address_font.fontName(), expected)
        self.assertEqual(machine_font.fontName(), expected)
        self.assertEqual(heard_font.fontName(), expected)

    def test_pairing_module_leads_overview_with_and_without_existing_machines(self):
        body = widgets.stack()
        for item in (widgets.label("Overview"), widgets.label("Connection status"), widgets.label("Keyboard and pointer"),
                     self.panel.machines_module.view, self.panel.sheet.view):
            body.addArrangedSubview_(item)
        owner = SimpleNamespace(_machines_first=None, overview_body=body, panel=self.panel)
        for empty in (False, True, False):
            kvm_bridge_app.ControlWindow._place_machines(owner, empty)
            self.assertEqual(list(body.arrangedSubviews()).index(self.panel.machines_module.view), 1)

    def test_shown_boxes_are_read_only_and_empty_boxes_draw_no_ink(self):
        boxes = self.panel.code_boxes
        for scale in (1, 2):
            self.assertEqual(offsets(bitmap(boxes.view, scale), boxes, scale), [None] * 6)
        shown = getattr(self.panel, "shown_code_boxes", None)
        self.assertIsNotNone(shown)
        self.panel.service.code = "482913"
        self.panel.service.qr_text = lambda _address: "beamer://pair?host=192.0.2.20&port=24821&code=482913&k=" + "A" * 22
        self.panel.refresh()
        self.assertEqual(shown.value, "482913")
        self.assertTrue(all(not field.isEditable() for field in shown.fields))
        for scale in (1, 2):
            save(bitmap(self.panel.sheet.view, scale), self.renders / f"mac-sheet-shown-{scale}x.png")

    def test_pairing_dialog_hides_the_qr_and_caption(self):
        self.panel.service.code = "482913"
        self.panel.service.qr_text = lambda _address: "synthetic pairing payload"
        self.panel.refresh()
        self.assertTrue(self.panel.qr_view.isHidden())
        self.assertTrue(self.panel.qr_caption.view.isHidden())
        for scale in (1, 2):
            save(bitmap(self.panel.sheet.view, scale), self.renders / f"mac-pairing-dialog-{scale}x.png")

    def test_selected_digits_use_the_same_centred_ink_and_restore_shared_editor_style(self):
        boxes = self.panel.code_boxes
        report, failures = [], []
        for scale in (1, 2):
            strips = []
            for digit in "0123456789":
                self.window.makeFirstResponder_(None)
                boxes.clear()
                boxes.focus(self.window)
                boxes.fields[0].selectText_(None)
                self.window.firstResponder().insertText_(digit * 6)
                for index, field in enumerate(boxes.fields):
                    self.window.makeFirstResponder_(field)
                    editor = field.currentEditor()
                    editor.setSelectedRange_((0, 1))
                    rep = bitmap(boxes.view, scale)
                    strips.append(rep)
                    row = offsets(rep, boxes, scale)[index]
                    report.append({"scale": scale, "digit": digit, "slot": index, "selected": row})
                    if row is None or abs(row["dx"]) > 1 or abs(row["dy"]) > 1:
                        failures.append((scale, digit, index, row))
            atlas(strips, self.renders / f"mac-digits-selected-{scale}x.png", columns=6)
        (self.renders / "mac-selected-measurements.json").write_text(json.dumps(report, indent=2))
        self.assertEqual(failures, [], f"selected ink offsets: {failures[:6]}")
        self.window.makeFirstResponder_(self.panel.find_field)
        self.assertNotEqual(self.panel.find_field.currentEditor().selectedTextAttributes()[AppKit.NSForegroundColorAttributeName],
                            AppKit.NSColor.clearColor())

    def test_leaving_an_empty_box_restores_the_shared_editor_without_typing(self):
        for field in self.panel.code_boxes.fields:
            self.window.makeFirstResponder_(field)
            self.assertIsNotNone(field.currentEditor())
            self.window.makeFirstResponder_(self.panel.find_field)
            attributes = self.panel.find_field.currentEditor().selectedTextAttributes()
            self.assertNotEqual(attributes[AppKit.NSForegroundColorAttributeName], AppKit.NSColor.clearColor())
            self.assertIsNone(field.pair_editor)


if __name__ == "__main__":
    unittest.main()
