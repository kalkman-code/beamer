"""US International dead keys, which Windows reports as a plain apostrophe and quotation mark."""

import unittest

from core.capture_types import TextInput, compose_dead_key


class UsInternationalAccentTests(unittest.TestCase):
    def test_apostrophe_types_acute_vowels(self):
        for letter, accented in (("a", "á"), ("e", "é"), ("i", "í"), ("o", "ó"), ("u", "ú"),
                                 ("y", "ý"), ("E", "É"), ("A", "Á")):
            self.assertEqual(compose_dead_key("'", letter), accented)

    def test_quotation_mark_types_diaeresis(self):
        for letter, accented in (("u", "ü"), ("o", "ö"), ("U", "Ü"), ("y", "ÿ")):
            self.assertEqual(compose_dead_key('"', letter), accented)

    def test_apostrophe_then_c_is_cedilla(self):
        self.assertEqual(compose_dead_key("'", "c"), "ç")
        self.assertEqual(compose_dead_key("'", "C"), "Ç")

    def test_apostrophe_before_a_letter_without_an_accent_stays_both_characters(self):
        for dead, letter in (("'", "s"), ("'", "t"), ('"', "n"), ('"', "1")):
            typed = compose_dead_key(dead, letter)
            self.assertIsInstance(typed, TextInput)
            self.assertEqual(typed.text, dead + letter)

    def test_other_dead_keys_are_unchanged(self):
        self.assertEqual(compose_dead_key("~", "a"), "ã")
        self.assertEqual(compose_dead_key("~", "n"), "ñ")
        self.assertEqual(compose_dead_key("^", "o"), "ô")
        self.assertEqual(compose_dead_key("`", "a"), "à")
        self.assertEqual(compose_dead_key("´", "e"), "é")
        self.assertEqual(compose_dead_key("¨", "u"), "ü")


if __name__ == "__main__":
    unittest.main()
