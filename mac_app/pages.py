"""The control window's pages, which one opens first, and which carry a dot in the sidebar.

Pure, so the rules are tested without AppKit. The Design page's groups of styles and colours live here
for the same reason.
"""

from __future__ import annotations

import re

from core import effects
from core import peerlist
from core.ways import part_names, parts_phrase, toggle_part

# (key, name, SF Symbol, what the page is for), in sidebar order, which is the order a new Mac is
# set up in after the Overview; Cmd+1 is the first.
PAGES = (
    ("overview", "Overview", "gauge.with.needle",
     "Where input is, whether each link is up, and the controls you reach for every day. Pair your "
     "machines here first."),
    ("permissions", "Permissions", "lock.shield",
     "macOS must allow Beamer to read this Mac's input and to move its pointer and type when another machine drives it."),
    ("crossing", "Crossing", "cursorarrow.motionlines",
     "Choose how the pointer or a key moves input to each machine, and how hard the edge pushes back first."),
    ("keyboard", "Keyboard", "keyboard",
     "How this Mac's modifier keys arrive on other machines, which keys and buttons stay on this Mac, "
     "and how fast another machine's pointer moves here. The switch key is on the Crossing page."),
    ("design", "Design", "paintpalette",
     "How crossing looks and feels on this Mac: the edge, the corner, the notch and the trackpad."),
    ("connection", "Connection", "network",
     "Local connection details for the first machine you paired. Pair again to change another machine's entry."),
)
KEYS = tuple(page[0] for page in PAGES)
# Under the purpose on the pages whose settings are this Mac's alone, so nobody looks for the other
# machine's on the Mac: each app sets only its own machine, and the other's are in Beamer there.
SCOPE = {
    "crossing": "Ways below lead to the selected machine. Its side is shared with that machine. Resistance, drag protection and Shortcut apply to this Mac's input for every destination.",
    "design": "Used on this screen. Shared animation settings may follow another machine; Animate, window appearance, notch and trackpad controls stay on this Mac.",
    "keyboard": "Modifier keys and Stays here belong to this Mac's input. Pointer speed and scrolling belong to this screen when another machine drives it. Every machine keeps its own.",
}
# With Same on all machines on, under each row of the shared pages that stays this Mac's own.
OWN_ROW = "This Mac only."
OWN_NOTCH = "The notch is this Mac only."
# The window's footer, and the pages where its first sentence would be untrue.
FOOTER_APPLIES = "Changes apply as you make them."
FOOTER_RUNNING = "Closing this window keeps Beamer running in the menu bar."
EXPLICIT_PAGES = ("connection",)


def footer(page):
    return FOOTER_RUNNING if page in EXPLICIT_PAGES else f"{FOOTER_APPLIES} {FOOTER_RUNNING}"


def opening_page(last, accessibility, input_monitoring):
    """The last page viewed in this run; on the first open, Permissions while either is still
    required, otherwise Overview."""
    if last in KEYS:
        return last
    return "overview" if accessibility and input_monitoring else "permissions"


def dots(accessibility, input_monitoring, link_key):
    """Page key to the tone of its sidebar dot, for the pages that need one."""
    marks = {}
    if not (accessibility and input_monitoring):
        marks["permissions"] = "amber"
    if link_key == "token":
        marks["overview"] = "fault"
    return marks


def step(current, direction):
    """The page an arrow key moves to, stopping at either end of the list."""
    index = KEYS.index(current) if current in KEYS else 0
    return KEYS[min(max(index + direction, 0), len(KEYS) - 1)]


# The Design page's styles and colours. The classic Glow and Beam come first and stay the default;
# the crossing effects follow in effects.DIRECTIONS order, each direction a row of quiet, medium
# and showpiece, and its three colour packs a row of swatches.
TODAY = "Light"
TODAY_STYLES = (
    ("glow", "Glow", "A band of light that deepens the harder you push."),
    ("beam", "Beam", "A thin line with a comet of light running along it."),
    ("aperture", "Aperture", "A fine lens of light opens under pressure and closes softly behind the arriving pointer."),
)
INTENSITIES = {"quiet": "Quiet", "medium": "Medium", "showpiece": "Showpiece", "classic": "Classic"}


def effects_load_error():
    """None when every crossing effect loads, else the exception: the Design page then offers only
    today's styles and colours rather than failing to build."""
    try:
        effects._load()
    except Exception as exc:
        return exc
    return None


def style_groups(with_effects=True):
    """[(group, [(style id, name, detail)])] for the style tiles."""
    with_effects = with_effects and effects_load_error() is None
    # Glow and Beam's detail is their kind, as an effect's is its intensity; what they do is said
    # under the preview.
    classic_styles = TODAY_STYLES if with_effects else TODAY_STYLES[:2]
    groups = [(TODAY, [(value, name, effects.CLASSIC[value].intensity.capitalize())
                       for value, name, _blurb in classic_styles])]
    for _module, direction, effect_ids, _packs in effects.DIRECTIONS if with_effects else ():
        row = []
        for effect_id in effect_ids:
            fx = effects.effect(effect_id)
            row.append((effect_id, fx.name, INTENSITIES.get(fx.intensity, fx.intensity.capitalize())))
        groups.append((direction, row))
    return groups


SIMPLE = "Simple"


def switch_style_groups(with_effects=True):
    """[(group, [(value, name, detail)])] for the style tiles in Switch mode: what a switch plays,
    effects.SWITCH_STYLES, with the effects grouped as the crossing's are."""
    simple = [("match", "Same as crossing", "Plays the crossing style"), ("locator", "Ring", "Closes onto the pointer")]
    with_effects = with_effects and effects_load_error() is None
    aperture = [(TODAY, [("aperture", effects.CLASSIC["aperture"].name,
                          effects.CLASSIC["aperture"].intensity.capitalize())])] if with_effects else []
    return [(SIMPLE, simple)] + aperture + style_groups(with_effects)[1:]


def colour_groups(today_colours, with_effects=True):
    """[(group, [(colour id, name)])] for the swatches; `today_colours` is crossing.GLOW_COLOURS."""
    return effects.colour_groups([(name, name.capitalize()) for name in today_colours] + [('aurora', 'Aurora')], with_effects)


def notch_style_applies(style):
    """A crossing effect draws the notch itself, so the notch style only matters under Glow and Beam."""
    return style not in effects.EFFECT_IDS


def share_sentence(holders, side, name):
    """Name the machines already using the side the chosen machine wants."""
    names = holders[0] if len(holders) == 1 else ", ".join(holders[:-1]) + " and " + holders[-1]
    verb = "crosses" if len(holders) == 1 else "cross"
    # The sentence says what picking a third means: it goes to `name`.
    keep = f"stay with {holders[0]}" if len(holders) == 1 else "stay where they are"
    return (f"{names} already {verb} from the {side} edge of this Mac. The thirds you pick go to {name}; "
            f"the rest {keep}.")


# The ways in, in the Crossing page's order.
WAYS = ("shortcut", "edge", "part", "corner", "notch")
_EXCLUSIVE = {"edge": "part", "part": "edge"}


def toggle_way(methods, way, on):
    """The ways in after `way` is switched on or off. Edge and Part of the edge exclude each other:
    the whole edge already includes any part of it."""
    chosen = set(methods) - {way}
    if on:
        chosen = (chosen | {way}) - {_EXCLUSIVE.get(way)}
    return [name for name in WAYS if name in chosen]


# The Crossing page's diagram: this Mac's screen at 16:10 and the other machine's at 16:9, a gap between.
DIAGRAM_THIS = (120.0, 75.0)
DIAGRAM_OTHER = (112.0, 63.0)
DIAGRAM_GAP = 10.0
MARK = 3.0
CORNER_MARK = 12.0


DIAGRAM_SPLIT_GAP = 6.0
DIAGRAM_MARGIN = 12.0
SIDES = ("left", "right", "top", "bottom")
PART_ORDER = ("start", "middle", "end")


def arrangement(width, height, side):
    """Where the two screens sit with one machine paired: (this, other) as (x, y, w, h)."""
    this, (other,) = diagram_layout(width, height, [(side, None)])
    return this, other


def diagram_layout(width, height, machines):
    """Where this Mac and each machine sit in a drawing `width` by `height`, top left at the origin.
    `machines` is (side, first third) per machine: side "" when it is not placed, first third the
    index of its first third when it crosses by Part of the edge, else None. Returns (this, frames),
    a frame (x, y, w, h) or None for each machine. Two or more on one side share it, each as much
    shorter as there are of them, in the order of their thirds when all have some, else of the
    list; the whole is centred, and shrinks to fit only when it must."""
    tw, th = DIAGRAM_THIS
    ow, oh = DIAGRAM_OTHER
    placed = [None] * len(machines)
    for side in SIDES:
        at = [index for index, (where, _third) in enumerate(machines) if where == side]
        if at and all(machines[index][1] is not None for index in at):
            at.sort(key=lambda index: machines[index][1])
        count = len(at)
        if not count:
            continue
        w, h = ow / count, oh / count
        upright = side in ("left", "right")
        step = (h if upright else w) + DIAGRAM_SPLIT_GAP
        start = ((th if upright else tw) - (step * count - DIAGRAM_SPLIT_GAP)) / 2
        for order, index in enumerate(at):
            along = start + order * step
            placed[index] = {
                "left": (-DIAGRAM_GAP - w, along), "right": (tw + DIAGRAM_GAP, along),
                "top": (along, -DIAGRAM_GAP - h), "bottom": (along, th + DIAGRAM_GAP),
            }[side] + (w, h)
    frames = [(0.0, 0.0, tw, th)] + [frame for frame in placed if frame is not None]
    left = min(x for x, _y, _w, _h in frames)
    top = min(y for _x, y, _w, _h in frames)
    span_w = max(x + w for x, _y, w, _h in frames) - left
    span_h = max(y + h for _x, y, _w, h in frames) - top
    scale = 1.0
    if span_w > width or span_h > height:
        scale = min((width - 2 * DIAGRAM_MARGIN) / span_w, (height - 2 * DIAGRAM_MARGIN) / span_h)
    dx = (width - span_w * scale) / 2 - left * scale
    dy = (height - span_h * scale) / 2 - top * scale

    def fit(frame):
        x, y, w, h = frame
        return (dx + x * scale, dy + y * scale, w * scale, h * scale)

    return fit(frames[0]), [None if frame is None else fit(frame) for frame in placed]


def placement(machine):
    """A machine as diagram_layout takes it: its side, and its first third when it crosses by Part
    of the edge."""
    parts = [PART_ORDER.index(part) for part in machine["parts"] if part in PART_ORDER]
    return machine["side"] if machine["side"] in SIDES else "", min(parts) if "part" in machine["methods"] and parts else None


def diagram_marks(this, machines):
    """What lights on this Mac's screen in the drawing. `machines` are dicts with `key`, `side`,
    `methods`, `parts`, `corner` and `chosen`. Returns (marks, notch): marks maps (key, "edge"),
    (key, "corner") and (key, "part", third) to (frame or None, tone or None), the tone "chosen"
    for the chosen machine's ways, "other" for another's, "track" for a third of the chosen
    machine's edge it does not use and no other machine does, and None for nothing lit; notch is
    the tone of the notch, or None."""
    held = {}
    for machine in machines:
        side, methods = machine["side"], machine["methods"]
        if side in SIDES and ("edge" in methods or "part" in methods):
            for part in PART_ORDER if "edge" in methods else machine["parts"]:
                held.setdefault((side, part), []).append(machine["key"])
    marks = {}
    for machine in machines:
        key, side, methods = machine["key"], machine["side"], machine["methods"]
        tone = "chosen" if machine["chosen"] else "other"
        placed = side in SIDES
        marks[(key, "edge")] = (side_mark(this, side) if placed else None, tone if placed and "edge" in methods else None)
        thirds = third_marks(this, side) if placed else {}
        for part in PART_ORDER:
            lit = None
            if placed and "part" in methods:
                if part in machine["parts"]:
                    lit = tone
                elif machine["chosen"] and not held.get((side, part)):
                    lit = "track"
            marks[(key, "part", part)] = (thirds.get(part), lit)
        marks[(key, "corner")] = (corner_mark(this, machine["corner"]), tone if "corner" in methods else None)
    notch = [machine["chosen"] for machine in machines if "notch" in machine["methods"]]
    return marks, ("chosen" if any(notch) else "other") if notch else None


_WHERE = {"left": "to the left of", "right": "to the right of", "top": "above", "bottom": "below"}


def _joined(names):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def not_placed(machines):
    """The line under the drawing naming the machines with no side yet; "" when every one has one."""
    names = [machine["label"] for machine in machines if machine["side"] not in SIDES]
    return f"Not placed yet: {_joined(names)}" if names else ""


def arrangement_description(machines, sentence):
    """What VoiceOver reads for the drawing: where every machine is, then the chosen one's ways in."""
    where = [f"{machine['label']} is {_WHERE[machine['side']]} this Mac." if machine["side"] in SIDES
             else f"{machine['label']} is not placed yet." for machine in machines]
    return " ".join(where + [sentence + "." if sentence else "No way in is on."])


def send_items(peers, on, hide):
    """The menu bar's items for sending input with more than one machine to send to: (id, title)
    for each machine this Mac sends to, "Bring input back" on the one input is on (`on`). [] with
    one or none, where the one Send input item stands as it always has."""
    labels = peerlist.labels(peers)
    targets = [entry for entry in peers if entry.get("id") and entry.get("in_use", True) is True
               and entry.get("send") is True and entry.get("port") != 0]
    if len(targets) < 2:
        return []
    return [(entry["id"], "Bring input back" if entry["id"] == on else redact(f"Send input to {labels[entry['token']]}", hide))
            for entry in targets]


def where_caption(label):
    """The caption of the side control: today's words with one machine, the chosen one's name with several."""
    return f"Where {label} is" if label else "Where the other machine is"


def corner_note():
    return "This corner crosses its left or right edge."


def shown_side(side, machines):
    """The side the page shows for a machine. With one machine paired, or none, one not placed yet
    shows Right as it always has; with several, nothing, so no two default onto one side."""
    return side or ("right" if machines <= 1 else "")


def side_mark(screen, side):
    """The strip of `screen` along `side` that lights for the Edge way."""
    x, y, w, h = screen
    return {
        "left": (x, y, MARK, h),
        "right": (x + w - MARK, y, MARK, h),
        "top": (x, y, w, MARK),
        "bottom": (x, y + h - MARK, w, MARK),
    }[side]


def third_marks(screen, side, gap=2.0):
    """The three thirds of the side strip, start to end as return_edge.PARTS runs: top to bottom
    on a side edge, left to right along the top or bottom."""
    x, y, w, h = side_mark(screen, side)
    if side in ("left", "right"):
        length = (h - 2 * gap) / 3
        return {part: (x, y + index * (length + gap), w, length) for index, part in enumerate(("start", "middle", "end"))}
    length = (w - 2 * gap) / 3
    return {part: (x + index * (length + gap), y, length, h) for index, part in enumerate(("start", "middle", "end"))}


def corner_mark(screen, corner):
    x, y, w, h = screen
    size = CORNER_MARK
    vertical, horizontal = corner.split("_")
    return (x if horizontal == "left" else x + w - size, y if vertical == "top" else y + h - size, size, size)


def notch_mark(screen):
    x, y, w, _h = screen
    width = round(w * 0.2)
    return (x + (w - width) / 2, y, width, 6.0)


def crossing_state_sentence(paired, sending, connected, armed, paused, full_screen_app):
    """The Crossing page's line under Pause: why nothing can cross, first match wins, and only then
    whether crossing is on, held or paused."""
    if not paired:
        return "Not paired yet, so no edge or shortcut moves input until you pair a machine on Overview."
    if not sending:
        return "No machine is driven by this Mac, so edges and the shortcut do nothing."
    if not connected:
        return "Not connected to any machine, so edges and the shortcut do nothing yet."
    if not armed:
        return "Only the shortcut is switched on; there is nothing to pause."
    if paused:
        return "Pauses pointer crossing through this screen, including another machine's pointer. Shortcuts and jump keys still work. Resets when Beamer restarts."
    if full_screen_app is not None:
        return "Held: an app is full screen."
    return "On. Pause it to lean on an edge without switching."


def crossing_state_blocked(paired, sending, connected):
    """Whether crossing_state_sentence is giving a reason nothing can cross."""
    return not (paired and sending and connected)


PLACES = {"edge": "the edge", "corner": "a corner", "notch": "the notch"}


def preview_place(methods):
    """Where the Design page's preview opens: the first of the pointer's ways in it can show, Part of
    the edge shown as the edge, or the edge when the only way in is the shortcut."""
    chosen = set(methods)
    for place, ways in (("edge", {"edge", "part"}), ("corner", {"corner"}), ("notch", {"notch"})):
        if chosen & ways:
            return place
    return "edge"


def place_note(style, place, methods, has_notch):
    """The line under the preview's Edge / Corner / Notch switch: why nothing would cross there, or
    how the chosen style plays there; empty when there is nothing to add."""
    if place == "notch" and not has_notch:
        return "This Mac has no notch, so this only shows how it would look."
    ways = {"edge": {"edge", "part"}, "corner": {"corner"}, "notch": {"notch"}}[place]
    off = not set(methods) & ways
    if off:
        lead = f"{PLACES[place].capitalize()} is not one of your ways in, so this only shows how it would look. "
        lead += "Turn it on under Crossing."
    else:
        lead = ""
    if place != "notch":
        return lead
    if notch_style_applies(style):
        return (lead + " " if lead else "") + "At the notch, Glow and Beam do not draw: the notch style you choose here plays instead, and their tiles show it."
    name = effects.effect(style).name if effects.effect(style) is not None else "The effect"
    return (lead + " " if lead else "") + f"{name} draws its own form round the notch."


def notch_or_corner_only(methods):
    """The pointer's only ways in are the notch and/or a corner, so input arrives by their own side
    and Where the other machine is does not steer it."""
    chosen = set(methods)
    return bool(chosen & {"notch", "corner"}) and not chosen & {"edge", "part"}


def notch_or_corner_note(label):
    """The note under the side control while only the notch or a corner leads there; `label` is the
    chosen machine's with several paired, None with one."""
    return ("With only the notch or a corner on, input arrives by the notch's or corner's own side, so this "
            f"choice does nothing on this Mac; {label or 'the other machine'}'s own push still follows it.")


def crossing_rows(methods):
    """Which of the Crossing page's rows and modules show for these ways. The rest are hidden, not
    dimmed, and reappear the moment a way that uses them is chosen."""
    chosen = set(methods)
    pointer = bool(chosen & {"edge", "part", "corner", "notch"})
    return {
        # Where the other machine sits, which every way in uses: a crossing arrives by it, and with
        # only the shortcut on it is still that machine's edge that leads back here.
        "edge": True,
        "parts": "part" in chosen,
        "corner": "corner" in chosen,
        "dragging": pointer,
        "resistance": pointer,
        "shortcut": "shortcut" in chosen,
    }


# An IPv4 address or a hardware address, anywhere in a line of text.
_ADDRESS = re.compile(r"\b(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})\b")
HIDDEN = "•••"


def redact(text, hide):
    """`text` with every address in it replaced when `hide` is on."""
    return _ADDRESS.sub(HIDDEN, text) if hide and text else text
