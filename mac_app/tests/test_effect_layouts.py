"""Every effect on one display of many: monitor layouts side by side, stacked, staggered, left of or
above the primary, portrait, mixed sizes and three displays. Each move plays on the display the
pointer is on and no other, framed as that display alone, and lands where the pointer lands.
Byte-identical in mac_app and win_app."""

import colorsys
import unittest
from types import SimpleNamespace
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import effects

PALETTE = ["#ff7828", "#ff3264", "#ffb43c"]
NOTCH = {"x": 771.5, "y": 0.0, "w": 185.0, "h": 32.0, "r": 10.0}

# Desktop-relative (x, y, w, h), as both overlays hand them to the Player. The first display is the
# one with the notch, at the top middle of it.
LAYOUTS = {
    "one": [(0.0, 0.0, 1728.0, 1117.0)],
    "side by side": [(0.0, 0.0, 1728.0, 1117.0), (1728.0, 0.0, 2560.0, 1440.0)],
    "stacked": [(416.0, 1440.0, 1728.0, 1117.0), (0.0, 0.0, 2560.0, 1440.0)],
    "staggered": [(0.0, 400.0, 1728.0, 1117.0), (1728.0, 0.0, 1920.0, 1080.0)],
    "left of the primary": [(1920.0, 0.0, 1728.0, 1117.0), (0.0, 300.0, 1920.0, 1080.0)],
    "portrait": [(0.0, 300.0, 1728.0, 1117.0), (1728.0, 0.0, 1080.0, 1920.0)],
    "three": [(1920.0, 200.0, 1728.0, 1117.0), (0.0, 0.0, 1920.0, 1080.0), (3648.0, 0.0, 1280.0, 1024.0)],
    "150% beside 100%": [(0.0, 0.0, 2560.0, 1440.0), (2560.0, 0.0, 1706.67, 960.0)],
}


def box(displays):
    return (max(x + w for x, _y, w, _h in displays), max(y + h for _x, y, _w, h in displays))


def notch_on(displays):
    x, y, _w, _h = displays[0]
    return dict(NOTCH, x=x + NOTCH["x"], y=y)


def edge_region(display, edge, desktop=None):
    """The engine's strip for `edge` of `display`; `desktop` stretches it along the whole box, as
    the Mac's engine measures an edge."""
    x, y, w, h = display
    if desktop is not None and edge in ("left", "right"):
        y, h = 0.0, desktop[1]
    if desktop is not None and edge in ("top", "bottom"):
        x, w = 0.0, desktop[0]
    strips = {"left": (x, y, 1.0, h), "right": (x + w - 1.0, y, 1.0, h), "top": (x, y, w, 1.0), "bottom": (x, y + h - 1.0, w, 1.0)}
    rx, ry, rw, rh = strips[edge]
    return {"kind": "edge", "edge": edge, "x": rx, "y": ry, "w": rw, "h": rh}


def corner_region(display, corner):
    x, y, w, h = display
    vertical, horizontal = corner.split("_")
    return {"kind": "corner", "edge": horizontal, "corner": corner, "x": x if horizontal == "left" else x + w - 8.0,
            "y": y if vertical == "top" else y + h - 8.0, "w": 8.0, "h": 8.0}


def corner_point(display, corner):
    x, y, w, h = display
    vertical, horizontal = corner.split("_")
    return (x if horizontal == "left" else x + w - 1.0, y if vertical == "top" else y + h - 1.0)


def moves(display, desktop, first):
    """(name, a function that feeds a Player at `now`) for every move on `display`."""
    x, y, w, h = display
    out = []
    for edge in ("left", "right", "top", "bottom"):
        region = edge_region(display, edge, desktop)
        own = edge_region(display, edge)
        for along in (0.0, 0.5, 1.0):
            at = (own["x"] + own["w"] * along if edge in ("top", "bottom") else own["x"],
                  own["y"] + own["h"] * along if edge in ("left", "right") else own["y"])
            at = (min(at[0], x + w - 1.0), min(at[1], y + h - 1.0))
            out.append((f"push {edge} {along}", lambda p, now, region=region, at=at: p.push(now, "edge", region, at, 0.6, 2, display=display)))
            out.append((f"cross {edge} {along}", lambda p, now, region=region, at=at: (p.push(now, "edge", region, at, 1.0, display=display), p.cross(now))))
            out.append((f"arrive {edge} {along}", lambda p, now, at=at, edge=edge: p.arrive(now, "edge", at, edge, display)))
        third = dict(own)
        if edge in ("left", "right"):
            third.update(y=y + 2.0 * h / 3.0, h=h / 3.0)
            at = (own["x"], y + 5.0 * h / 6.0)
        else:
            third.update(x=x + 2.0 * w / 3.0, w=w / 3.0)
            at = (x + 5.0 * w / 6.0, own["y"])
        out.append((f"part {edge}", lambda p, now, third=third, at=at: p.push(now, "edge", third, at, 0.6, 2, display=display)))
    for corner in ("top_left", "top_right", "bottom_left", "bottom_right"):
        region, at = corner_region(display, corner), corner_point(display, corner)
        out.append((f"corner {corner}", lambda p, now, region=region, at=at: (p.push(now, "corner", region, at, 1.0, display=display), p.cross(now))))
        out.append((f"switch near {corner}", lambda p, now, at=at: p.switched(now - effects.SWITCH_SETTLE_S, at, "left", None, display)))
    if first:
        region = {"kind": "notch", "edge": "top", "x": x + NOTCH["x"], "y": y, "w": NOTCH["w"], "h": 1.0}
        at = (x + NOTCH["x"] + NOTCH["w"] / 2.0, y)
        out.append(("notch", lambda p, now, region=region, at=at: (p.push(now, "notch", region, at, 1.0, display=display), p.cross(now))))
        out.append(("arrive under the notch", lambda p, now, at=(x + w / 2.0, y + 2.0): p.arrive(now, "edge", at, "top", display)))
    return out


def inside(box_, display):
    x, y, w, h = display
    return box_[0] >= x and box_[1] >= y and box_[2] <= x + w and box_[3] <= y + h


class EveryLayoutTests(unittest.TestCase):
    def test_every_move_draws_only_on_the_pointers_display(self):
        styles = [effects.effect(effect_id) for effect_id in effects.EFFECT_IDS] + [effects.LOCATOR]
        for layout, displays in LAYOUTS.items():
            desktop = box(displays)
            notch = notch_on(displays)
            for index, display in enumerate(displays):
                edge = (display[0], display[1], display[0] + display[2], display[1] + display[3])
                for name, feed in moves(display, desktop, index == 0):
                    for fx in styles:
                        if fx is effects.LOCATOR and not name.startswith("switch"):
                            continue
                        with self.subTest(layout=layout, display=index, move=name, effect=fx.id):
                            player = effects.Player()
                            feed(player, 10.0)
                            drawn = 0
                            for step in (0, 14, 35):
                                for pen in player.frames(10.0 + step / 60.0, fx, (desktop[0], desktop[1], "mac"),
                                                         PALETTE, False, True, notch):
                                    drawn += 1
                                    self.assertEqual(pen.clip, edge)
                                    self.assertTrue(inside(pen.bounds, display), pen.bounds)
                            self.assertGreater(drawn, 0)

    def test_an_effect_is_framed_as_its_display_alone(self):
        seen = []
        fx = effects.Effect("probe", "Probe", "quiet", "", lambda pen, s: None,
                            lambda pen, s: seen.append((s.screen.w, s.screen.h, s.point.x, s.point.y, s.edge)), 0.0, 0.5)
        player = effects.Player()
        player.arrive(0.0, "edge", (1730.0, 500.0), "left", (1728.0, 0.0, 1920.0, 1080.0))
        player.frames(0.1, fx, (3648.0, 1517.0, "mac"), PALETTE, False, True)
        self.assertEqual(seen, [(1920.0, 1080.0, 2.0, 500.0, "left")])

    def test_an_edge_strip_along_the_whole_desktop_is_cut_to_the_display(self):
        seen = []
        fx = effects.Effect("probe", "Probe", "quiet", "", lambda pen, s: seen.append(vars(s.region)),
                            lambda pen, s: None, 0.5, 0.0)
        player = effects.Player()
        display = (1728.0, 0.0, 1920.0, 1080.0)
        player.push(0.0, "edge", {"kind": "edge", "edge": "right", "x": 3647.0, "y": 0.0, "w": 1.0, "h": 1517.0},
                    (3647.0, 500.0), 0.5, display=display)
        player.frames(0.0, fx, (3648.0, 1517.0, "mac"), PALETTE, False, True)
        self.assertEqual((seen[0]["x"], seen[0]["y"], seen[0]["h"]), (1919.0, 0.0, 1080.0))

    def test_the_notch_is_only_seen_on_its_own_display(self):
        seen = []
        fx = effects.Effect("probe", "Probe", "quiet", "", lambda pen, s: None,
                            lambda pen, s: seen.append(getattr(s, "notch", None)), 0.0, 0.5)
        displays = LAYOUTS["left of the primary"]
        for display in displays:
            player = effects.Player()
            player.arrive(0.0, "edge", (display[0] + display[2] / 2.0, display[1] + 2.0), "top", display)
            player.frames(0.1, fx, (3648.0, 1380.0, "mac"), PALETTE, False, True, notch_on(displays))
        self.assertEqual((seen[0].x, seen[0].y), (NOTCH["x"], 0.0))
        self.assertIsNone(seen[1])


class AuditFindingTests(unittest.TestCase):
    def test_a_departure_carries_no_tick_from_its_push(self):
        seen = []
        fx = effects.Effect("probe", "Probe", "quiet", "", lambda pen, s: seen.append(s.tick), lambda pen, s: None, 0.5, 0.0)
        player = effects.Player()
        region = edge_region((0.0, 0.0, 1728.0, 1117.0), "right")
        player.push(0.0, "edge", region, (1727.0, 500.0), 0.8, 3)
        player.frames(0.01, fx, (1728.0, 1117.0, "mac"), PALETTE, False, True)
        player.cross(0.02)
        player.frames(0.05, fx, (1728.0, 1117.0, "mac"), PALETTE, False, True)
        self.assertEqual((seen[0].index, seen[1]), (3, None))

    def test_a_push_draining_between_events_stays_on_its_display(self):
        player = effects.Player()
        display = (1728.0, 0.0, 1920.0, 1080.0)
        region = edge_region(display, "right")
        player.push(0.0, "edge", region, (3647.0, 500.0), 0.6, display=display)
        player.push(0.1, "edge", region, (3647.0, 500.0), 0.4)
        self.assertEqual(player.depart["display"], display)

    def test_a_gauge_switch_near_a_corner_draws_no_corner_register(self):
        def ops(at):
            s = effects.state(1728.0, 1117.0, "mac", "switch", PALETTE, point=effects.point(*at),
                              edge=effects.nearest_edge(at[0], at[1], 1728.0, 1117.0), since=0.2)
            return len(effects.draw(effects.effect("gauge"), "arrive", s).ops)
        self.assertEqual(ops((5.0, 5.0)), ops((800.0, 500.0)))


class LegibleColourTests(unittest.TestCase):
    @staticmethod
    def lightness(colour):
        r, g, b, _a = effects.parse_colour(colour)
        return colorsys.rgb_to_hls(r, g, b)[1]

    def test_dark_colours_are_lifted_on_dark_and_keep_their_hue(self):
        ink = effects.pack("indigo_ink")[1]
        lifted = effects.legible(ink, True, effects.effect("flint"))
        self.assertTrue(all(self.lightness(c) >= effects.DARK_FLOOR - 0.01 for c in lifted))
        before = colorsys.rgb_to_hls(*effects.parse_colour(ink[0])[:3])[0]
        after = colorsys.rgb_to_hls(*effects.parse_colour(lifted[0])[:3])[0]
        self.assertAlmostEqual(before, after, delta=0.02)
        self.assertEqual(lifted[-1], ink[-1])

    def test_pale_colours_come_down_on_light_only_for_effects_that_draw_them_as_given(self):
        pearl = effects.pack("pearl")[1]
        for effect_id in ("hyperdrive", "capillary"):
            drawn = effects.legible(pearl, False, effects.effect(effect_id))
            self.assertTrue(all(self.lightness(c) <= effects.LIGHT_CEILING + 0.01 for c in drawn))
        for effect_id in ("film", "flint", "gauge"):
            self.assertEqual(effects.legible(pearl, False, effects.effect(effect_id)), list(pearl))


class DisplayForTests(unittest.TestCase):
    DISPLAYS = LAYOUTS["staggered"]

    def test_a_point_on_a_display_stays_where_it_is(self):
        self.assertEqual(effects.display_for((1800.0, 20.0), self.DISPLAYS), (self.DISPLAYS[1], (1800.0, 20.0)))

    def test_a_point_on_no_display_lands_on_the_nearest(self):
        # Above the shorter display, where an arrival along the staggered desktop's top edge lands.
        display, at = effects.display_for((500.0, 2.0), self.DISPLAYS)
        self.assertEqual((display, at), (self.DISPLAYS[0], (500.0, 400.0)))

    def test_the_far_edge_is_inside(self):
        display, at = effects.display_for((5000.0, 5000.0), self.DISPLAYS)
        self.assertEqual((display, at), (self.DISPLAYS[1], (3647.0, 1079.0)))


class InkCornerTests(unittest.TestCase):
    def test_every_corner_draws_on_screen(self):
        w, h = 1728.0, 1117.0
        for effect_id in ("capillary", "viscous_drop", "sumi_bloom"):
            for corner in ("top_left", "top_right", "bottom_left", "bottom_right"):
                for reduced in (False, True):
                    with self.subTest(effect=effect_id, corner=corner, reduced=reduced):
                        display = (0.0, 0.0, w, h)
                        region = corner_region(display, corner)
                        s = effects.state(w, h, "mac", "corner", PALETTE, reduced=reduced, region=SimpleNamespace(**region),
                                          point=effects.point(*corner_point(display, corner)), along=0.0, pressure=0.8,
                                          tick=None, since=None)
                        pen = effects.draw(effects.effect(effect_id), "depart", s)
                        points = [(seg[-2], seg[-1]) for op in pen.ops for seg in op[1] if len(seg) > 1]
                        on = [p for p in points if -1.0 <= p[0] <= w + 1.0 and -1.0 <= p[1] <= h + 1.0]
                        self.assertGreater(len(on) / len(points), 0.9)


class CornerSideTests(unittest.TestCase):
    def test_a_corner_draws_the_same_whichever_of_its_sides_leads_to_the_other_machine(self):
        # The region's edge is the side the other machine is on: the corner's horizontal side on
        # a machine beside it, its vertical side on one above or below.
        w, h = 2560.0, 1440.0
        display = (0.0, 0.0, w, h)
        for effect_id in effects.EFFECT_IDS:
            for corner in ("top_left", "top_right", "bottom_left", "bottom_right"):
                sizes = []
                for side in corner.split("_"):
                    region = dict(corner_region(display, corner), edge=side)
                    s = effects.state(w, h, "windows", "corner", PALETTE, region=SimpleNamespace(**region),
                                      point=effects.point(*corner_point(display, corner)), along=0.0, pressure=0.8,
                                      tick=None, since=None)
                    b = effects.draw(effects.effect(effect_id), "depart", s).bounds
                    sizes.append((round(min(b[2], w) - max(b[0], 0.0)), round(min(b[3], h) - max(b[1], 0.0))))
                with self.subTest(effect=effect_id, corner=corner):
                    self.assertEqual(sizes[0], sizes[1])


class ClassicPreviewTests(unittest.TestCase):
    def test_glow_and_beam_play_at_every_place_the_preview_shows(self):
        for style in ("glow", "beam"):
            fx = effects.preview_effect(style)
            for method in ("edge", "corner"):
                with self.subTest(style=style, method=method):
                    pushing = effects.preview_scene(fx, 1.8, method, PALETTE)
                    through = effects.preview_scene(fx, 2.3, method, PALETTE)
                    self.assertTrue(pushing["pens"])
                    self.assertTrue(through["pens"])
            # The Mac's notch style plays at the notch instead.
            self.assertFalse(effects.preview_scene(fx, 1.8, "notch", PALETTE)["pens"])

    def test_a_corner_lights_both_its_walls_on_the_mac_screen(self):
        fx = effects.preview_effect("glow")
        pen = effects.preview_scene(fx, 2.0, "corner", PALETTE)["pens"][0]
        w, h = effects.PREVIEW_SCREEN
        self.assertLess(pen.bounds[0], w - effects.CLASSIC_ARM + 10)
        self.assertGreater(pen.bounds[3], effects.CLASSIC_ARM - 10)
        self.assertLessEqual(pen.bounds[2], w + 4)

    def test_aperture_is_a_light_effect_and_has_a_preview(self):
        fx = effects.preview_effect("aperture")
        self.assertEqual((fx.id, fx.intensity), ("aperture", "showpiece"))
        self.assertEqual(effects.switch_effect("aperture", "glow").id, "aperture")
        self.assertTrue(effects.preview_scene(fx, 1.8, "edge", PALETTE)["pens"])

    def test_only_the_styles_have_a_preview(self):
        self.assertIsNone(effects.preview_effect("nonsense"))
        self.assertEqual(effects.preview_effect("flint").id, "flint")


class LengthTests(unittest.TestCase):
    def test_a_longer_length_plays_the_crossing_and_the_landing_for_longer(self):
        fx = effects.effect("rupture")
        region = {"kind": "edge", "edge": "right", "x": 1727.0, "y": 0.0, "w": 1.0, "h": 1117.0}
        lasted = {}
        for length in ("short", "normal", "long"):
            player = effects.Player(effects.pace(length))
            player.push(0.0, "edge", region, (1727.0, 500.0), 0.9)
            player.cross(0.0)
            player.arrive(0.0, "edge", (1.0, 500.0), "left")
            t = 0.0
            while player.frames(t, fx, (1728.0, 1117.0, "mac"), PALETTE, False, True) and t < 5.0:
                t += 1 / 60
            lasted[length] = t
        self.assertLess(lasted["short"], lasted["normal"])
        self.assertLess(lasted["normal"], lasted["long"])
        self.assertAlmostEqual(lasted["long"] / lasted["normal"], 2.0, delta=0.1)

    def test_the_push_follows_the_hand_whatever_the_length(self):
        fx = effects.effect("flint")
        region = {"kind": "edge", "edge": "right", "x": 1727.0, "y": 0.0, "w": 1.0, "h": 1117.0}
        drawn = []
        for length in ("short", "long"):
            player = effects.Player(effects.pace(length))
            player.push(0.0, "edge", region, (1727.0, 500.0), 0.6)
            drawn.append(player.frames(0.2, fx, (1728.0, 1117.0, "mac"), PALETTE, False, True)[0].bounds)
        self.assertEqual(drawn[0], drawn[1])


if __name__ == "__main__":
    unittest.main()
