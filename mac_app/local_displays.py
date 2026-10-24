"""Crossings between this Mac's own displays. macOS moves the pointer across them by itself, so the
crossing engine, which only watches the edge that leads to another machine, never sees it. This
watches the pointer and, when it slides from one display onto another, says where it left and where
it landed, so the overlay can play the chosen effect's departure on one and its arrival on the other.

Geometry is Quartz global points, (x, y, w, h) with the origin at the primary display's top left.
Main thread only.
"""

import math

import AppKit

import desktop_mac

OPPOSITE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
# Two movement events further apart than this are a warp, as an arrival from another machine or a
# switch makes, not the pointer sliding over a shared edge.
MAX_STEP_PX = 400.0
MOVED = (AppKit.NSEventMaskMouseMoved | AppKit.NSEventMaskLeftMouseDragged
         | AppKit.NSEventMaskRightMouseDragged | AppKit.NSEventMaskOtherMouseDragged)


def display_index(point, displays):
    x, y = point
    for index, (dx, dy, dw, dh) in enumerate(displays):
        if dx <= x < dx + dw and dy <= y < dy + dh:
            return index
    return None


def crossing_between(previous, point, displays):
    """(exit_point, exit_edge, entry_point, entry_edge) when the step from `previous` to `point`
    moves the pointer onto another display, else None. The exit point is where the step meets the
    edge of the display it left; through a corner the sideways edge wins."""
    if previous is None:
        return None
    left_from, landed_on = display_index(previous, displays), display_index(point, displays)
    if left_from is None or landed_on is None or left_from == landed_on:
        return None
    if math.dist(previous, point) > MAX_STEP_PX:
        return None
    x, y = point
    dx, dy, dw, dh = displays[left_from]
    if x >= dx + dw:
        edge = "right"
    elif x < dx:
        edge = "left"
    elif y >= dy + dh:
        edge = "bottom"
    else:
        edge = "top"
    exit_point = (min(max(x, dx), dx + dw - 1), min(max(y, dy), dy + dh - 1))
    return exit_point, edge, (x, y), OPPOSITE[edge]


class LocalDisplayWatcher:
    """Calls `on_cross(exit_point, exit_edge, entry_point, entry_edge)` for every slide between two
    of this Mac's displays while `enabled()` is true. One global monitor for other apps' windows and
    one local for Beamer's own, as NSEvent splits them."""

    def __init__(self, on_cross, enabled, logger):
        self.on_cross = on_cross
        self.enabled = enabled
        self.logger = logger
        self.previous = None
        self.displays = []
        self.monitors = []
        self.observer = None

    def start(self):
        self._read_displays()
        self.monitors = [
            AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(MOVED, self._moved),
            AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(MOVED, self._moved_local),
        ]
        self.observer = AppKit.NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            AppKit.NSApplicationDidChangeScreenParametersNotification, None,
            AppKit.NSOperationQueue.mainQueue(), lambda _note: self._read_displays(),
        )

    def _read_displays(self):
        self.displays = [(r.x, r.y, r.width, r.height) for r in desktop_mac.monitors()]
        self.previous = None

    def _moved_local(self, event):
        self._moved(event)
        return event

    def _moved(self, _event):
        try:
            if not self.enabled() or len(self.displays) < 2:
                self.previous = None
                return
            point = desktop_mac.cursor_position()
            found = crossing_between(self.previous, point, self.displays)
            self.previous = point
            if found is not None:
                self.on_cross(*found)
        except Exception:
            self.logger.exception("local display crossing failed")
