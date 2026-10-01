"""The crossing engine: pointer pressure against the outer edge of the Mac desktop.

Pure arithmetic, no AppKit and no Quartz, so it runs under unittest on any machine. bridge.py
feeds it every local mouse movement while the crossing methods are armed and acts on what comes
back; kvm_bridge_app.py reads pressure for the glow. Coordinates are Quartz global points
(top-left origin, the space CGEventGetLocation and CGDisplayBounds share), and `bounds` is the
union of every display as (left, top, right, bottom) with right and bottom exclusive.

Pressure is measured in raw pointer delta units, the same "pixels of travel" the trackpad reports,
and decays at a rate that empties a full push in DECAY_S once the outward movement stops. That one
mechanism is what keeps a slow lean against the edge from ever building a switch.
"""

from dataclasses import dataclass

from core import return_edge

EDGES = ("left", "right", "top", "bottom")
CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")
METHODS = ("shortcut", "edge", "part", "corner", "notch")
NOTCH_STYLES = ("beam", "island")
HAPTIC_FEELS = ("light", "medium", "firm")
HAPTIC_STEPS = ("quarters", "halves", "breakthrough")
GLOW_STYLES = ("glow", "beam")
# The names of tokens.PALETTES, spelled out because this module stays importable without the root.
GLOW_COLOURS = ("signal", "colourful", "ocean", "sunset", "mono")
OPPOSITE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}

DEFAULT_CROSSING = {
    "methods": ["shortcut", "edge"],
    "edge": "right",
    "corner": "top_right",
    "resistance_px": 120,
    "haptics": True,
    "glow": True,
    "notch_style": "beam",
    "notch_after_ms": 1200,
    "haptic_feel": "medium",
    "haptic_steps": "quarters",
    "glow_style": "glow",
    "glow_colour": "signal",
    "block_while_dragging": True,
    # A full-screen app in front holds this Mac's edges. This Mac's own choice, never shared.
    "hold_full_screen": True,
    # "Part of the edge": the thirds of `edge` that cross, as return_edge.PARTS names them.
    "edge_parts": ["middle"],
    # A switch by the shortcut or a menu, not a crossing, plays an arrival around the pointer.
    "shortcut_arrival": True,
    # What that plays: "match" for whatever the crossing style plays, else see effects.SWITCH_STYLES.
    "shortcut_arrival_style": "match",
    # How long an effect takes to play through once the pointer crosses: see effects.LENGTHS.
    "effect_length": "normal",
    # Unix seconds at the moment this Mac last changed the arrangement -- which
    # edge leads to the PC. Either machine may change it, so when two ends meet
    # holding different answers the newer stamp wins. Never read by the engine;
    # it lives here so it is saved and loaded with the rest of the crossing.
    "arrangement_set_at": 0,
}

DECAY_S = 0.4
CORNER_PX = 8.0
# How close to the last pixel of the desktop counts as touching it. The cursor's reported
# location at an edge is fractional on a Retina display, so an exact comparison misses.
EDGE_TOLERANCE = 1.5
# Pressure below this passes events through untouched: a lean of a pixel or two per event
# would otherwise alternate between pinning and releasing the pointer at the trackpad's rate.
HOLD_MIN_PX = 3.0


def tick_fires(steps, pressure):
    """Whether a quarter tick is felt under `crossing.haptic_steps`, given the pressure it fired at:
    every quarter, only the one at halfway, or none, leaving just the thud at breakthrough."""
    if steps == "quarters":
        return True
    if steps == "halves":
        return min(3, int(4 * pressure + 1e-9)) == 2
    return False


@dataclass
class Step:
    """What the bridge does with one movement event. `hold` means swallow it and warp the
    pointer back to `pin`; `crossed` means hand input to Windows, arriving at `edge` (a Windows
    edge) at `offset` along it. `mac_edge` and `region` describe the strip being pushed, for
    the glow, and `via` names the method that owns that strip, so the notch can get its own
    feedback; `pressure` is the fraction of the way to breakthrough."""

    pressure: float = 0.0
    hold: bool = False
    pin: tuple = None
    tick: bool = False
    crossed: bool = False
    edge: str = None
    offset: float = None
    mac_edge: str = None
    region: tuple = None
    via: str = None


class CrossingEngine:
    def __init__(
        self,
        methods=("shortcut", "edge"),
        edge="right",
        corner="top_right",
        resistance_px=120,
        block_while_dragging=True,
        parts=("middle",),
    ):
        self.methods = frozenset(methods)
        self.edge = edge
        self.parts = frozenset(parts)
        self.corner = corner
        self.resistance_px = float(resistance_px)
        self.block_while_dragging = block_while_dragging
        self.reset()

    @classmethod
    def from_config(cls, crossing):
        return cls(
            methods=crossing["methods"],
            edge=crossing["edge"],
            corner=crossing["corner"],
            resistance_px=crossing["resistance_px"],
            block_while_dragging=crossing["block_while_dragging"],
            parts=crossing.get("edge_parts", ("middle",)),
        )

    @property
    def armed(self):
        return bool(self.methods & {"edge", "part", "corner", "notch"})

    def reset(self):
        self.touching = False
        self.pressure = 0.0
        self.pin = None
        self.mac_edge = None
        self.region = None
        self._quarter_reached = 0
        self._last_at = None
        self._last_x = None
        self._last_y = None

    def pressure_at(self, now):
        """Pressure as a fraction, decayed to `now` without recording anything — for the glow,
        which has to keep fading after the last event arrives."""
        if self.resistance_px <= 0 or self._last_at is None:
            return 0.0
        remaining = self.pressure - self._decay(now - self._last_at)
        return max(0.0, min(1.0, remaining / self.resistance_px))

    def feed(self, x, y, dx, dy, bounds, now, notch_range=None, dragging=False, displays=None):
        """`displays` is each display's (left, top, right, bottom), which Part of the edge measures
        its thirds along; without them it measures along `bounds`."""
        if self._last_at is not None:
            self.pressure = max(0.0, self.pressure - self._decay(now - self._last_at))
        self._last_at = now
        previous_x, previous_y = self._last_x, self._last_y
        self._last_x, self._last_y = x, y
        if dragging and self.block_while_dragging:
            self.touching = False
            return self._release()

        target = self._target(x, y, bounds, notch_range, displays)
        # Whether the pointer is at an armed region at all, whatever the pressure: a slow push
        # drains to nothing between events and releases, but is still against the edge.
        self.touching = target is not None
        if target is None:
            return self._release()
        mac_edge, region, diagonal, via = target
        outward, inward = self._push(mac_edge, diagonal, x, y, dx, dy, previous_x, previous_y, self._push_box)
        if via == "corner" and outward <= 0.0:
            # A straight push in the corner's box is still a push on the edge it sits on, as on the PC.
            edge_target = self._target(x, y, bounds, notch_range, displays, corner=False)
            if edge_target is not None:
                mac_edge, region, diagonal, via = edge_target
                outward, inward = self._push(mac_edge, diagonal, x, y, dx, dy, previous_x, previous_y, self._push_box)
        before = self.pressure
        self.pressure = max(0.0, self.pressure + outward - inward)
        if self.pressure <= 0.0:
            return self._release()
        if self.pin is None:
            self.pin = (x, y)
        self.mac_edge = mac_edge
        self.region = region

        if outward > 0.0 and self.pressure >= self.resistance_px:
            step = Step(
                pressure=1.0,
                crossed=True,
                pin=self.pin,
                edge=OPPOSITE[mac_edge],
                offset=self._offset(mac_edge, self.pin, bounds),
                mac_edge=mac_edge,
                region=region,
                via=via,
            )
            self.reset()
            return step
        # Ticks are counted against the highest quarter this push has reached, not the
        # previous reading: decay between two events can drop the reading back under a
        # boundary, and comparing readings would then fire the same tick twice.
        quarter = self._quarter(self.pressure)
        tick = quarter > self._quarter_reached
        self._quarter_reached = max(self._quarter_reached, quarter)
        return Step(
            pressure=self._fraction(self.pressure),
            hold=before >= HOLD_MIN_PX,
            pin=self.pin,
            tick=tick,
            mac_edge=mac_edge,
            region=region,
            via=via,
        )

    @staticmethod
    def arrival_point(edge, offset, bounds, inset=2.0, displays=None):
        """Where the pointer lands when input comes home through a Mac `edge`, `offset` of the
        way along it, a couple of points inside so the arrival itself is not already a push. With
        `displays`, on the display return_edge.landing_monitor picks, so a staggered or stacked
        arrangement never lands it in a gap or on the wrong display."""
        left, top, right, bottom = bounds
        offset = max(0.0, min(1.0, float(offset)))
        side = edge in ("left", "right")
        along = top + offset * (bottom - 1 - top) if side else left + offset * (right - 1 - left)
        if displays:
            rects = [return_edge.Rect(d[0], d[1], d[2] - d[0], d[3] - d[1]) for d in displays]
            owner = displays[rects.index(return_edge.landing_monitor(rects, edge, along))]
            left, top, right, bottom = owner
            along = min(max(along, top if side else left), (bottom if side else right) - 1)
        if side:
            return (left + inset if edge == "left" else right - 1 - inset, along)
        return (along, top + inset if edge == "top" else bottom - 1 - inset)

    def _decay(self, elapsed):
        if elapsed <= 0 or self.resistance_px <= 0:
            return 0.0
        return self.resistance_px * elapsed / DECAY_S

    def _release(self):
        self.pressure = 0.0
        self.pin = None
        self.mac_edge = None
        self.region = None
        self._quarter_reached = 0
        return Step()

    def _fraction(self, pressure):
        if self.resistance_px <= 0:
            return 0.0
        return max(0.0, min(1.0, pressure / self.resistance_px))

    def _quarter(self, pressure):
        if self.resistance_px <= 0:
            return 0
        return min(3, int(4 * pressure / self.resistance_px))

    def _target(self, x, y, bounds, notch_range, displays=None, corner=True):
        """Which armed region the pointer is touching, as (mac_edge, region, diagonal, method). The
        corner wins over the edge it sits on, since its box is inside that edge's strip."""
        left, top, right, bottom = bounds
        x_max, y_max = right - 1, bottom - 1
        at = {
            "left": x <= left + EDGE_TOLERANCE,
            "right": x >= x_max - EDGE_TOLERANCE,
            "top": y <= top + EDGE_TOLERANCE,
            "bottom": y >= y_max - EDGE_TOLERANCE,
        }
        own = next((d for d in displays or () if d[0] <= x < d[2] and d[1] <= y < d[3]), None)
        edge_box = bounds
        if own is not None and not at[self.edge] and self._exposed(self.edge, x, y, own, displays):
            # The edge of a display that stops short of the desktop's, with nothing beyond it: a
            # wall all the same, as on the PC. Against the whole desktop it could never be reached.
            at[self.edge] = True
            edge_box = own
        # What the push is measured against: the display's own edge when that is the wall.
        self._push_box = bounds
        if corner and "corner" in self.methods:
            vertical, horizontal = self.corner.split("_")
            near_x = x <= left + CORNER_PX if horizontal == "left" else x >= x_max - CORNER_PX
            near_y = y <= top + CORNER_PX if vertical == "top" else y >= y_max - CORNER_PX
            if near_x and near_y and (at[horizontal] or at[vertical]):
                box_x = left if horizontal == "left" else x_max - CORNER_PX + 1
                box_y = top if vertical == "top" else y_max - CORNER_PX + 1
                return horizontal, (box_x, box_y, CORNER_PX, CORNER_PX), vertical, "corner"
        if "edge" in self.methods and at[self.edge]:
            self._push_box = edge_box
            return self.edge, self._strip(self.edge, edge_box), None, "edge"
        if "part" in self.methods and at[self.edge]:
            # The thirds are the pointer's own display's, as the PC measures them: along the whole
            # desktop, a short display beside a tall one could hold none of the middle third.
            part = return_edge.part_of(self._offset(self.edge, (x, y), own or bounds))
            if part in self.parts:
                self._push_box = edge_box
                return self.edge, self._part_strip(self.edge, own or bounds, part), None, "edge"
        if "notch" in self.methods and notch_range is not None and at["top"]:
            notch_left, notch_right = notch_range
            if notch_left <= x <= notch_right:
                return "top", (notch_left, top, notch_right - notch_left, 1), None, "notch"
        return None

    @staticmethod
    def _exposed(edge, x, y, own, displays):
        """Whether the pointer is at `own`'s `edge` with no display just beyond it."""
        left, top, right, bottom = own
        near = {
            "left": x <= left + EDGE_TOLERANCE,
            "right": x >= right - 1 - EDGE_TOLERANCE,
            "top": y <= top + EDGE_TOLERANCE,
            "bottom": y >= bottom - 1 - EDGE_TOLERANCE,
        }[edge]
        if not near:
            return False
        beyond = {"left": (left - 1, y), "right": (right, y), "top": (x, top - 1), "bottom": (x, bottom)}[edge]
        return not any(d[0] <= beyond[0] < d[2] and d[1] <= beyond[1] < d[3] for d in displays)

    @classmethod
    def _part_strip(cls, edge, bounds, part):
        """The third of `edge`'s strip that `part` names, which is all the glow lights."""
        x, y, w, h = cls._strip(edge, bounds)
        index = return_edge.PARTS.index(part)
        if edge in ("left", "right"):
            return (x, y + h * index / 3.0, w, h / 3.0)
        return (x + w * index / 3.0, y, w / 3.0, h)

    @staticmethod
    def _strip(edge, bounds):
        left, top, right, bottom = bounds
        if edge == "left":
            return (left, top, 1, bottom - top)
        if edge == "right":
            return (right - 1, top, 1, bottom - top)
        if edge == "top":
            return (left, top, right - left, 1)
        return (left, bottom - 1, right - left, 1)

    def _push(self, mac_edge, diagonal, x, y, dx, dy, previous_x, previous_y, bounds):
        """Outward and inward travel for this event. Against a flat edge the OS has already
        clamped the location, so the delta itself is the push once the pointer is there; on the
        event that first reaches the edge only the overshoot past it counts, so a pointer that
        merely arrives at the edge does not start with a head of pressure it never earned."""
        left, top, right, bottom = bounds
        out_x = self._axis(mac_edge, x, dx, previous_x, left, right - 1)
        if diagonal is None:
            out = out_x if mac_edge in ("left", "right") else self._axis(mac_edge, y, dy, previous_y, top, bottom - 1)
            return max(0.0, out), max(0.0, -out)
        out_y = self._axis(diagonal, y, dy, previous_y, top, bottom - 1)
        if out_x > 0 and out_y > 0:
            return (out_x * out_x + out_y * out_y) ** 0.5, 0.0
        inward = (min(0.0, out_x) ** 2 + min(0.0, out_y) ** 2) ** 0.5
        return 0.0, inward

    @staticmethod
    def _axis(edge, position, delta, previous, low, high):
        """Signed outward travel along one axis, positive when moving off the desktop."""
        if edge in ("left", "top"):
            outward = -delta
            if previous is not None and previous > low + EDGE_TOLERANCE:
                outward = min(outward, low - (previous + delta))
        else:
            outward = delta
            if previous is not None and previous < high - EDGE_TOLERANCE:
                outward = min(outward, (previous + delta) - high)
        return outward

    @staticmethod
    def _offset(mac_edge, point, bounds):
        left, top, right, bottom = bounds
        x, y = point
        if mac_edge in ("left", "right"):
            span = bottom - 1 - top
            fraction = (y - top) / span if span > 0 else 0.0
        else:
            span = right - 1 - left
            fraction = (x - left) / span if span > 0 else 0.0
        return round(max(0.0, min(1.0, fraction)), 4)
