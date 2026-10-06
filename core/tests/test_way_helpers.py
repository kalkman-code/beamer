import unittest

from core import ways


class WayHelperTests(unittest.TestCase):
    def test_part_names_follow_the_edge_direction(self):
        self.assertEqual(ways.part_names("right"), {"start": "Top", "middle": "Middle", "end": "Bottom"})
        self.assertEqual(ways.part_names("bottom"), {"start": "Left", "middle": "Middle", "end": "Right"})

    def test_parts_phrase_orders_known_parts_and_defaults_to_middle(self):
        self.assertEqual(ways.parts_phrase("left", ["end", "start"]), "the top and bottom of the left edge")
        self.assertEqual(ways.parts_phrase("top", ["unknown"]), "the middle of the top edge")

    def test_toggle_part_keeps_one_part_and_uses_edge_order(self):
        self.assertEqual(ways.toggle_part(["start", "end"], "start", False), ["end"])
        self.assertEqual(ways.toggle_part(["middle"], "middle", False), ["middle"])
        self.assertEqual(ways.toggle_part(["middle"], "start", True), ["start", "middle"])


if __name__ == "__main__":
    unittest.main()
