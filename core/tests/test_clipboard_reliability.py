"""Clipboard retries against transient platform failures, without native GUI imports."""

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from core import protocol, receiver
from core.tests import pngs
from core.tests.responder_harness import FakeClipboard, FakeInjector


class ClipboardReliability(unittest.TestCase):
    def setUp(self):
        self.clipboard = FakeClipboard("before")
        self.sent = []
        self.link = SimpleNamespace(name="Peer", closed=threading.Event(),
                                    caps={"clipboard"}, own_caps={"clipboard"},
                                    held={}, post=self.sent.append)
        self.responder = receiver.LinkResponder(None, lambda: {}, status_callback=lambda *_: None,
                                                clipboard=self.clipboard, injector=FakeInjector())
        self.responder._owner = self.link
        self.responder._visit = 1
        self.responder._clipboard_mark = self.clipboard.stamp

    def receive(self):
        self.responder._set_clipboard(self.link, 1, "peer text", None)

    def return_clipboard(self):
        self.clipboard.copy("copied remotely")
        self.let_go()
        self.responder._clipboard_worker()

    def let_go(self):
        self.responder._clipboard_running = True
        with self.responder._lock:
            after = self.responder._end_locked(self.link, let_go=True)
        self.responder._run(after)

    def test_sharing_off_ignores_incoming_clipboard_and_does_not_return_local_copies(self):
        self.responder.set_clipboard_sharing(False)
        self.link.own_caps.add("clipboard_image")
        with mock.patch.object(self.clipboard, "change_stamp", wraps=self.clipboard.change_stamp) as stamp:
            self.responder._on_clipboard(self.link, protocol.clipboard_msg("incoming", pngs.png()))
            self.return_clipboard()
        stamp.assert_not_called()
        self.assertEqual(self.clipboard.set_calls, [])
        self.assertEqual(self.sent, [])
        self.assertIsNone(self.responder._owner)

    def test_reenabling_sharing_does_not_revive_an_inbound_write_queued_before_disabling(self):
        self.responder._clipboard_running = True
        self.responder._on_clipboard(self.link, protocol.clipboard_msg("stale incoming"))
        self.responder.set_clipboard_sharing(False)
        self.responder.set_clipboard_sharing(True)
        self.responder._clipboard_worker()
        self.assertEqual(self.clipboard.set_calls, [])

        self.responder._clipboard_running = True
        self.responder._on_clipboard(self.link, protocol.clipboard_msg("fresh incoming"))
        self.responder._clipboard_worker()
        self.assertEqual(self.clipboard.set_calls, [("fresh incoming", None)])
        self.return_clipboard()
        self.assertEqual(self.sent, [protocol.clipboard_msg("copied remotely")])

    def test_reenabling_sharing_does_not_apply_clipboard_received_while_off(self):
        self.responder.set_clipboard_sharing(False)
        queued = self.read_ahead("clipboard while off")
        self.responder.set_clipboard_sharing(True)

        self.responder._clipboard_running = True
        for message in queued:
            self.responder._dispatch(self.link, message)
        self.responder._clipboard_worker()

        self.assertEqual(self.clipboard.set_calls, [])

    def test_reenabling_sharing_does_not_revive_a_clipboard_waiting_for_dispatch(self):
        queued = self.read_ahead("clipboard before toggle")
        self.assertEqual(len(queued), 1)
        self.responder.set_clipboard_sharing(False)
        self.responder.set_clipboard_sharing(True)
        queued += self.read_ahead("clipboard after toggle")

        for message in queued:
            self.responder._clipboard_running = True
            self.responder._dispatch(self.link, message)
            self.responder._clipboard_worker()

        self.assertEqual(self.clipboard.set_calls, [("clipboard after toggle", None)])

    def read_ahead(self, text):
        queued = []
        self.link.cond = threading.Condition()
        self.link.sock = None
        self.link.session = SimpleNamespace(open_raw=lambda frame: frame)
        self.link.timing = SimpleNamespace(add=lambda *_: None)
        self.link.receive = lambda message, _: queued.append(message) or True
        frame = protocol.dumps_message(protocol.clipboard_msg(text))
        with mock.patch.object(protocol, "recv_frame", side_effect=[frame, protocol.ConnectionClosed()]):
            self.responder._read_ahead(self.link)
        return queued

    def test_a_home_copy_before_the_queued_return_runs_is_not_sent(self):
        self.clipboard.copy("copied remotely")
        self.let_go()
        self.clipboard.copy("private home copy")
        self.responder._clipboard_worker()
        self.assertEqual(self.sent, [])

    def test_a_home_copy_before_the_return_is_queued_is_not_sent(self):
        self.clipboard.copy("copied remotely")
        self.responder._clipboard_running = True
        with self.responder._lock:
            after = self.responder._end_locked(self.link, let_go=True)
        self.clipboard.copy("private home copy")
        self.responder._run(after)
        self.responder._clipboard_worker()
        self.assertEqual(self.sent, [])

    def test_an_unavailable_let_go_stamp_never_returns_a_clipboard(self):
        self.clipboard.copy("copied remotely")
        with mock.patch.object(self.clipboard, "change_stamp", return_value=None):
            self.let_go()
        self.responder._clipboard_worker()
        self.assertEqual(self.sent, [])

    def test_a_burst_keeps_only_the_newest_pending_write(self):
        self.responder._clipboard_running = True
        for n in range(300):
            self.responder._on_clipboard(self.link, protocol.clipboard_msg(f"copy {n}"))
        self.assertLessEqual(len(self.responder._clipboard_jobs), 1)
        self.responder._clipboard_worker()
        self.assertEqual(self.clipboard.text, "copy 299")

    def test_pending_writes_do_not_coalesce_across_a_return_job(self):
        self.responder._clipboard_running = True
        self.responder._on_clipboard(self.link, protocol.clipboard_msg("peer text"))
        self.let_go()
        self.responder._owner = self.link
        self.responder._on_clipboard(self.link, protocol.clipboard_msg("new peer text"))
        self.assertEqual(len(self.responder._clipboard_jobs), 3)
        self.responder._clipboard_worker()
        self.assertEqual(self.clipboard.set_calls, [("peer text", None), ("new peer text", None)])
        self.assertEqual(self.sent, [])

    def test_a_write_finishing_after_let_go_updates_the_mark_before_return(self):
        original = self.clipboard.set_contents

        def write(*args):
            original(*args)
            self.let_go()
            return True

        with mock.patch.object(self.clipboard, "set_contents", side_effect=write):
            self.receive()
        self.responder._clipboard_worker()
        self.assertEqual(self.clipboard.text, "peer text")
        self.assertEqual(self.sent, [])

    def test_a_contended_inbound_write_recovers_without_another_crossing(self):
        original = self.clipboard.set_contents
        with mock.patch.object(self.clipboard, "set_contents", side_effect=[False, True]) as write:
            write.side_effect = lambda *args: False if write.call_count == 1 else original(*args)
            with mock.patch.object(receiver.time, "sleep"):
                self.receive()
        self.assertEqual(self.clipboard.text, "peer text")

    def test_a_failed_write_does_not_hide_a_local_copy_from_return(self):
        self.clipboard.copy("copied remotely")
        with mock.patch.object(self.clipboard, "set_contents", return_value=False), \
                mock.patch.object(receiver.time, "sleep"):
            self.receive()
        self.let_go()
        self.responder._clipboard_worker()
        self.assertEqual(self.sent, [protocol.clipboard_msg("copied remotely")])

    def test_a_local_copy_cancels_a_contended_inbound_write(self):
        original = self.clipboard.set_contents
        with mock.patch.object(self.clipboard, "set_contents", side_effect=[False, True]) as write:
            write.side_effect = lambda *args: False if write.call_count == 1 else original(*args)
            with mock.patch.object(receiver.time, "sleep", side_effect=lambda _: self.clipboard.copy("new local")):
                self.receive()
        self.assertEqual(self.clipboard.text, "new local")

    def test_a_new_visit_cancels_an_inbound_retry(self):
        original = self.clipboard.set_contents
        with mock.patch.object(self.clipboard, "set_contents", side_effect=[False, True]) as write:
            write.side_effect = lambda *args: False if write.call_count == 1 else original(*args)
            with mock.patch.object(receiver.time, "sleep", side_effect=lambda _: setattr(self.responder, "_visit", 2)):
                self.receive()
        self.assertEqual(self.clipboard.text, "before")

    def test_a_closed_link_cancels_an_inbound_retry(self):
        with mock.patch.object(self.clipboard, "set_contents", return_value=False), \
                mock.patch.object(receiver.time, "sleep", side_effect=lambda _: self.link.closed.set()):
            self.receive()
        self.assertEqual(self.clipboard.text, "before")

    def test_a_write_exception_is_retried(self):
        original = self.clipboard.set_contents
        attempts = iter([False, True])

        def write(*args):
            if not next(attempts):
                raise OSError("clipboard busy")
            return original(*args)

        with mock.patch.object(self.clipboard, "set_contents", side_effect=write), \
                mock.patch.object(receiver.time, "sleep"):
            self.receive()
        self.assertEqual(self.clipboard.text, "peer text")

    def test_a_copy_immediately_after_a_successful_write_is_returned(self):
        original = self.clipboard.set_contents
        write_stamp = []

        def write(*args):
            original(*args)
            write_stamp.append(self.clipboard.stamp)
            self.clipboard.copy("local immediately after write")
            return True

        self.clipboard.write_stamp = lambda: write_stamp[-1]
        with mock.patch.object(self.clipboard, "set_contents", side_effect=write):
            self.receive()
        self.let_go()
        self.responder._clipboard_worker()
        self.assertEqual(self.sent, [protocol.clipboard_msg("local immediately after write")])

    def test_an_empty_return_read_finishes_without_retrying_or_warning(self):
        with mock.patch.object(self.clipboard, "get_contents", return_value=(None, None)) as read, \
                mock.patch.object(receiver.time, "sleep") as sleep, \
                mock.patch.object(receiver.LOGGER, "warning") as warning:
            self.return_clipboard()
        self.assertEqual(self.sent, [])
        self.assertEqual(read.call_count, 1)
        sleep.assert_not_called()
        warning.assert_not_called()

    def test_a_native_failure_uses_the_strict_reader_and_recovers(self):
        self.clipboard.read_contents = mock.Mock(side_effect=[OSError("clipboard busy"), ("copied remotely", None)])
        with mock.patch.object(self.clipboard, "get_contents", return_value=(None, None)), \
                mock.patch.object(receiver.time, "sleep"):
            self.return_clipboard()
        self.assertEqual(self.sent, [protocol.clipboard_msg("copied remotely")])

    def test_a_return_read_exception_is_retried(self):
        original = self.clipboard.get_contents
        reads = iter([False, True])

        def read():
            if not next(reads):
                raise OSError("clipboard busy")
            return original()

        with mock.patch.object(self.clipboard, "get_contents", side_effect=read), \
                mock.patch.object(receiver.time, "sleep"):
            self.return_clipboard()
        self.assertEqual(self.sent, [protocol.clipboard_msg("copied remotely")])

    def test_a_local_copy_after_let_go_is_never_read_for_a_return_retry(self):
        with mock.patch.object(self.clipboard, "get_contents", side_effect=OSError("clipboard busy")), \
                mock.patch.object(receiver.time, "sleep", side_effect=lambda _: self.clipboard.copy("private local")):
            self.return_clipboard()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.clipboard.text, "private local")

    def test_a_new_owner_cancels_a_return_retry(self):
        with mock.patch.object(self.clipboard, "get_contents", side_effect=OSError("clipboard busy")), \
                mock.patch.object(receiver.time, "sleep", side_effect=lambda _: setattr(self.responder, "_owner", self.link)):
            self.return_clipboard()
        self.assertEqual(self.sent, [])

    def test_a_closed_link_cancels_a_return_retry(self):
        with mock.patch.object(self.clipboard, "get_contents", side_effect=OSError("clipboard busy")), \
                mock.patch.object(receiver.time, "sleep", side_effect=lambda _: self.link.closed.set()):
            self.return_clipboard()
        self.assertEqual(self.sent, [])

    def test_a_copy_changing_during_a_return_read_is_not_sent(self):
        def read():
            self.clipboard.copy("private local")
            return self.clipboard.text, None

        with mock.patch.object(self.clipboard, "get_contents", side_effect=read):
            self.return_clipboard()
        self.assertEqual(self.sent, [])

    def test_an_unreadable_start_of_visit_never_returns_a_clipboard(self):
        self.responder._clipboard_mark = None
        self.return_clipboard()
        self.assertEqual(self.sent, [])

    def test_repeated_contention_is_bounded(self):
        with mock.patch.object(self.clipboard, "set_contents", return_value=False) as write, \
                mock.patch.object(receiver.time, "sleep"):
            self.receive()
        self.assertGreater(write.call_count, 1)
        self.assertLessEqual(write.call_count, 25)
