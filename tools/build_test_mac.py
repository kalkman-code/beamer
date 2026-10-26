"""Build a local, ad-hoc signed Apple silicon preview, without installing it.

Run with a Python environment containing mac_app/requirements-mac.txt and PyInstaller.
The official universal2/notarised release workflow remains mac_app/build_dmg.sh.
"""

import plistlib
import subprocess
import sys
from pathlib import Path


def main():
    if sys.platform != "darwin":
        raise SystemExit("Build this preview on macOS")
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
               "--name", "Beamer RU Test", "--osx-bundle-identifier", "local.beamer.ru-test",
               "--icon", str(root / "Beamer.icns"),
               "--distpath", str(root / "mac_app/dist-preview"),
               "--workpath", str(root / "mac_app/build-preview"),
               "--specpath", str(root / "mac_app/build-preview-spec"),
               "--paths", str(root), "--paths", str(root / "mac_app")]
    for source, target in (("VERSION", "."), ("LICENSE", "."), ("locales", "locales"),
                           ("win_app/assets", "Fonts")):
        command += ["--add-data", str(root / source) + ":" + target]
    for module in ("objc", "Quartz", "AppKit", "ApplicationServices", "UserNotifications",
                   "core.keyboard_sync", "core.profiles", "_cffi_backend"):
        command += ["--hidden-import", module]
    for path in sorted((root / "core").glob("fx_*.py")):
        command += ["--hidden-import", "core." + path.stem]
    command += [str(root / "mac_app/preview_launcher.py")]
    subprocess.run(command, check=True, cwd=root)
    app = root / "mac_app/dist-preview/Beamer RU Test.app"
    path = app / "Contents/Info.plist"
    info = plistlib.loads(path.read_bytes())
    info.update(CFBundleShortVersionString="1.5.14", CFBundleVersion="2",
                CFBundleLocalizations=["en", "it", "ru"], CFBundleDevelopmentRegion="en_GB",
                ATSApplicationFontsPath="Fonts", NSPrincipalClass="NSApplication",
                NSHumanReadableCopyright="Copyright 2026 Toby Kalkman",
                NSLocalNetworkUsageDescription="Beamer needs local network access to share keyboard and mouse input.")
    path.write_bytes(plistlib.dumps(info))
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    print(app)


if __name__ == "__main__":
    main()
