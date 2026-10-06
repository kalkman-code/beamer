"""Pack completed measurement manifests and their renders for cross-machine review."""
import argparse
import json
from pathlib import Path
import sys
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--platforms", required=True)
    parser.add_argument("--scales", default="1.0,1.25,1.5,2.0")
    args = parser.parse_args()
    destination = sys.stdout.buffer if args.archive == Path("-") else args.archive
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        for platform in args.platforms.split(","):
            folder = args.root / platform
            for scale in args.scales.split(","):
                manifest = folder / f"measurements-{scale}.json"
                records = json.loads(manifest.read_text(encoding="utf-8"))
                paths = {folder / Path(row["path"]).name for row in records}
                paths.add(manifest)
                paths.update(folder.glob(f"*-{scale}.json"))
                for path in sorted(paths):
                    archive.write(path, arcname=path.relative_to(args.root).as_posix())
    if args.archive == Path("-"):
        print(json.dumps({"archive": "stdout"}), file=sys.stderr)
    else:
        print(json.dumps({"archive": str(args.archive), "bytes": args.archive.stat().st_size}))


if __name__ == "__main__":
    main()
