"""Build numbered, readable review sheets from every flagged offscreen render.

Qt measurements use logical coordinates; bitmap crops are normalised to 1x.
Each source remains separately catalogued, including repeated states/scales.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from PIL import Image, ImageDraw, ImageFont


def font(size):
    for path in ("/System/Library/Fonts/Helvetica.ttc", "C:/Windows/Fonts/arial.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def source_path(record, root):
    raw = str(record.get("path") or record.get("render") or "")
    candidate = Path(raw)
    if candidate.is_file():
        return candidate
    basename = re.split(r"[/\\]+", raw)[-1]
    platform = record.get("platform", "")
    for candidate in (root / platform / basename, root / basename):
        if candidate.is_file():
            return candidate
    matches = list(root.rglob(basename))
    return matches[0] if len(matches) == 1 else None


def boxes(record, image):
    logical = record.get("size") or record.get("window_size") or list(image.size)
    pixel_size = record.get("pixels") or list(image.size)
    ratio = float(pixel_size[0]) / logical[0] if logical[0] else 1.0
    # Panel/menu images often record their containing window size rather than their
    # own logical size. Their known scale is a safer conversion for those surfaces.
    if "panel" in str(record.get("page", "")) or record.get("page") in ("tray", "dialog"):
        ratio = float(record.get("scale", 1.0))
    width, height = image.width / ratio, image.height / ratio
    seen = set()
    found = []
    omitted = []
    for flag in record.get("flags", []):
        rect = flag.get("rect") or flag.get("bounds") or flag.get("frame")
        if not rect or len(rect) != 4:
            rect = [0, 0, width, min(height, 400)]
        x, y, w, h = (float(value) for value in rect)
        if "frame" in flag and not record.get("is_flipped", True):
            y = height - y - h
        if x + w <= 0 or y + h <= 0 or x >= width or y >= height:
            omitted.append({"reason": "Flagged widget is outside this scrolled bitmap; review its other scroll captures.",
                            "flag": flag})
            continue
        if flag.get("kind") == "outside-parent" and flag.get("name") == "page_column":
            parent_size = flag.get("parent_size", [])
            if len(parent_size) == 2 and w <= parent_size[0] + 1 and x <= 190 and h > parent_size[1]:
                omitted.append({"reason": "A vertically scrolled page column is intentionally taller than its scroll document.",
                                "flag": flag})
                continue
        left, top = max(0, x - 18), max(0, y - 25)
        right, bottom = min(width, x + w + 18), min(height, y + h + 25)
        if right - left <= 620:
            left = max(left, right - 580)
        # Split large regions rather than shrink the text below its actual 1x size.
        for tile_y in range(int(top), max(int(top) + 1, int(bottom)), 260):
            for tile_x in range(int(left), max(int(left) + 1, int(right)), 560):
                bound = (tile_x, tile_y, min(tile_x + 580, int(right)), min(tile_y + 280, int(bottom)))
                if bound in seen or bound[2] <= bound[0] or bound[3] <= bound[1]:
                    continue
                seen.add(bound)
                found.append((bound, flag))
    # Overlapping controls/labels on one row need one contextual crop, while the
    # original flags remain intact in the catalogue.
    merged = []
    for bound, flag in found:
        for index, (held, held_flag) in enumerate(merged):
            union = (min(bound[0], held[0]), min(bound[1], held[1]),
                     max(bound[2], held[2]), max(bound[3], held[3]))
            overlap = min(bound[2], held[2]) > max(bound[0], held[0]) and min(bound[3], held[3]) > max(bound[1], held[1])
            if overlap and union[2] - union[0] <= 590 and union[3] - union[1] <= 280:
                merged[index] = (union, held_flag)
                break
        else:
            merged.append((bound, flag))
    return ratio, merged, omitted


def build(inputs, root, output, platform=None):
    records = []
    for path in inputs:
        content = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(content, dict):
            content = content.get("renders", content.get("records", []))
        for record in content:
            record_platform = record.get("platform") or path.parent.name
            if record.get("flags") and (platform is None or record_platform == platform):
                record = dict(record, platform=record_platform)
                records.append((path, record))
    output.mkdir(parents=True, exist_ok=True)
    catalogue = []
    missing = []
    sheets = []
    page = Image.new("RGB", (1200, 1800), "#ececec")
    draw = ImageDraw.Draw(page)
    title_font = font(12)
    y_positions = [12, 12]
    crops_on_page = 0
    sheet_number = 1

    def flush():
        nonlocal page, draw, y_positions, crops_on_page, sheet_number
        if not crops_on_page:
            return
        name = f"sheet-{sheet_number:04d}.png"
        page.save(output / name)
        sheets.append({"path": str(output / name), "crops": crops_on_page})
        sheet_number += 1
        page = Image.new("RGB", (1200, 1800), "#ececec")
        draw = ImageDraw.Draw(page)
        y_positions = [12, 12]
        crops_on_page = 0

    for number, (measurement, record) in enumerate(records, 1):
        path = source_path(record, root)
        if path is None:
            missing.append({"number": number, "path": record.get("path"), "measurement": str(measurement)})
            continue
        with Image.open(path) as source:
            ratio, regions, omitted = boxes(record, source)
            entry = {"number": number, "path": str(path), "measurement": str(measurement),
                     "platform": record.get("platform"), "page": record.get("page"),
                     "state": record.get("state"), "size": record.get("size"),
                     "scale": record.get("scale"), "flags": record.get("flags"),
                     "omitted": omitted, "crops": []}
            for region_number, (bounds, flag) in enumerate(regions, 1):
                pixel_box = tuple(round(v * ratio) for v in bounds)
                crop = source.crop(pixel_box).convert("RGB")
                crop = crop.resize((bounds[2] - bounds[0], bounds[3] - bounds[1]), Image.Resampling.LANCZOS)
                column = min(range(2), key=lambda i: y_positions[i])
                need = crop.height + 55
                if y_positions[column] + need > 1790 or crops_on_page >= 20:
                    flush()
                    column = 0
                x, y = 10 + 600 * column, y_positions[column]
                label = f"{number}.{region_number} {record.get('page')} {record.get('size')} {record.get('scale')} {record.get('state')}"
                draw.text((x, y), label[:90], fill="#111111", font=title_font)
                draw.text((x, y + 15), path.name[:90], fill="#333333", font=title_font)
                draw.text((x, y + 30), str(flag.get("kind", "flag")), fill="#a02020", font=title_font)
                page.paste(crop, (x, y + 48))
                draw.rectangle((x, y + 48, x + crop.width, y + 48 + crop.height), outline="#888888")
                entry["crops"].append({"id": f"{number}.{region_number}", "sheet": f"sheet-{sheet_number:04d}.png",
                                       "bounds_logical": list(bounds), "kind": flag.get("kind")})
                y_positions[column] += need
                crops_on_page += 1
            catalogue.append(entry)
    flush()
    report = {"flagged_renders": len(records), "catalogued_renders": len(catalogue), "missing": missing,
              "sheets": sheets, "renders": catalogue}
    (output / "catalogue.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"flagged_renders": len(records), "catalogued_renders": len(catalogue),
                      "missing": len(missing), "sheets": len(sheets),
                      "crops": sum(s["crops"] for s in sheets)}))
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("measurements", nargs="+", type=Path)
    parser.add_argument("--renders", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--platform")
    args = parser.parse_args()
    build(args.measurements, args.renders, args.output, args.platform)


if __name__ == "__main__":
    main()
