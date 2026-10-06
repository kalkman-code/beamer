"""Every page of the settings window, rendered offscreen in Light and in Dark, read back as pixels:
each piece of text against what is behind it, the cards against the palette, and both after the
appearance changes with the window already built (05-10-2026: Light chosen on Design left the
Crossing page's cards dark, because the page was out of the window when the palette switched).

Never put on screen. BEAMER_RENDERS=<folder> also writes each page at 900 pt in both appearances."""

import base64
import copy
import json
import logging
import os
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import AppKit  # noqa: E402
import Quartz  # noqa: E402
from Foundation import NSDate, NSRunLoop  # noqa: E402

AppKit.NSApplication.sharedApplication()

import bridge  # noqa: E402
import kvm_bridge_app  # noqa: E402
import pages  # noqa: E402
import settings_store  # noqa: E402
import theme  # noqa: E402
import tokens  # noqa: E402
from bridge_fakes import PAIRED_TOKEN  # noqa: E402
from core import protocol  # noqa: E402
from fake_link import FakeLink  # noqa: E402
from wake import WakingController  # noqa: E402

PEER = protocol.id_text(bytes(range(40, 56)))
WIDTH = 900


def _luminance(rgb):
    def channel(value):
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    red, green, blue = (channel(value) for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first, second):
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _srgb(colour, view):
    """A system colour such as labelColor resolves against the appearance it is drawn in, which is
    the view's, not this process's."""
    resolved = []
    view.effectiveAppearance().performAsCurrentDrawingAppearance_(
        lambda: resolved.append(colour.colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())))
    colour = resolved[0]
    return colour.redComponent(), colour.greenComponent(), colour.blueComponent(), colour.alphaComponent()


def _hex_rgb(value):
    return theme.rgb(value)


def _shown(view, root):
    alpha = 1.0
    while view is not None and view != root:
        if view.isHidden():
            return None
        alpha *= view.alphaValue()
        view = view.superview()
    return alpha


def _walk(view):
    yield view
    for child in view.subviews() or ():
        yield from _walk(child)


def _runs(string):
    """(colour, point size, bold) for every run of an attributed string."""
    runs, index, length = [], 0, string.length()
    while index < length:
        attributes, extent = string.attributesAtIndex_effectiveRange_(index, None)
        colour = attributes.get(AppKit.NSForegroundColorAttributeName)
        font = attributes.get(AppKit.NSFontAttributeName)
        if colour is not None and string.string()[index:index + extent.length].replace("\ufffc", "").strip():
            bold = bool(font) and AppKit.NSFontManager.sharedFontManager().weightOfFont_(font) >= 9
            runs.append((colour, font.pointSize() if font else 13.0, bold))
        index += max(extent.length, 1)
    return runs


class Bitmap:
    """The content view drawn the way the window draws it, read in points from the top left."""

    def __init__(self, view):
        self.view = view
        width, height = view.bounds().size
        self.rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
        view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), self.rep)
        self.scale = self.rep.pixelsWide() / width
        self.height = height
        self.data = bytes(self.rep.bitmapData())
        self.row = self.rep.bytesPerRow()
        self.channels = self.rep.samplesPerPixel()

    def pixel(self, x, y):
        """Colour at view point (x, y), y measured up as AppKit does."""
        px = min(max(int(x * self.scale), 0), self.rep.pixelsWide() - 1)
        py = min(max(int((self.height - y) * self.scale), 0), self.rep.pixelsHigh() - 1)
        offset = py * self.row + px * self.channels
        return tuple(value / 255 for value in self.data[offset:offset + 3])

    def ground(self, rect):
        """The commonest colour in `rect`: what text in it is drawn on."""
        (x, y), (width, height) = rect
        counts = Counter()
        steps = 24
        for i in range(steps):
            for j in range(max(4, steps // 3)):
                sample = self.pixel(x + width * (i + 0.5) / steps, y + height * (j + 0.5) / max(4, steps // 3))
                counts[tuple(round(value * 255) for value in sample)] += 1
        return tuple(value / 255 for value in counts.most_common(1)[0][0])


def _settle(window):
    for _ in range(3):
        window.contentView().layoutSubtreeIfNeeded()
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.01))
    assert not window.isVisible()


class AppearanceContrast(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        theme.init_fonts()
        cls.directory = tempfile.TemporaryDirectory()
        path = Path(cls.directory.name) / "config.json"
        raw = settings_store.config_to_raw(settings_store.editable_default_config())
        raw.update(host="192.0.2.20", auth_token=PAIRED_TOKEN, appearance="dark")
        path.write_text(json.dumps(raw))
        store = settings_store.SettingsStore(path)
        settings = copy.deepcopy(store.current())
        secret = base64.urlsafe_b64encode(bytes(range(100, 132))).decode("ascii").rstrip("=")
        settings["peers"] = [{**settings_store.PEER_DEFAULTS, "id": PEER, "name": "Desk PC", "platform": "windows",
                              "token": secret, "host": "192.0.2.30", "port": 24820, "paired_at": 1_700_000_000,
                              "linked": True, "side": "right", "side_set_at": 100,
                              "side_by": settings["machine_id"]}]
        settings["zones"] = [{"peer": PEER, "kind": "edge"}]
        store.save_settings(settings)
        logger = logging.getLogger("test-appearance-contrast")
        book, identity = bridge.links_from_store(store, "1.5.0")
        controller = WakingController(store.load(), logger=logger, book=book, identity=identity, link_factory=FakeLink)
        cls.later = mock.patch.object(kvm_bridge_app.AppHelper, "callLater", lambda _delay, _call: None)
        cls.later.start()
        with mock.patch.object(kvm_bridge_app, "accessibility_granted", return_value=True), \
                mock.patch.object(kvm_bridge_app, "input_monitoring_granted", return_value=True):
            cls.control = kvm_bridge_app.ControlWindow.alloc().initWithController_settingsStore_logger_(
                controller, store, logger)
        cls.control.windows_input = mock.Mock()
        cls.control.windows_input.server.links.return_value = []
        cls.control.window.setFrame_display_(((100000, 100000), (WIDTH, 900)), False)
        cls.renders = os.environ.get("BEAMER_RENDERS")

    @classmethod
    def tearDownClass(cls):
        cls.control.appearance_watch.stop()
        cls.later.stop()
        theme.set_dark(True)
        cls.directory.cleanup()

    def _choose(self, choice):
        self.control.appearance_select.value = choice
        self.control._apply_appearance(choice)

    def _show(self, key):
        """The page at 900 pt, the window tall enough that the whole page is in view."""
        control = self.control
        control._select_page(key)
        window = control.window
        _settle(window)
        page = control.pages[key].documentView()
        chrome = window.frame().size.height - control.pages[key].contentView().bounds().size.height
        window.setFrame_display_(((100000, 100000), (WIDTH, page.frame().size.height + chrome + 2)), False)
        _settle(window)
        return Bitmap(window.contentView())

    def _texts(self, root):
        for view in _walk(root):
            if isinstance(view, AppKit.NSButton):
                string = view.attributedTitle()
            elif isinstance(view, AppKit.NSTextField) or type(view).__name__ == "_PreviewCaption":
                string = view.attributedStringValue()
            else:
                continue
            if string is None or not str(string.string()).strip():
                continue
            alpha = _shown(view, root)
            # Faded to show it is off, as a switch the Mac does not have: WCAG 1.4.3 exempts an
            # inactive control, and these fade as a whole.
            if alpha is None or alpha < 0.99:
                continue
            if isinstance(view, AppKit.NSControl) and not view.isEnabled():
                continue
            rect = view.convertRect_toView_(view.bounds(), root)
            if rect.size.width < 2 or rect.size.height < 2:
                continue
            yield view, string, rect

    def _check_text(self, bitmap, label):
        failures, checked = [], 0
        root = self.control.window.contentView()
        for view, string, rect in self._texts(root):
            behind = bitmap.ground(rect)
            for colour, size, bold in _runs(string):
                red, green, blue, alpha = _srgb(colour, view)
                ink = tuple(alpha * value + (1 - alpha) * base for value, base in zip((red, green, blue), behind))
                ratio = contrast(ink, behind)
                needed = 3.0 if size >= 18 or (size >= 14 and bold) else 4.5
                checked += 1
                if ratio < needed:
                    failures.append(f"{label}: {str(string.string())[:40]!r} {ratio:.2f} < {needed}")
        return checked, failures

    def _check_cards(self, bitmap, label):
        """Every tinted layer anywhere in the window, pages out of it included, carries the
        palette in use, and the cards in view measure as `panel` on screen."""
        failures = []
        palette = tokens.PALETTE if theme.is_dark() else tokens.PALETTE_LIGHT
        roots = [self.control.window.contentView(), *self.control.pages.values()]
        for root in roots:
            for view in _walk(root):
                layer = view.layer()
                spec = layer.valueForKey_(theme._KEY + "background") if layer is not None else None
                if not spec:
                    continue
                name = str(spec).partition("@")[0]
                red, green, blue = Quartz.CGColorGetComponents(layer.backgroundColor())[:3]
                expected = _hex_rgb(palette[name])
                if max(abs(a - b) for a, b in zip((red, green, blue), expected)) > 1.5 / 255:
                    failures.append(f"{label}: {type(view).__name__} {name} is not the {'dark' if theme.is_dark() else 'light'} {name}")
        content = self.control.window.contentView()
        page = self.control.pages[self.control.page]
        panel = _hex_rgb(palette["panel"])
        cards = 0
        for view in _walk(page):
            layer = view.layer()
            if layer is None or str(layer.valueForKey_(theme._KEY + "background") or "") != "panel":
                continue
            if _shown(view, content) is None or view.window() is None:
                continue
            (x, y), (width, height) = view.convertRect_toView_(view.bounds(), content)
            if width < 40 or height < 40:
                continue
            cards += 1
            measured = bitmap.pixel(x + 4, y + height - 4)
            if max(abs(a - b) for a, b in zip(measured, panel)) > 3 / 255:
                failures.append(f"{label}: card at {round(x)},{round(y)} measures {tuple(round(v * 255) for v in measured)}")
        return cards, failures

    def _sweep(self, appearance):
        checked = cards = 0
        failures = []
        for key in pages.KEYS:
            bitmap = self._show(key)
            label = f"{appearance}/{key}"
            count, text_failures = self._check_text(bitmap, label)
            panels, card_failures = self._check_cards(bitmap, label)
            checked += count
            cards += panels
            failures += text_failures + card_failures
            if self.renders:
                folder = Path(self.renders)
                folder.mkdir(parents=True, exist_ok=True)
                png = bitmap.rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
                png.writeToFile_atomically_(str(folder / f"{key}-{appearance}-{WIDTH}.png"), True)
        return checked, cards, failures

    def test_every_page_reads_in_dark_and_in_light_chosen_from_another_page(self):
        # Toby's path: the window built dark, Light chosen on the Design page, then every other page.
        self._choose("dark")
        self.control._select_page("design")
        self._choose("light")
        self.assertFalse(theme.is_dark())
        checked, cards, failures = self._sweep("light")
        self.assertGreater(checked, 150)
        self.assertGreater(cards, 10)
        self.assertEqual(failures, [])
        self.control._select_page("crossing")
        self._choose("dark")
        checked, cards, failures = self._sweep("dark")
        self.assertGreater(checked, 150)
        self.assertGreater(cards, 10)
        self.assertEqual(failures, [])

    def test_an_odd_way_out_fills_its_row_instead_of_leaving_a_grey_slot(self):
        self._show("crossing")
        tiles = list(self.control.method_boxes.values())
        last, frame = tiles[-1].view, tiles[-1].view.superview()
        self.assertEqual(len(tiles) % 2, 1)
        self.assertAlmostEqual(last.frame().origin.x + last.frame().size.width, frame.bounds().size.width, delta=0.5)

    def test_system_switching_while_the_window_exists_repaints_pages_out_of_view(self):
        self._choose("system")
        for dark in (False, True, False):
            self.control._select_page("overview" if dark else "keyboard")
            with mock.patch.object(theme, "system_dark", return_value=dark):
                self.control.appearance_watch.callback()
            self.assertEqual(theme.is_dark(), dark)
            bitmap = self._show("crossing")
            _count, text_failures = self._check_text(bitmap, f"system-{dark}/crossing")
            _cards, card_failures = self._check_cards(bitmap, f"system-{dark}/crossing")
            self.assertEqual(text_failures + card_failures, [])
        self._choose("dark")


if __name__ == "__main__":
    unittest.main()
