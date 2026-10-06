# Build Beamer's Linux Flatpak

Linux support in Beamer 1.5.0 is experimental. The Flatpak has been built and installed on an aarch64 Fedora 44 machine, and its Wayland back ends pass an automated test in a headless GNOME session. The normal window and tray, a physical mouse and keyboard, and pairing with a Mac or Windows PC have not yet been tested on a real Linux desktop, and neither has an x86_64 build. Please tell us how it goes.

The app ID is `uk.co.kalkman.beamer`, matching the existing app's bundle identifier. The manifest uses PySide BaseApp 6.11 (PySide 6.11.2) and KDE Platform/SDK 6.11. Python modules and libei 1.6.0 are pinned, downloaded with SHA-256 verification and built offline. The KDE runtime already provides libxcb and libxkbcommon, including its X11 library.

## Fedora

From this repository's root on Fedora:

```bash
sudo dnf install -y flatpak flatpak-builder
flatpak remote-add --user --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
bash tools/build_flatpak.sh x86_64
flatpak install --user -y "dist/flatpak/x86_64/Beamer-$(cat VERSION)-x86_64.flatpak"
flatpak run --env=XDG_SESSION_TYPE=wayland --command=python3 uk.co.kalkman.beamer /app/share/beamer/tools/flatpak_import_probe.py
flatpak run --env=XDG_SESSION_TYPE=x11 --command=python3 uk.co.kalkman.beamer /app/share/beamer/tools/flatpak_import_probe.py
```

The script installs the SDK, runtime and BaseApp from Flathub automatically. No host Python, pip, compiler, development libraries or pip generator is needed. Omit the architecture argument to build for the current machine. The output, build repository and builder cache are gitignored. The bundle is the app, not the KDE runtime; installing it needs Flathub and downloads the runtime if absent. Rebuilding uses the checked-in `VERSION` without changing Mac or Windows build numbers.

Flatpak **1.18.2 has a BaseApp/SELinux build regression**, [upstream issue #6818](https://github.com/flatpak/flatpak/issues/6818), fixed in [1.18.3](https://github.com/flatpak/flatpak/releases/tag/1.18.3). The script refuses 1.18.2 rather than changing SELinux. If Fedora still supplies 1.18.2, update it first. On 03-10-2026, Fedora 44's updates-testing repository supplies released Flatpak 1.18.4:

```bash
sudo dnf --enablerepo=updates-testing upgrade -y flatpak flatpak-selinux
bash tools/build_flatpak.sh x86_64
```

The build disables rofiles-fuse, so no host FUSE setup is needed; Flatpak's build and application sandboxes remain enabled.

## Run on the real desktop

```bash
flatpak run uk.co.kalkman.beamer
```

The manifest grants Wayland, fallback X11, network, shared IPC and graphics rendering. RemoteDesktop, InputCapture and Clipboard use `org.freedesktop.portal.Desktop`, which [Flatpak already permits on its filtered D-Bus](https://docs.flatpak.org/en/latest/sandbox-permissions.html#d-bus-access). Qt's tray additionally needs `org.kde.StatusNotifierWatcher`, as its [upstream implementation](https://github.com/qt/qtbase/blob/6.11/src/gui/platform/unix/dbusmenu/qdbusmenuconnection.cpp) shows. The inherited KDE config-file and unrelated bus grants are removed. It grants no host filesystem, input devices, unrestricted device access or unrestricted session bus.

Start at sign-in is currently disabled: this Python installation is not a frozen executable, and the existing XDG autostart backend returns no installed executable. A future Flatpak implementation needs the [Background portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Background.html); granting host config access would not fix it.

## Installed Wayland proof

The proof additionally needs GNOME Shell, xdg-desktop-portal-gnome, wl-clipboard, D-Bus tools and the system Python's GTK 4/PyGObject. Those are test tools, not build or app dependencies. It creates its own headless GNOME session and leaves the real desktop alone.

```bash
bash tools/flatpak_wayland_proof.sh
```

This wraps the existing `tools/wayland_proof.sh`. Its proof Python, Beamer modules, PySide, PyNaCl, jeepney and libei run inside the installed Flatpak. The compositor, portal services, GTK observer and wl-copy/wl-paste are external peers on the guest. Temporary per-run permissions let the harness drive Mutter, spawn those peers and share their observation files; they do not change the installed manifest or persist overrides. This is a backend integration proof, not a test of the normal Beamer window or pairing flow.

For the source comparison, using a Linux venv with `win_app/requirements-linux.txt`:

```bash
PYTHON="$HOME/venv/bin/python" bash tools/wayland_proof.sh
```
