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
    def test_action_survives_when_only_the_native_button_is_retained(self):
        import objc

        calls = []
        with objc.autorelease_pool():
            button = widgets.action_button("Send input", lambda: calls.append("sent"))
            view = button.view
            target = objc.WeakRef(button.target)
            del button
        gc.collect()
        with objc.autorelease_pool():
            self.assertIsNotNone(target())
            self.assertTrue(view.sendAction_to_(view.action(), view.target()))
        self.assertEqual(calls, ["sent"])
        with objc.autorelease_pool():
            del view
        gc.collect()
        with objc.autorelease_pool():
            self.assertIsNone(target())

    def test_idle_design_lock_does_not_redraw_unchanged_choices(self):
        import objc

        for choice in (widgets.ChoiceTiles([("one", "One", "Detail", widgets.box())]),
                       widgets.Swatches([("one", "One", ["#112233", "#445566"])])):
            choice.value = "one"
            with mock.patch.object(choice.ring, "show", wraps=choice.ring.show) as show:
                for _ in range(500):
                    with objc.autorelease_pool():
                        choice.set_enabled(True)
                self.assertEqual(show.call_count, 0)
                choice.set_enabled(False)
                self.assertEqual(show.call_count, 1)
                choice.set_enabled(False)
                self.assertEqual(show.call_count, 1)
                choice.set_enabled(True)
                self.assertEqual(show.call_count, 2)

    def test_menu_choices_update_without_replacing_the_popup(self):
        choice = widgets.MenuChoice([("", "None"), ("peer", "Peer")])
        popup = choice.popup
        item = choice.items[1][1]
        choice.value = "peer"
        choice.set_choices([("", "None"), ("peer", "Peer")])
        self.assertEqual(choice.items[1][1], item)
        choice.set_choices([("", "None"), ("peer", "Renamed"), ("other", "Other")])
        self.assertIs(choice.popup, popup)
        self.assertEqual(choice.value, "peer")
        self.assertEqual(choice.popup.titleOfSelectedItem(), "Renamed")
        choice.set_choices([("", "None")])
        self.assertEqual(choice.value, "")

    def test_replaced_menu_choices_release_their_native_targets(self):
        import objc

        host = widgets.stack()
        owners = []
        for _ in range(500):
            with objc.autorelease_pool():
                for view in host.arrangedSubviews():
                    host.removeArrangedSubview_(view)
                    view.removeFromSuperview()
                choice = widgets.MenuChoice([("", "None"), ("peer", "Peer")])
                owners.append(weakref.ref(choice))
                widgets.add(host, choice.view, full_width=False)
        gc.collect()
        self.assertEqual(sum(owner() is not None for owner in owners), 1)

    def test_menu_choice_action_still_reaches_its_live_owner(self):
        calls = []
        choice = widgets.MenuChoice([("", "None"), ("peer", "Peer")], calls.append)
        choice.value = "peer"
        choice.target.fire_(choice.popup)
        self.assertEqual(calls, ["peer"])
        target = choice.target
        owner = weakref.ref(choice)
        del choice
        gc.collect()
        self.assertIsNone(owner())
        target.fire_(None)
        self.assertEqual(calls, ["peer"])

    def test_figure_renders_its_unit_once(self):
        figure = widgets.Figure('120', theme.TYPE['numeral'], 'px')
        self.assertEqual(figure.view.attributedStringValue().string(), '120 px')
        self.assertEqual(figure.view.accessibilityLabel(), '120 px')

    def test_authored_us_text_reaches_rendered_text_and_accessibility(self):
        from core import i18n, locale

        with mock.patch.object(locale, "is_us_region", return_value=True):
            label = widgets.Label(i18n.tr("Colourful behaviour"))
            button = widgets.Button(i18n.tr("Minimise"), None, None)

        self.assertEqual(label.view.stringValue(), "Colorful behavior")
        self.assertEqual(label.view.accessibilityLabel(), "Colorful behavior")
        self.assertEqual(label.text, "Colorful behavior")
        self.assertEqual(button.view.title(), "Minimize")
        self.assertEqual(button.title, "Minimize")

    def test_us_region_preserves_direct_and_interpolated_user_data(self):
        from core import i18n, locale

        with mock.patch.object(locale, "is_us_region", return_value=True):
            for value in ("Colourful", "Favourite centre", "colour.example"):
                with self.subTest(value=value):
                    label = widgets.Label(value)
                    self.assertEqual(label.view.stringValue(), value)
                    self.assertEqual(label.view.accessibilityLabel(), value)
                    label.set(i18n.tr("Favourite: {name}").format(name=value))
                    self.assertEqual(label.view.stringValue(), "Favorite: " + value)

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
