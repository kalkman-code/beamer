"""The link's wire, version 6 (WIRE.md is the specification): the handshake, the keys, the
frames, and a builder and a validator for every message, shared by both apps' initiator
(core/link.py) and responder (core/receiver.py).

A connection opens with one cleartext preamble each way: WIRE_MAGIC, the version byte, that side's
8-byte nonce prefix drawn fresh per connection, the pair's key id and an ephemeral X25519 share.
Each direction's key is HKDF-SHA256 over the pairing token and the X25519 result, bound to both
preambles and the direction, so a recording cannot be read later even by someone who learns the
token. Every frame after that is a 4-byte big-endian length, a 4-byte big-endian counter, then
ChaCha20-Poly1305 ciphertext with its tag; the nonce is prefix + counter, each side counting its
own frames from 1, and the receiver rejects any counter that is not the next one.

Version 5 (Beamer 1.4.x) is no longer spoken. Its preamble is still read far enough to refuse it
in words the older peer shows (the short reply); core/tests/v5_link.py keeps a frozen copy for the
vector replay and the refusal tests.
"""

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import socket
import struct
import threading
import time
import zlib

from nacl.bindings import (
    crypto_aead_chacha20poly1305_ietf_ABYTES,
    crypto_aead_chacha20poly1305_ietf_decrypt,
    crypto_aead_chacha20poly1305_ietf_encrypt,
    crypto_scalarmult,
    crypto_scalarmult_base,
)
from nacl.exceptions import CryptoError

HEADER_SIZE = 4
COUNTER_SIZE = 4
NONCE_PREFIX_SIZE = 8
MAX_COUNTER = 0xFFFFFFFF
# Wire constant, deliberately kept as the pre-rename name: changing it would break an existing
# pairing and a mixed-version pair for no gain.
WIRE_MAGIC = b"BEAMY"
# 6: the key id and an ephemeral X25519 share in the preamble, keys with forward secrecy (1.5.0).
# 5 (1.4.1 to 1.4.x) keyed each connection from the token and both prefixes only.
LINK_VERSION = 6

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
KEY_INFO = b"beamer-frames"
CLIPBOARD_MAX_BYTES = 256 * 1024
# The clipboard goes out ahead of the focus message on every switch, sealed as one frame and
# buffered whole on both sides, so the image cap is a bound on how late the switch lands, not on
# bandwidth. A full-screen grab of a 3456x2234 Retina display measured 2.9MB as PNG;
# 8MB covers that with room for a busier desktop, and as a base64 frame (~10.7MB) it is about
# 0.1s on wired gigabit and half a second on decent Wi-Fi. Beyond it the text still goes.
CLIPBOARD_IMAGE_MAX_BYTES = 8 * 1024 * 1024
CLIPBOARD_IMAGE_PNG = "png"
# What a PNG may decode to, checked from its bytes first (png_fits): 2^25 pixels holds an 8K screen
# (7680x4320 is 33,177,600) and is 128 MiB as 8-bit RGBA, 256 MiB at 16 bits a channel, where a
# few compressed megabytes could otherwise claim gigabytes. Metadata a decoder inflates (colour profiles, compressed text) is held
# to 4 MiB in all; a real colour profile is a few kilobytes.
PNG_MAX_SIDE = 16384
PNG_MAX_PIXELS = 2**25
PNG_METADATA_MAX_BYTES = 4 * 1024 * 1024
# The largest sealed frame either end accepts. It must hold the biggest clipboard message -- an
# 8MB image is ~10.7MB as base64, beside up to 256KB of text -- because the clipboard also travels
# back on the reply stream. A 1MB cap there dropped the link on every return with a screenshot on
# the clipboard.
MAX_FRAME_BYTES = 16 * 1024 * 1024
# A frame arrives whole within this of its first length byte, or the link ends (WIRE.md section 2,
# frames): liveness alone lets a peer that sends a byte a second hold a 16 MiB read open for days.
FRAME_COMPLETE_SECONDS = 30.0
# A message that is not a clipboard fits in far less. The largest is `text`: 4096 bytes of UTF-8,
# which as JSON escapes is at most 6 bytes a character, about 24 KiB; hello, welcome, settings,
# focus and paired are each a few KiB at most. A frame over this from a peer that may not send a
# clipboard is authenticated, so the counter stays in step, but never parsed.
SMALL_MESSAGE_BYTES = 64 * 1024
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

    def __init__(self, peer_version, expected=LINK_VERSION):
        super().__init__(f"peer speaks wire version {peer_version!r}, expected {expected}")
        self.peer_version = peer_version


def hkdf_sha256(key_material: bytes, salt: bytes, info: bytes) -> bytes:
    """HKDF-SHA256 (RFC 5869) for one 32-byte block, the only length either key needs."""
    extracted = hmac.new(salt, key_material, hashlib.sha256).digest()
    return hmac.new(extracted, info + b"\x01", hashlib.sha256).digest()


def send_msg(sock: socket.socket, session, msg: dict) -> None:
    with session.send_lock:
        sock.sendall(session.seal(msg))


def legacy_frame(msg: dict) -> bytes:
    """The pre-v4 cleartext framing, kept only so an older peer can be told to
    update in words it understands rather than left waiting on silence."""
    body = json.dumps(msg).encode("utf-8")
    return struct.pack(">I", len(body)) + body


def _recv_exact(sock: socket.socket, n: int, deadline: float = None, idle: float = None) -> bytes:
    """`deadline` is a time.monotonic() moment the whole read must finish by.
    A socket timeout alone bounds each recv, not the read: a peer sending one
    byte every few seconds would hold a handshake open for as long as it
    liked. Past the deadline this raises socket.timeout like a quiet peer.
    `idle`, when given, still bounds each recv."""
    chunks = []
    remaining = n
    while remaining > 0:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout("deadline passed while reading")
            sock.settimeout(left if idle is None else min(left, idle))
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionClosed("socket closed while reading")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_msg(sock: socket.socket, session, limit: int = MAX_FRAME_BYTES, deadline: float = None) -> dict:
    """`limit` is the most a frame may claim before it is read. The receiver
    passes HELLO_MAX_BYTES for the first frame: until that frame's tag has
    been checked the peer has proved nothing, so it must not be able to make
    this side buffer a clipboard-sized frame per connection. `deadline`
    bounds the whole read the same way, for the same reason."""
    return session.open(recv_frame(sock, limit, deadline))


def recv_frame(sock: socket.socket, limit: int = MAX_FRAME_BYTES, deadline: float = None) -> bytes:
    """One frame as it came, after its length (held to `limit`) and before it is opened. Without a
    `deadline` the wait for the frame's first byte is the socket's own timeout (the link's
    liveness), and the frame then has FRAME_COMPLETE_SECONDS from that byte to arrive whole
    (WIRE.md section 2, frames), each read still bounded by that timeout."""
    if deadline is not None:
        header = _recv_exact(sock, HEADER_SIZE, deadline)
        (length,) = struct.unpack(">I", header)
        if length < 1 or length > limit:
            raise ProtocolError(f"invalid message length: {length}")
        return _recv_exact(sock, length, deadline)
    idle = sock.gettimeout()
    first = _recv_exact(sock, 1)
    deadline = time.monotonic() + FRAME_COMPLETE_SECONDS
    try:
        header = first + _recv_exact(sock, HEADER_SIZE - 1, deadline, idle)
        (length,) = struct.unpack(">I", header)
        if length < 1 or length > limit:
            raise ProtocolError(f"invalid message length: {length}")
        return _recv_exact(sock, length, deadline, idle)
    finally:
        try:
            sock.settimeout(idle)
        except OSError:
            pass  # closed meanwhile by another thread: what the read raised is the news


def ping_msg() -> dict:
    """Sent by the sender while idle so a dead connection is detected quickly.
    Carries no `seq` and must never be recorded as a processed input event."""
    return {"type": MSG_PING, "data": {}}


def scroll_msg(dy, dx=0.0, mode: str = "line") -> dict:
    """Scroll event. `mode` is "pixel" for continuous trackpad deltas (float
    px) or "line" for discrete wheel clicks. `dx` and `mode` are optional on
    receive for backward tolerance: a missing `dx` means 0, a missing `mode`
    means "line"."""
    return {"type": MSG_SCROLL, "data": {"dy": dy, "dx": dx, "mode": mode}}


def clipboard_msg(text: str = None, image: bytes = None) -> dict:
    """Whatever was on the clipboard at the switch: `text`, and an optional PNG under `image`
    (base64) with `image_format` naming it. Each key is omitted when absent (WIRE.md section 3)."""
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
    return image if png_fits(image) else None


def png_fits(image) -> bool:
    """Whether a clipboard image may be sent, or set where something will decode it (WIRE.md
    section 3), judged from its bytes alone: at most CLIPBOARD_IMAGE_MAX_BYTES; the PNG signature,
    then IHDR, whole, with each side 1 to PNG_MAX_SIDE pixels and PNG_MAX_PIXELS in all; every
    chunk inside the file, up to IEND; and the metadata a decoder inflates (iCCP, zTXt, a
    compressed iTXt) at most PNG_METADATA_MAX_BYTES in all. Nothing here touches the pixels: a PNG of a few
    megabytes can decode to gigabytes, and these bounds are what keeps that from happening."""
    if not isinstance(image, (bytes, bytearray)) or len(image) > CLIPBOARD_IMAGE_MAX_BYTES or not image.startswith(PNG_SIGNATURE):
        return False
    at, budget = len(PNG_SIGNATURE), PNG_METADATA_MAX_BYTES
    while at + 12 <= len(image):
        length, kind = struct.unpack_from(">I4s", image, at)
        data_at = at + 8
        if data_at + length + 4 > len(image):
            return False
        data = bytes(image[data_at:data_at + length])
        if at == len(PNG_SIGNATURE):
            if kind != b"IHDR" or length != 13:
                return False
            width, height = struct.unpack_from(">II", data)
            if not (1 <= width <= PNG_MAX_SIDE and 1 <= height <= PNG_MAX_SIDE) or width * height > PNG_MAX_PIXELS:
                return False
        elif kind in (b"IHDR", b"acTL", b"fcTL", b"fdAT"):
            # A clipboard image is a still with one header: these would claim sizes unchecked.
            return False
        elif kind in (b"iCCP", b"zTXt", b"iTXt"):
            budget = _inflated_within(_png_compressed(kind, data), budget)
            if budget is None:
                return False
        elif kind == b"IEND":
            return True
        at = data_at + length + 4
    return False


def _png_compressed(kind: bytes, data: bytes):
    """The zlib stream inside an iCCP, zTXt or compressed iTXt, False for an uncompressed iTXt,
    or None when the chunk is not shaped as the PNG specification says."""
    name_end = data.find(b"\x00")
    if name_end < 0:
        return None
    if kind != b"iTXt":
        return data[name_end + 2:]
    flag = data[name_end + 1:name_end + 2]
    if flag == b"\x00":
        return False
    language_end = data.find(b"\x00", name_end + 3)
    translated_end = data.find(b"\x00", language_end + 1) if language_end >= 0 else -1
    return data[translated_end + 1:] if flag == b"\x01" and translated_end >= 0 else None


def _inflated_within(stream, budget):
    """What is left of `budget` once `stream` is inflated, or None when it would pass the budget
    or is not one whole zlib stream ending where the chunk does (not zlib, cut short, or with
    bytes after its end). Inflates at most one byte past the budget."""
    if stream is None:
        return None
    if stream is False:
        return budget
    inflater = zlib.decompressobj()
    try:
        inflated = inflater.decompress(stream, budget + 1)
    except zlib.error:
        return None
    if len(inflated) > budget or not inflater.eof or inflater.unused_data:
        return None
    return budget - len(inflated)


def settings_msg(data: dict) -> dict:
    """Same on all machines: whether it is on, its stamp and, while on, the shared values, as
    settings_sync.message_data builds them. Sent on every change while on and announced on every
    new connection; an older Beamer ignores the type."""
    return {"type": MSG_SETTINGS, "data": dict(data)}


def gesture_msg(name: str) -> dict:
    """One completed system gesture (a GESTURE_* name). Unknown message types are
    ignored by the receiver, so this needs no protocol version bump."""
    return {"type": MSG_GESTURE, "data": {"name": name}}


LINK_SALT = b"beamer-link-v6"
KEY_ID_INFO = b"beamer-key-id"
KEY_ID_SIZE = 16
MACHINE_ID_SIZE = 16
SHARE_SIZE = 32
TOKEN_SIZE = 32
PREAMBLE_HEAD_SIZE = len(WIRE_MAGIC) + 1
LINK_PREAMBLE_SIZE = PREAMBLE_HEAD_SIZE + NONCE_PREFIX_SIZE + KEY_ID_SIZE + SHARE_SIZE
SHORT_REPLY_SIZE = PREAMBLE_HEAD_SIZE + NONCE_PREFIX_SIZE
# The smallest frame: a counter and a tag around an empty plaintext.
MIN_FRAME_BYTES = COUNTER_SIZE + 1 + crypto_aead_chacha20poly1305_ietf_ABYTES

ROLE_INITIATOR = "initiator"
ROLE_RESPONDER = "responder"
# The direction byte in `info`: whose frames the key seals.
_DIRECTION = {ROLE_INITIATOR: 1, ROLE_RESPONDER: 2}

MSG_ACCEPT = "accept"
MSG_REFUSE = "refuse"
MSG_ACCEPTS = "accepts"
MSG_PAIRED = "paired"
MSG_TEXT = "text"
INPUT_TYPES = frozenset({MSG_KEYDOWN, MSG_KEYUP, MSG_MOUSEMOVE, MSG_MOUSEDOWN, MSG_MOUSEUP, MSG_SCROLL, MSG_GESTURE, MSG_TEXT})

ERROR_INVALID_HELLO = "invalid_hello"
ERROR_WRONG_ID = "wrong_id"
WELCOME_ERRORS = (ERROR_INVALID_HELLO, ERROR_WRONG_ID)
REFUSE_REASONS = ("owned", "not_allowed", "busy", "sent_home", "malformed")

PLATFORMS = ("macos", "windows", "linux", "ios", "android")
EDGES = ("left", "right", "top", "bottom")
BUTTONS = ("left", "right", "middle", "back", "forward")
SCROLL_MODES = ("pixel", "line")

MAX_SAFE_INTEGER = 2**53 - 1
HELD_US_MAX = 2**31 - 1
SET_AT_MAX = 2**53 - 2
SET_AT_AHEAD = 86400
MAX_PEERS = 32
MAX_CAPS = 16
MAX_NAME_CHARS = 48
MAX_ASCII_FIELD = 32
MAX_KEY_CHARS = 32
MOVE_MAX = 65535
DEFAULT_RESISTANCE_PX = 120
MAX_RESISTANCE_PX = 1000
TEXT_MAX_BYTES = 4096

_PLATFORM_SHAPE = re.compile(r"[a-z0-9_]{1,16}")
_HW_SHAPE = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")
_PRINTABLE_ASCII = re.compile(r"[\x20-\x7e]+")
# A lone surrogate can only reach a parsed string through a \u escape: UTF-8 cannot carry one.
_SURROGATE_ESCAPE = re.compile(r"\\u[dD][89a-fA-F]")


class PreambleRefused(ProtocolError):
    """A version 6 preamble this side will not answer or go on from: another pair's key id, this
    side's own prefix back, or an ephemeral share that gives X25519's all-zero result."""


class NotAnObject(ProtocolError):
    """A frame opened, its tag and counter checked, but its plaintext is not a JSON object. On a
    later frame it ends the link like any ProtocolError; as the first frame to a responder it is
    the one failure answered, with `invalid_hello_v6()`, since only a token holder sealed it."""


class InvalidHello(ProtocolError):
    """The first frame opened but is not a valid `hello`: answered with `invalid_hello_v6()`."""


class InvalidWelcome(ProtocolError):
    """The first frame back opened but is not a valid `welcome`."""


class HelloRefused(ProtocolError):
    """The responder answered with an error `welcome`; `error` is `invalid_hello` or `wrong_id`."""

    def __init__(self, error):
        super().__init__(f"the peer refused this hello: {error}")
        self.error = error


def id_text(ident: bytes) -> str:
    """A machine id, a token or a QR secret as PAIRING.md's `b64`."""
    return base64.urlsafe_b64encode(bytes(ident)).decode("ascii").rstrip("=")


def _from_b64(text, size):
    """The bytes `text` spells, only when it is the one spelling `id_text` writes of `size` bytes."""
    if not isinstance(text, str) or not text.isascii():
        return None
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None
    if len(data) != size or id_text(data) != text:
        return None
    return data


def read_id(text):
    """A machine id's 16 bytes from its `b64`, or None: another length, another spelling, or all
    zero."""
    ident = _from_b64(text, MACHINE_ID_SIZE)
    return None if ident is None or ident == bytes(MACHINE_ID_SIZE) else ident


def is_paired_token(token) -> bool:
    """Whether `token` has the shape pairing gives: the `b64` of 32 bytes, in its one spelling.
    Anything else is a typed token. The shape proves nothing about where a token came from (a
    user could type 43 "A"s); `linkable` is what decides whether an entry links."""
    return _from_b64(token, TOKEN_SIZE) is not None


def linkable(entry) -> bool:
    """Whether a peer entry may link, and so have a key id: its token was made by 1.5.0's pairing.
    An entry migrated from 1.4.x never links, whatever its token looks like, because 1.4.x kept
    no record of whether its token was paired or typed, and a key id is an offline test of its
    token (WIRE.md sections 1 and 2)."""
    return isinstance(entry, dict) and entry.get("from_1_4") is not True and is_paired_token(entry.get("token"))


def key_id(token: str) -> bytes:
    """The pair's key id: the first 16 bytes of HKDF over the token's text. Refused for a typed
    token, because a key id is an offline test of its token."""
    if not is_paired_token(token):
        raise ValueError("a typed token has no key id")
    return hkdf_sha256(token.encode("ascii"), LINK_SALT, KEY_ID_INFO)[:KEY_ID_SIZE]


def link_ikm(token: str, shared: bytes) -> bytes:
    """75 bytes for a token made by pairing: its 43 characters of text, then the X25519 result."""
    return token.encode("ascii") + bytes(shared)


def link_info(key_id_: bytes, prefix_i: bytes, prefix_r: bytes, share_i: bytes, share_r: bytes, direction: int) -> bytes:
    """111 bytes: label, version, key id, both prefixes and both shares in role order, direction."""
    parts = ((key_id_, KEY_ID_SIZE), (prefix_i, NONCE_PREFIX_SIZE), (prefix_r, NONCE_PREFIX_SIZE), (share_i, SHARE_SIZE), (share_r, SHARE_SIZE))
    if any(len(part) != size for part, size in parts) or direction not in (1, 2):
        raise ValueError("link_info takes fixed-size parts and a direction of 1 or 2")
    return KEY_INFO + bytes([LINK_VERSION]) + b"".join(bytes(part) for part, _ in parts) + bytes([direction])


def link_key(token, shared, key_id, prefix_i, prefix_r, share_i, share_r, direction) -> bytes:
    """The key for one direction of one link (WIRE.md section 2, the keys)."""
    return hkdf_sha256(link_ikm(token, shared), LINK_SALT, link_info(key_id, prefix_i, prefix_r, share_i, share_r, direction))


def _x25519_shared(secret: bytes, share: bytes) -> bytes:
    # libsodium refuses a low-order share itself; the zero check stands for any X25519 that
    # does not, since every low-order share gives zeros and nothing else does.
    try:
        shared = crypto_scalarmult(secret, share)
    except CryptoError:
        raise PreambleRefused("the peer's ephemeral share is of low order") from None
    if shared == bytes(SHARE_SIZE):
        raise PreambleRefused("the peer's ephemeral share is of low order")
    return shared


def split_preamble(data: bytes):
    """(prefix, key id, share) from a whole version 6 preamble."""
    data = bytes(data)
    if len(data) != LINK_PREAMBLE_SIZE or data[:PREAMBLE_HEAD_SIZE] != WIRE_MAGIC + bytes([LINK_VERSION]):
        raise ProtocolError("not a version 6 preamble")
    prefix_end = PREAMBLE_HEAD_SIZE + NONCE_PREFIX_SIZE
    key_id_end = prefix_end + KEY_ID_SIZE
    return data[PREAMBLE_HEAD_SIZE:prefix_end], data[prefix_end:key_id_end], data[key_id_end:]


def short_reply(prefix: bytes = None) -> bytes:
    """What a version 6 responder answers an older or newer preamble with: 14 bytes, no key id,
    which a version 5 Beamer reads as a newer peer."""
    return WIRE_MAGIC + bytes([LINK_VERSION]) + (os.urandom(NONCE_PREFIX_SIZE) if prefix is None else bytes(prefix))


def legacy_version_reply() -> bytes:
    """What a version 6 responder answers a Beamer from before version 4 with."""
    return legacy_frame({"type": MSG_WELCOME, "data": {"version": LINK_VERSION, "error": "version_mismatch"}})


def recv_link_preamble(sock: socket.socket, deadline: float = None) -> bytes:
    """A peer's whole version 6 preamble. Reads 6 bytes first, and raises VersionMismatch naming
    the peer's version (None for a Beamer from before version 4, which sent no preamble) before
    reading anything a version 6 peer would not have sent: after an older version's 6 bytes it
    reads only the 8 left of its 14."""
    head = _recv_exact(sock, PREAMBLE_HEAD_SIZE, deadline)
    if head[: len(WIRE_MAGIC)] != WIRE_MAGIC:
        raise VersionMismatch(None, LINK_VERSION)
    version = head[len(WIRE_MAGIC)]
    if version < LINK_VERSION:
        try:
            _recv_exact(sock, NONCE_PREFIX_SIZE, deadline)
        except (ConnectionClosed, OSError):
            pass
        raise VersionMismatch(version, LINK_VERSION)
    if version > LINK_VERSION:
        raise VersionMismatch(version, LINK_VERSION)
    return head + _recv_exact(sock, LINK_PREAMBLE_SIZE - PREAMBLE_HEAD_SIZE, deadline)


class Message(dict):
    """A parsed message. `malformed` is set when its text broke a rule JSON parsers disagree on
    (WIRE.md, Encodings): a byte order mark, NaN or an infinity, a repeated key, a lone surrogate.
    The offending value, where there is one, reads as MALFORMED, so a validator refuses the field;
    the flag makes the message as a whole malformed even where that field is one it ignores."""

    malformed = False


class _Malformed:
    def __repr__(self):
        return "MALFORMED"


MALFORMED = _Malformed()


def parse_message(plain: bytes) -> Message:
    """A frame's plaintext as a Message. Raises NotAnObject when it is not a JSON object at all,
    which ends the link (and, for the first frame, is answered `invalid_hello`)."""
    flags = []
    while plain.startswith(b"\xef\xbb\xbf"):
        flags.append("byte order mark")
        plain = plain[3:]
    try:
        text = plain.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NotAnObject("message body is not UTF-8") from exc

    def constant(name):
        flags.append(name)
        return MALFORMED

    def number(text_):
        value = float(text_)
        if not math.isfinite(value):
            flags.append(text_)
            return MALFORMED
        return value

    def integer(text_):
        # Python refuses to parse an integer of thousands of digits; nothing here allows more than 16.
        return int(text_) if len(text_) <= 32 else MALFORMED

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                flags.append(key)
                value = MALFORMED
            result[key] = value
        return result

    try:
        value = json.loads(text, parse_constant=constant, parse_float=number, parse_int=integer, object_pairs_hook=pairs)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise NotAnObject(f"malformed message body: {exc}") from exc
    if not isinstance(value, dict):
        raise NotAnObject("message body must be a JSON object")
    if _SURROGATE_ESCAPE.search(text):
        try:
            json.dumps(value, ensure_ascii=False, default=repr).encode("utf-8")
        except UnicodeEncodeError:
            flags.append("lone surrogate")
    message = Message(value)
    message.malformed = bool(flags)
    return message


def dumps_message(msg: dict) -> bytes:
    """A message as version 6 sends it: UTF-8, compact, never NaN, never a lone surrogate
    (either raises ValueError)."""
    return json.dumps(msg, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


class LinkSession:
    """One version 6 link's handshake and cipher state, for one role. Build a new one per link: it
    draws a fresh nonce prefix and ephemeral X25519 secret, and starts both counters at zero. The
    initiator sends `preamble()` first; the responder sends its own only after `accept_preamble`
    has taken the initiator's, because its prefix must differ from it and its share is useless
    before the other is checked. Neither key exists until then, and once they are made the
    ephemeral secret is dropped. `send_lock` serialises sealing, and send_msg holds it across the
    write too, so counters are never reused and frames reach the wire in counter order; `open` is
    expected to run on a single reader thread per link."""

    def __init__(self, token: str, role: str, prefix: bytes = None, secret: bytes = None):
        if role not in _DIRECTION:
            raise ValueError(f"role must be {ROLE_INITIATOR!r} or {ROLE_RESPONDER!r}")
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
        self.role = role
        self.key_id = key_id(token)
        self._secret = os.urandom(SHARE_SIZE) if secret is None else bytes(secret)
        if len(self._secret) != SHARE_SIZE:
            raise ValueError("an ephemeral secret is 32 bytes")
        self.share = crypto_scalarmult_base(self._secret)
        self.peer_share = None

    def preamble(self) -> bytes:
        if self.role == ROLE_RESPONDER and self.peer_prefix is None:
            raise ProtocolError("the responder answers the initiator's preamble, never goes first")
        return WIRE_MAGIC + bytes([LINK_VERSION]) + self.prefix + self.key_id + self.share

    def accept_preamble(self, data: bytes) -> None:
        if self.peer_prefix is not None:
            raise ProtocolError("second preamble on one connection")
        if self._secret is None:
            raise ProtocolError("this session refused a preamble; build a new one")
        try:
            peer_prefix, peer_key_id, peer_share = split_preamble(data)
            if not hmac.compare_digest(peer_key_id, self.key_id):
                raise PreambleRefused("the peer's preamble names another pair's key id")
            if self.role == ROLE_RESPONDER:
                while self.prefix == peer_prefix:
                    self.prefix = os.urandom(NONCE_PREFIX_SIZE)
            elif peer_prefix == self.prefix:
                raise PreambleRefused("the peer sent this side's own nonce prefix back")
            shared = _x25519_shared(self._secret, peer_share)
        finally:
            self._secret = None
        if self.role == ROLE_INITIATOR:
            order = (self.prefix, peer_prefix, self.share, peer_share)
        else:
            order = (peer_prefix, self.prefix, peer_share, self.share)
        sending = _DIRECTION[self.role]
        with self.send_lock:
            self._send_key = link_key(self._token, shared, self.key_id, *order, sending)
        self._recv_key = link_key(self._token, shared, self.key_id, *order, 3 - sending)
        self.peer_share = peer_share
        self.peer_prefix = peer_prefix

    def seal(self, msg: dict) -> bytes:
        return self.seal_raw(dumps_message(msg))

    def seal_raw(self, body: bytes) -> bytes:
        with self.send_lock:
            if self._send_key is None:
                raise ProtocolError("nothing is sealed before the peer's preamble")
            if self._send_counter >= MAX_COUNTER:
                raise ProtocolError("frame counter exhausted; reconnect")
            self._send_counter += 1
            counter = struct.pack(">I", self._send_counter)
            ciphertext = crypto_aead_chacha20poly1305_ietf_encrypt(body, None, self.prefix + counter, self._send_key)
        return struct.pack(">I", COUNTER_SIZE + len(ciphertext)) + counter + ciphertext

    def open(self, body: bytes) -> Message:
        return parse_message(self.open_raw(body))

    def open_raw(self, body: bytes) -> bytes:
        """The frame's plaintext, once its counter and tag check out."""
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
        return plain


def recv_first_msg(sock: socket.socket, session: LinkSession, deadline: float = None) -> Message:
    """The first frame each way: its length held to 21 to 4096 before it is read. A responder
    answers `invalid_hello` to NotAnObject and to InvalidHello from read_hello, and closes with
    nothing sent on any other ProtocolError (a length out of range, the counter, a failed tag)."""
    header = _recv_exact(sock, HEADER_SIZE, deadline)
    (length,) = struct.unpack(">I", header)
    if length < MIN_FRAME_BYTES or length > HELLO_MAX_BYTES:
        raise ProtocolError(f"invalid first frame length: {length}")
    return session.open(_recv_exact(sock, length, deadline))


# Validators. Each takes a parsed message and returns its fields, checked and in Python types, or
# None when it is malformed, except read_hello, read_welcome and read_ack, which raise, because a
# malformed first frame or `ack` ends the link.

def _int(value, low, high) -> bool:
    return type(value) is int and low <= value <= high


def _number(value, low, high) -> bool:
    # An int is never infinite, and math.isfinite overflows on one past a float's range.
    return type(value) in (int, float) and (type(value) is int or math.isfinite(value)) and low <= value <= high


def _one_of(value, choices) -> bool:
    """Membership that a list or dict from the wire cannot make raise."""
    return isinstance(value, str) and value in choices


def _utf8_size(value):
    """The UTF-8 length of a string, or None for one holding a lone surrogate."""
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def _text(value, low, high) -> bool:
    return isinstance(value, str) and low <= len(value) <= high


def _ascii_field(value) -> bool:
    return _text(value, 1, MAX_ASCII_FIELD) and _PRINTABLE_ASCII.fullmatch(value) is not None


def valid_name(value) -> bool:
    """1 to 48 code points of valid Unicode (PAIRING.md's rule)."""
    if not _text(value, 1, MAX_NAME_CHARS):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def valid_platform(value) -> bool:
    """One of PLATFORMS, or any other 1 to 16 of a-z, 0-9 and _ (keyed as linux)."""
    return isinstance(value, str) and _PLATFORM_SHAPE.fullmatch(value) is not None


def _ids(value, limit=MAX_PEERS, repeats=True):
    if not isinstance(value, list) or len(value) > limit:
        return None
    ids = [read_id(item) for item in value]
    if None in ids or (not repeats and len(set(ids)) != len(ids)):
        return None
    return ids


def _clamp_offset(value) -> float:
    return float(min(1.0, max(0.0, value)))


def read_message(message):
    """(type, data) when `message` has a string `type` and an object `data` and broke none of the
    strictness rules, else None."""
    if not isinstance(message, dict) or getattr(message, "malformed", False):
        return None
    kind, data = message.get("type"), message.get("data")
    if not isinstance(kind, str) or not isinstance(data, dict):
        return None
    return kind, data


def _identity_fields(data, error):
    """The fields `hello` and `welcome` share, checked; raises `error` on any that is not valid."""
    ident = read_id(data.get("id"))
    caps = data.get("caps")
    hw = data.get("hw")
    if (
        not _int(data.get("version"), LINK_VERSION, LINK_VERSION)
        or ident is None
        or not valid_name(data.get("name"))
        or not valid_platform(data.get("platform"))
        or not _ascii_field(data.get("app"))
        or not isinstance(caps, list)
        or len(caps) > MAX_CAPS
        or not all(_ascii_field(cap) for cap in caps)
        or ("hw" in data and not (isinstance(hw, str) and _HW_SHAPE.fullmatch(hw)))
    ):
        raise error("malformed first frame")
    return {
        "version": LINK_VERSION,
        "id": ident,
        "name": data["name"],
        "platform": data["platform"],
        "app": data["app"],
        "caps": list(caps),
        "hw": hw if "hw" in data else None,
    }


def read_hello(message) -> dict:
    """A valid `hello`'s fields, or InvalidHello. `id` comes back as 16 bytes and `hw` as None
    when it is absent."""
    parsed = read_message(message)
    if parsed is None or parsed[0] != MSG_HELLO:
        raise InvalidHello("the first frame is not a hello")
    data = parsed[1]
    fields = _identity_fields(data, InvalidHello)
    if not _int(data.get("port"), 0, 65535):
        raise InvalidHello("malformed first frame")
    fields["port"] = data["port"]
    return fields


def read_welcome(message) -> dict:
    """A valid `welcome`'s fields, HelloRefused for an `invalid_hello` or `wrong_id` answer, or
    InvalidWelcome."""
    parsed = read_message(message)
    if parsed is None or parsed[0] != MSG_WELCOME:
        raise InvalidWelcome("the first frame back is not a welcome")
    data = parsed[1]
    if "error" in data:
        if _int(data.get("version"), LINK_VERSION, LINK_VERSION) and _one_of(data["error"], WELCOME_ERRORS):
            raise HelloRefused(data["error"])
        raise InvalidWelcome("a welcome with an unknown error")
    fields = _identity_fields(data, InvalidWelcome)
    if type(data.get("accepts")) is not bool:
        raise InvalidWelcome("malformed first frame")
    fields["accepts"] = data["accepts"]
    return fields


def _identity(machine_id, name, platform, app, caps, hw):
    # The sender cuts its name, as PAIRING.md says: a hostname can be 63 characters.
    name = name[:MAX_NAME_CHARS]
    if not name:
        raise ValueError("a machine's name is never empty")
    data = {"version": LINK_VERSION, "id": id_text(machine_id), "name": name, "platform": platform, "app": app, "caps": list(caps)}
    if hw:
        data["hw"] = hw
    return data


def hello_v6(machine_id: bytes, name: str, platform: str, app: str, caps, port: int, hw: str = None) -> dict:
    data = _identity(machine_id, name, platform, app, caps, hw)
    data["port"] = port
    return {"type": MSG_HELLO, "data": data}


def welcome_v6(machine_id: bytes, name: str, platform: str, app: str, caps, accepts: bool, hw: str = None) -> dict:
    data = _identity(machine_id, name, platform, app, caps, hw)
    data["accepts"] = bool(accepts)
    return {"type": MSG_WELCOME, "data": data}


def invalid_hello_v6() -> dict:
    return {"type": MSG_WELCOME, "data": {"version": LINK_VERSION, "error": ERROR_INVALID_HELLO}}


def wrong_id_v6() -> dict:
    return {"type": MSG_WELCOME, "data": {"version": LINK_VERSION, "error": ERROR_WRONG_ID}}


def focus_v6(route: int, target: bytes, edge: str = None, offset: float = None, resistance_px: int = None, reach=None, stay: bool = False) -> dict:
    """A `focus`. Leave everything but `route` and `target` out to let go; pass `reach` (and
    `resistance_px`) to take, with `edge` and `offset` for a zone, or with `stay` to re-arm."""
    data = {"route": route, "target": id_text(target)}
    if (edge is None) != (offset is None):
        raise ValueError("edge and offset go together")
    if edge is not None:
        data["edge"] = edge
        data["offset"] = offset
    if resistance_px is not None:
        data["resistance_px"] = resistance_px
    if reach is not None:
        data["reach"] = [id_text(ident) for ident in reach]
    if stay:
        data["stay"] = True
    return {"type": MSG_FOCUS, "data": data}


def read_focus(message, own_id: bytes):
    """A `focus` as the responder `own_id` reads it (WIRE.md sections 4 and 5), or None when its
    `route` or `target` is malformed, which cannot be answered and is ignored.

    `here` says it names this machine. A let-go (`here` false) carries only `route` and `target`,
    and any other field is ignored; a let-go that is malformed as a whole is None. A take or
    re-arm comes back with `malformed` set when any other field is, which the responder answers
    with `refuse {why: "malformed"}`. `reach` is kept as sent: the caller drops the owner and
    itself from it."""
    if not isinstance(message, dict) or message.get("type") != MSG_FOCUS or not isinstance(message.get("data"), dict):
        return None
    data = message["data"]
    route, target = data.get("route"), read_id(data.get("target"))
    if not _int(route, 1, MAX_SAFE_INTEGER) or target is None:
        return None
    here = target == bytes(own_id)
    focus = {"route": route, "target": target, "here": here, "malformed": False, "edge": None, "offset": None,
             "resistance_px": DEFAULT_RESISTANCE_PX, "reach": [], "stay": False}
    if not here:
        return None if getattr(message, "malformed", False) else focus
    malformed = getattr(message, "malformed", False)
    if "stay" in data:
        focus["stay"] = data["stay"] is True
        malformed = malformed or not focus["stay"]
    if not focus["stay"] and ("edge" in data or "offset" in data):
        if _one_of(data.get("edge"), EDGES) and _number(data.get("offset"), -math.inf, math.inf):
            focus["edge"], focus["offset"] = data["edge"], _clamp_offset(data["offset"])
        else:
            malformed = True
    if "resistance_px" in data:
        if _int(data["resistance_px"], 0, MAX_RESISTANCE_PX):
            focus["resistance_px"] = data["resistance_px"]
        else:
            malformed = True
    if "reach" in data:
        reach = _ids(data["reach"], repeats=False)
        if reach is None:
            malformed = True
        else:
            focus["reach"] = reach
    focus["malformed"] = malformed
    return focus


def accept_msg(route: int) -> dict:
    return {"type": MSG_ACCEPT, "data": {"route": route}}


def refuse_msg(route: int, why: str, other=None) -> dict:
    if why not in REFUSE_REASONS:
        raise ValueError(f"not a refusal: {why!r}")
    data = {"route": route, "why": why}
    if why in ("owned", "busy") and isinstance(other, bytes):
        text = id_text(other)
        if read_id(text) is not None:
            data["other"] = text
    return {"type": MSG_REFUSE, "data": data}


def accepts_msg(accepts: bool) -> dict:
    return {"type": MSG_ACCEPTS, "data": {"accepts": bool(accepts)}}


def read_accept(message):
    parsed = read_message(message)
    if parsed is None or parsed[0] != MSG_ACCEPT or not _int(parsed[1].get("route"), 1, MAX_SAFE_INTEGER):
        return None
    return {"route": parsed[1]["route"]}


def read_refuse(message):
    """`why` is kept as sent, and None when it is missing or not a string: an initiator treats
    either as a refusal it cannot name, rather than waiting out a take that was refused."""
    parsed = read_message(message)
    if parsed is None or parsed[0] != MSG_REFUSE:
        return None
    route, why = parsed[1].get("route"), parsed[1].get("why")
    if not _int(route, 1, MAX_SAFE_INTEGER):
        return None
    result = {"route": route, "why": why if isinstance(why, str) else None}
    other = read_id(parsed[1].get("other")) if why in ("owned", "busy") else None
    if other is not None:
        result["other"] = other
    return result


def read_accepts(message):
    parsed = read_message(message)
    if parsed is None or parsed[0] != MSG_ACCEPTS or type(parsed[1].get("accepts")) is not bool:
        return None
    return {"accepts": parsed[1]["accepts"]}


def switch_v6(route: int, next_id: bytes, edge: str = None, offset: float = None) -> dict:
    data = {"route": route, "next": id_text(next_id)}
    if (edge is None) != (offset is None):
        raise ValueError("edge and offset go together")
    if edge is not None:
        data["edge"] = edge
        data["offset"] = offset
    return {"type": MSG_SWITCH, "data": data}


def read_switch(message):
    parsed = read_message(message)
    if parsed is None or parsed[0] != MSG_SWITCH:
        return None
    data = parsed[1]
    route, next_id = data.get("route"), read_id(data.get("next"))
    if not _int(route, 1, MAX_SAFE_INTEGER) or next_id is None:
        return None
    edge = offset = None
    if "edge" in data or "offset" in data:
        if not _one_of(data.get("edge"), EDGES) or not _number(data.get("offset"), -math.inf, math.inf):
            return None
        edge, offset = data["edge"], _clamp_offset(data["offset"])
    return {"route": route, "next": next_id, "edge": edge, "offset": offset}


def ack_v6(seq: int, held_us: int, locked: bool = False) -> dict:
    """An `ack`, with `held_us` clamped to what the wire allows."""
    data = {"seq": seq, "held_us": min(HELD_US_MAX, max(0, int(held_us)))}
    if locked:
        data["locked"] = True
    return {"type": MSG_ACK, "data": data}


def read_ack(message) -> dict:
    """A valid `ack`'s fields; ProtocolError otherwise, which ends the link. Whether its `seq` goes
    backwards or ahead of what was sent is the link's to check."""
    parsed = read_message(message)
    if parsed is None or parsed[0] != MSG_ACK:
        raise ProtocolError("malformed ack")
    data = parsed[1]
    if not _int(data.get("seq"), 0, MAX_SAFE_INTEGER) or not _int(data.get("held_us"), 0, HELD_US_MAX):
        raise ProtocolError("malformed ack")
    if "locked" in data and data["locked"] is not True:
        raise ProtocolError("malformed ack")
    return {"seq": data["seq"], "held_us": data["held_us"], "locked": "locked" in data}


def _with_seq(kind, data, seq):
    if seq is not None:
        data["seq"] = seq
    return {"type": kind, "data": data}


def key_msg(kind: str, key: str, us: str = None, seq: int = None) -> dict:
    """`keydown` or `keyup`; the link adds `seq` when it is left out here."""
    data = {"key": key}
    if us is not None:
        data["us"] = us
    return _with_seq(kind, data, seq)


def mousemove_msg(dx: int, dy: int, seq: int = None) -> dict:
    return _with_seq(MSG_MOUSEMOVE, {"dx": dx, "dy": dy}, seq)


def text_msg(text: str, seq: int = None) -> dict:
    return _with_seq(MSG_TEXT, {"text": text}, seq)


def read_input(message):
    """An input message's fields with `type` and `seq`, or None. A `text` over 4096 bytes comes
    back with `text` None: it is dropped whole but still acknowledged."""
    parsed = read_message(message)
    if parsed is None or parsed[0] not in INPUT_TYPES:
        return None
    kind, data = parsed
    seq = data.get("seq")
    if not _int(seq, 1, MAX_SAFE_INTEGER):
        return None
    read = {"type": kind, "seq": seq}
    if kind in (MSG_KEYDOWN, MSG_KEYUP):
        us = data.get("us")
        if not _text(data.get("key"), 1, MAX_KEY_CHARS) or ("us" in data and not _text(us, 1, 1)):
            return None
        read.update(key=data["key"], us=us if "us" in data else None)
    elif kind == MSG_MOUSEMOVE:
        if not _int(data.get("dx"), -MOVE_MAX, MOVE_MAX) or not _int(data.get("dy"), -MOVE_MAX, MOVE_MAX):
            return None
        read.update(dx=data["dx"], dy=data["dy"])
    elif kind in (MSG_MOUSEDOWN, MSG_MOUSEUP):
        if not _one_of(data.get("button"), BUTTONS):
            return None
        read.update(button=data["button"])
    elif kind == MSG_SCROLL:
        dx, mode = data.get("dx", 0), data.get("mode", "line")
        if not _number(data.get("dy"), -MOVE_MAX, MOVE_MAX) or not _number(dx, -MOVE_MAX, MOVE_MAX) or not _one_of(mode, SCROLL_MODES):
            return None
        read.update(dy=data["dy"], dx=dx, mode=mode)
    elif kind == MSG_GESTURE:
        if not _one_of(data.get("name"), GESTURE_NAMES):
            return None
        read.update(name=data["name"])
    else:
        text = data.get("text")
        size = _utf8_size(text) if isinstance(text, str) else None
        if size is None:
            return None
        read.update(text=text if size <= TEXT_MAX_BYTES else None)
    return read


def read_clipboard(message):
    """{text, image} from a `clipboard`, each None when absent or over its limit (that part is
    dropped and the rest stands), or None when the message is malformed."""
    parsed = read_message(message)
    if parsed is None or parsed[0] != MSG_CLIPBOARD:
        return None
    data = parsed[1]
    text = data.get("text")
    if ("text" in data and (not isinstance(text, str) or _utf8_size(text) is None)) or ("image" in data and not isinstance(data["image"], str)):
        return None
    if text is not None and _utf8_size(text) > CLIPBOARD_MAX_BYTES:
        text = None
    return {"text": text, "image": clipboard_image(data)}


def arrangement_v6(edge: str, set_at: int, by: bytes, way_back=None, way_back_by=None) -> dict:
    """`way_back` says whether a sender's zone leads here; `way_back_by` names a holder when it does not."""
    data = {"edge": edge, "set_at": int(set_at), "by": id_text(by)}
    if isinstance(way_back, bool):
        data["way_back"] = way_back
        if way_back is False and isinstance(way_back_by, bytes):
            text = id_text(way_back_by)
            if read_id(text) is not None:
                data["way_back_by"] = text
    return {"type": MSG_ARRANGEMENT, "data": data}


def read_arrangement_v6(message, now: float):
    """{edge, set_at, by} from an `arrangement`, with `way_back` when it carried a boolean one and,
    beside a `way_back` of false, `way_back_by` when it is a valid id; or None: malformed, or stamped
    more than a day ahead of `now` or above 2^53 - 2, which the receiver ignores. A `way_back` that is
    not a boolean is left out, and the side still stands."""
    parsed = read_message(message)
    if parsed is None or parsed[0] != MSG_ARRANGEMENT:
        return None
    data = parsed[1]
    by = read_id(data.get("by"))
    set_at = data.get("set_at")
    if not _one_of(data.get("edge"), EDGES) or by is None or not _int(set_at, 0, SET_AT_MAX) or set_at > now + SET_AT_AHEAD:
        return None
    read = {"edge": data["edge"], "set_at": set_at, "by": by}
    if isinstance(data.get("way_back"), bool):
        read["way_back"] = data["way_back"]
        if data["way_back"] is False:
            way_back_by = read_id(data.get("way_back_by"))
            if way_back_by is not None:
                read["way_back_by"] = way_back_by
    return read


def paired_msg(ids) -> dict:
    return {"type": MSG_PAIRED, "data": {"ids": [id_text(ident) for ident in ids]}}


def read_paired(message):
    """The ids in a `paired`, as 16-byte ids, or None."""
    parsed = read_message(message)
    if parsed is None or parsed[0] != MSG_PAIRED:
        return None
    return _ids(parsed[1].get("ids"))
