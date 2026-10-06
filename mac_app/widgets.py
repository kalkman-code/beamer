"""Vernier's components for the macOS control window, in AppKit.

Page flow uses Auto Layout; preview grids place their cells locally so resizing does not solve
the entire preview tree. Labels keep their measured height. The custom controls draw with the
palette from theme.py; anything clickable takes keyboard focus, answers Space and shows a signal
focus ring.
"""

from __future__ import annotations

import math
import weakref

import AppKit
import CoreText
import Quartz
import objc

import media_keys
import motion
import theme
from core.locale import americanise
from ignored_titles import is_modifier_release, recorded_entry
from key_codes import KEY_NAME_TO_CODE, PRINTABLE_KEY_FALLBACKS, SPECIAL_KEY_NAMES, key_cap, key_title
from core import ways as core_ways


def _autolayout(view):
    view.setTranslatesAutoresizingMaskIntoConstraints_(False)
    return view


def stack(vertical: bool = True, spacing: float = 10.0):
    view = _autolayout(AppKit.NSStackView.alloc().init())
    view.setOrientation_(
        AppKit.NSUserInterfaceLayoutOrientationVertical if vertical else AppKit.NSUserInterfaceLayoutOrientationHorizontal
    )
    view.setSpacing_(spacing)
    view.setAlignment_(AppKit.NSLayoutAttributeLeading if vertical else AppKit.NSLayoutAttributeCenterY)
    view.setDistribution_(AppKit.NSStackViewDistributionFill)
    return view


class SideBySide:
    """Two blocks in one stack, beside each other when their minimum widths and gap fit."""

    def __init__(self, first, second, first_minimum, second_minimum, gap=None):
        self.first = first
        self.second = second
        self.first_minimum = first_minimum
        self.second_minimum = second_minimum
        self.gap = theme.tokens.SIDE_BY_SIDE_GAP if gap is None else gap
        self.minimum = first_minimum + second_minimum
        self.view = stack(spacing=self.gap)
        self.view.setAlignment_(AppKit.NSLayoutAttributeWidth)
        self.view.addArrangedSubview_(first)
        self.view.addArrangedSubview_(second)
        for child, minimum in ((first, first_minimum), (second, second_minimum)):
            constraint = child.widthAnchor().constraintGreaterThanOrEqualToConstant_(minimum)
            constraint.setPriority_(AppKit.NSLayoutPriorityDragThatCannotResizeWindow)
            constraint.setActive_(True)
        self.stacked_widths = [child.widthAnchor().constraintEqualToAnchor_(self.view.widthAnchor())
                               for child in (first, second)]
        for constraint in self.stacked_widths:
            constraint.setPriority_(1000)
        AppKit.NSLayoutConstraint.activateConstraints_(self.stacked_widths)
        self.view.setIdentifier_("side-by-side")
        self._beside = False

    def needed(self):
        return self.minimum + self.gap

    def orientation(self):
        return self.view.orientation()

    def bounds(self):
        return self.view.bounds()

    def isHidden(self):
        return self.view.isHidden()

    def update_orientation(self, width=None):
        width = self.view.bounds().size.width if width is None else width
        # A live resize can alternate rounded widths around the breakpoint. Keep the current
        # layout within a 16pt band rather than rebuilding the stack on every pointer step.
        threshold = self.needed() + (-16 if self._beside else 16)
        beside = width >= threshold
        if beside == self._beside:
            return
        self._beside = beside
        for constraint in self.stacked_widths:
            constraint.setPriority_(1 if beside else 1000)
        self.view.setOrientation_(
            AppKit.NSUserInterfaceLayoutOrientationHorizontal if beside
            else AppKit.NSUserInterfaceLayoutOrientationVertical
        )
        self.view.setAlignment_(
            AppKit.NSLayoutAttributeTop if beside else AppKit.NSLayoutAttributeWidth
        )


def _width_cap(view):
    for c in view.constraints():
        if (
            c.firstItem() == view
            and c.secondItem() is None
            and c.firstAttribute() == AppKit.NSLayoutAttributeWidth
            and c.relation() == AppKit.NSLayoutRelationLessThanOrEqual
        ):
            return c.constant()
    return None


def add(parent, view, full_width: bool = True):
    """Adds an arranged subview, stretched to the stack's width. NSStackView aligns its children
    but does not stretch them, and a control that stops short of the module edge is the giveaway.
    A child with its own maximum width (a note at the reading width) is as wide as the stack up to
    that maximum. Tied to the stack outright, it fixed its whole column at that width, and through
    the column the window: on 1.5.0 the window could not be made wider than 773 to 837 pt."""
    parent.addArrangedSubview_(view)
    if not full_width:
        return view
    cap = _width_cap(view)
    if cap is None:
        fill = view.widthAnchor().constraintEqualToAnchor_(parent.widthAnchor())
        # Hidden arranged children retain their external constraints. A required fill links their
        # intrinsic minima back into NSWindow's user-resize limit even while detached by the stack.
        fill.setPriority_(AppKit.NSLayoutPriorityDragThatCannotResizeWindow)
        fill.setActive_(True)
        return view
    view.widthAnchor().constraintLessThanOrEqualToAnchor_(parent.widthAnchor()).setActive_(True)
    # Towards the cap rather than the stack, so the pull never grows with the column. Above the
    # label's own hugging (251), so it fills to the cap; below NSStackView's equal sizing (260),
    # so it cannot unbalance Pairing's two halves.
    reach = view.widthAnchor().constraintEqualToConstant_(cap)
    reach.setPriority_(255)
    reach.setActive_(True)
    return view


def pin(view, container, insets=(0, 0, 0, 0)):
    top, leading, bottom, trailing = insets
    AppKit.NSLayoutConstraint.activateConstraints_([
        view.topAnchor().constraintEqualToAnchor_constant_(container.topAnchor(), top),
        view.leadingAnchor().constraintEqualToAnchor_constant_(container.leadingAnchor(), leading),
        container.bottomAnchor().constraintEqualToAnchor_constant_(view.bottomAnchor(), bottom),
        container.trailingAnchor().constraintEqualToAnchor_constant_(view.trailingAnchor(), trailing),
    ])
    return view


def size(view, width=None, height=None):
    if width is not None:
        view.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
    if height is not None:
        view.heightAnchor().constraintEqualToConstant_(height).setActive_(True)
    return view


def hug(view, priority=AppKit.NSLayoutPriorityRequired, horizontal=True):
    orientation = AppKit.NSLayoutConstraintOrientationHorizontal if horizontal else AppKit.NSLayoutConstraintOrientationVertical
    view.setContentHuggingPriority_forOrientation_(priority, orientation)
    if isinstance(view, AppKit.NSStackView):
        view.setHuggingPriority_forOrientation_(priority, orientation)
    return view


def squeeze(view, priority=AppKit.NSLayoutPriorityDefaultLow):
    view.setContentCompressionResistancePriority_forOrientation_(priority, AppKit.NSLayoutConstraintOrientationHorizontal)
    return view


def box(fill=None, stroke=None, radius=0.0, cls=None):
    view = _autolayout((cls or AppKit.NSView).alloc().init())
    view.setWantsLayer_(True)
    paint(view, fill, stroke, radius)
    return view


def paint(view, fill=None, stroke=None, radius=None):
    layer = view.layer()
    theme.tint(layer, background=fill, border=stroke)
    layer.setBorderWidth_(1 if stroke else 0)
    if radius is not None:
        layer.setCornerRadius_(radius)


def attributed(text, size, weight=400, ink="ink", mono=False, tracking=0.0, upper=False, align=None, wrap=False):
    paragraph = AppKit.NSMutableParagraphStyle.alloc().init()
    paragraph.setAlignment_(AppKit.NSTextAlignmentNatural if align is None else align)
    paragraph.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping if wrap else AppKit.NSLineBreakByTruncatingTail)
    return AppKit.NSAttributedString.alloc().initWithString_attributes_(
        text.upper() if upper else text,
        {
            AppKit.NSFontAttributeName: theme.mono_font(size, weight) if mono else theme.font(size, weight),
            AppKit.NSForegroundColorAttributeName: theme.colour(ink),
            AppKit.NSKernAttributeName: size * tracking,
            AppKit.NSParagraphStyleAttributeName: paragraph,
        },
    )


def _update_full_text_metadata(view, text, previous):
    if view.toolTip() in (None, previous):
        view.setToolTip_(text)
    if view.accessibilityLabel() in (None, "", previous):
        view.setAccessibilityLabel_(text)


class _PreviewCaption(AppKit.NSView):
    text = None

    @objc.python_method
    def setAttributedStringValue_(self, text):
        self.text = text
        self.setAccessibilityRole_(AppKit.NSAccessibilityStaticTextRole)
        self.setAccessibilityLabel_(text.string())
        self.setNeedsDisplay_(True)

    @objc.python_method
    def attributedStringValue(self):
        return self.text

    def isAccessibilityElement(self):
        return True

    def drawRect_(self, _rect):
        self.text.drawInRect_(self.bounds())


class Label:
    """A label that keeps its style, so the text, colour or size can change without restating it.
    NSFont has no letter spacing, so tracking is NSKern in the attributed string."""

    def __init__(self, text="", size=None, weight=400, ink="ink", wrap=False, mono=False, tracking=0.0,
                 upper=False, align=None, preview=False):
        self.text = text
        self.size = size or theme.TYPE["body"]
        self.weight = weight
        self.ink = ink
        self.wrap = wrap
        self.mono = mono
        self.tracking = tracking
        self.upper = upper
        self.align = align
        self._metadata_text = ""
        maker = AppKit.NSTextField.wrappingLabelWithString_ if wrap else AppKit.NSTextField.labelWithString_
        self.view = _autolayout(_PreviewCaption.alloc().init() if preview else maker(""))
        if not preview:
            # wrappingLabelWithString makes a selectable field. A click on one, inside a segment,
            # tile or switch, opened the field editor: I-beam, selected text, and the label redrawn
            # in the editor's own size, alignment and ink.
            self.view.setSelectable_(False)
        if wrap:
            squeeze(self.view)
        self._render()

    def set(self, text=None, ink=None, size=None, weight=None, mono=None):
        changed = False
        for name, value in (("text", text), ("ink", ink), ("size", size), ("weight", weight), ("mono", mono)):
            if value is not None and value != getattr(self, name):
                setattr(self, name, value)
                changed = True
        if changed:
            self._render()

    def _render(self):
        text = americanise(self.text)
        self.view.setAttributedStringValue_(attributed(
            text, self.size, self.weight, self.ink, self.mono, self.tracking,
            self.upper, self.align, self.wrap
        ))
        if not self.wrap:
            _update_full_text_metadata(self.view, text, self._metadata_text)
            self._metadata_text = text


def label(text, size=None, weight=400, ink="ink", wrap=False, mono=False):
    return Label(text, size, weight, ink, wrap, mono).view


def eyebrow(text, ink="ink_3"):
    return Label(text, theme.TYPE["eyebrow"], 700, ink, wrap=True, tracking=theme.TRACKING["eyebrow"], upper=True).view


def note(text="", ink="ink_2", align=None, reading=True):
    """`reading=False` for a note beside a control in a row: capped there, nothing could take the
    row's spare width, and the row, its module and the page column stopped at note plus control."""
    label = Label(text, theme.TYPE["note"], ink=ink, wrap=True, align=align)
    if reading:
        label.view.widthAnchor().constraintLessThanOrEqualToConstant_(theme.READING_WIDTH).setActive_(True)
    return label


def hairline():
    rule = box("rule")
    rule.heightAnchor().constraintEqualToConstant_(1).setActive_(True)
    return rule


class Module:
    """One panel in a rack: padded body stack on `panel`. The module is only as tall as its content
    wants, but a rack may stretch it to match the tallest neighbour in its row."""

    def __init__(self, spacing=12.0):
        self.view = box("panel")
        self.body = stack(spacing=spacing)
        self.view.addSubview_(self.body)
        vertical, horizontal = 16, 16
        AppKit.NSLayoutConstraint.activateConstraints_([
            self.body.topAnchor().constraintEqualToAnchor_constant_(self.view.topAnchor(), vertical),
            self.body.leadingAnchor().constraintEqualToAnchor_constant_(self.view.leadingAnchor(), horizontal),
            self.view.trailingAnchor().constraintEqualToAnchor_constant_(self.body.trailingAnchor(), horizontal),
            self.view.bottomAnchor().constraintGreaterThanOrEqualToAnchor_constant_(self.body.bottomAnchor(), horizontal),
        ])
        collapse = self.view.heightAnchor().constraintEqualToConstant_(0)
        collapse.setPriority_(AppKit.NSLayoutPriorityFittingSizeCompression)
        collapse.setActive_(True)

    def add(self, view, full_width=True):
        if getattr(view, 'segmented_owner', None) is not None:
            view = field_row(None, view)[0]
        return add(self.body, view, full_width)


class Rack:
    """Modules on the `rule` colour with RACK_GAP between them, so the gaps read as hairlines, or
    on nothing with a wider `gap`. `arrange` takes rows of (module index, column span) against a
    column count, and swaps the whole constraint set, so the window can reflow between its wide
    and narrow grids."""

    def __init__(self, modules, bottom_rule=True, gap=None, fill="rule"):
        self.view = box(fill)
        self.gap = theme.RACK_GAP if gap is None else gap
        self.bottom_rule = bottom_rule
        self.modules = modules
        for module in modules:
            self.view.addSubview_(module.view)
        self.constraints = []
        self.layout = None

    def arrange(self, rows, columns):
        key = (tuple(tuple(row) for row in rows), columns)
        if key == self.layout:
            return
        self.layout = key
        AppKit.NSLayoutConstraint.deactivateConstraints_(self.constraints)
        gap = self.gap
        constraints = []
        above = None
        for row in rows:
            first = previous = None
            for position, (index, span) in enumerate(row):
                view = self.modules[index].view
                top = above.bottomAnchor() if above is not None else self.view.topAnchor()
                constraints.append(view.topAnchor().constraintEqualToAnchor_constant_(top, gap if above is not None else 0))
                leading = previous.trailingAnchor() if previous is not None else self.view.leadingAnchor()
                constraints.append(view.leadingAnchor().constraintEqualToAnchor_constant_(leading, gap if previous is not None else 0))
                if position == len(row) - 1:
                    constraints.append(self.view.trailingAnchor().constraintEqualToAnchor_(view.trailingAnchor()))
                else:
                    constant = gap * ((span - 1) - span * (columns - 1) / columns)
                    constraints.append(view.widthAnchor().constraintEqualToAnchor_multiplier_constant_(
                        self.view.widthAnchor(), span / columns, constant
                    ))
                if first is None:
                    first = view
                else:
                    constraints.append(view.heightAnchor().constraintEqualToAnchor_(first.heightAnchor()))
                previous = view
            above = first
        constraints.append(self.view.bottomAnchor().constraintEqualToAnchor_constant_(
            above.bottomAnchor(), gap if self.bottom_rule else 0
        ))
        AppKit.NSLayoutConstraint.activateConstraints_(constraints)
        self.constraints = constraints


def grid(views, columns):
    """Views in equal columns on the `rule` colour, 1pt apart inside a 1pt border, each row as tall
    as its tallest view."""
    frame = box("rule")
    rows = _autolayout(_ResponsiveView.alloc().init())
    for view in views:
        rows.addSubview_(view)
    rows.owner = FormGrid(rows, views, columns)
    rows.owner.arrange(600)
    frame.addSubview_(rows)
    pin(rows, frame, (1, 1, 1, 1))
    return frame


class FormGrid:
    def __init__(self, view, cells, columns):
        self.view, self.cells, self.columns = view, list(cells), columns
        self.constraints, self.count = [], None

    def arrange(self, width):
        count = self.columns if width >= 350 else 1
        if self.count == count:
            return
        self.count = count
        AppKit.NSLayoutConstraint.deactivateConstraints_(self.constraints)
        constraints, previous = [], None
        for start in range(0, len(self.cells), count):
            row = self.cells[start:start + count]
            for index, cell in enumerate(row):
                constraints.extend([
                    cell.topAnchor().constraintEqualToAnchor_constant_(previous.bottomAnchor() if previous else self.view.topAnchor(), 1 if previous else 0),
                    cell.leadingAnchor().constraintEqualToAnchor_constant_(self.view.leadingAnchor() if index == 0 else row[index-1].trailingAnchor(), 0 if index == 0 else 1),
                    cell.heightAnchor().constraintEqualToAnchor_(row[0].heightAnchor()),
                ])
                # The last cell of a short row runs to the edge: an empty slot would show the
                # grid's `rule` ground as an odd grey cell.
                if index == len(row) - 1 and len(row) < count:
                    constraints.append(cell.trailingAnchor().constraintEqualToAnchor_(self.view.trailingAnchor()))
                else:
                    constraints.append(cell.widthAnchor().constraintEqualToAnchor_multiplier_constant_(self.view.widthAnchor(), 1/count, -(count-1)/count))
            previous = row[0]
        constraints.append(self.view.bottomAnchor().constraintEqualToAnchor_(previous.bottomAnchor()))
        self.constraints = constraints
        AppKit.NSLayoutConstraint.activateConstraints_(constraints)


def flipped(cls):
    return _autolayout(cls.alloc().init())


class Figure:
    """A mono figure and its unit as one attributed string. Two labels in a baseline-aligned stack
    let the smaller unit drift below the figure once the row is squeezed; one string cannot."""

    def __init__(self, text, size, unit="", unit_size=None, tracking=0.0, ink="ink"):
        self.text, self.size, self.unit, self.ink, self.tracking = text, size, unit, ink, tracking
        self.unit_size = unit_size or theme.TYPE["small"]
        self._metadata_text = ""
        self.view = squeeze(_autolayout(AppKit.NSTextField.labelWithString_("")))
        self._render()

    def set(self, text=None, ink=None, size=None):
        changed = False
        for name, value in (("text", text), ("ink", ink), ("size", size)):
            if value is not None and value != getattr(self, name):
                setattr(self, name, value)
                changed = True
        if changed:
            self._render()

    def _render(self):
        text = americanise(self.text + (" " + self.unit if self.unit else ""))
        string = AppKit.NSMutableAttributedString.alloc().initWithAttributedString_(
            attributed(americanise(self.text), self.size, ink=self.ink, mono=True, tracking=self.tracking)
        )
        if self.unit:
            string.appendAttributedString_(attributed(" " + self.unit, self.unit_size, ink="ink_3", mono=True))
        self.view.setAttributedStringValue_(string)
        _update_full_text_metadata(self.view, text, self._metadata_text)
        self._metadata_text = text


class Readout:
    """A figure in a `ground` well: the tracked key, the value in mono with its unit, then a footer
    such as a spark strip or a scale, kept at the bottom whatever the well's height. `sizes` is the
    value's size at the wide and the narrow grid."""

    def __init__(self, key, unit, footer, sizes=None):
        self.sizes = sizes or (theme.TYPE["readout"], theme.TYPE["readout_narrow"])
        self.view = box("ground")
        column = stack(spacing=6)
        column.setDistribution_(AppKit.NSStackViewDistributionEqualSpacing)
        add(column, Label(key, theme.TYPE["readout_key"], 700, "ink_3", tracking=theme.TRACKING["readout_key"], upper=True).view)
        self.value = Figure("—", self.sizes[0], unit, tracking=theme.TIGHT["readout"])
        add(column, self.value.view)
        add(column, footer)
        self.view.addSubview_(column)
        pin(column, self.view, (9, 10, 8, 10))

    def set_narrow(self, narrow):
        self.value.set(size=self.sizes[1 if narrow else 0])


class LED:
    def __init__(self, diameter=8.0):
        self.view = size(box(), diameter, diameter)
        self.view.layer().setMasksToBounds_(False)
        # The lamp is a sublayer, not the view's own layer: AppKit clears a backing layer's shadow
        # when the window's appearance changes, which would put the glow out on a palette switch.
        self.lamp = Quartz.CALayer.layer()
        self.lamp.setFrame_(((0, 0), (diameter, diameter)))
        self.lamp.setCornerRadius_(diameter / 2)
        self.view.layer().addSublayer_(self.lamp)
        self.tone = None
        self.blink = False
        self.set("off")

    def set(self, tone, blink=False):
        if (tone, blink) == (self.tone, self.blink):
            return
        self.tone, self.blink = tone, blink
        layer = self.lamp
        theme.tint(layer, background=tone, shadow="signal")
        glow = tone == "signal"
        layer.setShadowOffset_((0, 0))
        layer.setShadowRadius_(4 if glow else 0)
        layer.setShadowOpacity_(0.55 if glow else 0)
        layer.removeAnimationForKey_("blink")
        if blink:
            animation = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
            animation.setFromValue_(1.0)
            animation.setToValue_(0.25)
            animation.setDuration_(0.55)
            animation.setAutoreverses_(True)
            animation.setRepeatCount_(float("inf"))
            layer.addAnimation_forKey_(animation, "blink")


_NAVIGATION_KEYS = {"\t", "\x19", 
    AppKit.NSLeftArrowFunctionKey, AppKit.NSRightArrowFunctionKey,
    AppKit.NSUpArrowFunctionKey, AppKit.NSDownArrowFunctionKey}


def is_navigation_key(event):
    """Tab, Shift-Tab and the arrows turn focus rings on (spec section 6). Any other key, a
    shortcut included, leaves them as the last click left them: off."""
    keys = event.charactersIgnoringModifiers()
    return (bool(keys) and keys[0] in _NAVIGATION_KEYS
            and not event.modifierFlags() & AppKit.NSEventModifierFlagCommand)


def keyboard_focus_visible(view):
    window = view.window()
    return (window is not None and window.firstResponder() == view
            and bool(getattr(window, 'keyboard_navigation', False)))


class Pressable(AppKit.NSView):
    """A view that acts on a click or Space, the base for every custom control here. A focus ring in
    `signal` is drawn as its own sublayer, outside the view unless `ring_inset` says inside."""

    # Missing Python attributes on an NSView fall through to expensive Objective-C lookup.
    ring = None
    ring_owner = None
    selection_mark = None
    tile_content = None
    ring_inset = -3.0
    pointer_cursor = False

    @objc.python_method
    def set_tile_state(self, selected=None, hovered=None):
        if selected is not None:
            self.tile_selected = selected
        if hovered is not None:
            self.tile_hovered = hovered
        if not hasattr(self, "tile_selected"):
            return
        layer = self.layer()
        hover_background = "well" if theme.is_dark() else "panel"
        theme.tint(layer, background=hover_background if self.tile_hovered else "ground",
                   border="signal" if self.tile_selected else ("ink", .18) if self.tile_hovered else "rule")
        layer.setBorderWidth_(2 if self.tile_selected else 1)

    @objc.python_method
    def refresh_colours(self):
        self.set_tile_state()

    @objc.python_method
    def add_selection_mark(self):
        mark = Quartz.CAShapeLayer.layer()
        path = Quartz.CGPathCreateMutable()
        Quartz.CGPathMoveToPoint(path, None, 1, 6)
        Quartz.CGPathAddLineToPoint(path, None, 4, 3)
        Quartz.CGPathAddLineToPoint(path, None, 10, 10)
        mark.setPath_(path)
        mark.setFillColor_(None)
        mark.setLineWidth_(2)
        mark.setHidden_(True)
        self.layer().addSublayer_(mark)
        self.selection_mark = mark

    @objc.python_method
    def show_selection_mark(self, selected):
        self.selection_mark.setHidden_(not selected)
        theme.tint(self.selection_mark, stroke='signal')
        self.setNeedsLayout_(True)

    def acceptsFirstResponder(self):
        return getattr(self, "enabled", True) and getattr(self, "callback", None) is not None

    def acceptsFirstMouse_(self, _event):
        return True

    def mouseDown_(self, _event):
        if self.window() is not None:
            self.window().keyboard_navigation = False
            self._ring(False)

    def mouseUp_(self, event):
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        if AppKit.NSPointInRect(point, self.bounds()):
            self.press()

    def keyDown_(self, event):
        if self.window() is not None and is_navigation_key(event):
            self.window().keyboard_navigation = True
        if event.charactersIgnoringModifiers() in (" ", "\r"):
            self.press()
        else:
            objc.super(Pressable, self).keyDown_(event)

    def accessibilityPerformPress(self):
        self.press()
        return True

    def isAccessibilityElement(self):
        return True

    def isAccessibilityEnabled(self):
        return bool(getattr(self, "enabled", True))

    @objc.python_method
    def press(self):
        if getattr(self, "enabled", True) and getattr(self, "callback", None) is not None:
            self.callback()

    def becomeFirstResponder(self):
        self._ring(True)
        return True

    def resignFirstResponder(self):
        self._ring(False)
        return True

    def layout(self):
        objc.super(Pressable, self).layout()
        content = getattr(self, 'tile_content', None)
        if content is not None:
            content.arrange(self.bounds().size)
        ring = getattr(self, "ring", None)
        if ring is not None:
            inset = getattr(self, "ring_inset", -3.0)
            ring.setFrame_(AppKit.NSInsetRect(self.bounds(), inset, inset))
        mark = getattr(self, 'selection_mark', None)
        if mark is not None and not mark.isHidden():
            mark.setFrame_(((self.bounds().size.width - 18, self.bounds().size.height - 18), (12, 12)))
        owner = getattr(self, "ring_owner", None)
        if owner is not None:
            owner.follow(self)

    def resetCursorRects(self):
        # A pointing hand is for the one Pressable that opens something outside the app (the
        # sidebar's link back); every other one is a control, which macOS leaves as the arrow.
        if getattr(self, "pointer_cursor", False):
            self.addCursorRect_cursor_(self.bounds(), AppKit.NSCursor.pointingHandCursor())

    @objc.python_method
    def _ring(self, on):
        window = self.window()
        on = on and (window is None or getattr(window, 'keyboard_navigation', False))
        sidebar = getattr(self, 'sidebar_owner', None)
        if sidebar is not None:
            on = on and sidebar.rows[sidebar.selected][1] == self
        if getattr(self, "ring", None) is None:
            self.setWantsLayer_(True)
            ring = Quartz.CALayer.layer()
            theme.tint(ring, border="signal")
            ring.setBorderWidth_(2)
            ring.setCornerRadius_(self.layer().cornerRadius() + 2)
            self.layer().addSublayer_(ring)
            self.ring = ring
            self.setNeedsLayout_(True)
        self.ring.setHidden_(not on)


def pressable(callback, fill=None, stroke=None, radius=0.0, role=AppKit.NSAccessibilityButtonRole):
    view = box(fill, stroke, radius, cls=Pressable)
    view.callback = callback
    view.enabled = True
    view.setAccessibilityRole_(role)
    return view


class _WrappingButton(AppKit.NSButton):
    def intrinsicContentSize(self):
        natural = objc.super(_WrappingButton, self).intrinsicContentSize()
        width = self.bounds().size.width or min(240, max(64, natural.width))
        ink = self.attributedTitle().boundingRectWithSize_options_(
            (max(1, width - 24), 10000), AppKit.NSStringDrawingUsesLineFragmentOrigin)
        return AppKit.NSMakeSize(natural.width, max(30, math.ceil(ink.size.height) + 12))

    def setFrameSize_(self, size):
        changed = size.width != self.bounds().size.width
        objc.super(_WrappingButton, self).setFrameSize_(size)
        if changed:
            self.invalidateIntrinsicContentSize()


class Button:
    """A Vernier button. Wraps NSButton rather than subclassing it: its width is recomputed from the
    rendered title on every change, and an AppKit object has nowhere to keep that constraint.
    Styles are `plain` (well, edge border), `primary` (ink) and `live` (signal)."""

    HEIGHTS = {"small": 30, "regular": 30, "big": 40}

    def __init__(self, title, target, action, style="plain", scale="regular", full_width=False):
        self.view = _autolayout(_WrappingButton.buttonWithTitle_target_action_(title, target, action))
        self.view.setBordered_(False)
        self.view.setWantsLayer_(True)
        self.view.layer().setCornerRadius_(theme.RADIUS["button"])
        self.view.heightAnchor().constraintGreaterThanOrEqualToConstant_(self.HEIGHTS[scale]).setActive_(True)
        self.view.widthAnchor().constraintLessThanOrEqualToConstant_(240).setActive_(True)
        self.view.cell().setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
        self.view.cell().setUsesSingleLineMode_(False)
        self.view.cell().setWraps_(True)
        squeeze(self.view)
        self.style = style
        self.scale = scale
        self.full_width = full_width
        self.title = title
        self.enabled = True
        self.width = None
        self._restyle()

    def set_title(self, title):
        if title != self.title:
            self.title = title
            self._restyle()

    def set_style(self, style):
        if style != self.style:
            self.style = style
            self._restyle()

    def set_enabled(self, enabled):
        self.view.setEnabled_(enabled)
        if enabled != self.enabled:
            self.enabled = enabled
            self._restyle()

    def _restyle(self):
        fill, ink = {"primary": ("ink", "ground"), "live": ("signal", "ground")}.get(self.style, ("well", "ink"))
        stroke = "edge" if self.style == "plain" else fill
        if not self.enabled:
            # Faded, a filled button turns into a grey slab with its words lost in it; every style
            # disables to the same quiet outline instead.
            fill, stroke, ink = "panel", "rule", "ink_3"
        text_size = theme.TYPE["small"] if self.scale == "small" else theme.TYPE["button"]
        title = attributed(americanise(self.title), text_size, 600, ink, align=AppKit.NSTextAlignmentCenter, wrap=True)
        self.view.setAttributedTitle_(title)
        paint(self.view, fill, stroke)
        # Width comes from the rendered title, not a measured pixel, so a longer word or a
        # fallback face widens the button instead of clipping it.
        if self.width is not None:
            self.width.setActive_(False)
        self.width = self.view.widthAnchor().constraintEqualToConstant_(min(240, max(64, math.ceil(title.size().width) + 24)))
        self.width.setPriority_(255)
        self.width.setActive_(True)


class _Target(AppKit.NSObject):
    """An NSObject that turns a control's action into a Python callback, so the widgets below
    can hand back plain functions instead of making every window a target for every control."""

    def initWithCallback_(self, callback):
        self = objc.super(_Target, self).init()
        if self is None:
            return None
        self.callback = callback
        return self

    def fire_(self, sender):
        self.callback(sender)


def callback_target(callback):
    """A target for a control whose action should call `callback()`. The control holds its target
    weakly, so the caller keeps this."""
    return _Target.alloc().initWithCallback_(lambda _sender: callback())


def action_button(title, callback, **options):
    """A Button that calls `callback()`, for the buttons a window builds as it goes."""
    target = callback_target(callback)
    button = Button(title, target, "fire:", **options)
    button.target = target
    return button


class _CentredCell(AppKit.NSTextFieldCell):
    def drawingRectForBounds_(self, bounds):
        rect = objc.super(_CentredCell, self).drawingRectForBounds_(bounds)
        height = self.cellSizeForBounds_(rect).height
        return AppKit.NSMakeRect(rect.origin.x, rect.origin.y + max(0, (rect.size.height - height) / 2),
                                 rect.size.width, min(height, rect.size.height))

    def setUpFieldEditorAttributes_(self, editor):
        editor = objc.super(_CentredCell, self).setUpFieldEditorAttributes_(editor)
        control = self.controlView()
        if getattr(control, "pair_editor", None) is None:
            control.pair_editor = editor
            control.pair_selected_attributes = dict(editor.selectedTextAttributes())
            AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                control, "pairingSelectionChanged:", AppKit.NSTextViewDidChangeSelectionNotification, editor)
        editor.setSelectedTextAttributes_({
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.clearColor(),
            AppKit.NSBackgroundColorAttributeName: AppKit.NSColor.clearColor(),
        })
        return editor


class _DigitInk(AppKit.NSView):
    """Draw above the field editor: its selection compositor can erase an underlying glyph."""

    def hitTest_(self, _point):
        return None

    def drawRect_(self, rect):
        control = self.control()
        editor = control.currentEditor()
        text = editor.string() if editor is not None else control.stringValue()
        if not text:
            return
        font = CoreText.CTFontCreateWithName(control.font().fontName(), control.font().pointSize(), None)
        found, glyphs = CoreText.CTFontGetGlyphsForCharacters(font, text[-1], None, 1)
        if not found:
            return
        path = CoreText.CTFontCreatePathForGlyph(font, glyphs[0], None)
        ink = Quartz.CGPathGetBoundingBox(path)
        bounds = self.bounds()
        context = AppKit.NSGraphicsContext.currentContext().CGContext()
        Quartz.CGContextSaveGState(context)
        if editor is not None and editor.selectedRange().length:
            Quartz.CGContextSetFillColorWithColor(context, theme.cg("signal_dim"))
            Quartz.CGContextFillRect(context, bounds)
        Quartz.CGContextTranslateCTM(context, bounds.size.width / 2, bounds.size.height / 2)
        if self.isFlipped():
            Quartz.CGContextScaleCTM(context, 1, -1)
        Quartz.CGContextTranslateCTM(context, -ink.origin.x - ink.size.width / 2, -ink.origin.y - ink.size.height / 2)
        Quartz.CGContextSetFillColorWithColor(context, theme.cg("ink"))
        Quartz.CGContextAddPath(context, path)
        Quartz.CGContextFillPath(context)
        Quartz.CGContextRestoreGState(context)


class _CentredField(AppKit.NSTextField):
    """Keep native editing/accessibility while the overlay centres the outline, not the line box."""

    @classmethod
    def cellClass(cls):
        return _CentredCell

    def pairingSelectionChanged_(self, _notification):
        self.ink_view.setNeedsDisplay_(True)

    def becomeFirstResponder(self):
        focused = objc.super(_CentredField, self).becomeFirstResponder()
        if focused:
            self.delegate().owner.begin_editing(self)
        return focused

    def setStringValue_(self, value):
        objc.super(_CentredField, self).setStringValue_(value)
        self.setNeedsDisplay_(True)
        overlay = getattr(self, "ink_view", None)
        if overlay is not None:
            overlay.setNeedsDisplay_(True)


def field(secure=False, mono=True, cls=None):
    """A text input on `ground` with an `edge` border and the text inset off it. Returns
    (container, control): add the container, read the control."""
    cls = cls or (AppKit.NSSecureTextField if secure else AppKit.NSTextField)
    control = _autolayout(cls.alloc().init())
    control.setBordered_(False)
    control.setDrawsBackground_(False)
    control.setFocusRingType_(AppKit.NSFocusRingTypeNone)
    control.setFont_(theme.mono_font() if mono else theme.font(theme.TYPE["body"]))
    control.setTextColor_(theme.colour("ink"))
    control.cell().setUsesSingleLineMode_(True)
    control.cell().setScrollable_(True)
    squeeze(control)
    container = box("ground", "edge", theme.RADIUS["field"])
    container.addSubview_(control)
    pin(control, container, (5, 8, 5, 8))
    return container, control


class _ResponsiveView(AppKit.NSView):
    owner = None

    def setFrameSize_(self, size):
        changed = size.width != self.bounds().size.width
        objc.super(_ResponsiveView, self).setFrameSize_(size)
        if changed and self.owner is not None:
            self.setNeedsLayout_(True)

    def layout(self):
        if self.owner is not None:
            self.owner.arrange(self.bounds().size.width)
        objc.super(_ResponsiveView, self).layout()


class FieldRow:
    def __init__(self, caption, control):
        self.view = _autolayout(_ResponsiveView.alloc().init())
        self.view.owner = self
        self.view.field_owner = self
        self.control = control
        self.caption = caption
        self.words = Label(caption or '', theme.TYPE['note'], ink='ink_2', wrap=True)
        self.words.view.setHidden_(caption is None)
        for child in ((self.words.view, control) if caption is not None else (control,)):
            self.view.addSubview_(child)
        self.constraints = []
        self.stacked = None
        if caption is not None and not control.accessibilityLabel():
            control.setAccessibilityLabel_(caption)
        selector = getattr(control, 'segmented_owner', None)
        if selector is not None:
            selector.set_cap({'Length': 240, 'Size': 240, 'This window': 252,
                              'Ticks at': 360, 'Style for': 240, 'Show at': 264}.get(caption, selector.cap))
        self.arrange(640)

    def arrange(self, width):
        stacked = self.caption is None or width < 528
        selector = getattr(self.control, 'segmented_owner', None)
        if selector is not None:
            selector.set_available_width(max(0, width - (0 if stacked else 196)))
        if stacked == self.stacked:
            return
        self.stacked = stacked
        AppKit.NSLayoutConstraint.deactivateConstraints_(self.constraints)
        label, control, view = self.words.view, self.control, self.view
        if self.caption is None:
            self.constraints = [
                control.topAnchor().constraintEqualToAnchor_(view.topAnchor()),
                control.bottomAnchor().constraintEqualToAnchor_(view.bottomAnchor()),
                control.leadingAnchor().constraintEqualToAnchor_(view.leadingAnchor()),
                control.trailingAnchor().constraintLessThanOrEqualToAnchor_(view.trailingAnchor()),
            ]
            AppKit.NSLayoutConstraint.activateConstraints_(self.constraints)
            return
        constraints = [label.topAnchor().constraintEqualToAnchor_(view.topAnchor()),
                       label.leadingAnchor().constraintEqualToAnchor_(view.leadingAnchor()),
                       view.bottomAnchor().constraintGreaterThanOrEqualToAnchor_(label.bottomAnchor()),
                       view.bottomAnchor().constraintEqualToAnchor_(control.bottomAnchor()),
                       control.trailingAnchor().constraintLessThanOrEqualToAnchor_(view.trailingAnchor())]
        if stacked:
            constraints.extend([label.widthAnchor().constraintEqualToAnchor_(view.widthAnchor()),
                                control.topAnchor().constraintEqualToAnchor_constant_(label.bottomAnchor(), 8),
                                control.leadingAnchor().constraintEqualToAnchor_(view.leadingAnchor())])
        else:
            constraints.extend([label.widthAnchor().constraintEqualToConstant_(180),
                                control.topAnchor().constraintEqualToAnchor_(view.topAnchor()),
                                control.leadingAnchor().constraintEqualToAnchor_constant_(label.trailingAnchor(), 16)])
        available = control.widthAnchor().constraintEqualToAnchor_constant_(view.widthAnchor(), 0 if stacked else -196)
        available.setPriority_(255)
        constraints.append(available)
        self.constraints = constraints
        AppKit.NSLayoutConstraint.activateConstraints_(constraints)


def field_row(caption, control):
    row = FieldRow(caption, control)
    return row.view, row.words


class _Tick(AppKit.NSView):
    def isFlipped(self):
        return True

    def drawRect_(self, _rect):
        on = getattr(self, "on", False)
        bounds = AppKit.NSInsetRect(self.bounds(), 0.5, 0.5)
        outline = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, 3, 3)
        if on:
            theme.colour("signal").setFill()
            outline.fill()
            check = AppKit.NSBezierPath.bezierPath()
            check.moveToPoint_((3.5, 7.8))
            check.lineToPoint_((6.3, 10.6))
            check.lineToPoint_((11.5, 4.6))
            check.setLineWidth_(1.8)
            check.setLineCapStyle_(AppKit.NSLineCapStyleRound)
            check.setLineJoinStyle_(AppKit.NSLineJoinStyleRound)
            theme.colour("ground").setStroke()
            check.stroke()
        else:
            theme.colour("well").setFill()
            outline.fill()
            theme.colour("edge").setStroke()
            outline.stroke()


class WayTile:
    """A way in: a checkbox, its name and a line saying what it is, the whole tile clickable."""

    def __init__(self, title, detail="", on_change=None):
        self.on_change = on_change
        self._value = False
        self.enabled = True
        self.view = pressable(self._toggle, "ground", role=AppKit.NSAccessibilityCheckBoxRole)
        self.view.setAccessibilityLabel_(title)
        self.view.ring_inset = 1.0
        self.tick = size(_autolayout(_Tick.alloc().init()), 15, 15)
        self.name = Label(title, theme.TYPE["body"], 600, wrap=True)
        self.detail = Label(detail, theme.TYPE["small"], ink="ink_3", wrap=True)
        # No detail takes no line, so a tile that is only a name stays one line tall.
        self.detail.view.setHidden_(not detail)
        words = stack(spacing=2)
        add(words, self.name.view)
        add(words, self.detail.view)
        line = stack(vertical=False, spacing=9)
        line.setAlignment_(AppKit.NSLayoutAttributeTop)
        line.addArrangedSubview_(self.tick)
        line.addArrangedSubview_(words)
        hug(words, AppKit.NSLayoutPriorityDefaultLow)
        self.view.addSubview_(line)
        pin(line, self.view, (9, 10, 9, 10))

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        self._value = bool(value)
        self.tick.on = self._value
        self.tick.setNeedsDisplay_(True)
        self.view.setAccessibilityValue_(1 if self._value else 0)

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.view.enabled = enabled
        self.name.set(ink="ink" if enabled else "ink_3")
        self.tick.setAlphaValue_(1.0 if enabled else 0.45)

    def set_detail(self, text):
        self.detail.set(text)
        self.detail.view.setHidden_(not text)

    def _toggle(self):
        self.value = not self._value
        if self.on_change is not None:
            self.on_change(self._value)


class _Track(AppKit.NSView):
    """The switch's track, drawn; the knob is its own layer so it can slide across."""

    def isFlipped(self):
        return True

    def drawRect_(self, _rect):
        on = getattr(self, "on", False)
        track = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(AppKit.NSInsetRect(self.bounds(), 0.5, 0.5), 8.5, 8.5)
        theme.colour("well").setFill()
        track.fill()
        theme.colour("signal" if on else "edge").setStroke()
        track.stroke()

    @objc.python_method
    def place_knob(self, on, animate):
        knob = getattr(self, "knob", None)
        if knob is None:
            self.setWantsLayer_(True)
            knob = Quartz.CALayer.layer()
            knob.setCornerRadius_(6)
            self.layer().addSublayer_(knob)
            self.knob = knob
        motion.transaction(animate, motion.DURATION * 0.75)
        # Layer frames here are in the view's own flipped space, top left at the origin.
        knob.setFrame_(((18 if on else 3, 3), (12, 12)))
        theme.tint(knob, background="signal" if on else "ink_3")
        Quartz.CATransaction.commit()


class Switch:
    """A labelled on/off switch: the words on the left, the track on the right, the row clickable."""

    def __init__(self, title, on_change=None):
        self.on_change = on_change
        self._value = False
        self.enabled = True
        self.view = pressable(self._toggle, role=AppKit.NSAccessibilityCheckBoxRole)
        self.view.setAccessibilityLabel_(title)
        self.track = size(_autolayout(_Track.alloc().init()), 34, 18)
        self.track.place_knob(False, False)
        self.view.heightAnchor().constraintGreaterThanOrEqualToConstant_(30).setActive_(True)
        self.words = Label(title, theme.TYPE["body"], wrap=True)
        line = stack(vertical=False, spacing=12)
        line.addArrangedSubview_(self.words.view)
        line.addArrangedSubview_(self.track)
        hug(self.words.view, AppKit.NSLayoutPriorityDefaultLow)
        self.view.addSubview_(line)
        pin(line, self.view, (3, 0, 3, 0))

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        value = bool(value)
        changed = value != self._value
        self._value = value
        self.track.on = value
        self.track.setNeedsDisplay_(True)
        self.track.place_knob(value, changed and motion.live(self.view))
        self.view.setAccessibilityValue_(1 if value else 0)

    def set_enabled(self, enabled):
        self.enabled = enabled
        # Off the tab order too, not only faded: a dimmed switch that still takes Space reads as
        # broken.
        self.view.enabled = enabled
        self.view.setAlphaValue_(1.0 if enabled else 0.45)

    def _toggle(self):
        if not self.enabled:
            return
        self.value = not self._value
        if self.on_change is not None:
            self.on_change(self._value)


class _ControlBox(AppKit.NSView):
    pass


class Segmented:
    """One choice of several, laid out `columns` across in as many rows as it takes. `.value` reads
    and writes the value rather than the index; a value that is not a choice selects nothing."""

    def __init__(self, choices, columns=None, on_change=None):
        self.choices = list(choices)
        self.on_change = on_change
        self._value = None
        self.enabled = True
        columns = columns or len(self.choices)
        self.view = box("edge", "edge", theme.RADIUS["segment"], cls=_ControlBox)
        self.view.layer().setMasksToBounds_(True)
        # One group to VoiceOver, named by its field_row caption, with the choices inside it.
        self.view.setAccessibilityElement_(True)
        self.view.setAccessibilityRole_(AppKit.NSAccessibilityRadioGroupRole)
        grid = _autolayout(_ResponsiveView.alloc().init())
        grid.owner = self
        self.grid = grid
        self.columns = columns
        self.layout_columns = None
        self.available_width = None
        self.layout_constraints = []
        self.cells = []
        for start in range(0, len(self.choices), columns):
            for value, title in self.choices[start:start + columns]:
                cell = pressable(lambda value=value: self._choose(value), "well", role=AppKit.NSAccessibilityRadioButtonRole)
                cell.setAccessibilityLabel_(title)
                cell.ring_inset = 2.0
                words = Label(title, theme.TYPE["small"], ink="ink_2", wrap=True, align=AppKit.NSTextAlignmentCenter)
                squeeze(words.view)
                cell.addSubview_(words.view)
                pin(words.view, cell, (6, 4, 6, 4))
                cell.heightAnchor().constraintGreaterThanOrEqualToConstant_(30).setActive_(True)
                grid.addSubview_(cell)
                self.cells.append((value, cell, words))
        self.view.addSubview_(grid)
        pin(grid, self.view, (1, 1, 1, 1))
        self.view.segmented_owner = self
        measured = max(attributed(title, theme.TYPE['small']).size().width + 24 for _, title in self.choices)
        self.cap_constraint = self.view.widthAnchor().constraintLessThanOrEqualToConstant_(360)
        self.cap_constraint.setActive_(True)
        self.preference = self.view.widthAnchor().constraintEqualToConstant_(360)
        self.preference.setPriority_(255)
        self.preference.setActive_(True)
        self.set_cap(min(120, measured) * columns)
        self.arrange(self.cap - 2)

    def set_cap(self, width):
        self.cap = min(width, 120 * self.columns, 360)
        self.cap_constraint.setConstant_(self.cap)
        self.preference.setConstant_(self.cap)

    def set_available_width(self, width):
        self.available_width = width
        cap = self.cap if width >= self.cap else min(120, width)
        if cap != self.cap_constraint.constant():
            self.cap_constraint.setConstant_(cap)
            self.preference.setConstant_(cap)
            self.grid.setNeedsLayout_(True)

    def arrange(self, width):
        available = self.available_width if self.available_width is not None else width + 2
        columns = self.columns if available >= self.cap else 1
        if self.layout_columns == columns:
            return
        self.layout_columns = columns
        AppKit.NSLayoutConstraint.deactivateConstraints_(self.layout_constraints)
        constraints = []
        rows = [self.cells[start:start + columns] for start in range(0, len(self.cells), columns)]
        previous = None
        for row in rows:
            for index, (_value, cell, _words) in enumerate(row):
                constraints.extend([
                    cell.topAnchor().constraintEqualToAnchor_constant_(previous.bottomAnchor() if previous else self.grid.topAnchor(), 1 if previous else 0),
                    cell.leadingAnchor().constraintEqualToAnchor_constant_(self.grid.leadingAnchor() if index == 0 else row[index-1][1].trailingAnchor(), 0 if index == 0 else 1),
                    cell.widthAnchor().constraintEqualToAnchor_multiplier_constant_(self.grid.widthAnchor(), 1 / columns, -(columns - 1) / columns),
                    cell.heightAnchor().constraintEqualToAnchor_(row[0][1].heightAnchor()),
                ])
            previous = row[0][1]
        constraints.append(self.grid.bottomAnchor().constraintEqualToAnchor_(previous.bottomAnchor()))
        self.layout_constraints = constraints
        AppKit.NSLayoutConstraint.activateConstraints_(constraints)


    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        self._value = value

        def mark():
            for candidate, cell, words in self.cells:
                chosen = candidate == value
                # Disabled, the choice stays marked by weight but loses the ink fill, which would
                # otherwise still read as live through the fade.
                lit = chosen and self.enabled
                paint(cell, "ink" if lit else "well")
                words.set(ink="ground" if lit else "ink_2", weight=600 if chosen else 400)
                cell.setAccessibilityValue_(1 if chosen else 0)

        motion.fade_paint(self.view, mark)

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.view.setAlphaValue_(1.0 if enabled else 0.45)
        for _value, cell, _words in self.cells:
            cell.enabled = enabled
        self.value = self._value

    def _choose(self, value):
        if value == self._value:
            return
        self.value = value
        if self.on_change is not None:
            self.on_change(value)


class MenuChoice:
    """Native menu keeps full machine names available without growing the column."""
    def __init__(self, choices, on_change=None):
        self.choices = list(choices)
        self.on_change = on_change
        self.view = box()
        self.popup = _autolayout(AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(((0, 0), (0, 0)), False))
        self.popup.setFont_(theme.font(theme.TYPE['body']))
        self.popup.setAutoenablesItems_(False)
        self.popup.setAccessibilityLabel_('Match design with')
        self.view.addSubview_(self.popup)
        # Native popup frames extend beyond their alignment rect by seven points.
        pin(self.popup, self.view, (0, 4, 0, 4))
        self.items = []
        for value, title in choices:
            self.popup.addItemWithTitle_(title)
            item = self.popup.lastItem()
            item.setToolTip_(title)
            self.items.append((value, item))
        self.target = _Target.alloc().initWithCallback_(self._changed)
        self.popup.setTarget_(self.target)
        self.popup.setAction_('fire:')
        self.view.widthAnchor().constraintLessThanOrEqualToConstant_(320).setActive_(True)
        preference = self.view.widthAnchor().constraintEqualToConstant_(320)
        preference.setPriority_(255)
        preference.setActive_(True)
        self.view.heightAnchor().constraintGreaterThanOrEqualToConstant_(30).setActive_(True)
        squeeze(self.view)
        squeeze(self.popup)

    @property
    def value(self):
        return self.choices[self.popup.indexOfSelectedItem()][0]

    @value.setter
    def value(self, value):
        self.popup.selectItemAtIndex_(next((i for i, pair in enumerate(self.choices) if pair[0] == value), 0))
        self.popup.setAccessibilityValue_(self.popup.titleOfSelectedItem())

    def _changed(self, _sender):
        if self.on_change is not None:
            self.on_change(self.value)


class Selector:
    """The MAC / WINDOWS indicator: shows where input is, and is not itself a control."""

    def __init__(self):
        self.view = box(stroke="edge", radius=theme.RADIUS["field"])
        self.view.layer().setMasksToBounds_(True)
        row = stack(vertical=False, spacing=0)
        row.setDistribution_(AppKit.NSStackViewDistributionFillEqually)
        self.halves = {}
        for side, title in (("mac", "Mac"), ("windows", "Windows")):
            half = box()
            words = Label(title, theme.TYPE["eyebrow"], 700, "ink_3", tracking=theme.TRACKING["selector"], upper=True,
                          align=AppKit.NSTextAlignmentCenter)
            half.addSubview_(words.view)
            pin(words.view, half, (7, 4, 7, 4))
            bar = box()
            half.addSubview_(bar)
            AppKit.NSLayoutConstraint.activateConstraints_([
                bar.leadingAnchor().constraintEqualToAnchor_(half.leadingAnchor()),
                bar.trailingAnchor().constraintEqualToAnchor_(half.trailingAnchor()),
                bar.bottomAnchor().constraintEqualToAnchor_(half.bottomAnchor()),
                bar.heightAnchor().constraintEqualToConstant_(2),
            ])
            row.addArrangedSubview_(half)
            self.halves[side] = (half, words, bar)
        self.view.addSubview_(row)
        pin(row, self.view, (1, 1, 1, 1))
        self.side = None
        self.set("mac")

    def set(self, side):
        if side == self.side:
            return
        self.side = side
        for name, (half, words, bar) in self.halves.items():
            on = name == side
            accent = "signal" if name == "windows" else "ink_2"
            paint(half, "well" if on else None)
            paint(bar, accent if on else None)
            words.set(ink=("signal" if name == "windows" else "ink") if on else "ink_3")


class _Flipped(AppKit.NSView):
    def isFlipped(self):
        return True


def _fill(name, rect):
    theme.colour(name).setFill()
    AppKit.NSBezierPath.fillRect_(rect)


class Spark(_Flipped):
    """The last 24 round-trip samples as bars, the newest lit when live."""

    SAMPLES = 24

    def drawRect_(self, _rect):
        samples = list(getattr(self, "samples", ()))[-self.SAMPLES:]
        live = getattr(self, "live", False)
        width, height = self.bounds().size.width, self.bounds().size.height
        gap = 2.0
        bar = (width - gap * (self.SAMPLES - 1)) / self.SAMPLES
        peak = max(samples) if samples else 1
        padded = [None] * (self.SAMPLES - len(samples)) + samples
        for index, sample in enumerate(padded):
            tall = 1.0 if sample is None else max(1.0, height * min(1.0, sample / max(peak, 1)))
            colour = "ink_3"
            if live and sample is not None:
                colour = "signal" if index == self.SAMPLES - 1 else "signal_dim"
            _fill(colour, ((index * (bar + gap), height - tall), (bar, tall)))


class Scale(_Flipped):
    """A short tick scale with a marker at `fraction`, the resistance readout's vernier."""

    def drawRect_(self, _rect):
        width, height = self.bounds().size.width, self.bounds().size.height
        for step in range(11):
            x = min(width - 1, step * width / 10)
            major = step % 5 == 0
            tall = 9 if major else 5
            _fill("ink_3" if major else "edge", ((x, height - tall), (1, tall)))
        x = 5 + getattr(self, "fraction", 0.0) * (width - 10)
        marker = AppKit.NSBezierPath.bezierPath()
        marker.moveToPoint_((x - 5, 0))
        marker.lineToPoint_((x + 5, 0))
        marker.lineToPoint_((x, 6))
        marker.closePath()
        theme.colour("signal").setFill()
        marker.fill()


def scale_fraction(value, low, high, scale="linear"):
    """Where `value` sits along a ruler from 0 to 1, pinned to the ends outside the range."""
    value = min(max(value, low), high)
    if scale == "sqrt":
        return (math.sqrt(value) - math.sqrt(low)) / (math.sqrt(high) - math.sqrt(low))
    return (value - low) / (high - low)


def scale_value(fraction, low, high, scale="linear"):
    fraction = min(max(fraction, 0.0), 1.0)
    if scale == "sqrt":
        root = math.sqrt(low) + fraction * (math.sqrt(high) - math.sqrt(low))
        return root * root
    return low + fraction * (high - low)


class _RulerView(_Flipped):
    TRACK = 30.0
    TOP = 4.0
    drawn_focus = False

    def acceptsFirstResponder(self):
        return self.owner.enabled

    def refresh_keyboard_focus(self):
        if keyboard_focus_visible(self) != self.drawn_focus:
            self.setNeedsDisplay_(True)

    def acceptsFirstMouse_(self, _event):
        return True

    def becomeFirstResponder(self):
        self.setNeedsDisplay_(True)
        return True

    def resignFirstResponder(self):
        self.setNeedsDisplay_(True)
        return True

    def mouseDown_(self, event):
        self._drag(event)

    def mouseDragged_(self, event):
        self._drag(event)

    @objc.python_method
    def _drag(self, event):
        if not self.owner.enabled:
            return
        self.window().makeFirstResponder_(self)
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        span = max(1.0, self.bounds().size.width - 3)
        self.owner.move_to((point.x - 1.5) / span)

    def keyDown_(self, event):
        keys = event.charactersIgnoringModifiers()
        steps = {AppKit.NSLeftArrowFunctionKey: -1, AppKit.NSDownArrowFunctionKey: -1,
                 AppKit.NSRightArrowFunctionKey: 1, AppKit.NSUpArrowFunctionKey: 1}
        direction = steps.get(keys[0]) if keys else None
        if direction is None or not self.owner.enabled:
            objc.super(_RulerView, self).keyDown_(event)
            return
        self.owner.nudge(direction, fine=bool(event.modifierFlags() & AppKit.NSEventModifierFlagOption))

    def isAccessibilityElement(self):
        return True

    def isAccessibilityEnabled(self):
        return bool(self.owner.enabled)

    def accessibilityRole(self):
        return AppKit.NSAccessibilitySliderRole

    def accessibilityLabel(self):
        return self.owner.title

    def accessibilityValue(self):
        return self.owner.value

    def accessibilityPerformIncrement(self):
        self.owner.nudge(1)
        return True

    def accessibilityPerformDecrement(self):
        self.owner.nudge(-1)
        return True

    def drawRect_(self, _rect):
        owner = self.owner
        width = self.bounds().size.width
        base = self.TOP + self.TRACK
        span = width - 3
        thumb = 1.5 + owner.fraction() * span
        _fill(owner.fill, ((0, base - 2), (thumb, 2)))
        for step in range(owner.minor + 1):
            _fill("edge", ((min(width - 1, step * width / owner.minor), base - 8), (1, 8)))
        attributes = {
            AppKit.NSFontAttributeName: theme.mono_font(theme.TYPE["eyebrow"]),
            AppKit.NSForegroundColorAttributeName: theme.colour("ink_3"),
        }
        for index, value in enumerate(owner.labels):
            x = 1.5 + scale_fraction(value, owner.low, owner.high, owner.scale) * span
            _fill("ink_3", ((min(width - 1, x - 0.5), base - 16), (1, 16)))
            text = AppKit.NSString.stringWithString_(str(value))
            text_width = text.sizeWithAttributes_(attributes).width
            left = 0 if index == 0 else width - text_width if index == len(owner.labels) - 1 else x - text_width / 2
            text.drawAtPoint_withAttributes_((left, base + 5), attributes)
        # A drag makes the ruler first responder so the arrows nudge it next, which is not
        # keyboard navigation: the signal thumb waits for Tab or an arrow.
        focused = keyboard_focus_visible(self)
        self.drawn_focus = focused
        _fill("panel", ((thumb - 4.5, self.TOP), (9, self.TRACK)))
        _fill("signal" if focused else "ink", ((thumb - 1.5, self.TOP), (3, self.TRACK)))


class Ruler:
    """A slider drawn as a ruler: ticks, a filled run to the thumb, labelled majors. `scale` is
    `linear` or `sqrt`, which gives the low end of a wide range most of the travel. `.value` is the
    true value even when it lies outside the ruler, in which case the thumb pins to the end; it
    only changes when the ruler is moved. `on_change` fires continuously."""

    def __init__(self, low, high, labels, step=1, scale="linear", minor=25, fill="ink_2", on_change=None,
                 title="", arrow_step=None):
        self.low, self.high, self.labels, self.step, self.scale = low, high, labels, step, scale
        self.minor, self.fill, self.on_change = minor, fill, on_change
        self.title = title
        # A linear ruler's arrows move this far, and `step` with Option held.
        self.arrow_step = arrow_step or step
        self.enabled = True
        self._value = low
        self.view = _autolayout(_RulerView.alloc().init())
        self.view.owner = self
        self.view.heightAnchor().constraintEqualToConstant_(_RulerView.TOP + _RulerView.TRACK + 16).setActive_(True)

    def fraction(self):
        return scale_fraction(self._value, self.low, self.high, self.scale)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        self._value = int(value)
        self.view.setNeedsDisplay_(True)

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.view.setAlphaValue_(1.0 if enabled else 0.4)

    def move_to(self, fraction):
        raw = scale_value(fraction, self.low, self.high, self.scale)
        self._set(int(round(raw / self.step) * self.step))

    def nudge(self, direction, fine=False):
        pinned = min(max(self._value, self.low), self.high)
        # Equal steps along the ruler rather than in units, so arrows move evenly across a square
        # root scale; never less than one step, or a rounded step could go nowhere.
        target = scale_value(self.fraction() + direction / self.minor, self.low, self.high, self.scale)
        target = int(round(target / self.step) * self.step)
        if self.scale == "linear":
            target = pinned + direction * (self.step if fine else self.arrow_step)
        elif fine or target == pinned:
            target = pinned + direction * self.step
        self._set(target)

    def _set(self, value):
        value = min(max(value, self.low), self.high)
        if value == self._value:
            return
        self.value = value
        if self.on_change is not None:
            self.on_change(value)


# Loaded if a config names them, never offered by the recorder, as on Windows: typing keys a
# double-tap or a hold would take from every app.
UNRECORDABLE_TRIGGERS = {"enter", "tab", "space", "backspace", "esc"}


class KeyRecorder:
    """A keycap that records the next key pressed as the trigger. Clicking arms it; the next key
    or modifier press that Beamer can use as a trigger becomes the value, anything else cancels.
    Recorded through a local event monitor rather than the event tap, so it works before either
    Mac permission is granted."""

    RECORDING_TITLE = "Press a key…"
    HINT = "Click, then press the key"
    RECORDING_HINT = "Click again to cancel"

    def __init__(self, value, on_change=None):
        self.value = value
        self.on_change = on_change
        self.monitor = None
        # The keycap's thicker bottom edge is the outer box showing through a 3pt gap.
        self.view = pressable(self._clicked, "edge", radius=theme.RADIUS["keycap"])
        self.view.setAccessibilityLabel_(f"Shortcut key, {key_title(value)}")
        face = box("ground", radius=theme.RADIUS["keycap"] - 1)
        self.view.addSubview_(face)
        pin(face, self.view, (1, 1, 3, 1))
        self.key = Label(key_title(value), theme.TYPE["keycap"], mono=True, tracking=-0.02, wrap=True)
        squeeze(self.key.view)
        self.hint = Label(self.HINT, theme.TYPE["small"], ink="ink_3", align=AppKit.NSTextAlignmentRight, wrap=True)
        line = stack(spacing=8)
        line.addArrangedSubview_(self.key.view)
        line.addArrangedSubview_(self.hint.view)
        hug(self.key.view, AppKit.NSLayoutPriorityDefaultLow)
        face.addSubview_(line)
        pin(line, face, (9, 12, 8, 12))

    def set_value(self, value):
        self.value = value
        self.key.set(key_title(value))
        self.view.setAccessibilityLabel_(f"Shortcut key, {key_title(value)}")

    def _clicked(self):
        if self.monitor is not None:
            self._stop()
            return
        self.key.set(self.RECORDING_TITLE, ink="signal")
        self.hint.set(self.RECORDING_HINT)
        paint(self.view, "signal")
        mask = AppKit.NSEventMaskKeyDown | AppKit.NSEventMaskFlagsChanged
        self.monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask, self._recorded)

    def _recorded(self, event):
        # Modifier keys arrive as flagsChanged twice, down then up; the first is the press.
        # A key Beamer cannot use as a trigger cancels, so the previous value is what shows.
        name = SPECIAL_KEY_NAMES.get(int(event.keyCode()))
        self._stop()
        if name in KEY_NAME_TO_CODE and name not in UNRECORDABLE_TRIGGERS:
            self.set_value(name)
            if self.on_change is not None:
                self.on_change(name)
        return None

    def cancel(self):
        self._stop()

    def _stop(self):
        if self.monitor is not None:
            AppKit.NSEvent.removeMonitor_(self.monitor)
            self.monitor = None
        self.key.set(key_title(self.value), ink="ink")
        self.hint.set(self.HINT)
        paint(self.view, "edge")


class JumpKeyRecorder:
    """Records a modifier chord for a peer-specific local jump key."""

    HINT = "Click, then press a key combination"

    def __init__(self, value="", on_change=None, on_arm=None):
        self.value = value or ""
        self.on_change = on_change
        self.on_arm = on_arm
        self.monitor = None
        self.view = pressable(self._clicked, "edge", radius=theme.RADIUS["keycap"])
        self.view.setAccessibilityLabel_("Jump straight here key, " + self._title())
        face = box("ground", radius=theme.RADIUS["keycap"] - 1)
        self.view.addSubview_(face)
        pin(face, self.view, (1, 1, 3, 1))
        self.key = Label(self._title(), theme.TYPE["body"], ink="ink_3", wrap=True)
        squeeze(self.key.view)
        self.hint = Label(self.HINT, theme.TYPE["small"], ink="ink_3", align=AppKit.NSTextAlignmentLeft, wrap=True)
        self.line = stack(spacing=5)
        self.line.addArrangedSubview_(self.key.view)
        self.line.addArrangedSubview_(self.hint.view)
        line = self.line
        face.addSubview_(line)
        pin(line, face, (7, 12, 7, 12))

    def _show_title(self, ink=None):
        has_value = bool(self.value)
        self.key.set(
            self._title(),
            size=theme.TYPE["keycap"] if has_value else theme.TYPE["body"],
            ink=ink or ("ink" if has_value else "ink_3"),
            mono=has_value,
        )

    def _title(self):
        if not self.value:
            return "Choose a key combination"
        names = {"ctrl": "Control", "alt": "Option", "shift": "Shift", "cmd": "Command"}
        return " ".join(names.get(part, key_title(part)) for part in self.value.split("+"))

    def set_value(self, value):
        self.value = value or ""
        self._show_title()
        self.view.setAccessibilityLabel_("Jump straight here key, " + self._title())

    def _clicked(self):
        if self.monitor is not None:
            self._stop()
            return
        self.key.set("Press a key combination…", size=theme.TYPE["body"], ink="signal", mono=False)
        self.hint.set("Click again to cancel")
        paint(self.view, "signal")
        self.monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskKeyDown | AppKit.NSEventMaskFlagsChanged, self._recorded
        )
        if self.on_arm is not None:
            self.on_arm(True)

    def _recorded(self, event):
        if event.type() != AppKit.NSEventTypeKeyDown:
            return event
        flags = int(event.modifierFlags())
        modifiers = []
        for name, flag in (("ctrl", AppKit.NSEventModifierFlagControl), ("alt", AppKit.NSEventModifierFlagOption),
                           ("shift", AppKit.NSEventModifierFlagShift), ("cmd", AppKit.NSEventModifierFlagCommand)):
            if flags & flag:
                modifiers.append(name)
        keycode = int(event.keyCode())
        key = SPECIAL_KEY_NAMES.get(keycode) or PRINTABLE_KEY_FALLBACKS.get(keycode)
        chord = core_ways.jump_chord(modifiers, key)
        if chord is None:
            return None
        self._stop()
        self.set_value(chord)
        if self.on_change is not None:
            self.on_change(chord)
        return None

    def cancel(self):
        self._stop()

    def _stop(self):
        if self.monitor is not None:
            AppKit.NSEvent.removeMonitor_(self.monitor)
            self.monitor = None
            if self.on_arm is not None:
                self.on_arm(False)
        self._show_title()
        self.hint.set(self.HINT)
        paint(self.view, "edge")


class IgnoredRecorder:
    """A keycap that records the next key, mouse button or media key as an entry to keep on this
    Mac while its input is on Windows. Clicking arms it; the left mouse button is never recorded,
    since a left click on the control is what cancels it. The current trigger key refuses rather
    than recording -- it always stays with Beamer -- and `on_recorded` is called with None for
    that refusal. Recorded through a local event monitor, same as KeyRecorder, so it works before
    either Mac permission is granted."""

    TITLE = "Add a key or button"
    RECORDING_TITLE = "Press a key or button…"
    HINT = "Click, then press it"
    RECORDING_HINT = "Click again to cancel"

    def __init__(self, trigger_code, on_recorded=None):
        self.trigger_code = trigger_code
        self.on_recorded = on_recorded
        self.monitor = None
        self.view = pressable(self._clicked, "edge", radius=theme.RADIUS["keycap"])
        self.view.setAccessibilityLabel_("Add a key or button that stays on this Mac")
        face = box("ground", radius=theme.RADIUS["keycap"] - 1)
        self.view.addSubview_(face)
        pin(face, self.view, (1, 1, 3, 1))
        # Body size, unlike the shortcut's keycap: this is an action at the foot of a list, and at
        # keycap size it outweighed the entries above it.
        self.title = Label(self.TITLE, theme.TYPE["body"], 600, wrap=True)
        squeeze(self.title.view)
        self.hint = Label(self.HINT, theme.TYPE["small"], ink="ink_3", align=AppKit.NSTextAlignmentRight, wrap=True)
        line = stack(spacing=8)
        line.addArrangedSubview_(self.title.view)
        line.addArrangedSubview_(self.hint.view)
        hug(self.title.view, AppKit.NSLayoutPriorityDefaultLow)
        face.addSubview_(line)
        pin(line, face, (9, 12, 8, 12))

    def set_trigger_code(self, trigger_code):
        self.trigger_code = trigger_code

    def _clicked(self):
        if self.monitor is not None:
            self._stop()
            return
        self.title.set(self.RECORDING_TITLE, ink="signal")
        self.hint.set(self.RECORDING_HINT)
        paint(self.view, "signal")
        mask = (
            AppKit.NSEventMaskKeyDown
            | AppKit.NSEventMaskFlagsChanged
            | AppKit.NSEventMaskRightMouseDown
            | AppKit.NSEventMaskOtherMouseDown
            | AppKit.NSEventMaskSystemDefined
        )
        self.monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask, self._recorded)

    def _recorded(self, event):
        event_type = event.type()
        if event_type == AppKit.NSEventTypeSystemDefined:
            # Brightness, Mission Control and several undocumented events share this subtype-8
            # event type; only NX_SUBTYPE_AUX_CONTROL_BUTTONS is a media key.
            if int(event.subtype()) != media_keys.NX_SUBTYPE_AUX_CONTROL_BUTTONS:
                return event
            entry = recorded_entry("media", int(event.data1()), self.trigger_code)
            if entry is None:
                # A key going up, or one Beamer does not track, such as brightness: it keeps
                # working, and the recorder keeps listening.
                return event
        elif event_type == AppKit.NSEventTypeRightMouseDown:
            entry = recorded_entry("right", 0, self.trigger_code)
        elif event_type == AppKit.NSEventTypeOtherMouseDown:
            entry = recorded_entry("other", int(event.buttonNumber()), self.trigger_code)
        else:
            if event_type == AppKit.NSEventTypeFlagsChanged and is_modifier_release(event.keyCode(), event.modifierFlags()):
                return event
            entry = recorded_entry("key", int(event.keyCode()), self.trigger_code)
        self._stop()
        if self.on_recorded is not None:
            self.on_recorded(entry)
        return None

    def cancel(self):
        self._stop()

    def _stop(self):
        if self.monitor is not None:
            AppKit.NSEvent.removeMonitor_(self.monitor)
            self.monitor = None
        self.title.set(self.TITLE, ink="ink")
        self.hint.set(self.HINT)
        paint(self.view, "edge")


class _CodeDelegate(AppKit.NSObject):
    def controlTextDidBeginEditing_(self, notification):
        self.owner.begin_editing(notification.object())

    def controlTextDidChange_(self, notification):
        self.owner.changed(notification.object())

    def controlTextDidEndEditing_(self, notification):
        self.owner.end_editing(notification.object())

    def control_textView_doCommandBySelector_(self, control, _text_view, selector):
        return self.owner.command(control, selector)


class CodeBoxes:
    """Six single-digit boxes for the pairing code. Typing moves on, Backspace in an empty box moves
    back, pasting the whole code fills every box, and Return submits."""

    def __init__(self, digits, on_submit, editable=True):
        self.on_submit = on_submit
        self.delegate = _CodeDelegate.alloc().init()
        self.delegate.owner = self
        self.view = stack(vertical=False, spacing=4)
        self.view.setDistribution_(AppKit.NSStackViewDistributionFillEqually)
        self.fields = []
        for index in range(digits):
            container, control = field(cls=_CentredField)
            control.cell().setScrollable_(False)
            control.setFont_(theme.mono_font(theme.TYPE["keycap"]))
            control.setAlignment_(AppKit.NSTextAlignmentCenter)
            control.setTextColor_(AppKit.NSColor.clearColor())
            control.setEditable_(editable)
            control.setSelectable_(editable)
            overlay = _autolayout(_DigitInk.alloc().init())
            overlay.control = objc.WeakRef(control)
            control.ink_view = overlay
            container.addSubview_(overlay)
            AppKit.NSLayoutConstraint.activateConstraints_([
                overlay.leadingAnchor().constraintEqualToAnchor_(control.leadingAnchor()),
                overlay.trailingAnchor().constraintEqualToAnchor_(control.trailingAnchor()),
                overlay.topAnchor().constraintEqualToAnchor_(control.topAnchor()),
                overlay.bottomAnchor().constraintEqualToAnchor_(control.bottomAnchor()),
            ])
            container.heightAnchor().constraintEqualToConstant_(48).setActive_(True)
            control.setDelegate_(self.delegate)
            control.setAccessibilityLabel_(f"Digit {index + 1}")
            self.view.addArrangedSubview_(container)
            self.fields.append(control)

    @property
    def value(self):
        return "".join(control.stringValue() for control in self.fields)

    def clear(self):
        for control in self.fields:
            control.setStringValue_("")

    def focus(self, window):
        window.makeFirstResponder_(self.fields[0])

    def begin_editing(self, control):
        editor = control.currentEditor()
        if editor is None:
            return
        editor.setTextColor_(AppKit.NSColor.clearColor())
        editor.setInsertionPointColor_(theme.colour("ink"))
        font = control.font()
        inset = max(0.0, (editor.bounds().size.height - font.capHeight()) / 2 - (font.ascender() - font.capHeight()))
        editor.setTextContainerInset_(AppKit.NSMakeSize(0, inset))

    def end_editing(self, control):
        editor = getattr(control, "pair_editor", None)
        if editor is not None:
            editor.setTextContainerInset_(AppKit.NSMakeSize(0, 0))
            editor.setSelectedTextAttributes_(control.pair_selected_attributes)
            AppKit.NSNotificationCenter.defaultCenter().removeObserver_name_object_(
                control, AppKit.NSTextViewDidChangeSelectionNotification, editor)
            control.pair_editor = None
            control.setNeedsDisplay_(True)
            control.ink_view.setNeedsDisplay_(True)

    def changed(self, control):
        index = self.fields.index(control)
        digits = "".join(character for character in control.stringValue() if character.isdigit())
        if len(digits) > 1 and index == 0 or len(digits) >= len(self.fields):
            for position, control_at in enumerate(self.fields):
                control_at.setStringValue_(digits[position] if position < len(digits) else "")
            target = self.fields[min(len(digits), len(self.fields) - 1)]
        else:
            control.setStringValue_(digits[-1:] if digits else "")
            target = self.fields[index + 1] if digits and index + 1 < len(self.fields) else None
        if target is not None and control.window() is not None:
            control.window().makeFirstResponder_(target)

    def command(self, control, selector):
        index = self.fields.index(control)
        if selector == "insertNewline:":
            self.on_submit(control)
            return True
        if selector == "deleteBackward:" and not control.stringValue() and index > 0:
            previous = self.fields[index - 1]
            previous.setStringValue_("")
            control.window().makeFirstResponder_(previous)
            return True
        return False


class _PageList(AppKit.NSView):
    def keyDown_(self, event):
        keys = event.charactersIgnoringModifiers()
        arrows = {AppKit.NSUpArrowFunctionKey: -1, AppKit.NSDownArrowFunctionKey: 1}
        direction = arrows.get(keys[0]) if keys else None
        if direction is None:
            objc.super(_PageList, self).keyDown_(event)
        else:
            self.owner.step(direction)


class Sidebar:
    """The window's page list under a link block that stays in view whichever page is open: the
    LED, the state word in its tone and the peer. Each row is a focusable control, and while one
    has focus the up and down arrows move the selection. `pages` is (key, name, SF Symbol, ...)
    per row; `on_select` gets the key of a row the user chose. `footer_text`, if given, is a quiet
    line pinned to the foot of the sidebar, opening `on_footer` when clicked."""

    def __init__(self, pages, on_select, footer_text=None, footer_label=None, on_footer=None):
        self.on_select = on_select
        self.keys = [page[0] for page in pages]
        self.selected = None
        self.view = box("ground")
        column = stack(spacing=0)
        self.view.addSubview_(column)
        AppKit.NSLayoutConstraint.activateConstraints_([
            column.topAnchor().constraintEqualToAnchor_(self.view.topAnchor()),
            column.leadingAnchor().constraintEqualToAnchor_(self.view.leadingAnchor()),
            column.trailingAnchor().constraintEqualToAnchor_(self.view.trailingAnchor()),
        ])

        link = self.header = box()
        # The LED sits in a slot one word-line tall, so it centres on the first line when a long
        # state word wraps onto a second, and wide enough that its glow is not clipped square.
        slot = size(box(), 16, 20)
        self.led = LED()
        slot.addSubview_(self.led.view)
        AppKit.NSLayoutConstraint.activateConstraints_([
            self.led.view.centerXAnchor().constraintEqualToAnchor_(slot.centerXAnchor()),
            self.led.view.centerYAnchor().constraintEqualToAnchor_(slot.centerYAnchor()),
        ])
        self.word = Label("", theme.SIDEBAR_WORD, 700, wrap=True)
        self.peer = Label("", theme.TYPE["small"], mono=True, ink="ink_3")
        squeeze(self.peer.view)
        words = stack(spacing=3)
        self.link_words = words
        add(words, self.word.view)
        add(words, self.peer.view)
        line = stack(vertical=False, spacing=5)
        line.setAlignment_(AppKit.NSLayoutAttributeTop)
        line.addArrangedSubview_(slot)
        line.addArrangedSubview_(hug(words, AppKit.NSLayoutPriorityDefaultLow))
        link.addSubview_(line)
        pin(line, link, (18, 12, 18, 14))
        add(column, link)
        rule = add(column, hairline())
        column.setCustomSpacing_afterView_(12, rule)

        rows = _autolayout(_PageList.alloc().init())
        rows.owner = self
        list_stack = stack(spacing=2)
        rows.addSubview_(list_stack)
        pin(list_stack, rows)
        symbol_size = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(13, AppKit.NSFontWeightRegular)
        self.rows = {}
        self.row_insets = []
        self.row_widths = []
        self.dot_insets = []
        for index, (key, name, symbol, *_rest) in enumerate(pages):
            row = pressable(lambda key=key: self._choose(key), role=AppKit.NSAccessibilityRadioButtonRole)
            row.setAccessibilityLabel_(name)
            row.setToolTip_(f"{name}  ⌘{index + 1}")
            row.ring_inset = 2.0
            row.sidebar_owner = self
            row.heightAnchor().constraintEqualToConstant_(36).setActive_(True)
            row_width = row.widthAnchor().constraintEqualToAnchor_(self.view.widthAnchor())
            self.row_widths.append(row_width)
            icon = size(_autolayout(AppKit.NSImageView.alloc().init()), 18, 18)
            icon.setImage_(AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, None))
            icon.setSymbolConfiguration_(symbol_size)
            words = Label(name, theme.TYPE["body"], ink="ink_2")
            squeeze(words.view)
            dot = size(box(radius=3), 6, 6)
            dot.setHidden_(True)
            line = stack(vertical=False, spacing=10)
            line.addArrangedSubview_(icon)
            line.addArrangedSubview_(hug(words.view, AppKit.NSLayoutPriorityDefaultLow))
            row.addSubview_(line)
            row.addSubview_(dot)
            dot_inset = row.trailingAnchor().constraintEqualToAnchor_constant_(dot.trailingAnchor(), 8)
            self.dot_insets.append(dot_inset)
            AppKit.NSLayoutConstraint.activateConstraints_([
                dot_inset,
                dot.centerYAnchor().constraintEqualToAnchor_(row.centerYAnchor()),
            ])
            AppKit.NSLayoutConstraint.activateConstraints_([
                line.centerYAnchor().constraintEqualToAnchor_(row.centerYAnchor()),
                line.centerXAnchor().constraintEqualToAnchor_(row.centerXAnchor()),
            ])
            inset = line.widthAnchor().constraintEqualToAnchor_constant_(row.widthAnchor(), -32)
            inset.setActive_(True)
            self.row_insets.append(inset)
            bar = box()
            row.addSubview_(bar)
            AppKit.NSLayoutConstraint.activateConstraints_([
                bar.leadingAnchor().constraintEqualToAnchor_(row.leadingAnchor()),
                bar.topAnchor().constraintEqualToAnchor_(row.topAnchor()),
                bar.bottomAnchor().constraintEqualToAnchor_(row.bottomAnchor()),
                bar.widthAnchor().constraintEqualToConstant_(2),
            ])
            add(list_stack, row)
            self.rows[key] = (name, row, icon, words, bar, dot)
        add(column, rows)
        AppKit.NSLayoutConstraint.activateConstraints_(self.row_widths)

        if footer_text and on_footer is not None:
            footer = pressable(on_footer)
            footer.pointer_cursor = True
            footer.setAccessibilityLabel_(footer_label or footer_text)
            footer.setToolTip_(f'{footer_label or footer_text}\n{footer_text}')
            footer_label_view = Label(footer_text, theme.TYPE["small"], ink="ink_3", wrap=True)
            self.footer_words = footer_label_view
            footer.addSubview_(footer_label_view.view)
            # A slim right inset, with nothing beside the text: the address is 144pt of a 180pt
            # sidebar and wrapped onto a third line at the default window width at 14.
            pin(footer_label_view.view, footer, (8, 16, 8, 4))
            self.footer_icon = size(_autolayout(AppKit.NSImageView.alloc().init()), 18, 18)
            self.footer_icon.setImage_(AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_('arrow.up.right.square', footer_label))
            self.footer_icon.setContentTintColor_(theme.colour('ink_3'))
            self.footer_icon.setHidden_(True)
            footer.addSubview_(self.footer_icon)
            AppKit.NSLayoutConstraint.activateConstraints_([
                self.footer_icon.centerXAnchor().constraintEqualToAnchor_(footer.centerXAnchor()),
                self.footer_icon.centerYAnchor().constraintEqualToAnchor_(footer.centerYAnchor()),
                footer.heightAnchor().constraintGreaterThanOrEqualToConstant_(36),
            ])
            self.view.addSubview_(footer)
            AppKit.NSLayoutConstraint.activateConstraints_([
                footer.leadingAnchor().constraintEqualToAnchor_(self.view.leadingAnchor()),
                footer.trailingAnchor().constraintEqualToAnchor_(self.view.trailingAnchor()),
                footer.bottomAnchor().constraintEqualToAnchor_(self.view.bottomAnchor()),
            ])

    def select(self, key):
        self.selected = key
        for page, (_name, row, icon, words, bar, _dot) in self.rows.items():
            on = page == key
            paint(row, "well" if on else None)
            paint(bar, "signal" if on else None)
            words.set(ink="ink" if on else "ink_2", weight=600 if on else 400)
            icon.setContentTintColor_(theme.colour("ink" if on else "ink_3"))
            row.setAccessibilityValue_(1 if on else 0)
        window = self.view.window()
        responder = window.firstResponder() if window else None
        if window and responder in [values[1] for values in self.rows.values()] and getattr(window, 'keyboard_navigation', False):
            window.makeFirstResponder_(self.rows[key][1])
        self.refresh_focus()

    def refresh_focus(self):
        window = self.view.window()
        for key, (_name, row, *_rest) in self.rows.items():
            row._ring(bool(window and getattr(window, 'keyboard_navigation', False)
                           and key == self.selected and window.firstResponder() == row))

    def set_collapsed(self, collapsed):
        if collapsed == getattr(self, 'collapsed', None):
            return
        self.collapsed = collapsed
        self.link_words.setHidden_(collapsed)
        for (_name, _row, _icon, words, _bar, dot), inset, width, dot_inset in zip(
                self.rows.values(), self.row_insets, self.row_widths, self.dot_insets):
            words.view.setHidden_(collapsed)
            inset.setConstant_(-26 if collapsed else -32)
            width.setConstant_(-12 if collapsed else 0)
            dot_inset.setConstant_(0 if collapsed else 8)
        if hasattr(self, 'footer_words'):
            self.footer_words.view.setHidden_(collapsed)
            self.footer_icon.setHidden_(not collapsed)

    def step(self, direction):
        if self.view.window() is not None:
            self.view.window().keyboard_navigation = True
        index = min(max(self.keys.index(self.selected) + direction, 0), len(self.keys) - 1)
        key = self.keys[index]
        self._choose(key)
        row = self.rows[key][1]
        if row.window() is not None:
            row.window().makeFirstResponder_(row)
        self.refresh_focus()

    def _choose(self, key):
        self.select(key)
        self.on_select(key)

    def set_link(self, state, peer):
        self.led.set(state.led, state.blink)
        self.word.set(state.word, ink=state.tone)
        self.peer.set(peer)
        self.header.setAccessibilityLabel_(f'{state.word}, {peer}')
        self.header.setToolTip_(f'{state.word}, {peer}')

    def set_dots(self, marks):
        for page, (name, row, _icon, _words, _bar, dot) in self.rows.items():
            tone = marks.get(page)
            dot.setHidden_(tone is None)
            if tone is not None:
                paint(dot, tone)
            row.setAccessibilityLabel_(name if tone is None else f"{name}, needs attention")


class _Overlay(AppKit.NSView):
    """A view over its host that draws and takes no clicks: the ring's own layer tree. A layer
    added straight onto a stack view's backing layer is never drawn."""

    def hitTest_(self, _point):
        return None

    def isAccessibilityElement(self):
        return False

    def ringPositionChanged_(self, _notification):
        reference = getattr(self, "ring_owner", None)
        owner = reference() if reference is not None else None
        if owner is not None and owner.target is not None:
            owner.follow(owner.target)


class Ring:
    """The one ring round a group's chosen tile or chip. It lives over `host`, above what it rings,
    and slides from the old choice to the new. Tile layout and ancestor frame changes both
    carry the ring with a reflow."""

    def __init__(self, host, radius, inset=0.0):
        self.overlay = _autolayout(_Overlay.alloc().init())
        self.overlay.setWantsLayer_(True)
        host.addSubview_(self.overlay)
        pin(self.overlay, host)
        self.host = self.overlay
        self.inset = inset
        self.target = None
        self.enabled = True
        self.overlay.ring_owner = weakref.ref(self)
        self.layer = Quartz.CALayer.layer()
        self.layer.setBorderWidth_(2)
        self.layer.setCornerRadius_(radius)
        self.layer.setHidden_(True)
        self.overlay.layer().addSublayer_(self.layer)
        self.inner = Quartz.CALayer.layer()
        self.inner.setBorderWidth_(1)
        self.inner.setCornerRadius_(max(0, radius - 2))
        self.layer.addSublayer_(self.inner)

    def show(self, target, enabled=True):
        moving = self.target is not None and target is not None and target != self.target
        self.target, self.enabled = target, enabled
        centre = AppKit.NSNotificationCenter.defaultCenter()
        centre.removeObserver_name_object_(self.overlay, AppKit.NSViewFrameDidChangeNotification, None)
        if target is not None:
            target.ring_owner = self
            # A group's frame can move without changing its tile's own frame or layout.
            ancestor = target
            while ancestor is not None and ancestor != self.host.superview():
                ancestor.setPostsFrameChangedNotifications_(True)
                centre.addObserver_selector_name_object_(
                    self.overlay, "ringPositionChanged:", AppKit.NSViewFrameDidChangeNotification, ancestor
                )
                ancestor = ancestor.superview()
        motion.transaction(moving and motion.live(self.host))
        self.layer.setHidden_(target is None)
        if target is not None:
            self.layer.setFrame_(self._frame(target))
        theme.tint(self.layer, border="ink" if enabled else "rule")
        theme.tint(self.inner, border='ground')
        self.inner.setFrame_(AppKit.NSInsetRect(self.layer.bounds(), 2, 2))
        Quartz.CATransaction.commit()

    def follow(self, view):
        if view is not self.target:
            return
        motion.transaction(False)
        self.layer.setFrame_(self._frame(view))
        self.inner.setFrame_(AppKit.NSInsetRect(self.layer.bounds(), 2, 2))
        Quartz.CATransaction.commit()

    def _frame(self, target):
        rect = target.convertRect_toView_(target.bounds(), self.host)
        return AppKit.NSInsetRect(rect, self.inset, self.inset)


class ChoiceGrid:
    """Each family wraps within its own equal-width grid; partial rows keep that width."""
    def __init__(self, view, cells, owner, swatches=False):
        self.view, self.cells, self.owner = view, cells, owner
        self.swatches = swatches
        self.columns = None
        self.last_width = None
        self.height = view.heightAnchor().constraintEqualToConstant_(0)
        self.height.setActive_(True)
        for width in (0, 368, 568):
            self.arrange(width)

    def arrange(self, width):
        column = width + 32
        count = (5 if column >= 600 else 3 if column >= 400 else 2) if self.swatches else (3 if column >= 600 else 2 if column >= 400 else 1)
        count = min(count, len(self.cells))
        if width == self.last_width:
            return
        self.last_width = width
        self.columns = self.owner.columns = count
        gap = 8 if self.swatches else 12
        cell_width = max(0, (width - gap * (count - 1)) / count)
        cell_height = 68 if self.swatches else cell_width * 116 / 152 if self.owner.aspect else 116
        rows = math.ceil(len(self.cells) / count)
        height = rows * cell_height + (rows - 1) * gap
        self.height.setConstant_(height)
        for index, cell in enumerate(self.cells):
            row, column = divmod(index, count)
            cell.setFrame_(((column * (cell_width + gap), height - (row + 1) * cell_height - row * gap),
                            (cell_width, cell_height)))


class _TileContent:
    """Keep preview geometry out of the page's constraint engine during live resize."""
    def __init__(self, preview, name, detail):
        self.preview, self.name, self.detail = preview, name, detail
        self.last_size = None
        self.measurements = {}
        AppKit.NSLayoutConstraint.deactivateConstraints_([
            c for c in preview.constraints()
            if c.firstItem() == preview and c.secondItem() in (None, preview)
        ])

    def arrange(self, size):
        dimensions = (size.width, size.height)
        caption = tuple((label.text, label.size, label.weight) for label in (self.name, self.detail))
        if (dimensions, caption) == self.last_size:
            return
        self.last_size = (dimensions, caption)
        width, height = dimensions
        text_width = max(1, width - 16)
        key = (text_width, caption)
        heights = self.measurements.get(key)
        if heights is None:
            heights = [math.ceil(label.view.attributedStringValue().boundingRectWithSize_options_(
                (text_width, 1000), AppKit.NSStringDrawingUsesLineFragmentOrigin).size.height)
                       for label in (self.name, self.detail)]
            if len(self.measurements) >= 64:
                self.measurements.clear()
            self.measurements[key] = heights
        name_height, detail_height = heights
        preview_height = max(1, min(text_width / 2, height - name_height - detail_height - 26))
        preview_width = preview_height * 2
        self.preview.setFrame_(((width / 2 - preview_width / 2, height - 8 - preview_height),
                               (preview_width, preview_height)))
        self.preview.setNeedsLayout_(True)
        self.name.view.setFrame_(((8, height - 14 - preview_height - name_height),
                                 (text_width, name_height)))
        self.detail.view.setFrame_(((8, height - 18 - preview_height - name_height - detail_height),
                                   (text_width, detail_height)))
        self.name.view.setNeedsDisplay_(True)
        self.detail.view.setNeedsDisplay_(True)


class _SwatchContent:
    def __init__(self, chip, name):
        self.chip, self.name = chip, name
        self.last_size = None
        AppKit.NSLayoutConstraint.deactivateConstraints_(list(chip.constraints()))

    def arrange(self, size):
        dimensions = (size.width, size.height)
        if dimensions == self.last_size:
            return
        self.last_size = dimensions
        width, height = dimensions
        self.chip.setFrame_(((3, height - 31), (max(0, width - 6), 28)))
        self.chip.setNeedsLayout_(True)
        self.name.view.setFrame_(((3, 3), (max(0, width - 6), max(0, height - 39))))
        self.name.view.setNeedsDisplay_(True)


class ChoiceTiles:
    """A row or stack of preview cards. Style families use the full-width stacked form."""

    def __init__(self, choices, on_change=None, columns=None, tile_width=None, vertical=False, responsive=False, aspect=True):
        self.on_change = on_change
        self._value = None
        self.enabled = True
        self.tiles = []
        self.values = [choice[0] for choice in choices]
        self.previews = [choice[3] for choice in choices]
        self.vertical = vertical
        self.aspect = aspect
        self.view = _autolayout(_ResponsiveView.alloc().init()) if responsive else stack(vertical=vertical, spacing=8 if vertical else 10)
        if vertical and not responsive:
            self.view.setContentCompressionResistancePriority_forOrientation_(
                AppKit.NSLayoutPriorityDefaultLow, AppKit.NSLayoutConstraintOrientationHorizontal
            )
        if not responsive:
            self.view.setDistribution_(AppKit.NSStackViewDistributionFill if vertical
                                       else AppKit.NSStackViewDistributionFillEqually)
            self.view.setAlignment_(AppKit.NSLayoutAttributeWidth if vertical else AppKit.NSLayoutAttributeTop)
        for value, title, detail, preview in choices:
            tile = pressable(lambda value=value: self._choose(value), "ground", "rule", theme.RADIUS["segment"],
                             role=AppKit.NSAccessibilityRadioButtonRole)
            if vertical:
                tile.tile_selected = False
                tile.tile_hovered = False
                tile.set_tile_state()
                tile.setContentCompressionResistancePriority_forOrientation_(
                    AppKit.NSLayoutPriorityDefaultLow, AppKit.NSLayoutConstraintOrientationHorizontal
                )
            tile.setAccessibilityLabel_(title)
            tile.add_selection_mark()
            if responsive:
                name = Label(title, theme.TYPE["body"], 600, wrap=True, preview=True)
                words = Label(detail, theme.TYPE["small"], ink="ink_3", wrap=True, preview=True)
                for child in (preview, name.view, words.view):
                    tile.addSubview_(child)
                tile.tile_content = _TileContent(preview, name, words)
                self.view.addSubview_(tile)
                self.tiles.append((value, tile, name))
                continue
            column = stack(spacing=6)
            if vertical:
                column.setAlignment_(AppKit.NSLayoutAttributeWidth)
                column.setContentCompressionResistancePriority_forOrientation_(
                    AppKit.NSLayoutPriorityDefaultLow, AppKit.NSLayoutConstraintOrientationHorizontal
                )
            add(column, preview)
            if vertical:
                preview.setContentCompressionResistancePriority_forOrientation_(
                    AppKit.NSLayoutPriorityDefaultLow, AppKit.NSLayoutConstraintOrientationHorizontal
                )
            column.setCustomSpacing_afterView_(10, preview)
            name = Label(title, theme.TYPE["body"], 600, wrap=True)
            words = Label(detail, theme.TYPE["small"], ink="ink_3", wrap=True)
            # A narrow tile wraps its detail onto more lines; the row grows to fit rather than
            # squeezing the title or the last line out.
            for view in (name.view, words.view):
                view.setContentCompressionResistancePriority_forOrientation_(
                    AppKit.NSLayoutPriorityRequired, AppKit.NSLayoutConstraintOrientationVertical
                )
            add(column, name.view)
            add(column, words.view)
            tile.addSubview_(column)
            if vertical:
                self.view.addArrangedSubview_(tile)
                AppKit.NSLayoutConstraint.activateConstraints_([
                    tile.leadingAnchor().constraintEqualToAnchor_(self.view.leadingAnchor()),
                    tile.trailingAnchor().constraintEqualToAnchor_(self.view.trailingAnchor()),
                    column.topAnchor().constraintEqualToAnchor_constant_(tile.topAnchor(), 8),
                    column.leadingAnchor().constraintEqualToAnchor_constant_(tile.leadingAnchor(), 8),
                    tile.trailingAnchor().constraintEqualToAnchor_constant_(column.trailingAnchor(), 8),
                    tile.bottomAnchor().constraintEqualToAnchor_constant_(column.bottomAnchor(), 12),
                ])
            else:
                # Top-aligned, with the slack below so each preview and caption keep the same gap.
                AppKit.NSLayoutConstraint.activateConstraints_([
                    column.topAnchor().constraintEqualToAnchor_constant_(tile.topAnchor(), 8),
                    column.leadingAnchor().constraintEqualToAnchor_constant_(tile.leadingAnchor(), 8),
                    tile.trailingAnchor().constraintEqualToAnchor_constant_(column.trailingAnchor(), 8),
                    tile.bottomAnchor().constraintGreaterThanOrEqualToAnchor_constant_(column.bottomAnchor(), 12),
                ])
            if not vertical:
                self.view.addArrangedSubview_(tile)
                if tile_width is not None:
                    tile.widthAnchor().constraintEqualToConstant_(tile_width).setActive_(True)
                tile.heightAnchor().constraintEqualToAnchor_(self.view.heightAnchor()).setActive_(True)
            self.tiles.append((value, tile, name))
        if not vertical:
            _pad(self.view, columns, len(choices))
        self.ring = None if vertical else Ring(self.view, theme.RADIUS["segment"])
        self.ring_shared = False
        if responsive:
            self.grid = ChoiceGrid(self.view, [tile for _, tile, _ in self.tiles], self)
            self.view.owner = self.grid
            self.grid.arrange(640)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        self._value = value
        chosen_tile = None
        for candidate, tile, name in self.tiles:
            chosen = candidate == value
            tile.show_selection_mark(chosen)
            if chosen:
                chosen_tile = tile
            name.set(ink="ink" if self.enabled else "ink_3")
            tile.setAccessibilityValue_(1 if chosen else 0)
            if self.vertical:
                tile.set_tile_state(selected=chosen)
        if self.ring is not None and (chosen_tile is not None or not self.ring_shared):
            self.ring.show(chosen_tile, self.enabled)

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.view.setAlphaValue_(1.0 if enabled else 0.45)
        for _value, tile, _name in self.tiles:
            tile.enabled = enabled
        self.value = self._value

    def _choose(self, value):
        if value == self._value:
            return
        self.value = value
        if self.on_change is not None:
            self.on_change(value)


class _Chip(AppKit.NSView):
    ring_owner = None

    def layout(self):
        objc.super(_Chip, self).layout()
        gradient = getattr(self, "gradient", None)
        if gradient is not None:
            Quartz.CATransaction.begin()
            Quartz.CATransaction.setDisableActions_(True)
            gradient.setFrame_(self.bounds())
            Quartz.CATransaction.commit()
        owner = getattr(self, "ring_owner", None)
        if owner is not None:
            owner.follow(self)


class Swatches:
    """One colour of several: a chip of each palette's gradient over its name, the chosen chip ringed
    in ink. `.value` reads and writes the palette name. A choice is (name, title), coloured from
    theme.PALETTES, or (name, title, hex colours); `columns` pads as ChoiceTiles does."""

    def __init__(self, choices, on_change=None, columns=None, responsive=False):
        self.on_change = on_change
        self._value = None
        self.enabled = True
        self.cells = []
        self.values = [choice[0] for choice in choices]
        self.view = _autolayout(_ResponsiveView.alloc().init()) if responsive else stack(vertical=False, spacing=8)
        if not responsive:
            self.view.setDistribution_(AppKit.NSStackViewDistributionFillEqually)
        for choice in choices:
            value, title = choice[:2]
            cell = pressable(lambda value=value: self._choose(value), role=AppKit.NSAccessibilityRadioButtonRole)
            cell.setAccessibilityLabel_(title)
            cell.add_selection_mark()
            chip = size(box(None, "rule", theme.RADIUS["field"], cls=_Chip), height=28)
            chip.layer().setMasksToBounds_(True)
            colours = list(choice[2] if len(choice) > 2 else theme.PALETTES[value])
            if len(colours) == 1:
                colours *= 2
            gradient = Quartz.CAGradientLayer.layer()
            gradient.setColors_([AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(*theme.rgb(hex_value), 1.0).CGColor() for hex_value in colours])
            gradient.setStartPoint_((0.0, 0.5))
            gradient.setEndPoint_((1.0, 0.5))
            chip.layer().insertSublayer_atIndex_(gradient, 0)
            chip.gradient = gradient
            name = Label(title, theme.TYPE["small"], ink="ink_2", wrap=True,
                         align=AppKit.NSTextAlignmentCenter, preview=responsive)
            squeeze(name.view)
            if responsive:
                cell.addSubview_(chip)
                cell.addSubview_(name.view)
                cell.tile_content = _SwatchContent(chip, name)
                self.view.addSubview_(cell)
            else:
                column = stack(spacing=5)
                add(column, chip)
                add(column, name.view)
                cell.addSubview_(column)
                pin(column, cell, (3, 3, 3, 3))
                self.view.addArrangedSubview_(cell)
            self.cells.append((value, cell, chip, name))
        if not responsive:
            _pad(self.view, columns, len(choices))
        self.ring = Ring(self.view, theme.RADIUS["field"])
        self.ring_shared = False
        if responsive:
            self.grid = ChoiceGrid(self.view, [cell for _, cell, _, _ in self.cells], self, swatches=True)
            self.view.owner = self.grid
            self.grid.arrange(640)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        self._value = value
        chosen_chip = None
        for candidate, cell, chip, name in self.cells:
            chosen = candidate == value
            cell.show_selection_mark(chosen)
            if chosen:
                chosen_chip = chip
            name.set(ink="ink" if chosen else "ink_2", weight=600 if chosen else 400)
            cell.setAccessibilityValue_(1 if chosen else 0)
        if chosen_chip is not None or not self.ring_shared:
            self.ring.show(chosen_chip, self.enabled)

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.view.setAlphaValue_(1.0 if enabled else 0.45)
        for _value, cell, _chip, _name in self.cells:
            cell.enabled = enabled
        self.value = self._value

    def _choose(self, value):
        if value == self._value:
            return
        self.value = value
        if self.on_change is not None:
            self.on_change(value)


def _pad(row, columns, count):
    """Empty cells after the last choice, so a row of `count` sits in a grid `columns` wide."""
    for _ in range(max(0, (columns or count) - count)):
        row.addArrangedSubview_(_autolayout(AppKit.NSView.alloc().init()))


class Linked:
    """Several one-of-several rows (ChoiceTiles, Swatches) acting as one choice: choosing in any row
    clears the others. `.value`, `on_change` and `set_enabled` work as each row's do."""

    def __init__(self, rows, on_change=None, host=None):
        self.rows = list(rows)
        self.on_change = on_change
        self._value = None
        # With a host holding every row, one ring serves them all and slides between rows.
        self.ring = None
        if host is not None and self.rows and getattr(self.rows[0], "ring", None) is not None:
            first = self.rows[0].ring
            self.ring = Ring(host, first.layer.cornerRadius(), first.inset)
        for row in self.rows:
            row.on_change = self._chosen
            if self.ring is not None:
                if row.ring is not None:
                    row.ring.overlay.removeFromSuperview()
                row.ring = self.ring
                row.ring_shared = True

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        self._value = value
        for row in self.rows:
            row.value = value if value in row.values else None
        if self.ring is not None and not any(value in row.values for row in self.rows):
            self.ring.show(None)

    def set_enabled(self, enabled):
        for row in self.rows:
            row.set_enabled(enabled)

    def _chosen(self, value):
        self.value = value
        if self.on_change is not None:
            self.on_change(value)
