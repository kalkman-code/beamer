"""Opt-in RU/EN input-source synchronisation, independent of UI translation.

Only peers advertising input_language receive the extension. No native layout IDs
cross the network: each endpoint chooses from its own installed input sources.
"""

LANGUAGES = frozenset({"ru", "en"})
CAPABILITY = "input_language"
MESSAGE = "input_language"


class Shortcut:
    """Buffer a bare Alt+Shift chord so neither OS toggles a second time.

    Any other key or mouse action flushes the modifiers unchanged. AltGr+Shift
    typing and Alt+mouse gestures therefore keep their ordinary behaviour.
    """

    def __init__(self):
        self.held = set()
        self.pending = []
        self.chord = False
        self.used = False
        self.other = set()

    def feed(self, message):
        kind, data = message.get("type"), message.get("data", {})
        key = data.get("key")
        modifier = key in {"alt", "alt_r", "shift", "shift_r"}
        if kind == "keydown" and not modifier:
            self.other.add(key)
        elif kind == "keyup" and not modifier:
            self.other.discard(key)
        if kind not in {"keydown", "keyup"} or not modifier:
            self.used = bool(self.held)
            self.chord = False
            result, self.pending = self.pending + [message], []
            return result, False
        if kind == "keydown":
            self.held.add(key)
        else:
            self.held.discard(key)
        if self.used or self.other:
            result, self.pending = self.pending + [message], []
            self.chord = False
            if not self.held:
                self.used = False
            return result, False
        self.pending.append(message)
        self.chord |= (bool(self.held & {"alt", "alt_r"})
                       and bool(self.held & {"shift", "shift_r"}))
        if self.held:
            return [], False
        result = self.pending
        toggle = self.chord
        self.pending, self.chord = [], False
        return result, toggle


class Outbound:
    def __init__(self, backend):
        self.backend = backend
        self.shortcuts = {}

    def forget(self, peer):
        self.shortcuts.pop(peer, None)

    def toggle_language(self):
        """Select the other local RU/EN source and return its wire command."""
        current = self.backend.current_language()
        desired = "en" if current == "ru" else "ru" if current == "en" else None
        if desired and self.backend.select_language(desired):
            return {"type": MESSAGE, "data": {"language": desired}}
        return None

    def messages(self, peer, message, *, enabled, caps):
        if not enabled or CAPABILITY not in caps:
            self.forget(peer)
            return [message]
        if message.get("type") in {"keydown", "keyup", "mousemove", "mousedown", "mouseup", "scroll"}:
            messages, toggle = self.shortcuts.setdefault(peer, Shortcut()).feed(message)
            if toggle:
                changed = self.toggle_language()
                if changed:
                    return [changed]
                # No matching installed input source: preserve the original chord.
                return messages
        else:
            messages = [message]
        language = self.backend.current_language()
        if language not in LANGUAGES:
            return messages
        return [dict(part, data=dict(part.get("data", {}), input_language=language))
                if part.get("type") in {"focus", "keydown", "keyup"} else part
                for part in messages]
