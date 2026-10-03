"""Run the effects checks with synthetic settings confined to this checkout.

Mac: touched modules only. Windows: the complete Windows suite and the new core contracts.
"""

import argparse
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--touched", action="store_true", help="Rerun affected Windows modules after the full suite")
args = parser.parse_args()
scratch = ROOT / ".test-tmp"
scratch.mkdir(exist_ok=True)
tempfile.tempdir = str(scratch)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
if sys.platform == "win32":
    os.environ["LOCALAPPDATA"] = str(scratch / "local")


def synthetic_settings_only(event, args):
    if event != "open" or not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    path = Path(os.fsdecode(args[0])).resolve()
    if path.name.lower() in ("settings.json", "config.json") and not path.is_relative_to(ROOT):
        raise PermissionError("Tests may only open synthetic settings within their worktree")


sys.addaudithook(synthetic_settings_only)
sys.path.insert(0, str(ROOT))
app = ROOT / ("mac_app" if sys.platform == "darwin" else "win_app")
sys.path.insert(0, str(app))
sys.path.insert(0, str(app / "tests"))
loader = unittest.TestLoader()
suite = unittest.TestSuite()
suite.addTests(loader.loadTestsFromNames(["core.tests.test_effect_sets", "core.tests.test_settings_sync"]))
if sys.platform == "darwin":
    suite.addTests(loader.loadTestsFromNames([
        "test_pages", "test_settings_store", "test_effect_layouts", "test_previews", "test_effects_overlay",
    ]))
elif args.touched:
    suite.addTests(loader.loadTestsFromNames([
        "test_pages_win", "test_app_config", "test_settings_migration", "test_same_on_both",
        "test_effect_layouts", "test_effect_overlay", "test_effect_fires",
    ]))
else:
    suite.addTests(loader.discover(str(app / "tests")))
result = unittest.TextTestRunner(verbosity=1).run(suite)
raise SystemExit(not result.wasSuccessful())
