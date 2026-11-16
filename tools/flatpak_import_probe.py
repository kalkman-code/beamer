"""Import the installed Linux app without creating a window or reading settings."""
import importlib
import os
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path("/app/share/beamer")
version = (ROOT / "VERSION").read_text().strip()
release = ET.parse("/app/share/metainfo/uk.co.kalkman.beamer.metainfo.xml").getroot().find("releases/release")
if release is None or release.get("version") != version:
    raise RuntimeError("Installed AppStream release differs from VERSION")
print(f"ok AppStream release {version}", flush=True)
WINDOWS_ONLY = {
    "autostart_win", "capture_win", "clipboard_win", "desktop_win", "firewall_win",
    "input_injector", "secure_desktop_win", "secure_service_host", "secure_store_acl",
    "secure_worker", "touchpad_injector", "unlock_win",
}
names = ["core"] + sorted(f"core.{p.stem}" for p in (ROOT / "core").glob("*.py") if p.stem != "__init__")
names += sorted(p.stem for p in (ROOT / "win_app").glob("*.py") if p.stem not in WINDOWS_ONLY)
for name in names:
    module = importlib.import_module(name)
    path = Path(module.__file__).resolve()
    if not path.is_relative_to(ROOT):
        raise RuntimeError(f"{name} loaded outside the installed app: {path}")
    print(f"ok {name} {path}", flush=True)
import libei
libei._load()
print("ok native ei production loader", flush=True)
import xkb_x11
xkb_x11._load()
print("ok native xkbcommon, xkbcommon-x11, xcb production loader", flush=True)
import platform_parts
expected = "x11" if os.environ.get("XDG_SESSION_TYPE") == "x11" else "wayland"
if platform_parts.WAYLAND != (expected == "wayland"):
    raise RuntimeError(f"Wrong platform back ends for {expected}")
print(f"passed: {len(names)} installed modules, 4 native libraries, {expected} back ends", flush=True)
