"""Independent phone peer from PAIRING.md and WIRE.md sections 2, 4, 5 and 6.

No production encoding, handshake, pairing or routing code is imported here: a desktop
and this phone sharing a mistake cannot make the interoperability test pass.
"""

import base64
import hashlib
import hmac
import json
import queue
import secrets
import socket
import struct
import threading
import time

from nacl.bindings import (
    crypto_aead_chacha20poly1305_ietf_decrypt,
    crypto_aead_chacha20poly1305_ietf_encrypt,
    crypto_scalarmult,
    crypto_scalarmult_base,
)
from nacl.exceptions import CryptoError


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def unb64(text):
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if b64(raw) != text:
        raise ValueError("non-canonical base64")
    return raw


def lv(*parts):
    result = bytearray()
    for part in parts:
        length = len(part)
        while length >= 128:
            result.append((length & 127) | 128)
            length >>= 7
        result.append(length)
        result.extend(part)
    return bytes(result)


def hkdf(ikm, salt, info, length):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    result, block = b"", b""
    for counter in range(1, (length + 31) // 32 + 1):
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        result += block
    return result[:length]


def exchange(private, public):
    try:
        return crypto_scalarmult(private, public)
    except CryptoError as error:
        raise ValueError("invalid X25519 share") from error


def read_exact(sock, count):
    result = bytearray()
    while len(result) < count:
        chunk = sock.recv(count - len(result))
        if not chunk:
            raise EOFError("the desktop closed the connection")
        result.extend(chunk)
    return bytes(result)


def pair_send(sock, message):
    body = json.dumps(dict(message, beamy=1), separators=(",", ":")).encode("utf-8")
    if not 1 <= len(body) <= 1024:
        raise ValueError("pairing frame outside its limits")
    sock.sendall(struct.pack(">I", len(body)) + body)


def pair_read(sock):
    length = struct.unpack(">I", read_exact(sock, 4))[0]
    if not 1 <= length <= 1024:
        raise ValueError("pairing frame outside its limits")
    message = json.loads(read_exact(sock, length))
    if message.get("beamy") != 1:
        raise ValueError("not a pairing message")
    return message


class Phone:
    def __init__(self, identity, name, platform):
        self.identity, self.name, self.platform = identity, name, platform
        self.peers, self.links = {}, {}
        self.route = 0
        self.on = None

    def pair(self, tcp_port, code, qr_secret=None):
        with socket.create_connection(("127.0.0.1", tcp_port), timeout=3) as sock:
            beacon = pair_read(sock)
            if beacon["type"] != "beacon" or beacon["pairing"] != 3:
                raise ValueError("not a version 3 host")
            pair_id = beacon["pair"]
            nonce = secrets.token_bytes(16)
            sid = lv(pair_id.encode("ascii"), nonce)
            password = code.encode("ascii") + (qr_secret or b"")
            padding = bytes(128 - 1 - len(lv(password)) - len(lv(b"CPace255")))
            generator_string = lv(b"CPace255", password, padding, b"beamer-pair-v3", sid)
            r = int.from_bytes(hashlib.sha512(generator_string).digest()[:32], "little") & ((1 << 255) - 1)
            p, a = (1 << 255) - 19, 486662
            v = (-a * pow(1 + 2 * r * r, -1, p)) % p
            e = pow((v**3 + a * v * v + v) % p, (p - 1) // 2, p)
            generator = (v if e == 1 else (-v - a) % p).to_bytes(32, "little")
            private = secrets.token_bytes(32)
            ya = exchange(private, generator)
            start = {
                "type": "pair_start", "pair": pair_id, "v": 3, "nonce": b64(nonce),
                "share": b64(ya), "name": self.name, "id": b64(self.identity),
                "platform": self.platform, "port": 0,
            }
            if qr_secret is not None:
                start["qr"] = 1
            pair_send(sock, start)
            answer = pair_read(sock)
            if answer["type"] != "pair_answer" or answer.get("ok") is not True or answer["pair"] != pair_id:
                raise ValueError("pairing refused")
            host_id = unb64(answer["id"])
            if len(host_id) != 16 or host_id == bytes(16) or host_id == self.identity or answer["id"] in self.peers:
                raise ValueError("invalid or known host id")
            yb = unb64(answer["share"])
            shared = exchange(private, yb)
            ada = lv(b"requester", self.identity, self.name.encode("utf-8"), self.platform.encode("ascii"), bytes(2))
            adb = lv(b"host", host_id, answer["name"].encode("utf-8"), answer["platform"].encode("ascii"),
                     answer["port"].to_bytes(2, "big"))
            isk = hashlib.sha512(lv(b"CPace255_ISK", sid, shared) + lv(ya, ada) + lv(yb, adb)).digest()
            mac_key = hashlib.sha512(b"CPaceMac" + sid + isk).digest()
            tag = lambda value: hmac.new(mac_key, value, hashlib.sha512).digest()
            if not hmac.compare_digest(unb64(answer["tag"]), tag(lv(yb, adb))):
                pair_send(sock, {"type": "pair_confirm", "pair": pair_id, "tag": ""})
                raise ValueError("host confirmation failed")
            pair_send(sock, {"type": "pair_confirm", "pair": pair_id, "tag": b64(tag(lv(ya, ada)))})
            done = pair_read(sock)
            if done["type"] != "pair_done" or done.get("ok") is not True or done["pair"] != pair_id:
                raise ValueError("pairing was not saved")
            if not hmac.compare_digest(unb64(done["tag"]), tag(lv(b"beamer-pair-done"))):
                raise ValueError("receipt failed")
            peer = {"id": answer["id"], "name": answer["name"], "platform": answer["platform"],
                    "host": "127.0.0.1", "port": answer["port"],
                    "token": b64(hkdf(isk, sid, b"beamer-pair-token-v3", 32))}
            self.peers[peer["id"]] = peer
            return peer

    def connect(self, peer):
        link = PhoneLink(self, self.peers[peer])
        self.links[peer] = link
        return link

    def take(self, peer):
        self.route += 1
        self.links[peer].send("focus", route=self.route, target=peer,
                              reach=[other for other in self.links if other != peer])
        self.on = peer

    def follow_switch(self, source, message):
        if source != self.on or message["route"] != self.route:
            raise ValueError("stale switch")
        target = message["next"]
        if target == b64(self.identity):
            self.route += 1
            self.links[source].release()
            self.links[source].send("focus", route=self.route, target=target)
            self.on = None
            return
        if target not in self.links:
            raise ValueError("switch target has no direct link")
        self.route += 1
        fields = {k: message[k] for k in ("edge", "offset") if k in message}
        self.links[target].send("focus", route=self.route, target=target,
                                reach=[p for p in self.links if p != target], **fields)
        answer = self.links[target].expect("accept")
        if answer != {"route": self.route}:
            raise ValueError("switch target did not accept")
        self.links[source].release()
        self.links[source].send("focus", route=self.route, target=target)
        self.on = target


class PhoneLink:
    def __init__(self, phone, peer):
        self.sock = socket.create_connection((peer["host"], peer["port"]), timeout=3)
        self.counter, self.received_counter, self.seq = 0, 0, 0
        self.keys, self.buttons = set(), set()
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.inbox = queue.Queue()
        self.pending = []
        self.prefix = secrets.token_bytes(8)
        private = secrets.token_bytes(32)
        share = crypto_scalarmult_base(private)
        token = peer["token"].encode("ascii")
        key_id = hkdf(token, b"beamer-link-v6", b"beamer-key-id", 16)
        try:
            self.sock.sendall(b"BEAMY\x06" + self.prefix + key_id + share)
            preamble = read_exact(self.sock, 62)
            if preamble[:6] != b"BEAMY\x06" or preamble[14:30] != key_id or preamble[6:14] == self.prefix:
                raise ValueError("wrong preamble")
            self.peer_prefix = preamble[6:14]
            shared = exchange(private, preamble[30:62])
            info = b"beamer-frames\x06" + key_id + self.prefix + self.peer_prefix + share + preamble[30:62]
            self.send_key = hkdf(token + shared, b"beamer-link-v6", info + b"\x01", 32)
            self.receive_key = hkdf(token + shared, b"beamer-link-v6", info + b"\x02", 32)
            self.send("hello", version=6, id=b64(phone.identity), name=phone.name,
                      platform=phone.platform, app="1.5.0", caps=[], port=0)
            welcome = self.read()
            if welcome["type"] != "welcome" or welcome["data"].get("error"):
                raise ValueError(welcome["data"].get("error", "not a welcome"))
            self.welcome = welcome["data"]
            if self.welcome["id"] != peer["id"] or self.welcome["version"] != 6:
                raise ValueError("wrong desktop id or version")
            self.sock.settimeout(None)
            threading.Thread(target=self._reader, daemon=True).start()
            threading.Thread(target=self._heartbeat, daemon=True).start()
        except BaseException:
            self.close()
            raise

    def send(self, kind, **data):
        body = json.dumps({"type": kind, "data": data}, separators=(",", ":")).encode("utf-8")
        with self.lock:
            self.counter += 1
            counter = struct.pack(">I", self.counter)
            sealed = crypto_aead_chacha20poly1305_ietf_encrypt(body, None, self.prefix + counter, self.send_key)
            self.sock.sendall(struct.pack(">I", 4 + len(sealed)) + counter + sealed)

    def read(self):
        length = struct.unpack(">I", read_exact(self.sock, 4))[0]
        if not 21 <= length <= 16 * 1024 * 1024:
            raise ValueError("bad frame length")
        frame = read_exact(self.sock, length)
        counter = struct.unpack(">I", frame[:4])[0]
        if counter != self.received_counter + 1:
            raise ValueError("bad frame counter")
        self.received_counter = counter
        plain = crypto_aead_chacha20poly1305_ietf_decrypt(frame[4:], None, self.peer_prefix + frame[:4], self.receive_key)
        return json.loads(plain)

    def _reader(self):
        try:
            while not self.closed.is_set():
                self.inbox.put(self.read())
        except (OSError, EOFError, ValueError, CryptoError):
            self.closed.set()

    def _heartbeat(self):
        while not self.closed.wait(0.8):
            try:
                self.send("ping")
            except OSError:
                self.closed.set()

    def input(self, kind, **data):
        self.seq += 1
        self.send(kind, seq=self.seq, **data)
        if kind == "keydown":
            self.keys.add(data["key"])
        elif kind == "keyup":
            self.keys.discard(data["key"])
        elif kind == "mousedown":
            self.buttons.add(data["button"])
        elif kind == "mouseup":
            self.buttons.discard(data["button"])

    def release(self):
        for key in list(self.keys):
            self.input("keyup", key=key)
        for button in list(self.buttons):
            self.input("mouseup", button=button)

    def expect(self, kind, timeout=2):
        for message in self.pending:
            if message["type"] == kind:
                self.pending.remove(message)
                return message["data"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self.inbox.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if message["type"] == kind:
                return message["data"]
            self.pending.append(message)
        return None

    def close(self):
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
