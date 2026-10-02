"""A machine's zones, one peer at a time (WIRE.md sections 5 and 8), shared by both desktops.

What the Crossing page reads and writes for the machine it has chosen, the side settled with each
peer by `arrangement`, the sentence for two zones over one stretch, which machine the shortcut and
the Send button pick, and which machines that may drive this one cannot follow a zone. Pure: each
app calls these on its settings under its own lock and writes the file itself.
"""

import base64
import binascii
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
         corner_edge: Callable[[str, str], str], now: float, default: bool = False) -> bool:
    """Writes one peer's ways: its `side`, and one zone of each of `kinds` (the kinds this app
    offers), in use when named in `methods` and `off` otherwise. The whole edge covers its thirds,
    so with both chosen the thirds are written `off` (section 8). `corner_edge(corner, side)` is
    the edge this app's corner crosses. A changed side is stamped by this machine, never below the
    stamp held plus one. `default` says `side` is only what the page shows for a machine not placed
    yet, which nobody chose: it is written only while no side is held, unstamped and with no author,
    so a side either machine does choose wins over it. Returns whether the side changed, which the
    caller sends as `arrangement`. KeyError when no entry has `peer`."""
    entry = _entry(settings, peer)
    if entry is None:
        raise KeyError(peer)
    side = side if side in EDGES else ""
    if default and entry.get("side") in EDGES:
        side = entry["side"]
    moved = bool(side) and side != entry.get("side")
    if moved and default:
        entry.update(side=side, side_set_at=0, side_by="")
    elif moved:
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
    return data if len(data) == 16 and base64.urlsafe_b64encode(data).decode("ascii").rstrip("=") == text else None


def arrangement(settings: dict, peer: str, edge: str, set_at: int, by: str, here: str, way_back=None,
                corner_edge: Optional[Callable[[str, str], str]] = None):
    """A peer's `arrangement` (section 8): `edge` is the peer's edge that faces this machine, so this
    machine's side for it is the opposite. Taken when newer than the side held: a higher stamp, or the
    same stamp and the larger `by` as bytes. A machine with no zone yet gets its whole edge for that
    side, and a side that would put two zones in use over one stretch is applied and turns this peer's
    clashing zones off. `way_back`, when the message carried it, is kept on the entry whether or not
    the side was newer. `corner_edge(corner, side)`, the app's own, rewrites the edge its corner zones
    for the peer cross, as `edit` does, since a PC's corner crosses the side. Returns (changed,
    notices), a sentence for each zone turned off; `here` is "this Mac" or "this PC"."""
    entry = _entry(settings, peer)
    by_bytes = _id_bytes(by)
    if entry is None or edge not in EDGES or by_bytes is None:
        return False, []
    told = isinstance(way_back, bool) and entry.get("way_back") is not way_back
    if told:
        entry["way_back"] = way_back
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
    return True, _turn_off_clashes(settings, entry, here)


def _zoned(entry: dict) -> bool:
    """A machine that crosses by zones: a desktop with an id, never a phone."""
    return bool(entry.get("id")) and entry.get("port") != 0


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


def blocked_sentence(settings: dict, peer: str, here: str) -> str:
    """The Crossing page's sentence for a machine whose side another machine's edge already holds and
    that has no way in of its own; "" otherwise."""
    entry = _entry(settings, peer) or {}
    side = entry.get("side")
    if side not in EDGES or has_way(settings, peer):
        return ""
    sides = {item.get("id"): item.get("side", "") for item in settings["peers"]}
    holder = next((zone.get("peer") for zone in settings["zones"]
                   if zone.get("peer") != peer and zone.get("off") is not True and zone.get("kind") in ("edge", "part")
                   and _stretch(zone, sides) & {(side, part) for part in PARTS}), None)
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
    return f"{name} has no way back to this machine: none of its edges leads here. Its own Crossing page says why."


def settle(settings: dict, peer: str, here: str) -> list:
    """After a machine's side moved here: its zones that now clash with another machine's are turned
    off, as when the side arrives from that machine, and a sentence for each is returned."""
    entry = _entry(settings, peer)
    return _turn_off_clashes(settings, entry, here) if entry is not None else []


def _turn_off_clashes(settings: dict, entry: dict, here: str) -> list:
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
    notices = []
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
    return bool(entry.get("id")) and entry.get("send") is True and entry.get("port") != 0


def shortcut_peer(peers: list, last: Optional[str], ready: Callable[[str], bool]) -> Optional[str]:
    """The machine the shortcut, the Send button and the tray send this machine's input to: the one
    it was last on while that one can take it, else the first in the list that can, else the first
    this machine sends to at all, so the refusal or the wake names it. Never a machine this one does
    not send to, a phone, or an entry with no id yet. `ready(id)` says whether one can take input now."""
    candidates = [entry["id"] for entry in peers if _sendable(entry)]
    if last in candidates and ready(last):
        return last
    return next((ident for ident in candidates if ready(ident)), candidates[0] if candidates else None)


def missing_pairings(settings: dict, peer: str) -> list:
    """The machines that may drive this one and that a zone to `peer` cannot take onward, because
    they are not paired with `peer` (section 5, "What the Crossing page may offer"). A machine is
    judged only once it has linked, since its `paired` list arrives on a link."""
    return [entry for entry in settings["peers"]
            if entry.get("id") != peer and entry.get("allow_drive") is True and entry.get("linked") is True
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
