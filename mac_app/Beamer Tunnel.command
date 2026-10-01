#!/bin/zsh
# Forwards Beamer's port over SSH for a macOS 27 build that blocks the app's direct LAN access.
# The PC's address comes from Beamer's own settings; the account on the PC is asked for, because
# nothing on this Mac knows it. Requires an SSH server on the PC and a key or password for it.

set -u

CONFIG="$HOME/Library/Application Support/Beamer/settings.json"
[ -r "$CONFIG" ] || CONFIG="$HOME/Library/Application Support/Beamer/config.json"
HOST=""
if [ -r "$CONFIG" ]; then
    HOST="$(sed -n 's/.*"host": *"\([^"]*\)".*/\1/p' "$CONFIG" | head -n 1)"
fi

echo "Beamer macOS 27 fallback."
if [ -n "$HOST" ]; then
    read -r "TARGET?Windows account and address for SSH [user@$HOST]: "
    case "$TARGET" in
        "") echo "An account name is needed."; read -r "?Press Return to close"; exit 1 ;;
        *@*) ;;
        *) TARGET="$TARGET@$HOST" ;;
    esac
else
    read -r "TARGET?Windows account and address for SSH (user@address): "
    case "$TARGET" in
        *@*) ;;
        *) echo "Give it as user@address."; read -r "?Press Return to close"; exit 1 ;;
    esac
fi

echo "Tunnel to $TARGET is running. Keep this window open."
/usr/bin/ssh -N -T -o ExitOnForwardFailure=yes -o ConnectTimeout=5 -o ServerAliveInterval=15 -o ServerAliveCountMax=2 -L 127.0.0.1:24822:127.0.0.1:24820 "$TARGET" || {
    echo "Beamer could not start the tunnel to Windows."
    read -r "?Press Return to close"
}
