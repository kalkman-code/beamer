#!/usr/bin/env bash
# The X11 back ends against a real X server (tools/x11_proof.py says what is proved). Starts Xvfb
# on a free display, so it runs headless and never touches the desktop it is started from.
# Needs Xvfb, Python 3 and win_app/requirements-linux.txt; PYTHON picks the interpreter.
# Exit status: 0 when every check passes.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
python="${PYTHON:-python3}"
command -v Xvfb >/dev/null || { echo "Xvfb is not installed (Fedora: xorg-x11-server-Xvfb, Debian/Ubuntu: xvfb)" >&2; exit 2; }
for number in $(seq 90 120); do
    [ -e "/tmp/.X11-unix/X$number" ] || [ -e "/tmp/.X$number-lock" ] || break
done
Xvfb ":$number" -screen 0 1920x1080x24 -nolisten tcp >/dev/null 2>&1 &
server=$!
trap 'kill "$server" 2>/dev/null || true' EXIT
for _ in $(seq 50); do
    [ -e "/tmp/.X11-unix/X$number" ] && break
    sleep 0.1
done
unset WAYLAND_DISPLAY
DISPLAY=":$number" XDG_SESSION_TYPE=x11 "$python" "$here/x11_proof.py"
