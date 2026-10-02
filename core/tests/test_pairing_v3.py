"""Pairing version 3 (WIRE.md section 6): the exchange with 16-byte machine ids, platform and
port bound in, the `known` and `full` refusals, the peer entry written before `pair_done`, the
key id clash, and the QR's second secret. The QR text is here too.

Pure: no sockets, a fake clock for the code's life and a fake wall clock for `paired_at`.
"""

import hashlib
import hmac
import secrets
import unittest

from core import pairing
from core.pairing import AlreadyPaired, PairingClient, PairingError, PairingHost

REQUESTER_ID = bytes(range(1, 17))
HOST_ID = bytes(range(101, 117))
OTHER_ID = bytes(range(201, 217))


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


def hkdf_block(ikm, salt, info):
    """RFC 5869 for one 32-byte block, written here rather than borrowed from the code under test."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()


def b64(data):
    return pairing._b64(data)


class Book:
    """One machine's peers list as the apps keep it, with the store callback pairing calls."""

    def __init__(self, entries=()):
        self.entries = [dict(entry) for entry in entries]
        self.stored = []
        self.fail = False

    def peers(self):
        return [dict(entry) for entry in self.entries]

    def store(self, entry, replaces):
        if self.fail:
            raise OSError("disk full")
        if replaces is not None:
            self.entries = [known for known in self.entries if known["id"] != replaces["id"]]
        self.entries.append(dict(entry))
        self.stored.append((dict(entry), replaces))


def entry(identity, name="Somebody", platform="linux", linked=True, paired_at=1, token=None):
    return {
        "id": b64(identity), "name": name, "platform": platform, "linked": linked,
        "paired_at": paired_at, "token": token or b64(hashlib.sha256(identity).digest()),
    }


class Rig:
    """A v3 host and requester with their own books, clocks and entropy."""

    def __init__(self, host_book=None, requester_book=None, wall=1_790_000_000, host_entropy=None):
        self.clock = FakeClock()
        self.wall = [wall]
        self.host_book = host_book or Book()
        self.requester_book = requester_book or Book()
        kwargs = {} if host_entropy is None else {"entropy": host_entropy}
        self.host = PairingHost(
            self.clock, name="Host PC", identity=HOST_ID, version=3, platform="windows", port=24820,
            peers=self.host_book.peers, store=self.host_book.store, wall=lambda: self.wall[0], **kwargs,
        )

    def begin(self):
        self.code = self.host.begin()
        return self.code

    def client(self, code=None, qr_secret=None, name="Requester Mac", identity=REQUESTER_ID, platform="macos", port=24820, **kwargs):
        return PairingClient(
            self.host.pair_id, code or self.code, name, identity, version=3, platform=platform, port=port,
            qr_secret=qr_secret, peers=self.requester_book.peers, wall=lambda: self.wall[0], **kwargs,
        )

    def exchange(self, client=None, address="192.168.77.5"):
        """One honest exchange: (client, start, answer, confirm, done, token)."""
        client = client or self.client()
        start = client.start()
        answer = self.host.handle(start, address)
        confirm = client.accept(answer)
        done = self.host.handle(confirm, address)
        return client, start, answer, confirm, done, client.finish(done)


class ConstantTests(unittest.TestCase):
    def test_the_v3_labels_and_numbers(self):
        self.assertEqual(pairing.PAIRING_V3, 3)
        self.assertEqual(pairing.CHANNEL_V3, b"beamer-pair-v3")
        self.assertEqual(pairing.TOKEN_INFO_V3, b"beamer-pair-token-v3")
        self.assertEqual((pairing.ID_BYTES, pairing.QR_SECRET_BYTES, pairing.MAX_PEERS), (16, 16, 32))
        # Version 2 is untouched beside it until the apps' last call to it goes.
        self.assertEqual((pairing.PAIRING_VERSION, pairing.CHANNEL, pairing.TOKEN_INFO), (2, b"beamer-pair-v2", b"beamer-pair-token-v2"))

    def test_beacon_and_start_say_version_3(self):
        rig = Rig()
        rig.begin()
        self.assertEqual(rig.host.beacon(24820)["pairing"], 3)
        self.assertEqual(rig.client().start()["v"], 3)

    def test_the_exchange_matches_the_specification_computed_by_hand(self):
        """Every v3 input, built here from WIRE.md section 6 and PAIRING.md, gives the token both
        sides agree: CI, the token's info, ADa and ADb with platform and port, and the QR's
        22-byte password with its 95 bytes of padding."""
        for qr in (False, True):
            with self.subTest(qr=qr):
                secret = bytes(range(50, 66))
                rig = Rig(host_entropy=scripted(bytes(range(16)), secret, bytes([7]) * 32))
                code = rig.begin()
                nonce, scalar_a = bytes(range(30, 46)), bytes([9]) * 32
                client = rig.client(qr_secret=secret if qr else None, entropy=scripted(nonce, scalar_a))
                _client, start, answer, _confirm, _done, token = rig.exchange(client)
                self.assertEqual(start.get("qr"), 1 if qr else None)

                prs = code.encode() + (secret if qr else b"")
                self.assertEqual(len(prs), 22 if qr else 6)
                sid = pairing._lv_cat(start["pair"].encode(), nonce)
                padding = bytes(95 if qr else 111)
                generator_string = pairing._lv_cat(b"CPace255", prs, padding, b"beamer-pair-v3", sid)
                r = int.from_bytes(hashlib.sha512(generator_string).digest()[:32], "little") & ((1 << 255) - 1)
                generator = pairing._elligator2(r).to_bytes(32, "little")
                ya = pairing._x25519(scalar_a, generator)
                self.assertEqual(b64(ya), start["share"])
                yb = pairing._unb64(answer["share"])
                k = pairing._x25519(scalar_a, yb)
                ada = pairing._lv_cat(b"requester", REQUESTER_ID, b"Requester Mac", b"macos", (24820).to_bytes(2, "big"))
                adb = pairing._lv_cat(b"host", HOST_ID, b"Host PC", b"windows", (24820).to_bytes(2, "big"))
                isk = hashlib.sha512(pairing._lv_cat(b"CPace255_ISK", sid, k) + pairing._lv_cat(ya, ada) + pairing._lv_cat(yb, adb)).digest()
                self.assertEqual(token, b64(hkdf_block(isk, sid, b"beamer-pair-token-v3")))


class BindingTests(unittest.TestCase):
    def test_platform_or_port_changed_on_the_way_to_the_requester_fails_tb(self):
        for field, value in (("port", 24830), ("platform", "linux")):
            with self.subTest(field=field):
                rig = Rig()
                rig.begin()
                client = rig.client()
                answer = rig.host.handle(client.start(), "192.168.77.5")
                with self.assertRaises(PairingError) as caught:
                    client.accept(dict(answer, **{field: value}))
                self.assertEqual(str(caught.exception), pairing.ERROR_REFUSED)

    def test_platform_or_port_changed_on_the_way_to_the_host_fails_and_nothing_is_stored(self):
        for field, value in (("port", 24830), ("platform", "linux")):
            with self.subTest(field=field):
                rig = Rig()
                rig.begin()
                client = rig.client()
                start = dict(client.start(), **{field: value})
                answer = rig.host.handle(start, "192.168.77.5")
                self.assertTrue(answer["ok"])
                # The requester computed ADa with what it sent, so the host's Tb, over the same
                # transcript, fails first: either way nothing is stored.
                with self.assertRaises(PairingError):
                    client.accept(answer)
                done = rig.host.handle(client.abort(), "192.168.77.5")
                self.assertEqual(done["error"], pairing.ERROR_REFUSED)
                self.assertEqual(rig.host_book.stored, [])

    def test_identities_platforms_and_ports_out_of_shape_are_refused_in_pair_start(self):
        bad = [("id", b64(b"")), ("id", b64(bytes(range(15)))), ("id", b64(bytes(range(17)))),
               ("id", b64(bytes(range(32)))), ("id", b64(bytes(16))), ("id", None),
               ("platform", "MacOS"), ("platform", ""), ("platform", "a" * 17), ("platform", "mac-os"), ("platform", 5), ("platform", None),
               ("port", 70000), ("port", -1), ("port", "24820"), ("port", True), ("port", 1.0), ("port", None),
               ("name", ""), ("name", "x" * 49)]
        for field, value in bad:
            with self.subTest(field=field, value=value):
                rig = Rig()
                rig.begin()
                start = dict(rig.client().start(), **{field: value})
                if value is None:
                    del start[field]
                reply = rig.host.handle(start, "192.168.77.5")
                self.assertEqual(reply, {"type": "pair_answer", "pair": start["pair"], "ok": False, "error": "refused"})
                self.assertFalse(rig.host.active)

    def test_identities_platforms_and_ports_out_of_shape_are_refused_in_pair_answer(self):
        bad = [("id", b64(b"")), ("id", b64(bytes(range(15)))), ("id", b64(bytes(range(17)))),
               ("id", b64(bytes(range(32)))), ("id", b64(bytes(16))),
               ("platform", "Windows"), ("platform", "a" * 17), ("platform", 3),
               ("port", 65536), ("port", False), ("port", 24820.0), ("name", "")]
        for field, value in bad:
            with self.subTest(field=field, value=value):
                rig = Rig()
                rig.begin()
                client = rig.client()
                answer = rig.host.handle(client.start(), "192.168.77.5")
                with self.assertRaises(PairingError) as caught:
                    client.accept(dict(answer, **{field: value}))
                self.assertEqual(str(caught.exception), pairing.ERROR_REFUSED)

    def test_an_unknown_platform_of_the_allowed_shape_pairs(self):
        rig = Rig()
        rig.begin()
        rig.exchange(rig.client(platform="haiku_os2"))
        self.assertEqual(rig.host_book.entries[0]["platform"], "haiku_os2")

    def test_a_v3_side_needs_an_id_of_its_own(self):
        with self.assertRaises(ValueError):
            PairingHost(name="x", identity=b"", version=3, platform="macos", port=1, peers=list, store=lambda e, r: None)
        with self.assertRaises(ValueError):
            PairingClient("00" * 16, "123456", "x", bytes(16), version=3, platform="macos", port=1, peers=list)


class KnownTests(unittest.TestCase):
    def test_the_requester_refuses_a_host_it_already_has_and_tells_the_host(self):
        known = entry(HOST_ID, name="Old PC", paired_at=1_700_000_000)
        rig = Rig(requester_book=Book([known]))
        rig.begin()
        client = rig.client()
        answer = rig.host.handle(client.start(), "192.168.77.5")
        with self.assertRaises(AlreadyPaired) as caught:
            client.accept(answer)
        self.assertEqual(str(caught.exception), pairing.ERROR_KNOWN)
        self.assertEqual((caught.exception.entry["name"], caught.exception.entry["paired_at"]), ("Old PC", 1_700_000_000))
        abort = client.abort()
        self.assertEqual(abort, {"type": "pair_confirm", "pair": rig.host.pair_id, "tag": "", "error": "known"})
        done = rig.host.handle(abort, "192.168.77.5")
        self.assertEqual(done, {"type": "pair_done", "pair": abort["pair"], "ok": False, "error": "refused"})
        self.assertEqual(rig.host.outcome, "known_there")
        self.assertEqual((rig.host_book.stored, rig.requester_book.stored), ([], []))

    def test_the_requester_refuses_its_own_id(self):
        rig = Rig()
        rig.begin()
        client = rig.client(identity=HOST_ID)
        # The host refuses too, but only after Ta; the requester checks Tb first and stops there.
        answer = rig.host.handle(client.start(), "192.168.77.5")
        with self.assertRaises(AlreadyPaired):
            client.accept(answer)

    def test_the_host_refuses_a_requester_it_already_has_after_ta(self):
        known = entry(REQUESTER_ID, name="Old Mac", paired_at=1_700_000_000)
        rig = Rig(host_book=Book([known]))
        rig.begin()
        client = rig.client()
        answer = rig.host.handle(client.start(), "192.168.77.5")
        self.assertTrue(answer["ok"], "the id is proved only by Ta, so it is not judged before")
        done = rig.host.handle(client.accept(answer), "192.168.77.5")
        self.assertEqual(done, {"type": "pair_done", "pair": answer["pair"], "ok": False, "error": "known"})
        self.assertIsNone(rig.host.code)
        self.assertEqual(rig.host.outcome, "known")
        self.assertEqual(rig.host.known["name"], "Old Mac")
        self.assertEqual(rig.host_book.stored, [])
        with self.assertRaises(PairingError) as caught:
            client.finish(done)
        self.assertEqual(str(caught.exception), pairing.ERROR_KNOWN)

    def test_the_host_refuses_its_own_id(self):
        rig = Rig()
        rig.begin()
        client = rig.client(identity=HOST_ID)
        # A requester that skipped its own check still meets the host's.
        client._check_host = lambda identity, name, platform: None
        answer = rig.host.handle(client.start(), "192.168.77.5")
        done = rig.host.handle(client.accept(answer), "192.168.77.5")
        self.assertEqual(done["error"], "known")

    def test_a_lost_pair_done_can_be_paired_again_within_ten_minutes(self):
        rig = Rig()
        rig.begin()
        client = rig.client()
        start = client.start()
        answer = rig.host.handle(start, "192.168.77.5")
        rig.host.handle(client.accept(answer), "192.168.77.5")  # pair_done lost
        self.assertEqual(len(rig.host_book.entries), 1)
        rig.wall[0] += 599
        rig.begin()
        _client, *_rest, token = rig.exchange()
        self.assertEqual(len(rig.host_book.entries), 1)
        self.assertEqual(rig.host_book.entries[0]["token"], token)
        self.assertEqual(rig.host_book.stored[-1][1]["id"], b64(REQUESTER_ID))

    def test_an_unlinked_entry_is_known_after_ten_minutes_or_under_another_name_or_platform(self):
        cases = [
            ("ten minutes", entry(REQUESTER_ID, name="Requester Mac", platform="macos", linked=False, paired_at=1_790_000_000 - 600)),
            ("another name", entry(REQUESTER_ID, name="Other Mac", platform="macos", linked=False, paired_at=1_790_000_000 - 5)),
            ("another platform", entry(REQUESTER_ID, name="Requester Mac", platform="linux", linked=False, paired_at=1_790_000_000 - 5)),
            ("linked", entry(REQUESTER_ID, name="Requester Mac", platform="macos", linked=True, paired_at=1_790_000_000 - 5)),
            ("migrated", entry(REQUESTER_ID, name="Requester Mac", platform="macos", linked=False, paired_at=0)),
        ]
        for label, known in cases:
            with self.subTest(label):
                rig = Rig(host_book=Book([known]))
                rig.begin()
                client = rig.client()
                answer = rig.host.handle(client.start(), "192.168.77.5")
                done = rig.host.handle(client.accept(answer), "192.168.77.5")
                self.assertEqual(done["error"], "known")

    def test_the_requester_replaces_its_own_unlinked_entry_the_same_way(self):
        stale = entry(HOST_ID, name="Host PC", platform="windows", linked=False, paired_at=1_790_000_000 - 30)
        rig = Rig(requester_book=Book([stale]))
        rig.begin()
        client, *_rest, token = rig.exchange()
        self.assertEqual(client.replaces["id"], b64(HOST_ID))
        self.assertEqual(client.entry("192.168.77.4")["token"], token)


class FullTests(unittest.TestCase):
    def test_a_host_with_32_peers_shows_no_code(self):
        rig = Rig(host_book=Book([entry(bytes([n]) * 16) for n in range(1, 33)]))
        with self.assertRaises(PairingError) as caught:
            rig.begin()
        self.assertEqual(str(caught.exception), pairing.ERROR_FULL)
        self.assertIsNone(rig.host.code)

    def test_a_host_that_reaches_32_while_its_code_is_up_answers_full_and_drops_the_code(self):
        book = Book([entry(bytes([n]) * 16) for n in range(1, 32)])
        rig = Rig(host_book=book)
        rig.begin()
        book.entries.append(entry(bytes([40]) * 16))
        start = rig.client().start()
        reply = rig.host.handle(start, "192.168.77.5")
        self.assertEqual(reply, {"type": "pair_answer", "pair": start["pair"], "ok": False, "error": "full"})
        self.assertIsNone(rig.host.code)
        self.assertEqual(rig.host.outcome, "full")

    def test_a_host_filled_between_the_start_and_the_confirmation_stores_nothing(self):
        book = Book([entry(bytes([n]) * 16) for n in range(1, 32)])
        rig = Rig(host_book=book)
        rig.begin()
        client = rig.client()
        answer = rig.host.handle(client.start(), "192.168.77.5")
        self.assertTrue(answer["ok"])
        book.entries.append(entry(bytes([40]) * 16))
        done = rig.host.handle(client.accept(answer), "192.168.77.5")
        self.assertEqual(done["error"], "refused")
        self.assertEqual((rig.host.outcome, rig.host_book.stored), ("full", []))

    def test_a_requester_with_32_peers_does_not_start(self):
        rig = Rig(requester_book=Book([entry(bytes([n]) * 16) for n in range(1, 33)]))
        rig.begin()
        with self.assertRaises(PairingError) as caught:
            rig.client()
        self.assertEqual(str(caught.exception), pairing.ERROR_FULL)


class StrangerTests(unittest.TestCase):
    def test_a_confirmation_from_another_address_is_not_answered_and_ends_nothing(self):
        rig = Rig()
        rig.begin()
        client = rig.client()
        answer = rig.host.handle(client.start(), "192.168.77.5")
        junk = {"type": "pair_confirm", "pair": answer["pair"], "tag": b64(bytes(64))}
        self.assertIsNone(rig.host.handle(junk, "192.168.77.66"))
        self.assertIsNone(rig.host.handle(dict(junk, tag="", error="known"), "192.168.77.66"))
        self.assertTrue(rig.host.active)
        done = rig.host.handle(client.accept(answer), "192.168.77.5")
        self.assertTrue(done["ok"])

    def test_a_confirmation_from_the_same_address_on_another_channel_ends_nothing(self):
        # WIRE.md section 6: over TCP only on its own connection. A forged-source datagram, or a
        # second connection from that address, cannot end the exchange with a bad tag.
        rig = Rig()
        rig.begin()
        client = rig.client()
        answer = rig.host.handle(client.start(), "192.168.77.5", ("tcp", 1))
        junk = {"type": "pair_confirm", "pair": answer["pair"], "tag": b64(bytes(64))}
        for channel in ("udp", ("tcp", 2)):
            self.assertIsNone(rig.host.handle(junk, "192.168.77.5", channel))
        self.assertTrue(rig.host.active)
        done = rig.host.handle(client.accept(answer), "192.168.77.5", ("tcp", 1))
        self.assertTrue(done["ok"])

    def test_a_replayed_start_on_another_channel_is_answered_but_not_fresh(self):
        rig = Rig()
        rig.begin()
        start = rig.client().start()
        first = rig.host.handle(start, "192.168.77.5", ("tcp", 1))
        self.assertTrue(rig.host.fresh_answer)
        rig.host.fresh_answer = False
        self.assertEqual(rig.host.handle(start, "192.168.77.5", ("tcp", 2)), first)
        self.assertFalse(rig.host.fresh_answer)


class VersionTests(unittest.TestCase):
    def test_a_v2_start_to_a_v3_host_is_told_version_and_drops_the_code(self):
        rig = Rig()
        rig.begin()
        start = dict(rig.client().start(), v=2)
        self.assertEqual(rig.host.handle(start, "192.168.77.5")["error"], "version")
        self.assertIsNone(rig.host.code)
        self.assertEqual(rig.host.outcome, "version")

    def test_a_v3_start_to_a_v2_host_is_told_version(self):
        host = PairingHost(FakeClock(), name="Old PC")
        host.begin()
        client = PairingClient(host.pair_id, host.code, "New Mac", REQUESTER_ID, version=3, platform="macos", port=24820, peers=list)
        self.assertEqual(host.handle(client.start())["error"], "version")


def migrated(host="192.168.77.5", name="Old Name"):
    """The entry 1.4.x's one pairing migrated to: no id until its first link, a typed or old token."""
    return {"id": "", "name": name, "platform": "macos", "token": "typed-in-1.4", "host": host, "port": 24820,
            "linked": False, "paired_at": 0, "from_1_4": True}


class MigratedEntryTests(unittest.TestCase):
    """Pairing afresh with the machine held as the 1.4.x entry replaces that entry, so it does not
    linger beside the new one to be answered wrong_id on its first link."""

    def test_the_host_replaces_it_when_the_requester_comes_from_its_address(self):
        rig = Rig(host_book=Book([migrated(host="192.168.77.5")]))
        rig.begin()
        rig.exchange(address="192.168.77.5")
        self.assertEqual([e["id"] for e in rig.host_book.entries], [b64(REQUESTER_ID)])
        self.assertEqual(rig.host_book.stored[-1][1]["from_1_4"], True)

    def test_a_machine_elsewhere_replaces_it_only_by_the_name_and_platform_it_proves(self):
        # The machine's address changed since 1.4.x. A migrated entry never links, so taking it by a
        # name anyone could claim costs nothing that works; left, it would ask to be paired again
        # beside the pairing just made.
        for name, platform, replaced in (("Requester Mac", "macos", True), ("Other Mac", "macos", False),
                                         ("Requester Mac", "windows", False)):
            with self.subTest(name=name, platform=platform):
                old = dict(migrated(host="192.168.77.99", name=name), platform=platform)
                rig = Rig(host_book=Book([old]))
                rig.begin()
                rig.exchange(address="192.168.77.5")
                ids = sorted(e["id"] for e in rig.host_book.entries)
                self.assertEqual(ids, [b64(REQUESTER_ID)] if replaced else ["", b64(REQUESTER_ID)])

    def test_one_an_earlier_beta_linked_is_replaced_by_its_own_id_wherever_it_now_is(self):
        # Betas 1 and 2 let a migrated token link and learn its peer's id. It never links now, so
        # pairing that machine again replaces it rather than refusing it as known.
        linked = dict(migrated(host="192.168.77.99"), id=b64(REQUESTER_ID), linked=True)
        rig = Rig(host_book=Book([linked]))
        rig.begin()
        rig.exchange(address="192.168.77.5")
        self.assertEqual([(e["id"], e["from_1_4"]) for e in rig.host_book.entries], [(b64(REQUESTER_ID), False)])
        self.assertEqual(rig.host_book.stored[-1][1]["from_1_4"], True)

    def test_replacing_it_is_not_refused_by_a_count_reached_during_the_exchange(self):
        book = Book([entry(bytes([n]) * 16) for n in range(1, 31)] + [migrated(host="192.168.77.5")])
        rig = Rig(host_book=book)
        rig.begin()
        client = rig.client()
        answer = rig.host.handle(client.start(), "192.168.77.5")
        book.entries.append(entry(bytes([50]) * 16))
        done = rig.host.handle(client.accept(answer), "192.168.77.5")
        self.assertTrue(done["ok"])
        self.assertEqual(len(book.entries), 32)


class StoreTests(unittest.TestCase):
    def test_the_host_writes_the_entry_before_pair_done_and_both_entries_agree(self):
        rig = Rig()
        rig.begin()
        seen_before_reply = []
        store = rig.host_book.store
        rig.host.store = lambda new, replaces: (seen_before_reply.append(rig.host.code is not None), store(new, replaces))
        client, *_rest, token = rig.exchange(address="192.168.77.5")
        self.assertEqual(seen_before_reply, [True], "stored while the code was still up, before pair_done went")
        host_entry = rig.host_book.entries[0]
        self.assertEqual(host_entry, {
            "id": b64(REQUESTER_ID), "name": "Requester Mac", "platform": "macos", "token": token,
            "host": "192.168.77.5", "port": 24820, "hw": "", "send": True, "allow_drive": True,
            "side": "", "side_set_at": 0, "side_by": "", "paired_with": [], "paired_at": 1_790_000_000,
            "linked": False, "from_1_4": False,
        })
        requester_entry = client.entry("192.168.77.4")
        self.assertEqual(
            {k: requester_entry[k] for k in ("id", "name", "platform", "token", "host", "port", "send", "allow_drive", "linked")},
            {"id": b64(HOST_ID), "name": "Host PC", "platform": "windows", "token": token, "host": "192.168.77.4",
             "port": 24820, "send": True, "allow_drive": True, "linked": False},
        )
        self.assertEqual(rig.host.paired["token"], token)

    def test_a_phone_requester_is_saved_undialled(self):
        rig = Rig()
        rig.begin()
        rig.exchange(rig.client(name="Alex's iPhone", platform="ios", port=0), address="192.168.77.9")
        saved = rig.host_book.entries[0]
        self.assertEqual((saved["host"], saved["port"], saved["send"], saved["allow_drive"]), ("", 0, False, True))

    def test_a_write_that_fails_is_refused_and_nothing_is_kept(self):
        rig = Rig()
        rig.host_book.fail = True
        rig.begin()
        client = rig.client()
        answer = rig.host.handle(client.start(), "192.168.77.5")
        done = rig.host.handle(client.accept(answer), "192.168.77.5")
        self.assertEqual(done, {"type": "pair_done", "pair": answer["pair"], "ok": False, "error": "refused"})
        self.assertEqual(rig.host_book.entries, [])
        self.assertEqual(rig.host.outcome, "not_saved")
        self.assertIsNone(rig.host.paired)
        with self.assertRaises(PairingError):
            client.finish(done)


class KeyIdTests(unittest.TestCase):
    def test_the_key_id_is_the_link_s_and_a_typed_token_has_none(self):
        token = b64(bytes(range(32)))
        expected = hkdf_block(token.encode("ascii"), b"beamer-link-v6", b"beamer-key-id")[:16]
        self.assertEqual(pairing._key_id(token), expected)
        for typed in ("hunter2", b64(bytes(31)), b64(bytes(33)), b64(bytes(32)) + "=", "", None):
            with self.subTest(typed=typed):
                self.assertIsNone(pairing._key_id(typed))

    def test_an_entry_with_a_typed_token_never_clashes(self):
        rig = Rig(host_book=Book([entry(OTHER_ID, token="hunter2")]))
        rig.begin()
        rig.exchange()
        self.assertEqual(len(rig.host_book.stored), 1)

    def _token_of_a_fixed_exchange(self):
        rig = Rig(host_entropy=scripted(bytes(16), bytes([3]) * 16, bytes([7]) * 32))
        rig.begin()
        client = rig.client(entropy=scripted(bytes([4]) * 16, bytes([9]) * 32))
        return rig.code, rig.exchange(client)[-1]

    def _fixed_rig(self, code, host_book=None, requester_book=None):
        rig = Rig(host_book=host_book, requester_book=requester_book, host_entropy=scripted(bytes(16), bytes([3]) * 16, bytes([7]) * 32))
        rig.begin()
        rig.host.code = rig.code = code
        return rig, rig.client(entropy=scripted(bytes([4]) * 16, bytes([9]) * 32))

    def test_the_host_refuses_a_token_whose_key_id_is_already_an_entry_s(self):
        code, token = self._token_of_a_fixed_exchange()
        rig, client = self._fixed_rig(code, host_book=Book([entry(OTHER_ID, token=token)]))
        answer = rig.host.handle(client.start(), "192.168.77.5")
        done = rig.host.handle(client.accept(answer), "192.168.77.5")
        self.assertEqual(done["error"], "refused")
        self.assertEqual(rig.host_book.stored, [])

    def test_the_requester_finds_the_clash_after_td_and_the_host_s_entry_is_replaced_next_time(self):
        code, token = self._token_of_a_fixed_exchange()
        rig, client = self._fixed_rig(code, requester_book=Book([entry(OTHER_ID, token=token)]))
        answer = rig.host.handle(client.start(), "192.168.77.5")
        done = rig.host.handle(client.accept(answer), "192.168.77.5")
        self.assertTrue(done["ok"])
        with self.assertRaises(PairingError) as caught:
            client.finish(done)
        self.assertEqual(str(caught.exception), pairing.ERROR_REFUSED)
        self.assertEqual(len(rig.host_book.entries), 1)
        # Pairing again at once replaces the host's entry that never linked.
        rig.requester_book.entries = []
        rig.host._entropy = secrets.token_bytes
        rig.begin()
        rig.exchange()
        self.assertEqual(len(rig.host_book.entries), 1)
        self.assertIsNotNone(rig.host_book.stored[-1][1])


class QrSecretTests(unittest.TestCase):
    def test_a_qr_exchange_and_a_typed_one_each_agree_a_token(self):
        for qr in (True, False):
            with self.subTest(qr=qr):
                rig = Rig()
                rig.begin()
                client, start, *_rest, token = rig.exchange(rig.client(qr_secret=rig.host.qr_secret if qr else None))
                self.assertEqual("qr" in start, qr)
                self.assertEqual(rig.host.paired["token"], token)

    def test_qr_stripped_or_added_on_the_path_fails_and_drops_the_code(self):
        for strip in (True, False):
            with self.subTest(strip=strip):
                rig = Rig()
                rig.begin()
                client = rig.client(qr_secret=rig.host.qr_secret if strip else None)
                start = client.start()
                start = {k: v for k, v in start.items() if k != "qr"} if strip else dict(start, qr=1)
                answer = rig.host.handle(start, "192.168.77.5")
                with self.assertRaises(PairingError):
                    client.accept(answer)
                rig.host.handle(client.abort(), "192.168.77.5")
                self.assertIsNone(rig.host.code)
                self.assertEqual(rig.host_book.stored, [])

    def test_qr_of_any_value_but_the_integer_1_is_malformed(self):
        for value in (True, "1", 1.0, 2, 0, None):
            with self.subTest(value=value):
                rig = Rig()
                rig.begin()
                start = dict(rig.client(qr_secret=rig.host.qr_secret).start(), qr=value)
                self.assertEqual(rig.host.handle(start, "192.168.77.5")["error"], "refused")
                self.assertIsNone(rig.host.code)

    def test_every_code_has_a_new_secret_dropped_with_it(self):
        rig = Rig()
        rig.begin()
        first = rig.host.qr_secret
        rig.begin()
        second = rig.host.qr_secret
        self.assertEqual((len(first), len(second)), (16, 16))
        self.assertNotEqual(first, second)
        rig.host.cancel()
        self.assertIsNone(rig.host.qr_secret)

    def test_a_qr_secret_of_the_wrong_length_is_a_programming_error(self):
        rig = Rig()
        rig.begin()
        with self.assertRaises(ValueError):
            rig.client(qr_secret=bytes(15))


class BeaconTests(unittest.TestCase):
    def test_the_id_and_pairing_id_travel_only_while_a_code_is_up(self):
        rig = Rig()
        idle = rig.host.beacon(24820)
        self.assertEqual(idle, {"type": "beacon", "name": "Host PC", "port": 24820, "pairing": 3, "platform": "windows"})
        rig.begin()
        live = rig.host.beacon(24820)
        self.assertEqual((live["pair"], live["id"]), (rig.host.pair_id, b64(HOST_ID)))
        rig.clock.advance(pairing.CODE_LIFETIME_SECONDS)
        self.assertNotIn("id", rig.host.beacon(24820))


class QrTextTests(unittest.TestCase):
    SECRET = bytes(range(16))
    K = b64(bytes(range(16)))

    def test_the_text_is_built_and_read_back(self):
        text = pairing.qr_text("192.168.1.20", 24821, "048213", self.SECRET)
        self.assertEqual(text, f"beamer://pair?host=192.168.1.20&port=24821&code=048213&k={self.K}")
        self.assertEqual(pairing.read_qr(text), {"host": "192.168.1.20", "port": 24821, "code": "048213", "secret": self.SECRET})

    def test_anything_else_is_refused(self):
        k = self.K
        bad = [
            f"beamer://pair?host=192.168.77.4&port=24821&code=048213",
            f"beamer://pair?host=192.168.77.4&port=24821&k={k}",
            f"beamer://pair?host=192.168.77.4&host=192.168.77.4&port=24821&code=048213&k={k}",
            f"beamer://pair?port=24821&host=192.168.77.4&code=048213&k={k}",
            f"beamer://pair?host=192.168.77.4&port=24821&k={k}&code=048213",
            f"BEAMER://pair?host=192.168.77.4&port=24821&code=048213&k={k}",
            f"beamer://pair?HOST=192.168.77.4&port=24821&code=048213&k={k}",
            f"beamer://pair?host=192.168.77.4&port=024821&code=048213&k={k}",
            f"beamer://pair?host=192.168.77.4&port=65536&code=048213&k={k}",
            f"beamer://pair?host=192.168.77.4&port=0&code=048213&k={k}",
            f"beamer://pair?host=192.168.77.4&port=24821&code=48213&k={k}",
            f"beamer://pair?host=192.168.77.4&port=24821&code=0482133&k={k}",
            f"beamer://pair?host=::1&port=24821&code=048213&k={k}",
            f"beamer://pair?host=[::1]&port=24821&code=048213&k={k}",
            f"beamer://pair?host=10.0.0&port=24821&code=048213&k={k}",
            f"beamer://pair?host=192.168.77.256&port=24821&code=048213&k={k}",
            f"beamer://pair?host=192.168.77.04&port=24821&code=048213&k={k}",
            f"beamer://pair?host=beamer.local&port=24821&code=048213&k={k}",
            f"beamer://pair?host=192.168.77.4&port=24821&code=048213&k={k[:21]}",
            f"beamer://pair?host=192.168.77.4&port=24821&code=048213&k={k}A",
            f"beamer://pair?host=192.168.77.4&port=24821&code=048213&k={k[:21]}B",
            f"beamer://pair?host=192.168.77.4&port=24821&code=048213&k={k}&x=1",
            f"beamer://pair?host=192.168.77.4&port=24821&code=048213&k={k}&",
            f"beamer://pair?host=192.168.77.4&port=24821&code=04821%33&k={k}",
            f"beamer://pair?host=192.168.77.4&port=24821&code=０４８２１３&k={k}",
            f" beamer://pair?host=192.168.77.4&port=24821&code=048213&k={k}",
            f"beamer://pair/?host=192.168.77.4&port=24821&code=048213&k={k}",
            "",
            None,
        ]
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaises(PairingError) as caught:
                    pairing.read_qr(text)
                self.assertEqual(str(caught.exception), pairing.ERROR_BAD_QR)


if __name__ == "__main__":
    unittest.main()
