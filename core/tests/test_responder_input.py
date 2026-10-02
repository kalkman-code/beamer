"""What the responder does with its owner's input: `ack` and `held_us` (WIRE.md section 9),
`text` (section 10), liveness, and the lock screen, over real sockets on loopback against
LinkResponder."""

import threading
import time
import unittest
from unittest import mock

from core import protocol, receiver
from core.tests import pngs
from core.tests.responder_harness import B, C, TOKENS, Initiator, Machine, entry, wait_for


class Case(unittest.TestCase):
    caps = None

    def setUp(self):
        kwargs = {} if self.caps is None else {"caps": self.caps}
        self.machine = Machine([entry(B, "Bee"), entry(C, "Sea")], **kwargs).start()
        self.addCleanup(self.machine.stop)

    def owner(self, peer=B):
        link = Initiator(self.machine, peer)
        self.addCleanup(link.close)
        link.handshake()
        route = link.take()
        self.assertEqual(link.answer(route)[0], protocol.MSG_ACCEPT)
        return link


class Acks(Case):
    def test_held_us_is_in_every_ack_and_zero_before_any_input(self):
        link = Initiator(self.machine, B)
        self.addCleanup(link.close)
        link.handshake()
        first = link.expect(protocol.MSG_ACK, timeout=1.0)
        self.assertEqual(first, {"seq": 0, "held_us": 0})

    def test_a_heartbeat_that_repeats_a_seq_carries_held_us_zero(self):
        link = self.owner()
        seq = link.key("keydown", "a")
        self.assertIsNotNone(link.acked(seq))
        repeat = link.expect(protocol.MSG_ACK, timeout=1.0)
        self.assertEqual(repeat, {"seq": seq, "held_us": 0})

    def test_a_lone_event_is_acknowledged_at_once(self):
        # The responder's own measure of how long it held the ack says "at once": a coalesced ack
        # holds it ACK_COALESCE_SECONDS. A wall-clock bound on the round trip failed under a build.
        link = self.owner()
        link.expect(protocol.MSG_ACK, timeout=1.0)
        time.sleep(0.05)
        sent = time.monotonic()
        seq = link.key("keydown", "a")
        ack = link.acked(seq)
        self.assertLess(time.monotonic() - sent, 0.5)
        self.assertLess(ack["held_us"], receiver.ACK_COALESCE_SECONDS * 1_000_000)

    def test_a_burst_is_one_ack_at_the_end_of_15_ms(self):
        link = self.owner()
        link.expect(protocol.MSG_ACK, timeout=1.0)
        time.sleep(0.05)
        first = link.key("keydown", "a")
        self.assertIsNotNone(link.acked(first))
        acked_at = time.monotonic()
        for name in "bcd":
            last = link.key("keydown", name)
        last_sent = time.monotonic()
        ack = link.acked(last)
        arrived = time.monotonic()
        self.assertGreater(arrived - acked_at, 0.010)
        # Held for its highest seq, never longer than it was in flight (Windows' timer oversleeps 15 ms).
        self.assertLess(ack["held_us"], (arrived - last_sent) * 1_000_000 + 2000)
        self.assertGreater(ack["held_us"], 5000)
        between = [m["data"]["seq"] for m in link.seen if m["type"] == protocol.MSG_ACK and first < m["data"]["seq"] < last]
        self.assertEqual(between, [])

    def test_the_ack_timing_rule(self):
        # Timed from the last ack: at once when 15 ms have passed, else 15 ms after it; a heartbeat 0.4 s after.
        self.assertEqual(receiver.ack_due(last_ack_at=10.0, pending_since=10.020), 10.020)
        self.assertEqual(receiver.ack_due(last_ack_at=10.0, pending_since=10.005), 10.015)
        self.assertEqual(receiver.ack_due(last_ack_at=10.0, pending_since=None), 10.4)


class Liveness(Case):
    def test_a_large_frame_arriving_slowly_keeps_the_link(self):
        with mock.patch.object(receiver, "LINK_READ_TIMEOUT_SECONDS", 0.5):
            link = self.owner()
            frame = link.session.seal(protocol.clipboard_msg("x" * 200000))
            chunk = len(frame) // 8 + 1
            for start in range(0, len(frame), chunk):
                with link.session.send_lock:
                    link.sock.sendall(frame[start:start + chunk])
                time.sleep(0.2)
            self.assertTrue(wait_for(lambda: self.machine.clipboard.set_calls))
            self.assertFalse(link.closed.is_set())

    def test_silence_ends_the_link(self):
        with mock.patch.object(receiver, "LINK_READ_TIMEOUT_SECONDS", 0.3):
            link = self.owner()
            self.assertTrue(link.closed.wait(2))


class Text(Case):
    def test_text_is_typed_as_characters_with_return_and_tab(self):
        link = self.owner()
        link.input(protocol.text_msg("é😀\na\tb\x01c\x85d\x7f"))
        expected = [("text", "é😀"), ("key", "enter", True), ("key", "enter", False), ("text", "a"),
                    ("key", "tab", True), ("key", "tab", False), ("text", "bcd")]
        self.assertTrue(wait_for(lambda: self.machine.injected() == expected), self.machine.injected())

    def test_text_over_4096_bytes_is_dropped_and_acknowledged(self):
        link = self.owner()
        seq = link.input(protocol.text_msg("é" * 2049))
        self.assertIsNotNone(link.acked(seq))
        self.assertEqual(self.machine.injected(), [])

    def test_the_pure_split(self):
        self.assertEqual(receiver.text_runs("a\r\nb\x00\x9f\x80"), ["a", "\n", "b"])
        self.assertEqual(receiver.text_runs("\t\t"), ["\t", "\t"])
        self.assertEqual(receiver.text_runs(""), [])


class WithoutCapabilities(Case):
    caps = ["clipboard"]

    def test_text_and_gestures_are_dropped_and_acknowledged(self):
        link = self.owner()
        link.input(protocol.text_msg("hello"))
        seq = link.input(protocol.gesture_msg("pinch"))
        self.assertIsNotNone(link.acked(seq))
        self.assertEqual(self.machine.injected(), [])

    def test_an_image_is_dropped_and_the_text_kept(self):
        link = self.owner()
        link.send(protocol.clipboard_msg("words", pngs.png()))
        self.assertTrue(wait_for(lambda: self.machine.clipboard.set_calls == [("words", None)]))


class Injecting(Case):
    def test_each_kind_reaches_the_injector(self):
        link = self.owner()
        link.key("keydown", "a")
        link.key("keyup", "a")
        link.move(3, -4)
        link.input({"type": "mousedown", "data": {"button": "right"}})
        link.input({"type": "mouseup", "data": {"button": "right"}})
        link.input(protocol.scroll_msg(2.5, -1, "pixel"))
        seq = link.input(protocol.gesture_msg("swipe_left"))
        self.assertIsNotNone(link.acked(seq))
        self.assertEqual(self.machine.injected(), [
            ("key", "a", True), ("key", "a", False), ("move", 3, -4), ("button", "right", True),
            ("button", "right", False), ("scroll", 2.5, -1, "pixel"), ("gesture", "swipe_left"),
        ])

    def test_a_malformed_input_is_ignored_and_the_link_stays(self):
        link = self.owner()
        link.input({"type": "mousemove", "data": {"dx": 1.5, "dy": 0}})
        seq = link.key("keydown", "b")
        self.assertIsNotNone(link.acked(seq))
        self.assertEqual(self.machine.injected(), [("key", "b", True)])

    def test_the_input_scale_applies(self):
        self.machine.responder.input_scale = receiver.InputScale(pointer=2.0)
        link = self.owner()
        seq = link.move(3, 1)
        self.assertIsNotNone(link.acked(seq))
        self.assertEqual(self.machine.injected(), [("move", 6, 2)])


class LockScreen(Case):
    class Unlock:
        def __init__(self, locked=True, unlocks=True, takes=0.6):
            self.locked, self.unlocks, self.takes = locked, unlocks, takes

        def is_locked(self):
            return self.locked

        def ensure_unlocked(self):
            time.sleep(self.takes)
            if self.unlocks:
                self.locked = False
            return self.unlocks

    def test_a_locked_machine_answers_at_once_and_drops_input_while_it_unlocks(self):
        self.machine.responder._unlock = self.Unlock()
        link = Initiator(self.machine, B)
        self.addCleanup(link.close)
        link.handshake()
        started = time.monotonic()
        route = link.take()
        self.assertEqual(link.answer(route)[0], protocol.MSG_ACCEPT)
        self.assertLess(time.monotonic() - started, 0.3)
        seq = link.key("keydown", "a")
        ack = link.acked(seq)
        self.assertEqual(ack.get("locked"), True)
        self.assertEqual(self.machine.injected(), [])
        time.sleep(0.7)
        seq = link.key("keydown", "b")
        self.assertIsNotNone(link.acked(seq))
        self.assertTrue(wait_for(lambda: self.machine.injected() == [("key", "b", True)]))

    def test_a_machine_that_stays_locked_sends_its_owner_home(self):
        self.machine.responder._unlock = self.Unlock(unlocks=False, takes=0.0)
        link = self.owner()
        switch = link.expect(protocol.MSG_SWITCH, timeout=3.0)
        self.assertEqual(switch["next"], protocol.id_text(B))
        self.assertNotIn("edge", switch)


if __name__ == "__main__":
    unittest.main()
