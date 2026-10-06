import json
import sys
import unittest
from unittest import mock

from core import locale, settings_sync


class LocaleTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "NSLocale is available on macOS")
    def test_mac_region_matches_nslocale_country_code(self):
        from Foundation import NSLocale, NSLocaleCountryCode

        country = str(NSLocale.currentLocale().objectForKey_(NSLocaleCountryCode) or "").upper()
        self.assertEqual(locale.is_us_region(), country == "US")

    def test_linux_region_uses_the_first_defined_locale(self):
        with mock.patch.object(locale.sys, "platform", "linux"), \
                mock.patch.object(locale, "_linux_locale", return_value="en_GB.UTF-8"):
            self.assertFalse(locale.is_us_region())

        with mock.patch.object(locale.sys, "platform", "linux"), \
                mock.patch.object(locale, "_linux_locale", return_value="en_US.UTF-8"):
            self.assertTrue(locale.is_us_region())

    def test_linux_locale_variables_follow_posix_precedence(self):
        values = {"LC_ALL": "en_GB.UTF-8", "LC_MESSAGES": "en_US.UTF-8", "LANG": "en_US.UTF-8"}
        with mock.patch.dict(locale.os.environ, values, clear=True):
            self.assertEqual(locale._linux_locale(), "en_GB.UTF-8")
        values["LC_ALL"] = ""
        with mock.patch.dict(locale.os.environ, values, clear=True):
            self.assertEqual(locale._linux_locale(), "en_US.UTF-8")

    def test_mac_and_windows_use_the_operating_system_region(self):
        with mock.patch.object(locale.sys, "platform", "darwin"), \
                mock.patch.object(locale, "_mac_country", return_value="US"):
            self.assertTrue(locale.is_us_region())
        with mock.patch.object(locale.sys, "platform", "win32"), \
                mock.patch.object(locale, "_windows_locale", return_value="en-GB"):
            self.assertFalse(locale.is_us_region())
        with mock.patch.object(locale.sys, "platform", "win32"), \
                mock.patch.object(locale, "_windows_locale", return_value="en-US"):
            self.assertTrue(locale.is_us_region())

    def test_unknown_and_non_us_regions_keep_british_text(self):
        for platform in ("unknown", "linux"):
            with self.subTest(platform=platform), \
                    mock.patch.object(locale.sys, "platform", platform), \
                    mock.patch.object(locale, "_linux_locale", return_value="C"):
                self.assertFalse(locale.is_us_region())
                self.assertEqual(locale.americanise("Colourful behaviour at the centre"),
                                 "Colourful behaviour at the centre")

    def test_us_display_text_maps_words_and_preserves_case(self):
        british = "Colourful colours; behaviour at the centre, favourite, organise, licence"
        american = "Colorful colors; behavior at the center, favorite, organize, license"
        with mock.patch.object(locale, "is_us_region", return_value=True):
            self.assertEqual(locale.americanise(british), american)
            self.assertEqual(locale.americanise("MINIMISE, Initialising, cancelled"),
                             "MINIMIZE, Initializing, canceled")

    def test_region_does_not_change_settings_wire_bytes(self):
        settings = {"glow_colour": "colourful", "glow_style": "glow"}
        with mock.patch.object(locale, "is_us_region", return_value=False):
            british = settings_sync.message_data(True, 7, settings)
        with mock.patch.object(locale, "is_us_region", return_value=True):
            american = settings_sync.message_data(True, 7, settings)
        self.assertEqual(json.dumps(british, sort_keys=True, separators=(",", ":")),
                         json.dumps(american, sort_keys=True, separators=(",", ":")))
        self.assertEqual(settings["glow_colour"], "colourful")
