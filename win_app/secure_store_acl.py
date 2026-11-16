"""Keep machine-encrypted grants readable only by SYSTEM and Administrators."""

import ctypes as c
from ctypes import wintypes as w

from secure_desktop_win import a32, k32, signature

signature(a32, "GetNamedSecurityInfoW", w.DWORD, w.LPCWSTR, c.c_int, w.DWORD,
          c.POINTER(w.LPVOID), w.LPVOID, c.POINTER(w.LPVOID), w.LPVOID, c.POINTER(w.LPVOID))
signature(a32, "ConvertSidToStringSidW", w.BOOL, w.LPVOID, c.POINTER(w.LPWSTR))
signature(a32, "GetAce", w.BOOL, w.LPVOID, w.DWORD, c.POINTER(w.LPVOID))
signature(k32, "LocalFree", w.HANDLE, w.HANDLE)
TRUSTED = frozenset(("S-1-5-18", "S-1-5-32-544"))


def _sid_text(pointer):
    result = w.LPWSTR()
    if not a32.ConvertSidToStringSidW(pointer, c.byref(result)):
        raise PermissionError("Invalid secure store owner")
    try:
        return result.value
    finally:
        k32.LocalFree(result)


def require_protected_acl(path):
    owner, dacl, descriptor = w.LPVOID(), w.LPVOID(), w.LPVOID()
    if a32.GetNamedSecurityInfoW(str(path), 1, 5, c.byref(owner), None,
                                c.byref(dacl), None, c.byref(descriptor)) != 0:
        raise PermissionError("Cannot inspect secure store permissions")
    try:
        if not owner.value or _sid_text(owner) not in TRUSTED or not dacl.value:
            raise PermissionError("Secure store must belong to SYSTEM or Administrators")
        count = c.c_ushort.from_address(dacl.value + 4).value
        for index in range(count):
            ace = w.LPVOID()
            if not a32.GetAce(dacl, index, c.byref(ace)):
                raise PermissionError("Cannot inspect secure store permissions")
            kind = c.c_ubyte.from_address(ace.value).value
            if kind == 0:
                rights = w.DWORD.from_address(ace.value + 4).value
                if rights and _sid_text(ace.value + 8) not in TRUSTED:
                    raise PermissionError("Secure store allows untrusted access")
            elif kind != 1:
                raise PermissionError("Unsupported secure store permission entry")
    finally:
        k32.LocalFree(descriptor)
