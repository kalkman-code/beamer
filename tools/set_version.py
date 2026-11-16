"""Set the release version consumed by the desktop packaging scripts."""

import argparse
from pathlib import Path
import re


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="Stable release version: MAJOR.MINOR.PATCH")
    args = parser.parse_args()
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", args.version):
        parser.error("version must be MAJOR.MINOR.PATCH without leading zeroes")
    path = Path(__file__).resolve().parents[1] / "VERSION"
    previous = path.read_text().strip()
    path.write_text(args.version + "\n")
    print(f"VERSION: {previous} -> {args.version}")


if __name__ == "__main__":
    main()
