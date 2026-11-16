"""Frozen entry point for administrator enrolment, SCM supervision and secure input."""

import argparse
import ctypes as c
from ctypes import wintypes as w
import json
import logging
import os
from pathlib import Path
import sys
import tempfile

from core import protocol
from core.secure_desktop import GrantPolicy, create_receiver
import secure_desktop_win as native

MAX_STORE_BYTES = 1024 * 1024
crypt32 = c.WinDLL("crypt32", use_last_error=True)
shell32 = c.WinDLL("shell32", use_last_error=True)


class BLOB(c.Structure):
    _fields_ = [("size", w.DWORD), ("data", c.POINTER(c.c_ubyte))]


native.signature(crypt32, "CryptProtectData", w.BOOL, c.POINTER(BLOB), w.LPCWSTR,
                 w.LPVOID, w.LPVOID, w.LPVOID, w.DWORD, c.POINTER(BLOB))
native.signature(crypt32, "CryptUnprotectData", w.BOOL, c.POINTER(BLOB), w.LPVOID,
                 w.LPVOID, w.LPVOID, w.LPVOID, w.DWORD, c.POINTER(BLOB))
native.signature(native.k32, "LocalFree", w.HANDLE, w.HANDLE)
native.signature(shell32, "SHGetFolderPathW", c.c_long, w.HWND, c.c_int, w.HANDLE, w.DWORD, w.LPWSTR)
native.signature(shell32, "IsUserAnAdmin", w.BOOL)


def _dpapi(value, encrypt):
    if not isinstance(value, bytes) or not 0 < len(value) <= MAX_STORE_BYTES:
        raise ValueError("Invalid secure trust data")
    memory = (c.c_ubyte * len(value)).from_buffer_copy(value)
    source, result = BLOB(len(value), memory), BLOB()
    function = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    try:
        if not function(c.byref(source), None, None, None, None, 5 if encrypt else 1, c.byref(result)):
            raise c.WinError(c.get_last_error())
        return c.string_at(result.data, result.size)
    finally:
        c.memset(memory, 0, len(value))
        if result.data:
            c.memset(result.data, 0, result.size)
            native.k32.LocalFree(result.data)


def protect(value):
    return _dpapi(value, True)


def unprotect(value):
    return _dpapi(value, False)


def known_folder(number):
    buffer = c.create_unicode_buffer(32768)
    if shell32.SHGetFolderPathW(None, number, None, 0, buffer) != 0:
        raise OSError("Windows folder is unavailable")
    return Path(buffer.value)


def state_directory():
    return known_folder(0x23) / "BeamerSecureDesktop"


def require_plain_path(path):
    path = Path(path)
    if not path.is_absolute() or str(path).startswith("\\\\"):
        raise PermissionError("Secure input requires a local absolute path")
    for part in (path, *path.parents):
        if part.exists() and part.lstat().st_file_attributes & 0x400:
            raise PermissionError("Secure input refuses reparse points")
    return path


def require_installed_binary():
    expected = known_folder(0x26) / "Beamer Secure Desktop" / "BeamerSecure.exe"
    if not getattr(sys, "frozen", False) or Path(sys.executable) != expected:
        raise PermissionError("Secure input must run from its protected installation")
    require_plain_path(expected)


def read_json(path):
    path = require_plain_path(path)
    with path.open("rb") as handle:
        value = handle.read(MAX_STORE_BYTES + 1)
    if len(value) > MAX_STORE_BYTES:
        raise ValueError("Secure input settings are too large")
    return json.loads(value)


def make_grant(settings, source_path, peer_ids):
    if (not isinstance(settings, dict) or protocol.read_id(settings.get("machine_id")) is None
            or not peer_ids or len(set(peer_ids)) != len(peer_ids)):
        raise ValueError("Select existing paired desktop peers")
    peers = settings.get("peers")
    if not isinstance(peers, list):
        raise ValueError("Invalid pairing settings")
    grants = []
    for ident in peer_ids:
        matches = [p for p in peers if isinstance(p, dict) and p.get("id") == ident]
        if len(matches) != 1:
            raise ValueError("Selected peer is not uniquely paired")
        peer = matches[0]
        if (protocol.read_id(ident) is None or ident == settings["machine_id"]
                or not protocol.is_paired_token(peer.get("token"))
                or peer.get("platform") not in ("windows", "macos", "linux")
                or peer.get("allow_drive") is not True or peer.get("in_use", True) is not True
                or peer.get("from_1_4") is True):
            raise ValueError("Selected peer is not eligible for secure input")
        grants.append({"id": ident, "token": peer["token"], "platform": peer["platform"],
                       "in_use": True, "allow_drive": True})
    payload = {"machine_id": settings["machine_id"], "name": settings.get("name") or "Windows",
               "source_settings": str(source_path), "peers": grants}
    GrantPolicy(payload, lambda: settings)
    return payload


def enrol(source, peers):
    require_installed_binary()
    if not shell32.IsUserAnAdmin():
        raise PermissionError("Administrator approval is required")
    source = require_plain_path(source)
    payload = make_grant(read_json(source), source, peers)
    folder = require_plain_path(state_directory())
    if not folder.is_dir():
        raise PermissionError("Install the protected service storage first")
    from secure_store_acl import require_protected_acl
    require_protected_acl(folder)
    encrypted = protect(json.dumps(payload, separators=(",", ":")).encode())
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, folder / "grants.dat")
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def load_policy():
    from secure_store_acl import require_protected_acl
    folder = require_plain_path(state_directory())
    require_protected_acl(folder)
    path = require_plain_path(folder / "grants.dat")
    require_protected_acl(path)
    with path.open("rb") as handle:
        encrypted = handle.read(MAX_STORE_BYTES + 1)
    payload = json.loads(unprotect(encrypted))
    source = require_plain_path(Path(payload["source_settings"]))
    return GrantPolicy(payload, lambda: read_json(source))


def run_worker(stop_name):
    from secure_service_host import is_system, open_worker_stop_event
    if not is_system():
        raise PermissionError("Secure input requires the service identity")
    require_installed_binary()
    session = native.current_session()
    if session == 0 or session != native.active_console_session():
        raise PermissionError("Secure input requires the active console session")
    if native.desktop_name(native.u32.GetThreadDesktop(native.k32.GetCurrentThreadId())) != "Winlogon":
        raise PermissionError("Secure input requires the Winlogon desktop")
    policy = load_policy()
    stop = open_worker_stop_event(stop_name)
    injector = native.SecureInjector()
    responder = create_receiver(policy, injector=injector, desktop=native,
                                available=native.secure_desktop_available,
                                status_callback=lambda *_: None)
    try:
        responder.start(native.SECURE_PORT)
        while not stop.is_set() and session == native.active_console_session():
            # A listener with no authenticated links must still notice grant removal.
            responder.peers_changed()
            import time
            time.sleep(0.2)
    finally:
        responder.stop()
        injector.release_all()
        stop.close()
    return 0


def main(argv=None):
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description="Beamer secure desktop administrator utility")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--service", action="store_true")
    mode.add_argument("--worker", action="store_true")
    mode.add_argument("--enrol", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--peer-id", nargs="+")
    parser.add_argument("--stop-event")
    args = parser.parse_args(argv)
    try:
        if args.check:
            print("Secure desktop executable loaded; no service or input started")
            return 0
        if args.enrol:
            if args.settings is None or not args.peer_id:
                raise ValueError("Select paired desktop peers and their settings file")
            enrol(args.settings, args.peer_id)
            print("Secure input grants replaced")
            return 0
        if args.worker:
            return run_worker(args.stop_event)
        require_installed_binary()
        from secure_service_host import run_service
        return run_service(Path(sys.executable))
    except Exception:
        # No exception text: a parser, key planner or failed backend can include input.
        print("Secure desktop operation refused or failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
