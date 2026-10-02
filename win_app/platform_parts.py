"""The one switch between the OS parts the shell drives: capture, injection, clipboard, desktop
geometry, autostart, firewall and unlock, which also carry the touchpad injector and GameInputSvc
(input_injector and desktop_win reach them). Chosen once, at import.

Linux on X11 gets the X11 back ends for capture, injection, the clipboard, desktop geometry and
autostart; Linux on Wayland gets the portal back ends (InputCapture, RemoteDesktop and its
clipboard, over libei) and the same XDG autostart; both get linux_stand_ins for the firewall and
unlock, and no Windows module beyond the pure pieces capture_x11 and inject_x11 borrow. Every other platform keeps the Windows
modules, including the Mac, where the win_app tests import them on purpose. The callers keep
their old names (`capture_win` and so on) bound to these, so a test that patches
`kvm_bridge_win.firewall_win` still patches the part in use."""

import os
import sys
from types import SimpleNamespace

PARTS = ("capture", "desktop", "clipboard", "autostart", "firewall", "unlock", "injector")


def session(environ) -> str:
    """"x11" or "wayland": which display server a Linux desktop runs. XDG_SESSION_TYPE says when it
    says either; `startx` from a console leaves it "tty", so then an X display with no Wayland one
    is X11. Under Wayland, DISPLAY is XWayland's, which cannot capture or inject for other apps."""
    kind = environ.get("XDG_SESSION_TYPE", "").lower()
    if kind in ("x11", "wayland"):
        return kind
    if environ.get("DISPLAY") and not environ.get("WAYLAND_DISPLAY"):
        return "x11"
    return "wayland"


def load(platform: str, environ=os.environ) -> SimpleNamespace:
    # Plain imports, not importlib: PyInstaller finds modules by reading import statements.
    if platform.startswith("linux"):
        import linux_stand_ins

        stand_ins = {"firewall": linux_stand_ins.firewall, "unlock": linux_stand_ins.unlock}
        if session(environ) != "x11":
            import autostart_x11
            import capture_portal
            import clipboard_portal
            import desktop_portal
            import inject_portal

            return SimpleNamespace(**dict(
                stand_ins, capture=capture_portal, desktop=desktop_portal, clipboard=clipboard_portal,
                autostart=autostart_x11, injector=inject_portal,
            ))
        import autostart_x11
        import capture_x11
        import clipboard_x11
        import desktop_x11
        import inject_x11

        return SimpleNamespace(**dict(
            stand_ins, capture=capture_x11, desktop=desktop_x11, clipboard=clipboard_x11, autostart=autostart_x11,
            injector=inject_x11,
        ))
    import autostart_win
    import capture_win
    import clipboard_win
    import desktop_win
    import firewall_win
    import input_injector
    import unlock_win

    return SimpleNamespace(
        capture=capture_win, desktop=desktop_win, clipboard=clipboard_win, autostart=autostart_win,
        firewall=firewall_win, unlock=unlock_win, injector=input_injector,
    )


_parts = load(sys.platform)

# Wayland lets no app draw at the screen's edges, see a full-screen app or a button held before
# the pointer met a barrier, and lets input leave only across an edge: the app says so where it applies.
WAYLAND = sys.platform.startswith("linux") and session(os.environ) != "x11"

capture = _parts.capture
desktop = _parts.desktop
clipboard = _parts.clipboard
autostart = _parts.autostart
firewall = _parts.firewall
unlock = _parts.unlock
injector = _parts.injector
