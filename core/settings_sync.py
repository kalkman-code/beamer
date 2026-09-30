"""Same on both machines: the Crossing and Design settings the Mac and the PC keep in step.

Pure, with no AppKit and no Qt.
Off by default. While it is on, a change to a shared setting at either end is sent to the other;
on every new connection each end announces its own state and the newer `set_at` stands, as the
arrangement does. Turning it on at one end turns it on at the other and carries this end's values
across; turning it off turns it off at both and leaves each end with what it has.

The wire speaks the Mac's names. The PC's settings are flat fields with a `crossing_` prefix on
some, so each end has an adapter here: `mac_values`/`apply_mac` read and write the Mac's raw
settings dict, `pc_values`/`apply_pc` the PC's Config. A corner names a place on the leaving
machine's screen, and the two machines face each other, so it crosses the wire in the Mac's frame
and the PC mirrors it across the border between them.

Never shared, whatever the switch says: the Mac's notch way in and notch look, the haptics,
whether crossings animate on each screen, the arrangement (it has its own stamped message, which
also reaches an older Beamer), and anything outside the Crossing and Design pages.
"""

from . import effects
from . import return_edge

# The ways in both machines have; the notch is the Mac's alone, and a received set keeps the
# receiver's own notch.
WAYS = ("shortcut", "edge", "part", "corner")
CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")
TRIGGER_STYLES = ("double_tap", "hold")
GLOW_STYLES = ("glow", "beam") + effects.EFFECT_IDS
GLOW_COLOURS = ("signal", "colourful", "ocean", "sunset", "mono") + effects.PACK_IDS
LENGTHS = tuple(value for value, _name in effects.LENGTHS)

CROSSING_KEYS = ("methods", "edge_parts", "corner", "resistance_px", "block_while_dragging",
                 "trigger_key", "trigger_style", "double_tap_ms")
DESIGN_KEYS = ("glow_style", "glow_colour", "effect_length", "shortcut_arrival", "shortcut_arrival_style")

# The PC's field for each wire key; the PC's corner also needs mirroring (see pc_values).
PC_FIELDS = {
    "methods": "crossing_methods", "edge_parts": "crossing_edge_parts", "corner": "crossing_corner",
    "resistance_px": "crossing_resistance_px", "block_while_dragging": "block_while_dragging",
    "trigger_key": "trigger_key", "trigger_style": "trigger_style", "double_tap_ms": "double_tap_ms",
    "glow_style": "glow_style", "glow_colour": "glow_colour", "effect_length": "effect_length",
    "shortcut_arrival": "shortcut_arrival", "shortcut_arrival_style": "shortcut_arrival_style",
}
# The Mac keeps the shortcut at the top of its settings and the rest in `crossing`.
MAC_TOP_LEVEL = ("trigger_key", "trigger_style", "double_tap_ms")


def mirror_corner(corner, edge):
    """`corner` as the machine across `edge` names the same place: the two screens face each other,
    so the corner flips across the border and keeps its place along it. Its own inverse."""
    vertical, horizontal = corner.split("_")
    if edge in ("top", "bottom"):
        vertical = "bottom" if vertical == "top" else "top"
    else:
        horizontal = "right" if horizontal == "left" else "left"
    return f"{vertical}_{horizontal}"


def _whole(value, low, high):
    return not isinstance(value, bool) and isinstance(value, int) and low <= value <= high


def _valid(key, value, trigger_keys):
    if key == "methods":
        return isinstance(value, list) and all(way in WAYS for way in value)
    if key == "edge_parts":
        return isinstance(value, list) and bool(value) and all(part in return_edge.PARTS for part in value)
    if key == "corner":
        return value in CORNERS
    if key == "resistance_px":
        return _whole(value, 0, 500)
    if key in ("block_while_dragging", "shortcut_arrival"):
        return isinstance(value, bool)
    if key == "trigger_key":
        return isinstance(value, str) and value in trigger_keys
    if key == "trigger_style":
        return value in TRIGGER_STYLES
    if key == "double_tap_ms":
        return _whole(value, 50, 2000)
    if key == "glow_style":
        return value in GLOW_STYLES
    if key == "glow_colour":
        return value in GLOW_COLOURS
    if key == "effect_length":
        return value in LENGTHS
    if key == "shortcut_arrival_style":
        return value in effects.SWITCH_STYLES
    return False


def message_data(on, set_at, values=None):
    """What a `settings` message carries: whether it is on, when that or a shared value last
    changed, and while on, the values."""
    data = {"on": bool(on), "set_at": int(set_at)}
    if on and values is not None:
        data["crossing"] = {key: values[key] for key in CROSSING_KEYS if key in values}
        data["design"] = {key: values[key] for key in DESIGN_KEYS if key in values}
    return data


def read(data, trigger_keys):
    """(on, set_at, values) from a `settings` message's data, or None when it is not one. A value
    this end cannot hold -- a key it does not have, an effect it does not know -- is left out
    rather than failing the rest. Never raises: a malformed message must not take a link down."""
    if not isinstance(data, dict) or not isinstance(data.get("on"), bool):
        return None
    set_at = data.get("set_at")
    if isinstance(set_at, bool) or not isinstance(set_at, (int, float)):
        return None
    values = {}
    for group, keys in (("crossing", CROSSING_KEYS), ("design", DESIGN_KEYS)):
        section = data.get(group)
        if not isinstance(section, dict):
            continue
        for key in keys:
            if key in section and _valid(key, section[key], trigger_keys):
                values[key] = list(section[key]) if isinstance(section[key], list) else section[key]
    return data["on"], int(set_at), values


def wins(theirs, mine):
    """Whether the peer's state replaces this end's: the newer stamp stands, and a tie changes
    nothing, so the copy that arrives over the second link is a no-op."""
    return int(theirs or 0) > int(mine or 0)


def next_stamp(held, now):
    """The stamp for a change made here: now, or one past the stamp held if that is not older.
    Two changes in one second would otherwise tie and the second be ignored at the other end, and
    a peer whose clock runs ahead would win every time."""
    return max(int(now), int(held or 0) + 1)


def arrived(data, set_at_here, trigger_keys, held=None):
    """What to do with a `settings` message that arrived: (on, set_at, values) to take in place of
    this end's state, or None to keep it -- malformed, or no newer than what is held here.

    Two ends that each changed something in the same second while apart meet with equal stamps
    and different values. The Mac's stands: the PC passes `held`, its own (on, values), and takes a
    tie that differs from it; the Mac never takes a tie, so nothing goes back and forth."""
    read_back = read(data, trigger_keys)
    if read_back is None:
        return None
    on, set_at, values = read_back
    if wins(set_at, set_at_here):
        return read_back
    if held is not None and int(set_at) == int(set_at_here or 0) and set_at:
        held_on, held_values = held
        if on != held_on or (on and changed(held_values, {**held_values, **values})):
            return read_back
    return None


def _in_order(values):
    """The ways and thirds in one fixed order, as sets are: the windows build them in tile order,
    and a saved list in another order is not a change."""
    values["methods"] = [way for way in WAYS if way in (values.get("methods") or [])]
    values["edge_parts"] = [part for part in return_edge.PARTS if part in (values.get("edge_parts") or [])]
    return values


def mac_values(raw):
    """The shared values in the Mac's raw settings dict (settings_store.config_to_raw)."""
    crossing = raw.get("crossing", {})
    values = {key: raw[key] if key in MAC_TOP_LEVEL else crossing.get(key)
              for key in CROSSING_KEYS + DESIGN_KEYS}
    return _in_order(values)


def apply_mac(raw, values):
    """A copy of the Mac's raw settings with `values` in place. The notch stays in `methods` if it
    was there: it is this Mac's own way in."""
    raw = dict(raw)
    crossing = dict(raw.get("crossing", {}))
    for key, value in values.items():
        if key in MAC_TOP_LEVEL:
            raw[key] = value
        elif key == "methods":
            crossing[key] = list(value) + (["notch"] if "notch" in crossing.get("methods", []) else [])
        else:
            crossing[key] = list(value) if isinstance(value, list) else value
    raw["crossing"] = crossing
    return raw


def pc_values(config):
    """The shared values in the PC's Config, the corner in the Mac's frame. A PC that has not
    learned where the Mac is mirrors as if it were on the right, the PC's own default."""
    values = {key: getattr(config, field) for key, field in PC_FIELDS.items()}
    values["corner"] = mirror_corner(values["corner"], config.mac_return_edge or "right")
    return _in_order(values)


def apply_pc(config, values):
    """Sets `values` on the PC's Config in place; the corner arrives in the Mac's frame."""
    for key, value in values.items():
        if key == "corner":
            value = mirror_corner(value, config.mac_return_edge or "right")
        setattr(config, PC_FIELDS[key], list(value) if isinstance(value, list) else value)


def changed(before, after):
    """Whether any shared value differs between two value dicts."""
    return any(before.get(key) != after.get(key) for key in CROSSING_KEYS + DESIGN_KEYS)


# What the window says. `peer` is "PC" on the Mac and "Mac" on the PC.
SHARED_PAGES = ("crossing", "design")


def scope(page, peer, on, own):
    """The line under a page's purpose: `own` (the page's usual "for this machine only") unless
    the page is kept in step and the switch is on."""
    if on and page in SHARED_PAGES:
        return f"Kept the same as your {peer}. Change it on either machine."
    return own


def switch_note(peer, on, peer_too_old):
    """The note under Overview's Same on both machines switch."""
    if peer_too_old:
        return f"Your {peer}'s Beamer is too old to keep settings in step. Update it to turn this on."
    if on:
        return f"The Crossing and Design pages match your {peer}'s. A change on either machine reaches the other."
    return (f"Off: the Crossing and Design pages are this machine's own. On, they match your {peer}'s, "
            "and a change on either reaches the other.")
