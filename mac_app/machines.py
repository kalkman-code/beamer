"""One row of the Overview's list of machines: its name, where it is, what its link is doing.

Pure, so every state a row can be in is tested against a word. The faults take their sentence from
the link itself (core/link.py words them, naming the machine), so a version 5 machine that closes
the connection reads "Update Beamer on <name> to 1.5.0..." here and nowhere else is it reworded.
"""

from __future__ import annotations

from dataclasses import dataclass

from core import protocol
from link_state import AMBER, FAULT, INK, SIGNAL, LinkState

PLATFORMS = {"windows": "Windows", "macos": "Mac", "linux": "Linux", "ios": "iPhone", "android": "Android"}
TOKEN_KINDS = ("unauthenticated", "wrong_id", "different", "unreadable", "forgotten")
VERSION_KINDS = ("older", "newer", "not_beamer")


@dataclass(frozen=True)
class Row:
    token: str
    label: str
    platform: str
    address: str
    state: LinkState
    in_use: bool
    drives: bool
    driven: bool
    phone: bool = False


def platform_name(platform: str) -> str:
    return PLATFORMS.get(platform, "Other")


def row(entry: dict, label: str, live: bool, kind: str, status: str, here: bool, driving: bool, inbound: bool,
        has_link: bool) -> Row:
    """`live`, `kind` and `status` are the outbound link's, and `has_link` whether this Mac dials
    the machine at all; `inbound` is whether the machine has a link up to this Mac; `here` and
    `driving` are where input is."""
    host, port = entry.get("host"), entry.get("port")
    address = f"{host} · port {port}" if host and port else "No address"
    return Row(
        entry["token"], label, platform_name(entry.get("platform", "")), address,
        _state(entry, label, live, kind, status, here, driving, inbound, has_link), entry.get("in_use", True) is True,
        entry.get("send") is True, entry.get("allow_drive") is True, port == 0,
    )


def _state(entry, label, live, kind, status, here, driving, inbound, has_link) -> LinkState:
    if entry.get("in_use", True) is not True:
        return LinkState("not_in_use", AMBER, "NOT IN USE", "NOT IN USE", f"{label} is not in use on this Mac.", AMBER)
    if not protocol.linkable(entry):
        return LinkState(
            "pair_again", AMBER, "Pair again", "Pair again",
            f"{label} was paired on Beamer 1.4. Pair the two again to link them on 1.5.0.", AMBER,
        )
    if driving:
        return LinkState(
            "driving", SIGNAL, "Driving this Mac", "Live",
            f"{label}'s keyboard and pointer are on this Mac. Push the pointer back through the edge it arrived by to send them home.",
            SIGNAL,
        )
    if here:
        return LinkState(
            "here", SIGNAL, "Input here", "Live",
            f"This Mac's keyboard and pointer are on {label}. Do the same again to bring them home.", SIGNAL,
        )
    if live or inbound:
        return LinkState("linked", INK, "Linked", "Linked", f"Connected to {label}.", SIGNAL)
    if not has_link:
        return LinkState("waiting", AMBER, "Waiting", "Waiting", f"Waiting for {label} to connect.", AMBER)
    if kind in TOKEN_KINDS:
        return LinkState("token", FAULT, "Refused", "Refused", status, FAULT)
    if kind in VERSION_KINDS or (kind == "closed" and not entry.get("linked")):
        return LinkState("version", FAULT, "Version", "Refused", status, FAULT)
    if kind in ("unreachable", "timeout"):
        return LinkState("unreachable", FAULT, "Unreachable", "No reply", status, FAULT)
    if kind == "refused":
        return LinkState("not_listening", FAULT, "Not listening", "No receiver", status, FAULT)
    if kind == "blocked":
        return LinkState("blocked", FAULT, "Blocked", "Tunnel", status, FAULT)
    if kind in ("stopped", "closed", "failed") and status:
        return LinkState("dropped", FAULT, "Dropped", "Dropped", f"{status.rstrip('.')}. Beamer keeps trying to reconnect.", FAULT)
    return LinkState("waiting", AMBER, "Waiting", "Looking", f"Looking for {label}. It connects on its own once Beamer is open there.", AMBER, True)
