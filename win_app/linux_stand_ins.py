"""The OS parts Linux has no back end for, on X11 and Wayland alike: the firewall check and the
lock screen. Each answers as a machine that has nothing and does nothing, mirroring only the names
the shell reaches through platform_parts, with the Windows module's signatures."""

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

unlock = SimpleNamespace(
    is_locked=lambda: None,
    signal_unlock=lambda: False,
    ensure_unlocked=lambda *args, **kwargs: False,
)


@dataclass(frozen=True)
class FirewallStatus:
    port: int
    allowed: bool = False
    blocked: bool = False
    active_profiles: tuple = ()
    lan_profiles: tuple = ()
    covered: bool = False
    public_interfaces: tuple = ()
    elevated: bool = False
    error: str = ""
    pairing_allowed: bool = True


@dataclass(frozen=True)
class Advice:
    sentence: str
    button: str
    action: Optional[str]
    rule_profiles: tuple = ("Private",)


firewall = SimpleNamespace(
    FirewallStatus=FirewallStatus,
    Advice=Advice,
    status=lambda exe, port: FirewallStatus(port),
    advise=lambda status: Advice("Beamer's firewall check is not yet on Linux.", "Check again", None),
    repair=lambda exe, port, profiles: None,
    trust_network=lambda indexes: None,
    is_elevated=lambda: False,
)
