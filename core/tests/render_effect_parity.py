"""Render matching offscreen effect frames on Qt or Core Graphics.

The Windows output is the reference for the Mac replay. This imports no app entrypoint and
creates no window. Run from either platform's Beamer worktree with that platform's Python.
"""

import argparse
import json
import math
import struct
import sys
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ("mac_app" if sys.platform == "darwin" else "win_app")))

from core import effects
from tokens import PALETTES

TIMES = (0.92, 1.35, 1.78, 2.18, 2.27, 2.44, 2.7)
STAGES = ("push10", "push45", "push85", "threshold", "give", "arrival", "settle")
SCALE = 0.6
FORCE_NORMAL = False


def catalogue():
    yield "Light", "glow", "signal"
    yield "Light", "beam", "signal"
    yield "Light", "aperture", "signal"
    for _module, name, effect_ids, pack_ids in effects.ALL_DIRECTIONS:
        for effect_id in effect_ids:
            yield name, effect_id, pack_ids[0]


def png_bytes(width, height, rgba):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)

    rows = b"".join(b"\0" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


class Bitmap:
    def __init__(self, width, height, *, scale=SCALE, transparent=False, dark=True):
        self.width, self.height, self.scale = width, height, scale
        if sys.platform == "darwin":
            import Quartz as cg
            from effects_overlay import replay
            if FORCE_NORMAL:
                native_cg = cg
                class NormalBlend:
                    def __getattr__(self, name):
                        return native_cg.kCGBlendModeNormal if name == "kCGBlendModePlusLighter" else getattr(native_cg, name)
                cg = NormalBlend()
            self.cg, self.replay = cg, replay
            self.data = bytearray(width * height * 4)
            self.ctx = cg.CGBitmapContextCreate(
                self.data, width, height, 8, width * 4,
                cg.CGColorSpaceCreateWithName(cg.kCGColorSpaceSRGB),
                cg.kCGImageAlphaPremultipliedLast | cg.kCGBitmapByteOrder32Big,
            )
            cg.CGContextTranslateCTM(self.ctx, 0, height)
            cg.CGContextScaleCTM(self.ctx, scale, -scale)
            if not transparent:
                rgb = (0.065, 0.075, 0.09) if dark else (0.93, 0.92, 0.89)
                cg.CGContextSetRGBFillColor(self.ctx, *rgb, 1)
                cg.CGContextFillRect(self.ctx, ((-4000, -4000), (8000, 8000)))
        else:
            from PySide6.QtGui import QImage, QPainter, QColor
            from effect_overlay import replay
            self.replay = replay
            self.image = QImage(width, height, QImage.Format.Format_RGBA8888_Premultiplied)
            self.image.fill(0)
            self.painter = QPainter(self.image)
            self.painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            self.painter.scale(scale, scale)
            if not transparent:
                rgb = (0.065, 0.075, 0.09) if dark else (0.93, 0.92, 0.89)
                self.painter.fillRect(-4000, -4000, 8000, 8000, QColor.fromRgbF(*rgb, 1))

    def draw(self, pens):
        if sys.platform == "darwin":
            self.replay(self.cg, self.ctx, pens)
        else:
            self.replay(self.painter, pens)

    def raw(self):
        if sys.platform == "darwin":
            return bytes(self.data)
        return bytes(self.image.bits())

    def close(self):
        if sys.platform != "darwin":
            self.painter.end()


def frame(effect_id, pack_id, t, *, dark=True, transparent=False):
    fx = effects.preview_effect(effect_id) or effects.effect(effect_id)
    found = effects.pack(pack_id)
    palette = found[1] if found is not None else PALETTES[pack_id]
    scene = effects.preview_scene(fx, t, "edge", palette, dark=dark)
    scene_width, scene_height = effects.preview_size("edge")
    width, height = round(scene_width * SCALE), round(scene_height * SCALE)
    bitmap = Bitmap(width, height, scale=SCALE, transparent=transparent, dark=dark)
    if not transparent:
        boundary = effects.Pen()
        boundary.fillStyle = "#030406" if dark else "#d0cec8"
        boundary.fillRect(800, 0, 40, 450)
        bitmap.draw([boundary])
    for pen in scene["pens"]:
        x, y, w, h, _os = max(scene["screens"], key=lambda screen: max(
            0, min(pen.bounds[2], screen[0] + screen[2]) - max(pen.bounds[0], screen[0])
        ) * max(0, min(pen.bounds[3], screen[1] + screen[3]) - max(pen.bounds[1], screen[1])))
        pen.clip = (x, y, x + w, y + h)
    bitmap.draw(scene["pens"])
    if not transparent and scene["pointer"]:
        x, y, _os = scene["pointer"]
        pointer = effects.Pen()
        pointer.fillStyle = "#f7f5ef" if dark else "#24262c"
        pointer.beginPath()
        pointer.moveTo(x, y)
        pointer.lineTo(x + 5, y + 16)
        pointer.lineTo(x + 8, y + 10)
        pointer.lineTo(x + 15, y + 8)
        pointer.closePath()
        pointer.fill()
        bitmap.draw([pointer])
    raw = bitmap.raw()
    bitmap.close()
    return width, height, raw


def render(output):
    output.mkdir(parents=True, exist_ok=True)
    manifest = []
    for direction, effect_id, pack_id in catalogue():
        for dark in (True, False):
            mode = "dark" if dark else "light"
            for t, stage in zip(TIMES, STAGES):
                width, height, rgba = frame(effect_id, pack_id, t, dark=dark)
                stem = f"{effect_id}-{mode}-{stage}"
                (output / f"{stem}.rgba.z").write_bytes(zlib.compress(rgba, 6))
                (output / f"{stem}.png").write_bytes(png_bytes(width, height, rgba))
                if effect_id == "beam":
                    _width, _height, alpha = frame(effect_id, pack_id, t, dark=dark, transparent=True)
                    (output / f"beam-{mode}-{stage}-alpha.rgba.z").write_bytes(zlib.compress(alpha, 6))
                manifest.append({"set": direction, "effect": effect_id, "pack": pack_id, "mode": mode,
                                 "stage": stage, "time": t, "width": width, "height": height})
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Rendered {len(manifest)} RGBA frames with {sys.platform} to {output}")


def compare(windows, mac, output):
    output.mkdir(parents=True, exist_ok=True)
    results = []
    profiles = []
    try:
        import numpy as np
    except ImportError:
        np = None
    for direction, effect_id, pack_id in catalogue():
        frames = []
        strip = bytearray()
        for mode in ("dark", "light"):
            for stage in STAGES:
                stem = f"{effect_id}-{mode}-{stage}"
                win = zlib.decompress((windows / f"{stem}.rgba.z").read_bytes())
                mac_raw = zlib.decompress((mac / f"{stem}.rgba.z").read_bytes())
                if len(win) != len(mac_raw):
                    raise ValueError(f"dimensions differ: {stem}")
                scene_width, scene_height = effects.preview_size("edge")
                width, height = round(scene_width * SCALE), round(scene_height * SCALE)
                edge = round(800 * SCALE)
                left, right = max(0, edge - round(150 * SCALE)), min(width, edge + round(150 * SCALE))
                if np is not None:
                    win_pixels = np.frombuffer(win, dtype=np.uint8).reshape(height, width, 4)
                    mac_pixels = np.frombuffer(mac_raw, dtype=np.uint8).reshape(height, width, 4)
                    pixel_diffs = np.abs(win_pixels[:, left:right].astype(np.int16)
                                          - mac_pixels[:, left:right].astype(np.int16)).max(axis=2)
                    maximum = int(pixel_diffs.max())
                    total = int(pixel_diffs.sum())
                    y, x_local = np.unravel_index(int(pixel_diffs.argmax()), pixel_diffs.shape)
                    x = int(x_local) + left
                    offset = (int(y) * width + x) * 4
                    max_location = (x, int(y), tuple(win[offset:offset + 4]), tuple(mac_raw[offset:offset + 4]))
                    if stage == "push85":
                        for row in range(height):
                            start, end = row * width * 4, (row + 1) * width * 4
                            strip.extend(win[start:end])
                            strip.extend(mac_raw[start:end])
                else:
                    maximum = 0
                    total = 0
                    max_location = None
                    for y in range(height):
                        row = y * width * 4
                        for x in range(left, right):
                            offset = row + x * 4
                            delta = max(abs(win[offset + c] - mac_raw[offset + c]) for c in range(4))
                            total += delta
                            if delta > maximum:
                                maximum = delta
                                max_location = (x, y, tuple(win[offset:offset + 4]), tuple(mac_raw[offset:offset + 4]))
                        if stage == "push85":
                            start, end = row, row + width * 4
                            strip.extend(win[start:end])
                            strip.extend(mac_raw[start:end])
                pixels = (right - left) * height
                frames.append({"mode": mode, "stage": stage, "roi": [left, 0, right, height],
                               "mean_pixel_difference": round(total / pixels, 4), "max_pixel_difference": maximum,
                               "max_pixel_location_and_rgba": max_location})
                if effect_id == "beam":
                    win_alpha = zlib.decompress((windows / f"beam-{mode}-{stage}-alpha.rgba.z").read_bytes())
                    mac_alpha = zlib.decompress((mac / f"beam-{mode}-{stage}-alpha.rgba.z").read_bytes())
                    alpha_radius = round(40 * SCALE)
                    alpha_left, alpha_right = edge - alpha_radius, edge + 1
                    win_profile = [round(sum(win_alpha[(y * width + x) * 4 + 3]
                                             for y in range(height)) / height, 3)
                                   for x in range(alpha_left, alpha_right)]
                    mac_profile = [round(sum(mac_alpha[(y * width + x) * 4 + 3]
                                             for y in range(height)) / height, 3)
                                   for x in range(alpha_left, alpha_right)]
                    profiles.append({"mode": mode, "stage": stage, "x": list(range(alpha_left, alpha_right)),
                                     "windows": win_profile, "mac": mac_profile,
                                     "mean_difference": round(sum(abs(a - b) for a, b in zip(win_profile, mac_profile))
                                                               / len(win_profile), 4),
                                     "max_difference": round(max(abs(a - b) for a, b in zip(win_profile, mac_profile)), 3)})
        results.append({"set": direction, "effect": effect_id, "pack": pack_id, "frames": frames})
        sheet_width, sheet_height = width * 2, height * 2
        (output / f"{effect_id}-side-by-side.png").write_bytes(png_bytes(sheet_width, sheet_height, strip))
    (output / "metrics.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    (output / "beam-alpha-profiles.json").write_text(json.dumps(profiles, indent=2) + "\n", encoding="utf-8")
    for row in results:
        values = row["frames"]
        mean = sum(frame["mean_pixel_difference"] for frame in values) / len(values)
        maximum = max(frame["max_pixel_difference"] for frame in values)
        print(f"{row['effect']:16} {row['pack']:16} mean={mean:7.3f} max={maximum:3}")


def pack_reference(windows, output):
    manifest = []
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for direction, effect_id, pack_id in catalogue():
            if effect_id == "beam":
                for mode in ("dark", "light"):
                    for stage in STAGES:
                        name = f"beam-{mode}-{stage}-alpha.rgba.z"
                        archive.write(windows / name, name)
                continue
            for mode in ("dark", "light"):
                name = f"{effect_id}-{mode}-push85.rgba.z"
                archive.write(windows / name, name)
                manifest.append({"set": direction, "effect": effect_id, "pack": pack_id,
                                 "mode": mode, "stage": "push85", "frame": name})
        archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
    print(f"Packed Windows reference frames to {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("WINDOWS", "MAC"))
    parser.add_argument("--comparison-output", type=Path)
    parser.add_argument("--force-normal", action="store_true")
    parser.add_argument("--scale", type=float, default=SCALE)
    parser.add_argument("--pack-reference", nargs=2, type=Path, metavar=("WINDOWS", "OUTPUT"))
    args = parser.parse_args()
    SCALE = args.scale
    FORCE_NORMAL = args.force_normal
    if args.output:
        render(args.output)
    if args.compare:
        compare(*args.compare, args.comparison_output or args.compare[1] / "side-by-side")
    if args.pack_reference:
        pack_reference(*args.pack_reference)
