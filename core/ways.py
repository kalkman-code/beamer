"""Local way intent, picture-to-zone compilation, and the desktops' crossing helpers.

Crossing events live in crossing_state; mutating entry points here are compatibility
wrappers for the existing app loaders and pages. WIRE.md sections 5 and 8 are unchanged.
"""

import base64
import binascii
import copy
import math
import re
import sys
import time
from typing import Callable, Optional

from core.i18n import N_, joined, tr, trc
from core import peerlist
from core import return_edge
from core.settings_sync import next_stamp

EDGES = return_edge.EDGES
CORNERS = return_edge.CORNERS
PARTS = return_edge.PARTS
DEFAULT_PARTS = ["middle"]
DEFAULT_CORNER = "top_left"
EDGE_NAMES = {"left": N_("left"), "right": N_("right"), "top": N_("top"), "bottom": N_("bottom")}
HERE_NAMES = {"this PC": N_("this PC"), "this Mac": N_("this Mac"), "this computer": N_("this computer")}
PART_NAMES_LOWER = {"top": N_("top"), "middle": N_("middle"), "bottom": N_("bottom"),
                    "left": N_("left"), "right": N_("right")}
CORNER_NAMES = {"top_left": N_("Top left"), "top_right": N_("Top right"),
                "bottom_left": N_("Bottom left"), "bottom_right": N_("Bottom right")}
CORNER_NAMES_LOWER = {"top_left": N_("top left"), "top_right": N_("top right"),
                      "bottom_left": N_("bottom left"), "bottom_right": N_("bottom right")}
VERTICAL_PART_NAMES = {"start": N_("Top"), "middle": N_("Middle"), "end": N_("Bottom")}
HORIZONTAL_PART_NAMES = {"start": N_("Left"), "middle": N_("Middle"), "end": N_("Right")}
# How a notice names each kind of zone.
KIND_WORDS = {"edge": "edge", "part": "part of the edge", "corner": "corner", "notch": "notch"}
_JUMP_MODIFIERS = {"alt", "cmd", "ctrl", "shift"}
_JUMP_KEYS = {"backspace", "caps_lock", "delete", "down", "end", "enter", "esc", "home", "insert",
              "left", "page_down", "page_up", "pause", "print_screen", "right", "space", "tab", "up",
              "menu", "num_lock", "scroll_lock", "browser_back", "browser_forward", "volume_mute",
              "volume_down", "volume_up", "media_next", "media_prev", "media_stop", "media_play_pause"}


ATLAS_SENTENCES = {
    "set_elsewhere": N_("Set on {first} or {second}."),
    "between": N_("Crosses between {first} and {second}."),
    "gap": N_("No edge leads to {name}. The shortcut and the menu still reach it."),
    "edge_off": N_("Edge to {name} is off. Tick Edge to turn it back on."),
    "unpicked": N_("None of the thirds picked for {name} touch its tile. Click a third along its edge."),
    "overlap": N_("{first} overlaps {second}. Drag them apart."),
}
ATLAS_PARTS = ("start", "middle", "end")
DIAGRAM_THIS = (120.0, 75.0)
DIAGRAM_OTHER = (112.0, 63.0)
DIAGRAM_GAP = 10.0
DIAGRAM_SPLIT_GAP = 6.0
DIAGRAM_MARGIN = 12.0
# Machine tiles use map units; manual resizing clamps their longer side to this range.
RESIZE_MIN = 40
RESIZE_MAX = 480


def resize_tile(tile, long_side):
    """Return a proportionally resized copy with its origin and display rectangles unchanged."""
    width, height = tile["w"], tile["h"]
    target = max(RESIZE_MIN, min(RESIZE_MAX, int(round(long_side))))
    scale = target / max(width, height)
    resized = copy.deepcopy(tile)
    resized["w"] = max(1, int(round(width * scale)))
    resized["h"] = max(1, int(round(height * scale)))
    for offset in resized.get("display_offsets", {}).values():
        offset["x"] = int(round(offset["x"] * scale))
        offset["y"] = int(round(offset["y"] * scale))
    return resized


def resize_from_drag(tile, dx, dy):
    """Resize from the bottom-right handle using the larger proportional axis movement."""
    width, height = tile["w"], tile["h"]
    change = max((dx / width, dy / height), key=abs)
    return resize_tile(tile, max(width, height) * (1 + change))


def diagram_layout(width, height, machines):
    """The legacy star placement, shared by callers that draw the Crossing arrangement."""
    tw, th = DIAGRAM_THIS
    ow, oh = DIAGRAM_OTHER
    gap, split, margin = DIAGRAM_GAP, DIAGRAM_SPLIT_GAP, DIAGRAM_MARGIN
    placed = [None] * len(machines)
    for side in EDGES:
        indexes = [i for i, (where, _third) in enumerate(machines) if where == side]
        if indexes and all(machines[i][1] is not None for i in indexes):
            indexes.sort(key=lambda i: machines[i][1])
        count = len(indexes)
        if not count:
            continue
        w, h = ow / count, oh / count
        upright = side in ("left", "right")
        step = (h if upright else w) + split
        start = ((th if upright else tw) - (step * count - split)) / 2
        for order, index in enumerate(indexes):
            along = start + order * step
            placed[index] = {"left": (-gap-w, along), "right": (tw+gap, along),
                             "top": (along, -gap-h), "bottom": (along, th+gap)}[side] + (w, h)
    frames = [(0.0, 0.0, tw, th)] + [frame for frame in placed if frame is not None]
    left, top = min(f[0] for f in frames), min(f[1] for f in frames)
    span_w = max(f[0]+f[2] for f in frames)-left
    span_h = max(f[1]+f[3] for f in frames)-top
    scale = 1.0
    if span_w > width or span_h > height:
        scale = min((width-2*margin)/span_w, (height-2*margin)/span_h)
    dx, dy = (width-span_w*scale)/2-left*scale, (height-span_h*scale)/2-top*scale
    def fit(frame):
        x, y, w, h = frame
        return dx+x*scale, dy+y*scale, w*scale, h*scale
    return fit(frames[0]), [None if frame is None else fit(frame) for frame in placed]


def layout_ids(settings, layout=None):
    """Machine ids allowed here, including named third parties in active peers' current paired lists."""
    own = settings.get("machine_id", "")
    entries = settings.get("peers", [])
    known = {own} if isinstance(own, str) and own else set()
    desktops = [peer for peer in entries if peer.get("port", 24820) != 0]
    desktop_ids = {peer.get("id") for peer in desktops
                   if isinstance(peer.get("id"), str) and peer.get("id")}
    known.update(desktop_ids)
    layout = layout if isinstance(layout, dict) else {}
    for peer in desktops:
        if (peer.get("in_use", True) is False or peer.get("left_out") is True
                or peerlist.dormant_reason(peer) is not None):
            continue
        paired_with = peer.get("paired_with", [])
        if isinstance(paired_with, list):
            known.update(ident for ident in paired_with
                         if isinstance(ident, str) and ident not in known
                         and _valid_tile_name(layout.get(ident)))
    return known


def _valid_tile_name(tile):
    name = tile.get("name") if isinstance(tile, dict) else None
    return isinstance(name, str) and bool(name.strip()) and len(name) <= 64


def retain_held_peer_tiles(settings, layout, held_layout=None, received_layout=None):
    """Keep received peer tiles, filling only those omitted by the other machine."""
    result = copy.deepcopy(layout) if isinstance(layout, dict) else {}
    if isinstance(received_layout, dict):
        allowed = layout_ids(settings, received_layout)
        result.update({ident: copy.deepcopy(tile) for ident, tile in received_layout.items() if ident in allowed})
    held_layout = held_layout if isinstance(held_layout, dict) else settings.get("layout", {})
    protected_ids = layout_ids(settings, held_layout)
    missing = sorted(ident for ident in protected_ids if ident and ident not in result)
    if not missing:
        return result
    kept = {peer: held_layout[peer] for peer in missing if isinstance(held_layout.get(peer), dict)}
    defaults = derive_layout({**settings, "layout": {**result, **kept}}, gap=0)
    for peer in missing:
        tile = held_layout.get(peer) if isinstance(held_layout, dict) else None
        if not isinstance(tile, dict):
            tile = defaults.get(peer)
        if isinstance(tile, dict):
            result[peer] = copy.deepcopy(tile)
    return result


def prune_layout(settings, now=None):
    """Drop tiles outside the local map rules and remove legacy vouch metadata."""
    layout = settings.get("layout")
    if not isinstance(layout, dict):
        return False
    allowed = layout_ids(settings, layout)
    cleaned = copy.deepcopy({ident: tile for ident, tile in layout.items() if ident in allowed})
    dropped = len(cleaned) != len(layout)
    for tile in cleaned.values():
        if isinstance(tile, dict):
            tile.pop("vouched_by", None)
    if cleaned == layout:
        return False
    settings["layout"] = cleaned
    if dropped:
        settings["layout_set_at"] = next_stamp(settings.get("layout_set_at", 0), time.time() if now is None else now)
        settings["layout_by"] = settings.get("machine_id", "")
    return True


def derive_layout(settings, width=160, height=100, gap=12):
    """Default one-tile-per-machine positions derived from the local sides and zones."""
    own = settings.get("machine_id", "")
    saved_tiles = settings.get("layout") or {}
    saved = {key: tile for key, tile in saved_tiles.items() if key in layout_ids(settings, saved_tiles)}
    held = saved.get(own)
    x0, y0 = (held.get("x", 0), held.get("y", 0)) if isinstance(held, dict) else (0, 0)
    if isinstance(held, dict) and type(held.get("w")) is int and type(held.get("h")) is int:
        width, height = held["w"], held["h"]
    result = {own: {"x": x0, "y": y0, "w": width, "h": height}}
    for peer, tile in saved.items():
        if isinstance(tile, dict) and all(type(tile.get(key)) is int for key in ("x", "y", "w", "h")) and tile["w"] > 0 and tile["h"] > 0:
            result[peer] = {key: tile[key] for key in ("x", "y", "w", "h")}
            if "displays" in tile and _valid_tile_displays(tile):
                result[peer]["displays"] = copy.deepcopy(tile["displays"])
                if tile.get("display_offsets"):
                    result[peer]["display_offsets"] = copy.deepcopy(tile["display_offsets"])
            if _valid_tile_name(tile):
                result[peer]["name"] = tile["name"].strip()
    entries = [p for p in settings.get("peers", []) if p.get("id") and p.get("port", 24820) != 0]
    # A shelved machine's kept tile is off the map, so it never blocks an active one's place.
    shelved = {p["id"] for p in entries if p.get("left_out") is True or p.get("in_use", True) is False
               or peerlist.dormant_reason(p) is not None}
    def active():
        return {key: tile for key, tile in result.items() if key not in shelved}
    for side in EDGES:
        group = []
        for peer in entries:
            if peer.get("side") != side or peer.get("id") in result:
                continue
            zones = {z.get("kind"): z for z in settings.get("zones", []) if z.get("peer") == peer["id"] and z.get("off") is not True}
            parts = zones.get("part", {}).get("parts", [])
            rank = min((ATLAS_PARTS.index(p) for p in parts if p in ATLAS_PARTS), default=1)
            group.append((peer["id"], rank if "part" in zones else None))
        if group and all(item[1] is not None for item in group):
            group.sort(key=lambda item: item[1])
        for index, (peer, _rank) in enumerate(group):
            centre = y0 + height / 2 if side in ("left", "right") else x0 + width / 2
            along = centre + (index - (len(group)-1)/2) * ((height if side in ("left", "right") else width) + gap)
            if side == "left": rect = (x0-gap-width, along-height/2, width, height)
            elif side == "right": rect = (x0+width+gap, along-height/2, width, height)
            elif side == "top": rect = (along-width/2, y0-gap-height, width, height)
            else: rect = (along-width/2, y0+height+gap, width, height)
            tile = dict(zip(("x", "y", "w", "h"), map(round, rect)))
            if any(_overlaps(tile, other) for other in active().values()):
                tile = {**tile, **snap_position(tile, own, active(), radius=math.inf)}
            result[peer] = tile
    unplaced = [peer["id"] for peer in entries if peer["id"] not in result]
    if unplaced:
        below = max((tile["y"] + tile["h"] for key, tile in active().items() if key != own), default=y0+height)
        for index, peer in enumerate(unplaced):
            x = x0 + width/2 + (index - (len(unplaced)-1)/2) * (width + gap) - width/2
            tile = {"x": round(x), "y": round(below + gap), "w": width, "h": height}
            result[peer] = snap_position(tile, own, active(), radius=math.inf)
    return result


def crossing_stretches(contact, own, thirds):
    """The parts of a contact with `own`'s tile that cross: the whole contact, or only the stretches
    of it inside the listed thirds of `own`'s side when some but not all are."""
    side, start, end = contact
    if side == "corner" or not thirds or set(thirds) >= set(PARTS):
        return [contact]
    origin, length = (own["y"], own["h"]) if side in ("left", "right") else (own["x"], own["w"])
    stretches = []
    for index, part in enumerate(PARTS):
        if part in thirds:
            low, high = max(start, origin+index*length/3), min(end, origin+(index+1)*length/3)
            if high > low:
                stretches.append((side, low, high))
    return stretches


# Display frames are scaled into their tile, so their edges are floats: a smaller screen's edge can
# land a rounding error off its neighbour's integer edge (254.99999999999997 against 255), or between
# two map units where no integer tile can sit flush (a shorter screen's bottom at 57.75). Edges this
# close count as touching; anything that sinks in by more than CONTACT_EPS still overlaps.
CONTACT_EPS = 1e-6
CONTACT_SLACK = 1.0


def _touching(gap):
    return -CONTACT_EPS <= gap < CONTACT_SLACK


def _rect_contact(a, b):
    ax, ay, aw, ah = (a[k] for k in ("x", "y", "w", "h")); bx, by, bw, bh = (b[k] for k in ("x", "y", "w", "h"))
    ar, ab, br, bb = ax+aw, ay+ah, bx+bw, by+bh
    along_y, along_x = min(ab, bb)-max(ay, by), min(ar, br)-max(ax, bx)
    if _touching(ax - br) and along_y > CONTACT_EPS: return "left", max(ay, by), min(ab, bb)
    if _touching(bx - ar) and along_y > CONTACT_EPS: return "right", max(ay, by), min(ab, bb)
    if _touching(ay - bb) and along_x > CONTACT_EPS: return "top", max(ax, bx), min(ar, br)
    if _touching(by - ab) and along_x > CONTACT_EPS: return "bottom", max(ax, bx), min(ar, br)
    horizontal = "left" if _touching(ax - br) else "right" if _touching(bx - ar) else None
    vertical = "top" if _touching(ay - bb) else "bottom" if _touching(by - ab) else None
    if horizontal and vertical:
        return "corner", f"{vertical}_{horizontal}", ""
    return None


def display_contacts(own, peer):
    """[(screen, contact, target)]: each of `own`'s display frames that touches one of `peer`'s, with
    their contact, as the engine finds its zones; a tile without displays is its own one frame."""
    return [(screen, contact, target)
            for screen in _display_frames(own)
            for target in _display_frames(peer)
            if (contact := _rect_contact(screen, target))]


def side_contacts(own, peer):
    """[(screen, contact)]: the side contacts of `display_contacts` for drawing, with those that abut
    along the same one of `own`'s screens and sides (the peer's displays side by side) joined into one
    stretch, so a join between the peer's displays is not drawn as a break."""
    found = sorted(((screen, contact) for screen, contact, _target in display_contacts(own, peer)
                    if contact[0] in EDGES),
                   key=lambda item: (str(item[0]["display"]), item[1][0], item[1][1]))
    joined = []
    for screen, contact in found:
        if joined:
            held, (side, start, end) = joined[-1]
            if held["display"] == screen["display"] and side == contact[0] and abs(end - contact[1]) < 1e-6:
                joined[-1] = (held, (side, start, contact[2]))
                continue
        joined.append((screen, contact))
    return joined


def _overlaps(a, b):
    eps = CONTACT_EPS
    return (a["x"] < b["x"]+b["w"]-eps and b["x"] < a["x"]+a["w"]-eps
            and a["y"] < b["y"]+b["h"]-eps and b["y"] < a["y"]+a["h"]-eps)


def _display_frames(tile):
    """Map the physical display arrangement over the full bounds of its group tile."""
    screens = tile.get("displays")
    if not isinstance(screens, dict) or not screens:
        return [{"display": None, **{key: tile[key] for key in ("x", "y", "w", "h")}}]
    left = min(rect["x"] for rect in screens.values())
    top = min(rect["y"] for rect in screens.values())
    right = max(rect["x"] + rect["w"] for rect in screens.values())
    bottom = max(rect["y"] + rect["h"] for rect in screens.values())
    scale_x = tile["w"] / (right - left)
    scale_y = tile["h"] / (bottom - top)
    offset_x, offset_y = tile["x"], tile["y"]
    offsets = tile.get("display_offsets", {})
    return [{"display": key,
             "x": offset_x + (rect["x"] - left) * scale_x + offsets.get(key, {}).get("x", 0),
             "y": offset_y + (rect["y"] - top) * scale_y + offsets.get(key, {}).get("y", 0),
             "w": rect["w"] * scale_x, "h": rect["h"] * scale_y}
            for key, rect in sorted(screens.items(), key=lambda item: (item[1]["y"], item[1]["x"], item[0]))]


def _wrap_target(frames, side, leaving, own_tile):
    """The peer display furthest out on the edge opposite a wrap-around departure."""
    if not frames or not any(frame.get("display") is not None for frame in frames):
        return None
    opposite = return_edge.OPPOSITE[side]
    coordinate = {"left": lambda frame: frame["x"], "right": lambda frame: frame["x"] + frame["w"],
                  "top": lambda frame: frame["y"], "bottom": lambda frame: frame["y"] + frame["h"]}
    extreme = min(coordinate[opposite](frame) for frame in frames) if opposite in ("left", "top") else max(
        coordinate[opposite](frame) for frame in frames)
    candidates = [frame for frame in frames if coordinate[opposite](frame) == extreme]
    along = "y" if side in ("left", "right") else "x"
    size = "h" if along == "y" else "w"
    # Spans compared as fractions of each machine's own extent, so a bottom display lands on a bottom one.
    def fraction(low, high, origin, length):
        return ((low - origin) / length, (high - origin) / length) if length else (0, 0)
    start, end = fraction(leaving[along], leaving[along] + leaving[size], own_tile[along], own_tile[size])
    origin = min(frame[along] for frame in frames)
    length = max(frame[along] + frame[size] for frame in frames) - origin
    def rank(frame):
        low, high = fraction(frame[along], frame[along] + frame[size], origin, length)
        return max(0, min(end, high) - max(start, low))
    best_ratio = max(rank(frame) for frame in candidates)
    return min((frame for frame in candidates if rank(frame) == best_ratio),
               key=lambda frame: str(frame.get("display", "")))


def _corner_screen(screens, corner, tile):
    """The display vertex nearest the requested outer vertex of the machine tile."""
    vertical, horizontal = corner.split("_")
    x = tile["x"] if horizontal == "left" else tile["x"] + tile["w"]
    y = tile["y"] if vertical == "top" else tile["y"] + tile["h"]
    return min(screens, key=lambda frame: (((frame["x"] if horizontal == "left" else frame["x"] + frame["w"]) - x) ** 2
                                          + ((frame["y"] if vertical == "top" else frame["y"] + frame["h"]) - y) ** 2,
                                          str(frame.get("display", ""))), default=None)


def snap_position(tile, own_id, layout, radius=12):
    """Nearest free snap on a side's thirds, centre, flush alignments or a corner."""
    own = layout.get(own_id, {"x": 0, "y": 0, "w": 160, "h": 100})
    ox, oy, ow, oh = (own[k] for k in ("x", "y", "w", "h")); x, y, w, h = (tile[k] for k in ("x", "y", "w", "h"))
    candidates = []
    def add(cx, cy, centred=False):
        candidates.append(({"x": round(cx), "y": round(cy), "w": w, "h": h}, centred))

    for side in EDGES:
        for i in range(4):
            if side in ("left", "right"):
                cx, cy = (ox-w if side == "left" else ox+ow), oy+i*oh/3-h/2
            else:
                cx, cy = ox+i*ow/3-w/2, (oy-h if side == "top" else oy+oh)
            add(cx, cy)
        if side in ("left", "right"):
            cx = ox-w if side == "left" else ox+ow
            add(cx, oy+(oh-h)/2, centred=True)
            add(cx, oy)
            add(cx, oy+oh-h)
        else:
            cy = oy-h if side == "top" else oy+oh
            add(ox+(ow-w)/2, cy, centred=True)
            add(ox, cy)
            add(ox+ow-w, cy)
    candidates.extend((({"x": round(ox-w), "y": round(oy-h), "w": w, "h": h}, False),
                       ({"x": round(ox+ow), "y": round(oy-h), "w": w, "h": h}, False),
                       ({"x": round(ox-w), "y": round(oy+oh), "w": w, "h": h}, False),
                       ({"x": round(ox+ow), "y": round(oy+oh), "w": w, "h": h}, False)))
    # With screens of different sizes, a smaller screen's own edges sit inside the tile's box (the
    # bottom of a shorter screen beside a taller one): snap to each screen's sides as well, flush
    # outside it, where no other of this machine's screens is.
    frames = _display_frames(own) if isinstance(own.get("displays"), dict) and len(own["displays"]) > 1 else []
    inner = []
    for frame in frames:
        fx, fy, fw, fh = (frame[k] for k in ("x", "y", "w", "h"))
        for side in EDGES:
            if side in ("left", "right"):
                cx = math.floor(fx - w + CONTACT_EPS) if side == "left" else math.ceil(fx + fw - CONTACT_EPS)
                for cy, centred in ((fy+(fh-h)/2, False), (fy, False), (fy+fh-h, False)):
                    inner.append(({"x": cx, "y": round(cy), "w": w, "h": h}, centred))
            else:
                cy = math.floor(fy - h + CONTACT_EPS) if side == "top" else math.ceil(fy + fh - CONTACT_EPS)
                for cx, centred in ((fx+(fw-w)/2, False), (fx, False), (fx+fw-w, False)):
                    inner.append(({"x": round(cx), "y": cy, "w": w, "h": h}, centred))
    candidates.extend((candidate, centred) for candidate, centred in inner
                      if not any(_overlaps(candidate, frame) for frame in frames)
                      and (candidate, centred) not in candidates)
    free = [(candidate, centred) for candidate, centred in candidates
            if not any(k != own_id and _overlaps(candidate, other) for k, other in layout.items())]
    near = [(candidate, centred) for candidate, centred in free
            if math.hypot(candidate["x"]-x, candidate["y"]-y) <= radius * (1.5 if centred else 1)]
    return min(near or free,
               key=lambda item: (abs(item[0]["x"]-x)+abs(item[0]["y"]-y), not item[1],
                                 abs(item[0]["x"]-x), abs(item[0]["y"]-y)),
               default=(dict(tile), False))[0]


def within_snap_radius(tile, snap, own_id, layout, radius):
    """Whether a free snap candidate is close enough to preview or accept."""
    own = layout.get(own_id, {"x": 0, "y": 0, "w": 160, "h": 100})
    ox, oy, ow, oh = (own[k] for k in ("x", "y", "w", "h"))
    w, h = tile["w"], tile["h"]
    centred = any(
        (snap["x"], snap["y"]) == position
        for position in ((round(ox-w), round(oy+(oh-h)/2)),
                         (round(ox+ow), round(oy+(oh-h)/2)),
                         (round(ox+(ow-w)/2), round(oy-h)),
                         (round(ox+(ow-w)/2), round(oy+oh)))
    )
    return math.hypot(snap["x"]-tile["x"], snap["y"]-tile["y"]) <= radius * (1.5 if centred else 1)


def place_beside(layout, own_id, peer, side):
    """`peer`'s tile centred against `own_id`'s on `side`, so its whole edge crosses; the nearest free
    snap point when another tile is there already."""
    own, tile = layout[own_id], layout[peer]
    ox, oy, ow, oh = (own[k] for k in ("x", "y", "w", "h")); w, h = tile["w"], tile["h"]
    x, y = {"left": (ox-w, oy+(oh-h)/2), "right": (ox+ow, oy+(oh-h)/2),
            "top": (ox+(ow-w)/2, oy-h), "bottom": (ox+(ow-w)/2, oy+oh)}[side]
    placed = {**tile, "x": round(x), "y": round(y)}
    others = {key: value for key, value in layout.items() if key != peer}
    if any(key != own_id and _overlaps(placed, other) for key, other in others.items()):
        return {**tile, **snap_position(placed, own_id, others, radius=math.inf)}
    return placed


def share_side(layout, own_id, peer, side, ignore=()):
    """`peer` joins the tiles already on `own_id`'s `side`, the row re-centred on that side as
    derive_layout groups them, so every one still touches it; {} when they cannot all touch or the
    row would overlap another tile. Tiles in `ignore` (shelved ones) never block."""
    own = layout[own_id]
    vertical = side in ("left", "right")
    along, size = ("y", "h") if vertical else ("x", "w")
    group = sorted((key for key, tile in layout.items() if key not in (own_id, peer, *ignore)
                    and (_rect_contact(own, tile) or ("",))[0] == side), key=lambda key: layout[key][along])
    if not group:
        return {}
    placed, start = {}, own[along] + own[size]/2 - sum(layout[key][size] for key in (*group, peer))/2
    for key in (*group, peer):
        tile = layout[key]
        across = {"left": own["x"]-tile["w"], "right": own["x"]+own["w"],
                  "top": own["y"]-tile["h"], "bottom": own["y"]+own["h"]}[side]
        placed[key] = {**tile, along: round(start), ("x" if vertical else "y"): round(across)}
        start += tile[size]
    others = [tile for key, tile in layout.items() if key not in (own_id, *placed, *ignore)]
    if any(not _rect_contact(own, tile) or _rect_contact(own, tile)[0] != side for tile in placed.values()) or any(
            _overlaps(tile, other) for tile in placed.values() for other in others):
        return {}
    # A moved tile keeps every contact it had with the machines beyond it, or two of them stop crossing.
    beyond = {key: tile for key, tile in layout.items() if key not in (own_id, peer, *placed, *ignore)}
    if any(_rect_contact(layout[key], other) != _rect_contact(placed[key], other)
           for key in group for other in beyond.values()):
        return {}
    return placed


def corner_edge(corner, side):
    """The edge a PC's corner zone crosses: its side when that is one of the corner's two edges, else
    the corner's horizontal edge, as the Mac's always is. A corner push only fires through one of its
    own edges (receiver.zone_models), so a top-left corner for a machine on the right crossing by the
    right edge was silently never armed (Ledger harness, 10-10-2026)."""
    return side if side in EDGES and side in corner.split("_") else corner.split("_")[1]


def _valid_tile_displays(tile):
    screens = tile.get("displays")
    if screens is not None and not (isinstance(screens, dict) and 1 <= len(screens) <= 16
                                    and all(isinstance(key, str) and key.isascii() and 1 <= len(key) <= 64
                                            and isinstance(rect, dict)
                                            and all(type(rect.get(name)) is int for name in ("x", "y", "w", "h"))
                                            and rect["w"] > 0 and rect["h"] > 0
                                            for key, rect in screens.items())):
        return False
    offsets = tile.get("display_offsets", {})
    return (isinstance(offsets, dict) and len(offsets) <= 16
            and (not offsets or isinstance(screens, dict))
            and all(key in screens and isinstance(offset, dict)
                    and type(offset.get("x")) is int and type(offset.get("y")) is int
                    for key, offset in offsets.items()))


def refresh_displays(settings, display_map, now=None):
    """Record this machine's current logical display rectangles in the shared layout."""
    own = settings.get("machine_id")
    tile = (settings.get("layout") or {}).get(own)
    if not isinstance(tile, dict) or not isinstance(display_map, dict) or not display_map:
        return False
    screens = {}
    for key, rect in display_map.items():
        if not isinstance(key, str) or not key.isascii() or not 1 <= len(key) <= 64:
            return False
        if not isinstance(rect, return_edge.Rect):
            return False
        screens[key] = {"x": rect.x, "y": rect.y, "w": rect.width, "h": rect.height}
    if tile.get("displays") == screens:
        return False
    offsets = tile.get("display_offsets")
    if isinstance(offsets, dict):
        tile["display_offsets"] = {key: value for key, value in offsets.items() if key in screens}
    tile["displays"] = screens
    stamp = time.time() if now is None else now
    settings["layout_set_at"] = next_stamp(settings.get("layout_set_at", 0), stamp)
    settings["layout_by"] = own
    return True


def _read_intent(settings, peer):
    """Read legacy zones once at migration; thereafter only the peer's saved intent."""
    entry = _entry(settings, peer) or {}
    held = entry.get("ways")
    if isinstance(held, dict):
        corner = held.get("corner", False) is True
        corner_at = held.get("corner_at") if held.get("corner_at") in CORNERS else DEFAULT_CORNER
        raw_corners = held.get("corners")
        picked_corners = ([value for value in CORNERS if value in raw_corners]
                          if isinstance(raw_corners, list) else [])
        corners_agree = (isinstance(raw_corners, list) and raw_corners == picked_corners
                         and bool(picked_corners) == corner
                         and (not corner or corner_at in picked_corners))
        corners = picked_corners if corners_agree else ([corner_at] if corner else [])
        raw_edges = held.get("edges", [])
        return {"edge": held.get("edge", True) is True,
                "thirds": [part for part in PARTS if part in held.get("thirds", PARTS)] or list(PARTS),
                "edges": [side for side in EDGES if isinstance(raw_edges, list) and side in raw_edges],
                "corner": corner, "corner_at": corner_at, "corners": corners,
                "notch": held.get("notch", False) is True}
    mine = [zone for zone in settings.get("zones", []) if zone.get("peer") == peer]
    if not mine:
        return dict(edge=True, thirds=list(PARTS), edges=[], corner=False,
                    corner_at=DEFAULT_CORNER, corners=[], notch=False)
    kept = entry.get("left_out_ways") if entry.get("left_out") is True else None
    if isinstance(kept, list):
        # Left out in rc12: every zone is off and the kinds in use were kept here for Put back.
        on = lambda zone: zone.get("kind") in kept and zone.get("page_off") is not True
    else:
        # Off only by the page's own untick. A pick that touches no contact today (`unpicked`) and a
        # way set aside while its machine slept (`aside`) were still on in rc12.
        on = lambda zone: zone.get("page_off") is not True and (
            zone.get("off") is not True or zone.get("unpicked") is True or zone.get("aside") is True)
    active_edge = any(zone.get("kind") == "edge" and on(zone) for zone in mine)
    active_part = [zone for zone in mine if zone.get("kind") == "part" and on(zone)]
    picked = next((zone["page_parts"] for zone in mine
                   if zone.get("kind") == "part" and isinstance(zone.get("page_parts"), list)
                   and zone["page_parts"]), None)
    # The whole edge in use wins over any thirds: over a pick left on an off part zone (the page showed
    # the whole edge), and over a part zone also in use (1.4.x let both be on; the edge covered them).
    if active_edge:
        parts = list(PARTS)
    else:
        parts = picked if picked is not None else [part for zone in active_part for part in zone.get("parts", [])]
    if not parts:
        parts = next((zone.get("parts") for zone in mine if zone.get("kind") == "part"
                      and zone.get("parts")), list(PARTS))
    corner = next((zone.get("corner") for zone in mine if zone.get("kind") == "corner"), DEFAULT_CORNER)
    if corner not in CORNERS:
        corner = DEFAULT_CORNER
    corner_on = any(zone.get("kind") == "corner" and on(zone) for zone in mine)
    return dict(edge=active_edge or bool(active_part),
                thirds=[part for part in PARTS if part in parts] or list(PARTS),
                edges=[], corner=corner_on, corner_at=corner, corners=[corner] if corner_on else [],
                notch=any(zone.get("kind") == "notch" and on(zone) for zone in mine))


def picture_capable(settings, capable=()):
    """The machines that place by the picture: `capable` as the caller knows it, else the entries
    that advertise `layout`. Empty means no capability information at all."""
    peers = settings.get("peers", [])
    if not capable and any("caps" in entry for entry in peers):
        return {settings.get("machine_id", "")} | {entry.get("id") for entry in peers if "layout" in entry.get("caps", [])}
    return set(capable)


def compile_layout(settings, layout, moved=None, capable=(), now=None, platform="pc",
                   live=None, gone=None):
    """Generate zones from the picture and saved intent, without changing the caller's settings.

    An explicit capability set (including this machine) keeps old peers on their side-based path.
    With no capability information, tiles are treated as picture peers for existing canvas callers.
    """
    if not isinstance(layout, dict):
        return {"accepted": False, "tiles": {}}
    layout = copy.deepcopy({key: tile for key, tile in layout.items() if key in layout_ids(settings, layout)})
    own_id = settings.get("machine_id", "")
    layout.setdefault(own_id, derive_layout(settings).get(own_id, dict(x=0, y=0, w=160, h=100)))
    if any(not isinstance(tile, dict)
           or any(type(tile.get(key)) is not int for key in ("x", "y", "w", "h"))
           or tile["w"] < 1 or tile["h"] < 1 or not _valid_tile_displays(tile) for tile in layout.values()):
        return {"accepted": False, "tiles": {}}
    saved = copy.deepcopy(settings)
    peers = {entry.get("id"): entry for entry in saved.get("peers", [])}
    for peer, entry in peers.items():
        if _zoned(entry):
            entry["ways"] = _read_intent(settings, peer)
            entry.pop("left_out_ways", None)
    inactive = {peer for peer, entry in peers.items()
                if entry.get("left_out") is True or entry.get("in_use", True) is False
                or peerlist.dormant_reason(entry) is not None or (gone is not None and gone(peer))}
    active = {peer: tile for peer, tile in layout.items() if peer not in inactive}
    screens = {peer: _display_frames(tile) for peer, tile in active.items()}
    surfaces = [(peer, screen) for peer, frames in screens.items() for screen in frames]
    overlaps = {}
    for index, (one, a) in enumerate(surfaces):
        for two, b in surfaces[index + 1:]:
            if one != two and _overlaps(a, b):
                overlaps.setdefault(one, set()).add(two)
                overlaps.setdefault(two, set()).add(one)
    contacts = {}
    for peer in peers:
        contacts[peer] = [] if peer in overlaps or peer in inactive or peer not in active or own_id not in active \
            else display_contacts(active[own_id], active[peer])
    crowded = {}
    for peer, found in contacts.items():
        for screen, contact, target in found:
            if contact[0] in EDGES:
                crowded.setdefault((screen["display"], contact[0]), set()).add(peer)
    if any(len(group) >= 4 for group in crowded.values()):
        result = {"accepted": False, "tiles": {}}
        if moved in active and moved != own_id:
            result.update(snap={moved: snap_position(active[moved], own_id,
                          {key: tile for key, tile in active.items() if key != moved})},
                          tiles={moved: {"state": "overlap", "thirds": [], "wraps": [], "corners": [],
                                         "sentence_keys": []}})
        return result
    capable = picture_capable(saved, capable)
    picture_peers = set(capable) if capable else set(layout)
    held_sides = {peer: entry.get("side", "") for peer, entry in peers.items()}
    stamp = time.time() if now is None else now
    if layout != saved.get("layout"):
        saved.update(layout=copy.deepcopy(layout),
                     layout_set_at=next_stamp(saved.get("layout_set_at"), stamp), layout_by=own_id)
    else:
        saved["layout"] = copy.deepcopy(layout)
    allocations = {}
    # Thirds a wrap-around edge may not take: an old peer's picked contact, and a picture peer's contact
    # with its Edge off, which would otherwise take the side back silently when turned on.
    old_contact_blocks = set()
    for peer, found in contacts.items():
        entry = peers[peer]
        if not _zoned(entry):
            continue
        for index, (screen, contact, target) in enumerate(found):
            side, start, end = contact
            if side not in EDGES:
                continue
            origin, length = (screen["y"], screen["h"]) if side in ("left", "right") else (screen["x"], screen["w"])
            for third, part in enumerate(PARTS):
                overlap = min(end, origin + (third + 1) * length / 3) - max(start, origin + third * length / 3)
                if overlap + 1e-9 < length / 6:
                    continue
                key = (screen["display"], side, part)
                if peer not in picture_peers:
                    if entry["ways"]["edge"] and part in entry["ways"]["thirds"]:
                        old_contact_blocks.add(key)
                    continue
                if not entry["ways"]["edge"]:
                    old_contact_blocks.add(key)
                    continue
                rank =(overlap, _id_bytes(peer) or peer.encode(), -index)
                if key not in allocations or rank > allocations[key][0]:
                    allocations[key] = (rank, peer, index)
    zones, tiles, corners, wrap_taken = [], {}, set(), set()
    notch_taken = False
    def add_zone(peer, kind, screen=None, side=None, target=None, contact=None, **fields):
        zone = dict(peer=peer, kind=kind, **fields)
        if screen is not None:
            if screen["display"] is not None:
                zone["display"] = screen["display"]
            zone["side"] = side
            if target is not None and target["display"] is not None and side in EDGES:
                zone.update(target_display=target["display"], target_side=return_edge.OPPOSITE[side])
            if target is not None and side in EDGES and contact is not None:
                axis = "y" if side in ("left", "right") else "x"
                size = "h" if axis == "y" else "w"
                start, end = contact[1:]
                zone["contact_span"] = [(start - screen[axis]) / screen[size],
                                         (end - screen[axis]) / screen[size]]
                zone["target_span"] = [(start - target[axis]) / target[size],
                                        (end - target[axis]) / target[size]]
        zones.append(zone)
    for peer, entry in peers.items():
        if not _zoned(entry):
            tiles[peer] = {"state": "gap", "thirds": [], "wraps": [], "corners": [],
                           "sentence_keys": ["gap"]}
            continue
        intent = entry["ways"]
        found = contacts.get(peer, [])
        if peer in inactive:
            tiles[peer] = {"state": "dormant", "thirds": [], "wraps": [], "corners": [],
                           "sentence_keys": []}
            continue
        if peer in picture_peers:
            side = next((contact[0] for screen, contact, target in found if contact[0] in EDGES), "")
            if side != entry.get("side", ""):
                entry.update(side=side, side_set_at=saved.get("layout_set_at", int(stamp)),
                             side_by=saved.get("layout_by") or own_id)
        derived_side = entry.get("side", "")
        states, touched, wraps, corner_zones = [], set(), [], []
        if peer in picture_peers:
            for index, (screen, contact, target) in enumerate(found):
                side, start, end = contact
                if side == "corner":
                    corner = contact[1]
                    if (intent["edge"] or intent["corner"]) and corner not in corners:
                        corners.add(corner)
                        edge = corner.split("_")[1]
                        add_zone(peer, "corner", screen, edge, target, corner=corner, edge=edge)
                        corner_zones.append(corner)
                        states.append("corner")
                    continue
                origin, length = ((screen["y"], screen["h"]) if side in ("left", "right")
                                  else (screen["x"], screen["w"]))
                assigned = []
                for part_index, part in enumerate(PARTS):
                    if allocations.get((screen["display"], side, part), (None, None, None))[1] != peer:
                        continue
                    low = origin + part_index * length / 3
                    high = origin + (part_index + 1) * length / 3
                    if min(end, high) - max(start, low) + 1e-9 >= length / 6:
                        assigned.append(part)
                touched.update(assigned)
                kept = [part for part in assigned if part in intent["thirds"]]
                if intent["edge"] and kept:
                    kind = "edge" if len(kept) == len(PARTS) else "part"
                    add_zone(peer, kind, screen, side, target, contact=contact,
                             **({"parts": kept} if kind == "part" else {}))
                    states.append(kind)
            side_contacts = {contact[0] for _screen, contact, _target in found if contact[0] in EDGES}
            own_tile = active.get(own_id, layout[own_id])
            peer_frames = screens.get(peer, [])
            if intent["edge"] and peer not in overlaps:
                for wrap_side in intent["edges"]:
                    if wrap_side in side_contacts:
                        continue
                    for screen in screens.get(own_id, []):
                        origin, length = ((screen["y"], screen["h"]) if wrap_side in ("left", "right")
                                          else (screen["x"], screen["w"]))
                        free = []
                        for part_index, part in enumerate(PARTS):
                            key = (screen["display"], wrap_side, part)
                            if key in wrap_taken or key in allocations or key in old_contact_blocks:
                                continue
                            low, high = origin + part_index * length / 3, origin + (part_index + 1) * length / 3
                            internal = False
                            for other_screen in screens.get(own_id, []):
                                if other_screen is screen:
                                    continue
                                internal_contact = _rect_contact(screen, other_screen)
                                if internal_contact is None or internal_contact[0] != wrap_side:
                                    continue
                                overlap = max(0, min(high, internal_contact[2]) - max(low, internal_contact[1]))
                                if overlap >= (high - low) / 6:
                                    internal = True
                                    break
                            if not internal:
                                free.append(part)
                                wrap_taken.add(key)
                        if not free:
                            continue
                        target = _wrap_target(peer_frames, wrap_side, screen, own_tile)
                        kind = "edge" if len(free) == len(PARTS) else "part"
                        add_zone(peer, kind, screen, wrap_side, target,
                                 **({"parts": free} if kind == "part" else {}))
                        wraps.append({"display": screen["display"], "side": wrap_side, "parts": free})
                        states.append(kind)
            if side_contacts:
                for corner in intent["corners"]:
                    if corner in corners:
                        continue
                    screen = _corner_screen(screens.get(own_id, []), corner, own_tile)
                    if screen is None:
                        continue
                    _vertical, horizontal = corner.split("_")
                    edge = horizontal if platform == "mac" else corner_edge(corner, derived_side)
                    contact_target = next((target for contact_screen, contact, target in found
                                           if contact_screen["display"] == screen["display"]
                                           and contact[0] == edge), None)
                    target = contact_target or _wrap_target(peer_frames, edge, screen, own_tile)
                    corners.add(corner)
                    add_zone(peer, "corner", screen, edge, target, corner=corner, edge=edge)
                    corner_zones.append(corner)
                    states.append("corner")
        else:
            side = entry.get("side", "")
            # Picture contacts reserve their thirds even when the local pick narrows them.
            reserved = {part for (display, edge, part) in allocations if edge == side}
            occupied = {part for zone in zones if zone["kind"] in ("edge", "part")
                        and zone.get("side", peers.get(zone["peer"], {}).get("side")) == side
                        for part in (PARTS if zone["kind"] == "edge" else zone["parts"])}
            kept = [part for part in intent["thirds"] if part not in reserved | occupied]
            if intent["edge"] and side in EDGES and kept:
                kind = "edge" if len(kept) == 3 else "part"
                add_zone(peer, kind, **({"parts": kept} if kind == "part" else {}))
                states.append(kind)
                touched.update(kept)
            if intent["corner"] and intent["corner_at"] not in corners:
                corners.add(intent["corner_at"])
                edge = intent["corner_at"].split("_")[1] if platform == "mac" else corner_edge(intent["corner_at"], side)
                add_zone(peer, "corner", corner=intent["corner_at"], edge=edge)
                corner_zones.append(intent["corner_at"])
                states.append("corner")
        if platform == "mac" and intent["notch"] and not notch_taken:
            add_zone(peer, "notch", **({"return_here": True} if entry.get("notch_return") is True else {}))
            notch_taken = True
        state = next((kind for kind in ("edge", "part", "corner") if kind in states), "gap")
        tiles[peer] = {"state": state, "thirds": [part for part in PARTS if part in touched],
                       "wraps": wraps, "corners": corner_zones,
                       "sentence_keys": ["gap"] if state == "gap" else []}
        if found and not states and intent["edge"] and touched:
            tiles[peer].update(unpicked=True, sentence_keys=["unpicked"])
    for peer, found in contacts.items():
        if peer in inactive or found or peer not in active:
            continue
        for other in contacts:
            if other != peer and other not in inactive and not contacts[other] and other in active and _rect_contact(active[peer], active[other]):
                state = "between" if peer in capable and other in capable else "set-elsewhere"
                tiles[peer] = {"state": state, "thirds": [], "wraps": [], "corners": [],
                               "sentence_keys": ["between" if state == "between" else "set_elsewhere"]}
    for peer, others in overlaps.items():
        if peer in peers and peer not in inactive:
            tiles[peer] = {"state": "overlap", "thirds": [], "wraps": [], "corners": [],
                           "sentence_keys": ["overlap"], "overlap_with": min(others)}
    saved["zones"] = zones
    arrangements = [{"peer": peer, "side": entry.get("side", ""), "set_at": entry.get("side_set_at", 0),
                     "by": entry.get("side_by", "")} for peer, entry in peers.items()
                    if held_sides[peer] != entry.get("side", "")]
    result = dict(accepted=moved not in overlaps, tiles=tiles, layout=copy.deepcopy(layout),
                  arrangements=arrangements, settings=saved)
    if moved in overlaps and moved != own_id:
        result["snap"] = {moved: snap_position(layout[moved], own_id,
                            {key: tile for key, tile in active.items() if key != moved})}
    return result


def inverse_layout(settings, layout, width=160, height=100, gap=12, peer=None, old_side=None):
    """Re-place peers after an incoming arrangement changes their settled side."""
    updated = copy.deepcopy(layout)
    if peer is None:
        derived = derive_layout({**settings, "layout": {}}, width, height, gap)
    else:
        own = layout.get(settings.get("machine_id"), {"x": 0, "y": 0, "w": width, "h": height})
        contact = _rect_contact(own, layout[peer]) if peer in layout else None
        old_side = old_side or (contact[0] if contact and contact[0] != "corner" else "")
        if not old_side and peer in layout:
            tile = layout[peer]
            if tile["x"] + tile["w"] == own["x"] - gap: old_side = "left"
            elif tile["x"] == own["x"] + own["w"] + gap: old_side = "right"
            elif tile["y"] + tile["h"] == own["y"] - gap: old_side = "top"
            elif tile["y"] == own["y"] + own["h"] + gap: old_side = "bottom"
        entry = next((item for item in settings.get("peers", []) if item.get("id") == peer), {})
        affected = {side for side in (old_side, entry.get("side", "")) if side in EDGES}
        saved = {key: value for key, value in settings.get("layout", {}).items() if key != peer}
        derived = derive_layout({**settings, "layout": saved}, width, height, gap)
        if peer in derived:
            updated[peer] = derived[peer]
    return updated


def part_names(edge: str) -> dict:
    """The labels for thirds along an edge, in the direction they run."""
    if edge in ("left", "right"):
        return VERTICAL_PART_NAMES.copy()
    return HORIZONTAL_PART_NAMES.copy()


def parts_phrase(edge: str, parts) -> str:
    """The selected thirds in a sentence, defaulting to the middle when none are known."""
    names = part_names(edge)
    chosen = [tr(PART_NAMES_LOWER[names[part].lower()])
              for part in PARTS if part in parts] or [tr(PART_NAMES_LOWER["middle"])]
    return tr("the {parts} of the {edge} edge").format(parts=joined(chosen), edge=tr(EDGE_NAMES.get(edge, edge)) if edge in EDGE_NAMES else edge)


def toggle_part(parts, part, on) -> list:
    """The selected thirds after a change, keeping one selected so Part of the edge can cross."""
    chosen = set(parts) | {part} if on else set(parts) - {part}
    if not chosen:
        chosen = set(parts)
    return [name for name in PARTS if name in chosen]


# The whole edge and Part of the edge are one choice in two forms: every third, or the thirds picked.
def picked_thirds(methods, parts) -> list:
    """The thirds that cross for these ways: all three for the whole edge, the picked ones for Part
    of the edge, none when neither is on."""
    if "edge" in methods:
        return list(PARTS)
    if "part" in methods:
        return [part for part in PARTS if part in parts] or list(DEFAULT_PARTS)
    return []


def pick_thirds(methods, thirds):
    """(methods, parts) with the edge's form set from `thirds`: all three is the whole edge, some is
    Part of the edge, none turns both off. Other ways are kept, in their order."""
    thirds = [part for part in PARTS if part in thirds]
    kept = [way for way in methods if way not in ("edge", "part")]
    if not thirds:
        return kept, list(DEFAULT_PARTS)
    form = "edge" if len(thirds) == len(PARTS) else "part"
    order = ("shortcut", "edge", "part", "corner", "notch")
    return [way for way in order if way in set(kept) | {form}] + [way for way in kept if way not in order], thirds


def toggle_third(methods, parts, part):
    """(methods, parts) after one third is clicked, in the ledger or on the map: the last lit third
    turns Edge off, keeping it as the remembered thirds; with Edge off, a click lights that third alone."""
    current = picked_thirds(methods, parts)
    if not current:
        return pick_thirds(methods, [part])
    chosen = [third for third in current if third != part] if part in current else current + [part]
    if not chosen:
        return [way for way in methods if way not in ("edge", "part")], list(current)
    return pick_thirds(methods, chosen)


def toggle_edge(methods, parts):
    """(methods, parts) after the Edge text is clicked: on in any form turns off, keeping the thirds;
    off turns on as the whole edge, so the whole edge is always one click away."""
    if picked_thirds(methods, parts):
        return [way for way in methods if way not in ("edge", "part")], [p for p in PARTS if p in parts] or list(PARTS)
    return pick_thirds(methods, PARTS)


def notch_holder(settings: dict) -> Optional[str]:
    """The one machine the notch leads to: the first active one in ways order whose intent has it. A
    file from before 1.5.3 could tick several; the first keeps it, as the zones always did. A machine
    left out does not hold it, so it never keeps the notch from one in use."""
    return next((entry["id"] for entry in settings.get("peers", [])
                 if _zoned(entry) and entry.get("left_out") is not True and entry.get("in_use", True) is not False
                 and peerlist.dormant_reason(entry) is None and _read_intent(settings, entry["id"])["notch"]),
                None)


def corner_holder(settings: dict, corner: str, exclude: Optional[str] = None) -> Optional[str]:
    """The first active machine, other than `exclude`, holding this screen corner by intent or contact."""
    layout = settings.get("layout") if isinstance(settings.get("layout"), dict) else {}
    own_tile = layout.get(settings.get("machine_id"))
    for entry in settings.get("peers", []):
        ident = entry.get("id")
        if (ident == exclude or not _zoned(entry) or entry.get("left_out") is True
                or entry.get("in_use", True) is False or peerlist.dormant_reason(entry) is not None):
            continue
        intent = _read_intent(settings, ident)
        if corner in intent["corners"]:
            return ident
        tile = layout.get(ident)
        if (own_tile and isinstance(tile, dict) and (intent["edge"] or intent["corner"])
                and any((contact := _rect_contact(screen, target)) is not None
                        and contact[0] == "corner" and contact[1] == corner
                        for screen in _display_frames(own_tile) for target in _display_frames(tile))):
            return ident
    return None


def edge_form_detail(side, methods, parts, here="This Mac") -> str:
    """The Edge way's detail on the Crossing page: which form crosses, or where to place it first."""
    if side not in EDGES:
        return tr("Place it beside {here} on the map").format(here=here)
    thirds = picked_thirds(methods, parts)
    if not thirds:
        return tr("Whole edge or the thirds you pick")
    if len(thirds) == len(PARTS):
        return tr("Whole edge")
    names = [tr(part_names(side)[part])
             for part in thirds]
    if len(names) == 1:
        return tr("{name} third").format(name=names[0])
    head = joined(names[:-1])
    last = tr(PART_NAMES_LOWER[part_names(side)[thirds[-1]].lower()])
    return tr("{first} and {last} thirds").format(first=head, last=last)


def third_stretches(contact, own):
    """[(third, side, low, high)]: each third of `own`'s side that a side contact covers, clipped to
    the contact, for the map's click targets. None for a corner contact."""
    side, start, end = contact
    if side == "corner":
        return []
    origin, length = (own["y"], own["h"]) if side in ("left", "right") else (own["x"], own["w"])
    found = []
    for index, part in enumerate(PARTS):
        low, high = max(start, origin + index * length / 3), min(end, origin + (index + 1) * length / 3)
        if high > low:
            found.append((part, side, low, high))
    return found



def valid_jump_key(value) -> bool:
    """Whether a recorded jump key is one modifier chord with a single non-modifier key."""
    if not isinstance(value, str) or not value or value != value.lower():
        return False
    parts = value.split("+")
    if len(parts) < 2 or any(not part for part in parts) or len(set(parts)) != len(parts):
        return False
    modifiers = [part for part in parts if part in _JUMP_MODIFIERS]
    keys = [part for part in parts if part not in _JUMP_MODIFIERS]
    if not set(modifiers) - {"shift"} or len(keys) != 1:
        return False
    if parts[:-1] != [name for name in ("ctrl", "alt", "shift", "cmd") if name in modifiers]:
        return False
    key = keys[0]
    return (key in _JUMP_KEYS or key == "plus" or re.fullmatch(r"[a-z0-9]", key) is not None
            or re.fullmatch(r"f(?:[1-9]|1[0-9]|2[0-4])", key) is not None
            or (len(key) == 1 and key.isprintable() and key not in "+ "))


def jump_chord(modifiers, key) -> Optional[str]:
    """The stable spelling shared by both capture hooks and the key recorders."""
    modifiers = {name[:-2] if name in {"alt_r", "cmd_r", "ctrl_r", "shift_r"} else name for name in modifiers}
    modifiers &= _JUMP_MODIFIERS
    key = key.lower() if isinstance(key, str) else ""
    if key == "+":
        key = "plus"
    value = "+".join([name for name in ("ctrl", "ctrl_r", "alt", "alt_r", "shift", "shift_r", "cmd", "cmd_r")
                      if name in modifiers] + [key])
    return value if valid_jump_key(value) else None


def jump_key(peer: dict) -> Optional[str]:
    """The peer's optional local jump chord, or None when absent or empty."""
    value = peer.get("jump_key") if peer.get("port") != 0 else None
    return value if isinstance(value, str) and value else None


def check_jump_key(settings: dict, peer: str, value, trigger_key: str) -> Optional[str]:
    """Validate and clash-check a peer's jump chord before it is stored; empty clears it."""
    if value in (None, ""):
        return None
    entry = _entry(settings, peer)
    if entry is not None and entry.get("port") == 0:
        raise ValueError(tr("a phone cannot be a jump target"))
    if not isinstance(value, str):
        raise ValueError(tr("choose a modifier and one non-modifier key"))
    if not valid_jump_key(value):
        raise ValueError(tr("choose a modifier and one non-modifier key"))
    if _trigger_in_chord(trigger_key, value):
        raise ValueError(tr("that key is the trigger shortcut's: pick a chord without it"))
    for entry in settings.get("peers", []):
        if entry.get("id") != peer and jump_key(entry) == value:
            raise ValueError(tr("that key is already assigned to another machine"))
    return value


def _trigger_in_chord(trigger_key, chord):
    parts = chord.split("+")
    return (parts[-1] == trigger_key
            or isinstance(trigger_key, str) and trigger_key.removesuffix("_r") in parts)


def check_trigger_key(settings, trigger_key):
    """Apply the same overlap rule whichever side of a shortcut/jump clash is edited."""
    for peer in settings.get("peers", []):
        chord = jump_key(peer)
        if chord and _trigger_in_chord(trigger_key, chord):
            raise ValueError(tr("That shortcut clashes with the jump key {key}; the jump key has been kept.").format(key=chord))


def shared_trigger_values(settings, values):
    """Keep the local jump and trigger on a clash, while allowing the other shared values."""
    if "trigger_key" not in values:
        return values, None
    try:
        check_trigger_key(settings, values["trigger_key"])
    except ValueError as exc:
        return {key: value for key, value in values.items() if key != "trigger_key"}, str(exc)
    return values, ""


def jump_peer(peers: list, chord: str) -> Optional[str]:
    """The in-use machine assigned this jump chord, or None."""
    return next((entry.get("id") for entry in peers
                 if entry.get("in_use", True) is True and entry.get("id") and jump_key(entry) == chord), None)


def _entry(settings: dict, peer: str) -> Optional[dict]:
    return next((entry for entry in settings["peers"] if entry.get("id") == peer), None)








def ways(settings: dict, peer: str) -> dict:
    """Read the peer's saved intent, including thirds remembered while Edge is off."""
    entry = _entry(settings, peer) or {}
    intent = _read_intent(settings, peer)
    methods = []
    if entry and intent["edge"]:
        methods.append("edge" if len(intent["thirds"]) == 3 else "part")
    methods += [kind for kind in ("corner", "notch") if entry and intent[kind]
                and (kind != "notch" or notch_holder(settings) == peer)]
    return dict(side=entry.get("side") if entry.get("side") in EDGES else "",
                methods=methods, parts=list(intent["thirds"]), corner=intent["corner_at"],
                edges=list(intent["edges"]), corners=list(intent["corners"]))


def reach(settings: dict, peer: str, *, platform, capable=(), live=None, gone=None) -> dict:
    """Describe each side and corner for one peer from the current compiled map."""
    entry = _entry(settings, peer)
    layout = settings.get("layout") if isinstance(settings.get("layout"), dict) else {}
    if peer not in layout and entry and _zoned(entry):
        # A tile not in the picture yet sits where the derived layout puts it, as the map draws it.
        layout = {**derive_layout(settings, gap=0), **layout}
    tile = layout.get(peer)
    unavailable = {"state": "unavailable", "by": None}
    if not entry or not _zoned(entry) or not isinstance(tile, dict):
        return {"sides": {side: dict(unavailable) for side in EDGES},
                "corners": {corner: dict(unavailable) for corner in CORNERS}}
    if entry.get("left_out") is True or entry.get("in_use", True) is False:
        # A left-out row still shows its kept ways (the page makes them inert), as if put back.
        back = copy.deepcopy(settings)
        _entry(back, peer).pop("left_out", None)
        _entry(back, peer)["in_use"] = True
        return reach(back, peer, platform=platform, capable=capable, live=live, gone=gone)
    compiled = compile_layout(settings, layout, platform=platform, capable=capable, live=live, gone=gone)
    if not compiled.get("accepted", True):
        return {"sides": {side: dict(unavailable) for side in EDGES},
                "corners": {corner: dict(unavailable) for corner in CORNERS}}
    map_entry = compiled.get("tiles", {}).get(peer, {})
    if map_entry.get("state") in ("overlap", "dormant"):
        return {"sides": {side: dict(unavailable) for side in EDGES},
                "corners": {corner: dict(unavailable) for corner in CORNERS}}
    picture_peers = set(capable) if capable else set(layout)
    if not capable and any("caps" in item for item in settings.get("peers", [])):
        picture_peers = {settings.get("machine_id")} | {
            item.get("id") for item in settings.get("peers", []) if "layout" in item.get("caps", [])}
    intent = _read_intent(settings, peer)
    if peer not in picture_peers:
        # An old peer (1.5.0, 1.5.1) crosses by its side and one corner: no wrap-around, no second corner.
        side_held = entry.get("side")
        holder = lambda corner: corner_holder(settings, corner, exclude=peer)
        return {"sides": {side: ({"state": "contact", "by": peer} if side == side_held else dict(unavailable))
                          for side in EDGES},
                "corners": {corner: ({"state": "on", "by": None} if corner in intent["corners"]
                                     else {"state": "held", "by": holder(corner)} if holder(corner)
                                     else dict(unavailable) if intent["corners"] or side_held not in EDGES
                                     else {"state": "free", "by": None})
                            for corner in CORNERS}}
    wrap_sides = {item["side"]: item["parts"] for item in map_entry.get("wraps", [])}
    zones = compiled.get("settings", {}).get("zones", [])
    own_frames = _display_frames(compiled.get("settings", {}).get("layout", {}).get(
        settings.get("machine_id", ""), derive_layout(settings).get(settings.get("machine_id", ""))))
    peer_frames = _display_frames(tile)
    peer_contacts = [_rect_contact(own, other) for own in own_frames for other in peer_frames]
    contact_sides = {contact[0] for contact in peer_contacts if contact and contact[0] in EDGES}
    sides = {}
    for side in EDGES:
        if side in contact_sides:
            sides[side] = {"state": "contact", "by": peer}
        elif side in wrap_sides:
            sides[side] = {"state": "on", "by": None}
        elif side in intent["edges"]:
            sides[side] = {"state": "kept", "by": None}
        else:
            # Free exactly when picking it would cross now: the compile answers, so the two never differ.
            trial = copy.deepcopy(settings)
            _entry(trial, peer)["ways"] = {**intent, "edge": True, "edges": [*intent["edges"], side]}
            tried = compile_layout(trial, layout, platform=platform, capable=capable, live=live, gone=gone)
            if any(item["side"] == side for item in tried.get("tiles", {}).get(peer, {}).get("wraps", [])):
                sides[side] = {"state": "free", "by": None}
                continue
            holder = next((zone.get("peer") for zone in zones if zone.get("peer") != peer
                           and zone.get("side") == side and zone.get("kind") in ("edge", "part")), None)
            if holder is None:
                inactive = {item.get("id") for item in settings.get("peers", [])
                            if item.get("left_out") is True or item.get("in_use", True) is False
                            or peerlist.dormant_reason(item) is not None or (gone is not None and gone(item.get("id")))}
                holder = next((other for other, other_tile in layout.items()
                               if other not in (peer, settings.get("machine_id")) and other not in inactive
                               and isinstance(other_tile, dict)
                               and any((contact := _rect_contact(own, frame)) is not None and contact[0] == side
                                       for own in own_frames for frame in _display_frames(other_tile))), None)
            sides[side] = {"state": "held", "by": holder} if holder else dict(unavailable)
    corner_states = {}
    for corner in CORNERS:
        holder = next((zone.get("peer") for zone in zones if zone.get("peer") != peer
                       and zone.get("kind") == "corner" and zone.get("corner") == corner),
                      None) or corner_holder(settings, corner, exclude=peer)
        if corner in map_entry.get("corners", []):
            corner_states[corner] = {"state": "on", "by": None}
        elif not contact_sides:
            corner_states[corner] = dict(unavailable)
        elif holder:
            corner_states[corner] = {"state": "held", "by": holder}
        else:
            corner_states[corner] = {"state": "free", "by": None}
    return {"sides": sides, "corners": corner_states}


def edit(settings: dict, peer: str, *, side: str, methods, parts, corner: str, kinds,
         corner_edge: Callable[[str, str], str], now: float, **legacy) -> bool:
    """Compatibility wrapper: side edits place a tile; way edits save independent intent."""
    from core import crossing_state
    entry = _entry(settings, peer)
    if entry is None:
        raise KeyError(peer)
    if not _zoned(entry):
        raise ValueError("a phone cannot have a side or zones")
    moved = side in EDGES and side != entry.get("side")
    held = copy.deepcopy(settings)
    if moved:
        held["layout"] = derive_layout(held, gap=0)
        held["layout"][peer] = place_beside(held["layout"], held["machine_id"], peer, side)
        held["layout_set_at"] = next_stamp(held.get("layout_set_at"), now)
        held["layout_by"] = held["machine_id"]
    chosen = set(methods)
    platform = "mac" if "notch" in kinds else "pc"
    out = crossing_state.edit_ways(held, peer, platform=platform, edge=bool(chosen & {"edge", "part"}),
             thirds=list(PARTS) if "edge" in chosen else parts, corner="corner" in chosen, corner_at=corner,
             notch="notch" in chosen if platform == "mac" else None, live=None, gone=None, now=now)
    settings.clear()
    settings.update(out["settings"])
    return moved




def leave_out(settings: dict, peer: str, *, platform=None, capable=()) -> None:
    """Compatibility wrapper: shelf the peer while keeping its intent."""
    from core import crossing_state
    out = crossing_state.leave_out(settings, peer, platform=platform or _platform(settings), capable=capable)
    settings.clear()
    settings.update(out["settings"])


def put_back(settings: dict, peer: str, *, platform=None, capable=()) -> None:
    """Compatibility wrapper: compile the peer's held intent again."""
    from core import crossing_state
    out = crossing_state.put_back(settings, peer, platform=platform or _platform(settings), capable=capable)
    settings.clear()
    settings.update(out["settings"])


def _platform(settings):
    return "mac" if sys.platform == "darwin" else "pc"


def _stretch(zone: dict, sides: dict, peers=()) -> set:
    """What a zone in use covers, for the rule that two never share one (section 8); the notch is
    one stretch of its own, as a Mac has one notch."""
    owner = next((entry for entry in peers if entry.get("id") == zone.get("peer")), None)
    if owner is not None and (owner.get("in_use", True) is False or owner.get("left_out") is True
                              or owner.get("dormant") is True):
        return set()
    if zone.get("kind") == "notch":
        return {("notch", "all")}
    side = zone.get("side", sides.get(zone.get("peer"), ""))
    display = zone.get("display")
    kind = zone.get("kind")
    if kind == "corner" and zone.get("corner") in CORNERS:
        return {(('display_corner', display), zone["corner"])} if display is not None else {("corner", zone["corner"])}
    scope = ("display", display, side) if display is not None else side
    if kind == "edge" and side in EDGES:
        return {(scope, part) for part in PARTS}
    if kind == "part" and side in EDGES and isinstance(zone.get("parts"), list):
        return {(scope, part) for part in zone["parts"] if part in PARTS}
    return set()


def _stretch_side(stretch):
    scope = stretch[0]
    return scope[2] if isinstance(scope, tuple) and scope[0] == "display" else scope


def share_offer(settings: dict, peer: str, live=None) -> Optional[dict]:
    """No-op: shared stretches are chosen by contacts on the map."""
    return None


def share(settings: dict, side: str, thirds: dict, *, kinds) -> list:
    """No-op retained for app imports: the map owns shared stretches."""
    if side not in EDGES or not isinstance(thirds, dict) or set(thirds) != set(PARTS):
        raise ValueError("a split needs every third of a known side")
    peers = {entry.get("id"): entry for entry in settings.get("peers", []) if _zoned(entry)}
    if any(not isinstance(peer, str) or peers.get(peer, {}).get("side") != side for peer in thirds.values()):
        raise ValueError("every third must belong to a desktop on that side")
    if "part" not in kinds:
        raise ValueError("this app does not offer parts of an edge")
    return []


def _clashing(settings: dict):
    """The first two zones in use that cover one stretch, and that stretch; None when none do."""
    sides = {entry.get("id"): entry.get("side", "") for entry in settings["peers"]}
    seen = {}
    for zone in settings["zones"]:
        if zone.get("off") is True:
            continue
        for stretch in sorted(_stretch(zone, sides, settings["peers"])):
            if stretch in seen:
                return seen[stretch], zone, stretch
            seen[stretch] = zone
    return None


def clash(settings: dict, here: str) -> Optional[str]:
    """The sentence for two zones in use over one stretch of this machine's screen, naming the
    machines they lead to, or None when there is no such pair. `here` is "this Mac" or "this PC"."""
    here = tr(HERE_NAMES[here]) if here in HERE_NAMES else here
    found = _clashing(settings)
    if found is None:
        return None
    first, second, stretch = found
    peers = settings["peers"]
    one = peerlist.label_for(peers, peer_id=first.get("peer")) or peerlist.UNNAMED
    other = peerlist.label_for(peers, peer_id=second.get("peer")) or peerlist.UNNAMED
    scope, part = stretch
    if isinstance(scope, tuple) and scope[0] == "display":
        where = tr("the {edge} edge of display {display}").format(edge=tr(EDGE_NAMES[scope[2]]), display=scope[1])
        kind = scope[0]
    elif isinstance(scope, tuple) and scope[0] == "display_corner":
        where = tr("the {corner} corner of display {display}").format(corner=tr(CORNER_NAMES_LOWER[part]), display=scope[1])
        kind = "corner"
    else:
        where = (tr("the notch") if scope == "notch" else
                 tr("the {corner} corner").format(corner=tr(CORNER_NAMES_LOWER[part])) if scope == "corner" else
                 tr("the {edge} edge").format(edge=tr(EDGE_NAMES[scope])))
        kind = scope
    if first.get("peer") == second.get("peer"):
        # Translators: {where} names the screen region and {here} is this PC or Mac.
        return tr("Two of the ways to {name} cover {where} of {here}. Keep one of them.").format(name=one, where=where, here=here)
    if kind == "notch":
        # Translators: {where} names the screen region and {here} is this PC or Mac.
        return tr("{first} and {second} would both lead from {where} of {here}. Turn the notch off for one of them.").format(first=one, second=other, where=where, here=here)
    if kind == "corner":
        # Translators: {where} names the screen region and {here} is this PC or Mac.
        return tr("{first} and {second} would both lead from {where} of {here}. Choose another corner for one of them.").format(first=one, second=other, where=where, here=here)
    # Translators: {where} names the screen region and {here} is this PC or Mac.
    return tr("{first} and {second} would both lead from {where} of {here}. Move one of them to another side, or give each its own thirds of the edge.").format(first=one, second=other, where=where, here=here)


def _id_bytes(text) -> Optional[bytes]:
    if not isinstance(text, str) or not text:
        return None
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None
    return data if len(data) == 16 and any(data) and base64.urlsafe_b64encode(data).decode("ascii").rstrip("=") == text else None


def arrangement(settings: dict, peer: str, edge: str, set_at: int, by: str, here: str, way_back=None,
                way_back_by=None, corner_edge: Optional[Callable[[str, str], str]] = None,
                live: Optional[Callable[[str], bool]] = None):
    """Compatibility wrapper; advertised caps select the picture or old-peer path."""
    from core import crossing_state, protocol
    capable = {settings.get("machine_id", "")}
    capable.update(p.get("id") for p in settings.get("peers", []) if "layout" in p.get("caps", []))
    read = dict(edge=edge, set_at=set_at, by=protocol.read_id(by))
    if isinstance(way_back, bool):
        read.update(way_back=way_back, way_back_by=protocol.read_id(way_back_by))
    out = crossing_state.receive_arrangement(settings, peer, read,
             platform="mac" if "mac" in here.lower() else "pc", capable=capable,
             live=live, gone=None, now=time.time())
    settings.clear()
    settings.update(out["settings"])
    return out["changed"], []


def _zoned(entry: dict) -> bool:
    """A machine that crosses by zones: a desktop with an id, never a phone."""
    return bool(entry.get("id")) and entry.get("port") != 0


def default_side(settings: dict) -> bool:
    """The Mac's one paired machine with no side holds Right, the side its Crossing page shows, so
    its edge crosses from pairing rather than from the first save. Unstamped and with no author, so a
    side either machine chooses wins over it. Only with one desktop, as the page counts them: a
    second, even the one migrated from 1.4.x, may hold Right already. True when it wrote the side."""
    desktops = peerlist.desktops(settings["peers"])
    if len(desktops) != 1:
        return False
    entry = desktops[0]
    if not _zoned(entry) or entry.get("side") in EDGES:
        return False
    entry.update(side="right", side_set_at=0, side_by="")
    return True


def ensure_zones(settings: dict) -> bool:
    """Compatibility loader: seed intent and compile instead of persisting off zones."""
    from core import crossing_state
    out = crossing_state.migrate(settings, _platform(settings))
    settings.clear()
    settings.update(out["settings"])
    return out["changed"]


def has_way(settings: dict, peer: str) -> bool:
    """Whether a zone in use leads to `peer` and can fire: a corner or the notch, or an edge or thirds
    of a side that is set. What `arrangement`'s `way_back` says to that machine."""
    sides = {entry.get("id"): entry.get("side", "") for entry in settings["peers"]}
    return any(zone.get("peer") == peer and zone.get("off") is not True and _stretch(zone, sides, settings["peers"])
               for zone in settings["zones"])


def crosses_on_map(settings: dict, peer: str) -> bool:
    """Whether an active edge, part or corner crosses the peer's contact on the map."""
    sides = {entry.get("id"): entry.get("side", "") for entry in settings["peers"]}
    return any(zone.get("peer") == peer and zone.get("kind") in ("edge", "part", "corner")
               and zone.get("off") is not True and _stretch(zone, sides, settings["peers"])
               for zone in settings["zones"])


def blocked_sentence(settings: dict, peer: str, here: str, live: Optional[Callable[[str], bool]] = None) -> str:
    """The Crossing page's sentence for a machine whose side another machine's edge already holds and
    that has no way in of its own; "" otherwise, and "" for a holder with no link now, which gives way."""
    here = tr(HERE_NAMES[here]) if here in HERE_NAMES else here
    entry = _entry(settings, peer) or {}
    side = entry.get("side")
    if side not in EDGES or has_way(settings, peer):
        return ""
    holders = _side_holders(settings, peer, live)
    holder = holders[0] if holders else None
    if holder is None:
        return ""
    peers = settings["peers"]
    name = peerlist.label_for(peers, peer_id=peer) or peerlist.UNNAMED
    other = peerlist.label_for(peers, peer_id=holder) or peerlist.UNNAMED
    # Translators: {here} is this PC or Mac; {side} is the side of its screen.
    return tr("{name} has no way in: {other} already leads from the {side} edge of {here}. Give each its own thirds of that edge by moving the machines on the map, or move one to another side.").format(name=name, other=other, side=tr(EDGE_NAMES[side]), here=here)


def no_way_back_sentence(settings: dict, peer: str) -> str:
    """The Crossing page's sentence for a machine that said, in its `arrangement`, that none of its
    zones leads to this one; "" otherwise."""
    entry = _entry(settings, peer) or {}
    if entry.get("way_back") is not False:
        return ""
    name = peerlist.label_for(settings["peers"], peer_id=peer) or peerlist.UNNAMED
    other_id = entry.get("way_back_by")
    other_bytes = _id_bytes(other_id)
    if other_bytes is not None and other_bytes != _id_bytes(settings.get("machine_id")):
        other = peerlist.label_for(settings["peers"], peer_id=other_id) or tr("another machine")
        side = entry.get("side")
        facing = return_edge.OPPOSITE[side] if side in EDGES else None
        if facing:
            return tr("{name} has {other} on its {facing} too, so none of its edges leads back here. On {name}'s Crossing page, share that side by thirds or move one of them.").format(name=name, other=other, facing=tr(EDGE_NAMES[facing]))
        return tr("{name} has {other} too, so none of its edges leads back here. On {name}'s Crossing page, share that side by thirds or move one of them.").format(name=name, other=other)
    return tr("{name} has no way back to this machine: none of its edges leads here. Its own Crossing page says why.").format(name=name)


def way_back_by(settings: dict, peer: str) -> Optional[str]:
    """The machine whose active edge or part zone holds `peer`'s side here, when no way leads to it."""
    return _side_holder(settings, peer) if not has_way(settings, peer) else None


def way_back_state(settings: dict) -> dict:
    """What this machine tells every zoned peer about its way back, keyed by peer id."""
    return {entry["id"]: (has_way(settings, entry["id"]), way_back_by(settings, entry["id"]))
            for entry in settings["peers"] if _zoned(entry)}


def _side_holder(settings: dict, peer: str) -> Optional[str]:
    """The first active edge or part zone holding `peer`'s side, in zone order."""
    holders = _side_holders(settings, peer)
    return holders[0] if holders else None


def _side_holders(settings: dict, peer: str, live: Optional[Callable[[str], bool]] = None) -> list:
    """Every machine with an active edge or part zone over `peer`'s side, in zone order; with `live`,
    only those with a link up now."""
    entry = _entry(settings, peer) or {}
    side = entry.get("side")
    if side not in EDGES:
        return []
    sides = {item.get("id"): item.get("side", "") for item in settings["peers"]}
    holders = []
    for zone in settings["zones"]:
        owner = zone.get("peer")
        if (owner != peer and owner not in holders and zone.get("off") is not True
                and zone.get("kind") in ("edge", "part")
                and any(_stretch_side(stretch) == side for stretch in _stretch(zone, sides, settings["peers"]))
                and (live is None or live(owner))):
            holders.append(owner)
    return holders


def settle(settings: dict, peer: str, here: str, live=None) -> list:
    """No-op: compilation already assigns every stretch once."""
    return []


def give_way(settings: dict, peer: str, here: str, live=None) -> list:
    """No-op: a sleeping machine retains its intent and picture."""
    return []


def presence_due(settings: dict, live, gone) -> bool:
    """No-op: no aside state remains to settle."""
    return False


def settle_presence(settings: dict, here: str, live, gone) -> list:
    """No-op: events recompile instead of restoring zone history."""
    return []


def gone_sentence(settings: dict, peer: str, here: str, gone: Callable[[str], bool]) -> str:
    """The Crossing page's sentence for a machine that has removed this pairing, "" for any other."""
    here = tr(HERE_NAMES[here]) if here in HERE_NAMES else here
    if not gone(peer):
        return ""
    name = peerlist.label_for(settings["peers"], peer_id=peer) or peerlist.UNNAMED
    if has_way(settings, peer):
        # Translators: {here} is this PC or Mac.
        return tr("{name} no longer has this pairing. If another machine takes its side, {here} gives it up. Remove {name} here and pair the two again.").format(name=name, here=here)
    return tr("{name} no longer has this pairing, so it has no way in. Remove {name} here and pair the two again.").format(name=name)




def _turn_off_clashes(settings: dict, entry: dict, here: str, live=None) -> list:
    """No-op retained for older callers: compilation resolves overlap."""
    return []


def _sendable(entry: dict) -> bool:
    return bool(entry.get("id")) and entry.get("in_use", True) is True and entry.get("send") is True and entry.get("port") != 0


def shortcut_detail(key: str, mode: str) -> str:
    if mode == "next":
        template = tr("{key}, next in order")
    elif mode == "ticked":
        template = tr("{key}, next among ticked")
    else:
        template = tr("{key}, to where input last went")
    return template.format(key=key)


def shortcut_peer(peers: list, last: Optional[str], ready: Callable[[str], bool]) -> Optional[str]:
    """The machine the shortcut, the Send button and the tray send this machine's input to: the one
    it was last on while that one can take it, else the first in the list that can, else the one it
    was last on, else the first this machine sends to at all, so the refusal or the wake names it: a
    stale entry higher in the list must not take the shortcut from the machine just used (06-10-2026).
    Never a machine this one does not send to, a phone, or an entry with no id yet. `ready(id)` says
    whether one can take input now."""
    candidates = [entry["id"] for entry in peers if _sendable(entry)]
    if last in candidates and ready(last):
        return last
    fallback = last if last in candidates else (candidates[0] if candidates else None)
    return next((ident for ident in candidates if ready(ident)), fallback)


def shortcut_order(peers: list, machine_id: str, positions: dict, mode: str = "next") -> list:
    """Sendable peers in drawing reading order, rotated after this machine, followed by home.

    `positions` maps machine ids to the frames returned by the arrangement drawing. Entries without
    a frame follow in peers-list order; `None` at the end is home.
    """
    entries = [entry for entry in peers if _sendable(entry)
               and (mode != "ticked" or entry.get("shortcut_cycle") is not False)]
    placed = []
    unplaced = []
    for index, entry in enumerate(entries):
        ident = entry["id"]
        frame = positions.get(ident)
        if frame is None:
            unplaced.append((index, ident))
        else:
            x, y, _width, height = frame
            placed.append((y + height / 2, x + _width / 2, height, index, ident))
    placed.sort(key=lambda item: (item[0], item[1], item[3]))
    rows = []
    for item in placed:
        centre_y, _centre_x, height, _index, ident = item
        if not rows or centre_y - rows[-1][0][0] > min(height, rows[-1][0][2]) / 2:
            rows.append([item])
        else:
            rows[-1].append(item)
    reading = [item[4] for row in rows for item in sorted(row, key=lambda item: (item[1], item[3]))]
    home = positions.get(machine_id)
    if home is not None:
        hx, hy, _hw, hh = home
        hcy, hcx = hy + hh / 2, hx + _hw / 2
        insertion = next((index for index, ident in enumerate(reading)
                          if positions[ident][1] + positions[ident][3] / 2 - hcy >
                          min(positions[ident][3], hh) / 2 or
                          (abs(positions[ident][1] + positions[ident][3] / 2 - hcy) <=
                           min(positions[ident][3], hh) / 2 and
                           positions[ident][0] + positions[ident][2] / 2 > hcx)), len(reading))
        reading[insertion:insertion] = []
        before = reading[:insertion]
        after = reading[insertion:]
        reading = after + before
    reading.extend(ident for _index, ident in unplaced)
    return reading + [None]


def next_target(settings: dict, order: list, current: Optional[str], ready: Callable[[Optional[str]], bool],
                held: Optional[Callable[[str], bool]] = None):
    """Next ready peer or home, and the peer ids skipped because they are not ready. A peer `held`
    back after refusing a moment ago is passed over unnamed: the owner ignores a move there, so
    choosing it would leave input where it is (14-10-2026)."""
    held = held or (lambda _ident: False)
    peers = [ident for ident in order if ident is not None]
    if current is None:
        start = 0
    else:
        start = peers.index(current) + 1 if current in peers else 0
    candidates = peers[start:]
    skipped = []
    for ident in candidates:
        if held(ident):
            continue
        if ready(ident):
            return ident, skipped
        skipped.append(ident)
    if current is None:
        return next((ident for ident in peers if not held(ident)), None), skipped
    return None, skipped


def hold_target(settings: dict, order: list, last: Optional[str], ready: Callable[[str], bool],
                held: Optional[Callable[[str], bool]] = None):
    """The Hold-style destination and any preceding peers that were not ready; `held` as next_target."""
    held = held or (lambda _ident: False)
    peers = [ident for ident in order if ident is not None and not held(ident)]
    candidates = peers
    if settings.get("shortcut_goes_to", "last") == "last" and last in peers:
        candidates = [last] + [ident for ident in peers if ident != last]
    skipped = []
    for ident in candidates:
        if ready(ident):
            return ident, skipped
        skipped.append(ident)
    return (peers[0] if peers else None), skipped


def missing_pairings(settings: dict, peer: str) -> list:
    """The machines that may drive this one and that a zone to `peer` cannot take onward, because
    they are not paired with `peer` (section 5, "What the Crossing page may offer"). A machine is
    judged only once it has linked, since its `paired` list arrives on a link."""
    return [entry for entry in settings["peers"]
            if entry.get("id") != peer and entry.get("in_use", True) is True and entry.get("allow_drive") is True and entry.get("linked") is True
            and peer not in (entry.get("paired_with") or [])]


def missing_sentence(settings: dict, peer: str, here: str) -> str:
    """The Crossing page's sentence for `missing_pairings`, "" when there are none."""
    here = tr(HERE_NAMES[here]) if here in HERE_NAMES else here
    missing = missing_pairings(settings, peer)
    if not missing:
        return ""
    peers = settings["peers"]
    names = [peerlist.label_for(peers, peer_id=entry.get("id")) or peerlist.UNNAMED for entry in missing]
    target = peerlist.label_for(peers, peer_id=peer) or peerlist.UNNAMED
    if len(names) == 1:
        # Translators: {here} is this PC or Mac; {name} and {target} are machine names.
        return tr("While {name} drives {here}, this way to {target} does nothing: {name} is not paired with {target}. Pair them to use it from there too.").format(name=names[0], here=here, target=target)
    if len(names) == 2:
        drivers = trc("ways alternatives", "{first} or {last}").format(first=names[0], last=names[1])
        # Translators: {drivers} is a list of machine names; {here} is this PC or Mac.
        return tr("While {drivers} drives {here}, this way to {target} does nothing: neither is paired with {target}. Pair them to use it from there too.").format(drivers=drivers, here=here, target=target)
    drivers = trc("ways alternatives", "{head} or {last}").format(head=trc("list", ", ").join(names[:-1]), last=names[-1])
    # Translators: {drivers} is a list of machine names; {here} is this PC or Mac.
    return tr("While {drivers} drives {here}, this way to {target} does nothing: none of them is paired with {target}. Pair them to use it from there too.").format(drivers=drivers, here=here, target=target)
