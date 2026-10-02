"""What each paired machine's row on Overview says: a tone, a state word and one sentence.

Pure, so every state a link can be in is tested against a word. The booleans decide the broad
state; the link's own `kind` picks the flavour of a fault, and its status sentence (which already
names the machine, WIRE.md section 2) is the detail. The words are the Mac's, row for row.
"""

from __future__ import annotations

from dataclasses import dataclass

from core import protocol

PLATFORM_NAMES = {"macos": "Mac", "windows": "Windows", "linux": "Linux", "ios": "iPhone", "android": "Android"}

REFUSED_KINDS = ("unauthenticated", "wrong_id", "different", "unreadable")
VERSION_KINDS = ("older", "newer", "not_beamer")
UNREACHABLE_KINDS = ("unreachable", "timeout")
QUIET_KINDS = ("connected", "removed", "off")


@dataclass(frozen=True)
class PeerState:
    tone: str
    word: str
    detail: str


def platform_name(entry: dict) -> str:
    return PLATFORM_NAMES.get(entry.get("platform"), "Other")


def address_line(entry: dict, hide: bool) -> str:
    host, port = entry.get("host") or "", entry.get("port")
    if not host or not isinstance(port, int) or isinstance(port, bool) or port <= 0:
        return "No address"
    return f"{'•••' if hide else host} · port {port}"


def describe(entry: dict, label: str, *, up: bool, driving: bool, input_there: bool, kind: str, status: str,
             waking: bool) -> PeerState:
    """`up`: a link to this machine is live, either way round. `driving`: its input is on this PC.
    `input_there`: this PC's input is on it. `kind` and `status`: what the link this PC dials to it
    last reported."""
    if driving:
        return PeerState("signal", "Driving this PC",
                         f"{label}'s keyboard and pointer are on this PC. Push the pointer back through the edge it arrived by to send them home.")
    if input_there:
        return PeerState("signal", "Input here",
                         f"This PC's keyboard and pointer are on {label}. Do the same again to bring them home.")
    if not protocol.linkable(entry):
        return PeerState("amber", "Pair again",
                         f"{label} was paired on Beamer 1.4. Pair the two again to link them on 1.5.0.")
    if up:
        return PeerState("signal", "Linked", f"Connected to {label}.")
    if waking:
        return PeerState("amber", "Waiting", f"Waking {label}…")
    if not entry.get("send"):
        return PeerState("amber", "Waiting", f"Waiting for {label} to connect.")
    if not status or kind in QUIET_KINDS:
        return PeerState("amber", "Waiting", f"Looking for {label}. It connects on its own once Beamer is open there.")
    if kind in REFUSED_KINDS:
        return PeerState("fault", "Refused", status)
    if kind in VERSION_KINDS or (kind == "closed" and status.startswith("Update Beamer on")):
        return PeerState("fault", "Version", status)
    if kind in UNREACHABLE_KINDS:
        return PeerState("fault", "Unreachable", status)
    if kind == "refused":
        return PeerState("fault", "Not listening", status)
    return PeerState("fault", "Dropped", f"{status.rstrip('.')}. Beamer keeps trying to reconnect.")
