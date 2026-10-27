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

from core import protocol, windows_tools
from core.i18n import tr

LOGGER = logging.getLogger(__name__)

RULE_NAME = "Beamer Receiver (TCP-In)"
# The pairing beacon's UDP port, written alongside the TCP rule by repair() and the installer.
# It is judged separately from the TCP rule because the two fail differently: without TCP no
# other machine can reach Beamer at all, while without UDP everything works except that a new
# machine never sees this PC in its list and this PC never hears theirs -- and an install that
# predates the pairing rule has the first and not the second, so reporting only the TCP rule
# tells that user everything is fine.
PAIRING_RULE_NAME = "Beamer Pairing (UDP-In)"
PAIRING_PORT = protocol.PAIRING_PORT
# Pairing version 3 also hosts the pairing exchange over TCP on the same number while a code is
# on screen. Judged on its own: without it another machine still finds this PC, then fails to finish pairing.
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
    # Rule presence controls automatic setup; profile coverage only controls advice.
    pairing_covered: bool = True
    pairing_tcp_covered: bool = True


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
    pairing_rules = {
        transport: [rule for rule in payload.get("rules", []) if _rule_applies(rule, exe, PAIRING_PORT, transport)]
        for transport in ("udp", "tcp")
    }
    networks = list(payload.get("networks", []))
    active = tuple(sorted({str(n.get("category", "")) for n in networks if n.get("category")}))
    lan = _lan_networks(networks)
    lan_profiles = tuple(sorted({str(n.get("category", "")) for n in lan if n.get("category")}))

    allow_profiles = [_profiles(r.get("profile")) for r in rules if str(r.get("action", "")).lower() == "allow"]
    block_profiles = [_profiles(r.get("profile")) for r in rules + pairing_rules["udp"] + pairing_rules["tcp"]
                      if str(r.get("action", "")).lower() == "block"]

    def touches(profile_set: Optional[set], wanted: Iterable[str]) -> bool:
        return profile_set is None or any(("Domain" if name == "DomainAuthenticated" else name) in profile_set
                                         for name in wanted)

    def covers(profiles) -> bool:
        return bool(profiles) and all(any(touches(p, (name,)) for p in profiles) for name in lan_profiles)

    # A block rule beats an allow rule on any profile they share, so a block scoped to a
    # profile nothing is connected to is harmless and must not send the user chasing it.
    blocked = any(touches(p, active or ("Domain", "Private", "Public")) for p in block_profiles)
    covered = covers(allow_profiles)
    public = tuple((int(n.get("index", 0)), str(n.get("alias", ""))) for n in lan if str(n.get("category", "")) == "Public")
    pairing_profiles = {
        transport: [_profiles(rule.get("profile")) for rule in matching
                    if str(rule.get("action", "")).lower() == "allow"]
        for transport, matching in pairing_rules.items()
    }

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
        pairing_allowed=bool(pairing_profiles["udp"]),
        pairing_tcp_allowed=bool(pairing_profiles["tcp"]),
        pairing_covered=covers(pairing_profiles["udp"]),
        pairing_tcp_covered=covers(pairing_profiles["tcp"]),
    )


def advise(status: FirewallStatus) -> Advice:
    """Explain the next step without offering privileged changes from the app."""
    port = status.port
    pairing_udp = status.pairing_allowed and status.pairing_covered
    pairing_tcp = status.pairing_tcp_allowed and status.pairing_tcp_covered
    if status.error:
        if status.error == "permission-denied" and not status.elevated:
            sentence = tr("Beamer cannot check Windows Firewall without administrator permission; run the Beamer installer again to repair its rules.")
        else:
            sentence = tr("Beamer could not check Windows Firewall. Check again; if the problem continues, run the Beamer installer again to repair its rules.")
        return Advice(sentence, tr("Check again"), "check")
    if status.blocked:
        return Advice(
            tr("Windows Firewall has a block rule affecting Beamer's input or pairing traffic; ask your administrator "
               "to review Beamer's inbound block rules in Windows Firewall."),
            tr("Check again"), "check",
        )
    healthy = status.allowed and status.covered and pairing_udp and pairing_tcp
    if healthy:
        where = tr("this private network") if "Private" in status.lan_profiles else tr("this network")
        return Advice(
            tr("Windows Firewall lets your other machines reach Beamer on port {port} on {where}, and lets a new machine "
            "discover this PC.").format(port=port, where=where),
            tr("Check again"),
            "check",
        )
    if "DomainAuthenticated" in status.lan_profiles:
        return Advice(
            tr("This managed network needs Windows Firewall rules for Beamer; ask your administrator to allow its receiver "
               "on TCP {port} and pairing on UDP and TCP {pairing_port} for the Domain profile.").format(
                   port=port, pairing_port=PAIRING_PORT),
            tr("Check again"), "check",
        )
    if status.allowed and status.public_interfaces:
        sentence = tr("Windows treats this network as public; if you trust it, change its network profile to Private in Windows Settings.")
    else:
        sentence = tr("Beamer's firewall rules need attention; run the Beamer installer again to repair them for private networks.")
        if port != protocol.DEFAULT_PORT:
            sentence = tr("Beamer uses custom port {port}; ask your administrator to allow that port for Beamer on trusted networks.").format(port=port)
    return Advice(sentence, tr("Check again"), "check")


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
    completed = windows_tools.run(
        [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        encoding="utf-8",
        timeout=timeout,
        startupinfo=startupinfo,
    )
    stdout = completed.stdout
    stderr = completed.stderr.strip()
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
try {{
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
}} catch {{
    $category = [int]$_.CategoryInfo.Category
    $denied = $_.CategoryInfo.Category -eq [System.Management.Automation.ErrorCategory]::PermissionDenied
    $hresult = $_.Exception.HResult
    $nativeError = $null
    for ($failure = $_.Exception; $null -ne $failure; $failure = $failure.InnerException) {{
        if ($failure -is [Microsoft.Management.Infrastructure.CimException]) {{
            $nativeError = [int]$failure.NativeErrorCode
            if ($failure.NativeErrorCode -eq [Microsoft.Management.Infrastructure.NativeErrorCode]::AccessDenied) {{
                $denied = $true
            }}
        }}
        # E_ACCESSDENIED and WBEM_E_ACCESS_DENIED remain stable across display languages.
        if ($failure -is [System.UnauthorizedAccessException] -or
            $failure.HResult -eq -2147024891 -or $failure.HResult -eq -2147217405) {{
            $denied = $true
            $hresult = $failure.HResult
        }}
    }}
    $kind = if ($denied) {{ 'permission-denied' }} else {{ 'query-failed' }}
    [pscustomobject]@{{error=$kind; category=$category; hresult=$hresult; native_error=$nativeError}} | ConvertTo-Json -Compress
}}
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
        payload = json.loads(raw)
        if "error" not in payload:
            return interpret(payload, exe, port, elevated)
        error = "permission-denied" if payload["error"] == "permission-denied" else "query-failed"
        LOGGER.warning("Firewall status query failed: %s", payload)
    except Exception:
        LOGGER.exception("Firewall status failed")
        error = "query-failed"
    return FirewallStatus(port, False, False, (), (), False, (), elevated, error=error)


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
