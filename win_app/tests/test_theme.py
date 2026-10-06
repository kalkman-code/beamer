import unittest

try:
    import theme
    import tokens
except ImportError:  # PySide6 is only in the Windows build venv
    theme = None


@unittest.skipIf(theme is None, "PySide6 is not installed")
class SetDarkTests(unittest.TestCase):
    def tearDown(self):
        theme.set_dark(True)

    def test_swaps_the_palette_and_back(self):
        theme.set_dark(True)
        self.assertEqual(theme.P, tokens.PALETTE)
        self.assertTrue(theme.is_dark())
        theme.set_dark(False)
        self.assertEqual(theme.P, tokens.PALETTE_LIGHT)
        self.assertFalse(theme.is_dark())
        theme.set_dark(True)
        self.assertEqual(theme.P, tokens.PALETTE)
        self.assertTrue(theme.is_dark())

    def test_stylesheet_carries_the_light_palette_when_light(self):
        theme.set_dark(False)
        self.assertIn(tokens.PALETTE_LIGHT["ground"], theme.stylesheet())
        theme.set_dark(True)
        self.assertIn(tokens.PALETTE["ground"], theme.stylesheet())


@unittest.skipIf(theme is None, "PySide6 is not installed")
class WantsDarkTests(unittest.TestCase):
    def test_dark_and_light_are_absolute(self):
        self.assertTrue(theme.wants_dark("dark", False))
        self.assertFalse(theme.wants_dark("light", True))

    def test_system_and_anything_unrecognised_follow_the_system(self):
        self.assertTrue(theme.wants_dark("system", True))
        self.assertFalse(theme.wants_dark("system", False))
        self.assertTrue(theme.wants_dark("sepia", True))
        self.assertFalse(theme.wants_dark("sepia", False))


@unittest.skipIf(theme is None, "PySide6 is not installed")
class StylesheetApplicationTests(unittest.TestCase):
    def test_identical_application_stylesheet_is_not_repolished(self):
        class Application:
            def __init__(self):
                self.value = ""
                self.applied = []

            def styleSheet(self):
                return self.value

            def setStyleSheet(self, value):
                self.value = value
                self.applied.append(value)

        app = Application()
        self.assertTrue(theme.apply_stylesheet(app))
        self.assertFalse(theme.apply_stylesheet(app))
        self.assertEqual(app.applied, [theme.stylesheet()])


if __name__ == "__main__":
    unittest.main()
