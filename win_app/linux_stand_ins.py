"""The OS parts on Linux until their back ends land: each answers as a machine that has nothing,
says once in the log that it is not built yet, and does nothing. They let the shell start and show
its window and tray; nothing listens, pairs or captures.

Each mirrors only the names the shell reaches through platform_parts, with the Windows module's
signatures, so a real back end replaces one by name without touching a caller."""

import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

LOGGER = logging.getLogger("Beamer")

NOT_YET = "Linux support is not built yet: Beamer cannot pair, capture or be driven here"

_said: set = set()


def _say(part: str) -> None:
    if part not in _said:
        _said.add(part)
        LOGGER.info("%s is not yet on Linux", part)


# Wire names come from a per-OS key table. Until Linux has one, the default trigger alone is in it
# (evdev's KEY_RIGHTCTRL, 97), so a saved config still validates.
_KEY_RIGHTCTRL = 97


class _Hooks:
    def __init__(self, on_key, on_mouse, on_motion) -> None:
        pass

    def start(self) -> None:
        _say("Keyboard and mouse capture")

    def stop(self) -> None:
        pass


class _Trigger:
    def configure(self, key: str, style: str, window_ms: int) -> None:
        pass

    def feed(self, name: str, down: bool, now: float) -> Optional[str]:
        return None

    def claims(self, name: str, down: bool, redirecting: bool) -> bool:
        return False


capture = SimpleNamespace(
    Hooks=_Hooks,
    Trigger=_Trigger,
    TOGGLE="toggle",
    REDIRECT="redirect",
    RETURN="return",
    VK_TO_NAME={_KEY_RIGHTCTRL: "cmd_r"},
    VK_TITLES={_KEY_RIGHTCTRL: "Right Ctrl"},
    hook_vk=lambda vk, scan: vk,
    input_title=lambda entry: entry,
)

desktop = SimpleNamespace(
    monitors=lambda: [],
    cursor_position=lambda: (0, 0),
    set_cursor_position=lambda x, y: None,
    full_screen_app=lambda: None,
    button_down=lambda name: False,
)

clipboard = SimpleNamespace(
    get_contents=lambda: (None, None),
    changed_contents=lambda: (None, None),
    forget_sync=lambda: None,
    set_contents=lambda text, png: False,
)

autostart = SimpleNamespace(
    installed_exe=lambda: None,
    is_enabled=lambda: False,
    set_enabled=lambda enabled, exe: None,
)

unlock = SimpleNamespace(
    is_locked=lambda: None,
    signal_unlock=lambda: False,
    ensure_unlocked=lambda *args, **kwargs: False,
)

injector = SimpleNamespace(release_all=lambda: None)


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
