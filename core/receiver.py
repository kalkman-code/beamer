"""The responder: the listener every machine runs for the links its peers open to it.
`LinkResponder` speaks wire version 6 (WIRE.md) for any number of peers, with one owner at a
time; an older Beamer is answered only far enough to be told to update."""

import collections
import copy
import functools
import hmac
import ipaddress
import logging
import socket
import sys
import threading
import time
from enum import Enum
from typing import Callable, Optional, Tuple

from . import return_edge as crossing
from . import peerlist
from . import protocol


LOGGER = logging.getLogger(__name__)
# The longest an idle link goes without an ACK, so the initiator's watchdog keeps
# seeing a heartbeat when nothing is being typed.
ACK_IDLE_SECONDS = 0.4
# How long an ACK waits after input arrives, batching a burst into one reply.
# A per-event ACK at ~100Hz mouse rates would roughly double the packet count;
# 15ms caps it near 66/s while keeping the round trip the Mac reports honest,
# which a 400ms timer did not -- every reading carried up to 400ms of waiting
# that was never network latency.
ACK_COALESCE_SECONDS = 0.015
LOCK_CHECK_SECONDS = 0.5
INJECTION_ERROR_STATUS_INTERVAL_SECONDS = 5.0
# Windows hands the port back only when whatever holds it lets go — WinNAT's
# dynamic reservations move on every reboot and can land on ours — so a bind
# that fails is waited out rather than ending the listener.
BIND_RETRY_SECONDS = 5.0
# Connections still inside the handshake. Each is bounded to HANDSHAKE_SECONDS as a whole,
# so a stranger holding these slots loses them within seconds, while the real peer's handshake
# takes milliseconds and always finds one free.
MAX_PENDING_CONNECTIONS = 4


class ServerState(Enum):
    STOPPED = "Stopped"
    WAITING = "Waiting"
    CONNECTED = "Connected"
    ERROR = "Error"


class InputScale:
    """This machine's own speed for the pointer and scroll arriving from the peer. The peer sends
    what its own acceleration made of the hand's movement, and this machine may accelerate it
    again, so how fast it feels here is a setting here. Fractions carry over between messages, so
    a slow movement at a low speed still moves. `reverse` turns the peer's scroll round."""

    MIN, MAX = 0.25, 4.0

    def __init__(self, pointer: float = 1.0, scroll: float = 1.0, reverse: bool = False) -> None:
        self.pointer = min(self.MAX, max(self.MIN, float(pointer)))
        self.scroll = min(self.MAX, max(self.MIN, float(scroll)))
        self.reverse = bool(reverse)
        self._carry = [0.0, 0.0]

    def move(self, dx: int, dy: int) -> Tuple[int, int]:
        if self.pointer == 1.0:
            return dx, dy
        x = dx * self.pointer + self._carry[0]
        y = dy * self.pointer + self._carry[1]
        whole_x, whole_y = int(x), int(y)
        self._carry = [x - whole_x, y - whole_y]
        return whole_x, whole_y

    def wheel(self, dy, dx):
        if self.scroll == 1.0 and not self.reverse:
            return dy, dx
        sign = -1.0 if self.reverse else 1.0
        return dy * self.scroll * sign, dx * self.scroll * sign


def _close_socket(sock: Optional[socket.socket]) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


# The handshake's bounds, from the moment a connection is accepted (section 2).
PREAMBLE_SECONDS = 1.0
HANDSHAKE_SECONDS = 5.0
# How long an answer that ends a connection waits for the peer's bytes, so the answer is not lost to
# a reset; and how many such drains run at once. A drain holds no handshake slot, so under a flood
# of older peers the ones past the cap are closed without one rather than each costing a thread.
DRAIN_SECONDS = 1.0
MAX_DRAINS = 8
# Threads for connections not yet authenticated, drains included. A connection past it is closed at
# once, without a thread or a slot: a flood of connections must not become a flood of threads.
MAX_HANDSHAKE_THREADS = 32
# A frame goes out in pieces of this, each bounded by the link's timeout, so a large clipboard to a
# slow but working peer keeps going while bytes move (section 3, liveness).
SEND_CHUNK = 64 * 1024
# No byte at all for this long ends a link (section 3, liveness).
LINK_READ_TIMEOUT_SECONDS = 2.5
# A send-home the owner does not obey within this ends its ownership by force, and after either the
# initiator is refused `sent_home` for SENT_HOME_SECONDS (section 4).
FORCED_END_SECONDS = 1.0
SENT_HOME_SECONDS = 5.0
# Frames waiting to go to one peer. A peer that stops reading is closed at this, rather than let a
# queue grow without end; it is well past anything a working link queues between two writes.
OUTBOX_LIMIT = 512

ADMITTED = "admitted"
WRONG_ID = "wrong_id"
GONE = "gone"
IN_USE_OFF = "in_use_off"

_DROPPED_TEXT = frozenset(chr(code) for code in list(range(0x00, 0x20)) + list(range(0x7F, 0xA0))) - {"\n", "\t"}
_TEXT_KEYS = {"\n": "enter", "\t": "tab"}


def learnable_host(address, port) -> bool:
    """Whether a link from `address`, whose `hello` gave `port`, teaches the peer's `host`: never
    for a peer that accepts no links, nor from a loopback or link-local address (section 2)."""
    if port == 0:
        return False
    try:
        ip = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_link_local)


def ack_due(last_ack_at: float, pending_since: Optional[float]) -> float:
    """When the next `ack` goes, timed from the last (section 9): input dealt with 15 ms or more after
    it at once, input sooner 15 ms after it, and with none a heartbeat 0.4 seconds after it."""
    if pending_since is None:
        return last_ack_at + ACK_IDLE_SECONDS
    return max(pending_since, last_ack_at + ACK_COALESCE_SECONDS)


def text_runs(text: str) -> list:
    """`text` as the injector types it (section 10): runs of characters, with each `\\n` and `\\t`
    its own run, and the other control characters dropped."""
    runs, current = [], []
    for char in text:
        if char in _TEXT_KEYS:
            if current:
                runs.append("".join(current))
                current = []
            runs.append(char)
        elif char not in _DROPPED_TEXT:
            current.append(char)
    if current:
        runs.append("".join(current))
    return runs


def corner_offset(corner: str, edge: str) -> float:
    """Where a corner zone lands the pointer on the edge it crosses: the end nearest the corner,
    0.0 for a top or left end and 1.0 for a bottom or right one (section 8)."""
    vertical, horizontal = corner.split("_")
    if edge in ("left", "right"):
        return 0.0 if vertical == "top" else 1.0
    return 0.0 if horizontal == "left" else 1.0


def zone_models(zones, peers, allowed, resistance_px: int, notch_span=None) -> list:
    """[(peer id, model)] for this machine's zones (section 8) that lead to a peer in `allowed`, at
    `resistance_px`, in the order they are checked: corners, then edges, then parts, then the notch.
    `peers` are the settings' peer entries, for each peer's `side`; `notch_span` is the Mac's
    `SpanEdge` span, and without it a notch zone is left out. Zones that are off, name no learnt
    peer, or do not fit their kind are left out."""
    sides = {}
    for peer in peers or ():
        ident = protocol.read_id(peer.get("id")) if isinstance(peer, dict) else None
        if ident is not None and peer.get("in_use", True) is True and peer.get("port") != 0:
            sides[ident] = peer.get("side")
    found = {"corner": [], "edge": [], "part": [], "notch": []}
    for zone in zones or ():
        if not isinstance(zone, dict) or zone.get("off") is True:
            continue
        peer = protocol.read_id(zone.get("peer"))
        if peer is None or peer not in allowed or peer not in sides:
            continue
        kind, side = zone.get("kind"), sides.get(peer)
        try:
            if kind == "edge" and side in crossing.EDGES:
                model = crossing.ReturnEdge(side, resistance_px)
            elif kind == "part" and side in crossing.EDGES and isinstance(zone.get("parts"), list):
                model = crossing.PartEdge(side, [part for part in zone["parts"] if isinstance(part, str)], resistance_px)
            elif kind == "corner" and isinstance(zone.get("corner"), str) and zone.get("edge") in zone["corner"].split("_"):
                model = crossing.CornerPush(zone["corner"], zone["edge"], resistance_px)
            elif kind == "notch" and notch_span is not None:
                model = crossing.SpanEdge("top", notch_span, resistance_px)
            else:
                continue
        except ValueError:
            continue
        found[kind].append((peer, model))
    return found["corner"] + found["edge"] + found["part"] + found["notch"]


@functools.lru_cache(maxsize=128)
def _key_id_of(token) -> bytes:
    return protocol.key_id(token)


def _entry_key_id(entry) -> Optional[bytes]:
    # Only for an entry made by pairing: a key id is an offline test of its token (section 2).
    return _key_id_of(entry["token"]) if protocol.linkable(entry) else None


class HandshakeSlots:
    """The connections still in their handshakes: at most `limit`, and one per address. Taking a
    slot for an address that holds one displaces that connection; taking one when all are held
    displaces the oldest. The caller closes what `take` returns. Not locked: the responder is."""

    def __init__(self, limit: int = MAX_PENDING_CONNECTIONS) -> None:
        self._limit = limit
        self._held = collections.OrderedDict()

    def take(self, host, connection):
        displaced = self._held.pop(host, None)
        if displaced is None and len(self._held) >= self._limit:
            _, displaced = self._held.popitem(last=False)
        self._held[host] = connection
        return displaced

    def free(self, host, connection) -> None:
        if self._held.get(host) is connection:
            del self._held[host]

    def __len__(self) -> int:
        return len(self._held)


class PeerBook:
    """The peers list as the link reads and writes it: WIRE.md section 1's settings, through the
    app's own `load` (the settings as held in memory, a dict with `peers` and `zones`) and `save`
    (given a changed copy, which it writes atomically and keeps). `lock` is held around every
    read-check-write here; the app holds it too around its own changes to the peers, so an id
    learnt here and a peer removed there cannot cross. Neither the responder nor this ever takes
    the responder's lock while holding this one."""

    def __init__(self, load: Callable[[], dict], save: Callable[[dict], None], lock=None) -> None:
        self._load = load
        self._save = save
        self.lock = lock if lock is not None else threading.RLock()

    def peers(self) -> list:
        with self.lock:
            return copy.deepcopy(list((self._load() or {}).get("peers") or []))

    def zones(self) -> list:
        """The zones as the settings hold them, a copy."""
        with self.lock:
            return copy.deepcopy(list((self._load() or {}).get("zones") or []))

    def store_paired(self, peer: str, ids: list) -> None:
        """Keep, with the entry whose id is `peer` (a `b64`), the ids it said it is paired with
        (`paired`, WIRE.md section 3); saved only when the list changed."""
        with self.lock:
            settings = copy.deepcopy(self._load() or {})
            entry = next((item for item in settings.get("peers") or [] if item.get("id") == peer), None)
            if entry is None or entry.get("paired_with") == ids:
                return
            entry["paired_with"] = list(ids)
            self._save(settings)

    def find(self, key_id: bytes) -> Optional[dict]:
        """The entry, a copy, whose token (one made by pairing) has `key_id`."""
        for entry in self.peers():
            found = _entry_key_id(entry)
            if found is not None and hmac.compare_digest(found, key_id):
                return entry
        return None

    def admit(self, key_id: bytes, own_id: bytes, peer_id: bytes, changes: dict):
        """Section 2's id checks for a link under `key_id` whose first frame named `peer_id`, and on
        success the entry's update from it, in one step under the lock: `(outcome, entry)`.
        WRONG_ID when `peer_id` is this machine's own or is not the entry's; GONE when no entry has
        the key id any longer; ADMITTED otherwise. `changes` and `linked` are applied and saved when
        they change anything."""
        with self.lock:
            settings = copy.deepcopy(self._load() or {})
            peers = settings.get("peers") or []
            entry = next((p for p in peers if _entry_key_id(p) is not None and hmac.compare_digest(_entry_key_id(p), key_id)), None)
            if entry is None:
                return GONE, None
            if peer_id == own_id or entry.get("id") != protocol.id_text(peer_id):
                return WRONG_ID, None
            if entry.get("in_use", True) is not True:
                return IN_USE_OFF, None
            before = copy.deepcopy(entry)
            entry.update(changes)
            entry["linked"] = True
            if entry != before:
                self._save(settings)
            return ADMITTED, copy.deepcopy(entry)


class _Link:
    """One authenticated link an initiator opened to this machine. Its reader thread deals with
    what arrives; its writer thread sends everything that goes out, in order (the `outbox`) and
    the `ack`s when they are due, so nothing that holds the responder's lock ever waits on a
    socket."""

    def __init__(self, sock, session, peer_id, key_id, host, name, caps, own_id, own_caps, allow_drive) -> None:
        self.sock = sock
        self.session = session
        self.peer_id = peer_id
        self.key_id = key_id
        self.host = host
        self.name = name
        self.caps = frozenset(caps)
        self.own_id = own_id
        self.own_caps = frozenset(own_caps)
        self.allow_drive = allow_drive
        self.route = 0              # the last route accepted on this link
        self.held = {}              # ("key" or "button", name): `us`, what the owner holds down here
        self.closed = threading.Event()
        self.cond = threading.Condition()
        self.outbox = collections.deque()
        self.dealt = 0              # the highest seq dealt with
        self.dealt_at = 0.0
        self.pending_since = None   # when the first input not yet acknowledged was dealt with
        self.acked = 0
        self.last_ack_at = time.monotonic()
        self.input_error_at = float("-inf")  # when an injection error last reached the status

    def post(self, message: dict) -> bool:
        with self.cond:
            if self.closed.is_set():
                return False
            if len(self.outbox) >= OUTBOX_LIMIT:
                LOGGER.warning("%s stopped reading; closing its link", self.name)
                self._close_locked()
                return False
            self.outbox.append(message)
            self.cond.notify()
            return True

    def dealt_with(self, seq: int) -> None:
        with self.cond:
            if seq <= self.dealt:
                return
            now = time.monotonic()
            self.dealt, self.dealt_at = seq, now
            if self.pending_since is None:
                self.pending_since = now
            self.cond.notify()

    def close(self) -> None:
        with self.cond:
            self._close_locked()

    def _close_locked(self) -> None:
        self.closed.set()
        self.cond.notify_all()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class LinkResponder:
    """Wire version 6's responder (WIRE.md sections 2 to 5, 9 and 10): accepts links from every
    paired peer on one port, keeps one link per peer id, and at most one owner, whose input alone
    is injected. Nothing in it knows which platform it runs on; everything platform-shaped is
    handed in.

    - `book`: a PeerBook over the app's settings.
    - `identity()`: this machine as its `welcome` says it, a dict of `id` (16 bytes), `name`,
      `platform`, `app`, `caps` and optionally `hw`; read at each handshake.
    - `injector`, `desktop`, `clipboard`, `unlock`: the platform modules.
      The injector also needs `inject_text(chars)` for the `text` capability. The clipboard needs
      `get_contents()`, `set_contents(text, image)` and `change_stamp()`, a value that changes
      whenever the clipboard does; without it, a machine let go never sends its clipboard.
      `unlock` is Windows' lock-screen module, or None.
    - `hardware(host)`: this machine's address on the interface that reaches `host`, asked for each
      link's own address so the `welcome` gives every peer the address its wake-on-LAN must go to;
      without it, the `identity()`'s `hw` is sent as it stands.
    - `away()`: whether this machine's own input is on another machine (owner.Owner.away), which
      refuses a take as `busy`. Called under the responder's lock, so a plain read that never
      blocks. The app sets `away` before it sends a take and then reads `driven`; if that is true,
      a take here won the race and the app's input goes home.
    - `zones()`: this machine's zones as settings.json keeps them; `notch_span`: the Mac's notch
      span for SpanEdge, or None.

    Callbacks, each on a link's thread, never with the responder's lock held, each by peer id:
    `owner_callback(peer or None)` when ownership begins or ends, always in the order it did; `arrival_callback(edge, x, y)`
    and `pressure_callback(edge, pressure, crossed, part)` for the crossing effects;
    `arrangement_callback(peer, {edge, set_at, by})`; `settings_callback(peer, data)`, only from a
    peer whose `caps` list `settings`; `paired_callback(peer, ids)`; `link_callback(peer, up)`; and
    `announce(peer)`, the messages to send once a link is up (section 3, announcements).
    `status_callback(state, detail)` takes ServerState.

    The app calls `peers_changed()` after every change to the peers (a removal closes that peer's
    links; `allow_drive` sends `accepts` and, turned off, ends that peer's ownership), `rearm()`
    after a change to the zones, and `send_home()` to send the owner home from here."""

    def __init__(
        self,
        book: PeerBook,
        identity: Callable[[], dict],
        *,
        status_callback: Callable[[ServerState, str], None],
        injector=None,
        desktop=None,
        clipboard=None,
        unlock=None,
        hardware: Optional[Callable[[str], Optional[str]]] = None,
        away: Optional[Callable[[], bool]] = None,
        zones: Optional[Callable[[], list]] = None,
        notch_span=None,
        owner_callback=None,
        arrival_callback=None,
        pressure_callback=None,
        arrangement_callback=None,
        settings_callback=None,
        paired_callback=None,
        link_callback=None,
        announce=None,
    ) -> None:
        self._book = book
        self._identity = identity
        self._status_callback = status_callback
        self._injector = injector
        self._desktop = desktop
        self._clipboard = clipboard
        self._unlock = unlock
        self._hardware = hardware
        self._away = away or (lambda: False)
        self._zones = zones or (lambda: [])
        self._notch_span = notch_span
        self._callbacks = {
            "owner": owner_callback, "arrival": arrival_callback, "pressure": pressure_callback,
            "arrangement": arrangement_callback, "settings": settings_callback, "paired": paired_callback,
            "link": link_callback,
        }
        self._announce = announce or (lambda peer: [])
        # This machine's speed for a peer's pointer and scroll, and whether
        # its edges are held (Pause crossing, a full-screen app), which keeps the zones from firing.
        self.input_scale: Optional[InputScale] = None
        self.edges_held: Callable[[], bool] = lambda: False
        # One lock for the links, the owner and everything the owner decides. Held while the owner's
        # input is injected, so an ownership ending on another thread cannot release keys between
        # the check and the press. Never held while calling the app, or across a socket write.
        self._lock = threading.RLock()
        self._slots = HandshakeSlots()
        self._drains = 0
        self._handshaking = 0               # threads for connections not yet authenticated
        self._links = {}                    # peer id: _Link
        self._owner: Optional[_Link] = None
        self._claiming: Optional[_Link] = None  # a take being decided, visible before `away` is read
        self._visit = 0                     # raised each time an ownership begins
        self._generation = 0                # raised on every change to the peers or the zones
        self._reach = frozenset()
        self._resistance = protocol.DEFAULT_RESISTANCE_PX
        self._armed = []                    # [(peer id, model)] while the zones are live
        self._zones_live = False
        self._sent_home_at = {}             # peer id: when it was sent home or ended by force
        self._forced = None                 # the forced end's timer after a send-home
        self._clipboard_mark = None         # the clipboard's change stamp when this owner's visit began
        # Ownership changes for owner_callback, queued under the lock and delivered in order, one
        # thread at a time, so the app never hears an end after the next owner's beginning.
        self._owner_events = collections.deque()
        self._notify_lock = threading.RLock()
        self._unlocking = threading.Event()
        self._arriving_visit = None         # the visit whose pointer is landing and unlock not yet asked
        self._lock_checked_at = float("-inf")
        self._locked_since = None
        self._sent_home_for_lock = False
        self._connections = set()
        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None
        self._listener: Optional[socket.socket] = None
        self._port = None
        self._requested_running = False

    # The listener

    @property
    def listening(self) -> bool:
        with self._lock:
            return self._requested_running and self._thread is not None and self._thread.is_alive()

    @property
    def listening_port(self) -> Optional[int]:
        with self._lock:
            return self._port if self._listener is not None else None

    @property
    def owner(self) -> Optional[bytes]:
        link = self._owner
        return link.peer_id if link is not None else None

    @property
    def driven(self) -> bool:
        """True from the moment a take is being decided, before `away()` is read, until the take is
        refused or the ownership it began ends. The app sends its own input elsewhere by setting
        `away` first and reading this after: whichever of the two moves second sees the other, so
        a machine is never driven and driving at once (section 4). A plain read, never the lock: the
        hook thread asks it, and the lock is held while the owner's input is injected, which on
        Windows waits for that same thread's hook."""
        return self._claiming is not None or self._owner is not None

    def links(self) -> set:
        with self._lock:
            return set(self._links)

    def caps_of(self, peer: bytes) -> Optional[frozenset]:
        """The capabilities the peer's link announced, or None when it has no link here."""
        with self._lock:
            link = self._links.get(peer)
        return link.caps if link is not None else None

    def start(self, port: int) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            stop_event = threading.Event()
            thread = threading.Thread(target=self._server_thread, args=(port, stop_event), name="Beamer-v6-server", daemon=True)
            self._stop_event, self._thread, self._port = stop_event, thread, port
            self._requested_running = True
        self._set_status(ServerState.WAITING, f"Starting on port {port}")
        thread.start()

    def stop(self) -> None:
        with self._lock:
            self._requested_running = False
            stop_event, listener, thread = self._stop_event, self._listener, self._thread
            connections = set(self._connections)
            after = self._end_locked(self._owner) if self._owner is not None else []
            # Closed under the lock, so no take queued on one can be accepted after this.
            for link in self._links.values():
                link.close()
        self._run(after)
        if stop_event is not None:
            stop_event.set()
        for connection in connections:
            _close_socket(connection)
        _close_socket(listener)
        if thread is not None and thread is not threading.current_thread() and thread.ident is not None:
            thread.join(timeout=2.0)
        with self._lock:
            if self._thread is thread:
                self._thread = self._stop_event = self._listener = None
        self._set_status(ServerState.STOPPED, "Listening stopped")

    def _server_thread(self, port: int, stop_event: threading.Event) -> None:
        try:
            listener = self._bind(port, stop_event)
            if listener is None:
                return
            listener.listen(8)
            # Closing a listener does not wake accept() everywhere, so stop() waits on this poll.
            listener.settimeout(0.1)
            with self._lock:
                self._listener = listener
                self._port = listener.getsockname()[1]
            LOGGER.info("Listening for version 6 links on 0.0.0.0:%s", self._port)
            self._report()
            while not stop_event.is_set():
                try:
                    connection, address = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if stop_event.is_set():
                        break
                    raise
                accepted = time.monotonic()
                with self._lock:
                    crowded = self._handshaking >= MAX_HANDSHAKE_THREADS
                    if not crowded:
                        self._handshaking += 1
                        displaced = self._slots.take(address[0], connection)
                        self._connections.add(connection)
                if crowded:
                    LOGGER.warning("Closed a connection from %s: %d handshakes already running", address[0], MAX_HANDSHAKE_THREADS)
                    _close_socket(connection)
                    continue
                if displaced is not None:
                    _close_socket(displaced)
                threading.Thread(
                    target=self._connection_thread, args=(connection, address[0], accepted), name="Beamer-v6-link", daemon=True
                ).start()
        except Exception as exc:
            if not stop_event.is_set():
                LOGGER.exception("The version 6 listener failed")
                self._set_status(ServerState.ERROR, f"Listener failed: {exc}")
        finally:
            with self._lock:
                listener = self._listener
                self._listener = None
            _close_socket(listener)

    def _bind(self, port: int, stop_event: threading.Event):
        reported = False
        while not stop_event.is_set():
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # SO_REUSEADDR only off Windows, where it would let a second
            # listener share the port.
            if sys.platform != "win32":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("0.0.0.0", port))
                return listener
            except OSError as exc:
                _close_socket(listener)
                if not reported:
                    reported = True
                    LOGGER.error("Cannot listen on port %s: %s", port, exc)
                    self._set_status(ServerState.ERROR, f"Another program is using port {port} — change the port on the Connection page")
                stop_event.wait(BIND_RETRY_SECONDS)
        return None

    def _connection_thread(self, connection: socket.socket, host: str, accepted: float) -> None:
        link = None
        try:
            link = self._handshake(connection, host, accepted)
        except Exception:
            LOGGER.exception("The handshake with %s failed", host)
        finally:
            with self._lock:
                self._slots.free(host, connection)
                self._handshaking -= 1
        try:
            if link is not None:
                self._serve(link)
        finally:
            with self._lock:
                self._connections.discard(connection)
            _close_socket(connection)

    # The handshake (section 2)

    def _handshake(self, connection: socket.socket, host: str, accepted: float) -> Optional[_Link]:
        """Steps 2 to 9 of the responder's handshake. The link, once its `welcome` has gone, or
        None once the connection has been answered or needs closing with nothing sent."""
        try:
            try:
                preamble = protocol.recv_link_preamble(connection, accepted + PREAMBLE_SECONDS)
            except protocol.VersionMismatch as exc:
                self._not_version_6(connection, host, exc.peer_version)
                return None
            _, key_id, _ = protocol.split_preamble(preamble)
            entry = self._book.find(key_id)
            if entry is None:
                LOGGER.info("%s named a pairing this machine does not have; closed", host)
                return None
            session = protocol.LinkSession(entry["token"], protocol.ROLE_RESPONDER)
            session.accept_preamble(preamble)
            deadline = accepted + HANDSHAKE_SECONDS
            # The last read left the preamble's deadline on the socket, perhaps nearly spent.
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            connection.sendall(session.preamble())
            try:
                hello = protocol.read_hello(protocol.recv_first_msg(connection, session, deadline))
            except (protocol.NotAnObject, protocol.InvalidHello):
                LOGGER.warning("%s sent a first frame that is not a valid hello", host)
                self._answer(connection, host, session.seal(protocol.invalid_hello_v6()))
                return None
            identity = self._identity()
            own_id = bytes(identity["id"])
            changes = {"name": hello["name"], "platform": hello["platform"], "port": hello["port"]}
            if hello["hw"] is not None:
                changes["hw"] = hello["hw"]
            if learnable_host(host, hello["port"]):
                changes["host"] = host
            try:
                outcome, entry = self._book.admit(key_id, own_id, hello["id"], changes)
            except Exception:
                LOGGER.exception("Could not save what the link from %s taught", host)
                return None
            if outcome == GONE:
                LOGGER.info("The pairing %s linked under was removed during its handshake; closed", host)
                return None
            if outcome == WRONG_ID:
                LOGGER.warning("%s holds a pairing of this machine's under another machine's id", host)
                self._answer(connection, host, session.seal(protocol.wrong_id_v6()))
                return None
            if outcome == IN_USE_OFF:
                self._answer(connection, host, session.seal(protocol.invalid_hello_v6()))
                return None
            allow_drive = entry.get("allow_drive") is True
            welcome = protocol.welcome_v6(
                own_id, identity["name"], identity["platform"], identity["app"], identity["caps"], allow_drive, self._hardware_towards(host, identity)
            )
            protocol.send_msg(connection, session, welcome)
        except (socket.timeout, protocol.ConnectionClosed, protocol.ProtocolError, OSError) as exc:
            LOGGER.info("Handshake with %s ended: %s", host, exc)
            return None
        return _Link(connection, session, hello["id"], key_id, host, hello["name"], hello["caps"], own_id, identity["caps"], allow_drive)

    def _hardware_towards(self, host, identity):
        """The `hw` for the welcome to `host`, as the initiator's hook does for its hello: the hook is
        optional and never fatal, and one that raises sends none, because the field is optional on
        the wire and the link matters more."""
        if self._hardware is None:
            return identity.get("hw")
        try:
            return self._hardware(host) or None
        except Exception:
            LOGGER.exception("Could not read this machine's hardware address towards %s", host)
            return None

    def _not_version_6(self, connection: socket.socket, host: str, version) -> None:
        """An older or newer Beamer: the version 5 legacy frame for one from before version 4, else
        the short reply, and the status names the peer at that address, if one is."""
        self._answer(connection, host, protocol.legacy_version_reply() if version is None else protocol.short_reply())
        who = peerlist.label_for(self._settings_now()[0], host=host) or host
        if version is not None and version > protocol.LINK_VERSION:
            detail = f"{who} runs a newer Beamer: update Beamer on this machine"
        else:
            detail = f"Update Beamer on {who}: it is older than this one"
        LOGGER.warning("%s speaks wire version %r; answered and closed", host, version)
        with self._lock:
            linked = bool(self._links)
        if not linked:
            self._set_status(ServerState.ERROR, detail)

    def _answer(self, connection: socket.socket, host: str, data: bytes) -> None:
        """Send `data`, free the handshake slot, shut the sending side, and read what the peer sends
        for up to DRAIN_SECONDS so the answer is not lost to a reset."""
        with self._lock:
            self._slots.free(host, connection)
            draining = self._drains < MAX_DRAINS
            if draining:
                self._drains += 1
        try:
            connection.settimeout(DRAIN_SECONDS)
            connection.sendall(data)
            connection.shutdown(socket.SHUT_WR)
            end = time.monotonic() + DRAIN_SECONDS
            while draining:
                left = end - time.monotonic()
                if left <= 0:
                    break
                connection.settimeout(left)
                if not connection.recv(65536):
                    break
        except OSError:
            pass
        finally:
            if draining:
                with self._lock:
                    self._drains -= 1

    # A link

    def _serve(self, link: _Link) -> None:
        link.sock.settimeout(LINK_READ_TIMEOUT_SECONDS)
        try:
            link.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        with self._lock:
            earlier = self._links.get(link.peer_id)
            self._links[link.peer_id] = link
            # Step 10: an earlier link from this peer is closed, its ownership ended first, and
            # closed under the lock, so no take still queued on it can be accepted after this.
            after = self._end_locked(earlier) if earlier is not None else []
            if earlier is not None:
                earlier.close()
        writer = threading.Thread(target=self._writer, args=(link,), name="Beamer-v6-writer", daemon=True)
        try:
            self._run(after)
            writer.start()
            # A peer removed, or its allow_drive changed, between the handshake's read of the book
            # and this link joining the others was missed by the app's peers_changed(): look again.
            self.peers_changed()
            self._callback("link", link.peer_id, True)
            self._report()
            try:
                for message in list(self._announce(link.peer_id)):
                    link.post(message)
            except Exception:
                LOGGER.exception("Announcements for %s failed", link.name)
            while not link.closed.is_set():
                try:
                    plain = link.session.open_raw(protocol.recv_frame(link.sock))
                    if len(plain) > protocol.SMALL_MESSAGE_BYTES and self._owner is not link:
                        # Only the owner's clipboard is this large; opening it was enough to keep
                        # the counter in step, and parsing it is the peer's to make this machine pay for.
                        LOGGER.info("Dropped a %d byte frame from %s, which does not own this machine", len(plain), link.name)
                        continue
                    message = protocol.parse_message(plain)
                except socket.timeout:
                    LOGGER.warning("%s went quiet, or took too long over one message; its link is over", link.name)
                    break
                except (protocol.ConnectionClosed, protocol.ProtocolError, OSError) as exc:
                    if not link.closed.is_set():
                        LOGGER.info("The link from %s ended: %s", link.name, exc)
                    break
                self._dispatch(link, message)
        finally:
            with self._lock:
                current = self._links.get(link.peer_id) is link
                if current:
                    del self._links[link.peer_id]
                after = self._end_locked(link)
            link.close()
            self._run(after)
            if writer.ident is not None:
                writer.join(timeout=1.0)
            if current:
                self._callback("link", link.peer_id, False)
                self._report()

    def _writer(self, link: _Link) -> None:
        """Everything this link sends, in order, and its `ack`s when they are due (section 9)."""
        while True:
            is_ack = False
            with link.cond:
                while True:
                    if link.closed.is_set():
                        return
                    if link.outbox:
                        message = link.outbox.popleft()
                        break
                    now = time.monotonic()
                    due = ack_due(link.last_ack_at, link.pending_since)
                    if now >= due:
                        seq = link.dealt
                        held_us = int((now - link.dealt_at) * 1_000_000) if seq > link.acked else 0
                        link.acked, link.last_ack_at, link.pending_since = seq, now, None
                        # Only the owner hears of the lock screen: to anyone else it would say who
                        # has just arrived to drive this machine.
                        locked = self._unlocking.is_set() and self._owner is link
                        message = protocol.ack_v6(seq, held_us, locked=locked)
                        is_ack = True
                        break
                    link.cond.wait(due - now)
            try:
                # Only this thread sends once the link is up, so sealing and sending need not be
                # one step under the session's lock to keep the counters in order.
                frame = memoryview(link.session.seal(message))
                sent = 0
                while sent < len(frame):
                    sent += link.sock.send(frame[sent:sent + SEND_CHUNK])
            except (OSError, protocol.ProtocolError, ValueError) as exc:
                LOGGER.info("Sending to %s failed: %s", link.name, exc)
                link.close()
                return
            if is_ack:
                self._home_if_locked(link)

    def _dispatch(self, link: _Link, message) -> None:
        kind = message.get("type") if isinstance(message, dict) else None
        try:
            if kind in protocol.INPUT_TYPES:
                self._on_input(link, message)
            elif kind == protocol.MSG_FOCUS:
                self._on_focus(link, message)
            elif kind == protocol.MSG_CLIPBOARD:
                self._on_clipboard(link, message)
            elif kind == protocol.MSG_ARRANGEMENT:
                read = protocol.read_arrangement_v6(message, time.time())
                if read is not None:
                    self._callback("arrangement", link.peer_id, read)
            elif kind == protocol.MSG_SETTINGS:
                parsed = protocol.read_message(message)
                if parsed is not None and "settings" in link.caps:
                    self._callback("settings", link.peer_id, parsed[1])
            elif kind == protocol.MSG_PAIRED:
                ids = protocol.read_paired(message)
                if ids is not None:
                    self._callback("paired", link.peer_id, ids)
        except Exception:
            LOGGER.exception("A %r from %s could not be dealt with", kind, link.name)

    # Input (sections 3, 4 and 10)

    def _on_input(self, link: _Link, message) -> None:
        read = protocol.read_input(message)
        if read is None:
            return
        after = []
        try:
            with self._lock:
                if self._owner is not link or not self._takes(link, read):
                    pass
                elif self._unlocking.is_set():
                    # Input that arrives while the lock screen clears is dropped, but a release of
                    # something injected before it still goes, or that key stays down after.
                    if self._releases_held(link, read):
                        self._inject_locked(link, read)
                elif not (read["type"] == protocol.MSG_MOUSEMOVE and self._zones_locked(link, read, after)):
                    self._inject_locked(link, read)
        except Exception:
            LOGGER.exception("Input from %s could not be injected", link.name)
            self._input_error_status(link)
        finally:
            link.dealt_with(read["seq"])
        self._run(after)

    def _input_error_status(self, link: _Link) -> None:
        """Says in the status that this peer's input is not being injected, as ReceiverServer did:
        at most once per INJECTION_ERROR_STATUS_INTERVAL_SECONDS a link, since a broken injector
        fails on every event."""
        now = time.monotonic()
        if now - link.input_error_at < INJECTION_ERROR_STATUS_INTERVAL_SECONDS:
            return
        link.input_error_at = now
        who = peerlist.label_for(self._settings_now()[0], peer_id=protocol.id_text(link.peer_id)) or link.name
        self._set_status(ServerState.CONNECTED, f"Input error from {who}; see log")

    @staticmethod
    def _releases_held(link: _Link, read: dict) -> bool:
        if read["type"] == protocol.MSG_KEYUP:
            return ("key", read["key"]) in link.held
        if read["type"] == protocol.MSG_MOUSEUP:
            return ("button", read["button"]) in link.held
        return False

    def _takes(self, link: _Link, read: dict) -> bool:
        """Whether this machine acts on this input at all: a message its capabilities do not cover
        is dropped, and still acknowledged (section 2)."""
        if read["type"] == protocol.MSG_TEXT:
            return read["text"] is not None and "text" in link.own_caps and hasattr(self._injector_module(), "inject_text")
        if read["type"] == protocol.MSG_GESTURE:
            return "gestures" in link.own_caps
        return True

    def _inject_locked(self, link: _Link, read: dict) -> None:
        injector = self._injector_module()
        kind = read["type"]
        if kind in (protocol.MSG_KEYDOWN, protocol.MSG_KEYUP):
            down = kind == protocol.MSG_KEYDOWN
            if down:
                link.held[("key", read["key"])] = read["us"]
            else:
                link.held.pop(("key", read["key"]), None)
            injector.inject_key(read["key"], down=down, us=read["us"])
        elif kind == protocol.MSG_MOUSEMOVE:
            dx, dy = read["dx"], read["dy"]
            if self.input_scale is not None and (dx or dy):
                dx, dy = self.input_scale.move(dx, dy)
                if not (dx or dy):
                    return
            injector.inject_mouse_move(dx, dy)
        elif kind in (protocol.MSG_MOUSEDOWN, protocol.MSG_MOUSEUP):
            down = kind == protocol.MSG_MOUSEDOWN
            if down:
                link.held[("button", read["button"])] = None
            else:
                link.held.pop(("button", read["button"]), None)
            injector.inject_mouse_button(read["button"], down=down)
        elif kind == protocol.MSG_SCROLL:
            dy, dx = read["dy"], read["dx"]
            if self.input_scale is not None:
                dy, dx = self.input_scale.wheel(dy, dx)
            injector.inject_scroll(dy, dx, read["mode"])
        elif kind == protocol.MSG_GESTURE:
            injector.inject_gesture(read["name"])
        elif kind == protocol.MSG_TEXT:
            for run in text_runs(read["text"]):
                if run in _TEXT_KEYS:
                    injector.inject_key(_TEXT_KEYS[run], down=True)
                    injector.inject_key(_TEXT_KEYS[run], down=False)
                else:
                    injector.inject_text(run)

    def _release_locked(self, link: _Link) -> None:
        """Let go of every key and button injected for this owner (section 4, step 1), then the
        injector's own record of what it holds, as the backstop."""
        injector = self._injector_module()
        for (kind, name), us in reversed(list(link.held.items())):
            try:
                if kind == "key":
                    injector.inject_key(name, down=False, us=us)
                else:
                    injector.inject_mouse_button(name, down=False)
            except Exception:
                LOGGER.exception("Could not release %r for %s", name, link.name)
        link.held.clear()
        release_all = getattr(injector, "release_all", None)
        if release_all is not None:
            try:
                release_all()
            except Exception:
                LOGGER.exception("Could not release the keys %s was holding", link.name)

    # The zones (sections 5 and 8)

    def _arm_locked(self, peers, zones) -> None:
        owner = self._owner
        if owner is None:
            self._armed, self._zones_live = [], False
            return
        try:
            self._armed = zone_models(zones, peers, {owner.peer_id} | self._reach, self._resistance, self._notch_span)
        except Exception:
            LOGGER.exception("Could not arm the zones for %s", owner.name)
            self._armed = []
        self._zones_live = True

    def _disarm_locked(self) -> None:
        self._armed, self._zones_live = [], False

    def _zones_locked(self, link: _Link, read: dict, after: list) -> bool:
        """Feeds one mouse move to the armed zones, first that holds the pointer winning. True when
        the move was used at an edge (held there, or crossed and the owner sent a `switch`)."""
        if not self._armed or self.edges_held():
            return False
        desktop = self._desktop_module()
        monitors, pointer = desktop.monitors(), desktop.cursor_position()
        for peer, model in self._armed:
            outcome = model.feed(monitors, pointer, float(read["dx"]), float(read["dy"]))
            if outcome.action == crossing.PASS:
                continue
            part = None
            if isinstance(model, crossing.PartEdge):
                part = crossing.part_of(crossing.display_fraction(monitors, model.edge, pointer))
            elif isinstance(model, crossing.CornerPush):
                part = model.corner
            if outcome.action == crossing.HOLD:
                desktop.set_cursor_position(*outcome.position)
                after.append(lambda: self._callback("pressure", model.edge, outcome.pressure, False, part))
                return True
            offset = corner_offset(model.corner, model.edge) if isinstance(model, crossing.CornerPush) else outcome.offset
            self._disarm_locked()
            LOGGER.info("The pointer pushed through the zone at this machine's %s edge; asking %s to move its input on",
                        model.edge, link.name)
            link.post(protocol.switch_v6(link.route, peer, outcome.edge, offset))
            after.append(lambda: self._callback("pressure", model.edge, 1.0, True, part))
            return True
        return False

    def rearm(self) -> None:
        """Rebuilds the zones from the settings as they are now, while an owner drives and they are
        live, so a change on the Crossing page applies at once. Never revives zones spent by a
        `switch` or a send-home."""
        with self._lock:
            self._generation += 1
        self._rearm_fresh()

    def _rearm_fresh(self) -> None:
        # The settings are read without the lock, so a change landing meanwhile is caught by the
        # generation and read again, rather than armed over by what was read before it.
        for _ in range(5):
            with self._lock:
                generation = self._generation
            peers, zones = self._settings_now()
            with self._lock:
                if generation != self._generation:
                    continue
                if self._owner is not None and self._zones_live:
                    self._arm_locked(peers, zones)
                return

    def _settings_now(self):
        """The peers and zones as the app holds them now, or none of either when it cannot say."""
        try:
            peers = self._book.peers()
        except Exception:
            LOGGER.exception("Could not read the peers")
            peers = []
        try:
            zones = list(self._zones() or [])
        except Exception:
            LOGGER.exception("Could not read the zones")
            zones = []
        return peers, zones

    # Ownership (section 4)

    def _on_focus(self, link: _Link, message) -> None:
        focus = protocol.read_focus(message, link.own_id)
        if focus is None:
            return
        after = []
        with self._lock:
            generation = self._generation
        peers, zones = self._settings_now() if focus["here"] else ((), ())
        with self._lock:
            if link.closed.is_set() or focus["route"] <= link.route:
                return
            if not focus["here"]:
                if self._owner is link:
                    link.route = focus["route"]
                    after = self._end_locked(link, let_go=True)
            else:
                why = self._refusal_locked(link, focus)
                if why is not None:
                    reason, other = why
                    link.post(protocol.refuse_msg(focus["route"], reason, other=other))
                    return
                link.route = focus["route"]
                # The answer goes before the pointer is placed or anything plays.
                link.post(protocol.accept_msg(focus["route"]))
                after = self._own_locked(link, focus, peers, zones)
            stale = focus["here"] and generation != self._generation
        self._run(after)
        if stale:
            self._rearm_fresh()

    def _refusal_locked(self, link: _Link, focus: dict):
        """Section 4's decision order, after the stale route (steps 2 to 7). The take is made
        visible (`driven`) before `away()` is read, and stays so until it is refused or becomes
        the ownership; `away()` is called under the lock, so it must be a plain read."""
        if focus["malformed"]:
            return "malformed", None
        if not link.allow_drive:
            return "not_allowed", None
        self._claiming = link
        try:
            away = self._away()
            busy = bool(away)
        except Exception:
            LOGGER.exception("Could not tell whether this machine's input is elsewhere")
            busy = False
            away = None
        why = None
        other = None
        sent_home_at = self._sent_home_at.get(link.peer_id)
        if busy:
            why = "busy"
            if isinstance(away, bytes) and len(away) == protocol.MACHINE_ID_SIZE:
                other = away
        elif sent_home_at is not None and time.monotonic() - sent_home_at < SENT_HOME_SECONDS:
            why = "sent_home"
        elif self._owner is not None and self._owner is not link:
            why = "owned"
            other = self._owner.peer_id
        elif focus["stay"] and self._owner is not link:
            why = "malformed"
        if why is not None:
            self._claiming = None
        return (why, other) if why is not None else None

    def _own_locked(self, link: _Link, focus: dict, peers, zones) -> list:
        began = self._owner is not link
        # The owner is set before the claim is dropped, so `driven` never reads false between.
        self._owner = link
        self._claiming = None
        self._reach = frozenset(peer for peer in focus["reach"] if peer not in (link.peer_id, link.own_id))
        self._resistance = focus["resistance_px"]
        self._arm_locked(peers, zones)
        after = []
        if began:
            self._visit += 1
            self._clipboard_mark = self._clipboard_stamp()
            self._sent_home_for_lock = False
            self._locked_since = None
            self._owner_events.append(link.peer_id)
        if not focus["stay"]:
            # Held until the unlock has been asked for, so the lock check cannot send this owner
            # home in the moment between its arrival and the provider's chance to clear the lock.
            visit = self._visit
            self._arriving_visit = visit
            after.append(lambda: self._land(link, focus, visit))
        return after

    def _end_locked(self, link: Optional[_Link], let_go: bool = False, why: Optional[str] = None) -> list:
        """Ends `link`'s ownership, if it holds it (section 4): its keys and buttons released before
        anything else is injected, its zones dropped; on a let-go the clipboard, when something
        other than a `clipboard` message changed it; otherwise, with `why` and the link up, a
        `refuse`. Returns what is left to do without the lock."""
        if link is None or self._owner is not link:
            return []
        self._owner = None
        self._reach = frozenset()
        self._arriving_visit = None
        self._disarm_locked()
        self._cancel_forced_locked()
        self._release_locked(link)
        self._owner_events.append(None)
        after = []
        if let_go:
            mark, visit = self._clipboard_mark, self._visit
            after.append(lambda: self._clipboard_back(link, mark, visit))
        elif why is not None and not link.closed.is_set():
            link.post(protocol.refuse_msg(link.route, why))
        return after

    def send_home(self) -> bool:
        """Sends the owner home from here (section 5): its shortcut, a menu, a direction switch
        turned off, or Windows locking. False when nobody drives this machine. If the owner has not
        let go within FORCED_END_SECONDS, its ownership ends by force."""
        return self._send_home(None)

    def _send_home(self, expected: Optional[_Link]) -> bool:
        with self._lock:
            link = self._owner
            if link is None or (expected is not None and link is not expected):
                return False
            self._disarm_locked()
            self._sent_home_at[link.peer_id] = time.monotonic()
            LOGGER.info("Sending %s's input home from here", link.name)
            link.post(protocol.switch_v6(link.route, link.peer_id))
            self._cancel_forced_locked()
            timer = threading.Timer(FORCED_END_SECONDS, lambda: self._forced_end(link, timer))
            timer.daemon = True
            self._forced = timer
            timer.start()
            return True

    def _forced_end(self, link: _Link, timer) -> None:
        with self._lock:
            if self._forced is not timer or self._owner is not link:
                return
            LOGGER.info("%s did not let go after being sent home; its ownership ends here", link.name)
            self._sent_home_at[link.peer_id] = time.monotonic()
            after = self._end_locked(link, why="sent_home")
        self._run(after)

    def _cancel_forced_locked(self) -> None:
        if self._forced is not None:
            self._forced.cancel()
            self._forced = None

    def peers_changed(self) -> None:
        """Brings the links in step with the peers list after the app changed it: a removed peer's
        links close at once, ending its ownership first; a peer whose `allow_drive` changed is sent
        `accepts`, and turned off, loses its ownership with `refuse {why: "not_allowed"}`."""
        with self._lock:
            self._generation += 1
        peers, _ = self._settings_now()
        by_key_id = {}
        for peer in peers:
            found = _entry_key_id(peer)
            if found is not None:
                by_key_id[found] = peer
        after = []
        with self._lock:
            for link in list(self._links.values()):
                peer = by_key_id.get(link.key_id)
                if peer is None or peer.get("id") != protocol.id_text(link.peer_id):
                    after += self._end_locked(link)
                    LOGGER.info("%s was removed; its link is closed", link.name)
                    link.close()
                    continue
                if peer.get("in_use", True) is not True:
                    after += self._end_locked(link)
                    link.close()
                    LOGGER.info("%s is not in use; its link is closed", link.name)
                    continue
                allow_drive = peer.get("allow_drive") is True
                if allow_drive != link.allow_drive:
                    link.allow_drive = allow_drive
                    if not allow_drive:
                        after += self._end_locked(link, why="not_allowed")
                    link.post(protocol.accepts_msg(allow_drive))
        self._run(after)
        self._rearm_fresh()

    def send(self, peer: bytes, message: dict) -> bool:
        """Sends `message` on the link `peer` opened to this machine: False when there is none."""
        with self._lock:
            link = self._links.get(peer)
        return link is not None and link.post(message)

    # The clipboard (section 5)

    def _on_clipboard(self, link: _Link, message) -> None:
        read = protocol.read_clipboard(message)
        if read is None:
            return
        text = read["text"] if read["text"] and "clipboard" in link.own_caps else None
        image = read["image"] if "clipboard_image" in link.own_caps else None
        if text is None and image is None:
            return
        # Checked and set under one hold of the lock, so an ownership ending meanwhile cannot let
        # a peer that no longer owns this machine set its clipboard.
        with self._lock:
            if self._owner is not link:
                return
            try:
                if not self._clipboard_module().set_contents(text, image):
                    LOGGER.warning("Could not set the clipboard %s sent", link.name)
            except Exception:
                LOGGER.exception("Could not set the clipboard %s sent", link.name)
            self._clipboard_mark = self._clipboard_stamp()

    def _clipboard_stamp(self):
        stamp = getattr(self._clipboard_module(), "change_stamp", None)
        if stamp is None:
            return None
        try:
            return stamp()
        except Exception:
            LOGGER.exception("Could not read the clipboard's change stamp")
            return None

    def _clipboard_back(self, link: _Link, mark, visit: int) -> None:
        """A let-go owner gets this machine's clipboard only when something other than its own
        `clipboard` messages changed it during the visit, so an idle machine gives nothing away;
        and never once another ownership has begun or the clipboard changed while it was read, so
        what it gets is never the next owner's."""
        stamp = self._clipboard_stamp()
        if stamp is None or stamp == mark:
            return
        try:
            text, image = self._clipboard_module().get_contents()
        except Exception:
            LOGGER.exception("Could not read the clipboard for %s", link.name)
            return
        if "clipboard" not in link.caps or not text or len(text.encode("utf-8", "replace")) > protocol.CLIPBOARD_MAX_BYTES:
            text = None
        if "clipboard_image" not in link.caps or image is None or not protocol.png_fits(image):
            image = None
        if text is None and image is None:
            return
        with self._lock:
            if self._visit == visit and self._owner is None and self._clipboard_stamp() == stamp:
                link.post(protocol.clipboard_msg(text, image))

    # Landing and the lock screen

    def _land(self, link: _Link, focus: dict, visit: int) -> None:
        """Places the pointer where a take by a zone says, or says where it is for one by the
        shortcut, then clears Windows' lock screen out of the way; nothing, once that visit is
        over."""
        try:
            with self._lock:
                current = self._visit == visit and self._owner is link
            if not current:
                return
            desktop = self._desktop_module()
            if focus["edge"] is not None:
                x, y = crossing.arrival_position(desktop.monitors(), focus["edge"], focus["offset"])
                desktop.set_cursor_position(x, y)
                LOGGER.info("%s's pointer arrived at this machine's %s edge", link.name, focus["edge"])
                self._callback("arrival", focus["edge"], x, y)
            else:
                x, y = desktop.cursor_position()
                self._callback("arrival", None, x, y)
        except Exception:
            LOGGER.exception("Could not land %s's pointer", link.name)
        try:
            self._begin_unlock(link)
        finally:
            with self._lock:
                if self._arriving_visit == visit:
                    self._arriving_visit = None

    def _begin_unlock(self, link: _Link) -> None:
        unlock = self._unlock
        if unlock is None or self._unlocking.is_set():
            return
        try:
            if unlock.is_locked() is not True:
                return
        except Exception:
            LOGGER.exception("Could not tell whether this machine is locked")
            return
        self._unlocking.set()
        LOGGER.info("%s's input arrived at a locked machine; asking the provider to unlock", link.name)
        self._set_status(ServerState.CONNECTED, "Unlocking Windows…")
        threading.Thread(target=self._unlock_worker, args=(unlock, link), name="Beamer-v6-unlock", daemon=True).start()

    def _unlock_worker(self, unlock, link: _Link) -> None:
        try:
            unlocked = unlock.ensure_unlocked()
        except Exception:
            LOGGER.exception("The unlock failed")
            unlocked = False
        finally:
            self._unlocking.clear()
        # An unlock outlives an owner whose link ended during it: that end has reported, and a
        # failure must not say CONNECTED over it.
        if unlocked or self._owner is not link:
            self._report()
        else:
            self._set_status(ServerState.CONNECTED, "Windows is locked — unlock it at the PC")

    def _home_if_locked(self, link: _Link) -> None:
        """An owner left driving a machine on its lock screen is sent home,
        once per visit, after two checks LOCK_CHECK_SECONDS apart, and never while an unlock is in
        flight or the owner is arriving."""
        if self._unlock is None or self._owner is not link:
            return
        if self._unlocking.is_set() or self._arriving_visit is not None:
            self._locked_since = None
            return
        now = time.monotonic()
        if now - self._lock_checked_at < LOCK_CHECK_SECONDS:
            return
        self._lock_checked_at = now
        try:
            locked = self._unlock.is_locked() is True
        except Exception:
            locked = False
        if not locked:
            self._locked_since = None
            return
        if self._locked_since is None:
            self._locked_since = now
            return
        if not self._sent_home_for_lock:
            LOGGER.info("This machine is locked while %s drives it; sending its input home", link.name)
            self._sent_home_for_lock = self._send_home(link)

    # Plumbing

    def _injector_module(self):
        if self._injector is not None:
            return self._injector
        import input_injector
        return input_injector

    def _desktop_module(self):
        if self._desktop is not None:
            return self._desktop
        import desktop_win
        return desktop_win

    def _clipboard_module(self):
        if self._clipboard is not None:
            return self._clipboard
        import clipboard_win
        return clipboard_win

    def _callback(self, name: str, *args) -> None:
        callback = self._callbacks[name]
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            LOGGER.exception("The %s callback failed", name)

    def _run(self, steps) -> None:
        """What an ownership change leaves to do without the lock: the owner callbacks it queued,
        first and in order, then `steps`."""
        self._tell_owner()
        for step in steps:
            try:
                step()
            except Exception:
                LOGGER.exception("A step after an ownership change failed")
        self._tell_owner()

    def _tell_owner(self) -> None:
        # One thread at a time delivers, so the order queued under the lock is the order heard.
        with self._notify_lock:
            while True:
                with self._lock:
                    if not self._owner_events:
                        return
                    peer = self._owner_events.popleft()
                self._callback("owner", peer)

    def _report(self) -> None:
        with self._lock:
            links = list(self._links.values())
            port = self._port
        names = []
        if links:
            peers = self._settings_now()[0]
            names = sorted(peerlist.label_for(peers, peer_id=protocol.id_text(link.peer_id)) or link.name for link in links)
        if names:
            self._set_status(ServerState.CONNECTED, "Connected to " + ", ".join(names))
        else:
            self._set_status(ServerState.WAITING, f"Waiting on port {port}")

    def _set_status(self, state: ServerState, detail: str) -> None:
        try:
            self._status_callback(state, detail)
        except Exception:
            LOGGER.exception("Status callback failed")
