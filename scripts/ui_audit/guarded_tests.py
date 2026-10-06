"""Run unittest commands while forbidding access to the installed Beamer settings tree."""

import os
from pathlib import Path
import runpy
import sys


def protect_settings():
    roots = [Path.home() / "Library/Application Support/Beamer"]
    roots.extend(Path(os.environ[key]) / "Beamer" for key in ("LOCALAPPDATA", "APPDATA") if key in os.environ)
    roots = [root.resolve() for root in roots]

    def guard(event, values):
        if event != "open" or not values or not isinstance(values[0], (str, bytes, os.PathLike)):
            return
        path = Path(os.fsdecode(values[0])).resolve()
        if any(path == root or root in path.parents for root in roots):
            raise RuntimeError("Installed Beamer settings access is forbidden during verification")

    sys.addaudithook(guard)


if __name__ == "__main__":
    protect_settings()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    sys.path.insert(0, str(Path.cwd()))
    sys.argv[0] = "unittest"
    runpy.run_module("unittest", run_name="__main__")
