"""Exercise real Windows ACL inspection on job-owned synthetic state only."""

import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

if sys.platform == "win32":
    import secure_store_acl as acl
    from secure_desktop_win import signature
else:
    acl = signature = None


@unittest.skipUnless(sys.platform == "win32", "Windows secure desktop ACL tests")
class StorePermissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="beamer-secure-acl-")
        self.path = Path(self.temporary.name) / "synthetic.dat"
        self.path.write_bytes(b"synthetic data only")
        self.addCleanup(self.temporary.cleanup)

    def apply(self, permissions):
        signature(acl.a32, "ConvertStringSecurityDescriptorToSecurityDescriptorW", w.BOOL,
                  w.LPCWSTR, w.DWORD, c.POINTER(w.LPVOID), w.LPVOID)
        signature(acl.a32, "SetFileSecurityW", w.BOOL, w.LPCWSTR, w.DWORD, w.LPVOID)
        descriptor = w.LPVOID()
        self.assertTrue(acl.a32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "O:BAG:BAD:P" + permissions, 1, c.byref(descriptor), None))
        try:
            if not acl.a32.SetFileSecurityW(str(self.path), 0x80000005, descriptor):
                raise c.WinError(c.get_last_error())
        finally:
            acl.k32.LocalFree(descriptor)

    def test_administrator_and_system_only_state_is_accepted(self):
        self.apply("(A;;FA;;;SY)(A;;FA;;;BA)")
        acl.require_protected_acl(self.path)

    def test_users_read_access_to_machine_encrypted_grants_is_rejected(self):
        self.apply("(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;BU)")
        with self.assertRaises(PermissionError):
            acl.require_protected_acl(self.path)

    def test_installer_rejects_readable_state_but_accepts_readable_binaries(self):
        self.apply("(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;BU)")
        environment = dict(os.environ, BEAMER_ACL_TEST_PATH=str(self.path),
                           BEAMER_ACL_HELPER=str(Path(__file__).resolve().parents[1] / "Manage-SecureDesktop.ps1"))
        # Python otherwise carries PowerShell 7 modules into Windows PowerShell 5.
        environment["PSMODULEPATH"] = str(Path(os.environ["SYSTEMROOT"]) / "System32" /
                                           "WindowsPowerShell" / "v1.0" / "Modules")
        script = """
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$tree = [Management.Automation.Language.Parser]::ParseFile($env:BEAMER_ACL_HELPER, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Cannot parse administrator helper' }
foreach ($name in @('Assert-PlainPath', 'Assert-ProtectedTree')) {
    $definition = $tree.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name}, $false)
    if (-not $definition) { throw 'Missing ACL validation function' }
    Invoke-Expression $definition.Extent.Text
}
Assert-ProtectedTree -Path $env:BEAMER_ACL_TEST_PATH
try {
    Assert-ProtectedTree -Path $env:BEAMER_ACL_TEST_PATH -Confidential
} catch {
    if ($_.Exception.Message -like '*readable*') { exit 0 }
    throw
}
throw 'Readable state was accepted'
"""
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_users_write_or_delete_access_is_rejected(self):
        for rights in ("FW", "SD", "WD", "WO"):
            with self.subTest(rights=rights):
                self.apply(f"(A;;FA;;;SY)(A;;FA;;;BA)(A;;{rights};;;BU)")
                with self.assertRaises(PermissionError):
                    acl.require_protected_acl(self.path)

    def test_missing_state_is_rejected(self):
        with self.assertRaises(PermissionError):
            acl.require_protected_acl(self.path.with_name("missing.dat"))


if __name__ == "__main__":
    unittest.main()
