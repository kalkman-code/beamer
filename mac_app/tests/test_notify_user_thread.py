import logging
import threading
import unittest
from unittest import mock

import AppKit
from PyObjCTools import AppHelper

from kvm_bridge_app import TrayApp


class _FakeTrayApp:
    """Stand-in with just what TrayApp.notify_user reads from self."""

    _notify_user_main = TrayApp._notify_user_main

    def __init__(self):
        self.logger = logging.getLogger("test-notify-user")
        self.notices = mock.Mock()


class NotifyUserMainThreadTest(unittest.TestCase):
    def test_notify_user_marshals_appkit_calls_to_the_main_thread(self):
        """Every other controller callback in TrayApp (crossing_feedback,
        _arrangement, on_mac_learned) hops to the main thread with
        AppHelper.callAfter before touching AppKit. notify_user is wired
        straight to controller.on_user_alert, which bridge.py's _alert()
        invokes from the event-tap thread (see set_redirecting calls in
        _handle_trigger and the crossing-feed path) -- so it must do the
        same hop instead of calling AppKit.NSBeep()/Notices.post
        directly on the calling thread.
        """
        calling_threads = []

        def record_and_beep():
            calling_threads.append(threading.current_thread())

        def record_and_notify(*_args, **_kwargs):
            calling_threads.append(threading.current_thread())

        call_after_uses = []
        real_call_after = AppHelper.callAfter

        def tracking_call_after(callback, *args, **kwargs):
            call_after_uses.append(callback)
            return real_call_after(callback, *args, **kwargs)

        fake_self = _FakeTrayApp()
        background = threading.Thread(
            target=TrayApp.notify_user,
            args=(fake_self, "Beamer", "background alert"),
        )

        with mock.patch.object(AppKit, "NSBeep", side_effect=record_and_beep), mock.patch.object(
            fake_self.notices, "post", side_effect=record_and_notify
        ), mock.patch.object(AppHelper, "callAfter", side_effect=tracking_call_after):
            background.start()
            background.join(timeout=2)

        self.assertFalse(background.is_alive(), "notify_user did not return")
        self.assertTrue(
            call_after_uses,
            "notify_user must hop to the main thread via AppHelper.callAfter, "
            "like _crossing_feedback_main/_persist_mac/apply_arrangement, before "
            "touching AppKit",
        )
        self.assertEqual(
            calling_threads,
            [],
            "AppKit.NSBeep/Notices.post ran synchronously on the calling "
            "(background) thread instead of being marshalled onto the main thread "
            "via AppHelper.callAfter",
        )


if __name__ == "__main__":
    unittest.main()
