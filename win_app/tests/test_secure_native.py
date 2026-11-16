"""The secure worker's OS boundary, without injecting into a live desktop."""

import ctypes
import sys
import unittest
from unittest.mock import patch

if sys.platform == "win32":
    import secure_desktop_win as secure
else:
    secure = None


@unittest.skipUnless(sys.platform == "win32", "Windows secure desktop native tests")
class DesktopGateTests(unittest.TestCase):
    def setUp(self):
        secure._service_seen[:] = [float("-inf"), None]

    def test_service_state_is_asked_once_per_two_seconds(self):
        """Three Service Control Manager calls per check, twice per input event, was 140 us of a
        receiver's 150 us per event on the rig; the desktop name is still read every time."""
        clock = [100.0]
        with patch.object(secure, "service_running", return_value=True) as service, patch.object(
            secure, "input_desktop_name", side_effect=["Default", "Winlogon", "Default", "Default"]
        ) as desktop:
            self.assertTrue(secure.normal_desktop_available(clock=lambda: clock[0]))
            clock[0] += 1.9
            self.assertFalse(secure.normal_desktop_available(clock=lambda: clock[0]))
            self.assertEqual(service.call_count, 1)
            self.assertEqual(desktop.call_count, 2)
            clock[0] += 0.2
            service.return_value = False
            self.assertTrue(secure.normal_desktop_available(clock=lambda: clock[0]))
            self.assertEqual(service.call_count, 2)
            self.assertEqual(desktop.call_count, 2, "a service known to be absent needs no desktop read")

    def test_requires_own_active_console_and_winlogon(self):
        for current, console, name, expected in (
            (1, 1, "Winlogon", True), (1, 1, "Default", False),
            (1, 2, "Winlogon", False), (0, 0, "Winlogon", False),
            (1, 0xFFFFFFFF, "Winlogon", False), (1, 1, None, False),
        ):
            with self.subTest(current=current, console=console, name=name):
                self.assertEqual(secure.secure_desktop_matches(current, console, name), expected)

    def test_normal_receiver_unchanged_without_running_service(self):
        with patch.object(secure, "service_running", return_value=False), patch.object(
            secure, "input_desktop_name", side_effect=AssertionError("must not inspect")
        ):
            self.assertTrue(secure.normal_desktop_available())

    def test_normal_receiver_fails_closed_with_unknown_desktop(self):
        with patch.object(secure, "service_running", return_value=True):
            for name, expected in (("Default", True), ("Winlogon", False), (None, False)):
                with patch.object(secure, "input_desktop_name", return_value=name):
                    self.assertEqual(secure.normal_desktop_available(), expected)

    def test_unknown_service_and_desktop_state_never_opens_normal_input(self):
        with patch.object(secure, "service_running", return_value=None), patch.object(
            secure, "input_desktop_name", return_value=None
        ):
            self.assertFalse(secure.normal_desktop_available())

    def test_handover_needs_a_running_service_and_an_approved_peer(self):
        from core import protocol
        peer = b"owner-peer-id!!!"
        listed = lambda: frozenset((protocol.id_text(peer),))
        for state, approved, expected in (
            (True, listed, True), (True, lambda: frozenset(), False), (True, lambda: frozenset(("other",)), False),
            (False, listed, False), (None, listed, False),
        ):
            with self.subTest(state=state, expected=expected):
                secure._service_seen[:] = [float("-inf"), None]
                with patch.object(secure, "service_running", return_value=state):
                    self.assertIs(secure.hands_over_to_service(peer, approved=approved), expected)

    def test_approved_peer_list_reads_only_non_empty_text(self):
        self.assertEqual(secure.approved_peer_ids(read=lambda: ["abc", " ", "", 5, " def "]), frozenset(("abc", "def")))

    def test_released_modifier_does_not_affect_next_key(self):
        injector = secure.SecureInjector(bind=lambda: True, send=lambda inputs: None)
        injector.inject_key("ctrl", True)
        injector.inject_key("ctrl", False)
        self.assertEqual(injector._mods, set())

    def test_binding_refusal_never_calls_input_sink(self):
        calls = []
        injector = secure.SecureInjector(bind=lambda: False, send=lambda inputs: calls.extend(inputs))
        with self.assertRaises(PermissionError):
            injector.inject_mouse_move(3, 4)
        self.assertEqual(calls, [])

    def test_buttons_and_keys_are_released_after_disconnect(self):
        calls = []
        injector = secure.SecureInjector(bind=lambda: True, send=lambda inputs: calls.extend(inputs))
        injector.inject_key("ctrl", True)
        injector.inject_mouse_button("left", True)
        injector.release_all()
        self.assertEqual(len(calls), 4)
        self.assertTrue(calls[2].union.ki.dwFlags & 2)
        self.assertEqual(calls[3].union.mi.dwFlags, 4)
        injector.release_all()
        self.assertEqual(len(calls), 4)

    def test_lost_desktop_never_replays_held_input(self):
        calls = []
        allowed = [True]
        injector = secure.SecureInjector(bind=lambda: allowed[0], send=lambda inputs: calls.extend(inputs))
        injector.inject_key("shift", True)
        allowed[0] = False
        injector.release_all()
        allowed[0] = True
        injector.release_all()
        self.assertEqual(len(calls), 1)

    def test_unknown_keys_and_buttons_do_not_send(self):
        calls = []
        injector = secure.SecureInjector(bind=lambda: True, send=lambda inputs: calls.extend(inputs))
        injector.inject_key("run_command", True)
        injector.inject_mouse_button("arbitrary", True)
        self.assertEqual(calls, [])

    def test_native_input_layout_and_read_only_probe(self):
        self.assertEqual(ctypes.sizeof(secure.INPUT), 40)
        self.assertIsInstance(secure.current_session(), int)
        self.assertIsInstance(secure.active_console_session(), int)
        self.assertIn(secure.input_desktop_name(), (None, "Default", "Winlogon", "Disconnect"))


if __name__ == "__main__":
    unittest.main()
