"""The bundle exporter must never receive an app that cannot import on its runtime."""
import os
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]


class FlatpakBuildTests(unittest.TestCase):
    def test_packaged_appstream_version_matches_version_file_and_preserves_history(self):
        manifest = json.loads((ROOT / "uk.co.kalkman.beamer.json").read_text())
        command = next(command for module in manifest["modules"] for command in module.get("build-commands", [])
                       if command.startswith("sed -e "))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "flatpak").mkdir()
            template = ROOT / "flatpak/uk.co.kalkman.beamer.metainfo.xml"
            shutil.copy2(template, root / "flatpak")
            (root / "VERSION").write_text("1.5.21\n")
            subprocess.run(["bash", "-c", command], cwd=root, check=True)
            releases = ET.parse(root / "metainfo.xml").getroot().findall("releases/release")
            self.assertEqual(releases[0].get("version"), "1.5.21")
            historical = [release for release in ET.parse(template).getroot().findall("releases/release")
                          if release.get("version") != "@VERSION@"]
            self.assertEqual([ET.tostring(r) for r in releases[1:]], [ET.tostring(r) for r in historical])

    def run_build(self, import_status):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools").mkdir()
            shutil.copy2(ROOT / "tools/build_flatpak.sh", root / "tools")
            (root / "VERSION").write_text("1.5.20\n")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            # Simulate the native commands at the process boundary. A private ABI
            # mismatch is an import failure after a successful build/export step.
            for tool in ("flatpak", "flatpak-builder"):
                path = bin_dir / tool
                path.write_text("""#!/bin/sh
printf '%s %s\\n' "${0##*/}" "$*" >> "$CALLS"
case "$1" in
    --default-arch) echo x86_64 ;;
    --version) echo 'Flatpak 1.18.4' ;;
    build) exit "$IMPORT_STATUS" ;;
    build-bundle) touch "$5" ;;
esac
""")
                path.chmod(0o755)
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
                       CALLS=str(root / "calls"), IMPORT_STATUS=str(import_status))
            result = subprocess.run(["bash", str(root / "tools/build_flatpak.sh"), "x86_64"],
                                    env=env, capture_output=True, text=True, timeout=30)
            return result, (root / "calls").read_text().splitlines()

    def test_private_qt_abi_failure_blocks_bundle_export(self):
        result, calls = self.run_build(1)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(line.startswith("flatpak build ") for line in calls), calls)
        self.assertFalse(any(line.startswith("flatpak build-bundle ") for line in calls), calls)

    def test_matching_runtime_imports_both_backends_before_export(self):
        result, calls = self.run_build(0)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        probes = [line for line in calls if line.startswith("flatpak build ")]
        self.assertEqual(len(probes), 2, calls)
        for session, probe in zip(("wayland", "x11"), probes):
            self.assertIn("--runtime", probe)
            self.assertIn(f"--env=XDG_SESSION_TYPE={session}", probe)
            self.assertIn("flatpak_import_probe.py", probe)
        export = next(i for i, line in enumerate(calls) if line.startswith("flatpak build-bundle "))
        self.assertTrue(all(calls.index(probe) < export for probe in probes))


if __name__ == "__main__":
    unittest.main()
