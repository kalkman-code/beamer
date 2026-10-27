"""Dead-key composition, shared by the capture backends.

A dead key (acute, grave, circumflex, ...) types nothing on its own and waits
for the next character to combine with. Both captures compose here in software
so the sequence never depends on the OS holding a pending-accent state across
two intercepted events."""

import unicodedata
from typing import Union

from core.capture_types import TextInput


def compose(dead_key: str, character: str) -> Union[str, TextInput]:
    """`dead_key`, a spacing accent like U+00B4, combined with `character`: the
    single combined character when they combine, else the two of them as text.
    Used by the Windows capture, whose dead keys yield a spacing accent."""
    name = unicodedata.name(dead_key, "")
    if name.startswith("MODIFIER LETTER "):
        name = name[len("MODIFIER LETTER "):]
    try:
        combining = unicodedata.lookup("COMBINING " + name)
    except KeyError:
        return TextInput(dead_key + character)
    composed = unicodedata.normalize("NFC", character + combining)
    return composed if len(composed) == 1 else TextInput(dead_key + character)


def compose_combining(mark: str, spacing: str, character: str) -> str:
    """`character` combined with a dead key given as its combining mark, or the spacing accent and
    the character side by side when they do not combine. The Mac capture reads the mark straight
    from the layout because a spacing form is not enough: U.S. International terminates its acute and
    diaeresis to an ASCII quote, not U+00B4/U+00A8, so the mark cannot be inferred from that char."""
    composed = unicodedata.normalize("NFC", character + mark)
    return composed if len(composed) == 1 else spacing + character
