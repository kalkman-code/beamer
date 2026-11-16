"""Beamer's display language: gettext catalogues with British English as the source.

Every user-visible string is written in British English and wrapped where it is used:
``tr("Hide addresses")``, ``trn("{n} machine", "{n} machines", n).format(n=n)``, and
``trc("menu", "Open")`` where one English word needs two translations. A string defined at
import time (a module constant, a table of names) is marked with ``N_()`` and translated where it
is shown, with ``tr(CONSTANT)``, because the language is installed after imports and before the
first window. Names, addresses and anything else a person typed are never passed through ``tr()``:
they go into a template as a ``{field}`` instead, so a machine called "Home" is never translated.

Catalogues are ``locales/<code>.po`` beside VERSION, read with the standard library only. The
English catalogue is the template (``locales/beamer.pot``) that ``tools/i18n_extract.py`` writes;
a language is one more ``.po`` file and one line in ``LANGUAGES``. The US spelling of English
is applied to these authored templates before typed values are interpolated.
"""

from __future__ import annotations

import ast
import ctypes
import gettext as _gettext
import io
import os
import re
import struct
import sys
from pathlib import Path

from core.locale import americanise


# Code -> the language's own name for itself, shown in the Language choice whatever the current
# language is, so a person who cannot read the current one can still find theirs.
LANGUAGES = {
    "en": "English",
    "es": "Español",
    "it": "Italiano",
}
# Not a language: every string bracketed and a third longer, so a test can find text that never
# went through tr() and see where longer languages run out of room. Only BEAMER_LANGUAGE sets it.
PSEUDO = "qps"
AUTOMATIC = "auto"
SOURCE = "en"

_translations: _gettext.NullTranslations = _gettext.NullTranslations()
_language = SOURCE


def N_(message: str) -> str:
    """Mark a string for the catalogue without translating it yet."""
    return message


def tr(message: str) -> str:
    return americanise(_translations.gettext(message))


def matches_translation(text: str, *templates: str) -> bool:
    """Recognise a formatted translated template without interpreting its field values."""
    for template in templates:
        parts = re.split(r"\{[^{}]+\}", tr(template))
        if re.fullmatch(".*?".join(re.escape(part) for part in parts), text, re.DOTALL):
            return True
    return False


def trn(singular: str, plural: str, n: int) -> str:
    return americanise(_translations.ngettext(singular, plural, n))


def trc(context: str, message: str) -> str:
    return americanise(_translations.pgettext(context, message))


def joined(items) -> str:
    """Names as one phrase in the current language: "Mac", "Mac and PC", "Mac, PC and Laptop"."""
    items = [str(item) for item in items]
    if len(items) < 2:
        return "".join(items)
    # Translators: the separator between all but the last two names in a list, "Mac, PC and Laptop".
    head = trc("list", ", ").join(items[:-1])
    # Translators: the last two names of a list; {first} may itself be several names.
    return trc("list", "{first} and {last}").format(first=head, last=items[-1])


def language() -> str:
    """The installed language's code."""
    return _language


def locales_dir() -> Path:
    # py2app sets RESOURCEPATH and PyInstaller _MEIPASS; from source and in the Flatpak, the
    # folder sits beside core/, as VERSION does.
    for root in (os.environ.get("RESOURCEPATH"), getattr(sys, "_MEIPASS", None)):
        if root and (Path(root) / "locales").is_dir():
            return Path(root) / "locales"
    # __spec__.origin, not __file__: py2app's detect_dunder_file recipe copies the whole package of
    # any module naming __file__ into the bundle as loose source, core's tests with it, and those
    # import mac_app, which then copies the build folder into itself (14-10-2026).
    return Path(__spec__.origin).resolve().parent.parent / "locales"


def _unquote(token: str) -> str:
    # A .po string is a C string literal; for the escapes .po files use, \" \\ \n \t, that is
    # also a Python one.
    return ast.literal_eval(token)


def parse_po(text: str) -> list[dict]:
    """Every entry of a .po file, the header included, as {msgctxt, msgid, msgid_plural, msgstr: [..], flags, comments}."""
    entries, entry, field = [], None, None

    def finish():
        if entry is not None and "msgid" in entry:
            entries.append(entry)

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if entry is not None and "msgid" in entry:
                finish()
                entry = None
            entry = entry or {"msgstr": {}, "flags": set(), "comments": []}
            if line.startswith("#,"):
                entry["flags"].update(flag.strip() for flag in line[2:].split(","))
            else:
                entry["comments"].append(raw)
            continue
        if line.startswith('"'):
            value = _unquote(line)
            if field[0] == "msgstr":
                entry["msgstr"][field[1]] += value
            else:
                entry[field[0]] += value
            continue
        keyword, _space, rest = line.partition(" ")
        if keyword in ("msgctxt", "msgid") and entry is not None and "msgid" in entry:
            finish()
            entry = None
        entry = entry or {"msgstr": {}, "flags": set(), "comments": []}
        if keyword.startswith("msgstr"):
            index = int(keyword[7:-1]) if keyword.startswith("msgstr[") else 0
            entry["msgstr"][index] = _unquote(rest)
            field = ("msgstr", index)
        elif keyword in ("msgctxt", "msgid", "msgid_plural"):
            entry[keyword] = _unquote(rest)
            field = (keyword, None)
        else:
            raise ValueError(f"unreadable .po line: {raw!r}")
    finish()
    for item in entries:
        item["msgstr"] = [item["msgstr"][i] for i in sorted(item["msgstr"])]
    return entries


def _mo_bytes(entries: list[dict]) -> bytes:
    # The .mo layout from the GNU gettext manual, so the standard library's GNUTranslations reads
    # plural forms from the catalogue's own Plural-Forms header. Building it in memory keeps the
    # .po the only file there is: nothing to compile, nothing to fall out of step.
    pairs = {}
    for entry in entries:
        if "fuzzy" in entry["flags"] or (entry["msgid"] and not any(entry["msgstr"])):
            continue
        key = entry["msgid"]
        if "msgid_plural" in entry:
            key += "\0" + entry["msgid_plural"]
        if "msgctxt" in entry:
            key = entry["msgctxt"] + "\x04" + key
        pairs[key.encode()] = "\0".join(entry["msgstr"]).encode()
    keys = sorted(pairs)
    ids = strs = b""
    offsets = []
    for key in keys:
        offsets.append((len(key), len(ids), len(pairs[key]), len(strs)))
        ids += key + b"\0"
        strs += pairs[key] + b"\0"
    start = 7 * 4 + 16 * len(keys)
    table = []
    for id_len, id_at, str_len, str_at in offsets:
        table.append((id_len, start + id_at, str_len, start + len(ids) + str_at))
    head = struct.pack("Iiiiiii", 0x950412DE, 0, len(keys), 7 * 4, 7 * 4 + 8 * len(keys), 0, 0)
    return (head + b"".join(struct.pack("ii", a, b) for a, b, _c, _d in table)
            + b"".join(struct.pack("ii", c, d) for _a, _b, c, d in table) + ids + strs)


class _Pseudo(_gettext.NullTranslations):
    _ACCENTS = str.maketrans("aeiouAEIOU", "àéîõüÀÉÎÕÜ")

    def _wrap(self, text: str) -> str:
        parts = re.split(r"(\{[^{}]*\})", text)
        body = "".join(part if part.startswith("{") else part.translate(self._ACCENTS) for part in parts)
        return "⟦" + body + "·" * (len(text) // 3) + "⟧"

    def gettext(self, message):
        return self._wrap(message)

    def ngettext(self, singular, plural, n):
        return self._wrap(singular if n == 1 else plural)

    def pgettext(self, context, message):
        return self._wrap(message)


def load(code: str) -> _gettext.NullTranslations:
    if code == SOURCE:
        return _gettext.NullTranslations()
    if code == PSEUDO:
        return _Pseudo()
    path = locales_dir() / f"{code}.po"
    entries = parse_po(path.read_text(encoding="utf-8"))
    return _gettext.GNUTranslations(io.BytesIO(_mo_bytes(entries)))


def _mac_languages() -> list[str]:
    from Foundation import NSLocale

    return [str(code) for code in NSLocale.preferredLanguages() or ()]


def _windows_languages() -> list[str]:
    count, size = ctypes.c_ulong(), ctypes.c_ulong()
    get = ctypes.windll.kernel32.GetUserPreferredUILanguages
    if not get(8, ctypes.byref(count), None, ctypes.byref(size)):  # MUI_LANGUAGE_NAME
        return []
    buffer = ctypes.create_unicode_buffer(size.value)
    if not get(8, ctypes.byref(count), buffer, ctypes.byref(size)):
        return []
    return [code for code in buffer[:size.value].split("\0") if code]


def _linux_languages() -> list[str]:
    # gettext's own order: LANGUAGE is a list and wins, unless the locale itself is C/POSIX.
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(key)
        if value:
            if value.split(".")[0] in ("C", "POSIX"):
                return []
            listed = os.environ.get("LANGUAGE")
            return (listed.split(":") if listed else []) + [value]
    return []


def system_languages() -> list[str]:
    """The person's preferred languages, most preferred first, as the operating system lists them."""
    try:
        if sys.platform == "darwin":
            return _mac_languages()
        if sys.platform == "win32":
            return _windows_languages()
        return _linux_languages()
    except (AttributeError, OSError, ImportError, ValueError):
        return []


def _base(code: str) -> str:
    return code.replace("-", "_").split("_")[0].split(".")[0].split("@")[0].lower()


def resolve(choice: str | None, preferred: list[str] | None = None) -> str:
    """The language to show for a setting of `choice`: the code itself, or for Automatic the first
    of the system's preferred languages Beamer has, as macOS picks an app's language."""
    forced = os.environ.get("BEAMER_LANGUAGE")
    if forced in LANGUAGES or forced == PSEUDO:
        return forced
    if choice in LANGUAGES:
        return choice
    for code in system_languages() if preferred is None else preferred:
        base = _base(code)
        if base in LANGUAGES:
            return base
    return SOURCE


def install(choice: str | None = AUTOMATIC) -> str:
    """Install the language for `choice` ("auto" or a code) and return its code. A catalogue that
    cannot be read leaves the app in English rather than stopping it starting."""
    global _translations, _language
    code = resolve(choice)
    try:
        _translations = load(code)
    except (OSError, ValueError, SyntaxError):
        code, _translations = SOURCE, _gettext.NullTranslations()
    _language = code
    return code


def is_source() -> bool:
    return _language == SOURCE
