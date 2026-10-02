"""LAN discovery and pairing, shared by both apps.

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

Pairing version 3 (1.5.0, WIRE.md section 6) sits beside version 2 until the apps' windows move to
it (milestone 2, steps 6a and 6b), when PairingHost's and PairingClient's version 2 path,
Announcer and Discovery's own pair() go. In version 3 each side's identity is its 16-byte machine
id, platform and port are bound in beside the name, a machine already paired is refused as
`known`, the host writes the peer entry before pair_done so Td proves it saved the token, and a
code shown as a QR carries a second secret. PairingService is version 3's socket side: every
desktop announces and discovers on one UDP socket, and hosts over UDP and TCP with one
PairingHost, so a code answers one pair_start whichever way it came.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import ctypes.util
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import queue
import secrets
import socket
import struct
import sys
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
# How many replies already waiting are read for one from the address asked before a held one decides.
MAX_HELD_SWEEP = 64
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
# Version 3's: the other side's id is already a peer's or this machine's own, and the host
# reached MAX_PEERS while its code was up.
ERROR_KNOWN = "known"
ERROR_FULL = "full"
_ERRORS = (ERROR_NOT_PAIRING, ERROR_REFUSED, ERROR_VERSION, ERROR_KNOWN, ERROR_FULL)
# What read_qr raises for any text that is not exactly the pairing QR's.
ERROR_BAD_QR = "bad_qr"

PAIRING_V3 = 3
CHANNEL_V3 = b"beamer-pair-v3"
TOKEN_INFO_V3 = b"beamer-pair-token-v3"
ID_BYTES = 16
QR_SECRET_BYTES = 16
MAX_PEERS = 32
MAX_PLATFORM_CHARS = 16
_PLATFORM_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")
# A peer entry that never linked and was made this recently, under the name and platform the new
# pairing proves, is taken to be that pairing's lost pair_done and replaced rather than refused.
REPLACE_UNLINKED_SECONDS = 600
# Pairing over TCP, for a host no broadcast reaches and for the phones. The framing's limit is
# the datagram's, so a message means the same on either transport.
TCP_EXCHANGE_SECONDS = 10.0
TCP_CONNECT_SECONDS = 3.0
TCP_MAX_CONNECTIONS = 4
QR_PREFIX = "beamer://pair?"
_QR_KEYS = ("host", "port", "code", "k")
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))

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


class AlreadyPaired(PairingError):
    """Version 3's `known`: the other machine's id is one of this machine's peers', or its own.
    `entry` is that peer's entry, for the window to name it and when it was paired; None when the
    id was this machine's own."""

    def __init__(self, entry):
        super().__init__(ERROR_KNOWN)
        self.entry = entry


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


def _party(role: bytes, identity: bytes, name: str, platform: str = None, port: int = None) -> bytes:
    """One side's associated data: CPace's ADa or ADb. The role comes first, so the two sides'
    can never be equal, which is what stops a tag being reflected back at its sender. Version 3
    adds the platform and the link port, so the exchange proves everything pairing saves about
    the other machine but its address."""
    if platform is None:
        return _lv_cat(role, identity, name.encode("utf-8"))
    return _lv_cat(role, identity, name.encode("utf-8"), platform.encode("ascii"), port.to_bytes(2, "big"))


def _share(code: str, sid: bytes, scalar: bytes, channel: bytes = CHANNEL, secret: bytes = b"") -> bytes:
    # A code read from a QR is followed by the QR's secret, 22 bytes of password in all; the
    # generator's padding follows its length.
    return _x25519(scalar, _generator(code.encode("utf-8") + secret, channel, sid))


def _keys(sid: bytes, shared: bytes, ya: bytes, ada: bytes, yb: bytes, adb: bytes, token_info: bytes = TOKEN_INFO) -> tuple:
    """(token, Ta, Tb, Td) for one exchange, from the secret the two shares agree. Ta and Tb are
    the draft's tags over each side's own message; Td is the host's receipt."""
    isk = hashlib.sha512(_lv_cat(_DSI + b"_ISK", sid, shared) + _lv_cat(ya, ada) + _lv_cat(yb, adb)).digest()
    mac_key = hashlib.sha512(b"CPaceMac" + sid + isk).digest()

    def tag(message: bytes) -> bytes:
        return hmac.new(mac_key, message, hashlib.sha512).digest()

    return _b64(protocol.hkdf_sha256(isk, sid, token_info)), tag(_lv_cat(ya, ada)), tag(_lv_cat(yb, adb)), tag(_lv_cat(DONE_LABEL))


def _is_int(value) -> bool:
    # JSON's 1.0 and true both compare equal to 1 in Python; an integer field takes neither.
    return type(value) is int


def _platform_ok(platform) -> bool:
    return isinstance(platform, str) and 0 < len(platform) <= MAX_PLATFORM_CHARS and set(platform) <= _PLATFORM_CHARS


def _port_ok(port) -> bool:
    return _is_int(port) and 0 <= port <= 65535


def _name_ok(name) -> bool:
    if not isinstance(name, str) or not 0 < len(name) <= MAX_NAME_CHARS:
        return False
    try:
        name.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _machine_id_ok(identity: bytes) -> bool:
    return len(identity) == ID_BYTES and any(identity)


def _check_own(identity: bytes, platform: str, port: int) -> None:
    if not _machine_id_ok(identity) or not _platform_ok(platform) or not _port_ok(port):
        raise ValueError("a version 3 side needs its 16-byte machine id, its platform and its link port")


def _key_id(token):
    """The link's key id (WIRE.md section 2), or None for a typed token, which has none."""
    return protocol.key_id(token) if protocol.is_paired_token(token) else None


def peer_entry(identity: bytes, name: str, platform: str, port: int, token: str, host: str, paired_at: int) -> dict:
    """The peer entry pairing saves (WIRE.md sections 1 and 6). A machine that gives port 0 is a
    phone: it is never dialled and this machine's input never goes to it."""
    phone = port == 0
    return {
        "id": _b64(identity), "name": name, "platform": platform, "token": token,
        "host": "" if phone else host, "port": port, "hw": "", "send": not phone, "allow_drive": True,
        "in_use": True,
        "side": "", "side_set_at": 0, "side_by": "", "paired_with": [], "paired_at": int(paired_at),
        "linked": False, "from_1_4": False,
    }


def _find_known(peers: list, own: bytes, theirs: bytes, name: str, platform: str, now: float) -> tuple:
    """(refused, replaced) for a pairing that proved `theirs`, `name` and `platform`. `refused` is
    True when the id is this machine's own and otherwise the entry already holding it, None when
    there is none; `replaced` is the entry this pairing replaces: the one left by a lost pair_done,
    or one migrated from 1.4.x that an earlier beta linked and so knows the id of. A migrated entry
    never links, so replacing it takes nothing that works."""
    if theirs == own:
        return True, None
    wanted = _b64(theirs)
    for known in peers:
        if known.get("id") != wanted:
            continue
        if known.get("from_1_4") is True:
            return None, known
        paired_at = known.get("paired_at")
        lost_done = (
            known.get("linked") is False
            and _is_int(paired_at) and paired_at > 0 and 0 <= now - paired_at < REPLACE_UNLINKED_SECONDS
            and known.get("name") == name and known.get("platform") == platform
        )
        return (None, known) if lost_done else (known, None)
    return None, None


def _migrated_for(peers: list, address: str, name: str, platform: str):
    """The entry migrated from 1.4.x (`id: ""`) that a fresh pairing with the machine at `address`,
    proving `name` and `platform`, replaces, or None: its saved host is `address`, or (its address
    having changed) its name and platform are the ones proved. A name is anyone's to claim, which
    is safe only because a migrated entry never links and a match by name carries nothing over
    (peerlist.add_peer gives its side and zones only to the machine at its host on its platform):
    taking it loses nothing that works, and left, it would ask to be paired again beside this pairing."""
    for known in peers:
        if known.get("id") == "" and known.get("from_1_4") is True and (
                (address and known.get("host") == address)
                or (known.get("name") == name and known.get("platform") == platform)):
            return known
    return None


def _clashes(peers: list, token: str, replaced) -> bool:
    """Whether the new token's key id is already an entry's, bar the entry it replaces. With a
    fresh 256-bit token that never happens; the check keeps key ids unique whatever the entries
    hold."""
    new = _key_id(token)
    skip = replaced.get("id") if replaced else None
    return any(_key_id(known.get("token")) == new for known in peers if skip is None or known.get("id") != skip)


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


def qr_text(host: str, port: int, code: str, secret: bytes) -> str:
    """What the pairing sheet's QR code holds (WIRE.md section 6). The sheet never shows `k` as
    text."""
    return f"{QR_PREFIX}host={host}&port={port}&code={code}&k={_b64(secret)}"


def _decimal(text: str) -> bool:
    # str.isdigit() also takes full-width and superscript digits; the QR takes ASCII only, and
    # no leading zero, so a number has one spelling.
    return text.isascii() and text.isdigit() and (text == "0" or not text.startswith("0"))


def read_qr(text) -> dict:
    """The host, port, code and secret a pairing QR carries, or PairingError(ERROR_BAD_QR) for
    any other text: the four keys in order, lower case, each once, and nothing else."""
    if not isinstance(text, str) or not text.startswith(QR_PREFIX):
        raise PairingError(ERROR_BAD_QR)
    fields = [field.split("=", 1) for field in text[len(QR_PREFIX):].split("&")]
    if [field[0] for field in fields] != list(_QR_KEYS) or any(len(field) != 2 for field in fields):
        raise PairingError(ERROR_BAD_QR)
    host, port, code, k = (field[1] for field in fields)
    octets = host.split(".")
    if len(octets) != 4 or not all(_decimal(octet) and int(octet) <= 255 for octet in octets):
        raise PairingError(ERROR_BAD_QR)
    if not _decimal(port) or not 0 < int(port) <= 65535:
        raise PairingError(ERROR_BAD_QR)
    if len(code) != CODE_DIGITS or not (code.isascii() and code.isdigit()):
        raise PairingError(ERROR_BAD_QR)
    try:
        secret = _unb64(k)
    except PairingError as exc:
        raise PairingError(ERROR_BAD_QR) from exc
    if len(secret) != QR_SECRET_BYTES:
        raise PairingError(ERROR_BAD_QR)
    return {"host": host, "port": int(port), "code": code, "secret": secret}


def _private(address: str) -> bool:
    try:
        return any(ipaddress.ip_address(address) in network for network in _PRIVATE_NETWORKS)
    except ValueError:
        return False


def pairing_address():
    """The address the pairing sheet shows and the QR carries: this machine's address in a
    private range, preferring the default route's interface, else the default route's own
    address. None when the machine has no IPv4 address at all."""
    try:
        # A TEST-NET address: nothing is sent, and only the default route reaches it.
        default = local_address_towards("192.0.2.1")
    except OSError:
        default = None
    if default and _private(default):
        return default
    return next((address for address in _local_ipv4_addresses() if _private(address)), default)


class PairingHost:
    """The host's half: holds at most one live code and answers the exchange. Pure -- the
    Announcer or PairingService owns the socket, and the clock and the randomness are injected
    for the tests and the vectors.

    Version 3 also takes this machine's platform and link port, `peers` (a callable giving the
    peer entries as they stand) and `store(entry, replaced)`, which writes the new entry, and
    drops `replaced` when it is not None, before pair_done is sent, raising if the write fails.
    `wall` gives unix seconds for `paired_at`."""

    def __init__(self, clock=time.monotonic, name: str = "", identity: bytes = b"", entropy=secrets.token_bytes,
                 *, version: int = PAIRING_VERSION, platform: str = "", port: int = 0, peers=None, store=None, wall=time.time):
        if version == PAIRING_V3:
            _check_own(identity, platform, port)
        self.clock = clock
        self.name = name[:MAX_NAME_CHARS]
        self.identity = identity
        self._entropy = entropy
        self.version = version
        self.platform = platform
        self.port = port
        self._peers = peers or list
        self.store = store
        self.wall = wall
        self.code = None
        self.pair_id = None
        self.expires_at = 0.0
        # Version 3: the 16 bytes a QR carries beside the code, drawn with it and dropped with it.
        self.qr_secret = None
        # The one pair_start this code answered, as a dict; None until it arrives.
        self._bound = None
        self._last = None
        # True once a pair_answer has just been worked out from the code, until the caller that
        # sends it takes note. A retransmit's answer comes from memory and leaves it False.
        self.fresh_answer = False
        # The pairing just made: (token, requester_name) in version 2, the stored entry in 3.
        self.paired = None
        # How the last code ended -- "paired", "refused", "version" or "expired", and in version
        # 3 "known" (this machine has the requester already), "known_there" (the requester has
        # this one), "full" and "not_saved" -- for the window to say so; None while a code is
        # live or after a deliberate cancel.
        self.outcome = None
        # The entry that made the last code end "known", for the window to name; None when the
        # requester claimed this machine's own id.
        self.known = None

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

    @property
    def v3(self) -> bool:
        return self.version == PAIRING_V3

    def begin(self) -> str:
        if self.v3 and len(self._peers()) >= MAX_PEERS:
            # A host with 32 peers shows no code: the pairing could not be kept.
            raise PairingError(ERROR_FULL)
        self.code = new_code()
        self.pair_id = self._entropy(16).hex()
        self.qr_secret = self._entropy(QR_SECRET_BYTES) if self.v3 else None
        self.expires_at = self.clock() + CODE_LIFETIME_SECONDS
        self._bound = None
        self._last = None
        self.fresh_answer = False
        self.outcome = None
        self.known = None
        return self.code

    def cancel(self) -> None:
        self.code = None
        self.pair_id = None
        self.qr_secret = None
        self.expires_at = 0.0
        self._bound = None
        self.outcome = None

    def beacon(self, port: int) -> dict:
        """Version 3's beacon. The pairing id and this machine's id travel only while a code is
        up, so a machine does not broadcast a stable id on every network it joins."""
        message = {"type": MSG_BEACON, "name": self.name, "port": int(port), "pairing": PAIRING_V3, "platform": self.platform}
        if self.active:
            message["pair"] = self.pair_id
            message["id"] = _b64(self.identity)
        return message

    def handle(self, message: dict, address: str = "", via=None):
        """The reply to one message, or None when it deserves no answer. A successful pairing
        leaves `self.paired` set: (token, requester_name) in version 2, for the caller to store;
        in version 3 the entry `store` has already written. `address` is where the pair_start
        came from, which version 3 saves as the requester's host. `via` is the transport it came
        on (a TCP connection, or the UDP socket): version 3 takes a pair_confirm only on the channel
        whose pair_start bound the code."""
        kind = message.get("type")
        pair_id = message.get("pair")
        # Every reply echoes the pairing id, so one that is not shaped like an id gets none: a
        # reply is then never much larger than the datagram that drew it.
        if not isinstance(pair_id, str) or not pair_id.isascii() or not 0 < len(pair_id) <= MAX_PAIR_ID_CHARS:
            return None
        if kind == MSG_PAIR_START:
            return self._start(message, pair_id, address, via)
        if kind == MSG_PAIR_CONFIRM:
            return self._confirm(message, pair_id, address, via)
        if kind == MSG_PAIR_REQUEST:
            return self._outdated(pair_id)
        return None

    def _start(self, message: dict, pair_id: str, address: str, via=None) -> dict:
        fields = ("pair", "v", "nonce", "share", "name", "id") + (("platform", "port", "qr") if self.v3 else ())
        # "qr" absent and "qr": null are different messages; the sentinel keeps them apart.
        sent = [message.get(field, KeyError) for field in fields]
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
        version = message.get("v")
        if not _is_int(version) or version != self.version:
            return self._end(MSG_PAIR_ANSWER, sent, pair_id, ERROR_VERSION, "version")
        if self.v3 and len(self._peers()) >= MAX_PEERS:
            return self._end(MSG_PAIR_ANSWER, sent, pair_id, ERROR_FULL, "full")
        try:
            nonce = _unb64(message.get("nonce"))
            share = _unb64(message.get("share"))
            identity = _unb64(message.get("id"))
            name = message.get("name")
            platform = message.get("platform")
            port = message.get("port")
            secret = b""
            if (
                len(nonce) != NONCE_BYTES
                or len(share) != SHARE_BYTES
                or len(identity) > MAX_IDENTITY_BYTES
                or not isinstance(name, str)
                or len(name) > MAX_NAME_CHARS
            ):
                raise PairingError(ERROR_REFUSED)
            if self.v3:
                if not (_machine_id_ok(identity) and _name_ok(name) and _platform_ok(platform) and _port_ok(port)):
                    raise PairingError(ERROR_REFUSED)
                if "qr" in message:
                    if not _is_int(message["qr"]) or message["qr"] != 1:
                        raise PairingError(ERROR_REFUSED)
                    secret = self.qr_secret
                requester = _party(ROLE_REQUESTER, identity, name, platform, port)
                host = _party(ROLE_HOST, self.identity, self.name, self.platform, self.port)
            else:
                requester = _party(ROLE_REQUESTER, identity, name)
                host = _party(ROLE_HOST, self.identity, self.name)
            sid = _session_id(pair_id, nonce)
            scalar = self._entropy(SHARE_BYTES)
            # Everything that can refuse this datagram is settled before the code is touched:
            # a refusal goes back at once, and must not carry the time the generator took.
            shared = _x25519(scalar, share)
        except (PairingError, UnicodeError):
            return self._end(MSG_PAIR_ANSWER, sent, pair_id, ERROR_REFUSED, "refused")
        try:
            own = _share(self.code, sid, scalar, CHANNEL_V3 if self.v3 else CHANNEL, secret)
        except PairingError:
            return self._end(MSG_PAIR_ANSWER, sent, pair_id, ERROR_REFUSED, "refused")
        token, requester_tag, host_tag, done_tag = _keys(sid, shared, share, requester, own, host, TOKEN_INFO_V3 if self.v3 else TOKEN_INFO)
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
        if self.v3:
            answer["platform"] = self.platform
            answer["port"] = self.port
        self._bound = {
            "sent": sent,
            "answer": answer,
            "requester_tag": requester_tag,
            "done_tag": done_tag,
            "token": token,
            "name": name,
            "identity": identity,
            "platform": platform,
            "port": port,
            "address": address,
            "via": via,
        }
        return answer

    def _confirm(self, message: dict, pair_id: str, address: str, via=None) -> dict:
        sent = [pair_id, message.get("tag"), message.get("error")]
        if self._last is not None and self._last[0] == sent:
            return self._last[1]
        if not self.active or pair_id != self.pair_id or self._bound is None:
            return _failure(MSG_PAIR_DONE, pair_id, ERROR_NOT_PAIRING)
        bound = self._bound
        if self.v3 and (address != bound["address"] or via != bound["via"]):
            # Only the address and transport whose pair_start bound the code may end it. A
            # stranger's confirmation, a forged-source datagram or a second connection included, is
            # not answered and leaves the exchange alone.
            return None
        try:
            tag = _unb64(message.get("tag"))
        except PairingError:
            tag = b""
        if not hmac.compare_digest(tag, bound["requester_tag"]):
            # An `error: known` is unauthenticated, like every refusal: the window only says the
            # other machine may already have this one paired.
            there = self.v3 and message.get("error") == ERROR_KNOWN
            return self._end(MSG_PAIR_DONE, sent, pair_id, ERROR_REFUSED, "known_there" if there else "refused")
        if self.v3:
            return self._keep(bound, sent, pair_id)
        self.paired = (bound["token"], bound["name"])
        return self._done(sent, pair_id, bound)

    def _keep(self, bound: dict, sent: list, pair_id: str) -> dict:
        """Version 3, once Ta has proved the requester's id: refuse a machine already paired,
        keep key ids unique, and write the entry before pair_done says it was written."""
        peers = self._peers()
        known, replaced = _find_known(peers, self.identity, bound["identity"], bound["name"], bound["platform"], self.wall())
        if known is not None:
            reply = self._end(MSG_PAIR_DONE, sent, pair_id, ERROR_KNOWN, "known")
            self.known = None if known is True else known
            return reply
        replaced = replaced or _migrated_for(peers, bound["address"], bound["name"], bound["platform"])
        if _clashes(peers, bound["token"], replaced):
            return self._end(MSG_PAIR_DONE, sent, pair_id, ERROR_REFUSED, "refused")
        if replaced is None and len(peers) >= MAX_PEERS:
            # Filled by a pairing this machine requested while the code was up.
            return self._end(MSG_PAIR_DONE, sent, pair_id, ERROR_REFUSED, "full")
        requester_port = bound["port"]
        entry = peer_entry(bound["identity"], bound["name"], bound["platform"], requester_port, bound["token"], bound["address"], self.wall())
        try:
            self.store(entry, replaced)
        except Exception as exc:
            logging.getLogger("Beamer").warning("the new peer entry could not be saved: %s", exc)
            return self._end(MSG_PAIR_DONE, sent, pair_id, ERROR_REFUSED, "not_saved")
        self.paired = entry
        return self._done(sent, pair_id, bound)

    def _done(self, sent: list, pair_id: str, bound: dict) -> dict:
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
    """The requester's half of one attempt: builds the start, checks the answer, confirms, and
    yields the token once the host says it has stored it.

    Version 3 also takes this machine's platform and link port, `peers` (a callable giving the
    peer entries), `wall` for `paired_at`, and `qr_secret` when the code was read from a QR. It
    raises PairingError(ERROR_FULL) at once when this machine already has MAX_PEERS peers."""

    def __init__(self, pair_id: str, code: str, name: str, identity: bytes = b"", entropy=secrets.token_bytes,
                 *, version: int = PAIRING_VERSION, platform: str = "", port: int = 0, qr_secret: bytes = None, peers=None, wall=time.time):
        self.version = version
        self._peers = peers or list
        self.wall = wall
        if self.v3:
            _check_own(identity, platform, port)
            if qr_secret is not None and len(qr_secret) != QR_SECRET_BYTES:
                raise ValueError("a QR secret is 16 bytes")
            if len(self._peers()) >= MAX_PEERS:
                raise PairingError(ERROR_FULL)
        self.pair_id = pair_id
        self.name = name[:MAX_NAME_CHARS]
        self.identity = identity
        self.platform = platform
        self.port = port
        self.qr_secret = qr_secret
        self._nonce = entropy(NONCE_BYTES)
        self._sid = _session_id(pair_id, self._nonce)
        self._scalar = entropy(SHARE_BYTES)
        self._share = _share(code, self._sid, self._scalar, CHANNEL_V3 if self.v3 else CHANNEL, qr_secret or b"")
        self._answered = False
        self._known = False
        self._token = None
        self._done_tag = None
        # The host's name and identity as the exchange bound them; None until its tag checks out.
        self.host_name = None
        self.host_identity = None
        self.host_platform = None
        self.host_port = None
        # Version 3: this machine's entry that the new one replaces (a lost pair_done), or None.
        self.replaces = None

    @property
    def v3(self) -> bool:
        return self.version == PAIRING_V3

    def start(self) -> dict:
        message = {
            "type": MSG_PAIR_START,
            "pair": self.pair_id,
            "v": self.version,
            "nonce": _b64(self._nonce),
            "share": _b64(self._share),
            "name": self.name,
            "id": _b64(self.identity),
        }
        if self.v3:
            message["platform"] = self.platform
            message["port"] = self.port
            if self.qr_secret is not None:
                message["qr"] = 1
        return message

    def accept(self, answer: dict) -> dict:
        """The pair_confirm to send back, or PairingError naming why not. It judges one answer
        and no more: each answer it judged would be another guess at the code for whoever is
        answering. Version 3 refuses a host this machine already has with AlreadyPaired, once
        the host's tag has proved its id."""
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
            platform = answer.get("platform")
            port = answer.get("port")
            if len(share) != SHARE_BYTES or len(identity) > MAX_IDENTITY_BYTES or not isinstance(name, str) or len(name) > MAX_NAME_CHARS:
                raise PairingError(ERROR_REFUSED)
            if self.v3:
                if not (_machine_id_ok(identity) and _name_ok(name) and _platform_ok(platform) and _port_ok(port)):
                    raise PairingError(ERROR_REFUSED)
                requester = _party(ROLE_REQUESTER, self.identity, self.name, self.platform, self.port)
                host = _party(ROLE_HOST, identity, name, platform, port)
            else:
                requester = _party(ROLE_REQUESTER, self.identity, self.name)
                host = _party(ROLE_HOST, identity, name)
            shared = _x25519(self._scalar, share)
            token, requester_tag, host_tag, done_tag = _keys(self._sid, shared, self._share, requester, share, host, TOKEN_INFO_V3 if self.v3 else TOKEN_INFO)
        except (PairingError, UnicodeError) as exc:
            raise PairingError(ERROR_REFUSED) from exc
        if not hmac.compare_digest(tag, host_tag):
            raise PairingError(ERROR_REFUSED)
        if self.v3:
            self._check_host(identity, name, platform)
        self._token = token
        self._done_tag = done_tag
        self.host_name = name
        self.host_identity = identity
        self.host_platform = platform
        self.host_port = port
        return {"type": MSG_PAIR_CONFIRM, "pair": self.pair_id, "tag": _b64(requester_tag)}

    def _check_host(self, identity: bytes, name: str, platform: str) -> None:
        known, self.replaces = _find_known(self._peers(), self.identity, identity, name, platform, self.wall())
        if known is not None:
            self._known = True
            raise AlreadyPaired(None if known is True else known)

    def abort(self) -> dict:
        """What to send a host whose answer failed: a confirmation that cannot pass, so the host
        drops the code and says a wrong one was entered instead of showing it for the rest of
        its minute; or, after AlreadyPaired, that this machine has the host already."""
        message = {"type": MSG_PAIR_CONFIRM, "pair": self.pair_id, "tag": ""}
        if self._known:
            message["error"] = ERROR_KNOWN
        return message

    def finish(self, done: dict) -> str:
        """The agreed token, once the host's pair_done proves it stored the same one. Version 3
        refuses it when its key id is already one of this machine's entries'."""
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
        if self.v3 and _clashes(self._peers(), self._token, self.replaces):
            raise PairingError(ERROR_REFUSED)
        return self._token

    def proves(self, done: dict) -> bool:
        """Whether an ok pair_done carries the tag only the host holding this attempt's token could
        make. Checking it tests no guess at the code."""
        try:
            return self._done_tag is not None and hmac.compare_digest(_unb64(done.get("tag")), self._done_tag)
        except PairingError:
            return False

    def entry(self, address: str) -> dict:
        """Version 3: the peer entry for the host, reached at `address`, once finish() has
        returned the token."""
        return peer_entry(self.host_identity, self.host_name, self.host_platform, self.host_port, self._token, address, self.wall())


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


def broadcast_targets(port: int, interfaces=None) -> list:
    """Every address worth sending a beacon to, one per local IPv4 network plus the limited
    broadcast as a backstop. `interfaces` is _interfaces()'s list, read afresh when None.

    ⚠ 255.255.255.255 alone is not enough and this is not theoretical: it leaves by the default
    route only, so a PC with a second interface — a Hyper-V vEthernet switch is the common case —
    announces on whichever the routing table prefers and may never reach the LAN the Mac is on.
    Seen in practice: the PC beaconed steadily while no beacon reached the Mac at all, and the Mac
    then paired against a stale id from an older beacon that had got through, which is what
    produced `not_pairing`.

    Each network's directed broadcast follows its own netmask: a /24 guess sent a 169.254/16
    link-local interface to an address no host holds. Only where the platform will not list its
    interfaces is a /24 still assumed. The limited broadcast is always sent, so a wrong guess can
    only add delivery, never remove it."""
    if interfaces is None:
        interfaces = _interfaces()
    if interfaces:
        directed = [str(ipaddress.IPv4Network(f"{address}/{netmask}", strict=False).broadcast_address)
                    for address, netmask in interfaces]
    else:
        directed = [".".join(address.split(".")[:3] + ["255"]) for address in _local_ipv4_addresses()
                    if len(address.split(".")) == 4]
    targets = [("255.255.255.255", port)]
    seen = {"255.255.255.255"}
    for address in directed:
        if address not in seen:
            seen.add(address)
            targets.append((address, port))
    return targets


def _local_ipv4_addresses() -> list:
    found = [address for address, _netmask in _interfaces()]
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if not address.startswith("127.") and address not in found:
                found.append(address)
    except OSError:
        pass
    return found


def _interfaces() -> list:
    """(address, netmask) as dotted quads for each IPv4 interface that is up and has a network to
    broadcast on: loopback, point-to-point tunnels (a VPN's) and a /31 or /32 are left out. [] when
    the platform will not say, and the callers then do as they did before they could ask."""
    try:
        listed = _windows_interfaces() if sys.platform == "win32" else _posix_interfaces()
    except Exception:
        # ctypes over the C library: whatever goes wrong there costs this list and nothing else.
        return []
    found = []
    for address, netmask in listed:
        try:
            network = ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)
        except ValueError:
            continue
        if ipaddress.IPv4Address(address).is_loopback or address == "0.0.0.0" or not 0 < network.prefixlen < 31:
            continue
        if (address, netmask) not in found:
            found.append((address, netmask))
    return found


class _Ifaddrs(ctypes.Structure):
    pass


# The same layout on macOS and Linux; the sockaddrs it points at are not (BSD's start with a length).
_Ifaddrs._fields_ = [
    ("ifa_next", ctypes.POINTER(_Ifaddrs)),
    ("ifa_name", ctypes.c_char_p),
    ("ifa_flags", ctypes.c_uint),
    ("ifa_addr", ctypes.c_void_p),
    ("ifa_netmask", ctypes.c_void_p),
    ("ifa_dstaddr", ctypes.c_void_p),
    ("ifa_data", ctypes.c_void_p),
]
_IFF_UP, _IFF_BROADCAST, _IFF_LOOPBACK, _IFF_POINTOPOINT = 0x1, 0x2, 0x8, 0x10


def _sockaddr_ipv4(pointer):
    """(family, the four address bytes) of a sockaddr from getifaddrs. A BSD netmask may be cut
    short by its length byte, and may say no family: the bytes it leaves out are zeros."""
    if sys.platform == "darwin":
        length = ctypes.string_at(pointer, 1)[0]
        raw = ctypes.string_at(pointer, min(max(length, 2), 8))
        family = raw[1]
    else:
        raw = ctypes.string_at(pointer, 8)
        family = int.from_bytes(raw[:2], sys.byteorder)
    return family, raw[4:8].ljust(4, b"\0")


def _posix_interfaces() -> list:
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.getifaddrs.argtypes = [ctypes.POINTER(ctypes.POINTER(_Ifaddrs))]
    libc.freeifaddrs.argtypes = [ctypes.POINTER(_Ifaddrs)]
    head = ctypes.POINTER(_Ifaddrs)()
    if libc.getifaddrs(ctypes.byref(head)) != 0:
        raise OSError(ctypes.get_errno(), "getifaddrs failed")
    found = []
    try:
        node = head
        while node:
            item = node.contents
            node = item.ifa_next
            wanted = _IFF_UP | _IFF_BROADCAST
            if item.ifa_flags & (wanted | _IFF_LOOPBACK | _IFF_POINTOPOINT) != wanted or not item.ifa_addr or not item.ifa_netmask:
                continue
            family, address = _sockaddr_ipv4(item.ifa_addr)
            if family != socket.AF_INET:
                continue
            found.append((socket.inet_ntoa(address), socket.inet_ntoa(_sockaddr_ipv4(item.ifa_netmask)[1])))
    finally:
        libc.freeifaddrs(head)
    return found


class _IpAddrRow(ctypes.Structure):
    _fields_ = [
        ("dwAddr", ctypes.c_uint32),
        ("dwIndex", ctypes.c_uint32),
        ("dwMask", ctypes.c_uint32),
        ("dwBCastAddr", ctypes.c_uint32),
        ("dwReasmSize", ctypes.c_uint32),
        ("unused1", ctypes.c_ushort),
        ("wType", ctypes.c_ushort),
    ]


def _windows_interfaces() -> list:
    """GetIpAddrTable: every IPv4 address with its mask, each stored in network order. Windows
    gives no point-to-point flag here; a VPN adapter's /32 is what leaves it out."""
    iphlpapi = ctypes.WinDLL("iphlpapi")
    iphlpapi.GetIpAddrTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int]
    size = ctypes.c_ulong(0)
    iphlpapi.GetIpAddrTable(None, ctypes.byref(size), 0)
    for _attempt in range(3):
        table = ctypes.create_string_buffer(size.value)
        result = iphlpapi.GetIpAddrTable(table, ctypes.byref(size), 0)
        if result != 122:  # ERROR_INSUFFICIENT_BUFFER: an address came up in between
            break
    if result != 0:
        raise OSError(result, "GetIpAddrTable failed")
    count = ctypes.c_uint32.from_buffer(table).value
    rows = (_IpAddrRow * count).from_buffer(table, ctypes.sizeof(ctypes.c_uint32))
    gone = 0x0008 | 0x0040  # MIB_IPADDR_DISCONNECTED, MIB_IPADDR_DELETED
    return [(socket.inet_ntoa(struct.pack("=I", row.dwAddr)), socket.inet_ntoa(struct.pack("=I", row.dwMask)))
            for row in rows if not row.wType & gone]


def _answer_source(host: str, interfaces=None, towards=None):
    """The address to answer `host` from when the routing table would pick a different one: this
    machine's address on the network `host` is on. None when `host` is on none of them, or the
    routing table agrees. Seen on 01-10-2026: a VPN's route for the home network sent the laptop's
    answer out from the VPN's address, and the requester never knew it for an answer."""
    try:
        requester = ipaddress.IPv4Address(host)
    except ValueError:
        return None
    ours = [address for address, netmask in (_interfaces() if interfaces is None else interfaces)
            if requester in ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)]
    if not ours:
        return None
    try:
        routed = (towards or local_address_towards)(host)
    except OSError:
        routed = None
    # Two addresses on one network: whichever the routing table picks is already right.
    return None if routed in ours else ours[0]


def _oversized(exc: OSError) -> bool:
    """Windows' WSAEMSGSIZE: a datagram larger than the buffer, which Windows reports as an error
    where other platforms cut it short. Anyone can send one, so it must cost only itself; it once
    stopped a PC's pairing loop for good."""
    return getattr(exc, "winerror", None) == 10040


def _receive_on(sock, timeout: float):
    """The next datagram on `sock` as (message, address), {} for one that is not a message, or None
    once `timeout` passes."""
    sock.settimeout(max(timeout, 0.001))
    try:
        data, address = sock.recvfrom(MAX_DATAGRAM_BYTES + 1)
    except socket.timeout:
        return None
    except ConnectionResetError:
        # Windows: the port-unreachable of an earlier datagram.
        return {}, ("", 0)
    except OSError as exc:
        if _oversized(exc):
            return {}, ("", 0)
        raise PairingError("no_answer") from exc
    message = decode(data)
    return (message if isinstance(message, dict) else {}), address


def machine_name() -> str:
    return socket.gethostname().split(".", 1)[0] or "This machine"


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
        return _due(last, host, now)

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
                    if _oversized(exc):
                        continue
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
                    # At most one answer per address per interval: a beacon is larger than the `find` that
                    # draws it, so the rate is what keeps the port from being a useful amplifier.
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
        with self._attempt(code):
            client = PairingClient(pc["pair_id"], code, name or machine_name())
            token = self._exchange_udp(sock, (pc["address"], pc["reply_port"]), client, code)
            return token, client.host_name

    @contextlib.contextmanager
    def _attempt(self, code: str):
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
            yield
        finally:
            self._awaiting = None
            self._pairing.release()

    def _judge(self, client, answer: dict, code: str, send_abort) -> dict:
        """The pair_confirm for the first answer, which is the only one judged. Only an answer
        that claimed to be good can have tested a guess: that code is then remembered and the
        host told. A refusal tests nothing, and a stale pairing id draws one from an honest host."""
        try:
            return client.accept(answer)
        except PairingError:
            if answer.get("ok") is True:
                self._failed[code] = self.clock() + FAILED_CODE_SECONDS
                try:
                    send_abort(client.abort())
                except OSError:
                    pass
            raise

    def _exchange_udp(self, sock, target, client, code: str) -> str:
        while True:
            try:
                self._replies.get_nowait()
            except queue.Empty:
                break
        self._awaiting = client.pair_id
        source = _answer_source(target[0])
        if source is not None:
            # The routing table would send to `target` from another of this machine's addresses (a
            # VPN's route for the home network, the laptop on 01-10), which the host may have no
            # way back to. The attempt goes from the address on the target's network instead, on
            # a socket of its own that hears the replies itself.
            own = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                own.bind((source, 0))
            except OSError as exc:
                own.close()
                self.logger.warning("pairing not sent from %s, sent as routed: %s", source, exc)
            else:
                with own:
                    return self._exchange_on(own, lambda timeout: _receive_on(own, timeout), target, client, code)
        return self._exchange_on(sock, self._take_reply, target, client, code)

    def _exchange_on(self, sock, receive, target, client, code: str) -> str:
        send = lambda datagram: sock.sendto(datagram, target)
        answer = self._ask(send, receive, target, client.pair_id, client.start(), MSG_PAIR_ANSWER)
        confirm = self._judge(client, answer, code, lambda abort: send(encode(abort)))
        return client.finish(self._ask(send, receive, target, client.pair_id, confirm, MSG_PAIR_DONE, client.proves))

    def _take_reply(self, timeout: float):
        try:
            return self._replies.get(timeout=timeout)
        except queue.Empty:
            return None

    def _ask(self, send, receive, target, pair_id: str, message: dict, reply_type: str, proves=None) -> dict:
        """Sends `message` to `target` until the reply of `reply_type` for `pair_id` comes back;
        `receive(timeout)` gives the next (reply, address), or None once `timeout` passes.

        A reply from `target` decides at once: the caller judges it and never waits for a better
        one. A reply from any other address is held (the first ok one and the first refusal), and
        decides only if a window ends with nothing from `target`, even among replies already
        waiting, an ok one before a refusal: a host whose answer a VPN's route sent out from its
        own address (01-10) is still heard, while a stranger who read the pairing id from a beacon
        loses to a host's answer from the address asked. `proves`, for a receipt, says whether an
        ok one carries the host's proof: from another address one that does decides at once and
        one that does not is ignored, since checking it tests no guess."""
        datagram = encode(message)
        # The first ok reply and the first refusal from elsewhere, and nothing more.
        held = {}

        def consider(reply, address):
            if reply.get("pair") != pair_id or reply.get("type") != reply_type:
                return None
            if address[0] == target[0]:
                return reply
            if proves is not None and reply.get("ok") is True:
                return reply if proves(reply) else None
            held.setdefault(reply.get("ok") is True, reply)
            return None

        for _attempt in range(PAIR_ATTEMPTS):
            try:
                send(datagram)
            except OSError as exc:
                raise PairingError("no_answer") from exc
            deadline = self.clock() + PAIR_REPLY_TIMEOUT_SECONDS
            while True:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    break
                received = receive(remaining)
                if received is None:
                    break
                decided = consider(*received)
                if decided is not None:
                    return decided
            if held:
                # What has already come from the address asked still beats what was held.
                for _waiting in range(MAX_HELD_SWEEP):
                    received = receive(0)
                    if received is None:
                        break
                    decided = consider(*received)
                    if decided is not None:
                        return decided
                return held.get(True) or held[False]
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
                    if _oversized(exc):
                        continue
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
                elif kind in (MSG_PAIR_ANSWER, MSG_PAIR_DONE) and self._awaits(message):
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

    def _awaits(self, message: dict) -> bool:
        awaiting = self._awaiting
        return awaiting is not None and message.get("pair") == awaiting

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
        entry = self._beacon_entry(message, address)
        if entry is not None:
            self._remember(address, entry)

    def _beacon_entry(self, message: dict, address):
        name = message.get("name")
        port = message.get("port")
        pair_id = message.get("pair")
        version = message.get("pairing")
        if not isinstance(name, str) or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            return None
        try:
            name.encode("utf-8")
        except UnicodeError:
            # Half a surrogate pair is JSON a parser accepts and text no window can show.
            return None
        return {
            "name": name[:64] or address[0],
            "address": address[0],
            "port": port,
            "reply_port": address[1],
            "pair_id": pair_id if isinstance(pair_id, str) and pair_id.isascii() and 0 < len(pair_id) <= MAX_PAIR_ID_CHARS else None,
            # A beacon from 1.4.2 or earlier carries no number: that is version 1.
            "pairing": version if isinstance(version, int) and not isinstance(version, bool) else 1,
            "seen_at": self.clock(),
        }

    def _remember(self, address, entry: dict) -> None:
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


def _shut(sock) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _send_frame(sock, message: dict) -> None:
    body = encode(message)
    sock.sendall(struct.pack(">I", len(body)) + body)


def _recv_exact(sock, count: int, deadline: float) -> bytes:
    data = b""
    while len(data) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PairingError("no_answer")
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(count - len(data))
        except OSError as exc:
            raise PairingError("no_answer") from exc
        if not chunk:
            raise PairingError("no_answer")
        data += chunk
    return data


def _recv_frame(sock, deadline: float) -> dict:
    """One message over TCP: a 4-byte big-endian length, then the JSON a datagram would carry. A
    length outside 1 to 1024 is refused before the body is read, and a body that is not a Beamer
    message ends the exchange like a closed connection."""
    (length,) = struct.unpack(">I", _recv_exact(sock, 4, deadline))
    if not 0 < length <= MAX_DATAGRAM_BYTES:
        raise PairingError("no_answer")
    message = decode(_recv_exact(sock, length, deadline))
    if message is None:
        raise PairingError("no_answer")
    return message


def _linger(sock, deadline: float) -> None:
    """Shuts the sending side and reads what is left for up to a second, so the last message
    sent is not lost to a reset over unread bytes."""
    until = min(deadline, time.monotonic() + 1.0)
    try:
        sock.shutdown(socket.SHUT_WR)
        while True:
            remaining = until - time.monotonic()
            if remaining <= 0:
                return
            sock.settimeout(remaining)
            if not sock.recv(MAX_DATAGRAM_BYTES):
                return
    except OSError:
        pass


class _Slots:
    """The pairing listener's connections, on the link's handshake terms (WIRE.md section 2): at
    most `size`, one per address, a new connection from an address replacing its earlier one,
    and one from a new address replacing the oldest when all are held. A pinned connection, the
    one whose pair_start bound the code, is never the one replaced: the exchange under way is
    left alone. With nothing left to replace, the new connection is the one closed."""

    def __init__(self, size: int):
        self.size = size
        self._held = {}
        self._pinned = set()
        self._serial = 0
        self._lock = threading.Lock()

    def take(self, address: str, close):
        """The new connection's key, or None when every slot is pinned and it was closed."""
        with self._lock:
            free = [key for key in self._held if key not in self._pinned]
            victims = [key for key in free if self._held[key][0] == address]
            if not victims and len(self._held) >= self.size:
                victims = free[:1]
            closers = [self._held.pop(key)[1] for key in victims]
            if len(self._held) >= self.size:
                closers.append(close)
                taken = None
            else:
                self._serial += 1
                self._held[self._serial] = (address, close)
                taken = self._serial
        for closer in closers:
            closer()
        return taken

    def pin(self, key: int) -> bool:
        """False when the connection was already replaced: it is gone, and cannot be pinned."""
        with self._lock:
            if key not in self._held:
                return False
            self._pinned.add(key)
            return True

    def release(self, key: int) -> None:
        with self._lock:
            self._held.pop(key, None)
            self._pinned.discard(key)

    def close_all(self) -> None:
        with self._lock:
            closers = [close for _address, close in self._held.values()]
        for closer in closers:
            closer()


class PairingService(Discovery):
    """Pairing version 3's socket side, the same on every desktop (WIRE.md section 6, "Either
    end"). One UDP socket beacons every 2 seconds, keeps the list of machines it hears, answers
    `find`, carries this machine's requests and hosts its code; while a code is up a TCP listener
    on `tcp_port` hosts it too, with the same PairingHost, so rule 1 holds across both.

    `peers()` gives the peer entries as they stand; `store(entry, replaced)` writes a new one and
    drops `replaced` (that entry, by identity) when it is not None, raising if it cannot.
    `replaced` may be the entry migrated from 1.4.x, whose `id` is `""`: its zones go with it and
    are not moved to the new id, since pairing does not prove it is the same machine.
    `on_paired(entry)` is told of a pairing this machine hosted, after the entry is stored.

    ⚠ `peers` and `store` run on the socket threads while this service holds its own lock, which
    is what makes check-then-store one step for both roles. So they must never wait on the main
    thread (no synchronous hop to it), nor on a lock held by any thread while it calls into this
    service: the app's settings lock is taken inside `store`, and the app never calls
    `code`, `begin_pairing` and the rest while holding it. `on_paired` runs outside the lock."""

    def __init__(self, name, identity: bytes, platform: str, port_getter, peers, store, on_paired=None, logger=None,
                 bind_port=PAIRING_PORT, tcp_port=PAIRING_PORT, announce_to=("255.255.255.255", PAIRING_PORT),
                 clock=time.monotonic, wall=time.time):
        super().__init__(logger=logger, bind_port=bind_port, clock=clock)
        self.name = (name or machine_name())[:MAX_NAME_CHARS]
        self.identity = identity
        self.platform = platform
        self.port_getter = port_getter
        self._peer_entries = peers
        self.store = store
        self.on_paired = on_paired or (lambda entry: None)
        self.tcp_port = tcp_port
        self.announce_to = announce_to
        self.wall = wall
        self.host = PairingHost(clock, self.name, identity, version=PAIRING_V3, platform=platform, port=port_getter(),
                                peers=peers, store=store, wall=wall)
        self._host_lock = threading.Lock()
        self._listener = None
        self._listener_lock = threading.Lock()
        self._slots = _Slots(TCP_MAX_CONNECTIONS)
        self._own_addresses = frozenset()
        # Why the TCP listener is not up while a code is, or None.
        self.tcp_error = None

    # The host's side.

    def begin_pairing(self) -> str:
        """Shows a new code, raising PairingError(ERROR_FULL) when this machine has 32 peers."""
        with self._host_lock:
            self.host.port = self.port_getter()
            code = self.host.begin()
            self._open_listener()
        return code

    def cancel_pairing(self) -> None:
        with self._host_lock:
            self.host.cancel()
            self._close_listener()

    @property
    def code(self):
        with self._host_lock:
            return self.host.code if self.host.active else None

    @property
    def seconds_left(self) -> int:
        with self._host_lock:
            return self.host.seconds_left

    @property
    def outcome(self):
        with self._host_lock:
            self.host.active
            return self.host.outcome

    @property
    def known(self):
        with self._host_lock:
            return self.host.known

    def qr_text(self, address: str = None):
        """The QR's text for the code on screen, at `address` or pairing_address(); None when no
        code is up or this machine has no address."""
        with self._host_lock:
            if not self.host.active:
                return None
            code, secret = self.host.code, self.host.qr_secret
            if self._listener is None:
                # The TCP listener could not bind (tcp_error says why): a QR would point at
                # nothing, so the sheet shows the code alone.
                return None
        address = address or pairing_address()
        return qr_text(address, self.tcp_port, code, secret) if address else None

    def machines(self) -> list:
        """Every machine heard from recently: pcs()'s fields, with `platform` and, while its code
        is up, `id`."""
        return self.pcs()

    def stop(self) -> None:
        super().stop()
        with self._host_lock:
            self.host.cancel()
            self._close_listener()
        self._slots.close_all()

    def _open_listener(self) -> None:
        with self._listener_lock:
            if self._listener is not None:
                return
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if os.name != "nt":
                    # To bind again while a closed connection waits out TIME_WAIT. Windows'
                    # SO_REUSEADDR would let another program bind the port too, so not there.
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("", self.tcp_port))
                listener.listen(TCP_MAX_CONNECTIONS * 2)
                listener.settimeout(0.25)
            except OSError as exc:
                listener.close()
                self.tcp_error = str(exc)
                self.logger.warning("pairing over TCP %s is not available: %s", self.tcp_port, exc)
                return
            self.tcp_error = None
            stop = threading.Event()
            self._listener = (listener, stop)
        threading.Thread(target=self._accept, args=(listener, stop), name="Beamer-pairing-tcp", daemon=True).start()

    def _close_listener(self, only=None) -> None:
        with self._listener_lock:
            if only is not None and (self._listener is None or self._listener[0] is not only):
                return
            held, self._listener = self._listener, None
        if held is not None:
            held[1].set()
            held[0].close()

    def _accept(self, listener, stop) -> None:
        while not stop.is_set():
            # The listener is up only while a code is: it goes when the code expires as well as
            # when it is used or cancelled. Under the host's lock, so a new code shown at this
            # moment keeps its listener.
            with self._host_lock:
                if not self.host.active:
                    self._close_listener(only=listener)
                    return
            try:
                conn, address = listener.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                # Closed on purpose, or failed (out of descriptors, say). Either way it is let go,
                # so the next code opens a listener of its own rather than finding this one held.
                with self._host_lock:
                    if self._listener is not None and self._listener[0] is listener:
                        self.tcp_error = str(exc)
                        self._close_listener(only=listener)
                return
            deadline = time.monotonic() + TCP_EXCHANGE_SECONDS
            key = self._slots.take(address[0], lambda conn=conn: _shut(conn))
            if key is None:
                conn.close()
                continue
            threading.Thread(target=self._serve, args=(conn, address[0], key, deadline), name="Beamer-pairing-conn", daemon=True).start()

    def _serve(self, conn, address: str, key: int, deadline: float) -> None:
        """One TCP exchange: the beacon, then pair_start and pair_confirm in that order. Anything
        else, a second pair_start included, closes the connection with nothing sent; a refusal is
        sent before the close."""
        try:
            conn.settimeout(TCP_EXCHANGE_SECONDS)
            with self._host_lock:
                beacon = self.host.beacon(self.port_getter())
            if "pair" not in beacon:
                return
            _send_frame(conn, beacon)
            expected = MSG_PAIR_START
            while True:
                message = _recv_frame(conn, deadline)
                arrived = time.monotonic()
                if message.get("type") != expected:
                    return
                reply, paired, fresh = self._host_handle(message, address, ("tcp", key), lambda: self._slots.pin(key))
                if reply is None:
                    return
                if fresh:
                    time.sleep(max(0.0, arrived + PAIR_ANSWER_SECONDS - time.monotonic()))
                conn.settimeout(max(0.01, deadline - time.monotonic()))
                _send_frame(conn, reply)
                self._paired(paired, address)
                if reply.get("ok") is not True or expected == MSG_PAIR_CONFIRM:
                    _linger(conn, deadline)
                    return
                expected = MSG_PAIR_CONFIRM
        except (OSError, PairingError):
            pass
        except Exception:
            self.logger.exception("pairing connection dropped")
        finally:
            self._slots.release(key)
            conn.close()

    def _host_handle(self, message: dict, address: str, via, pin=None) -> tuple:
        """The host's reply. `pin` holds the connection whose pair_start just bound the code, under
        the host's lock, before its answer waits out PAIR_ANSWER_SECONDS: only a fresh answer pins,
        never one repeated from the cache to a replay on another connection."""
        with self._host_lock:
            reply = self.host.handle(message, address, via)
            paired, self.host.paired = self.host.paired, None
            fresh, self.host.fresh_answer = self.host.fresh_answer, False
            if pin is not None and fresh and message.get("type") == MSG_PAIR_START and reply is not None and reply.get("ok") is True:
                if not pin():
                    # Replaced in the instant before its pin: the code is bound to a connection
                    # that is gone, so the attempt ends as a refusal rather than holding the code.
                    self.host.cancel()
                    self.host.outcome = "refused"
                    reply = None
        return reply, paired, fresh

    def _paired(self, entry, address: str) -> None:
        if entry is None:
            return
        self.logger.info("paired with %s at %s", entry["name"], address)
        try:
            self.on_paired(entry)
        except Exception:
            self.logger.exception("on_paired failed")

    # The UDP socket, both ways.

    def _beacon(self) -> bytes:
        with self._host_lock:
            return encode(self.host.beacon(self.port_getter()))

    def _announce(self, sock) -> None:
        beacon = self._beacon()
        # A named address is honoured exactly, which keeps the tests off the real network.
        if self.announce_to[0] == "255.255.255.255":
            targets = broadcast_targets(self.announce_to[1])
        else:
            targets = [self.announce_to]
        delivered = 0
        for target in targets:
            try:
                sock.sendto(beacon, target)
                delivered += 1
            except OSError:
                continue
        if not delivered:
            self.logger.warning("beacon reached no interface")

    def _run(self) -> None:
        try:
            sock = _udp_socket(self.bind_port)
        except OSError as exc:
            self.error = str(exc)
            self.logger.error("pairing could not bind UDP %s: %s", self.bind_port, exc)
            return
        self.error = None
        self._sock = sock
        self._own_addresses = frozenset(_local_ipv4_addresses() + [pairing_address(), "127.0.0.1"]) - {None}
        next_beacon = 0.0
        answered = {}
        refused = {}
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_beacon:
                    next_beacon = now + BEACON_INTERVAL_SECONDS
                    self._announce(sock)
                self._send_find(sock)
                try:
                    data, address = sock.recvfrom(MAX_DATAGRAM_BYTES + 1)
                except socket.timeout:
                    continue
                except ConnectionResetError:
                    continue
                except OSError as exc:
                    if _oversized(exc):
                        continue
                    if self._stop.is_set():
                        break
                    self.error = str(exc)
                    self.logger.error("pairing stopped: %s", exc)
                    break
                arrived = time.monotonic()
                message = decode(data)
                if message is None:
                    continue
                try:
                    self._dispatch(sock, message, address, arrived, answered, refused)
                except Exception:
                    # Whatever one datagram does, the next beacon still goes out.
                    self.logger.exception("pairing datagram dropped")
        except Exception as exc:
            self.error = str(exc)
            self.logger.exception("pairing stopped")
        finally:
            self._sock = None
            sock.close()

    def _dispatch(self, sock, message: dict, address, arrived: float, answered: dict, refused: dict) -> None:
        kind = message.get("type")
        if kind == MSG_BEACON:
            # This machine hears its own broadcasts, from its own port at one of its own addresses.
            if address[1] != self.bind_port or address[0] not in self._own_addresses:
                self._note_beacon(message, address)
        elif kind == MSG_FIND:
            # At most one answer per address per interval: a beacon is larger than the `find` that
            # draws it, so the rate is what keeps the port from being a useful amplifier.
            if _due(answered, address[0], arrived):
                self._answer(sock, self._beacon(), address)
        elif kind in (MSG_PAIR_START, MSG_PAIR_CONFIRM, MSG_PAIR_REQUEST):
            reply, paired, fresh = self._host_handle(message, address[0], "udp")
            # A refusal is larger than the smallest datagram that draws one, so it is rationed.
            if reply is not None and (reply.get("ok") is True or _due(refused, address[0], arrived)):
                if fresh:
                    self._stop.wait(max(0.0, arrived + PAIR_ANSWER_SECONDS - time.monotonic()))
                try:
                    self._answer(sock, encode(reply), address)
                except OSError as exc:
                    self.logger.warning("pairing reply not sent: %s", exc)
            self._paired(paired, address[0])
        elif kind in (MSG_PAIR_ANSWER, MSG_PAIR_DONE) and self._awaits(message):
            try:
                self._replies.put_nowait((message, address))
            except queue.Full:
                pass

    def _answer(self, sock, data: bytes, address) -> None:
        """Sends a reply to a pairing request or a `find` from this machine's address on the
        requester's own network, where the routing table would pick another (_answer_source): a
        requester sorts answers by the address it asked, though never by port.

        ⚠ The socket that does it takes a port of its own. Bound to the pairing port beside the
        main socket, it would take any datagram for that address arriving while it lived (a
        pair_confirm, another machine's request), and Windows does not say which of two such
        sockets gets one. A requester takes beacons from strangers on its pairing port, so an
        answer from another port reaches it as well."""
        source = _answer_source(address[0])
        if source is not None:
            try:
                out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    out.bind((source, 0))
                    out.sendto(data, address)
                    return
                finally:
                    out.close()
            except OSError as exc:
                self.logger.warning("pairing reply not sent from %s, sent as routed: %s", source, exc)
        sock.sendto(data, address)

    def _beacon_entry(self, message: dict, address):
        entry = super()._beacon_entry(message, address)
        if entry is not None:
            platform = message.get("platform")
            entry["platform"] = platform if _platform_ok(platform) else None
            entry["id"] = message.get("id") if _id_text_ok(message.get("id")) else None
        return entry

    # The requester's side.

    def pair(self, machine: dict, code: str) -> dict:
        """Pairs over UDP with `machine`, an entry from machines(), and returns the entry stored
        for it. Blocks for up to PAIR_ATTEMPTS replies' worth, twice; call it off the main
        thread. Raises AlreadyPaired, or PairingError naming why."""
        sock = self._sock
        if sock is None:
            raise PairingError("discovery is not running")
        if machine.get("pairing") != PAIRING_V3:
            # Said before anything is sent: versions 2 and 3 never pair.
            raise PairingError(ERROR_VERSION)
        if not isinstance(machine.get("pair_id"), str):
            raise PairingError(ERROR_NOT_PAIRING)
        with self._attempt(code):
            client = self._client(machine["pair_id"], code)
            self._exchange_udp(sock, (machine["address"], machine["reply_port"]), client, code)
            return self._keep(client, machine["address"])

    def pair_by_address(self, address: str, code: str, port: int = PAIRING_PORT) -> dict:
        """"Pair by address": the exchange over TCP with a typed address and code, for a host no
        broadcast reaches. No QR secret, so no `qr`."""
        try:
            resolved = socket.getaddrinfo((address or "").strip(), port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
        except (OSError, IndexError, UnicodeError) as exc:
            raise PairingError("no_answer") from exc
        return self._pair_tcp(resolved, port, code, None)

    def pair_by_qr(self, text: str) -> dict:
        """The exchange over TCP from a QR's text, with its secret and `qr: 1`. A QR of a machine
        this one has already does not pair: AlreadyPaired, before anything is sent."""
        fields = read_qr(text)
        return self._pair_tcp(fields["host"], fields["port"], fields["code"], fields["secret"])

    def _client(self, pair_id: str, code: str, secret: bytes = None) -> PairingClient:
        return PairingClient(pair_id, code, self.name, self.identity, version=PAIRING_V3, platform=self.platform,
                             port=self.port_getter(), qr_secret=secret, peers=self._peer_entries, wall=self.wall)

    def _pair_tcp(self, address: str, port: int, code: str, secret) -> dict:
        with self._attempt(code):
            if len(self._peer_entries()) >= MAX_PEERS:
                raise PairingError(ERROR_FULL)
            try:
                sock = socket.create_connection((address, port), timeout=TCP_CONNECT_SECONDS)
            except OSError as exc:
                raise PairingError("no_answer") from exc
            deadline = time.monotonic() + TCP_EXCHANGE_SECONDS
            try:
                with sock:
                    beacon = _recv_frame(sock, deadline)
                    if beacon.get("type") != MSG_BEACON:
                        raise PairingError("no_answer")
                    version = beacon.get("pairing")
                    if not _is_int(version) or version != PAIRING_V3:
                        raise PairingError(ERROR_VERSION)
                    pair_id = beacon.get("pair")
                    if not isinstance(pair_id, str) or not pair_id.isascii() or not 0 < len(pair_id) <= MAX_PAIR_ID_CHARS:
                        raise PairingError(ERROR_NOT_PAIRING)
                    if secret is not None:
                        self._not_paired_already(beacon.get("id"))
                    client = self._client(pair_id, code, secret)
                    sock.settimeout(max(0.01, deadline - time.monotonic()))
                    _send_frame(sock, client.start())
                    answer = _recv_frame(sock, deadline)
                    if answer.get("type") != MSG_PAIR_ANSWER:
                        raise PairingError("no_answer")
                    confirm = self._judge(client, answer, code, lambda abort: _send_frame(sock, abort))
                    _send_frame(sock, confirm)
                    client.finish(_recv_frame(sock, deadline))
            except OSError as exc:
                raise PairingError("no_answer") from exc
            return self._keep(client, address)

    def _not_paired_already(self, identity) -> None:
        """A QR of a machine this one has paired already: the caller tries its link at that
        address instead of pairing again."""
        if not _id_text_ok(identity):
            return
        if identity == _b64(self.identity):
            raise AlreadyPaired(None)
        for known in self._peer_entries():
            if known.get("id") == identity:
                raise AlreadyPaired(known)

    def _keep(self, client: PairingClient, address: str) -> dict:
        """The requester writes its entry once Td has checked out. The checks are made again,
        under the lock the host's side stores under, because this machine may have hosted or
        requested another pairing since: two at once must not pass 32 peers or share an id. A
        failed write leaves the host an entry that never links, which pairing again within ten
        minutes replaces."""
        entry = client.entry(address)
        with self._host_lock:
            peers = self._peer_entries()
            known, replaced = _find_known(peers, self.identity, client.host_identity, client.host_name, client.host_platform, self.wall())
            if known is not None:
                raise AlreadyPaired(None if known is True else known)
            replaced = replaced or _migrated_for(peers, address, client.host_name, client.host_platform)
            if _clashes(peers, entry["token"], replaced):
                raise PairingError(ERROR_REFUSED)
            if replaced is None and len(peers) >= MAX_PEERS:
                raise PairingError(ERROR_FULL)
            try:
                self.store(entry, replaced)
            except Exception as exc:
                self.logger.warning("the new peer entry could not be saved: %s", exc)
                raise PairingError("not_saved") from exc
        return entry


def _id_text_ok(text) -> bool:
    return protocol.read_id(text) is not None
