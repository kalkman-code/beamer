#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
for tool in flatpak flatpak-builder; do
    command -v "$tool" >/dev/null || { echo "$tool is required" >&2; exit 2; }
done
arch="${1:-$(flatpak --default-arch)}"
case "$arch" in
    x86_64|aarch64) ;;
    *) echo "Supported architectures: x86_64, aarch64" >&2; exit 2 ;;
esac
if [ "$arch" != "$(flatpak --default-arch)" ]; then
    echo "Run this native build on a $arch machine; this script does not cross-compile." >&2
    exit 2
fi
flatpak --version
flatpak-builder --version
if [ "$(flatpak --version)" = "Flatpak 1.18.2" ]; then
    echo "Flatpak 1.18.2 cannot copy a BaseApp on Fedora's SELinux filesystem (upstream #6818). Use 1.18.3 or later." >&2
    exit 2
fi
version="$(tr -d '\r\n' < VERSION)"
output="$root/dist/flatpak/$arch"
mkdir -p "$output"
flatpak-builder --user --arch="$arch" --install-deps-from=flathub --assumeyes \
    --disable-rofiles-fuse --force-clean --repo="$output/repo" "$output/build" "$root/uk.co.kalkman.beamer.json"
bundle="$output/Beamer-$version-$arch.flatpak"
flatpak build-bundle --arch="$arch" --runtime-repo=https://flathub.org/repo/flathub.flatpakrepo \
    "$output/repo" "$bundle" uk.co.kalkman.beamer master
printf 'Bundle: %s\nBytes: ' "$bundle"
wc -c < "$bundle"
