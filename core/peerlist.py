"""What a desktop's window does to the peers list, and the words it uses for pairing.

Pure, and shared by both desktops so a machine pairing with either reads the same sentences. The
settings layer calls `add_peer` from `PairingService`'s `store` and `remove_peer` from the Remove
button, each under its own settings lock; neither touches a file.
"""

import copy
import time
from typing import Optional

from core import pairing

UNNAMED = "Unnamed machine"
TAIL = 4


def _name(entry: dict) -> str:
    return (entry.get("name") or "").strip()


def labels(peers: list) -> dict:
    """token -> what to call each machine: its name, and where two share one (ignoring case and
    surrounding space) the last four characters of its id too, so the two can be told apart. An
    entry that has no id yet (the one migrated from 1.4.x) shows the name alone."""
    counts = {}
    for entry in peers:
        key = _name(entry).casefold()
        counts[key] = counts.get(key, 0) + 1
    result = {}
    for entry in peers:
        name = _name(entry) or entry.get("host") or UNNAMED
        shared = counts[_name(entry).casefold()] > 1 and _name(entry)
        ident = entry.get("id") or ""
        result[entry["token"]] = f"{name} ({ident[-TAIL:]})" if shared and ident else name
    return result


def sharing_a_name(machines: list) -> list:
    """For each machine heard, in order, whether another in the list has its name (as `labels()`
    compares them): one machine heard on two interfaces is two rows, told apart by address."""
    counts = {}
    for machine in machines:
        key = _name(machine).casefold()
        counts[key] = counts.get(key, 0) + 1
    return [counts[_name(machine).casefold()] > 1 for machine in machines]


def label_for(peers: list, *, token: Optional[str] = None, peer_id: Optional[str] = None, host: Optional[str] = None) -> str:
    """`labels()` for one machine, found by its token, its id (`b64`) or its address, in that order of
    preference; "" when none matches. What a status or a notice calls a machine, so two of one name
    are told apart there as they are in the list."""
    shown = labels(peers)
    for key, value in (("token", token), ("id", peer_id), ("host", host)):
        if value:
            found = next((entry for entry in peers if entry.get(key) == value), None)
            if found is not None:
                return shown[found["token"]]
    return ""


def add_peer(settings: dict, entry: dict, replaced: Optional[dict] = None) -> None:
    """Puts a newly paired `entry` in `settings` (WIRE.md section 6, item 5). `replaced`, an entry
    as `peers()` gave it, is dropped by its token and the new one takes its place: the first entry
    is the one the flat settings read, so the new machine must not slip behind. Zones that name
    an id no entry has any more go with it; a machine paired again under its own id keeps its zones.
    The entry migrated from 1.4.x (`id` empty) gives its side and zones to a pairing with the machine
    at its saved host on its platform (section 6, item 4): that is the machine the user meant to pair
    again, and a match by name alone takes nothing, a name being anyone's to claim."""
    peers = settings["peers"]
    at = next((index for index, held in enumerate(peers) if replaced is not None and held.get("token") == replaced.get("token")), None)
    if at is None:
        peers.append(entry)
    else:
        peers[at] = entry
    if (replaced is not None and replaced.get("from_1_4") is True and replaced.get("id") == "" and entry.get("id")
            and replaced.get("host") and replaced.get("host") == entry.get("host")
            and replaced.get("platform") == entry.get("platform")):
        entry.update({field: replaced.get(field, default) for field, default in
                      (("side", ""), ("side_set_at", 0), ("side_by", ""))})
        for zone in settings["zones"]:
            if zone.get("peer") == "":
                zone["peer"] = entry["id"]
    _drop_orphan_zones(settings)
    # Imported here: ways names machines by this module's labels.
    from core import ways
    ways.ensure_zones(settings)


def remove_peer(settings: dict, token: str) -> Optional[dict]:
    """Takes the entry holding `token` out of `settings` with its zones, and returns it; None when
    there is none."""
    peers = settings["peers"]
    at = next((index for index, held in enumerate(peers) if held.get("token") == token), None)
    if at is None:
        return None
    removed = peers.pop(at)
    _drop_orphan_zones(settings)
    return removed


def _drop_orphan_zones(settings: dict) -> None:
    ids = {held.get("id") for held in settings["peers"]}
    settings["zones"] = [zone for zone in settings["zones"] if zone.get("peer") in ids]


def day(paired_at) -> str:
    """The day an entry was paired, DD-MM-YYYY in this machine's local time; "" when unknown."""
    if not isinstance(paired_at, int) or isinstance(paired_at, bool) or paired_at <= 0:
        return ""
    return time.strftime("%d-%m-%Y", time.localtime(paired_at))


def _since(entry: dict) -> str:
    when = day(entry.get("paired_at"))
    return f", since {when}" if when else ""


def pairing_error_text(error: Exception, name: str) -> str:
    """A requester's failure, said to its user; `name` is how the machine is shown (its label, or
    "That machine" when it is not known yet, which starts a sentence)."""
    if isinstance(error, pairing.AlreadyPaired):
        entry = error.entry
        if entry is None:
            return "That is this machine, not another one."
        label = _name(entry) or name
        return f"{label} is already paired with this machine{_since(entry)}. Remove it from the list first to pair again."
    reason = str(error)
    if reason == pairing.ERROR_NOT_PAIRING:
        return f"{name} is not showing a code. Show one in Beamer on it, then try again."
    if reason == pairing.ERROR_REFUSED:
        return f"That code was not accepted, and {name} has cancelled it. Show a new one there to try again."
    if reason == pairing.ERROR_VERSION:
        return f"{name} runs an older Beamer. Update Beamer on it to 1.5.0, then pair again."
    if reason == "no_answer":
        return f"{name} did not answer. Check both machines are on the same network, then try again."
    if reason == pairing.ERROR_FULL:
        return (f"Either this machine or {name} already has {pairing.MAX_PEERS} machines paired, the most "
                "Beamer keeps. Remove one, then try again.")
    if reason == pairing.ERROR_KNOWN:
        return f"{name} already has this machine paired. Remove this machine from its list there, then pair again."
    if reason == "not_saved":
        return "Pairing finished, but Beamer could not save it on this machine. Try again in a moment."
    if reason == "busy":
        return "Another pairing is still finishing. Try again in a moment."
    return f"Pairing failed: {reason}."


def refusal_text(why: str, name: str, other: Optional[str]) -> Optional[str]:
    """The alert for an `owned` or `busy` refusal, naming the machine in the way when known."""
    if why == "owned":
        return (f"Can't cross to {name}: it is being driven from {other}." if other else
                f"Can't cross to {name}: another machine is driving it.")
    if why == "busy":
        return (f"Can't cross to {name}: it is driving {other}." if other else
                f"Can't cross to {name}: it is driving another machine.")
    return None


def host_outcome_text(outcome: Optional[str], entry: Optional[dict]) -> str:
    """How the code on screen ended, said to the person at this machine (`PairingService.outcome`,
    with `entry` the machine that paired or, for `known`, the entry it was found to be); "" while
    a code is live or after a deliberate cancel."""
    name = _name(entry or {}) or "A machine"
    if outcome == "paired":
        return f"Paired with {name}."
    if outcome == "refused":
        return "A wrong code was tried, so this code was cancelled. Show a new one to try again."
    if outcome == "version":
        return "A machine on an older Beamer tried to pair. Update Beamer on it to 1.5.0, then show a new code."
    if outcome == "expired":
        return "The code expired. Show a new one."
    if outcome == "known":
        if entry is None:
            return "That machine gave this machine's own identity, so nothing was paired. Show a new code to try again."
        return f"{name} is already paired here{_since(entry)}. Remove it from the list first to pair again."
    if outcome == "known_there":
        return "That machine may already be paired here, or the code was wrong. Show a new code to try again."
    if outcome == "full":
        return f"This machine already has {pairing.MAX_PEERS} machines paired, the most Beamer keeps. Remove one, then show a new code."
    if outcome == "not_saved":
        return "Beamer could not save the pairing on this machine, so it did not complete. Show a new code to try again."
    return ""
