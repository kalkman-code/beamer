"""Run isolated offscreen processes; scaling must be fixed before QApplication exists."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--platforms", required=True, help="windows,linux-x11,linux-wayland on the rig; Linux modes on Linux")
    parser.add_argument("--scales", default="1.0,1.25,1.5,2.0")
    parser.add_argument("--actions", action="store_true")
    parser.add_argument("--states")
    parser.add_argument("--sizes")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    for platform in args.platforms.split(","):
        for scale in args.scales.split(","):
            environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR=scale)
            if platform.startswith("linux-"):
                environment["XDG_SESSION_TYPE"] = platform.removeprefix("linux-")
            command = [sys.executable, str(Path(__file__).with_name("qt_audit.py")),
                       "--platform", platform, "--output", str(args.output / platform)]
            if args.actions:
                command.append("--actions")
            for name in ("states", "sizes"):
                value = getattr(args, name)
                if value:
                    command.extend(["--"+name, value])
            log = args.output / f"{platform}-{scale}.log"
            with log.open("w", encoding="utf-8") as stream:
                run = subprocess.run(command, env=environment, stdout=stream, stderr=subprocess.STDOUT)
            row = dict(platform=platform, scale=scale, exit_code=run.returncode, log=str(log))
            results.append(row)
            print(json.dumps(row), flush=True)
            if run.returncode:
                raise SystemExit(run.returncode)
    manifest = "matrix-" + "-".join(args.platforms.split(",")) + ".json"
    (args.output / manifest).write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
