"""core/crash_log.py: a worker thread's uncaught exception reaches the log, and a fatal signal leaves
every thread's Python stack in crash.log."""

import logging
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from core import crash_log

REPO = Path(__file__).resolve().parent.parent.parent


class Catch(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class AThreadThatRaises(unittest.TestCase):
    def setUp(self):
        before = threading.excepthook
        self.addCleanup(setattr, threading, "excepthook", before)
        self.logger = logging.getLogger("crash-log-test")
        self.logger.propagate = False
        self.catch = Catch()
        self.logger.addHandler(self.catch)
        self.addCleanup(self.logger.removeHandler, self.catch)

    def test_its_exception_and_the_thread_are_in_the_log(self):
        with tempfile.TemporaryDirectory() as directory:
            crash_log.install(directory, self.logger, fatal_signals=False)

            def fails():
                raise ValueError("lost on a worker thread")

            worker = threading.Thread(target=fails, name="Beamer-link")
            worker.start()
            worker.join()
        [record] = self.catch.records
        self.assertEqual(record.levelno, logging.CRITICAL)
        self.assertIn("Beamer-link", record.getMessage())
        self.assertIs(record.exc_info[0], ValueError)


@unittest.skipUnless(hasattr(os, "kill") and sys.platform != "win32", "a fatal signal is a POSIX test")
class AFatalSignal(unittest.TestCase):
    def test_leaves_the_python_stack_of_every_thread_in_crash_log(self):
        with tempfile.TemporaryDirectory() as directory:
            script = (
                "import os, signal, threading, time\n"
                "from core import crash_log\n"
                f"crash_log.install({directory!r}, fatal_signals=True)\n"
                "def waits_on_the_link():\n"
                "    time.sleep(30)\n"
                "threading.Thread(target=waits_on_the_link, daemon=True).start()\n"
                "time.sleep(0.2)\n"
                "os.kill(os.getpid(), signal.SIGSEGV)\n"
            )
            result = subprocess.run([sys.executable, "-c", script], cwd=REPO, capture_output=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            text = (Path(directory) / "crash.log").read_text(encoding="utf-8")
        self.assertIn("Segmentation fault", text)
        self.assertIn("waits_on_the_link", text)


if __name__ == "__main__":
    unittest.main()
