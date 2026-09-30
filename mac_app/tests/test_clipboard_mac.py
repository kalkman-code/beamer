"""clipboard_mac's pasteboard logic against a fake pasteboard, plus the one
real AppKit conversion (TIFF to PNG) run on data that never touches the
general pasteboard, so the suite leaves the machine's clipboard alone."""

import struct
import unittest
import zlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import clipboard_mac
from core import protocol


def tiny_png(width=2, height=2):
    """A valid 8-bit RGBA PNG with no filtering, built from the standard
    library so the tests need no imaging dependency."""
    rgba = bytes(range(width * height * 4))[: width * height * 4]
    raw = b"".join(b"\x00" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return protocol.PNG_SIGNATURE + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class FakePasteboard:
    def __init__(self, data=None, string=None):
        self.data = data or {}
        self.string = string
        self.cleared = 0
        self.written = []
        self.count = 1

    def changeCount(self):
        return self.count

    def stringForType_(self, kind):
        return self.string

    def dataForType_(self, kind):
        return self.data.get(kind)

    def clearContents(self):
        self.cleared += 1
        self.count += 1

    def setString_forType_(self, text, kind):
        self.written.append((kind, text))
        return True

    def setData_forType_(self, data, kind):
        self.written.append((kind, bytes(data)))
        return True


@unittest.skipIf(clipboard_mac.AppKit is None, "AppKit is only available on macOS")
class PasteboardImageTests(unittest.TestCase):
    def test_png_on_the_pasteboard_is_returned_as_is(self):
        png = tiny_png()
        board = FakePasteboard({clipboard_mac.AppKit.NSPasteboardTypePNG: png})
        self.assertEqual(clipboard_mac._png_from_pasteboard(board), png)

    def test_tiff_only_is_normalised_to_png(self):
        AppKit = clipboard_mac.AppKit
        png = tiny_png()
        rep = AppKit.NSBitmapImageRep.imageRepWithData_(AppKit.NSData.dataWithBytes_length_(png, len(png)))
        board = FakePasteboard({AppKit.NSPasteboardTypeTIFF: rep.TIFFRepresentation()})
        out = clipboard_mac._png_from_pasteboard(board)
        self.assertTrue(out.startswith(protocol.PNG_SIGNATURE))
        back = AppKit.NSBitmapImageRep.imageRepWithData_(AppKit.NSData.dataWithBytes_length_(out, len(out)))
        self.assertEqual((back.pixelsWide(), back.pixelsHigh()), (2, 2))

    def test_junk_tiff_gives_no_image(self):
        board = FakePasteboard({clipboard_mac.AppKit.NSPasteboardTypeTIFF: b"not a tiff"})
        self.assertIsNone(clipboard_mac._png_from_pasteboard(board))

    def test_empty_pasteboard_gives_no_image(self):
        self.assertIsNone(clipboard_mac._png_from_pasteboard(FakePasteboard()))

    def test_set_contents_writes_text_and_png_after_one_clear(self):
        AppKit = clipboard_mac.AppKit
        board = FakePasteboard()
        fake_appkit = type("FakeAppKit", (), {})()
        fake_appkit.NSPasteboard = type("NSPasteboard", (), {"generalPasteboard": staticmethod(lambda: board)})
        fake_appkit.NSData = AppKit.NSData
        fake_appkit.NSPasteboardTypeString = AppKit.NSPasteboardTypeString
        fake_appkit.NSPasteboardTypePNG = AppKit.NSPasteboardTypePNG
        png = tiny_png()
        real = clipboard_mac.AppKit
        clipboard_mac.AppKit = fake_appkit
        try:
            self.assertTrue(clipboard_mac.set_contents("hello", png))
        finally:
            clipboard_mac.AppKit = real
        self.assertEqual(board.cleared, 1)
        self.assertEqual(board.written, [(AppKit.NSPasteboardTypeString, "hello"), (AppKit.NSPasteboardTypePNG, png)])

    def test_set_contents_with_nothing_writes_nothing(self):
        self.assertFalse(clipboard_mac.set_contents(None, None))



@unittest.skipIf(clipboard_mac.AppKit is None, "AppKit is only available on macOS")
class ChangedContentsTests(unittest.TestCase):
    def setUp(self):
        AppKit = clipboard_mac.AppKit
        self.board = FakePasteboard(string="copied")
        fake_appkit = type("FakeAppKit", (), {})()
        fake_appkit.NSPasteboard = type("NSPasteboard", (), {"generalPasteboard": staticmethod(lambda: self.board)})
        fake_appkit.NSData = AppKit.NSData
        for name in ("NSPasteboardTypeString", "NSPasteboardTypePNG", "NSPasteboardTypeTIFF"):
            setattr(fake_appkit, name, getattr(AppKit, name))
        real = clipboard_mac.AppKit
        clipboard_mac.AppKit = fake_appkit
        clipboard_mac.forget_sync()
        self.addCleanup(setattr, clipboard_mac, "AppKit", real)
        self.addCleanup(clipboard_mac.forget_sync)

    def test_an_unchanged_clipboard_is_sent_once(self):
        self.assertEqual(clipboard_mac.changed_contents()[0], "copied")
        self.assertEqual(clipboard_mac.changed_contents(), (None, None))

    def test_a_new_copy_is_sent_again(self):
        clipboard_mac.changed_contents()
        self.board.count += 1
        self.assertEqual(clipboard_mac.changed_contents()[0], "copied")

    def test_what_the_peer_sent_is_not_sent_back(self):
        clipboard_mac.set_contents("from the pc", None)
        self.assertEqual(clipboard_mac.changed_contents(), (None, None))

    def test_a_failed_read_does_not_hold_the_clipboard_back(self):
        real = clipboard_mac.get_contents
        clipboard_mac.get_contents = lambda: (None, None)
        try:
            self.assertEqual(clipboard_mac.changed_contents(), (None, None))
        finally:
            clipboard_mac.get_contents = real
        self.assertEqual(clipboard_mac.changed_contents()[0], "copied")

    def test_a_new_link_sends_it_again(self):
        clipboard_mac.changed_contents()
        clipboard_mac.forget_sync()
        self.assertEqual(clipboard_mac.changed_contents()[0], "copied")


if __name__ == "__main__":
    unittest.main()
