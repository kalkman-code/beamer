import unittest
from unittest import mock
from types import SimpleNamespace

from core import effects


class EdgeDepthTests(unittest.TestCase):
    def test_each_size_scales_from_the_shorter_screen_side(self):
        expected = {
            "small": (29.46, 64.8),
            "medium": (44.19, 97.2),
            "large": (58.92, 129.6),
        }
        for size, (laptop, television) in expected.items():
            with self.subTest(size=size):
                self.assertAlmostEqual(effects.edge_depth(1512, 982, size), laptop)
                self.assertAlmostEqual(effects.edge_depth(3840, 2160, size), television)

    def test_small_displays_keep_a_usable_floor_and_large_displays_a_ceiling(self):
        self.assertEqual(effects.edge_depth(320, 240, "small"), 24.0)
        self.assertEqual(effects.edge_depth(16000, 9000, "large"), 144.0)

    def test_glow_and_beam_draw_the_selected_depth(self):
        expected = {"small": 29.46, "medium": 44.19, "large": 58.92}
        for size, depth in expected.items():
            with self.subTest(size=size):
                region = SimpleNamespace(kind="edge", edge="right", x=1511.0, y=0.0, w=1.0, h=982.0)
                for style in ("glow", "beam"):
                    state = effects.state(1512, 982, "mac", "edge", ["#ffffff"], effect_size=size,
                                          region=region, point=effects.point(1511, 491), along=0.5,
                                          pressure=1.0, tick=None, since=None)
                    pen = effects.draw(effects.CLASSIC[style], "depart", state)
                    furthest_layer = max(abs(op[1][1][1] - op[1][0][1]) for op in pen.ops)
                    self.assertAlmostEqual(furthest_layer, depth * (0.45 if style == "beam" else 1.0), places=5)

    def test_length_uses_the_new_paces(self):
        self.assertEqual([effects.pace(value) for value in ("short", "normal", "long")], [0.5, 1.0, 2.0])

    def test_live_player_passes_the_selected_size_to_edge_and_arrival_effects(self):
        player = effects.Player()
        player.effect_size = "large"
        player.push(0.0, "edge", {"kind": "edge", "edge": "right", "x": 1511.0, "y": 0.0,
                                   "w": 1.0, "h": 982.0}, (1511.0, 491.0), 0.8)
        player.arrive(0.0, "edge", (0.0, 491.0), "left")
        with mock.patch.object(effects, "state", wraps=effects.state) as make_state:
            player.frames(0.2, effects.effect("rupture"), (1512.0, 982.0, "windows"),
                          ["#ffffff"], False, True)
        self.assertEqual([call.kwargs["effect_size"] for call in make_state.call_args_list], ["large", "large"])


if __name__ == "__main__":
    unittest.main()
