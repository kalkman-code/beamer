"""A copy of win_app/app_config.py from release-1.4.x (1.4.3, 60a3b70), its imports pointed at core/ and
platform_parts and nothing else changed. test_settings_migration loads the config.json 1.5.0 leaves
behind through it: a downgrade must still find its pairing.

Configuration loading and saving for the Windows tray application."""

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from platform_parts import capture as capture_win
from core import effects
from core import ignored
from core import protocol
from core import return_edge
import tokens


# Today's two styles and five colours, then every crossing effect and colour pack in effects.py.
# Any colour goes with any style.
GLOW_STYLES = ("glow", "beam") + effects.EFFECT_IDS
GLOW_COLOURS = ("signal", "colourful", "ocean", "sunset", "mono") + effects.PACK_IDS
EDGES = ("left", "right", "top", "bottom")
CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")
METHODS = ("edge", "part", "corner", "shortcut")
TRIGGER_STYLES = ("double_tap", "hold")
# Any named key that is not a character can be the trigger, recorded by pressing it, as on the
# Mac: a modifier, a function key, a navigation key. Stored as its wire name, which is Mac-shaped --
# "cmd_r" is the right Ctrl key on this keyboard -- so every trigger saved before the recorder
# (eight fixed keys, 1.2 to 1.3.1) is still one. Media and browser keys are left out: they are
# what the stays-on-this-PC list is for. The undifferentiated 0x10-0x12 never reach the hook.
_NOT_TRIGGERS = {0x10, 0x11, 0x12} | set(range(0xA6, 0xB4))
TRIGGER_VKS = {
    name: vk for vk, name in capture_win.VK_TO_NAME.items() if vk not in _NOT_TRIGGERS
}
TRIGGER_KEYS = {name: capture_win.VK_TITLES[vk] for name, vk in TRIGGER_VKS.items()}
# Loaded if a config names them, never offered by the recorder, as on the Mac: Backspace, Tab,
# Enter, Esc and Space are typing keys a double-tap or a hold would take from every app, and
# Windows gives a window no key-down for Print Screen, so it cannot be recorded at all.
UNRECORDABLE_TRIGGER_VKS = {0x08, 0x09, 0x0D, 0x1B, 0x20, 0x2C}
MODIFIER_STYLES = ("semantic", "positional")


def palette_colours(colour: str) -> tuple:
    """The colours of a `glow_colour` id: one of today's palettes, else a crossing effect's pack.
    An unknown id is Signal's, as today's glow always drew it."""
    if colour in tokens.PALETTES:
        return tuple(tokens.PALETTES[colour])
    found = effects.pack(colour) if colour in effects.PACK_IDS else None
    return tuple(found[1]) if found else tuple(tokens.PALETTES["signal"])


class ConfigError(Exception):
    pass


@dataclass
class Config:
    host: str
    port: int
    auth_token: str
    reconnect_interval_s: float = 2.0
    # The crossing settings that live here are how this PC's own edge looks: the
    # Mac configures the rest and tells this PC the return edge on every switch.
    edge_glow: bool = True
    glow_style: str = "glow"
    glow_colour: str = "signal"
    # A switch by the shortcut or a menu, not a crossing, plays an arrival around the pointer.
    shortcut_arrival: bool = True
    # What that plays: "match" for whatever the crossing style plays, else see effects.SWITCH_STYLES.
    shortcut_arrival_style: str = "match"
    # How long an effect takes to play through once the pointer crosses: see effects.LENGTHS.
    effect_length: str = "normal"
    # The Mac's name from the last pairing, for the window to say who this PC is paired with.
    paired_with: str = ""
    # Whether each machine may take the other's input. Two plain switches: the
    # receiver, and the outward link.
    allow_mac_to_drive: bool = True
    # Ask GitHub once a day whether a newer release is out; see updates.py.
    check_updates: bool = True
    # Every address the window shows is hidden.
    hide_addresses: bool = False
    # How the Mac's pointer and scroll feel on this PC; see receiver.InputScale.
    pointer_speed: float = 1.0
    scroll_speed: float = 1.0
    reverse_scroll: bool = False
    send_to_mac: bool = True
    # How input leaves this PC. Any combination of the methods can be on, and
    # none of them means the PC can only be driven, never drive.
    crossing_methods: list = field(default_factory=lambda: ["edge", "shortcut"])
    crossing_corner: str = "top_left"
    # "Part of the edge": the thirds of the edge to the Mac that cross, as return_edge.PARTS names them.
    crossing_edge_parts: list = field(default_factory=lambda: ["middle"])
    crossing_resistance_px: int = 120
    trigger_key: str = "cmd_r"
    trigger_style: str = "double_tap"
    double_tap_ms: int = 300
    # How this PC's Ctrl and Windows keys arrive on the Mac, the Mac's own two styles: Semantic
    # makes Ctrl+C Cmd+C there, Positional keeps each key where it sits.
    modifier_style: str = "semantic"
    # A push against the edge with a button held is a drag, not a crossing, as on the Mac.
    block_while_dragging: bool = True
    # The Mac's address is learned, never typed: it is the peer address the
    # Mac's own link arrives from.
    mac_host: str = ""
    # The edge of THIS PC that leads to the Mac, which is both the way home
    # and the way out -- one border, walked either way. It can be set at
    # either machine and is synced over the link, so `arrangement_set_at`
    # (unix seconds) says how recently this end changed it and settles which
    # of two ends that disagree is the newer.
    mac_return_edge: str = ""
    arrangement_set_at: int = 0
    # What the Mac asks for at the return edge. The PC's own push out has its
    # own number, above: one slider for each direction, because the hand does
    # not feel a trackpad and a mouse the same way.
    mac_resistance_px: int = 120
    # The Mac's hardware address, read from this PC's ARP table whenever the link comes up, so a
    # switch that finds the Mac asleep can send it a wake-on-LAN packet. Never typed.
    mac_hardware_address: str = ""
    # Keys and buttons that stay on this PC while its input is on the Mac; see ignored.py.
    ignored_inputs: list = field(default_factory=list)
    # The window's own palette: follow Windows, or keep one. tokens.APPEARANCES is the home of
    # these three values.
    appearance: str = "system"


def migrated_port(value) -> int:
    """The port to use for a config that still carries the old default.

    51820 sits inside the 49152-65535 range Windows hands out for itself, so it was never a
    port anyone could rely on keeping. Nobody chose it -- it was what the app shipped with --
    so it moves; a port a user actually typed is theirs and is left exactly as it is.
    """
    port = int(value)
    return protocol.DEFAULT_PORT if port == protocol.LEGACY_DEFAULT_PORT else port


def default_config() -> Config:
    # `host` is the address this PC shows the Mac; pairing fills it in with the
    # address that faces the Mac, so a fresh install carries none.
    return Config(host="", port=protocol.DEFAULT_PORT, auth_token="")


def default_config_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "Beamer" / "config.json"
    return Path.home() / ".config" / "Beamer" / "config.json"


def validate_config(config: Config) -> None:
    if not isinstance(config.host, str):
        raise ConfigError("host must be text")
    try:
        port = int(config.port)
    except (TypeError, ValueError) as exc:
        raise ConfigError("port must be an integer from 1 to 65535") from exc
    if isinstance(config.port, bool) or not 1 <= port <= 65535:
        raise ConfigError("port must be an integer from 1 to 65535")
    if not isinstance(config.auth_token, str) or not config.auth_token.strip():
        raise ConfigError("auth_token must not be empty")
    if config.auth_token == "CHANGE_ME":
        raise ConfigError("auth_token is still the placeholder")
    if config.trigger_key not in TRIGGER_KEYS:
        raise ConfigError(f"trigger_key must be one of: {', '.join(TRIGGER_KEYS)}")
    if config.trigger_style not in TRIGGER_STYLES:
        raise ConfigError(f"trigger_style must be one of: {', '.join(TRIGGER_STYLES)}")
    try:
        double_tap_ms = int(config.double_tap_ms)
    except (TypeError, ValueError) as exc:
        raise ConfigError("double_tap_ms must be greater than zero") from exc
    if isinstance(config.double_tap_ms, bool) or not 50 <= double_tap_ms <= 2000:
        raise ConfigError("double_tap_ms must be between 50 and 2000")
    if config.modifier_style not in MODIFIER_STYLES:
        raise ConfigError(f"modifier_style must be one of: {', '.join(MODIFIER_STYLES)}")
    if not isinstance(config.block_while_dragging, bool):
        raise ConfigError("block_while_dragging must be true or false")
    if not isinstance(config.mac_hardware_address, str):
        raise ConfigError("mac_hardware_address must be text")
    if not isinstance(config.crossing_methods, list) or any(
        method not in METHODS for method in config.crossing_methods
    ):
        raise ConfigError(f"crossing_methods must be a list of: {', '.join(METHODS)}")
    if config.crossing_corner not in CORNERS:
        raise ConfigError(f"crossing_corner must be one of: {', '.join(CORNERS)}")
    parts = config.crossing_edge_parts
    if not isinstance(parts, list) or not parts or not all(part in return_edge.PARTS for part in parts):
        raise ConfigError(f"crossing_edge_parts must be one or more of: {', '.join(return_edge.PARTS)}")
    for name in ("crossing_resistance_px", "mac_resistance_px"):
        value = getattr(config, name)
        try:
            resistance = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{name} must be a whole number of pixels from 0 to 500") from exc
        if isinstance(value, bool) or not 0 <= resistance <= 500:
            raise ConfigError(f"{name} must be a whole number of pixels from 0 to 500")
    if not isinstance(config.allow_mac_to_drive, bool):
        raise ConfigError("allow_mac_to_drive must be true or false")
    if not isinstance(config.check_updates, bool):
        raise ConfigError("check_updates must be true or false")
    if not isinstance(config.hide_addresses, bool):
        raise ConfigError("hide_addresses must be true or false")
    for name in ("pointer_speed", "scroll_speed"):
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.25 <= value <= 4.0:
            raise ConfigError(f"{name} must be a number from 0.25 to 4")
    if not isinstance(config.reverse_scroll, bool):
        raise ConfigError("reverse_scroll must be true or false")
    try:
        reconnect_interval_s = float(config.reconnect_interval_s)
    except (TypeError, ValueError) as exc:
        raise ConfigError("reconnect_interval_s must be greater than zero") from exc
    if isinstance(config.reconnect_interval_s, bool) or reconnect_interval_s <= 0:
        raise ConfigError("reconnect_interval_s must be greater than zero")
    if not isinstance(config.edge_glow, bool):
        raise ConfigError("edge_glow must be true or false")
    if config.glow_style not in GLOW_STYLES:
        raise ConfigError(f"glow_style must be one of: {', '.join(GLOW_STYLES)}")
    if config.glow_colour not in GLOW_COLOURS:
        raise ConfigError(f"glow_colour must be one of: {', '.join(GLOW_COLOURS)}")
    if not isinstance(config.shortcut_arrival, bool):
        raise ConfigError("shortcut_arrival must be true or false")
    if config.shortcut_arrival_style not in effects.SWITCH_STYLES:
        raise ConfigError(f"shortcut_arrival_style must be one of: {', '.join(effects.SWITCH_STYLES)}")
    lengths = tuple(value for value, _name in effects.LENGTHS)
    if config.effect_length not in lengths:
        raise ConfigError(f"effect_length must be one of: {', '.join(lengths)}")
    if not isinstance(config.paired_with, str):
        raise ConfigError("paired_with must be text")
    if not isinstance(config.send_to_mac, bool):
        raise ConfigError("send_to_mac must be true or false")
    if not isinstance(config.mac_host, str):
        raise ConfigError("mac_host must be text")
    if config.mac_return_edge not in ("",) + EDGES:
        raise ConfigError("mac_return_edge must be an edge name, or empty")
    try:
        ignored.validate(config.ignored_inputs)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if config.appearance not in tokens.APPEARANCES:
        raise ConfigError(f"appearance must be one of: {', '.join(tokens.APPEARANCES)}")


def config_from_dict(raw: dict) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config.json must contain a JSON object")
    missing = [key for key in ("host", "port", "auth_token") if key not in raw]
    if missing:
        raise ConfigError(f"config.json is missing required field(s): {', '.join(missing)}")
    trigger_key = raw.get("trigger_key", "cmd_r")
    try:
        glow_style, glow_colour, switch_style = effects.offered(
            raw.get("glow_style", "glow"), raw.get("glow_colour", "signal"), raw.get("shortcut_arrival_style", "match"))
        config = Config(
            host=raw["host"],
            port=migrated_port(raw["port"]),
            auth_token=raw["auth_token"],
            reconnect_interval_s=float(raw.get("reconnect_interval_s", 2.0)),
            edge_glow=raw.get("edge_glow", True),
            glow_style=glow_style,
            glow_colour=glow_colour,
            shortcut_arrival=raw.get("shortcut_arrival", True),
            shortcut_arrival_style=switch_style,
            effect_length=raw.get("effect_length", "normal"),
            paired_with=raw.get("paired_with", "") or "",
            allow_mac_to_drive=raw.get("allow_mac_to_drive", True),
            check_updates=raw.get("check_updates", True),
            hide_addresses=raw.get("hide_addresses", False),
            pointer_speed=raw.get("pointer_speed", 1.0),
            scroll_speed=raw.get("scroll_speed", 1.0),
            reverse_scroll=raw.get("reverse_scroll", False),
            send_to_mac=raw.get("send_to_mac", True),
            crossing_methods=list(raw.get("crossing_methods", ["edge", "shortcut"])),
            crossing_corner=raw.get("crossing_corner", "top_left"),
            crossing_edge_parts=raw.get("crossing_edge_parts", ["middle"]),
            crossing_resistance_px=int(raw.get("crossing_resistance_px", 120)),
            trigger_key=trigger_key,
            trigger_style=raw.get("trigger_style", "double_tap"),
            double_tap_ms=int(raw.get("double_tap_ms", 300)),
            modifier_style=raw.get("modifier_style", "semantic"),
            block_while_dragging=raw.get("block_while_dragging", True),
            mac_hardware_address=raw.get("mac_hardware_address", "") or "",
            mac_host=raw.get("mac_host", "") or "",
            mac_return_edge=raw.get("mac_return_edge", "") or "",
            arrangement_set_at=int(raw.get("arrangement_set_at", 0)),
            mac_resistance_px=int(raw.get("mac_resistance_px", 120)),
            ignored_inputs=raw.get("ignored_inputs", []),
            appearance=raw.get("appearance") if raw.get("appearance") in tokens.APPEARANCES else "system",
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"config.json contains an invalid value: {exc}") from exc
    validate_config(config)
    return config


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"config file contains invalid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"config file could not be read: {exc}") from exc
    config = config_from_dict(raw)
    if config.port != int(raw["port"]):
        try:
            save_config(path, config)
        except (ConfigError, OSError):
            # The port in hand is what matters; a config file that cannot be written must
            # not stop Beamer listening on it.
            pass
    return config


def config_to_dict(config: Config) -> dict:
    validate_config(config)
    return {
        "host": config.host.strip(),
        "port": int(config.port),
        "auth_token": config.auth_token,
        "reconnect_interval_s": float(config.reconnect_interval_s),
        "edge_glow": config.edge_glow,
        "glow_style": config.glow_style,
        "glow_colour": config.glow_colour,
        "shortcut_arrival": config.shortcut_arrival,
        "shortcut_arrival_style": config.shortcut_arrival_style,
        "effect_length": config.effect_length,
        "paired_with": config.paired_with,
        "allow_mac_to_drive": config.allow_mac_to_drive,
        "check_updates": config.check_updates,
        "hide_addresses": config.hide_addresses,
        "pointer_speed": config.pointer_speed,
        "scroll_speed": config.scroll_speed,
        "reverse_scroll": config.reverse_scroll,
        "send_to_mac": config.send_to_mac,
        "crossing_methods": list(config.crossing_methods),
        "crossing_corner": config.crossing_corner,
        "crossing_edge_parts": list(config.crossing_edge_parts),
        "crossing_resistance_px": int(config.crossing_resistance_px),
        "trigger_key": config.trigger_key,
        "trigger_style": config.trigger_style,
        "double_tap_ms": int(config.double_tap_ms),
        "modifier_style": config.modifier_style,
        "block_while_dragging": config.block_while_dragging,
        "mac_hardware_address": config.mac_hardware_address,
        "mac_host": config.mac_host,
        "mac_return_edge": config.mac_return_edge,
        "arrangement_set_at": int(config.arrangement_set_at),
        "mac_resistance_px": int(config.mac_resistance_px),
        "ignored_inputs": list(config.ignored_inputs),
        "appearance": config.appearance,
    }


def save_config(path: Path, config: Config) -> None:
    payload = config_to_dict(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
