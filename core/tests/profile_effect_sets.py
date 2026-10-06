"""Offscreen Qt cost breakdown and before/after render comparison; no app or settings access."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics
import sys
import time

from render_effect_sets import Bitmap, effects, preview


def p95(values):
    return round(sorted(values)[int(len(values) * 0.95)], 3)


class TimedPainter:
    def __init__(self, painter):
        self.painter = painter
        self.cost = defaultdict(float)
        self.calls = Counter()

    def __getattr__(self, name):
        method = getattr(self.painter, name)
        def measured(*args):
            start = time.perf_counter()
            result = method(*args)
            self.cost[name] += (time.perf_counter() - start) * 1000
            self.calls[name] += 1
            return result
        return measured


def profile():
    import effect_overlay
    rows = []
    for effect_id in ("concertina", "jacquard"):
        fx = effects.effect(effect_id)
        palette = effects.pack("marbled" if effect_id == "concertina" else "tide")[1]
        bitmap = Bitmap(3280, 900, 2)
        timed = TimedPainter(bitmap.painter)
        measurements = defaultdict(list)
        counts = Counter()
        original = effect_overlay.qpath
        def path(*args):
            start = time.perf_counter()
            result = original(*args)
            timed.cost["qpath"] += (time.perf_counter() - start) * 1000
            return result
        effect_overlay.qpath = path
        try:
            for i in range(180):
                timed.cost.clear()
                timed.calls.clear()
                start = time.perf_counter()
                scene = effects.preview_scene(fx, 1.1 + (i % 90) * 0.025, "edge", palette, dark=bool(i % 2))
                recorded = time.perf_counter()
                bitmap.clear(bool(i % 2))
                cleared = time.perf_counter()
                effect_overlay.replay(timed, scene["pens"])
                ended = time.perf_counter()
                if i >= 20:
                    measurements["generation"].append((recorded - start) * 1000)
                    measurements["clear"].append((cleared - recorded) * 1000)
                    measurements["replay"].append((ended - cleared) * 1000)
                    measurements["total"].append((ended - start) * 1000)
                    for name in ("qpath", "fillPath", "strokePath", "setOpacity", "setCompositionMode"):
                        measurements[name].append(timed.cost[name])
                    counts.update(timed.calls)
                    counts.update({"segments": sum(len(op[1]) for pen in scene["pens"] for op in pen.ops),
                                   "gradients": sum(isinstance(op[2], effects.Gradient) for pen in scene["pens"] for op in pen.ops)})
        finally:
            effect_overlay.qpath = original
            bitmap.close()
        rows.append(dict(effect=effect_id,
                         p95_ms={k: p95(v) for k, v in measurements.items()},
                         mean_ms={k: round(statistics.mean(v), 3) for k, v in measurements.items()},
                         calls_per_frame={k: round(v / 160, 2) for k, v in counts.items()}))
    print(json.dumps(rows, indent=2))


def renders(folder, compare, scale):
    from PIL import Image, ImageChops
    folder.mkdir(parents=True, exist_ok=True)
    frames, changed, maximum, total_difference = 0, 0, 0, 0
    for effect_id in ("concertina", "jacquard"):
        fx = effects.effect(effect_id)
        for pack in (("vellum", "carbon_copy", "marbled") if effect_id == "concertina" else ("flax", "madder", "tide")):
            palette = effects.pack(pack)[1]
            for dark in (True, False):
                for method in ("edge", "corner", "notch", "switch"):
                    for index, t in enumerate((0.92, 1.35, 1.78, 2.18, 2.27, 2.44, 2.7)):
                        picture = preview(fx, t, palette, dark, method, scale=scale)
                        name = f"{effect_id}-{pack}-{dark}-{method}-{index}.png"
                        target = folder / name
                        frames += 1
                        if compare:
                            with Image.open(target) as before:
                                if before.size != picture.size:
                                    raise ValueError(f"Render dimensions differ: {name}")
                                difference = ImageChops.difference(before, picture)
                                histogram = difference.histogram()
                                peak = max(max(pair) for pair in difference.getextrema())
                                maximum = max(maximum, peak)
                                total_difference += sum((i % 256) * n for i, n in enumerate(histogram))
                                if difference.getbbox():
                                    changed += 1
                                    picture.save(folder / name.replace(".png", "-after.png"))
                        else:
                            picture.save(target)
    print(json.dumps(dict(frames=frames, scale=scale, changed_frames=changed, maximum_channel_difference=maximum,
                          total_channel_difference=total_difference), indent=2))
    if compare and changed:
        raise SystemExit("Renders differ")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--renders", type=Path)
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--scale", type=float, default=0.6)
    args = parser.parse_args()
    if args.profile:
        if sys.platform == "darwin":
            parser.error("--profile measures the Qt replay")
        profile()
    if args.renders:
        renders(args.renders, args.compare, args.scale)
