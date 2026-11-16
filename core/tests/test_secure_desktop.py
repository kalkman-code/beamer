"""The privileged listener's authority boundary, with real v6 loopback frames."""

import copy
import os
import socket
import struct
import unittest
from types import SimpleNamespace
from unittest import mock

from core import protocol, receiver, secure_desktop
from core.tests.responder_harness import (
    B, C, HERE, TOKENS, FakeDesktop, FakeInjector, Initiator, entry,
    silent_close, token, wait_for,
)


def grant(peer=B, platform="macos"):
    return {"id": protocol.id_text(peer), "token": TOKENS[peer], "platform": platform,
            "in_use": True, "allow_drive": True}


def payload():
    return {"machine_id": protocol.id_text(HERE), "name": "Secure PC",
            "source_settings": os.path.abspath("synthetic-settings.json"), "peers": [grant()]}


def source():
    return {"machine_id": protocol.id_text(HERE), "peers": [entry(B, "Peer", in_use=True)]}


class GrantPolicyTests(unittest.TestCase):
    def setUp(self):
        self.original = payload()
        self.current = source()

    def policy(self):
        return secure_desktop.GrantPolicy(self.original, lambda: self.current)

    def test_only_the_administrator_snapshot_can_add_a_grant(self):
        policy = self.policy()
        self.current["peers"].append(entry(C, "Unapproved", in_use=True))
        self.assertEqual([p["id"] for p in policy.peers()], [protocol.id_text(B)])
        self.original["peers"].append(grant(C))
        self.original["peers"][0]["token"] = TOKENS[C]
        self.assertEqual(policy.peers(), [grant()])
        result = policy.peers()
        result[0]["allow_drive"] = False
        self.assertEqual(policy.peers(), [grant()])

    def test_removal_rotation_disabling_or_identity_change_revoke(self):
        cases = [
            lambda s: s.update(peers=[]),
            lambda s: s["peers"][0].update(token=token(190)),
            lambda s: s["peers"][0].update(id=protocol.id_text(C)),
            lambda s: s["peers"][0].update(platform="android"),
            lambda s: s["peers"][0].update(in_use=False),
            lambda s: s["peers"][0].update(allow_drive=False),
            lambda s: s["peers"][0].update(in_use=1),
            lambda s: s["peers"][0].update(allow_drive="true"),
            lambda s: s["peers"][0].update(from_1_4=True),
            lambda s: s.update(machine_id=protocol.id_text(C)),
            lambda s: s.update(peers="not a list"),
            lambda s: s["peers"].append(copy.deepcopy(s["peers"][0])),
        ]
        policy = self.policy()
        for mutate in cases:
            with self.subTest(mutate=mutate):
                self.current = source()
                mutate(self.current)
                self.assertEqual(policy.peers(), [])

    def test_unreadable_or_missing_source_fails_closed(self):
        def unreadable():
            raise OSError("synthetic unavailable source")

        self.assertEqual(secure_desktop.GrantPolicy(payload(), unreadable).peers(), [])
        policy = self.policy()
        for current in (None, [], {}, {"machine_id": protocol.id_text(HERE)}):
            self.current = current
            self.assertEqual(policy.peers(), [])

    def test_old_settings_default_in_use_to_true_but_never_allow_drive(self):
        policy = self.policy()
        self.current["peers"][0].pop("in_use")
        self.assertEqual(policy.peers(), [grant()])
        self.current["peers"][0].pop("allow_drive")
        self.assertEqual(policy.peers(), [])

    def test_input_grants_cannot_acquire_sign_in_authority_fields(self):
        for field, value in (("allow_sign_in", True), ("account_sid", "S-1-5-21-123-1001")):
            for target in ("snapshot", "peer"):
                with self.subTest(field=field, target=target):
                    candidate = payload()
                    record = candidate if target == "snapshot" else candidate["peers"][0]
                    record[field] = value
                    with self.assertRaises(ValueError):
                        secure_desktop.GrantPolicy(candidate, source)

    def test_bad_snapshot_is_rejected_before_a_listener_can_start(self):
        mutations = [
            lambda p: p.update(machine_id="wrong"),
            lambda p: p.update(source_settings="relative.json"),
            lambda p: p.update(name=""),
            lambda p: p.update(peers=[]),
            lambda p: p["peers"][0].update(platform="ios"),
            lambda p: p["peers"][0].update(platform="android"),
            lambda p: p["peers"][0].update(platform=[]),
            lambda p: p["peers"][0].update(token="typed password"),
            lambda p: p["peers"][0].update(in_use=1),
            lambda p: p["peers"][0].update(allow_drive="true"),
            lambda p: p["peers"][0].update(id=protocol.id_text(HERE)),
            lambda p: p["peers"][0].update(host="peer-controlled"),
            lambda p: p["peers"].append(grant()),
            lambda p: p["peers"].append({**grant(C), "token": TOKENS[B]}),
        ]
        for mutate in mutations:
            broken = payload()
            mutate(broken)
            with self.subTest(snapshot=broken), self.assertRaises(ValueError):
                secure_desktop.GrantPolicy(broken, source)


class SecureReceiverTests(unittest.TestCase):
    def setUp(self):
        self.current = source()
        self.policy = secure_desktop.GrantPolicy(payload(), lambda: self.current)
        self.injector = FakeInjector()
        self.desktop = FakeDesktop()
        self.is_secure = True
        self.responder = secure_desktop.create_receiver(
            self.policy, injector=self.injector, desktop=self.desktop,
            available=lambda: self.is_secure, status_callback=lambda state, detail: None,
        )
        self.responder.start(0)
        self.addCleanup(self.responder.stop)
        self.assertTrue(wait_for(lambda: self.responder.listening_port is not None))
        self.machine = SimpleNamespace(port=self.responder.listening_port)

    def link(self, peer=B, key=None, **hello):
        link = Initiator(self.machine, peer, key=key)
        self.addCleanup(link.close)
        welcome = link.handshake(link.hello(**hello))
        self.assertTrue(wait_for(lambda: peer in self.responder.links()))
        return link, welcome

    def own(self, link):
        route = link.take()
        self.assertEqual(link.answer(route), ("accept", {"route": route}))
        self.assertEqual(self.responder.owner, B)

    def held(self, link):
        self.own(link)
        self.assertIsNotNone(link.acked(link.key("keydown", "shift")))
        self.assertIn(("key", "shift", True), self.injector.seen())

    def assert_released(self, link):
        self.assertTrue(link.closed.wait(2))
        self.assertTrue(wait_for(lambda: self.responder.owner is None))
        self.assertTrue(wait_for(lambda: ("key", "shift", False) in self.injector.seen()))

    def test_pinned_peer_can_focus_and_send_only_keyboard_mouse_scroll(self):
        link, welcome = self.link()
        self.assertEqual(welcome["caps"], [])
        self.assertTrue(welcome["accepts"])
        self.own(link)
        link.key("keydown", "x")
        link.move(12, -4)
        link.input({"type": "mousedown", "data": {"button": "left"}})
        seq = link.input({"type": "scroll", "data": {"dy": 3, "dx": 0, "mode": "line"}})
        self.assertIsNotNone(link.acked(seq))
        self.assertEqual(self.injector.seen(), [
            ("key", "x", True), ("move", 12, -4), ("button", "left", True), ("scroll", 3, 0, "line"),
        ])

    def test_secure_input_focus_never_calls_the_external_unlock_helper(self):
        unlock = mock.Mock()
        unlock.is_locked.return_value = True
        self.responder._unlock = unlock
        link, _ = self.link()
        self.own(link)
        self.assertTrue(wait_for(lambda: self.responder._arriving_visit is None))
        self.assertIsNotNone(link.acked(link.move(1, 0)))
        unlock.is_locked.assert_not_called()
        unlock.ensure_unlocked.assert_not_called()

    def test_unapproved_or_wrong_key_receives_no_preamble(self):
        for key in (TOKENS[C], token(200)):
            link = Initiator(self.machine, key=key)
            self.addCleanup(link.close)
            self.assertTrue(silent_close(link.sock))
        self.assertEqual(self.responder.links(), set())
        self.assertEqual(self.injector.seen(), [])

    def test_a_pinned_token_cannot_claim_a_different_id_or_phone_platform(self):
        for fields in ({"id": C}, {"platform": "ios"}, {"platform": "android"}, {"platform": "linux"}):
            link = Initiator(self.machine)
            self.addCleanup(link.close)
            with self.assertRaises(protocol.ProtocolError):
                link.handshake(link.hello(**fields))
        self.assertEqual(self.injector.seen(), [])

    def test_remote_metadata_never_mutates_source_or_protected_grants(self):
        before = copy.deepcopy(self.current)
        link, _ = self.link(name="Remote rename", port=30000, hw="aa:bb:cc:dd:ee:ff")
        link.send(protocol.ping_msg())
        self.assertEqual(self.current, before)
        self.assertEqual(self.policy.peers(), [grant()])

    def test_ciphertext_tamper_and_replay_end_ownership_and_release_keys(self):
        for tamper in (False, True):
            with self.subTest(tamper=tamper):
                link, _ = self.link()
                self.held(link)
                frame = link.session.seal(protocol.ping_msg())
                if tamper:
                    frame = frame[:-1] + bytes([frame[-1] ^ 1])
                else:
                    link.sock.sendall(frame)
                link.sock.sendall(frame)
                self.assert_released(link)

    def test_oversized_frame_header_closes_without_waiting_for_body(self):
        link, _ = self.link()
        self.held(link)
        link.sock.sendall(struct.pack(">I", 65536))
        self.assert_released(link)

    def test_each_noninput_message_closes_without_injecting_or_saving(self):
        messages = [
            {"type": "text", "data": {"text": "secret", "seq": 1}},
            {"type": "gesture", "data": {"name": "mission_control", "seq": 1}},
            {"type": "clipboard", "data": {"text": "secret"}},
            {"type": "file", "data": {}},
            {"type": "settings", "data": {"on": True}},
            {"type": "arrangement", "data": {}},
            {"type": "paired", "data": {"ids": [protocol.id_text(C)]}},
            {"type": "sign_in", "data": {"account_sid": "S-1-5-21-123-1001"}},
            {"type": "unlock", "data": {}},
            {"type": "sign_in_approval", "data": {"challenge": "synthetic-attempt"}},
            {"type": "hello", "data": {}},
            {"type": "unknown", "data": {}},
        ]
        before = copy.deepcopy(self.current)
        for message in messages:
            with self.subTest(kind=message["type"]):
                link, _ = self.link()
                self.own(link)
                link.send(message)
                self.assertTrue(link.closed.wait(2))
                self.assertTrue(wait_for(lambda: self.responder.owner is None))
        self.assertEqual(self.current, before)
        self.assertFalse(any(call[0] != "release_all" for call in self.injector.seen()))

    def test_revoked_peer_is_closed_without_needing_new_input(self):
        link, _ = self.link()
        self.held(link)
        self.current["peers"][0]["allow_drive"] = False
        self.assert_released(link)

    def test_revoking_one_peer_keeps_another_approved_peer_available(self):
        self.responder.stop()
        snapshot = payload()
        snapshot["peers"].append(grant(C))
        self.current["peers"].append(entry(C, "Other peer", in_use=True))
        self.policy = secure_desktop.GrantPolicy(snapshot, lambda: self.current)
        self.responder = secure_desktop.create_receiver(
            self.policy, injector=self.injector, desktop=self.desktop,
            available=lambda: self.is_secure, status_callback=lambda state, detail: None,
        )
        self.addCleanup(self.responder.stop)
        self.responder.start(0)
        self.assertTrue(wait_for(lambda: self.responder.listening_port is not None))
        self.machine.port = self.responder.listening_port
        b, _ = self.link()
        c, _ = self.link(C)
        self.held(b)
        self.current["peers"] = [self.current["peers"][1]]
        self.assert_released(b)
        self.assertFalse(c.closed.is_set())
        route = c.take()
        self.assertEqual(c.answer(route), ("accept", {"route": route}))
        self.assertEqual(self.responder.owner, C)
        self.assertIsNotNone(c.acked(c.key("keydown", "x")))
        self.assertIn(("key", "x", True), self.injector.seen())

    def test_rotation_before_input_discards_the_queued_key(self):
        link, _ = self.link()
        self.held(link)
        self.current["peers"][0]["token"] = token(191)
        try:
            link.key("keydown", "z")
        except OSError:
            pass
        self.assert_released(link)
        self.assertNotIn(("key", "z", True), self.injector.seen())

    def test_source_cannot_add_a_new_peer_while_listener_is_running(self):
        self.current["peers"].append(entry(C, "Unapproved", in_use=True))
        link = Initiator(self.machine, C)
        self.addCleanup(link.close)
        self.assertTrue(silent_close(link.sock))

    def test_desktop_unknown_false_or_non_boolean_closes_and_releases(self):
        for state in (False, None, 1, "Winlogon"):
            with self.subTest(state=state):
                self.is_secure = True
                link, _ = self.link()
                self.held(link)
                self.is_secure = state
                self.assert_released(link)
                self.assertNotIn(("key", "z", True), self.injector.seen())

    def test_desktop_transition_before_input_discards_the_queued_key(self):
        link, _ = self.link()
        self.held(link)
        self.is_secure = False
        try:
            link.key("keydown", "z")
        except OSError:
            pass
        self.assert_released(link)
        self.assertNotIn(("key", "z", True), self.injector.seen())

    def test_unreadable_desktop_does_not_accept_a_handshake(self):
        self.is_secure = None
        link = Initiator(self.machine)
        self.addCleanup(link.close)
        self.assertTrue(silent_close(link.sock))

    def test_disconnect_releases_keys_and_buttons(self):
        link, _ = self.link()
        self.held(link)
        seq = link.input({"type": "mousedown", "data": {"button": "left"}})
        self.assertIsNotNone(link.acked(seq))
        link.sock.shutdown(socket.SHUT_RDWR)
        self.assertTrue(wait_for(lambda: self.responder.owner is None))
        self.assertIn(("key", "shift", False), self.injector.seen())
        self.assertIn(("button", "left", False), self.injector.seen())

    def test_let_go_releases_every_secure_input_without_preserving_a_drag(self):
        link, _ = self.link()
        self.held(link)
        seq = link.input({"type": "mousedown", "data": {"button": "left"}})
        self.assertIsNotNone(link.acked(seq))
        with self.responder._lock:
            after = self.responder._end_locked(self.responder._links[B], let_go=True)
        self.responder._run(after)
        self.assertIsNone(self.responder.owner)
        self.assertIn(("key", "shift", False), self.injector.seen())
        self.assertIn(("button", "left", False), self.injector.seen())
        self.assertIn(("release_all",), self.injector.seen())

    def test_injection_failure_closes_and_releases_without_logging_input(self):
        link, _ = self.link()
        self.held(link)
        inject = self.injector.inject_key

        def failing(key, down, us=None):
            if down and key == "z":
                raise OSError("synthetic typed input must never enter logs")
            return inject(key, down, us)

        with mock.patch.object(self.injector, "inject_key", failing), self.assertNoLogs("core.receiver", level="WARNING"):
            link.key("keydown", "z")
            self.assert_released(link)

    def test_release_failure_still_uses_backstop_without_logging_key(self):
        link, _ = self.link()
        self.held(link)

        def failing(key, down, us=None):
            raise OSError("synthetic held key must never enter logs")

        with mock.patch.object(self.injector, "inject_key", failing), self.assertNoLogs("core.receiver", level="WARNING"):
            link.sock.shutdown(socket.SHUT_RDWR)
            self.assertTrue(wait_for(lambda: self.responder.owner is None))
            self.assertTrue(wait_for(lambda: ("release_all",) in self.injector.seen()))


if __name__ == "__main__":
    unittest.main()
