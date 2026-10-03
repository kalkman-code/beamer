"""Vernier theming for the macOS sender.

Wraps tokens.py, the one home for every colour, size and face both apps draw with, so no hex value
is written twice. Two palettes, dark and light: `T` holds the one in use and is swapped in place, so
every reader of `T` and every colour from `colour()` follows a switch without being handed anything.
"""

from __future__ import annotations

from pathlib import Path

import AppKit
import objc

import tokens

T: dict[str, str] = tokens.tokens()
PALETTE = tokens.PALETTE
PALETTE_LIGHT = tokens.PALETTE_LIGHT
APPEARANCES = tokens.APPEARANCES
PALETTES = tokens.PALETTES
TYPE = tokens.TYPE
RADIUS = tokens.RADIUS
TRACKING = tokens.TRACKING
MODULE_PADDING = tokens.MODULE_PADDING
RACK_GAP = tokens.RACK_GAP
PAGE_CONTENT_WIDTH = tokens.PAGE_CONTENT_WIDTH
MIN_WINDOW = tokens.MIN_WINDOW["mac"]

# Derived here rather than added to the shared tokens: the mock's negative tracking on the large
# display figures, the looser tracking on the mono permission state, and the peer name, which has
# to fit a third of the status module at 640.
TIGHT = {"status_word": -0.03, "readout": -0.04, "numeral": -0.05}
STATE_TRACKING = 0.06
PEER_SIZE = 17.0
PEER_SIZE_NARROW = 14.0
# The lit edge of the resistance preview's little screen at rest, so it reads as a screen and
# not as an empty field.
PREVIEW_REST = 0.18
# The sidebar and its pages. The sidebar gives up to 30pt toward the minimum window so the page
# keeps its width; the page title is the one large Hanken Grotesk line on each page. Padding is
# top, leading, bottom, trailing.
SIDEBAR_WIDTH = (150.0, 180.0)
SIDEBAR_WORD = 15.0
PAGE_TITLE = 26.0
PAGE_TITLE_NARROW = 22.0
PAGE_PADDING = (28.0, 32.0, 36.0, 32.0)
PAGE_PADDING_NARROW = (22.0, 22.0, 28.0, 22.0)
MODULE_GAP = 20.0

_sans = "SF Pro Text"
_mono = "SF Mono"

# NSFontManager's 0-15 weight scale, against the numeric weights the tokens name.
_MANAGER_WEIGHTS = {400: 5, 500: 6, 600: 8, 700: 9}
_SYSTEM_WEIGHTS = {
    400: AppKit.NSFontWeightRegular,
    500: AppKit.NSFontWeightMedium,
    600: AppKit.NSFontWeightSemibold,
    700: AppKit.NSFontWeightBold,
}

_FONT_SOURCE = Path(__file__).resolve().parent / "win_app" / "assets"


def _register_source_fonts() -> None:
    """The bundle carries the faces under ATSApplicationFontsPath, which macOS registers before the
    app runs. From source nothing does, so the same files the Windows receiver ships are
    registered for this process only."""
    try:
        import CoreText
        import Foundation
    except ImportError:
        return
    for name in tokens.FONT_FILES:
        path = _FONT_SOURCE / name
        if path.exists():
            CoreText.CTFontManagerRegisterFontsForURL(
                Foundation.NSURL.fileURLWithPath_(str(path)), CoreText.kCTFontManagerScopeProcess, None
            )


def init_fonts() -> None:
    global _sans, _mono
    # Before the first family query: NSFontManager caches the family list on that call, so a face
    # registered after it never appears. In the bundle the source folder is absent and this does
    # nothing.
    _register_source_fonts()
    families = set(AppKit.NSFontManager.sharedFontManager().availableFontFamilies())
    _sans = tokens.UI_FAMILY if tokens.UI_FAMILY in families else "SF Pro Text"
    _mono = tokens.MONO_FAMILY if tokens.MONO_FAMILY in families else "Menlo"


def sans() -> str:
    return _sans


def mono() -> str:
    return _mono


def _named(family: str, size: float, weight: int):
    return AppKit.NSFontManager.sharedFontManager().fontWithFamily_traits_weight_size_(
        family, 0, _MANAGER_WEIGHTS.get(weight, 5), size
    )


def font(size: float, weight: int = 400):
    return _named(_sans, size, weight) or AppKit.NSFont.systemFontOfSize_weight_(size, _SYSTEM_WEIGHTS[weight])


def mono_font(size: float | None = None, weight: int = 400):
    size = size if size is not None else TYPE["field_mono"]
    return _named(_mono, size, weight) or AppKit.NSFont.monospacedSystemFontOfSize_weight_(
        size, _SYSTEM_WEIGHTS[weight]
    )


def rgb(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    return tuple(int(value[index:index + 2], 16) / 255 for index in range(0, 6, 2))


_dark = True
_static: dict = {}
_dynamic: dict = {}


def is_dark() -> bool:
    return _dark


def set_dark(dark: bool) -> None:
    global _dark
    _dark = bool(dark)
    T.clear()
    T.update(tokens.PALETTE if _dark else tokens.PALETTE_LIGHT)


def wants_dark(choice: str, system_dark: bool) -> bool:
    """The palette an `appearance` setting asks for: its own, or the system's for "system"."""
    return {"dark": True, "light": False}.get(choice, bool(system_dark))


def system_dark() -> bool:
    """Whether macOS is in dark mode now. Read off the app, which Beamer never pins; only its settings
    window is ever given an appearance of its own."""
    appearance = AppKit.NSApplication.sharedApplication().effectiveAppearance()
    names = [AppKit.NSAppearanceNameAqua, AppKit.NSAppearanceNameDarkAqua]
    return appearance.bestMatchFromAppearancesWithNames_(names) == AppKit.NSAppearanceNameDarkAqua


def _resolved(name: str):
    key = (_dark, name)
    if key not in _static:
        _static[key] = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(*rgb(T[name]), 1.0)
    return _static[key]


def colour(name: str):
    """A colour that resolves against the palette in use each time it is drawn, so a label, a field or
    a drawRect keeps up with a switch by being redrawn. It ignores the appearance AppKit offers: the
    window's appearance is set from the same choice, and a CGColor taken outside drawing would
    otherwise resolve against the system's."""
    if name not in _dynamic:
        _dynamic[name] = AppKit.NSColor.colorWithName_dynamicProvider_(None, lambda _appearance: _resolved(name))
    return _dynamic[name]


def cg(name: str, alpha: float = 1.0):
    """The palette colour as a CGColor, fixed at the palette in use: layers keep what they are given,
    so set one with `tint` if it must follow a switch."""
    return _resolved(name).colorWithAlphaComponent_(alpha).CGColor()


# A layer's palette colours are kept on the layer under these keys (CALayer stores any key) as
# "name" or "name@alpha", and a gradient's as "|"-joined, so `retint` can resolve them again.
_ROLES = {
    "background": "setBackgroundColor_",
    "border": "setBorderColor_",
    "shadow": "setShadowColor_",
    "fill": "setFillColor_",
    "stroke": "setStrokeColor_",
    "foreground": "setForegroundColor_",
}
_KEY = "beamerTint_"


def _spec(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    name, alpha = value
    return f"{name}@{alpha}"


def _cg_spec(spec: str):
    name, _, alpha = spec.partition("@")
    return cg(name, float(alpha) if alpha else 1.0)


def tint(layer, **roles) -> None:
    """Sets layer colours by palette name, `background="panel"` or `border=("signal", 0.4)`, and
    remembers them so `retint` can follow a switch. None clears the colour. `colours=[...]` sets a
    gradient's stops the same way."""
    for role, value in roles.items():
        if role == "colours":
            specs = [_spec(item) for item in value]
            layer.setColors_([_cg_spec(spec) for spec in specs])
            layer.setValue_forKey_("|".join(specs), _KEY + role)
            continue
        spec = _spec(value)
        getattr(layer, _ROLES[role])(_cg_spec(spec) if spec else None)
        layer.setValue_forKey_(spec, _KEY + role)


def retint(layer) -> None:
    """Resolves every tinted colour on `layer` and its sublayers against the palette in use."""
    for role, setter in _ROLES.items():
        spec = layer.valueForKey_(_KEY + role)
        if spec:
            getattr(layer, setter)(_cg_spec(str(spec)))
    specs = layer.valueForKey_(_KEY + "colours")
    if specs:
        layer.setColors_([_cg_spec(spec) for spec in str(specs).split("|")])
    for sublayer in layer.sublayers() or ():
        retint(sublayer)


def repaint(view) -> None:
    """Brings a view tree up to the palette in use: tinted layers resolve again, views that keep a
    colour of their own get `refresh_colours()`, and everything redraws. Walked view by view: a
    layer-backed view's layer does not list its subviews' layers among its sublayers."""
    if view.layer() is not None:
        retint(view.layer())
    refresh = getattr(view, "refresh_colours", None)
    if callable(refresh):
        refresh()
    view.setNeedsDisplay_(True)
    for subview in view.subviews() or ():
        repaint(subview)


def appearance_named(dark: bool):
    return AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua if dark else AppKit.NSAppearanceNameAqua)

# The longest a line of running text may get before it wraps, whatever the window width.
READING_WIDTH = tokens.READING_WIDTH


class AppearanceWatch(AppKit.NSObject):
    """Calls `callback()` whenever macOS switches between light and dark, by watching the app's
    effective appearance, which AppKit has already updated by the time it is told."""

    def initWithCallback_(self, callback):
        self = objc.super(AppearanceWatch, self).init()
        if self is None:
            return None
        self.callback = callback
        AppKit.NSApplication.sharedApplication().addObserver_forKeyPath_options_context_(
            self, "effectiveAppearance", AppKit.NSKeyValueObservingOptionNew, None
        )
        return self

    def observeValueForKeyPath_ofObject_change_context_(self, _path, _object, _change, _context):
        self.callback()

    def stop(self):
        AppKit.NSApplication.sharedApplication().removeObserver_forKeyPath_(self, "effectiveAppearance")
