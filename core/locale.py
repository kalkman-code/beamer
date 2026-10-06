"""Choose the display spelling from the operating system's user region."""

from __future__ import annotations

import ctypes
import os
import re
import sys


_WORDS = {
    "colourful": "colorful", "colours": "colors", "colour": "color",
    "behaviour": "behavior",
    "centred": "centered", "centring": "centering", "centres": "centers", "centre": "center",
    "favourites": "favorites", "favourite": "favorite",
    "organisations": "organizations", "organisation": "organization",
    "organisers": "organizers", "organiser": "organizer",
    "organises": "organizes", "organised": "organized", "organising": "organizing", "organise": "organize",
    "licences": "licenses", "licence": "license",
    "minimises": "minimizes", "minimised": "minimized", "minimising": "minimizing", "minimise": "minimize",
    "initialises": "initializes", "initialised": "initialized", "initialising": "initializing", "initialise": "initialize",
    "authorisations": "authorizations", "authorisation": "authorization",
    "authorises": "authorizes", "authorised": "authorized", "authorising": "authorizing", "authorise": "authorize",
    "cancelled": "canceled", "cancelling": "canceling",
}
_WORD_PATTERN = re.compile(r"\b(" + "|".join(sorted(_WORDS, key=len, reverse=True)) + r")\b", re.IGNORECASE)


def _linux_locale() -> str:
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(key)
        if value:
            return value
    return ""


def _country_in_locale(locale_name: str) -> str:
    match = re.search(r"(?:^|[-_])[A-Za-z]{2}[-_]([A-Za-z]{2})(?=$|[_.@])", locale_name)
    return match.group(1).upper() if match else ""


def _windows_locale() -> str:
    name = ctypes.create_unicode_buffer(85)
    if not ctypes.windll.kernel32.GetUserDefaultLocaleName(name, len(name)):
        return ""
    return name.value


def _mac_country() -> str:
    from Foundation import NSLocale, NSLocaleCountryCode

    locale = NSLocale.currentLocale()
    return str(locale.objectForKey_(NSLocaleCountryCode) or "")


def is_us_region() -> bool:
    """Return whether the current user's operating-system region is the United States."""
    try:
        if sys.platform == "darwin":
            return _mac_country().upper() == "US"
        if sys.platform == "win32":
            return _country_in_locale(_windows_locale()) == "US"
        if sys.platform.startswith("linux"):
            return _country_in_locale(_linux_locale()) == "US"
    except (AttributeError, OSError, ImportError, ValueError):
        pass
    return False


def americanise(text: str) -> str:
    """Convert British spellings in text intended for display on a US system."""
    if not is_us_region() or not isinstance(text, str):
        return text

    def replace(match: re.Match) -> str:
        source = match.group(0)
        target = _WORDS[source.casefold()]
        if source.isupper():
            return target.upper()
        if source[0].isupper():
            return target.capitalize()
        return target

    return _WORD_PATTERN.sub(replace, text)
