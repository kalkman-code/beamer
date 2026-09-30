"""What both apps share lives once, in core/. Until 1.5.0 each shared module was two byte-identical
copies, one in mac_app/ and one in win_app/, kept in step by a test; a copy creeping back into
either app would shadow nothing (the apps import `core.x`) but would drift unseen.

This fails if any app folder, or its tests, holds a file named like one of core's.
"""

import os
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_CORE = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(_CORE)
_APPS = ("mac_app", "win_app")


def _files(folder, keep):
    return {name for name in os.listdir(folder) if keep(name)}


class NoCopiesTests(unittest.TestCase):
    def test_no_app_holds_a_copy_of_a_core_module(self):
        modules = _files(_CORE, lambda name: name.endswith(".py") and name != "__init__.py")
        self.assertIn("protocol.py", modules)
        for app in _APPS:
            with self.subTest(app=app):
                self.assertEqual(modules & _files(os.path.join(_REPO_ROOT, app), lambda name: name.endswith(".py")), set())

    def test_no_app_holds_a_copy_of_a_core_test(self):
        tests = _files(_TESTS_DIR, lambda name: name != "__init__.py" and not name.startswith("__"))
        self.assertIn("pairing_vectors.json", tests)
        for app in _APPS:
            with self.subTest(app=app):
                self.assertEqual(tests & _files(os.path.join(_REPO_ROOT, app, "tests"), lambda name: True), set())


if __name__ == "__main__":
    unittest.main()
