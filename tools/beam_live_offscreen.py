"""Measure live Beam layers without creating an NSWindow or reading app settings."""

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "mac_app")]

import AppKit
import Quartz

import crossing
import desktop_mac
import kvm_bridge_app
import effects_overlay
from core import effects


class OffscreenPanel:
    """Only an NSView and its live layer tree; ordering a window is an error."""

    @classmethod
    def alloc(cls):
        return cls()

    def initWithContentRect_styleMask_backing_defer_(self, rect, *_args):
        self.rect = rect
        return self

    def setContentView_(self, view):
        self.view = view

    def contentView(self):
        return self.view

    def setFrame_display_(self, rect, _display):
        self.rect = rect
        self.view.setFrame_(AppKit.NSMakeRect(0, 0, rect.size.width, rect.size.height))
        self.view.layer().setFrame_(self.view.bounds())

    def frame(self):
        return self.rect

    def orderFrontRegardless(self):
        raise AssertionError("offscreen probe must never show a window")

    def __getattr__(self, name):
        if name.startswith("set"):
            return lambda *_args: None
        raise AttributeError(name)


def live_glow(edge, size, *, pressure=1.0, corner=None, third=False, style="beam"):
    width, height = 1728.0, 1117.0
    controller = SimpleNamespace(
        cfg=SimpleNamespace(crossing=dict(crossing.DEFAULT_CROSSING, glow_style=style,
                                          glow_colour="signal", effect_size=size)),
        crossing_pressure_now=lambda: pressure,
        _current_desktop_bounds=lambda: (0.0, 0.0, width, height),
    )
    glow = kvm_bridge_app.EdgeGlow(controller, logging.getLogger("beam-offscreen"))
    glow.timer = mock.Mock()
    glow.dark_appearance = lambda: False
    glow.mac_edge = edge
    if corner is not None:
        glow.region = (width - 8 if "right" in corner else 0,
                       height - 8 if "bottom" in corner else 0, 8, 8)
    elif edge in ("left", "right"):
        glow.region = (width - 1 if edge == "right" else 0, height / 3 if third else 0,
                       1, height / 3 if third else height)
    else:
        glow.region = (width / 3 if third else 0, height - 1 if edge == "bottom" else 0,
                       width / 3 if third else width, 1)
    with mock.patch.object(AppKit, "NSPanel", OffscreenPanel):
        glow._ensure_panel()
    # _draw still performs all frame, palette, mask and opacity updates, but cannot order a panel.
    glow.visible = True
    glow.centre = 0.5
    with mock.patch.object(desktop_mac, "monitors", return_value=[]), \
            mock.patch.object(kvm_bridge_app, "menu_bar_height", return_value=0), \
            mock.patch.object(kvm_bridge_app.time, "monotonic", return_value=100.0):
        glow._draw()
    if glow.disabled:
        raise AssertionError("live layer drawing disabled the effect")
    return glow


def render(layer, scale, space=None):
    bounds = layer.bounds()
    width, height = math.ceil(bounds.size.width * scale), math.ceil(bounds.size.height * scale)
    data = bytearray(width * height * 4)
    space = space or AppKit.NSScreen.screens()[0].colorSpace().CGColorSpace()
    ctx = Quartz.CGBitmapContextCreate(data, width, height, 8, width * 4, space,
                                      Quartz.kCGImageAlphaPremultipliedLast | Quartz.kCGBitmapByteOrder32Big)
    # Align the layer's top to the first memory row; fractional panel depths otherwise leave
    # a partially covered extra row at the top of the enclosing integer-pixel bitmap.
    Quartz.CGContextTranslateCTM(ctx, 0, height - bounds.size.height * scale)
    Quartz.CGContextScaleCTM(ctx, scale, scale)
    layer.renderInContext_(ctx)
    return width, height, bytes(data)


def layer_profile(layer, edge, scale, space=None):
    width, height, rgba = render(layer, scale, space)
    if edge in ("left", "right"):
        values = [rgba[((height // 2) * width + x) * 4 + 3]
                  for x in range(math.floor(layer.bounds().size.width * scale))]
    else:
        values = [rgba[(y * width + width // 2) * 4 + 3]
                  for y in range(math.floor(layer.bounds().size.height * scale))]
    # CGBitmapContext's memory rows run from the top, although CALayer coordinates run up.
    if edge in ("right", "bottom"):
        values.reverse()
    return values


def profile(glow, edge, scale, space=None):
    return layer_profile(glow.panel.contentView().layer(), edge, scale, space)


def canvas_parity(profiles):
    """The actual EffectsCanvas.drawRect/_paint, cropped as _show crops a per-display panel.

    Beam normally bypasses this canvas. Replaying the recorded preview here tests whether
    the live NSView context, output colour space or panel clipping would damage its alpha.
    """
    times = dict(zip(("push10", "push45", "push85", "threshold", "give", "arrival", "settle"),
                     (.92, 1.35, 1.78, 2.18, 2.27, 2.44, 2.7)))
    results = []
    for source in profiles:
        scale = len(source["x"][:-1]) / 40
        for size, _label in effects.SIZES:
            scene = effects.preview_scene(effects.preview_effect("beam"), times[source["stage"]],
                                          "edge", kvm_bridge_app.notch_beam.palette_values("signal"),
                                          dark=source["mode"] == "dark", effect_size=size)
            pens = scene["pens"]
            for pen in pens:
                pen.clip = (0, 0, 800, 450)
            boxes = [effects_overlay.padded(pen, 2) for pen in pens]
            left = math.floor(min(b[0] for b in boxes))
            top = math.floor(min(b[1] for b in boxes))
            right = math.ceil(max(b[2] for b in boxes))
            bottom = math.ceil(max(b[3] for b in boxes))
            view = effects_overlay.EffectsCanvas.alloc().initWithFrame_(
                AppKit.NSMakeRect(0, 0, right - left, bottom - top))
            view.painter = lambda ctx, _w, _h: effects_overlay.EffectsOverlay._paint(ctx, pens, (left, top))
            width, height = round((right - left) * scale), round((bottom - top) * scale)
            measured = []
            for space in (Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB),
                          AppKit.NSScreen.screens()[0].colorSpace().CGColorSpace()):
                data = bytearray(width * height * 4)
                ctx = Quartz.CGBitmapContextCreate(data, width, height, 8, width * 4, space,
                                                  Quartz.kCGImageAlphaPremultipliedLast
                                                  | Quartz.kCGBitmapByteOrder32Big)
                Quartz.CGContextTranslateCTM(ctx, 0, height)
                Quartz.CGContextScaleCTM(ctx, scale, -scale)
                graphics = AppKit.NSGraphicsContext.graphicsContextWithCGContext_flipped_(ctx, True)
                AppKit.NSGraphicsContext.saveGraphicsState()
                try:
                    AppKit.NSGraphicsContext.setCurrentContext_(graphics)
                    view.drawRect_(view.bounds())
                finally:
                    AppKit.NSGraphicsContext.restoreGraphicsState()
                if view.painter is None:
                    raise AssertionError("EffectsCanvas failed to paint offscreen")
                values = []
                for x in source["x"]:
                    local = x - round(left * scale)
                    values.append(round(sum(data[(y * width + local) * 4 + 3] for y in range(height)) / height, 3)
                                  if 0 <= local < width else 0.0)
                measured.append(values)
            delta = [abs(a - b) for a, b in zip(measured[0], source["mac"])]
            colour_delta = max(abs(a - b) for a, b in zip(*measured))
            results.append({"size": size, "stage": source["stage"], "mode": source["mode"],
                            "scale": scale, "panel_points": [right - left, bottom - top],
                            "alpha_profile": measured[0], "screen_colour_space_alpha_max_difference": colour_delta,
                            "parity_mac_alpha_max_difference": max(delta),
                            "parity_windows_alpha_max_difference": max(abs(a - b) for a, b in
                                                                       zip(measured[0], source["windows"]))})
    return results


def measure():
    rows = []
    for size, _label in effects.SIZES:
        for edge in ("left", "right", "top", "bottom"):
            for scale in (1, 2):
                glow = live_glow(edge, size)
                values = profile(glow, edge, scale)
                rows.append({"size": size, "edge": edge, "scale": scale,
                             "frame": [glow.panel.frame().size.width, glow.panel.frame().size.height],
                             "depth_points": effects.edge_depth(1728, 1117, size),
                             "alpha_edge_to_interior": values,
                             "inner_over_outer": round(values[-1] / max(1, values[0]), 4),
                             "at_tenth_half_nine_tenths": [values[int((len(values) - 1) * f)] for f in (.1, .5, .9)]})
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--parity-profiles", type=Path, nargs="+")
    parser.add_argument("--without-inward-mask", action="store_true",
                        help="reproduce the pre-fix live layers by removing only the new inward mask")
    args = parser.parse_args()
    with mock.patch.object(kvm_bridge_app.notch_beam, "mask_edge_falloff",
                           (lambda layer, _edge, _beam: layer.setMask_(None))
                           if args.without_inward_mask else kvm_bridge_app.notch_beam.mask_edge_falloff):
        rows = measure()
    sample = live_glow("left", "medium")
    view = sample.panel.contentView()
    surface = {"view_opaque": bool(view.isOpaque())}
    for name, layer in (("view", view.layer()), ("fill", sample.fill), ("comet", sample.comet)):
        surface[name] = {"opaque": bool(layer.isOpaque()), "opacity": layer.opacity(),
                         "background_colour": str(layer.backgroundColor()),
                         "compositing_filter": str(layer.compositingFilter()), "contents_scale": layer.contentsScale()}
    result = {"live_layers": rows, "without_inward_mask": args.without_inward_mask,
              "surface": surface,
              "colour_space": str(AppKit.NSScreen.screens()[0].colorSpace()),
              "format": "RGBA8 premultiplied-last, big-endian; NSView/CALayer renderInContext"}
    if args.parity_profiles:
        profiles = [profile for path in args.parity_profiles for profile in json.loads(path.read_text())]
        if any(not any(row["mac"]) or not any(row["windows"]) for row in profiles):
            raise ValueError("all-zero reference profile: check raw frame geometry before comparing")
        result["canvas_parity"] = canvas_parity(profiles)
        result["preview_parity_profiles"] = profiles
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Measured {len(rows)} live layer profiles; inner/outer alpha "
          f"{min(r['inner_over_outer'] for r in rows):.4f}..{max(r['inner_over_outer'] for r in rows):.4f}")
