import os
import sys
from pathlib import Path

from setuptools import setup


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
# Launch Services accepts only three integers in CFBundleShortVersionString, so a beta such as
# 1.5.0-beta.1 carries its release number there; the bundled VERSION file says beta to the user.
PLIST_VERSION = VERSION.split("-")[0]
BUILD = os.environ.get("BEAMER_BUILD", "1")


setup(
    name="Beamer",
    version=PLIST_VERSION,
    app=["kvm_bridge_app.py"],
    py_modules=[
        "bridge",
        "clipboard_mac",
        "config",
        "crossing",
        "desktop_mac",
        "diagram",
        "effects_overlay",
        "gestures",
        "hardware_mac",
        "ignored_titles",
        "input_injector_mac",
        "key_codes",
        "keyboard_layout",
        "link_state",
        "login_item",
        "machines",
        "machines_panel",
        "media_keys",
        "motion",
        "notices",
        "notch_beam",
        "notch_island",
        "pages",
        "pointer_hide",
        "previews",
        "no_unlock",
        "qr",
        "settings_store",
        "theme",
        "tokens",
        "wake",
        "widgets",
        "windows_input",
    ],
    # No Mac ships Hanken Grotesk or B612 Mono. macOS registers anything under
    # ATSApplicationFontsPath before the app runs, so the bundle carries the same files the
    # Windows receiver does rather than a second copy of them.
    data_files=[(
        "Fonts",
        [
            "../win_app/assets/HankenGrotesk-Variable.ttf",
            "../win_app/assets/B612Mono-Regular.ttf",
            "../win_app/assets/B612Mono-Bold.ttf",
        ],
    ), ("", ["../VERSION"])],
    options={
        "py2app": {
            "argv_emulation": False,
            # PyNaCl (nacl) ships a compiled _sodium extension reached through cffi; naming both
            # packages keeps their data files and submodules in the bundle.
            "packages": ["rumps", "cffi", "nacl"],
            # core/effects.py imports its fx_* modules by name at first use, which py2app's import
            # scan cannot see, so they are named here or the bundle ships without a single effect.
            # The rest of core/ is named too, through the repo root put on sys.path above, so a module
            # the scan misses cannot drop out; naming the package whole would ship its tests too.
            "includes": [
                "objc", "AppKit", "ApplicationServices", "Quartz", "UserNotifications",
                "core.crash_log", "core.effects", "core.ignored", "core.keytable", "core.link", "core.owner", "core.pairing",
                "core.peerlist", "core.protocol", "core.receiver", "core.return_edge", "core.settings_sync",
                "core.updates", "core.wol",
                "core.fx_ink", "core.fx_instrument", "core.fx_membrane", "core.fx_sparks", "core.fx_warp",
                "core.fx_light", "core.fx_material", "core.fx_folio", "core.fx_selvedge",
            ],
            "iconfile": "../Beamer.icns",
            "plist": {
                "CFBundleDisplayName": "Beamer",
                "CFBundleIdentifier": "uk.co.kalkman.beamer",
                "CFBundleName": "Beamer",
                "CFBundleShortVersionString": PLIST_VERSION,
                "CFBundleVersion": BUILD,
                "LSMinimumSystemVersion": "13.0",
                "LSUIElement": False,
                "NSHighResolutionCapable": True,
                "NSPrincipalClass": "NSApplication",
                "ATSApplicationFontsPath": "Fonts",
                "NSHumanReadableCopyright": "Copyright 2026 Toby Kalkman",
                "NSLocalNetworkUsageDescription": "Beamer needs to reach your other computers on your local network to share keyboard and mouse input.",
            },
        }
    },
)
