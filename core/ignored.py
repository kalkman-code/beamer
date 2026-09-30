"""Keys and buttons that stay on the machine they were pressed on while its input is on the other
one: a mouse's back and forward buttons kept for this machine's browser, a volume key kept for its
speakers. A per-machine setting, so each app records its own keyboard's codes -- a Mac keycode and
a Windows virtual key are different numbers for the same key, and neither end ever reads the
other's list.

Each entry is a string: `key:<code>` for a key in the platform's own numbering, `button:<name>`
for a mouse button, and `media:<name>` for a Mac media key, which arrives as a system event rather
than a key.
"""

import re
import threading

MAX_ENTRIES = 64
_ENTRY = re.compile(r"^(key:[0-9]{1,5}|button:[a-z0-9_]{1,24}|media:[a-z_]{1,24})$")


def key(code):
    return f"key:{int(code)}"


def button(name):
    return f"button:{name}"


def media(name):
    return f"media:{name}"


def validate(entries):
    """The list as saved, or ValueError naming what is wrong with it."""
    if not isinstance(entries, list):
        raise ValueError("ignored_inputs must be a list")
    if len(entries) > MAX_ENTRIES:
        raise ValueError(f"ignored_inputs holds at most {MAX_ENTRIES} entries")
    for entry in entries:
        if not isinstance(entry, str) or not _ENTRY.match(entry):
            raise ValueError(f"ignored_inputs has an entry Beamer cannot read: {entry!r}")
    return list(entries)


class Gate:
    """Decides, while input is on the other machine, which presses stay here.

    A press stays when it is on the list. Its release goes wherever its press went, whatever the
    list says by then: a key sent across and put on the list while still held would otherwise have
    its release kept here and stay down on the far side, and one kept here and taken off the list
    would stay down here. `keeps` runs on the one thread that reads input on each platform; `reset`
    is for when input comes home, since the far side lets go of everything it was holding then, and
    is called from other threads, hence the lock."""

    def __init__(self, entries=()):
        self.entries = frozenset(entries)
        self._sent = set()
        self._kept = set()
        self._lock = threading.Lock()

    def configure(self, entries):
        self.entries = frozenset(entries)

    def keeps(self, entry, down):
        with self._lock:
            if entry in self._sent:
                if not down:
                    self._sent.discard(entry)
                return False
            if entry in self._kept:
                if not down:
                    self._kept.discard(entry)
                return True
            if entry in self.entries:
                if down:
                    self._kept.add(entry)
                return True
            if down:
                self._sent.add(entry)
            return False

    def reset(self):
        with self._lock:
            self._sent.clear()
            # A release that arrives once input is home is never asked about, so what was kept
            # here is forgotten too.
            self._kept.clear()
