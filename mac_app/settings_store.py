import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

import config as config_module
from core import effects
from core import ignored
from core import protocol
from core import return_edge

from crossing import CORNERS, EDGES, GLOW_COLOURS, GLOW_STYLES, HAPTIC_FEELS, HAPTIC_STEPS, METHODS, NOTCH_STYLES
from key_codes import KEY_NAME_TO_CODE
from wake import parse_mac


APP_SUPPORT_DIRECTORY = Path.home() / "Library" / "Application Support" / "Beamer"
DEFAULT_SETTINGS_PATH = APP_SUPPORT_DIRECTORY / "config.json"

class SettingsError(Exception):
    pass


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
        "pointer_speed": cfg.pointer_speed,
        "scroll_speed": cfg.scroll_speed,
        "reverse_scroll": cfg.reverse_scroll,
        "appearance": cfg.appearance,
        "ignored_inputs": list(cfg.ignored_inputs),
    }


class SettingsStore:
    def __init__(self, path=None):
        self.path = Path(path).expanduser() if path else DEFAULT_SETTINGS_PATH

    def load(self):
        try:
            cfg = config_module.load_config(str(self.path))
        except (config_module.ConfigError, json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            raise SettingsError(str(exc)) from exc
        cfg = self._migrate_port(cfg)
        self._validate(cfg)
        return cfg

    def _migrate_port(self, cfg):
        """Move a config still on the old default port, and write the move down.

        51820 sat inside the 49152-65535 range both operating systems hand out for
        themselves, so it was never a port anyone could rely on keeping -- WinNAT claimed it on
        one PC and the app could not listen at all. Nobody chose that number, it was what the
        app shipped with, so it moves; a port a user typed is theirs and is left alone.
        """
        if cfg.port != protocol.LEGACY_DEFAULT_PORT:
            return cfg
        cfg = replace(cfg, port=protocol.DEFAULT_PORT)
        try:
            self.save(config_to_raw(cfg))
        except SettingsError:
            # The port in hand is what matters; a read-only config directory must not
            # stop the app connecting on it.
            pass
        return cfg

    def save(self, raw):
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
                prefix=".config.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(raw, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            cfg = config_module.load_config(str(temporary_path))
            self._validate(cfg)
            os.replace(temporary_path, self.path)
            temporary_path = None
            os.chmod(self.path, 0o600)
            return cfg
        except (config_module.ConfigError, json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            raise SettingsError(str(exc)) from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

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
