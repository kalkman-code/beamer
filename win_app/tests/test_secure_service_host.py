"""SCM boundary tests: no service installation or real worker creation."""

import ctypes as c
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch
from contextlib import ExitStack

if sys.platform == "win32":
    import secure_service_host as host
else:
    host = None


EXE = Path(r"C:\Program Files\Beamer\BeamerSecure.exe")


@unittest.skipUnless(sys.platform == "win32", "Windows secure desktop service tests")
class NativeLaunchTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.kernel = self.stack.enter_context(patch.object(host, "k32"))
        self.advapi = self.stack.enter_context(patch.object(host, "a32"))
        self.kernel.GetCurrentProcess.return_value = 100
        self.kernel.CreateJobObjectW.return_value = 300
        self.kernel.CreateEventW.return_value = 500
        self.kernel.SetEvent.return_value = 1
        self.kernel.WaitForSingleObject.return_value = 0
        self.kernel.ResumeThread.return_value = 1
        self.kernel.SetInformationJobObject.return_value = 1
        self.kernel.AssignProcessToJobObject.return_value = 1
        self.kernel.TerminateProcess.return_value = 1
        self.kernel.TerminateJobObject.return_value = 1
        self.advapi.AdjustTokenPrivileges.return_value = 1
        self.advapi.LookupPrivilegeValueW.return_value = 1
        self.advapi.SetTokenInformation.return_value = 1
        self.advapi.OpenProcessToken.side_effect = self.open_token
        self.advapi.DuplicateTokenEx.side_effect = self.duplicate_token
        self.advapi.CreateProcessAsUserW.side_effect = self.create_process
        self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.side_effect = self.security_descriptor
        self.stack.enter_context(patch.object(host.uuid, "uuid4", return_value=MagicMock(hex="a" * 32)))

    def security_descriptor(self, sddl, revision, result, length):
        self.assertEqual(sddl, "O:SYG:SYD:P(A;;GA;;;SY)(A;;GA;;;BA)")
        c.cast(result, c.POINTER(c.wintypes.LPVOID))[0] = 900
        return 1

    def open_token(self, process, access, result):
        self.assertEqual(process, 100)
        c.cast(result, c.POINTER(c.wintypes.HANDLE))[0] = 200
        return 1

    def duplicate_token(self, token, access, security, level, kind, result):
        self.assertEqual(token.value, 200)
        self.assertIsNone(security)
        self.assertEqual((level, kind), (2, 1))
        c.cast(result, c.POINTER(c.wintypes.HANDLE))[0] = 201
        return 1

    def create_process(self, token, application, command, process_sa, thread_sa,
                       inherit, flags, environment, directory, startup, info):
        self.assertEqual(token.value, 201)
        self.assertEqual(application, str(EXE))
        self.assertEqual(command.value, '"C:\\Program Files\\Beamer\\BeamerSecure.exe" --worker --stop-event "Global\\BeamerSecureStop-' + "a" * 32 + '"')
        self.assertIsNone(process_sa)
        self.assertIsNone(thread_sa)
        self.assertFalse(inherit)
        self.assertEqual(flags, 0x08000004)
        self.assertIsNone(environment)
        self.assertEqual(directory, str(EXE.parent))
        start = c.cast(startup, c.POINTER(host.STARTUPINFOW)).contents
        self.assertEqual(start.lpDesktop, "winsta0\\winlogon")
        self.assertEqual(start.cb, 104)
        child = c.cast(info, c.POINTER(host.PROCESS_INFORMATION)).contents
        child.hProcess, child.hThread = 400, 401
        child.dwProcessId, child.dwThreadId = 402, 403
        return 1

    def test_launch_uses_own_token_fixed_desktop_and_job_before_resuming(self):
        order = []
        self.kernel.AssignProcessToJobObject.side_effect = lambda *args: order.append("assign") or 1
        self.kernel.ResumeThread.side_effect = lambda *args: order.append("resume") or 1
        worker = host._launch_worker(EXE, 7)
        self.assertEqual(order, ["assign", "resume"])
        privileges = [call.args[1] for call in self.advapi.LookupPrivilegeValueW.call_args_list]
        self.assertEqual(privileges, ["SeTcbPrivilege", "SeAssignPrimaryTokenPrivilege", "SeIncreaseQuotaPrivilege"])
        session_call = self.advapi.SetTokenInformation.call_args.args
        self.assertEqual(session_call[1], 12)
        self.assertEqual(c.cast(session_call[2], c.POINTER(c.wintypes.DWORD))[0], 7)
        job_call = self.kernel.SetInformationJobObject.call_args.args
        self.assertEqual(job_call[1], 9)
        limits = c.cast(job_call[2], c.POINTER(host.JOBOBJECT_EXTENDED_LIMIT_INFORMATION)).contents
        self.assertEqual(limits.BasicLimitInformation.LimitFlags, 0x2000)
        self.assertEqual(worker.process, 400)
        worker.close()
        self.kernel.SetEvent.assert_called_once_with(500)
        self.kernel.TerminateJobObject.assert_not_called()
        closed = [call.args[0] for call in self.kernel.CloseHandle.call_args_list]
        self.assertEqual(closed, [401, 201, 200, 300, 400, 500])

    def test_invalid_console_never_opens_a_token(self):
        for session in (0, 0xFFFFFFFF):
            with self.subTest(session=session), self.assertRaises(ValueError):
                host._launch_worker(EXE, session)
        self.advapi.OpenProcessToken.assert_not_called()

    def test_assignment_failure_terminates_suspended_child_without_resuming(self):
        self.kernel.AssignProcessToJobObject.return_value = 0
        with self.assertRaises(OSError):
            host._launch_worker(EXE, 7)
        self.kernel.ResumeThread.assert_not_called()
        self.kernel.TerminateProcess.assert_called_once_with(400, 1)
        closed = [call.args[0] for call in self.kernel.CloseHandle.call_args_list]
        self.assertCountEqual(closed, [200, 201, 300, 400, 401, 500])

    def test_resume_failure_kills_job_and_closes_all_handles(self):
        self.kernel.ResumeThread.return_value = 0xFFFFFFFF
        with self.assertRaises(OSError):
            host._launch_worker(EXE, 7)
        self.kernel.TerminateJobObject.assert_called_once_with(300, 1)
        self.kernel.TerminateProcess.assert_not_called()
        closed = [call.args[0] for call in self.kernel.CloseHandle.call_args_list]
        self.assertCountEqual(closed, [200, 201, 300, 400, 401, 500])

    def test_privilege_not_assigned_fails_before_process_creation(self):
        with patch.object(host.c, "get_last_error", return_value=1300):
            with self.assertRaises(OSError):
                host._launch_worker(EXE, 7)
        self.advapi.CreateProcessAsUserW.assert_not_called()
        self.kernel.CloseHandle.assert_called_once()

    def test_create_failure_closes_job_and_tokens(self):
        self.advapi.CreateProcessAsUserW.side_effect = None
        self.advapi.CreateProcessAsUserW.return_value = 0
        with self.assertRaises(OSError):
            host._launch_worker(EXE, 7)
        self.kernel.ResumeThread.assert_not_called()
        closed = [call.args[0] for call in self.kernel.CloseHandle.call_args_list]
        self.assertCountEqual(closed, [200, 201, 300, 500])

    def test_exited_probe_rejects_wait_failure(self):
        worker = host._Worker(300, 400, 500)
        self.kernel.WaitForSingleObject.return_value = 258
        self.assertFalse(worker.exited())
        self.kernel.WaitForSingleObject.return_value = 0
        self.assertTrue(worker.exited())
        self.kernel.WaitForSingleObject.return_value = 0xFFFFFFFF
        with self.assertRaises(OSError):
            worker.exited()

    def test_cleanup_timeout_is_fatal_and_still_closes_handles(self):
        self.kernel.WaitForSingleObject.return_value = 258
        worker = host._Worker(300, 400, 500)
        with self.assertRaises(OSError):
            worker.close()
        self.kernel.CloseHandle.assert_any_call(300)
        self.kernel.CloseHandle.assert_any_call(400)
        worker.close()
        self.kernel.TerminateJobObject.assert_called_once()

    def test_launch_cleanup_timeout_is_fatal_not_a_retryable_launch_error(self):
        self.kernel.AssignProcessToJobObject.return_value = 0
        self.kernel.WaitForSingleObject.return_value = 258
        with self.assertRaises(host._WorkerCleanupError):
            host._launch_worker(EXE, 7)
        self.kernel.CloseHandle.assert_any_call(400)
        self.kernel.CloseHandle.assert_any_call(500)

    def test_stop_event_has_protected_acl_and_no_handle_inheritance(self):
        worker = host._launch_worker(EXE, 7)
        arguments = self.kernel.CreateEventW.call_args.args
        security = c.cast(arguments[0], c.POINTER(host.SECURITY_ATTRIBUTES)).contents
        self.assertFalse(security.bInheritHandle)
        self.assertEqual(security.lpSecurityDescriptor, 900)
        self.assertEqual(arguments[1:3], (True, False))
        self.assertEqual(arguments[3], "Global\\BeamerSecureStop-" + "a" * 32)
        self.assertEqual(self.kernel.LocalFree.call_args.args[0].value, 900)
        worker.close()

    def test_worker_event_opens_synchronise_only_and_rejects_arbitrary_names(self):
        self.kernel.OpenEventW.return_value = 600
        event = host.open_worker_stop_event("Global\\BeamerSecureStop-" + "a" * 32)
        self.kernel.OpenEventW.assert_called_once_with(0x00100000, False, "Global\\BeamerSecureStop-" + "a" * 32)
        self.kernel.WaitForSingleObject.return_value = 258
        self.assertFalse(event.is_set())
        self.kernel.WaitForSingleObject.return_value = 0
        self.assertTrue(event.is_set())
        event.close()
        self.kernel.CloseHandle.assert_called_with(600)
        with self.assertRaises(ValueError):
            host.open_worker_stop_event("Global\\arbitrary")


class StopAfter:
    def __init__(self, waits):
        self.remaining = waits
        self.stopped = False
        self.delays = []

    def is_set(self):
        return self.stopped

    def wait(self, timeout):
        self.delays.append(timeout)
        self.remaining -= 1
        self.stopped = self.remaining <= 0
        return self.stopped


@unittest.skipUnless(sys.platform == "win32", "Requires Windows SCM callbacks and ctypes ABI; covered on the rig")
class SupervisionTests(unittest.TestCase):
    def test_console_change_closes_old_job_before_replacement(self):
        service = host._ServiceHost(EXE)
        service.stop = StopAfter(3)
        order = []
        workers = []

        def launch(path, session):
            order.append(("launch", session))
            worker = MagicMock()
            worker.exited.return_value = False
            worker.close.side_effect = lambda: order.append(("close", session))
            workers.append(worker)
            return worker

        with patch.object(host.k32, "WTSGetActiveConsoleSessionId", side_effect=[1, 2, 2]), patch.object(
            host, "_launch_worker", side_effect=launch
        ):
            service._supervise()
        self.assertEqual(order, [("launch", 1), ("close", 1), ("launch", 2), ("close", 2)])

    def test_missing_console_does_not_create_worker(self):
        service = host._ServiceHost(EXE)
        service.stop = StopAfter(2)
        with patch.object(host.k32, "WTSGetActiveConsoleSessionId", side_effect=[0xFFFFFFFF, 0]), patch.object(
            host, "_launch_worker"
        ) as launch:
            service._supervise()
        launch.assert_not_called()

    def test_failed_launch_waits_two_seconds_before_retry(self):
        service = host._ServiceHost(EXE)
        service.stop = StopAfter(4)
        worker = MagicMock()
        worker.exited.return_value = False
        with patch.object(host.k32, "WTSGetActiveConsoleSessionId", return_value=1), patch.object(
            host.time, "monotonic", side_effect=[0.0, 0.25, 1.9, 2.0]
        ), patch.object(host, "_launch_worker", side_effect=[OSError(5, "denied"), worker]) as launch:
            service._supervise()
        self.assertEqual(launch.call_count, 2)
        worker.close.assert_called_once()
        self.assertTrue(all(delay <= 0.25 for delay in service.stop.delays))

    def test_worker_exit_has_retry_delay_and_job_cleanup(self):
        service = host._ServiceHost(EXE)
        service.stop = StopAfter(4)
        worker = MagicMock()
        worker.exited.return_value = True
        with patch.object(host.k32, "WTSGetActiveConsoleSessionId", return_value=1), patch.object(
            host.time, "monotonic", side_effect=[0.0, 0.25, 1.9, 2.25]
        ), patch.object(host, "_launch_worker", return_value=worker) as launch:
            service._supervise()
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(worker.close.call_count, 2)

    def test_unconfirmed_termination_never_launches_replacement(self):
        service = host._ServiceHost(EXE)
        service.stop = StopAfter(3)
        worker = MagicMock()
        worker.exited.return_value = False
        worker.close.side_effect = OSError(1460, "timeout")
        with patch.object(host.k32, "WTSGetActiveConsoleSessionId", side_effect=[1, 2]), patch.object(
            host, "_launch_worker", return_value=worker
        ) as launch, self.assertRaises(OSError):
            service._supervise()
        self.assertEqual(launch.call_count, 1)

    def test_launch_cleanup_failure_stops_service_without_retry(self):
        service = host._ServiceHost(EXE)
        service.stop = StopAfter(4)
        with patch.object(host.k32, "WTSGetActiveConsoleSessionId", return_value=1), patch.object(
            host, "_launch_worker", side_effect=host._WorkerCleanupError(1460, "timeout")
        ) as launch, self.assertRaises(host._WorkerCleanupError):
            service._supervise()
        launch.assert_called_once()


@unittest.skipUnless(sys.platform == "win32", "Requires Windows SCM callbacks and ctypes ABI; covered on the rig")
class ServiceTests(unittest.TestCase):
    def test_stop_during_registration_never_reports_running(self):
        service = host._ServiceHost(EXE)
        states = []

        def register(*arguments):
            service.stop.set()
            return 99

        with patch.object(host.a32, "RegisterServiceCtrlHandlerExW", side_effect=register), patch.object(
            host.a32, "SetServiceStatus", side_effect=lambda handle, status:
            states.append(c.cast(status, c.POINTER(host.SERVICE_STATUS)).contents.dwCurrentState) or 1
        ):
            service._main(0, None)
        self.assertEqual(states, [1])

    def test_stop_and_shutdown_stop_supervision(self):
        for control in (1, 5):
            with self.subTest(control=control):
                service = host._ServiceHost(EXE)
                service.status_handle = 99
                states = []
                with patch.object(host.a32, "SetServiceStatus", side_effect=lambda handle, status:
                                  states.append(c.cast(status, c.POINTER(host.SERVICE_STATUS)).contents.dwCurrentState) or 1):
                    self.assertEqual(service._control(control, 0, None, None), 0)
                self.assertTrue(service.stop.is_set())
                self.assertEqual(states, [3])

    def test_scm_dispatch_registers_and_reports_stopped_on_worker_error(self):
        states = []

        def dispatch(table):
            self.assertEqual(table[0].lpServiceName, "BeamerSecureDesktop")
            self.assertIsNone(table[1].lpServiceName)
            table[0].lpServiceProc(0, None)
            return 1

        with patch.object(host, "is_system", return_value=True), patch.object(
            host.a32, "StartServiceCtrlDispatcherW", side_effect=dispatch
        ), patch.object(host.a32, "RegisterServiceCtrlHandlerExW", return_value=99), patch.object(
            host.a32, "SetServiceStatus", side_effect=lambda handle, status:
            states.append((c.cast(status, c.POINTER(host.SERVICE_STATUS)).contents.dwCurrentState,
                           c.cast(status, c.POINTER(host.SERVICE_STATUS)).contents.dwWin32ExitCode)) or 1
        ), patch.object(host._ServiceHost, "_supervise", side_effect=OSError(5, "denied")):
            self.assertNotEqual(host.run_service(EXE), 0)
        self.assertEqual([state for state, code in states], [2, 4, 1])
        self.assertNotEqual(states[-1][1], 0)

    def test_non_system_cannot_enter_dispatcher(self):
        with patch.object(host, "is_system", return_value=False), patch.object(
            host.a32, "StartServiceCtrlDispatcherW"
        ) as dispatcher:
            self.assertEqual(host.run_service(EXE), 5)
        dispatcher.assert_not_called()

    def test_relative_or_quoted_path_cannot_enter_dispatcher(self):
        with patch.object(host, "is_system", return_value=True), patch.object(
            host.a32, "StartServiceCtrlDispatcherW"
        ) as dispatcher:
            for path in (Path("worker.exe"), Path('C:\\bad"path\\worker.exe')):
                self.assertEqual(host.run_service(path), 87)
        dispatcher.assert_not_called()

    def test_scm_connection_failure_returns_windows_error(self):
        with patch.object(host, "is_system", return_value=True), patch.object(
            host.a32, "StartServiceCtrlDispatcherW", return_value=0
        ), patch.object(host.c, "get_last_error", return_value=1063):
            self.assertEqual(host.run_service(EXE), 1063)

    def test_native_layouts_and_read_only_identity(self):
        self.assertEqual(c.sizeof(host.STARTUPINFOW), 104)
        self.assertEqual(c.sizeof(host.PROCESS_INFORMATION), 24)
        self.assertEqual(c.sizeof(host.JOBOBJECT_EXTENDED_LIMIT_INFORMATION), 144)
        self.assertEqual(c.sizeof(host.SERVICE_STATUS), 28)
        self.assertIsInstance(host.is_system(), bool)


if __name__ == "__main__":
    unittest.main()
