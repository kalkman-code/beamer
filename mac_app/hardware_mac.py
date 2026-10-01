"""This Mac's hardware address, for the handshake's `hw` field (WIRE.md section 2).

A peer sends wake-on-LAN to it, so it must be the interface this link leaves from, not the first one
the system lists. The route is read by connecting a UDP socket (nothing is sent) and the address by
walking getifaddrs through ctypes: no subprocess, so nothing can flash a window."""

import ctypes
import sys

from core import pairing

AF_INET = 2
AF_LINK = 18


class Ifaddrs(ctypes.Structure):
    pass


Ifaddrs._fields_ = [
    ("next", ctypes.POINTER(Ifaddrs)),
    ("name", ctypes.c_char_p),
    ("flags", ctypes.c_uint),
    ("addr", ctypes.c_void_p),
    ("netmask", ctypes.c_void_p),
    ("dstaddr", ctypes.c_void_p),
    ("data", ctypes.c_void_p),
]


class SockaddrHeader(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint8), ("family", ctypes.c_uint8)]


class SockaddrIn(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint8), ("family", ctypes.c_uint8), ("port", ctypes.c_uint16), ("addr", ctypes.c_ubyte * 4)]


class SockaddrDl(ctypes.Structure):
    """sockaddr_dl up to sdl_data, where the interface name and then its link-layer address sit."""
    _fields_ = [
        ("length", ctypes.c_uint8), ("family", ctypes.c_uint8), ("index", ctypes.c_uint16),
        ("kind", ctypes.c_uint8), ("name_length", ctypes.c_uint8), ("address_length", ctypes.c_uint8), ("selector_length", ctypes.c_uint8),
    ]


def format_mac(raw: bytes) -> str:
    """Six bytes as `aa:bb:cc:dd:ee:ff`; anything else, and all zeros, is unknown ('')."""
    if len(raw) != 6 or not any(raw):
        return ""
    return ":".join(f"{b:02x}" for b in raw)


def _interface_table() -> tuple:
    """({IPv4 text: interface name}, {interface name: link-layer address bytes})."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.getifaddrs.argtypes = [ctypes.POINTER(ctypes.POINTER(Ifaddrs))]
    libc.freeifaddrs.argtypes = [ctypes.POINTER(Ifaddrs)]
    head = ctypes.POINTER(Ifaddrs)()
    if libc.getifaddrs(ctypes.byref(head)) != 0:
        raise OSError(ctypes.get_errno(), "getifaddrs failed")
    ips, macs = {}, {}
    try:
        node = head
        while node:
            entry = node.contents
            if entry.addr and entry.name:
                name = entry.name.decode("ascii", "replace")
                family = ctypes.cast(entry.addr, ctypes.POINTER(SockaddrHeader)).contents.family
                if family == AF_INET:
                    parsed = ctypes.cast(entry.addr, ctypes.POINTER(SockaddrIn)).contents
                    ips[".".join(str(b) for b in parsed.addr)] = name
                elif family == AF_LINK:
                    link = ctypes.cast(entry.addr, ctypes.POINTER(SockaddrDl)).contents
                    # The data follows the 8-byte header; the sockaddr's own length bounds the read.
                    whole = ctypes.string_at(entry.addr, max(link.length, 8))
                    start = 8 + link.name_length
                    macs[name] = whole[start:start + link.address_length]
            node = entry.next
    finally:
        libc.freeifaddrs(head)
    return ips, macs


def hardware_address_towards(host: str) -> str:
    """The MAC of the interface that would reach `host` (an IPv4 string), or '' when unknown.
    Never raises: the field is optional on the wire, so a failure only costs the peer its wake."""
    if sys.platform != "darwin" or not host:
        return ""
    try:
        local = pairing.local_address_towards(host)
        ips, macs = _interface_table()
        return format_mac(macs.get(ips.get(local), b""))
    except Exception:
        return ""
