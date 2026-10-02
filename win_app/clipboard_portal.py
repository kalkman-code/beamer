"""The clipboard on Wayland, through the portal Clipboard on inject_portal's RemoteDesktop session:
the same four functions as clipboard_x11.py.

A Wayland desktop gives the clipboard only to the focused app, so a background app reaches it
through the portal, which GNOME ties to a remote control session: the one the injector opens,
which the desktop asks about once. Without that session (refused, or a desktop with no portal)
the clipboard reads as empty and nothing is written, and the app says why (inject_portal).

Another app's copy is read from an fd the portal hands over, on the caller's thread, so a slow
app costs one clipboard sync and never the injector. Beamer's own copy is offered as text and PNG
and written to each app that pastes it (inject_portal's transfer). The portal says whether the
clipboard is Beamer's own, so its echo is never sent back."""

import logging
import os
import select
import time
from typing import Optional, Tuple

import inject_portal

LOGGER = logging.getLogger(__name__)

TEXT_MIMES = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING", "TEXT")
PNG_MIME = "image/png"
TIMEOUT_SECONDS = 2.0
MAX_BYTES = 64 * 1024 * 1024

_synced: Optional[int] = None


def _read_fd(fd: int, timeout: float = TIMEOUT_SECONDS) -> Optional[bytes]:
    """All an app writes into `fd` until it closes it, within `timeout`; None if it did not finish."""
    chunks = []
    size = 0
    deadline = time.monotonic() + timeout
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                LOGGER.warning("The app holding the clipboard did not hand it over in time")
                return None
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > MAX_BYTES:
                LOGGER.warning("The clipboard is larger than Beamer sends; leaving it")
                return None
            chunks.append(chunk)
    finally:
        os.close(fd)


def _read(mime: str) -> Optional[bytes]:
    fd = inject_portal.remote().ask(lambda session: session.open_selection(mime), discard=os.close)
    if fd is None:
        return None
    return _read_fd(fd)


def _offered() -> Tuple[list, bool, int]:
    return inject_portal.remote().ask(lambda session: (list(session.offered), session.ours, session.changes),
                                      default=([], False, 0))


def _contents(offered: list) -> Tuple[Optional[str], Optional[bytes]]:
    text = png = None
    mime = next((m for m in TEXT_MIMES if m in offered), None)
    if mime is not None:
        data = _read(mime)
        if data:
            text = data.decode("utf-8", "replace").replace("\r\n", "\n")
    if PNG_MIME in offered:
        png = _read(PNG_MIME) or None
    return text or None, png


def get_contents() -> Tuple[Optional[str], Optional[bytes]]:
    """(text, png) another app has on the clipboard, either None when absent; both None when it
    is Beamer's own, or the clipboard cannot be reached. Never raises."""
    try:
        offered, ours, _changes = _offered()
        if ours:
            return None, None
        return _contents(offered)
    except Exception:
        LOGGER.exception("Could not read the clipboard")
        return None, None


def changed_contents() -> Tuple[Optional[str], Optional[bytes]]:
    """get_contents(), or (None, None) when no other app has copied since the clipboard was last
    sent or written. Either way it counts as sent from here on."""
    global _synced
    try:
        offered, ours, changes = _offered()
        if ours or changes == _synced:
            return None, None
        text, png = _contents(offered)
        if text is not None or png is not None:
            _synced = changes
        return text, png
    except Exception:
        LOGGER.exception("Could not read the clipboard")
        return None, None


def change_stamp() -> Optional[int]:
    """A number that moves whenever the clipboard changes, Beamer's own writes included, or None
    while the session is not up. Read without waiting: the sender asks under its lock."""
    session = inject_portal._remote
    if session is None or not session.ready.is_set():
        return None
    return session.stamp


def forget_sync() -> None:
    global _synced
    _synced = None


def set_contents(text: Optional[str], png: Optional[bytes]) -> bool:
    """Offer `text` and/or `png` as this machine's clipboard. False when there is nothing to set or
    the clipboard cannot be reached. Never raises."""
    global _synced
    offer = {}
    if text is not None:
        data = text.replace("\r\n", "\n").encode("utf-8")
        offer.update({"text/plain;charset=utf-8": data, "text/plain": data, "UTF8_STRING": data})
    if png is not None:
        offer[PNG_MIME] = png
    if not offer:
        return False
    try:
        written = bool(inject_portal.remote().ask(lambda session: session.set_selection(offer), default=False))
    except Exception:
        LOGGER.exception("Could not set the clipboard")
        return False
    if written:
        changes = inject_portal.remote().ask(lambda session: session.changes, default=None)
        _synced = changes
    return written
