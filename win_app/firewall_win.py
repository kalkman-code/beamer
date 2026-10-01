"""Windows Firewall as it applies to this executable: read it, explain it, repair it.

The only module that talks to the firewall. The shelling-out lives in `_powershell` and the
three public entry points that call it; everything else is pure so the interpretation runs
under unittest on the Mac, the same way receiver.py keeps input_injector at arm's length.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import subprocess
import sys
from typing import Iterable, Optional, Sequence

from core import protocol

LOGGER = logging.getLogger(__name__)

RULE_NAME = "Beamer Receiver (TCP-In)"
# The pairing beacon's UDP port, written alongside the TCP rule by repair() and the installer.
# It is judged separately from the TCP rule because the two fail differently: without TCP the
# Mac cannot reach Beamer at all, while without UDP everything works except that a new Mac never
# sees this PC in its list -- and an install that predates the pairing rule has the first and
# not the second, so reporting only the TCP rule tells that user everything is fine.
PAIRING_RULE_NAME = "Beamer Pairing (UDP-In)"
PAIRING_PORT = protocol.PAIRING_PORT
# Pairing version 3 also hosts the pairing exchange over TCP on the same number while a code is
# on screen. Judged on its own: without it a Mac still finds this PC, then fails to finish pairing.
PAIRING_TCP_RULE_NAME = "Beamer Pairing (TCP-In)"
_IS_WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class FirewallStatus:
    port: int
    allowed: bool
    blocked: bool
    active_profiles: tuple
    lan_profiles: tuple
    covered: bool
    public_interfaces: tuple
    elevated: bool
    error: str = ""
    # Whether the pairing beacon's UDP rule is present. Defaults True so a status built before
    # pairing existed does not claim a missing rule it knows nothing about.
    pairing_allowed: bool = True
    pairing_tcp_allowed: bool = True


def missing_rule(status: "FirewallStatus") -> bool:
    """Whether a rule this app needs is absent and nothing blocks it, so an elevated run may write the
    rules without asking: the receiver's, or either pairing rule."""
    return not status.error and not status.blocked and not (
        status.allowed and getattr(status, "pairing_allowed", True) and getattr(status, "pairing_tcp_allowed", True)
    )


@dataclass(frozen=True)
class Advice:
    sentence: str
    button: str
    action: Optional[str]
    rule_profiles: tuple = ("Private",)


# ---------------------------------------------------------------- interpretation (pure)


def _normalise_path(path: str) -> str:
    return os.path.expandvars(path or "").replace("/", "\\").strip().lower()


def _profiles(value) -> Optional[set]:
    """A rule's Profile as a set of names, or None for Any. Windows PowerShell renders the
    flags enum as "Private, Public"; a raw bitmask arrives if it ever does not."""
    if isinstance(value, int):
        if value == 0:
            return None
        return {name for bit, name in ((1, "Domain"), (2, "Private"), (4, "Public")) if value & bit}
    names = {part.strip() for part in str(value or "").split(",") if part.strip()}
    if not names or "Any" in names:
        return None
    return names


def _port_matches(entries, port: int) -> bool:
    if entries is None:
        return True
    if isinstance(entries, (str, int)):
        entries = [entries]
    for entry in entries:
        text = str(entry).strip()
        if text.lower() == "any":
            return True
        if "-" in text:
            low, _, high = text.partition("-")
            if low.strip().isdigit() and high.strip().isdigit() and int(low) <= port <= int(high):
                return True
        elif text.isdigit() and int(text) == port:
            return True
    return False


def _protocol_matches(value, wanted: str = "tcp") -> bool:
    text = str(value or "Any").strip().lower()
    return text in ("any", wanted, {"tcp": "6", "udp": "17"}[wanted])


def _rule_applies(rule: dict, exe: str, port: int, protocol: str = "tcp") -> bool:
    return (
        str(rule.get("enabled", "")).lower() == "true"
        and str(rule.get("direction", "Inbound")).lower() == "inbound"
        and _normalise_path(str(rule.get("program", ""))) == _normalise_path(exe)
        and _protocol_matches(rule.get("protocol"), protocol)
        and _port_matches(rule.get("localPort"), port)
    )


def _lan_networks(networks: Sequence[dict]) -> list:
    """The interface(s) the Mac would arrive on. Internet-connected first; a Hyper-V switch on
    Public with no route out is not the LAN and must not trigger the trust offer while Wi-Fi is
    Private, the common case."""
    ranked = {"internet": 3, "localnetwork": 2, "subnet": 1}
    best = 0
    for network in networks:
        score = max(ranked.get(str(network.get(key, "")).lower(), 0) for key in ("ipv4", "ipv6"))
        best = max(best, score)
    if best == 0:
        return list(networks)
    return [
        network
        for network in networks
        if max(ranked.get(str(network.get(key, "")).lower(), 0) for key in ("ipv4", "ipv6")) == best
    ]


def interpret(payload: dict, exe: str, port: int, elevated: bool = True) -> FirewallStatus:
    rules = [rule for rule in payload.get("rules", []) if _rule_applies(rule, exe, port)]
    networks = list(payload.get("networks", []))
    active = tuple(sorted({str(n.get("category", "")) for n in networks if n.get("category")}))
    lan = _lan_networks(networks)
    lan_profiles = tuple(sorted({str(n.get("category", "")) for n in lan if n.get("category")}))

    allow_profiles = [_profiles(r.get("profile")) for r in rules if str(r.get("action", "")).lower() == "allow"]
    block_profiles = [_profiles(r.get("profile")) for r in rules if str(r.get("action", "")).lower() == "block"]

    def touches(profile_set: Optional[set], wanted: Iterable[str]) -> bool:
        return profile_set is None or any(name in profile_set for name in wanted)

    # A block rule beats an allow rule on any profile they share, so a block scoped to a
    # profile nothing is connected to is harmless and must not send the user chasing it.
    blocked = any(touches(p, active or ("Domain", "Private", "Public")) for p in block_profiles)
    covered = all(any(touches(p, (name,)) for p in allow_profiles) for name in lan_profiles) and bool(allow_profiles)
    public = tuple((int(n.get("index", 0)), str(n.get("alias", ""))) for n in lan if str(n.get("category", "")) == "Public")
    def pairing_allowed(protocol: str) -> bool:
        return any(
            _rule_applies(rule, exe, PAIRING_PORT, protocol) and str(rule.get("action", "")).lower() == "allow"
            for rule in payload.get("rules", [])
        )

    return FirewallStatus(
        port=port,
        allowed=bool(allow_profiles),
        blocked=blocked,
        active_profiles=active,
        lan_profiles=lan_profiles,
        covered=covered,
        public_interfaces=public,
        elevated=elevated,
        error="",
        pairing_allowed=pairing_allowed("udp"),
        pairing_tcp_allowed=pairing_allowed("tcp"),
    )


def advise(status: FirewallStatus) -> Advice:
    """One sentence a non-technical person can act on, and the one button that fixes it."""
    port = status.port
    rule_profiles = ("Private",) + (("Domain",) if "DomainAuthenticated" in status.lan_profiles else ())
    if status.error:
        return Advice(f"Beamer could not read the Windows Firewall: {status.error}", "Check again", "check")
    if status.blocked:
        advice = Advice(
            f"Windows Firewall is blocking Beamer on port {port}. This happens when Cancel was pressed on "
            "Windows' own \"allow this app\" prompt, and a block rule beats any allow rule, so reinstalling "
            "does not clear it. Fixing it removes the block and adds the right rule for private networks.",
            "Fix the firewall",
            "repair",
            rule_profiles,
        )
    elif not status.allowed:
        advice = Advice(
            f"Windows Firewall has no rule letting your Mac reach Beamer on port {port}, so the Mac will say "
            "Windows is unreachable. This happens when the installer was run without administrator rights. "
            "Fixing it adds the rule for private networks.",
            "Fix the firewall",
            "repair",
            rule_profiles,
        )
    elif not status.covered and status.public_interfaces:
        names = ", ".join(alias for _, alias in status.public_interfaces if alias) or "this network"
        advice = Advice(
            f"Windows treats {names} as a public network, so Beamer's firewall rule does not apply on it and "
            "your Mac cannot get in. Trusting it marks the network as private, which lets the Mac in without "
            "opening the port on every other network this PC joins.",
            "Trust this network",
            "trust",
        )
    elif not status.covered:
        names = ", ".join(status.lan_profiles)
        advice = Advice(
            f"Beamer's firewall rule does not apply on this network (Windows calls it {names}). "
            "Fixing it rewrites the rule to cover it.",
            "Fix the firewall",
            "repair",
            rule_profiles,
        )
    elif not status.pairing_allowed or not status.pairing_tcp_allowed:
        missing = []
        if not status.pairing_allowed:
            missing.append((
                f"the pairing beacon on UDP {PAIRING_PORT}",
                "a Mac that has not been set up by hand will never see this PC in its list",
            ))
        if not status.pairing_tcp_allowed:
            missing.append((
                f"the pairing exchange on TCP {PAIRING_PORT}",
                "a Mac that finds this PC cannot finish pairing with it",
            ))
        advice = Advice(
            f"Windows Firewall lets your Mac reach Beamer on port {port}, but there is no rule for "
            f"{' or for '.join(rule for rule, _ in missing)}, so {' and '.join(effect for _, effect in missing)}. "
            "Installs made before these rules existed are missing them. Fixing it adds what is missing "
            "and leaves the working rules alone.",
            "Fix the firewall",
            "repair",
            rule_profiles,
        )
    else:
        where = "this private network" if "Private" in status.lan_profiles else "this network"
        return Advice(
            f"Windows Firewall lets your Mac reach Beamer on port {port} on {where}, and lets a new Mac "
            "discover this PC.",
            "Check again",
            "check",
        )
    if not status.elevated:
        return Advice(
            advice.sentence + " Beamer is not running as administrator, so it cannot make this change itself: "
            "quit Beamer and open it again from the Start menu.",
            advice.button,
            None,
            advice.rule_profiles,
        )
    return advice


# ---------------------------------------------------------------- Windows


def _quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _powershell(script: str, timeout: float = 60.0) -> str:
    if not _IS_WINDOWS:
        raise RuntimeError("Windows Firewall is only available on Windows")
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    # A GUI app must never flash a console: CREATE_NO_WINDOW plus a hidden STARTUPINFO covers
    # both the console host and any window the child asks for.
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    completed = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW,
        startupinfo=startupinfo,
    )
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    if completed.returncode != 0:
        raise RuntimeError(stderr.splitlines()[0] if stderr else f"PowerShell exited with code {completed.returncode}")
    return stdout


_PREAMBLE = "$ErrorActionPreference = 'Stop'; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"

# A bare Remove-NetFirewallRule deletes every rule on the machine, so the repair script only ever
# pipes into it from a non-empty, explicitly counted collection.
# Every rule whose program is a Beamer.exe, wherever it lives: our named rule, the pair Windows
# writes from its own prompt, and any left by an older install path. The exact-path match
# happens in interpret(), so a rule for a stale copy shows up as "no rule" rather than a pass.
_RULES_FOR_LEAF = (
    "$leaf = Split-Path -Leaf $exe; "
    "Get-NetFirewallApplicationFilter | Where-Object { $_.Program -and ($_.Program -like ('*\\' + $leaf)) }"
)

_STATUS_SCRIPT = _PREAMBLE + """
$exe = {exe}
$rules = @()
foreach ($filter in $({filters})) {{
    $rule = $filter | Get-NetFirewallRule
    if ([string]$rule.Direction -ne 'Inbound') {{ continue }}
    $portFilter = $rule | Get-NetFirewallPortFilter
    $rules += [pscustomobject]@{{
        name = $rule.Name; display = $rule.DisplayName; enabled = [string]$rule.Enabled
        action = [string]$rule.Action; direction = [string]$rule.Direction; profile = [string]$rule.Profile
        program = $filter.Program; protocol = [string]$portFilter.Protocol; localPort = @($portFilter.LocalPort)
    }}
}}
$networks = @(Get-NetConnectionProfile | ForEach-Object {{
    [pscustomobject]@{{
        alias = $_.InterfaceAlias; index = [int]$_.InterfaceIndex; category = [string]$_.NetworkCategory
        ipv4 = [string]$_.IPv4Connectivity; ipv6 = [string]$_.IPv6Connectivity
    }}
}})
[pscustomobject]@{{ rules = @($rules); networks = $networks }} | ConvertTo-Json -Depth 4 -Compress
"""

# The same rule is reached more than once (by its program and again by its display name), and a
# second removal of a rule already gone raises "The requested object could not be found". Each rule
# is therefore removed once, by name, and that one error is the state removal wants, not a failure.
_REMOVE_SCRIPT = _PREAMBLE + """
$exe = {exe}
$stale = @($({filters}) | Get-NetFirewallRule | Where-Object {{ [string]$_.Direction -eq 'Inbound' }})
$stale += @(Get-NetFirewallRule -DisplayName {name} -ErrorAction SilentlyContinue)
$stale += @(Get-NetFirewallRule -DisplayName {pairing_name} -ErrorAction SilentlyContinue)
$stale += @(Get-NetFirewallRule -DisplayName {pairing_tcp_name} -ErrorAction SilentlyContinue)
$stale = @($stale | Sort-Object Name -Unique)
foreach ($rule in $stale) {{
    try {{ Remove-NetFirewallRule -Name $rule.Name }}
    catch {{ if ($_.Exception.Message -notmatch 'could not be found') {{ throw }} }}
}}
"""

_ADD_SCRIPT = _PREAMBLE + """
$exe = {exe}
New-NetFirewallRule -DisplayName {name} -Direction Inbound -Protocol TCP -LocalPort {port} -Action Allow -Profile {profiles} -Program $exe | Out-Null
New-NetFirewallRule -DisplayName {pairing_name} -Direction Inbound -Protocol UDP -LocalPort {pairing_port} -Action Allow -Profile {profiles} -Program $exe | Out-Null
New-NetFirewallRule -DisplayName {pairing_tcp_name} -Direction Inbound -Protocol TCP -LocalPort {pairing_port} -Action Allow -Profile {profiles} -Program $exe | Out-Null
"""

_TRUST_SCRIPT = _PREAMBLE + """
foreach ($index in @({indexes})) {{ Set-NetConnectionProfile -InterfaceIndex $index -NetworkCategory Private }}
"""


def is_elevated() -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        LOGGER.debug("Elevation check failed", exc_info=True)
        return False


def status(exe: str, port: int) -> FirewallStatus:
    elevated = is_elevated()
    try:
        raw = _powershell(_STATUS_SCRIPT.format(exe=_quote(exe), filters=_RULES_FOR_LEAF))
        return interpret(json.loads(raw), exe, port, elevated)
    except Exception as exc:
        LOGGER.exception("Firewall status failed")
        return FirewallStatus(port, False, False, (), (), False, (), elevated, error=str(exc) or type(exc).__name__)


def repair(exe: str, port: int, profiles: Sequence[str] = ("Private",)) -> None:
    """Drop every inbound rule for this executable, blocking ones included, and write the one
    the installer would have. Never Public: that opens the port on every untrusted network the
    machine ever joins, which is the wrong trade for a KVM; trust_network() is the fix for that."""
    wanted = [p for p in profiles if p in ("Private", "Domain")] or ["Private"]
    names = dict(
        exe=_quote(exe),
        name=_quote(RULE_NAME),
        pairing_name=_quote(PAIRING_RULE_NAME),
        pairing_tcp_name=_quote(PAIRING_TCP_RULE_NAME),
    )
    try:
        _powershell(_REMOVE_SCRIPT.format(filters=_RULES_FOR_LEAF, **names))
    except RuntimeError as exc:
        # Belt and braces for the script's own tolerance: a rule that is already absent must never
        # stop the new ones being written, or the PC is left with none.
        if "could not be found" not in str(exc):
            raise
        LOGGER.info("A firewall rule was already gone while repairing: %s", exc)
    _powershell(_ADD_SCRIPT.format(port=int(port), pairing_port=PAIRING_PORT, profiles=",".join(wanted), **names))


def trust_network(interface_indexes: Sequence[int]) -> None:
    if not interface_indexes:
        return
    _powershell(_TRUST_SCRIPT.format(indexes=",".join(str(int(i)) for i in interface_indexes)))
