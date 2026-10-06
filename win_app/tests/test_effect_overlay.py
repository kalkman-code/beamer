"""The crossing effects' replay into QPainter, against a fake painter that records what it was asked
to draw and in what state, and the overlay window's attributes."""

import math
import os
import subprocess
import unittest
from dataclasses import replace
from types import SimpleNamespace
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import effects
from core.return_edge import Rect

try:
    from PySide6.QtCore import QPointF, QRect, Qt
    from PySide6.QtGui import QConicalGradient, QLinearGradient, QPainter, QPainterPath, QRadialGradient

    import effect_overlay
except ImportError:  # PySide6 is only in the Windows venv
    effect_overlay = None


class FakePainter:
    """Records every draw with the opacity and composition it was made in, and keeps save/restore
    as QPainter does."""

    def __init__(self):
        self.opacity = 1.0
        self.mode = "source-over-at-start"
        self._stack = []
        self.draws = []
        self.clips = []

    def setClipRect(self, rect):
        self.clips.append((rect.x(), rect.y(), rect.width(), rect.height()))

    def save(self):
        self._stack.append((self.opacity, self.mode))

    def restore(self):
        self.opacity, self.mode = self._stack.pop()

    def setOpacity(self, value):
        self.opacity = value

    def setCompositionMode(self, mode):
        self.mode = mode

    def fillPath(self, path, brush):
        self.draws.append(("fill", path, brush, self.opacity, self.mode))

    def strokePath(self, path, pen):
        self.draws.append(("stroke", path, pen, self.opacity, self.mode))


def gradient(kind, geometry, stops=((0.0, (1, 0, 0, 1)), (1.0, (0, 0, 1, 1)))):
    out = effects.Gradient(kind, geometry)
    for offset, colour in stops:
        out.addColorStop(offset, colour)
    return out


def elements(path):
    return [(path.elementAt(i).type, round(path.elementAt(i).x, 3), round(path.elementAt(i).y, 3))
            for i in range(path.elementCount())]


@unittest.skipIf(effect_overlay is None, "needs PySide6")
class ReplayTests(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])

    def pen_of(self, ops):
        pen = effects.Pen()
        pen.ops = list(ops)
        return pen

    @unittest.skipUnless(sys.platform == "win32", "Windows raster budget")
    def test_replay_sets_serial_rasterisation_before_the_first_paint(self):
        env = dict(os.environ)
        env.pop("QT_NO_GUI_THREADPOOL", None)
        result = subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, 'win_app'); import effect_overlay, os; print(os.environ.get('QT_NO_GUI_THREADPOOL'))"],
            cwd=os.path.dirname(os.path.dirname(effect_overlay.__file__)), env=env,
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), "1")

    def test_every_op_restores_opacity_and_composition(self):
        linear = gradient("linear", (0, 0, 10, 0))
        ops = [
            ("fill", [("M", 0, 0), ("L", 10, 0), ("L", 10, 10), ("Z",)], (1, 0, 0, 1), 0.5, "lighter", "nonzero"),
            ("stroke", [("M", 0, 0), ("L", 10, 10)], linear, 0.25, "source-over", 3.0, "round", "bevel"),
            ("fill", [("M", 0, 0), ("L", 5, 0), ("L", 5, 5), ("Z",)], (0, 1, 0, 1), 1.0, "source-over", "evenodd"),
        ]
        painter = FakePainter()
        effect_overlay.replay(painter, [self.pen_of(ops)])
        self.assertEqual((painter.opacity, painter.mode), (1.0, "source-over-at-start"))
        self.assertEqual(painter._stack, [])
        self.assertEqual([d[0] for d in painter.draws], ["fill", "stroke", "fill"])
        self.assertEqual([d[3] for d in painter.draws], [0.5, 0.25, 1.0])
        self.assertEqual(painter.draws[0][4], QPainter.CompositionMode.CompositionMode_Plus)
        self.assertEqual(painter.draws[1][4], QPainter.CompositionMode.CompositionMode_SourceOver)
        self.assertEqual(painter.draws[0][1].fillRule(), Qt.FillRule.WindingFill)
        self.assertEqual(painter.draws[2][1].fillRule(), Qt.FillRule.OddEvenFill)

    def test_a_stroke_carries_width_cap_and_join(self):
        for cap, join, qcap, qjoin in (
            ("butt", "miter", Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.MiterJoin),
            ("round", "round", Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin),
            ("square", "bevel", Qt.PenCapStyle.SquareCap, Qt.PenJoinStyle.BevelJoin),
        ):
            painter = FakePainter()
            op = ("stroke", [("M", 0, 0), ("L", 4, 4)], (1, 1, 1, 1), 1.0, "source-over", 2.5, cap, join)
            effect_overlay.replay(painter, [self.pen_of([op])])
            stroke = painter.draws[0][2]
            self.assertEqual((stroke.widthF(), stroke.capStyle(), stroke.joinStyle()), (2.5, qcap, qjoin))

    def test_every_path_segment(self):
        path = effect_overlay.qpath([("M", 1, 2), ("L", 3, 4), ("Q", 5, 6, 7, 8), ("C", 9, 10, 11, 12, 13, 14), ("Z",)])
        kinds = [kind for kind, _x, _y in elements(path)]
        Move, Line, Curve, Data = (QPainterPath.ElementType.MoveToElement, QPainterPath.ElementType.LineToElement,
                                   QPainterPath.ElementType.CurveToElement, QPainterPath.ElementType.CurveToDataElement)
        # Qt stores a quad as a cubic; the close is a line back to the start.
        self.assertEqual(kinds, [Move, Line, Curve, Data, Data, Curve, Data, Data, Line])
        self.assertEqual(elements(path)[-2][1:], (13.0, 14.0))
        self.assertEqual(elements(path)[-1][1:], (1.0, 2.0))

    def test_a_line_after_a_close_carries_on_from_the_subpath_start(self):
        path = effect_overlay.qpath([("M", 5, 5), ("L", 10, 5), ("Z",), ("L", 5, 10)])
        self.assertEqual(elements(path)[-2][1:], (5.0, 5.0))
        self.assertEqual(elements(path)[-1][1:], (5.0, 10.0))

    def test_every_paint_kind(self):
        colour = (0.2, 0.4, 0.6, 0.8)
        linear = gradient("linear", (0, 0, 10, 0))
        radial = gradient("radial", (1, 2, 3, 4, 5, 6))
        conic = gradient("conic", (math.pi / 2, 7, 8))
        ops = [("fill", [("M", 0, 0), ("L", 1, 1), ("L", 0, 1), ("Z",)], paint, 1.0, "source-over", "nonzero")
               for paint in (colour, linear, radial, conic)]
        painter = FakePainter()
        effect_overlay.replay(painter, [self.pen_of(ops)])
        brushes = [draw[2] for draw in painter.draws]
        self.assertEqual(tuple(round(v, 4) for v in brushes[0].color().getRgbF()), colour)
        self.assertIsInstance(brushes[1].gradient(), QLinearGradient)
        self.assertEqual((brushes[1].gradient().start(), brushes[1].gradient().finalStop()), (QPointF(0, 0), QPointF(10, 0)))
        radial_q = brushes[2].gradient()
        self.assertIsInstance(radial_q, QRadialGradient)
        # canvas's first circle is Qt's focal circle; its second is Qt's centre circle.
        self.assertEqual((radial_q.focalPoint(), radial_q.focalRadius()), (QPointF(1, 2), 3))
        self.assertEqual((radial_q.center(), radial_q.centerRadius()), (QPointF(4, 5), 6))
        conic_q = brushes[3].gradient()
        self.assertIsInstance(conic_q, QConicalGradient)
        self.assertEqual(conic_q.center(), QPointF(7, 8))
        self.assertAlmostEqual(conic_q.angle() % 360.0, 270.0)
        # Reversed: canvas's last stop is Qt's first.
        self.assertEqual([round(p, 6) for p, _c in conic_q.stops()], [0.0, 1.0])
        self.assertEqual(conic_q.stops()[0][1].getRgbF(), (0.0, 0.0, 1.0, 1.0))

    def test_one_qt_gradient_per_gradient_per_frame(self):
        shared = gradient("linear", (0, 0, 10, 0))
        op = ("fill", [("M", 0, 0), ("L", 1, 1), ("L", 0, 1), ("Z",)], shared, 1.0, "source-over", "nonzero")
        painter = FakePainter()
        effect_overlay.replay(painter, [self.pen_of([op, op]), self.pen_of([op])])
        self.assertEqual(len({id(draw[2]) for draw in painter.draws}), 1)

    def test_a_hard_stop_keeps_both_colours(self):
        stops = effect_overlay._stops([(0.0, "a"), (0.5, "b"), (0.5, "c"), (1.0, "d")])
        self.assertEqual([colour for _o, colour in stops], ["a", "b", "c", "d"])
        self.assertLess(stops[1][0], stops[2][0])

    def test_a_real_effect_replays_into_a_real_painter(self):
        from PySide6.QtGui import QImage

        image = QImage(900, 500, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        for effect_id in effects.EFFECT_IDS:
            for t in (1.2, 2.5):
                scene = effects.preview_scene(effects.effect(effect_id), t, "edge", ["#ff0000", "#00ff00"])
                effect_overlay.replay(painter, scene["pens"])
        painter.end()


@unittest.skipIf(effect_overlay is None, "needs PySide6")
class GeometryTests(unittest.TestCase):
    def test_union_of_bounds(self):
        a, b = effects.Pen(), effects.Pen()
        a.bounds, b.bounds = (0, 5, 10, 20), (-3, 8, 4, 30)
        self.assertEqual(effect_overlay.union_bounds([a, b, effects.Pen()]), (-3, 5, 10, 30))
        self.assertIsNone(effect_overlay.union_bounds([]))

    def test_the_region_is_the_owning_displays_edge_relative_to_the_desktop(self):
        rects = [Rect(-1920, 0, 1920, 1080), Rect(0, -200, 2560, 1440)]
        origin = (-1920, -200)
        self.assertEqual(effect_overlay.edge_region("left", rects, origin),
                         {"kind": "edge", "edge": "left", "x": 0.0, "y": 200.0, "w": 1.0, "h": 1080.0})
        self.assertEqual(effect_overlay.edge_region("right", rects, origin),
                         {"kind": "edge", "edge": "right", "x": 4479.0, "y": 0.0, "w": 1.0, "h": 1440.0})
        self.assertEqual(effect_overlay.edge_region("top", rects, origin)["y"], 0.0)
        self.assertEqual(effect_overlay.edge_region("bottom", rects, origin)["y"], 1439.0)

    def test_a_push_on_a_shorter_displays_exposed_edge_lights_that_display(self):
        # Side by side, the 1080-high display's bottom edge crosses too, 360 points above the
        # desktop's bottom; the effect belongs there, not on the taller display beside it.
        rects = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 2560, 1440)]
        region = effect_overlay.edge_region("bottom", rects, (0, 0), (100, 1079))
        self.assertEqual((region["x"], region["y"], region["w"]), (0.0, 1079.0, 1920.0))

    def test_part_of_the_edge_lights_only_the_third_being_pushed(self):
        rects = [Rect(0, 0, 1920, 1080)]
        region = effect_overlay.edge_region("right", rects, (0, 0), (1919, 900), "end")
        self.assertEqual((region["x"], region["y"], region["h"]), (1919.0, 720.0, 360.0))
        region = effect_overlay.edge_region("top", rects, (0, 0), (900, 0), "middle")
        self.assertEqual((region["x"], region["w"]), (640.0, 640.0))

    def test_a_third_is_the_display_under_the_pointers_own(self):
        # Stacked, each display's left edge has its own thirds, as PartEdge measures them.
        rects = [Rect(0, 0, 1920, 1080), Rect(0, 1080, 1920, 1080)]
        region = effect_overlay.edge_region("left", rects, (0, 0), (0, 1500), "middle")
        self.assertEqual((region["y"], region["h"]), (1440.0, 360.0))
        region = effect_overlay.edge_region("left", rects, (0, 0), (0, 100), "end")
        self.assertEqual((region["y"], region["h"]), (720.0, 360.0))


@unittest.skipIf(effect_overlay is None, "needs PySide6")
class OverlayTests(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])

    def test_the_window_never_takes_input_focus_or_a_taskbar_button(self):
        overlay = effect_overlay.EffectOverlay()
        flags = overlay.windowFlags()
        for flag in (Qt.WindowType.Tool, Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint,
                     Qt.WindowType.WindowTransparentForInput, Qt.WindowType.WindowDoesNotAcceptFocus):
            self.assertTrue(flags & flag, flag)
        self.assertTrue(overlay.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertTrue(overlay.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))

    def test_a_failing_effect_logs_once_and_turns_the_effects_off(self):
        failures = []
        overlay = effect_overlay.EffectOverlay(on_failure=lambda: failures.append(1))
        overlay.configure("skin", "signal")

        def boom(pen, s):
            raise RuntimeError("design bug")

        overlay._fx = effects.Effect("boom", "Boom", "quiet", "", boom, boom, 0.5, 0.5)
        with self.assertLogs(effect_overlay.LOGGER, "ERROR") as logged:
            overlay.push("right", 0.6, False, QPointF(10, 10))
            overlay._tick()
        self.assertEqual(len(logged.records), 1)
        self.assertIn("skin", logged.output[0])
        self.assertTrue(overlay.failed)
        self.assertEqual(failures, [1])
        self.assertTrue(overlay.isHidden())
        overlay.push("right", 0.6, False, QPointF(10, 10))
        self.assertFalse(overlay.player.busy)

    def test_effects_that_cannot_load_turn_themselves_off(self):
        failures = []
        overlay = effect_overlay.EffectOverlay(on_failure=lambda: failures.append(1))
        real = effects.effect

        def missing(_effect_id):
            raise ImportError("No module named 'fx_membrane'")

        effects.effect = missing
        try:
            with self.assertLogs(effect_overlay.LOGGER, "ERROR"):
                overlay.configure("skin", "signal")
        finally:
            effects.effect = real
        self.assertTrue(overlay.failed)
        self.assertEqual(failures, [1])
        overlay.push("right", 0.6, False, QPointF(10, 10))
        self.assertFalse(overlay.player.busy)

    def test_a_switch_with_glow_or_beam_plays_the_locator_where_the_pointer_is(self):
        for style in ("glow", "beam"):
            with self.subTest(style=style):
                overlay = effect_overlay.EffectOverlay()
                overlay.configure(style, "sunset")
                self.assertIs(overlay._fx, effects.LOCATOR)
                overlay.switched(QPointF(300, 200))
                arrival = overlay.player.arrival
                self.assertTrue(arrival["switch"])
                desk = overlay._desk
                self.assertEqual(arrival["at"], (300 - desk.x(), 200 - desk.y()))
                overlay.stop()

    def test_a_switch_with_an_effect_plays_that_effect(self):
        overlay = effect_overlay.EffectOverlay()
        overlay.configure("rupture", "neon")
        overlay.switched(QPointF(300, 200))
        self.assertEqual(overlay._fx.id, "rupture")
        self.assertEqual(overlay.player.arrival["method"], "switch")
        overlay.stop()

    def test_the_app_shows_a_switch_only_with_both_switches_on(self):
        import app_config
        import kvm_bridge_win

        def overlay_for(**change):
            config = replace(app_config.default_config(), glow_style="glow", **change)
            owner = SimpleNamespace(_closing=False, _config=config, effects=None, _on_effects_failed=lambda: None)
            return kvm_bridge_win.WindowsApplication._switch_overlay(owner)

        self.assertIsNotNone(overlay_for())
        self.assertIsNone(overlay_for(shortcut_arrival=False))
        self.assertIsNone(overlay_for(edge_glow=False))

    def test_the_window_margin_stops_at_a_pens_display(self):
        pen = effects.Pen()
        pen.fillRect(100, 100, 50, 50)
        pen.bounds, pen.clip = (100.0, 100.0, 150.0, 150.0), (100.0, 0.0, 400.0, 400.0)
        self.assertEqual(effect_overlay.union_bounds([pen], 2.0), (100.0, 98.0, 152.0, 152.0))

    def test_a_pen_that_names_its_display_is_drawn_inside_it(self):
        pen = effects.Pen()
        pen.fillRect(0, 0, 50, 50)
        pen.clip = (10.0, 20.0, 110.0, 220.0)
        painter = FakePainter()
        effect_overlay.replay(painter, [pen])
        self.assertEqual(painter.clips, [(10.0, 20.0, 100.0, 200.0)])
        self.assertEqual(painter._stack, [])

    def test_each_event_plays_on_the_display_under_the_pointer(self):
        overlay = effect_overlay.EffectOverlay()
        overlay.configure("skin", "signal")
        overlay.arrive("edge", "left", QPointF(10, 10))
        # A 150% display beside a 100% one, as Qt's logical geometry has them.
        overlay._displays = [(0.0, 0.0, 2560.0, 1440.0), (2560.0, 0.0, 1706.0, 960.0)]
        overlay._desk = QRect(0, 0, 4266, 1440)
        overlay.arrive("edge", "left", QPointF(3000, 1300))
        arrival = overlay.player.arrival
        overlay.stop()
        self.assertEqual((arrival["display"], arrival["at"]), ((2560.0, 0.0, 1706.0, 960.0), (3000.0, 959.0)))

    def test_the_event_that_starts_an_effect_draws_its_first_frame(self):
        overlay = effect_overlay.EffectOverlay()
        overlay.configure("rupture", "neon")
        overlay.arrive("edge", "left", QPointF(2, 200))
        drawn = bool(overlay._pens)
        overlay.stop()
        self.assertTrue(drawn)

    def test_a_corner_push_plays_the_corner_form_on_its_display(self):
        overlay = effect_overlay.EffectOverlay()
        overlay.configure("hyperdrive", "neon")
        overlay.push("left", 0.5, False, QPointF(1, 1), part="top_left")
        depart = overlay.player.depart
        overlay.stop()
        self.assertEqual((depart["method"], depart["region"]["kind"], depart["region"]["corner"]),
                         ("corner", "corner", "top_left"))
        self.assertEqual((depart["region"]["w"], depart["region"]["h"]), (8.0, 8.0))

    def test_a_push_ticks_each_quarter_once(self):
        overlay = effect_overlay.EffectOverlay()
        overlay.configure("skin", "signal")
        seen = []
        real_push = overlay.player.push
        overlay.player.push = lambda now, method, region, at, pressure, tick=None, display=None: (
            seen.append(tick), real_push(now, method, region, at, pressure, tick, display))
        for pressure in (0.1, 0.3, 0.3, 0.55, 0.8, 0.9):
            overlay.push("right", pressure, False, QPointF(10, 10))
        overlay.stop()
        self.assertEqual([tick for tick in seen if tick], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
