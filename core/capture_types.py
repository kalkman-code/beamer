"""Key results shared by capture backends and their consumers."""

import unicodedata


class DeadKey:
    def __init__(self, character: str, compositions=None) -> None:
        self.character = character
        self.compositions = compositions


class TextInput:
    def __init__(self, text: str) -> None:
        self.text = text


# US International on Windows reports its acute and diaeresis dead keys as the plain apostrophe
# and quotation mark, which have no combining form. Like Windows, they accent only the vowels and
# y, ' then c is the cedilla, and anything else stays two characters.
_PLAIN_DEAD_KEYS = {"'": "\u00b4", '"': "\u00a8"}
_PLAIN_DEAD_KEY_SPECIALS = {("'", "c"): "\u00e7", ("'", "C"): "\u00c7"}
_PLAIN_DEAD_KEY_TARGETS = "aeiouyAEIOUY"


def compose_dead_key(dead_key, character):
    special = _PLAIN_DEAD_KEY_SPECIALS.get((dead_key, character))
    if special is not None:
        return special
    if dead_key in _PLAIN_DEAD_KEYS:
        if character not in _PLAIN_DEAD_KEY_TARGETS:
            return TextInput(dead_key + character)
        dead_key = _PLAIN_DEAD_KEYS[dead_key]
    name = unicodedata.name(dead_key, "")
    if name.startswith("MODIFIER LETTER "):
        name = name[len("MODIFIER LETTER "):]
    try:
        combining = unicodedata.lookup("COMBINING " + name)
    except KeyError:
        return TextInput(dead_key + character)
    composed = unicodedata.normalize("NFC", character + combining)
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
            if translation == " ":
                result = dead.character
            elif dead.compositions is not None:
                result = dead.compositions.get(translation, dead.character + translation)
            else:
                result = compose_dead_key(dead.character, translation)
            self.swallowed[code] = translation
            return result.text if isinstance(result, TextInput) else result
        self.pending = None
        return None
