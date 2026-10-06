"""The Mac's notices go through UNUserNotificationCenter (beta.4 on three machines: a refused crossing
raised one through rumps' NSUserNotificationCenter, deprecated since macOS 11, and nothing showed;
Beamer was never in Notifications settings). Driven against a stand-in framework, so no test asks
this Mac for anything or puts a notification on screen."""

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import rumps

import notices
from kvm_bridge_app import TrayApp, _set_tray_title, _tray_item
from core import locale


class FakeContent:
    def __init__(self):
        self.title = self.body = None
        self.sound = None

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def setTitle_(self, text):
        self.title = text

    def setBody_(self, text):
        self.body = text

    def setSound_(self, sound):
        self.sound = sound


class FakeRequest:
    @classmethod
    def requestWithIdentifier_content_trigger_(cls, ident, content, trigger):
        request = cls()
        request.ident, request.content, request.trigger = ident, content, trigger
        return request


class FakeCenter:
    def __init__(self):
        self.asked = []
        self.added = []
        self.delegate = None

    def setDelegate_(self, delegate):
        self.delegate = delegate

    def requestAuthorizationWithOptions_completionHandler_(self, options, handler):
        self.asked.append(options)
        handler(True, None)

    def addNotificationRequest_withCompletionHandler_(self, request, handler):
        self.added.append(request)
        if handler is not None:
            handler(None)


class FakeFramework:
    UNAuthorizationOptionAlert = 1 << 2
    UNAuthorizationOptionSound = 1 << 1
    UNNotificationPresentationOptionBanner = 16
    UNNotificationPresentationOptionList = 8
    UNNotificationPresentationOptionSound = 2
    UNMutableNotificationContent = FakeContent
    UNNotificationRequest = FakeRequest

    def __init__(self, center=None, fails=False):
        self.center = center or FakeCenter()
        self.fails = fails

        class Sound:
            @staticmethod
            def defaultSound():
                return "default"

        self.UNNotificationSound = Sound
        framework = self

        class Center:
            @staticmethod
            def currentNotificationCenter():
                if framework.fails:
                    # What the real call raises in a process that is not an app bundle (run from source).
                    raise RuntimeError("bundleProxyForCurrentProcess is nil")
                return framework.center

        self.UNUserNotificationCenter = Center


class NoticesTest(unittest.TestCase):
    def setUp(self):
        self.framework = FakeFramework()
        self.notices = notices.Notices(logging.getLogger("test-notices"), framework=lambda: self.framework)

    def test_asking_requests_alerts_and_sound_once(self):
        self.notices.ask()
        self.notices.ask()
        self.assertEqual(self.framework.center.asked,
                         [FakeFramework.UNAuthorizationOptionAlert | FakeFramework.UNAuthorizationOptionSound])

    def test_a_notice_shows_while_beamer_is_in_front_too(self):
        # macOS keeps a notice from the app in front out of sight unless its delegate asks to show it,
        # and Beamer's window is usually in front when a crossing is refused from its own page.
        self.notices.ask()
        shown = []
        self.framework.center.delegate.userNotificationCenter_willPresentNotification_withCompletionHandler_(
            self.framework.center, None, shown.append)
        self.assertEqual(shown, [FakeFramework.UNNotificationPresentationOptionBanner
                                 | FakeFramework.UNNotificationPresentationOptionList
                                 | FakeFramework.UNNotificationPresentationOptionSound])

    def test_a_notice_is_one_request_with_its_title_body_and_the_default_sound_and_no_trigger(self):
        self.assertTrue(self.notices.post("Can't cross to Laptop-PC", "It is being driven from Desk-PC."))
        self.assertTrue(self.notices.post("Second", "Another."))
        first, second = self.framework.center.added
        self.assertEqual((first.content.title, first.content.body, first.content.sound, first.trigger),
                         ("Can't cross to Laptop-PC", "It is being driven from Desk-PC.", "default", None))
        self.assertNotEqual(first.ident, second.ident)

    def test_us_notice_text_uses_american_spelling(self):
        with mock.patch.object(locale, "is_us_region", return_value=True):
            self.assertTrue(self.notices.post("Cancelled", "Colourful behaviour is a favourite."))
        notice = self.framework.center.added[0].content
        self.assertEqual((notice.title, notice.body),
                         ("Canceled", "Colorful behavior is a favorite."))

    def test_posting_asks_first_when_nothing_has_asked_yet(self):
        self.notices.post("Title", "Body")
        self.assertEqual(len(self.framework.center.asked), 1)

    def test_run_from_source_with_no_bundle_a_notice_is_logged_and_never_raises(self):
        framework = FakeFramework(fails=True)
        quiet = notices.Notices(logging.getLogger("test-notices"), framework=lambda: framework)
        with self.assertLogs("test-notices", level="INFO") as said:
            self.assertFalse(quiet.post("Title", "Body"))
            quiet.ask()
        self.assertTrue(any("Title" in line for line in said.output))

    def test_the_real_framework_is_the_default_and_names_what_the_notices_use(self):
        framework = notices.user_notifications()
        for name in ("UNUserNotificationCenter", "UNMutableNotificationContent", "UNNotificationRequest",
                     "UNNotificationSound", "UNAuthorizationOptionAlert", "UNAuthorizationOptionSound"):
            self.assertTrue(hasattr(framework, name), name)


class _FakeTrayApp:
    _notify_user_main = TrayApp._notify_user_main

    def __init__(self):
        self.logger = logging.getLogger("test-notices-tray")
        self.controller = mock.Mock()
        self.controller.cfg.hide_addresses = True
        self.notices = mock.Mock()


class TrayNoticeTest(unittest.TestCase):
    def test_tray_menu_titles_use_american_spelling(self):
        with mock.patch.object(locale, "is_us_region", return_value=True):
            item = _tray_item("Minimise", callback=None)
            self.assertEqual(item.title, "Minimize")
            _set_tray_title(item, "Initialising")
        self.assertEqual(item.title, "Initializing")

    def test_the_tray_app_posts_through_the_notices_not_rumps_with_addresses_hidden_as_set(self):
        fake = _FakeTrayApp()
        with mock.patch.object(rumps, "notification") as old, mock.patch("AppKit.NSBeep"):
            fake._notify_user_main("Can't cross to Bee", "Bee at 192.0.2.30 refused.")
        old.assert_not_called()
        title, body = fake.notices.post.call_args.args
        self.assertEqual(title, "Can't cross to Bee")
        self.assertNotIn("192.0.2.30", body)


if __name__ == "__main__":
    unittest.main()
