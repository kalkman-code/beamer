import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import notch_beam


class TurnTest(unittest.TestCase):
    def test_travel_quickens_with_pressure_and_stays_in_range(self):
        self.assertEqual(notch_beam.turn_seconds(0.0), notch_beam.SLOW_TURN_S)
        self.assertAlmostEqual(notch_beam.turn_seconds(1.0), notch_beam.FAST_TURN_S)
        self.assertLess(notch_beam.turn_seconds(0.6), notch_beam.turn_seconds(0.3))
        self.assertAlmostEqual(notch_beam.turn_seconds(5.0), notch_beam.FAST_TURN_S)


class LapFinishTest(unittest.TestCase):
    def test_leaves_by_the_far_end_of_a_lap(self):
        for position in (0.0, 0.4, 0.9, 1.3):
            for after in (0.6, 1.2, 2.0, 3.0):
                with self.subTest(position=position, after=after):
                    travel = notch_beam.lap_finish(position, after)
                    self.assertGreaterEqual(travel, notch_beam.LAP - position - 1e-9)
                    laps = (position + travel) / notch_beam.LAP
                    self.assertAlmostEqual(laps, round(laps), places=9)

    def test_longer_durations_run_more_laps(self):
        self.assertGreater(notch_beam.lap_finish(0.3, 3.0), notch_beam.lap_finish(0.3, 0.6))


class CometTest(unittest.TestCase):
    def test_nothing_shows_at_either_end_of_a_lap(self):
        for position in (0.0, notch_beam.LAP):
            with self.subTest(position=position):
                self.assertTrue(all(end <= start for start, end in notch_beam.comet_segments(position)))

    def test_segments_nest_around_the_head_mid_lap(self):
        spans = notch_beam.comet_segments(notch_beam.LAP / 2.0)
        for (outer_start, outer_end), (inner_start, inner_end) in zip(spans, spans[1:]):
            self.assertLessEqual(outer_start, inner_start)
            self.assertGreaterEqual(outer_end, inner_end)
        head = notch_beam.LAP / 2.0 - notch_beam.COMET_HALF
        self.assertAlmostEqual((spans[-1][0] + spans[-1][1]) / 2.0, head)

    def test_travels_the_border_at_an_even_pace(self):
        # Equal steps in position move the head equal distances along the border, which the
        # angular sweep it replaced did not.
        heads = [sum(notch_beam.comet_segments(p)[-1]) / 2.0 for p in (0.4, 0.5, 0.6, 0.7)]
        steps = [round(b - a, 6) for a, b in zip(heads, heads[1:])]
        self.assertEqual(len(set(steps)), 1)


class EdgeCometTest(unittest.TestCase):
    def test_edge_is_brightest_at_the_comet_and_all_lit_at_breakthrough(self):
        self.assertEqual(notch_beam.edge_comet_alpha(0.5, 0.5, 0.0), 1.0)
        self.assertEqual(notch_beam.edge_comet_alpha(0.0, 0.5, 0.0), notch_beam.EDGE_BASE)
        self.assertEqual(notch_beam.edge_comet_alpha(0.0, 0.5, 1.0), 1.0)
        self.assertLess(notch_beam.edge_traverse_seconds(0.8), notch_beam.edge_traverse_seconds(0.2))


class PaletteTest(unittest.TestCase):
    def test_every_colour_choice_is_a_palette_a_gradient_can_use(self):
        import crossing
        import tokens

        self.assertEqual(set(crossing.GLOW_COLOURS) | {'aurora'}, set(tokens.PALETTES))
        for name in tokens.PALETTES:
            with self.subTest(colour=name):
                self.assertGreaterEqual(len(notch_beam.palette_colours(name)), 2)
        closed = notch_beam.palette_colours("colourful", closed=True)
        self.assertEqual(len(closed), len(tokens.PALETTES["colourful"]) + 1)


class ApproachTest(unittest.TestCase):
    def test_rises_faster_than_it_fades_and_never_overshoots(self):
        up = notch_beam.approach(0.0, 1.0, 0.06)
        down = 1.0 - notch_beam.approach(1.0, 0.0, 0.06)
        self.assertGreater(up, down)
        self.assertEqual(notch_beam.approach(0.9, 1.0, 1.0), 1.0)
        self.assertEqual(notch_beam.approach(0.1, 0.0, 1.0), 0.0)

    def test_a_released_push_fades_out_within_half_a_second(self):
        strength = 1.0
        for _ in range(30):
            strength = notch_beam.approach(strength, 0.0, 1 / 60)
        self.assertEqual(strength, 0.0)


if __name__ == "__main__":
    unittest.main()
