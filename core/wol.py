"""Wake-on-LAN, shared by both apps: each machine wakes the other when a switch finds it asleep.

The hardware address is never typed. Each end reads it from its own ARP table whenever a
connection succeeds -- the two machines have just exchanged packets, so the entry is fresh --
and stores it with the rest of its settings.
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys

WOL_PORT = 9
# Sleep answers in a few seconds; hibernate and a cold boot take most of a minute. Past this
# the other machine is reported as not woken rather than the switch waiting forever.
WAKE_WINDOW_SECONDS = 60.0

# Six groups of one or two hex digits: Windows pads and joins with "-", macOS leaves a leading
# zero off and joins with ":".
_MAC_PATTERN = re.compile(r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{1,2}(?:[:-][0-9A-Fa-f]{1,2}){5})(?![0-9A-Fa-f])")


def parse_mac(text: str):
    """The address as six upper-case, zero-padded pairs joined with ":", or None."""
    match = _MAC_PATTERN.fullmatch(text.strip()) if isinstance(text, str) else None
    if match is None:
        return None
    return ":".join(part.zfill(2).upper() for part in re.split(r"[:-]", match.group(1)))


def mac_from_arp_output(output: str, ip: str):
    """The hardware address `arp` printed for `ip`, from either platform's layout:

        Windows   192.168.1.3           02-1a-2b-3c-0d-4e     dynamic
        macOS     ? (192.168.1.3) at 2:1a:2b:3c:d:4e on en0 ifscope [ethernet]
    """
    ip_pattern = re.compile(r"(?<![\d.])" + re.escape(ip) + r"(?![\d.])")
    for line in output.splitlines():
        if not ip_pattern.search(line):
            continue
        match = _MAC_PATTERN.search(line)
        if match is not None:
            return parse_mac(match.group(1))
    return None


def lookup_mac(host: str):
    """The hardware address this machine's ARP table holds for `host`, or None. Only useful
    right after talking to the host, which is the only time it is called. On Windows `arp` is a
    console program, so it is started with no window: Beamer there is a tray app, and a console
    flashing up on every connection is exactly what it must never do."""
    windows = sys.platform == "win32"
    try:
        ip = socket.gethostbyname(host)
        completed = subprocess.run(
            ["arp", "-a", ip] if windows else ["arp", "-n", ip],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if windows else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return mac_from_arp_output(completed.stdout, ip)


def magic_packet(mac: str) -> bytes:
    address = parse_mac(mac)
    if address is None:
        raise ValueError(f"not a hardware address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(address.replace(":", "")) * 16


def send_magic_packet(mac: str, host: str = None) -> None:
    """Broadcast the packet, send it to the host's own subnet, and straight at its last address.
    The limited broadcast leaves by one interface only, which on a machine with a VPN or a
    virtual adapter can be the wrong one; the subnet's broadcast is routed out of the right one;
    and the direct send is what gets through on a network that filters broadcasts."""
    packet = magic_packet(mac)
    targets = ["255.255.255.255"]
    if host:
        subnet = subnet_broadcast(host)
        if subnet:
            targets.append(subnet)
        targets.append(host)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for index, target in enumerate(targets):
            try:
                sock.sendto(packet, (target, WOL_PORT))
            except OSError:
                if index == 0:
                    raise
    finally:
        sock.close()


def subnet_broadcast(host: str):
    """The /24 broadcast for a dotted IPv4 address, the size of nearly every home network, or
    None for anything else."""
    parts = host.split(".") if isinstance(host, str) else []
    if len(parts) != 4 or not all(part.isdigit() and int(part) <= 255 for part in parts):
        return None
    return ".".join(parts[:3] + ["255"])
