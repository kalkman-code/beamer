"""Wire version 5, frozen: the link of Beamer 1.4.x as core/protocol.py had it before version 6
replaced it (on libsodium, which reproduces 1.4.x's bytes exactly). No app speaks it any more; it
is kept beside the tests for two jobs only, the version 5 vector replay (test_wire_vectors.py) and
an older peer to refuse (WIRE.md section 2, Versions). Never change it: it is what a 1.4.x machine
sends, not what this one does. Errors are core/protocol.py's, so callers catch the one class."""

import json
import os
import struct
import threading

from nacl.bindings import (
    crypto_aead_chacha20poly1305_ietf_ABYTES,
    crypto_aead_chacha20poly1305_ietf_decrypt,
    crypto_aead_chacha20poly1305_ietf_encrypt,
)
from nacl.exceptions import CryptoError

from core.protocol import (
    AuthenticationError,
    ProtocolError,
    VersionMismatch,
    _recv_exact,
    hkdf_sha256,
)

WIRE_MAGIC = b"BEAMY"
PROTOCOL_VERSION = 5
NONCE_PREFIX_SIZE = 8
PREAMBLE_SIZE = len(WIRE_MAGIC) + 1 + NONCE_PREFIX_SIZE
HEADER_SIZE = 4
COUNTER_SIZE = 4
MAX_COUNTER = 0xFFFFFFFF
KEY_SALT = b"beamer-link-v5"
KEY_INFO = b"beamer-frames"


def derive_key(token: str, sender_prefix: bytes, receiver_prefix: bytes) -> bytes:
    return hkdf_sha256(token.encode("utf-8"), KEY_SALT, KEY_INFO + bytes(sender_prefix) + bytes(receiver_prefix))


class SecureSession:
    """One version 5 connection's cipher state for both directions."""

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
            raise VersionMismatch(None, PROTOCOL_VERSION)
        version = data[len(WIRE_MAGIC)]
        if version != PROTOCOL_VERSION:
            raise VersionMismatch(version, PROTOCOL_VERSION)
        peer_prefix = data[len(WIRE_MAGIC) + 1:]
        if self.peer_prefix is not None:
            raise ProtocolError("second preamble on one connection")
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
        body = bytes(memoryview(body))
        (counter,) = struct.unpack(">I", body[:COUNTER_SIZE])
        if counter != self._recv_counter + 1:
            raise ProtocolError("frame counter did not advance; replay rejected")
        sealed = body[COUNTER_SIZE:]
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


def recv_preamble(sock, session, deadline: float = None) -> None:
    session.accept_preamble(_recv_exact(sock, PREAMBLE_SIZE, deadline))


def hello_msg() -> dict:
    return {"type": "hello", "data": {"version": PROTOCOL_VERSION, "settings": 1}}


def welcome_msg(error: str = None) -> dict:
    data = {"version": PROTOCOL_VERSION, "settings": 1}
    if error is not None:
        data["error"] = error
    return {"type": "welcome", "data": data}
