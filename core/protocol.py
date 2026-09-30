"""Wire protocol shared by mac_app/bridge.py and win_app/receiver.py.

The link is encrypted and authenticated with ChaCha20-Poly1305; the shared
token never crosses the wire, and a peer that cannot produce a valid tag does
not hold it.

A connection opens with one cleartext preamble each way -- WIRE_MAGIC, one
version byte, and that side's 8-byte nonce prefix, drawn fresh per connection
-- so a version mismatch produces a readable status instead of undecryptable
garbage. Each direction then has its own key, HKDF-SHA256 over the token with
the sender's prefix and the receiver's prefix in `info`, so nothing is sealed
until the peer's preamble is in. Every frame after that is a 4-byte big-endian
length, then a 4-byte big-endian counter, then the ciphertext with its tag; the
length covers the counter and ciphertext. The nonce is prefix + counter, each
side counting its own frames from 1, and the receiver rejects any counter that
is not the next one.

What that stops: a frame played again, dropped or reordered inside a
connection fails the counter; a whole recorded connection played into a new
one fails the tag, because the receiver's fresh prefix is part of the key its
peer had to seal under; a side's own frames reflected back fail the tag,
because the two directions' keys differ. What it does not give is forward
secrecy: anyone who later learns the token can read a recording made before.
"""

import base64
import binascii
import hashlib
import hmac
import json
import os
import socket
import struct
import threading
import time

from nacl.bindings import (
    crypto_aead_chacha20poly1305_ietf_ABYTES,
    crypto_aead_chacha20poly1305_ietf_decrypt,
    crypto_aead_chacha20poly1305_ietf_encrypt,
)
from nacl.exceptions import CryptoError

HEADER_SIZE = 4
COUNTER_SIZE = 4
NONCE_PREFIX_SIZE = 8
MAX_COUNTER = 0xFFFFFFFF
# Wire constant, deliberately kept as the pre-rename name: changing it would break an existing
# pairing and a mixed-version pair for no gain.
WIRE_MAGIC = b"BEAMY"
PREAMBLE_SIZE = len(WIRE_MAGIC) + 1 + NONCE_PREFIX_SIZE
# 5: per-connection keys bound to both prefixes (1.4.1). 4 keyed every connection alike from the
# token, so a recorded connection could be played into a new one whole.
PROTOCOL_VERSION = 5

# Both ports sit below 49152 on purpose. Windows and macOS both hand out 49152-65535 as
# ephemeral ports, and Windows lets WinNAT reserve whole 100-port blocks from that range that
# move on every reboot: one landed on the old 51820 and the receiver could not bind at all,
# while the PC's outward link carried on working, so input went one way only. Nothing is
# handed out below 49152, so the port a user pairs on stays theirs.
DEFAULT_PORT = 24820
PAIRING_PORT = 24821
# What DEFAULT_PORT used to be. A config still carrying it was never chosen by
# anyone, so it is migrated rather than honoured; a port a user picked is left alone.
LEGACY_DEFAULT_PORT = 51820
KEY_SALT = b"beamer-link-v5"
KEY_INFO = b"beamer-frames"
CLIPBOARD_MAX_BYTES = 256 * 1024
# The clipboard goes out ahead of the focus message on every switch, sealed as one frame and
# buffered whole on both sides, so the image cap is a bound on how late the switch lands, not on
# bandwidth. A full-screen grab of a 3456x2234 Retina display measured 2.9MB as PNG;
# 8MB covers that with room for a busier desktop, and as a base64 frame (~10.7MB) it is about
# 0.1s on wired gigabit and half a second on decent Wi-Fi. Beyond it the text still goes.
CLIPBOARD_IMAGE_MAX_BYTES = 8 * 1024 * 1024
CLIPBOARD_IMAGE_PNG = "png"
# The largest sealed frame either end accepts. It must hold the biggest clipboard message -- an
# 8MB image is ~10.7MB as base64, beside up to 256KB of text -- because the clipboard also travels
# back on the reply stream. A 1MB cap there dropped the link on every return with a screenshot on
# the clipboard.
MAX_FRAME_BYTES = 16 * 1024 * 1024
# The first frame each way is a hello or a welcome of a few dozen bytes. Anything larger before
# authentication is not a Beamer, so it is refused before it is read.
HELLO_MAX_BYTES = 4096
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

MSG_HELLO = "hello"
MSG_WELCOME = "welcome"
MSG_PING = "ping"
MSG_KEYDOWN = "keydown"
MSG_KEYUP = "keyup"
MSG_MOUSEMOVE = "mousemove"
MSG_MOUSEDOWN = "mousedown"
MSG_MOUSEUP = "mouseup"
MSG_SCROLL = "scroll"
MSG_ACK = "ack"
MSG_CLIPBOARD = "clipboard"
MSG_FOCUS = "focus"
MSG_GESTURE = "gesture"
MSG_SWITCH = "switch"
MSG_ARRANGEMENT = "arrangement"
MSG_SETTINGS = "settings"

# System trackpad gestures. The Mac classifies one at the end of the movement and sends a
# single message; the name is the finger motion, not its meaning, so the receiver decides
# what a swipe does and Windows' own touchpad settings apply when it can inject a real one.
GESTURE_SWIPE_UP = "swipe_up"
GESTURE_SWIPE_DOWN = "swipe_down"
GESTURE_SWIPE_LEFT = "swipe_left"
GESTURE_SWIPE_RIGHT = "swipe_right"
GESTURE_SPREAD = "spread"
GESTURE_PINCH = "pinch"
GESTURE_NAMES = frozenset(
    {
        GESTURE_SWIPE_UP,
        GESTURE_SWIPE_DOWN,
        GESTURE_SWIPE_LEFT,
        GESTURE_SWIPE_RIGHT,
        GESTURE_SPREAD,
        GESTURE_PINCH,
    }
)

class ProtocolError(Exception):
    pass


class ConnectionClosed(Exception):
    pass


class AuthenticationError(ProtocolError):
    """A frame failed authentication: the peer does not hold the shared token,
    or the stream was tampered with. Deliberately carries nothing else."""

    def __init__(self):
        super().__init__("frame failed authentication")


class VersionMismatch(ProtocolError):
    """The peer's preamble named another wire version. `peer_version` is None
    when the peer sent no preamble at all -- an older Beamer that opened with a
    length-prefixed cleartext frame instead."""

    def __init__(self, peer_version):
        super().__init__(f"peer speaks wire version {peer_version!r}, expected {PROTOCOL_VERSION}")
        self.peer_version = peer_version


def hkdf_sha256(key_material: bytes, salt: bytes, info: bytes) -> bytes:
    """HKDF-SHA256 (RFC 5869) for one 32-byte block, the only length either key needs."""
    extracted = hmac.new(salt, key_material, hashlib.sha256).digest()
    return hmac.new(extracted, info + b"\x01", hashlib.sha256).digest()


def derive_key(token: str, sender_prefix: bytes, receiver_prefix: bytes) -> bytes:
    """The key for the frames one side seals and the other opens, on one
    connection only: both prefixes are drawn fresh per connection, and their
    order is the direction."""
    return hkdf_sha256(token.encode("utf-8"), KEY_SALT, KEY_INFO + bytes(sender_prefix) + bytes(receiver_prefix))


class SecureSession:
    """One connection's cipher state for both directions. Build a new one per
    connection: it draws a fresh nonce prefix and starts both counters at zero.
    Neither key exists until accept_preamble, so nothing can be sealed or
    opened before the peer's prefix is known.
    `send_lock` serialises sealing, and send_msg holds it across the write too,
    so counters are never reused and frames reach the wire in counter order;
    `open` is expected to run on a single reader thread per connection."""

    def __init__(self, token: str, prefix: bytes = None):
        self._token = token
        self.prefix = os.urandom(NONCE_PREFIX_SIZE) if prefix is None else bytes(prefix)
        if len(self.prefix) != NONCE_PREFIX_SIZE:
            raise ValueError("nonce prefix must be 8 bytes")
        self.send_lock = threading.RLock()
        self._send_counter = 0
        self._send_key = None
        self.peer_prefix = None
        self._recv_key = None
        self._recv_counter = 0

    def preamble(self) -> bytes:
        return WIRE_MAGIC + bytes([PROTOCOL_VERSION]) + self.prefix

    def accept_preamble(self, data: bytes) -> None:
        if len(data) != PREAMBLE_SIZE or not data.startswith(WIRE_MAGIC):
            raise VersionMismatch(None)
        version = data[len(WIRE_MAGIC)]
        if version != PROTOCOL_VERSION:
            raise VersionMismatch(version)
        peer_prefix = data[len(WIRE_MAGIC) + 1:]
        if self.peer_prefix is not None:
            raise ProtocolError("second preamble on one connection")
        # Equal prefixes would make both directions one key.
        if peer_prefix == self.prefix:
            raise ProtocolError("peer echoed this side's nonce prefix")
        with self.send_lock:
            self._send_key = derive_key(self._token, self.prefix, peer_prefix)
        self._recv_key = derive_key(self._token, peer_prefix, self.prefix)
        self.peer_prefix = bytes(peer_prefix)

    def seal(self, msg: dict) -> bytes:
        body = json.dumps(msg).encode("utf-8")
        with self.send_lock:
            if self._send_key is None:
                raise ProtocolError("nothing is sealed before the peer's preamble")
            if self._send_counter >= MAX_COUNTER:
                raise ProtocolError("frame counter exhausted; reconnect")
            self._send_counter += 1
            counter = struct.pack(">I", self._send_counter)
            ciphertext = crypto_aead_chacha20poly1305_ietf_encrypt(body, None, self.prefix + counter, self._send_key)
        return struct.pack(">I", COUNTER_SIZE + len(ciphertext)) + counter + ciphertext

    def open(self, body: bytes) -> dict:
        if self._recv_key is None:
            raise ProtocolError("frame received before the peer's preamble")
        if len(body) <= COUNTER_SIZE:
            raise ProtocolError("frame too short")
        # PyNaCl takes bytes only; any other buffer is copied, and a non-buffer is a TypeError as it
        # always was, never a tag failure.
        body = bytes(memoryview(body))
        (counter,) = struct.unpack(">I", body[:COUNTER_SIZE])
        if counter != self._recv_counter + 1:
            raise ProtocolError("frame counter did not advance; replay rejected")
        sealed = body[COUNTER_SIZE:]
        # Shorter than a tag cannot authenticate, and PyNaCl would fail to size its output
        # buffer with a ValueError rather than refuse it, so it is refused here first.
        if len(sealed) < crypto_aead_chacha20poly1305_ietf_ABYTES:
            raise AuthenticationError() from None
        try:
            plain = crypto_aead_chacha20poly1305_ietf_decrypt(sealed, None, self.peer_prefix + body[:COUNTER_SIZE], self._recv_key)
        except CryptoError:
            raise AuthenticationError() from None
        self._recv_counter = counter
        try:
            message = json.loads(plain.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"malformed message body: {exc}") from exc
        if not isinstance(message, dict):
            raise ProtocolError("message body must be a JSON object")
        return message


def send_msg(sock: socket.socket, session, msg: dict) -> None:
    with session.send_lock:
        sock.sendall(session.seal(msg))


def legacy_frame(msg: dict) -> bytes:
    """The pre-v4 cleartext framing, kept only so an older peer can be told to
    update in words it understands rather than left waiting on silence."""
    body = json.dumps(msg).encode("utf-8")
    return struct.pack(">I", len(body)) + body


def _recv_exact(sock: socket.socket, n: int, deadline: float = None) -> bytes:
    """`deadline` is a time.monotonic() moment the whole read must finish by.
    A socket timeout alone bounds each recv, not the read: a peer sending one
    byte every few seconds would hold a handshake open for as long as it
    liked. Past the deadline this raises socket.timeout like a quiet peer."""
    chunks = []
    remaining = n
    while remaining > 0:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout("deadline passed while reading")
            sock.settimeout(left)
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionClosed("socket closed while reading")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_preamble(sock: socket.socket, session, deadline: float = None) -> None:
    session.accept_preamble(_recv_exact(sock, PREAMBLE_SIZE, deadline))


def recv_msg(sock: socket.socket, session, limit: int = MAX_FRAME_BYTES, deadline: float = None) -> dict:
    """`limit` is the most a frame may claim before it is read. The receiver
    passes HELLO_MAX_BYTES for the first frame: until that frame's tag has
    been checked the peer has proved nothing, so it must not be able to make
    this side buffer a clipboard-sized frame per connection. `deadline`
    bounds the whole read the same way, for the same reason."""
    header = _recv_exact(sock, HEADER_SIZE, deadline)
    (length,) = struct.unpack(">I", header)
    if length < 1 or length > limit:
        raise ProtocolError(f"invalid message length: {length}")
    return session.open(_recv_exact(sock, length, deadline))


def hello_msg(return_edge: str = None, resistance_px: int = None) -> dict:
    """The first encrypted frame. It carries no token: decrypting it is the
    proof the sender holds the shared secret.

    `return_edge` and `resistance_px` are the same pair a focus message
    carries, sent here as well so the receiver knows the way home before the
    first crossing rather than after it -- which is what lets the receiver's
    own machine push back across that same border unprompted. Both are
    omitted when None and unknown keys are ignored, so this needs no version
    bump. `settings` says this end keeps settings in step (settings_sync), so an end
    holding the switch on can tell an older peer that never will."""
    data = {"version": PROTOCOL_VERSION, "settings": 1}
    if return_edge is not None:
        data["return_edge"] = return_edge
    if resistance_px is not None:
        data["resistance_px"] = resistance_px
    return {"type": MSG_HELLO, "data": data}


def welcome_msg(error: str = None) -> dict:
    """Sent by the receiver to confirm authentication and protocol version.
    `error` (e.g. "version_mismatch") is set when the sender's declared version
    is not supported. `settings` is the hello's: this end keeps settings in step."""
    data = {"version": PROTOCOL_VERSION, "settings": 1}
    if error is not None:
        data["error"] = error
    return {"type": MSG_WELCOME, "data": data}


def ping_msg() -> dict:
    """Sent by the sender while idle so a dead connection is detected quickly.
    Carries no `seq` and must never be recorded as a processed input event."""
    return {"type": MSG_PING, "data": {}}


def ack_msg(seq: int, locked: bool = False) -> dict:
    """Sent by the receiver to acknowledge processed input up to `seq`.
    The sender increments `seq` once per outbound input event; the receiver
    echoes back the highest `seq` it has processed. Used as a watchdog
    heartbeat — if the sender doesn't see an ACK for ~2s while redirecting,
    it must force-revert to local input.

    `locked` is set only while Windows is clearing its lock screen, during
    which input is being dropped rather than injected. It is omitted when
    false, and unknown keys are ignored on both sides, so this stays
    compatible in both directions and needs no protocol version bump."""
    data = {"seq": seq}
    if locked:
        data["locked"] = True
    return {"type": MSG_ACK, "data": data}


def scroll_msg(dy, dx=0.0, mode: str = "line") -> dict:
    """Scroll event. `mode` is "pixel" for continuous trackpad deltas (float
    px) or "line" for discrete wheel clicks. `dx` and `mode` are optional on
    receive for backward tolerance: a missing `dx` means 0, a missing `mode`
    means "line"."""
    return {"type": MSG_SCROLL, "data": {"dy": dy, "dx": dx, "mode": mode}}


def clipboard_msg(text: str = None, image: bytes = None) -> dict:
    """Whatever was on the clipboard at the switch: `text` as before, and an
    optional PNG under `image` (base64) with `image_format` naming it. Each
    key is omitted when absent. A pre-image receiver ignores the two new keys
    and an image-only message (no `text`) the same way it ignores any other
    unknown data, so this needs no protocol version bump: SecureSession.open
    only requires a JSON object, and neither app validates keys it does not
    read."""
    data = {}
    if text is not None:
        data["text"] = text
    if image is not None:
        data["image"] = base64.b64encode(image).decode("ascii")
        data["image_format"] = CLIPBOARD_IMAGE_PNG
    return {"type": MSG_CLIPBOARD, "data": data}


def clipboard_image(data) -> bytes:
    """The PNG bytes carried by a clipboard message's `data`, or None when
    there is no image or it is not usable: an unknown format, base64 that
    does not decode, bytes that do not start as a PNG, or more than
    CLIPBOARD_IMAGE_MAX_BYTES. Never raises; the clipboard must never break
    a switch, so junk is dropped here rather than surfaced."""
    if not isinstance(data, dict) or data.get("image_format") != CLIPBOARD_IMAGE_PNG:
        return None
    encoded = data.get("image")
    # Length check before decoding, so an oversized payload costs no decode.
    if not isinstance(encoded, str) or len(encoded) > CLIPBOARD_IMAGE_MAX_BYTES * 4 // 3 + 4:
        return None
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(image) > CLIPBOARD_IMAGE_MAX_BYTES or not image.startswith(PNG_SIGNATURE):
        return None
    return image


def focus_msg(
    target: str,
    edge: str = None,
    offset: float = None,
    return_edge: str = None,
    resistance_px: int = None,
) -> dict:
    """Sent by the sender to move input. `edge` is the receiver edge being
    arrived at and `offset` the fraction along it, both absent when the switch
    came from the shortcut rather than a crossing. `return_edge` and
    `resistance_px` are sent on every switch to the receiver: they are how it
    knows the way home, so nothing about crossing is configured on its side.
    `target` names the machine input is moving to, which is the receiver's own
    name on the way out and the sender's on the way back. All four are omitted when None, and unknown keys are ignored,
    so this needs no protocol version bump."""
    data = {"target": target}
    if edge is not None:
        data["edge"] = edge
    if offset is not None:
        data["offset"] = offset
    if return_edge is not None:
        data["return_edge"] = return_edge
    if resistance_px is not None:
        data["resistance_px"] = resistance_px
    return {"type": MSG_FOCUS, "data": data}


def switch_msg(target: str, edge: str = None, offset: float = None) -> dict:
    """Sent by the receiver to ask the sender to move input, when the pointer
    has been pushed through the receiver's return edge. `target` is the
    sender's machine -- "mac" on the Mac-to-Windows link, "windows" on the
    Windows-to-Mac one. `edge` is the edge to arrive at there and `offset`
    the fraction along it; both are absent for a plain return with no
    position to carry."""
    data = {"target": target}
    if edge is not None:
        data["edge"] = edge
    if offset is not None:
        data["offset"] = offset
    return {"type": MSG_SWITCH, "data": data}


def arrangement_msg(mac_edge: str, set_at: int) -> dict:
    """Where the two machines are in relation to each other, as one value:
    the edge of the MAC that leads to the PC. Everything else follows from it
    -- the Mac crosses out by that edge, arrives at the opposite edge of the
    PC, and comes home the same way -- so the two ends cannot describe a
    border that does not exist.

    Either machine may change it, and tells the other with this message.
    `set_at` is unix seconds at the machine that made the change: when two
    ends meet holding different arrangements, because one was changed while
    the other was asleep, the newer one wins rather than whichever spoke
    first. Unknown message types are ignored, so an older Beamer keeps
    working with the arrangement it already has."""
    return {"type": MSG_ARRANGEMENT, "data": {"mac_edge": mac_edge, "set_at": int(set_at)}}


def read_arrangement(data):
    """(mac_edge, set_at) from an arrangement message's data, or None when it
    carries no edge worth acting on. Never raises: a malformed arrangement
    must not take a working link down."""
    if not isinstance(data, dict):
        return None
    edge = data.get("mac_edge")
    if edge not in ("left", "right", "top", "bottom"):
        return None
    set_at = data.get("set_at")
    if isinstance(set_at, bool) or not isinstance(set_at, (int, float)):
        set_at = 0
    return edge, int(set_at)


def arrangement_wins(theirs: int, mine: int) -> bool:
    """Whether an arrangement the peer sent should replace the one held here.

    Both ends stamp their own change, so the newer stamp stands: without that,
    an arrangement changed at one machine while the other was asleep would be
    silently undone by the sleeper's hello on the next connection. A peer that
    sends no stamp at all (an older Beamer, or a first connection) loses to any
    stamp of ours, and two ends that have never changed it both hold zero and
    agree anyway."""
    return int(theirs or 0) > int(mine or 0)


def settings_msg(data: dict) -> dict:
    """Same on both machines: whether it is on, its stamp and, while on, the shared values, as
    settings_sync.message_data builds them. Sent on every change while on and announced on every
    new connection; an older Beamer ignores the type."""
    return {"type": MSG_SETTINGS, "data": dict(data)}


def keeps_settings(data) -> bool:
    """Whether a hello's or welcome's data says the peer keeps settings in step."""
    return isinstance(data, dict) and data.get("settings") == 1


def gesture_msg(name: str) -> dict:
    """One completed system gesture (a GESTURE_* name). Unknown message types are
    ignored by the receiver, so this needs no protocol version bump."""
    return {"type": MSG_GESTURE, "data": {"name": name}}
