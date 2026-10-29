"""Key results shared by capture backends and their consumers."""

import unicodedata


class DeadKey:
    def __init__(self, character: str, mark: str = "") -> None:
        self.character = character
        self.mark = mark


class TextInput:
    def __init__(self, text: str) -> None:
        self.text = text


def compose_dead_key(dead_key, character, mark=""):
    """Combine a dead key with the next character. `mark` is the dead key's combining mark where the
    layout gave one; without it the mark is inferred from the spacing accent's name, which fails for
    a dead key that terminates to an ASCII quote (U.S. International's acute and diaeresis), so the
    Mac path reads it from the layout and passes it in."""
    if not mark:
        name = unicodedata.name(dead_key, "")
        if name.startswith("MODIFIER LETTER "):
            name = name[len("MODIFIER LETTER "):]
        try:
            mark = unicodedata.lookup("COMBINING " + name)
        except KeyError:
            return TextInput(dead_key + character)
    composed = unicodedata.normalize("NFC", character + mark)
    return composed if len(composed) == 1 else TextInput(dead_key + character)


class MacDeadKeys:
    """Composition belongs to the remote capture session, never macOS's transient dead state."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.pending = None
        self.swallowed = {}
        self.layout = None

    def feed(self, code, down, translation, layout, shortcut=False):
        if self.layout != layout:
            self.reset()
            self.layout = layout
        if shortcut:
            self.pending = None
        if code in self.swallowed:
            repeat = self.swallowed[code]
            if not down:
                self.swallowed.pop(code)
            return repeat if down and not shortcut else ""
        if not down:
            return None
        if shortcut:
            self.pending = None
            return None
        if isinstance(translation, DeadKey):
            text = self.pending.character if self.pending else ""
            self.pending = None if (self.pending and self.pending.character == translation.character) else translation
            self.swallowed[code] = ""
            return text
        if self.pending and isinstance(translation, str) and translation.isprintable():
            dead, self.pending = self.pending, None
            result = dead.character if translation == " " else compose_dead_key(dead.character, translation, dead.mark)
            self.swallowed[code] = translation
            return result.text if isinstance(result, TextInput) else result
        self.pending = None
        return None
