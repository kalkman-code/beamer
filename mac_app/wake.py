"""The controller that wakes the PC when a switch finds it asleep. The packet, the ARP lookup and
the address parsing are in wol.py, shared with the Windows app, which wakes this Mac the same way.
"""

from __future__ import annotations

import threading

from bridge import KVMController
from core import protocol
from core.wol import WAKE_WINDOW_SECONDS, lookup_mac, mac_from_arp_output, magic_packet, parse_mac, send_magic_packet

__all__ = [
    "NOT_WOKEN_SUFFIX", "WAKE_WINDOW_SECONDS", "WakingController", "not_woken_status", "waking_status",
    "lookup_mac", "mac_from_arp_output", "magic_packet", "parse_mac", "send_magic_packet",
]

# What the link says when the machine answered and would not have it: a different Beamer, an older
# or newer one, a pairing it holds under another machine, or a first frame it could not read.
REFUSED_KINDS = ("older", "newer", "different", "unauthenticated", "wrong_id", "unreadable")

NOT_WOKEN_SUFFIX = " did not wake"


def waking_status(name):
    return f"Waking {name}…"


def not_woken_status(name):
    return f"{name}{NOT_WOKEN_SUFFIX}"


class WakingController(KVMController):
    """KVMController plus wake-on-LAN. A switch attempted while the PC is unreachable sends the
    magic packet and shows waking_status until the connection worker gets through, the same
    shape as the receiver's "Unlocking <name>…" -- and, like that path, nothing is queued: the
    switch is refused, and the person switches again once the PC is up. Completing it for them
    a minute later, into whatever they had gone back to typing on the Mac, would be worse.

    `on_mac_learned(peer, mac)` fires off the connection thread when the ARP table names a
    different address for a machine from the one stored for it; the app persists it."""

    WAKE_POLL_SECONDS = 0.5

    def __init__(self, cfg, wake_sender=send_magic_packet, mac_lookup=lookup_mac, **kwargs):
        self._waking = False
        self._waking_peer = None
        self._wake_lock = threading.Lock()
        self.wake_sender = wake_sender
        self.mac_lookup = mac_lookup
        self.on_mac_learned = None
        super().__init__(cfg, **kwargs)

    @property
    def connection_status(self):
        if self._waking:
            return waking_status(self._wake_name(self._waking_peer))
        return KVMController.connection_status.fget(self)

    @connection_status.setter
    def connection_status(self, value):
        self._connection_status = value

    def _wake_name(self, peer=None):
        peer = peer or self._in_question()
        name = self._label(lambda entry: entry.get("id") == peer) if peer else None
        return name or self.peer_label or self.cfg.pc_name or self.cfg.host or "the other machine"

    def _wake_address(self, peer):
        """`peer`'s (hardware address, host): its entry's; a Config-built controller's flat settings'."""
        if self._memory is not None:
            return self.cfg.mac_address, self.cfg.host
        entry = next((item for item in self.book.peers() if item.get("id") == peer), None) or {}
        return entry.get("hw") or "", entry.get("host") or ""

    def _awake(self, peer):
        if self._memory is not None:
            return self.connected
        link = self._peers_up.get(peer) if peer else None
        return link is not None and link.live()

    @property
    def waking(self) -> bool:
        return self._waking

    @property
    def can_wake(self) -> bool:
        """Whether the machine in question (where input is, else where the shortcut would send it)
        is asleep and can be woken."""
        peer = self._in_question()
        return bool(self._wake_address(peer)[0]) and not self._awake(peer)

    def set_redirecting(self, value, edge=None, offset=None, came_home=True, peer=None):
        target = peer or self._shortcut_peer()
        # From home, or on from the machine input is on to another (a menu naming it): one asleep is woken.
        leaving = not self.redirecting or (peer is not None and peer != self.owner.on)
        if (value and leaving and not self._awake(target) and self._wake_address(target)[0]
                and not self._refused(target)):
            self.wake(target)
            return False
        return super().set_redirecting(value, edge, offset, came_home=came_home, peer=peer)

    def _refused(self, peer=None) -> bool:
        """A machine that answered and refused -- the pairing, or the protocol version -- is awake,
        and waking it would hide why; the switch says the refusal instead."""
        link = self._link_of(peer) if peer else None
        kind = link.kind if link is not None else self.status_kind
        return kind in REFUSED_KINDS

    def wake(self, peer=None) -> bool:
        """Sends `peer` (the machine in question when not given) the packet and starts waiting,
        unless a wake is already in flight."""
        peer = peer or self._in_question()
        with self._wake_lock:
            if self._waking or self._awake(peer):
                return False
            self._waking = True
            self._waking_peer = peer
        threading.Thread(target=self._wake_worker, args=(peer,), name="wake", daemon=True).start()
        return True

    def _wake_worker(self, peer=None):
        started = self.clock()
        name = self._wake_name(peer)
        mac, host = self._wake_address(peer)
        try:
            self.wake_sender(mac, host)
        except (OSError, ValueError) as exc:
            self.logger.warning("wake-on-LAN packet not sent: %s", exc)
            self._waking = False
            self.connection_status = not_woken_status(name)
            self._alert("Beamer", "Could not send the wake-up packet")
            return
        self.logger.info("sent wake-on-LAN to %s", host)
        self._alert("Beamer", waking_status(name))
        while not self.stop_event.wait(self.WAKE_POLL_SECONDS):
            if self._awake(peer):
                self._waking = False
                self.logger.info("%s woke and connected", name)
                self._alert("Beamer", f"{name} is awake — switch again to send input")
                return
            if self.clock() - started >= WAKE_WINDOW_SECONDS:
                break
        self._waking = False
        self.connection_status = not_woken_status(name)
        self.logger.warning("%s did not answer within %.0fs of the wake-on-LAN packet", name, WAKE_WINDOW_SECONDS)
        self._alert("Beamer", not_woken_status(name))

    def _peer_up(self, link, fields):
        super()._peer_up(link, fields)
        # Off the link's thread: `arp` can take seconds, and the link's first read, which the peer
        # gives 2.5 seconds, waits behind this callback.
        host = (link.entry() or {}).get("host") or self.cfg.host
        peer = protocol.id_text(fields["id"])
        threading.Thread(target=self._learn_mac, args=(peer, host), name="Beamer-learn-mac", daemon=True).start()

    def _learn_mac(self, peer, host):
        try:
            mac = self.mac_lookup(host)
        except Exception:
            self.logger.exception("hardware address lookup failed")
            return
        if self._memory is not None:
            with self._cfg_lock:
                if not mac or mac == self.cfg.mac_address:
                    return
                self.cfg.mac_address = mac
        elif not mac or mac.lower() == self._wake_address(peer)[0].lower():
            return
        self.logger.info("learned %s's hardware address from the ARP table", self._wake_name(peer))
        callback = self.on_mac_learned
        if callback is not None:
            try:
                callback(peer, mac)
            except Exception:
                self.logger.exception("on_mac_learned callback failed")
