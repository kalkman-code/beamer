#!/bin/bash
# Builds Beamer.app (arm64 and x86_64 in one bundle) and Beamer.dmg.
#
#   ./build_dmg.sh            local build in mac_app/dist, signed with the same Developer ID identity
#   ./build_dmg.sh --release  Developer ID, notarised and stapled, written to
#                             docs/beamer-releases/<version>/ beside the repository as Beamer-<version>.dmg, with nothing left in dist
#
# The version is the repo-root VERSION file and the build number is the commit count, so a release
# rebuilt from the same commit carries the same numbers. Nothing here uploads anywhere but Apple's
# notary service.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APP="$SCRIPT_DIR/dist/Beamer.app"
DMG="$SCRIPT_DIR/dist/Beamer.dmg"
RELEASE=0
case "${1:-}" in
    "") ;;
    --release) RELEASE=1 ;;
    *) echo "usage: $0 [--release]" >&2; exit 2 ;;
esac

STAGING_DIR="$(mktemp -d /private/tmp/Beamer.build.XXXXXX)"
cleanup() {
    rm -rf "$STAGING_DIR"
    # A release is copied out, so the staged app goes: Spotlight would offer it beside the installed one.
    if [ "$RELEASE" -eq 1 ]; then rm -rf "$SCRIPT_DIR/build" "$SCRIPT_DIR/dist"; fi
}
trap cleanup EXIT

cd "$SCRIPT_DIR"
if [ -d "/Applications/Xcode.app/Contents/Developer" ]; then
    export DEVELOPER_DIR="/Applications/Xcode.app/Contents/Developer"
else
    export DEVELOPER_DIR="/Library/Developer/CommandLineTools"
fi
VERSION="$(tr -d '[:space:]' < "$PROJECT_DIR/VERSION")"
export BEAMER_BUILD="$(git -C "$PROJECT_DIR" rev-list --count HEAD)"

xcrun clang --version

if [ "$RELEASE" -eq 1 ]; then
    # docs/ holds working notes, not build output, and is excluded from this check; anything else
    # uncommitted would ship code that no commit records.
    if [ -n "$(git -C "$PROJECT_DIR" status --porcelain -- . ':(exclude)docs')" ]; then
        echo "uncommitted changes outside docs/ - commit them so the release matches a commit" >&2
        exit 1
    fi
    IDENTITY="Developer ID Application: Toby Kalkman (YNL35YT683)"
    KEY_DIR="$HOME/.appstoreconnect"
    KEY_ID="$(cat "$KEY_DIR/key_id")"
    [ -f "$KEY_DIR/private_keys/AuthKey_$KEY_ID.p8" ] && [ -s "$KEY_DIR/issuer_id" ] \
        || { echo "the pinned App Store Connect key is missing from $KEY_DIR" >&2; exit 1; }
    RELEASE_DIR="$(cd "$PROJECT_DIR/.." && pwd)/docs/beamer-releases/$VERSION"
    echo "Release build of Beamer $VERSION ($BEAMER_BUILD) from $(git -C "$PROJECT_DIR" rev-parse --short HEAD)"
else
    # The release identity too: macOS keys the privacy grants to it, so a local install over a
    # release keeps Accessibility and Input Monitoring instead of silently losing them.
    IDENTITY="${BEAMER_SIGN_IDENTITY:-Developer ID Application: Toby Kalkman (YNL35YT683)}"
    echo "Local build of Beamer $VERSION ($BEAMER_BUILD); not for release distribution"
fi

if [ ! -x ".venv/bin/python3" ]; then
    python3 -m venv .venv
fi
.venv/bin/python3 -m pip install -q -r requirements-mac.txt

rm -rf "$SCRIPT_DIR/build" "$SCRIPT_DIR/dist"
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python3 -B setup.py py2app

PY2APP_DIR="$(.venv/bin/python3 -c 'from pathlib import Path; import py2app; print(Path(py2app.__file__).parent)')"
clang -O2 -arch arm64 -arch x86_64 -mmacosx-version-min=13.0 "$PY2APP_DIR/apptemplate/src/main.c" -framework Cocoa -o "$APP/Contents/MacOS/Beamer"

# cffi ships no universal2 wheel, so the extension pip installed for this Mac is single-architecture.
# The other half comes from cffi's own wheel for the other architecture, and lipo fuses the two.
# Everything else in the bundle (Python itself, PyObjC, PyNaCl) ships universal2 and is checked below.
HOST_ARCH="$(uname -m)"
[ "$HOST_ARCH" = arm64 ] && OTHER_ARCH=x86_64 || OTHER_ARCH=arm64
CFFI_VERSION="$(.venv/bin/python3 -c 'import importlib.metadata as m; print(m.version("cffi"))')"
PY_TAG="$(.venv/bin/python3 -c 'import sys; print(f"{sys.version_info[0]}{sys.version_info[1]}")')"
mkdir -p "$STAGING_DIR/cffi-other"
if [ "$OTHER_ARCH" = x86_64 ]; then
    OTHER_PLATFORMS="--platform macosx_10_13_x86_64 --platform macosx_10_15_x86_64 --platform macosx_11_0_x86_64 --platform macosx_13_0_x86_64"
else
    OTHER_PLATFORMS="--platform macosx_11_0_arm64 --platform macosx_13_0_arm64"
fi
.venv/bin/python3 -m pip download -q --no-deps --only-binary=:all: --python-version "$PY_TAG" \
    $OTHER_PLATFORMS "cffi==$CFFI_VERSION" -d "$STAGING_DIR/cffi-other"
unzip -q -o "$STAGING_DIR"/cffi-other/cffi-*.whl '_cffi_backend*.so' -d "$STAGING_DIR/cffi-other/x"
for ours in $(find "$APP/Contents" -name '_cffi_backend*.so'); do
    theirs="$(ls "$STAGING_DIR"/cffi-other/x/_cffi_backend*.so)"
    if [ "$(lipo -archs "$ours" | wc -w)" -lt 2 ]; then
        lipo -create "$ours" "$theirs" -output "$STAGING_DIR/cffi-fused.so"
        cp "$STAGING_DIR/cffi-fused.so" "$ours"
    fi
done

xattr -cr "$APP"

# PyObjC's wheels carry dSYM debug symbols, which py2app copies into the zipped stdlib. They are
# Mach-O files nothing loads, and codesign cannot reach inside a zip to sign them.
find "$APP" -name '*.dSYM' -prune -exec rm -rf {} +
for archive in "$APP"/Contents/Resources/lib/python3*.zip; do
    zip -q -d "$archive" '*.dSYM/*' || [ $? -eq 12 ]
done

# The packages py2app copies whole (rumps, cffi, nacl, setuptools) arrive with the
# __pycache__ pip compiled at install time, whose code objects name the venv's path on this Mac, and
# py2app writes the building python's path into Info.plist. A shipped build once carried both. The
# caches go, inside the zip too, where zipimport never reads a __pycache__ anyway; the loose packages
# are compiled again under a relative name, since the app never writes bytecode into its own bundle.
find "$APP" -name __pycache__ -type d -prune -exec rm -rf {} +
for archive in "$APP"/Contents/Resources/lib/python3*.zip; do
    zip -q -d "$archive" '*/__pycache__/*' || [ $? -eq 12 ]
done
for packages in "$APP"/Contents/Resources/lib/python3.*/; do
    .venv/bin/python3 -m compileall -q -d "lib/$(basename "$packages")" "$packages" >/dev/null
done
/usr/libexec/PlistBuddy -c 'Delete :PythonInfoDict:PythonExecutable' "$APP/Contents/Info.plist"
# Every Mach-O in the bundle must carry both architectures, or an Intel Mac (or an Apple silicon one
# under Rosetta) fails on the one file that does not, at first use rather than at launch.
find "$APP/Contents" -type f -print0 | xargs -0 file | grep 'Mach-O' | grep -v '(for architecture' \
    | cut -d: -f1 > "$STAGING_DIR/machos.txt"
single=0
while IFS= read -r binary; do
    archs="$(lipo -archs "$binary")"
    if [ "$(printf '%s\n' $archs | grep -cx -e x86_64 -e arm64)" -ne 2 ]; then
        echo "single-architecture Mach-O ($archs): ${binary#$APP/}" >&2
        single=$((single + 1))
    fi
done < "$STAGING_DIR/machos.txt"
[ "$single" -eq 0 ] || { echo "$single Mach-O files in the app lack arm64 or x86_64" >&2; exit 1; }
echo "$(wc -l < "$STAGING_DIR/machos.txt" | tr -d ' ') Mach-O files in the app, every one arm64 and x86_64"

# Anything still naming this Mac's home folder, in a file or inside the zipped stdlib, stops the build.
.venv/bin/python3 - "$APP" "$HOME" <<'EOF'
import pathlib, sys, zipfile
app, home = pathlib.Path(sys.argv[1]), sys.argv[2].encode()
found = []
for path in app.rglob("*"):
    if path.is_file() and not path.is_symlink():
        if home in path.read_bytes():
            found.append(str(path.relative_to(app)))
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                found += [f"{path.name}:{name}" for name in archive.namelist() if home in archive.read(name)]
if found:
    sys.exit(f"{len(found)} files in the app name {home.decode()}, first {found[:5]}")
EOF

# Every build runs under the hardened runtime, so a local build breaks the way a notarised one would.
# Only a release takes a secure timestamp: notarisation requires it, and it needs Apple's server.
if [ "$RELEASE" -eq 1 ]; then TIMESTAMP="--timestamp"; else TIMESTAMP="--timestamp=none"; fi

# Inside-out rather than --deep: every nested Mach-O (the Python framework, the extension modules,
# PyNaCl's _sodium, the helper python) is signed on its own, then the framework, then the bundle,
# whose signature seals the lot. --identifier pins the bundle id, which must never change - the
# privacy grants are keyed to it.
sign_app() {
    local identity="$1" binary
    find "$APP/Contents" -type f ! -path "$APP/Contents/MacOS/Beamer" -print0 \
        | xargs -0 file | grep 'Mach-O' | grep -v '(for architecture' | cut -d: -f1 > "$STAGING_DIR/nested.txt"
    while IFS= read -r binary; do
        local entitle=()
        [ "$binary" = "$APP/Contents/MacOS/python" ] && entitle=(--entitlements "$SCRIPT_DIR/entitlements.plist")
        codesign --force --sign "$identity" --options runtime $TIMESTAMP ${entitle[@]+"${entitle[@]}"} "$binary" || return 1
    done < "$STAGING_DIR/nested.txt"
    codesign --force --sign "$identity" --options runtime $TIMESTAMP \
        "$APP/Contents/Frameworks/Python.framework/Versions/"[0-9]* || return 1
    codesign --force --sign "$identity" --options runtime $TIMESTAMP \
        --entitlements "$SCRIPT_DIR/entitlements.plist" --identifier uk.co.kalkman.beamer "$APP" || return 1
}

# A real identity is stable across rebuilds, so macOS keeps the Accessibility and Input Monitoring
# grants; an ad-hoc signature is a new code identity every build and silently drops them.
# A local build falls back to ad-hoc because the login keychain cannot be unlocked from a session
# driven over SSH (errSecInternalComponent), and losing the whole build to that would be worse.
# A release never falls back.
# Not grep -q anywhere here: it exits at the first match, and pipefail then reads the writer's SIGPIPE
# as failure.
if ! security find-identity -v -p codesigning 2>/dev/null | grep -F "$IDENTITY" >/dev/null; then
    [ "$RELEASE" -eq 1 ] && { echo "$IDENTITY is not in the keychain" >&2; exit 1; }
    echo "$IDENTITY is not in the keychain - signing ad-hoc instead"
    TIMESTAMP="--timestamp=none"
    sign_app -
elif ! sign_app "$IDENTITY" 2>"$STAGING_DIR/sign.log"; then
    [ "$RELEASE" -eq 1 ] && { cat "$STAGING_DIR/sign.log" >&2; exit 1; }
    echo "the login keychain would not release $IDENTITY - signing ad-hoc instead"
    TIMESTAMP="--timestamp=none"
    sign_app -
fi
codesign --verify --deep --strict --verbose=2 "$APP"
codesign -dvv "$APP" 2>&1 | grep -E '^(Identifier|Signature|Authority|Timestamp|CodeDirectory)' || true

# The bundle's own python runs under the same hardened runtime as the app, so code that needs an
# entitlement fails here rather than at a user's first launch.
env -i HOME="$HOME" PATH=/usr/bin:/bin PYTHONHOME="$APP/Contents/Resources" \
    "$APP/Contents/MacOS/python" "$PROJECT_DIR/tools/runtime_probe.py"
# The same probe as an Intel Mac would run it, where Rosetta is here to run it.
if /usr/bin/arch -x86_64 /usr/bin/true 2>/dev/null; then
    env -i HOME="$HOME" PATH=/usr/bin:/bin PYTHONHOME="$APP/Contents/Resources" \
        /usr/bin/arch -x86_64 "$APP/Contents/MacOS/python" "$PROJECT_DIR/tools/runtime_probe.py"
else
    echo "Rosetta is not installed here: the x86_64 half of the bundle was not run" >&2
fi

notarise() {
    local result="$STAGING_DIR/notary.json" id status
    xcrun notarytool submit "$1" --key "$KEY_DIR/private_keys/AuthKey_$KEY_ID.p8" --key-id "$KEY_ID" \
        --issuer "$(cat "$KEY_DIR/issuer_id")" --wait --output-format json > "$result" || true
    id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("id", ""))' "$result")"
    status="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status", ""))' "$result")"
    echo "Notary service: $status for $(basename "$1") ($id)"
    if [ "$status" != "Accepted" ]; then
        [ -n "$id" ] && xcrun notarytool log "$id" --key "$KEY_DIR/private_keys/AuthKey_$KEY_ID.p8" \
            --key-id "$KEY_ID" --issuer "$(cat "$KEY_DIR/issuer_id")" >&2
        exit 1
    fi
}

# The app is notarised and stapled before it goes into the image, so the copy a user drags out
# carries its own ticket; the image then gets a submission of its own.
if [ "$RELEASE" -eq 1 ]; then
    ditto -c -k --keepParent "$APP" "$STAGING_DIR/Beamer.zip"
    notarise "$STAGING_DIR/Beamer.zip"
    xcrun stapler staple "$APP"
fi

# The window a downloader sees: Beamer, an arrow and Applications on a background that says what to
# do, laid out by dmgbuild so no Finder scripting (and no Automation prompt) is involved.
rm -f "$DMG"
.venv/bin/python3 -m dmgbuild -s "$SCRIPT_DIR/dmg/settings.py" -D app="$APP" \
    -D background="$SCRIPT_DIR/dmg/background.png" "Beamer" "$DMG"

if [ "$RELEASE" -eq 1 ]; then
    codesign --force --sign "$IDENTITY" --timestamp "$DMG"
    notarise "$DMG"
    xcrun stapler staple "$DMG"
    xcrun stapler validate "$APP"
    xcrun stapler validate "$DMG"
    spctl -a -vv "$APP"
    spctl -a -vv -t install "$DMG"
    mkdir -p "$RELEASE_DIR"
    cp "$DMG" "$RELEASE_DIR/Beamer-$VERSION.dmg"
    echo "Released Beamer $VERSION ($BEAMER_BUILD) at $RELEASE_DIR/Beamer-$VERSION.dmg"
else
    echo "Built Beamer $VERSION ($BEAMER_BUILD) at $DMG"
fi
