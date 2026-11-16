"""Secure worker grant and lifecycle policy with synthetic data and inert native seams."""
import ctypes as c
import copy
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests import test_secure_worker as fixture


with patch.object(c, "WinDLL", side_effect=lambda *args, **kwargs: Mock(), create=True), \
        patch.object(c, "WINFUNCTYPE", c.CFUNCTYPE, create=True):
    worker = importlib.import_module("secure_worker")
    service = importlib.import_module("secure_service_host")
    acl = importlib.import_module("secure_store_acl")


class SecureWorkerTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(fixture, "worker", worker))
        self.enterContext(patch.object(worker, "known_folder", return_value=Path.cwd() / "synthetic-program-files"))
        fixture.EnrolmentTests.setUp(self)

    test_empty_unknown_disabled_and_mobile_selection_rejected = fixture.EnrolmentTests.test_empty_unknown_disabled_and_mobile_selection_rejected
    test_worker_refuses_to_run_from_user_writable_source_checkout = fixture.EnrolmentTests.test_worker_refuses_to_run_from_user_writable_source_checkout

    def test_grants_contain_only_selected_permissions_without_modifying_pairing(self):
        before = copy.deepcopy(self.source)
        grant = worker.make_grant(self.source, Path.cwd() / "synthetic-settings.json", [self.peer])
        self.assertEqual(self.source, before)
        self.assertEqual([peer["id"] for peer in grant["peers"]], [self.peer])
        self.assertEqual(set(grant["peers"][0]), {"id", "token", "platform", "allow_drive", "in_use"})

    def test_dpapi_boundary_uses_machine_scope_and_wipes_temporary_buffers(self):
        output = c.create_string_buffer(b"encrypted synthetic bytes")
        observed = []
        def encrypt(source, description, entropy, reserved, prompt, flags, target):
            observed.append((source._obj.data, source._obj.size, flags))
            target._obj.data = c.cast(output, c.POINTER(c.c_ubyte))
            target._obj.size = len(output) - 1
            return 1
        with patch.object(worker.crypt32, "CryptProtectData", side_effect=encrypt), \
                patch.object(worker.native.k32, "LocalFree") as free:
            self.assertEqual(worker.protect(b"synthetic cleartext"), b"encrypted synthetic bytes")
        self.assertEqual(observed[0][2], 5)
        self.assertEqual(c.string_at(observed[0][0], observed[0][1]), b"\0" * observed[0][1])
        self.assertEqual(output.raw, b"\0" * len(output))
        free.assert_called_once()
        for value in (b"", "not bytes", b"x" * (worker.MAX_STORE_BYTES + 1)):
            with self.assertRaises(ValueError):
                worker.protect(value)

    def test_enrolment_replaces_only_synthetic_grant_after_all_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(worker, "require_installed_binary"), \
                    patch.object(worker, "require_plain_path", side_effect=Path), \
                    patch.object(worker, "state_directory", return_value=folder), \
                    patch.object(worker, "read_json", return_value=self.source), \
                    patch.object(worker.shell32, "IsUserAnAdmin", return_value=1), \
                    patch.object(acl, "require_protected_acl"), \
                    patch.object(worker, "protect", return_value=b"synthetic encrypted bytes"):
                worker.enrol(folder / "settings.json", [self.peer])
            self.assertEqual((folder / "grants.dat").read_bytes(), b"synthetic encrypted bytes")
            self.assertEqual([path.name for path in folder.iterdir()], ["grants.dat"])

    def test_failed_atomic_replace_preserves_old_grant_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "grants.dat").write_bytes(b"previous synthetic grant")
            with patch.object(worker, "require_installed_binary"), \
                    patch.object(worker, "require_plain_path", side_effect=Path), \
                    patch.object(worker, "state_directory", return_value=folder), \
                    patch.object(worker, "read_json", return_value=self.source), \
                    patch.object(worker.shell32, "IsUserAnAdmin", return_value=1), \
                    patch.object(acl, "require_protected_acl"), \
                    patch.object(worker, "protect", return_value=b"replacement"), \
                    patch.object(worker.os, "replace", side_effect=OSError("refused")):
                with self.assertRaises(OSError):
                    worker.enrol(folder / "settings.json", [self.peer])
            self.assertEqual((folder / "grants.dat").read_bytes(), b"previous synthetic grant")
            self.assertEqual([path.name for path in folder.iterdir()], ["grants.dat"])

    def test_policy_reload_checks_acl_and_reloads_source_on_demand(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "settings.json"
            grant = worker.make_grant(self.source, source, [self.peer])
            (folder / "grants.dat").write_bytes(b"synthetic ciphertext")
            with patch.object(worker, "require_plain_path", side_effect=Path), \
                    patch.object(worker, "state_directory", return_value=folder), \
                    patch.object(acl, "require_protected_acl") as inspect, \
                    patch.object(worker, "unprotect", return_value=json.dumps(grant).encode()), \
                    patch.object(worker, "read_json", return_value=self.source) as read_source:
                policy = worker.load_policy()
                read_source.assert_not_called()
                self.assertEqual([entry["id"] for entry in policy.peers()], [self.peer])
                self.source["peers"][0]["allow_drive"] = False
                self.assertEqual(policy.peers(), [])
                self.assertEqual(read_source.call_count, 2)
            self.assertEqual(inspect.call_count, 2)

    def test_worker_releases_resources_when_receiver_fails(self):
        stop, receiver, injector = Mock(), Mock(), Mock()
        stop.is_set.return_value = False
        receiver.peers_changed.side_effect = RuntimeError("receiver stopped")
        with patch.object(service, "is_system", return_value=True), \
                patch.object(service, "open_worker_stop_event", return_value=stop), \
                patch.object(worker, "require_installed_binary"), \
                patch.object(worker.native, "current_session", return_value=1), \
                patch.object(worker.native, "active_console_session", return_value=1), \
                patch.object(worker.native, "desktop_name", return_value="Winlogon"), \
                patch.object(worker.native, "SecureInjector", return_value=injector), \
                patch.object(worker, "load_policy", return_value=Mock()), \
                patch.object(worker, "create_receiver", return_value=receiver):
            with self.assertRaises(RuntimeError):
                worker.run_worker("synthetic-stop")
        receiver.start.assert_called_once_with(worker.native.SECURE_PORT)
        receiver.stop.assert_called_once()
        injector.release_all.assert_called_once()
        stop.close.assert_called_once()

    def test_wrong_identity_or_console_never_starts_receiver(self):
        for system, session, console in ((False, 1, 1), (True, 0, 0), (True, 1, 2)):
            with self.subTest(system=system, session=session, console=console), \
                    patch.object(service, "is_system", return_value=system), \
                    patch.object(worker, "require_installed_binary"), \
                    patch.object(worker.native, "current_session", return_value=session), \
                    patch.object(worker.native, "active_console_session", return_value=console), \
                    patch.object(worker, "create_receiver") as create:
                with self.assertRaises(PermissionError):
                    worker.run_worker("synthetic-stop")
                create.assert_not_called()
