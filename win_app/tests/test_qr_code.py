import os
import sys
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qr_code
from core import pairing


@unittest.skipIf(qr_code.segno is None, "needs segno")
class QrCodeTest(unittest.TestCase):
    TEXT = pairing.qr_text("192.168.77.5", 24821, "123456", bytes(range(16)))

    def test_the_grid_is_square_with_a_quiet_zone_and_the_three_finder_corners(self):
        rows = qr_code.modules(self.TEXT)
        size = len(rows)
        self.assertTrue(all(len(row) == size for row in rows))
        self.assertFalse(any(any(row) for row in rows[: qr_code.QUIET]))
        self.assertFalse(any(any(row[: qr_code.QUIET]) for row in rows))
        top, left, right = qr_code.QUIET, qr_code.QUIET, size - qr_code.QUIET - 7
        for row_at, column_at in ((top, left), (top, right), (size - qr_code.QUIET - 7, left)):
            self.assertTrue(all(rows[row_at][column_at + step] for step in range(7)))
            self.assertTrue(all(rows[row_at + 6][column_at + step] for step in range(7)))

    def test_the_text_of_a_real_code_fits_a_small_symbol(self):
        self.assertLessEqual(len(qr_code.modules(self.TEXT)), 37 + 2 * qr_code.QUIET + 8)


if __name__ == "__main__":
    unittest.main()
