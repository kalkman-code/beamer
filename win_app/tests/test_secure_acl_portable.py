"""Secure grant ACL policy with allocated inert descriptors, never a filesystem ACL."""
import ctypes as c
import importlib
import unittest
from unittest.mock import Mock, patch


with patch.object(c, "WinDLL", side_effect=lambda *args, **kwargs: Mock(), create=True):
    acl = importlib.import_module("secure_store_acl")


class SecureAclTests(unittest.TestCase):
    def descriptor(self, *, owner=100, kind=0, rights=1, trusted=True, has_acl=True, ace_ok=True):
        dacl = c.create_string_buffer(32)
        ace = c.create_string_buffer(32)
        c.c_ushort.from_address(c.addressof(dacl) + 4).value = 1
        c.c_ubyte.from_address(c.addressof(ace)).value = kind
        acl.w.DWORD.from_address(c.addressof(ace) + 4).value = rights
        def read(path, object_type, flags, owner_out, group, dacl_out, sacl, descriptor):
            owner_out._obj.value = owner
            dacl_out._obj.value = c.addressof(dacl) if has_acl else None
            descriptor._obj.value = 300
            return 0
        def get_ace(pointer, index, output):
            output._obj.value = c.addressof(ace)
            return ace_ok
        def sid(pointer):
            value = pointer.value if isinstance(pointer, c.c_void_p) else pointer
            return "S-1-5-18" if value == owner or trusted else "S-1-5-21-999"
        return read, get_ace, sid

    def test_only_trusted_readers_are_permitted_and_descriptor_is_freed(self):
        read, ace, sid = self.descriptor()
        with patch.object(acl.a32, "GetNamedSecurityInfoW", side_effect=read), \
                patch.object(acl.a32, "GetAce", side_effect=ace), \
                patch.object(acl, "_sid_text", side_effect=sid), patch.object(acl.k32, "LocalFree") as free:
            self.assertIsNone(acl.require_protected_acl("synthetic/grant"))
        self.assertEqual(free.call_args.args[0].value, 300)

    def test_untrusted_access_and_unknown_entries_are_rejected(self):
        for options in (dict(trusted=False), dict(kind=2), dict(owner=0), dict(has_acl=False), dict(ace_ok=False)):
            read, ace, sid = self.descriptor(**options)
            with self.subTest(options=options), \
                    patch.object(acl.a32, "GetNamedSecurityInfoW", side_effect=read), \
                    patch.object(acl.a32, "GetAce", side_effect=ace), \
                    patch.object(acl, "_sid_text", side_effect=sid), \
                    patch.object(acl.k32, "LocalFree") as free:
                with self.assertRaises(PermissionError):
                    acl.require_protected_acl("synthetic/grant")
                free.assert_called_once()

    def test_deny_entries_and_zero_rights_do_not_grant_untrusted_access(self):
        for options in (dict(kind=1, trusted=False), dict(rights=0, trusted=False)):
            read, ace, sid = self.descriptor(**options)
            with patch.object(acl.a32, "GetNamedSecurityInfoW", side_effect=read), \
                    patch.object(acl.a32, "GetAce", side_effect=ace), patch.object(acl, "_sid_text", side_effect=sid):
                self.assertIsNone(acl.require_protected_acl("synthetic/grant"))

    def test_inspection_failure_is_not_treated_as_protected(self):
        with patch.object(acl.a32, "GetNamedSecurityInfoW", return_value=5), self.assertRaises(PermissionError):
            acl.require_protected_acl("synthetic/grant")

    def test_native_sid_conversion_frees_temporary_string(self):
        text = c.create_unicode_buffer("S-1-5-18")
        def convert(pointer, output):
            c.cast(output, c.POINTER(acl.w.LPWSTR))[0] = c.cast(text, acl.w.LPWSTR)
            return 1
        with patch.object(acl.a32, "ConvertSidToStringSidW", side_effect=convert), \
                patch.object(acl.k32, "LocalFree") as free:
            self.assertEqual(acl._sid_text(100), "S-1-5-18")
            free.assert_called_once()
        with patch.object(acl.a32, "ConvertSidToStringSidW", return_value=0), self.assertRaises(PermissionError):
            acl._sid_text(100)
