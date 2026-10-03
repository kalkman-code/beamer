"""Pure-core contracts for the Light, Folio and Selvedge effect sets."""

import colorsys
import math
import statistics
import time
import unittest
from types import SimpleNamespace

from core import effects


IDS = ("aperture", "crease", "pleat", "concertina", "thread", "weave", "jacquard")
PALETTES = {
    "crease": "vellum",
    "pleat": "carbon_copy",
    "concertina": "marbled",
    "thread": "flax",
    "weave": "madder",
    "jacquard": "tide",
}
W, H = 1200.0, 800.0
NOTCH = {"x": 500.0, "y": 0.0, "w": 200.0, "h": 32.0, "r": 10.0}


def palette_for(effect_id, dark):
    pack_id = PALETTES.get(effect_id)
    raw = effects.pack(pack_id) if pack_id else None
    if raw is None:  # Aperture uses the caller's shared spectrum palette.
        raw = ("Spectrum", ["#58c8f0", "#a673ec", "#f05e8a"])
    return effects.legible(raw[1], dark, effects.effect(effect_id))


def edge_departure(edge, along, pressure, *, reduced=False, dark=True, tick=None, since=None, effect_size="medium"):
    if edge in ("left", "right"):
        x, y = (0.0 if edge == "left" else W - 1.0), H * along
        region = {"kind": "edge", "edge": edge, "x": x, "y": 0.0, "w": 1.0, "h": H}
    else:
        x, y = W * along, (0.0 if edge == "top" else H - 1.0)
        region = {"kind": "edge", "edge": edge, "x": 0.0, "y": y, "w": W, "h": 1.0}
    return effects.state(W, H, "mac", "edge", [], reduced=reduced, dark=dark, effect_size=effect_size,
                         region=SimpleNamespace(**region), point=effects.point(x, y), along=along,
                         pressure=pressure, tick=tick, since=since)


def corner_departure(corner, pressure, *, reduced=False, dark=True, tick=None, since=None):
    vertical, horizontal = corner.split("_")
    x, y = (0.0 if horizontal == "left" else W - 1.0), (0.0 if vertical == "top" else H - 1.0)
    region = {"kind": "corner", "edge": horizontal, "corner": corner, "x": x, "y": y, "w": 8.0, "h": 8.0}
    return effects.state(W, H, "mac", "corner", [], reduced=reduced, dark=dark,
                         region=SimpleNamespace(**region), point=effects.point(x, y), along=0.5,
                         pressure=pressure, tick=tick, since=since)


def notch_departure(pressure=0.8, *, reduced=False, dark=True):
    region = {"kind": "notch", "edge": "top", "x": NOTCH["x"], "y": 0.0,
              "w": NOTCH["w"], "h": 1.0}
    return effects.state(W, H, "mac", "notch", [], notch=NOTCH, reduced=reduced, dark=dark,
                         region=SimpleNamespace(**region),
                         point=effects.point(NOTCH["x"] + NOTCH["w"] / 2.0, 0.0), along=0.5,
                         pressure=pressure, tick=None, since=None)


def arrival(edge, x, y, *, notch=True, reduced=False, dark=True, since=0.18, method="edge"):
    return effects.state(W, H, "mac", method, [], reduced=reduced, dark=dark,
                         notch=NOTCH if notch else None, point=effects.point(x, y), edge=edge, since=since)


def coords_and_segment_count(pen):
    count = 0
    for op in pen.ops:
        if op[0] not in ("fill", "stroke"):
            raise AssertionError(f"unsupported drawing operation: {op[0]!r}")
        if isinstance(op[2], effects.Gradient):
            if not all(math.isfinite(value) for value in op[2].geometry):
                raise AssertionError(f"non-finite gradient geometry: {op[2].geometry!r}")
        for segment in op[1]:
            count += 1
            if segment[0] not in ("M", "L", "Q", "C", "Z"):
                raise AssertionError(f"unsupported path command: {segment[0]!r}")
            for value in segment[1:]:
                if not math.isfinite(value):
                    raise AssertionError(f"non-finite path coordinate: {segment!r}")
    return count


def assert_bounded_frame(test, pen):
    test.assertLessEqual(len(pen.ops), 100)
    test.assertLessEqual(coords_and_segment_count(pen), 3000)
    if pen.bounds:
        test.assertTrue(all(math.isfinite(value) for value in pen.bounds))
        test.assertLess(pen.bounds[0], pen.bounds[2])
        test.assertLess(pen.bounds[1], pen.bounds[3])


def benchmark_frame_generation(samples=300):
    """Return median and p95 milliseconds for a representative Folio frame; opt-in diagnostic."""
    fx = effects.effect("concertina")
    s = corner_departure("top_right", 0.8)
    palette = palette_for(fx.id, True)
    timings = []
    for _ in range(samples):
        s.palette = palette
        start = time.perf_counter_ns()
        effects.draw(fx, "depart", s)
        timings.append((time.perf_counter_ns() - start) / 1_000_000)
    ordered = sorted(timings)
    return {"median_ms": statistics.median(timings), "p95_ms": ordered[int(0.95 * (len(ordered) - 1))]}


class EffectSetRegistryTests(unittest.TestCase):
    def test_new_effects_are_recognised_and_hidden_ids_stay_hidden(self):
        self.assertTrue(set(IDS) <= set(effects.EFFECT_IDS))
        for effect_id in IDS:
            with self.subTest(effect=effect_id):
                self.assertIsNotNone(effects.effect(effect_id))
        hidden_ids = {effect_id for module, _name, ids, _packs in effects.ALL_DIRECTIONS
                      if module in effects.HIDDEN for effect_id in ids}
        hidden_packs = {pack_id for module, _name, _ids, packs in effects.ALL_DIRECTIONS
                        if module in effects.HIDDEN for pack_id in packs}
        self.assertTrue(hidden_ids.isdisjoint(effects.EFFECT_IDS))
        self.assertTrue(hidden_packs.isdisjoint(effects.PACK_IDS))

    def test_each_new_pack_is_offered_and_legible_for_both_wallpaper_modes(self):
        for pack_id in effects.PACK_IDS:
            with self.subTest(pack=pack_id):
                self.assertIsNotNone(effects.pack(pack_id))
        for effect_id, pack_id in PALETTES.items():
            with self.subTest(effect=effect_id, pack=pack_id):
                packed = effects.pack(pack_id)
                self.assertIsNotNone(packed)
                self.assertEqual(len(packed[1]), 3)
                for dark in (False, True):
                    colours = effects.legible(packed[1], dark, effects.effect(effect_id))
                    self.assertEqual(len(colours), len(packed[1]))
                    self.assertTrue(all(len(effects.parse_colour(c)) == 4 for c in colours))
                    for colour in colours:
                        lightness = colorsys.rgb_to_hls(*effects.parse_colour(colour)[:3])[1]
                        if dark:
                            self.assertGreaterEqual(lightness, effects.DARK_FLOOR - 1 / 255)
                        else:
                            self.assertLessEqual(lightness, effects.LIGHT_CEILING + 1 / 255)


class EffectFrameContracts(unittest.TestCase):
    def draw_departure(self, effect_id, s):
        s.palette = palette_for(effect_id, s.dark)
        return effects.draw(effects.effect(effect_id), "depart", s)

    def draw_arrival(self, effect_id, s):
        s.palette = palette_for(effect_id, s.dark)
        return effects.draw(effects.effect(effect_id), "arrive", s)

    def test_full_phase_grid_in_both_wallpapers_and_motion_modes(self):
        for effect_id in IDS:
            for dark in (False, True):
                for reduced in (False, True):
                    cases = [edge_departure(edge, along, 0.5, dark=dark, reduced=reduced)
                             for edge in ("left", "right", "top", "bottom") for along in (0, 0.5, 1)]
                    cases.extend(corner_departure(corner, 0.5, dark=dark, reduced=reduced)
                                 for corner in ("top_left", "top_right", "bottom_left", "bottom_right"))
                    cases.append(notch_departure(dark=dark, reduced=reduced))
                    for s in cases:
                        for pressure in (0, 0.01, 0.25, 0.5, 0.75, 1):
                            s.pressure = pressure
                            pen = self.draw_departure(effect_id, s)
                            if pressure:
                                self.assertIsNotNone(pen)
                                assert_bounded_frame(self, pen)
                            else:
                                self.assertIsNone(pen)
                        for since in (0, 0.08, 0.25, 0.55, 0.79):
                            s.since = since
                            pen = self.draw_departure(effect_id, s)
                            if since < effects.effect(effect_id).depart_seconds:
                                self.assertIsNotNone(pen)
                                assert_bounded_frame(self, pen)
                            incoming = arrival(s.region.edge, s.point.x, s.point.y,
                                               notch=s.region.kind == "notch", reduced=reduced,
                                               dark=dark, since=since, method=s.method)
                            pen = self.draw_arrival(effect_id, incoming)
                            if since < effects.effect(effect_id).arrive_seconds:
                                self.assertIsNotNone(pen)
                                assert_bounded_frame(self, pen)

    def test_selected_size_scales_every_new_crossing_effect(self):
        for effect_id in IDS:
            bounds = []
            for size in ("small", "large"):
                pen = self.draw_departure(effect_id, edge_departure("right", 0.5, 1.0, effect_size=size))
                self.assertIsNotNone(pen)
                bounds.append(pen.bounds)
            with self.subTest(effect=effect_id):
                self.assertLess(bounds[1][0], bounds[0][0])

    def test_switch_preview_uses_the_selected_size_for_every_new_effect(self):
        for effect_id in IDS:
            bounds = []
            for size in ("small", "large"):
                scene = effects.preview_switch_scene(effects.effect(effect_id), 0.55, palette_for(effect_id, True),
                                                     effect_size=size)
                self.assertEqual(len(scene["pens"]), 1)
                bounds.append(scene["pens"][0].bounds)
            with self.subTest(effect=effect_id):
                self.assertGreater(bounds[1][3] - bounds[1][1], bounds[0][3] - bounds[0][1])

    def test_crossing_preview_arrival_uses_the_selected_size_for_every_new_effect(self):
        for effect_id in IDS:
            bounds = []
            for size in ("small", "large"):
                scene = effects.preview_scene(effects.effect(effect_id), 2.3, "edge", palette_for(effect_id, True),
                                              effect_size=size)
                self.assertEqual(len(scene["pens"]), 2)
                bounds.append(scene["pens"][-1].bounds)
            with self.subTest(effect=effect_id):
                self.assertGreater(bounds[1][2] - bounds[1][0], bounds[0][2] - bounds[0][0])

    def test_pressure_grid_draws_bounded_frames_on_each_edge_and_along_position(self):
        for effect_id in IDS:
            for edge in ("left", "right", "top", "bottom"):
                for along in (0.0, 0.5, 1.0):
                    for pressure in (0.0, 0.01, 0.25, 0.5, 0.75, 1.0):
                        with self.subTest(effect=effect_id, edge=edge, along=along, pressure=pressure):
                            pen = self.draw_departure(effect_id, edge_departure(edge, along, pressure))
                            if pressure == 0.0:
                                self.assertIsNone(pen)
                            elif pen is not None:
                                assert_bounded_frame(self, pen)

    def test_tick_departures_and_all_corners_draw_bounded_frames(self):
        tick = SimpleNamespace(index=2, age=0.04)
        for effect_id in IDS:
            for corner in ("top_left", "top_right", "bottom_left", "bottom_right"):
                for active_tick in (None, tick):
                    with self.subTest(effect=effect_id, corner=corner, tick=active_tick is not None):
                        pen = self.draw_departure(effect_id, corner_departure(corner, 0.75, tick=active_tick))
                        self.assertIsNotNone(pen)
                        assert_bounded_frame(self, pen)

    def test_notch_departure_and_arrival_with_or_without_hardware_notch(self):
        for effect_id in IDS:
            with self.subTest(effect=effect_id, kind="notch-depart"):
                pen = self.draw_departure(effect_id, notch_departure())
                self.assertIsNotNone(pen)
                assert_bounded_frame(self, pen)
            for present in (False, True):
                with self.subTest(effect=effect_id, kind="notch-arrive", present=present):
                    s = arrival("top", 600.0, 2.0, notch=present)
                    pen = self.draw_arrival(effect_id, s)
                    self.assertIsNotNone(pen)
                    assert_bounded_frame(self, pen)

    def test_reduced_motion_departures_draw_across_edges_corners_and_the_notch(self):
        for effect_id in IDS:
            cases = [edge_departure(edge, 0.5, 0.75, reduced=True)
                     for edge in ("left", "right", "top", "bottom")]
            cases.extend(corner_departure(corner, 0.75, reduced=True)
                         for corner in ("top_left", "top_right", "bottom_left", "bottom_right"))
            cases.append(notch_departure(reduced=True))
            for index, s in enumerate(cases):
                with self.subTest(effect=effect_id, case=index):
                    pen = self.draw_departure(effect_id, s)
                    self.assertIsNotNone(pen)
                    assert_bounded_frame(self, pen)

    def test_edge_and_switch_arrivals_draw_in_light_and_dark_modes(self):
        cases = (("left", 2.0, 400.0), ("right", W - 2.0, 400.0),
                 ("top", 300.0, 2.0), ("bottom", 300.0, H - 2.0))
        for effect_id in IDS:
            for dark in (False, True):
                for edge, x, y in cases:
                    for reduced in (False, True):
                        with self.subTest(effect=effect_id, dark=dark, edge=edge, reduced=reduced):
                            pen = self.draw_arrival(effect_id, arrival(edge, x, y, dark=dark, reduced=reduced))
                            self.assertIsNotNone(pen)
                            assert_bounded_frame(self, pen)
                with self.subTest(effect=effect_id, dark=dark, method="switch"):
                    pen = self.draw_arrival(effect_id, arrival("left", 600.0, 400.0, notch=False,
                                                                dark=dark, method="switch"))
                    self.assertIsNotNone(pen)
                    assert_bounded_frame(self, pen)

    def test_switch_arrivals_near_each_corner_are_finite(self):
        for effect_id in IDS:
            for x, y in ((2.0, 2.0), (W - 2.0, 2.0), (2.0, H - 2.0), (W - 2.0, H - 2.0)):
                with self.subTest(effect=effect_id, point=(x, y)):
                    s = arrival("left", x, y, notch=False, method="switch")
                    pen = self.draw_arrival(effect_id, s)
                    self.assertIsNotNone(pen)
                    assert_bounded_frame(self, pen)

    def test_zero_pressure_and_expired_frames_are_silent(self):
        for effect_id in IDS:
            fx = effects.effect(effect_id)
            with self.subTest(effect=effect_id, phase="zero-pressure"):
                self.assertIsNone(self.draw_departure(effect_id, edge_departure("left", 0.5, 0.0)))
            for kind, duration, s in (
                ("depart", fx.depart_seconds, edge_departure("right", 0.5, 1.0, since=fx.depart_seconds + 0.1)),
                ("arrive", fx.arrive_seconds, arrival("top", 250.0, 2.0, since=fx.arrive_seconds + 0.1)),
            ):
                with self.subTest(effect=effect_id, phase=kind, duration=duration):
                    s.palette = palette_for(effect_id, s.dark)
                    self.assertIsNone(effects.draw(fx, kind, s))

    def test_reduced_motion_keeps_arrival_geometry_fixed_while_opacity_changes(self):
        for effect_id in IDS:
            for method, edge, x, y in (("edge", "left", 2.0, 400.0),
                                       ("switch", "left", 600.0, 400.0)):
                frames = []
                for since in (0.12, 0.32):
                    s = arrival(edge, x, y, notch=False, reduced=True, since=since, method=method)
                    pen = self.draw_arrival(effect_id, s)
                    self.assertIsNotNone(pen)
                    frames.append(pen)
                with self.subTest(effect=effect_id, method=method):
                    self.assertEqual([op[1] for op in frames[0].ops], [op[1] for op in frames[1].ops])
                    self.assertNotEqual([op[3] for op in frames[0].ops], [op[3] for op in frames[1].ops])


if __name__ == "__main__":
    unittest.main()
