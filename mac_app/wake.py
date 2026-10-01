"""The controller that wakes the PC when a switch finds it asleep. The packet, the ARP lookup and
the address parsing are in wol.py, shared with the Windows app, which wakes this Mac the same way.
"""

from __future__ import annotations

import threading

from bridge import KVMController
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

    `on_mac_learned(mac)` fires off the connection thread when the ARP table names a different
    address from the one stored; the app persists it."""

    WAKE_POLL_SECONDS = 0.5

    def __init__(self, cfg, wake_sender=send_magic_packet, mac_lookup=lookup_mac, **kwargs):
        self._waking = False
        self._wake_lock = threading.Lock()
        self.wake_sender = wake_sender
        self.mac_lookup = mac_lookup
        self.on_mac_learned = None
        super().__init__(cfg, **kwargs)

    @property
    def connection_status(self):
        return waking_status(self._wake_name()) if self._waking else KVMController.connection_status.fget(self)

    @connection_status.setter
    def connection_status(self, value):
        self._connection_status = value

    def _wake_name(self):
        return self.peer_label or self.cfg.pc_name or self.cfg.host or "the other machine"

    @property
    def waking(self) -> bool:
        return self._waking

    @property
    def can_wake(self) -> bool:
        return bool(self.cfg.mac_address) and not self.connected

    def set_redirecting(self, value, edge=None, offset=None, came_home=True):
        if value and not self.redirecting and not self.connected and self.cfg.mac_address and not self._refused():
            self.wake()
            return False
        return super().set_redirecting(value, edge, offset, came_home=came_home)

    def _refused(self) -> bool:
        """A PC that answered and refused -- the token, or the protocol version -- is awake, and
        waking it would hide why; the switch says the refusal instead."""
        return self.status_kind in REFUSED_KINDS

    def wake(self) -> bool:
        """Sends the packet and starts waiting, unless a wake is already in flight."""
        with self._wake_lock:
            if self._waking or self.connected:
                return False
            self._waking = True
        threading.Thread(target=self._wake_worker, name="wake", daemon=True).start()
        return True

    def _wake_worker(self):
        started = self.clock()
        name = self._wake_name()
        try:
            self.wake_sender(self.cfg.mac_address, self.cfg.host)
        except (OSError, ValueError) as exc:
            self.logger.warning("wake-on-LAN packet not sent: %s", exc)
            self._waking = False
            self.connection_status = not_woken_status(name)
            self._alert("Beamer", "Could not send the wake-up packet")
            return
        self.logger.info("sent wake-on-LAN to %s", self.cfg.host)
        self._alert("Beamer", waking_status(name))
        while not self.stop_event.wait(self.WAKE_POLL_SECONDS):
            if self.connected:
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

    def _primary_up(self, link, fields):
        super()._primary_up(link, fields)
        # Off the link's thread: `arp` can take seconds, and the link's first read, which the peer
        # gives 2.5 seconds, waits behind this callback.
        host = (link.entry() or {}).get("host") or self.cfg.host
        threading.Thread(target=self._learn_mac, args=(host,), name="Beamer-learn-mac", daemon=True).start()

    def _learn_mac(self, host):
        try:
            mac = self.mac_lookup(host)
        except Exception:
            self.logger.exception("hardware address lookup failed")
            return
        with self._cfg_lock:
            if not mac or mac == self.cfg.mac_address:
                return
            self.cfg.mac_address = mac
        self.logger.info("learned the PC's hardware address from the ARP table")
        callback = self.on_mac_learned
        if callback is not None:
            try:
                callback(mac)
            except Exception:
                self.logger.exception("on_mac_learned callback failed")
