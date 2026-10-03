"""The version 6 and pairing version 3 vectors (WIRE.md, The vectors).

`core/tests/v6_vectors.json` holds the `v6` and `pairing_v3` sections of the vector file the phone
apps are built against, copied beside the tests, which run without it. Every replay test reads
that copy and checks the shipped code against it, byte for byte, and again against HKDF, X25519
and ChaCha20-Poly1305 written here without protocol.py in the way. The last class needs that file
and its generator, which are not part of the public source: it regenerates the whole file and
compares it, and holds the version 5 and pairing version 2 sections to the bytes they had when
version 6 was added.
"""

import hashlib
import hmac
import importlib.util
import json
import os
import re
import struct
import unittest

from nacl.bindings import (
    crypto_aead_chacha20poly1305_ietf_decrypt,
    crypto_scalarmult,
    crypto_scalarmult_base,
)
from nacl.exceptions import CryptoError

from core import keytable, pairing, protocol

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_TESTS_DIR))
_TOOLS = os.path.join(_REPO_ROOT, "tools")

with open(os.path.join(_TESTS_DIR, "v6_vectors.json"), encoding="utf-8") as _handle:
    FILE = json.load(_handle)
V6 = FILE["v6"]
P3 = FILE["pairing_v3"]

INITIATOR_TO_RESPONDER = "initiator_to_responder"
RESPONDER_TO_INITIATOR = "responder_to_initiator"


def hkdf(ikm, salt, info):
    """RFC 5869 for one 32-byte block, written here rather than borrowed from the code under test."""
    return hmac.new(hmac.new(salt, ikm, hashlib.sha256).digest(), info + b"\x01", hashlib.sha256).digest()


def unhex(text):
    return bytes.fromhex(text)


def sessions(link):
    """(initiator, responder) for one scripted link, preambles exchanged."""
    initiator = protocol.LinkSession(link["token"], protocol.ROLE_INITIATOR, unhex(link["initiator"]["prefix_hex"]), unhex(link["initiator"]["secret_hex"]))
    responder = protocol.LinkSession(link["token"], protocol.ROLE_RESPONDER, unhex(link["responder"]["prefix_hex"]), unhex(link["responder"]["secret_hex"]))
    responder.accept_preamble(initiator.preamble())
    initiator.accept_preamble(responder.preamble())
    return initiator, responder


class ConstantsAndKeyIdTests(unittest.TestCase):
    def test_the_constants_are_the_modules(self):
        constants = V6["constants"]
        self.assertEqual(constants["version"], protocol.LINK_VERSION)
        self.assertEqual(constants["salt_ascii"].encode("ascii"), protocol.LINK_SALT)
        self.assertEqual(constants["key_id_info_ascii"].encode("ascii"), protocol.KEY_ID_INFO)
        self.assertEqual(constants["frame_info_label_ascii"].encode("ascii"), protocol.KEY_INFO)
        self.assertEqual(constants["key_id_bytes"], protocol.KEY_ID_SIZE)
        self.assertEqual(constants["preamble_bytes"], protocol.LINK_PREAMBLE_SIZE)
        self.assertEqual(constants["short_reply_bytes"], protocol.SHORT_REPLY_SIZE)
        self.assertEqual(constants["min_frame_bytes"], protocol.MIN_FRAME_BYTES)
        self.assertEqual(constants["first_frame_max_bytes"], protocol.HELLO_MAX_BYTES)

    def test_every_token_has_the_committed_key_id_by_the_module_and_by_hand(self):
        self.assertGreaterEqual(len(V6["key_id"]["cases"]), 3)
        for case in V6["key_id"]["cases"]:
            with self.subTest(token=case["token"]):
                self.assertTrue(protocol.is_paired_token(case["token"]))
                self.assertEqual(protocol.key_id(case["token"]).hex(), case["key_id_hex"])
                self.assertEqual(hkdf(case["token"].encode("ascii"), b"beamer-link-v6", b"beamer-key-id")[:16].hex(), case["key_id_hex"])

    def test_a_typed_token_has_no_key_id(self):
        self.assertGreaterEqual(len(V6["key_id"]["typed_tokens"]), 2)
        for token in V6["key_id"]["typed_tokens"]:
            with self.subTest(token=token):
                self.assertFalse(protocol.is_paired_token(token))
                with self.assertRaises(ValueError):
                    protocol.key_id(token)


class PreambleTests(unittest.TestCase):
    def test_the_preamble_is_the_committed_62_bytes(self):
        vector = V6["preamble"]
        session = protocol.LinkSession(vector["token"], protocol.ROLE_INITIATOR, unhex(vector["prefix_hex"]), unhex(vector["secret_hex"]))
        self.assertEqual(session.key_id.hex(), vector["key_id_hex"])
        self.assertEqual(session.share.hex(), vector["share_hex"])
        self.assertEqual(session.preamble().hex(), vector["preamble_hex"])
        self.assertEqual(len(unhex(vector["preamble_hex"])), 62)

    def test_the_share_is_x25519_of_the_secret_and_the_base_point(self):
        vector = V6["preamble"]
        self.assertEqual(crypto_scalarmult_base(unhex(vector["secret_hex"])).hex(), vector["share_hex"])
        self.assertEqual(crypto_scalarmult(unhex(vector["secret_hex"]), bytes([9]) + bytes(31)).hex(), vector["share_hex"])

    def test_the_layout_is_magic_version_prefix_key_id_share(self):
        vector = V6["preamble"]
        data = unhex(vector["preamble_hex"])
        self.assertEqual(data[:5], b"BEAMY")
        self.assertEqual(data[5], 6)
        self.assertEqual(data[6:14].hex(), vector["prefix_hex"])
        self.assertEqual(data[14:30].hex(), vector["key_id_hex"])
        self.assertEqual(data[30:].hex(), vector["share_hex"])
        self.assertEqual(tuple(part.hex() for part in protocol.split_preamble(data)), (vector["prefix_hex"], vector["key_id_hex"], vector["share_hex"]))

    def test_the_short_reply_is_14_bytes(self):
        vector = V6["short_reply"]
        reply = protocol.short_reply(unhex(vector["prefix_hex"]))
        self.assertEqual(reply.hex(), vector["reply_hex"])
        self.assertEqual(len(reply), 14)
        self.assertEqual(unhex(vector["reply_hex"])[:6], b"BEAMY\x06")


class DeriveKeyTests(unittest.TestCase):
    def test_there_is_a_second_case_with_the_roles_swapped(self):
        first, second = V6["derive_key"]["cases"]
        self.assertEqual((first["prefix_i_hex"], first["secret_i_hex"]), (second["prefix_r_hex"], second["secret_r_hex"]))
        self.assertEqual((first["prefix_r_hex"], first["secret_r_hex"]), (second["prefix_i_hex"], second["secret_i_hex"]))
        self.assertEqual(first["shared_hex"], second["shared_hex"])
        self.assertNotEqual(first["key_i_hex"], second["key_i_hex"])
        self.assertNotEqual(first["key_r_hex"], second["key_r_hex"])

    def test_every_value_by_the_module(self):
        for case in V6["derive_key"]["cases"]:
            with self.subTest(case=case["comment"]):
                token, key_id = case["token"], unhex(case["key_id_hex"])
                self.assertEqual(protocol.key_id(token), key_id)
                parts = [unhex(case[name]) for name in ("prefix_i_hex", "prefix_r_hex", "share_i_hex", "share_r_hex")]
                self.assertEqual(crypto_scalarmult_base(unhex(case["secret_i_hex"])).hex(), case["share_i_hex"])
                self.assertEqual(crypto_scalarmult_base(unhex(case["secret_r_hex"])).hex(), case["share_r_hex"])
                self.assertEqual(protocol.link_ikm(token, unhex(case["shared_hex"])).hex(), case["ikm_hex"])
                self.assertEqual(len(unhex(case["ikm_hex"])), 75)
                for direction, info, key in ((1, "info_i_hex", "key_i_hex"), (2, "info_r_hex", "key_r_hex")):
                    self.assertEqual(protocol.link_info(key_id, *parts, direction).hex(), case[info])
                    self.assertEqual(len(unhex(case[info])), 111)
                    self.assertEqual(protocol.link_key(token, unhex(case["shared_hex"]), key_id, *parts, direction).hex(), case[key])

    def test_every_value_by_hand(self):
        for case in V6["derive_key"]["cases"]:
            with self.subTest(case=case["comment"]):
                shared_i = crypto_scalarmult(unhex(case["secret_i_hex"]), unhex(case["share_r_hex"]))
                shared_r = crypto_scalarmult(unhex(case["secret_r_hex"]), unhex(case["share_i_hex"]))
                self.assertEqual(shared_i, shared_r)
                self.assertEqual(shared_i.hex(), case["shared_hex"])
                ikm = case["token"].encode("ascii") + shared_i
                self.assertEqual(ikm.hex(), case["ikm_hex"])
                for direction, info, key in ((b"\x01", "info_i_hex", "key_i_hex"), (b"\x02", "info_r_hex", "key_r_hex")):
                    expected = (
                        b"beamer-frames" + b"\x06" + unhex(case["key_id_hex"])
                        + unhex(case["prefix_i_hex"]) + unhex(case["prefix_r_hex"])
                        + unhex(case["share_i_hex"]) + unhex(case["share_r_hex"]) + direction
                    )
                    self.assertEqual(expected.hex(), case[info])
                    self.assertEqual(hkdf(ikm, b"beamer-link-v6", expected).hex(), case[key])

    def test_a_handshake_of_the_sessions_makes_the_committed_keys(self):
        for case in V6["derive_key"]["cases"]:
            with self.subTest(case=case["comment"]):
                link = {
                    "token": case["token"],
                    "initiator": {"prefix_hex": case["prefix_i_hex"], "secret_hex": case["secret_i_hex"]},
                    "responder": {"prefix_hex": case["prefix_r_hex"], "secret_hex": case["secret_r_hex"]},
                }
                initiator, responder = sessions(link)
                self.assertEqual(initiator._send_key.hex(), case["key_i_hex"])
                self.assertEqual(responder._recv_key.hex(), case["key_i_hex"])
                self.assertEqual(responder._send_key.hex(), case["key_r_hex"])
                self.assertEqual(initiator._recv_key.hex(), case["key_r_hex"])


class LowOrderTests(unittest.TestCase):
    def test_every_share_ends_the_handshake(self):
        vector = V6["low_order"]
        secret = unhex(vector["secret_hex"])
        self.assertGreaterEqual(len(vector["shares"]), 14)
        for case in vector["shares"]:
            with self.subTest(share=case["share_hex"]):
                self.assertEqual(case["result_hex"], "00" * 32)
                with self.assertRaises(protocol.PreambleRefused):
                    protocol._x25519_shared(secret, unhex(case["share_hex"]))
                token = V6["preamble"]["token"]
                key_id = protocol.key_id(token)
                preamble = b"BEAMY\x06" + bytes(range(1, 9)) + key_id + unhex(case["share_hex"])
                for role in (protocol.ROLE_INITIATOR, protocol.ROLE_RESPONDER):
                    session = protocol.LinkSession(token, role, bytes(range(11, 19)), secret)
                    with self.assertRaises(protocol.PreambleRefused):
                        session.accept_preamble(preamble)

    def test_the_non_canonical_shares_are_there(self):
        shares = {case["share_hex"] for case in V6["low_order"]["shares"]}
        p = 2**255 - 19
        for value in (p, p + 1):
            low = value.to_bytes(32, "little")
            high = bytes(low[:31]) + bytes([low[31] | 0x80])
            self.assertIn(low.hex(), shares)
            self.assertIn(high.hex(), shares)
        self.assertIn("00" * 32, shares)
        self.assertIn("01" + "00" * 31, shares)

    def test_an_ordinary_share_is_not_refused(self):
        share = unhex(V6["preamble"]["share_hex"])
        self.assertNotEqual(protocol._x25519_shared(unhex(V6["low_order"]["secret_hex"]), share), bytes(32))


class FrameTests(unittest.TestCase):
    def test_the_script_covers_every_message_the_spec_lists(self):
        seen = set()
        for link in V6["frames"]["links"]:
            for frame in link["frames"]:
                seen.add(frame["message"]["type"])
        self.assertTrue(
            {"hello", "welcome", "paired", "arrangement", "focus", "clipboard", "mousemove", "keydown", "keyup",
             "text", "mousedown", "mouseup", "scroll", "gesture", "ping", "accepts", "ack", "accept", "switch", "refuse"} <= seen,
            seen,
        )

    def test_the_scripted_links_seal_and_open_to_the_committed_bytes(self):
        for link in V6["frames"]["links"]:
            with self.subTest(link=link["name"]):
                initiator, responder = sessions(link)
                self.assertEqual(initiator.preamble().hex(), link["preamble_i_hex"])
                self.assertEqual(responder.preamble().hex(), link["preamble_r_hex"])
                counters = {INITIATOR_TO_RESPONDER: 0, RESPONDER_TO_INITIATOR: 0}
                for frame in link["frames"]:
                    sender, receiver = (initiator, responder) if frame["direction"] == INITIATOR_TO_RESPONDER else (responder, initiator)
                    counters[frame["direction"]] += 1
                    self.assertEqual(frame["counter"], counters[frame["direction"]])
                    self.assertEqual(protocol.dumps_message(frame["message"]).decode("utf-8"), frame["plaintext_json"])
                    sealed = sender.seal(frame["message"])
                    self.assertEqual(sealed.hex(), frame["frame_hex"])
                    opened = receiver.open(sealed[protocol.HEADER_SIZE:])
                    self.assertEqual(dict(opened), frame["message"])
                    self.assertFalse(opened.malformed)

    def test_libsodium_by_hand_opens_every_committed_frame(self):
        for link in V6["frames"]["links"]:
            with self.subTest(link=link["name"]):
                token = link["token"]
                key_id = protocol.key_id(token)
                pre_i, pre_r = unhex(link["preamble_i_hex"]), unhex(link["preamble_r_hex"])
                prefix_i, prefix_r = pre_i[6:14], pre_r[6:14]
                share_i, share_r = pre_i[30:], pre_r[30:]
                shared = crypto_scalarmult(unhex(link["initiator"]["secret_hex"]), share_r)
                ikm = token.encode("ascii") + shared
                keys = {}
                for direction, byte in ((INITIATOR_TO_RESPONDER, b"\x01"), (RESPONDER_TO_INITIATOR, b"\x02")):
                    info = b"beamer-frames\x06" + key_id + prefix_i + prefix_r + share_i + share_r + byte
                    keys[direction] = hkdf(ikm, b"beamer-link-v6", info)
                prefixes = {INITIATOR_TO_RESPONDER: prefix_i, RESPONDER_TO_INITIATOR: prefix_r}
                for frame in link["frames"]:
                    data = unhex(frame["frame_hex"])
                    (length,) = struct.unpack(">I", data[:4])
                    self.assertEqual(length, len(data) - 4)
                    counter = data[4:8]
                    self.assertEqual(struct.unpack(">I", counter)[0], frame["counter"])
                    plain = crypto_aead_chacha20poly1305_ietf_decrypt(data[8:], None, prefixes[frame["direction"]] + counter, keys[frame["direction"]])
                    self.assertEqual(plain.decode("utf-8"), frame["plaintext_json"])

    def test_the_text_frame_carries_a_character_outside_the_bmp(self):
        texts = [f["message"]["data"]["text"] for link in V6["frames"]["links"] for f in link["frames"] if f["message"]["type"] == "text"]
        self.assertTrue(any(ord(char) > 0xFFFF for text in texts for char in text))

    def test_the_responder_first_frames_read_as_the_spec_says(self):
        links = {link["name"]: link for link in V6["frames"]["links"]}
        for name, error in (("invalid_hello", protocol.ERROR_INVALID_HELLO), ("wrong_id", protocol.ERROR_WRONG_ID)):
            with self.subTest(link=name):
                link = links[name]
                initiator, responder = sessions(link)
                first = link["frames"][0]
                self.assertEqual(first["direction"], INITIATOR_TO_RESPONDER)
                received = responder.open(unhex(first["frame_hex"])[protocol.HEADER_SIZE:])
                if name == "invalid_hello":
                    with self.assertRaises(protocol.InvalidHello):
                        protocol.read_hello(received)
                else:
                    self.assertEqual(protocol.read_hello(received)["id"].hex(), link["wrong_id_hello_id_hex"])
                answer = link["frames"][1]
                self.assertEqual(answer["direction"], RESPONDER_TO_INITIATOR)
                self.assertEqual(answer["message"], {"type": "welcome", "data": {"version": 6, "error": error}})
                with self.assertRaises(protocol.HelloRefused) as raised:
                    protocol.read_welcome(initiator.open(unhex(answer["frame_hex"])[protocol.HEADER_SIZE:]))
                self.assertEqual(raised.exception.error, error)
                self.assertEqual(len(link["frames"]), 2)

    def test_the_main_link_messages_pass_their_validators(self):
        link = next(link for link in V6["frames"]["links"] if link["name"] == "main")
        responder_id = unhex(link["responder"]["id_hex"])
        readers = {
            "hello": protocol.read_hello,
            "welcome": protocol.read_welcome,
            "paired": protocol.read_paired,
            "accept": protocol.read_accept,
            "switch": protocol.read_switch,
            "ack": protocol.read_ack,
            "clipboard": protocol.read_clipboard,
            "keydown": protocol.read_input,
            "keyup": protocol.read_input,
            "mousemove": protocol.read_input,
            "mousedown": protocol.read_input,
            "mouseup": protocol.read_input,
            "scroll": protocol.read_input,
            "gesture": protocol.read_input,
            "text": protocol.read_input,
            "ping": protocol.read_message,
            "accepts": protocol.read_accepts,
            "focus": lambda message: protocol.read_focus(message, responder_id),
            "arrangement": lambda message: protocol.read_arrangement_v6(message, now=1_790_000_000),
        }
        for frame in link["frames"]:
            kind = frame["message"]["type"]
            with self.subTest(type=kind, counter=frame["counter"], direction=frame["direction"]):
                message = protocol.parse_message(frame["plaintext_json"].encode("utf-8"))
                result = readers[kind](message)
                self.assertIsNotNone(result)


class BadFrameTests(unittest.TestCase):
    def test_each_bad_frame_ends_the_link(self):
        bad = V6["bad_frames"]
        self.assertGreaterEqual(len(bad["cases"]), 3)
        for case in bad["cases"]:
            with self.subTest(case=case["comment"]):
                initiator, responder = sessions(bad["link"])
                receiver = responder if case["receiver"] == "responder" else initiator
                self.assertEqual(case["must"], "close_without_reply")
                with self.assertRaises(protocol.ProtocolError):
                    receiver.open(unhex(case["frame_hex"])[protocol.HEADER_SIZE:])

    def test_the_three_kinds_the_spec_lists_are_there(self):
        kinds = " ".join(case["comment"] for case in V6["bad_frames"]["cases"]).lower()
        for word in ("tag", "counter", "direction"):
            self.assertIn(word, kinds)

    def test_the_untouched_frame_the_cases_are_made_from_opens(self):
        bad = V6["bad_frames"]
        initiator, responder = sessions(bad["link"])
        self.assertEqual(dict(responder.open(unhex(bad["good_frame_hex"])[protocol.HEADER_SIZE:])), bad["good_message"])


class IdTests(unittest.TestCase):
    def test_ids_spell_one_way(self):
        for case in V6["ids"]["cases"]:
            with self.subTest(b64=case["b64"]):
                self.assertEqual(len(case["b64"]), 22)
                self.assertEqual(protocol.id_text(unhex(case["id_hex"])), case["b64"])
                self.assertEqual(protocol.read_id(case["b64"]), unhex(case["id_hex"]))

    def test_the_refused_texts_are_refused(self):
        refused = V6["ids"]["refused"]
        self.assertGreaterEqual(len([case for case in refused if case["kind"] == "non_canonical"]), 3)
        for case in refused:
            with self.subTest(kind=case["kind"], text=case["text"]):
                self.assertIsNone(protocol.read_id(case["text"]))

    def test_a_non_canonical_spelling_decodes_to_the_same_bytes_in_a_lenient_decoder(self):
        import base64

        for case in V6["ids"]["refused"]:
            if case["kind"] != "non_canonical":
                continue
            with self.subTest(text=case["text"]):
                text = case["text"]
                self.assertEqual(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).hex(), case["id_hex"])


class MalformedTests(unittest.TestCase):
    def action(self, hex_text, reader):
        """What the shipped code does with a body: end_link, ignore, answer_invalid_hello or accepted."""
        try:
            message = protocol.parse_message(unhex(hex_text))
        except protocol.NotAnObject:
            return "answer_invalid_hello" if reader == "hello" else "end_link"
        if reader == "hello":
            try:
                protocol.read_hello(message)
            except protocol.InvalidHello:
                return "answer_invalid_hello"
            return "accepted"
        if reader in ("welcome", "ack"):
            try:
                {"welcome": protocol.read_welcome, "ack": protocol.read_ack}[reader](message)
            except protocol.ProtocolError:
                return "end_link"
            return "accepted"
        if message.malformed:
            return "ignore"
        result = {
            "message": protocol.read_message,
            "input": protocol.read_input,
            "accept": protocol.read_accept,
            "refuse": protocol.read_refuse,
            "switch": protocol.read_switch,
            "focus": lambda m: protocol.read_focus(m, bytes(range(1, 17))),
        }[reader](message)
        return "ignore" if result is None else "accepted"

    def test_every_case_is_what_the_desktops_do(self):
        cases = V6["malformed"]["cases"]
        self.assertGreaterEqual(len(cases), 40)
        for case in cases:
            with self.subTest(case=case["comment"]):
                self.assertIn(case["action"], ("end_link", "ignore", "answer_invalid_hello"))
                self.assertEqual(self.action(case["hex"], case["reader"]), case["action"])
                try:
                    decoded = unhex(case["hex"]).decode("utf-8")
                except UnicodeDecodeError:
                    decoded = None
                self.assertEqual(case["text"], decoded)

    def test_a_text_a_parser_may_refuse_outright_says_so(self):
        flagged = [case for case in V6["malformed"]["cases"] if "also_acceptable" in case]
        self.assertEqual(len(flagged), 4)
        for case in flagged:
            with self.subTest(case=case["comment"]):
                self.assertEqual(case["also_acceptable"], "end_link")
                self.assertEqual(case["action"], "ignore")
                self.assertTrue(protocol.parse_message(unhex(case["hex"])).malformed)

    def test_implementation_defined_cases_are_what_the_desktops_do(self):
        for case in V6["malformed"]["implementation_defined"]:
            with self.subTest(case=case["comment"]):
                self.assertTrue(protocol.parse_message(unhex(case["hex"])).malformed)
                self.assertEqual(self.action(case["hex"], case["reader"]), case["desktop_action"])

    def test_the_shapes_the_spec_names_are_there(self):
        comments = " ".join(case["comment"] for case in V6["malformed"]["cases"]).lower()
        for word in ("1.0", "boolean", "nan", "byte order mark", "hw", "app", "caps", "platform", "name", "welcome", "ack", "accept", "refuse", "switch"):
            self.assertIn(word, comments)

    def test_a_well_formed_hello_is_accepted_so_the_refusals_mean_something(self):
        good = V6["malformed"]["well_formed_hello"]
        self.assertEqual(self.action(good["hex"], "hello"), "accepted")


class QrTests(unittest.TestCase):
    def test_good_texts_read_to_their_fields(self):
        self.assertGreaterEqual(len(V6["qr"]["good"]), 2)
        for case in V6["qr"]["good"]:
            with self.subTest(text=case["text"]):
                read = pairing.read_qr(case["text"])
                self.assertEqual((read["host"], read["port"], read["code"], read["secret"].hex()), (case["host"], case["port"], case["code"], case["secret_hex"]))
                self.assertEqual(pairing.qr_text(case["host"], case["port"], case["code"], unhex(case["secret_hex"])), case["text"])
                self.assertEqual(len(case["text"].split("k=")[1]), 22)

    def test_bad_texts_are_refused(self):
        self.assertGreaterEqual(len(V6["qr"]["bad"]), 12)
        for case in V6["qr"]["bad"]:
            with self.subTest(case=case["comment"]):
                self.assertEqual(case["error"], "bad_qr")
                with self.assertRaises(pairing.PairingError) as raised:
                    pairing.read_qr(case["text"])
                self.assertEqual(str(raised.exception), "bad_qr")


def _wire_md_rows():
    with open(os.path.join(_REPO_ROOT, "WIRE.md"), encoding="utf-8") as handle:
        text = handle.read()
    section = text.split("### Which physical key sends which name", 1)[1].split("\n- Semantic", 1)[0]
    rows = {}
    for line in section.splitlines():
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0] not in ("Physical key on the sender", "---"):
            rows[cells[0]] = tuple(cells[1:])
    return rows


class KeyTableTests(unittest.TestCase):
    def test_every_row_is_what_keytable_gives(self):
        platforms = {"mac": ("macos", "windows"), "pc": ("windows", "macos")}
        for row in V6["key_table"]["rows"]:
            with self.subTest(key=row["key"]):
                own, peer = platforms[row["sender"]]
                self.assertEqual(keytable.wire_name(row["physical"], own, own, "semantic"), row["same_family"])
                self.assertEqual(keytable.wire_name(row["physical"], own, peer, "semantic"), row["across_semantic"])
                self.assertEqual(keytable.wire_name(row["physical"], own, peer, "positional"), row["across_positional"])
                if row["sender"] == "pc":
                    self.assertEqual(keytable.wire_name(row["physical"], own, peer, "mac_layout"), row["across_mac_layout"])

    def test_the_rows_are_section_7s_table(self):
        table = _wire_md_rows()
        self.assertEqual(len(table), 12)
        self.assertEqual({row["key"] for row in V6["key_table"]["rows"]}, set(table))
        for row in V6["key_table"]["rows"]:
            with self.subTest(key=row["key"]):
                self.assertEqual(
                    (row["same_family"], row["across_semantic"], row["across_positional"], row["across_mac_layout"]),
                    table[row["key"]],
                )

    def test_the_families_names_and_evdev_codes(self):
        data = V6["key_table"]
        self.assertEqual(set(data["mac_platforms"]), keytable.MAC_PLATFORMS)
        self.assertEqual(data["names"], sorted(keytable.SPEC_NAMES))
        self.assertEqual({int(code): name for code, name in data["evdev"].items()}, keytable.EVDEV_NAMES)
        self.assertEqual(data["unknown_platform_is"], "pc")


class AckTests(unittest.TestCase):
    def test_every_case_is_the_formula_and_the_module_s_message(self):
        cases = V6["ack"]["cases"]
        self.assertTrue(any(case["sample_us"] == 0 and case["arrived_at_us"] - case["sent_at_us"] < case["held_us_on_wire"] for case in cases))
        self.assertTrue(any(case["held_us_asked"] != case["held_us_on_wire"] for case in cases))
        for case in cases:
            with self.subTest(case=case["comment"]):
                message = protocol.ack_v6(case["seq"], case["held_us_asked"])
                self.assertEqual(message, case["message"])
                self.assertEqual(message["data"]["held_us"], case["held_us_on_wire"])
                self.assertEqual(protocol.read_ack(protocol.parse_message(protocol.dumps_message(message)))["held_us"], case["held_us_on_wire"])
                self.assertEqual(max(0, case["arrived_at_us"] - case["sent_at_us"] - case["held_us_on_wire"]), case["sample_us"])


def lv_cat(*parts):
    """The CPace draft's lv_cat with its own LEB128, not pairing.py's."""
    out = b""
    for part in parts:
        length, prefix = len(part), b""
        while True:
            prefix += bytes([(length & 0x7F) | (0x80 if length > 0x7F else 0)])
            length >>= 7
            if not length:
                break
        out += prefix + part
    return out


def _scripted(*values):
    queue = list(values)
    return lambda length: queue.pop(0)


class PairingV3ConstantsTests(unittest.TestCase):
    def test_the_constants_are_the_modules(self):
        constants = P3["constants"]
        self.assertEqual(constants["pairing_version"], pairing.PAIRING_V3)
        self.assertEqual(constants["pair_start_v"], pairing.PAIRING_V3)
        self.assertEqual(constants["channel_ascii"].encode("ascii"), pairing.CHANNEL_V3)
        self.assertEqual(constants["token_info_ascii"].encode("ascii"), pairing.TOKEN_INFO_V3)
        self.assertEqual(constants["done_label_ascii"].encode("ascii"), pairing.DONE_LABEL)
        self.assertEqual(constants["role_requester_ascii"].encode("ascii"), pairing.ROLE_REQUESTER)
        self.assertEqual(constants["role_host_ascii"].encode("ascii"), pairing.ROLE_HOST)
        self.assertEqual(constants["id_bytes"], pairing.ID_BYTES)
        self.assertEqual(constants["qr_secret_bytes"], pairing.QR_SECRET_BYTES)
        self.assertEqual(constants["max_peers"], pairing.MAX_PEERS)
        self.assertEqual(constants["nonce_bytes"], pairing.NONCE_BYTES)
        self.assertEqual(constants["max_name_chars"], pairing.MAX_NAME_CHARS)
        self.assertEqual(constants["tcp_port"], pairing.PAIRING_PORT)
        self.assertEqual(constants["link_key_id_info_ascii"], "beamer-key-id")


class PairingV3ExchangeTests(unittest.TestCase):
    def test_the_three_exchanges_are_the_ones_the_spec_lists(self):
        exchanges = P3["exchanges"]
        self.assertEqual(len(exchanges), 3)
        self.assertTrue(any(any(ord(char) > 0xFFFF for char in ex["requester"]["name"]) and any(ord(char) > 0x7F for char in ex["requester"]["name"]) for ex in exchanges))
        phone = [ex for ex in exchanges if ex["requester"]["port"] == 0]
        self.assertEqual(len(phone), 1)
        self.assertEqual(phone[0]["messages"]["pair_start"]["qr"], 1)
        self.assertEqual(len(unhex(phone[0]["prs_hex"])), 22)
        self.assertEqual(len([ex for ex in exchanges if "qr" not in ex["messages"]["pair_start"]]), 2)
        for ex in exchanges:
            self.assertEqual(len(unhex(ex["requester"]["id_hex"])), 16)
            self.assertEqual(len(unhex(ex["host"]["id_hex"])), 16)

    def test_one_exchange_needs_a_two_byte_length_in_its_associated_data(self):
        long_names = [ex for ex in P3["exchanges"] if len(ex["requester"]["name"].encode("utf-8")) >= 128]
        self.assertEqual(len(long_names), 1)
        self.assertEqual(len(long_names[0]["requester"]["name"]), 48)
        self.assertGreaterEqual(len(unhex(long_names[0]["ada_hex"])), 128)

    def test_the_module_reproduces_every_exchange(self):
        for ex in P3["exchanges"]:
            with self.subTest(comment=ex["comment"]):
                stored = []
                qr_secret = unhex(ex["qr_secret_hex"]) if ex["qr_secret_hex"] else None
                host = pairing.PairingHost(
                    name=ex["host"]["name"], identity=unhex(ex["host"]["id_hex"]), version=3, platform=ex["host"]["platform"], port=ex["host"]["port"],
                    entropy=_scripted(unhex(ex["pair_id"]), unhex(ex["qr_secret_hex"]) if qr_secret is not None else bytes(16), unhex(ex["host"]["scalar_hex"])),
                    peers=list, store=lambda entry, replaced: stored.append(entry), wall=lambda: ex["wall"],
                )
                host.begin()
                host.code = ex["code"]
                self.assertEqual(host.beacon(ex["host"]["port"]), ex["beacon"])
                client = pairing.PairingClient(
                    host.pair_id, ex["code"], ex["requester"]["name"], unhex(ex["requester"]["id_hex"]),
                    _scripted(unhex(ex["requester"]["nonce_hex"]), unhex(ex["requester"]["scalar_hex"])),
                    version=3, platform=ex["requester"]["platform"], port=ex["requester"]["port"], qr_secret=qr_secret, wall=lambda: ex["wall"],
                )
                start = client.start()
                self.assertEqual(start, ex["messages"]["pair_start"])
                answer = host.handle(start, "10.1.2.3")
                self.assertEqual(answer, ex["messages"]["pair_answer"])
                confirm = client.accept(answer)
                self.assertEqual(confirm, ex["messages"]["pair_confirm"])
                done = host.handle(confirm, "10.1.2.3")
                self.assertEqual(done, ex["messages"]["pair_done"])
                self.assertEqual(client.finish(done), ex["token"])
                self.assertEqual(len(stored), 1)
                self.assertEqual(protocol.key_id(ex["token"]).hex(), ex["key_id_hex"])

    def test_the_formulae_by_hand(self):
        for ex in P3["exchanges"]:
            with self.subTest(comment=ex["comment"]):
                lv = lv_cat
                requester, host = ex["requester"], ex["host"]
                ada = lv(b"requester", unhex(requester["id_hex"]), requester["name"].encode("utf-8"), requester["platform"].encode("ascii"), requester["port"].to_bytes(2, "big"))
                adb = lv(b"host", unhex(host["id_hex"]), host["name"].encode("utf-8"), host["platform"].encode("ascii"), host["port"].to_bytes(2, "big"))
                self.assertEqual(ada.hex(), ex["ada_hex"])
                self.assertEqual(adb.hex(), ex["adb_hex"])
                prs = ex["code"].encode("ascii") + (unhex(ex["qr_secret_hex"]) if ex["qr_secret_hex"] else b"")
                self.assertEqual(prs.hex(), ex["prs_hex"])
                self.assertEqual(len(prs), 22 if ex["qr_secret_hex"] else 6)
                sid = lv(ex["pair_id"].encode("ascii"), unhex(requester["nonce_hex"]))
                self.assertEqual(sid.hex(), ex["sid_hex"])
                padding = bytes(127 - len(pairing._prepend_len(prs)) - len(pairing._prepend_len(b"CPace255")))
                self.assertEqual(len(padding), 95 if ex["qr_secret_hex"] else 111)
                generator_string = lv(b"CPace255", prs, padding, b"beamer-pair-v3", sid)
                self.assertEqual(generator_string.hex(), ex["generator_string_hex"])
                digest = hashlib.sha512(generator_string).digest()[:32]
                generator = pairing._elligator2(int.from_bytes(digest, "little") & ((1 << 255) - 1)).to_bytes(32, "little")
                self.assertEqual(generator.hex(), ex["generator_hex"])
                ya = crypto_scalarmult(unhex(requester["scalar_hex"]), generator)
                yb = crypto_scalarmult(unhex(host["scalar_hex"]), generator)
                self.assertEqual((ya.hex(), yb.hex()), (ex["ya_hex"], ex["yb_hex"]))
                shared = crypto_scalarmult(unhex(requester["scalar_hex"]), yb)
                self.assertEqual(shared, crypto_scalarmult(unhex(host["scalar_hex"]), ya))
                self.assertEqual(shared.hex(), ex["k_hex"])
                isk = hashlib.sha512(lv(b"CPace255_ISK", sid, shared) + lv(ya, ada) + lv(yb, adb)).digest()
                self.assertEqual(isk.hex(), ex["isk_hex"])
                mac_key = hashlib.sha512(b"CPaceMac" + sid + isk).digest()
                self.assertEqual(mac_key.hex(), ex["mac_key_hex"])
                tags = [hmac.new(mac_key, message, hashlib.sha512).digest() for message in (lv(ya, ada), lv(yb, adb), lv(b"beamer-pair-done"))]
                self.assertEqual([tag.hex() for tag in tags], [ex["ta_hex"], ex["tb_hex"], ex["td_hex"]])
                messages = ex["messages"]
                self.assertEqual([messages["pair_confirm"]["tag"], messages["pair_answer"]["tag"], messages["pair_done"]["tag"]], [pairing._b64(tag) for tag in tags])
                token = pairing._b64(hkdf(isk, sid, b"beamer-pair-token-v3"))
                self.assertEqual(token, ex["token"])
                self.assertTrue(protocol.is_paired_token(token))


class PairingV3FailureTests(unittest.TestCase):
    def test_wrong_prs_makes_tb_fail(self):
        vector = P3["wrong_prs"]
        client = pairing.PairingClient(
            vector["pair_id"], vector["code"], vector["requester"]["name"], unhex(vector["requester"]["id_hex"]),
            _scripted(unhex(vector["requester"]["nonce_hex"]), unhex(vector["requester"]["scalar_hex"])),
            version=3, platform=vector["requester"]["platform"], port=vector["requester"]["port"], qr_secret=unhex(vector["qr_secret_hex"]),
        )
        self.assertEqual(client.start(), vector["pair_start_sent"])
        self.assertNotIn("qr", vector["pair_start_received"])
        self.assertEqual({k: v for k, v in vector["pair_start_sent"].items() if k != "qr"}, vector["pair_start_received"])
        self.assertEqual(len(unhex(vector["prs_used_by_host_hex"])), 6)
        self.assertEqual(len(unhex(vector["prs_used_by_requester_hex"])), 22)
        with self.assertRaises(pairing.PairingError) as raised:
            client.accept(vector["pair_answer"])
        self.assertEqual(str(raised.exception), pairing.ERROR_REFUSED)
        self.assertNotEqual(vector["expected_tb_hex"], vector["answer_tb_hex"])
        self.assertEqual(pairing._b64(unhex(vector["answer_tb_hex"])), vector["pair_answer"]["tag"])
        self.assertEqual(vector["requester_must"], "refused")
        self.assertEqual(client.abort(), vector["pair_confirm_abort"])
        self.assertEqual(vector["host_reply_to_abort"], {"type": "pair_done", "pair": vector["pair_id"], "ok": False, "error": "refused"})

    def test_the_host_answers_the_stripped_start_with_the_committed_answer(self):
        vector = P3["wrong_prs"]
        host = pairing.PairingHost(
            name=vector["host"]["name"], identity=unhex(vector["host"]["id_hex"]), version=3, platform=vector["host"]["platform"], port=vector["host"]["port"],
            entropy=_scripted(unhex(vector["pair_id"]), unhex(vector["qr_secret_hex"]), unhex(vector["host"]["scalar_hex"])),
        )
        host.begin()
        host.code = vector["code"]
        self.assertEqual(host.handle(vector["pair_start_received"], "10.1.2.3"), vector["pair_answer"])
        self.assertEqual(host.handle(vector["pair_confirm_abort"], "10.1.2.3"), vector["host_reply_to_abort"])

    def test_the_refused_identities_are_refused_by_the_host(self):
        refusals = P3["refusals"]
        self.assertGreaterEqual(len(refusals["cases"]), 4)
        for case in refusals["cases"]:
            with self.subTest(case=case["comment"]):
                host = pairing.PairingHost(
                    name=refusals["host"]["name"], identity=unhex(refusals["host"]["id_hex"]), version=3, platform=refusals["host"]["platform"],
                    port=refusals["host"]["port"], entropy=_scripted(unhex(refusals["pair_id"]), unhex(refusals["qr_secret_hex"]), unhex(refusals["host"]["scalar_hex"])),
                )
                host.begin()
                host.code = refusals["code"]
                self.assertEqual(host.handle(case["pair_start"], "10.1.2.3"), case["must_answer"])
                self.assertEqual(case["must_answer"], {"type": "pair_answer", "pair": refusals["pair_id"], "ok": False, "error": "refused"})
                self.assertIsNone(host.code)


    def test_the_requester_refuses_a_host_id_of_the_wrong_shape(self):
        refusals = P3["refusals"]
        requester = refusals["requester"]
        self.assertGreaterEqual(len(refusals["answer_cases"]), 4)
        for case in refusals["answer_cases"]:
            with self.subTest(case=case["comment"]):
                client = pairing.PairingClient(
                    refusals["pair_id"], refusals["code"], requester["name"], unhex(requester["id_hex"]),
                    _scripted(unhex(requester["nonce_hex"]), unhex(requester["scalar_hex"])),
                    version=3, platform=requester["platform"], port=requester["port"],
                )
                self.assertEqual(client.start(), refusals["requester_start"])
                self.assertEqual(case["requester_must"], "refused")
                with self.assertRaises(pairing.PairingError) as raised:
                    client.accept(case["pair_answer"])
                self.assertEqual(str(raised.exception), pairing.ERROR_REFUSED)


class PairingV3TcpTests(unittest.TestCase):
    def test_the_frames_are_length_then_the_datagram(self):
        tcp = P3["tcp"]
        self.assertEqual([frame["from"] for frame in tcp["frames"]], ["host", "requester", "host", "requester", "host"])
        self.assertEqual([frame["message"]["type"] for frame in tcp["frames"]], ["beacon", "pair_start", "pair_answer", "pair_confirm", "pair_done"])
        exchange = next(ex for ex in P3["exchanges"] if ex["requester"]["port"] == 0)
        self.assertEqual(tcp["exchange_comment"], exchange["comment"])
        for frame in tcp["frames"]:
            with self.subTest(type=frame["message"]["type"]):
                data = unhex(frame["bytes_hex"])
                (length,) = struct.unpack(">I", data[:4])
                self.assertTrue(0 < length <= 1024)
                self.assertEqual(length, len(data) - 4)
                self.assertEqual(data[4:], pairing.encode(frame["message"]))
                self.assertEqual(pairing.decode(data[4:]), {"beamy": 1, **frame["message"]})
        self.assertEqual(tcp["frames"][0]["message"], exchange["beacon"])
        for frame, name in zip(tcp["frames"][1:], ("pair_start", "pair_answer", "pair_confirm", "pair_done")):
            self.assertEqual(frame["message"], exchange["messages"][name])
        self.assertIn("pair", tcp["frames"][0]["message"])
        self.assertIn("id", tcp["frames"][0]["message"])


class ClipboardImageVectors(unittest.TestCase):
    def test_every_case_fits_or_not_as_committed_and_drops_only_the_image(self):
        cases = V6["clipboard_images"]["cases"]
        self.assertEqual({case["fits"] for case in cases}, {True, False})
        for case in cases:
            with self.subTest(case=case["comment"]):
                image = unhex(case["png_hex"])
                self.assertIs(protocol.png_fits(image), case["fits"])
                read = protocol.read_clipboard(protocol.clipboard_msg("words", image))
                self.assertEqual(read, {"text": "words", "image": image if case["fits"] else None})


@unittest.skipUnless(os.path.exists(os.path.join(_TOOLS, "phone_vectors.py")), "the vector generator is not part of the public source")
class GeneratorTests(unittest.TestCase):
    OLD_SECTIONS = {
        "protocol_version": "ef2d127de37b942baad06145e54b0c619a1f22327b2ebbcfbec78f5564afe39d",
        "preamble": "6517d3d862d57c606e41e8011c9fda7e6433f1e44c7669bf50aaddf998f39f5b",
        "derive_key": "e316f30ab87e15ebbf371e7f97270900dcffb382c68cbae4cdd32f5e0079d620",
        "frames": "e49055d12a6cccda7c866013c2965fc7c16ce9539e53e161085e2ad7bf8c9905",
        "pairing": "05d9382f56577edd834f93b4bb5f33dcb797df8f0d483154c8318b03e57ea31e",
        "messages": "20ca870fdb61ec2f85a5a58d430e1e0e173f2712789ccda46c92ae13125259bc",
    }

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("phone_vectors", os.path.join(_TOOLS, "phone_vectors.py"))
        cls.generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.generator)
        with open(os.path.join(_TOOLS, "phone_vectors.json"), encoding="utf-8") as handle:
            cls.text = handle.read()
        cls.vectors = json.loads(cls.text)

    def test_the_committed_file_is_what_the_generator_writes_byte_for_byte(self):
        self.assertEqual(self.generator.render(self.generator.build(self.vectors)), self.text)

    def test_the_old_sections_have_the_bytes_they_had_when_version_6_was_added(self):
        for name, digest in self.OLD_SECTIONS.items():
            with self.subTest(section=name):
                self.assertEqual(hashlib.sha256(json.dumps(self.vectors[name], indent=2).encode("utf-8")).hexdigest(), digest)

    def test_the_old_sections_are_carried_not_computed(self):
        altered = {**self.vectors, "derive_key": {"carried": "as it stands"}}
        self.assertEqual(self.generator.build(altered)["derive_key"], {"carried": "as it stands"})

    def test_the_file_has_the_new_sections_and_the_test_copy_is_them(self):
        self.assertEqual(list(self.vectors)[-2:], ["v6", "pairing_v3"])
        self.assertEqual(self.vectors["v6"], V6)
        self.assertEqual(self.vectors["pairing_v3"], P3)
        copy_path = os.path.join(_TESTS_DIR, "v6_vectors.json")
        with open(copy_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), json.dumps({"v6": V6, "pairing_v3": P3}, indent=2) + "\n")

    def test_the_version_5_copy_the_replay_reads_is_the_file_s_own(self):
        names = ("protocol_version", "preamble", "derive_key", "frames")
        with open(os.path.join(_TESTS_DIR, "v5_vectors.json"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), json.dumps({name: self.vectors[name] for name in names}, indent=2) + "\n")

    def test_the_pairing_copy_the_version_2_tests_read_is_unchanged(self):
        with open(os.path.join(_TESTS_DIR, "pairing_vectors.json"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), json.dumps(self.vectors["pairing"], indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()
