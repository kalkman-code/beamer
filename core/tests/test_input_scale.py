import unittest

from core import receiver


class InputScaleTests(unittest.TestCase):
    def test_a_slower_pointer_carries_its_fractions(self):
        scale = receiver.InputScale(pointer=0.5)
        moves = [scale.move(1, 1) for _ in range(4)]
        self.assertEqual(moves, [(0, 0), (1, 1), (0, 0), (1, 1)])

    def test_a_faster_pointer_multiplies(self):
        self.assertEqual(receiver.InputScale(pointer=2.0).move(3, -4), (6, -8))

    def test_speeds_are_held_to_their_range(self):
        scale = receiver.InputScale(pointer=99, scroll=0)
        self.assertEqual((scale.pointer, scale.scroll), (4.0, 0.25))

    def test_scroll_speed_and_reverse(self):
        self.assertEqual(receiver.InputScale(scroll=2.0).wheel(3, 1), (6.0, 2.0))
        self.assertEqual(receiver.InputScale(reverse=True).wheel(3, -1), (-3.0, 1.0))
        self.assertEqual(receiver.InputScale().wheel(3, 1), (3, 1))


if __name__ == "__main__":
    unittest.main()
