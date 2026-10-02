"""Linux X11 clipboard access through Qt's QClipboard, the same four functions as clipboard_win.py.

On X11 a clipboard is not a store but a promise: the owner keeps the data and answers other
apps' requests for it from its own event loop. QClipboard therefore must only be touched on the
GUI thread, which owns the selection; touched from elsewhere it reads stale data or never answers
a paste. Beamer's callers are the network receiver and sender threads, so every call here runs on
the GUI thread, directly when already on it and otherwise queued to it and waited on. The wait is
bounded: a wedged GUI thread costs one clipboard sync, never the link.

PNG crosses the wire as it lives on the clipboard, so the raw image/png data is handed on
untouched when the source offers it; only an image offered in another form is encoded.
"""

import logging
import threading
from typing import Optional, Tuple

from PySide6.QtCore import QBuffer, QIODevice, QMimeData, QObject, Qt, QThread, Signal
from PySide6.QtGui import QGuiApplication, QImage

LOGGER = logging.getLogger(__name__)

PNG_MIME = "image/png"
# Stamped on everything Beamer writes, with a token per write, so dataChanged can tell the echo of
# Beamer's own write from another app's copy.
OWN_FORMAT = "application/x-beamer-own"
TIMEOUT_SECONDS = 2.0

# Counts dataChanged signals. _synced_counter is its value when the clipboard was last sent to the
# other machine or written from it; None until then and after forget_sync. A switch sends the
# clipboard only when the counter has moved since, so an unchanged screenshot is not pushed across
# on every switch.
_change_counter = 0
_synced_counter = None
_own_token = 0
_connected_to = None

_runner = None
_runner_app = None
_runner_lock = threading.Lock()


class _Job:
    def __init__(self, fn, args, default):
        self.fn = fn
        self.args = args
        self.result = default
        self.done = threading.Event()
        self.cancelled = False


class _Runner(QObject):
    """Lives on the GUI thread; a queued signal is how a worker gets code run there."""

    run = Signal(object)

    def __init__(self):
        super().__init__()
        self.run.connect(self._execute, Qt.ConnectionType.QueuedConnection)

    def _execute(self, job):
        try:
            # A caller that timed out has been told False, so its write must not land late.
            if not job.cancelled:
                job.result = job.fn(*job.args)
        except Exception:
            LOGGER.exception("clipboard call failed on the GUI thread")
        finally:
            job.done.set()


def _runner_for(app):
    global _runner, _runner_app
    with _runner_lock:
        if _runner is None or _runner_app is not app:
            runner = _Runner()
            runner.moveToThread(app.thread())
            _runner, _runner_app = runner, app
        return _runner


def _on_gui(fn, *args, default):
    app = QGuiApplication.instance()
    if app is None:
        return default
    if QThread.currentThread() == app.thread():
        try:
            return fn(*args)
        except Exception:
            LOGGER.exception("clipboard call failed")
            return default
    job = _Job(fn, args, default)
    _runner_for(app).run.emit(job)
    if not job.done.wait(TIMEOUT_SECONDS):
        job.cancelled = True
        LOGGER.warning("the GUI thread did not answer a clipboard call within %s s; skipping it", TIMEOUT_SECONDS)
        return default
    return job.result


def _owns(clipboard) -> bool:
    return clipboard.ownsClipboard()


def _is_own_write(clipboard) -> bool:
    # Ownership first: asking a foreign owner for its formats is a blocking round trip to that app.
    if not _owns(clipboard):
        return False
    mime = clipboard.mimeData()
    return mime is not None and mime.hasFormat(OWN_FORMAT) and bytes(mime.data(OWN_FORMAT)) == str(_own_token).encode()


def _on_data_changed() -> None:
    global _change_counter, _synced_counter
    was_synced = _synced_counter == _change_counter
    _change_counter += 1
    # dataChanged may be delivered during setMimeData or later from the event loop: _write covers
    # the first, this covers the second, carrying the marker forward only if nothing else changed.
    if was_synced and _is_own_write(QGuiApplication.clipboard()):
        _synced_counter = _change_counter


def _connect() -> None:
    global _connected_to
    app = QGuiApplication.instance()
    if _connected_to is not app:
        QGuiApplication.clipboard().dataChanged.connect(_on_data_changed)
        _connected_to = app


def _image_to_png(image: QImage) -> Optional[bytes]:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        return None
    return bytes(buffer.data())


def _read() -> Tuple[Optional[str], Optional[bytes]]:
    _connect()
    clipboard = QGuiApplication.clipboard()
    mime = clipboard.mimeData()
    if mime is None:
        return None, None
    text = mime.text() if mime.hasText() else None
    png = None
    if mime.hasFormat(PNG_MIME):
        png = bytes(mime.data(PNG_MIME)) or None
    elif mime.hasImage():
        image = clipboard.image()
        if not image.isNull():
            png = _image_to_png(image)
    return text or None, png


def _changed() -> Tuple[Optional[str], Optional[bytes]]:
    global _synced_counter
    _connect()
    counter = _change_counter
    if counter == _synced_counter:
        return None, None
    text, png = _read()
    # Only what was read counts as sent: a failed read must not hold this clipboard back.
    if text is not None or png is not None:
        _synced_counter = counter
    return text, png


def _write(text: Optional[str], png: Optional[bytes]) -> bool:
    global _own_token, _synced_counter
    _connect()
    mime = QMimeData()
    if text is not None:
        # X11 text is LF; a Windows peer's CRLF would show as stray carriage returns.
        mime.setText(text.replace("\r\n", "\n"))
    if png is not None:
        mime.setData(PNG_MIME, png)
    _own_token += 1
    mime.setData(OWN_FORMAT, str(_own_token).encode())
    QGuiApplication.clipboard().setMimeData(mime)
    _synced_counter = _change_counter
    return True


def get_contents() -> Tuple[Optional[str], Optional[bytes]]:
    """(text, png) from the X11 clipboard, either None when absent, both None if there is no Qt
    application or reading failed for any reason. Never raises."""
    return _on_gui(_read, default=(None, None))


def changed_contents() -> Tuple[Optional[str], Optional[bytes]]:
    """get_contents(), or (None, None) when the clipboard has not changed since it was last sent
    to or written from the other machine. Either way the current contents count as sent from here
    on."""
    return _on_gui(_changed, default=(None, None))


def forget_sync() -> None:
    """A new link: the other machine may have lost what it was sent, so the next switch sends again."""
    global _synced_counter
    _synced_counter = None


def set_contents(text: Optional[str], png: Optional[bytes]) -> bool:
    """Replace the X11 clipboard with `text` and/or `png`. Returns True when at least one was set,
    False if there is no Qt application or the write failed. Never raises."""
    try:
        if png is not None and QImage.fromData(png, "PNG").isNull():
            LOGGER.warning("inbound clipboard image did not decode; setting the text only")
            png = None
        if text is None and png is None:
            return False
    except Exception:
        LOGGER.exception("failed to check an inbound clipboard image")
        return False
    return _on_gui(_write, text, png, default=False)
