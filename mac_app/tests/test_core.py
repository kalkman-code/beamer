"""core/'s own tests, run as part of this app's suite so one command still covers everything this
app ships, under this app's interpreter and packages."""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_tests(loader, tests, pattern):
    tests.addTests(loader.discover(os.path.join(_REPO_ROOT, "core", "tests"), pattern="test*.py", top_level_dir=_REPO_ROOT))
    # discover leaves the root first on sys.path. It goes behind the app's own folder instead: the
    # root also holds the Mac's theme.py, which must not stand in for win_app's.
    while _REPO_ROOT in sys.path:
        sys.path.remove(_REPO_ROOT)
    sys.path.append(_REPO_ROOT)
    return tests
