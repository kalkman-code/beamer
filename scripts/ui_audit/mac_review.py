"""Contact sheets of every flagged AppKit text crop; PNG originals stay untouched."""
import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]


def main():
    folder = ROOT / "renders/mac"
    action_mode = "--actions" in sys.argv
    output = folder / ("action-review" if action_mode else "review")
    output.mkdir(exist_ok=True)
    entries = []
    files = ("actions.json",) if action_mode else ("measurements.json", "state-measurements.json")
    for file in files:
        for row in json.loads((folder / file).read_text()):
            if action_mode:
                if not row.get("renders"):
                    continue
                row = {**row, "page": row["page"] + "-full", "width": 640, "scale": 1,
                       "path": row["renders"][1], "state": f"{row['label']} round {row['round']}", "is_flipped": True}
            for flag in row["flags"]:
                if flag["class"] == "NSButtonTextField":
                    continue
                entries.append((row, flag))
    index = []
    for batch_start in range(0, len(entries), 32):
        batch = entries[batch_start:batch_start + 32]
        sheet = Image.new("RGB", (1600, math.ceil(len(batch) / 4) * 135), "#303030")
        draw = ImageDraw.Draw(sheet)
        for offset, (row, flag) in enumerate(batch):
            image = Image.open(row["path"]).convert("RGB")
            scale = row["scale"]
            x, y, width, height = flag["frame"]
            if not row.get("is_flipped", row["page"].endswith("-full")):
                y = image.height / scale - y - height
            left, top = max(0, x - 15), max(0, y - 15)
            right, bottom = min(image.width / scale, x + width + 15), min(image.height / scale, y + height + 15)
            pos = ((offset % 4) * 400, (offset // 4) * 135)
            if right > left and bottom > top:
                crop = image.crop(tuple(round(v * scale) for v in (left, top, right, bottom)))
                crop.thumbnail((390, 90))
                sheet.paste(crop, (pos[0] + 4, pos[1] + 42))
            else:
                draw.text((pos[0] + 4, pos[1] + 60), "Below visible scroller; inspect -full PNG", fill="white")
            title = f"{batch_start + offset}: {row['page']} {row['width']} {scale}x"
            draw.text((pos[0] + 4, pos[1] + 3), title, fill="white")
            draw.text((pos[0] + 4, pos[1] + 19), row["state"], fill="white")
            index.append({"index": batch_start + offset, "original": row["path"], "flag": flag,
                          "sheet": str(output / f"flags-{batch_start // 32:03d}.png")})
        sheet.save(output / f"flags-{batch_start // 32:03d}.png")
    (output / "index.json").write_text(json.dumps(index, indent=2))
    print(f"{len(entries)} flagged crops in {math.ceil(len(entries) / 32)} contact sheets")


if __name__ == "__main__":
    main()
