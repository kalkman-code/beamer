"""The initiator's side of the link: WIRE.md sections 2, 3 and 9 from the machine that opens it.

One OutboundLink per peer entry with `send` on. It dials the entry's `host` and `port`, runs the
initiator's half of the handshake (section 2), keeps the link alive (a `ping` after a second of
silence, the link ended after two without a byte), numbers the input it is given, measures the
round trip from each `ack` and reconnects every couple of seconds while it is down. Nothing in it
knows which platform it runs on, and it decides nothing about routing: the owner (core/owner.py)
says what to send, the app hands it here, and every inbound message other than `ack` goes back to
the app through `message`.

Callbacks, each on the link's own thread, never with a lock of the link's held:
`state(link, up, text)` for every change of how the link is doing, `text` being the sentence to
show; `up(link, fields)` once the handshake is done and before anything else is sent, `fields`
being the `welcome` as protocol.read_welcome gives it (`id` as 16 bytes); `message(link, message)`."""

import collections
import errno
import logging
import math
import socket
import threading
import time

from core import peerlist
from core import protocol
from core import receiver

LOGGER = logging.getLogger("Beamer")

CONNECT_SECONDS = 1.0
HANDSHAKE_SECONDS = 5.0
RECONNECT_SECONDS = 2.0
FORGOTTEN_AFTER_SECONDS = 60.0
FORGOTTEN_RETRY_SECONDS = 30.0
PING_SECONDS = 1.0
SILENCE_SECONDS = 2.0
READ_SECONDS = 0.1
QUEUE_LIMIT = 2048
QUEUE_BYTES = 32 * 1024 * 1024
STALL_SECONDS = 2.0
SENT_RECORD_LIMIT = 20000
MEDIA_KEYS = frozenset({"volume_mute", "volume_down", "volume_up", "media_next", "media_prev", "media_stop", "media_play_pause"})
WINDOW_SECONDS = 30.0
LOG_SECONDS = 60.0
FOLLOW_AFTER_SECONDS = 10.0
FOLLOW_EVERY_SECONDS = 30.0
SEND_CHUNK = 65536
_CAP_FOR = {protocol.MSG_GESTURE: "gestures", protocol.MSG_TEXT: "text", protocol.MSG_SETTINGS: "settings"}


class LinkEnded(Exception):
    """The link is over; `text` is the sentence to show."""

    def __init__(self, text, kind="failed", stop_dialling=False):
        super().__init__(text)
        self.text = text
        self.kind = kind
        self.stop_dialling = stop_dialling


def connection_ended(exc, name):
    """What a failed connect says, in the words 1.4.x used, naming the peer."""
    number = getattr(exc, "errno", None)
    if number in (51, 64, 65, errno.EHOSTUNREACH, errno.ENETUNREACH):
        return LinkEnded(f"{name} is unreachable from this machine. Check that both can communicate on the local network.", "unreachable")
    if number in (61, errno.ECONNREFUSED):
        return LinkEnded(f"{name} is reachable, but Beamer is not listening on this port.", "refused")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return LinkEnded(f"{name} did not answer before the connection timed out.", "timeout")
    return LinkEnded(f"Connection failed: {exc}", "failed")


class OutboundLink:
    def __init__(
        self,
        token,
        book,
        identity,
        *,
        state=None,
        up=None,
        message=None,
        socket_factory=socket.create_connection,
        tunnel=None,
        clock=time.monotonic,
        reconnect_seconds=RECONNECT_SECONDS,
        hardware=None,
        large=None,
    ):
        self.token = token
        self.key_id = protocol.key_id(token) if protocol.is_paired_token(token) else None
        self._book = book
        self._identity = identity
        # `hardware(host)`: this machine's address on the interface that reaches `host`, asked for each
        # link's own host so every peer is sent the address its wake-on-LAN must go to.
        self._hardware = hardware
        # `large(link, began)`: whether a frame too large for any message but a clipboard, which began
        # arriving at `began`, may be read: only the clipboard of a let-go (owner.expects_clipboard).
        # Any other such frame is opened, so its counter is spent, and dropped without being parsed.
        self._large = large
        self._callbacks = (state, up, message)
        self._muted = False
        self._factory = socket_factory
        self._tunnel = tunnel
        self._clock = clock
        self._reconnect = reconnect_seconds
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stall = threading.Event()
        self._live = False
        self._sock = None
        self._pending = None
        self._session = None
        self._outbox = collections.deque()
        self._cond = threading.Condition(self._lock)
        self._seq = 0
        self.last_sent_seq = 0
        self._written_seq = 0
        self._sent_at = collections.deque()
        self._samples = collections.deque()
        self._period = []
        self._last_ack_seq = -1
        self.last_ack_at = None
        self._last_rx = 0.0
        self._last_tx = 0.0
        self._logged_at = 0.0
        self._caps = frozenset()
        self.peer_id = ""
        self.peer_platform = ""
        self.peer_name = ""
        self.via_tunnel = False
        self._queued_bytes = 0
        self._ping_queued = False
        self._writing = False
        self._locked = False
        self._last_progress = 0.0
        self.down_since = clock()
        self._blocked_for = None
        self._try_host = None
        self._followed_at = None
        self._followed_to = None
        self._answered = False
        self._checked_at = 0.0
        self.status = ""
        # What the last report was, for the app to word in its own way: "connected", "removed",
        # "off", "unreachable", "refused", "timeout", "stopped", "closed", "forgotten", "older", "newer",
        # "not_beamer", "different", "unauthenticated", "wrong_id", "unreadable", "failed".
        self.kind = "failed"
        self.dialled = None
        self._drop_reason = None

    # Lifetime

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def caps(self):
        return self._caps

    def start(self):
        old = self._thread
        if old is not None and old.is_alive() and self._stop.is_set() and old is not threading.current_thread():
            old.join(timeout=2.0)
        with self._lock:
            if self.running:
                self._blocked_for = None
                self._wake.set()
                return
            self._stop.clear()
            self._muted = False
            self._blocked_for = None
            self._thread = threading.Thread(target=self._run, name="Beamer-link", daemon=True)
        self._thread.start()

    def stop(self, wait=True):
        """End the link for good. By default this returns once its thread has, so nothing is
        reported afterwards; a caller that must not wait (a window removing machines, with the
        thread possibly in a 3 second connect) passes `wait=False`: the socket is closed, the
        thread ends on its own, and from this call on nothing it does is reported."""
        self._muted = not wait
        self._stop.set()
        self._wake.set()
        self._close()
        thread = self._thread
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def drop(self, reason):
        """End the link now, saying why; the link then reconnects as it would after any loss."""
        self._drop_reason = reason
        self._close()

    def flush(self, timeout=0.5):
        """Wait, for at most `timeout`, until what is queued has been written: a machine that is
        stopping gives the let-go `focus` it just posted time to leave."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._cond:
                if not self._live or (not self._outbox and not self._writing):
                    return
            time.sleep(0.01)

    def refresh(self):
        """The entry changed: a link that stopped dialling after `wrong_id` may try again."""
        self._blocked_for = None
        self._wake.set()

    def live(self):
        return self._live

    # Reading the entry

    def entry(self):
        for peer in self._book.peers():
            if peer.get("token") == self.token:
                return peer
        return None

    @staticmethod
    def _target(entry):
        """Where to dial the entry, or None when it is not dialled: send off, no address, or not
        made by pairing (a typed token, or any token migrated from 1.4.x)."""
        host, port = entry.get("host"), entry.get("port")
        if (not protocol.linkable(entry) or entry.get("in_use", True) is not True or entry.get("send") is not True
                or not host or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535):
            return None
        return host, port

    def _name(self, entry):
        """What this link's sentences call the machine: its label, so two of one name are told apart."""
        entry = entry or {}
        try:
            shown = peerlist.label_for(self._book.peers(), token=entry.get("token"))
        except Exception:
            shown = ""
        return shown or entry.get("name") or entry.get("host") or "The other machine"

    # The thread

    def _run(self):
        closed_since = None
        try:
            while not self._stop.is_set():
                try:
                    entry = self.entry()
                except Exception:
                    # A settings file that cannot be read this once (a lock, a half-written save) is
                    # not a removed pairing; the thread must outlive it or the peer is never dialled again.
                    LOGGER.exception("Could not read the settings for a link; trying again")
                    self._wake.wait(self._reconnect)
                    self._wake.clear()
                    continue
                if entry is None:
                    self._report(False, "This pairing was removed", "removed")
                    return
                target = self._target(entry)
                shape = (entry.get("host"), entry.get("port"), entry.get("id"))
                if self._blocked_for is not None and self._blocked_for != shape:
                    self._blocked_for = None
                with self._lock:
                    tried, self._try_host = self._try_host, None
                if self._blocked_for is not None:
                    self._wake.wait(0.25)
                    self._wake.clear()
                    continue
                if target is None:
                    # A beacon's address is tried only for an entry that could be dialled at all.
                    self._wake.wait(0.25)
                    self._wake.clear()
                    continue
                port = target[1]
                host = tried or target[0]
                wait = self._reconnect
                try:
                    self._connect(entry, host, port, direct=tried is not None)
                    closed_since = None
                except LinkEnded as ended:
                    # A try at a beacon's address that fails shows nothing: a forged beacon must
                    # not be able to put a message in front of the user, or block the real address.
                    if tried is None and not self._stop.is_set():
                        # Closed unanswered, and only that, for a minute by a machine this one has linked
                        # with: it no longer has this pairing (WIRE.md section 2, initiator step 3). A
                        # peer that is busy or still waking answers something else, or answers, within
                        # that time. Said once, and dialled slowly, rather than every two seconds for
                        # ever (06-10-2026).
                        now = self._clock()
                        if ended.kind == "closed" and entry.get("linked"):
                            if closed_since is None:
                                closed_since = now
                        else:
                            closed_since = None
                        if closed_since is not None and now - closed_since >= FORGOTTEN_AFTER_SECONDS:
                            name = self._name(entry)
                            if self.kind != "forgotten":
                                LOGGER.info("%s has closed every handshake for %d seconds; it no longer has this pairing",
                                            name, FORGOTTEN_AFTER_SECONDS)
                            self._report(False, f"{name} no longer has this pairing: remove {name} here and pair "
                                         "the two again", "forgotten")
                            wait = max(wait, FORGOTTEN_RETRY_SECONDS)
                        else:
                            self._report(False, ended.text, ended.kind)
                        if ended.stop_dialling:
                            self._blocked_for = shape
                except Exception:
                    closed_since = None
                    LOGGER.exception("the link to %s failed", self._name(entry))
                    if tried is None and not self._stop.is_set():
                        self._report(False, "The link failed", "failed")
                if self._stop.is_set():
                    break
                self._wake.wait(wait)
                self._wake.clear()
        finally:
            self._live = False

    def _report(self, up, text, kind=None):
        self.status = text
        if kind is not None:
            self.kind = kind
        state = self._callbacks[0]
        if state is not None and not self._muted:
            try:
                state(self, up, text)
            except Exception:
                LOGGER.exception("state callback failed")

    def _call(self, index, *args):
        callback = self._callbacks[index]
        if callback is None or self._muted:
            return
        try:
            callback(self, *args)
        except Exception:
            LOGGER.exception("link callback failed")

    # The handshake (WIRE.md section 2, the initiator)

    def _connect(self, entry, host, port, direct):
        name = self._name(entry)
        sock = None
        self.via_tunnel = False
        self._dialled_to, self._dialled_port = host, port
        try:
            try:
                sock = self._factory((host, port), timeout=CONNECT_SECONDS)
            except OSError as exc:
                if exc.errno != errno.EHOSTUNREACH or self._tunnel is None or direct:
                    raise connection_ended(exc, name) from None
                try:
                    sock = self._tunnel()
                except OSError as tunnel_exc:
                    raise LinkEnded("macOS 27 blocked direct LAN access. Open /Applications/Beamer Tunnel.command.", "blocked") from tunnel_exc
                self.via_tunnel = True
            self.status = f"Connecting to {host}:{port}"
            with self._lock:
                self._pending = sock
            if self._stop.is_set():
                raise LinkEnded("This link was stopped", "off")
            deadline = time.monotonic() + HANDSHAKE_SECONDS
            fields = self._handshake(_Interruptible(sock, self._stop), entry, host, deadline, direct)
            if self._stop.is_set():
                raise LinkEnded("This link was stopped", "off")
        except LinkEnded:
            self._forget_pending(sock)
            raise
        except (socket.timeout, TimeoutError):
            self._forget_pending(sock)
            if self._answered:
                raise LinkEnded(f"{name} stopped responding", "stopped") from None
            raise LinkEnded(f"{name} did not answer as a Beamer: it is probably running one from before version 4", "not_beamer") from None
        except (protocol.ConnectionClosed, ConnectionError):
            self._forget_pending(sock)
            raise LinkEnded(self._closed_text(entry), "closed") from None
        except OSError as exc:
            self._forget_pending(sock)
            raise connection_ended(exc, name) from None
        except protocol.AuthenticationError:
            self._forget_pending(sock)
            raise LinkEnded(f"{name} could not be authenticated: something on the path altered the link", "unauthenticated") from None
        except protocol.ProtocolError as exc:
            self._forget_pending(sock)
            raise LinkEnded(f"{name} did not answer like Beamer: {exc}", "failed") from None
        self._answered = True
        sock.settimeout(READ_SECONDS)
        with self._lock:
            self._pending = None
        self._serve(sock, fields)

    def _hardware_towards(self, host, identity):
        """The `hw` for the hello to `host`. The hook is optional and never fatal: a hook that raises
        sends none, because the field is optional on the wire and the link matters more."""
        if self._hardware is None:
            return identity.get("hw")
        try:
            return self._hardware(host) or None
        except Exception:
            LOGGER.exception("Could not read this machine's hardware address towards %s", host)
            return None

    def _forget_pending(self, sock):
        with self._lock:
            if self._pending is sock:
                self._pending = None
        _close_socket(sock)

    def _closed_text(self, entry):
        name = self._name(entry)
        if not entry.get("linked"):
            return f"Update Beamer on {name} to 1.5.0; if it is up to date, pair the two again"
        return f"{name} closed the connection: it may have removed this machine, or be too busy to answer"

    def _handshake(self, sock, entry, host, deadline, direct):
        name = self._name(entry)
        identity = self._identity()
        own_id = bytes(identity["id"])
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        session = protocol.LinkSession(self.token, protocol.ROLE_INITIATOR)
        sock.sendall(session.preamble())
        try:
            # No separate deadline for the preamble: the handshake's five seconds cover it all.
            preamble = protocol.recv_link_preamble(sock, deadline)
        except protocol.VersionMismatch as exc:
            if exc.peer_version is None:
                raise LinkEnded(f"{name} did not answer as a Beamer", "not_beamer") from None
            if exc.peer_version < protocol.LINK_VERSION:
                raise LinkEnded(f"Update Beamer on {name}: it is older than this one", "older") from None
            raise LinkEnded(f"{name} runs a newer Beamer: update Beamer on this machine", "newer") from None
        try:
            session.accept_preamble(preamble)
        except protocol.PreambleRefused:
            raise LinkEnded(f"A different Beamer answered at {host}", "different") from None
        hello = protocol.hello_v6(
            own_id, identity["name"], identity["platform"], identity["app"], identity["caps"], int(identity.get("port") or 0), self._hardware_towards(host, identity))
        sock.settimeout(max(0.001, deadline - time.monotonic()))
        protocol.send_msg(sock, session, hello)
        try:
            welcome = protocol.read_welcome(protocol.recv_first_msg(sock, session, deadline))
        except protocol.HelloRefused as refused:
            if refused.error == protocol.ERROR_WRONG_ID:
                raise LinkEnded(f"{name} has this pairing under another machine: remove it on both and pair again.", "wrong_id", stop_dialling=True) from None
            raise LinkEnded(f"{name} could not read what this machine sent first; Beamer on the two machines may differ", "unreadable") from None
        except (protocol.InvalidWelcome, protocol.NotAnObject):
            raise LinkEnded(f"A different Beamer answered at {host}", "different") from None
        changes = {"name": welcome["name"], "platform": welcome["platform"]}
        if welcome["hw"] is not None:
            changes["hw"] = welcome["hw"]
        if direct and host != entry.get("host"):
            changes["host"] = host
        try:
            outcome, _ = self._book.admit(self.key_id, own_id, welcome["id"], changes)
        except Exception:
            LOGGER.exception("Could not save what the link to %s taught", name)
            raise LinkEnded(f"Could not save what {name} told this machine", "failed") from None
        if outcome == receiver.GONE:
            raise LinkEnded("This pairing was removed", "removed")
        if outcome == receiver.WRONG_ID:
            raise LinkEnded(f"A different Beamer answered at {host}", "different")
        sock.settimeout(READ_SECONDS)
        welcome["session"] = session
        return welcome

    # A live link

    def _serve(self, sock, fields):
        session = fields.pop("session")
        entry = self.entry() or {}
        now = self._clock()
        with self._lock:
            self._sock, self._session = sock, session
            self._caps = frozenset(fields["caps"])
            self.peer_id = protocol.id_text(fields["id"])
            self.peer_platform = fields["platform"]
            self.peer_name = fields["name"]
            self._reset_link_state()
            self._last_rx = self._last_tx = self._last_progress = self._logged_at = now
            self._live = True
        name = self._name(entry)
        writer = threading.Thread(target=self._write, args=(sock, session), name="Beamer-link-writer", daemon=True)
        writer.start()
        way = " via the secure macOS 27 fallback" if self.via_tunnel else ""
        self.dialled = (self._dialled_to, self._dialled_port)
        self._report(True, f"Connected to {name}{way}", "connected")
        self._call(1, fields)
        reason, kind = f"{name} closed the connection", "closed"
        try:
            self._read(sock, session, name)
        except LinkEnded as ended:
            reason, kind = ended.text, ended.kind
        except (protocol.ConnectionClosed, ConnectionError):
            pass
        except protocol.AuthenticationError:
            reason, kind = f"{name} could not be authenticated: something on the path altered the link", "unauthenticated"
        except (OSError, protocol.ProtocolError) as exc:
            reason, kind = f"The link to {name} failed: {exc}", "failed"
        finally:
            if self._drop_reason is not None:
                reason, kind, self._drop_reason = self._drop_reason, "failed", None
            self._close()
            writer.join(timeout=1.0)
            self.down_since = self._clock()
            self._followed_at = None
            self._report(False, reason, kind)

    def _reset_link_state(self):
        self._outbox.clear()
        self._queued_bytes = 0
        self._ping_queued = False
        self._writing = False
        self._seq = self.last_sent_seq = self._written_seq = 0
        self._sent_at.clear()
        self._samples.clear()
        self._period = []
        self._last_ack_seq = -1
        self.last_ack_at = None
        self._locked = False
        self._drop_reason = None

    def _close(self, only=None):
        with self._cond:
            if only is not None and self._sock is not only:
                return
            sock, self._sock = self._sock, None
            pending = self._pending if only is None else None
            self._live = False
            self._cond.notify_all()
        _close_socket(sock)
        _close_socket(pending)

    def _read(self, sock, session, name):
        buffer = bytearray()
        began = None
        while not self._stop.is_set() and self._sock is sock:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                chunk = None
            now = self._clock()
            if chunk is not None:
                if not chunk:
                    raise protocol.ConnectionClosed("closed")
                if not buffer:
                    began = now
                buffer.extend(chunk)
                self._last_rx = now
            elif now - self._last_rx > SILENCE_SECONDS:
                raise LinkEnded(f"{name} stopped responding", "stopped")
            while len(buffer) >= protocol.HEADER_SIZE:
                length = int.from_bytes(buffer[:protocol.HEADER_SIZE], "big")
                if length < protocol.MIN_FRAME_BYTES or length > protocol.MAX_FRAME_BYTES:
                    raise protocol.ProtocolError(f"invalid message length: {length}")
                if len(buffer) < protocol.HEADER_SIZE + length:
                    break
                if now - began > protocol.FRAME_COMPLETE_SECONDS:
                    raise LinkEnded(f"{name} took too long to send one message", "stopped")
                body = bytes(buffer[protocol.HEADER_SIZE:protocol.HEADER_SIZE + length])
                del buffer[:protocol.HEADER_SIZE + length]
                plain = session.open_raw(body)
                if len(plain) > protocol.SMALL_MESSAGE_BYTES and not self._large_due(began or now):
                    LOGGER.warning("Dropped a %d byte message from %s that no clipboard was due for", len(plain), name)
                else:
                    self._deal(protocol.parse_message(plain), began or now)
                began = now if buffer else None
            if buffer and now - began > protocol.FRAME_COMPLETE_SECONDS:
                raise LinkEnded(f"{name} took too long to send one message", "stopped")
            self._tick(now)

    def _large_due(self, began):
        if self._large is None:
            return True
        try:
            return bool(self._large(self, began))
        except Exception:
            LOGGER.exception("Could not tell whether a large message was due")
            return False

    def _deal(self, message, began):
        if message.get("type") == protocol.MSG_ACK:
            self._ack(protocol.read_ack(message))
            return
        message.began_at = began
        self._call(2, message)

    def _tick(self, now):
        if self._writing and now - self._last_progress > STALL_SECONDS:
            raise LinkEnded(f"{self._name(self.entry())} stopped accepting data", "stopped")
        if now - self._checked_at >= 1.0:
            self._checked_at = now
            entry = self.entry()
            if entry is None:
                raise LinkEnded("This pairing was removed", "removed")
            if entry.get("send") is not True:
                raise LinkEnded("Sending to this machine was switched off", "off")
        if now - self._last_tx >= PING_SECONDS:
            self._tick_ping()
        if now - self._logged_at >= LOG_SECONDS:
            self._logged_at = now
            self._log_round_trips()

    def _log_round_trips(self):
        """Section 9's line for the minute just past: what was measured, and nothing when no input
        was on this link."""
        with self._lock:
            period, self._period = self._period, []
        if not period:
            return
        ordered = sorted(sample for sample, _held in period)
        count = len(ordered)
        LOGGER.info(
            "round trip to %s: %d samples, median %.1f ms, 95th percentile %.1f ms, largest %.1f ms, mean held_us %.0f",
            self.peer_name, count, ordered[count // 2] * 1000, ordered[math.ceil(count * 0.95) - 1] * 1000,
            ordered[-1] * 1000, sum(held for _sample, held in period) / count)

    def _tick_ping(self):
        with self._cond:
            if self._ping_queued:
                return
            self._ping_queued = True
        if not self._enqueue(protocol.ping_msg(), seq=False):
            self._ping_queued = False

    # Sending

    def post(self, message):
        """A message that carries no `seq`. False when the link is down, the queue is full, or the
        peer cannot take it; a clipboard keeps only the parts its capabilities cover."""
        if not self._live:
            return False
        kind = message.get("type")
        if kind == protocol.MSG_CLIPBOARD:
            data = dict(message.get("data") or {})
            if "clipboard" not in self._caps:
                data.pop("text", None)
            if "clipboard_image" not in self._caps:
                data.pop("image", None)
                data.pop("image_format", None)
            if not data.get("text") and "image" not in data:
                return True
            message = {"type": kind, "data": data}
        elif _CAP_FOR.get(kind) is not None and _CAP_FOR[kind] not in self._caps:
            return True
        return self._enqueue(message, seq=False)

    def send_input(self, message):
        """An input message: given the link's next `seq` and its send time recorded. False when the
        link is down or its queue is full, which the caller treats as the link having failed."""
        kind = message.get("type")
        if kind not in protocol.INPUT_TYPES or not self._live:
            return False
        if kind in _CAP_FOR and _CAP_FOR[kind] not in self._caps:
            return True
        if kind in (protocol.MSG_KEYDOWN, protocol.MSG_KEYUP) and (message.get("data") or {}).get("key") in MEDIA_KEYS and "media_keys" not in self._caps:
            return True
        return self._enqueue(message, seq=True)

    def _enqueue(self, message, seq):
        size = _weight(message)
        with self._cond:
            if not self._live or len(self._outbox) >= QUEUE_LIMIT or self._queued_bytes + size > QUEUE_BYTES:
                return False
            self._queued_bytes += size
            if seq:
                self._seq += 1
                data = dict(message.get("data") or {})
                data["seq"] = self._seq
                message = {"type": message["type"], "data": data}
                self.last_sent_seq = self._seq
            self._outbox.append(message)
            self._cond.notify()
            return True

    def _write(self, sock, session):
        try:
            while True:
                with self._cond:
                    while not self._outbox or self._stall.is_set():
                        if self._sock is not sock:
                            return
                        self._cond.wait(0.05)
                    if self._sock is not sock:
                        return
                    message = self._outbox.popleft()
                    self._queued_bytes = max(0, self._queued_bytes - _weight(message))
                    if message.get("type") == protocol.MSG_PING:
                        self._ping_queued = False
                    self._writing = True
                    self._last_progress = self._clock()
                seq = message.get("data", {}).get("seq")
                if seq is not None and message["type"] in protocol.INPUT_TYPES:
                    # Before the write: the peer's `ack` can reach the reader before this thread
                    # runs again, and it must find the seq already counted as sent.
                    with self._lock:
                        self._written_seq = seq
                        self._sent_at.append((seq, self._clock()))
                    self._check_sent_record()
                frame = memoryview(session.seal(message))
                sent = 0
                while sent < len(frame):
                    try:
                        sent += sock.send(frame[sent:sent + SEND_CHUNK])
                        self._last_progress = self._clock()
                    except socket.timeout:
                        if self._sock is not sock:
                            return
                self._last_tx = self._clock()
                self._writing = False
        except (OSError, protocol.ProtocolError, ValueError) as exc:
            LOGGER.info("Sending to %s failed: %s", self.peer_name, exc)
            self._close(only=sock)

    # Acknowledgements (section 9)

    def _check_sent_record(self):
        if len(self._sent_at) > SENT_RECORD_LIMIT:
            raise protocol.ProtocolError("the peer has not acknowledged anything it was sent")

    def _ack(self, ack):
        seq, held = ack["seq"], ack["held_us"]
        now = self._clock()
        with self._lock:
            if seq < self._last_ack_seq:
                raise protocol.ProtocolError("ack moved backwards")
            if seq > self._written_seq:
                raise protocol.ProtocolError("ack is ahead of anything sent")
            advanced = seq > self._last_ack_seq
            self._last_ack_seq = seq
            self.last_ack_at = now
            sent = None
            while self._sent_at and self._sent_at[0][0] <= seq:
                sent_seq, at = self._sent_at.popleft()
                if sent_seq == seq:
                    sent = at
            if advanced and sent is not None:
                sample = max(0.0, now - sent - held / 1_000_000)
                self._samples.append((now, sample))
                self._period.append((sample, held))
            while self._samples and now - self._samples[0][0] > WINDOW_SECONDS:
                self._samples.popleft()
        self._locked = ack["locked"]

    @property
    def peer_locked(self):
        return bool(getattr(self, "_locked", False)) and self._live

    def round_trips(self, max_age=WINDOW_SECONDS):
        """The round trips measured in the last `max_age` seconds (thirty at most), oldest first."""
        now = self._clock()
        with self._lock:
            return [sample for at, sample in self._samples if now - at <= min(max_age, WINDOW_SECONDS)]

    # Following a peer to a new address (section 6)

    def follow(self, machines):
        """`machines`: what discovery hears, dicts with `name`, `address` and, while that machine shows
        a code, `id`. A link down for ten seconds is tried once at the address of a beacon with this
        entry's exact name, at its saved port, at most every thirty seconds. The address is saved only
        once the link authenticates. Two machines may share a name, so a beacon whose `id` is another
        machine's is passed over, one at another paired machine's saved address is tried after the
        rest (the two may have swapped addresses), and several are tried in turn rather than the
        first for ever."""
        if self._live or self.key_id is None:
            return
        now = self._clock()
        if now - self.down_since < FOLLOW_AFTER_SECONDS:
            return
        if self._followed_at is not None and now - self._followed_at < FOLLOW_EVERY_SECONDS:
            return
        entry = self.entry()
        if entry is None or self._target(entry) is None or not entry.get("name"):
            return
        others = {peer.get("host") for peer in self._book.peers() if peer.get("token") != self.token}
        own_id = entry.get("id")
        found = [m["address"] for m in machines
                 if m.get("name") == entry["name"] and m.get("address") and m["address"] != entry.get("host")
                 and not (own_id and m.get("id") and m["id"] != own_id)]
        found = [address for address in found if address not in others] + [address for address in found if address in others]
        if not found:
            return
        address = found[(found.index(self._followed_to) + 1) % len(found)] if self._followed_to in found else found[0]
        self._followed_at = now
        self._followed_to = address
        with self._lock:
            self._try_host = address
        self._wake.set()


class _Interruptible:
    """The handshake's socket: `recv` waits in short slices so `stop()` is noticed within a tenth
    of a second. Shutting a socket down from another thread does not wake a blocked read on every
    platform (macOS does not), and a handshake can otherwise hold a stopped link for five seconds."""

    def __init__(self, sock, stopped):
        self._sock = sock
        self._stopped = stopped
        self._timeout = None

    def settimeout(self, value):
        self._timeout = value

    def recv(self, size):
        end = None if self._timeout is None else time.monotonic() + self._timeout
        while True:
            if self._stopped.is_set():
                raise OSError(errno.ECONNABORTED, "the link was stopped")
            left = 0.1 if end is None else min(0.1, end - time.monotonic())
            if left <= 0:
                raise socket.timeout("timed out")
            self._sock.settimeout(left)
            try:
                return self._sock.recv(size)
            except socket.timeout:
                continue

    def __getattr__(self, name):
        return getattr(self._sock, name)


def _weight(message):
    """About how many bytes a queued message holds: a clipboard's text and image, else nothing."""
    if message.get("type") != protocol.MSG_CLIPBOARD:
        return 0
    data = message.get("data") or {}
    return len(data.get("text") or "") * 4 + len(data.get("image") or "")


def _close_socket(sock):
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
