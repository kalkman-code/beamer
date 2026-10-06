"""The Crossing page's two drawings, in Core Animation layers so every change can move: the
arrangement, which shows where the other machine sits and what crosses, and the push strip, which shows how
far the pointer is pushed past the edge before it gives."""

from __future__ import annotations

import math
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


def _text_height(text, font, width):
    return AppKit.NSAttributedString.alloc().initWithString_attributes_(
        text, {AppKit.NSFontAttributeName: font}
    ).boundingRectWithSize_options_((max(1, width), 10000), AppKit.NSStringDrawingUsesLineFragmentOrigin).size.height


def _fit_text(layer, text, frame):
    x, y, width, height = frame
    layer.setString_(text)
    layer.setWrapped_(True)
    layer.setTruncationMode_(Quartz.kCATruncationNone)
    text_height = _text_height(text, layer.font(), width)
    layer.setFrame_(((x, y + max(0, (height - text_height) / 2)), (width, text_height)))


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
    """This Mac's screen in the middle, each placed machine's beside the side it is on, and this
    Mac lit where a way in crosses: the chosen machine's ways in the signal colour, the others'
    quieter, so the picture says which edge leads where."""

    # The screens take the space above the legend row (the key cap and the machines not placed),
    # which they would otherwise run into once neighbours push this Mac off the middle.
    HEIGHT = 210.0
    LEGEND = 36.0
    # A mark's colour by its tone in pages.diagram_marks. A third the chosen machine does not use
    # stays as a dim track while Part of the edge is on, so the gaps read as walls, not as nothing.
    # signal_dim all but vanishes on the dark ground, so the others' marks are signal held back.
    TONES = {"chosen": "signal", "other": ("signal", 0.45), "track": "edge"}

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
        self.this = _box("well", "edge", 4)
        self.this_label = _text_layer(theme.TYPE["small"], "ink_2")
        self.notch = _box("ground", None, 2)
        self.key = _box("well", "edge", theme.RADIUS["keycap"])
        # The shortcut as a key cap in the corner, and how it is pressed beside it: a legend, not a
        # sentence, since the note above the drawing already says it in words.
        self.key_label = _text_layer(theme.TYPE["small"], "ink", Quartz.kCAAlignmentCenter)
        self.key_label.setFont_(theme.font(theme.TYPE["small"], 600))
        self.key.addSublayer_(self.key_label)
        self.key_how = _text_layer(theme.TYPE["small"], "ink_3", Quartz.kCAAlignmentLeft)
        self.not_placed = _text_layer(theme.TYPE["small"], "ink_3", Quartz.kCAAlignmentRight)
        for layer in (self.this, self.this_label, self.notch, self.key, self.key_how, self.not_placed):
            root.addSublayer_(layer)
        # Made as machines and their marks first appear, by the machine's key, and dropped with it.
        self.screens = {}
        self.marks = {}
        self.machines = []
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
    def show(self, machines, key_text, has_notch, description, key_how="", shortcut=False):
        """Draws these machines, moving from the last drawing when on screen. Each machine is a dict
        with `key`, `label`, `side` ("" when not placed), its ways (`methods`, `parts`, `corner`) and
        whether it is `chosen`. `key_text` is the shortcut's key as its cap reads, `key_how` how it
        is pressed, `shortcut` whether it is on."""
        state = (tuple((m["key"], m["label"], m["side"], tuple(m["methods"]), tuple(m["parts"]), m["corner"], m["chosen"])
                       for m in machines), key_text, has_notch, key_how, shortcut)
        if state == self.state:
            return
        first = self.state is None
        self.state = state
        self.machines = [dict(machine) for machine in machines]
        self.setAccessibilityLabel_(description)
        self._draw(not first and motion.live(self))

    @objc.python_method
    def _screen(self, key):
        if key not in self.screens:
            screen = _box("panel", "rule", 4)
            label = _text_layer(theme.TYPE["small"], "ink_3")
            # Under this Mac's marks, which may sit at its edge beside a screen.
            for layer in (screen, label):
                self.layer().insertSublayer_below_(layer, self.this)
            self.screens[key] = (screen, label)
        return self.screens[key]

    @objc.python_method
    def _mark_layer(self, key):
        if key not in self.marks:
            layer = _box("signal", None, 2 if key[1] == "corner" else 1.5)
            self.layer().insertSublayer_above_(layer, self.notch)
            self.marks[key] = layer
        return self.marks[key]

    @objc.python_method
    def _draw(self, animate):
        _machines, key_text, has_notch, key_how, shortcut = self.state
        machines = self.machines
        bounds = self.bounds().size
        if bounds.width <= 0:
            return
        font = theme.font(theme.TYPE["small"], 500)
        width = min(bounds.width - 24, 24 + _text_width(key_text, theme.font(theme.TYPE["small"], 600)))
        how = _text_width(key_how, font) + 4
        stacked = bool(shortcut and width + 8 + how > bounds.width - 24)
        legend = self.LEGEND + (20 if stacked else 0)
        this, frames = pages.diagram_layout(bounds.width, bounds.height - legend, [pages.placement(m) for m in machines])
        # With one machine nothing is chosen between, and it looks as the one-machine drawing did.
        several = len(machines) > 1
        motion.transaction(animate)
        self.this.setFrame_(_rect(this))
        line = theme.TYPE["small"] + 4
        x, y, w, h = this
        _fit_text(self.this_label, "This Mac", (x + 6, y + (4 if has_notch else 0), w - 12, h))
        keys = {machine["key"] for machine in machines}
        for key in [key for key in self.screens if key not in keys]:
            for layer in self.screens.pop(key):
                layer.removeFromSuperlayer()
        for machine, frame in zip(machines, frames):
            screen, label = self._screen(machine["key"])
            strong = several and machine["chosen"]
            screen.setHidden_(frame is None)
            label.setHidden_(frame is None)
            if frame is None:
                continue
            theme.tint(screen, border="ink_3" if strong else "rule")
            theme.tint(label, foreground="ink" if strong else "ink_3")
            label.setFont_(theme.font(theme.TYPE["small"], 600 if strong else 500))
            screen.setFrame_(_rect(frame))
            fx, fy, fw, fh = frame
            _fit_text(label, machine["label"], (fx + 4, fy, fw - 8, fh))
        marks, notch = pages.diagram_marks(this, machines)
        for key in [key for key in self.marks if key not in marks]:
            self.marks.pop(key).removeFromSuperlayer()
            self.lit.pop(key, None)
        sides = {machine["key"]: machine["side"] for machine in machines}
        for key, (frame, tone) in marks.items():
            layer = self._mark_layer(key)
            if tone is not None:
                theme.tint(layer, background=self.TONES[tone])
            # The chosen machine's ways on top where two machines' marks meet.
            layer.setZPosition_(1.0 if tone in ("chosen", "track") else 0.0)
            self._mark(key, layer, frame, tone, sides[key[0]] if key[1] != "corner" else None, animate)
        self.notch.setFrame_(_rect(pages.notch_mark(this)))
        self.notch.setHidden_(not has_notch)
        lit_notch = has_notch and notch is not None
        self.notch.setBorderWidth_(1.5 if lit_notch else 0)
        theme.tint(self.notch, border=self.TONES[notch] if lit_notch else "signal",
                   background=("signal", 0.25 if notch == "chosen" else 0.12) if lit_notch else "ground")
        shortcut = shortcut and bool(key_text)
        self.key_label.setString_(key_text)
        self.key_how.setString_(key_how)
        top = bounds.height - legend
        self.key.setFrame_(((12, top), (width, 24)))
        self.key_label.setFrame_(((0, (24 - line) / 2), (width, line)))
        how_left = 12 if stacked else 12 + width + 8
        how_top = top + 26 if stacked else top + (24 - line) / 2
        self.key_how.setFrame_(((how_left, how_top), (min(how, bounds.width - 12 - how_left), line)))
        for layer in (self.key, self.key_how):
            layer.setOpacity_(1.0 if shortcut else 0.0)
        # The machines with no side, on the key cap's line, from wherever the legend ends.
        start = how_left + how + 16 if shortcut else 12
        self.not_placed.setString_(pages.not_placed(machines))
        self.not_placed.setFrame_(((min(start, bounds.width - 12), how_top), (max(0, bounds.width - 12 - start), line)))
        Quartz.CATransaction.commit()

    @objc.python_method
    def _mark(self, key, layer, frame, tone, side, animate):
        """Places a mark; switched on or newly lit while moving, it grows out from its middle as
        it fades in. A mark with no frame (its machine has no side) is not drawn."""
        on = tone is not None and frame is not None
        # The side is part of the state: moved to another side, a mark grows in afresh there, which
        # reads far better than a strip stretching across the screen from one side to the other.
        state = (on, tone, side)
        was = self.lit.get(key)
        self.lit[key] = state
        if frame is None:
            layer.setOpacity_(0.0)
            return
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
        self.minimum_height = self.heightAnchor().constraintEqualToConstant_(self.HEIGHT)
        self.minimum_height.setActive_(True)
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
        edge = max(round(width * self.EDGE_AT), 20 + _text_width("THIS MAC", self.this_label.font()))
        needed_height = max(self.HEIGHT, math.ceil(_text_height(
            self.other_name.upper(), self.other_label.font(), width - edge - 20)) + 8)
        if self.minimum_height.constant() != needed_height:
            self.minimum_height.setConstant_(needed_height)
        # The full 500 px stops short of the other machine's name, so the name is never under the push.
        room = max(0, width - edge - min(120, _text_width(self.other_name.upper(), self.other_label.font()) + 20))
        depth = room * self.fraction
        motion.transaction(animate)
        self.this.setFrame_(((0, 0), (edge, height)))
        self.edge.setFrame_(((edge - 1, 0), (2, height)))
        self.fill.setFrame_(((edge, 0), (depth, height)))
        self.pointer.setPosition_((edge + depth - 1, (height - 16) / 2))
        _fit_text(self.this_label, "THIS MAC", (10, 0, edge - 20, height))
        _fit_text(self.other_label, self.other_name.upper(), (edge + 10, 0, width - edge - 20, height))
        Quartz.CATransaction.commit()
