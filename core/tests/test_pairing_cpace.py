"""The pairing exchange itself: CPace against the published vectors, the committed vectors
replayed value by value, and the rules that hold an attacker to one guess at the code.

Pure: no sockets and no clock but the fake one, so it runs the same in both apps' suites.
"""

import json
import os
import unittest

from core import pairing
from core.pairing import PairingClient, PairingError, PairingHost, encode

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(_TESTS_DIR, "pairing_vectors.json"), encoding="utf-8") as _handle:
    VECTORS = json.load(_handle)


class FakeClock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def scripted(*values):
    queue = list(values)
    return lambda length: queue.pop(0)


def other_code(code):
    return str((int(code) + 1) % 10**6).zfill(6)


def paired(host, code, name="Test Mac"):
    """One honest exchange, as (client, start, answer, confirm, done, token)."""
    client = PairingClient(host.pair_id, code, name)
    start = client.start()
    answer = host.handle(start)
    confirm = client.accept(answer)
    done = host.handle(confirm)
    return client, start, answer, confirm, done, client.finish(done)


class PublishedVectorTests(unittest.TestCase):
    """The numbers other people published, so the core is right before Beamer adds to it."""

    def test_length_prefixes_match_the_draft(self):
        self.assertEqual(pairing._prepend_len(b"").hex(), "00")
        self.assertEqual(pairing._prepend_len(b"1234").hex(), "0431323334")
        self.assertEqual(pairing._prepend_len(bytes(128))[:2].hex(), "8001")
        self.assertEqual(pairing._prepend_len(bytes(300))[:2].hex(), "ac02")
        self.assertEqual(pairing._lv_cat(b"1234", b"5", b"", b"678").hex(), "043132333401350003363738")

    def test_the_draft_exchange_is_reproduced_step_by_step(self):
        draft = VECTORS["cpace_draft"]
        sid = bytes.fromhex(draft["sid_hex"])
        generator = pairing._generator(draft["prs_ascii"].encode(), bytes.fromhex(draft["ci_hex"]), sid)
        self.assertEqual(generator.hex(), draft["generator_hex"])
        ya_scalar = bytes.fromhex(draft["ya_scalar_hex"])
        yb_scalar = bytes.fromhex(draft["yb_scalar_hex"])
        ya = pairing._x25519(ya_scalar, generator)
        yb = pairing._x25519(yb_scalar, generator)
        self.assertEqual((ya.hex(), yb.hex()), (draft["ya_hex"], draft["yb_hex"]))
        self.assertEqual(pairing._x25519(ya_scalar, yb).hex(), draft["k_hex"])
        self.assertEqual(pairing._x25519(yb_scalar, ya).hex(), draft["k_hex"])

    def test_elligator_matches_rfc_9380(self):
        for vector in VECTORS["elligator2"]:
            with self.subTest(element=vector["field_element_hex_be"]):
                self.assertEqual(pairing._elligator2(int(vector["field_element_hex_be"], 16)), int(vector["u_hex_be"], 16))

    def test_low_order_shares_are_refused(self):
        low = VECTORS["low_order_shares"]
        scalar = bytes.fromhex(low["scalar_hex"])
        for share in low["must_refuse"]:
            with self.subTest(share=share):
                with self.assertRaises(PairingError):
                    pairing._x25519(scalar, bytes.fromhex(share))
        for vector in low["must_give"]:
            with self.subTest(share=vector["share_hex"]):
                self.assertEqual(pairing._x25519(scalar, bytes.fromhex(vector["share_hex"])).hex(), vector["product_hex"])

    def test_base64_has_one_spelling(self):
        for length in range(40):
            data = bytes(range(length))
            self.assertEqual(pairing._unb64(pairing._b64(data)), data)
        for text in ("AA==", "AA=", "A", "AAB", "AB+/", " AAAA", "AAAA\n", "!!!!", "====", "AA!A", None, 7, b"AAAA"):
            with self.subTest(text=text):
                with self.assertRaises(PairingError):
                    pairing._unb64(text)

    def test_hkdf_matches_rfc_5869(self):
        # Test case 1, whose first 32 bytes are the single block this HKDF produces.
        block = pairing.protocol.hkdf_sha256(bytes([0x0B]) * 22, bytes(range(13)), bytes(range(0xF0, 0xFA)))
        self.assertEqual(block.hex(), "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf")


class PhoneVectorTests(unittest.TestCase):
    """pairing_vectors.json is what a second implementation is built against; these replay
    it through this module, so a change to the exchange cannot leave the file behind."""

    def test_constants_are_the_ones_in_the_file(self):
        constants = VECTORS["constants"]
        self.assertEqual(VECTORS["pairing_version"], pairing.PAIRING_VERSION)
        self.assertEqual(constants["channel_ascii"].encode(), pairing.CHANNEL)
        self.assertEqual(constants["role_requester_ascii"].encode(), pairing.ROLE_REQUESTER)
        self.assertEqual(constants["role_host_ascii"].encode(), pairing.ROLE_HOST)
        self.assertEqual(constants["token_info_ascii"].encode(), pairing.TOKEN_INFO)
        self.assertEqual(constants["done_label_ascii"].encode(), pairing.DONE_LABEL)
        self.assertEqual(constants["max_name_chars"], pairing.MAX_NAME_CHARS)

    def test_each_exchange_replays_to_the_same_messages_and_token(self):
        for vector in VECTORS["exchanges"]:
            with self.subTest(exchange=vector["comment"]):
                requester, host_side = vector["requester"], vector["host"]
                host = PairingHost(
                    name=host_side["name"],
                    identity=bytes.fromhex(host_side["id_hex"]),
                    entropy=scripted(bytes.fromhex(vector["pair_id"]), bytes.fromhex(host_side["scalar_hex"])),
                )
                host.begin()
                host.code = vector["code"]
                self.assertEqual(host.pair_id, vector["pair_id"])
                client = PairingClient(
                    host.pair_id,
                    vector["code"],
                    requester["name"],
                    identity=bytes.fromhex(requester["id_hex"]),
                    entropy=scripted(bytes.fromhex(requester["nonce_hex"]), bytes.fromhex(requester["scalar_hex"])),
                )
                messages = vector["messages"]
                self.assertEqual(client.start(), messages["pair_start"])
                answer = host.handle(messages["pair_start"])
                self.assertEqual(answer, messages["pair_answer"])
                confirm = client.accept(answer)
                self.assertEqual(confirm, messages["pair_confirm"])
                done = host.handle(confirm)
                self.assertEqual(done, messages["pair_done"])
                self.assertEqual(client.finish(done), vector["token"])
                self.assertEqual(host.paired, (vector["token"], requester["name"]))

    def test_each_exchange_s_intermediate_values_follow_the_specification(self):
        for vector in VECTORS["exchanges"]:
            with self.subTest(exchange=vector["comment"]):
                requester, host_side = vector["requester"], vector["host"]
                sid = pairing._session_id(vector["pair_id"], bytes.fromhex(requester["nonce_hex"]))
                self.assertEqual(sid.hex(), vector["sid_hex"])
                ada = pairing._party(pairing.ROLE_REQUESTER, bytes.fromhex(requester["id_hex"]), requester["name"])
                adb = pairing._party(pairing.ROLE_HOST, bytes.fromhex(host_side["id_hex"]), host_side["name"])
                self.assertEqual((ada.hex(), adb.hex()), (vector["ada_hex"], vector["adb_hex"]))
                self.assertEqual(pairing._generator(vector["code"].encode(), pairing.CHANNEL, sid).hex(), vector["generator_hex"])
                ya = pairing._share(vector["code"], sid, bytes.fromhex(requester["scalar_hex"]))
                yb = pairing._share(vector["code"], sid, bytes.fromhex(host_side["scalar_hex"]))
                self.assertEqual((ya.hex(), yb.hex()), (vector["ya_hex"], vector["yb_hex"]))
                for scalar, peer in ((requester["scalar_hex"], yb), (host_side["scalar_hex"], ya)):
                    shared = pairing._x25519(bytes.fromhex(scalar), peer)
                    token, ta, tb, td = pairing._keys(sid, shared, ya, ada, yb, adb)
                    self.assertEqual(token, vector["token"])
                    self.assertEqual((ta.hex(), tb.hex(), td.hex()), (vector["ta_hex"], vector["tb_hex"], vector["td_hex"]))

    def test_the_wrong_code_vector_is_refused_and_aborted(self):
        vector = VECTORS["wrong_code"]
        requester = vector["requester"]
        client = PairingClient(
            vector["pair_id"],
            vector["requester_code"],
            requester["name"],
            entropy=scripted(bytes.fromhex(requester["nonce_hex"]), bytes.fromhex(requester["scalar_hex"])),
        )
        self.assertEqual(client.start(), vector["pair_start"])
        with self.assertRaises(PairingError) as caught:
            client.accept(vector["pair_answer"])
        self.assertEqual(str(caught.exception), pairing.ERROR_REFUSED)
        self.assertEqual(client.abort(), vector["pair_confirm_abort"])
        self.assertEqual(vector["requester_must"], pairing.ERROR_REFUSED)

    def test_a_name_is_cut_by_code_points(self):
        vector = VECTORS["name_cut"]
        client = PairingClient("00" * 16, "482913", vector["name"])
        self.assertEqual(client.start()["name"], vector["sent"])
        self.assertEqual((len(vector["name"]), len(vector["sent"])), (vector["code_points"], pairing.MAX_NAME_CHARS))


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.host = PairingHost(self.clock, name="TEST-PC")
        self.code = self.host.begin()

    def test_right_code_agrees_one_token_without_sending_it_or_the_code(self):
        _client, start, answer, confirm, done, token = paired(self.host, self.code)
        self.assertEqual(self.host.paired, (token, "Test Mac"))
        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(self.host.outcome, "paired")
        self.assertFalse(self.host.active)
        wire = b"".join(encode(message) for message in (start, answer, confirm, done))
        self.assertNotIn(token.encode(), wire)
        self.assertNotIn(self.code.encode(), wire)

    def test_the_host_stores_nothing_until_the_requester_confirms(self):
        client = PairingClient(self.host.pair_id, self.code, "Test Mac")
        answer = self.host.handle(client.start())
        self.assertTrue(answer["ok"])
        self.assertIsNone(self.host.paired)
        self.assertTrue(self.host.active)

    def test_two_exchanges_never_share_a_token_or_a_share(self):
        _client, first_start, _answer, _confirm, _done, first = paired(self.host, self.code)
        code = self.host.begin()
        _client, second_start, _answer, _confirm, _done, second = paired(self.host, code)
        self.assertNotEqual(first, second)
        self.assertNotEqual(first_start["share"], second_start["share"])

    def test_a_requester_with_the_wrong_code_refuses_the_answer_and_never_confirms(self):
        client = PairingClient(self.host.pair_id, other_code(self.code), "Test Mac")
        answer = self.host.handle(client.start())
        self.assertTrue(answer["ok"], "the host cannot tell a wrong code from its answer alone")
        with self.assertRaises(PairingError) as caught:
            client.accept(answer)
        self.assertEqual(str(caught.exception), pairing.ERROR_REFUSED)
        self.assertIsNone(self.host.paired)

    def test_the_abort_ends_the_code_and_says_a_wrong_one_was_entered(self):
        client = PairingClient(self.host.pair_id, other_code(self.code), "Test Mac")
        self.host.handle(client.start())
        done = self.host.handle(client.abort())
        self.assertEqual(done, {"type": "pair_done", "pair": client.pair_id, "ok": False, "error": "refused"})
        self.assertFalse(self.host.active)
        self.assertEqual(self.host.outcome, "refused")
        self.assertIsNone(self.host.paired)

    def test_a_host_with_another_code_is_refused(self):
        impostor = PairingHost(FakeClock(), name="TEST-PC")
        impostor.begin()
        impostor.code = other_code(self.code)
        impostor.pair_id = self.host.pair_id
        client = PairingClient(self.host.pair_id, self.code, "Test Mac")
        with self.assertRaises(PairingError):
            client.accept(impostor.handle(client.start()))
        with self.assertRaises(PairingError):
            client.finish({"type": "pair_done", "pair": client.pair_id, "ok": True, "tag": ""})


class OneGuessTests(unittest.TestCase):
    """Each rule here is what stops a second guess at the code; loosening one is a hole."""

    def setUp(self):
        self.clock = FakeClock()
        self.host = PairingHost(self.clock, name="TEST-PC")
        self.code = self.host.begin()

    def test_a_code_answers_one_share_even_for_the_right_code(self):
        attacker = PairingClient(self.host.pair_id, other_code(self.code), "Attacker")
        self.assertTrue(self.host.handle(attacker.start())["ok"])
        honest = PairingClient(self.host.pair_id, self.code, "Test Mac")
        reply = self.host.handle(honest.start())
        self.assertEqual(reply, {"type": "pair_answer", "pair": honest.pair_id, "ok": False, "error": "refused"})
        second = PairingClient(self.host.pair_id, other_code(other_code(self.code)), "Attacker")
        self.assertFalse(self.host.handle(second.start())["ok"])

    def test_a_second_share_does_not_disturb_the_exchange_under_way(self):
        honest = PairingClient(self.host.pair_id, self.code, "Test Mac")
        answer = self.host.handle(honest.start())
        stranger = PairingClient(self.host.pair_id, other_code(self.code), "Attacker")
        self.assertFalse(self.host.handle(stranger.start())["ok"])
        done = self.host.handle(honest.accept(answer))
        self.assertEqual(self.host.paired[0], honest.finish(done))

    def test_a_requester_judges_one_answer_and_no_more(self):
        client = PairingClient(self.host.pair_id, self.code, "Test Mac")
        good = self.host.handle(client.start())
        forged = dict(good, tag=pairing._b64(bytes(pairing.TAG_BYTES)))
        with self.assertRaises(PairingError):
            client.accept(forged)
        with self.assertRaises(PairingError) as caught:
            client.accept(good)
        self.assertEqual(str(caught.exception), pairing.ERROR_REFUSED)

    def test_an_error_answer_also_spends_the_attempt(self):
        client = PairingClient(self.host.pair_id, self.code, "Test Mac")
        good = self.host.handle(client.start())
        with self.assertRaises(PairingError):
            client.accept({"type": "pair_answer", "pair": client.pair_id, "ok": False, "error": "not_pairing"})
        with self.assertRaises(PairingError):
            client.accept(good)

    def test_a_wrong_confirmation_ends_the_code_and_stores_nothing(self):
        client = PairingClient(self.host.pair_id, self.code, "Test Mac")
        answer = self.host.handle(client.start())
        confirm = client.accept(answer)
        forged = dict(confirm, tag=pairing._b64(bytes(pairing.TAG_BYTES)))
        self.assertEqual(self.host.handle(forged)["error"], "refused")
        self.assertIsNone(self.host.paired)
        self.assertEqual(self.host.handle(confirm)["error"], "not_pairing")
        self.assertIsNone(self.host.paired)

    def test_the_host_s_own_tag_reflected_back_is_not_a_confirmation(self):
        client = PairingClient(self.host.pair_id, self.code, "Test Mac")
        answer = self.host.handle(client.start())
        done = self.host.handle({"type": "pair_confirm", "pair": client.pair_id, "tag": answer["tag"]})
        self.assertEqual(done["error"], "refused")
        self.assertIsNone(self.host.paired)

    def test_a_confirmation_with_no_exchange_under_way_does_nothing(self):
        done = self.host.handle({"type": "pair_confirm", "pair": self.host.pair_id, "tag": ""})
        self.assertEqual(done["error"], "not_pairing")
        self.assertTrue(self.host.active)

    def test_an_unknown_pairing_id_does_not_touch_the_code(self):
        stranger = PairingClient("deadbeef", self.code, "Test Mac")
        self.assertEqual(self.host.handle(stranger.start())["error"], "not_pairing")
        self.assertTrue(self.host.active)
        paired(self.host, self.code)

    def test_a_tampered_share_or_name_is_refused(self):
        for field, value in (("share", pairing._b64(bytes(range(1, 33)))), ("name", "EVIL-PC"), ("id", pairing._b64(b"x"))):
            with self.subTest(field=field):
                host = PairingHost(FakeClock(), name="TEST-PC")
                code = host.begin()
                client = PairingClient(host.pair_id, code, "Test Mac")
                answer = dict(host.handle(client.start()), **{field: value})
                with self.assertRaises(PairingError):
                    client.accept(answer)

    def test_a_name_changed_on_the_way_to_the_host_fails_the_exchange(self):
        client = PairingClient(self.host.pair_id, self.code, "Test Mac")
        answer = self.host.handle(dict(client.start(), name="Someone Else"))
        with self.assertRaises(PairingError):
            client.accept(answer)

    def test_identities_are_bound_into_the_token(self):
        host = PairingHost(FakeClock(), name="TEST-PC", identity=b"h" * 16)
        code = host.begin()
        client = PairingClient(host.pair_id, code, "Test Mac", identity=b"r" * 16)
        start = client.start()
        answer = host.handle(start)
        self.assertEqual((start["id"], answer["id"]), (pairing._b64(b"r" * 16), pairing._b64(b"h" * 16)))
        self.assertEqual(client.finish(host.handle(client.accept(answer))), host.paired[0])
        host = PairingHost(FakeClock(), name="TEST-PC", identity=b"h" * 16)
        code = host.begin()
        client = PairingClient(host.pair_id, code, "Test Mac", identity=b"r" * 16)
        answer = host.handle(dict(client.start(), id=pairing._b64(b"q" * 16)))
        with self.assertRaises(PairingError):
            client.accept(answer)


class HostStateTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.host = PairingHost(self.clock, name="TEST-PC")
        self.code = self.host.begin()

    def test_retransmitted_datagrams_get_the_same_replies(self):
        client = PairingClient(self.host.pair_id, self.code, "Test Mac")
        start = client.start()
        answer = self.host.handle(start)
        self.assertEqual(self.host.handle(start), answer)
        confirm = client.accept(answer)
        done = self.host.handle(confirm)
        self.assertEqual(self.host.handle(confirm), done)
        self.assertTrue(done["ok"])

    def test_a_refused_start_is_answered_the_same_way_again(self):
        start = dict(PairingClient(self.host.pair_id, self.code, "Test Mac").start(), share="!!")
        reply = self.host.handle(start)
        self.assertEqual(reply["error"], "refused")
        self.assertEqual(self.host.handle(start), reply)

    def test_an_expired_code_is_not_answered(self):
        pair_id = self.host.pair_id
        self.clock.advance(pairing.CODE_LIFETIME_SECONDS)
        client = PairingClient(pair_id, self.code, "Test Mac")
        self.assertEqual(self.host.handle(client.start())["error"], "not_pairing")
        self.assertEqual(self.host.outcome, "expired")

    def test_an_exchange_the_code_outlives_is_not_completed(self):
        client = PairingClient(self.host.pair_id, self.code, "Test Mac")
        confirm = client.accept(self.host.handle(client.start()))
        self.clock.advance(pairing.CODE_LIFETIME_SECONDS)
        self.assertEqual(self.host.handle(confirm)["error"], "not_pairing")
        self.assertIsNone(self.host.paired)

    def test_a_new_code_forgets_the_last_exchange(self):
        client, start, _answer, confirm, _done, _token = paired(self.host, self.code)
        self.host.paired = None
        self.host.begin()
        self.assertEqual(self.host.handle(start)["error"], "not_pairing")
        self.assertEqual(self.host.handle(confirm)["error"], "not_pairing")
        self.assertIsNone(self.host.paired)

    def test_malformed_starts_are_refused_without_an_exception(self):
        good = PairingClient(self.host.pair_id, self.code, "Test Mac").start()
        bad_fields = [
            ("nonce", "!!"), ("nonce", pairing._b64(b"short")), ("nonce", None), ("nonce", ["a"]),
            ("share", "??"), ("share", pairing._b64(bytes(31))), ("share", 7), ("share", {"a": 1}),
            ("share", pairing._b64(bytes(32))), ("share", pairing._b64(b"\x01" + bytes(31))),
            ("name", None), ("name", 12), ("name", "x" * (pairing.MAX_NAME_CHARS + 1)), ("name", "\ud800"),
            ("id", None), ("id", pairing._b64(bytes(pairing.MAX_IDENTITY_BYTES + 1))), ("id", "é"),
        ]
        for field, value in bad_fields:
            with self.subTest(field=field, value=value):
                host = PairingHost(FakeClock(), name="TEST-PC")
                host.begin()
                host.code = self.code
                host.pair_id = self.host.pair_id
                reply = host.handle(dict(good, **{field: value}))
                self.assertEqual(reply, {"type": "pair_answer", "pair": host.pair_id or good["pair"], "ok": False, "error": "refused"})
                self.assertFalse(host.active)
                self.assertIsNone(host.paired)

    def test_a_refusal_never_touches_the_code(self):
        # A refusal goes back at once, so nothing worked out from the code may come before it:
        # how long that took would be in the reply's timing.
        good = PairingClient(self.host.pair_id, self.code, "Test Mac").start()
        calls = []
        real = pairing._generator
        pairing._generator = lambda *args: calls.append(args) or real(*args)
        try:
            for field, value in (("share", pairing._b64(bytes(32))), ("share", pairing._b64(b"\x01" + bytes(31))),
                                 ("name", "\ud800"), ("nonce", "!!"), ("id", "=")):
                host = PairingHost(FakeClock(), name="TEST-PC")
                host.begin()
                host.code = self.code
                reply = host.handle(dict(good, pair=host.pair_id, **{field: value}))
                self.assertEqual(reply["error"], "refused")
                self.assertFalse(host.fresh_answer)
            self.assertEqual(calls, [])
            self.assertTrue(self.host.handle(good)["ok"])
            self.assertEqual(len(calls), 1)
        finally:
            pairing._generator = real

    def test_only_a_newly_made_answer_is_marked_fresh(self):
        start = PairingClient(self.host.pair_id, self.code, "Test Mac").start()
        self.assertFalse(self.host.fresh_answer)
        self.host.handle(start)
        self.assertTrue(self.host.fresh_answer)
        self.host.fresh_answer = False
        self.assertTrue(self.host.handle(start)["ok"])
        self.assertFalse(self.host.fresh_answer)

    def test_datagrams_that_are_not_for_the_host_get_no_answer(self):
        self.assertIsNone(self.host.handle({"type": "beacon", "name": "x", "port": 1}))
        self.assertIsNone(self.host.handle({"type": "pair_start", "pair": 7}))
        for pair_id in ("", "x" * 65, "\u00e9" * 8, "\ud83d"):
            self.assertIsNone(self.host.handle({"type": "pair_start", "pair": pair_id}))
            self.assertIsNone(self.host.handle({"type": "pair_request", "pair": pair_id}))
        self.assertIsNone(self.host.handle({"type": "pair_answer", "pair": self.host.pair_id}))
        self.assertTrue(self.host.active)

    def test_another_pairing_version_is_told_so(self):
        start = dict(PairingClient(self.host.pair_id, self.code, "Test Mac").start(), v=3)
        self.assertEqual(self.host.handle(start)["error"], "version")
        self.assertEqual(self.host.outcome, "version")
        self.assertFalse(self.host.active)

    def test_a_1_4_2_request_is_refused_in_the_reply_that_mac_reads(self):
        request = {"type": "pair_request", "pair": self.host.pair_id, "pub": "AAAA", "proof": "BBBB", "name": "Old Mac"}
        reply = self.host.handle(request)
        self.assertEqual(reply, {"type": "pair_reply", "pair": request["pair"], "ok": False, "error": "refused"})
        self.assertEqual(self.host.outcome, "version")
        self.assertIsNone(self.host.paired)
        self.assertEqual(self.host.handle(request)["error"], "not_pairing")

    def test_a_1_4_2_request_does_not_disturb_an_exchange_under_way(self):
        client = PairingClient(self.host.pair_id, self.code, "Test Mac")
        answer = self.host.handle(client.start())
        self.host.handle({"type": "pair_request", "pair": self.host.pair_id, "pub": "AAAA", "proof": "BBBB"})
        done = self.host.handle(client.accept(answer))
        self.assertEqual(self.host.paired[0], client.finish(done))


class ClientStateTests(unittest.TestCase):
    def setUp(self):
        self.host = PairingHost(FakeClock(), name="TEST-PC")
        self.code = self.host.begin()
        self.client = PairingClient(self.host.pair_id, self.code, "Test Mac")

    def test_malformed_answers_are_refused_without_an_exception(self):
        good = self.host.handle(self.client.start())
        bad_fields = [
            ("share", None), ("share", pairing._b64(bytes(32))), ("share", pairing._b64(bytes(33))), ("share", [1]),
            ("tag", None), ("tag", "!!"), ("tag", ""), ("name", 5), ("name", "\udfff"),
            ("name", "x" * (pairing.MAX_NAME_CHARS + 1)), ("id", 1.5), ("ok", 1), ("ok", "true"),
        ]
        for field, value in bad_fields:
            with self.subTest(field=field, value=value):
                client = PairingClient(self.host.pair_id, self.code, "Test Mac")
                with self.assertRaises(PairingError):
                    client.accept(dict(good, **{field: value}))

    def test_a_reply_to_another_attempt_is_not_judged(self):
        good = self.host.handle(self.client.start())
        for stray in (dict(good, pair="someone-else"), dict(good, type="pair_done"), {"type": "pair_reply", "pair": self.client.pair_id}):
            with self.assertRaises(PairingError) as caught:
                self.client.accept(stray)
            self.assertEqual(str(caught.exception), "not a reply to this attempt")
        self.client.accept(good)

    def test_the_token_is_only_handed_over_for_the_host_s_own_receipt(self):
        confirm = self.client.accept(self.host.handle(self.client.start()))
        done = self.host.handle(confirm)
        for forged in (dict(done, tag=confirm["tag"]), dict(done, tag=""), dict(done, tag=None)):
            with self.assertRaises(PairingError) as caught:
                self.client.finish(forged)
            self.assertEqual(str(caught.exception), pairing.ERROR_REFUSED)
        with self.assertRaises(PairingError) as caught:
            self.client.finish({"type": "pair_done", "pair": self.client.pair_id, "ok": False, "error": "not_pairing"})
        self.assertEqual(str(caught.exception), pairing.ERROR_NOT_PAIRING)
        self.assertEqual(self.client.finish(done), self.host.paired[0])

    def test_the_host_s_name_is_known_only_once_its_tag_checks_out(self):
        answer = self.host.handle(self.client.start())
        self.assertIsNone(self.client.host_name)
        self.client.accept(answer)
        self.assertEqual((self.client.host_name, self.client.host_identity), ("TEST-PC", b""))

    def test_no_token_before_the_answer_was_accepted(self):
        with self.assertRaises(PairingError):
            self.client.finish({"type": "pair_done", "pair": self.client.pair_id, "ok": True, "tag": ""})

    def test_names_are_cut_before_they_are_sent_and_bound_as_sent(self):
        long_name = "M" * 200
        host = PairingHost(FakeClock(), name="P" * 200)
        code = host.begin()
        _client, start, answer, _confirm, _done, token = paired(host, code, name=long_name)
        self.assertEqual(start["name"], long_name[: pairing.MAX_NAME_CHARS])
        self.assertEqual(answer["name"], "P" * pairing.MAX_NAME_CHARS)
        self.assertEqual(host.paired, (token, long_name[: pairing.MAX_NAME_CHARS]))

    def test_the_largest_datagrams_fit(self):
        name = "\U0001d539" * pairing.MAX_NAME_CHARS
        host = PairingHost(FakeClock(), name=name, identity=bytes(pairing.MAX_IDENTITY_BYTES))
        code = host.begin()
        client = PairingClient(host.pair_id, code, name, identity=bytes(pairing.MAX_IDENTITY_BYTES))
        start = client.start()
        answer = host.handle(start)
        confirm = client.accept(answer)
        done = host.handle(confirm)
        for message in (start, answer, confirm, done, pairing.beacon_msg(name, 65535, host.pair_id or start["pair"])):
            self.assertLessEqual(len(encode(message)), pairing.MAX_DATAGRAM_BYTES)
            self.assertEqual(pairing.decode(encode(message))["type"], message["type"])
        self.assertEqual(client.finish(done), host.paired[0])


class VersionTests(unittest.TestCase):
    def test_every_beacon_says_which_exchange_the_host_speaks(self):
        self.assertEqual(pairing.beacon_msg("TEST-PC", 51820)["pairing"], pairing.PAIRING_VERSION)
        self.assertEqual(pairing.beacon_msg("TEST-PC", 51820, "abcd")["pairing"], pairing.PAIRING_VERSION)

    def test_a_host_on_another_version_is_never_sent_anything(self):
        class Recorder:
            sent = []

            def sendto(self, data, address):
                self.sent.append((data, address))

        discovery = pairing.Discovery(bind_port=0)
        discovery._sock = Recorder()
        for version in (1, 3, None):
            pc = {"name": "OLD-PC", "address": "127.0.0.1", "port": 1, "reply_port": 1, "pair_id": "abcd", "pairing": version}
            with self.assertRaises(PairingError) as caught:
                discovery.pair(pc, "123456")
            self.assertEqual(str(caught.exception), pairing.ERROR_VERSION)
        self.assertEqual(Recorder.sent, [])

    def test_a_pairing_id_that_could_not_be_hashed_is_not_offered(self):
        discovery = pairing.Discovery(bind_port=0)
        for number, pair_id in enumerate(("\ud800", "é" * 8, "a" * 65, "", 7)):
            beacon = dict(pairing.beacon_msg("PC", 24820), pair=pair_id)
            discovery._note_beacon(beacon, (f"127.0.0.{number + 1}", 24821))
        self.assertEqual([pc["pair_id"] for pc in discovery.pcs()], [None] * 5)

    def test_a_beacon_whose_name_cannot_be_shown_is_dropped(self):
        discovery = pairing.Discovery(bind_port=0)
        discovery._note_beacon(pairing.beacon_msg("\ud800", 24820), ("127.0.0.1", 24821))
        self.assertEqual(discovery.pcs(), [])

    def test_only_one_attempt_runs_at_a_time(self):
        discovery = pairing.Discovery(bind_port=0)
        discovery._sock = object()
        discovery._pairing.acquire()
        pc = {"name": "PC", "address": "127.0.0.1", "port": 1, "reply_port": 1, "pair_id": "ab" * 16, "pairing": pairing.PAIRING_VERSION}
        with self.assertRaises(PairingError) as caught:
            discovery.pair(pc, "123456")
        self.assertEqual(str(caught.exception), "busy")

    def test_the_ration_is_not_wiped_by_a_burst_of_made_up_addresses(self):
        last = {}
        self.assertTrue(pairing.Announcer._due(last, "target", 100.0))
        for number in range(400):
            pairing.Announcer._due(last, f"spoofed-{number}", 100.1)
        self.assertFalse(pairing.Announcer._due(last, "target", 100.2))
        self.assertTrue(pairing.Announcer._due(last, "target", 101.0))
        self.assertLessEqual(len(last), 258)

    def test_forged_beacons_cannot_make_the_list_grow_without_end(self):
        clock = FakeClock()
        discovery = pairing.Discovery(bind_port=0, clock=clock)
        for number in range(500):
            discovery._note_beacon(pairing.beacon_msg(f"PC-{number}", 24820), (f"10.1.{number // 250}.{number % 250 + 1}", 24821))
        self.assertEqual(len(discovery._seen), pairing.MAX_SEEN_HOSTS)
        # A full list lets a newcomer in, the longest quiet making way for it.
        discovery._note_beacon(pairing.beacon_msg("REAL-PC", 24820), ("10.9.9.9", 24821))
        self.assertIn("REAL-PC", [pc["name"] for pc in discovery.pcs()])
        # A flood can still crowd it off again. The way through is the PC's address, typed:
        # that one is never the one to go.
        discovery._finding = ("10.9.9.9", 24821)
        for second in range(6):
            clock.advance(1.0)
            if second % 2 == 0:
                discovery._note_beacon(pairing.beacon_msg("REAL-PC", 24820), ("10.9.9.9", 24821))
            for number in range(200):
                discovery._note_beacon(pairing.beacon_msg("FORGED", 24820), (f"10.3.{second}.{number + 1}", 24821))
            self.assertIn("REAL-PC", [pc["name"] for pc in discovery.pcs()])
            self.assertEqual(len(discovery._seen), pairing.MAX_SEEN_HOSTS)
        discovery._finding = None
        clock.advance(pairing.BEACON_STALE_SECONDS + 1)
        discovery._note_beacon(pairing.beacon_msg("LATE-PC", 24820), ("10.2.0.1", 24821))
        self.assertEqual([pc["name"] for pc in discovery.pcs()], ["LATE-PC"])

    def test_a_datagram_of_nothing_but_brackets_is_just_ignored(self):
        self.assertIsNone(pairing.decode(b"[" * pairing.MAX_DATAGRAM_BYTES))
        self.assertIsNone(pairing.decode(b'{"a":' * 170))

    def test_a_beacon_without_a_version_is_the_old_exchange(self):
        discovery = pairing.Discovery(bind_port=0)
        discovery._note_beacon({"type": "beacon", "name": "OLD-PC", "port": 24820, "pair": "abcd"}, ("127.0.0.1", 24821))
        discovery._note_beacon(pairing.beacon_msg("NEW-PC", 24820, "abcd"), ("127.0.0.2", 24821))
        self.assertEqual({pc["name"]: pc["pairing"] for pc in discovery.pcs()}, {"OLD-PC": 1, "NEW-PC": pairing.PAIRING_VERSION})


if __name__ == "__main__":
    unittest.main()
