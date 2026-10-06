#!/usr/bin/env bash
# Only the test harness can reach Mutter and launch the host's GTK observer. The app's manifest
# keeps both names filtered. Beamer's back ends and Python dependencies run from the installed app.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
export BEAMER_FLATPAK_PROOF_TOOLS="$here"
PYTHON="$here/flatpak-proof/python" bash "$here/wayland_proof.sh"
