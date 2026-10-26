import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import i18n, locale


RUSSIAN = r'''msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\n"
"Plural-Forms: nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);\n"

#. a note for the translator
#: mac_app/pages.py:1
msgid "Open"
msgstr "Открыть"

msgctxt "switch"
msgid "Off"
msgstr "Выкл."

#, python-brace-format
msgid "{n} machine"
msgid_plural "{n} machines"
msgstr[0] "{n} компьютер"
msgstr[1] "{n} компьютера"
msgstr[2] "{n} компьютеров"

#, fuzzy
msgid "Unchecked"
msgstr "Не проверено"

msgid "Two lines"
msgstr ""
"Первая \"строка\"\n"
"вторая"

msgid "Not yet"
msgstr ""
'''


class I18nTests(unittest.TestCase):
    def test_shipped_russian_resolves_system_variants_and_has_no_missing_entries(self):
        from tools.i18n_extract import template_entries, extract, problems, header_of, plural_count
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BEAMER_LANGUAGE", None)
            for locale_code in ("ru-RU", "ru_RU.UTF-8", "ru"):
                self.assertEqual(i18n.resolve("auto", [locale_code, "en-US"]), "ru")
            self.assertEqual(i18n.resolve("ru", ["en-US"]), "ru")
            i18n.install("ru")
            self.assertEqual(i18n.tr("Settings"), "Настройки")
            self.assertEqual(i18n.tr("Connected to {name}").format(name="Colourful PC"),
                             "Colourful PC: подключено")
        entries = i18n.parse_po((i18n.locales_dir() / "ru.po").read_text(encoding="utf-8"))
        self.assertEqual(problems(entries, template_entries(extract()), plural_count(header_of(entries))), [])

    def tearDown(self):
        i18n.install("en")

    def catalogue(self, text):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        (Path(folder.name) / "ru.po").write_text(text, encoding="utf-8")
        patches = [mock.patch.object(i18n, "locales_dir", return_value=Path(folder.name)),
                   mock.patch.dict(i18n.LANGUAGES, {"ru": "Русский"})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_po_file_reads_with_plurals_contexts_and_continuations(self):
        self.catalogue(RUSSIAN)
        self.assertEqual(i18n.install("ru"), "ru")
        self.assertEqual(i18n.tr("Open"), "Открыть")
        self.assertEqual(i18n.trc("switch", "Off"), "Выкл.")
        self.assertEqual(i18n.tr("Off"), "Off")
        self.assertEqual([i18n.trn("{n} machine", "{n} machines", n).format(n=n) for n in (1, 3, 5, 21)],
                         ["1 компьютер", "3 компьютера", "5 компьютеров", "21 компьютер"])
        self.assertEqual(i18n.tr("Two lines"), 'Первая "строка"\nвторая')

    def test_fuzzy_and_empty_entries_fall_back_to_english(self):
        self.catalogue(RUSSIAN)
        i18n.install("ru")
        self.assertEqual(i18n.tr("Unchecked"), "Unchecked")
        self.assertEqual(i18n.tr("Not yet"), "Not yet")
        self.assertEqual(i18n.tr("Never seen"), "Never seen")

    def test_a_broken_catalogue_leaves_beamer_in_english(self):
        self.catalogue('msgid "Open"\nnonsense\n')
        self.assertEqual(i18n.install("ru"), "en")
        self.assertEqual(i18n.tr("Open"), "Open")

    def test_automatic_takes_the_first_preferred_language_beamer_has(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BEAMER_LANGUAGE", None)
            self.assertEqual(i18n.resolve("auto", ["fr-FR", "it-IT", "en-GB"]), "it")
            self.assertEqual(i18n.resolve("auto", ["en-US", "it"]), "en")
            self.assertEqual(i18n.resolve("auto", ["it_IT.UTF-8"]), "it")
            self.assertEqual(i18n.resolve("auto", ["de"]), "en")
            self.assertEqual(i18n.resolve("en", ["it"]), "en")
            self.assertEqual(i18n.resolve("it", ["en"]), "it")
            self.assertEqual(i18n.resolve("xx", ["it"]), "it")

    def test_the_environment_forces_a_language(self):
        with mock.patch.dict(os.environ, {"BEAMER_LANGUAGE": "it"}):
            self.assertEqual(i18n.resolve("en", ["en"]), "it")

    def test_linux_follows_gettext_order(self):
        with mock.patch.object(sys, "platform", "linux"):
            with mock.patch.dict(os.environ, {"LANGUAGE": "it:en", "LANG": "en_GB.UTF-8"}, clear=True):
                self.assertEqual(i18n.system_languages(), ["it", "en", "en_GB.UTF-8"])
            with mock.patch.dict(os.environ, {"LANGUAGE": "it", "LANG": "C.UTF-8"}, clear=True):
                self.assertEqual(i18n.system_languages(), [])

    def test_pseudo_language_brackets_and_keeps_fields(self):
        with mock.patch.dict(os.environ, {"BEAMER_LANGUAGE": i18n.PSEUDO}):
            i18n.install("auto")
        text = i18n.tr("Beam to {name}")
        self.assertTrue(text.startswith("⟦") and text.endswith("⟧"))
        self.assertEqual(text.format(name="Mac").count("Mac"), 1)

    def test_joined_lists_names(self):
        self.assertEqual(i18n.joined([]), "")
        self.assertEqual(i18n.joined(["Mac"]), "Mac")
        self.assertEqual(i18n.joined(["Mac", "PC"]), "Mac and PC")
        self.assertEqual(i18n.joined(["Mac", "PC", "Laptop"]), "Mac, PC and Laptop")

    def test_us_spelling_only_while_in_english(self):
        with mock.patch.object(locale, "is_us_region", return_value=True):
            self.assertEqual(locale.americanise("Colour"), "Color")
            self.catalogue(RUSSIAN)
            i18n.install("ru")
            self.assertEqual(locale.americanise("Colour"), "Colour")

    def test_us_templates_preserve_interpolated_data_and_field_names(self):
        with mock.patch.object(locale, "is_us_region", return_value=True):
            i18n.install("en")
            fields = {"colour": "Colourful", "address": "colour.example", "n": 2}
            self.assertEqual(i18n.tr("Favourite {colour}: {address}").format(**fields),
                             "Favorite Colourful: colour.example")
            self.assertEqual(i18n.trc("menu", "Centre {colour}").format(**fields),
                             "Center Colourful")
            self.assertEqual(i18n.trn("{n} colour for {colour}", "{n} colours for {colour}", 2).format(**fields),
                             "2 colors for Colourful")
            self.assertEqual(i18n.joined(["Colourful", "Favourite centre"]),
                             "Colourful and Favourite centre")

    def test_us_region_does_not_rewrite_italian_or_pseudo_templates(self):
        for code in ("it", "qps"):
            with self.subTest(code=code), \
                    mock.patch.dict(os.environ, {"BEAMER_LANGUAGE": code}), \
                    mock.patch.object(locale, "is_us_region", return_value=True):
                i18n.install("auto")
                expected = i18n.load(code).gettext("Favourite {name}")
                self.assertEqual(i18n.tr("Favourite {name}").format(name="Colourful"),
                                 expected.format(name="Colourful"))


if __name__ == "__main__":
    unittest.main()
