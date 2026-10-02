"""Same on all machines: the Crossing and Design settings every machine keeps in step (WIRE.md section 10).

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
screen, whether a full-screen app holds the edges, appearance, and anything outside the Crossing
and Design pages.
"""

import base64
import binascii
import re
import time

from . import effects
from .protocol import MACHINE_ID_SIZE

TRIGGER_STYLES = ("double_tap", "hold")
GLOW_STYLES = ("glow", "beam") + effects.EFFECT_IDS
GLOW_COLOURS = ("signal", "colourful", "ocean", "sunset", "mono") + effects.PACK_IDS
LENGTHS = tuple(value for value, _name in effects.LENGTHS)
# Keys a trigger cannot be, beside characters and media keys, which the receiver's own table rejects.
NOT_TRIGGERS = ("browser_back", "browser_forward", "backspace", "tab", "enter", "esc", "space", "print_screen")

CROSSING_KEYS = ("resistance_px", "block_while_dragging", "shortcut", "trigger_key", "trigger_style", "double_tap_ms")
DESIGN_KEYS = ("glow_style", "glow_colour", "effect_length", "shortcut_arrival", "shortcut_arrival_style")

MAX_STAMP = 2 ** 53 - 2
DAY = 24 * 60 * 60

# The PC's field for each wire key that is a field of its own; `shortcut` is a way in
# `crossing_methods`.
PC_FIELDS = {
    "resistance_px": "crossing_resistance_px", "block_while_dragging": "block_while_dragging",
    "trigger_key": "trigger_key", "trigger_style": "trigger_style", "double_tap_ms": "double_tap_ms",
    "glow_style": "glow_style", "glow_colour": "glow_colour", "effect_length": "effect_length",
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
    if key == "shortcut_arrival_style":
        return value in effects.SWITCH_STYLES
    return False


def message_data(on, set_at, values=None, by=""):
    """What a `settings` message carries: whether it is on, when that or a shared value last
    changed and by which machine (`by`, its id as base64), and while on, the values."""
    data = {"on": bool(on), "set_at": int(set_at), "by": by}
    if on and values is not None:
        data["crossing"] = {key: values[key] for key in CROSSING_KEYS if key in values}
        data["design"] = {key: values[key] for key in DESIGN_KEYS if key in values}
    return data


def read(data, trigger_keys):
    """(on, set_at, values) from a `settings` message's data, or None when it is not one. A value
    this end cannot hold -- a key it does not have, an effect it does not know, a zone -- is left
    out rather than failing the rest. Never raises: a malformed message must not take a link down."""
    if not isinstance(data, dict) or not isinstance(data.get("on"), bool):
        return None
    set_at = data.get("set_at")
    if type(set_at) is not int:
        return None
    values = {}
    for group, keys in (("crossing", CROSSING_KEYS), ("design", DESIGN_KEYS)):
        section = data.get(group)
        if not isinstance(section, dict):
            continue
        for key in keys:
            if key in section and _valid(key, section[key], trigger_keys):
                values[key] = section[key]
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


def next_stamp(held, now):
    """The stamp for a change made here: now, or one past the stamp held if that is not older.
    Two changes in one second would otherwise tie and the second be ignored at the other end, and
    a peer whose clock runs ahead would win every time."""
    return max(int(now), int(held or 0) + 1)


def arrived(data, set_at_here, trigger_keys, by_here="", now=None):
    """What to do with a `settings` message that arrived: (on, set_at, values) to take in place of
    this end's state, or None to keep it -- malformed, a stamp more than a day ahead of `now` or
    above 2^53 - 2, or no newer than what is held here.

    Newer is a higher stamp, or the same stamp and the larger `by` as bytes; `by_here` is the id
    that made the change this end holds. Equal in both is the copy that arrives over a second
    link, and changes nothing, so a state sent on never comes back round."""
    read_back = read(data, trigger_keys)
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
    values["shortcut"] = "shortcut" in crossing.get("methods", [])
    return _in_order(values)


def apply_mac(raw, values):
    """A copy of the Mac's raw settings with `values` in place. The other ways, the notch among
    them, stay as they were: they are this Mac's own zones."""
    raw = dict(raw)
    crossing = dict(raw.get("crossing", {}))
    for key, value in values.items():
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
    values = {key: getattr(config, field) for key, field in PC_FIELDS.items()}
    values["shortcut"] = "shortcut" in config.crossing_methods
    return _in_order(values)


def apply_pc(config, values):
    """Sets `values` on the PC's Config in place."""
    for key, value in values.items():
        if key == "shortcut":
            config.crossing_methods = _with_shortcut(config.crossing_methods, value)
        else:
            setattr(config, PC_FIELDS[key], value)


def changed(before, after):
    """Whether any shared value differs between two value dicts."""
    return any(before.get(key) != after.get(key) for key in CROSSING_KEYS + DESIGN_KEYS)


SHARED_PAGES = ("crossing", "design")


def who(labels):
    """How the notes name the machines the pages are kept in step with: the one paired machine by its
    label, else every paired machine."""
    return labels[0] if len(labels) == 1 else "every paired machine"


def scope(page, who, on, own):
    """The line under a page's purpose: `own` (the page's usual "for this machine only") unless
    the page is kept in step and the switch is on. `who` is from `who()`."""
    if on and page in SHARED_PAGES:
        return f"Kept the same as {who}. Change it on any machine."
    return own


def switch_note(who, on, peer_too_old):
    """The note under Overview's Same on all machines switch; `who` names the machines it keeps in step with."""
    if peer_too_old:
        return f"{who}'s Beamer is too old to keep settings in step. Update it to turn this on."
    if on:
        return f"The Crossing and Design pages match {who}'s. A change on any machine reaches the rest."
    return (f"Off: the Crossing and Design pages are this machine's own. On, they match {who}'s, "
            "and a change on any machine reaches the rest.")
