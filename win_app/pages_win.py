"""The control window's pages, and which carry a dot in the sidebar.

Pure, so the rules are tested without Qt.
"""

from __future__ import annotations

import re

from core import effects

# (key, name, what the page is for), in sidebar order, which is setup order; Ctrl+1 is the first.
PAGES = (
    ("overview", "Overview",
     "Where input is right now, and the controls you reach for every day. Pair with your Mac here "
     "first."),
    ("crossing", "Crossing",
     "Choose how the pointer or a key moves input to your Mac, and how hard the edge pushes back "
     "first."),
    ("keyboard", "Keyboard",
     "How this PC's Ctrl and Windows keys arrive on your Mac, the keys and buttons that stay here, "
     "and how fast your Mac's pointer moves here."),
    ("design", "Design",
     "How crossing looks on this PC: the light as you push toward your Mac, and where the pointer "
     "lands."),
    ("connection", "Connection",
     "Whether Windows Firewall lets your Mac in, this PC's address, port and shared token, and your "
     "Mac's IP address, which pairing learns."),
)
KEYS = tuple(page[0] for page in PAGES)
# Under the purpose on the pages whose settings are this PC's alone, so nobody looks for the Mac's
# on the PC: each app sets only its own machine.
SCOPE = {
    "crossing": "For this PC only; your Mac keeps its own. Only which side your Mac is on is shared. This PC's "
                "resistance is also what its pointer meets at your Mac's edge on the way back.",
    "design": "For this PC's screen only; your Mac keeps its own.",
    "keyboard": "For this PC's keyboard only; your Mac keeps its own.",
}
# With Same on both machines on, under each row of the shared pages that stays this PC's own.
OWN_ROW = "This PC only."
# The window's footer; the first sentence goes on Connection, whose changes wait for its button.
FOOTER_APPLY = "Changes apply as you make them."
FOOTER_TRAY = "Closing this window keeps Beamer running in the tray."


def footer(page: str) -> str:
    return FOOTER_TRAY if page == "connection" else f"{FOOTER_APPLY} {FOOTER_TRAY}"


def dots(config_error: bool, firewall_tone: str | None) -> dict:
    """Page key to the tone of its sidebar dot, for the pages that need one.

    `firewall_tone` is the note tone Connection's firewall module is already showing -- "note" is
    its healthy end-state (the button just offers a manual re-check), so only "note-amber" and
    "note-fault" earn a dot; a plain boolean would also mark the healthy state. Both problems sit
    on Connection, and a fault outranks amber."""
    if config_error or firewall_tone == "note-fault":
        return {"connection": "fault"}
    if firewall_tone == "note-amber":
        return {"connection": "amber"}
    return {}


# The Design page's styles and colours: Classic first, then each crossing effects direction in
# effects.DIRECTIONS order. Classic's Glow and Beam light this PC's edge or corner as the pointer
# leaves; the effects draw the departure and the arrival, at an edge, a corner or the Mac's notch.
CLASSIC = "Classic"
TODAY_STYLES = (
    ("glow", "Glow", "A band of light that deepens the harder you push."),
    ("beam", "Beam", "A thin line with a comet of light running along it."),
)
TODAY_COLOURS = (("signal", "Signal"), ("colourful", "Colourful"), ("ocean", "Ocean"), ("sunset", "Sunset"), ("mono", "Mono"))


def effects_load_error():
    """None when every crossing effect loads, else the exception: the Design page then offers only
    Classic's styles and colours rather than failing to build, and Beamer still starts."""
    try:
        effects._load()
    except Exception as exc:
        return exc
    return None


def style_groups() -> tuple:
    """(group title, ((style id, name, detail), ...)) in the page's order. An effect's detail is
    its intensity: quiet, medium or showpiece."""
    # Glow and Beam's detail is their kind, as an effect's is its intensity; what they do is said
    # under the preview.
    groups = [(CLASSIC, tuple((value, name, CLASSIC) for value, name, _blurb in TODAY_STYLES))]
    if effects_load_error() is not None:
        return tuple(groups)
    for _module, title, effect_ids, _packs in effects.DIRECTIONS:
        found = [effects.effect(effect_id) for effect_id in effect_ids]
        groups.append((title, tuple((fx.id, fx.name, fx.intensity.capitalize()) for fx in found)))
    return tuple(groups)


def colour_groups() -> tuple:
    """(group title, ((colour id, name), ...)) in the page's order, grouped as the styles are."""
    groups = [(CLASSIC, TODAY_COLOURS)]
    if effects_load_error() is not None:
        return tuple(groups)
    for _module, title, _effects, pack_ids in effects.DIRECTIONS:
        groups.append((title, tuple((pack_id, effects.pack(pack_id)[0]) for pack_id in pack_ids)))
    return tuple(groups)


def is_effect(style: str) -> bool:
    """Whether `style` is one of the crossing effects, which draw the arrival as well as the push."""
    return style in effects.EFFECT_IDS

PLACES = {"edge": "the edge", "corner": "a corner"}


def preview_place(methods) -> str:
    """Where the Design page's preview opens: the first of this PC's ways in it can show, Part of
    the edge shown as the edge, or the edge when the only way in is the shortcut."""
    chosen = set(methods)
    if chosen & {"edge", "part"}:
        return "edge"
    return "corner" if "corner" in chosen else "edge"


def place_note(style: str, place: str, methods) -> str:
    """The line under Show at: why nothing would cross at `place`, or empty when it does."""
    ways = {"edge": {"edge", "part"}, "corner": {"corner"}}[place]
    if set(methods) & ways:
        return ""
    return (f"{PLACES[place].capitalize()} is not one of this PC's ways in, so this only shows how it "
            "would look. Turn it on under Crossing.")



def part_names(edge):
    """The names of return_edge.PARTS along `edge`, as the page shows them: top to bottom on a side
    edge, left to right along the top or bottom."""
    if edge in ("left", "right"):
        return {"start": "Top", "middle": "Middle", "end": "Bottom"}
    return {"start": "Left", "middle": "Middle", "end": "Right"}


def parts_phrase(edge, parts):
    """The chosen thirds in a sentence: "the top and middle of the right edge"."""
    names = part_names(edge)
    chosen = [names[part].lower() for part in ("start", "middle", "end") if part in parts] or ["middle"]
    joined = chosen[0] if len(chosen) == 1 else ", ".join(chosen[:-1]) + " and " + chosen[-1]
    return f"the {joined} of the {edge} edge"


# The Crossing page's ways in, in the page's order. Edge and Part of the edge are two readings of
# one edge, so choosing either drops the other.
WAYS = ("shortcut", "edge", "part", "corner")
EXCLUSIVE_WAYS = ("edge", "part")


def toggle_way(methods, way, on) -> list:
    """`methods` with `way` turned on or off, in WAYS order."""
    chosen = {method for method in methods if method != way}
    if on:
        if way in EXCLUSIVE_WAYS:
            chosen -= set(EXCLUSIVE_WAYS)
        chosen.add(way)
    return [method for method in WAYS if method in chosen]


def toggle_part(parts, part, on) -> list:
    """`parts` with `part` turned on or off, in return_edge.PARTS order. Turning off the last one
    is refused: Part of the edge with no part would be a wall."""
    chosen = set(parts) | {part} if on else set(parts) - {part}
    if not chosen:
        chosen = set(parts)
    return [candidate for candidate in ("start", "middle", "end") if candidate in chosen]


NOT_LEARNED_EDGE = "Not learned yet: your Mac tells this PC when it first connects, or choose a side."


def crossing_state_sentence(paired, mac_heard, sending, connected, armed, paused, full_screen_app) -> str:
    """The Crossing page's line under Pause: why nothing can cross, first match wins, and only then
    whether crossing is on, held or paused."""
    if not paired:
        return "Not paired yet, so no edge or shortcut moves input until you pair with your Mac above."
    if not mac_heard:
        return "Waiting to hear from your Mac. This PC cannot push into it until your Mac has connected once."
    if not sending:
        return "This PC drives your Mac is off, so edges and the shortcut do nothing."
    if not connected:
        return (
            "Not connected to your Mac, so edges and the shortcut do nothing yet. "
            "Check that Windows drives this Mac is on, on your Mac."
        )
    if not armed:
        return "Only the shortcut is switched on; there is nothing to pause."
    if paused:
        return "Paused. The edge, part of the edge and corner do nothing until you resume; the shortcut still works."
    if full_screen_app is not None:
        return (
            f"Off while {full_screen_app} is full screen, so the pointer stays put at "
            "the edges; the shortcut still works."
        )
    return "On. Pause it to lean on an edge without switching."


def crossing_state_blocked(paired, mac_heard, sending, connected) -> bool:
    """Whether crossing_state_sentence is giving a reason nothing can cross."""
    return not (paired and mac_heard and sending and connected)


def outward_link_line(connected) -> str:
    """The Overview's line for this PC's own link to the Mac, which its edges need and the receiver's
    status above it does not say."""
    if connected:
        return "This PC to your Mac: Linked"
    return "This PC to your Mac: not connected, so pushing an edge does nothing"


def crossing_rows(methods) -> frozenset:
    """Which of the Crossing page's rows the chosen ways use; the rest are hidden."""
    methods = set(methods)
    rows = set()
    # Where the Mac sits, which every way in uses: a crossing lands by it, and this PC's edge facing
    # the Mac leads there whatever the Mac's own ways in are.
    rows.add("edge")
    if "part" in methods:
        rows.add("parts")
    if "corner" in methods:
        rows.add("corner")
    if methods & {"edge", "part", "corner"}:
        rows |= {"dragging", "resistance"}
    if "shortcut" in methods:
        rows.add("shortcut")
    return frozenset(rows)


# What a switch into this PC plays, as the Design page's Shortcut and menu tiles: the first group
# stands in for Classic's Glow and Beam, which have no arrival of their own. Named as the Mac's is.
SIMPLE = "Simple"
SWITCH_SIMPLE = (
    ("match", "Same as crossing", "Plays the crossing style"),
    ("locator", "Ring", "Closes onto the pointer"),
)


def switch_groups() -> tuple:
    """style_groups() for a switch: SWITCH_SIMPLE in place of Classic, then the same directions."""
    return ((SIMPLE, SWITCH_SIMPLE),) + style_groups()[1:]


# The Crossing page's "Where your Mac is": the value is the edge of this PC that leads to the Mac.
SIDE_CHOICES = (("left", "Left"), ("right", "Right"), ("top", "Above"), ("bottom", "Below"))
# The ways in, as their rows read: the pointer's first, then the shortcut.
WAY_ROWS = (
    ("edge", "Edge", "One whole side"),
    ("part", "Part of the edge", "Only the thirds you pick"),
    ("corner", "Corner", "Push diagonally into a corner"),
    ("shortcut", "Shortcut", ""),
)
CORNER_NAMES = {"top_left": "top-left", "top_right": "top-right", "bottom_left": "bottom-left", "bottom_right": "bottom-right"}


def trigger_phrase(key_name: str, style: str) -> str:
    """The shortcut as the arrangement diagram's key cap line reads it: "Right Ctrl, twice"."""
    return f"{key_name}, {'held' if style == 'hold' else 'twice'}"


def ways_summary(methods, edge, parts, corner, key_name, style) -> str:
    """The Ways in module's first line: every way input leaves for the Mac, in one sentence."""
    if not edge:
        return "Nothing moves input to your Mac until it has connected once and told this PC which side it is on."
    methods = set(methods)
    ways = []
    if "edge" in methods:
        ways.append(f"push through the whole {edge} edge")
    if "part" in methods:
        ways.append(f"push through {parts_phrase(edge, parts)}")
    if "corner" in methods:
        ways.append(f"push diagonally into the {CORNER_NAMES.get(corner, corner)} corner")
    if "shortcut" in methods:
        ways.append(f"hold {key_name}" if style == "hold" else f"press {key_name} twice")
    if not ways:
        return "Nothing moves input to your Mac: choose at least one way below."
    joined = ways[0] if len(ways) == 1 else ", ".join(ways[:-1]) + " or " + ways[-1]
    return f"Input moves to your Mac when you {joined}."


# The arrangement diagram. Each side of this PC has an angle, and the Mac's screen travels round
# this PC's between them: through a corner position, never across it.
SIDE_ANGLE = {"right": 0.0, "top": 90.0, "left": 180.0, "bottom": 270.0}


def turn_to(angle: float, side: str) -> float:
    """The angle to animate to from `angle`, which may be part-way through a turn, to reach `side`
    the short way round. Half a turn goes over the top from left or right, and round by the left
    from above or below."""
    delta = (SIDE_ANGLE[side] - angle) % 360.0
    if delta > 180.0:
        delta -= 360.0
    if delta == 180.0:
        current = angle % 360.0
        via = 90.0 if current in (0.0, 180.0) else 180.0
        delta = 180.0 if (current + 90.0) % 360.0 == via else -180.0
    return angle + delta


def square_point(angle: float) -> tuple:
    """Where a ray at `angle` degrees meets the unit square's edge, y up: (1, 0) at 0, (1, 1) at 45."""
    import math

    radians = math.radians(angle)
    x, y = math.cos(radians), math.sin(radians)
    scale = max(abs(x), abs(y))
    return round(x / scale, 9), round(y / scale, 9)


def arrangement_rects(width: float, top: float, angle: float, screen: tuple, gap: float) -> tuple:
    """(this PC's screen, your Mac's screen, the pair's height) as (x, y, w, h) rects, the pair
    centred across `width` and starting at `top`, the Mac's screen at `angle` from this PC's."""
    w, h = screen
    sx, sy = square_point(angle)
    dx, dy = sx * (w + gap), -sy * (h + gap)
    pair_h = h + abs(dy)
    cx, cy = width / 2.0, top + pair_h / 2.0
    pc = (cx - dx / 2.0 - w / 2.0, cy - dy / 2.0 - h / 2.0, w, h)
    mac = (cx + dx / 2.0 - w / 2.0, cy + dy / 2.0 - h / 2.0, w, h)
    return pc, mac, pair_h


def side_segment(rect: tuple, side: str, start: float = 0.0, end: float = 1.0, inset: float = 0.0) -> tuple:
    """The stretch of `rect`'s `side` from `start` to `end` (fractions, top to bottom or left to
    right, as return_edge's parts run), as a line (x1, y1, x2, y2) `inset` inside the edge."""
    x, y, w, h = rect
    if side in ("left", "right"):
        line_x = x + inset if side == "left" else x + w - inset
        return line_x, y + h * start, line_x, y + h * end
    line_y = y + inset if side == "top" else y + h - inset
    return x + w * start, line_y, x + w * end, line_y


PART_SPANS = {"start": (0.0, 1.0 / 3.0), "middle": (1.0 / 3.0, 2.0 / 3.0), "end": (2.0 / 3.0, 1.0)}


def corner_box(rect: tuple, corner: str, size: float) -> tuple:
    """The `size` square in `rect`'s `corner` ("top_left" and so on)."""
    x, y, w, h = rect
    vertical, horizontal = corner.split("_")
    return (x if horizontal == "left" else x + w - size, y if vertical == "top" else y + h - size, size, size)


RESISTANCE_MAX = 500


def push_depth(resistance: float, track: float) -> float:
    """How far the push strip's fill reaches from the edge: 0 to RESISTANCE_MAX px across `track`."""
    return max(0.0, min(1.0, resistance / RESISTANCE_MAX)) * track


# An IPv4 address or a hardware address, anywhere in a line of text.
_ADDRESS = re.compile(r"\b(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})\b")
HIDDEN = "•••"


def redact(text: str, hide: bool) -> str:
    """`text` with every address in it replaced when `hide` is on."""
    return _ADDRESS.sub(HIDDEN, text) if hide and text else text
