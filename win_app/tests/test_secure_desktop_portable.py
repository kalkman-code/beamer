"""Secure input planning with inert DLLs and an explicit sink; no desktop input."""
import ctypes
import importlib
import unittest
from unittest.mock import Mock, patch


with patch.object(ctypes, "WinDLL", side_effect=lambda *args, **kwargs: Mock(), create=True):
    secure = importlib.import_module("secure_desktop_win")


class SecureInputTests(unittest.TestCase):
    def setUp(self):
        secure._service_seen[:] = [float("-inf"), None]
        scanner = patch.object(secure.keys, "_map_virtual_key", return_value=29)
        scanner.start()
        self.addCleanup(scanner.stop)

    def test_refused_key_does_not_become_an_owned_modifier(self):
        sink = Mock()
        injector = secure.SecureInjector(bind=lambda: False, send=sink)
        with self.assertRaises(PermissionError):
            injector.inject_key("ctrl", True)
        self.assertEqual(injector._mods, set())
        self.assertEqual(injector._held, {})
        sink.assert_not_called()

    def test_refused_release_preserves_the_still_owned_modifier(self):
        allowed = [True]
        sink = Mock()
        injector = secure.SecureInjector(bind=lambda: allowed[0], send=sink)
        injector.inject_key("ctrl", True)
        allowed[0] = False
        with self.assertRaises(PermissionError):
            injector.inject_key("ctrl", False)
        self.assertEqual(injector._mods, {"ctrl"})
        self.assertIn(("key", "ctrl"), injector._held)
        self.assertEqual(sink.call_count, 1)

    def test_disconnect_releases_owned_input_once_and_lost_desktop_discards_it(self):
        for lost in (False, True):
            with self.subTest(lost=lost):
                calls, allowed = [], [True]
                injector = secure.SecureInjector(bind=lambda: allowed[0], send=calls.extend)
                injector.inject_key("ctrl", True)
                injector.inject_mouse_button("left", True)
                allowed[0] = not lost
                injector.release_all()
                allowed[0] = True
                injector.release_all()
                self.assertEqual(len(calls), 2 if lost else 4)
                self.assertEqual((injector._mods, injector._chars, injector._held), (set(), {}, {}))

    def test_unknown_input_is_ignored_and_fractional_scroll_accumulates(self):
        calls = []
        injector = secure.SecureInjector(bind=lambda: True, send=calls.extend)
        injector.inject_key("run_command", True)
        injector.inject_mouse_button("arbitrary", True)
        self.assertEqual(calls, [])
        injector.inject_scroll(.5)
        injector.inject_scroll(.5)
        self.assertGreater(len(calls), 0)

    def test_desktop_gate_requires_own_console_and_known_winlogon(self):
        for session, console, name, expected in ((1, 1, "Winlogon", True), (0, 0, "Winlogon", False),
                                                 (1, 2, "Winlogon", False), (1, 1, "Default", False),
                                                 (1, 1, None, False), (1, 0xFFFFFFFF, "Winlogon", False)):
            self.assertEqual(secure.secure_desktop_matches(session, console, name), expected)

    def test_service_cache_does_not_cache_desktop_changes(self):
        clock = [100.0]
        with patch.object(secure, "service_running", return_value=True) as service, \
                patch.object(secure, "input_desktop_name", side_effect=["Default", "Winlogon"]):
            self.assertTrue(secure.normal_desktop_available(clock=lambda: clock[0]))
            clock[0] += 1
            self.assertFalse(secure.normal_desktop_available(clock=lambda: clock[0]))
            service.assert_called_once()
        clock[0] += 2
        with patch.object(secure, "service_running", return_value=False), \
                patch.object(secure, "input_desktop_name") as desktop:
            self.assertTrue(secure.normal_desktop_available(clock=lambda: clock[0]))
            desktop.assert_not_called()
