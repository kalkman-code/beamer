"""The Mac's controller and the owner (WIRE.md sections 4 and 5) with the links faked: where input
goes, what is sent to the machine it leaves and the one it takes, the pointer and the alerts each
way home, the key table on the way out, and the clipboard. The links themselves are
core/tests/test_link.py's; the two ends together are test_mac_loopback.py's."""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from bridge import KVMController
from core import protocol
from bridge_fakes import PAIRED_TOKEN, FakeClipboard, FakeClock, FakeQuartz, make_config, quiet_logger, settle
from fake_link import CAPS, OTHER_ID, PEER_ID, FakeLink, bring_up


class Base(unittest.TestCase):
    def setUp(self):
        FakeQuartz.reset_cursor_spies()
        self.clock = FakeClock()
        self.clipboard = FakeClipboard("hello")
        self.controller = KVMController(
            make_config(PAIRED_TOKEN), logger=quiet_logger(), quartz=FakeQuartz, clock=self.clock,
            clipboard=self.clipboard, link_factory=FakeLink, desktop_bounds=lambda: (0, 0, 1920, 1080),
        )
        self.alerts = []
        self.controller.on_user_alert = lambda title, message: self.alerts.append(message)
        self.crossings = []
        self.controller.on_crossing = lambda kind, step: self.crossings.append((kind, step))
        self.link = self.controller._primary_link
        self.own = protocol.id_text(self.controller.identity()["id"])
        self.peer = protocol.id_text(PEER_ID)

    def up(self, **fields):
        return bring_up(self.controller, **fields)

    def focus(self, index=0):
        focuses = [m for m in self.link.posted if m["type"] == protocol.MSG_FOCUS]
        return focuses[index]["data"]


class HardwareHookTests(unittest.TestCase):
    def controller(self, **kwargs):
        return KVMController(
            make_config(PAIRED_TOKEN), logger=quiet_logger(), quartz=FakeQuartz, clock=FakeClock(),
            clipboard=FakeClipboard("hello"), link_factory=FakeLink, desktop_bounds=lambda: (0, 0, 1920, 1080), **kwargs,
        )

    def test_each_link_is_handed_the_controllers_hardware_hook(self):
        hook = lambda host: "aa:bb:cc:dd:ee:ff"
        self.assertIs(self.controller(hardware=hook)._primary_link.hardware, hook)

    def test_no_hook_leaves_the_link_without_one(self):
        self.assertIsNone(self.controller()._primary_link.hardware)

    def test_each_link_reads_a_large_frame_only_while_the_owner_expects_its_clipboard(self):
        controller = self.controller()
        link = controller._primary_link
        link.peer_id = "B"
        now = controller.owner._clock()
        self.assertFalse(link.large(link, now))
        controller.owner._let_go["B"] = now
        self.assertTrue(link.large(link, now))


class RemovedLinksTests(Base):
    def test_a_link_for_a_machine_that_went_is_stopped_without_waiting_for_it(self):
        # Stopping joins the link's thread, which can be in a connect: never on the main thread.
        self.controller.book.data["peers"][0]["send"] = False
        self.controller._sync_links()
        self.assertTrue(self.link.stopped)
        self.assertFalse(self.link.stop_waited)

    def test_a_link_that_goes_down_after_its_machine_went_cannot_bring_input_back_or_report_up(self):
        self.up()
        self.controller.book.data["peers"][0]["send"] = False
        self.controller._sync_links()
        self.controller._link_up(self.link, {"id": PEER_ID, "name": "Late", "accepts": True, "caps": CAPS})
        self.assertNotIn(self.peer, self.controller._peers_up)

    def test_a_link_whose_machine_goes_as_it_comes_up_is_not_left_registered(self):
        controller, link, peer = self.controller, self.link, self.peer

        class RemovesTheMachine(dict):
            # The machine goes between the check that its link is still wanted and the registration.
            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                controller.book.data["peers"][0]["send"] = False
                controller._sync_links()

        controller._up_names = RemovesTheMachine()
        link._live = True
        link.peer_id = peer
        controller._link_up(link, {"id": PEER_ID, "name": "Late", "accepts": True, "caps": CAPS})
        self.assertNotIn(peer, controller._peers_up)
        self.assertNotIn(peer, controller.owner._links)

    def test_stopping_the_controller_still_waits_for_its_links(self):
        self.controller.stop()
        self.assertTrue(self.link.stop_waited)


class TakingAndLeavingTests(Base):
    def test_a_take_sends_the_focus_then_the_clipboard(self):
        self.up()
        self.assertTrue(self.controller.set_redirecting(True, edge="left", offset=0.4))
        settle(self.controller)
        take = self.focus()
        self.assertEqual((take["route"], take["target"], take["edge"], take["offset"]), (1, self.peer, "left", 0.4))
        self.assertEqual(take["resistance_px"], int(self.controller.crossing.resistance_px))
        self.assertEqual(take["reach"], [])
        kinds = [m["type"] for m in self.link.posted if m["type"] in (protocol.MSG_FOCUS, protocol.MSG_CLIPBOARD)]
        self.assertEqual(kinds, [protocol.MSG_FOCUS, protocol.MSG_CLIPBOARD])
        self.assertEqual(self.link.posted[-1]["data"]["text"], "hello")
        self.assertTrue(self.controller.redirecting)
        self.assertEqual(FakeQuartz.associate_calls, [False])

    def test_the_shortcut_takes_with_no_position(self):
        self.up()
        self.controller.set_redirecting(True)
        self.assertNotIn("edge", self.focus())

    def test_input_follows_the_take_to_the_link(self):
        self.up()
        self.controller.set_redirecting(True)
        self.controller._process_outbound({"type": protocol.MSG_MOUSEMOVE, "data": {"dx": 3, "dy": 4}})
        self.assertEqual(self.link.sent, [{"type": protocol.MSG_MOUSEMOVE, "data": {"dx": 3, "dy": 4}}])

    def test_going_home_releases_what_was_pressed_then_lets_go(self):
        self.up()
        self.controller.set_redirecting(True)
        self.controller._process_outbound({"type": protocol.MSG_KEYDOWN, "data": {"key": "a", "us": "a"}})
        self.link.posted.clear()
        self.link.sent.clear()
        self.assertTrue(self.controller.set_redirecting(False))
        self.assertEqual(self.link.sent, [{"type": protocol.MSG_KEYUP, "data": {"key": "a", "us": "a"}}])
        let_go = self.focus()
        self.assertEqual((let_go["route"], let_go["target"]), (2, self.own))
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(FakeQuartz.associate_calls[-1], True)
        self.assertEqual([kind for kind, _ in self.crossings], ["home"])

    def test_nothing_is_taken_when_the_peer_does_not_accept_this_machine(self):
        self.up(accepts=False)
        self.assertFalse(self.controller.set_redirecting(True))
        self.assertFalse(self.controller.redirecting)
        self.assertIn("does not accept input from this Mac", self.alerts[-1])

    def test_nothing_is_taken_while_the_link_is_down(self):
        self.assertFalse(self.controller.set_redirecting(True))
        self.assertTrue(self.alerts)

    def test_sending_off_refuses_the_take(self):
        self.up()
        self.controller.cfg.send_to_windows = False
        self.assertFalse(self.controller.set_redirecting(True))
        self.assertEqual(self.link.posted.count(None), 0)
        self.assertEqual(self.focus_count(), 0)

    def focus_count(self):
        return len([m for m in self.link.posted if m["type"] == protocol.MSG_FOCUS])


class ModifierNamesTests(Base):
    def send(self, key, **link):
        self.up(**link)
        self.controller.set_redirecting(True)
        self.link.sent.clear()
        self.controller._process_outbound({"type": protocol.MSG_KEYDOWN, "data": {"key": key}})
        return self.link.sent[-1]["data"]["key"] if self.link.sent else None

    def test_command_goes_to_a_pc_as_control_and_control_as_the_windows_key(self):
        self.assertEqual(self.send("cmd"), "ctrl")
        self.link.sent.clear()
        self.controller._process_outbound({"type": protocol.MSG_KEYDOWN, "data": {"key": "ctrl"}})
        self.assertEqual(self.link.sent[-1]["data"]["key"], "cmd")

    def test_a_mac_peer_gets_every_key_as_itself(self):
        self.assertEqual(self.send("cmd", platform="macos"), "cmd")

    def test_the_positional_style_keeps_each_key_in_place(self):
        self.controller.cfg.key_map = {"alt": "alt", "alt_r": "alt", "ctrl": "ctrl", "ctrl_r": "ctrl", "cmd": "cmd", "cmd_r": "cmd"}
        self.assertEqual(self.send("cmd"), "cmd")

    def test_a_media_key_is_dropped_for_a_peer_that_does_not_act_on_it(self):
        self.assertIsNone(self.send("volume_up", caps=("clipboard",)))

    def test_a_media_key_goes_to_a_peer_that_does(self):
        self.assertEqual(self.send("volume_up"), "volume_up")


class _Gesture:
    def __init__(self, magnification=0.0, delta_x=0.0):
        self.magnification, self.deltaX, self.deltaY, self.phase = magnification, delta_x, 0.0, 0


def _wait(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class SlowPasteboardTests(Base):
    """Reading the pasteboard can take seconds (a large image, or data an app promised lazily), and
    the event tap takes the owner's lock, so the read never holds it; input that follows a take
    still reaches the machine after the take's clipboard (WIRE.md section 5)."""

    def setUp(self):
        super().setUp()
        self.up()
        self.order = []
        post, send = self.link.post, self.link.send_input
        self.link.post = lambda message: self.order.append(message["type"]) or post(message)
        self.link.send_input = lambda message: self.order.append(message["type"]) or send(message)
        self.reading, self.release = threading.Event(), threading.Event()

        # One pasteboard, as clipboard_mac's lock makes it: a write waits for a read in progress.
        board = threading.Lock()
        write = self.clipboard.set_contents

        def slow():
            with board:
                self.reading.set()
                self.release.wait(5)
                return self.clipboard.text, None

        def written(text, image):
            with board:
                return write(text, image)

        self.clipboard.get_contents = slow
        self.clipboard.set_contents = written
        self.addCleanup(self.release.set)

    def test_a_clipboard_written_here_during_the_read_holds_no_lock_the_event_tap_takes(self):
        from core import owner as owner_module
        self.take()
        writer = threading.Thread(target=self.controller._step, daemon=True,
                                  args=(lambda owner: [owner_module.SetClipboard(protocol.clipboard_msg("from there"))],))
        writer.start()
        writer.join(1)
        self.assertFalse(writer.is_alive(), "the write waited for the read")
        self.assertTrue(self.controller._owner_lock.acquire(timeout=1), "the owner's lock is held through the write")
        self.controller._owner_lock.release()
        self.release.set()
        settle(self.controller)
        self.assertEqual(self.clipboard.set_calls, [("from there", None)])

    def take(self):
        taker = threading.Thread(target=self.controller.set_redirecting, args=(True,), daemon=True)
        taker.start()
        self.assertTrue(self.reading.wait(2), "the take never read the pasteboard")
        taker.join(1)
        self.assertFalse(taker.is_alive(), "the take waited for the pasteboard")

    def test_the_read_holds_no_lock_the_event_tap_takes(self):
        self.take()
        self.assertTrue(self.controller._owner_lock.acquire(timeout=1), "the owner's lock is held through the read")
        self.controller._owner_lock.release()

    def test_input_after_the_take_waits_behind_the_clipboard(self):
        self.take()
        self.controller._process_outbound({"type": protocol.MSG_KEYDOWN, "data": {"key": "a"}})
        self.assertNotIn(protocol.MSG_KEYDOWN, self.order)
        self.release.set()
        self.assertTrue(_wait(lambda: protocol.MSG_KEYDOWN in self.order), self.order)
        self.assertLess(self.order.index(protocol.MSG_CLIPBOARD), self.order.index(protocol.MSG_KEYDOWN))

    def test_a_read_asked_for_again_waits_its_turn_behind_a_write_decided_before_it(self):
        from core import owner as owner_module
        self.take()
        arrived = protocol.clipboard_msg("from there")
        key = {"type": protocol.MSG_KEYDOWN, "data": {"key": "v"}}
        decide = lambda actions: threading.Thread(target=self.controller._step, args=(lambda owner: actions,), daemon=True)
        for step in (decide([owner_module.SetClipboard(arrived)]),
                     decide([owner_module.SendClipboard(self.peer), owner_module.Send(self.peer, key)])):
            step.start()
            step.join(1)
        self.release.set()
        settle(self.controller)
        clipboards = [m["data"]["text"] for m in self.link.posted if m["type"] == protocol.MSG_CLIPBOARD]
        self.assertEqual(clipboards, ["hello", "from there"])
        self.assertEqual(self.order[-3:], [protocol.MSG_CLIPBOARD, protocol.MSG_CLIPBOARD, protocol.MSG_KEYDOWN])

    def test_a_link_lost_during_the_read_drops_what_waited_and_a_retake_sends_the_clipboard_after_its_focus(self):
        self.take()
        self.controller._process_outbound({"type": protocol.MSG_KEYDOWN, "data": {"key": "a"}})
        self.link.down(self.controller)
        self.assertFalse(self.controller.redirecting)
        self.up()
        self.order.clear()
        threading.Thread(target=self.controller.set_redirecting, args=(True,), daemon=True).start()
        self.assertTrue(_wait(lambda: protocol.MSG_FOCUS in self.order), self.order)
        self.release.set()
        settle(self.controller)
        self.assertEqual(self.order, [protocol.MSG_FOCUS, protocol.MSG_CLIPBOARD])

    def test_stopping_during_the_read_still_lets_the_machine_go_after_it(self):
        self.take()
        threading.Timer(0.2, self.release.set).start()
        self.controller.stop()
        self.assertIn(protocol.MSG_CLIPBOARD, self.order)
        let_go = [m for m in self.link.posted if m["type"] == protocol.MSG_FOCUS][-1]["data"]
        self.assertEqual(let_go["target"], self.own)

    def test_pointer_moves_made_during_the_read_go_one_by_one_as_made(self):
        # Never summed: the far machine's edge feels each move where its pointer is (return_edge).
        self.take()
        for dx, dy in ((3, 1), (4, -2), (5, 0)):
            self.controller._process_outbound({"type": protocol.MSG_MOUSEMOVE, "data": {"dx": dx, "dy": dy}})
        self.release.set()
        settle(self.controller)
        moves = [m["data"] for m in self.link.sent if m["type"] == protocol.MSG_MOUSEMOVE]
        self.assertEqual(moves, [{"dx": 3, "dy": 1}, {"dx": 4, "dy": -2}, {"dx": 5, "dy": 0}])

    def test_input_flows_straight_through_once_the_clipboard_is_sent(self):
        self.take()
        self.release.set()
        self.assertTrue(_wait(lambda: protocol.MSG_CLIPBOARD in self.order))
        self.assertTrue(_wait(lambda: not self.controller._clip_wait))
        self.controller._process_outbound({"type": protocol.MSG_KEYDOWN, "data": {"key": "a"}})
        self.assertEqual(self.order[-1], protocol.MSG_KEYDOWN)


class GestureShapeTests(Base):
    """A pinch or a page swipe goes as the chord the machine it reaches answers to, decided when it
    is sent to that machine, not when it was captured."""

    def gesture(self, event_type, view, **link):
        self.up(**link)
        self.controller.set_redirecting(True)
        self.link.sent.clear()
        self.controller.handle_overlay_gesture(event_type, view)
        while not self.controller.outbound.empty():
            self.controller._process_outbound(self.controller.outbound.get_nowait())
        return [(m["type"], m["data"].get("key")) for m in self.link.sent]

    def test_a_page_swipe_to_a_mac_is_command_bracket(self):
        self.assertEqual(self.gesture(31, _Gesture(delta_x=1), platform="macos"), [
            (protocol.MSG_KEYDOWN, "cmd"), (protocol.MSG_KEYDOWN, "["), (protocol.MSG_KEYUP, "["), (protocol.MSG_KEYUP, "cmd")])

    def test_a_page_swipe_to_a_pc_is_alt_arrow(self):
        self.assertEqual(self.gesture(31, _Gesture(delta_x=1)), [
            (protocol.MSG_KEYDOWN, "alt"), (protocol.MSG_KEYDOWN, "left"), (protocol.MSG_KEYUP, "left"), (protocol.MSG_KEYUP, "alt")])

    def test_a_pinch_to_a_mac_is_command_plus_and_never_a_wheel(self):
        sent = self.gesture(30, _Gesture(magnification=0.06), platform="macos")
        self.assertEqual(sent[:2], [(protocol.MSG_KEYDOWN, "cmd"), (protocol.MSG_KEYDOWN, "=")])
        self.assertNotIn(protocol.MSG_SCROLL, [kind for kind, _ in sent])

    def test_a_pinch_to_a_pc_is_control_wheel(self):
        sent = self.gesture(30, _Gesture(magnification=0.06))
        self.assertEqual([kind for kind, _ in sent], [protocol.MSG_KEYDOWN, protocol.MSG_SCROLL, protocol.MSG_KEYUP])
        self.assertEqual(sent[0][1], "ctrl")


class GestureThroughAHandOverTests(Base):
    """A gesture made while the PC hands the input on to a Mac is held with the rest of the input and
    goes to the Mac as the Mac's chord: the chord is chosen when it is sent, not when it was made."""

    def setUp(self):
        super().setUp()
        self.up()
        with self.controller.book.lock:
            peers = self.controller.book.data["peers"]
            peers.append({**peers[0], "id": protocol.id_text(OTHER_ID), "token": "second", "send": True})
        self.controller._sync_links()
        self.mac = self.controller.links["second"]
        self.mac.up(self.controller, ident=OTHER_ID, name="Laptop", platform="macos")
        self.controller.set_redirecting(True)
        self.link.arrives(self.controller, protocol.accept_msg(1))
        self.link.arrives(self.controller, protocol.switch_v6(1, OTHER_ID))
        self.link.sent.clear()

    def pinch(self):
        self.controller.handle_overlay_gesture(30, _Gesture(magnification=0.06))
        while not self.controller.outbound.empty():
            self.controller._process_outbound(self.controller.outbound.get_nowait())

    def test_the_pinch_waits_for_the_mac_and_reaches_it_as_command_plus(self):
        self.pinch()
        self.assertEqual(self.mac.sent, [])
        self.mac.arrives(self.controller, protocol.accept_msg(self.controller.owner.route))
        settle(self.controller)
        self.assertEqual([(m["type"], m["data"].get("key")) for m in self.mac.sent], [
            (protocol.MSG_KEYDOWN, "cmd"), (protocol.MSG_KEYDOWN, "="), (protocol.MSG_KEYUP, "="), (protocol.MSG_KEYUP, "cmd")])
        self.assertEqual(self.link.sent, [])


class AbandonedHandOverTests(Base):
    """A hand-over given up before its answer closes the link to the machine asked, whose accept may
    still be on the way (WIRE.md section 5); the owner's tests cover every way it is given up."""

    def test_the_shortcut_home_while_the_next_machine_is_asked_closes_its_link(self):
        self.up()
        with self.controller.book.lock:
            peers = self.controller.book.data["peers"]
            peers.append({**peers[0], "id": protocol.id_text(OTHER_ID), "token": "second", "send": True})
        self.controller._sync_links()
        mac = self.controller.links["second"]
        mac.up(self.controller, ident=OTHER_ID, name="Laptop", platform="macos")
        self.controller.set_redirecting(True)
        self.link.arrives(self.controller, protocol.accept_msg(1))
        self.link.arrives(self.controller, protocol.switch_v6(1, OTHER_ID))
        self.assertEqual(mac.dropped, [])
        self.controller.set_redirecting(False)
        settle(self.controller)
        self.assertEqual(len(mac.dropped), 1)
        self.assertEqual(self.link.dropped, [])
        self.assertFalse(self.controller.redirecting)


class ChordCutShortTests(Base):
    def test_a_link_whose_queue_fills_mid_chord_is_dropped_rather_than_left_holding_a_modifier(self):
        self.up(platform="macos")
        self.controller.set_redirecting(True)
        self.link.sent.clear()
        send = self.link.send_input
        self.link.send_input = lambda message: not self.link.sent and send(message)
        self.controller.handle_overlay_gesture(31, _Gesture(delta_x=1))
        while not self.controller.outbound.empty():
            self.controller._process_outbound(self.controller.outbound.get_nowait())
        self.assertEqual([m["data"]["key"] for m in self.link.sent], ["cmd"])
        self.assertEqual(self.link.dropped, ["The outbound queue filled"])


class ComingHomeTests(Base):
    def setUp(self):
        super().setUp()
        self.up()
        self.controller.set_redirecting(True, edge="left", offset=0.5)
        self.link.posted.clear()

    def arrives(self, message):
        self.link.arrives(self.controller, message)

    def test_the_peer_sending_input_home_lands_the_pointer_where_it_left(self):
        self.arrives(protocol.switch_v6(1, protocol.read_id(self.own), "right", 0.25))
        self.assertFalse(self.controller.redirecting)
        self.assertEqual([kind for kind, _ in self.crossings], ["arrive"])
        self.assertEqual(self.crossings[0][1].mac_edge, "right")
        self.assertTrue(FakeQuartz.warp_calls)
        let_go = self.focus()
        self.assertEqual(let_go["target"], self.own)
        self.assertEqual(self.alerts, [])

    def test_a_send_home_without_a_position_still_returns_input_and_shows_the_arrival(self):
        self.arrives(protocol.switch_v6(1, protocol.read_id(self.own)))
        self.assertFalse(self.controller.redirecting)
        self.assertEqual([(kind, step.mac_edge) for kind, step in self.crossings], [("arrive", None)])

    def test_a_refusal_for_the_current_route_sends_input_home_with_the_reason(self):
        self.arrives(protocol.refuse_msg(1, "owned"))
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(self.alerts, ["Office PC is being driven from another machine"])

    def test_a_refusal_for_an_old_route_is_ignored(self):
        self.arrives(protocol.accept_msg(1))
        self.controller.owner.route = 5
        self.arrives(protocol.refuse_msg(1, "busy"))
        self.assertTrue(self.controller.redirecting)

    def test_losing_the_link_brings_input_home(self):
        self.link.down(self.controller)
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(self.alerts, ["Lost the link to Office PC"])
        self.assertEqual(FakeQuartz.associate_calls[-1], True)

    def test_no_answer_within_a_second_brings_input_home(self):
        self.clock.value += 1.2
        self.controller._owner_tick()
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(self.alerts, ["Office PC did not answer"])

    def test_an_accept_keeps_input_there(self):
        self.arrives(protocol.accept_msg(1))
        self.clock.value += 5
        self.controller._owner_tick()
        self.assertTrue(self.controller.redirecting)

    def test_a_stale_switch_is_ignored(self):
        self.arrives(protocol.accept_msg(1))
        self.controller.owner.route = 6
        self.arrives(protocol.switch_v6(5, OTHER_ID))
        self.assertTrue(self.controller.redirecting)


class ClipboardTests(Base):
    def test_a_clipboard_from_the_machine_just_let_go_is_taken_once(self):
        self.up()
        self.controller.set_redirecting(True)
        self.controller.set_redirecting(False)
        self.link.arrives(self.controller, protocol.clipboard_msg("from the pc"), began=self.clock.value)
        self.assertEqual(self.clipboard.set_calls, [("from the pc", None)])
        self.link.arrives(self.controller, protocol.clipboard_msg("again"), began=self.clock.value)
        self.assertEqual(len(self.clipboard.set_calls), 1)

    def test_a_clipboard_from_a_machine_never_taken_is_dropped(self):
        self.up()
        self.link.arrives(self.controller, protocol.clipboard_msg("unasked"), began=self.clock.value)
        self.assertEqual(self.clipboard.set_calls, [])

    def test_a_clipboard_that_began_arriving_too_late_is_dropped(self):
        self.up()
        self.controller.set_redirecting(True)
        self.controller.set_redirecting(False)
        self.link.arrives(self.controller, protocol.clipboard_msg("late"), began=self.clock.value + 11)
        self.assertEqual(self.clipboard.set_calls, [])

    def test_the_clipboard_is_sent_again_only_after_it_changed_here(self):
        self.up()
        self.controller.set_redirecting(True)
        settle(self.controller)
        self.link.arrives(self.controller, protocol.accept_msg(1))
        self.controller.set_redirecting(False)
        self.link.posted.clear()
        self.controller.set_redirecting(True)
        settle(self.controller)
        self.assertNotIn(protocol.MSG_CLIPBOARD, self.link.types())
        self.link.arrives(self.controller, protocol.accept_msg(3))
        self.controller.set_redirecting(False)
        self.clipboard.copy("something new")
        self.link.posted.clear()
        self.controller.set_redirecting(True)
        settle(self.controller)
        self.assertIn(protocol.MSG_CLIPBOARD, self.link.types())

    def test_an_oversized_text_is_skipped_but_the_take_still_goes(self):
        self.clipboard.text = "x" * (protocol.CLIPBOARD_MAX_BYTES + 1)
        self.up()
        self.controller.set_redirecting(True)
        settle(self.controller)
        self.assertNotIn(protocol.MSG_CLIPBOARD, self.link.types())
        self.assertEqual(self.focus_count(), 1)

    def arrivals(self):
        """What the owner is handed when a clipboard arrives from the machine just let go."""
        taken = []
        self.controller.owner.clipboard_arrived = lambda peer, began, message: taken.append(message) or []
        return taken

    def test_a_clipboard_with_a_text_that_is_not_a_string_goes_nowhere(self):
        self.up()
        taken = self.arrivals()
        self.link.arrives(self.controller, {"type": protocol.MSG_CLIPBOARD, "data": {"text": 5}}, began=self.clock.value)
        self.assertEqual(taken, [])

    def test_the_clipboard_goes_before_input_the_same_move_hands_on(self):
        # WIRE.md section 5: on the next machine's accept it gets the clipboard, then the held
        # input, in order. The pasteboard is read on its own thread, and what follows it for the
        # same machine waits for it.
        from core import owner as owner_module
        self.up()
        order = []
        post, send_input = self.link.post, self.link.send_input
        self.link.post = lambda message: order.append(message["type"]) or post(message)
        self.link.send_input = lambda message: order.append(message["type"]) or send_input(message)
        key = {"type": protocol.MSG_KEYDOWN, "data": {"key": "v"}}
        self.controller._step(lambda owner: [owner_module.SendClipboard(self.peer), owner_module.Send(self.peer, key)])
        self.assertTrue(_wait(lambda: len(order) == 2), order)
        self.assertEqual(order, [protocol.MSG_CLIPBOARD, protocol.MSG_KEYDOWN])

    def test_a_clipboard_is_handed_on_as_rebuilt_from_what_was_read(self):
        self.up()
        taken = self.arrivals()
        self.link.arrives(self.controller, {"type": protocol.MSG_CLIPBOARD, "data": {"text": "hi", "z": [{}, {}]}, "extra": 1}, began=self.clock.value)
        self.assertEqual(taken, [protocol.clipboard_msg("hi", None)])

    def focus_count(self):
        return len([m for m in self.link.posted if m["type"] == protocol.MSG_FOCUS])


class AnnouncementTests(Base):
    def test_the_link_up_announces_the_side_the_settings_and_who_else_is_paired(self):
        entry = self.controller.book.peers()[0]
        with self.controller.book.lock:
            self.controller.book.data["peers"][0].update(side="left", side_set_at=7, id=self.peer)
            self.controller.book.data["peers"].append({**entry, "id": protocol.id_text(OTHER_ID), "token": "other", "send": False})
        self.controller.announce = lambda: [protocol.settings_msg({"on": False, "set_at": 3, "by": ""})]
        self.up()
        types = self.link.types()
        self.assertIn(protocol.MSG_ARRANGEMENT, types)
        self.assertIn(protocol.MSG_SETTINGS, types)
        paired = next(m for m in self.link.posted if m["type"] == protocol.MSG_PAIRED)
        self.assertEqual(paired["data"]["ids"], [protocol.id_text(OTHER_ID)])
        arrangement = next(m for m in self.link.posted if m["type"] == protocol.MSG_ARRANGEMENT)
        self.assertEqual((arrangement["data"]["edge"], arrangement["data"]["set_at"]), ("left", 7))

    def test_settings_are_not_announced_to_a_peer_that_does_not_keep_them(self):
        self.controller.announce = lambda: [protocol.settings_msg({"on": False, "set_at": 3, "by": ""})]
        self.up(caps=("clipboard",))
        self.assertNotIn(protocol.MSG_SETTINGS, self.link.types())

    def test_an_arrangement_from_the_peer_reaches_the_app_as_this_macs_edge(self):
        seen = []
        self.controller.on_arrangement = lambda *args: seen.append(args)
        with self.controller.book.lock:
            self.controller.book.data["peers"][0]["id"] = self.peer
        self.up()
        self.link.arrives(self.controller, protocol.arrangement_v6("right", int(time.time()) - 5, PEER_ID))
        self.assertEqual(seen, [(self.peer, "right", seen[0][2], self.peer, None)])

    def test_a_peers_settings_reach_the_app_with_who_sent_them(self):
        seen = []
        self.controller.on_settings = lambda data, peer: seen.append((data, peer))
        self.up()
        self.link.arrives(self.controller, protocol.settings_msg({"on": True, "set_at": 5}))
        self.assertEqual(seen, [({"on": True, "set_at": 5}, self.peer)])

    def test_settings_go_on_to_every_peer_but_the_one_they_came_from(self):
        self.up()
        self.controller.send_settings({"on": True, "set_at": 9}, source=self.peer)
        self.assertNotIn(protocol.MSG_SETTINGS, self.link.types())
        self.controller.send_settings({"on": True, "set_at": 9})
        self.assertIn(protocol.MSG_SETTINGS, self.link.types())

    def test_an_arrangement_made_here_goes_to_the_machine_it_is_for(self):
        self.up()
        self.assertTrue(self.controller.send_arrangement(self.peer, "top", 12))
        self.assertFalse(self.controller.send_arrangement(protocol.id_text(OTHER_ID), "top", 13))
        sent = [m for m in self.link.posted if m["type"] == protocol.MSG_ARRANGEMENT][-1]["data"]
        self.assertEqual((sent["edge"], sent["set_at"], sent["by"]), ("top", 12, self.own))


class DrivenTests(Base):
    def test_a_driven_mac_sends_its_owner_home_then_takes_once_it_has_let_go(self):
        self.up()
        homes = []

        def send_home():
            homes.append(True)
            threading.Timer(0.1, lambda: self.controller.set_receiving(False)).start()
            return True

        self.controller.send_peer_home = send_home
        self.controller.set_receiving(True)
        self.assertTrue(self.controller.set_redirecting(True))
        self.assertEqual(homes, [True])
        self.assertFalse(self.controller.redirecting)
        deadline = time.monotonic() + 2
        while not self.controller.redirecting and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.controller.redirecting)

    def test_a_driven_mac_whose_owner_cannot_be_reached_does_not_take(self):
        self.up()
        self.controller.send_peer_home = lambda: False
        self.controller.set_receiving(True)
        self.assertFalse(self.controller.set_redirecting(True))

    def test_a_take_that_won_the_race_sends_this_macs_input_home(self):
        self.up()
        self.controller.responder = type("Driven", (), {"driven": True})()
        self.assertFalse(self.controller.set_redirecting(True))
        self.assertFalse(self.controller.redirecting)
        self.assertEqual(self.focus_count(), 2)

    def focus_count(self):
        return len([m for m in self.link.posted if m["type"] == protocol.MSG_FOCUS])


class LinkSetTests(Base):
    def test_one_link_per_peer_with_send_on(self):
        self.assertEqual(len(self.controller.links), 1)
        with self.controller.book.lock:
            peers = self.controller.book.data["peers"]
            peers.append({**peers[0], "id": protocol.id_text(OTHER_ID), "token": "second", "send": False})
        self.controller._sync_links()
        self.assertEqual(len(self.controller.links), 1)

    def test_a_new_token_makes_a_new_link_and_ends_the_old(self):
        old = self.link
        self.controller.update_config(make_config("a-different-token"))
        self.assertTrue(old.stopped)
        self.assertIsNot(self.controller._primary_link, old)

    def test_removing_the_peer_ends_its_link_and_brings_input_home(self):
        self.up()
        self.controller.set_redirecting(True)
        with self.controller.book.lock:
            self.controller.book.data["peers"].clear()
        self.controller._sync_links()
        self.assertFalse(self.controller.redirecting)

    def test_the_connection_status_follows_the_primary_link(self):
        self.controller._link_state(self.link, False, "Office PC is unreachable from this machine.")
        self.assertEqual(self.controller.connection_status, "Office PC is unreachable from this machine.")

    def test_a_locked_peer_is_called_unlocking(self):
        self.up()
        self.link.peer_locked = True
        self.assertTrue(self.controller.windows_locked)
        self.assertEqual(self.controller.connection_status, "Unlocking 192.0.2.10…")

    def test_the_round_trip_is_the_upper_median_of_the_last_trips_on_the_machine_input_is_on(self):
        self.up()
        self.assertIsNone(self.controller.round_trip_ms)
        self.controller.set_redirecting(True)
        self.link.samples = [0.010, 0.030, 0.020, 0.040]
        self.assertEqual(self.controller.round_trip_ms, 30)

    def test_the_beacons_are_offered_to_every_link(self):
        beacons = [{"name": "Office PC", "address": "192.0.2.77"}]
        self.controller.discovery = type("Found", (), {"pcs": lambda self: beacons})()
        self.controller._follow_the_peers()
        self.assertEqual(self.link.followed, [beacons])

    def test_a_learnt_address_is_handed_to_the_app(self):
        learnt = []
        self.controller.on_host_learned = learnt.append
        with self.controller.book.lock:
            self.controller.book.data["peers"][0]["host"] = "192.0.2.99"
        self.up()
        self.assertEqual(learnt, ["192.0.2.99"])
        self.assertEqual(self.controller.cfg.host, "192.0.2.99")


class PressedAtHomeTests(Base):
    """A key pressed while the input was home is released at home (WIRE.md section 4)."""

    def tap(self, kind, keycode=0x00, character="a"):
        event = {FakeQuartz.kCGKeyboardEventKeycode: keycode, FakeQuartz.kCGKeyboardEventAutorepeat: 0, "unicode": character}
        returned = self.controller._event_tap_callback(None, kind, event, None)
        return event, returned

    def test_the_release_of_a_key_pressed_at_home_stays_at_home(self):
        self.up()
        event, returned = self.tap(FakeQuartz.kCGEventKeyDown)
        self.assertIs(returned, event)
        self.controller.set_redirecting(True)
        event, returned = self.tap(FakeQuartz.kCGEventKeyUp)
        self.assertIs(returned, event)
        self.assertTrue(self.controller.outbound.empty())

    def test_a_key_pressed_after_the_input_left_is_sent_and_released_there(self):
        self.up()
        self.controller.set_redirecting(True)
        _, down = self.tap(FakeQuartz.kCGEventKeyDown)
        _, up = self.tap(FakeQuartz.kCGEventKeyUp)
        self.assertIsNone(down)
        self.assertIsNone(up)
        self.assertEqual([m["type"] for m in list(self.controller.outbound.queue)], [protocol.MSG_KEYDOWN, protocol.MSG_KEYUP])

    def test_a_key_pressed_and_released_at_home_is_not_remembered(self):
        self.up()
        self.tap(FakeQuartz.kCGEventKeyDown)
        self.tap(FakeQuartz.kCGEventKeyUp)
        self.controller.set_redirecting(True)
        _, down = self.tap(FakeQuartz.kCGEventKeyDown)
        _, up = self.tap(FakeQuartz.kCGEventKeyUp)
        self.assertIsNone(down)
        self.assertIsNone(up)
        self.assertEqual(self.controller.outbound.qsize(), 2)


class GestureNamesTests(Base):
    def test_a_pinch_reaches_a_pc_as_control_and_scroll_not_as_the_windows_key(self):
        from gestures import MAGNIFY_TYPE
        from bridge_fakes import FakeGestureEvent
        self.up()
        self.controller.set_redirecting(True)
        self.controller.handle_overlay_gesture(MAGNIFY_TYPE, FakeGestureEvent(magnification=0.05))
        while not self.controller.outbound.empty():
            self.controller._process_outbound(self.controller.outbound.get_nowait())
        keys = [m["data"]["key"] for m in self.link.sent if m["type"] in (protocol.MSG_KEYDOWN, protocol.MSG_KEYUP)]
        self.assertEqual(keys, ["ctrl", "ctrl"])


if __name__ == "__main__":
    unittest.main()
