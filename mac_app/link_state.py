"""What the status module says about the link: a tone, a state word, the LED tag and one sentence.

Pure, so every status the controller can report is tested against a word. The controller's
booleans decide the broad state; its status string only picks the flavour of a fault, and a string
nobody has seen before still lands on a word with the controller's own sentence as the detail.
"""

from __future__ import annotations

from dataclasses import dataclass

from wake import NOT_WOKEN_SUFFIX

SIGNAL = "signal"
AMBER = "amber"
FAULT = "fault"
INK = "ink"

@dataclass(frozen=True)
class LinkState:
    key: str
    tone: str
    word: str
    tag: str
    detail: str
    led: str
    blink: bool = False


def peer_name(cfg) -> str:
    return cfg.pc_name or cfg.host or "the other machine"


def describe(controller) -> LinkState:
    """The controller may name each machine (`peer_label` the first, `on_label` the one input is
    on, `driver_label` the one driving this Mac), which tells two of one name apart; without, the
    first machine's saved name stands in."""
    cfg = controller.cfg
    peer = getattr(controller, "peer_label", None) or peer_name(cfg)
    on = getattr(controller, "on_label", None) or peer
    driver = getattr(controller, "driver_label", None) or peer
    status = controller.connection_status or ""
    tunnel = bool(getattr(controller, "via_tunnel", False))
    kind = getattr(controller, "status_kind", "none")
    if getattr(controller, "receiving", False):
        return LinkState(
            "receiving", SIGNAL, f"From {driver}", "Live",
            f"{driver}'s keyboard and pointer are on this Mac. Push its pointer back through the edge "
            "it arrived by to send them home.",
            SIGNAL,
        )
    if controller.waking:
        return LinkState("waking", AMBER, "Waking", "Waking", f"Waking {peer}. It connects on its own once it is up.", AMBER, True)
    if controller.connected:
        linked = "Tunnel" if tunnel else "Linked"
        if controller.windows_locked:
            return LinkState(
                "unlocking", AMBER, "Unlocking", "Unlocking",
                f"{peer} is at its lock screen and is unlocking itself; input follows once the desktop is back.",
                AMBER, True,
            )
        if controller.redirecting:
            return LinkState(
                "windows", SIGNAL, f"On {on}", "Live",
                f"Keyboard and pointer are on {on}. Do the same again to bring them home.", SIGNAL,
            )
        armed = controller.crossing.armed
        if armed and controller.crossing_paused:
            return LinkState(
                "paused", INK, "Paused", "Held",
                f"Connected to {peer}. Crossing is held, so you can lean on an edge.", SIGNAL,
            )
        if armed and controller.full_screen_app is not None:
            return LinkState(
                "full_screen", INK, "Held", "Held",
                "Held: an app is full screen.", SIGNAL,
            )
        via = " through the macOS 27 tunnel" if tunnel else ""
        return LinkState("mac", INK, "On Mac", linked, f"Connected to {peer}{via}.", SIGNAL)
    if not (cfg.host and cfg.auth_token):
        return LinkState("unpaired", AMBER, "Not paired", "Setup", "Pair a machine on Overview and Beamer connects on its own.", AMBER)
    address = f"{cfg.host} port {cfg.port}"
    if kind == "forgotten":
        return LinkState("forgotten", FAULT, "Pairing removed", "Refused", f"{peer} no longer has this pairing: remove {peer} here and pair the two again.", FAULT)
    if kind in ("unauthenticated", "wrong_id", "different", "unreadable"):
        return LinkState("token", FAULT, "Token mismatch", "Refused", f"{peer} refused this Mac's token. Remove it, then pair again to write a fresh one.", FAULT)
    if kind in ("older", "newer", "not_beamer"):
        return LinkState("version", FAULT, "Version mismatch", "Refused", f"{peer} runs a different Beamer from this Mac. Update both to the same version.", FAULT)
    if status.endswith(NOT_WOKEN_SUFFIX):
        return LinkState("not_woken", FAULT, "Did not wake", "No reply", f"{peer} did not answer within a minute of the wake-up packet.", FAULT)
    if kind == "blocked":
        return LinkState("blocked", FAULT, "Blocked", "Tunnel", "macOS 27 blocked direct LAN access. Open Beamer Tunnel from Applications.", FAULT)
    if kind in ("unreachable", "timeout"):
        return LinkState("unreachable", FAULT, "Unreachable", "No reply", f"{address} is not answering. {peer} may be asleep or off this network.", FAULT)
    if kind == "refused":
        return LinkState("not_listening", FAULT, "Not listening", "No receiver", f"{peer} answers, but Beamer is not listening on port {cfg.port}. Open Beamer on {peer}.", FAULT)
    if kind in ("stopped", "closed"):
        return LinkState("stopped", FAULT, "No reply", "Dropped", f"{peer} stopped responding. Beamer keeps trying to reconnect.", FAULT)
    if status.startswith("Connecting to") or status in ("Waiting to connect", "Settings saved; reconnecting"):
        return LinkState("waiting", AMBER, "Waiting", "Looking", f"Looking for {peer}. It connects on its own once Beamer is open there.", AMBER, True)
    return LinkState("dropped", FAULT, "Disconnected", "Dropped", (status.rstrip(".") + ". Beamer keeps trying to reconnect.") if status else "Beamer keeps trying to reconnect.", FAULT)
