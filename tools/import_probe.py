"""Imports every module of a built Beamer inside the build itself, and fails on the first that will
not load: each module of the app's own folder and of core/, by name, from the bundle and never
from this source tree.

    python3 tools/import_probe.py mac mac_app/dist/Beamer.app [--arch x86_64]
    python  tools/import_probe.py win win_app/dist/Beamer.exe

The Mac bundle carries its own python, which runs this file again inside the bundle (`--arch`
runs it under Rosetta). The Windows build is one windowed exe with no interpreter to lend, so it
runs its own `--import-probe` switch (kvm_bridge_win.py), which writes each outcome to a file.
Neither starts the app, opens a window or touches its config.
"""

import argparse
import importlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# The entry scripts run as __main__ in a build and are not importable by name there.
APPS = {
    "mac": ("mac_app", {"kvm_bridge_app", "setup"}, ["theme", "tokens"]),
    "win": ("win_app", {"kvm_bridge_win"}, []),
}


def module_names(platform: str) -> list:
    folder, entry, extra = APPS[platform]
    own = sorted(path.stem for path in (REPO / folder).glob("*.py") if path.stem not in entry)
    core = sorted(f"core.{path.stem}" for path in (REPO / "core").glob("*.py") if path.stem != "__init__")
    return ["core"] + core + own + extra


def inside(bundle: str, names: list) -> int:
    """Run by the bundle's own python: nothing may come from outside the bundle."""
    root = os.path.realpath(bundle)
    for name in names:
        try:
            module = importlib.import_module(name)
        except BaseException as exc:
            print(f"FAIL {name}: {type(exc).__name__}: {exc}", flush=True)
            raise
        where = getattr(module, "__file__", None)
        if where and not os.path.realpath(where).startswith(root):
            print(f"FAIL {name} came from {where}, outside the bundle", flush=True)
            return 1
        print(f"ok {name}", flush=True)
    return 0


def probe_mac(app: Path, names: list, arch: str) -> int:
    python = app / "Contents" / "MacOS" / "python"
    command = ([f"/usr/bin/arch", f"-{arch}"] if arch else []) + [str(python), str(Path(__file__).resolve()), "--inside", str(app), *names]
    environment = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin", "PYTHONHOME": str(app / "Contents" / "Resources")}
    with tempfile.TemporaryDirectory() as cwd:
        return subprocess.run(command, env=environment, cwd=cwd).returncode


def probe_win(exe: Path, names: list) -> int:
    with tempfile.TemporaryDirectory() as cwd:
        out = Path(cwd) / "probe.txt"
        code = subprocess.run([str(exe), "--import-probe", str(out), *names], cwd=cwd).returncode
        result = out.read_text(encoding="utf-8") if out.exists() else "FAIL the exe wrote no result\n"
    print(result, end="")
    loaded = [line.split(" ", 2) for line in result.splitlines() if line.startswith("ok ")]
    outside = [name for _, name, where in loaded if where.startswith(str(REPO))]
    if outside:
        print(f"FAIL loaded from the source tree, not the exe: {', '.join(outside)}")
    return 1 if code or outside or len(loaded) != len(names) else 0


def main() -> int:
    if sys.argv[1:2] == ["--inside"]:
        return inside(sys.argv[2], sys.argv[3:])
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("platform", choices=sorted(APPS))
    parser.add_argument("build", type=Path, help="Beamer.app on the Mac, Beamer.exe on Windows")
    parser.add_argument("--arch", default="", help="Mac only: run the bundle's python as this architecture")
    arguments = parser.parse_args()
    names = module_names(arguments.platform)
    build = arguments.build.resolve()
    code = probe_mac(build, names, arguments.arch) if arguments.platform == "mac" else probe_win(build, names)
    print(f"{'passed' if code == 0 else 'FAILED'}: {len(names)} modules probed in {build}")
    return code


if __name__ == "__main__":
    sys.exit(main())
