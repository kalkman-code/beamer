"""The one switch between the OS parts the shell drives: capture, injection, clipboard, desktop
geometry, autostart, firewall and unlock, which also carry the touchpad injector and GameInputSvc
(input_injector and desktop_win reach them). Chosen once, at import.

Linux gets linux_stand_ins, and no Windows module is imported there. Every other platform keeps
the Windows modules, including the Mac, where the win_app tests import them on purpose. The
callers keep their old names (`capture_win` and so on) bound to these, so a test that patches
`kvm_bridge_win.firewall_win` still patches the part in use."""

import sys
from types import SimpleNamespace

PARTS = ("capture", "desktop", "clipboard", "autostart", "firewall", "unlock", "injector")


def load(platform: str) -> SimpleNamespace:
    # Plain imports, not importlib: PyInstaller finds modules by reading import statements.
    if platform.startswith("linux"):
        import linux_stand_ins

        return SimpleNamespace(not_yet=linux_stand_ins.NOT_YET, **{name: getattr(linux_stand_ins, name) for name in PARTS})
    import autostart_win
    import capture_win
    import clipboard_win
    import desktop_win
    import firewall_win
    import input_injector
    import unlock_win

    return SimpleNamespace(
        not_yet=None, capture=capture_win, desktop=desktop_win, clipboard=clipboard_win, autostart=autostart_win,
        firewall=firewall_win, unlock=unlock_win, injector=input_injector,
    )


_parts = load(sys.platform)

NOT_YET = _parts.not_yet
capture = _parts.capture
desktop = _parts.desktop
clipboard = _parts.clipboard
autostart = _parts.autostart
firewall = _parts.firewall
unlock = _parts.unlock
injector = _parts.injector
