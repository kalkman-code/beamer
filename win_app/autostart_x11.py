"""Start at login, owned by the app: the autostart entry is the setting, so there is no config
field to disagree with it.

An XDG autostart entry (freedesktop Desktop Application Autostart spec) in
$XDG_CONFIG_HOME/autostart, which every mainstream desktop session runs at login. Unlike the
Windows task it needs no elevation and no service manager.

The spec lets a user or desktop switch an entry off without deleting it, with Hidden=true (or
GNOME's X-GNOME-Autostart-enabled=false), so those read as disabled here; enabling rewrites the
file and so clears them.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

ENTRY_NAME = "beamer.desktop"
COMMENT = "Shares one keyboard and mouse between your computers"
# The spec's reserved characters: an Exec argument holding any of them must be double-quoted.
_RESERVED = set(" \t\n\"'\\<>~|&;$*?#()`")


def config_home() -> Path:
    """$XDG_CONFIG_HOME, or ~/.config when it is unset, empty or not absolute, as the base
    directory spec says."""
    configured = os.environ.get("XDG_CONFIG_HOME", "")
    return Path(configured) if configured and os.path.isabs(configured) else Path.home() / ".config"


def _entry_path(base: Optional[os.PathLike]) -> Path:
    return Path(base if base is not None else config_home()) / "autostart" / ENTRY_NAME


def installed_exe() -> Optional[str]:
    """The exe an entry can start, or None when running from source, where there is none."""
    return sys.executable if getattr(sys, "frozen", False) else None


def _exec_argument(arg: str) -> str:
    """One argument as the spec's Exec key wants it: %% for a literal percent, double quotes when
    a reserved character is present (with " ` $ and \\ backslash-escaped inside), then the whole
    value escaped again as a string, which doubles every backslash."""
    arg = arg.replace("%", "%%")
    if any(char in _RESERVED for char in arg):
        for char in '\\"`$':
            arg = arg.replace(char, "\\" + char)
        arg = f'"{arg}"'
    return arg.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")


def exec_line(exe: str) -> str:
    return f"{_exec_argument(exe)} --hidden"


def entry_text(exe: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Beamer\n"
        f"Exec={exec_line(exe)}\n"
        "X-GNOME-Autostart-enabled=true\n"
        "NoDisplay=true\n"
        f"Comment={COMMENT}\n"
    )


def is_enabled(base: Optional[os.PathLike] = None) -> bool:
    try:
        lines = _entry_path(base).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    in_entry = False
    for line in lines:
        line = line.strip()
        if line.startswith("["):
            in_entry = line == "[Desktop Entry]"
        elif in_entry:
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if (key == "Hidden" and value == "true") or (key == "X-GNOME-Autostart-enabled" and value == "false"):
                return False
    return True


def set_enabled(enabled: bool, exe: str, base: Optional[os.PathLike] = None) -> None:
    """Raises OSError saying which file could not be written, or removed, and why."""
    path = _entry_path(base)
    if not enabled:
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            raise OSError(f"could not remove {path}: {error.strerror or error}") from error
        return
    temp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".beamer-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(entry_text(exe))
        # mkstemp makes the file private; a desktop session reads it as the user, but others expect 0644.
        os.chmod(temp, 0o644)
        os.replace(temp, path)
        temp = None
    except OSError as error:
        raise OSError(f"could not write {path}: {error.strerror or error}") from error
    finally:
        if temp is not None:
            try:
                os.unlink(temp)
            except OSError:
                pass
