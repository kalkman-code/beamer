"""The Mac's settings.json (WIRE.md section 1) and the migration from 1.4.x's config.json.

The app above this layer still speaks Config, so the file is read into the shape 1.4.x had, with
peers[0] feeding host and auth_token; settings.json holds the rest (other peers, zones) for the steps
that follow. config.json is only ever read, so a downgrade finds its settings as they were.
"""

import base64
import binascii
import copy
import dataclasses
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

import config as config_module
from core import effects
from core import ignored
from core import peerlist
from core import protocol
from core import receiver
from core import return_edge
from core import ways

from crossing import CORNERS, EDGES, GLOW_COLOURS, GLOW_STYLES, HAPTIC_FEELS, HAPTIC_STEPS, METHODS, NOTCH_STYLES
from key_codes import KEY_NAME_TO_CODE
from wake import parse_mac


APP_SUPPORT_DIRECTORY = Path.home() / "Library" / "Application Support" / "Beamer"
DEFAULT_SETTINGS_PATH = APP_SUPPORT_DIRECTORY / "settings.json"
LEGACY_FILE = "config.json"

SCHEMA = 6
MAX_PEERS = 32
# What moves out of `crossing` into the peer and the zones once a peer exists (WIRE.md sections 1 and 8).
MOVED_CROSSING = ("methods", "edge", "edge_parts", "corner", "arrangement_set_at")
ZONE_KINDS = tuple(method for method in METHODS if method != "shortcut")
# Fields settings.json has that Config has no name for; everything else in it is Config's own.
SETTINGS_ONLY = ("schema", "machine_id", "name", "port", "shortcut", "migrated_token_sha256", "peers", "zones", "crossing")
PEER_DEFAULTS = {
    "id": "", "name": "", "platform": "", "token": "", "host": "", "port": 0, "hw": "", "send": True,
    "allow_drive": True, "in_use": True, "side": "", "side_set_at": 0, "side_by": "", "paired_with": [], "paired_at": 0,
    "linked": False, "from_1_4": False, "jump_key": "",
}


class SettingsError(Exception):
    pass


def _b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text, length):
    """The bytes of a b64 text of exactly `length` bytes, or None; one spelling only (WIRE.md, Encodings)."""
    if not isinstance(text, str):
        return None
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None
    return data if len(data) == length and _b64(data) == text else None


def is_machine_id(text):
    data = _unb64(text, 16)
    return data is not None and any(data)


def new_machine_id():
    while True:
        data = secrets.token_bytes(16)
        if any(data):
            return _b64(data)


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def editable_default_config():
    return config_module.Config(
        # Learned by pairing. Nothing about the PC is known on a fresh install.
        host="",
        port=protocol.DEFAULT_PORT,
        auth_token="",
        trigger_key="alt_r",
        double_tap_ms=300,
        key_map=dict(config_module.DEFAULT_KEY_MAP),
        reconnect_interval_s=2.0,
        trigger_style="double_tap",
        crossing=dict(config_module.DEFAULT_CROSSING),
        pc_name="",
        mac_address="",
        send_to_windows=True,
        allow_windows_to_drive=True,
        check_updates=True,
        hide_addresses=False,
        same_on_both=False,
        same_set_at=0,
        same_by="",
        pointer_speed=1.0,
        scroll_speed=1.0,
        reverse_scroll=False,
        appearance="system",
        ignored_inputs=[],
    )


def config_to_raw(cfg):
    return {
        "host": cfg.host,
        "port": cfg.port,
        "auth_token": cfg.auth_token,
        "trigger_key": cfg.trigger_key,
        "double_tap_ms": cfg.double_tap_ms,
        "key_map": config_module.key_map_style(cfg.key_map) or dict(cfg.key_map),
        "reconnect_interval_s": cfg.reconnect_interval_s,
        "trigger_style": cfg.trigger_style,
        "crossing": {
            **cfg.crossing,
            "methods": list(cfg.crossing["methods"]),
            "edge_parts": list(cfg.crossing["edge_parts"]),
        },
        "pc_name": cfg.pc_name,
        "mac_address": cfg.mac_address,
        "send_to_windows": cfg.send_to_windows,
        "allow_windows_to_drive": cfg.allow_windows_to_drive,
        "check_updates": cfg.check_updates,
        "hide_addresses": cfg.hide_addresses,
        "same_on_both": cfg.same_on_both,
        "same_set_at": cfg.same_set_at,
        "same_by": cfg.same_by,
        "pointer_speed": cfg.pointer_speed,
        "scroll_speed": cfg.scroll_speed,
        "reverse_scroll": cfg.reverse_scroll,
        "appearance": cfg.appearance,
        "ignored_inputs": list(cfg.ignored_inputs),
    }


def _corner_edge(corner):
    """The Mac's corner crossing always left by the corner's left or right edge (crossing.py); only the
    way home used the side, and that is the entry's `side`, not the zone's."""
    return corner.split("_")[1]


def _zones_for(peer_id, crossing):
    """One zone of each kind for a peer, from the crossing methods 1.4.x had (WIRE.md section 8)."""
    corner = crossing["corner"]
    wanted = [
        {"peer": peer_id, "kind": "edge"},
        {"peer": peer_id, "kind": "part", "parts": list(crossing["edge_parts"])},
        {"peer": peer_id, "kind": "corner", "corner": corner, "edge": _corner_edge(corner)},
        {"peer": peer_id, "kind": "notch"},
    ]
    methods = crossing["methods"]
    for zone in wanted:
        # The whole edge covers its thirds, so both on would be two zones over one stretch.
        if zone["kind"] not in methods or (zone["kind"] == "part" and "edge" in methods):
            zone["off"] = True
    return wanted


def _apply_zones(zones, peer_id, crossing):
    """Bring the peer's own zone of each kind to the crossing choices, keeping anything else in the list."""
    result = list(zones)
    for new in _zones_for(peer_id, crossing):
        for index, zone in enumerate(result):
            if zone.get("peer") == peer_id and zone.get("kind") == new["kind"]:
                merged = {**zone, **{key: value for key, value in new.items() if key != "off"}}
                if new.get("off"):
                    merged["off"] = True
                else:
                    merged.pop("off", None)
                result[index] = merged
                break
        else:
            result.append(new)
    return result


def _crossing_from_zones(settings, peer):
    """The crossing choices the zones and `shortcut` stand for, read for one peer."""
    mine = {}
    for zone in settings["zones"]:
        if zone.get("peer") == peer["id"] and zone.get("kind") in ZONE_KINDS:
            mine.setdefault(zone["kind"], zone)
    parts = mine.get("part", {}).get("parts")
    corner = mine.get("corner", {}).get("corner")
    return {
        "methods": (["shortcut"] if settings["shortcut"] else [])
                   + [kind for kind in ZONE_KINDS if kind in mine and mine[kind].get("off") is not True],
        "edge": peer["side"] or config_module.DEFAULT_CROSSING["edge"],
        "edge_parts": list(parts) if isinstance(parts, list) and parts else list(config_module.DEFAULT_CROSSING["edge_parts"]),
        "corner": corner if corner in CORNERS else config_module.DEFAULT_CROSSING["corner"],
        "arrangement_set_at": peer["side_set_at"],
    }


def raw_from_settings(settings):
    """settings.json in config.json's shape, with peers[0] standing in for the one PC."""
    raw = {name: copy.deepcopy(value) for name, value in settings.items() if name not in SETTINGS_ONLY}
    crossing = dict(settings["crossing"])
    if settings["peers"]:
        peer = settings["peers"][0]
        raw.update(
            host=peer["host"], port=peer["port"], auth_token=peer["token"], pc_name=peer["name"],
            mac_address=peer["hw"], send_to_windows=peer["send"], allow_windows_to_drive=peer["allow_drive"],
        )
        crossing.update(_crossing_from_zones(settings, peer))
    else:
        raw.update(
            host="", port=settings["port"], auth_token="", pc_name="", mac_address="",
            send_to_windows=True, allow_windows_to_drive=True,
        )
    raw["crossing"] = crossing
    return raw


def _entry_from_config(cfg, machine_id):
    """The peer entry a 1.4.x pairing becomes: no id until the first link, and never linked."""
    crossing = cfg.crossing
    stamp = crossing["arrangement_set_at"]
    return {
        "id": "", "name": cfg.pc_name, "platform": "windows", "token": cfg.auth_token, "host": cfg.host,
        "port": cfg.port, "hw": cfg.mac_address, "send": cfg.send_to_windows, "allow_drive": cfg.allow_windows_to_drive,
        "side": crossing["edge"], "side_set_at": stamp, "side_by": machine_id if stamp else "",
        "paired_with": [], "paired_at": 0, "linked": False, "from_1_4": True,
    }


def _rename_zones(zones, old, new):
    return [{**zone, "peer": new} if zone.get("peer") == old else zone for zone in zones]


def _merge_config(base, cfg):
    """`base` with the values of `cfg` written over it: the pairing fields into peers[0], the rest by name."""
    settings = copy.deepcopy(base)
    raw = config_to_raw(cfg)
    machine_id = settings["machine_id"]
    for name, value in raw.items():
        if name not in ("host", "port", "auth_token", "pc_name", "mac_address", "send_to_windows",
                        "allow_windows_to_drive", "crossing"):
            settings[name] = value
    settings["shortcut"] = "shortcut" in cfg.crossing["methods"]
    crossing = {name: value for name, value in raw["crossing"].items()}
    peers, zones = settings["peers"], settings["zones"]
    if not cfg.auth_token:
        if peers:
            gone = peers.pop(0)
            settings["zones"] = [zone for zone in zones if zone.get("peer") != gone["id"]]
    elif not peers or peers[0]["token"] != cfg.auth_token:
        entry = _entry_from_config(cfg, machine_id)
        if peers:
            zones = _rename_zones(zones, peers[0]["id"], "")
            peers[0] = entry
        else:
            peers.append(entry)
        settings["zones"] = _apply_zones(zones, "", crossing)
    else:
        # A machine's side and zones are written by `set_ways` and `arrangement` only: the flat view
        # mirrors the first machine's, and one read before another machine's arrangement landed
        # would otherwise put the old side back.
        # Its name and hardware address are learnt on its link: written here only with a new address,
        # which clears what was learnt at the old one, or a save from a window read before one was
        # learnt would put the old one back.
        if cfg.host != peers[0].get("host"):
            peers[0].update(name=cfg.pc_name, hw=cfg.mac_address)
        peers[0].update(host=cfg.host, port=cfg.port, send=cfg.send_to_windows, allow_drive=cfg.allow_windows_to_drive)
    settings["crossing"] = {
        name: value for name, value in crossing.items() if not (peers and name in MOVED_CROSSING)
    }
    return settings


class SettingsStore:
    """`path` is settings.json. Given a config.json instead (the name 1.4.x used), the settings go
    beside it and that file is what gets migrated."""

    def __init__(self, path=None):
        path = Path(path).expanduser() if path else DEFAULT_SETTINGS_PATH
        self.legacy_path = path.with_name(LEGACY_FILE)
        self.path = path.with_name("settings.json") if path.name == LEGACY_FILE else path
        # Held around every change to the file, and by the links around theirs (PeerBook.lock), so
        # an id learnt on a link thread and a save from the window cannot cross.
        self.lock = threading.RLock()
        self._cache = None
        # The token of the first machine in the flat settings last handed out by load() or save(),
        # which is what a window holds until it reads them again.
        self._handed_token = ""

    def current(self):
        """The settings as this process holds them: read once, then kept in step with every save,
        so the links can look at the peers a few times a second without reading the file."""
        with self.lock:
            if self._cache is None:
                self.load_settings()
            return self._cache

    def book(self):
        """A PeerBook over these settings, for the links."""
        return receiver.PeerBook(self.current, self.save_settings, self.lock)

    def load(self):
        return self._config(self.load_settings())

    def add_peer(self, entry, replaced=None):
        """Stores a pairing (`PairingService`'s `store`): `entry` joins the peers, taking the place
        of `replaced` when there is one. Raises SettingsError, writing nothing, if it cannot be saved."""
        if entry.get("port") == 0:
            # A phone's entry. The flat settings read the first machine's port, so one of these
            # in front would leave them unloadable; phones are the 1.6.0 milestone.
            raise SettingsError("a phone cannot be paired with this Mac in this version")
        with self.lock:
            settings = copy.deepcopy(self.current())
            if replaced is not None:
                held = next((peer for peer in settings["peers"] if peer["token"] == replaced.get("token")), None)
                # The snapshot `replaced` came from was taken before this lock: it may have changed
                # or gone since (WIRE.md section 6, item 4).
                if held is None or held["id"] != replaced.get("id") or held["linked"] != replaced.get("linked"):
                    raise SettingsError("the machine being replaced changed while pairing")
            peerlist.add_peer(settings, entry, replaced)
            self.save_settings(settings)

    def set_peer(self, token, **switches):
        """Turns `send`, `allow_drive` and `in_use` on or off for the machine holding `token`; returns
        its entry, None when no machine holds it."""
        if not switches or any(name not in ("send", "allow_drive", "in_use") or not isinstance(value, bool) for name, value in switches.items()):
            raise SettingsError("only send, allow_drive and in_use can be switched, each on or off")
        with self.lock:
            settings = copy.deepcopy(self.current())
            entry = next((peer for peer in settings["peers"] if peer["token"] == token), None)
            if entry is None:
                return None
            entry.update(switches)
            return copy.deepcopy(next(peer for peer in self.save_settings(settings)["peers"] if peer["token"] == token))

    def set_jump_key(self, peer_id, value, trigger_key):
        """Sets or clears one peer's local jump chord after the shared clash checks."""
        with self.lock:
            settings = copy.deepcopy(self.current())
            entry = next((peer for peer in settings["peers"] if peer["id"] == peer_id), None)
            if entry is None:
                raise SettingsError("that machine is no longer paired with this Mac")
            try:
                entry["jump_key"] = ways.check_jump_key(settings, peer_id, value, trigger_key) or ""
            except ValueError as exc:
                raise SettingsError(str(exc)) from exc
            return copy.deepcopy(next(peer for peer in self.save_settings(settings)["peers"] if peer["id"] == peer_id))

    def remove_peer(self, token):
        """Removes the machine holding `token`, with its zones, and returns its entry; None when
        no machine holds it."""
        with self.lock:
            settings = copy.deepcopy(self.current())
            removed = peerlist.remove_peer(settings, token)
            if removed is not None:
                self.save_settings(settings)
            return removed

    def set_ways(self, peer, *, side, methods, parts, corner):
        """Writes one machine's side and zones, as the Crossing page shows them for it (core/ways.py).
        Returns whether its side moved, which the caller sends it as `arrangement`; SettingsError, writing
        nothing, for a machine not in the list or zones that would clash with another's."""
        with self.lock:
            settings = copy.deepcopy(self.current())
            try:
                moved = ways.edit(settings, peer, side=side, methods=methods, parts=parts, corner=corner,
                                  kinds=ZONE_KINDS, corner_edge=lambda corner, _side: _corner_edge(corner), now=time.time())
            except KeyError:
                raise SettingsError("that machine is no longer paired with this Mac") from None
            if moved:
                # Moved onto a side another machine's edge holds: the side stands and this machine's
                # way there goes off, as when the side arrives from it; the page says who holds it.
                ways.settle(settings, peer, "this Mac")
            self.save_settings(settings)
            return moved

    def share_side(self, side, thirds):
        """Split one side's zones under the store lock, returning every machine whose zones changed."""
        with self.lock:
            settings = copy.deepcopy(self.current())
            changed = ways.share(settings, side, thirds, kinds=ZONE_KINDS)
            self.save_settings(settings)
            return changed

    def set_hardware(self, peer, address):
        """Writes the hardware address learnt for a machine (wake-on-LAN); True when it changed."""
        address = parse_mac(address)
        if address is None:
            return False
        with self.lock:
            settings = copy.deepcopy(self.current())
            entry = next((item for item in settings["peers"] if item["id"] == peer), None)
            if entry is None or entry["hw"] == address:
                return False
            entry["hw"] = address
            self.save_settings(settings)
            return True

    def arrangement(self, peer, edge, set_at, by, way_back=None, way_back_by=None):
        """A machine's `arrangement`, kept when it is newer than the side held for that machine, and
        its `way_back` whatever the side (core/ways.py). Returns (changed, notices), a sentence for each
        zone it turned off."""
        with self.lock:
            settings = copy.deepcopy(self.current())
            changed, notices = ways.arrangement(settings, peer, edge, set_at, by, "this Mac", way_back,
                                                 way_back_by=way_back_by)
            if changed:
                self.save_settings(settings)
            return changed, notices

    def save(self, raw):
        try:
            cfg = config_module.parse_config(raw)
        except (config_module.ConfigError, TypeError, ValueError, OverflowError) as exc:
            raise SettingsError(str(exc)) from exc
        self._validate(cfg)
        with self.lock:
            handed = self._handed_token
            base = self.load_settings()
            self._config(base)
            cfg = self._without_stale_pairing(cfg, base, handed)
            return self._config(self.save_settings(_merge_config(base, cfg)))

    @staticmethod
    def _without_stale_pairing(cfg, base, handed):
        """A caller that still holds the flat settings from before the first machine changed (a
        pairing stored from the pairing service's thread, a removal) would have its old machine
        written back over the new one, or the new one popped if there had been none. Its machine
        fields are the file's own then; everything else it changed is kept."""
        first = base["peers"][0]["token"] if base["peers"] else ""
        if cfg.auth_token == first or cfg.auth_token != handed:
            return cfg
        current = raw_from_settings(base)
        return dataclasses.replace(
            cfg, host=current["host"], port=current["port"], auth_token=current["auth_token"], pc_name=current["pc_name"],
            mac_address=current["mac_address"], send_to_windows=current["send_to_windows"],
            allow_windows_to_drive=current["allow_windows_to_drive"],
        )

    def load_settings(self):
        """The whole of settings.json, made from config.json on the first start and kept in step with it after."""
        with self.lock:
            if not self.path.exists():
                settings = self._migrate()
            else:
                settings = self._reimport(self._read())
            self._cache = copy.deepcopy(settings)
            return settings

    def save_settings(self, settings):
        """Check and write `settings` atomically; returns what was written."""
        with self.lock:
            written = self._save_settings(settings)
            self._cache = copy.deepcopy(written)
            return written

    def _save_settings(self, settings):
        settings = self._complete(settings)
        try:
            before = self._read()
        except SettingsError:
            before = None
        if before:
            unnamed = next((peer for peer in before["peers"] if peer["id"] == ""), None)
            if unnamed:
                learnt = next((peer for peer in settings["peers"] if peer["token"] == unnamed["token"] and peer["id"]), None)
                if learnt:
                    settings["zones"] = _rename_zones(settings["zones"], "", learnt["id"])
        self._ensure_zones(settings)
        self._check(settings)
        self._write(settings)
        return settings

    def _config(self, settings):
        try:
            cfg = config_module.parse_config(raw_from_settings(settings))
        except (config_module.ConfigError, TypeError, ValueError, OverflowError) as exc:
            raise SettingsError(str(exc)) from exc
        self._validate(cfg)
        self._handed_token = cfg.auth_token
        return cfg

    def _fresh(self):
        return {
            "schema": SCHEMA,
            "machine_id": new_machine_id(),
            "name": "",
            "port": protocol.DEFAULT_PORT,
            "shortcut": True,
            "migrated_token_sha256": "",
            "peers": [],
            "zones": [],
            "crossing": {},
        }

    def _read(self):
        try:
            settings = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            raise SettingsError(f"{self.path.name} could not be read: {exc}") from exc
        if not isinstance(settings, dict) or settings.get("schema") != SCHEMA:
            raise SettingsError(f"{self.path.name} is not a schema {SCHEMA} settings file")
        settings = self._complete(settings)
        self._ensure_zones(settings)
        self._check(settings)
        return settings

    def _complete(self, settings):
        """A copy with every field the format has, so a hand-edited file missing one does not break a read."""
        settings = copy.deepcopy(settings)
        for name in ("name", "port", "shortcut", "migrated_token_sha256", "crossing"):
            settings.setdefault(name, self._fresh()[name])
        for name in ("machine_id", "peers", "zones"):
            if name not in settings:
                raise SettingsError(f"{name} is missing; settings.json is not written over a file that lacks it")
        if not isinstance(settings["peers"], list):
            raise SettingsError("peers must be a list")
        settings["peers"] = [{**PEER_DEFAULTS, **peer} if isinstance(peer, dict) else peer for peer in settings["peers"]]
        if not isinstance(settings["crossing"], dict):
            raise SettingsError("crossing must be a JSON object")
        settings["crossing"] = {
            **{name: value for name, value in config_module.DEFAULT_CROSSING.items() if name not in MOVED_CROSSING},
            **settings["crossing"],
        } if settings["peers"] else {**config_module.DEFAULT_CROSSING, **settings["crossing"]}
        return settings

    @staticmethod
    def _ensure_zones(settings):
        """Give old pairs their edge and the one unplaced desktop its shown side on every read and save.
        Run after a learnt id has renamed migrated zones, or that machine would get a second edge."""
        if isinstance(settings["zones"], list) and all(isinstance(item, dict) for item in settings["peers"] + settings["zones"]):
            ways.ensure_zones(settings)
            ways.default_side(settings)

    def _check(self, settings):
        if not is_machine_id(settings["machine_id"]):
            raise SettingsError("machine_id must be a 16-byte id that is not all zero")
        for name, kind in (("name", str), ("shortcut", bool), ("migrated_token_sha256", str), ("zones", list)):
            if not isinstance(settings[name], kind):
                raise SettingsError(f"{name} must be {kind.__name__}")
        if isinstance(settings["port"], bool) or not isinstance(settings["port"], int) or not 1 <= settings["port"] <= 65535:
            raise SettingsError("port must be between 1 and 65535")
        if not all(isinstance(zone, dict) and isinstance(zone.get("peer"), str) and zone.get("kind") in ZONE_KINDS
                   for zone in settings["zones"]):
            raise SettingsError("every zone needs a peer and a kind of " + ", ".join(ZONE_KINDS))
        for zone in settings["zones"]:
            if "off" in zone and not isinstance(zone["off"], bool):
                raise SettingsError("a zone's off must be true or false")
            if zone["kind"] == "corner" and (zone.get("corner") not in CORNERS or zone.get("edge") not in EDGES):
                raise SettingsError("a corner zone needs a corner and the edge it crosses")
            if zone["kind"] == "part" and not (isinstance(zone.get("parts"), list) and zone["parts"]
                                               and all(part in return_edge.PARTS for part in zone["parts"])):
                raise SettingsError("a part zone needs thirds of " + ", ".join(return_edge.PARTS))
        peers = settings["peers"]
        if len(peers) > MAX_PEERS:
            raise SettingsError(f"at most {MAX_PEERS} peers")
        ids, key_ids, empty = set(), set(), 0
        for peer in peers:
            if not isinstance(peer, dict):
                raise SettingsError("every peer must be a JSON object")
            for name, default in PEER_DEFAULTS.items():
                value = peer[name]
                if isinstance(default, bool) != isinstance(value, bool) or not isinstance(value, type(default)):
                    raise SettingsError(f"peer {name} must be {type(default).__name__}")
            if "way_back_by" in peer and not isinstance(peer["way_back_by"], str):
                raise SettingsError("peer way_back_by must be str")
            if not isinstance(peer["jump_key"], str) or (peer["jump_key"] and not ways.valid_jump_key(peer["jump_key"])):
                raise SettingsError("peer jump_key must be an empty string or a recorded modifier chord")
            if peer["id"] == "":
                empty += 1
                if not peer["from_1_4"] or empty > 1:
                    raise SettingsError("only the entry migrated from 1.4.x may have an empty id")
            elif not is_machine_id(peer["id"]) or peer["id"] == settings["machine_id"] or peer["id"] in ids:
                raise SettingsError("a peer id must be a machine id that is neither ours nor another peer's")
            ids.add(peer["id"])
            if protocol.is_paired_token(peer["token"]):
                if protocol.key_id(peer["token"]) in key_ids:
                    raise SettingsError("two peers share one token")
                key_ids.add(protocol.key_id(peer["token"]))
        if any(zone["peer"] not in ids for zone in settings["zones"]):
            raise SettingsError("a zone names a peer that is not in the list")
        self._check_overlap(settings)

    def _check_overlap(self, settings):
        """Two zones in use never cover one stretch (WIRE.md section 8); a corner inside an edge is the
        exception, and the notch stands beside the top edge, as 1.4.x let it, but leads to one machine."""
        sentence = ways.clash(settings, "this Mac")
        if sentence is not None:
            raise SettingsError(sentence)

    def _write(self, settings):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".settings.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(settings, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
            temporary_path = None
            os.chmod(self.path, 0o600)
        except (OSError, TypeError, ValueError) as exc:
            raise SettingsError(str(exc)) from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

    def _legacy_config(self):
        """1.4.x's config.json as a Config, or None when it is missing, unreadable or not a pairing file.

        51820 sat inside the 49152-65535 range both operating systems hand out for themselves, so it
        was never a port anyone could rely on keeping: nobody chose it, it was what the app shipped
        with, and it moves. A port a user typed is theirs and is left alone.
        """
        try:
            cfg = config_module.load_config(str(self.legacy_path))
        except (config_module.ConfigError, json.JSONDecodeError, OSError, TypeError, ValueError, OverflowError):
            return None
        if cfg.port == protocol.LEGACY_DEFAULT_PORT:
            cfg.port = protocol.DEFAULT_PORT
        return cfg

    def _migrate(self):
        cfg = self._legacy_config() or editable_default_config()
        self._validate(cfg)
        settings = _merge_config(self._fresh(), cfg)
        settings["migrated_token_sha256"] = _sha256(cfg.auth_token)
        settings["port"] = cfg.port
        return self.save_settings(settings)

    def _reimport(self, settings):
        """A pair made again under 1.4.x (a new token in config.json) replaces the migrated entry, once."""
        legacy = self._legacy_config()
        if legacy is None or not legacy.auth_token or _sha256(legacy.auth_token) == settings["migrated_token_sha256"]:
            return settings
        settings = copy.deepcopy(settings)
        settings["migrated_token_sha256"] = _sha256(legacy.auth_token)
        peers = settings["peers"]
        at = next((index for index, peer in enumerate(peers) if peer["from_1_4"]), None)
        taken = {protocol.key_id(peer["token"]) for index, peer in enumerate(peers)
                 if index != at and protocol.is_paired_token(peer["token"])}
        token = legacy.auth_token
        try:
            self._validate(legacy)
        except SettingsError:
            legacy = None
        clash = protocol.is_paired_token(token) and protocol.key_id(token) in taken
        if legacy is not None and not clash and (at is not None or len(peers) < MAX_PEERS):
            entry = {**PEER_DEFAULTS, **_entry_from_config(legacy, settings["machine_id"])}
            if at is None:
                peers.append(entry)
                settings["zones"] = _apply_zones(settings["zones"], "", legacy.crossing)
            else:
                settings["zones"] = _rename_zones(settings["zones"], peers[at]["id"], "")
                peers[at] = entry
        try:
            return self.save_settings(settings)
        except SettingsError:
            # The move is retried on the next start; a read-only folder must not stop the app opening.
            return self._complete(settings)

    def _validate(self, cfg):
        if not isinstance(cfg.host, str):
            raise SettingsError("Windows host must be text")
        if not 1 <= cfg.port <= 65535:
            raise SettingsError("Port must be between 1 and 65535")
        if cfg.trigger_key not in KEY_NAME_TO_CODE:
            supported = ", ".join(sorted(KEY_NAME_TO_CODE))
            raise SettingsError(f"Unsupported trigger key. Choose one of: {supported}")
        if not 50 <= cfg.double_tap_ms <= 2000:
            raise SettingsError("Double-tap threshold must be between 50 and 2000 ms")
        if not isinstance(cfg.key_map, dict):
            raise SettingsError("key_map must be a JSON object")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in cfg.key_map.items()):
            raise SettingsError("key_map keys and values must be strings")
        if cfg.reconnect_interval_s <= 0:
            raise SettingsError("Reconnect interval must be greater than zero")
        if cfg.trigger_style not in ("double_tap", "hold"):
            raise SettingsError("trigger_style must be double_tap or hold")
        crossing = cfg.crossing
        methods = crossing["methods"]
        if not isinstance(methods, list) or not all(method in METHODS for method in methods):
            raise SettingsError(f"crossing.methods must only contain: {', '.join(METHODS)}")
        if crossing["edge"] not in EDGES:
            raise SettingsError(f"crossing.edge must be one of: {', '.join(EDGES)}")
        parts = crossing["edge_parts"]
        if not isinstance(parts, list) or not parts or not all(part in return_edge.PARTS for part in parts):
            raise SettingsError(f"crossing.edge_parts must be one or more of: {', '.join(return_edge.PARTS)}")
        if crossing["corner"] not in CORNERS:
            raise SettingsError(f"crossing.corner must be one of: {', '.join(CORNERS)}")
        if isinstance(crossing["resistance_px"], bool) or not 0 <= crossing["resistance_px"] <= 500:
            raise SettingsError("crossing.resistance_px must be a whole number between 0 and 500")
        if crossing["notch_style"] not in NOTCH_STYLES:
            raise SettingsError(f"crossing.notch_style must be one of: {', '.join(NOTCH_STYLES)}")
        after = crossing["notch_after_ms"]
        if isinstance(after, bool) or not isinstance(after, int) or not 300 <= after <= 3000:
            raise SettingsError("crossing.notch_after_ms must be a whole number between 300 and 3000")
        choices = (
            ("haptic_feel", HAPTIC_FEELS),
            ("haptic_steps", HAPTIC_STEPS),
            # Today's styles and colours, then every crossing effect and colour pack.
            ("glow_style", GLOW_STYLES + effects.EFFECT_IDS),
            ("glow_colour", GLOW_COLOURS + effects.PACK_IDS),
            ("shortcut_arrival_style", effects.SWITCH_STYLES),
            ("effect_length", tuple(value for value, _name in effects.LENGTHS)),
        )
        for name, allowed in choices:
            if crossing[name] not in allowed:
                raise SettingsError(f"crossing.{name} must be one of: {', '.join(allowed)}")
        for name in ("haptics", "glow", "block_while_dragging", "shortcut_arrival", "hold_full_screen"):
            if not isinstance(crossing[name], bool):
                raise SettingsError(f"crossing.{name} must be true or false")
        for name in ("pointer_speed", "scroll_speed"):
            value = getattr(cfg, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.25 <= value <= 4.0:
                raise SettingsError(f"{name} must be a number from 0.25 to 4")
        if not isinstance(cfg.reverse_scroll, bool):
            raise SettingsError("reverse_scroll must be true or false")
        try:
            ignored.validate(cfg.ignored_inputs)
        except ValueError as exc:
            raise SettingsError(str(exc)) from exc
        if cfg.mac_address and parse_mac(cfg.mac_address) is None:
            raise SettingsError("mac_address must be six hex pairs, such as 02:1A:2B:3C:0D:4E")
