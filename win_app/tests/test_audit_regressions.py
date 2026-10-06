"""Page regressions use the audit's raster ink, containment and overlap measurements."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PageAuditRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        output = ROOT / "renders" / "regressions"
        output.mkdir(parents=True, exist_ok=True)
        cls.directory = tempfile.TemporaryDirectory(dir=output)
        cls.addClassCleanup(cls.directory.cleanup)
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR="1.0",
                           QT_QPA_FONTDIR=str(ROOT / "win_app" / "assets"))
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/ui_audit/qt_audit.py"), "--platform", "windows",
             "--output", cls.directory.name, "--states", "none,one-long,three-long,five-shared-edge",
             "--sizes", "640x600,1000x800,1600x1000"],
            env=environment, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        cls.records = json.loads((Path(cls.directory.name) / "measurements-1.0.json").read_text())

    def reject(self, pages, predicate):
        found = [(record["path"], flag) for record in self.records if record["page"] in pages
                 for flag in record["flags"] if predicate(flag)]
        if found:
            self.fail(f"{len(found)} audit faults; first three: {found[:3]}")

    def test_direction_tracks_do_not_overlap_or_leave_their_container(self):
        self.reject({"directions"}, lambda f: f["kind"] == "sibling-overlap" and
                    f.get("first") == "This PC drives it" or
                    f["kind"] == "outside-parent" and f.get("widget") == "Switch")

    def test_remove_confirmation_ink_and_buttons_are_contained(self):
        self.reject({"remove-confirmation"}, lambda f:
                    f["kind"] == "label-ink" and f.get("text", "").startswith("Remove ") or
                    f["kind"] == "outside-parent" and f.get("widget") in {"QPushButton", "ElidingButton"})

    def test_arrangement_diagram_fits_its_allocation(self):
        self.reject({"crossing"}, lambda f: f["kind"] == "outside-parent" and
                    f.get("widget") == "ArrangementDiagram")

    def test_fixed_minimum_controls_fit_keyboard_and_crossing_rows(self):
        self.reject({"keyboard", "crossing"}, lambda f: f["kind"] == "outside-parent" and
                    f.get("widget") in {"QPushButton", "ElidingButton"})

    def test_wrapped_help_ink_is_not_cut(self):
        starts = ("This screen decides", "Changing a side updates", "On a Mac, Ctrl arrives",
                  "At the ", "Beamer cannot", "On Wayland")
        self.reject({"overview", "keyboard", "crossing", "design"}, lambda f:
                    f["kind"] == "label-ink" and f.get("text", "").startswith(starts))


if __name__ == "__main__":
    unittest.main()
