import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import AppKit

AppKit.NSApplication.sharedApplication()

import theme
import widgets


class WidthTests(unittest.TestCase):
    def test_reading_note_stays_capped_inside_a_wider_stack(self):
        parent = widgets.stack()
        note = widgets.note("A note that can wrap.")

        widgets.add(parent, note.view)

        constraints = list(note.view.constraints())
        cap = next(c for c in constraints if c.firstAttribute() == AppKit.NSLayoutAttributeWidth
                   and c.relation() == AppKit.NSLayoutRelationLessThanOrEqual)
        reach = next(c for c in parent.constraints() if c.firstItem() == note.view
                     and c.secondItem() == parent and c.firstAttribute() == AppKit.NSLayoutAttributeWidth)
        fill_to_cap = next(c for c in note.view.constraints() if c.firstItem() == note.view
                           and c.secondItem() is None and c.firstAttribute() == AppKit.NSLayoutAttributeWidth
                           and c.relation() == AppKit.NSLayoutRelationEqual)
        self.assertEqual(cap.constant(), theme.READING_WIDTH)
        self.assertEqual(reach.relation(), AppKit.NSLayoutRelationLessThanOrEqual)
        self.assertEqual(fill_to_cap.priority(), 255)


if __name__ == "__main__":
    unittest.main()
