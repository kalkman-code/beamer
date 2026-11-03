import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class FlatpakLibeiPathTests(unittest.TestCase):
    def test_libei_installs_in_flatpak_runtime_library_path(self):
        manifest = json.loads((ROOT / "uk.co.kalkman.beamer.json").read_text(encoding="utf-8"))
        libei = next(module for module in manifest["modules"] if module["name"] == "libei")
        self.assertIn("--libdir=lib", libei["config-opts"],
                      "Flatpak loads /app/lib; Meson's host default can select /app/lib64")


if __name__ == "__main__":
    unittest.main()
