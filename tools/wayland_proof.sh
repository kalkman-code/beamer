#!/usr/bin/env bash
# The Wayland back ends against a real GNOME Shell and its portals (tools/wayland_proof.py says what
# is proved). Starts a headless GNOME Shell on a D-Bus session of its own with one virtual monitor,
# so it never touches the desktop it is started from, then the proof, which opens the observer app.
# Needs GNOME Shell 45 or later, xdg-desktop-portal-gnome, libei, wl-clipboard, PyGObject with
# GTK 4 for the system python3, and win_app/requirements-linux.txt for PYTHON's.
# Exit status: 0 when every check passes.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
python="${PYTHON:-python3}"
if [ -z "${BEAMER_PROOF_DIR:-}" ]; then
    for tool in gnome-shell dbus-run-session wl-paste; do
        command -v "$tool" >/dev/null || { echo "$tool is not installed" >&2; exit 2; }
    done
    BEAMER_PROOF_DIR="$(mktemp -d)"
    export BEAMER_PROOF_DIR
    trap 'rm -rf "$BEAMER_PROOF_DIR"' EXIT
    dbus-run-session -- "$0" "$@"
    exit $?
fi
display="beamer-proof-$$"
export XDG_CURRENT_DESKTOP=GNOME XDG_SESSION_TYPE=wayland XDG_SESSION_DESKTOP=gnome WAYLAND_DISPLAY="$display"
unset DISPLAY
gnome-shell --headless --wayland --no-x11 --wayland-display "$display" --virtual-monitor 1280x800 \
    >"$BEAMER_PROOF_DIR/shell.log" 2>&1 &
shell=$!
trap 'kill "$shell" 2>/dev/null || true' EXIT
for _ in $(seq 100); do
    [ -S "$XDG_RUNTIME_DIR/$display" ] && break
    sleep 0.1
done
dbus-update-activation-environment WAYLAND_DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_TYPE
touch "$BEAMER_PROOF_DIR/commands"
# Beamer's remembered permissions start empty: a fresh state directory.
export XDG_STATE_HOME="$BEAMER_PROOF_DIR/state"
sleep 2
"$python" "$here/wayland_proof.py"
