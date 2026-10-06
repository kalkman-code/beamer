"""Offscreen judging sequences and native frame timings; never constructs an app window.

Run with the Mac venv and --output PATH for renders, or either platform's venv and --benchmark.
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ("mac_app" if sys.platform == "darwin" else "win_app")))

from core import effects

NEW_IDS = ("aperture", "crease", "pleat", "concertina", "thread", "weave", "jacquard")


class Bitmap:
    def __init__(self, width, height, scale=1):
        self.width, self.height = width, height
        if sys.platform == "darwin":
            import Quartz as cg
            from effects_overlay import replay
            self.cg, self.replay = cg, replay
            self.data = bytearray(width * height * 4)
            self.ctx = cg.CGBitmapContextCreate(self.data, width, height, 8, width * 4,
                                              cg.CGColorSpaceCreateWithName(cg.kCGColorSpaceSRGB),
                                              cg.kCGImageAlphaPremultipliedLast | cg.kCGBitmapByteOrder32Big)
            cg.CGContextTranslateCTM(self.ctx, 0, height)
            cg.CGContextScaleCTM(self.ctx, scale, -scale)
        else:
            from PySide6.QtGui import QImage, QPainter
            from effect_overlay import replay
            self.replay = replay
            self.image = QImage(width, height, QImage.Format.Format_RGBA8888_Premultiplied)
            self.painter = QPainter(self.image)
            self.painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            self.painter.scale(scale, scale)

    def clear(self, dark):
        rgb = (0.065, 0.075, 0.09) if dark else (0.93, 0.92, 0.89)
        if sys.platform == "darwin":
            self.cg.CGContextSetRGBFillColor(self.ctx, *rgb, 1)
            self.cg.CGContextFillRect(self.ctx, ((-4000, -4000), (8000, 8000)))
        else:
            from PySide6.QtGui import QColor
            self.painter.fillRect(-4000, -4000, 8000, 8000, QColor.fromRgbF(*rgb))

    def draw(self, pens):
        if sys.platform == "darwin":
            self.replay(self.cg, self.ctx, pens)
        else:
            self.replay(self.painter, pens)

    def pil(self):
        from PIL import Image
        if sys.platform == "darwin":
            return Image.frombytes("RGBA", (self.width, self.height), bytes(self.data)).convert("RGB")
        return Image.frombytes("RGBA", (self.width, self.height), bytes(self.image.bits())).convert("RGB")

    def close(self):
        if sys.platform != "darwin":
            self.painter.end()


def catalogue():
    from tokens import PALETTES
    yield "light", tuple(effects.CLASSIC), ("signal", "Signal", PALETTES["signal"])
    for _module, name, ids, packs in effects.DIRECTIONS:
        pack_name, palette = effects.pack(packs[0])
        yield name.lower(), ids, (packs[0], pack_name, palette)


def preview(fx, t, palette, dark, method="edge", scale=0.6):
    scene = (effects.preview_switch_scene(fx, max(0, t - 1.85), palette, dark=dark) if method == "switch"
             else effects.preview_scene(fx, t, method, palette, dark=dark))
    width, height = effects.preview_size(method)
    bitmap = Bitmap(round(width * scale), round(height * scale), scale)
    bitmap.clear(dark)
    if method != "switch":
        # Display boundaries belong to the judging scene, never to the effect itself.
        pen = effects.Pen()
        pen.fillStyle = "#030406" if dark else "#d0cec8"
        if method == "notch":
            pen.fillRect(0, 450, 800, 40)
        else:
            pen.fillRect(800, 0, 40, 450)
        bitmap.draw([pen])
    for pen in scene["pens"]:
        def overlap(screen):
            x, y, w, h, _os = screen
            a, b, c, d = pen.bounds
            return max(0, min(c, x + w) - max(a, x)) * max(0, min(d, y + h) - max(b, y))
        x, y, w, h, _os = max(scene["screens"], key=overlap)
        pen.clip = (x, y, x + w, y + h)
    bitmap.draw(scene["pens"])
    if scene["notch"]:
        pen = effects.Pen()
        pen.fillStyle = "#000000"
        effects.round_rect(pen, *scene["notch"])
        pen.fill()
        bitmap.draw([pen])
    pointer = scene["pointer"]
    if pointer:
        x, y, _os = pointer
        pen = effects.Pen()
        pen.fillStyle = "#f7f5ef" if dark else "#24262c"
        pen.beginPath()
        pen.moveTo(x, y)
        pen.lineTo(x + 5, y + 16)
        pen.lineTo(x + 8, y + 10)
        pen.lineTo(x + 15, y + 8)
        pen.closePath()
        pen.fill()
        bitmap.draw([pen])
    result = bitmap.pil()
    bitmap.close()
    return result


def render(output):
    from PIL import Image, ImageDraw, ImageFont
    output.mkdir(parents=True, exist_ok=True)
    font = ImageFont.truetype(str(ROOT / "win_app/assets/HankenGrotesk-Variable.ttf"), 17)
    times = (0.92, 1.35, 1.78, 2.18, 2.27, 2.44, 2.7)
    stages = ("Push · 10%", "Push · 45%", "Push · 85%", "Threshold", "Give", "Arrival", "Settle")
    total = 0
    for direction, ids, (_pack_id, pack_name, palette) in catalogue():
        for effect_id in ids:
            fx = effects.preview_effect(effect_id)
            for dark, method in ((dark, method) for dark in (True, False)
                                 for method in (("edge", "corner", "notch", "switch")
                                                if effect_id in NEW_IDS else ("edge",))):
                mode = "dark" if dark else "light"
                suffix = "" if method == "edge" else f"-{method}"
                stem = f"{direction}-{effect_id}-{mode}{suffix}"
                background, foreground = ("#101318", "#e5e8ef") if dark else ("#eeebe3", "#26272c")
                frames = []
                for i in range(56):
                    t = 0.7 + i * 0.05
                    picture = preview(fx, t, palette, dark, method)
                    frame = Image.new("RGB", (picture.width, picture.height + 42), background)
                    frame.paste(picture, (0, 42))
                    label = f"{direction.title()} / {fx.name}   ·   {method.title()}   ·   {pack_name}"
                    ImageDraw.Draw(frame).text((16, 10), label, font=font, fill=foreground)
                    frames.append(frame)
                frames[0].save(output / f"{stem}.gif", save_all=True, append_images=frames[1:],
                               duration=50, loop=0, disposal=2)
                strip = Image.new("RGB", (336 * len(times), 256), background)
                draw = ImageDraw.Draw(strip)
                for i, (t, stage) in enumerate(zip(times, stages)):
                    # A crop around the crossing keeps quiet effects readable at their true scale.
                    box = {"edge": (324, 24, 660, 240), "corner": (324, 0, 660, 216),
                           "notch": (72, 180, 408, 396), "switch": (72, 0, 408, 216)}[method]
                    crop = preview(fx, t, palette, dark, method).crop(box)
                    strip.paste(crop, (336 * i, 40))
                    label = stage if method != "switch" else ("Waiting" if t < 2.2 else stage)
                    draw.text((336 * i + 12, 10), label, font=font, fill=foreground)
                strip.save(output / f"{stem}-strip.png")
                total += 2
    print(f"Rendered {total} files to {output}")
    judging_index(output)


def judging_index(output):
    from html import escape
    parts = ['''<!doctype html><html lang="en-GB"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Beamer crossing effects</title><style>
body{margin:0 auto;padding:36px 24px;max-width:1500px;background:#17191c;color:#ede9df;
font:16px/1.5 system-ui,sans-serif}h1{font-size:32px}h2{margin:48px 0 8px}h3{margin:28px 0 4px}
p{max-width:75ch;color:#c0bbae}a{color:#d0c299}figure{margin:0}img{width:100%;display:block}
.pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}figcaption{padding:8px 0}
details{margin:12px 0}summary{cursor:pointer}.packs{display:flex;gap:24px;flex-wrap:wrap}
.swatch{display:inline-block;width:22px;height:22px;vertical-align:middle;margin-right:3px}
@media(max-width:700px){.pair{grid-template-columns:1fr}}
</style><h1>Beamer crossing effects</h1>
<p>Offscreen native renders. Light, Folio and Selvedge come first; the existing directions follow
for comparison. Each loop shows pressure building, breakthrough and arrival.</p>''']
    entries = list(catalogue())
    entries.sort(key=lambda row: (row[0] not in ("light", "folio", "selvedge"),
                                  {"light": 0, "folio": 1, "selvedge": 2}.get(row[0], 3)))
    for direction, ids, (_pack, _name, _palette) in entries:
        parts.append(f"<h2>{escape(direction.title())}</h2>")
        packs = next((row[3] for row in effects.DIRECTIONS if row[1].lower() == direction), ())
        parts.append('<div class="packs">')
        for pack_id in packs:
            name, colours = effects.pack(pack_id)
            swatches = ''.join(f'<span class="swatch" style="background:{c}"></span>' for c in colours)
            parts.append(f'<span>{swatches} {escape(name)}</span>')
        parts.append('</div>')
        for effect_id in ids:
            fx = effects.preview_effect(effect_id)
            parts.append(f'<h3>{escape(fx.name)} · {escape(fx.intensity.title())}</h3><p>{escape(fx.blurb)}</p>')
            methods = ("edge", "corner", "notch", "switch") if effect_id in NEW_IDS else ("edge",)
            for method in methods:
                if method != "edge":
                    parts.append(f'<details><summary>{method.title()}</summary>')
                parts.append('<div class="pair">')
                for mode in ("dark", "light"):
                    suffix = '' if method == 'edge' else f'-{method}'
                    stem = f'{direction}-{effect_id}-{mode}{suffix}'
                    parts.append(f'<figure><img loading="lazy" src="{stem}.gif" alt="{escape(fx.name)} on {mode} wallpaper">'
                                 f'<figcaption>{mode.title()} · <a href="{stem}-strip.png">Frame strip</a></figcaption></figure>')
                parts.append('</div>')
                if method != "edge":
                    parts.append('</details>')
    parts.append('</html>')
    (output / 'index.html').write_text('\n'.join(parts), encoding='utf-8')


def benchmark():
    results = []
    for effect_id in NEW_IDS:
        fx = effects.preview_effect(effect_id)
        palette = effects.pack("marbled" if effect_id in ("crease", "pleat", "concertina") else "tide")[1]
        generation, native = [], []
        bitmap = Bitmap(3280, 900, 2)
        for i in range(180):
            dark = bool(i % 2)
            t = 1.1 + (i % 90) * 0.025
            start = time.perf_counter()
            scene = effects.preview_scene(fx, t, "edge", palette, dark=dark)
            recorded = time.perf_counter()
            bitmap.clear(dark)
            bitmap.draw(scene["pens"])
            ended = time.perf_counter()
            if i >= 20:
                generation.append((recorded - start) * 1000)
                native.append((ended - start) * 1000)
        bitmap.close()
        def p95(values):
            return round(sorted(values)[int(len(values) * 0.95)], 3)
        results.append(dict(effect=effect_id, record_p95_ms=p95(generation),
                            total_p95_ms=p95(native), total_median_ms=round(statistics.median(native), 3)))
    print(json.dumps(dict(platform=sys.platform, scale=2, budget_record_ms=2, budget_total_ms=8,
                          frames_per_effect=160, results=results), indent=2))
    if any(row["record_p95_ms"] >= 2 or row["total_p95_ms"] >= 8 for row in results):
        raise SystemExit("Frame budget exceeded")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--benchmark", action="store_true")
    args = parser.parse_args()
    if args.output:
        render(args.output)
    if args.benchmark:
        benchmark()
