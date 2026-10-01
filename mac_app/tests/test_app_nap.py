"""The Mac app is never napped.

On 01-10-2026 macOS napped Beamer from 12:20:18 to 12:43:37 (runningboardd: "Set AppNap state ...
socket:Y cpu:Y timer:Tier5"), and in that window its links to the PC dropped 42 times: a napped
process sends its pings and acks seconds late, and the far side gives each link up after two. A
machine that may be taken at any moment by another holds an activity for as long as it runs."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import AppKit

import kvm_bridge_app


class FakeProcessInfo:
    def __init__(self):
        self.begun = []

    def beginActivityWithOptions_reason_(self, options, reason):
        self.begun.append((options, reason))
        return "token"


class AppNapTests(unittest.TestCase):
    def test_beamer_holds_an_activity_that_is_neither_napped_nor_coalesced(self):
        info = FakeProcessInfo()
        token = kvm_bridge_app.stay_awake(info)
        self.assertEqual(token, "token")
        (options, reason), = info.begun
        self.assertEqual(options & AppKit.NSActivityUserInitiatedAllowingIdleSystemSleep,
                         AppKit.NSActivityUserInitiatedAllowingIdleSystemSleep)
        self.assertEqual(options & AppKit.NSActivityLatencyCritical, AppKit.NSActivityLatencyCritical)
        self.assertTrue(reason)

    def test_it_lets_the_machine_sleep_when_idle(self):
        info = FakeProcessInfo()
        kvm_bridge_app.stay_awake(info)
        options = info.begun[0][0]
        self.assertFalse(options & AppKit.NSActivityIdleSystemSleepDisabled)

    def test_the_real_activity_is_taken(self):
        self.assertIsNotNone(kvm_bridge_app.stay_awake())


if __name__ == "__main__":
    unittest.main()
