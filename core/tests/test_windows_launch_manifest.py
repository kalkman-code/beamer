"""Packaging must allow an ordinary Explorer launch without requesting elevation."""

import runpy
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call, patch


class WindowsLaunchManifestTests(unittest.TestCase):
    def test_main_executable_does_not_request_administrator_or_uiaccess(self):
        source = Path(__file__).resolve().parents[2] / "win_app"
        options = {}

        def executable(*args, **kwargs):
            options.update(kwargs)

        # Resource constructors belong to the Windows build toolchain, not this pure test.
        modules = {name: ModuleType(name) for name in (
            "PyInstaller", "PyInstaller.utils", "PyInstaller.utils.win32",
            "PyInstaller.utils.win32.versioninfo",
        )}
        versioninfo = modules["PyInstaller.utils.win32.versioninfo"]
        for name in ("FixedFileInfo", "StringFileInfo", "StringStruct", "StringTable",
                     "VarFileInfo", "VarStruct", "VSVersionInfo"):
            setattr(versioninfo, name, Mock(name=name))

        with patch.dict("sys.modules", modules):
            runpy.run_path(str(source / "build.spec"), init_globals={
                "SPECPATH": str(source),
                "Analysis": lambda *args, **kwargs: SimpleNamespace(
                    binaries=[], pure=[], scripts=[], datas=[]),
                "PYZ": lambda value: value,
                "EXE": executable,
            })
        self.assertFalse(options.get("uac_admin", False))
        self.assertFalse(options.get("uac_uiaccess", False))
        self.assertIs(options["version"], versioninfo.VSVersionInfo.return_value)
        version = (source.parent / "VERSION").read_text(encoding="utf-8").strip()
        versioninfo.StringStruct.assert_has_calls([
            call("FileVersion", version), call("ProductVersion", version),
        ], any_order=True)
        numeric_version = tuple(int(part) for part in version.split("-", 1)[0].split("."))
        numeric_version += (0,) * (4 - len(numeric_version))
        fixed_info = versioninfo.FixedFileInfo.call_args.kwargs
        self.assertEqual(fixed_info["filevers"], numeric_version)
        self.assertEqual(fixed_info["prodvers"], numeric_version)


if __name__ == "__main__":
    unittest.main()
