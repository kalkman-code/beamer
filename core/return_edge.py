"""Return-edge geometry and the pressure model for crossing back to the Mac.

Pure: no ctypes, no Qt, no Windows. The receiver feeds it the monitor
rectangles, the pointer position, each incoming movement delta and a clock, and it
answers with what to do — inject the delta, hold the pointer at the edge, or
cross. The model mirrors the Mac's so the two boundaries feel like one:
outward travel at the edge accumulates as pressure, inward travel subtracts,
time decays it to nothing over DECAY_SECONDS, and at `resistance_px` of net
outward travel the edge gives.
"""

import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Tuple

EDGES = ("left", "right", "top", "bottom")
OPPOSITE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
DEFAULT_RESISTANCE_PX = 120
DECAY_SECONDS = 0.4
# How near a corner counts as being in it. The Mac's own engine uses the same
# 8 pixels, and the two boundaries are meant to feel like one.
CORNER_PX = 8
CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")
# The thirds of an edge "Part of the edge" may use, top to bottom or left to right. Both machines'
# engines read them through in_parts, so a third means the same stretch on each.
PARTS = ("start", "middle", "end")


def part_of(fraction: float) -> str:
    """Which third of an edge `fraction` of the way along it falls in."""
    return PARTS[min(2, max(0, int(fraction * 3)))]


def in_parts(fraction: float, parts) -> bool:
    return part_of(fraction) in parts

PASS = "pass"
HOLD = "hold"
CROSS = "cross"


@dataclass(frozen=True)
class Rect:
    """A monitor or the virtual desktop, in virtual-desktop pixels. `x`/`y` may
    be negative: the primary monitor is at the origin, not the bounding box."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width - 1

    @property
    def bottom(self) -> int:
        return self.y + self.height - 1


def union(monitors: Iterable[Rect]) -> Rect:
    rects = list(monitors)
    x = min(r.x for r in rects)
    y = min(r.y for r in rects)
    return Rect(x, y, max(r.right for r in rects) - x + 1, max(r.bottom for r in rects) - y + 1)


@dataclass(frozen=True)
class Outcome:
    action: str
    pressure: float = 0.0
    position: Optional[Tuple[int, int]] = None
    edge: Optional[str] = None
    offset: Optional[float] = None


def _clamp(value, low, high):
    return max(low, min(high, value))


def _contains(rect: Rect, point: Tuple[int, int]) -> bool:
    return rect.x <= point[0] <= rect.right and rect.y <= point[1] <= rect.bottom


def edge_offset(bounds: Rect, edge: str, pointer: Tuple[int, int]) -> float:
    """Fraction along `edge`, top to bottom or left to right, against the
    bounding box — the convention the Mac measures its own departures in."""
    if edge in ("left", "right"):
        return _clamp((pointer[1] - bounds.y) / max(bounds.height, 1), 0.0, 1.0)
    return _clamp((pointer[0] - bounds.x) / max(bounds.width, 1), 0.0, 1.0)


def display_fraction(monitors: List[Rect], edge: str, pointer: Tuple[int, int]) -> float:
    """Fraction along `edge` of the display the pointer is on, which is how Part of the edge
    measures its thirds: against the bounding box, a short display beside a tall one could hold
    none of the middle third. The bounding box when the pointer is on no display."""
    home = next((m for m in monitors if _contains(m, pointer)), None) or union(monitors)
    return edge_offset(home, edge, pointer)


def owning_monitor(monitors: List[Rect], edge: str) -> Rect:
    """The monitor that reaches the bounding box on `edge`; ties go to the
    first enumerated, which on Windows is the primary."""
    if edge == "left":
        return min(monitors, key=lambda r: r.x)
    if edge == "right":
        return max(monitors, key=lambda r: r.right)
    if edge == "top":
        return min(monitors, key=lambda r: r.y)
    return max(monitors, key=lambda r: r.bottom)


def landing_monitor(monitors: List[Rect], edge: str, along: float) -> Rect:
    """The monitor a pointer arriving through `edge` at coordinate `along` (y for a side edge, x
    for the top or bottom) lands on: of those spanning `along`, the outermost on that edge, so two
    stacked monitors each take their own stretch of it; with none spanning it, a gap in a staggered
    arrangement, the nearest. Ties go to the first enumerated, which on Windows is the primary."""
    def span(r: Rect):
        return (r.y, r.bottom) if edge in ("left", "right") else (r.x, r.right)

    def outer(r: Rect):
        return {"left": r.x, "right": -r.right, "top": r.y, "bottom": -r.bottom}[edge]

    def distance(r: Rect):
        low, high = span(r)
        return max(low - along, along - (high - 1), 0)

    return min(monitors, key=lambda r: (distance(r), outer(r)))


def arrival_position(monitors: List[Rect], edge: str, offset: float) -> Tuple[int, int]:
    """Where a pointer arriving at `edge`, `offset` of the way along it, lands. The fraction is
    applied against the whole bounding box, then placed on the monitor `landing_monitor` picks, so
    no arrangement lands it in a gap or on the wrong one of two monitors sharing the edge."""
    bounds = union(monitors)
    offset = _clamp(float(offset), 0.0, 1.0)
    if edge in ("left", "right"):
        along = int(round(bounds.y + offset * bounds.height))
        owner = landing_monitor(monitors, edge, along)
        x = owner.x if edge == "left" else owner.right
        y = _clamp(along, owner.y, owner.bottom)
    else:
        along = int(round(bounds.x + offset * bounds.width))
        owner = landing_monitor(monitors, edge, along)
        y = owner.y if edge == "top" else owner.bottom
        x = _clamp(along, owner.x, owner.right)
    return x, y


class ReturnEdge:
    """Pressure against one edge of the Windows virtual desktop. Armed by the
    Mac on every switch (it sends `return_edge` and `resistance_px`), and
    disarmed once a crossing has been sent, so a burst of deltas still in
    flight after breakthrough cannot send a second one."""

    def __init__(self, edge: str, resistance_px: int = DEFAULT_RESISTANCE_PX, clock: Callable[[], float] = time.monotonic) -> None:
        if edge not in EDGES:
            raise ValueError(f"unknown edge {edge!r}")
        self.edge = edge
        self.resistance_px = max(0, int(resistance_px))
        self._clock = clock
        self._travel = 0.0
        self._last_at = clock()
        self.armed = True

    @property
    def pressure(self) -> float:
        if self.resistance_px == 0:
            return 1.0 if self._travel > 0 else 0.0
        return _clamp(self._travel / self.resistance_px, 0.0, 1.0)

    def reset(self) -> None:
        self._travel = 0.0
        self._last_at = self._clock()

    def _outward(self, dx: float, dy: float) -> float:
        if self.edge == "left":
            return -dx
        if self.edge == "right":
            return dx
        if self.edge == "top":
            return -dy
        return dy

    def _at_edge(self, monitors: List[Rect], pointer: Tuple[int, int]) -> bool:
        """On a monitor, with no monitor one pixel beyond it: the pointer is
        against a wall. Testing the bounding box instead made the edge of a
        shorter monitor in a staggered arrangement unreachable — for example,
        pushing down on a monitor whose bottom stops above its neighbour's
        could never reach the edge."""
        x, y = pointer
        step_x, step_y = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}[self.edge]
        beyond = (x + step_x, y + step_y)
        return any(_contains(m, pointer) for m in monitors) and not any(_contains(m, beyond) for m in monitors)

    def feed(self, monitors: List[Rect], pointer: Tuple[int, int], dx: float, dy: float) -> Outcome:
        bounds = union(monitors)
        now = self._clock()
        elapsed = max(0.0, now - self._last_at)
        self._last_at = now
        # Decay runs against the wall clock between deltas, pushing or not. A
        # brisk push outruns it; a slow lean on the edge to reach a menu never
        # accumulates, which is the whole point of the resistance.
        self._travel = max(0.0, self._travel - self.resistance_px * elapsed / DECAY_SECONDS)
        if not self.armed:
            return Outcome(PASS, self.pressure)
        outward = self._outward(dx, dy)
        if outward <= 0 or not self._at_edge(monitors, pointer):
            if outward < 0:
                self._travel = max(0.0, self._travel + outward)
            return Outcome(PASS, self.pressure)
        self._travel += outward
        if self._travel >= self.resistance_px:
            self.armed = False
            offset = edge_offset(bounds, self.edge, pointer)
            self._travel = 0.0
            return Outcome(CROSS, 1.0, edge=OPPOSITE[self.edge], offset=offset)
        x, y = pointer
        if self.edge in ("left", "right"):
            position = (x, _clamp(int(y + dy), bounds.y, bounds.bottom))
        else:
            position = (_clamp(int(x + dx), bounds.x, bounds.right), y)
        return Outcome(HOLD, self.pressure, position=position)


class PartEdge(ReturnEdge):
    """The edge, but only along the chosen thirds of it: the rest of the edge is a plain wall, so
    a menu or a dock at one end can sit against it without the pointer ever crossing there. The
    thirds are measured along the display the pointer is on, by `display_fraction`."""

    def __init__(self, edge: str, parts, resistance_px: int = DEFAULT_RESISTANCE_PX, clock=time.monotonic) -> None:
        super().__init__(edge, resistance_px, clock)
        self.parts = frozenset(part for part in parts if part in PARTS)

    def _at_edge(self, monitors: List[Rect], pointer: Tuple[int, int]) -> bool:
        return super()._at_edge(monitors, pointer) and in_parts(display_fraction(monitors, self.edge, pointer), self.parts)


class SpanEdge(ReturnEdge):
    """The edge, but only along a stretch of it, in the monitors' own coordinates: a Mac's notch,
    a fixed stretch of its top edge rather than a third of it. `span()` gives (start, end) or None
    and is read at every push, so a stretch measured late, or moved by a display change, applies
    at once."""

    def __init__(self, edge: str, span: Callable[[], Optional[Tuple[float, float]]], resistance_px: int = DEFAULT_RESISTANCE_PX, clock=time.monotonic) -> None:
        super().__init__(edge, resistance_px, clock)
        self.span = span

    def _at_edge(self, monitors: List[Rect], pointer: Tuple[int, int]) -> bool:
        span = self.span()
        along = pointer[0] if self.edge in ("top", "bottom") else pointer[1]
        return span is not None and super()._at_edge(monitors, pointer) and span[0] <= along <= span[1]


class CornerPush(ReturnEdge):
    """The same pressure model gated to one corner, for a machine whose whole
    edge is busy with something else. The push has to be diagonal: travel that
    leaves by only one of the corner's two sides is not a corner push and
    counts for nothing, which is what stops a slide along an edge triggering
    the corner it ends at.

    `edge` is still the side that leads to the other machine, so a corner
    crossing arrives where an edge crossing would, at the end of that edge the
    corner sits at."""

    def __init__(self, corner: str, edge: str, resistance_px: int = DEFAULT_RESISTANCE_PX, clock=time.monotonic) -> None:
        if corner not in CORNERS:
            raise ValueError(f"unknown corner {corner!r}")
        super().__init__(edge, resistance_px, clock)
        self.corner = corner
        self.vertical, self.horizontal = corner.split("_")

    def _outward(self, dx: float, dy: float) -> float:
        out_x = -dx if self.horizontal == "left" else dx
        out_y = -dy if self.vertical == "top" else dy
        if out_x > 0 and out_y > 0:
            return (out_x * out_x + out_y * out_y) ** 0.5
        inward = (min(0.0, out_x) ** 2 + min(0.0, out_y) ** 2) ** 0.5
        return -inward

    def _at_edge(self, monitors: List[Rect], pointer: Tuple[int, int]) -> bool:
        """In the corner's own box, and against at least one of its two walls.
        The box is measured on the monitor the pointer is on, for the same
        reason the edge test is: on a staggered arrangement the bounding box
        has corners no display reaches."""
        x, y = pointer
        home = next((m for m in monitors if _contains(m, pointer)), None)
        if home is None:
            return False
        near_x = x <= home.x + CORNER_PX if self.horizontal == "left" else x >= home.right - CORNER_PX
        near_y = y <= home.y + CORNER_PX if self.vertical == "top" else y >= home.bottom - CORNER_PX
        if not (near_x and near_y):
            return False
        return any(
            not any(_contains(m, beyond) for m in monitors)
            for beyond in ((x + (-1 if self.horizontal == "left" else 1), y), (x, y + (-1 if self.vertical == "top" else 1)))
        )
