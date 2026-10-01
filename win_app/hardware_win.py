"""This PC's hardware address, for the handshake's `hw` field (WIRE.md section 2).

A peer sends wake-on-LAN to it, so it must be the adapter this link leaves from, not the first one
the system lists. No subprocess and no PowerShell: Beamer is a windowless app and either would
flash a console. ctypes types only, no ctypes.wintypes, so the module imports on every OS."""

import ctypes
import sys

from core import pairing

AF_INET = 2
GAA_FLAG_SKIP_ANYCAST = 0x2
GAA_FLAG_SKIP_MULTICAST = 0x4
GAA_FLAG_SKIP_DNS_SERVER = 0x8
ERROR_SUCCESS = 0
ERROR_BUFFER_OVERFLOW = 111


class SockaddrIn(ctypes.Structure):
    _fields_ = [("family", ctypes.c_uint16), ("port", ctypes.c_uint16), ("addr", ctypes.c_ubyte * 4)]


class SocketAddress(ctypes.Structure):
    _fields_ = [("sockaddr", ctypes.c_void_p), ("length", ctypes.c_int)]


class UnicastAddress(ctypes.Structure):
    pass


UnicastAddress._fields_ = [
    ("length", ctypes.c_uint32),
    ("flags", ctypes.c_uint32),
    ("next", ctypes.POINTER(UnicastAddress)),
    ("address", SocketAddress),
]


class AdapterAddresses(ctypes.Structure):
    """IP_ADAPTER_ADDRESSES_LH up to PhysicalAddressLength. ctypes' natural alignment matches the
    64-bit Windows header: Length and IfIndex fill the leading ULONGLONG union, every pointer is
    8-aligned. Only ever read through a pointer into the API's own buffer, so no tail is needed."""


AdapterAddresses._fields_ = [
    ("length", ctypes.c_uint32),
    ("if_index", ctypes.c_uint32),
    ("next", ctypes.POINTER(AdapterAddresses)),
    ("adapter_name", ctypes.c_void_p),
    ("first_unicast", ctypes.POINTER(UnicastAddress)),
    ("first_anycast", ctypes.c_void_p),
    ("first_multicast", ctypes.c_void_p),
    ("first_dns_server", ctypes.c_void_p),
    ("dns_suffix", ctypes.c_void_p),
    ("description", ctypes.c_void_p),
    ("friendly_name", ctypes.c_void_p),
    ("physical_address", ctypes.c_ubyte * 8),
    ("physical_address_length", ctypes.c_uint32),
]


def format_mac(raw: bytes) -> str:
    """Six bytes as `aa:bb:cc:dd:ee:ff`; anything else, and all zeros, is unknown ('')."""
    if len(raw) != 6 or not any(raw):
        return ""
    return ":".join(f"{b:02x}" for b in raw)


def _adapter_table() -> list:
    """(IPv4 text, physical address bytes) for every unicast address on every adapter."""
    lib = ctypes.WinDLL("iphlpapi")
    flags = GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_SKIP_DNS_SERVER
    size = ctypes.c_uint32(16384)
    # The adapter list can grow between the sizing call and the real one, hence the retry.
    for _ in range(4):
        buffer = ctypes.create_string_buffer(size.value)
        status = lib.GetAdaptersAddresses(AF_INET, flags, None, buffer, ctypes.byref(size))
        if status != ERROR_BUFFER_OVERFLOW:
            break
    if status != ERROR_SUCCESS:
        raise OSError(f"GetAdaptersAddresses returned {status}")

    table = []
    adapter = ctypes.cast(buffer, ctypes.POINTER(AdapterAddresses))
    while adapter:
        entry = adapter.contents
        raw = bytes(entry.physical_address[:min(entry.physical_address_length, 8)])
        unicast = entry.first_unicast
        while unicast:
            sockaddr = unicast.contents.address.sockaddr
            if sockaddr:
                parsed = ctypes.cast(sockaddr, ctypes.POINTER(SockaddrIn)).contents
                if parsed.family == AF_INET:
                    table.append((".".join(str(b) for b in parsed.addr), raw))
            unicast = unicast.contents.next
        adapter = entry.next
    return table


def hardware_address_towards(host: str) -> str:
    """The MAC of the adapter that would reach `host` (an IPv4 string), or '' when unknown.
    Never raises: the field is optional on the wire, so a failure only costs the peer its wake."""
    if sys.platform != "win32" or not host:
        return ""
    try:
        local = pairing.local_address_towards(host)
        for address, raw in _adapter_table():
            if address == local:
                mac = format_mac(raw)
                if mac:
                    return mac
    except Exception:
        return ""
    return ""
