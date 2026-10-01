"""Vernier theming for the Windows receiver, built from tokens.py, light and dark.

Widgets opt into a component with a `vernier` property set before the stylesheet is applied, or
re-polished with `repolish(widget)` after it changes. QSS cannot express letter spacing, so the
tracked labels and the big numerals take a QFont. The base face is the application font, never a
QWidget rule: a stylesheet font-size beats every QFont set on a widget beneath it.
"""

from __future__ import annotations

import os
import sys

import titlebar
import tokens

# Swapped in place by set_dark(), never reassigned, so a widget that took a reference to P (the
# stylesheet template's `values = dict(P)` included) sees the new palette without being rebuilt.
P = dict(tokens.PALETTE)
_dark = True
TYPE = tokens.TYPE
RADIUS = tokens.RADIUS

_sans = "Segoe UI"
_mono = "Consolas"

# Derived here because tokens.py has no value for them: the heading size inside a module (the
# Windows status word, 22px, between body and status_word_narrow), and the countdown numeral.
HEADING = 22.0
COUNT = 30.0

# The sidebar's fixed width. Windows does not reflow it with the window the way the Mac's
# does; a fixed width leaves 640 - SIDEBAR_WIDTH - 1 of content, which every page's grid is
# laid out to fit.
# 176, not 168, so the foot's kalkmancode.co.uk/beamer fits as Windows draws it.
SIDEBAR_WIDTH = 176

# The pairing code sits beside the countdown and the Pair button, in a page narrowed by the
# sidebar -- tokens.TYPE["code"] (92px, shared with the Mac's own pairing card) does not fit
# that row at 640 wide, so this page uses its own smaller size.
PAIRING_CODE = 48.0

# One tone per receiver state, shared by the LED, the tray dot and the heading.
STATE_TONE = {
    "connected": "signal",
    "waiting": "amber",
    "stopped": "off",
    "error": "fault",
}


def assets_dir() -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        return os.path.join(base, "assets")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def init_fonts() -> None:
    """Loads the bundled faces. A face that will not load leaves Segoe UI or Consolas in its
    place rather than stopping the window opening."""
    global _sans, _mono
    from PySide6.QtGui import QFontDatabase

    loaded: set[str] = set()
    for name in tokens.FONT_FILES:
        path = os.path.join(assets_dir(), name)
        handle = QFontDatabase.addApplicationFont(path) if os.path.exists(path) else -1
        if handle != -1:
            loaded.update(QFontDatabase.applicationFontFamilies(handle))
    _sans = tokens.UI_FAMILY if tokens.UI_FAMILY in loaded else "Segoe UI"
    _mono = tokens.MONO_FAMILY if tokens.MONO_FAMILY in loaded else "Consolas"


def sans() -> str:
    return _sans


def mono() -> str:
    return _mono


def colour(name: str) -> str:
    return P[name]


def set_dark(dark: bool) -> None:
    """Swaps every colour `colour()`/`P` hands out to the dark or light palette, in place."""
    global _dark
    _dark = dark
    P.clear()
    P.update(tokens.PALETTE if dark else tokens.PALETTE_LIGHT)


def is_dark() -> bool:
    return _dark


def wants_dark(choice: str, system_dark: bool) -> bool:
    """What the `appearance` setting means for `set_dark`: "dark" and "light" are absolute,
    "system" and anything unrecognised follow the system."""
    if choice == "dark":
        return True
    if choice == "light":
        return False
    return system_dark


def system_dark() -> bool:
    """The system's own light/dark choice. AppsUseLightTheme, not the taskbar's
    SystemUsesLightTheme, which Windows lets disagree with it -- this window is an app."""
    try:
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            ) as key:
                return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except (ImportError, OSError):
        pass
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    if app is None:
        return True
    from PySide6.QtCore import Qt

    return app.styleHints().colorScheme() == Qt.ColorScheme.Dark


def watch_system(callback) -> None:
    """Calls `callback()` whenever Windows' own light/dark choice changes, for "system" to follow
    it live. A no-op with no QGuiApplication yet, or on a PySide6 old enough to predate
    colorSchemeChanged (6.5) -- the guard costs nothing and the pin in requirements-win.txt may
    move under us."""
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    if app is None:
        return
    hints = app.styleHints()
    if hasattr(hints, "colorSchemeChanged"):
        hints.colorSchemeChanged.connect(lambda _scheme: callback())


def state_tone(state: str) -> str:
    return STATE_TONE.get(state, "off")


def state_colour(state: str) -> str:
    return P[state_tone(state)]


def font(size: float, weight: int = 400):
    from PySide6.QtGui import QFont

    face = QFont(_sans)
    face.setPixelSize(round(size))
    face.setWeight(QFont.Weight(weight))
    return face


def mono_font(size: float, weight: int = 400):
    from PySide6.QtGui import QFont

    face = QFont(_mono)
    face.setStyleHint(QFont.StyleHint.Monospace)
    face.setPixelSize(round(size))
    face.setWeight(QFont.Weight(weight))
    return face


def eyebrow_font(key: str = "eyebrow"):
    from PySide6.QtGui import QFont

    face = font(TYPE[key], 700)
    face.setCapitalization(QFont.Capitalization.AllUppercase)
    face.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, TYPE[key] * tokens.TRACKING[key])
    return face


def repolish(widget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def stylesheet() -> str:
    values = dict(P)
    values.update(
        sans=_sans,
        mono=_mono,
        body=round(TYPE["body"]),
        note=round(TYPE["note"]),
        small=round(TYPE["small"]),
        field_mono=round(TYPE["field_mono"]),
        button=round(TYPE["button"]),
        r_field=round(RADIUS["field"]),
        r_button=round(RADIUS["button"]),
        r_keycap=round(RADIUS["keycap"]),
    )
    return _TEMPLATE.format(**values)


_TEMPLATE = """
QWidget {{
    background: {ground};
    color: {ink};
}}
QWidget[vernier="rack"] {{ background: {rule}; }}
QFrame[vernier="module"] {{ background: {panel}; border: 0; }}
QFrame[vernier="commit"] {{ background: {ground}; border: 0; border-top: 1px solid {rule}; }}
QWidget[vernier="plain"] {{ background: transparent; }}
QWidget[vernier="rule"] {{ background: {rule}; }}
QWidget[vernier="sidebar"] {{ background: {panel}; }}

QLabel {{ background: transparent; color: {ink_2}; }}
QLabel[vernier="eyebrow"] {{ color: {ink_3}; }}
QLabel[vernier="heading"] {{ color: {ink}; }}
QLabel[vernier="heading-amber"] {{ color: {amber}; }}
QLabel[vernier="heading-fault"] {{ color: {fault}; }}
QLabel[vernier="note"] {{ color: {ink_2}; font-size: {note}px; }}
QLabel[vernier="note-amber"] {{ color: {amber}; font-size: {note}px; }}
QLabel[vernier="note-fault"] {{ color: {fault}; font-size: {note}px; }}
QLabel[vernier="note-live"] {{ color: {signal}; font-size: {note}px; }}
QLabel[vernier="note-quiet"] {{ color: {ink_3}; font-size: {note}px; }}
QLabel[vernier="key"] {{ color: {ink_2}; font-size: {note}px; }}
QLabel[vernier="mono"] {{ color: {ink_2}; font-family: "{mono}"; font-size: {note}px; }}
QLabel[vernier="readout"] {{ color: {ink}; font-family: "{mono}"; font-size: {note}px; font-weight: 700; }}
QLabel[vernier="code"] {{ color: {ink}; }}
QLabel[vernier="code-idle"] {{ color: {ink_3}; }}
QLabel[vernier="count"] {{ color: {ink}; }}
QLabel[vernier="count-idle"] {{ color: {ink_3}; }}
QLabel[vernier="sidebar-link"] {{ color: {ink}; font-size: {body}px; font-weight: 700; }}

QPushButton {{
    background: {well};
    border: 1px solid {edge};
    border-radius: {r_button}px;
    color: {ink};
    font-size: {button}px;
    font-weight: 600;
    padding: 7px 12px;
}}
QPushButton:hover {{ border-color: {ink_3}; }}
QPushButton:focus {{ border: 2px solid {signal}; padding: 6px 11px; }}
QPushButton:disabled {{ color: {ink_3}; border-color: {rule}; background: {panel}; }}
QPushButton[vernier="primary"] {{ background: {ink}; border-color: {ink}; color: {ground}; }}
QPushButton[vernier="primary"]:hover {{ background: {ink_2}; border-color: {ink_2}; }}
QPushButton[vernier="primary"]:focus {{ border: 2px solid {signal}; }}
QPushButton[vernier="primary"]:disabled {{ background: {well}; border-color: {rule}; color: {ink_3}; }}
QPushButton[vernier="live"] {{ background: {signal}; border-color: {signal}; color: {ground}; }}
QPushButton[vernier="live"]:focus {{ border: 2px solid {ink}; }}
QPushButton[vernier="small"] {{ font-size: {small}px; padding: 4px 10px; }}
QPushButton[vernier="small"]:focus {{ padding: 3px 9px; }}
QPushButton[vernier="choice"] {{ color: {ink_2}; font-size: {small}px; font-weight: 400; padding: 4px 8px; }}
QPushButton[vernier="choice"]:checked {{ background: {ink}; border-color: {ink}; color: {ground}; font-weight: 600; }}
QPushButton[vernier="choice"]:focus {{ padding: 3px 7px; }}
QPushButton[vernier="choice"]:disabled {{ color: {ink_3}; border-color: {rule}; background: {panel}; }}
QPushButton[vernier="choice"]:checked:disabled {{ background: {well}; border-color: {rule}; color: {ink_2}; }}

QPushButton[vernier="page"] {{
    background: transparent;
    border: 0;
    border-radius: {r_button}px;
    color: {ink_2};
    font-size: {body}px;
    font-weight: 500;
    padding: 8px 12px;
    text-align: left;
}}
QPushButton[vernier="page"]:hover {{ background: {well}; color: {ink}; }}
QPushButton[vernier="page"]:checked {{ background: {well}; color: {ink}; font-weight: 700; }}
QPushButton[vernier="page"]:focus {{ border: 1px solid {signal}; padding: 7px 11px; }}

QPushButton[vernier="foot"] {{
    background: transparent;
    border: 0;
    color: {ink_3};
    font-size: 11px;
    font-weight: 400;
    padding: 12px 4px 14px 16px;
    text-align: left;
}}
QPushButton[vernier="foot"]:hover {{ color: {ink_2}; }}
QPushButton[vernier="foot"]:focus {{ border: 1px solid {signal}; padding: 11px 3px 13px 15px; }}
QPushButton[vernier="keycap"] {{
    background: {ground};
    border: 1px solid {edge};
    border-bottom: 3px solid {edge};
    border-radius: {r_keycap}px;
    padding: 0;
}}
QPushButton[vernier="keycap"]:hover {{ border-color: {ink_3}; }}
QPushButton[vernier="keycap"]:focus {{ border-color: {signal}; }}
QPushButton[vernier="keycap-live"] {{
    background: {ground};
    border: 1px solid {signal};
    border-bottom: 3px solid {signal};
    border-radius: {r_keycap}px;
    padding: 0;
}}
QLabel[vernier="keycap"] {{ color: {ink}; }}
QLabel[vernier="keycap-live"] {{ color: {signal}; }}
QLabel[vernier="small"] {{ color: {ink_3}; font-size: {small}px; }}
QLabel[vernier="tile-name"] {{ color: {ink}; font-size: {body}px; font-weight: 600; }}
QFrame[vernier="tile"] {{ background: {ground}; border: 1px solid {rule}; border-radius: {r_button}px; }}
QFrame[vernier="tile"]:hover {{ border-color: {edge}; }}
QFrame[vernier="tile"]:focus {{ border: 2px solid {signal}; }}
QFrame[vernier="tile-on"] {{ background: {ground}; border: 1px solid {edge}; border-radius: {r_button}px; }}
QFrame[vernier="tile-on"]:focus {{ border: 2px solid {signal}; }}
QFrame[vernier="entry"] {{ background: {well}; border: 1px solid {rule}; border-radius: {r_field}px; }}
QPushButton[vernier="remove"] {{
    background: transparent;
    border: 0;
    color: {ink_3};
    font-size: {small}px;
    font-weight: 500;
    padding: 2px 6px;
}}
QPushButton[vernier="remove"]:hover {{ color: {fault}; }}
QPushButton[vernier="remove"]:focus {{ border: 1px solid {signal}; padding: 1px 5px; }}
QCheckBox {{ background: transparent; color: {ink}; font-size: {body}px; spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {edge}; border-radius: 3px; background: {well}; }}
QCheckBox::indicator:hover {{ border-color: {ink_3}; }}
QCheckBox::indicator:checked {{ background: {signal}; border-color: {signal}; }}

QSlider::groove:horizontal {{ height: 4px; background: {well}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {signal}; border-radius: 2px; }}
QSlider::add-page:horizontal {{ background: {well}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 14px; height: 14px; margin: -6px 0; border-radius: 7px; background: {ink}; border: 1px solid {ink}; }}
QSlider::handle:horizontal:hover {{ background: {signal}; border-color: {signal}; }}
QSlider::handle:horizontal:disabled {{ background: {ink_3}; border-color: {ink_3}; }}
QSlider::groove:horizontal:disabled {{ background: {panel}; }}

QLineEdit {{
    background: {ground};
    border: 1px solid {edge};
    border-radius: {r_field}px;
    color: {ink};
    font-family: "{mono}";
    font-size: {field_mono}px;
    padding: 6px 8px;
    selection-background-color: {signal_dim};
    selection-color: {ink};
    lineedit-password-character: 8226;
}}
QLineEdit:focus {{ border: 2px solid {signal}; padding: 5px 7px; }}

QScrollArea {{ background: {ground}; border: 0; }}
QScrollBar:vertical {{ background: {ground}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {edge}; border-radius: 3px; min-height: 32px; margin: 2px; }}
QScrollBar::handle:vertical:hover {{ background: {ink_3}; }}
QScrollBar:horizontal {{ height: 0; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QToolTip {{
    background: {well};
    border: 1px solid {edge};
    color: {ink};
    padding: 5px 8px;
}}
QMenu {{ background: {panel}; border: 1px solid {edge}; color: {ink}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: {r_field}px; }}
QMenu::item:selected {{ background: {well}; color: {ink}; }}
QMenu::item:disabled {{ color: {ink_3}; }}
QMenu::separator {{ height: 1px; background: {rule}; margin: 4px 6px; }}
"""


def apply_titlebar(hwnd: int) -> None:
    """Paints the native title bar at hwnd in Vernier's current ground, light or dark, so it stops
    following the user's accent colour."""
    titlebar.apply_caption(hwnd, P, is_dark())
