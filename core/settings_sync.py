"""Same on all machines: selected Crossing, Design, Shortcut and Names groups (WIRE.md section 10).

Pure, with no AppKit and no Qt.
Off by default. While it is on, a change to a shared setting at any machine is sent to every peer
that takes `settings`; on every new link each end announces its own state and the newer stamp
stands. Turning it on at one machine turns it on at the others and carries its values across;
turning it off turns it off everywhere and leaves each machine with what it has.

Newer is a higher `set_at`, or the same `set_at` and the larger `by` (the id of the machine that
made the change) compared as bytes, so there is no Mac to break a tie. A machine that takes a newer
state sends it on, unchanged, to every other peer that takes `settings`; an older or equal one
is not sent on, which is what stops it.

The wire speaks the Mac's names. The PC's settings are flat fields with a `crossing_` prefix on
some, so each end has an adapter here: `mac_values`/`apply_mac` read and write the Mac's raw
settings dict, `pc_values`/`apply_pc` the PC's Config. The shortcut is a way in the 1.4.x
settings, so the adapters read and write it there.

Never shared, whatever the switch says: zones (the ways, thirds and corner), `side`, the direction
switches, the Mac's notch way in and notch look, the haptics, whether crossings animate on each
screen, whether a full-screen app holds the edges, appearance, and per-system keyboard mapping.
Windows administrator mode is a per-PC registry preference, outside settings and this protocol.
"""

import base64
import copy
import binascii
import re
import time

from . import effects
from .i18n import N_, tr, trc
from .protocol import MACHINE_ID_SIZE, MAX_PEERS

TRIGGER_STYLES = ("double_tap", "hold")
GLOW_STYLES = ("glow", "beam") + effects.EFFECT_IDS
GLOW_COLOURS = ("signal", "colourful", "ocean", "sunset", "mono", "aurora") + effects.PACK_IDS
LENGTHS = tuple(value for value, _name in effects.LENGTHS)
# Keys a trigger cannot be, beside characters and media keys, which the receiver's own table rejects.
NOT_TRIGGERS = ("browser_back", "browser_forward", "backspace", "tab", "enter", "esc", "space", "print_screen")

CROSSING_KEYS = ("resistance_px", "block_while_dragging", "shortcut", "trigger_key", "shortcut_goes_to", "trigger_style", "double_tap_ms")
SHORTCUT_KEYS = ("shortcut", "trigger_key", "shortcut_goes_to", "trigger_style", "double_tap_ms")
CROSSING_GROUP_KEYS = ("resistance_px", "block_while_dragging")
DESIGN_KEYS = ("glow_style", "glow_colour", "effect_length", "effect_size", "effect_pace",
               "effect_size_fraction", "frame_rate", "shortcut_arrival", "shortcut_arrival_style")
GROUP_KEYS = {"crossing": CROSSING_GROUP_KEYS, "design": DESIGN_KEYS, "shortcut": SHORTCUT_KEYS, "names": ()}
ALL_GROUPS = tuple(GROUP_KEYS)
DESIGN_UNAVAILABLE = N_("Design not available yet: connect to this machine with Beamer 1.5.0 beta.6 or later.")
NEEDS_DESIGN_SYNC = N_("Needs beta.6 or later")
OLD_PEER_NOTE = N_("A connected machine is unavailable until it has beta.6 or later.")
NO_MACHINE_CONNECTED = N_("No paired machine is connected.")
GROUP_LABELS = {"crossing": N_("Crossing"), "design": N_("Design"), "shortcut": N_("Shortcut"), "names": N_("Names")}
SHARED_LABELS = {"Resistance": N_("Resistance"), "drag protection": N_("drag protection"),
                 "Shortcut": N_("Shortcut")}

MAX_STAMP = 2 ** 53 - 2
DAY = 24 * 60 * 60
SNAPSHOT_TTL = 30 * DAY


def normalise_groups(groups):
    """Known groups in the wire's stable order, ignoring unknown future groups."""
    if not isinstance(groups, (list, tuple, set)) or any(not isinstance(group, str) for group in groups):
        return ()
    selected = set(groups)
    return tuple(group for group in ALL_GROUPS if group in selected)


def groups_for_choice(groups):
    selected = normalise_groups(groups)
    return ALL_GROUPS if "everything" in groups else selected


def group_on(config, group):
    return config.same_on_both and group in normalise_groups(config.same_groups)


def _message_groups(data, current_groups=None):
    if "groups" not in data:
        if current_groups is None:
            return ALL_GROUPS
        # A peer from before groups (1.5.1) turning Same on while none is chosen here means every
        # group, as turning it on here does; kept empty, this end went on sharing nothing.
        return normalise_groups(current_groups) or (ALL_GROUPS if data.get("on") is True else ())
    groups = data.get("groups")
    if not isinstance(groups, list) or any(not isinstance(group, str) or group not in ALL_GROUPS for group in groups):
        return None
    return normalise_groups(groups)


def arrival_groups(data, current_groups):
    """Selected groups in an accepted message, retaining local selection for legacy peers."""
    return _message_groups(data, current_groups)


def same_snapshot(groups, values, now=None, value_groups=None, names=None):
    selected = normalise_groups(groups)
    included = selected if value_groups is None else normalise_groups(value_groups)
    keys = {key for group in included for key in GROUP_KEYS[group]}
    snapshot = {"groups": list(selected), "values": {key: values[key] for key in keys if key in values},
                "saved_at": int(time.time() if now is None else now)}
    if names is not None:
        snapshot["names"] = names
    return snapshot


def snapshot_available(snapshot, now=None):
    if (not isinstance(snapshot, dict) or type(snapshot.get("saved_at")) is not int
            or not isinstance(snapshot.get("groups"), list)
            or any(group not in ALL_GROUPS for group in snapshot["groups"])
            or not isinstance(snapshot.get("values"), dict)
            or any(key not in CROSSING_KEYS + DESIGN_KEYS for key in snapshot["values"])):
        return False
    names = snapshot.get("names", {})
    if (not isinstance(names, dict) or any(
            not isinstance(value, dict) or not isinstance(value.get("label"), str)
            or type(value.get("set_at")) is not int or not isinstance(value.get("by"), str)
            for value in names.values())):
        return False
    current = int(time.time() if now is None else now)
    return 0 <= current - snapshot["saved_at"] <= SNAPSHOT_TTL


def restore_snapshot(snapshot, now=None):
    if (not snapshot_available(snapshot, now) or not isinstance(snapshot.get("groups"), list)
            or not isinstance(snapshot.get("values"), dict)
            or "names" in snapshot and not isinstance(snapshot["names"], dict)):
        return None
    result = {"groups": list(normalise_groups(snapshot["groups"])),
              "values": {} if snapshot.get("selection_only") else dict(snapshot["values"])}
    if "names" in snapshot:
        result["names"] = dict(snapshot["names"])
    return result


def snapshot_for_arrival(data, held_stamp, trigger_keys, by_here, current_groups, current_values, now=None,
                         applied_values=None, merged=None):
    """The pre-update copy to save for an accepted update, or None when it changes no local values.
    `merged` is `merge`'s result: its values may be taken while the whole state is not newer."""
    current_groups = normalise_groups(current_groups)
    if merged is not None:
        on, incoming_values, groups = merged["on"], merged["values"], normalise_groups(merged["groups"])
        if applied_values is not None:
            incoming_values = applied_values
        applies = groups
    else:
        taken = arrived(data, held_stamp, trigger_keys, by_here=by_here, now=now,
                        current_groups=current_groups)
        if taken is None:
            return None
        on, _stamp, incoming_values = taken
        if applied_values is not None:
            incoming_values = applied_values
        groups = _message_groups(data, current_groups)
        if groups is None:
            return None
        applies = groups if "groups" in data else current_groups
    changed_values = on and any(current_values.get(key) != value for key, value in incoming_values.items())
    if not changed_values and groups == current_groups:
        return None
    snapshot = same_snapshot(current_groups, current_values, now=now, value_groups=applies)
    if not changed_values:
        snapshot["selection_only"] = True
    return snapshot


def revert_groups(snapshot, current_groups):
    """Value restorations travel under the current policy; selection-only undo restores policy."""
    return list(normalise_groups(snapshot["groups"] if snapshot.get("selection_only") else current_groups))

# The PC's field for each wire key that is a field of its own; `shortcut` is a way in
# `crossing_methods`.
PC_FIELDS = {
    "resistance_px": "crossing_resistance_px", "block_while_dragging": "block_while_dragging",
    "trigger_key": "trigger_key", "shortcut_goes_to": "shortcut_goes_to",
    "trigger_style": "trigger_style", "double_tap_ms": "double_tap_ms",
    "glow_style": "glow_style", "glow_colour": "glow_colour", "effect_length": "effect_length",
    "effect_size": "effect_size", "effect_pace": "effect_pace", "effect_size_fraction": "effect_size_fraction",
    "frame_rate": "frame_rate",
    "shortcut_arrival": "shortcut_arrival", "shortcut_arrival_style": "shortcut_arrival_style",
}
# The Mac keeps the shortcut's key at the top of its settings and the rest in `crossing`.
MAC_TOP_LEVEL = ("trigger_key", "trigger_style", "double_tap_ms")


def _whole(value, low, high):
    return not isinstance(value, bool) and isinstance(value, int) and low <= value <= high


def _valid(key, value, trigger_keys):
    if key == "resistance_px":
        return _whole(value, 0, 500)
    if key in ("block_while_dragging", "shortcut", "shortcut_arrival"):
        return isinstance(value, bool)
    if key == "shortcut_goes_to":
        return value in ("last", "next", "ticked")
    if key == "trigger_key":
        return isinstance(value, str) and value in trigger_keys and value not in NOT_TRIGGERS
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
    if key == "effect_size":
        return value in tuple(size for size, _name in effects.SIZES)
    if key == "effect_pace":
        return effects.valid_effect_pace(value)
    if key == "effect_size_fraction":
        return effects.valid_effect_size_fraction(value)
    if key == "frame_rate":
        return value in tuple(value for value, _name, _divisor in effects.FRAME_RATES)
    if key == "shortcut_arrival_style":
        return value in effects.SWITCH_STYLES
    return False


def message_data(on, set_at, values=None, by="", design_state=None, names=None, layout_state=None, groups=None,
                 stamps=None):
    """What a `settings` message carries: whether it is on, when that or a shared value last
    changed and by which machine (`by`, its id as base64), and while on, the values, each value's
    own stamp when `stamps` (held_stamps) is given, and optional per-machine names. Optional direct
    Design and layout states keep their own stamps."""
    selected = None if groups is None else normalise_groups(groups)
    data = {"on": bool(on), "set_at": int(set_at), "by": by}
    if selected is not None:
        data["groups"] = list(selected)
    if on and values is not None:
        crossing = GROUP_KEYS["crossing"] + GROUP_KEYS["shortcut"]
        data["crossing"] = {key: values[key] for key in crossing if key in values
                             and (selected is None or ("crossing" if key in CROSSING_GROUP_KEYS else "shortcut") in selected)}
        data["design"] = {key: values[key] for key in DESIGN_KEYS if key in values
                          and (selected is None or "design" in selected)}
        if stamps is not None:
            data["stamps"] = {key: [stamps[key][0], stamps[key][1]]
                              for section in ("crossing", "design") for key in data[section] if key in stamps}
    if design_state is not None:
        data["design_sync"] = design_state
    if on and names is not None and (selected is None or "names" in selected):
        data["names"] = names
    if layout_state is not None:
        data["layout"] = layout_state
    return data


def names_arrived(data, held, own_id="", now=None, current_groups=None, policy_groups=None):
    """The newer valid name overrides in a Same-on message, plus this machine's name if included."""
    groups = _message_groups(data, current_groups) if isinstance(data, dict) else None
    if (not _valid_envelope(data, now) or not data["on"] or groups is None
            or policy_groups is not None and "names" not in normalise_groups(policy_groups)
            or "names" in data and "names" not in groups):
        return {}, None
    if not isinstance(data.get("names"), dict):
        return {}, None
    held = held if isinstance(held, dict) else {}
    accepted = {}
    own_name = None
    current_time = time.time() if now is None else now
    for ident, state in data["names"].items():
        ident_bytes = _id(ident)
        if ident_bytes is None or len(ident_bytes) != MACHINE_ID_SIZE or not isinstance(state, dict):
            continue
        label = state.get("label")
        set_at = state.get("set_at")
        by = state.get("by")
        author = _id(by)
        if not isinstance(label, str):
            continue
        label = label.strip()
        if (len(label) > 64 or type(set_at) is not int or author is None
                or len(author) != MACHINE_ID_SIZE
                or not 0 <= set_at <= MAX_STAMP or set_at > current_time + DAY):
            continue
        entry = {"label": label, "set_at": set_at, "by": by}
        previous = held.get(ident)
        previous_at = previous.get("set_at", 0) if isinstance(previous, dict) else 0
        previous_by = _id(previous.get("by", "")) if isinstance(previous, dict) else b""
        if type(previous_at) is not int or previous_at < 0 or previous_at > MAX_STAMP:
            previous_at, previous_by = 0, b""
        if previous_by is None:
            previous_by = b""
        if set_at > previous_at or (set_at == previous_at and author > previous_by):
            accepted[ident] = entry
            if ident == own_id:
                own_name = entry["label"]
    return accepted, own_name


def names_from_settings(settings):
    """The names this machine can announce, including peers whose labels are still empty."""
    own_id = settings.get("machine_id", "")
    names = {}
    if own_id:
        own_stamp = settings.get("name_set_at", 0)
        names[own_id] = {"label": settings.get("name", ""),
                         "set_at": own_stamp,
                         "by": (settings.get("name_by") or own_id) if own_stamp else own_id}
    for entry in settings.get("peers", []):
        ident = entry.get("id")
        if ident:
            label_stamp = entry.get("label_set_at", 0)
            names[ident] = {"label": entry.get("label", ""),
                            "set_at": label_stamp,
                            "by": (entry.get("label_by") or own_id) if label_stamp else own_id}
    return names


def apply_names(settings, updates):
    """Keep accepted name stamps and values in the settings dict; return whether anything changed."""
    changed = False
    own_id = settings.get("machine_id", "")
    for ident, state in updates.items():
        if ident == own_id:
            fields = (("name", state["label"]), ("name_set_at", state["set_at"]), ("name_by", state["by"]))
            target = settings
        else:
            target = next((entry for entry in settings.get("peers", []) if entry.get("id") == ident), None)
            if target is None:
                continue
            fields = (("label", state["label"]), ("label_set_at", state["set_at"]), ("label_by", state["by"]))
        for key, value in fields:
            if target.get(key) != value:
                target[key] = value
                changed = True
    return changed


def restamp_names(settings, updates, by, now=None):
    """Names restored locally need newer per-entry stamps so peers accept the restoration."""
    held = names_from_settings(settings)
    stamp_now = time.time() if now is None else now
    result = {}
    for ident, state in updates.items():
        previous = held.get(ident, {})
        result[ident] = {"label": state["label"],
                         "set_at": next_stamp(previous.get("set_at", 0), stamp_now), "by": by}
    return result


def read_layout_state(data, now=None):
    """A validated independently stamped shared tile picture and display geometry, or None when malformed."""
    if not _valid_envelope(data, now):
        return None
    state = data.get("layout")
    if not isinstance(state, dict) or type(state.get("set_at")) is not int or _id(state.get("by", "")) is None:
        return None
    stamp = state["set_at"]
    if stamp < 0 or stamp > MAX_STAMP or stamp > (time.time() if now is None else now) + DAY:
        return None
    tiles = state.get("tiles")
    if not isinstance(tiles, dict) or not tiles or len(tiles) > MAX_PEERS + 1:
        return None
    accepted = {}
    for machine, tile in tiles.items():
        ident = _id(machine)
        if not machine or ident in (None, b"") or not any(ident) or not isinstance(tile, dict):
            return None
        if any(type(tile.get(key)) is not int for key in ("x", "y", "w", "h")) or tile["w"] < 1 or tile["h"] < 1:
            return None
        accepted_tile = {key: tile[key] for key in ("x", "y", "w", "h")}
        if "name" in tile:
            if isinstance(tile["name"], str) and tile["name"].strip() and len(tile["name"]) <= 64:
                accepted_tile["name"] = tile["name"].strip()
        if "displays" in tile:
            screens = tile["displays"]
            if not isinstance(screens, dict) or not screens or len(screens) > 16:
                return None
            accepted_screens = {}
            for key, rect in screens.items():
                if (not isinstance(key, str) or not key.isascii() or not 1 <= len(key) <= 64
                        or not isinstance(rect, dict)
                        or any(type(rect.get(name)) is not int for name in ("x", "y", "w", "h"))
                        or rect["w"] < 1 or rect["h"] < 1):
                    return None
                accepted_screens[key] = {name: rect[name] for name in ("x", "y", "w", "h")}
            accepted_tile["displays"] = accepted_screens
            offsets = tile.get("display_offsets", {})
            if (not isinstance(offsets, dict) or len(offsets) > 16
                    or any(key not in accepted_screens or not isinstance(offset, dict)
                           or type(offset.get("x")) is not int or type(offset.get("y")) is not int
                           for key, offset in offsets.items())):
                return None
            if offsets:
                accepted_tile["display_offsets"] = {
                    key: {"x": offset["x"], "y": offset["y"]} for key, offset in offsets.items()
                }
        elif tile.get("display_offsets"):
            return None
        accepted[machine] = accepted_tile
    return {"set_at": stamp, "by": state.get("by", ""), "tiles": accepted}


def layout_arrived(state, set_at_here, by_here=""):
    """Whether a validated shared tile picture is newer than the one held locally."""
    if not isinstance(state, dict) or _id(state.get("by", "")) is None or type(state.get("set_at")) is not int:
        return False
    return (state["set_at"] > int(set_at_here or 0) or
            (state["set_at"] == int(set_at_here or 0) and _id(state["by"]) > (_id(by_here) or b"")))


def layout_recipients(peers, source=None):
    """Linked peers that advertise both settings and the shared layout capability."""
    return [peer for peer, capabilities in peers.items()
            if peer != source and "settings" in capabilities and "layout" in capabilities]


def for_capabilities(data, capabilities):
    """Drop optional settings members a peer did not advertise support for."""
    result = dict(data)
    if "profiles" not in capabilities:
        result.pop("desk", None)
    if "layout" not in capabilities:
        result.pop("layout", None)
    elif "displays" not in capabilities and isinstance(result.get("layout"), dict):
        layout = dict(result["layout"])
        tiles = layout.get("tiles")
        if isinstance(tiles, dict):
            layout["tiles"] = {machine: {key: value for key, value in tile.items()
                                         if key not in ("displays", "display_offsets")}
                               if isinstance(tile, dict) else tile for machine, tile in tiles.items()}
        result["layout"] = layout
    return result


def desk_message_data(envelope_data, desk):
    """Repeat only the settings envelope, keeping the bounded desk in its own frame."""
    result = {key: copy.deepcopy(envelope_data[key]) for key in ("on", "set_at", "by", "groups")
              if key in envelope_data}
    result["desk"] = copy.deepcopy(desk)
    return result


def read_design_state(data, trigger_keys, now=None):
    """A directly advertised local Design state from a settings message, or None if absent or
    invalid. Each supported value stands alone, as for Same on all machines. The outer settings
    envelope is validated first, so a valid optional member cannot rescue a malformed message."""
    if not _valid_envelope(data, now):
        return None
    state = data.get("design_sync")
    if not isinstance(state, dict) or type(state.get("set_at")) is not int:
        return None
    by = state.get("by", "")
    if _id(by) is None:
        return None
    set_at = state["set_at"]
    if set_at < 0 or set_at > MAX_STAMP or set_at > (time.time() if now is None else now) + DAY:
        return None
    values = state.get("values")
    if not isinstance(values, dict):
        return None
    accepted = {key: values[key] for key in DESIGN_KEYS
                if key in values and _valid(key, values[key], trigger_keys)}
    _normalise_design(accepted, default_size="effect_size" not in values)
    result = {"set_at": set_at, "by": by, "values": accepted}
    if "path" in state:
        path = state["path"]
        if (not isinstance(path, list) or not 1 <= len(path) <= MAX_PEERS + 1
                or any(not item or _id(item) is None for item in path)
                or len({_id(item) for item in path}) != len(path)):
            return None
        result["path"] = list(path)
    return result


def design_arrived(state, set_at_here, by_here=""):
    """Whether a validated Design state is newer than the one this machine currently holds."""
    if not isinstance(state, dict):
        return False
    by = _id(state.get("by", ""))
    if by is None:
        return False
    held_by = _id(by_here) or b""
    set_at = state.get("set_at")
    return type(set_at) is int and (set_at > int(set_at_here or 0) or
                                    (set_at == int(set_at_here or 0) and by > held_by))


def followed_design(current, state, source, follower, same_on, set_at_here, by_here,
                    own_id="", force=False):
    """Adopt a selected leader, then compare versions against that leader's last adopted state.
    The returned stamp is the adoption cursor, never this machine's advertisement version."""
    if state is None or source != follower or same_on:
        return None
    path = list(state.get("path", []))
    if not path:
        path = list({_id(item): item for item in (state.get("by"), source) if item}.values())
    if own_id and any(_id(item) == _id(own_id) for item in path):
        return {"cycle": True}
    if not force and not design_arrived(state, set_at_here, by_here):
        return None
    values = dict(current)
    values.update(_with_continuous({key: value for key, value in state["values"].items() if key in DESIGN_KEYS}))
    changed = any(current.get(key) != values.get(key) for key in DESIGN_KEYS)
    result = {"changed": changed, "values": values, "set_at": state["set_at"], "by": state["by"]}
    if own_id:
        result["path"] = path + [own_id]
    return result


def follow_rows(desktops, caps, states, following=""):
    """Match design with's machines, as [(entry, why_not)]: the paired desktops linked now, and the
    one followed even while it is away so the selection still shows. `caps` is {id: capabilities}
    for the live links only, `states` the Design states received. `why_not` is None for a machine
    that can be chosen, NEEDS_DESIGN_SYNC for one whose link lacks `design_sync`, else
    DESIGN_UNAVAILABLE until its state arrives. No version string is read: capability decides."""
    rows = []
    for entry in desktops:
        peer = entry.get("id", "")
        if not peer or (peer not in caps and peer != following):
            continue
        known = caps.get(peer)
        if known is not None and "design_sync" not in known:
            rows.append((entry, NEEDS_DESIGN_SYNC))
        else:
            rows.append((entry, None if peer in states else DESIGN_UNAVAILABLE))
    return rows


def follow_note(rows):
    """The one line under Match design with when nothing is followed and no machine can be chosen,
    or None when one can. Offline machines are not listed, so they never set it."""
    reasons = [why_not for _entry, why_not in rows]
    if not reasons:
        return tr(NO_MACHINE_CONNECTED)
    if None in reasons:
        return None
    return tr(OLD_PEER_NOTE if NEEDS_DESIGN_SYNC in reasons else DESIGN_UNAVAILABLE)


STOP_FOLLOWING = "Stop following"


def follow_lock(following, same_on, desktops):
    """The paired desktop whose Design this machine follows, while the followed Design controls
    (DESIGN_KEYS: style, landing animation, colour, length, size) are locked; None while they are
    this machine's to change. Same on all machines pauses following, so it unlocks them. The lock
    holds while the followed machine is away: its last design stays until Stop following."""
    if not following or same_on:
        return None
    return next((entry for entry in desktops if entry.get("id") == following), None)


def following_banner(name):
    return tr("Following {name}'s design").format(name=name)


def design_change_stamp(current, incoming, set_at_here, source_state, own_id, now=None):
    """A Same-on Design change announced as this machine's direct state, or None if unchanged."""
    if not isinstance(current, dict) or not isinstance(incoming, dict):
        return None
    if not any(key in incoming and current.get(key) != incoming[key] for key in DESIGN_KEYS):
        return None
    held = int(set_at_here or 0)
    if isinstance(source_state, dict) and type(source_state.get("set_at")) is int:
        held = max(held, source_state["set_at"])
    stamp = next_stamp(held, time.time() if now is None else now)
    return {"set_at": stamp, "by": own_id}


def read(data, trigger_keys, now=None, current_groups=None):
    """(on, set_at, values) from a `settings` message's data, or None when it is not one. A value
    this end cannot hold -- a key it does not have, an effect it does not know, a zone -- is left
    out rather than failing the rest. Never raises: a malformed message must not take a link down."""
    if not _valid_envelope(data, now):
        return None
    set_at = data["set_at"]
    selected = arrival_groups(data, current_groups)
    if selected is None:
        return None
    values = {}
    for group, keys in (("crossing", CROSSING_KEYS), ("design", DESIGN_KEYS)):
        section = data.get(group)
        if not isinstance(section, dict):
            continue
        for key in keys:
            value_group = "shortcut" if group == "crossing" and key in SHORTCUT_KEYS else group
            if value_group in selected and key in section and _valid(key, section[key], trigger_keys):
                values[key] = section[key]
    _normalise_design(values)
    return data["on"], set_at, values


# Machine ids travel as PAIRING.md's b64 (URL-safe, unpadded); the standard alphabet and padding are read too.
_ID_SHAPE = re.compile(r"[A-Za-z0-9_+/-]*={0,2}")


def _id(text):
    """The bytes of a `by`, or None when it is not the base64 of a 16 byte machine id. A message with none is from nobody, b''."""
    if text == "":
        return b""
    if not isinstance(text, str):
        return None
    if not _ID_SHAPE.fullmatch(text):
        return None
    try:
        ident = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None
    return ident if len(ident) == MACHINE_ID_SIZE else None


def _valid_envelope(data, now=None):
    if not isinstance(data, dict) or not isinstance(data.get("on"), bool):
        return False
    set_at = data.get("set_at")
    if type(set_at) is not int or _id(data.get("by", "")) is None:
        return False
    return (0 <= set_at <= MAX_STAMP
            and set_at <= (time.time() if now is None else now) + DAY)


def _normalise_design(values, default_size=False):
    if "effect_pace" in values or "effect_length" in values:
        pace_value, _fraction, length, _size = effects.design_values(
            values.get("effect_pace"), None, values.get("effect_length"))
        values.update(effect_pace=pace_value, effect_length=length)
    if "effect_size_fraction" in values or "effect_size" in values or default_size:
        _pace, fraction, _length, size = effects.design_values(
            None, values.get("effect_size_fraction"), size=values.get("effect_size"))
        values.update(effect_size_fraction=fraction, effect_size=size)


def next_stamp(held, now):
    """The stamp for a change made here: now, or one past the stamp held if that is not older.
    Two changes in one second would otherwise tie and the second be ignored at the other end, and
    a peer whose clock runs ahead would win every time."""
    return max(int(now), int(held or 0) + 1)


def rename_peer(entry, text, by, now=None):
    """The fields to store for a local rename of a paired machine."""
    now = time.time() if now is None else now
    return {"label": text.strip()[:64],
            "label_set_at": next_stamp(entry.get("label_set_at", 0), now),
            "label_by": by}


def arrived(data, set_at_here, trigger_keys, by_here="", now=None, current_groups=None):
    """What to do with a `settings` message that arrived: (on, set_at, values) to take in place of
    this end's state, or None to keep it -- malformed, a stamp more than a day ahead of `now` or
    above 2^53 - 2, or no newer than what is held here.

    Newer is a higher stamp, or the same stamp and the larger `by` as bytes; `by_here` is the id
    that made the change this end holds. Equal in both is the copy that arrives over a second
    link, and changes nothing, so a state sent on never comes back round."""
    read_back = read(data, trigger_keys, now, current_groups)
    by = _id(data.get("by", "")) if read_back is not None else None
    if read_back is None or by is None:
        return None
    set_at = read_back[1]
    if set_at > MAX_STAMP or set_at > (time.time() if now is None else now) + DAY:
        return None
    mine = int(set_at_here or 0)
    if set_at > mine or (set_at == mine and by > (_id(by_here) or b"")):
        return read_back
    return None


# Each shared value's own stamp, (set_at, by), since 1.5.2 (WIRE.md section 10, "stamps"): an edit
# carries only what a person changed, so values a machine missed or never set no longer ride along
# with someone's edit and overwrite real choices everywhere (rc14, 10-10-2026).
STAMPED_KEYS = CROSSING_KEYS + DESIGN_KEYS


def _stamp_pair(value, now=None):
    """(set_at, by) from a stored or sent `[set_at, by]`, or None when it is not one."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    set_at, by = value
    if (type(set_at) is not int or not 0 <= set_at <= MAX_STAMP
            or set_at > (time.time() if now is None else now) + DAY or _id(by) is None):
        return None
    return set_at, by


def held_stamps(stamps, set_at, by):
    """Every shared key's (set_at, by) as held here. A key with no stamp of its own takes the whole
    state's, which is what it meant before 1.5.2: a fresh machine's are (0, ""), so it adopts the
    group's values and never sends its defaults over anyone's choices."""
    stamps = stamps if isinstance(stamps, dict) else {}
    whole = (int(set_at or 0), by if isinstance(by, str) and _id(by) is not None else "")
    return {key: _stamp_pair(stamps.get(key), now=MAX_STAMP) or whole for key in STAMPED_KEYS}


def stored_stamps(stamps):
    """The stamps as the settings file keeps them, `{key: [set_at, by]}`."""
    return {key: [set_at, by] for key, (set_at, by) in stamps.items()}


def stamp_changes(stamps, before, after, by, floor=0, now=None):
    """`stamps` with a new stamp on each key whose value differs between `before` and `after`, a
    change made here by `by`; and that stamp, or None when nothing changed. The stamp is newer than
    every one held, and than `floor` (the whole state's), as next_stamp makes it."""
    changed_keys = {key for key in STAMPED_KEYS if key in after and before.get(key) != after.get(key)}
    # A slider and its preset are one choice: stamped apart, a newer preset held elsewhere pulled a
    # newer slider back to itself (design_values) and the person's last choice was lost.
    for pair in (("effect_length", "effect_pace"), ("effect_size", "effect_size_fraction")):
        if changed_keys & set(pair):
            changed_keys |= {key for key in pair if key in after}
    if not changed_keys:
        return dict(stamps), None
    top = max([floor or 0] + [set_at for set_at, _by in stamps.values()])
    stamp = next_stamp(top, time.time() if now is None else now)
    result = dict(stamps)
    for key in changed_keys:
        result[key] = (stamp, by)
    return result, stamp


def _newer(one, other):
    return one[0] > other[0] or (one[0] == other[0] and (_id(one[1]) or b"") > (_id(other[1]) or b""))


def _value_group(key):
    return "crossing" if key in CROSSING_GROUP_KEYS else "shortcut" if key in SHORTCUT_KEYS else "design"


def merge(data, stamps_here, set_at_here, trigger_keys, by_here="", on_here=False, now=None, current_groups=None):
    """What a `settings` message brings, key by key: None when it is not one, else a dict of
    `policy` -- (on, set_at, values) as `arrived` gives it when the whole state is newer, which
    decides on, off and the groups, else None -- `on` and `groups` as they stand after it,
    `values`, the shared values in those groups whose own stamp is newer than the one held here,
    none while Same is off, and `stamps`, those keys' stamps.

    A peer from before 1.5.2 sends no stamps: each of its values counts as stamped with its whole
    state, which is what its state meant, so it behaves as it did against a peer of its own."""
    read_back = read(data, trigger_keys, now, current_groups)
    by = _id(data.get("by", "")) if read_back is not None else None
    if read_back is None or by is None:
        return None
    on, set_at, values = read_back
    if set_at > (time.time() if now is None else now) + DAY:
        return None
    policy = arrived(data, set_at_here, trigger_keys, by_here=by_here, now=now, current_groups=current_groups)
    final_on = policy[0] if policy is not None else bool(on_here)
    final_groups = (arrival_groups(data, current_groups) if policy is not None
                    else normalise_groups(current_groups or ()))
    sent = data.get("stamps") if isinstance(data.get("stamps"), dict) else {}
    whole = (set_at, data.get("by", ""))
    held = held_stamps(stamps_here, set_at_here, by_here)
    taken, taken_stamps, answer = {}, {}, False
    if on and final_on:
        for key, value in values.items():
            if _value_group(key) not in final_groups:
                continue
            # A value's stamp may be newer than the state it travels in: one changed while its group
            # was off is stamped then, and the state only when Same next changes.
            stamp = _stamp_pair(sent.get(key), now) if "stamps" in data else whole
            if stamp is None:
                continue
            if _newer(stamp, held[key]):
                taken[key], taken_stamps[key] = value, stamp
            elif _newer(held[key], stamp):
                answer = True
    # `answer`: this machine holds a value newer than the sender's, so it sends its own state, or
    # the sender, having nothing newer to take from the message it sent, would never learn it.
    return {"policy": policy, "on": final_on, "groups": tuple(final_groups), "values": taken,
            "stamps": taken_stamps, "answer": answer}


def recipients(peers, source=None):
    """Whom a state goes to: every peer that lists `settings` among its capabilities, except the
    one it came from. `peers` maps a peer's id to its capabilities."""
    return [peer for peer, capabilities in peers.items() if peer != source and "settings" in capabilities]


def _in_order(values):
    """The shortcut is a bool, whatever the adapter read."""
    values["shortcut"] = bool(values.get("shortcut"))
    return values


def _with_shortcut(methods, on):
    """`methods` with the shortcut on or off and every other way as it was."""
    ways = [way for way in methods if way != "shortcut"]
    return ways + ["shortcut"] if on else ways


def mac_values(raw):
    """The shared values in the Mac's raw settings dict (settings_store.config_to_raw)."""
    crossing = raw.get("crossing", {})
    values = {key: raw[key] if key in MAC_TOP_LEVEL else crossing.get(key)
              for key in CROSSING_KEYS + DESIGN_KEYS if key != "shortcut"}
    pace_value, fraction, length, size = effects.design_values(
        crossing.get("effect_pace"), crossing.get("effect_size_fraction"),
        crossing.get("effect_length"), crossing.get("effect_size"))
    values.update(effect_pace=pace_value, effect_size_fraction=fraction,
                  effect_length=length, effect_size=size)
    values["shortcut"] = "shortcut" in crossing.get("methods", [])
    return _in_order(values)


def _with_continuous(values):
    """`values` with the slider fields following a preset that arrives without them, as from a
    1.5.0 to 1.5.2 peer: kept as they were, the old pace or fraction would win over the new preset
    on the next read and the slider would snap back."""
    values = dict(values)
    if "effect_length" in values and "effect_pace" not in values:
        values["effect_pace"] = effects.design_values(length=values["effect_length"])[0]
    if "effect_size" in values and "effect_size_fraction" not in values:
        values["effect_size_fraction"] = effects.design_values(size=values["effect_size"])[1]
    return values


def apply_mac(raw, values):
    """A copy of the Mac's raw settings with `values` in place. The other ways, the notch among
    them, stay as they were: they are this Mac's own zones."""
    raw = dict(raw)
    crossing = dict(raw.get("crossing", {}))
    for key, value in _with_continuous(values).items():
        if key not in CROSSING_KEYS and key not in DESIGN_KEYS:
            continue
        if key in MAC_TOP_LEVEL:
            raw[key] = value
        elif key == "shortcut":
            crossing["methods"] = _with_shortcut(crossing.get("methods", []), value)
        else:
            crossing[key] = value
    raw["crossing"] = crossing
    return raw


def pc_values(config):
    """The shared values in the PC's Config."""
    values = {key: getattr(config, field) for key, field in PC_FIELDS.items()
              if key not in ("effect_pace", "effect_size_fraction")}
    values["effect_pace"] = getattr(config, "effect_pace", None)
    values["effect_size_fraction"] = getattr(config, "effect_size_fraction", None)
    pace_value, fraction, length, size = effects.design_values(
        getattr(config, "effect_pace", None), getattr(config, "effect_size_fraction", None),
        getattr(config, "effect_length", None), getattr(config, "effect_size", None))
    values.update(effect_pace=pace_value, effect_size_fraction=fraction,
                  effect_length=length, effect_size=size)
    values["shortcut"] = "shortcut" in config.crossing_methods
    return _in_order(values)


def apply_pc(config, values):
    """Sets `values` on the PC's Config in place."""
    for key, value in _with_continuous(values).items():
        if key not in CROSSING_KEYS and key not in DESIGN_KEYS:
            continue
        if key == "shortcut":
            config.crossing_methods = _with_shortcut(config.crossing_methods, value)
        else:
            setattr(config, PC_FIELDS[key], value)


def changed(before, after, groups=None):
    """Whether any shared value differs between two value dicts."""
    keys = CROSSING_KEYS + DESIGN_KEYS if groups is None else tuple(
        key for group in normalise_groups(groups) for key in GROUP_KEYS[group])
    return any(before.get(key) != after.get(key) for key in keys)


SHARED_PAGES = ("crossing", "design")


def group_note(groups, on):
    if not on:
        return tr("Off: each machine keeps its current values.")
    selected = normalise_groups(groups)
    names = trc("list", ", ").join(tr(GROUP_LABELS[group]) for group in selected)
    return tr("Shares {groups} with compatible paired machines.").format(groups=names)


def who(labels):
    """How the notes name the machines the pages are kept in step with: the one paired machine by its
    label, else every paired machine."""
    return labels[0] if len(labels) == 1 else "every paired machine"


def scope(page, who, on, own, platform="", groups=None):
    """The line under a page's purpose: `own` (the page's usual "for this machine only") unless
    the page is kept in step and the switch is on. `who` is from `who()`."""
    if on and page in SHARED_PAGES:
        selected = ALL_GROUPS if groups is None else normalise_groups(groups)
        if page == "crossing":
            if groups is not None and not {"crossing", "shortcut"} <= set(selected):
                shared = []
                if "crossing" in selected:
                    shared.extend(("Resistance", "drag protection"))
                if "shortcut" in selected:
                    shared.append("Shortcut")
                if not shared:
                    return tr(own)
                return tr("{shared} stay in step. Ways and jump keys below are local; each side is shared with its paired machine.").format(
                    shared=trc("list", ", ").join(tr(SHARED_LABELS[item]) for item in shared))
            return tr("Resistance, drag protection and Shortcut stay in step. Ways and jump keys below are local; each side is shared with its paired machine.")
        if "design" not in selected:
            return tr(own)
        if platform == "mac":
            return tr("Style, colour, length, size and landing animation stay in step. Animation enable and window appearance stay local. Notch and trackpad controls stay local.")
        return tr("Style, colour, length, size and landing animation stay in step. Animation enable and window appearance stay local.")
    return tr(own)


def switch_note(who, on, peer_too_old):
    """The note under Overview's Same on all machines switch; `who` names the machines it keeps in step with."""
    if peer_too_old:
        return tr("{name}'s Beamer is too old to keep settings in step. Update it to turn this on.").format(name=who)
    if not on:
        return tr("Off: each machine keeps its current values; previous separate values are not restored.")
    with_machines = tr("compatible paired machines") if who == "every paired machine" else tr("compatible versions of {name}").format(name=who)
    return tr("Shares resistance, drag protection, Shortcut and six animation settings with {machines}.").format(machines=with_machines)


def switch_detail(platform=""):
    if platform == "mac":
        return tr("Connected machines with compatible Beamer versions update now; others catch up when they reconnect. Zones, sides, jump keys, full-screen hold, animation enable and appearance stay on each machine. Notch and trackpad controls stay on this Mac.")
    return tr("Connected machines with compatible Beamer versions update now; others catch up when they reconnect. Zones, sides, jump keys, full-screen hold, animation enable and appearance stay on each machine.")
