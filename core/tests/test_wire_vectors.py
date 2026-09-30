"""The link's crypto against the committed v5 vectors in tools/phone_vectors.json, byte for byte.

The vectors were written by the cryptography-backed protocol.py of 1.4.x and are never
regenerated: libsodium through PyNaCl has to reproduce them exactly, in both directions, or a
1.5 app cannot talk to a 1.4 one or to the phone apps built against the same file. Each vector is
checked twice, once through protocol.py and once through the raw primitives with no protocol.py
in the way, so a mistake in the module and a mistake in the primitives cannot cancel out.

Also here: the failures are refused the way callers expect (AuthenticationError, and the
counter not spent), the pairing section's low-order shares are still refused, and no module of
either app imports cryptography, whose macOS wheels no longer run on Intel Macs.
"""

import ast
import hashlib
import hmac
import json
import os
import struct
import unittest

from nacl.bindings import crypto_aead_chacha20poly1305_ietf_decrypt, crypto_aead_chacha20poly1305_ietf_encrypt

from core import pairing
from core import protocol

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_TESTS_DIR))

with open(os.path.join(_REPO_ROOT, "tools", "phone_vectors.json"), encoding="utf-8") as _handle:
    VECTORS = json.load(_handle)


def _hkdf_sha256(key_material, salt, info):
    """RFC 5869 by hand, for one 32-byte block: an independent check on the module's HKDF."""
    extracted = hmac.new(salt, key_material, hashlib.sha256).digest()
    return hmac.new(extracted, info + b"\x01", hashlib.sha256).digest()


def _frame_parts(frame_hex):
    frame = bytes.fromhex(frame_hex)
    (length,) = struct.unpack(">I", frame[:protocol.HEADER_SIZE])
    body = frame[protocol.HEADER_SIZE:]
    assert length == len(body)
    return body[:protocol.COUNTER_SIZE], body[protocol.COUNTER_SIZE:]


class DeriveKeyVectorTests(unittest.TestCase):
    def test_the_salt_and_info_are_the_ones_in_the_file(self):
        vector = VECTORS["derive_key"]
        self.assertEqual(vector["salt_ascii"].encode("ascii"), protocol.KEY_SALT)
        self.assertEqual(vector["info_label_ascii"].encode("ascii"), protocol.KEY_INFO)
        self.assertEqual(VECTORS["protocol_version"], protocol.PROTOCOL_VERSION)

    def test_the_module_derives_the_committed_key(self):
        vector = VECTORS["derive_key"]
        sender, receiver = bytes.fromhex(vector["sender_prefix_hex"]), bytes.fromhex(vector["receiver_prefix_hex"])
        self.assertEqual(protocol.derive_key(vector["token"], sender, receiver).hex(), vector["key_hex"])

    def test_hmac_by_hand_derives_the_committed_key(self):
        vector = VECTORS["derive_key"]
        info = vector["info_label_ascii"].encode("ascii") + bytes.fromhex(vector["sender_prefix_hex"]) + bytes.fromhex(vector["receiver_prefix_hex"])
        key = _hkdf_sha256(vector["token"].encode("utf-8"), vector["salt_ascii"].encode("ascii"), info)
        self.assertEqual(key.hex(), vector["key_hex"])

    def test_the_two_directions_have_different_keys(self):
        vector = VECTORS["derive_key"]
        sender, receiver = bytes.fromhex(vector["sender_prefix_hex"]), bytes.fromhex(vector["receiver_prefix_hex"])
        self.assertNotEqual(protocol.derive_key(vector["token"], sender, receiver), protocol.derive_key(vector["token"], receiver, sender))

    def test_the_preamble_is_the_committed_one(self):
        vector = VECTORS["preamble"]
        session = protocol.SecureSession("any token", prefix=bytes.fromhex(vector["nonce_prefix_hex"]))
        self.assertEqual(session.preamble().hex(), vector["preamble_hex"])


class FrameVectorTests(unittest.TestCase):
    """Five frames of one link, alternating direction, each side counting from 1."""

    def setUp(self):
        vector = VECTORS["frames"]
        self.token = vector["token"]
        self.prefix_a = bytes.fromhex(vector["sender_prefix_hex"])
        self.prefix_b = bytes.fromhex(vector["receiver_prefix_hex"])
        self.frames = vector["frames"]
        self.assertEqual({frame["direction"] for frame in self.frames}, {"a_to_b", "b_to_a"})

    def _pair(self):
        side_a = protocol.SecureSession(self.token, prefix=self.prefix_a)
        side_b = protocol.SecureSession(self.token, prefix=self.prefix_b)
        side_a.accept_preamble(side_b.preamble())
        side_b.accept_preamble(side_a.preamble())
        return side_a, side_b

    def _ends(self, direction):
        """(sender prefix, receiver prefix) for one direction."""
        return (self.prefix_a, self.prefix_b) if direction == "a_to_b" else (self.prefix_b, self.prefix_a)

    def test_the_module_seals_every_frame_to_the_committed_bytes(self):
        side_a, side_b = self._pair()
        for frame in self.frames:
            with self.subTest(direction=frame["direction"], counter=frame["counter"]):
                sender = side_a if frame["direction"] == "a_to_b" else side_b
                self.assertEqual(sender.seal(frame["plaintext"]).hex(), frame["frame_hex"])

    def test_the_module_opens_every_committed_frame(self):
        side_a, side_b = self._pair()
        for frame in self.frames:
            with self.subTest(direction=frame["direction"], counter=frame["counter"]):
                receiver = side_b if frame["direction"] == "a_to_b" else side_a
                body = bytes.fromhex(frame["frame_hex"])[protocol.HEADER_SIZE:]
                self.assertEqual(receiver.open(body), frame["plaintext"])

    def test_libsodium_by_hand_seals_and_opens_every_committed_frame(self):
        for frame in self.frames:
            with self.subTest(direction=frame["direction"], counter=frame["counter"]):
                sender, receiver = self._ends(frame["direction"])
                key = _hkdf_sha256(self.token.encode("utf-8"), protocol.KEY_SALT, protocol.KEY_INFO + sender + receiver)
                counter, sealed = _frame_parts(frame["frame_hex"])
                self.assertEqual(struct.unpack(">I", counter)[0], frame["counter"])
                plaintext = json.dumps(frame["plaintext"]).encode("utf-8")
                self.assertEqual(crypto_aead_chacha20poly1305_ietf_encrypt(plaintext, None, sender + counter, key), sealed)
                self.assertEqual(crypto_aead_chacha20poly1305_ietf_decrypt(sealed, None, sender + counter, key), plaintext)

    def _refused(self, receiver, body):
        # Opened while another exception is being handled, as a caller's reader loop may be:
        # nothing about the failure is carried out, as before, neither a library error nor that one.
        with self.assertRaises(protocol.AuthenticationError) as caught:
            try:
                raise RuntimeError("handled elsewhere")
            except RuntimeError:
                receiver.open(body)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_a_flipped_bit_anywhere_after_the_counter_is_refused_and_spends_nothing(self):
        frame = self.frames[0]
        body = bytes.fromhex(frame["frame_hex"])[protocol.HEADER_SIZE:]
        for index in range(protocol.COUNTER_SIZE, len(body)):
            with self.subTest(byte=index):
                _, side_b = self._pair()
                tampered = bytearray(body)
                tampered[index] ^= 0x01
                self._refused(side_b, bytes(tampered))
                self.assertEqual(side_b.open(body), frame["plaintext"])

    def test_a_frame_shorter_than_its_tag_is_refused_as_a_tag_failure(self):
        body = bytes.fromhex(self.frames[0]["frame_hex"])[protocol.HEADER_SIZE:]
        for keep in (1, 8, 15, 16):
            with self.subTest(sealed_bytes=keep):
                _, side_b = self._pair()
                self._refused(side_b, body[:protocol.COUNTER_SIZE + keep])

    def test_a_body_that_is_not_bytes_like_is_a_type_error_not_a_tag_failure(self):
        body = bytes.fromhex(self.frames[0]["frame_hex"])[protocol.HEADER_SIZE:]
        _, side_b = self._pair()
        with self.assertRaises(TypeError):
            side_b.open(list(body))
        self.assertEqual(side_b.open(bytearray(body)), self.frames[0]["plaintext"])

    def test_a_frame_sealed_under_another_token_is_refused(self):
        stranger = protocol.SecureSession("another token", prefix=self.prefix_a)
        stranger.accept_preamble(protocol.SecureSession("another token", prefix=self.prefix_b).preamble())
        _, side_b = self._pair()
        self._refused(side_b, stranger.seal(self.frames[0]["plaintext"])[protocol.HEADER_SIZE:])

    def test_a_side_s_own_frame_reflected_back_is_refused(self):
        side_a, _ = self._pair()
        own = bytes.fromhex(self.frames[0]["frame_hex"])[protocol.HEADER_SIZE:]
        self._refused(side_a, own)

    def test_a_connection_recorded_under_other_prefixes_is_refused(self):
        _, side_b = self._pair()
        replayer = protocol.SecureSession(self.token, prefix=self.prefix_a)
        replayer.accept_preamble(protocol.SecureSession(self.token, prefix=bytes(8)).preamble())
        self._refused(side_b, replayer.seal(self.frames[0]["plaintext"])[protocol.HEADER_SIZE:])


class PairingShareVectorTests(unittest.TestCase):
    """Pairing moved to libsodium in 1.4.3; its low-order shares, from the same file."""

    def test_low_order_and_all_zero_shares_are_refused(self):
        low = VECTORS["pairing"]["low_order_shares"]
        scalar = bytes.fromhex(low["scalar_hex"])
        self.assertIn(bytes(32).hex(), low["must_refuse"])
        for share in low["must_refuse"]:
            with self.subTest(share=share):
                with self.assertRaises(pairing.PairingError):
                    pairing._x25519(scalar, bytes.fromhex(share))
        for vector in low["must_give"]:
            with self.subTest(share=vector["share_hex"]):
                self.assertEqual(pairing._x25519(scalar, bytes.fromhex(vector["share_hex"])).hex(), vector["product_hex"])


class NoCryptographyTests(unittest.TestCase):
    """cryptography 49 dropped x86_64 macOS wheels, so no module of either app may import it."""

    def test_no_module_imports_cryptography(self):
        offenders = []
        for top in ("mac_app", "win_app", "core", "tools"):
            for folder, dirs, files in os.walk(os.path.join(_REPO_ROOT, top)):
                dirs[:] = [name for name in dirs if not name.startswith(".") and name not in ("build", "dist", "__pycache__")]
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(folder, name)
                    with open(path, encoding="utf-8") as handle:
                        tree = ast.parse(handle.read(), path)
                    for node in ast.walk(tree):
                        modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                        if any(module == "cryptography" or module.startswith("cryptography.") for module in modules):
                            offenders.append(f"{os.path.relpath(path, _REPO_ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_neither_requirements_file_asks_for_it(self):
        for path in ("mac_app/requirements-mac.txt", "win_app/requirements-win.txt"):
            with self.subTest(path=path):
                with open(os.path.join(_REPO_ROOT, path), encoding="utf-8") as handle:
                    names = [line.split("#")[0].strip().lower() for line in handle]
                self.assertFalse([name for name in names if name.startswith("cryptography")])


if __name__ == "__main__":
    unittest.main()
