"""What plays when input arrives by the shortcut, a menu or a switch rather than a crossing: the
Player's switch arrival, the locator Glow and Beam get, and every effect's arrival away from an
edge. Byte-identical in mac_app and win_app."""

import math
import unittest

from core import effects

SCREEN = (1728.0, 1117.0, "mac")
MID = (700.0, 460.0)
PALETTE = ["#ff7828", "#ff3264", "#ffb43c"]


def frames(player, now, fx=None, reduced=False, dark=True):
    return player.frames(now, fx or effects.LOCATOR, SCREEN, PALETTE, reduced, dark)


def arrival_state(fx, since, reduced=False, dark=True, palette=PALETTE):
    x, y = MID
    edge = effects.nearest_edge(x, y, SCREEN[0], SCREEN[1])
    return effects.state(SCREEN[0], SCREEN[1], "mac", "switch", palette, reduced=reduced, dark=dark,
                         point=effects.point(x, y), edge=edge, since=since)


class PlayerSwitchTests(unittest.TestCase):
    def test_a_switch_waits_for_the_settle_then_plays_at_the_pointer(self):
        player = effects.Player()
        player.switched(10.0, MID, "top")
        self.assertEqual(frames(player, 10.0 + effects.SWITCH_SETTLE_S / 2), [])
        self.assertTrue(player.busy)
        pens = frames(player, 10.0 + effects.SWITCH_SETTLE_S + 0.1)
        self.assertEqual(len(pens), 1)
        left, top, right, bottom = pens[0].bounds
        self.assertTrue(left < MID[0] < right and top < MID[1] < bottom)

    def test_a_switch_plays_out_and_ends(self):
        player = effects.Player()
        player.switched(10.0, MID, "top")
        self.assertEqual(frames(player, 10.0 + effects.SWITCH_SETTLE_S + 1.0), [])
        self.assertFalse(player.busy)

    def test_a_crossing_arriving_during_the_settle_is_the_one_that_plays(self):
        seen = []
        fx = effects.Effect("probe", "Probe", "quiet", "", lambda pen, s: None,
                            lambda pen, s: seen.append(s.method) or pen.fillRect(0, 0, 4, 4), 0.1, 0.5)
        player = effects.Player()
        player.switched(10.0, MID, "top")
        player.arrive(10.05, "edge", (2.0, 300.0), "left")
        for step in range(10):
            frames(player, 10.05 + step * 0.05, fx)
        self.assertEqual(set(seen), {"edge"})

    def test_a_switch_just_after_a_crossing_arrival_gives_way_to_it(self):
        player = effects.Player()
        player.arrive(10.0, "edge", (2.0, 300.0), "left")
        player.switched(10.0 + effects.SWITCH_SETTLE_S / 2, MID, "top")
        self.assertFalse(player.arrival["switch"])
        self.assertEqual(player.arrival["at"], (2.0, 300.0))

    def test_a_crossing_drawn_elsewhere_still_cancels_a_waiting_switch(self):
        # Glow and Beam draw a crossing themselves and never tell the Player about it.
        player = effects.Player()
        player.switched(10.0, MID, "top")
        player.crossed_in(10.05)
        self.assertEqual(frames(player, 10.0 + effects.SWITCH_SETTLE_S + 0.1), [])
        self.assertFalse(player.busy)
        player.switched(10.1, MID, "top")
        self.assertIsNone(player.arrival)

    def test_a_later_switch_replaces_an_old_crossing_arrival(self):
        player = effects.Player()
        player.arrive(10.0, "edge", (2.0, 300.0), "left")
        player.switched(10.5, MID, "top")
        self.assertTrue(player.arrival["switch"])
        self.assertEqual(player.arrival["at"], MID)

    def test_a_switch_is_not_told_about_the_notch(self):
        seen = []
        fx = effects.Effect("probe", "Probe", "quiet", "", lambda pen, s: None,
                            lambda pen, s: seen.append(getattr(s, "notch", None)), 0.1, 0.5)
        player = effects.Player()
        player.switched(10.0, (860.0, 40.0), "top")
        notch = {"x": 771, "y": 0, "w": 185, "h": 32, "r": 10}
        player.frames(10.0 + effects.SWITCH_SETTLE_S + 0.1, fx, SCREEN, PALETTE, False, True, notch)
        self.assertEqual(seen, [None])


class SwitchStyleTests(unittest.TestCase):
    def test_same_as_crossing_follows_the_crossing_style(self):
        self.assertIs(effects.switch_effect("match", "glow"), effects.LOCATOR)
        self.assertIs(effects.switch_effect("match", "beam"), effects.LOCATOR)
        self.assertEqual(effects.switch_effect("match", "rupture").id, "rupture")

    def test_a_chosen_style_wins_over_the_crossing_style(self):
        self.assertIs(effects.switch_effect("locator", "rupture"), effects.LOCATOR)
        self.assertEqual(effects.switch_effect("discharge", "glow").id, "discharge")

    def test_the_picker_offers_every_choice_once(self):
        offered = [value for _title, items in effects.switch_style_groups() for value, _name in items]
        self.assertEqual(sorted(offered), sorted(effects.SWITCH_STYLES))
        self.assertEqual(offered[:2], ["match", "locator"])

    def test_a_switch_plays_its_own_effect_beside_the_crossing_one(self):
        seen = []
        own = effects.Effect("own", "Own", "quiet", "", lambda pen, s: None,
                             lambda pen, s: seen.append("own") or pen.fillRect(0, 0, 4, 4), 0.1, 0.5)
        crossing_fx = effects.Effect("crossing", "Crossing", "quiet", "", lambda pen, s: None,
                                     lambda pen, s: seen.append("crossing"), 0.1, 0.5)
        player = effects.Player()
        player.switched(10.0, MID, "top", own)
        frames(player, 10.0 + effects.SWITCH_SETTLE_S + 0.1, crossing_fx)
        self.assertEqual(seen, ["own"])


class SwitchPreviewTests(unittest.TestCase):
    def test_the_loop_rests_then_plays_round_the_pointer(self):
        for fx in (effects.LOCATOR, effects.effect("rupture")):
            with self.subTest(fx=fx.id):
                self.assertEqual(effects.preview_switch_scene(fx, 0.1, PALETTE)["pens"], [])
                scene = effects.preview_switch_scene(fx, 0.55, PALETTE)
                self.assertTrue(scene["pens"])
                self.assertEqual(len(scene["screens"]), 1)
                self.assertEqual(effects.preview_size("switch"), effects.PREVIEW_SCREEN)


class NearestEdgeTests(unittest.TestCase):
    def test_each_side(self):
        self.assertEqual(effects.nearest_edge(10, 500, 1000, 1000), "left")
        self.assertEqual(effects.nearest_edge(990, 500, 1000, 1000), "right")
        self.assertEqual(effects.nearest_edge(500, 10, 1000, 1000), "top")
        self.assertEqual(effects.nearest_edge(500, 990, 1000, 1000), "bottom")


class LocatorTests(unittest.TestCase):
    def _bounds(self, since, reduced):
        pen = effects.draw(effects.LOCATOR, "arrive", arrival_state(effects.LOCATOR, since, reduced))
        return None if pen is None else pen.bounds

    def test_it_is_never_more_than_260pt_across(self):
        for reduced in (False, True):
            for step in range(0, 80):
                box = self._bounds(step / 100.0, reduced)
                if box is None:
                    continue
                with self.subTest(since=step / 100.0, reduced=reduced):
                    # The Pen pads its bounds by the stroke and two points each side.
                    self.assertLessEqual(box[2] - box[0], 260.0 + 12.0)
                    self.assertLessEqual(box[3] - box[1], 260.0 + 12.0)

    def test_it_closes_onto_the_pointer(self):
        start, end = self._bounds(0.05, False), self._bounds(0.45, False)
        self.assertGreater(start[2] - start[0], 3 * (end[2] - end[0]))
        for box in (start, end):
            self.assertAlmostEqual((box[0] + box[2]) / 2, MID[0], delta=1.0)
            self.assertAlmostEqual((box[1] + box[3]) / 2, MID[1], delta=1.0)

    def test_reduce_motion_holds_its_size(self):
        early, late = self._bounds(0.15, True), self._bounds(0.4, True)
        self.assertAlmostEqual(early[2] - early[0], late[2] - late[0], delta=0.5)

    def test_it_is_over_within_0_8s(self):
        self.assertLessEqual(effects.LOCATOR.arrive_seconds, 0.8)
        self.assertIsNone(self._bounds(effects.LOCATOR.arrive_seconds + 0.06, False))

    def test_one_colour_and_many_both_draw(self):
        for palette in (["#5fd4f4"], PALETTE):
            with self.subTest(palette=palette):
                pen = effects.draw(effects.LOCATOR, "arrive", arrival_state(effects.LOCATOR, 0.2, palette=palette))
                self.assertTrue(pen.ops)


class EveryEffectAwayFromAnEdgeTests(unittest.TestCase):
    def test_each_arrival_plays_round_the_pointer(self):
        for effect_id in effects.EFFECT_IDS:
            fx = effects.effect(effect_id)
            for reduced in (False, True):
                for dark in (True, False):
                    with self.subTest(effect=effect_id, reduced=reduced, dark=dark):
                        pen = effects.draw(fx, "arrive", arrival_state(fx, 0.12, reduced, dark))
                        self.assertIsNotNone(pen, "drew nothing")
                        left, top, right, bottom = pen.bounds
                        centre = ((left + right) / 2, (top + bottom) / 2)
                        # Centred on the pointer, not left at the nearest edge 460pt away.
                        self.assertLess(math.dist(centre, MID), 60.0)


if __name__ == "__main__":
    unittest.main()
