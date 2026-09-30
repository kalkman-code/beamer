"""The settings window's motion: short, eased changes that show what a setting did, and none at all
under Reduce Motion or while the window is not on screen, when a change lands at once."""

from __future__ import annotations

import AppKit
import Quartz
import objc

DURATION = 0.2
# Hiding runs as a fade and then the collapse, each this long, so the row has gone before the rows
# under it close the gap over where it was.
FADE_OUT = 0.1

_serials: dict = {}


def reduced():
    try:
        return bool(AppKit.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
    except Exception:
        return False


def live(view):
    """Whether a change to `view` should move: its window is on screen and motion is not reduced."""
    window = view.window() if view is not None else None
    return window is not None and bool(window.isVisible()) and not reduced()


def timing():
    return Quartz.CAMediaTimingFunction.functionWithControlPoints____(0.16, 1.0, 0.3, 1.0)


def transaction(animate, duration=DURATION):
    """Opens a CATransaction for layer changes: eased over `duration`, or with actions off so the
    change lands at once. Close it with Quartz.CATransaction.commit()."""
    Quartz.CATransaction.begin()
    if animate:
        Quartz.CATransaction.setAnimationDuration_(duration)
        Quartz.CATransaction.setAnimationTimingFunction_(timing())
    else:
        Quartz.CATransaction.setDisableActions_(True)


def _group(duration, changes, done=None):
    def run(context):
        context.setDuration_(duration)
        context.setTimingFunction_(timing())
        context.setAllowsImplicitAnimation_(True)
        changes()

    AppKit.NSAnimationContext.runAnimationGroup_completionHandler_(run, done)


def _relayout(view):
    window = view.window()
    if window is not None:
        window.contentView().layoutSubtreeIfNeeded()


def _now(changes):
    """Runs `changes` unanimated, even inside a group, and stops any animation of what it sets."""
    def run(context):
        context.setDuration_(0.0)
        context.setAllowsImplicitAnimation_(False)
        changes()

    AppKit.NSAnimationContext.runAnimationGroup_completionHandler_(run, None)


def _collapse(view):
    """Hides `view` inside a running group. On a scrolling page with a `floor` constraint (its
    height at least the constant), the page gives up the space as the rows slide, not at once:
    a page left shorter than its scroll offset has the clip view clamp in a single frame, dropping
    everything on screen by the difference while the rows are still moving."""
    scroll = view.enclosingScrollView()
    page = scroll.documentView() if scroll is not None else None
    floor = getattr(page, "floor", None)
    if floor is None:
        view.setHidden_(True)
        _relayout(view)
        return
    clip = scroll.contentView()
    height, origin = page.frame().size.height, clip.bounds().origin
    _now(lambda: floor.setConstant_(0.0))
    view.setHidden_(True)
    _relayout(view)
    natural = page.frame().size.height
    if natural >= height:
        return

    def hold():
        floor.setConstant_(height)
        _relayout(view)
        clip.scrollToPoint_(origin)
        scroll.reflectScrolledClipView_(clip)

    _now(hold)
    key = objc.pyobjc_id(page)
    serial = _serials.get(key, (0, None))[0] + 1
    _serials[key] = (serial, None)

    def eased():
        if _serials.get(key, (None,))[0] == serial:
            _now(lambda: floor.setConstant_(0.0))

    _group(DURATION, lambda: floor.animator().setConstant_(natural), eased)


def heading(view):
    """Whether `view` is hidden, or is on its way to being: during a hide the view is still shown
    for its fade, so isHidden() reads False until the fade is over."""
    target = _serials.get(objc.pyobjc_id(view), (0, None))[1]
    return bool(view.isHidden()) if target is None else target


def moving(view):
    return _serials.get(objc.pyobjc_id(view), (0, None))[1] is not None


def set_hidden(view, hidden, done=None):
    """Shows or hides a row of a stack view. Moving, a row fades and the rows below slide to close
    or open its space, so the page does not jump under the pointer. `done` runs once a hide has
    collapsed, or at once when there is nothing to move."""
    hidden = bool(hidden)
    key = objc.pyobjc_id(view)
    serial, target = _serials.get(key, (0, None))
    # Called again for where it is already going, as the window's refresh does several times a
    # second, it leaves a move in flight alone.
    if target == hidden or (target is None and bool(view.isHidden()) == hidden):
        if done is not None and target is None:
            done()
        return
    serial += 1
    _serials[key] = (serial, hidden)

    def settled():
        if _serials.get(key, (None,))[0] == serial:
            _serials[key] = (serial, None)

    if not live(view):
        view.setHidden_(hidden)
        view.setAlphaValue_(1.0)
        settled()
        if done is not None:
            done()
        return
    if hidden:
        def collapsed():
            if _serials.get(key, (None,))[0] == serial:
                view.setAlphaValue_(1.0)
                settled()
                if done is not None:
                    done()

        def collapse():
            if _serials.get(key, (None,))[0] != serial:
                return
            _group(DURATION, lambda: _collapse(view), collapsed)

        _group(FADE_OUT, lambda: view.animator().setAlphaValue_(0.0), collapse)
        return
    def reveal():
        view.setHidden_(False)
        view.setAlphaValue_(1.0)
        _relayout(view)

    _group(DURATION, reveal, settled)
    layer = view.layer()
    if layer is not None:
        # Laid out for the first time, a row slides in from wherever it last sat and grows its
        # insides out of a corner. It stays clear while the rows below open its space, and fades
        # in once it has all but landed, so only the opening is seen.
        fade = Quartz.CAKeyframeAnimation.animationWithKeyPath_("opacity")
        fade.setValues_([0.0, 0.0, 1.0])
        fade.setKeyTimes_([0.0, 0.5, 1.0])
        fade.setDuration_(DURATION * 1.5)
        fade.setTimingFunctions_([timing(), timing()])
        layer.addAnimation_forKey_(fade, "reveal")


def cross_fade(view):
    """Cross-fades whatever `view` draws next with what it draws now: call it just before the change."""
    if not live(view) or view.layer() is None:
        return
    fade = Quartz.CATransition.animation()
    fade.setType_(Quartz.kCATransitionFade)
    fade.setDuration_(DURATION)
    fade.setTimingFunction_(timing())
    view.layer().addAnimation_forKey_(fade, "cross-fade")


def fade_paint(view, changes):
    """Runs `changes` to a view's layer colours so the old colours fade into the new."""
    if not live(view):
        changes()
        return
    _group(DURATION * 0.75, changes)
