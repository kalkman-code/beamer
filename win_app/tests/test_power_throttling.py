"""The PC's Beamer is never slowed by Windows' power throttling (EcoQoS).

On 01-10-2026 at 11:30:58 and 11:33:00 the Mac's responder found the rig's link quiet while the Mac
was not napped. Beamer lives in the tray with no window in front, which is what Windows' heuristics
throttle, and the rig's Beamer.exe had never said either way (control and state masks both 0). A
throttled process sends its pings and acks late, as the napped Mac did, so Beamer opts out at start,
the counterpart of the Mac's App Nap fix."""

import ctypes
import sys
import unittest
from unittest import mock

PROCESS_POWER_THROTTLING = 4
EXECUTION_SPEED = 0x1


class FakeKernel32:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def GetCurrentProcess(self):
        return -1

    def SetPriorityClass(self, process, priority):
        self.events.append("priority")
        return 1

    def SetProcessInformation(self, process, kind, info, size):
        self.calls.append((process, kind, tuple(info._obj), size))
        self.events.append("unthrottled")
        return 1


class Started(Exception):
    pass


@unittest.skipUnless(sys.platform == "win32", "the app module needs Windows")
class PowerThrottlingTests(unittest.TestCase):
    def test_execution_speed_throttling_is_turned_off(self):
        import kvm_bridge_win

        kernel32 = FakeKernel32([])
        kvm_bridge_win._stay_unthrottled(kernel32)
        (process, kind, (version, control, state), size), = kernel32.calls
        self.assertEqual(process, -1)
        self.assertEqual(kind, PROCESS_POWER_THROTTLING)
        self.assertEqual(version, 1)
        self.assertEqual(control & EXECUTION_SPEED, EXECUTION_SPEED)
        self.assertEqual(state, 0)
        self.assertEqual(size, 12)

    def test_it_is_done_at_start_before_any_window(self):
        import kvm_bridge_win

        events = []
        kernel32 = FakeKernel32(events)

        def window(*args):
            events.append("window")
            raise Started()

        with mock.patch.object(kvm_bridge_win, "_claim_instance", return_value=lambda: None), \
                mock.patch.object(kvm_bridge_win, "parse_args"), \
                mock.patch.object(kvm_bridge_win, "configure_logging", return_value=None), \
                mock.patch.object(kvm_bridge_win, "_windows_kernel32", return_value=kernel32), \
                mock.patch.object(kvm_bridge_win, "QApplication", side_effect=window):
            with self.assertRaises(Started):
                kvm_bridge_win.main()
        self.assertIn("unthrottled", events)
        self.assertEqual(events[-1], "window")

    def test_a_refusal_is_logged_and_start_goes_on(self):
        import kvm_bridge_win

        kernel32 = FakeKernel32([])
        kernel32.SetProcessInformation = lambda *args: 0
        with self.assertLogs(kvm_bridge_win.LOGGER, "WARNING"):
            kvm_bridge_win._stay_unthrottled(kernel32)

    def test_the_real_process_reads_back_unthrottled(self):
        import kvm_bridge_win

        kernel32 = kvm_bridge_win._windows_kernel32()
        kvm_bridge_win._stay_unthrottled(kernel32)
        state = (ctypes.c_uint32 * 3)(1, 0, 0)
        get = ctypes.WinDLL("kernel32", use_last_error=True).GetProcessInformation
        get.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        self.assertTrue(get(kernel32.GetCurrentProcess(), PROCESS_POWER_THROTTLING, ctypes.byref(state), 12),
                        ctypes.WinError(ctypes.get_last_error()))
        self.assertEqual(state[1] & EXECUTION_SPEED, EXECUTION_SPEED)
        self.assertEqual(state[2] & EXECUTION_SPEED, 0)


if __name__ == "__main__":
    unittest.main()
