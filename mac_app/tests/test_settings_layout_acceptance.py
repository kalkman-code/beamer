"""The corrected 1.5 layout contract, exercised without ordering a window."""
import unittest

from mac_app.tests.design_offscreen import AppKit, kvm_bridge_app, offscreen_control_window, settle

widgets = kvm_bridge_app.widgets
WIDTHS = (320, 426, 512, 640, 720, 900, 1100, 1440, 1920)


def walk(view):
    yield view
    if isinstance(view, (AppKit.NSTextField, AppKit.NSButton)):
        return
    for child in view.subviews():
        if not child.isHidden():
            yield from walk(child)


def frame_in(view, parent):
    rect = view.alignmentRectForFrame_(view.bounds()) if isinstance(view, AppKit.NSTextField) else view.bounds()
    return view.convertRect_toView_(rect, parent)


class SettingsLayoutAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        kvm_bridge_app.theme.init_fonts()
        cls.control = offscreen_control_window()

    def test_all_six_pages_share_the_centred_column_at_every_width_and_density(self):
        from mac_app.tests.settings_layout_offscreen import render
        control = self.control
        for density in (1, 2):
            for width in WIDTHS:
                sidebar = 56 if width < 640 else 144 if width < 800 else 176
                viewport = width - sidebar - 1 - 16
                expected = min(880, viewport - 48)
                for key, scroll in control.pages.items():
                    with self.subTest(page=key, width=width, density=density):
                        control.window.setFrame_display_(((100000, 100000), (width, 900)), False)
                        control._select_page(key)
                        settle(control.window)
                        self.assertFalse(control.window.isVisible())
                        self.assertAlmostEqual(control.window.frame().size.width, width, delta=1)
                        self.assertAlmostEqual(control.sidebar.view.frame().size.width, sidebar, delta=1)
                        page = scroll.documentView()
                        body = page.subviews()[0]
                        self.assertAlmostEqual(body.frame().size.width, expected, delta=1)
                        self.assertAlmostEqual(body.frame().origin.x, (viewport - expected) / 2, delta=1)
                        for section in body.arrangedSubviews():
                            if section.isHidden():
                                continue
                            rect = frame_in(section, body)
                            self.assertAlmostEqual(rect.origin.x, 0, delta=1)
                            self.assertAlmostEqual(rect.size.width, expected, delta=1)
                        self.assertLessEqual(page.frame().size.width, scroll.contentView().bounds().size.width + 1)
                        self.assertFalse(scroll.hasHorizontalScroller())
                        bitmap = render(control.window.contentView(), density)
                        self.assertEqual(bitmap.pixelsWide(), width * density)
                        self.assertEqual(bitmap.pixelsHigh(), round(control.window.contentView().bounds().size.height * density))

    def test_controls_labels_and_children_fit_all_six_pages(self):
        control = self.control
        for width in WIDTHS:
            for key, scroll in control.pages.items():
                control.window.setFrame_display_(((100000, 100000), (width, 900)), False)
                control._select_page(key)
                settle(control.window)
                body = scroll.documentView().subviews()[0]
                with self.subTest(page=key, width=width):
                    for child in walk(body):
                        rect = frame_in(child, body)
                        self.assertGreaterEqual(rect.origin.x, -.99, type(child).__name__)
                        self.assertLessEqual(rect.origin.x + rect.size.width, body.bounds().size.width + 1,
                                             type(child).__name__)
                        selector = getattr(child, 'segmented_owner', None)
                        if selector is not None:
                            self.assertLessEqual(child.bounds().size.width, selector.cap + 1)
                            for _, cell, _ in selector.cells:
                                self.assertLessEqual(cell.bounds().size.width, 120)
                                self.assertGreaterEqual(cell.bounds().size.height, 30)
                        if isinstance(child, AppKit.NSButton) and not isinstance(child, AppKit.NSPopUpButton):
                            self.assertLessEqual(child.bounds().size.width, 241)
                        field = getattr(child, 'field_owner', None)
                        if field is not None and field.caption is not None and body.bounds().size.width >= 560:
                            self.assertAlmostEqual(frame_in(field.words.view, child).size.width, 180, delta=1)
                            self.assertLessEqual(field.words.view.bounds().size.height, 20)

    def test_families_are_horizontal_equal_tiles_and_wrap_by_column_width(self):
        control = self.control
        for width in WIDTHS:
            control.window.setFrame_display_(((100000, 100000), (width, 900)), False)
            control._select_page('design')
            settle(control.window)
            column = control.pages['design'].documentView().subviews()[0].bounds().size.width
            for choices, count, gap in ((control.glow_style_select, 3 if column >= 600 else 2 if column >= 400 else 1, 12),
                                        (control.glow_colour_select, 5 if column >= 600 else 3 if column >= 400 else 2, 8)):
                for family in choices.rows:
                    cells = [tile for _, tile, *_ in getattr(family, 'tiles', getattr(family, 'cells', []))]
                    frames = [frame_in(cell, family.view) for cell in cells]
                    with self.subTest(width=width, family=family.values):
                        self.assertEqual(family.columns, min(count, len(cells)))
                        for rect in frames:
                            self.assertAlmostEqual(rect.size.width, frames[0].size.width, delta=1)
                            self.assertAlmostEqual(rect.size.height, frames[0].size.height, delta=1)
                            if choices is control.glow_style_select:
                                self.assertAlmostEqual(rect.size.width / rect.size.height, 152 / 116, delta=.015)
                        for index in range(1, min(count, len(frames))):
                            self.assertAlmostEqual(frames[index].origin.y, frames[0].origin.y, delta=1)
                            self.assertAlmostEqual(frames[index].origin.x - frames[index-1].origin.x,
                                                   frames[0].size.width + gap, delta=1)
            self.assertEqual(len(control.glow_colour_select.rows[1].cells), 5)

    def test_window_minimum_and_resize_constraints_cannot_drive_native_frame(self):
        control = self.control
        self.assertEqual(tuple(control.window.minSize()), (320, 300))
        for key in control.pages:
            control._select_page(key)
            self.assertLess(control.page_width_constraints[key].priority(), 500)
            with self.subTest(page=key):
                control.window.setFrame_display_(((100000, 100000), (320, 300)), False)
                settle(control.window)
                self.assertEqual(tuple(control.window.frame().size), (320, 300))

    def test_section_padding_and_ordinary_row_spacing_are_shared(self):
        control = self.control
        for key, scroll in control.pages.items():
            control._select_page(key)
            control.window.setFrame_display_(((100000, 100000), (1440, 900)), False)
            settle(control.window)
            for section in scroll.documentView().subviews()[0].arrangedSubviews():
                children = section.subviews()
                if len(children) != 1 or not isinstance(children[0], AppKit.NSStackView):
                    continue
                body = children[0]
                with self.subTest(page=key):
                    self.assertEqual(body.spacing(), 12)
                    self.assertAlmostEqual(body.frame().origin.x, 16, delta=1)
                    self.assertAlmostEqual(body.frame().origin.y, 16, delta=1)
                    self.assertAlmostEqual(section.bounds().size.width - body.frame().size.width, 32, delta=1)
        self.assertEqual(control.style_module.body.customSpacingAfterView_(control.chosen_box), 12)

    def test_machine_section_heading_does_not_split_at_the_minimum_width(self):
        control = self.control
        control._select_page('overview')
        control.window.setFrame_display_(((100000, 100000), (320, 900)), False)
        settle(control.window)
        heading = control.panel.pair_button.view.superview().arrangedSubviews()[0]
        self.assertGreaterEqual(heading.bounds().size.width, heading.attributedStringValue().size().width)

    def test_shortcut_diagram_legend_layers_fit_the_narrow_column(self):
        control = self.control
        control._select_page('crossing')
        control.window.setFrame_display_(((100000, 100000), (320, 900)), False)
        settle(control.window)
        diagram = control.arrangement_diagram
        for layer in (diagram.key, diagram.key_how):
            rect = layer.frame()
            self.assertLessEqual(rect.origin.x + rect.size.width, diagram.bounds().size.width)
            self.assertLessEqual(rect.origin.y + rect.size.height, diagram.bounds().size.height)

    def test_key_recorders_keep_their_titles_readable_in_the_narrow_column(self):
        control = self.control
        control.window.setFrame_display_(((100000, 100000), (320, 900)), False)
        for key, recorder, label in (('crossing', control.key_recorder, control.key_recorder.key),
                                    ('keyboard', control.ignored_recorder, control.ignored_recorder.title)):
            control._select_page(key)
            settle(control.window)
            string = label.view.attributedStringValue()
            available = label.view.cell().drawingRectForBounds_(label.view.bounds()).size.width
            if string.size().width > available:
                self.assertTrue(label.wrap)
            full = string.boundingRectWithSize_options_((available, 10000), AppKit.NSStringDrawingUsesLineFragmentOrigin)
            self.assertGreaterEqual(label.view.bounds().size.height + 1, full.size.height)

    def test_sidebar_selection_and_focus_never_diverge(self):
        control = self.control
        sidebar = control.sidebar
        control.window.keyboard_navigation = False
        control.window.makeFirstResponder_(sidebar.rows['keyboard'][1])
        sidebar._choose('design')
        for _, row, *_ in sidebar.rows.values():
            self.assertTrue(row.ring is None or row.ring.isHidden())
        for action in (lambda: sidebar.step(1), lambda: sidebar.step(-1),
                       lambda: control.windowDidBecomeKey_(None)):
            action()
            selected = [key for key, (_, row, *_) in sidebar.rows.items() if row.accessibilityValue() == 1]
            self.assertEqual(selected, [control.page])
            rings = [key for key, (_, row, *_) in sidebar.rows.items() if row.ring and not row.ring.isHidden()]
            self.assertEqual(rings, [control.page])

    def test_normal_frames_are_clamped_and_tiled_frames_are_not_persisted(self):
        from unittest import mock
        work = AppKit.NSMakeRect(20, 40, 640, 480)
        self.assertEqual(kvm_bridge_app.settings_normal_frame(None, work), (20, 40, 640, 480))
        self.assertEqual(kvm_bridge_app.settings_normal_frame([1000, -200, 500, 400], work), (160, 40, 500, 400))
        self.assertEqual(kvm_bridge_app.settings_normal_frame(['bad'], work), (20, 40, 640, 480))
        window = mock.Mock()
        window.isVisible.return_value = True
        window.isZoomed.return_value = False
        window.styleMask.return_value = 0
        window.frame.return_value = AppKit.NSMakeRect(30, 50, 500, 400)
        window.stringWithSavedFrame.return_value = '30 50 500 400 0 0 640 480'
        self.assertEqual(kvm_bridge_app.ordinary_window_frame(window), [30, 50, 500, 400])
        window.stringWithSavedFrame.return_value += ' {"tilingState":{}}'
        self.assertIsNone(kvm_bridge_app.ordinary_window_frame(window))
        window.stringWithSavedFrame.return_value = ''
        window.styleMask.return_value = AppKit.NSWindowStyleMaskFullScreen
        self.assertIsNone(kvm_bridge_app.ordinary_window_frame(window))
        control = self.control
        held = list(control._normal_frame)
        control._frame_transition = True
        with mock.patch.object(kvm_bridge_app, 'ordinary_window_frame') as ordinary:
            control._remember_normal_frame()
            ordinary.assert_not_called()
        self.assertEqual(control._normal_frame, held)
        control._frame_transition = False

    def test_tab_enters_the_selected_page_and_pointer_input_clears_all_focus(self):
        control = self.control
        control._select_page('design')
        control.window.makeFirstResponder_(control.window)
        control.window.selectNextKeyView_(None)
        self.assertEqual(control.window.firstResponder(), control.sidebar.rows['design'][1])
        cell = control.length_select.cells[0][1]
        control.window.makeFirstResponder_(cell)
        self.assertFalse(cell.ring.isHidden())
        control.window.keyboard_navigation = False
        control.window.refresh_input_focus()
        self.assertTrue(cell.ring.isHidden())

    def test_length_size_stay_vertical_and_toggle_targets_remain_usable(self):
        control = self.control
        for width in WIDTHS:
            control.window.setFrame_display_(((100000, 100000), (width, 900)), False)
            control._select_page('design')
            settle(control.window)
            module = control.style_module.view
            length = frame_in(control.length_select.view, module)
            size = frame_in(control.size_select.view, module)
            self.assertGreater(abs(length.origin.y - size.origin.y), 30)
            for switch in (control.glow_box, control.landing_box, control.haptics_box):
                self.assertGreaterEqual(switch.view.bounds().size.height, 30)

    def test_rail_targets_and_selected_marks_are_explicit(self):
        control = self.control
        control.window.setFrame_display_(((100000, 100000), (320, 900)), False)
        control._select_page('design')
        control.sidebar.set_dots({'overview': 'fault', 'permissions': 'amber'})
        settle(control.window)
        self.assertTrue(control.sidebar.footer_icon.image().isTemplate())
        for _, row, icon, words, _, dot in control.sidebar.rows.values():
            self.assertEqual(tuple(row.bounds().size), (44, 36))
            self.assertTrue(words.view.isHidden())
            self.assertTrue(row.toolTip())
            self.assertEqual(tuple(icon.alignmentRectForFrame_(icon.bounds()).size), (18, 18))
            if not dot.isHidden():
                icon_rect, dot_rect = frame_in(icon, row), frame_in(dot, row)
                self.assertGreaterEqual(dot_rect.origin.x, icon_rect.origin.x + 18 + 2)
        for choices, value in ((control.glow_style_select, 'flint'), (control.glow_colour_select, 'mono')):
            choices.value = value
            family = next(row for row in choices.rows if value in row.values)
            cells = getattr(family, 'tiles', getattr(family, 'cells', []))
            for candidate, cell, *_ in cells:
                self.assertEqual(cell.selection_mark.isHidden(), candidate != value)
            if value == 'mono':
                self.assertEqual(family.ring.inner.borderWidth(), 1)

    def test_native_machine_menu_complete_bounds_are_capped(self):
        control = self.control
        for width in WIDTHS:
            control.window.setFrame_display_(((100000, 100000), (width, 900)), False)
            control._select_page('design')
            settle(control.window)
            self.assertLessEqual(control.follow_select.view.bounds().size.width, 320)

    def test_pairing_notch_and_switch_states_fit_at_every_width(self):
        control = self.control
        control.panel.open = True
        control.panel.refresh()
        control.glow_box.value = True
        control.landing_box.value = True
        control.has_notch = True
        control._place_chosen = True
        control.place = 'notch'
        control.glow_style_select.value = 'glow'
        for mode in ('crossing', 'switch'):
            control.style_for_select.value = mode
            control._reflect()
            for width in WIDTHS:
                for key in ('overview', 'design'):
                    control.window.setFrame_display_(((100000, 100000), (width, 900)), False)
                    control._select_page(key)
                    settle(control.window)
                    body = control.pages[key].documentView().subviews()[0]
                    with self.subTest(page=key, mode=mode, width=width):
                        for child in walk(body):
                            rect = frame_in(child, body)
                            self.assertGreaterEqual(rect.origin.x, -1, type(child).__name__)
                            self.assertLessEqual(rect.origin.x + rect.size.width, body.bounds().size.width + 1,
                                                 type(child).__name__)

    def test_breakpoints_reflow_in_both_directions_without_waiting_for_another_resize(self):
        control = self.control
        for dark in (False, True):
            kvm_bridge_app.theme.set_dark(dark)
            widths = (319+1, 519, 520, 521, 638, 639, 640, 641, 767, 768, 769, 770, 798, 799, 800, 801, 839, 840, 841, 842, 1440)
            for width in widths + widths[::-1]:
                control.window.setFrame_display_(((100000, 100000), (width, 900)), False)
                control._select_page('design')
                settle(control.window)
                column = control.pages['design'].documentView().subviews()[0].bounds().size.width
                with self.subTest(dark=dark, width=width):
                    for family in control.glow_style_select.rows:
                        self.assertEqual(family.columns, 3 if column >= 600 else 2 if column >= 400 else 1)
                    self.assertEqual(control.length_row.field_owner.stacked, column < 560)
                    self.assertEqual(control.sidebar.rows['design'][3].view.isHidden(), width < 640)

    def test_long_buttons_grow_in_height_and_machine_names_remain_available(self):
        button = widgets.Button('A longer action whose full label needs several lines in this column', None, None)
        host = widgets.box()
        host.setFrame_(((0, 0), (180, 200)))
        host.addSubview_(button.view)
        AppKit.NSLayoutConstraint.activateConstraints_([
            button.view.leadingAnchor().constraintEqualToAnchor_(host.leadingAnchor()),
            button.view.topAnchor().constraintEqualToAnchor_(host.topAnchor()),
            button.view.widthAnchor().constraintEqualToConstant_(180),
        ])
        host.layoutSubtreeIfNeeded()
        ink = button.view.attributedTitle().boundingRectWithSize_options_(
            (156, 10000), AppKit.NSStringDrawingUsesLineFragmentOrigin)
        self.assertGreater(ink.size.height, 30)
        self.assertGreaterEqual(button.view.bounds().size.height, ink.size.height + 12)
        name = 'A very long machine name kept in full in the native menu'
        menu = widgets.MenuChoice([(None, 'None'), ('synthetic', name)])
        menu.value = 'synthetic'
        self.assertEqual(menu.items[1][1].title(), name)
        self.assertEqual(menu.popup.accessibilityValue(), name)


if __name__ == '__main__':
    unittest.main()
