"""Foreground diagnostics never guess elevation from a refused process query."""

import ctypes
from dataclasses import FrozenInstanceError
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_blocker

LORBY_CLASSES = (
    "HwndWrapper[LorbyAxisAndOhs_MSFS2024.exe;;00000000-0000-4000-8000-000000000001]",
    "HwndWrapper[LorbyAxisAndOhs_MSFS2024.exe;;00000000-0000-4000-8000-000000000002]",
    "HwndWrapper[LorbyAxisAndOhs_MSFS2024.exe;;00000000-0000-4000-8000-000000000003]",
)


class NativeFixture:
    HWND = 0x1234567887654321
    PROCESS = 0x2345678998765432
    TOKEN = 0x3456789AA9876543

    def __init__(self):
        self.user32 = mock.Mock()
        self.kernel32 = mock.Mock()
        self.advapi32 = mock.Mock()
        self.error = 0
        self.elevated = 1
        self.integrity = 0x3000
        self.failures = {}
        self.window_class = "FlightSimulatorWindow"
        self.image = r"C:\Games\PrivateFolder\FlightSimulator.exe"
        self.user32.GetForegroundWindow.return_value = self.HWND
        self.user32.GetClassNameW.side_effect = self.get_class
        self.user32.GetWindowThreadProcessId.side_effect = self.get_pid
        self.kernel32.OpenProcess.side_effect = self.open_process
        self.kernel32.QueryFullProcessImageNameW.side_effect = self.get_image
        self.advapi32.OpenProcessToken.side_effect = self.open_token
        self.advapi32.GetTokenInformation.side_effect = self.get_token_information
        self.advapi32.IsValidSid.return_value = 1
        self.sid_count = ctypes.c_ubyte(1)
        self.sid_rid = input_blocker.DWORD(self.integrity)
        self.advapi32.GetSidSubAuthorityCount.return_value = ctypes.pointer(self.sid_count)
        self.advapi32.GetSidSubAuthority.side_effect = self.get_rid
        self.sid = ctypes.create_string_buffer(12)

    def failed(self, operation):
        if operation in self.failures:
            self.error = self.failures[operation]
            return True
        return False

    def get_class(self, hwnd, buffer, capacity):
        buffer.value = self.window_class
        return len(buffer.value)

    def get_pid(self, hwnd, pid):
        if self.failed("pid"):
            return 0
        ctypes.cast(pid, ctypes.POINTER(input_blocker.DWORD))[0] = 1234
        return 5678

    def open_process(self, access, inherit, pid):
        return 0 if self.failed("process") else self.PROCESS

    def get_image(self, process, flags, buffer, size):
        if self.failed("image"):
            return 0
        buffer.value = self.image
        ctypes.cast(size, ctypes.POINTER(input_blocker.DWORD))[0] = len(self.image)
        return 1

    def open_token(self, process, access, token):
        if self.failed("token"):
            return 0
        ctypes.cast(token, ctypes.POINTER(ctypes.c_void_p))[0] = self.TOKEN
        return 1

    def get_token_information(self, token, kind, buffer, capacity, size):
        size = ctypes.cast(size, ctypes.POINTER(input_blocker.DWORD))
        if kind == 20:
            if self.failed("elevation"):
                return 0
            ctypes.cast(buffer, ctypes.POINTER(input_blocker.DWORD))[0] = self.elevated
            size[0] = 4
            return 1
        if self.failed("integrity"):
            return 0
        size[0] = ctypes.sizeof(input_blocker.TOKEN_MANDATORY_LABEL)
        if buffer is None:
            self.error = 122
            return 0
        if self.failed("integrity_data"):
            return 0
        label = ctypes.cast(buffer, ctypes.POINTER(input_blocker.TOKEN_MANDATORY_LABEL))
        label.contents.Label.Sid = ctypes.addressof(self.sid)
        return 1

    def get_rid(self, sid, index):
        self.sid_rid.value = self.integrity
        return ctypes.pointer(self.sid_rid)


class ForegroundBlockerTests(unittest.TestCase):
    def test_lorby_wpf_classes_name_the_app_and_only_confirmed_elevation(self):
        for window_class in LORBY_CLASSES:
            for elevated in (True, False, None):
                with self.subTest(window_class=window_class, elevated=elevated):
                    self.native.window_class = window_class
                    self.native.image = r"C:\Apps\LorbyAxisAndOhs_MSFS2024.exe"
                    self.native.elevated = bool(elevated)
                    self.native.failures = {"token": 5} if elevated is None else {}
                    result = input_blocker.foreground_blocker()
                    self.assertIn("LorbyAxisAndOhs_MSFS2024.exe", result.message())
                    self.assertNotIn("HwndWrapper", result.message())
                    self.assertEqual("running as administrator" in result.message(), elevated is True)
                    self.assertNotIn("GameInput", result.message())

    def setUp(self):
        self.native = NativeFixture()
        for name in ("user32", "kernel32", "advapi32"):
            patch = mock.patch.object(input_blocker, name, getattr(self.native, name))
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(ctypes, "get_last_error", create=True, side_effect=lambda: self.native.error)
        patch.start()
        self.addCleanup(patch.stop)

    def test_snapshot_preserves_large_handles_and_only_the_process_basename(self):
        result = input_blocker.foreground_blocker()
        self.assertEqual(result.hwnd, 0x1234567887654321)
        self.assertEqual(result.pid, 1234)
        self.assertEqual(result.process_name, "FlightSimulator.exe")
        self.assertEqual(result.key, (1234, 0x1234567887654321, "FlightSimulatorWindow"))
        self.assertIs(result.elevated, True)
        self.assertEqual(result.integrity, 0x3000)
        self.assertIsNone(result.query_error)
        self.native.kernel32.OpenProcess.assert_called_once_with(0x1000, False, 1234)
        self.native.advapi32.OpenProcessToken.assert_called_once()
        self.assertEqual(self.native.advapi32.OpenProcessToken.call_args.args[:2],
                         (0x2345678998765432, 0x0008))
        self.assertEqual([call.args[0] for call in self.native.kernel32.CloseHandle.call_args_list],
                         [0x3456789AA9876543, 0x2345678998765432])
        with self.assertRaises(FrozenInstanceError):
            result.pid = 9

    def test_access_denied_does_not_claim_administrator_or_system(self):
        self.native.failures["process"] = 5
        result = input_blocker.foreground_blocker()
        self.assertIsNone(result.elevated)
        self.assertIsNone(result.integrity)
        self.assertEqual(result.query_error, 5)
        self.assertIn("FlightSimulatorWindow", result.message())
        self.assertNotIn("administrator", result.message())
        self.assertNotIn("system-level", result.message())
        self.native.kernel32.CloseHandle.assert_not_called()

    def test_token_failure_keeps_the_named_process_and_closes_process(self):
        self.native.failures["token"] = 5
        result = input_blocker.foreground_blocker()
        self.assertEqual(result.process_name, "FlightSimulator.exe")
        self.assertIsNone(result.elevated)
        self.assertEqual(result.query_error, 5)
        self.native.kernel32.CloseHandle.assert_called_once_with(0x2345678998765432)

    def test_image_failure_still_checks_the_token_and_uses_class_fallback(self):
        self.native.failures["image"] = 299
        result = input_blocker.foreground_blocker()
        self.assertEqual(result.process_name, "")
        self.assertIs(result.elevated, True)
        self.assertEqual(result.integrity, 0x3000)
        self.assertEqual(result.query_error, 299)
        self.assertIn("FlightSimulatorWindow", result.message())
        self.assertIn("administrator", result.message())

    def test_elevation_failure_does_not_discard_confirmed_system_integrity(self):
        self.native.failures["elevation"] = 5
        self.native.integrity = 0x4000
        result = input_blocker.foreground_blocker()
        self.assertIsNone(result.elevated)
        self.assertEqual(result.integrity, 0x4000)
        self.assertEqual(result.query_error, 5)
        self.assertIn("system-level", result.message())

    def test_integrity_query_failures_close_both_handles_without_guessing(self):
        for failure in ("integrity", "integrity_data"):
            with self.subTest(failure=failure):
                self.native.failures = {failure: 5}
                self.native.kernel32.CloseHandle.reset_mock()
                self.native.elevated = 0
                result = input_blocker.foreground_blocker()
                self.assertIs(result.elevated, False)
                self.assertIsNone(result.integrity)
                self.assertEqual(result.query_error, 5)
                self.assertEqual(self.native.kernel32.CloseHandle.call_count, 2)

    def test_invalid_integrity_sid_stays_unknown(self):
        self.native.advapi32.IsValidSid.return_value = 0
        result = input_blocker.foreground_blocker()
        self.assertIsNone(result.integrity)
        self.assertIsNotNone(result.query_error)
        self.native.advapi32.GetSidSubAuthority.assert_not_called()
        self.assertEqual(self.native.kernel32.CloseHandle.call_count, 2)

    def test_zero_sid_subauthorities_never_reads_a_negative_index(self):
        self.native.sid_count.value = 0
        result = input_blocker.foreground_blocker()
        self.assertIsNone(result.integrity)
        self.native.advapi32.GetSidSubAuthority.assert_not_called()

    def test_native_exception_still_closes_both_handles(self):
        self.native.advapi32.GetTokenInformation.side_effect = OSError(6, "invalid handle")
        result = input_blocker.foreground_blocker()
        self.assertIsNone(result.elevated)
        self.assertEqual(result.query_error, 6)
        self.assertEqual(self.native.kernel32.CloseHandle.call_count, 2)

    def test_process_id_failure_never_opens_an_unrelated_process(self):
        self.native.failures["pid"] = 1400
        result = input_blocker.foreground_blocker()
        self.assertEqual(result.pid, 0)
        self.assertEqual(result.query_error, 1400)
        self.native.kernel32.OpenProcess.assert_not_called()

    def test_missing_foreground_never_queries_a_process(self):
        self.native.user32.GetForegroundWindow.return_value = 0
        result = input_blocker.foreground_blocker()
        self.assertEqual(result.key, (0, 0, ""))
        self.assertIsNone(result.elevated)
        self.native.kernel32.OpenProcess.assert_not_called()

    def test_unavailable_win32_is_safe_unknown(self):
        with mock.patch.object(input_blocker, "user32", None):
            result = input_blocker.foreground_blocker()
        self.assertEqual(result.key, (0, 0, ""))
        self.assertIsNone(result.elevated)
        self.assertIsNone(result.integrity)
        self.assertIsNone(result.query_error)
        self.assertIn("foreground window", result.message())

    def test_messages_distinguish_gameinput_elevated_and_unknown(self):
        cases = [
            ({"window_class": "GameInputServiceWindow"}, "GameInput"),
            ({"process_name": "admin.exe", "elevated": True}, "administrator"),
            ({"process_name": "normal.exe", "elevated": False, "integrity": 0x2000}, "normal.exe"),
            ({"window_class": "ProtectedWindow", "query_error": 5}, "ProtectedWindow"),
        ]
        for values, expected in cases:
            with self.subTest(values=values):
                self.assertIn(expected, input_blocker.ForegroundBlocker(**values).message())

    def test_unknown_message_never_invents_elevation(self):
        message = input_blocker.ForegroundBlocker(process_name="normal.exe", elevated=False,
                                                  integrity=0x2000).message()
        self.assertIn("normal.exe", message)
        self.assertNotIn("administrator", message)
        self.assertNotIn("system-level", message)

    def test_all_messages_translate_before_interpolating_process_name(self):
        blocker = input_blocker.ForegroundBlocker(process_name="PrivateApp.exe", elevated=True)
        seen = []
        with mock.patch.object(input_blocker, "tr", side_effect=lambda template: seen.append(template) or template):
            message = blocker.message()
        self.assertIn("PrivateApp.exe", message)
        self.assertTrue(any("{app}" in template for template in seen))
        self.assertFalse(any("PrivateApp.exe" in template for template in seen))


class NativePrototypeTests(unittest.TestCase):
    def test_handle_and_sid_results_are_pointer_sized_and_dwords_stay_32_bit(self):
        user32, kernel32, advapi32 = mock.Mock(), mock.Mock(), mock.Mock()
        input_blocker._configure_apis(user32, kernel32, advapi32)
        self.assertEqual(ctypes.sizeof(input_blocker.DWORD), 4)
        self.assertIs(user32.GetForegroundWindow.restype, ctypes.c_void_p)
        self.assertEqual(user32.GetForegroundWindow.argtypes, [])
        self.assertIs(kernel32.OpenProcess.restype, ctypes.c_void_p)
        self.assertIs(kernel32.CloseHandle.argtypes[0], ctypes.c_void_p)
        self.assertIs(user32.GetClassNameW.argtypes[0], ctypes.c_void_p)
        self.assertIs(user32.GetWindowThreadProcessId.argtypes[0], ctypes.c_void_p)
        self.assertIs(advapi32.OpenProcessToken.argtypes[0], ctypes.c_void_p)
        self.assertIs(advapi32.OpenProcessToken.argtypes[2]._type_, ctypes.c_void_p)
        self.assertIs(advapi32.GetTokenInformation.argtypes[0], ctypes.c_void_p)
        self.assertEqual(ctypes.sizeof(advapi32.GetSidSubAuthority.restype), ctypes.sizeof(ctypes.c_void_p))
        self.assertEqual(ctypes.sizeof(input_blocker.SID_AND_ATTRIBUTES),
                         16 if ctypes.sizeof(ctypes.c_void_p) == 8 else 8)


if __name__ == "__main__":
    unittest.main()
