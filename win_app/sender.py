"""Sending this PC's own keyboard and mouse to the Mac: the mirror of
mac_app/bridge.py's link half, talking to the same receiver.py the Mac's
sender talks to, over a second connection this PC opens outwards.

Two links, not one bidirectional socket. The existing Mac-to-Windows link is
the one used every day, and its sequence, acknowledgement and watchdog
bookkeeping is all sender-shaped; threading a second ownership state through
it would put that link at risk for nothing. This one is opened by this PC, to
the Mac's own listener on the same port number, so it needs no inbound
firewall rule here.

Nothing about the crossing is configured on this side. The Mac names the way
home on its own link -- the Windows edge input comes back through -- and that
is the same physical border this PC pushes out across, so the edge, its
resistance and the Mac's address are all learned rather than typed.

Which way input is flowing is one state across both links: the outbound edge
is armed only while the Mac is not driving this PC, so a pointer the Mac is
moving cannot push itself back out through the border it just came in by.
"""

import collections
import ipaddress
import logging
import queue
import socket
import struct
import threading
import time
from typing import Callable, Optional

from core import ignored
from core import protocol
from core import return_edge
from core import wol

LOGGER = logging.getLogger(__name__)

ACK_TIMEOUT_SECONDS = 2.0
CONNECT_TIMEOUT_SECONDS = 1.0
AUTH_TIMEOUT_SECONDS = 2.0
SOCKET_IO_TIMEOUT_SECONDS = 0.5
PING_INTERVAL_SECONDS = 1.0
OUTBOUND_QUEUE_SIZE = 2048
MONITORS_MAX_AGE_SECONDS = 1.0

CONTROL_MESSAGE_TYPES = frozenset({protocol.MSG_FOCUS, protocol.MSG_CLIPBOARD})
_LOCAL_CLIPBOARD_SENTINEL_TYPE = "_local_clipboard"
_GATE_EXEMPT_TYPES = CONTROL_MESSAGE_TYPES | {_LOCAL_CLIPBOARD_SENTINEL_TYPE}
# A key-up owed to the Mac for a key that went down there, queued as input
# comes home. Local to the queue: _process_outbound rebuilds the message from
# its type and data, so the mark never reaches the wire.
_RELEASE_MARK = "_release"

ROUND_TRIP_SAMPLES = 8
ROUND_TRIP_MAX_AGE_SECONDS = 5.0
WAKE_POLL_SECONDS = 0.5
WAKING_STATUS = "Waking your Mac…"
NOT_WOKEN_STATUS = "Your Mac did not wake"

# The capture names this PC's Ctrl "cmd" and its Windows key "ctrl", the Semantic style, so Ctrl+C
# is Cmd+C on the Mac. Positional swaps them back.
POSITIONAL_SWAP = {"cmd": "ctrl", "cmd_r": "ctrl_r", "ctrl": "cmd", "ctrl_r": "cmd_r"}

OLD_RECEIVER_STATUS ="The Mac did not answer the handshake — it is probably running an older Beamer; update it"
AUTH_FAILED_STATUS = "The Mac could not be authenticated — check the shared token matches on both sides"


class HandshakeError(protocol.ProtocolError):
    def __init__(self, status):
        super().__init__(status)
        self.status = status


class FrameDecoder:
    """Reassembles and opens frames from the raw chunks the reader thread
    receives. One per connection, like the session it opens frames with."""

    def __init__(self, session):
        self.session = session
        self.buffer = bytearray()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        messages = []
        while len(self.buffer) >= protocol.HEADER_SIZE:
            length = struct.unpack_from(">I", self.buffer)[0]
            if length < 1 or length > protocol.MAX_FRAME_BYTES:
                raise protocol.ProtocolError(f"invalid message length: {length}")
            frame_length = protocol.HEADER_SIZE + length
            if len(self.buffer) < frame_length:
                break
            body = bytes(self.buffer[protocol.HEADER_SIZE:frame_length])
            del self.buffer[:frame_length]
            messages.append(self.session.open(body))
        return messages


def is_this_machine(host: str, local_addresses=None, address_towards=None) -> bool:
    """Whether `host` is this PC rather than the Mac. Worth a guard of its
    own: the Mac's address is learned from the peer address of its own link,
    and when the Mac connects through the macOS 27 SSH tunnel that address is
    127.0.0.1 -- this PC's. Connecting there would reach this PC's own
    receiver, authenticate with the shared token, and the receiver's preempt
    rule would close the real Mac's session. The daily link would drop and
    reconnect for as long as both were running.

    Two answers are consulted, because the first one fails silently: the
    hostname-based address list is empty on a PC whose name does not
    resolve, and an empty list would have said "not this machine" for this
    machine's own address. The route probe asks the stack which local
    address would reach `host`; for this PC's own address that is the
    address itself."""
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


def _default_socket_factory(address, timeout):
    return socket.create_connection(address, timeout=timeout)


class MacSender:
    """One link to the Mac, plus the decision -- taken on the hook thread --
    of whether each local input event belongs to this PC or to the Mac.

    `status_callback(connected, detail)` and `redirect_callback(redirecting)`
    are called from worker threads; a GUI marshals them. `desktop` and
    `clipboard` default to the real Win32 modules and are handed in by tests.
    """

    def __init__(
        self,
        status_callback: Callable[[bool, str], None] = None,
        redirect_callback: Callable[[bool], None] = None,
        pressure_callback: Callable[[str, float, bool, Optional[str]], None] = None,
        arrangement_callback: Callable[[str, int], None] = None,
        desktop=None,
        clipboard=None,
        socket_factory=_default_socket_factory,
        is_local=is_this_machine,
        clock=time.monotonic,
        wake_sender=wol.send_magic_packet,
        mac_lookup=wol.lookup_mac,
        arrival_callback: Callable[[str, int, int], None] = None,
    ) -> None:
        self._status_callback = status_callback
        self._redirect_callback = redirect_callback
        self._pressure_callback = pressure_callback
        self._arrangement_callback = arrangement_callback
        # Same on both machines, as the receiver's: `settings_callback(data)` for each settings
        # message from the Mac, `announce()` for what to send the moment the link is up,
        # `peer_settings` whether the Mac's welcome said it keeps settings in step.
        self.settings_callback: Optional[Callable[[dict], None]] = None
        self.announce: Callable[[], list] = lambda: []
        self.peer_settings: Optional[bool] = None
        # `arrival_callback(edge, x, y)`, as the shared receiver's: the pointer came home through the
        # Mac's return edge and was placed at desktop pixel (x, y) on `edge` of this PC, or, with
        # edge None, input came home by a switch and the pointer is at (x, y) where it was left.
        self._arrival_callback = arrival_callback
        self._desktop = desktop
        self._clipboard = clipboard
        self._socket_factory = socket_factory
        self._is_local = is_local
        self._clock = clock

        self._config = None
        self._config_lock = threading.RLock()
        self._socket_lock = threading.RLock()
        self._sequence_lock = threading.RLock()
        self._sock: Optional[socket.socket] = None
        self._session: Optional[protocol.SecureSession] = None
        self._outbound: "queue.Queue[dict]" = queue.Queue(maxsize=OUTBOUND_QUEUE_SIZE)
        self._stop_event = threading.Event()
        self._threads = []

        self._status = "Not connected to the Mac"
        # Keys whose down-stroke went to the Mac and whose release has not: a
        # dict, not a set, only so the hook thread's pop() stays one operation.
        self._keys_down: dict = {}
        self._ignore_gate = ignored.Gate()
        self._last_sent_seq = 0
        self._last_ack_seq = -1
        self._last_ack_at = 0.0
        self._connected_at = 0.0
        self._last_send_at = 0.0

        self.redirecting = False
        # Where the pointer was pinned while the Mac has input, and where it
        # last was while this PC has it. Two names for two states: conflating
        # them made a push against the edge measure its delta from the pin.
        self._pin_point = None
        self._last_point = None
        self._edge = None
        # Two ways the pointer leaves, either or both armed: the whole edge
        # that leads to the Mac, and the corner sitting at one end of it. The
        # corner is tried first, because its box is inside the edge's strip.
        self._edge_model: Optional[return_edge.ReturnEdge] = None
        self._corner_model: Optional[return_edge.CornerPush] = None
        self._monitors = None
        self._monitors_at = 0.0
        # Set by the app while the Mac is driving this PC: the outbound edge
        # stays disarmed for as long as it is, so input that arrived over the
        # other link cannot push itself straight back out.
        self._receiving = False
        # The receiver's way of sending the Mac's input home, handed in by the
        # app that owns both halves.
        self.send_peer_home = None
        # Both hold the edges and leave the shortcut working, as on the Mac: Pause crossing,
        # which a restart forgets, and the name of a full-screen app in front, set by the app.
        self.crossing_paused = False
        self._full_screen_app = None
        # The buttons of this PC's own mouse that are down, so a push with one held is a drag.
        self._buttons_held = set()
        # (title, message) for a notification, and the Mac's hardware address when the ARP table
        # names a new one; both called off the GUI thread.
        self.on_alert = None
        self.on_mac_learned = None
        self._wake_sender = wake_sender
        self._mac_lookup = mac_lookup
        self._waking = False
        self._wake_lock = threading.Lock()
        self._unacked_sent_at = collections.deque()
        self._round_trips = collections.deque(maxlen=ROUND_TRIP_SAMPLES)
        self._round_trip_at = 0.0

    # -- state the GUI reads ------------------------------------------------

    @property
    def connected(self) -> bool:
        with self._socket_lock:
            return self._sock is not None

    @property
    def status(self) -> str:
        return self._status

    @property
    def edge(self) -> Optional[str]:
        return self._edge

    @property
    def waking(self) -> bool:
        return self._waking

    @property
    def round_trip_ms(self) -> Optional[int]:
        """The Mac's figure, measured the same way: the upper median of the last few trips from
        an input event's send to the ACK naming it, or None while input is not on the Mac, the
        link is down, or nothing fresh has been acknowledged for a few seconds."""
        if not self.redirecting or not self.connected:
            return None
        with self._sequence_lock:
            trips = sorted(self._round_trips)
            measured_at = self._round_trip_at
        if not trips or self._clock() - measured_at > ROUND_TRIP_MAX_AGE_SECONDS:
            return None
        return int(round(trips[len(trips) // 2] * 1000))

    @property
    def full_screen_app(self):
        """The full-screen app holding the edges, or None: also None while this PC's own setting
        has the hold off, read here so every reader of the hold agrees and a change applies at once."""
        if self._setting("hold_full_screen", True):
            return self._full_screen_app
        return None

    @full_screen_app.setter
    def full_screen_app(self, name) -> None:
        self._full_screen_app = name

    @property
    def edges_held(self) -> bool:
        return self.crossing_paused or self.full_screen_app is not None

    def set_receiving(self, receiving: bool) -> None:
        """Called when the Mac takes input on this PC, and again when it gives
        it back. While it is True nothing here arms, sends or crosses."""
        receiving = bool(receiving)
        if receiving == self._receiving:
            return
        self._receiving = receiving
        if receiving and self.redirecting:
            self.set_redirecting(False, came_home=False)
        self._last_point = None
        self._rearm_edge()

    # -- lifecycle ----------------------------------------------------------

    def start(self, config) -> None:
        self.update_config(config)
        if self._threads:
            return
        self._stop_event.clear()
        for name, target in (
            ("Beamer-mac-link", self._connection_worker),
            ("Beamer-mac-out", self._outbound_worker),
            ("Beamer-mac-in", self._reader_worker),
            ("Beamer-mac-watchdog", self._watchdog_worker),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop_event.set()
        self._buttons_held.clear()
        self.set_redirecting(False, came_home=False)
        self._drop_connection()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = []

    def update_config(self, config) -> None:
        with self._config_lock:
            previous = self._config
            self._config = config
        self._ignore_gate.configure(getattr(config, "ignored_inputs", None) or ())
        edge = getattr(config, "mac_return_edge", "") or None
        self._edge = edge if edge in return_edge.EDGES else None
        self._rearm_edge()
        if previous is not None and self._address(previous) != self._address(config):
            self._drop_connection()

    @staticmethod
    def _address(config):
        return (getattr(config, "mac_host", "") or "", int(getattr(config, "port", 0) or 0))

    def _ready_config(self):
        with self._config_lock:
            config = self._config
        if config is None or not getattr(config, "send_to_mac", True):
            return None
        host, port = self._address(config)
        if not host or not port or not config.auth_token:
            return None
        if self._is_local(host):
            return None
        return config

    # -- the hooks' two decisions -------------------------------------------

    def _link_live(self) -> bool:
        """Whether input may still be taken from this PC. Deliberately cheap
        and lock-light: it is read on the hook thread for every keystroke and
        every mouse move, and a hook that cannot answer promptly is a hook
        Windows removes -- with the user's keyboard inside it."""
        if self._sock is None:
            return False
        last = max(self._last_ack_at, self._connected_at)
        return (self._clock() - last) <= ACK_TIMEOUT_SECONDS

    def on_key(self, name: str, down: bool, vk: Optional[int] = None, us: Optional[str] = None) -> bool:
        # Held keys are known by their virtual key where there is one: the name is the character
        # with Shift applied, so Shift let go before A made A's release arrive as "a" and miss.
        held = vk if vk is not None else name
        if not self.redirecting:
            # A key that went down on the Mac and is still held after input
            # came home: the Mac was sent its release when input left, and
            # Windows never saw it go down, so it must see neither its
            # autorepeats nor its release. The entry goes on the release.
            if held not in self._keys_down:
                return False
            if not down:
                self._keys_down.pop(held, None)
            return True
        if not self._link_live():
            self._force_local("the link to the Mac went quiet")
            return False
        if vk is not None and self._ignore_gate.keeps(ignored.key(vk), down):
            return False
        if down:
            # The name it went down under is kept, so its release matches even if Shift or the
            # modifier style changes while it is held.
            data = self._keys_down.get(held) or self._key_data(name, us)
            self._keys_down[held] = data
            self._enqueue({"type": protocol.MSG_KEYDOWN, "data": data})
        else:
            data = self._keys_down.pop(held, None) or self._key_data(name, us)
            # Marked, so a release still queued when input comes home is sent
            # rather than dropped by the gate: the Mac has the key-down.
            self._enqueue({"type": protocol.MSG_KEYUP, "data": data, _RELEASE_MARK: True})
        return True

    def _key_data(self, name: str, us: Optional[str]) -> dict:
        """A key message's data. `us` is the key's place on a US keyboard, which the Mac types on
        when its layout cannot type the character: a PC on Russian sends C as "с"."""
        data = {"key": self._wire_name(name)}
        if us is not None:
            data["us"] = us
        return data

    def _wire_name(self, name: str) -> str:
        """The capture names Ctrl "cmd" and the Windows key "ctrl", the Semantic style;
        Positional swaps them back, so each key arrives as the Mac key in its place."""
        if self._setting("modifier_style", "semantic") == "positional":
            return POSITIONAL_SWAP.get(name, name)
        return name

    def on_mouse(self, message: int, x: int, y: int, mouse_data: int) -> bool:
        """Buttons and the wheel, and swallowing a move -- never the distance
        it covered, which comes from on_motion. Returns True when the event
        belongs to the Mac and Windows must not see it."""
        import capture_win

        button = capture_win.button_of(message, mouse_data)
        if button is not None:
            name, down = button
            if down:
                self._buttons_held.add(name)
            else:
                self._buttons_held.discard(name)
        if not self.redirecting:
            return False
        if not self._link_live():
            self._force_local("the link to the Mac went quiet")
            return False
        if message == capture_win.WM_MOUSEMOVE:
            # Swallowed, and the pointer put back where it was: a hook that
            # returns 1 stops the message, but the cursor has already been
            # moved by the time it runs.
            self._warp_to_pin()
            return True
        return self._send_mouse(capture_win, message, mouse_data)

    def on_motion(self, dx: int, dy: int) -> None:
        """One raw mouse movement, in the mouse's own counts. Consumes
        nothing: raw input cannot block an event, so the hook above is what
        keeps the movement off this PC."""
        if self.redirecting:
            if not self._link_live():
                self._force_local("the link to the Mac went quiet")
                return
            self._enqueue({"type": protocol.MSG_MOUSEMOVE, "data": {"dx": int(dx), "dy": int(dy)}})
            return
        self._press_edge(int(dx), int(dy))

    def _send_mouse(self, capture_win, message, mouse_data) -> bool:
        button = capture_win.button_of(message, mouse_data)
        if button is not None:
            name, down = button
            if self._ignore_gate.keeps(ignored.button(name), down):
                return False
            self._enqueue(
                {"type": protocol.MSG_MOUSEDOWN if down else protocol.MSG_MOUSEUP, "data": {"button": name}}
            )
            return True
        if message == capture_win.WM_MOUSEWHEEL:
            self._enqueue(protocol.scroll_msg(capture_win.wheel_notches(mouse_data), 0.0, "line"))
            return True
        if message == capture_win.WM_MOUSEHWHEEL:
            self._enqueue(protocol.scroll_msg(0.0, capture_win.wheel_notches(mouse_data), "line"))
            return True
        # Anything else the Mac has no name for: left to Windows rather than
        # dropped.
        return False

    def _press_edge(self, dx, dy) -> None:
        """One raw movement while input is still this PC's. Feeds the same
        return-edge model the Mac's receiver uses, so the border behaves
        identically whichever side of it the pointer starts on.

        Nothing is held here, unlike the receiver's side of the same model:
        Windows has already stopped the cursor at the edge of the desktop, so
        the hold is the screen's own. Only the pressure is ours to measure,
        and only a breakthrough acts.

        It runs while the Mac is driving this PC too. Every move the Mac makes
        here carries INJECTED_MARK and never arrives, so what does is this
        PC's own mouse, and its push out means the pointer is going back to
        the Mac with this mouse behind it (ignoring this once left only the
        Mac's trackpad able to take the pointer back)."""
        if not self.connected or self.edges_held:
            return
        if self._buttons_held and self._setting("block_while_dragging", True) and self._still_dragging():
            # A drag that reaches the edge is dragging, not leaving: the push so far is dropped,
            # as the Mac drops it, so letting go at the edge does not finish a crossing.
            for candidate in (self._corner_model, self._edge_model):
                if candidate is not None:
                    candidate.reset()
            return
        try:
            desktop = self._desktop_module()
            monitors = self._cached_monitors()
            pointer = desktop.cursor_position()
            outcome = model = None
            for candidate in (self._corner_model, self._edge_model):
                if candidate is None:
                    continue
                result = candidate.feed(monitors, pointer, float(dx), float(dy))
                if result.action != return_edge.PASS:
                    outcome, model = result, candidate
                    break
            if outcome is None:
                return
        except Exception:
            LOGGER.exception("The way out to the Mac failed; crossing out is off until the next reconnect")
            self._edge_model = self._corner_model = None
            return
        part = None
        if isinstance(model, return_edge.PartEdge):
            part = return_edge.part_of(return_edge.display_fraction(monitors, model.edge, pointer))
        elif isinstance(model, return_edge.CornerPush):
            # The corner's name, so the effects play their corner forms; the glow lights the edge.
            part = model.corner
        self._notify_pressure(model.edge, outcome.pressure, outcome.action == return_edge.CROSS, part)
        if outcome.action == return_edge.CROSS:
            if self._receiving:
                # The Mac's input goes home first, over its own link; its
                # focus back to the Mac clears receiving here too, later, and
                # finds it already cleared.
                send_home = self.send_peer_home
                if send_home is None or not send_home():
                    LOGGER.warning("cannot cross: the Mac is driving this PC and cannot be reached")
                    self._alert("Cannot switch — the Mac is driving this PC and cannot be reached")
                    return
                self._receiving = False
            # The model names the edge on the far side, not the one just left:
            # it is the same border, read from the other end.
            self.set_redirecting(True, arrival_edge=outcome.edge, offset=outcome.offset)

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

    def set_redirecting(self, value, arrival_edge=None, offset=None, came_home=True) -> bool:
        """`arrival_edge` is the Mac edge the pointer arrives at and `offset`
        the fraction along it -- the same fraction it left this PC at, which
        is what makes one border out of two screens. Both are absent when the
        shortcut moved input rather than a crossing.

        Input coming home this way is a switch -- the shortcut, the tray, the
        direction switch or the Mac sending it back -- and is reported to the
        arrival callback with edge None, for the arrival that shows where the
        pointer is. `came_home` is False for the two ways back that show their
        own: a crossing that lands here, and the Mac taking this PC over."""
        value = bool(value)
        if value == self.redirecting:
            return False
        if value:
            if not self.connected:
                LOGGER.warning("cannot redirect: the Mac is not connected")
                if not self.wake():
                    self._alert(f"Cannot switch — {self._status}")
                return False
            if self._receiving:
                # The Mac is driving this PC over the other link, so switching
                # to the Mac means sending its input home, the way back that
                # does not depend on the Mac's own return edge.
                send_home = self.send_peer_home
                if send_home is not None and send_home():
                    return True
                LOGGER.warning("cannot redirect: the Mac is driving this PC and cannot be reached")
                self._alert("Cannot switch — the Mac is driving this PC and cannot be reached")
                return False
            self.redirecting = True
            self._pin_point = self._desktop_module().cursor_position()
            arrival = arrival_edge if arrival_edge in return_edge.EDGES else None
            self._enqueue_control({"type": _LOCAL_CLIPBOARD_SENTINEL_TYPE, "data": {}})
            self._enqueue_control(
                protocol.focus_msg(
                    "mac",
                    edge=arrival,
                    offset=offset,
                    return_edge=arrival or self._mac_arrival_edge(),
                    resistance_px=int(self._resistance()),
                )
            )
            LOGGER.info("redirecting input to the Mac")
        else:
            # The flag first, so the hook thread stops adding keys; then
            # whatever is still down on the Mac is released there before the
            # focus goes home, marked to pass the gate that has just closed.
            # The entries stay, so the physical releases here are swallowed.
            self.redirecting = False
            self._ignore_gate.reset()
            for data in list(self._keys_down.values()):
                self._enqueue_control({"type": protocol.MSG_KEYUP, "data": data, _RELEASE_MARK: True})
            self._pin_point = None
            self._last_point = None
            # A fresh model, not a reset one: the old one disarmed itself at
            # breakthrough so a burst of deltas still in flight could not
            # cross twice, and nothing else re-arms it.
            self._rearm_edge()
            self._enqueue_control(protocol.focus_msg("windows"))
            LOGGER.info("input returned to this PC")
            if came_home:
                self._report_switch_home()
        if self._redirect_callback is not None:
            try:
                self._redirect_callback(value)
            except Exception:
                LOGGER.exception("Redirect callback failed")
        return True

    def _report_switch_home(self) -> None:
        if self._arrival_callback is None:
            return
        try:
            x, y = self._desktop_module().cursor_position()
            self._arrival_callback(None, x, y)
        except Exception:
            LOGGER.exception("Arrival callback for a switch failed")

    def toggle(self) -> None:
        """The shortcut's way across and back, for when the pointer is not at
        an edge -- or the edge is off."""
        self.set_redirecting(not self.redirecting)

    def _handle_switch(self, data) -> None:
        """The Mac pushed the pointer back through its return edge. Stop
        sending, then land the pointer on the edge of this PC it comes back
        to."""
        if not isinstance(data, dict) or data.get("target") != "windows":
            return
        edge = data.get("edge")
        offset = data.get("offset")
        crossed = edge in return_edge.EDGES and isinstance(offset, (int, float)) and not isinstance(offset, bool)
        # Without an edge the Mac sent this PC's input home by its own switch: that is a switch
        # arriving here, and shows where the pointer is.
        self.set_redirecting(False, came_home=not crossed)
        if crossed:
            try:
                desktop = self._desktop_module()
                x, y = return_edge.arrival_position(self._cached_monitors(), edge, offset)
                desktop.set_cursor_position(x, y)
            except Exception:
                LOGGER.exception("Could not place the pointer at the %s edge on arrival", edge)
                return
            if self._arrival_callback is not None:
                try:
                    self._arrival_callback(edge, x, y)
                except Exception:
                    LOGGER.exception("Arrival callback failed")

    # -- the link -----------------------------------------------------------

    def _connection_worker(self) -> None:
        while not self._stop_event.is_set():
            config = self._ready_config()
            if config is None or self.connected:
                self._stop_event.wait(0.5)
                continue
            if not self._connect_once(config):
                self._stop_event.wait(max(0.5, float(getattr(config, "reconnect_interval_s", 2.0))))

    def _connect_once(self, config) -> bool:
        host, port = self._address(config)
        sock = None
        self._set_status(f"Connecting to the Mac at {host}:{port}")
        try:
            sock = self._socket_factory((host, port), CONNECT_TIMEOUT_SECONDS)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            session = protocol.SecureSession(config.auth_token)
            sock.sendall(session.preamble())
            sock.settimeout(AUTH_TIMEOUT_SECONDS)
            try:
                protocol.recv_preamble(sock, session)
            except socket.timeout:
                raise HandshakeError(OLD_RECEIVER_STATUS) from None
            except protocol.VersionMismatch as exc:
                raise HandshakeError(
                    f"The Mac speaks Beamer protocol v{exc.peer_version}, this PC v{protocol.PROTOCOL_VERSION} — update both apps"
                ) from None
            protocol.send_msg(
                sock,
                session,
                protocol.hello_msg(
                    return_edge=self._mac_arrival_edge(), resistance_px=int(self._resistance())
                ),
            )
            try:
                reply = protocol.recv_msg(sock, session)
            except (protocol.ConnectionClosed, protocol.AuthenticationError):
                # The receiver closes without a word when a frame fails to authenticate.
                raise HandshakeError(AUTH_FAILED_STATUS) from None
            if reply.get("type") != protocol.MSG_WELCOME:
                raise protocol.ProtocolError("the Mac did not confirm authentication")
            data = reply.get("data")
            if not isinstance(data, dict):
                raise protocol.ProtocolError("welcome message missing data")
            if data.get("error") or data.get("version") != protocol.PROTOCOL_VERSION:
                raise HandshakeError(OLD_RECEIVER_STATUS)
            self.peer_settings = protocol.keeps_settings(data)
            sock.settimeout(SOCKET_IO_TIMEOUT_SECONDS)
        except HandshakeError as exc:
            self._close(sock)
            self._set_status(exc.status)
            LOGGER.warning("connection to the Mac failed: %s", exc.status)
            return False
        except (OSError, protocol.ProtocolError) as exc:
            self._close(sock)
            self._set_status(f"The Mac is not reachable on {host}:{port}")
            LOGGER.debug("connection to the Mac failed: %s", exc)
            return False
        with self._socket_lock:
            if self._stop_event.is_set() or self._sock is not None:
                self._close(sock)
                return False
            self._sock = sock
            self._session = session
        self._clipboard_module().forget_sync()
        now = self._clock()
        with self._sequence_lock:
            self._last_sent_seq = 0
            self._last_ack_seq = -1
            self._last_ack_at = now
            self._unacked_sent_at.clear()
            self._round_trips.clear()
            self._round_trip_at = 0.0
        self._connected_at = now
        self._last_send_at = now
        self._set_status(f"Connected to the Mac at {host}")
        LOGGER.info("connected to the Mac at %s:%s", host, port)
        self._learn_mac_address(host)
        try:
            announcements = list(self.announce())
        except Exception:
            LOGGER.exception("announce failed")
            announcements = []
        for announcement in announcements:
            self._send_raw(announcement)
        return True

    def _learn_mac_address(self, host) -> None:
        """Read the Mac's hardware address from the ARP table while the entry is fresh, for the
        wake-up a later switch may need."""
        try:
            address = self._mac_lookup(host)
        except Exception:
            LOGGER.exception("hardware address lookup failed")
            return
        if not address or address == self._setting("mac_hardware_address", ""):
            return
        # Not written here: the config is the app's own object, which the app saves.
        LOGGER.info("learned the Mac's hardware address from the ARP table")
        if self.on_mac_learned is not None:
            try:
                self.on_mac_learned(address)
            except Exception:
                LOGGER.exception("on_mac_learned callback failed")

    def wake(self) -> bool:
        """Send the Mac a wake-on-LAN packet and wait up to a minute for the link, as the Mac
        does for the PC. Nothing is queued: the person switches again once the Mac is up. False
        when there is no address to wake or a wake is already in flight."""
        address = self._setting("mac_hardware_address", "")
        if not address or self.connected or self._status in (AUTH_FAILED_STATUS, OLD_RECEIVER_STATUS) \
                or "protocol v" in self._status:
            # A Mac that answered and refused is awake; waking it would hide why.
            return False
        with self._wake_lock:
            if self._waking:
                return True
            self._waking = True
        threading.Thread(target=self._wake_worker, args=(address,), name="Beamer-wake-mac", daemon=True).start()
        return True

    def _wake_worker(self, address) -> None:
        started = self._clock()
        try:
            self._wake_sender(address, self._setting("mac_host", "") or None)
        except (OSError, ValueError) as exc:
            LOGGER.warning("wake-on-LAN packet not sent: %s", exc)
            self._waking = False
            self._alert("Could not send the wake-up packet")
            return
        LOGGER.info("sent wake-on-LAN to the Mac")
        self._alert(WAKING_STATUS)
        while not self._stop_event.wait(WAKE_POLL_SECONDS):
            if self.connected:
                self._waking = False
                LOGGER.info("the Mac woke and connected")
                self._alert("Your Mac is awake — switch again to send input")
                return
            if self._clock() - started >= wol.WAKE_WINDOW_SECONDS:
                break
        self._waking = False
        if self._stop_event.is_set():
            return
        LOGGER.warning("the Mac did not answer within %.0fs of the wake-on-LAN packet", wol.WAKE_WINDOW_SECONDS)
        self._alert(NOT_WOKEN_STATUS)

    def _alert(self, message: str) -> None:
        if self.on_alert is None:
            return
        try:
            self.on_alert("Beamer", message)
        except Exception:
            LOGGER.exception("Alert callback failed")

    def _outbound_worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                message = self._outbound.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._process_outbound(message)
            except Exception:
                # One bad message must not end the only thread that drains
                # this queue: nothing restarts it, and the hooks would keep
                # swallowing input into a queue nobody reads.
                LOGGER.exception("outbound worker recovered")
                self._force_local(f"outbound message {message.get('type')!r} failed")
            finally:
                self._outbound.task_done()

    def _process_outbound(self, message) -> None:
        message_type = message.get("type")
        if message_type not in _GATE_EXEMPT_TYPES and not self.redirecting and not message.get(_RELEASE_MARK):
            return
        if message_type == _LOCAL_CLIPBOARD_SENTINEL_TYPE:
            self._send_local_clipboard()
            return
        if message_type in CONTROL_MESSAGE_TYPES:
            self._send_raw(message)
            return
        with self._sequence_lock:
            self._last_sent_seq += 1
            seq = self._last_sent_seq
        data = dict(message.get("data", {}))
        data["seq"] = seq
        if self._send_raw({"type": message_type, "data": data}):
            with self._sequence_lock:
                self._unacked_sent_at.append((seq, self._last_send_at))

    def send_arrangement(self, mac_edge: str, set_at: int) -> bool:
        """Tell the Mac where the machines are, when the change was made here."""
        return self._send_raw(protocol.arrangement_msg(mac_edge, set_at))

    def send_settings(self, data: dict) -> bool:
        """Tell the Mac this PC's settings state."""
        return self._send_raw(protocol.settings_msg(data))

    def _send_local_clipboard(self) -> None:
        try:
            text, image = self._clipboard_module().changed_contents()
        except Exception:
            LOGGER.exception("Failed to read the local clipboard for the Mac")
            return
        if text and len(text.encode("utf-8")) > protocol.CLIPBOARD_MAX_BYTES:
            LOGGER.warning("Local clipboard text too large; skipping the text")
            text = None
        if image is not None and len(image) > protocol.CLIPBOARD_IMAGE_MAX_BYTES:
            LOGGER.warning("Local clipboard image over the cap; skipping the image")
            image = None
        if not text and image is None:
            return
        self._send_raw(protocol.clipboard_msg(text or None, image))

    def _send_raw(self, message) -> bool:
        with self._socket_lock:
            sock, session = self._sock, self._session
            if sock is None:
                return False
            try:
                protocol.send_msg(sock, session, message)
                self._last_send_at = self._clock()
                return True
            except (OSError, protocol.ProtocolError) as exc:
                self._connection_failed(f"Sending to the Mac failed: {exc}", expected_socket=sock)
                return False

    def _reader_worker(self) -> None:
        active_socket = None
        decoder = None
        while not self._stop_event.is_set():
            with self._socket_lock:
                sock, session = self._sock, self._session
            if sock is None:
                active_socket = None
                self._stop_event.wait(0.1)
                continue
            if sock is not active_socket:
                active_socket = sock
                decoder = FrameDecoder(session)
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                self._connection_failed(f"Receiving from the Mac failed: {exc}", expected_socket=sock)
                active_socket = None
                continue
            if not chunk:
                self._connection_failed("The Mac closed the connection", expected_socket=sock)
                active_socket = None
                continue
            try:
                for message in decoder.feed(chunk):
                    self._handle_inbound(message)
            except protocol.AuthenticationError:
                self._connection_failed(AUTH_FAILED_STATUS, expected_socket=sock)
                active_socket = None
            except protocol.ProtocolError as exc:
                self._connection_failed(f"Invalid stream from the Mac: {exc}", expected_socket=sock)
                active_socket = None
            except Exception as exc:
                # The same outcome as a malformed frame: this connection goes,
                # the thread stays, and the next connection gets a reader.
                LOGGER.exception("inbound handling failed")
                self._connection_failed(f"Inbound message from the Mac failed: {exc}", expected_socket=sock)
                active_socket = None

    def _handle_inbound(self, message) -> None:
        message_type = message.get("type")
        if not isinstance(message_type, str) or not message_type:
            raise protocol.ProtocolError(f"malformed inbound message: {message!r}")
        with self._sequence_lock:
            self._last_ack_at = self._clock()
        if message_type == protocol.MSG_ACK:
            self._record_ack(message)
            return
        if message_type == protocol.MSG_CLIPBOARD:
            self._apply_inbound_clipboard(message.get("data", {}))
            return
        if message_type == protocol.MSG_SWITCH:
            self._handle_switch(message.get("data", {}))
            return
        if message_type == protocol.MSG_ARRANGEMENT:
            read = protocol.read_arrangement(message.get("data"))
            if read is not None and self._arrangement_callback is not None:
                try:
                    self._arrangement_callback(*read)
                except Exception:
                    LOGGER.exception("Arrangement callback failed")
            return
        if message_type == protocol.MSG_SETTINGS:
            data = message.get("data")
            if isinstance(data, dict) and self.settings_callback is not None:
                try:
                    self.settings_callback(data)
                except Exception:
                    LOGGER.exception("Settings callback failed")
            return

    def _record_ack(self, message) -> None:
        data = message.get("data")
        if not isinstance(data, dict):
            raise protocol.ProtocolError("ACK data must be a JSON object")
        seq = data.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise protocol.ProtocolError("ACK seq must be a non-negative integer")
        with self._sequence_lock:
            if seq < self._last_ack_seq:
                raise protocol.ProtocolError("ACK seq moved backwards")
            if seq > self._last_sent_seq:
                raise protocol.ProtocolError("ACK seq is ahead of the sender")
            advanced = seq > self._last_ack_seq
            self._last_ack_seq = seq
            if advanced:
                self._measure_round_trip(seq, self._clock())

    def _measure_round_trip(self, seq, now) -> None:
        """Under _sequence_lock. From the send of the event the ACK names, as on the Mac: the
        receiver acknowledges the highest seq it has handled on a timer, so an older one would
        only measure that timer."""
        sent_at = None
        while self._unacked_sent_at and self._unacked_sent_at[0][0] <= seq:
            sent_seq, at = self._unacked_sent_at.popleft()
            if sent_seq == seq:
                sent_at = at
        if sent_at is not None:
            self._round_trips.append(now - sent_at)
            self._round_trip_at = now

    def _apply_inbound_clipboard(self, data) -> None:
        if not isinstance(data, dict):
            return
        text = data.get("text")
        if not isinstance(text, str) or not text:
            text = None
        elif len(text.encode("utf-8")) > protocol.CLIPBOARD_MAX_BYTES:
            LOGGER.warning("Ignoring oversized inbound clipboard text from the Mac")
            text = None
        image = protocol.clipboard_image(data)
        if text is None and image is None:
            return
        try:
            self._clipboard_module().set_contents(text, image)
        except Exception:
            LOGGER.exception("Failed to set the local clipboard from the Mac")

    def _watchdog_worker(self) -> None:
        while not self._stop_event.wait(0.1):
            self._watchdog_tick()

    def _watchdog_tick(self) -> bool:
        if not self.connected:
            return False
        now = self._clock()
        with self._sequence_lock:
            last_heartbeat = max(self._last_ack_at, self._connected_at)
        if now - last_heartbeat > ACK_TIMEOUT_SECONDS:
            self._connection_failed("The Mac stopped responding")
            return True
        if now - self._last_send_at >= PING_INTERVAL_SECONDS:
            self._send_raw(protocol.ping_msg())
        return False

    # -- plumbing -----------------------------------------------------------

    def _enqueue(self, message) -> None:
        try:
            self._outbound.put_nowait(message)
        except queue.Full:
            self._force_local("the outbound queue to the Mac filled up")

    def _enqueue_control(self, message) -> None:
        try:
            self._outbound.put_nowait(message)
        except queue.Full:
            LOGGER.error("outbound queue full; dropped control message %r", message.get("type"))

    def _force_local(self, reason) -> None:
        if self.redirecting:
            LOGGER.error("%s; input forced back to this PC", reason)
            self._alert("Input returned to this PC")
        self.redirecting = False
        self._pin_point = None
        # The model that crossed disarmed itself; without a fresh one the edge never crosses again.
        self._rearm_edge()
        if self._redirect_callback is not None:
            try:
                self._redirect_callback(False)
            except Exception:
                LOGGER.exception("Redirect callback failed")

    def _connection_failed(self, reason, expected_socket=None) -> None:
        with self._socket_lock:
            stale = expected_socket is not None and self._sock is not expected_socket
        if stale:
            # A deliberate stop or a reconnect already dropped this socket --
            # closing it out from under a worker mid-recv/-send raises an
            # OSError of its own, which is not a connection failure.
            return
        self._force_local(reason)
        self._set_status(reason)
        self._drop_connection(expected_socket)

    def _drop_connection(self, expected_socket=None) -> None:
        with self._socket_lock:
            if expected_socket is not None and self._sock is not expected_socket:
                return
            sock = self._sock
            self._sock = None
            self._session = None
        self._close(sock)

    @staticmethod
    def _close(sock) -> None:
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

    def _mac_arrival_edge(self) -> Optional[str]:
        """The Mac edge input arrives at, which is also the Mac's way home:
        the far side of this PC's own outward edge. Sent in the hello as well
        as on every switch, so the Mac is armed to push back before it has
        ever been pushed into."""
        return return_edge.OPPOSITE.get(self._edge) if self._edge else None

    def _rearm_edge(self) -> None:
        """A fresh pair of models. Not a reset: a model disarms itself at
        breakthrough so a burst of deltas still in flight cannot cross twice,
        and nothing else re-arms it."""
        methods = self._methods()
        resistance = int(self._resistance())
        if self._edge is None:
            self._edge_model = self._corner_model = None
            return
        if "edge" in methods:
            self._edge_model = return_edge.ReturnEdge(self._edge, resistance)
        elif "part" in methods:
            parts = self._setting("crossing_edge_parts", ("middle",))
            self._edge_model = return_edge.PartEdge(self._edge, parts, resistance)
        else:
            self._edge_model = None
        corner = self._setting("crossing_corner", "top_left")
        if "corner" in methods and corner in return_edge.CORNERS:
            self._corner_model = return_edge.CornerPush(corner, self._edge, resistance)
        else:
            self._corner_model = None

    def _methods(self):
        return set(self._setting("crossing_methods", ("edge", "shortcut")) or ())

    @property
    def shortcut_armed(self) -> bool:
        """Whether the double-tap may move input. The app asks before acting on
        the trigger, so turning the shortcut off here turns it off everywhere."""
        return "shortcut" in self._methods()

    def _setting(self, name, fallback):
        with self._config_lock:
            config = self._config
        if config is None:
            return fallback
        value = getattr(config, name, fallback)
        return fallback if value is None else value

    def _resistance(self) -> int:
        """How hard this PC's own way out pushes back, which is this PC's
        setting -- and also what the Mac is asked to use at its return edge, so
        one number governs the whole round trip in this direction."""
        return int(self._setting("crossing_resistance_px", return_edge.DEFAULT_RESISTANCE_PX))

    def _warp_to_pin(self) -> None:
        """Put the pointer back where it was when input left. Measured: SetCursorPos
        produces no raw input at all -- a deliberate 137-pixel warp in a quiet
        window produced no WM_INPUT -- so the pin cannot feed itself back to
        the Mac as movement nobody made."""
        pin = self._pin_point
        if pin is None:
            return
        try:
            self._desktop_module().set_cursor_position(*pin)
        except Exception:
            LOGGER.exception("Could not hold the pointer while the Mac has input")

    def _cached_monitors(self):
        now = self._clock()
        if self._monitors is None or now - self._monitors_at > MONITORS_MAX_AGE_SECONDS:
            self._monitors = self._desktop_module().monitors()
            self._monitors_at = now
        return self._monitors

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

    def _notify_pressure(self, edge, pressure, crossed, part=None) -> None:
        if self._pressure_callback is None:
            return
        try:
            self._pressure_callback(edge, pressure, crossed, part)
        except Exception:
            LOGGER.exception("Pressure callback failed")

    def _set_status(self, detail: str) -> None:
        self._status = detail
        if self._status_callback is None:
            return
        try:
            self._status_callback(self.connected, detail)
        except Exception:
            LOGGER.exception("Status callback failed")
