"""This PC's own keyboard and mouse going to its peers: the Windows half of WIRE.md sections 3 to 5.

One outbound link per peer with `send` on (core/link.py), and the owner state machine
(core/owner.py) deciding where each input event goes: here, or to one peer. This module is the glue
between the two and the platform: it feeds the owner the events the hooks capture, carries out what
the owner answers (messages on links, the clipboard, the pointer landing, the pin that holds the
pointer still while input is elsewhere), and arms this PC's own zones (WIRE.md section 8) so a push
through one takes the peer beyond it.

No platform name is imported here: the desktop, the clipboard and the way a hook says what happened
(`on_button`, `on_wheel`, `on_pointer_move`, `on_motion`, `on_key`) are handed in, so the Linux
shell reaches the same code. Keys arrive with the names Windows capture gives them, the Windows
key as "ctrl" and Ctrl as "cmd", and leave as the physical key (`PHYSICAL`) before core/keytable.py
names them for each peer.

The shortcut and the Send button pick the machine core.ways.shortcut_peer names: the one input was
last on while it can take input, else the first that can. A tray item per machine picks its own,
and a push through a zone picks that zone's peer.
"""

import collections
import ipaddress
import logging
import queue
import sys
import threading
import time
from typing import Callable, Optional

from core import ignored
from core import keytable
from core import owner as owner_module
from core import peerlist
from core import protocol
from core import receiver
from core import return_edge
from core import ways
from core import wol

LOGGER = logging.getLogger(__name__)

SEND_QUEUE_SIZE = 4096
MONITORS_MAX_AGE_SECONDS = 1.0
TICK_SECONDS = 0.1
ROUND_TRIP_MAX_AGE_SECONDS = 5.0
WAKE_POLL_SECONDS = 0.5
# How long a machine that is being driven waits for its driver to let go before it gives up on
# the move it was asked for: the responder's own forced end is one second.
LET_GO_WAIT_SECONDS = 1.5
WAKING_STATUS = "Waking {name}…"
NOT_WOKEN_STATUS = "{name} did not wake"
NOT_CONNECTED_STATUS = "Not connected"

# The capture names this PC's Ctrl "cmd" and its Windows key "ctrl", the Semantic style; the key
# table takes the physical key, so the two are swapped back here.
PHYSICAL = {"cmd": "ctrl", "cmd_r": "ctrl_r", "ctrl": "cmd", "ctrl_r": "cmd_r"}
MEDIA_KEYS = frozenset(
    {"volume_mute", "volume_down", "volume_up", "media_next", "media_prev", "media_stop", "media_play_pause"}
)
MODIFIER_KEYS = frozenset({"shift", "shift_r", "ctrl", "ctrl_r", "alt", "alt_r", "cmd", "cmd_r"})
# What this machine tells its peers it is (WIRE.md section 2); Linux and Windows read keys alike.
OWN_PLATFORM = "linux" if sys.platform.startswith("linux") else "windows"
INPUT_TYPES = protocol.INPUT_TYPES

# What the owner's reasons for bringing input home say, naming the machine (WIRE.md section 5).
HOME_REASONS = {
    "not_allowed": "{name} does not accept input from this one",
    "sent_home": "{name} sent the input back",
    "link_lost": "Lost the link to {name}",
    "no_answer": "{name} did not answer",
    "refused": "{name} would not take the input",
    "malformed": "{name} would not take the input",
}


def _at_edge(edge) -> str:
    return f", at its {edge} edge" if isinstance(edge, str) else ""


def is_this_machine(host: str, local_addresses=None, address_towards=None) -> bool:
    """Whether `host` is this PC rather than a peer. Worth a guard of its own: a peer's address is
    learned from the peer address of its own link, and when the Mac connects through the macOS 27
    SSH tunnel that address is 127.0.0.1 -- this PC's. Connecting there would reach this PC's own
    receiver, authenticate with the pair's token, and the responder would take it for the peer's
    second link and close the real one.

    Two answers are consulted, because the first one fails silently: the hostname-based address
    list is empty on a PC whose name does not resolve, and an empty list would have said "not this
    machine" for this machine's own address. The route probe asks the stack which local address
    would reach `host`; for this PC's own address that is the address itself."""
    if not host:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback or address.is_unspecified:
        return True
    from core import pairing

    if local_addresses is None:
        local_addresses = pairing._local_ipv4_addresses()
    if host in local_addresses:
        return True
    if address_towards is None:
        address_towards = pairing.local_address_towards
    try:
        return address_towards(host) == host
    except OSError:
        return False


class LinkSender:
    """The links to every peer this PC may send to, and the decision -- taken on the hook thread --
    of whether each local input event belongs to this PC or to one of them.

    `book` is a `receiver.PeerBook` over the app's settings and `zones()` gives this PC's zones as
    settings.json keeps them. `identity()` is this machine as a `hello` says it. Callbacks, each
    from a worker thread (a GUI marshals them): `status_callback(connected, detail)` for the machine
    the shortcut would pick, `redirect_callback(redirecting)`, `pressure_callback(edge, pressure, crossed, part)` and
    `arrival_callback(edge, x, y)`. `driven()` says whether a peer's input is on this PC, and
    `send_peer_home()` sends it home, both the responder's, handed in by the app that owns both
    halves. `desktop` and `clipboard` are the platform's modules. `hardware(host)` is this PC's
    hardware address on the interface that reaches `host`, which each link sends in its own `hello`."""

    def __init__(
        self,
        book: receiver.PeerBook,
        identity: Callable[[], dict],
        *,
        zones: Callable[[], list] = lambda: [],
        status_callback: Callable[[bool, str], None] = None,
        redirect_callback: Callable[[bool], None] = None,
        pressure_callback: Callable[[str, float, bool, Optional[str]], None] = None,
        arrival_callback: Callable[[str, int, int], None] = None,
        desktop=None,
        clipboard=None,
        links=None,
        is_local=is_this_machine,
        clock=time.monotonic,
        wake_sender=wol.send_magic_packet,
        mac_lookup=wol.lookup_mac,
        hardware: Callable[[str], str] = None,
    ) -> None:
        self._book = book
        self._identity = identity
        self._hardware = hardware
        self._zones = zones
        self._status_callback = status_callback
        self._redirect_callback = redirect_callback
        self._pressure_callback = pressure_callback
        self._arrival_callback = arrival_callback
        self._desktop = desktop
        self._clipboard = clipboard
        self._is_local = is_local
        self._clock = clock
        self._wake_sender = wake_sender
        self._mac_lookup = mac_lookup

        self._config = None
        self._config_lock = threading.RLock()
        # One lock for the owner, the zone models and every decision they feed. Held for the
        # owner's call and the queueing of what it answers, never across a socket or the clipboard.
        self._lock = threading.RLock()
        self._owner: Optional[owner_module.Owner] = None
        self._own_id: Optional[bytes] = None
        self._peers: dict = {}          # peer id: the entry as the settings hold it
        self._entries: list = []        # every entry, in the order the UI shows them
        self._zone_list: list = []
        self._models: list = []         # [(peer id, model)], this PC's zones in use
        self._ignore_gate = ignored.Gate()
        # The name a key went down under, by virtual key, so its release matches even when Shift or
        # the style changes while it is held.
        self._keys_down: dict = {}
        self._accepts: dict = {}        # peer id: whether it lets this PC drive it
        self._sent_names: dict = {}     # (peer, physical key): the name its press went out under
        self._buttons_held = set()
        self._pin_point = None
        self._monitors = None
        self._monitors_at = 0.0
        self._clipboard_stamp = None
        # The machine this PC's input last landed on, as an id text, for the shortcut's choice.
        self._last_on: Optional[str] = None
        self._jump_modifiers = set()
        self._jump_swallowed = set()
        self._jump_pressed = set()
        self.jump_recording = False
        self._reported_redirecting = False
        self._followed_at = 0.0
        self._overflowed = False
        self._armed_for = frozenset()
        self._clip_wait: dict = {}      # peer: actions held back while its clipboard is read
        self._clip_lock = threading.Lock()
        self._clip_jobs = collections.deque()
        self._clip_busy = False
        self._statuses: dict = {}
        self._waking = set()
        self._wake_lock = threading.Lock()
        self._outbound: "queue.Queue[tuple]" = queue.Queue(maxsize=SEND_QUEUE_SIZE)
        self._stop_event = threading.Event()
        self._threads = []

        # Pause crossing, which a restart forgets, and the name of a full-screen app in front, set
        # by the app: both hold the edges and leave the shortcut working, as on the Mac.
        self.crossing_paused = False
        self._full_screen_app = None
        # (title, message) for a notification, called off the GUI thread, and the hardware address
        # of a peer that did not say its own, read from the ARP table while its entry is fresh.
        self.on_alert = None
        # What discovery hears, dicts with `name` and `address`, for following a peer whose address
        # changed (section 6); the pairing window supplies it.
        self.machines: Callable[[], list] = lambda: []
        self.hw_learned: Optional[Callable[[bytes, str], None]] = None
        # Whether this machine's input can be taken now. Always, except where only the desktop can
        # capture it (Wayland), which it does at an edge barrier: there the shortcut and the tray
        # cannot send input away, only an edge push can.
        self.input_held: Callable[[], bool] = lambda: True
        # The responder's: whether a peer's input is on this PC, and sending it home.
        self.driven: Callable[[], bool] = lambda: False
        self.send_peer_home: Callable[[], bool] = lambda: False
        # The messages to send a peer once its link is up, and what to do with each kind that
        # arrives: set by the app, called on a link's thread.
        self.announce: Callable[[bytes], list] = lambda peer: []
        self.arrangement_callback: Optional[Callable] = None
        self.settings_callback: Optional[Callable] = None
        self.paired_callback: Optional[Callable] = None
        self.link_callback: Optional[Callable[[bytes, bool], None]] = None

        self.links = links(self) if links is not None else _default_links(self)

    # -- state the GUI reads ------------------------------------------------

    @property
    def redirecting(self) -> bool:
        owner = self._owner
        return owner is not None and owner.away

    @property
    def owner(self) -> Optional[bytes]:
        """The peer this PC's input is on, or None at home."""
        owner = self._owner
        return owner.on if owner is not None else None

    def expects_clipboard(self, link, began) -> bool:
        """For a link's `large` hook: whether a frame its peer began sending at `began` could be the
        clipboard of a let-go."""
        owner, peer = self._owner, protocol.read_id(link.peer_id)
        return owner is not None and peer is not None and owner.expects_clipboard(peer, began)

    @property
    def connected(self) -> bool:
        peer = self.shortcut_target()
        return peer is not None and self.links.up(peer)

    @property
    def status(self) -> str:
        """The link status of the machine the shortcut would take input to; before any entry has an
        id (the one migrated from 1.4.x), of the first this PC dials, by its token."""
        peer = self.shortcut_target()
        entry = self._peers.get(peer) if peer is not None else next(
            (item for item in self._entries if item.get("in_use", True) is True and item.get("send")
             and item.get("host") and item.get("port")), None)
        return (self._statuses.get(self.links.key_of(entry)) if entry is not None else None) or NOT_CONNECTED_STATUS

    def shortcut_target(self) -> Optional[bytes]:
        """Where the shortcut, the Send button and the tray's toggle send this PC's input (WIRE.md
        section 5, core.ways.shortcut_peer); None when this PC sends to no machine."""
        def ready(ident) -> bool:
            peer = protocol.read_id(ident)
            return peer is not None and self.links.up(peer) and self._accepts.get(peer) is True

        return protocol.read_id(ways.shortcut_peer(self._entries, self._last_on, ready))

    @property
    def waking(self) -> bool:
        return bool(self._waking)

    def is_waking(self, peer: bytes) -> bool:
        return peer in self._waking

    @property
    def round_trip_ms(self) -> Optional[int]:
        """The upper median of the last few trips from an input event's send to the ACK naming it,
        less the time the peer held that ACK, on the link the input is on; None while input is
        here or nothing fresh has been acknowledged."""
        peer = self.owner
        return self.links.round_trip_ms(peer) if peer is not None else None

    @property
    def full_screen_app(self):
        """The full-screen app holding the edges, or None: also None while this PC's own setting
        has the hold off, read here so every reader of the hold agrees and a change applies at once."""
        if self._setting("hold_full_screen", False):
            return self._full_screen_app
        return None

    @full_screen_app.setter
    def full_screen_app(self, name) -> None:
        self._full_screen_app = name

    @property
    def edges_held(self) -> bool:
        return self.crossing_paused or self.full_screen_app is not None

    @property
    def shortcut_armed(self) -> bool:
        """Whether the double-tap may move input. The app asks before acting on the trigger, so
        turning the shortcut off here turns it off everywhere."""
        return "shortcut" in set(self._setting("crossing_methods", ("edge", "shortcut")) or ())

    # -- lifecycle ----------------------------------------------------------

    def start(self, config) -> None:
        self.update_config(config)
        if self._threads:
            return
        self._stop_event.clear()
        for name, target in (("Beamer-links-out", self._outbound_worker), ("Beamer-links-tick", self._tick_worker)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        self.links.start()

    def stop(self) -> None:
        self._buttons_held.clear()
        self.set_redirecting(False)
        # The let-go and the releases ahead of it are sent before anything stops.
        deadline = self._clock() + 1.0
        while self._threads and (not self._outbound.empty() or self._clip_wait or self._clip_busy) and self._clock() < deadline:
            time.sleep(0.01)
        self._stop_event.set()
        self.links.stop()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = []

    def update_config(self, config) -> None:
        with self._config_lock:
            self._config = config
        self._ignore_gate.configure(getattr(config, "ignored_inputs", None) or ())
        own = protocol.read_id(getattr(config, "machine_id", "")) if config is not None else None
        with self._lock:
            if own is not None and own != self._own_id:
                self._own_id = own
                self._owner = owner_module.Owner(own, self._clock, self._resistance())
            elif self._owner is not None:
                self._owner.resistance_px = self._resistance()
        self.refresh()

    def refresh(self) -> None:
        """Reads the peers and zones as the app holds them now, brings the links in step and
        rebuilds the zones: after any change to either, and when a link has learnt a peer's id."""
        try:
            entries = self._book.peers()
            zones = list(self._zones() or [])
        except Exception:
            LOGGER.exception("The peers could not be read")
            return
        peers = {}
        for entry in entries:
            peer = protocol.read_id(entry.get("id"))
            if peer is not None:
                peers[peer] = entry
        with self._lock:
            self._entries, self._peers, self._zone_list = entries, peers, zones
            self._arm_locked()
        self.links.sync(entries)

    # -- the hooks' decisions -----------------------------------------------

    def _live(self) -> bool:
        """Whether input may still be taken from this PC. Deliberately cheap and lock-light: it is
        read on the hook thread for every keystroke and every mouse move, and a hook that cannot
        answer promptly is a hook Windows removes -- with the user's keyboard inside it."""
        peer = self.owner
        return peer is None or self.links.live(peer)

    def _lost(self) -> None:
        peer = self.owner
        if peer is None:
            return
        LOGGER.error("the link to %s went quiet; input returns to this PC", self._name(peer))
        self.links.close(peer)
        self._forget_held(peer)
        self._feed(lambda o: o.link_down(peer))

    def handle_jump_key(self, name: str, down: bool, vk: Optional[int] = None) -> bool:
        """Handles a recorded local chord before trigger detection or ordinary key routing. Keys are
        known by virtual key where there is one: the name carries Shift, so a Shift let go before the
        key would make its release a different name."""
        modifiers = {"alt", "alt_r", "cmd", "cmd_r", "ctrl", "ctrl_r", "shift", "shift_r"}
        if name in modifiers:
            if down:
                self._jump_modifiers.add(name)
            else:
                self._jump_modifiers.discard(name)
            return False
        held = vk if vk is not None else name
        if not down:
            self._jump_pressed.discard(held)
            if held in self._jump_swallowed:
                self._jump_swallowed.discard(held)
                return True
            return False
        # A key already down when its modifiers arrive repeats; it is not a press of the chord.
        repeat = held in self._jump_pressed
        self._jump_pressed.add(held)
        if held in self._jump_swallowed:
            return True
        if repeat or self.jump_recording:
            return False
        chord = ways.jump_chord(self._jump_modifiers, name)
        target = protocol.read_id(ways.jump_peer(self._entries, chord)) if chord else None
        if target is None:
            return False
        self._jump_swallowed.add(held)
        if self.owner == target:
            self.set_redirecting(False)
        else:
            self.go(target)
        return True

    def on_dead_key(self, vk: Optional[int] = None) -> bool:
        """Swallow a dead key only while its live peer owns this PC's input."""
        owner = self._owner
        if owner is None or not owner.away:
            return False
        if not self._live():
            self._lost()
            return False
        if vk is not None and self._ignore_gate.keeps(ignored.key(vk), True):
            return False
        return True

    def on_key(self, name: str, down: bool, vk: Optional[int] = None, us: Optional[str] = None) -> bool:
        """One key, from the hook thread: True when it is not for this PC. Held keys are known by
        their virtual key where there is one: the name is the character with Shift applied, so
        Shift let go before A made A's release arrive as "a" and miss."""
        owner = self._owner
        if owner is None:
            return False
        held = vk if vk is not None else name
        if down:
            data = self._keys_down.get(held) or self._key_data(name, us)
            self._keys_down[held] = data
        else:
            data = self._keys_down.pop(held, None) or self._key_data(name, us)
        if owner.away:
            if not self._live():
                self._lost()
            elif vk is not None and self._ignore_gate.keeps(ignored.key(vk), down):
                return False
        return self._input({"type": protocol.MSG_KEYDOWN if down else protocol.MSG_KEYUP, "data": dict(data)})

    def on_text(self, text: str, vk: Optional[int] = None) -> bool:
        """Sends a composed key sequence as text when this PC's input is away."""
        owner = self._owner
        if owner is None:
            return False
        if owner.away:
            if not self._live():
                self._lost()
                return False
            elif vk is not None and self._ignore_gate.keeps(ignored.key(vk), True):
                return False
        actions = []
        with self._lock:
            owner = self._owner
            if owner is None:
                return False
            message = protocol.text_msg(text)
            message["_bracket_modifiers"] = True
            actions = owner.input(message)
            self._queue(actions)
        self._report(actions)
        return not any(action is owner_module.LOCAL for action in actions)

    @staticmethod
    def _key_data(name: str, us: Optional[str]) -> dict:
        """A key message's data, with the physical key's name. `us` is the key's place on a US
        keyboard, which the receiver types on when its layout cannot type the character: a PC on
        Russian sends C as "с"."""
        data = {"key": PHYSICAL.get(name, name)}
        if us is not None:
            data["us"] = us
        return data

    def on_button(self, name: str, down: bool) -> bool:
        """A mouse button, from the hook thread: True when it is not for this PC."""
        if down:
            self._buttons_held.add(name)
        else:
            self._buttons_held.discard(name)
        owner = self._owner
        if owner is None:
            return False
        if owner.away:
            if not self._live():
                self._lost()
            elif self._ignore_gate.keeps(ignored.button(name), down):
                return False
        return self._input({"type": protocol.MSG_MOUSEDOWN if down else protocol.MSG_MOUSEUP, "data": {"button": name}})

    def on_wheel(self, dy: float, dx: float) -> bool:
        """The wheel, in notches, positive away from the hand: True when it is not for this PC."""
        if not self.redirecting:
            return False
        if not self._live():
            self._lost()
            return False
        return self._input(protocol.scroll_msg(dy, dx, "line"))

    def on_pointer_move(self) -> bool:
        """A move the hook saw, never the distance it covered, which comes from on_motion. While
        input is elsewhere the pointer is put back where it was, because a hook that returns True
        stops the message but the cursor has already been moved by the time it runs. True when the
        move is not for this PC."""
        if not self.redirecting:
            return False
        if not self._live():
            self._lost()
            return False
        self._warp_to_pin()
        return True

    def on_motion(self, dx: int, dy: int) -> None:
        """One raw mouse movement, in the mouse's own counts. Consumes nothing: raw input cannot
        block an event, so the hook above is what keeps the movement off this PC."""
        if self.redirecting:
            if not self._live():
                self._lost()
                return
            self._input({"type": protocol.MSG_MOUSEMOVE, "data": {"dx": int(dx), "dy": int(dy)}})
            return
        self._press_edge(int(dx), int(dy))

    def _input(self, message: dict) -> bool:
        """Gives the owner one input event and carries out what it answers; True when this PC
        must not act on the event."""
        with self._lock:
            actions = self._owner.input(message)
            self._queue(actions)
        self._report(actions)
        return not any(action is owner_module.LOCAL for action in actions)

    def _press_edge(self, dx, dy) -> None:
        """One raw movement while input is still this PC's. Feeds the zone models, the same ones
        the responder uses for a driven pointer, so the border behaves identically whichever side
        of it the pointer starts on.

        Nothing is held here, unlike the responder's side of the same models: Windows has already
        stopped the cursor at the edge of the desktop, so the hold is the screen's own. Only the
        pressure is ours to measure, and only a breakthrough acts.

        It runs while a peer is driving this PC too. Every move a peer makes here carries
        INJECTED_MARK and never arrives, so what does is this PC's own mouse, and its push out
        means the pointer is going on with this mouse behind it."""
        if not self._models or self.edges_held or self._owner is None:
            return
        if self._buttons_held and self._setting("block_while_dragging", True) and self._still_dragging():
            # A drag that reaches the edge is dragging, not leaving: the push so far is dropped,
            # as the Mac drops it, so letting go at the edge does not finish a crossing.
            for _peer, model in self._models:
                model.reset()
            return
        try:
            desktop = self._desktop_module()
            monitors = self._cached_monitors()
            pointer = desktop.cursor_position()
            outcome = model = target = None
            for peer, candidate in list(self._models):
                result = candidate.feed(monitors, pointer, float(dx), float(dy))
                if result.action != return_edge.PASS:
                    outcome, model, target = result, candidate, peer
                    break
            if outcome is None:
                return
        except Exception:
            LOGGER.exception("The way out failed; crossing out is off until the next change")
            self._models = []
            return
        part = None
        if isinstance(model, return_edge.PartEdge):
            part = return_edge.part_of(return_edge.display_fraction(monitors, model.edge, pointer))
        elif isinstance(model, return_edge.CornerPush):
            # The corner's name, so the effects play their corner forms; the glow lights the edge.
            part = model.corner
        self._notify_pressure(model.edge, outcome.pressure, outcome.action == return_edge.CROSS, part)
        if outcome.action == return_edge.CROSS:
            offset = receiver.corner_offset(model.corner, model.edge) if isinstance(model, return_edge.CornerPush) else outcome.offset
            # The model names the edge on the far side, not the one just left: it is the same
            # border, read from the other end.
            self._go(target, outcome.edge, offset)

    def zone_models(self) -> list:
        """The models of this PC's ways out, where a desktop that captures input at barriers puts
        them; none while the edges are held."""
        if self.edges_held or self._owner is None:
            return []
        return [model for _peer, model in list(self._models)]

    def _still_dragging(self) -> bool:
        """A release the hook never saw -- let go over the secure desktop or an elevated window --
        would otherwise hold the edge shut for good, so each button is asked of Windows again."""
        is_down = getattr(self._desktop_module(), "button_down", None)
        if is_down is not None:
            try:
                self._buttons_held = {name for name in self._buttons_held if is_down(name)}
            except Exception:
                LOGGER.exception("Could not read the mouse buttons; treating none as held")
                self._buttons_held = set()
        return bool(self._buttons_held)

    # -- switching ----------------------------------------------------------

    def set_redirecting(self, value, came_home=True) -> bool:
        """Moves this PC's input to the shortcut's machine, or home, as the shortcut, the Send button,
        the tray and the direction switch do. False when nothing moved."""
        if value:
            if self.redirecting:
                return False
            peer = self.shortcut_target()
            if peer is None:
                self._alert(f"Cannot switch — {self.status}")
                return False
            return self._go(peer)
        if not self.redirecting:
            return False
        with self._lock:
            actions = self._owner.go(None)
            self._queue(actions)
        self._report(actions)
        return True

    def toggle(self) -> None:
        """The shortcut's way across and back, for when the pointer is not at an edge -- or the
        edge is off."""
        self.set_redirecting(not self.redirecting)

    def go(self, peer: bytes, edge: Optional[str] = None, offset: Optional[float] = None) -> bool:
        return self._go(peer, edge, offset)

    def _go(self, peer: bytes, edge=None, offset=None) -> bool:
        owner = self._owner
        if owner is None:
            return False
        if not self.input_held():
            self._alert("Cannot switch — on this desktop input goes to another machine only by pushing the pointer "
                        "through an edge")
            return False
        if self.driven():
            # A machine that is driven does not drive (section 4): its driver goes home first, then
            # the move goes ahead. Waiting happens off this thread, which may be the hook's.
            threading.Thread(target=self._after_let_go, args=(peer, edge, offset), name="Beamer-links-home", daemon=True).start()
            return True
        with self._lock:
            self._note_clipboard()
            actions = owner.go(peer, edge, offset)
            self._queue(actions)
        self._report(actions)
        if owner.away and self.driven():
            # A take here won as this one was sent (section 4's busy rule): both stay at home.
            with self._lock:
                actions = owner.go(None)
                self._queue(actions)
            self._report(actions)
        return owner.away

    def _after_let_go(self, peer, edge, offset) -> None:
        if not self.send_peer_home() and self.driven():
            self._alert(f"Cannot switch — {self._name(peer)} could not be reached while this PC is being driven")
            return
        deadline = self._clock() + LET_GO_WAIT_SECONDS
        while self.driven() and self._clock() < deadline and not self._stop_event.is_set():
            time.sleep(0.02)
        if not self.driven() and not self.redirecting:
            self._go(peer, edge, offset)

    def set_receiving(self, receiving: bool) -> None:
        """A peer took this PC's input, or gave it back. Input cannot be here and elsewhere at
        once (section 4): if it was on a peer, it comes home."""
        if receiving and self.redirecting:
            self.set_redirecting(False)

    # -- inbound messages ---------------------------------------------------

    def on_link(self, peer: bytes, up: bool, accepts: bool = False) -> None:
        """A link came up (its handshake done) or went down, from its own thread."""
        with self._lock:
            if up:
                self._accepts[peer] = accepts
            else:
                self._accepts.pop(peer, None)
                self._forget_held(peer)
            actions = self._owner.link_up(peer, accepts) if up else self._owner.link_down(peer)
            self._queue(actions)
        self._report(actions)
        self.refresh()
        self._report_status()
        if up and self.hw_learned is not None:
            threading.Thread(target=self._learn_hardware, args=(peer,), name="Beamer-links-hw", daemon=True).start()
        callback = self.link_callback
        if callback is not None:
            self._call(callback, peer, up)

    def on_message(self, peer: bytes, message: dict, began_at: Optional[float] = None) -> None:
        """What a peer sent back on the link this PC opened to it, from that link's thread."""
        kind = message.get("type")
        data = message.get("data") or {}
        if kind == protocol.MSG_SWITCH:
            read = protocol.read_switch(message)
            if read is not None:
                with self._lock:
                    self._note_clipboard()
                    actions = self._owner.switch(peer, read)
                    self._queue(actions)
                self._report(actions)
        elif kind == protocol.MSG_ACCEPT:
            read = protocol.read_accept(message)
            if read is not None:
                with self._lock:
                    self._note_clipboard()
                    actions = self._owner.accept(peer, read)
                    self._queue(actions)
                self._report(actions)
        elif kind == protocol.MSG_REFUSE:
            read = protocol.read_refuse(message)
            if read is not None:
                with self._lock:
                    actions = self._owner.refuse(peer, read)
                    self._queue(actions)
                self._report(actions)
        elif kind == protocol.MSG_ACCEPTS:
            read = protocol.read_accepts(message)
            if read is not None:
                with self._lock:
                    self._accepts[peer] = read["accepts"]
                    actions = self._owner.accepts(peer, read["accepts"])
                    self._queue(actions)
                self._report(actions)
        elif kind == protocol.MSG_CLIPBOARD:
            read = protocol.read_clipboard(message)
            if read is not None:
                # Rebuilt from what was read: the owner sends it on to the machine input is on.
                clean = protocol.clipboard_msg(read["text"], read["image"])
                with self._lock:
                    actions = self._owner.clipboard_arrived(peer, began_at if began_at is not None else self._clock(), clean)
                    self._queue(actions)
                self._report(actions)
        elif kind == protocol.MSG_ARRANGEMENT:
            read = protocol.read_arrangement_v6(message, time.time())
            if read is not None and self.arrangement_callback is not None:
                self._call(self.arrangement_callback, peer, read)
        elif kind == protocol.MSG_SETTINGS:
            if isinstance(data, dict) and self.settings_callback is not None and "settings" in self.links.caps(peer):
                self._call(self.settings_callback, peer, data)
        elif kind == protocol.MSG_PAIRED:
            read = protocol.read_paired(message)
            if read is not None and self.paired_callback is not None:
                self._call(self.paired_callback, peer, read)

    def on_status(self, key, up: bool, detail: str) -> None:
        """A link's status text, for the peer the UI shows."""
        self._statuses[key] = detail
        self._report_status()

    def _report_status(self) -> None:
        if self._status_callback is not None:
            self._call(self._status_callback, self.connected, self.status)

    # -- sending ------------------------------------------------------------

    def send_to(self, peer: bytes, message: dict) -> bool:
        """A message of this machine's own (`arrangement`, `settings`, `paired`) on the link it
        opened to `peer`; False when there is none, and the app then tries the peer's own link."""
        return self.links.up(peer) and self.links.post(peer, message)

    def send_arrangement(self, peer: bytes, edge: str, set_at: int) -> bool:
        """Tells a peer which of this PC's edges faces it, when the change was made here."""
        settings = {"machine_id": protocol.id_text(self._own_id), "peers": self._entries, "zones": self._zone_list}
        peer_text = protocol.id_text(peer)
        way_back = ways.has_way(settings, peer_text)
        way_back_by = protocol.read_id(ways.way_back_by(settings, peer_text))
        return self.send_to(peer, protocol.arrangement_v6(edge, set_at, self._own_id,
                                                          way_back=way_back, way_back_by=way_back_by))

    def peers_up(self) -> list:
        return [peer for peer in self._peers if self.links.up(peer)]

    def _queue(self, actions) -> None:
        """Hands what the owner answered to the outbound worker, in order. Called with the lock still
        held, in the same step as the decision, so two threads' decisions cannot reach the worker the
        other way round (a release behind the press it releases would leave the key down)."""
        queued_actions = []
        for action in actions:
            if isinstance(action, owner_module.Send) and action.message.get("type") == protocol.MSG_TEXT:
                modifiers = [
                    data for data in action.message.pop("_pressed_keys", [])
                    if data.get("key") in MODIFIER_KEYS
                ]
                queued_actions.extend(
                    owner_module.Send(action.peer, {"type": protocol.MSG_KEYUP, "data": modifier,
                                                     "_preserve_key_name": True})
                    for modifier in modifiers
                )
                queued_actions.append(action)
                queued_actions.extend(
                    owner_module.Send(action.peer, {"type": protocol.MSG_KEYDOWN, "data": modifier})
                    for modifier in modifiers
                )
                continue
            queued_actions.append(action)

        actions = queued_actions
        for index, action in enumerate(actions):
            if isinstance(action, (owner_module.Send, owner_module.SendClipboard, owner_module.SetClipboard, owner_module.Drop)):
                try:
                    self._outbound.put_nowait(action)
                except queue.Full:
                    LOGGER.error("the outbound queue filled up; input returns to this PC")
                    self._overflowed = True
                    # What is lost with the rest can be sent again, but a link the owner has
                    # already counted as closed must still close, or that machine stays owned.
                    for lost in actions[index:]:
                        if isinstance(lost, owner_module.Drop):
                            self._forget_held(lost.peer)
                            self.links.close(lost.peer, owner_module.DROPPED)
                    return

    def _report(self, actions) -> None:
        """What the owner reported that is not a message: where the input is now, a move that did not
        happen. Never under the lock, since it calls the app."""
        if self._overflowed:
            self._overflowed = False
            self._lost()
        for action in actions:
            if isinstance(action, owner_module.Moved):
                self._moved(action)
            elif isinstance(action, owner_module.Unreachable):
                self._unreachable(action)

    def _outbound_worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                action = self._outbound.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._deliver(action)
            except Exception:
                # One bad message must not end the only thread that drains this queue: nothing
                # restarts it, and the hooks would keep swallowing input into a queue nobody reads.
                LOGGER.exception("outbound worker recovered")
                self._lost()
            finally:
                self._outbound.task_done()

    def _deliver(self, action) -> None:
        """Sends in the order the owner gave them, except that reading the clipboard (which can be
        slow) never holds up another peer's messages: what follows a clipboard read for the same
        peer waits for it, and everything else goes on. The clipboard's reads and writes go one at a
        time, in the order decided, on a thread of their own, so a read never overtakes a write
        decided before it."""
        if isinstance(action, owner_module.SetClipboard):
            self._on_clipboard(lambda message=action.message: self._set_clipboard(message))
            return
        peer = action.peer
        if isinstance(action, owner_module.Drop):
            # A hand-over given up before its answer: closing the link ends any ownership a late
            # accept began there, and it reconnects (WIRE.md section 5).
            self._forget_held(peer)
            self.links.close(peer, owner_module.DROPPED)
            return
        with self._clip_lock:
            waiting = self._clip_wait.get(peer)
            if waiting is not None:
                waiting.append(action)
                return
            if isinstance(action, owner_module.SendClipboard):
                waiting = self._clip_wait[peer] = collections.deque()
                try:
                    self._on_clipboard_locked(lambda peer=peer: self._clipboard_then_rest(peer, waiting))
                except RuntimeError:
                    # No thread at shutdown: nothing must wait behind a read that never starts.
                    del self._clip_wait[peer]
                    raise
                return
        self._send(peer, action.message)

    def _clipboard_then_rest(self, peer: bytes, waiting) -> None:
        """The clipboard for `peer`, then what `waiting` held behind it, in order. `waiting` belongs to
        the link it was made on: once that link is lost (`_forget_held`) none of it is sent, as the
        machine that reconnects will be taken afresh. A clipboard asked for again takes its turn
        among the clipboard's other work, behind any write decided before it."""
        try:
            self._send_clipboard(peer, waiting)
        except Exception:
            LOGGER.exception("The clipboard could not be sent")
        while True:
            with self._clip_lock:
                if self._clip_wait.get(peer) is not waiting:
                    return
                if not waiting:
                    del self._clip_wait[peer]
                    return
                action = waiting.popleft()
                if isinstance(action, owner_module.SendClipboard):
                    self._on_clipboard_locked(lambda peer=peer: self._clipboard_then_rest(peer, waiting))
                    return
            try:
                self._send(peer, action.message)
            except Exception:
                LOGGER.exception("A message held behind the clipboard could not be sent")

    def _on_clipboard(self, job) -> None:
        with self._clip_lock:
            self._on_clipboard_locked(job)

    def _on_clipboard_locked(self, job) -> None:
        """Under `_clip_lock`: `job` done after every clipboard job before it, on the one thread
        that does them, started when there is none."""
        self._clip_jobs.append(job)
        if self._clip_busy:
            return
        try:
            threading.Thread(target=self._clipboard_worker, name="Beamer-links-clipboard", daemon=True).start()
        except RuntimeError:
            self._clip_jobs.pop()
            raise
        self._clip_busy = True

    def _clipboard_worker(self) -> None:
        while True:
            with self._clip_lock:
                if not self._clip_jobs:
                    self._clip_busy = False
                    return
                job = self._clip_jobs.popleft()
            try:
                job()
            except Exception:
                LOGGER.exception("Clipboard work failed")

    def _forget_held(self, peer: bytes) -> None:
        """`peer`'s link is lost: what waited for its clipboard is for a link that is gone."""
        with self._clip_lock:
            self._clip_wait.pop(peer, None)

    def _send(self, peer: bytes, message: dict) -> None:
        kind = message["type"]
        if kind == protocol.MSG_FOCUS:
            data = message["data"]
            message = protocol.focus_v6(
                data["route"], data["target"], data.get("edge"), data.get("offset"),
                data.get("resistance_px"), data.get("reach"), bool(data.get("stay")),
            )
        elif kind in (protocol.MSG_KEYDOWN, protocol.MSG_KEYUP):
            preserve = message.pop("_preserve_key_name", False)
            message = self._key_message(peer, message, preserve=preserve)
            if message is None:
                return
        if kind in INPUT_TYPES:
            if not self.links.send_input(peer, message):
                LOGGER.warning("input could not be sent to %s", self._name(peer))
                self._send_failed(peer)
        else:
            self.links.post(peer, message)

    def _key_message(self, peer: bytes, message: dict, preserve: bool = False) -> Optional[dict]:
        """The key as `peer` is sent it: named by the key table for the peer's platform and the
        user's style, and dropped when the peer does not take media keys."""
        data = message["data"]
        physical = data["key"]
        entry = self._peers.get(peer) or {}
        if physical in MEDIA_KEYS and "media_keys" not in self.links.caps(peer):
            return None
        held = (peer, physical)
        if message["type"] == protocol.MSG_KEYUP and held in self._sent_names and preserve:
            name = self._sent_names[held]
        elif message["type"] == protocol.MSG_KEYUP and held in self._sent_names:
            # The release leaves under the name its press went under, whatever the style says now.
            name = self._sent_names.pop(held)
        elif message["type"] == protocol.MSG_KEYDOWN and held in self._sent_names:
            # So does each auto-repeat of it.
            name = self._sent_names[held]
        else:
            style = self._setting("modifier_style", "semantic")
            try:
                name = keytable.wire_name(physical, OWN_PLATFORM, entry.get("platform") or "", style)
            except ValueError:
                name = physical
            if message["type"] == protocol.MSG_KEYDOWN:
                self._sent_names[held] = name
        return {"type": message["type"], "data": dict(data, key=name)}

    def _send_failed(self, peer: bytes) -> None:
        self.links.close(peer)
        self._forget_held(peer)
        self._feed(lambda o: o.link_down(peer))

    def _feed(self, call) -> None:
        with self._lock:
            actions = call(self._owner)
            self._queue(actions)
        self._report(actions)

    # -- the clipboard ------------------------------------------------------

    def _note_clipboard(self) -> None:
        """Under the lock, before anything that may send the clipboard: whether it changed by this
        PC's own hand since this PC last sent or set it, which the owner needs to know (section 5)."""
        stamp = getattr(self._clipboard_module(), "change_stamp", lambda: None)()
        if stamp is not None and stamp != self._clipboard_stamp:
            self._clipboard_stamp = stamp
            self._owner.clipboard_changed()

    def _send_clipboard(self, peer: bytes, waiting) -> None:
        try:
            text, image = self._clipboard_module().get_contents()
        except Exception:
            LOGGER.exception("Failed to read the local clipboard for %s", self._name(peer))
            return
        with self._clip_lock:
            if self._clip_wait.get(peer) is not waiting:
                return
        caps = self.links.caps(peer)
        if text and ("clipboard" not in caps or len(text.encode("utf-8")) > protocol.CLIPBOARD_MAX_BYTES):
            text = None
        if image is not None and ("clipboard_image" not in caps or not protocol.png_fits(image)):
            image = None
        if not text and image is None:
            return
        self.links.post(peer, protocol.clipboard_msg(text or None, image))

    def _set_clipboard(self, message: dict) -> None:
        read = protocol.read_clipboard(message)
        if read is None or (read["text"] is None and read["image"] is None):
            return
        try:
            self._clipboard_module().set_contents(read["text"], read["image"])
        except Exception:
            LOGGER.exception("Failed to set the local clipboard")
            return
        self._clipboard_stamp = getattr(self._clipboard_module(), "change_stamp", lambda: None)()

    # -- what the owner reports ---------------------------------------------

    def _moved(self, moved: owner_module.Moved) -> None:
        if moved.to is not None:
            self._last_on = protocol.id_text(moved.to)
            if self._pin_point is None:
                self._pin_point = self._desktop_module().cursor_position()
            LOGGER.info("input is on %s%s", self._name(moved.to), _at_edge(moved.edge))
            self._report_redirecting(True)
            return
        self._pin_point = None
        self._ignore_gate.reset()
        # A fresh set of models, not a reset one: a model disarms itself at breakthrough so a burst
        # of deltas still in flight cannot cross twice, and nothing else re-arms it.
        with self._lock:
            self._arm_locked()
        LOGGER.info("input returned to this PC (%s)%s", moved.why, _at_edge(moved.edge))
        self._report_redirecting(False)
        if moved.edge in return_edge.EDGES and isinstance(moved.offset, (int, float)):
            self._land(moved.edge, moved.offset)
        elif moved.why in ("asked", "switch"):
            self._report_arrival(None)
        else:
            other = ""
            if isinstance(moved.other, bytes) and moved.other != self._own_id:
                other = peerlist.label_for(self._entries, peer_id=protocol.id_text(moved.other))
            reason = peerlist.refusal_text(moved.why, self._name(moved.left), other)
            if reason is None:
                reason = HOME_REASONS.get(moved.why, "{name} returned the input").format(name=self._name(moved.left))
            self._alert(reason)

    def _land(self, edge: str, offset: float) -> None:
        """The peer pushed the pointer back through its zone: land it on the edge of this PC it comes
        back to."""
        try:
            desktop = self._desktop_module()
            x, y = return_edge.arrival_position(self._cached_monitors(), edge, offset)
            desktop.set_cursor_position(x, y)
        except Exception:
            LOGGER.exception("Could not place the pointer at the %s edge on arrival", edge)
            return
        self._report_arrival(edge, x, y)

    def _report_arrival(self, edge, x=None, y=None) -> None:
        if self._arrival_callback is None:
            return
        try:
            if x is None:
                x, y = self._desktop_module().cursor_position()
            self._arrival_callback(edge, x, y)
        except Exception:
            LOGGER.exception("Arrival callback failed")

    def _report_redirecting(self, value: bool) -> None:
        if value == self._reported_redirecting:
            return
        self._reported_redirecting = value
        if self._redirect_callback is not None:
            self._call(self._redirect_callback, value)

    def _unreachable(self, action: owner_module.Unreachable) -> None:
        name = self._name(action.peer)
        if action.why == "unreachable" and not self.links.up(action.peer):
            if not self.wake(action.peer):
                self._alert(f"Cannot switch — {self._status_of(action.peer)}")
            return
        if action.why == "unreachable" and self._accepts.get(action.peer) is False:
            self._alert("Cannot switch — " + HOME_REASONS["not_allowed"].format(name=name))
            return
        other = ""
        if isinstance(action.other, bytes) and action.other != self._own_id:
            other = peerlist.label_for(self._entries, peer_id=protocol.id_text(action.other))
        reason = peerlist.refusal_text(action.why, name, other)
        if reason is None:
            reason = f"Cannot switch — {name} could not be reached"
        self._alert(reason)

    def _learn_hardware(self, peer: bytes) -> None:
        """A peer's hardware address, read from the ARP table while the entry is fresh, for the wake-up
        a later switch may need. Only when the peer did not say its own in its `welcome`."""
        entry = self._peers.get(peer) or {}
        if entry.get("hw") or not entry.get("host"):
            return
        try:
            address = self._mac_lookup(entry["host"])
        except Exception:
            LOGGER.exception("hardware address lookup failed")
            return
        if address and self.hw_learned is not None:
            LOGGER.info("learned the hardware address of %s from the ARP table", self._name(peer))
            self._call(self.hw_learned, peer, address)

    # -- waking -------------------------------------------------------------

    def wake(self, peer: Optional[bytes] = None) -> bool:
        """Sends a peer a wake-on-LAN packet and waits up to a minute for its link. Nothing is
        queued: the person switches again once it is up. False when there is no address to wake,
        the peer answered and refused (it is awake; waking it would hide why), or a wake is
        already in flight."""
        peer = peer or self.shortcut_target()
        entry = self._peers.get(peer) if peer is not None else None
        address = (entry or {}).get("hw") or ""
        if not address or self.links.up(peer) or self.links.refused(peer):
            return False
        with self._wake_lock:
            if peer in self._waking:
                return True
            self._waking.add(peer)
        threading.Thread(target=self._wake_worker, args=(peer, address, entry), name="Beamer-wake", daemon=True).start()
        return True

    def _wake_worker(self, peer, address, entry) -> None:
        name = self._name(peer)
        started = self._clock()
        try:
            try:
                self._wake_sender(address, entry.get("host") or None)
            except (OSError, ValueError) as exc:
                LOGGER.warning("wake-on-LAN packet not sent: %s", exc)
                self._alert("Could not send the wake-up packet")
                return
            LOGGER.info("sent wake-on-LAN to %s", name)
            self._alert(WAKING_STATUS.format(name=name))
            while not self._stop_event.wait(WAKE_POLL_SECONDS):
                if self.links.up(peer):
                    LOGGER.info("%s woke and connected", name)
                    self._alert(f"{name} is awake — switch again to send input")
                    return
                if self._clock() - started >= wol.WAKE_WINDOW_SECONDS:
                    break
            if not self._stop_event.is_set():
                LOGGER.warning("%s did not answer within %.0fs of the wake-on-LAN packet", name, wol.WAKE_WINDOW_SECONDS)
                self._alert(NOT_WOKEN_STATUS.format(name=name))
        finally:
            with self._wake_lock:
                self._waking.discard(peer)

    # -- plumbing -----------------------------------------------------------

    def _tick_worker(self) -> None:
        while not self._stop_event.wait(TICK_SECONDS):
            self.tick()

    def tick(self) -> None:
        """What happens as time passes: the owner's waits and left-out peers, the zones following who
        can be reached, and following a peer's beacon to a new address."""
        owner = self._owner
        if owner is None:
            return
        with self._lock:
            actions = owner.tick()
            self._queue(actions)
            if frozenset(owner.reach_for(None)) != self._armed_for:
                # A peer began to accept this PC's input, or one left out after a failed hand-over
                # has had its three seconds: the zones that lead to it are live or dead now.
                self._arm_locked()
        self._report(actions)
        now = self._clock()
        if now - self._followed_at >= 1.0:
            self._followed_at = now
            self.links.follow(self.machines())

    def _arm_locked(self) -> None:
        """A fresh set of zone models, for the peers this PC can send to now (never the one its input
        is on: that peer's own zones lead on from there)."""
        owner = self._owner
        if owner is None:
            self._models = []
            return
        allowed = set(owner.reach_for(None))
        self._armed_for = frozenset(allowed)
        try:
            self._models = receiver.zone_models(
                self._zone_list, self._entries, allowed, self._resistance(), None,
            )
        except Exception:
            LOGGER.exception("Could not arm the zones")
            self._models = []

    def _status_of(self, peer) -> str:
        entry = self._peers.get(peer) or {}
        return self._statuses.get(self.links.key_of(entry)) or NOT_CONNECTED_STATUS

    def _name(self, peer) -> str:
        """What this PC's statuses and alerts call a machine: its label, so two of one name are told apart."""
        return peerlist.label_for(self._entries, peer_id=protocol.id_text(peer)) or "The other machine"

    def _alert(self, message: str) -> None:
        if self.on_alert is None:
            return
        try:
            self.on_alert("Beamer", message)
        except Exception:
            LOGGER.exception("Alert callback failed")

    @staticmethod
    def _call(callback, *args) -> None:
        try:
            callback(*args)
        except Exception:
            LOGGER.exception("A callback failed")

    def _setting(self, name, fallback):
        with self._config_lock:
            config = self._config
        if config is None:
            return fallback
        value = getattr(config, name, fallback)
        return fallback if value is None else value

    def _resistance(self) -> int:
        """How hard this PC's own way out pushes back: this PC's setting, and what every peer is
        told to use at its zones while this PC's input is on it."""
        return int(self._setting("crossing_resistance_px", return_edge.DEFAULT_RESISTANCE_PX))

    def _warp_to_pin(self) -> None:
        """Put the pointer back where it was when input left. Measured: SetCursorPos produces no raw
        input at all -- a deliberate 137-pixel warp in a quiet window produced no WM_INPUT -- so the
        pin cannot feed itself back as movement nobody made."""
        pin = self._pin_point
        if pin is None:
            return
        try:
            self._desktop_module().set_cursor_position(*pin)
        except Exception:
            LOGGER.exception("Could not hold the pointer while a peer has input")

    def _cached_monitors(self):
        now = self._clock()
        if self._monitors is None or now - self._monitors_at > MONITORS_MAX_AGE_SECONDS:
            self._monitors = self._desktop_module().monitors()
            self._monitors_at = now
        return self._monitors

    def _desktop_module(self):
        if self._desktop is not None:
            return self._desktop
        import platform_parts

        return platform_parts.desktop

    def _clipboard_module(self):
        if self._clipboard is not None:
            return self._clipboard
        import platform_parts

        return platform_parts.clipboard

    def _notify_pressure(self, edge, pressure, crossed, part=None) -> None:
        if self._pressure_callback is None:
            return
        try:
            self._pressure_callback(edge, pressure, crossed, part)
        except Exception:
            LOGGER.exception("Pressure callback failed")


class LinkSet:
    """One core.link.OutboundLink for each peer entry this PC dials, keyed by the entry's token and
    found by peer id once up. What
    `LinkSender` asks of its links, and what each tells it back."""

    REFUSALS = ("older", "newer", "wrong_id", "unauthenticated", "different")

    def __init__(self, sender: "LinkSender", is_local=is_this_machine, link_class=None) -> None:
        self._sender = sender
        self._is_local = is_local
        self._link_class = link_class or _outbound_link()
        self._links: dict = {}          # token: OutboundLink
        self._by_peer: dict = {}        # peer id: OutboundLink, while up
        self._lock = threading.RLock()
        self._started = False

    def start(self) -> None:
        with self._lock:
            self._started = True
            links = list(self._links.values())
        for link in links:
            link.start()

    def stop(self) -> None:
        with self._lock:
            self._started = False
            links = list(self._links.values())
        for link in links:
            link.stop()

    def key_of(self, entry: dict):
        return entry.get("token")

    def sync(self, entries: list) -> None:
        """Dials exactly the entries made by pairing whose address is not this PC's; an entry gone or
        turned local is stopped. The link itself ends and waits when `send` is off."""
        wanted = {
            entry["token"]: entry for entry in entries
            if protocol.linkable(entry) and not self._is_local(entry.get("host") or "")
        }
        with self._lock:
            gone = [token for token in self._links if token not in wanted]
            added = [token for token in wanted if token not in self._links]
            stale = [self._links.pop(token) for token in gone]
            downed = []
            for link in stale:
                # stop(wait=False) mutes the link's own down report, so the owner is told here.
                peer = protocol.read_id(link.peer_id)
                if peer is not None and self._by_peer.get(peer) is link:
                    del self._by_peer[peer]
                    downed.append(peer)
            fresh = []
            hooks = {"hardware": self._sender._hardware} if self._sender._hardware is not None else {}
            if hasattr(self._sender, "expects_clipboard"):
                hooks["large"] = self._sender.expects_clipboard
            for token in added:
                link = self._link_class(
                    token, self._sender._book, self._sender._identity,
                    state=self._state, up=self._up, message=self._message, **hooks,
                )
                self._links[token] = link
                fresh.append(link)
            started = self._started
        for link in stale:
            # Often the window's thread, which must not wait out a link that is in a 3 second connect.
            link.stop(wait=False)
        for peer in downed:
            self._sender.on_link(peer, False)
        if started:
            for link in fresh:
                link.start()

    def _link(self, peer):
        with self._lock:
            return self._by_peer.get(peer)

    def up(self, peer) -> bool:
        link = self._link(peer)
        return link is not None and link.live()

    live = up

    def refused(self, peer) -> bool:
        """The peer answered as a Beamer and refused this one: it is awake, and waking it would hide why."""
        link = self._link(peer)
        if link is None:
            entry = self._sender._peers.get(peer) or {}
            link = self._links.get(entry.get("token"))
        return link is not None and link.kind in self.REFUSALS

    def caps(self, peer) -> frozenset:
        link = self._link(peer)
        return link.caps if link is not None else frozenset()

    def post(self, peer, message) -> bool:
        link = self._link(peer)
        return link is not None and link.post(message)

    def send_input(self, peer, message) -> bool:
        link = self._link(peer)
        return link is not None and link.send_input(message)

    def close(self, peer, reason="The link went quiet") -> None:
        link = self._link(peer)
        if link is not None:
            link.drop(reason)

    def round_trip_ms(self, peer) -> Optional[int]:
        """The upper median of the last few trips, with the time the peer held each ACK taken off, or
        None when nothing fresh has been acknowledged."""
        link = self._link(peer)
        if link is None:
            return None
        trips = sorted(link.round_trips(ROUND_TRIP_MAX_AGE_SECONDS))
        return int(round(trips[len(trips) // 2] * 1000)) if trips else None

    def status(self, key) -> str:
        link = self._links.get(key)
        return link.status if link is not None else ""

    def kind(self, key) -> str:
        """What the link to the entry at `key` last reported (core.link's kinds), "" when none is dialled."""
        link = self._links.get(key)
        return link.kind if link is not None else ""

    def follow(self, machines: list) -> None:
        with self._lock:
            links = list(self._links.values())
        for link in links:
            link.follow(machines)

    def _state(self, link, up, text) -> None:
        if not up:
            peer = protocol.read_id(link.peer_id)
            with self._lock:
                held = peer is not None and self._by_peer.get(peer) is link
                if held:
                    del self._by_peer[peer]
            if held:
                self._sender.on_link(peer, False)
        self._sender.on_status(link.token, up, text)

    def _up(self, link, fields) -> None:
        peer = fields["id"]
        with self._lock:
            if self._links.get(link.token) is not link:
                # Its machine was removed while the handshake finished: nothing would take it out again.
                return
            self._by_peer[peer] = link
        self._sender.on_link(peer, True, fields["accepts"])
        with self._lock:
            removed = self._links.get(link.token) is not link
        if removed:
            # sync took the machine out after the check above and told the owner it was down before
            # this told it up: that True landed last, so the owner is told down again.
            self._sender.on_link(peer, False)
            return
        try:
            announcements = list(self._sender.announce(peer))
        except Exception:
            LOGGER.exception("announce failed")
            announcements = []
        for message in announcements:
            link.post(message)

    def _message(self, link, message) -> None:
        peer = protocol.read_id(link.peer_id)
        if peer is not None:
            self._sender.on_message(peer, message, getattr(message, "began_at", None))


def _outbound_link():
    from core import link

    return link.OutboundLink


def _default_links(sender):
    return LinkSet(sender, sender._is_local)
