"""Test vectors for phone clients of the v6 wire protocol, proved against the desktop's own
protocol.py and pairing.py rather than a description of them, so another implementation can be
checked byte for byte against the wire format.

Every input (tokens, codes, key material, plaintexts) is fixed here, never random, so the file
this writes is a golden file: rerun it and the bytes do not move unless the wire format itself
changed.

    python3 tools/phone_vectors.py

Writes tools/phone_vectors.json beside this script.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from nacl.bindings import crypto_aead_chacha20poly1305_ietf_encrypt, crypto_scalarmult, crypto_scalarmult_base  # noqa: E402

from core import keytable  # noqa: E402
from core import pairing  # noqa: E402
from core import protocol  # noqa: E402

OLD_SECTIONS = ("protocol_version", "preamble", "derive_key", "frames", "pairing", "messages")
WALL = 1_790_000_000
I_TO_R = "initiator_to_responder"
R_TO_I = "responder_to_initiator"


def h(data: bytes) -> str:
    return data.hex()


def unhex(text: str) -> bytes:
    return bytes.fromhex(text)


def fixed_bytes(label: str, length: int = 32) -> bytes:
    """Stand-ins for the random values of an exchange, so the vectors never move."""
    return hashlib.sha256(label.encode("ascii")).digest()[:length]


def token_for(label: str) -> str:
    """A token as pairing makes one: the `b64` of 32 bytes."""
    return protocol.id_text(fixed_bytes(f"{label}-token"))


def scripted(*values: bytes):
    """An entropy source that hands out `values` in order, each checked for its length."""
    queue = list(values)

    def entropy(length: int) -> bytes:
        value = queue.pop(0)
        assert len(value) == length, (len(value), length)
        return value

    return entropy


def leb128(length: int) -> bytes:
    out = bytearray()
    while True:
        out.append((length & 0x7F) | (0x80 if length > 0x7F else 0))
        length >>= 7
        if not length:
            return bytes(out)


def lv(*parts: bytes) -> bytes:
    """The CPace draft's lv_cat, written here: each part behind its length as LEB128. A part of 128
    bytes or more needs two length bytes."""
    result = b"".join(leb128(len(part)) + part for part in parts)
    assert result == pairing._lv_cat(*parts)
    return result


def hkdf(ikm: bytes, salt: bytes, info: bytes) -> bytes:
    """RFC 5869 for one 32-byte block."""
    return hmac.new(hmac.new(salt, ikm, hashlib.sha256).digest(), info + b"\x01", hashlib.sha256).digest()


P = 2**255 - 19


def x25519_ladder(scalar: bytes, u: bytes) -> bytes:
    """RFC 7748's ladder in Python integers: bit 255 of the point ignored, the point reduced mod p,
    and a low-order point giving 32 zero bytes (z2 is 0, and pow(0, p - 2, p) is 0)."""
    k = bytearray(scalar)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    k = int.from_bytes(k, "little")
    x1 = (int.from_bytes(u, "little") & ((1 << 255) - 1)) % P
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in range(254, -1, -1):
        bit = (k >> t) & 1
        swap ^= bit
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b = (x2 + z2) % P, (x2 - z2) % P
        aa, bb = a * a % P, b * b % P
        e = (aa - bb) % P
        c, d = (x3 + z3) % P, (x3 - z3) % P
        da, cb = d * a % P, c * b % P
        x3, z3 = (da + cb) ** 2 % P, x1 * (da - cb) ** 2 % P
        x2, z2 = aa * bb % P, e * (aa + 121665 * e) % P
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, P - 2, P) % P).to_bytes(32, "little")


# draft-irtf-cfrg-cpace-21, appendix B.1: the IETF's own numbers for CPACE-X25519-SHA512.
CPACE_DRAFT = {
    "prs_ascii": "Password",
    "ci_hex": "0b415f696e69746961746f720b425f726573706f6e646572",
    "sid_hex": "7e4b4791d6a8ef019b936c79fb7f2c57",
    "generator_hex": "d04bf6d41f6a289632a2e929fa29bebd51092512a7829fdde7d314b62f05a73f",
    "ada_ascii": "ADa",
    "ya_scalar_hex": "21b4f4bd9e64ed355c3eb676a28ebedaf6d8f17bdc365995b319097153044080",
    "ya_hex": "1d13c89278cdadd826f6d8d7f887701430f8380ddc17611cdd6dc989ce0c9f32",
    "adb_ascii": "ADb",
    "yb_scalar_hex": "848b0779ff415f0af4ea14df9dd1d3c29ac41d836c7808896c4eba19c51ac40a",
    "yb_hex": "248cccf6d5cdc3646f0ad593f9e6cef4e69d4945f8372e623512ecea32185623",
    "k_hex": "5b067effbdc0b2a0e1d907b21ebb25cfedb96a852179a847c37e43ee71322c6b",
    "isk_hex": (
        "6e19b875f7a561d6b3ca3dbb9ef42ac55de3e717881018204b8922b4d5e53bb2"
        "aa82c300bea7b65d2b671da71922ddf6472301b79bc270adfa8bf413285f2263"
    ),
}

# RFC 9380, appendix J.4.2: u[0] and Q.x of each vector, the map's input and output before the
# cofactor is cleared. Both are integers printed big-endian, as the RFC prints them.
ELLIGATOR2 = [
    ("608d892b641f0328523802a6603427c26e55e6f27e71a91a478148d45b5093cd", "51125222da5e763d97f3c10fcc92ea6860b9ccbbd2eb1285728f566721c1e65b"),
    ("46f5b22494bfeaa7f232cc8d054be68561af50230234d7d1d63d1d9abeca8da5", "7d56d1e08cb0ccb92baf069c18c49bb5a0dcd927eff8dcf75ca921ef7f3e6eeb"),
    ("235fe40c443766ce7e18111c33862d66c3b33267efa50d50f9e8e5d252a40aaa", "3fbe66b9c9883d79e8407150e7c2a1c8680bee496c62fabe4619a72b3cabe90f"),
    ("001e92a544463bda9bd04ddbe3d6eed248f82de32f522669efc5ddce95f46f5b", "227e0bb89de700385d19ec40e857db6e6a3e634b1c32962f370d26f84ff19683"),
    ("1a68a1af9f663592291af987203393f707305c7bac9c8d63d6a729bdc553dc19", "3bcd651ee54d5f7b6013898aab251ee8ecc0688166fce6e9548d38472f6bd196"),
]

# draft-irtf-cfrg-cpace-21, appendix B.1.10: shares on low-order points. With this scalar the
# first list gives the neutral element, so an exchange carrying one must be refused; the second
# gives the listed product, and only when bit 255 of the share is ignored as RFC 7748 requires.
LOW_ORDER_SCALAR = "af46e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449aff"
LOW_ORDER_REFUSED = [
    "0000000000000000000000000000000000000000000000000000000000000000",
    "0100000000000000000000000000000000000000000000000000000000000000",
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800",
    "5f9c95bca3508c24b1d0b1559c83ef5b04445cc4581c8e86d8224eddd09f1157",
    "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
]


def check_published_vectors() -> None:
    """The IETF's and RFC 9380's numbers above, against pairing.py, before any of them is relied
    on: a phone checked against this file is then checked against the IETF's, not a typo."""
    draft = CPACE_DRAFT
    sid = bytes.fromhex(draft["sid_hex"])
    generator = pairing._generator(draft["prs_ascii"].encode("ascii"), bytes.fromhex(draft["ci_hex"]), sid)
    assert h(generator) == draft["generator_hex"]
    ya = pairing._x25519(bytes.fromhex(draft["ya_scalar_hex"]), generator)
    yb = pairing._x25519(bytes.fromhex(draft["yb_scalar_hex"]), generator)
    assert (h(ya), h(yb)) == (draft["ya_hex"], draft["yb_hex"])
    shared = pairing._x25519(bytes.fromhex(draft["ya_scalar_hex"]), yb)
    assert h(shared) == draft["k_hex"] == h(pairing._x25519(bytes.fromhex(draft["yb_scalar_hex"]), ya))
    isk = hashlib.sha512(
        pairing._lv_cat(b"CPace255_ISK", sid, shared)
        + pairing._lv_cat(ya, draft["ada_ascii"].encode("ascii"))
        + pairing._lv_cat(yb, draft["adb_ascii"].encode("ascii"))
    ).digest()
    assert h(isk) == draft["isk_hex"]
    for element, mapped in ELLIGATOR2:
        assert pairing._elligator2(int(element, 16)) == int(mapped, 16)
    assert x25519_ladder(bytes.fromhex(draft["ya_scalar_hex"]), generator) == ya
    assert x25519_ladder(bytes.fromhex(draft["ya_scalar_hex"]), yb) == shared


# ----------------------------------------------------------------------------------------------
# v6

def build_constants() -> dict:
    return {
        "version": protocol.LINK_VERSION,
        "salt_ascii": protocol.LINK_SALT.decode("ascii"),
        "key_id_info_ascii": protocol.KEY_ID_INFO.decode("ascii"),
        "frame_info_label_ascii": protocol.KEY_INFO.decode("ascii"),
        "key_id_bytes": protocol.KEY_ID_SIZE,
        "preamble_bytes": protocol.LINK_PREAMBLE_SIZE,
        "short_reply_bytes": protocol.SHORT_REPLY_SIZE,
        "min_frame_bytes": protocol.MIN_FRAME_BYTES,
        "first_frame_max_bytes": protocol.HELLO_MAX_BYTES,
    }


def build_key_id() -> dict:
    cases = []
    for label in ("v6-key-id-1", "v6-key-id-2", "v6-key-id-3"):
        token = token_for(label)
        key = protocol.key_id(token)
        assert key == hkdf(token.encode("ascii"), b"beamer-link-v6", b"beamer-key-id")[:16]
        cases.append({"token": token, "key_id_hex": h(key)})
    canonical = cases[0]["token"]
    typed = ["hunter2", canonical[:-1] + ("B" if canonical[-1] != "B" else "C"), canonical + "=", "A" * 42]
    for token in typed:
        assert not protocol.is_paired_token(token)
    return {
        "$comment": "A token made by pairing is the b64 of 32 bytes in its one spelling. A typed token has no key id and never links.",
        "cases": cases,
        "typed_tokens": typed,
    }


def build_preamble() -> dict:
    token = token_for("v6-preamble")
    prefix, secret = fixed_bytes("v6-preamble-prefix", 8), fixed_bytes("v6-preamble-secret")
    session = protocol.LinkSession(token, protocol.ROLE_INITIATOR, prefix, secret)
    preamble = session.preamble()
    share = crypto_scalarmult_base(secret)
    assert preamble == b"BEAMY\x06" + prefix + protocol.key_id(token) + share and len(preamble) == 62
    assert share == x25519_ladder(secret, bytes([9]) + bytes(31))
    return {
        "token": token,
        "prefix_hex": h(prefix),
        "key_id_hex": h(protocol.key_id(token)),
        "secret_hex": h(secret),
        "share_hex": h(share),
        "preamble_hex": h(preamble),
    }


def build_short_reply() -> dict:
    prefix = fixed_bytes("v6-short-reply-prefix", 8)
    reply = protocol.short_reply(prefix)
    assert reply == b"BEAMY\x06" + prefix and len(reply) == 14
    return {"prefix_hex": h(prefix), "reply_hex": h(reply)}


def derive_case(comment: str, token: str, prefix_i: bytes, prefix_r: bytes, secret_i: bytes, secret_r: bytes) -> dict:
    key_id = protocol.key_id(token)
    share_i, share_r = crypto_scalarmult_base(secret_i), crypto_scalarmult_base(secret_r)
    assert share_i == x25519_ladder(secret_i, bytes([9]) + bytes(31)) and share_r == x25519_ladder(secret_r, bytes([9]) + bytes(31))
    shared = crypto_scalarmult(secret_i, share_r)
    assert shared == crypto_scalarmult(secret_r, share_i) == x25519_ladder(secret_i, share_r)
    ikm = token.encode("ascii") + shared
    assert ikm == protocol.link_ikm(token, shared) and len(ikm) == 75
    infos, keys = {}, {}
    for direction in (1, 2):
        info = b"beamer-frames" + b"\x06" + key_id + prefix_i + prefix_r + share_i + share_r + bytes([direction])
        assert info == protocol.link_info(key_id, prefix_i, prefix_r, share_i, share_r, direction) and len(info) == 111
        keys[direction] = hkdf(ikm, b"beamer-link-v6", info)
        assert keys[direction] == protocol.link_key(token, shared, key_id, prefix_i, prefix_r, share_i, share_r, direction)
        infos[direction] = info
    initiator = protocol.LinkSession(token, protocol.ROLE_INITIATOR, prefix_i, secret_i)
    responder = protocol.LinkSession(token, protocol.ROLE_RESPONDER, prefix_r, secret_r)
    responder.accept_preamble(initiator.preamble())
    initiator.accept_preamble(responder.preamble())
    assert (initiator._send_key, initiator._recv_key) == (keys[1], keys[2]) and (responder._send_key, responder._recv_key) == (keys[2], keys[1])
    return {
        "comment": comment,
        "token": token,
        "key_id_hex": h(key_id),
        "prefix_i_hex": h(prefix_i),
        "prefix_r_hex": h(prefix_r),
        "secret_i_hex": h(secret_i),
        "secret_r_hex": h(secret_r),
        "share_i_hex": h(share_i),
        "share_r_hex": h(share_r),
        "shared_hex": h(shared),
        "ikm_hex": h(ikm),
        "info_i_hex": h(infos[1]),
        "info_r_hex": h(infos[2]),
        "key_i_hex": h(keys[1]),
        "key_r_hex": h(keys[2]),
    }


def build_derive_key() -> dict:
    token = token_for("v6-derive")
    prefix_a, prefix_b = fixed_bytes("v6-derive-prefix-a", 8), fixed_bytes("v6-derive-prefix-b", 8)
    secret_a, secret_b = fixed_bytes("v6-derive-secret-a"), fixed_bytes("v6-derive-secret-b")
    return {
        "$comment": (
            "info_i and key_i are for direction 0x01, the frames the initiator seals; info_r and key_r for 0x02. "
            "The second case gives the same two machines' prefixes and secrets the opposite roles: the shared secret is "
            "the same and every key changes."
        ),
        "cases": [
            derive_case("A initiates, B responds", token, prefix_a, prefix_b, secret_a, secret_b),
            derive_case("the roles swapped: B initiates, A responds", token, prefix_b, prefix_a, secret_b, secret_a),
        ],
    }


def build_low_order() -> dict:
    secret = fixed_bytes("v6-low-order-secret")
    shares, seen = [], set()
    for canonical in LOW_ORDER_REFUSED:
        raw = bytes.fromhex(canonical)
        for comment, share in ((f"{canonical[:8]}..., bit 255 as listed", raw), (f"{canonical[:8]}..., bit 255 set", raw[:31] + bytes([raw[31] | 0x80]))):
            if share in seen:
                continue
            seen.add(share)
            result = x25519_ladder(secret, share)
            assert result == bytes(32)
            try:
                protocol._x25519_shared(secret, share)
            except protocol.PreambleRefused:
                pass
            else:
                raise AssertionError(f"low-order share {h(share)} was not refused")
            shares.append({"comment": comment, "share_hex": h(share), "result_hex": h(result)})
    p_low, p1_low = P.to_bytes(32, "little"), (P + 1).to_bytes(32, "little")
    assert h(p_low) in {s["share_hex"] for s in shares} and h(p1_low) in {s["share_hex"] for s in shares}
    return {
        "$comment": (
            "X25519 shares of low order, including the non-canonical spellings (u = p and p + 1, and each with bit 255 set, "
            "which X25519 ignores). Every one gives 32 zero bytes whatever the secret; a side that gets that result, or whose "
            "X25519 refuses the share, ends the handshake and sends nothing."
        ),
        "secret_hex": h(secret),
        "shares": shares,
    }


class ScriptedLink:
    """One link run through real LinkSessions, each frame also sealed a second time by hand and
    each opened by the other end before it is written down."""

    def __init__(self, name: str, comment: str, token: str, initiator_id: bytes, responder_id: bytes):
        self.name, self.comment, self.token = name, comment, token
        self.ini_id, self.res_id = initiator_id, responder_id
        self.prefix_i, self.prefix_r = fixed_bytes(f"{name}-prefix-i", 8), fixed_bytes(f"{name}-prefix-r", 8)
        self.secret_i, self.secret_r = fixed_bytes(f"{name}-secret-i"), fixed_bytes(f"{name}-secret-r")
        self.ini = protocol.LinkSession(token, protocol.ROLE_INITIATOR, self.prefix_i, self.secret_i)
        self.res = protocol.LinkSession(token, protocol.ROLE_RESPONDER, self.prefix_r, self.secret_r)
        self.pre_i = self.ini.preamble()
        self.res.accept_preamble(self.pre_i)
        self.pre_r = self.res.preamble()
        self.ini.accept_preamble(self.pre_r)
        shared = crypto_scalarmult(self.secret_i, self.pre_r[30:])
        ikm = token.encode("ascii") + shared
        info = b"beamer-frames\x06" + protocol.key_id(token) + self.prefix_i + self.prefix_r + self.pre_i[30:] + self.pre_r[30:]
        self.keys = {I_TO_R: hkdf(ikm, b"beamer-link-v6", info + b"\x01"), R_TO_I: hkdf(ikm, b"beamer-link-v6", info + b"\x02")}
        self.counters = {I_TO_R: 0, R_TO_I: 0}
        self.frames = []

    def send(self, direction: str, message: dict) -> dict:
        sender, receiver = (self.ini, self.res) if direction == I_TO_R else (self.res, self.ini)
        plain = protocol.dumps_message(message)
        frame = sender.seal(message)
        self.counters[direction] += 1
        counter = struct.pack(">I", self.counters[direction])
        prefix = self.prefix_i if direction == I_TO_R else self.prefix_r
        by_hand = crypto_aead_chacha20poly1305_ietf_encrypt(plain, None, prefix + counter, self.keys[direction])
        assert frame == struct.pack(">I", 4 + len(by_hand)) + counter + by_hand
        assert dict(receiver.open(frame[4:])) == message
        entry = {"direction": direction, "counter": self.counters[direction], "message": message, "plaintext_json": plain.decode("utf-8"), "frame_hex": h(frame)}
        self.frames.append(entry)
        return entry

    def describe(self) -> dict:
        return {
            "name": self.name,
            "comment": self.comment,
            "token": self.token,
            "initiator": {"id_hex": h(self.ini_id), "prefix_hex": h(self.prefix_i), "secret_hex": h(self.secret_i)},
            "responder": {"id_hex": h(self.res_id), "prefix_hex": h(self.prefix_r), "secret_hex": h(self.secret_r)},
            "preamble_i_hex": h(self.pre_i),
            "preamble_r_hex": h(self.pre_r),
            "frames": self.frames,
        }


INITIATOR_ID = fixed_bytes("v6-initiator-id", 16)
RESPONDER_ID = fixed_bytes("v6-responder-id", 16)
THIRD_ID = fixed_bytes("v6-third-id", 16)
CAPS = ["clipboard", "clipboard_image", "gestures", "media_keys", "text", "settings"]
INITIATOR_NAME = "Zoë’s \U0001d539eamer Mac"
# 48 code points and 177 bytes of UTF-8: the name's length and the whole of ADa both need two LEB128 bytes.
LONG_NAME = "Zoë’s " + "\U0001d539" * 42


def build_frames() -> dict:
    main = ScriptedLink(
        "main",
        "One pair, scripted end to end: the handshake frames, announcements, a take that lands at an edge, the clipboard and input, "
        "an acknowledgement, the responder sending the owner on, and the owner letting go.",
        token_for("v6-main"), INITIATOR_ID, RESPONDER_ID,
    )
    main.send(I_TO_R, protocol.hello_v6(INITIATOR_ID, INITIATOR_NAME, "macos", "1.5.0", CAPS, 24820, hw="02:00:5e:10:00:01"))
    main.send(R_TO_I, protocol.welcome_v6(RESPONDER_ID, "VECTOR-PC", "windows", "1.5.0", CAPS, True, hw="02:00:5e:10:00:02"))
    main.send(I_TO_R, protocol.paired_msg([THIRD_ID]))
    main.send(R_TO_I, protocol.paired_msg([THIRD_ID]))
    main.send(R_TO_I, protocol.arrangement_v6("left", WALL - 600, RESPONDER_ID))
    main.send(I_TO_R, protocol.focus_v6(1, RESPONDER_ID, edge="left", offset=0.42, resistance_px=120, reach=[THIRD_ID]))
    main.send(I_TO_R, {"type": protocol.MSG_CLIPBOARD, "data": {"text": "copied on the Mac"}})
    main.send(R_TO_I, protocol.accept_msg(1))
    main.send(I_TO_R, protocol.mousemove_msg(12, -7, seq=1))
    main.send(I_TO_R, protocol.key_msg(protocol.MSG_KEYDOWN, "a", us="a", seq=2))
    main.send(I_TO_R, protocol.key_msg(protocol.MSG_KEYUP, "a", us="a", seq=3))
    main.send(I_TO_R, protocol.text_msg("café \U0001f600", seq=4))
    main.send(I_TO_R, {"type": protocol.MSG_MOUSEDOWN, "data": {"button": "left", "seq": 5}})
    main.send(I_TO_R, {"type": protocol.MSG_MOUSEUP, "data": {"button": "left", "seq": 6}})
    main.send(I_TO_R, {"type": protocol.MSG_SCROLL, "data": {"dy": -14.5, "dx": 0, "mode": "pixel", "seq": 7}})
    main.send(I_TO_R, {"type": protocol.MSG_GESTURE, "data": {"name": "swipe_left", "seq": 8}})
    main.send(I_TO_R, {"type": "ping", "data": {}})
    main.send(R_TO_I, protocol.accepts_msg(True))
    main.send(R_TO_I, protocol.ack_v6(8, 1830))
    main.send(R_TO_I, protocol.switch_v6(1, THIRD_ID, edge="right", offset=0.5))
    main.send(I_TO_R, protocol.focus_v6(2, THIRD_ID))

    second = ScriptedLink(
        "second_link_refuse",
        "Another machine's link to the same responder while the first owns it: its take is refused as owned.",
        token_for("v6-second"), THIRD_ID, RESPONDER_ID,
    )
    second.send(I_TO_R, protocol.hello_v6(THIRD_ID, "Vector Linux", "linux", "1.5.0", ["text"], 24820))
    second.send(R_TO_I, protocol.welcome_v6(RESPONDER_ID, "VECTOR-PC", "windows", "1.5.0", CAPS, True))
    second.send(I_TO_R, protocol.focus_v6(1, RESPONDER_ID, edge="right", offset=0.1, resistance_px=120, reach=[]))
    second.send(R_TO_I, protocol.refuse_msg(1, "owned"))

    invalid = ScriptedLink(
        "invalid_hello",
        "A first frame that opens but is not a hello is answered with welcome carrying only version and error invalid_hello; the link then closes.",
        token_for("v6-invalid"), INITIATOR_ID, RESPONDER_ID,
    )
    invalid.send(I_TO_R, {"type": "ping", "data": {}})
    invalid.send(R_TO_I, protocol.invalid_hello_v6())

    wrong = ScriptedLink(
        "wrong_id",
        "A well-formed hello whose id is not the one the responder holds for this pair is answered with welcome carrying only version and error wrong_id.",
        token_for("v6-wrong"), INITIATOR_ID, RESPONDER_ID,
    )
    wrong_id = fixed_bytes("v6-wrong-id", 16)
    wrong.send(I_TO_R, protocol.hello_v6(wrong_id, "Not the paired machine", "macos", "1.5.0", CAPS, 24820))
    wrong.send(R_TO_I, protocol.wrong_id_v6())

    links = [main.describe(), second.describe(), invalid.describe(), wrong.describe()]
    links[3]["wrong_id_hello_id_hex"] = h(wrong_id)
    return {
        "$comment": (
            "Preambles and frames of scripted links, in order. `message` is the object sealed, `plaintext_json` its exact bytes as text "
            "(compact, UTF-8, keys in this order), `frame_hex` the whole frame as it crosses the wire: length, counter, ciphertext and tag. "
            "Each side counts its own frames from 1. A link's `initiator` and `responder` give the fixed prefix and ephemeral secret "
            "that made its keys."
        ),
        "links": links,
    }


def build_bad_frames() -> dict:
    link = ScriptedLink("bad_frames", "Fresh responders offered one bad frame each.", token_for("v6-bad"), INITIATOR_ID, RESPONDER_ID)
    good_message = protocol.hello_v6(INITIATOR_ID, "Vector Mac", "macos", "1.5.0", ["text"], 24820)
    good = link.send(I_TO_R, good_message)
    description = {key: value for key, value in link.describe().items() if key not in ("frames", "comment")}

    flipped = bytearray(unhex(good["frame_hex"]))
    flipped[-1] ^= 1

    twin = ScriptedLink("bad_frames", "", link.token, INITIATOR_ID, RESPONDER_ID)
    twin.send(I_TO_R, {"type": "ping", "data": {}})
    skipped = twin.send(I_TO_R, good_message)
    assert skipped["counter"] == 2

    reflect = ScriptedLink("bad_frames", "", link.token, INITIATOR_ID, RESPONDER_ID)
    reflected = reflect.send(R_TO_I, {"type": "ping", "data": {}})

    cases = [
        {"comment": "the last tag byte flipped", "receiver": "responder", "frame_hex": h(bytes(flipped)), "must": "close_without_reply"},
        {"comment": "a skipped counter: a valid hello sealed as counter 2, offered as the first frame", "receiver": "responder", "frame_hex": skipped["frame_hex"], "must": "close_without_reply"},
        {"comment": "sealed in the wrong direction: a frame the responder sealed, offered to the responder", "receiver": "responder", "frame_hex": reflected["frame_hex"], "must": "close_without_reply"},
    ]
    for case in cases:
        initiator, responder = sessions_from(description)
        try:
            responder.open(unhex(case["frame_hex"])[4:])
        except protocol.ProtocolError:
            pass
        else:
            raise AssertionError(case["comment"])
    return {
        "$comment": "Each case is offered to a responder that has accepted nothing yet; it must end the link without sending a byte. `good_frame_hex` is the untouched first frame the cases are made from.",
        "link": description,
        "good_message": good_message,
        "good_frame_hex": good["frame_hex"],
        "cases": cases,
    }


def sessions_from(link: dict):
    initiator = protocol.LinkSession(link["token"], protocol.ROLE_INITIATOR, unhex(link["initiator"]["prefix_hex"]), unhex(link["initiator"]["secret_hex"]))
    responder = protocol.LinkSession(link["token"], protocol.ROLE_RESPONDER, unhex(link["responder"]["prefix_hex"]), unhex(link["responder"]["secret_hex"]))
    responder.accept_preamble(initiator.preamble())
    initiator.accept_preamble(responder.preamble())
    return initiator, responder


def build_ids() -> dict:
    crafted = unhex("fbffbf") + bytes([0x11] * 13)
    ids = [fixed_bytes("v6-id-1", 16), fixed_bytes("v6-id-2", 16), crafted]
    cases = [{"id_hex": h(ident), "b64": protocol.id_text(ident)} for ident in ids]
    assert "-" in cases[2]["b64"] or "_" in cases[2]["b64"]
    spelling = cases[0]["b64"]
    flipped = spelling[:-1] + unused_bits_set(spelling[-1])
    standard = cases[2]["b64"].replace("-", "+").replace("_", "/")
    refused = [
        {"kind": "non_canonical", "comment": "padding added", "text": spelling + "==", "id_hex": cases[0]["id_hex"]},
        {"kind": "non_canonical", "comment": "the standard alphabet, + and / for - and _", "text": standard, "id_hex": cases[2]["id_hex"]},
        {"kind": "non_canonical", "comment": "the last character changed in its unused bits", "text": flipped, "id_hex": cases[0]["id_hex"]},
        {"kind": "all_zero", "comment": "sixteen zero bytes are never an id", "text": "A" * 22},
        {"kind": "wrong_length", "comment": "15 bytes", "text": protocol.id_text(ids[0][:15])},
        {"kind": "wrong_length", "comment": "17 bytes", "text": protocol.id_text(ids[0] + b"\x07")},
        {"kind": "whitespace", "comment": "a leading space", "text": " " + spelling},
        {"kind": "non_ascii", "comment": "a character outside ASCII", "text": spelling[:-1] + "é"},
    ]
    for item in refused:
        assert protocol.read_id(item["text"]) is None, item
    for case in cases:
        assert protocol.read_id(case["b64"]) == unhex(case["id_hex"])
    return {
        "$comment": "A machine id is 16 bytes in JSON as URL-safe base64 without padding, 22 characters, in its one spelling: decode, encode again, refuse the text if it differs.",
        "cases": cases,
        "refused": refused,
    }


URL_SAFE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def unused_bits_set(last: str) -> str:
    """The character of the same value in its top bits as the last of a 16-byte value's 22 characters,
    with one of its four unused low bits set: a lenient decoder reads the same bytes, `id_text` writes
    the other spelling."""
    index = URL_SAFE.index(last)
    assert index & 0xF == 0
    return URL_SAFE[index | 1]


def hello_fields(**change) -> dict:
    data = {
        "version": 6, "id": protocol.id_text(INITIATOR_ID), "name": "Vector Mac", "platform": "macos", "app": "1.5.0",
        "caps": ["clipboard", "text"], "port": 24820, "hw": "02:00:5e:10:00:01",
    }
    data.update(change)
    return data


def action_of(plain: bytes, reader: str) -> str:
    """What a desktop does with `plain` read as `reader` does: `end_link`, `ignore` (the message, the
    link goes on), `answer_invalid_hello` (a first frame that is not a valid hello), or `accepted`."""
    try:
        message = protocol.parse_message(plain)
    except protocol.NotAnObject:
        return "answer_invalid_hello" if reader == "hello" else "end_link"
    if reader == "hello":
        try:
            protocol.read_hello(message)
        except protocol.InvalidHello:
            return "answer_invalid_hello"
        return "accepted"
    if message.malformed:
        return "end_link" if reader in ("welcome", "ack") else "ignore"
    if reader in ("welcome", "ack"):
        try:
            {"welcome": protocol.read_welcome, "ack": protocol.read_ack}[reader](message)
        except protocol.ProtocolError:
            return "end_link"
        return "accepted"
    result = {
        "message": protocol.read_message,
        "input": protocol.read_input,
        "focus": lambda m: protocol.read_focus(m, bytes(range(1, 17))),
        "accept": protocol.read_accept,
        "refuse": protocol.read_refuse,
        "switch": protocol.read_switch,
        "arrangement": lambda m: protocol.read_arrangement_v6(m, now=WALL),
    }[reader](message)
    return "ignore" if result is None else "accepted"


def build_malformed() -> dict:
    def text(document) -> bytes:
        return json.dumps(document, separators=(",", ":")).encode("utf-8")

    def hello(**change) -> bytes:
        return text({"type": "hello", "data": hello_fields(**change)})

    def welcome(**change) -> bytes:
        data = {
            "version": 6, "id": protocol.id_text(RESPONDER_ID), "name": "VECTOR-PC", "platform": "windows", "app": "1.5.0",
            "caps": ["text"], "accepts": True,
        }
        data.update(change)
        return text({"type": "welcome", "data": data})

    move = b'{"type":"mousemove","data":{"dx":%s,"dy":0,"seq":%s}}'
    # (comment, body, reader, action, whether a parser that refuses the text outright may end the link instead)
    cases = [
        ("an integer written 1.0 (dx)", move % (b"1.0", b"1"), "input", "ignore", False),
        ("an integer written 1e0 (seq)", move % (b"1", b"1e0"), "input", "ignore", False),
        ("a boolean for an integer (seq)", move % (b"1", b"true"), "input", "ignore", False),
        ("a boolean for an integer (dx)", move % (b"false", b"1"), "input", "ignore", False),
        ("NaN in a number", b'{"type":"scroll","data":{"dy":NaN,"dx":0,"mode":"line","seq":1}}', "input", "ignore", True),
        ("Infinity in a number", b'{"type":"scroll","data":{"dy":Infinity,"dx":0,"mode":"line","seq":1}}', "input", "ignore", True),
        ("a byte order mark before the object", b"\xef\xbb\xbf" + move % (b"1", b"1"), "input", "ignore", True),
        ("a lone surrogate in a string", b'{"type":"text","data":{"text":"\\ud800","seq":1}}', "input", "ignore", True),
        ("not an object: an array", b"[1,2]", "message", "end_link", False),
        ("not an object: a number", b"7", "message", "end_link", False),
        ("not UTF-8", b"\xff\xfe{}", "message", "end_link", False),
        ("no type", b'{"data":{}}', "message", "ignore", False),
        ("a type that is not a string", b'{"type":5,"data":{}}', "message", "ignore", False),
        ("data that is not an object", b'{"type":"ping","data":[]}', "message", "ignore", False),
        ("hello: hw in upper case", hello(hw="02:00:5E:10:00:01"), "hello", "answer_invalid_hello", False),
        ("hello: hw too short", hello(hw="02:00:5e"), "hello", "answer_invalid_hello", False),
        ("hello: app empty", hello(app=""), "hello", "answer_invalid_hello", False),
        ("hello: app over 32 characters", hello(app="x" * 33), "hello", "answer_invalid_hello", False),
        ("hello: app outside printable ASCII", hello(app="1.5.0é"), "hello", "answer_invalid_hello", False),
        ("hello: caps not a list", hello(caps="clipboard"), "hello", "answer_invalid_hello", False),
        ("hello: more than 16 caps", hello(caps=[f"cap{i}" for i in range(17)]), "hello", "answer_invalid_hello", False),
        ("hello: an empty cap", hello(caps=["text", ""]), "hello", "answer_invalid_hello", False),
        ("hello: a cap that is not a string", hello(caps=["text", 5]), "hello", "answer_invalid_hello", False),
        ("hello: platform with a space", hello(platform="Mac OS"), "hello", "answer_invalid_hello", False),
        ("hello: platform over 16 characters", hello(platform="a" * 17), "hello", "answer_invalid_hello", False),
        ("hello: name empty", hello(name=""), "hello", "answer_invalid_hello", False),
        ("hello: name of 49 code points", hello(name="n" * 49), "hello", "answer_invalid_hello", False),
        ("hello: id of sixteen zero bytes", hello(id="A" * 22), "hello", "answer_invalid_hello", False),
        ("hello: id of the wrong length", hello(id=protocol.id_text(INITIATOR_ID[:15])), "hello", "answer_invalid_hello", False),
        ("hello: version 5", hello(version=5), "hello", "answer_invalid_hello", False),
        ("hello: port above 65535", hello(port=65536), "hello", "answer_invalid_hello", False),
        ("hello: port as a string", hello(port="24820"), "hello", "answer_invalid_hello", False),
        ("hello: port written 1.0", hello(port=1.0), "hello", "answer_invalid_hello", False),
        ("welcome: accepts as a string", welcome(accepts="true"), "welcome", "end_link", False),
        ("welcome: accepts missing", text({"type": "welcome", "data": {k: v for k, v in json.loads(welcome())["data"].items() if k != "accepts"}}), "welcome", "end_link", False),
        ("welcome: an error that is not one of the two", text({"type": "welcome", "data": {"version": 6, "error": "go_away"}}), "welcome", "end_link", False),
        ("welcome: id of sixteen zero bytes", welcome(id="A" * 22), "welcome", "end_link", False),
        ("welcome: version 5", welcome(version=5), "welcome", "end_link", False),
        ("ack: seq written 1.0", b'{"type":"ack","data":{"seq":1.0,"held_us":0}}', "ack", "end_link", False),
        ("ack: held_us negative", b'{"type":"ack","data":{"seq":1,"held_us":-1}}', "ack", "end_link", False),
        ("ack: held_us above 2^31 - 1", b'{"type":"ack","data":{"seq":1,"held_us":2147483648}}', "ack", "end_link", False),
        ("ack: held_us missing", b'{"type":"ack","data":{"seq":1}}', "ack", "end_link", False),
        ("ack: locked false", b'{"type":"ack","data":{"seq":1,"held_us":0,"locked":false}}', "ack", "end_link", False),
        ("accept: route 0", b'{"type":"accept","data":{"route":0}}', "accept", "ignore", False),
        ("accept: route as a string", b'{"type":"accept","data":{"route":"1"}}', "accept", "ignore", False),
        ("refuse: no route", b'{"type":"refuse","data":{"why":"owned"}}', "refuse", "ignore", False),
        ("switch: next of sixteen zero bytes", text({"type": "switch", "data": {"route": 1, "next": "A" * 22}}), "switch", "ignore", False),
        ("switch: edge without offset", text({"type": "switch", "data": {"route": 1, "next": protocol.id_text(THIRD_ID), "edge": "left"}}), "switch", "ignore", False),
        ("switch: an edge that is not one", text({"type": "switch", "data": {"route": 1, "next": protocol.id_text(THIRD_ID), "edge": "middle", "offset": 0.5}}), "switch", "ignore", False),
    ]
    result = []
    for comment, plain, reader, action, parser_may_end in cases:
        assert action_of(plain, reader) == action, comment
        try:
            decoded = plain.decode("utf-8")
        except UnicodeDecodeError:
            decoded = None
        case = {"comment": comment, "hex": h(plain), "text": decoded, "reader": reader, "action": action}
        if parser_may_end:
            case["also_acceptable"] = "end_link"
        result.append(case)

    repeated = b'{"type":"mousemove","data":{"dx":1,"dx":2,"dy":0,"seq":1}}'
    assert action_of(repeated, "input") == "ignore"
    good = hello()
    assert action_of(good, "hello") == "accepted"
    return {
        "$comment": (
            "Message bodies a receiver must not act on, and what a desktop does with each: `end_link` ends the link, `ignore` drops the message and the link goes on, "
            "`answer_invalid_hello` is a first frame answered with welcome carrying error invalid_hello (and the link closed). `reader` says which validator a desktop "
            "reads it with: a hello or welcome as the first frame each way, an ack, or any other message. `also_acceptable` is for a text a JSON parser may refuse outright "
            "(WIRE.md, Encodings): ending the link instead of ignoring it is allowed. `well_formed_hello` is accepted, so the refusals around it mean something."
        ),
        "well_formed_hello": {"hex": h(good), "text": good.decode("utf-8")},
        "cases": result,
        "implementation_defined": [
            {
                "comment": "a repeated key: a receiver may refuse the message or keep the last value (WIRE.md, Encodings); a desktop ignores it, and a correct sender never sends one",
                "hex": h(repeated), "text": repeated.decode("utf-8"), "reader": "input", "desktop_action": "ignore",
            }
        ],
    }


def build_qr() -> dict:
    good = []
    for host, port, code, label in (("192.168.0.7", 24821, "482913", "v6-qr-1"), ("192.168.1.20", 24821, "007045", "v6-qr-2")):
        secret = fixed_bytes(label, 16)
        qr = pairing.qr_text(host, port, code, secret)
        assert pairing.read_qr(qr) == {"host": host, "port": port, "code": code, "secret": secret} and len(qr.split("k=")[1]) == 22
        good.append({"text": qr, "host": host, "port": port, "code": code, "secret_hex": h(secret)})
    base = good[0]["text"]
    k = base.split("k=")[1]
    standard_k = protocol.id_text(unhex("fbffbf") + bytes(13))
    standard_k = standard_k.replace("-", "+").replace("_", "/")
    assert "+" in standard_k or "/" in standard_k
    bad = [
        ("the wrong scheme", base.replace("beamer://", "beamerx://")),
        ("a slash before the query", base.replace("pair?", "pair/?")),
        ("no k", base.rsplit("&k=", 1)[0]),
        ("an extra key", base + "&x=1"),
        ("the keys in another order", f"beamer://pair?port=24821&host=192.168.0.7&code=482913&k={k}"),
        ("an upper-case key", base.replace("host=", "Host=")),
        ("a repeated key", base + "&k=" + k),
        ("a host with a leading zero octet", base.replace("192.168.0.7", "192.168.0.07")),
        ("a host that is a name", base.replace("192.168.0.7", "beamer.example")),
        ("a host of five octets", base.replace("192.168.0.7", "192.168.0.7.5")),
        ("a host octet above 255", base.replace("192.168.0.7", "192.168.0.256")),
        ("port 0", base.replace("port=24821", "port=0")),
        ("port 65536", base.replace("port=24821", "port=65536")),
        ("a port with a leading zero", base.replace("port=24821", "port=024821")),
        ("a port with a sign", base.replace("port=24821", "port=+24821")),
        ("a port with a digit percent-encoded: the text is read as it is", base.replace("port=24821", "port=%32%34821")),
        ("a code of five digits", base.replace("code=482913", "code=48291")),
        ("a code with a letter", base.replace("code=482913", "code=48291a")),
        ("a code in full-width digits", base.replace("code=482913", "code=４８２９１３")),
        ("k of 21 characters", base.replace("k=" + k, "k=" + k[:-1])),
        ("k with padding", base + "=="),
        ("k in a non-canonical spelling: its last character changed in its unused bits", base[:-1] + unused_bits_set(k[-1])),
        ("k in the standard alphabet, + and / for - and _", base.replace("k=" + k, "k=" + standard_k)),
    ]
    for comment, text in bad:
        try:
            pairing.read_qr(text)
        except pairing.PairingError as exc:
            assert str(exc) == "bad_qr", comment
        else:
            raise AssertionError(comment)
    return {
        "$comment": "The text a pairing QR holds: four keys in order, lower case, each once, nothing else. `k` is the code's 16-byte QR secret as 22 characters of b64.",
        "good": good,
        "bad": [{"comment": comment, "text": text, "error": "bad_qr"} for comment, text in bad],
    }


KEY_ROWS = [
    ("Mac Control", "mac", "ctrl"), ("Mac right Control", "mac", "ctrl_r"), ("Mac Command", "mac", "cmd"),
    ("Mac right Command", "mac", "cmd_r"), ("Mac Option", "mac", "alt"), ("Mac right Option", "mac", "alt_r"),
    ("PC Control", "pc", "ctrl"), ("PC right Control", "pc", "ctrl_r"), ("PC Windows or Super", "pc", "cmd"),
    ("PC right Windows or Super", "pc", "cmd_r"), ("PC Alt", "pc", "alt"), ("PC right Alt or AltGr", "pc", "alt_r"),
]


def build_key_table() -> dict:
    platforms = {"mac": ("macos", "windows"), "pc": ("windows", "macos")}
    rows = []
    for label, sender, physical in KEY_ROWS:
        own, other = platforms[sender]
        rows.append({
            "key": label, "sender": sender, "physical": physical,
            "same_family": keytable.wire_name(physical, own, own, "semantic"),
            "across_semantic": keytable.wire_name(physical, own, other, "semantic"),
            "across_positional": keytable.wire_name(physical, own, other, "positional"),
            "across_mac_layout": (
                keytable.wire_name(physical, own, other, "mac_layout") if sender == "pc" else "—"
            ),
        })
    assert keytable.wire_name("ctrl", "freebsd", "macos", "semantic") == "cmd" and keytable.wire_name("ctrl", "freebsd", "windows", "semantic") == "ctrl"
    return {
        "$comment": (
            "WIRE.md section 7's table. `sender` is the family of the machine whose key was pressed (mac: macos and ios; pc: every other platform, an unknown one included); "
            "`physical` is the key on that machine's own keyboard; the columns are the name it travels as to a peer of the same family and across families under each saved style. "
            "`evdev` maps a Linux sender's key codes to physical names; a code not in it is a character key."
        ),
        "mac_platforms": sorted(keytable.MAC_PLATFORMS),
        "unknown_platform_is": "pc",
        "rows": rows,
        "names": sorted(keytable.SPEC_NAMES),
        "evdev": {str(code): name for code, name in sorted(keytable.EVDEV_NAMES.items())},
    }


def build_ack() -> dict:
    raw = [
        ("an ordinary acknowledgement", 5, 1_000_000, 1_012_400, 1_830),
        ("held longer than the round trip: the sample is clamped to 0", 6, 2_000_000, 2_000_900, 1_500),
        ("held_us asked above the limit is sent as 2^31 - 1", 7, 10_000_000, 13_000_000_000, 5_000_000_000),
        ("a negative held_us is sent as 0", 8, 500, 4_500, -5),
    ]
    cases = []
    for comment, seq, sent, arrived, asked in raw:
        message = protocol.ack_v6(seq, asked)
        on_wire = message["data"]["held_us"]
        assert on_wire == min(2**31 - 1, max(0, asked)) and protocol.read_ack(protocol.parse_message(protocol.dumps_message(message)))["held_us"] == on_wire
        cases.append({
            "comment": comment, "seq": seq, "sent_at_us": sent, "arrived_at_us": arrived,
            "held_us_asked": asked, "held_us_on_wire": on_wire, "sample_us": max(0, arrived - sent - on_wire), "message": message,
        })
    return {
        "$comment": "sample_us = max(0, (arrived_at_us - sent_at_us) - held_us): the round trip with the responder's holding time taken out, for an ack whose seq is higher than the last. `held_us_asked` is what a sender has, `held_us_on_wire` what it sends.",
        "cases": cases,
    }


def build_clipboard_images() -> dict:
    from core.tests import pngs
    limit = protocol.PNG_METADATA_MAX_BYTES
    stream = pngs.run(b"a", 1000)
    raw = [
        ("a 1 by 1 image", pngs.png(), True),
        ("an 8K screen's header, 7680 by 4320", pngs.png(7680, 4320), True),
        ("16384 by 2048, exactly 2^25 pixels", pngs.png(16384, 2048), True),
        ("bytes after IEND are ignored", pngs.png() + b"trailing", True),
        ("zTXt inflating to exactly 4 MiB", pngs.png(8, 8, pngs.ztxt(limit)), True),
        ("a side of 16385", pngs.png(16385, 1), False),
        ("6000 by 6000, past 2^25 pixels", pngs.png(6000, 6000), False),
        ("IHDR not the first chunk", protocol.PNG_SIGNATURE + pngs.chunk(b"tEXt", b"a\x00b") + pngs.png()[8:], False),
        ("no IEND", pngs.png()[:-12], False),
        ("a second IHDR", pngs.png(8, 8, pngs.ihdr(100000, 100000)), False),
        ("an APNG acTL", pngs.png(8, 8, pngs.chunk(b"acTL", bytes(8))), False),
        ("zTXt inflating to 4 MiB and one byte", pngs.png(8, 8, pngs.ztxt(limit + 1)), False),
        ("zTXt whose zlib stream is cut short", pngs.png(8, 8, pngs.chunk(b"zTXt", b"Comment\x00\x00" + stream[:-6])), False),
        ("iTXt with a compression flag of 2", pngs.png(8, 8, pngs.chunk(b"iTXt", b"Key\x00\x02\x00en\x00\x00text")), False),
    ]
    cases = []
    for comment, image, fits in raw:
        assert protocol.png_fits(image) is fits, comment
        cases.append({"comment": comment, "png_hex": h(image), "fits": fits})
    return {
        "$comment": "Clipboard images judged from their bytes before anything decodes them (WIRE.md section 3). `fits` false drops the image part of a clipboard message and keeps its text. Pixel data is not real past 1 by 1: nothing here decodes it.",
        "cases": cases,
    }


def build_v6() -> dict:
    return {
        "$comment": "Wire version 6 (WIRE.md). Hex is bytes in wire order. Generated from core/protocol.py and checked against HKDF, X25519 and ChaCha20-Poly1305 written apart from it.",
        "constants": build_constants(),
        "key_id": build_key_id(),
        "preamble": build_preamble(),
        "short_reply": build_short_reply(),
        "derive_key": build_derive_key(),
        "low_order": build_low_order(),
        "frames": build_frames(),
        "bad_frames": build_bad_frames(),
        "ids": build_ids(),
        "malformed": build_malformed(),
        "qr": build_qr(),
        "key_table": build_key_table(),
        "ack": build_ack(),
        "clipboard_images": build_clipboard_images(),
    }


# ----------------------------------------------------------------------------------------------
# pairing v3

def party(role: bytes, who: dict) -> bytes:
    return lv(role, unhex(who["id_hex"]), who["name"].encode("utf-8"), who["platform"].encode("ascii"), who["port"].to_bytes(2, "big"))


def side(name: str, label: str, platform: str, port: int) -> dict:
    return {"name": name, "id_hex": h(fixed_bytes(f"{label}-id", 16)), "platform": platform, "port": port}


def make_host(label: str, host: dict, code: str, qr_secret, entropy_pair_id: bytes, scalar: bytes):
    stored = []
    machine = pairing.PairingHost(
        name=host["name"], identity=unhex(host["id_hex"]), version=3, platform=host["platform"], port=host["port"],
        entropy=scripted(entropy_pair_id, qr_secret if qr_secret is not None else bytes(16), scalar),
        peers=list, store=lambda entry, replaced: stored.append(entry), wall=lambda: WALL,
    )
    machine.begin()
    machine.code = code
    return machine, stored


def build_exchange_v3(comment: str, label: str, code: str, qr: bool, requester: dict, host: dict) -> dict:
    pair_id = fixed_bytes(f"{label}-pair-id", 16)
    qr_secret = fixed_bytes(f"{label}-qr-secret", 16) if qr else None
    nonce = fixed_bytes(f"{label}-nonce", pairing.NONCE_BYTES)
    requester_scalar, host_scalar = fixed_bytes(f"{label}-requester-scalar"), fixed_bytes(f"{label}-host-scalar")
    requester = {**requester, "nonce_hex": h(nonce), "scalar_hex": h(requester_scalar)}
    host = {**host, "scalar_hex": h(host_scalar)}

    machine, stored = make_host(label, host, code, qr_secret, pair_id, host_scalar)
    beacon = machine.beacon(host["port"])
    assert beacon["pair"] == pair_id.hex() and beacon["id"] == pairing._b64(unhex(host["id_hex"])) and beacon["pairing"] == 3
    client = pairing.PairingClient(
        machine.pair_id, code, requester["name"], unhex(requester["id_hex"]), scripted(nonce, requester_scalar),
        version=3, platform=requester["platform"], port=requester["port"], qr_secret=qr_secret, wall=lambda: WALL,
    )
    start = client.start()
    answer = machine.handle(start, "10.1.2.3")
    confirm = client.accept(answer)
    done = machine.handle(confirm, "10.1.2.3")
    token = client.finish(done)
    assert len(stored) == 1 and stored[0]["token"] == token
    assert all(len(pairing.encode(message)) <= pairing.MAX_DATAGRAM_BYTES for message in (start, answer, confirm, done))

    prs = code.encode("ascii") + (qr_secret or b"")
    assert len(prs) == (22 if qr else 6)
    sid = lv(pair_id.hex().encode("ascii"), nonce)
    padding = bytes(128 - 1 - len(pairing._prepend_len(prs)) - len(pairing._prepend_len(b"CPace255")))
    assert len(padding) == (95 if qr else 111)
    generator_string = lv(b"CPace255", prs, padding, b"beamer-pair-v3", sid)
    generator = pairing._elligator2(int.from_bytes(hashlib.sha512(generator_string).digest()[:32], "little") & ((1 << 255) - 1)).to_bytes(32, "little")
    assert generator == pairing._generator(prs, b"beamer-pair-v3", sid)
    ya, yb = x25519_ladder(requester_scalar, generator), x25519_ladder(host_scalar, generator)
    shared = x25519_ladder(requester_scalar, yb)
    assert shared == x25519_ladder(host_scalar, ya)
    ada, adb = party(b"requester", requester), party(b"host", host)
    isk = hashlib.sha512(lv(b"CPace255_ISK", sid, shared) + lv(ya, ada) + lv(yb, adb)).digest()
    mac_key = hashlib.sha512(b"CPaceMac" + sid + isk).digest()
    ta, tb, td = (hmac.new(mac_key, message, hashlib.sha512).digest() for message in (lv(ya, ada), lv(yb, adb), lv(b"beamer-pair-done")))
    expected_token = pairing._b64(hkdf(isk, sid, b"beamer-pair-token-v3"))
    assert token == expected_token
    assert (start["share"], answer["share"]) == (pairing._b64(ya), pairing._b64(yb))
    assert (confirm["tag"], answer["tag"], done["tag"]) == (pairing._b64(ta), pairing._b64(tb), pairing._b64(td))

    return {
        "comment": comment,
        "code": code,
        "qr_secret_hex": h(qr_secret) if qr_secret else None,
        "prs_hex": h(prs),
        "wall": WALL,
        "pair_id": pair_id.hex(),
        "requester": requester,
        "host": host,
        "sid_hex": h(sid),
        "ada_hex": h(ada),
        "adb_hex": h(adb),
        "generator_string_hex": h(generator_string),
        "generator_hex": h(generator),
        "ya_hex": h(ya),
        "yb_hex": h(yb),
        "k_hex": h(shared),
        "isk_hex": h(isk),
        "mac_key_hex": h(mac_key),
        "ta_hex": h(ta),
        "tb_hex": h(tb),
        "td_hex": h(td),
        "token": token,
        "key_id_hex": h(protocol.key_id(token)),
        "beacon": beacon,
        "messages": {"pair_start": start, "pair_answer": answer, "pair_confirm": confirm, "pair_done": done},
    }


def build_wrong_prs() -> dict:
    label, code = "v3-wrong-prs", "482913"
    pair_id, qr_secret = fixed_bytes(f"{label}-pair-id", 16), fixed_bytes(f"{label}-qr-secret", 16)
    nonce = fixed_bytes(f"{label}-nonce", pairing.NONCE_BYTES)
    requester_scalar, host_scalar = fixed_bytes(f"{label}-requester-scalar"), fixed_bytes(f"{label}-host-scalar")
    requester = {**side("Vector iPhone", f"{label}-requester", "ios", 0), "nonce_hex": h(nonce), "scalar_hex": h(requester_scalar)}
    host = {**side("Vector Mac", f"{label}-host", "macos", 24820), "scalar_hex": h(host_scalar)}
    machine, _ = make_host(label, host, code, qr_secret, pair_id, host_scalar)
    client = pairing.PairingClient(
        machine.pair_id, code, requester["name"], unhex(requester["id_hex"]), scripted(nonce, requester_scalar),
        version=3, platform="ios", port=0, qr_secret=qr_secret,
    )
    sent = client.start()
    assert sent["qr"] == 1
    received = {key: value for key, value in sent.items() if key != "qr"}
    answer = machine.handle(received, "10.1.2.3")
    assert answer["ok"] is True
    try:
        client.accept(answer)
    except pairing.PairingError as exc:
        assert str(exc) == pairing.ERROR_REFUSED
    else:
        raise AssertionError("an answer made under another password was accepted")
    abort = client.abort()
    reply = machine.handle(abort, "10.1.2.3")
    assert reply == {"type": "pair_done", "pair": pair_id.hex(), "ok": False, "error": "refused"}

    sid = lv(pair_id.hex().encode("ascii"), nonce)
    prs_requester = code.encode("ascii") + qr_secret
    generator = pairing._generator(prs_requester, b"beamer-pair-v3", sid)
    ya = x25519_ladder(requester_scalar, generator)
    assert pairing._b64(ya) == sent["share"]
    yb = unhex(h(pairing._unb64(answer["share"])))
    shared = x25519_ladder(requester_scalar, yb)
    ada = party(b"requester", requester)
    adb = party(b"host", {"name": answer["name"], "id_hex": h(pairing._unb64(answer["id"])), "platform": answer["platform"], "port": answer["port"]})
    isk = hashlib.sha512(lv(b"CPace255_ISK", sid, shared) + lv(ya, ada) + lv(yb, adb)).digest()
    expected_tb = hmac.new(hashlib.sha512(b"CPaceMac" + sid + isk).digest(), lv(yb, adb), hashlib.sha512).digest()
    answer_tb = pairing._unb64(answer["tag"])
    assert expected_tb != answer_tb
    return {
        "$comment": (
            "A QR's `qr` stripped from pair_start on the path. The host, seeing none, makes its answer from the 6-byte code; the requester, which "
            "used the 22-byte password, works out another Tb, so it must refuse the answer, send the abort once, and never send a tag of its own."
        ),
        "code": code,
        "qr_secret_hex": h(qr_secret),
        "pair_id": pair_id.hex(),
        "requester": requester,
        "host": host,
        "pair_start_sent": sent,
        "pair_start_received": received,
        "prs_used_by_host_hex": h(code.encode("ascii")),
        "prs_used_by_requester_hex": h(prs_requester),
        "pair_answer": answer,
        "answer_tb_hex": h(answer_tb),
        "expected_tb_hex": h(expected_tb),
        "requester_must": "refused",
        "pair_confirm_abort": abort,
        "host_reply_to_abort": reply,
    }


def build_refusals() -> dict:
    label, code = "v3-refusals", "482913"
    pair_id, qr_secret = fixed_bytes(f"{label}-pair-id", 16), fixed_bytes(f"{label}-qr-secret", 16)
    nonce = fixed_bytes(f"{label}-nonce", pairing.NONCE_BYTES)
    requester_scalar, host_scalar = fixed_bytes(f"{label}-requester-scalar"), fixed_bytes(f"{label}-host-scalar")
    host = {**side("Vector Mac", f"{label}-host", "macos", 24820), "scalar_hex": h(host_scalar)}
    requester_id = fixed_bytes(f"{label}-requester-id", 16)
    machine, _ = make_host(label, host, code, qr_secret, pair_id, host_scalar)
    client = pairing.PairingClient(
        machine.pair_id, code, "Vector Mac 2", requester_id, scripted(nonce, requester_scalar), version=3, platform="macos", port=24820,
    )
    good = client.start()
    answer = {"type": "pair_answer", "pair": pair_id.hex(), "ok": False, "error": "refused"}
    variants = [
        ("an id of 15 bytes", requester_id[:15]),
        ("an id of 17 bytes", requester_id + b"\x09"),
        ("an id of sixteen zero bytes", bytes(16)),
        ("an empty id, as 1.4.3 sent", b""),
        ("an id of 32 bytes", fixed_bytes(f"{label}-long-id", 32)),
    ]
    cases = []
    for comment, ident in variants:
        start = {**good, "id": pairing._b64(ident)}
        fresh, _ = make_host(label, host, code, qr_secret, pair_id, host_scalar)
        assert fresh.handle(start, "10.1.2.3") == answer and fresh.code is None, comment
        cases.append({"comment": comment, "pair_start": start, "must_answer": answer})
    served, _ = make_host(label, host, code, qr_secret, pair_id, host_scalar)
    honest = served.handle(good, "10.1.2.3")
    assert honest["ok"] is True
    answer_cases = []
    for comment, ident in variants:
        reply = {**honest, "id": pairing._b64(ident)}
        fresh = pairing.PairingClient(
            pair_id.hex(), code, "Vector Mac 2", requester_id, scripted(nonce, requester_scalar), version=3, platform="macos", port=24820,
        )
        assert fresh.start() == good
        try:
            fresh.accept(reply)
        except pairing.PairingError as exc:
            assert str(exc) == pairing.ERROR_REFUSED, comment
        else:
            raise AssertionError(comment)
        answer_cases.append({"comment": comment.replace("an id", "a host id", 1) if comment.startswith("an id") else comment, "pair_answer": reply, "requester_must": "refused"})
    return {
        "$comment": (
            "pair_start messages whose id is not 16 bytes or is all zero. The host answers each with a refusal, drops its code, and stores nothing. "
            "Each case is offered to a fresh host. `answer_cases` are the same identities in an otherwise valid pair_answer to the requester whose "
            "`pair_start` is `requester_start`: it must refuse each before it sends a tag."
        ),
        "code": code,
        "qr_secret_hex": h(qr_secret),
        "pair_id": pair_id.hex(),
        "host": host,
        "cases": cases,
        "requester": {"name": "Vector Mac 2", "id_hex": h(requester_id), "platform": "macos", "port": 24820, "nonce_hex": h(nonce), "scalar_hex": h(requester_scalar)},
        "requester_start": good,
        "answer_cases": answer_cases,
    }


class _Capture:
    def __init__(self):
        self.data = b""

    def sendall(self, data: bytes) -> None:
        self.data += data


def build_tcp(exchange: dict) -> dict:
    order = (("host", "beacon"), ("requester", "pair_start"), ("host", "pair_answer"), ("requester", "pair_confirm"), ("host", "pair_done"))
    frames = []
    for who, name in order:
        message = exchange["beacon"] if name == "beacon" else exchange["messages"][name]
        sock = _Capture()
        pairing._send_frame(sock, message)
        (length,) = struct.unpack(">I", sock.data[:4])
        assert length == len(sock.data) - 4 and 0 < length <= 1024
        assert pairing.decode(sock.data[4:]) == {"beamy": 1, **message}
        frames.append({"from": who, "message": message, "bytes_hex": h(sock.data)})
    return {
        "$comment": (
            "One exchange over TCP on port 24821, in order: the host sends its beacon as soon as the connection is accepted, then the four messages. "
            "Each is a 4-byte big-endian length, 1 to 1024, then the JSON a datagram would carry with \"beamy\":1 first. `message` is the object "
            "without `beamy`. The desktops write compact JSON with every character outside ASCII escaped as \\uXXXX (the requester's name here has "
            "some); a receiver must accept either spelling, because the tags bind the decoded name and id, never these bytes."
        ),
        "exchange_comment": exchange["comment"],
        "frames": frames,
    }


def build_pairing_v3() -> dict:
    check_published_vectors()
    exchanges = [
        build_exchange_v3(
            "Desktop to desktop with the code typed; the requester's name is 48 code points outside ASCII and the BMP, 177 bytes, so its length in ADa takes two LEB128 bytes",
            "v3-pairing-1", "482913", False,
            side(LONG_NAME, "v3-pairing-1-requester", "macos", 24820), side("VECTOR-PC", "v3-pairing-1-host", "windows", 24820),
        ),
        build_exchange_v3(
            "A phone pairing by QR: port 0, qr 1, and a 22-byte password",
            "v3-pairing-2", "007045", True,
            side("Zoë’s \U0001d539Phone", "v3-pairing-2-requester", "ios", 0), side("Vector Mac", "v3-pairing-2-host", "macos", 24820),
        ),
        build_exchange_v3(
            "Desktop to desktop, typed, a code with leading zeros and a platform of the other family",
            "v3-pairing-3", "000042", False,
            side("Vector Linux", "v3-pairing-3-requester", "linux", 24820), side("Vector Mac", "v3-pairing-3-host", "macos", 24820),
        ),
    ]
    return {
        "$comment": (
            "Pairing version 3 (WIRE.md section 6). Hex is bytes in wire order. Every exchange shows every intermediate value and all four "
            "messages. The host of a typed exchange still draws a QR secret when it shows a code, which nothing uses, so its `qr_secret_hex` is null. "
            "`beacon` is the host's beacon while the code is up."
        ),
        "constants": {
            "pairing_version": pairing.PAIRING_V3,
            "pair_start_v": pairing.PAIRING_V3,
            "dsi_ascii": "CPace255",
            "channel_ascii": pairing.CHANNEL_V3.decode("ascii"),
            "token_info_ascii": pairing.TOKEN_INFO_V3.decode("ascii"),
            "done_label_ascii": pairing.DONE_LABEL.decode("ascii"),
            "role_requester_ascii": pairing.ROLE_REQUESTER.decode("ascii"),
            "role_host_ascii": pairing.ROLE_HOST.decode("ascii"),
            "id_bytes": pairing.ID_BYTES,
            "qr_secret_bytes": pairing.QR_SECRET_BYTES,
            "max_peers": pairing.MAX_PEERS,
            "nonce_bytes": pairing.NONCE_BYTES,
            "max_name_chars": pairing.MAX_NAME_CHARS,
            "tcp_port": pairing.PAIRING_PORT,
            "link_key_id_info_ascii": protocol.KEY_ID_INFO.decode("ascii"),
        },
        "exchanges": exchanges,
        "wrong_prs": build_wrong_prs(),
        "tcp": build_tcp(exchanges[1]),
        "refusals": build_refusals(),
    }


# ----------------------------------------------------------------------------------------------

def build(old: dict) -> dict:
    """The whole file: the old sections as `old` has them, then the two new ones."""
    vectors = {
        "$comment": (
            "Generated by beamer/tools/phone_vectors.py. The protocol_version, preamble, derive_key, frames, pairing and messages sections "
            "(wire version 5 and pairing version 2) were written by 1.4.x's code and are carried over untouched; v6 and pairing_v3 are made from "
            "core/protocol.py and core/pairing.py. Every value is fixed, not random -- rerunning this script must reproduce this file byte for "
            "byte until the wire format changes."
        ),
    }
    for name in OLD_SECTIONS:
        vectors[name] = old[name]
    vectors["v6"] = build_v6()
    vectors["pairing_v3"] = build_pairing_v3()
    return vectors


def render(vectors: dict) -> str:
    return json.dumps(vectors, indent=2, sort_keys=False) + "\n"


def main() -> None:
    out_path = Path(__file__).resolve().parent / "phone_vectors.json"
    vectors = build(json.loads(out_path.read_text(encoding="utf-8")))
    out_path.write_text(render(vectors), encoding="utf-8")
    print(f"wrote {out_path}")
    # The sections beside the tests that replay them: this folder is not part of the public
    # repository, and the test suites and PAIRING.md there need them.
    tests = REPO / "core" / "tests"
    (tests / "pairing_vectors.json").write_text(render(vectors["pairing"]), encoding="utf-8")
    print(f"wrote {tests / 'pairing_vectors.json'}")
    (tests / "v6_vectors.json").write_text(render({"v6": vectors["v6"], "pairing_v3": vectors["pairing_v3"]}), encoding="utf-8")
    print(f"wrote {tests / 'v6_vectors.json'}")


if __name__ == "__main__":
    main()
