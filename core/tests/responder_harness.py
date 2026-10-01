"""A version 6 responder on loopback, and a scripted initiator that speaks to it through
protocol.py's builders, so the responder's half of WIRE.md sections 2 to 5, 9 and 10 is tested
without either app's shell. Only the platform edges are faked: the injector, the desktop, the
clipboard and the settings the peer book reads."""

import base64
import copy
import queue
import socket
import threading
import time

from core import protocol, receiver
from core.return_edge import Rect


def token(seed):
    return base64.urlsafe_b64encode(bytes((seed + i) % 256 for i in range(32))).decode("ascii").rstrip("=")


def ident(seed):
    return bytes((seed + i) % 256 for i in range(1, 17))


HERE = ident(10)
B, C, D = ident(40), ident(70), ident(100)
TOKENS = {B: token(1), C: token(2), D: token(3)}
MONITORS = [Rect(0, 0, 1920, 1080)]


def entry(peer, name, **fields):
    """A peer entry as settings.json holds it (WIRE.md section 1)."""
    data = {
        "id": protocol.id_text(peer) if isinstance(peer, bytes) else peer,
        "name": name,
        "platform": "macos",
        "token": fields.pop("token", None) or TOKENS.get(peer),
        "host": "192.168.77.9",
        "port": 24820,
        "hw": "",
        "send": True,
        "allow_drive": True,
        "side": "",
        "side_set_at": 0,
        "side_by": "",
        "paired_with": [],
        "paired_at": 1790000000,
        "linked": True,
        "from_1_4": False,
    }
    data.update(fields)
    return data


class Settings:
    """The app's settings as held in memory: `load` gives them, `save` replaces them."""

    def __init__(self, peers, zones=(), own=HERE):
        self.data = {"schema": 6, "machine_id": protocol.id_text(own), "peers": copy.deepcopy(list(peers)), "zones": copy.deepcopy(list(zones))}
        self.saves = 0

    def load(self):
        return self.data

    def save(self, data):
        self.data = copy.deepcopy(data)
        self.saves += 1

    def peer(self, key):
        for item in self.data["peers"]:
            if item["token"] == key or item["id"] == key:
                return item
        return None


class FakeInjector:
    def __init__(self):
        self.calls = []
        self.keys_us = []
        self._lock = threading.Lock()

    def _add(self, *call):
        with self._lock:
            self.calls.append(call)

    def inject_key(self, key, down, us=None):
        with self._lock:
            self.keys_us.append(us)
        self._add("key", key, down)

    def inject_mouse_move(self, dx, dy):
        self._add("move", dx, dy)

    def inject_mouse_button(self, button, down):
        self._add("button", button, down)

    def inject_scroll(self, dy, dx=0.0, mode="line"):
        self._add("scroll", dy, dx, mode)

    def inject_gesture(self, name):
        self._add("gesture", name)

    def inject_text(self, text):
        self._add("text", text)

    def release_all(self):
        self._add("release_all")

    def seen(self):
        with self._lock:
            return list(self.calls)


class FakeDesktop:
    def __init__(self, monitors=MONITORS, cursor=(960, 540), slow=0.0):
        self._monitors = list(monitors)
        self.cursor = cursor
        self.slow = slow
        self.placed = []

    def monitors(self):
        return list(self._monitors)

    def cursor_position(self):
        return self.cursor

    def set_cursor_position(self, x, y):
        if self.slow:
            time.sleep(self.slow)
        self.cursor = (x, y)
        self.placed.append((x, y))


class FakeClipboard:
    """`stamp` rises on every change, as the Mac's change count and Windows' sequence number do."""

    def __init__(self, text="here", events=None):
        self.text = text
        self.image = None
        self.stamp = 1
        self.set_calls = []
        self.slow_get = 0.0
        self.slow_set = 0.0
        self.events = events if events is not None else []

    def change_stamp(self):
        return self.stamp

    def copy(self, text):
        self.text = text
        self.stamp += 1

    def get_contents(self):
        time.sleep(self.slow_get)
        return self.text, self.image

    def set_contents(self, text, image):
        time.sleep(self.slow_set)
        self.events.append(("set", text))
        self.set_calls.append((text, image))
        self.text, self.image = text, image
        self.stamp += 1
        return True


CAPS = ["clipboard", "clipboard_image", "gestures", "media_keys", "text", "settings"]


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class Machine:
    """A responder listening on loopback, with fakes at every platform edge."""

    def __init__(self, peers, zones=(), caps=CAPS, away=False, desktop=None, notch_span=None, own=HERE, name="Here", unlock=None, hardware=None, hw=None):
        self.own = own
        self.settings = Settings(peers, zones, own)
        self.book = receiver.PeerBook(self.settings.load, self.settings.save)
        self.injector = FakeInjector()
        self.desktop = desktop or FakeDesktop()
        self.events = []
        self.clipboard = FakeClipboard(events=self.events)
        self.away = away
        self.statuses = []
        self.notices = []
        self.owners = []
        self.arrivals = []
        self.pressures = []
        self.arrangements = []
        self.settings_seen = []
        self.paired = []
        self.links = []
        self.announcements = {}
        self.responder = receiver.LinkResponder(
            self.book,
            lambda: {"id": own, "name": name, "platform": "windows", "app": "1.5.0", "caps": list(caps), **({"hw": hw} if hw else {})},
            status_callback=lambda state, detail: self.statuses.append((state, detail)),
            injector=self.injector,
            desktop=self.desktop,
            clipboard=self.clipboard,
            unlock=unlock,
            hardware=hardware,
            away=lambda: self.away() if callable(self.away) else self.away,
            zones=lambda: self.settings.data["zones"],
            notch_span=notch_span,
            owner_callback=lambda peer: (self.owners.append(peer), self.events.append(("owner", peer))),
            arrival_callback=lambda edge, x, y: self.arrivals.append((edge, x, y)),
            pressure_callback=lambda edge, pressure, crossed, part: self.pressures.append((edge, pressure, crossed, part)),
            arrangement_callback=lambda peer, read: self.arrangements.append((peer, read)),
            settings_callback=lambda peer, data: self.settings_seen.append((peer, data)),
            paired_callback=lambda peer, ids: self.paired.append((peer, ids)),
            notice_callback=self.notices.append,
            link_callback=lambda peer, up: self.links.append((peer, up)),
            announce=lambda peer: list(self.announcements.get(peer, [])),
        )
        self.port = None

    def start(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.responder.start(self.port)
        assert wait_for(lambda: self.responder.listening and self._accepting()), "the responder did not listen"
        return self

    def _accepting(self):
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                return True
        except OSError:
            return False

    def stop(self):
        self.responder.stop()

    def injected(self):
        return [call for call in self.injector.seen() if call[0] != "release_all"]


class Initiator:
    """A scripted version 6 initiator. Its reader thread queues every frame the responder sends."""

    def __init__(self, machine, peer=B, key=None, preamble=True):
        self.peer = peer
        self.token = key or TOKENS[peer]
        self.sock = socket.create_connection(("127.0.0.1", machine.port), timeout=5)
        self.session = protocol.LinkSession(self.token, protocol.ROLE_INITIATOR)
        self.inbox = queue.Queue()
        self.seen = []
        self.unclaimed = []
        self.closed = threading.Event()
        self.seq = 0
        self.route = 0
        if preamble:
            self.sock.sendall(self.session.preamble())

    def hello(self, **fields):
        data = {"id": self.peer, "name": "Peer", "platform": "macos", "app": "1.5.0", "caps": list(CAPS), "port": 24820}
        data.update(fields)
        return protocol.hello_v6(data.pop("id"), data.pop("name"), data.pop("platform"), data.pop("app"), data.pop("caps"), data.pop("port"), data.pop("hw", None))

    def handshake(self, hello=None, raw=None, reader=True):
        """The responder's `welcome` as read_welcome gives it; raises HelloRefused for its error
        answers and ConnectionClosed when it closes."""
        deadline = time.monotonic() + 5
        self.session.accept_preamble(protocol.recv_link_preamble(self.sock, deadline))
        if raw is not None:
            with self.session.send_lock:
                self.sock.sendall(self.session.seal_raw(raw))
        else:
            protocol.send_msg(self.sock, self.session, hello or self.hello())
        welcome = protocol.read_welcome(protocol.recv_first_msg(self.sock, self.session, deadline))
        if reader:
            threading.Thread(target=self._read, daemon=True).start()
        return welcome

    def _read(self):
        self.sock.settimeout(None)
        try:
            while True:
                self.inbox.put(protocol.recv_msg(self.sock, self.session))
        except Exception:
            self.closed.set()

    def send(self, message):
        protocol.send_msg(self.sock, self.session, message)

    def input(self, message):
        self.seq += 1
        message = copy.deepcopy(message)
        message["data"]["seq"] = self.seq
        self.send(message)
        return self.seq

    def key(self, kind, name):
        return self.input(protocol.key_msg(kind, name))

    def move(self, dx, dy):
        return self.input(protocol.mousemove_msg(dx, dy))

    def take(self, route=None, **fields):
        self.route = self.route + 1 if route is None else route
        self.send(protocol.focus_v6(self.route, HERE, **fields))
        return self.route

    def let_go(self, target=None, route=None):
        self.route = self.route + 1 if route is None else route
        self.send(protocol.focus_v6(self.route, target or self.peer))
        return self.route

    def _claim(self, match, timeout):
        """The first frame `match` accepts. Frames passed over are kept for a later call, except
        `ack`s, which are a stream: only one that arrives while it is waited for counts."""
        for message in self.unclaimed:
            if match(message):
                self.unclaimed.remove(message)
                return message
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                message = self.inbox.get(timeout=left)
            except queue.Empty:
                return None
            self.seen.append(message)
            if match(message):
                return message
            if message.get("type") != protocol.MSG_ACK:
                self.unclaimed.append(message)

    def expect(self, kind, timeout=2.0, where=None):
        """The next frame of `kind` (and matching `where`); None when none comes."""
        found = self._claim(lambda m: m.get("type") == kind and (where is None or where(m["data"])), timeout)
        return None if found is None else found["data"]

    def answer(self, route, timeout=2.0):
        """The `accept` or `refuse` for `route`, as (type, data), or None."""
        found = self._claim(
            lambda m: m.get("type") in (protocol.MSG_ACCEPT, protocol.MSG_REFUSE) and m["data"].get("route") == route, timeout
        )
        return None if found is None else (found["type"], found["data"])

    def acked(self, seq, timeout=2.0):
        return self.expect(protocol.MSG_ACK, timeout, where=lambda data: data["seq"] >= seq)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def silent_close(sock, timeout=2.0):
    """True when the responder closed `sock` without sending a byte."""
    sock.settimeout(timeout)
    try:
        return sock.recv(1) == b""
    except ConnectionError:
        return True
    except socket.timeout:
        return False
