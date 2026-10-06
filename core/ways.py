"""A machine's zones, one peer at a time (WIRE.md sections 5 and 8), shared by both desktops.

What the Crossing page reads and writes for the machine it has chosen, the side settled with each
peer by `arrangement`, the sentence for two zones over one stretch, which machine the shortcut and
the Send button pick, and which machines that may drive this one cannot follow a zone. Pure: each
app calls these on its settings under its own lock and writes the file itself.
"""

import base64
import binascii
import copy
import re
from typing import Callable, Optional

from core import peerlist
from core import return_edge
from core.settings_sync import next_stamp

EDGES = return_edge.EDGES
CORNERS = return_edge.CORNERS
PARTS = return_edge.PARTS
DEFAULT_PARTS = ["middle"]
DEFAULT_CORNER = "top_left"
# How a notice names each kind of zone.
KIND_WORDS = {"edge": "edge", "part": "part of the edge", "corner": "corner", "notch": "notch"}
_JUMP_MODIFIERS = {"alt", "cmd", "ctrl", "shift"}
_JUMP_KEYS = {"backspace", "caps_lock", "delete", "down", "end", "enter", "esc", "home", "insert",
              "left", "page_down", "page_up", "pause", "print_screen", "right", "space", "tab", "up",
              "menu", "num_lock", "scroll_lock", "browser_back", "browser_forward", "volume_mute",
              "volume_down", "volume_up", "media_next", "media_prev", "media_stop", "media_play_pause"}


def part_names(edge: str) -> dict:
    """The labels for thirds along an edge, in the direction they run."""
    if edge in ("left", "right"):
        return {"start": "Top", "middle": "Middle", "end": "Bottom"}
    return {"start": "Left", "middle": "Middle", "end": "Right"}


def parts_phrase(edge: str, parts) -> str:
    """The selected thirds in a sentence, defaulting to the middle when none are known."""
    names = part_names(edge)
    chosen = [names[part].lower() for part in PARTS if part in parts] or ["middle"]
    joined = chosen[0] if len(chosen) == 1 else ", ".join(chosen[:-1]) + " and " + chosen[-1]
    return f"the {joined} of the {edge} edge"


def toggle_part(parts, part, on) -> list:
    """The selected thirds after a change, keeping one selected so Part of the edge can cross."""
    chosen = set(parts) | {part} if on else set(parts) - {part}
    if not chosen:
        chosen = set(parts)
    return [name for name in PARTS if name in chosen]


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
        raise ValueError("a phone cannot be a jump target")
    if not isinstance(value, str):
        raise ValueError("choose a modifier and one non-modifier key")
    if not valid_jump_key(value):
        raise ValueError("choose a modifier and one non-modifier key")
    if _trigger_in_chord(trigger_key, value):
        raise ValueError("that key is the trigger shortcut's: pick a chord without it")
    for entry in settings.get("peers", []):
        if entry.get("id") != peer and jump_key(entry) == value:
            raise ValueError("that key is already assigned to another machine")
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
            raise ValueError(f"That shortcut clashes with the jump key {chord}; the jump key has been kept.")


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


def _mine(settings: dict, peer: str) -> dict:
    found = {}
    for zone in settings["zones"]:
        if zone.get("peer") == peer:
            found.setdefault(zone.get("kind"), zone)
    return found


def ways(settings: dict, peer: str) -> dict:
    """The ways across to one peer as the Crossing page shows them: its `side`, the kinds of zone in
    use in section 8's order of kinds, the thirds and the corner its zones hold (the defaults where
    it has none)."""
    entry = _entry(settings, peer) or {}
    mine = _mine(settings, peer)
    parts = mine.get("part", {}).get("parts")
    corner = mine.get("corner", {}).get("corner")
    return {
        "side": entry.get("side") if entry.get("side") in EDGES else "",
        "methods": [kind for kind in KIND_WORDS if kind in mine and mine[kind].get("off") is not True],
        "parts": [part for part in PARTS if part in parts] if isinstance(parts, list) and parts else list(DEFAULT_PARTS),
        "corner": corner if corner in CORNERS else DEFAULT_CORNER,
    }


def edit(settings: dict, peer: str, *, side: str, methods, parts, corner: str, kinds,
         corner_edge: Callable[[str, str], str], now: float) -> bool:
    """Writes one peer's ways: its `side`, and one zone of each of `kinds` (the kinds this app
    offers), in use when named in `methods` and `off` otherwise. The whole edge covers its thirds,
    so with both chosen the thirds are written `off` (section 8). `corner_edge(corner, side)` is
    the edge this app's corner crosses. A changed side is stamped by this machine, never below the
    stamp held plus one. Returns whether the side changed, which the caller sends as `arrangement`.
    KeyError when no entry has `peer`."""
    entry = _entry(settings, peer)
    if entry is None:
        raise KeyError(peer)
    if entry.get("port") == 0:
        raise ValueError("a phone cannot have a side or zones")
    side = side if side in EDGES else ""
    moved = bool(side) and side != entry.get("side")
    if moved:
        entry.update(side=side, side_set_at=next_stamp(entry.get("side_set_at"), now), side_by=settings["machine_id"])
    methods = set(methods)
    if "edge" in methods:
        methods.discard("part")
    parts = [part for part in PARTS if part in parts] or list(DEFAULT_PARTS)
    corner = corner if corner in CORNERS else DEFAULT_CORNER
    fields = {"edge": {}, "part": {"parts": parts}, "corner": {"corner": corner, "edge": corner_edge(corner, side)},
              "notch": {}}
    mine = _mine(settings, peer)
    for kind in kinds:
        zone = mine.get(kind)
        if zone is None:
            zone = {"peer": peer, "kind": kind}
            settings["zones"].append(zone)
        zone.update(fields[kind])
        zone.pop("aside", None)
        if kind in methods:
            zone.pop("off", None)
        else:
            zone["off"] = True
    return moved


def _stretch(zone: dict, sides: dict) -> set:
    """What a zone in use covers, for the rule that two never share one (section 8); the notch is
    one stretch of its own, as a Mac has one notch."""
    if zone.get("kind") == "notch":
        return {("notch",)}
    side = sides.get(zone.get("peer"), "")
    kind = zone.get("kind")
    if kind == "corner" and zone.get("corner") in CORNERS:
        return {("corner", zone["corner"])}
    if kind == "edge" and side in EDGES:
        return {(side, part) for part in PARTS}
    if kind == "part" and side in EDGES and isinstance(zone.get("parts"), list):
        return {(side, part) for part in zone["parts"] if part in PARTS}
    return set()


def share_offer(settings: dict, peer: str, live: Optional[Callable[[str], bool]] = None) -> Optional[dict]:
    """Propose thirds for `peer` when another machine already uses part of its side. A machine with
    no link now is never offered a third: choosing the side takes it from that one (`give_way`)."""
    entry = _entry(settings, peer)
    side = entry.get("side") if entry else ""
    if side not in EDGES:
        return None
    sides = {item.get("id"): item.get("side", "") for item in settings["peers"]}
    holders = _side_holders(settings, peer, live)
    if not holders:
        return None
    holder_set = set(holders)
    assigned = {}
    for zone in settings["zones"]:
        owner = zone.get("peer")
        if owner not in holder_set or zone.get("off") is True or sides.get(owner) != side:
            continue
        if zone.get("kind") not in ("edge", "part"):
            continue
        covered = {part for stretch_side, part in _stretch(zone, sides) if stretch_side == side}
        if not covered:
            continue
        for part in PARTS:
            if part in covered:
                assigned.setdefault(part, owner)
    thirds = {part: assigned.get(part, peer) for part in PARTS}
    if peer not in thirds.values():
        thirds["end"] = peer
    return {"side": side, "holders": holders, "thirds": thirds}


def share(settings: dict, side: str, thirds: dict, *, kinds) -> list:
    """Writes a side split by thirds (`thirds`: part -> machine id) after checking every third and its
    machine, before anything changes: each machine on that side gets its thirds as a part zone in use
    and its whole edge off; one left with none keeps its part zone off. Returns every machine to tell:
    those whose zones changed and those whose way back or way_back_by did (section 8)."""
    if side not in EDGES or not isinstance(thirds, dict) or set(thirds) != set(PARTS):
        raise ValueError("a split needs every third of a known side")
    peers = {entry.get("id"): entry for entry in peerlist.desktops(settings["peers"])}
    if any(not isinstance(peer, str) or peers.get(peer, {}).get("side") != side for peer in thirds.values()):
        raise ValueError("every third must belong to a machine on that side")
    if "part" not in kinds:
        raise ValueError("this app does not offer parts of an edge")
    sides = {entry.get("id"): entry.get("side", "") for entry in settings["peers"]}
    owners = set(thirds.values())
    told = way_back_state(settings)
    changed = []
    for entry in settings["peers"]:
        if entry.get("port") == 0:
            continue
        peer = entry.get("id")
        mine = _mine(settings, peer)
        zones = [zone for zone in mine.values() if zone.get("off") is not True and
                 any(stretch[0] == side for stretch in _stretch(zone, sides))]
        if entry.get("side") != side or (not zones and peer not in owners):
            continue
        edge = mine.get("edge")
        part = mine.get("part")
        expected = [name for name in PARTS if thirds[name] == peer]
        before = (dict(edge) if edge else None, dict(part) if part else None)
        if edge is not None:
            edge["off"] = True
            edge.pop("aside", None)
        if part is None:
            part = {"peer": peer, "kind": "part"}
            settings["zones"].append(part)
        part.pop("aside", None)
        if expected:
            part["parts"] = expected
            part.pop("off", None)
        else:
            # Off, never empty: the settings refuse a part zone that names no third.
            if not (isinstance(part.get("parts"), list) and part["parts"]):
                part["parts"] = list(DEFAULT_PARTS)
            part["off"] = True
        if before != (dict(edge) if edge else None, dict(part)):
            changed.append(peer)
    # A machine whose zones did not change can still be told something new: who holds its side.
    changed += [peer for peer, state in way_back_state(settings).items() if told.get(peer) != state and peer not in changed]
    return changed


def _clashing(settings: dict):
    """The first two zones in use that cover one stretch, and that stretch; None when none do."""
    sides = {entry.get("id"): entry.get("side", "") for entry in settings["peers"]}
    seen = {}
    for zone in settings["zones"]:
        if zone.get("off") is True:
            continue
        for stretch in sorted(_stretch(zone, sides)):
            if stretch in seen:
                return seen[stretch], zone, stretch
            seen[stretch] = zone
    return None


def clash(settings: dict, here: str) -> Optional[str]:
    """The sentence for two zones in use over one stretch of this machine's screen, naming the
    machines they lead to, or None when there is no such pair. `here` is "this Mac" or "this PC"."""
    found = _clashing(settings)
    if found is None:
        return None
    first, second, stretch = found
    peers = settings["peers"]
    one = peerlist.label_for(peers, peer_id=first.get("peer")) or peerlist.UNNAMED
    other = peerlist.label_for(peers, peer_id=second.get("peer")) or peerlist.UNNAMED
    where = {"notch": "the notch", "corner": f"the {stretch[-1].replace('_', ' ')} corner"}.get(
        stretch[0], f"the {stretch[0]} edge")
    if first.get("peer") == second.get("peer"):
        return f"Two of the ways to {one} cover {where} of {here}. Keep one of them."
    fix = {"notch": "Turn the notch off for one of them.", "corner": "Choose another corner for one of them."}.get(
        stretch[0], "Move one of them to another side, or give each its own thirds of the edge.")
    return f"{one} and {other} would both lead from {where} of {here}. {fix}"


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
    """A peer's `arrangement` (section 8): `edge` is the peer's edge that faces this machine, so this
    machine's side for it is the opposite. Taken when newer than the side held: a higher stamp, or the
    same stamp and the larger `by` as bytes. A machine with no zone yet gets its whole edge for that
    side, and a side that would put two zones in use over one stretch is applied and turns this peer's
    clashing zones off. `way_back` and `way_back_by`, when carried, are kept on the entry whether or
    not the side was newer. `corner_edge(corner, side)`, the app's own, rewrites the edge its corner zones
    for the peer cross, as `edit` does, since a PC's corner crosses the side. `live(id)`, when given,
    says whether a machine has a link up now: one that has not gives way (`give_way`). Returns
    (changed, notices), a sentence for each zone turned off; `here` is "this Mac" or "this PC"."""
    entry = _entry(settings, peer)
    by_bytes = _id_bytes(by)
    if entry is None or entry.get("port") == 0 or edge not in EDGES or by_bytes is None:
        return False, []
    told = isinstance(way_back, bool) and entry.get("way_back") is not way_back
    if told:
        entry["way_back"] = way_back
    if isinstance(way_back, bool):
        holder = _id_bytes(way_back_by)
        valid_holder = holder is not None and way_back is False and holder != _id_bytes(settings.get("machine_id"))
        if valid_holder:
            valid_text = way_back_by
            if entry.get("way_back_by") != valid_text:
                entry["way_back_by"] = valid_text
                told = True
        elif "way_back_by" in entry:
            del entry["way_back_by"]
            told = True
    held = (int(entry.get("side_set_at") or 0), _id_bytes(entry.get("side_by")) or b"")
    if (int(set_at), by_bytes) <= held:
        return told, []
    entry.update(side=return_edge.OPPOSITE[edge], side_set_at=int(set_at), side_by=by)
    if corner_edge is not None:
        for zone in settings["zones"]:
            if zone.get("peer") == peer and zone.get("kind") == "corner" and zone.get("corner") in CORNERS:
                zone["edge"] = corner_edge(zone["corner"], entry["side"])
    if peer not in {zone.get("peer") for zone in settings["zones"]}:
        settings["zones"].append({"peer": peer, "kind": "edge"})
    return True, _turn_off_clashes(settings, entry, here, live)


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
    """Gives every machine with no zone at all its whole edge, `off` when its side is another machine's
    already, so the settings stay readable (section 8, overlap). Pairing never made zones before
    1.5.0-beta.5, so a machine paired since 1.4.x has none until this runs. True when it added any."""
    sides = {entry.get("id"): entry.get("side", "") for entry in settings["peers"]}
    covered = set()
    for zone in settings["zones"]:
        if zone.get("off") is not True:
            covered |= _stretch(zone, sides)
    zoned = {zone.get("peer") for zone in settings["zones"]}
    added = False
    for entry in settings["peers"]:
        if not _zoned(entry) or entry["id"] in zoned:
            continue
        zone = {"peer": entry["id"], "kind": "edge"}
        stretch = _stretch(zone, sides)
        if stretch & covered:
            zone["off"] = True
        else:
            covered |= stretch
        settings["zones"].append(zone)
        added = True
    return added


def has_way(settings: dict, peer: str) -> bool:
    """Whether a zone in use leads to `peer` and can fire: a corner or the notch, or an edge or thirds
    of a side that is set. What `arrangement`'s `way_back` says to that machine."""
    sides = {entry.get("id"): entry.get("side", "") for entry in settings["peers"]}
    return any(zone.get("peer") == peer and zone.get("off") is not True and _stretch(zone, sides)
               for zone in settings["zones"])


def blocked_sentence(settings: dict, peer: str, here: str, live: Optional[Callable[[str], bool]] = None) -> str:
    """The Crossing page's sentence for a machine whose side another machine's edge already holds and
    that has no way in of its own; "" otherwise, and "" for a holder with no link now, which gives way."""
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
    return (f"{name} has no way in: {other} already leads from the {side} edge of {here}. Give each its own "
            "thirds of that edge under Part of the edge, or move one to another side.")


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
        other = peerlist.label_for(settings["peers"], peer_id=other_id) or "another machine"
        side = entry.get("side")
        facing = return_edge.OPPOSITE[side] if side in EDGES else None
        where = f" on its {facing} too" if facing else " too"
        return (f"{name} has {other}{where}, so none of its edges leads back here. On {name}'s Crossing page, "
                "share that side by thirds or move one of them.")
    return f"{name} has no way back to this machine: none of its edges leads here. Its own Crossing page says why."


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
    stretches = {(side, part) for part in PARTS}
    holders = []
    for zone in settings["zones"]:
        owner = zone.get("peer")
        if (owner != peer and owner not in holders and zone.get("off") is not True
                and zone.get("kind") in ("edge", "part") and _stretch(zone, sides) & stretches
                and (live is None or live(owner))):
            holders.append(owner)
    return holders


def settle(settings: dict, peer: str, here: str, live: Optional[Callable[[str], bool]] = None) -> list:
    """After a machine's side moved here: its zones that now clash with another machine's are turned
    off, as when the side arrives from that machine, and a sentence for each is returned. With
    `live`, a machine with no link now gives way instead (`give_way`)."""
    entry = _entry(settings, peer)
    return _turn_off_clashes(settings, entry, here, live) if entry is not None else []


def give_way(settings: dict, peer: str, here: str, live: Optional[Callable[[str], bool]]) -> list:
    """Sets aside every zone in use of a machine with no link now (`live(id)` false) that covers a
    stretch one of `peer`'s zones in use covers, and returns a sentence for each. The zone is `off`
    and carries `aside`: a machine with its lid shut is not gone, so `settle_presence` puts the zone
    back when it connects, and turns it off for good only when it is found to have removed this one
    (06-10-2026). Nothing without `live`."""
    if live is None:
        return []
    sides = {item.get("id"): item.get("side", "") for item in settings["peers"]}
    wanted = set()
    for zone in settings["zones"]:
        if zone.get("peer") == peer and zone.get("off") is not True:
            wanted |= _stretch(zone, sides)
    peers = settings["peers"]
    name = peerlist.label_for(peers, peer_id=peer) or peerlist.UNNAMED
    notices = []
    for zone in settings["zones"]:
        owner = zone.get("peer")
        if owner == peer or zone.get("off") is True or not (_stretch(zone, sides) & wanted) or live(owner):
            continue
        zone["off"] = True
        zone["aside"] = True
        other = peerlist.label_for(peers, peer_id=owner) or peerlist.UNNAMED
        notices.append(f"{other} is not connected, so {name} takes that part of {here}'s screen for now and "
                       f"{other}'s {KIND_WORDS.get(zone.get('kind'), 'zone')} is set aside until it connects.")
    return notices


def presence_due(settings: dict, live: Callable[[str], bool], gone: Callable[[str], bool]) -> bool:
    """Whether `settle_presence` would change anything now: a zone is set aside (`give_way`) for a
    machine that has connected, or has removed this pairing, since."""
    if not any(zone.get("aside") is True for zone in settings["zones"]):
        return False
    probe = copy.deepcopy(settings)
    settle_presence(probe, "", live, gone)
    return probe != settings


def settle_presence(settings: dict, here: str, live: Callable[[str], bool], gone: Callable[[str], bool]) -> list:
    """What became of the zones `give_way` set aside, as the links stand now: a machine with a link
    up has its zone back unless another zone in use covers that stretch (then it stays off, as when two
    machines that are both here want one stretch, and the Crossing page offers the thirds), and a machine
    `gone(id)` says has removed this pairing keeps its zone off for good. Returns a sentence for each
    zone it settled, apart from those put back; settings change in place."""
    sides = {item.get("id"): item.get("side", "") for item in settings["peers"]}
    peers = settings["peers"]
    notices = []
    for zone in settings["zones"]:
        if zone.get("aside") is not True:
            continue
        owner = zone.get("peer")
        if zone.get("off") is not True:
            del zone["aside"]
            continue
        name = peerlist.label_for(peers, peer_id=owner) or peerlist.UNNAMED
        word = KIND_WORDS.get(zone.get("kind"), "zone")
        if gone(owner):
            del zone["aside"]
            notices.append(f"{name} no longer has this pairing, so its {word} stays off. Remove {name} here and "
                           "pair the two again.")
        elif live(owner):
            del zone["aside"]
            stretch = _stretch(zone, sides)
            holder = next((other.get("peer") for other in settings["zones"] if other is not zone
                           and other.get("off") is not True and _stretch(other, sides) & stretch), None)
            if holder is None:
                del zone["off"]
            else:
                held = peerlist.label_for(peers, peer_id=holder) or peerlist.UNNAMED
                notices.append(f"{name} is connected again, but {held} leads from that part of {here}'s screen, so "
                               f"{name}'s {word} stays off. Share the side on the Crossing page.")
    return notices


def gone_sentence(settings: dict, peer: str, here: str, gone: Callable[[str], bool]) -> str:
    """The Crossing page's sentence for a machine that has removed this pairing, "" for any other."""
    if not gone(peer):
        return ""
    name = peerlist.label_for(settings["peers"], peer_id=peer) or peerlist.UNNAMED
    if has_way(settings, peer):
        return (f"{name} no longer has this pairing. If another machine takes its side, {here} gives it up. "
                f"Remove {name} here and pair the two again.")
    return f"{name} no longer has this pairing, so it has no way in. Remove {name} here and pair the two again."


def _turn_off_clashes(settings: dict, entry: dict, here: str, live: Optional[Callable[[str], bool]] = None) -> list:
    notices = give_way(settings, entry.get("id"), here, live)
    sides = {item.get("id"): item.get("side", "") for item in settings["peers"]}
    peers = settings["peers"]
    covered = {}
    mine = []
    for zone in settings["zones"]:
        if zone.get("off") is True:
            continue
        if zone.get("peer") == entry.get("id"):
            mine.append(zone)
            continue
        for stretch in _stretch(zone, sides):
            covered.setdefault(stretch, zone.get("peer"))
    name = peerlist.label_for(peers, peer_id=entry.get("id")) or peerlist.UNNAMED
    for zone in mine:
        stretch = _stretch(zone, sides)
        other = next((covered[item] for item in sorted(stretch) if item in covered), None)
        if other is None:
            for item in stretch:
                covered.setdefault(item, entry.get("id"))
            continue
        zone["off"] = True
        other_name = peerlist.label_for(peers, peer_id=other) or peerlist.UNNAMED
        notices.append(f"{other_name} and {name} now lead from the same part of {here}'s screen, so {name}'s "
                       f"{KIND_WORDS.get(zone.get('kind'), 'zone')} is off.")
    return notices


def _sendable(entry: dict) -> bool:
    return bool(entry.get("id")) and entry.get("in_use", True) is True and entry.get("send") is True and entry.get("port") != 0


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


def missing_pairings(settings: dict, peer: str) -> list:
    """The machines that may drive this one and that a zone to `peer` cannot take onward, because
    they are not paired with `peer` (section 5, "What the Crossing page may offer"). A machine is
    judged only once it has linked, since its `paired` list arrives on a link."""
    return [entry for entry in settings["peers"]
            if entry.get("id") != peer and entry.get("in_use", True) is True and entry.get("allow_drive") is True and entry.get("linked") is True
            and peer not in (entry.get("paired_with") or [])]


def missing_sentence(settings: dict, peer: str, here: str) -> str:
    """The Crossing page's sentence for `missing_pairings`, "" when there are none."""
    missing = missing_pairings(settings, peer)
    if not missing:
        return ""
    peers = settings["peers"]
    names = [peerlist.label_for(peers, peer_id=entry.get("id")) or peerlist.UNNAMED for entry in missing]
    target = peerlist.label_for(peers, peer_id=peer) or peerlist.UNNAMED
    drivers = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
    if len(names) == 1:
        why = f"{names[0]} is not paired with {target}"
    elif len(names) == 2:
        why = f"neither is paired with {target}"
    else:
        why = f"none of them is paired with {target}"
    return f"While {drivers} drives {here}, this way to {target} does nothing: {why}. Pair them to use it from there too."
