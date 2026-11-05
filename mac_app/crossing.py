"""The crossing engine: pointer pressure against the outer edge of the Mac desktop.

Pure arithmetic, no AppKit and no Quartz, so it runs under unittest on any machine. bridge.py
feeds it every local mouse movement while the crossing methods are armed and acts on what comes
back; kvm_bridge_app.py reads pressure for the glow. Coordinates are Quartz global points
(top-left origin, the space CGEventGetLocation and CGDisplayBounds share), and `bounds` is the
union of every display as (left, top, right, bottom) with right and bottom exclusive.

Pressure is measured in raw pointer delta units, the same "pixels of travel" the trackpad reports,
and decays at a fixed rate so Resistance changes required travel without scaling the sustained-speed
threshold. That mechanism is what keeps a slow lean against the edge from ever building a switch.
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
    "shortcut_goes_to": "last",
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
    "glow_colour": "colourful",
    "block_while_dragging": True,
    # A full-screen app in front holds this Mac's edges. This Mac's own choice, never shared.
    "hold_full_screen": False,
    # Play the crossing effect when the pointer slides between this Mac's own displays. This Mac's
    # own choice, never shared.
    "local_displays": False,
    # "Part of the edge": the thirds of `edge` that cross, as return_edge.PARTS names them.
    "edge_parts": ["middle"],
    # A switch by the shortcut or a menu, not a crossing, plays an arrival around the pointer.
    "shortcut_arrival": True,
    # What that plays: "match" for whatever the crossing style plays, else see effects.SWITCH_STYLES.
    "shortcut_arrival_style": "match",
    # How long an effect takes to play through once the pointer crosses: see effects.LENGTHS.
    "effect_length": "normal",
    "effect_size": "medium",
    "effect_pace": 1.0,
    "effect_size_fraction": 0.045,
    "frame_rate": "display",
    # Unix seconds at the moment this Mac last changed the arrangement -- which
    # edge leads to the PC. Either machine may change it, so when two ends meet
    # holding different answers the newer stamp wins. Never read by the engine;
    # it lives here so it is saved and loaded with the rest of the crossing.
    "arrangement_set_at": 0,
}

DECAY_S = 0.4
DECAY_PX_PER_SECOND = 300.0
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
    pointer back to `pin`; `crossed` means hand input to `peer`, the machine the zone leads to,
    arriving at `edge` (that machine's edge) at `offset` along it. `mac_edge` and `region` describe
    the strip being pushed, for the glow, and `via` names the method that owns that strip, so the
    notch can get its own feedback; `pressure` is the fraction of the way to breakthrough."""

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
    peer: str = None
    display: str = None
    side: str = None


@dataclass(frozen=True)
class Ways:
    """One machine's zones on this Mac (WIRE.md section 8): the kinds in use, the side it is on, the
    thirds of that side and the corner. `peer` is its id, None for an engine built the 1.4.x way."""

    peer: str
    methods: frozenset
    edge: str
    parts: frozenset
    corner: str
    display: str = None
    target_display: str = None
    target_side: str = None
    contact_span: tuple = None
    target_span: tuple = None


class CrossingEngine:
    def __init__(
        self,
        methods=("shortcut", "edge"),
        edge="right",
        corner="top_right",
        resistance_px=120,
        block_while_dragging=True,
        parts=("middle",),
        ways=None,
    ):
        if ways is None:
            ways = [Ways(None, frozenset(methods), edge, frozenset(parts), corner)]
        self.ways = list(ways)
        self.resistance_px = float(resistance_px)
        self.block_while_dragging = block_while_dragging
        # Outside reset(): a click while the pointer lands must not end the grace (keep_grace for a rebuild).
        self._grace_inward = dict.fromkeys(EDGES, 0.0)
        self._grace_at = None
        self._seam_overshoot = False
        self._seam_pin = None
        self._seam_previous = None
        self.reset()

    def start_grace(self, now):
        """The pointer has just landed here from another machine: for ARRIVAL_GRACE_SECONDS no zone
        builds pressure until the pointer has moved ARRIVAL_GRACE_PX back from its edge, as on the PC.
        The hand still moving from the push that brought it here otherwise crossed the zone beside the
        landing, 14 ms after it (rc14, 10-10-2026)."""
        self._grace_inward = dict.fromkeys(EDGES, 0.0)
        self._grace_at = now

    def keep_grace(self, previous):
        """An engine built to replace `previous` (a link up, a settings save) keeps its grace."""
        self._grace_inward = dict(previous._grace_inward)
        self._grace_at = previous._grace_at

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

    @classmethod
    def from_zones(cls, crossing, zones, peers):
        """An engine for every zone in use that leads to one of `peers` (settings entries, each with
        its `id` and `side`): the machines this Mac can send to now. A machine with no side keeps its
        corner and the notch, which do not need one."""
        sides = {entry.get("id"): entry.get("side") for entry in peers
                 if entry.get("id") and entry.get("in_use", True) is True}
        found = {}
        for zone in zones:
            peer = zone.get("peer")
            if zone.get("off") is True or peer not in sides:
                continue
            display = zone.get("display")
            edge = zone.get("side", sides[peer])
            corner_key = zone.get("corner") if zone.get("kind") == "corner" else None
            contact_span = return_edge._normalised_span(zone.get("contact_span"))
            target_span = return_edge._normalised_span(zone.get("target_span"))
            key = (peer, display, edge, corner_key, zone.get("target_display"), contact_span, target_span)
            held = found.setdefault(key, {"methods": set(), "parts": ("middle",), "corner": "top_right",
                                          "target_display": zone.get("target_display"),
                                          "target_side": zone.get("target_side"),
                                          "contact_span": zone.get("contact_span"),
                                          "target_span": zone.get("target_span")})
            kind = zone.get("kind")
            if kind in ("edge", "part") and edge not in EDGES:
                continue
            if kind == "part":
                held["parts"] = tuple(part for part in zone.get("parts") or () if part in return_edge.PARTS)
            elif kind == "corner":
                if zone.get("corner") not in CORNERS:
                    continue
                held["corner"] = zone["corner"]
            elif kind not in ("edge", "notch"):
                continue
            held["methods"].add(kind)
        ways = [Ways(peer, frozenset(held["methods"]), edge if edge in EDGES else "right",
                     frozenset(held["parts"]), held["corner"], display, held["target_display"],
                     held["target_side"], return_edge._normalised_span(held["contact_span"]),
                     return_edge._normalised_span(held["target_span"]))
                for (peer, display, edge, _corner, _target_display, _contact_span, _target_span), held
                in found.items() if held["methods"]]
        return cls(resistance_px=crossing["resistance_px"], block_while_dragging=crossing["block_while_dragging"],
                   ways=ways)

    @property
    def armed(self):
        return any(way.methods & {"edge", "part", "corner", "notch"} for way in self.ways)

    @property
    def peers(self):
        """The machines this engine's zones lead to."""
        return {way.peer for way in self.ways if way.methods}

    # The one machine's settings, for an engine built the 1.4.x way.
    @property
    def methods(self):
        return self.ways[0].methods if self.ways else frozenset()

    @property
    def edge(self):
        return self.ways[0].edge if self.ways else "right"

    @property
    def parts(self):
        return self.ways[0].parts if self.ways else frozenset()

    @property
    def corner(self):
        return self.ways[0].corner if self.ways else "top_right"

    def reset(self):
        self.touching = False
        self.pressure = 0.0
        self.pin = None
        self.mac_edge = None
        self.region = None
        self._quarter_reached = 0
        self._peer = None
        self._selected_way = None
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

    def feed(self, x, y, dx, dy, bounds, now, notch_range=None, dragging=False, displays=None, ready=None):
        """`displays` is each display's (left, top, right, bottom), which Part of the edge measures
        its thirds along; without them it measures along `bounds`. `ready(peer)` says whether a
        machine can take input now: one that cannot has no wall."""
        if self._last_at is not None:
            self.pressure = max(0.0, self.pressure - self._decay(now - self._last_at))
        self._last_at = now
        previous_x, previous_y = self._last_x, self._last_y
        self._last_x, self._last_y = x, y
        if self._grace_at is not None:
            if now - self._grace_at >= return_edge.ARRIVAL_GRACE_SECONDS:
                self._grace_at = None
            else:
                for edge, inward in (("left", dx), ("right", -dx), ("top", dy), ("bottom", -dy)):
                    if inward > 0:
                        self._grace_inward[edge] += inward
        if dragging and self.block_while_dragging:
            self.touching = False
            return self._release()

        self._seam_overshoot = False
        self._seam_pin = None
        target = self._target(x, y, bounds, notch_range, displays, ready=ready,
                              previous=(previous_x, previous_y), delta=(dx, dy))
        # Whether the pointer is at an armed region at all, whatever the pressure: a slow push
        # drains to nothing between events and releases, but is still against the edge.
        self.touching = target is not None
        if target is None:
            return self._release()
        mac_edge, region, diagonal, via, peer = target
        if self._seam_overshoot and previous_x is None and self._seam_previous is not None:
            previous_x, previous_y = self._seam_previous
        if via == "corner" and self._selected_way and self._selected_way.display is not None:
            vertical, horizontal = self._selected_way.corner.split("_")
            out_x = -dx if horizontal == "left" else dx
            out_y = -dy if vertical == "top" else dy
            outward = (out_x * out_x + out_y * out_y) ** 0.5 if out_x > 0 and out_y > 0 else 0.0
            inward = (min(0.0, out_x) ** 2 + min(0.0, out_y) ** 2) ** 0.5
        else:
            outward, inward = self._push(mac_edge, diagonal, x, y, dx, dy,
                                         previous_x, previous_y, self._push_box)
        if via == "corner" and outward <= 0.0:
            # A straight push in the corner's box is still a push on the edge it sits on, as on the PC.
            edge_target = self._target(x, y, bounds, notch_range, displays, corner=False, ready=ready,
                                       previous=(previous_x, previous_y))
            if edge_target is not None:
                mac_edge, region, diagonal, via, peer = edge_target
                outward, inward = self._push(mac_edge, diagonal, x, y, dx, dy, previous_x, previous_y, self._push_box)
        if self._grace_at is not None and self._grace_inward.get(mac_edge, 0.0) < return_edge.ARRIVAL_GRACE_PX:
            return self._release()
        if self._peer is not None and peer != self._peer:
            # Pressure built against one machine's zone is not a push on another's beside it.
            self.pressure = 0.0
            self.pin = None
            self._quarter_reached = 0
        self._peer = peer
        before = self.pressure
        self.pressure = max(0.0, self.pressure + outward - inward)
        if self.pressure <= 0.0:
            return self._release()
        if self.pin is None:
            self.pin = self._seam_pin or (self._pin_point(x, y, self._push_box)
                                          if self._seam_overshoot else (x, y))
        self.mac_edge = mac_edge
        self.region = region

        if outward > 0.0 and self.pressure >= self.resistance_px:
            source_bounds = bounds
            display = self._selected_way.display if self._selected_way else None
            if isinstance(display, str) and display.isdigit() and int(display) < len(displays or ()):
                source_bounds = displays[int(display)]
            step = Step(
                pressure=1.0,
                crossed=True,
                pin=self.pin,
                edge=OPPOSITE[mac_edge],
                offset=self._landing_offset(self._offset(mac_edge, self.pin, source_bounds),
                                            self._selected_way),
                mac_edge=mac_edge,
                region=region,
                via=via,
                peer=peer,
                display=self._selected_way.target_display if self._selected_way else None,
                side=(self._selected_way.target_side or OPPOSITE[mac_edge])
                     if self._selected_way and self._selected_way.target_display else None,
            )
            self.reset()
            return step
        # Ticks are counted against the highest quarter this push has reached, not the
        # previous reading: decay between two events can drop the reading back under a
        # boundary, and comparing readings would then fire the same tick twice.
        quarter = self._quarter(self.pressure)
        tick = quarter > self._quarter_reached
        self._quarter_reached = max(self._quarter_reached, quarter)
        holding = before >= HOLD_MIN_PX or self._seam_overshoot
        if holding:
            self._last_x, self._last_y = self.pin
        return Step(
            pressure=self._fraction(self.pressure),
            hold=holding,
            pin=self.pin,
            tick=tick,
            mac_edge=mac_edge,
            region=region,
            via=via,
            peer=peer,
        )

    @staticmethod
    def arrival_point(edge, offset, bounds, inset=2.0, displays=None, display=None, side=None):
        """Where the pointer lands when input comes home through a Mac `edge`, `offset` of the
        way along it, a couple of points inside so the arrival itself is not already a push. With
        `displays`, on the display return_edge.landing_monitor picks, so a staggered or stacked
        arrangement never lands it in a gap or on the wrong display."""
        left, top, right, bottom = bounds
        offset = max(0.0, min(1.0, float(offset)))
        along_side = edge in ("left", "right")
        if displays:
            rects = [return_edge.Rect(d[0], d[1], d[2] - d[0], d[3] - d[1]) for d in displays]
            if isinstance(display, str) and display.isdigit() and int(display) < len(displays) and side in EDGES:
                owner = displays[int(display)]
                edge = side
                left, top, right, bottom = owner
                along = top + offset * (bottom - 1 - top) if edge in ("left", "right") else left + offset * (right - 1 - left)
            else:
                # The fraction is of the whole edge, as on the PC (return_edge.arrival_position): laid
                # across the one display it lands on, it put the pointer up to a display's width off.
                along = top + offset * (bottom - 1 - top) if along_side else left + offset * (right - 1 - left)
                owner = displays[rects.index(return_edge.landing_monitor(rects, edge, along))]
                left, top, right, bottom = owner
        else:
            along = top + offset * (bottom - 1 - top) if along_side else left + offset * (right - 1 - left)
        # Clear of both ends, as on the PC (return_edge.LANDING_CLEAR_PX): at an end the pointer sat on
        # the edge beside it, and the hand still moving crossed that edge to its machine.
        low, high = (top, bottom - 1) if edge in ("left", "right") else (left, right - 1)
        clear = return_edge.LANDING_CLEAR_PX if high - low > 2 * return_edge.LANDING_CLEAR_PX else 0
        cleared = min(max(along, low + clear), high - clear)
        beyond = {"left": (left - 1, cleared), "right": (right, cleared),
                  "top": (cleared, top - 1), "bottom": (cleared, bottom)}[edge]
        if displays and any(d[0] <= beyond[0] < d[2] and d[1] <= beyond[1] < d[3] for d in displays):
            # Moved in along a staggered neighbour's seam: the wall is the exposed end itself.
            cleared = min(max(along, low), high)
        along = cleared
        if edge in ("left", "right"):
            return (left + inset if edge == "left" else right - 1 - inset, along)
        return (along, top + inset if edge == "top" else bottom - 1 - inset)

    @staticmethod
    def notch_arrival_point(notch_range, depth, bounds, displays=None):
        """Where input coming home through the notch lands: the middle of the notch's span, just below
        it, through the same top-edge landing as any arrival."""
        left, _top, right, _bottom = bounds
        offset = ((notch_range[0] + notch_range[1]) / 2 - left) / max(1, right - 1 - left)
        return CrossingEngine.arrival_point("top", offset, bounds, inset=depth + 2.0, displays=displays)

    def _decay(self, elapsed):
        if elapsed <= 0 or self.resistance_px <= 0:
            return 0.0
        return DECAY_PX_PER_SECOND * elapsed

    def _release(self):
        self.pressure = 0.0
        self.pin = None
        self.mac_edge = None
        self.region = None
        self._quarter_reached = 0
        self._peer = None
        self._selected_way = None
        return Step()

    def _fraction(self, pressure):
        if self.resistance_px <= 0:
            return 0.0
        return max(0.0, min(1.0, pressure / self.resistance_px))

    def _quarter(self, pressure):
        if self.resistance_px <= 0:
            return 0
        return min(3, int(4 * pressure / self.resistance_px))

    def _target(self, x, y, bounds, notch_range, displays=None, corner=True, ready=None,
                previous=None, delta=(0.0, 0.0)):
        """Which armed region the pointer is touching, as (mac_edge, region, diagonal, method, peer).
        Every machine's corners are checked first, then edges, then thirds, then the notch (WIRE.md
        section 8): a corner wins over the edge it sits on, since its box is inside that edge's strip.
        `ready(peer)`, when given, leaves out the zones of machines that cannot take input now."""
        ways = [way for way in self.ways if ready is None or ready(way.peer)]
        display_frames = {str(index): frame for index, frame in enumerate(displays or ())}
        left, top, right, bottom = bounds
        x_max, y_max = right - 1, bottom - 1
        at = {
            "left": x <= left + EDGE_TOLERANCE,
            "right": x >= x_max - EDGE_TOLERANCE,
            "top": y <= top + EDGE_TOLERANCE,
            "bottom": y >= y_max - EDGE_TOLERANCE,
        }
        own = next((d for d in displays or () if d[0] <= x < d[2] and d[1] <= y < d[3]), None)
        edge_box = {}
        for edge in {way.edge for way in ways if way.methods & {"edge", "part"}}:
            edge_box[edge] = bounds
            if own is not None and not at[edge] and self._exposed(edge, x, y, own, displays):
                # The edge of a display that stops short of the desktop's, with nothing beyond it: a
                # wall all the same, as on the PC. Against the whole desktop it could never be reached.
                at[edge] = True
                edge_box[edge] = own
        # What the push is measured against: the display's own edge when that is the wall.
        self._push_box = bounds
        previous_point = previous if previous and previous[0] is not None and previous[1] is not None else None
        for way in ways if corner else ():
            if "corner" not in way.methods:
                continue
            vertical, horizontal = way.corner.split("_")
            screen = display_frames.get(way.display) if way.display is not None else None
            if way.display is not None and screen is None:
                continue
            if screen is not None:
                sx, sy, sr, sb = screen
                if sx <= x < sr and sy <= y < sb:
                    near_x = x <= sx + CORNER_PX if horizontal == "left" else x >= sr - CORNER_PX
                    near_y = y <= sy + CORNER_PX if vertical == "top" else y >= sb - CORNER_PX
                    at_x = x <= sx + EDGE_TOLERANCE if horizontal == "left" else x >= sr - 1 - EDGE_TOLERANCE
                    at_y = y <= sy + EDGE_TOLERANCE if vertical == "top" else y >= sb - 1 - EDGE_TOLERANCE
                    if near_x and near_y and (at_x or at_y):
                        box_x = sx if horizontal == "left" else sr - CORNER_PX
                        box_y = sy if vertical == "top" else sb - CORNER_PX
                        self._push_box = screen
                        self._selected_way = way
                        return horizontal, (box_x, box_y, CORNER_PX, CORNER_PX), vertical, "corner", way.peer
                prior = previous_point
                if prior is None:
                    candidate = (x - delta[0], y - delta[1])
                    if not (x < sx or x >= sr or y < sy or y >= sb):
                        continue
                    prior = candidate
                px, py = prior
                in_corner = (sx <= px < sr and sy <= py < sb
                             and (px <= sx + CORNER_PX if horizontal == "left" else px >= sr - CORNER_PX)
                             and (py <= sy + CORNER_PX if vertical == "top" else py >= sb - CORNER_PX))
                crossed_x = px < sr <= x if horizontal == "right" else x < sx <= px
                crossed_y = py < sb <= y if vertical == "bottom" else y < sy <= py
                if in_corner and (crossed_x or crossed_y):
                    box_x = sx if horizontal == "left" else sr - CORNER_PX
                    box_y = sy if vertical == "top" else sb - CORNER_PX
                    self._seam_overshoot = True
                    fractions = []
                    if crossed_x:
                        wall_x = sx if horizontal == "left" else sr
                        fractions.append((wall_x - px) / (x - px))
                    if crossed_y:
                        wall_y = sy if vertical == "top" else sb
                        fractions.append((wall_y - py) / (y - py))
                    fraction = min(fractions)
                    hit_x, hit_y = px + fraction * (x - px), py + fraction * (y - py)
                    pin_x = min(max(hit_x, sx), sr - 1)
                    pin_y = min(max(hit_y, sy), sb - 1)
                    self._seam_pin = (pin_x, pin_y)
                    self._seam_previous = prior
                    self._push_box = screen
                    self._selected_way = way
                    return horizontal, (box_x, box_y, CORNER_PX, CORNER_PX), vertical, "corner", way.peer
                continue
            near_x = x <= left + CORNER_PX if horizontal == "left" else x >= x_max - CORNER_PX
            near_y = y <= top + CORNER_PX if vertical == "top" else y >= y_max - CORNER_PX
            if near_x and near_y and (at[horizontal] or at[vertical]):
                box_x = left if horizontal == "left" else x_max - CORNER_PX + 1
                box_y = top if vertical == "top" else y_max - CORNER_PX + 1
                self._selected_way = way
                return horizontal, (box_x, box_y, CORNER_PX, CORNER_PX), vertical, "corner", way.peer
        for way in ways:
            screen = display_frames.get(way.display) if way.display is not None else None
            if way.display is not None and screen is None:
                continue
            edge_at = at[way.edge]
            overshot_pin = None
            if screen is not None:
                sx, sy, sr, sb = screen
                edge_at = (sx <= x < sr and sy <= y < sb and {
                    "left": x <= sx + EDGE_TOLERANCE,
                    "right": x >= sr - 1 - EDGE_TOLERANCE,
                    "top": y <= sy + EDGE_TOLERANCE,
                    "bottom": y >= sb - 1 - EDGE_TOLERANCE,
                }[way.edge])
                overshot_pin = self._overshot_edge(way.edge, previous, (x, y), delta, screen)
                edge_at = edge_at or overshot_pin is not None
            if "edge" in way.methods and edge_at:
                point = overshot_pin or (x, y)
                if not self._within_contact(way, point, screen, bounds):
                    continue
                if screen is not None and overshot_pin is not None:
                    self._seam_overshoot = True
                    self._seam_pin = overshot_pin
                    self._seam_previous = (previous if previous[0] is not None else
                                           (x - delta[0], y - delta[1]))
                self._push_box = screen or edge_box[way.edge]
                self._selected_way = way
                return way.edge, self._strip(way.edge, self._push_box), None, "edge", way.peer
        for way in ways:
            screen = display_frames.get(way.display) if way.display is not None else None
            if way.display is not None and screen is None:
                continue
            edge_at = at[way.edge]
            overshot_pin = None
            if screen is not None:
                sx, sy, sr, sb = screen
                edge_at = (sx <= x < sr and sy <= y < sb and {
                    "left": x <= sx + EDGE_TOLERANCE,
                    "right": x >= sr - 1 - EDGE_TOLERANCE,
                    "top": y <= sy + EDGE_TOLERANCE,
                    "bottom": y >= sb - 1 - EDGE_TOLERANCE,
                }[way.edge])
                overshot_pin = self._overshot_edge(way.edge, previous, (x, y), delta, screen)
                edge_at = edge_at or overshot_pin is not None
            if "part" in way.methods and edge_at:
                # The thirds are the pointer's own display's, as the PC measures them: along the whole
                # desktop, a short display beside a tall one could hold none of the middle third.
                local_bounds = screen or own or bounds
                point = overshot_pin or (x, y)
                if not self._within_contact(way, point, screen, bounds):
                    continue
                part = return_edge.part_of(self._offset(way.edge, point, local_bounds))
                if part in way.parts:
                    if screen is not None and overshot_pin is not None:
                        self._seam_overshoot = True
                        self._seam_pin = overshot_pin
                        self._seam_previous = (previous if previous[0] is not None else
                                               (x - delta[0], y - delta[1]))
                    self._push_box = local_bounds
                    self._selected_way = way
                    return way.edge, self._part_strip(way.edge, local_bounds, part), None, "edge", way.peer
        for way in ways:
            if "notch" in way.methods and notch_range is not None and at["top"]:
                notch_left, notch_right = notch_range
                if notch_left <= x <= notch_right:
                    return "top", (notch_left, top, notch_right - notch_left, 1), None, "notch", way.peer
        return None

    @classmethod
    def _within_contact(cls, way, point, screen, bounds=None):
        if way.contact_span is None:
            return True
        target = screen or bounds
        if target is None:
            return False
        fraction = cls._offset(way.edge, point, target)
        return way.contact_span[0] <= fraction <= way.contact_span[1]

    @staticmethod
    def _landing_offset(offset, way):
        if way is None or way.contact_span is None or way.target_span is None:
            return offset
        low, high = way.contact_span
        target_low, target_high = way.target_span
        ratio = max(0.0, min(1.0, (offset - low) / max(high - low, 1e-12)))
        return round(target_low + ratio * (target_high - target_low), 4)

    def _overshot_edge(self, edge, previous, current, delta, screen):
        if previous is None or previous[0] is None or previous[1] is None:
            previous = (current[0] - delta[0], current[1] - delta[1])
        sx, sy, sr, sb = screen
        px, py = previous
        x, y = current
        delta_x, delta_y = x - px, y - py
        if not (sx <= px < sr and sy <= py < sb):
            return None
        if edge == "right" and sx <= px < sr and delta_x > 0 and x >= sr:
            fraction = (sr - px) / delta_x
        elif edge == "left" and sx <= px < sr and delta_x < 0 and x < sx:
            fraction = (sx - px) / delta_x
        elif edge == "bottom" and sy <= py < sb and delta_y > 0 and y >= sb:
            fraction = (sb - py) / delta_y
        elif edge == "top" and sy <= py < sb and delta_y < 0 and y < sy:
            fraction = (sy - py) / delta_y
        else:
            return None
        along = py + fraction * delta_y if edge in ("left", "right") else px + fraction * delta_x
        low, high = (sy, sb) if edge in ("left", "right") else (sx, sr)
        if not low <= along < high:
            return None
        return ((sx if edge == "left" else sr - 1, along) if edge in ("left", "right")
                else (along, sy if edge == "top" else sb - 1))

    @staticmethod
    def _pin_point(x, y, bounds):
        left, top, right, bottom = bounds
        return min(max(x, left), right - 1), min(max(y, top), bottom - 1)

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
