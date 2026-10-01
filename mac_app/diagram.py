"""The Crossing page's two drawings, in Core Animation layers so every change can move: the
arrangement, which shows where the other machine sits and what crosses, and the push strip, which shows how
far the pointer is pushed past the edge before it gives."""

from __future__ import annotations

import AppKit
import Quartz
import objc

import motion
import pages
import theme


def _rect(frame):
    x, y, w, h = frame
    return ((x, y), (w, h))


def _text_width(text, font):
    return AppKit.NSAttributedString.alloc().initWithString_attributes_(
        text, {AppKit.NSFontAttributeName: font}
    ).size().width


def _text_layer(size, ink, align=Quartz.kCAAlignmentCenter):
    layer = Quartz.CATextLayer.layer()
    layer.setFont_(theme.font(size, 500))
    layer.setFontSize_(size)
    theme.tint(layer, foreground=ink)
    layer.setAlignmentMode_(align)
    layer.setTruncationMode_(Quartz.kCATruncationEnd)
    layer.setContentsScale_(2.0)
    return layer


def _box(fill=None, stroke=None, radius=0.0):
    layer = Quartz.CALayer.layer()
    theme.tint(layer, background=fill, border=stroke)
    if stroke:
        layer.setBorderWidth_(1)
    layer.setCornerRadius_(radius)
    return layer


def _collapsed(frame, side):
    """`frame` shrunk to its middle along the side it runs down, where a mark grows from."""
    x, y, w, h = frame
    if side in ("left", "right"):
        return (x, y + h / 2, w, 0.0)
    return (x + w / 2, y, 0.0, h)


class ArrangementDiagram(AppKit.NSView):
    """This Mac's screen with the other machine's beside it on the chosen side, lit where a way in crosses."""

    HEIGHT = 186.0

    def isFlipped(self):
        return True

    @objc.python_method
    def setup(self):
        self.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.heightAnchor().constraintEqualToConstant_(self.HEIGHT).setActive_(True)
        self.setWantsLayer_(True)
        root = self.layer()
        theme.tint(root, background="ground", border="rule")
        root.setBorderWidth_(1)
        root.setCornerRadius_(3)
        self.other = _box("panel", "rule", 4)
        self.this = _box("well", "edge", 4)
        self.this_label = _text_layer(theme.TYPE["small"], "ink_2")
        self.other_label = _text_layer(theme.TYPE["small"], "ink_3")
        self.notch = _box("ground", None, 2)
        self.side = _box("signal", None, 1.5)
        self.thirds = {part: _box("signal", None, 1.5) for part in ("start", "middle", "end")}
        self.corner = _box("signal", None, 2)
        self.key = _box("well", "edge", theme.RADIUS["keycap"])
        # The shortcut as a key cap in the corner, and how it is pressed beside it: a legend, not a
        # sentence, since the note above the drawing already says it in words.
        self.key_label = _text_layer(theme.TYPE["small"], "ink", Quartz.kCAAlignmentCenter)
        self.key_label.setFont_(theme.font(theme.TYPE["small"], 600))
        self.key.addSublayer_(self.key_label)
        self.key_how = _text_layer(theme.TYPE["small"], "ink_3", Quartz.kCAAlignmentLeft)
        for layer in (self.other, self.this, self.this_label, self.other_label, self.notch, self.side,
                      *self.thirds.values(), self.corner, self.key, self.key_how):
            root.addSublayer_(layer)
        self.state = None
        self.lit = {}
        self.setAccessibilityElement_(True)
        self.setAccessibilityRole_(AppKit.NSAccessibilityImageRole)
        return self

    def layout(self):
        objc.super(ArrangementDiagram, self).layout()
        if self.state is not None:
            self._draw(False)

    @objc.python_method
    def show(self, side, methods, parts, corner, key_text, other_name, has_notch, description, key_how=""):
        """Draws the arrangement for these settings, moving from the last one when on screen.
        `key_text` is the shortcut's key as its cap reads, `key_how` how it is pressed."""
        state = (side, frozenset(methods), frozenset(parts), corner, key_text, other_name, has_notch, key_how)
        if state == self.state:
            return
        first = self.state is None
        self.state = state
        self.setAccessibilityLabel_(description)
        self._draw(not first and motion.live(self))

    @objc.python_method
    def _draw(self, animate):
        side, methods, parts, corner, key_text, other_name, has_notch, key_how = self.state
        bounds = self.bounds().size
        if bounds.width <= 0:
            return
        this, other = pages.arrangement(bounds.width, bounds.height, side)
        motion.transaction(animate)
        self.this.setFrame_(_rect(this))
        self.other.setFrame_(_rect(other))
        self.this_label.setString_("This Mac")
        self.other_label.setString_(other_name)
        line = theme.TYPE["small"] + 4
        for layer, (x, y, w, h) in ((self.this_label, this), (self.other_label, other)):
            layer.setFrame_(((x + 6, y + (h - line) / 2 + (4 if layer is self.this_label and has_notch else 0)), (w - 12, line)))
        self.notch.setFrame_(_rect(pages.notch_mark(this)))
        self.notch.setHidden_(not has_notch)
        lit_notch = has_notch and "notch" in methods
        self.notch.setBorderWidth_(1.5 if lit_notch else 0)
        theme.tint(self.notch, border="signal", background=("signal", 0.25) if lit_notch else "ground")
        part = "part" in methods
        self._mark("side", self.side, pages.side_mark(this, side), "edge" in methods, side, animate)
        for name, frame in pages.third_marks(this, side).items():
            layer = self.thirds[name]
            chosen = part and name in parts
            # Unchosen thirds stay as a dim track while Part of the edge is on, so the gaps read
            # as walls rather than as nothing.
            theme.tint(layer, background="signal" if chosen else "edge")
            self._mark(name, layer, frame, part, side, animate, lit=chosen)
        self._mark("corner", self.corner, pages.corner_mark(this, corner), "corner" in methods, None, animate)
        shortcut = "shortcut" in methods and bool(key_text)
        self.key_label.setString_(key_text)
        self.key_how.setString_(key_how)
        width = min(bounds.width - 20, 24 + _text_width(key_text, theme.font(theme.TYPE["small"], 600)))
        top = bounds.height - 36
        self.key.setFrame_(((12, top), (width, 24)))
        self.key_label.setFrame_(((0, (24 - line) / 2), (width, line)))
        self.key_how.setFrame_(((12 + width + 8, top + (24 - line) / 2), (max(0, bounds.width - width - 32), line)))
        for layer in (self.key, self.key_how):
            layer.setOpacity_(1.0 if shortcut else 0.0)
        Quartz.CATransaction.commit()

    @objc.python_method
    def _mark(self, key, layer, frame, on, side, animate, lit=None):
        """Places a mark; switched on or newly lit while moving, it grows out from its middle as
        it fades in."""
        # The side is part of the state: moved to another side, a mark grows in afresh there, which
        # reads far better than a strip stretching across the screen from one side to the other.
        state = (on, on if lit is None else lit, self.state[0])
        was = self.lit.get(key)
        self.lit[key] = state
        if not (on and was != state and animate):
            layer.setFrame_(_rect(frame))
            layer.setOpacity_(1.0 if on else 0.0)
            return
        # Explicit animations from the collapsed mark: an implicit one would start from wherever
        # the mark was drawn before, inside this same transaction.
        start = _collapsed(frame, side) if side else (frame[0] + frame[2] / 2, frame[1] + frame[3] / 2, 0, 0)
        motion.transaction(False)
        layer.setFrame_(_rect(frame))
        layer.setOpacity_(1.0)
        Quartz.CATransaction.commit()
        x, y, w, h = frame
        sx, sy, sw, sh = start
        for key, old, new in (
            ("bounds", AppKit.NSValue.valueWithRect_(((0, 0), (sw, sh))), AppKit.NSValue.valueWithRect_(((0, 0), (w, h)))),
            ("position", AppKit.NSValue.valueWithPoint_((sx + sw / 2, sy + sh / 2)), AppKit.NSValue.valueWithPoint_((x + w / 2, y + h / 2))),
            ("opacity", 0.0, 1.0),
        ):
            grow = Quartz.CABasicAnimation.animationWithKeyPath_(key)
            grow.setFromValue_(old)
            grow.setToValue_(new)
            grow.setDuration_(motion.DURATION)
            grow.setTimingFunction_(motion.timing())
            layer.addAnimation_forKey_(grow, key)


class PushStrip(AppKit.NSView):
    """The last stretch of this Mac's screen, the edge, and the other machine beyond it, with the push past the
    edge drawn in: its depth is the resistance, out of the ruler's 500 px."""

    HEIGHT = 44.0
    EDGE_AT = 0.36
    REST = 0.55

    def isFlipped(self):
        return True

    @objc.python_method
    def setup(self):
        self.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.heightAnchor().constraintEqualToConstant_(self.HEIGHT).setActive_(True)
        self.setWantsLayer_(True)
        root = self.layer()
        root.setCornerRadius_(3)
        root.setMasksToBounds_(True)
        theme.tint(root, background="ground", border="rule")
        root.setBorderWidth_(1)
        self.this = _box("well")
        self.fill = Quartz.CAGradientLayer.layer()
        theme.tint(self.fill, colours=[("signal", 0.9), ("signal", 0.15)])
        self.fill.setStartPoint_((0.0, 0.5))
        self.fill.setEndPoint_((1.0, 0.5))
        self.fill.setOpacity_(self.REST)
        self.edge = _box("ink_3")
        self.pointer = Quartz.CAShapeLayer.layer()
        path = Quartz.CGPathCreateMutable()
        for index, (x, y) in enumerate(((0, 0), (0, 14), (3.6, 10.6), (6.2, 16), (8.2, 15), (5.7, 9.8), (10, 9.8))):
            (Quartz.CGPathMoveToPoint if index == 0 else Quartz.CGPathAddLineToPoint)(path, None, x, y)
        Quartz.CGPathCloseSubpath(path)
        self.pointer.setPath_(path)
        theme.tint(self.pointer, fill="ink", stroke="ground")
        self.pointer.setLineWidth_(1)
        self.pointer.setBounds_(((0, 0), (10, 16)))
        self.pointer.setAnchorPoint_((0.0, 0.0))
        self.this_label = _text_layer(theme.TYPE["eyebrow"], "ink_3", Quartz.kCAAlignmentLeft)
        self.other_label = _text_layer(theme.TYPE["eyebrow"], "ink_3", Quartz.kCAAlignmentRight)
        for layer in (self.this, self.fill, self.edge, self.this_label, self.other_label, self.pointer):
            root.addSublayer_(layer)
        self.fraction = None
        self.other_name = "Other machine"
        self.setAccessibilityElement_(False)
        return self

    def layout(self):
        objc.super(PushStrip, self).layout()
        if self.fraction is not None:
            self._draw(False)

    @objc.python_method
    def show(self, fraction, other_name="Other machine"):
        fraction = min(1.0, max(0.0, float(fraction)))
        if fraction == self.fraction and other_name == self.other_name:
            return
        moving = self.fraction is not None and motion.live(self)
        self.fraction, self.other_name = fraction, other_name
        self._draw(moving)
        if moving and fraction > 0:
            # The push brightens as it moves, then settles back.
            pulse = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
            pulse.setFromValue_(1.0)
            pulse.setToValue_(self.REST)
            pulse.setDuration_(0.6)
            pulse.setTimingFunction_(motion.timing())
            self.fill.addAnimation_forKey_(pulse, "pulse")

    @objc.python_method
    def _draw(self, animate):
        width, height = self.bounds().size.width, self.bounds().size.height
        if width <= 0:
            return
        edge = round(width * self.EDGE_AT)
        # The full 500 px stops short of the other machine's name, so the name is never under the push.
        room = width - edge - 120
        depth = room * self.fraction
        motion.transaction(animate)
        self.this.setFrame_(((0, 0), (edge, height)))
        self.edge.setFrame_(((edge - 1, 0), (2, height)))
        self.fill.setFrame_(((edge, 0), (depth, height)))
        self.pointer.setPosition_((edge + depth - 1, (height - 16) / 2))
        line = theme.TYPE["eyebrow"] + 4
        self.this_label.setString_("THIS MAC")
        self.other_label.setString_(self.other_name.upper())
        self.this_label.setFrame_(((10, (height - line) / 2), (edge - 20, line)))
        self.other_label.setFrame_(((edge + 10, (height - line) / 2), (width - edge - 20, line)))
        Quartz.CATransaction.commit()
