import json
import sys
import unittest
import zlib
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mac_app"))

from core.tests import render_effect_parity as parity

parity.SCALE = 2.0
REFERENCE = Path(__file__).parent / "fixtures" / "effects_parity" / "qt_reference_frames.zip"


def effect_region(rgba, width, height):
    left = round((800 - 150) * parity.SCALE)
    right = round((800 + 150) * parity.SCALE)
    stride = width * 4
    return b"".join(rgba[row * stride + left * 4:row * stride + right * 4]
                    for row in range(height))


def pixel_difference(actual, expected):
    total = over_32 = maximum = 0
    pixels = len(actual) // 4
    for offset in range(0, len(actual), 4):
        difference = max(abs(actual[offset + channel] - expected[offset + channel])
                         for channel in range(4))
        total += difference
        over_32 += difference > 32
        maximum = max(maximum, difference)
    return total / pixels, maximum, over_32 / pixels


class MacEffectParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.references = ZipFile(REFERENCE)
        cls.manifest = json.loads(cls.references.read("manifest.json"))

    @classmethod
    def tearDownClass(cls):
        cls.references.close()

    def test_each_effect_matches_its_windows_reference_frame(self):
        for _direction, effect_id, pack_id in parity.catalogue():
            if effect_id == "beam":
                continue
            for dark in (True, False):
                mode = "dark" if dark else "light"
                with self.subTest(effect=effect_id, mode=mode):
                    width, height, actual = parity.frame(effect_id, pack_id, 1.78, dark=dark)
                    expected = zlib.decompress(
                        self.references.read(f"{effect_id}-{mode}-push85.rgba.z")
                    )
                    mean, maximum, large_pixel_share = pixel_difference(
                        effect_region(actual, width, height), effect_region(expected, width, height)
                    )
                    self.assertLessEqual(mean, 0.5, f"mean pixel difference {mean:.3f}")
                    self.assertLessEqual(large_pixel_share, 0.001,
                                         f"pixels differing by over 32 levels: {large_pixel_share:.4%}")
                    self.assertLessEqual(maximum, 100, f"maximum pixel difference {maximum}")

    def test_beam_edge_alpha_profile_matches_windows_across_every_stage(self):
        scene_width, scene_height = parity.effects.preview_size("edge")
        width, height = round(scene_width * parity.SCALE), round(scene_height * parity.SCALE)
        edge, radius = round(800 * parity.SCALE), round(40 * parity.SCALE)
        start = edge - radius

        for dark in (True, False):
            mode = "dark" if dark else "light"
            for t, stage in zip(parity.TIMES, parity.STAGES):
                with self.subTest(mode=mode, stage=stage):
                    _width, _height, actual = parity.frame("beam", "signal", t, dark=dark, transparent=True)
                    expected = zlib.decompress(
                        self.references.read(f"beam-{mode}-{stage}-alpha.rgba.z")
                    )
                    for x in range(start, edge + 1):
                        actual_alpha = sum(actual[(y * width + x) * 4 + 3] for y in range(height)) / height
                        expected_alpha = sum(expected[(y * width + x) * 4 + 3] for y in range(height)) / height
                        self.assertLessEqual(abs(actual_alpha - expected_alpha), 3.0,
                                             f"Beam alpha differs at x={x} on {mode} {stage}")


if __name__ == "__main__":
    unittest.main()
