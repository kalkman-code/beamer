"""The Qt colour catalogue and native preview pixels on an offscreen platform."""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.render_colour_choices import QtRenderer, NEW_COLOURS, colour_rows, sample


class ColourPixelTests(unittest.TestCase):
    def test_ring_arrival_preview_draws_the_new_bright_colour(self):
        renderer = QtRenderer()
        renderer.set_dark(False)
        from tools.render_colour_choices import sample
        _image, pixels = sample(renderer, 'switch:locator', 'citron')
        self.assertGreater(pixels, 0)

    def test_every_colour_group_has_five_choices_and_mono_has_its_own_home(self):
        groups = colour_rows("qt")
        self.assertTrue(NEW_COLOURS <= {value for _group, values in groups for value, _title in values})
        self.assertEqual([values for name, values in groups if name == "Mono"], [[
            ("mono", "White"), ("mono_silver", "Silver"), ("mono_graphite", "Graphite"),
            ("mono_ink", "Ink"), ("mono_warm", "Warm grey")]])
        self.assertTrue(any(name == "Bright" for name, _values in groups))
        for name, values in groups:
            self.assertEqual(len(values), 5, name)

    def test_every_colour_draws_visible_pixels_in_every_native_preview(self):
        renderer = QtRenderer()
        for dark in (False, True):
            renderer.set_dark(dark)
            for group, values in colour_rows("qt"):
                for colour, _name in values:
                    for style in renderer.styles:
                        with self.subTest(group=group, colour=colour, style=style, dark=dark):
                            _image, changed = sample(renderer, style, colour)
                            self.assertGreater(changed, 0)


if __name__ == "__main__":
    unittest.main()
