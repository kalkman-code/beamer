"""The pairing QR is drawn with Core Image's generator and must read back as the text it was given."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import AppKit
import Quartz

import qr

TEXT = "beamer://pair?host=192.168.1.20&port=24821&code=123456&k=" + "Ab_-" * 5 + "Ab"


def decoded(image):
    rep = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
    ci = Quartz.CIImage.alloc().initWithBitmapImageRep_(rep)
    detector = Quartz.CIDetector.detectorOfType_context_options_("CIDetectorTypeQRCode", None, {"CIDetectorAccuracy": "CIDetectorAccuracyHigh"})
    return [feature.messageString() for feature in detector.featuresInImage_(ci)]


class QRTests(unittest.TestCase):
    def test_the_drawing_reads_back_as_the_text(self):
        self.assertEqual(decoded(qr.image(TEXT, 220)), [TEXT])

    def test_the_modules_are_a_square_with_the_finder_pattern_in_three_corners(self):
        grid = qr.modules(TEXT)
        size = len(grid)
        self.assertTrue(all(len(row) == size for row in grid))
        for row, col in ((0, 0), (0, size - 7), (size - 7, 0)):
            self.assertTrue(all(grid[row][col + i] and grid[row + 6][col + i] for i in range(7)))

    def test_the_image_is_whole_modules_no_bigger_than_asked_and_dark_on_light_whatever_the_appearance(self):
        image = qr.image(TEXT, 180)
        width = image.size().width
        self.assertEqual(width, image.size().height)
        self.assertLessEqual(width, 180)
        self.assertGreater(width, 180 - (len(qr.modules(TEXT)) + 2 * qr.QUIET_MODULES))
        self.assertEqual(width % (len(qr.modules(TEXT)) + 2 * qr.QUIET_MODULES), 0)
        rep = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
        corner = rep.colorAtX_y_(1, 1).redComponent()
        self.assertGreater(corner, 0.95)

    def test_a_text_that_does_not_fit_is_refused(self):
        with self.assertRaises(ValueError):
            qr.modules("x" * 4000)


if __name__ == "__main__":
    unittest.main()
