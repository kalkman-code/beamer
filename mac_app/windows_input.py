"""The other direction: other machines' keyboards and pointers arriving on this Mac.

A listener on this Mac's port running core.receiver.LinkResponder, the responder the other
desktops run, with this Mac's own injector, clipboard and display geometry handed to it. Nothing
about the protocol or the crossing is duplicated here; this module is only the wiring that says
which platform's modules to use and what to do when input arrives.

The two directions never overlap. The responder's `owner_callback` tells the controller when a
machine has taken this Mac or let it go, which stops the controller's own tap from reading a
pointer another machine is moving as a push against this Mac's edge, and the controller's owner
tells the responder whether this Mac's own input is away, which it refuses a take for.
"""

import json
import logging
import threading

import clipboard_mac
import crossing
import desktop_mac
import hardware_mac
import input_injector_mac
import no_unlock
from core import protocol
from core import receiver
from core.receiver import LinkResponder, ServerState

LOGGER = logging.getLogger("Beamer")


class WindowsInput:
    """Owns the listener and keeps it in step with the settings. `sync` is called from the status
    tick, so a port, a pairing or a switch changed in the window takes effect without anyone
    restarting Beamer."""

    def __init__(self, controller, logger=None, arrangement_callback=None, pressure_callback=None, arrival_callback=None):
        self._controller = controller
        self._logger = logger or LOGGER
        # A peer can change the arrangement too, and it reaches this Mac over whichever of the two
        # links is up: this one, or the Mac's own, which bridge.py handles. Both end at the same
        # handler in the app, called with (peer, edge, set_at, by, way_back).
        self._arrangement_callback = arrangement_callback
        self._lock = threading.RLock()
        self._running_for = None
        self._speeds = None
        self._armed_for = None
        self.state = ServerState.STOPPED
        self.detail = "Not listening"
        self.settings_callback = None
        self.paired_callback = None
        self.server = LinkResponder(
            controller.book,
            controller.identity,
            status_callback=self._on_status,
            injector=input_injector_mac,
            desktop=desktop_mac,
            clipboard=clipboard_mac,
            unlock=no_unlock,
            hardware=hardware_mac.hardware_address_towards,
            away=lambda: controller.owner.away,
            zones=controller.book.zones,
            notch_span=lambda: controller.notch_range,
            owner_callback=self._on_owner,
            # The crossing effects: the push home through a zone, and a peer's pointer landing
            # here. Both fire on a link's thread; the app marshals them.
            pressure_callback=pressure_callback,
            arrival_callback=arrival_callback,
            arrangement_callback=self._on_arrangement,
            settings_callback=self._on_settings,
            paired_callback=self._on_paired,
            announce=self._announce,
        )
        controller.responder = self.server
        controller.send_peer_home = self.server.send_home
        # Pause crossing and the full-screen hold are about this screen: a peer's pointer does not
        # go onward or home through a held edge either.
        self.server.edges_held = lambda: controller.crossing_paused or controller.full_screen_app is not None

    @property
    def receiving(self) -> bool:
        return bool(self._controller.receiving)

    @property
    def peer_settings(self):
        """Whether a machine linked to this Mac keeps Same on all machines; None while none is, or
        none has told."""
        known = [self.server.caps_of(peer) for peer in self.server.links()]
        known = [caps for caps in known if caps is not None]
        return any("settings" in caps for caps in known) if known else None

    def sync(self, cfg) -> None:
        speeds = (cfg.pointer_speed, cfg.scroll_speed, cfg.reverse_scroll)
        if self.server.input_scale is None or speeds != self._speeds:
            self._speeds = speeds
            self.server.input_scale = receiver.InputScale(*speeds)
        wanted = self._controller.own_port
        with self._lock:
            if wanted == self._running_for and self.server.listening:
                # Only when the peers or the zones changed: re-arming starts every push over, and
                # this runs on the status tick while another machine may be pushing through a zone.
                held = self._held()
                if held != self._armed_for:
                    self._armed_for = held
                    self.server.peers_changed()
                    self.server.rearm()
                return
            if not wanted:
                return
            if self._running_for is not None:
                self.server.stop()
            self._running_for = wanted
            self._armed_for = self._held()
        try:
            self.server.start(wanted)
        except Exception:
            self._logger.exception("The listener for other machines' input could not start")

    def _held(self):
        """What the responder arms and answers from: the peers' ids, tokens, sides and switches, and the zones."""
        book = self._controller.book
        peers = tuple((p.get("id"), p.get("token"), p.get("side"), p.get("allow_drive")) for p in book.peers())
        return peers, json.dumps(book.zones(), sort_keys=True)

    def stop(self) -> None:
        with self._lock:
            self._running_for = None
        self.server.stop()
        self._controller.set_receiving(False)
        try:
            input_injector_mac.release_all()
        except Exception:
            self._logger.exception("Could not release the keys another machine was holding")

    def _outbound_up(self, peer_id: bytes) -> bool:
        link = self._controller._peers_up.get(protocol.id_text(peer_id))
        return link is not None and link.live()

    def send_arrangement(self, peer: str, mac_edge: str, set_at: int, by=None) -> bool:
        """Tell `peer` (a b64 id) which edge of this Mac faces it, as the controller's send_arrangement
        does, over the link it opened to this Mac, when this Mac has none open to it (WIRE.md section 3,
        "Which link"). False when it has no link here either, which is not a failure worth reporting."""
        ident = protocol.read_id(peer)
        if ident is None or self._outbound_up(ident):
            return False
        author = protocol.read_id(by) or bytes(self._controller.identity()["id"])
        return self.server.send(ident, protocol.arrangement_v6(mac_edge, int(set_at), author,
                                                               way_back=self._controller.way_back(peer)))

    def send_settings(self, data, source=None) -> bool:
        """This Mac's settings state to every machine linked to it that keeps it and that this Mac
        has no link open to, `source` (a b64 id, the machine a newer state came from) excepted."""
        sent = False
        for peer in self.server.links():
            caps = self.server.caps_of(peer)
            if protocol.id_text(peer) == source or caps is None or "settings" not in caps or self._outbound_up(peer):
                continue
            sent = self.server.send(peer, protocol.settings_msg(data)) or sent
        return sent

    def _announce(self, peer: bytes):
        """What this Mac tells a machine the moment it links in: where the two sit, its settings
        state, and who else this Mac has, unless this Mac has a link of its own open to it (that
        link carries them)."""
        if self._outbound_up(peer):
            return []
        text = protocol.id_text(peer)
        entry = next((item for item in self._controller.book.peers() if item.get("id") == text), None)
        if entry is None:
            return []
        messages = []
        if entry.get("side") in crossing.EDGES:
            by = protocol.read_id(entry.get("side_by")) or bytes(self._controller.identity()["id"])
            messages.append(protocol.arrangement_v6(entry["side"], entry.get("side_set_at", 0), by,
                                                    way_back=self._controller.way_back(text)))
        caps = self.server.caps_of(peer) or frozenset()
        if "settings" in caps:
            messages += [m for m in self._controller.announce() if m.get("type") == protocol.MSG_SETTINGS]
        if entry.get("send") is True:
            ids = [protocol.read_id(item.get("id")) for item in self._controller.book.peers() if item.get("id") != text]
            messages.append(protocol.paired_msg([ident for ident in ids if ident is not None][:protocol.MAX_PEERS]))
        return messages

    def _on_arrangement(self, peer, read) -> None:
        if self._arrangement_callback is None:
            return
        self._arrangement_callback(protocol.id_text(peer), read["edge"], read["set_at"], protocol.id_text(read["by"]),
                                   read.get("way_back"))

    def _on_settings(self, peer, data) -> None:
        if self.settings_callback is not None:
            self.settings_callback(data, protocol.id_text(peer))

    def _on_paired(self, peer, ids) -> None:
        self._controller.book.store_paired(protocol.id_text(peer), [protocol.id_text(ident) for ident in ids])
        if self.paired_callback is not None:
            self.paired_callback(protocol.id_text(peer), [protocol.id_text(ident) for ident in ids])

    def _on_status(self, state, detail) -> None:
        self.state = state
        self.detail = detail

    def _on_owner(self, peer) -> None:
        # The responder itself releases whatever the owner was holding before this fires.
        self._controller.driver = protocol.id_text(peer) if peer is not None else None
        self._controller.set_receiving(peer is not None)
