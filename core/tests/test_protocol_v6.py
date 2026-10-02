"""Wire version 6 in protocol.py: the pure half of WIRE.md sections 2 to 5 and 9.

The key id, the preamble, the keys and X25519 are checked against HKDF and a Montgomery ladder
written here, never through protocol.py's own helpers, so a mistake in the module and a mistake in
the test cannot cancel out. The version 5 names protocol.py still carries are covered by
test_wire_vectors.py and the apps' suites; nothing here touches them.
"""

import base64
import hashlib
import hmac
import json
import os
import socket
import struct
import threading
import time
import unittest
import zlib

from nacl.bindings import crypto_aead_chacha20poly1305_ietf_decrypt

from core import protocol
from core.tests import pngs

TOKEN = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
OTHER_TOKEN = base64.urlsafe_b64encode(bytes(range(1, 33))).decode("ascii").rstrip("=")
ID_A = bytes(range(16, 32))
ID_B = bytes(range(32, 48))
ID_C = bytes(range(48, 64))

_P = 2**255 - 19

# X25519's low-order points (and the same with bit 255 set, which RFC 7748 ignores): every one
# gives the all-zero result.
LOW_ORDER = [
    bytes(32),
    b"\x01" + bytes(31),
    bytes.fromhex("e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800"),
    bytes.fromhex("5f9c95bca3508c24b1d0b1559c83ef5b04445cc4581c8e86d8224eddd09f1157"),
    (_P - 1).to_bytes(32, "little"),
    _P.to_bytes(32, "little"),
    (_P + 1).to_bytes(32, "little"),
]
LOW_ORDER += [point[:31] + bytes([point[31] | 0x80]) for point in LOW_ORDER]


def _b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _hkdf(ikm, salt, info, length):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def _x25519(scalar, u_bytes):
    """RFC 7748 section 5, written out: the clamped scalar, bit 255 of u ignored."""
    k = bytearray(scalar)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    k = int.from_bytes(k, "little")
    u = int.from_bytes(u_bytes, "little") & ((1 << 255) - 1)
    x1, x2, z2, x3, z3, swap = u, 1, 0, u, 1, 0
    for t in reversed(range(255)):
        bit = (k >> t) & 1
        swap ^= bit
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b = (x2 + z2) % _P, (x2 - z2) % _P
        aa, bb = a * a % _P, b * b % _P
        e = (aa - bb) % _P
        c, d = (x3 + z3) % _P, (x3 - z3) % _P
        da, cb = d * a % _P, c * b % _P
        x3, z3 = (da + cb) ** 2 % _P, x1 * (da - cb) ** 2 % _P
        x2, z2 = aa * bb % _P, e * (aa + 121665 * e) % _P
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def _key_id(token):
    return _hkdf(token.encode("ascii"), b"beamer-link-v6", b"beamer-key-id", 16)


def _key(token, shared, key_id, prefix_i, prefix_r, share_i, share_r, direction):
    info = b"beamer-frames" + b"\x06" + key_id + prefix_i + prefix_r + share_i + share_r + bytes([direction])
    return _hkdf(token.encode("ascii") + shared, b"beamer-link-v6", info, 32)


def _pair(token=TOKEN, prefix_i=b"I" * 8, prefix_r=b"R" * 8, secret_i=b"\x11" * 32, secret_r=b"\x22" * 32):
    initiator = protocol.LinkSession(token, protocol.ROLE_INITIATOR, prefix=prefix_i, secret=secret_i)
    responder = protocol.LinkSession(token, protocol.ROLE_RESPONDER, prefix=prefix_r, secret=secret_r)
    first = initiator.preamble()
    responder.accept_preamble(first)
    second = responder.preamble()
    initiator.accept_preamble(second)
    return initiator, responder, first, second


def _open_frame(session, frame):
    return session.open(frame[protocol.HEADER_SIZE:])


class Tokens(unittest.TestCase):
    def test_a_paired_token_is_43_characters_of_one_spelling(self):
        self.assertTrue(protocol.is_paired_token(TOKEN))
        # The last character carries four spare bits: a second spelling of the same 32 bytes.
        spare = TOKEN[:-1] + ("B" if TOKEN[-1] == "A" else chr(ord(TOKEN[-1]) + 1))
        self.assertEqual(base64.urlsafe_b64decode(spare + "="), base64.urlsafe_b64decode(TOKEN + "="))
        for typed in ("", "hunter2", TOKEN[:-1], TOKEN + "A", TOKEN + "=", TOKEN[:5] + "+" + TOKEN[6:], spare, 12, None):
            with self.subTest(typed=typed):
                self.assertFalse(protocol.is_paired_token(typed))

    def test_the_key_id_is_hkdf_over_the_tokens_text(self):
        for token in (TOKEN, OTHER_TOKEN, _b64(os.urandom(32))):
            with self.subTest(token=token):
                self.assertEqual(protocol.key_id(token), _key_id(token))
        self.assertNotEqual(protocol.key_id(TOKEN), protocol.key_id(OTHER_TOKEN))

    def test_only_an_entry_made_by_pairing_links(self):
        # 43 "A"s spell 32 zero bytes in the one spelling: the encoding proves nothing about where a
        # token came from, and 1.4.x kept no record of whether its token was paired or typed.
        zeros = "A" * 43
        self.assertTrue(protocol.is_paired_token(zeros))
        self.assertTrue(protocol.linkable({"token": TOKEN, "from_1_4": False}))
        for entry in ({"token": zeros, "from_1_4": True}, {"token": TOKEN, "from_1_4": True}, {"token": "hunter2", "from_1_4": False},
                      {"from_1_4": False}, None):
            with self.subTest(entry=entry):
                self.assertFalse(protocol.linkable(entry))

    def test_a_typed_token_has_no_key_id_and_no_session(self):
        with self.assertRaises(ValueError):
            protocol.key_id("hunter2")
        with self.assertRaises(ValueError):
            protocol.LinkSession("hunter2", protocol.ROLE_INITIATOR)
        with self.assertRaises(ValueError):
            protocol.LinkSession(TOKEN, "server")


class Ids(unittest.TestCase):
    def test_round_trip(self):
        for ident in (ID_A, b"\xff" * 16, b"\x00" * 15 + b"\x01"):
            text = protocol.id_text(ident)
            self.assertEqual(len(text), 22)
            self.assertEqual(protocol.read_id(text), ident)

    def test_refused(self):
        good = protocol.id_text(ID_A)
        spare = good[:-1] + ("B" if good[-1] == "A" else chr(ord(good[-1]) + 1))
        for bad in (protocol.id_text(bytes(16)), _b64(bytes(15)), _b64(bytes(17)), good + "=", spare, good[:5] + "/" + good[6:], "", 5, None, ["x"]):
            with self.subTest(bad=bad):
                self.assertIsNone(protocol.read_id(bad))


class Preamble(unittest.TestCase):
    def test_layout(self):
        secret = b"\x33" * 32
        session = protocol.LinkSession(TOKEN, protocol.ROLE_INITIATOR, prefix=b"P" * 8, secret=secret)
        data = session.preamble()
        self.assertEqual(len(data), 62)
        self.assertEqual(protocol.LINK_PREAMBLE_SIZE, 62)
        self.assertEqual(data[:5], b"BEAMY")
        self.assertEqual(data[5], 6)
        self.assertEqual(data[6:14], b"P" * 8)
        self.assertEqual(data[14:30], _key_id(TOKEN))
        self.assertEqual(data[30:62], _x25519(secret, b"\x09" + bytes(31)))
        self.assertEqual(session.key_id, _key_id(TOKEN))

    def test_the_responder_carries_the_same_key_id_back(self):
        _, _, first, second = _pair()
        self.assertEqual(first[14:30], second[14:30])

    def test_fresh_prefix_and_share_per_session(self):
        one = protocol.LinkSession(TOKEN, protocol.ROLE_INITIATOR).preamble()
        two = protocol.LinkSession(TOKEN, protocol.ROLE_INITIATOR).preamble()
        self.assertNotEqual(one[6:14], two[6:14])
        self.assertNotEqual(one[30:], two[30:])

    def test_the_responder_sends_nothing_before_the_initiators_preamble(self):
        responder = protocol.LinkSession(TOKEN, protocol.ROLE_RESPONDER)
        with self.assertRaises(protocol.ProtocolError):
            responder.preamble()

    def test_short_reply(self):
        reply = protocol.short_reply(prefix=b"Q" * 8)
        self.assertEqual(reply, b"BEAMY\x06" + b"Q" * 8)
        self.assertEqual(len(protocol.short_reply()), 14)

    def test_legacy_reply(self):
        reply = protocol.legacy_version_reply()
        (length,) = struct.unpack(">I", reply[:4])
        self.assertEqual(length, len(reply) - 4)
        self.assertEqual(json.loads(reply[4:]), {"type": "welcome", "data": {"version": 6, "error": "version_mismatch"}})

    def test_split(self):
        _, _, first, _ = _pair()
        prefix, key_id, share = protocol.split_preamble(first)
        self.assertEqual((prefix, key_id, share), (first[6:14], first[14:30], first[30:]))
        for bad in (first[:61], first + b"\x00", b"BEAMX" + first[5:], first[:5] + b"\x05" + first[6:]):
            with self.assertRaises(protocol.ProtocolError):
                protocol.split_preamble(bad)


class ReadingThePreamble(unittest.TestCase):
    """recv_link_preamble reads 6 bytes, then the rest only for version 6, and names what it met."""

    def _read(self, sent, close=True):
        left, right = socket.socketpair()
        try:
            left.sendall(sent)
            if close:
                left.shutdown(socket.SHUT_WR)
            return protocol.recv_link_preamble(right, time.monotonic() + 1)
        finally:
            left.close()
            right.close()

    def test_version_6(self):
        _, _, first, _ = _pair()
        self.assertEqual(self._read(first), first)

    def test_version_5_reads_its_whole_preamble(self):
        with self.assertRaises(protocol.VersionMismatch) as caught:
            self._read(b"BEAMY\x05" + b"x" * 8)
        self.assertEqual(caught.exception.peer_version, 5)

    def test_older_and_newer_versions(self):
        for version in (0, 1, 4, 7, 255):
            with self.subTest(version=version), self.assertRaises(protocol.VersionMismatch) as caught:
                self._read(b"BEAMY" + bytes([version]) + b"x" * 8)
            self.assertEqual(caught.exception.peer_version, version)

    def test_not_a_beamer_preamble(self):
        with self.assertRaises(protocol.VersionMismatch) as caught:
            self._read(protocol.legacy_frame({"type": "hello", "data": {}}))
        self.assertIsNone(caught.exception.peer_version)

    def test_eof_before_a_byte(self):
        with self.assertRaises(protocol.ConnectionClosed):
            self._read(b"")

    def test_the_deadline_bounds_the_whole_preamble(self):
        _, _, first, _ = _pair()
        started = time.monotonic()
        with self.assertRaises(socket.timeout):
            self._read(first[:61], close=False)
        self.assertLess(time.monotonic() - started, 2.0)


class Keys(unittest.TestCase):
    def test_both_keys_match_hkdf_and_x25519_written_here(self):
        secret_i, secret_r = b"\x11" * 32, b"\x22" * 32
        initiator, responder, first, second = _pair(secret_i=secret_i, secret_r=secret_r)
        share_i, share_r = first[30:], second[30:]
        shared = _x25519(secret_i, share_r)
        self.assertEqual(shared, _x25519(secret_r, share_i))
        key_id = _key_id(TOKEN)
        for direction, sealer, opener in ((1, initiator, responder), (2, responder, initiator)):
            key = _key(TOKEN, shared, key_id, first[6:14], second[6:14], share_i, share_r, direction)
            frame = sealer.seal({"type": "ping", "data": {}})
            body = frame[4:]
            nonce = sealer.prefix + body[:4]
            plain = crypto_aead_chacha20poly1305_ietf_decrypt(body[4:], None, nonce, key)
            self.assertEqual(json.loads(plain), {"type": "ping", "data": {}})
            self.assertEqual(_open_frame(opener, frame), {"type": "ping", "data": {}})

    def test_the_sizes(self):
        key_id = _key_id(TOKEN)
        info = protocol.link_info(key_id, b"I" * 8, b"R" * 8, b"a" * 32, b"b" * 32, 1)
        self.assertEqual(len(info), 111)
        self.assertEqual(info, b"beamer-frames\x06" + key_id + b"I" * 8 + b"R" * 8 + b"a" * 32 + b"b" * 32 + b"\x01")
        self.assertEqual(len(protocol.link_ikm(TOKEN, b"s" * 32)), 75)
        self.assertEqual(protocol.link_ikm(TOKEN, b"s" * 32), TOKEN.encode("ascii") + b"s" * 32)

    def test_every_input_is_bound(self):
        key_id = _key_id(TOKEN)
        base = dict(token=TOKEN, shared=b"s" * 32, key_id=key_id, prefix_i=b"I" * 8, prefix_r=b"R" * 8,
                    share_i=b"a" * 32, share_r=b"b" * 32, direction=1)
        reference = protocol.link_key(**base)
        self.assertEqual(reference, _key(**base))
        changes = {
            "token": OTHER_TOKEN,
            "shared": b"t" * 32,
            "key_id": _key_id(OTHER_TOKEN),
            "prefix_i": b"J" * 8,
            "prefix_r": b"S" * 8,
            "share_i": b"c" * 32,
            "share_r": b"d" * 32,
            "direction": 2,
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertNotEqual(protocol.link_key(**dict(base, **{field: value})), reference)
        swapped = dict(base, prefix_i=b"R" * 8, prefix_r=b"I" * 8, share_i=b"b" * 32, share_r=b"a" * 32)
        self.assertNotEqual(protocol.link_key(**swapped), reference)

    def test_reflection_fails_the_tag(self):
        initiator, _, _, _ = _pair()
        frame = initiator.seal({"type": "ping", "data": {}})
        with self.assertRaises(protocol.AuthenticationError):
            _open_frame(initiator, frame)

    def test_a_rewritten_preamble_fails_the_first_tag(self):
        initiator = protocol.LinkSession(TOKEN, protocol.ROLE_INITIATOR)
        responder = protocol.LinkSession(TOKEN, protocol.ROLE_RESPONDER)
        first = bytearray(initiator.preamble())
        first[10] ^= 1  # the initiator's prefix, as the responder sees it
        responder.accept_preamble(bytes(first))
        initiator.accept_preamble(responder.preamble())
        with self.assertRaises(protocol.AuthenticationError):
            _open_frame(responder, initiator.seal({"type": "ping", "data": {}}))

    def test_a_recorded_link_does_not_open_in_a_new_one(self):
        initiator, _, first, _ = _pair()
        recorded = initiator.seal({"type": "ping", "data": {}})
        responder = protocol.LinkSession(TOKEN, protocol.ROLE_RESPONDER)
        responder.accept_preamble(first)
        with self.assertRaises(protocol.AuthenticationError):
            _open_frame(responder, recorded)

    def test_forward_secrecy_the_token_alone_opens_nothing(self):
        initiator, _, first, second = _pair()
        frame = initiator.seal({"type": "ping", "data": {}})
        key_id = _key_id(TOKEN)
        # Everything in the clear plus the token, with the one thing an eavesdropper lacks guessed
        # as the obvious candidates: no shared secret, and each share used as if it were one.
        for shared in (bytes(32), first[30:], second[30:], _x25519(b"\x00" * 32, first[30:])):
            key = _key(TOKEN, shared, key_id, first[6:14], second[6:14], first[30:], second[30:], 1)
            with self.assertRaises(Exception):
                crypto_aead_chacha20poly1305_ietf_decrypt(frame[8:], None, first[6:14] + frame[4:8], key)
        self.assertIsNone(initiator._secret)

    def test_counters(self):
        initiator, responder, _, _ = _pair()
        one = initiator.seal({"type": "ping", "data": {}})
        two = initiator.seal({"type": "ping", "data": {}})
        with self.assertRaises(protocol.ProtocolError):
            _open_frame(responder, two)
        _open_frame(responder, one)
        with self.assertRaises(protocol.ProtocolError):
            _open_frame(responder, one)
        _open_frame(responder, two)

    def test_nothing_is_sealed_before_the_keys(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.LinkSession(TOKEN, protocol.ROLE_INITIATOR).seal({"type": "ping", "data": {}})


class PreambleRefusals(unittest.TestCase):
    def test_low_order_share_from_the_initiator(self):
        for point in LOW_ORDER:
            with self.subTest(point=point.hex()):
                self.assertEqual(_x25519(b"\x22" * 32, point), bytes(32))
                initiator = protocol.LinkSession(TOKEN, protocol.ROLE_INITIATOR)
                forged = initiator.preamble()[:30] + point
                responder = protocol.LinkSession(TOKEN, protocol.ROLE_RESPONDER)
                with self.assertRaises(protocol.PreambleRefused):
                    responder.accept_preamble(forged)
                with self.assertRaises(protocol.ProtocolError):
                    responder.preamble()

    def test_low_order_share_from_the_responder(self):
        for point in LOW_ORDER:
            with self.subTest(point=point.hex()):
                initiator = protocol.LinkSession(TOKEN, protocol.ROLE_INITIATOR)
                first = initiator.preamble()
                responder = protocol.LinkSession(TOKEN, protocol.ROLE_RESPONDER)
                responder.accept_preamble(first)
                forged = responder.preamble()[:30] + point
                with self.assertRaises(protocol.PreambleRefused):
                    initiator.accept_preamble(forged)
                with self.assertRaises(protocol.ProtocolError):
                    initiator.seal({"type": "ping", "data": {}})

    def test_equal_prefixes(self):
        initiator = protocol.LinkSession(TOKEN, protocol.ROLE_INITIATOR, prefix=b"S" * 8)
        first = initiator.preamble()
        responder = protocol.LinkSession(TOKEN, protocol.ROLE_RESPONDER, prefix=b"S" * 8)
        responder.accept_preamble(first)
        self.assertNotEqual(responder.prefix, b"S" * 8)
        second = responder.preamble()
        self.assertEqual(second[6:14], responder.prefix)
        initiator.accept_preamble(second)
        # An initiator given its own prefix back closes.
        again = protocol.LinkSession(TOKEN, protocol.ROLE_INITIATOR, prefix=b"S" * 8)
        again.preamble()
        echo = second[:6] + b"S" * 8 + second[14:]
        with self.assertRaises(protocol.PreambleRefused):
            again.accept_preamble(echo)

    def test_a_reflected_preamble(self):
        initiator = protocol.LinkSession(TOKEN, protocol.ROLE_INITIATOR)
        first = initiator.preamble()
        with self.assertRaises(protocol.PreambleRefused):
            initiator.accept_preamble(first)

    def test_the_initiator_requires_its_own_key_id_back(self):
        initiator = protocol.LinkSession(TOKEN, protocol.ROLE_INITIATOR)
        first = initiator.preamble()
        responder = protocol.LinkSession(TOKEN, protocol.ROLE_RESPONDER)
        responder.accept_preamble(first)
        second = responder.preamble()
        forged = second[:14] + _key_id(OTHER_TOKEN) + second[30:]
        with self.assertRaises(protocol.PreambleRefused):
            initiator.accept_preamble(forged)

    def test_the_responder_requires_its_tokens_key_id(self):
        initiator = protocol.LinkSession(OTHER_TOKEN, protocol.ROLE_INITIATOR)
        responder = protocol.LinkSession(TOKEN, protocol.ROLE_RESPONDER)
        with self.assertRaises(protocol.PreambleRefused):
            responder.accept_preamble(initiator.preamble())

    def test_a_second_preamble(self):
        initiator, responder, first, second = _pair()
        with self.assertRaises(protocol.ProtocolError):
            responder.accept_preamble(first)
        with self.assertRaises(protocol.ProtocolError):
            initiator.accept_preamble(second)


class StrictJson(unittest.TestCase):
    def _open(self, plain):
        initiator, responder, _, _ = _pair()
        frame = initiator.seal_raw(plain)
        return _open_frame(responder, frame)

    def test_a_clean_message(self):
        message = self._open(b'{"type":"ping","data":{}}')
        self.assertEqual(message, {"type": "ping", "data": {}})
        self.assertFalse(message.malformed)

    def test_malformed_but_an_object(self):
        for plain in (
            b'{"type":"ping","data":{"x":NaN}}',
            b'{"type":"ping","data":{"x":Infinity}}',
            b'{"type":"ping","data":{"x":-Infinity}}',
            b'{"type":"ping","data":{"x":1e400}}',
            b'{"type":"ping","data":{},"data":{}}',
            b'{"type":"ping","data":{"x":1,"x":2}}',
            b'{"type":"ping","data":{"x":"\\ud800"}}',
            b'{"type":"ping","data":{"\\udc00":1}}',
            b'\xef\xbb\xbf{"type":"ping","data":{}}',
        ):
            with self.subTest(plain=plain):
                message = self._open(plain)
                self.assertTrue(message.malformed)
                self.assertIsNone(protocol.read_message(message))

    def test_not_an_object_ends_the_link(self):
        for plain in (b"[]", b"1", b'"x"', b"{", b"\xff", b"", b"[" * 100000):
            with self.subTest(plain=plain[:10]), self.assertRaises(protocol.ProtocolError):
                self._open(plain)

    def test_envelope(self):
        for message in ({"type": 1, "data": {}}, {"data": {}}, {"type": "ping"}, {"type": "ping", "data": []}):
            with self.subTest(message=message):
                self.assertIsNone(protocol.read_message(message))
        self.assertEqual(protocol.read_message({"type": "ping", "data": {}, "extra": 1}), ("ping", {}))

    def test_integers_by_their_text(self):
        for plain, ok in ((b'{"type":"accept","data":{"route":1}}', True),
                          (b'{"type":"accept","data":{"route":1.0}}', False),
                          (b'{"type":"accept","data":{"route":1e0}}', False),
                          (b'{"type":"accept","data":{"route":true}}', False)):
            with self.subTest(plain=plain):
                self.assertEqual(protocol.read_accept(self._open(plain)) is not None, ok)

    def test_seal_writes_strict_json(self):
        initiator, responder, _, _ = _pair()
        with self.assertRaises(ValueError):
            initiator.seal({"type": "ping", "data": {"x": float("nan")}})
        message = _open_frame(responder, initiator.seal({"type": "text", "data": {"text": "é\U0001F600", "seq": 1}}))
        self.assertEqual(message["data"]["text"], "é\U0001F600")


def _hello(**changes):
    data = {"version": 6, "id": protocol.id_text(ID_A), "name": "Studio Mac", "platform": "macos",
            "app": "1.5.0", "caps": ["clipboard", "settings"], "port": 24820}
    data.update(changes)
    return {"type": "hello", "data": {k: v for k, v in data.items() if v is not _DROP}}


_DROP = object()


class HelloAndWelcome(unittest.TestCase):
    def test_builders_read_back(self):
        hello = protocol.hello_v6(ID_A, "Studio Mac", "macos", "1.5.0", ["clipboard"], 24820, hw="aa:bb:cc:dd:ee:ff")
        read = protocol.read_hello(hello)
        self.assertEqual(read["id"], ID_A)
        self.assertEqual(read["name"], "Studio Mac")
        self.assertEqual(read["port"], 24820)
        self.assertEqual(read["hw"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(hello["data"]["version"], 6)
        welcome = protocol.welcome_v6(ID_B, "PC", "windows", "1.5.0", ["text"], accepts=True)
        read = protocol.read_welcome(welcome)
        self.assertEqual(read["id"], ID_B)
        self.assertIs(read["accepts"], True)
        self.assertIsNone(read["hw"])
        self.assertNotIn("port", welcome["data"])

    def test_invalid_hellos(self):
        cases = {
            "version 5": _hello(version=5),
            "version 6.0": _hello(version=6.0),
            "no id": _hello(id=_DROP),
            "15-byte id": _hello(id=_b64(bytes(range(15)))),
            "zero id": _hello(id=_b64(bytes(16))),
            "empty name": _hello(name=""),
            "49 code points": _hello(name="é" * 49),
            "port 70000": _hello(port=70000),
            "port true": _hello(port=True),
            "17 caps": _hello(caps=["c%d" % n for n in range(17)]),
            "a cap of 33": _hello(caps=["c" * 33]),
            "a non-ASCII cap": _hello(caps=["cé"]),
            "capital platform": _hello(platform="MacOS"),
            "long platform": _hello(platform="a" * 17),
            "empty app": _hello(app=""),
            "app of 33": _hello(app="1" * 33),
            "upper-case hw": _hello(hw="AA:BB:CC:DD:EE:FF"),
            "not a hello": {"type": "ping", "data": {}},
        }
        for label, message in cases.items():
            with self.subTest(label), self.assertRaises(protocol.InvalidHello):
                protocol.read_hello(message)
        self.assertEqual(protocol.read_hello(_hello(name="é" * 48))["name"], "é" * 48)
        self.assertEqual(protocol.read_hello(_hello(platform="haiku_os"))["platform"], "haiku_os")
        self.assertEqual(protocol.read_hello(_hello(port=0))["port"], 0)
        self.assertEqual(protocol.read_hello(_hello(extra="ignored"))["name"], "Studio Mac")

    def test_the_error_answers(self):
        for answer, error in ((protocol.invalid_hello_v6(), "invalid_hello"), (protocol.wrong_id_v6(), "wrong_id")):
            self.assertEqual(answer, {"type": "welcome", "data": {"version": 6, "error": error}})
            with self.assertRaises(protocol.HelloRefused) as caught:
                protocol.read_welcome(answer)
            self.assertEqual(caught.exception.error, error)
        for data in ({"version": 6, "error": "other"}, {"version": 6, "error": 1}, {"version": 5, "error": "wrong_id"}):
            with self.subTest(data=data), self.assertRaises(protocol.InvalidWelcome):
                protocol.read_welcome({"type": "welcome", "data": data})

    def test_invalid_welcomes(self):
        good = protocol.welcome_v6(ID_B, "PC", "windows", "1.5.0", [], accepts=False)
        self.assertIs(protocol.read_welcome(good)["accepts"], False)
        for change in ({"accepts": 1}, {"accepts": None}, {"id": "x"}, {"version": 5}):
            bad = {"type": "welcome", "data": dict(good["data"], **change)}
            with self.subTest(change=change), self.assertRaises(protocol.InvalidWelcome):
                protocol.read_welcome(bad)
        with self.assertRaises(protocol.InvalidWelcome):
            protocol.read_welcome({"type": "hello", "data": good["data"]})


class Focus(unittest.TestCase):
    def test_take_by_a_zone(self):
        message = protocol.focus_v6(7, ID_B, edge="left", offset=0.25, resistance_px=90, reach=[ID_C])
        read = protocol.read_focus(message, ID_B)
        self.assertEqual(read["route"], 7)
        self.assertEqual(read["target"], ID_B)
        self.assertTrue(read["here"])
        self.assertFalse(read["malformed"])
        self.assertEqual((read["edge"], read["offset"], read["resistance_px"], read["reach"], read["stay"]),
                         ("left", 0.25, 90, [ID_C], False))

    def test_defaults_and_clamping(self):
        read = protocol.read_focus({"type": "focus", "data": {"route": 1, "target": protocol.id_text(ID_B),
                                                              "edge": "top", "offset": 1.5}}, ID_B)
        self.assertEqual((read["offset"], read["resistance_px"], read["reach"]), (1.0, 120, []))
        read = protocol.read_focus({"type": "focus", "data": {"route": 1, "target": protocol.id_text(ID_B),
                                                              "edge": "top", "offset": -2}}, ID_B)
        self.assertEqual(read["offset"], 0.0)

    def test_stay(self):
        read = protocol.read_focus(protocol.focus_v6(3, ID_B, resistance_px=120, reach=[], stay=True), ID_B)
        self.assertTrue(read["stay"])
        self.assertFalse(read["malformed"])

    def test_let_go_ignores_the_fields_it_does_not_have(self):
        message = protocol.focus_v6(9, ID_C)
        self.assertEqual(message["data"], {"route": 9, "target": protocol.id_text(ID_C)})
        extra = {"type": "focus", "data": dict(message["data"], reach="junk", resistance_px=-4, stay=5, edge="up")}
        read = protocol.read_focus(extra, ID_B)
        self.assertFalse(read["here"])
        self.assertFalse(read["malformed"])
        self.assertEqual(read["target"], ID_C)

    def test_route_or_target_malformed_is_ignored(self):
        for data in ({"route": 0, "target": protocol.id_text(ID_B)},
                     {"route": 2**53, "target": protocol.id_text(ID_B)},
                     {"route": 1.0, "target": protocol.id_text(ID_B)},
                     {"route": True, "target": protocol.id_text(ID_B)},
                     {"target": protocol.id_text(ID_B)},
                     {"route": 1, "target": "nope"},
                     {"route": 1}):
            with self.subTest(data=data):
                self.assertIsNone(protocol.read_focus({"type": "focus", "data": data}, ID_B))

    def test_other_fields_malformed_are_flagged_for_refuse(self):
        target = protocol.id_text(ID_B)
        for extra in ({"edge": "left"}, {"offset": 0.5}, {"edge": "middle", "offset": 0.5},
                      {"edge": "left", "offset": True}, {"resistance_px": 1001}, {"resistance_px": -1},
                      {"resistance_px": 12.5}, {"reach": [protocol.id_text(ID_C)] * 2},
                      {"reach": [protocol.id_text(bytes([n]) * 16) for n in range(1, 34)]},
                      {"reach": ["bad"]}, {"reach": "x"}, {"stay": False}, {"stay": 1}):
            with self.subTest(extra=extra):
                read = protocol.read_focus({"type": "focus", "data": dict({"route": 4, "target": target}, **extra)}, ID_B)
                self.assertEqual(read["route"], 4)
                self.assertTrue(read["malformed"])

    def test_strictness_marks_a_take_malformed(self):
        initiator, responder, _, _ = _pair()
        plain = ('{"type":"focus","data":{"route":4,"target":"%s","offset":NaN,"edge":"left"}}' % protocol.id_text(ID_B)).encode()
        read = protocol.read_focus(_open_frame(responder, initiator.seal_raw(plain)), ID_B)
        self.assertEqual(read["route"], 4)
        self.assertTrue(read["malformed"])

    def test_a_reach_naming_the_owner_or_responder_is_kept_for_the_caller(self):
        read = protocol.read_focus(protocol.focus_v6(1, ID_B, reach=[ID_A, ID_B, ID_C]), ID_B)
        self.assertEqual(read["reach"], [ID_A, ID_B, ID_C])


class Answers(unittest.TestCase):
    def test_accept_refuse_accepts(self):
        self.assertEqual(protocol.read_accept(protocol.accept_msg(5)), {"route": 5})
        self.assertEqual(protocol.read_refuse(protocol.refuse_msg(5, "owned")), {"route": 5, "why": "owned"})
        self.assertEqual(protocol.read_refuse({"type": "refuse", "data": {"route": 5, "why": "new_reason"}})["why"], "new_reason")
        self.assertIsNone(protocol.read_accept({"type": "accept", "data": {"route": 0}}))
        self.assertEqual(protocol.read_accepts(protocol.accepts_msg(False)), {"accepts": False})
        self.assertIsNone(protocol.read_accepts({"type": "accepts", "data": {"accepts": 0}}))
        with self.assertRaises(ValueError):
            protocol.refuse_msg(5, "because")

    def test_a_refuse_names_the_machine_in_the_way_only_when_owned_or_busy(self):
        for why in ("owned", "busy"):
            with self.subTest(why=why):
                message = protocol.refuse_msg(5, why, other=ID_B)
                self.assertEqual(message["data"], {"route": 5, "why": why, "other": protocol.id_text(ID_B)})
                self.assertEqual(protocol.read_refuse(message), {"route": 5, "why": why, "other": ID_B})
        for why in ("not_allowed", "sent_home", "malformed"):
            with self.subTest(why=why):
                self.assertNotIn("other", protocol.refuse_msg(5, why, other=ID_B)["data"])
        self.assertNotIn("other", protocol.refuse_msg(5, "owned")["data"])
        # One that is not an id is left out, and the refusal still stands.
        for odd in ("x", 7, None, protocol.id_text(bytes(16))):
            data = {"route": 5, "why": "owned", "other": odd}
            with self.subTest(other=odd):
                self.assertEqual(protocol.read_refuse({"type": "refuse", "data": data}), {"route": 5, "why": "owned"})

    def test_switch(self):
        read = protocol.read_switch(protocol.switch_v6(3, ID_A, edge="right", offset=0.5))
        self.assertEqual(read, {"route": 3, "next": ID_A, "edge": "right", "offset": 0.5})
        read = protocol.read_switch(protocol.switch_v6(3, ID_A))
        self.assertEqual(read, {"route": 3, "next": ID_A, "edge": None, "offset": None})
        for data in ({"route": 3, "next": protocol.id_text(ID_A), "edge": "right"},
                     {"route": 3, "next": protocol.id_text(ID_A), "offset": 0.5},
                     {"route": 3, "next": "x"}, {"route": 0, "next": protocol.id_text(ID_A)}):
            with self.subTest(data=data):
                self.assertIsNone(protocol.read_switch({"type": "switch", "data": data}))

    def test_ack(self):
        self.assertEqual(protocol.read_ack(protocol.ack_v6(4, 250)), {"seq": 4, "held_us": 250, "locked": False})
        self.assertEqual(protocol.ack_v6(4, -3)["data"]["held_us"], 0)
        self.assertEqual(protocol.ack_v6(4, 2**40)["data"]["held_us"], 2**31 - 1)
        self.assertEqual(protocol.read_ack(protocol.ack_v6(0, 0, locked=True))["locked"], True)
        for data in ({"seq": 1}, {"seq": 1, "held_us": -1}, {"seq": 1, "held_us": 2**31}, {"seq": -1, "held_us": 0},
                     {"seq": 1.0, "held_us": 0}, {"seq": 1, "held_us": 0, "locked": False}, {"held_us": 0}):
            with self.subTest(data=data), self.assertRaises(protocol.ProtocolError):
                protocol.read_ack({"type": "ack", "data": data})


class Input(unittest.TestCase):
    def test_each_kind(self):
        good = [
            ({"type": "keydown", "data": {"key": "a", "us": "a", "seq": 1}}, {"key": "a", "us": "a"}),
            ({"type": "keyup", "data": {"key": "shift_r", "seq": 2}}, {"key": "shift_r", "us": None}),
            ({"type": "mousemove", "data": {"dx": -65535, "dy": 65535, "seq": 3}}, {"dx": -65535, "dy": 65535}),
            ({"type": "mousedown", "data": {"button": "forward", "seq": 4}}, {"button": "forward"}),
            ({"type": "scroll", "data": {"dy": -1.5, "dx": 2, "mode": "pixel", "seq": 5}}, {"dy": -1.5, "dx": 2, "mode": "pixel"}),
            ({"type": "scroll", "data": {"dy": 3, "seq": 6}}, {"dy": 3, "dx": 0, "mode": "line"}),
            ({"type": "gesture", "data": {"name": "pinch", "seq": 7}}, {"name": "pinch"}),
            ({"type": "text", "data": {"text": "a\n\U0001F600", "seq": 8}}, {"text": "a\n\U0001F600"}),
        ]
        for message, expected in good:
            with self.subTest(message=message):
                read = protocol.read_input(message)
                self.assertEqual(read["seq"], message["data"]["seq"])
                self.assertEqual(read["type"], message["type"])
                for key, value in expected.items():
                    self.assertEqual(read[key], value)

    def test_builders(self):
        self.assertEqual(protocol.read_input(protocol.key_msg("keydown", "c", us="c", seq=1))["key"], "c")
        self.assertEqual(protocol.read_input(protocol.text_msg("hé", seq=2))["text"], "hé")
        self.assertEqual(protocol.read_input(protocol.mousemove_msg(3, -4, seq=3))["dx"], 3)

    def test_malformed(self):
        for message in (
            {"type": "keydown", "data": {"key": "", "seq": 1}},
            {"type": "keydown", "data": {"key": "a", "us": "ab", "seq": 1}},
            {"type": "keydown", "data": {"key": 5, "seq": 1}},
            {"type": "keydown", "data": {"key": "a"}},
            {"type": "keydown", "data": {"key": "a", "seq": 0}},
            {"type": "mousemove", "data": {"dx": 1.5, "dy": 0, "seq": 1}},
            {"type": "mousemove", "data": {"dx": 65536, "dy": 0, "seq": 1}},
            {"type": "mousemove", "data": {"dx": 0, "seq": 1}},
            {"type": "mousedown", "data": {"button": "fourth", "seq": 1}},
            {"type": "scroll", "data": {"dy": 70000, "seq": 1}},
            {"type": "scroll", "data": {"dy": 1, "mode": "page", "seq": 1}},
            {"type": "scroll", "data": {"dy": True, "seq": 1}},
            {"type": "gesture", "data": {"name": "rotate", "seq": 1}},
            {"type": "text", "data": {"text": 5, "seq": 1}},
            {"type": "ping", "data": {}},
        ):
            with self.subTest(message=message):
                self.assertIsNone(protocol.read_input(message))

    def test_text_over_4096_bytes_is_dropped_but_keeps_its_seq(self):
        read = protocol.read_input({"type": "text", "data": {"text": "é" * 2049, "seq": 9}})
        self.assertEqual(read["seq"], 9)
        self.assertIsNone(read["text"])
        self.assertEqual(protocol.read_input({"type": "text", "data": {"text": "é" * 2048, "seq": 9}})["text"], "é" * 2048)


class ClipboardImages(unittest.TestCase):
    """An image's size in pixels and its inflated metadata are bounded from its bytes before anything
    decodes it (WIRE.md section 3): a PNG of a few megabytes can otherwise decode to gigabytes."""

    def test_screens_up_to_8k_fit(self):
        for width, height in ((1, 1), (6016, 3384), (7680, 4320), (16384, 2048), (2048, 16384)):
            with self.subTest(size=(width, height)):
                self.assertTrue(protocol.png_fits(pngs.png(width, height)))

    def test_a_side_over_16384_or_more_than_2_to_the_25_pixels_does_not(self):
        for width, height in ((16385, 1), (1, 16385), (0, 1), (1, 0), (6000, 6000), (16384, 16384), (2**31 - 1, 1)):
            with self.subTest(size=(width, height)):
                self.assertFalse(protocol.png_fits(pngs.png(width, height)))

    def test_the_header_must_come_first_and_be_whole(self):
        self.assertFalse(protocol.png_fits(protocol.PNG_SIGNATURE + pngs.chunk(b"tEXt", b"a\x00b") + pngs.ihdr(1, 1)))
        self.assertFalse(protocol.png_fits(pngs.png(header=pngs.chunk(b"IHDR", bytes(12)))))
        self.assertFalse(protocol.png_fits(protocol.PNG_SIGNATURE))
        self.assertFalse(protocol.png_fits(protocol.PNG_SIGNATURE + b"\x00" * 64))
        self.assertFalse(protocol.png_fits(b"GIF89a" + pngs.png()[8:]))

    def test_a_chunk_that_runs_past_the_end_does_not(self):
        whole = pngs.png()
        self.assertFalse(protocol.png_fits(whole[:-5]))
        self.assertFalse(protocol.png_fits(whole[:33] + struct.pack(">I", 10**6) + whole[37:]))

    def test_metadata_inflates_to_at_most_4_mib_in_all(self):
        limit = protocol.PNG_METADATA_MAX_BYTES
        self.assertEqual(limit, 4 * 1024 * 1024)
        self.assertTrue(protocol.png_fits(pngs.png(64, 64, pngs.iccp(600_000), pngs.ztxt(limit - 600_000))))
        for extra in ((pngs.ztxt(limit + 1),), (pngs.iccp(limit + 1),), (pngs.itxt(limit + 1, compressed=True),),
                      (pngs.iccp(limit // 2), pngs.ztxt(limit // 2), pngs.ztxt(1))):
            with self.subTest(chunks=[c[4:8] for c in extra]):
                self.assertFalse(protocol.png_fits(pngs.png(64, 64, *extra)))
        # Uncompressed text is bounded by the file itself.
        self.assertTrue(protocol.png_fits(pngs.png(64, 64, pngs.itxt(limit + 1, compressed=False))))

    def test_metadata_that_does_not_inflate_does_not_fit(self):
        stream = zlib.compress(b"a" * 1000)
        for data in (b"not zlib at all", stream[:-6], stream + b"junk"):
            with self.subTest(data=data[:8]):
                self.assertFalse(protocol.png_fits(pngs.png(8, 8, pngs.chunk(b"zTXt", b"Comment\x00\x00" + data))))
        self.assertFalse(protocol.png_fits(pngs.png(8, 8, pngs.chunk(b"iTXt", b"Key\x00\x02\x00en\x00\x00text"))))

    def test_compressed_text_needs_a_whole_zlib_stream_even_when_empty(self):
        for chunk in (
            pngs.chunk(b"zTXt", b"Comment\x00\x00"),
            pngs.chunk(b"iTXt", b"Key\x00\x01\x00en\x00\x00"),
            pngs.chunk(b"zTXt", b"Comment\x00\x00" + pngs.run(b"a", 1)[:-1]),
        ):
            with self.subTest(kind=chunk[4:8], data_length=len(chunk) - 12):
                self.assertFalse(protocol.png_fits(pngs.png(8, 8, chunk)))
        self.assertTrue(protocol.png_fits(pngs.png(8, 8, pngs.chunk(b"iTXt", b"Key\x00\x00\x00en\x00\x00"))))

    def test_a_clipboard_image_is_a_still_with_one_header(self):
        # A second IHDR or an animation's frames would claim sizes the first header never checked.
        for extra in (pngs.ihdr(100000, 100000), pngs.chunk(b"acTL", bytes(8)),
                      pngs.chunk(b"fcTL", struct.pack(">IIIII", 0, 60000, 60000, 0, 0) + bytes(6)),
                      pngs.chunk(b"fdAT", bytes(8))):
            with self.subTest(chunk=extra[4:8]):
                self.assertFalse(protocol.png_fits(pngs.png(8, 8, extra)))

    def test_bytes_after_iend_are_ignored(self):
        self.assertTrue(protocol.png_fits(pngs.png() + b"trailing"))

    def test_only_the_image_part_of_a_clipboard_is_dropped(self):
        for image in (pngs.png(16385, 1), pngs.png(8, 8, pngs.ztxt(5 * 1024 * 1024))):
            with self.subTest(size=len(image)):
                self.assertEqual(protocol.read_clipboard(protocol.clipboard_msg(text="hi", image=image)), {"text": "hi", "image": None})


class Others(unittest.TestCase):
    def test_clipboard(self):
        png = pngs.png()
        read = protocol.read_clipboard(protocol.clipboard_msg(text="hi", image=png))
        self.assertEqual(read, {"text": "hi", "image": png})
        read = protocol.read_clipboard({"type": "clipboard", "data": {"text": "x" * (256 * 1024 + 1)}})
        self.assertEqual(read, {"text": None, "image": None})
        self.assertIsNone(protocol.read_clipboard({"type": "clipboard", "data": {"text": "hi", "image": 5}}))
        self.assertIsNone(protocol.read_clipboard({"type": "clipboard", "data": {"text": 5}}))
        self.assertEqual(protocol.read_clipboard({"type": "clipboard", "data": {"text": "é" * (128 * 1024)}})["text"], "é" * (128 * 1024))

    def test_arrangement(self):
        now = 1_790_000_000
        read = protocol.read_arrangement_v6(protocol.arrangement_v6("left", now, ID_A), now)
        self.assertEqual(read, {"edge": "left", "set_at": now, "by": ID_A})
        for data in ({"edge": "middle", "set_at": now, "by": protocol.id_text(ID_A)},
                     {"edge": "left", "set_at": now + 86401, "by": protocol.id_text(ID_A)},
                     {"edge": "left", "set_at": 2**53 - 1, "by": protocol.id_text(ID_A)},
                     {"edge": "left", "set_at": -1, "by": protocol.id_text(ID_A)},
                     {"edge": "left", "set_at": now, "by": ""},
                     {"edge": "left", "set_at": float(now), "by": protocol.id_text(ID_A)}):
            with self.subTest(data=data):
                self.assertIsNone(protocol.read_arrangement_v6({"type": "arrangement", "data": data}, now))
        self.assertIsNotNone(protocol.read_arrangement_v6(
            {"type": "arrangement", "data": {"edge": "top", "set_at": now + 86400, "by": protocol.id_text(ID_A)}}, now))

    def test_arrangement_way_back(self):
        now = 1_790_000_000
        for way_back in (True, False):
            message = protocol.arrangement_v6("left", now, ID_A, way_back=way_back)
            self.assertIs(message["data"]["way_back"], way_back)
            self.assertEqual(protocol.read_arrangement_v6(message, now),
                             {"edge": "left", "set_at": now, "by": ID_A, "way_back": way_back})
        self.assertNotIn("way_back", protocol.arrangement_v6("left", now, ID_A)["data"])
        # Anything but a boolean is left out, and the side it came with still stands.
        for odd in (1, "false", None, [True]):
            data = {"edge": "left", "set_at": now, "by": protocol.id_text(ID_A), "way_back": odd}
            with self.subTest(way_back=odd):
                self.assertEqual(protocol.read_arrangement_v6({"type": "arrangement", "data": data}, now),
                                 {"edge": "left", "set_at": now, "by": ID_A})

    def test_arrangement_way_back_by_names_the_machine_holding_the_side_only_with_way_back_false(self):
        now = 1_790_000_000
        message = protocol.arrangement_v6("left", now, ID_A, way_back=False, way_back_by=ID_C)
        self.assertEqual(message["data"]["way_back_by"], protocol.id_text(ID_C))
        self.assertEqual(protocol.read_arrangement_v6(message, now),
                         {"edge": "left", "set_at": now, "by": ID_A, "way_back": False, "way_back_by": ID_C})
        for way_back in (True, None):
            with self.subTest(way_back=way_back):
                self.assertNotIn("way_back_by", protocol.arrangement_v6("left", now, ID_A, way_back=way_back,
                                                                        way_back_by=ID_C)["data"])
        for data in ({"way_back": True, "way_back_by": protocol.id_text(ID_C)},
                     {"way_back_by": protocol.id_text(ID_C)},
                     {"way_back": False, "way_back_by": "x"}):
            data = {"edge": "left", "set_at": now, "by": protocol.id_text(ID_A), **data}
            with self.subTest(data=data):
                self.assertNotIn("way_back_by", protocol.read_arrangement_v6({"type": "arrangement", "data": data}, now))

    def test_paired(self):
        self.assertEqual(protocol.read_paired(protocol.paired_msg([ID_A, ID_C])), [ID_A, ID_C])
        self.assertEqual(protocol.read_paired(protocol.paired_msg([])), [])
        for ids in (["x"], [protocol.id_text(bytes([n]) * 16) for n in range(1, 34)], "x"):
            with self.subTest(ids=ids):
                self.assertIsNone(protocol.read_paired({"type": "paired", "data": {"ids": ids}}))

    def test_platforms_and_names(self):
        for platform in ("macos", "windows", "linux", "ios", "android", "haiku", "a_1"):
            self.assertTrue(protocol.valid_platform(platform))
        for platform in ("", "MacOS", "a" * 17, "mac-os", "é", 5):
            self.assertFalse(protocol.valid_platform(platform))


class OverASocket(unittest.TestCase):
    """The helpers the links use, end to end over a socket pair: preambles each way, then the
    first frames, as the handshake steps order them."""

    def test_a_whole_handshake(self):
        left, right = socket.socketpair()
        results = {}

        def responder():
            deadline = time.monotonic() + 5
            first = protocol.recv_link_preamble(right, deadline)
            _, key_id, _ = protocol.split_preamble(first)
            results["key_id"] = key_id
            session = protocol.LinkSession(TOKEN, protocol.ROLE_RESPONDER)
            session.accept_preamble(first)
            right.sendall(session.preamble())
            results["hello"] = protocol.read_hello(protocol.recv_msg(right, session, protocol.HELLO_MAX_BYTES, deadline))
            protocol.send_msg(right, session, protocol.welcome_v6(ID_B, "PC", "windows", "1.5.0", [], accepts=True))

        thread = threading.Thread(target=responder)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            session = protocol.LinkSession(TOKEN, protocol.ROLE_INITIATOR)
            left.sendall(session.preamble())
            session.accept_preamble(protocol.recv_link_preamble(left, deadline))
            protocol.send_msg(left, session, protocol.hello_v6(ID_A, "Mac", "macos", "1.5.0", [], 24820))
            welcome = protocol.read_welcome(protocol.recv_msg(left, session, protocol.HELLO_MAX_BYTES, deadline))
            thread.join(5)
            self.assertEqual(welcome["id"], ID_B)
            self.assertEqual(results["hello"]["id"], ID_A)
            self.assertEqual(results["key_id"], _key_id(TOKEN))
        finally:
            left.close()
            right.close()


class HostileInput(unittest.TestCase):
    """What the Part B review found: nothing a peer sends may raise anything but ProtocolError."""

    def test_unhashable_values_where_a_name_is_due(self):
        for value in (["x"], {"x": 1}):
            with self.subTest(value=value):
                self.assertIsNone(protocol.read_input({"type": "gesture", "data": {"seq": 1, "name": value}}))
                self.assertIsNone(protocol.read_input({"type": "mousedown", "data": {"seq": 1, "button": value}}))
                self.assertIsNone(protocol.read_input({"type": "scroll", "data": {"seq": 1, "dy": 1, "mode": value}}))
                self.assertIsNone(protocol.read_switch({"type": "switch", "data": {"route": 1, "next": protocol.id_text(ID_A), "edge": value, "offset": 0}}))
                self.assertIsNone(protocol.read_arrangement_v6({"type": "arrangement", "data": {"edge": value, "set_at": 1, "by": protocol.id_text(ID_A)}}, 10))
                read = protocol.read_focus({"type": "focus", "data": {"route": 1, "target": protocol.id_text(ID_B), "edge": value, "offset": 0}}, ID_B)
                self.assertTrue(read["malformed"])
                with self.assertRaises(protocol.InvalidWelcome):
                    protocol.read_welcome({"type": "welcome", "data": {"version": 6, "error": value}})

    def test_plain_dicts_with_what_a_parser_would_flag(self):
        self.assertIsNone(protocol.read_input({"type": "text", "data": {"seq": 1, "text": "\ud800"}}))
        self.assertIsNone(protocol.read_clipboard({"type": "clipboard", "data": {"text": "\ud800"}}))
        self.assertIsNone(protocol.read_input({"type": "scroll", "data": {"seq": 1, "dy": 10**400}}))

    def test_a_refused_session_stays_refused(self):
        initiator = protocol.LinkSession(TOKEN, protocol.ROLE_INITIATOR)
        responder = protocol.LinkSession(TOKEN, protocol.ROLE_RESPONDER)
        first = initiator.preamble()
        with self.assertRaises(protocol.PreambleRefused):
            responder.accept_preamble(first[:30] + bytes(32))
        with self.assertRaises(protocol.ProtocolError):
            responder.accept_preamble(first)

    def test_repeated_byte_order_marks_are_malformed_not_fatal(self):
        message = protocol.parse_message(b"\xef\xbb\xbf\xef\xbb\xbf" + b'{"type":"ping","data":{}}')
        self.assertTrue(message.malformed)

    def test_a_refuse_without_a_why_is_still_a_refusal(self):
        self.assertEqual(protocol.read_refuse({"type": "refuse", "data": {"route": 5}}), {"route": 5, "why": None})
        self.assertEqual(protocol.read_refuse({"type": "refuse", "data": {"route": 5, "why": 3}}), {"route": 5, "why": None})
        self.assertIsNone(protocol.read_refuse({"type": "refuse", "data": {"route": 0, "why": "owned"}}))


class Builders(unittest.TestCase):
    def test_names_are_cut_to_48_code_points(self):
        hello = protocol.hello_v6(ID_A, "é" * 60, "macos", "1.5.0", [], 24820)
        self.assertEqual(protocol.read_hello(hello)["name"], "é" * 48)
        welcome = protocol.welcome_v6(ID_B, "x" * 63, "windows", "1.5.0", [], accepts=True)
        self.assertEqual(protocol.read_welcome(welcome)["name"], "x" * 48)
        with self.assertRaises(ValueError):
            protocol.hello_v6(ID_A, "", "macos", "1.5.0", [], 24820)

    def test_edge_and_offset_go_together(self):
        for build in (lambda **kw: protocol.focus_v6(1, ID_B, **kw), lambda **kw: protocol.switch_v6(1, ID_A, **kw)):
            with self.assertRaises(ValueError):
                build(edge="left")
            with self.assertRaises(ValueError):
                build(offset=0.5)


class FirstFrames(unittest.TestCase):
    """recv_first_msg, and telling apart the one failure the responder answers (a frame that opened
    but is not a JSON object: invalid_hello) from those it closes on without a word."""

    def _first(self, frame):
        initiator, responder, _, _ = _pair()
        left, right = socket.socketpair()
        try:
            left.sendall(frame(initiator))
            left.shutdown(socket.SHUT_WR)
            return protocol.recv_first_msg(right, responder, time.monotonic() + 1)
        finally:
            left.close()
            right.close()

    def test_a_good_first_frame(self):
        self.assertEqual(self._first(lambda s: s.seal({"type": "ping", "data": {}})), {"type": "ping", "data": {}})

    def test_not_an_object_is_its_own_error(self):
        for plain in (b"[]", b"{", b"\xff", b"1"):
            with self.subTest(plain=plain), self.assertRaises(protocol.NotAnObject):
                self._first(lambda s: s.seal_raw(plain))

    def test_the_length_bounds_are_refused_before_reading(self):
        for length in (0, 20, 4097, 2**31):
            with self.subTest(length=length), self.assertRaises(protocol.ProtocolError) as caught:
                self._first(lambda s: struct.pack(">I", length))
            self.assertNotIsInstance(caught.exception, protocol.NotAnObject)
        self.assertEqual(self._first(lambda s: s.seal_raw(b"{}")), {})

    def test_a_bad_tag_or_counter_is_not_answered(self):
        def flipped(session):
            frame = bytearray(session.seal({"type": "ping", "data": {}}))
            frame[-1] ^= 1
            return bytes(frame)

        def skipped(session):
            session.seal({"type": "ping", "data": {}})
            return session.seal({"type": "ping", "data": {}})

        for frame in (flipped, skipped):
            with self.subTest(frame=frame.__name__), self.assertRaises(protocol.ProtocolError) as caught:
                self._first(frame)
            self.assertNotIsInstance(caught.exception, protocol.NotAnObject)


if __name__ == "__main__":
    unittest.main()
