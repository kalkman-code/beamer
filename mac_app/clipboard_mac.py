"""macOS clipboard access via AppKit's NSPasteboard.

`get_contents`/`set_contents` are called from background threads: the
controller's clipboard thread, and the responder's link threads when this Mac
is driven. Apple doesn't document NSPasteboard as thread-safe, and two threads
reading it at once have raised from inside AppKit, so every read and write of
the contents takes `_LOCK`.

AppKit is imported at module level (pyobjc is already a hard dependency of
this app -- bridge.py imports Quartz/objc the same way) but guarded so the
module stays importable, and its behaviour fakeable, on a machine without
it: tests replace the module-level `AppKit` attribute with a fake object
rather than mocking imports.
"""

import logging
import threading

try:
    import AppKit
except ImportError:  # pragma: no cover - exercised only off macOS
    AppKit = None

LOGGER = logging.getLogger("Beamer")

# The pasteboard's changeCount when it was last sent to the peer or written from it; None until
# then and after forget_sync. A switch sends the clipboard only when the count has moved since,
# so an unchanged screenshot is not pushed across on every switch.
_synced_count = None

# Every read and write of the contents, from whichever thread: one at a time. The change count is
# left out, so a caller that only asks whether the pasteboard moved never waits behind a slow read.
_LOCK = threading.Lock()


def _text_from_pasteboard(pasteboard):
    text = pasteboard.stringForType_(AppKit.NSPasteboardTypeString)
    return text if isinstance(text, str) and text else None


def _png_from_pasteboard(pasteboard):
    """PNG bytes for the image on the pasteboard, or None. PNG is taken as
    is; TIFF (what most apps put up, and what a Retina grab carries
    uncompressed at 25MB+) is re-encoded through NSBitmapImageRep, which
    also handles the odd TIFF a Windows consumer would never open."""
    png = pasteboard.dataForType_(AppKit.NSPasteboardTypePNG)
    if png is not None and len(png):
        return bytes(png)
    tiff = pasteboard.dataForType_(AppKit.NSPasteboardTypeTIFF)
    if tiff is None or not len(tiff):
        return None
    rep = AppKit.NSBitmapImageRep.imageRepWithData_(tiff)
    if rep is None:
        return None
    png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None)
    return bytes(png) if png is not None and len(png) else None


def get_contents():
    """(text, png) from the general pasteboard, either None when absent.
    Both are read independently and both are sent: an image copied from
    Finder or a browser usually carries a filename or URL as its text, and
    the pasting app on the other side picks the representation it wants,
    exactly as it would from the source clipboard. Never raises."""
    if AppKit is None:
        return None, None
    try:
        with _LOCK:
            pasteboard = AppKit.NSPasteboard.generalPasteboard()
            return _text_from_pasteboard(pasteboard), _png_from_pasteboard(pasteboard)
    except Exception:
        LOGGER.exception("failed to read the macOS clipboard")
        return None, None


def _change_count():
    try:
        return AppKit.NSPasteboard.generalPasteboard().changeCount()
    except Exception:
        return None


def change_stamp():
    """A number that changes whenever the pasteboard does, or None when it cannot be read."""
    return _change_count()


def changed_contents():
    """get_contents(), or (None, None) when the pasteboard has not changed since it was last sent
    to or written from the peer. Either way the current contents count as sent from here on."""
    global _synced_count
    if AppKit is None:
        return None, None
    count = _change_count()
    if count is not None and count == _synced_count:
        return None, None
    text, png = get_contents()
    # Only what was read counts as sent: a failed read must not hold this clipboard back.
    if text is not None or png is not None:
        _synced_count = count
    return text, png


def forget_sync():
    """A new link: the peer may have lost what it was sent, so the next switch sends again."""
    global _synced_count
    _synced_count = None


def set_contents(text, png):
    """Replace the general pasteboard with `text` and/or `png`. Returns True
    when at least one of them was set, False otherwise. Only the PNG
    representation is written for the image; every current app pastes it,
    and a TIFF alongside would double the memory for nothing. Never raises."""
    global _synced_count
    if AppKit is None or (text is None and png is None):
        return False
    try:
        with _LOCK:
            pasteboard = AppKit.NSPasteboard.generalPasteboard()
            pasteboard.clearContents()
            wrote = False
            if text is not None:
                wrote |= bool(pasteboard.setString_forType_(text, AppKit.NSPasteboardTypeString))
            if png is not None:
                data = AppKit.NSData.dataWithBytes_length_(png, len(png))
                wrote |= bool(pasteboard.setData_forType_(data, AppKit.NSPasteboardTypePNG))
            if wrote:
                _synced_count = pasteboard.changeCount()
        return wrote
    except Exception:
        LOGGER.exception("failed to set the macOS clipboard")
        return False
