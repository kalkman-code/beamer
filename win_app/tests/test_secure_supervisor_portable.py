"""Exercise SCM policy through inert DLLs; native ABI and identity probes remain Windows-only."""
import ctypes as c
import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from tests import test_secure_service_host as fixture


with patch.object(c, "WinDLL", side_effect=lambda *args, **kwargs: Mock(), create=True), \
        patch.object(c, "WINFUNCTYPE", c.CFUNCTYPE, create=True):
    host = importlib.import_module("secure_service_host")


class PortableBoundary:
    def setUp(self):
        self.enterContext(patch.object(fixture, "host", host))
        self.enterContext(patch.object(fixture, "EXE", Path.cwd() / "synthetic-BeamerSecure.exe"))
        self.enterContext(patch.object(c, "get_last_error", return_value=0, create=True))
        self.enterContext(patch.object(c, "set_last_error", create=True))
        self.enterContext(patch.object(c, "WinError", side_effect=lambda error: OSError(error, "synthetic refusal"), create=True))


class PortableSupervisionTests(PortableBoundary, fixture.SupervisionTests):
    __unittest_skip__ = False


class PortableServiceTests(PortableBoundary, fixture.ServiceTests):
    __unittest_skip__ = False

    @unittest.skipUnless(sys.platform == "win32", "Native Windows ABI and real process identity require Windows")
    def test_native_layouts_and_read_only_identity(self):
        super().test_native_layouts_and_read_only_identity()


class PortableLaunchTests(PortableBoundary, fixture.NativeLaunchTests):
    __unittest_skip__ = False

    def setUp(self):
        PortableBoundary.setUp(self)
        fixture.NativeLaunchTests.setUp(self)

    def create_process(self, token, executable, command, process_security, thread_security,
                       inherit, flags, environment, cwd, startup, child):
        self.assertEqual(executable, str(fixture.EXE))
        self.assertEqual(command.value, f'"{fixture.EXE}" --worker --stop-event "Global\\BeamerSecureStop-' + "a" * 32 + '"')
        self.assertFalse(inherit)
        self.assertEqual(flags, 0x08000004)
        start = c.cast(startup, c.POINTER(host.STARTUPINFOW)).contents
        self.assertEqual(start.lpDesktop, "winsta0\\winlogon")
        self.assertEqual(start.cb, c.sizeof(host.STARTUPINFOW))
        process = c.cast(child, c.POINTER(host.PROCESS_INFORMATION)).contents
        process.hProcess, process.hThread = 400, 401
        process.dwProcessId, process.dwThreadId = 40, 41
        return 1
