import gc
import sys
import unittest
import weakref
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import AppKit

AppKit.NSApplication.sharedApplication()

import theme
import widgets


class WidthTests(unittest.TestCase):
    def test_figure_renders_its_unit_once(self):
        figure = widgets.Figure('120', theme.TYPE['numeral'], 'px')
        self.assertEqual(figure.view.attributedStringValue().string(), '120 px')
        self.assertEqual(figure.view.accessibilityLabel(), '120 px')

    def test_us_region_changes_rendered_text_and_accessibility_without_changing_source(self):
        from core import locale

        with mock.patch.object(locale, "is_us_region", return_value=True):
            label = widgets.Label("Colourful behaviour")
            button = widgets.Button("Minimise", None, None)

        self.assertEqual(label.view.stringValue(), "Colorful behavior")
        self.assertEqual(label.view.accessibilityLabel(), "Colorful behavior")
        self.assertEqual(label.text, "Colourful behaviour")
        self.assertEqual(button.view.title(), "Minimize")
        self.assertEqual(button.title, "Minimise")

    def test_nonwrapping_label_updates_full_text_metadata(self):
        label = widgets.Label("Short name")
        full_name = "A very long workstation name with translated length"
        label.view.setFrameSize_((100, 20))

        label.set(full_name)

        paragraph = label.view.attributedStringValue().attribute_atIndex_effectiveRange_(
            AppKit.NSParagraphStyleAttributeName, 0, None)[0]
        visible_width = label.view.cell().drawingRectForBounds_(label.view.bounds()).size.width
        self.assertGreater(label.view.attributedStringValue().size().width, visible_width)
        self.assertEqual(paragraph.lineBreakMode(), AppKit.NSLineBreakByTruncatingTail)
        self.assertEqual(label.view.toolTip(), full_name)
        self.assertEqual(label.view.accessibilityLabel(), full_name)

    def test_nonwrapping_label_update_preserves_explicit_tooltip(self):
        label = widgets.Label("Short name")
        label.view.setToolTip_("Use this explanatory tooltip")

        label.set("A longer machine name")

        self.assertEqual(label.view.toolTip(), "Use this explanatory tooltip")

    def test_nonwrapping_figure_update_keeps_full_text_metadata_when_elided(self):
        figure = widgets.Figure("Short", theme.TYPE["readout"])
        full_name = "A very long workstation name with translated length"
        figure.view.setFrameSize_((100, 30))

        figure.set(full_name)

        paragraph = figure.view.attributedStringValue().attribute_atIndex_effectiveRange_(
            AppKit.NSParagraphStyleAttributeName, 0, None)[0]
        visible_width = figure.view.cell().drawingRectForBounds_(figure.view.bounds()).size.width
        self.assertGreater(figure.view.attributedStringValue().size().width, visible_width)
        self.assertEqual(paragraph.lineBreakMode(), AppKit.NSLineBreakByTruncatingTail)
        self.assertEqual(figure.view.toolTip(), full_name)
        self.assertEqual(figure.view.accessibilityLabel(), full_name)

    def test_ring_overlay_does_not_retain_its_owner(self):
        host = widgets.box()
        ring = widgets.Ring(host, 4)
        overlay = ring.overlay
        owner = weakref.ref(ring)
        del ring
        gc.collect()
        self.assertIsNone(owner())
        overlay.ringPositionChanged_(None)

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
