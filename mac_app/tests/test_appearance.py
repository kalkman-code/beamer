"""The settings window's light and dark palettes: the choice, the Mac's own setting and a live switch.

The window is built for real, as the README's screenshots build it, and never put on screen."""

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import AppKit  # noqa: E402
import Quartz  # noqa: E402

AppKit.NSApplication.sharedApplication()

import kvm_bridge_app  # noqa: E402
import motion  # noqa: E402
import settings_store  # noqa: E402
import theme  # noqa: E402
import tokens  # noqa: E402
from wake import WakingController  # noqa: E402


def _hex(cg_colour):
    red, green, blue = Quartz.CGColorGetComponents(cg_colour)[:3]
    return "#" + "".join(f"{round(value * 255):02x}" for value in (red, green, blue))


class ThemeTests(unittest.TestCase):
    def tearDown(self):
        theme.set_dark(True)

    def test_wants_dark_keeps_a_choice_and_follows_the_system_otherwise(self):
        self.assertTrue(theme.wants_dark("dark", False))
        self.assertFalse(theme.wants_dark("light", True))
        self.assertTrue(theme.wants_dark("system", True))
        self.assertFalse(theme.wants_dark("system", False))

    def test_a_tinted_layer_follows_a_switch_and_a_dynamic_colour_resolves_to_the_palette_in_use(self):
        layer = Quartz.CALayer.layer()
        theme.tint(layer, background="panel", border=("signal", 0.5))
        theme.set_dark(False)
        theme.retint(layer)
        self.assertEqual(_hex(layer.backgroundColor()), tokens.PALETTE_LIGHT["panel"])
        self.assertAlmostEqual(Quartz.CGColorGetAlpha(layer.borderColor()), 0.5, places=3)
        ink = theme.colour("ink").colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
        self.assertEqual(_hex(ink.CGColor()), tokens.PALETTE_LIGHT["ink"])

    def test_both_palettes_name_the_same_colours(self):
        self.assertEqual(set(tokens.PALETTE), set(tokens.PALETTE_LIGHT))


class WindowAppearanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        path = Path(cls.directory.name) / "config.json"
        raw = settings_store.config_to_raw(settings_store.editable_default_config())
        raw.update(host="192.0.2.20", auth_token="synthetic", appearance="dark")
        path.write_text(json.dumps(raw))
        store = settings_store.SettingsStore(path)
        logger = logging.getLogger("test-appearance")
        with mock.patch.object(kvm_bridge_app, "accessibility_granted", return_value=True), \
                mock.patch.object(kvm_bridge_app, "input_monitoring_granted", return_value=True):
            cls.window = kvm_bridge_app.ControlWindow.alloc().initWithController_settingsStore_logger_(
                WakingController(store.load(), logger=logger), store, logger
            )

    @classmethod
    def tearDownClass(cls):
        cls.window.appearance_watch.stop()
        theme.set_dark(True)
        cls.directory.cleanup()

    def setUp(self):
        self.window._apply_appearance("dark")

    def _ground(self):
        return _hex(self.window.window.contentView().layer().backgroundColor())

    def _appearance(self):
        return self.window.window.appearance().name()

    def test_choosing_light_repaints_the_window_and_its_title_bar(self):
        self.assertEqual(self._ground(), tokens.PALETTE["ground"])
        self.window._apply_appearance("light")
        self.assertFalse(theme.is_dark())
        self.assertEqual(self._ground(), tokens.PALETTE_LIGHT["ground"])
        self.assertEqual(self._appearance(), AppKit.NSAppearanceNameAqua)
        self.window._apply_appearance("dark")
        self.assertEqual(self._ground(), tokens.PALETTE["ground"])
        self.assertEqual(self._appearance(), AppKit.NSAppearanceNameDarkAqua)

    def test_system_follows_the_mac_and_a_fixed_choice_does_not(self):
        self.window.appearance_select.value = "system"
        with mock.patch.object(theme, "system_dark", return_value=False):
            self.window.appearance_watch.callback()
        self.assertFalse(theme.is_dark())
        self.window.appearance_select.value = "dark"
        self.window._apply_appearance()
        with mock.patch.object(theme, "system_dark", return_value=False):
            self.window.appearance_watch.callback()
        self.assertTrue(theme.is_dark())

    def test_a_switch_on_screen_cross_fades(self):
        with mock.patch.object(motion, "live", return_value=True):
            self.window._apply_appearance("light")
        # A CATransition is filed under "transition" whatever key it was added with.
        self.assertIsNotNone(self.window.window.contentView().layer().animationForKey_("transition"))

    def test_the_choice_is_saved(self):
        self.window.appearance_select.value = "light"
        self.window._apply_settings()
        self.assertEqual(self.window.settings_store.load().appearance, "light")

    def test_turning_animations_off_hides_the_style_modules_without_a_visible_window(self):
        window = self.window
        window._select_page("design")
        scroll = window.pages["design"]
        clip = scroll.contentView()
        window.glow_box.value = True
        window._reflect()
        window.window.contentView().layoutSubtreeIfNeeded()
        clip.scrollToPoint_((0, scroll.documentView().frame().size.height))
        scroll.reflectScrolledClipView_(clip)
        with mock.patch.object(motion, "live", return_value=False):
            window.glow_box.value = False
            window._reflect()
        self.assertTrue(window.style_module.view.isHidden())
        self.assertTrue(window.colour_module.view.isHidden())
        self.assertEqual(scroll.documentView().floor.constant(), 0.0)
        window.glow_box.value = True
        window._reflect()


if __name__ == "__main__":
    unittest.main()
