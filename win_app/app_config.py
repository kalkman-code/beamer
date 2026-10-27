"""Configuration loading and saving for the Windows tray application.

The file is settings.json (WIRE.md section 1), migrated from 1.4.x's config.json on the first start
and never written back to it. `Config` is the flat view the window, the sender and the receiver have
always read: this PC's own settings, and the first desktop peer as the fields that used to be the Mac's
(`mac_host`, `auth_token`, `send_to_mac` and the rest). Crossing events use core.crossing_state;
the picture derives each peer's side and zones while ways retain the user's choices. Saving the
flat view cannot restore stale crossing state. Unrelated settings survive every save."""

import base64
import binascii
import copy
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from platform_parts import capture as capture_win
from core import effects
from core import ignored
from core import peerlist
from core import protocol
from core import receiver
from core import i18n
from core import return_edge
from core import ways, crossing_state
from core import profiles
from core import portable_runtime
from core.portable_store import PortableStoreError
from core.pairing import local_address_towards
import tokens

LOGGER = logging.getLogger(__name__)
_PROFILE_LOAD_NOTES = {}


def load_notes(path: Path) -> list:
    """Consume notices discovered while loading this file, without storing UI flags in it."""
    with SETTINGS_LOCK:
        return sorted(_PROFILE_LOAD_NOTES.pop(Path(path).resolve(), set()))


def _remember_profile_notes(path, notes):
    if "older_version" in notes:
        _PROFILE_LOAD_NOTES.setdefault(Path(path).resolve(), set()).add("older_version")

try:
    PROFILE_APP_VERSION = (Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent.parent)
                           / "VERSION").read_text().strip()
except OSError:
    PROFILE_APP_VERSION = profiles.APP_VERSION


# Today's two styles and five colours, then every crossing effect and colour pack in effects.py.
# Any colour goes with any style.
GLOW_STYLES = ("glow", "beam") + effects.EFFECT_IDS
GLOW_COLOURS = tuple(tokens.PALETTES) + effects.PACK_IDS
EDGES = ("left", "right", "top", "bottom")
CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")
METHODS = ("edge", "part", "corner", "shortcut")
TRIGGER_STYLES = ("double_tap", "hold")
# Any named key that is not a character can be the trigger, recorded by pressing it, as on the
# Mac: a modifier, a function key, a navigation key. Stored as its wire name, which is Mac-shaped --
# "cmd_r" is the right Ctrl key on this keyboard -- so every trigger saved before the recorder
# (eight fixed keys, 1.2 to 1.3.1) is still one. Which keys are left out, and which are loaded but
# never offered, are the capture's, in its own key codes.
TRIGGER_VKS = {
    name: vk for vk, name in capture_win.VK_TO_NAME.items() if vk not in capture_win.NOT_TRIGGER_VKS
}
TRIGGER_KEYS = {name: capture_win.VK_TITLES[vk] for name, vk in TRIGGER_VKS.items()}
UNRECORDABLE_TRIGGER_VKS = capture_win.UNRECORDABLE_TRIGGER_VKS
MODIFIER_STYLES = ("semantic", "positional", "mac_layout")


def palette_colours(colour: str) -> tuple:
    """The colours of a `glow_colour` id: one of today's palettes, else a crossing effect's pack.
    An unknown id is Signal's, as today's glow always drew it."""
    if colour in tokens.PALETTES:
        return tuple(tokens.PALETTES[colour])
    found = effects.pack(colour) if colour in effects.PACK_IDS else None
    return tuple(found[1]) if found else tuple(tokens.PALETTES["signal"])


class ConfigError(Exception):
    pass


class SettingsFileError(ConfigError):
    """settings.json exists but this version cannot use it (another schema, a hand edit that broke
    it). Nothing is written over it."""


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
    glow_colour: str = "colourful"
    # A switch by the shortcut or a menu, not a crossing, plays an arrival around the pointer.
    shortcut_arrival: bool = True
    # What that plays: "match" for whatever the crossing style plays, else see effects.SWITCH_STYLES.
    shortcut_arrival_style: str = "match"
    # How long an effect takes to play through once the pointer crosses: see effects.LENGTHS.
    effect_length: str = "normal"
    effect_size: str = "medium"
    effect_pace: float = 1.0
    effect_size_fraction: float = 0.045
    frame_rate: str = "display"
    # The Mac's name from the last pairing, for the window to say who this PC is paired with.
    paired_with: str = ""
    # Whether each machine may take the other's input. Two plain switches: the
    # receiver, and the outward link.
    allow_mac_to_drive: bool = True
    # Ask GitHub once a day whether a newer release is out; see updates.py.
    check_updates: bool = True
    # Every address the window shows is hidden.
    hide_addresses: bool = False
    language: str = "auto"
    keyboard_sync: bool = False
    # Same on all machines: the Crossing and Design pages kept in step with the Mac's, and the unix
    # seconds of the last change to that or to a shared value; see settings_sync.
    same_on_both: bool = False
    same_set_at: int = 0
    same_groups: list = field(default_factory=list)
    # The machine that made that last change, its id as base64: the larger id wins a tie.
    same_by: str = ""
    # Each shared value's own stamp, {key: [set_at, by]}, since 1.5.2; a key with none takes
    # same_set_at and same_by (settings_sync.held_stamps).
    same_stamps: dict = field(default_factory=dict)
    design_set_at: int = 0
    design_by: str = ""
    design_follow_peer: str = ""
    # How the Mac's pointer and scroll feel on this PC; see receiver.InputScale.
    pointer_speed: float = 1.0
    scroll_speed: float = 1.0
    reverse_scroll: bool = False
    send_to_mac: bool = True
    # How input leaves this PC: "shortcut" is this PC's own and is saved from here; the rest, with the
    # corner, the thirds, `mac_return_edge` and `arrangement_set_at`, are the first peer's ways as
    # loaded, never saved from here (set_ways writes each peer's).
    crossing_methods: list = field(default_factory=lambda: ["edge", "shortcut"])
    crossing_corner: str = "top_left"
    # "Part of the edge": the thirds of the edge to the Mac that cross, as return_edge.PARTS names them.
    crossing_edge_parts: list = field(default_factory=lambda: ["middle"])
    crossing_resistance_px: int = 120
    shortcut_goes_to: str = "last"
    trigger_key: str = "cmd_r"
    trigger_style: str = "double_tap"
    double_tap_ms: int = 300
    # How this PC's Ctrl and Windows keys arrive on the Mac, the Mac's own two styles: Semantic
    # makes Ctrl+C Cmd+C there, Positional keeps each key where it sits.
    modifier_style: str = "semantic"
    # A push against the edge with a button held is a drag, not a crossing, as on the Mac.
    block_while_dragging: bool = True
    # A full-screen app in front holds this PC's edges. This PC's own choice, never shared.
    hold_full_screen: bool = False
    # The tray icon, one key on all three apps (the Mac's is the menu bar item). This machine's own
    # choice, never shared.
    show_in_tray: bool = True
    # Only the machine in use shows a pointer: this PC's while it drives another, and after a driver
    # lets go of it until it moves (core/idle_pointer). This machine's own choice, never shared.
    hide_idle_pointer: bool = True
    # Linux on Wayland: a key combination bound through the desktop's GlobalShortcuts portal sends
    # input out while it is at home, where the double-tap cannot be seen. Off until chosen, since
    # turning it on asks the desktop's permission. This machine's own choice, never shared.
    home_shortcut: bool = False
    # The Mac's address is learned, never typed: it is the peer address the
    # Mac's own link arrives from.
    mac_host: str = ""
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
    mouse_only: bool = False
    # The window's own palette: follow Windows, or keep one. tokens.APPEARANCES is the home of
    # these three values.
    appearance: str = "system"
    # This machine's identity from settings.json (WIRE.md section 1), for the links and Same on all
    # machines; empty until a settings file has given it one. The id never changes once made.
    machine_id: str = ""
    name: str = ""
    layout: dict = field(default_factory=dict)
    layout_set_at: int = 0
    layout_by: str = ""


def _stamp(value) -> int:
    """A saved unix-seconds stamp, or 0 for anything that is not one."""
    return int(value) if not isinstance(value, bool) and isinstance(value, (int, float)) and value > 0 else 0


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
    if portable_runtime.enabled():
        return portable_runtime.settings_path()
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "Beamer" / "settings.json"
    return Path.home() / ".config" / "Beamer" / "settings.json"


def legacy_config_path(path: Path) -> Path:
    """1.4.x's config.json, which sits beside settings.json and is only ever read."""
    return path.with_name("config.json")


def validate_config(config: Config) -> None:
    if not isinstance(config.host, str):
        raise ConfigError("host must be text")
    try:
        port = int(config.port)
    except (TypeError, ValueError) as exc:
        raise ConfigError("port must be an integer from 1 to 65535") from exc
    if isinstance(config.port, bool) or not 1 <= port <= 65535:
        raise ConfigError("port must be an integer from 1 to 65535")
    # Empty is a machine with nothing paired: it still has settings of its own to keep.
    if not isinstance(config.auth_token, str):
        raise ConfigError("auth_token must be text")
    if config.auth_token == "CHANGE_ME":
        raise ConfigError("auth_token is still the placeholder")
    if not isinstance(config.layout, dict) or not isinstance(config.layout_by, str):
        raise ConfigError("layout is invalid")
    if config.trigger_key not in TRIGGER_KEYS:
        raise ConfigError(f"trigger_key must be one of: {', '.join(TRIGGER_KEYS)}")
    if config.trigger_style not in TRIGGER_STYLES:
        raise ConfigError(f"trigger_style must be one of: {', '.join(TRIGGER_STYLES)}")
    if config.shortcut_goes_to not in ("last", "next", "ticked"):
        raise ConfigError("shortcut_goes_to must be last, next or ticked")
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
    if not isinstance(config.language, str) or (config.language != "auto" and config.language not in i18n.LANGUAGES):
        raise ConfigError("language must be auto or a supported language code")
    if not isinstance(config.hold_full_screen, bool):
        raise ConfigError("hold_full_screen must be true or false")
    if not isinstance(config.show_in_tray, bool):
        raise ConfigError("show_in_tray must be true or false")
    if not isinstance(config.hide_idle_pointer, bool):
        raise ConfigError("hide_idle_pointer must be true or false")
    if not isinstance(config.home_shortcut, bool):
        raise ConfigError("home_shortcut must be true or false")
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
    if not isinstance(config.same_on_both, bool):
        raise ConfigError("same_on_both must be true or false")
    if (not isinstance(config.same_groups, list)
            or any(group not in ("crossing", "design", "shortcut", "names") for group in config.same_groups)):
        raise ConfigError("same_groups must contain known group names")
    if not isinstance(config.same_stamps, dict):
        raise ConfigError("same_stamps must be an object")
    for name in ("pointer_speed", "scroll_speed"):
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.25 <= value <= 4.0:
            raise ConfigError(f"{name} must be a number from 0.25 to 4")
    if not isinstance(config.reverse_scroll, bool):
        raise ConfigError("reverse_scroll must be true or false")
    if not isinstance(config.mouse_only, bool):
        raise ConfigError("mouse_only must be true or false")
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
    sizes = tuple(value for value, _name in effects.SIZES)
    if config.effect_size not in sizes:
        raise ConfigError(f"effect_size must be one of: {', '.join(sizes)}")
    if not effects.valid_effect_pace(config.effect_pace):
        raise ConfigError("effect_pace must be a number from 0.25 to 2.5")
    if not effects.valid_effect_size_fraction(config.effect_size_fraction):
        raise ConfigError("effect_size_fraction must be a number from 0.02 to 0.25")
    if config.frame_rate not in tuple(value for value, _name, _divisor in effects.FRAME_RATES):
        raise ConfigError("frame_rate must be one of: display, half, quarter")
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
    if config.machine_id and not is_machine_id(config.machine_id):
        raise ConfigError("machine_id must be a machine id")
    if not isinstance(config.name, str):
        raise ConfigError("name must be text")


def config_from_dict(raw: dict) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config.json must contain a JSON object")
    missing = [key for key in ("host", "port", "auth_token") if key not in raw]
    if missing:
        raise ConfigError(f"config.json is missing required field(s): {', '.join(missing)}")
    lengths = tuple(value for value, _name in effects.LENGTHS)
    if raw.get("effect_length", "normal") not in lengths:
        raise ConfigError(f"effect_length must be one of: {', '.join(lengths)}")
    sizes = tuple(value for value, _name in effects.SIZES)
    if raw.get("effect_size", "medium") not in sizes:
        raise ConfigError(f"effect_size must be one of: {', '.join(sizes)}")
    trigger_key = raw.get("trigger_key", "cmd_r")
    try:
        glow_style, glow_colour, switch_style = effects.offered(
            raw.get("glow_style", "glow"), raw.get("glow_colour", "colourful"), raw.get("shortcut_arrival_style", "match"))
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
            effect_size=raw.get("effect_size", "medium"),
            effect_pace=raw.get("effect_pace"),
            effect_size_fraction=raw.get("effect_size_fraction"),
            frame_rate=raw.get("frame_rate", "display"),
            paired_with=raw.get("paired_with", "") or "",
            allow_mac_to_drive=raw.get("allow_mac_to_drive", True),
            check_updates=raw.get("check_updates", True),
            hide_addresses=raw.get("hide_addresses", False),
            language=raw.get("language", "auto"),
            keyboard_sync=raw.get("keyboard_sync") is True,
            same_on_both=raw.get("same_on_both", False),
            same_set_at=_stamp(raw.get("same_set_at", 0)),
            same_groups=raw.get("same_groups", ["crossing", "design", "shortcut", "names"]
                                if raw.get("same_on_both", False) else []),
            same_by=raw.get("same_by", "") or "",
            same_stamps=dict(raw["same_stamps"]) if isinstance(raw.get("same_stamps"), dict) else {},
            design_set_at=_stamp(raw.get("design_set_at", 0)),
            design_by=raw.get("design_by", "") or "",
            design_follow_peer=raw.get("design_follow_peer", "") or "",
            pointer_speed=raw.get("pointer_speed", 1.0),
            scroll_speed=raw.get("scroll_speed", 1.0),
            reverse_scroll=raw.get("reverse_scroll", False),
            send_to_mac=raw.get("send_to_mac", True),
            crossing_methods=list(raw.get("crossing_methods", ["edge", "shortcut"])),
            crossing_corner=raw.get("crossing_corner", "top_left"),
            crossing_edge_parts=raw.get("crossing_edge_parts", ["middle"]),
            crossing_resistance_px=int(raw.get("crossing_resistance_px", 120)),
            shortcut_goes_to=raw.get("shortcut_goes_to", "last"),
            trigger_key=trigger_key,
            trigger_style=raw.get("trigger_style", "double_tap"),
            double_tap_ms=int(raw.get("double_tap_ms", 300)),
            modifier_style=raw.get("modifier_style", "semantic"),
            block_while_dragging=raw.get("block_while_dragging", True),
            hold_full_screen=raw.get("hold_full_screen", False),
            show_in_tray=raw.get("show_in_tray", True),
            hide_idle_pointer=raw.get("hide_idle_pointer", True),
            home_shortcut=raw.get("home_shortcut", False),
            mac_hardware_address=raw.get("mac_hardware_address", "") or "",
            mac_host=raw.get("mac_host", "") or "",
            mac_return_edge=raw.get("mac_return_edge", "") or "",
            arrangement_set_at=int(raw.get("arrangement_set_at", 0)),
            mac_resistance_px=int(raw.get("mac_resistance_px", 120)),
            ignored_inputs=raw.get("ignored_inputs", []),
            mouse_only=raw.get("mouse_only", False),
            appearance=raw.get("appearance") if raw.get("appearance") in tokens.APPEARANCES else "system",
            machine_id=raw.get("machine_id", "") or "",
            name=raw.get("name", "") or "",
            layout=raw.get("layout", {}) or {},
            layout_set_at=_stamp(raw.get("layout_set_at", 0)),
            layout_by=raw.get("layout_by", "") or "",
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"config.json contains an invalid value: {exc}") from exc
    (config.effect_pace, config.effect_size_fraction,
     config.effect_length, config.effect_size) = effects.design_values(
        config.effect_pace, config.effect_size_fraction, config.effect_length, config.effect_size)
    validate_config(config)
    return config


def load_config(path: Path, unpaired_ok: bool = False) -> Config:
    """The flat config from settings.json at `path`, migrating 1.4.x's config.json first when there
    is no settings file yet. A machine with no peer is unpaired, which raises ConfigError as a missing
    config always did, unless `unpaired_ok`: the window then gets this machine's own settings, with no
    token, to show and keep."""
    try:
        settings = load_settings(path)
    except OSError as exc:
        raise ConfigError(f"settings could not be read or written: {exc}") from exc
    if not settings["peers"] and not unpaired_ok:
        raise ConfigError("nothing is paired yet")
    config = config_from_dict(_flat_from_settings(settings))
    return replace(config, host=_own_address(config.mac_host))


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
        "effect_size": config.effect_size,
        "effect_pace": config.effect_pace,
        "effect_size_fraction": config.effect_size_fraction,
        "frame_rate": config.frame_rate,
        "paired_with": config.paired_with,
        "allow_mac_to_drive": config.allow_mac_to_drive,
        "check_updates": config.check_updates,
        "hide_addresses": config.hide_addresses,
        "language": config.language,
        "same_on_both": config.same_on_both,
        "same_set_at": int(config.same_set_at),
        "same_groups": list(config.same_groups),
        "same_by": config.same_by,
        "same_stamps": copy.deepcopy(config.same_stamps),
        "design_set_at": int(config.design_set_at),
        "design_by": config.design_by,
        "design_follow_peer": config.design_follow_peer,
        "pointer_speed": config.pointer_speed,
        "scroll_speed": config.scroll_speed,
        "reverse_scroll": config.reverse_scroll,
        "send_to_mac": config.send_to_mac,
        "crossing_methods": list(config.crossing_methods),
        "crossing_corner": config.crossing_corner,
        "crossing_edge_parts": list(config.crossing_edge_parts),
        "crossing_resistance_px": int(config.crossing_resistance_px),
        "shortcut_goes_to": config.shortcut_goes_to,
        "trigger_key": config.trigger_key,
        "trigger_style": config.trigger_style,
        "double_tap_ms": int(config.double_tap_ms),
        "modifier_style": config.modifier_style,
        "keyboard_sync": config.keyboard_sync,
        "block_while_dragging": config.block_while_dragging,
        "hold_full_screen": config.hold_full_screen,
        "show_in_tray": config.show_in_tray,
        "hide_idle_pointer": config.hide_idle_pointer,
        "home_shortcut": config.home_shortcut,
        "mac_hardware_address": config.mac_hardware_address,
        "mac_host": config.mac_host,
        "mac_return_edge": config.mac_return_edge,
        "arrangement_set_at": int(config.arrangement_set_at),
        "mac_resistance_px": int(config.mac_resistance_px),
        "ignored_inputs": list(config.ignored_inputs),
        "mouse_only": config.mouse_only,
        "appearance": config.appearance,
        "machine_id": config.machine_id,
        "name": config.name,
        "layout": config.layout,
        "layout_set_at": int(config.layout_set_at),
        "layout_by": config.layout_by,
    }


# -- settings.json (WIRE.md sections 1 and 8) ---------------------------------------------------

SCHEMA = 6
MAX_PEERS = 32
# Flat fields that settings.json keeps somewhere else (a peer entry, the zones, or the top level's
# own `port` and `name`) or no longer keeps (the PC's own address, the Mac's resistance).
_ELSEWHERE = {
    "host", "port", "auth_token", "paired_with", "mac_host", "mac_hardware_address", "send_to_mac",
    "allow_mac_to_drive", "mac_return_edge", "arrangement_set_at", "crossing_methods", "crossing_corner",
    "crossing_edge_parts", "mac_resistance_px", "machine_id", "name",
}
# Every other field keeps its 1.4.x name and value at the top level.
KEPT_FIELDS = tuple(item.name for item in fields(Config) if item.name not in _ELSEWHERE)
KINDS = ("edge", "part", "corner")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text, length: int):
    """The bytes of a `b64` text of that length, or None: it must encode back to the same text."""
    if not isinstance(text, str) or not text.isascii():
        return None
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) == length and _b64(raw) == text else None


def new_machine_id() -> str:
    while True:
        raw = os.urandom(16)
        if raw != bytes(16):
            return _b64(raw)


def is_machine_id(text) -> bool:
    raw = _unb64(text, 16)
    return raw is not None and raw != bytes(16)


def is_paired_token(token) -> bool:
    """A token pairing made: 43 characters of `b64`. Any other is one a user typed into 1.4.x."""
    return _unb64(token, 32) is not None


def key_id(token: str) -> bytes:
    """The pair's key id (WIRE.md section 2), computed from a paired token when needed and never stored."""
    return protocol.hkdf_sha256(token.encode("utf-8"), b"beamer-link-v6", b"beamer-key-id")[:16]


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _flag(value, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _legacy_port(raw: dict) -> int:
    try:
        return migrated_port(raw.get("port", protocol.DEFAULT_PORT))
    except (TypeError, ValueError):
        return protocol.DEFAULT_PORT


def _corner_edge(corner: str, side: str) -> str:
    """The edge a corner zone crosses: the side, as 1.4.x built the PC's corner push with the return
    edge whichever edges the corner touches (WIRE.md section 8); the corner's own left or right edge
    until a side is chosen."""
    return side if side in return_edge.EDGES else corner.split("_")[1]


def _peer_entry(raw: dict, token: str, port: int, own_id: str) -> dict:
    """A peer entry for a 1.4.x pairing, from a dict in 1.4.x's field names: a migrated config.json,
    or the flat fields of a Config that has just paired."""
    side = raw.get("mac_return_edge")
    side = side if side in EDGES else ""
    set_at = _stamp(raw.get("arrangement_set_at", 0))
    methods, corner, parts = _zone_fields(raw)
    return {
        "id": "", "name": _text(raw.get("paired_with")), "platform": "macos", "token": token,
        "host": _text(raw.get("mac_host")), "port": port, "hw": _text(raw.get("mac_hardware_address")),
        "send": _flag(raw.get("send_to_mac"), True), "allow_drive": _flag(raw.get("allow_mac_to_drive"), True),
        "in_use": True,
        "side": side, "side_set_at": set_at, "side_by": own_id if set_at else "",
        "paired_with": [], "paired_at": 0, "linked": False, "from_1_4": True,
        "ways": {"edge": bool(set(methods) & {"edge", "part"}),
                 "thirds": list(ways.PARTS) if "edge" in methods else parts,
                 "corner": "corner" in methods, "corner_at": corner, "notch": False},
    }


def _zone_fields(legacy: dict):
    methods = legacy.get("crossing_methods", ["edge", "shortcut"])
    methods = methods if isinstance(methods, list) else ["edge", "shortcut"]
    # The whole edge covers its thirds, so both on would be two zones over one stretch (WIRE.md section 8).
    methods = [m for m in methods if not (m == "part" and "edge" in methods)]
    corner = legacy.get("crossing_corner")
    parts = legacy.get("crossing_edge_parts")
    if not isinstance(parts, list) or not parts or not all(part in return_edge.PARTS for part in parts):
        parts = ["middle"]
    return methods, (corner if corner in CORNERS else "top_left"), parts


def _zones_from_methods(peer_id: str, legacy: dict, side: str) -> list:
    methods, corner, parts = _zone_fields(legacy)
    rest = {"edge": {}, "part": {"parts": list(parts)}, "corner": {"corner": corner, "edge": _corner_edge(corner, side)}}
    return [{"peer": peer_id, "kind": kind, **rest[kind], **({} if kind in methods else {"off": True})} for kind in KINDS]


def migrate(legacy, machine_id=None) -> dict:
    """The settings for a config.json in 1.4.x's shape (`None` when there is none) and a new id."""
    machine_id = machine_id or new_machine_id()
    settings = {
        "schema": SCHEMA, "machine_id": machine_id, "name": "", "port": protocol.DEFAULT_PORT, "shortcut": True,
        "peers": [], "zones": [], "migrated_token_sha256": "", "layout": {}, "layout_set_at": 0, "layout_by": "",
    }
    if not isinstance(legacy, dict):
        return settings
    settings.update({key: legacy[key] for key in KEPT_FIELDS if key in legacy})
    settings["port"] = _legacy_port(legacy)
    methods, _corner, _parts = _zone_fields(legacy)
    settings["shortcut"] = "shortcut" in methods
    token = _text(legacy.get("auth_token"))
    if token:
        settings["migrated_token_sha256"] = token_sha256(token)
        peer = _peer_entry(legacy, token, settings["port"], machine_id)
        settings["peers"].append(peer)
        settings["zones"] = _zones_from_methods("", legacy, peer["side"])
    return settings


def remigrate(settings: dict, legacy: dict) -> bool:
    """A config.json whose token is not the one already migrated is a pairing made under 1.4.x after
    the upgrade: it replaces the `from_1_4` entry, or comes back if the user removed it. True when the
    settings changed."""
    token = _text(legacy.get("auth_token"))
    if not token or token_sha256(token) == settings["migrated_token_sha256"]:
        return False
    settings["migrated_token_sha256"] = token_sha256(token)
    peers, zones = settings["peers"], settings["zones"]
    old = next((index for index, peer in enumerate(peers) if peer.get("from_1_4") and peer.get("port") != 0), None)
    if is_paired_token(token) and any(
        is_paired_token(peer.get("token")) and key_id(peer["token"]) == key_id(token)
        for index, peer in enumerate(peers) if index != old
    ):
        return True
    entry = _peer_entry(legacy, token, _legacy_port(legacy), settings["machine_id"])
    if old is not None:
        for zone in zones:
            if zone.get("peer") == peers[old].get("id"):
                zone["peer"] = ""
        peers[old] = entry
    elif len(peers) < MAX_PEERS:
        peers.append(entry)
        zones.extend(_zones_from_methods("", legacy, entry["side"]))
    return True


def learn_peer_id(settings: dict, index: int, peer_id: str) -> None:
    """The first link with a migrated entry says who it is: fill the id in and point the zones that
    named the empty id at it."""
    peers = settings["peers"]
    if not is_machine_id(peer_id):
        raise ConfigError("a peer id must be a machine id")
    if peers[index].get("id"):
        raise ConfigError("that peer already has an id")
    if peer_id == settings["machine_id"] or any(peer.get("id") == peer_id for peer in peers):
        raise ConfigError("two machines cannot share an id")
    for zone in settings["zones"]:
        if zone.get("peer") == "":
            zone["peer"] = peer_id
    peers[index]["id"] = peer_id


def _refuse_unreadable_target(path: Path) -> None:
    """Every writer ends here, so a settings file this build cannot read (a newer Beamer's, or damaged)
    is never replaced by one written from defaults; the file is the user's to move aside."""
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        existing = None
    if not isinstance(existing, dict) or existing.get("schema") != SCHEMA:
        raise SettingsFileError(f"{path} is not a version {SCHEMA} Beamer settings file, and is left as it is")


def _write_json(path: Path, payload: dict) -> None:
    """A new file beside the old, then a rename over it."""
    if portable_runtime.enabled():
        try:
            _portable_store(path).write(payload)
        except PortableStoreError as exc:
            raise SettingsFileError("Portable settings could not be saved") from exc
        return
    _refuse_unreadable_target(path)
    _backup_before_profile_upgrade(path)
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


def _read_object(path: Path):
    """The JSON object in a file, or None when it is missing or does not parse. A file that cannot be
    read for any other reason (locked, denied) raises, so it is never taken for a missing one."""
    if portable_runtime.enabled():
        if Path(path).resolve() == legacy_config_path(portable_runtime.settings_path()).resolve():
            return None
        try:
            settings = _portable_store(path).read()
        except PortableStoreError as exc:
            raise SettingsFileError("Portable settings could not be read") from exc
        if settings:
            settings["port"] = portable_runtime.reserve_listener(settings.get("port", 0))
        return settings
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _portable_store(path):
    if Path(path).resolve() != portable_runtime.settings_path().resolve():
        raise SettingsFileError("Portable settings must stay beside the app")
    if portable_runtime.store is None or not portable_runtime.store.is_unlocked:
        raise SettingsFileError("Portable settings are locked")
    return portable_runtime.store


def load_settings(path: Path) -> dict:
    """settings.json at `path`, made from 1.4.x's config.json beside it (or defaults) when there is
    none, and brought up to date when 1.4.x paired again after the upgrade."""
    if legacy_config_path(path) == path:
        raise ConfigError("the settings file is settings.json; config.json is 1.4.x's and is never written")
    legacy = _read_object(legacy_config_path(path))
    if not path.exists() or (portable_runtime.enabled() and not _portable_store(path).read()):
        settings = migrate(legacy)
        if portable_runtime.enabled():
            settings["port"] = portable_runtime.reserve_listener()
        settings, notes = profiles.before_load(settings)
        _remember_profile_notes(path, notes)
        settings = crossing_state.migrate(settings, "pc")["settings"]
        settings = crossing_state.paired(settings, platform="pc", own_displays={}, now=time.time())["settings"]
        settings, _ = profiles.after_load(settings, platform="pc", now=time.time(), app=PROFILE_APP_VERSION)
        _write_json(path, settings)
        return settings
    settings = _read_object(path)
    if (
        settings is None or settings.get("schema") != SCHEMA or not is_machine_id(settings.get("machine_id"))
        or not isinstance(settings.get("peers"), list) or not isinstance(settings.get("zones"), list)
        or not all(isinstance(entry, dict) for entry in settings["peers"] + settings["zones"])
    ):
        raise SettingsFileError(f"{path} is not a version {SCHEMA} Beamer settings file, and is left as it is")
    original = copy.deepcopy(settings)
    settings, notes = profiles.before_load(settings)
    _remember_profile_notes(path, notes)
    if any("way_back_by" in entry and not isinstance(entry["way_back_by"], str) for entry in settings["peers"]):
        raise SettingsFileError(f"{path} has a peer way_back_by that is not text, and is left as it is")
    if any("in_use" in entry and not isinstance(entry["in_use"], bool) for entry in settings["peers"]):
        raise SettingsFileError(f"{path} has a peer in_use that is not true or false, and is left as it is")
    phones_repaired = peerlist.normalise_phones(settings)
    if any("jump_key" in entry and (not isinstance(entry["jump_key"], str)
                                    or (entry["jump_key"] and not ways.valid_jump_key(entry["jump_key"])))
           for entry in settings["peers"]):
        raise SettingsFileError(f"{path} has a peer jump_key that is not a recorded modifier chord, and is left as it is")
    settings.setdefault("name", "")
    settings.setdefault("port", protocol.DEFAULT_PORT)
    settings.setdefault("shortcut", True)
    settings.setdefault("migrated_token_sha256", "")
    settings.setdefault("layout", {})
    settings.setdefault("layout_set_at", 0)
    settings.setdefault("layout_by", "")
    if (not isinstance(settings["layout"], dict) or type(settings["layout_set_at"]) is not int
            or not 0 <= settings["layout_set_at"] <= 2 ** 53 - 2
            or not isinstance(settings["layout_by"], str)
            or settings["layout_by"] and not is_machine_id(settings["layout_by"])):
        raise SettingsFileError(f"{path} has an invalid layout")
    if (len(settings["layout"]) > MAX_PEERS + 1 or any(
            not is_machine_id(machine) or not isinstance(tile, dict)
            or any(type(tile.get(key)) is not int for key in ("x", "y", "w", "h"))
            or tile["w"] < 1 or tile["h"] < 1 or not ways._valid_tile_displays(tile)
            for machine, tile in settings["layout"].items())):
        raise SettingsFileError(f"{path} has an invalid layout tile")
    remigrated = legacy is not None and remigrate(settings, legacy)
    legacy_edges = [zone.get("peer") for zone in settings["zones"]
                    if zone.get("kind") == "edge" and not zone.get("off") and not zone.get("page_off")]
    intents_repaired = False
    for entry in settings["peers"]:
        if "ways" not in entry and entry.get("port") != 0 and entry.get("id", "") in legacy_edges:
            # Legacy whole edges covered thirds; retain that precedence before canonical allocation.
            entry["ways"] = ways._read_intent(settings, entry.get("id", ""))
            entry["ways"]["thirds"] = list(ways.PARTS)
            intents_repaired = True
        elif not entry.get("id") and entry.get("port") != 0 and "ways" not in entry:
            entry["ways"] = ways._read_intent(settings, "")
    outcome = crossing_state.migrate(settings, "pc")
    settings = outcome["settings"]
    if remigrated:
        settings = crossing_state.paired(settings, platform="pc", own_displays={}, now=time.time())["settings"]
    settings, _ = profiles.after_load(settings, platform="pc", now=time.time(), app=PROFILE_APP_VERSION)
    if (remigrated or phones_repaired or intents_repaired or outcome["changed"] or "older_version" in notes
            or _without_profile_state(settings) != _without_profile_state(original)
            # Once the file holds profile state, a load's reconcile (its status, an older Beamer's
            # edit folded in) is saved; only an upgrade waits for the first real save.
            or "crossing_own" in original and settings != original):
        _write_json(path, settings)
    return settings


def _own_address(target: str) -> str:
    if not target:
        return ""
    try:
        return local_address_towards(target)
    except (OSError, ValueError):
        return ""




def _zones_of(settings: dict, peer_id: str) -> dict:
    found = {}
    for zone in settings["zones"]:
        if zone.get("peer") == peer_id:
            found.setdefault(zone.get("kind"), zone)
    return found


def _flat_from_settings(settings: dict) -> dict:
    """The dict `config_from_dict` reads: settings.json's kept fields, and the first desktop and its
    zones as the fields that used to be the Mac's."""
    flat = {key: settings[key] for key in KEPT_FIELDS if key in settings}
    flat.update(machine_id=settings["machine_id"], name=_text(settings["name"]), port=settings["port"],
                host="", mac_host="", auth_token="")
    peer = peerlist.first_desktop(settings["peers"])
    zones = _zones_of(settings, peer.get("id", "")) if peer else {}
    flat["crossing_methods"] = [kind for kind in KINDS if kind in zones and not zones[kind].get("off")]
    if settings["shortcut"]:
        flat["crossing_methods"].append("shortcut")
    _methods, corner, parts = _zone_fields({
        "crossing_corner": zones.get("corner", {}).get("corner"), "crossing_edge_parts": zones.get("part", {}).get("parts"),
    })
    flat["crossing_edge_parts"], flat["crossing_corner"] = parts, corner
    if peer:
        flat.update(
            auth_token=peer.get("token", ""), mac_host=peer.get("host", ""), paired_with=peer.get("name", ""),
            mac_hardware_address=peer.get("hw", ""), send_to_mac=peer.get("send", True),
            allow_mac_to_drive=peer.get("allow_drive", True), mac_return_edge=peer.get("side", ""),
            arrangement_set_at=peer.get("side_set_at", 0),
        )
    return flat


def _apply_flat(settings: dict, flat: dict, *, pin_address=False) -> None:
    """Write a Config's flat fields (from `config_to_dict`) into the settings: this PC's own, and the
    first desktop's address, name and switches, never a side or a zone. A token that is not that
    desktop's is a new pairing: it replaces that entry, and its zones point at the empty id until a link
    learns the new one."""
    own_id = settings["machine_id"]
    settings.update({key: flat[key] for key in KEPT_FIELDS})
    old_port, settings["port"] = settings["port"], flat["port"]
    settings["shortcut"] = "shortcut" in flat["crossing_methods"]
    settings["name"] = flat["name"]
    if not flat["auth_token"]:
        # Nothing paired: this machine's own settings are all there is to write, and no peer is made.
        return
    peers = settings["peers"]
    index = next((index for index, entry in enumerate(peers) if entry.get("port") != 0), None)
    peer = peers[index] if index is not None else None
    if peer is None or peer.get("token") != flat["auth_token"]:
        entry = _peer_entry(flat, flat["auth_token"], flat["port"], own_id)
        held = peer or {}
        # The side is the pair's, settled by `arrangement`, and is not the flat view's to set.
        entry.update(side=held.get("side", ""), side_set_at=held.get("side_set_at", 0), side_by=held.get("side_by", ""))
        if index is not None:
            for zone in settings["zones"]:
                if zone.get("peer") == peer.get("id"):
                    zone["peer"] = ""
            peers[index] = entry
        else:
            peers.append(entry)
        peer = entry
    # Its name and hardware address are learnt on its link: written here only with a new address, which
    # clears what was learnt at the old one, or a save from a window read before one was learnt would
    # put the old one back.
    if flat["mac_host"] != peer.get("host"):
        peer.update(name=flat["paired_with"], hw=flat["mac_hardware_address"], host_pinned=True)
    if pin_address and flat["mac_host"]:
        peer["host_pinned"] = True
    peer.update(host=flat["mac_host"], send=flat["send_to_mac"], allow_drive=flat["allow_mac_to_drive"])
    if flat["port"] != old_port and peer.get("port") == old_port:
        peer["port"] = flat["port"]


# Held around every read-change-write of settings.json, here and by the link's PeerBook (which takes
# this lock), so an id a link has just learnt and a change the window makes cannot cross.
SETTINGS_LOCK = threading.RLock()


class ReadFallbackPeerBook(receiver.PeerBook):
    """The link's PeerBook over settings.json. `load` reads the file on every call. One read that
    fails (a lock held by a scanner, a save half written) must not raise into a link thread for the
    read-only calls, `peers` and `zones`, which then give the last good copy. `admit` and
    `store_paired` load, change and save, and a copy the window has since changed (it writes
    settings.json directly) would be written back over that change, so a failed read raises there
    and the handshake fails and retries. With no good copy yet, every call raises."""

    def __init__(self, load, save, lock=None) -> None:
        super().__init__(self._strict, save, lock if lock is not None else SETTINGS_LOCK)
        self._read_settings = load
        self._last_good = None

    def _strict(self) -> dict:
        settings = self._read_settings()
        self._last_good = copy.deepcopy(settings)
        return settings

    def _tolerant(self) -> dict:
        try:
            return self._strict()
        except Exception:
            if self._last_good is None:
                raise
            LOGGER.warning("Could not read the settings; using the last good copy", exc_info=True)
            return copy.deepcopy(self._last_good)

    def peers(self) -> list:
        with self.lock:
            return copy.deepcopy(list((self._tolerant() or {}).get("peers") or []))

    def zones(self) -> list:
        with self.lock:
            return copy.deepcopy(list((self._tolerant() or {}).get("zones") or []))

    def snapshot(self) -> tuple[list, list]:
        with self.lock:
            settings = self._tolerant() or {}
            return (copy.deepcopy(list(settings.get("peers") or [])),
                    copy.deepcopy(list(settings.get("zones") or [])))


def save_config(path: Path, config: Config, *, pin_address=False) -> None:
    payload = config_to_dict(config)
    with SETTINGS_LOCK:
        settings = load_settings(path)
        if config.trigger_key != settings.get("trigger_key"):
            try:
                ways.check_trigger_key(settings, config.trigger_key)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
        old_tokens = [entry.get("token") for entry in settings["peers"]]
        _apply_flat(settings, payload, pin_address=pin_address)
        if old_tokens != [entry.get("token") for entry in settings["peers"]]:
            settings = crossing_state.paired(settings, platform="pc", own_displays={}, now=time.time())["settings"]
        write_settings(path, settings)


def set_jump_key(path: Path, peer_id: str, value, trigger_key: str) -> dict:
    """Sets or clears one peer's local jump chord after the shared clash checks."""
    with SETTINGS_LOCK:
        settings = load_settings(path)
        entry = next((peer for peer in settings["peers"] if peer["id"] == peer_id), None)
        if entry is None:
            raise ConfigError("that machine is no longer paired with this PC")
        try:
            entry["jump_key"] = ways.check_jump_key(settings, peer_id, value, trigger_key) or ""
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        write_settings(path, settings)
        return copy.deepcopy(entry)


def write_settings(path: Path, settings: dict) -> None:
    """The settings as the link changed them (an id learnt, an address, `linked`), written atomically."""
    if any("in_use" in entry and not isinstance(entry["in_use"], bool) for entry in settings.get("peers", []) if isinstance(entry, dict)):
        raise SettingsFileError(f"{path} has a peer in_use that is not true or false")
    peerlist.normalise_phones(settings)
    if any("jump_key" in entry and (not isinstance(entry["jump_key"], str)
                                    or (entry["jump_key"] and not ways.valid_jump_key(entry["jump_key"])))
           for entry in settings.get("peers", []) if isinstance(entry, dict)):
        raise SettingsFileError(f"{path} has a peer jump_key that is not a recorded modifier chord")
    _write_json(path, settings)


def _without_profile_state(settings: dict) -> dict:
    result = copy.deepcopy(settings)
    for name in ("crossing_own", "crossing_desk", "crossing_own_unreadable", "crossing_desk_unreadable",
                 "_crossing_profiles_upgrade_pending"):
        result.pop(name, None)
    return result


def _backup_before_profile_upgrade(path: Path) -> None:
    """Keep the exact schema-6 source before the first save of a profile-migrated file."""
    if portable_runtime.enabled():
        return
    backup = path.with_name("settings.json.before-1.5.5")
    if backup.exists():
        return
    try:
        source = path.read_bytes()
    except FileNotFoundError:
        return
    except OSError:
        raise
    raw = json.loads(source)
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        return
    own, desk = raw.get("crossing_own"), raw.get("crossing_desk")
    if profiles._local_shape(own, desk):
        return
    temporary_path = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{backup.name}.", dir=path.parent)
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        shutil.copyfile(path, temporary_path)
        try:
            os.link(temporary_path, backup)
        except FileExistsError:
            pass
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def set_own(path: Path, **fields) -> None:
    """Writes fields that belong to this PC rather than to a peer (`port`, `hide_addresses`) straight into the
    settings, which works with no machine paired and leaves every peer entry as it is."""
    with SETTINGS_LOCK:
        settings = load_settings(path)
        settings.update(fields)
        _write_json(path, settings)


def learned_fields(path: Path) -> dict:
    """What links write into the first peer's entry, in the flat view's names: its token, address,
    hardware address and name, the two direction switches, and the shared layout."""
    with SETTINGS_LOCK:
        flat = _flat_from_settings(load_settings(path))
    # With no peer there is nothing for links to have written, and the flat view has no such fields.
    fallback = default_config()
    return {name: flat[name] if name in flat else getattr(fallback, name) for name in LEARNED}


# What links and a fold write while the window holds its flat view.
LEARNED = ("auth_token", "mac_host", "mac_hardware_address", "paired_with", "send_to_mac", "allow_mac_to_drive",
           "layout", "layout_set_at", "layout_by")
# The ones a link writes while the window is not looking, which a save must never overwrite.
LINK_WRITTEN = ("mac_host", "mac_hardware_address", "paired_with")


def set_peer_hardware_address(path: Path, peer: str, address: str) -> bool:
    """Writes a peer's hardware address, as learnt from the ARP table; True when it changed."""
    with SETTINGS_LOCK:
        settings = load_settings(path)
        entry = next((item for item in settings["peers"] if item.get("id") == peer), None)
        if entry is None or entry.get("port") == 0 or entry.get("hw") == address:
            return False
        entry["hw"] = address
        _write_json(path, settings)
        return True


def set_ways(path: Path, peer: str, *, side=None, methods, parts, corner, live=None, capable=()) -> bool:
    """Save local intent through the shared crossing event; side belongs to the picture."""
    with SETTINGS_LOCK:
        settings = load_settings(path)
        entry = next((item for item in settings["peers"] if item.get("id") == peer), None)
        if entry is None:
            raise ConfigError("that machine is not paired with this PC")
        outcome = crossing_state.edit_ways(
            settings, peer, platform="pc", edge=bool(set(methods) & {"edge", "part"}),
            thirds=list(ways.PARTS) if "edge" in methods else parts,
            corner="corner" in methods, corner_at=corner, live=live, gone=None,
            now=time.time(), capable=capable)
        _write_json(path, outcome["settings"])
        return entry.get("side", "") != ways._entry(outcome["settings"], peer).get("side", "")


def share_side(path: Path, side: str, thirds: dict) -> list:
    """Compatibility no-op: placement and thirds now allocate shared edges."""
    return []


def settle_presence(path: Path, live, gone):
    """Compatibility no-op: picture peers keep intent through link changes."""
    return False, []


def apply_arrangement(path: Path, peer: str, edge: str, set_at: int, by: str, way_back=None, way_back_by=None,
                      live=None, capable=()):
    """Compatibility entry point for capability-aware arrangement reception."""
    with SETTINGS_LOCK:
        settings = load_settings(path)
        outcome = crossing_state.receive_arrangement(
            settings, peer, {"edge": edge, "set_at": set_at, "by": protocol.read_id(by),
                             "way_back": way_back, "way_back_by": protocol.read_id(way_back_by)},
            platform="pc", capable=capable, live=live, gone=None, now=time.time())
        if outcome["changed"]:
            _write_json(path, outcome["settings"])
        return outcome["changed"], []
