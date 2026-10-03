import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import crossing
from core import effects
import effects_overlay
from core.return_edge import Rect


class FakeCG:
    """A recording stand-in for the Quartz module: every CGContext* call effects_overlay.replay
    makes is logged, and a real graphics-state stack tracks alpha and blend mode across
    Save/Restore, so a test can prove each op's state is undone by the end of it. No
    CGContextDrawConicGradient here, so a conic paint falls back to _conic_wedges; ConicFakeCG
    below adds it back for the direct-call path."""

    kCGColorSpaceSRGB = "sRGB"
    kCGBlendModeNormal = "normal"
    kCGBlendModePlusLighter = "plusLighter"
    kCGBlendModeCopy = "copy"
    kCGLineCapButt = "cap-butt"
    kCGLineCapRound = "cap-round"
    kCGLineCapSquare = "cap-square"
    kCGLineJoinMiter = "join-miter"
    kCGLineJoinRound = "join-round"
    kCGLineJoinBevel = "join-bevel"
    kCGGradientDrawsBeforeStartLocation = 1
    kCGGradientDrawsAfterEndLocation = 2

    BASE_STATE = {"alpha": 1.0, "blend": kCGBlendModeNormal}

    def __init__(self, clip_box=((0.0, 0.0), (1440.0, 900.0))):
        self.calls = []
        self.gradients = []
        self.restored_states = []
        self.state = dict(self.BASE_STATE)
        self._stack = []
        self._clip_box = clip_box

    def _log(self, name, *args):
        self.calls.append((name, args))

    def names(self):
        return [name for name, _args in self.calls]

    # -- state --

    def CGContextSaveGState(self, ctx):
        self._log("CGContextSaveGState")
        self._stack.append(dict(self.state))

    def CGContextRestoreGState(self, ctx):
        self._log("CGContextRestoreGState")
        self.state = self._stack.pop()
        self.restored_states.append(dict(self.state))

    def CGContextSetAlpha(self, ctx, alpha):
        self._log("CGContextSetAlpha", alpha)
        self.state["alpha"] = alpha

    def CGContextSetBlendMode(self, ctx, mode):
        self._log("CGContextSetBlendMode", mode)
        self.state["blend"] = mode

    def CGContextBeginTransparencyLayer(self, ctx, info):
        self._log("CGContextBeginTransparencyLayer")
        self._stack.append(dict(self.state))

    def CGContextEndTransparencyLayer(self, ctx):
        self._log("CGContextEndTransparencyLayer")
        self.state = self._stack.pop()

    # -- path --

    def CGContextBeginPath(self, ctx):
        self._log("CGContextBeginPath")

    def CGContextMoveToPoint(self, ctx, x, y):
        self._log("CGContextMoveToPoint", x, y)

    def CGContextAddLineToPoint(self, ctx, x, y):
        self._log("CGContextAddLineToPoint", x, y)

    def CGContextAddQuadCurveToPoint(self, ctx, cx, cy, x, y):
        self._log("CGContextAddQuadCurveToPoint", cx, cy, x, y)

    def CGContextAddCurveToPoint(self, ctx, c1x, c1y, c2x, c2y, x, y):
        self._log("CGContextAddCurveToPoint", c1x, c1y, c2x, c2y, x, y)

    def CGContextClosePath(self, ctx):
        self._log("CGContextClosePath")

    # -- stroke setup --

    def CGContextSetLineWidth(self, ctx, width):
        self._log("CGContextSetLineWidth", width)

    def CGContextSetLineCap(self, ctx, cap):
        self._log("CGContextSetLineCap", cap)

    def CGContextSetLineJoin(self, ctx, join):
        self._log("CGContextSetLineJoin", join)

    def CGContextReplacePathWithStrokedPath(self, ctx):
        self._log("CGContextReplacePathWithStrokedPath")

    # -- solid colour --

    def CGContextSetRGBFillColor(self, ctx, r, g, b, a):
        self._log("CGContextSetRGBFillColor", r, g, b, a)

    def CGContextSetRGBStrokeColor(self, ctx, r, g, b, a):
        self._log("CGContextSetRGBStrokeColor", r, g, b, a)

    def CGContextFillPath(self, ctx):
        self._log("CGContextFillPath")

    def CGContextEOFillPath(self, ctx):
        self._log("CGContextEOFillPath")

    def CGContextStrokePath(self, ctx):
        self._log("CGContextStrokePath")

    def CGContextClip(self, ctx):
        self._log("CGContextClip")

    def CGContextClipToRect(self, ctx, rect):
        self._log("CGContextClipToRect", rect)

    def CGContextEOClip(self, ctx):
        self._log("CGContextEOClip")

    # -- gradients --

    def CGGradientCreateWithColorComponents(self, space, components, locations, count):
        gradient = {"space": space, "components": list(components), "locations": list(locations), "count": count}
        self.gradients.append(gradient)
        self._log("CGGradientCreateWithColorComponents", gradient)
        return gradient

    def CGContextDrawLinearGradient(self, ctx, gradient, start, end, options):
        self._log("CGContextDrawLinearGradient", gradient, start, end, options)

    def CGContextDrawRadialGradient(self, ctx, gradient, start, start_r, end, end_r, options):
        self._log("CGContextDrawRadialGradient", gradient, start, start_r, end, end_r, options)

    def CGContextGetClipBoundingBox(self, ctx):
        self._log("CGContextGetClipBoundingBox")
        return self._clip_box

    # -- misc, used by draw_scene / not by replay but harmless to have --

    def CGColorSpaceCreateWithName(self, name):
        return ("colourspace", name)


class ConicFakeCG(FakeCG):
    """The same recorder, with CGContextDrawConicGradient present, so replay takes the direct path
    instead of the 72-wedge fallback."""

    def CGContextDrawConicGradient(self, ctx, gradient, centre, angle):
        self._log("CGContextDrawConicGradient", gradient, centre, angle)


CTX = "the-context"


def _square_pen(fill=None, stroke=None, rule="nonzero"):
    pen = effects.Pen()
    pen.moveTo(0.0, 0.0)
    pen.lineTo(10.0, 0.0)
    pen.lineTo(10.0, 10.0)
    pen.lineTo(0.0, 10.0)
    pen.closePath()
    if fill is not None:
        pen.fillStyle = fill
        pen.fill(rule)
    if stroke is not None:
        pen.strokeStyle = stroke
        pen.stroke()
    return pen


class ColourFillTest(unittest.TestCase):
    def test_nonzero_fill_sets_colour_and_fills(self):
        pen = _square_pen(fill="#ff0000")
        cg = FakeCG()
        effects_overlay.replay(cg, CTX, [pen])
        self.assertIn(("CGContextSetRGBFillColor", (1.0, 0.0, 0.0, 1.0)), cg.calls)
        self.assertIn("CGContextFillPath", cg.names())
        self.assertNotIn("CGContextEOFillPath", cg.names())

    def test_evenodd_fill_uses_the_eo_variant(self):
        pen = _square_pen(fill="#00ff00", rule="evenodd")
        cg = FakeCG()
        effects_overlay.replay(cg, CTX, [pen])
        self.assertIn("CGContextEOFillPath", cg.names())
        self.assertNotIn("CGContextFillPath", cg.names())


class ColourStrokeTest(unittest.TestCase):
    def test_stroke_sets_width_cap_join_and_colour(self):
        pen = effects.Pen()
        pen.moveTo(0.0, 0.0)
        pen.lineTo(10.0, 0.0)
        pen.strokeStyle = "#0000ff"
        pen.lineWidth = 3.0
        pen.lineCap = "round"
        pen.lineJoin = "bevel"
        pen.stroke()
        cg = FakeCG()
        effects_overlay.replay(cg, CTX, [pen])
        self.assertIn(("CGContextSetLineWidth", (3.0,)), cg.calls)
        self.assertIn(("CGContextSetLineCap", (cg.kCGLineCapRound,)), cg.calls)
        self.assertIn(("CGContextSetLineJoin", (cg.kCGLineJoinBevel,)), cg.calls)
        self.assertIn(("CGContextSetRGBStrokeColor", (0.0, 0.0, 1.0, 1.0)), cg.calls)
        names = cg.names()
        self.assertLess(names.index("CGContextSetRGBStrokeColor"), names.index("CGContextStrokePath"))

    def test_every_cap_and_join_maps_to_its_cg_constant(self):
        for canvas_cap, cg_name in effects_overlay._CAPS.items():
            for canvas_join, join_name in effects_overlay._JOINS.items():
                with self.subTest(cap=canvas_cap, join=canvas_join):
                    pen = effects.Pen()
                    pen.moveTo(0.0, 0.0)
                    pen.lineTo(5.0, 0.0)
                    pen.strokeStyle = "#fff"
                    pen.lineCap = canvas_cap
                    pen.lineJoin = canvas_join
                    pen.stroke()
                    cg = FakeCG()
                    effects_overlay.replay(cg, CTX, [pen])
                    self.assertIn(("CGContextSetLineCap", (getattr(cg, cg_name),)), cg.calls)
                    self.assertIn(("CGContextSetLineJoin", (getattr(cg, join_name),)), cg.calls)


class GradientFillTest(unittest.TestCase):
    def test_nonzero_gradient_fill_clips_then_draws(self):
        pen = effects.Pen()
        pen.moveTo(0.0, 0.0)
        pen.lineTo(10.0, 0.0)
        pen.lineTo(10.0, 10.0)
        pen.closePath()
        gradient = pen.createLinearGradient(0.0, 0.0, 10.0, 0.0)
        gradient.addColorStop(0.0, "#000000")
        gradient.addColorStop(1.0, "#ffffff")
        pen.fillStyle = gradient
        pen.fill()
        cg = FakeCG()
        effects_overlay.replay(cg, CTX, [pen])
        names = cg.names()
        self.assertIn("CGContextClip", names)
        self.assertNotIn("CGContextEOClip", names)
        self.assertIn("CGContextDrawLinearGradient", names)
        self.assertLess(names.index("CGContextClip"), names.index("CGContextDrawLinearGradient"))

    def test_evenodd_gradient_fill_clips_with_the_eo_variant(self):
        pen = effects.Pen()
        pen.moveTo(0.0, 0.0)
        pen.lineTo(10.0, 0.0)
        pen.lineTo(10.0, 10.0)
        pen.closePath()
        gradient = pen.createLinearGradient(0.0, 0.0, 10.0, 0.0)
        gradient.addColorStop(0.0, "#000000")
        gradient.addColorStop(1.0, "#ffffff")
        pen.fillStyle = gradient
        pen.fill("evenodd")
        cg = FakeCG()
        effects_overlay.replay(cg, CTX, [pen])
        names = cg.names()
        self.assertIn("CGContextEOClip", names)
        self.assertNotIn("CGContextClip", names)


class GradientStrokeTest(unittest.TestCase):
    def test_gradient_stroke_replaces_path_then_clips_then_draws(self):
        pen = effects.Pen()
        pen.moveTo(0.0, 0.0)
        pen.lineTo(10.0, 0.0)
        gradient = pen.createLinearGradient(0.0, 0.0, 10.0, 0.0)
        gradient.addColorStop(0.0, "#000000")
        gradient.addColorStop(1.0, "#ffffff")
        pen.strokeStyle = gradient
        pen.lineWidth = 2.0
        pen.stroke()
        cg = FakeCG()
        effects_overlay.replay(cg, CTX, [pen])
        names = cg.names()
        for required in ("CGContextReplacePathWithStrokedPath", "CGContextClip", "CGContextDrawLinearGradient"):
            self.assertIn(required, names)
        self.assertLess(names.index("CGContextReplacePathWithStrokedPath"), names.index("CGContextClip"))
        self.assertLess(names.index("CGContextClip"), names.index("CGContextDrawLinearGradient"))
        self.assertNotIn("CGContextStrokePath", names)


class PathVerbTest(unittest.TestCase):
    def test_every_verb_maps_to_its_cg_call_with_the_right_numbers(self):
        pen = effects.Pen()
        pen.moveTo(1.0, 2.0)
        pen.lineTo(3.0, 4.0)
        pen.quadraticCurveTo(5.0, 6.0, 7.0, 8.0)
        pen.bezierCurveTo(9.0, 10.0, 11.0, 12.0, 13.0, 14.0)
        pen.closePath()
        pen.fillStyle = "#fff"
        pen.fill()
        cg = FakeCG()
        effects_overlay.replay(cg, CTX, [pen])
        self.assertIn(("CGContextMoveToPoint", (1.0, 2.0)), cg.calls)
        self.assertIn(("CGContextAddLineToPoint", (3.0, 4.0)), cg.calls)
        self.assertIn(("CGContextAddQuadCurveToPoint", (5.0, 6.0, 7.0, 8.0)), cg.calls)
        self.assertIn(("CGContextAddCurveToPoint", (9.0, 10.0, 11.0, 12.0, 13.0, 14.0)), cg.calls)
        self.assertIn(("CGContextClosePath", ()), cg.calls)


class PaintKindTest(unittest.TestCase):
    def test_rgba_colour(self):
        pen = _square_pen(fill="rgba(255, 0, 0, 0.5)")
        cg = FakeCG()
        effects_overlay.replay(cg, CTX, [pen])
        self.assertIn(("CGContextSetRGBFillColor", (1.0, 0.0, 0.0, 0.5)), cg.calls)

    def test_linear_gradient_passes_its_endpoints_and_extends_both_ways(self):
        pen = effects.Pen()
        pen.moveTo(0.0, 0.0)
        pen.lineTo(10.0, 0.0)
        pen.lineTo(10.0, 10.0)
        pen.closePath()
        gradient = pen.createLinearGradient(1.0, 2.0, 8.0, 9.0)
        gradient.addColorStop(0.0, "#000000")
        gradient.addColorStop(1.0, "#ffffff")
        pen.fillStyle = gradient
        pen.fill()
        cg = FakeCG()
        effects_overlay.replay(cg, CTX, [pen])
        extend = cg.kCGGradientDrawsBeforeStartLocation | cg.kCGGradientDrawsAfterEndLocation
        calls = [args for name, args in cg.calls if name == "CGContextDrawLinearGradient"]
        self.assertEqual(len(calls), 1)
        _made, start, end, options = calls[0]
        self.assertEqual(start, (1.0, 2.0))
        self.assertEqual(end, (8.0, 9.0))
        self.assertEqual(options, extend)

    def test_radial_gradient_passes_both_circles(self):
        pen = effects.Pen()
        pen.moveTo(0.0, 0.0)
        pen.lineTo(10.0, 0.0)
        pen.lineTo(10.0, 10.0)
        pen.closePath()
        gradient = pen.createRadialGradient(1.0, 2.0, 3.0, 8.0, 9.0, 20.0)
        gradient.addColorStop(0.0, "#000000")
        gradient.addColorStop(1.0, "#ffffff")
        pen.fillStyle = gradient
        pen.fill()
        cg = FakeCG()
        effects_overlay.replay(cg, CTX, [pen])
        extend = cg.kCGGradientDrawsBeforeStartLocation | cg.kCGGradientDrawsAfterEndLocation
        calls = [args for name, args in cg.calls if name == "CGContextDrawRadialGradient"]
        self.assertEqual(len(calls), 1)
        _made, start, start_r, end, end_r, options = calls[0]
        self.assertEqual(start, (1.0, 2.0))
        self.assertEqual(start_r, 3.0)
        self.assertEqual(end, (8.0, 9.0))
        self.assertEqual(end_r, 20.0)
        self.assertEqual(options, extend)

    def _conic_pen(self):
        pen = effects.Pen()
        pen.moveTo(0.0, 0.0)
        pen.lineTo(10.0, 0.0)
        pen.lineTo(10.0, 10.0)
        pen.closePath()
        gradient = pen.createConicGradient(0.75, 4.0, 5.0)
        gradient.addColorStop(0.0, "#ff0000")
        gradient.addColorStop(1.0, "#0000ff")
        pen.fillStyle = gradient
        pen.fill()
        return pen

    def test_conic_gradient_draws_directly_when_the_fake_offers_it(self):
        pen = self._conic_pen()
        cg = ConicFakeCG()
        effects_overlay.replay(cg, CTX, [pen])
        calls = [args for name, args in cg.calls if name == "CGContextDrawConicGradient"]
        self.assertEqual(len(calls), 1)
        _made, centre, angle = calls[0]
        self.assertEqual(centre, (4.0, 5.0))
        self.assertEqual(angle, 0.75)
        self.assertNotIn("CGContextGetClipBoundingBox", cg.names())

    def test_conic_gradient_falls_back_to_72_wedges_without_the_real_call(self):
        pen = self._conic_pen()
        cg = FakeCG(clip_box=((0.0, 0.0), (20.0, 20.0)))
        effects_overlay.replay(cg, CTX, [pen])
        names = cg.names()
        self.assertNotIn("CGContextDrawConicGradient", names)
        self.assertIn("CGContextGetClipBoundingBox", names)
        wedge_fills = [name for name in names if name == "CGContextFillPath"]
        self.assertEqual(len(wedge_fills), effects_overlay.CONIC_WEDGES)
        # Overlapping wedges are copied inside one layer, so a translucent stop never doubles.
        begin, end = names.index("CGContextBeginTransparencyLayer"), names.index("CGContextEndTransparencyLayer")
        self.assertLess(begin, names.index("CGContextFillPath"))
        self.assertGreater(end, len(names) - 1 - names[::-1].index("CGContextFillPath"))
        self.assertIn(("CGContextSetBlendMode", ("copy",)), cg.calls[begin:end])


class StateRestoreTest(unittest.TestCase):
    def test_each_op_is_bracketed_and_state_returns_to_base_afterwards(self):
        pen = effects.Pen()
        pen.moveTo(0.0, 0.0)
        pen.lineTo(10.0, 0.0)
        pen.lineTo(10.0, 10.0)
        pen.closePath()
        pen.globalCompositeOperation = "lighter"
        pen.globalAlpha = 0.3
        pen.fillStyle = "#ff0000"
        pen.fill()
        pen.beginPath()
        pen.moveTo(0.0, 0.0)
        pen.lineTo(5.0, 0.0)
        pen.lineTo(5.0, 5.0)
        pen.closePath()
        pen.globalCompositeOperation = "source-over"
        pen.globalAlpha = 1.0
        pen.fillStyle = "#0000ff"
        pen.fill()

        cg = FakeCG()
        effects_overlay.replay(cg, CTX, [pen])

        self.assertEqual(cg.calls.count(("CGContextSaveGState", ())), 2)
        self.assertEqual(cg.calls.count(("CGContextRestoreGState", ())), 2)
        self.assertIn(("CGContextSetAlpha", (0.3,)), cg.calls)
        self.assertIn(("CGContextSetAlpha", (1.0,)), cg.calls)
        self.assertIn(("CGContextSetBlendMode", (cg.kCGBlendModePlusLighter,)), cg.calls)
        self.assertIn(("CGContextSetBlendMode", (cg.kCGBlendModeNormal,)), cg.calls)
        # Every RestoreGState left the fake back at the untouched base state.
        for restored in cg.restored_states:
            self.assertEqual(restored, cg.BASE_STATE)
        self.assertEqual(cg.state, cg.BASE_STATE)


class GradientStopsTest(unittest.TestCase):
    def test_stops_are_sorted_by_offset(self):
        gradient = effects.Gradient("linear", (0.0, 0.0, 1.0, 0.0))
        gradient.addColorStop(1.0, "#ff0000")
        gradient.addColorStop(0.0, "#0000ff")
        stops = effects_overlay._stops(gradient)
        self.assertEqual([offset for offset, _rgba in stops], [0.0, 1.0])
        self.assertEqual(stops[0][1], (0.0, 0.0, 1.0, 1.0))
        self.assertEqual(stops[1][1], (1.0, 0.0, 0.0, 1.0))

    def test_a_single_stop_becomes_flat_colour_at_both_ends(self):
        gradient = effects.Gradient("linear", (0.0, 0.0, 1.0, 0.0))
        gradient.addColorStop(0.5, "#00ff00")
        stops = effects_overlay._stops(gradient)
        self.assertEqual(stops, [(0.0, (0.0, 1.0, 0.0, 1.0)), (1.0, (0.0, 1.0, 0.0, 1.0))])

    def test_an_empty_gradient_draws_nothing(self):
        pen = effects.Pen()
        pen.moveTo(0.0, 0.0)
        pen.lineTo(10.0, 0.0)
        pen.lineTo(10.0, 10.0)
        pen.closePath()
        gradient = pen.createLinearGradient(0.0, 0.0, 10.0, 0.0)
        pen.fillStyle = gradient
        pen.fill()
        cg = FakeCG()
        effects_overlay.replay(cg, CTX, [pen])
        self.assertEqual(cg.gradients, [])
        self.assertNotIn("CGContextDrawLinearGradient", cg.names())

    def test_one_gradient_build_per_pen_object_even_when_two_ops_share_it(self):
        pen = effects.Pen()
        pen.moveTo(0.0, 0.0)
        pen.lineTo(10.0, 0.0)
        pen.lineTo(10.0, 10.0)
        pen.closePath()
        gradient = pen.createLinearGradient(0.0, 0.0, 10.0, 0.0)
        gradient.addColorStop(0.0, "#000000")
        gradient.addColorStop(1.0, "#ffffff")
        pen.fillStyle = gradient
        pen.fill()
        pen.beginPath()
        pen.moveTo(0.0, 0.0)
        pen.lineTo(5.0, 0.0)
        pen.strokeStyle = gradient
        pen.lineWidth = 1.0
        pen.stroke()

        cg = FakeCG()
        effects_overlay.replay(cg, CTX, [pen])
        self.assertEqual(len(cg.gradients), 1)


class RegionGeometryTest(unittest.TestCase):
    def test_corner_name_top_right(self):
        box = (0.0, 0.0, 1440.0, 900.0)
        region = (1432.0, 0.0, 8.0, 8.0)
        self.assertEqual(effects_overlay.corner_name(region, box), "top_right")

    def test_corner_name_bottom_left(self):
        box = (0.0, 0.0, 1440.0, 900.0)
        region = (0.0, 892.0, 8.0, 8.0)
        self.assertEqual(effects_overlay.corner_name(region, box), "bottom_left")

    def test_region_of_corner_is_relative_to_the_box_top_left(self):
        found = effects_overlay.region_of("corner", "right", (1432.0, 0.0, 8.0, 8.0), (0.0, 0.0, 1440.0, 900.0))
        self.assertEqual(found, {
            "kind": "corner", "edge": "right", "x": 1432.0, "y": 0.0, "w": 8.0, "h": 8.0, "corner": "top_right",
        })

    def test_region_of_corner_with_a_non_zero_origin_box(self):
        box = (-1920.0, -200.0, 1440.0, 900.0)
        region = (1432.0, -200.0, 8.0, 8.0)
        found = effects_overlay.region_of("corner", "right", region, box)
        self.assertEqual(found["kind"], "corner")
        self.assertEqual(found["edge"], "right")
        self.assertEqual(found["corner"], "top_right")
        self.assertEqual(found["x"], 1432.0 - (-1920.0))
        self.assertEqual(found["y"], -200.0 - (-200.0))
        self.assertEqual(found["w"], 8.0)
        self.assertEqual(found["h"], 8.0)

    def test_region_of_edge_has_no_corner_key(self):
        found = effects_overlay.region_of("edge", "right", (1439.0, 0.0, 1.0, 900.0), (0.0, 0.0, 1440.0, 900.0))
        self.assertNotIn("corner", found)


# Which fx_*.py module each of effects.EFFECT_IDS lives in, from effects.DIRECTIONS.
_MODULE_FOR_EFFECT = {
    effect_id: module_name
    for module_name, _name, effect_ids, _packs in effects.DIRECTIONS
    for effect_id in effect_ids
}


class DisplayClipTest(unittest.TestCase):
    def test_a_pen_that_names_its_display_is_drawn_inside_it(self):
        pen = effects.Pen()
        pen.fillRect(0, 0, 50, 50)
        pen.clip = (10.0, 20.0, 110.0, 220.0)
        cg = FakeCG()
        effects_overlay.replay(cg, "ctx", [pen])
        clip = [args for name, args in cg.calls if name == "CGContextClipToRect"]
        self.assertEqual(clip, [(((10.0, 20.0), (100.0, 200.0)),)])
        self.assertEqual(cg.state, FakeCG.BASE_STATE)


class EdgeGlowDisplayTest(unittest.TestCase):
    """Glow and Beam's band on the pointer's own display, and on the push home while the PC drives."""

    DISPLAYS = [Rect(0, 0, 1728, 1117), Rect(0, -1440, 2560, 1440)]
    BOX = (0.0, -1440.0, 2560.0, 1117.0)

    def setUp(self):
        mock.patch.object(effects_overlay.desktop_mac, "monitors", return_value=self.DISPLAYS).start()
        self.addCleanup(mock.patch.stopall)

    def test_a_strip_along_the_whole_desktop_is_cut_to_the_pointers_display(self):
        # Stacked, the external above: the desktop's left edge runs down both displays.
        strip = (0.0, -1440.0, 1.0, 2557.0)
        self.assertEqual(effects_overlay.strip_on_display(strip, (0.0, 500.0), self.BOX), (0.0, 0.0, 1.0, 1117.0))
        self.assertEqual(effects_overlay.strip_on_display(strip, None, self.BOX), strip)

    def glow(self):
        import kvm_bridge_app
        controller = SimpleNamespace(_current_desktop_bounds=lambda: self.BOX, crossing_pressure_now=lambda: 0.0)
        glow = kvm_bridge_app.EdgeGlow(controller, mock.Mock())
        glow._draw = mock.Mock()
        return glow

    def test_the_push_home_while_the_pc_drives_lights_the_band_and_drains(self):
        glow = self.glow()
        glow.driven("left", 0.6, False, (0.0, 500.0))
        self.assertEqual((glow.mac_edge, glow.region), ("left", (0, 0, 1, 1117)))
        self.assertAlmostEqual(glow.level_now(), 0.6, places=1)
        glow.driven("left", 1.0, True, (0.0, 500.0))
        self.assertEqual(glow.level_now(), 0.0)
        self.assertIsNotNone(glow.flash_at)

    def test_the_macs_own_push_uses_the_engines_pressure_again(self):
        glow = self.glow()
        glow.driven("left", 0.6, False, (0.0, 500.0))
        glow.update("pressure", crossing.Step(pressure=0.3, pin=(0.0, -700.0), mac_edge="left",
                                              region=(0.0, -1440.0, 1.0, 2557.0), via="edge"))
        self.assertIsNone(glow.level_now)
        self.assertEqual(glow.region, (0.0, -1440.0, 1.0, 1440.0))


class RealEffectIntegrationTest(unittest.TestCase):
    def test_every_landed_effect_replays_its_preview_scene_without_error(self):
        """The fx_*.py modules land one at a time (module docstring); an id whose OWN module is
        not here yet is skipped rather than failed. Landed ids are not caught here: if
        effects.effect() itself errors for a module that does exist, that is a real bug to see,
        not something to swallow as a skip."""
        drew_something = False
        checked_any = False
        for effect_id in effects.EFFECT_IDS:
            module_name = "fx_light" if effect_id == "aperture" else _MODULE_FOR_EFFECT[effect_id]
            with self.subTest(effect=effect_id, module=module_name):
                try:
                    importlib.import_module(f"core.{module_name}")
                except ImportError:
                    continue
                fx = effects.effect(effect_id)
                if fx is None:
                    continue
                checked_any = True
                for t in (1.2, 2.5):
                    scene = effects.preview_scene(fx, t, "edge", ["#5fd4f4", "#ff7828"])
                    cg = FakeCG()
                    effects_overlay.replay(cg, CTX, scene["pens"])
                    if any(pen.ops for pen in scene["pens"]):
                        drew_something = True
        if not checked_any:
            self.skipTest("no fx_*.py module has landed yet")
        self.assertTrue(drew_something, "no landed effect's preview scene drew any op")


def AppKit_rect(x, y, w, h):
    return SimpleNamespace(origin=SimpleNamespace(x=x, y=y), size=SimpleNamespace(width=w, height=h))


class _Controller:
    def __init__(self, style="rupture"):
        self.cfg = SimpleNamespace(crossing=dict(crossing.DEFAULT_CROSSING, glow_style=style, glow_colour="neon"))
        self.level = 0.0

    def _current_desktop_bounds(self):
        return (-100.0, -50.0, 1340.0, 850.0)

    def crossing_pressure_now(self):
        return self.level


class OverlayFeedTest(unittest.TestCase):
    """What the app's events do to the Player, without a window: events only move it on and start
    the timer, and each lands where the pointer is."""

    def setUp(self):
        self.overlay = effects_overlay.EffectsOverlay(_Controller(), mock.Mock())
        self.overlay.timer = mock.Mock()
        self.overlay.timer.is_alive.return_value = False
        self.overlay._notch = mock.Mock(return_value=None)
        self.monitors = mock.patch.object(effects_overlay.desktop_mac, "monitors",
                                          return_value=[Rect(-100, -50, 1440, 900)]).start()
        self.draw = mock.patch.object(self.overlay, "_draw").start()
        self.addCleanup(mock.patch.stopall)

    def test_an_event_starts_the_timer_and_never_draws_itself(self):
        region = (1339.0, -50.0, 1.0, 900.0)
        step = crossing.Step(pressure=0.3, hold=True, pin=(1339.0, 400.0), mac_edge="right", region=region, via="edge")
        for _ in range(50):
            self.overlay.departure("pressure", step)
        self.draw.assert_not_called()
        self.overlay.timer.start.assert_called()
        self.assertFalse(self.overlay.disabled)
        self.assertEqual(self.overlay.player.depart["at"], (1439.0, 450.0))

    def test_crossing_on_the_first_event_starts_where_it_gave(self):
        step = crossing.Step(pressure=1.0, crossed=True, pin=(1339.0, 700.0), mac_edge="right",
                             region=(1339.0, -50.0, 1.0, 900.0), via="edge")
        self.overlay.departure("cross", step)
        self.assertEqual(self.overlay.player.depart["at"], (1439.0, 750.0))
        self.assertIsNotNone(self.overlay.player.depart["crossed_at"])

    def test_a_notch_push_with_no_notch_plays_as_the_top_edge(self):
        step = crossing.Step(pressure=0.5, hold=True, pin=(700.0, -50.0), mac_edge="top",
                             region=(600.0, -50.0, 200.0, 1.0), via="notch")
        self.overlay.departure("pressure", step)
        self.assertEqual(self.overlay.player.depart["method"], "edge")
        self.assertEqual(self.overlay.player.depart["region"]["kind"], "edge")

    def test_the_way_home_runs_along_the_pointers_own_display(self):
        displays = [Rect(0, 0, 1440, 900), Rect(1440, -300, 2560, 1440)]
        with mock.patch.object(effects_overlay.desktop_mac, "monitors", return_value=displays):
            self.overlay.return_push("left", 0.4, False, (0, 500))
        region = self.overlay.player.depart["region"]
        # The box starts at (-100, -50); the Mac's own display is 0..900 tall, not the box's height.
        self.assertEqual((region["x"], region["y"], region["h"]), (100.0, 50.0, 900.0))

    def test_a_way_home_through_a_corner_plays_the_corner_form_there(self):
        with mock.patch.object(effects_overlay.desktop_mac, "monitors", return_value=[Rect(-100, -50, 1440, 900)]):
            self.overlay.return_push("top", 0.4, False, (1339, -50), "top_right")
        depart = self.overlay.player.depart
        self.assertEqual((depart["method"], depart["region"]["kind"], depart["region"]["corner"]),
                         ("corner", "corner", "top_right"))
        self.assertEqual((depart["region"]["x"], depart["region"]["y"], depart["region"]["w"]), (1432.0, 0.0, 8.0))

    def test_each_event_plays_on_the_display_under_the_pointer(self):
        # A display left of and below the primary's top, so its Quartz points are negative.
        self.monitors.return_value = [Rect(0, 0, 1728, 1117), Rect(-1920, 300, 1920, 1080)]
        self.overlay.controller._current_desktop_bounds = lambda: (-1920.0, 0.0, 1728.0, 1380.0)
        self.overlay.arrival("edge", -1000.0, 800.0, "left")
        arrival = self.overlay.player.arrival
        self.assertEqual((arrival["display"], arrival["at"]), ((0.0, 300.0, 1920.0, 1080.0), (920.0, 800.0)))

    def test_a_switch_is_oriented_by_its_own_display(self):
        self.monitors.return_value = [Rect(0, 0, 1728, 1117), Rect(1728, 0, 2560, 1440)]
        self.overlay.controller._current_desktop_bounds = lambda: (0.0, 0.0, 4288.0, 1440.0)
        self.overlay.switched(1740.0, 700.0)
        # Near the second display's left edge, though in the middle of the whole desktop.
        self.assertEqual(self.overlay.player.arrival["edge"], "left")

    def test_the_timer_stops_and_the_panels_hide_once_nothing_plays(self):
        self.overlay.arrival("edge", 10.0, 400.0, "left")
        panel = mock.Mock()
        self.overlay.panels = {0: (panel, mock.Mock())}
        self.overlay.player.arrival["started"] -= 5.0
        effects_overlay.EffectsOverlay._draw(self.overlay)
        panel.orderOut_.assert_called_once()
        self.overlay.timer.stop.assert_called_once()
        self.assertIsNone(self.overlay.fx)

    def test_a_display_unplugged_mid_effect_shows_nothing_and_the_effect_still_ends(self):
        self.monitors.return_value = [Rect(0, 0, 1728, 1117), Rect(1728, 0, 2560, 1440)]
        self.overlay.controller._current_desktop_bounds = lambda: (0.0, 0.0, 4288.0, 1440.0)
        self.overlay.arrival("edge", 1730.0, 700.0, "left")
        # Only the built-in display is left, in AppKit's bottom-left coordinates.
        builtin = SimpleNamespace(frame=lambda: AppKit_rect(0.0, 0.0, 1728.0, 1117.0))
        panel = mock.Mock()
        self.overlay._panel = mock.Mock(return_value=(panel, mock.Mock()))
        appkit = SimpleNamespace(NSScreen=SimpleNamespace(screens=lambda: [builtin]))
        with mock.patch.object(effects_overlay, "AppKit", appkit):
            effects_overlay.EffectsOverlay._draw(self.overlay)
            self.overlay._panel.assert_not_called()
            self.overlay.player.arrival["started"] -= 5.0
            effects_overlay.EffectsOverlay._draw(self.overlay)
        self.overlay.timer.stop.assert_called_once()

    def test_a_switch_plays_the_chosen_effect_where_the_pointer_is(self):
        self.overlay.switched(200.0, 150.0)
        self.assertEqual(self.overlay.fx.id, "rupture")
        arrival = self.overlay.player.arrival
        self.assertEqual((arrival["method"], arrival["at"], arrival["switch"]), ("switch", (300.0, 200.0), True))
        self.overlay.timer.start.assert_called()

    def test_a_switch_with_glow_or_beam_plays_the_locator(self):
        for style in ("glow", "beam"):
            with self.subTest(style=style):
                overlay = effects_overlay.EffectsOverlay(_Controller(style), mock.Mock())
                overlay.timer = mock.Mock()
                overlay.timer.is_alive.return_value = False
                overlay._notch = mock.Mock(return_value=None)
                overlay.switched(200.0, 150.0)
                self.assertIs(overlay.fx, effects.LOCATOR)
                self.assertFalse(overlay.wanted(overlay.controller.cfg.crossing))
                self.assertTrue(overlay.wanted_switch(overlay.controller.cfg.crossing))

    def test_a_switch_is_wanted_only_with_both_switches_on(self):
        feel = dict(crossing.DEFAULT_CROSSING)
        self.assertTrue(self.overlay.wanted_switch(feel))
        self.assertFalse(self.overlay.wanted_switch(dict(feel, shortcut_arrival=False)))
        self.assertFalse(self.overlay.wanted_switch(dict(feel, glow=False)))

    def test_effects_that_cannot_load_are_off_from_the_start(self):
        logger = mock.Mock()
        with mock.patch.object(effects, "_load", side_effect=ModuleNotFoundError("fx_membrane")):
            overlay = effects_overlay.EffectsOverlay(_Controller(), logger)
        self.assertTrue(overlay.disabled)
        self.assertFalse(overlay.wanted(_Controller().cfg.crossing))
        logger.exception.assert_called_once()


if __name__ == "__main__":
    unittest.main()
