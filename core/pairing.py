"""LAN discovery and pairing, shared by mac_app and win_app (two identical copies; the Mac
test suite checks they match byte for byte).

The host (the PC) broadcasts a small UDP beacon -- hostname, Beamer TCP port, and a random
pairing id while a code is on screen -- and the requester (the Mac) lists what it hears. Pairing
is then a PAKE with the six-digit code as its password: CPace, draft-irtf-cfrg-cpace-21, suite
CPACE-X25519-SHA512 with initiator and responder roles, followed by the explicit key
confirmation of the draft's section 10.4. Four datagrams:

  requester -> host  pair_start    {pair, v, nonce, share: Ya, name, id}
  host -> requester  pair_answer   {pair, ok, share: Yb, tag: Tb, name, id}
  requester -> host  pair_confirm  {pair, tag: Ta}
  host -> requester  pair_done     {pair, ok, tag: Td}

Both shares are X25519 public values over a generator derived from the code, so neither says
anything about the code to someone who does not already know it. The shared token is HKDF over
CPace's session key. The full specification is PAIRING.md at the repository root;
tests/pairing_vectors.json holds vectors for every step.

Where no broadcast reaches the Mac (another subnet, a VPN), the Mac asks a typed address for its
beacon with a `find` datagram, and pairing continues exactly as above.

What this protects against, and what it does not:

- The token never crosses the wire, and neither does the code. The beacon carries nothing a
  token or key could be derived from.
- Nothing on the wire can be tested against a list of codes offline, recorded or live. Whoever
  takes part without the code gets one guess against each side: a host answers only the first
  pair_start a code receives, a requester judges only the first pair_answer to its share, and
  it does not send a code again once an answer to it has failed. These rules are what keep an
  attacker to a guess a side, two chances in a million for a code, so none may be relaxed.
- A wrong code fails closed. The requester checks the host's tag before it sends its own, and
  the host stores a token only after the requester's tag checks out. A fresh code needs a person
  at the PC to press Pair a Mac again, and that is the rate limit. Codes expire after
  CODE_LIFETIME_SECONDS.
- Anyone on the LAN can still stop a pairing: a junk pair_start uses up the code. That is a
  denial, not a compromise.
- The host's address and port still come from the beacon, which nothing authenticates. That
  can point the Mac at the wrong address; it cannot read or forge the link.
- The generator is mapped to the curve in Python integers, which are not constant time. The
  code it depends on is single-use and lives a minute, so there is one computation to time per
  code on the PC and one per attempt on the Mac, and the PC's answer leaves a fixed time after
  the request whatever that computation took.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import queue
import secrets
import socket
import threading
import time

from nacl.bindings import crypto_scalarmult
from nacl.exceptions import CryptoError

from . import protocol

# One home for the port, in the module both apps share byte-for-byte.
PAIRING_PORT = protocol.PAIRING_PORT
BEACON_VERSION = 1
# The pairing exchange's own version, in every beacon and every pair_start. 1 was the
# scrypt-keyed HMAC proof of 1.4.2 and earlier, which carried no number. There is no fallback
# to it: a version that does not match ends the attempt with ERROR_VERSION.
PAIRING_VERSION = 2
BEACON_INTERVAL_SECONDS = 2.0
# Three missed beacons: long enough to ride out Wi-Fi hiccups, short enough that a PC that
# went to sleep leaves the list before anyone tries it.
BEACON_STALE_SECONDS = 7.0
# More PCs than any one network shows, and a ceiling on what forged beacons can make a Mac hold.
MAX_SEEN_HOSTS = 64
# A good pair_answer leaves this long after its pair_start arrived, however long it took to
# make. The generator's arithmetic is not constant time, and this keeps its duration out of
# the one thing a stranger can time: the answer.
PAIR_ANSWER_SECONDS = 0.05
CODE_LIFETIME_SECONDS = 60.0
# How long a requester will not send a code again after an answer to it failed: past the code's
# own life, after which a guess at it is worth nothing.
FAILED_CODE_SECONDS = 2 * CODE_LIFETIME_SECONDS
CODE_DIGITS = 6
MAX_DATAGRAM_BYTES = 1024
PAIR_REPLY_TIMEOUT_SECONDS = 1.5
PAIR_ATTEMPTS = 3
NONCE_BYTES = 16
SHARE_BYTES = 32
TAG_BYTES = 64
# A name is bound into the exchange as sent, so it is cut before it is sent, never after. 48
# characters keep the largest datagram under MAX_DATAGRAM_BYTES even when JSON escapes each one.
MAX_NAME_CHARS = 48
MAX_IDENTITY_BYTES = 32
# A pairing id is 32 hex characters; 1.4.2's was 8. Anything longer or not ASCII is not one.
MAX_PAIR_ID_CHARS = 64

MSG_BEACON = "beacon"
MSG_PAIR_START = "pair_start"
MSG_PAIR_ANSWER = "pair_answer"
MSG_PAIR_CONFIRM = "pair_confirm"
MSG_PAIR_DONE = "pair_done"
# The 1.4.2 exchange's two datagrams. A host still answers the request, with a refusal, so an
# older Mac is told something rather than left to time out.
MSG_PAIR_REQUEST = "pair_request"
MSG_PAIR_REPLY = "pair_reply"
# The Mac asking one address for its beacon, for a PC that no broadcast reaches (another subnet,
# a VPN). The answer is the beacon every listener already hears, so it discloses nothing new.
MSG_FIND = "find"

ERROR_NOT_PAIRING = "not_pairing"
ERROR_REFUSED = "refused"
ERROR_VERSION = "version"
_ERRORS = (ERROR_NOT_PAIRING, ERROR_REFUSED, ERROR_VERSION)

# What the exchange binds besides the code. The two roles are fixed by who shows the code; the
# identities are parameters, empty until machines have ids of their own (wire v6), so a later
# version changes what is fed in and nothing else.
ROLE_REQUESTER = b"requester"
ROLE_HOST = b"host"
CHANNEL = b"beamer-pair-v2"
TOKEN_INFO = b"beamer-pair-token-v2"
DONE_LABEL = b"beamer-pair-done"

_DSI = b"CPace255"
_FIELD_PRIME = 2**255 - 19
_CURVE_A = 486662
_SHA512_BLOCK_BYTES = 128


class PairingError(Exception):
    pass


def new_code() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(CODE_DIGITS))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    if not isinstance(text, str):
        raise PairingError("expected base64 text")
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError as exc:
        raise PairingError("bad base64") from exc
    # The decoder skips what it does not know, so several spellings give the same bytes. Only
    # the one _b64 writes is taken: a field then has one form on the wire on every platform.
    if _b64(data) != text:
        raise PairingError("bad base64")
    return data


def _prepend_len(data: bytes) -> bytes:
    """The draft's prepend_len: the length as LEB128, then the bytes."""
    length = len(data)
    encoded = bytearray()
    while True:
        encoded.append(length & 0x7F if length < 128 else (length & 0x7F) | 0x80)
        length >>= 7
        if length == 0:
            return bytes(encoded) + data


def _lv_cat(*parts: bytes) -> bytes:
    return b"".join(_prepend_len(part) for part in parts)


def _elligator2(r: int) -> int:
    """The u coordinate Elligator 2 maps a field element to on Curve25519 (RFC 9380 6.7.1 with
    Z = 2, as the draft's appendix A.5 repeats it)."""
    p = _FIELD_PRIME
    v = -_CURVE_A * pow(1 + 2 * r * r, p - 2, p) % p
    square = pow((v * v * v + _CURVE_A * v * v + v) % p, (p - 1) // 2, p) == 1
    return v if square else (-v - _CURVE_A) % p


def _generator(prs: bytes, channel: bytes, sid: bytes) -> bytes:
    """The draft's calculate_generator for G_X25519 with SHA-512 (sections 8.1 and 8.2)."""
    padding = bytes(max(0, _SHA512_BLOCK_BYTES - 1 - len(_prepend_len(prs)) - len(_prepend_len(_DSI))))
    digest = hashlib.sha512(_lv_cat(_DSI, prs, padding, channel, sid)).digest()[:SHARE_BYTES]
    r = int.from_bytes(digest, "little") & ((1 << 255) - 1)
    return _elligator2(r).to_bytes(SHARE_BYTES, "little")


def _x25519(scalar: bytes, point: bytes) -> bytes:
    """X25519 (RFC 7748), refusing a point that lands on the neutral element: the draft's
    scalar_mult_vfy, which MUST abort there. libsodium reports exactly that case as an error."""
    try:
        product = crypto_scalarmult(scalar, point)
    except CryptoError as exc:
        raise PairingError(ERROR_REFUSED) from exc
    if product == bytes(SHARE_BYTES):
        raise PairingError(ERROR_REFUSED)
    return product


def _session_id(pair_id: str, nonce: bytes) -> bytes:
    # Random bytes from both ends, as the draft recommends for sid: the host's in the pairing
    # id, the requester's in its nonce.
    return _lv_cat(pair_id.encode("utf-8"), nonce)


def _party(role: bytes, identity: bytes, name: str) -> bytes:
    """One side's associated data: CPace's ADa or ADb. The role comes first, so the two sides'
    can never be equal, which is what stops a tag being reflected back at its sender."""
    return _lv_cat(role, identity, name.encode("utf-8"))


def _share(code: str, sid: bytes, scalar: bytes) -> bytes:
    return _x25519(scalar, _generator(code.encode("utf-8"), CHANNEL, sid))


def _keys(sid: bytes, shared: bytes, ya: bytes, ada: bytes, yb: bytes, adb: bytes) -> tuple:
    """(token, Ta, Tb, Td) for one exchange, from the secret the two shares agree. Ta and Tb are
    the draft's tags over each side's own message; Td is the host's receipt."""
    isk = hashlib.sha512(_lv_cat(_DSI + b"_ISK", sid, shared) + _lv_cat(ya, ada) + _lv_cat(yb, adb)).digest()
    mac_key = hashlib.sha512(b"CPaceMac" + sid + isk).digest()

    def tag(message: bytes) -> bytes:
        return hmac.new(mac_key, message, hashlib.sha512).digest()

    return _b64(protocol.hkdf_sha256(isk, sid, TOKEN_INFO)), tag(_lv_cat(ya, ada)), tag(_lv_cat(yb, adb)), tag(_lv_cat(DONE_LABEL))


def _failure(kind: str, pair_id: str, error: str) -> dict:
    return {"type": kind, "pair": pair_id, "ok": False, "error": error}


def _error_of(reply: dict) -> str:
    error = reply.get("error")
    return error if error in _ERRORS else ERROR_REFUSED


def encode(message: dict) -> bytes:
    # The "beamy" key is a wire constant, deliberately kept as the pre-rename name: changing it
    # would break an existing pairing and a mixed-version pair for no gain.
    return json.dumps({"beamy": BEACON_VERSION, **message}, separators=(",", ":")).encode("utf-8")


def decode(data: bytes):
    """The dict a Beamer datagram carries, or None for anything else on the port."""
    if len(data) > MAX_DATAGRAM_BYTES:
        return None
    try:
        message = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        # RecursionError is what a datagram of nothing but brackets raises on some Pythons, and
        # left uncaught it would end the socket loop that called this.
        return None
    if not isinstance(message, dict) or message.get("beamy") != BEACON_VERSION:
        return None
    return message


def beacon_msg(name: str, port: int, pair_id: str = None) -> dict:
    message = {"type": MSG_BEACON, "name": name, "port": int(port), "pairing": PAIRING_VERSION}
    if pair_id:
        message["pair"] = pair_id
    return message


class PairingHost:
    """The PC's half: holds at most one live code and answers the exchange. Pure -- the
    Announcer owns the socket, and the clock and the randomness are injected for the tests and
    the vectors."""

    def __init__(self, clock=time.monotonic, name: str = "", identity: bytes = b"", entropy=secrets.token_bytes):
        self.clock = clock
        self.name = name[:MAX_NAME_CHARS]
        self.identity = identity
        self._entropy = entropy
        self.code = None
        self.pair_id = None
        self.expires_at = 0.0
        # The one pair_start this code answered, as a dict; None until it arrives.
        self._bound = None
        self._last = None
        # True once a pair_answer has just been worked out from the code, until the caller that
        # sends it takes note. A retransmit's answer comes from memory and leaves it False.
        self.fresh_answer = False
        self.paired = None
        # How the last code ended -- "paired", "refused", "version" or "expired" -- for the
        # window to say so; None while a code is live or after a deliberate cancel.
        self.outcome = None

    @property
    def active(self) -> bool:
        if self.code is None:
            return False
        if self.clock() >= self.expires_at:
            self.cancel()
            self.outcome = "expired"
            return False
        return True

    @property
    def seconds_left(self) -> int:
        return max(0, int(self.expires_at - self.clock())) if self.active else 0

    def begin(self) -> str:
        self.code = new_code()
        self.pair_id = self._entropy(16).hex()
        self.expires_at = self.clock() + CODE_LIFETIME_SECONDS
        self._bound = None
        self._last = None
        self.fresh_answer = False
        self.outcome = None
        return self.code

    def cancel(self) -> None:
        self.code = None
        self.pair_id = None
        self.expires_at = 0.0
        self._bound = None
        self.outcome = None

    def handle(self, message: dict):
        """The reply to one datagram, or None when it deserves no answer. A successful pairing
        leaves the token in `self.paired` as (token, requester_name); the caller stores it."""
        kind = message.get("type")
        pair_id = message.get("pair")
        # Every reply echoes the pairing id, so one that is not shaped like an id gets none: a
        # reply is then never much larger than the datagram that drew it.
        if not isinstance(pair_id, str) or not pair_id.isascii() or not 0 < len(pair_id) <= MAX_PAIR_ID_CHARS:
            return None
        if kind == MSG_PAIR_START:
            return self._start(message, pair_id)
        if kind == MSG_PAIR_CONFIRM:
            return self._confirm(message, pair_id)
        if kind == MSG_PAIR_REQUEST:
            return self._outdated(pair_id)
        return None

    def _start(self, message: dict, pair_id: str) -> dict:
        sent = [message.get(field) for field in ("pair", "v", "nonce", "share", "name", "id")]
        if self._last is not None and self._last[0] == sent:
            # A lost reply makes the requester send the same datagram again; answer it the same
            # way, so a retransmit is not read as a second attempt.
            return self._last[1]
        if not self.active or pair_id != self.pair_id:
            return _failure(MSG_PAIR_ANSWER, pair_id, ERROR_NOT_PAIRING)
        if self._bound is not None:
            if self._bound["sent"] == sent:
                return self._bound["answer"]
            # One code answers one share. A second answer would hand whoever sent it a second
            # guess at the code, so it is refused, and the exchange already under way is left
            # alone rather than cancelled by a stranger's datagram.
            return _failure(MSG_PAIR_ANSWER, pair_id, ERROR_REFUSED)
        if message.get("v") != PAIRING_VERSION:
            return self._end(MSG_PAIR_ANSWER, sent, pair_id, ERROR_VERSION, "version")
        try:
            nonce = _unb64(message.get("nonce"))
            share = _unb64(message.get("share"))
            identity = _unb64(message.get("id"))
            name = message.get("name")
            if (
                len(nonce) != NONCE_BYTES
                or len(share) != SHARE_BYTES
                or len(identity) > MAX_IDENTITY_BYTES
                or not isinstance(name, str)
                or len(name) > MAX_NAME_CHARS
            ):
                raise PairingError(ERROR_REFUSED)
            sid = _session_id(pair_id, nonce)
            scalar = self._entropy(SHARE_BYTES)
            requester = _party(ROLE_REQUESTER, identity, name)
            host = _party(ROLE_HOST, self.identity, self.name)
            # Everything that can refuse this datagram is settled before the code is touched:
            # a refusal goes back at once, and must not carry the time the generator took.
            shared = _x25519(scalar, share)
        except (PairingError, UnicodeError):
            return self._end(MSG_PAIR_ANSWER, sent, pair_id, ERROR_REFUSED, "refused")
        try:
            own = _share(self.code, sid, scalar)
        except PairingError:
            return self._end(MSG_PAIR_ANSWER, sent, pair_id, ERROR_REFUSED, "refused")
        token, requester_tag, host_tag, done_tag = _keys(sid, shared, share, requester, own, host)
        self.fresh_answer = True
        answer = {
            "type": MSG_PAIR_ANSWER,
            "pair": pair_id,
            "ok": True,
            "share": _b64(own),
            "tag": _b64(host_tag),
            "name": self.name,
            "id": _b64(self.identity),
        }
        self._bound = {
            "sent": sent,
            "answer": answer,
            "requester_tag": requester_tag,
            "done_tag": done_tag,
            "token": token,
            "name": name,
        }
        return answer

    def _confirm(self, message: dict, pair_id: str) -> dict:
        sent = [pair_id, message.get("tag")]
        if self._last is not None and self._last[0] == sent:
            return self._last[1]
        if not self.active or pair_id != self.pair_id or self._bound is None:
            return _failure(MSG_PAIR_DONE, pair_id, ERROR_NOT_PAIRING)
        bound = self._bound
        try:
            tag = _unb64(message.get("tag"))
        except PairingError:
            tag = b""
        if not hmac.compare_digest(tag, bound["requester_tag"]):
            return self._end(MSG_PAIR_DONE, sent, pair_id, ERROR_REFUSED, "refused")
        self.paired = (bound["token"], bound["name"])
        reply = {"type": MSG_PAIR_DONE, "pair": pair_id, "ok": True, "tag": _b64(bound["done_tag"])}
        self._last = (sent, reply)
        self.cancel()
        self.outcome = "paired"
        return reply

    def _outdated(self, pair_id: str) -> dict:
        """A 1.4.2 Mac's request. It is answered in the datagram that Mac reads, and the code
        goes, because its window then says the PC cancelled it."""
        if self.active and pair_id == self.pair_id and self._bound is None:
            self.cancel()
            self.outcome = "version"
            return _failure(MSG_PAIR_REPLY, pair_id, ERROR_REFUSED)
        return _failure(MSG_PAIR_REPLY, pair_id, ERROR_REFUSED if self.active else ERROR_NOT_PAIRING)

    def _end(self, kind: str, sent: list, pair_id: str, error: str, outcome: str) -> dict:
        reply = _failure(kind, pair_id, error)
        self._last = (sent, reply)
        self.cancel()
        self.outcome = outcome
        return reply


class PairingClient:
    """The Mac's half of one attempt: builds the start, checks the answer, confirms, and yields
    the token once the host says it has stored it."""

    def __init__(self, pair_id: str, code: str, name: str, identity: bytes = b"", entropy=secrets.token_bytes):
        self.pair_id = pair_id
        self.name = name[:MAX_NAME_CHARS]
        self.identity = identity
        self._nonce = entropy(NONCE_BYTES)
        self._sid = _session_id(pair_id, self._nonce)
        self._scalar = entropy(SHARE_BYTES)
        self._share = _share(code, self._sid, self._scalar)
        self._answered = False
        self._token = None
        self._done_tag = None
        # The host's name and identity as the exchange bound them; None until its tag checks out.
        self.host_name = None
        self.host_identity = None

    def start(self) -> dict:
        return {
            "type": MSG_PAIR_START,
            "pair": self.pair_id,
            "v": PAIRING_VERSION,
            "nonce": _b64(self._nonce),
            "share": _b64(self._share),
            "name": self.name,
            "id": _b64(self.identity),
        }

    def accept(self, answer: dict) -> dict:
        """The pair_confirm to send back, or PairingError naming why not. It judges one answer
        and no more: each answer it judged would be another guess at the code for whoever is
        answering."""
        if answer.get("type") != MSG_PAIR_ANSWER or answer.get("pair") != self.pair_id:
            raise PairingError("not a reply to this attempt")
        if self._answered:
            raise PairingError(ERROR_REFUSED)
        self._answered = True
        if answer.get("ok") is not True:
            raise PairingError(_error_of(answer))
        try:
            share = _unb64(answer.get("share"))
            tag = _unb64(answer.get("tag"))
            identity = _unb64(answer.get("id"))
            name = answer.get("name")
            if len(share) != SHARE_BYTES or len(identity) > MAX_IDENTITY_BYTES or not isinstance(name, str) or len(name) > MAX_NAME_CHARS:
                raise PairingError(ERROR_REFUSED)
            requester = _party(ROLE_REQUESTER, self.identity, self.name)
            host = _party(ROLE_HOST, identity, name)
            shared = _x25519(self._scalar, share)
            token, requester_tag, host_tag, done_tag = _keys(self._sid, shared, self._share, requester, share, host)
        except (PairingError, UnicodeError) as exc:
            raise PairingError(ERROR_REFUSED) from exc
        if not hmac.compare_digest(tag, host_tag):
            raise PairingError(ERROR_REFUSED)
        self._token = token
        self._done_tag = done_tag
        self.host_name = name
        self.host_identity = identity
        return {"type": MSG_PAIR_CONFIRM, "pair": self.pair_id, "tag": _b64(requester_tag)}

    def abort(self) -> dict:
        """What to send a host whose answer failed: a confirmation that cannot pass, so the PC
        drops the code and says a wrong one was entered instead of showing it for the rest of
        its minute."""
        return {"type": MSG_PAIR_CONFIRM, "pair": self.pair_id, "tag": ""}

    def finish(self, done: dict) -> str:
        """The agreed token, once the host's pair_done proves it stored the same one."""
        if done.get("type") != MSG_PAIR_DONE or done.get("pair") != self.pair_id or self._token is None:
            raise PairingError("not a reply to this attempt")
        if done.get("ok") is not True:
            raise PairingError(_error_of(done))
        try:
            tag = _unb64(done.get("tag"))
        except PairingError as exc:
            raise PairingError(ERROR_REFUSED) from exc
        if not hmac.compare_digest(tag, self._done_tag):
            raise PairingError(ERROR_REFUSED)
        return self._token


def _udp_socket(bind_port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", bind_port))
    sock.settimeout(0.5)
    return sock


def local_address_towards(host: str) -> str:
    """This machine's address on the interface that reaches `host`; nothing is sent."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((host, 9))
        return probe.getsockname()[0]
    finally:
        probe.close()


def broadcast_targets(port: int) -> list:
    """Every address worth sending a beacon to, one per local IPv4 interface plus the limited
    broadcast as a backstop.

    ⚠ 255.255.255.255 alone is not enough and this is not theoretical: it leaves by the default
    route only, so a PC with a second interface — a Hyper-V vEthernet switch is the common case —
    announces on whichever the routing table prefers and may never reach the LAN the Mac is on.
    Seen in practice: the PC beaconed steadily while no beacon reached the Mac at all, and the Mac
    then paired against a stale id from an older beacon that had got through, which is what
    produced `not_pairing`.

    The per-interface directed broadcast assumes a /24, because the stdlib exposes no netmask.
    A wrong guess costs one datagram that goes nowhere; the limited broadcast is still sent, so
    this can only add delivery, never remove it."""
    targets = [("255.255.255.255", port)]
    seen = {"255.255.255.255"}
    for address in _local_ipv4_addresses():
        parts = address.split(".")
        if len(parts) != 4:
            continue
        directed = ".".join(parts[:3] + ["255"])
        if directed not in seen:
            seen.add(directed)
            targets.append((directed, port))
    return targets


def _local_ipv4_addresses() -> list:
    found = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if not address.startswith("127.") and address not in found:
                found.append(address)
    except OSError:
        pass
    return found


def machine_name() -> str:
    return socket.gethostname().split(".", 1)[0] or "This machine"


class Announcer:
    """The PC's socket loop: broadcasts the beacon and answers pairing requests on one socket.
    `port_getter` returns the current TCP port, so a changed listen port is announced without a
    restart. `on_paired(token, mac_name, mac_address)` is called on this thread."""

    def __init__(self, port_getter, on_paired, logger=None, bind_port=PAIRING_PORT, announce_to=("255.255.255.255", PAIRING_PORT), name=None):
        self.port_getter = port_getter
        self.on_paired = on_paired
        self.logger = logger or logging.getLogger("Beamer")
        self.bind_port = bind_port
        self.announce_to = announce_to
        self.name = name or machine_name()
        self.host = PairingHost(name=self.name)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.error = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="Beamer-announcer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def begin_pairing(self) -> str:
        with self._lock:
            return self.host.begin()

    def cancel_pairing(self) -> None:
        with self._lock:
            self.host.cancel()

    @property
    def code(self):
        with self._lock:
            return self.host.code if self.host.active else None

    @property
    def seconds_left(self) -> int:
        with self._lock:
            return self.host.seconds_left

    @property
    def outcome(self):
        with self._lock:
            self.host.active
            return self.host.outcome

    def _beacon(self) -> bytes:
        with self._lock:
            pair_id = self.host.pair_id if self.host.active else None
        return encode(beacon_msg(self.name, self.port_getter(), pair_id))

    @staticmethod
    def _due(last: dict, host: str, now: float) -> bool:
        """Whether `host` may be sent another datagram of a rationed kind, noting it if so."""
        interval = BEACON_INTERVAL_SECONDS / 4
        if now - last.get(host, -BEACON_INTERVAL_SECONDS) < interval:
            return False
        if len(last) > 256:
            # Only what has lapsed is forgotten. Emptying the table would let a burst from
            # made-up addresses wipe the record of the one being aimed at.
            for lapsed in [known for known, sent in last.items() if now - sent >= interval]:
                del last[lapsed]
            if len(last) > 256:
                return False
        last[host] = now
        return True

    def _run(self) -> None:
        try:
            sock = _udp_socket(self.bind_port)
        except OSError as exc:
            self.error = str(exc)
            self.logger.error("pairing beacon could not bind UDP %s: %s", self.bind_port, exc)
            return
        self.error = None
        next_beacon = 0.0
        answered = {}
        refused = {}
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_beacon:
                    next_beacon = now + BEACON_INTERVAL_SECONDS
                    beacon = self._beacon()
                    delivered = 0
                    # Only a caller asking for the limited broadcast wants the per-interface
                    # fan-out; a named address is honoured exactly, which keeps the tests off
                    # the real network.
                    if self.announce_to[0] == "255.255.255.255":
                        targets = broadcast_targets(self.announce_to[1])
                    else:
                        targets = [self.announce_to]
                    for target in targets:
                        try:
                            sock.sendto(beacon, target)
                            delivered += 1
                        except OSError:
                            continue
                    if not delivered:
                        self.logger.warning("beacon reached no interface")
                try:
                    data, address = sock.recvfrom(MAX_DATAGRAM_BYTES + 1)
                except socket.timeout:
                    continue
                except ConnectionResetError:
                    # Windows reports an ICMP port-unreachable for a datagram this
                    # socket sent as a reset on the next receive. The socket is
                    # fine; the peer that was not listening is the Mac's problem.
                    continue
                except OSError as exc:
                    if self._stop.is_set():
                        break
                    self.error = str(exc)
                    self.logger.error("pairing beacon stopped: %s", exc)
                    break
                arrived = time.monotonic()
                message = decode(data)
                if message is None:
                    continue
                if message.get("type") == MSG_FIND:
                    # At most one answer per address per interval, so the port is no amplifier.
                    if self._due(answered, address[0], arrived):
                        try:
                            sock.sendto(self._beacon(), address)
                        except OSError as exc:
                            self.logger.warning("beacon to %s not sent: %s", address[0], exc)
                    continue
                with self._lock:
                    try:
                        reply = self.host.handle(message)
                    except Exception:
                        # Whatever one datagram does, the next beacon still goes out.
                        self.logger.exception("pairing datagram dropped")
                        reply = None
                    paired, self.host.paired = self.host.paired, None
                    fresh, self.host.fresh_answer = self.host.fresh_answer, False
                # A refusal is larger than the smallest datagram that draws one, so it is rationed
                # the same way. A requester that misses one asks again after its own timeout.
                if reply is not None and (reply.get("ok") is True or self._due(refused, address[0], arrived)):
                    if fresh:
                        # Only the answer just worked out from the code is held back. A
                        # retransmit's comes from memory, and holding each of those would let a
                        # stream of replays stall this loop.
                        self._stop.wait(max(0.0, arrived + PAIR_ANSWER_SECONDS - time.monotonic()))
                    try:
                        sock.sendto(encode(reply), address)
                    except OSError as exc:
                        self.logger.warning("pairing reply not sent: %s", exc)
                if paired is not None:
                    self.logger.info("paired with %s at %s", paired[1] or "a Mac", address[0])
                    try:
                        self.on_paired(paired[0], paired[1], address[0])
                    except Exception:
                        self.logger.exception("on_paired failed")
        except Exception as exc:
            self.error = str(exc)
            self.logger.exception("pairing beacon stopped")
        finally:
            sock.close()


class Discovery:
    """The Mac's socket loop: listens for beacons and carries pairing attempts on the same
    socket, so a reply comes back to the port the PC heard from."""

    def __init__(self, logger=None, bind_port=PAIRING_PORT, clock=time.monotonic):
        self.logger = logger or logging.getLogger("Beamer")
        self.bind_port = bind_port
        self.clock = clock
        self._lock = threading.Lock()
        self._seen = {}
        self._replies = queue.Queue(maxsize=64)
        self._awaiting = None
        self._pairing = threading.Lock()
        # Codes an answer has failed under, each with the time it may be tried again.
        self._failed = {}
        self._stop = threading.Event()
        self._thread = None
        self._sock = None
        self._finding = None
        self._find_serial = 0
        self._next_find = 0.0
        self.error = None

    def find(self, host: str, port: int = PAIRING_PORT) -> None:
        """Ask `host` for its beacon, for a PC no broadcast reaches, and keep asking while
        discovery runs so it stays listed. Returns at once; the PC appears in pcs() when it
        answers. An empty host stops asking."""
        host = (host or "").strip()
        with self._lock:
            self._find_serial += 1
            serial = self._find_serial
            self._finding = None
            self._next_find = 0.0
        if host:
            # A name can take seconds to resolve, and this loop also carries the pairing replies.
            threading.Thread(target=self._resolve, args=(host, int(port), serial),
                             name="Beamer-find", daemon=True).start()

    def _resolve(self, host: str, port: int, serial: int) -> None:
        try:
            address = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)[0][4][0]
        except (OSError, IndexError) as exc:
            self.logger.warning("could not resolve %s: %s", host, exc)
            return
        with self._lock:
            if serial == self._find_serial:
                self._finding = (address, port)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="Beamer-discovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def pcs(self) -> list:
        """Every PC heard from recently: dicts of name, address, port, reply_port, pair_id and
        pairing, the version of the exchange it speaks."""
        cutoff = self.clock() - BEACON_STALE_SECONDS
        with self._lock:
            stale = [key for key, entry in self._seen.items() if entry["seen_at"] < cutoff]
            for key in stale:
                del self._seen[key]
            return sorted(
                ({k: v for k, v in entry.items() if k != "seen_at"} for entry in self._seen.values()),
                key=lambda entry: (entry["name"].lower(), entry["address"]),
            )

    def pair(self, pc: dict, code: str, name: str = None) -> tuple:
        """Runs the exchange with `pc` (an entry from pcs()) and returns (token, host_name),
        the name being the one the exchange proved. Blocks for up to PAIR_ATTEMPTS replies'
        worth, once for the answer and once for the receipt; call it off the main thread."""
        sock = self._sock
        if sock is None:
            raise PairingError("discovery is not running")
        if pc.get("pairing") != PAIRING_VERSION:
            # Said here, before anything is sent: there is no older exchange to fall back to.
            raise PairingError(ERROR_VERSION)
        if not isinstance(pc.get("pair_id"), str):
            raise PairingError(ERROR_NOT_PAIRING)
        target = (pc["address"], pc["reply_port"])
        if not self._pairing.acquire(blocking=False):
            # One attempt at a time: a second would take the first one's replies, and could
            # slip past the record of failed codes below.
            raise PairingError("busy")
        try:
            now = self.clock()
            self._failed = {failed: until for failed, until in self._failed.items() if until > now}
            if code in self._failed:
                # An answer to this code was judged and failed, so whoever answered has had a
                # guess at it. The real host has bound the code to that attempt or dropped it;
                # sending another share would pair with nobody and only buy the answerer a
                # second guess. It is the code that is remembered, not the pairing id, which a
                # beacon can forge.
                raise PairingError(ERROR_REFUSED)
            client = PairingClient(pc["pair_id"], code, name or machine_name())
            while True:
                try:
                    self._replies.get_nowait()
                except queue.Empty:
                    break
            self._awaiting = (target[0], client.pair_id)
            answer = self._ask(sock, target, client.pair_id, client.start(), MSG_PAIR_ANSWER)
            try:
                confirm = client.accept(answer)
            except PairingError:
                # Only an answer that claimed to be good can have tested a guess. A refusal
                # tests nothing, and a stale pairing id draws one from an honest PC.
                if answer.get("ok") is True:
                    self._failed[code] = self.clock() + FAILED_CODE_SECONDS
                    try:
                        sock.sendto(encode(client.abort()), target)
                    except OSError:
                        pass
                raise
            token = client.finish(self._ask(sock, target, client.pair_id, confirm, MSG_PAIR_DONE))
            return token, client.host_name
        finally:
            self._awaiting = None
            self._pairing.release()

    def _ask(self, sock, target, pair_id: str, message: dict, reply_type: str) -> dict:
        """Sends one datagram until the first reply of `reply_type` from `target` comes back.
        The first one decides: the caller judges it and never waits for a better one."""
        datagram = encode(message)
        for _attempt in range(PAIR_ATTEMPTS):
            try:
                sock.sendto(datagram, target)
            except OSError as exc:
                raise PairingError("no_answer") from exc
            deadline = self.clock() + PAIR_REPLY_TIMEOUT_SECONDS
            while True:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    break
                try:
                    reply, address = self._replies.get(timeout=remaining)
                except queue.Empty:
                    break
                if address[0] != target[0] or reply.get("pair") != pair_id or reply.get("type") != reply_type:
                    continue
                return reply
        raise PairingError("no_answer")

    def _run(self) -> None:
        try:
            sock = _udp_socket(self.bind_port)
        except OSError as exc:
            self.error = str(exc)
            self.logger.error("discovery could not bind UDP %s: %s", self.bind_port, exc)
            return
        self.error = None
        self._sock = sock
        try:
            while not self._stop.is_set():
                self._send_find(sock)
                try:
                    data, address = sock.recvfrom(MAX_DATAGRAM_BYTES + 1)
                except socket.timeout:
                    continue
                except ConnectionResetError:
                    continue
                except OSError as exc:
                    if self._stop.is_set():
                        break
                    self.error = str(exc)
                    self.logger.error("discovery stopped: %s", exc)
                    break
                message = decode(data)
                if message is None:
                    continue
                kind = message.get("type")
                if kind == MSG_BEACON:
                    try:
                        self._note_beacon(message, address)
                    except Exception:
                        self.logger.exception("beacon dropped")
                elif kind in (MSG_PAIR_ANSWER, MSG_PAIR_DONE) and self._awaiting == (address[0], message.get("pair")):
                    # Only what the attempt under way is waiting for is kept, and only so much
                    # of it, so nothing a stranger sends piles up in memory.
                    try:
                        self._replies.put_nowait((message, address))
                    except queue.Full:
                        pass
        except Exception as exc:
            self.error = str(exc)
            self.logger.exception("discovery stopped")
        finally:
            self._sock = None
            sock.close()

    def _send_find(self, sock) -> None:
        with self._lock:
            target = self._finding
            if target is None or self.clock() < self._next_find:
                return
            self._next_find = self.clock() + BEACON_INTERVAL_SECONDS
        try:
            sock.sendto(encode({"type": MSG_FIND}), target)
        except OSError as exc:
            self.logger.warning("could not ask %s for its beacon: %s", target[0], exc)

    def _note_beacon(self, message: dict, address) -> None:
        name = message.get("name")
        port = message.get("port")
        pair_id = message.get("pair")
        version = message.get("pairing")
        if not isinstance(name, str) or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            return
        try:
            name.encode("utf-8")
        except UnicodeError:
            # Half a surrogate pair is JSON a parser accepts and text no window can show.
            return
        entry = {
            "name": name[:64] or address[0],
            "address": address[0],
            "port": port,
            "reply_port": address[1],
            "pair_id": pair_id if isinstance(pair_id, str) and pair_id.isascii() and 0 < len(pair_id) <= MAX_PAIR_ID_CHARS else None,
            # A beacon from 1.4.2 or earlier carries no number: that is version 1.
            "pairing": version if isinstance(version, int) and not isinstance(version, bool) else 1,
            "seen_at": self.clock(),
        }
        with self._lock:
            if address[0] not in self._seen and len(self._seen) >= MAX_SEEN_HOSTS:
                # The list is only read while the window is open, so what has gone quiet is
                # cleared here too. Beacons from made-up addresses then cost a fixed amount of
                # memory and no more.
                cutoff = self.clock() - BEACON_STALE_SECONDS
                for stale in [key for key, known in self._seen.items() if known["seen_at"] < cutoff]:
                    del self._seen[stale]
                if len(self._seen) >= MAX_SEEN_HOSTS:
                    # Still full: whoever has been quiet longest gives way. A flood of forgeries
                    # can crowd real PCs off the list that way, which is a denial like any
                    # other here, so the one address the user typed is never the one to go.
                    wanted = self._finding[0] if self._finding else None
                    del self._seen[min((key for key in self._seen if key != wanted), key=lambda key: self._seen[key]["seen_at"])]
            self._seen[address[0]] = entry
