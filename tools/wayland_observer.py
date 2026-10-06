"""The app on the other end of tools/wayland_proof.py: a full-screen GTK 4 window, run with the
system's Python (it needs PyGObject), that writes every key, pointer move, button and scroll it is
given to a log as JSON lines, and copies or pastes the clipboard when the proof asks through a
commands file. What reaches it reached an app; what Beamer captured never does.

    python3 tools/wayland_observer.py <log> <commands>"""

import json
import os
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

LOG = open(sys.argv[1], "a", buffering=1)
COMMANDS = sys.argv[2]
# Commands written before this one started were for the one before it.
_read_to = [os.path.getsize(COMMANDS) if os.path.exists(COMMANDS) else 0]


def log(**fields) -> None:
    fields["t"] = time.monotonic()
    LOG.write(json.dumps(fields) + "\n")


def poll_commands(window) -> bool:
    try:
        with open(COMMANDS) as handle:
            handle.seek(_read_to[0])
            lines = handle.read()
            _read_to[0] = handle.tell()
    except OSError:
        return True
    clipboard = Gdk.Display.get_default().get_clipboard()
    for line in lines.splitlines():
        if line.startswith("copy:"):
            clipboard.set(line[5:])
            log(kind="copied", text=line[5:])
        elif line == "paste":
            def done(source, result):
                try:
                    text = source.read_text_finish(result)
                except GLib.Error as error:
                    text = None
                    log(kind="paste-failed", error=str(error))
                log(kind="paste", text=text)

            clipboard.read_text_async(None, done)
        elif line == "quit":
            window.get_application().quit()
    return True


def activate(app) -> None:
    window = Gtk.ApplicationWindow(application=app, title="Beamer proof observer")
    window.fullscreen()
    keys = Gtk.EventControllerKey()
    keys.connect("key-pressed", lambda c, keyval, code, state: log(
        kind="key", down=True, name=Gdk.keyval_name(keyval), code=code, state=int(state)) and False)
    keys.connect("key-released", lambda c, keyval, code, state: log(
        kind="key", down=False, name=Gdk.keyval_name(keyval), code=code, state=int(state)))
    window.add_controller(keys)
    motion = Gtk.EventControllerMotion()
    motion.connect("motion", lambda c, x, y: log(kind="motion", x=x, y=y))
    window.add_controller(motion)
    click = Gtk.GestureClick(button=0)
    click.connect("pressed", lambda g, n, x, y: log(kind="button", down=True, button=g.get_current_button(), x=x, y=y))
    click.connect("released", lambda g, n, x, y: log(kind="button", down=False, button=g.get_current_button(), x=x, y=y))
    window.add_controller(click)
    scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
    scroll.connect("scroll", lambda c, dx, dy: log(kind="scroll", dx=dx, dy=dy) or True)
    window.add_controller(scroll)
    window.present()
    GLib.timeout_add(50, poll_commands, window)
    log(kind="ready", pid=os.getpid())


app = Gtk.Application(application_id="uk.co.kalkman.BeamerProofObserver")
app.connect("activate", activate)
app.run([])
