"""The control window's pages, which one opens first, and which carry a dot in the sidebar.

Pure, so the rules are tested without AppKit. The Design page's groups of styles and colours live here
for the same reason.
"""

from __future__ import annotations

import re

from core import effects

# (key, name, SF Symbol, what the page is for), in sidebar order, which is the order a new Mac is
# set up in after the Overview; Cmd+1 is the first.
PAGES = (
    ("overview", "Overview", "gauge.with.needle",
     "Where input is, whether each link is up, and the controls you reach for every day. Pair your "
     "machines here first."),
    ("permissions", "Permissions", "lock.shield",
     "macOS must allow Beamer to read this keyboard and trackpad before it can send them anywhere."),
    ("crossing", "Crossing", "cursorarrow.motionlines",
     "Choose how the pointer or a key moves input to Windows, and how hard the edge pushes back first."),
    ("keyboard", "Keyboard", "keyboard",
     "How this Mac's modifier keys arrive on Windows, which keys and buttons stay on this Mac, and "
     "how fast the PC's pointer moves here. The switch key is on the Crossing page."),
    ("design", "Design", "paintpalette",
     "How crossing looks and feels on this Mac: the edge, the corner, the notch and the trackpad."),
    ("connection", "Connection", "network",
     "Where the first machine you paired is. Pairing fills these in."),
)
KEYS = tuple(page[0] for page in PAGES)
# Under the purpose on the pages whose settings are this Mac's alone, so nobody looks for the PC's
# on the Mac: each app sets only its own machine, and the PC's are in Beamer on the PC.
SCOPE = {
    "crossing": "For this Mac only; the PC keeps its own. Only which side the PC is on is shared. This Mac's "
                "resistance is also what its pointer meets at the PC's edge on the way back.",
    "design": "For this Mac's screen only; the PC keeps its own.",
    "keyboard": "For this Mac's keyboard only; the PC keeps its own.",
}
# With Same on both machines on, under each row of the shared pages that stays this Mac's own.
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
TODAY = "Classic"
TODAY_STYLES = (
    ("glow", "Glow", "A band of light that deepens the harder you push."),
    ("beam", "Beam", "A thin line with a comet of light running along it."),
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
    # Glow and Beam's detail is their kind, as an effect's is its intensity; what they do is said
    # under the preview.
    groups = [(TODAY, [(value, name, INTENSITIES["classic"]) for value, name, _blurb in TODAY_STYLES])]
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
    return [(SIMPLE, simple)] + style_groups(with_effects)[1:]


def colour_groups(today_colours, with_effects=True):
    """[(group, [(colour id, name)])] for the swatches; `today_colours` is crossing.GLOW_COLOURS."""
    groups = [(TODAY, [(name, name.capitalize()) for name in today_colours])]
    for _module, direction, _effects, pack_ids in effects.DIRECTIONS if with_effects else ():
        groups.append((direction, [(pack_id, effects.pack(pack_id)[0]) for pack_id in pack_ids]))
    return groups


def notch_style_applies(style):
    """A crossing effect draws the notch itself, so the notch style only matters under Glow and Beam."""
    return style not in effects.EFFECT_IDS


def part_names(edge):
    """The names of return_edge.PARTS along `edge`, as the page shows them: top to bottom on a side
    edge, left to right along the top or bottom."""
    if edge in ("left", "right"):
        return {"start": "Top", "middle": "Middle", "end": "Bottom"}
    return {"start": "Left", "middle": "Middle", "end": "Right"}


def parts_phrase(edge, parts):
    """The chosen thirds in a sentence: "the top and middle of the right edge"."""
    names = part_names(edge)
    # No third, or none known, is never saved, but a sentence must not be what fails on it.
    chosen = [names[part].lower() for part in ("start", "middle", "end") if part in parts] or ["middle"]
    joined = chosen[0] if len(chosen) == 1 else ", ".join(chosen[:-1]) + " and " + chosen[-1]
    return f"the {joined} of the {edge} edge"


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


def toggle_part(parts, part, on):
    """The thirds after `part` is ticked or unticked. The last one cannot be unticked: no thirds
    would be a way in that never crosses; switching Part of the edge off is how to have none."""
    chosen = set(parts) | {part} if on else set(parts) - {part}
    if not chosen:
        chosen = set(parts)
    return [name for name in ("start", "middle", "end") if name in chosen]


# The Crossing page's diagram: this Mac's screen at 16:10 and the PC's at 16:9, a gap between.
DIAGRAM_THIS = (120.0, 75.0)
DIAGRAM_OTHER = (112.0, 63.0)
DIAGRAM_GAP = 10.0
MARK = 3.0
CORNER_MARK = 12.0


def arrangement(width, height, side):
    """Where the two screens sit in a diagram `width` by `height`, top left at the origin: (this,
    other) as (x, y, w, h), the pair centred with the other screen on `side` of this one."""
    tw, th = DIAGRAM_THIS
    ow, oh = DIAGRAM_OTHER
    if side in ("left", "right"):
        left = (width - (tw + DIAGRAM_GAP + ow)) / 2
        this_x = left + ow + DIAGRAM_GAP if side == "left" else left
        other_x = left if side == "left" else left + tw + DIAGRAM_GAP
        return (this_x, (height - th) / 2, tw, th), (other_x, (height - oh) / 2, ow, oh)
    top = (height - (th + DIAGRAM_GAP + oh)) / 2
    this_y = top + oh + DIAGRAM_GAP if side == "top" else top
    other_y = top if side == "top" else top + th + DIAGRAM_GAP
    return ((width - tw) / 2, this_y, tw, th), ((width - ow) / 2, other_y, ow, oh)


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
        return "Paused. Edges, corners and the notch do nothing until you resume; the shortcut still works."
    if full_screen_app is not None:
        return (
            f"Off while {full_screen_app} is full screen, so the pointer stays put at "
            "the edges; the shortcut still works."
        )
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
    and Where the PC is does not steer it."""
    chosen = set(methods)
    return bool(chosen & {"notch", "corner"}) and not chosen & {"edge", "part"}


NOTCH_OR_CORNER_NOTE = (
    "With only the notch or a corner on, input arrives by the notch's or corner's own side, so this "
    "choice does nothing on this Mac; your PC's own push still follows it."
)


def crossing_rows(methods):
    """Which of the Crossing page's rows and modules show for these ways. The rest are hidden, not
    dimmed, and reappear the moment a way that uses them is chosen."""
    chosen = set(methods)
    pointer = bool(chosen & {"edge", "part", "corner", "notch"})
    return {
        # Where the PC sits, which every way in uses: a crossing arrives by it, and with only the
        # shortcut on it is still the PC's edge that leads back here.
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
