import unittest

try:
    import edge_glow
except ImportError:  # PySide6 is only in the Windows build venv
    edge_glow = None


@unittest.skipIf(edge_glow is None, "PySide6 is not installed")
class BeamMathsTests(unittest.TestCase):
    def test_comet_travels_faster_as_pressure_builds(self):
        self.assertEqual(edge_glow.traverse_seconds(0.0), edge_glow.SLOW_TRAVERSE_S)
        self.assertAlmostEqual(edge_glow.traverse_seconds(1.0), edge_glow.FAST_TRAVERSE_S)
        self.assertLess(edge_glow.traverse_seconds(0.7), edge_glow.traverse_seconds(0.2))

    def test_edge_is_brightest_at_the_comet_and_dim_away_from_it(self):
        self.assertEqual(edge_glow.comet_alpha(0.5, 0.5, 0.0), 1.0)
        self.assertEqual(edge_glow.comet_alpha(0.0, 0.5, 0.0), edge_glow.BEAM_BASE)

    def test_breakthrough_lights_the_whole_edge(self):
        self.assertEqual(edge_glow.comet_alpha(0.0, 0.5, 1.0), 1.0)


@unittest.skipIf(edge_glow is None, "PySide6 is not installed")
class GlowStateTests(unittest.TestCase):
    def test_a_breakthrough_flashes_then_fades_out(self):
        state = edge_glow.GlowState()
        state.push(0.6, False)
        state.push(1.0, True)
        self.assertEqual((state.pressure, state.flash, state.finish), (0.0, 1.0, 1.0))
        self.assertTrue(state.step(0.1, stale=True))
        self.assertFalse(state.step(5.0, stale=True))

    def test_pressure_holds_while_the_push_keeps_arriving(self):
        state = edge_glow.GlowState()
        state.push(0.5, False)
        state.step(0.2, stale=False)
        self.assertEqual(state.pressure, 0.5)


@unittest.skipIf(edge_glow is None, "PySide6 is not installed")
class PreviewScriptTests(unittest.TestCase):
    def test_the_push_builds_goes_through_and_rests(self):
        self.assertEqual(edge_glow.preview_frame(0.0), (0.0, "push"))
        level, phase = edge_glow.preview_frame(edge_glow.PUSH_S * 0.9)
        self.assertEqual(phase, "push")
        self.assertGreater(level, 0.8)
        self.assertEqual(edge_glow.preview_frame(edge_glow.PUSH_S + 0.1), (0.0, "after"))
        self.assertEqual(edge_glow.preview_frame(edge_glow.PUSH_S + edge_glow.AFTER_S + 0.1), (0.0, "rest"))

    def test_it_loops(self):
        loop = edge_glow.PUSH_S + edge_glow.AFTER_S + edge_glow.REST_S
        self.assertEqual(edge_glow.preview_frame(loop + 0.5), edge_glow.preview_frame(0.5))



@unittest.skipIf(edge_glow is None, "PySide6 is not installed")
class DarkColoursTest(unittest.TestCase):
    def test_a_dark_pack_is_lifted_on_a_dark_appearance_only(self):
        from unittest import mock

        from core import effects

        # Indigo ink, the darkest pack, though Ink is offered nowhere for now.
        def first(dark):
            with mock.patch.object(edge_glow.app_config, "palette_colours", return_value=effects.pack("indigo_ink")[1]):
                stops = edge_glow._colours("indigo_ink", True, 100, dark).stops()
            return stops[0][1].lightnessF()
        self.assertLess(first(False), 0.2)
        self.assertGreater(first(True), 0.4)


@unittest.skipIf(edge_glow is None, "PySide6 is not installed")
class PreviewLoopHoverTests(unittest.TestCase):
    """Glow and Beam's tiles hold a still; the one the pointer is over plays, from that still."""

    def setUp(self):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication, QWidget

        self.app = QApplication.instance() or QApplication([])
        self.parent = QWidget()
        self.loop = edge_glow.PreviewLoop(self.parent)
        self.tiles = [edge_glow.GlowPreview(style) for style in ("glow", "beam")]
        for tile in self.tiles:
            tile.isVisible = lambda: True
            tile.visibleRegion = lambda: type("Region", (), {"isEmpty": lambda self: False})()
            self.loop.add(tile)
        self.loop.run(True)

    def tearDown(self):
        self.loop.run(False)

    def test_every_tile_holds_a_still_until_one_is_hovered(self):
        self.loop._tick()
        self.assertEqual([tile.state.pressure for tile in self.tiles], [edge_glow.PreviewLoop.HOLD] * 2)
        self.assertFalse(any(self.loop.playing(tile) for tile in self.tiles))

    def test_a_pointer_passing_over_starts_nothing(self):
        self.loop.hover(self.tiles[1])
        self.loop._tick()
        self.assertFalse(self.loop.playing(self.tiles[1]))

    def test_the_hovered_tile_plays_on_from_its_still_and_settles_back(self):
        self.loop.hover(self.tiles[1])
        self.loop._hovered_at -= edge_glow.PreviewLoop.INTENT_S
        self.loop._tick()
        self.assertTrue(self.loop.playing(self.tiles[1]))
        self.assertAlmostEqual(self.tiles[1].state.pressure, edge_glow.PreviewLoop.HOLD, places=2)
        self.loop.hover(None)
        self.assertFalse(self.loop.playing(self.tiles[1]))
        self.assertEqual(self.tiles[1].state.pressure, edge_glow.PreviewLoop.HOLD)


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(edge_glow is None, "PySide6 is not installed")
class CornerGlowTests(unittest.TestCase):
    """Glow and Beam in a corner light the corner's two walls, and every window stays above the
    taskbar."""

    def setUp(self):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])

    def render(self, corner, style="glow", centre=0.5):
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QImage, QPainter, Qt

        size = edge_glow.CORNER_ARM_PX
        image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        state = edge_glow.GlowState()
        state.push(0.9, False)
        state.centre = centre
        painter = QPainter(image)
        edge_glow.paint_corner(painter, QRect(0, 0, size, size), corner, style, "signal", state)
        painter.end()
        return image

    def test_a_corner_lights_both_its_walls_brightest_where_they_meet(self):
        last = edge_glow.CORNER_ARM_PX - 1
        for corner, (cx, cy) in (("top_left", (0, 0)), ("top_right", (last, 0)),
                                 ("bottom_left", (0, last)), ("bottom_right", (last, last))):
            image = self.render(corner)
            alpha = lambda x, y: image.pixelColor(x, y).alpha()
            far_x = last if cx == 0 else 0
            far_y = last if cy == 0 else 0
            step_x = 1 if cx == 0 else -1
            step_y = 1 if cy == 0 else -1
            with self.subTest(corner=corner):
                self.assertGreater(alpha(cx, cy), 150)
                # A third of the way along each wall is lit, less than the corner itself.
                self.assertGreater(alpha(cx + step_x * 60, cy), 40)
                self.assertGreater(alpha(cx, cy + step_y * 60), 40)
                self.assertLess(alpha(cx + step_x * 60, cy), alpha(cx, cy))
                # Nothing at the far ends of the walls, nor out in the middle of the square.
                self.assertLess(alpha(far_x, cy), 8)
                self.assertLess(alpha(cx, far_y), 8)
                self.assertEqual(alpha(edge_glow.CORNER_ARM_PX // 2, edge_glow.CORNER_ARM_PX // 2), 0)

    def test_the_beams_comet_runs_in_along_one_wall_and_out_along_the_other(self):
        last = edge_glow.CORNER_ARM_PX - 1
        # bottom_right: the bottom wall from its left end to the corner, then up the right wall.
        early, late = self.render("bottom_right", "beam", 0.15), self.render("bottom_right", "beam", 0.85)
        on_bottom = lambda image: image.pixelColor(60, last).alpha()
        on_right = lambda image: image.pixelColor(last, 60).alpha()
        self.assertGreater(on_bottom(early), on_bottom(late))
        self.assertGreater(on_right(late), on_right(early))

    def test_a_corner_push_places_the_light_at_that_corner_of_the_display(self):
        from unittest import mock

        from PySide6.QtCore import QPoint

        glow = edge_glow.EdgeGlow()
        screen = self.app.primaryScreen().geometry()
        with mock.patch.object(edge_glow.QCursor, "pos", return_value=QPoint(screen.right() - 2, screen.bottom() - 2)):
            glow.set_pressure("bottom", 0.5, False, "bottom_right")
        size = min(edge_glow.CORNER_ARM_PX, screen.width(), screen.height())
        self.assertEqual((glow.geometry().right(), glow.geometry().bottom()), (screen.right(), screen.bottom()))
        self.assertEqual((glow.width(), glow.height()), (size, size))
        glow.hide()

    def test_every_frame_puts_the_window_back_above_the_taskbar(self):
        from unittest import mock

        glow = edge_glow.EdgeGlow()
        with mock.patch.object(edge_glow, "keep_on_top") as raised:
            glow.set_pressure("left", 0.5, False)
            glow._tick()
        self.assertEqual(raised.call_count, 2)
        glow.hide()


@unittest.skipIf(edge_glow is None, "PySide6 is not installed")
class ThirdEndsTests(unittest.TestCase):
    def setUp(self):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])

    def test_a_third_of_the_edge_fades_in_and_out_at_its_ends(self):
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QImage, QPainter, Qt

        for style in ("glow", "beam"):
            for taper in (False, True):
                image = QImage(44, 400, QImage.Format.Format_ARGB32_Premultiplied)
                image.fill(Qt.GlobalColor.transparent)
                state = edge_glow.GlowState()
                state.push(0.9, False)
                state.flash = 1.0
                painter = QPainter(image)
                edge_glow.paint(painter, QRect(0, 0, 44, 400), "right", style, "signal", state, taper=taper)
                painter.end()
                end, middle = image.pixelColor(43, 1).alpha(), image.pixelColor(43, 200).alpha()
                with self.subTest(style=style, taper=taper):
                    self.assertGreater(middle, 100)
                    if taper:
                        self.assertLess(end, 12)
                    else:
                        self.assertGreater(end, 100)
