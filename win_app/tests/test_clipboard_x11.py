import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

try:
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication
except ImportError:  # PySide6 is only in the Windows venv
    QApplication = None

WIN_APP = Path(__file__).resolve().parent.parent
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def make_png(colour="red", size=2) -> bytes:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(QColor(colour))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(buffer.data())


@unittest.skipIf(QApplication is None, "needs PySide6")
class ClipboardX11Test(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        import clipboard_x11

        self.cb = clipboard_x11
        self.clipboard = self.app.clipboard()
        self.real_timeout = clipboard_x11.TIMEOUT_SECONDS
        self.real_owns = clipboard_x11._owns
        self.clipboard.clear()
        clipboard_x11.forget_sync()

    def tearDown(self):
        self.cb.TIMEOUT_SECONDS = self.real_timeout
        self.cb._owns = self.real_owns
        self.app.processEvents()

    def outside_text(self, text):
        self.clipboard.setText(text)

    def spin_until(self, done, limit=3.0):
        end = time.monotonic() + limit
        while not done() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.001)

    def on_worker(self, fn):
        """Run fn on a plain thread while this (the GUI) thread spins the event loop."""
        box = {}

        def run():
            box["value"] = fn()

        thread = threading.Thread(target=run)
        thread.start()
        self.spin_until(lambda: not thread.is_alive())
        thread.join(1)
        self.assertFalse(thread.is_alive(), "worker call never returned")
        return box["value"]

    def test_text_round_trip(self):
        self.assertTrue(self.cb.set_contents("héllo\nworld", None))
        self.assertEqual(self.cb.get_contents(), ("héllo\nworld", None))

    def test_crlf_text_is_written_as_lf(self):
        self.assertTrue(self.cb.set_contents("one\r\ntwo", None))
        self.assertEqual(self.cb.get_contents(), ("one\ntwo", None))

    def test_png_round_trip_is_the_same_bytes(self):
        png = make_png()
        self.assertTrue(self.cb.set_contents(None, png))
        self.assertEqual(self.cb.get_contents(), (None, png))

    def test_image_only_clipboard_is_encoded_as_png(self):
        image = QImage(3, 2, QImage.Format.Format_ARGB32)
        image.fill(QColor("blue"))
        self.clipboard.setImage(image)
        text, png = self.cb.get_contents()
        self.assertIsNone(text)
        self.assertTrue(png.startswith(PNG_SIGNATURE))
        decoded = QImage.fromData(png, "PNG")
        self.assertEqual((decoded.width(), decoded.height()), (3, 2))

    def test_text_and_png_together(self):
        png = make_png("green")
        self.assertTrue(self.cb.set_contents("caption", png))
        self.assertEqual(self.cb.get_contents(), ("caption", png))

    def test_nothing_to_set_is_false(self):
        self.assertFalse(self.cb.set_contents(None, None))

    def test_undecodable_png_leaves_the_text_only(self):
        self.assertTrue(self.cb.set_contents("kept", b"not a png"))
        self.assertEqual(self.cb.get_contents(), ("kept", None))
        self.assertFalse(self.cb.set_contents(None, b"not a png"))

    def test_empty_clipboard_reads_as_nothing(self):
        self.assertEqual(self.cb.get_contents(), (None, None))

    def test_changed_contents_after_an_outside_change(self):
        self.outside_text("from another app")
        self.assertEqual(self.cb.changed_contents(), ("from another app", None))
        self.assertEqual(self.cb.changed_contents(), (None, None))
        self.outside_text("again")
        self.assertEqual(self.cb.changed_contents(), ("again", None))

    def test_first_call_sends_what_is_there(self):
        self.outside_text("already copied")
        self.cb.forget_sync()
        self.assertEqual(self.cb.changed_contents(), ("already copied", None))

    def test_a_failed_read_does_not_count_as_sent(self):
        self.assertEqual(self.cb.changed_contents(), (None, None))
        self.assertIsNone(self.cb._synced_counter)
        self.outside_text("now there")
        self.assertEqual(self.cb.changed_contents(), ("now there", None))

    def test_own_write_is_not_a_change(self):
        self.outside_text("old")
        self.cb.changed_contents()
        self.assertTrue(self.cb.set_contents("from the other machine", None))
        self.assertEqual(self.cb.changed_contents(), (None, None))
        self.outside_text("copied here afterwards")
        self.assertEqual(self.cb.changed_contents(), ("copied here afterwards", None))

    def test_own_write_announced_late_is_not_a_change(self):
        # dataChanged may arrive from the event loop after setMimeData has returned. The offscreen
        # platform never reports ownership, which X11 does for data this process set.
        self.cb._owns = lambda clipboard: True
        self.assertTrue(self.cb.set_contents("late", None))
        self.cb._on_data_changed()
        self.assertEqual(self.cb.changed_contents(), (None, None))

    def test_outside_change_after_a_late_own_announcement_still_counts(self):
        self.cb._owns = lambda clipboard: True
        self.assertTrue(self.cb.set_contents("mine", None))
        self.outside_text("theirs")
        self.cb._on_data_changed()
        self.assertEqual(self.cb.changed_contents(), ("theirs", None))

    def test_forget_sync_sends_again(self):
        self.outside_text("once")
        self.assertEqual(self.cb.changed_contents(), ("once", None))
        self.assertEqual(self.cb.changed_contents(), (None, None))
        self.cb.forget_sync()
        self.assertEqual(self.cb.changed_contents(), ("once", None))

    def test_worker_thread_calls_are_marshalled_to_the_gui_thread(self):
        seen = []
        real = self.cb._read

        def spy():
            seen.append(threading.current_thread() is threading.main_thread())
            return real()

        self.cb._read = spy
        try:
            self.assertTrue(self.on_worker(lambda: self.cb.set_contents("via worker", None)))
            self.assertEqual(self.on_worker(self.cb.get_contents), ("via worker", None))
            self.assertEqual(self.on_worker(self.cb.changed_contents), (None, None))
            self.outside_text("outside")
            self.assertEqual(self.on_worker(self.cb.changed_contents), ("outside", None))
        finally:
            self.cb._read = real
        self.assertEqual(seen, [True, True])

    def test_a_wedged_gui_thread_times_out_instead_of_wedging_the_caller(self):
        self.cb.TIMEOUT_SECONDS = 0.05
        box = {}

        def run():
            box["get"] = self.cb.get_contents()
            box["changed"] = self.cb.changed_contents()
            box["set"] = self.cb.set_contents("never written", None)

        thread = threading.Thread(target=run)
        started = time.monotonic()
        thread.start()
        thread.join(3)  # this thread does not process events, as a wedged GUI thread would not
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(box, {"get": (None, None), "changed": (None, None), "set": False})
        self.app.processEvents()
        self.assertEqual(self.cb.get_contents(), (None, None), "a cancelled write must not land late")

    def test_no_application_reads_and_writes_nothing(self):
        code = (
            "import clipboard_x11 as c; "
            "print(c.get_contents(), c.changed_contents(), c.set_contents('a', None)); "
            "c.forget_sync()"
        )
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=WIN_APP, env=env, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "(None, None) (None, None) False")


if __name__ == "__main__":
    unittest.main()
