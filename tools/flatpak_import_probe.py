"""Import the installed Linux app without creating a window or reading settings."""
import ctypes
import ctypes.util
import importlib
import os
from pathlib import Path

ROOT = Path("/app/share/beamer")
WINDOWS_ONLY = {
    "autostart_win", "capture_win", "clipboard_win", "desktop_win", "firewall_win",
    "input_injector", "touchpad_injector", "unlock_win",
}
names = ["core"] + sorted(f"core.{p.stem}" for p in (ROOT / "core").glob("*.py") if p.stem != "__init__")
names += sorted(p.stem for p in (ROOT / "win_app").glob("*.py") if p.stem not in WINDOWS_ONLY)
for name in names:
    module = importlib.import_module(name)
    path = Path(module.__file__).resolve()
    if not path.is_relative_to(ROOT):
        raise RuntimeError(f"{name} loaded outside the installed app: {path}")
    print(f"ok {name} {path}", flush=True)
for name in ("ei", "xkbcommon", "xkbcommon-x11", "xcb"):
    path = ctypes.util.find_library(name)
    if not path:
        raise RuntimeError(f"lib{name} is missing")
    ctypes.CDLL(path)
    print(f"ok native {name} {path}", flush=True)
import platform_parts
expected = "x11" if os.environ.get("XDG_SESSION_TYPE") == "x11" else "wayland"
if platform_parts.WAYLAND != (expected == "wayland"):
    raise RuntimeError(f"Wrong platform back ends for {expected}")
print(f"passed: {len(names)} installed modules, 4 native libraries, {expected} back ends", flush=True)
